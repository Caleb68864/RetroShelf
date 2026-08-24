---
type: phase-spec
master_spec: "docs/specs/2026-08-23-epub-reader.md"
sub_spec_id: SS-01
depends_on: []
date: 2026-08-23
---

# Sub-spec 1: Sanitizer and block splitter

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

1. **Failing tests first** — create `tests/test_reader_sanitize.py` with:
   `test_hostile_markup_stripped` (script/onclick/style/iframe/form/javascript:
   href/external img → none survive), `test_malformed_input_escapes` (non-XML in,
   escaped blocks out, no raw `<` from source), `test_structure_survives`
   (h1/p/ul/table/img→`{IMG:n}`/internal a→`{CH:i}`),
   `test_fragment_links_unwrapped` (`#note` → plain text),
   `test_colspan_validated` (non-integer colspan dropped). Import
   `from app.reader import sanitize_chapter`.
2. Run `.venv/bin/python -m pytest -q tests/test_reader_sanitize.py` — expect
   ImportError (module missing). That is the failing state.
3. **Create `app/reader.py`** — module docstring naming the trusted-seam
   contract; constants `_ALLOWED_ELEMENTS`, `_DROPPED_ELEMENTS`;
   `sanitize_chapter(source: str | bytes, *, resolve_image: Callable[[str], int | None],
   resolve_link: Callable[[str], int | None]) -> list[str]`.
   Parse with `defusedxml.ElementTree.fromstring` (strip XML namespaces to local
   names first — XHTML chapters are namespaced). Walk the body: rebuild
   allowlisted elements attribute-free; `img` src → `resolve_image(href)` →
   `{IMG:n}` + keep `alt`, unresolved → drop element; `a` href →
   `resolve_link(href)` → `{CH:i}`, fragment-only or unresolved → unwrap to
   text; td/th keep integer-validated colspan/rowspan. Dropped-with-children:
   script style iframe object embed form link meta svg video audio math.
   Non-allowlisted → unwrap. Top-level children of body become the block list;
   empty/whitespace-only blocks skipped. On parse failure: `xml.sax.saxutils.escape`
   the text, split on blank lines, wrap each in `<p>`.
4. Run the test file until green; then full suite + mypy + ruff.
5. Commit: `reader: sanitizer + block splitter (SS-01) — allowlist rebuild, escaped-text fallback`.

## Interface Contracts

### sanitize_chapter
- Direction: Sub-spec 1 -> Sub-spec 2
- Owner: Sub-spec 1
- Shape: `sanitize_chapter(source: str | bytes, *, resolve_image: Callable[[str], int | None], resolve_link: Callable[[str], int | None]) -> list[str]`
- Blocks contain only allowlisted markup; image srcs are `{IMG:n}`, chapter links `{CH:i}`.

## Verification Commands

- `.venv/bin/python -m pytest -q tests/test_reader_sanitize.py`
- `.venv/bin/python -m pytest -q` (full suite still green)
- `.venv/bin/python -m mypy app` · `.venv/bin/python -m ruff check app tests`

## Checks

Auto-generated from `[MECHANICAL]` and `[STRUCTURAL]` criteria. Each command exits 0 on pass.

| Criterion | Type | Command |
|-----------|------|---------|
| sanitize_chapter defined with committed signature | [STRUCTURAL] | `grep -q "def sanitize_chapter" app/reader.py || (echo "FAIL: sanitize_chapter missing" && exit 1)` |
| module docstring names trusted seam | [STRUCTURAL] | `grep -qi "trusted" app/reader.py || (echo "FAIL: seam contract not documented" && exit 1)` |
| sanitize tests + suite + types + lint pass | [MECHANICAL] | `.venv/bin/python -m pytest -q tests/test_reader_sanitize.py && .venv/bin/python -m mypy app && .venv/bin/python -m ruff check app tests || (echo "FAIL: SS-01 gates" && exit 1)` |
