"""Sign-in, registration, invitations and SSO."""

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
from flask_login import current_user, login_required, login_user, logout_user

from ...errors import ScopeMakerError
from ...extensions import db, limiter, oauth
from ...models import Organization
from ...models.base import utcnow
from ...models.user import ACTIVE_ORG_SESSION_KEY, User
from ...security import generate_token, needs_rehash
from ...services.accounts import (
    accept_invitation,
    create_organization,
    create_user,
    find_invitation,
    provision_sso_user,
    safe_redirect_target,
)
from . import bp
from .forms import ChangePasswordForm, InviteAcceptForm, LoginForm, ProfileForm, RegisterForm

logger = logging.getLogger(__name__)

OAUTH_STATE_KEY = "oidc_state"
INVITE_TOKEN_KEY = "pending_invite_token"


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute; 40 per hour", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    form = LoginForm()
    if form.validate_on_submit():
        user = User.by_email(form.email.data)
        # Always run the same work whether or not the account exists, and give
        # the same message either way, so this endpoint cannot be used to
        # enumerate which email addresses have accounts.
        authenticated = bool(user and user.check_password(form.password.data))

        if authenticated and not user.is_active:
            flash("That account has been deactivated. Contact your administrator.", "error")
        elif authenticated:
            if needs_rehash(user.password_hash or ""):
                user.set_password(form.password.data)
            user.last_login_at = utcnow()
            db.session.commit()
            login_user(user, remember=form.remember.data)
            session.permanent = True
            logger.info("User %s signed in", user.email)
            return redirect(
                safe_redirect_target(request.args.get("next"), url_for("main.dashboard"))
            )
        else:
            flash("Incorrect email address or password.", "error")

    return render_template("auth/login.html", form=form)


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    session.pop(ACTIVE_ORG_SESSION_KEY, None)
    flash("You have been signed out.", "info")
    return redirect(url_for("main.index"))


@bp.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per minute; 20 per hour", methods=["POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    mode = current_app.config["REGISTRATION_MODE"]
    invite_token = request.args.get("invite") or session.get(INVITE_TOKEN_KEY)
    invitation = find_invitation(invite_token) if invite_token else None

    if mode == "closed" and invitation is None:
        return render_template("auth/registration_closed.html"), 403
    if mode == "invite" and (invitation is None or not invitation.is_usable):
        return render_template("auth/registration_closed.html", invite_required=True), 403

    form = RegisterForm()
    if invitation is not None and invitation.is_usable:
        form.email.data = form.email.data or invitation.email
        # The organization comes from the invite, so hide the field entirely
        # rather than letting the user type one that will be ignored.
        del form.organization_name

    if form.validate_on_submit():
        try:
            user = create_user(
                email=form.email.data,
                full_name=form.full_name.data,
                password=form.password.data,
            )
            if invitation is not None and invitation.is_usable:
                accept_invitation(invitation, user)
                session.pop(INVITE_TOKEN_KEY, None)
            else:
                name = (getattr(form, "organization_name", None)
                        and form.organization_name.data) or form.full_name.data
                create_organization(name, owner=user, role="admin")
            db.session.commit()
        except ScopeMakerError as exc:
            db.session.rollback()
            flash(exc.message, "error")
        else:
            login_user(user)
            session.permanent = True
            flash("Welcome to ScopeMaker.", "success")
            return redirect(url_for("main.dashboard"))

    return render_template("auth/register.html", form=form, invitation=invitation)


