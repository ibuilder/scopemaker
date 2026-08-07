"""Build the numbered document tree, and render it to HTML.

``build_document`` is the single source of truth for what a scope *says* and
what number each line carries.  Every other renderer consumes its output, which
is why the PDF and the DOCX cannot drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from flask import render_template

from ...models import Scope
from ...models.scope import SectionKind
from ..numbering import NumberedNode, build_numberer
from ..sanitize import sanitize_html, sanitize_inline, strip_html


@dataclass
class DocumentLine:
    """One numbered line, ready to render in any format."""

    label: str
    html: str
    text: str
    depth: int
    path: str
    role: str = ""
    children: list[DocumentLine] = field(default_factory=list)

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass
class DocumentSection:
    key: str
    heading: str
    kind: str
    body_html: str
    lines: list[DocumentLine] = field(default_factory=list)
    number: str = ""

    @property
    def has_content(self) -> bool:
        return bool(self.lines or strip_html(self.body_html).strip())


@dataclass
class RecapRow:
    label: str
    amount: Decimal | None
    is_total: bool = False


@dataclass
class Document:
    """A fully resolved, numbered scope document."""

    scope_id: str
    title: str
    exhibit_label: str
    scope_title: str
    division_label: str
    trade_name: str
    status: str
    version: int
    currency: str
    project: dict[str, Any]
    bid_package: dict[str, Any]
    organization: dict[str, Any]
    sections: list[DocumentSection] = field(default_factory=list)
    recap_rows: list[RecapRow] = field(default_factory=list)
    generated_at: str = ""

    @property
    def enabled_sections(self) -> list[DocumentSection]:
        return [s for s in self.sections if s.has_content]

    def line_count(self) -> int:
        return sum(len(list(line.walk())) for s in self.sections for line in s.lines)


def _to_lines(nodes: list[NumberedNode]) -> list[DocumentLine]:
    lines: list[DocumentLine] = []
    for node in nodes:
        item = node.item
        html = str(sanitize_inline(getattr(item, "text_html", "")))
        meta = getattr(item, "meta", None) or {}
        lines.append(
            DocumentLine(
                label=node.label,
                html=html,
                text=strip_html(html),
                depth=node.depth,
                path=node.path,
                role=meta.get("role", ""),
                children=_to_lines(node.children),
            )
        )
    return lines


def build_document(scope: Scope, *, organization: Any = None) -> Document:
    """Resolve a Scope into a numbered, sanitized, render-ready document."""
    from ...models.base import utcnow

    numberer = build_numberer(scope)
    org = organization

    sections: list[DocumentSection] = []
    # Section headings carry their own top-level number (1., 2., 3.) which is
    # independent of the item numbering inside them.
    section_index = 0
    for section in sorted(scope.sections, key=lambda s: s.position):
        if not section.is_enabled:
            continue

        lines = (
            _to_lines(numberer.walk(section.root_items))
            if section.kind == SectionKind.ITEMS
            else []
        )
        doc_section = DocumentSection(
            key=section.key,
            heading=section.heading,
            kind=section.kind,
            body_html=str(sanitize_html(section.body_html or "")),
            lines=lines,
        )
        if not doc_section.has_content and section.kind != SectionKind.RECAP:
            continue
        section_index += 1
        doc_section.number = f"{section_index}."
        sections.append(doc_section)

    recap_rows: list[RecapRow] = []
    if any(s.kind == SectionKind.RECAP for s in sections):
        package_label = (
            scope.bid_package.number if scope.bid_package else scope.exhibit_label
        )
        recap_rows = [
            RecapRow(f"{package_label} – Base Bid Amount", scope.base_bid_amount),
            RecapRow("Add: Accepted Alternates", scope.alternates_amount),
            RecapRow("Other Additions / Deletions", scope.adjustments_amount),
            RecapRow("TOTAL SUBCONTRACT AMOUNT", scope.total_amount, is_total=True),
        ]

    project = scope.project
    package = scope.bid_package

    return Document(
        scope_id=scope.id,
        title=scope.document_title,
        exhibit_label=scope.exhibit_label,
        scope_title=scope.title,
        division_label=(
            f"Division {scope.division_code}" if scope.division_code else ""
        ),
        trade_name=scope.trade_name or "",
        status=scope.status,
        version=scope.version,
        currency=scope.currency,
        project={
            "name": project.name if project else "",
            "number": project.number if project else "",
            "location": project.location if project else "",
            "owner": project.owner_name if project else "",
            "architect": project.architect_name if project else "",
            "contractor": project.contractor_name if project else "",
        },
        bid_package={
            "number": package.number if package else "",
            "name": package.name if package else "",
            "subcontractor": package.subcontractor_name if package else "",
        },
        organization={
            "name": getattr(org, "display_name", "") if org else "",
            "address": getattr(org, "address", "") if org else "",
            "phone": getattr(org, "phone", "") if org else "",
        },
        sections=sections,
        recap_rows=recap_rows,
        generated_at=utcnow().strftime("%B %d, %Y"),
    )


def render_html(scope: Scope, *, organization: Any = None,
                standalone: bool = True) -> str:
    """Render the document to HTML.

    ``standalone`` produces a complete page with the print stylesheet inlined,
    which is exactly what WeasyPrint consumes; otherwise only the document body
    is returned for embedding in the editor's live preview.
    """
    document = build_document(scope, organization=organization)
    template = (
        "documents/scope_standalone.html" if standalone else "documents/scope_body.html"
    )
    return render_template(template, doc=document, scope=scope)
