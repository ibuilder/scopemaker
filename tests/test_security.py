"""Authentication, authorization, tenant isolation and sanitization."""

from __future__ import annotations

import pytest

from scopemaker.security import (
    decrypt_secret,
    encrypt_secret,
    hash_password,
    password_problems,
    verify_password,
)
from scopemaker.services.accounts import is_safe_redirect
from scopemaker.services.sanitize import sanitize_html, sanitize_inline, strip_html

from .conftest import login

# ---------------------------------------------------------------------------
# Passwords and encryption
# ---------------------------------------------------------------------------

def test_password_hashing_round_trip():
    stored = hash_password("correct-horse-battery-staple")
    assert stored != "correct-horse-battery-staple"
    assert stored.startswith("$argon2")
    assert verify_password(stored, "correct-horse-battery-staple")
    assert not verify_password(stored, "wrong")


def test_verify_never_raises_on_garbage():
    assert verify_password("", "x") is False
    assert verify_password("not-a-hash", "x") is False


@pytest.mark.parametrize(
    "password",
    ["short", "password123", "aaaaaaaaaaaaaaa", ""],
)
def test_weak_passwords_are_rejected(password):
    assert password_problems(password)


def test_reasonable_passphrase_is_accepted():
    assert password_problems("correct-horse-battery-staple") == []


def test_token_encryption_round_trip(app):
    with app.app_context():
        ciphertext = encrypt_secret("procore-access-token")
        assert ciphertext != "procore-access-token"
        assert decrypt_secret(ciphertext) == "procore-access-token"


def test_undecryptable_ciphertext_returns_none(app):
    with app.app_context():
        assert decrypt_secret("not-a-fernet-token") is None
        assert decrypt_secret(None) is None


def test_procore_tokens_are_not_stored_in_plaintext(db, organization):
    from scopemaker.models import ProcoreConnection

    connection = ProcoreConnection(organization_id=organization.id)
    connection.access_token = "super-secret-access"
    connection.refresh_token = "super-secret-refresh"
    db.session.add(connection)
    db.session.commit()

    assert connection.access_token == "super-secret-access"
    # The column itself must hold ciphertext.
    assert "super-secret-access" not in (connection._access_token or "")
    assert "super-secret-refresh" not in (connection._refresh_token or "")


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "payload",
    [
        '<script>alert(1)</script>',
        '<img src=x onerror="alert(1)">',
        '<a href="javascript:alert(1)">click</a>',
        '<iframe src="https://evil.example"></iframe>',
        '<div onclick="steal()">text</div>',
        '<style>body{display:none}</style>',
        '<object data="x"></object>',
    ],
)
def test_dangerous_markup_is_stripped(payload):
    cleaned = str(sanitize_html(payload))
    assert "<script" not in cleaned.lower()
    assert "onerror" not in cleaned.lower()
    assert "onclick" not in cleaned.lower()
    assert "javascript:" not in cleaned.lower()
    assert "<iframe" not in cleaned.lower()
    assert "<object" not in cleaned.lower()


def test_contract_formatting_survives_sanitizing():
    cleaned = str(sanitize_html("<p><strong>Bold</strong> and <u>underlined</u>.</p>"))
    assert "<strong>Bold</strong>" in cleaned
    assert "<u>underlined</u>" in cleaned


def test_inline_sanitizer_drops_block_structure():
    cleaned = str(sanitize_inline("<p>One</p><ul><li>Two</li></ul>"))
    assert "<p>" not in cleaned
    assert "<li>" not in cleaned
    assert "One" in cleaned and "Two" in cleaned


def test_strip_html_decodes_entities():
    assert strip_html("210500 &ndash; Common Work &amp; Results") == (
        "210500 – Common Work & Results"
    )


