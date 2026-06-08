---
date: 2026-06-08
topic: "iPad QoL pack for RetroShelf (features 1, 2, 4, 6)"
author: Caleb Bennett
status: draft
tags:
  - design
  - ipad-qol-pack
  - retroshelf
---

# iPad QoL Pack — Design

## Summary

Four quality-of-life improvements for RetroShelf that stay strictly within the
iOS 5.1.1–12 Mobile Safari envelope (server-rendered HTML, **no JavaScript**,
conservative CSS — no Grid, no unprefixed flexbox for layout, same-origin assets
only). The pack is decomposed into four **independent sub-specs** so it is
dark-factory build-ready: each touches a small, well-bounded slice of the app
and ships with its own tests.

1. **SS1 — Home-screen web app**: Add-to-Home-Screen meta tags + a same-origin
   `apple-touch-icon` so RetroShelf launches fullscreen/chromeless like a native
   app on a dedicated iPad.
2. **SS2 — Cover transcoding**: Fix WebP covers (unrenderable before iOS 14) and
   tame oversized covers by transcoding-when-needed to baseline JPEG with
   downscaling, via Pillow, with a disk cache on the `/cache` volume.
3. **SS4 — Current-page sort**: `?sort=title|author|format` links that reorder
   the books **on the current feed page** only (pagination is upstream-driven, so
   whole-library sort is infeasible); honest "this page" labelling.
4. **SS6 — Polish bundle**: search-input iOS keyboard hints, long `Cache-Control`
   on `/static`, HTML-only gzip, and larger tap targets in large-print mode.

(The numbering 1/2/4/6 mirrors the original QoL idea list; "3" and "5" were not
selected.)

## Approach Selected

**Incremental, per-feature sub-specs that follow existing RetroShelf patterns.**
Each feature is implemented where its concern already lives (templates for meta
tags, `app/download.py` for cover handling, the `/feed` route for sort, a
middleware + CSS for polish), with no cross-cutting refactor. Rationale: the
codebase is small, well-factored, and heavily tested (133 tests); the lowest-risk
path is to extend each seam rather than introduce a new abstraction layer.

## Architecture

RetroShelf is a single FastAPI app (`app/`) with Jinja2 templates
(`app/templates/`) and one static stylesheet (`app/static/app.css`). The pack
adds nothing to the top-level architecture; it extends four existing seams:

```
                    ┌─────────────────────── app/main.py (routes, middleware) ──┐
  iPad Safari ──▶  │  /                home.html                                 │
  (no JS)          │  /feed/{fid} ──▶  feed.html      ← SS4 sort control + ?sort  │
                   │  /search          search.html                               │
                   │  /cover/{cid} ─▶  stream_cover() ← SS2 transcode + /cache    │
                   │  /static/app.css  StaticFiles     ← SS6 Cache-Control        │
                   │  (all HTML resp.)                 ← SS6 gzip (html only)      │
                   │  base.html <head>                 ← SS1 web-app meta + icon   │
                   └────────────────────────────────────────────────────────────┘
                                  │
                      app/static/icons/apple-touch-icon-180.png   ← SS1 asset
                      {CACHE_DIR}/covers/{sha256}                  ← SS2 disk cache
```

Key components touched, by sub-spec:

- **SS1**: `app/templates/base.html` (`<head>`), new static PNG icon +
  generator script. No Python logic.
- **SS2**: `app/download.py` (`stream_cover` → buffer/inspect/transcode/cache),
  `app/config.py` (new `cache_dir`, default `/cache`), `app/main.py` cover route
  wiring, `requirements.txt` (+`Pillow`).
- **SS4**: `app/main.py` `/feed/{fid}` handler (accept `sort`, reorder entries,
  carry param into pager links), `app/templates/feed.html` (sort control row),
  `app/static/app.css` (control styling).
- **SS6**: `app/main.py` (gzip-HTML middleware, static Cache-Control),
  `app/templates/{feed,search,home}.html` (search input attrs),
  `app/static/app.css` (`.big` tap-target rules).

