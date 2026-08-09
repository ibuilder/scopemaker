"""Exporting an account's data, and deleting an account.

Two things a person should be able to do with their own account without filing
a support ticket. Both have edges that matter more than the happy path.

**What an export is not.** A departing member's personal data is not the same as
their employer's documents. Scopes belong to the organization that paid for
them, so the export lists the ones this person authored -- id, title, when --
without their contents. Anything else would turn "download my data" into a way
to walk out with the clause library. Members who want the documents can already
export each scope from the app, which is an organization-scoped action with
organization-scoped permissions.

**What deletion preserves.** The audit log outlives its actor: ``user_id`` is
``ON DELETE SET NULL`` but ``actor_label`` keeps the email, so removing an
account cannot erase who did what. Scopes, revisions and templates likewise
keep their content and lose only the authorship link. The rows that genuinely
belong to the person -- memberships, API tokens, reset tokens -- cascade away.

**What it does not.** ``audit_events.organization_id`` is ``ON DELETE
CASCADE``, so when a sole-member organization goes, that organization's own
audit history goes with it, along with its projects and scopes. That is
intended. What must not go is the record *of* the deletion, which is why the
two entries written below are deliberately unattached to any organization --
see the comment in ``delete_account``.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select

from ..errors import ValidationError
from ..extensions import db
from ..models import (
    ApiToken,
    AuditEvent,
    Membership,
    Organization,
    Scope,
    User,
)
from ..models.base import utcnow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _isoformat(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def export_account(user: User) -> dict[str, Any]:
    """Everything held about this person, as a JSON-serialisable dict."""
    memberships = list(
        db.session.scalars(
            select(Membership).where(Membership.user_id == user.id)
        )
    )

    authored = list(
        db.session.scalars(
            select(Scope)
            .where(Scope.created_by_id == user.id)
            .order_by(Scope.created_at)
        )
    )

    events = list(
        db.session.scalars(
            select(AuditEvent)
            .where(AuditEvent.user_id == user.id)
            .order_by(AuditEvent.created_at)
        )
    )

    tokens = list(
        db.session.scalars(select(ApiToken).where(ApiToken.user_id == user.id))
    )

    return {
        "exported_at": _isoformat(utcnow()),
        "format": "scopemaker.account-export.v1",
        "notice": (
            "Scopes are listed without their contents because they belong to "
            "the organization, not to an individual member. Export a scope "
            "from the application to obtain its text."
        ),
        "account": {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "is_active": user.is_active,
            "is_superuser": user.is_superuser,
            "created_at": _isoformat(user.created_at),
            "last_login_at": _isoformat(user.last_login_at),
            # Whether two-factor is on is personal data; the secret is not, and
            # is never exported in any form.
            "mfa_enabled": user.mfa_enabled,
            "sso_provider": user.sso_provider,
        },
        "organizations": [
            {
                "id": m.organization_id,
                "name": m.organization.name if m.organization else None,
                "role": m.role,
                "joined_at": _isoformat(m.created_at),
            }
            for m in memberships
        ],
        "scopes_authored": [
            {
                "id": s.id,
                "title": s.title,
                "exhibit_label": s.exhibit_label,
                "division_code": s.division_code,
                "status": s.status,
                "version": s.version,
                "organization_id": s.organization_id,
                "created_at": _isoformat(s.created_at),
            }
            for s in authored
        ],
        "api_tokens": [
            {
                "name": t.name,
                "scopes": t.scopes,
                "prefix": t.token_prefix,
                "created_at": _isoformat(t.created_at),
                "last_used_at": _isoformat(t.last_used_at),
                "revoked_at": _isoformat(t.revoked_at),
            }
            for t in tokens
        ],
        "audit_events": [
            {
                "action": e.action,
                "summary": e.summary,
                "organization_id": e.organization_id,
                "ip_address": e.ip_address,
                "created_at": _isoformat(e.created_at),
            }
            for e in events
        ],
    }


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

def _admin_count(organization_id: str) -> int:
    return db.session.scalar(
        select(func.count(Membership.id)).where(
            Membership.organization_id == organization_id,
            Membership.role == "admin",
        )
    ) or 0


def _member_count(organization_id: str) -> int:
    return db.session.scalar(
        select(func.count(Membership.id)).where(
            Membership.organization_id == organization_id
        )
    ) or 0


def deletion_blockers(user: User) -> list[str]:
    """Reasons this account cannot be deleted yet.

    Only one: being the last administrator of an organization that other people
    still belong to. Deleting would leave those members with nobody able to
    manage them, invite anyone, or issue a scope.
    """
    blockers = []
    for membership in user.memberships:
        if membership.role != "admin":
            continue
        organization = membership.organization
        if organization is None:
            continue
        if _admin_count(organization.id) <= 1 and _member_count(organization.id) > 1:
            blockers.append(
                f"You are the only administrator of {organization.name}. "
                "Promote another administrator before deleting your account."
            )
    return blockers


def organizations_deleted_with(user: User) -> list[Organization]:
    """Organizations that would be destroyed along with this account.

    An organization whose only member leaves is unreachable afterwards -- no
    one can sign in to it, so its projects and scopes are dead data. Rather
    than leave that lying in the database, deletion takes it too. The caller is
    expected to name these in the confirmation, because it is the destructive
    part people do not anticipate.
    """
    doomed = []
    for membership in user.memberships:
        organization = membership.organization
        if organization is not None and _member_count(organization.id) == 1:
            doomed.append(organization)
    return doomed


def delete_account(user: User) -> dict[str, Any]:
    """Delete the account. Returns a summary of what went with it.

    Raises ``ValidationError`` if a blocker applies. Audit entries are written
    *before* the delete so they capture the actor's own label, and they survive
    it: the foreign key nulls out but ``actor_label`` remains.
    """
    from . import audit

    blockers = deletion_blockers(user)
    if blockers:
        raise ValidationError(" ".join(blockers))

    doomed = organizations_deleted_with(user)
    email = user.email
    summary = {
        "email": email,
        "organizations_deleted": [o.name for o in doomed],
        "scopes_retained": db.session.scalar(
            select(func.count(Scope.id)).where(Scope.created_by_id == user.id)
        ) or 0,
    }

    # Both records are deliberately unattached to any organization.
    #
    # audit_events.organization_id is ON DELETE CASCADE, and audit.record()
    # defaults that column to the actor's active organization. Leave it to the
    # default and deleting a sole-member organization takes these two rows with
    # it -- the record of the deletion destroyed by the deletion, in the same
    # transaction that wrote it. Passing organization_id=None does not help:
    # None is also the parameter's default, so the fallback still fires. Hence
    # the explicit flag. Which organization was removed is kept in
    # target_id/target_label and in the summary text, neither a foreign key.
    #
    # The organization's *own* history does go with the organization. That is
    # intended: its projects, scopes and members are being removed too. What has
    # to outlive it is the fact that somebody deleted it, and who.
    for organization in doomed:
        audit.record(
            audit.AuditAction.ORGANIZATION_DELETED,
            summary=f"{organization.name} deleted with its last member {email}",
            inherit_organization=False,
            user_id=user.id,
            actor_label=email,
            target_type="organization",
            target_id=organization.id,
            target_label=organization.name,
        )

    audit.record(
        audit.AuditAction.ACCOUNT_DELETED,
        summary=f"{email} deleted their own account",
        inherit_organization=False,
        user_id=user.id,
        actor_label=email,
        target_type="user",
        target_id=user.id,
        target_label=email,
    )
    db.session.flush()

    # Templates, scopes and revisions keep their content and lose only the
    # authorship link -- every one of those foreign keys is ON DELETE SET NULL.
    # Order matters: both the user and the organization cascade to the same
    # membership rows. Removing the organization first leaves the ORM trying to
    # delete a membership that has already gone, which is harmless but emits a
    # SAWarning that reads like a real problem.
    db.session.delete(user)
    db.session.flush()
    for organization in doomed:
        db.session.delete(organization)
    db.session.commit()

    logger.info(
        "Account %s deleted (%s organization(s) removed)", email, len(doomed)
    )
    return summary
