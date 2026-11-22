"""Reader follow-ups (2026-11-21): bounded HTML image fetches and placeholder
neutralization across unwrapped elements."""
import asyncio
import time

import httpx

from app import reader


class _StallingKC:
    async def open_stream(self, url, *, range_header=None):
        async def body():
            yield b"\x89PNG"
            await asyncio.sleep(8)
            yield b"never"
        return httpx.Response(200, content=body())


def test_stalled_html_image_fetch_times_out(monkeypatch):
    monkeypatch.setattr(reader, "IMAGE_FETCH_TIMEOUT", 0.2, raising=False)
    start = time.monotonic()
    got = asyncio.run(reader._fetch_capped_image(_StallingKC(), "http://kavita:5000/i.png"))
    assert got is None
    assert time.monotonic() - start < 5


def test_image_fetch_timeout_reuses_the_feed_read_timeout():
    from app.kavita import FEED_READ_TIMEOUT
    assert reader.IMAGE_FETCH_TIMEOUT == FEED_READ_TIMEOUT


def _sanitize(src, resolve_image=lambda h: None):
    return reader.sanitize_chapter(src, resolve_image=resolve_image,
                                   resolve_link=lambda h: None)


def test_placeholder_cannot_be_forged_across_an_unwrapped_element():
    for forged, token in (("{<foo/>IMG:0}", "{IMG:0}"), ("{<x/>FRAG:abc}", "{FRAG:abc}"),
                          ("{<span>IMG:0}</span>", "{IMG:0}"), ("{<x/>CH:1}", "{CH:1}")):
        joined = "".join(_sanitize(f"<html><body><p>{forged}</p></body></html>"))
        assert token not in joined, forged


def test_real_image_placeholder_survives():
    joined = "".join(_sanitize('<html><body><p>See <img src="a.png" alt="A"/> {</p></body></html>',
                               resolve_image=lambda h: 0))
    assert 'src="{IMG:0}"' in joined


def test_prune_spares_an_in_progress_build_but_not_a_stale_one(tmp_path):
    import os
    reader_dir = tmp_path / "reader"
    live = reader_dir / "bookA.tmp-123"
    stale = reader_dir / "bookB.tmp-456"
    done = reader_dir / "bookC"
    for d in (live, stale, done):
        (d / "chapters").mkdir(parents=True)
        (d / "chapters" / "0.json").write_bytes(b"x" * 1000)
    (done / "manifest.json").write_text("{}")
    old = time.time() - 2 * 3600
    os.utime(stale, (old, old))
    os.utime(done / "manifest.json", (old - 10, old - 10))
    reader.prune_reader_cache(str(tmp_path), 0)
    assert live.exists()
    assert not stale.exists() and not done.exists()


# -- obfuscated fonts are not DRM --------------------------------------------
def _epub_with_encryption(enc_xml: bytes) -> bytes:
    import io
    import zipfile
    from tests.test_reader_shelve import make_epub
    src = zipfile.ZipFile(io.BytesIO(make_epub(chapters=1, image=False)))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for info in src.infolist():
            z.writestr(info, src.read(info))
        z.writestr("META-INF/encryption.xml", enc_xml)
    return out.getvalue()


def _enc(alg: str, uri: str) -> bytes:
    return f"""<?xml version="1.0"?>
<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
  xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
 <enc:EncryptedData><enc:EncryptionMethod Algorithm="{alg}"/>
  <enc:CipherData><enc:CipherReference URI="{uri}"/></enc:CipherData>
 </enc:EncryptedData></encryption>""".encode()


def _extract(data: bytes):
    import os
    import tempfile
    d = tempfile.mkdtemp()
    tmp = os.path.join(d, "t")
    os.makedirs(os.path.join(tmp, "chapters"))
    os.makedirs(os.path.join(tmp, "images"))
    path = os.path.join(d, "b.epub")
    with open(path, "wb") as f:
        f.write(data)
    return reader._extract_epub(path, "k", {"t": "x"}, tmp)


def test_font_obfuscation_only_is_readable():
    for alg in ("http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC"):
        manifest = _extract(_epub_with_encryption(_enc(alg, "OEBPS/fonts/Body%20Text.otf")))
        assert manifest.chapters


def test_real_drm_is_still_refused():
    import pytest
    from app.errors import ReaderError
    cases = [
        _enc("http://www.w3.org/2001/04/xmlenc#aes128-cbc", "OEBPS/ch0.xhtml"),
        _enc("http://www.idpf.org/2008/embedding", "OEBPS/ch0.xhtml"),  # not a font
        b"<encryption/>",
        b"not xml",
    ]
    for enc in cases:
        with pytest.raises(ReaderError, match="DRM"):
            _extract(_epub_with_encryption(enc))
