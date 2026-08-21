---
name: nemoclaw-user-prep-data
description: "Run the end-to-end multi-file CSV preparation workflow in NemoClaw: discover, assess, propose a cleaning plan, execute after one confirmation, validate, and report. Use when the user wants the whole pipeline handled as one task. For an explicitly phased workflow, use the prep-data-discover, prep-data-clean, or prep-data-report skill instead."
---

# NemoClaw User: Prep Data

Autonomous data preparation pipeline. User says "I want to do X with data in Y" and the agent handles discovery, assessment, cleaning, and reporting — with a single user confirmation point on the cleaning plan. The final report is delivered without waiting for confirmation.

## When to Trigger

**Trigger when:**
- User mentions "prepare data", "clean data", "look at my data", "what's wrong with my data"
- User describes a project goal and references local data files or directories
- User asks to process multiple data files in a directory

**Do NOT trigger when:**
- User asks a simple query ("how many rows in this file")
- User already knows the data structure and requests a single specific operation
- User is doing exploratory analysis, not preparation

## Supported Formats

**CSV only.** Input must be UTF-8 (or latin-1; the loader auto-falls back). Output is also CSV. If the user has TSV, Parquet, JSON, or Excel files, ask them to convert to CSV before invoking this skill.

The execution backend is **pandas** (not DuckDB). This means the full file is loaded into memory — for files larger than ~2 GB, pass `--sample-limit N` to `profile.py` and tell the user the assessment is approximate.

## Backends: pandas + cuDF (in-process, two-speed operation)

Two execution backends run **in-process** on the DGX Spark (GB10 ARM64) host. There is no remote service, no Docker container, and no HTTP hop.

1. **pandas** (`scripts/profile.py` / `scripts/clean.py` / `scripts/validate.py`) — the authoritative analyzer. Produces the full `quality_issues` list (20+ auto-detectors) and the structured cleaning log. This is the ONLY backend that supports Phase 2 quality assessment and Phase 3 cleaning.

2. **cuDF (RAPIDS, in-process)** — call `cudf.read_csv(...)` / `cudf.read_parquet(...)` directly inside the same Python process. Used opportunistically for fast first-pass file reads on large datasets. Import is wrapped in `try/except ImportError`, so any script that uses cuDF degrades gracefully to pandas on machines without RAPIDS installed (e.g. a dev Mac).

**When to use which**:
- **Phase 1 Discovery**: use `cudf.read_csv(...)` directly for fast first-pass profiling on large datasets (>100 MB). For smaller files, pandas is fine. The accelerated `scripts/profile.py` will pick cuDF automatically when `import cudf` succeeds and the file is above the size threshold; otherwise it stays on pandas.
- **Phase 2 Assessment**: **pandas only**. cuDF does not support every detector in the `quality_issues` scanner (mixed boolean encoding, lenient date parsing, exact-row dedup at scale, etc.), so the assessment report MUST be built from the pandas `profile.py` output.
- **Phase 3 Execution**: **pandas only**. `scripts/clean.py` is pandas-native; the 12 supported ops rely on pandas semantics (`Series.replace`, `pd.to_datetime(errors='coerce')`, etc.).
- **Phase 4 Report**: cuDF or pandas, in-process. Final-profile reads can use cuDF for speed when available; pandas remains the default.

### Prompt budget & exec output hygiene (important)

Nemotron 30B runs with `max_tokens=4096` in this deployment. The agent's exec-tool responses are fed back into model context for the next turn, so large JSON tool outputs can push the model into `stopReason: length` with zero usable text.

**Rules the agent MUST follow**:
1. Call agent-side data commands with `--thinking off` (or equivalent) unless the command legitimately needs reasoning. Don't burn 73 seconds on a 5-row sample.
2. When profiling many files, **do not dump every profile's full JSON into the next prompt**. Summarize to one line per dataset: `{file, rows, cols, high_null_cols_count}`. Keep the full JSON on disk at `{output_dir}/{file}.profile.json` for Phase 4 reference.
3. Do **not** inline long JSON bodies (cleaning plan, profile output) as strings in agent messages — write them to `/tmp/{task}-{file}.json` and reference the path.

