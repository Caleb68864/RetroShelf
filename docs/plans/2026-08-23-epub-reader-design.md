---
date: 2026-08-23
topic: "In-browser EPUB reader for RetroShelf (old-iPad compatible, no-JS)"
author: Caleb Bennett
status: evaluated
evaluated_date: 2026-08-23
tags:
  - design
  - epub-reader
---

# In-Browser EPUB Reader — Design

## Summary

RetroShelf grows from an OPDS→iBooks bridge into a reading app: a server-rendered,
zero-JavaScript EPUB reader that works on iOS 5.1.1–12 Safari. The server unpacks
and sanitizes an EPUB once into a block-fragment cache; every reading page is then
a fast disk read wrapped in a template, navigated with plain `<a>` links. Reading
position is tracked server-side at block granularity, so "Continue reading"
survives both restarts and changes to the per-device page-size preference.

All decisions below were settled interactively with the operator before this
document was written; the "Approaches Considered" section records the fork that
was explicitly weighed.

## Approach Selected

**Pre-render sanitized chapters at first open; group blocks into parts at serve
time.** One cached rendering serves every device; each device's cookie decides
how much text lands on a page. Chosen over on-demand zip extraction because the
serve path must be as fast as possible for a 2010 iPad, and over per-device
pre-rendering because the split preference is per-device state.

## Constraints (inherited, non-negotiable)

From `vault/Build Constraints.md` — verified against real old-Safari behavior:

- **No JavaScript, ever.** All flows are `<a href>` links and `GET` forms.
- **No CSS Grid; no unprefixed flexbox for core layout.** Block layout +
  `max-width`. Same-origin CSS, system font stack only.
- **Single same-origin surface.** The apiKey stays server-side; nothing written
  to disk or rendered into HTML may contain an upstream URL with a credential.
- **Never buffer book downloads** — but the reader deliberately *does* buffer
  one EPUB at shelving time, under hard caps (below); this is a bounded,
  one-time cost, not a streaming path.
- Existing invariants stay intact: SSRF guard on every upstream URL, opaque v2
  bridge ids, secret-masked logs, `X-Content-Type-Options: nosniff`, security
  headers on HTML only.

## Architecture

```
Book page ── "Read here" ──> GET /read/{bid}
                                  │
                        shelved?──┼── no ──> SHELVE (one-time, per book):
                                  │           KavitaClient fetch (SSRF-guarded,
                                  │           size-capped) → zip open (zip-slip
                                  │           guarded) → container.xml → OPF →
                                  │           spine → per-chapter sanitize into
                                  │           block fragments → images extracted
                                  │           + downscaled → manifest.json
                                  │           all under /cache/reader/{book_key}/
                                  ▼
                        303 → /read/{bid}/{chapter}/{part}   (resume position
                                  │                           or chapter 1)
                                  ▼
                        READ PAGE: load chapter blocks from cache → group into
                        parts by rs_split cookie target → render read.html
                        (content + Prev/Next/TOC links) → record position
                        (book, chapter, first-block-index) in Store
```

Components:

- **`app/reader.py` (new)** — everything EPUB: fetch-and-shelve, OPF/spine
  parsing, sanitizer, block splitting, part grouping, manifest I/O, cache
  pruning. Pure functions where possible; no FastAPI imports.
- **`app/main.py` routes (new section)** — `/read/*` handlers: thin glue from
  request → reader functions → templates → Store.
- **`app/store.py`** — gains a bounded `reading` section (position records).
- **Templates** — `read.html` (reading page, slim chrome), `toc.html`
  (table of contents); `book.html` gains the "Read here" / "Continue reading"
  button; `home.html` gains the "Currently Reading" shelf.
- **`app/static/app.css`** — reader theme additions (book + phosphor modes).

## Components and Responsibilities

### `app/reader.py`

Owns the EPUB domain. Does NOT own HTTP routing, cookies, or the Store.

