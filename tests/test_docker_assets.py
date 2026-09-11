"""Runs ``tests/validate_docker.py`` as part of the pytest suite.

``validate_docker.py`` is deliberately a standalone runnable script rather than
a ``test_``-prefixed module — ``docs/specs/2026-06-07-retroshelf-spec.md:72``
says so explicitly, ``README.md:372`` documents invoking it directly, and
``tools/verify.sh:22`` calls it. That decision stands.

What did not follow from it is that **nothing automatic ever ran it**. pytest
collects ``test_*.py``, so the validator was invisible to the suite, and
``.github/workflows/tests.yml`` runs ``python -m pytest -q`` and nothing else —
so the only thing that ever executed these checks was a human remembering to
type ``tools/verify.sh``. A guard nobody runs is a guard, in the sense that a
sign nobody reads is a sign.

This module is the four lines that put it on the CI path without changing what
it is.
"""
from __future__ import annotations

import importlib.util
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VALIDATOR = os.path.join(REPO_ROOT, "tests", "validate_docker.py")


def _load_validator():
    """Import the script by path — it is not an importable module name."""
    spec = importlib.util.spec_from_file_location("validate_docker", VALIDATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_docker_assets_validate():
    """The Dockerfile, .dockerignore and both compose files must still hold up."""
    validator = _load_validator()
    rc = validator.main()
    assert rc == 0, "docker asset validation failed:\n  - " + "\n  - ".join(
        validator.failures)


def test_the_validator_can_fail():
    """Control. A validator whose checks all pass vacuously guards nothing.

    Feeds ``check`` a false condition and confirms it is recorded, so a future
    refactor that turns ``check`` into a no-op is caught here rather than by the
    next broken image.
    """
    validator = _load_validator()
    before = len(validator.failures)
    validator.check(False, "control: this must be recorded")
    assert len(validator.failures) == before + 1
    assert validator.failures[-1] == "control: this must be recorded"
