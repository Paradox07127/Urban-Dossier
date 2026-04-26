# Data Quality Checklist

This checklist is used by the agent during Phase 2 (Assessment). Walk through EVERY item for EVERY relevant dataset. Do not skip items — the value of this checklist is completeness.

For each item, record: Pass / Fail / N/A, with specifics if Fail.

> **Auto-detected items:** Items marked **🤖 AUTO** are computed deterministically by `profile.py` and appear in its `quality_issues` output. The agent should READ them off the profile, not re-judge them. Items marked **👤 AGENT** still require LLM judgment because they involve cross-dataset reasoning or near-duplicate semantics.

---

## 1. Structural Checks

- [ ] **🤖 AUTO Empty columns**: Are there columns that are 100% null? — `quality_issues[type=empty_column]`
- [ ] **🤖 AUTO Constant columns**: Are there columns with only one unique value? — `quality_issues[type=constant_column]`
- [ ] **🤖 AUTO Suspected duplicate columns**: Are there columns with different names but identical values? — `quality_issues[type=suspected_duplicate_columns]`
- [ ] **🤖 AUTO Column name issues**: Spaces or special characters in column names — `quality_issues[type=problematic_column_name]`
- [ ] **👤 AGENT Column count sanity**: Does the number of columns match what you'd expect for this type of data? Flag if suspiciously high (>100) or low (1-2). (Domain judgment required.)

## 2. Completeness Checks

- [ ] **🤖 AUTO High null rate columns**: Columns with >50% null values — `quality_issues[type=high_null_rate]`. The agent still needs to interpret whether the nulls are structural (column doesn't apply to all rows) or quality issues.
- [ ] **👤 AGENT Row completeness**: What percentage of rows have zero null values? What percentage have more than half their columns null? (Compute from `columns[].null_rate`.)
- [ ] **👤 AGENT Time coverage** (if time series): What is the date range? Are there gaps in the time series? (Use `columns[].min`/`max` for any column with `semantic_role=date`.)
- [ ] **🤖 AUTO ID uniqueness**: Identifier columns with duplicate values — `quality_issues[type=duplicate_identifier]`. The role-inference auto-flags identifier columns and the scanner reports collisions.

## 3. Consistency Checks

- [ ] **🤖 AUTO Category standardization (casing)** — `quality_issues[type=casing_inconsistency]` reports value pairs that collide on `lower()` (e.g. "New York" vs "new york" vs "NEW YORK"). Whitespace drift is also auto-detected via `quality_issues[type=whitespace_drift]`.
- [ ] **👤 AGENT Category standardization (abbreviations)**: Auto-detection cannot tell that "NY" and "New York" mean the same thing. The agent still has to spot abbreviation mixing in low-cardinality columns and propose `replace_values` mappings.
- [ ] **🤖 AUTO Date format consistency** — `quality_issues[type=mixed_date_formats]` flags columns where 2+ date formats appear in the same column.
- [ ] **🤖 AUTO Invalid dates** — `quality_issues[type=invalid_dates]` flags values that look like dates but don't parse (e.g. `2024-13-45`).
- [ ] **🤖 AUTO Numeric outliers**:
  - **Negative values where positives expected** — `quality_issues[type=negative_value_unexpected]`
  - **Suspicious zeros** — `quality_issues[type=suspicious_zero]`
  - **>10x above 99th percentile** — *(not yet auto-detected; agent should compute from `columns[].max`)*
- [ ] **🤖 AUTO Geographic bounds** — `quality_issues[type=geographic_out_of_bounds]` flags lat outside [-90, 90] or lon outside [-180, 180]. Agent still has to judge whether the bounds match the *expected* region (e.g. NYC data should fall inside ~40.5–40.9 lat, ~-74.3–-73.7 lon).

## 4. Type Checks

- [ ] **🤖 AUTO String-encoded numbers** — `quality_issues[type=numeric_stored_as_string]` flags string columns where ≥95% of values parse as numeric.
- [ ] **🤖 AUTO Mixed numeric/string** — `quality_issues[type=mixed_numeric_string]` flags string columns where 70-95% are numeric and the rest is garbage (with example values).
- [ ] **🤖 AUTO Mixed boolean encoding** — `quality_issues[type=mixed_boolean_encoding]` flags low-cardinality string columns using 3+ different boolean tokens simultaneously (e.g. {Y, yes, TRUE}).
- [ ] **👤 AGENT String-encoded dates**: Date stored as string is *not* itself an issue — the auto-detector flags mixed formats and invalid values, but a uniformly-formatted string date column is fine. Agent should propose a `cast_type` to `TIMESTAMP` only if downstream operations need datetime arithmetic.

## 5. Cross-Dataset Checks

Only applicable when multiple datasets are being assessed together. **All items in this section are 👤 AGENT** — `profile.py` only sees one file at a time and cannot reason across them.

- [ ] **👤 AGENT Shared column names**: List columns that appear in multiple datasets with the same name. (Inspect each profile's `columns[].name` lists.)
- [ ] **👤 AGENT Value domain overlap**: For shared columns, do the values actually overlap? Compare `columns[].sample_values` between datasets.
- [ ] **👤 AGENT Key compatibility**: If datasets share an ID-like column, are the formats compatible? Watch for `int` vs `int.0` vs string drift — this is the failure mode the D-bench D6 task highlighted.
- [ ] **👤 AGENT Temporal alignment**: If multiple datasets have date columns, do their time ranges overlap? Compare `columns[].min`/`max` for date columns.
- [ ] **👤 AGENT Granularity match**: Are the datasets at the same level of granularity? Compare `total_rows` and date span.

## 6. Data Integrity Checks

- [ ] **🤖 AUTO Exact duplicate rows** — `quality_issues[type=exact_row_duplicates]` reports the row indices participating in duplicate groups.
- [ ] **👤 AGENT Near-duplicate rows**: Rows that are identical except for one or two columns. Auto-detection cannot guess which fields to ignore — agent has to propose `drop_duplicates` with an explicit `subset` after looking at the data.
- [ ] **👤 AGENT Referential integrity**: If a column references IDs from another dataset, do all referenced IDs actually exist in the other dataset? Cross-dataset check, agent only.
- [ ] **👤 AGENT Monotonicity**: For columns that should increase over time (like IDs or timestamps), do they actually increase monotonically? *(Not yet auto-detected; could be added to a future version of `profile.py`.)*

---

## How to Use This Checklist

1. **Read all 🤖 AUTO items off the profile output** — they live in `profile_result["quality_issues"]`. Do NOT re-judge them in `<think>`; that's how the agent burns its reasoning budget.
2. For each 👤 AGENT item, walk the data and judge.
3. For every issue (auto or agent), propose a specific cleaning operation (value-level, not vague).
4. For each N/A, briefly note why it doesn't apply.
5. Aggregate findings into the assessment report.
6. Cross-dataset checks should be done after individual dataset checks.

The goal is exhaustive coverage. It's better to check something and find it's fine than to skip it and miss a problem.
