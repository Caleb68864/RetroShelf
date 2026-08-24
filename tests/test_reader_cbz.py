"""Tests for in-browser CBZ comic reading (shelving + reader ergonomics).

CBZ fixtures are built in-test with stdlib ``zipfile`` and tiny Pillow-generated
page images — no committed binaries, mirroring
``tests/test_reader_shelve.py::make_epub``. Covers format detection, natural
page ordering, the page-per-chapter shelving model, the off-event-loop extract,
comic reader ergonomics, bookmarks, undecodable/zip-slip handling, and the
additive ``Manifest.kind`` back-compat contract.
"""
from __future__ import annotations

import io
import os
import threading
import zipfile

import pytest
from PIL import Image

from app import reader
from app.download import format_of
from app.errors import ReaderError
from app.opds import Acquisition, Entry
from app.reader import (
    Manifest,
    _manifest_from_dict,
    _manifest_to_dict,
    _natural_sort_key,
    load_chapter,
    shelve_cbz_book,
)
from tests.test_reader_routes import _bid, _token, make_client
from tests.test_reader_shelve import FakeKC

CBZ_URL = "http://kavita:5000/api/download/9003/book.cbz"
CBZ_MEDIA = "application/vnd.comicbook+zip"


def _page_png(width: int, height: int = 12, color: tuple = (30, 90, 180)) -> bytes:
    """A tiny real PNG of a given size, so Pillow can decode it in tests."""
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def make_cbz(
    pages: list[tuple[str, int]] | None = None,
    *,
    comicinfo: bytes | None = None,
    extra: dict[str, bytes] | None = None,
    zip_slip: bool = False,
    bad_pages: dict[str, bytes] | None = None,
) -> bytes:
    """Build a small synthetic CBZ in memory and return its bytes.

    :param pages: ``(member_name, width)`` in the order they are written to the
        zip (deliberately not pre-sorted, so tests can prove natural ordering).
        Each page's width encodes which page it is, so the stored image's width
        after shelving identifies the source page. Defaults to page1/page2/page10
        written in scrambled order.
    :param comicinfo: Raw ``ComicInfo.xml`` bytes to include, or ``None``.
    :param extra: Extra ``{name: bytes}`` members (e.g. a stray ``.txt``).
    :param zip_slip: Add a ``../../evil.jpg`` image member (must be excluded).
    :param bad_pages: ``{name: bytes}`` overrides written verbatim (undecodable).
    """
    if pages is None:
        pages = [("page10.jpg", 100), ("page1.jpg", 10), ("page2.jpg", 20)]
    bad_pages = bad_pages or {}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, width in pages:
            if name in bad_pages:
                zf.writestr(name, bad_pages[name])
            else:
                zf.writestr(name, _page_png(width))
        if comicinfo is not None:
            zf.writestr("ComicInfo.xml", comicinfo)
        for n, d in (extra or {}).items():
            zf.writestr(n, d)
        if zip_slip:
            zf.writestr("../../evil.jpg", _page_png(7))
    return buf.getvalue()


def _record(url: str = CBZ_URL, media: str = CBZ_MEDIA) -> dict:
    return {"u": url, "t": "Test Comic", "a": "Test Artist", "m": media}


def make_cbz_handler(cbz_bytes: bytes, calls: list | None = None):
    """Mock-transport handler serving *cbz_bytes* at the CBZ download path."""
    import httpx

    calls = calls if calls is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/download/9003/book.cbz":
            async def _stream():
                yield cbz_bytes
            return httpx.Response(200, content=_stream())
        return httpx.Response(404, text="nope")

    return handler, calls


# -- detection ----------------------------------------------------------------


def test_format_of_detects_cbz_media_types_and_url_extension():
    assert format_of("application/vnd.comicbook+zip") == "cbz"
    assert format_of("application/x-cbz") == "cbz"
    assert format_of("APPLICATION/VND.COMICBOOK+ZIP") == "cbz"  # case-insensitive
    # .cbz URL-extension fallback (format_of is a plain substring test).
    assert format_of("https://example.test/comics/issue-1.cbz") == "cbz"
    # CBR (a RAR archive) is out of scope and must NOT be mistaken for CBZ.
    assert format_of("application/vnd.comicbook-rar") != "cbz"
    assert format_of("application/x-cbr") != "cbz"
    # EPUB still wins over everything.
    assert format_of("application/epub+zip") == "epub"


