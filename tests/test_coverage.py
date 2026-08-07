"""Project scope coverage analysis.

These tests encode the judgement calls the analysis makes, because getting them
wrong in either direction destroys its usefulness: a false gap sends someone
chasing work that is already bought, and a missed gap is the thing that shows
up as a change order.
"""

from __future__ import annotations

import csv
import io

import pytest

from scopemaker.extensions import db
from scopemaker.models import BidPackage, ScopeItem
from scopemaker.services import library as library_service
from scopemaker.services.coverage import (
    COVERED,
    GAP,
    OVERLAP,
    SHARED,
    analyse_project,
    to_csv,
)
from scopemaker.services.scope_builder import ScopeDraft, build_scope


def make_scope(organization, project, division, trade, *, number=None, **kwargs):
    package = BidPackage(
        project_id=project.id,
        organization_id=organization.id,
        number=number or f"BP-{division}A",
        name=trade,
        division_code=division,
        trade_name=trade,
    )
    db.session.add(package)
    db.session.commit()
    return build_scope(
        ScopeDraft(
            organization_id=organization.id,
            division_code=division,
            trade_name=trade,
            project_id=project.id,
            bid_package_id=package.id,
            clause_ids=library_service.default_clause_ids(organization.id, division),
            spec_section_ids=library_service.default_spec_section_ids(
                organization.id, division
            ),
            **kwargs,
        )
    )


@pytest.fixture()
def multi_trade(db, organization, project):
    """A project with the trades that actually interact on a real job."""
    return {
        "fire": make_scope(organization, project, "21", "Fire Protection"),
        "hvac": make_scope(organization, project, "23", "HVAC"),
        "electrical": make_scope(organization, project, "26", "Electrical"),
        "drywall": make_scope(organization, project, "09", "Drywall and Finishes"),
    }


def find(report, code):
    return next((s for s in report.sections if s.code == code), None)


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------

def test_empty_project_reports_nothing(db, project):
    report = analyse_project(project)
    assert report.scopes == []
    assert report.sections == []
    assert report.is_clean


def test_a_freshly_generated_project_has_no_gaps(multi_trade, project):
    """Scopes built from the library defaults must not accuse themselves."""
    report = analyse_project(project)
    assert report.gaps == [], [s.code for s in report.gaps]
    assert len(report.scopes) == 4
    assert report.divisions_present == ["09", "21", "23", "26"]


def test_generated_project_has_no_false_overlaps(multi_trade, project):
    """Cross-trade sections must not be reported as double-buying."""
    report = analyse_project(project)
    assert report.overlaps == [], [s.code for s in report.overlaps]


# ---------------------------------------------------------------------------
# Gaps
# ---------------------------------------------------------------------------

def test_removing_a_section_creates_a_gap(db, multi_trade, project):
    fire = multi_trade["fire"]
    for section in fire.sections:
        for item in list(section.items):
            if (item.meta or {}).get("spec_code") == "211316":
                db.session.delete(item)
    db.session.commit()

    report = analyse_project(project)
    gap_codes = {s.code for s in report.gaps}
    assert "211316" in gap_codes
    assert find(report, "211316").status == GAP
    assert find(report, "211316").claimed_by == []
    assert not report.is_clean


def test_a_hand_typed_section_number_still_counts_as_claimed(db, multi_trade, project):
    """Editing a spec line by hand must not manufacture a gap."""
    fire = multi_trade["fire"]
    target = None
    for section in fire.sections:
        for item in section.items:
            if (item.meta or {}).get("spec_code") == "211316":
                target = item
    assert target is not None

    # Strip the structured metadata, keeping only the text a person would type.
    target.meta = {"role": "spec_section"}
    target.text_html = "211316 &ndash; Dry-Pipe Sprinkler Systems (revised)"
    db.session.commit()

    report = analyse_project(project)
    assert "211316" not in {s.code for s in report.gaps}
    assert find(report, "211316").status == COVERED


