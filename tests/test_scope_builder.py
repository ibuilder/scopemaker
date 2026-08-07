"""Scope generation."""

from __future__ import annotations

from decimal import Decimal

from scopemaker.extensions import db
from scopemaker.models import Clause, Scope, ScopeItem
from scopemaker.models.scope import SPEC_LIST_ROLE
from scopemaker.services import library as library_service
from scopemaker.services.scope_builder import (
    BLANK,
    ScopeDraft,
    build_context,
    build_scope,
    duplicate_scope,
    issue_scope,
    render_template_text,
    revise_scope,
    save_as_template,
)


def test_generates_a_populated_document(scope):
    assert scope.item_count > 30
    assert scope.division_code == "21"
    assert scope.trade_name == "Fire Protection"
    assert scope.status == "draft"
    assert scope.version == 1

    keys = {section.key for section in scope.enabled_sections}
    assert {"intent", "summary", "inclusions", "exclusions", "recap"} <= keys


def test_trade_name_defaults_from_the_division(db, organization, user):
    result = build_scope(
        ScopeDraft(organization_id=organization.id, division_code="26",
                   created_by_id=user.id)
    )
    assert result.trade_name == "Electrical"


def test_placeholders_are_merged_into_boilerplate(scope):
    intent = scope.section("intent")
    summary = scope.section("summary")
    lead = summary.root_items[0].text_html
    assert "FIRE PROTECTION" in lead
    assert "{trade_upper}" not in lead
    assert intent.body_html and "{" not in intent.body_html


def test_unresolved_placeholders_render_as_a_visible_blank():
    context = build_context(
        trade_name=None, division_code=None, project=None, bid_package=None
    )
    rendered = render_template_text("Owner: {owner_name}.", context)
    assert BLANK in rendered
    assert "{owner_name}" not in rendered


def test_unknown_placeholder_is_left_visible():
    rendered = render_template_text("Hello {not_a_field}.", {})
    assert "{not_a_field}" in rendered


def test_spec_sections_nest_under_the_summary(scope):
    summary = scope.section("summary")
    lead = next(
        item for item in summary.items if (item.meta or {}).get("role") == SPEC_LIST_ROLE
    )
    children = sorted(lead.children, key=lambda c: c.position)
    assert len(children) >= 10
    assert all((c.meta or {}).get("role") == "spec_section" for c in children)


def test_cross_division_spec_sections_are_offered(organization):
    """A Division 21 package must be offered Division 07 firestopping.

    Forgetting the firestopping a services trade is contractually responsible
    for is the classic scope gap this library exists to close.
    """
    sections = library_service.available_spec_sections(
        organization.id, division_code="21"
    )
    codes = {s.code for s in sections}
    assert "078413" in codes, "penetration firestopping missing from a Div 21 scope"
    assert "083113" in codes, "access doors missing from a Div 21 scope"
    assert any(s.division_code == "01" for s in sections), "Division 01 sections missing"


def test_universal_and_division_clauses_are_both_selected(scope):
    inclusions = scope.section("inclusions")
    clause_ids = [i.source_clause_id for i in inclusions.items if i.source_clause_id]
    clauses = db.session.query(Clause).filter(Clause.id.in_(clause_ids)).all()
    assert any(c.division_code is None for c in clauses), "no universal clauses"
    assert any(c.division_code == "21" for c in clauses), "no trade clauses"


def test_a_section_with_content_is_enabled_even_when_off_by_default(
    db, organization, user
):
    safety = [
        c.id
        for c in library_service.available_clauses(
            organization.id, division_code="21", categories=["safety"]
        )
    ]
    assert safety, "expected shipped safety clauses"
    result = build_scope(
        ScopeDraft(
            organization_id=organization.id,
            division_code="21",
            clause_ids=safety,
            enabled_sections=["intent", "summary"],
            created_by_id=user.id,
        )
    )
    section = result.section("safety")
    assert section.is_enabled, "clauses were selected but the section stayed hidden"


