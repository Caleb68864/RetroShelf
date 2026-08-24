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
- Pagination: feed-level ``rel`` ``next`` / ``previous``.
  Search: ``rel`` ``search``.

:raises OpdsParseError: Propagated by :func:`parse` when the XML document is
    malformed or does not represent a valid Atom ``<feed>`` root element.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as DET

# Bounds on what a single feed document may cost this process. ``defusedxml``
# already blocks entity expansion and external entities, but it has nothing to
# say about a *syntactically valid* feed with a million entries or a title the
# size of a novel. Every one of these limits truncates rather than raises: a
# greedy feed should render its first page, not take the shelf offline. [SS-14]
MAX_DOC_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 500
MAX_LINKS_PER_ENTRY = 64
MAX_TITLE_LEN = 1000
MAX_SUMMARY_LEN = 8000

# ``<?xml version="1.0" encoding="iso-8859-1"?>`` on a ``str`` input is a
# contradiction ElementTree refuses outright. Callers hand us text that has
# already been decoded, so the declared encoding is stale by definition.
_XML_DECL_ENCODING_RE = re.compile(
    r"^(\s*<\?xml[^>]*?)\s+encoding\s*=\s*[\"'][^\"']*[\"']", re.IGNORECASE
)

ATOM = "{http://www.w3.org/2005/Atom}"
ACQUISITION_REL_PREFIX = "http://opds-spec.org/acquisition"
IMAGE_REL = "http://opds-spec.org/image"
THUMB_REL = "http://opds-spec.org/image/thumbnail"
# Some catalogs (e.g. ManyBooks) use the shorter cover/thumbnail rels.
COVER_RELS = ("http://opds-spec.org/image", "http://opds-spec.org/cover")
THUMB_RELS = ("http://opds-spec.org/image/thumbnail", "http://opds-spec.org/thumbnail")


class OpdsParseError(Exception):
    """Raised when an OPDS/Atom document cannot be parsed.

    Wraps all low-level :mod:`defusedxml` / :mod:`xml.etree` parse exceptions
    so callers only need to catch a single, well-typed error and can render a
    friendly error page without leaking internal parser details.
    """


@dataclass(frozen=True)
class Acquisition:
    """An individual acquisition link extracted from an OPDS ``atom:entry``.

    Instances are immutable (``frozen=True``) so they can be stored safely in
    sets or used as dict keys.

    :ivar media_type: MIME type of the downloadable resource, e.g.
        ``"application/epub+zip"`` or ``"application/pdf"``.
    :vartype media_type: str

    :ivar href: Absolute or relative URL of the downloadable file.
    :vartype href: str

    :ivar rel: Full ``rel`` attribute value from the ``atom:link`` element,
        always starting with ``http://opds-spec.org/acquisition``.
    :vartype rel: str

    :ivar length: Declared file size in bytes from the link's ``length``
        attribute, or ``None`` when absent or non-numeric.
    :vartype length: int | None
    """

    media_type: str
    href: str
    rel: str
    length: int | None = None

    @property
    def is_epub(self) -> bool:
        """Return ``True`` when :attr:`media_type` identifies an EPUB file.

        Detection is case-insensitive substring match on ``"epub"``, which
        covers ``application/epub+zip`` and any vendor-prefixed variants.

        :returns: ``True`` if the media type contains ``"epub"``, else ``False``.
        :rtype: bool
        """
        return "epub" in (self.media_type or "").lower()

    @property
    def is_pdf(self) -> bool:
        """Return ``True`` when :attr:`media_type` identifies a PDF file.

        Detection is case-insensitive substring match on ``"pdf"``, which
        covers ``application/pdf`` and any vendor-prefixed variants.

        :returns: ``True`` if the media type contains ``"pdf"``, else ``False``.
        :rtype: bool
        """
        return "pdf" in (self.media_type or "").lower()


