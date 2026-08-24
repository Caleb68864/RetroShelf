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
import html
import io
import json
import logging
import os
import posixpath
import re
import shutil
import time
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import unquote
from xml.etree.ElementTree import Element, tostring  # noqa: S405 - build/serialize only; all parsing goes through defusedxml
from xml.sax.saxutils import escape, quoteattr

from defusedxml.ElementTree import ParseError, fromstring

from .errors import PdfNoTextError, ReaderError, SsrfError
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

try:
    import pypdf as _pypdf
    _PYPDF_AVAILABLE = True
except ImportError:  # pragma: no cover - pypdf is a declared runtime dependency
    _PYPDF_AVAILABLE = False

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

# Sentinel prefixes this module emits (``{IMG:n}`` / ``{CH:i}`` / ``{FRAG:id}``).
# Book text or an ``alt`` value that happens to contain one of these literally
# must never be indistinguishable from a real, module-generated placeholder
# once it reaches the serve-time substitution step (``_substitute_placeholders``
# rewrites every one of these three forms) — so any occurrence coming from
# untrusted content is neutralized with an invisible break. ``{FRAG:`` belongs
# here for the same reason as the other two: a literal ``{FRAG:x}`` in book
# prose would otherwise be rewritten into a spurious in-book link URL at serve
# time (the charset guard keeps it from ever being an injection, but forging a
# placeholder is exactly what this defense exists to prevent).
_PLACEHOLDER_PREFIXES = ("{IMG:", "{CH:", "{FRAG:")
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
        frag: str | None = None
        if href:
            if href.startswith("#"):
                candidate = href[1:]
                if _VALID_ANCHOR_ID.match(candidate):
                    frag = candidate  # same-chapter footnote/anchor link
            else:
                link_index = resolve_link(href)
        inner = _render_children(
            element, depth=depth, resolve_image=resolve_image, resolve_link=resolve_link
        )
        if frag is not None:
            return f'<a href="{{FRAG:{frag}}}">{inner}</a>'
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


