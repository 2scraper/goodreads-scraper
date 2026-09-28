# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/) as closely as a CLI
toolkit can: a patch release means **fixes**, not that every flag and
default is frozen. A default that changes behaviour for an existing user is
said so at the top of its release notes.

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
