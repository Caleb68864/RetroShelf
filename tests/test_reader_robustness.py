"""Reader defects found by the 2026-11-21 bug hunt, each pinned by a test that
failed on the old code: corrupt zips escaping as 500s, EPUB2 named entities,
non-UTF-8 chapters, text outside block elements, stray non-characters in HTML
books, misaligned find snippets, and EPUB/PDF shelving blocking the loop."""
import asyncio
import io
import os
import struct
import tempfile
import time
import zipfile

import pytest
from PIL import Image

from app import reader
from app.errors import ReaderError

CONTAINER = (b'<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:'
             b'xmlns:container"><rootfiles><rootfile full-path="OEBPS/c.opf"/></rootfiles>'
             b'</container>')
OPF = (b'<package xmlns="http://www.idpf.org/2007/opf"><manifest><item id="c1" '
       b'href="ch1.xhtml" media-type="application/xhtml+xml"/></manifest><spine>'
       b'<itemref idref="c1"/></spine></package>')
CHAPTER = (b'<html xmlns="http://www.w3.org/1999/xhtml"><body>'
           + b'<p>hello world</p>' * 2000 + b'</body></html>')


def _tmp_dirs():
    d = tempfile.mkdtemp()
    tmp = os.path.join(d, "t")
    os.makedirs(os.path.join(tmp, "chapters"))
    os.makedirs(os.path.join(tmp, "images"))
    return d, tmp


def _garble(data: bytes, member: str) -> bytes:
    """XOR part of *member*'s deflate stream so inflating it raises zlib.error."""
    buf = bytearray(data)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        info = z.getinfo(member)
    off = info.header_offset + 30 + len(info.filename) + len(info.extra)
    for i in range(off + 5, off + 40):
        buf[i] ^= 0xFF
    return bytes(buf)


def _sanitize(src):
    return reader.sanitize_chapter(src, resolve_image=lambda h: None,
                                   resolve_link=lambda h: None)


# -- corrupt zips are a ReaderError, never a raw zlib/NotImplemented error ----
def test_corrupt_deflate_chapter_is_a_reader_error():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/c.opf", OPF)
        z.writestr("OEBPS/ch1.xhtml", CHAPTER)
    d, tmp = _tmp_dirs()
    path = os.path.join(d, "b.epub")
    with open(path, "wb") as f:
        f.write(_garble(buf.getvalue(), "OEBPS/ch1.xhtml"))
    with pytest.raises(ReaderError):
        reader._extract_epub(path, "k", {"t": "x"}, tmp)


def _png() -> bytes:
    out = io.BytesIO()
    Image.effect_noise((64, 64), 50).convert("RGB").save(out, format="PNG", compress_level=0)
    return out.getvalue()


def test_corrupt_cbz_page_is_skipped_not_a_500():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("001.png", _png())
        z.writestr("002.png", _png())
    d, tmp = _tmp_dirs()
    path = os.path.join(d, "c.cbz")
    with open(path, "wb") as f:
        f.write(_garble(buf.getvalue(), "001.png"))
    manifest = reader._extract_cbz(path, "k", {"t": "x"}, tmp)
    assert len(manifest.chapters) == 1  # the bad page is skipped


def test_unsupported_zip_version_is_a_reader_error():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("001.png", _png())
    data = bytearray(buf.getvalue())
    cd = data.rfind(b"PK\x01\x02")
    struct.pack_into("<H", data, cd + 6, 0xFF)  # "version needed to extract"
    d, tmp = _tmp_dirs()
    for name, fn in (("c.cbz", reader._extract_cbz), ("b.epub", reader._extract_epub)):
        path = os.path.join(d, name)
        with open(path, "wb") as f:
            f.write(bytes(data))
        with pytest.raises(ReaderError):
            fn(path, "k", {"t": "x"}, tmp)


# -- valid EPUB2 named entities render as text, not as escaped source ---------
EPUB2 = b'''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title><style>p{}</style></head>
<body><h1>One&nbsp;Title</h1><p>Hello&nbsp;world</p><p>Second &mdash; para &amp; more</p></body></html>'''


def test_named_html_entities_in_an_epub2_chapter_parse():
    blocks = _sanitize(EPUB2)
    joined = "".join(blocks)
    assert "&lt;" not in joined and "DOCTYPE" not in joined and "p{}" not in joined
    assert "Hello world" in joined and "—" in joined
    assert reader._title_from_heading(EPUB2) == "One Title"


# -- a chapter's declared encoding is honoured -------------------------------
def test_latin1_chapter_is_decoded_by_its_declaration():
    src = ('<?xml version="1.0" encoding="iso-8859-1"?><html><body><p>café</p>'
           '</body></html>').encode("latin-1")
    assert "café" in "".join(_sanitize(src))


def test_utf16_chapter_parses():
    src = '<?xml version="1.0" encoding="utf-16"?><html><body><p>Hi there</p></body></html>'
    blocks = _sanitize(src.encode("utf-16"))
    assert blocks == ["<p>Hi there</p>"]


# -- text outside block elements is kept --------------------------------------
@pytest.mark.parametrize("body, must", [
    ("<div><b>Bold</b> rest of the sentence.</div>", ["Bold", "rest of the sentence."]),
    ("<p>a</p>stray<p>b</p>", ["a", "stray", "b"]),
    ("Just text", ["Just text"]),
    ("<section><p>x</p></section>tail text", ["x", "tail text"]),
])
def test_loose_text_is_not_dropped(body, must):
    joined = "".join(_sanitize(f"<html><body>{body}</body></html>"))
    for text in must:
        assert text in joined


def test_wrapper_without_loose_text_still_splits_into_blocks():
    blocks = _sanitize("<html><body><div><p>a</p><p>b</p></div></body></html>")
    assert blocks == ["<p>a</p>", "<p>b</p>"]


# -- an XML non-character in an HTML book is scrubbed, not fatal --------------
def test_html_book_with_a_noncharacter_still_parses():
    assert reader._scrub_xml("a￾b￿c") == "abc"
    assert reader._scrub_xml("ok é \U0001f600") == "ok é \U0001f600"


# -- find snippets line up when lower() changes a string's length ------------
def test_find_snippet_aligns_with_length_changing_lowercase(monkeypatch):
    monkeypatch.setattr(reader, "load_chapter",
                        lambda c, k, i: ["<p>İİİİ the needle here</p>"])
    hits = reader.search_book("/nonexistent", "k", 1, "needle")
    assert hits and hits[0].match == "needle"


# -- EPUB and PDF extraction run off the event loop ---------------------------
@pytest.mark.parametrize("adapter, target", [
    ("_extract_epub_shelf", "_extract_epub"),
    ("_extract_pdf_shelf", "_extract_pdf"),
])
def test_extraction_does_not_block_the_event_loop(monkeypatch, adapter, target):
    monkeypatch.setattr(reader, target, lambda *a: time.sleep(0.4) or "done")

    async def main():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        t = asyncio.create_task(ticker())
        result = await getattr(reader, adapter)(None, "spool", "k", {}, "tmp")
        t.cancel()
        return result, ticks

    result, ticks = asyncio.run(main())
    assert result == "done"
    assert ticks >= 5
