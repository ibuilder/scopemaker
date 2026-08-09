"""The operator commands.

These are what somebody reaches for when they are provisioning an instance or
when something has gone wrong at an inconvenient hour, which is the worst
possible time to discover that ``grant-role`` exits 0 without doing anything.

The failure paths matter more than the happy ones here: an operator acting on
"done" when nothing happened is worse than a command that refuses.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from scopemaker.models import Membership, Organization, User

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture()
def run(app, db):
    """Invoke a CLI command against the test application."""
    runner = app.test_cli_runner()

    def invoke(*args: str):
        return runner.invoke(args=list(args))

    return invoke


# ---------------------------------------------------------------------------
# create-org / create-user
# ---------------------------------------------------------------------------

def test_create_org(run, db):
    result = run("create-org", "Bridgeport Mechanical")
    assert result.exit_code == 0, result.output

    organization = db.session.scalar(
        select(Organization).where(Organization.name == "Bridgeport Mechanical")
    )
    assert organization is not None
    assert organization.slug in result.output


def test_create_user_makes_the_org_when_it_is_missing(run, db):
    result = run(
        "create-user", "pat@example.com", "--name", "Pat Operator",
        "--org", "new-org", "--role", "admin", "--password", PASSWORD,
    )
    assert result.exit_code == 0, result.output

    user = User.by_email("pat@example.com")
    assert user is not None
    assert user.full_name == "Pat Operator"
    assert [m.role for m in user.memberships] == ["admin"]
    assert user.check_password(PASSWORD)


def test_create_user_joins_an_existing_org_rather_than_duplicating_it(run, db):
    run("create-org", "Shared Builders")
    slug = db.session.scalar(
        select(Organization.slug).where(Organization.name == "Shared Builders")
    )

    run("create-user", "first@example.com", "--org", slug,
        "--role", "admin", "--password", PASSWORD)
    run("create-user", "second@example.com", "--org", slug,
        "--role", "editor", "--password", PASSWORD)

    organizations = db.session.scalars(
        select(Organization).where(Organization.name == "Shared Builders")
    ).all()
    assert len(organizations) == 1, "the second user created a duplicate org"
    assert len(organizations[0].memberships) == 2


def test_create_user_refuses_a_weak_password(run, db):
    result = run("create-user", "weak@example.com", "--org", "o",
                 "--role", "admin", "--password", "short")
    assert result.exit_code == 1
    assert "rejected" in result.output.lower()
    assert User.by_email("weak@example.com") is None


def test_create_user_refuses_a_duplicate_email(run, db):
    run("create-user", "dup@example.com", "--org", "a",
        "--role", "admin", "--password", PASSWORD)
    result = run("create-user", "dup@example.com", "--org", "b",
                 "--role", "admin", "--password", PASSWORD)

    assert result.exit_code == 1
    assert len(db.session.scalars(
        select(User).where(User.email == "dup@example.com")
    ).all()) == 1


def test_create_user_can_grant_superuser(run, db):
    run("create-user", "root@example.com", "--org", "o", "--role", "admin",
        "--password", PASSWORD, "--superuser")
    assert User.by_email("root@example.com").is_superuser


def test_an_unknown_role_is_rejected_by_the_parser(run, db):
    result = run("create-user", "x@example.com", "--org", "o",
                 "--role", "wizard", "--password", PASSWORD)
    assert result.exit_code != 0
    assert User.by_email("x@example.com") is None


# ---------------------------------------------------------------------------
# grant-role
# ---------------------------------------------------------------------------

def test_grant_role_changes_an_existing_membership(run, db, user, organization):
    result = run("grant-role", user.email, organization.slug, "viewer")
    assert result.exit_code == 0, result.output

    db.session.refresh(user)
    assert user.membership_for(organization.id).role == "viewer"


def test_grant_role_adds_a_member_who_is_not_one_yet(run, db, organization):
    from scopemaker.services.accounts import create_user as make_user

    outsider = make_user(email="outside@example.com", full_name="Out Sider",
                         password=PASSWORD)
    db.session.commit()
    assert not outsider.memberships

    result = run("grant-role", outsider.email, organization.slug, "editor")
    assert result.exit_code == 0, result.output

    db.session.refresh(outsider)
    assert [m.role for m in outsider.memberships] == ["editor"]


def test_grant_role_fails_loudly_on_an_unknown_user(run, db, organization):
    result = run("grant-role", "ghost@example.com", organization.slug, "admin")
    assert result.exit_code == 1, "silently succeeding here is the dangerous case"
    assert "no user" in result.output.lower()


def test_grant_role_fails_loudly_on_an_unknown_org(run, db, user):
    result = run("grant-role", user.email, "no-such-org", "admin")
    assert result.exit_code == 1
    assert "no organization" in result.output.lower()


def test_grant_role_does_not_duplicate_a_membership(run, db, user, organization):
    run("grant-role", user.email, organization.slug, "editor")
    run("grant-role", user.email, organization.slug, "viewer")

    memberships = db.session.scalars(
        select(Membership).where(
            Membership.user_id == user.id,
            Membership.organization_id == organization.id,
        )
    ).all()
    assert len(memberships) == 1
    assert memberships[0].role == "viewer"


# ---------------------------------------------------------------------------
# Library and diagnostics
# ---------------------------------------------------------------------------

def test_seed_library_is_idempotent(run, db):
    first = run("seed-library")
    assert first.exit_code == 0, first.output

    second = run("seed-library")
    assert second.exit_code == 0
    assert "0 created" in second.output, (
        f"re-seeding created rows again: {second.output}"
    )


def test_seed_library_no_update_still_succeeds(run, db):
    result = run("seed-library", "--no-update")
    assert result.exit_code == 0, result.output


def test_check_pdf_reports_without_crashing(run):
    """It must answer even when the native stack is missing -- that is the
    whole point of asking."""
    result = run("check-pdf")
    assert result.exit_code in (0, 1)
    assert result.output.strip()


def test_render_queue_reports_depth(run, db):
    result = run("render-queue")
    assert result.exit_code == 0, result.output
    for field in ("queued", "running", "complete", "failed"):
        assert field in result.output


def test_demo_data_builds_a_scope(run, db, organization):
    result = run("demo-data", "--org", organization.slug)
    assert result.exit_code == 0, result.output
    assert "items" in result.output

    from scopemaker.models import Scope

    scopes = db.session.scalars(
        select(Scope).where(Scope.organization_id == organization.id)
    ).all()
    assert scopes and scopes[0].item_count > 0


def test_demo_data_refuses_an_unknown_org(run, db):
    result = run("demo-data", "--org", "no-such-org")
    assert result.exit_code == 1
    assert "no organization" in result.output.lower()


def test_run_worker_processes_nothing_and_exits_when_idle(run, db):
    """--once must return on an empty queue rather than block forever."""
    result = run("run-worker", "--once")
    assert result.exit_code == 0, result.output
    assert "0 job" in result.output


def test_run_worker_renders_a_queued_job(run, db, scope, user):
    from scopemaker.models.render import RenderJob

    db.session.add(
        RenderJob(
            organization_id=scope.organization_id,
            scope_id=scope.id,
            requested_by_id=user.id,
            format="md",
            fingerprint="cli-test",
            status="queued",
        )
    )
    db.session.commit()

    result = run("run-worker", "--once")
    assert result.exit_code == 0, result.output
    assert "1 job" in result.output

    job = db.session.scalar(
        select(RenderJob).where(RenderJob.fingerprint == "cli-test")
    )
    assert job.status == "complete"
    assert job.result
