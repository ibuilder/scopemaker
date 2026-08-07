"""Creating, editing, previewing and issuing scopes."""

from __future__ import annotations

from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import or_, select

from ...data.masterformat import get_division, normalize_code
from ...extensions import db
from ...models import BidPackage, Project, Scope, ScopeItem, ScopeSection, ScopeTemplate
from ...models.scope import DEFAULT_SECTIONS, SCOPE_STATUSES, STATUS_LABELS
from ...security import editor_required
from ...services import audit, scope_builder
from ...services import library as library_service
from ...services.numbering import build_numberer
from ...services.renderers import PDF_AVAILABLE, render_html
from ...services.sanitize import sanitize_html, sanitize_inline
from ..helpers import (
    current_org_id,
    get_scope_or_404,
    require_unlocked,
)
from . import bp
from .forms import (
    AddClausesForm,
    ItemForm,
    RevisionNoteForm,
    SaveTemplateForm,
    ScopeGenerateForm,
    ScopeSettingsForm,
    ScopeStartForm,
    SectionForm,
    parse_money,
)


def _projects(org_id: str) -> list[Project]:
    return list(
        db.session.scalars(
            select(Project)
            .where(Project.organization_id == org_id, Project.is_archived.is_(False))
            .order_by(Project.name)
        )
    )


def _packages(org_id: str, project_id: str | None = None) -> list[BidPackage]:
    stmt = select(BidPackage).where(BidPackage.organization_id == org_id)
    if project_id:
        stmt = stmt.where(BidPackage.project_id == project_id)
    return list(db.session.scalars(stmt.order_by(BidPackage.number)))


def _templates(org_id: str, division_code: str | None = None) -> list[ScopeTemplate]:
    stmt = select(ScopeTemplate).where(
        or_(
            ScopeTemplate.organization_id == org_id,
            ScopeTemplate.organization_id.is_(None),
        ),
        ScopeTemplate.is_active.is_(True),
    )
    if division_code:
        stmt = stmt.where(
            or_(
                ScopeTemplate.division_code == division_code,
                ScopeTemplate.division_code.is_(None),
            )
        )
    return list(db.session.scalars(stmt.order_by(ScopeTemplate.position, ScopeTemplate.name)))


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

@bp.route("/")
@login_required
def index():
    org_id = current_org_id()
    status = request.args.get("status") or ""
    division = normalize_code(request.args.get("division"))
    query = (request.args.get("q") or "").strip()

    stmt = select(Scope).where(Scope.organization_id == org_id)
    if status in SCOPE_STATUSES:
        stmt = stmt.where(Scope.status == status)
    if division:
        stmt = stmt.where(Scope.division_code == division)
    if query:
        pattern = f"%{query}%"
        stmt = stmt.where(
            or_(Scope.title.ilike(pattern), Scope.trade_name.ilike(pattern))
        )

    scopes = list(db.session.scalars(stmt.order_by(Scope.updated_at.desc()).limit(200)))
    return render_template(
        "scopes/index.html",
        scopes=scopes,
        status=status,
        division=division,
        query=query,
        statuses=[(s, STATUS_LABELS[s]) for s in SCOPE_STATUSES],
    )


# ---------------------------------------------------------------------------
# Creation wizard
# ---------------------------------------------------------------------------

@bp.route("/new", methods=["GET", "POST"])
@login_required
@editor_required
def new():
    """Step 1 -- identify the package."""
    org_id = current_org_id()
    projects = _projects(org_id)
    packages = _packages(org_id)
    form = ScopeStartForm(projects=projects, packages=packages, templates=_templates(org_id))

    # Pre-fill from a bid package when the user arrived from a project page.
    package_id = request.args.get("bid_package_id")
    if request.method == "GET" and package_id:
        package = db.session.get(BidPackage, package_id)
        if package is not None and package.organization_id == org_id:
            form.bid_package_id.data = package.id
            form.project_id.data = package.project_id
            form.division_code.data = package.division_code or ""
            form.trade_name.data = package.trade_name or ""

    if form.validate_on_submit():
        return redirect(
            url_for(
                "scopes.select_clauses",
                division_code=form.division_code.data,
                trade_name=form.trade_name.data or "",
                project_id=form.project_id.data or "",
                bid_package_id=form.bid_package_id.data or "",
                exhibit_label=form.exhibit_label.data,
                title=form.title.data,
                numbering_scheme=form.numbering_scheme.data,
                template_id=form.template_id.data or "",
            )
        )

    return render_template("scopes/new.html", form=form)


