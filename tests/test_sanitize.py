"""Sanitization, and the fast path that skips the HTML parser.

``strip_stored_html`` exists because profiling the coverage report showed
bleach's HTML5 parser accounting for a third of the runtime, all of it spent
re-parsing markup this application had already sanitised on the way in.

A shortcut like that is only worth having if it cannot drift from the thing it
shortcuts. These tests compare it against ``strip_html`` across every clause and
specification section that ships with the product, plus inputs chosen to break
a naive tag-stripper.
"""

from __future__ import annotations

import pytest

from scopemaker.services.sanitize import (
    sanitize_html,
    sanitize_inline,
    strip_html,
    strip_stored_html,
)

# Inputs where a regex tag-stripper could plausibly disagree with a real parser.
AWKWARD = [
    "",
    "plain text with no markup at all",
    "<p>A paragraph.</p><p>And another.</p>",
    "Line one<br>Line two",
    "Line one<br/>Line two",
    "<strong>Bold</strong> and <em>italic</em> and <u>underlined</u>",
    "Div<b>ision</b> 07 &mdash; tags splitting a word",
    "Section <span class='x'>210500</span> inside a span",
    "<a href='/spec/210500' title='Division 07'>a link with attributes</a>",
    "entities: &amp; &lt; &gt; &ndash; &nbsp; &#8212; &quot;",
    "<!-- a comment -->visible",
    "<!-- a comment with > a bracket -->visible",
    "<ul><li>one</li><li>two</li></ul>",
    "<table><tr><td>cell</td><td>other</td></tr></table>",
    "trailing   whitespace   collapses   ",
    "<p></p>",
    "<p>&nbsp;</p>",
    "nested <strong>bold <em>and italic</em></strong> text",
    "<sup>1</sup> and <sub>2</sub>",
    "a &lt; b and c &gt; d",
]


@pytest.mark.parametrize("markup", AWKWARD)
def test_the_fast_path_matches_the_parser_on_awkward_input(markup):
    assert strip_stored_html(markup) == strip_html(markup), (
        f"diverged on {markup!r}"
    )


@pytest.mark.parametrize("markup", AWKWARD)
def test_the_fast_path_matches_after_sanitising(markup):
    """The realistic case: whatever a user typed, stored, then read back."""
    stored = str(sanitize_inline(markup))
    assert strip_stored_html(stored) == strip_html(stored), (
        f"diverged on stored {stored!r}"
    )

    block = str(sanitize_html(markup))
    assert strip_stored_html(block) == strip_html(block), (
        f"diverged on stored block {block!r}"
    )


def test_the_fast_path_matches_across_the_whole_shipped_library(app, db):
    """Every clause and specification section the product ships with.

    A corpus of real contract language is a better test of equivalence than any
    example I would think to write.
    """
    from scopemaker.models import Clause, SpecSection

    texts: list[str] = []
    for clause in db.session.query(Clause).all():
        texts.append(clause.text)
    for section in db.session.query(SpecSection).all():
        texts.extend([section.title, section.code])

    texts = [t for t in texts if t]
    assert len(texts) > 300, f"expected the seeded library, found {len(texts)}"

    diverged = [t for t in texts if strip_stored_html(t) != strip_html(t)]
    assert not diverged, (
        f"{len(diverged)} of {len(texts)} library entries diverged, "
        f"first: {diverged[0]!r}"
    )


def test_none_and_empty_are_handled():
    assert strip_stored_html(None) == ""
    assert strip_stored_html("") == ""
    assert strip_html(None) == ""


def test_the_fast_path_still_removes_every_tag():
    """The one property that must hold no matter what."""
    text = strip_stored_html(
        "<p>Furnish <strong>all</strong> <em>labour</em> per <a href='#'>210500</a>.</p>"
    )
    assert "<" not in text and ">" not in text
    assert text == "Furnish all labour per 210500."


def test_it_is_not_a_sanitiser():
    """Documenting the boundary: this extracts text, it does not make markup
    safe. Script *content* survives, exactly as it does with strip_html --
    which is why neither is ever used to produce HTML."""
    payload = "<script>alert(1)</script>"
    assert strip_stored_html(payload) == strip_html(payload)
    assert "<script>" not in strip_stored_html(payload)
