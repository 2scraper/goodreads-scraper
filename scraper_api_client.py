#!/usr/bin/env python3
"""
goodreads-scraper — 2captcha Scraper API edition (fourth engine)
================================================================

A fourth way to run this scraper. Unlike the three browser engines, this one
manages **no browser and no CDP session of its own**: it POSTs a URL to
2captcha's separate **Scraper API** (https://scraper.2captcha.com, a
different product from the Scraping Browser API the other three reach
through --cdp-endpoint), gets the rendered page back over plain HTTPS, and
feeds it to this project's product_parser.

Why you would want it: no Chromium to install, runs from a tiny container or
a lambda, and the page is fetched from the API's exits rather than yours —
which matters on this site, where AWS WAF challenges an address it has
scored (product_parser's docstring).

WHAT THIS CLIENT READS
----------------------
    --mode list    a listing url, --pages of it, with the same "which page
                   did the site serve" check the browser engines make
    --mode book    ONE book url

**--mode reviews is not implemented by this client.** Reviews come from the
site's GraphQL API, which answers POST only, and the Scraper API fetches a
URL. The browser engines implement it.

Usage
-----
    python3 scraper_api_client.py --url https://www.goodreads.com/list/show/1.Best_Books_Ever --pages 2

    # the key comes from $TWOCAPTCHA_KEY and a CDP endpoint from
    # $GOODREADS_CDP_ENDPOINT, so neither needs to be typed — a secret in
    # argv is readable by anything that can run `ps`

Requires: pip install -r requirements.txt
          (no playwright/selenium/pyppeteer needed for this engine)
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from typing import Optional

import requests

from product_parser import (Query, detect_page_state, page_totals,
                            parse_page, query_from_url, request_for,
                            requested_page, served_page)
from output_writer import dedupe_by_key, finish_run, SOURCE_DEFAULT
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("scraper_api_client")

API_BASE = "https://scraper.2captcha.com"
SYNC_ENDPOINT = f"{API_BASE}/tasks/sync"

# The API caps `timeout` at 120s and rejects bodies over 10,000 bytes.
MAX_API_TIMEOUT = 120

# Exit codes. Kept distinct from 2 (bad usage) on purpose: a remote API
# failing is not the operator passing wrong arguments, and a harness that
# lumps them together sends you looking in the wrong place. An early run
# reported `exit=2` for an HTTP 422 from the API — which reads as "you called
# it wrong".
#
# Imported rather than redefined: the browser engines return the same code for
# a Scraping Browser that will not accept a connection, and two definitions
# of one exit code is how a family's contract drifts.
from output_writer import EXIT_API_ERROR  # noqa: E402

def _mask_credentials(url: str) -> str:
    """Never print a username:password embedded in a ws://... or http://... URL."""
    if "@" not in url:
        return url
    scheme_sep = url.find("://")
    if scheme_sep == -1:
        return url
    scheme, rest = url[:scheme_sep + 3], url[scheme_sep + 3:]
    _, _, host_part = rest.partition("@")
    return f"{scheme}***:***@{host_part}"


# Credentials embedded ANYWHERE in a blob of text, not just in a string that
# is entirely a URL — and every occurrence, not the first. A masker that
# handles one occurrence prints the password the other four times and looks
# like it is working.
_CREDS_IN_TEXT_RE = re.compile(r"([a-z][a-z0-9+.-]*://)[^/\s'\"@]+@", re.IGNORECASE)
# Same shape as captcha_solver's and fingerprint_client's. A third copy is
# one too many and they should be unified in a family pass; reaching into
# another module's private name to avoid it would be worse.
_KEY_IN_TEXT_RE = re.compile(
    r"((?:client)?key|token|api[_-]?key)=([^&\s'\"]{6,})", re.IGNORECASE)


