"""
product_parser.py
-----------------
Everything this repo knows about goodreads.com lives here (CLAUDE.md §1).

Three modes
-----------
    --mode list      a LISTING page, read from the page itself:
                         /list/show/{id}      Listopia, 100 books a page
                         /author/list/{id}    an author's books, 30 a page
                         /search?q=...        book search, 20 a page
                         /shelf/show/{name}   a genre shelf, 50 books
                         /series/{id}         a series, one page
    --mode book      a book's own page, /book/show/{id}: its Apollo cache
                     (the page's __NEXT_DATA__) cross-checked against its
                     schema.org/Book JSON-LD. Given a LISTING url, every
                     book on the listing's pages is read in turn.
    --mode reviews   a book's community reviews, through the site's own
                     front-end GraphQL API (AWS AppSync), 30 a page by
                     default, walked by the cursor the API hands back.

What was measured, and what it decided
--------------------------------------
Measured 2026-09-27 from a datacentre address (netcup, AS197540):

    /book/show/{id}        HTTP 202, empty body, x-amzn-waf-action:
                           challenge -- AWS WAF's JS CHALLENGE, not its
                           CAPTCHA. Intermittent: the same address was
                           served 200 an hour earlier and again later.
                           Real Chromium passed it by itself in 1.7-2.4 s,
                           headless and headful, 3 of 3 each.
    /search?q=             202 to curl; headless Chromium HTTP 403 with a
                           39-byte body (3 of 3); HEADFUL Chromium served
                           (3 of 3). A browser gets a different page from
                           curl: a Next.js App Router page whose books are
                           in its React (RSC) payload, where curl, when it
                           is served, gets the old Rails table.
    /list/show, /author/list, /shelf/show, /series
                           HTTP 200 to plain curl, every time.

Every listing page is server-rendered, and three of them carry schema.org
microdata (`itemtype="http://schema.org/Book"`), which is the anchor this
parser uses: a standard, not a build-generated class (§4). None of the
listing pages carries a JSON-LD block. The book page carries exactly one
(`@type: Book`), and a much richer Apollo cache beside it.

The API behind reviews
----------------------
The book page publishes, to every visitor, the key its own front end uses
for the GraphQL API (`pageProps.apiKey`, a `da2-` AppSync key). Without it
the API answers 401. With it, `getReviews` walks a book's reviews by an
opaque cursor, and walking The Hobbit 11,000 reviews deep (110 pages of 100,
44 s) met no ceiling and no duplicate. The key is never stored: every run
reads it from the page it lands on, because an AppSync key expires.

Its silent fallbacks, each measured, each refused before a request is sent:

    languageCode "xx"          -> HTTP 200, totalCount 0 on a book with
                                  95,000 reviews
    pagination.limit 200       -> HTTP 200, ZERO edges (100 works)
    sort "BOGUS"               -> a validation error: loud, called
                                  `rejected` so nobody hunts a proxy problem

Out-of-range pages answer 200 with a DIFFERENT page
---------------------------------------------------
    /list/show/1?page=800      -> page 100, the list's last (it states
                                  79,607 books and serves 100 pages of 100)
    /search?q=dune&page=999    -> "Page 100 of 100"
    /shelf/show/fantasy?page=2 -> page 1 again, to an anonymous visitor
    /author/list/{id}?page=999 -> HTTP 404

So every listing page is asked which page it SERVED (§23), and an answer
that differs from the page requested is the end of the listing, not more
data.
"""

import html as html_lib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

from output_writer import Book, ListedBook, Review, SOURCE_DEFAULT

BASE = "https://www.goodreads.com"
HOSTS = ("www.goodreads.com", "goodreads.com")

# The production AppSync endpoint, from the site's own `_app` bundle, which
# names three: Development/Beta, Preprod and Production. Only this one
# accepted the page's key (the other two answered 401, 2026-09-27).
GRAPHQL_ENDPOINT = ("https://kxbwmqov6jgg3daaamb744ycu4.appsync-api."
                    "us-east-1.amazonaws.com/graphql")

# ---------------------------------------------------------------------------
# Page sizes and ceilings, measured per listing kind
# ---------------------------------------------------------------------------

LIST_KINDS = ("list", "author", "search", "shelf", "series")

ROWS_PER_PAGE = {"list": 100, "author": 30, "search": 20, "shelf": 50,
                 "series": 100}

# Which kinds can be addressed page by page. A shelf answers ?page=2 with
# page 1 to an anonymous visitor, and a series is one page (its own
# pagination props say `perPage: 100`), so both are single-page listings
# and --pages above 1 on them is said, not silently ignored.
ADDRESSABLE_KINDS = ("list", "author", "search")

# Reviews: the site's own page is 30. The API takes 1..100; 200 returned
# ZERO edges with HTTP 200, so the ceiling is enforced here.
REVIEWS_PAGE_SIZE = 30
REVIEWS_MAX_PAGE_SIZE = 100

# The API's orderings. Each gave a different first review on The Hobbit;
# anything else is a validation error.
REVIEW_SORTS = {"default": "DEFAULT", "newest": "NEWEST", "oldest": "OLDEST"}

# A ceiling on --pages, so a typo cannot start a ten-thousand-request run.
MAX_PAGES = 1000

# ---------------------------------------------------------------------------
# Requests and the query
# ---------------------------------------------------------------------------


