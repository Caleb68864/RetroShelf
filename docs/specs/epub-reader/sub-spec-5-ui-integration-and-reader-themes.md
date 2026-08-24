---
type: phase-spec
master_spec: "docs/specs/2026-08-23-epub-reader.md"
sub_spec_id: SS-05
depends_on: ['SS-04']
date: 2026-08-23
---

# Sub-spec 5: UI integration and reader themes

## Shared Context

Master spec: `../2026-08-23-epub-reader.md` · Design: `../../plans/2026-08-23-epub-reader-design.md`.
RetroShelf is a no-JS, server-rendered FastAPI + Jinja2 bridge for iOS 5.1.1-12
Safari. Hard rules: no JavaScript, no CSS Grid/unprefixed flex, plain `<a href>`
links only; sanitizer output is the ONLY `| safe` seam; every upstream fetch goes
through `KavitaClient.resolve_url`; no upstream URL on disk; mypy zero, ruff
clean, Sphinx docstrings; never touch existing download/header behavior.
Commands: test `.venv/bin/python -m pytest -q`, types `.venv/bin/python -m mypy app`,
lint `.venv/bin/python -m ruff check app tests`.
Patterns: capped reads (`app/kavita.py::KavitaClient._read_capped`), atomic
writes (`app/download.py` cover cache, `app/store.py::Store._save`), errors via
`RetroShelfError` subclass + `app/main.py::_ERROR_TABLE` row, record projection
(`app/store.py::sanitize_record`), ids via `app/main.py::_record_id`, image
types via `app/download.py::_safe_image_type`.

## Implementation Steps (TDD)

1. **Failing tests** — extend `tests/test_reader_routes.py` (or a new class):
   EPUB book page contains `/read/` link + "(first open takes a moment)" hint;
   PDF book page has none; after one part view the book page shows "Continue
   reading (Ch." with percent and home shows the book under "Currently Reading";
   finished book shows "finished"; `rs_reader_theme=phosphor` puts the phosphor
   class on the read page body, default gets the book class; `rs_big=1` applies.
2. **`app/main.py`** — book route: for EPUB records add `read_url`,
   `read_label` ("Read here" / "Continue reading (Ch. N · P%)" / "Read again —
   finished") + hint flag from `store.get_position`; home route: `reading` list
   from `store.reading_list(4)` with `_record_id`-minted links + percent/finished;
   read route passes theme class from `rs_reader_theme` cookie.
3. **Templates** — `book.html`: reader button under the iBooks button (plain
   `<a class="button">`, hint span when unshelved); `home.html`: "Currently
   Reading" sectionhead + list (reuse `_macros.html` book_item pattern or a
   compact row).
4. **`app/static/app.css`** — `.readpage` block styles: book theme (sepia
   `#f4ecd8`/`#33291f`, `Georgia, 'Times New Roman', serif`, `line-height:1.6`,
   `max-width:40em`, big-class font bump) + `body.reader-phosphor` overrides
   reusing the terminal palette. Block layout only — no grid, no flex, no
   web fonts.
5. Green, then full suite + mypy + ruff.
6. Commit: `reader: book/home UI integration + book/phosphor themes (SS-05)`.

## Interface Contracts

Implements contracts from Sub-spec 3 (Store positions) and Sub-spec 4 (routes).
Provides no new cross-sub-spec symbols.

## Verification Commands

- `.venv/bin/python -m pytest -q tests/test_reader_routes.py`
- `.venv/bin/python -m pytest -q` · `.venv/bin/python -m mypy app` · `.venv/bin/python -m ruff check app tests`

## Checks

| Criterion | Type | Command |
|-----------|------|---------|
| no grid/flex in reader CSS | [STRUCTURAL] | `! grep -E "display:\s*(grid|flex)" app/static/app.css || (echo "FAIL: grid/flex found" && exit 1)` |
| zero script tags in touched templates | [STRUCTURAL] | `! grep -l "<script" app/templates/read.html app/templates/toc.html app/templates/book.html app/templates/home.html 2>/dev/null || (echo "FAIL: script tag found" && exit 1)` |
| full suite + types + lint pass | [MECHANICAL] | `.venv/bin/python -m pytest -q && .venv/bin/python -m mypy app && .venv/bin/python -m ruff check app tests || (echo "FAIL: SS-05 gates" && exit 1)` |