@bp.route("/new/clauses", methods=["GET", "POST"])
@login_required
@editor_required
def select_clauses():
    """Step 2 -- pick the clauses and specification sections."""
    org_id = current_org_id()
    source = request.form if request.method == "POST" else request.args

    division_code = normalize_code(source.get("division_code"))
    if not division_code:
        flash("Choose a CSI division to continue.", "error")
        return redirect(url_for("scopes.new"))

    division = get_division(division_code)
    form = ScopeGenerateForm(formdata=request.form if request.method == "POST" else None)

    # Carry step 1's answers through the hidden fields.
    for field in ("project_id", "bid_package_id", "trade_name", "exhibit_label",
                  "title", "numbering_scheme", "template_id"):
        if request.method == "GET":
            getattr(form, field).data = source.get(field, "")
    form.division_code.data = division_code

    grouped = library_service.clauses_by_category(org_id, division_code=division_code)
    spec_sections = library_service.available_spec_sections(
        org_id, division_code=division_code
    )

    if request.method == "GET":
        form.clause_ids.data = library_service.default_clause_ids(org_id, division_code)
        form.spec_section_ids.data = library_service.default_spec_section_ids(
            org_id, division_code
        )
        form.enabled_sections.data = [
            s["key"] for s in DEFAULT_SECTIONS if s["enabled"]
        ]

    if request.method == "POST" and form.validate_on_submit():
        draft = scope_builder.ScopeDraft(
            organization_id=org_id,
            division_code=division_code,
            trade_name=form.trade_name.data or None,
            title=form.title.data or "Scope of Work",
            exhibit_label=form.exhibit_label.data or "EXHIBIT B",
            project_id=form.project_id.data or None,
            bid_package_id=form.bid_package_id.data or None,
            clause_ids=list(form.clause_ids.data or []),
            spec_section_ids=list(form.spec_section_ids.data or []),
            enabled_sections=list(form.enabled_sections.data or []) or None,
            numbering_scheme=form.numbering_scheme.data or "legal",
            template_id=form.template_id.data or None,
            created_by_id=current_user.id,
            base_bid_amount=parse_money(form.base_bid_amount.data),
        )
        scope = scope_builder.build_scope(draft)
        flash(
            f"Generated {scope.document_title} with {scope.item_count} items. "
            "Edit anything you need to before exporting.",
            "success",
        )
        return redirect(url_for("scopes.edit", scope_id=scope.id))

    return render_template(
        "scopes/select_clauses.html",
        form=form,
        division=division,
        grouped_clauses=grouped,
        spec_sections=spec_sections,
    )


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------

@bp.route("/<scope_id>")
@login_required
def edit(scope_id: str):
    scope = get_scope_or_404(scope_id)
    org_id = current_org_id()

    # Number every section -- including the switched-off ones, which the editor
    # still lists so they can be turned back on -- using the same Numberer the
    # renderers use, so the labels beside each row match the exported document.
    numberer = build_numberer(scope)
    outlines = {
        section.key: numberer.walk(section.root_items) for section in scope.sections
    }

    return render_template(
        "scopes/edit.html",
        scope=scope,
        outlines=outlines,
        add_form=AddClausesForm(),
        item_form=ItemForm(),
        grouped_clauses=library_service.clauses_by_category(
            org_id, division_code=scope.division_code
        ),
        preview_html=render_html(
            scope, organization=current_user.active_organization, standalone=False
        ),
        pdf_available=PDF_AVAILABLE,
    )


@bp.route("/<scope_id>/preview")
@login_required
def preview(scope_id: str):
    """Full-page preview -- the same HTML the PDF renderer consumes."""
    scope = get_scope_or_404(scope_id)
    return render_template(
        "scopes/preview.html",
        scope=scope,
        preview_html=render_html(
            scope, organization=current_user.active_organization, standalone=False
        ),
        pdf_available=PDF_AVAILABLE,
    )