@dataclass
class Entry:
    """A single OPDS catalog entry, representing either a book or a sub-catalog.

    An entry is considered a *navigation* entry (sub-catalog) when it carries no
    acquisition links and has a navigable ``nav_href``.  It is an *acquisition*
    entry (a book) when ``acquisitions`` is non-empty.

    :ivar title: Human-readable title of the entry.
    :vartype title: str

    :ivar author: Author name extracted from the nested ``atom:author/atom:name``
        element; empty string when absent.
    :vartype author: str

    :ivar summary: Short description from ``atom:summary`` or ``atom:content``;
        empty string when absent.
    :vartype summary: str

    :ivar updated: ISO 8601 timestamp string from ``atom:updated``; empty string
        when absent.
    :vartype updated: str

    :ivar acquisitions: All acquisition links found in the entry, in document
        order.  Empty for navigation entries.
    :vartype acquisitions: list[Acquisition]

    :ivar cover_url: URL of the full-size cover image, or ``None``.
    :vartype cover_url: str | None

    :ivar thumbnail_url: URL of the thumbnail cover image, or ``None``.
    :vartype thumbnail_url: str | None

    :ivar nav_href: URL of the sub-catalog this entry points to, or ``None``.
        Only set for navigation entries (no acquisition links present).
    :vartype nav_href: str | None
    """

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
        """Return ``True`` when this entry is a navigation (sub-catalog) entry.

        An entry is navigable when it has no acquisition links **and** a
        non-``None`` :attr:`nav_href`.

        :returns: ``True`` if the entry has no acquisitions and a nav href.
        :rtype: bool
        """
        return not self.acquisitions and self.nav_href is not None

    @property
    def primary_acquisition(self) -> Acquisition | None:
        """Return the most relevant acquisition link for this entry.

        Selection priority: EPUB first, then PDF, then the first link in
        document order.  Returns ``None`` when :attr:`acquisitions` is empty.

        :returns: The preferred :class:`Acquisition`, or ``None`` if the entry
            has no acquisition links.
        :rtype: Acquisition | None
        """
        if not self.acquisitions:
            return None
        for a in self.acquisitions:
            if a.is_epub:
                return a
        for a in self.acquisitions:
            if a.is_pdf:
                return a
        return self.acquisitions[0]

    @property
    def supported_acquisition(self) -> Acquisition | None:
        """Return an EPUB or PDF acquisition (the only formats iBooks imports).

        EPUB is preferred over PDF. Returns ``None`` when the entry offers
        neither — e.g. a Gutenberg book exposing only Kindle/mobi, or a comic
        feed offering only CBZ — so the bridge never mislabels or proxies a
        format old iPads cannot import.

        :returns: A supported :class:`Acquisition`, or ``None``.
        :rtype: Acquisition | None
        """
        for a in self.acquisitions:
            if a.is_epub:
                return a
        for a in self.acquisitions:
            if a.is_pdf:
                return a
        return None


@dataclass
class Feed:
    """A parsed OPDS Atom feed document.

    Holds the feed-level metadata and the list of parsed :class:`Entry` objects.
    Pagination and navigation links are exposed as optional URL strings so the
    caller can build previous/next controls without re-parsing the XML.

    :ivar title: Human-readable feed title from ``atom:title``.
    :vartype title: str

    :ivar entries: All ``atom:entry`` elements parsed from the feed, in document
        order.
    :vartype entries: list[Entry]

    :ivar next_url: URL of the next page (``rel="next"`` link), or ``None``.
    :vartype next_url: str | None

    :ivar prev_url: URL of the previous page (``rel="previous"`` or
        ``rel="prev"`` link), or ``None``.
    :vartype prev_url: str | None

    :ivar search_url: OpenSearch description URL (``rel="search"``), or ``None``.
    :vartype search_url: str | None

    :ivar start_url: URL of the catalog root (``rel="start"``), or ``None``.
    :vartype start_url: str | None

    :ivar self_url: Canonical URL of this feed (``rel="self"``), or ``None``.
    :vartype self_url: str | None
    """

    title: str = ""
    entries: list[Entry] = field(default_factory=list)
    next_url: str | None = None
    prev_url: str | None = None
    search_url: str | None = None
    start_url: str | None = None
    self_url: str | None = None


def _all_text(el: Element | None) -> str:
    """Return all descendant text of *el*, whitespace-collapsed.

    Handles Atom ``type="xhtml"`` / ``type="html"`` elements where the
    element's own ``.text`` is empty and the real content lives in child
    nodes.  ManyBooks, for example, emits
    ``<title type="text/xhtml"><div ...>Real Title</div></title>`` --
    reading only ``title.text`` would yield whitespace ("Untitled").

    :param el: An XML element, or ``None``.
    :type el: xml.etree.ElementTree.Element or None
    :returns: The collapsed text content, or ``""`` when *el* is ``None``.
    :rtype: str
    """
    if el is None:
        return ""
    return " ".join("".join(el.itertext()).split())


def _text(el: Element, tag: str) -> str:
    """Return the full collapsed text of the first ``{ATOM}{tag}`` child.

    Uses :func:`_all_text` so typed-XHTML/HTML titles and summaries are read
    correctly, not just the element's direct ``.text``.

    :param el: Parent XML element to search within.
    :type el: xml.etree.ElementTree.Element
    :param tag: Local name of the Atom child element (without namespace prefix),
        e.g. ``"title"``, ``"summary"``, ``"updated"``.
    :type tag: str
    :returns: Collapsed text content of the child element, or ``""`` if absent.
    :rtype: str
    """
    return _all_text(el.find(f"{ATOM}{tag}"))


