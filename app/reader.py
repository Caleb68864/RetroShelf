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

import asyncio
import io
import json
import logging
import os
import posixpath
import re
import shutil
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import unquote
from xml.etree.ElementTree import Element  # noqa: S405 - typing only, parsing goes through defusedxml
from xml.sax.saxutils import escape, quoteattr

from defusedxml.ElementTree import ParseError, fromstring

from .errors import ReaderError
from .kavita import KavitaClient
from .store import book_key as _store_book_key

try:
    from PIL import Image as _PILImage
    _PIL_AVAILABLE = True
    # Mirrors app/download.py's decompression-bomb cap: an embedded chapter
    # image never legitimately needs tens of megapixels. Set here too since
    # this module may be imported/tested without app/download.py ever
    # running (the attribute is process-global on PIL.Image either way).
    _PILImage.MAX_IMAGE_PIXELS = 64_000_000
except ImportError:  # pragma: no cover
    _PIL_AVAILABLE = False

log = logging.getLogger("retroshelf.reader")

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


# Transparent structural wrappers. Some EPUBs (especially EPUB3) wrap a whole
# chapter in one ``<section>``/``<div>``, which would otherwise collapse the
# chapter into a single un-splittable block — verified against a real
# Project Gutenberg EPUB3 (Frankenstein: 32 chapters, only 38 blocks). We
# descend through these at the top level so the real paragraphs/headings inside
# become individual blocks and fine pagination works. [book-fidelity]
_FLOW_CONTAINERS = frozenset({"section", "div", "article", "main"})
_MAX_FLATTEN_DEPTH = 6


def _top_level_block_elements(container: Element, depth: int = 0):
    """Yield the effective top-level block elements of a chapter *container*.

    A transparent structural wrapper (``section``/``div``/``article``/``main``)
    with no direct text of its own is descended into, so its block-level
    children surface as individual blocks rather than one giant block. Wrappers
    that carry direct text, non-container children, or nesting past
    :data:`_MAX_FLATTEN_DEPTH` are yielded whole (rendered normally). Order is
    preserved; content is never dropped.

    :param container: The ``<body>`` (or a wrapper being descended).
    :param depth: Current descent depth, bounded by :data:`_MAX_FLATTEN_DEPTH`.
    :returns: An iterator of the elements to render as top-level blocks.
    """
    for child in container:
        name = _local_name(child.tag)
        transparent = (
            name in _FLOW_CONTAINERS
            and depth < _MAX_FLATTEN_DEPTH
            and not (child.text and child.text.strip())
            and len(child)  # has element children to surface
        )
        if transparent:
            yield from _top_level_block_elements(child, depth + 1)
        else:
            yield child


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
    for child in _top_level_block_elements(body):
        rendered = _render_element(
            child, depth=0, resolve_image=resolve_image, resolve_link=resolve_link
        )
        if rendered.strip():
            blocks.append(rendered)
    return blocks


# ---------------------------------------------------------------------------
# EPUB parsing and shelving (SS-02)
# ---------------------------------------------------------------------------
#
# ``shelve_book`` turns an upstream EPUB into a directory of pre-sanitized,
# pre-split chapter fragments under ``{cache_dir}/reader/{book_key}/`` so
# every subsequent page view is a local file read with zero upstream
# traffic. Every cap below exists to keep a single hostile or oversized book
# from exhausting memory, disk, or the request that is shelving it.

# A book download is a few tens of MB at most; 80MB is generous headroom
# while still bounding a hostile or mistaken upstream. The spool is written
# straight to disk in capped chunks — never buffered whole in RAM. [SS-02]
MAX_EPUB_BYTES = 80 * 1024 * 1024

# A spine with thousands of entries is not a book; it is an attempt to make
# shelving do unbounded work. Real EPUBs (even omnibus editions) stay well
# under this. [SS-02]
MAX_SPINE_ITEMS = 500

# Per-chapter source cap. A single 2MB-plus XHTML chapter is already
# pathological; capping it keeps one bloated chapter from blowing the
# unpacked-total budget on its own. [SS-02]
MAX_CHAPTER_BYTES = 2 * 1024 * 1024

# Ceiling on total *decompressed* bytes read from the zip across container,
# OPF, NCX/nav, every chapter, and every image — enforced against actual
# bytes read off the decompression stream, not the (forgeable) declared
# size in the zip's central directory, so this also defeats zip bombs.
# [SS-02]
MAX_UNPACKED_BYTES = 120 * 1024 * 1024

# Ceiling for the on-disk reader cache (``{cache_dir}/reader``), pruned
# oldest-shelved-book-first by :func:`prune_reader_cache`. Mirrors
# ``app.download.MAX_COVER_CACHE_BYTES``. [SS-02]
MAX_READER_CACHE_BYTES = 1024 * 1024 * 1024

