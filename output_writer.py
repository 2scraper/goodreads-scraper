"""
output_writer.py
-----------------
Row models + JSON/CSV writers shared by all three engines.

Three modes, three row classes
------------------------------
    --mode list      ListedBook   one book as a LISTING shows it: a list,
                                  an author's books, a search, a shelf,
                                  a series
    --mode book      Book         one book's own page: edition details,
                                  genres, series, rating distribution
    --mode reviews   Review       one community review of a book

A listing tile and a book page describe the same thing at two depths, and a
tile has perhaps a third of the columns, so they are two dataclasses rather
than one wide row that is two-thirds null on every listing line (CLAUDE.md
§9: a column that is null on every row of a mode should not exist in that
mode's file). A review is a different kind of thing altogether.

What they DO share, byte-identical and in order, is the family prefix:
`source`, `scraped_at`, `url`, `sku`, `title`. One column name then works
across the whole family. The run-describing tail — `page`, `position`,
`mode`, `data_source` — is shared too.

Nothing on Goodreads is sold, so there is no `price`/`currency`/`brand`
triple. Buy links carry an e-book price on some books; that is Amazon's
price, not this site's, and it is not read.

Everything below the dataclasses is row-class-agnostic: pass `row_cls` so
an empty CSV still gets the right header for the mode that produced it.
"""

import csv
import json
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Optional, List, Set, Sequence, Any, Type


SOURCE_DEFAULT = "goodreads.com"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ListedBook:
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=_now)
    # The book's own page, without the tracking tail a search result adds
    # (`?from_search=true&qid=...&rank=N`), so one book has one url.
    url: str = ""
    # The book's numeric id: the `5907` in /book/show/5907.The_Hobbit. It is
    # an EDITION id. Ratings and reviews belong to the WORK (all editions
    # together), which is `work_id`.
    sku: Optional[str] = None
    # As the listing prints it, series suffix included:
    # "The Hunger Games (The Hunger Games, #1)".
    title: Optional[str] = None
    author: Optional[str] = None
    author_id: Optional[str] = None
    author_url: Optional[str] = None
    # Average of the WORK's ratings, 0-5, and how many there are. A book
    # nobody has rated is null on both, never 0.00 (§21).
    avg_rating: Optional[float] = None
    ratings_count: Optional[int] = None
    # The ORIGINAL publication year, as the listing prints it
    # ("published 1937"), not this edition's.
    published_year: Optional[int] = None
    editions_count: Optional[int] = None
    work_id: Optional[str] = None
    cover_url: Optional[str] = None
    # A Listopia list's own ranking figures. `list_score` is the site's
    # weighted score, `list_votes` how many people voted the book onto the
    # list. Null on every other listing kind.
    list_score: Optional[int] = None
    list_votes: Optional[int] = None
    # How many readers put the book on this shelf. Shelf listings only.
    shelved_count: Optional[int] = None
    # A series page's own label for the book's place: "Book 1", "Book 0.5",
    # "Book 2-4". A string because the site's labels are not numbers.
    series_position: Optional[str] = None
    # Which kind of listing the row came from: list, author, search, shelf,
    # series. Recorded because the columns above that are filled differ by
    # kind, and a consumer merging runs should know which it is reading.
    listing_kind: Optional[str] = None

    page: Optional[int] = None
    position: Optional[int] = None
    mode: Optional[str] = None
    # Which of the site's structures the row was read from: "microdata"
    # (schema.org/Book on the page), "rsc" (the search page's own React
    # payload), "shelf-html", "series-props". Provenance in a column (§8).
    data_source: Optional[str] = None


