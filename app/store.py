"""Tiny JSON-file persistence for the Reading List, download history, and (opt-in)
accounts + profiles.

No database — a single small JSON file in the ``/config`` volume, written
atomically. This is what turns RetroShelf from an OPDS proxy into a personal,
cross-library reading manager: a book starred from Gutenberg, ManyBooks, or
Kavita all live in one list. Safe for a low-traffic LAN tool (a process-wide
lock serialises every read *and* write).

Reading state (favourites, history, reading positions, bookmarks) is keyed by a
**profile id**. In the default (accounts-disabled) deployment every call uses a
single fixed sentinel profile (:data:`_SENTINEL_PROFILE`) whose state lives at
the top level of the JSON file exactly as it always has — so an existing
deployment's state file loads and saves byte-for-byte unchanged, and the whole
accounts machinery costs nothing when it is off. When accounts are enabled each
profile's state lives under :data:`_PROFILE_STATE_KEY`, and
:meth:`Store.migrate_global_state_into` folds any pre-existing sentinel state
into the admin's first profile on setup.

Durability rules this module holds to:

- **Nothing a feed says can grow the file without bound.** Records are
  projected onto a known key set and every string is length-capped, so a
  catalogue with a 4MB book blurb cannot inflate the state file.
- **A corrupt file never costs data silently.** It is moved aside to
  ``.corrupt`` and logged, so the operator can recover it by hand. A malformed
  accounts or profile-state blob is repaired field-by-field, never trusted.
- **A write either lands whole or not at all** — temp file, ``fsync``,
  ``os.replace``, then ``fsync`` on the directory.
- **The password hash never leaves the store.** Account lookups return a public
  view without the hash/salt; verification happens inside
  :meth:`Store.verify_login`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
import time

from . import accounts as _accounts

log = logging.getLogger("retroshelf.store")

_MAX_HISTORY = 300
# The Reading List is hand-curated, so this ceiling exists only so a stuck
# client cannot grow the file forever. Oldest entries are dropped first.
_MAX_FAVORITES = 2000
# Reading positions are one per book, so this ceiling is generous headroom
# against a runaway client rather than a realistic limit. Oldest-updated
# entries are dropped first.
_MAX_READING = 100

# Bookmarks: a per-book list of saved spots. Bounded per book and in total
# against a runaway client. [bookmarks]
_MAX_BOOKMARK_BOOKS = 200
_MAX_BOOKMARKS_PER_BOOK = 50

# -- accounts + profiles bounds ----------------------------------------------
# Ceilings so a runaway or hostile client cannot grow the accounts section
# without bound; all are far above any real household's needs.
_MAX_ACCOUNTS = 200
_MAX_PROFILES = 20  # per account
_MAX_USERNAME = 64
_MAX_PROFILE_NAME = 40
# Netflix-style profile accent choices. Purely cosmetic; validated on the way
# in so a crafted value can never reach a template or a CSS class.
_PROFILE_COLORS = ("amber", "green", "cyan", "white", "magenta", "blue")
#: Public, ordered tuple of the selectable profile accent colours (for forms).
PROFILE_COLORS = _PROFILE_COLORS
_KDF_NAME = "pbkdf2_sha256"

# The fixed profile id used when accounts are disabled (and for any state that
# predates a profile). Its reading state lives at the top level of the JSON
# file, so an accounts-off deployment's file is identical to the pre-accounts
# format. Never a real, user-selectable profile id (those are random hex).
_SENTINEL_PROFILE = "_"
# Top-level key under which each non-sentinel profile's reading state is stored.
_PROFILE_STATE_KEY = "profile_state"

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


def _new_id() -> str:
    """Return a fresh random opaque id for an account or profile.

    :returns: 16 hex characters from :func:`secrets.token_hex`.
    :rtype: str
    """
    return secrets.token_hex(8)


def _clean_text(value: object, limit: int) -> str:
    """Coerce *value* to a length-capped string, dropping control characters.

    :param value: Any value an upstream feed or the state file supplied;
        ``None`` becomes ``""``.
    :param limit: Maximum number of characters to keep.
    :rtype: str
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = "".join(ch for ch in text if ch == " " or ch.isprintable())
    return text[:limit]