@dataclass
class ApiRequest:
    """One thing to fetch.

    `navigate` requests are PAGES: the browser goes to `url` and the
    document is what gets parsed, because every listing and book page is
    server-rendered HTML and AWS WAF's challenge can only run in a real
    navigation. The rest are the GraphQL API, issued as a `fetch()` from a
    www.goodreads.com page, which is the only place the API key comes from.
    """
    url: str
    method: str = "GET"
    body: Optional[Dict[str, Any]] = None
    headers: Dict[str, str] = field(default_factory=dict)
    label: str = ""
    navigate: bool = True

    @property
    def body_json(self) -> Optional[str]:
        return None if self.body is None else json.dumps(self.body)


@dataclass
class Query:
    mode: str = "list"
    # The page the run starts from, normalised: tracking parameters gone.
    url: str = ""
    # What that url is: one of LIST_KINDS, or "book".
    kind: str = "list"
    # The page the url itself asks for (?page=N). A run started on page 3
    # calls it page 1 of the RUN and asks the site for page 3; the check
    # that the site served what was asked compares against the page the
    # URL names, never the loop counter (§24).
    start_page: int = 1
    # book mode from a listing: at most this many books (None: all found).
    max_books: Optional[int] = None
    # --- reviews ---
    sort: str = "default"
    rating: Optional[int] = None
    review_language: Optional[str] = None
    search_text: Optional[str] = None
    page_size: int = REVIEWS_PAGE_SIZE
    # --- book mode from a listing: the books phase A found, in page order ---
    book_urls: List[str] = field(default_factory=list)
    # --- learned from the landing page, never typed ---
    api_key: Optional[str] = field(default=None, repr=False)
    work_id: Optional[str] = None
    work_legacy_id: Optional[str] = None
    book_id: Optional[str] = None
    book_title: Optional[str] = None
    languages: Optional[List[str]] = None
    # page number -> the cursor that page starts after. Page 1 has none.
    cursors: Dict[int, Optional[str]] = field(default_factory=dict)

    def validate(self) -> Optional[str]:
        if self.mode not in ("list", "book", "reviews"):
            return "unknown --mode %r" % self.mode
        if self.mode == "list" and self.kind not in LIST_KINDS:
            return ("--mode list needs a listing url (a list, an author's "
                    "books, a search, a shelf or a series); %s is a %s page. "
                    "Use --mode book for it." % (self.url, self.kind))
        if self.mode == "reviews":
            if self.kind != "book":
                return ("--mode reviews needs a book url (/book/show/{id}); "
                        "%s is a %s page." % (self.url, self.kind))
            if self.sort not in REVIEW_SORTS:
                return "--sort must be one of %s" % ", ".join(REVIEW_SORTS)
            if self.rating is not None and not 1 <= self.rating <= 5:
                return "--rating must be 1-5"
            if not 1 <= self.page_size <= REVIEWS_MAX_PAGE_SIZE:
                return ("--page-size must be 1-%d: the API answers a larger "
                        "one with HTTP 200 and ZERO reviews, which would "
                        "read as a book nobody reviewed."
                        % REVIEWS_MAX_PAGE_SIZE)
            if self.review_language is not None and not re.fullmatch(
                    r"[a-z]{2,3}", self.review_language):
                return ("--review-language takes an ISO 639 code, lower "
                        "case: en, fr, es ...")
        return None


def request_for(query: Query, page: int) -> ApiRequest:
    """What to fetch for page `page` of this run."""
    if query.mode == "list":
        url = page_url(query.url, query.start_page + page - 1) \
            if query.kind in ADDRESSABLE_KINDS else query.url
        return ApiRequest(url=url, label=url)
    if query.mode == "book":
        url = query.book_urls[page - 1] if query.book_urls else query.url
        return ApiRequest(url=url, label=url)
    return reviews_request(query, page)


# The fields a Review row reads, and nothing else: a smaller answer, and a
# query that states what the parser depends on.
REVIEWS_QUERY = """query getReviews($filters: BookReviewsFilterInput!, $pagination: PaginationInput) {
  getReviews(filters: $filters, pagination: $pagination) {
    totalCount
    pageInfo { nextPageToken }
    edges { node {
      id rating spoilerStatus text createdAt updatedAt likeCount commentCount
      shelving { webUrl shelf { name } taggings { tag { name } } }
      creator { id legacyId name webUrl isAuthor followersCount textReviewsCount }
    } }
  }
}"""


def reviews_request(query: Query, page: int) -> ApiRequest:
    filters: Dict[str, Any] = {"resourceType": "WORK",
                               "resourceId": query.work_id or "",
                               "sort": REVIEW_SORTS[query.sort]}
    if query.rating is not None:
        filters["ratingMin"] = filters["ratingMax"] = query.rating
    if query.review_language:
        filters["languageCode"] = query.review_language
    if query.search_text:
        filters["searchText"] = query.search_text
    pagination: Dict[str, Any] = {"limit": query.page_size}
    cursor = query.cursors.get(page)
    if cursor:
        pagination["after"] = cursor
    return ApiRequest(
        url=GRAPHQL_ENDPOINT, method="POST",
        body={"operationName": "getReviews", "query": REVIEWS_QUERY,
              "variables": {"filters": filters, "pagination": pagination}},
        headers={"x-api-key": query.api_key or ""},
        label="reviews of %s, page %d" % (query.url, page), navigate=False)


def landing_url(query: Query) -> str:
    """Where a reviews run lands before its first API call: the book page,
    which is the only place the key and the work id are published."""
    return query.url


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

