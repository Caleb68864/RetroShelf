"""Tiny JSON-file persistence for the Reading List and download history.

No database — a single small JSON file in the ``/config`` volume, written
atomically. This is what turns RetroShelf from an OPDS proxy into a personal,
cross-library reading manager: a book starred from Gutenberg, ManyBooks, or
Kavita all live in one list. Safe for a low-traffic LAN tool (a process-wide
lock serialises every read *and* write).

Durability rules this module holds to:

- **Nothing a feed says can grow the file without bound.** Records are
  projected onto a known key set and every string is length-capped, so a
  catalogue with a 4MB book blurb cannot inflate the state file.
- **A corrupt file never costs data silently.** It is moved aside to
  ``.corrupt`` and logged, so the operator can recover it by hand.
- **A write either lands whole or not at all** — temp file, ``fsync``,
  ``os.replace``, then ``fsync`` on the directory.
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
# The Reading List is hand-curated, so this ceiling exists only so a stuck
# client cannot grow the file forever. Oldest entries are dropped first.
_MAX_FAVORITES = 2000

# The only keys a persisted record may carry, and how long each may be. ``u``
# (the download URL) gets the most room; free text from an upstream summary
# gets the least.
_FIELD_LIMITS = {
    "u": 2048, "m": 128, "t": 512, "a": 256, "s": 2000, "c": 2048,
    "key": 64, "feed_name": 128,
}
_MAX_FORMATS = 8


def book_key(url: str) -> str:
    """Stable short identifier for a book, derived from its download URL.

    The same physical book has the same key regardless of which feed surfaced
    it, so the Reading List and history de-duplicate across libraries.

    :param url: The upstream acquisition (download) URL.
    :returns: A 16-hex-char key.
    :rtype: str
    """
    return hashlib.sha256((url or "").encode("utf-8")).hexdigest()[:16]


def _clean_text(value, limit: int) -> str:
    """Coerce *value* to a length-capped string, dropping control characters."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = "".join(ch for ch in text if ch == " " or ch.isprintable())
    return text[:limit]


def sanitize_record(record: dict) -> dict:
    """Project *record* onto the persisted schema with every field bounded.

    Anything the upstream catalogue supplies passes through here before it can
    reach disk, so the state file's size is a function of how many books were
    starred — never of how verbose one feed happens to be.

    :param record: A raw book view-record.
    :returns: A new dict containing only known, length-capped fields.
    :rtype: dict
    """
    if not isinstance(record, dict):
        return {}
    out: dict[str, object] = {
        k: _clean_text(record.get(k), limit) for k, limit in _FIELD_LIMITS.items()
        if record.get(k) is not None
    }
    # Timestamps are the ordering key for both lists, so they must survive a
    # reload — but only as numbers, never as whatever a feed put there.
    for stamp in ("added", "when"):
        value = record.get(stamp)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[stamp] = float(value)
    formats = record.get("fmts")
    if isinstance(formats, list):
        clean: list[dict[str, object]] = []
        for item in formats[:_MAX_FORMATS]:
            if not isinstance(item, dict) or not item.get("u"):
                continue
            entry: dict[str, object] = {
                "u": _clean_text(item.get("u"), _FIELD_LIMITS["u"]),
                "m": _clean_text(item.get("m"), _FIELD_LIMITS["m"]),
                "f": "pdf" if item.get("f") == "pdf" else "epub",
            }
            length = item.get("len")
            entry["len"] = length if isinstance(length, int) and 0 <= length < 2**40 else None
            clean.append(entry)
        if clean:
            out["fmts"] = clean
    return out