def test_a_disabled_section_does_not_count_as_claimed(db, multi_trade, project):
    fire = multi_trade["fire"]
    summary = fire.section("summary")
    summary.is_enabled = False
    db.session.commit()

    report = analyse_project(project)
    # The summary carries the spec list, so switching it off drops every claim.
    assert any(s.code.startswith("2113") for s in report.gaps)


# ---------------------------------------------------------------------------
# Overlaps and shared seams
# ---------------------------------------------------------------------------

def test_firestopping_across_four_trades_is_shared_not_an_overlap(multi_trade, project):
    """Every trade firestops its own penetrations. That is correct."""
    report = analyse_project(project)
    firestopping = find(report, "078413")
    assert firestopping is not None
    assert len(firestopping.claimed_by) >= 3
    assert firestopping.status == SHARED
    assert firestopping not in report.overlaps


def test_shared_seams_do_not_count_as_findings(multi_trade, project):
    report = analyse_project(project)
    assert report.shared, "expected cross-trade seams to be reported"
    assert report.finding_count == len(report.gaps) + len(report.overlaps) + len(
        report.redirects
    )


def test_two_trades_on_a_trade_specific_section_is_an_overlap(
    db, organization, project, multi_trade
):
    """Both plumbing and HVAC claiming domestic water is a real double-buy."""
    plumbing = make_scope(organization, project, "22", "Plumbing")

    hvac = multi_trade["hvac"]
    summary = hvac.section("summary")
    parent = next(
        i for i in summary.items if (i.meta or {}).get("role") == "spec_list"
    )
    db.session.add(
        ScopeItem(
            section_id=summary.id,
            parent_id=parent.id,
            text_html="221116 &ndash; Domestic Water Piping",
            position=999,
            meta={"role": "spec_section", "spec_code": "221116",
                  "spec_title": "Domestic Water Piping", "spec_division": "22"},
        )
    )
    db.session.commit()

    report = analyse_project(project)
    water = find(report, "221116")
    assert water is not None
    assert water.status == OVERLAP
    assert {c.division_code for c in water.claimed_by} == {"22", "23"}
    assert water in report.overlaps
    assert plumbing.id in {c.id for c in water.claimed_by}


def test_division_01_sections_are_never_a_finding(multi_trade, project):
    report = analyse_project(project)
    for section in report.sections:
        if section.division_code == "01":
            assert section.status == COVERED, section.code


# ---------------------------------------------------------------------------
# Exclusion hand-offs
# ---------------------------------------------------------------------------

def test_exclusion_to_an_absent_division_is_flagged(db, organization, project):
    """Fire protection excludes the fire alarm; with no Division 28, that is a gap."""
    make_scope(organization, project, "21", "Fire Protection")
    report = analyse_project(project)

    referenced = {r.referenced_division for r in report.redirects}
    assert "28" in referenced
    finding = next(r for r in report.redirects if r.referenced_division == "28")
    assert finding.scope.division_code == "21"
    assert "fire alarm" in finding.text.lower()
    assert "Electronic Safety" in finding.summary


def test_the_hand_off_clears_once_that_trade_is_scoped(db, organization, project):
    make_scope(organization, project, "21", "Fire Protection")
    assert "28" in {r.referenced_division for r in analyse_project(project).redirects}

    make_scope(organization, project, "28", "Fire Alarm")
    after = analyse_project(project)
    assert "28" not in {r.referenced_division for r in after.redirects}


def test_a_scope_referring_to_its_own_division_is_not_a_hand_off(
    db, organization, project
):
    scope = make_scope(organization, project, "26", "Electrical")
    exclusions = scope.section("exclusions")
    db.session.add(
        ScopeItem(
            section_id=exclusions.id,
            text_html="Work under Division 26 performed outside normal hours.",
            position=900,
        )
    )
    db.session.commit()

    report = analyse_project(project)
    assert "26" not in {r.referenced_division for r in report.redirects}


