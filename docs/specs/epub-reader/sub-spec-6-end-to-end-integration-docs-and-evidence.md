---
type: phase-spec
master_spec: "docs/specs/2026-08-23-epub-reader.md"
sub_spec_id: SS-06
depends_on: ['SS-04', 'SS-05']
date: 2026-08-23
---

# Sub-spec 6: End-to-end integration, docs, and evidence

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

This is the integration sub-spec: it exercises every boundary the other five
created, per the master spec's `## Verification` section.

## Implementation Steps

1. **`tests/test_reader_e2e.py`** — one integration test using the synthetic
   EPUB + mock transport: capture the EPUB download's `Content-Type` and
   `Content-Disposition` BEFORE any reader use; walk home → book page → follow
   the `/read/` link → 303 → read three parts crossing a chapter boundary →
   `/read/{bid}/toc` → set `rs_split=small` via `/prefs` (site token) →
   resume lands on the part containing the stored block → home shows the book
   with rising percent → read to the final part → book page shows finished →
   re-fetch the EPUB download and assert both headers byte-identical to the
   captured values.
2. **README.md** — add a "Read in the browser" section after the existing
   feature docs: what it does, EPUB-only, `rs_split` sizes, book/phosphor
   themes, caps (80MB/500 chapters/1GB cache), friendly failure → iBooks path.
3. **Evidence** — write `docs/notes/ss06-reader-evidence.md` with the final
   `pytest -q`, `mypy app`, `ruff check app tests` outputs (last lines).
4. Commit: `reader: e2e integration test + README + evidence (SS-06)`.
   The worker MUST commit the artifacts before reporting complete.

## Interface Contracts

Consumes every contract (Sub-specs 1-5). Provides the regression guarantee.

## Verification Commands

- `.venv/bin/python -m pytest -q tests/test_reader_e2e.py`
- `.venv/bin/python -m pytest -q` · `.venv/bin/python -m mypy app` · `.venv/bin/python -m ruff check app tests`

## Checks

| Criterion | Type | Command |
|-----------|------|---------|
| README documents the reader | [STRUCTURAL] | `grep -q "Read in the browser" README.md || (echo "FAIL: README section missing" && exit 1)` |
| evidence file exists | [STRUCTURAL] | `test -f docs/notes/ss06-reader-evidence.md || (echo "FAIL: evidence missing" && exit 1)` |
| e2e + suite + types + lint pass | [MECHANICAL] | `.venv/bin/python -m pytest -q && .venv/bin/python -m mypy app && .venv/bin/python -m ruff check app tests || (echo "FAIL: SS-06 gates" && exit 1)` |
