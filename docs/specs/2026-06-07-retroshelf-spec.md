# Spec: RetroShelf — Kavita→iBooks Bridge for Old iPads

## Meta
- Client: Internal / Logic Nebraska
- Project: RetroShelf
- Repo: /home/caleb/Projects/Kavita-Retro-iPad
- Date: 2026-06-07
- Author: Caleb Bennett + Forge
- Quality Score: 33/35
  - Outcome: 5/5
  - Scope: 5/5
  - Decision guidance: 5/5
  - Edges: 5/5
  - Criteria: 5/5
  - Decomposition: 5/5
  - Purpose: 3/5
- Status: ready

## Outcome
A running Docker-hosted FastAPI + Jinja2 web service ("RetroShelf") that lets old iPads (iOS 5.1.1–12) browse a Kavita ebook library in Safari with **no JavaScript** and import EPUB/PDF into iBooks/Apple Books. The bridge proxies Kavita's OPDS feed server-side and streams books with the exact HTTP headers iOS needs for the "Open in iBooks" hand-off.

## Intent
The old iPad is a dumb glass book selector; the server does all the work. Build a **Kavita→iBooks import bridge, not an online reader**. Every core flow works with plain `<a href>` links and server-rendered HTML. When something fails, fail loudly with a debuggable, masked log line and a friendly error page — never a silent blank.

## Context
Grounded in adversarially-verified research in `vault/` (see `vault/Build Constraints.md`, `vault/Corrected Assumptions.md`, `vault/Decisions Index.md`). Key verified facts driving the build:
- iOS 5–12 Safari has **no download manager / no Save As**. PDF renders inline (ignores `Content-Disposition: attachment`); EPUB (`application/epub+zip`) triggers an "Open in iBooks" hand-off. Safari derives the saved extension from **Content-Type**, and the base name from the **URL path** — so set correct MIME *and* a correct-extension URL *and* a `filename`.
- **No JS** of any kind (no fetch <10.3, no service workers <11.3, no `download` attr <13). Plain links + GET forms only.
- Basic Auth prompts suppressed since iOS 11.2 → hold apiKey server-side, single same-origin surface.
- OPDS 1.x = Atom; acquisition links have `rel` starting `http://opds-spec.org/acquisition`, `type`=media type, `href`=file; detect nav vs acquisition structurally (kind param is advisory).
- Kavita root `GET /api/opds/{apiKey}`; **feed hrefs are root-relative absolute paths** → resolve against Kavita *origin*. Covers at `/api/image/*-cover?...&apiKey=` (query param). Downloads stream original file with format MIME.
- Use httpx streaming + Starlette `StreamingResponse` (NOT FileResponse); lifespan `AsyncClient` with `Timeout(read=None)`; close upstream via BackgroundTask.
- Old Safari CSS: no Grid; modern flexbox unreliable <iOS 9 → single-column block layout, system fonts, one same-origin stylesheet, no CDN.

## Infrastructure
- Python 3.12 in container (`python:3.12-slim`); local dev venv is Python 3.14 at `.venv/`.
- Deps: fastapi, uvicorn[standard], httpx, jinja2, defusedxml; pytest, pytest-asyncio (dev). (NO python-multipart — GET-forms only, per [M-3].)
- **No Docker daemon in the dev environment** — verify natively (venv import + pytest + uvicorn smoke); Dockerfile/compose validated statically (parse + content checks).
- Tests live in `tests/`; run with `.venv/bin/python -m pytest -q`.
- Storage volumes: `/config`, `/cache`; host root `/srv/docker_data/kavita-ibooks-bridge`. Listen `0.0.0.0:8099`.

