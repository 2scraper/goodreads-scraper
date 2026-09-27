"""
page_flow.py
------------
The retry / solve / blocked decision, as DATA rather than as three copies of
an if-chain (CLAUDE.md §1), and the fetch loop the three engines share.

goodreads.com answers this repo's requests in nine ways, and they want seven
different responses:

    a listing or book page with books on it           -> parse
    the site's own "No results." / 404 / a book id    -> parse, it is an
      that names nothing                                 answer (exit 4)
    AWS WAF's challenge (202 + x-amzn-waf-action)     -> let the browser run
                                                         it, ~2 s; then solve
                                                         or rotate
    the GraphQL API refusing the PARAMETERS           -> stop: a retry sends
                                                         them again
    the GraphQL API refusing the KEY (401)            -> land again: the page
                                                         hands out a fresh one
    429 / 503                                         -> wait, same exit
    403 (headless Chromium on /search)                -> rotate, or --headful
    anything else                                     -> retry

Three copies of that triage across three engines would drift, and the drift
would be silent: one engine reporting exit 3 where its twin reports exit 0
on the same response.

Nothing here imports a browser, and **no JavaScript crosses this boundary**
(§1). Each engine spells its fetch() in its own driver's dialect.
"""

import copy
import logging
import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from output_writer import dedupe_by_key, finish_run, SOURCE_DEFAULT
from product_parser import (ADDRESSABLE_KINDS, MAX_PAGES, REVIEW_SORTS,  # noqa: F401
                            Query, api_error, book_landing, book_url,
                            check_review_language, detect_bot_challenge,
                            detect_page_state, landing_url, next_cursor,
                            page_totals, parse_page, query_from_url,
                            request_for, requested_page, served_page)

log = logging.getLogger("page_flow")


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------

# How long one fetch() may take before the engine gives up on it. A 100-
# review GraphQL page arrived in well under a second (2026-09-27). The bound
# exists because a browser fetch() has no timeout of its own, and CLAUDE.md
# §8 requires every remote call to have one.
FETCH_TIMEOUT_MS = 30_000

# How long to wait at the SAME exit after a 429/503 before trying again. No
# throttle was met in this repo's measurements; the values are the family's.
THROTTLE_WAIT_S = 10.0
THROTTLE_RETRIES = 2

# How long to let AWS WAF's own challenge script run before judging a page,
# and how often to look. The challenge action computes a token and reloads
# the page by itself: real Chromium was served the book page 1.7-2.4 s after
# the 202, headless and headful, 3 of 3 each (2026-09-27). Polled rather
# than slept, so a page that was never challenged costs nothing.
CHALLENGE_SETTLE_MS = 15_000
CHALLENGE_POLL_MS = 500

# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------

def classify(html: Optional[str], status: Optional[int] = None,
             url: str = "", waf_action: Optional[str] = None) -> str:
    """Name what the site answered with. See product_parser.detect_page_state.

    The argument ORDER is the contract: every engine calls
    `classify(html, status, url, waf_action)`. A sibling repo shipped
    `classify(html, url=...)` in two of three engines against a callee that
    took `status` second, and both crashed on their first fetch (§17).
    `smoke_test.py` binds every engine's call against this signature for
    that reason.
    """
    return detect_page_state(html or "", status, url, waf_action)


STATE_POLICY = {
    "content":      {"retry": False, "solve": False, "blocked": False, "parse": True},
    # A listing or a search with nothing in it, an author list asked for a
    # page past its end (HTTP 404, the site's own page), a book id that
    # names no book. The site served exactly what was asked for, so this is
    # EXIT_NO_PRODUCTS rather than EXIT_BLOCKED.
    "empty":        {"retry": False, "solve": False, "blocked": False, "parse": True},
    # The GraphQL API refused the PARAMETERS ("Variable 'sort' has an
    # invalid value."). The same request sent again gets the same answer,
    # and no exit or solve changes it.
    "rejected":     {"retry": False, "solve": False, "blocked": False, "parse": False},
    # The API refused the KEY: 401 "Valid authorization header not
    # provided." The key is the one the book page hands every visitor, and
    # an AppSync key expires, so the answer is to land again and read the
    # page's current one. Not blocked: nobody refused this address.
    "unauthorized": {"retry": True,  "solve": False, "blocked": False, "parse": False},
    # AWS WAF. Its CHALLENGE action is a script a real browser passes by
    # itself (the settle wait); its CAPTCHA action is solvable (AmazonTask).
    # A fresh exit clears either, hence retry.
    "challenge":    {"retry": True,  "solve": True,  "blocked": True,  "parse": False},
    # Rate limited. The retry happens at the same exit after a wait
    # (THROTTLE_*). NOT counted as blocked: calling a throttle a block
    # reports exit 3 for a page that was about to come back (§24).
    "throttled":    {"retry": True,  "solve": False, "blocked": False, "parse": False},
    # HTTP 403. Headless Chromium on /search got this with a 39-byte body,
    # 3 of 3, while headful was served 3 of 3 (2026-09-27).
    "blocked":      {"retry": True,  "solve": False, "blocked": True,  "parse": False},
    # Not a page this parser recognises and not an interstitial either.
    # Worth one more try.
    "unknown":      {"retry": True,  "solve": False, "blocked": False, "parse": False},
}


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["retry"]


