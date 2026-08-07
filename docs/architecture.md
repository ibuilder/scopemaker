# Architecture

## Layout

```
scopemaker/
├── __init__.py            application factory, request hooks, security headers
├── config.py              environment-driven config; production validates itself
├── extensions.py          SQLAlchemy, Login, CSRF, Limiter, OAuth singletons
├── errors.py              typed errors + HTML/JSON handlers
├── security.py            Argon2 hashing, Fernet encryption, role decorators
├── cli.py                 flask seed-library, create-user, check-pdf, demo-data
├── data/
│   ├── masterformat.py    canonical CSI divisions
│   └── seed/*.yaml        the shipped clause and specification library
├── models/                Organization, User, Project, Scope, Clause, Procore…
├── services/
│   ├── scope_builder.py   assembles a document from a division + selections
│   ├── numbering.py       outline label computation
│   ├── library.py         clause and spec-section queries
│   ├── sanitize.py        bleach allowlists
│   ├── seeding.py         idempotent library loading
│   ├── procore_client.py  Procore REST client
│   └── renderers/         html · pdf · docx · json · markdown
├── blueprints/            main auth projects scopes library exports admin procore api
├── templates/
└── static/                no CDN: all CSS/JS served from this origin
```

Views stay thin: validate, call a service, render. Anything shared between the web UI
and the JSON API lives in `services/`.

## The document model

A `Scope` owns ordered `ScopeSection`s. Sections hold prose (`body_html`) and/or a tree
of `ScopeItem`s that nest through `parent_id` — that tree is what produces the
`1.` / `1.1` / `1.1.1` outline a subcontract exhibit is written in.

```
Scope
 └── ScopeSection (intent, summary, inclusions, exclusions, …, recap)
      └── ScopeItem
           └── ScopeItem            (nested clause)
                └── ScopeItem       (arbitrary depth)
```

Two details worth knowing:

**Every item carries `section_id`, including nested ones.** `ScopeSection.items` is
therefore the flat set and owns the `delete-orphan` cascade. `ScopeItem.children` uses
a plain `delete` cascade with `passive_deletes=True`. Declaring `delete-orphan` on both
would make an item an orphan the moment it left *either* collection; omitting `delete`
entirely would null out `parent_id` on deletion and silently promote a removed clause's
sub-clauses to top-level items, where they would reappear in the exhibit under new
numbers.

**Specification sections nest under the summary** rather than forming their own
section, marked with `meta.role == "spec_list"`. That is how the numbering reads on a
real exhibit: `2.3` introduces the list and `2.3.1` onward are the sections.

## Numbering

`services/numbering.py` computes labels and every renderer writes them as literal
text. Nothing relies on CSS counters or Word list numbering, because those cannot be
shared across PDF, DOCX, Markdown and JSON — and the whole point is that clause `3.2.4`
identifies the same sentence in all of them.

`NumberedNode.path` is the dotted counter path (`3.2.4`), independent of the rendering
style, which is what revision diffing keys on.

## Rendering

```
Scope ──▶ build_document() ──▶ Document (numbered, sanitized)
                                  │
              ┌───────────┬───────┼────────┬──────────┐
            HTML        PDF     DOCX     JSON     Markdown
                     WeasyPrint  python-docx
```

`build_document()` is the single source of truth. The PDF is produced from the same
HTML the preview shows, with `static/css/document.css` **inlined** into the page —
WeasyPrint resolves relative URLs against the package directory while a browser
resolves them against the request path, and inlining sidesteps that disagreement
entirely.

Paged media lives in `@page`: margins, running header and footer via `string-set`, and
`counter(page) of counter(pages)`.

## Multi-tenancy

Every content row carries `organization_id`. `blueprints/helpers.py` holds the
tenant-scoped getters; views never call `db.session.get(Model, id)` directly. A record
belonging to another organization returns **404, not 403** — a 403 would confirm the id
exists. Tests cover this for scopes, projects and the API.

The clause library is the one deliberate exception: rows with `organization_id IS NULL`
are the shipped system library, readable by everyone. An organization cannot edit them
(they are shared rows) — it copies one, or records a `ClauseSuppression` to hide it for
itself alone.

## Security posture

| Concern | Approach |
|---|---|
| Passwords | Argon2id, 64 MiB / t=3 / p=2, transparent rehash on login |
| Third-party tokens | Fernet-encrypted columns; a rotated key degrades to "reconnect", not a crash |
| API tokens | Only an Argon2 hash is stored; lookup by non-secret prefix |
| XSS | `bleach` allowlists on every authored string, block and inline variants |
| CSP | `default-src 'self'`; no CDN, so the policy can stay tight |
| CSRF | Flask-WTF everywhere except the bearer-authenticated API |
| Host header | `ALLOWED_HOSTS` check, because absolute URLs are built from it |
| Open redirect | `is_safe_redirect()` on every `next` parameter |
| Proxy spoofing | `ProxyFix` trusts exactly `TRUSTED_PROXY_COUNT` hops |
| Account enumeration | Identical login failure message and work either way |
| Config | Production refuses to boot without secrets, and refuses SQLite |

## Testing

`tests/conftest.py` seeds the shipped library once per session and clears tenant data
after each test. It deliberately does **not** wrap tests in a rolled-back transaction:
Flask-SQLAlchemy overrides `Session.get_bind`, so a session rebound to a test-owned
connection is silently ignored and the writes land for real.

It also avoids holding an app context open for the session. Flask reuses an
already-pushed app context for test-client requests, and `g` lives on that context — a
session-scoped context would let Flask-Login's cached user leak between tests.

PDF tests are marked `@pytest.mark.pdf` and skip when the native stack is missing. CI
installs it and asserts they ran, so a PDF regression cannot pass unnoticed.
