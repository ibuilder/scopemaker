"""Procore REST API client.

Two grant types are supported, both entirely server-side:

* **authorization_code** -- a person clicks "Connect to Procore" and the
  resulting tokens are stored encrypted against their organization.
* **client_credentials** -- a Developer Managed Service Account (DMSA), for
  unattended sync.  Procore retired traditional service accounts on
  2025-03-18, so DMSA is the supported path for background jobs.

The client secret never leaves the server.  The prototype this replaces kept
it in ``localStorage``, where any script on the page could read it.
"""

from __future__ import annotations

import logging
from typing import Any

import requests
from flask import current_app

from ..errors import IntegrationError
from ..extensions import db
from ..models import ProcoreConnection

logger = logging.getLogger(__name__)

API_VERSION = "rest/v1.0"
# Procore paginates with page/per_page and caps per_page at 100.
MAX_PER_PAGE = 100
# A hard stop so a misbehaving endpoint cannot spin forever.
MAX_PAGES = 50


class ProcoreClient:
    """Authenticated access to one organization's Procore connection."""

    def __init__(self, connection: ProcoreConnection):
        self.connection = connection
        config = current_app.config
        self.api_base = config["PROCORE_API_BASE"].rstrip("/")
        self.login_base = config["PROCORE_LOGIN_BASE"].rstrip("/")
        self.client_id = config["PROCORE_CLIENT_ID"]
        self.client_secret = config["PROCORE_CLIENT_SECRET"]
        self.timeout = config.get("PROCORE_TIMEOUT", 30)

    # -- Token management ---------------------------------------------------
    @property
    def token_url(self) -> str:
        return f"{self.login_base}/oauth/token"

    def _post_token(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = {
            **payload,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        try:
            response = requests.post(self.token_url, data=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            raise IntegrationError(f"Could not reach Procore: {exc}") from exc

        if response.status_code >= 400:
            detail = _error_detail(response)
            logger.warning("Procore token request failed (%s): %s",
                           response.status_code, detail)
            raise IntegrationError(f"Procore rejected the token request: {detail}")
        return response.json()

    def exchange_code(self, code: str, redirect_uri: str) -> dict[str, Any]:
        return self._post_token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            }
        )

    def refresh(self) -> dict[str, Any]:
        """Refresh the access token, or mint a new one for a service account."""
        if self.connection.grant_type == "client_credentials":
            return self._post_token({"grant_type": "client_credentials"})

        refresh_token = self.connection.refresh_token
        if not refresh_token:
            raise IntegrationError(
                "This Procore connection has expired and has no refresh token. "
                "Reconnect from the integration settings.",
                code="procore_reauth_required",
            )
        return self._post_token(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    def ensure_token(self) -> str:
        """Return a usable access token, refreshing it when necessary."""
        if self.connection.is_expired:
            payload = self.refresh()
            self.connection.apply_token_response(payload)
            db.session.commit()
        token = self.connection.access_token
        if not token:
            raise IntegrationError(
                "The stored Procore credentials could not be read. This usually "
                "means ENCRYPTION_KEY was rotated; reconnect to Procore.",
                code="procore_reauth_required",
            )
        return token

    # -- Requests -----------------------------------------------------------
    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.ensure_token()}",
            "Accept": "application/json",
        }
        if self.connection.company_id:
            # Required by most v1.1+ endpoints and harmless on the rest.
            headers["Procore-Company-Id"] = str(self.connection.company_id)
        if extra:
            headers.update(extra)
        return headers

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        files: Any = None,
        data: Any = None,
        _retry: bool = True,
    ) -> Any:
        url = f"{self.api_base}/{API_VERSION}/{path.lstrip('/')}"
        try:
            response = requests.request(
                method,
                url,
                headers=self._headers(),
                params=params,
                json=json_body,
                files=files,
                data=data,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise IntegrationError(f"Could not reach Procore: {exc}") from exc

        # A 401 mid-session means the token died early; refresh once and retry
        # rather than making the user reconnect.
        if response.status_code == 401 and _retry:
            logger.info("Procore returned 401; refreshing token and retrying")
            self.connection.apply_token_response(self.refresh())
            db.session.commit()
            return self.request(
                method, path, params=params, json_body=json_body,
                files=files, data=data, _retry=False,
            )

        if response.status_code == 403:
            raise IntegrationError(
                "Procore denied access to that resource. Check the app's "
                "permissions and the connected user's project access.",
                code="procore_forbidden",
            )
        if response.status_code == 404:
            raise IntegrationError("That Procore resource was not found.",
                                   code="procore_not_found")
        if response.status_code == 429:
            raise IntegrationError(
                "Procore rate limit reached. Try again shortly.",
                code="procore_rate_limited",
            )
        if response.status_code >= 400:
            raise IntegrationError(
                f"Procore returned {response.status_code}: {_error_detail(response)}"
            )

        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params or None)

    def paginated(self, path: str, **params: Any) -> list[dict[str, Any]]:
        """Follow Procore's page/per_page pagination to the end."""
        results: list[dict[str, Any]] = []
        page = 1
        while page <= MAX_PAGES:
            batch = self.request(
                "GET", path, params={**params, "page": page, "per_page": MAX_PER_PAGE}
            )
            if not batch:
                break
            if isinstance(batch, dict):
                # Some endpoints wrap the collection in a key.
                batch = next(
                    (v for v in batch.values() if isinstance(v, list)), []
                )
            results.extend(batch)
            if len(batch) < MAX_PER_PAGE:
                break
            page += 1
        else:  # pragma: no cover - defensive
            logger.warning("Stopped paginating %s after %s pages", path, MAX_PAGES)
        return results

    # -- Endpoints ----------------------------------------------------------
    def me(self) -> dict[str, Any]:
        return self.get("me")

    def companies(self) -> list[dict[str, Any]]:
        return self.paginated("companies")

    def projects(self, company_id: str | int) -> list[dict[str, Any]]:
        return self.paginated("projects", company_id=company_id)

    def project(self, project_id: str | int) -> dict[str, Any]:
        return self.get(f"projects/{project_id}")

    def bid_packages(self, project_id: str | int) -> list[dict[str, Any]]:
        """Bid packages for a project.

        Procore has moved this endpoint between namespaces across API
        versions, so both known shapes are tried before giving up.
        """
        try:
            return self.paginated(f"projects/{project_id}/bid_packages")
        except IntegrationError as exc:
            if getattr(exc, "code", "") != "procore_not_found":
                raise
            logger.info("Falling back to the flat bid_packages endpoint")
            return self.paginated("bid_packages", project_id=project_id)

    def commitments(self, project_id: str | int) -> list[dict[str, Any]]:
        return self.paginated("work_order_contracts", project_id=project_id)

    def upload_commitment_attachment(
        self, project_id: str | int, commitment_id: str | int,
        filename: str, content: bytes, mimetype: str,
    ) -> Any:
        """Attach a generated exhibit to a Procore commitment."""
        return self.request(
            "POST",
            f"work_order_contracts/{commitment_id}/attachments",
            data={"project_id": str(project_id)},
            files={"attachments[]": (filename, content, mimetype)},
        )


def _error_detail(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return (response.text or "")[:300]
    for key in ("error_description", "errors", "error", "message"):
        if key in payload:
            return str(payload[key])[:300]
    return str(payload)[:300]


def authorize_url(redirect_uri: str, state: str) -> str:
    """Build the Procore consent URL."""
    from urllib.parse import urlencode

    config = current_app.config
    params = {
        "client_id": config["PROCORE_CLIENT_ID"],
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"{config['PROCORE_LOGIN_BASE'].rstrip('/')}/oauth/authorize?{urlencode(params)}"


def get_connection(organization_id: str) -> ProcoreConnection | None:
    from sqlalchemy import select

    return db.session.scalar(
        select(ProcoreConnection).where(
            ProcoreConnection.organization_id == organization_id
        )
    )


def client_for(organization_id: str) -> ProcoreClient:
    connection = get_connection(organization_id)
    if connection is None or not connection.is_connected:
        raise IntegrationError(
            "This organization is not connected to Procore.",
            code="procore_not_connected",
        )
    return ProcoreClient(connection)
