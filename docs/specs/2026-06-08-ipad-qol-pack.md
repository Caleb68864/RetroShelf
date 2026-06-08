# Spec: iPad QoL Pack

## Meta
- Client: Logic Nebraska (internal)
- Project: RetroShelf
- Repo: /home/caleb/Projects/Kavita-Retro-iPad
- Date: 2026-06-08
- Author: Caleb Bennett + Forge
- Quality Score: 28/30
  - Outcome: 5/5
  - Scope: 5/5
  - Decision guidance: 4/5
  - Edges: 4/5
  - Criteria: 5/5
  - Decomposition: 5/5
- Status: draft
- Design: docs/plans/2026-06-08-ipad-qol-pack-design.md

## Outcome
Four quality-of-life improvements ship to RetroShelf, each within the iOS
5.1.1–12 Mobile Safari envelope (server-rendered, no JavaScript, conservative
CSS, same-origin assets): (1) the site installs to the iPad home screen and
launches fullscreen via web-app meta tags + an apple-touch-icon; (2) book covers
that arrive as WebP or oversized images are transcoded server-side to baseline
JPEG (downscaled) and disk-cached, so they render on old Safari and load fast;
(3) feed pages offer `?sort=title|author|format` links that reorder the books on
the current page only, honestly labelled; (4) a polish bundle adds iOS keyboard
hints to the search box, a one-week `Cache-Control` on `/static`, HTML-only
gzip, and larger tap targets in large-print mode. Done = all four land,
`.venv/bin/python -m pytest -q` passes (existing 133 tests + new ones),
`.venv/bin/python -m compileall -q app` is clean, and the Docker image still
builds.

## Intent
Trade-Off Hierarchy:
1. **iPad compatibility over modern convenience** — never introduce JS, CSS Grid,
   unprefixed flexbox for layout, web fonts, or external assets. If a "nicer"
   technique isn't safe on iOS 5–12 Safari, do not use it.
2. **Correctness/safety over feature richness** — never weaken the SSRF guard,
   never buffer whole book/file streams, never let the Kavita apiKey reach the
   client or land in plaintext on disk.
3. **Follow existing RetroShelf patterns over inventing new ones** — extend the
   seams already present (templates, `app/download.py`, the `/feed` route, a
   middleware + CSS) rather than adding new abstraction layers.
4. **Honest UX over impressive-but-misleading** — current-page sort must be
   labelled as such, not implied to be whole-library.

Decision Boundaries — stop and escalate if:
- A change would require JavaScript, CSS Grid, or breaking the no-buffer
  streaming rule to satisfy a criterion.
- Adding Pillow forces apt/system-library changes to the Dockerfile (the
  manylinux wheel does NOT install cleanly on `python:3.12-slim`).
- Any existing test must be weakened or deleted to make a new one pass.
Decide autonomously for everything else.

## Context
RetroShelf is a single FastAPI app (`app/`) with Jinja2 templates
(`app/templates/`) and one static stylesheet (`app/static/app.css`). It bridges a
Kavita (or other OPDS) library to very old iPads. Hard constraints live in
`vault/Build Constraints.md`: no JS (mandatory), no CSS Grid (pre iOS 10.3),
unprefixed flexbox unreliable pre iOS 9, same-origin CSS + system fonts only,
books streamed (never buffered), SSRF choke point in `app/kavita.py: resolve_url`,
apiKey held server-side only.

Relevant current code:
- `app/templates/base.html` `<head>` sets charset, viewport, the stylesheet link,
  and an OPDS `<link rel=alternate>`. No apple-touch-icon / web-app meta yet.
- `app/static/app.css` is already conservative (no `var()`, grid, `gap`,
  `sticky`). Body carries `color-{amber|green|white}` and optional `big` class.
- `app/download.py: stream_cover()` proxies the upstream cover and copies the
  upstream `Content-Type` through (so WebP reaches the iPad as WebP). `app/main.py`
  `/cover/{cid}` returns a tiny empty 404 GIF on error.
- `app/main.py` `/feed/{fid}` builds `entries` via `_to_view_model`, then computes
  `next_url`/`prev_url` from the upstream feed's own `rel=next/prev` — pagination
  is upstream-driven; only the current page's entries are in hand.
- `app/config.py` exposes env config (`state_dir` default `/config`); a `/cache`
  volume is mounted but no `cache_dir` config exists yet.
- The `q` search input renders in `feed.html`, `search.html`, `home.html`.

Build/test commands (from `forge-project.json`):
- Test: `.venv/bin/python -m pytest -q`
- Build/byte-compile: `.venv/bin/python -m compileall -q app`

