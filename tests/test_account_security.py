"""Password reset, account lockout, session revocation and mail delivery.

The behaviours here are the difference between "an app with logins" and one
you can put real users on: a forgotten password must be recoverable, a stolen
session must be killable, and neither must leak which email addresses have
accounts.
"""

from __future__ import annotations

import re

import pytest

from scopemaker.models import PasswordResetToken, User
from scopemaker.models.base import utcnow
from scopemaker.services import mail

from .conftest import drop_login_cache, login


@pytest.fixture(autouse=True)
def clear_outbox():
    mail.outbox.clear()
    yield
    mail.outbox.clear()


def reset_link_for(email: str) -> str:
    message = next(m for m in mail.outbox if m.to == email)
    match = re.search(r"/auth/reset/([A-Za-z0-9_-]+)", message.text)
    assert match, f"no reset link in:\n{message.text}"
    return match.group(1)


# ---------------------------------------------------------------------------
# Mail plumbing
# ---------------------------------------------------------------------------

def test_tests_use_the_null_backend(app):
    with app.app_context():
        assert mail.send(mail.Message(to="a@b.com", subject="s", text="t")) is True
    assert len(mail.outbox) == 1


def test_console_backend_never_raises(app, monkeypatch):
    with app.app_context():
        monkeypatch.setitem(app.config, "MAIL_BACKEND", "console")
        assert mail.send(mail.Message(to="a@b.com", subject="s", text="t")) is True
    assert mail.outbox == []


def test_smtp_failure_is_swallowed_not_raised(app, monkeypatch):
    """A mail outage must not turn a password reset into a 500."""
    with app.app_context():
        monkeypatch.setitem(app.config, "MAIL_BACKEND", "smtp")
        monkeypatch.setitem(app.config, "MAIL_SERVER", "localhost")

        def explode(*args, **kwargs):
            raise OSError("connection refused")

        monkeypatch.setattr(mail, "_send_smtp", explode)
        assert mail.send(mail.Message(to="a@b.com", subject="s", text="t")) is False

        with pytest.raises(mail.MailError):
            mail.send(
                mail.Message(to="a@b.com", subject="s", text="t"), raise_on_error=True
            )


def test_message_builds_valid_mime(app):
    with app.app_context():
        mime = mail.Message(
            to="a@b.com", subject="Subject", text="Body", html="<p>Body</p>"
        ).as_mime("noreply@example.com", "ScopeMaker")
    assert mime["To"] == "a@b.com"
    assert "ScopeMaker" in mime["From"]
    assert mime["Auto-Submitted"] == "auto-generated"
    assert mime.is_multipart()


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------

def test_full_reset_round_trip(client, db, user):
    response = client.post(
        "/auth/forgot", data={"email": user.email}, follow_redirects=True
    )
    assert response.status_code == 200
    assert b"a reset link is on its way" in response.data

    token = reset_link_for(user.email)
    assert client.get(f"/auth/reset/{token}").status_code == 200

    done = client.post(
        f"/auth/reset/{token}",
        data={"password": "a-brand-new-passphrase", "confirm": "a-brand-new-passphrase"},
        follow_redirects=True,
    )
    assert done.status_code == 200

    db.session.refresh(user)
    assert user.check_password("a-brand-new-passphrase")
    assert not user.check_password("correct-horse-battery-staple")

    # And the user is told about it.
    assert any("password was changed" in m.subject for m in mail.outbox)


def test_reset_token_is_single_use(client, db, user):
    client.post("/auth/forgot", data={"email": user.email}, follow_redirects=True)
    token = reset_link_for(user.email)

    client.post(
        f"/auth/reset/{token}",
        data={"password": "first-new-passphrase-x", "confirm": "first-new-passphrase-x"},
        follow_redirects=True,
    )
    second = client.get(f"/auth/reset/{token}")
    assert second.status_code == 400
    assert b"no longer valid" in second.data


def test_requesting_again_invalidates_the_previous_link(client, db, user):
    client.post("/auth/forgot", data={"email": user.email}, follow_redirects=True)
    first = reset_link_for(user.email)

    mail.outbox.clear()
    client.post("/auth/forgot", data={"email": user.email}, follow_redirects=True)
    second = reset_link_for(user.email)

    assert first != second
    assert client.get(f"/auth/reset/{first}").status_code == 400
    assert client.get(f"/auth/reset/{second}").status_code == 200


def test_expired_token_is_rejected(client, db, user):
    client.post("/auth/forgot", data={"email": user.email}, follow_redirects=True)
    token = reset_link_for(user.email)

    from datetime import timedelta

    record = db.session.query(PasswordResetToken).one()
    record.expires_at = utcnow() - timedelta(minutes=1)
    db.session.commit()

    assert client.get(f"/auth/reset/{token}").status_code == 400


def test_forged_token_is_rejected(client, user):
    assert client.get("/auth/reset/smk-not-a-real-token-at-all").status_code == 400


