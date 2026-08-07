"""Prometheus metrics, without a hard dependency on prometheus_client.

Text exposition format is simple enough to emit directly, and adding a required
dependency for something most self-hosters will not scrape is a poor trade.
Counters live in process memory, which means each gunicorn worker reports its
own -- that is normal for this exposition style, and Prometheus sums across
targets.

The endpoint is deliberately not public: it reports request volume, render
timings and queue depth, which is operational detail rather than something to
hand to anonymous visitors.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict

from flask import Flask, Response, current_app, g, request

_lock = threading.Lock()

# name -> {labels tuple -> value}
_counters: dict[str, dict[tuple, float]] = defaultdict(lambda: defaultdict(float))
_histograms: dict[str, dict[tuple, list[float]]] = defaultdict(lambda: defaultdict(list))

#: Latency buckets in seconds. Chosen around what this application actually
#: does: sub-100ms page renders, and PDF generation in the 0.5-3s range.
BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

#: Cap retained observations so a long-running process cannot grow unbounded.
MAX_OBSERVATIONS = 5000


def increment(name: str, labels: dict[str, str] | None = None, amount: float = 1.0) -> None:
    key = tuple(sorted((labels or {}).items()))
    with _lock:
        _counters[name][key] += amount


def observe(name: str, value: float, labels: dict[str, str] | None = None) -> None:
    key = tuple(sorted((labels or {}).items()))
    with _lock:
        series = _histograms[name][key]
        series.append(value)
        if len(series) > MAX_OBSERVATIONS:
            del series[: len(series) - MAX_OBSERVATIONS]


def reset() -> None:
    """Used by tests."""
    with _lock:
        _counters.clear()
        _histograms.clear()


def _format_labels(pairs: tuple, extra: dict[str, str] | None = None) -> str:
    items = dict(pairs)
    if extra:
        items.update(extra)
    if not items:
        return ""
    inner = ",".join(
        f'{k}="{str(v).replace(chr(92), chr(92) * 2)}"' for k, v in sorted(items.items())
    )
    return "{" + inner + "}"


def render_exposition() -> str:
    """Everything collected so far, in Prometheus text format."""
    lines: list[str] = []

    with _lock:
        counters = {name: dict(series) for name, series in _counters.items()}
        histograms = {
            name: {key: list(values) for key, values in series.items()}
            for name, series in _histograms.items()
        }

    for name, series in sorted(counters.items()):
        lines.append(f"# TYPE {name} counter")
        for key, value in sorted(series.items()):
            lines.append(f"{name}{_format_labels(key)} {value:g}")

    for name, series in sorted(histograms.items()):
        lines.append(f"# TYPE {name} histogram")
        for key, values in sorted(series.items()):
            if not values:
                continue
            ordered = sorted(values)
            cumulative = 0
            index = 0
            for bucket in BUCKETS:
                while index < len(ordered) and ordered[index] <= bucket:
                    cumulative += 1
                    index += 1
                lines.append(
                    f"{name}_bucket{_format_labels(key, {'le': str(bucket)})} {cumulative}"
                )
            lines.append(
                f"{name}_bucket{_format_labels(key, {'le': '+Inf'})} {len(ordered)}"
            )
            lines.append(f"{name}_sum{_format_labels(key)} {sum(ordered):g}")
            lines.append(f"{name}_count{_format_labels(key)} {len(ordered)}")

    return "\n".join(lines) + "\n"


def _gauges() -> str:
    """Values read live rather than accumulated."""
    from .render_queue import queue_stats

    lines = ["# TYPE scopemaker_render_queue gauge"]
    try:
        for status, count in queue_stats().items():
            lines.append(f'scopemaker_render_queue{{status="{status}"}} {count}')
    except Exception:
        lines.append('scopemaker_render_queue{status="unavailable"} 0')
    return "\n".join(lines) + "\n"


def register(app: Flask) -> None:
    """Install request timing and the /metrics endpoint."""

    @app.before_request
    def _start_timer() -> None:
        g._metrics_started = time.perf_counter()

    @app.after_request
    def _record(response: Response) -> Response:
        started = getattr(g, "_metrics_started", None)
        if started is None or request.path.startswith("/static/"):
            return response

        # Label on the endpoint, never the raw path: paths contain record ids,
        # and one time series per scope id would melt any Prometheus.
        endpoint = request.endpoint or "unknown"
        labels = {"endpoint": endpoint, "method": request.method}
        increment(
            "scopemaker_requests_total",
            {**labels, "status": str(response.status_code)},
        )
        observe("scopemaker_request_seconds", time.perf_counter() - started, labels)
        return response

    @app.route("/metrics")
    def metrics():
        token = current_app.config.get("METRICS_TOKEN")
        if token:
            supplied = request.headers.get("Authorization", "")
            from ..security import constant_time_equals

            if not supplied.startswith("Bearer ") or not constant_time_equals(
                supplied[7:], token
            ):
                return Response("Unauthorized\n", status=401, mimetype="text/plain")
        elif not current_app.debug:
            # Refusing by default is the safer failure: operational detail
            # should not be readable by anyone who finds the URL.
            return Response(
                "Set METRICS_TOKEN to enable this endpoint.\n",
                status=404,
                mimetype="text/plain",
            )

        body = render_exposition() + _gauges()
        return Response(body, mimetype="text/plain; version=0.0.4; charset=utf-8")
