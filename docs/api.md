# JSON API v1

Base path: `/api/v1`

## Authentication

Create a token under **Admin → API tokens**. The plaintext is shown once; only an
Argon2 hash is stored.

```
Authorization: Bearer smk_...
```

Tokens are scoped to a single organization and carry either `read` or `read write`.
Browser sessions authenticate against the same endpoints, so the UI and integrations
share one implementation.

## Errors

```json
{ "error": { "code": "validation_error", "message": "The request body is invalid.",
             "details": { "fields": [ { "field": "division_code", "message": "..." } ] } } }
```

| Status | Code | Meaning |
|---|---|---|
| 401 | `unauthorized` | Missing, invalid, expired or revoked token |
| 403 | `insufficient_scope` | Read-only token attempted a write |
| 404 | `not_found` | No such record **in your organization** |
| 422 | `validation_error` | Body failed validation; `details.fields` says where |
| 422 | `scope_locked` | The scope is issued; revise it first |
| 429 | — | Rate limited |
| 500 | `internal_error` | Includes an `incident` id that matches the server log |
| 500 | `pdf_unavailable` | WeasyPrint's native libraries are not installed |

## Reference

### `GET /api/v1/` — public

Endpoint index. No authentication.

### `GET /api/v1/divisions` — public

Selectable CSI divisions (reserved numbers excluded), section keys, clause categories
and scope statuses.

### `GET /api/v1/me`

The authenticated user, organization and token scopes.

### `GET /api/v1/library/clauses`

| Query | Meaning |
|---|---|
| `division` | Two-digit division; returns that division plus universal clauses |
| `category` | `inclusion`, `exclusion`, `clarification`, … |

### `GET /api/v1/library/spec-sections`

`?division=21` returns Division 21's own sections, the universal Division 01 sections,
and every section cross-referenced to 21.

### `GET /api/v1/scopes`

`status`, `division`, `project_id`, `limit` (≤200), `offset`.

### `POST /api/v1/scopes`

```json
{
  "division_code": "21",
  "trade_name": "Fire Protection",
  "title": "Scope of Work",
  "exhibit_label": "EXHIBIT B",
  "project_id": "…",
  "bid_package_id": "…",
  "use_defaults": true,
  "clause_ids": [],
  "spec_section_ids": [],
  "enabled_sections": ["intent", "summary", "inclusions", "exclusions", "recap"],
  "numbering_scheme": "legal",
  "base_bid_amount": "1425000.00",
  "currency": "USD"
}
```

`use_defaults` takes the library's pre-selected clauses and specification sections for
the division — the one-call "just generate me a scope" path. Unknown fields are
rejected rather than ignored. Returns `201`.

### `GET /api/v1/scopes/{id}`

The full resolved document: every section, every item, computed outline `number` and
style-independent `path`, plus the recap rows.

### `PATCH /api/v1/scopes/{id}`

`title`, `exhibit_label`, `trade_name`, `status`, and the three recap amounts. Returns
`422 scope_locked` once the scope has been issued.

### `POST /api/v1/scopes/{id}/issue` · `/revise`

`issue` freezes the current version as an immutable revision and locks the scope;
`revise` opens the next version for editing.

### `GET /api/v1/scopes/{id}/export/{format}`

`pdf` · `docx` · `html` · `md` · `json`. All formats carry identical clause numbering.

### Projects

| Method | Path |
|---|---|
| `GET` `POST` | `/api/v1/projects` |
| `GET` `POST` | `/api/v1/projects/{id}/bid-packages` |

## Worked example

```bash
TOKEN=smk_...
HOST=https://scopes.example.com

PROJECT=$(curl -s -X POST $HOST/api/v1/projects \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"Riverside Medical Center","number":"2024-118"}' | jq -r .project.id)

curl -s -X POST $HOST/api/v1/projects/$PROJECT/bid-packages \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"number":"BP-21A","name":"Fire Protection","division_code":"21"}'

SCOPE=$(curl -s -X POST $HOST/api/v1/scopes \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"division_code\":\"21\",\"project_id\":\"$PROJECT\",\"use_defaults\":true}" \
  | jq -r .scope.id)

curl -s -H "Authorization: Bearer $TOKEN" -o exhibit.pdf \
  $HOST/api/v1/scopes/$SCOPE/export/pdf
```

## Rate limits

600 requests/hour by default, with tighter limits on scope creation (60/hour) and
exports (120/hour). Configure with `RATELIMIT_DEFAULT`; use Redis via
`RATELIMIT_STORAGE_URI` for multi-worker deployments, since the in-memory default is
per-process.
