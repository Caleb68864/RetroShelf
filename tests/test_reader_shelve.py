"""Tests for :mod:`app.reader`'s EPUB parsing/shelving pipeline (SS-02).

Every fixture is built in-test with stdlib ``zipfile`` — no committed binary
EPUB fixtures — mirroring ``tests/test_download_headers.py::FakeUpstream``
for the fake upstream transport.
"""
from __future__ import annotations

import asyncio
import io
import os
import struct
import zipfile

import httpx
import pytest
from PIL import Image

from app import reader
from app.errors import ReaderError
from app.reader import Manifest, load_chapter, load_manifest, prune_reader_cache, shelve_book
from app.store import book_key

FIXTURE_URL = "https://library.example.test/download/9001/s3cr3t-token/book.epub"

_CONTAINER_XML = b"""<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

_NAV_TEMPLATE = """<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Nav</title></head>
<body>
<nav epub:type="toc">
<ol>
{items}
</ol>
</nav>
</body>
</html>
"""

_NCX_TEMPLATE = """<?xml version="1.0"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
<navMap>
{points}
</navMap>
</ncx>
"""

_CHAPTER_TEMPLATE = """<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<body>
<h1>Nav Chapter {i}</h1>
<p>Body text for chapter {i}.</p>
{extra}
</body>
</html>
"""


def _cover_jpeg_bytes() -> bytes:
    """A tiny real JPEG, so Pillow can open/downscale it in tests."""
    buf = io.BytesIO()
    Image.new("RGB", (2000, 10), (200, 40, 40)).save(buf, format="JPEG")
    return buf.getvalue()


def _patch_encrypted_flag(data: bytes, member: str) -> bytes:
    """Flip the general-purpose "encrypted" bit (bit 0) on *member*'s
    **central directory** record, simulating zip-layer (traditional
    PKWARE) encryption without OCF ``encryption.xml``.

    ``zipfile.ZipFile.open()`` reads this flag from the central directory
    entry (not the local file header) and raises a bare ``RuntimeError``
    ("... is encrypted, password required ...") when it is set and no
    password is supplied — a genuinely DRM'd book, distinct from the
    ``encryption.xml``-declared DRM path.
    """
    buf = bytearray(data)
    target = member.encode("utf-8")
    sig = b"PK\x01\x02"
    i = 0
    patched = False
    while True:
        i = buf.find(sig, i)
        if i == -1:
            break
        name_len = struct.unpack_from("<H", buf, i + 28)[0]
        name = bytes(buf[i + 46 : i + 46 + name_len])
        if name == target:
            flag_off = i + 8
            flags = struct.unpack_from("<H", buf, flag_off)[0]
            struct.pack_into("<H", buf, flag_off, flags | 0x1)
            patched = True
        i += 4
    assert patched, f"{member!r} not found in central directory"
    return bytes(buf)


def make_epub(
    *,
    chapters: int = 3,
    image: bool = True,
    ncx: bool = True,
    encryption: bool = False,
    zip_slip: bool = False,
    chapter_bytes: dict | None = None,
    manifest_extra: str = "",
    spine_extra: str = "",
) -> bytes:
    """Build a small synthetic EPUB in memory and return its bytes.

    :param chapters: Number of spine chapters to generate.
    :param image: Whether to include a cover image referenced from
        chapter 0.
    :param ncx: Whether to include a ``toc.ncx``.
    :param encryption: Whether to include ``META-INF/encryption.xml``
        (simulating DRM).
    :param zip_slip: Whether to add a ``../../evil.txt`` member.
    :param chapter_bytes: Optional ``{index: raw_bytes}`` override for
        specific chapter contents (e.g. malformed markup).
    :param manifest_extra: Raw XML injected into the OPF ``<manifest>``.
    :param spine_extra: Raw XML injected into the OPF ``<spine>``.
    """
    chapter_bytes = chapter_bytes or {}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", _CONTAINER_XML)
        if encryption:
            zf.writestr("META-INF/encryption.xml", b"<encryption/>")
        if zip_slip:
            zf.writestr("../../evil.txt", b"pwned")

        manifest_items = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>']
        if ncx:
            manifest_items.append('<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>')
        if image:
            manifest_items.append('<item id="img0" href="images/cover.jpg" media-type="image/jpeg"/>')

        nav_items = []
        ncx_points = []
        spine_refs = []
        for i in range(chapters):
            manifest_items.append(
                f'<item id="ch{i}" href="ch{i}.xhtml" media-type="application/xhtml+xml"/>'
            )
            spine_refs.append(f'<itemref idref="ch{i}"/>')
            nav_items.append(f'<li><a href="ch{i}.xhtml">Nav Chapter {i}</a></li>')
            ncx_points.append(
                f'<navPoint id="np{i}"><navLabel><text>NCX Chapter {i}</text></navLabel>'
                f'<content src="ch{i}.xhtml"/></navPoint>'
            )
            if i in chapter_bytes:
                body = chapter_bytes[i]
            else:
                extra = '<img src="images/cover.jpg" alt="Cover"/>' if (image and i == 0) else ""
                body = _CHAPTER_TEMPLATE.format(i=i, extra=extra).encode("utf-8")
            zf.writestr(f"OEBPS/ch{i}.xhtml", body)

        manifest_items.append(manifest_extra)
        opf = f"""<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Test Book</dc:title></metadata>
  <manifest>
    {"".join(manifest_items)}
  </manifest>
  <spine{' toc="ncx"' if ncx else ""}>
    {"".join(spine_refs)}{spine_extra}
  </spine>
