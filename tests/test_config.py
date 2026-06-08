"""Tests for app.config — env parsing, validation, origin derivation, masking."""
import pytest

from app.config import ConfigError, load_config, origin_tuple, _normalize_origin

BASE_ENV = {
    "KAVITA_BASE_URL": "http://kavita:5000",
    "KAVITA_OPDS_URL": "http://kavita:5000/api/opds/SECRETKEY123",
}


def test_loads_minimal_required():
    cfg = load_config(BASE_ENV)
    assert cfg.kavita_origin == "http://kavita:5000"
    assert cfg.api_key == "SECRETKEY123"
    assert cfg.app_port == 8099
    assert cfg.pdf_disposition == "inline"
    assert cfg.epub_disposition == "attachment"
    assert cfg.cache_feeds_seconds == 300
    assert cfg.cache_books is False
    assert cfg.show_covers is True


def test_missing_opds_url_raises_clear_configerror():
    with pytest.raises(ConfigError) as exc:
        load_config({"KAVITA_BASE_URL": "http://kavita:5000"})
    assert "KAVITA_OPDS_URL" in str(exc.value)


def test_base_url_derived_from_opds_when_omitted():
    cfg = load_config({"KAVITA_OPDS_URL": "http://kavita:5000/api/opds/K"})
    assert cfg.kavita_base_url == "http://kavita:5000"
    assert cfg.kavita_origin == "http://kavita:5000"


def test_mismatched_origins_raise():
    with pytest.raises(ConfigError):
        load_config({
            "KAVITA_BASE_URL": "http://other:5000",
            "KAVITA_OPDS_URL": "http://kavita:5000/api/opds/K",
        })


def test_origin_normalizes_default_ports():
    assert _normalize_origin("http://kavita") == "http://kavita"
    assert _normalize_origin("http://kavita:80") == "http://kavita"
    assert _normalize_origin("https://k:443/x") == "https://k"
    assert _normalize_origin("http://k:5000") == "http://k:5000"


def test_origin_tuple_default_port():
    assert origin_tuple("http://kavita") == ("http", "kavita", 80)
    assert origin_tuple("https://kavita") == ("https", "kavita", 443)
    assert origin_tuple("http://kavita:5000/x") == ("http", "kavita", 5000)


def test_mask_redacts_api_key_and_access_key():
    cfg = load_config({**BASE_ENV, "BRIDGE_ACCESS_KEY": "topsecret"})
    masked = cfg.mask("url=http://kavita:5000/api/opds/SECRETKEY123/libraries key=topsecret")
    assert "SECRETKEY123" not in masked
    assert "topsecret" not in masked
    assert "***" in masked


def test_invalid_pdf_disposition_raises():
    with pytest.raises(ConfigError):
        load_config({**BASE_ENV, "PDF_DISPOSITION": "bogus"})


def test_invalid_int_raises():
    with pytest.raises(ConfigError):
        load_config({**BASE_ENV, "CACHE_FEEDS_SECONDS": "notanint"})


def test_allowed_ips_parsed():
    cfg = load_config({**BASE_ENV, "ALLOWED_IPS": "192.168.1.0/24, 10.0.0.5"})
    assert cfg.allowed_ips == ("192.168.1.0/24", "10.0.0.5")


def test_debug_flag():
    cfg = load_config({**BASE_ENV, "LOG_LEVEL": "debug"})
    assert cfg.debug is True
    assert load_config(BASE_ENV).debug is False
