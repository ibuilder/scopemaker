"""Mapping an OIDC identity onto a local account.

This is the function that decides, on every single-sign-on, *which* local
account an incoming identity becomes. Getting it wrong means duplicate accounts
at best and signing somebody in as somebody else at worst, and it ran with no
direct test coverage at all.

A real identity provider is only needed for the OAuth handshake. The matching
itself takes a plain claims dict, so all of it is testable here.
"""

from __future__ import annotations

import pytest

from scopemaker.errors import ValidationError
from scopemaker.models import Membership, Organization, User
from scopemaker.services.accounts import create_user, provision_sso_user


def claims(**overrides) -> dict:
    base = {
        "sub": "idp-subject-001",
        "email": "newcomer@meridian.example",
        "email_verified": True,
        "name": "New Comer",
    }
    base.update(overrides)
    return base


@pytest.fixture()
def sso(app):
    """An application context with SSO configured the usual way."""
    with app.test_request_context():
        app.config["OIDC_NAME"] = "okta"
        yield app


# ---------------------------------------------------------------------------
# Creating an account
# ---------------------------------------------------------------------------

def test_a_new_identity_creates_an_account(sso, db):
    user = provision_sso_user(claims())
    db.session.commit()

    assert user.email == "newcomer@meridian.example"
    assert user.full_name == "New Comer"
    assert user.sso_subject == "idp-subject-001"
    assert user.sso_provider == "okta"
    assert user.is_sso_only, "an SSO-provisioned account has no password"


def test_the_email_is_normalised(sso, db):
    user = provision_sso_user(claims(email="  NewComer@Meridian.Example  "))
    db.session.commit()
    assert user.email == "newcomer@meridian.example"


def test_a_missing_email_is_refused(sso, db):
    with pytest.raises(ValidationError):
        provision_sso_user(claims(email=""))


def test_the_local_part_becomes_the_name_when_the_idp_sends_none(sso, db):
    user = provision_sso_user(claims(name=None))
    db.session.commit()
    assert user.full_name == "newcomer"


# ---------------------------------------------------------------------------
# Matching an existing account
# ---------------------------------------------------------------------------

def test_the_same_subject_returns_the_same_account(sso, db):
    first = provision_sso_user(claims())
    db.session.commit()
    second = provision_sso_user(claims())
    db.session.commit()

    assert first.id == second.id


def test_a_changed_email_still_matches_on_subject(sso, db):
    """The point of matching on subject: renaming at the IdP is not a new person."""
    original = provision_sso_user(claims())
    db.session.commit()
    original_id = original.id

    moved = provision_sso_user(claims(email="new.address@meridian.example"))
    db.session.commit()

    assert moved.id == original_id, "a rename created a second account"


def test_the_same_email_from_a_different_provider_is_a_different_lookup(sso, db, app):
    """Subject matching is scoped to the provider that issued it."""
    provision_sso_user(claims())
    db.session.commit()

    app.config["OIDC_NAME"] = "azure"
    # Same subject string, different provider: must not match on subject, and
    # falls through to the (verified) email, which is the same person.
    same_person = provision_sso_user(claims())
    db.session.commit()
    assert same_person.sso_provider == "azure"


# ---------------------------------------------------------------------------
# Linking to a pre-existing password account
# ---------------------------------------------------------------------------

def test_a_verified_email_links_to_the_existing_password_account(sso, db, organization):
    """An employee who had a password and now signs in through the IdP keeps
    their account, their memberships and their scopes."""
    existing = create_user(
        email="dana@meridian.example",
        full_name="Dana Reyes",
        password="correct-horse-battery-staple",
    )
    db.session.add(
        Membership(
            organization_id=organization.id, user_id=existing.id, role="admin"
        )
    )
    db.session.commit()
    existing_id = existing.id

    linked = provision_sso_user(
        claims(sub="idp-dana", email="dana@meridian.example", email_verified=True)
    )
    db.session.commit()

    assert linked.id == existing_id
    assert linked.sso_subject == "idp-dana"
    assert [m.role for m in linked.memberships] == ["admin"]


def test_an_unverified_email_cannot_take_over_an_existing_account(sso, db, organization):
    """The account-takeover case.

    Matching a pre-existing local account on the email claim alone trusts the
    identity provider to have verified that address. Plenty do not -- a
    multi-tenant IdP with self-service signup will happily issue a token
    asserting somebody else's address. Anyone who could obtain such a token
    would inherit the victim's account, their organizations and their documents
    without ever knowing the password.

    Subject matching is unaffected: a subject is issued by the IdP and cannot
    be chosen by the person signing in.
    """
    victim = create_user(
        email="dana@meridian.example",
        full_name="Dana Reyes",
        password="correct-horse-battery-staple",
    )
    db.session.add(
        Membership(organization_id=organization.id, user_id=victim.id, role="admin")
    )
    db.session.commit()
    victim_id = victim.id

    with pytest.raises(ValidationError) as caught:
        provision_sso_user(
            claims(
                sub="attacker-subject",
                email="dana@meridian.example",
                email_verified=False,
            )
        )

    assert "verified" in str(caught.value).lower()

    db.session.rollback()
    unchanged = db.session.get(User, victim_id)
    assert unchanged.sso_subject is None, "the attacker's identity was bound"
    assert unchanged.check_password("correct-horse-battery-staple")


