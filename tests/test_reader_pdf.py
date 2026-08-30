"""Tests for in-browser PDF reading (text reflow — the ``shelve_pdf_book`` path).

Every fixture is a synthetic PDF built in-test by :func:`_make_pdf` — no
committed binaries, no reportlab. ``_make_pdf`` emits a valid multi-page
text-layer PDF (pure bytes, adapted from the project's PDF spike) and can add a
nested document outline (bookmarks); pages given ``""`` carry no text layer, so
the same builder produces the scanned/image-only fixture for the no-text
fallback. Kavita is mocked at the httpx transport layer (a real
:class:`~app.kavita.KavitaClient`, so the genuine SSRF guard runs), mirroring
``tests/test_reader_routes.py``'s harness.

The critical case is the escape seam: extracted PDF text is PLAIN TEXT and must
reach served pages HTML-escaped — a page whose text literally contains
``<script>`` renders as visible characters, never as surviving markup.
"""
from __future__ import annotations

import io
import json

import httpx

from app.config import load_config
from app.errors import PdfNoTextError, ReaderError
from app.kavita import KavitaClient
from app.reader import Manifest, load_chapter, shelve_pdf_book
from tests.test_reader_routes import ENV, make_client

ORIGIN = "http://kavita:5000"
PDF_URL = f"{ORIGIN}/api/download/7001/book.pdf"


# ---------------------------------------------------------------------------
# In-test PDF builder (adapted from scratchpad/pdfspike.py + an outline)
# ---------------------------------------------------------------------------


def _make_pdf(pages_text: list[str], outline: list | None = None) -> bytes:
    """Build a valid text-layer PDF in pure bytes.

    :param pages_text: One line of text per page; ``""`` yields a page with no
        text layer (an image-only/scanned page).
    :param outline: Optional list of ``(title, page_index, children)`` tuples
        (children recursively the same) building a nested document outline.
    :returns: The PDF file bytes.
    """
    n = 2
    content_ids: list[int] = []
    page_ids: list[int] = []
    for _ in pages_text:
        n += 1
        content_ids.append(n)
        n += 1
        page_ids.append(n)
    n += 1
    font_id = n

    bodies: dict[int, bytes] = {}
    kids = b" ".join(b"%d 0 R" % pid for pid in page_ids)
    bodies[2] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_ids))
    for i, text in enumerate(pages_text):
        stream = (
            f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode("latin-1") if text else b""
        )
        bodies[content_ids[i]] = b"<< /Length %d >>\nstream\n%s\nendstream" % (
            len(stream), stream,
        )
        bodies[page_ids[i]] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (font_id, content_ids[i])
        )
    bodies[font_id] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    outlines_root_id: int | None = None
    if outline:
        items: list[dict] = []

        def alloc(title: str, page_index: int, children: list, parent_id: int) -> int:
            nonlocal n
            n += 1
            my_id = n
            node = {"id": my_id, "title": title, "page": page_index,
                    "parent": parent_id, "children": []}
            items.append(node)
            for (ct, cp, cc) in children:
                node["children"].append(alloc(ct, cp, cc, my_id))
            return my_id

        n += 1
        outlines_root_id = n
        roots = [alloc(t, p, c, outlines_root_id) for (t, p, c) in outline]
        by_id = {it["id"]: it for it in items}

        def sib_body(node: dict, siblings: list[int], idx: int) -> bytes:
            first = node["children"][0] if node["children"] else None
            last = node["children"][-1] if node["children"] else None
            parts = [b"/Title (%s)" % node["title"].encode("latin-1"),
                     b"/Parent %d 0 R" % node["parent"],
                     b"/Dest [%d 0 R /Fit]" % page_ids[node["page"]]]
            if idx > 0:
                parts.append(b"/Prev %d 0 R" % siblings[idx - 1])
            if idx < len(siblings) - 1:
                parts.append(b"/Next %d 0 R" % siblings[idx + 1])
            if first:
                parts.append(b"/First %d 0 R" % first)
                parts.append(b"/Last %d 0 R" % last)
                parts.append(b"/Count %d" % len(node["children"]))
            return b"<< %s >>" % b" ".join(parts)

        def emit(node_ids: list[int]) -> None:
            for idx, nid in enumerate(node_ids):
                node = by_id[nid]
                bodies[nid] = sib_body(node, node_ids, idx)
                if node["children"]:
                    emit(node["children"])

        emit(roots)
        bodies[outlines_root_id] = (
            b"<< /Type /Outlines /First %d 0 R /Last %d 0 R /Count %d >>"
            % (roots[0], roots[-1], len(roots))
        )

    if outlines_root_id:
        bodies[1] = b"<< /Type /Catalog /Pages 2 0 R /Outlines %d 0 R >>" % outlines_root_id
    else:
        bodies[1] = b"<< /Type /Catalog /Pages 2 0 R >>"

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num in range(1, n + 1):
        offsets[num] = out.tell()
        out.write(b"%d 0 obj\n%s\nendobj\n" % (num, bodies[num]))
    xref_pos = out.tell()
    count = n + 1
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % count)
    for num in range(1, n + 1):
        out.write(b"%010d 00000 n \n" % offsets[num])
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (count, xref_pos)
    )
    return out.getvalue()


