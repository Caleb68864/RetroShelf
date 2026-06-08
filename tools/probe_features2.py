"""Live test of features 4-6 against real servers."""
import re

from fastapi.testclient import TestClient

from app import opds
from app.config import load_config
from app.main import create_app

cfg = load_config({
    "KAVITA_OPDS_URL": "https://www.gutenberg.org/ebooks.opds/",
    "KAVITA_FEED_NAME": "Project Gutenberg",
    "BRIDGE_ID_SECRET": "f2",
    "STATE_DIR": "/tmp/rs-f2",
})

with TestClient(create_app(cfg)) as c:
    # Walk to a Gutenberg book.
    home = c.get("/").text
    fid = re.search(r"/feed/([\w\-.]+)", home).group(1)
    bid, frontier, seen = None, [fid], set()
    while frontier and not bid and len(seen) < 20:
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

    detail = c.get(f"/book/{bid}").text
    title = re.search(r"<h1>([^<]+)</h1>", detail).group(1)
    sizes = re.findall(r"&middot; ([\d.]+ [KMG]?B)", detail)
    print("book:", title)
    print("FEATURE 5 — download shows size:", bool(sizes), sizes[:2])

    # Feature 6: star + republish OPDS
    c.get(f"/star/{bid}")
    feed_xml = c.get("/opds/reading-list")
    parsed = opds.parse(feed_xml.text)
    e = parsed.entries[0] if parsed.entries else None
    print("FEATURE 6 — republished feed parses:", e is not None,
          "| title:", e.title if e else None,
          "| has download link:", bool(e and e.primary_acquisition and "/download/" in e.primary_acquisition.href))
    print("           content-type:", feed_xml.headers["content-type"][:55])
    print("           no upstream-key leak:", "/api/opds/" not in feed_xml.text)

    # Feature 4: color theme
    green = c.get("/prefs?color=green&next=/").text
    print("FEATURE 4 — green phosphor:", 'class="color-green"' in green)