def test_acquisition_is_cbz_and_supported_prefers_epub():
    ACQ = "http://opds-spec.org/acquisition"
    assert Acquisition("application/vnd.comicbook+zip", "/x.cbz", ACQ).is_cbz
    assert Acquisition("application/x-cbz", "/x.cbz", ACQ).is_cbz
    assert not Acquisition("application/vnd.comicbook-rar", "/x.cbr", ACQ).is_cbz
    # EPUB preferred when both offered; CBZ-only surfaces the CBZ.
    both = Entry(acquisitions=[
        Acquisition("application/vnd.comicbook+zip", "/x.cbz", ACQ),
        Acquisition("application/epub+zip", "/x.epub", ACQ),
    ])
    assert both.supported_acquisition.is_epub
    cbz_only = Entry(acquisitions=[Acquisition("application/x-cbz", "/x.cbz", ACQ)])
    assert cbz_only.supported_acquisition.is_cbz


# -- natural page ordering ----------------------------------------------------


def test_natural_sort_key_orders_page2_before_page10():
    names = ["page10.jpg", "page1.jpg", "page2.jpg", "page100.jpg"]
    assert sorted(names, key=_natural_sort_key) == [
        "page1.jpg", "page2.jpg", "page10.jpg", "page100.jpg",
    ]
    # Plain lexicographic ordering would (wrongly) put page10 before page2.
    assert sorted(names) != sorted(names, key=_natural_sort_key)


async def test_shelve_orders_pages_naturally_not_lexicographically(tmp_path):
    cache_dir = str(tmp_path / "cache")
    # Written to the zip as 10,1,2 — natural order must yield 1,2,10.
    kc = FakeKC(make_cbz())
    manifest = await shelve_cbz_book(kc, _record(), cache_dir)
    assert manifest.kind == "comic"
    assert [c.title for c in manifest.chapters] == ["Page 1", "Page 2", "Page 3"]

    # Each source page encodes its ordinal in its width (page1=10, page2=20,
    # page10=100), so the stored images prove the true reading order.
    img_dir = os.path.join(cache_dir, "reader", manifest.book_key, "images")
    widths = [Image.open(os.path.join(img_dir, str(i))).size[0] for i in range(3)]
    assert widths == [10, 20, 100]


# -- shelving model -----------------------------------------------------------


async def test_shelve_one_chapter_per_page_with_img_block(tmp_path):
    cache_dir = str(tmp_path / "cache")
    kc = FakeKC(make_cbz())
    manifest = await shelve_cbz_book(kc, _record(), cache_dir)

    assert isinstance(manifest, Manifest)
    assert manifest.version == 2
    assert manifest.kind == "comic"
    assert manifest.title == "Test Comic"
    assert len(manifest.chapters) == 3
    assert manifest.images == 3
    for i in range(3):
        blocks = load_chapter(cache_dir, manifest.book_key, i)
        assert blocks == [f'<img src="{{IMG:{i}}}"/>']  # our own integer markup only
        assert manifest.chapters[i].blocks == 1
        # image + content-type sidecar written per page
        img_dir = os.path.join(cache_dir, "reader", manifest.book_key, "images")
        assert os.path.isfile(os.path.join(img_dir, str(i)))
        assert os.path.isfile(os.path.join(img_dir, f"{i}.ct"))


async def test_shelve_sparse_toc(tmp_path):
    cache_dir = str(tmp_path / "cache")
    pages = [(f"p{i:03d}.jpg", 10) for i in range(25)]
    kc = FakeKC(make_cbz(pages))
    manifest = await shelve_cbz_book(kc, _record(), cache_dir)
    assert len(manifest.chapters) == 25
    # Sparse ToC: page 1, then every 10th (10, 20) — not one entry per page.
    assert [t[2] for t in manifest.toc] == [0, 9, 19]
    assert [t[1] for t in manifest.toc] == ["Page 1", "Page 10", "Page 20"]


async def test_comicinfo_title_used_when_record_has_none(tmp_path):
    cache_dir = str(tmp_path / "cache")
    ci = b"<?xml version='1.0'?><ComicInfo><Series>The Great Comic</Series></ComicInfo>"
    kc = FakeKC(make_cbz(comicinfo=ci))
    rec = {"u": CBZ_URL, "m": CBZ_MEDIA}  # no title in record
    manifest = await shelve_cbz_book(kc, rec, cache_dir)
    assert manifest.title == "The Great Comic"


