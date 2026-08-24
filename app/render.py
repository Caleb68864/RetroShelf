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


templates.env.globals["site_token"] = site_token
