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

## Features

More than an OPDS-to-HTML proxy — a personal cross-library reading manager:

- **Multiple libraries (portal).** Front several OPDS catalogs at once (Kavita,
  Project Gutenberg, ManyBooks, …) — `OPDS_FEEDS`. The home screen becomes a menu.
- **Fan-out search.** From home, one search queries *every* library at once and
  groups the results by library; in-library search stays scoped.
- **Cross-feed Reading List.** Star books from any library into one persistent
  list (`/list`). Survives restarts (stored in `/config`).
- **"On this iPad" history.** Already-downloaded books get a ✓ *sent* mark, and
  the home screen shows a "Recently sent to iBooks" shelf.
- **Multi-format downloads.** When a book offers both EPUB and PDF you get a
  button for each, with file sizes.
- **Re-publishes as OPDS.** Your Reading List is itself a valid OPDS feed at
  `/opds/reading-list` — subscribe to your curated shelf from any OPDS reader.
- **More by this author.** A book's author links to a fan-out search for their
  other titles across every library.
- **Library status dashboard** (`/status`) — live reachability of each
  configured library (online/offline, response time, shelf count).
- **Surprise Me** (`/random`) — jump to a random book from a random library.
- **Add to Home Screen.** Web-app meta + an apple-touch-icon, so RetroShelf
  installs to the iPad home screen and launches fullscreen, like a native app.
- **Sort this page.** Reorder the current shelf page by title, author, or format
  (server-side; pagination is upstream-driven, so it's honestly labelled "this page").
- **Old-Safari-safe covers.** Covers are transcoded to baseline JPEG and
  downscaled server-side — old Safari can't render WebP and big images choke old
  iPads — then disk-cached (`/cache`) so repeat browsing is fast.
- **Accessibility.** One-tap Large-Print mode (with enlarged tap targets),
  hide-covers (for speed), and an amber / green / white **CRT phosphor** theme
  switch — all via the footer, no JS.

Everything is server-rendered, no-JavaScript, and works on iOS 5.1.1–12 Safari.

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

**2. Deploy the stack.** There are two ways to do this in Portainer — pick one:

#### Option A — Pull the pre-built image (easiest, Web editor)

In Portainer go to **Stacks → Add stack → Web editor** and paste
[`portainer-stack.yml`](portainer-stack.yml). It pulls a published multi-arch
image (`ghcr.io/caleb68864/retroshelf:latest`, amd64 + arm64) so there's no
build step. Set your values in Portainer's **Environment variables** section —
at minimum `KAVITA_OPDS_URL`, and `KAVITA_NETWORK` (the network your Kavita
stack is on).

> The image is published by the included GitHub Action on every push to `main`.
> After the first run, make the GHCR package **public** (GitHub → Packages →
> retroshelf → Package settings → Change visibility) so Portainer can pull it
> without credentials — otherwise add the registry login under Portainer →
> Registries.

#### Option B — Build from this repo (Repository method)

In Portainer go to **Stacks → Add stack → Repository**, point it at this GitHub
repo, and set the compose path to `docker-compose.yml`. Portainer clones the
repo and builds the image itself (`build: .`). Use this if you'd rather not rely
on GHCR. Edit the env values in `docker-compose.yml` first (or fork and commit).

> **Networking note (both options):** For `http://kavita:5000` to resolve,
> RetroShelf must be on the **same Docker network as Kavita**, and Kavita's
> *service name* (or a network alias) must be `kavita`. Find Kavita's network
> with `docker network ls` and set it as `KAVITA_NETWORK` (Option A) or the
> network `name:` (Option B). Use Kavita's **internal** port (5000), not a
> host-published port.

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
| `EXTRA_UPSTREAM_ORIGINS` | – | — | Comma-separated extra origins to trust (for download hosts on an unrelated domain) |
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

Some sources serve book files from a sibling host (e.g. ManyBooks lists feeds on
`manybooks.net` but downloads from `library.manybooks.net`). The guard trusts a
host automatically when it shares a configured feed's registrable domain (same
scheme and port) — so these "just work" with no extra setup. If a source serves
files from an *unrelated* domain (some Project Gutenberg mirrors do), add it to
`EXTRA_UPSTREAM_ORIGINS` (e.g. `EXTRA_UPSTREAM_ORIGINS=https://gutenberg.pglaf.org`).

---

## Run without Docker (bare metal)

For a quick LAN setup without containers, use the launcher — it creates a venv,
installs deps, and serves on `0.0.0.0:8099`:

```bash
./run.sh                                         # defaults to the public ManyBooks feed
./run.sh "http://kavita:5000/api/opds/YOUR_KEY"  # or point at your Kavita OPDS URL
```

Then open `http://<this-computer-ip>:8099` from the iPad. If other devices can't
connect, open the firewall port — see **Troubleshooting → "The iPad can't connect"**.

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
IP and port `:8099`, and that the server is running (`/health` returns `ok`). Use the machine's
**LAN** IP (e.g. `192.168.x.x`), not a Tailscale (`100.x`) or Docker (`172.17.x`) address.

If `curl http://<server-ip>:8099/health` works **on the server itself** but no other device can
connect, the host **firewall** is blocking the port — this is the most common bare-metal gotcha.
RetroShelf binds to `0.0.0.0`, so the bind isn't the problem; you just need to open `8099/tcp`:

```bash
# firewalld (Fedora, Arch, RHEL, openSUSE, …) — open 8099 on the zone your LAN NIC is in
firewall-cmd --get-active-zones                       # find the zone (often "public")
sudo firewall-cmd --zone=public --permanent --add-port=8099/tcp
sudo firewall-cmd --reload

# ufw (Debian/Ubuntu)
sudo ufw allow 8099/tcp
```

The Docker deployment publishes the port itself, so this only applies to running it bare-metal
(e.g. via `./run.sh`). If the port is open and it *still* fails, check for Wi-Fi **AP/client
isolation** on your router (it blocks device-to-device traffic even on one subnet).

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