1. **Shelving** (`shelve_book(kc, record, cache_dir) -> Manifest`):
   - Fetch the EPUB via the existing `KavitaClient.open_stream` (URL comes from
     the decoded book record and is re-validated by `resolve_url`); **spool the
     stream to a temp file** under `/cache/reader/` with a hard cap
     (`MAX_EPUB_BYTES = 80MB`, capped-read pattern) — never hold the whole EPUB
     in RAM — then open `zipfile` against the on-disk file (zipfile needs
     random access; disk gives it for free). Delete the temp file when
     shelving completes or fails. <!-- Assumption: ASM-5 resolved by spooling. -->
   - Open with `zipfile`; **zip-slip guard**: resolve every member name, reject
     absolute paths and `..` traversal; never extract to disk paths derived
     from member names (content goes into *our* numbered files, so traversal
     cannot escape even in principle).
   - Parse `META-INF/container.xml` → OPF path → OPF (defusedxml). Build the
     spine: ordered list of content documents. Caps: ≤ 500 spine items,
     per-chapter source ≤ 2MB, total unpacked ≤ 120MB. Encrypted EPUBs
     (`META-INF/encryption.xml` present covering content) → `ReaderError`
     ("DRM or encrypted").
   - **Sanitize each chapter** (see Security) into an ordered list of
     block-level HTML fragment strings; write `chapters/{i}.json`
     (`{"blocks": [...]}`, UTF-8).
   - **Chapter titles** for the TOC: prefer the EPUB nav document / NCX label;
     fall back to the chapter's first `h1–h3` text; fall back to "Chapter N".
   - **Images**: collect referenced images from sanitized chapters, extract
     from the zip, downscale via the existing Pillow pipeline (max edge 1024,
     JPEG/PNG passthrough when small; decompression-bomb cap already global),
     write `images/{n}` + `.ct` sidecar (same pattern as the cover cache).
     Chapter `<img src>` is rewritten to `/read/{bid}/img/{n}` at serve time
     (the sanitized fragments store a stable `{IMG:n}` internal src; see Data
     Flow).
   - Write `manifest.json`: `{version: 1, book_key, title, author,
     chapters: [{title, blocks, chars}], images: N, total_chars, created}`.
     **No upstream URLs are written to disk** — re-shelving after a prune goes
     back through `/read/{bid}`, which carries the URL in the signed id.
   - Concurrency: shelve into a working directory `{book_key}.tmp-{pid}` and
     `os.rename` it to the final `{book_key}` directory as the last step —
     idempotent under any race (last rename wins; losers' work is discarded and
     cleaned). A per-process `asyncio.Lock` keyed by `book_key` remains as a
     fast path so two taps in one process don't do the work twice. Deployment
     runs single-worker uvicorn (Dockerfile), but correctness must not depend
     on that.
   - Cache bound: `/cache/reader` total ≤ 1GB, pruned oldest-manifest-first
     (whole book directories at a time), same best-effort pattern as covers.

2. **Pagination** (`parts_for(chapter_blocks, target_chars) -> list[range]`):
   deterministic greedy grouping — accumulate consecutive blocks until the
   running character total reaches the target, then start a new part; a single
   oversized block is its own part (blocks are never split). `target_chars`
   comes from the `rs_split` cookie: `small=6000`, `medium=12000` (default),
   `large=24000`, `whole=∞`. Also `part_containing(block_index, parts)` for
   resume.

3. **Progress** (`percent_of(manifest, chapter, block) -> int`): cumulative
   characters before the position ÷ `total_chars`.

### `app/main.py` — new routes

| Route | Behavior |
|---|---|
| `GET /read/{bid}` | Decode record id (existing codec, existing `BadIdError` path). **Non-EPUB record (e.g. a PDF id pasted by hand) → friendly 404: "Only EPUB books can be read in the browser."** If not shelved: shelve (friendly error page on `ReaderError`). Then 303 → the part containing the stored resume block, else chapter 0 part 1. |
| `GET /read/{bid}/toc` | Render `toc.html` from the manifest: chapter list with titles, current-position marker, "start over" link. |
| `GET /read/{bid}/{chapter}/{part}` | Load chapter blocks, group by cookie target, render `read.html`. Record position `(book_key, chapter, first_block_of_part)` + the display record in the Store. Out-of-range chapter/part → 404 error page. |
| `GET /read/{bid}/img/{n}` | Serve shelved image `n` with its stored content type (validated through `_safe_image_type`), `Cache-Control: private, max-age=86400`. Missing → tiny 404 GIF (same as covers). |

