---
name: blocksense-poster
description: Generate a printable community poster/flyer from Urban Dossier neighborhood analysis data. Use when the user asks to create a poster, flyer, community handout, or printable neighborhood summary. Trigger keywords - poster, flyer, community handout, print, brochure, neighborhood summary.
---

# BlockSense Poster

Generates a self-contained HTML community flyer poster from Urban Dossier analysis data. Output is a single HTML file with all styles inlined — no CDN dependencies, no external images, fully offline-safe and print-ready.

## When to Trigger

**Trigger when:**
- User asks to create a poster, flyer, community handout, or printable summary
- User wants a visual neighborhood report for printing or sharing
- User mentions "brochure", "print", "community handout", or "neighborhood summary"

**Do NOT trigger when:**
- User asks for raw data or JSON output (use prep-data instead)
- User wants an interactive web dashboard
- User asks to analyze data without producing a visual output

## Prerequisites

- Python 3.9+
- Jinja2 (`pip install jinja2`)
- Urban Dossier analysis JSON (output of `_build_detail_payload()`)

Run `bash bootstrap.sh` once to verify and install dependencies.

## Workflow

### Phase 1: Data Extraction (deterministic)

Extract poster-relevant highlights from the analysis JSON.

```bash
python3 scripts/extract_highlights.py <analysis.json> [--output highlights.json] [--demo]
```

- Reads the full Urban Dossier analysis JSON
- Extracts scores, key findings, priority actions, location metadata
- Derives score labels and colors from overall score
- Handles missing fields gracefully (defaults to null/empty)
- Outputs a compact `highlights.json` for template rendering

### Phase 2: Headline Generation (LLM)

Generate a headline and summary for the poster. The LLM should produce:

- **Headline**: <15 words, factual, mentions the location name
- **Summary**: <50 words, balances positive and negative findings, plain language

See `references/poster_guidelines.md` for detailed rules.

Example prompt to the LLM:

```
Given these neighborhood highlights:
- Location: {location_name}, {borough}
- Overall score: {overall_score}/100 ({score_label})
- Top findings: {top_findings}
- Priority action: {priority_action}

Generate:
1. A poster headline (<15 words, factual, mention the location)
2. A brief summary (<50 words, balance positive and negative)

Do NOT invent numbers. Use plain language.
```

### Phase 3: Rendering (deterministic)

Render the final poster HTML from template + data.

```bash
python3 scripts/render_poster.py \
    --highlights highlights.json \
    --headline "Your Neighborhood at a Glance" \
    --summary "Brief balanced description of the area." \
    [--template offline|horizontal|analytical] \
    [--output poster.html] \
    [--demo]
```

- `--template` choices: `offline` (default portrait A4), `horizontal` (landscape A4), `analytical` (landscape A4 with bar charts)
- Merges highlights JSON + LLM-generated headline + summary into Jinja2 template context
- Outputs a single self-contained HTML file
- All styles are inlined — no CDN, no external fonts, no external images

### Demo Mode

All scripts support a `--demo` flag. When passed, the script checks `scripts/demo_cache/` for a pre-computed result file keyed by the input file's basename. If a cache hit is found, the cached result is returned immediately. If no cache hit, the script runs normally.

**Cache filename conventions** (all live in `scripts/demo_cache/`):

| script | cache filename | example |
|---|---|---|
| `extract_highlights.py <file>` | `{file_basename}.highlights.json` | `analysis.json.highlights.json` |
| `render_poster.py --highlights <file>` | `{file_basename}.poster.html` | `highlights.json.poster.html` |

## Template Choices

| Template | Layout | Best for |
|---|---|---|
| `offline` (default) | Portrait A4 | Simple handouts, door-to-door flyers |
| `horizontal` | Landscape A4 | Community board postings, wide displays |
| `analytical` | Landscape A4 + bar charts | Data-oriented audiences, civic meetings |

All templates are fully offline-safe: system fonts, unicode emoji icons, pure CSS charts, inline styles only.