# -- off-event-loop extraction ------------------------------------------------


async def test_extract_runs_off_the_event_loop(tmp_path, monkeypatch):
    """The CPU-heavy per-page decode must run in a worker thread, not on the
    event loop (transcoding hundreds of pages would otherwise freeze the
    server for every other request)."""
    cache_dir = str(tmp_path / "cache")
    seen: dict[str, bool] = {}
    real_extract = reader._extract_cbz

    def _spy(*args, **kwargs):
        seen["on_main_thread"] = threading.current_thread() is threading.main_thread()
        return real_extract(*args, **kwargs)

    monkeypatch.setattr(reader, "_extract_cbz", _spy)
    kc = FakeKC(make_cbz())
    await shelve_cbz_book(kc, _record(), cache_dir)
    assert seen.get("on_main_thread") is False  # ran off the event loop's thread


# -- undecodable / empty / zip-slip -------------------------------------------


async def test_undecodable_page_skipped_not_fatal(tmp_path):
    cache_dir = str(tmp_path / "cache")
    # Middle page is not a valid image; it is skipped, the comic still shelves.
    kc = FakeKC(make_cbz(
        pages=[("page1.jpg", 10), ("page2.jpg", 20), ("page3.jpg", 30)],
        bad_pages={"page2.jpg": b"not an image at all"},
    ))
    manifest = await shelve_cbz_book(kc, _record(), cache_dir)
    assert len(manifest.chapters) == 2  # the bad page dropped, indices contiguous
    assert manifest.images == 2
    assert [c.title for c in manifest.chapters] == ["Page 1", "Page 2"]


async def test_zero_decodable_pages_raises_friendly_error(tmp_path):
    cache_dir = str(tmp_path / "cache")
    kc = FakeKC(make_cbz(
        pages=[("page1.jpg", 10)],
        bad_pages={"page1.jpg": b"garbage"},
    ))
    with pytest.raises(ReaderError):
        await shelve_cbz_book(kc, _record(), cache_dir)


async def test_no_image_members_raises_friendly_error(tmp_path):
    cache_dir = str(tmp_path / "cache")
    kc = FakeKC(make_cbz(pages=[], extra={"readme.txt": b"no pages here"}))
    with pytest.raises(ReaderError):
        await shelve_cbz_book(kc, _record(), cache_dir)


async def test_zip_slip_member_is_excluded(tmp_path):
    cache_dir = str(tmp_path / "cache")
    kc = FakeKC(make_cbz(
        pages=[("page1.jpg", 10), ("page2.jpg", 20)],
        zip_slip=True,
    ))
    manifest = await shelve_cbz_book(kc, _record(), cache_dir)
    # The ../../evil.jpg member is never read or counted as a page.
    assert manifest.images == 2
    assert len(manifest.chapters) == 2
    # And nothing escaped the book's own cache directory.
    assert not os.path.exists(os.path.join(str(tmp_path), "evil.jpg"))


async def test_no_pillow_cbz_raises_friendly_error(tmp_path, monkeypatch):
    monkeypatch.setattr(reader, "_PIL_AVAILABLE", False)
    cache_dir = str(tmp_path / "cache")
    kc = FakeKC(make_cbz())
    with pytest.raises(ReaderError):
        await shelve_cbz_book(kc, _record(), cache_dir)


# -- Manifest.kind back-compat ------------------------------------------------


def test_manifest_without_kind_loads_as_book():
    """A manifest written before comics existed has no ``kind`` and MUST load
    as an ordinary book — the additive-field back-compat contract."""
    data = _manifest_to_dict(Manifest(
        version=2, book_key="k", title="T", author="A",
        chapters=[], images=0, total_chars=0, created=0.0,
    ))
    del data["kind"]  # simulate a pre-comic manifest.json on disk
    assert _manifest_from_dict(data).kind == "book"
    # An unknown/garbage kind also degrades to "book", never raises.
    data["kind"] = "wat"
    assert _manifest_from_dict(data).kind == "book"


