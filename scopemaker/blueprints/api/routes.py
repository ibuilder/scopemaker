"""JSON API v1.

Errors use the same envelope as the rest of the app:
``{"error": {"code": ..., "message": ..., "details": ...}}``.
"""

from __future__ import annotations

from flask import Response, g, jsonify, request, url_for
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select

from ... import __version__
from ...data.masterformat import DIVISIONS, divisions_by_subgroup, normalize_code
from ...errors import NotFoundError, ValidationError
from ...extensions import db, limiter
from ...models import BidPackage, Project, Scope
from ...models.library import CLAUSE_CATEGORIES
from ...models.scope import DEFAULT_SECTIONS, SCOPE_STATUSES
from ...services import coverage as coverage_service
from ...services import library as library_service
from ...services import scope_builder
from ...services.renderers import (
    FORMATS,
    render_docx,
    render_html,
    render_json,
    render_markdown,
    render_pdf,
)
from ...services.renderers.json_export import build_payload
from . import bp
from .auth import api_auth, api_org_id
from .schemas import (
    BidPackageCreateRequest,
    ProjectCreateRequest,
    ScopeCreateRequest,
    ScopeUpdateRequest,
)

RENDERERS = {
    "pdf": render_pdf,
    "docx": render_docx,
    "json": render_json,
    "md": render_markdown,
}


def _body(model):
    """Parse and validate the JSON body, raising a 422 with field detail."""
    payload = request.get_json(silent=True)
    if payload is None:
        raise ValidationError("A JSON request body is required.")
    try:
        return model.model_validate(payload)
    except PydanticValidationError as exc:
        raise ValidationError(
            "The request body is invalid.",
            details={
                "fields": [
                    {
                        "field": ".".join(str(p) for p in error["loc"]),
                        "message": error["msg"],
                    }
                    for error in exc.errors()
                ]
            },
        ) from exc


def _scope_or_404(scope_id: str) -> Scope:
    scope = db.session.get(Scope, scope_id)
    if scope is None or scope.organization_id != api_org_id():
        raise NotFoundError("No scope with that id.")
    return scope


def _scope_summary(scope: Scope) -> dict:
    return {
        "id": scope.id,
        "title": scope.title,
        "document_title": scope.document_title,
        "exhibit_label": scope.exhibit_label,
        "division_code": scope.division_code,
        "trade_name": scope.trade_name,
        "status": scope.status,
        "version": scope.version,
        "item_count": scope.item_count,
        "project_id": scope.project_id,
        "bid_package_id": scope.bid_package_id,
        "updated_at": scope.updated_at.isoformat() if scope.updated_at else None,
        "links": {
            "self": url_for("api.get_scope", scope_id=scope.id, _external=False),
            "html": url_for("api.export_scope", scope_id=scope.id, format_key="html"),
            "pdf": url_for("api.export_scope", scope_id=scope.id, format_key="pdf"),
            "docx": url_for("api.export_scope", scope_id=scope.id, format_key="docx"),
        },
    }


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------

@bp.route("/")
def root():
    """Unauthenticated index describing the API."""
    return jsonify(
        {
            "name": "ScopeMaker API",
            "version": "v1",
            "app_version": __version__,
            "authentication": "Authorization: Bearer smk_...",
            "endpoints": {
                "divisions": url_for("api.list_divisions"),
                "clauses": url_for("api.list_clauses"),
                "spec_sections": url_for("api.list_spec_sections"),
                "scopes": url_for("api.list_scopes"),
                "projects": url_for("api.list_projects"),
                "coverage": "/api/v1/projects/{project_id}/coverage",
            },
        }
    )


@bp.route("/me")
@api_auth()
def me():
    user = g.api_user
    return jsonify(
        {
            "user": {"id": user.id, "email": user.email, "name": user.full_name},
            "organization_id": api_org_id(),
            "scopes": sorted(g.api_scopes),
            "token": None if g.api_token is None else {"name": g.api_token.name},
        }
    )


@bp.route("/divisions")
def list_divisions():
    """The canonical MasterFormat divisions this app will accept."""
    return jsonify(
        {
            "divisions": [
                {
                    "code": d.code,
                    "title": d.title,
                    "subgroup": d.subgroup,
                    "trades": list(d.trades),
                }
                for d in DIVISIONS
            ],
            "grouped": [
                {"subgroup": name, "codes": [d.code for d in items]}
                for name, items in divisions_by_subgroup()
            ],
            "section_keys": [s["key"] for s in DEFAULT_SECTIONS],
            "clause_categories": CLAUSE_CATEGORIES,
            "statuses": list(SCOPE_STATUSES),
        }
    )


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------

