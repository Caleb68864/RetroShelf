# In-Browser EPUB Reader

## Meta

- Client: Personal
- Project: RetroShelf (Kavita-Retro-iPad)
- Repo: `/home/caleb/Projects/Kavita-Retro-iPad`
- Date: 2026-08-23
- Author: Caleb Bennett
- Source design: `docs/plans/2026-08-23-epub-reader-design.md` (evaluated 2026-08-23)
- Quality score: 34/35 (Outcome 5, Scope 5, Decision guidance 5, Edge coverage 4, Acceptance criteria 5, Decomposition 5, Purpose alignment 5)
- Status: ready

## Outcome

An operator taps "Read here" on any EPUB in RetroShelf from an iOS 5.1.1–12
iPad and reads the whole book in Safari with plain-link page turns, resumable
across sessions and page-size changes. The iBooks hand-off is untouched and
every pre-existing test stays green.

## Intent

**Trade-off hierarchy (when valid approaches conflict):**
1. Old-Safari compatibility (no JS, no Grid/flex, plain links) beats everything.
2. Correctness of the sanitizer seam beats performance.
3. Serve-path speed beats shelve-path speed.
4. Consistency with existing codebase patterns beats ideal design.
5. Simple beats flexible (YAGNI; no config knobs beyond those specced).

**Decision boundaries:** The agent decides autonomously: block-split internals,
chapter-file JSON layout, TOC heuristic details, test case design, CSS values
within the old-Safari safe list, sanitizer allowlist fine-tuning, error message
wording. Stop and ask when: a new runtime dependency seems required; the
sanitizer approach cannot handle a fixture without weakening attribute rules;
any change to existing download/header code paths seems necessary; the Store
schema needs fields beyond those committed here.

## Context

RetroShelf is a server-rendered, zero-JavaScript FastAPI + Jinja2 bridge that
proxies OPDS libraries (Kavita, Gutenberg, ManyBooks) to very old iPads and
hands books to iBooks with exact headers. This spec adds an in-browser EPUB
reading experience on the same no-JS constraints. Full rationale, approach
comparison, war-gaming, and committed interface defaults live in the design
doc: `docs/plans/2026-08-23-epub-reader-design.md`. Research grounding:
`vault/Build Constraints.md` (verified old-Safari behavior; no-JS is mandatory,
no CSS Grid, system fonts only).

Existing patterns to follow: capped reads (`app/kavita.py` `_read_capped`),
temp-file + `os.replace` atomic writes (`app/download.py` cover cache,
`app/store.py` `_save`), typed errors via `RetroShelfError` subclass + one
`_ERROR_TABLE` row (`app/main.py`), record projection (`app/store.py`
`sanitize_record`), bridge ids via `_record_id` (`app/main.py`), image type
validation (`app/download.py` `_safe_image_type`), Sphinx-style docstrings
throughout, `mypy app` at zero errors, `ruff check app tests` clean.

## Requirements

1. EPUB book pages offer "Read here" (or "Continue reading (Ch. N · P%)");
   PDF pages are unchanged.
2. First open shelves the book into `/cache/reader/{book_key}/` (sanitized
   block fragments + manifest); subsequent pages serve from cache with zero
   upstream fetches.
3. Reading pages are chapter-scroll with long chapters split into parts;
   part size follows the per-device `rs_split` cookie
   (small=6000 / medium=12000 (default) / large=24000 / whole=None chars).
4. Position (book, chapter, first-block-index) is recorded server-side on
   every part view; resume survives restarts and split-size changes; last
   part of last chapter records 100% ("finished").
5. Home page shows a "Currently Reading" shelf (≤ 4 books, % or "finished").
6. A TOC page lists every spine chapter with title and link.
7. Book content is sanitized at shelve time through a strict allowlist; no
   script/style/event-handler/foreign-URL survives into served HTML.
8. Resource caps enforced: EPUB ≤ 80MB (spooled to temp file, never RAM),
   ≤ 500 spine items, ≤ 2MB/chapter source, ≤ 120MB unpacked,
   `/cache/reader` ≤ 1GB pruned oldest-first.
9. DRM'd/malformed/oversized EPUBs fail with a friendly error page steering
   to the iBooks path; one bad chapter degrades to escaped text without
   failing the book.
