# Roadmap

What is done, what is deliberately not being done, and what is worth doing next.

`CHANGELOG.md` records what shipped. This records intent — including the
decisions that look like oversights and are not, so they do not get relitigated
every few months.

**Current release: v1.5.6.** 553 Python tests, 13 JavaScript tests, 87% coverage.

---

## Where it stands

The product does what it was built to do: generate a construction subcontract
Scope of Work exhibit from a CSI MasterFormat clause library, let somebody edit
it, and export it as PDF, DOCX, Markdown or JSON with identical clause numbering
in every format.

Everything below is verified by something that runs, not by assertion:

| Area | How it is verified |
|---|---|
| Scope generation, editing, numbering | 553 tests, 87% coverage |
| PDF output | Rendered in CI and inspected; page-fill assertions catch blank pages |
| Editor interaction | 13 JavaScript tests in jsdom against the shipped `app.js` |
| Multi-tenancy | Cross-tenant access returns 404 (not 403) — tested per route |
| Migrations | Applied and rolled back against real PostgreSQL on every push |
| Backups | CI dumps, drops the schema, restores, and re-renders an exhibit byte-for-byte |
| Performance | Load test against PostgreSQL, 8 concurrent clients, every push |
| Dependencies | pip-audit, CycloneDX SBOM, Dependabot across pip/npm/actions/docker |

---

## Blocked — needs credentials

**This is the largest remaining risk, and it is not a coding task.**

Three integrations are verified only against mocks. Mocks are precisely what
fails on first contact with a real provider.

| Integration | State | What unblocks it |
|---|---|---|
| **Procore** | Client and sync tested against `responses` mocks only | A Procore sandbox account |
| **OIDC / SSO** | Our half of the handshake is tested with a stubbed Authlib client; account provisioning and linking are fully tested | A real IdP (Okta, Entra, Auth0) |
| **Email** | SMTP paths tested with a fake `smtplib`; a console backend is used in development | A real relay |

Each is a short job once the credentials exist. None should be attempted by
holding someone else's secrets.

---

## Worth doing next

Ordered by value, not effort. Nothing here is committed to a date.

### 1. End-to-end browser tests
The JavaScript tests cover reordering logic in jsdom. Nothing drives the real
application through a browser in CI — the accessibility audit that found the
keyboard-reordering bug was done by hand.

One hazard to plan around: `data-confirm` produces a native `confirm()`, which
blocks the renderer and freezes CDP automation. Any suite touching revoke,
delete-item or sign-out-everywhere must override `window.confirm` first.

### 2. Reduce the remaining coverage gaps
`logging_config.py` sits at 68%. The uncovered branches are the JSON formatter
and request-id plumbing — low risk, but they are what you read during an
incident.

### 3. Template and clause library depth
The library ships 236 clauses and 139 specification sections across 20+ trades.
Divisions with thin coverage are the obvious product investment, and it is
domain work rather than engineering.

### 4. Operational polish
- A structured `/readyz` payload that reports render-queue depth, not just
  database reachability.
- Alert examples in the docs for the two conditions that matter:
  `scopemaker_render_queue{status="failed"}` climbing, and
  `status="queued"` staying above zero (the worker is gone or wedged).

---

## Deliberate non-goals

Each of these looks like a gap and is a decision. Reopen them only with a reason
that is not "this looks wrong".

**A Rust rewrite.** Reimplementing generation, coverage, numbering and
paged-media PDF in Rust would discard ~4,000 lines of tested domain logic and
every test with it. Months of work for no user-visible gain.

**A packaged desktop build is a different question, and is now costed** — see
[docs/desktop-app-scope.md](docs/desktop-app-scope.md). The blocker previously
recorded here ("WeasyPrint's GTK/cairo stack is difficult to bundle") was based
on stale information: WeasyPrint 69 renders PDFs itself and loads five
Pango-chain libraries, not a GTK runtime. The realistic estimate is 2–4 weeks
for a Tauri shell around the existing app, gated on a 2–3 day spike proving
those libraries load on a clean Windows machine. Not started, not committed to.

**Grouping SSO users by email domain.** With no `OIDC_DEFAULT_ORG` configured,
each new SSO user gets their own organization named after their email domain.
The naming makes it look like grouping was intended and broken. It was not:
grouping people who merely share a domain would put strangers in one tenant the
first time somebody signed in with a consumer address.

**Caching the API authorization decision.** The verified-token cache skips the
Argon2 comparison and *only* that. Every request still loads the row and
re-checks revocation and expiry, which is why revoking a token takes effect on
the very next call. Caching the decision would make that untrue.

**Using `strip_stored_html` on untrusted input.** It is a regex tag-stripper for
markup this application already sanitised. `strip_html` runs bleach and is the
correct tool for anything arriving from a client. The two are diff-tested against
each other across the whole shipped clause library; that guarantee only holds for
sanitised input.

**Redis or a message broker for rendering.** The render queue is a database
table. This deployment already runs PostgreSQL, and requiring a broker in order
to download a PDF is a poor trade. Workers claim jobs with a conditional
`UPDATE`, so the same code runs on SQLite and PostgreSQL.

**Procore as a headline feature.** The connector is retained and functional but
deliberately de-emphasised. The value is the clause library and the
cross-division specification references; the connector is one optional
integration among others.

---

## Performance baselines

Regressions here are bugs. All measured on a 25-scope project.

| Path | Baseline | Notes |
|---|---|---|
| Coverage report | 31 ms, 4 queries | Was 332 ms before v1.5.2–1.5.3 |
| Scopes list | 7 queries | Constant — must not scale with list length |
| Export, cache hit | ~13 ms | Fingerprinted on document content, not `updated_at` |
| API token auth | ~7.6 ms | Was 151 ms before the verified-token cache |

Two guards exist because a stopwatch assertion is flaky on shared CI: the
coverage report asserts the HTML parser is invoked **zero** times, and asserts
**zero** `ScopeItem`/`ScopeSection` ORM instances are built.

---

## How this file is maintained

Update it when a decision is made, not when work finishes — `CHANGELOG.md`
already covers the latter. If something in "Deliberate non-goals" is reversed,
move it out and say why in the changelog entry.