`/prefs` gains two cookie params, same pattern as existing ones:
`split=small|medium|large|whole` → `rs_split`; `reader=book|phosphor` →
`rs_reader_theme`. Both links carry the existing site token (they are
state-changing GETs).

`book.html`: for EPUB books, a "Read here" button under "Open in iBooks"
(iBooks stays the primary action); label becomes "Continue reading (Ch. N ·
P%)" when a position exists. PDFs: unchanged, no reader button.

`home.html`: "Currently Reading" shelf — up to 4 books by most-recent position,
each `title · author · P%`, linking to `/read/{bid}` (bid minted from the
stored record via the existing `_record_id`).

### `app/store.py` — `reading` section

`set_position(record, chapter, block, percent)` / `get_position(book_key)` /
`reading_list(limit)` — bounded (≤ 100 entries, oldest dropped), records pass
through the existing `sanitize_record` projection plus `chapter`/`block`/
`percent`/`updated` numeric fields. Persistence inherits the store's atomic
fsync write.

**Decision (accepted):** position is server-global, not per-device — two people
sharing one bridge share "last read". Acceptable for a personal LAN tool;
last-read-wins.

**Accepted ops cost:** every part view rewrites the (tiny) state file with an
fsync — trivial on a LAN bridge; noted deliberately.

**Finished state:** serving the last part of the last chapter records 100%;
the home shelf renders such books as "finished" instead of a percentage.

## Data Flow

1. **Shelving**: upstream EPUB bytes → capped buffer → zipfile → per-chapter
   XHTML → defusedxml parse → allowlist rebuild → block fragment strings
   (images referenced as internal `{IMG:n}` placeholders, intra-book links
   resolved to `{CH:i}` placeholders or unwrapped) → `chapters/{i}.json` +
   `images/{n}` + `manifest.json`.
2. **Serving**: `chapters/{i}.json` → blocks → part grouping (cookie) →
   placeholder substitution (`{IMG:n}` → `/read/{bid}/img/{n}`, `{CH:i}` →
   `/read/{bid}/{i}/1`) → joined into `content_html` → `read.html` renders it
   with `| safe` (safety is established at shelve time by the sanitizer — this
   is the single trusted-HTML seam in the app and is commented as such).
3. **Position**: each part render → `Store.set_position` → home shelf and the
   book-page button read it back.

Placeholders use a format that cannot survive sanitization from book content
(they are inserted *after* attribute stripping, into attributes we construct),
so a book cannot forge an `{IMG:...}` that escapes the cache directory — image
lookup is by integer index into the manifest, never by name. Substitution
values are restricted to `[A-Za-z0-9/._-]` (bridge ids and integer indexes),
so a substituted value can never break out of the attribute it lands in.

## Security

Book content is untrusted HTML rendered on our origin, and **old Safari has no
CSP** — the sanitizer is the entire wall:

- Parse each chapter with defusedxml. **Parse failure → the chapter degrades to
  escaped plain text** split on blank lines (never raw passthrough).
- Rebuild keeping only allowlisted elements:
  `p div span h1 h2 h3 h4 h5 h6 em strong i b u s small br hr blockquote ul ol
  li dl dt dd img a table thead tbody tr td th caption sup sub pre code cite
  figure figcaption section article`.
  Non-allowlisted elements are **unwrapped** (children kept, tag dropped);
  `script`, `style`, `iframe`, `object`, `embed`, `form`, `link`, `meta` are
  **dropped entirely, children included**.
- All attributes dropped except: `img` keeps rewritten `src` (internal
  placeholder only) + `alt`; `a` keeps rewritten internal `href` only (foreign
  or unresolvable links are unwrapped to plain text); `td/th` keep
  `colspan`/`rowspan` (integer-validated); everything else attribute-free.
  No `on*`, no `style`, no `class` from the book.
- Entity/DTD attacks: defusedxml already refuses external entities and
  billion-laughs.
- Zip: member-count, per-member and total-size caps enforced against *decompressed*
  sizes; traversal names rejected.
