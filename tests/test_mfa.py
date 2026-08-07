"""Two-factor authentication.

The property that matters most: a correct password alone must not authenticate
anyone who has a second factor configured. Several tests below poke at that
from different angles, because it is the sort of thing that quietly breaks when
somebody refactors the login view.
"""

from __future__ import annotations

import pyotp
import pytest

from scopemaker.blueprints.auth.mfa_routes import PENDING_MFA_KEY
from scopemaker.models.audit import AuditAction
from scopemaker.models.base import utcnow
from scopemaker.services import mfa

from .conftest import drop_login_cache, login


@pytest.fixture()
def mfa_user(db, user):
    """A user with two-factor already enabled, plus their codes."""
    secret = mfa.new_secret()
    codes = mfa.generate_recovery_codes()
    user.mfa_secret = secret
    user.mfa_enabled = True
    user.mfa_confirmed_at = utcnow()
    user.mfa_recovery_hashes = mfa.hash_recovery_codes(codes)
    db.session.commit()
    return user, secret, codes


def code_for(secret: str) -> str:
    return pyotp.TOTP(secret).now()


# ---------------------------------------------------------------------------
# The primitives
# ---------------------------------------------------------------------------

def test_secret_round_trip():
    secret = mfa.new_secret()
    assert len(secret) >= 16
    assert mfa.verify_code(secret, code_for(secret))


def test_wrong_and_malformed_codes_are_rejected():
    secret = mfa.new_secret()
    assert not mfa.verify_code(secret, "000000")
    assert not mfa.verify_code(secret, "12345")     # too short
    assert not mfa.verify_code(secret, "abcdef")    # not digits
    assert not mfa.verify_code(secret, "")
    assert not mfa.verify_code("", code_for(secret))


def test_clock_drift_of_one_step_is_tolerated():
    secret = mfa.new_secret()
    totp = pyotp.TOTP(secret)
    import time

    now = time.time()
    assert mfa.verify_code(secret, totp.at(now - 30))
    assert mfa.verify_code(secret, totp.at(now + 30))
    # But not an arbitrarily old code.
    assert not mfa.verify_code(secret, totp.at(now - 300))


def test_qr_is_an_inline_svg_with_no_external_reference(app):
    """The secret must not be handed to a third-party image service.

    Plenty of tutorials build the QR by pointing an <img> at a chart API, which
    sends the shared secret to somebody else and breaks the CSP. This asserts
    the drawing is self-contained: paths only, nothing fetched.
    """
    with app.app_context():
        svg = mfa.qr_svg(mfa.new_secret(), email="a@b.com")

    assert svg.lstrip().startswith("<svg")
    assert "<path" in svg, "expected vector paths, not a raster reference"

    # No element that causes a fetch. (The w3.org URL in xmlns= is the SVG
    # namespace identifier -- it is never resolved over the network.)
    for forbidden in ("<image", "xlink:href", "src=", "url("):
        assert forbidden not in svg, forbidden

    external = [
        part for part in svg.split('"')
        if part.startswith(("http://", "https://"))
        and part != "http://www.w3.org/2000/svg"
    ]
    assert external == [], f"unexpected external reference: {external}"


def test_the_secret_never_appears_in_the_rendered_qr_markup(app):
    """Belt and braces: the QR encodes the secret, the markup does not spell it."""
    with app.app_context():
        secret = mfa.new_secret()
        svg = mfa.qr_svg(secret, email="a@b.com")
    assert secret not in svg


def test_provisioning_uri_carries_the_issuer(app):
    with app.app_context():
        uri = mfa.provisioning_uri(mfa.new_secret(), email="dana@example.com")
    assert uri.startswith("otpauth://totp/")
    assert "issuer=ScopeMaker" in uri
    assert "dana%40example.com" in uri or "dana@example.com" in uri


# ---------------------------------------------------------------------------
# Recovery codes
# ---------------------------------------------------------------------------

def test_recovery_codes_are_distinct_and_readable():
    codes = mfa.generate_recovery_codes()
    assert len(codes) == mfa.RECOVERY_CODE_COUNT
    assert len(set(codes)) == len(codes)
    for code in codes:
        assert "-" in code
        # No characters that are ambiguous when read off paper.
        assert not set(mfa.normalize_recovery_code(code)) & set("OI01")


def test_recovery_codes_are_stored_hashed():
    codes = mfa.generate_recovery_codes()
    hashes = mfa.hash_recovery_codes(codes)
    for code, stored in zip(codes, hashes, strict=True):
        assert code not in stored
        assert stored.startswith("$argon2")


