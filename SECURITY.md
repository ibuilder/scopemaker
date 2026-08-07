# Security policy

## Reporting a vulnerability

Please report security issues privately through
[GitHub Security Advisories](https://github.com/ibuilder/scopemaker/security/advisories/new).
Do not open a public issue for anything exploitable.

Include what you can: affected version, reproduction steps, and what an attacker
gets out of it. A proof of concept helps but is not required to report something.

We aim to acknowledge within 3 working days and to ship a fix or a mitigation
plan within 30 days for anything rated high or critical. You will be credited in
the release notes unless you would rather not be.

## Supported versions

| Version | Supported |
|---|---|
| 1.3.x | ✅ |
| 1.2.x | Security fixes only |
| < 1.2 | ❌ — these predate password reset and account lockout |

## What this application does about security

Because self-hosters carry the operational risk, it is worth being explicit
about what the code does and does not protect you from.

### Credentials

- Passwords are hashed with **Argon2id** (64 MiB, t=3, p=2), rehashed
  transparently on sign-in when the parameters change.
- **Two-factor authentication** is TOTP, with single-use recovery codes stored
  as Argon2 hashes. The enrolment QR is an inline SVG — the shared secret is
  never sent to an image host.
- **API tokens** are stored as hashes only; lookup is by a non-secret prefix.
- **Third-party OAuth tokens** and TOTP secrets are Fernet-encrypted at rest
  with `ENCRYPTION_KEY`. Rotating that key does not destroy data — affected
  connections simply need re-authorizing.
- A password reset or change **revokes every existing session**, so a reset
  evicts an attacker rather than running alongside them.

### Resisting attack

- Sign-in failures are counted **per account**, not per IP, because IP limits do
  nothing against credential stuffing spread across addresses. Second-factor
  failures count against the same lockout.
- The sign-in and password-reset endpoints return **identical responses**
  whether or not an account exists, and whether or not it is locked.
- `ALLOWED_HOSTS` is validated on every request, because absolute URLs — reset
  links, OAuth redirects — are built from the `Host` header.
- Every `next=` parameter is checked against an open-redirect allowlist.
- `ProxyFix` trusts exactly `TRUSTED_PROXY_COUNT` hops. Trusting an unbounded
  number would let a client spoof its own source IP and defeat rate limiting.
- Content Security Policy is `default-src 'self'`. There are **no CDN
  dependencies** — every asset is served by the application.
- All authored HTML passes through a `bleach` allowlist before it is stored.

### Tenancy

Every content row carries an `organization_id`, and all reads go through a small
set of scoped getters. Cross-tenant access returns **404, not 403** — a 403
would confirm the record exists. This is covered by tests for the web UI, the
API and the coverage report.

### Auditing

Privileged actions are recorded to an append-only log: sign-ins and failures,
lockouts, password resets, session revocation, role changes, member removal,
token issue and revocation, scope issue/revise/archive, MFA changes, and
integration connect/disconnect. Entries outlive the deletion of their actor.

### Configuration that fails closed

`ProductionConfig` refuses to start without `SECRET_KEY`, `ENCRYPTION_KEY`, a
non-SQLite database, and a working mail relay. A deployment that cannot send a
password reset is not a working deployment, so it will not boot pretending
otherwise.

## What this does not protect you from

- **Anyone with database access** can read your scopes and clause library. Only
  credentials and third-party tokens are encrypted at rest; document content is
  not. Use encryption at the storage layer if that matters.
- **A malicious organization administrator.** Admins can read everything in
  their own organization, issue tokens and change policy. The audit log records
  it, but nothing prevents it.
- **Denial of service.** Rate limits are per-account and per-endpoint, not a
  DDoS defence. Put it behind something that is.
- **Supply chain.** Dependencies are pinned by range and scanned by Dependabot
  and `pip-audit` in CI, but neither is a guarantee.

## Verifying a release

CI runs `pip-audit`, generates a CycloneDX SBOM as a build artifact, and
enforces `ruff` and `mypy`. The PDF rendering tests run for real against the
native stack rather than being skipped.

## Hardening checklist

Before exposing this to anyone:

- [ ] `SECRET_KEY` and `ENCRYPTION_KEY` generated fresh, stored in a secret
      manager, never committed
- [ ] `ALLOWED_HOSTS` set to the hostnames you actually serve
- [ ] `FORCE_HTTPS=1` and TLS terminated in front
- [ ] `TRUSTED_PROXY_COUNT` set to the real number of proxies
- [ ] `REGISTRATION_MODE=invite` or `closed` for an internal deployment
- [ ] `RATELIMIT_STORAGE_URI` pointed at Redis if you run more than one worker
- [ ] `MAIL_SERVER` configured and a test reset actually delivered
- [ ] Two-factor required for the organization (**Admin → Security**)
- [ ] Database backups running, and a restore tested
