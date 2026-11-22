"""EPUB3 media and fixed layout (last deferred item, 2026-11-22).

The browser reader cannot play audio/video or reproduce a fixed layout on
iOS 5 without JS, so it says so: a book-level notice, and an inline note
where each clip was.
"""
import io
import json
import os
import tempfile
import zipfile

from app import reader
from tests.test_old_ipad_guard import _html_violations
from tests.test_reader_shelve import make_epub

MEDIA_CHAPTER = ('<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Listen:</p>'
                 '<audio src="a.mp3" controls="controls"><p>fallback</p></audio>'
                 '<p>Watch <video src="v.mp4" onplay="x()"/> now.</p></body></html>')


def _shelve(data: bytes):
    d = tempfile.mkdtemp()
    tmp = os.path.join(d, "t")
    os.makedirs(os.path.join(tmp, "chapters"))
    os.makedirs(os.path.join(tmp, "images"))
    path = os.path.join(d, "b.epub")
    with open(path, "wb") as f:
        f.write(data)
    m = reader._extract_epub(path, "k", {"t": "x"}, tmp)
    with open(os.path.join(tmp, "chapters", "0.json")) as f:
        return m, "".join(json.load(f)["blocks"])


def _with(data: bytes, *, opf_meta: str = "", extra: dict | None = None) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for info in src.infolist():
            body = src.read(info)
            if info.filename.endswith(".opf") and opf_meta:
                body = body.replace(b"</metadata>", opf_meta.encode() + b"</metadata>")
            z.writestr(info, body)
        for name, body in (extra or {}).items():
            z.writestr(name, body)
    return out.getvalue()


def test_audio_and_video_leave_an_inline_note_and_a_book_notice():
    m, html = _shelve(make_epub(chapters=1, image=False, chapter_bytes={0: MEDIA_CHAPTER.encode()}))
    assert "[Audio clip: not playable in the browser reader]" in html
    assert "[Video clip: not playable in the browser reader]" in html
    assert "<audio" not in html and "<video" not in html and "fallback" not in html
    assert "Listen:" in html and "now." in html
    assert any("audio or video" in n for n in m.notices)
    assert _html_violations(html) == []


def test_media_overlay_items_in_the_manifest_also_give_the_notice():
    data = make_epub(chapters=1, image=False,
                     manifest_extra='<item id="s1" href="s1.smil" media-type="application/smil+xml"/>'
                                    '<item id="a1" href="a1.mp3" media-type="audio/mpeg"/>')
    m, _html = _shelve(data)
    assert any("audio or video" in n for n in m.notices)


def test_fixed_layout_epub3_gets_a_notice():
    data = _with(make_epub(chapters=1, image=False),
                 opf_meta='<meta property="rendition:layout">pre-paginated</meta>')
    m, _html = _shelve(data)
    assert any("fixed-layout" in n for n in m.notices)


def test_fixed_layout_ibooks_option_gets_a_notice():
    opts = (b'<?xml version="1.0"?><display_options><platform name="*">'
            b'<option name="fixed-layout">true</option></platform></display_options>')
    data = _with(make_epub(chapters=1, image=False),
                 extra={"META-INF/com.apple.ibooks.display-options.xml": opts})
    m, _html = _shelve(data)
    assert any("fixed-layout" in n for n in m.notices)


def test_an_ordinary_reflowable_book_has_no_epub3_notice():
    m, _html = _shelve(make_epub(chapters=1, image=False))
    assert not any("fixed-layout" in n or "audio or video" in n for n in m.notices)