## Workflow Overview

```
User request
    │
    ▼
Phase 1: Discovery (autonomous, internal)
    │ ── results stay internal, feed Phase 2 directly
    ▼
Phase 2: Assessment (autonomous)
    │ ── filter to relevant datasets only
    │ ── present cleaning plan ──▶ USER CONFIRMS / ADJUSTS ◀── intervention
    ▼
Phase 3: Execution (autonomous)
    │
    ▼
Phase 4: Report (autonomous, report-only)
    │ ── present final report to user, end
    ▼
Done
```

---

## Phase 1: Discovery

**Goal:** Silently discover and profile every file in the target directory. Results stay internal and feed Phase 2 — nothing is reported to the user in this phase.

**Steps:**

1. Confirm three things with the user:
   - **Input directory path** — where the raw files live
   - **Output directory path** — where cleaned CSVs, profiles, validation reports, the cleaning log, and the final data dictionary will be written. If the user doesn't specify one, propose `{input_dir}/cleaned/` as the default and confirm.
   - **Project goal** — what they want to do with the data. If the user hasn't stated a goal, ask for one now. A rough, high-level goal is fine — users often haven't looked at the data themselves and can't be precise. You will ground any follow-up clarifying questions in Phase 2, after you have concrete datasets to point at.
2. List all supported files (CSV, TSV, Parquet, JSON, Excel) in the input directory
3. Run `scripts/profile.py` on every file
4. Collect all profiling results (sorted by file size) as internal state. Pass them directly to Phase 2.

**Constraints:**
- Scan ALL CSV files in the input directory. Do not stop after a few. Non-CSV files (TSV/Parquet/JSON/Excel) should be reported to the user with a "convert to CSV first" note and skipped.
- `profile.py` does a **full read by default** — pandas loads the entire CSV into memory. For files >2 GB this can OOM. If a single file is too large to fit in memory, pass `--sample-limit N` to profile.py as a last resort. When that happens, the agent MUST record which datasets were sampled and surface the warning in Phase 2's assessment report, because the duplicate-detection / ID-uniqueness / exact-row-duplicate checks for those datasets are approximate, not exact.
- If a file cannot be read (corrupt, encoding error, malformed CSV), profile.py returns a `{"status": "error"}` result. Record it internally; surface to the user only if the file appeared potentially relevant based on filename.
- **Phase 1 has NO user-facing output beyond the initial confirmations.** Do not summarize findings, do not list discovered files, do not report volumes. The user should only see results in Phase 2's filtered assessment report.

**How to run profiling:**
```bash
python scripts/profile.py <file_path> [--output <output.json>] [--demo]
```

---

## Phase 2: Assessment

**Goal:** Filter discovered datasets to the ones that actually matter for the project goal, analyze their quality, and propose a cleaning plan.

**Steps:**

0. **Goal-to-column mapping (concept resolution).** Before deciding any dataset is "missing" something, identify every concept word in the user's project goal that sounds like a *data dimension* (e.g. "neighborhood", "rush hour", "weekend", "borough", "type", "category"), and map each one to the SUPPORTING column(s) in the candidate datasets that can derive it. **A concept is only "missing" if no combination of available columns can derive it.** Examples:

   | Concept word in user's goal | How it can be derived |
   |---|---|
   | "neighborhood" / "area" | `Borough` + `Incident Zip` + `Community Board` (any subset) |
   | "rush hour" / "morning" / "overnight" / "weekend" | `Created Date` (or any timestamp) + HOUR() / DAYOFWEEK() filter at query time |
   | "year" / "month" / "season" | Any timestamp column, post-cleaning truncated |
   | "borough" | `Borough`, or `BBL` first digit, or `Borough Code` |
   | "category" / "type" | Any low-cardinality categorical column |
   | "location" / "site" | `Latitude/Longitude`, OR `BBL`, OR `Address` + geocode |
   | "agency" / "department" | `Agency`, or `Agency Name` |
   | "duration" | `start_date` + `end_date` (compute the diff) |

   Record this mapping internally and apply it in step 1 below. **Never report a column as MISSING in the user-facing report unless you have explicitly checked that no combination of present columns can derive the concept.** The most common failure of LLM-driven data prep is treating a question word like "neighborhood" as a literal required column name and incorrectly declaring the dataset insufficient.

