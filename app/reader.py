"""Chapter sanitizer and block splitter — the app's **only** trusted seam.

:func:`sanitize_chapter` is the sole place in RetroShelf whose output is
later rendered with Jinja's ``| safe`` filter. iOS 5.1.1-12 Safari has no
CSP to fall back on, so this module is the entire XSS wall: it parses
upstream (untrusted) XHTML with :mod:`defusedxml`, rebuilds only an
allowlisted, attribute-free set of block-level elements, and drops
everything else — including all inline event handlers, ``style``, and any
element capable of executing script or loading a remote resource. Nothing
that was not explicitly allowed below survives into the returned blocks.

Image and chapter-link references are resolved to small integer indexes
via injected callbacks (``resolve_image`` / ``resolve_link``) rather than
being carried through as raw URLs, so this module never needs to know
about EPUB zip layout or upstream hosts, and no upstream URL ever reaches
rendered output or disk.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from xml.etree.ElementTree import Element  # noqa: S405 - typing only, parsing goes through defusedxml
from xml.sax.saxutils import escape, quoteattr

from defusedxml.ElementTree import ParseError, fromstring

# Elements rebuilt verbatim (attribute-free, except the few carve-outs
# handled explicitly in :func:`_render_element`). Anything not listed here
# and not in :data:`_DROPPED_ELEMENTS` is unwrapped — its children are kept,
# the wrapping tag itself is discarded.
_ALLOWED_ELEMENTS = frozenset(
    {
        "p", "div", "span", "h1", "h2", "h3", "h4", "h5", "h6", "em",
        "strong", "i", "b", "u", "s", "small", "br", "hr", "blockquote",
        "ul", "ol", "li", "dl", "dt", "dd", "img", "a", "table", "thead",
        "tbody", "tr", "td", "th", "caption", "sup", "sub", "pre", "code",
        "cite", "figure", "figcaption", "section", "article",
    }
)

# Elements dropped together with their entire subtree — never merely
# unwrapped, since their children (script bodies, style rules, form
# controls) are hostile or meaningless outside the element itself.
_DROPPED_ELEMENTS = frozenset(
    {
        "script", "style", "iframe", "object", "embed", "form", "link",
        "meta", "svg", "video", "audio", "math",
    }
)

# Self-closing / no-content elements: rendered as a bare tag, never with a
# closing tag or text content.
_VOID_ELEMENTS = frozenset({"br", "hr"})

_SPAN_ATTRS = ("colspan", "rowspan")

_INT_RE = re.compile(r"^[0-9]+$")

# Recursion cap for the (pure-Python, genuinely recursive) render walk. A
# hostile chapter can nest thousands of elements deep to blow the Python
# call stack (RecursionError, an uncaught 500); this bounds render depth the
# same way app/download.py::_PILImage.MAX_IMAGE_PIXELS bounds decoded cover
# pixels — cap untrusted-input expansion at the boundary. Comfortably below
# both realistic document nesting and Python's default recursion limit.
MAX_NESTING_DEPTH = 100

# Sentinel prefixes this module emits (``{IMG:n}`` / ``{CH:i}``). Book text
# or an ``alt`` value that happens to contain one of these literally must
# never be indistinguishable from a real, module-generated placeholder once
# it reaches SS-02/03's substitution step — so any occurrence coming from
# untrusted content is neutralized with an invisible break.
_PLACEHOLDER_PREFIXES = ("{IMG:", "{CH:")
_ZERO_WIDTH_BREAK = "​"


def _local_name(tag: str) -> str:
    """Strip an XML namespace URI from *tag*, returning the lowercased local
    name.

    XHTML chapters are namespaced (``{http://www.w3.org/1999/xhtml}p``);
    the allowlist is namespace-agnostic, so every tag and attribute lookup
    goes through this first. Lowercased so tag matching against the
    allowlist/droplist is case-insensitive (``<SCRIPT>``/``<Script>`` must
    drop exactly like ``<script>``).

    :param tag: A tag as reported by :mod:`xml.etree.ElementTree`, possibly
        of the form ``{uri}local``.
    :returns: The local (namespace-stripped), lowercased tag name.
    """
    local = tag.split("}", 1)[1] if tag.startswith("{") else tag
    return local.lower()


def _neutralize_placeholders(text: str) -> str:
    """Break any literal ``{IMG:``/``{CH:`` sequence in untrusted *text*.

    Inserts a zero-width space immediately after the opening brace so the
    sequence is visually unchanged but can no longer be mistaken, byte for
    byte, for a placeholder this module generates itself.

    :param text: Untrusted text (element text/tail or an ``alt`` value).
    :returns: *text* with any forgeable placeholder prefix broken up.
    """
    for prefix in _PLACEHOLDER_PREFIXES:
        if prefix in text:
            broken = prefix[0] + _ZERO_WIDTH_BREAK + prefix[1:]
            text = text.replace(prefix, broken)
    return text


def _escape_text(text: str) -> str:
    """HTML-escape *text* and neutralize forgeable placeholder sequences.

    :param text: Untrusted element text or tail content.
    :returns: Escaped text safe to splice into rebuilt markup.
    """
    return escape(_neutralize_placeholders(text))


def _render_children(
    element: Element,
    *,
    depth: int,
    resolve_image: Callable[[str], int | None],
    resolve_link: Callable[[str], int | None],
) -> str:
    """Render *element*'s children (and trailing text) to an HTML string.

    :param element: The parent whose children are walked.
    :param depth: Current render recursion depth (see
        :data:`MAX_NESTING_DEPTH`); children are rendered one level deeper.
    :param resolve_image: Callback mapping an ``img`` ``src`` to an image
        index, or ``None`` if unresolvable.
    :param resolve_link: Callback mapping an ``a`` ``href`` to a chapter
        index, or ``None`` if unresolvable.
    :returns: Concatenated HTML for all children plus intervening text.
    """
    parts: list[str] = []
    if element.text:
        parts.append(_escape_text(element.text))
    for child in element:
        parts.append(
            _render_element(
                child,
                depth=depth + 1,
                resolve_image=resolve_image,
                resolve_link=resolve_link,
            )
        )
        if child.tail:
            parts.append(_escape_text(child.tail))
    return "".join(parts)


def _render_element(
    element: Element,
    *,
    depth: int,
    resolve_image: Callable[[str], int | None],
    resolve_link: Callable[[str], int | None],
) -> str:
    """Render one element (and its subtree) to a sanitized HTML string.

    Non-allowlisted elements are unwrapped (children kept, tag dropped).
    Elements in :data:`_DROPPED_ELEMENTS` vanish along with their subtree.
    Past :data:`MAX_NESTING_DEPTH`, an element is dropped outright (treated
    as unresolvable) rather than recursed into, so a nesting bomb degrades
    gracefully instead of raising ``RecursionError``.

    :param element: The element to render.
    :param depth: Current render recursion depth, counted from the
        top-level children of ``<body>`` at depth 0.
    :param resolve_image: See :func:`_render_children`.
    :param resolve_link: See :func:`_render_children`.
    :returns: The sanitized HTML fragment for this element's subtree.
    """
    if depth > MAX_NESTING_DEPTH:
        return ""

    tag = _local_name(element.tag)

    if tag in _DROPPED_ELEMENTS:
        return ""

    if tag not in _ALLOWED_ELEMENTS:
        return _render_children(
            element, depth=depth, resolve_image=resolve_image, resolve_link=resolve_link
        )

    if tag == "img":
        src = element.get("src")
        index = resolve_image(src) if src else None
        if index is None:
            return ""
        alt = element.get("alt")
        alt_attr = f" alt={quoteattr(_neutralize_placeholders(alt))}" if alt else ""
        return f'<img src="{{IMG:{index}}}"{alt_attr}/>'

    if tag == "a":
        href = element.get("href")
        link_index: int | None = None
        if href and not href.startswith("#"):
            link_index = resolve_link(href)
        inner = _render_children(
            element, depth=depth, resolve_image=resolve_image, resolve_link=resolve_link
        )
        if link_index is None:
            return inner
        return f'<a href="{{CH:{link_index}}}">{inner}</a>'

    if tag in _VOID_ELEMENTS:
        return f"<{tag}/>"

    attrs = ""
    if tag in ("td", "th"):
        attrs = "".join(
            f' {name}="{value}"'
            for name in _SPAN_ATTRS
            if (value := element.get(name)) is not None and _INT_RE.match(value)
        )

    inner = _render_children(
        element, depth=depth, resolve_image=resolve_image, resolve_link=resolve_link
    )
    return f"<{tag}{attrs}>{inner}</{tag}>"


def _escaped_text_fallback(source: str) -> list[str]:
    """Build escaped-plain-text blocks from *source* when parsing fails.

    The source is never trusted as markup once parsing fails: every
    character is HTML-escaped, then the text is split on blank lines and
    each resulting paragraph is wrapped in a ``<p>``.

    :param source: The raw (non-XML) chapter text.
    :returns: A list of ``<p>...</p>`` block strings; empty paragraphs are
        skipped.
    """
    blocks: list[str] = []
    for paragraph in re.split(r"\n\s*\n", source):
        stripped = paragraph.strip()
        if not stripped:
            continue
        blocks.append(f"<p>{_escape_text(stripped)}</p>")
    if not blocks:
        blocks.append("<p></p>")
    return blocks


def _find_body(root: Element) -> Element:
    """Return the ``body`` element under *root*, or *root* itself if absent.

    :param root: The parsed document root.
    :returns: The element whose children become the block list.
    """
    for child in root.iter():
        if _local_name(child.tag) == "body":
            return child
    return root


def sanitize_chapter(
    source: str | bytes,
    *,
    resolve_image: Callable[[str], int | None],
    resolve_link: Callable[[str], int | None],
) -> list[str]:
    """Sanitize an XHTML chapter into an ordered list of safe HTML blocks.

    Parses *source* with :func:`defusedxml.ElementTree.fromstring`, strips
    XML namespaces to local tag names, and rebuilds only the allowlisted
    element set (see module docstring) as attribute-free HTML fragments —
    one fragment per top-level child of ``<body>``. Non-allowlisted
    elements are unwrapped (children kept); elements in
    :data:`_DROPPED_ELEMENTS` are dropped with their entire subtree.

    ``img`` elements are resolved via *resolve_image*: on a hit, the tag is
    rebuilt with ``src="{IMG:n}"`` and the original ``alt`` (if any); on a
    miss, the element is dropped. ``a`` elements are resolved via
    *resolve_link*: on a hit, ``href="{CH:i}"``; on a miss — including
    fragment-only hrefs like ``#note3``, which are never passed to the
    resolver — the tag is unwrapped to its text content. ``td``/``th``
    keep only integer-validated ``colspan``/``rowspan``.

    If *source* cannot be parsed as XML at all, it is treated as plain
    text: every character is escaped and the text is split on blank lines
    into ``<p>`` blocks, so raw markup from a malformed chapter can never
    reach the ``| safe`` render seam.

    :param source: The chapter's raw XHTML content, as ``str`` or ``bytes``.
    :param resolve_image: Maps an ``img`` ``src`` href to an image index,
        or ``None`` if it cannot be resolved (the image is dropped).
    :param resolve_link: Maps an ``a`` ``href`` to a chapter index, or
        ``None`` if it cannot be resolved (the link is unwrapped to text).
    :returns: An ordered list of block-level HTML fragment strings.
        Empty or whitespace-only blocks are skipped.
    """
    text = source.decode("utf-8", errors="replace") if isinstance(source, bytes) else source

    try:
        root = fromstring(text)
    except (ParseError, ValueError):
        return _escaped_text_fallback(text)

    body = _find_body(root)

    blocks: list[str] = []
    for child in body:
        rendered = _render_element(
            child, depth=0, resolve_image=resolve_image, resolve_link=resolve_link
        )
        if rendered.strip():
            blocks.append(rendered)
    return blocks
