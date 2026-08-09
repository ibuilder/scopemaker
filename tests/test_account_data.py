"""Exporting and deleting an account.

The interesting cases are all about what must *survive* a deletion. An audit
log that could be erased by deleting the account that misbehaved would be
worthless, and a shared organization must not lose its documents because one
member left.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from scopemaker.errors import ValidationError
from scopemaker.models import (
    ApiToken,
    AuditEvent,
    Membership,
    Organization,
    Scope,
    User,
)
from scopemaker.services import account_data
from scopemaker.services.accounts import create_organization, create_user

from .conftest import login

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture()
def colleague(db, organization) -> User:
    """A second admin, so the first is not the last one."""
    account = create_user(
        email="colleague@meridian.example",
        full_name="Cody Colleague",
        password=PASSWORD,
    )
    db.session.add(
        Membership(organization_id=organization.id, user_id=account.id, role="admin")
    )
    db.session.commit()
    return account


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def test_export_contains_the_account_and_its_memberships(app, db, user, organization):
    payload = account_data.export_account(user)

    assert payload["account"]["email"] == user.email
    assert payload["account"]["full_name"] == user.full_name
    assert [o["name"] for o in payload["organizations"]] == [organization.name]
    assert payload["organizations"][0]["role"] == "admin"


def test_export_never_includes_secrets(app, db, user, organization):
    """A data export must not become a credential dump."""
    token, raw = ApiToken.issue(
        user=user, organization_id=organization.id, name="t", scopes="read"
    )
    db.session.add(token)
    db.session.commit()

    body = json.dumps(account_data.export_account(user))

    assert user.password_hash not in body
    assert token.token_hash not in body
    assert raw not in body
    assert "password_hash" not in body
    assert "mfa_secret" not in body
    assert "_mfa_secret" not in body
    # The prefix is not secret and is how a token is identified in the UI.
    assert token.token_prefix in body


def test_export_lists_authored_scopes_without_their_contents(
    app, db, user, organization, scope
):
    """Scopes belong to the organization, not to the member who typed them."""
    payload = account_data.export_account(user)

    listed = payload["scopes_authored"]
    assert [s["id"] for s in listed] == [scope.id]
    assert listed[0]["title"] == scope.title

    body = json.dumps(payload)
    first_item = next(
        item for section in scope.sections for item in section.items if item.text_html
    )
    assert first_item.text_html not in body


def test_export_is_json_serialisable(app, db, user, organization, scope):
    json.dumps(account_data.export_account(user))


def test_export_route_downloads_a_file(auth_client, user):
    response = auth_client.get("/auth/account/export")
    assert response.status_code == 200
    assert response.mimetype == "application/json"
    assert "attachment" in response.headers["Content-Disposition"]
    assert json.loads(response.get_data())["account"]["email"] == user.email


def test_exporting_is_recorded(auth_client, db, user):
    auth_client.get("/auth/account/export")
    actions = db.session.scalars(
        select(AuditEvent.action).where(AuditEvent.user_id == user.id)
    ).all()
    assert "access.account_exported" in actions


# ---------------------------------------------------------------------------
# Deletion: what stops it
# ---------------------------------------------------------------------------

def test_the_last_admin_of_a_shared_org_cannot_delete(app, db, user, viewer):
    """Leaving would strand the viewer with nobody able to administer them."""
    blockers = account_data.deletion_blockers(user)
    assert blockers
    assert "only administrator" in blockers[0]

    with pytest.raises(ValidationError):
        account_data.delete_account(user)

    assert db.session.get(User, user.id) is not None


def test_one_of_two_admins_can_delete(app, db, user, colleague):
    assert account_data.deletion_blockers(user) == []


def test_a_sole_member_can_delete_and_takes_the_org(app, db, user, organization):
    """Nobody else could ever sign in to it, so it is not left behind."""
    assert account_data.deletion_blockers(user) == []
    doomed = account_data.organizations_deleted_with(user)
    assert [o.id for o in doomed] == [organization.id]


# ---------------------------------------------------------------------------
# Deletion: what survives
# ---------------------------------------------------------------------------

def test_deletion_keeps_the_audit_trail_and_the_actor_label(
    app, db, user, colleague, organization
):
    """The whole point of an append-only log."""
    from scopemaker.services import audit

    email = user.email
    audit.record(
        audit.AuditAction.SIGN_IN,
        summary=f"{email} signed in",
        organization_id=organization.id,
        user_id=user.id,
        actor_label=email,
        commit=True,
    )

    account_data.delete_account(user)

    events = db.session.scalars(
        select(AuditEvent).where(AuditEvent.actor_label == email)
    ).all()
    assert events, "the audit trail was erased with the account"
    assert all(e.user_id is None for e in events), "foreign key should be nulled"
    assert any(e.action == "access.account_deleted" for e in events)


def test_a_shared_organization_keeps_its_scopes(
    app, db, user, colleague, organization, scope
):
    scope_id = scope.id
    account_data.delete_account(user)

    survivor = db.session.get(Scope, scope_id)
    assert survivor is not None, "the organization lost a document"
    assert survivor.created_by_id is None, "authorship link should be nulled"
    assert db.session.get(Organization, organization.id) is not None


def test_the_users_own_rows_are_removed(app, db, user, colleague, organization):
    token, _raw = ApiToken.issue(
        user=user, organization_id=organization.id, name="t", scopes="read"
    )
    db.session.add(token)
    db.session.commit()
    user_id, token_id = user.id, token.id

    account_data.delete_account(user)

    assert db.session.get(User, user_id) is None
    assert db.session.get(ApiToken, token_id) is None
    assert db.session.scalars(
        select(Membership).where(Membership.user_id == user_id)
    ).all() == []


def test_a_sole_member_deletion_removes_the_organization(app, db, user, organization, scope):
    organization_id, scope_id = organization.id, scope.id

    summary = account_data.delete_account(user)

    assert summary["organizations_deleted"] == [organization.name]
    assert db.session.get(Organization, organization_id) is None
    assert db.session.get(Scope, scope_id) is None, "org content should go with it"


def test_another_tenants_data_is_untouched(app, db, user, organization, other_org):
    before = db.session.scalar(
        select(Organization).where(Organization.id == other_org.id)
    )
    assert before is not None

    account_data.delete_account(user)

    assert db.session.get(Organization, other_org.id) is not None


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------

def test_delete_page_names_the_organizations_that_will_go(auth_client, organization):
    response = auth_client.get("/auth/account/delete")
    assert response.status_code == 200
    assert organization.name in response.get_data(as_text=True)


def test_delete_page_shows_blockers_instead_of_a_form(auth_client, user, viewer):
    response = auth_client.get("/auth/account/delete")
    body = response.get_data(as_text=True)
    assert "only administrator" in body
    assert "Delete my account" not in body


def test_the_wrong_email_does_not_delete(auth_client, db, user):
    response = auth_client.post(
        "/auth/account/delete",
        data={"confirm_email": "someone@else.example", "password": PASSWORD},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert db.session.get(User, user.id) is not None


def test_the_wrong_password_does_not_delete(auth_client, db, user):
    response = auth_client.post(
        "/auth/account/delete",
        data={"confirm_email": user.email, "password": "not-the-password"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert db.session.get(User, user.id) is not None


def test_confirming_deletes_and_signs_out(client, db, organization):
    account = create_user(
        email="leaver@meridian.example", full_name="Lee Leaver", password=PASSWORD
    )
    db.session.add(
        Membership(organization_id=organization.id, user_id=account.id, role="editor")
    )
    db.session.commit()
    user_id = account.id

    login(client, account.email, PASSWORD)
    response = client.post(
        "/auth/account/delete",
        data={"confirm_email": account.email, "password": PASSWORD},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert db.session.get(User, user_id) is None
    # The session must not survive the account.
    assert client.get("/auth/profile").status_code in (302, 401)


def test_deleting_requires_a_signed_in_user(client):
    assert client.get("/auth/account/delete").status_code in (302, 401)
    assert client.get("/auth/account/export").status_code in (302, 401)


def test_a_member_cannot_delete_someone_else(auth_client, db, viewer):
    """There is no route that takes a user id, and that is deliberate."""
    before = db.session.get(User, viewer.id)
    assert before is not None
    response = auth_client.post(
        "/auth/account/delete",
        data={"confirm_email": viewer.email, "password": PASSWORD},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert db.session.get(User, viewer.id) is not None


def test_an_sso_only_account_needs_no_password(app, db, organization, client):
    account = create_user(
        email="sso@meridian.example",
        full_name="Sam SSO",
        sso_subject="abc123",
        sso_provider="okta",
    )
    db.session.add(
        Membership(organization_id=organization.id, user_id=account.id, role="editor")
    )
    db.session.commit()
    assert account.is_sso_only

    from flask_login import login_user

    with client.session_transaction():
        pass
    with app.test_request_context():
        login_user(account)

    # Exercised at the service level: the route's password branch is skipped
    # for SSO-only accounts, and there is no password to check.
    assert account_data.deletion_blockers(account) == []
    account_data.delete_account(account)
    assert db.session.get(User, account.id) is None


def test_deleting_an_org_with_other_members_is_not_offered(app, db, user, colleague):
    """Two members means the organization outlives either of them."""
    assert account_data.organizations_deleted_with(user) == []


def test_second_organization_membership_is_handled(app, db, user, colleague):
    """Sole member of one org, co-admin of another: only the lonely one goes."""
    solo = create_organization("Solo Consulting")
    db.session.add(
        Membership(organization_id=solo.id, user_id=user.id, role="admin")
    )
    db.session.commit()

    doomed = account_data.organizations_deleted_with(user)
    assert [o.name for o in doomed] == ["Solo Consulting"]


def test_deleting_a_sole_member_org_does_not_erase_the_deletion_record(
    app, db, user, organization
):
    """The record of a destructive act must outlive what it destroyed.

    audit_events.organization_id is ON DELETE CASCADE, and audit.record()
    defaults that column to the actor's active organization. Left to the
    default, deleting a sole-member organization took the ACCOUNT_DELETED and
    ORGANIZATION_DELETED rows with it -- the audit trail of the deletion wiped
    out by the deletion, inside the transaction that wrote it. Nothing failed;
    the rows simply were not there afterwards.

    The org's own history going with the org is intended. The fact that
    somebody deleted it is not part of that history.
    """
    from flask_login import login_user

    from scopemaker.services import audit

    email, org_name = user.email, organization.name
    assert [o.id for o in account_data.organizations_deleted_with(user)] == [
        organization.id
    ], "fixture should leave this user as the only member"

    with app.test_request_context():
        login_user(user)
        audit.record(audit.AuditAction.SIGN_IN, summary=f"{email} signed in",
                     commit=True)
        account_data.delete_account(user)

    surviving = db.session.scalars(select(AuditEvent)).all()
    actions = {event.action for event in surviving}

    assert "access.account_deleted" in actions, (
        "the record of the account deletion was destroyed by the deletion"
    )
    assert "access.organization_deleted" in actions, (
        "the record of the organization deletion was destroyed by it"
    )

    deletion = next(e for e in surviving if e.action == "access.account_deleted")
    assert deletion.actor_label == email, "who did it must survive"
    assert deletion.user_id is None, "the foreign key should be nulled"

    removed = next(
        e for e in surviving if e.action == "access.organization_deleted"
    )
    assert removed.target_label == org_name, "which organization must survive"
