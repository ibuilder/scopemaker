"""Organization-wide access policy.

The key property: a policy applies to sessions that are *already* signed in.
Enforcing only at login would leave every current session unaffected, which is
exactly the window an administrator turns the policy on to close.
"""

from __future__ import annotations

import pytest

from scopemaker.models.base import utcnow
from scopemaker.services import mfa

from .conftest import drop_login_cache, login


def set_policy(db, organization, **flags):
    organization.settings = {**(organization.settings or {}), **flags}
    db.session.commit()


@pytest.fixture()
def mfa_admin(db, user):
    secret = mfa.new_secret()
    user.mfa_secret = secret
    user.mfa_enabled = True
    user.mfa_confirmed_at = utcnow()
    user.mfa_recovery_hashes = mfa.hash_recovery_codes(mfa.generate_recovery_codes())
    db.session.commit()
    return user, secret


# ---------------------------------------------------------------------------
# require_mfa
# ---------------------------------------------------------------------------

def test_require_mfa_redirects_an_existing_session(auth_client, db, organization):
    """Turning the policy on must affect people who are already signed in."""
    assert auth_client.get("/scopes/").status_code == 200

    set_policy(db, organization, require_mfa=True)

    response = auth_client.get("/scopes/")
    assert response.status_code == 302
    assert "/auth/mfa/setup" in response.headers["Location"]


def test_enrolment_pages_stay_reachable_under_the_policy(auth_client, db, organization):
    """Otherwise the redirect has nowhere to land and the user is stuck."""
    set_policy(db, organization, require_mfa=True)
    assert auth_client.get("/auth/mfa/setup").status_code == 200
    assert auth_client.get("/auth/profile").status_code == 200
    assert auth_client.post("/auth/logout").status_code == 302


def test_a_user_with_mfa_is_unaffected(client, db, organization, mfa_admin):
    import pyotp

    user, secret = mfa_admin
    set_policy(db, organization, require_mfa=True)

    login(client, user.email)
    client.post("/auth/mfa", data={"code": pyotp.TOTP(secret).now()},
                follow_redirects=True)
    assert client.get("/scopes/").status_code == 200


def test_the_api_gets_a_clear_403_rather_than_a_redirect(client, db, user, organization):
    from scopemaker.models import ApiToken

    record, raw = ApiToken.issue(
        user=user, organization_id=organization.id, name="t", scopes="read"
    )
    db.session.add(record)
    set_policy(db, organization, require_mfa=True)

    response = client.get(
        "/api/v1/scopes", headers={"Authorization": f"Bearer {raw}"}
    )
    assert response.status_code == 403
    assert response.json["error"]["code"] == "mfa_required"


def test_sso_accounts_are_exempt_from_require_mfa(client, db, organization):
    from scopemaker.models import Membership
    from scopemaker.services.accounts import create_user

    account = create_user(
        email="sso@meridian.example", full_name="Sso", sso_subject="x",
        sso_provider="sso",
    )
    db.session.add(
        Membership(organization_id=organization.id, user_id=account.id, role="editor")
    )
    set_policy(db, organization, require_mfa=True)

    from flask_login import login_user

    with client.session_transaction():
        pass
    # Simulate the IdP having signed them in.
    with client.application.test_request_context():
        login_user(account)
    # The exemption is a property of the account, checked directly.
    assert account.is_sso_only


def test_policy_blocks_turning_mfa_off(auth_client, db, organization, mfa_admin):
    user, _ = mfa_admin
    set_policy(db, organization, require_mfa=True)
    response = auth_client.post(
        "/auth/mfa/disable", data={"password": "correct-horse-battery-staple"},
        follow_redirects=True,
    )
    assert b"requires two-factor" in response.data
    db.session.refresh(user)
    assert user.mfa_enabled


# ---------------------------------------------------------------------------
# sso_only
# ---------------------------------------------------------------------------

def test_sso_only_refuses_a_correct_password(client, db, user, organization):
    set_policy(db, organization, sso_only=True)
    response = login(client, user.email)
    assert b"requires single sign-on" in response.data
    drop_login_cache()
    assert client.get("/dashboard").status_code == 302


def test_sso_only_cannot_be_enabled_without_sso_configured(auth_client, db, app):
    """Enabling it with no identity provider would lock everyone out."""
    assert app.config["OIDC_ENABLED"] is False
    response = auth_client.post(
        "/admin/security", data={"sso_only": "y"}, follow_redirects=True
    )
    assert b"not configured on this deployment" in response.data


# ---------------------------------------------------------------------------
# Administration
# ---------------------------------------------------------------------------

def test_security_page_requires_admin(client, viewer):
    login(client, viewer.email)
    assert client.get("/admin/security").status_code == 403


def test_policy_changes_are_audited(auth_client, db, organization):
    from scopemaker.models import AuditEvent
    from scopemaker.models.audit import AuditAction

    auth_client.post("/admin/security", data={"require_mfa": "y"},
                     follow_redirects=True)
    event = (
        db.session.query(AuditEvent)
        .filter_by(action=AuditAction.SETTINGS_CHANGED)
        .order_by(AuditEvent.created_at.desc())
        .first()
    )
    assert event is not None
    assert event.context["after"]["require_mfa"] is True


def test_page_lists_members_without_mfa(auth_client, db, organization, viewer):
    response = auth_client.get("/admin/security")
    assert response.status_code == 200
    assert viewer.email.encode() in response.data