@bp.route("/<scope_id>/settings", methods=["GET", "POST"])
@login_required
@editor_required
def settings(scope_id: str):
    scope = get_scope_or_404(scope_id)
    org_id = current_org_id()
    projects = _projects(org_id)
    form = ScopeSettingsForm(
        obj=scope, projects=projects, packages=_packages(org_id)
    )

    if request.method == "GET":
        styles = list(scope.numbering_style or [])
        form.numbering_scheme.data = (scope.settings or {}).get("numbering_scheme", "legal")
        for index, field in enumerate(
            (form.level1_style, form.level2_style, form.level3_style)
        ):
            if index < len(styles):
                field.data = styles[index]
        form.project_id.data = scope.project_id or ""
        form.bid_package_id.data = scope.bid_package_id or ""
        form.division_code.data = scope.division_code or ""

    if form.validate_on_submit():
        require_unlocked(scope)
        scope.title = form.title.data
        scope.exhibit_label = form.exhibit_label.data
        scope.trade_name = form.trade_name.data or None
        scope.division_code = normalize_code(form.division_code.data)
        scope.status = form.status.data
        scope.project_id = form.project_id.data or None
        scope.bid_package_id = form.bid_package_id.data or None
        scope.currency = (form.currency.data or "USD").upper()
        scope.base_bid_amount = form.base_bid_amount.data
        scope.alternates_amount = form.alternates_amount.data
        scope.adjustments_amount = form.adjustments_amount.data
        scope.numbering_style = [
            form.level1_style.data, form.level2_style.data, form.level3_style.data
        ]
        scope.settings = {
            **(scope.settings or {}),
            "numbering_scheme": form.numbering_scheme.data,
        }
        scope.updated_by_id = current_user.id
        db.session.commit()
        flash("Scope settings saved.", "success")
        return redirect(url_for("scopes.edit", scope_id=scope.id))

    return render_template("scopes/settings.html", scope=scope, form=form)


@bp.route("/<scope_id>/sections/<section_key>", methods=["POST"])
@login_required
@editor_required
def update_section(scope_id: str, section_key: str):
    scope = get_scope_or_404(scope_id)
    require_unlocked(scope)
    section = scope.section(section_key)
    if section is None:
        flash("That section does not exist on this scope.", "error")
        return redirect(url_for("scopes.edit", scope_id=scope.id))

    form = SectionForm()
    if form.validate_on_submit():
        section.heading = form.heading.data
        section.body_html = str(sanitize_html(form.body_html.data))
        section.is_enabled = form.is_enabled.data
        scope.updated_by_id = current_user.id
        db.session.commit()
        flash(f"Updated “{section.heading}”.", "success")
    else:
        flash("The section could not be saved.", "error")
    return redirect(url_for("scopes.edit", scope_id=scope.id) + f"#section-{section_key}")


@bp.route("/<scope_id>/sections/<section_key>/toggle", methods=["POST"])
@login_required
@editor_required
def toggle_section(scope_id: str, section_key: str):
    scope = get_scope_or_404(scope_id)
    require_unlocked(scope)
    section = scope.section(section_key)
    if section is not None:
        section.is_enabled = not section.is_enabled
        scope.updated_by_id = current_user.id
        db.session.commit()
    return redirect(url_for("scopes.edit", scope_id=scope.id) + f"#section-{section_key}")


@bp.route("/<scope_id>/sections/<section_key>/items", methods=["POST"])
@login_required
@editor_required
def add_item(scope_id: str, section_key: str):
    scope = get_scope_or_404(scope_id)
    require_unlocked(scope)
    section = scope.section(section_key)
    if section is None:
        flash("That section does not exist on this scope.", "error")
        return redirect(url_for("scopes.edit", scope_id=scope.id))

    form = ItemForm()
    if form.validate_on_submit():
        parent_id = form.parent_id.data or None
        if parent_id and not _item_belongs_to(section, parent_id):
            parent_id = None
        siblings = [i for i in section.items if i.parent_id == parent_id]
        db.session.add(
            ScopeItem(
                section_id=section.id,
                parent_id=parent_id,
                text_html=str(sanitize_inline(form.text_html.data)),
                position=max((i.position for i in siblings), default=-10) + 10,
                is_edited=True,
                meta={"role": "custom"},
            )
        )
        scope.updated_by_id = current_user.id
        db.session.commit()
        flash("Item added.", "success")
    else:
        flash("Enter some text for the new item.", "error")
    return redirect(url_for("scopes.edit", scope_id=scope.id) + f"#section-{section_key}")


def _item_belongs_to(section: ScopeSection, item_id: str) -> bool:
    return any(i.id == item_id for i in section.items)


def _get_item(scope: Scope, item_id: str) -> ScopeItem | None:
    """Fetch an item, confirming it belongs to this scope."""
    item = db.session.get(ScopeItem, item_id)
    if item is None:
        return None
    section = db.session.get(ScopeSection, item.section_id)
    if section is None or section.scope_id != scope.id:
        return None
    return item


