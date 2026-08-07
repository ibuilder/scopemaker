"""Download a scope in any supported format.

Every download goes through the render cache. A scope that has not changed
since it was last exported is served straight from stored bytes, which is what
absorbs most of the load in practice -- the same exhibit gets downloaded
repeatedly while people review it.

When ``RENDER_ASYNC`` is on and a render *is* needed, the request enqueues a
job and returns a small waiting page instead of holding a worker for a second
or two. With it off (the default, since it needs a worker process) the render
happens inline and is still cached for next time.
"""

from __future__ import annotations

import re

from flask import Response, abort, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from slugify import slugify

from ...extensions import db, limiter
from ...models import Scope
from ...models.render import RenderJob
from ...services import metrics, render_queue
from ...services.renderers import FORMATS, render_html
from ..helpers import current_org_id, get_scope_or_404
from . import bp


def filename_for(scope: Scope, extension: str) -> str:
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


def _serve(payload: bytes, fmt: str, filename: str) -> Response:
    export_format = FORMATS[fmt]
    response = Response(payload, mimetype=export_format.mimetype)
    # PDFs open in the browser's viewer; everything else downloads.
    response.headers["Content-Disposition"] = _content_disposition(
        filename, inline=(fmt == "pdf")
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@bp.route("/<scope_id>.<format_key>")
@login_required
@limiter.limit("60 per minute")
def download(scope_id: str, format_key: str):
    scope = get_scope_or_404(scope_id)
    if format_key not in FORMATS:
        abort(404)

    organization = current_user.active_organization
    print_key = render_queue.fingerprint(scope, organization)

    # 1. Already rendered this exact document state?
    hit = render_queue.cached(scope, format_key, print_key)
    if hit is not None and hit.result is not None:
        response = _serve(hit.result, format_key, hit.filename or filename_for(
            scope, FORMATS[format_key].extension))
        response.headers["X-Render-Cache"] = "hit"
        metrics.increment("scopemaker_export_cache_total",
                          {"format": format_key, "result": "hit"})
        return response

    # 2. Hand it to a worker when one is running.
    if render_queue.async_enabled():
        job = render_queue.enqueue(
            scope, format_key, organization=organization, user_id=current_user.id
        )
        if not job.is_terminal:
            return redirect(url_for("exports.job_status", job_id=job.id))
        if job.status == "failed":
            from ...errors import RenderError

            raise RenderError(job.error or "The document could not be rendered.")
        if job.result is None:
            # Oversized: not retained, so fall through and render it again.
            return redirect(url_for("exports.job_status", job_id=job.id))
        return _serve(
            job.result, format_key,
            job.filename or filename_for(scope, FORMATS[format_key].extension),
        )

    # 3. Render inline, and cache it so the next request is free.
    job = RenderJob(
        organization_id=scope.organization_id,
        scope_id=scope.id,
        requested_by_id=current_user.id,
        format=format_key,
        fingerprint=print_key,
        status="running",
    )
    db.session.add(job)
    db.session.commit()

    render_queue.render_now(job)
    if job.status == "failed":
        from ...errors import RenderError

        raise RenderError(job.error or "The document could not be rendered.")

    # mark_complete drops oversized payloads rather than caching them, so fall
    # back to rendering once more only in that (rare) case.
    payload = job.result
    if payload is None:
        from ...services.renderers import render_docx, render_json, render_markdown, render_pdf

        renderers = {"pdf": render_pdf, "docx": render_docx,
                     "json": render_json, "md": render_markdown}
        payload = (
            render_html(scope, organization=organization, standalone=True).encode()
            if format_key == "html"
            else renderers[format_key](scope, organization=organization)
        )

    response = _serve(payload, format_key, job.filename or filename_for(
        scope, FORMATS[format_key].extension))
    response.headers["X-Render-Cache"] = "miss"
    metrics.increment("scopemaker_export_cache_total",
                      {"format": format_key, "result": "miss"})
    return response


@bp.route("/jobs/<job_id>")
@login_required
def job_status(job_id: str):
    """Waiting page for an async render."""
    job = db.session.get(RenderJob, job_id)
    if job is None or job.organization_id != current_org_id():
        abort(404)

    if job.is_complete and job.result is not None:
        return _serve(job.result, job.format, job.filename or "scope")
    if job.status == "failed":
        from ...errors import RenderError

        raise RenderError(job.error or "The document could not be rendered.")

    scope = db.session.get(Scope, job.scope_id)
    return render_template("exports/pending.html", job=job, scope=scope), 202


@bp.route("/jobs/<job_id>/state")
@login_required
def job_state(job_id: str):
    """Polled by the waiting page."""
    job = db.session.get(RenderJob, job_id)
    if job is None or job.organization_id != current_org_id():
        abort(404)
    return jsonify(
        {
            "status": job.status,
            "ready": job.is_complete,
            "error": job.error,
            "download": url_for("exports.job_status", job_id=job.id)
            if job.is_complete
            else None,
        }
    )


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


__all__ = ["filename_for", "request"]
