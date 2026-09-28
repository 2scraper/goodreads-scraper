#!/usr/bin/env python3
"""
smoke_test.py — the offline suite for goodreads-scraper.

One file of plain functions. `tests/test_smoke.py` wraps it as a single
pytest test so `pytest` works as an entry point without a second copy of the
checks.

    python3 smoke_test.py            run everything
    python3 smoke_test.py -v         print every check as it passes

It must pass with NO engine library installed at all: every
`import playwright_scraper` / `selenium_scraper` / `puppeteer_scraper` is
guarded and the skip is RECORDED, because "skipped, engine absent" reads
identically to a real import error. CI installs each engine in its own venv
and fails if that engine's group reports a skip.

THE FIXTURES ARE IN `fixtures_generated.json`, NOT INLINE. They are real
pages and API responses captured 2026-09-27, cut and scrubbed by
`make_fixtures.py`, which proves each one parses identically to its uncut
original. Not verbatim: the book pages' API key is a placeholder, CSRF tokens
and asset fingerprints are gone, reviewers' names, ids and words are
placeholders, and the search pages' React payload is re-encoded (see
make_fixtures.py for why each).
"""

import argparse
import ast
import copy
import csv
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import types
from dataclasses import asdict, fields

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FAILURES = []
PASSED = 0
SKIPS = []
VERBOSE = False


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        if VERBOSE:
            print("  ok   %s" % name)
    else:
        FAILURES.append("%s%s" % (name, (" — " + detail) if detail else ""))
        print("  FAIL %s%s" % (name, (" — " + detail) if detail else ""))


def equal(name, got, want):
    check(name, got == want, "got %r, want %r" % (got, want))


def skip(group, reason):
    SKIPS.append("%s: %s" % (group, reason))
    print("  SKIP %s — %s" % (group, reason))


FIXTURES_PATH = os.path.join(HERE, "fixtures_generated.json")
FIXTURES = json.load(open(FIXTURES_PATH, encoding="utf-8"))


def fx(name) -> str:
    """A fixture as the text the site returned."""
    return FIXTURES[name]["text"]


def fx_url(name) -> str:
    return FIXTURES[name]["url"]


ENGINES = ("playwright_scraper", "selenium_scraper", "puppeteer_scraper")
DRIVER_IMPORTS = {"playwright_scraper": "playwright",
                  "selenium_scraper": "selenium",
                  "puppeteer_scraper": "pyppeteer"}


def _import_engine(name):
    try:
        return __import__(name)
    except ImportError as e:
        skip(name, "engine library absent (%s)" % e)
        return None


def _q(url, mode=None, **kw):
    import product_parser as P
    q, why = P.query_from_url(url)
    assert q is not None, why
    if mode:
        q.mode = mode
    for k, v in kw.items():
        setattr(q, k, v)
    return q


def _rows(name, mode=None, page=1, **kw):
    import product_parser as P
    return P.parse_page(fx(name), _q(fx_url(name), mode, **kw), page)


# ---------------------------------------------------------------------------
# The fixtures themselves
# ---------------------------------------------------------------------------

def check_fixture_corpus_is_real_and_scrubbed():
    expected = {"list_p1", "list_p2", "list_p800", "author_p1", "author_p999",
                "search_legacy", "search_p2", "search_p999", "search_none",
                "shelf_p1", "series", "book_hobbit", "book_dune", "book_missing",
                "reviews_p1", "reviews_p2", "reviews_fr_1star", "reviews_xx",
                "reviews_401", "reviews_bad_sort", "waf_challenge", "cdp_book"}
    missing = expected - set(FIXTURES)
    check("every fixture the suite uses is in fixtures_generated.json",
          not missing, "missing %s" % sorted(missing))
    blob = json.dumps(FIXTURES)
    check("the corpus is not empty (a scan of nothing passes for the wrong reason)",
          len(blob) > 50000, "%d bytes" % len(blob))
    check("no 32-hex string survived the scrub",
          not re.search(r"\b[0-9a-f]{32}\b", blob))
    check("no CSRF token survived the scrub", "authenticity_token" not in blob)
    check("no AppSync key survived the scrub", not re.search(r"da2-[a-z0-9]{20,}", blob))
    check("...the book pages carry the placeholder in its place",
          "REDACTED-appsync-key" in fx("book_hobbit"))
    check("reviewers are placeholders", "Reviewer 1" in blob and "Will Byrnes" not in blob)
    check("...and so are their words", "Placeholder review." in blob
          and "In a hole in the ground there lived a hobbit.\\n\\nBooks exist" not in blob)
    check("the WAF page's token material is replaced",
          '"key":"PLACEHOLDER"' in fx("waf_challenge"))


# ---------------------------------------------------------------------------
# The parser, asserted on VALUES rather than on coverage (§10)
# ---------------------------------------------------------------------------

def check_a_list_page_parses_to_the_captured_values():
    import product_parser as P
    rows = _rows("list_p1")
    equal("list page 1: five tiles kept", len(rows), 5)
    r = rows[0]
    equal("sku is the book id from the url", r.sku, "2767052")
    equal("url is the book's page, no tracking tail", r.url,
          "https://www.goodreads.com/book/show/2767052-the-hunger-games")
    equal("title as the list prints it, series included", r.title,
          "The Hunger Games (The Hunger Games, #1)")
    equal("author", (r.author, r.author_id), ("Suzanne Collins", "153394"))
    equal("rating and count, read from '4.36 avg rating — 10,292,810 ratings'",
          (r.avg_rating, r.ratings_count), (4.36, 10292810))
    equal("the list's own score and votes", (r.list_score, r.list_votes), (4546935, 46204))
    equal("provenance", (r.data_source, r.listing_kind), ("microdata", "list"))
    equal("positions count the emitted rows", [x.position for x in rows], [1, 2, 3, 4, 5])
    html = fx("list_p1")
    equal("the list states 79,607 books in its title", P.total_results(html, "list"), 79607)
    equal("...and serves 100 pages of them", P.pages_available(html, "list"), 100)
    equal("page 1 says it is page 1", P.served_page(html, "list"), 1)
    equal("page 2 says it is page 2", P.served_page(fx("list_p2"), "list"), 2)


def check_out_of_range_pages_are_the_site_serving_another_page():
    """§23: /list/show/1?page=800 answers 200 with page 100, and a search
    ?page=999 with "Page 100 of 100". A run that trusted its request would
    re-collect the last page for as long as it was asked to."""
    import product_parser as P
    equal("list ?page=800 is served as page 100", P.served_page(fx("list_p800"), "list"), 100)
    equal("search ?page=999 is served as page 100",
          P.served_page(fx("search_p999"), "search"), 100)
    equal("the page a url ASKS for is read from the url",
          P.requested_page(fx_url("list_p800")), 800)


def check_an_author_list_parses_to_the_captured_values():
    import product_parser as P
    r = _rows("author_p1")[0]
    equal("The Hobbit on Tolkien's list", (r.sku, r.title),
          ("5907", "The Hobbit, or There and Back Again"))
    equal("published year and editions, as printed", (r.published_year, r.editions_count),
          (1937, 1458))
    equal("the work id, from the editions link", r.work_id, "1540236")
    equal("27 pages of 30, from will_paginate's own markup (no div.pagination)",
          P.pages_available(fx("author_p1"), "author"), 27)


def check_both_search_pages_read_alike():
    """A browser gets a Next.js page whose books are in its React payload;
    curl, when served, gets the old table. One parser reads both, and a
    title reads the same from either."""
    import product_parser as P
    legacy = _rows("search_legacy")
    new = _rows("search_p2")
    equal("the old table: microdata", legacy[0].data_source, "microdata")
    equal("the new page: its React payload", new[0].data_source, "rsc")
    equal("old table's first result", (legacy[0].sku, legacy[0].title),
          ("44767458", "Dune (Dune, #1)"))
    equal("new page's first result, series rebuilt into the title the old way",
          (new[0].sku, new[0].title),
          ("99220", "The Battle of Corrin (Legends of Dune, #3)"))
    equal("...with the work's stats", (new[0].avg_rating, new[0].ratings_count, new[0].work_id),
          (3.82, 16403, "21444"))
    equal("'Page 2 of 100' is read as served page and page count",
          (P.served_page(fx("search_p2"), "search"), P.pages_available(fx("search_p2"), "search")),
          (2, 100))
    equal("no results is EMPTY, an answer", P.detect_page_state(
        fx("search_none"), 200, fx_url("search_none")), "empty")


def check_a_shelf_and_a_series_parse_to_the_captured_values():
    import product_parser as P
    r = _rows("shelf_p1")[0]
    equal("shelf: title and shelved count", (r.sku, r.shelved_count), ("42844155", 90659))
    equal("shelf: rating read from 'avg rating 4.47 — 11,840,415 ratings'",
          (r.avg_rating, r.ratings_count, r.published_year), (4.47, 11840415, 1997))
    equal("the shelf states 100,000 books", P.total_results(fx("shelf_p1"), "shelf"), 100000)
    equal("...and is ONE page to an anonymous visitor", P.pages_available(fx("shelf_p1"), "shelf"), 1)
    rows = _rows("series")
    equal("the series lists 15 works", len(rows), 15)
    equal("...each labelled by the site's own heading",
          [x.series_position for x in rows[:4]], ["Book 0.5", "Book 1", "Book 1.5", "Book 2"])
    equal("...and read from its React props", rows[0].data_source, "series-props")


