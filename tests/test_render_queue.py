"""Render caching, the job queue, and concurrent-edit safety.

Rendering a PDF costs a second or two of CPU and pins a synchronous worker.
Caching absorbs most of that in practice; the queue handles the rest. Both are
tested here, along with the optimistic lock that stops two people silently
overwriting each other.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm.exc import StaleDataError

from scopemaker.models import RenderJob, Scope
from scopemaker.models.base import utcnow
from scopemaker.models.render import COMPLETE, FAILED, MAX_ATTEMPTS, QUEUED, RUNNING
from scopemaker.services import render_queue

# ---------------------------------------------------------------------------
# Fingerprinting -- what decides whether a cached render is still valid
# ---------------------------------------------------------------------------

def test_fingerprint_is_stable_for_an_unchanged_scope(db, scope, organization):
    first = render_queue.fingerprint(scope, organization)
    second = render_queue.fingerprint(scope, organization)
    assert first == second


def test_editing_the_scope_changes_the_fingerprint(db, scope, organization, user):
    before = render_queue.fingerprint(scope, organization)
    scope.title = "Revised Scope of Work"
    scope.updated_by_id = user.id
    db.session.commit()
    assert render_queue.fingerprint(scope, organization) != before


def test_editing_an_item_changes_the_fingerprint(auth_client, db, scope, organization):
    before = render_queue.fingerprint(scope, organization)
    auth_client.post(
        f"/scopes/{scope.id}/sections/inclusions/items",
        data={"text_html": "A newly added obligation.", "submit": "Save"},
        follow_redirects=True,
    )
    db.session.refresh(scope)
    assert render_queue.fingerprint(scope, organization) != before, (
        "an edit that does not move the fingerprint would serve a stale document"
    )


def test_renaming_the_organization_changes_the_fingerprint(db, scope, organization):
    """The organization's name is printed on the exhibit."""
    before = render_queue.fingerprint(scope, organization)
    organization.legal_name = "Meridian Construction Group, LLC"
    db.session.commit()
    assert render_queue.fingerprint(scope, organization) != before


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

def test_second_download_is_served_from_cache(auth_client, db, scope):
    first = auth_client.get(f"/exports/{scope.id}.docx")
    assert first.status_code == 200
    assert first.headers["X-Render-Cache"] == "miss"

    second = auth_client.get(f"/exports/{scope.id}.docx")
    assert second.status_code == 200
    assert second.headers["X-Render-Cache"] == "hit"
    assert second.data == first.data


def test_an_edit_invalidates_the_cache(auth_client, db, scope):
    auth_client.get(f"/exports/{scope.id}.md")
    assert auth_client.get(f"/exports/{scope.id}.md").headers["X-Render-Cache"] == "hit"

    auth_client.post(
        f"/scopes/{scope.id}/sections/inclusions/items",
        data={"text_html": "Something new that must appear in the export.",
              "submit": "Save"},
        follow_redirects=True,
    )

    after = auth_client.get(f"/exports/{scope.id}.md")
    assert after.headers["X-Render-Cache"] == "miss"
    assert b"Something new that must appear" in after.data


def test_formats_are_cached_independently(auth_client, db, scope):
    auth_client.get(f"/exports/{scope.id}.md")
    assert auth_client.get(f"/exports/{scope.id}.md").headers["X-Render-Cache"] == "hit"
    # A different format has not been rendered yet.
    assert auth_client.get(f"/exports/{scope.id}.json").headers["X-Render-Cache"] == "miss"


def test_expired_results_are_not_served(auth_client, db, scope):
    from datetime import timedelta

    auth_client.get(f"/exports/{scope.id}.md")
    job = db.session.query(RenderJob).filter_by(format="md").one()
    job.expires_at = utcnow() - timedelta(minutes=1)
    db.session.commit()

    assert auth_client.get(f"/exports/{scope.id}.md").headers["X-Render-Cache"] == "miss"


def test_cache_is_scoped_to_the_scope(auth_client, db, scope, organization, user, project):
    from scopemaker.services import library as library_service
    from scopemaker.services.scope_builder import ScopeDraft, build_scope

    other = build_scope(
        ScopeDraft(
            organization_id=organization.id, division_code="26",
            project_id=project.id, created_by_id=user.id,
            clause_ids=library_service.default_clause_ids(organization.id, "26"),
        )
    )
    auth_client.get(f"/exports/{scope.id}.md")
    assert auth_client.get(f"/exports/{other.id}.md").headers["X-Render-Cache"] == "miss"


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------