# Bounds the network spool only (see ``_spool_epub``). The shared httpx
# client runs with ``read=None`` so book downloads are never cut off
# mid-stream; that is wrong for the *first* tap on an unshelved book, where
# a stalled upstream must not hang the request forever. Mirrors the
# per-request ``httpx.Timeout`` override in ``KavitaClient.fetch_feed``,
# applied here via ``asyncio.wait_for`` since ``open_stream`` itself takes
# no timeout override. [SS-02]
SHELVE_TIMEOUT = 120.0

# Chapter images are downscaled to this max edge, same idea as
# ``app.download.stream_cover``'s cover downscaling. [SS-02]
READER_IMAGE_MAX_EDGE = 1024
READER_IMAGE_JPEG_QUALITY = 80

# container.xml/OPF/NCX/nav are structural metadata, never legitimately
# large; a tighter per-member cap than MAX_UNPACKED_BYTES shrinks the
# worst-case single-read RAM for these specifically. [SS-02]
MAX_METADATA_MEMBER_BYTES = 4 * 1024 * 1024

_PASSTHROUGH_IMAGE_FORMATS = {"JPEG", "PNG"}

_HEADING_TAGS = ("h1", "h2", "h3")


@dataclass
class ChapterMeta:
    """Per-chapter metadata stored in a book's :class:`Manifest`.

    :ivar title: The chapter's display title (from the nav doc, NCX, first
        heading, or a ``"Chapter N"`` fallback).
    :ivar blocks: Number of sanitized HTML blocks in the chapter.
    :ivar chars: Total character count across the chapter's blocks, used by
        SS-03's pagination to size parts.
    """

    title: str
    blocks: int
    chars: int


@dataclass
class Manifest:
    """Everything the reader needs to serve a shelved book without ever
    re-parsing its EPUB.

    Written as ``manifest.json`` inside the book's cache directory by
    :func:`shelve_book`; loaded back by :func:`load_manifest`.

    :ivar version: Manifest schema version (currently always ``1``).
    :ivar book_key: The book's stable cache key (see
        :func:`app.store.book_key`).
    :ivar title: Book title, as already known to the caller's record.
    :ivar author: Book author, as already known to the caller's record.
    :ivar chapters: Per-chapter metadata, in spine order.
    :ivar images: Number of images extracted into ``images/``.
    :ivar total_chars: Sum of every chapter's ``chars``.
    :ivar created: ``time.time()`` at the moment shelving finished.
    """

    version: int
    book_key: str
    title: str
    author: str
    chapters: list[ChapterMeta]
    images: int
    total_chars: int
    created: float


class _UnpackBudget:
    """Running counter enforcing :data:`MAX_UNPACKED_BYTES` across an
    entire shelving pass.

    Every capped zip-member read (:func:`_read_member_capped`) reports the
    bytes it actually decompressed here; once the cumulative total crosses
    the ceiling, shelving fails before the host does unbounded work on a
    zip bomb.
    """

    def __init__(self, limit: int) -> None:
        """:param limit: Maximum cumulative decompressed bytes to allow."""
        self._limit = limit
        self.used = 0

    def add(self, n: int) -> None:
        """Record *n* more decompressed bytes; raise once past the limit.

        :param n: Bytes just read from a zip member.
        :raises ReaderError: If the running total now exceeds the limit.
        """
        self.used += n
        if self.used > self._limit:
            raise ReaderError("This book unpacks to more content than is allowed")


def _is_safe_member(path: str) -> bool:
    """Return whether a normalized zip-relative *path* stays inside the
    archive (the zip-slip guard).

    Rejects any path that is absolute or whose normalized form still has a
    ``..`` segment (i.e. it would resolve outside the archive root).
    :func:`shelve_book` never uses a zip member name as an output path —
    extracted content is always written to our own numbered files — so this
    guard exists purely to refuse *reading* anything outside the archive's
    logical namespace, defence in depth against a hostile zip.

    :param path: A posix-style path, already run through
        :func:`posixpath.normpath`.
    :rtype: bool
    """
    if not path or path.startswith("/"):
        return False
    return ".." not in path.split("/")


def _norm_zip_path(base_dir: str, href: str) -> str | None:
    """Resolve *href* (an EPUB-internal relative reference) against
    *base_dir* to a normalized, zip-slip-safe zip path.

    Strips any URL fragment and percent-decodes the path before joining.

    :param base_dir: The zip-relative directory the href is relative to
        (e.g. the OPF's or a chapter's own directory).
    :param href: The raw href from OPF/nav/NCX/chapter markup.
    :returns: The normalized zip path, or ``None`` if *href* is empty or
        resolves outside the archive.
    """
    if not href:
        return None
    raw_path = unquote(href.split("#", 1)[0])
    if not raw_path:
        return None
    joined = posixpath.normpath(posixpath.join(base_dir, raw_path)) if base_dir else posixpath.normpath(raw_path)
    if not _is_safe_member(joined):
        return None
    return joined


