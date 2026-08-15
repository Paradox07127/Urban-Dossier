---
name: urban-dossier-analyst
description: Goal-driven NYC neighborhood analysis agent. Use when the user asks open-ended questions about NYC neighborhoods (compare, find, simulate, recommend) or wants the system to figure out which datasets to consult and how. Trigger keywords - "find me a neighborhood that...", "compare X and Y", "recommend areas for...", "what would happen if...", "which neighborhoods need...", "explain why this area..."
---

# Urban Dossier Analyst

Master ReAct agent for the Urban Dossier system. Runs against a local vLLM
server with `--enable-auto-tool-choice` and a tool-call parser, on an
NVIDIA RTX PRO 6000 Blackwell Workstation Edition (96 GB, compute capability
12.0). Earlier revisions of this file said DGX Spark (GB10, ARM64, 128 GB
unified); that was never this machine, and the difference decides which of
NVIDIA's serving recipes applies — see `MODEL_CANDIDATES.md`.

The served checkpoint is deployment configuration, not part of this skill.
Nemotron-3-Nano, Nemotron-3.5-Lightning and Qwen3.8-27B have all run this
loop unchanged; the reasoning field is read from whichever channel the
runtime provides, and the prompts name no model. See `MODEL_CANDIDATES.md`
for what is currently deployed and what is under evaluation.

The skill exposes a single Python entry point - `run_agent(user_message, ...)` -
that drives a Thought-Action-Observation loop over a fixed set of 8 tools and
returns a structured, evidence-cited answer.

## When to Trigger

**Trigger when** the user asks an open-ended question about NYC neighborhoods
that requires the system to decide which datasets to consult and in what
order. Typical phrasings:

- "Find me a quiet neighborhood with good transit near a park"
- "Compare Astoria and Williamsburg for a young family"
- "Recommend areas with low rodent complaints and lots of trees"
- "What would happen if we added 3 bike lanes here?"
- "Which neighborhoods most need a public toilet?"
- "Explain why this area got a low safety score"

**Do NOT trigger when** the user has already supplied an exact lat/lon and
wants a single direct lookup - that should bypass the agent and call the
backend `/api/analyze-point` endpoint directly. The ReAct loop adds latency
and reasoning tokens that are wasted on a one-shot query.

Other anti-triggers:

- The user is uploading or preparing raw data (route to a data-prep skill).
- The user wants a polished printable report (route to the report skill).
- The user wants a poster (route to the poster skill).

## Tool catalog

The catalog contains eight locked names. Runtime release gates publish only the
subset whose graph, fitted curves or vector index is valid; never claim a
catalogued but unpublished tool ran.

| # | Tool | Purpose |
|---|------|---------|
| 1 | `score_neighborhood(latitude, longitude, radius_m=500)` | Four category scores for a point (safety, transit, amenities, building). |
| 2 | `compare_neighborhoods(point_a, point_b, radius_m=500)` | Side-by-side score comparison of two points. |
| 3 | `query_dataset(dataset_id, filters, limit=100)` | Filtered raw rows from one of the 18 NYC Open Data sources. |
| 4 | `find_similar_neighborhoods(latitude, longitude, k=5)` | Reserved for score-vector KNN; not released while the implementation is a watchlist approximation. |
| 5 | `walking_isochrone(latitude, longitude, minutes=10)` | GeoJSON polygon reachable on foot within N minutes. |
| 6 | `simulate_intervention(latitude, longitude, intervention_type, count=1)` | What-if projection for adding bike_lane / park / toilet / linknyc / bus_stop. |
| 7 | `search_address(query, limit=5)` | Geocode an address or place name to candidate lat/lon. |
| 8 | `retrieve_dataset_docs(query, dataset_filter=None, top_k=5)` | RAG over dataset documentation - the primary anti-hallucination guard. |

The 18 datasets behind these tools span four categories:

- **Safety:** collisions, rodent_complaints, 311_sanitation, ems_response, fire_response
- **Transit:** collision_transport, subway_stations, bus_stops, bike_routes, open_streets
- **Amenities:** parks_access, street_trees, public_toilets, linknyc_kiosks, restaurants, public_facilities
- **Building:** housing_violations, aep_buildings

## ReAct Loop Behavior

Each iteration:

1. **Thought** - the model reasons about what evidence is still missing.
2. **Action** - the model emits zero or one OpenAI function-tool call.
3. **Observation** - `dispatch_tool` runs the tool and returns a JSON result.
4. The result is appended to the conversation and the loop advances.

Operational limits:

- **Max iterations:** 8. After the 8th iteration the loop appends the
  `FINAL_ANSWER_PROMPT` system message and forces a wrap-up call with
  `tool_choice="none"`.
- **Reflection cadence:** every 3 iterations the loop appends the
  `REFLECTION_PROMPT` system message, asking the model to restate the user
  goal, list evidence gathered, and identify what is missing.
- **Default tool_choice:** `"auto"` for all in-loop calls; `"none"` only on
  the forced wrap-up.

## Reasoning Mode

vLLM is started with `--reasoning-parser nano_v3`, which exposes the
Nemotron internal chain-of-thought as a separate `reasoning_content` channel.
The agent uses `reasoning_effort: medium` by default - high enough for
multi-step planning, low enough to keep total response time under ~15s on
GB10. Visible Thought content stays terse; long internal chains live in the
hidden reasoning channel.

## Failure Recovery

`dispatch_tool` never raises. Every failure mode produces a JSON observation:

