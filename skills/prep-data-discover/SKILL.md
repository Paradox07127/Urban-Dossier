---
name: prep-data-discover
description: Discover and assess CSV data quality. Use when the user mentions preparing data, cleaning data, exploring data quality, data preparation, look at my data, what's wrong with my data. Scans a directory of CSVs, profiles each file, detects 20+ quality issues, and presents an assessment report. After this step, tell the user to run prep-data-clean.
---

# Prep Data: Discover & Assess

Scan a directory of CSVs, profile each file, detect quality issues, and present an assessment. This is Phase 1+2 of the data preparation pipeline.

## When to Trigger

- User mentions "prepare data", "clean data", "look at my data", "what's wrong with my data"
- User describes a project goal and references local data files

## Inputs

Confirm three things with the user:
1. **Input directory** — where the raw CSV files live
2. **Output directory** — where cleaned results will go (default: `{input_dir}/cleaned/`)
3. **Project goal** — what they want to do with the data

## Phase 1: Discovery

1. List all CSV files in the input directory
2. Run `scripts/profile.py` on every file:
   ```bash
   python scripts/profile.py <file_path> [--output <output.json>] [--demo]
   ```
3. Collect results sorted by file size. For files >2GB, use `--sample-limit N`.

Scripts are located in the skill directory at `skills/prep-data-discover/scripts/` or `skills/nemoclaw-user-prep-data/scripts/`.

**No user-facing output in this phase.** Results feed Phase 2 directly.

## Phase 2: Assessment

### Step 1 — Goal-to-column mapping

Before judging relevance, map each concept in the user's goal to available columns:
- "neighborhood" → Borough + ZIP + Community Board
- "rush hour" → any timestamp column + HOUR() filter
- "location" → Latitude/Longitude or BBL or Address

A concept is only "missing" if NO combination of columns can derive it.

### Step 2 — Relevance filter

Rate each dataset: High / Medium / Low / Irrelevant. Only High and Medium proceed. Do not show Low/Irrelevant datasets to the user.

### Step 3 — Quality checklist

For each relevant dataset, read quality issues from `profile.py` output's `quality_issues` list. Auto-detected issues include:

**Structural:** `empty_column`, `constant_column`, `high_null_rate`, `problematic_column_name`, `suspected_duplicate_columns`

**Value-level:** `casing_inconsistency`, `whitespace_drift`, `mixed_date_formats`, `invalid_dates`, `mixed_boolean_encoding`, `numeric_stored_as_string`, `mixed_numeric_string`, `negative_value_unexpected`, `suspicious_zero`, `geographic_out_of_bounds`, `exact_row_duplicates`, `duplicate_identifier`

Agent-only checks: referential integrity, temporal alignment, granularity match across datasets, near-duplicate rows.

### Step 4 — Cross-dataset checks

Among relevant datasets, identify joinable fields (shared column names, overlapping values).

### Step 5 — Present assessment report

```
For each RELEVANT dataset (High/Medium only):
  - Relevance: High/Medium + reasoning
  - Quality issues found (specific)
  - Cross-dataset joins possible

Summary: X datasets found, Y relevant, Z quality issues detected.
```

**Tell the user:** "Assessment complete. Run `prep-data-clean` to create and execute a cleaning plan."

## Constraints

- Scan ALL CSV files. Do not stop early.
- Never modify original files.
- Relevance judgments MUST include reasoning.
- If no datasets score High/Medium, tell the user and ask to clarify.
- Keep profiling output on disk, not in the prompt (token budget).