def test_a_missing_email_verified_claim_is_treated_as_unverified(sso, db, organization):
    """Absent is not the same as true. An IdP that omits the claim has not
    told us the address is verified, so it cannot be used to claim an account
    that already exists."""
    create_user(
        email="dana@meridian.example",
        full_name="Dana Reyes",
        password="correct-horse-battery-staple",
    )
    db.session.commit()

    payload = claims(sub="unknown-subject", email="dana@meridian.example")
    payload.pop("email_verified")

    with pytest.raises(ValidationError):
        provision_sso_user(payload)


def test_the_verification_requirement_can_be_relaxed_deliberately(
    sso, db, app, organization
):
    """An operator whose IdP omits the claim can opt out, knowingly."""
    create_user(
        email="dana@meridian.example",
        full_name="Dana Reyes",
        password="correct-horse-battery-staple",
    )
    db.session.commit()

    app.config["OIDC_REQUIRE_VERIFIED_EMAIL"] = False
    try:
        payload = claims(sub="some-subject", email="dana@meridian.example")
        payload.pop("email_verified")
        linked = provision_sso_user(payload)
        db.session.commit()
    finally:
        app.config["OIDC_REQUIRE_VERIFIED_EMAIL"] = True

    assert linked.sso_subject == "some-subject"


def test_string_true_counts_as_verified(sso, db):
    """Some providers send the claim as a string rather than a boolean."""
    create_user(
        email="dana@meridian.example",
        full_name="Dana Reyes",
        password="correct-horse-battery-staple",
    )
    db.session.commit()

    linked = provision_sso_user(
        claims(sub="idp-dana", email="dana@meridian.example", email_verified="true")
    )
    db.session.commit()
    assert linked.sso_subject == "idp-dana"


# ---------------------------------------------------------------------------
# Domain allowlist
# ---------------------------------------------------------------------------

def test_a_domain_outside_the_allowlist_is_refused(sso, db, app):
    app.config["OIDC_ALLOWED_DOMAINS"] = ["meridian.example"]
    try:
        with pytest.raises(ValidationError) as caught:
            provision_sso_user(claims(email="someone@elsewhere.example"))
        assert "elsewhere.example" in str(caught.value)
    finally:
        app.config["OIDC_ALLOWED_DOMAINS"] = []


def test_the_allowlist_is_case_insensitive(sso, db, app):
    app.config["OIDC_ALLOWED_DOMAINS"] = ["Meridian.Example"]
    try:
        user = provision_sso_user(claims(email="someone@MERIDIAN.EXAMPLE"))
        db.session.commit()
        assert user.email == "someone@meridian.example"
    finally:
        app.config["OIDC_ALLOWED_DOMAINS"] = []


def test_an_empty_allowlist_permits_any_domain(sso, db, app):
    app.config["OIDC_ALLOWED_DOMAINS"] = []
    user = provision_sso_user(claims(email="someone@anywhere.example"))
    db.session.commit()
    assert user.email == "someone@anywhere.example"


# ---------------------------------------------------------------------------
# Which organization a new user lands in
# ---------------------------------------------------------------------------

def test_a_new_user_joins_the_configured_default_organization(sso, db, app, organization):
    app.config["OIDC_DEFAULT_ORG"] = organization.slug
    try:
        user = provision_sso_user(claims())
        db.session.commit()
    finally:
        app.config["OIDC_DEFAULT_ORG"] = ""

    assert [m.organization_id for m in user.memberships] == [organization.id]
    assert user.memberships[0].role == "editor", "SSO users should not self-promote"


def test_an_unknown_default_org_falls_back_rather_than_failing(sso, db, app):
    """A typo in OIDC_DEFAULT_ORG must not lock everybody out."""
    app.config["OIDC_DEFAULT_ORG"] = "no-such-organization"
    try:
        user = provision_sso_user(claims())
        db.session.commit()
    finally:
        app.config["OIDC_DEFAULT_ORG"] = ""

    assert user.memberships, "the user was left with no organization at all"


def test_two_users_from_one_domain_do_not_share_an_auto_created_org(sso, db, app):
    """Deliberate, and the safe choice.

    With no default organization configured, each new SSO user gets their own,
    named after their email domain. Grouping strangers who happen to share a
    domain would be a data leak the first time somebody signed in with a
    consumer address -- everyone at gmail.com is not one company.
    """
    app.config["OIDC_DEFAULT_ORG"] = ""
    first = provision_sso_user(claims(sub="a", email="a@shared.example"))
    db.session.commit()
    second = provision_sso_user(claims(sub="b", email="b@shared.example"))
    db.session.commit()

    assert first.memberships[0].organization_id != second.memberships[0].organization_id


def test_an_existing_member_is_not_given_another_organization(sso, db, organization):
    """Signing in again must not accumulate memberships."""
    existing = create_user(email="dana@meridian.example", full_name="Dana")
    db.session.add(
        Membership(organization_id=organization.id, user_id=existing.id, role="viewer")
    )
    db.session.commit()

    user = provision_sso_user(
        claims(sub="idp-dana", email="dana@meridian.example", email_verified=True)
    )
    db.session.commit()

    assert len(user.memberships) == 1
    assert user.memberships[0].role == "viewer", "role should not be reset"


def test_the_auto_created_organization_is_named_after_the_domain(sso, db, app):
    app.config["OIDC_DEFAULT_ORG"] = ""
    user = provision_sso_user(claims(email="someone@acme-builders.example"))
    db.session.commit()

    organization = db.session.get(
        Organization, user.memberships[0].organization_id
    )
    assert "acme-builders" in organization.name