## Requirements
1. Server-rendered HTML + one static CSS file; **zero JavaScript**; all flows via `<a href>` and `method="GET"` forms.
2. Browse Kavita OPDS (root, nested navigation, acquisition feeds, pagination, search) rendered as old-Safari HTML.
3. Stream EPUB with `Content-Type: application/epub+zip`, `Content-Disposition: attachment; filename="<safe>.epub"`.
4. Stream PDF with `Content-Type: application/pdf`, `Content-Disposition: inline; filename="<safe>.pdf"` (configurable).
5. Always set `Content-Disposition` with a sanitized ASCII filename + correct extension; `X-Content-Type-Options: nosniff`; relay `Content-Length`/`Accept-Ranges`/Range/206 when upstream provides.
6. apiKey held server-side, never rendered in HTML; downloads proxied through the bridge; opaque internal ids.
7. Config via env: `KAVITA_BASE_URL`, `KAVITA_OPDS_URL` (required), plus `APP_PORT`, `BRIDGE_PUBLIC_URL`, `BRIDGE_ACCESS_KEY`, `BRIDGE_ID_SECRET` (stable HMAC seed, per [M-2]), `ALLOWED_IPS`, `SHOW_COVERS` (default true), `CACHE_FEEDS_SECONDS`, `CACHE_BOOKS`, `LOG_LEVEL`, `PDF_DISPOSITION`, `EPUB_DISPOSITION`, `TZ`, `PUID`, `PGID`.
8. Optional access key (`/?key=` / header) and optional IP allowlist; both off by default. No cookies, no JS login, no Basic Auth.
9. SSRF guard: only fetch URLs whose origin == configured Kavita origin. Path-traversal-safe filenames.
10. Docker image (`python:3.12-slim`, non-root, HEALTHCHECK→/health) + Portainer-compatible `docker-compose.yml` + README.

## Red-Team Amendments (AUTHORITATIVE — overrides sub-spec text on any conflict)
Verified against the live `.venv` (Python 3.14; fastapi 0.136.3, **starlette 1.2.1**, httpx 0.28.1, pydantic 2.13.4, defusedxml 0.7.1). Apply ALL of these.

