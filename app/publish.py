"""Generate OPDS Atom feeds — RetroShelf as an OPDS *publisher*.

This is what makes RetroShelf more than a parser: it both consumes OPDS (from
upstream libraries) and *produces* it. The Reading List is re-published as a
standard OPDS acquisition feed, so any OPDS reader — or another RetroShelf —
can subscribe to your curated, cross-library shelf.

Uses stdlib :mod:`xml.etree.ElementTree` for output (it escapes text/attributes
safely; we are generating, not parsing untrusted input).
"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET

ATOM = "http://www.w3.org/2005/Atom"
NAV_TYPE = "application/atom+xml;profile=opds-catalog;kind=navigation"
ACQ_TYPE = "application/atom+xml;profile=opds-catalog;kind=acquisition"
ACQ_REL = "http://opds-spec.org/acquisition"

# XML 1.0 forbids most C0 control characters even when escaped (only tab, LF and
# CR are allowed). ElementTree escapes ``&``/``<``/``>``/quotes but writes these
# controls through verbatim, so a single such byte in a book's title, author, or
# summary — all ultimately sourced from an untrusted upstream OPDS feed — would
# serialize into a document that is NOT well-formed. Since the Reading List is
# re-published as a feed others subscribe to, one poisoned title would make the
# WHOLE feed unparseable for every consumer, not just corrupt one entry. Mirrors
# ``app.reader._XML_BAD_CHARS`` so both the parse and publish seams agree on
# what a legal character is.
_XML_BAD_CHARS = re.compile("[^\t\n\r\x20-\U0010ffff]")


def _xml_safe(text: str) -> str:
    """Strip XML-1.0-illegal control characters from untrusted *text*.

    :param text: A value bound for a feed element's text or an attribute.
    :returns: *text* with disallowed control characters removed.
    :rtype: str
    """
    # ``text or ""`` preserves ElementTree's own tolerance of a ``None`` text
    # value (an empty element) rather than raising on it.
    return _XML_BAD_CHARS.sub("", text or "")


def _now() -> str:
    """Return the current UTC time as an Atom ``<updated>`` timestamp string.

    :rtype: str
    """
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def build_feed(*, feed_id: str, title: str, self_href: str, start_href: str,
               kind: str, entries: list[dict]) -> bytes:
    """Build an OPDS Atom feed document.

    :param feed_id: Unique feed id (a URN or URL).
    :param title: Feed title.
    :param self_href: Absolute URL of this feed (``rel="self"``).
    :param start_href: Absolute URL of the catalog root (``rel="start"``).
    :param kind: ``"navigation"`` or ``"acquisition"``.
    :param entries: List of entry dicts, each with ``id``/``title`` and either
        ``acquisitions`` (``[{type, href}]``) or ``nav_href``/``nav_type``,
        plus optional ``author``/``summary``.
    :returns: UTF-8 XML bytes.
    :rtype: bytes
    """
    ET.register_namespace("", ATOM)
    feed = ET.Element(f"{{{ATOM}}}feed")
    ET.SubElement(feed, f"{{{ATOM}}}id").text = _xml_safe(feed_id)
    ET.SubElement(feed, f"{{{ATOM}}}title").text = _xml_safe(title)
    ET.SubElement(feed, f"{{{ATOM}}}updated").text = _now()
    feed_type = NAV_TYPE if kind == "navigation" else ACQ_TYPE
    _link(feed, "self", self_href, feed_type)
    _link(feed, "start", start_href, NAV_TYPE)

    for e in entries:
        en = ET.SubElement(feed, f"{{{ATOM}}}entry")
        ET.SubElement(en, f"{{{ATOM}}}id").text = _xml_safe(e["id"])
        ET.SubElement(en, f"{{{ATOM}}}title").text = _xml_safe(e.get("title") or "Untitled")
        ET.SubElement(en, f"{{{ATOM}}}updated").text = _now()
        if e.get("author"):
            author = ET.SubElement(en, f"{{{ATOM}}}author")
            ET.SubElement(author, f"{{{ATOM}}}name").text = _xml_safe(e["author"])
        if e.get("summary"):
            sm = ET.SubElement(en, f"{{{ATOM}}}content")
            sm.set("type", "text")
            sm.text = _xml_safe(e["summary"])
        if e.get("nav_href"):
            _link(en, "subsection", e["nav_href"], e.get("nav_type", ACQ_TYPE))
        for acq in e.get("acquisitions", []):
            _link(en, ACQ_REL, acq["href"], acq["type"])
        if e.get("cover_href"):
            _link(en, "http://opds-spec.org/image", e["cover_href"], "image/jpeg")

    return ET.tostring(feed, encoding="utf-8", xml_declaration=True)


def _link(parent: ET.Element, rel: str, href: str, type_: str) -> None:
    """Append an ``atom:link`` element to *parent*.

    :param parent: The ``feed`` or ``entry`` element to attach the link to.
    :type parent: xml.etree.ElementTree.Element
    :param rel: Link relation (e.g. ``"self"`` or an OPDS acquisition rel).
    :param href: Absolute URL of the link target.
    :param type_: Media type of the linked resource.
    """
    el = ET.SubElement(parent, f"{{{ATOM}}}link")
    el.set("rel", _xml_safe(rel))
    el.set("href", _xml_safe(href))
    el.set("type", _xml_safe(type_))
