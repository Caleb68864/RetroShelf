---
date: 2026-06-07
topic: "RetroShelf — Kavita→iBooks bridge for old iPads"
author: Caleb Bennett + Forge
status: draft
tags:
  - design
  - retroshelf
---

# RetroShelf — Design

## Summary
RetroShelf is a tiny Docker-hosted FastAPI + Jinja2 bridge that lets very old iPads (iOS 5.1.1–12) browse a Kavita ebook library in Safari and import EPUB/PDF into iBooks / Apple Books — no iTunes, no modern apps, no JavaScript. The bridge fetches Kavita's OPDS feed server-side, renders old-Safari-friendly HTML, and proxies/streams book files back with the exact HTTP headers iOS needs to hand a file to Books. This design is grounded in adversarially-verified research (see `vault/Build Constraints.md` and `vault/Corrected Assumptions.md`).

## Approach Selected
**Server-side OPDS proxy + streaming download bridge (no-JS, server-rendered).** Chosen over (B) a static pre-generated catalog and (C) a client-side reader, because only a live server-side proxy can hold the Kavita apiKey securely, keep a single same-origin surface (old Safari suppresses cross-origin Basic Auth since iOS 11.2), and apply the precise Content-Type/Content-Disposition headers that trigger the iOS "Open in iBooks" hand-off.

## Architecture

```
┌────────────────┐   plain GET / <a href>     ┌────────────────────────────┐
│ Old iPad Safari│ ─────────────────────────▶ │ RetroShelf (FastAPI+Jinja2)│
│ iOS 5.1.1–12   │ ◀───────────────────────── │  - config (env)            │
│ no JS, no fetch│   server-rendered HTML     │  - opds client (httpx)     │
└────────────────┘                            │  - opds parser (defusedxml)│
        │  tap book → GET /download/{id}      │  - id mapper (stable hash) │
        ▼                                      │  - HTML render (templates) │
   Safari streams file  ◀────proxy stream──── │  - download proxy (stream) │
   PDF: inline → Share → Copy to Books        └─────────────┬──────────────┘
   EPUB: "Open in iBooks" hand-off sheet                    │ httpx (apiKey server-side)
                                                            ▼
                                              ┌────────────────────────────┐
                                              │ Kavita  /api/opds/{apiKey} │
                                              │ /api/image/*-cover (covers)│
                                              └────────────────────────────┘
```

**Layered modules (single small package `app/`):**
- `config.py` — env parsing + validation (KAVITA_BASE_URL, KAVITA_OPDS_URL, ports, dispositions, access key, allowlist, cache TTL). Masks secrets.
- `kavita.py` — httpx client wrapper. Lifespan-managed `AsyncClient` with `Timeout(read=None)`. Fetch OPDS feed; stream downloads; resolve Kavita-relative absolute-path hrefs against the Kavita origin.
- `opds.py` — parse Atom/OPDS with `defusedxml.ElementTree`. Extract feed title, navigation links, acquisition links (rel startswith `http://opds-spec.org/acquisition`), covers, authors, pagination (`next`/`previous`), search.
- `ids.py` — map upstream acquisition/feed URLs ↔ stable opaque internal ids (signed/hashed) so the apiKey never appears in rendered HTML and user input can't drive arbitrary fetches.
- `security.py` — optional access key check, optional IP allowlist, filename sanitization, upstream-URL allowlisting (only Kavita origin).
- `render.py` / `templates/` — Jinja2 server-rendered HTML; one static CSS file. No JS.
- `main.py` — FastAPI routes + lifespan + middleware.

## Components

| Component | Owns | Does NOT own |
|---|---|---|
| `config` | env validation, defaults, secret masking | network, parsing |
| `kavita` (httpx) | upstream fetch + streaming, origin resolution, timeouts | XML parsing, HTML |
| `opds` (parser) | Atom→typed feed/entry objects | network, HTML |
| `ids` | url↔id mapping, signing | rendering |
| `security` | access key, allowlist, sanitization, SSRF guard | business logic |
| `render`/templates | old-Safari HTML/CSS | data fetching |
| `main` | routing, lifespan, wiring | parsing, rendering internals |

