"""Shared view helpers.

The tenant-scoped getters here are the enforcement point for isolation: views
never call ``db.session.get(Scope, id)`` directly, so a record belonging to
another organization returns 404 rather than leaking across tenants.
"""

from __future__ import annotations

from typing import TypeVar

from flask import abort
from flask_login import current_user

from ..extensions import db
from ..models import BidPackage, Clause, Project, Scope, ScopeTemplate, SpecSection

T = TypeVar("T")


def current_org_id() -> str:
    """The active organization, or 403 when the user has none."""
    organization_id = current_user.active_organization_id
    if not organization_id:
        abort(403)
    return organization_id


def _scoped(model: type[T], record_id: str, *, allow_system: bool = False) -> T:
    record = db.session.get(model, record_id)
    if record is None:
        abort(404)
    owner = getattr(record, "organization_id", None)
    if owner == current_org_id():
        return record
    # System library rows (organization_id IS NULL) are readable by everyone.
    if allow_system and owner is None:
        return record
    # Deliberately 404 rather than 403: a 403 would confirm the id exists.
    abort(404)


def get_scope_or_404(scope_id: str) -> Scope:
    return _scoped(Scope, scope_id)


def get_project_or_404(project_id: str) -> Project:
    return _scoped(Project, project_id)


def get_bid_package_or_404(package_id: str) -> BidPackage:
    return _scoped(BidPackage, package_id)


def get_clause_or_404(clause_id: str, *, allow_system: bool = True) -> Clause:
    return _scoped(Clause, clause_id, allow_system=allow_system)


def get_spec_section_or_404(section_id: str, *, allow_system: bool = True) -> SpecSection:
    return _scoped(SpecSection, section_id, allow_system=allow_system)


def get_template_or_404(template_id: str, *, allow_system: bool = True) -> ScopeTemplate:
    return _scoped(ScopeTemplate, template_id, allow_system=allow_system)


def require_unlocked(scope: Scope) -> None:
    """Block edits to an issued or archived scope."""
    if scope.is_locked:
        abort(
            409,
            description=(
                f"This scope is {scope.status_label.lower()} and cannot be edited. "
                "Create a new revision to make changes."
            ),
        )