1. **Relevance filter.** Based on the user's stated project goal AND the goal-to-column mapping from Step 0, assess each dataset's relevance: High / Medium / Low / Irrelevant. Record reasoning for ALL four tiers internally, but ONLY High and Medium datasets proceed to the next steps and appear in the user-facing report. Low and Irrelevant datasets are silently dropped — do NOT mention them to the user unless the user explicitly asks what else was in the directory.
2. **Vague-goal check.** If the user's goal is too vague to assign relevance with confidence (e.g., most datasets end up Medium/Low with no clear separation, or nothing scores High), STOP and ask the user a clarifying question grounded in concrete datasets you actually found. Example: "I see crime complaints, weather, and subway data in there — are you focused on crime patterns, weather correlations, or transit coverage?" Wait for the answer, then redo the relevance filter. Never ask a generic "what do you want to do" question — always ground it in specific files you found.
3. **Checklist walkthrough.** For each relevant dataset (High/Medium only), walk through EVERY item in `references/quality_checklist.md`. Do not skip items. **Optimization:** `profile.py` now auto-detects MOST checklist items — read them directly from the profile output's `quality_issues` list instead of re-judging. The auto-detected issue types are:

   *Structural (column-level):*
   - `empty_column`, `constant_column`, `high_null_rate`, `problematic_column_name`, `suspected_duplicate_columns`

   *Value-level (Patch A — added in the pandas rewrite):*
   - `casing_inconsistency` — same value with different cases (e.g. "Brooklyn" vs "brooklyn")
   - `whitespace_drift` — leading/trailing whitespace in cell values
   - `mixed_date_formats` — multiple date format patterns in the same column
   - `invalid_dates` — values that look like dates but don't parse (e.g. `2024-13-45`)
   - `mixed_boolean_encoding` — column uses 3+ encodings like {Y, yes, TRUE} simultaneously
   - `numeric_stored_as_string` — column should be cast to a numeric type
   - `mixed_numeric_string` — 70%+ values are numeric but the rest is non-numeric garbage
   - `negative_value_unexpected` — negative values in a column whose name suggests positives only (price, qty, rate, etc.)
   - `suspicious_zero` — minority zeros in a column where zeros are unusual
   - `geographic_out_of_bounds` — lat outside [-90, 90] or lon outside [-180, 180]
   - `exact_row_duplicates` — rows that match every column exactly
   - `duplicate_identifier` — values repeating in a column the role-inference flagged as `identifier`

   The agent does NOT need to re-judge any of the above. Read them off the profile output. The remaining checklist items that still require agent judgment are: **referential integrity across datasets**, **temporal alignment across datasets**, **granularity match across datasets**, and **near-duplicate rows that differ in 1–2 columns** (not exact). Everything else is now mechanical.
4. **Cross-dataset checks.** Among the relevant datasets only, check for joinable fields (shared column names, overlapping value domains, compatible key formats).
5. **Assessment report.** Produce a structured report covering only the relevant datasets.
6. **Cleaning plan.** Produce a value-level cleaning plan: each operation specifies exact column, exact values, exact transformation. **Every operation MUST be drawn from the Available Operations table below** — do not invent operation names that are not in the table.

### Available Operations (`clean.py` vocabulary)

The cleaning plan is a JSON array of operations, executed in order. Each operation is a dict with an `op` key naming one of the 12 supported operations below. Any other operation name will be rejected by `clean.py`.

**Column operations**

