# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Builder: compile wheels once so the runtime image needs no toolchain.
# ---------------------------------------------------------------------------
FROM python:3.14-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md ./
COPY scopemaker ./scopemaker

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --upgrade pip setuptools wheel \
    && pip install ".[postgres,server]"

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM python:3.14-slim-bookworm AS runtime

# WeasyPrint renders PDFs through Pango/cairo rather than a browser, so these
# native libraries are what make PDF export work. Without them the app still
# runs and every other format exports; only PDF is disabled.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libharfbuzz0b \
        libcairo2 \
        libgdk-pixbuf-2.0-0 \
        libffi8 \
        libjpeg62-turbo \
        libpq5 \
        shared-mime-info \
        fonts-dejavu-core \
        fonts-liberation2 \
        curl \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FLASK_APP=wsgi:app \
    FLASK_ENV=production

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY scopemaker ./scopemaker
COPY migrations ./migrations
COPY wsgi.py gunicorn.conf.py docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

# Run unprivileged.
RUN useradd --create-home --shell /usr/sbin/nologin --uid 10001 scopemaker \
    && chown -R scopemaker:scopemaker /app
USER scopemaker

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["gunicorn", "--config", "gunicorn.conf.py", "wsgi:app"]
