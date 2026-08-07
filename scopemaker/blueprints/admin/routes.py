"""Organization administration: members, invitations, settings and API tokens."""

from __future__ import annotations

from datetime import timedelta

from flask import (
    Response,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from sqlalchemy import select
from wtforms import (
    BooleanField,
    IntegerField,
    SelectField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import DataRequired, Length, NumberRange, Optional

from ...extensions import db
from ...models import ACTION_LABELS, ApiToken, Invitation, Membership, User
from ...models.base import utcnow
from ...models.organization import ROLE_HIERARCHY, ROLE_LABELS
from ...security import admin_required
from ...services import audit, mail
from ..auth.forms import InviteForm
from ..helpers import current_org_id
from . import bp


class OrganizationForm(FlaskForm):
    name = StringField("Display name", validators=[DataRequired(), Length(max=200)])
    legal_name = StringField("Legal name", validators=[Optional(), Length(max=255)])
    address = TextAreaField("Address", validators=[Optional(), Length(max=500)])
    phone = StringField("Phone", validators=[Optional(), Length(max=60)])
    submit = SubmitField("Save")


class SecurityPolicyForm(FlaskForm):
    """Organization-wide access policy.

    Both settings are enforced by a before_request hook rather than only at
    sign-in, so turning one on takes effect for users who are already signed in
    instead of waiting for their next login.
    """

    require_mfa = BooleanField("Require two-factor authentication for everyone")
    sso_only = BooleanField("Require single sign-on (disable password sign-in)")
    submit = SubmitField("Save policy")


class ApiTokenForm(FlaskForm):
    name = StringField("Label", validators=[DataRequired(), Length(max=120)])
    scopes = SelectField(
        "Access",
        choices=[("read", "Read only"), ("read write", "Read and write")],
        default="read",
    )
    expires_days = IntegerField(
        "Expires in (days)", validators=[Optional(), NumberRange(min=1, max=3650)],
        default=365,
    )
    submit = SubmitField("Create token")


@bp.route("/")
@login_required
@admin_required
def index():
    organization = current_user.active_organization
    memberships = list(
        db.session.scalars(
            select(Membership).where(Membership.organization_id == organization.id)
        )
    )
    pending = list(
        db.session.scalars(
            select(Invitation)
            .where(
                Invitation.organization_id == organization.id,
                Invitation.accepted_at.is_(None),
            )
            .order_by(Invitation.created_at.desc())
        )
    )
    return render_template(
        "admin/index.html",
        organization=organization,
        memberships=memberships,
        pending=[i for i in pending if not i.is_expired],
        invite_form=InviteForm(),
        roles=ROLE_HIERARCHY,
        role_labels=ROLE_LABELS,
    )


@bp.route("/settings", methods=["GET", "POST"])
@login_required
@admin_required
def settings():
    organization = current_user.active_organization
    form = OrganizationForm(obj=organization)
    if form.validate_on_submit():
        form.populate_obj(organization)
        audit.record(
            audit.AuditAction.SETTINGS_CHANGED,
            summary="Organization settings updated",
            target_type="organization", target_id=organization.id,
            target_label=organization.name,
        )
        db.session.commit()
        flash("Organization settings saved.", "success")
        return redirect(url_for("admin.settings"))
    return render_template("admin/settings.html", form=form, organization=organization)


@bp.route("/invite", methods=["POST"])
@login_required
@admin_required
def invite():
    organization = current_user.active_organization
    form = InviteForm()
    if not form.validate_on_submit():
        flash("Enter a valid email address.", "error")
        return redirect(url_for("admin.index"))

    email = form.email.data.strip().lower()
    existing_user = User.by_email(email)
    if existing_user and existing_user.membership_for(organization.id):
        flash(f"{email} is already a member.", "info")
        return redirect(url_for("admin.index"))

    invitation = Invitation(
        organization_id=organization.id,
        email=email,
        role=form.role.data if form.role.data in ROLE_HIERARCHY else "editor",
        expires_at=Invitation.default_expiry(),
        invited_by_id=current_user.id,
    )
    db.session.add(invitation)
    audit.record(
        audit.AuditAction.MEMBER_INVITED,
        summary=f"{email} invited as {invitation.role}",
        target_type="invitation", target_label=email,
        context={"role": invitation.role},
    )
    db.session.commit()

    link = url_for("auth.accept_invite", token=invitation.token, _external=True)
    delivered = mail.send_invitation(
        to=email,
        organization=organization.name,
        inviter=current_user.full_name or current_user.email,
        url=link,
    )
    if delivered and current_app.config.get("MAIL_SERVER"):
        flash(f"Invitation sent to {email}.", "success")
    else:
        # Console or failed delivery: surface the link so the admin can still
        # share it rather than leaving them to guess whether it worked.
        flash(f"Invitation created for {email}. Share this link: {link}", "success")
    return redirect(url_for("admin.index"))


@bp.route("/invite/<invitation_id>/revoke", methods=["POST"])
@login_required
@admin_required
def revoke_invite(invitation_id: str):
    invitation = db.session.get(Invitation, invitation_id)
    if invitation is None or invitation.organization_id != current_org_id():
        abort(404)
    audit.record(
        audit.AuditAction.INVITE_REVOKED,
        summary=f"Invitation for {invitation.email} revoked",
        target_type="invitation", target_label=invitation.email,
    )
    db.session.delete(invitation)
    db.session.commit()
    flash("Invitation revoked.", "info")
    return redirect(url_for("admin.index"))


@bp.route("/members/<membership_id>/role", methods=["POST"])
@login_required
@admin_required
def change_role(membership_id: str):
    membership = db.session.get(Membership, membership_id)
    if membership is None or membership.organization_id != current_org_id():
        abort(404)

    role = request.form.get("role", "")
    if role not in ROLE_HIERARCHY:
        flash("Unknown role.", "error")
        return redirect(url_for("admin.index"))

    demoting_last_admin = (
        membership.role == "admin"
        and role != "admin"
        and _admin_count(membership.organization_id) <= 1
    )
    if demoting_last_admin:
        flash(
            "This is the only administrator. Promote someone else before "
            "changing this role.",
            "error",
        )
        return redirect(url_for("admin.index"))

    previous = membership.role
    membership.role = role
    audit.record(
        audit.AuditAction.ROLE_CHANGED,
        summary=f"{membership.user.email} changed from {previous} to {role}",
        target_type="user", target_id=membership.user_id,
        target_label=membership.user.email,
        context={"from": previous, "to": role},
    )
    db.session.commit()
    flash(f"Role updated to {role}.", "success")
    return redirect(url_for("admin.index"))


@bp.route("/members/<membership_id>/remove", methods=["POST"])
@login_required
@admin_required
def remove_member(membership_id: str):
    membership = db.session.get(Membership, membership_id)
    if membership is None or membership.organization_id != current_org_id():
        abort(404)

    if membership.role == "admin" and _admin_count(membership.organization_id) <= 1:
        flash("You cannot remove the only administrator.", "error")
        return redirect(url_for("admin.index"))

    audit.record(
        audit.AuditAction.MEMBER_REMOVED,
        summary=f"{membership.user.email} removed from the organization",
        target_type="user", target_id=membership.user_id,
        target_label=membership.user.email,
        context={"role": membership.role},
    )
    db.session.delete(membership)
    db.session.commit()
    flash("Member removed from this organization.", "info")
    return redirect(url_for("admin.index"))


def _admin_count(organization_id: str) -> int:
    return len(
        list(
            db.session.scalars(
                select(Membership).where(
                    Membership.organization_id == organization_id,
                    Membership.role == "admin",
                )
            )
        )
    )


# ---------------------------------------------------------------------------
# API tokens
# ---------------------------------------------------------------------------

@bp.route("/tokens", methods=["GET", "POST"])
@login_required
@admin_required
def tokens():
    org_id = current_org_id()
    form = ApiTokenForm()
    issued_token = None

    if form.validate_on_submit():
        expires_at = None
        if form.expires_days.data:
            expires_at = utcnow() + timedelta(days=int(form.expires_days.data))
        token, issued_token = ApiToken.issue(
            user=current_user,
            organization_id=org_id,
            name=form.name.data,
            scopes=form.scopes.data,
            expires_at=expires_at,
        )
        db.session.add(token)
        audit.record(
            audit.AuditAction.TOKEN_ISSUED,
            summary=f"API token '{token.name}' issued with scopes: {token.scopes}",
            target_type="api_token", target_label=token.name,
            context={"scopes": token.scopes, "prefix": token.token_prefix},
        )
        db.session.commit()
        flash("Token created. Copy it now -- it will not be shown again.", "success")

    existing = list(
        db.session.scalars(
            select(ApiToken)
            .where(ApiToken.organization_id == org_id, ApiToken.revoked_at.is_(None))
            .order_by(ApiToken.created_at.desc())
        )
    )
    return render_template(
        "admin/tokens.html", form=form, tokens=existing, issued_token=issued_token
    )


@bp.route("/security", methods=["GET", "POST"])
@login_required
@admin_required
def security_policy():
    organization = current_user.active_organization
    settings = dict(organization.settings or {})
    form = SecurityPolicyForm(
        data={
            "require_mfa": bool(settings.get("require_mfa")),
            "sso_only": bool(settings.get("sso_only")),
        }
    )

    if form.validate_on_submit():
        if form.sso_only.data and not current_app.config.get("OIDC_ENABLED"):
            flash(
                "Single sign-on is not configured on this deployment. Enabling "
                "this would lock everyone out.",
                "error",
            )
            return redirect(url_for("admin.security_policy"))

        before = {k: settings.get(k) for k in ("require_mfa", "sso_only")}
        settings["require_mfa"] = bool(form.require_mfa.data)
        settings["sso_only"] = bool(form.sso_only.data)
        organization.settings = settings

        audit.record(
            audit.AuditAction.SETTINGS_CHANGED,
            summary="Security policy updated",
            target_type="organization", target_id=organization.id,
            target_label=organization.name,
            context={"before": before, "after": {
                "require_mfa": settings["require_mfa"],
                "sso_only": settings["sso_only"],
            }},
        )
        db.session.commit()
        flash("Security policy saved.", "success")
        return redirect(url_for("admin.security_policy"))

    members = list(
        db.session.scalars(
            select(Membership).where(Membership.organization_id == organization.id)
        )
    )
    return render_template(
        "admin/security.html",
        form=form,
        organization=organization,
        members=members,
        without_mfa=[m for m in members if not m.user.mfa_enabled and not m.user.is_sso_only],
        oidc_enabled=current_app.config.get("OIDC_ENABLED"),
    )


@bp.route("/audit")
@login_required
@admin_required
def audit_log():
    """Who did what, and when."""
    org_id = current_org_id()
    action = request.args.get("action") or ""
    security_only = request.args.get("security") == "1"
    page = max(int(request.args.get("page", 1) or 1), 1)
    per_page = 100

    events = audit.query(
        org_id,
        action=action or None,
        security_only=security_only,
        limit=per_page + 1,
        offset=(page - 1) * per_page,
    )
    has_next = len(events) > per_page
    return render_template(
        "admin/audit.html",
        events=events[:per_page],
        action=action,
        security_only=security_only,
        page=page,
        has_next=has_next,
        action_labels=ACTION_LABELS,
    )


@bp.route("/audit.csv")
@login_required
@admin_required
def audit_csv():
    org_id = current_org_id()
    events = audit.query(
        org_id,
        action=request.args.get("action") or None,
        security_only=request.args.get("security") == "1",
        limit=10_000,
    )
    audit.record(
        audit.AuditAction.SETTINGS_CHANGED,
        summary=f"Audit log exported ({len(events)} entries)",
        target_type="audit", target_label="export",
        commit=True,
    )
    return Response(
        audit.to_csv(events),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="audit-log.csv"'},
    )


@bp.route("/tokens/<token_id>/revoke", methods=["POST"])
@login_required
@admin_required
def revoke_token(token_id: str):
    token = db.session.get(ApiToken, token_id)
    if token is None or token.organization_id != current_org_id():
        abort(404)
    token.revoked_at = utcnow()
    audit.record(
        audit.AuditAction.TOKEN_REVOKED,
        summary=f"API token '{token.name}' revoked",
        target_type="api_token", target_id=token.id, target_label=token.name,
    )
    db.session.commit()
    flash("Token revoked.", "info")
    return redirect(url_for("admin.tokens"))
