"""Manage the clause library, specification sections and templates."""

from __future__ import annotations

from flask import flash, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy import or_, select

from ...data.masterformat import get_division, normalize_code
from ...extensions import db
from ...models import Clause, ClauseSuppression, ScopeTemplate, SpecSection
from ...models.library import CLAUSE_CATEGORIES
from ...security import editor_required
from ...services import library as library_service
from ..helpers import (
    current_org_id,
    get_clause_or_404,
    get_spec_section_or_404,
    get_template_or_404,
)
from . import bp
from .forms import ClauseForm, SpecSectionForm, TemplateForm


@bp.route("/")
@login_required
def index():
    org_id = current_org_id()
    division = normalize_code(request.args.get("division"))
    category = request.args.get("category") or ""
    query = (request.args.get("q") or "").strip().lower()

    clauses = library_service.available_clauses(
        org_id,
        division_code=division,
        categories=[category] if category in CLAUSE_CATEGORIES else None,
        include_inactive=True,
    )
    if query:
        clauses = [c for c in clauses if query in c.text.lower()]

    return render_template(
        "library/index.html",
        clauses=clauses,
        division=division,
        division_obj=get_division(division),
        category=category,
        query=query,
        categories=CLAUSE_CATEGORIES,
        stats=library_service.library_stats(org_id),
        suppressed=library_service.suppressed_clause_ids(org_id),
    )


@bp.route("/clauses/new", methods=["GET", "POST"])
@login_required
@editor_required
def create_clause():
    form = ClauseForm()
    if request.method == "GET":
        form.division_code.data = request.args.get("division", "")
        form.category.data = request.args.get("category", "inclusion")
    if form.validate_on_submit():
        clause = Clause(
            organization_id=current_org_id(),
            category=form.category.data,
            division_code=normalize_code(form.division_code.data),
            text=" ".join((form.text.data or "").split()),
            is_default=form.is_default.data,
            is_active=form.is_active.data,
            position=form.position.data or 0,
            notes=form.notes.data or None,
        )
        db.session.add(clause)
        db.session.commit()
        flash("Clause added to your library.", "success")
        return redirect(url_for("library.index", division=clause.division_code or ""))
    return render_template("library/clause_form.html", form=form, clause=None)


@bp.route("/clauses/<clause_id>/edit", methods=["GET", "POST"])
@login_required
@editor_required
def edit_clause(clause_id: str):
    clause = get_clause_or_404(clause_id)
    if clause.is_system:
        flash(
            "Shipped clauses are shared by every organization and cannot be edited. "
            "Copy it to your library instead, then edit the copy.",
            "info",
        )
        return redirect(url_for("library.copy_clause", clause_id=clause.id))

    form = ClauseForm(obj=clause)
    if request.method == "GET":
        form.division_code.data = clause.division_code or ""
    if form.validate_on_submit():
        clause.category = form.category.data
        clause.division_code = normalize_code(form.division_code.data)
        clause.text = " ".join((form.text.data or "").split())
        clause.is_default = form.is_default.data
        clause.is_active = form.is_active.data
        clause.position = form.position.data or 0
        clause.notes = form.notes.data or None
        db.session.commit()
        flash("Clause updated.", "success")
        return redirect(url_for("library.index", division=clause.division_code or ""))
    return render_template("library/clause_form.html", form=form, clause=clause)


@bp.route("/clauses/<clause_id>/copy", methods=["GET", "POST"])
@login_required
@editor_required
def copy_clause(clause_id: str):
    """Fork a shipped clause into this organization's own library."""
    source = get_clause_or_404(clause_id)
    form = ClauseForm(
        data={
            "category": source.category,
            "division_code": source.division_code or "",
            "text": source.text,
            "is_default": source.is_default,
            "is_active": True,
            "position": source.position,
        }
    )
    if form.validate_on_submit():
        clause = Clause(
            organization_id=current_org_id(),
            category=form.category.data,
            division_code=normalize_code(form.division_code.data),
            text=" ".join((form.text.data or "").split()),
            is_default=form.is_default.data,
            is_active=form.is_active.data,
            position=form.position.data or 0,
            notes=form.notes.data or None,
        )
        db.session.add(clause)
        # Hide the original so the picker does not show near-identical twins.
        if source.is_system:
            db.session.add(
                ClauseSuppression(
                    organization_id=current_org_id(), clause_id=source.id
                )
            )
        db.session.commit()
        flash("Copied to your library. The shipped version has been hidden.", "success")
        return redirect(url_for("library.index", division=clause.division_code or ""))
    return render_template(
        "library/clause_form.html", form=form, clause=None, copying_from=source
    )


@bp.route("/clauses/<clause_id>/suppress", methods=["POST"])
@login_required
@editor_required
def toggle_suppression(clause_id: str):
    """Hide or restore a shipped clause for this organization."""
    clause = get_clause_or_404(clause_id)
    org_id = current_org_id()

    existing = db.session.scalar(
        select(ClauseSuppression).where(
            ClauseSuppression.organization_id == org_id,
            ClauseSuppression.clause_id == clause.id,
        )
    )
    if existing is not None:
        db.session.delete(existing)
        message = "Clause restored to your library."
    elif clause.is_system:
        db.session.add(ClauseSuppression(organization_id=org_id, clause_id=clause.id))
        message = "Clause hidden from your library."
    else:
        clause.is_active = not clause.is_active
        message = "Clause deactivated." if not clause.is_active else "Clause reactivated."
    db.session.commit()
    flash(message, "info")
    return redirect(request.referrer or url_for("library.index"))


