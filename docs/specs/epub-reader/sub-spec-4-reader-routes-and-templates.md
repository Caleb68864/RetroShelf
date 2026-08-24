---
type: phase-spec
master_spec: "docs/specs/2026-08-23-epub-reader.md"
sub_spec_id: SS-04
depends_on: ['SS-03']
date: 2026-08-23
---

# Sub-spec 4: Reader routes and templates

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

1. **Failing tests** — `tests/test_reader_routes.py` reusing the SS-02 fixture
   builder and the `make_client` harness style from `tests/test_app.py` (mock
   transport serving the synthetic EPUB at the book URL): first-open shelve +
   303 → part URL; part page has text + Prev/Next + zero `<script`/`on*=`/`style=`;
   second request → zero upstream calls (transport counter); split-size cookie
   changes part count; resume across sizes; PDF-record bid → friendly 404; DRM
   fixture → 502 ReaderError page; out-of-range chapter/part → 404; `/read/{bid}/img/0`
   → image/*, missing index → 404 GIF; with `BRIDGE_ACCESS_KEY` set, `/read/{bid}`
   with no key/cookie → 403.
2. **Templates** — `app/templates/read.html` (slim chrome: top bar links book
   title → `/read/{bid}/toc`; content div `class="readpage"`; the ONLY
   `| safe` usage, written exactly `{{ content_html | safe }}` with the
   trusted-seam comment above it; footer: Prev/Next anchors, "Ch {n} · part
   {p} of {q}", split links `/prefs?split=small|medium|large|whole&next=...&t=...`,
   theme toggle `/prefs?reader=book|phosphor...`, back-to-book link) and
   `app/templates/toc.html` (chapter list, current marker, start-over link;
   no `| safe`). Both extend `base.html`; no script, no inline style.
3. **Routes in `app/main.py`** (`_register_routes`, after the cover route):
   `GET /read/{bid}` (decode → dict record required, `format_of(m) == "epub"`
   else friendly 404; `load_manifest` → else `shelve_book`; 303 to resume part
   via `get_position` + `part_containing`, else `/read/{bid}/0/1`);
   `GET /read/{bid}/toc`; `GET /read/{bid}/{chapter}/{part}`
   (`chapter: int`, `part: int`; blocks → `parts_for` with cookie target →
   slice → placeholder substitution — compiled regex, substituted values
   restricted to `[A-Za-z0-9/._-]` — → `content_html`; record position;
   bounds → 404); `GET /read/{bid}/img/{n}` (`n: int`, `_safe_image_type`,
   `Cache-Control: private, max-age=86400`, missing → 404 GIF).
   `_ERROR_TABLE` row: `(ReaderError, 502, "Can't read this book",
   "This book can't be read in the browser — use Open in iBooks instead.")`
   placed before `KavitaError`. Extend `/prefs` with `split=` / `reader=`
   cookie params (site-token gated, same `remember` helper).
4. Green the file, then full suite + mypy + ruff.
5. Commit: `reader: /read routes, read/toc templates, prefs cookies (SS-04)`.

## Interface Contracts

### Reader routes
- Direction: Sub-spec 4 -> Sub-specs 5, 6
- Owner: Sub-spec 4
- Shape: `GET /read/{bid}` (303), `GET /read/{bid}/toc`, `GET /read/{bid}/{chapter:int}/{part:int}`, `GET /read/{bid}/img/{n:int}`; chapter 0-based, part 1-based

Implements contracts from Sub-spec 2 (`shelve_book`, `load_manifest`,
`load_chapter`, `ReaderError`) and Sub-spec 3 (`parts_for`, `part_containing`,
`percent_of`, `SPLIT_TARGETS`, Store positions).

## Verification Commands

- `.venv/bin/python -m pytest -q tests/test_reader_routes.py`
- `.venv/bin/python -m pytest -q` · `.venv/bin/python -m mypy app` · `.venv/bin/python -m ruff check app tests`

## Checks

| Criterion | Type | Command |
|-----------|------|---------|
| exactly one \| safe in read.html, none in toc.html | [STRUCTURAL] | `[ $(grep -c "\| safe" app/templates/read.html) -eq 1 ] && ! grep -q "\| safe" app/templates/toc.html || (echo "FAIL: safe-seam count wrong" && exit 1)` |
| substitution charset guard exists | [STRUCTURAL] | `grep -q "A-Za-z0-9/._-" app/main.py || (echo "FAIL: charset guard missing" && exit 1)` |
| ReaderError row in error table | [STRUCTURAL] | `grep -q "ReaderError" app/main.py || (echo "FAIL: no error-table row" && exit 1)` |
| route tests + suite + types + lint pass | [MECHANICAL] | `.venv/bin/python -m pytest -q tests/test_reader_routes.py && .venv/bin/python -m mypy app && .venv/bin/python -m ruff check app tests || (echo "FAIL: SS-04 gates" && exit 1)` |