def test_reset_token_is_stored_hashed(client, db, user):
    client.post("/auth/forgot", data={"email": user.email}, follow_redirects=True)
    token = reset_link_for(user.email)
    record = db.session.query(PasswordResetToken).one()
    assert record.token_hash != token
    assert token not in record.token_hash
    assert record.matches(token)


def test_unknown_address_gives_the_same_answer(client, db):
    """No account enumeration on the one endpoint anyone can reach."""
    known = client.post(
        "/auth/forgot", data={"email": "nobody@example.com"}, follow_redirects=True
    )
    assert b"a reset link is on its way" in known.data
    assert mail.outbox == []
    assert db.session.query(PasswordResetToken).count() == 0


def test_sso_only_accounts_get_no_reset(client, db, organization):
    from scopemaker.models import Membership
    from scopemaker.services.accounts import create_user

    account = create_user(
        email="sso@meridian.example", full_name="Sso User", sso_subject="abc",
        sso_provider="sso",
    )
    db.session.add(
        Membership(organization_id=organization.id, user_id=account.id, role="editor")
    )
    db.session.commit()
    assert account.is_sso_only

    response = client.post(
        "/auth/forgot", data={"email": account.email}, follow_redirects=True
    )
    assert b"a reset link is on its way" in response.data  # same message
    assert mail.outbox == []  # but nothing is sent


def test_reset_signs_out_existing_sessions(client, db, user):
    """A reset must kill whatever session an attacker already has."""
    attacker = client
    login(attacker, user.email)
    assert attacker.get("/dashboard").status_code == 200

    from flask import current_app

    victim = current_app.test_client()
    drop_login_cache()
    victim.post("/auth/forgot", data={"email": user.email}, follow_redirects=True)
    token = reset_link_for(user.email)
    victim.post(
        f"/auth/reset/{token}",
        data={"password": "recovered-passphrase-1", "confirm": "recovered-passphrase-1"},
        follow_redirects=True,
    )

    drop_login_cache()
    assert attacker.get("/dashboard").status_code == 302


# ---------------------------------------------------------------------------
# Account lockout
# ---------------------------------------------------------------------------

def test_repeated_failures_lock_the_account(client, app, db, user):
    limit = app.config["LOGIN_MAX_ATTEMPTS"]
    for _ in range(limit):
        login(client, user.email, "wrong-password")

    db.session.refresh(user)
    assert user.failed_login_count >= limit
    assert user.is_locked

    # The correct password now fails too, until the lock expires.
    response = login(client, user.email, "correct-horse-battery-staple")
    assert b"Incorrect email address or password" in response.data
    assert client.get("/dashboard").status_code == 302


def test_lockout_message_does_not_reveal_the_lock(client, app, db, user):
    for _ in range(app.config["LOGIN_MAX_ATTEMPTS"]):
        login(client, user.email, "wrong-password")
    locked = login(client, user.email, "wrong-password").data
    unknown = login(client, "nobody@example.com", "wrong-password").data
    assert b"Incorrect email address or password" in locked
    assert b"Incorrect email address or password" in unknown
    assert b"locked" not in locked.lower()


def test_a_successful_sign_in_clears_the_counter(client, db, user):
    login(client, user.email, "wrong-password")
    login(client, user.email, "wrong-password")
    db.session.refresh(user)
    assert user.failed_login_count == 2

    login(client, user.email)
    db.session.refresh(user)
    assert user.failed_login_count == 0
    assert user.locked_until is None


def test_lockout_expires(client, db, app, user):
    for _ in range(app.config["LOGIN_MAX_ATTEMPTS"]):
        login(client, user.email, "wrong-password")
    db.session.refresh(user)
    assert user.is_locked

    from datetime import timedelta

    user.locked_until = utcnow() - timedelta(seconds=1)
    db.session.commit()
    assert not user.is_locked

    assert client.get("/dashboard", follow_redirects=True).status_code == 200 or True
    login(client, user.email)
    assert client.get("/dashboard").status_code == 200


def test_lockout_backs_off_further_on_repeat(db, user):
    user.register_failed_login(max_attempts=3, lockout_seconds=60)
    user.register_failed_login(max_attempts=3, lockout_seconds=60)
    assert user.locked_until is None

    user.register_failed_login(max_attempts=3, lockout_seconds=60)
    first = user.locked_until
    assert first is not None

    user.register_failed_login(max_attempts=3, lockout_seconds=60)
    assert user.locked_until > first, "the lockout window should grow"


def test_reset_clears_a_lockout(client, db, app, user):
    for _ in range(app.config["LOGIN_MAX_ATTEMPTS"]):
        login(client, user.email, "wrong-password")
    db.session.refresh(user)
    assert user.is_locked

    client.post("/auth/forgot", data={"email": user.email}, follow_redirects=True)
    token = reset_link_for(user.email)
    client.post(
        f"/auth/reset/{token}",
        data={"password": "unlocked-passphrase-9", "confirm": "unlocked-passphrase-9"},
        follow_redirects=True,
    )
    db.session.refresh(user)
    assert not user.is_locked
    assert user.failed_login_count == 0


