"""Procore client and sync, with the HTTP layer mocked."""

from __future__ import annotations

import pytest
import responses

from scopemaker.errors import IntegrationError
from scopemaker.models import ProcoreConnection
from scopemaker.models.base import utcnow
from scopemaker.services.procore_client import ProcoreClient, authorize_url
from scopemaker.services.procore_sync import (
    _infer_division,
    sync_bid_packages,
    sync_projects,
)

API = "https://api.procore.com/rest/v1.0"
LOGIN = "https://login.procore.com"


@pytest.fixture()
def connection(db, organization, app):
    record = ProcoreConnection(
        organization_id=organization.id,
        grant_type="authorization_code",
        company_id="1001",
        is_active=True,
    )
    record.access_token = "live-token"
    record.refresh_token = "refresh-token"
    from datetime import timedelta

    record.token_expires_at = utcnow() + timedelta(hours=1)
    db.session.add(record)
    db.session.commit()
    return record


def test_authorize_url_carries_state(app):
    with app.test_request_context():
        url = authorize_url("https://app.example/procore/callback", "abc123")
    assert "response_type=code" in url
    assert "state=abc123" in url
    assert url.startswith(LOGIN)


def test_client_secret_is_never_sent_to_the_browser(app, connection):
    """The secret only ever travels server-to-server in a token request."""
    with app.test_request_context():
        client = ProcoreClient(connection)
        assert client.client_secret == "test-client-secret"
    # Nothing on the model exposes it.
    assert not hasattr(connection, "client_secret")


@responses.activate
def test_exchange_code(app, connection):
    responses.add(
        responses.POST,
        f"{LOGIN}/oauth/token",
        json={"access_token": "new-token", "refresh_token": "new-refresh",
              "expires_in": 5400},
        status=200,
    )
    with app.test_request_context():
        client = ProcoreClient(connection)
        payload = client.exchange_code("the-code", "https://app.example/cb")
    assert payload["access_token"] == "new-token"

    body = responses.calls[0].request.body
    assert "grant_type=authorization_code" in body
    assert "client_secret=test-client-secret" in body


@responses.activate
def test_token_failure_becomes_an_integration_error(app, connection):
    responses.add(
        responses.POST,
        f"{LOGIN}/oauth/token",
        json={"error_description": "invalid_grant"},
        status=400,
    )
    with app.test_request_context(), pytest.raises(IntegrationError) as excinfo:
        ProcoreClient(connection).exchange_code("bad", "https://app.example/cb")
    assert "invalid_grant" in excinfo.value.message


@responses.activate
def test_expired_token_is_refreshed_before_a_request(app, db, connection):
    from datetime import timedelta

    connection.token_expires_at = utcnow() - timedelta(minutes=5)
    db.session.commit()

    responses.add(
        responses.POST, f"{LOGIN}/oauth/token",
        json={"access_token": "refreshed", "expires_in": 5400}, status=200,
    )
    responses.add(responses.GET, f"{API}/me", json={"id": 7, "name": "Dana"}, status=200)

    with app.test_request_context():
        result = ProcoreClient(connection).me()

    assert result["name"] == "Dana"
    assert connection.access_token == "refreshed"
    assert responses.calls[1].request.headers["Authorization"] == "Bearer refreshed"


@responses.activate
def test_a_401_mid_session_triggers_one_retry(app, connection):
    responses.add(responses.GET, f"{API}/me", json={"error": "expired"}, status=401)
    responses.add(
        responses.POST, f"{LOGIN}/oauth/token",
        json={"access_token": "second-wind", "expires_in": 5400}, status=200,
    )
    responses.add(responses.GET, f"{API}/me", json={"id": 7, "name": "Dana"}, status=200)

    with app.test_request_context():
        assert ProcoreClient(connection).me()["name"] == "Dana"
    assert len(responses.calls) == 3


@responses.activate
def test_company_header_is_sent(app, connection):
    responses.add(responses.GET, f"{API}/me", json={"id": 1}, status=200)
    with app.test_request_context():
        ProcoreClient(connection).me()
    assert responses.calls[0].request.headers["Procore-Company-Id"] == "1001"


@responses.activate
def test_pagination_follows_every_page(app, connection):
    first = [{"id": i, "name": f"Project {i}"} for i in range(100)]
    responses.add(responses.GET, f"{API}/projects", json=first, status=200)
    responses.add(
        responses.GET, f"{API}/projects",
        json=[{"id": 100, "name": "Project 100"}], status=200,
    )
    with app.test_request_context():
        projects = ProcoreClient(connection).projects("1001")
    assert len(projects) == 101


