"""Render a sample exhibit in every format, for review.

Run by the "Build sample exhibits" workflow, which has the WeasyPrint native
stack installed. Also runnable locally:

    DATABASE_URL=sqlite:///sample.sqlite3 python scripts/build_samples.py

Formats other than PDF work without the native libraries; the script reports
what it managed to produce rather than failing outright.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scopemaker import create_app
from scopemaker.data.masterformat import get_division
from scopemaker.extensions import db
from scopemaker.models import BidPackage, Project
from scopemaker.services import library as library_service
from scopemaker.services.accounts import create_organization
from scopemaker.services.renderers import (
    PDF_AVAILABLE,
    render_docx,
    render_json,
    render_markdown,
    render_pdf,
)
from scopemaker.services.scope_builder import ScopeDraft, build_scope
from scopemaker.services.seeding import seed_library

OUT = Path("samples")
DIVISIONS = [
    code.strip()
    for code in os.environ.get("DIVISIONS", "21,22,23,26,09,31").split(",")
    if code.strip()
]


def main() -> int:
    app = create_app("development")
    OUT.mkdir(exist_ok=True)
    rows: list[str] = []

    with app.app_context():
        db.drop_all()
        db.create_all()
        seed_library()

        org = create_organization("Meridian Construction")
        org.legal_name = "Meridian Construction Group, LLC"
        org.address = "220 Grandview Ave, Suite 400, Columbus, OH 43215"
        org.phone = "(614) 555-0142"
        db.session.flush()

        project = Project(
            organization_id=org.id,
            name="Riverside Medical Center",
            number="2024-118",
            address="1400 River Road",
            city="Columbus",
            state="OH",
            postal_code="43215",
            owner_name="Riverside Health System",
            architect_name="Whitfield Architects",
            engineer_name="Calder Engineering Group",
            contractor_name="Meridian Construction Group, LLC",
            delivery_method="CMAR",
        )
        db.session.add(project)
        db.session.commit()

        for index, code in enumerate(DIVISIONS, start=1):
            division = get_division(code)
            if division is None or division.reserved:
                print(f"  skipping {code!r}: not a specifiable division")
                continue

            trade = division.default_trade
            package = BidPackage(
                project_id=project.id,
                organization_id=org.id,
                number=f"BP-{code}A",
                name=trade,
                division_code=code,
                trade_name=trade,
            )
            db.session.add(package)
            db.session.commit()

            scope = build_scope(
                ScopeDraft(
                    organization_id=org.id,
                    division_code=code,
                    project_id=project.id,
                    bid_package_id=package.id,
                    clause_ids=library_service.default_clause_ids(org.id, code),
                    spec_section_ids=library_service.default_spec_section_ids(org.id, code),
                    base_bid_amount=250_000 * index + 175_400,
                )
            )

            stem = f"EXHIBIT-B-{code}-{trade.replace(' ', '-').replace('/', '-')}"
            pages = "n/a"

            written = {}
            written["docx"] = render_docx(scope, organization=org)
            written["md"] = render_markdown(scope, organization=org)
            written["json"] = render_json(scope, organization=org)

            if PDF_AVAILABLE:
                pdf = render_pdf(scope, organization=org)
                written["pdf"] = pdf
                try:
                    from io import BytesIO

                    from pypdf import PdfReader

                    pages = str(len(PdfReader(BytesIO(pdf)).pages))
                except Exception:
                    pages = "?"

            for extension, payload in written.items():
                (OUT / f"{stem}.{extension}").write_bytes(payload)

            size = len(written.get("pdf", b"")) // 1024
            rows.append(
                f"| {code} | {trade} | {scope.item_count} | {pages} | "
                f"{size or '—'} KB |"
            )
            print(f"  Division {code} {trade}: {scope.item_count} items, {pages} pages")

    summary = [
        "## Sample exhibits",
        "",
        f"PDF rendering: **{'available' if PDF_AVAILABLE else 'UNAVAILABLE'}**",
        "",
        "| Division | Trade | Items | PDF pages | PDF size |",
        "|---|---|---:|---:|---:|",
        *rows,
        "",
        "Each exhibit is provided as PDF, DOCX, Markdown and JSON. All four",
        "carry identical clause numbering.",
        "",
    ]
    (OUT / "SUMMARY.md").write_text("\n".join(summary), encoding="utf-8")
    print("\n".join(summary))

    if not PDF_AVAILABLE:
        print("WARNING: PDFs were not produced; the native stack is missing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
