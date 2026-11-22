"""Search results page through the library's own next/previous links
(decided by agent 2026-11-21)."""
import html
import re

import httpx

from tests.test_app import ROOT_XML, make_client


def _page(n: int, next_href: str | None, prev_href: str | None = None) -> str:
    links = ""
    if next_href:
        links += f'<link rel="next" href="{next_href}"/>'
    if prev_href:
        links += f'<link rel="previous" href="{prev_href}"/>'
    return f"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<title>Results</title>{links}<entry><title>Result Page {n}</title>
<link rel="http://opds-spec.org/acquisition" type="application/epub+zip"
 href="/dl/{n}.epub"/></entry></feed>"""


def _handler(request):
    p = request.url.path
    if p == "/api/opds/SECRETKEY":
        return httpx.Response(200, text=ROOT_XML)
    if p == "/results/2":
        return httpx.Response(200, text=_page(2, None, "/results/1"))
    if "/search" in p:
        return httpx.Response(200, text=_page(1, "/results/2"))
    return httpx.Response(404)


def _links(text: str, label: str) -> list[str]:
    return [html.unescape(m) for m in re.findall(rf'href="([^"]+)"[^>]*>[^<]*{label}', text)]


def test_search_offers_more_results_and_pages_through_them():
    with make_client(_handler) as client:
        first = client.get("/search?q=time").text
        assert "Result Page 1" in first
        more = _links(first, "More results")
        assert more, "expected a More results link"
        second = client.get(more[0]).text
        assert "Result Page 2" in second and "Result Page 1" not in second
        assert not _links(second, "More results")
        assert _links(second, "Earlier results")


def test_a_forged_or_foreign_page_id_restarts_at_page_one():
    with make_client(_handler) as client:
        ids = client.app.state.ids
        r = client.get("/search", params={"q": "time", "page": "junk"})
        assert r.status_code == 200 and "Result Page 1" in r.text
        foreign = ids.encode("https://evil.example/results/9")
        r = client.get("/search", params={"q": "time", "page": foreign})
        assert r.status_code == 200 and "Result Page 1" in r.text
