"""Shared model primitives: id generation, timestamps, JSON portability."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from ..extensions import db

# JSONB on PostgreSQL (indexable, typed) and plain JSON everywhere else so the
# same models run against SQLite in development and tests.
JSONType = JSON().with_variant(JSONB(), "postgresql")


def new_uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class UUIDMixin:
    """String UUID primary key.

    UUIDs keep ids non-enumerable in URLs and let records be created
    client-side or merged across databases without collisions.
    """

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
    )


class Model(UUIDMixin, TimestampMixin, db.Model):  # type: ignore[name-defined]
    """Abstract base for application tables."""

    __abstract__ = True

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        label = getattr(self, "name", None) or getattr(self, "title", None) or self.id
        return f"<{type(self).__name__} {label}>"