@bp.route("/<scope_id>/items/<item_id>", methods=["POST"])
@login_required
@editor_required
def update_item(scope_id: str, item_id: str):
    scope = get_scope_or_404(scope_id)
    require_unlocked(scope)
    item = _get_item(scope, item_id)
    if item is None:
        return jsonify({"error": {"code": "not_found", "message": "No such item."}}), 404

    form = ItemForm()
    if not form.validate_on_submit():
        flash("The item text cannot be empty.", "error")
        return redirect(url_for("scopes.edit", scope_id=scope.id))

    cleaned = str(sanitize_inline(form.text_html.data))
    if cleaned != item.text_html:
        item.text_html = cleaned
        # Mark drift from the library so a report can show how far a scope has
        # been customised away from the company standard.
        item.is_edited = True
    scope.updated_by_id = current_user.id
    db.session.commit()
    flash("Item updated.", "success")
    section = db.session.get(ScopeSection, item.section_id)
    anchor = f"#section-{section.key}" if section else ""
    return redirect(url_for("scopes.edit", scope_id=scope.id) + anchor)


@bp.route("/<scope_id>/items/<item_id>/delete", methods=["POST"])
@login_required
@editor_required
def delete_item(scope_id: str, item_id: str):
    scope = get_scope_or_404(scope_id)
    require_unlocked(scope)
    item = _get_item(scope, item_id)
    if item is None:
        flash("That item no longer exists.", "error")
        return redirect(url_for("scopes.edit", scope_id=scope.id))

    section = db.session.get(ScopeSection, item.section_id)
    anchor = f"#section-{section.key}" if section else ""
    db.session.delete(item)
    scope.updated_by_id = current_user.id
    db.session.commit()
    flash("Item removed.", "info")
    return redirect(url_for("scopes.edit", scope_id=scope.id) + anchor)


@bp.route("/<scope_id>/items/reorder", methods=["POST"])
@login_required
@editor_required
def reorder_items(scope_id: str):
    """Persist a drag-and-drop reorder.

    Payload: ``{"section_key": "...", "order": [{"id":..., "parent_id":...}, ...]}``
    in the new visual sequence.
    """
    scope = get_scope_or_404(scope_id)
    require_unlocked(scope)
    payload = request.get_json(silent=True) or {}
    section = scope.section(payload.get("section_key", ""))
    if section is None:
        return jsonify({"error": {"code": "not_found", "message": "No such section."}}), 404

    by_id = {i.id: i for i in section.items}
    counters: dict[str | None, int] = {}
    updated = 0

    for entry in payload.get("order") or []:
        item = by_id.get(entry.get("id"))
        if item is None:
            continue
        parent_id = entry.get("parent_id") or None
        if parent_id is not None and parent_id not in by_id:
            parent_id = None
        if parent_id == item.id:
            parent_id = None  # an item cannot parent itself
        if parent_id and _creates_cycle(by_id, item.id, parent_id):
            continue
        item.parent_id = parent_id
        counters[parent_id] = counters.get(parent_id, -10) + 10
        item.position = counters[parent_id]
        updated += 1

    scope.updated_by_id = current_user.id
    db.session.commit()
    return jsonify({"status": "ok", "updated": updated})


def _creates_cycle(by_id: dict[str, ScopeItem], item_id: str, parent_id: str) -> bool:
    """True when re-parenting would make an item its own ancestor."""
    seen: set[str] = set()
    cursor: str | None = parent_id
    while cursor:
        if cursor == item_id:
            return True
        if cursor in seen:
            return True
        seen.add(cursor)
        parent = by_id.get(cursor)
        cursor = parent.parent_id if parent else None
    return False


