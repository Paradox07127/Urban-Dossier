---
name: blocksense-report
description: Generate a deep neighborhood analysis report from Urban Dossier data. Splits analysis into per-dimension segments for focused LLM narrative generation, then assembles into HTML and Markdown reports. Use when the user asks for a detailed report, deep analysis, or comprehensive neighborhood assessment. Trigger keywords - report, deep analysis, detailed report, neighborhood assessment, comprehensive analysis, generate report.
---

# BlockSense Report

Deep neighborhood analysis report generator. Takes an Urban Dossier analysis JSON payload and produces a multi-section HTML + Markdown report with LLM-generated narratives for each dimension (safety, transit, amenities, building) plus a cross-signal synthesis.

## When to Trigger

**Trigger when:**
- User asks for a "report", "deep analysis", "detailed report", or "comprehensive assessment"
- User wants a neighborhood writeup or summary document
- User asks to generate a report from Urban Dossier analysis output

**Do NOT trigger when:**
- User asks a quick question about a single metric ("what's the transit score?")
- User wants to modify or rerun the Urban Dossier analysis itself
- User wants raw data export without narrative

## Workflow Overview

```
User request (analysis.json path, or auto-detect from recent analysis)
    |
    v
Phase 1: Data Extraction (autonomous)
    | -- extract_segments.py splits analysis into per-dimension segments
    v
Phase 2: Narrative Generation (LLM calls)
    | -- one focused prompt per dimension (safety, transit, amenities, building)
    | -- one synthesis prompt for cross-signal patterns
    | -- write narratives.json
    v
Phase 3: Report Rendering (autonomous)
    | -- render_report.py merges segments + narratives into HTML + Markdown
    v
Done — deliver report files to user
```

---

## Phase 1: Data Extraction

**Goal:** Split the full Urban Dossier analysis payload into per-dimension segments optimized for focused LLM prompting.

**Steps:**

1. Locate the analysis JSON file. If the user provides a path, use it. Otherwise, look for the most recent `*_analysis.json` or `*_detail.json` in the working directory.
2. Run `extract_segments.py` to split by dimension.

**How to run extraction:**
```bash
python3 scripts/extract_segments.py <analysis.json> --output /tmp/segments.json
```

With demo mode (uses cached output from `scripts/demo_cache/`):
```bash
python3 scripts/extract_segments.py <analysis.json> --output /tmp/segments.json --demo
```

**Output:** `segments.json` — one segment per dimension with metrics, trends, enriched context, and related actions. See `extract_segments.py` for the full schema.

**Constraints:**
- Missing fields in the input are handled gracefully — the segment is still emitted with whatever data is available.
- Exit code 0 on success, 1 on error (invalid JSON, file not found, etc.).

---

## Phase 2: Narrative Generation

**Goal:** Generate 2-3 sentence narratives for each dimension, plus a 3-4 sentence cross-signal synthesis, using focused per-dimension prompts.

**Steps:**

