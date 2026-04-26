---
name: prep-data-clean
description: Create and execute a data cleaning plan. Use after prep-data-discover has assessed the data. Takes the assessment results, proposes a cleaning plan with exact operations, waits for user confirmation, then executes. Trigger keywords - clean this data, execute cleaning plan, apply the plan, run the cleaning.
---

# Prep Data: Clean & Execute

Build a cleaning plan from the assessment, get user confirmation, execute, and validate. This is Phase 3 of the data preparation pipeline.

## When to Trigger

- After `prep-data-discover` has presented an assessment
- User says "clean this data", "execute the plan", "apply cleaning"

## Step 1 — Read assessment

Read the profile JSON files from the output directory (created by prep-data-discover). Use these to build the cleaning plan.

## Step 2 — Build cleaning plan

For each relevant dataset, propose operations using ONLY these 12 ops:

**Column operations:**
| op | params | effect |
|---|---|---|
| `drop_column` | `column` | Drop a column |
| `rename_column` | `old_name`, `new_name` | Rename a column |

**Value modification:**
| op | params | effect |
|---|---|---|
| `replace_values` | `column`, `mapping` (dict) | Map values. Unmapped pass through. |
| `cast_type` | `column`, `target_type` | Cast type. Invalid→NULL. Types: BOOLEAN, INTEGER, BIGINT, FLOAT, DOUBLE, DECIMAL(p,s), VARCHAR, DATE, TIMESTAMP. |
| `strip_whitespace` | `column` | Remove leading/trailing whitespace |
| `transform_case` | `column`, `mode` (lower/upper/title) | Change text case |

**Row filtering:**
| op | params | effect |
|---|---|---|
| `drop_duplicates` | `subset` (optional list) | Remove duplicate rows |
| `drop_nulls` | `column` | Delete rows where column is NULL |
| `filter_rows` | `column`, `condition`, `value`/`values` | KEEP matching rows. Conditions: gt, lt, gte, lte, eq, neq, between, in, not_in, is_null, not_null |

**Geographic enrichment:**
| op | params | effect |
|---|---|---|
| `add_h3_index` | `lat_col`, `lon_col`, `resolution` (default 9) | Add H3 cell index column |
| `transform_coords` | `x_col`, `y_col`, `source_crs` | Reproject coordinates (e.g. EPSG:2263→EPSG:4326) |
| `polygon_centroid` | `geom_col` | Extract centroid from WKT geometry |

**Plan format:** JSON array of operations per dataset.
```json
[
  {"op": "strip_whitespace", "column": "Borough"},
  {"op": "replace_values", "column": "BORO", "mapping": {"MN": "manhattan", "BK": "brooklyn"}},
  {"op": "cast_type", "column": "Latitude", "target_type": "DOUBLE"},
  {"op": "add_h3_index", "lat_col": "Latitude", "lon_col": "Longitude", "resolution": 9}
]
```

**Every operation must be value-level specific.** Not "standardize BORO" but the exact mapping.

## >>> INTERVENTION POINT <<<

Present the cleaning plan to the user. Wait for confirmation before executing.

## Step 3 — Execute

Scripts are in `skills/prep-data-clean/scripts/` or `skills/nemoclaw-user-prep-data/scripts/`.

For each dataset:
1. Run cleaning:
   ```bash
   python scripts/clean.py <input.csv> <output.csv> --ops '<json_array>' [--demo]
   ```
2. Run validation:
   ```bash
   python scripts/validate.py <original.csv> <cleaned.csv> [--output <report.json>] [--demo]
   ```
3. Append result to `cleaning_log.json` and flush before moving to the next dataset.

**Resume support:** If `cleaning_log.json` exists, skip datasets with `status: "ok"`.

**Output structure:**
```
{output_dir}/
├── {name}.cleaned.csv
├── {name}.profile.json
├── {name}.validation.json
└── cleaning_log.json
```

After all datasets: "Cleaning complete. Run `prep-data-report` to generate the data dictionary."

## Constraints

- Never modify original files.
- Operations must use ONLY the 12 ops listed above.
- Validation is MANDATORY after each clean.
- Order matters: ops run sequentially, each sees post-state of previous.
- Column names validated against live df.columns.
- Never include delete/drop-rows ops without listing in the plan.