@bp.route("/<scope_id>/clauses", methods=["POST"])
@login_required
@editor_required
def add_clauses(scope_id: str):
    scope = get_scope_or_404(scope_id)
    require_unlocked(scope)
    form = AddClausesForm()
    if not form.validate_on_submit():
        flash("Select at least one clause to add.", "error")
        return redirect(url_for("scopes.edit", scope_id=scope.id))

    clauses = library_service.get_clauses(
        current_org_id(), list(form.clause_ids.data or [])
    )
    added = scope_builder.add_clauses_to_scope(scope, form.section_key.data, clauses)
    scope.updated_by_id = current_user.id
    db.session.commit()

    skipped = len(clauses) - added
    message = f"Added {added} clause{'s' if added != 1 else ''}."
    if skipped:
        message += f" {skipped} already on the scope."
    flash(message, "success" if added else "info")
    return redirect(
        url_for("scopes.edit", scope_id=scope.id) + f"#section-{form.section_key.data}"
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@bp.route("/<scope_id>/issue", methods=["GET", "POST"])
@login_required
@editor_required
def issue(scope_id: str):
    scope = get_scope_or_404(scope_id)
    form = RevisionNoteForm()
    if form.validate_on_submit():
        if scope.is_locked:
            flash("This scope has already been issued.", "info")
        else:
            scope_builder.issue_scope(scope, user_id=current_user.id, note=form.note.data)
            audit.record(
                audit.AuditAction.SCOPE_ISSUED,
                summary=f"{scope.document_title} issued as version {scope.version}",
                target_type="scope", target_id=scope.id,
                target_label=scope.document_title,
                context={"version": scope.version, "note": form.note.data or None},
                commit=True,
            )
            flash(
                f"{scope.document_title} issued as version {scope.version} and locked. "
                "Create a revision to make further changes.",
                "success",
            )
        return redirect(url_for("scopes.edit", scope_id=scope.id))
    return render_template("scopes/issue.html", scope=scope, form=form)


@bp.route("/<scope_id>/revise", methods=["POST"])
@login_required
@editor_required
def revise(scope_id: str):
    scope = get_scope_or_404(scope_id)
    if not scope.is_locked:
        flash("This scope is already editable.", "info")
    else:
        scope_builder.revise_scope(scope, user_id=current_user.id)
        audit.record(
            audit.AuditAction.SCOPE_REVISED,
            summary=f"{scope.document_title} reopened as version {scope.version}",
            target_type="scope", target_id=scope.id,
            target_label=scope.document_title,
            context={"version": scope.version}, commit=True,
        )
        flash(f"Now editing version {scope.version}.", "success")
    return redirect(url_for("scopes.edit", scope_id=scope.id))


@bp.route("/<scope_id>/duplicate", methods=["POST"])
@login_required
@editor_required
def duplicate(scope_id: str):
    scope = get_scope_or_404(scope_id)
    clone = scope_builder.duplicate_scope(scope, user_id=current_user.id)
    flash("Scope duplicated.", "success")
    return redirect(url_for("scopes.edit", scope_id=clone.id))


@bp.route("/<scope_id>/archive", methods=["POST"])
@login_required
@editor_required
def archive(scope_id: str):
    scope = get_scope_or_404(scope_id)
    scope.status = "archived"
    scope.updated_by_id = current_user.id
    audit.record(
        audit.AuditAction.SCOPE_ARCHIVED,
        summary=f"{scope.document_title} archived",
        target_type="scope", target_id=scope.id, target_label=scope.document_title,
    )
    db.session.commit()
    flash("Scope archived.", "info")
    return redirect(url_for("scopes.index"))


@bp.route("/<scope_id>/revisions")
@login_required
def revisions(scope_id: str):
    scope = get_scope_or_404(scope_id)
    return render_template("scopes/revisions.html", scope=scope)


@bp.route("/<scope_id>/template", methods=["GET", "POST"])
@login_required
@editor_required
def save_template(scope_id: str):
    scope = get_scope_or_404(scope_id)
    form = SaveTemplateForm()
    if form.validate_on_submit():
        template = scope_builder.save_as_template(
            scope,
            name=form.name.data,
            description=form.description.data,
            user_id=current_user.id,
        )
        flash(f"Saved “{template.name}” as a reusable template.", "success")
        return redirect(url_for("library.templates"))
    form.name.data = form.name.data or f"{scope.trade_name or 'Standard'} scope"
    return render_template("scopes/save_template.html", scope=scope, form=form)


# ---------------------------------------------------------------------------
# Small JSON helpers used by the wizard
# ---------------------------------------------------------------------------

@bp.route("/api/bid-packages")
@login_required
def bid_packages_for_project():
    """Bid packages for a project, for the wizard's dependent dropdown."""
    org_id = current_org_id()
    project_id = request.args.get("project_id") or None
    packages = _packages(org_id, project_id)
    return jsonify(
        {
            "bid_packages": [
                {
                    "id": p.id,
                    "label": p.display_title,
                    "division_code": p.division_code,
                    "trade_name": p.trade_name,
                }
                for p in packages
            ]
        }
    )


@bp.route("/api/division/<division_code>/defaults")
@login_required
def division_defaults(division_code: str):
    """Default clause and specification selections for a division."""
    org_id = current_org_id()
    code = normalize_code(division_code)
    division = get_division(code)
    return jsonify(
        {
            "division": {"code": code, "title": division.title if division else None},
            "trades": list(division.trades) if division else [],
            "clause_ids": library_service.default_clause_ids(org_id, code),
            "spec_section_ids": library_service.default_spec_section_ids(org_id, code),
        }
    )
