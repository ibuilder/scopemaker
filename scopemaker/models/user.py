"""Users and API tokens."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from flask import session
from flask_login import UserMixin
from sqlalchemy import Boolean, DateTime, ForeignKey, String, select
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..extensions import db
from ..security import generate_token, hash_password, verify_password
from .base import Model
from .organization import Membership, Organization

if TYPE_CHECKING:
    pass

# Session key holding the organization the user is currently working in.
ACTIVE_ORG_SESSION_KEY = "active_organization_id"


class User(UserMixin, Model):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    password_hash: Mapped[str | None] = mapped_column(String(255))

    # Set when the account was provisioned through OIDC; such accounts have no
    # local password and must keep signing in through the IdP.
    sso_subject: Mapped[str | None] = mapped_column(String(255), index=True)
    sso_provider: Mapped[str | None] = mapped_column(String(80))

    is_active_flag: Mapped[bool] = mapped_column(
        "is_active", Boolean, default=True, nullable=False
    )
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )
    api_tokens: Mapped[list[ApiToken]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    # -- Flask-Login --------------------------------------------------------
    @property
    def is_active(self) -> bool:  # type: ignore[override]
        return self.is_active_flag

    def get_id(self) -> str:
        return self.id

    # -- Passwords ----------------------------------------------------------
    def set_password(self, password: str) -> None:
        self.password_hash = hash_password(password)

    def check_password(self, password: str) -> bool:
        if not self.password_hash:
            return False
        return verify_password(self.password_hash, password)

    @property
    def is_sso_only(self) -> bool:
        return self.password_hash is None and self.sso_subject is not None

    # -- Organizations ------------------------------------------------------
    @property
    def organizations(self) -> list[Organization]:
        return [m.organization for m in self.memberships]

    def membership_for(self, organization_id: str | None) -> Membership | None:
        if not organization_id:
            return None
        for membership in self.memberships:
            if membership.organization_id == organization_id:
                return membership
        return None

    @property
    def active_organization_id(self) -> str | None:
        """The org selected in this session, falling back to the first one.

        The session value is validated against real memberships on every read,
        so a stale or tampered session cannot grant access to another tenant.
        """
        candidate = session.get(ACTIVE_ORG_SESSION_KEY)
        if candidate and self.membership_for(candidate):
            return candidate
        return self.memberships[0].organization_id if self.memberships else None

    @property
    def active_organization(self) -> Organization | None:
        membership = self.active_membership
        return membership.organization if membership else None

    @property
    def active_membership(self) -> Membership | None:
        return self.membership_for(self.active_organization_id)

    @property
    def role(self) -> str | None:
        membership = self.active_membership
        return membership.role if membership else None

    def has_role(self, required: str) -> bool:
        """True when the user meets ``required`` in their active organization."""
        if self.is_superuser:
            return True
        membership = self.active_membership
        return bool(membership and membership.satisfies(required))

    def switch_organization(self, organization_id: str) -> bool:
        if not self.membership_for(organization_id):
            return False
        session[ACTIVE_ORG_SESSION_KEY] = organization_id
        return True

    # -- Lookups ------------------------------------------------------------
    @classmethod
    def by_email(cls, email: str) -> User | None:
        if not email:
            return None
        return db.session.scalar(select(cls).where(cls.email == email.strip().lower()))


class ApiToken(Model):
    """A bearer token for the JSON API.

    Only a hash of the token is stored; the plaintext is shown once at
    creation and is unrecoverable afterwards.
    """

    __tablename__ = "api_tokens"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    scopes: Mapped[str] = mapped_column(String(255), default="read", nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="api_tokens")

    PREFIX = "smk_"

    @classmethod
    def issue(
        cls,
        *,
        user: User,
        organization_id: str,
        name: str,
        scopes: str = "read",
        expires_at: datetime | None = None,
    ) -> tuple[ApiToken, str]:
        """Create a token, returning the record and the one-time plaintext."""
        raw = f"{cls.PREFIX}{generate_token(32)}"
        token = cls(
            user_id=user.id,
            organization_id=organization_id,
            name=name,
            scopes=scopes,
            expires_at=expires_at,
            token_prefix=raw[: len(cls.PREFIX) + 8],
            token_hash=hash_password(raw),
        )
        return token, raw

    def matches(self, raw: str) -> bool:
        return verify_password(self.token_hash, raw)

    @property
    def is_valid(self) -> bool:
        from .base import utcnow

        if self.revoked_at is not None:
            return False
        if self.expires_at is None:
            return True
        expires = self.expires_at
        if expires.tzinfo is None:
            from datetime import UTC

            expires = expires.replace(tzinfo=UTC)
        return utcnow() < expires
