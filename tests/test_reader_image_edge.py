"""READER_IMAGE_MAX_EDGE is a setting (deferred item, built 2026-11-21)."""
import io

import pytest
from PIL import Image

from app import reader
from app.config import ConfigError, load_config
from tests.test_reader_routes import ENV, _bid, make_client, make_handler
from tests.test_reader_shelve import make_epub


def test_setting_parses_with_default_and_bounds():
    assert load_config(ENV).reader_image_max_edge == 1024
    assert load_config({**ENV, "READER_IMAGE_MAX_EDGE": "2048"}).reader_image_max_edge == 2048
    for bad in ("100", "9000", "big"):
        with pytest.raises(ConfigError):
            load_config({**ENV, "READER_IMAGE_MAX_EDGE": bad})


def test_transcode_uses_the_configured_edge(monkeypatch):
    buf = io.BytesIO()
    Image.new("RGB", (3000, 100), (1, 2, 3)).save(buf, format="JPEG")
    monkeypatch.setattr(reader, "READER_IMAGE_MAX_EDGE", 2000)
    out, _ct = reader._transcode_reader_image(buf.getvalue())
    assert max(Image.open(io.BytesIO(out)).size) == 2000


def _big_image_epub() -> bytes:
    import zipfile
    src = zipfile.ZipFile(io.BytesIO(make_epub(chapters=1)))
    big = io.BytesIO()
    Image.new("RGB", (3000, 100), (9, 9, 9)).save(big, format="JPEG")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for info in src.infolist():
            data = big.getvalue() if info.filename.endswith("cover.jpg") else src.read(info)
            z.writestr(info, data)
    return out.getvalue()


def test_app_applies_the_setting_and_reshelves_on_change(tmp_path):
    handler, calls = make_handler(_big_image_epub())
    cache = str(tmp_path)
    with make_client(handler, cache, {"READER_IMAGE_MAX_EDGE": "512"}) as client:
        bid = _bid(client)
        client.get(f"/read/{bid}", follow_redirects=False)
        img = client.get(f"/read/{bid}/img/0")
        assert max(Image.open(io.BytesIO(img.content)).size) == 512
    fetched = len(calls)
    with make_client(handler, cache, {"READER_IMAGE_MAX_EDGE": "2048"}) as client:
        bid = _bid(client)
        client.get(f"/read/{bid}", follow_redirects=False)
        assert len(calls) > fetched  # re-shelved for the new size
        img = client.get(f"/read/{bid}/img/0")
        assert max(Image.open(io.BytesIO(img.content)).size) == 2048
    # Back to the default for the rest of the suite.
    reader.READER_IMAGE_MAX_EDGE = 1024