def _resolver(base_dir: str, index_by_path: dict[str, int]) -> Callable[[str], int | None]:
    """Build a ``resolve_image``/``resolve_link`` callback for one chapter.

    :param base_dir: The chapter's own zip-relative directory, used to
        resolve the hrefs it contains.
    :param index_by_path: Map of normalized zip path to integer index
        (either the image index map or the chapter index map).
    :returns: A callable suitable for :func:`sanitize_chapter`'s
        ``resolve_image``/``resolve_link`` parameters.
    """

    def resolve(href: str) -> int | None:
        path = _norm_zip_path(base_dir, href)
        if path is None:
            return None
        return index_by_path.get(path)

    return resolve


def _read_member_capped(zf: zipfile.ZipFile, name: str, member_cap: int, budget: _UnpackBudget) -> bytes:
    """Read zip member *name* fully, refusing to exceed *member_cap* bytes.

    Reads from the live decompression stream in chunks and counts actual
    decompressed bytes — not the (forgeable) size declared in the zip's
    central directory — so a zip-bomb entry is caught mid-read rather than
    trusted. Successfully read bytes are also charged against *budget*.

    :param zf: The open archive.
    :param name: A zip member name already validated by
        :func:`_is_safe_member` (or a fixed, known-safe path).
    :param member_cap: Maximum bytes to accept for this one member.
    :param budget: The shelving pass's running :class:`_UnpackBudget`.
    :returns: The member's full decompressed content.
    :raises ReaderError: If the member exceeds *member_cap*, the archive
        entry cannot be opened/decompressed, or the entry is
        zip-layer-encrypted or uses an unsupported compression method.
    """
    chunks: list[bytes] = []
    total = 0
    try:
        with zf.open(name) as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > member_cap:
                    raise ReaderError(f"This book's {name!r} entry exceeds the size cap")
                chunks.append(chunk)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ReaderError(f"Could not read {name!r} from this book") from exc
    except RuntimeError as exc:
        # zipfile raises plain RuntimeError (NotImplementedError, its
        # subclass, for an unsupported compression method) for a
        # zip-layer-encrypted or otherwise unreadable entry — never
        # BadZipFile/OSError. This is a *second*, lower-level form of DRM
        # distinct from the OCF ``encryption.xml`` check: a book can be
        # zip-encrypted without ever declaring it in encryption.xml.
        if "encrypted" in str(exc).lower():
            raise ReaderError(
                "This book is protected (DRM) and can't be read in the browser"
            ) from exc
        raise ReaderError(f"Could not read {name!r} from this book") from exc
    budget.add(total)
    return b"".join(chunks)


def _parse_container(data: bytes) -> str:
    """Parse ``META-INF/container.xml`` and return the OPF's zip path.

    :param data: The raw ``container.xml`` bytes.
    :returns: The ``full-path`` of the first ``rootfile``.
    :raises ReaderError: If the document does not parse or declares no
        rootfile.
    """
    try:
        root = fromstring(data)
    except (ParseError, ValueError) as exc:
        raise ReaderError("This does not look like a valid EPUB file") from exc
    for el in root.iter():
        if _local_name(el.tag) == "rootfile":
            path = el.get("full-path")
            if path:
                return path
    raise ReaderError("This does not look like a valid EPUB file")


def _parse_opf(data: bytes) -> tuple[dict[str, tuple[str, str, str]], list[str], str | None]:
    """Parse the OPF package document's manifest and spine.

    :param data: The raw OPF bytes.
    :returns: A 3-tuple ``(manifest, spine_idrefs, ncx_idref)`` where
        *manifest* maps item id to ``(href, media_type, properties)``,
        *spine_idrefs* is the ordered list of ``itemref`` idrefs, and
        *ncx_idref* is the spine's ``toc`` attribute (an NCX manifest item
        id), or ``None``.
    :raises ReaderError: If the document does not parse as XML.
    """
    try:
        root = fromstring(data)
    except (ParseError, ValueError) as exc:
        raise ReaderError("This does not look like a valid EPUB file") from exc
    manifest: dict[str, tuple[str, str, str]] = {}
    spine: list[str] = []
    ncx_idref: str | None = None
    for el in root.iter():
        name = _local_name(el.tag)
        if name == "item":
            item_id = el.get("id")
            href = el.get("href")
            if item_id and href:
                manifest[item_id] = (href, el.get("media-type") or "", el.get("properties") or "")
        elif name == "itemref":
            idref = el.get("idref")
            if idref:
                spine.append(idref)
        elif name == "spine":
            ncx_idref = el.get("toc")
    return manifest, spine, ncx_idref


def _titles_from_nav(data: bytes, nav_dir: str) -> dict[str, str]:
    """Extract chapter titles from an EPUB3 navigation document's ToC.

    Looks for the first ``<nav epub:type="toc">`` and reads its anchor
    text, keyed by the anchor's normalized target zip path.

    :param data: The raw nav document bytes.
    :param nav_dir: The nav document's own zip-relative directory, used to
        resolve its (relative) anchor hrefs.
    :returns: Map of normalized zip path to chapter title. Empty on any
        parse failure or if no ToC nav is found.
    """
    try:
        root = fromstring(data)
    except (ParseError, ValueError):
        return {}
    titles: dict[str, str] = {}
    for nav_el in root.iter():
        if _local_name(nav_el.tag) != "nav":
            continue
        type_attr = next(
            (v for k, v in nav_el.attrib.items() if _local_name(k) == "type"), ""
        )
        if "toc" not in type_attr.split():
            continue
        for a_el in nav_el.iter():
            if _local_name(a_el.tag) != "a":
                continue
            href = a_el.get("href")
            path = _norm_zip_path(nav_dir, href) if href else None
            text = "".join(a_el.itertext()).strip()
            if path and text and path not in titles:
                titles[path] = text
        break
    return titles