def _redact_debug_header(value: str) -> str:
    """The x-debug header, safe to log.

    SECURITY.md names this header as one of three places credentials reach a
    log unmasked, and it was logged verbatim: the API echoes back the task it
    ran, so a run driven through a credentialed CDP endpoint put that
    endpoint's username and password into the log, and a key passed as a
    query parameter would go the same way.

    Redaction rather than an allowlist of fields, deliberately: the header is
    the API's own metadata and its shape is not ours to pin, so an allowlist
    would silently drop the cost and timing figures this is logged FOR the
    first time the API adds a field.
    """
    return _KEY_IN_TEXT_RE.sub(r"\1=***",
                               _CREDS_IN_TEXT_RE.sub(r"\1***:***@", value))


def _build_wait_for(args) -> Optional[dict]:
    """`waitFor` is an OBJECT. Measured 2026-09-23 against the live
    /tasks/sync endpoint: the JSON-encoded string form this client used to
    send was answered with HTTP 422 ("params.waitFor must be an object")
    and was still billed ($0.0005); the same request with an object
    answered HTTP 200. The earlier note here, that the API wanted a
    double-encoded string, no longer describes the API.

    Default (no flag): wait for the DOM. On a challenge-protected page
    that resolves instantly against the challenge page itself — which is
    exactly the trap documented in this module's docstring, so
    --wait-text/--wait-element exist to wait on something only the real
    page can contain."""
    if args.wait_text:
        return {"text": args.wait_text}
    if args.wait_element:
        return {"element": args.wait_element, "checkVisible": True}
    if args.wait_state:
        return {"state": args.wait_state}
    return None


def fetch_html(args) -> str:
    payload = {
        "task_type": "scrape",
        "url": args.url,
        "data_format": "raw",   # we want HTML; product_parser does the rest
        "format": "json",       # {"status": verdict, "http_code": target status, "headers", "body"}
        "timeout": min(args.timeout, MAX_API_TIMEOUT),
    }

    wait_for = _build_wait_for(args)
    if wait_for:
        payload["waitFor"] = wait_for
        logger.info("waitFor: %s", json.dumps(wait_for))

    if args.cdp_url:
        payload["cdpurl"] = args.cdp_url
        logger.info("Routing through an existing browser session: %s",
                    _mask_credentials(args.cdp_url))

    logger.info("POST %s (url=%s)", SYNC_ENDPOINT, args.url)
    resp = requests.post(
        SYNC_ENDPOINT,
        headers={"Authorization": f"Bearer {args.key}", "Content-Type": "application/json"},
        json=payload,
        # Give the HTTP call more headroom than the API-side task timeout,
        # otherwise a task that legitimately runs the full 120s looks like
        # a client-side network failure.
        timeout=min(args.timeout, MAX_API_TIMEOUT) + 30,
    )

    # The API returns its own per-task metadata (price, timings, status)
    # in an x-debug header — worth logging, it's the only place the real
    # cost of the call shows up.
    debug = resp.headers.get("x-debug")
    if debug:
        logger.info("x-debug: %s", _redact_debug_header(debug))

    if resp.status_code != 200:
        # 422 = task ran but errored (this is what a bad/unreachable
        # cdpurl produces: "CDP connect failed (user cdpurl) after N
        # attempts"); 402 = out of balance; 408 = sync wait exceeded.
        raise RuntimeError(
            f"Scraper API returned HTTP {resp.status_code}: {resp.text[:500]}"
        )

    body = resp.json()
    html = body.get("body") or ""
    # The TARGET's HTTP status is `http_code`. `status` is the API's own
    # verdict string ("success"), measured 2026-09-23 -- passing it on
    # handed the page classifier a string, so a target 403/503 was never
    # seen. Fall back to `status` only if it is itself an integer.
    upstream_status = body.get("http_code")
    if not isinstance(upstream_status, int):
        legacy = body.get("status")
        upstream_status = legacy if isinstance(legacy, int) and not isinstance(legacy, bool) else None
    logger.info("Upstream page HTTP status %s, %d bytes of HTML.", upstream_status, len(html))
    # The STATUS is returned alongside the HTML, not thrown away. It used to
    # be, and that cost this engine the family's central distinction. On this
    # site a refusal carries no markup at all — nothing a challenge check
    # on it, so the challenge check below finds nothing and the run fell
    # through to "0 products" and exit 4. A pipeline branching on the exit
    # code then reads a block as an empty category. See detect_page_state,
    # which the three browser engines already reach through page_flow.
    return html, upstream_status