Dependency/ordering note: sub-specs that touch the same file are serialized via
`depends_on` so the factory never edits a shared file in two parallel workers.
SS-01 (covers) installs Pillow, which SS-02's icon generator needs. SS-03 and
SS-04 both touch `app/main.py`, `app/templates/feed.html`, `app/static/app.css`,
and `tests/test_app.py`, so SS-04 depends on SS-03. SS-01, SS-03 also share
`app/main.py`, so SS-03 depends on SS-01. SS-02's files are disjoint from SS-03's,
so they may run in parallel after SS-01.

## Requirements
1. REQ-001: WebP (and other non-JPEG/PNG) covers are transcoded to baseline JPEG
   so iOS 5–12 Safari can render them.
2. REQ-002: Oversized covers are downscaled to a bounded max edge before serving.
3. REQ-003: Small JPEG/PNG covers are served byte-identical (passthrough).
4. REQ-004: Repeated cover requests are served from a disk cache on `/cache`
   without re-fetching upstream or re-running Pillow.
5. REQ-005: The Kavita apiKey never appears in cover responses or cache filenames.
6. REQ-006: Existing `/cover` 404-GIF error behaviour is preserved; cover handling
   degrades gracefully (passthrough) if Pillow is unavailable.
7. REQ-007: Every page advertises Add-to-Home-Screen via web-app meta tags + a
   same-origin apple-touch-icon.
8. REQ-008: The apple-touch-icon is a valid 180×180 same-origin PNG generated by a
   committed, reproducible script.
9. REQ-009: A feed page accepts `?sort=title|author|format` and stably reorders the
   current page's book entries (nav entries grouped first, original order);
   absent/invalid sort preserves upstream order.
10. REQ-010: The feed sort control renders (no JS, plain links) only when book
    entries exist, marks the active sort, carries the sort into pager links, and is
    labelled "this page".
11. REQ-011: The search input suppresses iOS auto-correct/auto-capitalize.
12. REQ-012: `/static/*` responses carry `Cache-Control: public, max-age=604800`.
13. REQ-013: HTML responses are gzipped when the client accepts gzip; streaming
    proxy responses (`/download`, `/cover`) and Range requests are never gzipped.
14. REQ-014: Large-print mode (`body.big`) enlarges tap targets.
15. REQ-015: No JS, CSS Grid, web fonts, or external assets are introduced; the
    existing suite still passes and the image still builds.

## Sub-Specs

---
sub_spec_id: SS-01
phase: run
depends_on: []
---

### 1. Cover transcoding + disk cache (design SS2)

- **Scope:** Make cover serving inspect and, when needed, transcode/downscale
  covers, with a disk cache. Add Pillow to dependencies. Add config: `cache_dir`
  (env `CACHE_DIR`, default `/cache`), `cover_max_edge` (default 320),
  `cover_jpeg_quality` (default 80). Algorithm in `stream_cover` (or a helper it
  calls): (a) `key = sha256(upstream_url)`; if `{cache_dir}/covers/{key}` exists,
  serve it (no fetch, no Pillow); (b) else fetch the cover fully into memory
  (covers are small — buffering allowed here, unlike book streams); (c) inspect
  with Pillow — passthrough original bytes when format ∈ {JPEG, PNG} and
  `max(w,h) ≤ cover_max_edge`; (d) otherwise downscale to `cover_max_edge`
  (preserve aspect) and re-encode baseline JPEG; (e) write served bytes +
  content-type to the disk cache, then return. Drop Range for covers (serve full
  200). If Pillow import/decode fails, fall back to serving the fetched bytes with
  the upstream content-type. Wire `cache_dir` from config into the `/cover` route.
- **Files (modify):**
  - `app/download.py`
  - `app/config.py`
  - `app/main.py`
  - `requirements.txt`
- **Files (new):**
  - `tests/test_covers.py`
- **Acceptance criteria:**
  - [MECHANICAL] `.venv/bin/python -m pytest -q tests/test_covers.py` exits 0.
  - [MECHANICAL] `.venv/bin/python -c "import PIL; print(PIL.__version__)"` exits 0
    and `Pillow` appears in `requirements.txt`.
  - [STRUCTURAL] `app/config.py` defines `cache_dir` (env `CACHE_DIR`, default
    `/cache`), `cover_max_edge`, and `cover_jpeg_quality`.
  - [BEHAVIORAL] A synthetic WebP cover routed through the cover path yields a
    response `Content-Type` of `image/jpeg`.
  - [BEHAVIORAL] An oversized (e.g. 1500px) image is served downscaled so
    `max(width,height) == cover_max_edge`.
  - [BEHAVIORAL] A small JPEG is served byte-identical to the input (passthrough).
  - [BEHAVIORAL] Requesting the same cover twice, with a transport that fails on a
    2nd upstream hit, proves the 2nd request is served from `{cache_dir}/covers/`.
  - [STRUCTURAL] No cache filename contains the apiKey; apiKey absent from response
    headers/body.
  - [HUMAN REVIEW] Confirm covers are buffered but `stream_download` book/file
    streaming remains unbuffered (no regression to the no-buffer rule).

