"""The /metrics endpoint and the counters behind it.

Two things matter here beyond "does it emit numbers": that the endpoint is not
readable without the token, and that label values never contain record ids. The
second one is not cosmetic -- a label per scope id creates a new time series per
document, which is how a Prometheus instance falls over.
"""

from __future__ import annotations

import re

import pytest

from scopemaker.services import metrics


@pytest.fixture(autouse=True)
def _clean_metrics():
    metrics.reset()
    yield
    metrics.reset()


# ---------------------------------------------------------------------------
# Exposition format
# ---------------------------------------------------------------------------

def test_counter_renders_with_labels():
    metrics.increment("things_total", {"kind": "widget"})
    metrics.increment("things_total", {"kind": "widget"})
    metrics.increment("things_total", {"kind": "sprocket"})

    body = metrics.render_exposition()

    assert "# TYPE things_total counter" in body
    assert 'things_total{kind="widget"} 2' in body
    assert 'things_total{kind="sprocket"} 1' in body


def test_counter_without_labels_has_no_braces():
    metrics.increment("bare_total")
    assert "bare_total 1" in metrics.render_exposition()


def test_histogram_buckets_are_cumulative():
    for value in (0.005, 0.02, 0.3, 7.0):
        metrics.observe("latency_seconds", value)

    body = metrics.render_exposition()
    buckets = dict(
        re.findall(r'latency_seconds_bucket\{le="([^"]+)"\} (\d+)', body)
    )

    assert buckets["0.01"] == "1"
    assert buckets["0.05"] == "2"
    assert buckets["0.5"] == "3"
    assert buckets["5.0"] == "3"
    assert buckets["+Inf"] == "4"
    assert "latency_seconds_count 4" in body
    assert "latency_seconds_sum 7.325" in body


def test_histogram_retains_at_most_the_cap():
    for _ in range(metrics.MAX_OBSERVATIONS + 250):
        metrics.observe("capped_seconds", 0.001)

    body = metrics.render_exposition()
    assert f"capped_seconds_count {metrics.MAX_OBSERVATIONS}" in body


def test_exposition_ends_with_a_newline():
    metrics.increment("things_total")
    assert metrics.render_exposition().endswith("\n")


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------

def test_metrics_is_404_without_a_token(client, app):
    app.config["METRICS_TOKEN"] = ""
    response = client.get("/metrics")
    assert response.status_code == 404


def test_metrics_rejects_a_wrong_token(client, app):
    app.config["METRICS_TOKEN"] = "s3cret"
    try:
        assert client.get("/metrics").status_code == 401
        assert client.get(
            "/metrics", headers={"Authorization": "Bearer wrong"}
        ).status_code == 401
        # A prefix of the real token must not pass either.
        assert client.get(
            "/metrics", headers={"Authorization": "Bearer s3c"}
        ).status_code == 401
    finally:
        app.config["METRICS_TOKEN"] = ""


def test_metrics_serves_with_the_right_token(client, app):
    app.config["METRICS_TOKEN"] = "s3cret"
    try:
        client.get("/")
        response = client.get(
            "/metrics", headers={"Authorization": "Bearer s3cret"}
        )
    finally:
        app.config["METRICS_TOKEN"] = ""

    assert response.status_code == 200
    assert response.mimetype == "text/plain"
    body = response.get_data(as_text=True)
    assert "scopemaker_requests_total" in body
    assert "scopemaker_request_seconds_bucket" in body
    # Queue depth is read live rather than accumulated.
    assert "scopemaker_render_queue" in body


def test_requests_are_labelled_by_endpoint_not_path(auth_client, app, scope):
    """Paths carry record ids; endpoints do not."""
    metrics.reset()
    auth_client.get(f"/scopes/{scope.id}")

    body = metrics.render_exposition()
    assert 'endpoint="scopes.edit"' in body
    assert scope.id not in body


def test_status_code_is_recorded(client):
    client.get("/no-such-page")
    assert 'status="404"' in metrics.render_exposition()


def test_static_requests_are_not_counted(client, app):
    metrics.reset()
    client.get("/static/css/app.css")
    assert "scopemaker_requests_total" not in metrics.render_exposition()


# ---------------------------------------------------------------------------
# Wiring into the export path
# ---------------------------------------------------------------------------

def test_export_records_cache_hit_and_miss(auth_client, scope):
    metrics.reset()

    first = auth_client.get(f"/exports/{scope.id}.md")
    second = auth_client.get(f"/exports/{scope.id}.md")
    assert first.headers["X-Render-Cache"] == "miss"
    assert second.headers["X-Render-Cache"] == "hit"

    body = metrics.render_exposition()
    assert 'scopemaker_export_cache_total{format="md",result="miss"} 1' in body
    assert 'scopemaker_export_cache_total{format="md",result="hit"} 1' in body
    assert 'scopemaker_renders_total{format="md",result="ok"} 1' in body
    assert 'scopemaker_render_seconds_count{format="md"} 1' in body


def test_failed_render_is_counted(app, db, scope, user):
    from scopemaker.models.render import RenderJob
    from scopemaker.services import render_queue

    metrics.reset()
    job = RenderJob(
        organization_id=scope.organization_id,
        scope_id=scope.id,
        requested_by_id=user.id,
        format="docx",
        fingerprint="deadbeef",
        status="running",
    )
    db.session.add(job)
    db.session.commit()

    def explode(*args, **kwargs):
        raise RuntimeError("no")

    import scopemaker.services.renderers as renderers

    original = renderers.render_docx
    renderers.render_docx = explode
    try:
        render_queue.render_now(job)
    finally:
        renderers.render_docx = original

    assert job.status == "failed"
    body = metrics.render_exposition()
    assert 'scopemaker_renders_total{format="docx",result="failed"} 1' in body
