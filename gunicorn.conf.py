"""Gunicorn configuration.

Tuned for a request mix that is mostly cheap page renders punctuated by
occasional PDF generation, which is CPU-bound and can take a second or two.
"""

from __future__ import annotations

import multiprocessing
import os

bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:8000")

# Sync workers: WeasyPrint and python-docx are CPU-bound and release no GIL
# time worth sharing, so processes beat threads here.
workers = int(os.environ.get("WEB_CONCURRENCY", (multiprocessing.cpu_count() * 2) + 1))
worker_class = "sync"
threads = int(os.environ.get("GUNICORN_THREADS", 1))

# Long enough that a large PDF finishes, short enough to shed a wedged worker.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", 120))
graceful_timeout = 30
keepalive = 5

# Recycle workers periodically so a slow leak cannot accumulate.
max_requests = int(os.environ.get("GUNICORN_MAX_REQUESTS", 1000))
max_requests_jitter = 100

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info").lower()
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(L)ss "%(a)s"'

# Reject oversized request lines and headers outright.
limit_request_line = 8190
limit_request_fields = 100
limit_request_field_size = 8190

preload_app = os.environ.get("GUNICORN_PRELOAD", "1") == "1"
forwarded_allow_ips = os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1")


def on_starting(server):  # pragma: no cover - lifecycle hook
    server.log.info("ScopeMaker starting with %s worker(s)", workers)