def test_only_one_hand_off_per_scope_and_division(db, organization, project):
    scope = make_scope(organization, project, "21", "Fire Protection")
    exclusions = scope.section("exclusions")
    for index in range(3):
        db.session.add(
            ScopeItem(
                section_id=exclusions.id,
                text_html=f"Item {index}, which is by the Division 28 Subcontractor.",
                position=900 + index,
            )
        )
    db.session.commit()

    report = analyse_project(project)
    div28 = [r for r in report.redirects if r.referenced_division == "28"]
    assert len(div28) == 1, "the same hand-off was reported repeatedly"


# ---------------------------------------------------------------------------
# Bookkeeping
# ---------------------------------------------------------------------------

def test_packages_without_a_scope_are_listed(db, organization, project):
    make_scope(organization, project, "21", "Fire Protection")
    db.session.add(
        BidPackage(
            project_id=project.id, organization_id=organization.id,
            number="BP-32A", name="Site Improvements", division_code="32",
        )
    )
    db.session.commit()

    report = analyse_project(project)
    assert ("BP-32A", "Site Improvements") in report.packages_without_scope


def test_archived_scopes_are_excluded_by_default(db, organization, project):
    scope = make_scope(organization, project, "21", "Fire Protection")
    scope.status = "archived"
    db.session.commit()

    assert analyse_project(project).scopes == []
    assert len(analyse_project(project, include_archived=True).scopes) == 1


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def test_to_dict_shape(multi_trade, project):
    payload = analyse_project(project).to_dict()
    assert payload["project"]["id"] == project.id
    assert payload["summary"]["scopes"] == 4
    assert set(payload["summary"]) >= {"gaps", "overlaps", "shared", "redirects"}
    assert payload["divisions_present"] == ["09", "21", "23", "26"]
    section = payload["sections"][0]
    assert set(section) == {"code", "title", "division_code", "status", "claimed_by"}


def test_csv_lists_findings_first(db, multi_trade, project):
    fire = multi_trade["fire"]
    for section in fire.sections:
        for item in list(section.items):
            if (item.meta or {}).get("spec_code") == "211316":
                db.session.delete(item)
    db.session.commit()

    report = analyse_project(project)
    rows = list(csv.reader(io.StringIO(to_csv(report))))

    header = next(i for i, r in enumerate(rows) if r and r[0] == "Section")
    first = rows[header + 1]
    assert first[0] == "211316"
    assert first[3] == GAP
    assert first[4] == "NOBODY"


def test_csv_includes_hand_offs(db, organization, project):
    make_scope(organization, project, "21", "Fire Protection")
    output = to_csv(analyse_project(project))
    assert "Exclusions assigning work to a division not on the project" in output
    assert "28 Electronic Safety and Security" in output


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

def test_coverage_page_renders(auth_client, multi_trade, project):
    response = auth_client.get(f"/projects/{project.id}/coverage")
    assert response.status_code == 200
    body = response.data.decode()
    assert "Scope coverage" in body
    assert "078413" in body, "the shared firestopping seam should be shown"


def test_coverage_csv_download(auth_client, multi_trade, project):
    response = auth_client.get(f"/projects/{project.id}/coverage.csv")
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert "attachment" in response.headers["Content-Disposition"]
    assert b"ScopeMaker coverage report" in response.data


def test_coverage_api(client, db, user, organization, multi_trade, project):
    from scopemaker.models import ApiToken

    record, raw = ApiToken.issue(
        user=user, organization_id=organization.id, name="cov", scopes="read"
    )
    db.session.add(record)
    db.session.commit()

    response = client.get(
        f"/api/v1/projects/{project.id}/coverage",
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert response.status_code == 200
    assert response.json["summary"]["scopes"] == 4


def test_coverage_is_tenant_scoped(db, client, other_org, project, multi_trade):
    from scopemaker.models import User

    rival = db.session.query(User).filter_by(email="rival@rival.example").one()
    from .conftest import login

    login(client, rival.email)
    assert client.get(f"/projects/{project.id}/coverage").status_code == 404
    assert client.get(f"/projects/{project.id}/coverage.csv").status_code == 404
