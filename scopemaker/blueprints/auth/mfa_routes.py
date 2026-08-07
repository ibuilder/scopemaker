"""Two-factor enrolment and the challenge step of signing in.

The login flow becomes two stages when a user has MFA. Stage one verifies the
password and, instead of calling ``login_user``, parks the user id in the
session under ``PENDING_MFA_KEY``. Nothing is authenticated until stage two
succeeds, so a correct password alone grants nothing.

The pending marker carries a timestamp and the session epoch it was issued
against: an abandoned challenge expires, and a password change mid-challenge
invalidates it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from flask import current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user
from flask_wtf import FlaskForm
from wtforms import BooleanField, PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired, Length

from ...extensions import db, limiter
from ...models.base import utcnow
from ...models.user import User
from ...services import audit, mfa
from ...services.accounts import safe_redirect_target
from . import bp

logger = logging.getLogger(__name__)

PENDING_MFA_KEY = "pending_mfa"
#: An unfinished challenge is worthless after this long.
CHALLENGE_TTL = timedelta(minutes=10)
#: Held only between showing the QR and confirming the first code.
ENROL_SECRET_KEY = "mfa_enrol_secret"


class MfaChallengeForm(FlaskForm):
    code = StringField(
        "Authentication code", validators=[DataRequired(), Length(min=6, max=14)]
    )
    remember = BooleanField("Keep me signed in")
    submit = SubmitField("Verify")


class MfaEnableForm(FlaskForm):
    code = StringField(
        "Enter the six-digit code", validators=[DataRequired(), Length(min=6, max=6)]
    )
    submit = SubmitField("Turn on two-factor")


class MfaDisableForm(FlaskForm):
    password = PasswordField("Your password", validators=[DataRequired()])
    submit = SubmitField("Turn off two-factor")


# ---------------------------------------------------------------------------
# Challenge (stage two of signing in)
# ---------------------------------------------------------------------------

def begin_challenge(user: User, *, remember: bool = False, next_url: str | None = None):
    """Park a password-verified user and send them to the code prompt."""
    session[PENDING_MFA_KEY] = {
        "user_id": user.id,
        "epoch": user.session_epoch or 1,
        "at": utcnow().isoformat(),
        "remember": bool(remember),
        "next": next_url or "",
    }
    return redirect(url_for("auth.mfa_challenge"))


def _pending_user() -> User | None:
    """The user mid-challenge, or None if there isn't a valid one."""
    payload = session.get(PENDING_MFA_KEY)
    if not isinstance(payload, dict):
        return None

    try:
        issued = datetime.fromisoformat(payload["at"])
    except (KeyError, ValueError):
        session.pop(PENDING_MFA_KEY, None)
        return None
    if issued.tzinfo is None:
        issued = issued.replace(tzinfo=UTC)
    if datetime.now(UTC) - issued > CHALLENGE_TTL:
        session.pop(PENDING_MFA_KEY, None)
        return None

    user = db.session.get(User, payload.get("user_id", ""))
    if user is None or not user.is_active or not user.mfa_enabled:
        session.pop(PENDING_MFA_KEY, None)
        return None
    # A password change during the challenge invalidates it.
    if (user.session_epoch or 1) != payload.get("epoch"):
        session.pop(PENDING_MFA_KEY, None)
        return None
    return user


@bp.route("/mfa", methods=["GET", "POST"])
@limiter.limit("12 per 10 minutes; 40 per hour", methods=["POST"])
def mfa_challenge():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    user = _pending_user()
    if user is None:
        flash("Your sign-in attempt expired. Please start again.", "info")
        return redirect(url_for("auth.login"))

    payload = session.get(PENDING_MFA_KEY, {})
    form = MfaChallengeForm()

    if form.validate_on_submit():
        submitted = form.code.data or ""
        used_recovery = False

        if mfa.verify_code(user.mfa_secret or "", submitted):
            accepted = True
        else:
            accepted, remaining = mfa.consume_recovery_code(
                list(user.mfa_recovery_hashes or []), submitted
            )
            if accepted:
                used_recovery = True
                user.mfa_recovery_hashes = remaining

        if not accepted:
            # Counted against the same lockout as password failures, so the
            # second factor cannot be brute-forced independently.
            locked = user.register_failed_login(
                max_attempts=current_app.config["LOGIN_MAX_ATTEMPTS"],
                lockout_seconds=current_app.config["LOGIN_LOCKOUT_SECONDS"],
            )
            audit.record(
                audit.AuditAction.SIGN_IN_FAILED,
                summary=f"Failed two-factor code for {user.email}",
                user_id=user.id, actor_label=user.email,
                context={"stage": "mfa", "attempts": user.failed_login_count},
            )
            db.session.commit()
            if locked:
                session.pop(PENDING_MFA_KEY, None)
                flash("Too many attempts. Try again later.", "error")
                return redirect(url_for("auth.login"))
            flash("That code was not correct.", "error")
            return render_template("auth/mfa_challenge.html", form=form)

        user.clear_lockout()
        user.last_login_at = utcnow()
        if used_recovery:
            audit.record(
                audit.AuditAction.MFA_RECOVERY_USED,
                summary=f"{user.email} signed in with a recovery code "
                f"({user.recovery_codes_remaining} left)",
                user_id=user.id, actor_label=user.email,
                context={"remaining": user.recovery_codes_remaining},
            )
        audit.record(
            audit.AuditAction.SIGN_IN,
            summary=f"{user.email} signed in with two-factor",
            user_id=user.id, actor_label=user.email,
            organization_id=user.memberships[0].organization_id
            if user.memberships else None,
        )
        db.session.commit()

        session.pop(PENDING_MFA_KEY, None)
        login_user(user, remember=payload.get("remember", False))
        session.permanent = True

        if used_recovery:
            flash(
                f"Signed in with a recovery code. You have "
                f"{user.recovery_codes_remaining} left — generate new ones soon.",
                "warning",
            )
        return redirect(
            safe_redirect_target(payload.get("next"), url_for("main.dashboard"))
        )

    return render_template("auth/mfa_challenge.html", form=form)


