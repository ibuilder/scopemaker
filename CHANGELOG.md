# Changelog

All notable changes to this project are documented here.
This project adheres to [Semantic Versioning](https://semver.org/).

## [1.5.4] — 2026-08-09

### Fixed

- **A link whose attribute contained `>` could fabricate a coverage finding.**
  The fast text extraction added in 1.5.2 matched tags with `<[^>]*>`, which
  stops at the first `>`. bleach does not escape `>` inside attribute values, so
  `<a title="see > 220500 and Division 28">Excludes controls</a>` is a
  legitimate stored item — and everything after the first `>` leaked into the
  extracted text.

  The consequences were both wrong answers in the report it exists to produce:
  the leaked `220500` was recorded as a specification section the scope claims,
  **suppressing a genuine gap**, and the leaked `Division 28` became a hand-off
  finding nobody had written.

  The tag pattern is now attribute-aware. The differential test missed this
  because no clause in the shipped library has `>` inside an attribute; those
  cases are now in it explicitly, along with a check that the pattern does not
  backtrack.

  Affects 1.5.2 and 1.5.3 for anyone whose scope text contains a link with `>`
  in an attribute value. No migration — the next report is correct.

## [1.5.3] — 2026-08-09

### Changed

- **The coverage report is a further 2.3x faster** — 72 ms to 31 ms median on a
  25-scope project, 5 queries down to 4. Together with 1.5.2 that is 332 ms to
  31 ms, roughly 10x.

  `Scope.sections` and `ScopeSection.items` are both `lazy="selectin"`, so
  merely touching `project.scopes` eagerly built every section and item as an
  ORM instance — 2012 of them — whether or not anything read them. The report
  needs two columns per item. It now loads scopes with `noload(Scope.sections)`
  and takes those columns from a single row query.

  The first attempt at this added the row query but left `project.scopes` in
  place, so the instances were built anyway and the extra query made it 12%
  *slower*. It only looked like a win because the benchmark loaded the objects
  outside the timed region.

### Added

- **Tests for the operator commands** (`cli.py`, 40% → 94%): `create-user`,
  `grant-role`, `seed-library`, `run-worker`, `demo-data`, `render-queue`. The
  emphasis is on refusing loudly — a command that exits 0 having done nothing is
  worse than one that fails, and these get run during incidents.
- **Tests for the OIDC callback**: the CSRF state check, replay of a used state,
  a provider error not leaking its detail to the page, a missing email claim,
  and a deactivated account being refused even though the provider authenticated
  it. The Authlib client is stubbed — this covers our side of the handshake, not
  the provider, and does not replace testing against a real issuer.

Overall coverage 84% → 86%; 517 tests.

## [1.5.2] — 2026-08-09

### Changed

- **The coverage report is 2.15x faster** (332 ms -> 154 ms on a 25-scope
  project, measured interleaved so machine drift hit both sides). It was the
  slowest page in the application.

  Profiling found bleach's HTML5 parser accounting for a third of the runtime.
  `strip_html` runs a full parse because it has to be safe against anything a
  browser could be tricked into executing — the right tool for input arriving
  from a client, and the wrong one for scanning text this application had
  already sanitised on the way in, purely to find six-digit section numbers and
  the word "Division".

  `strip_stored_html` skips the parser for markup that has already been through
  `sanitize_inline`/`sanitize_html`. `strip_html` is unchanged and still handles
  everything untrusted.

  The shortcut is only safe if it cannot drift from what it replaces, so it is
  checked against `strip_html` across every clause and specification section
  the product ships with — 375 entries — plus twenty inputs picked to break a
  naive tag-stripper (comments containing `>`, tags splitting a word, entity
  edge cases). A separate test asserts the coverage report invokes the HTML
  parser zero times; a stopwatch assertion would be flaky on CI, a call count
  is not.

## [1.5.1] — 2026-08-08

### Security

- **An unverified email claim could take over an existing account.**
  `provision_sso_user` matched an incoming OIDC identity on the issuer's subject
  first and the email claim second. The email fallback would bind that identity
  to *any* pre-existing local account with the same address — including one with
  a password, an admin role and a project's worth of documents.

  That trusts the provider to have verified the address, and not all do: a
  multi-tenant identity provider with self-service signup will issue a token
  asserting somebody else's email. Whoever held such a token inherited the
  victim's account without ever knowing the password.

  Linking to an account that already exists now requires the provider to report
  `email_verified`, and **an absent claim counts as unverified** — an issuer
  that says nothing has confirmed nothing. Subject matching is unchanged: a
  subject is issued by the provider and cannot be chosen by the person signing
  in. Creating a *new* account from an unverified address is still permitted,
  because it lands in its own organization and can reach nothing that exists.

  `OIDC_REQUIRE_VERIFIED_EMAIL=0` restores the old behaviour for an identity
  provider you operate that omits the claim.

  **Who is affected:** deployments with `OIDC_ENABLED=1` whose identity provider
  can issue tokens for addresses it has not verified. Password-only and
  Procore-only deployments are unaffected.

### Added

- 20 tests for SSO account provisioning, which previously had none: subject
  matching, email changes at the provider, provider scoping, the domain
  allowlist, and organization attachment. Coverage of `accounts.py` went from
  47% to 86%.

### Changed

- The GitHub Pages site now describes what the product actually does. It had
  drifted four releases behind — its own argument is that scope gaps are
  expensive, and it never mentioned the coverage analysis that finds them.

## [1.5.0] — 2026-08-08

Everything here came from looking at the product rather than reading the code:
rendering an exhibit and inspecting it, measuring against a real database, and
driving the editor from a keyboard.

### Added

- **Export your data, and delete your account.** The export is personal data,
  not the employer's documents — it lists the scopes you authored without their
  contents, because those belong to the organization. Tests assert no password
  hash, token hash, raw token or MFA secret appears anywhere in it.

  Deletion preserves what has to survive it: the audit log keeps `actor_label`
  when the foreign key nulls out, so deleting an account cannot erase what it
  did, and shared organizations keep their scopes. The last administrator of an
  organization with other members is blocked rather than warned. An
  organization whose only member leaves is deleted with them — nobody could
  sign in to it again — and the confirmation page names it.
- **Keyboard reordering in the editor.** Dragging was the only way to reorder
  clauses, so the core editing action was impossible without a mouse
  (WCAG 2.1.1). Each item now has move up/down buttons driving the same code
  path, announcing the new position, with focus restored to the moved button.
- **A backup and restore drill in CI.** `docs/deployment.md` said to back up
  PostgreSQL and that this was all the state there was. Nobody had ever
  restored one. CI now populates a database, `pg_dump`s it, drops the schema,
  restores, and asserts both the row counts *and* that the restored database
  renders a reference exhibit byte for byte — row counts alone would pass with
  the encrypted columns coming back as mush.
- **A PostgreSQL load test in CI**, with real concurrency, published to the job
  summary. SQLite serialises writers, so the local numbers were measuring lock
  contention.
- **Page-fill assertions on the PDF.** The existing tests checked that text was
  present and that the document paginated; both passed while a page was 45%
  blank. These measure how far down each page the content reaches.

### Fixed

- **A clause with a long sub-list jumped the page whole.** Page 1 of the
  Division 21 exhibit was 45% blank: clause 3 has 22 specification sections
  under it, and the wrapper `<li>` holding that sub-list inherited
  `break-inside: avoid`, making the whole block unbreakable. Found by rendering
  a sample in CI and looking at it.
- **The scopes list issued a query per row.** 32 queries at 25 scopes, 16 at 12
  — each row lazily loaded its bid package. Projects repeat and came from the
  identity map, which is why a single-project dataset looked fine. Now a
  constant 7. Found by the PostgreSQL load test.
- The admin role dropdown had no accessible name, so every row announced
  identically; no table declared `scope` on its header cells.

## [1.4.1] — 2026-08-08

### Added

- **Rate limiting is now tested.** The whole suite ran with
  `RATELIMIT_ENABLED = False` — limits and fixtures that sign in dozens of
  times do not mix — which left the only defence against unlimited password
  guessing with no coverage at all. A dependency upgrade could have turned it
  into a no-op and nothing would have failed. Verified against Flask-Limiter
  3.12 and 4.1.1.

### Changed

- **API token verification is ~20x faster** (151 ms → 7.6 ms median on
  `/api/v1/me`). Argon2 is deliberately slow, which is right for a password
  typed once and wrong for a token presented on every call — it was most of the
  request. A short-lived per-process cache keeps the hash comparison off the hot
  path.

  Only the *verification* is cached, never the authorization decision: every
  request still loads the row and re-checks revocation and expiry, so revoking
  a token takes effect on the very next call. The raw token is never a cache
  key — it is hashed with BLAKE2b keyed on `SECRET_KEY` — and the cache is
  bounded and expiring.
- `last_used_at` is written at a five-minute resolution instead of on every
  request. It answers "roughly when was this token last seen", which does not
  justify a database write per API call.
- Dependencies: `actions/checkout` v4→v7, `actions/setup-python` v5→v7,
  `actions/configure-pages` v5→v6, `actions/deploy-pages` v4→v5,
  `docker/setup-buildx-action` v3→v4, and Flask-Limiter widened to allow 4.x.

## [1.4.0] — 2026-08-07

Track C: the work that decides whether this holds up under more than one user at
a time. Everything here was measured before and after rather than assumed.

### Added

- **Render cache.** Every export is keyed on a fingerprint of the document's
  actual content — scope fields, project, bid package, every section and item.
  An unchanged scope is served from stored bytes and renders nothing. The
  obvious implementation, hashing `updated_at`, is wrong here: the edit routes
  set `updated_by_id` to mark a scope dirty, and assigning the *same* user id is
  not a change, so no `UPDATE` fires and `onupdate` never triggers. One person
  editing twice in a row would have been served a document missing their edit.
- **Render queue.** With `RENDER_ASYNC=1` a needed render is handed to a worker
  (`flask run-worker`) and the request returns a waiting page that polls. The
  queue is a database table, not Redis — this deployment already runs
  PostgreSQL, and requiring a broker to download a PDF is a poor trade. Workers
  claim jobs with a conditional `UPDATE` rather than `FOR UPDATE SKIP LOCKED`,
  so the same code works on SQLite and PostgreSQL and several workers can share
  one queue. Jobs orphaned by a dead worker are requeued after ten minutes and
  abandoned after three attempts; results expire after seven days. Async is off
  by default, because turning it on without a worker means exports never finish.
- **Optimistic locking on scopes** (`row_version`). Two people editing the same
  scope now get an explicit conflict instead of a silent overwrite.
- **Prometheus metrics** at `/metrics`: request counts and latency histograms,
  render timings by format, export cache hit rate, and live queue depth. No
  `prometheus_client` dependency — the text format is simple enough to emit
  directly, and most self-hosters will never scrape it. The endpoint returns 404
  until `METRICS_TOKEN` is set and then requires the token. Requests are
  labelled by Flask endpoint, never by path: paths contain scope ids, and one
  time series per document is how a Prometheus instance falls over.
- **`scripts/load_test.py`** — builds a throwaway dataset and reports latency
  percentiles alongside the query count per page, because query count is the
  number that transfers between a laptop and production.

### Changed

- **The editor's N+1.** Rendering a scope walked `item.children` per item, one
  query each. The item tree is now built from the already-loaded flat collection
  with `set_committed_value`, which also stops SQLAlchemy from re-fetching it.

  Measured on 13 scopes / 843 items:

  | Page | Before | After |
  |---|---|---|
  | editor | 167 ms, 73 queries | 110 ms, 11 queries |
  | DOCX export (repeat) | 341 ms, 20 queries | 13 ms, 7 queries |

### Fixed

- Deleting a parent item promoted its children to the top level instead of
  deleting them, quietly corrupting the outline. The relationship now cascades
  with `passive_deletes`.
- `render_now` never stamped `started_at` on the synchronous path, so those jobs
  reported no duration.

## [1.3.0] — 2026-08-07

Track B: what it takes to survive a customer's security questionnaire. Also
renames the repository — the product is ScopeMaker, and the Procore connector is
one optional integration among others.

### Added

- **Append-only audit log.** Sign-ins and failures, lockouts, password resets,
  session revocation, role changes, member removal, invitations, API token
  issue and revocation, scope issue/revise/archive, MFA changes, and
  integration connect/disconnect/sync. Entries outlive the deletion of their
  actor: the foreign key is nulled but the actor's email is preserved, so
  removing a member does not erase what they did. Admin UI with an action
  filter, a security-events-only view, and CSV export.
- **Two-factor authentication.** TOTP with single-use recovery codes. The
  enrolment QR is an **inline SVG** — the shared secret is never handed to an
  image host, which also keeps the strict CSP intact. Secrets are encrypted at
  rest; recovery codes are stored as Argon2 hashes. A correct password alone
  does not authenticate: it parks a pending challenge that expires, is
  invalidated by a password change, and shares the account lockout so the
  second factor cannot be brute-forced separately. Turning MFA off requires the
  password, because that is exactly what a hijacked session would try.
- **Organization security policy** (Admin → Security): require two-factor for
  everyone, and require single sign-on. Enforced on every request rather than
  only at sign-in, so enabling a policy takes effect for sessions that are
  already open — which is the window an administrator turns it on to close.
  `sso_only` cannot be enabled when no identity provider is configured.
- **SECURITY.md** with a private disclosure route, and an explicit statement of
  what the application does *not* protect against.
- **Dependabot** for pip, GitHub Actions and Docker, and a CI job running
  `pip-audit --strict` plus a CycloneDX SBOM artifact.

### Changed

- **The repository is now `ibuilder/scopemaker`.** GitHub redirects the old
  URLs. Procore documentation moved out of the README headline into
  `docs/integrations.md` alongside OIDC; the integration itself is unchanged and
  still fully supported, off by default.
- **mypy now blocks CI.** It was advisory, which meant nobody read it. The
  seven type errors it was hiding are fixed — including two
  `ScopeSection | None` dereferences that would have been 500s.

### Fixed

- **API tokens bypassed the organization's MFA requirement.** The request hook
  that enforces policy keys off Flask-Login, and a bearer token is not a
  session — so a token issued before the policy was enabled kept working.
  Enforcement now also happens where the bearer identity resolves.
- Alembic renders JSONB columns as `JSONB(astext_type=Text())` without
  importing `Text`, producing a `NameError` the moment the migration runs. Fixed
  in the affected migrations and in `script.py.mako`, so it cannot recur.

## [1.2.0] — 2026-08-07

Track A of the path to production readiness: make the application safe to put
real users on. The headline is that **there was no password reset** — a user who
forgot their password was locked out permanently unless somebody with shell
access ran a CLI command.

### Added

- **Password reset.** Request and confirm flows with single-use, expiring
  tokens stored as Argon2 hashes. Requesting a new link invalidates the
  previous one, and completing a reset signs out every existing session — so a
  reset genuinely evicts an attacker rather than running alongside them.
- **Email delivery** on `smtplib`, with three backends: `console` (the
  development default, writing the message and its link to the log so a reset
  can be completed with no mail infrastructure at all), `smtp`, and `null` for
  tests. Delivery failure is logged, never raised into the user's request.
  Invitations are now emailed instead of only surfacing a link.
- **Account lockout.** Failed sign-ins are counted per account, with the lock
  window growing on repeated failures. Per-account rather than per-IP, because
  an IP limit does nothing against credential stuffing spread across addresses.
  A locked account fails before the password is checked, and the message is
  identical to every other failure so a lockout cannot be probed.
- **Session revocation.** The session cookie carries a per-user epoch; bumping
  it invalidates every live session. Exposed as "sign out everywhere else" on
  the profile page, and triggered automatically by password changes and resets.
- A loud startup warning when rate limiting uses in-memory storage in a
  multi-worker deployment, where configured limits are silently multiplied by
  the worker count. Redis is now wired into `docker-compose.yml`.

### Fixed

- **`login_user(current_user)` caused a `RecursionError`.** Flask-Login stores
  whatever it is handed on `g._login_user`, and `current_user` is a LocalProxy
  that reads that same slot — so passing the proxy made it resolve to itself.
  Both call sites now pass the concrete object.
- **Two forms on the profile page each had a field named `submit`,** so posting
  either one looked like a submission of both. They now have distinct names.

### Changed

- Production configuration refuses to boot without a mail relay, for the same
  reason it already refuses to boot without a secret key: a deployment that
  cannot send a password reset is not a working deployment.

## [1.1.0] — 2026-08-07

### Added

**Project scope coverage analysis.** A scope gap is work that appears in the drawings
but ends up in nobody's contract — classically at the seam between two trades, where
each assumed the other had it. Because ScopeMaker holds every exhibit as structured
rows rather than a PDF, answering "what has nobody's name against it?" is a query.

The new page at `/projects/{id}/coverage` lines up the specification sections claimed
across every scope on a project and reports four things:

- **Gaps** — a section that applies to a division on the project but is claimed by no
  scope.
- **Overlaps** — a trade-specific section claimed by two or more trades, which usually
  means the same work is being bought twice.
- **Shared seams** — sections the library cross-references to several divisions, and
  which several trades correctly carry. Every trade firestops its own penetrations, so
  four claims on `078413` is right, not a double-buy. Reported separately because the
  seam still needs a decision: who paints the exposed sprinkler pipe, and who furnishes
  the access door for whose valve.
- **Unassigned hand-offs** — an exclusion that pushes work onto another division
  ("…which is by the Division 28 Subcontractor") when no scope for that division exists
  on the project. Fire protection excluding the fire alarm is only safe if Division 28
  is actually coming.

Also lists bid packages with no scope written yet.

Available as a CSV download for buyout meetings, and at
`GET /api/v1/projects/{id}/coverage`.

A section claimed by a hand-edited line still counts: the analysis prefers the
structured id recorded at generation and falls back to a six-digit number in the text,
so rewording a spec line cannot manufacture a phantom gap.

### Fixed

- **`.env` was never actually being read.** Config classes read `os.environ` at
  class-definition time — that is, at import — so `load_dotenv()` in the application
  factory ran far too late to have any effect. The `flask` CLI happens to load dotenv
  itself, which masked the problem; gunicorn, a cron job or any maintenance script
  silently fell back to the default SQLite path and presented as a mysteriously empty
  database. Loading now happens at the top of `scopemaker/config.py`, from an explicit
  project-root path, before the classes are defined.
- **The licence file did not match the declared licence.** `LICENSE` was GPL-3.0 while
  the README, `pyproject.toml` and release notes all said MIT. The file is now the MIT
  text.

### Added (tooling)

- `scripts/build_samples.py` and a manually dispatched **Build sample exhibits**
  workflow that renders a full set of exhibits as PDF, DOCX, Markdown and JSON and
  uploads them as an artifact — a way to review real output without installing the
  WeasyPrint native stack locally.

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

[1.0.0]: https://github.com/ibuilder/scopemaker/releases/tag/v1.0.0