@bp.route("/invite/<token>", methods=["GET", "POST"])
def accept_invite(token: str):
    invitation = find_invitation(token)
    if invitation is None or not invitation.is_usable:
        return render_template("auth/invite_invalid.html"), 404

    # Already signed in: just add the membership.
    if current_user.is_authenticated:
        accept_invitation(invitation, current_user)
        db.session.commit()
        current_user.switch_organization(invitation.organization_id)
        flash(f"You have joined {invitation.organization.name}.", "success")
        return redirect(url_for("main.dashboard"))

    existing = User.by_email(invitation.email)
    if existing is not None:
        session[INVITE_TOKEN_KEY] = token
        flash("Sign in to accept this invitation.", "info")
        return redirect(url_for("auth.login", next=url_for("auth.accept_invite", token=token)))

    form = InviteAcceptForm()
    if form.validate_on_submit():
        try:
            user = create_user(
                email=invitation.email,
                full_name=form.full_name.data,
                password=form.password.data,
            )
            accept_invitation(invitation, user)
            db.session.commit()
        except ScopeMakerError as exc:
            db.session.rollback()
            flash(exc.message, "error")
        else:
            login_user(user)
            session.permanent = True
            flash(f"Welcome to {invitation.organization.name}.", "success")
            return redirect(url_for("main.dashboard"))

    return render_template("auth/accept_invite.html", form=form, invitation=invitation)


# ---------------------------------------------------------------------------
# OIDC single sign-on
# ---------------------------------------------------------------------------

@bp.route("/sso")
def sso_login():
    if not current_app.config["OIDC_ENABLED"]:
        abort(404)
    client = oauth.create_client(current_app.config["OIDC_NAME"])
    if client is None:  # pragma: no cover - misconfiguration
        abort(500)

    # Authlib stores and checks its own state; we keep a copy so the callback
    # can also verify it rather than trusting whatever comes back.
    state = generate_token(24)
    session[OAUTH_STATE_KEY] = state
    redirect_uri = url_for("auth.sso_callback", _external=True)
    return client.authorize_redirect(redirect_uri, state=state)


@bp.route("/sso/callback")
def sso_callback():
    if not current_app.config["OIDC_ENABLED"]:
        abort(404)

    expected = session.pop(OAUTH_STATE_KEY, None)
    received = request.args.get("state")
    if not expected or expected != received:
        logger.warning("OIDC callback rejected: state mismatch")
        flash("Sign-in could not be completed. Please try again.", "error")
        return redirect(url_for("auth.login"))

    client = oauth.create_client(current_app.config["OIDC_NAME"])
    try:
        token = client.authorize_access_token()
        claims = token.get("userinfo") or client.userinfo(token=token)
        user = provision_sso_user(dict(claims))
        db.session.commit()
    except ScopeMakerError as exc:
        db.session.rollback()
        flash(exc.message, "error")
        return redirect(url_for("auth.login"))
    except Exception:
        db.session.rollback()
        logger.exception("OIDC sign-in failed")
        flash("Single sign-on failed. Contact your administrator.", "error")
        return redirect(url_for("auth.login"))

    if not user.is_active:
        flash("That account has been deactivated.", "error")
        return redirect(url_for("auth.login"))

    user.last_login_at = utcnow()
    db.session.commit()
    login_user(user)
    session.permanent = True
    return redirect(url_for("main.dashboard"))


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    form = ProfileForm(obj=current_user)
    password_form = ChangePasswordForm()

    if form.submit.data and form.validate_on_submit():
        current_user.full_name = form.full_name.data
        db.session.commit()
        flash("Profile updated.", "success")
        return redirect(url_for("auth.profile"))

    if password_form.submit.data and password_form.validate_on_submit():
        if current_user.is_sso_only:
            flash("This account signs in through your identity provider.", "error")
        elif not current_user.check_password(password_form.current_password.data):
            flash("Your current password is incorrect.", "error")
        else:
            current_user.set_password(password_form.password.data)
            db.session.commit()
            flash("Password changed.", "success")
            return redirect(url_for("auth.profile"))

    return render_template("auth/profile.html", form=form, password_form=password_form)


@bp.route("/switch/<organization_id>", methods=["POST"])
@login_required
def switch_organization(organization_id: str):
    if not current_user.switch_organization(organization_id):
        abort(403)
    organization = db.session.get(Organization, organization_id)
    flash(f"Switched to {organization.name}.", "info")
    return redirect(
        safe_redirect_target(request.form.get("next"), url_for("main.dashboard"))
    )