---
sub_spec_id: SS-02
phase: run
depends_on: ['SS-01']
---

### 2. Home-screen web app meta + apple-touch-icon (design SS1)

- **Scope:** In `app/templates/base.html` `<head>` add
  `apple-mobile-web-app-capable=yes`,
  `apple-mobile-web-app-status-bar-style=black`,
  `apple-mobile-web-app-title=RetroShelf`, `<link rel="apple-touch-icon"
  href="/static/icons/apple-touch-icon-180.png">`, a matching `<link rel="icon">`,
  and `-webkit-text-size-adjust:100%` if not already present. Add a committed
  generator `tools/make_icon.py` (uses Pillow — provided by SS-01) that renders a
  180×180 baseline PNG in the amber-on-black retro aesthetic; run it and commit the
  generated `app/static/icons/apple-touch-icon-180.png`. No route logic; no Web App
  Manifest (out of scope).
- **Files (new):**
  - `tools/make_icon.py`
  - `app/static/icons/apple-touch-icon-180.png`
  - `tests/test_webapp_meta.py`
- **Files (modify):**
  - `app/templates/base.html`
- **Acceptance criteria:**
  - [STRUCTURAL] `app/static/icons/apple-touch-icon-180.png` exists.
  - [MECHANICAL] `.venv/bin/python -c "from PIL import Image; i=Image.open('app/static/icons/apple-touch-icon-180.png'); i.verify(); assert i.size==(180,180)"` exits 0.
  - [BEHAVIORAL] A rendered page's HTML contains `apple-mobile-web-app-capable`,
    `apple-mobile-web-app-title`, and an `apple-touch-icon` link pointing at
    `/static/` (asserted in `tests/test_webapp_meta.py`).
  - [MECHANICAL] `.venv/bin/python -m pytest -q tests/test_webapp_meta.py` exits 0.
  - [HUMAN REVIEW] Confirm the icon reads acceptably at small size, matches the
    retro aesthetic, and uses no external font/asset.

---
sub_spec_id: SS-03
phase: run
depends_on: ['SS-01']
---

### 3. Current-page sort (design SS4)

- **Scope:** In `app/main.py` `/feed/{fid}`, accept `sort` ∈
  {`title`,`author`,`format`} (absent/other = upstream order). After building
  `entries`, stably reorder: navigation entries (`is_nav=True`) keep original order
  and stay grouped first; book entries sort by the chosen key (title/author
  casefolded, empty author last; format groups EPUB before PDF). Carry the active
  `sort` into `next_url`/`prev_url`. In `app/templates/feed.html`, render a sort
  control row (plain `<a href>` links, no JS) only when book entries exist —
  `Sort: [Default] [Title] [Author] [Format]` — marking the active one and showing
  a small "sorted: this page" caption when active. Style in `app/static/app.css`
  with existing classes, block layout, large tap targets.
- **Files (modify):**
  - `app/main.py`
  - `app/templates/feed.html`
  - `app/static/app.css`
  - `tests/test_app.py`
- **Acceptance criteria:**
  - [BEHAVIORAL] `/feed/{fid}?sort=author` renders book entries ordered by author;
    nav entries remain first and unmoved.
  - [BEHAVIORAL] `/feed/{fid}` (no sort) and `?sort=bogus` both preserve upstream
    order.
  - [STRUCTURAL] The rendered feed page contains sort-control links with `?sort=`
    preserving the feed path, and the active one marked.
  - [MECHANICAL] `.venv/bin/python -m pytest -q tests/test_app.py` exits 0.
  - [MECHANICAL] `grep -nE "var\(--|display:[ ]*grid" app/static/app.css` returns
    no matches (CSS stays old-Safari-safe).

---
sub_spec_id: SS-04
phase: run
depends_on: ['SS-03']
---

### 4. Polish bundle (design SS6)

- **Scope:** (a) Add `autocorrect="off" autocapitalize="off" autocomplete="off"
  spellcheck="false"` to the `q` search input in `feed.html`, `search.html`,
  `home.html`. (b) Serve `/static/*` with `Cache-Control: public,
  max-age=604800`. (c) Add a lightweight middleware in `app/main.py` that
  gzip-encodes responses ONLY when the response `Content-Type` is `text/html` and
  the request sent `Accept-Encoding: gzip`; it must never compress `/download` or
  `/cover` responses or any Range request (do NOT use Starlette's stock
  `GZipMiddleware`). (d) Add CSS under `body.big` enlarging tap targets (min-height
  + padding on menubar/list/prefs/sort links).