def check_a_book_page_parses_to_the_captured_values():
    b = _rows("book_hobbit")[0]
    equal("sku and title", (b.sku, b.title), ("5907", "The Hobbit, or There and Back Again"))
    equal("the WORK's rating and counts", (b.avg_rating, b.ratings_count, b.reviews_count),
          (4.3, 4638530, 95336))
    dist = [b.rating_1, b.rating_2, b.rating_3, b.rating_4, b.rating_5]
    equal("the distribution", dist, [94599, 145934, 544961, 1336215, 2516821])
    equal("...sums to the count (a distribution that did not would be misread)",
          sum(dist), b.ratings_count)
    equal("first published before 1970 (a negative epoch)", b.first_published, "1937-09-21")
    equal("this edition's own publication", b.published_at, "2002-08-15")
    equal("contributors with their roles, primary first", b.contributors,
          ["J.R.R. Tolkien (Author)", "Douglas A. Anderson (Editor)",
           "Michael Hague (Illustrator)", "Jemima Catlin (Illustrator)"])
    equal("series and place", (b.series, b.series_position), ("Middle Earth", "1"))
    equal("genres, in the site's order", b.genres[:3], ["Fantasy", "Classics", "Fiction"])
    check("per-language review counts", b.review_languages and "fr:468" in b.review_languages)
    equal("the social counts", (b.currently_reading, b.want_to_read), (119042, 1394198))
    equal("awards with year", b.awards[0], "Keith Barker Millennium Book Award (2000)")
    equal("an edition with no ISBN keeps it null", (b.isbn, b.isbn13), (None, None))
    check("the blurb is plain text", b.description.startswith("In a hole in the ground")
          and "<br" not in b.description)
    equal("provenance", b.data_source, "apollo+jsonld")
    d = _rows("book_dune")[0]
    equal("both ISBNs where the edition has them", (d.isbn, d.isbn13), ("059309932X", "9780593099322"))
    equal("a book page fetched by a BROWSER parses too", (d.sku, d.pages, d.format),
          ("44767458", 658, "Hardcover"))
    equal("a book id that names nothing parses to no rows", _rows("book_missing"), [])


def check_the_root_book_is_the_one_the_page_asked_for():
    """A book page's cache holds a dozen other books (similar titles, the
    series). The page's own is the one ROOT_QUERY asked for by id."""
    import product_parser as P
    nd = P.next_data(fx("book_hobbit"))
    equal("the root book is 5907", P._root_book(nd).get("legacyId"), 5907)


def check_the_landing_gives_what_a_reviews_run_needs():
    import product_parser as P
    got = P.book_landing(fx("book_hobbit"))
    check("the API key is read from the page", bool(got["api_key"]))
    check("the work id is the API's own", got["work_id"].startswith("kca://work/"))
    equal("the book's own id and the work's legacy id", (got["book_id"], got["work_legacy_id"]),
          ("5907", "1540236"))
    equal("the languages it has reviews in: 55 of them", len(got["languages"]), 55)
    equal("a language it has: allowed", P.check_review_language("fr", got["languages"]), None)
    check("a language it has none in: refused WITH the list",
          "It has reviews in: en, fr" in (P.check_review_language("xx", got["languages"]) or ""))
    equal("unknown list: not refused", P.check_review_language("xx", None), None)


def check_reviews_parse_to_the_captured_values():
    import product_parser as P
    q = _q(fx_url("reviews_p1"), "reviews", book_title="The Hobbit", book_id="5907",
           work_legacy_id="1540236")
    rows = P.parse_reviews(fx("reviews_p1"), q, 1)
    equal("four reviews kept", len(rows), 4)
    r = rows[0]
    equal("sku is the review id from its url", (r.sku, r.url),
          ("900000001", "https://www.goodreads.com/review/show/900000001"))
    equal("the family `title` is the book's", r.title, "The Hobbit")
    equal("stars, likes, comments", (r.rating, r.likes, r.comments), (5, 872, 150))
    equal("dates as ISO-8601 UTC", r.created_at, "2008-11-05T15:18:37Z")
    equal("the reviewer's own shelf and tags", (r.shelf, r.tags[:2]),
          ("read", ["all-time-favorites-fiction", "young-adult"]))
    equal("the review's HTML as text, paragraphs kept", r.text,
          "Placeholder review. The first paragraph of a review, standing in for "
          "a reader's words.\n\nA second paragraph after a break.")
    equal("the reviewer (a placeholder)", (r.reviewer, r.reviewer_id), ("Reviewer 1", "100000001"))
    equal("the API's own count", P.reviews_total(fx("reviews_p1")), 95086)
    check("a cursor to the next page", bool(P.next_cursor(fx("reviews_p1"))))
    equal("pages available at 30 a page", P.page_totals(fx("reviews_p1"), q), (95086, 3170))
    last = P.parse_reviews(fx("reviews_fr_1star"), q, 1)
    check("a filtered page is all one star", last and all(x.rating == 1 for x in last))
    equal("...and is the last one: no cursor after it", P.next_cursor(fx("reviews_fr_1star")), None)
    payload = json.loads(fx("reviews_p1"))
    payload["data"]["getReviews"]["edges"][0]["node"]["rating"] = 0
    equal("a review with no stars is null, not 0 (§21)",
          P.parse_reviews(json.dumps(payload), q, 1)[0].rating, None)


def check_the_reviews_request_is_the_one_measured():
    import product_parser as P
    q = _q("https://www.goodreads.com/book/show/5907", "reviews", work_id="kca://work/x",
           api_key="da2-k", sort="newest", rating=1, review_language="fr",
           search_text="dragon", page_size=50)
    q.cursors[2] = "CURSOR"
    r1, r2 = P.request_for(q, 1), P.request_for(q, 2)
    v = r1.body["variables"]
    equal("filters: the WORK, every edition's reviews", (v["filters"]["resourceType"],
          v["filters"]["resourceId"]), ("WORK", "kca://work/x"))
    equal("sort mapped to the API's value", v["filters"]["sort"], "NEWEST")
    equal("one star is min = max = 1", (v["filters"]["ratingMin"], v["filters"]["ratingMax"]), (1, 1))
    equal("language and text", (v["filters"]["languageCode"], v["filters"]["searchText"]),
          ("fr", "dragon"))
    equal("page 1 has no cursor", v["pagination"], {"limit": 50})
    equal("page 2 starts after page 1's cursor", r2.body["variables"]["pagination"]["after"], "CURSOR")
    equal("the key rides in x-api-key, never in the url", (r1.headers, "da2-k" in r1.url),
          ({"x-api-key": "da2-k"}, False))
    equal("it is a fetch, not a navigation", (r1.navigate, r1.method), (False, "POST"))


def check_the_api_silent_fallbacks_are_refused_up_front():
    """§26: each of these was answered by the live API with HTTP 200 and
    nothing, or refused only after a request."""
    q = _q("https://www.goodreads.com/book/show/5907", "reviews")
    q.page_size = 200
    check("page size 200 (answered with ZERO reviews) is refused", q.validate())
    q.page_size = 100
    equal("100 is the ceiling that works", q.validate(), None)
    q.sort = "bogus"
    check("an unknown sort is refused", q.validate())
    q.sort, q.rating = "default", 6
    check("rating 6 is refused", q.validate())
    q.rating, q.review_language = None, "FR"
    check("an upper-case language code is refused (the site's are lower case)", q.validate())


def check_zero_ratings_are_null_not_zero():
    import product_parser as P
    html = fx("author_p1").replace("4.30 avg rating — 4,638,531 ratings",
                                   "0.00 avg rating — 0 ratings")
    r = P.parse_listing(html, _q(fx_url("author_p1")), 1)[0]
    check("the edit applied (a control that did nothing proves nothing)", "0 ratings" in html)
    equal("an unrated book is null on both, never 0.00 (§21)", (r.avg_rating, r.ratings_count),
          (None, None))


