"""Hardening tests: failure modes anticipated for real-world / generic OPDS use."""
import httpx
import pytest

from app.config import load_config
from app.errors import BadIdError, SsrfError
from app.ids import IdCodec
from app.kavita import KavitaClient

ENV = {"KAVITA_OPDS_URL": "http://kavita:5000/api/opds/SECRETKEYLONGENOUGH"}


def _kc(env=ENV, handler=None):
    cfg = load_config(env)
    handler = handler or (lambda r: httpx.Response(200))
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                             timeout=httpx.Timeout(connect=2, read=None, write=None, pool=2))
    return cfg, KavitaClient(cfg, http)


# -- H1: multi-origin allowlist for generic OPDS ------------------------------

def test_extra_origin_allowed():
    env = {**ENV, "EXTRA_UPSTREAM_ORIGINS": "https://cdn.example.com, https://files.example.com:8443"}
    cfg, kc = _kc(env)
    assert kc.resolve_url("https://cdn.example.com/cover/1.jpg") == "https://cdn.example.com/cover/1.jpg"
    assert kc.resolve_url("https://files.example.com:8443/x.epub").startswith("https://files.example.com:8443")
    # A still-foreign origin remains refused.
    with pytest.raises(SsrfError):
        kc.resolve_url("https://evil.example.org/x")


def test_extra_origins_empty_by_default():
    cfg, kc = _kc()
    with pytest.raises(SsrfError):
        kc.resolve_url("https://cdn.example.com/x")


# -- H2: masking does not mangle short generic segments -----------------------

def test_short_key_not_masked():
    cfg = load_config({"KAVITA_OPDS_URL": "https://manybooks.net/opds"})
    # api_key would be "opds" (4 chars) — must NOT be redacted.
    assert cfg.mask("visit https://manybooks.net/opds/feed") == "visit https://manybooks.net/opds/feed"


def test_long_key_is_masked():
    cfg = load_config(ENV)
    assert "SECRETKEYLONGENOUGH" not in cfg.mask("k=SECRETKEYLONGENOUGH")


# -- H4: id length cap (DoS guard) --------------------------------------------

def test_huge_id_rejected_fast():
    c = IdCodec("s")
    with pytest.raises(BadIdError):
        c.decode("A" * 100000 + "." + "B" * 100000)


# -- H8: generic (non-Kavita) OPDS parsing ------------------------------------

def test_parse_generic_opds_with_absolute_cross_host_urls():
    import pathlib
    from app.opds import parse
    xml = (pathlib.Path(__file__).parent / "fixtures" / "opds_generic.xml").read_text()
    feed = parse(xml)
    assert feed.title == "Generic OPDS Catalog"
    assert feed.search_url and feed.search_url.startswith("https://")
    nav, book = feed.entries
    assert nav.is_navigation and nav.nav_href == "https://books.example.com/opds/genres"
    assert not book.is_navigation
    assert len(book.acquisitions) == 2
    assert book.primary_acquisition.is_epub
    assert book.cover_url == "https://cdn.example.com/covers/9.jpg"


def test_generic_opds_ssrf_needs_extra_origins():
    # Cross-host download URL is refused unless its origin is allowlisted.
    cfg, kc = _kc()
    with pytest.raises(SsrfError):
        kc.resolve_url("https://files.example.com/dl/9.epub")
    env = {**ENV, "EXTRA_UPSTREAM_ORIGINS": "https://files.example.com,https://cdn.example.com"}
    _, kc2 = _kc(env)
    assert kc2.resolve_url("https://files.example.com/dl/9.epub") == "https://files.example.com/dl/9.epub"
    assert kc2.resolve_url("https://cdn.example.com/covers/9.jpg") == "https://cdn.example.com/covers/9.jpg"
