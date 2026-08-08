"""The verified-token cache keeps Argon2 off the hot path -- and only that.

Argon2 is deliberately slow, which is correct for a password typed once and
wrong for a token presented on every API call. Caching the *verification* is
safe; caching the *authorization decision* would not be, because a revoked
token would keep working until the entry expired. These tests pin that
distinction down, since it is the kind of thing a later refactor quietly gets
wrong.
"""

from __future__ import annotations

import pytest

from scopemaker.blueprints.api import auth as api_auth
from scopemaker.models import ApiToken
from scopemaker.models.base import utcnow


@pytest.fixture(autouse=True)
def _clear_cache():
    api_auth.clear_token_cache()
    yield
    api_auth.clear_token_cache()


@pytest.fixture()
def token(db, user, organization):
    record, raw = ApiToken.issue(
        user=user, organization_id=organization.id, name="cache", scopes="read write"
    )
    db.session.add(record)
    db.session.commit()
    return raw


def headers(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


class VerifyCounter:
    """Count Argon2 comparisons, which is what the cache exists to avoid."""

    def __init__(self, monkeypatch):
        self.calls = 0
        original = ApiToken.matches

        def counted(inner_self, raw):
            self.calls += 1
            return original(inner_self, raw)

        monkeypatch.setattr(ApiToken, "matches", counted)


# ---------------------------------------------------------------------------
# The cache does its job
# ---------------------------------------------------------------------------

def test_repeat_requests_skip_the_hash_comparison(client, token, monkeypatch):
    counter = VerifyCounter(monkeypatch)

    for _ in range(5):
        assert client.get("/api/v1/me", headers=headers(token)).status_code == 200

    assert counter.calls == 1, (
        f"verified the hash {counter.calls} times for 5 requests"
    )


def test_a_wrong_token_is_never_cached(client, token, monkeypatch):
    """Only successes are remembered; a bad token pays the cost every time."""
    counter = VerifyCounter(monkeypatch)

    for _ in range(3):
        response = client.get("/api/v1/me", headers=headers(token + "x"))
        assert response.status_code == 401

    assert counter.calls == 3


# ---------------------------------------------------------------------------
# ...without weakening anything
# ---------------------------------------------------------------------------

def test_revocation_takes_effect_on_the_next_request(client, db, token):
    """The whole risk of this cache, in one test."""
    assert client.get("/api/v1/me", headers=headers(token)).status_code == 200

    record = db.session.query(ApiToken).filter_by(name="cache").one()
    record.revoked_at = utcnow()
    db.session.commit()

    assert client.get("/api/v1/me", headers=headers(token)).status_code == 401


def test_expiry_takes_effect_on_the_next_request(client, db, token):
    assert client.get("/api/v1/me", headers=headers(token)).status_code == 200

    record = db.session.query(ApiToken).filter_by(name="cache").one()
    record.expires_at = utcnow() - api_auth.LAST_USED_RESOLUTION
    db.session.commit()

    assert client.get("/api/v1/me", headers=headers(token)).status_code == 401


def test_deleting_the_token_takes_effect_on_the_next_request(client, db, token):
    assert client.get("/api/v1/me", headers=headers(token)).status_code == 200

    record = db.session.query(ApiToken).filter_by(name="cache").one()
    db.session.delete(record)
    db.session.commit()

    assert client.get("/api/v1/me", headers=headers(token)).status_code == 401


def test_the_raw_token_is_not_a_cache_key(client, app, token):
    """A memory dump must not hand over usable credentials."""
    client.get("/api/v1/me", headers=headers(token))

    keys = list(api_auth._verified_cache)
    assert len(keys) == 1
    assert token not in keys[0]
    assert token[len(ApiToken.PREFIX):] not in keys[0]


def test_the_cache_is_bounded(client, app, token):
    """A flood of distinct tokens cannot grow it without limit."""
    with app.app_context():
        for index in range(api_auth.CACHE_MAX_ENTRIES + 50):
            api_auth._remember(f"key-{index}", "some-token-id")

    assert len(api_auth._verified_cache) <= api_auth.CACHE_MAX_ENTRIES


def test_an_expired_entry_is_not_used(client, db, token, monkeypatch):
    """Past the TTL the hash is compared again rather than trusted."""
    counter = VerifyCounter(monkeypatch)
    assert client.get("/api/v1/me", headers=headers(token)).status_code == 200
    assert counter.calls == 1

    monkeypatch.setattr(api_auth, "CACHE_TTL_SECONDS", -1)
    api_auth.clear_token_cache()

    assert client.get("/api/v1/me", headers=headers(token)).status_code == 200
    assert counter.calls == 2


# ---------------------------------------------------------------------------
# last_used_at
# ---------------------------------------------------------------------------

def test_last_used_is_not_written_on_every_request(client, db, token):
    """It answers "roughly when was this seen", which does not justify a write
    per API call."""
    client.get("/api/v1/me", headers=headers(token))
    record = db.session.query(ApiToken).filter_by(name="cache").one()
    first = record.last_used_at
    assert first is not None

    for _ in range(4):
        client.get("/api/v1/me", headers=headers(token))

    db.session.refresh(record)
    assert record.last_used_at == first


def test_last_used_is_written_once_it_is_stale(client, db, token):
    client.get("/api/v1/me", headers=headers(token))
    record = db.session.query(ApiToken).filter_by(name="cache").one()

    record.last_used_at = utcnow() - api_auth.LAST_USED_RESOLUTION * 2
    db.session.commit()
    stale = record.last_used_at

    client.get("/api/v1/me", headers=headers(token))
    db.session.refresh(record)
    assert record.last_used_at > stale
