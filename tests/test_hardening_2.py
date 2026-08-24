"""Regression tests for the second hardening sweep (SS-09 … SS-16).

One section per hardening pass. Every test here encodes a behaviour that was
either missing or wrong before the sweep, so a future refactor that quietly
undoes one of them fails loudly.

A recurring assertion across the file: nothing added here may cost an iOS
5.1.1–12 iPad anything. The app stays no-JavaScript, its links stay plain
``<a href>``, and every new protection is either a response header old Safari
ignores or a query parameter it passes through untouched.
"""
import base64
import json
import logging
import os
import tempfile

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import load_config
from app.download import build_headers, content_disposition
from app.errors import KavitaError, SsrfError
from app.ids import IdCodec
from app.kavita import KavitaClient, MAX_FEED_BYTES
from app.main import _safe_path, create_app
from app.opds import MAX_ENTRIES, OpdsParseError, parse
from app.store import Store, sanitize_record

from tests.test_app import ENV, _first_book_id, make_client, make_handler


def _kc(env=None, handler=None):
    cfg = load_config(env or {"KAVITA_OPDS_URL": "https://manybooks.net/opds"})
    handler = handler or (lambda r: httpx.Response(200))
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                             timeout=httpx.Timeout(connect=2, read=None, write=None, pool=2))
    return cfg, KavitaClient(cfg, http)


# -- Pass 1: open redirect ----------------------------------------------------

@pytest.mark.parametrize("target", [
    "//evil.example/",          # protocol-relative — a real open redirect
    "/\\evil.example/",         # backslash variant browsers normalise to //
    "https://evil.example/",    # absolute
    "javascript:alert(1)",      # scheme
    "/ok\r\nX-Injected: 1",     # header smuggling through the Location
    "",
    None,
])
def test_unsafe_redirect_targets_fall_back(target):
    assert _safe_path(target, "/") == "/"


@pytest.mark.parametrize("target", ["/", "/list", "/feed/abc?sort=title", "/search?q=x"])
def test_same_origin_paths_are_preserved(target):
    assert _safe_path(target, "/") == target


def test_prefs_does_not_redirect_off_site():
    with make_client(make_handler()) as client:
        token = client.app.state.ids.site_token
        resp = client.get(f"/prefs?color=green&next=//evil.example&t={token}",
                          follow_redirects=False)
        assert resp.headers["location"] == "/"


# -- Pass 2: secret leakage in logs -------------------------------------------

def test_access_key_in_query_is_masked_even_when_short():
    # Below the 8-char value-masking floor, so only the structural rule can
    # catch it — which is exactly the uvicorn access-log case. [H-7]
    cfg = load_config({**ENV, "BRIDGE_ACCESS_KEY": "abc"})
    assert "abc" not in cfg.mask('GET /feed/x?key=abc HTTP/1.1')


@pytest.mark.parametrize("param", ["key", "apiKey", "api_key", "token", "access_token", "password"])
def test_sensitive_query_params_masked_by_name(param):
    cfg = load_config(ENV)
    assert "hunter2" not in cfg.mask(f"https://x/y?{param}=hunter2&page=3")


def test_foreign_opds_path_key_is_masked():
    cfg = load_config(ENV)
    masked = cfg.mask("GET http://other-host:5000/api/opds/SOMEONE_ELSES_KEY/libraries")
    assert "SOMEONE_ELSES_KEY" not in masked


def test_non_secret_text_is_left_alone():
    cfg = load_config(ENV)
    for benign in ("visit https://manybooks.net/opds/feed", "monkey=banana", "turkey=roast"):
        assert cfg.mask(benign) == benign


