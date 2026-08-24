"""Tests for :mod:`app.reader` — the sanitizer/block-splitter trusted seam.

Every hostile-input case here is a regression guard for the app's *only*
``| safe`` render seam (old Safari has no CSP to fall back on).
"""
from __future__ import annotations

import re

from app.reader import sanitize_chapter

_ATTR_NAME_RE = re.compile(r'([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(".*?"|\'.*?\')')


def _tag_attr_names(fragment: str, tag: str) -> set[str]:
    """Return the set of attribute *names* a rendered ``<tag ...>`` carries.

    Used to prove an attacker-controlled attribute value cannot break out
    of its quotes and register as a new, separate attribute (e.g. a
    hostile ``alt`` value forging a live ``onerror=`` attribute).
    """
    match = re.search(rf"<{tag}\b[^>]*>", fragment)
    assert match, f"no <{tag}> tag found in {fragment!r}"
    return {name for name, _ in _ATTR_NAME_RE.findall(match.group(0))}


def _resolve_image_none(_href: str) -> int | None:
    return None


def _resolve_link_none(_href: str) -> int | None:
    return None


def test_hostile_markup_stripped() -> None:
    xhtml = (
        '<?xml version="1.0"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        "<script>alert(1)</script>"
        '<p onclick="alert(1)" style="color:red">hi</p>'
        "<iframe src=\"http://evil/\"></iframe>"
        '<form action="http://evil/"><input/></form>'
        '<a href="javascript:alert(1)">bad link</a>'
        '<img src="http://evil/x.png"/>'
        "</body></html>"
    )
    blocks = sanitize_chapter(
        xhtml, resolve_image=_resolve_image_none, resolve_link=_resolve_link_none
    )
    joined = "\n".join(blocks)
    for hostile in (
        "<script",
        " on",
        "style=",
        "<iframe",
        "<form",
        "javascript:",
        "http://evil",
    ):
        assert hostile not in joined, f"{hostile!r} leaked into sanitized output"


def test_malformed_input_escapes() -> None:
    source = "<p>unterminated <b>bold\n\nnext para <weird"
    blocks = sanitize_chapter(
        source, resolve_image=_resolve_image_none, resolve_link=_resolve_link_none
    )
    assert len(blocks) >= 1
    joined = "\n".join(blocks)
    # No raw '<' from the source may survive unescaped.
    assert "<b>" not in joined
    assert "<weird" not in joined
    assert "&lt;" in joined


def test_structure_survives() -> None:
    xhtml = (
        '<?xml version="1.0"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        "<h1>Title</h1>"
        "<p>Some text</p>"
        "<ul><li>one</li><li>two</li></ul>"
        "<table><tr><td>a</td></tr></table>"
        '<img src="images/cover.jpg" alt="Cover"/>'
        '<a href="chapter2.xhtml">Next chapter</a>'
        "</body></html>"
    )

    def resolve_image(href: str) -> int | None:
        return 0 if href == "images/cover.jpg" else None

    def resolve_link(href: str) -> int | None:
        return 1 if href == "chapter2.xhtml" else None

    blocks = sanitize_chapter(
        xhtml, resolve_image=resolve_image, resolve_link=resolve_link
    )
    joined = "\n".join(blocks)
    assert "<h1>" in joined and "Title" in joined
    assert "<p>" in joined
    assert "<ul>" in joined and "<li>" in joined
    assert "<table>" in joined
    assert "{IMG:0}" in joined
    assert "{CH:1}" in joined
    assert 'alt="Cover"' in joined


def test_fragment_links_unwrapped() -> None:
    xhtml = (
        '<?xml version="1.0"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        '<p>See <a href="#note3">this note</a>.</p>'
        "</body></html>"
    )
    blocks = sanitize_chapter(
        xhtml, resolve_image=_resolve_image_none, resolve_link=_resolve_link_none
    )
    joined = "\n".join(blocks)
    assert "<a" not in joined
    assert "this note" in joined


def test_alt_attribute_cannot_break_out_of_quotes() -> None:
    """A hostile alt value must never inject a live attribute (SEC-01)."""
    xhtml = (
        '<?xml version="1.0"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        "<img src=\"a.png\" alt='x\" onerror=\"alert(1)'/>"
        "</body></html>"
    )

    def resolve_image(_href: str) -> int | None:
        return 1

    blocks = sanitize_chapter(
        xhtml, resolve_image=resolve_image, resolve_link=_resolve_link_none
    )
    joined = "\n".join(blocks)
    # The rendered alt attribute must stay a single, well-formed attribute —
    # no stray quote from the source may terminate it early and register a
    # live 'onerror' attribute alongside it.
    assert _tag_attr_names(joined, "img") == {"src", "alt"}