| Failure | Observation shape | Model expected behavior |
|---------|-------------------|-------------------------|
| Unknown tool name | `{"error": "Unknown tool ...", "retry_hint": "..."}` | Pick a valid tool. |
| Pydantic validation fails | `{"error": "Argument validation failed: ...", "retry_hint": "..."}` | Re-issue with corrected args. |
| Backend endpoint missing (NotImplementedError) | `{"error": "Tool X requires backend endpoint Y ...", "retry_hint": "..."}` | Skip the tool, try an alternative, or surface the gap to the user. |
| FastAPI unreachable (ConnectionError) | `{"error": "Backend unreachable ...", "retry_hint": "..."}` | Tell the user the backend is down rather than fabricating numbers. |
| Any other exception | `{"error": "Unhandled exception ...", "retry_hint": "..."}` | Try a simpler argument set or pivot. |

## Termination Conditions

The loop terminates when **any** of the following holds:

1. **Final text response** - the model emits a message with non-empty
   `content` and no `tool_calls`. The loop returns that text as the answer.
2. **Max iterations reached** - on iteration 8 the loop appends
   `FINAL_ANSWER_PROMPT` and makes one final call with `tool_choice="none"`.
3. **Repeated identical tool call** - if the same `(tool_name, args)` hash
   appears 3 times in a row, the loop aborts immediately with an explicit
   "stuck in a loop" message and returns whatever evidence was gathered so
   far. The model is steered away from infinite retry storms.

## Output Format

`run_agent` returns a `dict` matching `schemas.AgentResponse`:

```json
{
  "answer": "Astoria scores higher on amenities (0.78) than Williamsburg (0.71)...",
  "evidence": [
    {"source": "score_neighborhood", "detail": "scores: ['amenities','safety','transit']"},
    {"source": "search_address", "detail": "1 address candidate(s) returned"}
  ],
  "tools_called": ["search_address", "score_neighborhood", "score_neighborhood"],
  "iterations": 4,
  "trace": [
    {"iteration": 0, "tool_name": "search_address", "args": {...}, "result": {...}, "latency_ms": 142}
  ]
}
```

The `trace` is the full Thought-Action-Observation log and is intended for
the frontend timeline view. The `evidence` list is a reduced citation set
suitable for inline rendering inside the answer prose.

## Example Conversations

### Example 1 - Compare two neighborhoods

```
User: Compare Astoria and Williamsburg for a young family.

Thought: I need coordinates for both neighborhoods, then category scores
         for each.
Action: search_address(query="Astoria, Queens, NY")
Obs:    {"results": [{"latitude": 40.7720, "longitude": -73.9300, ...}]}

Thought: Got Astoria. Now Williamsburg.
Action: search_address(query="Williamsburg, Brooklyn, NY")
Obs:    {"results": [{"latitude": 40.7081, "longitude": -73.9571, ...}]}

Thought: Score both at 500m radius.
Action: score_neighborhood(latitude=40.7720, longitude=-73.9300)
Obs:    {"scores": {"amenities": 0.78, "safety": 0.66, "transit": 0.81, ...}}

Action: score_neighborhood(latitude=40.7081, longitude=-73.9571)
Obs:    {"scores": {"amenities": 0.71, "safety": 0.59, "transit": 0.74, ...}}

Final: Astoria edges Williamsburg on every category for a young family
       (amenities 0.78 vs 0.71, safety 0.66 vs 0.59, transit 0.81 vs 0.74)
       [score_neighborhood for both points].
```

### Example 2 - Find neighborhoods like a seed

```
User: Find me 3 neighborhoods with the same vibe as 100 8th Ave.

Action: search_address(query="100 8th Ave, New York, NY")
Obs:    {"results": [{"latitude": 40.7406, "longitude": -74.0027, ...}]}

Action: find_similar_neighborhoods(latitude=40.7406, longitude=-74.0027, k=3)
Obs:    {"neighbors": [...]}

Final: The three closest matches in score-vector space are ...
       [find_similar_neighborhoods].
```

### Example 3 - Recover from a missing backend endpoint

```
User: What would happen if we added two bike lanes near Union Square?

Action: search_address(query="Union Square, Manhattan")
Obs:    {"results": [{"latitude": 40.7359, "longitude": -73.9911, ...}]}

Action: simulate_intervention(latitude=40.7359, longitude=-73.9911,
                              intervention_type="bike_lane", count=2)
Obs:    {"error": "Tool simulate_intervention requires backend endpoint
        POST /api/simulate (not yet implemented). ...",
        "retry_hint": "..."}

Thought: simulate_intervention is unavailable. Fall back to scoring the
         current state and noting the limitation.
Action: score_neighborhood(latitude=40.7359, longitude=-73.9911)
Obs:    {"scores": {...}}

Final: I cannot project the intervention impact yet (the /api/simulate
       endpoint is not deployed). Today the area scores transit 0.83 and
       safety 0.61 [score_neighborhood]. Once /api/simulate ships, the
       agent will surface the projected delta.
```

## Constraints

Hard rules the agent must obey:

- **Never invent dataset names.** Only the 17 listed dataset ids and the
  five category aliases (`safety`, `transit`, `amenities`, `building`,
  `overall`) are valid.
- **Never invent column names.** When unsure, call `retrieve_dataset_docs`.
- **Always cite the dataset.** Every numeric claim and every superlative
  ("most", "fewest", "highest") in the final answer must reference the
  underlying tool call and dataset.
- **Never fabricate coordinates.** Resolve place names via `search_address`
  before any spatial tool.
- **Never silently mock.** When a tool returns `{"error": ...}`, surface the
  gap to the user; do not pretend the call succeeded.
- **Stay inside NYC.** All tools enforce the NYC bounding box
  (lat 40.4 - 40.95, lon -74.3 to -73.7). Out-of-range arguments are rejected
  by Pydantic before any backend call.
