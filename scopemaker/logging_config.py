"""Logging setup.

``LOG_FORMAT=json`` emits one JSON object per line, which is what you want when
logs are shipped to CloudWatch/Loki/Datadog.  ``text`` stays human-readable for
local development.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

from flask import Flask, g, has_request_context, request

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "asctime",
    "message",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if has_request_context():
            payload["request_id"] = getattr(g, "request_id", None)
            payload["method"] = request.method
            payload["path"] = request.path
            payload["remote_addr"] = request.remote_addr
        # Preserve structured extras passed via logger.info(..., extra={...}).
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        return json.dumps(payload, default=str)


class RequestContextFilter(logging.Filter):
    """Adds the per-request id to text-formatted records."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = getattr(g, "request_id", "-") if has_request_context() else "-"
        return True


def configure_logging(app: Flask) -> None:
    level = getattr(logging, str(app.config.get("LOG_LEVEL", "INFO")), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)

    if str(app.config.get("LOG_FORMAT", "text")).lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-8s [%(request_id)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        handler.addFilter(RequestContextFilter())

    root = logging.getLogger()
    # Replace handlers so repeated create_app() calls (tests) don't duplicate output.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    app.logger.handlers = []
    app.logger.propagate = True
    app.logger.setLevel(level)

    # Werkzeug's own request log is noise once we log requests ourselves.
    logging.getLogger("werkzeug").setLevel(
        logging.WARNING if not app.debug else logging.INFO
    )
