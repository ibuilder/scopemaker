"""Organization administration: members, invitations, settings and API tokens."""

from __future__ import annotations

from datetime import timedelta

from flask import abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from sqlalchemy import select
from wtforms import IntegerField, SelectField, StringField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, Length, NumberRange, Optional

from ...extensions import db
from ...models import ApiToken, Invitation, Membership, User
from ...models.base import utcnow
from ...models.organization import ROLE_HIERARCHY, ROLE_LABELS
from ...security import admin_required
from ...services import mail
from ..auth.forms import InviteForm
from ..helpers import current_org_id
from . import bp


class OrganizationForm(FlaskForm):
    name = StringField("Display name", validators=[DataRequired(), Length(max=200)])
    legal_name = StringField("Legal name", validators=[Optional(), Length(max=255)])
    address = TextAreaField("Address", validators=[Optional(), Length(max=500)])
    phone = StringField("Phone", validators=[Optional(), Length(max=60)])
    submit = SubmitField("Save")


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

    membership.role = role
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


@bp.route("/tokens/<token_id>/revoke", methods=["POST"])
@login_required
@admin_required
def revoke_token(token_id: str):
    token = db.session.get(ApiToken, token_id)
    if token is None or token.organization_id != current_org_id():
        abort(404)
    token.revoked_at = utcnow()
    db.session.commit()
    flash("Token revoked.", "info")
    return redirect(url_for("admin.tokens"))
