"""Model package.

Importing this module registers every mapped class with the declarative
registry, which is what Alembic autogenerate and ``db.create_all()`` need.
"""

from __future__ import annotations

import sqlite3

from sqlalchemy import event
from sqlalchemy.engine import Engine

from .audit import ACTION_LABELS, SECURITY_ACTIONS, AuditAction, AuditEvent
from .base import Model, new_uuid, utcnow
from .library import (
    CATEGORY_ORDER,
    CLAUSE_CATEGORIES,
    Clause,
    ClauseSuppression,
    SpecSection,
)
from .organization import (
    ROLE_HIERARCHY,
    ROLE_LABELS,
    Invitation,
    Membership,
    Organization,
    role_rank,
)
from .procore import ProcoreConnection
from .project import BidPackage, Project
from .render import RenderJob
from .scope import (
    CATEGORY_TO_SECTION,
    DEFAULT_SECTIONS,
    LOCKED_STATUSES,
    SCOPE_STATUSES,
    SECTION_KEYS,
    STATUS_LABELS,
    Scope,
    ScopeItem,
    ScopeRevision,
    ScopeSection,
    SectionKind,
)
from .template import ScopeTemplate
from .user import ApiToken, PasswordResetToken, User

__all__ = [
    "ACTION_LABELS",
    "CATEGORY_ORDER",
    "CATEGORY_TO_SECTION",
    "CLAUSE_CATEGORIES",
    "DEFAULT_SECTIONS",
    "LOCKED_STATUSES",
    "ROLE_HIERARCHY",
    "ROLE_LABELS",
    "SCOPE_STATUSES",
    "SECTION_KEYS",
    "SECURITY_ACTIONS",
    "STATUS_LABELS",
    "ApiToken",
    "AuditAction",
    "AuditEvent",
    "BidPackage",
    "Clause",
    "ClauseSuppression",
    "Invitation",
    "Membership",
    "Model",
    "Organization",
    "PasswordResetToken",
    "ProcoreConnection",
    "Project",
    "RenderJob",
    "Scope",
    "ScopeItem",
    "ScopeRevision",
    "ScopeSection",
    "ScopeTemplate",
    "SectionKind",
    "SpecSection",
    "User",
    "new_uuid",
    "role_rank",
    "utcnow",
]


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
    """SQLite ignores FK constraints unless asked not to.

    Several relationships (notably the ScopeItem tree) rely on ON DELETE
    CASCADE at the database level, so this has to be on for dev and test runs
    to behave like PostgreSQL.
    """
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
