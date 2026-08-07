# The clause library

The library is what makes generation useful rather than a blank form. It ships as YAML
under `scopemaker/data/seed/` and is loaded into the database by `flask seed-library`.

## What ships

| File | Contents |
|---|---|
| `boilerplate.yaml` | Prose for the non-list sections (intent, section lead-ins, recap) |
| `clauses_universal.yaml` | Obligations that apply to every trade |
| `clauses_facility.yaml` | Divisions 02–14 |
| `clauses_services.yaml` | Divisions 21–28 |
| `clauses_site.yaml` | Divisions 31–35 |
| `spec_sections.yaml` | Specification sections and their cross-references |

236 clauses and 139 specification sections in total.

## Clause format

```yaml
- key: d21.incl.fire_pump      # stable id; re-seeding updates rather than duplicates
  division: "21"               # null = universal, applies to every trade
  category: inclusion
  default: true                # pre-selected when a scope is generated
  position: 40                 # sort order within the category
  text: >-
    Fire pump, jockey pump, fire pump controller, suction and discharge piping,
    test header, flow meter, and all pump accessories, including factory
    certified acceptance testing.
```

Categories, and the section each feeds:

| Category | Section |
|---|---|
| `inclusion` | Trade Specific Scope of Work Items |
| `exclusion` | Trade Specific Scope Exclusions |
| `clarification` | Clarifications and Assumptions |
| `general_requirement` | Scope of Work Summary |
| `allowance` `alternate` `unit_price` | Their own optional sections |
| `safety` `closeout` `schedule` | Their own optional sections |

## Specification sections

```yaml
- key: spec.078413
  division: "07"                                  # where the section actually lives
  code: "078413"
  title: "Penetration Firestopping"
  default: true
  related: ["21", "22", "23", "25", "26", "27", "28", "09"]
```

`related` is the important field. A fire protection package is contractually
responsible for firestopping its own penetrations, but that section lives in
Division 07 — so a Division 21 scope that only lists Division 21 sections has a gap.
Listing `21` under `related` puts it in front of the estimator automatically.

`universal: true` marks the Division 01 procedural sections (submittals, quality,
closeout, commissioning) that belong on every trade's exhibit.

## Placeholders

Boilerplate supports `{placeholder}` tokens merged from the scope, project and bid
package:

`{trade}` `{trade_upper}` `{division_code}` `{division_title}` `{project_name}`
`{project_number}` `{project_location}` `{owner_name}` `{architect_name}`
`{engineer_name}` `{contractor_name}` `{delivery_method}` `{bid_package_number}`
`{bid_package_name}` `{subcontractor_name}` `{currency}`

A token with no available value renders as `__________` — a visible blank the author is
expected to fill in, never a sentence that silently reads as complete. An unknown token
is left as-is so the typo is obvious.

## Customising it for your company

You cannot edit a shipped clause: it is one shared row used by every organization.
Instead, from **Library → Clauses**:

- **Copy** a shipped clause to fork it into your own library. The original is
  automatically hidden so the picker does not show near-identical twins.
- **Hide** a clause you disagree with. This records a `ClauseSuppression` scoped to
  your organization; nobody else is affected.
- **New clause** to add your own language, universal or division-specific.

Mark a clause `default` and it is pre-selected on every new scope for that division —
which is how you encode "we always say this".

To capture a whole exhibit, build one the way you want it and use **Save as template**
from the editor. Templates carry the sections, their boilerplate and every item.

## Re-seeding

`flask seed-library` is idempotent and keyed on `system_key`. Running it:

- inserts clauses added in a new release;
- updates the text of existing shipped clauses (so a correction actually lands);
- never touches your own clauses, suppressions or templates.

`flask seed-library --no-update` inserts only, leaving existing text alone.

## A note on MasterFormat

Division titles follow MasterFormat 2020. The section *numbers* here are limited to
those in common public use as cross-references in bid documents. The complete
MasterFormat section list is published and copyrighted by the
[Construction Specifications Institute](https://www.csiresources.org/) — load your own
project specification index for authoritative numbering.