| op | required params | optional params | effect |
|---|---|---|---|
| `drop_column` | `column` | — | Drop a column from the table. |
| `rename_column` | `old_name`, `new_name` | — | Rename a column. |

**Value modification**

| op | required params | optional params | effect |
|---|---|---|---|
| `replace_values` | `column`, `mapping` (dict of `{old_value: new_value}`) | — | Map values in a column. Unmapped values pass through unchanged. Example: `{"op": "replace_values", "column": "BORO", "mapping": {"MN": "manhattan", "BK": "brooklyn"}}`. |
| `cast_type` | `column`, `target_type` | — | Cast a column to a new type. Invalid values become NULL (uses `pd.to_numeric(errors='coerce')` / `pd.to_datetime(errors='coerce')` / lenient boolean coercion). Allowed types: `BOOLEAN`, `TINYINT`, `SMALLINT`, `INTEGER`, `BIGINT`, `HUGEINT`, `UTINYINT`, `USMALLINT`, `UINTEGER`, `UBIGINT`, `FLOAT`, `REAL`, `DOUBLE`, `DECIMAL(p,s)`, `VARCHAR`, `TEXT`, `BLOB`, `DATE`, `TIME`, `TIMESTAMP`, `TIMESTAMP_S`, `TIMESTAMP_MS`, `TIMESTAMP_NS`, `INTERVAL`, `UUID`, `JSON`. (Internally these map to pandas dtypes — `DECIMAL` degrades to float64, `TIME` keeps as string.) |
| `strip_whitespace` | `column` | — | Removes leading/trailing whitespace from a string column (`Series.str.strip()`). |
| `transform_case` | `column` | `mode` (default `lower`, one of `lower`/`upper`/`title`) | Change text case. `title` only capitalizes the first character. |

**Row filtering**

| op | required params | optional params | effect |
|---|---|---|---|
| `drop_duplicates` | — | `subset` (list of column names) | With `subset`: keep the first row per partition key. Without `subset`: drop full-row duplicates. |
| `drop_nulls` | `column` | — | Delete rows where the given column is NULL. |
| `filter_rows` | `column`, `condition` | `value`, `value2`, `values` (depends on condition) | **KEEP rows matching the condition, delete the rest.** Conditions: `gt`, `lt`, `gte`, `lte`, `eq`, `neq` (use `value`); `between` (use `value` and `value2`); `in`, `not_in` (use `values` list); `is_null`, `not_null` (no value needed). |

**Geographic enrichment**

| op | required params | optional params | effect |
|---|---|---|---|
| `add_h3_index` | `lat_col`, `lon_col` | `resolution` (0–15, default `9`), `output_col` (default `h3_index`) | Add a VARCHAR column with the H3 cell index at the given resolution, computed from lat/lon. Enables fast radius queries via H3 `grid_disk` at query time. Rows with NULL coordinates get NULL index. |
| `transform_coords` | `x_col`, `y_col`, `source_crs` | `target_crs` (default `EPSG:4326`), `out_lon_col` (default `longitude`), `out_lat_col` (default `latitude`) | Reproject a pair of X/Y columns from one CRS to another via pyproj, writing two new DOUBLE columns. CRS strings must be of the form `EPSG:<digits>`. Primary use case: NY State Plane `EPSG:2263` → WGS84 for datasets that ship only state-plane coordinates (e.g. NYC 311). |
| `polygon_centroid` | `geom_col` | `out_lon_col` (default `centroid_lon`), `out_lat_col` (default `centroid_lat`) | Parse WKT geometry (POINT / POLYGON / MULTIPOLYGON) in `geom_col` via shapely and write the centroid into two new DOUBLE columns. Null, empty, or unparseable geometries get NULL outputs. |

