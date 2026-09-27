# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key, and takes
about a second:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

It prints its own check count, and lists any group it had to skip because an
engine library is absent.

**The suite must pass with no engine installed at all.** CI installs only
`beautifulsoup4` and `requests`, so any import of `playwright_scraper`,
`puppeteer_scraper` or `selenium_scraper` in a test has to sit inside
`try/except ImportError` with the skip recorded. This is easy to get wrong
locally, where you almost certainly have an engine installed and an unguarded
import passes.

If the suite fails on a clean clone, that is itself the bug — say so.

## Never commit a credential

`.env` is in `.gitignore`. Keep it there.

The scrapers mask `user:pass@` in their own log lines, but three things are **not**
masked: raw HTML dumps, the Scraper API's `x-debug` response header, and your
shell history. Before pasting any output into an issue or a PR, replace keys,
proxy passwords and full `ws://user:pass@host:9222` endpoints with `***`.

CI fails the build if something that looks like a credential is committed. That
check is a backstop, not a review — a leaked key has to be rotated whether or
not the check caught it.

## Reporting a site change

Goodreads changing a page is the normal way this stops working, and it has
its own issue template. There are four structures the parser reads, and
each breaks in its own way:

1. **Listing microdata** (`itemtype="http://schema.org/Book"` on a list, an
   author's books and the old search table). A standard rather than a
   build-generated class, so it moves rarely; when it does, a page with
   books on it classifies `unknown` and the run says so.
2. **The search page's React payload.** A browser is served a Next.js page
   whose books are only in its React Server Components stream. It is
   internal to the site and the likeliest thing here to change.
3. **A book page's Apollo cache** (`__NEXT_DATA__`). This is where the
   quiet failure lives: the row still writes, with a column null.
   `page_flow.CORE_FIELDS` is the guard, a coverage floor of 99% on the
   columns every captured record carried.
4. **The GraphQL API behind reviews.** A refused parameter is classified
   `rejected` and the run stops naming the API's own complaint. A key that
   stops working is `unauthorized`, and the run lands on the book page
   again for the current one. The allowlists at the top of
   `product_parser.py` exist because the API accepts several wrong values
   SILENTLY.

## Before this repository goes public

One item cannot be undone later, so it belongs on a checklist rather than in
someone's head. **A commit on top cannot reach what a published tag and a
merged PR's refs already hold** — those stay attached to the PR and cannot be
deleted from it. Afterwards, only a fresh repository removes anything.

```bash
python3 .github/ci_checks.py --history-check
```

That applies the same credential rules CI enforces to **every blob that has
ever existed**, not just the working tree. It is deliberately not part of
`--all` and not run by CI: it shells out to git once per object, and a dirty
history needs a decision, not a red check on every push.

Then the rest of the presentation, in the order that matters:

1. `python3 smoke_test.py` green, and the canary dispatched at least once.
   It runs daily with no secrets at all and is expected to be green,
   because no mode needs a credential and a green badge there is exactly
   the claim the README makes. If a run is refused for the runner's
   address, the canary's `GOODREADS_PROXY` secret (an exit elsewhere) is
   the fix, with no workflow edit.
2. The repo description, homepage and topics set (see the family notes on
   what those should say).
3. Only then the row in the org profile README — and check it with an
   ANONYMOUS request rather than your own logged-in browser. A row pointing
   at a private repo is a 404 for every visitor, which costs more trust than
   the missing row.

## Pull requests

**Add a test for the behaviour you are changing.** `smoke_test.py` is a single
file of plain functions. Its fixtures are real pages and API responses, cut and scrubbed, in
`fixtures_generated.json`, which `make_fixtures.py` regenerates from a
capture directory. Copy the nearest existing check and edit it.

Six properties in this repo exist because they were measured against
expectation and cost real time. Tests pin all six, so a PR that breaks one
fails rather than silently regressing:

- **A listing answers a page past its end with ANOTHER page, HTTP 200.** A
  list serves its last page, a search "Page 100 of 100", a shelf its first.
  Every page is asked which page it served, against the page its URL asked
  for (not the loop counter), and a mismatch ends the listing.
- **Every review parameter is allowlisted, because the API does not
  validate.** An unknown `languageCode` returns 0 reviews on a book with
  95,000; a page size of 200 returns ZERO edges. `--review-language` is
  checked against the languages the book's own page lists.
- **AWS WAF's challenge is waited for, not reported.** A browser passes it
  by itself in about two seconds. Judged at once, every run from a scored
  address would exit 3 on a page that was about to be served.
- **The API key is read off the book page on every landing**, never stored:
  an AppSync key expires, and a 401 lands again for the current one.
- **A rating of 0 is not a rating.** A review with no stars and an unrated
  book both come back as 0 and are written as null.
- **An author list repeats books across pages**, deterministically, and never
  serves the same number of others. The dedupe drops the repeats, the
  sidecar counts them, and the log says the rest were never served.

Plus the family's own invariants, which are not negotiable:

- **A run that finds nothing writes nothing.** It must not replace a good
  output file with `[]`. `--allow-empty` is the opt-out.
- **Exit codes are a contract**, not decoration: `0` ok, `1` crash, `2` bad
  usage, `3` blocked, `4` zero rows — including a query that genuinely
  matched nothing, which is a correct answer — `5` remote API error, `6`
  partial. A pipeline branches on these.
- **An EMPTY page is never retried and never counted as blocked.** A query
  that matched nothing was served exactly as asked.
- **Credentials never reach argv or a log, and an exception message is a
  log.** The masker is global rather than first-occurrence: a Playwright
  connection error repeats the endpoint five times.
- **Merge in page order, not arrival order**, so concurrency cannot change
  the output.

### If your change needs a live run

Most do not: the suite covers the parser, the writers, the classifier and
the CLI contract against real, trimmed responses. If yours genuinely needs
goodreads.com, say in the PR what you ran (engine, mode, url), from which
exit, and what you got, including the sidecar's `total_results`.

Two things about running this live that are specific to Goodreads:

* **No mode needs an exit, a key or an account.** Every mode ran from a
  datacentre VPS, AWS WAF's challenge included, which a browser passes by
  itself. So "it worked from my laptop" is reproducible here.
* **Run searches headful.** /search refuses a headless browser with 403.

**Run more than the primary engine.** "Mirror them exactly" is a design
rule, not a verification. The fetch loop is shared (`page_flow.run_pages`),
but each engine's driver plumbing is its own, and only running it proves it.

## Scope

This repo reads **public data** on goodreads.com: listings, book pages and
community reviews, exactly as the site shows them to an anonymous visitor
and as its own front end fetches them.

Out of scope: anything behind a login (a user's private shelves, their
feed), anything that rates, shelves, reviews or submits any other form, and
anything that defeats a protection rather than passing it the way an
ordinary browser does.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.