10. Reader theme: book look by default (serif/sepia, honors Large Print),
    phosphor via `rs_reader_theme` cookie; both set through `/prefs` with the
    site token.
11. Images inside chapters are served from the shelved cache at
    `/read/{bid}/img/{n}` (downscaled ≤ 1024px at shelve time); Pillow absent
    → text-only shelving.
12. No JavaScript, no CSS Grid, no unprefixed-flex core layout anywhere new;
    all interactions are `<a href>` links or GET forms.
13. `mypy app` stays at zero errors; `ruff check app tests` stays clean; all
    pre-existing tests pass unchanged.

## Sub-Specs

---
sub_spec_id: SS-01
phase: run
depends_on: []
---

### 1. Sanitizer and block splitter

- **Scope:** Create `app/reader.py` with the pure content layer: chapter
  sanitization and block splitting. `sanitize_chapter(xhtml: str | bytes) ->
  list[str]` parses with defusedxml and rebuilds an ordered list of
  block-level HTML fragment strings. Parse failure → escaped-plain-text
  blocks split on blank lines (never raw passthrough). Element allowlist:
  `p div span h1 h2 h3 h4 h5 h6 em strong i b u s small br hr blockquote ul
  ol li dl dt dd img a table thead tbody tr td th caption sup sub pre code
  cite figure figcaption section article`; non-allowlisted elements are
  unwrapped (children kept); `script style iframe object embed form link
  meta` (and `svg`, `video`, `audio`, `math`) are dropped with children.
  Attributes: all dropped except `img` → placeholder `src` `{IMG:n}` + `alt`;
  `a` → internal `href` placeholder `{CH:i}` only (unresolvable links
  unwrapped to text; **fragment-only hrefs like `#note3` are unwrapped too**
  — an in-page anchor may land in a different part, so a dead link is worse
  than plain text); `td`/`th` → integer-validated `colspan`/`rowspan`.
  Image/link resolution callbacks are injected (the caller maps hrefs to
  image indexes / chapter indexes) so this module stays zip-agnostic.
  Module-level constants for caps and the allowlist. Sphinx docstrings.
- **Decisions:** Signature default `sanitize_chapter(source: str | bytes, *,
  resolve_image: Callable[[str], int | None], resolve_link: Callable[[str],
  int | None]) -> list[str]`. Placeholder substitution values downstream are
  restricted to `[A-Za-z0-9/._-]`.
- **Files (new):**
  - `app/reader.py`
  - `tests/test_reader_sanitize.py`
- **Acceptance criteria:**
  - [STRUCTURAL] `app/reader.py` defines `sanitize_chapter` with the committed signature and a module docstring describing the trusted-seam contract.
  - [BEHAVIORAL] A hostile chapter containing `<script>`, `onclick=`, `style=`, `<iframe>`, `<form>`, `javascript:` hrefs, and an external `<img src="http://evil/x.png">` sanitizes to output containing none of: `<script`, `on`-prefixed attributes, `style=`, `<iframe`, `<form`, `javascript:`, `http://evil`.
  - [BEHAVIORAL] Malformed (non-XML) input returns escaped-text blocks — output contains no unescaped `<` from the source and at least one block.
  - [BEHAVIORAL] Allowlisted structure survives: headings, paragraphs, lists, tables, `img` (as `{IMG:n}` placeholder src), intra-book links (as `{CH:i}` placeholders).
  - [MECHANICAL] `.venv/bin/python -m pytest -q tests/test_reader_sanitize.py` passes; `.venv/bin/python -m mypy app` reports no issues; `.venv/bin/python -m ruff check app tests` is clean.
- **Dependencies:** none

---
sub_spec_id: SS-02
phase: run
depends_on: ['SS-01']
---

### 2. EPUB parsing and shelving

