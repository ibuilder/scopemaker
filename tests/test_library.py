"""Clause library queries and the seeded content itself."""

from __future__ import annotations

from scopemaker.data import load_seed_clauses, load_seed_spec_sections
from scopemaker.data.masterformat import RESERVED_CODES, normalize_code
from scopemaker.models import Clause, ClauseSuppression, SpecSection
from scopemaker.models.library import CLAUSE_CATEGORIES
from scopemaker.services import library as library_service
from scopemaker.services.seeding import seed_library

# ---------------------------------------------------------------------------
# Seed data quality
# ---------------------------------------------------------------------------

def test_seed_keys_are_unique():
    keys = [c["key"] for c in load_seed_clauses()]
    assert len(keys) == len(set(keys))
    section_keys = [s["key"] for s in load_seed_spec_sections()]
    assert len(section_keys) == len(set(section_keys))


def test_seed_clauses_use_real_categories_and_divisions():
    for entry in load_seed_clauses():
        assert entry["category"] in CLAUSE_CATEGORIES, entry["key"]
        division = entry.get("division")
        if division is not None:
            assert normalize_code(division) is not None, entry["key"]
            assert division not in RESERVED_CODES, entry["key"]


def test_seed_clause_text_is_substantial():
    for entry in load_seed_clauses():
        text = " ".join(str(entry["text"]).split())
        assert len(text) > 40, f"{entry['key']} is too short to be contract language"
        assert not text.endswith(("and", "or", "the", ",")), entry["key"]


def test_seed_spec_sections_reference_real_divisions():
    for entry in load_seed_spec_sections():
        assert normalize_code(entry["division"]) is not None, entry["key"]
        for related in entry.get("related") or []:
            assert normalize_code(related) is not None, entry["key"]


def test_seeding_is_idempotent(db):
    before = db.session.query(Clause).count()
    result = seed_library()
    assert result.clauses_created == 0
    assert db.session.query(Clause).count() == before


def test_seeding_refreshes_changed_text(db):
    clause = (
        db.session.query(Clause)
        .filter(Clause.system_key == "univ.incl.cleanup")
        .one()
    )
    clause.text = "Tampered text"
    db.session.commit()

    seed_library()
    db.session.refresh(clause)
    assert clause.text != "Tampered text"


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def test_division_query_includes_universal_clauses(organization):
    clauses = library_service.available_clauses(organization.id, division_code="21")
    assert any(c.division_code is None for c in clauses)
    assert any(c.division_code == "21" for c in clauses)
    assert not any(
        c.division_code not in (None, "21") for c in clauses
    ), "clauses from another division leaked in"


def test_universal_clauses_sort_before_trade_clauses(organization):
    clauses = library_service.available_clauses(
        organization.id, division_code="21", categories=["inclusion"]
    )
    divisions = [c.division_code for c in clauses]
    first_trade = divisions.index("21")
    assert all(d is None for d in divisions[:first_trade])


def test_defaults_are_a_subset_of_available(organization):
    available = {c.id for c in library_service.available_clauses(
        organization.id, division_code="26")}
    defaults = set(library_service.default_clause_ids(organization.id, "26"))
    assert defaults
    assert defaults <= available


def test_every_covered_division_has_defaults(organization):
    """A user who picks a covered division must get a usable starting document."""
    covered = {
        normalize_code(c["division"])
        for c in load_seed_clauses()
        if c.get("division") is not None
    }
    for code in sorted(covered):
        defaults = library_service.default_clause_ids(organization.id, code)
        assert len(defaults) >= 10, f"Division {code} generates too thin a scope"


def test_suppression_hides_a_system_clause(db, organization):
    clauses = library_service.available_clauses(organization.id, division_code="21")
    target = clauses[0]
    db.session.add(
        ClauseSuppression(organization_id=organization.id, clause_id=target.id)
    )
    db.session.commit()

    after = library_service.available_clauses(organization.id, division_code="21")
    assert target.id not in {c.id for c in after}


def test_suppression_does_not_affect_another_organization(db, organization, other_org):
    clauses = library_service.available_clauses(organization.id, division_code="21")
    target = clauses[0]
    db.session.add(
        ClauseSuppression(organization_id=organization.id, clause_id=target.id)
    )
    db.session.commit()

    theirs = library_service.available_clauses(other_org.id, division_code="21")
    assert target.id in {c.id for c in theirs}


def test_custom_clauses_are_private_to_their_organization(db, organization, other_org):
    mine = Clause(
        organization_id=organization.id,
        category="inclusion",
        division_code="21",
        text="Our own standard obligation for fire protection work on this project.",
    )
    db.session.add(mine)
    db.session.commit()

    assert mine.id in {
        c.id for c in library_service.available_clauses(organization.id, division_code="21")
    }
    assert mine.id not in {
        c.id for c in library_service.available_clauses(other_org.id, division_code="21")
    }


def test_get_clauses_filters_out_foreign_ids(db, organization, other_org):
    theirs = Clause(
        organization_id=other_org.id,
        category="inclusion",
        division_code="21",
        text="A rival contractor's private clause that must never leak across tenants.",
    )
    db.session.add(theirs)
    db.session.commit()

    fetched = library_service.get_clauses(organization.id, [theirs.id])
    assert fetched == []


def test_spec_sections_include_cross_references_and_division_01(organization):
    sections = library_service.available_spec_sections(
        organization.id, division_code="26"
    )
    divisions = {s.division_code for s in sections}
    assert "26" in divisions
    assert "01" in divisions, "Division 01 procedural sections missing"
    assert "07" in divisions, "firestopping cross-reference missing"


def test_spec_sections_exclude_unrelated_divisions(organization):
    sections = library_service.available_spec_sections(
        organization.id, division_code="26"
    )
    codes = {s.code for s in sections}
    # Masonry mortar has nothing to do with an electrical package.
    assert "040511" not in codes


def test_applies_to_logic():
    section = SpecSection(
        division_code="07", related_divisions=["21", "22"], code="078413", title="x"
    )
    assert section.applies_to("07") is True
    assert section.applies_to("21") is True
    assert section.applies_to("26") is False
    assert section.applies_to(None) is False

    universal = SpecSection(
        division_code="01", related_divisions=[], is_universal=True, code="013300",
        title="y",
    )
    assert universal.applies_to("26") is True
    assert universal.applies_to(None) is True


def test_library_stats(organization):
    stats = library_service.library_stats(organization.id)
    assert stats["clauses_total"] > 100
    assert stats["divisions_covered"] > 10
    assert stats["clauses_custom"] == 0