# -- fixtures ----------------------------------------------------------------

_PLAIN_PDF = _make_pdf(["Call me Ishmael", "The whale surfaced", "The very end"])

_OUTLINE_PDF = _make_pdf(
    ["Front matter page", "Chapter one body", "Chapter one more",
     "Chapter two body", "Chapter two tail"],
    outline=[
        ("Chapter One", 1, [("Section A", 2, [])]),
        ("Chapter Two", 3, []),
    ],
)

# A page whose text layer literally contains a script tag: it MUST render as
# visible characters, never as surviving/executed markup.
_XSS_PDF = _make_pdf(["Before <script>alert(1)</script> after"])

# No text layer on any page — a scanned / image-only PDF.
_SCANNED_PDF = _make_pdf(["", ""])


def make_pdf_handler(pdf_bytes: bytes, calls: list | None = None):
    """Build a mock-transport handler serving *pdf_bytes* at :data:`PDF_URL`."""
    calls = calls if calls is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.host, request.url.path))
        if request.url.host == "kavita" and request.url.path == "/api/download/7001/book.pdf":
            async def _pdf():
                yield pdf_bytes
            return httpx.Response(200, content=_pdf(),
                                  headers={"content-type": "application/pdf"})
        return httpx.Response(404, text="nope")

    return handler, calls


def _record(url: str = PDF_URL, media: str = "application/pdf") -> dict:
    return {"u": url, "m": media, "t": "A PDF Book", "a": "P Author"}


def _pdf_bid(client, *, url: str = PDF_URL, media: str = "application/pdf",
             title: str = "A PDF Book", author: str = "P Author") -> str:
    rec = {"u": url, "m": media, "t": title, "a": author, "s": "", "c": None}
    return client.app.state.ids.encode(json.dumps(rec, separators=(",", ":")))


async def _shelve(cache_dir: str, handler, record: dict) -> Manifest:
    """Shelve *record* through a real KavitaClient over *handler*'s transport."""
    cfg = load_config({**ENV, "CACHE_DIR": cache_dir})
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(connect=5, read=None, write=None, pool=5),
    ) as http:
        kc = KavitaClient(cfg, http)
        return await shelve_pdf_book(kc, record, cache_dir)


# -- shelving: text extraction, manifest shape, outline -> ToC ---------------


async def test_shelve_pdf_extracts_text_into_manifest(tmp_path):
    cache_dir = str(tmp_path / "cache")
    handler, calls = make_pdf_handler(_PLAIN_PDF)
    manifest = await _shelve(cache_dir, handler, _record())

    assert isinstance(manifest, Manifest)
    assert manifest.version == 2
    assert manifest.title == "A PDF Book"
    assert manifest.images == 0  # v1: text reflow only
    assert calls  # shelving fetched the upstream PDF
    # A short, outline-less PDF becomes one chapter titled by the book.
    assert len(manifest.chapters) == 1
    assert manifest.chapters[0].title == "A PDF Book"
    assert manifest.total_chars == sum(c.chars for c in manifest.chapters)

    served = "".join(load_chapter(cache_dir, manifest.book_key, 0))
    assert "Call me Ishmael" in served
    assert "The whale surfaced" in served
    assert "The very end" in served


