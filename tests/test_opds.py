"""Tests for app.opds — Atom parsing, nav vs acquisition, pagination, covers."""
import pathlib

import pytest

from app.opds import parse, OpdsParseError, Acquisition

FIX = pathlib.Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def test_parse_navigation_feed():
    feed = parse(_load("opds_root.xml"))
    assert feed.title == "RetroShelf Library"
    assert feed.search_url and "search" in feed.search_url
    assert len(feed.entries) == 2
    nav = feed.entries[0]
    assert nav.is_navigation is True
    assert nav.nav_href == "/api/opds/KEY/libraries"
    assert nav.acquisitions == []


def test_parse_acquisition_feed_epub_and_pdf():
    feed = parse(_load("opds_acquisition.xml"))
    assert feed.title == "Recently Added"
    assert len(feed.entries) == 2

    epub_entry = feed.entries[0]
    assert epub_entry.title == "The Time Machine"
    assert epub_entry.author == "H. G. Wells"
    assert epub_entry.is_navigation is False
    acq = epub_entry.primary_acquisition
    assert isinstance(acq, Acquisition)
    assert acq.media_type == "application/epub+zip"
    assert acq.is_epub is True
    assert acq.href.endswith("the-time-machine.epub")
    assert epub_entry.cover_url == "/api/image/series-cover?seriesId=1&apiKey=KEY"

    pdf_entry = feed.entries[1]
    assert pdf_entry.primary_acquisition.media_type == "application/pdf"
    assert pdf_entry.primary_acquisition.is_pdf is True


def test_pagination_links():
    feed = parse(_load("opds_acquisition.xml"))
    assert feed.next_url.endswith("pageNumber=2")
    assert feed.prev_url.endswith("pageNumber=0")


def test_primary_acquisition_prefers_epub_over_pdf():
    from app.opds import Entry, Acquisition
    e = Entry(acquisitions=[
        Acquisition("application/pdf", "/x.pdf", "http://opds-spec.org/acquisition"),
        Acquisition("application/epub+zip", "/x.epub", "http://opds-spec.org/acquisition"),
    ])
    assert e.primary_acquisition.is_epub


def test_parse_xhtml_typed_title():
    # ManyBooks wraps titles in typed XHTML; the real text is in a nested <div>.
    xml = (
        '<?xml version="1.0"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        '<title type="text/xhtml"><div xmlns="http://www.w3.org/1999/xhtml">My Catalogue</div></title>'
        '<entry>'
        '<title type="text/xhtml"><div xmlns="http://www.w3.org/1999/xhtml">The Real Title</div></title>'
        '<author><name>A. Writer</name></author>'
        '<link type="application/atom+xml" href="/sub"/>'
        '</entry></feed>'
    )
    feed = parse(xml)
    assert feed.title == "My Catalogue"
    assert feed.entries[0].title == "The Real Title"
    assert feed.entries[0].author == "A. Writer"


def test_supported_acquisition_prefers_epub_skips_unsupported():
    from app.opds import Entry, Acquisition
    ACQ = "http://opds-spec.org/acquisition"
    # Gutenberg-style: epub + Kindle/mobi → pick EPUB.
    mixed = Entry(acquisitions=[
        Acquisition("application/x-mobipocket-ebook", "/103.kindle", ACQ),
        Acquisition("application/epub+zip", "/103.epub", ACQ),
    ])
    assert mixed.supported_acquisition.is_epub
    # PDF-only → pick PDF.
    pdf = Entry(acquisitions=[Acquisition("application/pdf", "/x.pdf", ACQ)])
    assert pdf.supported_acquisition.is_pdf
    # Only unsupported formats (mobi / cbz) → None (entry is skipped, not mislabeled).
    unsupported = Entry(acquisitions=[
        Acquisition("application/x-mobipocket-ebook", "/x.mobi", ACQ),
        Acquisition("application/x-cbz", "/x.cbz", ACQ),
    ])
    assert unsupported.supported_acquisition is None


def test_malformed_xml_raises_opdsparseerror():
    with pytest.raises(OpdsParseError):
        parse("<feed><entry></feed>")  # mismatched tags


def test_empty_raises():
    with pytest.raises(OpdsParseError):
        parse("")


def test_non_feed_root_raises():
    with pytest.raises(OpdsParseError):
        parse('<?xml version="1.0"?><notafeed/>')


def test_defused_blocks_external_entity():
    # A billion-laughs / external-entity payload must not expand; defusedxml
    # raises (caught and re-raised as OpdsParseError).
    bomb = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE feed [<!ENTITY a "AAAA"><!ENTITY b "&a;&a;&a;">]>'
        '<feed xmlns="http://www.w3.org/2005/Atom"><title>&b;</title></feed>'
    )
    with pytest.raises(OpdsParseError):
        parse(bomb)


def test_absurd_length_attribute_degrades_to_none_not_crash():
    # A syntactically valid feed whose acquisition link declares an
    # over-long `length` must not crash parsing (int() refuses >4300-digit
    # strings, and this loop runs outside parse()'s try) — the length just
    # degrades to None, like every other over-limit field.
    big = "9" * 6000
    xml = (
        '<?xml version="1.0"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>'
        '<entry><title>b</title>'
        '<link rel="http://opds-spec.org/acquisition" type="application/epub+zip"'
        f' href="http://x/y.epub" length="{big}"/>'
        '</entry></feed>'
    )
    feed = parse(xml)
    assert len(feed.entries) == 1
    acq = feed.entries[0].primary_acquisition
    assert acq is not None
    assert acq.length is None  # absurd length dropped, not crashed


def test_normal_length_attribute_still_parsed():
    xml = (
        '<?xml version="1.0"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>'
        '<entry><title>b</title>'
        '<link rel="http://opds-spec.org/acquisition" type="application/epub+zip"'
        ' href="http://x/y.epub" length="123456"/>'
        '</entry></feed>'
    )
    feed = parse(xml)
    assert feed.entries[0].primary_acquisition.length == 123456
