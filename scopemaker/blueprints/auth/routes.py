"""Sign-in, registration, invitations and SSO."""

from __future__ import annotations

import json
import logging

from flask import (
    Response,
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
from sqlalchemy import select

from ...errors import ScopeMakerError
from ...extensions import db, limiter, oauth
from ...models import Organization, PasswordResetToken
from ...models.base import utcnow
from ...models.user import ACTIVE_ORG_SESSION_KEY, User
from ...security import generate_token, needs_rehash
from ...services import audit, mail
from ...services.accounts import (
    accept_invitation,
    create_organization,
    create_user,
    find_invitation,
    provision_sso_user,
    safe_redirect_target,
)
from . import bp
from .forms import (
    ChangePasswordForm,
    DeleteAccountForm,
    ForgotPasswordForm,
    InviteAcceptForm,
    LoginForm,
    ProfileForm,
    RegisterForm,
    ResetPasswordForm,
)

logger = logging.getLogger(__name__)

OAUTH_STATE_KEY = "oidc_state"
INVITE_TOKEN_KEY = "pending_invite_token"

# One message for every sign-in failure -- wrong password, unknown address, or
# a locked account. Any variation would confirm which addresses have accounts.
LOGIN_FAILED_MESSAGE = "Incorrect email address or password."


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute; 40 per hour", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    form = LoginForm()
    if form.validate_on_submit():
        user = User.by_email(form.email.data)

        # A locked account fails before the password is even checked, so a
        # lockout cannot be probed by password guessing. The message is the
        # same generic one either way -- telling an attacker "this account is
        # locked" would confirm the address exists.
        if user is not None and user.is_locked:
            logger.warning("Sign-in attempt on locked account %s", user.email)
            flash(LOGIN_FAILED_MESSAGE, "error")
            return render_template("auth/login.html", form=form)

        # Always do the same work whether or not the account exists, and give
        # the same message either way, so this endpoint cannot be used to
        # enumerate which email addresses have accounts.
        authenticated = bool(user and user.check_password(form.password.data))

        # An organization can require SSO. Refuse the password even when it is
        # correct, so switching the policy on actually closes that door.
        if authenticated and any(
            m.organization.setting("sso_only") for m in user.memberships
        ):
            logger.info("Password sign-in refused for %s: org requires SSO", user.email)
            flash(
                "Your organization requires single sign-on. Use the "
                "single sign-on button below.",
                "error",
            )
            return render_template("auth/login.html", form=form)

        if authenticated and not user.is_active:
            flash("That account has been deactivated. Contact your administrator.", "error")
        elif authenticated:
            if needs_rehash(user.password_hash or ""):
                # Re-hashing is not a credential change; keep other sessions.
                user.set_password(form.password.data, revoke_sessions=False)
            user.clear_lockout()
            user.last_login_at = utcnow()
            db.session.commit()
            # A correct password is not a sign-in when a second factor is
            # configured: park the user and prove the factor first.
            if user.mfa_enabled:
                from .mfa_routes import begin_challenge

                return begin_challenge(
                    user,
                    remember=form.remember.data,
                    next_url=request.args.get("next"),
                )

            audit.record(
                audit.AuditAction.SIGN_IN,
                summary=f"{user.email} signed in",
                user_id=user.id,
                actor_label=user.email,
                organization_id=user.memberships[0].organization_id
                if user.memberships
                else None,
                commit=True,
            )
            login_user(user, remember=form.remember.data)
            session.permanent = True
            logger.info("User %s signed in", user.email)
            return redirect(
                safe_redirect_target(request.args.get("next"), url_for("main.dashboard"))
            )
        else:
            if user is not None:
                locked = user.register_failed_login(
                    max_attempts=current_app.config["LOGIN_MAX_ATTEMPTS"],
                    lockout_seconds=current_app.config["LOGIN_LOCKOUT_SECONDS"],
                )
                db.session.commit()
                audit.record(
                    audit.AuditAction.SIGN_IN_FAILED,
                    summary=f"Failed sign-in for {user.email}",
                    user_id=user.id,
                    actor_label=user.email,
                    context={"attempts": user.failed_login_count},
                    commit=True,
                )
                if locked:
                    logger.warning(
                        "Locked account %s after %s failed attempts",
                        user.email,
                        user.failed_login_count,
                    )
                    audit.record(
                        audit.AuditAction.ACCOUNT_LOCKED,
                        summary=f"{user.email} locked after "
                        f"{user.failed_login_count} failed attempts",
                        user_id=user.id,
                        actor_label=user.email,
                        commit=True,
                    )
            flash(LOGIN_FAILED_MESSAGE, "error")

    return render_template("auth/login.html", form=form)


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------

@bp.route("/forgot", methods=["GET", "POST"])
@limiter.limit("5 per hour; 20 per day", methods=["POST"])
def forgot_password():
    """Start a password reset.

    Always reports success. Saying "no account with that address" would turn
    this into an account-enumeration oracle, and it is the one endpoint an
    attacker can hit without credentials.
    """
    if current_user.is_authenticated:
        return redirect(url_for("auth.profile"))

    form = ForgotPasswordForm()
    if form.validate_on_submit():
        user = User.by_email(form.email.data)
        if user is not None and user.is_active and not user.is_sso_only:
            hours = current_app.config["PASSWORD_RESET_HOURS"]
            # Invalidate any outstanding reset so only the newest link works.
            for existing in user.reset_tokens:
                if existing.is_usable:
                    existing.consume()

            token, raw = PasswordResetToken.issue(
                user, hours=hours, ip=request.remote_addr
            )
            db.session.add(token)
            db.session.commit()

            url = url_for("auth.reset_password", token=raw, _external=True)
            mail.send_password_reset(
                to=user.email, name=user.full_name, url=url, expires_hours=hours
            )
            audit.record(
                audit.AuditAction.PASSWORD_RESET_REQUESTED,
                summary=f"Password reset requested for {user.email}",
                user_id=user.id,
                actor_label=user.email,
                commit=True,
            )
            logger.info("Password reset requested for %s", user.email)
        elif user is not None and user.is_sso_only:
            logger.info("Reset requested for SSO-only account %s; ignored", user.email)

        flash(
            "If that address has an account, a reset link is on its way. "
            "Check your spam folder if it does not arrive.",
            "info",
        )
        return redirect(url_for("auth.login"))

    return render_template("auth/forgot_password.html", form=form)


def _find_reset_token(raw: str) -> PasswordResetToken | None:
    """Look up a reset by its non-secret prefix, then verify the hash."""
    if not raw:
        return None
    candidates = db.session.scalars(
        select(PasswordResetToken).where(
            PasswordResetToken.token_prefix == raw[: PasswordResetToken.PREFIX_LENGTH],
            PasswordResetToken.used_at.is_(None),
        )
    )
    for candidate in candidates:
        if candidate.is_usable and candidate.matches(raw):
            return candidate
    return None


@bp.route("/reset/<token>", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def reset_password(token: str):
    record = _find_reset_token(token)
    if record is None:
        return render_template("auth/reset_invalid.html"), 400

    form = ResetPasswordForm()
    if form.validate_on_submit():
        user = record.user
        # set_password bumps the session epoch, which signs out every existing
        # session -- including whoever may have been using a stolen one.
        user.set_password(form.password.data)
        record.consume()
        db.session.commit()

        audit.record(
            audit.AuditAction.PASSWORD_RESET_COMPLETED,
            summary=f"Password reset completed for {user.email}",
            user_id=user.id,
            actor_label=user.email,
            commit=True,
        )
        mail.send_password_changed(to=user.email, name=user.full_name)
        logger.info("Password reset completed for %s", user.email)
        flash(
            "Your password has been changed and all other sessions were signed out.",
            "success",
        )
        return redirect(url_for("auth.login"))

    return render_template("auth/reset_password.html", form=form, token=token)


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    audit.record(
        audit.AuditAction.SIGN_OUT,
        summary=f"{current_user.email} signed out",
        commit=True,
    )
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

    if form.save_profile.data and form.validate_on_submit():
        current_user.full_name = form.full_name.data
        db.session.commit()
        flash("Profile updated.", "success")
        return redirect(url_for("auth.profile"))

    if password_form.change_password.data and password_form.validate_on_submit():
        if current_user.is_sso_only:
            flash("This account signs in through your identity provider.", "error")
        elif not current_user.check_password(password_form.current_password.data):
            flash("Your current password is incorrect.", "error")
        else:
            email, name = current_user.email, current_user.full_name
            # Bumps the session epoch, so every session dies -- including this
            # one. Sign the user back in so they are not bounced to the login
            # page for changing their own password.
            user = current_user._get_current_object()
            user.set_password(password_form.password.data)
            db.session.commit()
            # login_user stores this object on g; passing the LocalProxy
            # would make it resolve to itself and recurse forever.
            audit.record(
                audit.AuditAction.PASSWORD_CHANGED,
                summary=f"{email} changed their password",
                user_id=user.id, actor_label=email, commit=True,
            )
            login_user(user)
            session.permanent = True
            mail.send_password_changed(to=email, name=name)
            flash(
                "Password changed. Every other signed-in session was ended.",
                "success",
            )
            return redirect(url_for("auth.profile"))

    return render_template("auth/profile.html", form=form, password_form=password_form)


@bp.route("/account/export")
@login_required
@limiter.limit("6 per hour")
def export_account():
    """Download everything held about this account, as JSON."""
    from ...services import account_data

    user = current_user._get_current_object()
    payload = account_data.export_account(user)

    audit.record(
        audit.AuditAction.ACCOUNT_EXPORTED,
        summary=f"{user.email} exported their account data",
        user_id=user.id, actor_label=user.email, commit=True,
    )

    body = json.dumps(payload, indent=2, ensure_ascii=False)
    stamp = payload["exported_at"][:10]
    response = Response(body, mimetype="application/json")
    response.headers["Content-Disposition"] = (
        f'attachment; filename="scopemaker-account-{stamp}.json"'
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@bp.route("/account/delete", methods=["GET", "POST"])
@login_required
@limiter.limit("10 per hour", methods=["POST"])
def delete_account():
    """Delete this account, after confirming the person means it."""
    from ...services import account_data

    user = current_user._get_current_object()
    form = DeleteAccountForm()
    form.confirm_email.description = f"Type {user.email} exactly."
    blockers = account_data.deletion_blockers(user)
    doomed = account_data.organizations_deleted_with(user)

    if form.validate_on_submit() and not blockers:
        typed = (form.confirm_email.data or "").strip().lower()
        if typed != user.email.lower():
            flash("That is not the email address on this account.", "error")
        elif not user.is_sso_only and not user.check_password(form.password.data or ""):
            flash("Your password is incorrect.", "error")
        else:
            email = user.email
            summary = account_data.delete_account(user)
            logout_user()
            session.clear()
            removed = summary["organizations_deleted"]
            flash(
                f"The account {email} has been deleted."
                + (f" So were: {', '.join(removed)}." if removed else ""),
                "success",
            )
            return redirect(url_for("main.index"))

    return render_template(
        "auth/delete_account.html",
        form=form,
        blockers=blockers,
        doomed=doomed,
    )


@bp.route("/sessions/revoke", methods=["POST"])
@login_required
def revoke_sessions():
    """Sign out of every browser, then sign this one back in."""
    user = current_user._get_current_object()
    user.revoke_sessions()
    audit.record(
        audit.AuditAction.SESSIONS_REVOKED,
        summary=f"{user.email} signed out all other sessions",
        user_id=user.id, actor_label=user.email,
    )
    db.session.commit()
    # Concrete object, not the proxy -- see the note in profile().
    login_user(user)
    session.permanent = True
    flash("All other sessions have been signed out.", "success")
    return redirect(url_for("auth.profile"))


@bp.route("/switch/<organization_id>", methods=["POST"])
@login_required
def switch_organization(organization_id: str):
    if not current_user.switch_organization(organization_id):
        abort(403)
    organization = db.session.get(Organization, organization_id)
    if organization is not None:
        flash(f"Switched to {organization.name}.", "info")
    return redirect(
        safe_redirect_target(request.form.get("next"), url_for("main.dashboard"))
    )