## Data Flow
1. iPad `GET /` → home: Kavita connection status, root feed link, search form, help link.
2. iPad `GET /feed/{id}` → bridge maps id→Kavita feed URL → httpx fetch → `opds.parse` → render list of navigation entries + book entries (format badges, author, covers). Pagination as plain prev/next links.
3. iPad `GET /book/{id}` → render detail (title, author, format, summary) + primary "Open in iBooks/Books" button → `/download/{id}` (or `/open/{id}`).
4. iPad `GET /download/{id}` → map id→Kavita acquisition URL → httpx `stream("GET")` with forwarded Range → `StreamingResponse(aiter_raw, headers, background=aclose)`. Headers: `Content-Type` (epub+zip|pdf), `Content-Disposition` (epub=attachment, pdf=inline; sanitized ASCII `filename` with correct extension), `X-Content-Type-Options: nosniff`, relay `Content-Length`/`Accept-Ranges`/`Content-Range`/206 when upstream provides.
5. `GET /search?q=` → GET form → OPDS search feed → render like a feed listing.
6. `GET /health` → plain text `ok`. `GET /help` → static instructions. `GET /static/app.css` → the one stylesheet.

**Download URL ends in the correct extension** (`/download/{id}.epub` / `.pdf` form or trailing filename segment) AND correct Content-Type AND filename — belt-and-suspenders, because old WebKit derives the saved name from the URL path and the extension from the MIME type (see Corrected Assumptions).

## Error Handling
- **Kavita unreachable / timeout / non-200:** friendly server-rendered error page (no stack trace to user) + logged detail. Home page shows "Cannot reach Kavita" status. Never 500-blank.
- **Malformed OPDS XML:** defusedxml parse guarded; render "Could not read library feed" with logged raw snippet (masked).
- **Unknown/forged id:** 404 page. ids are opaque + validated; reject anything that doesn't map.
- **SSRF / path traversal:** only fetch URLs whose origin == configured Kavita origin; sanitize filenames (strip quotes/slashes/control/`..`); reject otherwise.
- **Download stream breaks mid-flight:** background task closes upstream; client sees truncated transfer (acceptable, Safari retries). Log it.
- **Access key required but missing/wrong (if configured):** 403 page. **IP not in allowlist (if configured):** 403.
- **Secrets:** apiKey/access key never logged unless `LOG_LEVEL=debug`; masked in all other logs and never rendered in HTML.

**Fail-loud principle (for hardening):** every upstream/parse/config failure raises a typed exception with enough context (which url, which id, upstream status) to debug, surfaced as a clear error page + structured log line — never a silent empty page.

## Open Questions
- Exact Kavita OPDS root-feed entry set / link rels to confirm against a live instance (handled defensively by structural detection). → `vault/Open Questions.md`
- Whether to show cover thumbnails by default (old Safari can be slow / memory-limited) — make it a config toggle, default on but degrade gracefully.
- Range relay: forward upstream Range vs. always stream 200 — implement forward-and-relay; fall back to full 200 if upstream ignores Range.
- Container ownership model: static `USER` uid 1000 (chosen for simplicity) vs PUID/PGID gosu — default static, document the alternative.

## Approaches Considered
- **A. Server-side OPDS proxy + streaming bridge (SELECTED)** — secure apiKey handling, single same-origin surface, exact headers, no-JS. Slightly more server work.
- **B. Static pre-generated HTML catalog** — simplest hosting, but stale, can't stream/proxy auth, leaks apiKey or breaks on updates. Rejected.
- **C. Client-side OPDS reader (JS)** — impossible: no fetch <iOS 10.3, no-JS mandate, Basic Auth suppressed. Rejected outright.

## Next Steps
- [ ] Turn this design into a Forge spec (`/forge docs/plans/2026-06-07-retroshelf-design.md`)
- [ ] Adversarial red-team of the spec before build
- [ ] Build via dark factory (or direct TDD fallback if CLI can't run headless / no Docker)
