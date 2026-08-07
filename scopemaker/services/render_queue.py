"""Rendering documents off the request path, with a cache in front.

The flow an export request takes:

1. Compute the document's fingerprint. If a completed job already exists for
   that exact fingerprint and format, serve its bytes. Nothing is rendered.
2. Otherwise, if async rendering is on, enqueue a job and hand the caller a
   ticket to poll. The request returns immediately.
3. If async rendering is off (the default -- it needs a worker process), render
   inline and store the result, so the *next* request hits the cache.

Claiming a job is done with a conditional UPDATE rather than ``SELECT ... FOR
UPDATE SKIP LOCKED`` so the same code works on SQLite and PostgreSQL. Two
workers racing for the same row means one of them updates zero rows and simply
tries the next.
"""

from __future__ import annotations

import hashlib
import logging
import os
import socket
import time
from typing import Any

from flask import current_app
from sqlalchemy import delete, func, select, update

from ..extensions import db
from ..models import Scope
from ..models.base import utcnow
from ..models.render import (
    COMPLETE,
    FAILED,
    MAX_ATTEMPTS,
    QUEUED,
    RUNNING,
    RenderJob,
)

logger = logging.getLogger(__name__)


def worker_identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------

def fingerprint(scope: Scope, organization: Any = None) -> str:
    """Identify the exact document state a render belongs to.

    Hashes the content that actually ends up in the document, rather than
    relying on ``scope.updated_at``. That timestamp is not trustworthy for this
    purpose: the edit routes set ``scope.updated_by_id`` to mark the scope
    dirty, but assigning the *same* user id is not a change, so SQLAlchemy
    issues no UPDATE and ``onupdate`` never fires. The same person editing two
    items in a row would then keep the old fingerprint -- and be served a
    cached document missing their edit.

    Hashing the content costs one extra query (the sections and items are
    selectin-loaded together) and cannot go wrong in that way.
    """
    parts: list[str] = [
        scope.id,
        str(scope.version),
        scope.status,
        scope.title or "",
        scope.exhibit_label or "",
        scope.trade_name or "",
        scope.division_code or "",
        scope.currency or "",
        str(scope.base_bid_amount or ""),
        str(scope.alternates_amount or ""),
        str(scope.adjustments_amount or ""),
        repr(scope.numbering_style or []),
        repr(sorted((scope.settings or {}).items())),
    ]

    if scope.project is not None:
        parts.append(f"p:{scope.project.name}|{scope.project.number}|{scope.project.location}")
        parts.append(f"p2:{scope.project.owner_name}|{scope.project.architect_name}")
    if scope.bid_package is not None:
        parts.append(
            f"b:{scope.bid_package.number}|{scope.bid_package.name}"
            f"|{scope.bid_package.subcontractor_name}"
        )

    for section in sorted(scope.sections, key=lambda s: s.position):
        parts.append(
            f"s:{section.key}|{section.position}|{int(section.is_enabled)}"
            f"|{section.heading}|{section.body_html or ''}"
        )
        for item in sorted(section.items, key=lambda i: (i.parent_id or "", i.position)):
            parts.append(f"i:{item.id}|{item.parent_id or ''}|{item.position}|{item.text_html}")

    if organization is not None:
        parts.append(f"o:{getattr(organization, 'display_name', '') or ''}")
        parts.append(f"o2:{getattr(organization, 'address', '') or ''}")
        parts.append(f"o3:{getattr(organization, 'phone', '') or ''}")

    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:40]


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def cached(scope: Scope, fmt: str, print_key: str) -> RenderJob | None:
    """A completed render for exactly this document state, if there is one."""
    job = db.session.scalar(
        select(RenderJob)
        .where(
            RenderJob.scope_id == scope.id,
            RenderJob.format == fmt,
            RenderJob.fingerprint == print_key,
            RenderJob.status == COMPLETE,
        )
        .order_by(RenderJob.finished_at.desc())
        .limit(1)
    )
    if job is None or job.result is None:
        return None
    if job.expires_at is not None:
        expires = job.expires_at
        if expires.tzinfo is None:
            from datetime import UTC

            expires = expires.replace(tzinfo=UTC)
        if utcnow() > expires:
            return None
    return job


def pending(scope: Scope, fmt: str, print_key: str) -> RenderJob | None:
    """A job already queued or running for this document state."""
    return db.session.scalar(
        select(RenderJob).where(
            RenderJob.scope_id == scope.id,
            RenderJob.format == fmt,
            RenderJob.fingerprint == print_key,
            RenderJob.status.in_((QUEUED, RUNNING)),
        )
    )