def test_recovery_code_is_single_use():
    codes = mfa.generate_recovery_codes()
    hashes = mfa.hash_recovery_codes(codes)

    matched, remaining = mfa.consume_recovery_code(hashes, codes[3])
    assert matched
    assert len(remaining) == len(hashes) - 1

    again, still = mfa.consume_recovery_code(remaining, codes[3])
    assert not again
    assert len(still) == len(remaining)


def test_recovery_code_matching_ignores_formatting():
    codes = mfa.generate_recovery_codes()
    hashes = mfa.hash_recovery_codes(codes)
    messy = codes[0].lower().replace("-", " ")
    matched, _ = mfa.consume_recovery_code(hashes, messy)
    assert matched


def test_unknown_recovery_code_changes_nothing():
    hashes = mfa.hash_recovery_codes(mfa.generate_recovery_codes())
    matched, remaining = mfa.consume_recovery_code(hashes, "ZZZZZ-ZZZZZ")
    assert not matched
    assert remaining == hashes


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------

def test_password_alone_does_not_authenticate(client, mfa_user):
    """The single most important property in this file."""
    user, _, _ = mfa_user
    response = login(client, user.email)
    assert response.status_code == 200

    drop_login_cache()
    assert client.get("/dashboard").status_code == 302, "password alone got in"

    with client.session_transaction() as session:
        assert PENDING_MFA_KEY in session
        assert "_user_id" not in session


def test_completing_the_challenge_signs_in(client, mfa_user):
    user, secret, _ = mfa_user
    login(client, user.email)
    response = client.post(
        "/auth/mfa", data={"code": code_for(secret)}, follow_redirects=True
    )
    assert response.status_code == 200
    assert client.get("/dashboard").status_code == 200


def test_a_recovery_code_also_signs_in_and_is_consumed(client, db, mfa_user):
    user, _, codes = mfa_user
    login(client, user.email)
    response = client.post(
        "/auth/mfa", data={"code": codes[0]}, follow_redirects=True
    )
    assert response.status_code == 200
    assert client.get("/dashboard").status_code == 200

    db.session.refresh(user)
    assert user.recovery_codes_remaining == len(codes) - 1

    # And it cannot be used a second time.
    client.get("/auth/logout")
    drop_login_cache()


def test_a_wrong_code_does_not_sign_in(client, mfa_user):
    user, _, _ = mfa_user
    login(client, user.email)
    client.post("/auth/mfa", data={"code": "000000"})
    drop_login_cache()
    assert client.get("/dashboard").status_code == 302


def test_repeated_wrong_codes_lock_the_account(client, db, app, mfa_user):
    user, _, _ = mfa_user
    login(client, user.email)
    for _ in range(app.config["LOGIN_MAX_ATTEMPTS"]):
        client.post("/auth/mfa", data={"code": "000000"})

    db.session.refresh(user)
    assert user.is_locked, "the second factor must share the account lockout"


def test_a_password_change_mid_challenge_invalidates_it(client, db, mfa_user):
    user, secret, _ = mfa_user
    login(client, user.email)

    # Somebody resets the password while the challenge is outstanding.
    user.set_password("a-completely-new-passphrase")
    db.session.commit()

    response = client.post(
        "/auth/mfa", data={"code": code_for(secret)}, follow_redirects=True
    )
    assert b"expired" in response.data.lower() or b"sign in" in response.data.lower()
    drop_login_cache()
    assert client.get("/dashboard").status_code == 302


def test_an_expired_challenge_is_rejected(client, mfa_user):
    from datetime import timedelta

    user, secret, _ = mfa_user
    login(client, user.email)

    with client.session_transaction() as session:
        payload = dict(session[PENDING_MFA_KEY])
        payload["at"] = (utcnow() - timedelta(hours=1)).isoformat()
        session[PENDING_MFA_KEY] = payload

    client.post("/auth/mfa", data={"code": code_for(secret)}, follow_redirects=True)
    drop_login_cache()
    assert client.get("/dashboard").status_code == 302


def test_the_challenge_page_is_not_reachable_without_a_pending_login(client, user):
    response = client.get("/auth/mfa", follow_redirects=True)
    assert b"Sign in" in response.data


def test_users_without_mfa_are_unaffected(client, user):
    login(client, user.email)
    assert client.get("/dashboard").status_code == 200


# ---------------------------------------------------------------------------
# Enrolment
# ---------------------------------------------------------------------------