- The reader routes sit behind the existing access-key/IP middleware
  automatically (they are not in `_OPEN_PREFIXES`).

## Error Handling

| Failure | Behavior |
|---|---|
| EPUB too big / too many items / chapter too big | `ReaderError` → friendly page: "This book is too large to read in the browser — use Open in iBooks." (502/413-style, existing error template) |
| DRM / encrypted | `ReaderError` → "This book is protected and can't be read in the browser — use Open in iBooks." |
| Malformed zip / missing OPF | `ReaderError` → "This book can't be read in the browser — use Open in iBooks." |
| One bad chapter in an otherwise good book | Chapter degrades to escaped text; book still readable. |
| Upstream fetch fails | Existing `KavitaError` path (502 "Library unavailable"). |
| Cache pruned mid-book | Next page request finds no manifest → re-shelve transparently via the same `/read/{bid}` flow (Prev/Next links include `bid`). |
| Bad chapter/part in URL | 404 via existing `BadIdError`-style friendly page. |
| Image missing | Tiny 404 GIF (cover pattern). |
| Pillow unavailable | Shelve text-only: skip image extraction, drop `img` placeholders; book remains fully readable (mirrors the cover-transcode fallback). |

`ReaderError` is a new `RetroShelfError` subclass → one new row in
`_ERROR_TABLE`; messages steer the user to the iBooks path that always works.

## Reader Presentation

- `read.html` extends a slimmed chrome: thin top bar (book title → links to
  `/read/{bid}/toc`), content column (`max-width: 40em`), footer nav:
  `← Prev · Ch N, part P of Q · Next →`, then split-size links
  `[ Page size: S M L Whole ]`, theme toggle `[ Book / Phosphor ]`, and
  `[ Back to book ]`.
- **Book theme (default)**: sepia page (`#f4ecd8` / `#33291f`), system serif
  stack (`Georgia, 'Times New Roman', serif`), `line-height: 1.6`, honors the
  existing Large Print cookie. **Phosphor theme**: the existing terminal look.
- CSS: block layout only, no grid, no flex; safe-list features per Build
  Constraints (border-radius, media queries, em/rem).
- Prev on first part / Next on last part link across chapter boundaries;
  absent at book start/end (replaced by TOC link).

## Success Criteria

1. On an EPUB book page, "Read here" appears; first tap shelves and lands on a
   readable page; the *second* request performs **zero upstream fetches**
   (assertable via mock transport call count).
2. A hostile fixture EPUB (scripts, event handlers, style/iframe/form, entity
   tricks, zip-slip member names, image named `../../etc/passwd`) renders with
   **no** `<script`, `on*=`, `style=`, or foreign `href`/`src` in the output,
   and writes nothing outside `/cache/reader/`.
3. Changing `rs_split` regroups pages; a stored position re-resolves to the
   part containing the same block under the new size.
4. After reading a part, the home page shows the book under "Currently Reading"
   with a percentage that increases as later parts are read.
5. TOC lists every spine chapter with a title and working link.
6. Oversized / DRM / malformed EPUBs produce the friendly error page (no
   traceback, correct status), and the book page's iBooks button still works.
7. All pre-existing tests still pass; new unit tests cover sanitizer
   (adversarial), spine parsing, pagination determinism (incl. oversized
   block), resume-across-size-change, and route flows against a synthetic
   EPUB fixture built in-test (stdlib zipfile).
8. `mypy app` stays at zero errors; `ruff check app tests` stays clean.
9. No `<script`, no inline `style=`, no CSS grid/flex in any new template or
   CSS; every new interaction is a plain `<a href>` or GET form.
10. Existing download/iBooks headers are byte-identical (regression-guarded by
    the existing suite).

## Exclusions

- **No PDF in-browser reading** — PDFs keep the current inline-view behavior.
- **No JavaScript of any kind**, including "progressive enhancement".
- No annotations, highlights, bookmarks, or in-book search (resume only).
- No EPUB CSS passthrough (publisher styles are always discarded).
- No per-device reading positions (server-global, last-read-wins).
- No EPUB3 audio/video/MathML/fixed-layout support — such elements are
  unwrapped/dropped by the sanitizer; reflowable text is the target.
