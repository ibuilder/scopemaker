"""JSON export.

Emits the *resolved* document -- with computed outline labels -- rather than the
raw database rows, so two revisions of a scope can be diffed clause by clause
and downstream systems can reference "3.2.4" and mean the same line the PDF
does.
"""

from __future__ import annotations

import json
from typing import Any

from ...models import Scope
from .html import Document, DocumentLine, build_document


def _line_dict(line: DocumentLine) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "number": line.label,
        "path": line.path,
        "depth": line.depth,
        "text": line.text,
        "html": line.html,
    }
    if line.role:
        payload["role"] = line.role
    if line.children:
        payload["children"] = [_line_dict(child) for child in line.children]
    return payload


def build_payload(scope: Scope, *, organization: Any = None) -> dict[str, Any]:
    document: Document = build_document(scope, organization=organization)
    return {
        "format": "scopemaker.scope",
        "format_version": "1.0",
        "generated_at": document.generated_at,
        "scope": {
            "id": document.scope_id,
            "title": document.title,
            "exhibit_label": document.exhibit_label,
            "name": document.scope_title,
            "division": document.division_label,
            "trade": document.trade_name,
            "status": document.status,
            "version": document.version,
            "currency": document.currency,
        },
        "organization": document.organization,
        "project": document.project,
        "bid_package": document.bid_package,
        "sections": [
            {
                "key": section.key,
                "number": section.number,
                "heading": section.heading,
                "kind": section.kind,
                "body_html": section.body_html,
                "items": [_line_dict(line) for line in section.lines],
            }
            for section in document.enabled_sections
        ],
        "recap": [
            {
                "label": row.label,
                "amount": None if row.amount is None else f"{row.amount:.2f}",
                "is_total": row.is_total,
            }
            for row in document.recap_rows
        ],
    }


def render_json(scope: Scope, *, organization: Any = None, indent: int = 2) -> bytes:
    payload = build_payload(scope, organization=organization)
    return json.dumps(payload, indent=indent, ensure_ascii=False).encode("utf-8")
