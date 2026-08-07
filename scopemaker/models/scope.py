"""The scope document itself: sections, items and immutable revisions.

A ``Scope`` is an ordered list of ``ScopeSection``s.  Sections hold prose
(``body_html``) and/or an ordered tree of ``ScopeItem``s.  Items nest through
``parent_id``, which is what produces the 1. / 1.1 / 1.1.1 outline that
subcontract exhibits are written in.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import JSONType, Model

if TYPE_CHECKING:
    from .project import BidPackage, Project

SCOPE_STATUSES = ("draft", "in_review", "approved", "issued", "archived")

STATUS_LABELS = {
    "draft": "Draft",
    "in_review": "In review",
    "approved": "Approved",
    "issued": "Issued",
    "archived": "Archived",
}

# Once a scope is issued it has been attached to a subcontract, so further
# edits must go through a new revision rather than silently rewriting history.
LOCKED_STATUSES = frozenset({"issued", "archived"})


class SectionKind:
    """How a section's content is authored and rendered."""

    PROSE = "prose"          # a paragraph or two of boilerplate
    ITEMS = "items"          # a numbered outline of clauses
    RECAP = "recap"          # the contract amount table


# Marks the summary item whose children are the cross-referenced specification
# sections. They nest there rather than forming a sibling section because that
# is how the numbering reads on a real exhibit: 1.3 introduces the list and
# 1.3.1 onward are the sections themselves.
SPEC_LIST_ROLE = "spec_list"


# The default anatomy of an exhibit, in the order the industry writes it.
DEFAULT_SECTIONS: list[dict[str, Any]] = [
    {"key": "intent", "heading": "Intent", "kind": SectionKind.PROSE, "enabled": True},
    {"key": "summary", "heading": "Scope of Work Summary", "kind": SectionKind.ITEMS, "enabled": True},
    {"key": "inclusions", "heading": "Trade Specific Scope of Work Items", "kind": SectionKind.ITEMS, "enabled": True},
    {"key": "exclusions", "heading": "Trade Specific Scope Exclusions", "kind": SectionKind.ITEMS, "enabled": True},
    {"key": "clarifications", "heading": "Clarifications and Assumptions", "kind": SectionKind.ITEMS, "enabled": True},
    {"key": "allowances", "heading": "Allowances", "kind": SectionKind.ITEMS, "enabled": False},
    {"key": "alternates", "heading": "Alternates", "kind": SectionKind.ITEMS, "enabled": False},
    {"key": "unit_prices", "heading": "Unit Prices", "kind": SectionKind.ITEMS, "enabled": False},
    {"key": "schedule", "heading": "Schedule Requirements", "kind": SectionKind.ITEMS, "enabled": False},
    {"key": "safety", "heading": "Safety Requirements", "kind": SectionKind.ITEMS, "enabled": False},
    {"key": "closeout", "heading": "Closeout Requirements", "kind": SectionKind.ITEMS, "enabled": False},
    {"key": "attachments", "heading": "Attachments", "kind": SectionKind.ITEMS, "enabled": False},
    {"key": "recap", "heading": "Recap of Contract Amount", "kind": SectionKind.RECAP, "enabled": True},
]

SECTION_KEYS = [s["key"] for s in DEFAULT_SECTIONS]

# Which clause category feeds which section when a scope is generated.
CATEGORY_TO_SECTION = {
    "inclusion": "inclusions",
    "exclusion": "exclusions",
    "clarification": "clarifications",
    "allowance": "allowances",
    "alternate": "alternates",
    "unit_price": "unit_prices",
    "general_requirement": "summary",
    "safety": "safety",
    "closeout": "closeout",
    "schedule": "schedule",
}


