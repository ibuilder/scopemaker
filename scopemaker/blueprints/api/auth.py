"""Bearer-token authentication for the JSON API.

Tokens are stored as Argon2 hashes.  Lookup is by the non-secret prefix, then
the candidate hashes are verified -- so a stolen database still does not yield
usable tokens.

Argon2 is deliberately slow, which is right for a password typed once and wrong
for a token presented on every call: verification alone was most of a ~150ms
API request. A short-lived cache keeps the hash comparison off the hot path
without weakening revocation -- see ``_verified_cache`` below.
"""

from __future__ import annotations

import functools
import hashlib
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from flask import current_app, g, request
from flask_login import current_user
from sqlalchemy import select

from ...errors import PermissionDeniedError, ScopeMakerError
from ...extensions import db
from ...models import ApiToken
from ...models.base import utcnow

# ---------------------------------------------------------------------------
# Verified-token cache
# ---------------------------------------------------------------------------
#
# Maps a keyed digest of the presented token to the id of the ApiToken row it
# verified against. Three properties make this safe:
#
#   * The raw token is never a key. It is hashed with BLAKE2b keyed on
#     SECRET_KEY, so a memory dump does not yield usable credentials.
#   * Only the *hash comparison* is cached, never the authorization decision.
#     Every request still loads the row and re-checks revocation and expiry, so
#     revoking a token takes effect on the very next call.
#   * Entries expire, and the cache is bounded, so it cannot grow without limit
#     under a flood of distinct tokens.
#
# It is per process, like the metrics counters -- each gunicorn worker warms its
# own, which costs one Argon2 verification per worker per token.

CACHE_TTL_SECONDS = 300
CACHE_MAX_ENTRIES = 2048

#: Skip the last-used write unless the stored stamp is at least this old. It is
#: a "roughly when was this token last seen" field, not an audit record, and a
#: database write on every API call is a poor way to maintain one.
LAST_USED_RESOLUTION = timedelta(minutes=5)

_cache_lock = threading.Lock()
_verified_cache: dict[str, tuple[str, float]] = {}


def _cache_key(raw: str) -> str:
    secret = str(current_app.config.get("SECRET_KEY", "")).encode()
    return hashlib.blake2b(raw.encode(), key=secret[:64], digest_size=32).hexdigest()


def _cached_token_id(key: str) -> str | None:
    now = time.monotonic()
    with _cache_lock:
        entry = _verified_cache.get(key)
        if entry is None:
            return None
        token_id, expires = entry
        if expires <= now:
            del _verified_cache[key]
            return None
        return token_id


def _remember(key: str, token_id: str) -> None:
    now = time.monotonic()
    with _cache_lock:
        if len(_verified_cache) >= CACHE_MAX_ENTRIES:
            for stale, (_, expires) in list(_verified_cache.items()):
                if expires <= now:
                    del _verified_cache[stale]
            if len(_verified_cache) >= CACHE_MAX_ENTRIES:
                _verified_cache.clear()
        _verified_cache[key] = (token_id, now + CACHE_TTL_SECONDS)


def clear_token_cache() -> None:
    """Drop every cached verification. Used by tests."""
    with _cache_lock:
        _verified_cache.clear()


class UnauthorizedError(ScopeMakerError):
    status_code = 401
    code = "unauthorized"


def _token_from_request() -> str | None:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def _resolve_token(raw: str) -> ApiToken | None:
    key = _cache_key(raw)

    # A previously verified token still has to prove it is *currently* valid:
    # the cache short-circuits the Argon2 comparison, nothing else.
    cached_id = _cached_token_id(key)
    if cached_id is not None:
        token = db.session.get(ApiToken, cached_id)
        if token is not None and token.is_valid:
            return token
        # Revoked, expired or deleted since it was cached.
        with _cache_lock:
            _verified_cache.pop(key, None)
        return None

    prefix = raw[: len(ApiToken.PREFIX) + 8]
    candidates = db.session.scalars(
        select(ApiToken).where(
            ApiToken.token_prefix == prefix, ApiToken.revoked_at.is_(None)
        )
    )
    for token in candidates:
        if token.is_valid and token.matches(raw):
            _remember(key, token.id)
            return token
    return None


def _touch(token: ApiToken) -> None:
    """Record that the token was used, at a coarse resolution."""
    now = utcnow()
    last = token.last_used_at
    if last is not None:
        if last.tzinfo is None:
            from datetime import UTC

            last = last.replace(tzinfo=UTC)
        if now - last < LAST_USED_RESOLUTION:
            return
    token.last_used_at = now
    db.session.commit()


class PolicyError(ScopeMakerError):
    status_code = 403
    code = "mfa_required"


def _enforce_organization_policy(user, organization_id: str) -> None:
    """Apply the organization's access policy to a bearer-token request.

    The request hook that enforces this for browsers keys off Flask-Login, and
    an API token is not a session -- so without this check a token would be a
    way around a policy the organization has explicitly turned on. A token
    issued *before* the policy was enabled is exactly the case that matters.
    """
    from ...models import Organization

    organization = db.session.get(Organization, organization_id)
    if organization is None or not organization.setting("require_mfa"):
        return
    if user is None or user.mfa_enabled or user.is_sso_only:
        return
    raise PolicyError(
        "This organization requires two-factor authentication. Enable it on "
        "the account that owns this token, or have an administrator issue a "
        "token from a compliant account."
    )


def authenticate() -> None:
    """Populate ``g`` with the caller's identity, or raise 401."""
    raw = _token_from_request()
    if raw:
        token = _resolve_token(raw)
        if token is None:
            raise UnauthorizedError("Invalid or expired API token.")
        # A best-effort last-used stamp; not worth failing a request over.
        _touch(token)
        _enforce_organization_policy(token.user, token.organization_id)

        g.api_token = token
        g.api_organization_id = token.organization_id
        g.api_scopes = set(token.scopes.split())
        g.api_user = token.user
        return

    # Fall back to the browser session so the same endpoints work from the UI.
    if current_user.is_authenticated and current_user.active_organization_id:
        g.api_token = None
        g.api_organization_id = current_user.active_organization_id
        g.api_scopes = {"read", "write"}
        g.api_user = current_user
        return

    raise UnauthorizedError(
        "Provide an API token: Authorization: Bearer smk_..."
    )


def api_auth(*, write: bool = False) -> Callable:
    """Require authentication, and optionally the ``write`` scope."""

    def decorator(view: Callable) -> Callable:
        @functools.wraps(view)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            authenticate()
            if write and "write" not in g.api_scopes:
                raise PermissionDeniedError(
                    "This token is read-only.", code="insufficient_scope"
                )
            return view(*args, **kwargs)

        return wrapper

    return decorator


def api_org_id() -> str:
    organization_id = getattr(g, "api_organization_id", None)
    if not organization_id:
        raise UnauthorizedError("No organization is associated with this request.")
    return organization_id
