"""Turn a handful of user choices into a complete scope document.

This is the part of ScopeMaker that does the actual work.  Given a division, a
trade, a project and a set of selected clauses, ``build_scope`` assembles the
whole exhibit -- boilerplate prose with the project's facts merged in, the
numbered summary, the cross-referenced specification sections, inclusions,
exclusions, clarifications and the contract recap -- as real, editable rows.

Nothing here is a formatting shortcut: every line the author later sees in the
editor is a ``ScopeItem`` they can reword, reorder, promote, demote or delete.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from ..data import load_boilerplate
from ..data.masterformat import get_division, normalize_code
from ..extensions import db
from ..models import (
    CATEGORY_TO_SECTION,
    DEFAULT_SECTIONS,
    BidPackage,
    Clause,
    Project,
    Scope,
    ScopeItem,
    ScopeRevision,
    ScopeSection,
    ScopeTemplate,
    SpecSection,
)
from ..models.scope import SPEC_LIST_ROLE, SectionKind
from . import library as library_service
from .sanitize import sanitize_html, sanitize_inline

# A placeholder with no value renders as an underscore run the author fills in,
# so an unmerged field is visible on the page instead of silently reading as a
# complete sentence.
BLANK = "__________"

_PLACEHOLDER = re.compile(r"\{([a-z0-9_]+)\}")


@dataclass
class ScopeDraft:
    """Everything needed to generate a scope, as collected by the wizard."""

    organization_id: str
    division_code: str | None = None
    trade_name: str | None = None
    title: str = "Scope of Work"
    exhibit_label: str = "EXHIBIT B"
    project_id: str | None = None
    bid_package_id: str | None = None
    clause_ids: list[str] = field(default_factory=list)
    spec_section_ids: list[str] = field(default_factory=list)
    enabled_sections: list[str] | None = None
    numbering_scheme: str = "legal"
    numbering_style: list[str] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    template_id: str | None = None
    created_by_id: str | None = None
    base_bid_amount: Decimal | None = None
    currency: str = "USD"

    @classmethod
    def from_form(cls, organization_id: str, form: Any, **overrides: Any) -> ScopeDraft:
        """Build a draft from a WTForms form or a plain mapping."""
        def value(name: str, default: Any = None) -> Any:
            if hasattr(form, name):
                attribute = getattr(form, name)
                return getattr(attribute, "data", attribute)
            if isinstance(form, dict):
                return form.get(name, default)
            return default

        draft = cls(
            organization_id=organization_id,
            division_code=normalize_code(value("division_code")),
            trade_name=(value("trade_name") or None),
            title=(value("title") or "Scope of Work"),
            exhibit_label=(value("exhibit_label") or "EXHIBIT B"),
            project_id=value("project_id") or None,
            bid_package_id=value("bid_package_id") or None,
            clause_ids=list(value("clause_ids") or []),
            spec_section_ids=list(value("spec_section_ids") or []),
            enabled_sections=value("enabled_sections"),
            numbering_scheme=value("numbering_scheme") or "legal",
            template_id=value("template_id") or None,
            base_bid_amount=_to_decimal(value("base_bid_amount")),
        )
        for key, val in overrides.items():
            setattr(draft, key, val)
        return draft


def _to_decimal(value: Any) -> Decimal | None:
    if value in (None, "", False):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        return None


# ---------------------------------------------------------------------------
# Placeholder merging
# ---------------------------------------------------------------------------

def build_context(
    *,
    trade_name: str | None,
    division_code: str | None,
    project: Project | None,
    bid_package: BidPackage | None,
    currency: str = "USD",
) -> dict[str, str]:
    """Values available to ``{placeholder}`` tokens in the boilerplate."""
    division = get_division(division_code)
    trade = trade_name or (division.default_trade if division else None)

    context = {
        "trade": trade or "",
        "trade_upper": (trade or "").upper(),
        "division_code": division_code or "",
        "division_title": division.title if division else "",
        "division_label": division.label if division else "",
        "currency": currency or "USD",
        "project_name": project.name if project else "",
        "project_number": project.number if project else "",
        "project_location": project.location if project else "",
        "owner_name": project.owner_name if project else "",
        "architect_name": project.architect_name if project else "",
        "engineer_name": project.engineer_name if project else "",
        "contractor_name": project.contractor_name if project else "",
        "delivery_method": project.delivery_method if project else "",
        "bid_package_number": bid_package.number if bid_package else "",
        "bid_package_name": bid_package.name if bid_package else "",
        "subcontractor_name": bid_package.subcontractor_name if bid_package else "",
    }
    return {k: (v or "") for k, v in context.items()}


def render_template_text(text: str, context: dict[str, str]) -> str:
    """Substitute ``{placeholder}`` tokens, marking anything unresolved."""
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in context:
            # Unknown token: leave it visible so the typo is obvious.
            return match.group(0)
        return context[key] or BLANK

    return _PLACEHOLDER.sub(replace, text)


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------

def build_scope(draft: ScopeDraft) -> Scope:
    """Create and persist a complete scope document from a draft."""
    project = (
        db.session.get(Project, draft.project_id) if draft.project_id else None
    )
    if project is not None and project.organization_id != draft.organization_id:
        project = None

    bid_package = (
        db.session.get(BidPackage, draft.bid_package_id) if draft.bid_package_id else None
    )
    if bid_package is not None and bid_package.organization_id != draft.organization_id:
        bid_package = None

    # A bid package knows its own division and trade; use them when the draft
    # left them blank rather than producing an untagged scope.
    division_code = draft.division_code or (bid_package.division_code if bid_package else None)
    division = get_division(division_code)
    trade_name = (
        draft.trade_name
        or (bid_package.trade_name if bid_package else None)
        or (division.default_trade if division else None)
    )

    template = _load_template(draft)

    scope = Scope(
        organization_id=draft.organization_id,
        project_id=project.id if project else None,
        bid_package_id=bid_package.id if bid_package else None,
        title=draft.title or "Scope of Work",
        exhibit_label=draft.exhibit_label or "EXHIBIT B",
        division_code=division.code if division else None,
        trade_name=trade_name,
        status="draft",
        version=1,
        numbering_style=list(draft.numbering_style or []),
        settings={"numbering_scheme": draft.numbering_scheme, **(draft.settings or {})},
        currency=draft.currency or "USD",
        base_bid_amount=draft.base_bid_amount
        or (bid_package.base_bid_amount if bid_package else None),
        created_by_id=draft.created_by_id,
        updated_by_id=draft.created_by_id,
    )
    db.session.add(scope)
    db.session.flush()  # assign scope.id before building children

    context = build_context(
        trade_name=trade_name,
        division_code=division.code if division else None,
        project=project,
        bid_package=bid_package,
        currency=scope.currency,
    )

    clauses = library_service.get_clauses(draft.organization_id, draft.clause_ids)
    spec_sections = library_service.get_spec_sections(
        draft.organization_id, draft.spec_section_ids
    )

    _build_sections(
        scope,
        context=context,
        clauses=clauses,
        spec_sections=spec_sections,
        enabled_sections=draft.enabled_sections,
        template=template,
    )

    db.session.commit()
    return scope


def _load_template(draft: ScopeDraft) -> ScopeTemplate | None:
    if not draft.template_id:
        return None
    template = db.session.get(ScopeTemplate, draft.template_id)
    if template is None or not template.is_active:
        return None
    # System templates (organization_id is None) are available to everyone.
    if template.organization_id not in (None, draft.organization_id):
        return None
    return template


def _build_sections(
    scope: Scope,
    *,
    context: dict[str, str],
    clauses: list[Clause],
    spec_sections: list[SpecSection],
    enabled_sections: list[str] | None,
    template: ScopeTemplate | None,
) -> None:
    boilerplate = load_boilerplate()
    payload = (template.payload if template else {}) or {}
    template_sections = {s.get("key"): s for s in payload.get("sections") or []}

    # Group the selected clauses by the section each category feeds.
    by_section: dict[str, list[Clause]] = {}
    for clause in clauses:
        key = CATEGORY_TO_SECTION.get(clause.category)
        if key:
            by_section.setdefault(key, []).append(clause)

    for position, definition in enumerate(DEFAULT_SECTIONS):
        key = definition["key"]
        overrides = template_sections.get(key, {})

        if enabled_sections is not None:
            is_enabled = key in enabled_sections
        elif "is_enabled" in overrides:
            is_enabled = bool(overrides["is_enabled"])
        else:
            is_enabled = bool(definition["enabled"])

        # A section with content selected for it should not stay switched off
        # just because it defaults to off -- the author asked for those clauses.
        if by_section.get(key):
            is_enabled = True

        section = ScopeSection(
            scope_id=scope.id,
            key=key,
            heading=overrides.get("heading") or definition["heading"],
            kind=definition["kind"],
            position=position * 10,
            is_enabled=is_enabled,
            is_numbered=definition["kind"] == SectionKind.ITEMS,
        )

        if key == "intent":
            lead = overrides.get("body_html") or boilerplate.get("intent", "")
        elif key == "summary":
            # The summary's opening language is authored as numbered items 1.1
            # and 1.2 (see _build_summary_items), which is how the exhibit
            # format reads. Setting it as section prose as well would print the
            # same sentence twice.
            lead = overrides.get("body_html") or ""
        else:
            lead = overrides.get("body_html") or boilerplate.get(f"{key}_lead") or ""
        if lead:
            section.body_html = sanitize_html(render_template_text(lead, context))

        db.session.add(section)
        db.session.flush()

        if key == "summary":
            _build_summary_items(section, context, boilerplate, spec_sections,
                                 by_section.get("summary", []))
        elif definition["kind"] == SectionKind.ITEMS:
            _append_clause_items(section, by_section.get(key, []))

        # Template-supplied literal items (custom language saved with the
        # template) are appended after the generated ones.
        for entry in overrides.get("items") or []:
            _append_literal_item(section, entry, context)


def _build_summary_items(
    section: ScopeSection,
    context: dict[str, str],
    boilerplate: dict[str, str],
    spec_sections: list[SpecSection],
    extra_clauses: list[Clause],
) -> None:
    """The three-part summary, with spec sections nested under the third item."""
    position = 0

    for template_key in ("summary_lead", "summary_means_and_methods"):
        text = boilerplate.get(template_key)
        if not text:
            continue
        db.session.add(
            ScopeItem(
                section_id=section.id,
                text_html=sanitize_inline(render_template_text(text, context)),
                position=position,
                meta={"role": template_key},
            )
        )
        position += 10

    if spec_sections:
        lead = ScopeItem(
            section_id=section.id,
            text_html=sanitize_inline(
                render_template_text(boilerplate.get("summary_spec_lead", ""), context)
            ),
            position=position,
            meta={"role": SPEC_LIST_ROLE},
        )
        db.session.add(lead)
        db.session.flush()
        position += 10

        for index, spec in enumerate(spec_sections):
            db.session.add(
                ScopeItem(
                    section_id=section.id,
                    parent_id=lead.id,
                    text_html=sanitize_inline(f"{spec.code} &ndash; {spec.title}"),
                    position=index * 10,
                    meta={
                        "role": "spec_section",
                        "spec_code": spec.code,
                        "spec_title": spec.title,
                        "spec_division": spec.division_code,
                        "spec_section_id": spec.id,
                    },
                )
            )

    # General-requirement clauses land in the summary as further numbered items.
    for index, clause in enumerate(extra_clauses):
        db.session.add(
            ScopeItem(
                section_id=section.id,
                text_html=sanitize_inline(clause.text),
                position=position + index * 10,
                source_clause_id=clause.id,
                meta={"role": "clause"},
            )
        )


def _append_clause_items(section: ScopeSection, clauses: list[Clause]) -> None:
    for index, clause in enumerate(clauses):
        db.session.add(
            ScopeItem(
                section_id=section.id,
                text_html=sanitize_inline(clause.text),
                position=index * 10,
                source_clause_id=clause.id,
                meta={"role": "clause", "category": clause.category},
            )
        )


def _append_literal_item(
    section: ScopeSection, entry: dict[str, Any], context: dict[str, str],
    parent_id: str | None = None, base_position: int = 10_000,
) -> None:
    text = entry.get("text_html") or entry.get("text") or ""
    if not text:
        return
    item = ScopeItem(
        section_id=section.id,
        parent_id=parent_id,
        text_html=sanitize_inline(render_template_text(text, context)),
        position=int(entry.get("position", base_position)),
        meta={"role": "template"},
    )
    db.session.add(item)
    db.session.flush()
    for index, child in enumerate(entry.get("children") or []):
        _append_literal_item(section, child, context, parent_id=item.id,
                             base_position=index * 10)


# ---------------------------------------------------------------------------
# Editing operations
# ---------------------------------------------------------------------------

def add_clauses_to_scope(scope: Scope, section_key: str, clauses: list[Clause]) -> int:
    """Append library clauses to an existing section. Returns how many landed."""
    section = scope.section(section_key)
    if section is None or not clauses:
        return 0
    existing = {i.source_clause_id for i in section.items if i.source_clause_id}
    start = max((i.position for i in section.items), default=-10) + 10
    added = 0
    for clause in clauses:
        if clause.id in existing:
            continue  # already on the scope; adding it twice helps nobody
        db.session.add(
            ScopeItem(
                section_id=section.id,
                text_html=sanitize_inline(clause.text),
                position=start + added * 10,
                source_clause_id=clause.id,
                meta={"role": "clause", "category": clause.category},
            )
        )
        added += 1
    return added


def renumber_section(section: ScopeSection) -> None:
    """Normalise positions to a clean 10-step sequence at every depth."""
    def renumber(items: list[ScopeItem]) -> None:
        for index, item in enumerate(sorted(items, key=lambda i: i.position)):
            item.position = index * 10
            renumber(list(item.children))

    renumber([i for i in section.items if i.parent_id is None])


def snapshot_scope(scope: Scope, *, note: str | None = None,
                   user_id: str | None = None) -> ScopeRevision:
    """Freeze the current state as an immutable revision."""
    revision = ScopeRevision(
        scope_id=scope.id,
        version=scope.version,
        snapshot=scope.to_dict(),
        note=note,
        created_by_id=user_id,
    )
    db.session.add(revision)
    return revision


def issue_scope(scope: Scope, *, user_id: str | None = None,
                note: str | None = None) -> ScopeRevision:
    """Mark a scope as issued and freeze the version that went out."""
    from ..models.base import utcnow

    revision = snapshot_scope(scope, note=note or "Issued", user_id=user_id)
    scope.status = "issued"
    scope.issued_at = utcnow()
    scope.updated_by_id = user_id
    db.session.commit()
    return revision


def revise_scope(scope: Scope, *, user_id: str | None = None) -> Scope:
    """Reopen an issued scope for editing as the next version."""
    # Issuing already froze this version. Snapshotting again would violate the
    # one-revision-per-version constraint, so only capture a version that was
    # never frozen (a scope revised straight from draft).
    if not any(revision.version == scope.version for revision in scope.revisions):
        snapshot_scope(
            scope,
            note=f"Superseded by version {scope.version + 1}",
            user_id=user_id,
        )
    scope.version += 1
    scope.status = "draft"
    scope.issued_at = None
    scope.updated_by_id = user_id
    db.session.commit()
    return scope


def duplicate_scope(scope: Scope, *, user_id: str | None = None,
                    title: str | None = None) -> Scope:
    """Deep-copy a scope, including the full item tree."""
    clone = Scope(
        organization_id=scope.organization_id,
        project_id=scope.project_id,
        bid_package_id=scope.bid_package_id,
        title=title or f"{scope.title} (copy)",
        exhibit_label=scope.exhibit_label,
        division_code=scope.division_code,
        trade_name=scope.trade_name,
        status="draft",
        version=1,
        numbering_style=list(scope.numbering_style or []),
        settings=dict(scope.settings or {}),
        currency=scope.currency,
        base_bid_amount=scope.base_bid_amount,
        alternates_amount=scope.alternates_amount,
        adjustments_amount=scope.adjustments_amount,
        created_by_id=user_id,
        updated_by_id=user_id,
    )
    db.session.add(clone)
    db.session.flush()

    for section in scope.sections:
        new_section = ScopeSection(
            scope_id=clone.id,
            key=section.key,
            heading=section.heading,
            kind=section.kind,
            body_html=section.body_html,
            position=section.position,
            is_enabled=section.is_enabled,
            is_numbered=section.is_numbered,
        )
        db.session.add(new_section)
        db.session.flush()
        _copy_items(section.root_items, new_section.id, None)

    db.session.commit()
    return clone


def _copy_items(items: list[ScopeItem], section_id: str, parent_id: str | None) -> None:
    for item in items:
        clone = ScopeItem(
            section_id=section_id,
            parent_id=parent_id,
            text_html=item.text_html,
            position=item.position,
            source_clause_id=item.source_clause_id,
            is_edited=item.is_edited,
            meta=dict(item.meta or {}),
        )
        db.session.add(clone)
        db.session.flush()
        children = sorted(item.children, key=lambda c: c.position)
        if children:
            _copy_items(children, section_id, clone.id)


def save_as_template(
    scope: Scope, *, name: str, description: str | None = None,
    user_id: str | None = None,
) -> ScopeTemplate:
    """Capture a scope's structure and language as a reusable template."""
    payload = {
        "exhibit_label": scope.exhibit_label,
        "title": scope.title,
        "numbering_style": list(scope.numbering_style or []),
        "settings": dict(scope.settings or {}),
        "sections": [
            {
                "key": section.key,
                "heading": section.heading,
                "kind": section.kind,
                "is_enabled": section.is_enabled,
                "body_html": section.body_html,
                "items": [_item_payload(i) for i in section.root_items],
            }
            for section in scope.sections
        ],
    }
    template = ScopeTemplate(
        organization_id=scope.organization_id,
        name=name,
        description=description,
        division_code=scope.division_code,
        trade_name=scope.trade_name,
        payload=payload,
        created_by_id=user_id,
    )
    db.session.add(template)
    db.session.commit()
    return template


def _item_payload(item: ScopeItem) -> dict[str, Any]:
    return {
        "text_html": item.text_html,
        "position": item.position,
        "children": [
            _item_payload(c) for c in sorted(item.children, key=lambda c: c.position)
        ],
    }
