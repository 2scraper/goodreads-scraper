#!/usr/bin/env python3
"""
goodreads-scraper — pyppeteer edition (secondary engine)
========================================================

The same scrape as playwright_scraper.py, driven through pyppeteer. It must
agree with its twins on exit codes, run status, and whether a run crashes or
spends money. The fetch loop that decides all three lives in page_flow.py
and is shared, so this file is browser plumbing and nothing else: how
pyppeteer navigates, reads a document, issues a fetch() and sets a cookie.

    --mode list      (default)  the books a listing shows: a Listopia list,
                                an author's books, a search, a genre shelf,
                                a series
    --mode book                 each book's own page: genres, series,
                                edition details, the rating distribution,
                                awards, characters. From a book url, or from
                                a LISTING url: every book on its --pages
    --mode reviews              a book's community reviews, through the
                                site's own GraphQL API, as deep as --pages

See playwright_scraper.py's header for what is different about this site.

Two things to know before choosing this engine:

  * **pyppeteer is effectively unmaintained** and its own README points at
    Playwright. It is here for parity, and for anyone who already has it.
  * Unlike the Selenium engine, it CAN authenticate a remote CDP endpoint
    (`browserWSEndpoint` takes a full `ws://user:pass@host:port`) and a
    proxy.

Usage
-----
    python puppeteer_scraper.py --pages 3

Requires: pip install -r requirements.txt -r requirements-puppeteer.txt
          (pyppeteer downloads its own Chromium on first run)
"""

import argparse
import asyncio
import concurrent.futures
import logging
import queue
import re
import sys
import threading
import time
from typing import Optional
from urllib.parse import urlparse

# At module level, deliberately, and not inside the launch path. The offline
# suite guards `import puppeteer_scraper` behind try/except ImportError and
# REPORTS the skip, and CI's engine-smoke job fails on any reported skip.
# That only works if importing this module actually requires the driver
# (CLAUDE.md §10).
from pyppeteer import launch, connect

from captcha_solver import detect_aws_waf, solve_recaptcha, AWS_WAF_COOKIE
from product_parser import REVIEW_SORTS, Query
from output_writer import EXIT_API_ERROR
import page_flow
from proxy_pool import (from_args as proxy_pool_from_args, mask, ROTATE_MODES,
                        ProxyError, split_credentials)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("puppeteer_scraper")

# Every await in this file goes through the bridge below with a timeout, so a
# hung remote call ends the operation instead of the run. pyppeteer provides
# no connect timeout of its own (§8).
DEFAULT_OP_TIMEOUT = 120
CONNECT_TIMEOUT = 30

# Chromium's own names for a proxy that could not be used (§8).
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

# pyppeteer's dialect of the fetch() in playwright_scraper.FETCH_JS: the
# arguments arrive POSITIONALLY rather than as one array. Same body, same
# return shape, same AbortController timeout (§8), same credentials rule
# (cookies on a same-origin request only; the API's CORS admits none).
FETCH_JS = """
async (url, method, body, timeoutMs, extra) => {
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

class _AsyncBridge:
    """Runs pyppeteer's coroutines on a private event loop, synchronously.

    Lets this engine drive page_flow's synchronous fetch loop unchanged,
    and gives every call an explicit, enforced timeout: `.result(timeout)`
    returns control even when the browser never answers, which pyppeteer's
    own API does not offer.
    """

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="pyppeteer-loop")
        self._thread.start()

    def _serve(self):
        asyncio.set_event_loop(self.loop)
        self.loop.set_exception_handler(self._on_loop_exception)
        self.loop.run_forever()

    @staticmethod
    def _on_loop_exception(loop, context):
        # pyppeteer leaves CDP calls in flight when a browser closes, and the
        # loop then logs each as an ERROR after a successful run has printed
        # its results. Only that shape is swallowed; anything else still gets
        # the default handler.
        message = " | ".join(
            str(context.get(k)) for k in ("exception", "message")
            if context.get(k))
        if any(m in message for m in (
                "Target closed", "Connection closed",
                "Task was destroyed but it is pending",
                "Future exception was never retrieved",
                "No session with given id",
                # A rejected --cdp-endpoint handshake, raised by websockets in
                # a task pyppeteer never awaits, AFTER the connect has already
                # timed out and been reported with the reason.
                "server rejected WebSocket connection",
                "Event loop is closed")):
            logger.debug("Ignoring teardown noise from pyppeteer: %s", message)
            return
        loop.default_exception_handler(context)

    def run(self, coro, timeout: Optional[float] = DEFAULT_OP_TIMEOUT):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(
                f"pyppeteer call did not return within {timeout}s")

    def close(self):
        """Stop the loop, CANCELLING what it still has in flight, on the loop
        thread, so asyncio does not print a traceback per pending task after
        a successful run."""
        def _cancel_and_stop():
            pending = [t for t in asyncio.all_tasks(self.loop)
                       if t is not asyncio.current_task(self.loop)]
            for task in pending:
                task.cancel()
            self.loop.stop()

        self.loop.call_soon_threadsafe(_cancel_and_stop)
        self._thread.join(timeout=5)


# Every `scheme://user:pass@` in a string, however many times it occurs (§8).
_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced."""
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _chrome_ua(version: str) -> str:
    """A desktop-Chrome UA naming the browser's OWN real version (§8).
    pyppeteer's `browser.version()` returns "HeadlessChrome/115.0.0.0"."""
    number = version.split("/")[-1] if "/" in version else version
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{number} Safari/537.36")


