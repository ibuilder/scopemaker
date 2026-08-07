"""JSON API v1."""

from __future__ import annotations

import pytest

from scopemaker.models import ApiToken


@pytest.fixture()
def token(db, user, organization):
    record, raw = ApiToken.issue(
        user=user, organization_id=organization.id, name="tests", scopes="read write"
    )
    db.session.add(record)
    db.session.commit()
    return raw


@pytest.fixture()
def readonly_token(db, user, organization):
    record, raw = ApiToken.issue(
        user=user, organization_id=organization.id, name="ro", scopes="read"
    )
    db.session.add(record)
    db.session.commit()
    return raw


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_root_is_public(client):
    response = client.get("/api/v1/")
    assert response.status_code == 200
    assert response.json["version"] == "v1"


def test_divisions_endpoint_excludes_reserved(client):
    payload = client.get("/api/v1/divisions").json
    codes = {d["code"] for d in payload["divisions"]}
    assert "21" in codes
    assert "20" not in codes and "15" not in codes


def test_authentication_is_required(client):
    assert client.get("/api/v1/scopes").status_code == 401
    assert client.get("/api/v1/me").status_code == 401


def test_invalid_token_is_rejected(client):
    response = client.get("/api/v1/scopes", headers=auth("smk_totally-made-up"))
    assert response.status_code == 401
    assert response.json["error"]["code"] == "unauthorized"


def test_revoked_token_stops_working(client, db, token):
    assert client.get("/api/v1/me", headers=auth(token)).status_code == 200
    record = db.session.query(ApiToken).filter_by(name="tests").one()
    from scopemaker.models.base import utcnow

    record.revoked_at = utcnow()
    db.session.commit()
    assert client.get("/api/v1/me", headers=auth(token)).status_code == 401


def test_token_plaintext_is_not_stored(db, token):
    record = db.session.query(ApiToken).filter_by(name="tests").one()
    assert record.token_hash != token
    assert token not in record.token_hash
    assert record.matches(token)


def test_me_reports_the_organization(client, token, organization):
    payload = client.get("/api/v1/me", headers=auth(token)).json
    assert payload["organization_id"] == organization.id
    assert set(payload["scopes"]) == {"read", "write"}


def test_create_scope_with_library_defaults(client, token, organization):
    response = client.post(
        "/api/v1/scopes",
        headers=auth(token),
        json={"division_code": "21", "use_defaults": True, "title": "Scope of Work"},
    )
    assert response.status_code == 201
    scope = response.json["scope"]
    assert scope["division_code"] == "21"
    assert scope["trade_name"] == "Fire Protection"
    assert scope["item_count"] > 20


def test_create_scope_rejects_a_reserved_division(client, token):
    response = client.post(
        "/api/v1/scopes", headers=auth(token), json={"division_code": "20"}
    )
    assert response.status_code == 422
    fields = response.json["error"]["details"]["fields"]
    assert any(f["field"] == "division_code" for f in fields)


def test_create_scope_rejects_unknown_fields(client, token):
    response = client.post(
        "/api/v1/scopes",
        headers=auth(token),
        json={"division_code": "21", "surprise": "value"},
    )
    assert response.status_code == 422


def test_create_scope_rejects_unknown_section_keys(client, token):
    response = client.post(
        "/api/v1/scopes",
        headers=auth(token),
        json={"division_code": "21", "enabled_sections": ["intent", "nonsense"]},
    )
    assert response.status_code == 422


def test_missing_body_is_a_422(client, token):
    response = client.post("/api/v1/scopes", headers=auth(token))
    assert response.status_code == 422


def test_read_only_token_cannot_write(client, readonly_token):
    response = client.post(
        "/api/v1/scopes", headers=auth(readonly_token), json={"division_code": "21"}
    )
    assert response.status_code == 403
    assert response.json["error"]["code"] == "insufficient_scope"


def test_read_only_token_can_read(client, readonly_token):
    assert client.get("/api/v1/scopes", headers=auth(readonly_token)).status_code == 200


def test_get_scope_returns_the_numbered_document(client, token, scope):
    payload = client.get(f"/api/v1/scopes/{scope.id}", headers=auth(token)).json
    assert payload["scope"]["id"] == scope.id
    summary = next(s for s in payload["sections"] if s["key"] == "summary")
    assert summary["items"][0]["number"] == "1."