DEFAULT_URL = "https://www.goodreads.com/list/show/1.Best_Books_Ever"


def main() -> int:
    args = parse_args()
    if not args.key:
        logger.error("No 2captcha API key. Pass --key, or better, export TWOCAPTCHA_KEY.")
        return 2
    query: Query = args.query

    rows, seen, pages_done, failed = [], set(), 0, []
    stop_reason, blocked, total, available = "completed", False, None, None
    plan = args.pages if query.mode == "list" else 1
    page = 1
    while page <= plan:
        req = request_for(query, page)
        args.url = req.url
        rc, text, status = _fetch_once(args)
        if rc:
            failed.append(page)
            stop_reason = "blocked_aws-waf" if rc == 3 else "page_load_timeout"
            blocked = rc == 3
            break
        state = detect_page_state(text, status, req.url)
        if state not in ("content", "empty"):
            logger.error("Page %d came back as %s (upstream HTTP %s).", page,
                         state, status)
            failed.append(page)
            blocked = state in ("challenge", "blocked")
            stop_reason = ("blocked_aws-waf" if state == "challenge" else
                           "blocked_http-403" if state == "blocked" else
                           "page_load_timeout")
            break
        if query.mode == "list" and state == "content":
            got_page = served_page(text, query.kind)
            if got_page is not None and got_page != requested_page(req.url):
                # A list and a search answer a page past their end with
                # their LAST page (product_parser's docstring).
                logger.info("Asked for page %d, the site served page %d — the "
                            "listing ended.", requested_page(req.url), got_page)
                stop_reason = "end_of_listing"
                break
        got = parse_page(text, query, page) if state == "content" else []
        pages_done += 1
        if page == 1:
            total, available = page_totals(text, query)
            if query.mode == "list" and available:
                plan = max(1, min(args.pages, available))
        logger.info("Parsed %d row(s) from page %d.", len(got), page)
        if not got:
            stop_reason = "end_of_listing" if page > 1 else "completed"
            break
        rows.extend(dedupe_by_key(got, seen))
        page += 1
        if page <= plan:
            time.sleep(args.delay)

    return finish_run(rows, args.out, args.format, args.allow_empty,
                      blocked=blocked, stop_reason=stop_reason,
                      pages_requested=args.pages, pages_completed=pages_done,
                      pages_failed=failed, mode=query.mode,
                      source=SOURCE_DEFAULT,
                      start_url=query.url,
                      final_url=request_for(query, max(1, pages_done)).url,
                      extra={"total_results": total,
                             "pages_available": available,
                             "query": {"kind": query.kind, "url": query.url},
                             "engine": "scraper_api"})


def _fetch_once(args):
    """(rc, page_text, upstream_status). rc is 0 on a fetch that returned,
    EXIT_API_ERROR on the API failing, 3 on a WAF page after the retries."""
    attempts = max(1, args.retries + 1)
    for attempt in range(1, attempts + 1):
        try:
            body, upstream_status = fetch_html(args)
        except requests.RequestException as e:
            logger.error("Network error talking to the Scraper API: %s", e)
            return EXIT_API_ERROR, "", None
        except RuntimeError as e:
            logger.error("%s", e)
            return EXIT_API_ERROR, "", None
        text = body
        if args.dump_html:
            with open(args.dump_html, "w", encoding="utf-8") as f:
                f.write(text)
            logger.info("Response written to %s", args.dump_html)
        if detect_page_state(text, upstream_status, args.url) != "challenge":
            return 0, text, upstream_status
        if attempt < attempts:
            logger.info("AWS WAF page on attempt %d/%d — retrying in %ds.",
                        attempt, attempts, args.retry_delay)
            time.sleep(args.retry_delay)
    logger.error("The Scraper API's fetch met AWS WAF on every attempt. Route "
                 "it through a Scraping Browser session (--cdp-url), or use a "
                 "browser engine.")
    return 3, "", None


