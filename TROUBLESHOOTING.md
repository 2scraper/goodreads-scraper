# Troubleshooting

Find your exit code first (`echo $?` straight after the run), then the
sidecar's `stop_reason` in `<out>.meta.json` if one was written.

## Exit 2 — bad usage

* **"--review-language X: this book has no reviews in that language"** — the
  API would have answered with an empty result, so the run was stopped
  before it started. The message lists the languages the book does have.
* **"--mode list needs a listing url"** — a book url is read with `--mode
  book` (its page) or `--mode reviews` (its reviews).
* **"--concurrency cannot apply to --mode reviews"** — each page of reviews
  starts where the previous one's cursor says, so they cannot be fetched in
  parallel.
* **"... is a goodreads.com page this repo does not read"** — an author's
  PROFILE (`/author/show/{id}`) is not a listing; their books are at
  `/author/list/{id}`.

## Exit 3 — blocked

The log names what refused the run:

* **`aws-waf`** — AWS WAF challenged the session and it did not clear. Its
  usual CHALLENGE action is a script a browser passes by itself in about two
  seconds, and the engines wait for it; one that stays challenged was
  probably shown the WAF's CAPTCHA. A residential `--proxy`, or
  `TWOCAPTCHA_KEY` in `.env` so the CAPTCHA can be solved (AmazonTask), are
  the two answers.
* **`http-403`** — the site refused the request. On `/search` from a
  HEADLESS browser this is expected: run it with `--headful`
  (`xvfb-run -a python3 playwright_scraper.py ... --headful` on a server).
  Anywhere else, use a different exit.

A `<out>_page<N>_debug.html` beside the output holds what came back.

## Exit 4 — zero rows

The listing genuinely has nothing in it: a search with no results, an
author list asked for a page past its end, a book id that names no book.
Nothing is written, so an earlier good file is left alone; `--allow-empty`
writes the empty file.

## Exit 5 — the data never arrived

* **"The API refused this request (...)"** — the GraphQL API rejected the
  parameters. The same request would be rejected again, so it is not
  retried. The flags are checked before anything is sent, so this means the
  API changed: open a "Site changed" issue with `--dump-html` output.
* **"The book page carries no API key or work id"** — the page shape moved;
  same issue template.
* **"Gave up on page N"** — a timeout or a dead proxy. The log names which.

## Exit 6 — partial

Some pages came back and a later one did not, or in `--mode book` from a
listing, some books did and others did not. The output holds what was
gathered, and the sidecar lists `pages_failed` (and `books_failed`).

## A run looks fine but a column is wrong

Re-run with `--dump-html page.html`: it writes the exact document the parser
was given, on success too, so a parsing bug can be told apart from a change
in what the site sends.

## Fewer books than the listing states

Three different things, each recorded in the sidecar:

* **The site caps what it serves.** A Listopia list states ~80,000 books and
  serves 100 pages of 100; a search serves 100 pages of 20; a shelf serves
  50 books to an anonymous visitor. `capped_by_site` and `reachable_max`.
* **The site repeats books across pages**, and never serves the same number
  of others. Measured on an author's list: sorted by popularity, 71 of 791
  places repeated a book already shown, identically on two runs; sorted by
  title (`?sort=title` in the url), 52. The two orderings together served
  all 791. `repeated_across_pages`, and a warning in the log.
* **A list is live.** Votes reorder a Listopia list while a run walks it.
