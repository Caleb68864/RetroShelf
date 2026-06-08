"""OPDS 1.x (Atom) catalog parser.

Parses a Kavita/OPDS Atom feed into typed :class:`Feed` / :class:`Entry`
objects using :mod:`defusedxml` (no external-entity / billion-laughs exposure).

Key OPDS facts encoded here (verified — see vault/Build Constraints.md):
- Acquisition links are ``atom:link`` with ``rel`` *starting with*
  ``http://opds-spec.org/acquisition`` (covers ``/acquisition/open-access`` too).
- The ``type`` attribute is the media type (``application/epub+zip``,
  ``application/pdf``); ``href`` is the file URL.
- Navigation vs acquisition is detected *structurally* (does the entry contain
  an acquisition link?) — the ``kind=`` MIME param is only an advisory hint.
- Covers: ``rel`` ``http://opds-spec.org/image`` / ``.../image/thumbnail``.
- Pagination: feed-level ``rel`` ``next`` / ``previous``. Search: ``rel`` ``search``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from defusedxml import ElementTree as DET

ATOM = "{http://www.w3.org/2005/Atom}"
ACQUISITION_REL_PREFIX = "http://opds-spec.org/acquisition"
IMAGE_REL = "http://opds-spec.org/image"
THUMB_REL = "http://opds-spec.org/image/thumbnail"
# Some catalogs (e.g. ManyBooks) use the shorter cover/thumbnail rels.
COVER_RELS = ("http://opds-spec.org/image", "http://opds-spec.org/cover")
THUMB_RELS = ("http://opds-spec.org/image/thumbnail", "http://opds-spec.org/thumbnail")


class OpdsParseError(Exception):
    """Raised when an OPDS/Atom document cannot be parsed."""


@dataclass(frozen=True)
class Acquisition:
    media_type: str
    href: str
    rel: str

    @property
    def is_epub(self) -> bool:
        return "epub" in (self.media_type or "").lower()

    @property
    def is_pdf(self) -> bool:
        return "pdf" in (self.media_type or "").lower()


@dataclass
class Entry:
    title: str = ""
    author: str = ""
    summary: str = ""
    updated: str = ""
    acquisitions: list[Acquisition] = field(default_factory=list)
    cover_url: str | None = None
    thumbnail_url: str | None = None
    nav_href: str | None = None

    @property
    def is_navigation(self) -> bool:
        return not self.acquisitions and self.nav_href is not None

    @property
    def primary_acquisition(self) -> Acquisition | None:
        """Prefer EPUB, then PDF, then the first acquisition link."""
        if not self.acquisitions:
            return None
        for a in self.acquisitions:
            if a.is_epub:
                return a
        for a in self.acquisitions:
            if a.is_pdf:
                return a
        return self.acquisitions[0]


@dataclass
class Feed:
    title: str = ""
    entries: list[Entry] = field(default_factory=list)
    next_url: str | None = None
    prev_url: str | None = None
    search_url: str | None = None
    start_url: str | None = None
    self_url: str | None = None


def _text(el, tag: str) -> str:
    child = el.find(f"{ATOM}{tag}")
    return (child.text or "").strip() if child is not None and child.text else ""


def _author(entry) -> str:
    author = entry.find(f"{ATOM}author")
    if author is not None:
        name = author.find(f"{ATOM}name")
        if name is not None and name.text:
            return name.text.strip()
    return ""


def parse(xml_text: str | bytes) -> Feed:
    """Parse an OPDS Atom feed document into a :class:`Feed`.

    Raises :class:`OpdsParseError` on malformed XML (never a raw ElementTree
    exception), so callers can render a friendly error page.
    """
    if xml_text is None or (isinstance(xml_text, (str, bytes)) and not str(xml_text).strip()):
        raise OpdsParseError("Empty OPDS feed document")
    try:
        if isinstance(xml_text, str):
            xml_text = xml_text.encode("utf-8")
        root = DET.fromstring(xml_text)
    except Exception as exc:  # defusedxml raises various ParseError/EntitiesForbidden
        raise OpdsParseError(f"Could not parse OPDS feed: {exc}") from exc

    if root.tag != f"{ATOM}feed" and root.tag != "feed":
        raise OpdsParseError(f"Root element is not an Atom <feed> (got {root.tag!r})")

    feed = Feed(title=_text(root, "title"))

    # Feed-level navigation links (pagination / search / self / start).
    for link in root.findall(f"{ATOM}link"):
        rel = (link.get("rel") or "").strip()
        href = (link.get("href") or "").strip()
        if not href:
            continue
        if rel == "next":
            feed.next_url = href
        elif rel in ("previous", "prev"):
            feed.prev_url = href
        elif rel == "search":
            feed.search_url = href
        elif rel == "start":
            feed.start_url = href
        elif rel == "self":
            feed.self_url = href

    for entry_el in root.findall(f"{ATOM}entry"):
        feed.entries.append(_parse_entry(entry_el))

    return feed


def _parse_entry(entry_el) -> Entry:
    entry = Entry(
        title=_text(entry_el, "title"),
        author=_author(entry_el),
        summary=_text(entry_el, "summary") or _text(entry_el, "content"),
        updated=_text(entry_el, "updated"),
    )

    nav_candidate: str | None = None
    for link in entry_el.findall(f"{ATOM}link"):
        rel = (link.get("rel") or "").strip()
        href = (link.get("href") or "").strip()
        mtype = (link.get("type") or "").strip()
        if not href:
            continue
        if rel.startswith(ACQUISITION_REL_PREFIX):
            entry.acquisitions.append(Acquisition(media_type=mtype, href=href, rel=rel))
        elif rel in THUMB_RELS:
            entry.thumbnail_url = href
            entry.cover_url = entry.cover_url or href
        elif rel in COVER_RELS or rel.endswith("/cover"):
            entry.cover_url = href
        elif "image" in rel and mtype.startswith("image/"):
            # Generic image link fallback.
            entry.cover_url = entry.cover_url or href
        else:
            # A navigable (sub-catalog) link — remember the first atom+xml one,
            # else the first non-acquisition link.
            if nav_candidate is None and ("atom+xml" in mtype or rel in ("subsection", "")):
                nav_candidate = href
            elif nav_candidate is None:
                nav_candidate = href

    if not entry.acquisitions:
        entry.nav_href = nav_candidate
    return entry