**Guardrails enforced by `clean.py`**
- Column names are validated against the live `df.columns` whitelist — unknown columns raise an error.
- `cast_type` target types are whitelist-only; `DECIMAL(p,s)` allows digits/commas inside the parens.
- All user-supplied values flow through pandas API calls (`Series.replace`, `Series.isin`, comparison operators) that take values, not strings — there is no SQL interpolation surface.
- `clean.py` **never** calls `df.eval()` or `df.query()`. No op exposes a free-form expression slot.
- `add_h3_index`, `transform_coords`, and `polygon_centroid` refuse to overwrite an existing column.
- Order matters: operations run sequentially, and each op sees the post-state of the previous one. If you `rename_column` first and then reference the old name, the second op fails.

**Assessment report structure:**
```
For each RELEVANT dataset (High/Medium only — Low/Irrelevant are not shown):
  - Relevance: High/Medium
  - Relevance reasoning: why this dataset matters for the project goal
  - Quality issues found: list each issue with specifics
  - Cross-dataset joins: which columns could link to which other relevant datasets

Cleaning plan:
  For each dataset:
    - List of operations, each one a specific clean.py operation with exact parameters
    - Example: {"op": "replace_values", "column": "BORO", "mapping": {"MN": "manhattan", "BK": "brooklyn"}}
```

**Constraints:**
- Relevance judgments MUST include reasoning. Never just say "High" without explaining why. Reasoning for Low/Irrelevant stays in internal state; reasoning for High/Medium is user-visible.
- NEVER list Low/Irrelevant datasets in the user-facing report. The user asked for help on their project, not a directory listing.
- Cleaning operations MUST be value-level specific. Not "standardize the BORO column" but "replace 'MN' with 'manhattan', 'BK' with 'brooklyn', ..." with every value listed.
- If you cannot determine what a column means, say "I'm not sure what this column represents" explicitly.
- NEVER include delete/drop-rows operations without listing them in the plan for user review.
- If a column's meaning is ambiguous, flag it and ask in the intervention point.
- If NO datasets score High or Medium, don't fabricate a plan. Tell the user "I couldn't find any datasets in this directory that clearly match your project goal" and ask them to clarify or point at specific files.

**>>> INTERVENTION POINT <<<**
Present the assessment report and cleaning plan to the user. Wait for confirmation or adjustments before proceeding to Phase 3. If the user disagrees with any part of the plan, iterate conversationally until they approve it — do not proceed to execution on a disputed plan.

---

## Phase 3: Execution

**Goal:** Execute the confirmed cleaning plan.

**Steps:**

1. **Resume check.** Before starting, check whether `cleaning_log.json` already exists in `{output_dir}`. If it does, read it and collect the set of datasets with `status: "ok"` — those are already done and MUST be skipped on this run. Report which datasets are being skipped.
2. **Initialize log if missing.** If `cleaning_log.json` does not exist, write an empty skeleton: `{"started_at": <iso8601>, "datasets": []}` and fsync.
3. For each dataset NOT in the skip set:
   1. Execute the confirmed operations via `scripts/clean.py`
   2. Immediately run `scripts/validate.py` to compare before/after
   3. Append the combined `{dataset, clean_result, validation_result}` entry to `cleaning_log.json`, **rewrite the full file, flush, and fsync**. Do this **before** moving to the next dataset. This is the crash-safe commit point.
4. If validate.py detects new problems introduced by cleaning (e.g., new nulls from type casting), record them in the log entry but continue to the next dataset.
5. After all datasets are processed, set the top-level `finished_at` field and output a summary.

**How to run cleaning:**
```bash
python scripts/clean.py <input.csv> <output.csv> --ops '<json_array_of_operations>' [--demo]
```

**How to run validation:**
```bash
python scripts/validate.py <original.csv> <cleaned.csv> [--output <report.json>] [--demo]
```

**Output directory structure:**
```
{output_dir}/
├── {dataset_name}.cleaned.csv          # Cleaned data (always CSV in this version)
├── {dataset_name}.profile.json         # Post-cleaning profile
├── {dataset_name}.validation.json      # Before/after comparison
├── cleaning_log.json                   # Global log — crash-safe, append-on-completion
└── data_dictionary.json                # Structured metadata — written in Phase 4
```