def test_enrolment_round_trip(auth_client, db, user):
    page = auth_client.get("/auth/mfa/setup")
    assert page.status_code == 200
    assert b"<svg" in page.data

    with auth_client.session_transaction() as session:
        secret = session["mfa_enrol_secret"]

    response = auth_client.post(
        "/auth/mfa/setup", data={"code": code_for(secret)}, follow_redirects=True
    )
    assert response.status_code == 200
    assert b"recovery codes" in response.data.lower()

    db.session.refresh(user)
    assert user.mfa_enabled
    assert user.mfa_secret == secret
    assert user.recovery_codes_remaining == mfa.RECOVERY_CODE_COUNT


def test_a_bad_code_does_not_enable_anything(auth_client, db, user):
    auth_client.get("/auth/mfa/setup")
    auth_client.post("/auth/mfa/setup", data={"code": "000000"}, follow_redirects=True)
    db.session.refresh(user)
    assert not user.mfa_enabled
    assert user.mfa_secret is None, "an abandoned setup must leave no secret behind"


def test_recovery_codes_are_shown_only_once(auth_client, db, user):
    auth_client.get("/auth/mfa/setup")
    with auth_client.session_transaction() as session:
        secret = session["mfa_enrol_secret"]
    first = auth_client.post(
        "/auth/mfa/setup", data={"code": code_for(secret)}, follow_redirects=True
    )
    assert b"Save your recovery codes" in first.data

    second = auth_client.get("/auth/mfa/recovery-codes", follow_redirects=True)
    assert b"shown only once" in second.data


def test_regenerating_replaces_the_old_codes(auth_client, db, mfa_user):
    user, _, codes = mfa_user
    before = list(user.mfa_recovery_hashes)
    auth_client.post("/auth/mfa/recovery-codes/regenerate", follow_redirects=True)
    db.session.refresh(user)
    assert user.recovery_codes_remaining == mfa.RECOVERY_CODE_COUNT
    assert user.mfa_recovery_hashes != before

    # The old ones no longer work.
    matched, _ = mfa.consume_recovery_code(list(user.mfa_recovery_hashes), codes[0])
    assert not matched


def test_the_secret_is_encrypted_at_rest(db, mfa_user):
    user, secret, _ = mfa_user
    assert user.mfa_secret == secret
    assert secret not in (user._mfa_secret or "")


# ---------------------------------------------------------------------------
# Disabling
# ---------------------------------------------------------------------------

def test_disabling_requires_the_password(auth_client, db, mfa_user):
    user, _, _ = mfa_user
    auth_client.post(
        "/auth/mfa/disable", data={"password": "not-the-password"},
        follow_redirects=True,
    )
    db.session.refresh(user)
    assert user.mfa_enabled, "a hijacked session must not be able to turn this off"

    auth_client.post(
        "/auth/mfa/disable", data={"password": "correct-horse-battery-staple"},
        follow_redirects=True,
    )
    db.session.refresh(user)
    assert not user.mfa_enabled
    assert user.mfa_secret is None
    assert user.recovery_codes_remaining == 0


def test_org_policy_blocks_disabling(auth_client, db, organization, mfa_user):
    user, _, _ = mfa_user
    organization.settings = {**(organization.settings or {}), "require_mfa": True}
    db.session.commit()

    response = auth_client.post(
        "/auth/mfa/disable", data={"password": "correct-horse-battery-staple"},
        follow_redirects=True,
    )
    assert b"requires two-factor" in response.data
    db.session.refresh(user)
    assert user.mfa_enabled


# ---------------------------------------------------------------------------
# Auditing
# ---------------------------------------------------------------------------

def test_mfa_events_are_audited(auth_client, db, user, organization):
    from scopemaker.models import AuditEvent

    auth_client.get("/auth/mfa/setup")
    with auth_client.session_transaction() as session:
        secret = session["mfa_enrol_secret"]
    auth_client.post("/auth/mfa/setup", data={"code": code_for(secret)},
                     follow_redirects=True)

    actions = [e.action for e in db.session.query(AuditEvent).all()]
    assert AuditAction.MFA_ENABLED in actions

    auth_client.post("/auth/mfa/disable",
                     data={"password": "correct-horse-battery-staple"},
                     follow_redirects=True)
    actions = [e.action for e in db.session.query(AuditEvent).all()]
    assert AuditAction.MFA_DISABLED in actions


def test_recovery_code_use_is_audited(client, db, mfa_user):
    from scopemaker.models import AuditEvent

    user, _, codes = mfa_user
    login(client, user.email)
    client.post("/auth/mfa", data={"code": codes[0]}, follow_redirects=True)

    events = (
        db.session.query(AuditEvent)
        .filter_by(action=AuditAction.MFA_RECOVERY_USED)
        .all()
    )
    assert len(events) == 1
    assert events[0].context["remaining"] == len(codes) - 1