1. Read `segments.json` from Phase 1.
2. For each dimension (`safety`, `transit`, `amenities`, `building`), build a focused prompt containing ONLY that dimension's data:
   - Score, metrics with baselines, trends, enriched context, related actions
   - Keep each segment prompt under **800 tokens** (critical for Nemotron 30B's 4096 token budget)
3. Call the LLM for each dimension. Expected output: 2-3 sentences citing specific numbers.
4. Build a synthesis prompt containing:
   - All four dimension scores
   - The `cross_signal_patterns` array
   - The `why_now` array
   - The top 3 priority actions
5. Call the LLM for the synthesis. Expected output: 3-4 sentences connecting cross-signal patterns.
6. Write all narratives to `narratives.json`.

**Prompt template for each dimension:**
```
You are writing a neighborhood analysis for {{ location.name }}, {{ location.borough }}.
Focus on {{ dimension }} (score: {{ score }}/100).

Key metrics:
{% for m in metrics %}
- {{ m.label }}: {{ m.value }} {{ m.unit }}{% if m.annotation %} ({{ m.annotation }}){% endif %}
{% endfor %}

{% if trends %}Trends:
{% for t in trends %}- {{ t.signal }}: {{ t.direction }}{% if t.delta_pct %} ({{ t.delta_pct }}% change){% endif %}{% if t.is_anomaly %} [ANOMALY: {{ t.anomaly_context }}]{% endif %}
{% endfor %}{% endif %}

Write 2-3 sentences analyzing this dimension. Cite specific numbers. Compare to baselines using plain language ("twice the city median", not "P75"). Balance positive and negative findings.
```

**Prompt template for synthesis:**
```
You are writing the synthesis section of a neighborhood analysis for {{ location.name }}.
Scores: Safety {{ scores.safety }}, Transit {{ scores.transit }}, Amenities {{ scores.amenities }}, Building {{ scores.building }}. Overall: {{ overall_score }}/100.

Cross-signal patterns:
{% for p in patterns %}- {{ p.title }}: {{ p.summary }}
{% endfor %}

Why now:
{% for w in why_now %}- {{ w.signal }}
{% endfor %}

Top actions:
{% for a in priority_actions[:3] %}- {{ a.action }} (priority: {{ a.priority_score }})
{% endfor %}

Write 3-4 sentences connecting cross-signal patterns and explaining why this area deserves attention now. Keep total under 80 words.
```

**Fallback behavior:** If the LLM fails on any segment (timeout, error, empty response), generate a data-only fallback:
```
{{ dimension|title }} scores {{ score }}/100. Key metrics: {{ metric_summary }}.
```
This ensures the report is always generated, even if the LLM is unavailable.

**narratives.json structure:**
```json
{
  "safety": "The area shows elevated rodent activity...",
  "transit": "Public transit access is strong with...",
  "amenities": "Street trees and park access are well above...",
  "building": "Housing violations are a concern with...",
  "synthesis": "Overall, this neighborhood scores 72/100..."
}
```

Write the narratives file:
```bash
cat > /tmp/narratives.json << 'EOF'
{ ... assembled narratives ... }
EOF
```

**Constraints:**
- Each dimension prompt MUST stay under 800 tokens. Strip enriched context fields if the prompt exceeds budget.
- Never show raw z-scores or percentile labels (P25, P75) to the user. Translate to plain language.
- Use the location name, not coordinates.
- Follow `references/style_guide.md` for tone and formatting rules.

---

## Phase 3: Report Rendering

**Goal:** Merge segments and narratives into final HTML and Markdown reports.

**Steps:**

1. Run `render_report.py` with segments and narratives as inputs.

**How to run rendering:**
```bash
python3 scripts/render_report.py \
  --segments /tmp/segments.json \
  --narratives /tmp/narratives.json \
  --template templates/report.html \
  --output-html /tmp/report.html \
  --output-md /tmp/report.md
```

With demo mode:
```bash
python3 scripts/render_report.py \
  --segments /tmp/segments.json \
  --narratives /tmp/narratives.json \
  --template templates/report.html \
  --output-html /tmp/report.html \
  --output-md /tmp/report.md \
  --demo
```

**Output:**
- `report.html` — self-contained offline-safe HTML report (no CDN, no external resources)
- `report.md` — plain Markdown version of the same content

2. Present both file paths to the user. If in a web context, offer to open the HTML report.

**Constraints:**
- The HTML report MUST be fully offline-safe. No CDN links, no Google Fonts, no external JS.
- For any dimension where the narrative is missing or empty, `render_report.py` auto-generates a data-only fallback sentence.
- Exit code 0 on success, 1 on error.

---

## Demo Mode

All scripts support a `--demo` flag. When passed, the script checks `scripts/demo_cache/` for a pre-computed result file keyed by the input file's basename. If a cache hit is found, the cached result is returned and the real computation is skipped. If no cache hit, the script runs normally.

**Cache filename conventions** (all live in `scripts/demo_cache/`):

| script | cache filename | example |
|---|---|---|
| `extract_segments.py <file>` | `{file_basename}.segments.json` | `analysis.json.segments.json` |
| `render_report.py --segments <file>` | `{segments_basename}.report.html` | `segments.json.report.html` |

---

## Behavioral Constraints (apply to ALL phases)

1. **Fully offline.** No external network calls. All data is local. Templates are self-contained.
2. **Graceful degradation.** Missing fields produce warnings, not crashes. Missing narratives get data-only fallbacks.
3. **Prompt budget.** Each dimension prompt under 800 tokens. Total narrative generation under 400 words. Fits Nemotron 30B's 4096 token budget.
4. **Honest about data gaps.** If the analysis JSON has a `data_gaps` field, surface those gaps in the report.
5. **No invented data.** Narratives must cite numbers from the actual segments, not hallucinated statistics.
