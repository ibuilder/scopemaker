"""User and organization provisioning."""

from __future__ import annotations

import logging
from urllib.parse import urljoin, urlparse

from flask import current_app, request
from slugify import slugify
from sqlalchemy import func, select

from ..errors import ConflictError, ValidationError
from ..extensions import db
from ..models import Invitation, Membership, Organization, User
from ..models.base import utcnow

logger = logging.getLogger(__name__)


def unique_slug(name: str) -> str:
    """A URL-safe organization slug that is not already taken."""
    base = slugify(name)[:60] or "org"
    candidate = base
    suffix = 2
    while db.session.scalar(select(Organization).where(Organization.slug == candidate)):
        candidate = f"{base}-{suffix}"[:70]
        suffix += 1
    return candidate


def create_organization(name: str, *, owner: User | None = None,
                        role: str = "admin") -> Organization:
    """Create an organization, optionally with a first member."""
    if not name or not name.strip():
        raise ValidationError("An organization name is required.")
    organization = Organization(name=name.strip(), slug=unique_slug(name))
    db.session.add(organization)
    db.session.flush()
    if owner is not None:
        db.session.add(
            Membership(organization_id=organization.id, user_id=owner.id, role=role)
        )
    return organization


def create_user(
    *,
    email: str,
    full_name: str,
    password: str | None = None,
    sso_subject: str | None = None,
    sso_provider: str | None = None,
) -> User:
    """Create a user, rejecting a duplicate email case-insensitively."""
    normalized = (email or "").strip().lower()
    if not normalized:
        raise ValidationError("An email address is required.")

    existing = db.session.scalar(
        select(User).where(func.lower(User.email) == normalized)
    )
    if existing is not None:
        raise ConflictError("An account with that email address already exists.")

    user = User(
        email=normalized,
        full_name=(full_name or "").strip(),
        sso_subject=sso_subject,
        sso_provider=sso_provider,
    )
    if password:
        user.set_password(password)
    db.session.add(user)
    db.session.flush()
    return user


def accept_invitation(invitation: Invitation, user: User) -> Membership:
    """Attach a user to the inviting organization and burn the invite."""
    if not invitation.is_usable:
        raise ValidationError("This invitation has expired or has already been used.")

    existing = user.membership_for(invitation.organization_id)
    if existing is not None:
        membership = existing
    else:
        membership = Membership(
            organization_id=invitation.organization_id,
            user_id=user.id,
            role=invitation.role,
        )
        db.session.add(membership)

    invitation.accepted_at = utcnow()
    return membership


def find_invitation(token: str) -> Invitation | None:
    if not token:
        return None
    return db.session.scalar(select(Invitation).where(Invitation.token == token))


def provision_sso_user(claims: dict) -> User:
    """Find or create the local account behind an OIDC identity.

    Matching is on the issuer's stable subject first and email second, so a
    user whose email address changes at the IdP keeps their account and their
    scopes rather than silently getting a second one.
    """
    config = current_app.config
    provider = config["OIDC_NAME"]
    subject = claims.get("sub")
    email = (claims.get("email") or "").strip().lower()

    if not email:
        raise ValidationError("The identity provider did not return an email address.")

    allowed = config.get("OIDC_ALLOWED_DOMAINS") or []
    if allowed:
        domain = email.rsplit("@", 1)[-1]
        if domain not in {d.lower() for d in allowed}:
            raise ValidationError(
                f"Sign-in is not permitted for the {domain} domain."
            )

    user = None
    if subject:
        user = db.session.scalar(
            select(User).where(
                User.sso_subject == subject, User.sso_provider == provider
            )
        )
    if user is None:
        user = db.session.scalar(select(User).where(func.lower(User.email) == email))

    if user is None:
        user = User(email=email, full_name=claims.get("name") or email.split("@")[0])
        db.session.add(user)
        db.session.flush()
        logger.info("Provisioned new SSO user %s", email)

    # Bind the identity so future logins match on subject even if email moves.
    user.sso_subject = subject or user.sso_subject
    user.sso_provider = provider
    if claims.get("name") and not user.full_name:
        user.full_name = claims["name"]

    if not user.memberships:
        _attach_default_organization(user, email)

    return user


def _attach_default_organization(user: User, email: str) -> None:
    """Put a brand-new SSO user somewhere sensible."""
    slug = current_app.config.get("OIDC_DEFAULT_ORG")
    organization = None
    if slug:
        organization = db.session.scalar(
            select(Organization).where(Organization.slug == slug)
        )
        if organization is None:
            logger.warning(
                "OIDC_DEFAULT_ORG=%r does not match any organization; "
                "creating a personal organization for %s instead",
                slug,
                email,
            )
    if organization is None:
        domain = email.rsplit("@", 1)[-1]
        organization = create_organization(domain)
    db.session.add(
        Membership(organization_id=organization.id, user_id=user.id, role="editor")
    )


def is_safe_redirect(target: str | None) -> bool:
    """Reject off-site ``next`` targets.

    Without this an attacker can send a victim to /auth/login?next=//evil.example
    and have the application itself perform the redirect after a successful
    login, lending the destination the app's credibility.
    """
    if not target:
        return False
    candidate = urlparse(urljoin(request.host_url, target))
    host_url = urlparse(request.host_url)
    return candidate.scheme in ("http", "https") and candidate.netloc == host_url.netloc


def safe_redirect_target(target: str | None, fallback: str) -> str:
    return target if (target and is_safe_redirect(target)) else fallback