def check_url_shapes():
    import product_parser as P
    cases = {
        "https://www.goodreads.com/book/show/5907.The_Hobbit": ("book", "book"),
        "goodreads.com/list/show/1.Best_Books_Ever": ("list", "list"),
        "https://www.goodreads.com/author/list/656983.J_R_R_Tolkien": ("author", "list"),
        "https://www.goodreads.com/search?q=dune": ("search", "list"),
        "https://www.goodreads.com/shelf/show/fantasy": ("shelf", "list"),
        "https://www.goodreads.com/series/66175-the-lord-of-the-rings": ("series", "list"),
    }
    for url, (kind, mode) in cases.items():
        q, why = P.query_from_url(url)
        equal("%s is a %s url" % (url, kind), (q and q.kind, q and q.mode), (kind, mode))
    q, _ = P.query_from_url("https://www.goodreads.com/search?q=dune&qid=abc&page=3&from_search=true")
    equal("a search url loses its tracking tail and keeps its page as the START",
          (q.url, q.start_page), ("https://www.goodreads.com/search?q=dune", 3))
    equal("...and page 1 of the RUN asks the site for page 3", P.request_for(q, 1).url,
          "https://www.goodreads.com/search?q=dune&page=3")
    equal("page_url replaces rather than duplicates",
          P.page_url("https://www.goodreads.com/search?q=a&page=2", 5),
          "https://www.goodreads.com/search?q=a&page=5")
    _, why = P.query_from_url("https://www.goodreads.com/author/show/656983.J_R_R_Tolkien")
    check("an author PROFILE is refused, pointing at their list", "/author/list/" in (why or ""))
    _, why = P.query_from_url("https://www.amazon.com/book/show/1")
    check("another host is refused", "not a goodreads.com url" in (why or ""))
    _, why = P.query_from_url("https://www.goodreads.com/search")
    check("a search with no q is refused", "?q=" in (why or ""))


def check_page_states_on_real_captures():
    import page_flow as F
    import product_parser as P
    for name in ("list_p1", "author_p1", "search_legacy", "search_p2", "shelf_p1",
                 "series", "book_hobbit", "book_dune"):
        equal("%s is content" % name, F.classify(fx(name), 200, fx_url(name)), "content")
    equal("an author list past its end: the site's own 404 is EMPTY, an answer",
          F.classify(fx("author_p999"), 404, fx_url("author_p999")), "empty")
    equal("a book id that names nothing is EMPTY",
          F.classify(fx("book_missing"), 200, fx_url("book_missing")), "empty")
    equal("AWS WAF's challenge page with its 202", F.classify(fx("waf_challenge"), 202,
          fx_url("waf_challenge")), "challenge")
    equal("...and with no status (Selenium), by its markers",
          F.classify(fx("waf_challenge"), None, fx_url("waf_challenge")), "challenge")
    equal("the 202's EMPTY body + header", F.classify("", 202, "", "challenge"), "challenge")
    api = P.GRAPHQL_ENDPOINT
    equal("reviews JSON is content", F.classify(fx("reviews_p1"), 200, api), "content")
    equal("an unknown language is EMPTY, an answer", F.classify(fx("reviews_xx"), 200, api), "empty")
    equal("401 is the KEY refused, not a block", F.classify(fx("reviews_401"), 401, api),
          "unauthorized")
    equal("a validation error is REJECTED", F.classify(fx("reviews_bad_sort"), 200, api), "rejected")
    equal("...and the API's complaint is named", P.api_error(fx("reviews_bad_sort")),
          "Variable 'sort' has an invalid value.")
    equal("429 is throttled", F.classify("", 429), "throttled")
    equal("403 is blocked", F.classify("<html><head></head><body></body></html>", 403,
                                       "https://www.goodreads.com/search?q=a"), "blocked")
    equal("anything else is unknown", F.classify("<html>hello</html>", 200,
                                                 "https://www.goodreads.com/list/show/1"), "unknown")


def check_markers_do_not_match_a_good_page_or_the_extension():
    """§18: count a marker on pages you know are good. §24: the Scraping
    Browser's auto-solve extension injects captcha hunters (amazon_waf among
    them) into every page, and the set must score zero on that WITHOUT any
    strip."""
    import product_parser as P
    html = fx("cdp_book")
    check("the CDP fixture really carries the extension's hunters (not vacuous)",
          html.count("chrome-extension://") >= 10 and "amazon_waf" in html
          and "cf-turnstile" in html and "<captcha-widgets" in html)
    equal("no AWS WAF marker fires on a page the Scraping Browser served",
          P.detect_bot_challenge(html), None)
    # The line above would pass on the own-asset guard alone (a served page
    # references gr-assets.com), whatever the markers were: a planted
    # "amazon_waf" marker left it green. So the SET is checked directly
    # too, as a raw substring count, which is what §24 asks for.
    equal("...and not one marker even OCCURS in it (the set, not the guard)",
          [m for m in P.AWS_WAF_MARKERS if m in html], [])
    equal("...and it classifies as the book it is", P.detect_page_state(
        html, 200, fx_url("cdp_book")), "content")
    equal("...and parses to it", _rows("cdp_book")[0].sku, "5907")
    for name in ("list_p1", "search_p2", "shelf_p1", "series", "book_hobbit", "author_p999"):
        equal("no marker fires on a served page (%s)" % name, P.detect_bot_challenge(fx(name)), None)
    check("the WAF page itself IS caught (the markers are not dead)",
          P.detect_bot_challenge(fx("waf_challenge")) == "aws-waf")
    check("'awswaf' alone is not a marker (the site's own bundle names one)",
          "awswaf" not in P.AWS_WAF_MARKERS)


def check_the_waf_page_is_a_challenge_not_a_captcha():
    """§19: "unsolvable" is a property of a PAGE. This is the CHALLENGE
    action: no widget, nothing to buy, and a browser passes it for free."""
    from captcha_solver import detect_aws_waf
    c = detect_aws_waf(fx("waf_challenge"), fx_url("waf_challenge"))
    check("the captured page is detected as AWS WAF", c is not None and c.is_aws_waf)
    equal("...as its challenge action", c and c.aws_waf_action, "challenge")
    check("...with no CAPTCHA widget, so nothing is sent to the solver",
          c is not None and not c.has_captcha_widget)
    import page_flow as F
    equal("its (empty) cookie-domain list means the page host",
          F.cookie_domain("www.goodreads.com", fx("waf_challenge")), "www.goodreads.com")


def check_state_policy():
    import page_flow as F
    equal("every state has a policy", sorted(F.STATE_POLICY),
          ["blocked", "challenge", "content", "empty", "rejected", "throttled",
           "unauthorized", "unknown"])
    check("content and empty are parsed, never retried or blocked",
          all(F.should_parse(s) and not F.should_retry(s) and not F.counts_as_blocked(s)
              for s in ("content", "empty")))
    check("rejected: not retried, not solved, NOT blocked",
          not F.should_retry("rejected") and not F.should_solve("rejected")
          and not F.counts_as_blocked("rejected"))
    check("unauthorized: retried (a fresh landing reads a fresh key), NOT blocked",
          F.should_retry("unauthorized") and not F.counts_as_blocked("unauthorized"))
    check("challenge: retried, solved, blocked", F.should_retry("challenge")
          and F.should_solve("challenge") and F.counts_as_blocked("challenge"))
    check("throttled: retried, NOT blocked (§24)",
          F.should_retry("throttled") and not F.counts_as_blocked("throttled"))
    equal("at most one solve per page", F.SOLVES_PER_PAGE, 1)
    check("headless /search is told to go headful",
          "--headful" in F.refusal_advice("blocked", "https://www.goodreads.com/search?q=a", True))
    check("...and a headful run is not", "--headful" not in F.refusal_advice(
        "blocked", "https://www.goodreads.com/search?q=a", False))
    check("a CDP 401 is explained as expired credentials",
          "expired" in F.cdp_connect_hint("WebSocket error: 401 Unauthorized"))
    check("addressable: a list and books yes; reviews and a shelf no",
          F.addressable(_q("https://www.goodreads.com/list/show/1"))
          and F.addressable(_q("https://www.goodreads.com/book/show/1"))
          and not F.addressable(_q("https://www.goodreads.com/book/show/1", "reviews"))
          and not F.addressable(_q("https://www.goodreads.com/shelf/show/fantasy")))


# ---------------------------------------------------------------------------
# The shared fetch loop, driven end to end with a fake driver
# ---------------------------------------------------------------------------

class _FakeOps:
    """page_flow's named operations, answering from fixtures.

    `pages(url)` answers a navigation with (status, html); `api` is a queue
    of (status, text) for fetch() calls."""

    def __init__(self, pages=None, api=None):
        self.pages = pages or (lambda url: (200, fx("list_p1")))
        self.api = list(api or [])
        self.doc = ""
        self.landed = False
        self.pool = None
        self.gotos, self.fetches = [], 0
        self.relaunches = self.solves = 0

    def goto(self, url):
        self.gotos.append(url)
        status, self.doc = self.pages(url)
        return status, ("challenge" if status == 202 else None)

    def document_text(self):
        return self.doc

    def wait_ms(self, ms):
        pass

    def solve_captcha(self):
        self.solves += 1
        return False

    def fetch(self, req):
        self.fetches += 1
        status, text = self.api.pop(0) if len(self.api) > 1 else self.api[0]
        return status, text, None, None

    def relaunch(self):
        self.relaunches += 1
        self.landed = False

    def proxy_failure(self, text):
        return ""

    def close(self):
        pass