def should_solve(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["solve"]


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["blocked"]


def should_parse(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["parse"]


# Whether a blocked page is worth re-fetching at all. CONSULTED by every
# engine, so setting it False really does stop the retry loop (§17).
RETRY_ON_BLOCKED = True

# How many times to re-fetch a blocked page when there is no proxy pool to
# rotate into. One: a WAF decision is about the address and the session, and
# a second request from a FRESH browser re-rolls the session half. WITH a
# pool the engines retry once per remaining exit instead.
BLOCK_RETRIES_WITHOUT_POOL = 1

# At most one solve per page. A challenge that survives a solved token is not
# a challenge this run can pass, and a second solve is a second charge for
# the same answer.
SOLVES_PER_PAGE = 1


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def pages_to_plan(pages_requested: int, pages_available: Optional[int]) -> int:
    """How many pages a run may ask for, given what page 1 reported.

    Every addressable listing states its last page on page 1 (a list's
    pagination, "Page 1 of 100" on a search), and a reviews feed states its
    total. Walking past the end is not harmless here: a list and a search
    answer an out-of-range page with their LAST page, which would be
    re-collected for as long as the run was asked to go on.
    """
    ceiling = MAX_PAGES if pages_available is None else min(pages_available, MAX_PAGES)
    return max(1, min(int(pages_requested), ceiling))


def concurrency_limit(cdp_endpoint: Optional[str]) -> Optional[int]:
    """1 when workers would collide, else None for "no limit imposed here".

    The Scraping Browser API allows ONE live connection per profile, so N
    workers sharing a `pid` collide with `profile_locked`. Several `pid`s,
    one run each, is the way to parallelise that path (§7).
    """
    return 1 if cdp_endpoint else None


def addressable(query: Query) -> bool:
    """Whether pages 2..N can be handed to workers. A reviews feed is walked
    by the cursor page N-1 returned, so page 5's address does not exist
    until page 4 has been read; a shelf and a series are one page each."""
    if query.mode == "reviews":
        return False
    if query.mode == "book":
        return True
    return query.kind in ADDRESSABLE_KINDS


# ---------------------------------------------------------------------------
# Refusals: how they are named and what the reader is told
# ---------------------------------------------------------------------------

def refusal_name(state: str) -> str:
    """The name a refusal is reported by, in logs and in `stop_reason`."""
    return {"challenge": "aws-waf", "blocked": "http-403"}.get(state, state)


def refusal_advice(state: str, url: str = "", headless: bool = False) -> str:
    """One sentence on what changes the answer, per refusal. Kept here so
    the three engines cannot give three different pieces of advice."""
    if "/search" in url and headless:
        return ("/search refuses HEADLESS Chromium (HTTP 403, an empty "
                "document; 3 of 3, 2026-09-27) and served headful Chromium 3 "
                "of 3 from the same address. Re-run with --headful (under "
                "xvfb-run on a server).")
    if state == "challenge":
        return ("AWS WAF challenged this session and it did not clear. Its "
                "usual action is a script a real browser passes by itself in "
                "about two seconds, so a page still challenged after the "
                "wait was given the CAPTCHA action or refused outright. A "
                "different exit (--proxy) changes the answer; TWOCAPTCHA_KEY "
                "lets its CAPTCHA be solved (AmazonTask).")
    return ("The site refused this address (HTTP 403). A different exit is "
            "what changes that: --proxy / --proxy-file, or --cdp-endpoint.")


def stop_reason_for(outcome) -> str:
    """The run's stop_reason when `outcome` is the page that ended it."""
    if getattr(outcome, "rejected", None):
        return "api_rejected"
    if getattr(outcome, "blocked_by", None):
        return "blocked_%s" % outcome.blocked_by
    if getattr(outcome, "state", None) == "throttled":
        return "throttled"
    return "page_load_timeout"


# ---------------------------------------------------------------------------
# The query, and the end of a run
# ---------------------------------------------------------------------------

# The review flags, by the argparse dest they land in. A flag left at None
# was not typed, which is how build_query tells a user's value from a
# default, and refuses one on a mode it would do nothing in.
REVIEW_FLAGS = (("sort", "--sort"), ("rating", "--rating"),
                ("review_language", "--review-language"),
                ("search_text", "--search-text"), ("page_size", "--page-size"))

DEFAULT_URL = "https://www.goodreads.com/list/show/1.Best_Books_Ever"


def build_query(args, error: Callable[[str], None]) -> Query:
    """The Query a run sends, from --url and --mode, validated.

    `error` is argparse's `p.error`, so a refusal is exit 2 with the usage
    line, as in every engine.
    """
    url = args.url or DEFAULT_URL
    query, why = query_from_url(url)
    if query is None:
        error(why)
    if args.mode:
        if args.mode == "list" and query.kind == "book":
            error("--mode list needs a listing url; %s is a book page. Use "
                  "--mode book or --mode reviews for it." % url)
        query.mode = args.mode
    typed = [flag for dest, flag in REVIEW_FLAGS
             if getattr(args, dest, None) is not None]
    if typed and query.mode != "reviews":
        error("%s only mean something with --mode reviews." % ", ".join(typed))
    if query.mode == "reviews":
        query.sort = args.sort or "default"
        query.rating = args.rating
        query.review_language = args.review_language
        query.search_text = args.search_text
        if args.page_size is not None:
            query.page_size = args.page_size
    if getattr(args, "max_books", None) is not None:
        if query.mode != "book" or query.kind == "book":
            error("--max-books only means something with --mode book and a "
                  "LISTING url.")
        if args.max_books < 1:
            error("--max-books must be at least 1")
        query.max_books = args.max_books
    why = query.validate()
    if why:
        error(why)
    if args.pages < 1:
        error("--pages must be at least 1")
    if args.concurrency > 1 and query.mode == "reviews":
        error("--concurrency cannot apply to --mode reviews: the feed is "
              "walked by the cursor each page returns, so page N's address "
              "does not exist until page N-1 has been read (§18).")
    return query


def query_summary(query: Query) -> dict:
    """The query as the sidecar records it: only the fields its mode uses."""
    out = {"kind": query.kind, "url": query.url}
    if query.mode == "reviews":
        out.update({"sort": query.sort, "rating": query.rating,
                    "review_language": query.review_language,
                    "search_text": query.search_text,
                    "page_size": query.page_size})
    if query.mode == "book" and query.kind != "book":
        out.update({"max_books": query.max_books,
                    "books_planned": len(query.book_urls)})
    return out


def merge_outcomes(outcomes: List, key: str = "sku") -> List:
    """Rows of `outcomes` in PAGE order, duplicates dropped (§8)."""
    rows, seen = [], set()
    for oc in sorted(outcomes, key=lambda o: o.page_num):
        fresh = dedupe_by_key(oc.products, seen, key=key)
        if len(fresh) < len(oc.products):
            log.info("Page %d: dropped %d duplicate row(s) — the live listing "
                     "moved between page fetches.", oc.page_num,
                     len(oc.products) - len(fresh))
        rows.extend(fresh)
    return rows


def finish(args, query: Query, outcomes: List, stop_reason: str,
           blocked: bool, listing: Optional[List] = None) -> int:
    """Merge the pages in PAGE order, write the output, return the exit code.

    One implementation for the three engines, so the merge order, the
    dedupe and the sidecar cannot differ between them (§6). `listing` is
    phase A of a book run started from a listing: its pages are what
    `pages_requested` describes, and a listing page that failed is a
    failed page of the run.
    """
    rows = merge_outcomes(outcomes)
    first_pages = listing if listing is not None else outcomes
    repeated = sum(len(o.products) for o in outcomes) - len(rows)
    first = next((o for o in first_pages if o.page_num == 1), None)
    total = getattr(first, "total_available", None)
    available = getattr(first, "pages_available", None)
    ok_pages = [o for o in first_pages if o.ok]
    failed_pages = sorted(o.page_num for o in first_pages if not o.ok)
    extra = {"total_results": total, "pages_available": available,
             "query": query_summary(query)}
    if query.mode == "list":
        # A listing states far more than it serves: a list says 79,607
        # books and serves 100 pages of 100. "complete" then means "every
        # page the site will serve", which is not the whole listing, and the
        # sidecar says which (§21).
        per = {"list": 100, "author": 30, "search": 20, "shelf": 50,
               "series": 100}[query.kind]
        reachable = (available or 1) * per
        extra["reachable_max"] = reachable
        extra["capped_by_site"] = bool(total and total > reachable)
    extra["repeated_across_pages"] = repeated
    if repeated and query.mode == "list":
        # Measured 2026-09-27 on Tolkien's author list, 27 pages, 791 slots:
        # sorted by popularity it served 720 distinct books and repeated 71,
        # identically on two runs; sorted by title, 739 and 52. The union of
        # the two orderings was exactly 791. So a repeat is not a listing
        # moving under the run: the site's paging over tied sort keys shows
        # some books twice and the same number of others NEVER (§21:
        # "complete" is not "exhaustive").
        log.warning("%d book(s) appeared on more than one page, so the site "
                    "never served %d others under this ordering. Another "
                    "ordering serves a different set (on an author list, "
                    "?sort=title); the union of two runs is the whole list.",
                    repeated, repeated)
    if listing is not None:
        failed_books = sorted(o.page_num for o in outcomes if not o.ok)
        extra["books_planned"] = len(query.book_urls)
        extra["books_failed"] = failed_books
        if failed_books:
            # A book page that failed is missing data like a listing page
            # that failed, and the sidecar must not call the run complete.
            failed_pages = failed_pages + ["book-%d" % n for n in failed_books]
    if rows and total:
        log.info("The site reports %d match(es); this run holds %d (%.1f%%).",
                 total, len(rows), 100.0 * len(rows) / total)
    last_ok = max([o.page_num for o in ok_pages] or [1])
    final = (request_for(query, last_ok).url if query.mode != "book"
             or query.kind == "book" else query.url)
    return finish_run(
        rows, args.out, args.format, args.allow_empty,
        blocked=blocked, stop_reason=stop_reason,
        pages_requested=args.pages, pages_completed=len(ok_pages),
        pages_failed=failed_pages, mode=query.mode, source=SOURCE_DEFAULT,
        start_url=query.url, final_url=final, extra=extra)


_WAF_COOKIE_DOMAINS_RE = re.compile(r"awsWafCookieDomainList\s*=\s*\[([^\]]*)\]")


def cookie_domain(host: Optional[str], html: Optional[str] = None) -> str:
    """The domain an `aws-waf-token` cookie is set on: the one the SITE says.

    AWS WAF's own integration names it on the challenge page, in
    `window.awsWafCookieDomainList`, and the token cookie goes on the listed
    domain that covers the page's host, or on the host itself when the list
    is empty. Measured 2026-09-24:

        binance.com         ['binance.com','binance.bh', ...]  -> .binance.com
        transfermarkt.com   []                                 -> the host
        goodreads.com       []                                 -> the host
                            (its challenge page, 2026-09-27)

    On binance, a token scoped to www.binance.com alone left the page on
    "Human Verification", and the same token on .binance.com was served. So
    "the registrable domain" was binance's list, not a rule, and a site with
    an empty list wants the host.
    """
    host = (host or "www.goodreads.com").lower()
    listed = None
    if html:
        m = _WAF_COOKIE_DOMAINS_RE.search(html)
        if m:
            listed = [d.strip().strip("'\"").lower().lstrip(".")
                      for d in m.group(1).split(",") if d.strip().strip("'\"")]
    if listed is None:
        # No page to read it from: the host itself, which is what an empty
        # list means.
        listed = []
    for d in listed:
        if host == d or host.endswith("." + d):
            return "." + d
    return host



# ---------------------------------------------------------------------------
# The fetch loop, driven through named operations
# ---------------------------------------------------------------------------
#
# Everything about fetching one page lives here, once: navigating, letting
# the WAF's script run, paying for a CAPTCHA, retrying a transport failure,
# waiting out a throttle, rotating on a refusal, reading the API key off the
# book page, and parsing what came back. The three engines differ only in
# HOW they ask their driver, so each passes in an object with these
# operations and no JavaScript crosses this boundary (§1):
#
#     ops.goto(url)        -> (status, waf_header). Raises TransportError.
#     ops.document_text()  -> the current document's markup
#     ops.wait_ms(ms)
#     ops.solve_captcha()  -> True if a CAPTCHA was solved and the page reloaded
#     ops.fetch(req)       -> (status, text, waf_header, error_or_None)
#     ops.relaunch()       -> a fresh browser (on the pool's current exit)
#     ops.landed           -> bool attribute, owned by the loop: the page
#                             holds the book a reviews run reads its key from
#     ops.proxy_failure(text) -> the driver's proxy-error name in text, or ""
#
# One copy of the loop is how the three engines agree on exit codes and on
# whether a run spends money by construction rather than by discipline (§6).


class TransportError(Exception):
    """A navigation that did not complete: a timeout, a dead proxy."""


@dataclass
class PageOutcome:
    """What one page produced.

    Collected per page and merged afterwards, in page order, rather than
    folded into shared state as the loop goes, so the output cannot depend
    on which page happened to finish first (§8).
    """
    page_num: int
    url: str
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    # The state the page came back as. Carried so the caller can tell an
    # EMPTY page (the end of a listing) from a failed one: both hold zero
    # rows and they mean opposite things.
    state: Optional[str] = None
    # The API's own complaint when it refused the parameters, or this
    # repo's when a --review-language names a language the book has none in.
    rejected: Optional[str] = None
    # The site's own count of what matched, from this page.
    total_available: Optional[int] = None
    pages_available: Optional[int] = None
    # The site served a DIFFERENT page from the one asked for: the listing
    # ended before this page (module docstring of product_parser).
    past_end: bool = False
    # A reviews page with no cursor after it: the feed ended here.
    last_page: bool = False

    @property
    def ok(self) -> bool:
        return (not self.load_failed and self.blocked_by is None
                and self.rejected is None)


# The columns the site filled on EVERY record of every capture, per mode.
# Below this share, the page shape has moved rather than the data being
# unusual. Deliberately NOT here: `published_year` (a Listopia tile does not
# print one), `isbn` (null on The Hobbit's own edition), `rating` (null on a
# review with no stars, 2 of 100).
CORE_FIELD_FLOOR = 99
CORE_FIELDS = {
    "list": ("sku", "title", "author"),
    "book": ("sku", "title", "author", "work_id", "ratings_count"),
    "reviews": ("sku", "reviewer", "created_at"),
}


def settle(ops, url: str, status: Optional[int], waf: Optional[str]) -> str:
    """Classify the document `ops` is on, letting a WAF challenge run first.

    Solves at most SOLVES_PER_PAGE CAPTCHAs, because a CAPTCHA that survives
    a solved token is not one this run can pass, and a second solve is a
    second charge for the same answer (§23).
    """
    text = ops.document_text()
    state = classify(text, status, url, waf)
    if status is None and state == "unknown" and len(text.strip()) < 2_000:
        # Selenium reports no status. The WAF's 202 and /search's 403 to a
        # headless browser both reach it as an EMPTY document (the 403 is
        # 39 bytes: `<html><head></head><body></body></html>`), so an
        # empty document is waited on like the challenge it usually is,
        # rather than retried as a parse failure (§24: read the ambiguous
        # answer as the recoverable one).
        state = "challenge"
    if state != "challenge":
        return state
    # The WAF's challenge action computes a token and reloads the page by
    # itself, so let it run before judging it or paying for anything. The
    # document AFTER the reload is judged by what it holds: the status and
    # header belonged to the interstitial it replaced (§26).
    #
    # "unknown" keeps the wait going too. The first poll after the WAF's
    # reload often lands on the NEW page before it has finished arriving
    # (no __NEXT_DATA__ yet), and judged there it cost a retry and its
    # pause on 2 of 3 runs on 2026-09-27. After a challenge, a page that is
    # not yet recognisable is still loading.
    waited = 0
    while waited < CHALLENGE_SETTLE_MS and state in ("challenge", "unknown"):
        ops.wait_ms(CHALLENGE_POLL_MS)
        waited += CHALLENGE_POLL_MS
        state = _unsettled_state(ops.document_text(), url)
    if state == "challenge" and should_solve(state):
        for _ in range(SOLVES_PER_PAGE):
            if not ops.solve_captcha():
                break
            state = classify(ops.document_text(), None, url, None)
            if state != "challenge":
                break
    if state != "challenge":
        log.info("AWS WAF's challenge cleared after %.1fs.", waited / 1000.0)
    return state


def _unsettled_state(text: str, url: str) -> str:
    """What a document is while the settle wait runs, with the status gone.

    The 202 the WAF answers with has an EMPTY body, and so does the moment
    between its reload and the real page. Classified alone, an empty
    document is "unknown", which would end the wait on its first poll and
    report a retryable failure (exit 5) for what is still the challenge
    (exit 3). So a near-empty document counts as the challenge until the
    wait runs out."""
    state = classify(text, None, url, None)
    if state in ("unknown",) and len((text or "").strip()) < 2_000:
        return "challenge"
    return state


def navigate(ops, url: str) -> Tuple[str, str, Optional[str]]:
    """Go to `url` and judge the document: (state, text, transport_error)."""
    try:
        status, waf = ops.goto(url)
    except TransportError as e:
        return "load_failed", "", str(e)
    state = settle(ops, url, status, waf)
    return state, ops.document_text(), None


def land_for_reviews(ops, query: Query) -> Tuple[str, Optional[str]]:
    """Put the page on the book, and read the API key and ids off it.

    Returns (state, error). The key is the one the page hands every visitor
    for its own front end, and it is read fresh on every landing because an
    AppSync key expires; it is never stored anywhere but in memory.
    """
    url = landing_url(query)
    state, text, err = navigate(ops, url)
    if err:
        return "load_failed", err
    if state != "content":
        ops.landed = False
        return state, None
    got = book_landing(text)
    if not got.get("api_key") or not got.get("work_id"):
        # A served book page with no key in it: the page's shape moved.
        log.error("The book page carries no API key or work id — the page "
                  "shape has changed. Re-run with --dump-html.")
        ops.landed = False
        return "unknown", None
    for k, v in got.items():
        if hasattr(query, k) and k != "reviews_total":
            setattr(query, k, v)
    ops.landed = True
    return "content", None


def _core_field_warnings(rows: List, mode: str, page_num: int) -> None:
    for name in CORE_FIELDS.get(mode, ()):
        if not rows:
            return
        filled = sum(1 for r in rows if getattr(r, name, None) not in (None, "", []))
        share = 100.0 * filled / len(rows)
        if share < CORE_FIELD_FLOOR:
            log.warning("Only %.0f%% of page %d carries `%s`, against a "
                        "measured floor of %d%%. Every record of every capture "
                        "had one, so the page shape has moved — re-run with "
                        "--dump-html.", share, page_num, name, CORE_FIELD_FLOOR)


def _dump(args, page_num: int, text: str, pages: int) -> None:
    """Write the exact response the parser was given, on success too (§9)."""
    if not args.dump_html:
        return
    path = args.dump_html if pages == 1 else f"{args.dump_html}.page{page_num}"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    log.info("Saved the response the parser sees to %s (%d bytes).",
             path, len(text))


def _save_debug(args, page_num: int, text: str) -> str:
    path = f"{args.out}_page{page_num}_debug.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text or "")
    return path


def _fetch_attempt(ops, query: Query, req, page_num: int) -> Tuple[str, str, Optional[str]]:
    """One try at `req`: (state, text, transport_error_or_None)."""
    if req.navigate:
        return navigate(ops, req.url)
    if not ops.landed:
        state, err = land_for_reviews(ops, query)
        if err:
            return "load_failed", "", err
        if not ops.landed:
            return state, ops.document_text(), None
        # The key and work id arrived with the landing: rebuild the request.
        req = request_for(query, page_num)
    status, text, waf, err = ops.fetch(req)
    if err:
        return "load_failed", "", err
    state = classify(text, status, req.url, waf)
    if state in ("challenge", "unauthorized"):
        # A fresh navigation is where the WAF's page can run, and where a
        # fresh key comes from.
        ops.landed = False
    return state, text, None


def fetch_one_page(ops, args, pool, query: Query, page_num: int,
                   mask: Callable[[str], str] = lambda s: s,
                   pages: Optional[int] = None) -> PageOutcome:
    """Fetch and parse one page. Retries, rotations and debug dumps live here.

    Never raises for an EXPECTED failure. A timeout, a refusal, a WAF
    challenge and a dead exit are all recorded on the outcome, because what
    the run should do about them differs between the sequential and the
    concurrent paths.
    """
    pages = pages or args.pages
    req = request_for(query, page_num)
    outcome = PageOutcome(page_num=page_num, url=req.label)

    has_pool = bool(pool and len(pool) > 1)
    block_retries = 0 if not RETRY_ON_BLOCKED else (
        args.proxy_block_retries if has_pool else BLOCK_RETRIES_WITHOUT_POOL)
    throttles = 0
    state, text, last_error, exit_failed = "unknown", "", None, None

    for block_attempt in range(block_retries + 1):
        log.info("Fetching %s %d/%d: %s",
                 "book" if query.mode == "book" and query.book_urls else "page",
                 page_num, pages, req.label)
        exit_failed = None
        attempt = 0
        while attempt < args.retries:
            attempt += 1
            state, text, last_error = _fetch_attempt(ops, query, req, page_num)
            if last_error:
                exit_failed = ops.proxy_failure(last_error) or None
                if exit_failed:
                    break  # a different exit is the only thing that helps
            if state == "throttled" and throttles < THROTTLE_RETRIES:
                throttles += 1
                attempt -= 1  # a throttle wait spends its own budget (§24)
                pause = THROTTLE_WAIT_S * throttles
                log.warning("Rate-limited on page %d — waiting %.0fs at the "
                            "same exit (%d/%d).", page_num, pause, throttles,
                            THROTTLE_RETRIES)
                ops.wait_ms(int(pause * 1000))
                continue
            if (state in ("load_failed", "unknown", "unauthorized")
                    and attempt < args.retries):
                pause = args.retry_delay * (2 ** (attempt - 1))
                log.warning("Page %d came back %s (attempt %d/%d)%s — retrying "
                            "in %.1fs.", page_num, state, attempt, args.retries,
                            f": {mask(last_error)}" if last_error else "", pause)
                ops.wait_ms(int(pause * 1000))
                continue
            break

        if exit_failed and has_pool and block_attempt < block_retries:
            log.warning("Exit %s is unusable (%s) — rotating to another one "
                        "(%d/%d).", mask(pool.current), exit_failed,
                        block_attempt + 1, block_retries)
            pool.advance(f"unusable exit: {exit_failed}")
            ops.relaunch()
            continue
        if (counts_as_blocked(state) and should_retry(state)
                and block_attempt < block_retries):
            if has_pool:
                log.warning("Page %d refused (%s) at %s — rotating to another "
                            "exit (%d/%d).", page_num, refusal_name(state),
                            mask(pool.current), block_attempt + 1, block_retries)
                pool.advance(f"refused: {refusal_name(state)}")
            else:
                log.warning("Page %d refused (%s) — re-fetching once from a "
                            "fresh browser.", page_num, refusal_name(state))
            ops.relaunch()
            continue
        break

    outcome.state = state
    if state == "load_failed" or exit_failed:
        outcome.load_failed = True
        log.error("Gave up on page %d: %s", page_num,
                  mask(last_error or "the request never completed"))
        return outcome
    if state == "rejected":
        outcome.rejected = api_error(text) or "the API refused the request"
        log.error("The API refused this request (%s). That is a statement "
                  "about the PARAMETERS, and the same request sent again gets "
                  "the same answer, so it is not retried. If the flags look "
                  "right, the site's API has changed — open an issue with "
                  "--dump-html.", outcome.rejected)
        _dump(args, page_num, text, pages)
        return outcome
    if counts_as_blocked(state):
        outcome.blocked_by = refusal_name(state)
        debug = _save_debug(args, page_num, text)
        log.error("Blocked by %s on page %d — saved to %s. This is exit 3, "
                  "distinct from an empty listing (exit 4). %s",
                  outcome.blocked_by, page_num, debug,
                  refusal_advice(state, req.url, getattr(args, "headless", False)))
        return outcome
    if not should_parse(state):
        # throttled past its budget, a key that never worked, or a page this
        # parser does not recognise
        outcome.load_failed = True
        debug = _save_debug(args, page_num, text)
        log.error("Page %d never came back as a page this parser reads (%s) — "
                  "saved to %s. %s", page_num, state, debug,
                  "Raise --delay, or spread the run over --proxy-file."
                  if state == "throttled" else "")
        return outcome

    _dump(args, page_num, text, pages)
    if query.mode == "list" and req.navigate and state == "content":
        asked = requested_page(req.url)
        got = served_page(text, query.kind)
        if got is not None and got != asked:
            # A list and a search answer a page past their end with their
            # LAST page, and a shelf with its first. Those rows were already
            # collected, so this is the end of the listing, not more data.
            log.info("Asked for page %d, the site served page %d — the "
                     "listing ended before this page.", asked, got)
            outcome.past_end = True
            outcome.total_available, outcome.pages_available = page_totals(text, query)
            return outcome
    rows = parse_page(text, query, page_num) if state == "content" else []
    outcome.products = rows
    outcome.total_available, outcome.pages_available = page_totals(text, query)
    if query.mode == "reviews":
        cursor = next_cursor(text)
        query.cursors[page_num + 1] = cursor
        outcome.last_page = not cursor
    log.info("Parsed %d row(s) from page %d.", len(rows), page_num)
    if page_num == 1 and outcome.total_available is not None:
        log.info("The site reports %d match(es) — %s page(s) at this page "
                 "size.", outcome.total_available, outcome.pages_available)
    _core_field_warnings(rows, query.mode, page_num)
    return outcome


def check_language(ops, args, query: Query) -> Optional[str]:
    """Refuse a --review-language the book has no reviews in, before the
    first review request, with the list it does have. The API answers one
    with an EMPTY result, not an error, so without this a typo is an exit-4
    run on a book with 95,000 reviews."""
    if query.mode != "reviews" or not query.review_language:
        return None
    if not ops.landed:
        land_for_reviews(ops, query)
    if not ops.landed:
        return None  # the first page reports why
    if query.languages is None:
        log.warning("The book page lists no review languages — "
                    "--review-language is sent unchecked.")
        return None
    return check_review_language(query.review_language, query.languages)


def _run_sequence(ops_box, args, pool, query: Query, plan_from: PageOutcome,
                  pages_wanted: int, concurrency: int, run_concurrently,
                  open_ops, close_ops, mask) -> Tuple[List[PageOutcome], str, bool]:
    """Pages 2..N after page 1, sequentially or across workers.

    `ops_box` is a one-element list holding the live ops, so a switch to
    workers can close it and the caller's finally still sees the change.
    """
    outcomes: List[PageOutcome] = []
    stop_reason, blocked = "completed", False
    plan = pages_to_plan(pages_wanted, plan_from.pages_available)
    if plan < pages_wanted and query.mode != "book":
        log.info("Asked for %d page(s); the site will serve %s. Fetching all "
                 "of them.", pages_wanted, plan_from.pages_available)
    if query.mode == "list" and query.kind not in ADDRESSABLE_KINDS and pages_wanted > 1:
        log.warning("A %s is ONE page to an anonymous visitor (a shelf answers "
                    "?page=2 with page 1 again; a series lists every work on "
                    "one page), so --pages %d fetches one.", query.kind,
                    pages_wanted)
        plan = 1
    if query.mode == "reviews" and plan_from.last_page:
        plan = 1
    rest = list(range(2, plan + 1)) if (plan_from.products or query.mode == "book") else []
    if plan_from.past_end:
        rest = []
    if rest and concurrency > 1 and addressable(query):
        close_ops(ops_box[0])
        ops_box[0] = None
        log.info("Fetching pages 2-%d across %d workers%s.", plan,
                 concurrency, f" over {len(pool)} exit(s)" if pool else "")
        more, unattempted, exhausted = run_concurrently(rest, query)
        outcomes.extend(more)
        failed = [o for o in more if not o.ok]
        if failed:
            stop_reason = stop_reason_for(min(failed, key=lambda o: o.page_num))
            blocked = any(o.blocked_by for o in more)
        elif exhausted:
            stop_reason = "end_of_listing"
        elif unattempted:
            stop_reason = "pages_unattempted"
        return outcomes, stop_reason, blocked
    for page_num in rest:
        ops = ops_box[0]
        ops.wait_ms(int(args.delay * 1000))
        if pool and pool.rotates_per_page():
            pool.advance(f"per-page rotation, page {page_num}")
            ops.relaunch()
        outcome = fetch_one_page(ops, args, pool, query, page_num, mask,
                                 pages=plan)
        outcomes.append(outcome)
        if not outcome.ok:
            stop_reason = stop_reason_for(outcome)
            blocked = outcome.blocked_by is not None
            if query.mode == "book":
                # One book that failed does not end a run over forty: the
                # others are independent pages. It is recorded, and the run
                # is partial.
                continue
            break
        if query.mode == "book":
            continue
        if outcome.past_end or not outcome.products:
            log.info("Page %d holds nothing new — the listing ended before "
                     "the plan did.", page_num)
            stop_reason = "end_of_listing"
            break
        if outcome.last_page:
            stop_reason = "end_of_listing"
            break
    if query.mode == "book":
        failed = [o for o in outcomes if not o.ok]
        stop_reason = stop_reason_for(failed[0]) if failed else "completed"
        blocked = bool(failed) and all(o.blocked_by for o in failed)
    return outcomes, stop_reason, blocked


def _collect_book_urls(outcomes: List[PageOutcome], limit: Optional[int]) -> List[str]:
    urls = [r.url for r in merge_outcomes(outcomes) if r.url]
    return urls[:limit] if limit else urls


def run_pages(open_ops, close_ops, run_concurrently, args, pool,
              query: Query, concurrency: int,
              mask: Callable[[str], str] = lambda s: s) -> int:
    """The whole run after argument handling, shared by the three engines.

    `open_ops()` returns a ready ops object on `pool`, `close_ops(ops)`
    tears it down, and `run_concurrently(page_nums, query)` returns
    (outcomes, unattempted, exhausted) for pages fetched by workers. The
    engines supply those three because a browser's lifecycle (and on
    Playwright, its thread) is the one thing that cannot be shared.

    A book run from a LISTING is two phases: the listing's pages (phase A,
    exactly a list run), then every book they named (phase B, one "page"
    per book).
    """
    ops_box = [open_ops()]
    listing: Optional[List[PageOutcome]] = None
    try:
        refusal = check_language(ops_box[0], args, query)
        if refusal:
            log.error("%s", refusal)
            return 2

        if query.mode == "book" and query.kind != "book":
            lq = copy.copy(query)
            lq.mode = "list"
            first = fetch_one_page(ops_box[0], args, pool, lq, 1, mask)
            listing = [first]
            if not first.ok:
                return finish(args, query, [], stop_reason_for(first),
                              first.blocked_by is not None, listing=listing)
            if ops_box[0] is None:
                ops_box[0] = open_ops()
            more, stop_a, blocked_a = _run_sequence(
                ops_box, args, pool, lq, first, args.pages, concurrency,
                run_concurrently, open_ops, close_ops, mask)
            listing.extend(more)
            query.book_urls = _collect_book_urls(listing, query.max_books)
            log.info("The listing named %d book(s); reading each one's page.",
                     len(query.book_urls))
            if not query.book_urls:
                return finish(args, query, [], stop_a, blocked_a, listing=listing)
            if ops_box[0] is None:
                ops_box[0] = open_ops()

        n = len(query.book_urls) if query.book_urls else args.pages
        first = fetch_one_page(ops_box[0], args, pool, query, 1, mask, pages=n)
        outcomes = [first]
        if not first.ok and query.mode != "book":
            return finish(args, query, outcomes, stop_reason_for(first),
                          first.blocked_by is not None, listing=listing)
        if query.mode == "book" and not query.book_urls:
            stop_reason = "completed" if first.ok else stop_reason_for(first)
            return finish(args, query, outcomes, stop_reason,
                          first.blocked_by is not None)
        if query.mode == "book":
            first.pages_available = n
        more, stop_reason, blocked = _run_sequence(
            ops_box, args, pool, query, first, n if query.mode == "book" else args.pages,
            concurrency, run_concurrently, open_ops, close_ops, mask)
        outcomes.extend(more)
        if query.mode == "book":
            failed = [o for o in outcomes if not o.ok]
            stop_reason = stop_reason_for(failed[0]) if failed else "completed"
            blocked = bool(failed) and all(o.blocked_by for o in failed)
        elif first.past_end or (first.ok and not first.products):
            stop_reason = "completed"
        elif first.last_page and query.mode == "reviews":
            stop_reason = "end_of_listing" if args.pages > 1 else "completed"
        return finish(args, query, outcomes, stop_reason, blocked, listing=listing)
    finally:
        if ops_box[0] is not None:
            close_ops(ops_box[0])


def worker_loop(ops, args, query: Query, work, results, results_lock,
                exhausted, name: str, mask: Callable[[str], str] = lambda s: s):
    """One concurrent worker's page loop, after its engine opened `ops`.

    Takes pages until the queue is empty or a listing page comes back past
    the end, which sets `exhausted` so the other workers stop taking work
    too. A book run never sets it: each book is its own page.
    """
    first = True
    total = len(query.book_urls) if query.mode == "book" and query.book_urls else args.pages
    while not exhausted.is_set():
        try:
            page_num = work.get_nowait()
        except Exception:  # queue.Empty
            break
        if not first:
            ops.wait_ms(int(args.delay * 1000))
        first = False
        outcome = fetch_one_page(ops, args, ops.pool, query, page_num, mask,
                                 pages=total)
        with results_lock:
            results.append(outcome)
        if (query.mode != "book" and outcome.ok
                and (outcome.past_end or not outcome.products)):
            log.info("[%s] page %d is past the end of the listing; stopping "
                     "dispatch.", name, page_num)
            exhausted.set()


def concurrency_for(args, pool) -> int:
    """How many workers this run may use, with the warnings said once for
    all three engines."""
    concurrency = max(1, args.concurrency)
    if concurrency <= 1:
        return 1
    if concurrency_limit(args.cdp_endpoint) == 1:
        log.warning("--concurrency is ignored with --cdp-endpoint: the Scraping "
                    "Browser API allows one live connection per profile, and "
                    "several workers would collide on it (profile_locked). Use "
                    "several pids instead.")
        return 1
    if not pool:
        log.warning("--concurrency %d with no proxy pool: every worker leaves "
                    "from the SAME address, and AWS WAF scores an address by "
                    "its request rate. Pass --proxy-file to spread the load.",
                    concurrency)
    if concurrency > 8:
        log.warning("--concurrency %d means %d browsers at once (~150-300MB "
                    "each).", concurrency, concurrency)
    return concurrency


def worker_pool(pool, worker_index: int):
    """A private ProxyPool for one worker, starting at a different exit.

    Workers start on distinct exits and share no mutable state, so rotation
    needs no lock (§7).
    """
    if not pool:
        return None
    from proxy_pool import ProxyPool
    proxies = pool.proxies
    offset = worker_index % len(proxies)
    return ProxyPool(proxies[offset:] + proxies[:offset], rotate="per-run")


def cdp_connect_hint(error_text: str) -> str:
    """What a failed --cdp-endpoint connection means, from its status.

    Two answers that want opposite fixes. Measured 2026-09-24 against four
    Scraping Browser endpoints left in sibling repos' .env files: all four
    answered 401 Unauthorized, because a profile's credentials last about a
    day. The message used to explain a 500 (a pid another run still holds)
    whatever the status was, which sent the reader to wait for a run that
    did not exist.
    """
    if "401" in (error_text or ""):
        return ("HTTP 401: the endpoint's credentials were refused. A Scraping "
                "Browser profile's credentials last about a day, so an "
                "endpoint copied from an older .env has usually expired. "
                "Get a fresh one from your 2Captcha dashboard.")
    return ("A Scraping Browser profile allows ONE live connection at a time, "
            "so an HTTP 500 here usually means another run still holds this "
            "`pid`. Wait for it to finish, or use a different pid.")


# Connecting to a Scraping Browser profile right after the previous run let
# go of it answers HTTP 500 `profile_locked`: the service releases a profile
# 1.6-1.9 s after a clean disconnect (measured 3 of 3, 2026-09-24). Two
# back-to-back runs therefore failed with exit 5 in the first live matrix
# through --cdp-endpoint. Three attempts 3 s apart ride that out, and a
# profile genuinely held by another run still fails, after ~9 s, with the
# pid explanation.
CDP_CONNECT_ATTEMPTS = 3
CDP_LOCKED_WAIT_S = 3.0
# pyppeteer does not surface the 500 at all: its connect() waits on a future
# the rejected handshake never resolves, so only a timeout ends it. A
# successful connect measured 0.8-0.95 s, so 10 s is an order of magnitude
# of headroom and a third of the 30 s it used to wait per attempt.
CDP_CONNECT_TIMEOUT_S = 10


def cdp_should_retry(error_text: str) -> bool:
    """Whether a failed --cdp-endpoint connection is worth another attempt:
    a locked profile (500) or a connect that never answered. A 401 is not:
    expired credentials stay expired."""
    text = error_text or ""
    if "401" in text:
        return False
    return ("profile_locked" in text or " 500" in text or "HTTP 500" in text
            or "did not return within" in text)