def _titles_from_ncx(data: bytes, ncx_dir: str) -> dict[str, str]:
    """Extract chapter titles from an EPUB2 NCX table of contents.

    :param data: The raw ``toc.ncx`` bytes.
    :param ncx_dir: The NCX's own zip-relative directory, used to resolve
        its (relative) ``content`` ``src`` references.
    :returns: Map of normalized zip path to chapter title. Empty on any
        parse failure.
    """
    try:
        root = fromstring(data)
    except (ParseError, ValueError):
        return {}
    titles: dict[str, str] = {}
    for navpoint in root.iter():
        if _local_name(navpoint.tag) != "navpoint":
            continue
        title_text = ""
        src: str | None = None
        for child in navpoint:
            cname = _local_name(child.tag)
            if cname == "navlabel":
                for text_el in child:
                    if _local_name(text_el.tag) == "text":
                        title_text = (text_el.text or "").strip()
            elif cname == "content":
                src = child.get("src")
        path = _norm_zip_path(ncx_dir, src) if src else None
        if path and title_text:
            titles.setdefault(path, title_text)
    return titles


def _title_from_heading(source: bytes) -> str | None:
    """Return the text of the first ``h1``-``h3`` element in *source*.

    :param source: A chapter's raw XHTML bytes.
    :returns: The heading's text, or ``None`` if *source* does not parse
        or contains no heading.
    """
    try:
        root = fromstring(source)
    except (ParseError, ValueError):
        return None
    for el in root.iter():
        if _local_name(el.tag) in _HEADING_TAGS:
            text = "".join(el.itertext()).strip()
            if text:
                return text
    return None


def _extract_images(
    zf: zipfile.ZipFile, names: set[str], manifest_items: dict[str, tuple[str, str, str]],
    opf_dir: str, tmp_dir: str, budget: _UnpackBudget,
) -> dict[str, int]:
    """Extract and downscale every image manifest item into ``images/``.

    A no-op (returns an empty map) when Pillow is unavailable — per the
    shelving contract, an EPUB shelved without Pillow is text-only and
    every ``img`` placeholder is dropped rather than pointing at a missing
    file.

    :param zf: The open archive.
    :param names: The set of all member names in the archive.
    :param manifest_items: The OPF manifest, as returned by
        :func:`_parse_opf`.
    :param opf_dir: The OPF's own zip-relative directory.
    :param tmp_dir: The book's in-progress shelving directory; images are
        written under ``{tmp_dir}/images/``.
    :param budget: The shelving pass's running :class:`_UnpackBudget`.
    :returns: Map of normalized zip path to image index, for images that
        extracted and decoded successfully.
    """
    index_by_path: dict[str, int] = {}
    if not _PIL_AVAILABLE:
        return index_by_path
    images_dir = os.path.join(tmp_dir, "images")
    for href, media_type, _props in manifest_items.values():
        if not media_type.startswith("image/"):
            continue
        path = _norm_zip_path(opf_dir, href)
        if path is None or path not in names or path in index_by_path:
            continue
        try:
            raw = _read_member_capped(zf, path, MAX_UNPACKED_BYTES, budget)
            img: _PILImage.Image = _PILImage.open(io.BytesIO(raw))
            img.load()
            fmt = img.format or ""
            w, h = img.size
            if fmt in _PASSTHROUGH_IMAGE_FORMATS and max(w, h) <= READER_IMAGE_MAX_EDGE:
                out_bytes = raw
                out_ct = f"image/{fmt.lower()}"
            else:
                if max(w, h) > READER_IMAGE_MAX_EDGE:
                    ratio = READER_IMAGE_MAX_EDGE / max(w, h)
                    img = img.resize(
                        (max(1, int(w * ratio)), max(1, int(h * ratio))), _PILImage.Resampling.LANCZOS
                    )
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=READER_IMAGE_JPEG_QUALITY)
                out_bytes = buf.getvalue()
                out_ct = "image/jpeg"
        except ReaderError:
            raise
        except Exception:  # noqa: BLE001 - a single bad image must not fail the book
            continue
        idx = len(index_by_path)
        with open(os.path.join(images_dir, str(idx)), "wb") as f:
            f.write(out_bytes)
        with open(os.path.join(images_dir, f"{idx}.ct"), "w", encoding="ascii") as f:
            f.write(out_ct)
        index_by_path[path] = idx
    return index_by_path