def test_scope_item_text_is_sanitized_on_save(auth_client, scope):
    section = scope.section("inclusions")
    response = auth_client.post(
        f"/scopes/{scope.id}/sections/{section.key}/items",
        data={"text_html": '<script>alert(1)</script>Legitimate clause text',
              "submit": "Save"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    texts = [item.text_html for item in section.items]
    assert any("Legitimate clause text" in t for t in texts)
    assert not any("<script" in t.lower() for t in texts)


# ---------------------------------------------------------------------------
# Redirect safety
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "target",
    ["//evil.example", "https://evil.example/x", "http://evil.example",
     "https://evil.example\\@localhost"],
)
def test_offsite_redirect_targets_are_rejected(app, target):
    with app.test_request_context("/auth/login"):
        assert is_safe_redirect(target) is False


@pytest.mark.parametrize("target", ["/dashboard", "/scopes/", "/projects/1"])
def test_local_redirect_targets_are_allowed(app, target):
    with app.test_request_context("/auth/login"):
        assert is_safe_redirect(target) is True


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def test_login_rejects_a_bad_password(client, user):
    response = login(client, user.email, "wrong-password")
    assert b"Incorrect email address or password" in response.data


def test_login_message_does_not_reveal_whether_an_account_exists(client, user):
    known = login(client, user.email, "wrong-password").data
    client.get("/auth/logout")
    unknown = login(client, "nobody@example.com", "wrong-password").data
    assert b"Incorrect email address or password" in known
    assert b"Incorrect email address or password" in unknown


def test_protected_pages_redirect_when_signed_out(client):
    for path in ("/dashboard", "/scopes/", "/projects/", "/library/", "/admin/"):
        response = client.get(path)
        assert response.status_code in (302, 401), path


def test_security_headers_are_applied(client):
    response = client.get("/auth/login")
    headers = response.headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    csp = headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    # No CDN origins: every asset is served locally.
    assert "http://" not in csp and "https://" not in csp


# ---------------------------------------------------------------------------
# Authorization and tenant isolation
# ---------------------------------------------------------------------------

def test_viewer_cannot_reach_editor_routes(client, viewer, scope):
    login(client, viewer.email)
    assert client.get("/scopes/new").status_code == 403
    assert client.get(f"/scopes/{scope.id}/settings").status_code == 403
    assert client.post(
        f"/scopes/{scope.id}/duplicate", follow_redirects=False
    ).status_code == 403


def test_viewer_can_still_read_and_export(client, viewer, scope):
    login(client, viewer.email)
    assert client.get("/scopes/").status_code == 200
    assert client.get(f"/scopes/{scope.id}").status_code == 200
    assert client.get(f"/exports/{scope.id}.md").status_code == 200


def test_viewer_cannot_reach_admin(client, viewer):
    login(client, viewer.email)
    assert client.get("/admin/").status_code == 403
    assert client.get("/admin/tokens").status_code == 403


def test_another_tenants_scope_returns_404_not_403(db, client, other_org, scope):
    """404 rather than 403: a 403 would confirm the id exists."""
    from scopemaker.models import User

    rival = db.session.query(User).filter_by(email="rival@rival.example").one()
    login(client, rival.email)

    assert client.get(f"/scopes/{scope.id}").status_code == 404
    assert client.get(f"/exports/{scope.id}.md").status_code == 404
    assert client.get(f"/scopes/{scope.id}/settings").status_code == 404


def test_another_tenants_project_is_invisible(db, client, other_org, project):
    from scopemaker.models import User

    rival = db.session.query(User).filter_by(email="rival@rival.example").one()
    login(client, rival.email)
    assert client.get(f"/projects/{project.id}").status_code == 404
    listing = client.get("/projects/")
    assert b"Riverside Medical Center" not in listing.data


def test_issued_scope_rejects_edits(auth_client, db, scope, user):
    from scopemaker.services.scope_builder import issue_scope

    issue_scope(scope, user_id=user.id)
    section = scope.section("inclusions")
    response = auth_client.post(
        f"/scopes/{scope.id}/sections/{section.key}/items",
        data={"text_html": "Sneaky late addition", "submit": "Save"},
    )
    assert response.status_code == 409
    assert not any("Sneaky late addition" in i.text_html for i in section.items)


def test_csrf_is_enforced_when_enabled(app, client, user):
    """The test config disables CSRF; confirm it is on by default elsewhere."""
    assert app.config["WTF_CSRF_ENABLED"] is False
    from scopemaker.config import DevelopmentConfig, ProductionConfig

    assert getattr(DevelopmentConfig, "WTF_CSRF_ENABLED", True) is True
    assert getattr(ProductionConfig, "WTF_CSRF_ENABLED", True) is True


def test_production_config_refuses_to_boot_without_secrets(monkeypatch):
    from scopemaker.config import ConfigError, ProductionConfig

    monkeypatch.setattr(ProductionConfig, "SECRET_KEY", "")
    monkeypatch.setattr(ProductionConfig, "ENCRYPTION_KEY", "")
    with pytest.raises(ConfigError) as excinfo:
        ProductionConfig.validate()
    assert "SECRET_KEY" in str(excinfo.value)


def test_production_config_rejects_sqlite(monkeypatch):
    from scopemaker.config import ConfigError, ProductionConfig

    monkeypatch.setattr(ProductionConfig, "SECRET_KEY", "x" * 40)
    monkeypatch.setattr(ProductionConfig, "ENCRYPTION_KEY", "y" * 44)
    monkeypatch.setattr(ProductionConfig, "SQLALCHEMY_DATABASE_URI", "sqlite:///x.db")
    with pytest.raises(ConfigError) as excinfo:
        ProductionConfig.validate()
    assert "SQLite" in str(excinfo.value)