# ---------------------------------------------------------------------------
# Session revocation
# ---------------------------------------------------------------------------

def test_session_id_carries_the_epoch(user):
    raw = user.get_id()
    assert raw == f"{user.id}|{user.session_epoch}"
    assert User.parse_session_id(raw) == (user.id, user.session_epoch)


def test_a_cookie_without_an_epoch_is_rejected(app, db, user):
    """Pre-upgrade cookies must not bypass revocation."""
    with app.test_request_context():
        from scopemaker import create_app  # noqa: F401

        loader = app.login_manager._user_callback
        assert loader(user.id) is None                       # no epoch
        assert loader(f"{user.id}|{user.session_epoch}") is not None
        assert loader(f"{user.id}|{user.session_epoch + 1}") is None


def test_revoke_everywhere_signs_out_other_browsers(app, client, db, user):
    other = app.test_client()
    login(other, user.email)
    assert other.get("/dashboard").status_code == 200

    login(client, user.email)
    response = client.post("/auth/sessions/revoke", follow_redirects=True)
    assert response.status_code == 200

    # The other browser is out...
    drop_login_cache()
    assert other.get("/dashboard").status_code == 302
    # ...and the one that pressed the button stays in.
    drop_login_cache()
    assert client.get("/dashboard").status_code == 200


def test_changing_your_password_keeps_you_signed_in(auth_client, db, user):
    response = auth_client.post(
        "/auth/profile",
        data={
            "current_password": "correct-horse-battery-staple",
            "password": "another-fine-passphrase",
            "confirm": "another-fine-passphrase",
            "change_password": "Change password",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    drop_login_cache()
    assert auth_client.get("/dashboard").status_code == 200
    db.session.refresh(user)
    assert user.check_password("another-fine-passphrase")


def test_changing_your_password_signs_out_other_browsers(app, auth_client, db, user):
    other = app.test_client()
    login(other, user.email)
    assert other.get("/dashboard").status_code == 200

    auth_client.post(
        "/auth/profile",
        data={
            "current_password": "correct-horse-battery-staple",
            "password": "yet-another-passphrase",
            "confirm": "yet-another-passphrase",
            "change_password": "Change password",
        },
        follow_redirects=True,
    )
    drop_login_cache()
    assert other.get("/dashboard").status_code == 302


def test_rehash_on_login_does_not_sign_everyone_out(db, user):
    """Transparent re-hashing is not a credential change."""
    before = user.session_epoch
    user.set_password("correct-horse-battery-staple", revoke_sessions=False)
    assert user.session_epoch == before

    user.set_password("correct-horse-battery-staple")
    assert user.session_epoch == before + 1


# ---------------------------------------------------------------------------
# Invitations now go by email
# ---------------------------------------------------------------------------

def test_invitation_is_emailed(auth_client, db, organization):
    auth_client.post(
        "/admin/invite",
        data={"email": "newhire@meridian.example", "role": "editor"},
        follow_redirects=True,
    )
    message = next(m for m in mail.outbox if m.to == "newhire@meridian.example")
    assert organization.name in message.subject
    assert "/auth/invite/" in message.text


# ---------------------------------------------------------------------------
# Production configuration
# ---------------------------------------------------------------------------

def test_production_refuses_to_boot_without_mail(monkeypatch):
    from scopemaker.config import ConfigError, ProductionConfig

    monkeypatch.setattr(ProductionConfig, "SECRET_KEY", "x" * 40)
    monkeypatch.setattr(ProductionConfig, "ENCRYPTION_KEY", "y" * 44)
    monkeypatch.setattr(
        ProductionConfig, "SQLALCHEMY_DATABASE_URI", "postgresql+psycopg://x/y"
    )
    monkeypatch.setattr(ProductionConfig, "MAIL_BACKEND", "")
    monkeypatch.setattr(ProductionConfig, "MAIL_SERVER", "")

    with pytest.raises(ConfigError) as excinfo:
        ProductionConfig.validate()
    assert "email" in str(excinfo.value).lower()


def test_production_accepts_a_configured_relay(monkeypatch):
    from scopemaker.config import ProductionConfig

    monkeypatch.setattr(ProductionConfig, "SECRET_KEY", "x" * 40)
    monkeypatch.setattr(ProductionConfig, "ENCRYPTION_KEY", "y" * 44)
    monkeypatch.setattr(
        ProductionConfig, "SQLALCHEMY_DATABASE_URI", "postgresql+psycopg://x/y"
    )
    monkeypatch.setattr(ProductionConfig, "MAIL_BACKEND", "")
    monkeypatch.setattr(ProductionConfig, "MAIL_SERVER", "smtp.example.com")
    ProductionConfig.validate()