@dataclass
class Book:
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=_now)
    url: str = ""
    # The edition id, as ListedBook.sku.
    sku: Optional[str] = None
    title: Optional[str] = None
    # The primary contributor, as the page names it.
    author: Optional[str] = None
    author_id: Optional[str] = None
    author_url: Optional[str] = None
    # Every contributor, primary first, as "Name (Role)":
    # "Douglas A. Anderson (Editor)". A LIST.
    contributors: Optional[List[str]] = None

    # ---- the WORK: every edition together --------------------------------
    work_id: Optional[str] = None
    avg_rating: Optional[float] = None
    ratings_count: Optional[int] = None
    # Text reviews, as distinct from star ratings: 95,336 against 4,638,530
    # on The Hobbit (2026-09-27).
    reviews_count: Optional[int] = None
    # How many of the ratings gave 1..5 stars. Five columns rather than a
    # list, so a spreadsheet can sort on them.
    rating_1: Optional[int] = None
    rating_2: Optional[int] = None
    rating_3: Optional[int] = None
    rating_4: Optional[int] = None
    rating_5: Optional[int] = None
    # Text reviews per language, as "en:79996" — the site's own breakdown,
    # and the list --review-language is checked against. A LIST.
    review_languages: Optional[List[str]] = None
    # The work's first publication, ISO date. May be before 1970.
    first_published: Optional[str] = None
    original_title: Optional[str] = None
    genres: Optional[List[str]] = None
    series: Optional[str] = None
    series_url: Optional[str] = None
    # The site's `userPosition` string: "1", "0.5", "1-3".
    series_position: Optional[str] = None
    # "Name (year)" for each award the work won or was nominated for, as
    # the site lists them. A LIST.
    awards: Optional[List[str]] = None
    characters: Optional[List[str]] = None
    places: Optional[List[str]] = None
    # Readers with the book on their currently-reading / want-to-read shelf.
    currently_reading: Optional[int] = None
    want_to_read: Optional[int] = None

    # ---- the EDITION this page is -----------------------------------------
    format: Optional[str] = None
    pages: Optional[int] = None
    # This edition's publication, ISO date.
    published_at: Optional[str] = None
    publisher: Optional[str] = None
    language: Optional[str] = None
    isbn: Optional[str] = None
    isbn13: Optional[str] = None
    asin: Optional[str] = None
    # The blurb as plain text, the site's own stripped rendering.
    description: Optional[str] = None
    cover_url: Optional[str] = None

    page: Optional[int] = None
    position: Optional[int] = None
    mode: Optional[str] = None
    # "apollo+jsonld": the page's own Apollo cache, cross-checked against
    # its schema.org/Book block. "jsonld" when only the block was usable.
    data_source: Optional[str] = None


@dataclass
class Review:
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=_now)
    # The review's own page, /review/show/{id}.
    url: str = ""
    # The review's numeric id, from that url. Unique per review, which
    # `book_id` is not: many rows share one book (§9).
    sku: Optional[str] = None
    # The BOOK's title, so the family's `title` column says what the row is
    # about in every repo.
    title: Optional[str] = None
    book_id: Optional[str] = None
    work_id: Optional[str] = None
    # 1-5 stars. Null when the reviewer wrote a review and gave no stars:
    # the site sends 0 for that (2 of 100 measured), and 0 is not a grade.
    rating: Optional[int] = None
    # The review as plain text, paragraphs kept as blank lines.
    text: Optional[str] = None
    # Whether the reviewer marked it as containing spoilers.
    spoiler: Optional[bool] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    likes: Optional[int] = None
    comments: Optional[int] = None
    # The shelf the reviewer filed the book on ("read", "did-not-finish",
    # or one of their own) and the tags they gave it. `shelf` is the
    # reviewer's own word, in their own language.
    shelf: Optional[str] = None
    tags: Optional[List[str]] = None
    reviewer: Optional[str] = None
    reviewer_id: Optional[str] = None
    reviewer_url: Optional[str] = None
    reviewer_is_author: Optional[bool] = None
    reviewer_followers: Optional[int] = None
    reviewer_reviews: Optional[int] = None
    # The ordering the run asked for (default, newest, oldest). It decides
    # WHICH reviews a capped run holds, so it is part of what the file
    # means and diff_runs.py refuses to compare two different ones (§21).
    sort: Optional[str] = None

    page: Optional[int] = None
    position: Optional[int] = None
    mode: Optional[str] = None
    # "graphql": the site's own front-end API.
    data_source: Optional[str] = None


