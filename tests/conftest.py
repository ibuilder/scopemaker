"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from scopemaker import create_app
from scopemaker.extensions import db as _db
from scopemaker.models import BidPackage, Membership, Organization, Project, User
from scopemaker.services import library as library_service
from scopemaker.services.accounts import create_organization, create_user
from scopemaker.services.scope_builder import ScopeDraft, build_scope
from scopemaker.services.seeding import seed_library


@pytest.fixture(scope="session")
def app():
    """The application, with the shipped library seeded once.

    Note that no app context is held open here. Flask reuses an already-pushed
    app context for test-client requests, and ``g`` lives on that context -- so
    a session-scoped context would let Flask-Login's cached user leak from one
    test into the next. Each test pushes its own context in the ``db`` fixture.
    """
    application = create_app("testing")
    with application.app_context():
        _db.create_all()
        seed_library()

    yield application

    with application.app_context():
        _db.session.remove()
        _db.drop_all()


# Rows seeded once per session and shared by every test. Everything else is
# cleared between tests.
SYSTEM_LIBRARY_TABLES = {"clauses", "spec_sections"}


@pytest.fixture()
def db(app):
    """Isolate each test by clearing tenant data afterwards.

    Wrapping tests in a rolled-back transaction does not work here:
    Flask-SQLAlchemy overrides ``Session.get_bind``, so a session rebound to a
    test-owned connection is silently ignored and the writes land for real.
    Deleting the rows afterwards is blunt but actually isolates. The shipped
    library (``organization_id IS NULL``) is left in place so the expensive
    seed runs only once for the session.
    """
    with app.app_context():
        yield _db

        _db.session.rollback()
        for table in reversed(_db.metadata.sorted_tables):
            if table.name in SYSTEM_LIBRARY_TABLES:
                _db.session.execute(
                    table.delete().where(table.c.organization_id.is_not(None))
                )
            else:
                _db.session.execute(table.delete())
        _db.session.commit()


@pytest.fixture()
def client(app, db):
    return app.test_client()


@pytest.fixture()
def organization(db) -> Organization:
    org = create_organization("Meridian Construction")
    db.session.commit()
    return org


@pytest.fixture()
def user(db, organization) -> User:
    account = create_user(
        email="dana@meridian.example",
        full_name="Dana Reyes",
        password="correct-horse-battery-staple",
    )
    db.session.add(
        Membership(organization_id=organization.id, user_id=account.id, role="admin")
    )
    db.session.commit()
    return account


@pytest.fixture()
def viewer(db, organization) -> User:
    account = create_user(
        email="viewer@meridian.example",
        full_name="Val Viewer",
        password="correct-horse-battery-staple",
    )
    db.session.add(
        Membership(organization_id=organization.id, user_id=account.id, role="viewer")
    )
    db.session.commit()
    return account


@pytest.fixture()
def other_org(db) -> Organization:
    """A second tenant, for isolation tests."""
    org = create_organization("Rival Builders")
    account = create_user(
        email="rival@rival.example",
        full_name="Rival User",
        password="correct-horse-battery-staple",
    )
    db.session.add(Membership(organization_id=org.id, user_id=account.id, role="admin"))
    db.session.commit()
    return org


@pytest.fixture()
def project(db, organization) -> Project:
    record = Project(
        organization_id=organization.id,
        name="Riverside Medical Center",
        number="2024-118",
        address="1400 River Road",
        city="Columbus",
        state="OH",
        owner_name="Riverside Health System",
        architect_name="Whitfield Architects",
        contractor_name="Meridian Construction",
        delivery_method="CMAR",
    )
    db.session.add(record)
    db.session.commit()
    return record


@pytest.fixture()
def bid_package(db, project) -> BidPackage:
    record = BidPackage(
        project_id=project.id,
        organization_id=project.organization_id,
        number="BP-21A",
        name="Fire Protection",
        division_code="21",
        trade_name="Fire Protection",
    )
    db.session.add(record)
    db.session.commit()
    return record


@pytest.fixture()
def scope(db, organization, project, bid_package, user):
    """A generated Division 21 scope, the way the wizard would build it."""
    return build_scope(
        ScopeDraft(
            organization_id=organization.id,
            division_code="21",
            project_id=project.id,
            bid_package_id=bid_package.id,
            clause_ids=library_service.default_clause_ids(organization.id, "21"),
            spec_section_ids=library_service.default_spec_section_ids(
                organization.id, "21"
            ),
            created_by_id=user.id,
            base_bid_amount=1_425_000,
        )
    )


@pytest.fixture()
def auth_client(client, user):
    """A test client already signed in as the admin user."""
    response = client.post(
        "/auth/login",
        data={"email": user.email, "password": "correct-horse-battery-staple"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    return client


def login(client, email: str, password: str = "correct-horse-battery-staple"):
    return client.post(
        "/auth/login",
        data={"email": email, "password": password},
        follow_redirects=True,
    )


def drop_login_cache() -> None:
    """Forget the user Flask-Login cached on the app context.

    Flask-Login memoises the resolved user on ``g``. The ``db`` fixture holds a
    single app context open for the whole test, and Flask reuses an
    already-pushed context for test-client requests -- so that cache survives
    between requests in a way it never does in production, where every request
    pushes and pops its own context.

    Any test that asserts a session has been *invalidated* has to clear it
    first, or the stale cached user answers the request and the assertion
    passes or fails for the wrong reason. Verified against a run with no held
    context: revocation logs both browsers out exactly as these tests expect.
    """
    from flask import g

    g.pop("_login_user", None)