def test_manifest_kind_comic_round_trips():
    m = Manifest(
        version=2, book_key="k", title="T", author="A",
        chapters=[], images=0, total_chars=0, created=0.0, kind="comic",
    )
    assert _manifest_from_dict(_manifest_to_dict(m)).kind == "comic"


# -- end-to-end reader routes -------------------------------------------------


def test_book_page_offers_read_here_and_no_download_button(tmp_path):
    handler, _calls = make_cbz_handler(make_cbz())
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client, url=CBZ_URL, media=CBZ_MEDIA, title="Test Comic")
        r = client.get(f"/book/{bid}")
        assert r.status_code == 200
        assert "Read here" in r.text
        assert ">CBZ<" in r.text or "CBZ" in r.text  # badge shown
        # A CBZ has no iBooks hand-off: no download button at all.
        assert "/download/" not in r.text
        assert "Open in iBooks" not in r.text
        assert "Open PDF" not in r.text


def test_first_open_shelves_and_reads_pages_with_prev_next(tmp_path):
    handler, calls = make_cbz_handler(make_cbz())
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client, url=CBZ_URL, media=CBZ_MEDIA, title="Test Comic")
        r = client.get(f"/read/{bid}", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == f"/read/{bid}/0/1"
        assert calls  # shelving fetched the upstream comic

        # Page 1: image, Next to page 2, no Prev, "Page 1 of 3".
        p1 = client.get(f"/read/{bid}/0/1")
        assert p1.status_code == 200
        assert f'src="/read/{bid}/img/0"' in p1.text
        assert "Page 1 of 3" in p1.text
        assert f"/read/{bid}/1/1" in p1.text  # Next -> page 2
        assert "&laquo; Prev" not in p1.text

        # Page 2: Prev to page 1, Next to page 3.
        p2 = client.get(f"/read/{bid}/1/1")
        assert "Page 2 of 3" in p2.text
        assert f"/read/{bid}/0/1" in p2.text
        assert f"/read/{bid}/2/1" in p2.text

        # The served image bytes are a real image.
        img = client.get(f"/read/{bid}/img/0")
        assert img.status_code == 200
        assert img.headers["content-type"].startswith("image/")


def test_comic_hides_split_and_find_but_book_shows_them(tmp_path):
    # Comic: split-size row and Find link are hidden; position reads "Page N".
    handler, _calls = make_cbz_handler(make_cbz())
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client, url=CBZ_URL, media=CBZ_MEDIA, title="Test Comic")
        text = client.get(f"/read/{bid}/0/1").text
        assert "Part size:" not in text
        assert "Find in book" not in text
        assert "Page 1 of 3" in text
        # Bookmarks + ToC link kept.
        assert "Bookmark this page" in text
        assert f"/read/{bid}/toc" in text

    # Book (EPUB): split-size row and Find link ARE present, position "Ch N".
    from tests.test_reader_routes import BOOK_URL, make_handler
    from tests.test_reader_shelve import make_epub
    handler2, _c = make_handler(make_epub(chapters=2))
    with make_client(handler2, str(tmp_path / "cache2")) as client:
        bid = _bid(client, url=BOOK_URL, media="application/epub+zip")
        text = client.get(f"/read/{bid}/0/1").text
        assert "Part size:" in text
        assert "Find in book" in text
        assert "Ch 1 &middot;" in text or "part 1 of" in text


def test_bookmarks_work_on_a_comic(tmp_path):
    handler, _calls = make_cbz_handler(make_cbz())
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client, url=CBZ_URL, media=CBZ_MEDIA, title="Test Comic")
        token = _token(client)
        client.get(f"/read/{bid}", follow_redirects=False)  # shelve
        # Bookmark page 2 (chapter index 1, block 0).
        r = client.get(
            f"/read/{bid}/bookmark?chapter=1&block=0&part=1&t={token}",
            follow_redirects=False,
        )
        assert r.status_code == 303
        marks = client.get(f"/read/{bid}/bookmarks")
        assert marks.status_code == 200
        assert f"/read/{bid}/1/1" in marks.text


def test_resume_reopens_at_last_read_page(tmp_path):
    handler, _calls = make_cbz_handler(make_cbz())
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client, url=CBZ_URL, media=CBZ_MEDIA, title="Test Comic")
        client.get(f"/read/{bid}/2/1")  # read page 3 -> stores position
        r = client.get(f"/read/{bid}", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == f"/read/{bid}/2/1"
