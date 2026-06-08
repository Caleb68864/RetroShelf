"""Live end-to-end through the real app against Project Gutenberg: home → walk
feeds → reach a book → download an EPUB with correct headers."""
import re

from fastapi.testclient import TestClient

from app.config import load_config
from app.main import create_app

cfg = load_config({
    "KAVITA_OPDS_URL": "https://www.gutenberg.org/ebooks.opds/",
    "KAVITA_FEED_NAME": "Project Gutenberg",
    "BRIDGE_ID_SECRET": "pg",
})

with TestClient(create_app(cfg)) as c:
    home = c.get("/").text
    fid = re.search(r"/feed/([\w\-.]+)", home).group(1)
    seen, frontier, book_id = set(), [fid], None
    steps = 0
    while frontier and book_id is None and steps < 25:
        steps += 1
        f = frontier.pop(0)
        if f in seen:
            continue
        seen.add(f)
        page = c.get(f"/feed/{f}").text
        assert "/api/opds/" not in page  # no leak
        m = re.search(r'/book/([\w\-.]+)', page)
        if m:
            book_id = m.group(1)
            break
        frontier += re.findall(r'/feed/([\w\-.]+)"', page)[:6]
    print("reached a book after", steps, "feed loads:", bool(book_id))
    detail = c.get(f"/book/{book_id}").text
    title = re.search(r"<h1>([^<]+)</h1>", detail)
    dl = re.search(r'/download/([\w\-.]+)/([^"]+\.(?:epub|pdf))', detail)
    print("book title:", title.group(1) if title else "?")
    print("download link:", dl.group(0) if dl else "NONE")
    if dl:
        r = c.get(f"/download/{dl.group(1)}/{dl.group(2)}")
        print("download status:", r.status_code)
        print("content-type:", r.headers.get("content-type"))
        print("disposition:", r.headers.get("content-disposition"))
        print("first bytes:", r.content[:4])
        print("no key leak in detail:", "/api/opds/" not in detail)