def test_enqueue_then_render(db, scope, organization, user):
    job = render_queue.enqueue(scope, "md", organization=organization, user_id=user.id)
    assert job.status == QUEUED
    assert job.result is None

    render_queue.render_now(job)
    assert job.status == COMPLETE
    assert job.result
    assert job.filename.endswith(".md")
    assert job.duration_ms is not None


def test_enqueue_is_idempotent_for_the_same_document_state(db, scope, organization):
    first = render_queue.enqueue(scope, "md", organization=organization)
    second = render_queue.enqueue(scope, "md", organization=organization)
    assert first.id == second.id, "the same render should not be queued twice"


def test_worker_claims_and_completes(db, scope, organization):
    render_queue.enqueue(scope, "json", organization=organization)
    processed = render_queue.run_worker(once=True, poll_seconds=0)
    assert processed == 1

    job = db.session.query(RenderJob).filter_by(format="json").one()
    assert job.status == COMPLETE
    assert job.result


def test_claiming_is_atomic(db, scope, organization):
    """Two workers must not both take the same job."""
    render_queue.enqueue(scope, "md", organization=organization)

    first = render_queue.claim_one("worker-a")
    second = render_queue.claim_one("worker-b")

    assert first is not None
    assert second is None
    assert first.status == RUNNING
    assert first.worker_id == "worker-a"
    assert first.attempts == 1


def test_worker_on_an_empty_queue_does_nothing(db):
    assert render_queue.run_worker(once=True, poll_seconds=0) == 0


def test_a_stale_claim_is_requeued(db, scope, organization):
    from datetime import timedelta

    render_queue.enqueue(scope, "md", organization=organization)
    job = render_queue.claim_one("worker-that-died")
    job.started_at = utcnow() - timedelta(hours=1)
    db.session.commit()

    assert job.is_stale
    assert render_queue.requeue_stale() == 1
    db.session.refresh(job)
    assert job.status == QUEUED
    assert job.worker_id is None


def test_a_job_is_abandoned_after_repeated_failures(db, scope, organization):
    from datetime import timedelta

    render_queue.enqueue(scope, "md", organization=organization)
    job = db.session.query(RenderJob).one()
    job.attempts = MAX_ATTEMPTS
    job.status = RUNNING
    job.started_at = utcnow() - timedelta(hours=1)
    db.session.commit()

    render_queue.requeue_stale()
    db.session.refresh(job)
    assert job.status == FAILED
    assert "Gave up" in job.error


def test_a_render_failure_is_recorded_not_raised(db, scope, organization, monkeypatch):
    job = render_queue.enqueue(scope, "md", organization=organization)

    def explode(*args, **kwargs):
        raise RuntimeError("renderer exploded")

    monkeypatch.setattr(render_queue, "render_now", render_queue.render_now)
    import scopemaker.services.renderers as renderers

    monkeypatch.setattr(renderers, "render_markdown", explode)
    render_queue.render_now(job)

    assert job.status == FAILED
    assert "exploded" in job.error


def test_deleting_a_scope_removes_its_queued_renders(db, scope, organization):
    """The FK cascade is what stops orphaned jobs existing at all.

    render_now() still guards against a missing scope, but the database makes
    that branch unreachable through normal operation -- which is the right way
    round.
    """
    render_queue.enqueue(scope, "md", organization=organization)
    assert db.session.query(RenderJob).count() == 1

    db.session.delete(scope)
    db.session.commit()
    assert db.session.query(RenderJob).count() == 0


def test_purge_removes_expired_results(db, scope, organization):
    from datetime import timedelta

    job = render_queue.enqueue(scope, "md", organization=organization)
    render_queue.render_now(job)
    job.expires_at = utcnow() - timedelta(days=1)
    db.session.commit()

    assert render_queue.purge_expired() == 1
    assert db.session.query(RenderJob).count() == 0


