"""Tests for app.security — filename sanitization, access key, IP allowlist."""
from app.security import sanitize_filename, access_key_ok, ip_allowed


def test_sanitize_basic():
    assert sanitize_filename("The Time Machine", "epub") == "The_Time_Machine.epub"


def test_sanitize_strips_path_and_traversal():
    out = sanitize_filename('../../etc/passwd', "pdf")
    assert "/" not in out and ".." not in out
    assert out.endswith(".pdf")


def test_sanitize_strips_quotes_control_backslash():
    out = sanitize_filename('a"b\\c\x00\n.epub', "epub")
    assert '"' not in out and "\\" not in out and "\x00" not in out
    assert "/" not in out
    assert out.endswith(".epub")
    assert not out.endswith(".epub.epub")


def test_sanitize_unicode_to_ascii():
    out = sanitize_filename("Naïve Café résumé", "pdf")
    assert all(ord(c) < 128 for c in out)
    assert out.endswith(".pdf")


def test_sanitize_empty_fallback():
    assert sanitize_filename("", "epub") == "download.epub"
    assert sanitize_filename(None, "pdf") == "download.pdf"


def test_sanitize_no_double_extension():
    assert sanitize_filename("book.epub", "epub") == "book.epub"


def test_access_key_open_when_unconfigured():
    assert access_key_ok(None, None) is True
    assert access_key_ok("anything", None) is True


def test_access_key_enforced():
    assert access_key_ok("abc", "abc") is True
    assert access_key_ok("wrong", "abc") is False
    assert access_key_ok(None, "abc") is False


def test_ip_allowlist_open_when_unconfigured():
    assert ip_allowed("1.2.3.4", ()) is True


def test_ip_allowlist_cidr_and_exact():
    allowed = ("192.168.1.0/24", "10.0.0.5")
    assert ip_allowed("192.168.1.50", allowed) is True
    assert ip_allowed("10.0.0.5", allowed) is True
    assert ip_allowed("10.0.0.6", allowed) is False
    assert ip_allowed("8.8.8.8", allowed) is False
    assert ip_allowed("not-an-ip", allowed) is False
    assert ip_allowed(None, allowed) is False