- No changes to the OPDS publisher, downloads, covers, or search subsystems.

## Open Questions

1. **TOC source of truth**: nav doc vs NCX vs headings — design says try in
   that order; if real-world books surface bad titles, the fallback chain may
   need tuning (does not change architecture).
2. **Image quality cap** (1024px max edge) — may warrant a config knob later if
   diagrams are illegible on retina iPads; start fixed, YAGNI.

## Approaches Considered

- **A. On-demand from cached EPUB zip** — keep only the `.epub`; sanitize per
  request. Less disk; but every tap pays parse+sanitize, and part-splitting
  must be recomputed identically per request. Rejected: serve-path speed is the
  scarcest resource on a 2010 iPad.
- **B. Pre-render at first open + serve-time part grouping** — *selected*, see
  above.
- **C. Pre-render per split-size** (4 variants per book) — rejected: 4× disk
  for state that is naturally a serve-time parameter.
- (All-in-memory serving was dismissed early: dies on restart, unbounded RAM.)

## Commander's Intent

**Desired End State:** An operator taps "Read here" on any EPUB in RetroShelf
from an iOS 5.1.1–12 iPad and reads the whole book in Safari with plain-link
page turns, resumable across sessions and page-size changes — with the iBooks
hand-off untouched and every existing test still green.

**Purpose:** Turn RetroShelf from a delivery bridge into a reading app, so
books are usable even when iBooks import is inconvenient (shared iPads, quick
sampling, books not worth importing) — without sacrificing the vintage-device
guarantee that is the project's reason to exist.

**Constraints:**
- MUST NOT emit any JavaScript, CSS Grid, or unprefixed-flex core layout in
  reader pages; all interactions are `<a href>` / GET forms.
- MUST NOT alter existing download/cover/OPDS behavior or headers (the iBooks
  path is regression-guarded and sacred).
- MUST NOT write any upstream URL containing a credential to disk or render
  one into HTML.
- MUST pass every upstream byte through the existing SSRF guard before fetch.
- MUST sanitize book HTML at shelve time through the allowlist; `| safe` is
  permitted ONLY on sanitizer output (the single trusted seam, commented).
- MUST enforce all resource caps (80MB EPUB, 500 spine items, 2MB/chapter,
  120MB unpacked, 1GB reader cache) and degrade with friendly errors.
- MUST keep `mypy app` at zero errors and `ruff check app tests` clean;
  Sphinx-style docstrings on all new public functions.
- MUST NOT add new runtime dependencies (stdlib + existing: fastapi, httpx,
  jinja2, defusedxml, Pillow).

**Freedoms:**
- Agent MAY choose internal file formats within the cache layout described
  (e.g. exact JSON field layout of chapter files) if the manifest carries
  `version: 1` for future migration.
- Agent MAY tune the sanitizer allowlist ±a few benign inline elements.
- Agent MAY choose TOC title heuristics order details and fallback text.
- Agent MAY organize `reader.py` internals freely (classes vs functions) as
  long as parse/sanitize/paginate stay unit-testable without FastAPI.

**Committed interface/contract defaults:**
- Shelving → **Default:** `async def shelve_book(kc: KavitaClient, record: dict, cache_dir: str) -> Manifest` where `Manifest` is a dataclass `{version: int, book_key: str, title: str, author: str, chapters: list[ChapterMeta], images: int, total_chars: int, created: float}` and `ChapterMeta = {title: str, blocks: int, chars: int}` _(override only if a field proves unnecessary)_.
- Pagination → **Default:** `def parts_for(block_lengths: list[int], target_chars: int | None) -> list[tuple[int, int]]` returning `(start, end_exclusive)` block ranges; `None` target = one part. `def part_containing(block_index: int, parts: list[tuple[int, int]]) -> int` (1-based part number).
- Progress → **Default:** `def percent_of(manifest: Manifest, chapter: int, block: int) -> int` (0–100, cumulative chars).
- Store → **Default:** `set_position(record: dict, chapter: int, block: int, percent: int) -> None`, `get_position(book_key: str) -> dict | None`, `reading_list(limit: int = 4) -> list[dict]`; positions live under a top-level `reading` key, ≤ 100 entries.
- Cookie contract → **Default:** `rs_split` ∈ `small|medium|large|whole` → targets `6000|12000|24000|None`; `rs_reader_theme` ∈ `book|phosphor`; both set via `/prefs` with the site token, defaults `medium`/`book`.
- Routes → **Default:** exactly the four routes in the Components table; chapter is 0-based in URLs, part is 1-based.
- Errors → **Default:** `class ReaderError(RetroShelfError)` + one `_ERROR_TABLE` row (502, "Can't read this book", message steering to iBooks); subtype messages chosen per failure in the Error Handling table.

