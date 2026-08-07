<div align="center">

# ScopeMaker

**Construction scope of work exhibits, generated properly.**

Pick a CSI division, choose from a curated clause library, edit anything you need to,
and export a paginated PDF, an editable Word file, Markdown or JSON — all carrying
identical clause numbering.

[![CI](https://github.com/ibuilder/scopemaker/actions/workflows/ci.yml/badge.svg)](https://github.com/ibuilder/scopemaker/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![Flask 3](https://img.shields.io/badge/flask-3.x-000000)](https://flask.palletsprojects.com/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[Documentation](https://ibuilder.github.io/scopemaker/) ·
[Quick start](#quick-start) ·
[API](#json-api) ·
[Deployment](docs/deployment.md)

</div>

---

## What this is

Writing the Scope of Work exhibit that gets attached to a subcontract is repetitive,
and getting it wrong is expensive. Miss the Division 07 firestopping that your fire
protection subcontractor is contractually responsible for, and you have a scope gap
that somebody pays for later.

ScopeMaker turns that document into structured data:

- a **clause library** of universal obligations plus trade-specific inclusions,
  exclusions and clarifications, organised by CSI MasterFormat division;
- **cross-referenced specification sections**, so a Division 21 package is
  automatically offered the Division 07 firestopping and Division 08 access doors it
  actually carries;
- a **document model** where every line is an editable, reorderable, numberable item;
- **exports** — PDF, DOCX, HTML, Markdown, JSON — rendered from one numbered tree, so
  clause `3.2.4` means the same sentence in every one of them.

> [!NOTE]
> **This repository was previously `procore-exhibit-generator`.** v1.0.0 replaced a
> browser-only prototype — a handful of static HTML pages whose JavaScript was never
> committed, an OAuth client secret kept in `localStorage`, and a "PDF export" that
> screenshotted the page onto a single A4 sheet. None of that survives, and the
> product is now **ScopeMaker**: the scope engine is the point, and the Procore
> connector is one optional integration among others. Old URLs redirect.
> See [What changed in v1.0.0](#what-changed-in-v100).

---

## Quick start

### Docker (recommended)

```bash
git clone https://github.com/ibuilder/scopemaker.git
cd scopemaker
cp .env.example .env
```

Generate the two required secrets and put them in `.env`:

```bash
python -c "import secrets; print('SECRET_KEY=' + secrets.token_urlsafe(64))"
```

```bash
python -c "from cryptography.fernet import Fernet; print('ENCRYPTION_KEY=' + Fernet.generate_key().decode())"
```

Then bring it up:

```bash
docker compose up --build
```

The app is on <http://localhost:8000>. Migrations run and the clause library is seeded
automatically on first boot.

### Local development

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env
export FLASK_APP=wsgi:app FLASK_ENV=development
flask db upgrade
flask seed-library
flask run
```

Create yourself an account and organization:

```bash
flask create-user you@example.com --name "Your Name" --org acme --role admin
```

Want something to look at immediately?

```bash
flask demo-data --org acme
```

That builds a sample project, a `BP-21A Fire Protection` bid package and a fully
generated Division 21 exhibit.

> [!IMPORTANT]
> **PDF export needs native libraries.** WeasyPrint renders through Pango and cairo
> rather than a browser. They are already in the Docker image; on a bare machine
> install them first, or PDF export will be disabled while every other format keeps
> working. Run `flask check-pdf` to see where you stand.
>
> | Platform | Command |
> |---|---|
> | Debian / Ubuntu | `apt-get install libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libcairo2 libgdk-pixbuf-2.0-0` |
> | macOS | `brew install pango libffi` |
> | Windows | Install the [GTK3 runtime](https://github.com/tschoonj/GTK-for-Windows-Runtime-Environment-Installer/releases), then restart your shell |

---

## How a scope gets generated

```
Division 21 (Fire Suppression)
        │
        ├── universal clauses ────────┐
        ├── Division 21 clauses ──────┤
        ├── Division 01 spec sections ┤──▶ ScopeDocument ──▶ outline numbering ──┐
        └── cross-referenced sections ┘         │                                │
             (07 firestopping,                  │                                │
              08 access doors,             editable items                        │
              28 fire alarm…)              in the browser                        │
                                                                                 ▼
                                          PDF · DOCX · HTML · Markdown · JSON (identical numbering)
```

The generated exhibit follows the structure the industry actually writes:

| § | Section | Content |
|---|---|---|
| 1 | Intent | Boilerplate, with project facts merged in |
| 2 | Scope of Work Summary | The furnish-and-install statement, means and methods, and the applicable specification sections nested beneath (2.3.1, 2.3.2 …) |
| 3 | Trade Specific Scope of Work Items | Inclusions |
| 4 | Trade Specific Scope Exclusions | Exclusions |
| 5 | Clarifications and Assumptions | Basis of the price |
| 6–11 | Allowances · Alternates · Unit Prices · Schedule · Safety · Closeout | Optional |
| 12 | Recap of Contract Amount | Base bid, alternates, adjustments, total |

Every one of those lines is a database row you can reword, reorder, nest, promote,
demote or delete before export.

---

## Features

**Coverage analysis** *(new in 1.1)*
- Project-level matrix showing which specification sections are claimed by **nobody**
  (a gap), by **two trades** (bought twice), and which are **shared by design** —
  every trade firestops its own penetrations, but somebody still has to decide who
  paints the exposed sprinkler pipe
- Flags exclusions that hand work to a division with no scope on the project: fire
  protection excluding the fire alarm is only safe if Division 28 is actually coming
- CSV export for buyout meetings, plus `GET /api/v1/projects/{id}/coverage`

**Performance and operations** *(new in 1.4)*
- Exports are cached by a fingerprint of the document's content, so an unchanged
  scope is served from stored bytes — a repeat DOCX download went from 341 ms to
  13 ms, and from 20 queries to 7
- Optional worker process (`RENDER_ASYNC=1` + `flask run-worker`) takes rendering
  off the request path; workers claim jobs with a conditional `UPDATE`, so several
  can share one queue with no broker to run
- Optimistic locking on scopes: two people editing the same document get a clear
  conflict instead of one silently overwriting the other
- Prometheus metrics at `/metrics`, gated on `METRICS_TOKEN` and labelled by
  endpoint rather than path
- `scripts/load_test.py` reports latency percentiles *and query counts* per page

**Scope generation**
- Full CSI MasterFormat 2020 division list — all 50 numbers, with 15–20, 24, 29, 30,
  36–39, 47 and 49 correctly marked reserved and never offered for selection
- 236 shipped clauses across universal obligations and 20+ trades
- 139 specification sections with cross-division references
- Reusable templates: save any scope's structure and language and apply it again

**Editing**
- Live preview rendered from the same stylesheet as the PDF
- Drag-to-reorder with cycle detection, inline editing, per-section enable/disable
- Configurable outline numbering: legal (`1.`, `1.1`, `1.1.1`) or outline
  (`1.`, `A.`, `1)`, `a)`), with per-level styles

**Documents**
- **PDF** via WeasyPrint: real paged media, running headers and footers,
  `Page N of M`, selectable and searchable text
- **DOCX** via python-docx: character-level formatting preserved, live page-number
  fields, ready to redline
- **JSON** and **Markdown** for archiving, diffing revisions and downstream systems

**Security and governance** *(hardened in 1.2–1.3)*
- Two-factor authentication with single-use recovery codes; the enrolment QR is
  an inline SVG, so the shared secret never leaves the server
- Organization policy to require two-factor or single sign-on, enforced on
  sessions that are already open rather than only at the next login
- Append-only audit log of every privileged action, with CSV export
- See [SECURITY.md](SECURITY.md) for the full posture and the hardening checklist

**Accounts and access**
- Password reset with single-use expiring tokens; completing one signs out every
  existing session, so a reset evicts an attacker rather than running alongside them
- Per-account lockout with a growing backoff — an IP limit does nothing against
  credential stuffing spread across addresses
- "Sign out everywhere else", and automatic session revocation on password change
- Email on `smtplib` with a console backend, so development needs no mail server:
  the reset link appears in the log

**Governance**
- Organizations with `viewer` / `editor` / `admin` roles, invitations, and OIDC SSO
- Issuing a scope freezes an immutable revision; further edits create a new version
- Tenant isolation enforced in one place and covered by tests

**Integration**
- Token-authenticated JSON API
- No CDN dependencies — every asset is served from the app, so it runs air-gapped
- Optional connectors, all off by default — see [Integrations](docs/integrations.md)

---

## JSON API

Create a token under **Admin → API tokens**, then:

```bash
curl -H "Authorization: Bearer smk_..." https://your-host/api/v1/divisions
```

Generate a complete Division 26 scope in one call:

```bash
curl -X POST https://your-host/api/v1/scopes \
  -H "Authorization: Bearer smk_..." \
  -H "Content-Type: application/json" \
  -d '{"division_code": "26", "use_defaults": true, "title": "Scope of Work"}'
```

Download it as a PDF:

```bash
curl -H "Authorization: Bearer smk_..." -o exhibit.pdf \
  https://your-host/api/v1/scopes/<id>/export/pdf
```

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/v1/divisions` | Selectable CSI divisions, categories and statuses |
| `GET` | `/api/v1/library/clauses?division=21` | Clauses available for a division |
| `GET` | `/api/v1/library/spec-sections?division=21` | Specification sections, including cross-references |
| `GET` `POST` | `/api/v1/scopes` | List / generate scopes |
| `GET` `PATCH` | `/api/v1/scopes/{id}` | Full numbered document / update |
| `POST` | `/api/v1/scopes/{id}/issue` `/revise` | Freeze a version / open the next |
| `GET` | `/api/v1/scopes/{id}/export/{pdf\|docx\|html\|md\|json}` | Download |
| `GET` `POST` | `/api/v1/projects` | Projects and bid packages |

Errors always come back as `{"error": {"code": ..., "message": ..., "details": ...}}`.
Full reference: [docs/api.md](docs/api.md).

---

## Configuration

Everything is environment-driven; see [`.env.example`](.env.example) for the annotated
list. The settings that matter most:

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Session signing. **Required in production** |
| `ENCRYPTION_KEY` | Fernet key encrypting third-party tokens at rest. **Required in production** |
| `DATABASE_URL` | PostgreSQL in production; SQLite is refused there |
| `ALLOWED_HOSTS` | Comma-separated hostnames; blocks Host-header poisoning |
| `REGISTRATION_MODE` | `open`, `invite` or `closed` |
| `TRUSTED_PROXY_COUNT` | Number of proxies whose `X-Forwarded-*` to trust |
| `RENDER_ASYNC` | Render documents on a worker instead of in the request. Needs `flask run-worker` |
| `METRICS_TOKEN` | Enables `/metrics`; without it the endpoint is 404 |
| `PROCORE_ENABLED` | Off by default; the app is fully usable without it |
| `OIDC_ENABLED` | Off by default |

`ProductionConfig` refuses to boot without the secrets, and refuses SQLite. A
misconfigured deploy fails at startup rather than quietly leaking sessions.

---

## Commands

| Command | Does |
|---|---|
| `flask db upgrade` | Apply migrations |
| `flask seed-library` | Load or refresh the shipped clause library (idempotent) |
| `flask create-user EMAIL --org SLUG --role admin` | Create a user and organization |
| `flask grant-role EMAIL SLUG ROLE` | Change a role |
| `flask check-pdf` | Report whether PDF rendering is usable |
| `flask demo-data --org SLUG` | Create a sample project, package and scope |
| `flask run-worker` | Render queued documents; run one or more alongside the web process |
| `flask render-queue` | Show queue depth, requeue stale jobs, purge expired results |

---

## Development

```bash
pytest                     # full suite
pytest -m pdf              # PDF rendering (needs the native stack)
ruff check .               # lint
mypy scopemaker            # types
```

CI runs the suite on Python 3.11 and 3.12 with the WeasyPrint libraries installed, so
the PDF tests execute for real; it also exercises the migrations against PostgreSQL
in both directions, fails the build if the models have drifted from the migrations,
and builds the Docker image.

Architecture notes are in [docs/architecture.md](docs/architecture.md); the clause
library format is documented in [docs/clause-library.md](docs/clause-library.md);
optional connectors are in [docs/integrations.md](docs/integrations.md).

---

## What changed in v1.0.0

| Prototype | v1.0.0 |
|---|---|
| 5 static HTML pages; the four referenced `js/*.js` files were never committed, so nothing ran | Flask 3 application, app factory, 9 blueprints, service layer |
| Procore **client secret stored in `localStorage`** | Server-side OAuth only; tokens Fernet-encrypted at rest, secret never leaves the server |
| Traditional Procore service accounts (retired 2025-03-18) | Authorization-code grant plus Developer Managed Service Accounts |
| "PDF" = `html2canvas` screenshot pasted onto one A4 page — no pagination, no selectable text | WeasyPrint paged media: real pagination, running headers/footers, `Page N of M`, searchable text |
| DOCX export was `alert('would be implemented here')` | python-docx with formatting runs and live page-number fields |
| Hardcoded Fire Protection sample text | 236-clause library across 20+ trades, plus 139 cross-referenced spec sections |
| 16 hand-typed divisions, several of which do not exist | Canonical MasterFormat 2020, reserved numbers excluded and tested |
| Browser `localStorage` "database" | PostgreSQL, SQLAlchemy 2.0, Alembic migrations |
| No accounts | Organizations, roles, invitations, OIDC SSO, immutable revisions |
| No tests | 214 tests, CI on two Python versions, PostgreSQL migration checks |
| 8 CDN `<script>` tags | Zero external dependencies at runtime; strict `default-src 'self'` CSP |

---

## License

MIT — see [LICENSE](LICENSE).

Section titles from CSI MasterFormat are used for identification. The complete
MasterFormat section list is published and copyrighted by the
[Construction Specifications Institute](https://www.csiresources.org/); load your own
project specification index for authoritative numbering.

This project is not affiliated with or endorsed by Procore Technologies, Inc.
