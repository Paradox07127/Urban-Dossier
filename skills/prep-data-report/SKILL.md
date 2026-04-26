---
name: prep-data-report
description: Generate a data dictionary and final summary after cleaning. Use after prep-data-clean has executed the cleaning plan. Profiles cleaned files, generates structured metadata, and presents a human-readable summary. Trigger keywords - generate report, data dictionary, what did we clean, summarize the data.
---

# Prep Data: Report

Profile cleaned files, generate a data dictionary, and present a final summary. This is Phase 4 of the data preparation pipeline.

## When to Trigger

- After `prep-data-clean` has finished execution
- User says "generate report", "data dictionary", "summarize"

## Steps

1. Run `scripts/profile.py` on each cleaned CSV file in the output directory:
   ```bash
   python scripts/profile.py <cleaned_file> [--output <profile.json>] [--demo]
   ```

2. For each cleaned dataset, generate a data dictionary entry:
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

3. Summarize cross-dataset relationships (shared keys, join paths).

4. Write structured metadata to `{output_dir}/data_dictionary.json`.

5. Present the human-readable summary to the user.

## Constraints

- This is a report-only phase. Do not modify any data files.
- Do NOT wait for confirmation after presenting. The user will request changes if needed.
- If cleaning_log.json exists, read it to get the list of cleaned datasets and operations applied.
- Be honest about remaining limitations and data gaps.