- **[C-1 / SS-07] `/health` must be plain text.** FastAPI returning `"ok"` yields JSON `"ok"` with `application/json`. Use `from starlette.responses import PlainTextResponse` → `PlainTextResponse("ok")`. Test asserts `r.text == "ok"` AND `r.headers["content-type"].startswith("text/plain")`. The Dockerfile HEALTHCHECK must match.
- **[C-2 / SS-03] SSRF origin check, pinned.** Resolve with `kavita_origin.join(href)` then accept ONLY if `(scheme, host, port-with-default-80/443)` equals the Kavita origin after normalizing implicit ports (`httpx.URL.port` is `None` for default ports). Explicitly REJECT: protocol-relative hrefs (`//host`), absolute-scheme hrefs (`http://…`, `https://…` to a different origin), backslash forms, and anything that isn't a single leading-`/` absolute path. Negative ACs required: `resolve_url("//evil.com/x")`, `resolve_url("https://evil.com/x")`, `resolve_url("\\\\evil.com")` each raise `SsrfError`.
- **[C-3 / SS-05+SS-07] Re-validate decoded ids.** Signing proves "we issued it," not "it is safe." EVERY `decode_id()` result MUST be re-passed through `kavita.resolve_url()` (origin check) BEFORE any fetch/stream.
- **[C-4 / SS-03+SS-07] apiKey masking is unconditional on user-visible surfaces.** The apiKey is embedded in every OPDS path segment (`/api/opds/{apiKey}/…`). Error pages, `KavitaError.__str__`, and any HTTP response body MUST mask the apiKey path segment even when `LOG_LEVEL=debug` (debug only relaxes *log* verbosity, never response bodies).
- **[C-5 / SS-07] Add an `[INTEGRATION]` AC:** mocked acquisition feed → parse → encode every nav/acquisition/cover href to a bridge id → render → the rendered `/download/{id}…` link decodes back to the original Kavita URL and streams mocked bytes with correct headers.
- **[C-6 / SS-05+SS-06+SS-07] Cover-image proxy route is REQUIRED.** Covers live at a different path with the key as a QUERY param: `{kavita_origin}/api/image/...-cover?...&apiKey={apiKey}`. Add `GET /cover/{id}` that decodes a bridge id → that URL → streams via the SSRF-guarded client with `Content-Type` from upstream. Templates use the bridge `/cover/{id}` id, NEVER the upstream URL. AC: the apiKey appears in NO `<img src>` (grep-asserted). Covers are config-toggle-able and degrade gracefully if absent.
- **[C-7 / SS-01] `pytest.ini` must neutralize the Starlette TestClient deprecation.** Starlette 1.2.1 raises `StarletteDeprecationWarning` on `TestClient` import. `pytest.ini` sets `filterwarnings` to NOT error on it (e.g. `ignore::DeprecationWarning` scoped, or no `error` default). Starlette 1.x is intentional.
- **[H-1 / SS-05+SS-06] Extension-bearing download URL is REQUIRED (not optional)** for the primary link: route shape `GET /download/{id}/{filename}` where `{filename}` ends in `.epub`/`.pdf` (old Safari names the saved file from the URL path). Keep `/download/{id}` and `/open/{id}` as aliases; CD `filename` stays as the secondary signal.
- **[H-2 / SS-07] Name the rewrite seam:** `app/main.py::_to_view_model(feed)` calls `ids.encode_id` on every navigation, acquisition, AND cover href, producing template-ready bridge ids. AC asserts it is invoked for all three link kinds.
- **[H-3 / SS-02] Pin an explicit `Acquisition` type** (`@dataclass Acquisition(media_type: str, href: str, rel: str)`). `Entry.acquisitions: list[Acquisition]`. SS-04/SS-06 reference this exact type.
- **[H-4 / SS-05] Stream-cleanup AC:** simulate a client disconnect / mid-stream break; assert the upstream response `aclose` runs (spy/mock) so the pooled `AsyncClient` doesn't leak connections.
- **[H-5 / SS-07] Feed cache contract:** cache key = the bridge `id` (NOT the apiKey-bearing URL); bounded (simple TTL dict with a max-entries cap); cached bodies are never logged.
- **[H-6 / SS-04+SS-09] `ALLOWED_IPS` is direct-LAN only** (uses `request.client.host`; not proxy/XFF-aware). Document this clearly in README + a code comment; do not pretend it works behind a reverse proxy.
- **[M-2 / SS-04] id secret stability:** seed the HMAC key from a stable `BRIDGE_ID_SECRET` env (fallback to a per-process random with a logged warning) so bookmarked `/download/{id}` links survive container restarts.
- **[M-3 / SS-01] Drop `python-multipart`** — GET-forms-only, no uploads; remove from requirements.
- **[M-4 / SS-08] `will-create: tests/validate_docker.py`** explicitly (a runnable script, not a `test_`-prefixed pytest module).
- **[M-7 / SS-07] `/health` and `/static/*` bypass the access-key + IP-allowlist middleware** (else the container HEALTHCHECK gets 403 when a key is configured).
- **[L-2 / SS-05] Use `Cache-Control: no-store` on the streamed download/cover responses** (avoid old Safari serving a stale partial after a broken stream).

## Sub-Specs
<!-- All implementation sub-specs are pipeline phase `run`; dependency DAG via depends_on. -->