- **Scope:** Extend `app/reader.py` with the shelving pipeline:
  `async shelve_book(kc: KavitaClient, record: dict, cache_dir: str) ->
  Manifest`. Spool the upstream EPUB via `kc.open_stream` to a temp file
  under `{cache_dir}/reader/` with an 80MB cap (capped-read pattern; never
  whole-file RAM); open `zipfile` from disk; zip-slip guard (reject absolute
  and `..` member names; content only ever written to our numbered files);
  parse `META-INF/container.xml` → OPF → spine (defusedxml); detect
  `META-INF/encryption.xml` covering content → DRM error. Caps: ≤ 500 spine
  items, ≤ 2MB/chapter source, ≤ 120MB total unpacked. Sanitize each chapter
  via SS-01 with resolvers that map image/chapter hrefs; extract referenced
  images, downscale with Pillow (max edge 1024, JPEG/PNG passthrough when
  small, decompression caps; Pillow absent → text-only, drop img
  placeholders); chapter titles from EPUB3 nav doc, then NCX, then first
  `h1–h3`, then "Chapter N". Write `chapters/{i}.json` (`{"blocks":
  [...]}`), `images/{n}` + `.ct` sidecar, `manifest.json` (dataclass
  `Manifest`: `version=1, book_key, title, author, chapters:
  list[ChapterMeta(title, blocks, chars)], images, total_chars, created`).
  Build in `{book_key}.tmp-{pid}` and `os.rename` to final dir (idempotent
  under races); per-process `asyncio.Lock` keyed by `book_key` as fast path.
  `load_manifest(cache_dir, book_key) -> Manifest | None`,
  `load_chapter(cache_dir, book_key, i) -> list[str]`. Prune `/cache/reader`
  to ≤ 1GB oldest-manifest-first, whole directories, best-effort. No
  upstream URL is ever written to disk. Add `ReaderError(RetroShelfError)`
  to `app/errors.py`. The spool fetch uses a **bounded per-request timeout
  (120s)** — the shared client's `read=None` must not let a stalled upstream
  hang the first tap forever (mirror the `fetch_feed` timeout-override
  pattern). An EPUB whose spine yields **zero readable chapters** raises
  `ReaderError` ("no readable content"). Log exactly one masked INFO line
  per successful shelve: book_key, chapter count, spooled bytes, elapsed ms.
- **Files (new):**
  - `tests/test_reader_shelve.py`
- **Files (modify):**
  - `app/reader.py`
  - `app/errors.py`
- **Acceptance criteria:**
  - [BEHAVIORAL] A synthetic EPUB (built in-test with stdlib `zipfile`: container.xml, OPF, 3 chapters, 1 image, NCX) shelves into `manifest.json` + `chapters/*.json` + `images/0`; manifest fields match the committed dataclass shape.
  - [BEHAVIORAL] Zip-slip fixture (member named `../../evil.txt`) shelves or fails cleanly and writes nothing outside `{cache_dir}/reader/` (asserted by directory walk).
  - [BEHAVIORAL] Oversized EPUB (declared or actual > cap), > 500 spine items, zero readable chapters, and `encryption.xml` fixtures each raise `ReaderError` with distinct messages; the temp spool file is removed afterward.
  - [BEHAVIORAL] One malformed chapter inside an otherwise good EPUB yields a readable book whose bad chapter is escaped text.
  - [STRUCTURAL] `app/errors.py` defines `ReaderError(RetroShelfError)`; no upstream URL string appears in any file written under the cache (fixture URL is asserted absent by grep over the shelved tree).
  - [MECHANICAL] `.venv/bin/python -m pytest -q tests/test_reader_shelve.py` passes; mypy/ruff clean as in SS-01.
- **Dependencies:** SS-01

---
sub_spec_id: SS-03
phase: run
depends_on: ['SS-02']
---

### 3. Pagination, progress, and Store positions

- **Scope:** Add to `app/reader.py`: `parts_for(block_lengths: list[int],
  target_chars: int | None) -> list[tuple[int, int]]` (greedy grouping of
  consecutive blocks into `(start, end_exclusive)` ranges; `None` → one
  part; an oversized single block is its own part; deterministic),
  `part_containing(block_index: int, parts: list[tuple[int, int]]) -> int`
  (1-based), `percent_of(manifest: Manifest, chapter: int, block: int) ->
  int` (0–100 by cumulative chars; last part of last chapter → 100). Cookie
  mapping constant `SPLIT_TARGETS = {"small": 6000, "medium": 12000,
  "large": 24000, "whole": None}` with default `medium`. Extend
  `app/store.py` with a bounded `reading` section: `set_position(record:
  dict, chapter: int, block: int, percent: int) -> None` (projects via
  `sanitize_record`, adds validated ints + `updated`), `get_position(
  book_key: str) -> dict | None`, `reading_list(limit: int = 4) ->
  list[dict]` (most-recent first), ≤ 100 entries oldest-dropped, loaded
  tolerantly like favorites/history.
