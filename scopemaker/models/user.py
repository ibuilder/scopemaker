"""Users and API tokens."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from flask import session
from flask_login import UserMixin
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, select
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

    # -- Brute-force resistance --------------------------------------------
    # Counted per account rather than per IP: an IP limit does nothing against
    # credential stuffing spread across many addresses at one account.
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # -- Session revocation -------------------------------------------------
    # Bumped to invalidate every signed-in session at once. The value is baked
    # into the session cookie by get_id(), so raising it makes existing cookies
    # fail to resolve to a user on the next request.
    session_epoch: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )
    api_tokens: Mapped[list[ApiToken]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    reset_tokens: Mapped[list[PasswordResetToken]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    # -- Flask-Login --------------------------------------------------------
    @property
    def is_active(self) -> bool:  # type: ignore[override]
        return self.is_active_flag

    def get_id(self) -> str:
        """Identity stored in the session cookie.

        The session epoch travels with the id so that bumping it invalidates
        every existing cookie -- that is what makes "sign out everywhere" and
        "kill sessions on password change" work without a server-side session
        store.
        """
        return f"{self.id}|{self.session_epoch or 1}"

    @staticmethod
    def parse_session_id(raw: str) -> tuple[str, int | None]:
        """Split ``get_id()`` back into (user id, epoch)."""
        user_id, separator, epoch = (raw or "").partition("|")
        if not separator:
            # A cookie issued before session epochs existed.
            return user_id, None
        try:
            return user_id, int(epoch)
        except ValueError:
            return user_id, -1

    def revoke_sessions(self) -> None:
        """Sign this user out of every browser, including the current one.

        Column defaults are applied at INSERT, so a user that has not been
        flushed yet still has ``None`` here -- which happens on the very first
        set_password() during registration.
        """
        self.session_epoch = (self.session_epoch or 1) + 1

    # -- Passwords ----------------------------------------------------------
    def set_password(self, password: str, *, revoke_sessions: bool = True) -> None:
        self.password_hash = hash_password(password)
        # A password change must not leave an attacker's existing session live.
        if revoke_sessions:
            self.revoke_sessions()
        self.clear_lockout()

    def check_password(self, password: str) -> bool:
        if not self.password_hash:
            return False
        return verify_password(self.password_hash, password)

    # -- Lockout ------------------------------------------------------------
    @property
    def is_locked(self) -> bool:
        if self.locked_until is None:
            return False
        locked_until = self.locked_until
        if locked_until.tzinfo is None:
            from datetime import UTC

            locked_until = locked_until.replace(tzinfo=UTC)
        from .base import utcnow

        return utcnow() < locked_until

    def register_failed_login(self, *, max_attempts: int, lockout_seconds: int) -> bool:
        """Record a failed sign-in. Returns True if the account is now locked.

        The lockout window grows with each additional failure past the
        threshold, so a persistent attacker slows down while a user who simply
        mistyped is delayed only briefly.
        """
        from datetime import timedelta

        from .base import utcnow

        self.failed_login_count = (self.failed_login_count or 0) + 1
        if self.failed_login_count < max_attempts:
            return False

        over = self.failed_login_count - max_attempts
        multiplier = min(2**over, 32)  # cap the wait at a sane maximum
        self.locked_until = utcnow() + timedelta(seconds=lockout_seconds * multiplier)
        return True

    def clear_lockout(self) -> None:
        self.failed_login_count = 0
        self.locked_until = None

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


class PasswordResetToken(Model):
    """A single-use, expiring password reset.

    Stored as a hash for the same reason API tokens are: a leaked database
    should not hand an attacker a working reset link for every account that has
    one outstanding.
    """

    __tablename__ = "password_reset_tokens"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_ip: Mapped[str | None] = mapped_column(String(64))

    user: Mapped[User] = relationship(back_populates="reset_tokens")

    PREFIX_LENGTH = 12

    @classmethod
    def issue(cls, user: User, *, hours: int, ip: str | None = None) -> tuple[
        PasswordResetToken, str
    ]:
        from datetime import timedelta

        from .base import utcnow

        raw = generate_token(32)
        record = cls(
            user_id=user.id,
            token_prefix=raw[: cls.PREFIX_LENGTH],
            token_hash=hash_password(raw),
            expires_at=utcnow() + timedelta(hours=hours),
            requested_ip=ip,
        )
        return record, raw

    def matches(self, raw: str) -> bool:
        return verify_password(self.token_hash, raw)

    @property
    def is_usable(self) -> bool:
        if self.used_at is not None:
            return False
        expires = self.expires_at
        if expires.tzinfo is None:
            from datetime import UTC

            expires = expires.replace(tzinfo=UTC)
        from .base import utcnow

        return utcnow() < expires

    def consume(self) -> None:
        from .base import utcnow

        self.used_at = utcnow()


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
