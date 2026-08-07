"""Procore connection, sync and push-back."""

from __future__ import annotations

import logging

from flask import (
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required

from ...errors import IntegrationError
from ...extensions import db, limiter
from ...models import ProcoreConnection
from ...security import admin_required, constant_time_equals, editor_required, generate_token
from ...services import audit, procore_sync
from ...services.procore_client import (
    ProcoreClient,
    authorize_url,
    client_for,
    get_connection,
)
from ...services.renderers import FORMATS, render_docx, render_pdf
from ..helpers import current_org_id, get_project_or_404, get_scope_or_404
from . import bp

logger = logging.getLogger(__name__)

STATE_KEY = "procore_oauth_state"


def _require_enabled() -> None:
    if not current_app.config["PROCORE_ENABLED"]:
        abort(404)


@bp.route("/")
@login_required
def index():
    _require_enabled()
    connection = get_connection(current_org_id())
    identity = None
    companies: list = []

    if connection is not None and connection.is_connected:
        try:
            client = ProcoreClient(connection)
            identity = client.me()
            companies = client.companies()
        except IntegrationError as exc:
            flash(exc.message, "error")
            connection.last_error = exc.message
            db.session.commit()

    return render_template(
        "procore/index.html",
        connection=connection,
        identity=identity,
        companies=companies,
        redirect_uri=current_app.config["PROCORE_REDIRECT_URI"],
    )


@bp.route("/connect", methods=["POST"])
@login_required
@admin_required
def connect():
    """Begin the authorization-code flow."""
    _require_enabled()
    state = generate_token(24)
    session[STATE_KEY] = state
    redirect_uri = current_app.config["PROCORE_REDIRECT_URI"]
    return redirect(authorize_url(redirect_uri, state))


@bp.route("/callback")
@login_required
@limiter.limit("20 per hour")
def callback():
    """Procore redirects here with an authorization code."""
    _require_enabled()

    expected = session.pop(STATE_KEY, None)
    received = request.args.get("state", "")
    # Without this check an attacker could have the victim's browser complete
    # a flow that binds the attacker's Procore account to the victim's org.
    if not expected or not constant_time_equals(expected, received):
        logger.warning("Procore callback rejected: OAuth state mismatch")
        flash("The Procore connection could not be verified. Please try again.", "error")
        return redirect(url_for("procore.index"))

    error = request.args.get("error")
    if error:
        flash(f"Procore returned an error: {error}", "error")
        return redirect(url_for("procore.index"))

    code = request.args.get("code")
    if not code:
        flash("Procore did not return an authorization code.", "error")
        return redirect(url_for("procore.index"))

    org_id = current_org_id()
    connection = get_connection(org_id) or ProcoreConnection(organization_id=org_id)
    connection.grant_type = "authorization_code"
    connection.is_active = True
    connection.connected_by_id = current_user.id

    client = ProcoreClient(connection)
    try:
        payload = client.exchange_code(code, current_app.config["PROCORE_REDIRECT_URI"])
        connection.apply_token_response(payload)
        db.session.add(connection)
        db.session.flush()

        identity = client.me()
        connection.procore_user_id = str(identity.get("id", "")) or None
        connection.procore_user_name = identity.get("name")
        connection.procore_user_email = identity.get("login") or identity.get("email")

        companies = client.companies()
        if companies:
            connection.company_id = str(companies[0].get("id"))
            connection.company_name = companies[0].get("name")
        db.session.commit()
    except IntegrationError as exc:
        db.session.rollback()
        flash(exc.message, "error")
        return redirect(url_for("procore.index"))

    audit.record(
        audit.AuditAction.INTEGRATION_CONNECTED,
        summary=f"Procore connected as {connection.procore_user_name or 'unknown'}",
        target_type="integration", target_label="procore",
        context={"grant_type": connection.grant_type,
                 "company_id": connection.company_id},
        commit=True,
    )
    flash(
        f"Connected to Procore as {connection.procore_user_name or 'your account'}.",
        "success",
    )
    return redirect(url_for("procore.index"))


@bp.route("/connect-service-account", methods=["POST"])
@login_required
@admin_required
def connect_service_account():
    """Connect using a Developer Managed Service Account (unattended sync)."""
    _require_enabled()
    company_id = (
        request.form.get("company_id")
        or current_app.config.get("PROCORE_DMSA_COMPANY_ID")
        or ""
    ).strip()
    if not company_id:
        flash(
            "A Procore company id is required for a service account connection.",
            "error",
        )
        return redirect(url_for("procore.index"))

    org_id = current_org_id()
    connection = get_connection(org_id) or ProcoreConnection(organization_id=org_id)
    connection.grant_type = "client_credentials"
    connection.company_id = company_id
    connection.is_active = True
    connection.connected_by_id = current_user.id

    client = ProcoreClient(connection)
    try:
        connection.apply_token_response(client.refresh())
        db.session.add(connection)
        db.session.commit()
        identity = client.me()
        connection.procore_user_name = identity.get("name") or "Service account"
        db.session.commit()
    except IntegrationError as exc:
        db.session.rollback()
        flash(exc.message, "error")
        return redirect(url_for("procore.index"))

    flash("Service account connected.", "success")
    return redirect(url_for("procore.index"))


@bp.route("/company", methods=["POST"])
@login_required
@admin_required
def set_company():
    _require_enabled()
    connection = get_connection(current_org_id())
    if connection is None:
        abort(404)
    connection.company_id = request.form.get("company_id") or None
    connection.company_name = request.form.get("company_name") or None
    db.session.commit()
    flash("Active Procore company updated.", "success")
    return redirect(url_for("procore.index"))


@bp.route("/disconnect", methods=["POST"])
@login_required
@admin_required
def disconnect():
    _require_enabled()
    connection = get_connection(current_org_id())
    if connection is not None:
        connection.disconnect()
        audit.record(
            audit.AuditAction.INTEGRATION_DISCONNECTED,
            summary="Procore disconnected",
            target_type="integration", target_label="procore",
        )
        db.session.commit()
    flash("Disconnected from Procore. Imported projects were kept.", "info")
    return redirect(url_for("procore.index"))


@bp.route("/sync/projects", methods=["POST"])
@login_required
@editor_required
@limiter.limit("10 per hour")
def sync_projects():
    _require_enabled()
    org_id = current_org_id()
    try:
        client = client_for(org_id)
        if not client.connection.company_id:
            flash("Choose a Procore company before syncing.", "error")
            return redirect(url_for("procore.index"))
        result = procore_sync.sync_projects(
            client, org_id, client.connection.company_id
        )
        procore_sync.touch_connection(client.connection)
    except IntegrationError as exc:
        flash(exc.message, "error")
        return redirect(url_for("procore.index"))

    audit.record(
        audit.AuditAction.INTEGRATION_SYNCED,
        summary=f"Procore project sync: {result.summary()}",
        target_type="integration", target_label="procore", commit=True,
    )
    flash(f"Sync complete: {result.summary()}.", "success")
    return redirect(url_for("projects.index"))


@bp.route("/sync/project/<project_id>/packages", methods=["POST"])
@login_required
@editor_required
def sync_packages(project_id: str):
    _require_enabled()
    project = get_project_or_404(project_id)
    try:
        client = client_for(current_org_id())
        result = procore_sync.sync_bid_packages(client, project)
    except IntegrationError as exc:
        flash(exc.message, "error")
        return redirect(url_for("projects.detail", project_id=project.id))

    for warning in result.warnings:
        flash(warning, "error")
    if not result.warnings:
        flash(f"Bid packages synced: {result.summary()}.", "success")
    return redirect(url_for("projects.detail", project_id=project.id))


@bp.route("/scopes/<scope_id>/push", methods=["GET", "POST"])
@login_required
@editor_required
def push_scope(scope_id: str):
    """Attach a generated exhibit to a Procore commitment."""
    _require_enabled()
    scope = get_scope_or_404(scope_id)

    if scope.project is None or not scope.project.procore_project_id:
        flash(
            "This scope's project is not linked to Procore. Sync projects first.",
            "error",
        )
        return redirect(url_for("scopes.edit", scope_id=scope.id))

    try:
        client = client_for(current_org_id())
        commitments = client.commitments(scope.project.procore_project_id)
    except IntegrationError as exc:
        flash(exc.message, "error")
        return redirect(url_for("scopes.edit", scope_id=scope.id))

    if request.method == "POST":
        commitment_id = request.form.get("commitment_id")
        fmt = request.form.get("format", "pdf")
        if not commitment_id:
            flash("Choose a commitment to attach the exhibit to.", "error")
            return redirect(url_for("procore.push_scope", scope_id=scope.id))

        organization = current_user.active_organization
        try:
            if fmt == "docx":
                content = render_docx(scope, organization=organization)
            else:
                fmt = "pdf"
                content = render_pdf(scope, organization=organization)

            export_format = FORMATS[fmt]
            filename = f"{scope.exhibit_label}-{scope.trade_name or scope.title}".strip()
            filename = f"{filename.replace(' ', '-')}.{export_format.extension}"

            client.upload_commitment_attachment(
                scope.project.procore_project_id,
                commitment_id,
                filename,
                content,
                export_format.mimetype,
            )
            scope.procore_commitment_id = str(commitment_id)
            scope.procore_synced_at = procore_sync.utcnow()
            db.session.commit()
        except IntegrationError as exc:
            flash(exc.message, "error")
            return redirect(url_for("procore.push_scope", scope_id=scope.id))

        flash(f"Exhibit attached to the Procore commitment as {filename}.", "success")
        return redirect(url_for("scopes.edit", scope_id=scope.id))

    return render_template(
        "procore/push.html", scope=scope, commitments=commitments
    )