def test_scope_from_another_tenant_is_404(client, db, other_org, scope):
    from scopemaker.models import User

    rival = db.session.query(User).filter_by(email="rival@rival.example").one()
    record, raw = ApiToken.issue(
        user=rival, organization_id=other_org.id, name="rival", scopes="read write"
    )
    db.session.add(record)
    db.session.commit()

    response = client.get(f"/api/v1/scopes/{scope.id}", headers=auth(raw))
    assert response.status_code == 404


def test_list_scopes_filters_by_status(client, token, scope):
    assert client.get("/api/v1/scopes?status=draft", headers=auth(token)).json["count"] == 1
    assert client.get("/api/v1/scopes?status=issued", headers=auth(token)).json["count"] == 0


def test_list_scopes_rejects_an_unknown_status(client, token):
    response = client.get("/api/v1/scopes?status=nonsense", headers=auth(token))
    assert response.status_code == 422


def test_patch_updates_a_scope(client, token, scope):
    response = client.patch(
        f"/api/v1/scopes/{scope.id}",
        headers=auth(token),
        json={"title": "Revised Scope of Work"},
    )
    assert response.status_code == 200
    assert response.json["scope"]["title"] == "Revised Scope of Work"


def test_patch_is_blocked_once_issued(client, token, scope, user):
    from scopemaker.services.scope_builder import issue_scope

    issue_scope(scope, user_id=user.id)
    response = client.patch(
        f"/api/v1/scopes/{scope.id}", headers=auth(token), json={"title": "Nope"}
    )
    assert response.status_code == 422
    assert response.json["error"]["code"] == "scope_locked"


def test_issue_then_revise_via_api(client, token, scope):
    issued = client.post(f"/api/v1/scopes/{scope.id}/issue", headers=auth(token))
    assert issued.status_code == 200
    assert issued.json["scope"]["status"] == "issued"

    revised = client.post(f"/api/v1/scopes/{scope.id}/revise", headers=auth(token))
    assert revised.json["scope"]["version"] == 2
    assert revised.json["scope"]["status"] == "draft"


def test_export_endpoints(client, token, scope):
    for fmt, expected in (
        ("json", b"scopemaker.scope"),
        ("md", b"# EXHIBIT B"),
        ("html", b"<!DOCTYPE html>"),
    ):
        response = client.get(
            f"/api/v1/scopes/{scope.id}/export/{fmt}", headers=auth(token)
        )
        assert response.status_code == 200, fmt
        assert expected in response.data, fmt


def test_unknown_export_format_is_404(client, token, scope):
    response = client.get(
        f"/api/v1/scopes/{scope.id}/export/xlsx", headers=auth(token)
    )
    assert response.status_code == 404


def test_library_endpoints(client, token, organization):
    clauses = client.get("/api/v1/library/clauses?division=21", headers=auth(token)).json
    assert clauses["count"] > 20
    assert any(c["division_code"] == "21" for c in clauses["clauses"])
    assert any(c["division_code"] is None for c in clauses["clauses"])

    sections = client.get(
        "/api/v1/library/spec-sections?division=21", headers=auth(token)
    ).json
    codes = {s["code"] for s in sections["spec_sections"]}
    assert "078413" in codes


def test_project_and_bid_package_creation(client, token):
    created = client.post(
        "/api/v1/projects",
        headers=auth(token),
        json={"name": "New Tower", "number": "2026-001"},
    )
    assert created.status_code == 201
    project_id = created.json["project"]["id"]

    package = client.post(
        f"/api/v1/projects/{project_id}/bid-packages",
        headers=auth(token),
        json={"number": "BP-26A", "name": "Electrical", "division_code": "26"},
    )
    assert package.status_code == 201

    listing = client.get(
        f"/api/v1/projects/{project_id}/bid-packages", headers=auth(token)
    ).json
    assert listing["count"] == 1
    assert listing["bid_packages"][0]["division_code"] == "26"


def test_session_auth_also_works(auth_client, scope):
    """The browser UI calls the same endpoints with a cookie."""
    response = auth_client.get("/api/v1/scopes")
    assert response.status_code == 200
    assert response.json["count"] >= 1


def test_api_is_csrf_exempt(client, token):
    """Bearer-authenticated clients cannot supply a CSRF token."""
    response = client.post(
        "/api/v1/scopes", headers=auth(token), json={"division_code": "09"}
    )
    assert response.status_code == 201


def test_error_envelope_shape(client):
    payload = client.get("/api/v1/scopes").json
    assert set(payload) == {"error"}
    assert "code" in payload["error"] and "message" in payload["error"]
