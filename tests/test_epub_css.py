"""Opt-in safe EPUB CSS subset (deferred item, built 2026-11-21).

Only old-Safari-safe, reader-friendly declarations survive; everything that
could break layout (positioning, floats, transforms, colours, fonts, url())
is stripped.
"""
import io
import os
import tempfile
import zipfile

import pytest

from app import reader
from app.epub_css import BookStyles, safe_declarations
from tests.test_old_ipad_guard import _css_violations, _html_violations
from tests.test_reader_shelve import make_epub

BOOK_CSS = """
@import url(fonts.css);
@font-face { font-family: X; src: url(x.otf); }
@media screen { .center { position: fixed } }
body { margin-left: 3em; color: red }
p { text-indent: 1.5em; margin-top: 0; float: left }
.center { text-align: center; position: absolute; top: 0 }
.ital, span.it { font-style: italic }
h2.small { font-size: 80%; display: flex; transform: rotate(3deg) }
div p.deep { font-weight: bold }
.evil { background: url(javascript:alert(1)); font-family: Comic Sans }
.huge { margin-left: 999em; font-size: 50em }
"""


def test_only_allowlisted_declarations_survive():
    assert safe_declarations("text-align:center;position:absolute;color:red") == {
        "text-align": "center"}
    assert safe_declarations("font-style: ITALIC !important") == {"font-style": "italic"}
    assert safe_declarations("margin-left: 999em; text-indent: -1em") == {"text-indent": "-1em"}
    assert safe_declarations('font-weight:bold" onmouseover="x') == {}
    for bad in ("display:grid", "float:left", "width:50vw", "background:url(x)",
                "font-family:serif", "transform:none", "position:sticky", "gap:1em"):
        assert safe_declarations(bad) == {}, bad


def test_book_styles_cascade_and_ignore_unsafe_rules():
    st = BookStyles()
    st.add_stylesheet(BOOK_CSS)
    assert st.style_for("p", None, None) == "text-indent:1.5em;margin-top:0"
    assert st.style_for("p", "center", None) == "text-indent:1.5em;margin-top:0;text-align:center"
    assert st.style_for("span", "it", None) == "font-style:italic"
    assert st.style_for("h2", "small", None) == "font-size:80%"
    assert st.style_for("p", "deep", None) == "text-indent:1.5em;margin-top:0"  # combinator ignored
    assert st.style_for("div", "evil huge", None) == ""
    assert st.style_for("body", None, None) == ""
    assert st.style_for("p", None, "text-align:right;position:fixed") == \
        "text-indent:1.5em;margin-top:0;text-align:right"
    assert not _css_violations("a{" + st.style_for("h2", "small center", "font-weight:bold") + "}")


def _styled_epub() -> bytes:
    chapter = ('<html xmlns="http://www.w3.org/1999/xhtml"><head>'
               '<link rel="stylesheet" href="style.css"/></head><body>'
               '<h2 class="center">Title</h2><p>Body <span class="ital">it</span></p>'
               '<p style="text-align:right;position:absolute">R</p></body></html>')
    src = zipfile.ZipFile(io.BytesIO(make_epub(
        chapters=1, image=False, chapter_bytes={0: chapter.encode()},
        manifest_extra='<item id="css" href="style.css" media-type="text/css"/>')))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for info in src.infolist():
            z.writestr(info, src.read(info))
        z.writestr("OEBPS/style.css", BOOK_CSS)
    return out.getvalue()


def _shelve(data: bytes):
    d = tempfile.mkdtemp()
    tmp = os.path.join(d, "t")
    os.makedirs(os.path.join(tmp, "chapters"))
    os.makedirs(os.path.join(tmp, "images"))
    path = os.path.join(d, "b.epub")
    with open(path, "wb") as f:
        f.write(data)
    m = reader._extract_epub(path, "k", {"t": "x"}, tmp)
    import json
    with open(os.path.join(tmp, "chapters", "0.json")) as f:
        return m, "".join(json.load(f)["blocks"])


def test_epub_css_on_keeps_the_safe_subset(monkeypatch):
    monkeypatch.setattr(reader, "EPUB_CSS_ENABLED", True)
    m, html = _shelve(_styled_epub())
    assert m.epub_css is True
    assert '<h2 style="text-align:center">' in html
    assert '<span style="font-style:italic">it</span>' in html
    assert 'style="text-indent:1.5em;margin-top:0;text-align:right"' in html
    assert "position" not in html and "url(" not in html
    assert _html_violations(html) == []


def test_epub_css_off_by_default_strips_all_styles():
    m, html = _shelve(_styled_epub())
    assert m.epub_css is False
    assert "style=" not in html


def test_switching_the_setting_reshelves_epubs_only(monkeypatch):
    m = reader.Manifest(version=2, book_key="k", title="t", author="", images=0,
                        total_chars=0, created=0.0, chapters=[], epub_css=False)
    d = reader._manifest_to_dict(m)
    assert reader._manifest_from_dict(d).epub_css is False
    m2 = reader.Manifest(version=2, book_key="k", title="t", author="", images=0,
                         total_chars=0, created=0.0, chapters=[])
    assert "epub_css" not in reader._manifest_to_dict(m2)  # non-EPUB shape unchanged


def test_load_manifest_drops_an_epub_shelved_with_the_other_setting(tmp_path, monkeypatch):
    import json
    book = tmp_path / "reader" / "k"
    book.mkdir(parents=True)
    m = reader.Manifest(version=2, book_key="k", title="t", author="", images=0,
                        total_chars=0, created=0.0,
                        chapters=[reader.ChapterMeta("a", 1, 1)], epub_css=False)
    (book / "manifest.json").write_text(json.dumps(reader._manifest_to_dict(m)))
    assert reader.load_manifest(str(tmp_path), "k") is not None
    monkeypatch.setattr(reader, "EPUB_CSS_ENABLED", True)
    assert reader.load_manifest(str(tmp_path), "k") is None