def _args(tmp, **kw):
    a = types.SimpleNamespace(
        url=None, mode=None, pages=1, retries=2, retry_delay=0, delay=0,
        proxy_block_retries=2, out=os.path.join(tmp, "out"), format="json",
        allow_empty=False, dump_html=None, cdp_endpoint=None, concurrency=1,
        headless=True, sort=None, rating=None, review_language=None,
        search_text=None, page_size=None, max_books=None)
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def _run(ops, **kw):
    import page_flow
    with tempfile.TemporaryDirectory() as tmp:
        args = _args(tmp, **kw)
        errors = []
        q = page_flow.build_query(args, errors.append)
        if errors:
            return ("usage", errors), None, None, ops
        rc = page_flow.run_pages(lambda: ops, lambda o: None,
                                 lambda pages, query: ([], [], False), args, None, q, 1)
        meta_path = args.out + ".meta.json"
        meta = json.load(open(meta_path)) if os.path.exists(meta_path) else None
        rows = (json.load(open(args.out + ".json"))
                if os.path.exists(args.out + ".json") else None)
    return rc, meta, rows, ops


def _listing(n_to_fixture):
    import product_parser as P
    return lambda url: (200, fx(n_to_fixture(P.requested_page(url))))


def check_the_shared_loop_end_to_end():
    rc, meta, rows, ops = _run(_FakeOps(_listing(lambda n: "list_p1")))
    equal("a served list page: exit 0", rc, 0)
    equal("...complete", meta and meta["status"], "complete")
    equal("...the site's total in the sidecar", meta and meta["total_results"], 79607)
    equal("...and that it caps what it serves (§21)",
          meta and (meta["reachable_max"], meta["capped_by_site"]), (10000, True))
    equal("...rows written", len(rows or []), 5)

    rc, meta, rows, ops = _run(_FakeOps(_listing(
        lambda n: {1: "list_p1", 2: "list_p2"}.get(n, "list_p800"))), pages=5)
    equal("page 3 served as page 100: the run ENDS there, exit 0", rc, 0)
    equal("...complete, stopped on the site's own answer",
          (meta["status"], meta["stop_reason"]), ("complete", "end_of_listing"))
    equal("...pages 1-3 fetched, not 4-5", len(ops.gotos), 3)
    equal("...and page 100's rows were NOT collected", len(rows), 8)
    check("page+position unique across the run (§18)",
          len({(r["page"], r["position"]) for r in rows}) == len(rows))

    rc, meta, rows, ops = _run(_FakeOps(lambda u: (404, fx("author_p999"))),
                               url=fx_url("author_p999"))
    equal("an author list past its end on page 1: exit 4, the listing is empty", rc, 4)

    rc, meta, rows, ops = _run(_FakeOps(lambda u: (403, "<html><head></head><body></body></html>")),
                               url="https://www.goodreads.com/search?q=dune")
    equal("a 403 on page 1: exit 3", rc, 3)
    equal("...re-fetched once from a fresh browser (no pool)", ops.relaunches, 1)

    rc, meta, rows, ops = _run(_FakeOps(lambda u: (202, "")),
                               url="https://www.goodreads.com/book/show/5907")
    equal("a WAF challenge that never clears: exit 3, not 5", rc, 3)
    equal("...and nothing was sent to the solver? (no widget: one attempt, returned False)",
          ops.solves <= 2, True)

    class Clearing(_FakeOps):
        """The WAF's own script reloads the page: the first reads see the
        challenge, the later ones the real book."""
        reads = 0

        def document_text(self):
            self.reads += 1
            return fx("waf_challenge") if self.reads <= 2 else fx("book_hobbit")

    rc, meta, rows, ops = _run(Clearing(lambda u: (202, fx("waf_challenge"))),
                               url="https://www.goodreads.com/book/show/5907")
    equal("a WAF challenge the browser clears by itself: exit 0", rc, 0)
    equal("...nothing paid for", ops.solves, 0)
    equal("...one navigation", len(ops.gotos), 1)

    half = ('<!DOCTYPE html><html><head><title>The Hobbit</title>'
            '<link rel="stylesheet" href="https://s.gr-assets.com/x.css"></head>'
            '<body>' + "<div>loading</div>" * 200 + '</body></html>')

    class HalfLoaded(_FakeOps):
        """After the WAF's reload the first read can land on the NEW page
        before it has arrived: no __NEXT_DATA__ yet, so "unknown"."""
        reads = 0

        def document_text(self):
            self.reads += 1
            return (fx("waf_challenge"), half, half)[self.reads - 1] if self.reads <= 3 \
                else fx("book_hobbit")
    rc, meta, rows, ops = _run(HalfLoaded(lambda u: (202, fx("waf_challenge"))),
                               url="https://www.goodreads.com/book/show/5907")
    equal("a half-loaded page after the reload is waited on, not retried: "
          "exit 0 from ONE navigation", (rc, len(ops.gotos)), (0, 1))

    rc, meta, rows, ops = _run(_FakeOps(lambda u: (200, fx("book_missing"))),
                               url=fx_url("book_missing"))
    equal("a book id that names nothing: exit 4", rc, 4)

    book = lambda u: (200, fx("book_hobbit"))
    rc, meta, rows, ops = _run(_FakeOps(book, [(200, fx("reviews_p1")), (200, fx("reviews_p2"))]),
                               url="https://www.goodreads.com/book/show/5907", mode="reviews",
                               pages=2)
    equal("two review pages: exit 0", rc, 0)
    equal("...landed ONCE on the book, then two API calls", (len(ops.gotos), ops.fetches), (1, 2))
    equal("...rows carry the book they are about", rows[0]["title"],
          "The Hobbit, or There and Back Again")

    rc, meta, rows, ops = _run(_FakeOps(book, [(401, fx("reviews_401")), (200, fx("reviews_p1"))]),
                               url="https://www.goodreads.com/book/show/5907", mode="reviews")
    equal("a 401 (the key refused), then served: exit 0", rc, 0)
    equal("...the page was landed on AGAIN for a fresh key", len(ops.gotos), 2)

    rc, meta, rows, ops = _run(_FakeOps(book, [(200, fx("reviews_bad_sort"))]),
                               url="https://www.goodreads.com/book/show/5907", mode="reviews")
    equal("a REJECTED request: exit 5, the data never arrived", rc, 5)
    equal("...sent once, not retried", ops.fetches, 1)

    rc, meta, rows, ops = _run(_FakeOps(book, [(200, fx("reviews_p1"))]),
                               url="https://www.goodreads.com/book/show/5907", mode="reviews",
                               review_language="xx")
    equal("a language the book has no reviews in: exit 2 before any API call", rc, 2)
    equal("...no review request was sent", ops.fetches, 0)

    rc, meta, rows, ops = _run(_FakeOps(book, [(200, fx("reviews_fr_1star"))]),
                               url="https://www.goodreads.com/book/show/5907", mode="reviews",
                               pages=5, review_language="fr", rating=1)
    equal("a feed that ends on page 1 of a planned 5: exit 0, complete",
          (rc, meta["status"]), (0, "complete"))
    equal("...one request, not five", ops.fetches, 1)

    throttled = [(429, ""), (200, fx("reviews_p1"))]
    rc, meta, rows, ops = _run(_FakeOps(book, throttled), url="https://www.goodreads.com/book/show/5907",
                               mode="reviews", retries=1)
    equal("a throttle wait spends its OWN budget, not --retries (§24, §26: "
          "with --retries 1 the page still arrives)", rc, 0)
    equal("...at the SAME exit", ops.relaunches, 0)


def check_a_book_run_from_a_listing_reads_every_book():
    pages = lambda u: (200, fx("series") if "/series/" in u else fx("book_hobbit"))
    rc, meta, rows, ops = _run(_FakeOps(pages), url=fx_url("series"), mode="book", max_books=3)
    equal("a series then three books: exit 0", rc, 0)
    equal("...one listing page and three book pages", len(ops.gotos), 4)
    check("...the book pages are the ones the listing named",
          ops.gotos[1].startswith("https://www.goodreads.com/book/show/7332"), ops.gotos[1])
    equal("...the sidecar says how many were planned", meta["books_planned"], 3)

    def one_fails(u):
        if "/series/" in u:
            return 200, fx("series")
        if "7332" in u:
            return 403, "<html><head></head><body></body></html>"
        return 200, fx("book_dune")
    rc, meta, rows, ops = _run(_FakeOps(one_fails), url=fx_url("series"), mode="book", max_books=2)
    equal("one book refused of two: exit 6, partial", (rc, meta["status"]), (6, "partial"))
    equal("...the refused book is named in the sidecar", meta["books_failed"], [1])


def check_usage_errors_are_refused_before_a_request():
    ops = _FakeOps()
    rc, *_ = _run(ops, url="https://www.goodreads.com/book/show/5907", mode="list")
    check("--mode list with a book url: refused", rc[0] == "usage")
    rc, *_ = _run(ops, url="https://www.goodreads.com/list/show/1", sort="newest")
    check("--sort on a list run: refused (it would do nothing)", rc[0] == "usage")
    rc, *_ = _run(ops, url="https://www.goodreads.com/book/show/1", mode="reviews", concurrency=3)
    check("--concurrency with reviews: refused, the cursor chains the pages (§18)",
          rc[0] == "usage" and "cursor" in rc[1][0])
    rc, *_ = _run(ops, url="https://www.goodreads.com/list/show/1", max_books=5)
    check("--max-books without --mode book: refused", rc[0] == "usage")
    equal("...and no page was fetched for any of them", ops.gotos, [])