- **Files (new):**
  - `tests/test_reader_paging.py`
- **Files (modify):**
  - `app/reader.py`
  - `app/store.py`
- **Acceptance criteria:**
  - [BEHAVIORAL] `parts_for` is deterministic and total: every block appears in exactly one part across all four split settings; an oversized block forms a singleton part; `part_containing` round-trips for every block.
  - [BEHAVIORAL] Resume-across-size-change: position stored while reading at `medium` resolves via `part_containing` to the part holding the same block at `small` and `whole`.
  - [BEHAVIORAL] `percent_of` is monotonic over reading order and returns 100 at the final block; Store `reading` section persists across a reload (new `Store` instance on the same file) and drops the oldest entry past 100.
  - [STRUCTURAL] Wrong-shaped `reading` data in the state file (list instead of dict, junk entries) loads to an empty section without raising.
  - [MECHANICAL] `.venv/bin/python -m pytest -q tests/test_reader_paging.py` passes; mypy/ruff clean.
- **Dependencies:** SS-02

---
sub_spec_id: SS-04
phase: run
depends_on: ['SS-03']
---

### 4. Reader routes and templates

- **Scope:** Add to `app/main.py` (inside `_register_routes`): `GET
  /read/{bid}` (decode via existing codec → non-EPUB record → friendly 404
  "Only EPUB books can be read in the browser"; shelve if unshelved; 303 to
  resume part or chapter 0 part 1), `GET /read/{bid}/toc`, `GET
  /read/{bid}/{chapter}/{part}` (load blocks, group by `rs_split` cookie,
  substitute placeholders — `{IMG:n}` → `/read/{bid}/img/{n}`, `{CH:i}` →
  `/read/{bid}/{i}/1`, values restricted to `[A-Za-z0-9/._-]` — render
  `read.html`, record position; out-of-range → 404), `GET
  /read/{bid}/img/{n}` (serve shelved image with `_safe_image_type`,
  `Cache-Control: private, max-age=86400`; missing → tiny 404 GIF). Add one
  `_ERROR_TABLE` row for `ReaderError` (502, "Can't read this book",
  message steering to iBooks). Extend `/prefs` with `split=` and `reader=`
  cookie params (site-token-gated, same pattern as existing). New templates
  `read.html` (slim chrome: top bar with book title → TOC link; content
  column `max-width: 40em`; footer: Prev/Next, "Ch N · part P of Q",
  split-size links, theme toggle, back-to-book link) and `toc.html`
  (chapter list, current-position marker, start-over link). The `| safe`
  filter appears exactly once, on sanitizer output, with a comment naming
  the trusted seam.
- **Decisions:** Chapter is 0-based in URLs, part 1-based; the image index
  route parameter is typed `n: int` so a non-integer can never reach the
  filesystem layer. Reader pages are gated by the existing access-key/IP
  middleware (not added to `_OPEN_PREFIXES`). The safe filter is written
  exactly as `| safe` (space before the pipe target) — the structural AC
  greps for that spelling.
- **Files (new):**
  - `app/templates/read.html`
  - `app/templates/toc.html`
  - `tests/test_reader_routes.py`
- **Files (modify):**
  - `app/main.py`
- **Acceptance criteria:**
  - [BEHAVIORAL] With a mocked upstream serving a synthetic EPUB: first `GET /read/{bid}` shelves and 303s to a part URL; the part page contains book text, Prev/Next links, and no `<script`/`on*=`/`style=`; a second part request performs zero upstream calls (mock transport call count).
  - [BEHAVIORAL] `rs_split=small` vs `whole` cookies yield different part counts for the same chapter; positions recorded under one size resume correctly under another.
  - [BEHAVIORAL] A PDF-record bid on `/read/{bid}` returns the friendly 404; a DRM fixture returns the `ReaderError` page (502) with the iBooks steer; out-of-range chapter/part → 404.
  - [BEHAVIORAL] `/read/{bid}/img/0` serves the shelved image with an `image/*` content type; a missing index returns the tiny 404 GIF.
  - [BEHAVIORAL] With `BRIDGE_ACCESS_KEY` configured, `/read/{bid}` without a key or cookie returns 403 (reader routes are inside the middleware gate).
  - [STRUCTURAL] `grep -c '| safe' app/templates/read.html` equals 1 and `app/templates/toc.html` contains none; the substitution charset guard exists in `app/main.py`.
  - [MECHANICAL] `.venv/bin/python -m pytest -q tests/test_reader_routes.py` passes; mypy/ruff clean.
