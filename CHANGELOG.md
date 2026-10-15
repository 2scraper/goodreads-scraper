# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/) as closely as a CLI
toolkit can: a patch release means **fixes**, not that every flag and
default is frozen. A default that changes behaviour for an existing user is
said so at the top of its release notes.

## [Unreleased]

> **Behaviour changes:**
>
> * **A page buys at most one paid CAPTCHA solve, however many times it is
>   fetched.** A page re-fetched after a refusal used to start a new
>   allowance: 2 solves per page with no proxy pool, and one per exit with a
>   pool (4 on a pool of four). With `TWOCAPTCHA_KEY` set, that was money.
> * **A served page that states rows and parses to none is now a parser
>   failure** (`stop_reason: parser_found_nothing`): exit 5 on page 1
>   (was 4, "the listing is empty") and exit 6 on a later page (was 0 and
>   `status: complete`).
> * **CSV cells that start with `=`, `+`, `-`, `@`, a tab or a line break
>   get a leading `'`**, so a spreadsheet does not run them. JSON is
>   unchanged; the sidecar's `csv_cells_escaped` counts the difference.
> * **New usage errors (exit 2):** `--retries 0`; `--pages`,
>   `--concurrency` and `--max-books` below 1; a negative `--delay`,
>   `--retry-delay` or `--proxy-block-retries`; a negative `--retries`,
>   `--pages` below 1 or a negative delay in `scraper_api_client.py`. If a
>   script of yours passes `--retries 0`, pass **1**: the flag counts
>   ATTEMPTS, so 1 means "try once and do not retry".

### Fixed

- **`--retries 0` fetched nothing and called it a result.** The load loop is
  `range(1, retries + 1)`, so 0 never runs it and the engine judged
  `about:blank`. Three third-party audits of this family ran exactly that and
  reported a working scraper as broken. `--retries` now goes through
  `page_flow.attempts` in every engine; a check reads the source of each
  one and calls the validator, so a new engine declaring the flag with a
  plain int fails the suite.
- **The paid-solve limit was per call, not per page.** `SOLVES_PER_PAGE = 1`
  was applied inside each navigation, and every re-fetch after a refusal (a
  fresh browser, or the next exit in a pool) navigated again. One
  allowance now lives for the whole page, is checked before each purchase
  and is not reset by a retry or a rotation; the language check that lands
  on the book before page 1 of a reviews run spends from page 1's. The
  suite's old assertion `solves <= 2` pinned the bug; it now asserts 1, with
  no pool, with a pool of four, and on a reviews landing.
- **A page whose own count says it holds rows, parsed to none, was reported
  as an empty listing or as the end of one.** It is now
  `parser_found_nothing`, the page is saved for an issue, and the canary
  names it.
- **Output writes are atomic.** JSON, CSV and the sidecar are written to a
  temporary file in the same directory and renamed over the target, so a
  crash or a full disk mid-write leaves the previous file whole (before: a
  539-byte `out.json` came back as 10 bytes). A new file gets the mode
  `open()` would give it under the current umask; an existing file keeps its
  own.
- **`--delay`, `--retry-delay`, `--pages`, `--concurrency`,
  `--proxy-block-retries` and `--max-books` are validated** in every engine,
  and `--pages`, `--delay`, `--retries` and `--retry-delay` in
  `scraper_api_client.py` (where `--retries` counts EXTRA attempts, so 0
  stays legal there).
- Comments, docstrings and one `--concurrency` error message pointed at
  notes that are not public; each rule is now stated where it is used. The
  captcha module's notes described another site; they describe this one.
- The expired-endpoint hint no longer says a Scraping Browser profile lasts
  "about a day": one was measured working after ten days, so the HTTP 401 is
  the signal, not an age.

## [0.1.1] — 2026-09-28

### Measured

- The Scraping Browser API (`--cdp-endpoint`) run live through a
  `country-us` profile: Playwright and pyppeteer, all three modes and a
  search, 8 of 8 runs complete with identical rows. It serves `/search`,
  which refuses a local headless browser. 0.1.0's README said this path had
  not been run; it now has.

### Changed

- The offline suite's Scraping Browser fixture is now a goodreads.com book
  page fetched over `--cdp-endpoint` (16 scripts injected by the auto-solve
  extension, `amazon_waf` among them) rather than a sibling repo's capture.
- The check that the AWS WAF markers stay silent on that page now counts the
  markers as raw substrings too. The previous form passed on the page's own
  asset references alone, whatever the markers were: a planted `amazon_waf`
  marker left it green.

## [0.1.0] — 2026-09-27

First release. Three modes over goodreads.com's own pages and its front
end's GraphQL API, three browser engines over one shared fetch loop, and the
2Captcha Scraper API for the two modes it can reach.

### Added

- `--mode list`: the books a listing shows, one row per book. Five listing
  kinds, each read from the structure it really has: a Listopia list, an
  author's books and the old search table from their schema.org microdata;
  the new search page from its React payload; a genre shelf from its markup;
  a series from its React props (with the site's own "Book 0.5" labels).
- `--mode book`: a book's own page from its Apollo cache, cross-checked
  against its schema.org/Book block. 44 columns: every contributor with a
  role, the WORK's rating, count and 1-5 distribution, review counts per
  language, genres, series, awards, characters, places, the want-to-read and
  currently-reading counts, and the edition's own format, pages, dates,
  publisher, ISBN-10/13 and ASIN. From a LISTING url, every book on its
  pages; `--max-books` caps how many.
- `--mode reviews`: a book's community reviews through the site's GraphQL
  API, walked by its cursor as deep as `--pages` goes, with `--sort`
  (default, newest, oldest), `--rating`, `--review-language`,
  `--search-text` and `--page-size`. The API key is the one the book page
  hands every visitor, read fresh on every landing and never stored.
- A `<out>.meta.json` per run with the site's own count, the pages it will
  serve, whether it caps them, and how many rows it repeated across pages.
- `diff_runs.py`, the family's run-to-run diff, refusing two review runs
  under different orderings.
- `scraper_api_client.py`: `--mode list` and one `--mode book` url through
  the 2Captcha Scraper API, no local browser. `--mode reviews` is refused
  with the reason: its API answers POST only.

### Measured before it was written (2026-09-27, a datacentre address)

- AWS WAF challenges book and search pages intermittently (HTTP 202, empty
  body). Real Chromium passes the challenge by itself in 1.0-2.4 s, so the
  engines wait for it instead of reporting a block, and nothing is paid.
- `/search` refuses headless Chromium (403, 3 of 3) and serves headful
  Chromium (3 of 3); the engines' advice says so.
- A listing answers a page past its end with another page, HTTP 200 (a list
  with its last, a search with "Page 100 of 100", a shelf with its first),
  so every page is checked against the page its url asked for.
- An author's list repeats books across pages and never serves as many
  others; two orderings together serve the whole list.
- The review API returns nothing, not an error, for a language the book has
  no reviews in and for a page size above 100; both are refused before a
  request is sent.
