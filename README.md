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

## Read in the browser

For **EPUB** books — and any book offered as a **web page (HTML)** or plain
text — RetroShelf can also shelve the book server-side and let you read it
right there in old Safari, no iBooks import needed. Tap **Read here** on a
book's page: the first open unpacks and sanitizes the book into the reader
cache (a few seconds), then every page after that is instant. Your place is
remembered automatically (server-side, last-read-wins across devices), so
tapping **Continue reading** always picks up where you left off, and the home
screen's **Currently Reading** shelf shows your progress.

EPUB is always preferred when a book offers it; an **HTML** edition (for
example Project Gutenberg's "Read online" link) is a read-in-browser fallback
for books that offer no EPUB. HTML books split into chapters on their own
headings, images are fetched and downscaled just like EPUB images, and — like
every other upstream fetch — each image URL passes the same SSRF guard, so a
page can never make the bridge reach a foreign host. HTML/text editions have
no iBooks hand-off, so their book page shows only **Read here**.

**Comics (CBZ) read here too** — Kavita's second-biggest content type. A CBZ is
a zip of page images; RetroShelf unpacks it, downscales each page to an
iPad-sized image, and shows one page per screen. Page-turning is the same
**Prev / Next** as any book, your place is remembered per page, and bookmarks
work. There's no iBooks hand-off (Books can't import a CBZ), so a comic's book
page shows only **Read here**. (CBR — the RAR-based variant — is not supported.)

**PDFs read here too, via text reflow** — a PDF's text layer is extracted and
reflowed into the same reader (chapters come from the PDF's bookmarks/outline
when it has one). PDF is the one **dual** format: its book page keeps the
native **Open PDF** button (inline view → Share → "Copy to Books", which
preserves the original layout and figures) *and* offers **Read here** for
comfortable reflowed reading. A **scanned / image-only PDF** (no text layer)
can't be reflowed — you get a friendly page pointing you at **Open PDF**
instead. This is text-only for now: page images aren't rendered in the reflow
view. A book that's DRM-protected or otherwise unreadable falls back
gracefully: you get a friendly message, and where an iBooks path exists the
**Open in iBooks** button still works.

The footer's `rs_split` control sets how much text lands on one page:
**Small**, **Medium** (default), **Large**, or **Whole** (chapter). It's a
per-device cookie, no JS, and switching sizes mid-book still resumes at the
right spot. The same footer also has a **book** / **phosphor** reader theme
toggle (phosphor is the amber/green CRT look, same family as the site-wide
theme switch above) and honors the Large-Print toggle.

To keep the bridge safe against hostile or oversized files, the reader
enforces hard caps: **80 MB** per EPUB or PDF (**16 MB** per HTML/text
document, **300 MB** per CBZ comic), **500 chapters** per book, **5000 pages**
per PDF, **800 pages** per comic, per-image size limits, and a **1 GB** total
on-disk reader cache (pruned oldest-shelved-book-first). A book over any cap
gets a friendly error and, where one exists, the iBooks download path instead.

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
sudo mkdir -p /srv/docker_data/retroshelf/{config,cache}
sudo chown -R 1000:1000 /srv/docker_data/retroshelf
```

**2. Deploy the stack.** There are two ways to do this in Portainer — pick one:

#### Option A — Pull the pre-built image (easiest, Web editor)

In Portainer go to **Stacks → Add stack → Web editor** and paste
[`portainer-stack.yml`](portainer-stack.yml). It pulls a published multi-arch
image (`ghcr.io/caleb68864/retroshelf:latest`, amd64 + arm64) so there's no
build step. This stack **runs with zero configuration** — out of the box it
fronts two public libraries (ManyBooks + Project Gutenberg), so you can deploy
and open it immediately. To add your own Kavita library, set `KAVITA_OPDS_URL`
in Portainer's **Environment variables** section (and, only if Kavita is a
Docker container on this host, uncomment the `networks:` blocks in the stack and
set `KAVITA_NETWORK` — see the comments at the top of the file).

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

> **Networking note (only when connecting to a Docker-hosted Kavita):** For
> `http://kavita:5000` to resolve, RetroShelf must be on the **same Docker
> network as Kavita**, and Kavita's *service name* (or a network alias) must be
> `kavita`. Find Kavita's network with `docker network ls` and set it as
> `KAVITA_NETWORK` (Option A) or the network `name:` (Option B). Use Kavita's
> **internal** port (5000), not a host-published port. If Kavita runs on another
> machine, just point `KAVITA_OPDS_URL` at its LAN IP — no shared network needed.

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
| `BRIDGE_ACCESS_KEY` | – | off | Require a key: visit `/?key=...` once, then a cookie carries it (or send `X-Access-Key`) |
| `EXTRA_UPSTREAM_ORIGINS` | – | — | Comma-separated extra origins to trust (for download hosts on an unrelated domain) |
| `ALLOWED_IPS` | – | off | Direct-LAN IP/CIDR allowlist (not proxy-aware) |
| `ACCOUNTS_ENABLED` | – | off | Multi-account login + profiles; first visit creates an admin at `/setup`, `/login` becomes the gate (supersedes `BRIDGE_ACCESS_KEY`), reading state is per-profile |
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

