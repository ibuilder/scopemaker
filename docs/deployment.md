# Deployment

## Before you expose it

1. **Generate both secrets.** Production refuses to start without them.

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(64))"          # SECRET_KEY
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"   # ENCRYPTION_KEY
   ```

   `ENCRYPTION_KEY` encrypts stored Procore tokens. Rotating it does not lose data, but
   every Procore connection must be re-authorized.

2. **Use PostgreSQL.** `ProductionConfig` rejects SQLite outright.

   ```
   DATABASE_URL=postgresql+psycopg://user:password@host:5432/scopemaker
   ```

3. **Set `ALLOWED_HOSTS`** to the hostnames you actually serve. Password-reset links
   and OAuth redirects are built from the `Host` header.

4. **Behind a proxy**, set `TRUSTED_PROXY_COUNT` to the number of hops and
   `FORCE_HTTPS=1`. Leaving the count at 0 means `X-Forwarded-For` is ignored — which
   is correct when nothing is in front, and wrong (rate limiting sees one IP) when
   something is.

5. **Choose a registration mode.** `open` lets anyone sign up. For an internal
   deployment use `invite` or `closed`.

## Docker Compose

```bash
cp .env.example .env      # fill in SECRET_KEY and ENCRYPTION_KEY
docker compose up -d --build
docker compose exec web flask create-user you@example.com --org acme --role admin
```

The entrypoint applies migrations and seeds the library on boot. When running more
than one replica, set `RUN_MIGRATIONS=0` and `SEED_LIBRARY=0` on the web service and
run them once from a separate job — otherwise every replica races to migrate the same
database.

## Without Docker

```bash
pip install ".[postgres,server]"
export FLASK_APP=wsgi:app FLASK_ENV=production
flask db upgrade
flask seed-library
gunicorn --config gunicorn.conf.py wsgi:app
```

Tuning lives in `gunicorn.conf.py` and is environment-overridable: `WEB_CONCURRENCY`,
`GUNICORN_TIMEOUT`, `GUNICORN_MAX_REQUESTS`. Sync workers are used deliberately —
WeasyPrint and python-docx are CPU-bound.

## PDF rendering

WeasyPrint needs Pango, cairo and GDK-PixBuf. They are in the Docker image. Elsewhere:

```bash
# Debian / Ubuntu
apt-get install -y libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b \
                   libcairo2 libgdk-pixbuf-2.0-0 shared-mime-info fonts-dejavu-core

# macOS
brew install pango libffi
```

On Windows, install the
[GTK3 runtime](https://github.com/tschoonj/GTK-for-Windows-Runtime-Environment-Installer/releases)
and restart your shell.

Verify with `flask check-pdf`. Without it the application still runs and DOCX, HTML,
Markdown and JSON all export; only PDF is disabled, and the UI says so.

Install the fonts your exhibits reference. The Docker image ships DejaVu and
Liberation; `document.css` asks for Times New Roman and falls back to the default
serif, so add `fonts-crosextra-caladea`/`fonts-liberation2` if you need metric
compatibility with Word output.

## Render workers

Every export is cached by a fingerprint of the document's actual content, so an
unchanged scope is served from stored bytes and renders nothing. That absorbs most of
the load: the same exhibit gets downloaded repeatedly while people review it.

When a render *is* needed it can either happen inline (the default) or on a worker:

```bash
RENDER_ASYNC=1        # request enqueues a job and returns a waiting page
flask run-worker      # one or more of these alongside the web process
```

`RENDER_ASYNC=1` without a running worker means exports never complete, so it is off
unless you turn it on. Compose starts a worker and sets it for you.

Workers claim jobs with a conditional `UPDATE`, so several can share one queue with no
extra infrastructure — no Redis, no broker, just the database you already run:

```bash
docker compose up -d --scale worker=3
```

A worker that dies mid-render leaves its job `running`; the next worker requeues it
after ten minutes and gives up after three attempts. Cached results are dropped after
seven days. Check the depth with:

```bash
flask render-queue
```

## Metrics

`/metrics` serves Prometheus text exposition. It returns **404 until `METRICS_TOKEN` is
set**, and then requires the token:

```bash
curl -H "Authorization: Bearer $METRICS_TOKEN" https://scopemaker.example.com/metrics
```

| Metric | Type | Labels |
|---|---|---|
| `scopemaker_requests_total` | counter | `endpoint`, `method`, `status` |
| `scopemaker_request_seconds` | histogram | `endpoint`, `method` |
| `scopemaker_renders_total` | counter | `format`, `result` |
| `scopemaker_render_seconds` | histogram | `format` |
| `scopemaker_export_cache_total` | counter | `format`, `result` (`hit`/`miss`) |
| `scopemaker_render_queue` | gauge | `status` |

Requests are labelled by Flask endpoint, never by path — paths contain scope ids, and a
time series per document would take the Prometheus instance down with it.

Counters live in each process's memory, so every gunicorn worker reports its own and
Prometheus sums across targets. A restart resets them, which is what `rate()` expects.

Two things worth alerting on: `scopemaker_render_queue{status="failed"}` climbing, and
`scopemaker_render_queue{status="queued"}` staying above zero — the second means the
worker is gone or wedged.

## Health checks

| Endpoint | Meaning |
|---|---|
| `/healthz` | Process is alive. Does not touch the database. Use for liveness. |
| `/readyz` | Database reachable; also reports PDF availability. Use for readiness. |

## Logging

`LOG_FORMAT=json` emits one JSON object per line with a `request_id` that is also
returned in the `X-Request-ID` response header. Unhandled exceptions log an incident id
that is shown on the error page, so a user's screenshot maps to a traceback.

## Measuring performance

`scripts/load_test.py` builds a throwaway dataset, drives the application in process and
reports latency percentiles with the **query count** for each page:

```bash
python scripts/load_test.py --scopes 40 --runs 20
```

Query count is the number to watch. Latency on a laptop with SQLite says little about
production, but a page that issues 70 queries is a defect wherever it runs. For a
realistic concurrency figure, point `DATABASE_URL` at PostgreSQL and pass
`--concurrency 8`; SQLite serialises writers and will report lock contention instead of
application cost.

## Backups

Back up the PostgreSQL database. That is all the state there is — the clause library is
re-seedable from the image, and no files are written at runtime.

This is not advice we have only written down. `scripts/backup_drill.py` runs on every
push: it populates a database, `pg_dump`s it, drops the schema, restores, and asserts
both that the row counts match and that the restored database renders a reference
exhibit byte for byte. Row counts alone would pass with the encrypted columns coming
back as mush, which is why it re-renders.

Run it against your own instance before you rely on it:

```bash
DATABASE_URL=postgresql+psycopg://... python scripts/backup_drill.py
```

It is destructive — it drops the schema it just dumped — so point it at a scratch
database, never production.

Issued scopes are stored as immutable revision snapshots. If a scope is ever disputed,
the exact text that went out is in `scope_revisions`, not reconstructed.

## Upgrading

```bash
docker compose pull && docker compose up -d
```

The entrypoint runs `flask db upgrade` and re-seeds. Seeding is idempotent and keyed on
`system_key`: a corrected clause in a new release updates in place rather than
duplicating, and your own clauses and suppressions are never touched.

## Reverse proxy example

```nginx
server {
    listen 443 ssl http2;
    server_name scopes.example.com;

    client_max_body_size 16m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;   # allow for large PDF renders
    }
}
```

With that in front, set `TRUSTED_PROXY_COUNT=1`, `FORCE_HTTPS=1` and
`ALLOWED_HOSTS=scopes.example.com`.
