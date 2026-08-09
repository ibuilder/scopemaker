"""Recording audit events.

``record()`` is deliberately forgiving: an audit write must never be the reason
a user's action fails. If the insert blows up, it is logged loudly and the
caller carries on. The alternative -- a failed audit write rolling back a
successful role change -- is worse than a gap in the log, and the gap is
visible in the application log either way.

It is, however, written in the *same transaction* as the action it describes
whenever the caller commits afterwards. That is what stops the log and reality
diverging: if the role change rolls back, so does the entry saying it happened.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import g, has_request_context, request
from flask_login import current_user
from sqlalchemy import select

from ..extensions import db
from ..models.audit import ACTION_LABELS, SECURITY_ACTIONS, AuditAction, AuditEvent

logger = logging.getLogger(__name__)

#: User-Agent strings are attacker-controlled; cap what we store.
MAX_USER_AGENT = 300


def _actor() -> tuple[str | None, str]:
    """(user id, label) for whoever is acting, without assuming a login."""
    try:
        if current_user and current_user.is_authenticated:
            user = current_user._get_current_object()
            return user.id, (user.email or user.full_name or "unknown")
    except Exception:
        pass
    return None, "system"


def record(
    action: str,
    *,
    summary: str = "",
    organization_id: str | None = None,
    user_id: str | None = None,
    actor_label: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    target_label: str | None = None,
    context: dict[str, Any] | None = None,
    commit: bool = False,
    inherit_organization: bool = True,
) -> AuditEvent | None:
    """Append an audit entry. Never raises.

    Pass ``commit=True`` only when the surrounding code will not commit for
    itself -- for example a failed sign-in, which has no other state to save.

    ``inherit_organization=False`` keeps the entry unattached to any
    organization. ``organization_id`` defaults to the actor's active
    organization, and passing ``None`` cannot express "no organization"
    because that is also the parameter's default -- the fallback fires either
    way. It matters because ``audit_events.organization_id`` is ON DELETE
    CASCADE: an entry recording the deletion *of* an organization must not be
    attached to it, or the deletion takes the record of itself away.
    """
    try:
        actor_id, label = _actor()
        if user_id is None:
            user_id = actor_id
        if actor_label is None:
            actor_label = label

        if organization_id is None and inherit_organization:
            try:
                if current_user and current_user.is_authenticated:
                    organization_id = current_user.active_organization_id
            except Exception:
                organization_id = None

        event = AuditEvent(
            organization_id=organization_id,
            user_id=user_id,
            actor_label=(actor_label or "system")[:255],
            action=action,
            summary=(summary or ACTION_LABELS.get(action, action))[:2000],
            target_type=target_type,
            target_id=str(target_id)[:64] if target_id else None,
            target_label=target_label[:255] if target_label else None,
            context=context or {},
        )

        if has_request_context():
            event.ip_address = (request.remote_addr or "")[:64] or None
            event.user_agent = (request.headers.get("User-Agent") or "")[
                :MAX_USER_AGENT
            ] or None
            event.request_id = getattr(g, "request_id", None)

        db.session.add(event)
        if commit:
            db.session.commit()
        return event
    except Exception:
        logger.exception("Failed to record audit event %r", action)
        return None


def query(
    organization_id: str,
    *,
    action: str | None = None,
    security_only: bool = False,
    user_id: str | None = None,
    target_id: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[AuditEvent]:
    """Most recent entries first."""
    stmt = select(AuditEvent).where(AuditEvent.organization_id == organization_id)
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    if security_only:
        stmt = stmt.where(AuditEvent.action.in_(SECURITY_ACTIONS))
    if user_id:
        stmt = stmt.where(AuditEvent.user_id == user_id)
    if target_id:
        stmt = stmt.where(AuditEvent.target_id == target_id)
    stmt = stmt.order_by(AuditEvent.created_at.desc()).limit(limit).offset(offset)
    return list(db.session.scalars(stmt))


def to_csv(events: list[AuditEvent]) -> str:
    """Export for an auditor who wants it in a spreadsheet."""
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        ["Timestamp (UTC)", "Actor", "Action", "Summary", "Target", "IP", "Request id"]
    )
    for event in events:
        writer.writerow(
            [
                event.created_at.isoformat() if event.created_at else "",
                event.actor_label,
                event.action,
                event.summary,
                event.target_label or event.target_id or "",
                event.ip_address or "",
                event.request_id or "",
            ]
        )
    return buffer.getvalue()


__all__ = ["AuditAction", "query", "record", "to_csv"]