def _manifest_to_dict(manifest: Manifest) -> dict:
    """Serialize *manifest* to a JSON-ready ``dict``.

    :param manifest: The manifest to serialize.
    :rtype: dict
    """
    return {
        "version": manifest.version,
        "book_key": manifest.book_key,
        "title": manifest.title,
        "author": manifest.author,
        "chapters": [
            {"title": c.title, "blocks": c.blocks, "chars": c.chars} for c in manifest.chapters
        ],
        "images": manifest.images,
        "total_chars": manifest.total_chars,
        "created": manifest.created,
    }


def _manifest_from_dict(data: dict) -> Manifest:
    """Reconstruct a :class:`Manifest` from :func:`_manifest_to_dict`'s output.

    :param data: A parsed ``manifest.json`` document.
    :returns: The reconstructed manifest.
    :raises KeyError, TypeError: If *data* is missing required fields or is
        the wrong shape; callers are expected to treat any such failure as
        "no manifest".
    """
    chapters = [
        ChapterMeta(title=str(c["title"]), blocks=int(c["blocks"]), chars=int(c["chars"]))
        for c in data["chapters"]
    ]
    return Manifest(
        version=int(data["version"]),
        book_key=str(data["book_key"]),
        title=str(data["title"]),
        author=str(data["author"]),
        chapters=chapters,
        images=int(data["images"]),
        total_chars=int(data["total_chars"]),
        created=float(data["created"]),
    )


def _rmtree_ignore(path: str) -> None:
    """Best-effort recursive delete of *path*; never raises.

    :param path: The directory to remove, if it exists.
    """
    shutil.rmtree(path, ignore_errors=True)


