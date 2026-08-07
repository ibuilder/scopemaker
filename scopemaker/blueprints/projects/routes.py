"""Projects and bid packages."""

from __future__ import annotations

from flask import flash, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy import or_, select

from ...data.masterformat import normalize_code
from ...extensions import db
from ...models import BidPackage, Project, Scope
from ...security import editor_required
from ..helpers import current_org_id, get_bid_package_or_404, get_project_or_404
from . import bp
from .forms import BidPackageForm, ProjectForm


@bp.route("/")
@login_required
def index():
    org_id = current_org_id()
    query = (request.args.get("q") or "").strip()
    show_archived = request.args.get("archived") == "1"

    stmt = select(Project).where(Project.organization_id == org_id)
    if not show_archived:
        stmt = stmt.where(Project.is_archived.is_(False))
    if query:
        pattern = f"%{query}%"
        stmt = stmt.where(or_(Project.name.ilike(pattern), Project.number.ilike(pattern)))

    projects = list(db.session.scalars(stmt.order_by(Project.name)))
    return render_template(
        "projects/index.html", projects=projects, query=query, show_archived=show_archived
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
@editor_required
def create():
    form = ProjectForm()
    if form.validate_on_submit():
        project = Project(organization_id=current_org_id())
        form.populate_obj(project)
        project.delivery_method = form.delivery_method.data or None
        db.session.add(project)
        db.session.commit()
        flash(f"Created {project.display_title}.", "success")
        return redirect(url_for("projects.detail", project_id=project.id))
    return render_template("projects/form.html", form=form, project=None)


@bp.route("/<project_id>")
@login_required
def detail(project_id: str):
    project = get_project_or_404(project_id)
    scopes = list(
        db.session.scalars(
            select(Scope)
            .where(Scope.project_id == project.id)
            .order_by(Scope.updated_at.desc())
        )
    )
    return render_template("projects/detail.html", project=project, scopes=scopes)


@bp.route("/<project_id>/edit", methods=["GET", "POST"])
@login_required
@editor_required
def edit(project_id: str):
    project = get_project_or_404(project_id)
    form = ProjectForm(obj=project)
    if form.validate_on_submit():
        form.populate_obj(project)
        project.delivery_method = form.delivery_method.data or None
        db.session.commit()
        flash("Project updated.", "success")
        return redirect(url_for("projects.detail", project_id=project.id))
    return render_template("projects/form.html", form=form, project=project)


@bp.route("/<project_id>/archive", methods=["POST"])
@login_required
@editor_required
def archive(project_id: str):
    project = get_project_or_404(project_id)
    project.is_archived = not project.is_archived
    db.session.commit()
    flash(
        f"{project.display_title} {'archived' if project.is_archived else 'restored'}.",
        "info",
    )
    return redirect(url_for("projects.index"))


# ---------------------------------------------------------------------------
# Bid packages
# ---------------------------------------------------------------------------

@bp.route("/<project_id>/packages/new", methods=["GET", "POST"])
@login_required
@editor_required
def create_package(project_id: str):
    project = get_project_or_404(project_id)
    form = BidPackageForm()
    if form.validate_on_submit():
        package = BidPackage(
            project_id=project.id, organization_id=project.organization_id
        )
        form.populate_obj(package)
        package.division_code = normalize_code(form.division_code.data)
        db.session.add(package)
        db.session.commit()
        flash(f"Added {package.display_title}.", "success")
        return redirect(url_for("projects.detail", project_id=project.id))
    return render_template(
        "projects/package_form.html", form=form, project=project, package=None
    )


@bp.route("/packages/<package_id>/edit", methods=["GET", "POST"])
@login_required
@editor_required
def edit_package(package_id: str):
    package = get_bid_package_or_404(package_id)
    form = BidPackageForm(obj=package)
    if form.validate_on_submit():
        form.populate_obj(package)
        package.division_code = normalize_code(form.division_code.data)
        db.session.commit()
        flash("Bid package updated.", "success")
        return redirect(url_for("projects.detail", project_id=package.project_id))
    return render_template(
        "projects/package_form.html", form=form, project=package.project, package=package
    )


@bp.route("/packages/<package_id>/delete", methods=["POST"])
@login_required
@editor_required
def delete_package(package_id: str):
    package = get_bid_package_or_404(package_id)
    project_id = package.project_id
    if package.scopes:
        flash(
            "That bid package still has scopes attached. Reassign them first.", "error"
        )
    else:
        db.session.delete(package)
        db.session.commit()
        flash("Bid package removed.", "info")
    return redirect(url_for("projects.detail", project_id=project_id))
