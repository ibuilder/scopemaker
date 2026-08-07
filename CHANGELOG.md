# Changelog

All notable changes to this project are documented here.
This project adheres to [Semantic Versioning](https://semver.org/).

## [1.0.0] — 2026-08-06

Complete rewrite. The repository was previously a browser-only prototype: five static
HTML pages whose four referenced JavaScript files were never committed, so nothing in
it ran. v1.0.0 replaces it with a Flask application, and the product is renamed
**ScopeMaker** to reflect that scope generation — not the Procore connector — is the
substance of it.

### Added

**Scope generation**
- Clause library of 236 shipped clauses covering universal obligations plus
  trade-specific inclusions, exclusions and clarifications across 20+ CSI divisions
- 139 specification sections with cross-division references, so a Division 21 package
  is offered the Division 07 firestopping and Division 08 access doors it carries
- Canonical CSI MasterFormat 2020 divisions, with 15–20, 24, 29, 30, 36–39, 47 and 49
  correctly marked reserved and excluded from selection
- Two-step generation wizard with library defaults pre-selected per division
- Reusable scope templates saved from any existing scope
- Placeholder merging of project and bid-package facts into boilerplate, with
  unresolved fields rendered as a visible blank rather than a silently complete sentence

**Editing**
- Section-by-section editor with inline editing, drag-to-reorder (with cycle
  detection), per-section enable/disable and nesting
- Live preview rendered from the same stylesheet as the PDF
- Configurable outline numbering: legal (`1.`, `1.1`, `1.1.1`) or outline
  (`1.`, `A.`, `1)`, `a)`), with per-level styles

**Documents**
- PDF export via WeasyPrint with real paged media: running headers and footers,
  `Page N of M`, page breaks that keep a clause with its number, and selectable text
- DOCX export via python-docx with character-level formatting and live page-number fields
- HTML, Markdown and JSON exports, all carrying identical clause numbering
- Browser print view rendered from the same markup as the PDF

**Platform**
- Organizations with viewer/editor/admin roles, invitations and OIDC single sign-on
- Immutable revision snapshots taken when a scope is issued
- Token-authenticated JSON API with pydantic validation and a consistent error envelope
- PostgreSQL with SQLAlchemy 2.0 and Alembic migrations
- Docker image, Compose file, gunicorn configuration and health/readiness endpoints
- CLI: `seed-library`, `create-user`, `grant-role`, `check-pdf`, `demo-data`
- 214 tests; CI on Python 3.11 and 3.12 with the PDF stack installed, PostgreSQL
  migration round-trip, model/migration drift detection, and a Docker image build

**Procore integration** (optional, disabled by default)
- Server-side authorization-code OAuth and Developer Managed Service Accounts
- Tokens encrypted at rest; automatic refresh and one retry on a mid-session 401
- Project and bid-package sync, with CSI division inferred from package numbering
- Pushing generated exhibits onto Procore commitments as PDF or DOCX

### Fixed

- **The application did not run at all.** `generator.html` and the other pages loaded
  `js/procore-api.js`, `js/exhibit-generator.js`, `js/database-service.js` and
  `js/export-utils.js`; none of those files existed in the repository, nor did the
  `css/styles.css` the README documented.
- **PDF export produced an unusable document.** It rasterised the page with
  `html2canvas` and pasted a single PNG onto one A4 sheet, so anything longer than one
  page was scaled into illegibility and no text was selectable or searchable.
- **DOCX export was a stub** — an `alert()` saying it would be implemented.
- **The CSI division list was wrong**, offering 16 hand-typed entries including numbers
  CSI reserves.
- **The exhibit content was hardcoded**, a fixed Fire Protection sample with no data model.

### Security

- Removed storage of the Procore **client secret in browser `localStorage`**, where any
  XSS could read it. OAuth is now entirely server-side and tokens are Fernet-encrypted
  at rest.
- Migrated off traditional Procore service accounts, retired 2025-03-18, to Developer
  Managed Service Accounts.
- Argon2id password hashing with transparent rehash on login.
- API tokens stored as hashes only; the plaintext is shown once at creation.
- `bleach` allowlist sanitization on every authored string, in both block and inline forms.
- Strict `default-src 'self'` Content-Security-Policy, made possible by removing all
  eight CDN `<script>` tags — the app now has no external runtime dependencies and runs
  air-gapped.
- CSRF protection on all cookie-authenticated state changes.
- `ALLOWED_HOSTS` validation, open-redirect checks on every `next` parameter, and
  `ProxyFix` limited to a declared number of proxy hops.
- Tenant isolation enforced through a single set of scoped getters; cross-tenant access
  returns 404 rather than 403, so ids cannot be probed.
- `ProductionConfig` refuses to boot without `SECRET_KEY` and `ENCRYPTION_KEY`, and
  refuses SQLite.

### Removed

- `index.html`, `generator.html`, `settings.html`, `preview_page.html`,
  `callback.html`, `installation-guide.md` — the static prototype.

[1.0.0]: https://github.com/ibuilder/procore-exhibit-generator/releases/tag/v1.0.0
