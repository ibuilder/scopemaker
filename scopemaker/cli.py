"""Command line utilities, exposed as ``flask <command>``."""

from __future__ import annotations

import sys

import click
from flask import Flask
from flask.cli import with_appcontext
from sqlalchemy import select

from .extensions import db


def register_commands(app: Flask) -> None:
    app.cli.add_command(init_db)
    app.cli.add_command(seed_library)
    app.cli.add_command(create_org)
    app.cli.add_command(create_user)
    app.cli.add_command(grant_role)
    app.cli.add_command(check_pdf)
    app.cli.add_command(demo_data)
    app.cli.add_command(run_worker)
    app.cli.add_command(render_queue_status)


@click.command("init-db")
@with_appcontext
def init_db() -> None:
    """Create every table directly, bypassing migrations.

    Convenient for a throwaway development database. Use
    ``flask db upgrade`` for anything you intend to keep.
    """
    from . import models  # noqa: F401  (registers the mappings)

    db.create_all()
    click.secho("Database tables created.", fg="green")


@click.command("seed-library")
@click.option("--no-update", is_flag=True, help="Only insert new rows; leave existing text alone.")
@with_appcontext
def seed_library(no_update: bool) -> None:
    """Load or refresh the shipped clause and specification library."""
    from .services.seeding import seed_library as run_seed

    result = run_seed(update_existing=not no_update)
    click.secho(result.summary(), fg="green")
    for message in result.skipped:
        click.secho(f"  skipped: {message}", fg="yellow")


@click.command("create-org")
@click.argument("name")
@with_appcontext
def create_org(name: str) -> None:
    """Create an organization."""
    from .services.accounts import create_organization

    organization = create_organization(name)
    db.session.commit()
    click.secho(f"Created organization {organization.name} ({organization.slug}).", fg="green")


@click.command("create-user")
@click.argument("email")
@click.option("--name", default="", help="Full name.")
@click.option("--org", default=None, help="Organization slug to join. Created if missing.")
@click.option("--role", default="admin", type=click.Choice(["viewer", "editor", "admin"]))
@click.option("--superuser", is_flag=True, help="Grant application-wide access.")
@click.password_option(help="Password (prompted if omitted).")
@with_appcontext
def create_user(
    email: str, name: str, org: str | None, role: str, superuser: bool, password: str
) -> None:
    """Create a user and place them in an organization."""
    from .errors import ScopeMakerError
    from .models import Membership, Organization
    from .security import password_problems
    from .services.accounts import create_organization
    from .services.accounts import create_user as make_user

    problems = password_problems(password)
    if problems:
        click.secho("Password rejected: " + " ".join(problems), fg="red")
        sys.exit(1)

    try:
        user = make_user(email=email, full_name=name or email.split("@")[0],
                         password=password)
    except ScopeMakerError as exc:
        click.secho(exc.message, fg="red")
        sys.exit(1)

    user.is_superuser = superuser

    organization = None
    if org:
        organization = db.session.scalar(
            select(Organization).where(Organization.slug == org)
        )
        if organization is None:
            organization = create_organization(org)
            click.secho(f"Created organization {organization.slug}.", fg="yellow")
    else:
        organization = create_organization(name or email.split("@")[0])

    db.session.add(
        Membership(organization_id=organization.id, user_id=user.id, role=role)
    )
    db.session.commit()
    click.secho(
        f"Created {user.email} as {role} of {organization.name} ({organization.slug}).",
        fg="green",
    )


@click.command("grant-role")
@click.argument("email")
@click.argument("org_slug")
@click.argument("role", type=click.Choice(["viewer", "editor", "admin"]))
@with_appcontext
def grant_role(email: str, org_slug: str, role: str) -> None:
    """Set a user's role in an organization, adding them if needed."""
    from .models import Membership, Organization, User

    user = User.by_email(email)
    if user is None:
        click.secho(f"No user with email {email}.", fg="red")
        sys.exit(1)

    organization = db.session.scalar(
        select(Organization).where(Organization.slug == org_slug)
    )
    if organization is None:
        click.secho(f"No organization with slug {org_slug}.", fg="red")
        sys.exit(1)

    membership = user.membership_for(organization.id)
    if membership is None:
        membership = Membership(organization_id=organization.id, user_id=user.id)
        db.session.add(membership)
    membership.role = role
    db.session.commit()
    click.secho(f"{email} is now {role} of {organization.name}.", fg="green")