@bp.route("/mfa/cancel", methods=["POST"])
def mfa_cancel():
    session.pop(PENDING_MFA_KEY, None)
    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------------------
# Enrolment
# ---------------------------------------------------------------------------

@bp.route("/mfa/setup", methods=["GET", "POST"])
@login_required
def mfa_setup():
    user = current_user._get_current_object()
    if user.mfa_enabled:
        return redirect(url_for("auth.profile"))
    if user.is_sso_only:
        flash("Two-factor is managed by your identity provider.", "info")
        return redirect(url_for("auth.profile"))

    # Held in the session, not the database: an abandoned enrolment leaves no
    # half-configured secret behind on the account.
    secret = session.get(ENROL_SECRET_KEY)
    if not secret:
        secret = mfa.new_secret()
        session[ENROL_SECRET_KEY] = secret

    form = MfaEnableForm()
    if form.validate_on_submit():
        if not mfa.verify_code(secret, form.code.data or ""):
            flash("That code was not correct. Check your device's clock.", "error")
        else:
            codes = mfa.generate_recovery_codes()
            user.mfa_secret = secret
            user.mfa_enabled = True
            user.mfa_confirmed_at = utcnow()
            user.mfa_recovery_hashes = mfa.hash_recovery_codes(codes)
            audit.record(
                audit.AuditAction.MFA_ENABLED,
                summary=f"{user.email} enabled two-factor",
                user_id=user.id, actor_label=user.email,
            )
            db.session.commit()
            session.pop(ENROL_SECRET_KEY, None)
            # Shown exactly once.
            session["mfa_recovery_codes"] = codes
            return redirect(url_for("auth.mfa_recovery_codes"))

    return render_template(
        "auth/mfa_setup.html",
        form=form,
        secret=secret,
        qr_svg=mfa.qr_svg(secret, email=user.email),
    )


@bp.route("/mfa/recovery-codes")
@login_required
def mfa_recovery_codes():
    codes = session.pop("mfa_recovery_codes", None)
    if not codes:
        flash(
            "Recovery codes are shown only once. Generate a new set if you no "
            "longer have them.",
            "info",
        )
        return redirect(url_for("auth.profile"))
    return render_template("auth/mfa_recovery_codes.html", codes=codes)


@bp.route("/mfa/recovery-codes/regenerate", methods=["POST"])
@login_required
def mfa_regenerate_recovery_codes():
    user = current_user._get_current_object()
    if not user.mfa_enabled:
        return redirect(url_for("auth.profile"))

    codes = mfa.generate_recovery_codes()
    user.mfa_recovery_hashes = mfa.hash_recovery_codes(codes)
    audit.record(
        audit.AuditAction.MFA_ENABLED,
        summary=f"{user.email} regenerated recovery codes",
        user_id=user.id, actor_label=user.email,
    )
    db.session.commit()
    session["mfa_recovery_codes"] = codes
    return redirect(url_for("auth.mfa_recovery_codes"))


@bp.route("/mfa/disable", methods=["GET", "POST"])
@login_required
def mfa_disable():
    user = current_user._get_current_object()
    if not user.mfa_enabled:
        return redirect(url_for("auth.profile"))

    organization = user.active_organization
    if organization and organization.setting("require_mfa"):
        flash(
            f"{organization.name} requires two-factor authentication. "
            "An administrator must change that policy first.",
            "error",
        )
        return redirect(url_for("auth.profile"))

    form = MfaDisableForm()
    if form.validate_on_submit():
        # Re-authenticate: turning off a second factor is exactly the action a
        # hijacked session would want to take.
        if not user.check_password(form.password.data):
            flash("That password is not correct.", "error")
        else:
            user.disable_mfa()
            audit.record(
                audit.AuditAction.MFA_DISABLED,
                summary=f"{user.email} disabled two-factor",
                user_id=user.id, actor_label=user.email,
            )
            db.session.commit()
            flash("Two-factor authentication is off.", "success")
            return redirect(url_for("auth.profile"))

    return render_template("auth/mfa_disable.html", form=form)


__all__ = ["PENDING_MFA_KEY", "begin_challenge", "request"]
