"""Project-level scope coverage analysis.

A scope gap is work that appears in the drawings and specifications but ends up
in nobody's contract -- classically at the seam between two trades, where each
assumed the other had it. It surfaces during construction, and somebody pays
for it then.

Every other tool in this space has to infer coverage from PDFs. ScopeMaker
already holds each exhibit as structured rows, so the same question is a query:
line up the specification sections that every package on a project claims, and
look at what has nobody's name against it -- or two names.

Three findings come out of this:

``gap``
    A specification section that applies to a division present on the project
    but is claimed by no scope.

``overlap``
    A trade-specific section claimed by two or more scopes. Usually means the
    same work is being bought twice.

``shared``
    A section that is *designed* to be carried by several trades -- the ones
    the library cross-references to other divisions -- and is. Every trade
    firestops its own penetrations, so four claims on 078413 is correct, not a
    double-buy. These are reported separately because the seam still needs a
    decision: who paints the exposed sprinkler pipe is exactly the kind of
    question that turns into a change order.

``redirect``
    An exclusion that pushes work onto another division ("...which is by the
    Division 28 Subcontractor") when no scope for that division exists on the
    project yet. That is a gap in the making.

The output is deliberately advisory. It reports what the documents say, and
leaves the judgement to the person running buyout.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import noload

from ..data.masterformat import get_division
from ..extensions import db
from ..models import Project, Scope, ScopeItem, ScopeSection, SpecSection
from ..services.sanitize import strip_stored_html

# Matches "Division 07" / "Division 7" in clause text. Used to spot exclusions
# that hand work to a trade which may not be under contract.
_DIVISION_MENTION = re.compile(r"\bDivision\s+(\d{1,2})\b", re.IGNORECASE)

# Matches a CSI section number (six digits, optionally with separators) in item
# text, so a hand-typed section still counts as claimed.
_SECTION_NUMBER = re.compile(r"\b(\d{6})\b")

GAP = "gap"
COVERED = "covered"
OVERLAP = "overlap"
SHARED = "shared"

# Division 00 and 01 are procurement and general requirements: every trade
# carries them, and saying so on every project would be pure noise. These never
# produce a finding.
PROCEDURAL_DIVISIONS = frozenset({"00", "01"})


@dataclass(frozen=True)
class ScopeRef:
    """A scope, as referenced from a finding."""

    id: str
    label: str
    division_code: str | None
    trade_name: str | None
    status: str

    @property
    def short(self) -> str:
        parts = [self.division_code and f"Div {self.division_code}", self.trade_name]
        return " – ".join(p for p in parts if p) or self.label


@dataclass
class SectionCoverage:
    code: str
    title: str
    division_code: str
    claimed_by: list[ScopeRef] = field(default_factory=list)
    # True when the library cross-references this section to other divisions,
    # i.e. it is meant to be carried by several trades at once.
    is_shared_by_design: bool = False
    # True for Division 00/01 procedural sections, which every trade carries.
    is_procedural: bool = False

    @property
    def status(self) -> str:
        if not self.claimed_by:
            return GAP
        if len(self.claimed_by) == 1 or self.is_procedural:
            return COVERED
        return SHARED if self.is_shared_by_design else OVERLAP

    @property
    def display(self) -> str:
        return f"{self.code} - {self.title}"


@dataclass
class RedirectFinding:
    """An exclusion that hands work to a division with no scope on the project."""

    scope: ScopeRef
    referenced_division: str
    referenced_title: str
    text: str

    @property
    def summary(self) -> str:
        return (
            f"{self.scope.short} excludes work it assigns to Division "
            f"{self.referenced_division} ({self.referenced_title}), which has no "
            "scope on this project."
        )


@dataclass
class CoverageReport:
    project_id: str
    project_name: str
    scopes: list[ScopeRef] = field(default_factory=list)
    sections: list[SectionCoverage] = field(default_factory=list)
    redirects: list[RedirectFinding] = field(default_factory=list)
    packages_without_scope: list[tuple[str, str]] = field(default_factory=list)

    # -- Rollups ------------------------------------------------------------
    @property
    def gaps(self) -> list[SectionCoverage]:
        return [s for s in self.sections if s.status == GAP]

    @property
    def overlaps(self) -> list[SectionCoverage]:
        return [s for s in self.sections if s.status == OVERLAP]

    @property
    def shared(self) -> list[SectionCoverage]:
        """Cross-trade seams -- correct, but they still need a decision."""
        return [s for s in self.sections if s.status == SHARED]

    @property
    def covered(self) -> list[SectionCoverage]:
        return [s for s in self.sections if s.status == COVERED]

    @property
    def finding_count(self) -> int:
        """Only the things that are probably wrong. Shared seams are advisory."""
        return len(self.gaps) + len(self.overlaps) + len(self.redirects)

    @property
    def is_clean(self) -> bool:
        return self.finding_count == 0

    @property
    def divisions_present(self) -> list[str]:
        return sorted({s.division_code for s in self.scopes if s.division_code})

    def to_dict(self) -> dict:
        return {
            "project": {"id": self.project_id, "name": self.project_name},
            "summary": {
                "scopes": len(self.scopes),
                "sections_analysed": len(self.sections),
                "gaps": len(self.gaps),
                "overlaps": len(self.overlaps),
                "shared": len(self.shared),
                "redirects": len(self.redirects),
                "packages_without_scope": len(self.packages_without_scope),
            },
            "divisions_present": self.divisions_present,
            "scopes": [
                {
                    "id": s.id,
                    "label": s.label,
                    "division_code": s.division_code,
                    "trade_name": s.trade_name,
                    "status": s.status,
                }
                for s in self.scopes
            ],
            "sections": [
                {
                    "code": s.code,
                    "title": s.title,
                    "division_code": s.division_code,
                    "status": s.status,
                    "claimed_by": [
                        {"scope_id": c.id, "division_code": c.division_code,
                         "trade_name": c.trade_name}
                        for c in s.claimed_by
                    ],
                }
                for s in self.sections
            ],
            "redirects": [
                {
                    "scope_id": r.scope.id,
                    "scope_division": r.scope.division_code,
                    "referenced_division": r.referenced_division,
                    "referenced_title": r.referenced_title,
                    "text": r.text,
                }
                for r in self.redirects
            ],
            "packages_without_scope": [
                {"number": number, "name": name}
                for number, name in self.packages_without_scope
            ],
        }


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def _scope_ref(scope: Scope) -> ScopeRef:
    return ScopeRef(
        id=scope.id,
        label=scope.document_title,
        division_code=scope.division_code,
        trade_name=scope.trade_name,
        status=scope.status,
    )


#: One row per item: (section key, meta, text_html), for enabled sections only.
ItemRow = tuple[str, dict, str]


def _load_item_rows(scope_ids: list[str]) -> dict[str, list[ItemRow]]:
    """Every enabled item of every listed scope, as rows rather than objects.

    Walking ``scope.sections`` then ``section.items`` reads naturally and makes
    SQLAlchemy construct a full ORM instance per row -- instrumented, tracked in
    the identity map, registered with the unit of work. Profiling a 25-scope
    report showed 2012 of those built purely so two columns could be read off
    them, and instance construction alone was 45% of the remaining runtime.

    Nothing here mutates an item, so the ORM machinery buys nothing. One query
    returning plain tuples replaces it.
    """
    if not scope_ids:
        return {}

    rows = db.session.execute(
        select(
            ScopeSection.scope_id,
            ScopeSection.key,
            ScopeItem.meta,
            ScopeItem.text_html,
        )
        .join(ScopeItem, ScopeItem.section_id == ScopeSection.id)
        .where(
            ScopeSection.scope_id.in_(scope_ids),
            ScopeSection.is_enabled.is_(True),
        )
        .order_by(ScopeSection.position, ScopeItem.position)
    ).all()

    by_scope: dict[str, list[ItemRow]] = {}
    for scope_id, key, meta, text_html in rows:
        by_scope.setdefault(scope_id, []).append((key, meta or {}, text_html or ""))
    return by_scope


def _claimed_section_codes(items: list[ItemRow]) -> set[str]:
    """Every specification section a scope references.

    Prefers the structured id recorded when the scope was generated, and falls
    back to a six-digit number in the text so a hand-typed section still counts
    as claimed -- otherwise editing a line by hand would manufacture a gap.
    """
    codes: set[str] = set()
    for _key, meta, text_html in items:
        code = meta.get("spec_code")
        if code:
            codes.add(str(code).strip())
            continue
        if meta.get("role") == "spec_section":
            match = _SECTION_NUMBER.search(strip_stored_html(text_html))
            if match:
                codes.add(match.group(1))
    return codes


def _exclusion_redirects(
    items: list[ItemRow], division_code: str | None
) -> list[tuple[str, str]]:
    """(division, text) for exclusions that assign work to another division."""
    found: list[tuple[str, str]] = []
    for key, _meta, text_html in items:
        if key != "exclusions":
            continue
        text = strip_stored_html(text_html)
        for match in _DIVISION_MENTION.finditer(text):
            code = match.group(1).zfill(2)
            if code == division_code:
                continue  # a trade referring to its own division is not a hand-off
            found.append((code, text))
    return found


def analyse_project(project: Project, *, include_archived: bool = False) -> CoverageReport:
    """Build the coverage report for one project."""
    # Loaded explicitly rather than through ``project.scopes``. Scope.sections
    # and ScopeSection.items are both lazy="selectin", so merely touching that
    # relationship eagerly materialises every section and every item of every
    # scope as ORM objects -- 2012 of them on a 25-scope project -- whether or
    # not anything reads them. This report only needs two columns per item, and
    # gets them from one row query in _load_item_rows.
    #
    # noload is safe here precisely because of that: nothing below walks
    # scope.sections. If that changes, it will read as an empty list rather
    # than fail, so keep item access going through _load_item_rows.
    scopes = [
        scope
        for scope in db.session.scalars(
            select(Scope)
            .where(Scope.project_id == project.id)
            .options(noload(Scope.sections))
        )
        if include_archived or scope.status != "archived"
    ]
    report = CoverageReport(project_id=project.id, project_name=project.display_title)
    report.scopes = [_scope_ref(s) for s in scopes]

    if not scopes:
        report.packages_without_scope = [
            (p.number, p.name) for p in project.bid_packages
        ]
        return report

    divisions_present = {s.division_code for s in scopes if s.division_code}

    # Every item of every scope, in one query -- see _load_item_rows.
    items_by_scope = _load_item_rows([s.id for s in scopes])

    # What each scope claims, keyed by section number.
    claims: dict[str, list[ScopeRef]] = {}
    for scope in scopes:
        ref = _scope_ref(scope)
        for code in _claimed_section_codes(items_by_scope.get(scope.id, [])):
            claims.setdefault(code, []).append(ref)

    # The universe of sections we hold this project to: everything the library
    # marks as a default for a division that is actually on the project, plus
    # anything a scope already claims. Offering every conceivable section would
    # bury the real findings in noise.
    catalogue = list(
        db.session.scalars(
            select(SpecSection).where(SpecSection.is_active.is_(True))
        )
    )
    by_code: dict[str, SpecSection] = {}
    for candidate in catalogue:
        # Organization rows take precedence over the shipped ones.
        existing = by_code.get(candidate.code)
        if existing is None or (existing.organization_id is None
                                and candidate.organization_id is not None):
            by_code[candidate.code] = candidate

    expected: set[str] = set(claims)
    for section in by_code.values():
        if not section.is_default:
            continue
        if any(section.applies_to(division) for division in divisions_present):
            expected.add(section.code)

    for code in sorted(expected):
        # A claimed code with no catalogue entry is possible: somebody typed a
        # section number by hand that is not in the library.
        known: SpecSection | None = by_code.get(code)
        title = known.title if known else "Unknown section"
        division_code = known.division_code if known else code[:2]
        report.sections.append(
            SectionCoverage(
                code=code,
                title=title,
                division_code=division_code,
                claimed_by=sorted(
                    claims.get(code, []), key=lambda r: (r.division_code or "", r.label)
                ),
                is_shared_by_design=bool(known and known.related_divisions),
                is_procedural=division_code in PROCEDURAL_DIVISIONS
                or bool(known and known.is_universal),
            )
        )

    # Exclusions that hand work to a division nobody is under contract for.
    seen: set[tuple[str, str]] = set()
    for scope in scopes:
        ref = _scope_ref(scope)
        redirects = _exclusion_redirects(
            items_by_scope.get(scope.id, []), scope.division_code
        )
        for division_code, text in redirects:
            if division_code in divisions_present:
                continue
            key = (scope.id, division_code)
            if key in seen:
                continue  # one finding per scope per referenced division
            seen.add(key)
            division = get_division(division_code)
            report.redirects.append(
                RedirectFinding(
                    scope=ref,
                    referenced_division=division_code,
                    referenced_title=division.title if division else "Unknown division",
                    text=text,
                )
            )

    scoped_package_ids = {s.bid_package_id for s in scopes if s.bid_package_id}
    report.packages_without_scope = [
        (p.number, p.name)
        for p in project.bid_packages
        if p.id not in scoped_package_ids
    ]

    return report


def to_csv(report: CoverageReport) -> str:
    """The matrix as CSV, for taking into a buyout meeting."""
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")

    writer.writerow(["ScopeMaker coverage report", report.project_name])
    writer.writerow(
        [
            f"{len(report.gaps)} gap(s)",
            f"{len(report.overlaps)} overlap(s)",
            f"{len(report.shared)} shared seam(s)",
            f"{len(report.redirects)} unassigned hand-off(s)",
        ]
    )
    writer.writerow([])
    writer.writerow(["Section", "Title", "Division", "Status", "Claimed by"])
    # Findings first: this gets printed and taken into a buyout meeting.
    order = {GAP: 0, OVERLAP: 1, SHARED: 2, COVERED: 3}
    for section in sorted(
        report.sections, key=lambda s: (order.get(s.status, 9), s.code)
    ):
        writer.writerow(
            [
                section.code,
                section.title,
                section.division_code,
                section.status,
                "; ".join(c.short for c in section.claimed_by) or "NOBODY",
            ]
        )

    if report.redirects:
        writer.writerow([])
        writer.writerow(["Exclusions assigning work to a division not on the project"])
        writer.writerow(["Scope", "Referenced division", "Exclusion text"])
        for redirect in report.redirects:
            writer.writerow(
                [
                    redirect.scope.short,
                    f"{redirect.referenced_division} {redirect.referenced_title}",
                    redirect.text,
                ]
            )

    if report.packages_without_scope:
        writer.writerow([])
        writer.writerow(["Bid packages with no scope written yet"])
        for number, name in report.packages_without_scope:
            writer.writerow([number, name])

    return buffer.getvalue()


__all__ = [
    "COVERED",
    "GAP",
    "OVERLAP",
    "SHARED",
    "CoverageReport",
    "RedirectFinding",
    "ScopeRef",
    "SectionCoverage",
    "analyse_project",
    "to_csv",
]
