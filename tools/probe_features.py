"""Live test of Reading List + history + prefs against ManyBooks via the app."""
import re

from fastapi.testclient import TestClient

from app.config import load_config
from app.main import create_app

cfg = load_config({
    "KAVITA_OPDS_URL": "https://manybooks.net/opds",
    "KAVITA_FEED_NAME": "ManyBooks",
    "BRIDGE_ID_SECRET": "feat",
    "STATE_DIR": "/tmp/rs-feat",  # writable temp state for the probe
})

with TestClient(create_app(cfg)) as c:
    # Walk to a real book.
    home = c.get("/").text
    fid = re.search(r"/feed/([\w\-.]+)", home).group(1)
    bid, frontier, seen = None, [fid], set()
    while frontier and not bid and len(seen) < 12:
        f = frontier.pop(0)
        if f in seen:
            continue
        seen.add(f)
        page = c.get(f"/feed/{f}").text
        m = re.search(r"/book/([\w\-.]+)", page)
        if m:
            bid = m.group(1)
            break
        frontier += re.findall(r'/feed/([\w\-.]+)"', page)[:6]
    title = re.search(r"<h1>([^<]+)</h1>", c.get(f"/book/{bid}").text).group(1)
    print("book:", title)

    print("list empty initially:", "reading list is empty" in c.get("/list").text.lower())
    c.get(f"/star/{bid}")
    lst = c.get("/list").text
    print("after star, in list:", title in lst, "| has from-label:", "from " in lst)
    print("book shows starred:", "Remove from Reading List" in c.get(f"/book/{bid}").text)

    # Download → history
    dl = re.search(r'/download/([\w\-.]+)/([^"]+\.epub)', c.get(f"/book/{bid}").text)
    r = c.get(f"/download/{dl.group(1)}/{dl.group(2)}")
    print("download ok:", r.status_code == 200, "epub:", r.content[:4] == b"PK\x03\x04")
    print("home shows recent:", "Recently sent to iBooks" in c.get("/").text)
    print("book marked sent:", "already sent" in c.get(f"/book/{bid}").text)

    # Prefs
    big = c.get("/prefs?big=toggle&next=/").text
    print("large-print on:", 'class="big"' in big)
    print("no key leak in list:", "/api/opds/" not in lst and "apiKey=" not in lst)
