"""Rate limiting actually rate-limits.

Every other test runs with ``RATELIMIT_ENABLED = False`` -- rate limits and
fixtures that sign in dozens of times do not mix. That left the limiter with no
coverage at all, which matters because it is the only thing standing between a
password guesser and an unlimited number of attempts. An upgrade could have
silently turned it into a no-op and nothing would have failed.

These tests build their own application with limiting switched on.
"""

from __future__ import annotations

import pytest

from scopemaker import create_app
from scopemaker.extensions import db as _db
from scopemaker.extensions import limiter
from scopemaker.models import Membership
from scopemaker.services.accounts import create_organization, create_user
from scopemaker.services.seeding import seed_library

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture()
def limited_app():
    """A second application instance with rate limiting on."""
    app = create_app(
        "testing",
        RATELIMIT_ENABLED=True,
        RATELIMIT_STORAGE_URI="memory://",
    )
    with app.app_context():
        _db.create_all()
        seed_library()

        organization = create_organization("Limited Contractors")
        user = create_user(
            email="limited@example.com",
            full_name="Limited User",
            password=PASSWORD,
        )
        _db.session.add(
            Membership(
                organization_id=organization.id, user_id=user.id, role="admin"
            )
        )
        _db.session.commit()

        yield app

        _db.session.rollback()
        for table in reversed(_db.metadata.sorted_tables):
            _db.session.execute(table.delete())
        _db.session.commit()

    # ``limiter`` is a module-level singleton shared by every application built
    # in this process, and the most recent init_app wins: leaving it enabled
    # here throttles the session-scoped app used by every other test, whose
    # fixtures sign in far more than ten times a minute. Its in-memory counters
    # outlive the app object too, so both have to be undone.
    #
    # Only a test artefact -- production runs one application per process.
    limiter.enabled = False
    limiter.reset()


def test_login_attempts_are_throttled(limited_app):
    """The eleventh POST inside a minute is refused.

    The route allows "10 per minute". Wrong passwords are used deliberately:
    the limit has to bite on failures, which is the case that matters.
    """
    client = limited_app.test_client()

    statuses = [
        client.post(
            "/auth/login",
            data={"email": "limited@example.com", "password": "wrong-password"},
        ).status_code
        for _ in range(12)
    ]

    assert 429 in statuses, f"never throttled: {statuses}"
    assert statuses.index(429) == 10, (
        f"throttled after {statuses.index(429)} attempts, expected 10"
    )
    # Once throttled, it stays throttled.
    assert statuses[-1] == 429


def test_a_correct_password_does_not_bypass_the_limit(limited_app):
    """Exhaust the limit with guesses, then try the real password."""
    client = limited_app.test_client()

    for _ in range(11):
        client.post(
            "/auth/login",
            data={"email": "limited@example.com", "password": "wrong-password"},
        )

    response = client.post(
        "/auth/login",
        data={"email": "limited@example.com", "password": PASSWORD},
    )
    assert response.status_code == 429


def test_get_requests_are_not_limited(limited_app):
    """The limit is declared for POST only; loading the form is not an attempt."""
    client = limited_app.test_client()
    statuses = {client.get("/auth/login").status_code for _ in range(15)}
    assert statuses == {200}


def test_rate_limit_headers_are_present(limited_app):
    """RATELIMIT_HEADERS_ENABLED is on, so clients can see the budget."""
    client = limited_app.test_client()
    response = client.post(
        "/auth/login",
        data={"email": "limited@example.com", "password": "wrong-password"},
    )
    assert "X-RateLimit-Limit" in response.headers
    assert "X-RateLimit-Remaining" in response.headers
