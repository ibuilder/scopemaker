"""The clause library -- the substance behind "generate a scope".

A clause is one reusable sentence of contract language, tagged with the CSI
division it applies to and the category it belongs to (inclusion, exclusion,
clarification...).  Clauses with ``organization_id IS NULL`` are the shipped
system library; an organization can add its own and can suppress system clauses
it disagrees with.
"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import JSONType, Model

# Category -> the exhibit section a clause feeds by default.
CLAUSE_CATEGORIES: dict[str, str] = {
    "inclusion": "Trade Specific Scope of Work Items",
    "exclusion": "Trade Specific Scope Exclusions",
    "clarification": "Clarifications and Assumptions",
    "allowance": "Allowances",
    "alternate": "Alternates",
    "unit_price": "Unit Prices",
    "general_requirement": "General Requirements",
    "safety": "Safety Requirements",
    "closeout": "Closeout Requirements",
    "schedule": "Schedule Requirements",
}

CATEGORY_ORDER = list(CLAUSE_CATEGORIES)


class Clause(Model):
    """One reusable piece of scope language."""

    __tablename__ = "clauses"
    __table_args__ = (
        Index("ix_clauses_lookup", "organization_id", "division_code", "category", "is_active"),
        Index("ix_clauses_system", "division_code", "category"),
    )

    # NULL means this is a system clause available to every organization.
    organization_id: Mapped[str | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )

    # NULL means the clause is universal -- it applies to every trade.
    division_code: Mapped[str | None] = mapped_column(String(2), index=True)

    category: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)

    # Stable identifier for shipped clauses so re-seeding updates rather than
    # duplicates them, and so org overrides can point at a specific clause.
    system_key: Mapped[str | None] = mapped_column(String(120), unique=True, index=True)

    # Selected automatically when a scope is generated for this division.
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tags: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    @property
    def is_system(self) -> bool:
        return self.organization_id is None

    @property
    def scope_of(self) -> str:
        return "All trades" if self.division_code is None else f"Division {self.division_code}"


class ClauseSuppression(Model):
    """Records that an organization has opted out of a system clause.

    System clauses are shared rows, so an org cannot delete one.  Suppressing
    it hides it from that org's pickers without touching anyone else's library.
    """

    __tablename__ = "clause_suppressions"
    __table_args__ = (
        UniqueConstraint("organization_id", "clause_id", name="uq_suppression_org_clause"),
    )

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    clause_id: Mapped[str] = mapped_column(
        ForeignKey("clauses.id", ondelete="CASCADE"), nullable=False, index=True
    )


class SpecSection(Model):
    """A CSI specification section that can be cross-referenced by a scope.

    Trades routinely carry sections from *other* divisions -- fire protection
    picks up firestopping from Division 07 and access doors from Division 08 --
    and forgetting them is a classic scope gap.  ``related_to_division`` is what
    lets the generator surface those automatically.
    """

    __tablename__ = "spec_sections"
    __table_args__ = (
        Index("ix_spec_sections_lookup", "organization_id", "division_code"),
    )

    organization_id: Mapped[str | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )

    # The division the section actually lives in (e.g. "07" for firestopping).
    division_code: Mapped[str] = mapped_column(String(2), nullable=False, index=True)

    # Other divisions whose scopes should also be offered this section. One
    # firestopping section is carried by fire protection, plumbing, HVAC,
    # electrical and communications alike, so this is a list rather than a
    # single foreign division.
    related_divisions: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)

    # Offered on every scope regardless of division (Division 01 procedural
    # sections such as submittals, closeout and commissioning).
    is_universal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    code: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)

    system_key: Mapped[str | None] = mapped_column(String(120), unique=True, index=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    @property
    def display(self) -> str:
        return f"{self.code} - {self.title}"

    @property
    def is_system(self) -> bool:
        return self.organization_id is None

    def applies_to(self, division_code: str | None) -> bool:
        """True when this section should be offered to a scope in ``division_code``."""
        if self.is_universal:
            return True
        if not division_code:
            return False
        if self.division_code == division_code:
            return True
        return division_code in (self.related_divisions or [])