## Components

### SS1 — Home-screen web app
- **Owns**: the `<head>` meta tags and the icon asset. Adds:
  `apple-mobile-web-app-capable=yes`, `apple-mobile-web-app-status-bar-style=black`,
  `apple-mobile-web-app-title=RetroShelf`, `<link rel="apple-touch-icon"
  href="/static/icons/apple-touch-icon-180.png">`, and a matching
  `<link rel="icon">`. Also adds `-webkit-text-size-adjust:100%` if not present.
- **Does NOT own**: any routing, the manifest (Web App Manifest is ignored by
  old Safari — explicitly out of scope), or PWA/service-worker behaviour.
- **Icon asset**: a 180×180 baseline PNG in the retro amber-on-black aesthetic,
  generated deterministically by a committed script (`tools/make_icon.py`, uses
  Pillow which SS2 adds) and the generated PNG committed to `app/static/icons/`.
  Same-origin, no external fonts/libraries (per Build Constraints §4).

### SS2 — Cover transcoding
- **Owns**: turning an arbitrary upstream cover into something old Safari can
  render efficiently. New behaviour in `stream_cover` (or a new helper it calls):
  1. Compute `key = sha256(upstream_url)`; if `{cache_dir}/covers/{key}` exists,
     serve those bytes with the stored content-type (no upstream fetch, no
     Pillow). **Disk-cache hit is the fast path.**
  2. Else fetch the cover fully into memory (covers are small — buffering is
     acceptable here, unlike book streams which must never buffer).
  3. Inspect with Pillow. **Passthrough** (serve original bytes, original
     content-type) when format ∈ {JPEG, PNG} and `max(w,h) ≤ MAX_EDGE`.
  4. **Transcode** otherwise (WebP, oversized, or unknown): downscale to
     `MAX_EDGE` preserving aspect, re-encode **baseline** JPEG quality
     `JPEG_QUALITY`, content-type `image/jpeg`.
  5. Write the served bytes+type to the disk cache, then return them.
- **Config**: `MAX_EDGE` default 320, `JPEG_QUALITY` default 80, `cache_dir`
  default `/cache` (new `CACHE_DIR` env). Cover cache lives in
  `{cache_dir}/covers/`.
- **Does NOT own**: book/file streaming (untouched — still streamed, never
  buffered). Range support for covers is dropped (covers are small; serve full
  `200`). The `apiKey` never reaches the client (already true; hashing the URL
  for the cache key also keeps the key off disk in plaintext filenames).
- **Graceful degradation**: if Pillow import fails at runtime, fall back to the
  current passthrough-stream behaviour (covers still work, just un-transcoded).

### SS4 — Current-page sort
- **Owns**: optional reordering of the **already-fetched** entries on one feed
  page. `/feed/{fid}` accepts `sort` ∈ {`title`,`author`,`format`}; absent/any
  other value = upstream order (default). Sorting is **stable**; navigation
  entries (`is_nav=True`) keep their original relative order and stay grouped
  ahead of book entries, which are sorted by the chosen key
  (title/author casefolded, empty author sorts last; format groups EPUB before
  PDF). The pager `next_url`/`prev_url` carry the active `sort` param forward.
- **UI**: a sort-control row in `feed.html` rendered only when book entries are
  present — `Sort: [ Default ] [ Title ] [ Author ] [ Format ]` as plain
  `<a href>` links (no JS), the active one marked; when active, a small honest
  caption "sorted: this page". Styled with the existing button/link classes,
  block layout, large tap targets.
- **Does NOT own**: any whole-library/global sort (infeasible — pagination is
  upstream-driven), and does not change which entries appear, only their order.

### SS6 — Polish bundle
- **Search input hints**: add `autocorrect="off" autocapitalize="off"
  autocomplete="off" spellcheck="false"` to the `q` text input in `feed.html`,
  `search.html`, and `home.html` (wherever the search box renders). Old Safari
  honours `autocorrect`/`autocapitalize`.