@bp.route("/clauses/<clause_id>/delete", methods=["POST"])
@login_required
@editor_required
def delete_clause(clause_id: str):
    clause = get_clause_or_404(clause_id, allow_system=False)
    division = clause.division_code or ""
    db.session.delete(clause)
    db.session.commit()
    flash("Clause deleted.", "info")
    return redirect(url_for("library.index", division=division))


# ---------------------------------------------------------------------------
# Specification sections
# ---------------------------------------------------------------------------

@bp.route("/spec-sections")
@login_required
def spec_sections():
    org_id = current_org_id()
    division = normalize_code(request.args.get("division"))
    sections = library_service.available_spec_sections(
        org_id, division_code=division, include_inactive=True
    )
    return render_template(
        "library/spec_sections.html",
        sections=sections,
        division=division,
        division_obj=get_division(division),
    )


@bp.route("/spec-sections/new", methods=["GET", "POST"])
@login_required
@editor_required
def create_spec_section():
    form = SpecSectionForm()
    if request.method == "GET":
        form.division_code.data = request.args.get("division") or "01"
    if form.validate_on_submit():
        section = SpecSection(
            organization_id=current_org_id(),
            code=form.code.data.strip(),
            title=form.title.data.strip(),
            division_code=normalize_code(form.division_code.data),
            related_divisions=_parse_divisions(form.related_divisions.data),
            is_universal=form.is_universal.data,
            is_default=form.is_default.data,
            is_active=form.is_active.data,
            position=form.position.data or 0,
        )
        db.session.add(section)
        db.session.commit()
        flash(f"Added {section.display}.", "success")
        return redirect(url_for("library.spec_sections", division=section.division_code))
    return render_template("library/spec_form.html", form=form, section=None)


@bp.route("/spec-sections/<section_id>/edit", methods=["GET", "POST"])
@login_required
@editor_required
def edit_spec_section(section_id: str):
    section = get_spec_section_or_404(section_id)
    if section.is_system:
        flash("Shipped specification sections cannot be edited.", "info")
        return redirect(url_for("library.spec_sections", division=section.division_code))

    form = SpecSectionForm(obj=section)
    if request.method == "GET":
        form.related_divisions.data = ", ".join(section.related_divisions or [])
    if form.validate_on_submit():
        section.code = form.code.data.strip()
        section.title = form.title.data.strip()
        section.division_code = normalize_code(form.division_code.data)
        section.related_divisions = _parse_divisions(form.related_divisions.data)
        section.is_universal = form.is_universal.data
        section.is_default = form.is_default.data
        section.is_active = form.is_active.data
        section.position = form.position.data or 0
        db.session.commit()
        flash("Specification section updated.", "success")
        return redirect(url_for("library.spec_sections", division=section.division_code))
    return render_template("library/spec_form.html", form=form, section=section)


@bp.route("/spec-sections/<section_id>/delete", methods=["POST"])
@login_required
@editor_required
def delete_spec_section(section_id: str):
    section = get_spec_section_or_404(section_id, allow_system=False)
    division = section.division_code
    db.session.delete(section)
    db.session.commit()
    flash("Specification section deleted.", "info")
    return redirect(url_for("library.spec_sections", division=division))


def _parse_divisions(raw: str | None) -> list[str]:
    if not raw:
        return []
    codes = [normalize_code(part) for part in raw.replace(";", ",").split(",")]
    return [code for code in codes if code]


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

@bp.route("/templates")
@login_required
def templates():
    org_id = current_org_id()
    items = list(
        db.session.scalars(
            select(ScopeTemplate)
            .where(
                or_(
                    ScopeTemplate.organization_id == org_id,
                    ScopeTemplate.organization_id.is_(None),
                )
            )
            .order_by(ScopeTemplate.position, ScopeTemplate.name)
        )
    )
    return render_template("library/templates.html", templates=items)


@bp.route("/templates/<template_id>/edit", methods=["GET", "POST"])
@login_required
@editor_required
def edit_template(template_id: str):
    template = get_template_or_404(template_id)
    if template.is_system:
        flash("Shipped templates cannot be edited.", "info")
        return redirect(url_for("library.templates"))

    form = TemplateForm(obj=template)
    if request.method == "GET":
        form.division_code.data = template.division_code or ""
    if form.validate_on_submit():
        template.name = form.name.data
        template.description = form.description.data or None
        template.division_code = normalize_code(form.division_code.data)
        template.is_default = form.is_default.data
        template.is_active = form.is_active.data
        db.session.commit()
        flash("Template updated.", "success")
        return redirect(url_for("library.templates"))
    return render_template("library/template_form.html", form=form, template=template)


@bp.route("/templates/<template_id>/delete", methods=["POST"])
@login_required
@editor_required
def delete_template(template_id: str):
    template = get_template_or_404(template_id, allow_system=False)
    db.session.delete(template)
    db.session.commit()
    flash("Template deleted.", "info")
    return redirect(url_for("library.templates"))