**`cleaning_log.json` schema:**
```json
{
  "started_at": "2026-04-11T14:30:00Z",
  "finished_at": "2026-04-11T14:32:15Z",
  "datasets": [
    {
      "dataset": "complaints.csv",
      "status": "ok",
      "clean_result": { ... output of clean.py ... },
      "validation_result": { ... output of validate.py ... },
      "completed_at": "2026-04-11T14:31:02Z"
    }
  ]
}
```

**Constraints:**
- NEVER modify original files. All output goes to a new directory.
- Every completed dataset is appended to `cleaning_log.json` and fsynced **before** the agent moves on. One dataset = one atomic commit. This is how resume works after a crash or timeout.
- If an operation fails inside clean.py, it returns a non-ok result — log the entry with `status: "error"` so resume does NOT skip it (the agent gets another chance next run).
- Validation is MANDATORY after each successful clean. Never skip it.
- Input AND output are CSV. The pandas backend does not write Parquet in this version. If you need Parquet output later, swap `df.to_csv` for `df.to_parquet` in `clean.py` (one line) and add `pyarrow` to requirements.

---

## Phase 4: Report

**Goal:** Produce a data dictionary and final summary.

**Steps:**

1. Run `scripts/profile.py` on each cleaned CSV file to get final stats
2. For each cleaned dataset, generate a data dictionary entry
3. Summarize cross-dataset relationships
4. Write the structured metadata to `{output_dir}/data_dictionary.json` (machine-readable) AND present a human-readable summary of the same content to the user in the chat

**Data dictionary entry structure:**
```
Dataset: {name}
Source: {original_file_path}
Rows: {count}  Columns: {count}  Time range: {if applicable}

Fields:
  {column_name}:
    Type: {data_type}
    Inferred meaning: {what this column likely represents}
    Null rate: {percentage}
    Unique values: {count}
    Sample values: {3-5 examples}
    Related to: {other_dataset.column, if applicable}

Cleaning applied:
  - {operation 1 description}
  - {operation 2 description}

Known limitations:
  - {any remaining quality issues}
```

**>>> FINAL REPORT (no confirmation required) <<<**
Present the final report and data dictionary to the user. This is the end of the workflow — do NOT wait for confirmation, do NOT ask "does this look right?". The user will request rework unprompted if they want it.

---

## Behavioral Constraints (apply to ALL phases)

1. **Never modify original files.** All outputs go to a new directory.
2. **Never make silent decisions.** If the agent drops rows, changes types, or removes columns, it must be in the confirmed plan or reported after the fact.
3. **Run the full checklist.** In Phase 2, every item in `quality_checklist.md` must be checked. Do not shortcut.
4. **Value-level specificity.** Cleaning operations specify exact values, not vague descriptions.
5. **Log everything.** Every operation, every row count change, every failure — logged.
6. **Fail gracefully.** One file failing does not stop the pipeline. Report and continue.
7. **Be honest about uncertainty.** "I don't know what this column is" is better than a wrong guess presented as fact.

## Demo Mode

All three scripts support a `--demo` flag. When passed, the script checks `scripts/demo_cache/` for a pre-computed result file keyed by the **input file's basename**. If a cache hit is found, the cached result is returned and the real computation is skipped. If no cache hit, the script runs normally.

This is for hackathon demos to guarantee fast, deterministic results.

**Cache filename conventions** (all live in `scripts/demo_cache/`, keyed by the basename of the script's primary input):

| script | cache filename | example |
|---|---|---|
| `profile.py <file>` | `{file_basename}.profile.json` | `complaints.csv.profile.json` |
| `clean.py <input> <output> --ops ...` | `{input_basename}.clean.json` | `complaints.csv.clean.json` |
| `validate.py <original> <cleaned>` | `{original_basename}.validation.json` | `complaints.csv.validation.json` |

To populate the cache, run each script normally first, then copy (or redirect with `--output`) the JSON result into `scripts/demo_cache/` using the exact filename above. Cached entries are tagged with `"_demo_cached": true` in the returned JSON so you can tell them apart from live runs.
