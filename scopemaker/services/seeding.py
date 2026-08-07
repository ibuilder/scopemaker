"""Load the shipped clause and specification library into the database.

Seeding is idempotent and keyed on ``system_key``: re-running it updates the
text of existing system rows rather than creating duplicates, so shipping a
corrected clause in a new release actually corrects it for existing installs.

System rows have ``organization_id IS NULL``. An organization never edits them
directly -- it either suppresses a clause or adds its own.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from ..data import load_seed_clauses, load_seed_spec_sections
from ..data.masterformat import normalize_code
from ..extensions import db
from ..models import Clause, SpecSection

logger = logging.getLogger(__name__)


@dataclass
class SeedResult:
    clauses_created: int = 0
    clauses_updated: int = 0
    spec_sections_created: int = 0
    spec_sections_updated: int = 0
    skipped: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.skipped is None:
            self.skipped = []

    @property
    def total(self) -> int:
        return (
            self.clauses_created
            + self.clauses_updated
            + self.spec_sections_created
            + self.spec_sections_updated
        )

    def summary(self) -> str:
        return (
            f"clauses: {self.clauses_created} created, {self.clauses_updated} updated; "
            f"spec sections: {self.spec_sections_created} created, "
            f"{self.spec_sections_updated} updated"
            + (f"; {len(self.skipped)} skipped" if self.skipped else "")
        )


def _clean_text(value: Any) -> str:
    """Collapse YAML folded-scalar whitespace into a single clean paragraph."""
    return " ".join(str(value or "").split())


def seed_library(*, update_existing: bool = True) -> SeedResult:
    """Insert or refresh every system clause and specification section."""
    result = SeedResult()

    existing_clauses = {
        clause.system_key: clause
        for clause in db.session.scalars(
            select(Clause).where(Clause.system_key.is_not(None))
        )
    }

    for entry in load_seed_clauses():
        key = entry.get("key")
        if not key:
            result.skipped.append(f"clause with no key in {entry.get('_source_file')}")
            continue

        division = entry.get("division")
        # A null division means "universal"; anything else must be a real,
        # non-reserved MasterFormat number or the clause would be unreachable.
        if division is not None:
            division = normalize_code(division)
            if division is None:
                result.skipped.append(f"{key}: unknown division {entry.get('division')!r}")
                continue

        payload = {
            "division_code": division,
            "category": entry.get("category", "inclusion"),
            "text": _clean_text(entry.get("text")),
            "is_default": bool(entry.get("default", False)),
            "position": int(entry.get("position", 0)),
            "tags": entry.get("tags") or [],
            "notes": entry.get("notes"),
            "is_active": True,
        }

        clause = existing_clauses.get(key)
        if clause is None:
            db.session.add(Clause(system_key=key, organization_id=None, **payload))
            result.clauses_created += 1
        elif update_existing:
            changed = False
            for field, value in payload.items():
                if getattr(clause, field) != value:
                    setattr(clause, field, value)
                    changed = True
            if changed:
                result.clauses_updated += 1

    existing_sections = {
        section.system_key: section
        for section in db.session.scalars(
            select(SpecSection).where(SpecSection.system_key.is_not(None))
        )
    }

    for entry in load_seed_spec_sections():
        key = entry.get("key")
        if not key:
            result.skipped.append("spec section with no key")
            continue

        division = normalize_code(entry.get("division"))
        if division is None:
            result.skipped.append(f"{key}: unknown division {entry.get('division')!r}")
            continue

        related = [
            code
            for code in (normalize_code(c) for c in entry.get("related") or [])
            if code
        ]

        payload = {
            "division_code": division,
            "related_divisions": related,
            "is_universal": bool(entry.get("universal", False)),
            "code": str(entry.get("code", "")).strip(),
            "title": str(entry.get("title", "")).strip(),
            "is_default": bool(entry.get("default", False)),
            "position": int(entry.get("position", 0)),
            "is_active": True,
        }

        section = existing_sections.get(key)
        if section is None:
            db.session.add(SpecSection(system_key=key, organization_id=None, **payload))
            result.spec_sections_created += 1
        elif update_existing:
            changed = False
            for field, value in payload.items():
                if getattr(section, field) != value:
                    setattr(section, field, value)
                    changed = True
            if changed:
                result.spec_sections_updated += 1

    db.session.commit()

    if result.skipped:
        for message in result.skipped:
            logger.warning("Seed skipped: %s", message)
    logger.info("Library seed complete -- %s", result.summary())
    return result
