"""Content shelving leaves out now carries a notice (decided by agent
2026-11-21): skipped images, skipped comic pages, over-deep nesting."""
import asyncio
import io
import os
import tempfile
import zipfile

from app import reader
from tests.test_reader_cbz import make_cbz
from tests.test_reader_html import _BENIGN_HTML, HTML_URL, _shelve, make_html_handler
from tests.test_reader_shelve import make_epub


def _dirs():
    d = tempfile.mkdtemp()
    tmp = os.path.join(d, "t")
    os.makedirs(os.path.join(tmp, "chapters"))
    os.makedirs(os.path.join(tmp, "images"))
    return d, tmp


def _extract(fn, data: bytes, name: str):
    d, tmp = _dirs()
    path = os.path.join(d, name)
    with open(path, "wb") as f:
        f.write(data)
    return fn(path, "k", {"t": "x"}, tmp)


def _replace_member(data: bytes, member: str, new: bytes) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for info in src.infolist():
            z.writestr(info, new if info.filename == member else src.read(info))
    return out.getvalue()


def test_epub_with_an_undecodable_image_carries_a_notice():
    data = _replace_member(make_epub(chapters=1), "OEBPS/images/cover.jpg", b"not an image")
    m = _extract(reader._extract_epub, data, "b.epub")
    assert any("1 image in this book could not be shown" in n for n in m.notices)


def test_clean_epub_has_no_notice():
    assert _extract(reader._extract_epub, make_epub(chapters=2), "b.epub").notices == []


def test_epub_with_over_deep_nesting_carries_a_notice():
    deep = "<div>" * (reader.MAX_NESTING_DEPTH + 5) + "deep" + "</div>" * (reader.MAX_NESTING_DEPTH + 5)
    chapter = f'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>top</p>{deep}</body></html>'
    m = _extract(reader._extract_epub, make_epub(chapters=1, image=False,
                                                 chapter_bytes={0: chapter.encode()}), "b.epub")
    assert any("deeply nested content in 1 chapter" in n for n in m.notices)


def test_cbz_with_an_undecodable_page_carries_a_notice():
    m = _extract(reader._extract_cbz, make_cbz(bad_pages={"page2.jpg": b"garbage"}), "c.cbz")
    assert any("1 page of this comic could not be shown" in n for n in m.notices)


def test_html_book_with_a_missing_image_carries_a_notice(tmp_path):
    handler, _calls = make_html_handler(_BENIGN_HTML, image_bytes=None)  # image 404s
    m = asyncio.run(_shelve(str(tmp_path), handler,
                            {"u": HTML_URL, "m": "text/html", "t": "Web Book"}))
    assert any("1 image in this book could not be shown" in n for n in m.notices)
