"""Download a scope in any supported format."""

from __future__ import annotations

import re

from flask import Response, abort
from flask_login import current_user, login_required
from slugify import slugify

from ...extensions import limiter
from ...models import Scope
from ...services.renderers import (
    FORMATS,
    render_docx,
    render_html,
    render_json,
    render_markdown,
    render_pdf,
)
from ..helpers import get_scope_or_404
from . import bp

RENDERERS = {
    "pdf": render_pdf,
    "docx": render_docx,
    "json": render_json,
    "md": render_markdown,
}


def _filename(scope: Scope, extension: str) -> str:
    """A descriptive, filesystem-safe download name."""
    parts = [scope.exhibit_label, scope.trade_name or scope.title]
    if scope.bid_package is not None:
        parts.insert(0, scope.bid_package.number)
    stem = slugify(" ".join(p for p in parts if p), separator="-")[:120] or "scope"
    if scope.version > 1:
        stem = f"{stem}-v{scope.version}"
    return f"{stem}.{extension}"


def _content_disposition(filename: str, *, inline: bool = False) -> str:
    """RFC 6266 header with an ASCII fallback for non-Latin filenames."""
    disposition = "inline" if inline else "attachment"
    ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "scope"
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{filename}"


@bp.route("/<scope_id>.<format_key>")
@login_required
@limiter.limit("60 per minute")
def download(scope_id: str, format_key: str):
    scope = get_scope_or_404(scope_id)
    export_format = FORMATS.get(format_key)
    if export_format is None:
        abort(404)

    organization = current_user.active_organization

    if format_key == "html":
        body = render_html(scope, organization=organization, standalone=True)
        response = Response(body, mimetype=export_format.mimetype)
    else:
        # render_pdf raises RenderError when the native stack is missing; the
        # global handler turns that into a clear message rather than a 500.
        payload = RENDERERS[format_key](scope, organization=organization)
        response = Response(payload, mimetype=export_format.mimetype)

    filename = _filename(scope, export_format.extension)
    # PDFs open in the browser's viewer; everything else downloads.
    response.headers["Content-Disposition"] = _content_disposition(
        filename, inline=(format_key == "pdf")
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@bp.route("/<scope_id>/print")
@login_required
def print_view(scope_id: str):
    """Browser-printable page -- the same markup WeasyPrint renders.

    Useful when the server-side PDF stack is unavailable, and as a way to check
    that the print layout and the generated PDF agree.
    """
    scope = get_scope_or_404(scope_id)
    html = render_html(
        scope, organization=current_user.active_organization, standalone=True
    )
    return Response(html, mimetype="text/html; charset=utf-8")
