"""Authentication forms."""

from __future__ import annotations

from flask_wtf import FlaskForm
from wtforms import BooleanField, PasswordField, SelectField, StringField, SubmitField
from wtforms.validators import DataRequired, Email, EqualTo, Length, Optional

from ...models.organization import ROLE_HIERARCHY, ROLE_LABELS
from ...security import MIN_PASSWORD_LENGTH, password_problems


def _password_policy(_form, field) -> None:
    from wtforms.validators import ValidationError

    problems = password_problems(field.data or "")
    if problems:
        raise ValidationError(" ".join(problems))


class LoginForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=255)])
    password = PasswordField("Password", validators=[DataRequired()])
    remember = BooleanField("Keep me signed in")
    submit = SubmitField("Sign in")


class RegisterForm(FlaskForm):
    full_name = StringField("Your name", validators=[DataRequired(), Length(max=200)])
    email = StringField("Work email", validators=[DataRequired(), Email(), Length(max=255)])
    organization_name = StringField(
        "Company name",
        validators=[Optional(), Length(max=200)],
        description="Leave blank when joining an existing organization by invite.",
    )
    password = PasswordField(
        "Password",
        validators=[DataRequired(), Length(min=MIN_PASSWORD_LENGTH), _password_policy],
    )
    confirm = PasswordField(
        "Confirm password",
        validators=[DataRequired(), EqualTo("password", message="Passwords must match.")],
    )
    submit = SubmitField("Create account")


class InviteAcceptForm(FlaskForm):
    full_name = StringField("Your name", validators=[DataRequired(), Length(max=200)])
    password = PasswordField(
        "Password",
        validators=[DataRequired(), Length(min=MIN_PASSWORD_LENGTH), _password_policy],
    )
    confirm = PasswordField(
        "Confirm password",
        validators=[DataRequired(), EqualTo("password", message="Passwords must match.")],
    )
    submit = SubmitField("Join organization")


class InviteForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=255)])
    role = SelectField(
        "Role",
        choices=[(role, ROLE_LABELS[role]) for role in reversed(ROLE_HIERARCHY)],
        default="editor",
    )
    submit = SubmitField("Send invite")


class ForgotPasswordForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=255)])
    submit = SubmitField("Send reset link")


class ResetPasswordForm(FlaskForm):
    password = PasswordField(
        "New password",
        validators=[DataRequired(), Length(min=MIN_PASSWORD_LENGTH), _password_policy],
    )
    confirm = PasswordField(
        "Confirm new password",
        validators=[DataRequired(), EqualTo("password", message="Passwords must match.")],
    )
    submit = SubmitField("Set new password")


class ProfileForm(FlaskForm):
    full_name = StringField("Your name", validators=[DataRequired(), Length(max=200)])
    # Distinct name: this form shares a page with ChangePasswordForm, and two
    # fields both called "submit" make either post look like both.
    save_profile = SubmitField("Save")


class DeleteAccountForm(FlaskForm):
    """Deleting an account is irreversible, so make it deliberate.

    Typing the email address is the confirmation for everyone; a password is
    additionally required for accounts that have one. SSO-only accounts have no
    password to check, and demanding one would lock them out of a right they
    are supposed to have.
    """

    confirm_email = StringField(
        "Type your email address to confirm",
        validators=[DataRequired(), Length(max=255)],
    )
    password = PasswordField("Your password")
    delete_account = SubmitField("Delete my account")


class ChangePasswordForm(FlaskForm):
    current_password = PasswordField("Current password", validators=[DataRequired()])
    password = PasswordField(
        "New password",
        validators=[DataRequired(), Length(min=MIN_PASSWORD_LENGTH), _password_policy],
    )
    confirm = PasswordField(
        "Confirm new password",
        validators=[DataRequired(), EqualTo("password", message="Passwords must match.")],
    )
    change_password = SubmitField("Change password")
