"""Application error types and the handlers that render them.

HTML routes get a rendered error page; anything under ``/api/`` gets a JSON
envelope so clients never have to parse an HTML error page.
"""

from __future__ import annotations

import logging
import uuid

from flask import Flask, Response, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

logger = logging.getLogger(__name__)


class ScopeMakerError(Exception):
    """Base class for expected, user-facing application failures."""

    status_code = 400
    code = "error"

    def __init__(self, message: str, *, code: str | None = None, details: dict | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details or {}

    def to_dict(self) -> dict:
        payload = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            payload["error"]["details"] = self.details
        return payload


class ValidationError(ScopeMakerError):
    status_code = 422
    code = "validation_error"


class NotFoundError(ScopeMakerError):
    status_code = 404
    code = "not_found"


class PermissionDeniedError(ScopeMakerError):
    status_code = 403
    code = "permission_denied"


class ConflictError(ScopeMakerError):
    status_code = 409
    code = "conflict"


class IntegrationError(ScopeMakerError):
    """A third-party system (Procore, the IdP) failed or misbehaved."""

    status_code = 502
    code = "integration_error"


class RenderError(ScopeMakerError):
    """A document could not be rendered into the requested format."""

    status_code = 500
    code = "render_error"


def _wants_json() -> bool:
    if request.path.startswith("/api/"):
        return True
    accept = request.accept_mimetypes
    return accept.accept_json and not accept.accept_html


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(ScopeMakerError)
    def _handle_app_error(exc: ScopeMakerError) -> Response | tuple:
        if _wants_json():
            return jsonify(exc.to_dict()), exc.status_code
        return (
            render_template(
                "errors/generic.html",
                status_code=exc.status_code,
                title=exc.code.replace("_", " ").title(),
                message=exc.message,
            ),
            exc.status_code,
        )

    @app.errorhandler(HTTPException)
    def _handle_http_error(exc: HTTPException) -> Response | tuple:
        status = exc.code or 500
        if _wants_json():
            return (
                jsonify(
                    {
                        "error": {
                            "code": (exc.name or "http_error").lower().replace(" ", "_"),
                            "message": exc.description or exc.name or "",
                        }
                    }
                ),
                status,
            )
        template = f"errors/{status}.html"
        try:
            return render_template(template, error=exc), status
        except Exception:
            return (
                render_template(
                    "errors/generic.html",
                    status_code=status,
                    title=exc.name,
                    message=exc.description,
                ),
                status,
            )

    @app.errorhandler(Exception)
    def _handle_unexpected(exc: Exception) -> Response | tuple:
        # Correlate the opaque page the user sees with the traceback in the log.
        incident = uuid.uuid4().hex[:12]
        logger.exception("Unhandled exception [incident=%s] on %s", incident, request.path)
        if _wants_json():
            return (
                jsonify(
                    {
                        "error": {
                            "code": "internal_error",
                            "message": "An unexpected error occurred.",
                            "incident": incident,
                        }
                    }
                ),
                500,
            )
        return render_template("errors/500.html", incident=incident), 500