def _extract_epub(spool_path: str, key: str, record: dict, tmp_dir: str) -> Manifest:
    """Parse the spooled EPUB at *spool_path* and populate *tmp_dir* with
    its shelved (sanitized) form.

    Performs the full parse pipeline: zip-slip-safe container→OPF→spine
    resolution, an ``encryption.xml`` DRM check, spine/chapter/unpacked
    size-cap enforcement, per-chapter sanitization via
    :func:`sanitize_chapter`, chapter title resolution (nav doc → NCX →
    first heading → ``"Chapter N"``), and image extraction via
    :func:`_extract_images`.

    :param spool_path: Path to the fully-downloaded EPUB file on disk.
    :param key: The book's cache key (``Manifest.book_key``).
    :param record: The caller's book record; only ``t`` (title) and ``a``
        (author) are read — no URL from *record* is ever written to disk.
    :param tmp_dir: The book's in-progress shelving directory (already
        created, with empty ``chapters/`` and ``images/`` subdirectories).
    :returns: The completed :class:`Manifest` (also written to
        ``{tmp_dir}/manifest.json``).
    :raises ReaderError: On DRM, malformed/oversized input, an oversized
        spine, or zero readable chapters.
    """
    budget = _UnpackBudget(MAX_UNPACKED_BYTES)
    try:
        zf = zipfile.ZipFile(spool_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ReaderError("This does not look like a valid EPUB file") from exc

    with zf:
        names = set(zf.namelist())

        # DRM'd content cannot be sanitized or rendered; fail fast with a
        # distinct, friendly message steering back to the iBooks path.
        if "META-INF/encryption.xml" in names:
            raise ReaderError("This book is protected (DRM) and can't be read in the browser")

        if "META-INF/container.xml" not in names:
            raise ReaderError("This does not look like a valid EPUB file")
        container_bytes = _read_member_capped(zf, "META-INF/container.xml", MAX_METADATA_MEMBER_BYTES, budget)
        opf_path = _parse_container(container_bytes)
        if not _is_safe_member(opf_path) or opf_path not in names:
            raise ReaderError("This does not look like a valid EPUB file")

        opf_bytes = _read_member_capped(zf, opf_path, MAX_METADATA_MEMBER_BYTES, budget)
        opf_dir = posixpath.dirname(opf_path)
        manifest_items, spine_idrefs, ncx_idref = _parse_opf(opf_bytes)

        if len(spine_idrefs) > MAX_SPINE_ITEMS:
            raise ReaderError(f"This book has too many chapters (limit {MAX_SPINE_ITEMS})")

        chapter_paths: list[str] = []
        for idref in spine_idrefs:
            item = manifest_items.get(idref)
            if not item:
                continue
            path = _norm_zip_path(opf_dir, item[0])
            if path is not None and path in names:
                chapter_paths.append(path)

        if not chapter_paths:
            raise ReaderError("This book has no readable content")

        chapter_index_by_path = {path: i for i, path in enumerate(chapter_paths)}

        # -- chapter titles: nav doc, then NCX --
        nav_titles: dict[str, str] = {}
        for href, media_type, properties in manifest_items.values():
            if "nav" in properties.split():
                nav_path = _norm_zip_path(opf_dir, href)
                if nav_path and nav_path in names:
                    nav_bytes = _read_member_capped(zf, nav_path, MAX_METADATA_MEMBER_BYTES, budget)
                    nav_titles = _titles_from_nav(nav_bytes, posixpath.dirname(nav_path))
                break
        ncx_titles: dict[str, str] = {}
        ncx_item = manifest_items.get(ncx_idref) if ncx_idref else None
        if ncx_item is None:
            ncx_item = next(
                (v for v in manifest_items.values() if v[1] == "application/x-dtbncx+xml"), None
            )
        if ncx_item is not None:
            ncx_path = _norm_zip_path(opf_dir, ncx_item[0])
            if ncx_path and ncx_path in names:
                ncx_bytes = _read_member_capped(zf, ncx_path, MAX_METADATA_MEMBER_BYTES, budget)
                ncx_titles = _titles_from_ncx(ncx_bytes, posixpath.dirname(ncx_path))

        image_index_by_path = _extract_images(zf, names, manifest_items, opf_dir, tmp_dir, budget)

        chapters_meta: list[ChapterMeta] = []
        total_chars = 0
        for i, path in enumerate(chapter_paths):
            raw = _read_member_capped(zf, path, MAX_CHAPTER_BYTES, budget)
            chapter_dir = posixpath.dirname(path)
            blocks = sanitize_chapter(
                raw,
                resolve_image=_resolver(chapter_dir, image_index_by_path),
                resolve_link=_resolver(chapter_dir, chapter_index_by_path),
            )
            title = (
                nav_titles.get(path)
                or ncx_titles.get(path)
                or _title_from_heading(raw)
                or f"Chapter {i + 1}"
            )
            chars = sum(len(b) for b in blocks)
            total_chars += chars
            with open(os.path.join(tmp_dir, "chapters", f"{i}.json"), "w", encoding="utf-8") as f:
                json.dump({"blocks": blocks}, f)
            chapters_meta.append(ChapterMeta(title=title, blocks=len(blocks), chars=chars))

    manifest = Manifest(
        version=1,
        book_key=key,
        title=str(record.get("t") or "") or "Untitled",
        author=str(record.get("a") or ""),
        chapters=chapters_meta,
        images=len(image_index_by_path),
        total_chars=total_chars,
        created=time.time(),
    )
    with open(os.path.join(tmp_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(_manifest_to_dict(manifest), f)
    return manifest


async def _spool_epub(kc: KavitaClient, url: str, spool_path: str, cap: int) -> int:
    """Stream *url* to *spool_path* on disk, refusing to exceed *cap* bytes.

    The upstream body is never buffered whole in memory: it is written to
    disk chunk-by-chunk as it arrives, mirroring
    ``KavitaClient._read_capped``'s cap logic but writing to a file instead
    of accumulating in a list.

    :param kc: Kavita client; ``open_stream`` applies the SSRF guard.
    :param url: The upstream EPUB acquisition URL.
    :param spool_path: Destination path for the spooled file.
    :param cap: Maximum acceptable body size in bytes.
    :returns: The number of bytes written.
    :raises ReaderError: If the declared or actual body size exceeds *cap*.
    :raises KavitaError: If the upstream fetch fails outright.
    """
    resp = await kc.open_stream(url)
    total = 0
    try:
        declared = resp.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > cap:
            raise ReaderError(f"This book is too large to read here ({declared} bytes)")
        with open(spool_path, "wb") as f:
            async for chunk in resp.aiter_raw():
                total += len(chunk)
                if total > cap:
                    raise ReaderError(f"This book exceeds the {cap}-byte size cap")
                f.write(chunk)
    finally:
        await resp.aclose()
    return total


_SHELVE_LOCKS: dict[str, asyncio.Lock] = {}


def _lock_for(key: str) -> asyncio.Lock:
    """Return the process-wide :class:`asyncio.Lock` for book *key*.

    Lazily created and cached in :data:`_SHELVE_LOCKS`. Safe without an
    additional guard lock: there is no ``await`` between the dict lookup
    and insertion, so two concurrent shelves of the same book on this
    event loop cannot race each other into creating two different locks.

    :param key: The book's cache key.
    :rtype: asyncio.Lock
    """
    lock = _SHELVE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _SHELVE_LOCKS[key] = lock
    return lock


async def shelve_book(kc: KavitaClient, record: dict, cache_dir: str) -> Manifest:
    """Shelve *record*'s EPUB into the reader cache, or return its
    existing :class:`Manifest` if already shelved.

    First open is expensive (download + parse + sanitize); every
    subsequent call for the same book is a cache hit with zero upstream
    traffic. Concurrent shelves of the *same* book on this process are
    serialized by a per-``book_key`` :class:`asyncio.Lock`; the book is
    built into a ``{book_key}.tmp-{pid}`` directory and only becomes
    visible via an ``os.rename`` once complete, so a reader can never see
    a half-written book, and a crash mid-shelve leaves no visible
    partial state. No upstream URL is ever written to any cache file.

    :param kc: Kavita client used to spool the upstream EPUB. Every fetch
        goes through its SSRF guard.
    :param record: A book record with at least a ``u`` (acquisition URL)
        key; ``t`` (title) and ``a`` (author) are used for the manifest
        when present.
    :param cache_dir: The application cache root (i.e. ``Config.cache_dir``);
        the book is shelved under ``{cache_dir}/reader/``.
    :returns: The book's :class:`Manifest`.
    :raises ReaderError: On DRM, malformed/oversized EPUBs, an oversized
        spine, zero readable chapters, or a shelving timeout.
    :raises KavitaError: If the upstream fetch fails outright.
    """
    url = str(record.get("u", ""))
    key = _store_book_key(url)
    reader_dir = os.path.join(cache_dir, "reader")
    os.makedirs(reader_dir, exist_ok=True)

    existing = load_manifest(cache_dir, key)
    if existing is not None:
        return existing

    async with _lock_for(key):
        existing = load_manifest(cache_dir, key)
        if existing is not None:
            return existing

        start = time.monotonic()
        spool_path = os.path.join(reader_dir, f".spool-{os.getpid()}-{key}")
        try:
            try:
                spooled_bytes = await asyncio.wait_for(
                    _spool_epub(kc, url, spool_path, MAX_EPUB_BYTES), timeout=SHELVE_TIMEOUT
                )
            except TimeoutError as exc:
                raise ReaderError("Timed out downloading this book") from exc

            tmp_dir = os.path.join(reader_dir, f"{key}.tmp-{os.getpid()}")
            final_dir = os.path.join(reader_dir, key)
            _rmtree_ignore(tmp_dir)
            os.makedirs(os.path.join(tmp_dir, "chapters"))
            os.makedirs(os.path.join(tmp_dir, "images"))
            try:
                manifest = _extract_epub(spool_path, key, record, tmp_dir)
            except Exception:
                _rmtree_ignore(tmp_dir)
                raise

            try:
                os.rename(tmp_dir, final_dir)
            except OSError:
                winner = load_manifest(cache_dir, key)
                if winner is not None:
                    # Lost a cross-process race to another shelve of the
                    # same book: discard our copy and defer to whichever
                    # finished first (no corruption, no partial state
                    # left behind).
                    _rmtree_ignore(tmp_dir)
                    manifest = winner
                else:
                    # final_dir exists but has no valid manifest -- a
                    # stale/corrupt leftover from a crashed earlier
                    # attempt, not a live winner. Replace it with our
                    # freshly-built copy instead of surfacing a bare
                    # OSError as a 500.
                    _rmtree_ignore(final_dir)
                    try:
                        os.rename(tmp_dir, final_dir)
                    except OSError as exc:
                        _rmtree_ignore(tmp_dir)
                        raise ReaderError("Could not finish shelving this book") from exc
        finally:
            try:
                os.remove(spool_path)
            except OSError:
                pass

        elapsed_ms = int((time.monotonic() - start) * 1000)
        log.info(
            "shelved book_key=%s chapters=%d bytes=%d ms=%d",
            key, len(manifest.chapters), spooled_bytes, elapsed_ms,
        )
        # Enforce the reader-cache ceiling now that a fresh book has landed:
        # newly-shelved books are the ones that push the directory over the
        # limit, so pruning here keeps ``/cache/reader`` bounded oldest-first.
        # Best-effort — a full disk is not a reason to fail a completed shelve.
        prune_reader_cache(cache_dir, MAX_READER_CACHE_BYTES)
        return manifest


def load_manifest(cache_dir: str, book_key: str) -> Manifest | None:
    """Load a previously shelved book's manifest, if present and readable.

    :param cache_dir: The application cache root.
    :param book_key: The book's cache key.
    :returns: The :class:`Manifest`, or ``None`` if the book has not been
        shelved (or its manifest is missing/corrupt — treated the same as
        "not shelved yet" so a bad cache entry re-shelves transparently).
    """
    path = os.path.join(cache_dir, "reader", book_key, "manifest.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return _manifest_from_dict(data)
    except (FileNotFoundError, OSError, ValueError, KeyError, TypeError):
        return None


def load_chapter(cache_dir: str, book_key: str, i: int) -> list[str]:
    """Load chapter *i*'s sanitized HTML blocks for an already-shelved book.

    :param cache_dir: The application cache root.
    :param book_key: The book's cache key.
    :param i: The chapter's spine index (0-based).
    :returns: The chapter's ordered list of sanitized HTML block strings.
    :raises ReaderError: If the chapter file is missing, unreadable, or
        malformed.
    """
    path = os.path.join(cache_dir, "reader", book_key, "chapters", f"{i}.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        blocks = data["blocks"]
        if not isinstance(blocks, list):
            raise TypeError("malformed chapter cache entry")
        return [str(b) for b in blocks]
    except (FileNotFoundError, OSError, ValueError, KeyError, TypeError) as exc:
        raise ReaderError(f"Could not load chapter {i}") from exc


def prune_reader_cache(cache_dir: str, limit: int) -> None:
    """Delete the oldest shelved books until ``{cache_dir}/reader`` fits
    under *limit* bytes.

    Mirrors ``app.download._prune_cover_cache``, but deletes whole book
    directories (oldest manifest first) rather than individual files.
    Best-effort and never raises: a cache that cannot be pruned is a
    disk-space problem, not a reason to fail the request in flight.

    :param cache_dir: The application cache root.
    :param limit: Target maximum total size in bytes for
        ``{cache_dir}/reader``.
    """
    reader_dir = os.path.join(cache_dir, "reader")
    try:
        entries: list[tuple[float, int, str]] = []
        total = 0
        with os.scandir(reader_dir) as it:
            for item in it:
                if not item.is_dir() or item.name.startswith("."):
                    continue
                mtime = item.stat().st_mtime
                manifest_path = os.path.join(item.path, "manifest.json")
                if os.path.exists(manifest_path):
                    mtime = os.path.getmtime(manifest_path)
                size = 0
                for root, _dirs, files in os.walk(item.path):
                    for fname in files:
                        try:
                            size += os.path.getsize(os.path.join(root, fname))
                        except OSError:
                            pass
                entries.append((mtime, size, item.path))
                total += size
        if total <= limit:
            return
        for _mtime, size, path in sorted(entries):
            _rmtree_ignore(path)
            total -= size
            if total <= limit:
                return
    except OSError:
        pass


# -- Pagination and progress (SS-03) -----------------------------------------

#: Maps the ``rs_split`` cookie value to a target character count per part.
#: ``None`` (the ``"whole"`` setting) means "one part covering the whole
#: chapter" — see :func:`parts_for`.
SPLIT_TARGETS: dict[str, int | None] = {
    "small": 6000,
    "medium": 12000,
    "large": 24000,
    "whole": None,
}

#: The ``rs_split`` cookie value used when a device has not set one.
DEFAULT_SPLIT = "medium"


def parts_for(block_lengths: list[int], target_chars: int | None) -> list[tuple[int, int]]:
    """Greedily group a chapter's blocks into reading-length parts.

    Consecutive blocks are accumulated into a part until the running
    character total reaches *target_chars*, at which point a new part
    begins. A block is never split across parts — a single block longer
    than *target_chars* becomes its own singleton part. This keeps part
    boundaries stable across re-shelves (they depend only on block lengths,
    which sanitization reproduces deterministically) and total: every block
    in *block_lengths* appears in exactly one returned range.

    :param block_lengths: Character length of each block, in reading order.
    :param target_chars: Target characters per part, or ``None`` to return a
        single part covering every block (the ``"whole"`` split setting).
    :returns: Ordered ``(start, end_exclusive)`` block-index ranges.
    :rtype: list[tuple[int, int]]
    """
    if not block_lengths:
        return []
    if target_chars is None:
        return [(0, len(block_lengths))]

    parts: list[tuple[int, int]] = []
    start = 0
    running = 0
    for i, length in enumerate(block_lengths):
        running += length
        if running >= target_chars:
            parts.append((start, i + 1))
            start = i + 1
            running = 0
    if start < len(block_lengths):
        parts.append((start, len(block_lengths)))
    return parts


def part_containing(block_index: int, parts: list[tuple[int, int]]) -> int:
    """Find the 1-based part number holding *block_index*.

    Used to resume reading at the correct part after the ``rs_split``
    cookie changes and :func:`parts_for` regroups the same blocks
    differently. Out-of-range indexes clamp to the nearest valid part
    rather than raising, so a manifest that shrank (or a stale stored
    position) degrades to "start" or "end" instead of an error.

    :param block_index: 0-based index of the block to locate.
    :param parts: Ranges as returned by :func:`parts_for`.
    :returns: The 1-based part number containing *block_index*.
    :rtype: int
    """
    if not parts:
        return 1
    for i, (start, end) in enumerate(parts):
        if start <= block_index < end:
            return i + 1
    if block_index < parts[0][0]:
        return 1
    return len(parts)


def percent_of(manifest: Manifest, chapter: int, block: int) -> int:
    """Compute overall reading progress as a percentage.

    Progress is measured in characters, not blocks or chapters, so a few
    short chapters do not overstate how far along the reader is. The final
    block of the final chapter always reports 100, even if rounding of the
    character ratio would otherwise land just under it.

    :param manifest: The book's :class:`Manifest`.
    :param chapter: 0-based chapter index.
    :param block: 0-based block index within *chapter*.
    :returns: Percent complete, 0-100.
    :rtype: int
    """
    if manifest.total_chars <= 0 or not manifest.chapters:
        return 0
    chapter = max(0, min(chapter, len(manifest.chapters) - 1))
    meta = manifest.chapters[chapter]
    is_last_chapter = chapter == len(manifest.chapters) - 1
    is_last_block = block >= meta.blocks - 1
    if is_last_chapter and is_last_block:
        return 100
    chars_before: float = sum(c.chars for c in manifest.chapters[:chapter])
    # Approximate how far into this chapter *block* falls (per-block lengths
    # are not carried in the manifest, only per-chapter totals), so within a
    # chapter progress still advances block-by-block instead of jumping in
    # one chapter-sized step.
    if meta.blocks > 0:
        block_frac = max(0, min(block, meta.blocks - 1)) / meta.blocks
        chars_before += meta.chars * block_frac
    percent = int((chars_before / manifest.total_chars) * 100)
    return max(0, min(percent, 100))