@responses.activate
def test_rate_limit_is_reported_clearly(app, connection):
    responses.add(responses.GET, f"{API}/me", json={}, status=429)
    with app.test_request_context(), pytest.raises(IntegrationError) as excinfo:
        ProcoreClient(connection).me()
    assert excinfo.value.code == "procore_rate_limited"


@responses.activate
def test_project_sync_upserts(app, db, connection, organization):
    payload = [
        {"id": 555, "name": "Riverside Medical Center", "project_number": "2024-118",
         "address": "1400 River Road", "city": "Columbus", "state_code": "OH",
         "owner": {"name": "Riverside Health System"}},
        {"id": 556, "name": "Northside Depot", "project_number": "2024-119"},
    ]
    responses.add(responses.GET, f"{API}/projects", json=payload, status=200)

    with app.test_request_context():
        result = sync_projects(ProcoreClient(connection), organization.id, "1001")

    assert result.projects_created == 2
    from scopemaker.models import Project

    project = db.session.query(Project).filter_by(procore_project_id="555").one()
    assert project.owner_name == "Riverside Health System"
    assert project.state == "OH"

    # Re-syncing must update rather than duplicate.
    responses.add(responses.GET, f"{API}/projects", json=payload, status=200)
    with app.test_request_context():
        again = sync_projects(ProcoreClient(connection), organization.id, "1001")
    assert again.projects_created == 0
    assert db.session.query(Project).filter_by(procore_project_id="555").count() == 1


@responses.activate
def test_sync_never_blanks_a_populated_local_field(app, db, connection, organization):
    from scopemaker.models import Project

    existing = Project(
        organization_id=organization.id,
        procore_project_id="555",
        name="Riverside Medical Center",
        architect_name="Whitfield Architects",
    )
    db.session.add(existing)
    db.session.commit()

    responses.add(
        responses.GET, f"{API}/projects",
        json=[{"id": 555, "name": "Riverside Medical Center"}], status=200,
    )
    with app.test_request_context():
        sync_projects(ProcoreClient(connection), organization.id, "1001")

    db.session.refresh(existing)
    assert existing.architect_name == "Whitfield Architects"


@responses.activate
def test_bid_package_sync_and_division_inference(app, db, connection, project):
    project.procore_project_id = "555"
    db.session.commit()

    responses.add(
        responses.GET,
        f"{API}/projects/555/bid_packages",
        json=[{"id": 9001, "number": "BP-21A", "title": "Fire Protection"},
              {"id": 9002, "number": "BP-26B", "title": "Electrical"}],
        status=200,
    )
    with app.test_request_context():
        result = sync_bid_packages(ProcoreClient(connection), project)

    assert result.packages_created == 2
    db.session.refresh(project)
    divisions = {p.number: p.division_code for p in project.bid_packages}
    assert divisions["BP-21A"] == "21"
    assert divisions["BP-26B"] == "26"


@responses.activate
def test_bid_package_sync_falls_back_to_the_flat_endpoint(app, db, connection, project):
    project.procore_project_id = "555"
    db.session.commit()

    responses.add(responses.GET, f"{API}/projects/555/bid_packages", json={}, status=404)
    responses.add(
        responses.GET, f"{API}/bid_packages",
        json=[{"id": 9003, "number": "BP-23A", "title": "HVAC"}], status=200,
    )
    with app.test_request_context():
        result = sync_bid_packages(ProcoreClient(connection), project)
    assert result.packages_created == 1


@pytest.mark.parametrize(
    ("number", "name", "expected"),
    [
        ("BP-21A", "Fire Protection", "21"),
        ("BP-03", "Concrete", "03"),
        ("26-100", "Electrical", "26"),
        ("BP-A", "Miscellaneous", None),
        ("BP-99", "Nonsense", None),   # 99 is not a MasterFormat division
        ("BP-20", "Mechanical Support", None),  # 20 is reserved
    ],
)
def test_division_inference(number, name, expected):
    assert _infer_division(number, name) == expected


def test_disconnect_clears_the_stored_tokens(db, connection):
    connection.disconnect()
    db.session.commit()
    assert connection._access_token is None
    assert connection._refresh_token is None
    assert connection.is_connected is False


def test_service_account_stays_connected_without_a_refresh_token(db, connection):
    connection.grant_type = "client_credentials"
    connection._refresh_token = None
    from datetime import timedelta

    connection.token_expires_at = utcnow() - timedelta(minutes=1)
    db.session.commit()
    # Expired, but a DMSA can mint a new token from the client secret.
    assert connection.is_expired is True
    assert connection.is_connected is True
