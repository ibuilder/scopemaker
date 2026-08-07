"""Procore connection state.

Tokens are never stored in plaintext.  ``access_token``/``refresh_token`` are
properties that transparently encrypt on write and decrypt on read using the
application's ``ENCRYPTION_KEY``; the columns themselves hold ciphertext.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..security import decrypt_secret, encrypt_secret
from .base import JSONType, Model, utcnow

# Refresh a little before actual expiry so an in-flight request never races
# the token going stale.
EXPIRY_SKEW = timedelta(seconds=120)


class ProcoreConnection(Model):
    """One organization's link to one Procore company."""

    __tablename__ = "procore_connections"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    # "authorization_code" (a person authorized it) or "client_credentials"
    # (a Developer Managed Service Account, for unattended sync).
    grant_type: Mapped[str] = mapped_column(
        String(30), default="authorization_code", nullable=False
    )

    company_id: Mapped[str | None] = mapped_column(String(60))
    company_name: Mapped[str | None] = mapped_column(String(255))

    procore_user_id: Mapped[str | None] = mapped_column(String(60))
    procore_user_name: Mapped[str | None] = mapped_column(String(255))
    procore_user_email: Mapped[str | None] = mapped_column(String(255))

    _access_token: Mapped[str | None] = mapped_column("access_token", Text)
    _refresh_token: Mapped[str | None] = mapped_column("refresh_token", Text)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    connected_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    extra: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)

    # -- Encrypted token accessors -----------------------------------------
    @property
    def access_token(self) -> str | None:
        return decrypt_secret(self._access_token)

    @access_token.setter
    def access_token(self, value: str | None) -> None:
        self._access_token = encrypt_secret(value)

    @property
    def refresh_token(self) -> str | None:
        return decrypt_secret(self._refresh_token)

    @refresh_token.setter
    def refresh_token(self, value: str | None) -> None:
        self._refresh_token = encrypt_secret(value)

    # -- State --------------------------------------------------------------
    @property
    def is_expired(self) -> bool:
        if self.token_expires_at is None:
            return True
        expires = self.token_expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return datetime.now(UTC) >= (expires - EXPIRY_SKEW)

    @property
    def is_connected(self) -> bool:
        """Usable now, or refreshable into a usable state."""
        if not self.is_active:
            return False
        if self._access_token is None:
            return False
        if not self.is_expired:
            return True
        # Service accounts mint a fresh token from the client secret, so they
        # stay usable without a refresh token.
        return self.grant_type == "client_credentials" or self._refresh_token is not None

    def apply_token_response(self, payload: dict[str, Any]) -> None:
        """Store the result of a token or refresh call."""
        self.access_token = payload.get("access_token")
        # Procore omits refresh_token for client-credentials grants; don't
        # clobber an existing one with None on refresh.
        if payload.get("refresh_token"):
            self.refresh_token = payload["refresh_token"]
        expires_in = payload.get("expires_in")
        if expires_in:
            self.token_expires_at = utcnow() + timedelta(seconds=int(expires_in))
        elif payload.get("created_at") and payload.get("expires_in"):  # pragma: no cover
            self.token_expires_at = utcnow() + timedelta(seconds=int(payload["expires_in"]))
        self.last_error = None

    def disconnect(self) -> None:
        self._access_token = None
        self._refresh_token = None
        self.token_expires_at = None
        self.is_active = False
