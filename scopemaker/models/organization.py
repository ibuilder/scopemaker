"""Organizations, memberships and invitations.

Every piece of content in ScopeMaker belongs to exactly one organization.  That
single rule is what makes tenant isolation auditable: if a query does not
filter on ``organization_id`` it is a bug.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..security import generate_token
from .base import JSONType, Model, utcnow

if TYPE_CHECKING:
    from .user import User

# Ordered from least to most privileged. Membership.satisfies() relies on the
# index, so only ever append to the *end* of this tuple.
ROLE_HIERARCHY: tuple[str, ...] = ("viewer", "editor", "admin")
ROLE_LABELS = {
    "viewer": "Viewer - can read and export scopes",
    "editor": "Editor - can create and edit scopes and clauses",
    "admin": "Admin - can manage members, settings and integrations",
}


def role_rank(role: str) -> int:
    try:
        return ROLE_HIERARCHY.index(role)
    except ValueError:
        return -1


class Organization(Model):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)

    # Printed on generated exhibits
    legal_name: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(String(60))
    logo_path: Mapped[str | None] = mapped_column(String(500))

    # Org-wide defaults applied to new scopes (numbering style, boilerplate,
    # default exhibit label, footer text...). Free-form so operators can extend
    # it without a migration.
    settings: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )
    invitations: Mapped[list[Invitation]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )

    def setting(self, key: str, default=None):
        return (self.settings or {}).get(key, default)

    @property
    def display_name(self) -> str:
        return self.legal_name or self.name


class Membership(Model):
    """Join between a user and an organization, carrying the user's role."""

    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_membership_org_user"),
    )

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(20), default="editor", nullable=False)

    organization: Mapped[Organization] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(back_populates="memberships")

    def satisfies(self, required: str) -> bool:
        """True when this membership's role is at least ``required``."""
        return role_rank(self.role) >= role_rank(required) >= 0


class Invitation(Model):
    """A single-use invite granting membership at a fixed role."""

    __tablename__ = "invitations"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), default="editor", nullable=False)
    token: Mapped[str] = mapped_column(
        String(64), default=generate_token, nullable=False, unique=True, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invited_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    organization: Mapped[Organization] = relationship(back_populates="invitations")

    @staticmethod
    def default_expiry(days: int = 14) -> datetime:
        return utcnow() + timedelta(days=days)

    @property
    def is_expired(self) -> bool:
        expires = self.expires_at
        if expires.tzinfo is None:  # SQLite round-trips naive datetimes
            expires = expires.replace(tzinfo=UTC)
        return datetime.now(UTC) > expires

    @property
    def is_usable(self) -> bool:
        return self.accepted_at is None and not self.is_expired
