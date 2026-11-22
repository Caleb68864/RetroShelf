"""A small, safe subset of an EPUB's own CSS for the in-browser reader.

Opt-in (``EPUB_CSS=1``). The reader normally strips every attribute, so a
book's italics-by-class, centred headings and indents are lost. This module
parses the book's stylesheets and inline ``style`` attributes and keeps only
declarations that are safe for the reader and render correctly on old iPads
(iOS 5.1.1-12 Safari):

* Only these properties: ``text-align``, ``font-style``, ``font-weight``,
  ``font-variant``, ``text-decoration``, ``text-transform``,
  ``vertical-align``, ``text-indent``, ``margin-*``, and ``font-size`` in
  relative units. Values are checked against a strict pattern and bounds.
* No positioning, floats, transforms, colours, backgrounds, font families,
  ``url()`` or absolute units. The reader's own themes, fonts and text size
  stay in charge.
* Only simple selectors: ``tag``, ``.class`` and ``tag.class``. At-rules
  (``@media``, ``@font-face``...), combinators and pseudo-classes are
  ignored. Rules for ``html``/``body`` are ignored, so a book cannot restyle
  the page around it.

The output is a ``prop:value;...`` string made only of characters from the
value patterns below, so it is safe inside a double-quoted attribute.
"""
from __future__ import annotations

import re

_LEN_EM = r"-?(?:\d{1,2}(?:\.\d{1,3})?|\.\d{1,3})(?:em|%)|0"

#: property -> (value pattern, numeric bounds per unit or None)
_ALLOWED: dict[str, tuple[re.Pattern, dict[str, tuple[float, float]] | None]] = {
    "text-align": (re.compile(r"^(left|right|center|justify)$"), None),
    "font-style": (re.compile(r"^(normal|italic|oblique)$"), None),
    "font-weight": (re.compile(r"^(normal|bold|bolder|lighter|[1-9]00)$"), None),
    "font-variant": (re.compile(r"^(normal|small-caps)$"), None),
    "text-decoration": (re.compile(r"^(none|underline|line-through|overline)$"), None),
    "text-transform": (re.compile(r"^(none|uppercase|lowercase|capitalize)$"), None),
    "vertical-align": (re.compile(r"^(baseline|super|sub)$"), None),
    "text-indent": (re.compile(rf"^({_LEN_EM})$"), {"em": (-5, 5), "%": (-20, 20)}),
    "margin-left": (re.compile(rf"^({_LEN_EM})$"), {"em": (0, 6), "%": (0, 25)}),
    "margin-right": (re.compile(rf"^({_LEN_EM})$"), {"em": (0, 6), "%": (0, 25)}),
    "margin-top": (re.compile(rf"^({_LEN_EM})$"), {"em": (0, 4), "%": (0, 10)}),
    "margin-bottom": (re.compile(rf"^({_LEN_EM})$"), {"em": (0, 4), "%": (0, 10)}),
    "font-size": (re.compile(rf"^({_LEN_EM}|smaller|larger)$"), {"em": (0.6, 2), "%": (60, 200)}),
}

_SIMPLE_SELECTOR = re.compile(r"^([a-z][a-z0-9]*)?(?:\.([A-Za-z_][\w-]{0,63}))?$")
_IGNORED_TAGS = frozenset({"html", "body"})
_CLASS_RE = re.compile(r"^[A-Za-z_][\w-]{0,63}$")
_MAX_RULES = 2000
_MAX_CSS_CHARS = 512 * 1024


def safe_declarations(text: str) -> dict[str, str]:
    """Return the allowlisted ``{property: value}`` pairs in *text*
    (the body of one rule, or a ``style`` attribute). Later wins."""
    out: dict[str, str] = {}
    for decl in (text or "").split(";"):
        if ":" not in decl:
            continue
        prop, _, value = decl.partition(":")
        prop = prop.strip().lower()
        value = re.sub(r"\s*!important\s*$", "", value.strip().lower())
        rule = _ALLOWED.get(prop)
        if rule is None or not rule[0].match(value):
            continue
        bounds = rule[1]
        num = re.match(r"^(-?[\d.]+)(em|%)$", value)
        if bounds and num:
            lo, hi = bounds[num.group(2)]
            if not lo <= float(num.group(1)) <= hi:
                continue
        out[prop] = value
    return out


def _top_level_rules(css: str):
    """Yield ``(selector_text, body)`` for top-level rules, skipping any
    at-rule together with its whole block."""
    css = re.sub(r"/\*.*?\*/", "", css[:_MAX_CSS_CHARS], flags=re.S)
    i, n = 0, len(css)
    while i < n:
        open_at = css.find("{", i)
        if open_at < 0:
            return
        head = css[i:open_at].strip()
        depth, j = 1, open_at + 1
        while j < n and depth:
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
            j += 1
        body = css[open_at + 1:j - 1]
        # A statement at-rule (@import ...;) before this rule's selector.
        head = head.rsplit(";", 1)[-1].strip()
        if head and not head.startswith("@") and "{" not in body:
            yield head, body
        i = j


class BookStyles:
    """The safe rules from one book's stylesheets, keyed by selector."""

    def __init__(self) -> None:
        # (tag or None, class or None) -> declarations, in source order.
        self._rules: dict[tuple[str | None, str | None], dict[str, str]] = {}
        self._count = 0

    def add_stylesheet(self, css: str) -> None:
        """Merge the safe parts of one stylesheet (later rules win)."""
        for selectors, body in _top_level_rules(css):
            decls = safe_declarations(body)
            if not decls:
                continue
            for sel in selectors.split(","):
                m = _SIMPLE_SELECTOR.match(sel.strip())
                if not m or not (m.group(1) or m.group(2)) or m.group(1) in _IGNORED_TAGS:
                    continue
                if self._count >= _MAX_RULES:
                    return
                self._count += 1
                self._rules.setdefault((m.group(1), m.group(2)), {}).update(decls)

    def style_for(self, tag: str, class_attr: str | None, style_attr: str | None) -> str:
        """The inline ``style`` value for one element, or ``""``.

        Cascade, lowest first: ``tag``, ``.class`` (in the element's class
        order), ``tag.class``, then the element's own ``style`` attribute.
        """
        classes = [c for c in (class_attr or "").split() if _CLASS_RE.match(c)][:8]
        merged: dict[str, str] = {}
        merged.update(self._rules.get((tag, None), {}))
        for c in classes:
            merged.update(self._rules.get((None, c), {}))
        for c in classes:
            merged.update(self._rules.get((tag, c), {}))
        merged.update(safe_declarations(style_attr or ""))
        return ";".join(f"{k}:{v}" for k, v in merged.items())
