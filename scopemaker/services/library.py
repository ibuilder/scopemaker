"""Queries over the clause and specification library.

Every lookup unions the shipped system rows with the organization's own rows
and then removes anything the organization has suppressed.  Callers never query
``Clause`` directly, so the suppression rule cannot be forgotten in one place
and enforced in another.
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import or_, select

from ..data.masterformat import normalize_code
from ..extensions import db
from ..models import Clause, ClauseSuppression, SpecSection
from ..models.library import CATEGORY_ORDER


def suppressed_clause_ids(organization_id: str) -> set[str]:
    return set(
        db.session.scalars(
            select(ClauseSuppression.clause_id).where(
                ClauseSuppression.organization_id == organization_id
            )
        )
    )


def available_clauses(
    organization_id: str,
    *,
    division_code: str | None = None,
    categories: list[str] | None = None,
    include_universal: bool = True,
    include_inactive: bool = False,
) -> list[Clause]:
    """Clauses this organization may put on a scope, in display order.

    ``division_code`` narrows to that division plus (unless disabled) the
    universal clauses that apply to every trade.
    """
    division = normalize_code(division_code)

    stmt = select(Clause).where(
        or_(Clause.organization_id == organization_id, Clause.organization_id.is_(None))
    )
    if not include_inactive:
        stmt = stmt.where(Clause.is_active.is_(True))

    if division is not None:
        if include_universal:
            stmt = stmt.where(
                or_(Clause.division_code == division, Clause.division_code.is_(None))
            )
        else:
            stmt = stmt.where(Clause.division_code == division)
    elif not include_universal:
        stmt = stmt.where(Clause.division_code.is_not(None))

    if categories:
        stmt = stmt.where(Clause.category.in_(categories))

    suppressed = suppressed_clause_ids(organization_id)
    clauses = [c for c in db.session.scalars(stmt) if c.id not in suppressed]

    def sort_key(clause: Clause) -> tuple:
        category_rank = (
            CATEGORY_ORDER.index(clause.category)
            if clause.category in CATEGORY_ORDER
            else len(CATEGORY_ORDER)
        )
        # Within a category: universal clauses first, then trade-specific,
        # then the organization's own additions -- which mirrors how a scope
        # reads from general obligations down to trade detail.
        return (
            category_rank,
            0 if clause.division_code is None else 1,
            0 if clause.organization_id is None else 1,
            clause.position,
            clause.id,
        )

    return sorted(clauses, key=sort_key)


def clauses_by_category(
    organization_id: str,
    *,
    division_code: str | None = None,
    categories: list[str] | None = None,
) -> dict[str, list[Clause]]:
    """The same set, grouped for rendering the clause picker."""
    grouped: dict[str, list[Clause]] = defaultdict(list)
    for clause in available_clauses(
        organization_id, division_code=division_code, categories=categories
    ):
        grouped[clause.category].append(clause)
    return {
        category: grouped[category]
        for category in CATEGORY_ORDER
        if grouped.get(category)
    }


def default_clause_ids(organization_id: str, division_code: str | None) -> list[str]:
    """Clauses pre-selected when a scope is generated for this division."""
    return [
        c.id
        for c in available_clauses(organization_id, division_code=division_code)
        if c.is_default
    ]


def available_spec_sections(
    organization_id: str,
    *,
    division_code: str | None = None,
    include_inactive: bool = False,
) -> list[SpecSection]:
    """Specification sections offered for a scope in ``division_code``.

    Includes sections that live in other divisions but are cross-referenced to
    this one -- Division 07 firestopping on a Division 21 package, for
    instance.
    """
    division = normalize_code(division_code)

    stmt = select(SpecSection).where(
        or_(
            SpecSection.organization_id == organization_id,
            SpecSection.organization_id.is_(None),
        )
    )
    if not include_inactive:
        stmt = stmt.where(SpecSection.is_active.is_(True))

    sections = list(db.session.scalars(stmt))
    if division is not None:
        sections = [s for s in sections if s.applies_to(division)]

    def sort_key(section: SpecSection) -> tuple:
        # A trade's own division first, then Division 01 procedural sections,
        # then cross-referenced sections from other divisions.
        if division is not None and section.division_code == division:
            group = 0
        elif section.is_universal:
            group = 1
        else:
            group = 2
        return (group, section.division_code, section.position, section.code)

    return sorted(sections, key=sort_key)


def default_spec_section_ids(organization_id: str, division_code: str | None) -> list[str]:
    return [
        s.id
        for s in available_spec_sections(organization_id, division_code=division_code)
        if s.is_default
    ]


def get_clauses(organization_id: str, clause_ids: list[str]) -> list[Clause]:
    """Fetch specific clauses, filtered to what this organization may use.

    Ids arrive from client form posts, so they are re-checked against the
    organization's visible set rather than trusted.
    """
    if not clause_ids:
        return []
    wanted = set(clause_ids)
    suppressed = suppressed_clause_ids(organization_id)
    stmt = select(Clause).where(
        Clause.id.in_(wanted),
        or_(Clause.organization_id == organization_id, Clause.organization_id.is_(None)),
    )
    found = [c for c in db.session.scalars(stmt) if c.id not in suppressed]
    order = {cid: i for i, cid in enumerate(clause_ids)}
    return sorted(found, key=lambda c: order.get(c.id, 0))


def get_spec_sections(organization_id: str, section_ids: list[str]) -> list[SpecSection]:
    if not section_ids:
        return []
    stmt = select(SpecSection).where(
        SpecSection.id.in_(set(section_ids)),
        or_(
            SpecSection.organization_id == organization_id,
            SpecSection.organization_id.is_(None),
        ),
    )
    found = list(db.session.scalars(stmt))
    order = {sid: i for i, sid in enumerate(section_ids)}
    return sorted(found, key=lambda s: order.get(s.id, 0))


def library_stats(organization_id: str) -> dict[str, int]:
    """Counts for the library dashboard."""
    clauses = available_clauses(organization_id)
    sections = available_spec_sections(organization_id)
    return {
        "clauses_total": len(clauses),
        "clauses_custom": sum(1 for c in clauses if c.organization_id is not None),
        "clauses_suppressed": len(suppressed_clause_ids(organization_id)),
        "spec_sections_total": len(sections),
        "spec_sections_custom": sum(1 for s in sections if s.organization_id is not None),
        "divisions_covered": len({c.division_code for c in clauses if c.division_code}),
    }
