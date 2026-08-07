"""Audit logging.

An audit log is only worth having if it is complete, tamper-resistant and
scoped to the right tenant. These tests hold it to those three things.
"""

from __future__ import annotations

import csv
import io

from scopemaker.models import AuditEvent
from scopemaker.models.audit import AuditAction
from scopemaker.services import audit

from .conftest import login


def actions_for(db, organization=None) -> list[str]:
    query = db.session.query(AuditEvent)
    if organization is not None:
        query = query.filter(AuditEvent.organization_id == organization.id)
    return [e.action for e in query.order_by(AuditEvent.created_at)]


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def test_sign_in_is_recorded(client, db, user, organization):
    login(client, user.email)
    events = db.session.query(AuditEvent).filter_by(action=AuditAction.SIGN_IN).all()
    assert len(events) == 1
    event = events[0]
    assert event.actor_label == user.email
    assert event.user_id == user.id
    assert event.organization_id == organization.id
    assert event.request_id, "the request id should be captured for correlation"


def test_failed_sign_in_is_recorded_with_the_attempt_count(client, db, user):
    login(client, user.email, "wrong-password")
    login(client, user.email, "wrong-password")

    events = (
        db.session.query(AuditEvent)
        .filter_by(action=AuditAction.SIGN_IN_FAILED)
        .order_by(AuditEvent.created_at)
        .all()
    )
    assert len(events) == 2
    assert events[-1].context["attempts"] == 2
    assert events[-1].is_security_event


def test_lockout_is_recorded(client, db, app, user):
    for _ in range(app.config["LOGIN_MAX_ATTEMPTS"]):
        login(client, user.email, "wrong-password")
    assert AuditAction.ACCOUNT_LOCKED in actions_for(db)


def test_failed_sign_in_for_an_unknown_address_records_nothing(client, db):
    """There is no account to attribute it to, and no org to file it under."""
    login(client, "nobody@example.com", "wrong-password")
    assert db.session.query(AuditEvent).count() == 0


def test_role_change_records_before_and_after(auth_client, db, organization, viewer):
    membership = viewer.memberships[0]
    auth_client.post(
        f"/admin/members/{membership.id}/role", data={"role": "editor"},
        follow_redirects=True,
    )
    event = db.session.query(AuditEvent).filter_by(action=AuditAction.ROLE_CHANGED).one()
    assert event.context == {"from": "viewer", "to": "editor"}
    assert event.target_label == viewer.email
    assert viewer.email in event.summary


def test_token_issue_and_revoke_are_recorded(auth_client, db, organization):
    auth_client.post(
        "/admin/tokens",
        data={"name": "CI", "scopes": "read", "expires_days": "30"},
        follow_redirects=True,
    )
    issued = db.session.query(AuditEvent).filter_by(action=AuditAction.TOKEN_ISSUED).one()
    assert issued.context["scopes"] == "read"
    # The prefix is recorded; the secret is not.
    assert "prefix" in issued.context

    from scopemaker.models import ApiToken

    token = db.session.query(ApiToken).one()
    auth_client.post(f"/admin/tokens/{token.id}/revoke", follow_redirects=True)
    assert AuditAction.TOKEN_REVOKED in actions_for(db, organization)


def test_scope_issue_and_revise_are_recorded(auth_client, db, scope, organization):
    auth_client.post(
        f"/scopes/{scope.id}/issue", data={"note": "Sent", "submit": "Issue scope"},
        follow_redirects=True,
    )
    issued = db.session.query(AuditEvent).filter_by(action=AuditAction.SCOPE_ISSUED).one()
    assert issued.target_id == scope.id
    assert issued.context["version"] == 1

    auth_client.post(f"/scopes/{scope.id}/revise", follow_redirects=True)
    revised = db.session.query(AuditEvent).filter_by(action=AuditAction.SCOPE_REVISED).one()
    assert revised.context["version"] == 2


def test_invitation_and_revocation_are_recorded(auth_client, db, organization):
    auth_client.post(
        "/admin/invite", data={"email": "new@meridian.example", "role": "editor"},
        follow_redirects=True,
    )
    invited = db.session.query(AuditEvent).filter_by(action=AuditAction.MEMBER_INVITED).one()
    assert invited.target_label == "new@meridian.example"

    from scopemaker.models import Invitation

    invitation = db.session.query(Invitation).one()
    auth_client.post(f"/admin/invite/{invitation.id}/revoke", follow_redirects=True)
    assert AuditAction.INVITE_REVOKED in actions_for(db, organization)


def test_password_reset_is_recorded_end_to_end(client, db, user):
    import re

    from scopemaker.services import mail

    mail.outbox.clear()
    client.post("/auth/forgot", data={"email": user.email}, follow_redirects=True)
    assert AuditAction.PASSWORD_RESET_REQUESTED in actions_for(db)

    token = re.search(
        r"/auth/reset/([A-Za-z0-9_-]+)",
        next(m for m in mail.outbox if m.to == user.email).text,
    ).group(1)
    client.post(
        f"/auth/reset/{token}",
        data={"password": "a-fresh-passphrase-x", "confirm": "a-fresh-passphrase-x"},
        follow_redirects=True,
    )
    assert AuditAction.PASSWORD_RESET_COMPLETED in actions_for(db)
    mail.outbox.clear()


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

