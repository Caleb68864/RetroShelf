"""Guards against configuration that lies — a knob that moves nothing.

Four settings in this repository were documented, parsed, and read by no code:
``APP_PORT``, ``CACHE_BOOKS``, ``KAVITA_BASE_URL`` and ``TZ`` all reached a
:class:`~app.config.Config` field that nothing ever looked at. One of them could
refuse to start the app. Deleting those four fields fixes four instances; these
two guards are what fails when a fifth appears.

**Guard 1** — every field on ``Config`` has a reader somewhere outside
``app/config.py``, or is listed in :data:`FIELDS_READ_ONLY_INSIDE_CONFIG` with a
reason.

**Guard 2** — every environment variable advertised in ``.env.example`` is one
``load_config`` actually reads, or is listed in :data:`ENV_NOT_READ_BY_THE_APP`
with a reason.

Known blind spots, stated rather than assumed — a guard's scope is a claim:

* Guard 1 reads source text, so it is coupled to the *shape* of the source. It
  recognises attribute access whose base is a config-shaped name (``cfg``,
  ``config``, ``CFG``, ``self._cfg``, ``app.state.config``, …). A field read
  through some other route — ``getattr(cfg, name)``, a dict round-trip,
  ``dataclasses.asdict`` — reads to this guard as unread, and would have to be
  exempted with that as its reason.
* Guard 1 proves a field is *mentioned*, not that the mention is reachable. A
  reader inside dead code still counts.
* Guard 2 covers ``.env.example`` only. ``docker-compose.yml`` and
  ``portainer-stack.yml`` set container-level variables (``TZ``) and compose's
  own interpolation variables (``HOST_PORT``, ``RETROSHELF_TAG``) that
  ``load_config`` is not supposed to read, so including them would make the
  guard mostly exemptions.
"""
from __future__ import annotations

import ast
import dataclasses
import os
import re

from app.config import Config

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Names that, used as the base of an attribute access, mean "the configuration".
_CONFIG_BASES = {"cfg", "_cfg", "config", "_config", "CFG", "Config", "settings"}

#: Fields whose only readers are inside ``app/config.py`` itself, with the
#: reason each is allowed to stay. An entry here is an argument, not a waiver:
#: it has to say what reads the field and why that reader is legitimate.
FIELDS_READ_ONLY_INSIDE_CONFIG: dict[str, str] = {
    "api_key": (
        "Read by Config.mask, which redacts the primary feed's key out of every "
        "log line. A masking table is legitimately internal to the config object."
    ),
    "bridge_id_secret": (
        "Read by Config.mask for the same reason (it signs session cookies, so it "
        "must never survive into a log line) and by app/main.py's lifespan."
    ),
    "extra_origins": (
        "Read by Config.allowed_origins, the single accessor the SSRF guard calls. "
        "Reading the raw tuple anywhere else would be a second copy of that rule."
    ),
}

#: Variables ``.env.example`` advertises that ``load_config`` does not read,
#: with what does read them instead.
ENV_NOT_READ_BY_THE_APP: dict[str, str] = {
    "APP_PORT": (
        "Consumed by run.sh:64,83 and run.bat:19,34, which pass it to uvicorn as "
        "--port. The application never binds a socket, so it cannot be the thing "
        "that reads a listen port. Not set in the container, where the port is "
        "fixed at 8099 by EXPOSE, the HEALTHCHECK URL and the published mapping."
    ),
    "TZ": (
        "Consumed by the C library via tzdata, which the Dockerfile installs for "
        "it — not by RetroShelf. The app stores epoch timestamps (time.time()) "
        "and stamps UTC explicitly (app/publish.py:51), so it renders no local "
        "time of its own."
    ),
}


def _iter_python_sources() -> list[str]:
    """Every ``.py`` under ``app/`` and ``tools/`` except ``app/config.py``."""
    out = []
    for sub in ("app", "tools"):
        for dirpath, _dirs, files in os.walk(os.path.join(REPO_ROOT, sub)):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                if os.path.relpath(path, REPO_ROOT) == os.path.join("app", "config.py"):
                    continue
                out.append(path)
    return out