def test_alt_attribute_numeric_entity_cannot_break_out_of_quotes() -> None:
    """Same attack via a numeric character reference for the quote (&#34;)."""
    xhtml = (
        '<?xml version="1.0"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        '<img src="a.png" alt="x&#34; onerror=&#34;alert(1)"/>'
        "</body></html>"
    )

    def resolve_image(_href: str) -> int | None:
        return 1

    blocks = sanitize_chapter(
        xhtml, resolve_image=resolve_image, resolve_link=_resolve_link_none
    )
    joined = "\n".join(blocks)
    assert _tag_attr_names(joined, "img") == {"src", "alt"}


def test_uppercase_script_and_style_dropped_with_subtree() -> None:
    """Tag matching must be case-insensitive — SCRIPT/STYLE must still drop."""
    xhtml = (
        '<?xml version="1.0"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        "<SCRIPT>alert(1)</SCRIPT>"
        "<STYLE>body{background:url(http://evil/x)}</STYLE>"
        "<p>safe</p>"
        "</body></html>"
    )
    blocks = sanitize_chapter(
        xhtml, resolve_image=_resolve_image_none, resolve_link=_resolve_link_none
    )
    joined = "\n".join(blocks)
    assert "alert(1)" not in joined
    assert "background" not in joined
    assert "http://evil" not in joined
    assert "safe" in joined


def test_deeply_nested_markup_degrades_without_raising() -> None:
    """A nesting bomb must degrade gracefully, never raise RecursionError."""
    depth = 3000
    xhtml = (
        '<?xml version="1.0"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        + "<div>" * depth
        + "deep"
        + "</div>" * depth
        + "</body></html>"
    )
    blocks = sanitize_chapter(
        xhtml, resolve_image=_resolve_image_none, resolve_link=_resolve_link_none
    )
    # No exception raised is the primary assertion; a list (possibly empty
    # past the cap) is a valid graceful result.
    assert isinstance(blocks, list)


def test_placeholder_forgery_neutralized_in_text_and_alt() -> None:
    """Book text/alt containing literal {IMG:n}/{CH:i} must not forge a
    sentinel that a later stage would mistake for one this module emitted.
    """
    xhtml = (
        '<?xml version="1.0"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        '<p>fake {IMG:0} and {CH:1} sentinels</p>'
        '<img src="a.png" alt="fake {IMG:0} alt"/>'
        "</body></html>"
    )

    def resolve_image(_href: str) -> int | None:
        return 9

    blocks = sanitize_chapter(
        xhtml, resolve_image=resolve_image, resolve_link=_resolve_link_none
    )
    joined = "\n".join(blocks)
    assert "{IMG:0}" not in joined
    assert "{CH:1}" not in joined
    # The real, module-generated placeholder must still be intact.
    assert "{IMG:9}" in joined


def test_colspan_validated() -> None:
    xhtml = (
        '<?xml version="1.0"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        '<table><tr><td colspan="2">good</td>'
        '<td colspan="two">bad</td>'
        '<td colspan="3" rowspan="x">mixed</td></tr></table>'
        "</body></html>"
    )
    blocks = sanitize_chapter(
        xhtml, resolve_image=_resolve_image_none, resolve_link=_resolve_link_none
    )
    joined = "\n".join(blocks)
    assert 'colspan="2"' in joined
    assert 'colspan="two"' not in joined
    assert 'colspan="3"' in joined
    assert 'rowspan="x"' not in joined


def test_section_wrapper_flattens_to_individual_blocks() -> None:
    """A chapter wrapped in one <section> paginates by paragraph, not as a
    single un-splittable block (book-fidelity fix; verified on a real EPUB3)."""
    xhtml = (
        "<html><body><section>"
        + "".join(f"<p>Paragraph {i}.</p>" for i in range(5))
        + "</section></body></html>"
    )
    blocks = sanitize_chapter(
        xhtml, resolve_image=_resolve_image_none, resolve_link=_resolve_link_none
    )
    assert len(blocks) == 5
    assert all(b.startswith("<p>") for b in blocks)


def test_nested_div_wrappers_flatten() -> None:
    """Nested transparent wrappers still surface the real blocks."""
    xhtml = (
        "<html><body><div><div>"
        "<h2>Title</h2><p>One.</p><p>Two.</p>"
        "</div></div></body></html>"
    )
    blocks = sanitize_chapter(
        xhtml, resolve_image=_resolve_image_none, resolve_link=_resolve_link_none
    )
    assert len(blocks) == 3


def test_wrapper_with_direct_text_is_kept_whole() -> None:
    """A container carrying its own text is NOT flattened (no content loss)."""
    xhtml = "<html><body><div>Loose text before <p>a para</p></div></body></html>"
    blocks = sanitize_chapter(
        xhtml, resolve_image=_resolve_image_none, resolve_link=_resolve_link_none
    )
    assert len(blocks) == 1
    assert "Loose text before" in blocks[0]
