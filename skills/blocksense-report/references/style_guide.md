# BlockSense Report — LLM Narrative Style Guide

Guidelines for generating neighborhood analysis narratives with Nemotron 30B (4096 token budget).

## General Rules

1. **Cite specific numbers.** Every sentence should reference at least one concrete metric from the data. Never write vague statements like "the area has some issues."
2. **Use plain-language baselines.** Write "twice the city median" or "in the bottom quarter citywide" — never show raw percentile labels like "P75" or "above the 75th percentile." The reader is a resident or buyer, not a data scientist.
3. **Never show z-scores.** If the data flags an anomaly, describe the effect: "a sharp spike in Q4" not "z-score of 1.8."
4. **Use the location name.** Write "near 123 Main St" or "in this part of Brooklyn" — never "at coordinates (40.68, -73.97)."
5. **Balance positive and negative.** Even a low-scoring dimension has something neutral or positive to note (e.g., "response times remain within city norms despite elevated complaint volumes"). Avoid doom-and-gloom tone.
6. **Active voice, present tense.** "The area shows 12 rodent-positive sites" not "12 rodent-positive sites were found in the area."

## Per-Dimension Narratives

- **Length:** 2-3 sentences per dimension.
- **Structure:** Lead with the most notable finding (highest priority action or strongest anomaly), then provide context with 1-2 supporting metrics.
- **Tone:** Informative and neutral. Not alarmist, not dismissive.

### Examples

**Safety (score 65/100):**
> Rodent activity near 123 Main St is elevated at 12 positive sites within 500 meters, roughly twice the city median of 7. Sanitation complaints are moderate at 45 recent filings, while EMS response averages 7 minutes — within typical citywide range.

**Transit (score 78/100):**
> Public transit access is solid with 2 subway stations and 5 bus stops within walking distance. However, traffic collisions have worsened by 18% recently, reaching 23 incidents in the surrounding area — well above the typical range of 8-15 for similar neighborhoods.

**Amenities (score 80/100):**
> Street tree coverage is strong at 156 trees within 500 meters, with 77% rated in good health. The area benefits from 3 nearby parks including Prospect Park and Fort Greene Park, plus 25 restaurants — 67% holding an A sanitation grade.

**Building (score 55/100):**
> There are 8 open Class C housing violations within 250 meters, with an average age of 450 days — suggesting slow resolution. Three buildings are flagged under the Alternative Enforcement Program, indicating persistent maintenance concerns.

## Synthesis Narrative

- **Length:** 3-4 sentences.
- **Structure:** Open with the overall score and one-line characterization. Connect 1-2 cross-signal patterns. Close with the strongest "why now" signal.
- **Purpose:** Help the reader understand how dimensions interact, not just repeat individual scores.

### Example

> Overall, this area of Brooklyn scores 72/100 — a neighborhood with strong amenities and transit access offset by building maintenance concerns. The co-elevation of collision rates and rodent activity suggests infrastructure stress that affects both street safety and sanitation. With rodent complaints spiking in Q4 and several long-standing building violations unresolved, this is a moment to watch for whether conditions stabilize or continue to slide.

## Token Budget

- Each dimension prompt (data + instructions): **under 800 tokens**.
- Each dimension response: **40-60 words** (2-3 sentences).
- Synthesis response: **50-80 words** (3-4 sentences).
- **Total narrative word count: under 400 words** across all five sections.
- This fits within Nemotron 30B's 4096 token generation budget with room for the prompt.

## What NOT to Do

- Do not invent statistics not present in the segment data.
- Do not use jargon: "percentile", "z-score", "standard deviation", "p-value."
- Do not address the reader directly ("you should..."). Write in third-person observational style.
- Do not include disclaimers like "based on available data" — that is implicit.
- Do not pad with filler. If a dimension has only 2 metrics, two sentences are enough.
