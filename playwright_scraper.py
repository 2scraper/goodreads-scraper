#!/usr/bin/env python3
"""
goodreads-scraper — Playwright edition (primary engine)
=======================================================

Scrapes three things goodreads.com publishes and has had no public API for
since December 2020:

    --mode list      (default)  the books a listing shows: a Listopia list,
                                an author's books, a search, a genre shelf,
                                a series
    --mode book                 each book's own page: genres, series,
                                edition details, the rating distribution,
                                awards, characters. From a book url, or from
                                a LISTING url: every book on its --pages
    --mode reviews              a book's community reviews, through the
                                site's own GraphQL API, as deep as --pages

Three engines ship in this repo and they must agree on exit codes, run
status, and whether a run crashes or spends money. The shared decisions live
in output_writer.finish_run() and page_flow.py so they cannot drift apart.

What is different about Goodreads
---------------------------------
* **AWS WAF challenges an address it has scored, and a browser passes it
  for free.** Measured 2026-09-27 from a datacentre address: a book page
  answered 202 with `x-amzn-waf-action: challenge` and an empty body to
  curl, intermittently. Real Chromium was served the page 1.7-2.4 s later by
  the WAF's own script, headless and headful, 3 of 3 each. So the engine
  waits for the challenge before judging a page (page_flow.settle), and
  pays for nothing unless the WAF shows its CAPTCHA instead.
* **/search refuses headless Chromium** (HTTP 403, an empty document, 3 of
  3) and served headful Chromium 3 of 3 from the same address. Run searches
  with --headful (under xvfb-run on a server).
* **Reviews come from the site's own GraphQL API**, not from pages. The book
  page publishes the key its front end uses, the engine lands there, reads
  it, and issues each request as a fetch() from that page with the real
  TLS stack. The API is on another origin and admits no cookies; the key
  rides in `x-api-key`, as the site's own front end sends it.
* **A listing answers a page past its end with ANOTHER page, HTTP 200**: a
  list with its last, a search with "Page 100 of 100", a shelf with its
  first. Every page is asked which page it served, and a mismatch ends the
  listing (§23).

Usage
-----
    python playwright_scraper.py --pages 3          # the Best Books Ever list

    python playwright_scraper.py --url https://www.goodreads.com/author/list/656983.J_R_R_Tolkien --pages 5

    python playwright_scraper.py --mode book --url https://www.goodreads.com/series/66175-the-lord-of-the-rings

    python playwright_scraper.py --mode reviews --url https://www.goodreads.com/book/show/5907.The_Hobbit \\
        --sort newest --pages 10

    python playwright_scraper.py --url "https://www.goodreads.com/search?q=dune" --headful

Requires: pip install -r requirements.txt -r requirements-playwright.txt
          then: playwright install chromium   (only if NOT using --cdp-endpoint)
"""

import argparse
import logging
import queue
import re
import sys
import threading
import time
from typing import Optional
from urllib.parse import urlparse

from playwright.sync_api import (sync_playwright, Error as PWError,
                                 TimeoutError as PWTimeout)

from captcha_solver import detect_aws_waf, solve_recaptcha, AWS_WAF_COOKIE
from product_parser import REVIEW_SORTS, Query
from output_writer import EXIT_API_ERROR
import page_flow
from proxy_pool import (from_args as proxy_pool_from_args, to_playwright, mask,
                        ROTATE_MODES, ProxyError)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("playwright_scraper")


# Chromium's own names for a proxy that could not be used. Distinguished
# from a timeout because the two want opposite responses (CLAUDE.md §8).
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED",
    "ERR_PROXY_AUTH_REQUESTED",
    "ERR_PROXY_CERTIFICATE_INVALID",
    "ERR_NO_SUPPORTED_PROXIES",
    "ERR_SOCKS_CONNECTION_FAILED",
    "ERR_MANDATORY_PROXY_CONFIGURATION_FAILED",
)

# The fetch() the GraphQL API goes through. It is handed to `page.evaluate`
# as a FUNCTION, which Playwright sends through `Runtime.callFunctionOn`
# rather than evaluating a string, so it works under any Content-Security-
# Policy (§18). It returns the status, the body and the WAF's own header,
# because a 202 challenge has an empty body and the header is then the only
# signal.
#
# The API lives on another origin (AppSync), and its CORS answer does not
# admit credentials, so cookies ride along only on a same-origin request;
# the key goes in `x-api-key`, exactly as the site's own front end sends it.
# The AbortController is the timeout: a browser fetch() has none of its own,
# and §8 requires every remote call to be bounded.
FETCH_JS = """
async ([url, method, body, timeoutMs, extra]) => {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const same = new URL(url, location.href).origin === location.origin;
    const init = {method, credentials: same ? "include" : "omit",
                  signal: ctl.signal,
                  headers: Object.assign({"accept": "application/json, text/plain, */*"},
                                         extra || {})};
    if (body !== null) {
      init.headers["content-type"] = "application/json";
      init.body = body;
    }
    const r = await fetch(url, init);
    return {status: r.status, text: await r.text(),
            waf: r.headers.get("x-amzn-waf-action")};
  } catch (e) {
    return {status: 0, text: "", waf: null, error: String(e)};
  } finally {
    clearTimeout(timer);
  }
}
"""