def test_mask_filter_reaches_uvicorn_access_logger():
    from app.main import _install_mask_filter
    cfg = load_config({**ENV, "BRIDGE_ACCESS_KEY": "topsecretkey"})
    access = logging.getLogger("uvicorn.access")
    before = len(access.filters)
    _install_mask_filter(cfg)
    try:
        assert len(access.filters) > before
        record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                                   'GET /?key=topsecretkey HTTP/1.1', (), None)
        for flt in access.filters:
            flt.filter(record)
        assert "topsecretkey" not in record.getMessage()
    finally:
        access.filters = access.filters[:before]


# -- Pass 3: resource-exhaustion caps -----------------------------------------

@pytest.mark.asyncio
async def test_feed_larger_than_cap_is_refused():
    body = b"<feed>" + b"x" * (MAX_FEED_BYTES + 1)
    _cfg, kc = _kc(handler=lambda r: httpx.Response(200, content=body))
    with pytest.raises(KavitaError, match="exceeded|too large"):
        await kc.fetch_feed("https://manybooks.net/opds")


@pytest.mark.asyncio
async def test_lying_content_length_is_refused_before_transfer():
    def handler(request):
        return httpx.Response(200, content=b"<feed/>",
                              headers={"Content-Length": str(MAX_FEED_BYTES * 4)})
    _cfg, kc = _kc(handler=handler)
    with pytest.raises(KavitaError, match="too large"):
        await kc.fetch_feed("https://manybooks.net/opds")


@pytest.mark.asyncio
async def test_feed_under_the_cap_still_works():
    _cfg, kc = _kc(handler=lambda r: httpx.Response(200, text="<feed/>"))
    assert await kc.fetch_feed("https://manybooks.net/opds") == "<feed/>"