def _proxy_failure(text) -> str:
    text = str(text)
    for marker in _PROXY_ERROR_MARKERS:
        if marker in text:
            return marker
    return ""


def _proxy_for_task(args, pool) -> Optional[str]:
    """The exit an AmazonTask should solve from, or None over CDP."""
    if args.cdp_endpoint or not pool:
        return None
    return pool.current


class _Ops:
    """One pyppeteer browser + page, exposed as page_flow's named operations.

    Same contract as playwright_scraper._Ops, including the rule that a
    rotation means a genuinely FRESH browser (§8).
    """

    def __init__(self, bridge: _AsyncBridge, args, pool):
        self.bridge, self.args, self.pool = bridge, args, pool
        self.remote = bool(args.cdp_endpoint)
        self.browser = self.page = None
        self.landed = False

    def open(self):
        self.landed = False
        if self.remote:
            logger.info("Connecting to an existing browser over CDP: %s",
                        _mask_credentials(self.args.cdp_endpoint))
            err = None
            for attempt in range(1, page_flow.CDP_CONNECT_ATTEMPTS + 1):
                try:
                    self.browser = self.bridge.run(
                        connect(browserWSEndpoint=self.args.cdp_endpoint,
                                ignoreHTTPSErrors=True),
                        timeout=page_flow.CDP_CONNECT_TIMEOUT_S)
                    err = None
                    break
                except Exception as e:  # noqa: BLE001 — see below
                    err = e
                    if (attempt < page_flow.CDP_CONNECT_ATTEMPTS
                            and page_flow.cdp_should_retry(str(e))):
                        logger.warning("The Scraping Browser profile did not "
                                       "accept the connection (attempt %d/%d) "
                                       "— it may still be locked by a previous "
                                       "run; retrying in %.0fs.", attempt,
                                       page_flow.CDP_CONNECT_ATTEMPTS,
                                       page_flow.CDP_LOCKED_WAIT_S)
                        time.sleep(page_flow.CDP_LOCKED_WAIT_S)
                        continue
                    break
            if err is not None:
                e = err
                # websockets' message ("server rejected WebSocket connection:
                # HTTP 500") names neither the endpoint nor the reason.
                # Re-raised masked, with the meaning spelled out, so
                # __main__ can map it onto exit 5.
                raise RuntimeError(
                    "could not connect to --cdp-endpoint %s: %s\n%s"
                    % (_mask_credentials(self.args.cdp_endpoint),
                       _mask_credentials(str(e)),
                       page_flow.cdp_connect_hint(str(e)))) from None
            self.page = self.bridge.run(self.browser.newPage())
            self._enable_autosolve()
            return self

        launch_args = ["--no-sandbox", "--disable-dev-shm-usage",
                       f"--lang={self.args.locale}"]
        launch_kwargs = {}
        if self.args.chromium_path:
            launch_kwargs["executablePath"] = self.args.chromium_path
        credentials = None
        if self.pool:
            exit_url = self.pool.current
            # Credentials go through page.authenticate(), never onto the
            # command line: --proxy-server= is part of the browser's argv,
            # readable by anything that can run `ps` (§8).
            scrubbed, credentials = split_credentials(exit_url)
            launch_args.append(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(exit_url))
        # Signal handlers off: pyppeteer installs them inside launch(), and
        # `signal.signal` raises off the main thread, which is where this
        # event loop lives. close() handles teardown instead.
        self.browser = self.bridge.run(
            launch(headless=self.args.headless, args=launch_args,
                   ignoreHTTPSErrors=True, handleSIGINT=False,
                   handleSIGTERM=False, handleSIGHUP=False, **launch_kwargs),
            timeout=CONNECT_TIMEOUT * 2)
        self.page = self.bridge.run(self.browser.newPage())
        version = self.bridge.run(self.browser.version())
        self.bridge.run(self.page.setUserAgent(_chrome_ua(version)))
        if self.args.fingerprint:
            self._apply_fingerprint()
        if credentials:
            self.bridge.run(self.page.authenticate(
                {"username": credentials[0], "password": credentials[1]}))
        return self

    def _enable_autosolve(self):
        """The Scraping Browser API's own CAPTCHA domain, as the Playwright
        engine enables it: if the WAF ever puts its CAPTCHA in front of the
        landing, the extension can clear it before the local solver gets a
        turn. Absent from this engine until the first live run over
        --cdp-endpoint showed the difference."""
        async def _enable():
            # One coroutine for both calls: pyppeteer's CDPSession.send
            # returns a Future rather than a coroutine, and the bridge's
            # run_coroutine_threadsafe accepts only the latter ("A coroutine
            # object is required" on the first live run).
            session = await self.page.target.createCDPSession()
            await session.send("Captcha.setAutoSolve",
                               {"autoSolve": True, "options": [{"type": "*"}]})

        try:
            self.bridge.run(_enable(), timeout=30)
            logger.info("Scraping Browser API Captcha.setAutoSolve enabled.")
        except Exception as e:  # noqa: BLE001 — a non-Scraping-Browser endpoint
            logger.info("Captcha.setAutoSolve not available on this "
                        "--cdp-endpoint (%s) — relying on this script's own "
                        "detect+solve logic instead.", e)

    def _apply_fingerprint(self):
        """The SAME init script the other two engines install, so no engine
        applies a different half of one fingerprint."""
        from fingerprint_client import get_fingerprint, playwright_init_script
        fp = get_fingerprint(self.args.twocaptcha_key, tags=self.args.fp_tags,
                             country=self.args.fp_country)
        ua = (fp.get("userAgent") or {}).get("value")
        try:
            if ua:
                self.bridge.run(self.page.setUserAgent(ua))
            self.bridge.run(
                self.page.evaluateOnNewDocument(playwright_init_script(fp)))
            logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"),
                        fp.get("country"))
        except Exception as e:  # noqa: BLE001 — a fingerprint is not the run
            logger.warning("Could not apply the fingerprint (%s) — continuing "
                           "without it.", e)

    # ---- page_flow's operations -----------------------------------------

    def goto(self, url: str):
        try:
            resp = self.bridge.run(self.page.goto(
                url, waitUntil="domcontentloaded", timeout=60000), timeout=90)
        except Exception as e:  # noqa: BLE001 — pyppeteer raises several types
            raise page_flow.TransportError(_mask_credentials(str(e))) from None
        if resp is None:
            return None, None
        headers = resp.headers or {}
        return resp.status, headers.get("x-amzn-waf-action")

    def document_text(self) -> str:
        """The current document's markup, retried across the reload AWS
        WAF's challenge makes by itself (a read inside it raises)."""
        for _ in range(20):
            try:
                return self.bridge.run(self.page.content()) or ""
            except Exception as e:  # noqa: BLE001
                if "navigat" not in str(e) and "context was destroyed" not in str(e):
                    return ""
                time.sleep(0.25)
        return ""

    def wait_ms(self, ms: int) -> None:
        time.sleep(ms / 1000.0)

    def solve_captcha(self) -> bool:
        return handle_captcha_if_present(self, self.args,
                                         _proxy_for_task(self.args, self.pool))

    def fetch(self, req, timeout_ms: int = page_flow.FETCH_TIMEOUT_MS):
        try:
            got = self.bridge.run(self.page.evaluate(
                FETCH_JS, req.url, req.method, req.body_json, timeout_ms, req.headers),
                timeout=timeout_ms / 1000 + 15)
        except Exception as e:  # noqa: BLE001
            return None, "", None, _mask_credentials(str(e))
        if not isinstance(got, dict):
            return None, "", None, "fetch() returned nothing"
        if got.get("error"):
            return None, "", None, str(got["error"])
        return got.get("status"), got.get("text") or "", got.get("waf"), None

    def proxy_failure(self, text: str) -> str:
        return _proxy_failure(text)

    def relaunch(self):
        if self.remote:
            self.landed = False
            return
        try:
            self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error while closing browser: %s", e)
        self.open()

    def close(self):
        """Close the page, and on a REMOTE browser disconnect too: closing
        only the page leaves the websocket open, and its unwinding prints
        tracebacks after the output is written. The remote BROWSER is left
        running; it is not ours."""
        try:
            if self.remote:
                self.bridge.run(self.page.close(), timeout=30)
                self.bridge.run(self.browser.disconnect(), timeout=30)
            else:
                self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


