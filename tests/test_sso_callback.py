"""The OIDC callback -- our half of the handshake.

**This is not an integration test.** It does not talk to an identity provider,
and it cannot tell you that your Okta or Entra configuration is right. What it
does cover is everything on this side of the redirect: the CSRF state check, the
provider returning an error, a deactivated account, and the session that results
from a successful sign-in.

Those are the parts written here and therefore the parts that can be got wrong
here. Verifying the other half needs a real issuer and real credentials.
"""

from __future__ import annotations

import pytest

from scopemaker.blueprints.auth.routes import OAUTH_STATE_KEY
from scopemaker.extensions import oauth
from scopemaker.models import Membership, User
from scopemaker.services.accounts import create_user

CLAIMS = {
    "sub": "idp-subject-42",
    "email": "sso.user@meridian.example",
    "email_verified": True,
    "name": "Sso User",
}


class FakeClient:
    """Stands in for the Authlib client, which is the only part that would
    otherwise need a live provider."""

    def __init__(self, claims=None, error: Exception | None = None):
        self._claims = claims
        self._error = error

    def authorize_access_token(self):
        if self._error is not None:
            raise self._error
        return {"userinfo": dict(self._claims)} if self._claims else {}

    def userinfo(self, token=None):
        return dict(self._claims or {})

    def authorize_redirect(self, redirect_uri, state=None):
        from flask import redirect

        return redirect(f"https://idp.example/authorize?state={state}")


@pytest.fixture()
def sso_app(app, monkeypatch):
    """SSO switched on, with the provider replaced.

    Every key is restored, not just the one being turned on. ``app`` is
    session-scoped, so leaving OIDC_NAME as "okta" would silently change the
    provider every later test sees -- the kind of leak that makes a suite pass
    in one order and fail in another.
    """
    keys = ("OIDC_ENABLED", "OIDC_NAME", "OIDC_DEFAULT_ORG", "OIDC_ALLOWED_DOMAINS")
    previous = {key: app.config.get(key) for key in keys}

    app.config["OIDC_ENABLED"] = True
    app.config["OIDC_NAME"] = "okta"
    app.config["OIDC_DEFAULT_ORG"] = ""
    app.config["OIDC_ALLOWED_DOMAINS"] = []
    try:
        yield app
    finally:
        app.config.update(previous)


def use_client(monkeypatch, client_obj):
    monkeypatch.setattr(oauth, "create_client", lambda name: client_obj)


def start_flow(http_client, state: str = "known-state") -> None:
    """Put a state in the session, as /auth/sso would."""
    with http_client.session_transaction() as session:
        session[OAUTH_STATE_KEY] = state


# ---------------------------------------------------------------------------
# The state check
# ---------------------------------------------------------------------------

def test_a_callback_with_no_state_in_the_session_is_refused(sso_app, client, monkeypatch):
    """Landing on the callback without having started a sign-in."""
    use_client(monkeypatch, FakeClient(CLAIMS))

    response = client.get("/auth/sso/callback?state=anything", follow_redirects=True)

    assert response.status_code == 200
    assert "could not be completed" in response.get_data(as_text=True)
    assert User.by_email(CLAIMS["email"]) is None, "an account was provisioned"


def test_a_mismatched_state_is_refused(sso_app, client, monkeypatch):
    """The CSRF property: somebody else's callback must not sign you in."""
    use_client(monkeypatch, FakeClient(CLAIMS))
    start_flow(client, "the-real-state")

    response = client.get(
        "/auth/sso/callback?state=an-attackers-state", follow_redirects=True
    )

    assert "could not be completed" in response.get_data(as_text=True)
    assert User.by_email(CLAIMS["email"]) is None


def test_the_state_is_single_use(sso_app, client, db, monkeypatch):
    """Replaying a callback must not work twice."""
    use_client(monkeypatch, FakeClient(CLAIMS))
    start_flow(client, "one-shot")

    first = client.get("/auth/sso/callback?state=one-shot", follow_redirects=True)
    assert first.status_code == 200

    second = client.get("/auth/sso/callback?state=one-shot", follow_redirects=True)
    assert "could not be completed" in second.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Failures from the provider