def test_queue_stats(db, scope, organization):
    render_queue.enqueue(scope, "md", organization=organization)
    stats = render_queue.queue_stats(organization.id)
    assert stats["queued"] == 1
    assert stats["complete"] == 0


def test_oversized_results_are_served_but_not_retained(db, scope, organization):
    """A pathological document must not bloat the table."""
    from scopemaker.models import render as render_model

    job = render_queue.enqueue(scope, "md", organization=organization)
    job.mark_complete(b"x" * (render_model.MAX_RESULT_BYTES + 1), "big.md")
    assert job.status == COMPLETE
    assert job.result is None
    assert job.result_bytes > render_model.MAX_RESULT_BYTES


# ---------------------------------------------------------------------------
# Async mode
# ---------------------------------------------------------------------------

def test_async_mode_returns_a_waiting_page(auth_client, app, db, scope, monkeypatch):
    monkeypatch.setitem(app.config, "RENDER_ASYNC", True)

    response = auth_client.get(f"/exports/{scope.id}.md")
    assert response.status_code == 302
    assert "/exports/jobs/" in response.headers["Location"]

    job = db.session.query(RenderJob).one()
    assert job.status == QUEUED

    waiting = auth_client.get(f"/exports/jobs/{job.id}")
    assert waiting.status_code == 202
    assert b"Preparing your document" in waiting.data

    state = auth_client.get(f"/exports/jobs/{job.id}/state").json
    assert state["ready"] is False
    assert state["status"] == QUEUED

    # Once a worker runs it, the same URL serves the file.
    render_queue.run_worker(once=True, poll_seconds=0)
    ready = auth_client.get(f"/exports/jobs/{job.id}")
    assert ready.status_code == 200
    assert b"EXHIBIT B" in ready.data


def test_async_mode_still_uses_the_cache(auth_client, app, db, scope, monkeypatch):
    auth_client.get(f"/exports/{scope.id}.md")  # renders inline, populates cache
    monkeypatch.setitem(app.config, "RENDER_ASYNC", True)
    response = auth_client.get(f"/exports/{scope.id}.md")
    assert response.status_code == 200
    assert response.headers["X-Render-Cache"] == "hit"


def test_job_endpoints_are_tenant_scoped(db, client, other_org, scope, organization):
    from scopemaker.models import User

    from .conftest import login

    job = render_queue.enqueue(scope, "md", organization=organization)
    rival = db.session.query(User).filter_by(email="rival@rival.example").one()
    login(client, rival.email)

    assert client.get(f"/exports/jobs/{job.id}").status_code == 404
    assert client.get(f"/exports/jobs/{job.id}/state").status_code == 404


# ---------------------------------------------------------------------------
# Optimistic locking
# ---------------------------------------------------------------------------

def test_row_version_increments_on_update(db, scope):
    before = scope.row_version
    scope.title = "Changed"
    db.session.commit()
    assert scope.row_version == before + 1


def test_concurrent_edits_raise_rather_than_silently_overwrite(app, db, scope):
    """Two people editing one scope must not lose one of the edits.

    Uses a genuinely separate Session: doing the competing write through the
    same session would let SQLAlchemy synchronise its own in-memory state and
    the conflict would never arise.
    """
    from sqlalchemy.orm import Session

    scope_id = scope.id
    db.session.commit()
    original_version = scope.row_version

    # Somebody else opens the scope, edits it, and saves first.
    other = Session(bind=db.session.get_bind())
    try:
        theirs = other.get(Scope, scope_id)
        theirs.title = "Edited by somebody else"
        other.commit()
        assert theirs.row_version == original_version + 1
    finally:
        other.close()

    # We are still holding the version we read before they saved.
    assert scope.row_version == original_version
    scope.title = "Edited by us"
    with pytest.raises(StaleDataError):
        db.session.commit()
    db.session.rollback()


def test_a_fresh_read_can_write_again(db, scope):
    scope_id = scope.id
    db.session.execute(
        Scope.__table__.update()
        .where(Scope.__table__.c.id == scope_id)
        .values(row_version=Scope.__table__.c.row_version + 1)
    )
    db.session.commit()
    db.session.expire_all()

    fresh = db.session.get(Scope, scope_id)
    fresh.title = "Edited after re-reading"
    db.session.commit()
    assert fresh.title == "Edited after re-reading"
