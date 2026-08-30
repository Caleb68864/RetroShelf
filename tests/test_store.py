"""Tests for app.store — Reading List + download history persistence."""
import os
import tempfile

from app.store import Store, book_key


def _store() -> Store:
    return Store(os.path.join(tempfile.mkdtemp(), "state.json"))


def test_favorites_add_is_remove():
    s = _store()
    key = s.add_favorite({"u": "http://x/1.epub", "t": "Book One"})
    assert s.is_favorite(key)
    assert key in s.favorite_keys()
    assert any(f["t"] == "Book One" for f in s.favorites())
    s.remove_favorite(key)
    assert not s.is_favorite(key)
    assert s.favorites() == []


def test_history_dedup_and_recency():
    s = _store()
    s.record_download({"u": "http://x/1.epub", "t": "A"})
    s.record_download({"u": "http://x/2.epub", "t": "B"})
    s.record_download({"u": "http://x/1.epub", "t": "A"})  # re-download → front, no dup
    recent = s.recent_downloads()
    assert [r["t"] for r in recent] == ["A", "B"]
    assert book_key("http://x/1.epub") in s.downloaded_keys()


def test_cross_feed_dedup_same_url_same_key():
    # The same book (same download URL) surfaced from two feeds shares a key.
    assert book_key("https://manybooks.net/dl/9.epub") == book_key("https://manybooks.net/dl/9.epub")
    assert book_key("a") != book_key("b")


def test_persistence_roundtrip():
    path = os.path.join(tempfile.mkdtemp(), "state.json")
    s1 = Store(path)
    s1.add_favorite({"u": "http://x/1.epub", "t": "Persisted"})
    s1.record_download({"u": "http://x/2.epub", "t": "Hist"})
    s2 = Store(path)  # fresh instance reloads from disk
    assert any(f["t"] == "Persisted" for f in s2.favorites())
    assert s2.recent_downloads()[0]["t"] == "Hist"


def test_unwritable_path_does_not_crash():
    # A bad directory must not raise — persistence is best-effort.
    s = Store("/nonexistent-dir-xyz/state.json")
    key = s.add_favorite({"u": "http://x/1.epub", "t": "InMemory"})
    assert s.is_favorite(key)  # still works in memory


def test_bookmarks_add_list_dedupe_and_remove(tmp_path):
    p = str(tmp_path / "s.json")
    s = Store(p)
    s.add_bookmark("bk", 2, 5, "Chapter Three")
    s.add_bookmark("bk", 0, 1, "Chapter One")
    # Re-bookmarking the same spot updates rather than duplicating.
    s.add_bookmark("bk", 0, 1, "Chapter One (again)")
    marks = s.bookmarks("bk")
    assert [(m["chapter"], m["block"]) for m in marks] == [(0, 1), (2, 5)]  # reading order
    assert marks[0]["label"] == "Chapter One (again)"
    assert len(marks) == 2  # not 3

    # Persists across reload.
    assert [(m["chapter"], m["block"]) for m in Store(p).bookmarks("bk")] == [(0, 1), (2, 5)]

    s.remove_bookmark("bk", 0, 1)
    assert [(m["chapter"], m["block"]) for m in s.bookmarks("bk")] == [(2, 5)]
    s.remove_bookmark("bk", 2, 5)  # last one → book entry dropped
    assert s.bookmarks("bk") == []


def test_bookmarks_reject_bad_positions_on_load(tmp_path):
    import json
    p = str(tmp_path / "s.json")
    with open(p, "w") as f:
        json.dump({"bookmarks": {"bk": [
            {"chapter": 1, "block": 2, "label": "ok"},
            {"chapter": -1, "block": 0},          # bad
            "not a dict",                          # bad
        ]}}, f)
    s = Store(p)
    assert [(m["chapter"], m["block"]) for m in s.bookmarks("bk")] == [(1, 2)]


def test_bookmarks_wrong_shape_loads_empty(tmp_path):
    import json
    p = str(tmp_path / "s.json")
    with open(p, "w") as f:
        json.dump({"bookmarks": "not a dict"}, f)
    s = Store(p)  # must not raise
    assert s.bookmarks("anything") == []


# -- accounts + profiles (opt-in) -------------------------------------------
def test_first_account_is_admin_and_username_is_unique(tmp_path):
    s = Store(str(tmp_path / "s.json"))
    assert s.has_accounts() is False
    admin_id = s.create_account("Admin", "hunter2000")
    assert s.has_accounts() and s.account_count() == 1
    admin = s.get_account(admin_id)
    assert admin["is_admin"] is True
    # The public view never leaks the password hash.
    assert "pw_hash" not in admin and "pw_salt" not in admin
    # Second account is not admin.
    second_id = s.create_account("bob", "password1")
    assert s.get_account(second_id)["is_admin"] is False
    # Usernames are unique, case-insensitively.
    import pytest
    with pytest.raises(ValueError):
        s.create_account("admin", "whatever9")


