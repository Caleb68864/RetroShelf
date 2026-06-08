"""Live test of features 7-9 (author search, status dashboard, surprise-me)."""
import re

from fastapi.testclient import TestClient

from app.config import load_config
from app.main import create_app

cfg = load_config({
    "KAVITA_OPDS_URL": "https://manybooks.net/opds",
    "KAVITA_FEED_NAME": "ManyBooks",
    "OPDS_FEEDS": "Project Gutenberg|https://www.gutenberg.org/ebooks.opds/",
    "BRIDGE_ID_SECRET": "f3",
    "STATE_DIR": "/tmp/rs-f3",
})

with TestClient(create_app(cfg)) as c:
    # FEATURE 8: status dashboard
    st = c.get("/status").text
    print("FEATURE 8 — status page online count:", st.count("ONLINE"),
          "| names:", "ManyBooks" in st, "Project Gutenberg" in st)

    # FEATURE 9: surprise me
    r = c.get("/random", follow_redirects=False)
    print("FEATURE 9 — /random ->", r.status_code, r.headers.get("location", "")[:14])
    book = c.get(r.headers["location"]).text if r.status_code == 303 else ""
    title = re.search(r"<h1>([^<]+)</h1>", book)
    print("           landed on book:", title.group(1) if title else "?")

    # FEATURE 7: more-by-author link on that random book
    m = re.search(r'href="(/search\?q=[^"]+&feed=\*)"', book)
    print("FEATURE 7 — author search link (fan-out):", bool(m))
    if m:
        res = c.get(m.group(1).replace("&amp;", "&")).text
        print("           author search runs:", "across all libraries" in res,
              "| no key leak:", "/api/opds/" not in res)