def _chrome_ua(chromium_version: str) -> str:
    """A desktop-Chrome UA naming the browser's OWN real version (§8)."""
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{chromium_version} Safari/537.36")


def _proxy_failure(exc) -> str:
    """The Chromium proxy-error name in `exc`, or "" if it is not one."""
    text = str(exc)
    for marker in _PROXY_ERROR_MARKERS:
        if marker in text:
            return marker
    return ""


def _launch_local(pw, args, pool):
    """Launch our own Chromium on `pool`'s current exit; return (browser, context, page).

    A proxy rotation tears the whole browser down and calls this again.
    Cookies a bot manager issued against one exit, replayed from another,
    are a stronger signal than either address alone (§8), and on this site
    that includes a solved `aws-waf-token`.
    """
    launch_kwargs = {"headless": args.headless}
    proxy = to_playwright(pool.current) if pool else None
    if proxy:
        launch_kwargs["proxy"] = proxy
        logger.info("Using proxy exit %s", mask(pool.current))

    browser = pw.chromium.launch(**launch_kwargs)
    ctx_kwargs = {"user_agent": _chrome_ua(browser.version), "locale": args.locale}
    init_script = None
    if args.fingerprint:
        from fingerprint_client import (get_fingerprint,
                                        playwright_context_kwargs,
                                        playwright_init_script)
        fp = get_fingerprint(args.twocaptcha_key,
                             tags=args.fp_tags, country=args.fp_country)
        ctx_kwargs.update(playwright_context_kwargs(fp))
        init_script = playwright_init_script(fp)
        logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"), fp.get("country"))

    context = browser.new_context(**ctx_kwargs)
    if init_script:
        context.add_init_script(init_script)
    return browser, context, context.new_page()


class _Ops:
    """One browser + context + page, exposed as page_flow's named operations.

    page_flow owns the fetch loop for all three engines. This class answers
    only HOW Playwright does each step. `landed` records whether the page
    sits on the book page a reviews run reads its API key from; the
    loop owns it, and a relaunch clears it.

    A rotation means a genuinely FRESH browser: cookies a bot manager issued
    against one exit, replayed from another, are a stronger signal than
    either address alone (§8), and here that includes a solved
    `aws-waf-token`.
    """

    def __init__(self, pw, args, pool, remote: bool = False):
        self.pw, self.args, self.pool, self.remote = pw, args, pool, remote
        self.browser = self.context = self.page = None
        self.landed = False

    def open(self):
        if self.remote:
            self.browser, self.context, self.page = _connect_remote(self.pw, self.args)
        else:
            self.browser, self.context, self.page = _launch_local(
                self.pw, self.args, self.pool)
        self.landed = False
        return self

    # ---- page_flow's operations -----------------------------------------

    def goto(self, url: str):
        try:
            resp = self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except (PWTimeout, PWError) as e:
            raise page_flow.TransportError(_mask_credentials(str(e))) from None
        if resp is None:
            return None, None
        try:
            waf = resp.headers.get("x-amzn-waf-action")
        except PWError:
            waf = None
        return resp.status, waf

    def document_text(self) -> str:
        """The current document's markup.

        AWS WAF's challenge reloads the page by itself, and Playwright
        refuses `content()` while a navigation is under way ("the page is
        navigating and changing the content"), so a read that lands inside
        that reload waits and tries again rather than failing the page.
        """
        for _ in range(20):
            try:
                return self.page.content()
            except PWError as e:
                if "navigating" not in str(e):
                    return ""
                time.sleep(0.25)
        return ""

    def wait_ms(self, ms: int) -> None:
        time.sleep(ms / 1000.0)

    def solve_captcha(self) -> bool:
        return handle_captcha_if_present(self.page, self.args,
                                         _proxy_for_task(self.args, self.pool))

    def fetch(self, req, timeout_ms: int = page_flow.FETCH_TIMEOUT_MS):
        try:
            got = self.page.evaluate(
                FETCH_JS, [req.url, req.method, req.body_json, timeout_ms, req.headers])
        except (PWError, PWTimeout) as e:
            return None, "", None, _mask_credentials(str(e))
        if not isinstance(got, dict):
            return None, "", None, "fetch() returned nothing"
        if got.get("error"):
            return None, "", None, str(got["error"])
        return got.get("status"), got.get("text") or "", got.get("waf"), None

    def proxy_failure(self, text: str) -> str:
        return _proxy_failure(text)

    def relaunch(self):
        """A fresh browser on the pool's current exit. On a remote browser
        only the landing is reset, since its exit is not ours to change."""
        if self.remote:
            self.landed = False
            return
        try:
            self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error while closing browser for rotation: %s", e)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.page.close()  # leave the remote browser app running
            else:
                self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


