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
    # EPUB is preferred over a CBZ when a book offers both.
    epub_cbz = Entry(acquisitions=[
        Acquisition("application/vnd.comicbook+zip", "/x.cbz", ACQ),
        Acquisition("application/epub+zip", "/x.epub", ACQ),
    ])
    assert epub_cbz.supported_acquisition.is_epub
    # CBZ-only → the CBZ is surfaced (read in the browser as a comic).
    cbz_only = Entry(acquisitions=[Acquisition("application/x-cbz", "/x.cbz", ACQ)])
    assert cbz_only.supported_acquisition.is_cbz
    # Only genuinely unsupported formats (mobi / CBR) → None (entry skipped).
    unsupported = Entry(acquisitions=[
        Acquisition("application/x-mobipocket-ebook", "/x.mobi", ACQ),
        Acquisition("application/vnd.comicbook-rar", "/x.cbr", ACQ),
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


# -- Gutenberg series-convention helpers -------------------------------------
from app.opds import clean_summary, series_of  # noqa: E402

GUT_SUMMARY = (
    "This edition had all images removed. Title: A princess of Mars "
    "Series Title: Barsoom series, 1 Note: Wikipedia page about this book: "
    "https://en.wikipedia.org/wiki/A_Princess_of_Mars Summary: \"A Princess of "
    "Mars\" by Edgar Rice Burroughs is a science fantasy novel first serialized "
    "in 1912. Reading Level: Reading ease score: 59.1 (10th to 12th grade)."
)


def test_series_of_extracts_name_and_position():
    assert series_of(GUT_SUMMARY) == ("Barsoom series", 1)


def test_series_of_handles_comma_in_name():
    assert series_of("x Series Title: Oz, the famous forty, 3 Note: y") == (
        "Oz, the famous forty", 3)


def test_series_of_absent_or_malformed_is_none():
    assert series_of("An ordinary description with no labels.") is None
    assert series_of("") is None
    assert series_of("Series Title: Nameless") is None          # no position
    assert series_of("Series Title: , 5 Note:") is None          # empty name


def test_series_of_bounds_the_index():
    # A silly huge "index" is not treated as a series position.
    assert series_of("Series Title: X, 99999999999 Note:") is None


def test_clean_summary_extracts_the_real_description():
    got = clean_summary(GUT_SUMMARY)
    assert got.startswith('"A Princess of Mars" by Edgar Rice Burroughs')
    assert "Reading Level" not in got
    assert "Series Title" not in got


def test_clean_summary_passthrough_without_labels():
    assert clean_summary("Just a normal blurb.") == "Just a normal blurb."
    assert clean_summary("") == ""


# -- app.publish: the OPDS *publisher* (re-publishing the Reading List) --------
from app.publish import build_feed  # noqa: E402


def test_published_feed_round_trips_through_the_parser():
    # What we publish, a subscriber (including another RetroShelf) must be able
    # to parse back. Untrusted metadata should survive as content, not markup.
    xml = build_feed(
        feed_id="urn:rs:reading-list",
        title="My Shelf",
        self_href="http://host/opds/list",
        start_href="http://host/opds",
        kind="acquisition",
        entries=[{
            "id": "urn:b:1",
            "title": "A <Book> & \"Friends\"",
            "author": "Ada & Bob",
            "summary": "Angle < and amp & in the blurb",
            "acquisitions": [{"type": "application/epub+zip", "href": "http://host/d/1.epub"}],
            "cover_href": "http://host/c/1.jpg",
        }],
    )
    feed = parse(xml)
    assert feed.title == "My Shelf"
    assert len(feed.entries) == 1
    assert feed.entries[0].title == 'A <Book> & "Friends"'
    assert feed.entries[0].author == "Ada & Bob"
    assert feed.entries[0].acquisitions[0].is_epub


def test_published_feed_scrubs_xml_illegal_control_chars():
    # A book title/author/summary carrying a C0 control char (from an untrusted
    # upstream feed) must NOT make the whole re-published feed unparseable — one
    # poisoned entry would otherwise take the entire subscriber-facing feed
    # offline. Legal whitespace (tab/newline) is preserved.
    xml = build_feed(
        feed_id="urn:rs:list",
        title="Shelf\tName",
        self_href="http://host/opds/list",
        start_href="http://host/opds",
        kind="acquisition",
        entries=[{
            "id": "urn:b:\x00bad",
            "title": "Bad\x0cTitle\x00End",
            "author": "A\x08uthor",
            "summary": "line1\nline2\x1fmore",
            "acquisitions": [{"type": "application/epub+zip", "href": "http://host/d/\x001.epub"}],
        }],
    )
    feed = parse(xml)  # must not raise OpdsParseError
    # (The parser collapses runs of legal whitespace to single spaces on read;
    # what matters here is that the control chars were dropped, not escaped into
    # an unparseable token.)
    assert feed.title == "Shelf Name"
    entry = feed.entries[0]
    assert entry.title == "BadTitleEnd"
    assert entry.author == "Author"
    assert entry.summary == "line1 line2more"