- **Dependencies:** SS-03

---
sub_spec_id: SS-05
phase: run
depends_on: ['SS-04']
---

### 5. UI integration and reader themes

- **Scope:** Surface the reader in the existing UI. `book.html`: for EPUB
  records, add a "Read here" button under the iBooks button; when a
  position exists show "Continue reading (Ch. N · P%)" (or "Read again —
  finished"). The unshelved-state button carries the hint "(first open takes
  a moment)" so a slow first tap on old Safari reads as expected behavior. `home.html`: "Currently Reading" shelf (≤ 4 from
  `reading_list()`, each `title · author · %` or "finished", linking
  `/read/{bid}` via `_record_id`). `app/static/app.css`: reader styles —
  book theme default (sepia `#f4ecd8`/`#33291f`, `Georgia, 'Times New
  Roman', serif`, `line-height: 1.6`, honors the existing `big` class) and
  phosphor variant keyed off `rs_reader_theme` cookie → body class; block
  layout only (no grid, no flex), tap-friendly footer links. Route context
  for `book.html`/`home.html` extended accordingly in `app/main.py`.
- **Files (modify):**
  - `app/templates/book.html`
  - `app/templates/home.html`
  - `app/static/app.css`
  - `app/main.py`
- **Acceptance criteria:**
  - [BEHAVIORAL] An EPUB book page contains a `/read/` link; a PDF book page contains none; after reading a part, the book page shows "Continue reading" with chapter and percent, and the home page lists the book under "Currently Reading".
  - [BEHAVIORAL] With `rs_reader_theme=phosphor`, the read page body carries the phosphor class; default carries the book class; `rs_big=1` increases reader font size via the existing `big` mechanism.
  - [STRUCTURAL] `grep -E 'display:\s*(grid|flex)' app/static/app.css` finds no match in the new reader styles; new CSS uses only the old-Safari safe list; `grep -c '<script' app/templates/read.html app/templates/toc.html app/templates/book.html app/templates/home.html` totals 0.
  - [MECHANICAL] `.venv/bin/python -m pytest -q` passes (full suite); mypy/ruff clean.
- **Dependencies:** SS-04

---
sub_spec_id: SS-06
phase: run
depends_on: ['SS-04', 'SS-05']
---

### 6. End-to-end integration, docs, and evidence

- **Scope:** Prove the whole flow and document the feature. New
  `tests/test_reader_e2e.py`: one `[INTEGRATION]` test walking home → book
  page → "Read here" → shelve → read three parts across a chapter boundary
  → TOC → change split size → resume → home shelf shows progress → finish
  book → 100%/"finished" — all through `TestClient` with a mocked upstream,
  asserting the iBooks download headers on the same book are byte-identical
  to before (regression guard). README: add a "Read in the browser" feature
  section (what it does, split-size/theme prefs, caps, EPUB-only). Write
  `docs/notes/ss06-reader-evidence.md` with the final pytest/mypy/ruff
  outputs.
- **Files (new):**
  - `tests/test_reader_e2e.py`
  - `docs/notes/ss06-reader-evidence.md`
- **Files (modify):**
  - `README.md`
- **Acceptance criteria:**
  - [INTEGRATION] The end-to-end test exercises home → book → shelve → read → resume → finish across all sub-spec boundaries and passes.
  - [BEHAVIORAL] The download-headers regression assertion passes: `Content-Type` and `Content-Disposition` for the EPUB download are unchanged.
  - [STRUCTURAL] README contains a "Read in the browser" section mentioning `rs_split` sizes and EPUB-only scope; `docs/notes/ss06-reader-evidence.md` exists with command outputs.
  - [MECHANICAL] `.venv/bin/python -m pytest -q` (full suite), `.venv/bin/python -m mypy app`, and `.venv/bin/python -m ruff check app tests` all pass, outputs captured in the evidence file.
