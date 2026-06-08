"""Tiny JSON-file persistence for the Reading List and download history.

No database — a single small JSON file in the ``/config`` volume, written
atomically. This is what turns RetroShelf from an OPDS proxy into a personal,
cross-library reading manager: a book starred from Gutenberg, ManyBooks, or
Kavita all live in one list. Safe for a low-traffic LAN tool (a process-wide
lock serialises writes).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time

log = logging.getLogger("retroshelf.store")

_MAX_HISTORY = 300


def book_key(url: str) -> str:
    """Stable short identifier for a book, derived from its download URL.

    The same physical book has the same key regardless of which feed surfaced
    it, so the Reading List and history de-duplicate across libraries.

    :param url: The upstream acquisition (download) URL.
    :returns: A 16-hex-char key.
    :rtype: str
    """
    return hashlib.sha256((url or "").encode("utf-8")).hexdigest()[:16]


class Store:
    """JSON-backed store of favourites (Reading List) and download history."""

    def __init__(self, path: str):
        """:param path: Filesystem path to the JSON state file."""
        self._path = path
        self._lock = threading.Lock()
        self._data: dict = {"favorites": {}, "history": []}
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._data["favorites"] = data.get("favorites", {}) or {}
                self._data["history"] = data.get("history", []) or []
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001 - never let a bad file crash startup
            log.warning("Could not read state file %s: %s", self._path, exc)

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = f"{self._path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f)
            os.replace(tmp, self._path)
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            log.warning("Could not persist state to %s: %s", self._path, exc)

    # -- Reading List --------------------------------------------------------
    def add_favorite(self, record: dict) -> str:
        """Add *record* (a book view-record with a ``u`` URL) to the list.

        :returns: The book key.
        :rtype: str
        """
        key = book_key(record.get("u", ""))
        with self._lock:
            rec = dict(record)
            rec["key"] = key
            rec["added"] = time.time()
            self._data["favorites"][key] = rec
            self._save()
        return key

    def remove_favorite(self, key: str) -> None:
        """Remove the favourite identified by *key* (no-op if absent)."""
        with self._lock:
            if self._data["favorites"].pop(key, None) is not None:
                self._save()

    def is_favorite(self, key: str) -> bool:
        """:returns: ``True`` if *key* is in the Reading List."""
        return key in self._data["favorites"]

    def favorite_keys(self) -> set[str]:
        """:returns: The set of starred book keys."""
        return set(self._data["favorites"].keys())

    def favorites(self) -> list[dict]:
        """:returns: Favourite records, most-recently-added first."""
        return sorted(self._data["favorites"].values(),
                      key=lambda r: r.get("added", 0), reverse=True)

    # -- Download history ----------------------------------------------------
    def record_download(self, record: dict) -> None:
        """Record a download of *record*, moving it to the front of history."""
        key = book_key(record.get("u", ""))
        with self._lock:
            self._data["history"] = [h for h in self._data["history"] if h.get("key") != key]
            rec = dict(record)
            rec["key"] = key
            rec["when"] = time.time()
            self._data["history"].insert(0, rec)
            del self._data["history"][_MAX_HISTORY:]
            self._save()

    def downloaded_keys(self) -> set[str]:
        """:returns: The set of book keys that have been downloaded."""
        return {h.get("key") for h in self._data["history"]}

    def recent_downloads(self, limit: int = 12) -> list[dict]:
        """:returns: The *limit* most recent download records."""
        return self._data["history"][:limit]