class Scope(Model):
    __tablename__ = "scopes"
    __table_args__ = (
        Index("ix_scopes_org_status", "organization_id", "status"),
        Index("ix_scopes_project", "project_id"),
    )

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    bid_package_id: Mapped[str | None] = mapped_column(
        ForeignKey("bid_packages.id", ondelete="SET NULL"), index=True
    )

    # -- Identity -----------------------------------------------------------
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="Scope of Work")
    exhibit_label: Mapped[str] = mapped_column(String(60), nullable=False, default="EXHIBIT B")
    division_code: Mapped[str | None] = mapped_column(String(2), index=True)
    trade_name: Mapped[str | None] = mapped_column(String(160))

    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # -- Presentation -------------------------------------------------------
    # Per-level outline styles, e.g. ["decimal", "decimal", "lower-alpha"].
    numbering_style: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    # Free-form render options: show_header, show_footer, paper size, fonts...
    settings: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)

    # -- Financial recap ----------------------------------------------------
    base_bid_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    alternates_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    adjustments_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)

    # -- Audit --------------------------------------------------------------
    created_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    updated_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    procore_commitment_id: Mapped[str | None] = mapped_column(String(60), index=True)
    procore_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    project: Mapped[Project | None] = relationship(back_populates="scopes")
    bid_package: Mapped[BidPackage | None] = relationship(back_populates="scopes")
    sections: Mapped[list[ScopeSection]] = relationship(
        back_populates="scope",
        cascade="all, delete-orphan",
        order_by="ScopeSection.position",
        lazy="selectin",
    )
    revisions: Mapped[list[ScopeRevision]] = relationship(
        back_populates="scope",
        cascade="all, delete-orphan",
        order_by="ScopeRevision.version.desc()",
    )

    # -- Derived ------------------------------------------------------------
    @property
    def is_locked(self) -> bool:
        return self.status in LOCKED_STATUSES

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status.title())

    @property
    def document_title(self) -> str:
        return f"{self.exhibit_label} – {self.title}".strip()

    @property
    def total_amount(self) -> Decimal | None:
        """Base bid plus alternates plus adjustments, or None if unpriced."""
        parts = [self.base_bid_amount, self.alternates_amount, self.adjustments_amount]
        if all(p is None for p in parts):
            return None
        return sum((p for p in parts if p is not None), Decimal("0"))

    def section(self, key: str) -> ScopeSection | None:
        for section in self.sections:
            if section.key == key:
                return section
        return None

    @property
    def enabled_sections(self) -> list[ScopeSection]:
        return [s for s in self.sections if s.is_enabled]

    @property
    def item_count(self) -> int:
        return sum(len(s.items) for s in self.sections)

    def to_dict(self, *, include_items: bool = True) -> dict[str, Any]:
        """Serialize the whole document. Used for exports and revisions."""
        return {
            "id": self.id,
            "title": self.title,
            "exhibit_label": self.exhibit_label,
            "document_title": self.document_title,
            "division_code": self.division_code,
            "trade_name": self.trade_name,
            "status": self.status,
            "version": self.version,
            "currency": self.currency,
            "numbering_style": self.numbering_style or [],
            "settings": self.settings or {},
            "financials": {
                "base_bid_amount": _money(self.base_bid_amount),
                "alternates_amount": _money(self.alternates_amount),
                "adjustments_amount": _money(self.adjustments_amount),
                "total_amount": _money(self.total_amount),
            },
            "project": _project_dict(self.project),
            "bid_package": _bid_package_dict(self.bid_package),
            "sections": [s.to_dict(include_items=include_items) for s in self.sections],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ScopeSection(Model):
    __tablename__ = "scope_sections"
    __table_args__ = (UniqueConstraint("scope_id", "key", name="uq_section_scope_key"),)

    scope_id: Mapped[str] = mapped_column(
        ForeignKey("scopes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(40), nullable=False)
    heading: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), default=SectionKind.ITEMS, nullable=False)
    body_html: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_numbered: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    scope: Mapped[Scope] = relationship(back_populates="sections")
    items: Mapped[list[ScopeItem]] = relationship(
        back_populates="section",
        cascade="all, delete-orphan",
        order_by="ScopeItem.position",
        lazy="selectin",
    )

    @property
    def root_items(self) -> list[ScopeItem]:
        """Top-level items, ordered. Children hang off ``item.children``."""
        return sorted(
            (i for i in self.items if i.parent_id is None), key=lambda i: i.position
        )

    def to_dict(self, *, include_items: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "key": self.key,
            "heading": self.heading,
            "kind": self.kind,
            "body_html": self.body_html,
            "position": self.position,
            "is_enabled": self.is_enabled,
            "is_numbered": self.is_numbered,
        }
        if include_items:
            payload["items"] = [i.to_dict() for i in self.root_items]
        return payload


class ScopeItem(Model):
    """One numbered line in a section, optionally with nested children."""

    __tablename__ = "scope_items"
    __table_args__ = (Index("ix_scope_items_section_pos", "section_id", "position"),)

    section_id: Mapped[str] = mapped_column(
        ForeignKey("scope_sections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("scope_items.id", ondelete="CASCADE"), index=True
    )

    text_html: Mapped[str] = mapped_column(Text, nullable=False, default="")
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Which library clause this line came from, so edits can be diffed against
    # the standard language and reports can show how far a scope has drifted.
    source_clause_id: Mapped[str | None] = mapped_column(
        ForeignKey("clauses.id", ondelete="SET NULL")
    )
    is_edited: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Spec-section rows carry structured data alongside the rendered text.
    meta: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)

    section: Mapped[ScopeSection] = relationship(back_populates="items")
    # Self-referential tree. Every item -- child or not -- also belongs to
    # ``section.items``, which owns the delete-orphan cascade. Declaring
    # delete-orphan here as well would make an item an orphan the moment it was
    # detached from *either* collection.
    #
    # The plain `delete` cascade (never `delete-orphan`) removes a subtree when
    # its parent goes. Without it SQLAlchemy nulls out parent_id on any loaded
    # children instead, silently promoting a deleted clause's sub-clauses to
    # top-level items -- they would reappear in the exhibit under new numbers.
    # passive_deletes lets the FK's ON DELETE CASCADE handle unloaded rows.
    # remote_side belongs only on the many-to-one side.
    children: Mapped[list[ScopeItem]] = relationship(
        back_populates="parent",
        cascade="save-update, merge, delete",
        passive_deletes=True,
        order_by="ScopeItem.position",
        lazy="selectin",
    )
    parent: Mapped[ScopeItem | None] = relationship(
        back_populates="children", remote_side="ScopeItem.id"
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text_html": self.text_html,
            "position": self.position,
            "source_clause_id": self.source_clause_id,
            "is_edited": self.is_edited,
            "meta": self.meta or {},
            "children": [c.to_dict() for c in sorted(self.children, key=lambda c: c.position)],
        }


class ScopeRevision(Model):
    """An immutable snapshot taken whenever a scope is issued or reverted.

    Contract exhibits get argued about after the fact, so every issued version
    is preserved verbatim rather than being reconstructed from an edit log.
    """

    __tablename__ = "scope_revisions"
    __table_args__ = (
        UniqueConstraint("scope_id", "version", name="uq_revision_scope_version"),
    )

    scope_id: Mapped[str] = mapped_column(
        ForeignKey("scopes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONType, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    scope: Mapped[Scope] = relationship(back_populates="revisions")


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _money(value: Decimal | None) -> str | None:
    return None if value is None else f"{Decimal(value):.2f}"


def _project_dict(project: Project | None) -> dict[str, Any] | None:
    if project is None:
        return None
    return {
        "id": project.id,
        "name": project.name,
        "number": project.number,
        "location": project.location,
        "owner_name": project.owner_name,
        "architect_name": project.architect_name,
        "engineer_name": project.engineer_name,
        "contractor_name": project.contractor_name,
        "delivery_method": project.delivery_method,
    }


def _bid_package_dict(package: BidPackage | None) -> dict[str, Any] | None:
    if package is None:
        return None
    return {
        "id": package.id,
        "number": package.number,
        "name": package.name,
        "division_code": package.division_code,
        "trade_name": package.trade_name,
        "subcontractor_name": package.subcontractor_name,
    }
