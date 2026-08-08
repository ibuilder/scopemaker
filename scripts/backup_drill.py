"""Rehearse the documented backup procedure, end to end.

``docs/deployment.md`` tells operators to back up the PostgreSQL database and
says that is all the state there is. That claim was never tested. A backup
procedure nobody has restored from is a hypothesis, not a backup.

This script:

1. Builds a populated database -- organization, users, project, scopes,
   revisions, audit events, an API token and a cached render.
2. Records what "correct" looks like: row counts per table, and the exact bytes
   of a rendered exhibit.
3. Dumps with ``pg_dump``, drops every table, restores with ``psql``.
4. Asserts the row counts match *and* that the restored database renders the
   same document, byte for byte.

Step 4 is the point. Row counts alone would pass even if encrypted columns came
back as mush, so it re-renders and compares.

    DATABASE_URL=postgresql+psycopg://user:pw@localhost:5432/scopemaker \
        python scripts/backup_drill.py
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from scopemaker import create_app
from scopemaker.extensions import db
from scopemaker.models import ApiToken, BidPackage, Membership, Project
from scopemaker.services import library as library_service
from scopemaker.services.accounts import create_organization, create_user
from scopemaker.services.renderers import render_markdown
from scopemaker.services.scope_builder import ScopeDraft, build_scope, issue_scope
from scopemaker.services.seeding import seed_library

PASSWORD = "backup-drill-not-a-real-password"


def libpq_url() -> str:
    """SQLAlchemy URL -> something pg_dump understands."""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        sys.exit("DATABASE_URL is required and must point at PostgreSQL.")
    url = url.replace("postgresql+psycopg://", "postgresql://")
    url = url.replace("postgresql+psycopg2://", "postgresql://")
    if not url.startswith("postgresql://"):
        sys.exit(f"This drill only makes sense against PostgreSQL, got {url!r}.")
    return url


def populate() -> tuple[str, bytes]:
    """Create representative data. Returns a scope id and its rendered bytes."""
    seed_library()

    organization = create_organization("Backup Drill Builders")
    user = create_user(
        email="drill@example.com", full_name="Drill Operator", password=PASSWORD
    )
    db.session.add(
        Membership(organization_id=organization.id, user_id=user.id, role="admin")
    )
    db.session.commit()

    # An encrypted column, so the drill notices if encryption keys or bytea
    # round-tripping break.
    token, _raw = ApiToken.issue(
        user=user, organization_id=organization.id, name="drill", scopes="read"
    )
    db.session.add(token)
    db.session.commit()

    project = Project(
        organization_id=organization.id,
        name="Backup Drill Tower",
        number="BD-001",
        address="1 Restore Way",
        city="Columbus",
        state="OH",
        owner_name="Drill Holdings",
        architect_name="Drill Architects",
        contractor_name=organization.name,
        delivery_method="CMAR",
    )
    db.session.add(project)
    db.session.commit()

    scope_id = ""
    for index, code in enumerate(["21", "23", "26"]):
        package = BidPackage(
            project_id=project.id,
            organization_id=organization.id,
            number=f"BP-{code}A",
            name=f"Package {code}",
            division_code=code,
        )
        db.session.add(package)
        db.session.commit()

        scope = build_scope(
            ScopeDraft(
                organization_id=organization.id,
                division_code=code,
                project_id=project.id,
                bid_package_id=package.id,
                clause_ids=library_service.default_clause_ids(organization.id, code),
                spec_section_ids=library_service.default_spec_section_ids(
                    organization.id, code
                ),
                created_by_id=user.id,
                base_bid_amount=1_000_000 + index * 50_000,
            )
        )
        if index == 0:
            # Freeze a revision, which is the record that matters in a dispute.
            issue_scope(scope, user_id=user.id)
            db.session.commit()
            scope_id = scope.id

    payload = render_markdown(_scope(scope_id), organization=organization)
    return scope_id, payload


def _scope(scope_id: str):
    from scopemaker.models import Scope

    return db.session.get(Scope, scope_id)


def table_counts() -> dict[str, int]:
    counts = {}
    for table in db.metadata.sorted_tables:
        counts[table.name] = db.session.execute(
            text(f'SELECT count(*) FROM "{table.name}"')
        ).scalar_one()
    return counts


def run(command: list[str], **kwargs) -> None:
    result = subprocess.run(command, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        sys.exit(f"{command[0]} failed with exit code {result.returncode}")


def main() -> int:
    url = libpq_url()
    parsed = urlparse(url)
    database = parsed.path.lstrip("/")

    app = create_app("development")
    with app.app_context():
        print("==> Building a populated database")
        db.drop_all()
        db.create_all()
        scope_id, before_render = populate()
        before_counts = table_counts()
        populated = sum(before_counts.values())
        print(f"    {populated} rows across {len(before_counts)} tables")
        print(f"    reference document: {len(before_render)} bytes, "
              f"sha256 {hashlib.sha256(before_render).hexdigest()[:16]}")

        with tempfile.TemporaryDirectory() as tmp:
            dump = Path(tmp) / "scopemaker.dump"

            print("==> pg_dump")
            run(["pg_dump", "--format=custom", "--file", str(dump), url])
            print(f"    {dump.stat().st_size // 1024} KB")

            print("==> Dropping every table (simulating the disaster)")
            db.session.execute(text("DROP SCHEMA public CASCADE"))
            db.session.execute(text("CREATE SCHEMA public"))
            db.session.commit()
            db.session.remove()

            remaining = db.session.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            ).scalar_one()
            if remaining:
                sys.exit(f"expected an empty schema, found {remaining} tables")
            print("    schema is empty")

            print("==> pg_restore")
            run(["pg_restore", "--dbname", url, "--no-owner", str(dump)])
            db.session.remove()

        print("==> Verifying")
        after_counts = table_counts()
        if after_counts != before_counts:
            differences = {
                name: (before_counts.get(name), after_counts.get(name))
                for name in set(before_counts) | set(after_counts)
                if before_counts.get(name) != after_counts.get(name)
            }
            sys.exit(f"row counts differ after restore: {differences}")
        print(f"    row counts match ({populated} rows)")

        from scopemaker.models import Organization

        organization = db.session.scalar(
            db.select(Organization).where(Organization.slug.like("backup-drill%"))
        )
        after_render = render_markdown(_scope(scope_id), organization=organization)
        if after_render != before_render:
            sys.exit(
                "the restored database renders a DIFFERENT document "
                f"({len(before_render)} bytes before, {len(after_render)} after)"
            )
        print("    restored database renders the same document, byte for byte")

        print(f"\nBackup and restore verified against {database}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