def _connect_remote(pw, args):
    """Attach to an already-running browser over CDP; return (browser, context, page)."""
    logger.info("Connecting to existing browser over CDP: %s",
                _mask_credentials(args.cdp_endpoint))
    browser, e = None, None
    for attempt in range(1, page_flow.CDP_CONNECT_ATTEMPTS + 1):
        try:
            browser = pw.chromium.connect_over_cdp(args.cdp_endpoint, timeout=30000)
            break
        except (PWError, PWTimeout) as err:
            e = err
            if attempt < page_flow.CDP_CONNECT_ATTEMPTS and page_flow.cdp_should_retry(str(err)):
                logger.warning("The Scraping Browser profile is still locked "
                               "(attempt %d/%d) — a previous run may be "
                               "releasing it; retrying in %.0fs.", attempt,
                               page_flow.CDP_CONNECT_ATTEMPTS,
                               page_flow.CDP_LOCKED_WAIT_S)
                time.sleep(page_flow.CDP_LOCKED_WAIT_S)
                continue
            break
    if browser is None:
        # The endpoint carries a password, and Playwright repeats it five
        # times in its error text (§8). Rewritten with it masked, keeping
        # host and port, which are the useful half.
        raise PWError(
            f"could not connect to --cdp-endpoint "
            f"{_mask_credentials(args.cdp_endpoint)}: "
            f"{_mask_credentials(str(e))}\n"
            f"{page_flow.cdp_connect_hint(str(e))}"
        ) from None
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()
    # The Scraping Browser API's own CAPTCHA domain
    # (https://2captcha.com/scraper/browser-api/api). If the WAF ever puts
    # its CAPTCHA in front of the landing, the extension can clear it before
    # the local solver gets a turn.
    try:
        cdp_session = context.new_cdp_session(page)
        cdp_session.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
        cdp_session.on("Captcha.detected", lambda *_: logger.info("[Scraping Browser] CAPTCHA detected on page."))
        cdp_session.on("Captcha.solveFinished", lambda *_: logger.info("[Scraping Browser] CAPTCHA solved automatically."))
        cdp_session.on("Captcha.solveFailed", lambda *_: logger.warning("[Scraping Browser] CAPTCHA auto-solve failed."))
        logger.info("Scraping Browser API Captcha.setAutoSolve enabled.")
    except Exception as e:  # noqa: BLE001
        logger.info("Captcha.setAutoSolve not available on this --cdp-endpoint (%s) — "
                    "relying on this script's own detect+solve logic instead.", e)
    return browser, context, page


