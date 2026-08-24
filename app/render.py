"""Jinja2 template configuration for the server-rendered, no-JS UI.

Exposes a single :data:`templates` instance pre-configured with the
project's ``templates/`` directory, and the :data:`STATIC_DIR` path
constant used by the router to mount static assets.

Module-level constants
-----------------------
:data:`TEMPLATES_DIR`
    Absolute :class:`pathlib.Path` to the ``app/templates/`` directory.
:data:`STATIC_DIR`
    Absolute :class:`pathlib.Path` to the ``app/static/`` directory.
:data:`templates`
    Configured :class:`fastapi.templating.Jinja2Templates` instance ready
    to be used in route handlers via ``templates.TemplateResponse(...)``.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from . import accounts

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def site_token(request: object) -> str:
    """Return the site token that authorises state-changing links, or ``""``.

    Registered as a Jinja global so templates can stamp ``t=…`` onto the
    ``/star``, ``/unstar`` and ``/prefs`` links without every route having to
    thread the value through its context. Missing state degrades to an empty
    string rather than an ``UndefinedError``, which keeps templates renderable
    in isolation (tests, and the unconfigured-app fallback).

    :param request: The current request, or anything without app state.
    :type request: object
    :rtype: str
    """
    ids = getattr(getattr(getattr(request, "app", None), "state", None), "ids", None)
    return getattr(ids, "site_token", "") or ""


def account_chrome(request: object) -> dict:
    """Return the signed-in chrome for the shared ``base.html`` header.

    Registered as a Jinja global so every page can render the
    "Signed in as … · Switch · Sign out" line without each route threading the
    session through its context — mirroring :func:`site_token`. Any missing or
    malformed state degrades to ``{"enabled": False}`` (or ``signed_in``
    ``False``) rather than raising, so templates stay renderable in isolation.

    :param request: The current request, or anything without app state.
    :returns: A dict the template reads: ``enabled`` (accounts turned on),
        ``signed_in``, and — when signed in — ``profile_name``, ``username``,
        ``has_profile``, and the ``csrf`` token for the sign-out POST form.
    :rtype: dict
    """
    state = getattr(getattr(request, "app", None), "state", None)
    cfg = getattr(state, "config", None)
    if state is None or cfg is None or not getattr(cfg, "accounts_enabled", False):
        return {"enabled": False}
    secret = getattr(state, "session_secret", None)
    store = getattr(state, "store", None)
    cookie = ""
    getter = getattr(getattr(request, "cookies", None), "get", None)
    if callable(getter):
        cookie = getter(accounts.SESSION_COOKIE) or ""
    if not secret or store is None:
        return {"enabled": True, "signed_in": False}
    data = accounts.decode_session(secret, cookie)
    if data is None:
        return {"enabled": True, "signed_in": False}
    acct = store.get_account(data["account_id"])
    if acct is None or acct["token_version"] != data["token_version"]:
        return {"enabled": True, "signed_in": False}
    profile = acct["profiles"].get(data["profile_id"]) if data["profile_id"] else None
    return {
        "enabled": True,
        "signed_in": True,
        "username": acct["username"],
        "profile_name": profile["name"] if profile else None,
        "has_profile": profile is not None,
        "csrf": accounts.csrf_token(secret, cookie),
    }


templates.env.globals["site_token"] = site_token
templates.env.globals["account_chrome"] = account_chrome
