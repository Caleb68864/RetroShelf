# RetroShelf

**A tiny bridge that lets very old iPads browse your Kavita ebook library in Safari and import EPUB/PDF straight into iBooks / Apple Books — no iTunes, no modern apps, no JavaScript.**

RetroShelf targets iPads running **iOS 5.1.1 through iOS 12** (original iPad, iPad 2, early Lightning iPads). It renders a dead-simple, server-rendered, no-JavaScript website that those old Safari versions can actually use, fetches your [Kavita](https://www.kavitareader.com/) OPDS library server-side, and streams each book back with exactly the HTTP headers iOS needs to hand the file to Books.

## Why it exists

Old iPads can't run the App Store apps or modern browsers that normally talk to Kavita, and Apple removed iTunes book syncing. But every one of these iPads has Safari and iBooks/Books. RetroShelf is the missing link: the old iPad is a *dumb glass book selector*, and the server does all the work.

---

## How it works

```
old iPad Safari ──GET──▶ RetroShelf ──(apiKey, server-side)──▶ Kavita OPDS
   tap a book  ◀── plain HTML ──┘
        │
        ▼   GET /download/{id}/Book-Title.epub
   RetroShelf streams the file with:
     EPUB → Content-Type: application/epub+zip  + attachment   → "Open in iBooks"
     PDF  → Content-Type: application/pdf        + inline       → Share → "Copy to Books"
```

Your Kavita API key stays on the server and never appears in a page or a link.

---

## Getting your Kavita OPDS URL

1. In Kavita, click your user icon → **Settings** → **3rd Party Clients / OPDS**.
2. Enable OPDS if it isn't already, and copy your personal **OPDS URL**. It looks like:
   `http://YOUR-KAVITA-HOST:5000/api/opds/4f1e...your-api-key...`
3. The trailing path segment is your **API key** — treat it like a password.

You'll give that full URL to RetroShelf as `KAVITA_OPDS_URL`.

---

## Install (Portainer / Docker Compose)

**1. Create the data folders on the host (run once):**

```bash
sudo mkdir -p /srv/docker_data/kavita-ibooks-bridge/{config,cache}
sudo chown -R 1000:1000 /srv/docker_data/kavita-ibooks-bridge
```

**2. Deploy the stack** (Portainer → Stacks → Add stack, paste `docker-compose.yml`):

```yaml
services:
  kavita-ibooks-bridge:
    image: retroshelf:latest
    container_name: retroshelf
    build: .
    restart: unless-stopped
    ports:
      - "8099:8099"
    environment:
      - TZ=America/Chicago
      - KAVITA_BASE_URL=http://kavita:5000
      - KAVITA_OPDS_URL=http://kavita:5000/api/opds/YOUR_AUTH_KEY
      - PDF_DISPOSITION=inline
      - EPUB_DISPOSITION=attachment
    volumes:
      - /srv/docker_data/kavita-ibooks-bridge/config:/config
      - /srv/docker_data/kavita-ibooks-bridge/cache:/cache
    networks:
      - kavita_net

networks:
  kavita_net:
    external: true
    name: kavita_default   # <-- the network your Kavita container is on
```

> **Networking note:** For `http://kavita:5000` to resolve, RetroShelf must be on the
> **same Docker network as Kavita**, and Kavita's *service name* (or a network alias)
> must be `kavita`. Find Kavita's network with `docker network ls` and set `name:`
> accordingly. Use Kavita's **internal** port (5000), not a host-published port.

### Configuration (environment variables)

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `KAVITA_OPDS_URL` | ✅* | — | Full OPDS URL incl. your API key (the primary library) |
| `OPDS_FEEDS` | – | — | Extra libraries for the portal menu (see below) |
| `KAVITA_FEED_NAME` | – | `Library` | Menu name for the primary feed |
| `KAVITA_BASE_URL` | – | derived | Kavita base (derived from OPDS origin if unset) |
| `APP_PORT` | – | `8099` | Listen port |
| `PDF_DISPOSITION` | – | `inline` | `inline` (render in Safari) or `attachment` |
| `EPUB_DISPOSITION` | – | `attachment` | EPUB disposition |
| `SHOW_COVERS` | – | `true` | Show cover thumbnails |
| `CACHE_FEEDS_SECONDS` | – | `300` | OPDS feed cache TTL |
| `BRIDGE_ID_SECRET` | – | random | Stable secret so bookmarked links survive restarts |
| `BRIDGE_ACCESS_KEY` | – | off | Require `?key=...` on every page |
| `ALLOWED_IPS` | – | off | Direct-LAN IP/CIDR allowlist (not proxy-aware) |
| `LOG_LEVEL` | – | `info` | `debug` for verbose (masked) logs |
| `TZ` | – | `America/Chicago` | Container timezone |

\* At least one feed is required — via `KAVITA_OPDS_URL`, `OPDS_FEEDS`, or both.

### Multiple libraries (portal mode)

RetroShelf can front **several OPDS libraries** at once. Set `OPDS_FEEDS` to a
comma- or newline-separated list of `Name|URL` entries; the home screen becomes a
menu where you pick a library, then browse and search it normally.

```yaml
environment:
  - KAVITA_OPDS_URL=http://kavita:5000/api/opds/YOUR_AUTH_KEY   # primary
  - KAVITA_FEED_NAME=My Kavita
  - OPDS_FEEDS=Project Gutenberg|https://www.gutenberg.org/ebooks.opds/, ManyBooks|https://manybooks.net/opds
```

Each library's downloads/covers are fetched from its own origin (the SSRF guard
allows every configured feed's origin automatically), each library's apiKey is
masked, and search is scoped to the library you're in. Public feeds behind
Cloudflare work because the bridge sends a browser `User-Agent`.

---

## Opening it from the iPad

1. On the iPad, open **Safari**.
2. Go to `http://YOUR-SERVER-IP:8099` (e.g. `http://192.168.1.50:8099`).
3. Tap **Browse the Library** and find a book.

### Importing an EPUB
Tap **Open in iBooks**. If iOS asks, choose **iBooks** / **Books**. Done — it's in your library.

### Importing a PDF
Tap **Open PDF**. Safari shows the PDF. Tap the screen once if the buttons hide, then tap
the **Share** button and choose **Copy to iBooks** / **Add to iBooks** / **Open in Books**.

---

## Troubleshooting

**The book downloads as `.zip` or `.epub.zip`.**
The server sent the wrong `Content-Type`. RetroShelf sends `application/epub+zip`; if you
still see this, you may be hitting Kavita directly instead of the bridge, or a reverse
proxy is rewriting headers. Check `curl -I` (see `docs/notes/header-expectations.md`).

**PDF opens in Safari but I can't save it.**
That's normal on old iOS — there's no download button. Use the **Share** button → **Copy/Open
in Books** while the PDF is on screen.

**The iPad can't connect.**
Confirm the iPad is on the same Wi-Fi/LAN as the server, that you used `http://` and the right
IP and port `:8099`, and that the container is running (`/health` returns `ok`).

**"Open in iBooks" doesn't appear.**
Press and hold the button, then choose **Open** / **Open in New Tab**. Very old iOS (5–6) can
be finicky; the PDF flow (inline + Share) is the most reliable there.

**Covers don't load / are slow.**
Set `SHOW_COVERS=false` — old iPads can be memory-limited.

---

## Security

RetroShelf is intended for a **trusted home LAN**. It holds your Kavita API key server-side
and never exposes it, but it has **no login by default**. If you expose it beyond your LAN,
put it behind a reverse proxy with HTTPS and authentication, and/or set `BRIDGE_ACCESS_KEY`
and `ALLOWED_IPS`. The IP allowlist uses the direct socket address and is **not** aware of
`X-Forwarded-For`, so it only works for direct-LAN access, not behind a proxy.

---

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q          # run tests
KAVITA_OPDS_URL=http://localhost:5000/api/opds/KEY \
  .venv/bin/python -m uvicorn app.main:app --port 8099
.venv/bin/python tests/validate_docker.py   # static Docker checks
```

Architecture and the full verified research live in [`docs/`](docs/) and the
Obsidian research vault at [`vault/`](vault/) (see `vault/Build Constraints.md`).
