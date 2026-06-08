"""Fetch a range of real public OPDS feeds and run our parser against each,
reporting what parses and what breaks (so we can harden app.opds)."""
import asyncio

from app import opds
from app.kavita import build_client

# (label, root_url, a_query_url_or_None) — query URL is a direct acquisition feed.
FEEDS = [
    ("ManyBooks", "https://manybooks.net/opds", "https://manybooks.net/opds/search?q=verne"),
    ("Project Gutenberg", "https://www.gutenberg.org/ebooks.opds/", "https://www.gutenberg.org/ebooks/search.opds/?query=verne"),
    ("Gutenberg (m.)", "https://m.gutenberg.org/ebooks.opds/", None),
    ("Standard Ebooks", "https://standardebooks.org/feeds/opds", "https://standardebooks.org/feeds/opds/all"),
    ("Feedbooks PD", "https://catalog.feedbooks.com/catalog/index.atom", None),
    ("Internet Archive", "https://bookserver.archive.org/catalog/", None),
]


async def fetch(client, url):
    r = await client.get(url, headers={"Accept": "application/atom+xml, */*"})
    return r.status_code, r.text


def describe(label, url, body):
    try:
        feed = opds.parse(body)
    except opds.OpdsParseError as exc:
        return f"  PARSE-FAIL: {exc}"
    navs = sum(1 for e in feed.entries if e.is_navigation)
    acqs = [e for e in feed.entries if e.primary_acquisition]
    epub = sum(1 for e in acqs if e.primary_acquisition.is_epub)
    pdf = sum(1 for e in acqs if e.primary_acquisition.is_pdf)
    other = len(acqs) - epub - pdf
    sample = next((e.title for e in feed.entries if e.title), "")
    types = sorted({a.primary_acquisition.media_type for a in acqs})[:5]
    return (f"  title={feed.title!r} entries={len(feed.entries)} nav={navs} "
            f"acq={len(acqs)} epub={epub} pdf={pdf} other={other}\n"
            f"    sample_title={sample!r}\n"
            f"    search={feed.search_url!r}\n"
            f"    acq_types={types}")


async def main():
    client = build_client()
    try:
        for label, root, query in FEEDS:
            print(f"\n=== {label} ===")
            for kind, url in (("root", root), ("acq/search", query)):
                if not url:
                    continue
                try:
                    status, body = await fetch(client, url)
                except Exception as exc:  # noqa: BLE001
                    print(f"  [{kind}] FETCH-ERROR {type(exc).__name__}: {exc}")
                    continue
                head = body.lstrip()[:40].replace("\n", " ")
                if status != 200:
                    print(f"  [{kind}] HTTP {status}  ({head!r})")
                    continue
                if "<feed" not in body[:2000] and "<?xml" not in body[:40]:
                    print(f"  [{kind}] HTTP 200 but not OPDS XML ({head!r})")
                    continue
                print(f"  [{kind}] HTTP 200")
                print(describe(label, url, body))
    finally:
        await client.aclose()


asyncio.run(main())
