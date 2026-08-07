"""Reusable scope templates.

A template is a frozen recipe -- which sections are on, which clauses are
pre-selected, what boilerplate the prose sections carry -- so a company's
standard Division 26 exhibit can be produced identically every time.
"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import JSONType, Model


class ScopeTemplate(Model):
    __tablename__ = "scope_templates"
    __table_args__ = (
        Index("ix_templates_org_division", "organization_id", "division_code"),
    )

    # NULL = a system template shipped with the app.
    organization_id: Mapped[str | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    division_code: Mapped[str | None] = mapped_column(String(2), index=True)
    trade_name: Mapped[str | None] = mapped_column(String(160))

    # {
    #   "exhibit_label": "EXHIBIT B",
    #   "title": "Scope of Work",
    #   "numbering_style": [...],
    #   "settings": {...},
    #   "sections": [{"key", "heading", "kind", "is_enabled", "body_html",
    #                 "items": [{"text_html", "children": [...]}]}],
    #   "clause_system_keys": [...],
    #   "spec_section_keys": [...]
    # }
    payload: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)

    # Auto-applied when a scope is created for this division.
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    system_key: Mapped[str | None] = mapped_column(String(120), unique=True, index=True)

    created_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    @property
    def is_system(self) -> bool:
        return self.organization_id is None
