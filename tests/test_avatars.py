"""Built-in profile avatars (deferred item, built 2026-11-21).

iOS 5 Safari cannot upload files, so avatars are a fixed set of shapes in
the profile's colour: tiny static baseline PNGs, chosen with a plain form.
"""
import pathlib

import pytest
from PIL import Image

from app.store import AVATAR_SHAPES, PROFILE_COLORS, Store
from tests.test_auth_routes import _csrf, make_client, register_admin

AVATARS = pathlib.Path(__file__).resolve().parent.parent / "app" / "static" / "avatars"


def test_every_shape_and_colour_has_a_small_baseline_png():
    for shape in AVATAR_SHAPES:
        for colour in PROFILE_COLORS:
            path = AVATARS / f"{shape}-{colour}.png"
            assert path.stat().st_size < 2048, path.name
            with Image.open(path) as im:
                assert im.format == "PNG" and im.size == (48, 48)
                assert not im.info.get("interlace")


def test_store_sets_a_known_avatar_and_refuses_others(tmp_path):
    st = Store(str(tmp_path / "s.json"))
    acct = st.create_account("ann", "password1")
    pid = st.add_profile(acct, "Ann")
    assert st.set_avatar(acct, pid, "star") is True
    assert Store(str(tmp_path / "s.json")).get_account(acct)["profiles"][pid]["avatar"] == "star"
    with pytest.raises(ValueError):
        st.set_avatar(acct, pid, "../../etc/passwd")
    assert st.set_avatar(acct, "nope", "star") is False


def test_account_page_sets_the_picture_and_profiles_page_shows_it():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        page = client.get("/account").text
        assert "/static/avatars/circle-amber.png" in page  # default
        pid = client._store.get_account_by_username("admin")["profiles"]
        pid = next(iter(pid))
        r = client.post("/account", data={"_csrf": _csrf(page), "action": "set_avatar",
                                          "profile_id": pid, "avatar": "moon"})
        assert r.status_code == 200 and "Picture changed." in r.text
        assert "/static/avatars/moon-amber.png" in client.get("/profiles").text
        assert client.get("/static/avatars/moon-amber.png").status_code == 200
        bad = client.post("/account", data={"_csrf": _csrf(r.text), "action": "set_avatar",
                                            "profile_id": pid, "avatar": "<script>"})
        assert bad.status_code == 400