@pytest.mark.asyncio
async def test_oversized_cover_is_refused():
    from app.download import MAX_COVER_BYTES, stream_cover
    from tests.test_download_headers import FakeKC, FakeUpstream

    huge = FakeUpstream(200, {"Content-Type": "image/jpeg"},
                        chunks=[b"\x00" * 65536] * ((MAX_COVER_BYTES // 65536) + 2))
    with tempfile.TemporaryDirectory() as cache:
        with pytest.raises(KavitaError):
            await stream_cover(FakeKC(huge), "http://k/cover", cache_dir=cache)
    assert huge.closed


def test_cover_cache_is_pruned_to_its_ceiling():
    from app.download import _prune_cover_cache

    with tempfile.TemporaryDirectory() as cache:
        for i in range(10):
            with open(os.path.join(cache, f"cover{i}"), "wb") as f:
                f.write(b"x" * 1000)
            os.utime(os.path.join(cache, f"cover{i}"), (i, i))  # oldest first
        _prune_cover_cache(cache, 3000)
        remaining = sorted(os.listdir(cache))
        assert sum(os.path.getsize(os.path.join(cache, n)) for n in remaining) <= 3000
        # The survivors are the newest ones.
        assert "cover9" in remaining and "cover0" not in remaining


def test_non_image_upstream_type_is_not_relayed_as_a_document():
    from app.download import _safe_image_type
    assert _safe_image_type("image/png") == "image/png"
    assert _safe_image_type("image/jpeg; charset=binary") == "image/jpeg"
    assert _safe_image_type("text/html") == "application/octet-stream"
    assert _safe_image_type(None) == "application/octet-stream"


# -- Pass 4: security response headers ----------------------------------------

def test_html_pages_carry_security_headers():
    with make_client(make_handler()) as client:
        resp = client.get("/help")
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["Referrer-Policy"] == "same-origin"
        csp = resp.headers["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in csp
        # Never upgrade to https: RetroShelf is a plain-HTTP LAN service and an
        # upgraded link is a dead link on the iPad.
        assert "upgrade-insecure-requests" not in csp


def test_csp_matches_what_the_pages_actually_use():
    # The policy allows no script at all. If a template ever grows one, this
    # test is the thing that says so — and a script would break iOS 5 anyway.
    with make_client(make_handler()) as client:
        for path in ("/", "/help", "/list"):
            body = client.get(path).text
            assert "<script" not in body.lower()
            assert "javascript:" not in body.lower()
            assert " style=" not in body  # style-src has no 'unsafe-inline'


def test_book_download_keeps_its_own_headers_untouched():
    # The security headers must never reach a book stream: iOS decides what to
    # do with the file from exactly these headers. [SS-10]
    with make_client(make_handler()) as client:
        detail = client.get(f"/book/{_first_book_id(client)}").text
        resp = client.get(_first_download_path(detail))
        assert resp.headers["content-type"].startswith("application/epub+zip")
        assert 'filename="' in resp.headers["content-disposition"]
        assert "Content-Security-Policy" not in resp.headers


def _first_download_path(html: str) -> str:
    return "/download/" + html.split('href="/download/', 1)[1].split('"', 1)[0]


# -- Pass 5: state store robustness -------------------------------------------

def test_corrupt_state_file_is_quarantined_not_lost():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "state.json")
        with open(path, "w") as f:
            f.write("{not json at all")
        store = Store(path)
        assert store.favorites() == []
        assert os.path.exists(path + ".corrupt")


@pytest.mark.parametrize("payload", [
    {"favorites": ["not", "a", "mapping"], "history": "not a list"},
    {"favorites": {"k": "not a record"}, "history": [1, 2, 3]},
    ["not", "an", "object"],
])
def test_wrong_shaped_state_file_does_not_crash(payload):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "state.json")
        with open(path, "w") as f:
            json.dump(payload, f)
        store = Store(path)
        assert store.favorites() == []
        assert store.recent_downloads() == []
        assert store.downloaded_keys() == set()


def test_record_fields_are_bounded_before_they_reach_disk():
    record = sanitize_record({"u": "http://x/a.epub", "t": "T" * 5000, "s": "S" * 100000,
                              "evil": "dropped", "fmts": [{"u": "http://x/a.epub", "f": "epub",
                                                           "len": "not an int"}] * 50})
    assert len(record["t"]) <= 512
    assert len(record["s"]) <= 2000
    assert "evil" not in record
    assert len(record["fmts"]) <= 8
    assert record["fmts"][0]["len"] is None


def test_timestamps_survive_a_reload_so_ordering_holds():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "state.json")
        store = Store(path)
        store.add_favorite({"u": "http://x/first.epub", "t": "First"})
        store.add_favorite({"u": "http://x/second.epub", "t": "Second"})
        assert [f["t"] for f in Store(path).favorites()] == ["Second", "First"]


# -- Pass 6: bridge-id crypto -------------------------------------------------

def test_new_tokens_are_v2():
    codec = IdCodec("stable-secret")
    token = codec.encode("http://kavita:5000/api/opds/KEY/book")
    assert token.startswith("2.")
    assert codec.decode(token) == "http://kavita:5000/api/opds/KEY/book"


def test_v1_tokens_still_decode_so_home_screen_links_survive():
    # An iPad with a bookmarked /download link must keep working after upgrade.
    import hashlib
    import hmac
    secret = "stable-secret"
    key = hashlib.sha256(secret.encode()).digest()
    plaintext = b"http://kavita:5000/legacy.epub"
    nonce = b"abcdef"

    def keystream(length):
        out, counter = b"", 0
        while len(out) < length:
            out += hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
            counter += 1
        return out[:length]

    body = nonce + bytes(a ^ b for a, b in zip(plaintext, keystream(len(plaintext))))
    mac = hmac.new(key, body, hashlib.sha256).digest()[:12]
    b64 = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b"=").decode()  # noqa: E731
    assert IdCodec(secret).decode(f"{b64(body)}.{b64(mac)}") == plaintext.decode()


def test_v2_token_cannot_be_downgraded_to_v1():
    codec = IdCodec("stable-secret")
    from app.errors import BadIdError
    _version, body, mac = codec.encode("http://kavita:5000/x").split(".")
    with pytest.raises(BadIdError):
        codec.decode(f"{body}.{mac}")   # version stripped → MAC no longer matches


