"""Rendered documents: a work queue and a cache in one table.

Rendering a PDF takes a second or two of CPU. With synchronous gunicorn workers
that pins a whole worker, so a handful of concurrent downloads can starve the
rest of the application.

Two things fix that, and they want the same storage:

* **Caching.** A scope that has not changed since it was last rendered does not
  need rendering again. In practice this absorbs most of the load, because the
  same exhibit gets downloaded repeatedly while people review it.
* **A queue.** When a render *is* needed, doing it in a worker process keeps it
  off the request path.

So one row is both: a job that is `complete` is also the cache entry. The
``fingerprint`` is what ties a result to an exact state of the document -- when
the scope changes, the fingerprint changes, and the old row simply stops
matching rather than having to be invalidated.

Results are stored in the database rather than on disk or in object storage so
that a multi-replica deployment needs no shared filesystem and no extra
infrastructure. Exhibits are tens of kilobytes; ``MAX_RESULT_BYTES`` keeps a
pathological document from being cached at all rather than bloating the table.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Model, utcnow

QUEUED = "queued"
RUNNING = "running"
COMPLETE = "complete"
FAILED = "failed"

#: Anything larger is rendered and served but not retained.
MAX_RESULT_BYTES = 12 * 1024 * 1024

#: A job stuck in `running` for longer than this is assumed to have died with
#: its worker and is returned to the queue.
STALE_AFTER = timedelta(minutes=10)

#: How long a completed render stays useful. Short enough that the table does
#: not grow without bound, long enough to cover a review session.
RESULT_TTL = timedelta(days=7)

MAX_ATTEMPTS = 3


class RenderJob(Model):
    __tablename__ = "render_jobs"
    __table_args__ = (
        # The cache lookup: exact document state, exact format, complete.
        Index("ix_render_lookup", "scope_id", "format", "fingerprint", "status"),
        # The worker's claim query.
        Index("ix_render_queue", "status", "created_at"),
    )

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scope_id: Mapped[str] = mapped_column(
        ForeignKey("scopes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requested_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    format: Mapped[str] = mapped_column(String(10), nullable=False)
    #: Identifies the exact document state this result belongs to.
    fingerprint: Mapped[str] = mapped_column(String(80), nullable=False)

    status: Mapped[str] = mapped_column(String(12), default=QUEUED, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)

    #: Set while a worker holds the job, so a dead worker's claim can be spotted.
    worker_id: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    filename: Mapped[str | None] = mapped_column(String(255))
    result: Mapped[bytes | None] = mapped_column(LargeBinary)
    result_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # -- State --------------------------------------------------------------
    @property
    def is_complete(self) -> bool:
        return self.status == COMPLETE and self.result is not None

    @property
    def is_terminal(self) -> bool:
        return self.status in (COMPLETE, FAILED)

    @property
    def is_stale(self) -> bool:
        """A claim whose worker appears to have died."""
        if self.status != RUNNING or self.started_at is None:
            return False
        started = self.started_at
        if started.tzinfo is None:
            from datetime import UTC

            started = started.replace(tzinfo=UTC)
        return utcnow() - started > STALE_AFTER

    @property
    def duration_ms(self) -> int | None:
        if not (self.started_at and self.finished_at):
            return None
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    def mark_complete(self, payload: bytes, filename: str) -> None:
        self.status = COMPLETE
        self.filename = filename
        self.result_bytes = len(payload)
        # Oversized results are served once and not retained; caching them
        # would trade a bounded CPU cost for an unbounded storage one.
        self.result = payload if len(payload) <= MAX_RESULT_BYTES else None
        self.finished_at = utcnow()
        self.expires_at = utcnow() + RESULT_TTL
        self.error = None
        self.worker_id = None

    def mark_failed(self, message: str) -> None:
        self.status = FAILED
        self.error = (message or "")[:2000]
        self.finished_at = utcnow()
        self.worker_id = None

    def requeue(self) -> None:
        self.status = QUEUED
        self.worker_id = None
        self.started_at = None
