"""Cross-cutting typed exceptions for RetroShelf.

Every failure mode raises a typed exception carrying enough context to debug
(which url, which id, upstream status) — with secrets masked by the caller —
so nothing fails silently.  The app layer maps these to friendly error pages.

Exception hierarchy
-------------------
:class:`RetroShelfError`
    Base for all domain errors.

    :class:`KavitaError`
        Non-2xx, timeout, or connection error from the upstream Kavita API.

    :class:`SsrfError`
        A URL was refused by the SSRF guard (bad origin or hostile shape).

    :class:`BadIdError`
        An opaque bridge id was missing, malformed, tampered, or expired.
"""
from __future__ import annotations


class RetroShelfError(Exception):
    """Base class for all RetroShelf domain errors.

    Subclass this to define failure modes that the application layer can
    catch and map to user-facing error responses.
    """


class KavitaError(RetroShelfError):
    """Upstream Kavita fetch/stream failed (non-2xx, timeout, connection error).

    The *message* passed to this exception should already have secrets
    (API keys, tokens) masked by the raising code before construction.

    :param message: Human-readable description of the failure with secrets
        already redacted.
    :type message: str
    :param url: The upstream Kavita URL that triggered the error, with any
        sensitive query parameters removed.  ``None`` when not applicable.
    :type url: str or None
    :param status: HTTP status code returned by Kavita, or ``None`` for
        network-level failures (timeouts, connection errors).
    :type status: int or None

    :ivar url: Upstream URL associated with the failure (secrets masked).
    :ivar status: HTTP status code from Kavita, or ``None``.
    """

    def __init__(self, message: str, *, url: str | None = None, status: int | None = None) -> None:
        """Store the failure context and initialise the base exception.

        See the class docstring for the parameter semantics.
        """
        self.url = url
        self.status = status
        super().__init__(message)


class SsrfError(RetroShelfError):
    """A URL was refused by the SSRF guard.

    Raised by the URL-validation layer when a supplied URL is not an allowed
    origin (any configured feed, a same-site sibling of one, or an entry in
    ``EXTRA_UPSTREAM_ORIGINS``) — or when its *shape* is hostile: a non-HTTP
    scheme, embedded credentials, control characters, a protocol-relative or
    backslash form, or an oversized href.
    """


class BadIdError(RetroShelfError):
    """An opaque bridge id was missing, malformed, tampered, or expired.

    Raised when the application cannot decode or verify an id that was
    previously issued as an opaque token (e.g. a signed download id).
    The client should treat this as a 400 or 404 condition.
    """