- **Static caching**: serve `/static/*` with `Cache-Control: public,
  max-age=604800` (one week) so the stylesheet/icon aren't refetched every page.
- **Gzip (HTML only)**: a lightweight middleware that gzip-encodes responses
  **only when** the response `Content-Type` is `text/html` and the client sent
  `Accept-Encoding: gzip`. **Streaming proxy responses (`/download`, `/cover`)
  and any Range request are never gzipped** — protecting book import and
  `206`/`Content-Range` behaviour. (Starlette's stock `GZipMiddleware` is *not*
  used, because it would also compress the proxy streams.)
- **Tap targets**: CSS rules under the existing `body.big` class enlarging
  link/menubar/list/sort-control hit areas (min-height + padding). Pure CSS.
- **Does NOT own**: any change to download/cover headers, content, or routing.

## Data Flow

- **SS1**: static — meta tags render on every page from `base.html`; the icon is
  a static file. Add-to-Home-Screen is an OS action; no server flow.
- **SS2**: `/cover/{cid}` → decode+SSRF-resolve URL → `sha256` → cache lookup →
  (hit) disk bytes | (miss) httpx fetch → Pillow inspect → passthrough or
  transcode → write cache → respond. Bytes flow once through memory on a miss
  only.
- **SS4**: `/feed/{fid}?sort=K` → existing fetch+parse+`_to_view_model` →
  in-memory stable sort of the entry list by `K` → render. No upstream change,
  no extra fetch.
- **SS6**: request → (gzip-HTML middleware wraps response) → for `/static`,
  Cache-Control header added → response. Proxy/Range responses pass through
  uncompressed.

## Error Handling

- **SS1**: a missing icon file degrades to a normal favicon-less page (no error).
  CI/test asserts the generated PNG exists and is a valid image so it can't go
  missing silently.
- **SS2**: upstream cover fetch failure keeps the **existing** behaviour — the
  `/cover` route already returns a tiny empty 404 GIF on `RetroShelfError` so a
  broken cover never injects an HTML error page into an `<img>`. Pillow
  decode/transcode failure → fall back to serving the original fetched bytes
  with the original content-type (still better than crashing); if even that
  fails, the existing 404-GIF path catches it. Disk-cache write failure (e.g.
  read-only `/cache`) is non-fatal: log and serve the in-memory bytes.
- **SS4**: an unknown/garbage `sort` value is treated as "default" (no error,
  upstream order). Sorting never drops or duplicates entries (stable sort over
  the existing list).
- **SS6**: gzip is skipped on any response whose content-type isn't `text/html`
  or when `Accept-Encoding` lacks gzip — so worst case is "no compression", never
  a corrupted stream. Cache-Control is additive headers only.

## Open Questions

- **Cover cache eviction**: the disk cache grows unbounded (covers are tiny, KBs
  each, so this is low-risk for a personal LAN library). YAGNI for v1; a simple
  size/age cap can be added later. Flagged, not blocking.
- **Pillow wheel on `python:3.12-slim`**: modern Pillow ships self-contained
  manylinux wheels (bundled libjpeg/zlib), so `pip install Pillow` needs no apt
  packages. The SS2 verification must confirm the image still builds; if a source
  build is ever forced, `libjpeg`/`zlib` dev headers would be needed (documented,
  not expected).

## Approaches Considered

- **Selected — per-feature sub-specs extending existing seams.** Lowest risk,
  matches the codebase's small-focused-files style, naturally parallelisable for
  the dark factory.
- **Single bundled change.** Rejected: couples four unrelated concerns into one
  reviewable unit, harder to test/verify in isolation, no dark-factory benefit.
- **SS2 alternative — stdlib-only, skip WebP.** Rejected during brainstorm (user
  chose Pillow transcode-when-needed): zero deps but WebP covers simply wouldn't
  display and oversized covers wouldn't be tamed.