def test_verify_login_and_password_change_revokes(tmp_path):
    s = Store(str(tmp_path / "s.json"))
    acct_id = s.create_account("alice", "correct-horse")
    assert s.verify_login("alice", "correct-horse") == acct_id
    assert s.verify_login("alice", "wrong") is None
    assert s.verify_login("nobody", "correct-horse") is None
    v0 = s.get_account(acct_id)["token_version"]
    assert s.set_password(acct_id, "new-passphrase") is True
    # token_version bumped (revokes old sessions) and the new password works.
    assert s.get_account(acct_id)["token_version"] == v0 + 1
    assert s.verify_login("alice", "new-passphrase") == acct_id
    assert s.verify_login("alice", "correct-horse") is None


def test_profiles_add_rename_delete_and_isolation(tmp_path):
    import pytest
    s = Store(str(tmp_path / "s.json"))
    a = s.create_account("a", "passworda")
    b = s.create_account("b", "passwordb")
    pa = s.add_profile(a, "Ann", "amber")
    pb = s.add_profile(b, "Bob", "green")
    # A profile only belongs to its own account (cross-account isolation check).
    assert s.profile_belongs(a, pa) is True
    assert s.profile_belongs(a, pb) is False
    assert s.profile_belongs(b, pa) is False
    # Rename is scoped to the owning account; a cross-account rename is refused.
    assert s.rename_profile(a, pa, "Annie") is True
    assert s.rename_profile(b, pa, "Hijack") is False
    assert s.get_account(a)["profiles"][pa]["name"] == "Annie"
    # An account must keep at least one profile.
    with pytest.raises(ValueError):
        s.delete_profile(a, pa)
    pa2 = s.add_profile(a, "Second", "cyan")
    assert s.delete_profile(a, pa2) is True
    # A cross-account delete is refused.
    assert s.delete_profile(b, pa) is False


def test_two_profiles_have_independent_reading_state(tmp_path):
    s = Store(str(tmp_path / "s.json"))
    s.add_favorite({"u": "http://x/1.epub", "t": "For P1"}, profile_id="p1")
    s.set_position({"u": "http://x/1.epub", "t": "For P1"}, 3, 2, 40, profile_id="p1")
    s.add_bookmark("bk1", 1, 1, "P1 mark", profile_id="p1")
    # p2 sees none of p1's state.
    assert [f["t"] for f in s.favorites(profile_id="p1")] == ["For P1"]
    assert s.favorites(profile_id="p2") == []
    assert s.get_position(book_key("http://x/1.epub"), profile_id="p2") is None
    assert s.bookmarks("bk1", profile_id="p2") == []
    # And the sentinel (accounts-off) profile is independent of both.
    assert s.favorites() == []


def test_migrate_global_state_into_first_profile(tmp_path):
    p = str(tmp_path / "s.json")
    s = Store(p)
    # Simulate pre-accounts (global/sentinel) reading state.
    s.add_favorite({"u": "http://x/1.epub", "t": "Old Global"})
    s.set_position({"u": "http://x/1.epub", "t": "Old Global"}, 2, 1, 33)
    acct = s.create_account("admin", "hunter2000")
    prof = s.add_profile(acct, "admin", "amber")
    s.migrate_global_state_into(prof)
    # State moved into the profile; the sentinel is now empty.
    assert [f["t"] for f in s.favorites(profile_id=prof)] == ["Old Global"]
    assert s.get_position(book_key("http://x/1.epub"), profile_id=prof) is not None
    assert s.favorites() == []
    assert s.reading_list() == []
    # Persists across reload.
    assert [f["t"] for f in Store(p).favorites(profile_id=prof)] == ["Old Global"]


def test_hostile_file_shapes_never_crash_startup(tmp_path):
    import json
    # A missing/corrupt/wrong-shaped file must never crash startup: every section
    # is repaired field-by-field or quarantined, and reads must return safely.
    shapes = [
        [1, 2, 3],                                              # top-level list
        {"favorites": [1, 2]},                                  # favorites wrong type
        {"favorites": {"k": "notadict"}},                       # fav value not dict
        {"history": {"notalist": 1}},                           # history wrong type
        {"reading": {"k": {"chapter": "x", "block": 1, "percent": 2}}},  # bad pos
        {"bookmarks": {"bk": {"notalist": 1}}},                 # per-book not list
        {"shelves": {"s": {"items": [1, 2]}}},                  # shelf items not dict
        {"shelves": "x"},                                       # shelves not dict
        {"accounts": [1, 2]},                                   # accounts not dict
        {"accounts": {"a": {"username": "u", "pw_hash": "h",
                            "pw_salt": "s", "iterations": True}}},  # bool iters dropped
        {"profile_state": [1, 2, 3]},                           # profile_state not dict
        {"profile_state": {"p": "notadict"}},                   # profile value not dict
    ]
    for i, sh in enumerate(shapes):
        p = str(tmp_path / f"s{i}.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(sh, f)
        s = Store(p)  # must not raise
        # Exercising every read path must not surface a bad value.
        s.favorites()
        s.recent_downloads()
        s.list_shelves()
        s.has_accounts()
        s.bookmarks("bk")
        s.reading_list()


def test_bad_position_dropped_and_percent_clamped_on_load(tmp_path):
    import json
    p = str(tmp_path / "s.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"reading": {
            "good": {"u": "http://x/1", "chapter": 1, "block": 2, "percent": 999},
            "bad": {"chapter": "x", "block": 1, "percent": 2},
            "neg": {"chapter": -1, "block": 0, "percent": 0},
        }}, f)
    s = Store(p)
    good = s.get_position("good")
    assert good is not None and good["percent"] == 100  # clamped
    assert s.get_position("bad") is None
    assert s.get_position("neg") is None


