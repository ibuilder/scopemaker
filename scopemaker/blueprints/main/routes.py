"""Landing page, dashboard and health checks."""

from __future__ import annotations

from flask import current_app, jsonify, redirect, render_template, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, select

from ... import __version__
from ...extensions import db
from ...models import Project, Scope
from ...services.library import library_stats
from ...services.renderers import PDF_AVAILABLE
from . import bp


@bp.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    return render_template("main/index.html")


@bp.route("/dashboard")
@login_required
def dashboard():
    organization = current_user.active_organization
    if organization is None:
        return render_template("main/no_organization.html")

    org_id = organization.id

    recent_scopes = list(
        db.session.scalars(
            select(Scope)
            .where(Scope.organization_id == org_id)
            .order_by(Scope.updated_at.desc())
            .limit(8)
        )
    )

    status_counts = dict(
        db.session.execute(
            select(Scope.status, func.count(Scope.id))
            .where(Scope.organization_id == org_id)
            .group_by(Scope.status)
        ).all()
    )

    project_count = db.session.scalar(
        select(func.count(Project.id)).where(
            Project.organization_id == org_id, Project.is_archived.is_(False)
        )
    )

    return render_template(
        "main/dashboard.html",
        organization=organization,
        recent_scopes=recent_scopes,
        status_counts=status_counts,
        scope_total=sum(status_counts.values()),
        project_count=project_count or 0,
        library=library_stats(org_id),
        pdf_available=PDF_AVAILABLE,
    )


@bp.route("/healthz")
def healthz():
    """Liveness probe -- answers without touching the database."""
    return jsonify({"status": "ok", "version": __version__})


@bp.route("/readyz")
def readyz():
    """Readiness probe -- fails when the database is unreachable."""
    try:
        db.session.execute(select(1))
    except Exception as exc:
        current_app.logger.error("Readiness check failed: %s", exc)
        return jsonify({"status": "unavailable", "database": "error"}), 503
    return jsonify(
        {
            "status": "ok",
            "version": __version__,
            "database": "ok",
            "pdf": "available" if PDF_AVAILABLE else "unavailable",
        }
    )
