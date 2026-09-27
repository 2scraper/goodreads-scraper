# goodreads-scraper

[![release](https://img.shields.io/github/v/release/2scraper/goodreads-scraper)](https://github.com/2scraper/goodreads-scraper/releases)
[![tests](https://github.com/2scraper/goodreads-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/goodreads-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/goodreads-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/goodreads-scraper/actions/workflows/canary.yml)
![python](https://img.shields.io/badge/python-3.9%20%7C%203.12-blue)
[![licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE)
![engines](https://img.shields.io/badge/engines-playwright%20%7C%20selenium%20%7C%20pyppeteer-lightgrey)
![runs without an account](https://img.shields.io/badge/all%20three%20modes-no%20account%20needed-brightgreen)

Scrapes [goodreads.com](https://www.goodreads.com) into JSON and CSV. The
site retired its public API in December 2020; this reads what its pages show
every visitor, and what its own front end fetches.

| `--mode` | what | one row per | per page |
|---|---|---|---|
| `list` (default) | a **listing**: a Listopia list, an author's books, a search, a genre shelf, a series. Title, author, rating and count, publication year, editions, a list's score and votes | book | 100 on a list, 30 on an author's, 20 on a search |
| `book` | a **book's own page**: every contributor with their role, the rating distribution, review counts per language, genres, series, awards, characters, places, edition details (ISBN, ASIN, pages, publisher, dates), the blurb. From a book url, or from a LISTING url: every book on its pages | book | one book |
| `reviews` | a book's **community reviews**, walked as deep as you ask: stars, text, date, likes, comments, the reviewer's own shelf and tags, the reviewer | review | 30 reviews (1-100) |

Every run writes a `<out>.meta.json` beside the output with the site's own
count, so a file can say "90 of 95,086" rather than only "90", and whether
the site caps what it will serve.

---

## Start with the part most scrapers bury

**You need no key, no proxy and no account for any of the three modes.**

Measured 2026-09-27 from a datacentre VPS (netcup, Nuremberg):

| what was asked | answer |
|---|---|
| a book page, plain curl | HTTP 202, empty body, `x-amzn-waf-action: challenge` (AWS WAF), intermittently: the same address was served 200 an hour before and after |
| the same page in real Chromium, headless and headful | **served, 1.0-2.4 s later**, by the WAF's own script (3 of 3 each). Nothing to solve, nothing paid |
| a search, headless Chromium | HTTP 403, an empty document (3 of 3) |
| the same search, headful Chromium | served (3 of 3) |
| lists, author lists, shelves, series, plain curl | HTTP 200, every time |
| a book's reviews, through the site's own GraphQL API with the key its book page hands every visitor | HTTP 200; **11,000 reviews walked, 110 pages of 100, 44 s, no ceiling, no duplicate** |

So the engines wait for AWS WAF's challenge instead of calling it a block,
and run searches with `--headful`. What the paid products buy is below, and
it is not "getting in".

---

## Install

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-playwright.txt
playwright install chromium
```

Install exactly one engine per virtualenv (their pins conflict; see
`requirements-*.txt`).

## Run

```bash
# the Best Books Ever list, 3 pages of 100
python3 playwright_scraper.py --pages 3

# every book J.R.R. Tolkien is credited on, 27 pages
python3 playwright_scraper.py --url https://www.goodreads.com/author/list/656983.J_R_R_Tolkien --pages 30

# a search: run it headful (xvfb-run -a on a server)
python3 playwright_scraper.py --url "https://www.goodreads.com/search?q=dune" --pages 5 --headful

# a book's full page
python3 playwright_scraper.py --url https://www.goodreads.com/book/show/5907.The_Hobbit

# every book in a series, each one's full page
python3 playwright_scraper.py --mode book --url https://www.goodreads.com/series/66175-the-lord-of-the-rings

# the first 20 books of a list, each one's full page, two browsers at once
python3 playwright_scraper.py --mode book --url https://www.goodreads.com/list/show/1.Best_Books_Ever --max-books 20 --concurrency 2

# a book's reviews: 10 pages of 30, newest first
python3 playwright_scraper.py --mode reviews --url https://www.goodreads.com/book/show/5907.The_Hobbit --sort newest --pages 10

# ...only one-star reviews in French
python3 playwright_scraper.py --mode reviews --url https://www.goodreads.com/book/show/5907.The_Hobbit --rating 1 --review-language fr --pages 50
```

Output: `goodreads_rows.json`, `goodreads_rows.csv` and
`goodreads_rows.meta.json` (`--out` changes the prefix). One real run of
each mode is committed: [list](sample_output.json), [book](sample_output_book.json),
[reviews](sample_output_reviews.json) (the reviews sample's reviewer names,
ids and words are placeholders; everything else is as the run wrote it).

---

## Six things about Goodreads that will look like bugs

### 1. A page past the end is ANOTHER page, not an error

`/list/show/1?page=800` answers HTTP 200 with page 100, the list's last.
`/search?q=dune&page=999` answers "Page 100 of 100". A genre shelf answers
`?page=2` with page 1 again, to an anonymous visitor. A scraper that trusts
its own request re-collects the last page for as long as it is asked to.
Every page here is asked which page it served, and a mismatch ends the
listing (`stop_reason: end_of_listing`, a complete run).

### 2. Listings state far more than they serve

The Best Books Ever list states 79,607 books and serves 100 pages of 100.
A search serves 100 pages of 20; a shelf states 100,000 books and serves 50.
The sidecar records `total_results`, `reachable_max` and `capped_by_site`,
so "complete" never reads as "exhaustive".

### 3. An author's list repeats books, and hides as many others

Measured on Tolkien's list (27 pages, 791 places): sorted by popularity, the
site served 720 distinct books and showed 71 of them twice, identically on
two runs. Sorted by title (`?sort=title` in the url), 739 and 52. **The two
orderings together served all 791.** The repeats are dropped, the sidecar
counts them (`repeated_across_pages`), and the log says that many books were
never served under that ordering. For the whole list, run both orderings and
merge on `sku`.

### 4. Ratings belong to the work, not the edition

`sku` is the edition's id, the number in `/book/show/5907`. The rating,
the rating count, the distribution and every review belong to the WORK (all
editions together), which is `work_id`. Two editions of one book carry the
same figures.

### 5. The review API answers a wrong value with nothing

A language the book has no reviews in returns 0 reviews, not an error; so
does a page size of 200. `--review-language` is checked against the
languages the book's own page lists (The Hobbit has reviews in 55) and a
page size above 100 is refused, both before anything is sent. `totalCount`
is the API's own count and differs slightly from the book page's figure
(95,086 against 95,336 on 2026-09-27); both are recorded.

### 6. Zero is not a rating

A review with no stars comes back as `rating: 0` (2 of 100 measured), and an
unrated book as "0.00 avg rating — 0 ratings". Both are written as null,
so an average computed over the column is not dragged down by them.

---

## Engines

| | |
|---|---|
| `playwright_scraper.py` | **Primary.** Authenticates a proxy and a remote CDP endpoint. |
| `puppeteer_scraper.py` | pyppeteer is effectively unmaintained; here for parity. Authenticates a proxy and a CDP endpoint. `--chromium-path` points it at another browser if its own will not start. |
| `selenium_scraper.py` | Drives the Chrome you already have. **Cannot authenticate a proxy or a remote CDP endpoint** (`debuggerAddress` is a bare `host:port`), so it refuses a credentialled `--cdp-endpoint` with exit 2. None of the modes needs either. |
| `scraper_api_client.py` | No local browser: the 2Captcha Scraper API fetches the page. Reads `--mode list` and one `--mode book` url. **This repo does not implement `--mode reviews` through the Scraper API**: the reviews API answers POST, and the Scraper API fetches a URL. Measured: 2 list pages (200 books) and a book page, $0.0005 a page. |

The fetch loop itself (navigating, the WAF's challenge, retries, throttling,
rotation, the API key, parsing) is one implementation in `page_flow.py` that
all three browser engines drive, so they cannot disagree about a page. All
three ran live on 2026-09-27, headless, in all three modes (2 list pages, a
book, 2 review pages) and wrote identical rows in identical order.

`--category` is the one family flag this repo does not take: a listing here
IS its url, and a second flag naming it could only disagree with `--url`.

---

## What the 2Captcha products buy, and when

One key, four separately-billed products ([2captcha.com](https://2captcha.com)):

* **Captcha solving**: AWS WAF's CAPTCHA, with the AmazonTask and
  AmazonTaskProxyless task types
  ([docs](https://2captcha.com/api-docs/amazon-aws-waf-captcha)). Every WAF
  page this repo met on goodreads.com was the free CHALLENGE action, which a
  browser passes by itself, so a normal run buys nothing. The solve path is
  there for a session the WAF shows its CAPTCHA instead; this repo has not
  met that CAPTCHA here, so the path is exercised offline only.
* **Proxies** (`--proxy`, `--proxy-file`): volume from more than one
  address. AWS WAF scores an address by its request rate, and
  `--concurrency` without a pool sends N times the rate from one.
* **The Scraping Browser API** (`--cdp-endpoint`): a remote browser you do
  not run. One live connection per `pid`, so `--concurrency` is ignored with
  it. **Not run live for this release**: no current profile was available
  (a profile's credentials last about a day).
* **Fingerprints** (`--fingerprint`): a consistent device identity for a
  local browser. Run live on 2026-09-27: a US fingerprint, a book page,
  the WAF's challenge cleared in 1.0 s. Ignored with `--cdp-endpoint`,
  which brings its own.

Nothing here integrates a competitor.

---

## How it compares

Surveyed 2026-09-27:

| | reviews | book detail | listings | notes |
|---|---|---|---|---|
| **this repo** | every review, by cursor, with rating / language / text filters and three orderings | 44 columns, rating distribution, per-language review counts | lists, authors, search, shelves, series | three engines, exit codes that tell blocked from empty from partial, run sidecar, daily canary |
| [maria-antoniak/goodreads-scraper](https://github.com/maria-antoniak/goodreads-scraper) | capped at 300 | yes | shelves | its README says it no longer works since the redesign |
| [havanagrawal/GoodreadsScraper](https://github.com/havanagrawal/GoodreadsScraper) | no | yes | lists, authors | Scrapy; last push 2024 |
| [Apify epctex/goodreads-scraper](https://apify.com/epctex/goodreads-scraper) | optional | yes | search, list, genre, author, shelf | $15/month rental plus compute |
| Apify pay-per-result actors | one walks the GraphQL cursor | yes | varies | $0.001-0.003 a row |
| [Bright Data](https://brightdata.com/products/web-scraper/goodreads) | yes | yes | yes | $1.50 per 1,000 records pay-as-you-go |

---

## Exit codes

| | |
|---|---|
| 0 | rows written |
| 1 | crash |
| 2 | bad usage, including a `--review-language` the book has no reviews in |
| 3 | blocked: AWS WAF's challenge did not clear, or a 403; distinct from an empty listing |
| 4 | zero rows: the listing has nothing in it, or the book id names no book |
| 5 | the data never arrived: a timeout, a dead proxy, a refused API parameter, a remote API error |
| 6 | partial: some pages (or, from a listing, some books) came back and some did not |

**A run that finds nothing writes nothing**, so a failure never replaces last
night's good output with `[]`. `--allow-empty` is the opt-out.

`diff_runs.py --old a.json --new b.json` compares two runs of the same mode
and query by `sku`: new and vanished books or reviews, and every tracked
column that changed. It refuses two review runs under different orderings,
because the ordering decides which reviews a capped run holds.

---

## Configuration

Credentials live in `.env` next to the scripts, never on a command line.
Copy [`.env.example`](.env.example) and fill in what you use;
`python3 env_config.py` prints what was picked up **without printing
secrets**. Precedence: explicit flag → exported environment variable →
`.env` → default.

---

## Tests

```bash
python3 smoke_test.py          # offline, no network, no engine needed
python3 smoke_test.py -v       # every check as it passes
pytest                          # the same suite, one test
```

The fixtures are real pages and API responses, cut and scrubbed by
`make_fixtures.py`, which proves each one parses identically to its
original: the page's API key, CSRF tokens and reviewers' names and words are
placeholders. The suite also drives the shared fetch loop end to end with a
fake browser: a listing that ends by serving another page, an author list
past its end, a 403, a WAF challenge that clears and one that does not, a
key refused and re-read, a refused parameter, a language the book has no
reviews in, a throttle, and a book run from a listing with one book refused.

The [canary](.github/workflows/canary.yml) runs a real scrape of each mode
daily **with no secrets**, which is what keeps "no account needed" honest.

---

## Legal

This reads **public data**: listings, book pages and community reviews, as
the site shows them to an anonymous visitor and as its own front end fetches
them. It rates, shelves and reviews nothing, and reads nothing behind a
login. Reviews are people's writing under their names: what you do with
them is between you, them and the site's terms.

Rate limits, terms of service and the legality of scraping in your
jurisdiction are your responsibility as the operator. `--delay` defaults to
1 second between pages.

MIT licensed. Not affiliated with or endorsed by Goodreads or Amazon.