@click.command("check-pdf")
@with_appcontext
def check_pdf() -> None:
    """Report whether the PDF renderer's native stack is usable."""
    from .services.renderers import PDF_AVAILABLE, pdf_unavailable_reason

    if PDF_AVAILABLE:
        click.secho("PDF rendering is available (WeasyPrint loaded).", fg="green")
        return
    click.secho("PDF rendering is NOT available.", fg="red")
    click.echo(pdf_unavailable_reason())
    sys.exit(1)


@click.command("demo-data")
@click.option("--org", required=True, help="Organization slug to populate.")
@with_appcontext
def demo_data(org: str) -> None:
    """Create a sample project, bid package and generated scope."""
    from .models import BidPackage, Organization, Project
    from .services import library as library_service
    from .services.scope_builder import ScopeDraft, build_scope

    organization = db.session.scalar(
        select(Organization).where(Organization.slug == org)
    )
    if organization is None:
        click.secho(f"No organization with slug {org}.", fg="red")
        sys.exit(1)

    project = Project(
        organization_id=organization.id,
        name="Riverside Medical Center",
        number="2024-118",
        address="1400 River Road",
        city="Columbus",
        state="OH",
        postal_code="43215",
        owner_name="Riverside Health System",
        architect_name="Whitfield Architects",
        contractor_name=organization.name,
        delivery_method="CMAR",
    )
    db.session.add(project)
    db.session.flush()

    package = BidPackage(
        project_id=project.id,
        organization_id=organization.id,
        number="BP-21A",
        name="Fire Protection",
        division_code="21",
        trade_name="Fire Protection",
    )
    db.session.add(package)
    db.session.commit()

    scope = build_scope(
        ScopeDraft(
            organization_id=organization.id,
            division_code="21",
            trade_name="Fire Protection",
            project_id=project.id,
            bid_package_id=package.id,
            clause_ids=library_service.default_clause_ids(organization.id, "21"),
            spec_section_ids=library_service.default_spec_section_ids(
                organization.id, "21"
            ),
        )
    )
    click.secho(
        f"Created {project.display_title}, {package.display_title} and "
        f"{scope.document_title} with {scope.item_count} items.",
        fg="green",
    )


@click.command("run-worker")
@click.option("--poll", default=2.0, help="Seconds to wait when the queue is empty.")
@click.option("--once", is_flag=True, help="Process at most one job, then exit.")
@click.option("--max-jobs", type=int, default=None, help="Exit after this many jobs.")
@with_appcontext
def run_worker(poll: float, once: bool, max_jobs: int | None) -> None:
    """Render queued documents. Run one or more alongside the web process."""
    from .services.render_queue import run_worker as run

    processed = run(poll_seconds=poll, once=once, max_jobs=max_jobs)
    click.secho(f"Processed {processed} job(s).", fg="green")


@click.command("render-queue")
@with_appcontext
def render_queue_status() -> None:
    """Show the render queue's depth."""
    from .services.render_queue import purge_expired, queue_stats, requeue_stale

    requeued = requeue_stale()
    stats = queue_stats()
    click.echo(
        f"queued={stats['queued']}  running={stats['running']}  "
        f"complete={stats['complete']}  failed={stats['failed']}"
    )
    if requeued:
        click.secho(f"Requeued {requeued} stale job(s).", fg="yellow")
    purged = purge_expired()
    if purged:
        click.echo(f"Purged {purged} expired result(s).")


def main() -> None:  # pragma: no cover - console-script entry point
    """``scopemaker`` console script -- delegates to the Flask CLI."""
    from flask.cli import main as flask_main

    sys.argv[0] = "flask"
    flask_main()