# ---------------------------------------------------------------------------

def test_a_provider_error_does_not_leak_its_detail(sso_app, client, monkeypatch):
    """Whatever the IdP said belongs in the log, not on the page."""
    use_client(
        monkeypatch,
        FakeClient(error=RuntimeError("upstream said: client_secret invalid")),
    )
    start_flow(client)

    response = client.get("/auth/sso/callback?state=known-state",
                          follow_redirects=True)
    body = response.get_data(as_text=True)

    assert "Single sign-on failed" in body
    assert "client_secret" not in body


def test_a_rejected_identity_shows_its_reason(sso_app, client, monkeypatch):
    """A ValidationError is ours and is meant to be read -- an unverified email
    or a domain outside the allowlist."""
    use_client(monkeypatch, FakeClient({**CLAIMS, "email": "nobody@elsewhere.example"}))
    sso_app.config["OIDC_ALLOWED_DOMAINS"] = ["meridian.example"]  # fixture restores
    start_flow(client)
    response = client.get("/auth/sso/callback?state=known-state",
                          follow_redirects=True)

    assert "elsewhere.example" in response.get_data(as_text=True)


def test_a_provider_that_returns_no_email_is_refused(sso_app, client, monkeypatch):
    use_client(monkeypatch, FakeClient({"sub": "x", "email": ""}))
    start_flow(client)

    response = client.get("/auth/sso/callback?state=known-state",
                          follow_redirects=True)
    assert "did not return an email" in response.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Success, and the account state that gates it
# ---------------------------------------------------------------------------

def test_a_successful_callback_signs_the_user_in(sso_app, client, db, monkeypatch):
    use_client(monkeypatch, FakeClient(CLAIMS))
    start_flow(client)

    response = client.get("/auth/sso/callback?state=known-state",
                          follow_redirects=True)
    assert response.status_code == 200

    user = User.by_email(CLAIMS["email"])
    assert user is not None
    assert user.last_login_at is not None, "sign-in was not stamped"

    # Genuinely authenticated, not merely provisioned.
    assert client.get("/auth/profile").status_code == 200


def test_a_deactivated_account_is_refused(sso_app, client, db, monkeypatch):
    """The identity provider authenticating somebody does not overrule us
    having switched their account off."""
    account = create_user(
        email=CLAIMS["email"], full_name="Sso User",
        sso_subject=CLAIMS["sub"], sso_provider="okta",
    )
    account.is_active_flag = False
    db.session.commit()

    use_client(monkeypatch, FakeClient(CLAIMS))
    start_flow(client)

    response = client.get("/auth/sso/callback?state=known-state",
                          follow_redirects=True)

    assert "deactivated" in response.get_data(as_text=True)
    assert client.get("/auth/profile").status_code in (302, 401), (
        "a deactivated account was signed in"
    )


def test_an_existing_member_keeps_their_role(sso_app, client, db, organization,
                                             monkeypatch):
    account = create_user(email=CLAIMS["email"], full_name="Sso User")
    db.session.add(
        Membership(organization_id=organization.id, user_id=account.id,
                   role="viewer")
    )
    db.session.commit()

    use_client(monkeypatch, FakeClient(CLAIMS))
    start_flow(client)
    client.get("/auth/sso/callback?state=known-state", follow_redirects=True)

    db.session.refresh(account)
    assert [m.role for m in account.memberships] == ["viewer"]


# ---------------------------------------------------------------------------
# The routes are absent unless SSO is configured
# ---------------------------------------------------------------------------

def test_the_sso_routes_are_404_when_disabled(app, client):
    app.config["OIDC_ENABLED"] = False
    assert client.get("/auth/sso").status_code == 404
    assert client.get("/auth/sso/callback?state=x").status_code == 404


def test_starting_the_flow_stores_a_state_and_redirects(sso_app, client, monkeypatch):
    use_client(monkeypatch, FakeClient(CLAIMS))

    response = client.get("/auth/sso")

    assert response.status_code == 302
    assert "idp.example" in response.headers["Location"]
    with client.session_transaction() as session:
        assert session.get(OAUTH_STATE_KEY), "no state was stored to check later"
