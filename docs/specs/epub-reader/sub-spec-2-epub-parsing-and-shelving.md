---
type: phase-spec
master_spec: "docs/specs/2026-08-23-epub-reader.md"
sub_spec_id: SS-02
depends_on: ['SS-01']
date: 2026-08-23
---

# Sub-spec 2: EPUB parsing and shelving

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

**Red-team note (A-9):** this is the heavyweight sub-spec — follow the steps in
order and keep each green before moving on.

## Implementation Steps (TDD)

1. **Fixture builder first** — in `tests/test_reader_shelve.py` add
   `make_epub(chapters=..., image=..., ncx=..., encryption=False, zip_slip=False) -> bytes`
   using stdlib `zipfile` + `io.BytesIO`: `mimetype`, `META-INF/container.xml`,
   `OEBPS/content.opf` (manifest + spine), chapter XHTML files, optional image,
   optional `toc.ncx`, optional `META-INF/encryption.xml`, optional `../../evil.txt`
   member. Add a `FakeKC` whose `open_stream` yields the bytes in chunks
   (mirror `tests/test_download_headers.py::FakeUpstream`).
2. **Failing tests**: happy-path shelve (manifest fields, chapters/*.json,
   images/0 exist), zip-slip containment (walk cache dir, nothing outside),
   oversize / >500 spine / zero-chapters / encryption → `ReaderError` distinct
   messages + spool cleaned, malformed-chapter degradation, no-upstream-URL-on-disk
   (grep shelved tree for the fixture URL), idempotent re-shelve (second call
   returns manifest without refetch).
3. **`ReaderError`** — add to `app/errors.py` (subclass `RetroShelfError`,
   docstring; hierarchy note updated).
4. **Shelving in `app/reader.py`** — caps constants (`MAX_EPUB_BYTES=80MB`,
   `MAX_SPINE_ITEMS=500`, `MAX_CHAPTER_BYTES=2MB`, `MAX_UNPACKED_BYTES=120MB`,
   `MAX_READER_CACHE_BYTES=1GB`, `SHELVE_TIMEOUT=120.0`); dataclasses
   `ChapterMeta(title, blocks, chars)` and `Manifest(version, book_key, title,
   author, chapters, images, total_chars, created)`;
   `async shelve_book(kc, record, cache_dir) -> Manifest`:
   spool via `kc.open_stream(url)` with a 120s `httpx.Timeout` override
   (mirror `fetch_feed`) reading capped chunks to
   `{cache_dir}/reader/.spool-{pid}-{book_key}`; `zipfile.ZipFile` from
   the path; member-name guard (reject absolute / `..`); container→OPF→spine
   parse with defusedxml (namespace-tolerant); encryption.xml → ReaderError;
   per-chapter size cap + running unpacked total cap; titles nav-doc → NCX →
   first h1-h3 → "Chapter N"; sanitize via SS-01 with resolvers built from the
   OPF manifest (image href → index, spine href → chapter index); images
   extracted + Pillow-downscaled (max edge 1024, JPEG/PNG passthrough small,
   absent Pillow → text-only) into `images/{n}` + `.ct`; write
   `chapters/{i}.json` + `manifest.json` inside `{book_key}.tmp-{pid}`
   then `os.rename` to `{book_key}`; per-process `asyncio.Lock` per book_key;
   `finally:` remove spool. Zero readable chapters → ReaderError("no readable
   content"). One masked INFO log line (book_key, chapters, bytes, ms) via
   `logging.getLogger("retroshelf.reader")`.
   `load_manifest(cache_dir, book_key) -> Manifest | None`,
   `load_chapter(cache_dir, book_key, i) -> list[str]`,
   `prune_reader_cache(cache_dir, limit) -> None` (oldest manifest first,
   whole dirs, best-effort — mirror `_prune_cover_cache`).
5. Green the file, then full suite + mypy + ruff.
6. Commit: `reader: EPUB shelving pipeline (SS-02) — spool, caps, zip-slip guard, manifest`.

## Interface Contracts

### Manifest / ChapterMeta
- Direction: Sub-spec 2 -> Sub-specs 3, 4, 5
- Owner: Sub-spec 2
- Shape: `Manifest(version: int, book_key: str, title: str, author: str, chapters: list[ChapterMeta], images: int, total_chars: int, created: float)`; `ChapterMeta(title: str, blocks: int, chars: int)`

### shelve_book / load_manifest / load_chapter
- Direction: Sub-spec 2 -> Sub-spec 4
- Owner: Sub-spec 2
- Shape: `async shelve_book(kc: KavitaClient, record: dict, cache_dir: str) -> Manifest`; `load_manifest(cache_dir: str, book_key: str) -> Manifest | None`; `load_chapter(cache_dir: str, book_key: str, i: int) -> list[str]`

### ReaderError
- Direction: Sub-spec 2 -> Sub-spec 4
- Owner: Sub-spec 2
- Shape: `class ReaderError(RetroShelfError)` in `app/errors.py`

Implements contract from Sub-spec 1 (`sanitize_chapter`).

## Verification Commands

- `.venv/bin/python -m pytest -q tests/test_reader_shelve.py`
- `.venv/bin/python -m pytest -q` · `.venv/bin/python -m mypy app` · `.venv/bin/python -m ruff check app tests`

## Checks

| Criterion | Type | Command |
|-----------|------|---------|
| ReaderError defined in app/errors.py | [STRUCTURAL] | `grep -q "class ReaderError(RetroShelfError)" app/errors.py || (echo "FAIL: ReaderError missing" && exit 1)` |
| shelve timeout constant present | [STRUCTURAL] | `grep -q "SHELVE_TIMEOUT" app/reader.py || (echo "FAIL: no bounded shelve timeout" && exit 1)` |
| shelve tests + suite + types + lint pass | [MECHANICAL] | `.venv/bin/python -m pytest -q tests/test_reader_shelve.py && .venv/bin/python -m mypy app && .venv/bin/python -m ruff check app tests || (echo "FAIL: SS-02 gates" && exit 1)` |