def test_v2_uses_a_wider_nonce_and_mac():
    from app.ids import _V2_MAC_LEN, _V2_NONCE_LEN
    assert _V2_NONCE_LEN >= 12 and _V2_MAC_LEN >= 16


def test_encryption_and_mac_keys_are_distinct():
    codec = IdCodec("stable-secret")
    assert codec._enc_key != codec._mac_key != codec._key


# -- Pass 7: response-header injection ----------------------------------------

def test_content_disposition_cannot_be_broken_out_of():
    value = content_disposition("attachment", 'x".epub\r\nX-Injected: yes')
    assert "\r" not in value and "\n" not in value
    assert value.count('"') == 2


def test_content_disposition_keeps_the_plain_filename_old_safari_needs():
    # No RFC 5987 filename* — iOS 5/6 Safari does not understand it and would
    # save the book under a wrong name (or as .zip). [C-1]
    value = content_disposition("attachment", "My_Book.epub")
    assert value == 'attachment; filename="My_Book.epub"'
    assert "filename*" not in value


@pytest.mark.parametrize("bad", ["12; drop", "abc", "-1", "9" * 40, ""])
def test_malformed_upstream_content_length_is_not_relayed(bad):
    upstream = httpx.Response(200, headers={"Content-Length": bad})
    headers = build_headers(filename="b.epub", disposition="attachment", upstream=upstream)
    assert headers.get("Content-Length") != bad


def test_wellformed_range_headers_are_still_relayed():
    upstream = httpx.Response(206, headers={
        "Content-Length": "50", "Content-Range": "bytes 0-49/100", "Accept-Ranges": "bytes"})
    headers = build_headers(filename="b.epub", disposition="attachment", upstream=upstream)
    assert headers["Content-Length"] == "50"
    assert headers["Content-Range"] == "bytes 0-49/100"


def test_malformed_content_range_is_dropped():
    upstream = httpx.Response(206, headers={"Content-Range": "pages 1-2/3"})
    headers = build_headers(filename="b.epub", disposition="attachment", upstream=upstream)
    assert "Content-Range" not in headers


# -- Pass 8: SSRF guard -------------------------------------------------------

@pytest.mark.parametrize("href,reason", [
    ("file:///etc/passwd", "non-HTTP scheme"),
    ("gopher://manybooks.net/x", "non-HTTP scheme"),
    ("https://user:pw@manybooks.net/x", "embedded credentials"),
    ("https://manybooks.net/a\r\nX-Smuggled: 1", "control characters"),
    ("https://manybooks.net/a\tb", "control characters"),
])
def test_ssrf_guard_refuses_hostile_href_shapes(href, reason):
    _cfg, kc = _kc()
    with pytest.raises(SsrfError, match=reason.split()[0]):
        kc.resolve_url(href)


def test_oversized_href_is_refused():
    _cfg, kc = _kc()
    with pytest.raises(SsrfError, match="oversized"):
        kc.resolve_url("https://manybooks.net/" + "a" * 5000)


@pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.5", "192.168.1.7",
                                  "169.254.169.254", "[::1]", "0.0.0.0"])
def test_sibling_rule_never_widens_to_internal_addresses(host):
    # The same-site rule must not become a path to the host's own network.
    _cfg, kc = _kc()
    with pytest.raises(SsrfError):
        kc.resolve_url(f"https://{host}/x.epub")


def test_same_site_sibling_still_resolves():
    # The convenience the sibling rule exists for must survive the tightening.
    _cfg, kc = _kc()
    assert kc.resolve_url("https://library.manybooks.net/x.epub").startswith("https://library.")


def test_scheme_downgrade_is_refused():
    _cfg, kc = _kc()
    with pytest.raises(SsrfError):
        kc.resolve_url("http://manybooks.net/x.epub")