def test_records_from_another_organization_are_ignored(
    db, organization, other_org, user
):
    """A forged project id must not attach another tenant's project."""
    from scopemaker.models import Project

    foreign = Project(organization_id=other_org.id, name="Someone else's job")
    db.session.add(foreign)
    db.session.commit()

    result = build_scope(
        ScopeDraft(
            organization_id=organization.id,
            division_code="21",
            project_id=foreign.id,
            created_by_id=user.id,
        )
    )
    assert result.project_id is None


def test_total_amount_sums_the_components(db, scope):
    scope.alternates_amount = Decimal("50000")
    scope.adjustments_amount = Decimal("-12500")
    db.session.commit()
    assert scope.total_amount == Decimal("1462500")


def test_total_amount_is_none_when_unpriced(db, organization, user):
    result = build_scope(
        ScopeDraft(organization_id=organization.id, division_code="09",
                   created_by_id=user.id)
    )
    assert result.total_amount is None


def test_issuing_freezes_a_revision_and_locks(db, scope, user):
    issue_scope(scope, user_id=user.id, note="Sent with subcontract")
    assert scope.status == "issued"
    assert scope.is_locked
    assert len(scope.revisions) == 1
    revision = scope.revisions[0]
    assert revision.version == 1
    assert revision.note == "Sent with subcontract"
    assert revision.snapshot["sections"], "snapshot captured no content"


def test_revising_bumps_the_version_and_unlocks(db, scope, user):
    issue_scope(scope, user_id=user.id)
    revise_scope(scope, user_id=user.id)
    assert scope.version == 2
    assert scope.status == "draft"
    assert not scope.is_locked
    # Issuing froze v1; revising must not write a second snapshot of it.
    assert [r.version for r in scope.revisions] == [1]

    issue_scope(scope, user_id=user.id)
    assert sorted(r.version for r in scope.revisions) == [1, 2]


def test_revising_a_never_issued_scope_still_snapshots(db, scope, user):
    revise_scope(scope, user_id=user.id)
    assert scope.version == 2
    assert [r.version for r in scope.revisions] == [1]


def test_duplicate_deep_copies_the_item_tree(db, scope, user):
    clone = duplicate_scope(scope, user_id=user.id)
    assert clone.id != scope.id
    assert clone.item_count == scope.item_count
    assert clone.status == "draft"
    assert clone.version == 1

    original_ids = {i.id for s in scope.sections for i in s.items}
    clone_ids = {i.id for s in clone.sections for i in s.items}
    assert original_ids.isdisjoint(clone_ids), "the copy shares rows with the original"

    # Nesting must survive the copy.
    clone_summary = clone.section("summary")
    assert any(item.children for item in clone_summary.root_items)


def test_deleting_a_parent_item_removes_its_children(db, scope):
    summary = scope.section("summary")
    parent = next(item for item in summary.root_items if item.children)
    child_ids = [c.id for c in parent.children]
    assert child_ids

    db.session.delete(parent)
    db.session.commit()
    db.session.expire_all()

    remaining = db.session.query(ScopeItem).filter(ScopeItem.id.in_(child_ids)).count()
    assert remaining == 0, "orphaned children survived the parent's deletion"


def test_save_as_template_captures_structure(db, scope, user):
    template = save_as_template(scope, name="Standard fire protection", user_id=user.id)
    assert template.organization_id == scope.organization_id
    assert template.division_code == "21"
    sections = template.payload["sections"]
    assert sections
    summary = next(s for s in sections if s["key"] == "summary")
    assert any(item["children"] for item in summary["items"])


def test_deleting_a_scope_cascades(db, scope):
    scope_id = scope.id
    db.session.delete(scope)
    db.session.commit()
    assert db.session.get(Scope, scope_id) is None
    assert db.session.query(ScopeItem).count() == 0