- **Dependencies:** SS-04, SS-05

## Edge Cases

- Malformed chapter XML → escaped-text blocks; book stays readable.
- DRM/encrypted → `ReaderError`, friendly page, iBooks path preserved.
- Oversized EPUB / spine / chapter / unpacked total → `ReaderError` before
  resource exhaustion; temp spool always cleaned.
- Zip-slip member names → rejected; nothing written outside the cache.
- Cache pruned mid-book → next `/read/{bid}` re-shelves transparently.
- Pillow unavailable → text-only shelving; `img` placeholders dropped.
- Non-EPUB record on `/read/{bid}` → friendly 404.
- EPUB with zero readable chapters → `ReaderError` ("no readable content").
- Stalled upstream during shelve → bounded 120s timeout → `KavitaError` page,
  never an indefinite hang.
- Concurrent shelving (two devices, same book) → tmp-dir + rename, last
  rename wins, no corruption; single-process lock as fast path.
- Book text containing literal `{IMG:0}` → substitution values are benign
  path strings; no markup injection possible.
- Position fsync per page turn → accepted ops cost (tiny file, LAN scale).

## Out of Scope

- PDF in-browser reading (PDFs keep current inline behavior).
- Any JavaScript, including progressive enhancement.
- Annotations, highlights, bookmarks, in-book search.
- Per-device reading positions (server-global, last-read-wins).
- EPUB3 audio/video/MathML/fixed-layout support.
- Changes to OPDS publisher, downloads, covers, or search subsystems.
- New runtime dependencies or config knobs beyond the two cookies.

## Constraints

**Musts:**
- Every upstream byte passes the existing SSRF guard (`resolve_url`) before fetch.
- Sanitizer runs at shelve time; `| safe` only on its output, once, commented.
- All resource caps from Requirements 8 enforced with friendly failures.
- Atomic writes (temp + rename) for every cache artifact; no upstream URL on disk.
- Sphinx docstrings on all new public functions; mypy zero; ruff clean.

**Must-Nots:**
- MUST NOT emit JavaScript, CSS Grid, or unprefixed-flex core layout.
- MUST NOT alter existing download/cover/OPDS behavior or headers.
- MUST NOT write credentialed URLs to disk or HTML.
- MUST NOT add runtime dependencies.

**Preferences:**
- Prefer existing codebase patterns (capped reads, cover-cache writes,
  `_ERROR_TABLE`, `sanitize_record`, `_record_id`) over new abstractions.
- Prefer pure, FastAPI-free functions in `app/reader.py`.
- Prefer synthetic in-test EPUB fixtures over committed binaries.

**Escalation Triggers:**
- A new runtime dependency appears necessary.
- Sanitizer cannot pass a fixture without weakening attribute rules.
- Any modification to existing download/header code paths.
- Store schema needs fields beyond the committed interface.

## Verification

Full-suite `pytest -q` green (233 pre-existing + all new tests), `mypy app`
zero errors, `ruff check app tests` clean, and the SS-06 integration test
walking the complete user journey — including the byte-identical iBooks
header regression check — passing. Manual smoke (operator): open a book on a
real or simulated old-Safari UA, read three pages, change page size, resume,
confirm home shelf progress.

## Phase Specs

Refined by `/forge-prep` on 2026-08-23.

| Sub-Spec | Phase Spec |
|----------|------------|
| 1. Sanitizer and block splitter | `docs/specs/epub-reader/sub-spec-1-sanitizer-and-block-splitter.md` |
| 2. EPUB parsing and shelving | `docs/specs/epub-reader/sub-spec-2-epub-parsing-and-shelving.md` |
| 3. Pagination, progress, Store positions | `docs/specs/epub-reader/sub-spec-3-pagination-progress-and-store-positions.md` |
| 4. Reader routes and templates | `docs/specs/epub-reader/sub-spec-4-reader-routes-and-templates.md` |
| 5. UI integration and reader themes | `docs/specs/epub-reader/sub-spec-5-ui-integration-and-reader-themes.md` |
| 6. End-to-end integration, docs, evidence | `docs/specs/epub-reader/sub-spec-6-end-to-end-integration-docs-and-evidence.md` |

Index: `docs/specs/epub-reader/index.md`