- **Files (modify):**
  - `app/main.py`
  - `app/templates/feed.html`
  - `app/templates/search.html`
  - `app/templates/home.html`
  - `app/static/app.css`
  - `tests/test_app.py`
- **Acceptance criteria:**
  - [STRUCTURAL] The `q` input in feed/search/home templates carries
    `autocorrect="off"` and `autocapitalize="off"`.
  - [BEHAVIORAL] `GET /static/app.css` returns `Cache-Control: public,
    max-age=604800`.
  - [BEHAVIORAL] An HTML page request with `Accept-Encoding: gzip` returns
    `Content-Encoding: gzip`.
  - [BEHAVIORAL] A `/download/...` (or `/cover/...`) response is NOT gzip-encoded
    and retains correct streaming/Range headers.
  - [STRUCTURAL] `app/static/app.css` contains `body.big` (or `.big`) rules
    enlarging tap-target size.
  - [MECHANICAL] `.venv/bin/python -m pytest -q tests/test_app.py` exits 0.

## Edge Cases
- **Upstream cover fetch fails**: keep the existing `/cover` path — return the
  tiny empty 404 GIF (never an HTML error page in an `<img>`).
- **Pillow unavailable / decode error**: fall back to serving the fetched bytes
  with the upstream content-type; if that also fails, the existing 404-GIF path
  catches it. Cover handling must not 500.
- **Read-only `/cache`**: a disk-cache write failure is non-fatal — log and serve
  the in-memory bytes.
- **Unknown/garbage `?sort` value**: treated as "default" (upstream order), no
  error; sort never drops or duplicates entries.
- **Feed page mixing nav + book entries**: nav entries stay grouped first in
  original order; only book entries are sorted.
- **gzip + Range/streaming**: gzip is skipped for non-HTML content types and for
  `/download` `/cover` and Range requests, protecting iBooks import and
  `206`/`Content-Range`.
- **HEAD /cover**: existing HEAD behaviour (200 headers-only) is preserved.

## Out of Scope
- Whole-library or upstream-driven sort / A–Z jump (infeasible: pagination is
  upstream-driven). Current-page sort only.
- Web App Manifest / PWA / service workers (ignored by iOS 5–12 Safari).
- Cover cache eviction / size cap (covers are KBs; unbounded acceptable for v1).
- Any change to book/file download headers, content, or the streaming model.
- Any JavaScript, CSS Grid, flexbox-based core layout, web fonts, external assets.
- Cookie-based access-key login, "recently viewed" shelf, A–Z index (considered in
  brainstorming, not in this pack).

## Constraints
**Musts:**
- Stay within iOS 5.1.1–12 Safari: server-rendered HTML, `<a href>`/GET forms
  only, conservative CSS (`vault/Build Constraints.md`).
- Covers may be buffered (small); book/file streams must remain unbuffered.
- Preserve the SSRF guard and apiKey secrecy (no apiKey in responses or cache
  filenames).
- Add new tests for each sub-spec; keep all existing tests passing.

**Must-Nots:**
- No JavaScript, CSS Grid, unprefixed flexbox for layout, web fonts, CDN, or icon
  libraries.
- Do not gzip or buffer `/download` or `/cover` streaming responses.
- Do not weaken or delete an existing test to pass a new one.

**Preferences:**
- Prefer extending existing seams over new abstractions.
- Prefer stdlib + already-present deps; Pillow is the only new dependency and must
  install via its manylinux wheel (no Dockerfile apt changes expected).
- Prefer baseline JPEG (universally safe on old Safari) over progressive.

**Escalation Triggers:**
- Pillow needs apt/system libs on `python:3.12-slim`: stop and surface.
- A criterion can't be met without JS or breaking streaming: stop and surface.

## Verification
1. `.venv/bin/python -m pytest -q` — full suite green (existing 133 + new tests).
2. `.venv/bin/python -m compileall -q app` — clean byte-compile.
3. `.venv/bin/python -c "from PIL import Image; i=Image.open('app/static/icons/apple-touch-icon-180.png'); i.verify(); print(i.size)"` → `(180, 180)`.
4. `grep -nE "var\(--|display:[ ]*grid" app/static/app.css` → no matches.
5. Docker image still builds: `docker build -t retroshelf:qol .` (operator step).
6. Manual smoke (operator): load a feed on a WebP-cover source — covers render;
   tap a sort link — current page reorders; view source — head has
   apple-touch-icon + web-app meta.