async def test_outline_pdf_builds_nested_chapters_and_toc(tmp_path):
    cache_dir = str(tmp_path / "cache")
    handler, _calls = make_pdf_handler(_OUTLINE_PDF)
    manifest = await _shelve(cache_dir, handler, _record())

    # Each distinct bookmark page starts its own chapter (leading matter is
    # page 0 with no bookmark): Chapter One (page 1), its nested Section A
    # (page 2), then Chapter Two (page 3-4).
    titles = [c.title for c in manifest.chapters]
    assert titles == ["A PDF Book", "Chapter One", "Section A", "Chapter Two"]
    # The nested ToC carries outline depth: Section A (a child of Chapter One)
    # sits one level deeper, and every entry maps to the chapter it opens.
    assert manifest.toc == [(0, "Chapter One", 1), (1, "Section A", 2), (0, "Chapter Two", 3)]

    # Each chapter holds exactly its page range's text.
    ch1 = "".join(load_chapter(cache_dir, manifest.book_key, 1))
    assert "Chapter one body" in ch1 and "Chapter one more" not in ch1
    sec_a = "".join(load_chapter(cache_dir, manifest.book_key, 2))
    assert "Chapter one more" in sec_a
    ch2 = "".join(load_chapter(cache_dir, manifest.book_key, 3))
    assert "Chapter two body" in ch2 and "Chapter two tail" in ch2


# -- the critical security test: extracted text is ESCAPED, not markup -------


async def test_pdf_script_text_is_escaped_not_executable(tmp_path):
    cache_dir = str(tmp_path / "cache")
    handler, _calls = make_pdf_handler(_XSS_PDF)
    manifest = await _shelve(cache_dir, handler, _record())

    blocks = load_chapter(cache_dir, manifest.book_key, 0)
    served = "".join(blocks)
    # The visible characters survive, escaped — no executable/real <script> tag.
    assert "Before" in served and "after" in served
    assert "&lt;script&gt;" in served
    assert "<script>" not in served
    assert "alert(1)" in served  # the text content is visible, just inert
    # Escaped-text fallback wraps content in <p>, never re-parsed markup.
    assert all(b.startswith("<p>") for b in blocks)


def test_read_route_serves_escaped_script_text(tmp_path):
    handler, _calls = make_pdf_handler(_XSS_PDF)
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _pdf_bid(client)
        r = client.get(f"/read/{bid}", follow_redirects=False)
        assert r.status_code == 303
        part = client.get(r.headers["location"])
        assert part.status_code == 200
        # Rendered page shows the literal text, with no live script element.
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in part.text
        assert "<script>alert(1)</script>" not in part.text


# -- routes: dual book page, pagination, search, bookmarks -------------------


def test_pdf_book_page_offers_both_read_here_and_open_pdf(tmp_path):
    handler, _calls = make_pdf_handler(_PLAIN_PDF)
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _pdf_bid(client)
        r = client.get(f"/book/{bid}")
        assert r.status_code == 200
        # DUAL path: the reflow reader button AND the native Open-PDF download.
        assert f"/read/{bid}" in r.text        # "Read here" reader button
        assert "Open PDF" in r.text            # native download button kept
        assert "/download/" in r.text          # a real download link is present
        assert "badge-pdf" in r.text           # PDF format badge


def test_pdf_reads_with_pagination(tmp_path):
    # A many-page PDF: "small" split paginates it into multiple parts.
    big = _make_pdf([f"Page {i} " + ("lorem ipsum dolor sit amet " * 60)
                     for i in range(8)])
    handler, _calls = make_pdf_handler(big)
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _pdf_bid(client)
        client.get(f"/read/{bid}", follow_redirects=False)  # shelve

        client.cookies.set("rs_split", "small")
        assert client.get(f"/read/{bid}/0/1").status_code == 200
        assert client.get(f"/read/{bid}/0/2").status_code == 200  # a 2nd part exists

        # "whole" collapses the single chapter into one part -> part 2 is 404.
        client.cookies.set("rs_split", "whole")
        assert client.get(f"/read/{bid}/0/1").status_code == 200
        assert client.get(f"/read/{bid}/0/2").status_code == 404


def test_search_finds_text_in_pdf(tmp_path):
    handler, _calls = make_pdf_handler(_PLAIN_PDF)
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _pdf_bid(client)
        client.get(f"/read/{bid}", follow_redirects=False)  # shelve
        r = client.get(f"/read/{bid}/find", params={"q": "whale surfaced"})
        assert r.status_code == 200
        assert f"/read/{bid}/" in r.text  # a hit links into the book