def enqueue(scope: Scope, fmt: str, *, organization: Any = None,
            user_id: str | None = None) -> RenderJob:
    """Queue a render, or return the job already doing this exact work."""
    print_key = fingerprint(scope, organization)

    existing = pending(scope, fmt, print_key)
    if existing is not None:
        return existing

    job = RenderJob(
        organization_id=scope.organization_id,
        scope_id=scope.id,
        requested_by_id=user_id,
        format=fmt,
        fingerprint=print_key,
        status=QUEUED,
    )
    db.session.add(job)
    db.session.commit()
    logger.info("Queued %s render for scope %s (job %s)", fmt, scope.id, job.id)
    return job


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_now(job: RenderJob) -> RenderJob:
    """Render a job in this process and store the result."""
    from ..blueprints.exports.routes import filename_for
    from ..models import Organization
    from .renderers import (
        FORMATS,
        render_docx,
        render_html,
        render_json,
        render_markdown,
        render_pdf,
    )

    renderers = {
        "pdf": render_pdf,
        "docx": render_docx,
        "json": render_json,
        "md": render_markdown,
    }

    scope = db.session.get(Scope, job.scope_id)
    if scope is None:
        job.mark_failed("The scope no longer exists.")
        db.session.commit()
        return job

    organization = db.session.get(Organization, job.organization_id)
    started = time.perf_counter()
    if job.started_at is None:
        # The synchronous path never went through claim_one(), so stamp it here
        # or the job has no duration recorded.
        job.started_at = utcnow()

    try:
        if job.format == "html":
            payload = render_html(
                scope, organization=organization, standalone=True
            ).encode("utf-8")
        else:
            payload = renderers[job.format](scope, organization=organization)

        extension = FORMATS[job.format].extension
        job.mark_complete(payload, filename_for(scope, extension))
        db.session.commit()

        from . import metrics

        metrics.observe(
            "scopemaker_render_seconds", time.perf_counter() - started,
            {"format": job.format},
        )
        metrics.increment("scopemaker_renders_total", {"format": job.format,
                                                       "result": "ok"})
        logger.info(
            "Rendered %s for scope %s in %d ms (%d KB)",
            job.format, scope.id, int((time.perf_counter() - started) * 1000),
            len(payload) // 1024,
        )
    except Exception as exc:
        logger.exception("Render failed for job %s", job.id)
        job.mark_failed(str(exc))
        db.session.commit()

        from . import metrics

        metrics.increment("scopemaker_renders_total", {"format": job.format,
                                                       "result": "failed"})

    return job


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def claim_one(worker: str) -> RenderJob | None:
    """Atomically take the oldest queued job, or return None.

    A conditional UPDATE keeps this portable across SQLite and PostgreSQL. If
    two workers race, one updates zero rows and moves on.
    """
    candidate = db.session.scalar(
        select(RenderJob)
        .where(RenderJob.status == QUEUED)
        .order_by(RenderJob.created_at)
        .limit(1)
    )
    if candidate is None:
        return None

    result: Any = db.session.execute(
        update(RenderJob)
        .where(RenderJob.id == candidate.id, RenderJob.status == QUEUED)
        .values(
            status=RUNNING,
            worker_id=worker,
            started_at=utcnow(),
            attempts=RenderJob.attempts + 1,
        )
    )
    db.session.commit()

    if result.rowcount != 1:
        return None  # somebody else got it
    db.session.refresh(candidate)
    return candidate


def requeue_stale() -> int:
    """Return jobs whose worker died to the queue. Gives up after MAX_ATTEMPTS."""
    from ..models.render import STALE_AFTER

    cutoff = utcnow() - STALE_AFTER
    stale = list(
        db.session.scalars(
            select(RenderJob).where(
                RenderJob.status == RUNNING, RenderJob.started_at < cutoff
            )
        )
    )
    for job in stale:
        if job.attempts >= MAX_ATTEMPTS:
            job.mark_failed(
                f"Gave up after {job.attempts} attempts; the worker did not finish."
            )
            logger.error("Job %s abandoned after %s attempts", job.id, job.attempts)
        else:
            job.requeue()
            logger.warning("Requeued stale job %s (attempt %s)", job.id, job.attempts)
    if stale:
        db.session.commit()
    return len(stale)


def purge_expired() -> int:
    """Drop results past their TTL so the table does not grow without bound."""
    result: Any = db.session.execute(
        delete(RenderJob).where(
            RenderJob.expires_at.is_not(None), RenderJob.expires_at < utcnow()
        )
    )
    db.session.commit()
    return result.rowcount or 0


def run_worker(*, poll_seconds: float = 2.0, once: bool = False,
               max_jobs: int | None = None) -> int:
    """Process the queue until interrupted. Returns how many jobs were run."""
    worker = worker_identity()
    logger.info("Render worker %s started", worker)
    processed = 0
    idle_cycles = 0

    while True:
        requeue_stale()
        job = claim_one(worker)

        if job is None:
            if once:
                break
            idle_cycles += 1
            # Housekeeping only when there is nothing better to do.
            if idle_cycles % 30 == 0:
                purged = purge_expired()
                if purged:
                    logger.info("Purged %s expired render results", purged)
            time.sleep(poll_seconds)
            continue

        idle_cycles = 0
        render_now(job)
        processed += 1
        if max_jobs is not None and processed >= max_jobs:
            break
        if once:
            break

    logger.info("Render worker %s stopping after %s job(s)", worker, processed)
    return processed


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------

def queue_stats(organization_id: str | None = None) -> dict[str, int]:
    stmt = select(RenderJob.status, func.count(RenderJob.id)).group_by(RenderJob.status)
    if organization_id:
        stmt = stmt.where(RenderJob.organization_id == organization_id)
    counts: dict[str, int] = {row[0]: row[1] for row in db.session.execute(stmt).all()}
    return {
        "queued": counts.get(QUEUED, 0),
        "running": counts.get(RUNNING, 0),
        "complete": counts.get(COMPLETE, 0),
        "failed": counts.get(FAILED, 0),
    }


def async_enabled() -> bool:
    """Async rendering needs a worker process, so it is opt-in."""
    return bool(current_app.config.get("RENDER_ASYNC"))