---
sub_spec_id: SS-01
phase: run
depends_on: []
dispatch: factory
---
### 1. Project skeleton, packaging & config module
- **Scope:** Create the `app/` package, `requirements.txt`, `requirements-dev.txt`, `pytest.ini`, and `app/config.py` (typed env parsing with validation, defaults, secret masking, Kavita origin derivation). Establish `tests/`.
- **Files likely touched:** `will-create: app/__init__.py`, `will-create: app/config.py`, `will-create: requirements.txt`, `will-create: requirements-dev.txt`, `will-create: pytest.ini`, `will-create: tests/__init__.py`, `will-create: tests/test_config.py`
- **Acceptance criteria:**
1. `[MECHANICAL]` `.venv/bin/python -m pytest tests/test_config.py -q` passes.
2. `[STRUCTURAL]` `requirements.txt` lists fastapi, uvicorn[standard], httpx, jinja2, defusedxml (NO python-multipart); `requirements-dev.txt` adds pytest, pytest-asyncio. `pytest.ini` sets `filterwarnings` so the Starlette 1.x `TestClient` DeprecationWarning does NOT error the suite (per [C-7]).
3. `[BEHAVIORAL]` `app/config.py` `load_config()` reads env, requires `KAVITA_OPDS_URL`/`KAVITA_BASE_URL`, derives Kavita origin (scheme+host+port), exposes `PDF_DISPOSITION`(default inline)/`EPUB_DISPOSITION`(default attachment), `CACHE_FEEDS_SECONDS`(300), `CACHE_BOOKS`(false), access key + allowlist (optional), and a `mask_secret()` helper that never returns the full apiKey unless `LOG_LEVEL=debug`.
4. `[STRUCTURAL]` Missing required env raises a typed `ConfigError` with a clear message (no traceback leak).
- **Depends on:** none
- **Constraints:** No network. Pure stdlib + pydantic optional. Secrets masked.
- **Escalation triggers:** none

## Tasks
- 01.1 — package + requirements + pytest config
- 01.2 — config.py env parsing + masking + origin derivation
- 01.3 — tests/test_config.py

---
sub_spec_id: SS-02
phase: run
depends_on: [SS-01]
dispatch: factory
---
### 2. OPDS parser
- **Scope:** `app/opds.py` — parse OPDS 1.x Atom with `defusedxml.ElementTree` into typed objects: `Feed(title, entries, nav_links, next_url, prev_url, search_url)` and `Entry(title, author, summary, updated, acquisitions[], cover_url, is_navigation)`. Acquisition = link with `rel` startswith `http://opds-spec.org/acquisition`; capture `type` + `href`. Detect navigation entries structurally. Handle namespaces, missing fields, pagination (`rel=next/previous`), search (`rel=search` / OpenSearch).
- **Files likely touched:** `will-create: app/opds.py`, `will-create: tests/test_opds.py`, `will-create: tests/fixtures/opds_root.xml`, `will-create: tests/fixtures/opds_acquisition.xml`
- **Acceptance criteria:**
1. `[MECHANICAL]` `.venv/bin/python -m pytest tests/test_opds.py -q` passes.
2. `[STRUCTURAL]` Fixtures `tests/fixtures/opds_root.xml` (navigation feed) and `tests/fixtures/opds_acquisition.xml` (acquisition feed with one EPUB + one PDF entry, covers, authors, pagination) exist.
3. `[BEHAVIORAL]` Parser extracts EPUB (`application/epub+zip`) and PDF (`application/pdf`) acquisition hrefs + types, author names, cover urls, and next/prev pagination; classifies navigation vs acquisition entries correctly; uses defusedxml (no external entity expansion).
4. `[BEHAVIORAL]` Malformed XML raises a typed `OpdsParseError` (caught upstream), not a raw ElementTree exception.
- **Depends on:** SS-01
- **Constraints:** defusedxml only; no network; namespace-robust.
- **Escalation triggers:** none

## Tasks
- 02.1 — Feed/Entry models
- 02.2 — defusedxml parsing + rel/type extraction + pagination/search
- 02.3 — fixtures + tests