def _attribute_reads() -> set[str]:
    """Attribute names read off something config-shaped, across app/ and tools/.

    Also scans the Jinja templates, where a field would be reached as
    ``{{ cfg.something }}`` rather than through Python.
    """
    found: set[str] = set()

    def base_is_config(node: ast.expr) -> bool:
        if isinstance(node, ast.Name):
            return node.id in _CONFIG_BASES
        if isinstance(node, ast.Attribute):
            return node.attr in _CONFIG_BASES
        return False

    for path in _iter_python_sources():
        with open(path, encoding="utf-8") as fh:
            try:
                tree = ast.parse(fh.read(), filename=path)
            except SyntaxError:  # pragma: no cover - a tool we cannot parse
                continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and base_is_config(node.value):
                found.add(node.attr)

    tmpl_re = re.compile(r"\b(?:" + "|".join(sorted(_CONFIG_BASES)) + r")\.(\w+)")
    tmpl_dir = os.path.join(REPO_ROOT, "app", "templates")
    for name in sorted(os.listdir(tmpl_dir)):
        if not name.endswith(".html"):
            continue
        with open(os.path.join(tmpl_dir, name), encoding="utf-8") as fh:
            found.update(tmpl_re.findall(fh.read()))
    return found


def _env_vars_load_config_reads() -> set[str]:
    """Every ``e.get("NAME")`` literal in ``load_config`` and its helpers."""
    path = os.path.join(REPO_ROOT, "app", "config.py")
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    names: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            names.add(node.args[0].value)
    return names


def _env_vars_advertised() -> set[str]:
    """Every ``NAME=`` in ``.env.example``, commented-out examples included."""
    path = os.path.join(REPO_ROOT, ".env.example")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    return set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", text, re.MULTILINE))


# -- the guards ---------------------------------------------------------------

def test_every_config_field_has_a_reader():
    """A field written by load_config and read by nobody is a knob that lies."""
    fields = {f.name for f in dataclasses.fields(Config)}
    readers = _attribute_reads()
    unread = sorted(fields - readers - set(FIELDS_READ_ONLY_INSIDE_CONFIG))
    assert not unread, (
        "Config fields nothing outside app/config.py reads: " + ", ".join(unread)
        + ". Wire each one or remove it — and if it genuinely belongs to "
          "config.py alone, add it to FIELDS_READ_ONLY_INSIDE_CONFIG with the "
          "reason."
    )


def test_every_advertised_env_var_is_read():
    """`.env.example` says 'copy to .env and edit'. Editing must do something."""
    advertised = _env_vars_advertised()
    parsed = _env_vars_load_config_reads()
    dead = sorted(advertised - parsed - set(ENV_NOT_READ_BY_THE_APP))
    assert not dead, (
        ".env.example advertises variables load_config never reads: "
        + ", ".join(dead)
        + ". Wire each one or remove it — or record in ENV_NOT_READ_BY_THE_APP "
          "what does read it."
    )


def test_exemptions_are_not_stale():
    """An exemption for something that no longer exists is its own small lie."""
    fields = {f.name for f in dataclasses.fields(Config)}
    gone = sorted(set(FIELDS_READ_ONLY_INSIDE_CONFIG) - fields)
    assert not gone, f"exempted Config fields that no longer exist: {gone}"

    advertised = _env_vars_advertised()
    gone_env = sorted(set(ENV_NOT_READ_BY_THE_APP) - advertised)
    assert not gone_env, (
        f".env.example no longer advertises these exempted variables: {gone_env}")


def test_the_guard_can_see_a_field_that_is_read():
    """Control. If this fails the guard is not reading the source it claims to.

    ``show_covers`` is read at app/main.py; ``state_dir`` at app/store.py. A
    guard that reports every field as unread would pass
    :func:`test_every_config_field_has_a_reader` only by accident of the
    exemption list, so prove it can see a positive.
    """
    readers = _attribute_reads()
    assert "show_covers" in readers
    assert "state_dir" in readers
    assert "pdf_disposition" in readers


def test_the_guard_can_see_an_env_var_that_is_read():
    """Control, same reasoning, for the environment half."""
    parsed = _env_vars_load_config_reads()
    assert {"KAVITA_OPDS_URL", "LOG_LEVEL", "SHOW_COVERS"} <= parsed
