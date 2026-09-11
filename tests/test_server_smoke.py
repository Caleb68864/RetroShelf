"""Smoke tests that boot the *real* server, not the ASGI app.

Every other test in this suite drives ``app.main:create_app`` through
``fastapi.testclient.TestClient``. That exercises the application and none of
uvicorn: no uvicorn logging config, no ``uvicorn.access`` logger, no
``uvicorn.logging.AccessFormatter``. A whole class of defect lives in that gap —
anything that only goes wrong once the app is running under the server it ships
with.

One such defect shipped: ``_SecretMaskingFilter`` cleared ``record.args`` on
every log record, ``AccessFormatter`` unpacks five values out of exactly that
tuple, and so **every HTTP request wrote a ``ValueError`` traceback to stderr
instead of an access-log line** — in the container too, where the 30-second
healthcheck produced one on its own. 537 green tests never saw it.

These tests therefore start ``python -m uvicorn app.main:app`` as a subprocess,
make real HTTP requests to it, and assert on what the server actually wrote to
its own stderr. They are slower than the rest of the suite (a few seconds) and
that is the price of covering the gap.

Each test asserts that its request **reached the server** before asserting
anything about the log, so a probe whose setup silently failed fails loudly
rather than reporting on a subject it never touched.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: A value long enough (>= 8 chars) for ``Config.mask`` to redact by value.
SECRET = "topsecretkey1234"

#: uvicorn's own access-log shape, as ``AccessFormatter`` renders it behind the
#: default ``levelprefix``::
#:
#:     INFO:     127.0.0.1:1234 - "GET /x HTTP/1.1" 200 OK
#:
#: The status *phrase* matters as much as the code: it is produced by
#: ``AccessFormatter.get_status_code``, so a line carrying it is proof the real
#: access formatter ran to completion rather than being replaced or bypassed.
ACCESS_LINE_RE = re.compile(
    r'(?P<addr>[\d.]+:\d+) - "(?P<method>[A-Z]+) (?P<path>\S+) HTTP/1\.1"'
    r' (?P<status>\d{3}) (?P<phrase>[A-Za-z ]+)$',
    re.MULTILINE,
)


def _free_port() -> int:
    """Return a port that was free a moment ago."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Server:
    """A real uvicorn process serving ``app.main:app``, with its output captured."""

    def __init__(self, env_extra: dict[str, str] | None = None) -> None:
        self.port = _free_port()
        self.output = ""
        env = {
            # A minimal, deterministic environment: no network is touched at
            # startup, and nothing outside it leaks in from the dev shell.
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "LANG": os.environ.get("LANG", ""),
            "PYTHONPATH": REPO_ROOT,
            "PYTHONUNBUFFERED": "1",
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),  # Windows needs this
            "KAVITA_OPDS_URL": "http://kavita.invalid:5000/api/opds/" + SECRET,
            "BRIDGE_ID_SECRET": "smoke-test-secret",
            "STATE_DIR": "",  # set per-test to a tmp_path
            "CACHE_DIR": "",
        }
        env.update(env_extra or {})
        self._env = {k: v for k, v in env.items() if v != ""}
        self._proc: subprocess.Popen[str] | None = None

    def __enter__(self) -> "_Server":
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=REPO_ROOT,
            env=self._env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise AssertionError(
                    "server exited before it listened:\n" + self._drain())
            try:
                self.get("/health")
                return self
            except (urllib.error.URLError, OSError):
                time.sleep(0.2)
        raise AssertionError("server never listened:\n" + self._drain())

    def __exit__(self, *exc: object) -> None:
        """Stop the server and fold its remaining output into :attr:`output`.

        Idempotent: a test may call it to read the log mid-test, and the
        fixture calls it again on teardown.
        """
        if self._proc is None or self._proc.poll() is not None:
            return  # already stopped, and its output already drained
        self._proc.terminate()
        try:
            self._proc.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            self._proc.kill()
            self._proc.wait(timeout=15)
        self.output += self._drain()

    def _drain(self) -> str:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            return self._proc.stdout.read() or ""
        except ValueError:  # pragma: no cover - already closed
            return ""

    def get(self, path: str) -> int:
        """Issue a real HTTP GET and return its status code."""
        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                return int(resp.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)


@pytest.fixture
def server(tmp_path):
    """A booted server writing its state under *tmp_path*."""
    with _Server({"STATE_DIR": str(tmp_path / "state"),
                  "CACHE_DIR": str(tmp_path / "cache")}) as srv:
        yield srv


def test_request_logs_an_access_line_and_no_traceback(server):
    """[R1] One request must produce one access line — not a logging error.

    This is the test the suite did not have. Before the masking rewrite it fails
    on the first assertion: the server emits ``--- Logging error ---`` and a
    ``ValueError: not enough values to unpack (expected 5, got 0)`` for every
    request, including its own healthcheck.
    """
    assert server.get("/health") == 200, "probe never reached the server"
    server.__exit__()
    out = server.output

    # The probe reached its subject: the server logged *something* for a request.
    assert "/health" in out, f"no access log for the request at all:\n{out}"

    assert "--- Logging error ---" not in out, f"logging failed:\n{out}"
    assert "Traceback (most recent call last)" not in out, f"traceback in log:\n{out}"
    assert "not enough values to unpack" not in out, f"AccessFormatter broke:\n{out}"

    matches = [m for m in ACCESS_LINE_RE.finditer(out)
               if m.group("path").startswith("/health")]
    assert matches, f"no well-formed access line for /health:\n{out}"
    assert all(m.group("status") == "200" for m in matches), [m.group(0) for m in matches]
    assert all(m.group("phrase") == "OK" for m in matches), [m.group(0) for m in matches]


def test_access_line_is_masked_and_still_well_formed(server):
    """[R1/H-7] Masking must redact the secret *without* destroying the line.

    The two halves have to hold together: an access line with the key stripped
    out of it is the feature, and an access line that is a stack trace is not a
    masked access line.
    """
    assert server.get(f"/health?apiKey={SECRET}&page=3") == 200, \
        "probe never reached the server"
    server.__exit__()
    out = server.output

    assert SECRET not in out, f"secret survived into the log:\n{out}"
    assert "--- Logging error ---" not in out, f"logging failed:\n{out}"

    matches = [m for m in ACCESS_LINE_RE.finditer(out)
               if m.group("path").startswith("/health?")]
    assert matches, f"no well-formed access line for the masked request:\n{out}"
    match = matches[-1]
    line = match.group(0)
    assert "apiKey=***" in line, line
    # The rest of the line survives masking: method, path, version, status.
    assert match.group("method") == "GET", line
    assert match.group("status") == "200", line
    assert "page=3" in match.group("path"), line