@bp.route("/library/clauses")
@api_auth()
def list_clauses():
    division = normalize_code(request.args.get("division"))
    category = request.args.get("category")
    clauses = library_service.available_clauses(
        api_org_id(),
        division_code=division,
        categories=[category] if category in CLAUSE_CATEGORIES else None,
    )
    return jsonify(
        {
            "count": len(clauses),
            "clauses": [
                {
                    "id": c.id,
                    "category": c.category,
                    "division_code": c.division_code,
                    "text": c.text,
                    "is_default": c.is_default,
                    "is_system": c.is_system,
                    "system_key": c.system_key,
                }
                for c in clauses
            ],
        }
    )


@bp.route("/library/spec-sections")
@api_auth()
def list_spec_sections():
    division = normalize_code(request.args.get("division"))
    sections = library_service.available_spec_sections(
        api_org_id(), division_code=division
    )
    return jsonify(
        {
            "count": len(sections),
            "spec_sections": [
                {
                    "id": s.id,
                    "code": s.code,
                    "title": s.title,
                    "division_code": s.division_code,
                    "related_divisions": s.related_divisions,
                    "is_universal": s.is_universal,
                    "is_default": s.is_default,
                }
                for s in sections
            ],
        }
    )


# ---------------------------------------------------------------------------
# Scopes
# ---------------------------------------------------------------------------

@bp.route("/scopes")
@api_auth()
def list_scopes():
    org_id = api_org_id()
    stmt = select(Scope).where(Scope.organization_id == org_id)

    status = request.args.get("status")
    if status:
        if status not in SCOPE_STATUSES:
            raise ValidationError(f"status must be one of: {', '.join(SCOPE_STATUSES)}")
        stmt = stmt.where(Scope.status == status)

    division = normalize_code(request.args.get("division"))
    if division:
        stmt = stmt.where(Scope.division_code == division)

    project_id = request.args.get("project_id")
    if project_id:
        stmt = stmt.where(Scope.project_id == project_id)

    limit = min(int(request.args.get("limit", 50) or 50), 200)
    offset = max(int(request.args.get("offset", 0) or 0), 0)

    scopes = list(
        db.session.scalars(
            stmt.order_by(Scope.updated_at.desc()).limit(limit).offset(offset)
        )
    )
    return jsonify(
        {
            "count": len(scopes),
            "limit": limit,
            "offset": offset,
            "scopes": [_scope_summary(s) for s in scopes],
        }
    )


@bp.route("/scopes", methods=["POST"])
@api_auth(write=True)
@limiter.limit("60 per hour")
def create_scope():
    payload = _body(ScopeCreateRequest)
    org_id = api_org_id()

    clause_ids = list(payload.clause_ids)
    spec_ids = list(payload.spec_section_ids)
    if payload.use_defaults or not clause_ids:
        clause_ids = library_service.default_clause_ids(org_id, payload.division_code)
    if payload.use_defaults or not spec_ids:
        spec_ids = library_service.default_spec_section_ids(org_id, payload.division_code)

    draft = scope_builder.ScopeDraft(
        organization_id=org_id,
        division_code=payload.division_code,
        trade_name=payload.trade_name,
        title=payload.title,
        exhibit_label=payload.exhibit_label,
        project_id=payload.project_id,
        bid_package_id=payload.bid_package_id,
        clause_ids=clause_ids,
        spec_section_ids=spec_ids,
        enabled_sections=payload.enabled_sections,
        numbering_scheme=payload.numbering_scheme,
        template_id=payload.template_id,
        created_by_id=g.api_user.id,
        base_bid_amount=payload.base_bid_amount,
        currency=payload.currency,
    )
    scope = scope_builder.build_scope(draft)
    return jsonify({"scope": _scope_summary(scope)}), 201


@bp.route("/scopes/<scope_id>")
@api_auth()
def get_scope(scope_id: str):
    scope = _scope_or_404(scope_id)
    return jsonify(build_payload(scope))


@bp.route("/scopes/<scope_id>", methods=["PATCH"])
@api_auth(write=True)
def update_scope(scope_id: str):
    scope = _scope_or_404(scope_id)
    if scope.is_locked:
        raise ValidationError(
            f"This scope is {scope.status_label.lower()}. Create a revision "
            "before editing it.",
            code="scope_locked",
        )
    payload = _body(ScopeUpdateRequest)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(scope, field, value)
    scope.updated_by_id = g.api_user.id
    db.session.commit()
    return jsonify({"scope": _scope_summary(scope)})