def handle_captcha_if_present(ops, args, proxy: Optional[str] = None) -> bool:
    """Solve an AWS WAF CAPTCHA on the current document. Mirrors
    playwright_scraper.handle_captcha_if_present: nothing is paid for a page
    with no widget, a solver error is a warning, and the token goes into the
    `aws-waf-token` cookie on the domain the WAF's page names."""
    try:
        html = ops.bridge.run(ops.page.content())
    except Exception:  # noqa: BLE001
        return False
    challenge = detect_aws_waf(html, ops.page.url)
    if challenge is None:
        return False
    if not challenge.has_captcha_widget:
        logger.info("AWS WAF %s action and no CAPTCHA widget on the page — "
                    "not sending it to the solver; there is no puzzle to buy "
                    "an answer to.", challenge.aws_waf_action)
        return False
    logger.warning("AWS WAF CAPTCHA on %s — attempting to solve (AmazonTask%s).",
                   ops.page.url, "" if proxy else "Proxyless")
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
    domain = page_flow.cookie_domain(urlparse(ops.page.url).hostname, html)
    ops.bridge.run(ops.page.setCookie({"name": AWS_WAF_COOKIE, "value": token,
                                       "domain": domain, "path": "/"}))
    logger.info("Set %s for %s — reloading to let the WAF re-check.",
                AWS_WAF_COOKIE, domain)
    time.sleep(1.5)
    ops.bridge.run(ops.page.reload(waitUntil="domcontentloaded", timeout=60000),
                   timeout=90)
    return True