def _author(entry: Element) -> str:
    """Extract the author name from an Atom ``entry`` element.

    Navigates ``atom:author`` -> ``atom:name`` and returns the collapsed text.
    Returns an empty string when either element is absent.

    :param entry: An ``atom:entry`` XML element.
    :type entry: xml.etree.ElementTree.Element
    :returns: Author display name, or ``""`` if not present.
    :rtype: str
    """
    author = entry.find(f"{ATOM}author")
    if author is not None:
        return _all_text(author.find(f"{ATOM}name"))
    return ""


def parse(xml_text: str | bytes) -> Feed:
    """Parse an OPDS Atom feed document into a :class:`Feed`.

    Accepts the raw XML as either a ``str`` or ``bytes`` object.  String input
    is encoded to UTF-8 before parsing so that :mod:`defusedxml` always
    receives bytes.  All low-level parser exceptions are caught and re-raised
    as :class:`OpdsParseError` so callers never see a raw
    :class:`xml.etree.ElementTree.ParseError`.

    :param xml_text: Raw OPDS/Atom XML document content.
    :type xml_text: str | bytes
    :returns: A fully populated :class:`Feed` containing zero or more
        :class:`Entry` objects.
    :rtype: Feed
    :raises OpdsParseError: When *xml_text* is empty, cannot be parsed as XML,
        or the root element is not an Atom ``<feed>``.
    """
    if xml_text is None or (isinstance(xml_text, (str, bytes)) and not str(xml_text).strip()):
        raise OpdsParseError("Empty OPDS feed document")
    try:
        if isinstance(xml_text, str):
            # The text was decoded using the charset the *server* declared, so
            # any encoding named inside the document is now wrong. Strip it and
            # re-encode as UTF-8, which is what the bytes will actually be.
            xml_text = _XML_DECL_ENCODING_RE.sub(r"\1", xml_text, count=1).encode("utf-8")
        if len(xml_text) > MAX_DOC_BYTES:
            raise OpdsParseError(
                f"OPDS feed is too large to parse ({len(xml_text)} bytes)"
            )
        root = DET.fromstring(xml_text)
    except OpdsParseError:
        raise
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

    for entry_el in root.findall(f"{ATOM}entry")[:MAX_ENTRIES]:
        feed.entries.append(_parse_entry(entry_el))

    return feed


def _parse_entry(entry_el: Element) -> Entry:
    """Parse a single ``atom:entry`` XML element into an :class:`Entry`.

    Extracts title, author, summary/content, updated timestamp, acquisition
    links, cover/thumbnail image URLs, and the navigation href.  Acquisition
    detection uses a ``rel`` prefix match against
    :data:`ACQUISITION_REL_PREFIX`; image detection checks against
    :data:`COVER_RELS` and :data:`THUMB_RELS`; navigation links are collected
    as a fallback for any remaining ``atom:link`` elements.

    :param entry_el: An ``atom:entry`` XML element from the feed.
    :type entry_el: xml.etree.ElementTree.Element
    :returns: A populated :class:`Entry` instance.
    :rtype: Entry
    """
    entry = Entry(
        title=_text(entry_el, "title")[:MAX_TITLE_LEN],
        author=_author(entry_el)[:MAX_TITLE_LEN],
        summary=(_text(entry_el, "summary") or _text(entry_el, "content"))[:MAX_SUMMARY_LEN],
        updated=_text(entry_el, "updated")[:MAX_TITLE_LEN],
    )

    nav_candidate: str | None = None
    for link in entry_el.findall(f"{ATOM}link")[:MAX_LINKS_PER_ENTRY]:
        rel = (link.get("rel") or "").strip()
        href = (link.get("href") or "").strip()
        mtype = (link.get("type") or "").strip()
        if not href:
            continue
        if rel.startswith(ACQUISITION_REL_PREFIX):
            length = None
            raw_len = (link.get("length") or "").strip()
            # Bound the digit count before int(): a real file length is <= 19
            # digits (< 2**63), and Python refuses to convert very long digit
            # strings (>4300) at all, raising ValueError. Without this cap a
            # feed declaring a novel-length `length` attribute would crash
            # parsing with an *uncaught* ValueError (this loop runs outside
            # parse()'s try). An absurd length simply degrades to None, like
            # every other over-limit field here. [SS-14]
            if raw_len.isdigit() and len(raw_len) <= 19:
                length = int(raw_len)
            entry.acquisitions.append(Acquisition(media_type=mtype, href=href, rel=rel, length=length))
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