# Every `scheme://user:pass@` in a string, however many times it occurs.
# Matching GLOBALLY is the point: a Playwright connection error repeats the
# endpoint five times (§8).
_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced."""
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _proxy_for_task(args, pool) -> Optional[str]:
    """The exit an AmazonTask should solve from, or None over CDP.

    AmazonTaskProxyless on a proxied run returned `existing_token` and no
    voucher in a sibling repo. 2Captcha's own exit had not been challenged,
    so there was nothing to solve (captcha_solver._v2_task_for).
    """
    if args.cdp_endpoint or not pool:
        return None
    return pool.current


def handle_captcha_if_present(page, args, proxy: Optional[str] = None) -> bool:
    """Solve an AWS WAF CAPTCHA on the current document. True if one was
    solved and the page reloaded.

    Nothing is paid for a page that holds no puzzle. The WAF's CHALLENGE
    action carries only `challenge.js`, and there is no answer to buy for
    that. A browser that runs it passes by itself, which is what the settle
    wait in `_land` is for (§19: "unsolvable" is a property of a page).
    """
    try:
        html = page.content()
    except PWError:
        return False
    challenge = detect_aws_waf(html, page.url)
    if challenge is None:
        return False
    if not challenge.has_captcha_widget:
        logger.info("AWS WAF %s action and no CAPTCHA widget on the page — "
                    "not sending it to the solver; there is no puzzle to buy "
                    "an answer to.", challenge.aws_waf_action)
        return False
    logger.warning("AWS WAF CAPTCHA on %s — attempting to solve (AmazonTask%s).",
                   page.url, "" if proxy else "Proxyless")
    if not args.twocaptcha_key:
        logger.warning("No 2captcha API key — cannot solve it. Set "
                       "TWOCAPTCHA_KEY in .env, or use a residential exit "
                       "(--proxy), which the WAF may not challenge at all.")
        return False
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                                api_version=args.captcha_api,
                                min_score=args.min_score, proxy=proxy)
    except Exception as e:  # noqa: BLE001 — a solver error is a warning (§8)
        logger.error("Solving the AWS WAF CAPTCHA failed (%s) — continuing.",
                     _mask_credentials(str(e)))
        return False
    domain = page_flow.cookie_domain(urlparse(page.url).hostname, html)
    # AWS WAF reads its answer back from a COOKIE, not a form field. Set on
    # the context so the reload carries it, and on the domain the WAF's own
    # page names (page_flow.cookie_domain): on goodreads.com that list is
    # empty, which means the page host.
    page.context.add_cookies([{"name": AWS_WAF_COOKIE, "value": token,
                               "domain": domain, "path": "/"}])
    logger.info("Set %s for %s — reloading to let the WAF re-check.",
                AWS_WAF_COOKIE, domain)
    page.wait_for_timeout(1500)
    page.reload(wait_until="domcontentloaded", timeout=60000)
    return True


def _fetch_pages_concurrently(args, pool, query: Query, page_nums, concurrency: int):
    """Fetch `page_nums` across `concurrency` workers.

    Each worker owns its own Playwright instance, browser and exit: with the
    sync API a browser belongs to the thread that made it, so sharing one is
    not an option even in principle (§7). The page loop itself is
    page_flow.worker_loop, shared by all three engines.
    """
    work = queue.Queue()
    for n in page_nums:
        work.put(n)
    results, results_lock = [], threading.Lock()
    exhausted = threading.Event()

    def worker(index: int):
        name = f"worker-{index + 1}"
        try:
            with sync_playwright() as pw:
                ops = _Ops(pw, args, page_flow.worker_pool(pool, index)).open()
                try:
                    page_flow.worker_loop(ops, args, query, work, results,
                                          results_lock, exhausted, name,
                                          _mask_credentials)
                finally:
                    ops.close()
        except Exception:  # noqa: BLE001 — a dead worker must not hang the run
            logger.exception("[%s] died; its pages will be reported as failed.", name)

    threads = [threading.Thread(target=worker, args=(i,), name=f"page-worker-{i + 1}")
               for i in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    unattempted = []
    while True:
        try:
            unattempted.append(work.get_nowait())
        except queue.Empty:
            break
    return results, sorted(unattempted), exhausted.is_set()


def scrape(args) -> int:
    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        pool = None
    concurrency = page_flow.concurrency_for(args, pool)
    with sync_playwright() as pw:
        return page_flow.run_pages(
            lambda: _Ops(pw, args, pool, remote=bool(args.cdp_endpoint)).open(),
            lambda ops: ops.close(),
            lambda pages, q: _fetch_pages_concurrently(args, pool, q,
                                                    pages, concurrency),
            args, pool, args.query, concurrency, _mask_credentials)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="goodreads.com scraper — listings, book pages and "
                    "community reviews (Playwright edition)")
    p.add_argument("--mode", choices=["list", "book", "reviews"],
                   default=None,
                   help="list: the books a listing shows (inferred from a "
                        "listing --url). book: each book's own page, with "
                        "genres, series, edition details and the rating "
                        "distribution (inferred from a /book/show/ url; with a "
                        "LISTING url, every book on its --pages). reviews: a "
                        "book's community reviews, walked as deep as --pages "
                        "goes.")
    p.add_argument("--url", default=None,
                   help="A goodreads.com page: /list/show/{id}, "
                        "/author/list/{id}, /search?q=..., /shelf/show/{name}, "
                        "/series/{id} or /book/show/{id}. Default: the Best "
                        "Books Ever list. Also read from GOODREADS_URL.")
    g = p.add_argument_group("book")
    g.add_argument("--max-books", type=int, default=None, metavar="N",
                   help="With --mode book and a listing url: read at most N of "
                        "the books the listing named (default: all of them).")
    g = p.add_argument_group("reviews")
    g.add_argument("--sort", choices=sorted(REVIEW_SORTS), default=None,
                   help="default (the site's own ordering), newest or oldest. "
                        "Not cosmetic: a capped run holds the first N reviews "
                        "by this ordering, so it decides WHICH reviews are in "
                        "the file.")
    g.add_argument("--rating", type=int, default=None, metavar="1-5",
                   help="Only reviews giving this many stars.")
    g.add_argument("--review-language", default=None, metavar="ISO",
                   help="Only reviews in this language (en, fr, es ...). "
                        "Checked against the languages the book's own page "
                        "lists: the API answers one it has no reviews in with "
                        "an EMPTY result, not an error.")
    g.add_argument("--search-text", default=None, metavar="TEXT",
                   help="Only reviews containing this text.")
    g.add_argument("--page-size", type=int, default=None, metavar="N",
                   help="Reviews per request, 1-100 (default 30, the site's "
                        "own). 200 is answered with ZERO reviews, so it is "
                        "refused.")
    p.add_argument("--pages", type=int, default=1,
                   help="Pages to fetch (100 books on a list, 30 on an "
                        "author's, 20 on a search, 30 reviews). Planned "
                        "against what page 1 states, so asking for more than "
                        "exist fetches all of them.")
    p.add_argument("--delay", type=float, default=1.0,
                   help="Delay between pages, seconds (default %(default)s)")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Fetch pages through N parallel workers (default 1). "
                        "Each worker runs its own browser and holds its own "
                        "proxy exit. Ignored with --cdp-endpoint.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page on a transport failure (default 3). "
                        "The pause doubles each time. A request the API "
                        "REFUSED is not retried: its parameters would be "
                        "refused again.")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first retry, doubling thereafter.")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="goodreads_rows", help="Output file prefix")
    p.add_argument("--locale", default="en-US",
                   help="Browser locale (default en-US). Goodreads serves "
                        "one English site; the books' own titles and reviews "
                        "are in whatever language their authors wrote.")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL, e.g. http://ACCOUNT:PASSWORD@HOST:9999 "
                        "(2captcha.com/proxy)")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line to rotate across. "
                        "Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run",
                   help="per-run (default): one exit for the whole run. "
                        "per-page: a new exit, and a fresh browser, per page.")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="Shuffle the pool at startup.")
    p.add_argument("--proxy-block-retries", type=int, default=2,
                   help="When a page is refused (403, AWS WAF), retry it "
                        "from this many OTHER exits (default 2). Needs a pool "
                        "of more than one.")
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Apply a browser fingerprint from 2captcha's "
                        "Fingerprint API. Needs --twocaptcha-key. Ignored with "
                        "--cdp-endpoint.")
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag for the fingerprint filter: "
                        "Windows, Microsoft Windows or Android. NOT a list — "
                        "Chrome, Desktop and Mobile are each rejected by the "
                        "API with 400. (default: Windows)")
    p.add_argument("--fp-country", default=None,
                   help="Fingerprint country, ISO 3166-1 alpha-2. Match it to "
                        "your proxy's exit country.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2",
                   help="Which 2captcha solver API to use (v2: createTask, "
                        "which AmazonTask needs).")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="when-blocked (default): solve an AWS WAF CAPTCHA only "
                        "when it stands between the run and the data. The WAF "
                        "is met as its CHALLENGE action, which a browser "
                        "passes for free, so 'always' behaves the same.")
    p.add_argument("--min-score", type=float, default=0.7,
                   help="reCAPTCHA v3 minimum score (0.3, 0.7 or 0.9). Kept "
                        "for parity with the family; AWS WAF has no score.")
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to an already-running browser over CDP "
                        "instead of launching Chromium, e.g. the Scraping "
                        "Browser API endpoint ws://user:pass@host:port. "
                        "--proxy and --headless/--headful are ignored.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact response the parser is given, on "
                        "success as well as failure: the page's HTML, or the "
                        "API's JSON in --mode reviews.")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    args = p.parse_args(argv)
    env_config.apply(args)
    args.query = page_flow.build_query(args, p.error)
    args.mode = args.query.mode
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key (the Fingerprint API "
                     "uses the same key, though it's a separate subscription "
                     "from solving).")
        sys.exit(2)
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the "
                       "Scraping Browser supplies its own fingerprint.")
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        logger.error("%s", e)
        sys.exit(2)
    except PWError as e:
        # A remote browser refusing the connection is a REMOTE API failure
        # (exit 5), not a crash in this code (exit 1).
        text = _mask_credentials(str(e))
        if "profile_locked" in text or "connect to --cdp-endpoint" in text:
            logger.error("%s", text)
            sys.exit(EXIT_API_ERROR)
        raise
