---
type: phase-spec
master_spec: "docs/specs/2026-08-23-epub-reader.md"
sub_spec_id: SS-03
depends_on: ['SS-02']
date: 2026-08-23
---

# Sub-spec 3: Pagination, progress, and Store positions

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

1. **Failing tests** — `tests/test_reader_paging.py`: partition totality across
   all four split settings (every block in exactly one `(start, end)` range),
   oversized-block singleton, `part_containing` round-trip for every block,
   resume-across-size-change (block stored at medium resolves at small/whole),
   `percent_of` monotonic + 100 at final block, Store reading persistence across
   reload, 100-entry cap drops oldest, wrong-shaped `reading` data loads empty.
2. **`app/reader.py` additions** — `SPLIT_TARGETS = {"small": 6000, "medium": 12000,
   "large": 24000, "whole": None}`, `DEFAULT_SPLIT = "medium"`;
   `parts_for(block_lengths: list[int], target_chars: int | None) -> list[tuple[int, int]]`
   (greedy: accumulate until running total >= target, never split a block; `None`
   → single part; empty input → `[(0, 0)]`-free empty list);
   `part_containing(block_index: int, parts: list[tuple[int, int]]) -> int` (1-based,
   clamps out-of-range to nearest valid part);
   `percent_of(manifest: Manifest, chapter: int, block: int) -> int` (cumulative
   chars before position / total_chars; final block → 100).
3. **`app/store.py` additions** — `reading` key in `self._data`; tolerant load
   (mirror favorites/history shape-validation); `set_position(record, chapter,
   block, percent)` → `sanitize_record` projection + validated non-negative ints
   + `updated=time.time()`, keyed by `book_key(record["u"])`, cap 100 oldest-
   dropped; `get_position(book_key) -> dict | None`; `reading_list(limit=4)`
   most-recent-first. All lock-guarded like existing methods.
4. Green the file, then full suite + mypy + ruff.
5. Commit: `reader: pagination + progress + Store reading positions (SS-03)`.

## Interface Contracts

### parts_for / part_containing / percent_of / SPLIT_TARGETS
- Direction: Sub-spec 3 -> Sub-spec 4
- Owner: Sub-spec 3
- Shape: `parts_for(block_lengths: list[int], target_chars: int | None) -> list[tuple[int, int]]`; `part_containing(block_index: int, parts: list[tuple[int, int]]) -> int`; `percent_of(manifest: Manifest, chapter: int, block: int) -> int`; `SPLIT_TARGETS: dict[str, int | None]`

### Store reading positions
- Direction: Sub-spec 3 -> Sub-specs 4, 5
- Owner: Sub-spec 3
- Shape: `Store.set_position(record: dict, chapter: int, block: int, percent: int) -> None`; `Store.get_position(book_key: str) -> dict | None`; `Store.reading_list(limit: int = 4) -> list[dict]`

Implements contract from Sub-spec 2 (`Manifest`).

## Verification Commands

- `.venv/bin/python -m pytest -q tests/test_reader_paging.py`
- `.venv/bin/python -m pytest -q` · `.venv/bin/python -m mypy app` · `.venv/bin/python -m ruff check app tests`

## Checks

| Criterion | Type | Command |
|-----------|------|---------|
| wrong-shaped reading data tolerated | [STRUCTURAL] | `grep -q "reading" app/store.py || (echo "FAIL: Store has no reading section" && exit 1)` |
| paging tests + suite + types + lint pass | [MECHANICAL] | `.venv/bin/python -m pytest -q tests/test_reader_paging.py && .venv/bin/python -m mypy app && .venv/bin/python -m ruff check app tests || (echo "FAIL: SS-03 gates" && exit 1)` |
