"""Live multi-feed proof: configure two real OPDS servers and drive the real
app (TestClient runs the real lifespan → real httpx → real internet)."""
import re

from fastapi.testclient import TestClient

from app.config import load_config
from app.main import create_app

cfg = load_config({
    "KAVITA_OPDS_URL": "https://manybooks.net/opds",   # primary
    "KAVITA_FEED_NAME": "ManyBooks",
    "OPDS_FEEDS": "Project Gutenberg|https://www.gutenberg.org/ebooks.opds/",
    "BRIDGE_ID_SECRET": "portal",
})
print("configured feeds:", [(f.name, f.origin) for f in cfg.feeds])
print("allowed origins:", cfg.allowed_origins)

with TestClient(create_app(cfg)) as c:
    home = c.get("/").text
    print("home lists ManyBooks:", "ManyBooks" in home)
    print("home lists Project Gutenberg:", "Project Gutenberg" in home)
    print("no key leak on home:", "/api/opds/" not in home and "apiKey=" not in home)
    fids = re.findall(r'/feed/([\w\-.]+)"', home)
    print("feed links in menu:", len(fids))
    # Browse the first feed (ManyBooks) one level.
    page = c.get(f"/feed/{fids[0]}")
    print("browse feed[0] status:", page.status_code)
    titles = re.findall(r'class="navlink"[^>]*>.*?</span>([^<]+)<', page.text)
    print("feed[0] sample entries:", [t.strip() for t in titles[:3]])
    print("feed[0] no leak:", "/api/opds/" not in page.text)
