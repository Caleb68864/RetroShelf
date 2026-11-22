"""Tests for cover transcoding + disk cache (SS-01).

Covers all acceptance criteria:
- WebP → image/jpeg
- Oversized image → max edge == cover_max_edge
- Small JPEG passthrough (SHA256 matches)
- 2nd request served from cache (transport would fail on 2nd hit)
- No apiKey in cache filename or response
- Pillow importable
"""
import asyncio
import hashlib
import io
import os

import httpx

from PIL import Image


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_jpeg(w: int = 100, h: int = 100) -> bytes:
    img = Image.new("RGB", (w, h), color=(200, 100, 50))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _make_png(w: int = 100, h: int = 100) -> bytes:
    img = Image.new("RGB", (w, h), color=(50, 100, 200))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_webp(w: int = 100, h: int = 100) -> bytes:
    img = Image.new("RGB", (w, h), color=(100, 200, 50))
    buf = io.BytesIO()
    img.save(buf, format="WEBP")
    return buf.getvalue()


def _mock_resp(content: bytes, ct: str = "image/jpeg") -> httpx.Response:
    """httpx.Response with already-buffered content (aread() returns immediately)."""
    return httpx.Response(200, content=content, headers={"Content-Type": ct})


class _MockKC:
    """Minimal KavitaClient stand-in for testing stream_cover."""

    def __init__(self, *responses):
        self._queue = list(responses)
        self._idx = 0

    async def open_stream(self, url: str, *, range_header: str | None = None):
        if self._idx >= len(self._queue):
            raise RuntimeError("MockKC: no more responses")
        item = self._queue[self._idx]
        self._idx += 1
        if isinstance(item, Exception):
            raise item
        return item


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_pillow_importable():
    """Pillow must be importable and have a version string."""
    import PIL
    assert PIL.__version__


def test_webp_transcoded_to_jpeg(tmp_path):
    """WebP cover → Content-Type: image/jpeg."""
    from app.download import stream_cover

    webp_bytes = _make_webp()
    kc = _MockKC(_mock_resp(webp_bytes, "image/webp"))
    resp = _run(stream_cover(kc, "http://kavita/cover.webp",
                             cache_dir=str(tmp_path), cover_max_edge=320, cover_jpeg_quality=80))
    assert resp.media_type == "image/jpeg"


def test_oversized_image_downscaled(tmp_path):
    """1500×1000 image → max(width, height) == cover_max_edge after serving."""
    from app.download import stream_cover

    large_jpeg = _make_jpeg(1500, 1000)
    kc = _MockKC(_mock_resp(large_jpeg, "image/jpeg"))
    resp = _run(stream_cover(kc, "http://kavita/cover_big.jpg",
                             cache_dir=str(tmp_path), cover_max_edge=320, cover_jpeg_quality=80))
    body = resp.body
    img = Image.open(io.BytesIO(body))
    assert max(img.size) == 320


def test_small_jpeg_passthrough_sha256(tmp_path):
    """Small JPEG (fits within cover_max_edge) → served without re-encoding.

    SHA256 of the response body must equal SHA256 of the original bytes.
    """
    from app.download import stream_cover

    small_jpeg = _make_jpeg(100, 100)
    orig_sha = hashlib.sha256(small_jpeg).hexdigest()
    kc = _MockKC(_mock_resp(small_jpeg, "image/jpeg"))
    resp = _run(stream_cover(kc, "http://kavita/cover_small.jpg",
                             cache_dir=str(tmp_path), cover_max_edge=320, cover_jpeg_quality=80))
    resp_sha = hashlib.sha256(resp.body).hexdigest()
    assert resp_sha == orig_sha, "Small JPEG must not be re-encoded"


def test_small_png_passthrough_sha256(tmp_path):
    """Small PNG (fits within cover_max_edge) → served without re-encoding."""
    from app.download import stream_cover

    small_png = _make_png(80, 120)
    orig_sha = hashlib.sha256(small_png).hexdigest()
    kc = _MockKC(_mock_resp(small_png, "image/png"))
    resp = _run(stream_cover(kc, "http://kavita/cover_small.png",
                             cache_dir=str(tmp_path), cover_max_edge=320, cover_jpeg_quality=80))
    resp_sha = hashlib.sha256(resp.body).hexdigest()
    assert resp_sha == orig_sha, "Small PNG must not be re-encoded"


def test_cache_hit_on_second_request(tmp_path):
    """2nd request for the same URL is served from disk cache.

    The mock raises on the 2nd upstream hit to prove the cache was used.
    """
    from app.download import stream_cover

    small_jpeg = _make_jpeg(100, 100)
    url = "http://kavita/cover_cached.jpg"
    # 1st response succeeds; 2nd raises to prove the cache is hit instead.
    kc = _MockKC(
        _mock_resp(small_jpeg, "image/jpeg"),
        RuntimeError("upstream must NOT be contacted on 2nd request"),
    )

    # First request: populate cache
    resp1 = _run(stream_cover(kc, url, cache_dir=str(tmp_path), cover_max_edge=320, cover_jpeg_quality=80))
    assert resp1.status_code == 200

    # Second request: must come from cache, not upstream
    resp2 = _run(stream_cover(kc, url, cache_dir=str(tmp_path), cover_max_edge=320, cover_jpeg_quality=80))
    assert resp2.status_code == 200
    assert resp2.body  # non-empty body from cache