## Execution Guidance

**Observe:**
- `pytest -q` after each component lands (suite must stay green; currently 233).
- `mypy app` and `ruff check app tests` after each file (both must stay clean).
- Fixture EPUB round-trip: shelve → read pages → assert zero upstream calls on
  the second request (mock transport call count).

**Orient:**
- Follow the capped-read pattern from `app/kavita.py::_read_capped` for the
  EPUB spool; follow the temp-file + `os.replace` pattern from
  `app/download.py` cover cache and `app/store.py::_save` for all writes.
- New errors go through `app/errors.py` subclass + `app/main.py::_ERROR_TABLE`.
- Mint book ids with `app/main.py::_record_id`; keys with `store.book_key`.
- Serve images through `app/download.py::_safe_image_type` validation.
- Templates extend `base.html` conventions; reuse `_macros.html` where a book
  row appears; keep the site-token pattern for the two new `/prefs` params.
- Match the Sphinx docstring style used across `app/`.

**Escalate When:**
- A new runtime dependency seems required (it shouldn't).
- The sanitizer approach can't handle a fixture without weakening the
  allowlist or attribute rules.
- Any change to existing download/header code paths seems necessary.
- The Store schema needs fields beyond those committed above.

**Shortcuts (Apply Without Deliberation):**
- `RetroShelfError` subclass + `_ERROR_TABLE` row for every user-visible failure.
- Temp file/dir + atomic rename for every cache write.
- `sanitize_record`-style projection for anything persisted.
- Tests: unit tests in `tests/test_reader.py`, route tests in
  `tests/test_reader_routes.py`, synthetic EPUB fixtures built with stdlib
  `zipfile` in-test (no binary fixtures committed).

## Decision Authority

**Agent Decides Autonomously:** block-split internals, chapter-file JSON
layout, TOC heuristic details, test case design, CSS values within the
old-Safari safe list, sanitizer allowlist fine-tuning, error message wording.

**Agent Recommends, Human Approves:** any new env/config knob, any change to
existing routes/templates beyond those listed, any deviation from committed
interface defaults, relaxation of any resource cap.

**Human Decides:** scope additions (annotations, search, per-device positions,
PDF reading), new dependencies, changes to the iBooks delivery flow.

## War-Game Results

**Most Likely Failure:** real-world chapter is not well-formed XML → per-chapter
escaped-plain-text degradation keeps the book readable; malformed fixture test
locks it in.
**Scale Stress:** 500-chapter book = one small JSON read per page turn; TOC page
is long but renders; acceptable — N/A beyond caps.
**Dependency Risk:** Pillow missing → text-only shelving (images dropped),
mirroring the cover fallback; defusedxml is a hard dependency already shipped.
**Maintenance Assessment:** `reader.py` isolation + `version: 1` manifests +
this document = a new maintainer can rebuild context from the plan alone. Good.

## Evaluation Metadata
- Evaluated: 2026-08-23
- Cynefin Domain: Complicated (depth matches)
- Critical Gaps Found: 0
- Important Gaps Found: 2 (2 resolved)
- Suggestions: 3 (3 resolved)
- Assumptions: 9 audited; 0 contradicted; ASM-7 (sanitizer seam) flagged as
  mandatory review focus

## Next Steps

- [x] `/forge-evaluate` — done 2026-08-23 (this revision)
- [ ] `/forge` the improved design into a master spec
- [ ] `/forge-red-team` the master spec
- [ ] `/forge-prep` phase specs
- [ ] `/forge-run` to implement