class Store:
    """JSON-backed store of favourites (Reading List) and download history."""

    def __init__(self, path: str):
        """:param path: Filesystem path to the JSON state file."""
        self._path = path
        self._lock = threading.RLock()
        self._data: dict = {"favorites": {}, "history": []}
        self._load()

    # -- persistence ---------------------------------------------------------
    def _load(self) -> None:
        """Read the state file, tolerating absence, corruption, and wrong shapes.

        A file that parses but has the wrong structure (a list where a mapping
        belongs, entries that are not dicts) is repaired field-by-field rather
        than trusted: a bad value must not surface as an ``AttributeError`` on
        some later request.
        """
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (ValueError, UnicodeDecodeError) as exc:
            self._quarantine(exc)
            return
        except Exception as exc:  # noqa: BLE001 - never let a bad file crash startup
            log.warning("Could not read state file %s: %s", self._path, exc)
            return

        if not isinstance(data, dict):
            self._quarantine("top-level value is not an object")
            return

        favorites = data.get("favorites")
        if isinstance(favorites, dict):
            self._data["favorites"] = {
                str(k): sanitize_record(v)
                for k, v in list(favorites.items())[:_MAX_FAVORITES]
                if isinstance(v, dict)
            }
        history = data.get("history")
        if isinstance(history, list):
            self._data["history"] = [
                sanitize_record(h) for h in history[:_MAX_HISTORY] if isinstance(h, dict)
            ]

    def _quarantine(self, reason) -> None:
        """Move an unreadable state file aside so the operator can recover it."""
        backup = f"{self._path}.corrupt"
        try:
            os.replace(self._path, backup)
            log.warning("State file %s was unreadable (%s); moved to %s and starting empty.",
                        self._path, reason, backup)
        except OSError as exc:
            log.warning("State file %s was unreadable (%s) and could not be moved aside: %s",
                        self._path, reason, exc)

    def _save(self) -> None:
        """Persist state atomically and durably. Caller must hold the lock."""
        tmp = f"{self._path}.{os.getpid()}.tmp"
        try:
            directory = os.path.dirname(self._path) or "."
            os.makedirs(directory, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
            # fsync the directory so the rename itself survives a power cut.
            try:
                dir_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass  # not all filesystems allow this; the rename is still atomic
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            log.warning("Could not persist state to %s: %s", self._path, exc)
            try:
                os.remove(tmp)
            except OSError:
                pass

    # -- Reading List --------------------------------------------------------
    def add_favorite(self, record: dict) -> str:
        """Add *record* (a book view-record with a ``u`` URL) to the list.

        :returns: The book key.
        :rtype: str
        """
        key = book_key(record.get("u", ""))
        with self._lock:
            rec = sanitize_record(record)
            rec["key"] = key
            rec["added"] = time.time()
            self._data["favorites"][key] = rec
            self._trim_favorites()
            self._save()
        return key

    def _trim_favorites(self) -> None:
        """Drop the oldest favourites past the cap. Caller must hold the lock."""
        favorites = self._data["favorites"]
        if len(favorites) <= _MAX_FAVORITES:
            return
        ordered = sorted(favorites.items(), key=lambda kv: kv[1].get("added", 0), reverse=True)
        self._data["favorites"] = dict(ordered[:_MAX_FAVORITES])

    def remove_favorite(self, key: str) -> None:
        """Remove the favourite identified by *key* (no-op if absent)."""
        with self._lock:
            if self._data["favorites"].pop(key, None) is not None:
                self._save()

    def is_favorite(self, key: str) -> bool:
        """:returns: ``True`` if *key* is in the Reading List."""
        with self._lock:
            return key in self._data["favorites"]

    def favorite_keys(self) -> set[str]:
        """:returns: The set of starred book keys."""
        with self._lock:
            return set(self._data["favorites"].keys())

    def favorites(self) -> list[dict]:
        """:returns: Favourite records, most-recently-added first."""
        with self._lock:
            return sorted(self._data["favorites"].values(),
                          key=lambda r: r.get("added", 0), reverse=True)

    # -- Download history ----------------------------------------------------
    def record_download(self, record: dict) -> None:
        """Record a download of *record*, moving it to the front of history."""
        key = book_key(record.get("u", ""))
        with self._lock:
            self._data["history"] = [h for h in self._data["history"] if h.get("key") != key]
            rec = sanitize_record(record)
            rec["key"] = key
            rec["when"] = time.time()
            self._data["history"].insert(0, rec)
            del self._data["history"][_MAX_HISTORY:]
            self._save()

    def downloaded_keys(self) -> set[str]:
        """:returns: The set of book keys that have been downloaded."""
        with self._lock:
            return {h.get("key") for h in self._data["history"]}

    def recent_downloads(self, limit: int = 12) -> list[dict]:
        """:returns: The *limit* most recent download records."""
        with self._lock:
            return list(self._data["history"][:limit])