def test_a_failed_audit_write_does_not_break_the_action(app, db, monkeypatch, caplog):
    """An audit failure must never be why a user's action fails."""
    def explode(*args, **kwargs):
        raise RuntimeError("audit table is on fire")

    monkeypatch.setattr(audit, "AuditEvent", explode)
    with app.test_request_context():
        assert audit.record(AuditAction.SIGN_IN, summary="x") is None
    assert "Failed to record audit event" in caplog.text


def test_record_outside_a_request_context_still_works(db, organization):
    """A cron job or CLI command has no request, and must still be auditable.

    The db fixture already holds an app context; nesting another one would tear
    down the session on exit and detach the event we are asserting on.
    """
    event = audit.record(
        AuditAction.INTEGRATION_SYNCED,
        summary="Nightly sync",
        organization_id=organization.id,
        commit=True,
    )
    assert event is not None
    assert event.actor_label == "system"
    assert event.ip_address is None
    assert event.request_id is None


def test_long_values_are_truncated_not_rejected(app, db, organization):
    """User-Agent and summary are attacker-influenced; they must not overflow."""
    with app.test_request_context(headers={"User-Agent": "U" * 5000}):
        event = audit.record(
            AuditAction.SIGN_IN,
            summary="S" * 9000,
            organization_id=organization.id,
            target_label="T" * 900,
            commit=True,
        )
    assert len(event.user_agent) <= audit.MAX_USER_AGENT
    assert len(event.summary) <= 2000
    assert len(event.target_label) <= 255


def test_entries_survive_the_actor_being_deleted(db, organization, viewer):
    """Removing a member must not erase the record of what they did."""
    from scopemaker.extensions import db as database

    event = audit.record(
        AuditAction.SCOPE_ISSUED,
        summary="issued something",
        organization_id=organization.id,
        user_id=viewer.id,
        actor_label=viewer.email,
        commit=True,
    )
    event_id = event.id
    email = viewer.email

    database.session.delete(viewer)
    database.session.commit()

    survivor = database.session.get(AuditEvent, event_id)
    assert survivor is not None
    assert survivor.user_id is None      # FK nulled
    assert survivor.actor_label == email  # but who it was is preserved


# ---------------------------------------------------------------------------
# Querying, isolation and export
# ---------------------------------------------------------------------------

def test_security_filter(db, organization):
    audit.record(AuditAction.SIGN_IN, organization_id=organization.id)
    audit.record(AuditAction.ROLE_CHANGED, organization_id=organization.id)
    db.session.commit()

    everything = audit.query(organization.id)
    security = audit.query(organization.id, security_only=True)
    assert len(everything) == 2
    assert [e.action for e in security] == [AuditAction.ROLE_CHANGED]


def test_query_is_newest_first(db, organization):
    for index in range(3):
        audit.record(
            AuditAction.SIGN_IN, summary=f"event {index}",
            organization_id=organization.id,
        )
        db.session.commit()
    summaries = [e.summary for e in audit.query(organization.id)]
    assert summaries[0] == "event 2"


def test_audit_is_tenant_scoped(db, organization, other_org):
    audit.record(AuditAction.SIGN_IN, summary="ours", organization_id=organization.id)
    audit.record(AuditAction.SIGN_IN, summary="theirs", organization_id=other_org.id)
    db.session.commit()

    assert [e.summary for e in audit.query(organization.id)] == ["ours"]
    assert [e.summary for e in audit.query(other_org.id)] == ["theirs"]


def test_csv_export_shape(db, organization):
    audit.record(
        AuditAction.ROLE_CHANGED, summary="a to b", organization_id=organization.id
    )
    db.session.commit()
    rows = list(csv.reader(io.StringIO(audit.to_csv(audit.query(organization.id)))))
    assert rows[0][:4] == ["Timestamp (UTC)", "Actor", "Action", "Summary"]
    assert rows[1][2] == AuditAction.ROLE_CHANGED


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

def test_audit_page_requires_admin(client, viewer):
    login(client, viewer.email)
    assert client.get("/admin/audit").status_code == 403
    assert client.get("/admin/audit.csv").status_code == 403


def test_audit_page_renders(auth_client, db, organization):
    audit.record(
        AuditAction.ROLE_CHANGED, summary="promoted somebody",
        organization_id=organization.id, commit=True,
    )
    response = auth_client.get("/admin/audit")
    assert response.status_code == 200
    assert b"promoted somebody" in response.data


def test_audit_csv_download(auth_client, db, organization):
    audit.record(
        AuditAction.SIGN_IN, organization_id=organization.id, commit=True
    )
    response = auth_client.get("/admin/audit.csv")
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert b"Timestamp (UTC)" in response.data


def test_another_tenant_cannot_see_the_log(db, client, other_org, organization):
    audit.record(
        AuditAction.ROLE_CHANGED, summary="secret change",
        organization_id=organization.id, commit=True,
    )
    from scopemaker.models import User

    rival = db.session.query(User).filter_by(email="rival@rival.example").one()
    login(client, rival.email)
    response = client.get("/admin/audit")
    assert response.status_code == 200
    assert b"secret change" not in response.data