def check_every_engine_implements_the_operations_page_flow_uses():
    """The fetch loop is shared, so an engine missing ONE operation fails
    only when a live run reaches it. The set is DERIVED from page_flow's own
    source (every `ops.<name>`), not listed by hand."""
    tree = ast.parse(open(os.path.join(HERE, "page_flow.py"), encoding="utf-8").read())
    used = {n.attr for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "ops"}
    check("page_flow drives the engines through named operations (not vacuous)",
          {"goto", "fetch", "document_text", "solve_captcha", "relaunch"} <= used,
          repr(sorted(used)))
    check("...and the fake driver this suite uses implements every one",
          all(hasattr(_FakeOps(), name) for name in used),
          repr(sorted(n for n in used if not hasattr(_FakeOps(), n))))
    for module in ENGINES:
        path = os.path.join(HERE, module + ".py")
        tree = ast.parse(open(path, encoding="utf-8").read())
        ops_cls = next((n for n in tree.body
                        if isinstance(n, ast.ClassDef) and n.name == "_Ops"), None)
        if ops_cls is None:
            check("%s defines _Ops" % module, False)
            continue
        methods = {n.name for n in ops_cls.body if isinstance(n, ast.FunctionDef)}
        attrs = {t.attr for n in ast.walk(ops_cls) if isinstance(n, ast.Assign)
                 for target in n.targets for t in ast.walk(target)
                 if isinstance(t, ast.Attribute)
                 and isinstance(t.value, ast.Name) and t.value.id == "self"}
        missing = sorted(used - methods - attrs)
        check("%s._Ops provides every operation page_flow uses" % module,
              not missing, "missing %s" % missing)


# ---------------------------------------------------------------------------
# The output contract
# ---------------------------------------------------------------------------

def check_row_schema():
    from output_writer import (Book, ListedBook, Review, ROW_CLASS_BY_MODE,
                               UNIQUE_BY_SKU_MODES)
    for cls in (ListedBook, Book, Review):
        names = [f.name for f in fields(cls)]
        equal("%s: the family prefix is byte-identical and in order (§9)" % cls.__name__,
              names[:5], ["source", "scraped_at", "url", "sku", "title"])
        equal("%s: the run-describing tail closes the row" % cls.__name__,
              names[-4:], ["page", "position", "mode", "data_source"])
        check("%s: no commerce column that would be null forever" % cls.__name__,
              not ({"price", "currency", "brand", "original_price", "discount_pct"} & set(names)))
    equal("every mode maps to its row class", sorted(ROW_CLASS_BY_MODE), ["book", "list", "reviews"])
    equal("every mode is one row per sku", sorted(UNIQUE_BY_SKU_MODES), ["book", "list", "reviews"])
    check("a review records the ordering that chose it", "sort" in {f.name for f in fields(Review)})


def check_csv_and_json_writers():
    from output_writer import Book, write_csv, write_json
    rows = _rows("book_hobbit")
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = os.path.join(tmp, "out.csv")
        write_csv(rows, csv_path, row_cls=Book)
        reader = list(csv.reader(open(csv_path, encoding="utf-8")))
        equal("CSV header matches the dataclass, in order", reader[0], [f.name for f in fields(Book)])
        equal("CSV holds every row", len(reader) - 1, len(rows))
        check("no Python list repr leaked into the CSV",
              not any(cell.startswith("['") for row in reader[1:] for cell in row))
        genres = reader[1][reader[0].index("genres")]
        check("a list column is ' | '-joined in CSV", genres.startswith("Fantasy | Classics"), genres)
        empty_csv = os.path.join(tmp, "empty.csv")
        write_csv([], empty_csv, row_cls=Book)
        equal("an EMPTY csv still carries its header",
              len(list(csv.reader(open(empty_csv, encoding="utf-8")))), 1)
        json_path = os.path.join(tmp, "out.json")
        write_json(rows, json_path)
        loaded = json.load(open(json_path, encoding="utf-8"))
        check("a list column stays a real list in JSON", isinstance(loaded[0]["genres"], list))


def check_exit_codes():
    import output_writer as O
    equal("3 blocked / 4 empty / 5 never obtained / 6 partial",
          (O.EXIT_BLOCKED, O.EXIT_NO_PRODUCTS, O.EXIT_FETCH_FAILED, O.EXIT_PARTIAL),
          (3, 4, 5, 6))
    check("end_of_listing is a COMPLETE stop reason (§24)",
          "end_of_listing" in O.COMPLETE_STOP_REASONS)
    check("api_rejected is NOT complete", "api_rejected" not in O.COMPLETE_STOP_REASONS)


def check_diff_runs_tracks_the_real_columns():
    import diff_runs as D
    for mode in ("list", "book", "reviews"):
        check("%s: tracked columns are derived and non-empty" % mode,
              len(D.tracked_fields(mode)) >= 3, repr(D.tracked_fields(mode)))
    check("ratings_count is tracked on a book", "ratings_count" in D.tracked_fields("book"))
    check("position is NOT tracked (a list reorders itself by votes)",
          "position" not in D.tracked_fields("list"))
    old = [asdict(r) for r in _rows("list_p1")]
    new = copy.deepcopy(old)
    new[0]["ratings_count"] += 17
    del new[1]
    result = D.diff_products(old, new)
    equal("one changed", [c["sku"] for c in result["changed"]], [old[0]["sku"]])
    equal("...with the count named", list(result["changed"][0]["changes"]), ["ratings_count"])
    equal("one removed", len(result["removed"]), 1)
    with tempfile.TemporaryDirectory() as tmp:
        a, b = os.path.join(tmp, "a.json"), os.path.join(tmp, "b.json")
        json.dump(old, open(a, "w"))
        json.dump(old, open(b, "w"))
        json.dump({"status": "complete", "mode": "reviews", "query": {"sort": "newest"}},
                  open(a[:-5] + ".meta.json", "w"))
        json.dump({"status": "complete", "mode": "reviews", "query": {"sort": "oldest"}},
                  open(b[:-5] + ".meta.json", "w"))
        check("two different review ORDERINGS are refused",
              not D._check_comparable(types.SimpleNamespace(old=a, new=b)))


def check_sidecar_shape():
    from output_writer import run_meta
    meta = run_meta(status="complete", stop_reason="completed", pages_requested=3,
                    pages_completed=3, pages_failed=[], products=300,
                    mode="list", source="goodreads.com",
                    start_url="https://www.goodreads.com/list/show/1",
                    final_url="https://www.goodreads.com/list/show/1?page=3",
                    extra={"total_results": 79607, "pages_available": 100,
                           "query": {"kind": "list"}})
    for key in ("status", "stop_reason", "pages_requested", "pages_completed",
                "pages_failed", "mode", "source", "total_results", "query"):
        check("the sidecar records %r" % key, key in meta)
    check("pages_failed is a LIST", isinstance(meta["pages_failed"], list))


# ---------------------------------------------------------------------------
# The family's own checks (CLAUDE.md §10, §17, §22, §24, §26)
# ---------------------------------------------------------------------------

def check_policy_constants_have_a_consumer():
    """§17: a policy constant nothing reads is the same defect as dead code."""
    src = open(os.path.join(HERE, "page_flow.py"), encoding="utf-8").read()
    for constant in ("RETRY_ON_BLOCKED", "BLOCK_RETRIES_WITHOUT_POOL",
                     "SOLVES_PER_PAGE", "THROTTLE_RETRIES", "THROTTLE_WAIT_S",
                     "CHALLENGE_SETTLE_MS", "CHALLENGE_POLL_MS", "FETCH_TIMEOUT_MS",
                     "CORE_FIELD_FLOOR"):
        uses = len(re.findall(r"\b%s\b" % constant, src))
        engines = "".join(open(os.path.join(HERE, m + ".py"), encoding="utf-8").read()
                          for m in ENGINES)
        check("page_flow.%s is READ, not only defined" % constant,
              uses >= 2 or constant in engines, "%d occurrence(s)" % uses)


# ---------------------------------------------------------------------------
# The shared fetch loop, driven end to end with a fake driver
# ---------------------------------------------------------------------------


def check_a_run_that_finds_nothing_writes_nothing():
    from output_writer import save
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "out")
        with open(prefix + ".json", "w", encoding="utf-8") as f:
            f.write('[{"sku": "yesterday"}]')
        equal("an empty run exits 4", save([], prefix, "json", allow_empty=False), 4)
        equal("...and leaves the previous good file alone",
              open(prefix + ".json", encoding="utf-8").read(), '[{"sku": "yesterday"}]')
        equal("--allow-empty writes it, and still reports exit 4",
              save([], prefix, "json", allow_empty=True), 4)