</package>
"""
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/nav.xhtml", _NAV_TEMPLATE.format(items="\n".join(nav_items)))
        if ncx:
            zf.writestr("OEBPS/toc.ncx", _NCX_TEMPLATE.format(points="\n".join(ncx_points)))
        if image:
            zf.writestr("OEBPS/images/cover.jpg", _cover_jpeg_bytes())
    return buf.getvalue()


class FakeUpstream:
    """Mirrors ``tests/test_download_headers.py::FakeUpstream``."""

    def __init__(self, body: bytes, headers: dict | None = None, chunk_size: int = 4096):
        self.status_code = 200
        self.headers = httpx.Headers(headers or {})
        self._body = body
        self._chunk_size = chunk_size
        self.closed = False

    async def aiter_raw(self):
        for i in range(0, len(self._body), self._chunk_size):
            yield self._body[i : i + self._chunk_size]

    async def aclose(self):
        self.closed = True


class FakeKC:
    """Fake ``KavitaClient`` counting ``open_stream`` calls."""

    def __init__(self, body: bytes, headers: dict | None = None):
        self._body = body
        self._headers = headers or {}
        self.calls = 0

    async def open_stream(self, url, *, range_header=None):
        self.calls += 1
        return FakeUpstream(self._body, self._headers)


def _record(url: str = FIXTURE_URL) -> dict:
    return {"u": url, "t": "Test Book", "a": "Test Author", "m": "application/epub+zip"}


def _walk_text(root: str):
    """Yield the decoded text content of every file under *root*."""
    for base, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(base, name)
            try:
                with open(path, encoding="utf-8") as f:
                    yield f.read()
            except (UnicodeDecodeError, OSError):
                continue


# -- happy path ---------------------------------------------------------------


async def test_shelve_happy_path(tmp_path):
    cache_dir = str(tmp_path / "cache")
    kc = FakeKC(make_epub(chapters=3, image=True, ncx=True))
    manifest = await shelve_book(kc, _record(), cache_dir)

    assert isinstance(manifest, Manifest)
    assert manifest.version == 2
    assert manifest.title == "Test Book"
    assert manifest.author == "Test Author"
    assert len(manifest.chapters) == 3
    assert manifest.images == 1
    assert manifest.total_chars == sum(c.chars for c in manifest.chapters)

    book_dir = os.path.join(cache_dir, "reader", manifest.book_key)
    assert os.path.isfile(os.path.join(book_dir, "manifest.json"))
    for i in range(3):
        assert os.path.isfile(os.path.join(book_dir, "chapters", f"{i}.json"))
    assert os.path.isfile(os.path.join(book_dir, "images", "0"))
    assert os.path.isfile(os.path.join(book_dir, "images", "0.ct"))

    # Chapter titles resolved from the nav doc.
    assert manifest.chapters[0].title == "Nav Chapter 0"
    # Chapter 0 references the cover image; the placeholder survives sanitization.
    blocks = load_chapter(cache_dir, manifest.book_key, 0)
    assert any("{IMG:0}" in b for b in blocks)


async def test_shelve_no_pillow_is_text_only(tmp_path, monkeypatch):
    monkeypatch.setattr(reader, "_PIL_AVAILABLE", False)
    cache_dir = str(tmp_path / "cache")
    kc = FakeKC(make_epub(chapters=1, image=True))
    manifest = await shelve_book(kc, _record(), cache_dir)
    assert manifest.images == 0
    blocks = load_chapter(cache_dir, manifest.book_key, 0)
    assert not any("{IMG:" in b for b in blocks)


async def test_oversized_image_is_skipped_not_fatal(tmp_path, monkeypatch):
    # An image larger than the per-image source cap must be dropped (book
    # stays readable, text-only for that image), never fail the whole book.
    monkeypatch.setattr(reader, "MAX_IMAGE_SRC_BYTES", 8)  # cover jpeg is bigger
    cache_dir = str(tmp_path / "cache")
    kc = FakeKC(make_epub(chapters=2, image=True))
    manifest = await shelve_book(kc, _record(), cache_dir)
    assert manifest.images == 0
    assert len(manifest.chapters) == 2
    # The dropped image's placeholder does not survive into served blocks.
    blocks = load_chapter(cache_dir, manifest.book_key, 0)
    assert not any("{IMG:" in b for b in blocks)


async def test_image_count_is_capped(tmp_path, monkeypatch):
    # The per-book image-attempt ceiling bounds decode work; at zero, no
    # image is extracted even though the EPUB declares one, and the book
    # still shelves cleanly.
    monkeypatch.setattr(reader, "MAX_IMAGES", 0)
    cache_dir = str(tmp_path / "cache")
    kc = FakeKC(make_epub(chapters=1, image=True))
    manifest = await shelve_book(kc, _record(), cache_dir)
    assert manifest.images == 0
    assert len(manifest.chapters) == 1


def test_skipped_oversized_image_still_charges_the_budget(monkeypatch):
    # A skipped (too-large) image must still charge the global unpacked budget,
    # so a "many oversized images" archive cannot read past the ceiling by
    # having every image individually skipped.
    monkeypatch.setattr(reader, "MAX_IMAGE_SRC_BYTES", 8)
    buf = io.BytesIO()
    payload = b"x" * 4096
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("images/big.bin", payload)
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
        budget = reader._UnpackBudget(reader.MAX_UNPACKED_BYTES)
        result = reader._read_image_member(zf, "images/big.bin", budget)
    assert result is None                 # over the per-image cap -> skipped
    assert budget.used >= reader.MAX_IMAGE_SRC_BYTES  # but the read was charged


# -- zip-slip -------------------------------------------------------------


async def test_zip_slip_contained(tmp_path):
    cache_dir = str(tmp_path / "cache")
    kc = FakeKC(make_epub(chapters=2, zip_slip=True))
    manifest = await shelve_book(kc, _record(), cache_dir)
    assert len(manifest.chapters) == 2

    # Nothing was ever written outside {cache_dir}/reader/, and the evil
    # member name never became a file on disk anywhere in the tree.
    reader_root = os.path.join(cache_dir, "reader")
    for base, _dirs, files in os.walk(str(tmp_path)):
        for name in files:
            full = os.path.join(base, name)
            assert full.startswith(reader_root)
            assert "evil" not in name


# -- resource caps ----------------------------------------------------------


async def test_oversized_epub_declared_length(tmp_path, monkeypatch):
    monkeypatch.setattr(reader, "MAX_EPUB_BYTES", 100)
    cache_dir = str(tmp_path / "cache")
    body = make_epub(chapters=1)
    kc = FakeKC(body, headers={"Content-Length": str(len(body) * 10)})
    with pytest.raises(ReaderError) as exc:
        await shelve_book(kc, _record(), cache_dir)
    assert "too large" in str(exc.value)
    # spool file cleaned up
    assert not os.path.exists(os.path.join(cache_dir, "reader", f".spool-{os.getpid()}-{reader._store_book_key(FIXTURE_URL)}"))


async def test_oversized_epub_actual_length(tmp_path, monkeypatch):
    monkeypatch.setattr(reader, "MAX_EPUB_BYTES", 100)
    cache_dir = str(tmp_path / "cache")
    body = make_epub(chapters=3, image=True)
    assert len(body) > 100
    kc = FakeKC(body)
    with pytest.raises(ReaderError) as exc:
        await shelve_book(kc, _record(), cache_dir)
    assert "exceeds" in str(exc.value)


async def test_too_many_spine_items(tmp_path, monkeypatch):
    monkeypatch.setattr(reader, "MAX_SPINE_ITEMS", 2)
    cache_dir = str(tmp_path / "cache")
    kc = FakeKC(make_epub(chapters=3))
    with pytest.raises(ReaderError) as exc:
        await shelve_book(kc, _record(), cache_dir)
    assert "too many chapters" in str(exc.value)


async def test_zero_readable_chapters(tmp_path):
    cache_dir = str(tmp_path / "cache")
    # Spine references idrefs that do not resolve to any manifest item.
    kc = FakeKC(make_epub(chapters=0, spine_extra='<itemref idref="missing"/>'))
    with pytest.raises(ReaderError) as exc:
        await shelve_book(kc, _record(), cache_dir)
    assert "no readable content" in str(exc.value)


async def test_encrypted_epub_rejected(tmp_path):
    cache_dir = str(tmp_path / "cache")
    kc = FakeKC(make_epub(chapters=1, encryption=True))
    with pytest.raises(ReaderError) as exc:
        await shelve_book(kc, _record(), cache_dir)
    assert "DRM" in str(exc.value)


async def test_malformed_zip_rejected(tmp_path):
    cache_dir = str(tmp_path / "cache")
    kc = FakeKC(b"not actually a zip file")
    with pytest.raises(ReaderError):
        await shelve_book(kc, _record(), cache_dir)


async def test_zip_layer_encrypted_epub_rejected_cleanly(tmp_path):
    """A genuinely DRM'd (zip-layer-encrypted) EPUB — no OCF
    encryption.xml at all — must fail with a friendly ReaderError, not a
    bare RuntimeError/500, and must clean up its spool file. Reproduces
    the CRITICAL from the SS-02 adversarial review: zipfile.open() raises
    plain RuntimeError for an encrypted member, which _read_member_capped
    previously did not catch.
    """
    cache_dir = str(tmp_path / "cache")
    body = _patch_encrypted_flag(make_epub(chapters=1), "META-INF/container.xml")
    kc = FakeKC(body)
    with pytest.raises(ReaderError) as exc:
        await shelve_book(kc, _record(), cache_dir)
    assert "DRM" in str(exc.value)

    key = reader._store_book_key(FIXTURE_URL)
    assert not os.path.exists(os.path.join(cache_dir, "reader", f".spool-{os.getpid()}-{key}"))
    assert not os.path.exists(os.path.join(cache_dir, "reader", f"{key}.tmp-{os.getpid()}"))
    assert not os.path.exists(os.path.join(cache_dir, "reader", key))


# -- malformed chapter degrades gracefully -----------------------------------


async def test_malformed_chapter_degrades(tmp_path):
    cache_dir = str(tmp_path / "cache")
    kc = FakeKC(
        make_epub(chapters=3, chapter_bytes={1: b"<p>Unterminated <b>tag & <stray"})
    )
    manifest = await shelve_book(kc, _record(), cache_dir)
    assert len(manifest.chapters) == 3
    blocks = load_chapter(cache_dir, manifest.book_key, 1)
    assert blocks  # still readable
    joined = "".join(blocks)
    # escaped fallback: no unescaped '<' survived from the malformed source
    assert "<stray" not in joined
    assert "&lt;stray" in joined


# -- no upstream URL on disk -------------------------------------------------


async def test_no_upstream_url_on_disk(tmp_path):
    cache_dir = str(tmp_path / "cache")
    kc = FakeKC(make_epub(chapters=2, image=True))
    manifest = await shelve_book(kc, _record(), cache_dir)
    book_dir = os.path.join(cache_dir, "reader", manifest.book_key)
    for text in _walk_text(book_dir):
        assert FIXTURE_URL not in text
        assert "s3cr3t-token" not in text


# -- idempotent re-shelve -----------------------------------------------------


async def test_idempotent_reshelve_no_refetch(tmp_path):
    cache_dir = str(tmp_path / "cache")
    kc = FakeKC(make_epub(chapters=2))
    first = await shelve_book(kc, _record(), cache_dir)
    assert kc.calls == 1
    second = await shelve_book(kc, _record(), cache_dir)
    assert kc.calls == 1  # no refetch
    assert second.book_key == first.book_key
    assert len(second.chapters) == len(first.chapters)


# -- load_manifest / load_chapter / prune_reader_cache ------------------------


def test_load_manifest_missing_returns_none(tmp_path):
    assert load_manifest(str(tmp_path), "nope") is None


def test_load_manifest_corrupt_returns_none(tmp_path):
    cache_dir = tmp_path / "cache"
    book_dir = cache_dir / "reader" / "abc123"
    book_dir.mkdir(parents=True)
    (book_dir / "manifest.json").write_text("not json", encoding="utf-8")
    assert load_manifest(str(cache_dir), "abc123") is None


def test_load_chapter_missing_raises_reader_error(tmp_path):
    with pytest.raises(ReaderError):
        load_chapter(str(tmp_path), "nope", 0)


async def test_prune_reader_cache_deletes_oldest(tmp_path):
    cache_dir = str(tmp_path / "cache")
    kc1 = FakeKC(make_epub(chapters=1))
    m1 = await shelve_book(kc1, _record("https://library.example.test/a.epub"), cache_dir)
    kc2 = FakeKC(make_epub(chapters=1))
    m2 = await shelve_book(kc2, _record("https://library.example.test/b.epub"), cache_dir)

    # Make book 1 look older than book 2.
    dir1 = os.path.join(cache_dir, "reader", m1.book_key)
    dir2 = os.path.join(cache_dir, "reader", m2.book_key)
    os.utime(os.path.join(dir1, "manifest.json"), (1000, 1000))
    os.utime(os.path.join(dir2, "manifest.json"), (2_000_000_000, 2_000_000_000))

    prune_reader_cache(cache_dir, limit=0)
    assert not os.path.exists(dir1) or not os.path.exists(dir2)
    # At least the older one is gone; the cache never fully explodes.
    assert not os.path.exists(dir1)


def test_reader_error_is_retroshelf_error():
    from app.errors import RetroShelfError

    assert issubclass(ReaderError, RetroShelfError)


async def test_shelve_invokes_reader_cache_prune(tmp_path, monkeypatch):
    """``shelve_book`` wires in the reader-cache ceiling (Requirement 8).

    ``prune_reader_cache`` was defined-but-unwired before convergence pass 1,
    so the 1GB cap was never enforced at runtime. A recording stub confirms a
    completed shelve calls it with the cache root and the ceiling constant.
    """
    import app.reader as reader

    seen: list[tuple[str, int]] = []
    monkeypatch.setattr(
        reader, "prune_reader_cache", lambda cd, limit: seen.append((cd, limit))
    )

    cache_dir = str(tmp_path)
    await reader.shelve_book(FakeKC(make_epub(chapters=2)), _record(), cache_dir)

    assert seen == [(cache_dir, reader.MAX_READER_CACHE_BYTES)]


class _StallingKC:
    """A Kavita client whose download stalls forever — models a hung upstream."""

    async def open_stream(self, url, *, range_header=None):
        return _StallingUpstream()


class _StallingUpstream:
    status_code = 200
    headers = httpx.Headers({})

    async def aiter_raw(self):
        await asyncio.sleep(30)  # longer than the (patched-tiny) shelve timeout
        yield b""

    async def aclose(self):
        pass


async def test_stalled_upstream_is_bounded_not_indefinite(tmp_path, monkeypatch):
    """A hung upstream fails with a friendly, bounded error — never a hang.

    Edge case: "Stalled upstream during shelve → bounded 120s timeout". The
    timeout is shrunk here so the test is fast; it asserts ``shelve_book``
    raises a ``RetroShelfError`` (the friendly-page base class) rather than
    blocking forever.
    """
    import app.reader as reader
    from app.errors import RetroShelfError

    monkeypatch.setattr(reader, "SHELVE_TIMEOUT", 0.1)
    with pytest.raises(RetroShelfError):
        await reader.shelve_book(_StallingKC(), _record(), str(tmp_path))


async def test_stale_final_dir_is_replaced_not_a_500(tmp_path):
    """A crashed earlier attempt's leftover dir is recovered, not surfaced raw.

    Edge case: the ``os.rename`` onto a non-empty ``{book_key}`` dir raises
    ``OSError``; with no valid manifest there (a stale/corrupt leftover, not a
    live race winner), ``shelve_book`` clears it and retries rather than
    letting a bare ``OSError`` become a 500.
    """
    import app.reader as reader

    cache_dir = str(tmp_path)
    key = book_key(_record()["u"])
    stale = os.path.join(cache_dir, "reader", key)
    os.makedirs(stale)
    with open(os.path.join(stale, "junk.txt"), "w") as f:
        f.write("leftover from a crashed shelve")  # non-empty, no manifest.json

    manifest = await reader.shelve_book(FakeKC(make_epub(chapters=2)), _record(), cache_dir)

    assert manifest.chapters  # shelved successfully over the stale dir
    assert reader.load_manifest(cache_dir, key) is not None
    assert not os.path.exists(os.path.join(stale, "junk.txt"))  # leftover cleared


# -- hierarchical table of contents (book-fidelity) ---------------------------


def test_nav_toc_captures_nesting_depth():
    nav = (
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops"><body>'
        '<nav epub:type="toc"><ol>'
        '<li><a href="p1.xhtml">Part One</a>'
        '  <ol><li><a href="c1.xhtml">Chapter 1</a></li>'
        '      <li><a href="c2.xhtml">Chapter 2</a></li></ol></li>'
        '<li><a href="p2.xhtml">Part Two</a></li>'
        '</ol></nav></body></html>'
    ).encode("utf-8")
    entries = reader._toc_entries_from_nav(nav, "OEBPS")
    assert entries == [
        (0, "Part One", "OEBPS/p1.xhtml"),
        (1, "Chapter 1", "OEBPS/c1.xhtml"),
        (1, "Chapter 2", "OEBPS/c2.xhtml"),
        (0, "Part Two", "OEBPS/p2.xhtml"),
    ]


def test_ncx_toc_captures_nesting_depth():
    ncx = (
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>'
        '<navPoint><navLabel><text>Part One</text></navLabel>'
        '<content src="p1.xhtml"/>'
        '<navPoint><navLabel><text>Chapter 1</text></navLabel>'
        '<content src="c1.xhtml"/></navPoint></navPoint>'
        '</navMap></ncx>'
    ).encode("utf-8")
    entries = reader._toc_entries_from_ncx(ncx, "OEBPS")
    assert entries == [
        (0, "Part One", "OEBPS/p1.xhtml"),
        (1, "Chapter 1", "OEBPS/c1.xhtml"),
    ]


def test_build_toc_maps_paths_to_chapter_indices_and_normalizes_depth():
    nav_toc = [(1, "Part One", "OEBPS/p1.xhtml"), (2, "Chapter 1", "OEBPS/c1.xhtml")]
    idx = {"OEBPS/p1.xhtml": 0, "OEBPS/c1.xhtml": 1}
    # nav preferred over ncx; shallowest depth normalized to 0; unknown paths dropped.
    built = reader._build_toc(nav_toc, [], idx)
    assert built == [(0, "Part One", 0), (1, "Chapter 1", 1)]
    assert reader._build_toc([(0, "x", "OEBPS/missing.xhtml")], [], idx) == []


def test_v1_manifest_without_toc_loads_with_empty_toc(tmp_path):
    """A legacy v1 manifest (no ``toc`` key) loads fine and degrades to flat."""
    data = {
        "version": 1, "book_key": "abc", "title": "Old", "author": "",
        "chapters": [{"title": "Ch", "blocks": 1, "chars": 5}],
        "images": 0, "total_chars": 5, "created": 1.0,
    }
    m = reader._manifest_from_dict(data)
    assert m.version == 1
    assert m.toc == []


# -- in-book search (findability) ---------------------------------------------


async def test_search_book_finds_matches_with_snippets(tmp_path):
    body = (
        '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body>'
        "<p>Call me Ishmael. Some years ago I sailed the whale roads.</p>"
        "<p>The whale is a mighty beast.</p></body></html>"
    ).encode("utf-8")
    kc = FakeKC(make_epub(chapters=1, image=False, ncx=False,
                          chapter_bytes={0: body}))
    d = str(tmp_path)
    m = await reader.shelve_book(kc, _record(), d)
    hits = reader.search_book(d, m.book_key, len(m.chapters), "whale")
    assert len(hits) == 2  # two paragraphs (blocks) mention "whale"
    assert hits[0].match == "whale"
    assert "roads" in hits[0].after or "roads" in hits[0].before
    assert {h.block for h in hits} == {0, 1}  # located per block for part resolution


def test_search_book_short_query_returns_nothing(tmp_path):
    # No shelved book needed: a sub-minimum query short-circuits before any I/O.
    assert reader.search_book(str(tmp_path), "nokey", 3, "a") == []


async def test_search_snippet_is_plain_text_not_markup(tmp_path):
    # A block whose text contains angle brackets (already escaped in storage)
    # yields plain-text snippet fields — no raw markup for the template to
    # accidentally render unescaped.
    body = (
        '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body>'
        "<p>a needle &lt;b&gt;bold&lt;/b&gt; here</p></body></html>"
    ).encode("utf-8")
    kc = FakeKC(make_epub(chapters=1, image=False, ncx=False,
                          chapter_bytes={0: body}))
    d = str(tmp_path)
    m = await reader.shelve_book(kc, _record(), d)
    hits = reader.search_book(d, m.book_key, len(m.chapters), "needle")
    assert len(hits) == 1
    joined = hits[0].before + hits[0].match + hits[0].after
    assert "<b>" in joined  # decoded to plain text, not an HTML element
    assert "&lt;" not in joined  # entities were decoded for the snippet


# -- hostile ToC metadata: depth bombs, entry floods, giant titles ------------


def test_nav_toc_depth_bomb_does_not_blow_the_stack():
    # 2000-deep <li><ol> nesting: the walk must stop at its depth guard
    # instead of raising RecursionError (an uncaught 500 at shelve time).
    depth = 2000
    nav = (
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops"><body>'
        '<nav epub:type="toc"><ol>'
        + '<li><a href="c.xhtml">T</a><ol>' * depth
        + "</ol></li>" * depth
        + "</ol></nav></body></html>"
    ).encode("utf-8")
    entries = reader._toc_entries_from_nav(nav, "OEBPS")
    # Entries above the walk guard are kept (recorded depth clamps to
    # _MAX_TOC_DEPTH); everything deeper is truncated, never a crash.
    assert 0 < len(entries) <= reader._MAX_TOC_WALK_DEPTH + 1
    assert all(d <= reader._MAX_TOC_DEPTH for d, _t, _p in entries)


def test_ncx_toc_depth_bomb_does_not_blow_the_stack():
    depth = 2000
    ncx = (
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>'
        + ('<navPoint><navLabel><text>T</text></navLabel>'
           '<content src="c.xhtml"/>') * depth
        + "</navPoint>" * depth
        + "</navMap></ncx>"
    ).encode("utf-8")
    entries = reader._toc_entries_from_ncx(ncx, "OEBPS")
    assert 0 < len(entries) <= reader._MAX_TOC_WALK_DEPTH + 1


def test_nav_toc_entry_flood_is_capped():
    count = reader.MAX_TOC_ENTRIES + 500
    items = "".join(
        f'<li><a href="c{i}.xhtml">Chapter {i}</a></li>' for i in range(count)
    )
    nav = (
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops"><body>'
        f'<nav epub:type="toc"><ol>{items}</ol></nav></body></html>'
    ).encode("utf-8")
    entries = reader._toc_entries_from_nav(nav, "OEBPS")
    assert len(entries) == reader.MAX_TOC_ENTRIES


def test_nav_toc_giant_title_is_clipped():
    long_title = "A" * 10_000
    nav = (
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops"><body>'
        f'<nav epub:type="toc"><ol><li><a href="c.xhtml">{long_title}</a></li>'
        "</ol></nav></body></html>"
    ).encode("utf-8")
    entries = reader._toc_entries_from_nav(nav, "OEBPS")
    titles = reader._titles_from_nav(nav, "OEBPS")
    assert len(entries) == 1
    assert len(entries[0][1]) <= reader.MAX_TITLE_CHARS
    assert all(len(t) <= reader.MAX_TITLE_CHARS for t in titles.values())


def test_manifest_toc_depth_is_clamped_on_load(tmp_path):
    # A tampered/corrupt cache row must not surface an out-of-range depth.
    book_dir = tmp_path / "reader" / "clampkey"
    book_dir.mkdir(parents=True)
    manifest = {
        "version": 2, "book_key": "clampkey", "title": "T", "author": "",
        "chapters": [{"title": "c", "blocks": 1, "chars": 5}],
        "images": 0, "total_chars": 5, "created": 0.0,
        "toc": [[99, "deep", 0], [-5, "neg", 0]],
    }
    (book_dir / "manifest.json").write_text(__import__("json").dumps(manifest))
    loaded = load_manifest(str(tmp_path), "clampkey")
    assert loaded is not None
    assert [d for d, _t, _i in loaded.toc] == [reader._MAX_TOC_DEPTH, 0]


async def test_search_query_is_clamped_to_cap(tmp_path):
    run = "q" * (reader.SEARCH_MAX_QUERY_CHARS + 50)
    body = (
        '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body>'
        f"<p>{run}</p></body></html>"
    ).encode("utf-8")
    kc = FakeKC(make_epub(chapters=1, image=False, ncx=False,
                          chapter_bytes={0: body}))
    d = str(tmp_path)
    m = await reader.shelve_book(kc, _record(), d)
    hits = reader.search_book(d, m.book_key, len(m.chapters), run)
    # The over-long query is truncated to the cap, then matched normally.
    assert hits and len(hits[0].match) == reader.SEARCH_MAX_QUERY_CHARS


def test_load_chapter_non_string_blocks_rejected_not_coerced(tmp_path):
    # A tampered/corrupt chapter entry holding non-string blocks must raise,
    # never be str()-coerced — a dict's repr carries literal angle brackets
    # straight into the one | safe render seam. [SS-01]
    book_dir = tmp_path / "reader" / "strictkey" / "chapters"
    book_dir.mkdir(parents=True)
    (book_dir / "0.json").write_text(
        '{"blocks": [{"evil": "<script>alert(1)</script>"}], "anchors": {}}'
    )
    with pytest.raises(ReaderError):
        load_chapter(str(tmp_path), "strictkey", 0)
