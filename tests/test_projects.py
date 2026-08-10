"""Projects and bid packages.

The routes that mutate: create, edit, archive, and the package CRUD underneath
them. The guard worth pinning down is the one on deleting a package that still
has scopes attached -- getting that wrong orphans documents.
"""

from __future__ import annotations

from sqlalchemy import select

from scopemaker.models import BidPackage, Project

from .conftest import login


def test_creating_a_project(auth_client, db, organization):
    response = auth_client.post(
        "/projects/new",
        data={
            "name": "Northgate Terminal",
            "number": "2025-004",
            "city": "Columbus",
            "state": "OH",
            "delivery_method": "CMAR",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200

    created = db.session.scalar(
        select(Project).where(Project.name == "Northgate Terminal")
    )
    assert created is not None
    assert created.organization_id == organization.id, "created in the wrong tenant"
    assert created.delivery_method == "CMAR"


def test_editing_a_project(auth_client, db, project):
    response = auth_client.post(
        f"/projects/{project.id}/edit",
        data={
            "name": "Riverside Medical Center - Phase 2",
            "number": project.number,
            "city": project.city or "",
            "state": project.state or "",
            "delivery_method": "",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200

    db.session.refresh(project)
    assert project.name == "Riverside Medical Center - Phase 2"
    assert project.delivery_method is None, "a blank choice should clear, not store ''"


def test_archiving_a_project_toggles(auth_client, db, project):
    assert not project.is_archived

    auth_client.post(f"/projects/{project.id}/archive", follow_redirects=True)
    db.session.refresh(project)
    assert project.is_archived

    auth_client.post(f"/projects/{project.id}/archive", follow_redirects=True)
    db.session.refresh(project)
    assert not project.is_archived, "archive should restore on a second press"


def test_an_archived_project_is_hidden_until_asked_for(auth_client, db, project):
    auth_client.post(f"/projects/{project.id}/archive", follow_redirects=True)

    hidden = auth_client.get("/projects/").get_data(as_text=True)
    assert project.name not in hidden

    shown = auth_client.get("/projects/?archived=1").get_data(as_text=True)
    assert project.name in shown


# ---------------------------------------------------------------------------
# Bid packages
# ---------------------------------------------------------------------------

def test_adding_a_bid_package(auth_client, db, project):
    response = auth_client.post(
        f"/projects/{project.id}/packages/new",
        data={"number": "BP-23A", "name": "HVAC", "division_code": "23",
              "trade_name": "HVAC"},
        follow_redirects=True,
    )
    assert response.status_code == 200

    package = db.session.scalar(
        select(BidPackage).where(BidPackage.number == "BP-23A")
    )
    assert package is not None
    assert package.division_code == "23"
    assert package.organization_id == project.organization_id


def test_editing_a_bid_package(auth_client, db, bid_package):
    auth_client.post(
        f"/projects/packages/{bid_package.id}/edit",
        data={"number": "BP-09A", "name": "Drywall and Framing",
              "division_code": "09", "trade_name": "Finishes"},
        follow_redirects=True,
    )

    db.session.refresh(bid_package)
    assert bid_package.number == "BP-09A"
    assert bid_package.division_code == "09"
    assert bid_package.trade_name == "Finishes"


def test_an_invalid_division_is_rejected_rather_than_stored(
    auth_client, db, bid_package
):
    """division_code is a select, so anything off the list is a forged post.

    normalize_code() in the route is belt-and-braces behind that; the form is
    what actually refuses. Worth a test either way -- a hand-rolled POST is the
    realistic route to a bad division code.
    """
    original = bid_package.division_code

    auth_client.post(
        f"/projects/packages/{bid_package.id}/edit",
        data={"number": bid_package.number, "name": bid_package.name,
              "division_code": "99", "trade_name": "Nonsense"},
        follow_redirects=True,
    )

    db.session.refresh(bid_package)
    assert bid_package.division_code == original, "a reserved division was stored"


def test_deleting_an_empty_bid_package(auth_client, db, project):
    package = BidPackage(
        project_id=project.id,
        organization_id=project.organization_id,
        number="BP-99Z",
        name="Spare",
        division_code="26",
    )
    db.session.add(package)
    db.session.commit()
    package_id = package.id

    auth_client.post(
        f"/projects/packages/{package_id}/delete", follow_redirects=True
    )

    assert db.session.get(BidPackage, package_id) is None


def test_a_package_with_scopes_is_not_deleted(auth_client, db, bid_package, scope):
    """Deleting it would orphan a document somebody may have issued."""
    assert scope.bid_package_id == bid_package.id
    package_id = bid_package.id

    response = auth_client.post(
        f"/projects/packages/{package_id}/delete", follow_redirects=True
    )

    assert "still has scopes" in response.get_data(as_text=True)
    assert db.session.get(BidPackage, package_id) is not None


# ---------------------------------------------------------------------------
# Tenancy and permissions
# ---------------------------------------------------------------------------

def test_another_tenant_cannot_reach_a_project(client, db, other_org, project):
    from scopemaker.models import User

    rival = db.session.query(User).filter_by(email="rival@rival.example").one()
    login(client, rival.email)

    for path in (
        f"/projects/{project.id}",
        f"/projects/{project.id}/edit",
        f"/projects/{project.id}/coverage",
    ):
        assert client.get(path).status_code == 404, f"{path} leaked across tenants"


def test_another_tenant_cannot_delete_a_bid_package(client, db, other_org, bid_package):
    from scopemaker.models import User

    rival = db.session.query(User).filter_by(email="rival@rival.example").one()
    login(client, rival.email)
    package_id = bid_package.id

    assert client.post(
        f"/projects/packages/{package_id}/delete"
    ).status_code == 404
    assert db.session.get(BidPackage, package_id) is not None


def test_a_viewer_cannot_create_or_archive(client, db, viewer, project):
    login(client, viewer.email)

    assert client.post(
        "/projects/new", data={"name": "Nope", "number": "X"}
    ).status_code == 403
    assert client.post(f"/projects/{project.id}/archive").status_code == 403

    db.session.refresh(project)
    assert not project.is_archived