def check_engines_import_their_driver_at_module_level():
    for module, driver in DRIVER_IMPORTS.items():
        tree = ast.parse(open(os.path.join(HERE, module + ".py"), encoding="utf-8").read())
        top = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top.add(node.module.split(".")[0])
        check("%s imports %s at MODULE level" % (module, driver), driver in top,
              "top-level imports: %s" % sorted(top))


def check_shared_calls_bind_against_the_real_signature():
    """§17's check #1. Every call from an engine (and the Scraper API client,
    diff_runs and page_flow itself) into a shared module is bound against the
    callee's real signature. A name that does not exist FAILS (§22). A name
    bound in the calling file shadows a same-named module."""
    import captcha_solver
    import output_writer
    import page_flow
    import product_parser
    import proxy_pool
    targets = {"page_flow": page_flow, "product_parser": product_parser,
               "output_writer": output_writer, "captcha_solver": captcha_solver,
               "proxy_pool": proxy_pool}
    bound = 0
    for module in ENGINES + ("scraper_api_client", "diff_runs", "page_flow"):
        tree = ast.parse(open(os.path.join(HERE, module + ".py"), encoding="utf-8").read())
        local_names = {n.arg for n in ast.walk(tree) if isinstance(n, ast.arg)}
        direct = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in targets:
                for alias in node.names:
                    direct[alias.asname or alias.name] = (targets[node.module], alias.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func, owner, attr = node.func, None, None
            if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                    and func.value.id in targets and func.value.id not in local_names):
                owner, attr = targets[func.value.id], func.attr
            elif isinstance(func, ast.Name) and func.id in direct:
                owner, attr = direct[func.id]
            if owner is None:
                continue
            if not hasattr(owner, attr):
                check("%s.%s exists (called from %s:%d)" % (owner.__name__, attr,
                      module, node.lineno), False, "AttributeError on a live run")
                continue
            callee = getattr(owner, attr)
            if not callable(callee):
                continue
            try:
                sig = inspect.signature(callee)
            except (TypeError, ValueError):
                continue
            if any(kw.arg is None for kw in node.keywords) or any(
                    isinstance(a, ast.Starred) for a in node.args):
                continue
            try:
                sig.bind(*[None] * len(node.args), **{kw.arg: None for kw in node.keywords})
                bound += 1
            except TypeError as e:
                check("%s:%d %s.%s(...) binds against its real signature"
                      % (module, node.lineno, owner.__name__, attr), False,
                      "%s; signature is %s" % (e, sig))
    check("the binding walk checked something (%d calls)" % bound, bound > 60,
          "only %d calls were bound — is the walk finding them?" % bound)


def _argparse_flags(module_name):
    tree = ast.parse(open(os.path.join(HERE, module_name + ".py"), encoding="utf-8").read())
    parsers = {"p"}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr in ("add_argument_group",
                                             "add_mutually_exclusive_group")):
            parsers.update(t.id for t in node.targets if isinstance(t, ast.Name))
    flags = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in parsers):
            flags.update(a.value for a in node.args if isinstance(a, ast.Constant)
                         and isinstance(a.value, str) and a.value.startswith("--"))
    return flags


# The family's flag contract (CLAUDE.md §9, including the five it omitted
# for months), plus this repo's own query flags.
CONTRACT_FLAGS = {
    "--url", "--pages", "--category", "--format", "--out", "--delay",
    "--retries", "--retry-delay", "--concurrency", "--proxy", "--proxy-file",
    "--proxy-rotate", "--proxy-shuffle", "--proxy-block-retries",
    "--twocaptcha-key", "--captcha-api", "--solve-captcha", "--min-score",
    "--cdp-endpoint", "--allow-empty", "--dump-html", "--headless", "--headful",
    "--fingerprint", "--fp-country", "--fp-tags", "--locale", "--mode",
} - {"--category"}
# `--category` is the family's name for "which listing". Here a listing IS
# its url (a list, an author, a search, a shelf, a series), and a second
# flag naming it could only disagree with --url. The README says so.
GOODREADS_FLAGS = {"--sort", "--rating", "--review-language", "--search-text",
                   "--page-size", "--max-books"}


def check_engine_flag_sets():
    """§17's check #2: against the contract AND against each other, both
    ways. The exception list IS the documentation."""
    sets = {m: _argparse_flags(m) for m in ENGINES}
    for module, flags in sets.items():
        missing = (CONTRACT_FLAGS | GOODREADS_FLAGS) - flags
        check("%s defines every contract flag" % module, not missing,
              "missing %s" % sorted(missing))
    DOCUMENTED_DIFFERENCES = {"puppeteer_scraper": {"--chromium-path"}}
    names = sorted(sets)
    for i in range(len(names) - 1):
        a, b = names[i], names[i + 1]
        only_a = sets[a] - sets[b] - DOCUMENTED_DIFFERENCES.get(a, set())
        only_b = sets[b] - sets[a] - DOCUMENTED_DIFFERENCES.get(b, set())
        check("%s and %s define the same flags" % (a, b), not only_a and not only_b,
              "only in %s: %s; only in %s: %s" % (a, sorted(only_a), b, sorted(only_b)))
    check("the documented difference still exists (closing it must be a decision)",
          "--chromium-path" in sets["puppeteer_scraper"])


def check_banned_and_removed_flags():
    """Scoped to the engines. `--country` is banned: it could disagree with
    the --url, and Goodreads serves one site to every country."""
    for module in ENGINES:
        source = open(os.path.join(HERE, module + ".py"), encoding="utf-8").read()
        for flag in ("--antidetect", "--country", "--country-code"):
            check("%s does not define %s" % (module, flag), '"%s"' % flag not in source)


