"""Outline numbering for scope documents.

Subcontract exhibits are argued over clause by clause, so the label on a line
matters: "we agreed to 3.2.4" has to point at exactly one sentence.  Browser
default list numbering cannot do this -- it restarts per list and offers no
multi-level scheme -- so labels are computed here and rendered as literal text
in every output format.  The PDF, the DOCX, the HTML preview and the JSON
export therefore all carry identical numbering.

Two schemes are supported:

``legal``
    Concatenated ancestors: ``1.``, ``1.1``, ``1.1.1``.  The default, and what
    most GC exhibit templates use.

``outline``
    A distinct style per level: ``1.``, ``A.``, ``1)``, ``a)``.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

LEGAL = "legal"
OUTLINE = "outline"

DEFAULT_LEGAL_STYLES: tuple[str, ...] = ("decimal", "decimal", "decimal", "decimal", "decimal")
DEFAULT_OUTLINE_STYLES: tuple[str, ...] = (
    "decimal",
    "upper-alpha",
    "decimal-paren",
    "lower-alpha-paren",
    "lower-roman-paren",
)

_ROMAN = (
    (1000, "m"), (900, "cm"), (500, "d"), (400, "cd"),
    (100, "c"), (90, "xc"), (50, "l"), (40, "xl"),
    (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"),
)


def _to_roman(value: int) -> str:
    if value <= 0:
        return str(value)
    out: list[str] = []
    remaining = value
    for amount, numeral in _ROMAN:
        count, remaining = divmod(remaining, amount)
        out.append(numeral * count)
    return "".join(out)


def _to_alpha(value: int) -> str:
    """1 -> a, 26 -> z, 27 -> aa (spreadsheet-column style)."""
    if value <= 0:
        return str(value)
    out = ""
    remaining = value
    while remaining > 0:
        remaining, remainder = divmod(remaining - 1, 26)
        out = chr(ord("a") + remainder) + out
    return out


def format_counter(value: int, style: str) -> str:
    """Render one counter in the given style, without any separator."""
    base = style.removesuffix("-paren")
    if base == "decimal":
        text = str(value)
    elif base == "lower-alpha":
        text = _to_alpha(value)
    elif base == "upper-alpha":
        text = _to_alpha(value).upper()
    elif base == "lower-roman":
        text = _to_roman(value)
    elif base == "upper-roman":
        text = _to_roman(value).upper()
    else:
        text = str(value)
    return text


SUPPORTED_STYLES: tuple[str, ...] = (
    "decimal",
    "lower-alpha",
    "upper-alpha",
    "lower-roman",
    "upper-roman",
    "decimal-paren",
    "lower-alpha-paren",
    "upper-alpha-paren",
    "lower-roman-paren",
    "upper-roman-paren",
)

STYLE_LABELS: dict[str, str] = {
    "decimal": "1, 2, 3",
    "lower-alpha": "a, b, c",
    "upper-alpha": "A, B, C",
    "lower-roman": "i, ii, iii",
    "upper-roman": "I, II, III",
    "decimal-paren": "1), 2), 3)",
    "lower-alpha-paren": "a), b), c)",
    "upper-alpha-paren": "A), B), C)",
    "lower-roman-paren": "i), ii), iii)",
    "upper-roman-paren": "I), II), III)",
}


class Numberable(Protocol):
    """Anything with ordered children can be numbered."""

    @property
    def children(self) -> list[Any]: ...


@dataclass
class NumberedNode:
    """One line of the outline, flattened with its computed label."""

    item: Any
    label: str
    depth: int
    counters: tuple[int, ...]
    children: list[NumberedNode] = field(default_factory=list)

    @property
    def path(self) -> str:
        """Dotted counter path, e.g. ``3.2.4`` -- stable regardless of style."""
        return ".".join(str(c) for c in self.counters)


class Numberer:
    """Computes outline labels for a tree of items."""

    def __init__(
        self,
        styles: Iterable[str] | None = None,
        *,
        scheme: str = LEGAL,
        separator: str = ".",
        trailing: str | None = None,
    ):
        self.scheme = scheme if scheme in (LEGAL, OUTLINE) else LEGAL
        chosen = [s for s in (styles or ()) if s in SUPPORTED_STYLES]
        if not chosen:
            chosen = list(
                DEFAULT_LEGAL_STYLES if self.scheme == LEGAL else DEFAULT_OUTLINE_STYLES
            )
        self.styles = chosen
        self.separator = separator
        # Legal numbering conventionally ends the top level with a period
        # ("1.") but not deeper levels ("1.1"). ``trailing`` overrides that.
        self.trailing = trailing

    def style_for(self, depth: int) -> str:
        if not self.styles:
            return "decimal"
        # Deeper than the configured styles: reuse the last one rather than
        # falling over, so an unexpectedly deep outline still renders.
        return self.styles[min(depth, len(self.styles) - 1)]

    def label(self, counters: tuple[int, ...]) -> str:
        if not counters:
            return ""
        if self.scheme == OUTLINE:
            depth = len(counters) - 1
            style = self.style_for(depth)
            text = format_counter(counters[-1], style)
            suffix = ")" if style.endswith("-paren") else "."
            return f"{text}{suffix}"

        parts = [
            format_counter(value, self.style_for(depth))
            for depth, value in enumerate(counters)
        ]
        text = self.separator.join(parts)
        if self.trailing is not None:
            return f"{text}{self.trailing}"
        return f"{text}." if len(counters) == 1 else text

    def walk(self, items: Iterable[Any], _prefix: tuple[int, ...] = ()) -> list[NumberedNode]:
        """Number a tree, returning nodes that mirror the input structure."""
        nodes: list[NumberedNode] = []
        for index, item in enumerate(items, start=1):
            counters = (*_prefix, index)
            children = getattr(item, "children", None) or []
            if children:
                children = sorted(children, key=lambda c: getattr(c, "position", 0))
            nodes.append(
                NumberedNode(
                    item=item,
                    label=self.label(counters),
                    depth=len(counters) - 1,
                    counters=counters,
                    children=self.walk(children, counters),
                )
            )
        return nodes

    def flatten(self, items: Iterable[Any]) -> Iterator[NumberedNode]:
        """Depth-first sequence of numbered nodes -- convenient for DOCX."""
        yield from _flatten(self.walk(items))


def _flatten(nodes: Iterable[NumberedNode]) -> Iterator[NumberedNode]:
    for node in nodes:
        yield node
        yield from _flatten(node.children)


def build_numberer(scope: Any) -> Numberer:
    """Construct a Numberer from a Scope's stored presentation settings."""
    settings = getattr(scope, "settings", None) or {}
    styles = getattr(scope, "numbering_style", None) or []
    return Numberer(
        styles,
        scheme=settings.get("numbering_scheme", LEGAL),
        separator=settings.get("numbering_separator", "."),
        trailing=settings.get("numbering_trailing"),
    )
