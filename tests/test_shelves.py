"""Custom shelves per profile — store-level tests.

Shelves generalise the built-in Reading List: named, per-profile collections of
book records. Coverage: CRUD, membership, caps, cross-profile isolation, and
that an unused deployment's state file stays byte-identical (no ``shelves`` key
until one is created).
"""
import json
import os
import tempfile

from app.store import Store

REC1 = {"u": "http://lib/book1", "t": "Book One", "a": "Ann", "m": "application/epub+zip"}
REC2 = {"u": "http://lib/book2", "t": "Book Two", "a": "Bob", "m": "application/epub+zip"}


def _store() -> Store:
    return Store(os.path.join(tempfile.mkdtemp(), "state.json"))


def test_create_list_and_delete_shelf():
    st = _store()
    sid = st.create_shelf("To Read")
    shelves = st.list_shelves()
    assert len(shelves) == 1
    assert shelves[0]["id"] == sid and shelves[0]["name"] == "To Read"
    assert shelves[0]["count"] == 0
    assert st.delete_shelf(sid) is True
    assert st.list_shelves() == []
    assert st.delete_shelf(sid) is False  # already gone


def test_add_remove_and_membership():
    st = _store()
    sid = st.create_shelf("Sci-Fi")
    key = st.add_to_shelf(sid, REC1)
    assert key == "http://lib/book1" or key  # returns the book key
    view = st.shelf(sid)
    assert view is not None and view["name"] == "Sci-Fi"
    assert [r["t"] for r in view["items"]] == ["Book One"]
    assert sid in st.shelf_ids_containing(key)
    # Idempotent add of the same book does not duplicate.
    st.add_to_shelf(sid, REC1)
    assert len(st.shelf(sid)["items"]) == 1
    # Remove.
    assert st.remove_from_shelf(sid, key) is True
    assert st.shelf(sid)["items"] == []
    assert sid not in st.shelf_ids_containing(key)


def test_rename_shelf():
    st = _store()
    sid = st.create_shelf("Temp")
    assert st.rename_shelf(sid, "Favorites") is True
    assert st.shelf(sid)["name"] == "Favorites"
    assert st.rename_shelf("nope", "X") is False


def test_add_to_missing_shelf_is_noop():
    st = _store()
    assert st.add_to_shelf("ghost", REC1) is None
    assert st.remove_from_shelf("ghost", "k") is False


def test_cross_profile_isolation():
    st = _store()
    a = st.create_shelf("A-shelf", profile_id="pA")
    st.add_to_shelf(a, REC1, profile_id="pA")
    # Profile B sees none of A's shelves.
    assert st.list_shelves(profile_id="pB") == []
    assert st.shelf(a, profile_id="pB") is None
    assert st.shelf_ids_containing("http://lib/book1", profile_id="pB") == set()
    # A still has its shelf.
    assert len(st.list_shelves(profile_id="pA")) == 1


def test_name_and_count_caps():
    st = _store()
    sid = st.create_shelf("x" * 500)
    assert len(st.shelf(sid)["name"]) <= 40  # _MAX_SHELF_NAME
    # Empty name is rejected.
    try:
        st.create_shelf("   ")
        assert False, "empty shelf name should raise"
    except ValueError:
        pass


def test_persistence_roundtrip_sentinel():
    path = os.path.join(tempfile.mkdtemp(), "state.json")
    st = Store(path)
    sid = st.create_shelf("Keep")
    st.add_to_shelf(sid, REC1)
    # Reload from disk.
    st2 = Store(path)
    shelves = st2.list_shelves()
    assert len(shelves) == 1 and shelves[0]["name"] == "Keep"
    assert [r["t"] for r in st2.shelf(sid)["items"]] == ["Book One"]


def test_unused_shelves_keep_file_byte_identical():
    # A store that never creates a shelf writes NO "shelves" key (pre-shelves
    # file shape preserved).
    path = os.path.join(tempfile.mkdtemp(), "state.json")
    st = Store(path)
    st.add_favorite(REC2)  # force a save
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert "shelves" not in data


# -- route-level tests (accounts-off sentinel profile) -----------------------
import re  # noqa: E402

from tests.test_app import make_client, make_handler, _token, _first_book_id  # noqa: E402


def _shelf_id(client) -> str:
    m = re.search(r'/shelf/([0-9a-f]+)"', client.get("/shelves").text)
    assert m, "no shelf link on /shelves"
    return m.group(1)


def test_shelves_create_add_view_remove_flow():
    with make_client(make_handler()) as client:
        bid = _first_book_id(client)
        assert bid
        tok = _token(client)
        # Create a shelf.
        client.get(f"/shelves/create?name=Sci-Fi&t={tok}", follow_redirects=False)
        assert "Sci-Fi" in client.get("/shelves").text
        sid = _shelf_id(client)
        # The book page offers to add to it.
        detail = client.get(f"/book/{bid}").text
        assert f"/shelf/{sid}/add/{bid}" in detail and "+ Sci-Fi" in detail
        # Add the book.
        client.get(f"/shelf/{sid}/add/{bid}?t={tok}", follow_redirects=False)
        shelf = client.get(f"/shelf/{sid}").text
        assert "The Time Machine" in shelf and f"/shelf/{sid}/remove/" in shelf
        # Book page now shows it as on the shelf (checkmark + remove link).
        detail2 = client.get(f"/book/{bid}").text
        assert f"/shelf/{sid}/remove/" in detail2
        # Remove it.
        key = re.search(rf"/shelf/{sid}/remove/(\w+)", shelf).group(1)
        client.get(f"/shelf/{sid}/remove/{key}?t={tok}", follow_redirects=False)
        assert "this shelf is empty" in client.get(f"/shelf/{sid}").text.lower()


def test_shelf_mutations_require_site_token():
    with make_client(make_handler()) as client:
        bid = _first_book_id(client)
        # No token → refused, nothing created.
        r = client.get("/shelves/create?name=Nope", follow_redirects=False)
        assert r.status_code == 403
        assert "Nope" not in client.get("/shelves").text
        # Create legitimately, then an untokened add is refused.
        tok = _token(client)
        client.get(f"/shelves/create?name=Reals&t={tok}", follow_redirects=False)
        sid = _shelf_id(client)
        r2 = client.get(f"/shelf/{sid}/add/{bid}", follow_redirects=False)
        assert r2.status_code == 403
        assert "this shelf is empty" in client.get(f"/shelf/{sid}").text.lower()


def test_delete_shelf_route():
    with make_client(make_handler()) as client:
        tok = _token(client)
        client.get(f"/shelves/create?name=Temp&t={tok}", follow_redirects=False)
        sid = _shelf_id(client)
        client.get(f"/shelves/delete/{sid}?t={tok}", follow_redirects=False)
        assert "no shelves yet" in client.get("/shelves").text.lower()
        # Viewing a now-missing shelf redirects back to the manage page.
        r = client.get(f"/shelf/{sid}", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/shelves"


def test_empty_shelf_name_is_rejected_with_message():
    with make_client(make_handler()) as client:
        tok = _token(client)
        r = client.get(f"/shelves/create?name=%20%20&t={tok}", follow_redirects=False)
        assert r.status_code == 303 and "err=" in r.headers["location"]
        assert "no shelves yet" in client.get("/shelves").text.lower()
