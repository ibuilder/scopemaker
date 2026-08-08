"""Page layout, measured rather than eyeballed.

The existing PDF tests assert that text is *present* and that the document
paginates. Both passed while page 1 of the Division 21 exhibit was 45% blank:
a clause with 22 specification sections nested under it was an unbreakable
block, so it moved to the next page whole instead of splitting.

Nothing in a text-presence assertion can see that. These tests measure how far
down each page the content actually reaches, which is the property a reader
notices immediately and a test suite otherwise never checks.
"""

from __future__ import annotations

import pytest

from scopemaker.services.renderers import PDF_AVAILABLE, render_pdf

pytestmark = [
    pytest.mark.pdf,
    pytest.mark.skipif(
        not PDF_AVAILABLE, reason="WeasyPrint native libraries unavailable"
    ),
]

MM = 72 / 25.4
PAGE_HEIGHT = 792.0  # Letter portrait, matching @page in document.css
CONTENT_TOP = PAGE_HEIGHT - 25 * MM  # 25mm top margin
CONTENT_BOTTOM = 22 * MM  # 22mm bottom margin
CONTENT_HEIGHT = CONTENT_TOP - CONTENT_BOTTOM

#: A page that stops above this much of the content area has wasted the rest.
#: The regression this guards against measured 67%; healthy pages of the same
#: document measure 86-98%. 75% sits between the two with room for the ordinary
#: raggedness of keeping a heading with the paragraph beneath it.
MIN_FILL = 0.75


def page_fill(payload: bytes) -> list[float]:
    """How far down the content area each page's text reaches, 0.0 to 1.0.

    WeasyPrint emits a flipped, scaled coordinate system, so a glyph's position
    on the page is ``cm[3] * tm[5] + cm[5]`` -- the text matrix alone reports
    the same value for a full page and a half-empty one, which is exactly the
    mistake that makes this measurement look like it works when it does not.

    Text outside the margins is the running header and footer, which live in
    the page margin boxes and would otherwise make every page look full.
    """
    import io

    from pypdf import PdfReader

    fills: list[float] = []
    for page in PdfReader(io.BytesIO(payload)).pages:
        positions: list[float] = []

        def visit(text, cm, tm, font, size, sink=positions):
            if text.strip():
                sink.append(cm[3] * tm[5] + cm[5])

        page.extract_text(visitor_text=visit)
        body = [y for y in positions if CONTENT_BOTTOM <= y <= CONTENT_TOP]
        fills.append((CONTENT_TOP - min(body)) / CONTENT_HEIGHT if body else 0.0)
    return fills


def test_no_page_is_left_half_empty(app, scope, organization):
    """Every page but the last has to be substantially full.

    The last page is exempt: a document ends where it ends, and the signature
    block legitimately sits alone.
    """
    fills = page_fill(render_pdf(scope, organization=organization))
    assert len(fills) > 1, "need a multi-page document to measure pagination"

    thin = [
        (number, fill)
        for number, fill in enumerate(fills[:-1], start=1)
        if fill < MIN_FILL
    ]
    assert not thin, (
        "page(s) left mostly blank: "
        + ", ".join(f"page {n} only {f:.0%} full" for n, f in thin)
        + f" (all pages: {[f'{f:.0%}' for f in fills]})"
    )


def test_a_long_nested_list_splits_across_pages(app, db, scope, organization):
    """The specific shape that regressed: one clause, many children.

    A clause whose sub-list cannot fit in the space remaining must split, not
    move to the next page in one piece.
    """
    from scopemaker.models import ScopeItem

    section = scope.section("summary")
    parent = next(
        item for item in section.items if item.parent_id is None and item.children
    )
    existing = len(parent.children)
    for index in range(40):
        db.session.add(
            ScopeItem(
                section_id=section.id,
                parent_id=parent.id,
                position=existing + index,
                text_html=f"0{index:05d} &ndash; Additional specification section",
            )
        )
    db.session.commit()
    db.session.refresh(scope)

    fills = page_fill(render_pdf(scope, organization=organization))
    thin = [f for f in fills[:-1] if f < MIN_FILL]
    assert not thin, (
        f"a {existing + 40}-item sub-list did not split cleanly: "
        f"{[f'{f:.0%}' for f in fills]}"
    )


def test_the_measurement_itself_is_not_vacuous(app, scope, organization):
    """Guard the guard.

    If ``page_fill`` silently returned the same number for every page -- which
    is what happens if the coordinate transform is wrong -- the tests above
    would pass no matter how the document laid out. Real pagination produces
    varying fills, and the last page of this document is not full.
    """
    fills = page_fill(render_pdf(scope, organization=organization))
    assert len({round(f, 3) for f in fills}) > 1, (
        f"every page reported identical fill ({fills[0]:.0%}); "
        "the coordinate transform is probably wrong"
    )
    assert max(fills) > 0.8, f"no page looks full at all: {fills}"