def _as_real_number(value: object) -> int | float | None:
    """Return *value* when it is a genuine ``int``/``float``, else ``None``.

    ``bool`` is a subclass of ``int``, so a bare ``isinstance(value, int)`` check
    would accept ``True``/``False`` as numbers. Both the position/bookmark
    coordinates and the ordering timestamps must reject that, so the guard is
    named here rather than re-spelled at each field. Returning the value itself
    (rather than a coerced ``float``) both gives the caller a non-``None``
    binding mypy can narrow on stdlib-only typing — no 3.13 ``TypeIs`` — and
    preserves the exact ``int`` for the integer fields, which a round-trip
    through ``float`` could not.

    :param value: Any value loaded from the state file or a book record.
    :returns: *value* unchanged when it is a real number, otherwise ``None``.
    :rtype: int | float | None
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return None


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
        number = _as_real_number(record.get(stamp))
        if number is not None:
            out[stamp] = float(number)
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


def _sanitize_position(entry: dict) -> dict | None:
    """Validate a persisted reading-position entry.

    Applied both to entries loaded from disk (a hand-edited or corrupted
    state file must not surface a bad ``chapter``/``block`` to the reader
    routes) and to entries built by :meth:`Store.set_position`.

    :param entry: A raw ``reading`` section value.
    :returns: A cleaned dict, or ``None`` if *entry* has no usable position.
    :rtype: dict | None
    """
    rec = sanitize_record(entry)
    for field in ("chapter", "block", "percent"):
        number = _as_real_number(entry.get(field))
        if number is None or number < 0:
            return None
        rec[field] = int(number)
    if rec["percent"] > 100:
        rec["percent"] = 100
    updated = _as_real_number(entry.get("updated"))
    rec["updated"] = float(updated) if updated is not None else 0.0
    key = entry.get("key")
    if isinstance(key, str) and key:
        rec["key"] = _clean_text(key, _FIELD_LIMITS["key"])
    return rec


def _sanitize_bookmark(entry: dict) -> dict | None:
    """Validate a persisted bookmark entry (``chapter``/``block``/``label``).

    Applied to both freshly-created and loaded-from-disk entries, so a
    hand-edited state file can never surface a bad position to the reader.

    :param entry: A raw bookmark value.
    :returns: A cleaned dict, or ``None`` if the position is unusable.
    :rtype: dict | None
    """
    out: dict = {}
    for numeric in ("chapter", "block"):
        number = _as_real_number(entry.get(numeric))
        if number is None or number < 0:
            return None
        out[numeric] = int(number)
    out["label"] = _clean_text(entry.get("label"), _FIELD_LIMITS["t"])
    when = _as_real_number(entry.get("when"))
    out["when"] = float(when) if when is not None else 0.0
    return out


def _empty_state() -> dict:
    """Return a fresh, empty per-profile reading-state dict.

    :returns: ``{"favorites": {}, "history": [], "reading": {}, "bookmarks": {}}``.
    :rtype: dict
    """
    return {"favorites": {}, "history": [], "reading": {}, "bookmarks": {}}


def _clean_state(data: dict) -> dict:
    """Return a validated per-profile reading-state dict from raw *data*.

    Applies the same field-by-field repair to ``favorites``/``history``/
    ``reading``/``bookmarks`` whether they came from the top level of the file
    (the sentinel profile) or from one profile under
    :data:`_PROFILE_STATE_KEY`. A wrong-shaped section becomes empty rather than
    surfacing a bad value to a later request.

    :param data: A raw mapping that may hold the four reading sections.
    :returns: A clean four-section state dict.
    :rtype: dict
    """
    state = _empty_state()
    if not isinstance(data, dict):
        return state
    favorites = data.get("favorites")
    if isinstance(favorites, dict):
        state["favorites"] = {
            str(k): sanitize_record(v)
            for k, v in list(favorites.items())[:_MAX_FAVORITES]
            if isinstance(v, dict)
        }
    history = data.get("history")
    if isinstance(history, list):
        state["history"] = [
            sanitize_record(h) for h in history[:_MAX_HISTORY] if isinstance(h, dict)
        ]
    reading = data.get("reading")
    if isinstance(reading, dict):
        state["reading"] = {
            str(k): entry
            for k, v in list(reading.items())[:_MAX_READING]
            if isinstance(v, dict) and (entry := _sanitize_position(v)) is not None
        }
    bookmarks = data.get("bookmarks")
    if isinstance(bookmarks, dict):
        clean_marks: dict = {}
        for k, v in list(bookmarks.items())[:_MAX_BOOKMARK_BOOKS]:
            if not isinstance(v, list):
                continue
            entries = [
                m for item in v[:_MAX_BOOKMARKS_PER_BOOK]
                if isinstance(item, dict) and (m := _sanitize_bookmark(item)) is not None
            ]
            if entries:
                clean_marks[str(k)] = entries
        state["bookmarks"] = clean_marks
    return state


def _sanitize_profile(raw: object) -> dict | None:
    """Validate one profile-metadata record (``name``/``color``/``created``).

    :param raw: A raw value from an account's ``profiles`` mapping.
    :returns: A cleaned profile dict, or ``None`` when it is not a mapping.
    :rtype: dict | None
    """
    if not isinstance(raw, dict):
        return None
    name = _clean_text(raw.get("name"), _MAX_PROFILE_NAME) or "Reader"
    color = raw.get("color") if raw.get("color") in _PROFILE_COLORS else _PROFILE_COLORS[0]
    created = _as_real_number(raw.get("created"))
    return {"name": name, "color": color, "created": float(created) if created is not None else 0.0}


def _sanitize_account(raw: object) -> dict | None:
    """Validate one account record loaded from disk.

    An account missing a usable username or password hash is dropped (it could
    never authenticate anyway); individual malformed profiles are dropped while
    the account is kept.

    :param raw: A raw value from the ``accounts`` mapping.
    :returns: A cleaned account dict, or ``None`` to drop the account.
    :rtype: dict | None
    """
    if not isinstance(raw, dict):
        return None
    username = _clean_text(raw.get("username"), _MAX_USERNAME)
    pw_hash = raw.get("pw_hash")
    pw_salt = raw.get("pw_salt")
    iterations = raw.get("iterations")
    if not username or not isinstance(pw_hash, str) or not isinstance(pw_salt, str):
        return None
    if not isinstance(iterations, int) or isinstance(iterations, bool) or iterations <= 0:
        return None
    token_version = raw.get("token_version")
    if not isinstance(token_version, int) or isinstance(token_version, bool) or token_version < 1:
        token_version = 1
    created = _as_real_number(raw.get("created"))
    kdf = raw.get("kdf") if isinstance(raw.get("kdf"), str) else _KDF_NAME
    profiles: dict = {}
    raw_profiles = raw.get("profiles")
    if isinstance(raw_profiles, dict):
        for pid, pv in list(raw_profiles.items())[:_MAX_PROFILES]:
            prof = _sanitize_profile(pv)
            if prof is not None:
                profiles[str(pid)] = prof
    return {
        "username": username, "pw_hash": pw_hash, "pw_salt": pw_salt,
        "iterations": iterations, "kdf": kdf, "is_admin": bool(raw.get("is_admin")),
        "token_version": token_version,
        "created": float(created) if created is not None else 0.0,
        "profiles": profiles,
    }


def _public_account(account_id: str, account: dict) -> dict:
    """Return an account view for the app layer, **without** the password hash.

    The store never hands the ``pw_hash``/``pw_salt``/``iterations`` fields to a
    route or template — verification stays inside :meth:`Store.verify_login`.

    :param account_id: The account's id.
    :param account: The internal account record.
    :returns: ``{"account_id", "username", "is_admin", "token_version",
        "created", "profiles"}`` with a copied ``profiles`` mapping.
    :rtype: dict
    """
    return {
        "account_id": account_id,
        "username": account["username"],
        "is_admin": account["is_admin"],
        "token_version": account["token_version"],
        "created": account["created"],
        "profiles": {pid: dict(p) for pid, p in account["profiles"].items()},
    }


class Store:
    """JSON-backed store of favourites, history, bookmarks, and (opt-in) accounts."""

    def __init__(self, path: str) -> None:
        """:param path: Filesystem path to the JSON state file."""
        self._path = path
        self._lock = threading.RLock()
        # The four top-level sections hold the sentinel profile's reading state
        # (accounts-off, and pre-accounts files). ``accounts`` and
        # ``profile_state`` are only ever written when non-empty, so an
        # accounts-off file is byte-identical to the pre-accounts format.
        self._data: dict = {
            "favorites": {}, "history": [], "reading": {}, "bookmarks": {},
            "accounts": {}, _PROFILE_STATE_KEY: {},
        }
        self._load()

    # -- persistence ---------------------------------------------------------
    def _load(self) -> None:
        """Read the state file, tolerating absence, corruption, and wrong shapes.

        A file that parses but has the wrong structure (a list where a mapping
        belongs, entries that are not dicts) is repaired field-by-field rather
        than trusted: a bad value must not surface as an ``AttributeError`` on
        some later request. This holds for the reading sections, the accounts
        blob, and every profile's state alike.
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

        # Sentinel profile: the four reading sections at the top level.
        sentinel = _clean_state(data)
        for name in ("favorites", "history", "reading", "bookmarks"):
            self._data[name] = sentinel[name]

        # Accounts (each with its profile metadata). Malformed accounts dropped.
        accounts = data.get("accounts")
        if isinstance(accounts, dict):
            clean_accounts: dict = {}
            for acct_id, raw in list(accounts.items())[:_MAX_ACCOUNTS]:
                acct = _sanitize_account(raw)
                if acct is not None:
                    clean_accounts[str(acct_id)] = acct
            self._data["accounts"] = clean_accounts

        # Per-profile reading state (non-sentinel profiles).
        profile_state = data.get(_PROFILE_STATE_KEY)
        if isinstance(profile_state, dict):
            self._data[_PROFILE_STATE_KEY] = {
                str(pid): _clean_state(pv)
                for pid, pv in profile_state.items()
                if isinstance(pv, dict)
            }

    def _quarantine(self, reason: object) -> None:
        """Move an unreadable state file aside so the operator can recover it.

        :param reason: Why the file was unreadable (an exception or a short
            description), included in the warning log line.
        """
        backup = f"{self._path}.corrupt"
        try:
            os.replace(self._path, backup)
            log.warning("State file %s was unreadable (%s); moved to %s and starting empty.",
                        self._path, reason, backup)
        except OSError as exc:
            log.warning("State file %s was unreadable (%s) and could not be moved aside: %s",
                        self._path, reason, exc)

    def _save(self) -> None:
        """Persist state atomically and durably. Caller must hold the lock.

        The ``accounts`` and per-profile ``profile_state`` sections are written
        only when non-empty, so an accounts-off deployment's file keeps exactly
        the pre-accounts shape.
        """
        payload: dict = {
            "favorites": self._data["favorites"],
            "history": self._data["history"],
            "reading": self._data["reading"],
            "bookmarks": self._data["bookmarks"],
        }
        if self._data["accounts"]:
            payload["accounts"] = self._data["accounts"]
        if self._data[_PROFILE_STATE_KEY]:
            payload[_PROFILE_STATE_KEY] = self._data[_PROFILE_STATE_KEY]
        tmp = f"{self._path}.{os.getpid()}.tmp"
        try:
            directory = os.path.dirname(self._path) or "."
            os.makedirs(directory, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
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

    # -- per-profile state access -------------------------------------------
    def _pstate(self, profile_id: str) -> dict:
        """Return the writable reading-state dict for *profile_id*, creating it.

        The sentinel profile's state is the top-level four sections; any other
        profile's state is created under :data:`_PROFILE_STATE_KEY` on first
        write. Caller must hold the lock.

        :param profile_id: The profile whose state to fetch (writers).
        :returns: A dict with ``favorites``/``history``/``reading``/``bookmarks``.
        :rtype: dict
        """
        if profile_id == _SENTINEL_PROFILE:
            return self._data
        return self._data[_PROFILE_STATE_KEY].setdefault(profile_id, _empty_state())

    def _pstate_ro(self, profile_id: str) -> dict:
        """Return the reading-state dict for *profile_id* without creating it.

        Readers (and no-op removes) use this so that looking at an unknown
        profile does not persist an empty state entry. For an existing profile
        the real dict is returned (so in-place removal works); for a missing one
        a throwaway empty state is returned. Caller must hold the lock.

        :param profile_id: The profile whose state to fetch.
        :returns: A dict with ``favorites``/``history``/``reading``/``bookmarks``.
        :rtype: dict
        """
        if profile_id == _SENTINEL_PROFILE:
            return self._data
        return self._data[_PROFILE_STATE_KEY].get(profile_id) or _empty_state()

    @staticmethod
    def _trim_mapping(mapping: dict, cap: int, order_key: str) -> None:
        """Drop the lowest-*order_key* entries of *mapping* past *cap*, in place.

        Mutates *mapping* so the caller's reference (a profile's section) stays
        valid. Caller must hold the lock.

        :param mapping: The section dict to bound.
        :param cap: Maximum number of entries to keep.
        :param order_key: Record field to sort by (kept highest-first).
        """
        if len(mapping) <= cap:
            return
        ordered = sorted(mapping.items(), key=lambda kv: kv[1].get(order_key, 0), reverse=True)
        keep = dict(ordered[:cap])
        mapping.clear()
        mapping.update(keep)

    # -- Reading List --------------------------------------------------------
    def add_favorite(self, record: dict, profile_id: str = _SENTINEL_PROFILE) -> str:
        """Add *record* (a book view-record with a ``u`` URL) to the list.

        :param record: A book view-record.
        :param profile_id: The profile whose Reading List to add to.
        :returns: The book key.
        :rtype: str
        """
        key = book_key(record.get("u", ""))
        with self._lock:
            favorites = self._pstate(profile_id)["favorites"]
            rec = sanitize_record(record)
            rec["key"] = key
            rec["added"] = time.time()
            favorites[key] = rec
            self._trim_mapping(favorites, _MAX_FAVORITES, "added")
            self._save()
        return key

    def remove_favorite(self, key: str, profile_id: str = _SENTINEL_PROFILE) -> None:
        """Remove the favourite identified by *key* (no-op if absent)."""
        with self._lock:
            if self._pstate_ro(profile_id)["favorites"].pop(key, None) is not None:
                self._save()

    def is_favorite(self, key: str, profile_id: str = _SENTINEL_PROFILE) -> bool:
        """:returns: ``True`` if *key* is in the profile's Reading List."""
        with self._lock:
            return key in self._pstate_ro(profile_id)["favorites"]

    def favorite_keys(self, profile_id: str = _SENTINEL_PROFILE) -> set[str]:
        """:returns: The set of starred book keys for the profile."""
        with self._lock:
            return set(self._pstate_ro(profile_id)["favorites"].keys())

    def favorites(self, profile_id: str = _SENTINEL_PROFILE) -> list[dict]:
        """:returns: Favourite records, most-recently-added first."""
        with self._lock:
            return sorted(self._pstate_ro(profile_id)["favorites"].values(),
                          key=lambda r: r.get("added", 0), reverse=True)

    # -- Download history ----------------------------------------------------
    def record_download(self, record: dict, profile_id: str = _SENTINEL_PROFILE) -> None:
        """Record a download of *record*, moving it to the front of history."""
        key = book_key(record.get("u", ""))
        with self._lock:
            state = self._pstate(profile_id)
            state["history"] = [h for h in state["history"] if h.get("key") != key]
            rec = sanitize_record(record)
            rec["key"] = key
            rec["when"] = time.time()
            state["history"].insert(0, rec)
            del state["history"][_MAX_HISTORY:]
            self._save()

    def downloaded_keys(self, profile_id: str = _SENTINEL_PROFILE) -> set[str]:
        """:returns: The set of book keys that have been downloaded."""
        with self._lock:
            return {h.get("key") for h in self._pstate_ro(profile_id)["history"]}

    def recent_downloads(self, limit: int = 12, profile_id: str = _SENTINEL_PROFILE) -> list[dict]:
        """:returns: The *limit* most recent download records."""
        with self._lock:
            return list(self._pstate_ro(profile_id)["history"][:limit])

    # -- Reading positions (SS-03) -------------------------------------------
    def set_position(self, record: dict, chapter: int, block: int, percent: int,
                     profile_id: str = _SENTINEL_PROFILE) -> None:
        """Record a book's current reading position.

        Called on every part view from the reader routes, so this is the
        hot path for the reading experience — the same atomic-write and
        length-capping discipline as :meth:`add_favorite` applies.

        :param record: A book view-record with a ``u`` URL.
        :param chapter: 0-based spine chapter index.
        :param block: 0-based index of the position's first block within
            *chapter*.
        :param percent: Overall progress, 0-100 (see :func:`app.reader.percent_of`).
        :param profile_id: The profile whose position to record.
        """
        key = book_key(record.get("u", ""))
        with self._lock:
            reading = self._pstate(profile_id)["reading"]
            rec = sanitize_record(record)
            rec["key"] = key
            rec["chapter"] = max(0, int(chapter))
            rec["block"] = max(0, int(block))
            rec["percent"] = max(0, min(100, int(percent)))
            rec["updated"] = time.time()
            reading[key] = rec
            self._trim_mapping(reading, _MAX_READING, "updated")
            self._save()

    def get_position(self, book_key: str, profile_id: str = _SENTINEL_PROFILE) -> dict | None:
        """:returns: The reading-position record for *book_key*, or ``None``."""
        with self._lock:
            entry = self._pstate_ro(profile_id)["reading"].get(book_key)
            return dict(entry) if entry is not None else None

    def reading_list(self, limit: int = 4, profile_id: str = _SENTINEL_PROFILE) -> list[dict]:
        """:returns: The *limit* most-recently-updated reading positions."""
        with self._lock:
            ordered = sorted(
                self._pstate_ro(profile_id)["reading"].values(),
                key=lambda r: r.get("updated", 0), reverse=True,
            )
            return ordered[:limit]

    # -- Bookmarks -----------------------------------------------------------
    def add_bookmark(self, book_key: str, chapter: int, block: int, label: str,
                     profile_id: str = _SENTINEL_PROFILE) -> None:
        """Save a bookmark at (*chapter*, *block*) for *book_key*.

        Re-bookmarking the same spot refreshes its label and timestamp rather
        than duplicating it. Per-book and total counts are bounded; the
        oldest bookmark in a full book is dropped first.

        :param book_key: The book's cache key (:func:`book_key`).
        :param chapter: 0-based spine chapter index.
        :param block: 0-based block index within *chapter*.
        :param label: A short human label (e.g. the chapter title).
        :param profile_id: The profile whose bookmarks to add to.
        """
        with self._lock:
            bookmarks = self._pstate(profile_id)["bookmarks"]
            marks = bookmarks.setdefault(book_key, [])
            entry = _sanitize_bookmark(
                {"chapter": chapter, "block": block, "label": label, "when": time.time()}
            )
            if entry is None:
                if not marks:
                    bookmarks.pop(book_key, None)
                return
            marks[:] = [m for m in marks
                        if not (m["chapter"] == entry["chapter"] and m["block"] == entry["block"])]
            marks.append(entry)
            if len(marks) > _MAX_BOOKMARKS_PER_BOOK:
                marks.sort(key=lambda m: m.get("when", 0))
                del marks[: len(marks) - _MAX_BOOKMARKS_PER_BOOK]
            if len(bookmarks) > _MAX_BOOKMARK_BOOKS:
                # Drop the book whose most-recent bookmark is oldest.
                oldest = min(
                    bookmarks.items(),
                    key=lambda kv: max((m.get("when", 0) for m in kv[1]), default=0),
                )[0]
                if oldest != book_key:
                    bookmarks.pop(oldest, None)
            self._save()

    def bookmarks(self, book_key: str, profile_id: str = _SENTINEL_PROFILE) -> list[dict]:
        """:returns: *book_key*'s bookmarks in reading order (chapter, block)."""
        with self._lock:
            marks = self._pstate_ro(profile_id)["bookmarks"].get(book_key, [])
            return sorted((dict(m) for m in marks),
                          key=lambda m: (m["chapter"], m["block"]))

    def remove_bookmark(self, book_key: str, chapter: int, block: int,
                        profile_id: str = _SENTINEL_PROFILE) -> None:
        """Remove the bookmark at (*chapter*, *block*) for *book_key* (no-op if
        absent). Drops the book's entry entirely once its last mark is gone."""
        with self._lock:
            bookmarks = self._pstate_ro(profile_id)["bookmarks"]
            marks = bookmarks.get(book_key)
            if not marks:
                return
            kept = [m for m in marks if not (m["chapter"] == chapter and m["block"] == block)]
            if len(kept) == len(marks):
                return
            if kept:
                bookmarks[book_key] = kept
            else:
                bookmarks.pop(book_key, None)
            self._save()

    # -- Accounts + profiles (opt-in) ---------------------------------------
    def has_accounts(self) -> bool:
        """:returns: ``True`` when at least one account exists."""
        with self._lock:
            return bool(self._data["accounts"])

    def account_count(self) -> int:
        """:returns: The number of accounts."""
        with self._lock:
            return len(self._data["accounts"])

    def _find_by_username(self, username: str) -> tuple[str | None, dict | None]:
        """Return ``(account_id, record)`` for *username* (case-insensitive).

        Caller must hold the lock. Returns ``(None, None)`` when no account
        matches.

        :param username: The username to look up.
        :rtype: tuple[str | None, dict | None]
        """
        want = (username or "").strip().casefold()
        if not want:
            return None, None
        for acct_id, acct in self._data["accounts"].items():
            if acct["username"].casefold() == want:
                return acct_id, acct
        return None, None

    def get_account(self, account_id: str) -> dict | None:
        """:returns: The public account view for *account_id*, or ``None``.

        The returned view never contains the password hash/salt.
        """
        with self._lock:
            acct = self._data["accounts"].get(account_id)
            return _public_account(account_id, acct) if acct is not None else None

    def get_account_by_username(self, username: str) -> dict | None:
        """:returns: The public account view for *username*, or ``None``."""
        with self._lock:
            acct_id, acct = self._find_by_username(username)
            return _public_account(acct_id, acct) if acct is not None and acct_id else None

    def create_account(self, username: str, password: str) -> str:
        """Create an account with a hashed password; the first one is the admin.

        :param username: The desired username (must be non-empty and unique,
            case-insensitively).
        :param password: The plaintext password (hashed here, never stored).
        :returns: The new account's id.
        :rtype: str
        :raises ValueError: If *username* is empty or already taken.
        """
        clean = _clean_text(username, _MAX_USERNAME).strip()
        if not clean:
            raise ValueError("Username must not be empty.")
        with self._lock:
            if len(self._data["accounts"]) >= _MAX_ACCOUNTS:
                raise ValueError("Too many accounts.")
            existing, _ = self._find_by_username(clean)
            if existing is not None:
                raise ValueError("That username is already taken.")
            salt_hex, iterations, hash_hex = _accounts.hash_password(password)
            account_id = _new_id()
            while account_id in self._data["accounts"]:
                account_id = _new_id()
            self._data["accounts"][account_id] = {
                "username": clean, "pw_hash": hash_hex, "pw_salt": salt_hex,
                "iterations": iterations, "kdf": _KDF_NAME,
                "is_admin": len(self._data["accounts"]) == 0,
                "token_version": 1, "created": time.time(), "profiles": {},
            }
            self._save()
            return account_id

    def verify_login(self, username: str, password: str) -> str | None:
        """Verify a login, resisting user enumeration by uniform work + result.

        For an unknown username the same PBKDF2 work is burned via
        :func:`app.accounts.dummy_verify`, so timing does not reveal whether the
        username exists; the caller shows one uniform error either way.

        :param username: The submitted username.
        :param password: The submitted plaintext password.
        :returns: The account id on success, else ``None``.
        :rtype: str | None
        """
        with self._lock:
            acct_id, acct = self._find_by_username(username)
            if acct is None or acct_id is None:
                _accounts.dummy_verify(password)
                return None
            if _accounts.verify_password(
                password, acct["pw_salt"], acct["iterations"], acct["pw_hash"]
            ):
                return acct_id
            return None

    def set_password(self, account_id: str, new_password: str) -> bool:
        """Change an account's password and bump its token version.

        Bumping ``token_version`` invalidates every outstanding session cookie
        for the account (they no longer match), so a password change signs out
        all existing sessions.

        :param account_id: The account to update.
        :param new_password: The new plaintext password.
        :returns: ``True`` when the account existed and was updated.
        :rtype: bool
        """
        with self._lock:
            acct = self._data["accounts"].get(account_id)
            if acct is None:
                return False
            salt_hex, iterations, hash_hex = _accounts.hash_password(new_password)
            acct["pw_hash"] = hash_hex
            acct["pw_salt"] = salt_hex
            acct["iterations"] = iterations
            acct["kdf"] = _KDF_NAME
            acct["token_version"] = int(acct["token_version"]) + 1
            self._save()
            return True

    def bump_token_version(self, account_id: str) -> bool:
        """Invalidate all of an account's outstanding sessions.

        :param account_id: The account whose sessions to revoke.
        :returns: ``True`` when the account existed and was bumped.
        :rtype: bool
        """
        with self._lock:
            acct = self._data["accounts"].get(account_id)
            if acct is None:
                return False
            acct["token_version"] = int(acct["token_version"]) + 1
            self._save()
            return True

    def profile_belongs(self, account_id: str, profile_id: str) -> bool:
        """:returns: ``True`` only when *profile_id* is one of *account_id*'s own
        profiles.

        This is the cross-account isolation check: a profile switch is only ever
        allowed among the session account's own profiles, so one account can
        never select or reach another account's profile or its reading state.
        """
        with self._lock:
            acct = self._data["accounts"].get(account_id)
            return acct is not None and profile_id in acct["profiles"]

    def add_profile(self, account_id: str, name: str, color: str | None = None) -> str:
        """Add a profile to *account_id* and return its new id.

        :param account_id: The owning account.
        :param name: The profile's display name.
        :param color: An accent from :data:`_PROFILE_COLORS`; the first colour
            is used when *color* is unknown or ``None``.
        :returns: The new profile's id.
        :rtype: str
        :raises KeyError: If *account_id* does not exist.
        :raises ValueError: If the account already has the maximum profiles.
        """
        with self._lock:
            acct = self._data["accounts"].get(account_id)
            if acct is None:
                raise KeyError(account_id)
            if len(acct["profiles"]) >= _MAX_PROFILES:
                raise ValueError("Too many profiles for this account.")
            profile_id = _new_id()
            while profile_id in acct["profiles"] or profile_id in self._data[_PROFILE_STATE_KEY]:
                profile_id = _new_id()
            acct["profiles"][profile_id] = {
                "name": _clean_text(name, _MAX_PROFILE_NAME) or "Reader",
                "color": color if color in _PROFILE_COLORS else _PROFILE_COLORS[0],
                "created": time.time(),
            }
            self._save()
            return profile_id

    def rename_profile(self, account_id: str, profile_id: str, name: str) -> bool:
        """Rename one of *account_id*'s own profiles.

        :returns: ``True`` when the profile existed under this account.
        :rtype: bool
        """
        with self._lock:
            acct = self._data["accounts"].get(account_id)
            if acct is None or profile_id not in acct["profiles"]:
                return False
            acct["profiles"][profile_id]["name"] = _clean_text(name, _MAX_PROFILE_NAME) or "Reader"
            self._save()
            return True

    def delete_profile(self, account_id: str, profile_id: str) -> bool:
        """Delete one of *account_id*'s profiles and its reading state.

        Refuses to delete an account's last profile (an account must keep at
        least one selectable profile).

        :returns: ``True`` when the profile was deleted.
        :rtype: bool
        :raises ValueError: If *profile_id* is the account's only profile.
        """
        with self._lock:
            acct = self._data["accounts"].get(account_id)
            if acct is None or profile_id not in acct["profiles"]:
                return False
            if len(acct["profiles"]) <= 1:
                raise ValueError("An account must keep at least one profile.")
            acct["profiles"].pop(profile_id, None)
            self._data[_PROFILE_STATE_KEY].pop(profile_id, None)
            self._save()
            return True

    def migrate_global_state_into(self, profile_id: str) -> None:
        """Fold any pre-existing sentinel (global) reading state into *profile_id*.

        Called once, when the admin's first profile is created on setup, so the
        operator's own history/positions/bookmarks/Reading List — accumulated
        before accounts were enabled — carry over into their first profile
        rather than being stranded. Does nothing when there is no sentinel state
        or when the target profile already has state (never clobbers).

        :param profile_id: The profile to adopt the global state.
        """
        if profile_id == _SENTINEL_PROFILE:
            return
        with self._lock:
            sentinel = self._data
            has_global = (sentinel["favorites"] or sentinel["history"]
                          or sentinel["reading"] or sentinel["bookmarks"])
            if not has_global:
                return
            existing = self._data[_PROFILE_STATE_KEY].get(profile_id)
            if existing and (existing["favorites"] or existing["history"]
                             or existing["reading"] or existing["bookmarks"]):
                return
            self._data[_PROFILE_STATE_KEY][profile_id] = {
                "favorites": sentinel["favorites"], "history": sentinel["history"],
                "reading": sentinel["reading"], "bookmarks": sentinel["bookmarks"],
            }
            self._data["favorites"] = {}
            self._data["history"] = []
            self._data["reading"] = {}
            self._data["bookmarks"] = {}
            self._save()