---
sub_spec_id: SS-03
phase: run
depends_on: [SS-01]
dispatch: factory
---
### 3. Kavita httpx client + streaming + origin resolution
- **Scope:** `app/kavita.py` — lifespan-managed `httpx.AsyncClient` (`Timeout(connect=10, read=None, write=None, pool=10)`). `fetch_feed(url)` GET → text (raises typed `KavitaError` on non-2xx/timeout). `stream_download(url, range_header)` async context yielding `(upstream_response, aiter_raw)` for proxying, forwarding `Range`. `resolve_url(href)` resolves Kavita root-relative absolute-path hrefs against the configured Kavita **origin**; rejects hrefs whose resolved origin != Kavita origin (SSRF guard).
- **Files likely touched:** `will-create: app/kavita.py`, `will-create: tests/test_kavita.py`
- **Acceptance criteria:**
1. `[MECHANICAL]` `.venv/bin/python -m pytest tests/test_kavita.py -q` passes (using httpx MockTransport / monkeypatch — no real network).
2. `[BEHAVIORAL]` `resolve_url("/api/opds/KEY/libraries")` → `{kavita_origin}/api/opds/KEY/libraries`; an absolute foreign-origin href raises `SsrfError`.
3. `[BEHAVIORAL]` `fetch_feed` raises typed `KavitaError` (with url + status, apiKey masked) on 4xx/5xx/timeout; success returns body text.
4. `[STRUCTURAL]` Uses a single shared AsyncClient created in lifespan (not per-request); streaming uses `client.stream`/`build_request`+`send(stream=True)` and never reads the whole body into memory.
- **Depends on:** SS-01
- **Constraints:** No global client at import time; inject client. Mask apiKey in all errors/logs.
- **Escalation triggers:** none

## Tasks
- 03.1 — AsyncClient factory + timeouts + lifespan hooks
- 03.2 — fetch_feed + stream_download + resolve_url/SSRF guard
- 03.3 — tests with MockTransport