# -- Pass 9: OPDS parser limits -----------------------------------------------

def test_entry_count_is_capped():
    xml = ('<feed xmlns="http://www.w3.org/2005/Atom">'
           + "<entry><title>t</title></entry>" * (MAX_ENTRIES + 200)
           + "</feed>")
    assert len(parse(xml).entries) == MAX_ENTRIES


def test_absurd_text_fields_are_truncated():
    xml = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
           f'<title>{"T" * 50000}</title><summary>{"S" * 200000}</summary>'
           "</entry></feed>")
    entry = parse(xml).entries[0]
    assert len(entry.title) <= 1000
    assert len(entry.summary) <= 8000


def test_oversized_document_is_rejected():
    with pytest.raises(OpdsParseError, match="too large"):
        parse(b'<feed xmlns="http://www.w3.org/2005/Atom"/>' + b" " * (9 * 1024 * 1024))


def test_stale_encoding_declaration_does_not_corrupt_text():
    # Text arrives already decoded, so the declaration inside it is stale.
    # Before the fix this silently mangled every non-ASCII title on any feed
    # served as anything but UTF-8.
    xml = ('<?xml version="1.0" encoding="iso-8859-1"?>'
           '<feed xmlns="http://www.w3.org/2005/Atom"><title>Café Society</title></feed>')
    assert parse(xml).title == "Café Society"


# -- Pass 10: state-changing request safety + access key ----------------------

def test_star_without_the_site_token_is_refused():
    with make_client(make_handler()) as client:
        bid = _first_book_id(client)
        # This is the cross-site shape: a bare GET, no token.
        assert client.get(f"/star/{bid}", follow_redirects=False).status_code == 403
        assert client.get("/list").text.count("/book/") == 0


def test_star_with_the_site_token_works():
    with make_client(make_handler()) as client:
        bid = _first_book_id(client)
        token = client.app.state.ids.site_token
        client.get(f"/star/{bid}?t={token}")
        assert "/book/" in client.get("/list").text


def test_rendered_pages_carry_the_token_on_their_own_links():
    # The protection must be invisible to the user: our own pages just work.
    with make_client(make_handler()) as client:
        assert f"t={client.app.state.ids.site_token}" in client.get("/").text


def test_state_changing_links_stay_plain_anchors_for_old_safari():
    # No <form>, no method=post, no JavaScript — an iOS 5 tap must be enough.
    with make_client(make_handler()) as client:
        detail = client.get(f"/book/{_first_book_id(client)}").text
        star_markup = detail[detail.index("/star/") - 200:detail.index("/star/") + 100]
        assert "<a " in star_markup
        assert "method=" not in star_markup.lower()


def test_prefs_without_the_token_is_refused():
    with make_client(make_handler()) as client:
        assert client.get("/prefs?color=green&next=/",
                          follow_redirects=False).status_code == 403


def _keyed_app():
    return create_app(load_config({**ENV, "BRIDGE_ACCESS_KEY": "hunter2000"}))


def test_access_key_once_in_the_url_then_links_just_work():
    with TestClient(_keyed_app()) as client:
        assert client.get("/?key=hunter2000").status_code == 200
        # Every internal link omits the key; before the cookie they all 403'd.
        assert client.get("/list").status_code == 200
        assert client.get("/help").status_code == 200


def test_access_key_is_still_required():
    with TestClient(_keyed_app()) as client:
        assert client.get("/list").status_code == 403
        assert client.get("/list?key=wrong").status_code == 403


def test_access_key_header_still_works_for_opds_readers():
    with TestClient(_keyed_app()) as client:
        assert client.get("/list", headers={"X-Access-Key": "hunter2000"}).status_code == 200


def test_health_stays_open_for_the_container_healthcheck():
    with TestClient(_keyed_app()) as client:
        assert client.get("/health").status_code == 200
