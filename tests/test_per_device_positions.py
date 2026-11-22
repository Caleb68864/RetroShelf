"""Opt-in per-device reading positions (deferred item, built 2026-11-21).

With PER_DEVICE_POSITIONS on, each browser gets a random ``rs_device``
cookie and resumes where *it* left off. A device that has not read the book
yet resumes at the profile-wide position. Off (the default), nothing changes.
"""
import json
import os
import tempfile

from app.store import Store, book_key
from tests.test_reader_routes import _bid, make_client, make_handler
from tests.test_reader_shelve import make_epub

REC = {"u": "http://lib/b1", "t": "B"}
DEV_A, DEV_B = "a" * 32, "b" * 32


def _store(path=None):
    return Store(path or os.path.join(tempfile.mkdtemp(), "state.json"))


def test_store_keeps_a_position_per_device_with_profile_fallback():
    st = _store()
    key = book_key("http://lib/b1")
    st.set_position(REC, 5, 0, 50, device=DEV_A)
    st.set_position(REC, 2, 0, 20, device=DEV_B)
    assert st.get_position(key, device=DEV_A)["chapter"] == 5
    assert st.get_position(key, device=DEV_B)["chapter"] == 2
    # A new device, or the feature off: the profile-wide (latest) position.
    assert st.get_position(key, device="c" * 32)["chapter"] == 2
    assert st.get_position(key)["chapter"] == 2


def test_device_positions_persist_and_unused_file_shape_is_unchanged():
    path = os.path.join(tempfile.mkdtemp(), "state.json")
    st = _store(path)
    st.set_position(REC, 1, 0, 10)
    with open(path) as f:
        assert "device_reading" not in json.load(f)
    st.set_position(REC, 3, 0, 30, device=DEV_A)
    assert _store(path).get_position(book_key("http://lib/b1"), device=DEV_A)["chapter"] == 3


def test_bad_device_ids_are_ignored():
    st = _store()
    st.set_position(REC, 4, 0, 40, device="../../etc")
    assert st.get_position(book_key("http://lib/b1"), device="../../etc")["chapter"] == 4  # profile-wide
    assert st._data["device_reading"] == {}


def test_two_browsers_resume_independently_when_enabled(tmp_path):
    handler, _ = make_handler(make_epub(chapters=3))
    env = {"PER_DEVICE_POSITIONS": "1"}
    with make_client(handler, str(tmp_path), env) as ipad:
        bid = _bid(ipad)
        ipad.get(f"/read/{bid}", follow_redirects=False)
        ipad.get(f"/read/{bid}/2/1")
        cookie = ipad.cookies.get("rs_device")
        assert cookie and len(cookie) == 32
        # A second browser on the same app and profile: its own cookie.
        store = ipad.app.state.store
        other = dict(ipad.cookies)
        ipad.cookies.clear()
        ipad.cookies.update({k: v for k, v in other.items() if k != "rs_device"})
        ipad.get(f"/read/{bid}/1/1")
        second = ipad.cookies.get("rs_device")
        assert second and second != cookie
        assert ipad.get(f"/read/{bid}", follow_redirects=False).headers["location"] == f"/read/{bid}/1/1"
        ipad.cookies.set("rs_device", cookie)
        assert ipad.get(f"/read/{bid}", follow_redirects=False).headers["location"] == f"/read/{bid}/2/1"
        assert store.get_position(next(iter(store._data["reading"])))["chapter"] == 1


def test_disabled_by_default_sets_no_cookie(tmp_path):
    handler, _ = make_handler(make_epub(chapters=2))
    with make_client(handler, str(tmp_path)) as client:
        bid = _bid(client)
        client.get(f"/read/{bid}", follow_redirects=False)
        client.get(f"/read/{bid}/1/1")
        assert "rs_device" not in client.cookies