_BOOK_RE = re.compile(r"^/book/show/(\d+)")
_KIND_RES = (
    ("book", re.compile(r"^/book/show/(\d+)")),
    ("list", re.compile(r"^/list/show/(\d+)")),
    ("author", re.compile(r"^/author/list/(\d+)")),
    ("search", re.compile(r"^/search/?$")),
    ("shelf", re.compile(r"^/shelf/show/([^/?#]+)")),
    ("series", re.compile(r"^/series/(\d+)")),
)
# A search result links to its book with a tracking tail. Dropped, so one
# book has one url, and so two runs of one search compare.
_TRACKING = {"from_search", "from_srp", "qid", "rank", "ref", "ac",
             "from_choice", "from_home_module"}


def kind_of(url: str) -> Optional[str]:
    path = urlparse(url).path
    for kind, rx in _KIND_RES:
        if rx.match(path):
            return kind
    return None


def query_from_url(url: str) -> Tuple[Optional[Query], Optional[str]]:
    """The Query a url describes, or (None, why) for one this repo cannot
    read. The reason names what the url IS, never "not a Goodreads url"
    when it is one (§5)."""
    u = urlparse(url if "://" in url else "https://" + url)
    host = (u.hostname or "").lower()
    if host not in HOSTS:
        return None, "%s is not a goodreads.com url" % url
    kind = kind_of(u.geturl())
    if kind is None:
        return None, ("%s is a goodreads.com page this repo does not read. "
                      "It reads /book/show/{id}, /list/show/{id}, "
                      "/author/list/{id}, /search?q=..., /shelf/show/{name} "
                      "and /series/{id}. An author's PROFILE "
                      "(/author/show/{id}) links to /author/list/{id}, "
                      "which is the paginated list of their books."
                      % u.path)
    params = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
              if k not in _TRACKING and k != "page"]
    if kind == "search" and not dict(params).get("q"):
        return None, "a search url needs ?q=..."
    if kind == "book":
        params = []
    clean = urlunparse(("https", "www.goodreads.com", u.path, "",
                        urlencode(params), ""))
    mode = "list" if kind in LIST_KINDS else "book"
    start = requested_page(u.geturl()) if kind in ADDRESSABLE_KINDS else 1
    return Query(mode=mode, url=clean, kind=kind, start_page=start), None


def page_url(url: str, page: int) -> str:
    """`url` asking for page `page`: ?page=N, replacing rather than
    duplicating an existing one, other parameters kept. Page 1 carries no
    parameter, which is the address the site's own links use."""
    u = urlparse(url)
    params = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
              if k != "page"]
    if page > 1:
        params.append(("page", str(page)))
    return urlunparse(u._replace(query=urlencode(params)))


def requested_page(url: str) -> int:
    for k, v in parse_qsl(urlparse(url).query):
        if k == "page" and v.isdigit():
            return int(v)
    return 1


def book_url(book_id: Any, slug_url: Optional[str] = None) -> str:
    """The book's canonical page, from its id; a slug url is kept when the
    site gave one, minus any tracking tail."""
    if slug_url:
        u = urlparse(slug_url if "://" in slug_url else BASE + slug_url)
        if _BOOK_RE.match(u.path):
            return urlunparse(("https", "www.goodreads.com", u.path, "", "", ""))
    return "%s/book/show/%s" % (BASE, book_id)