# An HTML id worth turning into an in-chapter footnote/anchor link. Restricted
# to the placeholder-safe charset (no quotes, angles, colon, slash or space) so
# a ``{FRAG:id}`` sentinel is always well-formed. Note the id itself is only
# ever used as an anchor-map lookup key — it never reaches the output HTML — so
# this bound is about sentinel hygiene, not injection. [footnotes]
_VALID_ANCHOR_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def sanitize_chapter(
    source: str | bytes,
    *,
    resolve_image: Callable[[str], int | None],
    resolve_link: Callable[[str], int | None],
    anchors: dict[str, int] | None = None,
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
    :param anchors: If provided, populated with ``{element_id: block_index}``
        for every valid-id element, so same-chapter fragment links
        (``#id`` → ``{FRAG:id}``) can be resolved to the part containing
        their target at serve time. Left untouched when ``None``.
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
        if not rendered.strip():
            continue
        if anchors is not None:
            block_index = len(blocks)
            for el in child.iter():
                el_id = el.get("id")
                if el_id and _VALID_ANCHOR_ID.match(el_id):
                    anchors.setdefault(el_id, block_index)
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

# Per-image source read cap. A single embedded chapter image never
# legitimately needs tens of megabytes; without a dedicated cap an image was
# read under the whole 120MB unpacked budget, so one crafted image entry could
# pull ~120MB into a transient decode buffer. An image larger than this is
# dropped (the book stays readable, just text-only for that image) rather than
# failing the whole book — the same "one bad image must not fail the book"
# contract the decode step already honours. Realistic covers/illustrations are
# a few MB at most. [SS-02]
MAX_IMAGE_SRC_BYTES = 16 * 1024 * 1024

# Ceiling on how many distinct image members shelving will attempt to read and
# decode. A hostile OPF can declare thousands of tiny image items (each
# resolving to a real zip member) purely to force that many decode attempts;
# a real book's image count is far lower. Mirrors MAX_SPINE_ITEMS bounding
# chapter work. [SS-02]
MAX_IMAGES = 2000

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
    :ivar toc: Hierarchical table of contents from the EPUB nav/NCX, as
        ``(depth, title, chapter_index)`` triples in reading order. Empty when
        the book has no nav/NCX (the reader then falls back to the flat
        spine-chapter list). [book-fidelity]
    :ivar kind: What the shelved item *is*, so the reader can tailor its chrome
        without re-parsing anything: ``"book"`` (the default — EPUB/HTML/PDF
        prose, page-turning by reading-length part) or ``"comic"`` (a CBZ, one
        image page per chapter, page-turning by whole page). Additive and
        back-compatible: a manifest written before this field existed loads as
        ``"book"``. [cbz-reader]
    """

    version: int
    book_key: str
    title: str
    author: str
    chapters: list[ChapterMeta]
    images: int
    total_chars: int
    created: float
    toc: list[tuple[int, str, int]] = field(default_factory=list)
    kind: str = "book"


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


_MAX_TOC_DEPTH = 3


def _toc_entries_from_nav(data: bytes, nav_dir: str) -> list[tuple[int, str, str]]:
    """Extract the *hierarchical* ToC from an EPUB3 nav document.

    Walks the first ``<nav epub:type="toc">``'s nested ``<ol>``/``<li>`` tree,
    yielding ``(depth, title, zip_path)`` in reading order — depth 0 at the top
    level, capped at :data:`_MAX_TOC_DEPTH`.

    :param data: The raw nav document bytes.
    :param nav_dir: The nav document's own zip-relative directory.
    :returns: Ordered ``(depth, title, path)`` triples; empty on parse failure.
    """
    try:
        root = fromstring(data)
    except (ParseError, ValueError):
        return []
    entries: list[tuple[int, str, str]] = []

    def walk_ol(ol: Element, depth: int) -> None:
        for li in ol:
            if _local_name(li.tag) != "li":
                continue
            anchor = next((e for e in li.iter() if _local_name(e.tag) == "a"), None)
            if anchor is not None:
                href = anchor.get("href")
                path = _norm_zip_path(nav_dir, href) if href else None
                text = " ".join("".join(anchor.itertext()).split())
                if path and text:
                    entries.append((min(depth, _MAX_TOC_DEPTH), text, path))
            child_ol = next((e for e in li if _local_name(e.tag) == "ol"), None)
            if child_ol is not None:
                walk_ol(child_ol, depth + 1)

    for nav_el in root.iter():
        if _local_name(nav_el.tag) != "nav":
            continue
        type_attr = next(
            (v for k, v in nav_el.attrib.items() if _local_name(k) == "type"), ""
        )
        if "toc" not in type_attr.split():
            continue
        top_ol = next((e for e in nav_el if _local_name(e.tag) == "ol"), None)
        if top_ol is not None:
            walk_ol(top_ol, 0)
        break
    return entries


def _toc_entries_from_ncx(data: bytes, ncx_dir: str) -> list[tuple[int, str, str]]:
    """Extract the hierarchical ToC from an EPUB2 NCX ``navMap``.

    ``navPoint`` nesting gives depth. Returns ``(depth, title, zip_path)`` in
    reading order, capped at :data:`_MAX_TOC_DEPTH`.

    :param data: The raw ``toc.ncx`` bytes.
    :param ncx_dir: The NCX's own zip-relative directory.
    :returns: Ordered ``(depth, title, path)`` triples; empty on parse failure.
    """
    try:
        root = fromstring(data)
    except (ParseError, ValueError):
        return []
    entries: list[tuple[int, str, str]] = []

    def walk_point(point: Element, depth: int) -> None:
        title_text = ""
        src: str | None = None
        children_points: list[Element] = []
        for child in point:
            cname = _local_name(child.tag)
            if cname == "navlabel":
                for text_el in child:
                    if _local_name(text_el.tag) == "text":
                        title_text = " ".join((text_el.text or "").split())
            elif cname == "content":
                src = child.get("src")
            elif cname == "navpoint":
                children_points.append(child)
        path = _norm_zip_path(ncx_dir, src) if src else None
        if path and title_text:
            entries.append((min(depth, _MAX_TOC_DEPTH), title_text, path))
        for cp in children_points:
            walk_point(cp, depth + 1)

    nav_map = next((e for e in root.iter() if _local_name(e.tag) == "navmap"), None)
    if nav_map is not None:
        for point in nav_map:
            if _local_name(point.tag) == "navpoint":
                walk_point(point, 0)
    return entries


def _build_toc(
    nav_toc: list[tuple[int, str, str]],
    ncx_toc: list[tuple[int, str, str]],
    chapter_index_by_path: dict[str, int],
) -> list[tuple[int, str, int]]:
    """Map an ordered nav/NCX ToC onto spine-chapter indices.

    Prefers the EPUB3 nav ToC, falling back to the NCX. Each entry's target
    path (already fragment-stripped by :func:`_norm_zip_path`) is resolved to
    the containing spine chapter; entries whose target is not a spine chapter
    are dropped. The result is normalized so its shallowest entries sit at
    depth 0.

    :returns: ``(depth, title, chapter_index)`` triples, or empty for a flat
        fallback ToC.
    """
    source = nav_toc or ncx_toc
    mapped: list[tuple[int, str, int]] = []
    for depth, title, path in source:
        idx = chapter_index_by_path.get(path)
        if idx is not None:
            mapped.append((depth, title, idx))
    if not mapped:
        return []
    base = min(d for d, _t, _i in mapped)
    return [(d - base, t, i) for d, t, i in mapped]


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


def _transcode_reader_image(raw: bytes) -> tuple[bytes, str] | None:
    """Decode *raw* image bytes into shelf-ready ``(served_bytes, content_type)``.

    Small JPEG/PNG images pass through untouched; anything larger than
    :data:`READER_IMAGE_MAX_EDGE` on its longest edge, or in any other format,
    is downscaled (aspect-preserving) and re-encoded as baseline JPEG — the
    same Pillow path :func:`app.download.stream_cover` uses for covers. Shared
    by EPUB image extraction (:func:`_extract_images`) and HTML image fetching
    (:func:`_fetch_html_images`) so both formats downscale identically.

    :param raw: The image's raw source bytes.
    :returns: ``(served_bytes, content_type)`` on success, or ``None`` when
        Pillow is unavailable or the bytes cannot be decoded — the caller then
        drops the image, since a single bad image must never fail the book.
    :rtype: tuple[bytes, str] or None
    """
    if not _PIL_AVAILABLE:
        return None
    try:
        img: _PILImage.Image = _PILImage.open(io.BytesIO(raw))
        img.load()
        fmt = img.format or ""
        w, h = img.size
        if fmt in _PASSTHROUGH_IMAGE_FORMATS and max(w, h) <= READER_IMAGE_MAX_EDGE:
            return raw, f"image/{fmt.lower()}"
        if max(w, h) > READER_IMAGE_MAX_EDGE:
            ratio = READER_IMAGE_MAX_EDGE / max(w, h)
            img = img.resize(
                (max(1, int(w * ratio)), max(1, int(h * ratio))), _PILImage.Resampling.LANCZOS
            )
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=READER_IMAGE_JPEG_QUALITY)
        return buf.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001 - a single bad image must not fail the book
        return None


def _write_reader_image(images_dir: str, idx: int, served_bytes: bytes, content_type: str) -> None:
    """Persist one transcoded page/illustration image plus its type sidecar.

    Every shelved image is stored as two files: the served bytes at
    ``images/{idx}`` and its content type at ``images/{idx}.ct``. Shared by
    EPUB illustration extraction (:func:`_extract_images`) and CBZ page
    extraction (:func:`_extract_cbz`) so both formats lay images out on disk
    identically. [cbz-reader]

    :param images_dir: The book's ``images/`` directory (already created).
    :param idx: The image's integer index (its ``{IMG:n}`` placeholder number).
    :param served_bytes: The transcoded bytes to serve for this image.
    :param content_type: The served bytes' content type (e.g. ``image/jpeg``).
    """
    with open(os.path.join(images_dir, str(idx)), "wb") as f:
        f.write(served_bytes)
    with open(os.path.join(images_dir, f"{idx}.ct"), "w", encoding="ascii") as f:
        f.write(content_type)


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
    attempts = 0
    for href, media_type, _props in manifest_items.values():
        if not media_type.startswith("image/"):
            continue
        path = _norm_zip_path(opf_dir, href)
        if path is None or path not in names or path in index_by_path:
            continue
        if attempts >= MAX_IMAGES:
            break
        attempts += 1
        raw = _read_image_member(zf, path, budget)
        if raw is None:
            continue
        transcoded = _transcode_reader_image(raw)
        if transcoded is None:
            continue
        out_bytes, out_ct = transcoded
        idx = len(index_by_path)
        _write_reader_image(images_dir, idx, out_bytes, out_ct)
        index_by_path[path] = idx
    return index_by_path


def _read_image_member(zf: zipfile.ZipFile, name: str, budget: _UnpackBudget) -> bytes | None:
    """Read image member *name*, capped at :data:`MAX_IMAGE_SRC_BYTES`.

    Unlike :func:`_read_member_capped` (which fails the whole book when a
    chapter/metadata member is over-sized), an image that overflows its cap
    or cannot be decompressed is *skipped* — the book stays readable, just
    text-only for that image. Every byte actually pulled off the
    decompression stream is charged against *budget* even on a skip, so a
    "many oversized images" archive still cannot read more than the global
    unpacked ceiling in total.

    :param zf: The open archive.
    :param name: A zip member name already validated by
        :func:`_is_safe_member` (or a fixed, known-safe path).
    :param budget: The shelving pass's running :class:`_UnpackBudget`.
    :returns: The image's decompressed bytes, or ``None`` to skip it.
    :raises ReaderError: Only via *budget* — i.e. when the cumulative
        unpacked total crosses the global ceiling (a genuine exhaustion
        signal that must fail the book).
    """
    chunks: list[bytes] = []
    total = 0
    too_large = False
    try:
        with zf.open(name) as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_IMAGE_SRC_BYTES:
                    too_large = True
                    break
                chunks.append(chunk)
    except (zipfile.BadZipFile, OSError, RuntimeError):
        # An unreadable/zip-encrypted image is skipped, not fatal: the OCF
        # encryption.xml check and the chapter reads already surface real DRM.
        budget.add(total)
        return None
    budget.add(total)  # charges even a skipped read; may raise on global overflow
    if too_large:
        return None
    return b"".join(chunks)


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
        "toc": [[d, t, i] for d, t, i in manifest.toc],
        "kind": manifest.kind,
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
        # ``toc`` is v2+; a v1 manifest (or a malformed entry) degrades to the
        # flat spine-chapter ToC, so tolerate absence and bad rows.
        toc=[
            (int(row[0]), str(row[1]), int(row[2]))
            for row in data.get("toc", [])
            if isinstance(row, (list, tuple)) and len(row) == 3
        ],
        # ``kind`` is additive: a manifest written before comics existed has no
        # ``kind`` and MUST load as an ordinary book, so any missing/unknown
        # value degrades to ``"book"`` rather than raising. [cbz-reader]
        kind="comic" if data.get("kind") == "comic" else "book",
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

        # -- chapter titles + hierarchical ToC: nav doc, then NCX --
        nav_titles: dict[str, str] = {}
        nav_toc: list[tuple[int, str, str]] = []
        for href, media_type, properties in manifest_items.values():
            if "nav" in properties.split():
                nav_path = _norm_zip_path(opf_dir, href)
                if nav_path and nav_path in names:
                    nav_bytes = _read_member_capped(zf, nav_path, MAX_METADATA_MEMBER_BYTES, budget)
                    nav_dir = posixpath.dirname(nav_path)
                    nav_titles = _titles_from_nav(nav_bytes, nav_dir)
                    nav_toc = _toc_entries_from_nav(nav_bytes, nav_dir)
                break
        ncx_titles: dict[str, str] = {}
        ncx_toc: list[tuple[int, str, str]] = []
        ncx_item = manifest_items.get(ncx_idref) if ncx_idref else None
        if ncx_item is None:
            ncx_item = next(
                (v for v in manifest_items.values() if v[1] == "application/x-dtbncx+xml"), None
            )
        if ncx_item is not None:
            ncx_path = _norm_zip_path(opf_dir, ncx_item[0])
            if ncx_path and ncx_path in names:
                ncx_bytes = _read_member_capped(zf, ncx_path, MAX_METADATA_MEMBER_BYTES, budget)
                ncx_dir = posixpath.dirname(ncx_path)
                ncx_titles = _titles_from_ncx(ncx_bytes, ncx_dir)
                ncx_toc = _toc_entries_from_ncx(ncx_bytes, ncx_dir)
        toc = _build_toc(nav_toc, ncx_toc, chapter_index_by_path)

        image_index_by_path = _extract_images(zf, names, manifest_items, opf_dir, tmp_dir, budget)

        chapters_meta: list[ChapterMeta] = []
        total_chars = 0
        for i, path in enumerate(chapter_paths):
            raw = _read_member_capped(zf, path, MAX_CHAPTER_BYTES, budget)
            chapter_dir = posixpath.dirname(path)
            anchors: dict[str, int] = {}
            blocks = sanitize_chapter(
                raw,
                resolve_image=_resolver(chapter_dir, image_index_by_path),
                resolve_link=_resolver(chapter_dir, chapter_index_by_path),
                anchors=anchors,
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
                json.dump({"blocks": blocks, "anchors": anchors}, f)
            chapters_meta.append(ChapterMeta(title=title, blocks=len(blocks), chars=chars))

    manifest = Manifest(
        version=2,
        book_key=key,
        title=str(record.get("t") or "") or "Untitled",
        author=str(record.get("a") or ""),
        chapters=chapters_meta,
        images=len(image_index_by_path),
        total_chars=total_chars,
        created=time.time(),
        toc=toc,
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


# ---------------------------------------------------------------------------
# HTML / plain-text shelving
# ---------------------------------------------------------------------------
#
# ``shelve_html_book`` reads a single upstream HTML document (a Project
# Gutenberg "Read online" edition, or any ``text/html`` OPDS acquisition) into
# the same shelved layout an EPUB produces, so every downstream reader function
# works unchanged. Pipeline: spool (capped) -> normalize tag-soup HTML to
# well-formed XHTML with the stdlib parser -> split into chapters on top-level
# h1/h2 -> sanitize each chapter through ``sanitize_chapter`` (the sole XSS
# wall) -> fetch + downscale referenced images through the same SSRF guard and
# Pillow path EPUB uses. A ``text/plain`` acquisition skips normalization and
# goes straight through the sanitizer's escaped-text fallback.

# HTML books are small; 16MB is generous while bounding a hostile or mistaken
# upstream. Spooled to disk in capped chunks like the EPUB path. [html-reader]
MAX_HTML_BYTES = 16 * 1024 * 1024

# Ceiling on start tags accepted from one document, so a "tag bomb" (millions
# of empty elements) cannot make normalization do unbounded work. A real book
# stays far below this. [html-reader]
MAX_HTML_ELEMENTS = 200_000

# How many distinct images one HTML book will attempt to fetch, and the total
# stored image bytes across them — each fetch is a live network round-trip, so
# these bound both work and disk. Per-image source stays capped at
# :data:`MAX_IMAGE_SRC_BYTES` (shared with EPUB). [html-reader]
MAX_HTML_IMAGES = 200
MAX_HTML_IMAGE_TOTAL_BYTES = 64 * 1024 * 1024

# HTML void elements: emitted self-closing, never pushed on the open-tag stack.
_HTML_VOID = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})

# Structurally transparent wrappers: ``<html>``/``<body>`` fold into the single
# synthetic root the normalizer emits (their flow children surface directly).
# The ``<head>`` subtree is skipped whole (handled separately). This is a
# layout decision, not a security one — ``sanitize_chapter`` remains the wall
# for everything that passes through (``script``/``style``/``iframe`` are
# emitted as elements precisely so the sanitizer is what drops them).
_HTML_TRANSPARENT = frozenset({"html", "body"})

# Attributes the normalizer preserves. ``sanitize_chapter`` drops every other
# attribute anyway and keeps only this same set (``img`` src/alt, ``a`` href,
# ``td``/``th`` spans) plus reading ``id`` for same-page anchor mapping.
# Emitting a minimal set keeps the intermediate XHTML well-formed without
# having to escape arbitrary tag-soup attribute names.
_HTML_KEEP_ATTRS = frozenset({"src", "alt", "href", "colspan", "rowspan", "id"})

# A valid XHTML element name the normalizer will emit. Anything else
# (namespaced or exotic tag-soup names) is unwrapped: its children and text
# survive, the tag does not — mirroring ``sanitize_chapter``'s treatment of
# unknown elements.
_HTML_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")

# Block-level starts that implicitly close an open ``<p>`` (HTML's
# optional-end-tag rule, the subset real books rely on), so paragraphs stay
# separate top-level blocks instead of nesting into one giant unpaginatable
# block when an author omits ``</p>``.
_HTML_CLOSES_P = frozenset({
    "p", "div", "section", "article", "main", "header", "footer", "aside",
    "figure", "figcaption", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol",
    "li", "dl", "table", "blockquote", "pre", "hr", "tr",
})

# XML 1.0 forbids most C0 control characters even when escaped; strip them from
# text/attribute content so the normalized document always parses. Tab, LF and
# CR are allowed.
_XML_BAD_CHARS = re.compile("[^\t\n\r\x20-\U0010ffff]")


def _scrub_xml(text: str) -> str:
    """Remove XML-1.0-illegal control characters from *text*.

    :param text: Untrusted text or attribute value.
    :returns: *text* with disallowed control characters removed.
    """
    return _XML_BAD_CHARS.sub("", text)


class _XHTMLNormalizer(HTMLParser):
    """Re-serialize tag-soup HTML into one well-formed XHTML ``<body>`` string.

    This is **not** a sanitizer and grants no trust. It only turns real-world
    HTML — unclosed tags, bare ``<br>``, undeclared entities — into a single
    well-formed XML fragment that :func:`sanitize_chapter`, the actual XSS
    wall, can parse and allowlist. Every element it emits is still subject to
    that function's drop/unwrap/attribute rules; ``script``/``style``/
    ``iframe`` are emitted faithfully so the sanitizer is demonstrably what
    removes them.

    Only non-security normalizations happen here: ``<html>``/``<body>`` fold
    into one synthetic root, the ``<head>`` subtree is skipped, unknown-named
    tags are unwrapped, loose top-level text is wrapped in ``<p>`` (so it
    survives as a block), and a small optional-end-tag rule keeps paragraphs
    separate. Collected image ``src`` values (document order, de-duplicated)
    are exposed via :attr:`img_srcs` for the caller to fetch.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = ["<body>"]
        self._stack: list[str] = []
        self._skip = 0  # >0 while inside a <head> subtree
        self._elements = 0
        self.img_srcs: list[str] = []
        self._seen_src: set[str] = set()

    def _close(self, tag: str) -> None:
        self._out.append(f"</{tag}>")

    def _implicit_close(self, tag: str) -> None:
        """Close open elements whose end tag HTML lets an author omit.

        :param tag: The start tag about to be emitted.
        """
        while self._stack:
            top = self._stack[-1]
            if top == "p" and tag in _HTML_CLOSES_P:
                self._close(self._stack.pop())
            elif top == "li" and tag == "li":
                self._close(self._stack.pop())
            elif top in ("td", "th") and tag in ("td", "th", "tr"):
                self._close(self._stack.pop())
            elif top == "tr" and tag == "tr":
                self._close(self._stack.pop())
            elif top in ("dt", "dd") and tag in ("dt", "dd"):
                self._close(self._stack.pop())
            else:
                break

    def _emit_attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        """Render the preserved subset of *attrs* as an XML attribute string.

        :param tag: The (lowercased) element name.
        :param attrs: HTMLParser's ``(name, value)`` attribute list.
        :returns: A leading-space-prefixed attribute string (possibly empty).
        """
        has_id = any((a[0] or "").lower() == "id" for a in attrs)
        pieces: list[str] = []
        for name, value in attrs:
            lname = (name or "").lower()
            # Old-style ``<a name="x">`` anchors become ``id`` so ``#x``
            # fragment links can resolve to them at serve time.
            if lname == "name" and tag == "a" and not has_id:
                lname = "id"
            if lname not in _HTML_KEEP_ATTRS or value is None:
                continue
            pieces.append(f" {lname}={quoteattr(_scrub_xml(value))}")
            if lname == "src" and tag == "img" and value not in self._seen_src:
                self._seen_src.add(value)
                self.img_srcs.append(value)
        return "".join(pieces)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._skip:
            if tag == "head":
                self._skip += 1
            return
        if tag == "head":
            self._skip = 1
            return
        if tag in _HTML_TRANSPARENT:
            return
        if self._elements >= MAX_HTML_ELEMENTS or not _HTML_NAME_RE.match(tag):
            return  # cap hit, or unknown/exotic name unwrapped (children survive)
        self._elements += 1
        self._implicit_close(tag)
        attr_str = self._emit_attrs(tag, attrs)
        if tag in _HTML_VOID:
            self._out.append(f"<{tag}{attr_str}/>")
        else:
            self._out.append(f"<{tag}{attr_str}>")
            self._stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._skip or tag in _HTML_TRANSPARENT:
            return
        if self._elements >= MAX_HTML_ELEMENTS or not _HTML_NAME_RE.match(tag):
            return
        self._elements += 1
        self._implicit_close(tag)
        self._out.append(f"<{tag}{self._emit_attrs(tag, attrs)}/>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip:
            if tag == "head":
                self._skip -= 1
            return
        if tag in _HTML_TRANSPARENT or tag in _HTML_VOID or tag not in self._stack:
            return
        # Close any elements left open above the match, then the match itself.
        while self._stack:
            top = self._stack.pop()
            self._close(top)
            if top == tag:
                break

    def handle_data(self, data: str) -> None:
        if self._skip or not data:
            return
        scrubbed = _scrub_xml(data)
        if not self._stack:
            # Loose text directly under <body>: inter-element whitespace is
            # dropped; real text is wrapped so it survives (sanitize renders
            # only element children of <body>, not its loose text).
            if scrubbed.strip():
                self._out.append(f"<p>{escape(scrubbed)}</p>")
            return
        self._out.append(escape(scrubbed))

    def result(self) -> str:
        """Close any still-open tags and return the ``<body>…</body>`` string."""
        while self._stack:
            self._close(self._stack.pop())
        self._out.append("</body>")
        return "".join(self._out)


def _normalize_html(source: str) -> tuple[str, list[str]]:
    """Normalize tag-soup *source* to a well-formed XHTML ``<body>`` string.

    :param source: The raw HTML document text.
    :returns: ``(xhtml, img_srcs)`` — the normalized body string and the
        de-duplicated image ``src`` values found, in document order.
    :raises ReaderError: If the stdlib HTML parser fails outright (rare; it is
        deliberately lenient).
    """
    parser = _XHTMLNormalizer()
    try:
        parser.feed(source)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - HTMLParser is lenient; guard anyway
        raise ReaderError("This page could not be read in the browser") from exc
    return parser.result(), parser.img_srcs


def _split_html_chapters(body: Element) -> list[tuple[int, str | None, list[Element]]]:
    """Group a body's top-level block elements into chapters at ``h1``/``h2``.

    A new chapter begins at each top-level ``h1`` (depth 0) or ``h2`` (depth 1);
    that heading's text titles the chapter it opens. Content before the first
    heading forms an untitled leading chapter. A document with no top-level
    heading yields a single untitled chapter. Reuses
    :func:`_top_level_block_elements` so a document wrapped in one
    ``<div>``/``<section>`` still splits on the headings inside it.

    :param body: The parsed ``<body>`` element.
    :returns: ``(depth, title_or_None, elements)`` groups in reading order.
    """
    chapters: list[tuple[int, str | None, list[Element]]] = []
    depth = 0
    title: str | None = None
    current: list[Element] = []
    for el in _top_level_block_elements(body):
        name = _local_name(el.tag)
        if name in ("h1", "h2"):
            if current:
                chapters.append((depth, title, current))
                current = []
            depth = 0 if name == "h1" else 1
            title = " ".join("".join(el.itertext()).split()) or None
        current.append(el)
    if current:
        chapters.append((depth, title, current))
    return chapters


def _wrap_body(elements: list[Element]) -> str:
    """Serialize *elements* as the children of a fresh ``<body>`` XML string.

    :param elements: Top-level block elements for one chapter.
    :returns: A ``<body>…</body>`` XML string ready for :func:`sanitize_chapter`.
    """
    root = Element("body")
    for el in elements:
        root.append(el)
    return tostring(root, encoding="unicode")


def _html_chapter_title(title: str | None, index: int, record: dict) -> str:
    """Pick a display title for HTML chapter *index*.

    :param title: The chapter's heading text, or ``None`` for an untitled group.
    :param index: The chapter's 0-based position.
    :param record: The book record (its ``t`` titles the leading chapter).
    :returns: A non-empty display title.
    """
    if title:
        return title
    if index == 0:
        return str(record.get("t") or "") or "Beginning"
    return f"Section {index + 1}"


async def _fetch_capped_image(kc: KavitaClient, url: str) -> bytes | None:
    """Fetch *url* into memory, capped at :data:`MAX_IMAGE_SRC_BYTES`.

    *url* must already be an SSRF-validated absolute URL (see
    :func:`_fetch_html_images`); ``open_stream`` re-validates it. Any failure
    — network error, over-cap body, malformed stream — returns ``None`` so the
    image is dropped without failing the book.

    :param kc: Kavita client; its ``open_stream`` applies the SSRF guard.
    :param url: The absolute, already-validated image URL.
    :returns: The image's raw bytes, or ``None`` to drop it.
    """
    try:
        resp = await kc.open_stream(url)
    except Exception:  # noqa: BLE001 - a single bad image must not fail the book
        return None
    total = 0
    chunks: list[bytes] = []
    try:
        declared = resp.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_IMAGE_SRC_BYTES:
            return None
        async for chunk in resp.aiter_raw():
            total += len(chunk)
            if total > MAX_IMAGE_SRC_BYTES:
                return None
            chunks.append(chunk)
    except Exception:  # noqa: BLE001 - a single bad image must not fail the book
        return None
    finally:
        await resp.aclose()
    return b"".join(chunks)


async def _fetch_html_images(
    kc: KavitaClient, book_url: str, srcs: list[str], tmp_dir: str,
) -> dict[str, int]:
    """Fetch, SSRF-guard, and downscale the images an HTML book references.

    Each raw ``src`` is resolved against *book_url* through the SAME SSRF guard
    every other upstream fetch uses (:meth:`KavitaClient.resolve_url`); a
    foreign-origin or malformed ``src`` raises :class:`SsrfError` and the image
    is dropped **without being fetched**. Successful fetches are downscaled by
    the shared :func:`_transcode_reader_image` and written as
    ``images/{n}`` + ``images/{n}.ct``, exactly like EPUB images. A no-op
    (empty map) when Pillow is unavailable — the book is then text-only and
    every ``img`` placeholder is dropped.

    :param kc: Kavita client used to resolve and fetch each image.
    :param book_url: The book's own URL, the base for relative ``src`` values.
    :param srcs: De-duplicated image ``src`` strings, in document order.
    :param tmp_dir: The in-progress shelving directory; images land under
        ``{tmp_dir}/images/``.
    :returns: Map of original ``src`` string to stored image index.
    """
    index_by_src: dict[str, int] = {}
    if not _PIL_AVAILABLE:
        return index_by_src
    images_dir = os.path.join(tmp_dir, "images")
    total_bytes = 0
    for src in srcs:
        if len(index_by_src) >= MAX_HTML_IMAGES or total_bytes >= MAX_HTML_IMAGE_TOTAL_BYTES:
            break
        try:
            abs_url = kc.resolve_url(src, base=book_url)
        except SsrfError:
            continue  # foreign-origin / malformed src: never fetched, dropped
        raw = await _fetch_capped_image(kc, abs_url)
        if raw is None:
            continue
        transcoded = _transcode_reader_image(raw)
        if transcoded is None:
            continue
        out_bytes, out_ct = transcoded
        idx = len(index_by_src)
        with open(os.path.join(images_dir, str(idx)), "wb") as f:
            f.write(out_bytes)
        with open(os.path.join(images_dir, f"{idx}.ct"), "w", encoding="ascii") as f:
            f.write(out_ct)
        index_by_src[src] = idx
        total_bytes += len(out_bytes)
    return index_by_src


def _sanitize_html_chapters(
    body: Element, resolve_image: Callable[[str], int | None],
) -> list[tuple[int, str | None, list[str], dict[str, int]]]:
    """Split *body* into chapters and sanitize each into stored blocks.

    Every chapter's element group is re-serialized and passed through
    :func:`sanitize_chapter` — the single XSS wall — so nothing from the
    upstream document reaches served HTML unallowlisted. In-document links
    (``<a href="…">``) resolve to ``None`` (unwrapped to text): a single HTML
    file has no sibling chapters to link to, and same-page ``#fragment`` links
    are handled inside the sanitizer.

    :param body: The parsed, normalized ``<body>`` element.
    :param resolve_image: Maps an ``img`` ``src`` to its stored image index.
    :returns: ``(depth, title, blocks, anchors)`` per non-empty chapter.
    """
    def resolve_link(_href: str) -> int | None:
        return None

    chapters: list[tuple[int, str | None, list[str], dict[str, int]]] = []
    for depth, title, elements in _split_html_chapters(body):
        anchors: dict[str, int] = {}
        blocks = sanitize_chapter(
            _wrap_body(elements),
            resolve_image=resolve_image,
            resolve_link=resolve_link,
            anchors=anchors,
        )
        if blocks:
            chapters.append((depth, title, blocks, anchors))
    return chapters


async def _extract_html_book(
    kc: KavitaClient, spool_path: str, key: str, record: dict, tmp_dir: str,
) -> Manifest:
    """Parse the spooled HTML/text document into *tmp_dir*'s shelved form.

    HTML is normalized (:func:`_normalize_html`), its images fetched and
    guarded (:func:`_fetch_html_images`), then split and sanitized
    (:func:`_sanitize_html_chapters`). A ``text/plain`` document skips
    normalization and images and goes straight through
    :func:`sanitize_chapter`'s escaped-text fallback as one chapter. Produces
    the identical :class:`Manifest` + ``chapters/*.json`` shape as
    :func:`_extract_epub`.

    :param kc: Kavita client, used to fetch images (HTML only).
    :param spool_path: Path to the fully-downloaded document on disk.
    :param key: The book's cache key (``Manifest.book_key``).
    :param record: The caller's book record; ``t``/``a``/``m``/``u`` are read.
        No URL from *record* is ever written to disk.
    :param tmp_dir: The in-progress shelving directory (with empty
        ``chapters/`` and ``images/`` subdirectories).
    :returns: The completed :class:`Manifest` (also written to
        ``manifest.json``).
    :raises ReaderError: On an unparseable document or one with no readable text.
    """
    with open(spool_path, "rb") as f:
        source = f.read().decode("utf-8", errors="replace")

    if "html" in str(record.get("m", "")).lower():
        normalized, img_srcs = _normalize_html(source)
        image_index_by_src = await _fetch_html_images(
            kc, str(record.get("u", "")), img_srcs, tmp_dir
        )
        try:
            root = fromstring(normalized)
        except (ParseError, ValueError) as exc:
            raise ReaderError("This page could not be read in the browser") from exc
        chapters = _sanitize_html_chapters(
            _find_body(root), lambda s: image_index_by_src.get(s)
        )
        images = len(image_index_by_src)
    else:
        # Plain text: one chapter via the sanitizer's escaped-text fallback
        # (blank-line-split <p> blocks). No images, no headings.
        anchors: dict[str, int] = {}
        blocks = sanitize_chapter(
            source, resolve_image=lambda _s: None, resolve_link=lambda _h: None,
            anchors=anchors,
        )
        chapters = (
            [(0, str(record.get("t") or "") or None, blocks, anchors)] if blocks else []
        )
        images = 0

    chapters_meta: list[ChapterMeta] = []
    toc: list[tuple[int, str, int]] = []
    total_chars = 0
    for i, (depth, title, blocks, anchors) in enumerate(chapters):
        chars = sum(len(b) for b in blocks)
        total_chars += chars
        ctitle = _html_chapter_title(title, i, record)
        with open(os.path.join(tmp_dir, "chapters", f"{i}.json"), "w", encoding="utf-8") as f:
            json.dump({"blocks": blocks, "anchors": anchors}, f)
        chapters_meta.append(ChapterMeta(title=ctitle, blocks=len(blocks), chars=chars))
        toc.append((depth, ctitle, i))

    if not chapters_meta:
        raise ReaderError("This page has no readable text")

    manifest = Manifest(
        version=2,
        book_key=key,
        title=str(record.get("t") or "") or "Untitled",
        author=str(record.get("a") or ""),
        chapters=chapters_meta,
        images=images,
        total_chars=total_chars,
        created=time.time(),
        toc=toc,
    )
    with open(os.path.join(tmp_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(_manifest_to_dict(manifest), f)
    return manifest


#: Extract callback signature for :func:`_shelve_record`. Given the spooled
#: source file, the book key, the caller's record, and the (already-created)
#: in-progress ``tmp_dir``, it parses/sanitizes the book into ``tmp_dir`` and
#: returns the completed :class:`Manifest`. Awaitable so an HTML book can fetch
#: its images over the network (:func:`_extract_html_book`); the EPUB extractor
#: does only local work and adapts through :func:`_extract_epub_shelf`.
_ShelfExtractor = Callable[[KavitaClient, str, str, dict, str], Awaitable["Manifest"]]


async def _shelve_record(
    kc: KavitaClient, record: dict, cache_dir: str, *,
    spool_cap: int, extract: _ShelfExtractor,
) -> Manifest:
    """Spool *record*'s upstream file and shelve it via *extract*.

    The format-agnostic shelving skeleton shared by :func:`shelve_book`
    (EPUB) and :func:`shelve_html_book` (HTML/text): existing-manifest fast
    path, per-``book_key`` :class:`asyncio.Lock`, capped disk spool through
    the SSRF-guarded ``open_stream`` under a bounded timeout, a
    ``{book_key}.tmp-{pid}`` build directory made visible only by an atomic
    ``os.rename`` (so a reader never sees a half-written book and a crash
    leaves no visible partial state), cross-process race resolution, spool
    cleanup, one masked INFO log line, and oldest-first cache pruning. The
    *only* per-format differences are the spool size cap and the *extract*
    callback. No upstream URL is ever written to any cache file.

    :param kc: Kavita client; every fetch goes through its SSRF guard.
    :param record: A book record with at least a ``u`` (acquisition URL) key;
        ``t`` (title) and ``a`` (author) feed the manifest when present.
    :param cache_dir: The application cache root; the book is shelved under
        ``{cache_dir}/reader/``.
    :param spool_cap: Maximum spooled body size in bytes for this format.
    :param extract: The format-specific parser (see :data:`_ShelfExtractor`).
    :returns: The book's :class:`Manifest`.
    :raises ReaderError: On malformed/oversized input, a shelving timeout, or
        any format-specific failure raised by *extract*.
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
                    _spool_epub(kc, url, spool_path, spool_cap), timeout=SHELVE_TIMEOUT
                )
            except TimeoutError as exc:
                raise ReaderError("Timed out downloading this book") from exc

            tmp_dir = os.path.join(reader_dir, f"{key}.tmp-{os.getpid()}")
            final_dir = os.path.join(reader_dir, key)
            _rmtree_ignore(tmp_dir)
            os.makedirs(os.path.join(tmp_dir, "chapters"))
            os.makedirs(os.path.join(tmp_dir, "images"))
            try:
                manifest = await extract(kc, spool_path, key, record, tmp_dir)
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


async def _extract_epub_shelf(
    kc: KavitaClient, spool_path: str, key: str, record: dict, tmp_dir: str,
) -> Manifest:
    """Awaitable adapter making the synchronous EPUB extractor a
    :data:`_ShelfExtractor`.

    EPUB parsing reads only the local spool file (its images come from the
    zip), so it does no network I/O and *kc* is unused; the parse stays
    synchronous — this wrapper exists solely to satisfy the awaitable
    extractor contract that HTML shelving needs for remote image fetches.

    :param kc: Unused (EPUB images come from the spooled archive).
    :param spool_path: Path to the fully-downloaded EPUB on disk.
    :param key: The book's cache key.
    :param record: The caller's book record.
    :param tmp_dir: The in-progress shelving directory.
    :returns: The completed :class:`Manifest`.
    """
    return _extract_epub(spool_path, key, record, tmp_dir)


async def shelve_book(kc: KavitaClient, record: dict, cache_dir: str) -> Manifest:
    """Shelve *record*'s EPUB into the reader cache, or return its existing
    :class:`Manifest` if already shelved.

    First open is expensive (download + parse + sanitize); every subsequent
    call for the same book is a cache hit with zero upstream traffic. See
    :func:`_shelve_record` for the shared shelving mechanics.

    :param kc: Kavita client used to spool the upstream EPUB. Every fetch goes
        through its SSRF guard.
    :param record: A book record with at least a ``u`` (acquisition URL) key;
        ``t`` (title) and ``a`` (author) are used for the manifest when present.
    :param cache_dir: The application cache root (i.e. ``Config.cache_dir``);
        the book is shelved under ``{cache_dir}/reader/``.
    :returns: The book's :class:`Manifest`.
    :raises ReaderError: On DRM, malformed/oversized EPUBs, an oversized spine,
        zero readable chapters, or a shelving timeout.
    :raises KavitaError: If the upstream fetch fails outright.
    """
    return await _shelve_record(
        kc, record, cache_dir, spool_cap=MAX_EPUB_BYTES, extract=_extract_epub_shelf
    )


async def shelve_html_book(kc: KavitaClient, record: dict, cache_dir: str) -> Manifest:
    """Shelve *record*'s HTML (or plain-text) document into the reader cache.

    The HTML counterpart to :func:`shelve_book`: it produces the identical
    :class:`Manifest` + ``chapters/*.json`` + ``images/*`` layout, so every
    downstream reader function (:func:`load_manifest`, :func:`load_chapter`,
    :func:`parts_for`, :func:`percent_of`, :func:`search_book`, anchors)
    works unchanged. A single upstream HTML document is normalized to
    well-formed XHTML, split into chapters on its top-level ``h1``/``h2``
    headings, sanitized through :func:`sanitize_chapter` (the one XSS wall),
    and its images are fetched — each re-validated through the SSRF guard —
    and downscaled exactly like EPUB images. See :func:`_extract_html_book`.

    :param kc: Kavita client used to spool the document and fetch its images.
        Every fetch goes through its SSRF guard.
    :param record: A book record with at least a ``u`` (acquisition URL) key;
        ``t`` (title) and ``a`` (author) are used for the manifest when present.
    :param cache_dir: The application cache root; the book is shelved under
        ``{cache_dir}/reader/``.
    :returns: The book's :class:`Manifest`.
    :raises ReaderError: On an oversized/unparseable document, one with no
        readable text, or a shelving timeout.
    :raises KavitaError: If the upstream fetch fails outright.
    """
    return await _shelve_record(
        kc, record, cache_dir, spool_cap=MAX_HTML_BYTES, extract=_extract_html_book
    )


# ---------------------------------------------------------------------------
# PDF text-reflow shelving
# ---------------------------------------------------------------------------
#
# ``shelve_pdf_book`` turns a text-layer PDF into the same shelved layout an
# EPUB/HTML book produces, so every downstream reader function works unchanged.
# This is v1: TEXT REFLOW ONLY — the PDF's text layer and outline are
# extracted; page images are NOT rendered. A scanned/image-only PDF (no text
# layer) raises :class:`PdfNoTextError`, which the route turns into a friendly
# "use Open PDF" page. Pipeline: spool (capped) -> ``pypdf`` open (DRM/corrupt
# guarded) -> per-page ``extract_text`` -> chapter boundaries from the document
# outline (or page-grouped fallback) -> per-chapter text run through
# :func:`_escaped_text_fallback`, so extracted text is HTML-ESCAPED (never
# re-parsed as markup) before it reaches the single ``| safe`` render seam.

# A PDF download is a few tens of MB at most; 80MB mirrors the EPUB cap while
# bounding a hostile or mistaken upstream. Spooled to disk in capped chunks by
# the shared skeleton. [pdf-reader]
MAX_PDF_BYTES = 80 * 1024 * 1024

# A document with thousands of pages is not a book we reflow in a browser;
# capping bounds per-page extraction work. Realistic books stay well under. A
# PDF over this cap fails with a friendly error steering to Open PDF. [pdf-reader]
MAX_PDF_PAGES = 5000

# Ceiling on total extracted (escaped) text characters across the whole book,
# so a PDF whose text layer expands to hundreds of MB cannot exhaust memory or
# the reader-cache budget on its own. [pdf-reader]
MAX_PDF_TEXT_CHARS = 40 * 1024 * 1024

# A book whose entire text layer extracts to fewer than this many non-space
# characters is treated as having NO usable text (scanned/image-only), and the
# reflow reader defers to Open PDF via :class:`PdfNoTextError`. Small enough
# that any genuinely text-bearing book clears it. [pdf-reader]
PDF_MIN_TEXT_CHARS = 16

# When a PDF has no usable outline, its pages are grouped into chapters this
# many pages at a time, so a long book still gets a navigable (bounded) ToC and
# resumable chapters instead of one monolithic chapter. A book of at most this
# many pages becomes a single chapter titled by the book. [pdf-reader]
PDF_PAGES_PER_CHAPTER = 10

# Hard cap on chapters a PDF outline can produce, mirroring
# :data:`MAX_SPINE_ITEMS`: a hostile outline with thousands of entries cannot
# make shelving build an unbounded number of chapter files. [pdf-reader]
MAX_PDF_CHAPTERS = 500


def _flatten_pdf_outline(
    reader: "_pypdf.PdfReader", outline: object, depth: int = 0,
) -> list[tuple[int, str, int]]:
    """Flatten a ``pypdf`` outline tree into ``(depth, title, page_index)``.

    ``pypdf`` returns the outline as a list in reading order where a nested
    list holds the children of the entry immediately before it. Each leaf is a
    destination with a ``.title`` resolvable to a 0-based page index via
    :meth:`pypdf.PdfReader.get_destination_page_number`. Entries whose page or
    title cannot be resolved are skipped (a broken bookmark must not fail the
    book). Depth is capped at :data:`_MAX_TOC_DEPTH`.

    :param reader: The open ``pypdf`` reader (for page-number resolution).
    :param outline: A ``reader.outline`` node (list) to walk.
    :param depth: Current outline nesting depth.
    :returns: Ordered ``(depth, title, page_index)`` triples.
    """
    entries: list[tuple[int, str, int]] = []
    if not isinstance(outline, list):
        return entries
    for item in outline:
        if isinstance(item, list):
            entries.extend(_flatten_pdf_outline(reader, item, depth + 1))
            continue
        try:
            page = reader.get_destination_page_number(item)
            title = " ".join(str(item.title or "").split())
        except Exception:  # noqa: BLE001 - a broken bookmark must not fail the book
            continue
        if title and isinstance(page, int) and page >= 0:
            entries.append((min(depth, _MAX_TOC_DEPTH), title, page))
    return entries


def _pdf_chapter_ranges(
    entries: list[tuple[int, str, int]], num_pages: int, record: dict,
) -> tuple[list[tuple[str, int, int]], list[tuple[int, str, int]]]:
    """Plan chapter page-ranges and the hierarchical ToC for a PDF.

    When the outline yields entries, each distinct start page begins a chapter
    (pages before the first entry form a leading chapter); every outline entry
    maps to the chapter it opens, giving a real nested ToC. With no usable
    outline, pages are grouped in fixed :data:`PDF_PAGES_PER_CHAPTER` runs (a
    single chapter titled by the book when the whole document fits one group).

    :param entries: Flattened outline, as from :func:`_flatten_pdf_outline`.
    :param num_pages: Total page count of the document.
    :param record: The book record (its ``t`` titles the leading/only chapter).
    :returns: ``(chapters, toc)`` where *chapters* is an ordered list of
        ``(title, start_page, end_page_exclusive)`` and *toc* is
        ``(depth, title, chapter_index)`` in reading order.
    """
    book_title = str(record.get("t") or "") or "Untitled"
    valid = [(d, t, p) for d, t, p in entries if 0 <= p < num_pages]
    if valid:
        starts = sorted({p for _d, _t, p in valid})
        if starts[0] > 0:
            starts.insert(0, 0)  # leading matter before the first bookmark
        starts = starts[:MAX_PDF_CHAPTERS]
        chapter_of_page = {p: i for i, p in enumerate(starts)}
        title_for_start: dict[int, str] = {}
        for _d, title, page in valid:
            title_for_start.setdefault(page, title)
        chapters: list[tuple[str, int, int]] = []
        for i, sp in enumerate(starts):
            ep = starts[i + 1] if i + 1 < len(starts) else num_pages
            title = title_for_start.get(sp) or (book_title if sp == 0 else f"Section {i + 1}")
            chapters.append((title, sp, ep))
        toc: list[tuple[int, str, int]] = []
        for depth, title, page in valid:
            ci = chapter_of_page.get(page)
            if ci is not None:
                toc.append((depth, title, ci))
        if toc:
            base = min(d for d, _t, _i in toc)
            toc = [(d - base, t, i) for d, t, i in toc]
        return chapters, toc

    # No outline: fixed page-run chapters (or one chapter for a short book).
    if num_pages <= PDF_PAGES_PER_CHAPTER:
        return [(book_title, 0, num_pages)], [(0, book_title, 0)]
    chapters = []
    toc = []
    for ci, start in enumerate(range(0, num_pages, PDF_PAGES_PER_CHAPTER)):
        end = min(start + PDF_PAGES_PER_CHAPTER, num_pages)
        title = f"Pages {start + 1}–{end}"
        chapters.append((title, start, end))
        toc.append((0, title, ci))
        if len(chapters) >= MAX_PDF_CHAPTERS:
            break
    return chapters, toc


def _extract_pdf(spool_path: str, key: str, record: dict, tmp_dir: str) -> Manifest:
    """Parse the spooled PDF at *spool_path* into *tmp_dir*'s shelved form.

    Opens the PDF with ``pypdf`` (guarding malformed/encrypted files into a
    friendly :class:`ReaderError`), extracts each page's text layer, and groups
    pages into chapters by the document outline (:func:`_pdf_chapter_ranges`).
    Every chapter's text is escaped and split into ``<p>`` blocks by
    :func:`_escaped_text_fallback` — the extracted text is treated as PLAIN
    TEXT and never re-parsed as markup, so a page whose text literally contains
    ``<script>`` renders as visible characters. Produces the identical
    :class:`Manifest` + ``chapters/*.json`` layout as :func:`_extract_epub`
    (v1: no page images).

    :param spool_path: Path to the fully-downloaded PDF file on disk.
    :param key: The book's cache key (``Manifest.book_key``).
    :param record: The caller's book record; only ``t``/``a`` are read.
    :param tmp_dir: The in-progress shelving directory (with empty
        ``chapters/`` and ``images/`` subdirectories).
    :returns: The completed :class:`Manifest` (also written to ``manifest.json``).
    :raises PdfNoTextError: If the document has no extractable text layer.
    :raises ReaderError: On an encrypted, malformed, or oversized PDF.
    """
    if not _PYPDF_AVAILABLE:  # pragma: no cover - pypdf is a declared dependency
        raise ReaderError("This PDF can't be read in the browser — use Open PDF instead")

    try:
        with open(spool_path, "rb") as fh:
            reader = _pypdf.PdfReader(fh)
            if reader.is_encrypted:
                raise ReaderError(
                    "This PDF is protected and can't be read in the browser — use Open PDF"
                )
            num_pages = len(reader.pages)
            if num_pages == 0:
                raise ReaderError("This PDF has no pages")
            if num_pages > MAX_PDF_PAGES:
                raise ReaderError(f"This PDF has too many pages (limit {MAX_PDF_PAGES})")

            page_texts: list[str] = []
            total_text = 0
            for page in reader.pages:
                try:
                    text = page.extract_text() or ""
                except Exception:  # noqa: BLE001 - one bad page must not fail the book
                    text = ""
                total_text += len(text)
                if total_text > MAX_PDF_TEXT_CHARS:
                    raise ReaderError("This PDF has more text than can be read here")
                page_texts.append(text)

            outline_entries = _flatten_pdf_outline(reader, reader.outline)
    except ReaderError:
        raise
    except Exception as exc:  # noqa: BLE001 - any pypdf failure -> friendly error
        raise ReaderError("This does not look like a readable PDF file") from exc

    if sum(len(t.strip()) for t in page_texts) < PDF_MIN_TEXT_CHARS:
        # No usable text layer at all: a scanned / image-only PDF. Signal the
        # route to steer the reader to Open PDF rather than reflow nothing.
        raise PdfNoTextError("This PDF has no readable text layer")

    chapter_ranges, toc = _pdf_chapter_ranges(outline_entries, num_pages, record)

    chapters_meta: list[ChapterMeta] = []
    total_chars = 0
    for i, (title, start_page, end_page) in enumerate(chapter_ranges):
        # Join the chapter's pages with a blank line so each page becomes its
        # own paragraph block at minimum (giving pagination something to split).
        chapter_text = "\n\n".join(page_texts[start_page:end_page])
        blocks = _escaped_text_fallback(chapter_text)
        chars = sum(len(b) for b in blocks)
        total_chars += chars
        with open(os.path.join(tmp_dir, "chapters", f"{i}.json"), "w", encoding="utf-8") as f:
            json.dump({"blocks": blocks, "anchors": {}}, f)
        chapters_meta.append(ChapterMeta(title=title, blocks=len(blocks), chars=chars))

    manifest = Manifest(
        version=2,
        book_key=key,
        title=str(record.get("t") or "") or "Untitled",
        author=str(record.get("a") or ""),
        chapters=chapters_meta,
        images=0,  # v1: text reflow only, no page images
        total_chars=total_chars,
        created=time.time(),
        toc=toc,
    )
    with open(os.path.join(tmp_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(_manifest_to_dict(manifest), f)
    return manifest


async def _extract_pdf_shelf(
    kc: KavitaClient, spool_path: str, key: str, record: dict, tmp_dir: str,
) -> Manifest:
    """Awaitable adapter making the synchronous PDF extractor a
    :data:`_ShelfExtractor`.

    PDF text extraction reads only the local spool file (v1 has no images), so
    it does no network I/O and *kc* is unused; this wrapper exists solely to
    satisfy the awaitable extractor contract the shared skeleton expects.

    :param kc: Unused (PDF reflow reads only the spooled file).
    :param spool_path: Path to the fully-downloaded PDF on disk.
    :param key: The book's cache key.
    :param record: The caller's book record.
    :param tmp_dir: The in-progress shelving directory.
    :returns: The completed :class:`Manifest`.
    """
    return _extract_pdf(spool_path, key, record, tmp_dir)


async def shelve_pdf_book(kc: KavitaClient, record: dict, cache_dir: str) -> Manifest:
    """Shelve *record*'s PDF (text reflow) into the reader cache.

    The PDF counterpart to :func:`shelve_book`: it produces the identical
    :class:`Manifest` + ``chapters/*.json`` layout, so every downstream reader
    function (:func:`load_manifest`, :func:`load_chapter`, :func:`parts_for`,
    :func:`percent_of`, :func:`search_book`, positions, bookmarks) works
    unchanged. The PDF's text layer is extracted per page, chaptered by its
    document outline (or page-grouped when it has none), and each chapter's
    text is escaped through :func:`_escaped_text_fallback` — never re-parsed as
    markup — before reaching the one ``| safe`` render seam. A scanned /
    image-only PDF (no text layer) raises :class:`PdfNoTextError` so the route
    can steer the reader to the native Open-PDF path. See :func:`_extract_pdf`.

    Unlike EPUB/HTML, the PDF book page keeps BOTH this "Read here" entry point
    and the existing "Open PDF" download button (the layout-preserving native
    inline view / Copy-to-Books flow); the two are complementary.

    :param kc: Kavita client used to spool the upstream PDF. Every fetch goes
        through its SSRF guard.
    :param record: A book record with at least a ``u`` (acquisition URL) key;
        ``t`` (title) and ``a`` (author) are used for the manifest when present.
    :param cache_dir: The application cache root; the book is shelved under
        ``{cache_dir}/reader/``.
    :returns: The book's :class:`Manifest`.
    :raises PdfNoTextError: If the PDF has no extractable text layer.
    :raises ReaderError: On an encrypted, malformed, or oversized PDF, or a
        shelving timeout.
    :raises KavitaError: If the upstream fetch fails outright.
    """
    return await _shelve_record(
        kc, record, cache_dir, spool_cap=MAX_PDF_BYTES, extract=_extract_pdf_shelf
    )


# ---------------------------------------------------------------------------
# CBZ comic shelving
# ---------------------------------------------------------------------------
#
# ``shelve_cbz_book`` turns a CBZ (a plain zip of page images) into the same
# shelved layout an EPUB/HTML/PDF book produces, so every downstream reader
# function (:func:`load_manifest`, :func:`load_chapter`, :func:`parts_for`,
# positions, bookmarks, prune) works unchanged. The reading model is what makes
# comics nearly free: each page image becomes ONE chapter whose single block is
# exactly ``<img src="{IMG:n}"/>`` — our own integer-indexed markup, never any
# comic-supplied text or markup. Page-turning is the existing chapter Prev/Next;
# positions and bookmarks are per-page; ``percent`` tracks pages read. The only
# things that differ from a prose book are ``Manifest.kind == "comic"`` and the
# reader chrome the route selects on it. Pipeline: spool (capped) -> stdlib
# ``zipfile`` open from the spool -> select + natural-sort image members ->
# per-page transcode through the shared :func:`_transcode_reader_image` Pillow
# path -> one chapter + one image per decodable page.

# A comic is BIG — hundreds of full-bleed pages — so its spool cap is far larger
# than a prose book's. This is safe: like every other format the body is spooled
# straight to DISK in capped chunks, never buffered whole in RAM. [cbz-reader]
MAX_CBZ_BYTES = 300 * 1024 * 1024

# Ceiling on pages (== chapters == stored images) one comic yields. A real comic
# issue is tens of pages; a fat omnibus a few hundred. Mirrors MAX_SPINE_ITEMS
# bounding chapter work — a zip with thousands of image members cannot make
# shelving build an unbounded number of chapter/image files. [cbz-reader]
MAX_CBZ_PAGES = 800

# Ceiling on total *decompressed* bytes read from the zip across every page,
# enforced against actual bytes read off the decompression stream (see
# :func:`_read_image_member`) — not the forgeable central-directory size — so it
# also defeats a zip bomb. Comic page images barely compress, so a 300MB spool
# decompresses to roughly the same; this leaves generous headroom above that
# while still bounding a hostile archive. [cbz-reader]
MAX_CBZ_UNPACKED_BYTES = 700 * 1024 * 1024

# Image member extensions Pillow can decode into a comic page. ComicInfo.xml and
# any other non-image member are ignored (ComicInfo is optionally parsed for a
# title, below). WebP is listed for completeness — it will be transcoded to
# baseline JPEG like everything else, so old Safari never sees WebP. [cbz-reader]
_CBZ_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")

_CBZ_DIGITS_RE = re.compile(r"(\d+)")


def _natural_sort_key(path: str) -> list[object]:
    """Return a natural-ordering sort key for a comic page member *path*.

    Splits *path* into alternating non-digit / digit runs and converts each
    digit run to an ``int``, so numeric segments compare by value rather than
    lexicographically. This is why ``page2`` sorts before ``page10`` — plain
    string ordering would put ``page10`` first, scrambling the whole comic. The
    path is lower-cased first so ``Page`` and ``page`` interleave naturally.

    :param path: A zip member path (e.g. ``"comic/page10.jpg"``).
    :returns: A mixed ``list`` of ``str``/``int`` segments usable as a sort key.
    :rtype: list[object]
    """
    return [
        int(seg) if seg.isdigit() else seg
        for seg in _CBZ_DIGITS_RE.split(path.lower())
    ]


def _cbz_title_from_comicinfo(zf: zipfile.ZipFile, names: set[str], budget: _UnpackBudget) -> str | None:
    """Return a display title parsed from a CBZ's ``ComicInfo.xml``, if present.

    ComicInfo.xml is the de-facto comic metadata sidecar. Only its ``<Series>``
    (preferred) or ``<Title>`` text is read, via :mod:`defusedxml`, and it is
    returned as plain text — the caller surfaces it through normal Jinja
    auto-escaping, never the ``| safe`` seam, so no comic-supplied markup is
    ever rendered. Any absence or parse failure returns ``None`` (the book's own
    record title is used instead); this never fails the comic.

    :param zf: The open archive.
    :param names: The set of all member names in the archive.
    :param budget: The shelving pass's running :class:`_UnpackBudget`.
    :returns: A collapsed title string, or ``None``.
    :rtype: str or None
    """
    member = next((n for n in names if n.lower().rsplit("/", 1)[-1] == "comicinfo.xml"), None)
    if member is None or not _is_safe_member(member):
        return None
    try:
        data = _read_member_capped(zf, member, MAX_METADATA_MEMBER_BYTES, budget)
        root = fromstring(data)
    except (ReaderError, ParseError, ValueError):
        return None
    series: str | None = None
    title: str | None = None
    for el in root.iter():
        name = _local_name(el.tag)
        if name == "series" and el.text and el.text.strip():
            series = " ".join(el.text.split())
        elif name == "title" and el.text and el.text.strip():
            title = " ".join(el.text.split())
    return series or title


def _extract_cbz(spool_path: str, key: str, record: dict, tmp_dir: str) -> Manifest:
    """Parse the spooled CBZ at *spool_path* into *tmp_dir*'s shelved form.

    Opens the zip with stdlib :mod:`zipfile`, selects the image members (by
    extension, zip-slip-guarded exactly like EPUB), orders them by
    :func:`_natural_sort_key`, and transcodes each through the shared
    :func:`_transcode_reader_image` Pillow path into ``images/{n}`` (+ ``.ct``).
    Every decodable page becomes one chapter (``chapters/{n}.json``) whose single
    block is exactly ``<img src="{IMG:n}"/>`` — our own integer-indexed markup,
    so no comic-supplied text or markup is ever rendered.

    This function is deliberately **synchronous and CPU-heavy** (decoding and
    re-encoding hundreds of images is tens of seconds of work); it is only ever
    invoked off the event loop via :func:`asyncio.to_thread` in
    :func:`_extract_cbz_shelf`, so shelving one large comic never freezes the
    server for other requests.

    An undecodable page is skipped (never fails the comic); a comic with zero
    decodable pages, or none shelvable because Pillow is unavailable, raises a
    friendly :class:`ReaderError`. Produces ``Manifest.kind == "comic"``.

    :param spool_path: Path to the fully-downloaded CBZ file on disk.
    :param key: The book's cache key (``Manifest.book_key``).
    :param record: The caller's book record; only ``t``/``a`` are read.
    :param tmp_dir: The in-progress shelving directory (with empty ``chapters/``
        and ``images/`` subdirectories).
    :returns: The completed :class:`Manifest` (also written to ``manifest.json``).
    :raises ReaderError: On a malformed archive, one with no image pages, or
        (with Pillow unavailable) no decodable pages.
    """
    if not _PIL_AVAILABLE:
        raise ReaderError("This comic can't be read in the browser (no image support)")

    budget = _UnpackBudget(MAX_CBZ_UNPACKED_BYTES)
    try:
        zf = zipfile.ZipFile(spool_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ReaderError("This does not look like a valid CBZ comic") from exc

    with zf:
        names = set(zf.namelist())
        pages = sorted(
            (
                n for n in names
                if _is_safe_member(n) and n.lower().endswith(_CBZ_IMAGE_EXTS)
            ),
            key=_natural_sort_key,
        )
        if not pages:
            raise ReaderError("This comic has no readable pages")
        pages = pages[:MAX_CBZ_PAGES]

        comic_title = _cbz_title_from_comicinfo(zf, names, budget)

        images_dir = os.path.join(tmp_dir, "images")
        chapters_dir = os.path.join(tmp_dir, "chapters")
        chapters_meta: list[ChapterMeta] = []
        toc: list[tuple[int, str, int]] = []
        total_chars = 0
        for member in pages:
            raw = _read_image_member(zf, member, budget)
            if raw is None:
                continue  # oversized/unreadable page: skip, never fail the comic
            transcoded = _transcode_reader_image(raw)
            if transcoded is None:
                continue  # undecodable page: skip
            out_bytes, out_ct = transcoded
            idx = len(chapters_meta)
            _write_reader_image(images_dir, idx, out_bytes, out_ct)
            block = f'<img src="{{IMG:{idx}}}"/>'
            with open(os.path.join(chapters_dir, f"{idx}.json"), "w", encoding="utf-8") as f:
                json.dump({"blocks": [block], "anchors": {}}, f)
            # A 300-entry per-page ToC is noise; surface every 10th page so the
            # ToC is a usable "jump roughly here" index, not a wall of pages.
            page_no = idx + 1
            page_title = f"Page {page_no}"
            if idx == 0 or page_no % 10 == 0:
                toc.append((0, page_title, idx))
            chapters_meta.append(ChapterMeta(title=page_title, blocks=1, chars=len(block)))
            total_chars += len(block)

    if not chapters_meta:
        raise ReaderError("This comic has no readable pages")

    manifest = Manifest(
        version=2,
        book_key=key,
        title=str(record.get("t") or "") or comic_title or "Untitled",
        author=str(record.get("a") or ""),
        chapters=chapters_meta,
        images=len(chapters_meta),
        total_chars=total_chars,
        created=time.time(),
        toc=toc,
        kind="comic",
    )
    with open(os.path.join(tmp_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(_manifest_to_dict(manifest), f)
    return manifest


async def _extract_cbz_shelf(
    kc: KavitaClient, spool_path: str, key: str, record: dict, tmp_dir: str,
) -> Manifest:
    """Awaitable adapter making the synchronous CBZ extractor a
    :data:`_ShelfExtractor`, run **off the event loop**.

    Unlike the EPUB/PDF adapters (whose local work is light and inline), comic
    extraction decodes and re-encodes hundreds of images — tens of seconds of
    CPU. Running that inline would freeze the whole single-threaded server for
    every other request for the duration, so it is dispatched to a worker thread
    via :func:`asyncio.to_thread`; *kc* is unused (a CBZ's pages come from the
    spooled archive, so there is no network I/O).

    :param kc: Unused (CBZ images come from the spooled archive).
    :param spool_path: Path to the fully-downloaded CBZ on disk.
    :param key: The book's cache key.
    :param record: The caller's book record.
    :param tmp_dir: The in-progress shelving directory.
    :returns: The completed :class:`Manifest`.
    """
    return await asyncio.to_thread(_extract_cbz, spool_path, key, record, tmp_dir)


async def shelve_cbz_book(kc: KavitaClient, record: dict, cache_dir: str) -> Manifest:
    """Shelve *record*'s CBZ comic into the reader cache (text-free, page-image).

    The comic counterpart to :func:`shelve_book`: it produces the identical
    :class:`Manifest` + ``chapters/*.json`` + ``images/*`` layout, so every
    downstream reader function works unchanged — the difference is only
    ``Manifest.kind == "comic"`` and one image page per chapter. Each page image
    is transcoded (downscaled to an iPad-sized baseline JPEG) exactly like an
    EPUB image; the sole rendered "content" is our own ``<img src="{IMG:n}"/>``
    blocks, never any comic-supplied text or markup. The heavy per-page decode
    work runs off the event loop (see :func:`_extract_cbz_shelf`).

    :param kc: Kavita client used to spool the upstream CBZ. Every fetch goes
        through its SSRF guard.
    :param record: A book record with at least a ``u`` (acquisition URL) key;
        ``t`` (title) and ``a`` (author) are used for the manifest when present.
    :param cache_dir: The application cache root; the comic is shelved under
        ``{cache_dir}/reader/``.
    :returns: The comic's :class:`Manifest`.
    :raises ReaderError: On a malformed/oversized archive, one with no readable
        pages, or a shelving timeout.
    :raises KavitaError: If the upstream fetch fails outright.
    """
    return await _shelve_record(
        kc, record, cache_dir, spool_cap=MAX_CBZ_BYTES, extract=_extract_cbz_shelf
    )


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


def load_chapter_anchors(cache_dir: str, book_key: str, i: int) -> dict[str, int]:
    """Load chapter *i*'s ``{anchor_id: block_index}`` map (empty if none).

    Never raises: a missing/legacy chapter file (v1 shelving wrote no
    ``anchors``) or a malformed map degrades to ``{}``, so footnote links
    simply fall back to the chapter's first part.

    :param cache_dir: The application cache root.
    :param book_key: The book's cache key.
    :param i: The chapter's spine index (0-based).
    :returns: The chapter's anchor map (possibly empty).
    :rtype: dict[str, int]
    """
    path = os.path.join(cache_dir, "reader", book_key, "chapters", f"{i}.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        anchors = data.get("anchors")
        if not isinstance(anchors, dict):
            return {}
        return {
            str(k): int(v) for k, v in anchors.items()
            if isinstance(v, int) and not isinstance(v, bool) and v >= 0
        }
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return {}


# In-book search. Minimum query length (single characters would match every
# page and are useless); results and per-book cost are bounded. [findability]
SEARCH_MIN_QUERY = 2
SEARCH_MAX_HITS = 60
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class SearchHit:
    """One in-book search match, located at block granularity so the caller
    can resolve it to the reading part under the current page size.

    :ivar chapter: 0-based spine chapter index.
    :ivar block: Index of the block within the chapter that contains the match.
    :ivar before: Plain-text context immediately before the match.
    :ivar match: The matched text, in the book's own casing.
    :ivar after: Plain-text context immediately after the match.
    """

    chapter: int
    block: int
    before: str
    match: str
    after: str


def _block_plain_text(block: str) -> str:
    """Return a block's human-readable text: tags stripped, entities decoded."""
    return html.unescape(_TAG_RE.sub("", block))


def search_book(
    cache_dir: str,
    book_key: str,
    chapter_count: int,
    query: str,
    *,
    max_hits: int = SEARCH_MAX_HITS,
    radius: int = 48,
) -> list[SearchHit]:
    """Search a shelved book's text for *query* (case-insensitive substring).

    Scans each chapter's sanitized blocks as plain text (tags stripped,
    entities decoded), returning up to *max_hits* :class:`SearchHit` snippets
    in reading order. Returns ``[]`` for a query shorter than
    :data:`SEARCH_MIN_QUERY`. Never raises: an unreadable chapter is skipped.

    :param cache_dir: The application cache root.
    :param book_key: The book's cache key.
    :param chapter_count: Number of spine chapters (from the manifest).
    :param query: The search text.
    :param max_hits: Hard cap on returned matches.
    :param radius: Characters of context to include on each side of a match.
    :returns: Ordered list of matches, each locating the containing block.
    :rtype: list[SearchHit]
    """
    q = query.strip()
    if len(q) < SEARCH_MIN_QUERY:
        return []
    needle = q.lower()
    hits: list[SearchHit] = []
    for chapter in range(chapter_count):
        try:
            blocks = load_chapter(cache_dir, book_key, chapter)
        except ReaderError:
            continue
        for block_index, block in enumerate(blocks):
            text = _block_plain_text(block)
            low = text.lower()
            start = 0
            while True:
                pos = low.find(needle, start)
                if pos < 0:
                    break
                end = pos + len(q)
                hits.append(SearchHit(
                    chapter=chapter,
                    block=block_index,
                    before=text[max(0, pos - radius):pos],
                    match=text[pos:end],
                    after=text[end:end + radius],
                ))
                if len(hits) >= max_hits:
                    return hits
                start = end
    return hits


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