def parse_args():
    p = argparse.ArgumentParser(
        description="goodreads.com scraper — 2captcha Scraper API edition (no "
                    "local browser). Reads listings and book pages; --mode "
                    "reviews needs the site's POST-only GraphQL API and is "
                    "not implemented by this client.")
    # NOT required: prefer the TWOCAPTCHA_KEY env var (a key in argv is
    # visible to anyone who can run `ps`).
    p.add_argument("--key", default=os.environ.get("TWOCAPTCHA_KEY"),
                   help="2captcha.com API key (sent as a Bearer token). "
                        "Defaults to $TWOCAPTCHA_KEY, which is the safer way to pass it.")
    p.add_argument("--url", default=None,
                   help="A listing or book url (default: the Best Books Ever "
                        "list). Also read from GOODREADS_URL.")
    p.add_argument("--mode", choices=["list", "book", "reviews"], default=None,
                   help="Inferred from --url. reviews is refused with the "
                        "reason: its API answers POST only.")
    p.add_argument("--pages", type=int, default=1,
                   help="Listing pages to fetch (default 1). Each is a "
                        "separate billable task.")
    p.add_argument("--delay", type=float, default=1.0,
                   help="Seconds between pages (default 1.0)")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="goodreads_rows_scraperapi", help="Output file prefix")
    p.add_argument("--timeout", type=int, default=60,
                   help=f"API-side task timeout in seconds (1-{MAX_API_TIMEOUT}, default 60)")
    p.add_argument("--cdp-url", default=None,
                   help="Route the fetch through an existing browser session over CDP "
                        "(sent as the API's `cdpurl` param), e.g. ws://user:pass@host:port")
    wait = p.add_mutually_exclusive_group()
    wait.add_argument("--wait-text", default=None,
                      help="Wait until this string appears on the page.")
    wait.add_argument("--wait-element", default=None,
                      help="Wait until this CSS selector is visible.")
    wait.add_argument("--wait-state", choices=["load", "domcontentloaded"], default=None,
                      help="Wait for a page load state instead of specific content")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were parsed.")
    p.add_argument("--retries", type=int, default=1,
                   help="Extra attempts if a WAF page comes back. Each attempt is "
                        "a separate billable task, so this defaults to 1.")
    p.add_argument("--retry-delay", type=int, default=10,
                   help="Seconds between retries (default 10)")
    p.add_argument("--dump-html", default=None,
                   help="Also write the page to this path, even on success")
    args = p.parse_args()
    # This client uses --key and --cdp-url rather than --twocaptcha-key and
    # --cdp-endpoint, so the env mapping is spelled out instead of defaulted.
    env_config.apply(args, keys={
        "TWOCAPTCHA_KEY": "key",
        "GOODREADS_CDP_ENDPOINT": "cdp_url",
        "GOODREADS_URL": "url",
    })
    query, why = query_from_url(args.url or DEFAULT_URL)
    if query is None:
        p.error(why)
    if args.mode:
        query.mode = args.mode
    if query.mode == "reviews":
        p.error("--mode reviews is not implemented by this client: reviews come "
                "from the site's GraphQL API, which answers POST only, and the "
                "Scraper API fetches a URL. Use playwright_scraper.py, "
                "puppeteer_scraper.py or selenium_scraper.py for it.")
    if query.mode == "book" and query.kind != "book":
        p.error("--mode book here reads ONE book url. The browser engines read "
                "every book on a listing; this client does not.")
    why = query.validate()
    if why:
        p.error(why)
    if args.pages < 1:
        p.error("--pages must be at least 1")
    args.query = query
    return args


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
