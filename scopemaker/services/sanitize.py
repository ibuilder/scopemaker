"""HTML sanitization.

Scope text is authored in a rich-text editor and is later embedded verbatim in
HTML previews, PDFs and DOCX files.  Everything that arrives from a client is
run through here first: the allowlist is deliberately small -- the formatting a
contract exhibit actually uses -- so there is no route from a pasted clause to
script execution for the next person who opens the document.
"""

from __future__ import annotations

import re
from html import unescape as html_unescape

import bleach
from markupsafe import Markup

# Formatting genuinely used in scope language. Note the absence of any tag that
# can execute, load or navigate on its own.
ALLOWED_TAGS: set[str] = {
    "p", "br", "span",
    "strong", "b", "em", "i", "u", "s",
    "sup", "sub",
    "ul", "ol", "li",
    "table", "thead", "tbody", "tr", "th", "td",
    "a",
    "blockquote",
}

ALLOWED_ATTRIBUTES: dict[str, list[str]] = {
    "a": ["href", "title", "rel", "target"],
    "span": ["class"],
    "td": ["colspan", "rowspan"],
    "th": ["colspan", "rowspan", "scope"],
    "ol": ["type", "start"],
    "ul": ["type"],
}

ALLOWED_PROTOCOLS: list[str] = ["http", "https", "mailto"]

# Inline text only -- used for headings, titles and single-line item text where
# block structure would break the numbered outline.
INLINE_TAGS: set[str] = {"strong", "b", "em", "i", "u", "s", "sup", "sub", "span", "br", "a"}

_cleaner = bleach.Cleaner(
    tags=ALLOWED_TAGS,
    attributes=ALLOWED_ATTRIBUTES,
    protocols=ALLOWED_PROTOCOLS,
    strip=True,
    strip_comments=True,
)

_inline_cleaner = bleach.Cleaner(
    tags=INLINE_TAGS,
    attributes={"a": ["href", "title", "rel"], "span": ["class"]},
    protocols=ALLOWED_PROTOCOLS,
    strip=True,
    strip_comments=True,
)

_WHITESPACE = re.compile(r"[ \t]+")
_EMPTY_PARAGRAPH = re.compile(r"<p>(\s|&nbsp;|<br\s*/?>)*</p>", re.IGNORECASE)


def sanitize_html(value: str | None) -> Markup:
    """Clean block-level rich text and mark it safe for template output."""
    if not value:
        return Markup("")
    cleaned = _cleaner.clean(str(value))
    cleaned = _EMPTY_PARAGRAPH.sub("", cleaned)
    return Markup(cleaned.strip())


def sanitize_inline(value: str | None) -> Markup:
    """Clean text that must stay on one line (item text, headings)."""
    if not value:
        return Markup("")
    cleaned = _inline_cleaner.clean(str(value))
    cleaned = _WHITESPACE.sub(" ", cleaned)
    return Markup(cleaned.strip())


def strip_html(value: str | None) -> str:
    """Plain text with entities resolved -- for Markdown, JSON and search.

    Entity decoding covers the whole HTML5 table, not a hand-written subset:
    clause text routinely carries &ndash;, &nbsp; and &amp;, and a partial list
    leaks the raw entity into exported plain text.
    """
    if not value:
        return ""
    text = re.sub(r"<br\s*/?>", " ", str(value), flags=re.IGNORECASE)
    text = re.sub(r"</(p|li|div|tr)>", " ", text, flags=re.IGNORECASE)
    text = bleach.clean(text, tags=set(), attributes={}, strip=True)
    # bleach escapes as it strips, so unescape once to get readable text.
    text = html_unescape(text).replace("\xa0", " ")
    return " ".join(text.split())


_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

#: A tag, allowing for ``>`` inside a quoted attribute value.
#:
#: The obvious ``<[^>]*>`` is wrong, and wrong in a way that survives
#: sanitisation: bleach does not escape ``>`` inside attribute values, so
#: ``<a title="see > 220500">x</a>`` is a legitimate stored value. Stopping at
#: the first ``>`` leaves ``220500">x`` as text, and the coverage report then
#: reads a specification section number that nothing claims -- suppressing a
#: real gap -- or a "Division NN" mention that becomes a phantom hand-off.
#:
#: Each repetition consumes a whole quoted run, so there is no nested-quantifier
#: backtracking here.
_TAG = re.compile(r"""<[^>"']*(?:(?:"[^"]*"|'[^']*')[^>"']*)*>""")


def strip_stored_html(value: str | None) -> str:
    """Plain text from markup this application already sanitised.

    ``strip_html`` runs bleach, which parses the input with a full HTML5 parser
    because it has to be safe against anything a browser might be tricked into
    executing. That is the right tool for untrusted input and the wrong one for
    scanning our own stored text: profiling the coverage report showed the
    parser accounting for a third of the total time, entirely to find six-digit
    section numbers and the word "Division".

    Every ``text_html`` and ``body_html`` in the database has already been
    through ``sanitize_inline`` or ``sanitize_html`` on the way in, so it is a
    small, known, well-formed tag set. Removing tags from *that* needs no
    parser.

    Only for stored markup. Anything arriving from a client goes through
    ``strip_html``. ``tests/test_sanitize.py`` checks the two agree across the
    whole shipped clause library and a set of deliberately awkward inputs, so
    the shortcut cannot silently drift from the real thing.
    """
    if not value:
        return ""
    text = re.sub(r"<br\s*/?>", " ", str(value), flags=re.IGNORECASE)
    text = re.sub(r"</(p|li|div|tr)>", " ", text, flags=re.IGNORECASE)
    text = _COMMENT.sub("", text)
    text = _TAG.sub("", text)
    text = html_unescape(text).replace("\xa0", " ")
    return " ".join(text.split())


def is_effectively_empty(value: str | None) -> bool:
    """True when the markup carries no visible text."""
    return not strip_html(value).strip()