def check_undefined_names_in_every_module():
    """§10: compileall proves a file PARSES, not that its names RESOLVE.
    Kept COARSE (pooled bindings) so it under-reports rather than invents."""
    import builtins
    for filename in sorted(f for f in os.listdir(HERE) if f.endswith(".py")):
        tree = ast.parse(open(os.path.join(HERE, filename), encoding="utf-8").read())
        defined = set(dir(builtins)) | {"__file__", "__name__", "__doc__",
                                        "__package__", "__spec__"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    defined.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                defined.add(node.id)
            elif isinstance(node, ast.arg):
                defined.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                defined.add(node.name)
        used = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        unresolved = sorted(used - defined)
        check("%s: every name resolves" % filename, not unresolved, repr(unresolved))


def check_no_statement_is_unreachable():
    """A statement after a return/raise/break/continue in the SAME block."""
    for filename in sorted(f for f in os.listdir(HERE) if f.endswith(".py")):
        tree = ast.parse(open(os.path.join(HERE, filename), encoding="utf-8").read())
        dead = []
        for node in ast.walk(tree):
            for fld in ("body", "orelse", "finalbody"):
                block = getattr(node, fld, None)
                if not isinstance(block, list):
                    continue
                for i, stmt in enumerate(block[:-1]):
                    if isinstance(stmt, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
                        dead.append(block[i + 1].lineno)
                        break
        check("%s: no statement the control flow can never reach" % filename,
              not dead, "first at line %d" % min(dead) if dead else "")


def _import_graph(entrypoint):
    local = {f[:-3] for f in os.listdir(HERE) if f.endswith(".py")}
    seen, todo = set(), [entrypoint]
    while todo:
        name = todo.pop()
        if name in seen or name not in local:
            continue
        seen.add(name)
        tree = ast.parse(open(os.path.join(HERE, name + ".py"), encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                todo.extend(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                todo.append(node.module.split(".")[0])
    return seen


def check_dockerfile_copies_everything_the_entrypoint_imports():
    dockerfile = open(os.path.join(HERE, "Dockerfile"), encoding="utf-8").read()
    copy_lines, joining = [], False
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if joining or stripped.upper().startswith("COPY "):
            copy_lines.append(stripped)
            joining = stripped.endswith("\\")
    copied = set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\.py", " ".join(copy_lines)))
    missing = sorted(_import_graph("playwright_scraper") - copied)
    check("the Dockerfile COPYs every module playwright_scraper.py imports",
          not missing, "missing %s" % missing)
    check("the image does not carry the test suite", "smoke_test" not in copied)


def check_env_example_documents_exactly_what_the_loader_reads():
    import env_config
    text = open(os.path.join(HERE, ".env.example"), encoding="utf-8").read()
    documented = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", text, re.M))
    read = set(env_config.ENV_KEYS)
    equal("the example and the loader name the same variables",
          sorted(documented), sorted(read))
    check("the per-site variables carry the GOODREADS_ prefix",
          {"GOODREADS_CDP_ENDPOINT", "GOODREADS_PROXY", "GOODREADS_URL"} <= read)


def check_a_copied_env_example_reads_as_UNSET():
    import env_config
    text = open(os.path.join(HERE, ".env.example"), encoding="utf-8").read()
    values = dict(re.findall(r"^([A-Z][A-Z0-9_]+)=(.*)$", text, re.M))
    credentials = {"TWOCAPTCHA_KEY", "GOODREADS_CDP_ENDPOINT", "GOODREADS_PROXY"}
    before = dict(os.environ)
    try:
        for name, raw in values.items():
            os.environ[name] = raw
            got = env_config.env_value(name)
            if name in credentials:
                check("a copied .env.example leaves %s unset" % name, got is None, repr(got))
            else:
                check("...while %s stays a usable default" % name, got == raw.strip(), repr(got))
        os.environ["TWOCAPTCHA_KEY"] = "not-a-real-key-but-a-real-value"
        equal("a real value is still read", env_config.env_value("TWOCAPTCHA_KEY"),
              "not-a-real-key-but-a-real-value")
    finally:
        os.environ.clear()
        os.environ.update(before)


def check_credential_scan_is_one_implementation_invoked_from_both():
    script = os.path.join(HERE, ".github", "ci_checks.py")
    if not os.path.isdir(os.path.join(HERE, ".github")):
        # Inside the Docker image, which copies no .github at all. Triggered
        # by the WHOLE directory being absent, never by one file in it (§22).
        skip("ci_checks", "no .github directory (the image)")
        return
    check("the credential scan exists as a script", os.path.exists(script))
    workflow = open(os.path.join(HERE, ".github", "workflows", "tests.yml"),
                    encoding="utf-8").read()
    check("CI INVOKES the script rather than reimplementing it", "ci_checks.py" in workflow)
    result = subprocess.run([sys.executable, script, "--secret-check", "--sample-check"],
                            cwd=HERE, capture_output=True, text=True)
    check("the credential scan and sample check pass on this tree",
          result.returncode == 0, (result.stdout + result.stderr)[-600:])


def check_no_workflow_imports_the_code_inline():
    """The first push of this repo went red on an inline heredoc in
    tests.yml doing `from output_writer import Business`: the donor repo's
    row class, invisible to every local run because nothing local executes a
    workflow. A workflow calls ci_checks.py or the CLIs; it does not carry
    its own copy of a check that imports the code."""
    wf_dir = os.path.join(HERE, ".github", "workflows")
    if not os.path.isdir(wf_dir):
        skip("workflows", "no .github directory (the image)")
        return
    local = {f[:-3] for f in os.listdir(HERE) if f.endswith(".py")}
    pattern = re.compile(r"^\s*(?:from|import)\s+(%s)\b" % "|".join(sorted(local)), re.M)
    for name in sorted(os.listdir(wf_dir)):
        hits = pattern.findall(open(os.path.join(wf_dir, name), encoding="utf-8").read())
        check("%s imports no local module inline" % name, not hits, repr(hits))


def check_the_hex_exemption_is_one_context_only():
    """SITE_PUBLIC_IDS is EMPTY here: no row carries a 32-hex id, so a bare
    32-hex anywhere fails, including inside a goodreads.com url. Planted,
    not assumed."""
    if not os.path.isdir(os.path.join(HERE, ".github")):
        skip("ci_checks", "no .github directory (the image)")
        return
    sys.path.insert(0, os.path.join(HERE, ".github"))
    import ci_checks as C
    equal("no site id is exempted", C.SITE_PUBLIC_IDS, ())
    hexkey = "0123456789abcdef" * 2
    check("a 32-hex inside a goodreads.com url is still caught",
          bool(C.HEX32.search(C._without_site_ids("https://www.goodreads.com/x/" + hexkey))))
    check("...and in a key-shaped field", bool(C.HEX32.search(
        C._without_site_ids('"key": "%s"' % hexkey))))


# Assembled from pieces, so this file can be scanned like every other rather
# than exempted (§22: the file most likely to acquire a stray phrase is the
# one a wholesale exemption never reads).
BANNED_WORDING = (
    "cloud" + " browser", "anti" + "detect browser", "2scraper " + "Anti" + "detect Browser",
    "gate." + "2prx.com", "ANTI" + "DETECT_LOCAL_API",
)


def check_banned_wording():
    """§12, enforced by this test rather than by review."""
    scanned = 0
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".pytest_cache",
                                                "live", "captures", ".claude")]
        for filename in files:
            if not filename.endswith((".py", ".md", ".yml", ".yaml", ".txt",
                                      ".toml", ".html", ".example", ".json")):
                continue
            path = os.path.join(root, filename)
            text = open(path, encoding="utf-8", errors="replace").read().lower()
            scanned += 1
            for phrase in BANNED_WORDING:
                if phrase.lower() in text:
                    check("%s contains no banned phrase #%d" % (
                        os.path.relpath(path, HERE), BANNED_WORDING.index(phrase)), False)
    check("the banned-wording scan read the repo (%d files)" % scanned, scanned > 20)


def check_concurrency_with_the_browser_stubbed():
    """§10: a live run cannot always reach this machinery. Driven through the
    SHARED worker loop with the fetch replaced."""
    import page_flow
    import queue as queue_mod
    fetched, lock = [], threading.Lock()
    real = page_flow.fetch_one_page

    def fake(ops, args, pool, query, page_num, mask=None, pages=None):
        with lock:
            fetched.append(page_num)
        o = page_flow.PageOutcome(page_num=page_num, url="u")
        o.products = [] if page_num >= 6 else [object()]
        o.state = "empty" if page_num >= 6 else "content"
        return o

    work = queue_mod.Queue()
    for n in range(2, 51):
        work.put(n)
    results, rlock, exhausted = [], threading.Lock(), threading.Event()
    args = types.SimpleNamespace(delay=0, pages=50)
    page_flow.fetch_one_page = fake
    try:
        threads = [threading.Thread(target=page_flow.worker_loop,
                                    args=(_FakeOps(), args,
                                          _q("https://www.goodreads.com/list/show/1"), work, results,
                                          rlock, exhausted, "w%d" % i))
                   for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
    finally:
        page_flow.fetch_one_page = real
    check("every page fetched was fetched exactly once", len(fetched) == len(set(fetched)))
    check("dispatch STOPPED at the end of the listing", exhausted.is_set())
    check("...so 49 queued pages cost far fewer fetches", len(fetched) < 15,
          "fetched %d" % len(fetched))
    equal("attempted + unattempted covers the whole queue",
          len(set(fetched)) + work.qsize(), 49)


def check_a_dead_worker_neither_hangs_nor_loses_its_siblings():
    engine = _import_engine("playwright_scraper")
    if engine is None:
        return
    import page_flow

    def exploding(ops, args, pool, query, page_num, mask=None, pages=None):
        if page_num == 3:
            raise RuntimeError("worker died")
        o = page_flow.PageOutcome(page_num=page_num, url="u")
        o.products = [object()]
        o.state = "content"
        return o

    class FakeOps(_FakeOps):
        def __init__(self, *a, **k):
            super().__init__()

        def open(self):
            return self

    class FakePlaywright:
        def __enter__(self):
            return None

        def __exit__(self, *a):
            return False

    real = (page_flow.fetch_one_page, engine._Ops, engine.sync_playwright)
    page_flow.fetch_one_page = exploding
    engine._Ops = FakeOps
    engine.sync_playwright = lambda: FakePlaywright()
    try:
        results, unattempted, exhausted = engine._fetch_pages_concurrently(
            types.SimpleNamespace(delay=0, pages=8), None,
            _q("https://www.goodreads.com/list/show/1"), list(range(2, 8)), 3)
    finally:
        page_flow.fetch_one_page, engine._Ops, engine.sync_playwright = real
    check("the dead worker's siblings still delivered their pages",
          len(results) >= 3, "%d results" % len(results))
    check("page 3 is not reported as a success", 3 not in [o.page_num for o in results])


def check_worker_pools_start_on_different_exits():
    import page_flow
    from proxy_pool import ProxyPool
    pool = ProxyPool(["http://a:1", "http://b:2", "http://c:3"], rotate="per-run")
    equal("three workers start on three different exits",
          len({page_flow.worker_pool(pool, i).current for i in range(3)}), 3)
    equal("a missing pool stays missing", page_flow.worker_pool(None, 0), None)


def check_fingerprint_kwargs_are_ones_the_driver_accepts():
    engine = _import_engine("playwright_scraper")
    if engine is None:
        return
    from fingerprint_client import playwright_context_kwargs
    import playwright.sync_api as pw_api
    sample = {"id": "x", "country": "US", "userAgent": "Mozilla/5.0 Chrome/140.0.0.0",
              "screen": {"width": 1920, "height": 1080},
              "timezone": "America/New_York", "language": "en-US", "devicePixelRatio": 2}
    kwargs = playwright_context_kwargs(sample)
    signature = inspect.signature(pw_api.Browser.new_context)
    unknown = [k for k in kwargs if k not in signature.parameters]
    check("every fingerprint kwarg is one new_context accepts", not unknown, repr(unknown))


def check_engines_do_not_evaluate_a_string_in_the_browser():
    """§18: wait_for_function evaluates a string, which a CSP without
    unsafe-eval kills. page.evaluate with a real function is fine."""
    for module in ENGINES:
        tree = ast.parse(open(os.path.join(HERE, module + ".py"), encoding="utf-8").read())
        called = {n.func.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        for banned in ("wait_for_function", "waitForFunction", "waitFor"):
            check("%s never CALLS %s" % (module, banned), banned not in called)


def check_fetch_js_is_one_request_in_three_dialects():
    """The one piece of JavaScript each engine spells its own way. It must
    make the same request, with the same timeout and the same cookie rule."""
    for module in ENGINES:
        src = open(os.path.join(HERE, module + ".py"), encoding="utf-8").read()
        js = re.search(r'FETCH_JS = """(.*?)"""', src, re.S)
        check("%s defines FETCH_JS" % module, js is not None)
        if not js:
            continue
        body = js.group(1)
        # Cookies ride on a same-origin request only: the GraphQL API is on
        # another origin and its CORS answer admits none. The key rides in
        # the extra headers the request carries.
        for needle in ('same ? "include" : "omit"', "AbortController",
                       'r.headers.get("x-amzn-waf-action")', '"content-type"',
                       "location.origin"):
            check("%s's fetch() carries %s" % (module, needle), needle in body)


def check_credentials_never_reach_a_log():
    for module in ENGINES:
        engine = _import_engine(module)
        if engine is None:
            continue
        masked = engine._mask_credentials(
            "tried ws://u:supersecret@h1:9222 and ws://u:supersecret@h2:9222 "
            "and again ws://u:supersecret@h1:9222")
        check("%s masks EVERY occurrence" % module, "supersecret" not in masked, masked)
        check("%s keeps host and port" % module, "h1:9222" in masked and "h2:9222" in masked)
    from proxy_pool import mask
    masked = mask("http://user:secret@exit.example.com:2334")
    check("proxy_pool.mask hides the password", "secret" not in masked)
    check("proxy_pool.mask keeps the exit", "exit.example.com:2334" in masked)


def check_aws_waf_solution_prefers_existing_token():
    """Measured 2026-09-24: `existing_token` set as the aws-waf-token cleared
    the CAPTCHA, a captcha_voucher set the same way did not. Driven through
    the real solve with requests stubbed — no network."""
    import captcha_solver as C
    # The challenge page carries the same key/iv/context an AmazonTask
    # sends (as placeholders here); the solve path is driven with requests
    # stubbed, so which WAF action the page was does not matter to it.
    ch = C.detect_aws_waf(fx("waf_challenge"), fx_url("waf_challenge"))
    sent = []

    class Resp:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            pass

        def json(self):
            return self.body

    def make_post(solution):
        def post(url, json=None, timeout=None):
            sent.append(json)
            if "createTask" in url:
                return Resp({"errorId": 0, "taskId": 1})
            return Resp({"errorId": 0, "status": "ready", "solution": solution})
        return post

    real_post, real_sleep = C.requests.post, C.time.sleep
    C.time.sleep = lambda s: None
    try:
        C.requests.post = make_post({"captcha_voucher": "V", "existing_token": "E"})
        both = C._solve_with_2captcha_v2("k", ch)
        C.requests.post = make_post({"captcha_voucher": "V"})
        only_voucher = C._solve_with_2captcha_v2("k", ch)
        C.requests.post = make_post({"existing_token": "E", "bnc-uuid": "x"})
        only_existing = C._solve_with_2captcha_v2("k", ch)
    finally:
        C.requests.post, C.time.sleep = real_post, real_sleep
    equal("with both, existing_token is the cookie value", both, "E")
    equal("with only a voucher, the voucher", only_voucher, "V")
    equal("with existing_token and the site's cookies, existing_token", only_existing, "E")
    task = sent[0]["task"]
    equal("no proxy: the Proxyless task type", task["type"], "AmazonTaskProxyless")
    check("the task carries iv and context", task.get("iv") and task.get("context"))


def check_scraper_api_sends_waitfor_as_an_object_and_reads_http_code():
    """Measured 2026-09-23 against the live Scraper API: a JSON-encoded
    STRING waitFor is answered HTTP 422 and still billed; the target's status
    is `http_code`, while `status` is the API's own verdict."""
    try:
        import scraper_api_client as sac
    except ImportError as e:
        skip("scraper_api_client", str(e))
        return
    sent = {}

    class Resp:
        status_code = 200
        headers = {}
        text = ""

        def json(self):
            return {"status": "success", "http_code": 403, "headers": {},
                    "body": "<html></html>"}

    def post(url, **kw):
        sent.update(kw.get("json") or {})
        return Resp()

    args = types.SimpleNamespace(url="https://www.goodreads.com/list/show/1", key="k" * 8,
                                 timeout=60, cdp_url=None, wait_text="000000",
                                 wait_element=None, wait_state=None)
    real = sac.requests.post
    sac.requests.post = post
    try:
        _html, status = sac.fetch_html(args)
    finally:
        sac.requests.post = real
    equal("--wait-text sends waitFor as an OBJECT", sent.get("waitFor"), {"text": "000000"})
    equal("the target status handed onward is http_code", status, 403)



def check_x_debug_header_is_redacted():
    try:
        import scraper_api_client as sac
    except ImportError:
        return
    pw = "SeCr" + "EtPw"
    key = "abcdef01" * 4
    raw = ("cdpurl=ws://acct-zone-scraping_browser-pid-7:" + pw
           + "@cb.2captcha.com:9222 cost=0.00145 key=" + key + " status=200")
    out = sac._redact_debug_header(raw)
    check("x-debug: the credential and the key are gone", pw not in out and key not in out)
    check("x-debug: the cost, host and status survive",
          "cost=0.00145" in out and "cb.2captcha.com:9222" in out and "status=200" in out)
    check("x-debug: the log line calls the redactor",
          'logger.info("x-debug: %s", _redact_debug_header(debug))' in inspect.getsource(sac))


def check_captcha_capability_claims_match_the_code():
    """§19: the most expensive bug this family can ship is a SENTENCE."""
    readme = open(os.path.join(HERE, "README.md"), encoding="utf-8").read()
    solver = open(os.path.join(HERE, "captcha_solver.py"), encoding="utf-8").read()
    low = readme.lower()
    for phrase in ("cannot be solved", "can't be solved", "is not solvable",
                   "solver is inapplicable", "no solver can"):
        check("README: no %r — write 'this repo does not implement X'" % phrase,
              phrase not in low)
    check("the solver builds AmazonTask, which the README credits",
          "AmazonTask" in solver and "amazontask" in low)


def check_readme_numbers_are_not_stale():
    """§17's check #4: a column count claimed in the README is a class's."""
    readme = open(os.path.join(HERE, "README.md"), encoding="utf-8").read()
    from output_writer import ROW_CLASS_BY_MODE
    sizes = {len(fields(c)) for c in ROW_CLASS_BY_MODE.values()}
    for number in re.findall(r"(\d+)\s+columns", readme):
        check("the README's '%s columns' is a row class's size" % number,
              int(number) in sizes, "sizes are %s" % sorted(sizes))
    import product_parser as P
    for claim, value in (("100 books on a list", P.ROWS_PER_PAGE["list"]),
                         ("30 on an author's", P.ROWS_PER_PAGE["author"]),
                         ("20 on a search", P.ROWS_PER_PAGE["search"]),
                         ("30 reviews", P.REVIEWS_PAGE_SIZE)):
        n = int(claim.split()[0])
        if claim in readme:
            equal("the README's %r matches the code" % claim, value, n)


_TREE_BEFORE = None


def _tree_state():
    result = subprocess.run(["git", "status", "--porcelain"], cwd=HERE,
                            capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return sorted(line for line in result.stdout.splitlines() if not line.endswith(".pyc"))


def check_no_test_mutates_the_working_tree():
    if _TREE_BEFORE is None:
        skip("git status", "not a git repository")
        return
    changed = sorted(set(_tree_state()) - set(_TREE_BEFORE))
    check("the suite itself changed nothing in the working tree", not changed, repr(changed))


CHECKS = [v for k, v in sorted(globals().items()) if k.startswith("check_")]


def main():
    global VERBOSE, _TREE_BEFORE
    parser = argparse.ArgumentParser(description="goodreads-scraper offline suite")
    parser.add_argument("-v", "--verbose", action="store_true")
    VERBOSE = parser.parse_args().verbose
    _TREE_BEFORE = _tree_state()
    for fn in CHECKS:
        if VERBOSE:
            print("\n== %s" % fn.__name__)
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — a broken check is a failure
            import traceback
            FAILURES.append("%s raised %s: %s" % (fn.__name__, type(e).__name__, e))
            print("  ERROR %s raised %s: %s" % (fn.__name__, type(e).__name__, e))
            if VERBOSE:
                traceback.print_exc()
    print("\n%d checks passed, %d failed, %d group(s) skipped."
          % (PASSED, len(FAILURES), len(SKIPS)))
    for line in SKIPS:
        print("  skipped: %s" % line)
    if FAILURES:
        print("\nFailures:")
        for line in FAILURES:
            print("  - %s" % line)
        return 1
    return 0



if __name__ == "__main__":
    sys.exit(main())