# Row classes by --mode, so an engine maps its mode to a schema in one place.
ROW_CLASS_BY_MODE = {"list": ListedBook, "book": Book, "reviews": Review}

# Modes whose rows are one-per-sku, and therefore safe to dedupe on `sku`
# and to hand to diff_runs.py. All three qualify: a review's sku is the
# review's own id.
UNIQUE_BY_SKU_MODES = ("list", "book", "reviews")


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages.

    On this site the drop count is NOT always zero, and that is a property
    of the data rather than a fault. Listopia lists and review feeds are
    LIVE: votes and new reviews reorder them while a run walks them. A
    listing that gains an entry at the top
    between page 1 and page 2 pushes one row from page 1 onto page 2, where
    it is fetched a second time. The duplicate is dropped here. The mirror
    case, an entry REMOVED above the cut, pushes one row from page 2 onto
    page 1 after page 1 was fetched, and no scraper can see that row. The
    README says so.

    A row with no key is always kept: there is nothing to check a duplicate
    against, and dropping it would be a silent data loss.
    """
    fresh = []
    for r in rows:
        val = getattr(r, key, None)
        if val is None or val not in seen:
            if val is not None:
                seen.add(val)
            fresh.append(r)
    return fresh


# Kept under its old name: the engines and smoke tests in this family all
# call it.
def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")


# CSV cannot hold a list. Joining with " | " keeps the cell readable in a
# spreadsheet and round-trippable by splitting on the same separator; the
# JSON output keeps the real list, so nothing is lost for a consumer that
# wants structure. `repr()` of a Python list (the default if this is not
# handled) is neither readable nor parseable by anything but Python.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    return v


def write_json(rows: Sequence[Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = ListedBook) -> None:
    # An empty result still gets the header row. A zero-byte file makes a
    # consumer fail on read (no columns to parse) instead of reading a valid
    # table with zero rows — and "an empty result is still a well-formed
    # result" is the same principle as `save` refusing to overwrite good data.
    #
    # The header comes from `row_cls`, not from the first row, so an empty
    # run still writes the columns of the mode that produced it.
    fieldnames = [f.name for f in fields(row_cls)]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: _csv_value(v) for k, v in asdict(r).items()})


# Exit code used when a run completes but produced nothing. Distinct from 1
# (crash) so a caller can tell "ran, found nothing" from "blew up".
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked before any data arrived: AWS WAF's CAPTCHA or
# challenge, a 403, or a 451 refusing the exit's country. Distinct from
# EXIT_NO_PRODUCTS so a caller can tell "the listing genuinely has nothing in
# it" from "something stood between us and the listing".
#
# An empty listing is NOT this code. A search with no results answers
# HTTP 200 with the site's own "No results." and that is EXIT_NO_PRODUCTS:
# the request was served exactly as asked.
EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME rows and then stopped early — a
# page-load timeout, a 503 throttle, or a challenge on page 3 of 10. The
# output file is still written (throwing away three good pages would be
# worse), but it is not a complete picture, and a consumer that cannot tell
# the difference will read the pages that were never fetched as products that
# disappeared from the catalogue. See write_run_meta.
# A REMOTE service failed — the Scraping Browser refusing the connection
# (`profile_locked` is the common one: a profile allows a single live
# connection), or the Scraper API answering an error. Distinct from 1 (a
# crash in this code) and from 2 (bad usage) because it means "try again, or
# use a different profile", not "there is a bug here". Defined once, here,
# because the browser engines and scraper_api_client.py both return it and
# two definitions of the same code is exactly how a family's exit contract
# drifts.
EXIT_API_ERROR = 5

EXIT_PARTIAL = 6


# Exit code for a run that never GOT its pages: a navigation timeout, a dead
# or unauthenticated proxy, a DNS failure, or an edge answering with
# something that is not the page that was asked for.
#
# Distinct from EXIT_NO_PRODUCTS because those are opposite facts. Exit 4 is
# a statement about the CATALOGUE — "we asked, and the answer was nothing" —
# so handing it to a run that never reached the site tells a pipeline the
# listing is empty when nothing was read at all.
#
# 5 rather than a new number, and 5 rather than EXIT_PARTIAL:
#
#   * this family's contract already reserves 5 for a transport failure
#     (scraper_api_client has used it for a remote API error since it was
#     written), so this needs no new code and no per-repo table for a caller
#     driving more than one of these scrapers;
#   * EXIT_PARTIAL (6) means "some rows were gathered and the output is
#     incomplete". A run holding nothing writes no output at all, so a
#     consumer that reads the file on a 6 finds either nothing or the
#     PREVIOUS run's good data, which `save` deliberately does not
#     overwrite. Exit 5 promises no file.
#
# Deliberately NOT applied when rows WERE gathered: a timeout on page 7 of
# 10 is a partial run (exit 6, output written), which is already right. This
# decides only what a run holding nothing reports.
EXIT_FETCH_FAILED = 5


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path.

    Deliberately a separate `<out>.meta.json` rather than columns on every
    row: this describes the RUN, not the product, and repeating it across
    every row would both bloat the output and change the schema every
    consumer of this project already parses.

    diff_runs.py reads it to refuse a comparison between runs that are not
    both complete, and between runs of different `mode`.
    """
    path = f"{out_prefix}.meta.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "listing", source: str = SOURCE_DEFAULT,
             extra: Optional[dict] = None) -> dict:
    """Build the metadata dict for a finished run.

    `status` is the field a consumer branches on:
      complete — every requested page was fetched, or the site's own
                 pagination genuinely ran out (nothing more existed to get)
      partial  — rows were gathered, then the run stopped early
      failed   — nothing was gathered at all

    `mode` and `source` are recorded because `mode` is not implied by the
    repo: one output prefix can hold a listing run, a book run or a
    reviews run, and those have different row classes. diff_runs.py
    refuses a pair whose modes or sources differ.

    `extra` carries facts about the run that are not about any single row:
    the query that was sent (the listing, the review filters and ordering)
    and the site's OWN count of what matched (`total_results`,
    `pages_available`, `capped_by_site`). The count is the only honest way
    to say how much of a listing a run holds. A 3-page reviews run of The
    Hobbit is complete as a REQUEST and a 90-of-95,000 sample as a LISTING,
    and only the sidecar can say so.

    `pages_failed` lists the pages that did not yield data, by number.
    `pages_completed` alone was enough only while pages were fetched strictly
    in order, where "3 of 10 completed" could only mean 1-2-3: a count is not
    a description once pages can be fetched independently and page 3 can fail
    while 4 and 5 succeed. Recording the numbers keeps the sidecar honest
    about WHICH part of the catalogue is missing, not just how much.
    """
    meta = {
        "source": source,
        "mode": mode,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        # Named "products" even though these are books or reviews, and kept that way deliberately: every repo in this family
        # writes this key, and a consumer reading several of them reads one
        # sidecar shape. The row TYPE is `mode`, right beside it.
        "products": products,
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        # Merged rather than nested under a key, so a consumer reads
        # `shop_rating` at the top level beside `products`. Run fields win a
        # name collision: a caller cannot accidentally overwrite `status`.
        meta.update({k: v for k, v in extra.items() if k not in meta})
    return meta


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = ListedBook) -> int:
    """Write JSON/CSV and return a process exit code.

    Returns 0 when rows were written, EXIT_NO_PRODUCTS when there were none.
    Callers are expected to exit with it.

    On zero rows, nothing is written at all unless `allow_empty`. Two reasons,
    and a live run demonstrated both. A page-load timeout produced
    `Saved 0 rows -> out.json` and exit 0: a two-byte `[]` that a
    consuming pipeline reads as a successful run with no stock. Worse, if the
    file already held a good result from an earlier run, that result is now
    gone — the failure destroyed the last known good data. So an empty result
    leaves the previous file intact and says why.

    `allow_empty=True` is for the legitimate case: a filter that genuinely
    matches nothing, where an empty file is the answer.
    """
    if not rows and not allow_empty:
        print(f"[!] 0 rows — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected answer.")
        return EXIT_NO_PRODUCTS

    if fmt in ("json", "both"):
        write_json(rows, f"{out_prefix}.json")
        print(f"[+] Saved {len(rows)} rows -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        print(f"[+] Saved {len(rows)} rows -> {out_prefix}.csv")
    return 0 if rows else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see. Anything
# else ended the page loop early, so the result is only a partial view.
#
# "no_new_products" belongs here and "pagination_exhausted" is kept for the
# engines that still stop on a missing next-link: the first is a property of
# the DATA (a page contributed nothing not already seen, so the listing is
# over), while the second is a property of a CSS SELECTOR and is therefore
# the weaker signal — a renamed attribute looks identical to a short
# catalogue.
#
# On this site there is a third and stronger signal, the site's own
# arithmetic. Every listing states its total on page 1, so the number of
# pages is PLANNED rather than discovered, and a run that fetched them all
# ends "completed". "end_of_listing" is the data-side stop: a page came back
# empty, meaning the live listing shrank below the plan during the run.
# That is complete too, because there was nothing more to get.
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted", "no_new_products",
                         "end_of_listing")


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "listing", source: str = SOURCE_DEFAULT,
               extra: Optional[dict] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them.

    The metadata sidecar is written ONLY when the row file was written.
    Otherwise a failed run would leave a "status": "failed" sidecar next to
    the previous run's still-intact good output (which `save` deliberately
    does not overwrite) — the two files would contradict each other, and
    diff_runs.py would refuse to compare data that is in fact fine.
    """
    # Completeness is decided by the reason AND by the evidence. A named
    # list of stop reasons cannot cover a failure recorded somewhere else,
    # and `pages_failed` is somewhere else: a run whose loop ended for a
    # COMPLETE reason while individual pages failed reported exit 0 and
    # `status: complete` with a non-empty `pages_failed` in the same
    # sidecar — a file that contradicts itself, and a pipeline branching
    # on `status` reading a short run as a whole one.
    #
    # Found by a third-party audit of a sibling repo and measured across
    # the family by CALLING each `finish_run` rather than grepping for the
    # fix: 28 of 32 repos behaved this way. Same shape as the exit-code
    # unification this file already carries — a rule keyed on a list of
    # names has a hole for every name nobody added to it.
    complete = stop_reason in COMPLETE_STOP_REASONS and not pages_failed
    row_cls = ROW_CLASS_BY_MODE.get(mode, ListedBook)
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls)
    wrote_output = bool(rows) or allow_empty

    if wrote_output:
        status = "complete" if (rows and complete) else (
            "partial" if rows else "failed")
        write_run_meta(out_prefix, run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            pages_failed=pages_failed, mode=mode, source=source,
            start_url=start_url, final_url=final_url, products=len(rows),
            extra=extra))

    if not rows:
        # Nothing gathered at all, and WHY decides the code. The three
        # outcomes are different facts and a pipeline branches on them
        # (blocked is not empty is not "never reached"):
        #
        #   blocked            something stood between the run and the content
        #   did not complete   we never got the pages — a dead proxy, a load
        #                      timeout, an edge serving something else
        #   completed          we asked, and the answer was nothing
        #
        # Keyed on `not complete` rather than on a list of stop reasons, on
        # purpose: a list cannot cover a reason nobody has added to it yet,
        # so a new one falls silently through to "the catalogue is empty" —
        # which is the defect this branch exists to prevent.
        if blocked:
            return EXIT_BLOCKED
        if not complete:
            print(f"[!] Nothing was gathered and the run did not finish "
                  f"({stop_reason}) — exit {EXIT_FETCH_FAILED}, NOT an empty "
                  f"result (exit {EXIT_NO_PRODUCTS}). Nothing can be "
                  f"concluded about the catalogue from this run.")
            return EXIT_FETCH_FAILED
        return rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
