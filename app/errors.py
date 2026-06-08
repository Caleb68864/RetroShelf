"""Cross-cutting typed exceptions for RetroShelf.

Every failure mode raises a typed exception carrying enough context to debug
(which url, which id, upstream status) — with secrets masked by the caller —
so nothing fails silently. The app layer maps these to friendly error pages.
"""
from __future__ import annotations


class RetroShelfError(Exception):
    """Base class for all RetroShelf domain errors."""


class KavitaError(RetroShelfError):
    """Upstream Kavita fetch/stream failed (non-2xx, timeout, connection error).

    The message should already have secrets masked by the raiser.
    """

    def __init__(self, message: str, *, url: str | None = None, status: int | None = None):
        self.url = url
        self.status = status
        super().__init__(message)


class SsrfError(RetroShelfError):
    """A URL did not resolve to the configured Kavita origin and was refused."""


class BadIdError(RetroShelfError):
    """An opaque bridge id was missing, malformed, tampered, or expired."""