def _fetch_pages_concurrently(args, pool, query, page_nums, concurrency: int):
    """Fetch `page_nums` across `concurrency` workers. Each owns its own event
    loop, browser and exit; the page loop is page_flow.worker_loop."""
    work = queue.Queue()
    for n in page_nums:
        work.put(n)
    results, results_lock = [], threading.Lock()
    exhausted = threading.Event()

    def worker(index: int):
        name = f"worker-{index + 1}"
        bridge = _AsyncBridge()
        try:
            ops = _Ops(bridge, args, page_flow.worker_pool(pool, index)).open()
            try:
                page_flow.worker_loop(ops, args, query, work, results,
                                      results_lock, exhausted, name,
                                      _mask_credentials)
            finally:
                ops.close()
        except Exception:  # noqa: BLE001 — a dead worker must not hang the run
            logger.exception("[%s] died; its pages will be reported as failed.", name)
        finally:
            bridge.close()

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
    bridge = _AsyncBridge()
    try:
        return page_flow.run_pages(
            lambda: _Ops(bridge, args, pool).open(),
            lambda ops: ops.close(),
            lambda pages, q: _fetch_pages_concurrently(args, pool, q,
                                                    pages, concurrency),
            args, pool, args.query, concurrency, _mask_credentials)
    finally:
        bridge.close()


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="goodreads.com scraper — listings, book pages and "
                    "community reviews (pyppeteer edition)")
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
    p.add_argument("--chromium-path", default=None, metavar="PATH",
                   help="Browser executable to drive, instead of the Chromium "
                        "pyppeteer downloads for itself. Needed where that "
                        "build will not start: on an Apple Silicon Mac "
                        "pyppeteer fetches an x86_64 Chromium that runs under "
                        "Rosetta far enough to print --version and then fails "
                        "to open its DevTools socket. Point it at a Chrome or "
                        "Chromium of your own — Playwright's, if installed.")
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
    except RuntimeError as e:
        # A remote browser refusing the connection is a REMOTE API failure
        # (exit 5), not a crash in this code (exit 1).
        text = _mask_credentials(str(e))
        if "connect to --cdp-endpoint" in text:
            logger.error("%s", text)
            sys.exit(EXIT_API_ERROR)
        raise
