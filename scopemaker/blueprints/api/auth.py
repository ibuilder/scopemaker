"""Bearer-token authentication for the JSON API.

Tokens are stored as Argon2 hashes.  Lookup is by the non-secret prefix, then
the candidate hashes are verified -- so a stolen database still does not yield
usable tokens.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from flask import g, request
from flask_login import current_user
from sqlalchemy import select

from ...errors import PermissionDeniedError, ScopeMakerError
from ...extensions import db
from ...models import ApiToken
from ...models.base import utcnow


class UnauthorizedError(ScopeMakerError):
    status_code = 401
    code = "unauthorized"


def _token_from_request() -> str | None:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def _resolve_token(raw: str) -> ApiToken | None:
    prefix = raw[: len(ApiToken.PREFIX) + 8]
    candidates = db.session.scalars(
        select(ApiToken).where(
            ApiToken.token_prefix == prefix, ApiToken.revoked_at.is_(None)
        )
    )
    for token in candidates:
        if token.is_valid and token.matches(raw):
            return token
    return None


def authenticate() -> None:
    """Populate ``g`` with the caller's identity, or raise 401."""
    raw = _token_from_request()
    if raw:
        token = _resolve_token(raw)
        if token is None:
            raise UnauthorizedError("Invalid or expired API token.")
        # A best-effort last-used stamp; not worth failing a request over.
        token.last_used_at = utcnow()
        db.session.commit()
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