def _book_id(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    m = _BOOK_RE.match(urlparse(url if "://" in url else BASE + url).path)
    return m.group(1) if m else None


def _author_id(url: Optional[str]) -> Optional[str]:
    m = re.search(r"/author/show/(\d+)", url or "")
    return m.group(1) if m else None


def _clean_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    u = urlparse(url if "://" in url else BASE + url)
    return urlunparse(("https", u.hostname or "www.goodreads.com", u.path,
                       "", "", ""))


# ---------------------------------------------------------------------------
# Page states
# ---------------------------------------------------------------------------

# AWS WAF's own vocabulary. Counted on every served capture (0 each) and
# present on the challenge. `awswaf` alone is NOT here: the site's `_app`
# bundle names a WAF challenge script in its config, and a marker that
# matches a served page is worse than none (§18).
AWS_WAF_MARKERS = ("gokuProps", "token.awswaf.com", "challenge.js")
WAF_STATUSES = (202, 405)
# A page the site built references its own asset host; an interstitial or
# Chromium's own error page does not (§8, §18). 9 to 298 occurrences on
# every served capture, the 404 and the missing-book page included.
OWN_ASSET = "gr-assets.com"


def detect_bot_challenge(html: Optional[str], url: str = "") -> Optional[str]:
    """The vendor gating this document, or None."""
    head = (html or "")[:200_000]
    if any(m in head for m in AWS_WAF_MARKERS) and OWN_ASSET not in head:
        return "aws-waf"
    return None


def _json_or_none(text: Optional[str]) -> Any:
    t = (text or "").lstrip()
    if not t.startswith(("{", "[")):
        return None
    try:
        return json.loads(t)
    except ValueError:
        return None


def detect_page_state(text: Optional[str], status: Optional[int] = None,
                      url: str = "", waf_action: Optional[str] = None) -> str:
    """Name what the site answered with. The names are page_flow's policy
    states; order is by how much each signal PROVES (§17): a WAF header or
    status first, then the page's own content, then heuristics."""
    text = text or ""
    if waf_action or (status in WAF_STATUSES and len(text) < 20_000):
        return "challenge"
    if detect_bot_challenge(text, url):
        return "challenge"
    if status == 429 or status == 503:
        return "throttled"
    if status == 403:
        return "blocked"

    payload = _json_or_none(text)
    if payload is not None:
        return _api_state(payload, status)
    if status == 401:
        return "unauthorized"

    kind = kind_of(url) or "list"
    if status == 404:
        # /author/list past its end, or a url that names nothing. The site
        # served its own 404 page: an answer, not a failure.
        return "empty" if OWN_ASSET in text else "unknown"
    if kind == "book":
        nd = next_data(text)
        if nd is None:
            return "unknown"
        return "content" if _root_book(nd) else "empty"
    n = count_listing_items(text, kind)
    if n:
        return "content"
    if _says_no_results(text):
        return "empty"
    return "unknown"


def _api_state(payload: Any, status: Optional[int]) -> str:
    if not isinstance(payload, dict):
        return "unknown"
    errors = payload.get("errors") or []
    if status == 401 or any("Unauthorized" in str(e.get("errorType", ""))
                            for e in errors if isinstance(e, dict)):
        # The key the landing page gave has expired or was never read. A
        # fresh landing reads a fresh one.
        return "unauthorized"
    got = (payload.get("data") or {}).get("getReviews")
    if got is None:
        return "rejected" if errors else "unknown"
    return "content" if got.get("edges") else "empty"


def api_error(text: Optional[str]) -> Optional[str]:
    """The API's own complaint, for the log."""
    payload = _json_or_none(text)
    if not isinstance(payload, dict):
        return None
    msgs = [str(e.get("message", "")) for e in payload.get("errors") or []
            if isinstance(e, dict)]
    return "; ".join(m for m in msgs if m)[:300] or None


def _says_no_results(html: str) -> bool:
    # The new search page's own sentence, and the old one's.
    return ("No results." in html or "No results found" in html
            or "Looks like we can" in html)


# ---------------------------------------------------------------------------
# Small readers
# ---------------------------------------------------------------------------

def _int(v: Any) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    m = re.search(r"-?\d[\d,]*", str(v))
    return int(m.group(0).replace(",", "")) if m else None


def _float(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def _date_ms(v: Any) -> Optional[str]:
    """Epoch milliseconds as an ISO date. Negative values are real: The
    Hobbit's first publication is -1018627200000 (1937-09-21)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        d = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=float(v))
    except (TypeError, ValueError, OverflowError):
        return None
    return d.date().isoformat()


def _iso_ms(v: Any) -> Optional[str]:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=float(v))
    except (TypeError, ValueError, OverflowError):
        return None
    return d.isoformat().replace("+00:00", "Z")


def _text(el) -> Optional[str]:
    if el is None:
        return None
    t = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
    return t or None


def html_to_text(html: Optional[str]) -> Optional[str]:
    """A review's or a blurb's HTML as plain text, paragraphs kept."""
    if not html:
        return None
    t = re.sub(r"(?i)<br\s*/?>", "\n", html)
    t = re.sub(r"(?i)</(p|div|blockquote)>", "\n\n", t)
    t = BeautifulSoup(t, "html.parser").get_text()
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip() or None


def _rating_pair(avg: Any, count: Any) -> Tuple[Optional[float], Optional[int]]:
    """A book nobody has rated shows "0.00 avg rating — 0 ratings". Both go
    null together, so the zero does not drag an average (§21)."""
    c = _int(count)
    if not c:
        return None, None
    return _float(avg), c


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------

_MINIRATING_RE = re.compile(r"(\d+(?:\.\d+)?)\s*avg rating\s*[—-]\s*([\d,]+)\s*ratings?")
_SHELF_RATING_RE = re.compile(r"avg rating\s*(\d+(?:\.\d+)?)\s*[—-]\s*([\d,]+)\s*ratings?")
_PUBLISHED_RE = re.compile(r"published\s+(-?\d{1,4})\b")
_EDITIONS_RE = re.compile(r"([\d,]+)\s+editions?")
_SCORE_RE = re.compile(r"score:\s*([\d,]+)")
_VOTED_RE = re.compile(r"([\d,]+)\s+people voted")
_SHELVED_RE = re.compile(r"shelved\s+([\d,]+)\s+times")


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html or "", "html.parser")


def _microdata_tiles(soup) -> List[Any]:
    return soup.select('[itemtype="http://schema.org/Book"]')


def count_listing_items(html: str, kind: str) -> int:
    """How many books a listing page holds, by the structure its kind uses.
    Cheap enough to classify with, and the same readers parse_listing uses,
    so the two cannot disagree about whether a page has content."""
    if not html:
        return 0
    if kind == "series":
        return sum(len(p.get("series") or []) for p in _series_props(html))
    if kind == "shelf":
        return len(_soup(html).select("div.elementList a.bookTitle"))
    if kind == "search" and "__next_f" in html:
        return len(_rsc_books(html))
    return html.count('itemtype="http://schema.org/Book"')


def _parse_microdata(html: str, query: Query, page: int, kind: str) -> List[ListedBook]:
    rows = []
    for tile in _microdata_tiles(_soup(html)):
        a = tile.select_one("a.bookTitle") or tile.select_one('a[href*="/book/show/"]')
        href = a.get("href") if a else None
        bid = _book_id(href)
        if not bid:
            continue
        name = tile.select_one('[itemprop="name"]')
        author_a = tile.select_one("a.authorName")
        text = _text(tile) or ""
        m = _MINIRATING_RE.search(text)
        avg, count = _rating_pair(m.group(1), m.group(2)) if m else (None, None)
        pub = _PUBLISHED_RE.search(text)
        ed = tile.select_one('a[href*="/work/editions/"]')
        wm = re.search(r"/work/editions/(\d+)", ed.get("href", "")) if ed else None
        edm = _EDITIONS_RE.search(_text(ed) or "") if ed else None
        img = tile.select_one("img.bookCover")
        row = ListedBook(
            url=book_url(bid, href), sku=bid, title=_text(name),
            author=_text(author_a.select_one('[itemprop="name"]') or author_a) if author_a else None,
            author_url=_clean_url(author_a.get("href")) if author_a else None,
            author_id=_author_id(author_a.get("href")) if author_a else None,
            avg_rating=avg, ratings_count=count,
            published_year=_int(pub.group(1)) if pub else None,
            editions_count=_int(edm.group(1)) if edm else None,
            work_id=wm.group(1) if wm else None,
            cover_url=img.get("src") if img else None,
            listing_kind=kind, page=page, mode=query.mode,
            data_source="microdata")
        if kind == "list":
            sm, vm = _SCORE_RE.search(text), _VOTED_RE.search(text)
            row.list_score = _int(sm.group(1)) if sm else None
            row.list_votes = _int(vm.group(1)) if vm else None
        rows.append(row)
    return rows


def _parse_shelf(html: str, query: Query, page: int) -> List[ListedBook]:
    rows = []
    for el in _soup(html).select("div.elementList"):
        a = el.select_one("a.bookTitle")
        bid = _book_id(a.get("href")) if a else None
        if not bid:
            continue
        author_a = el.select_one("a.authorName")
        text = _text(el) or ""
        m = _SHELF_RATING_RE.search(text)
        avg, count = _rating_pair(m.group(1), m.group(2)) if m else (None, None)
        pub, sh = _PUBLISHED_RE.search(text), _SHELVED_RE.search(text)
        img = el.select_one("a.leftAlignedImage img")
        rows.append(ListedBook(
            url=book_url(bid, a.get("href")), sku=bid, title=_text(a),
            author=_text(author_a) if author_a else None,
            author_url=_clean_url(author_a.get("href")) if author_a else None,
            author_id=_author_id(author_a.get("href")) if author_a else None,
            avg_rating=avg, ratings_count=count,
            published_year=_int(pub.group(1)) if pub else None,
            shelved_count=_int(sh.group(1)) if sh else None,
            cover_url=img.get("src") if img else None,
            listing_kind="shelf", page=page, mode=query.mode,
            data_source="shelf-html"))
    return rows


_SERIES_PROPS_RE = re.compile(
    r'data-react-class="ReactComponents\.SeriesList"\s+data-react-props="([^"]*)"')
_SERIES_HEADER_RE = re.compile(r"<h3[^>]*>\s*(Book [^<]{1,40}?)\s*</h3>")


def _series_props(html: str) -> List[Dict[str, Any]]:
    out = []
    for raw in _SERIES_PROPS_RE.findall(html or ""):
        try:
            out.append(json.loads(html_lib.unescape(raw)))
        except ValueError:
            continue
    return out


def _parse_series(html: str, query: Query, page: int) -> List[ListedBook]:
    items = [it.get("book") or {} for p in _series_props(html)
             for it in (p.get("series") or [])]
    # The "Book 1" / "Book 0.5" labels are headings beside each entry, in
    # the same order: 15 headings for 15 entries on the Lord of the Rings.
    # Used only when the counts agree, so a moved heading cannot shift
    # every label by one.
    headers = _SERIES_HEADER_RE.findall(html)
    labels = headers if len(headers) == len(items) else [None] * len(items)
    rows = []
    for b, label in zip(items, labels):
        bid = str(b.get("bookId") or "") or _book_id(b.get("bookUrl"))
        if not bid:
            continue
        author = b.get("author") or {}
        avg, count = _rating_pair(b.get("avgRating"), b.get("ratingsCount"))
        rows.append(ListedBook(
            url=book_url(bid, b.get("bookUrl")), sku=bid,
            title=b.get("title") or b.get("bookTitleBare"),
            author=author.get("name"), author_url=_clean_url(author.get("profileUrl")),
            author_id=str(author["id"]) if author.get("id") is not None else None,
            avg_rating=avg, ratings_count=count,
            published_year=_int(b.get("publicationDate")),
            editions_count=_int(b.get("editions")),
            work_id=str(b["workId"]) if b.get("workId") else None,
            cover_url=b.get("imageUrl"), series_position=label,
            listing_kind="series", page=page, mode=query.mode,
            data_source="series-props"))
    return rows


_RSC_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', re.S)


def _rsc_flight(html: str) -> str:
    out = []
    for chunk in _RSC_PUSH_RE.findall(html or ""):
        try:
            out.append(json.loads('"' + chunk + '"'))
        except ValueError:
            continue
    return "".join(out)


def _rsc_books(html: str) -> List[Dict[str, Any]]:
    """Every Book object in the search page's React payload, in first-seen
    order, merged by id.

    One card mentions its book several times (cover, title, actions), each
    time with a different subset of fields, so the objects are merged. The
    first-seen order is the card order: 20 of 20 on a captured page."""
    flight = _rsc_flight(html)
    dec = json.JSONDecoder()
    books: Dict[Any, Dict[str, Any]] = {}
    for m in re.finditer(r'\{"__typename":"Book",', flight):
        try:
            obj, _ = dec.raw_decode(flight, m.start())
        except ValueError:
            continue
        lid = obj.get("legacyId")
        if lid is None:
            continue
        have = books.setdefault(lid, {})
        for k, v in obj.items():
            if k not in have or have[k] in (None, "", [], {}):
                have[k] = v
    return list(books.values())


def _parse_rsc_search(html: str, query: Query, page: int) -> List[ListedBook]:
    rows = []
    for b in _rsc_books(html):
        bid = str(b.get("legacyId"))
        work = b.get("work") if isinstance(b.get("work"), dict) else {}
        stats = work.get("stats") or {}
        node = ((b.get("primaryContributorEdge") or {}).get("node") or {})
        avg, count = _rating_pair(stats.get("averageRating"), stats.get("ratingsCount"))
        series = (b.get("bookSeries") or [{}])[0] if b.get("bookSeries") else {}
        title = b.get("title")
        s = series.get("series") if isinstance(series, dict) else None
        if isinstance(s, dict) and s.get("title") and title and "(" not in title:
            # The table and the list print the series inside the title;
            # the new page keeps it apart. Rebuilt the same way, so a title
            # reads alike whichever page a run was served.
            pos = series.get("seriesPlacement") or series.get("userPosition")
            title = "%s (%s%s)" % (title, s["title"], ", #%s" % pos if pos else "")
        pub = (b.get("details") or {}).get("publicationTime")
        rows.append(ListedBook(
            url=book_url(bid, b.get("webUrl")), sku=bid, title=title,
            author=node.get("name"), author_url=_clean_url(node.get("webUrl")),
            author_id=str(node["legacyId"]) if node.get("legacyId") is not None else None,
            avg_rating=avg, ratings_count=count,
            published_year=_int((_date_ms(pub) or "")[:4]) if pub is not None else None,
            editions_count=_int((work.get("details") or {}).get("booksCount")),
            work_id=str(work["legacyId"]) if work.get("legacyId") is not None else None,
            cover_url=b.get("imageUrl"), listing_kind="search", page=page,
            mode=query.mode, data_source="rsc"))
    return rows


def parse_listing(html: str, query: Query, page: int = 1,
                  kind: Optional[str] = None) -> List[ListedBook]:
    kind = kind or query.kind
    if kind == "series":
        rows = _parse_series(html, query, page)
    elif kind == "shelf":
        rows = _parse_shelf(html, query, page)
    elif kind == "search" and "__next_f" in (html or "") and _rsc_books(html):
        rows = _parse_rsc_search(html, query, page)
    else:
        rows = _parse_microdata(html, query, page, kind)
    for i, r in enumerate(rows, 1):
        r.position = i
    return rows


# --- which page was served, and how many there are ------------------------

_PAGE_OF_RE = re.compile(r"Page\s+(\d+)\s+of\s+(\d+)")


def _pagination(html: str):
    """The pagination block: the parent of will_paginate's `em.current`.

    Not `div.pagination`, which the list page has and the author list and
    the old search table do not; all three render will_paginate's own
    markup (`em.current`, `a.next_page`) inside whatever box they chose.
    Only a box that also holds a page link counts, so a stray
    `em.current` elsewhere on the page cannot pose as one."""
    for em in _soup(html).select("em.current"):
        box = em.parent
        if box is not None and box.select_one('a[href*="page="]'):
            return box
    return None


def served_page(html: str, kind: str) -> Optional[int]:
    """The page number the SITE says it served, or None where it says
    nothing. An out-of-range request is answered with a different page
    (module docstring), and this is how the run notices."""
    if kind == "search" and "__next_f" in (html or ""):
        m = _PAGE_OF_RE.search(html)
        return int(m.group(1)) if m else None
    if kind in ("list", "author", "search"):
        box = _pagination(html)
        if box is not None:
            return _int(_text(box.select_one("em.current")))
        # A listing short enough to have no pagination block is page 1.
        return 1 if count_listing_items(html, kind) else None
    return 1


def pages_available(html: str, kind: str) -> Optional[int]:
    if kind not in ADDRESSABLE_KINDS:
        return 1
    if kind == "search" and "__next_f" in (html or ""):
        m = _PAGE_OF_RE.search(html)
        return int(m.group(2)) if m else None
    pag = _pagination(html)
    if pag is None:
        return 1 if count_listing_items(html, kind) else None
    nums = [_int(_text(x)) for x in pag.select("a, em.current")]
    nums = [n for n in nums if n]
    return max(nums) if nums else 1


_LIST_TOTAL_RE = re.compile(r"<title>[^<]*\(([\d,]+) books\)")
_SEARCH_TOTAL_RE = re.compile(r"of about ([\d,]+) results|of ([\d,]+) books\)")
_SHELF_TOTAL_RE = re.compile(r"Showing\s+[\d,]+\s*-\s*[\d,]+\s+of\s+([\d,]+)")
_SERIES_WORKS_RE = re.compile(r"(\d+)\s+primary works?\s*[•·]\s*(\d+)\s+total works?")


def total_results(html: str, kind: str) -> Optional[int]:
    """The site's own count of what the listing holds, where it states one.
    Often far above what it will SERVE: a list states 79,607 books and
    serves 10,000; a shelf states 100,000 and serves 50."""
    html = html or ""
    if kind == "list":
        m = _LIST_TOTAL_RE.search(html)
        return _int(m.group(1)) if m else None
    if kind == "search":
        m = _SEARCH_TOTAL_RE.search(html)
        return _int(m.group(1) or m.group(2)) if m else None
    if kind == "shelf":
        m = _SHELF_TOTAL_RE.search(_text(_soup(html)) or "")
        return _int(m.group(1)) if m else None
    if kind == "series":
        props = _series_props(html)
        return sum(len(p.get("series") or []) for p in props) or None
    return None


# ---------------------------------------------------------------------------
# The book page
# ---------------------------------------------------------------------------

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_JSONLD_RE = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def next_data(html: Optional[str]) -> Optional[Dict[str, Any]]:
    m = _NEXT_DATA_RE.search(html or "")
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


def _apollo(nd: Dict[str, Any]) -> Dict[str, Any]:
    return ((nd.get("props") or {}).get("pageProps") or {}).get("apolloState") or {}


def _deref(ap: Dict[str, Any], v: Any) -> Dict[str, Any]:
    if isinstance(v, dict) and "__ref" in v:
        return ap.get(v["__ref"]) or {}
    return v if isinstance(v, dict) else {}


def _root_book(nd: Dict[str, Any]) -> Dict[str, Any]:
    """The page's own book, by the ROOT_QUERY entry that asked for it. A
    book page's cache also holds a dozen OTHER books (similar titles, the
    series), so "the first Book in the cache" is not an answer."""
    ap = _apollo(nd)
    root = ap.get("ROOT_QUERY") or {}
    for k, v in root.items():
        if k.startswith("getBookByLegacyId"):
            return _deref(ap, v)
    return {}


def _jsonld_book(html: str) -> Dict[str, Any]:
    for raw in _JSONLD_RE.findall(html or ""):
        try:
            node = json.loads(raw)
        except ValueError:
            continue
        for n in (node if isinstance(node, list) else [node]):
            if isinstance(n, dict) and n.get("@type") == "Book":
                return n
    return {}


def _social(ap: Dict[str, Any]) -> Dict[str, int]:
    for k, v in (ap.get("ROOT_QUERY") or {}).items():
        if k.startswith("getSocialSignals") and isinstance(v, list):
            return {s.get("name"): _int(s.get("count")) for s in v if isinstance(s, dict)}
    return {}


def parse_book(html: str, query: Query, page: int = 1) -> List[Book]:
    nd = next_data(html) or {}
    ap = _apollo(nd)
    b = _root_book(nd)
    ld = _jsonld_book(html)
    if not b and not ld:
        return []
    work = _deref(ap, b.get("work"))
    stats = work.get("stats") or {}
    wd = work.get("details") or {}
    d = b.get("details") or {}

    edges = [b.get("primaryContributorEdge")] + list(b.get("secondaryContributorEdges") or [])
    contributors, first = [], {}
    for e in edges:
        if not isinstance(e, dict):
            continue
        node = _deref(ap, e.get("node"))
        if not node.get("name"):
            continue
        first = first or node
        contributors.append("%s (%s)" % (node["name"], e.get("role") or "Contributor"))
    if not contributors:
        for a in ld.get("author") or []:
            if isinstance(a, dict) and a.get("name"):
                first = first or {"name": a["name"], "webUrl": a.get("url")}
                contributors.append(a["name"])

    series, series_url, series_pos = None, None, None
    if b.get("bookSeries"):
        bs = b["bookSeries"][0]
        s = _deref(ap, bs.get("series"))
        series, series_url = s.get("title"), s.get("webUrl")
        series_pos = bs.get("userPosition") or None

    dist = stats.get("ratingsCountDist") or []
    dist = [(_int(x) if x is not None else None) for x in dist] if len(dist) == 5 else [None] * 5
    rating_ld = ld.get("aggregateRating") or {}
    avg, count = _rating_pair(stats.get("averageRating", rating_ld.get("ratingValue")),
                              stats.get("ratingsCount", rating_ld.get("ratingCount")))
    if count is None:
        dist = [None] * 5
    social = _social(ap)
    awards = []
    for aw in wd.get("awardsWon") or []:
        if not isinstance(aw, dict) or not aw.get("name"):
            continue
        year = (_date_ms(aw.get("awardedAt")) or "")[:4]
        label = aw["name"] + (" — %s" % aw["category"] if aw.get("category") else "")
        awards.append("%s (%s)" % (label, year) if year else label)

    bid = str(b.get("legacyId") or "") or _book_id(query.url) or None
    # JSON-LD carries one `isbn` of either length; the Apollo cache names
    # both. The block fills only what the cache left empty.
    ld_isbn = str(ld.get("isbn") or "")
    reviews = _int(stats.get("textReviewsCount", rating_ld.get("reviewCount")))
    row = Book(
        url=book_url(bid, b.get("webUrl")) if bid else query.url,
        sku=bid, title=b.get("titleComplete") or b.get("title") or ld.get("name"),
        author=first.get("name"), author_url=_clean_url(first.get("webUrl")),
        author_id=_author_id(first.get("webUrl")),
        contributors=contributors or None,
        work_id=str(work["legacyId"]) if work.get("legacyId") is not None else None,
        avg_rating=avg, ratings_count=count,
        reviews_count=reviews,
        rating_1=dist[0], rating_2=dist[1], rating_3=dist[2],
        rating_4=dist[3], rating_5=dist[4],
        review_languages=["%s:%s" % (x.get("isoLanguageCode"), x.get("count"))
                          for x in stats.get("textReviewsLanguageCounts") or []
                          if isinstance(x, dict) and x.get("isoLanguageCode")] or None,
        first_published=_date_ms(wd.get("publicationTime")),
        original_title=wd.get("originalTitle") or None,
        genres=[g["genre"]["name"] for g in b.get("bookGenres") or []
                if isinstance(g, dict) and isinstance(g.get("genre"), dict)
                and g["genre"].get("name")] or None,
        series=series, series_url=series_url, series_position=series_pos,
        awards=awards or None,
        characters=[c["name"] for c in wd.get("characters") or []
                    if isinstance(c, dict) and c.get("name")] or None,
        places=[p["name"] for p in wd.get("places") or []
                if isinstance(p, dict) and p.get("name")] or None,
        currently_reading=social.get("CURRENTLY_READING"),
        want_to_read=social.get("TO_READ"),
        format=d.get("format") or ld.get("bookFormat"),
        pages=_int(d.get("numPages", ld.get("numberOfPages"))),
        published_at=_date_ms(d.get("publicationTime")),
        publisher=d.get("publisher") or None,
        language=((d.get("language") or {}).get("name") if isinstance(d.get("language"), dict)
                  else None) or ld.get("inLanguage"),
        isbn=d.get("isbn") or (ld_isbn if len(ld_isbn) == 10 else None),
        isbn13=d.get("isbn13") or (ld_isbn if len(ld_isbn) == 13 else None),
        asin=d.get("asin") or None,
        description=html_to_text(b.get('description({"stripped":true})') or b.get("description")),
        cover_url=b.get("imageUrl") or ld.get("image"),
        page=page, position=1, mode=query.mode,
        data_source="apollo+jsonld" if b else "jsonld")
    return [row]


def book_landing(html: str) -> Dict[str, Any]:
    """What a reviews run needs from the book page it landed on: the API
    key, the work id, the book's own id and title, and the languages the
    site has reviews in (the allowlist for --review-language)."""
    nd = next_data(html) or {}
    pp = (nd.get("props") or {}).get("pageProps") or {}
    ap = _apollo(nd)
    b = _root_book(nd)
    work = _deref(ap, b.get("work"))
    stats = work.get("stats") or {}
    return {
        "api_key": pp.get("apiKey") or None,
        "work_id": work.get("id"),
        "book_id": str(b["legacyId"]) if b.get("legacyId") is not None else None,
        "book_title": b.get("titleComplete") or b.get("title"),
        "work_legacy_id": str(work["legacyId"]) if work.get("legacyId") is not None else None,
        "reviews_total": _int(stats.get("textReviewsCount")),
        "languages": [x.get("isoLanguageCode") for x in stats.get("textReviewsLanguageCounts") or []
                      if isinstance(x, dict) and x.get("isoLanguageCode")] or None,
    }


def check_review_language(wanted: Optional[str], offered: Optional[Sequence[str]]) -> Optional[str]:
    """Refuse a --review-language the book has no reviews in, with the list
    it does have. The API answers one with an EMPTY result, not an error,
    so without this a typo is an exit-4 run on a book full of reviews."""
    if not wanted or offered is None:
        return None
    if wanted in offered:
        return None
    return ("--review-language %s: this book has no reviews in that language. "
            "It has reviews in: %s." % (wanted, ", ".join(offered)))


# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------

_REVIEW_ID_RE = re.compile(r"/review/show/(\d+)")


def _reviews_block(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return ((payload.get("data") or {}).get("getReviews")) or {}


def parse_reviews(text: str, query: Query, page: int = 1) -> List[Review]:
    block = _reviews_block(_json_or_none(text))
    rows = []
    for edge in block.get("edges") or []:
        n = (edge or {}).get("node") or {}
        shelving = n.get("shelving") or {}
        url = shelving.get("webUrl") or ""
        m = _REVIEW_ID_RE.search(url)
        creator = n.get("creator") or {}
        rating = _int(n.get("rating"))
        rows.append(Review(
            url=url, sku=m.group(1) if m else n.get("id"),
            title=query.book_title, book_id=query.book_id,
            work_id=query.work_legacy_id,
            rating=rating if rating else None,
            text=html_to_text(n.get("text")),
            spoiler=n.get("spoilerStatus") if isinstance(n.get("spoilerStatus"), bool) else None,
            created_at=_iso_ms(n.get("createdAt")),
            updated_at=_iso_ms(n.get("updatedAt")),
            likes=_int(n.get("likeCount")), comments=_int(n.get("commentCount")),
            shelf=((shelving.get("shelf") or {}).get("name")),
            tags=[t["tag"]["name"] for t in shelving.get("taggings") or []
                  if isinstance(t, dict) and isinstance(t.get("tag"), dict)
                  and t["tag"].get("name")] or None,
            reviewer=creator.get("name"),
            reviewer_id=str(creator["legacyId"]) if creator.get("legacyId") is not None else None,
            reviewer_url=creator.get("webUrl"),
            reviewer_is_author=creator.get("isAuthor") if isinstance(creator.get("isAuthor"), bool) else None,
            reviewer_followers=_int(creator.get("followersCount")),
            reviewer_reviews=_int(creator.get("textReviewsCount")),
            sort=query.sort, page=page, mode=query.mode, data_source="graphql"))
    for i, r in enumerate(rows, 1):
        r.position = i
    return rows


def next_cursor(text: str) -> Optional[str]:
    return (_reviews_block(_json_or_none(text)).get("pageInfo") or {}).get("nextPageToken") or None


def reviews_total(text: str) -> Optional[int]:
    return _int(_reviews_block(_json_or_none(text)).get("totalCount"))


# ---------------------------------------------------------------------------
# One entry point per page, for the loop
# ---------------------------------------------------------------------------

def parse_page(text: str, query: Query, page: int = 1) -> List[Any]:
    if query.mode == "reviews":
        return parse_reviews(text, query, page)
    if query.mode == "book":
        return parse_book(text, query, page)
    return parse_listing(text, query, page)


def page_totals(text: str, query: Query, page_size: Optional[int] = None) -> Tuple[Optional[int], Optional[int]]:
    """(total_results, pages_available) as this page states them."""
    if query.mode == "reviews":
        total = reviews_total(text)
        size = page_size or query.page_size
        return total, (math.ceil(total / size) if total is not None else None)
    if query.mode == "book":
        return None, None
    return total_results(text, query.kind), pages_available(text, query.kind)