Then open `http://<this-computer-ip>:8099` from the iPad. `run.sh` **opens the
firewall port automatically** (firewalld/ufw) so other devices can reach it — you
may be prompted for your sudo password. Set `RS_OPEN_FIREWALL=0 ./run.sh` to skip
that if you manage the firewall yourself (then see **Troubleshooting → "The iPad
can't connect"** for the manual commands).

---

## Opening it from the iPad

1. On the iPad, open **Safari**.
2. Go to `http://YOUR-SERVER-IP:8099` (e.g. `http://192.168.1.50:8099`).
3. Tap **Enter the Library** (or pick a library from the menu) and find a book.

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

**Accounts + profiles (opt-in).** Set `ACCOUNTS_ENABLED=true` for multi-account login:
the first visit creates an admin account at `/setup`, then `/login` is the gate
(superseding `BRIDGE_ACCESS_KEY`; `ALLOWED_IPS` still fronts it). Each account has
Netflix-style profiles (no per-profile PIN) and its own reading state — positions,
bookmarks, Reading List, history. Passwords are salted PBKDF2-SHA256 (stdlib, no new
dependency); sessions are HMAC-signed, expiring, HttpOnly+SameSite=Lax cookies. Every
mutating form carries a server-checked CSRF token. Set a stable `BRIDGE_ID_SECRET` so
sessions survive restarts. Note: with accounts on, external OPDS readers can't
authenticate to `/opds` (browser login only) — a v1 limitation.

Login hardening (all automatic, no configuration):

- **Brute-force throttle.** Failed logins are rate-limited in-process (no new
  dependency; counters reset on restart, which only the operator can trigger).
  A per-**device** hard lockout stops guessing after too many failures from one
  address, and a per-**username** escalating delay slows each attempt. The
  lockout keys on the *attacker's* address, never a username, so it can't be
  used to lock a victim out; the response is the same whether the username
  exists or not (no user enumeration). A correct login clears the counters.
  *If you front RetroShelf with a reverse proxy, all clients share the proxy's
  address as far as the throttle is concerned — let the proxy do auth in that
  setup (the IP-allowlist has the same direct-socket limitation).*
- **Password bounds.** A minimum length, and a 256-char maximum plus a 64 KB
  cap on any auth form body, so a giant password can't be used as a hashing DoS.
- **Revocation.** Changing your password, or the **“Sign out all other devices”**
  button on the Account page, invalidates every outstanding session cookie for
  that account (a lost or copied cookie included) while keeping the device you
  clicked from signed in. Plain **Sign out** only clears the local cookie, so one
  family device signing off doesn't sign the whole household out.
- **Secure cookie over HTTPS.** When `BRIDGE_PUBLIC_URL` is an `https://` URL the
  session cookie is marked `Secure`; on a plain-HTTP LAN it is not (a `Secure`
  cookie would never be sent over `http://`).

What the bridge does on its own, with nothing configured:

- **SSRF guard** — every upstream URL (including ones decoded from bridge ids)
  must match a configured feed origin, a same-site sibling of one, or
  `EXTRA_UPSTREAM_ORIGINS`; non-HTTP schemes, credentials-in-URL, private-IP
  literals via the sibling rule, and redirect hops are all checked.
- **Opaque ids** — every URL the iPad sees is an authenticated, encrypted
  token; the Kavita apiKey never appears in a page, link, log line, or cache
  filename. Set `BRIDGE_ID_SECRET` so tokens survive restarts.
- **Bounded everything** — feed size/time, cover size and decode pixels, OPDS
  entry counts and field lengths, state-file fields, and the cover disk cache
  all have hard caps, so one hostile or broken catalogue cannot take the
  bridge down.
- **State-change links** (`/star`, `/unstar`, `/prefs`) carry a same-site
  token, so another page on your LAN cannot trigger them with an `<img>` tag.
- **Secret-masking logs** — access keys, apiKeys, and the session-signing
  `BRIDGE_ID_SECRET` are redacted from every log record, including uvicorn's
  access log. Passwords and session/CSRF tokens never enter a URL or log line.
- The Docker deployment runs read-only, as uid 1000, with all capabilities
  dropped.

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
