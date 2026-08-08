"""End-to-end walks through the web UI."""

from __future__ import annotations

import re

from scopemaker.services import library as library_service


def test_health_endpoints(client):
    assert client.get("/healthz").json["status"] == "ok"
    ready = client.get("/readyz").json
    assert ready["status"] == "ok"
    assert ready["database"] == "ok"


def test_landing_page_renders_for_anonymous_visitors(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"Scope of work exhibits" in response.data


def test_registration_creates_an_organization_and_signs_in(client, db):
    response = client.post(
        "/auth/register",
        data={
            "full_name": "Sam Foreman",
            "email": "sam@newco.example",
            "organization_name": "Newco Builders",
            "password": "correct-horse-battery-staple",
            "confirm": "correct-horse-battery-staple",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Newco Builders" in response.data

    from scopemaker.models import User

    account = User.by_email("sam@newco.example")
    assert account is not None
    assert account.memberships[0].role == "admin"


def test_registration_rejects_a_duplicate_email(client, user):
    response = client.post(
        "/auth/register",
        data={
            "full_name": "Impostor",
            "email": user.email,
            "organization_name": "Fake Co",
            "password": "correct-horse-battery-staple",
            "confirm": "correct-horse-battery-staple",
        },
        follow_redirects=True,
    )
    assert b"already exists" in response.data


def test_dashboard_summarises_the_organization(auth_client, scope):
    response = auth_client.get("/dashboard")
    assert response.status_code == 200
    assert b"Meridian Construction" in response.data
    assert b"EXHIBIT B" in response.data


def test_full_wizard_walk(auth_client, db, organization, project, bid_package):
    step_one = auth_client.get(f"/scopes/new?bid_package_id={bid_package.id}")
    assert step_one.status_code == 200
    assert b"BP-21A" in step_one.data

    picker = auth_client.get(
        "/scopes/new/clauses?division_code=21&title=Scope+of+Work"
        "&exhibit_label=EXHIBIT+B&numbering_scheme=legal"
    )
    assert picker.status_code == 200
    prechecked = re.findall(rb'name="clause_ids" value="[^"]+"\s+checked', picker.data)
    assert len(prechecked) > 20, "the picker did not pre-select the library defaults"
    assert b"078413" in picker.data, "cross-referenced firestopping not offered"

    created = auth_client.post(
        "/scopes/new/clauses",
        data={
            "division_code": "21",
            "title": "Scope of Work",
            "exhibit_label": "EXHIBIT B",
            "numbering_scheme": "legal",
            "project_id": project.id,
            "bid_package_id": bid_package.id,
            "clause_ids": library_service.default_clause_ids(organization.id, "21"),
            "spec_section_ids": library_service.default_spec_section_ids(
                organization.id, "21"
            ),
            "enabled_sections": ["intent", "summary", "inclusions", "exclusions", "recap"],
            "base_bid_amount": "1425000",
        },
        follow_redirects=True,
    )
    assert created.status_code == 200
    assert b"Generated" in created.data

    from scopemaker.models import Scope

    record = db.session.query(Scope).order_by(Scope.created_at.desc()).first()
    assert record.item_count > 20
    assert record.project_id == project.id


def test_editor_shows_outline_labels_matching_the_export(auth_client, scope):
    response = auth_client.get(f"/scopes/{scope.id}")
    assert response.status_code == 200
    body = response.data.decode()
    assert 'class="item__label">1.' in body
    # Nested spec sections produce multi-level labels.
    assert 'class="item__label">3.1' in body


def test_add_edit_and_delete_an_item(auth_client, db, scope):
    section = scope.section("inclusions")
    before = len(section.items)

    auth_client.post(
        f"/scopes/{scope.id}/sections/inclusions/items",
        data={"text_html": "Provide temporary fire watch during hot work.",
              "submit": "Save"},
        follow_redirects=True,
    )
    db.session.refresh(section)
    assert len(section.items) == before + 1

    item = next(i for i in section.items if "fire watch" in i.text_html)
    auth_client.post(
        f"/scopes/{scope.id}/items/{item.id}",
        data={"text_html": "Provide a two-hour fire watch after all hot work.",
              "submit": "Save"},
        follow_redirects=True,
    )
    db.session.refresh(item)
    assert "two-hour" in item.text_html
    assert item.is_edited is True

    auth_client.post(f"/scopes/{scope.id}/items/{item.id}/delete", follow_redirects=True)
    db.session.refresh(section)
    assert len(section.items) == before


def test_reorder_persists_and_rejects_cycles(auth_client, db, scope):
    section = scope.section("inclusions")
    items = section.root_items[:3]
    reversed_order = [
        {"id": items[2].id, "parent_id": None},
        {"id": items[1].id, "parent_id": None},
        {"id": items[0].id, "parent_id": None},
    ]
    response = auth_client.post(
        f"/scopes/{scope.id}/items/reorder",
        json={"section_key": "inclusions", "order": reversed_order},
    )
    assert response.status_code == 200
    assert response.json["updated"] == 3

    db.session.refresh(section)
    assert section.root_items[0].id == items[2].id

    # An item cannot be made its own parent.
    cycle = auth_client.post(
        f"/scopes/{scope.id}/items/reorder",
        json={"section_key": "inclusions",
              "order": [{"id": items[0].id, "parent_id": items[0].id}]},
    )
    assert cycle.status_code == 200
    db.session.refresh(items[0])
    assert items[0].parent_id is None


def test_section_toggle(auth_client, db, scope):
    section = scope.section("safety")
    assert section.is_enabled is False
    auth_client.post(
        f"/scopes/{scope.id}/sections/safety/toggle", follow_redirects=True
    )
    db.session.refresh(section)
    assert section.is_enabled is True


def test_adding_the_same_clause_twice_is_a_no_op(auth_client, db, scope, organization):
    section = scope.section("inclusions")
    existing = next(i.source_clause_id for i in section.items if i.source_clause_id)
    before = len(section.items)

    auth_client.post(
        f"/scopes/{scope.id}/clauses",
        data={"section_key": "inclusions", "clause_ids": [existing],
              "submit": "Add selected clauses"},
        follow_redirects=True,
    )
    db.session.refresh(section)
    assert len(section.items) == before


def test_settings_update_changes_numbering(auth_client, db, scope, project):
    response = auth_client.post(
        f"/scopes/{scope.id}/settings",
        data={
            "title": "Scope of Work", "exhibit_label": "EXHIBIT C",
            "trade_name": "Fire Protection", "division_code": "21",
            "status": "in_review", "project_id": project.id, "bid_package_id": "",
            "numbering_scheme": "outline",
            "level1_style": "decimal", "level2_style": "upper-alpha",
            "level3_style": "lower-alpha-paren",
            "currency": "USD", "base_bid_amount": "1500000",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    db.session.refresh(scope)
    assert scope.exhibit_label == "EXHIBIT C"
    assert scope.status == "in_review"
    assert scope.settings["numbering_scheme"] == "outline"

    preview = auth_client.get(f"/scopes/{scope.id}/preview")
    assert b'class="doc__item-label">A.' in preview.data


def test_issue_then_revise_through_the_ui(auth_client, db, scope):
    issued = auth_client.post(
        f"/scopes/{scope.id}/issue", data={"note": "Sent 6 Aug", "submit": "Issue scope"},
        follow_redirects=True,
    )
    assert issued.status_code == 200
    db.session.refresh(scope)
    assert scope.status == "issued"
    assert b"locked" in issued.data

    revised = auth_client.post(f"/scopes/{scope.id}/revise", follow_redirects=True)
    assert revised.status_code == 200
    db.session.refresh(scope)
    assert scope.version == 2
    assert scope.status == "draft"


def test_export_downloads_have_descriptive_filenames(auth_client, scope):
    response = auth_client.get(f"/exports/{scope.id}.docx")
    assert response.status_code == 200
    disposition = response.headers["Content-Disposition"]
    assert "attachment" in disposition
    assert "bp-21a" in disposition.lower()
    assert disposition.endswith(".docx") or ".docx" in disposition


def test_pdf_is_served_inline_when_available(auth_client, scope):
    from scopemaker.services.renderers import PDF_AVAILABLE

    response = auth_client.get(f"/exports/{scope.id}.pdf")
    if PDF_AVAILABLE:
        assert response.status_code == 200
        assert response.mimetype == "application/pdf"
        assert response.headers["Content-Disposition"].startswith("inline")
    else:
        # Must fail with a clear message rather than an opaque crash.
        assert response.status_code == 500
        assert b"WeasyPrint" in response.data or b"not available" in response.data


def test_print_view_is_standalone(auth_client, scope):
    response = auth_client.get(f"/exports/{scope.id}/print")
    assert response.status_code == 200
    assert b"@page" in response.data
    assert b"<!DOCTYPE html>" in response.data


def test_project_and_package_crud(auth_client, db):
    created = auth_client.post(
        "/projects/new",
        data={"name": "Northside Depot", "number": "2026-002", "city": "Columbus",
              "state": "OH", "delivery_method": "GMP"},
        follow_redirects=True,
    )
    assert created.status_code == 200

    from scopemaker.models import Project

    project = db.session.query(Project).filter_by(number="2026-002").one()

    auth_client.post(
        f"/projects/{project.id}/packages/new",
        data={"number": "BP-09A", "name": "Drywall and Finishes",
              "division_code": "09", "trade_name": "Drywall"},
        follow_redirects=True,
    )
    db.session.refresh(project)
    assert project.bid_packages[0].division_code == "09"


def test_library_pages_render(auth_client):
    for path in ("/library/", "/library/spec-sections", "/library/templates",
                 "/library/clauses/new", "/library/spec-sections/new"):
        assert auth_client.get(path).status_code == 200, path


def test_custom_clause_creation_and_deletion(auth_client, db, organization):
    auth_client.post(
        "/library/clauses/new",
        data={"category": "exclusion", "division_code": "21",
              "text": "Excludes any work above the fourth floor after normal hours.",
              "is_active": "y", "position": "10"},
        follow_redirects=True,
    )
    from scopemaker.models import Clause

    clause = (
        db.session.query(Clause)
        .filter(Clause.organization_id == organization.id)
        .one()
    )
    assert clause.category == "exclusion"

    auth_client.post(f"/library/clauses/{clause.id}/delete", follow_redirects=True)
    assert db.session.query(Clause).filter_by(id=clause.id).count() == 0


def test_system_clause_edit_redirects_to_copy(auth_client, organization):
    clauses = library_service.available_clauses(organization.id, division_code="21")
    system = next(c for c in clauses if c.is_system)
    response = auth_client.get(f"/library/clauses/{system.id}/edit", follow_redirects=True)
    assert response.status_code == 200
    assert b"Copy clause" in response.data


def test_admin_can_invite_and_a_link_is_surfaced(auth_client, db, organization):
    response = auth_client.post(
        "/admin/invite",
        data={"email": "newhire@meridian.example", "role": "editor"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"/auth/invite/" in response.data

    from scopemaker.models import Invitation

    invitation = db.session.query(Invitation).one()
    assert invitation.role == "editor"
    assert invitation.is_usable


def test_last_admin_cannot_be_demoted(auth_client, db, organization, user):
    membership = user.memberships[0]
    response = auth_client.post(
        f"/admin/members/{membership.id}/role", data={"role": "viewer"},
        follow_redirects=True,
    )
    assert b"only administrator" in response.data
    db.session.refresh(membership)
    assert membership.role == "admin"


def test_api_token_is_shown_once(auth_client, db):
    response = auth_client.post(
        "/admin/tokens",
        data={"name": "CI pipeline", "scopes": "read", "expires_days": "30"},
        follow_redirects=True,
    )
    assert response.status_code == 200

    from scopemaker.models import ApiToken

    record = db.session.query(ApiToken).one()
    body = response.data.decode()
    secret = re.search(r"smk_[A-Za-z0-9_-]{20,}", body)
    assert secret, "the plaintext token was not displayed after creation"

    # Reloading shows only the non-secret prefix, never the full token again.
    reloaded = auth_client.get("/admin/tokens").data.decode()
    assert secret.group(0) not in reloaded
    assert record.token_prefix in reloaded


def test_save_scope_as_template(auth_client, db, scope):
    response = auth_client.post(
        f"/scopes/{scope.id}/template",
        data={"name": "Standard fire protection", "description": "Div 21 baseline"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    from scopemaker.models import ScopeTemplate

    template = db.session.query(ScopeTemplate).one()
    assert template.division_code == "21"
    assert template.payload["sections"]


def test_unknown_page_renders_the_404_template(auth_client):
    response = auth_client.get("/scopes/does-not-exist")
    assert response.status_code == 404
    assert b"404" in response.data


def test_the_scopes_list_does_not_scale_its_queries(auth_client, db, organization,
                                                    project, user):
    """A listing must cost the same whether it shows 3 documents or 30.

    Found by the PostgreSQL load test, which reported 32 queries for 25 scopes
    and 16 for 12. Each row lazily loaded its bid package; projects repeat and
    were answered from the identity map, but packages are distinct, so the
    count tracked the length of the list.

    Verified to fail without the fix: 10 queries for 3 scopes, 20 for 13.
    """
    from sqlalchemy import event

    from scopemaker.models import BidPackage
    from scopemaker.services import library as library_service
    from scopemaker.services.scope_builder import ScopeDraft, build_scope

    def make(count: int) -> None:
        for index in range(count):
            package = BidPackage(
                project_id=project.id,
                organization_id=organization.id,
                number=f"BP-Q{index:03d}",
                name=f"Query package {index}",
                division_code="23",
            )
            db.session.add(package)
            db.session.commit()
            build_scope(
                ScopeDraft(
                    organization_id=organization.id,
                    division_code="23",
                    project_id=project.id,
                    bid_package_id=package.id,
                    clause_ids=library_service.default_clause_ids(
                        organization.id, "23"
                    ),
                    spec_section_ids=[],
                    created_by_id=user.id,
                )
            )

    def count_queries() -> int:
        total = 0

        def bump(*args, **kwargs):
            nonlocal total
            total += 1

        # expire_all rather than remove: the fixtures stay attached to this
        # session, but every instance re-reads from the database, so a lazy
        # load in the template costs a query the way it would in production.
        db.session.expire_all()
        event.listen(db.engine, "before_cursor_execute", bump)
        try:
            assert auth_client.get("/scopes/").status_code == 200
        finally:
            event.remove(db.engine, "before_cursor_execute", bump)
        return total

    make(2)
    small = count_queries()
    make(10)
    large = count_queries()

    assert large <= small, (
        f"queries grew with the list: {small} for 3 scopes, {large} for 13. "
        "Something in the listing is lazy loading per row."
    )