- **SS4 alternative — upstream-aware/global sort.** Rejected: OPDS exposes no
  standard sort facet across Kavita/Gutenberg/ManyBooks, and we never hold the
  whole library; current-page sort is the honest, universal win.

## Sub-spec Decomposition (dark-factory map)

Each sub-spec is independently buildable and verifiable. SS2 should land before
SS1 (SS1's icon generator uses Pillow). SS4 and SS6 are independent of all
others. SS6's CSS and SS1's `<head>` both touch shared files but not the same
lines (different files / regions).

| ID | Title | Primary files | Depends on |
|----|-------|---------------|------------|
| SS1 | Home-screen web app | `app/templates/base.html`, `app/static/icons/apple-touch-icon-180.png`, `tools/make_icon.py` | SS2 (Pillow) |
| SS2 | Cover transcoding + cache | `app/download.py`, `app/config.py`, `app/main.py`, `requirements.txt` | — |
| SS4 | Current-page sort | `app/main.py`, `app/templates/feed.html`, `app/static/app.css` | — |
| SS6 | Polish bundle | `app/main.py`, `app/templates/{feed,search,home}.html`, `app/static/app.css` | — |

### Acceptance criteria

**SS1**
- Every rendered page's `<head>` contains `apple-mobile-web-app-capable`,
  `apple-mobile-web-app-status-bar-style`, `apple-mobile-web-app-title`, an
  `apple-touch-icon` link, and an `icon` link, all pointing at `/static/...`.
- `app/static/icons/apple-touch-icon-180.png` exists, is a valid 180×180 PNG.
- A test asserts the meta tags render and the icon file is a valid image.

**SS2**
- A WebP cover from upstream is served to the client as `image/jpeg`.
- An oversized (e.g. 1500px) cover is downscaled so `max(w,h) == MAX_EDGE`.
- A small JPEG/PNG cover is served byte-identical (passthrough), correct type.
- A second request for the same cover is served from `{cache_dir}/covers/` with
  no upstream fetch (assert via a transport that fails on a 2nd hit, or a call
  counter).
- The Kavita `apiKey` never appears in the response or in cache filenames.
- `requirements.txt` includes `Pillow`; the Docker image still builds.
- Existing `/cover` 404-GIF-on-error behaviour preserved.

**SS4**
- `/feed/{fid}?sort=author` returns the page's book entries ordered by author
  (casefold, empty last); nav entries unmoved and grouped first.
- `/feed/{fid}` with no `sort` (or an invalid value) returns upstream order.
- Sort is stable and never adds/drops entries (count unchanged).
- `feed.html` renders the sort control only when book entries exist; pager
  links preserve the active `sort`.

**SS6**
- The `q` search input carries `autocorrect="off"`/`autocapitalize="off"` in
  feed, search, and home templates.
- A `GET /static/app.css` response has `Cache-Control: public, max-age=604800`.
- An HTML page response with `Accept-Encoding: gzip` is gzip-encoded; a
  `/download/...` or `/cover/...` response (or any Range request) is **not**
  gzip-encoded and retains correct streaming/Range headers.
- `body.big` CSS enlarges tap targets (assert rule presence).

### Verification commands

- Native test suite (authoritative): `.venv/bin/python -m pytest -q`
- Byte-compile check: `.venv/bin/python -m compileall -q app`
- Docker build (SS2 image sanity, operator/CI step):
  `docker build -t retroshelf:qol .`
- Icon validity (SS1): `.venv/bin/python -c "from PIL import Image;
  im=Image.open('app/static/icons/apple-touch-icon-180.png'); im.verify();
  print(im.size)"`

## Next Steps
- [ ] Turn this design into a Forge spec (`/forge docs/plans/2026-06-08-ipad-qol-pack-design.md`) — produces the dark-factory 3.0 spec with `sub_spec_id` blocks SS1/SS2/SS4/SS6.
- [ ] Build via `/forge-dark-factory` once the spec is generated.
