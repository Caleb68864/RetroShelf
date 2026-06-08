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
