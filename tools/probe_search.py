"""Replicate the bridge's search path against live ManyBooks to find the bug."""
import asyncio
from urllib.parse import quote

from app import opds
from app.config import load_config
from app.kavita import KavitaClient, build_client

CFG = load_config({"KAVITA_OPDS_URL": "https://manybooks.net/opds"})


async def main():
    client = build_client()
    kc = KavitaClient(CFG, client)
    try:
        # --- replicate _resolve_search_url ---
        q = "love"
        encoded = quote(q)
        root = opds.parse(await kc.fetch_feed(CFG.kavita_opds_url))
        template = root.search_url
        print("root.search_url template:", template)
        if template and "{searchTerms}" in template:
            url = template.replace("{searchTerms}", encoded)
            while "{" in url and "}" in url:
                s = url.index("{")
                url = url[:s] + url[url.index("}", s) + 1:]
            url = url.rstrip("?&")
        else:
            url = f"{CFG.kavita_opds_url}/search?query={encoded}"
        print("resolved search url:", url)

        # --- fetch + parse ---
        feed = opds.parse(await kc.fetch_feed(url))
        print("feed title:", feed.title)
        print("entries parsed:", len(feed.entries))
        for e in feed.entries[:3]:
            print("   entry:", repr(e.title), "| nav=", e.is_navigation,
                  "| acq=", bool(e.acquisitions), "| nav_href=", e.nav_href)
    finally:
        await client.aclose()


asyncio.run(main())