def test_bookmark_roundtrip_on_pdf(tmp_path):
    handler, _calls = make_pdf_handler(_PLAIN_PDF)
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _pdf_bid(client)
        client.get(f"/read/{bid}", follow_redirects=False)  # shelve
        token = client.app.state.ids.site_token
        add = client.get(f"/read/{bid}/bookmark",
                         params={"chapter": 0, "block": 0, "part": 1, "t": token},
                         follow_redirects=False)
        assert add.status_code == 303
        marks = client.get(f"/read/{bid}/bookmarks")
        assert marks.status_code == 200
        assert "A PDF Book" in marks.text


# -- no-text-layer fallback (scanned PDF) ------------------------------------


async def test_scanned_pdf_shelve_raises_pdf_no_text(tmp_path):
    cache_dir = str(tmp_path / "cache")
    handler, _calls = make_pdf_handler(_SCANNED_PDF)
    try:
        await _shelve(cache_dir, handler, _record())
        raised = False
    except PdfNoTextError:
        raised = True
    assert raised


def test_scanned_pdf_read_route_shows_friendly_open_pdf_page(tmp_path):
    handler, _calls = make_pdf_handler(_SCANNED_PDF)
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _pdf_bid(client)
        r = client.get(f"/read/{bid}", follow_redirects=False)
        # Not a 500, not a redirect into a (non-existent) reader position.
        assert r.status_code == 200
        assert "no readable text layer" in r.text
        assert "Open PDF" in r.text
        assert "/download/" in r.text  # the native inline-view link is offered


# -- friendly failures (never a raw 500) -------------------------------------


async def test_encrypted_pdf_raises_reader_error(tmp_path, monkeypatch):
    # Simulate an encrypted PDF: pypdf reports is_encrypted -> steer to Open PDF.
    import app.reader as reader

    class _FakeReader:
        is_encrypted = True
        pages: list = []

    monkeypatch.setattr(reader._pypdf, "PdfReader", lambda *a, **k: _FakeReader())
    cache_dir = str(tmp_path / "cache")
    handler, _calls = make_pdf_handler(_PLAIN_PDF)
    try:
        await _shelve(cache_dir, handler, _record())
        raised = False
    except ReaderError as exc:
        raised = "protected" in str(exc)
    assert raised


async def test_corrupt_pdf_raises_reader_error(tmp_path):
    cache_dir = str(tmp_path / "cache")
    handler, _calls = make_pdf_handler(b"%PDF-1.4 this is not a real pdf at all")
    try:
        await _shelve(cache_dir, handler, _record())
        raised = False
    except ReaderError:
        raised = True
    assert raised


async def test_oversized_pdf_raises_reader_error(tmp_path, monkeypatch):
    import app.reader as reader
    monkeypatch.setattr(reader, "MAX_PDF_BYTES", 64)  # smaller than the doc
    cache_dir = str(tmp_path / "cache")
    handler, _calls = make_pdf_handler(_PLAIN_PDF)
    try:
        await _shelve(cache_dir, handler, _record())
        raised = False
    except ReaderError:
        raised = True
    assert raised


# -- hostile outlines: depth bombs and entry floods ---------------------------


def test_outline_depth_bomb_returns_empty_not_recursion_error():
    # pypdf represents nesting as nested lists; 500 levels must hit the walk
    # guard, not the Python recursion limit. No leaf destinations are ever
    # resolved, so no reader object is needed.
    from app import reader as r
    outline: list = []
    for _ in range(500):
        outline = [outline]
    assert r._flatten_pdf_outline(None, outline) == []


def test_outline_entry_flood_and_giant_titles_are_capped():
    from app import reader as r

    class _FakeDest:
        def __init__(self, title: str) -> None:
            self.title = title

    class _FakeReader:
        def get_destination_page_number(self, _item) -> int:
            return 0

    flood = [_FakeDest("T" * 10_000) for _ in range(r.MAX_TOC_ENTRIES + 500)]
    entries = r._flatten_pdf_outline(_FakeReader(), flood)
    assert len(entries) == r.MAX_TOC_ENTRIES
    assert all(len(t) <= r.MAX_TITLE_CHARS for _d, t, _p in entries)