---
sub_spec_id: SS-04
phase: run
depends_on: [SS-01]
dispatch: factory
---
### 4. ID mapping + security helpers
- **Scope:** `app/ids.py` — bidirectional opaque id ↔ upstream URL mapping using HMAC-signed/base64url tokens keyed by a per-process secret (so apiKey/URLs never appear in HTML and forged ids are rejected). `app/security.py` — `sanitize_filename(name, ext)` (strip quotes/slashes/control/`..`, ASCII fallback, force correct extension), `check_access_key(request)`, `check_ip_allowlist(request)`, both no-op when unconfigured.
- **Files likely touched:** `will-create: app/ids.py`, `will-create: app/security.py`, `will-create: tests/test_ids.py`, `will-create: tests/test_security.py`
- **Acceptance criteria:**
1. `[MECHANICAL]` `.venv/bin/python -m pytest tests/test_ids.py tests/test_security.py -q` passes.
2. `[BEHAVIORAL]` `encode_id(url)`→token; `decode_id(token)`→url; a tampered/foreign token raises `BadIdError`. apiKey never present in the token plaintext output rendered to users.
3. `[BEHAVIORAL]` `sanitize_filename('a/b"..\\x00.epub','epub')` → safe ASCII ending `.epub`; never contains `/`, `\`, `..`, quotes, control chars.
4. `[BEHAVIORAL]` access-key + IP-allowlist checks pass-through when env unset; enforce (403) when set.
- **Depends on:** SS-01
- **Constraints:** No DB; stateless signed tokens. Constant-time compare for keys.
- **Escalation triggers:** none

## Tasks
- 04.1 — signed id codec
- 04.2 — sanitize_filename + access/allowlist guards
- 04.3 — tests

---
sub_spec_id: SS-05
phase: run
depends_on: [SS-03, SS-04]
dispatch: factory
---
### 5. Download proxy endpoint + iOS headers
- **Scope:** `app/download.py` — build the `StreamingResponse` for the REQUIRED extension-bearing primary route `/download/{id}/{filename}` (filename ends `.epub`/`.pdf`, per [H-1]) plus aliases `/download/{id}` and `/open/{id}`, AND the REQUIRED cover proxy `GET /cover/{id}` (per [C-6]). Decode id → Kavita URL, RE-VALIDATE via `kavita.resolve_url()` before fetch (per [C-3]), stream via SS-03, set headers per format: EPUB → `application/epub+zip` + `attachment`; PDF → `application/pdf` + disposition from config (default inline); cover → upstream `Content-Type`. Always `Content-Disposition` with sanitized `filename` + correct extension; `X-Content-Type-Options: nosniff`; relay `Content-Length`/`Accept-Ranges`/`Content-Range` + 206 when upstream supplies; `Cache-Control: no-store` (per [L-2]). Close upstream via `BackgroundTask` (cleanup verified on disconnect, per [H-4]).
- **Files likely touched:** `will-create: app/download.py`, `will-create: tests/test_download_headers.py`
- **Acceptance criteria:**
1. `[MECHANICAL]` `.venv/bin/python -m pytest tests/test_download_headers.py -q` passes.
2. `[BEHAVIORAL]` EPUB response: `Content-Type: application/epub+zip` AND `Content-Disposition` contains `attachment` and `filename="...epub"` (never `.epub.zip`/`.zip`).
3. `[BEHAVIORAL]` PDF response: `Content-Type: application/pdf` AND disposition `inline` by default (switchable to attachment via config); `filename="...pdf"`.
4. `[BEHAVIORAL]` `X-Content-Type-Options: nosniff` present; when a client `Range` is sent and upstream returns 206, the bridge relays 206 + `Content-Range`; otherwise streams 200. Body is streamed (generator), not fully buffered.
- **Depends on:** SS-03, SS-04
- **Constraints:** No full-file buffering. Extension belt-and-suspenders (URL + MIME + filename).
- **Escalation triggers:** none

## Tasks
- 05.1 — header builder (epub/pdf/disposition/nosniff/range relay)
- 05.2 — StreamingResponse wiring + BackgroundTask close
- 05.3 — header tests (TestClient + mocked upstream)

---
sub_spec_id: SS-06
phase: run
depends_on: [SS-02, SS-04]
dispatch: factory
---
### 6. HTML rendering, templates & CSS (old-Safari, no JS)
- **Scope:** `app/templates/` (Jinja2) + `app/static/app.css`. Templates: `base.html` (HTML5 doctype, viewport, `-webkit-text-size-adjust:100%`, single-column block layout, links to `/static/app.css`), `home.html`, `feed.html` (breadcrumb/back, nav entries, book entries with EPUB/PDF badges, author, optional covers, prev/next), `book.html` (title/author/format/summary, primary "Open in iBooks/Books" button, back), `search.html`, `help.html`, `error.html`. `app/render.py` configures `Jinja2Templates`. CSS: no Grid, no modern-flexbox-for-layout, large tap targets, system font stack, no CDN/web-fonts.
- **Files likely touched:** `will-create: app/render.py`, `will-create: app/templates/base.html`, `will-create: app/templates/home.html`, `will-create: app/templates/feed.html`, `will-create: app/templates/book.html`, `will-create: app/templates/search.html`, `will-create: app/templates/help.html`, `will-create: app/templates/error.html`, `will-create: app/static/app.css`, `will-create: tests/test_render.py`
- **Acceptance criteria:**
1. `[MECHANICAL]` `.venv/bin/python -m pytest tests/test_render.py -q` passes.
2. `[STRUCTURAL]` No template or CSS contains `<script`, `display:grid`, `grid-template`, `fetch(`, `XMLHttpRequest`, or any `http(s)://` external asset/CDN/font reference (grep-asserted in the test).
3. `[BEHAVIORAL]` `feed.html` renders navigation entries as `<a href>` links and book entries with format badges + a download/open `<a href>` to `/download/{id}/{safe-filename}.epub|pdf`; covers (when `SHOW_COVERS`) use `<img src="/cover/{id}">` (bridge id, never the apiKey). `book.html` renders the primary action `<a>` to the extension-bearing download URL. All links/img use bridge ids; the apiKey appears in no `href`/`src` (grep-asserted).
4. `[STRUCTURAL]` `base.html` includes `<meta name="viewport" content="width=device-width, initial-scale=1">` and the single `/static/app.css` link.
- **Depends on:** SS-02, SS-04
- **Constraints:** No JS, no external assets. Render the apiKey nowhere.
- **Escalation triggers:** none

## Tasks
- 06.1 — base/home/help/error templates + CSS
- 06.2 — feed/book/search templates + badges
- 06.3 — render.py + tests (incl. no-JS/no-grid grep asserts)

---
sub_spec_id: SS-07
phase: run
depends_on: [SS-05, SS-06]
dispatch: factory
---
### 7. FastAPI app wiring & routes
- **Scope:** `app/main.py` — FastAPI app with lifespan (create/close AsyncClient), optional access-key + IP-allowlist middleware, feed cache (TTL `CACHE_FEEDS_SECONDS`), and routes: `GET /` (home + Kavita status + search form + help link), `GET /feed/{id}`, `GET /book/{id}`, `GET /search?q=`, `GET /download/{id}/{filename}` (primary, extension-bearing) + `/download/{id}` + `/open/{id}` aliases, `GET /cover/{id}` (cover proxy), `GET /help`, `GET /health` (PlainTextResponse `ok`, middleware-exempt), `GET /static/app.css`. The `_to_view_model(feed)` seam encodes every nav/acquisition/cover href via `ids.encode_id` (per [H-2]). Friendly `error.html` for KavitaError/OpdsParseError/BadIdError/SsrfError; 404 for unknown ids. Wire opds→render with bridge-id rewriting of all upstream hrefs.
- **Files likely touched:** `will-create: app/main.py`, `will-create: app/errors.py`, `will-create: tests/test_app.py`
- **Acceptance criteria:**
1. `[MECHANICAL]` `.venv/bin/python -m pytest tests/test_app.py -q` passes (FastAPI `TestClient`, Kavita mocked).
2. `[BEHAVIORAL]` `GET /health` → 200 via `PlainTextResponse("ok")` (assert `r.text=="ok"` and content-type `text/plain`, per [C-1]); `/health` + `/static/*` bypass access-key/allowlist middleware (per [M-7]). `GET /` renders home with a link to the root feed and a GET search form. Unknown id → 404 error page. Kavita-down → friendly error page (not 500 traceback).
3. `[BEHAVIORAL]` `GET /feed/{id}` fetches+parses+renders a mocked OPDS feed; all rendered book/feed links are bridge ids; apiKey appears nowhere in any response body (grep-asserted).
4. `[STRUCTURAL]` AsyncClient created in lifespan; feed cache honored; access-key/allowlist middleware present and no-op when unconfigured.
- **Depends on:** SS-05, SS-06
- **Constraints:** No secret in any response body. No blocking I/O on the event loop.
- **Escalation triggers:** none

## Tasks
- 07.1 — app factory, lifespan, middleware, cache
- 07.2 — routes + error handling + id rewriting
- 07.3 — integration tests (TestClient + mocked Kavita)

---
sub_spec_id: SS-08
phase: run
depends_on: [SS-07]
dispatch: factory
---
### 8. Docker image & Portainer compose
- **Scope:** `Dockerfile` (`python:3.12-slim`, non-root uid 1000, requirements-before-code layer cache, `--no-cache-dir`, tzdata, EXPOSE 8099, HEALTHCHECK→/health, uvicorn single worker), `.dockerignore`, `docker-compose.yml` (Portainer stack: env vars, ports 8099, volumes `/config` `/cache`, restart unless-stopped, joins Kavita's network as **external**), `.env.example`.
- **Files likely touched:** `will-create: Dockerfile`, `will-create: .dockerignore`, `will-create: docker-compose.yml`, `will-create: .env.example`
- **Acceptance criteria:**
1. `[MECHANICAL]` `.venv/bin/python tests/validate_docker.py` exits 0 (static checks: Dockerfile has FROM python:3.12-slim, non-root USER, HEALTHCHECK, EXPOSE 8099; compose parses as valid YAML with service, ports 8099:8099, both volumes, and an external network for Kavita).
2. `[STRUCTURAL]` `.dockerignore` excludes `.venv`, `vault`, `docs`, `tests`, `.git`, `graphify-out`.
3. `[BEHAVIORAL]` `docker-compose.yml` env block includes KAVITA_BASE_URL, KAVITA_OPDS_URL, APP_PORT, PDF_DISPOSITION, EPUB_DISPOSITION, CACHE_FEEDS_SECONDS, CACHE_BOOKS, LOG_LEVEL, TZ; comments explain joining Kavita's existing network + service name must be `kavita`.
4. `[HUMAN REVIEW]` A real `docker build` + `docker compose up` against a live Kavita is left for the operator (no Docker in CI here); static validation must pass.
- **Depends on:** SS-07
- **Constraints:** Internal Kavita port 5000, not host-published. Pick static USER uid 1000 model.
- **Escalation triggers:** none

## Tasks
- 08.1 — Dockerfile + .dockerignore
- 08.2 — docker-compose.yml + .env.example
- 08.3 — tests/validate_docker.py

---
sub_spec_id: SS-09
phase: run
depends_on: [SS-07]
dispatch: factory
---
### 9. README, help content & header curl-doc
- **Scope:** `README.md` (what/why, get Kavita OPDS URL, Portainer folder-setup command, compose example, open-from-iPad steps, EPUB + PDF import instructions, troubleshooting wrong filenames / iPad can't connect / PDF won't save / EPUB downloads as .zip, LAN-only security warning). Ensure `/help` page content matches. `tests/test_headers_curl.md` documents expected `curl -I` output.
- **Files likely touched:** `will-create: README.md`, `will-create: docs/notes/header-expectations.md`
- **Acceptance criteria:**
1. `[STRUCTURAL]` `README.md` contains sections: What/Why, Kavita OPDS URL, Portainer setup, Compose, Open from iPad, EPUB import, PDF import, Troubleshooting, Security warning.
2. `[STRUCTURAL]` `docs/notes/header-expectations.md` shows expected EPUB + PDF `curl -I` headers.
3. `[BEHAVIORAL]` Instructions match actual routes/headers produced by the app.
- **Depends on:** SS-07
- **Constraints:** Accurate to the implementation.
- **Escalation triggers:** none

## Tasks
- 09.1 — README
- 09.2 — header-expectations note + /help parity

## Edge Cases
- Kavita unreachable/timeout/non-2xx → friendly error page + masked log; home shows status.
- Malformed/empty OPDS XML → `OpdsParseError` → error page.
- Forged/unknown/tampered id → 404 / `BadIdError`.
- Foreign-origin acquisition href → `SsrfError` (never fetched).
- Filename with quotes/slashes/`..`/control/unicode → sanitized ASCII + correct extension.
- Range request: forward upstream; relay 206 or fall back to 200.
- Very large book → streamed, never buffered; generous timeout (read=None).
- Access key wrong / IP blocked (when configured) → 403.
- apiKey must never appear in any HTML/log (unless LOG_LEVEL=debug).

## Out of Scope
Online reader/streaming reader; CBZ/CBR/MOBI/AZW/DJVU; audiobooks; progress sync to Kavita; user accounts/OAuth; cookies; HTTPS termination (reverse proxy's job); real Docker build in CI (no daemon here).

## Constraints
- **No JavaScript anywhere.** No SPA, no fetch/XHR, no `download` attr, no service workers, no CDN/web-fonts.
- Server-rendered HTML + one same-origin CSS file; old-Safari-safe CSS (no Grid, no modern-flex layout).
- apiKey server-side only; single same-origin surface; opaque ids.
- Stream, never buffer, book files; lifespan AsyncClient; `Timeout(read=None)`.
- Fail loud: typed exceptions with debuggable context + masked secrets; friendly error pages.

## Verification
- `.venv/bin/python -m pytest -q` → all green.
- `.venv/bin/python -m compileall -q app` → clean.
- Uvicorn smoke: app imports and `GET /health` returns `ok` via TestClient.
- grep asserts: no `<script`, no `display:grid`, no `fetch(`, no external `http(s)://` asset, no apiKey in any response body or template.
- `.venv/bin/python tests/validate_docker.py` → exit 0.
- Header checks: EPUB `application/epub+zip` + attachment + `.epub` filename; PDF `application/pdf` + inline + `.pdf` filename; `nosniff` present.