def test_cache_contents_match_served_bytes(tmp_path):
    """Cache file must contain exactly the bytes that were served."""
    from app.download import stream_cover
    from app.download import _cover_cache_key

    small_jpeg = _make_jpeg(50, 50)
    url = "http://kavita/cover_exact.jpg"
    kc = _MockKC(_mock_resp(small_jpeg, "image/jpeg"))

    resp = _run(stream_cover(kc, url, cache_dir=str(tmp_path), cover_max_edge=320, cover_jpeg_quality=80))
    key = _cover_cache_key(url)
    cache_path = os.path.join(str(tmp_path), "covers", key)
    assert os.path.exists(cache_path), "Cache file must be written"
    with open(cache_path, "rb") as f:
        cached = f.read()
    assert cached == resp.body


def test_no_api_key_in_cache_filename(tmp_path):
    """Cache filename must not contain the Kavita apiKey."""
    from app.download import stream_cover

    api_key = "MYSUPERSECRETAPIKEY"
    url = f"http://kavita/api/image/cover?apiKey={api_key}"
    small_jpeg = _make_jpeg(60, 60)
    kc = _MockKC(_mock_resp(small_jpeg, "image/jpeg"))

    _run(stream_cover(kc, url, cache_dir=str(tmp_path), cover_max_edge=320, cover_jpeg_quality=80))

    cache_covers = os.path.join(str(tmp_path), "covers")
    for fn in os.listdir(cache_covers):
        assert api_key not in fn, f"apiKey leaked into cache filename: {fn!r}"


def test_no_api_key_in_response_body(tmp_path):
    """apiKey must not appear in the response body."""
    from app.download import stream_cover

    api_key = "ANOTHERSECRETKEY999"
    url = f"http://kavita/api/image/cover?apiKey={api_key}"
    small_jpeg = _make_jpeg(60, 60)
    kc = _MockKC(_mock_resp(small_jpeg, "image/jpeg"))

    resp = _run(stream_cover(kc, url, cache_dir=str(tmp_path), cover_max_edge=320, cover_jpeg_quality=80))
    assert api_key.encode() not in resp.body


def test_config_cover_fields_defaults():
    """Config defines cache_dir, cover_max_edge, cover_jpeg_quality with correct defaults."""
    from app.config import load_config

    cfg = load_config({"KAVITA_OPDS_URL": "http://kavita:5000/api/opds/SECRETKEY"})
    assert cfg.cache_dir == "/cache"
    assert cfg.cover_max_edge == 320
    assert cfg.cover_jpeg_quality == 80


def test_config_cover_fields_from_env():
    """Config reads CACHE_DIR, COVER_MAX_EDGE, COVER_JPEG_QUALITY from env."""
    from app.config import load_config

    cfg = load_config({
        "KAVITA_OPDS_URL": "http://kavita:5000/api/opds/SECRETKEY",
        "CACHE_DIR": "/tmp/my-cache",
        "COVER_MAX_EDGE": "640",
        "COVER_JPEG_QUALITY": "70",
    })
    assert cfg.cache_dir == "/tmp/my-cache"
    assert cfg.cover_max_edge == 640
    assert cfg.cover_jpeg_quality == 70


# -- LRU eviction and settings-aware keys (decided by agent 2026-11-21) ------
def test_prune_evicts_least_recently_used_not_oldest_written(tmp_path):
    import time
    from app import download
    from app.download import stream_cover

    cache = str(tmp_path)
    urls = [f"http://kavita/lru{i}.jpg" for i in range(3)]
    for i, url in enumerate(urls):
        _run(stream_cover(_MockKC(_mock_resp(_make_jpeg(40 + i, 40))), url, cache_dir=cache,
                          cover_max_edge=320, cover_jpeg_quality=80))
    covers = os.path.join(cache, "covers")
    old = time.time() - 1000
    for i, url in enumerate(urls):  # written in order 0, 1, 2
        p = os.path.join(covers, download._cover_cache_key(url))
        for f in (p, p + ".ct"):
            if os.path.exists(f):
                os.utime(f, (old + i, old + i))
    # Re-read the oldest-written one: a cache hit (no upstream left in the queue).
    _run(stream_cover(_MockKC(), urls[0], cache_dir=cache, cover_max_edge=320,
                      cover_jpeg_quality=80))
    one = os.path.getsize(os.path.join(covers, download._cover_cache_key(urls[1])))
    download._prune_cover_cache(covers, limit=sum(
        os.path.getsize(os.path.join(covers, n)) for n in os.listdir(covers)) - one)
    left = set(os.listdir(covers))
    assert download._cover_cache_key(urls[0]) in left       # recently used: kept
    assert download._cover_cache_key(urls[1]) not in left   # least recently used: gone
    assert download._cover_cache_key(urls[1]) + ".ct" not in left


def test_changing_cover_max_edge_does_not_serve_the_old_size(tmp_path):
    from app.download import stream_cover
    url = "http://kavita/edge.jpg"
    big = _make_jpeg(800, 800)
    r1 = _run(stream_cover(_MockKC(_mock_resp(big)), url, cache_dir=str(tmp_path),
                           cover_max_edge=320, cover_jpeg_quality=80))
    r2 = _run(stream_cover(_MockKC(_mock_resp(big)), url, cache_dir=str(tmp_path),
                           cover_max_edge=100, cover_jpeg_quality=80))
    assert max(Image.open(io.BytesIO(r1.body)).size) == 320
    assert max(Image.open(io.BytesIO(r2.body)).size) == 100


def test_default_settings_keep_the_original_cache_key():
    import hashlib
    from app.download import _cover_cache_key
    url = "http://kavita/x.jpg"
    assert _cover_cache_key(url) == hashlib.sha256(url.encode()).hexdigest()
    assert _cover_cache_key(url, 320, 80) == _cover_cache_key(url)
    assert _cover_cache_key(url, 100, 80) != _cover_cache_key(url)
