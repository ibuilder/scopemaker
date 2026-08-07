# Integrations

ScopeMaker works entirely on its own. Everything here is optional, off by default,
and adds nothing to the app's behaviour until you configure it.

The design rule for all of them: **credentials never reach the browser.** Every
token is exchanged server-side and stored encrypted with the application's
`ENCRYPTION_KEY`. Rotating that key does not lose data — it means each connection
has to be re-authorized.

---

## Procore

Imports projects and bid packages, and pushes finished exhibits onto commitments.

### Enabling it

```bash
PROCORE_ENABLED=1
PROCORE_CLIENT_ID=...
PROCORE_CLIENT_SECRET=...
PROCORE_REDIRECT_URI=https://your-host/procore/callback
```

Register that exact redirect URI in the
[Procore Developer Portal](https://developers.procore.com/) first — it has to match
character for character. With `PROCORE_ENABLED=0` the routes return 404 and the
navigation item disappears entirely.

### Two ways to connect

**Authorization code** — an administrator clicks *Connect to Procore* and authorizes
with their own login. ScopeMaker sees the projects that person can see. Tokens
refresh automatically, and a 401 mid-session triggers one silent retry rather than
making the user reconnect.

**Developer Managed Service Account** — for unattended sync. Procore retired
traditional service accounts on **2025-03-18**, so DMSA with the client-credentials
grant is the supported path. Supply the Procore company id when connecting. Service
accounts have no refresh token; a new access token is minted from the client secret
each time one is needed.

### What it syncs

| Direction | What |
|---|---|
| In | Projects — name, number, address, owner, architect |
| In | Bid packages, with the CSI division inferred from the package number (`BP-21A` → 21) |
| Out | A generated exhibit attached to a commitment, as PDF or DOCX |

Sync is upsert-by-Procore-id and never destructive. A record that disappears from
Procore is left alone locally, because a scope may already reference it, and a
populated local field is never overwritten with an empty remote one — somebody may
have filled in what Procore does not hold.

Division inference deliberately gives up rather than guessing: a two-digit run that
lands on a number CSI reserves (20, 24, 29…) is treated as a coincidence and the
field is left blank.

### Rate limits and failures

Procore's rate limit surfaces as a clear "try again shortly" rather than a stack
trace. Any integration failure is recorded against the connection and shown on the
Procore settings page, so a sync that has been quietly failing for a week is visible.

---

## OpenID Connect single sign-on

```bash
OIDC_ENABLED=1
OIDC_CLIENT_ID=...
OIDC_CLIENT_SECRET=...
OIDC_DISCOVERY_URL=https://idp.example.com/.well-known/openid-configuration
OIDC_ALLOWED_DOMAINS=yourcompany.com
OIDC_DEFAULT_ORG=your-org-slug
```

Users are matched on the issuer's stable subject first and email second, so somebody
whose address changes at the identity provider keeps their account and their scopes
rather than silently getting a second one.

`OIDC_ALLOWED_DOMAINS` restricts which email domains may sign in.
`OIDC_DEFAULT_ORG` is the organization new SSO users land in; if the slug does not
match anything, a personal organization is created from their email domain and a
warning is logged rather than the sign-in failing.

SSO accounts have no local password. Password reset deliberately does nothing for
them — the reset page reports the same neutral message as always, but no email is
sent, because the identity provider owns that credential.

---

## Email

Not optional in production: without it a user who forgets their password cannot get
back in. See [deployment](deployment.md#before-you-expose-it).

---

## Writing your own

The JSON API covers everything the web UI does — generating scopes, reading the full
numbered document, coverage analysis and exports. See [api.md](api.md). For most
integrations that is a better starting point than adding code here.
