"""Projects and bid packages.

These carry the facts that get merged into an exhibit's boilerplate: project
name and number, the owner/architect/GC, and the bid package a scope is written
against.  They can be created by hand or synced from Procore.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Date, ForeignKey, Index, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Model

if TYPE_CHECKING:
    from .scope import Scope


class Project(Model):
    __tablename__ = "projects"
    __table_args__ = (
        Index("ix_projects_org_name", "organization_id", "name"),
        Index("ix_projects_procore", "organization_id", "procore_project_id"),
    )

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    number: Mapped[str | None] = mapped_column(String(80))
    description: Mapped[str | None] = mapped_column(Text)

    address: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(60))
    postal_code: Mapped[str | None] = mapped_column(String(20))

    owner_name: Mapped[str | None] = mapped_column(String(255))
    architect_name: Mapped[str | None] = mapped_column(String(255))
    engineer_name: Mapped[str | None] = mapped_column(String(255))
    contractor_name: Mapped[str | None] = mapped_column(String(255))

    # "CMAR", "GMP", "Design-Build", "Lump Sum" - drives some boilerplate wording
    delivery_method: Mapped[str | None] = mapped_column(String(60))

    start_date: Mapped[date | None] = mapped_column(Date)
    completion_date: Mapped[date | None] = mapped_column(Date)

    procore_project_id: Mapped[str | None] = mapped_column(String(60), index=True)
    procore_company_id: Mapped[str | None] = mapped_column(String(60))

    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    bid_packages: Mapped[list[BidPackage]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="BidPackage.number",
    )
    scopes: Mapped[list[Scope]] = relationship(back_populates="project")

    @property
    def location(self) -> str:
        parts = [self.address, self.city, self.state, self.postal_code]
        return ", ".join(p for p in parts if p)

    @property
    def display_title(self) -> str:
        return f"{self.number} - {self.name}" if self.number else self.name


class BidPackage(Model):
    """A scope-of-work package put out to bid, e.g. ``BP-21A Fire Protection``."""

    __tablename__ = "bid_packages"
    __table_args__ = (Index("ix_bid_packages_project_number", "project_id", "number"),)

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )

    number: Mapped[str] = mapped_column(String(60), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # The CSI division this package is anchored to. Drives which library
    # clauses and spec sections are suggested.
    division_code: Mapped[str | None] = mapped_column(String(2), index=True)
    trade_name: Mapped[str | None] = mapped_column(String(160))

    subcontractor_name: Mapped[str | None] = mapped_column(String(255))
    base_bid_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))

    bid_due_date: Mapped[date | None] = mapped_column(Date)

    procore_bid_package_id: Mapped[str | None] = mapped_column(String(60), index=True)

    project: Mapped[Project] = relationship(back_populates="bid_packages")
    scopes: Mapped[list[Scope]] = relationship(back_populates="bid_package")

    @property
    def display_title(self) -> str:
        return f"{self.number} - {self.name}"