@bp.route("/scopes/<scope_id>/issue", methods=["POST"])
@api_auth(write=True)
def issue_scope(scope_id: str):
    scope = _scope_or_404(scope_id)
    if scope.is_locked:
        raise ValidationError("This scope has already been issued.", code="scope_locked")
    note = (request.get_json(silent=True) or {}).get("note")
    scope_builder.issue_scope(scope, user_id=g.api_user.id, note=note)
    return jsonify({"scope": _scope_summary(scope)})


@bp.route("/scopes/<scope_id>/revise", methods=["POST"])
@api_auth(write=True)
def revise_scope(scope_id: str):
    scope = _scope_or_404(scope_id)
    scope_builder.revise_scope(scope, user_id=g.api_user.id)
    return jsonify({"scope": _scope_summary(scope)})


@bp.route("/scopes/<scope_id>/export/<format_key>")
@api_auth()
@limiter.limit("120 per hour")
def export_scope(scope_id: str, format_key: str):
    scope = _scope_or_404(scope_id)
    export_format = FORMATS.get(format_key)
    if export_format is None:
        raise NotFoundError(
            f"Unknown format {format_key!r}. Supported: {', '.join(FORMATS)}."
        )

    organization = scope.organization if hasattr(scope, "organization") else None
    if format_key == "html":
        body = render_html(scope, organization=organization, standalone=True)
        return Response(body, mimetype=export_format.mimetype)

    payload = RENDERERS[format_key](scope, organization=organization)
    response = Response(payload, mimetype=export_format.mimetype)
    response.headers["Content-Disposition"] = (
        f'attachment; filename="scope-{scope.id}.{export_format.extension}"'
    )
    return response


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@bp.route("/projects")
@api_auth()
def list_projects():
    org_id = api_org_id()
    stmt = select(Project).where(Project.organization_id == org_id)
    if request.args.get("archived") != "1":
        stmt = stmt.where(Project.is_archived.is_(False))

    projects = list(db.session.scalars(stmt.order_by(Project.name)))
    return jsonify(
        {
            "count": len(projects),
            "projects": [
                {
                    "id": p.id,
                    "name": p.name,
                    "number": p.number,
                    "location": p.location,
                    "owner_name": p.owner_name,
                    "architect_name": p.architect_name,
                    "procore_project_id": p.procore_project_id,
                    "bid_packages": [
                        {
                            "id": b.id,
                            "number": b.number,
                            "name": b.name,
                            "division_code": b.division_code,
                            "trade_name": b.trade_name,
                        }
                        for b in p.bid_packages
                    ],
                }
                for p in projects
            ],
        }
    )


@bp.route("/projects", methods=["POST"])
@api_auth(write=True)
def create_project():
    payload = _body(ProjectCreateRequest)
    project = Project(organization_id=api_org_id(), **payload.model_dump(exclude_none=True))
    db.session.add(project)
    db.session.commit()
    return jsonify({"project": {"id": project.id, "name": project.name}}), 201


@bp.route("/projects/<project_id>/bid-packages", methods=["POST"])
@api_auth(write=True)
def create_bid_package(project_id: str):
    org_id = api_org_id()
    project = db.session.get(Project, project_id)
    if project is None or project.organization_id != org_id:
        raise NotFoundError("No project with that id.")

    payload = _body(BidPackageCreateRequest)
    package = BidPackage(
        project_id=project.id,
        organization_id=org_id,
        **payload.model_dump(exclude_none=True),
    )
    db.session.add(package)
    db.session.commit()
    return jsonify(
        {"bid_package": {"id": package.id, "number": package.number, "name": package.name}}
    ), 201


@bp.route("/projects/<project_id>/coverage")
@api_auth()
def project_coverage(project_id: str):
    """Specification sections claimed by nobody, or by more than one trade."""
    project = db.session.get(Project, project_id)
    if project is None or project.organization_id != api_org_id():
        raise NotFoundError("No project with that id.")

    report = coverage_service.analyse_project(
        project, include_archived=request.args.get("archived") == "1"
    )
    return jsonify(report.to_dict())


@bp.route("/projects/<project_id>/bid-packages")
@api_auth()
def list_bid_packages(project_id: str):
    org_id = api_org_id()
    packages = list(
        db.session.scalars(
            select(BidPackage).where(
                BidPackage.project_id == project_id,
                BidPackage.organization_id == org_id,
            ).order_by(BidPackage.number)
        )
    )
    return jsonify(
        {
            "count": len(packages),
            "bid_packages": [
                {
                    "id": p.id,
                    "number": p.number,
                    "name": p.name,
                    "division_code": p.division_code,
                    "trade_name": p.trade_name,
                }
                for p in packages
            ],
        }
    )
