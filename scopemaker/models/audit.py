"""Append-only audit trail.

Scope revisions already capture *what a document said*. This captures *what
people did*: who signed in, who changed a role, who issued a scope, who
connected an integration, who revoked a token.

Deliberately append-only. Nothing in the application updates or deletes an
entry, and the model exposes no way to. An audit log an administrator can
quietly edit is not evidence of anything.

Entries survive the deletion of the actor: ``user_id`` is nulled on delete but
``actor_label`` keeps the email as it was at the time, so removing a member does
not erase what they did.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import JSONType, Model


class AuditAction:
    """The vocabulary of recordable actions.

    Grouped by subject so a reviewer can filter to "everything about access" or
    "everything about this scope" without knowing every constant.
    """

    # Authentication
    SIGN_IN = "auth.sign_in"
    SIGN_IN_FAILED = "auth.sign_in_failed"
    SIGN_OUT = "auth.sign_out"
    ACCOUNT_LOCKED = "auth.account_locked"
    PASSWORD_RESET_REQUESTED = "auth.password_reset_requested"
    PASSWORD_RESET_COMPLETED = "auth.password_reset_completed"
    PASSWORD_CHANGED = "auth.password_changed"
    SESSIONS_REVOKED = "auth.sessions_revoked"
    MFA_ENABLED = "auth.mfa_enabled"
    MFA_DISABLED = "auth.mfa_disabled"
    MFA_RECOVERY_USED = "auth.mfa_recovery_used"

    # Membership and access
    MEMBER_INVITED = "access.member_invited"
    MEMBER_JOINED = "access.member_joined"
    MEMBER_REMOVED = "access.member_removed"
    ROLE_CHANGED = "access.role_changed"
    INVITE_REVOKED = "access.invite_revoked"
    TOKEN_ISSUED = "access.token_issued"
    TOKEN_REVOKED = "access.token_revoked"

    # Documents
    SCOPE_CREATED = "scope.created"
    SCOPE_ISSUED = "scope.issued"
    SCOPE_REVISED = "scope.revised"
    SCOPE_ARCHIVED = "scope.archived"
    SCOPE_EXPORTED = "scope.exported"

    # Library
    CLAUSE_CREATED = "library.clause_created"
    CLAUSE_UPDATED = "library.clause_updated"
    CLAUSE_DELETED = "library.clause_deleted"
    CLAUSE_SUPPRESSED = "library.clause_suppressed"

    # Integrations and settings
    INTEGRATION_CONNECTED = "integration.connected"
    INTEGRATION_DISCONNECTED = "integration.disconnected"
    INTEGRATION_SYNCED = "integration.synced"
    SETTINGS_CHANGED = "settings.changed"


#: Actions a reviewer almost always wants to see first.
SECURITY_ACTIONS: frozenset[str] = frozenset(
    {
        AuditAction.SIGN_IN_FAILED,
        AuditAction.ACCOUNT_LOCKED,
        AuditAction.PASSWORD_RESET_COMPLETED,
        AuditAction.SESSIONS_REVOKED,
        AuditAction.MFA_DISABLED,
        AuditAction.MFA_RECOVERY_USED,
        AuditAction.ROLE_CHANGED,
        AuditAction.MEMBER_REMOVED,
        AuditAction.TOKEN_ISSUED,
        AuditAction.TOKEN_REVOKED,
        AuditAction.INTEGRATION_CONNECTED,
        AuditAction.INTEGRATION_DISCONNECTED,
    }
)

ACTION_LABELS: dict[str, str] = {
    AuditAction.SIGN_IN: "Signed in",
    AuditAction.SIGN_IN_FAILED: "Failed sign-in",
    AuditAction.SIGN_OUT: "Signed out",
    AuditAction.ACCOUNT_LOCKED: "Account locked",
    AuditAction.PASSWORD_RESET_REQUESTED: "Password reset requested",
    AuditAction.PASSWORD_RESET_COMPLETED: "Password reset completed",
    AuditAction.PASSWORD_CHANGED: "Password changed",
    AuditAction.SESSIONS_REVOKED: "All sessions revoked",
    AuditAction.MFA_ENABLED: "Two-factor enabled",
    AuditAction.MFA_DISABLED: "Two-factor disabled",
    AuditAction.MFA_RECOVERY_USED: "Recovery code used",
    AuditAction.MEMBER_INVITED: "Member invited",
    AuditAction.MEMBER_JOINED: "Member joined",
    AuditAction.MEMBER_REMOVED: "Member removed",
    AuditAction.ROLE_CHANGED: "Role changed",
    AuditAction.INVITE_REVOKED: "Invitation revoked",
    AuditAction.TOKEN_ISSUED: "API token issued",
    AuditAction.TOKEN_REVOKED: "API token revoked",
    AuditAction.SCOPE_CREATED: "Scope created",
    AuditAction.SCOPE_ISSUED: "Scope issued",
    AuditAction.SCOPE_REVISED: "Scope revised",
    AuditAction.SCOPE_ARCHIVED: "Scope archived",
    AuditAction.SCOPE_EXPORTED: "Scope exported",
    AuditAction.CLAUSE_CREATED: "Clause created",
    AuditAction.CLAUSE_UPDATED: "Clause updated",
    AuditAction.CLAUSE_DELETED: "Clause deleted",
    AuditAction.CLAUSE_SUPPRESSED: "Clause hidden",
    AuditAction.INTEGRATION_CONNECTED: "Integration connected",
    AuditAction.INTEGRATION_DISCONNECTED: "Integration disconnected",
    AuditAction.INTEGRATION_SYNCED: "Integration synced",
    AuditAction.SETTINGS_CHANGED: "Settings changed",
}


class AuditEvent(Model):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_org_created", "organization_id", "created_at"),
        Index("ix_audit_org_action", "organization_id", "action"),
        Index("ix_audit_target", "target_type", "target_id"),
    )

    # Nullable: sign-in failures happen before any organization is known.
    organization_id: Mapped[str | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )

    # SET NULL rather than CASCADE -- removing a member must not erase the
    # record of what they did.
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    #: The actor's email as it was at the time, so the entry stays readable.
    actor_label: Mapped[str] = mapped_column(String(255), nullable=False, default="system")

    action: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")

    target_type: Mapped[str | None] = mapped_column(String(40))
    target_id: Mapped[str | None] = mapped_column(String(64))
    target_label: Mapped[str | None] = mapped_column(String(255))

    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    request_id: Mapped[str | None] = mapped_column(String(40))

    #: Extra structured detail: before/after values, counts, error codes.
    context: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)

    @property
    def label(self) -> str:
        return ACTION_LABELS.get(self.action, self.action)

    @property
    def is_security_event(self) -> bool:
        return self.action in SECURITY_ACTIONS

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "at": self.created_at.isoformat() if self.created_at else None,
            "actor": self.actor_label,
            "user_id": self.user_id,
            "action": self.action,
            "label": self.label,
            "summary": self.summary,
            "target": {
                "type": self.target_type,
                "id": self.target_id,
                "label": self.target_label,
            }
            if self.target_type
            else None,
            "ip_address": self.ip_address,
            "request_id": self.request_id,
            "context": self.context or {},
        }