def test_migrate_carries_shelves_and_new_shelf_does_not_crash(tmp_path):
    # Regression: migrate_global_state_into once omitted the ``shelves`` section,
    # so the migrated profile-state dict had no ``shelves`` key and the admin's
    # first create_shelf/list_shelves KeyError'd. Pre-accounts shelves must carry
    # over, and shelf ops on the migrated profile must keep working.
    p = str(tmp_path / "s.json")
    s = Store(p)
    sid = s.create_shelf("Pre-accounts shelf")
    s.add_to_shelf(sid, {"u": "http://x/1.epub", "t": "Old"})
    acct = s.create_account("admin", "hunter2000")
    prof = s.add_profile(acct, "admin", "amber")
    s.migrate_global_state_into(prof)
    # The old shelf came across, and the sentinel is now empty.
    names = [row["name"] for row in s.list_shelves(profile_id=prof)]
    assert names == ["Pre-accounts shelf"]
    assert s.list_shelves() == []
    # Creating and listing a fresh shelf on the migrated profile must not crash.
    new_sid = s.create_shelf("Brand new", profile_id=prof)
    assert new_sid in {r["id"] for r in s.list_shelves(profile_id=prof)}
    # Survives a reload.
    assert sorted(r["name"] for r in Store(p).list_shelves(profile_id=prof)) == \
        ["Brand new", "Pre-accounts shelf"]


def test_profile_state_count_is_capped_on_load(tmp_path):
    import json

    from app.store import _MAX_PROFILE_STATES
    p = str(tmp_path / "s.json")
    # A hostile/corrupt file lists far more profile-state blobs than could ever
    # legitimately exist; the loader must not import them all.
    bogus = {str(i): {"favorites": {}, "history": [], "reading": {}, "bookmarks": {}}
             for i in range(_MAX_PROFILE_STATES + 250)}
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"favorites": {}, "history": [], "reading": {}, "bookmarks": {},
                   "profile_state": bogus}, f)
    s = Store(p)  # must not hang or blow up
    assert len(s._data["profile_state"]) <= _MAX_PROFILE_STATES


def test_loaded_mapping_keys_are_length_capped(tmp_path):
    import json

    from app.store import _MAX_STATE_KEY
    p = str(tmp_path / "s.json")
    huge = "k" * 100_000  # a hostile/bit-rotted key
    with open(p, "w", encoding="utf-8") as f:
        json.dump({
            "favorites": {huge: {"u": "http://x/1", "t": "T"}},
            "reading": {huge: {"chapter": 0, "block": 0, "percent": 5}},
            "shelves": {huge: {"name": "S", "items": {huge: {"u": "http://x/2"}}}},
        }, f)
    s = Store(p)
    # No key that survived the load exceeds the cap.
    fav_keys = list(s._pstate_ro("_")["favorites"].keys())
    assert fav_keys and all(len(k) <= _MAX_STATE_KEY for k in fav_keys)
    read_keys = list(s._pstate_ro("_")["reading"].keys())
    assert read_keys and all(len(k) <= _MAX_STATE_KEY for k in read_keys)
    shelf_keys = list(s._pstate_ro("_")["shelves"].keys())
    assert shelf_keys and all(len(k) <= _MAX_STATE_KEY for k in shelf_keys)


def test_format_len_rejects_bool(tmp_path):
    from app.store import sanitize_record
    # ``True``/``False`` are ints in Python; a crafted feed must not smuggle one
    # into the numeric ``len`` field.
    rec = sanitize_record({"u": "http://x/1.epub", "fmts": [
        {"u": "http://x/a.epub", "f": "epub", "len": True},
        {"u": "http://x/b.epub", "f": "epub", "len": 1234},
    ]})
    assert rec["fmts"][0]["len"] is None
    assert rec["fmts"][1]["len"] == 1234


def test_accounts_off_file_stays_pre_accounts_shape(tmp_path):
    import json
    p = str(tmp_path / "s.json")
    s = Store(p)
    s.add_favorite({"u": "http://x/1.epub", "t": "A"})
    with open(p, encoding="utf-8") as f:
        raw = json.load(f)
    # No accounts were created → the accounts/profile_state keys are never
    # written, so the file is byte-shape-identical to the pre-accounts format.
    assert set(raw) == {"favorites", "history", "reading", "bookmarks"}
