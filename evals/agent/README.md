# Agent business evaluation (EXPANSION_PLAN 4.1)

Two fixed corpora, two runners, one rule: **no model switch, KV-cache
change, or agent-prompt rewrite is decided without citing a run of this
directory.** The two halves grade different seams and answer different
questions — keep both.

| Corpus | Runner | Seam | Question it answers |
|---|---|---|---|
| `business_cases.json` | `scripts/evaluate_agent_business.py` | `/api/agent/ask` (full service: intent gate, agent service, sandbox path) | "Is the deployed service behaving?" |
| `model_cases.json` | `scripts/vllm/business_eval.py` | `run_agent` directly, endpoint-swappable | "Which MODEL should serve?" (4.2 / 4.3 decisions) |

## Service-level: `evaluate_agent_business.py`

24 fixed trajectories across evidence lookup, new analysis, product help
and out-of-scope handling. Expectations cover tool presence and order,
forbidden tools, structured evidence and answer guard terms. Collection is
separated from grading: a live run writes one JSON object per case, and
the same artifact can be regraded after scorer changes without spending
another model run.

```bash
# Validate the corpus without a model:
python scripts/evaluate_agent_business.py --validate-only

# Collect and grade a live local service:
python scripts/evaluate_agent_business.py \
  --base-url http://127.0.0.1:8090 \
  --output evals/results/agent-business.jsonl

# Regrade a captured JSONL artifact deterministically:
python scripts/evaluate_agent_business.py \
  --responses evals/results/agent-business.jsonl \
  --output evals/results/agent-business-replay.jsonl
```

The report records the corpus SHA-256, per-check results and pass rates by
business intent. Cases with `release_gate` intentionally remain in the
fixed set when an artifact or implementation is unavailable; benchmarks
must expose those gaps rather than silently dropping the cases.

## Model-level: `business_eval.py`

24 cases across six categories, run through the REAL production loop
(production system prompt, tool schemas, iteration caps, observation
budgets) pointed at **any OpenAI-compatible endpoint** — the seam the
service-level runner cannot reach, because the sandbox pins its model.
Tools dispatch over HTTP loopback to the live backend (`:8090`).

| Category | Cases | What a failure means |
|---|---|---|
| `routing` | 4 | the deterministic intent gate drifted (no model involved) |
| `tool_call` | 8 | wrong tool, wrong arguments, or geocoding skipped |
| `evidence` | 6 | uncited claims, invented numbers, or fake confidence where data is absent |
| `multi_step` | 2 | cannot chain two tools into one comparison |
| `format` | 2 | ignores explicit output-shape instructions |
| `robustness` | 2 | out-of-coverage and ambiguous inputs handled dishonestly |

The evidence category is the discriminator that mattered in the 2026-08
Nano/Lightning A/B (`MODEL_CANDIDATES.md`): given a question whose data we
do not have, the desired behavior is a refusal that names the gap, not a
plausible invention.

```bash
# Baseline (production model on :8000):
python3 scripts/vllm/business_eval.py \
  --endpoint current=http://127.0.0.1:8000 \
  --output /mnt/data/urban-dossier-state/evals/business_eval_$(date +%Y%m%d).json

# Candidate comparison:
python3 scripts/vllm/business_eval.py \
  --endpoint current=http://127.0.0.1:8000 \
  --endpoint lightning=http://127.0.0.1:8002

# Gate check without a model (CI-safe):
python3 scripts/vllm/business_eval.py --routing-only

# Decision-grade run: 3 attempts per case, full trajectories kept:
python3 scripts/vllm/business_eval.py \
  --endpoint lightning=http://127.0.0.1:8002 \
  --repeat 3 \
  --responses /mnt/data/urban-dossier-state/evals/run.jsonl \
  --output /mnt/data/urban-dossier-state/evals/run.json

# Re-grade that run after changing a grader -- no GPU, no model drift:
python3 scripts/vllm/business_eval.py \
  --regrade /mnt/data/urban-dossier-state/evals/run.jsonl

# Same candidate at the sampling ITS OWN model card asks for:
URBAN_DOSSIER_AGENT_TEMPERATURE=1.0 \
URBAN_DOSSIER_AGENT_WRAPUP_TEMPERATURE=0.7 \
python3 scripts/vllm/business_eval.py --endpoint qwen38=http://127.0.0.1:8004
```

**Run every candidate twice: once at our production temperature (0.2, the
default) and once at whatever its card recommends.** The first answers "can
it drop into the service as-is", the second answers "is our sampling
hiding what it can do" — and the two can disagree sharply. Qwen3.8-27B
failed `format-three-sentences` at 0.2 and cleared the entire set at its
recommended 1.0 (2026-08-14). A candidate judged only at 0.2 is judged
under a Nemotron-era assumption.

Statuses: `pass` / `warn` (only the soft numeric-faithfulness check
missed) / `fail` / `skip` (case needs an availability-gated tool that is
not released — `find_similar_neighborhoods` and `retrieve_dataset_docs`
today) / `error` (harness or endpoint failure).

## What every run records

Each case keeps, alongside its status and metrics:

- `trace` — the **action** record. One entry per tool call with the
  arguments in full, the latency, and the result's shape plus a head.
  Arguments are never truncated: "what radius did it actually query" is the
  first question a surprising result raises.
- `turns` — the **deliberation** record. One entry per model call with the
  reasoning text (clipped, with the original length kept), what it said, the
  finish reason, and which tools it decided to call.

Full-fidelity responses go to `--responses` as JSONL; `--regrade` replays
that file through the current graders without calling a model. Keep the
JSONL for any run that decides something — a grader bug found next month can
then be re-run over the decision instead of re-run against models that have
since changed underneath it.

This is not bookkeeping. The first run after `trace` landed overturned a
conclusion already written into `MODEL_CANDIDATES.md`: a case recorded as
"Lightning fails to select `compare_neighborhoods`" turned out to be
`search_address` returning nothing for `"Union Square Manhattan"`, with the
model reasoning correctly and retrying the geocode until it ran out of
iterations. Tool names alone could not tell those two apart.

## pass^k

`--repeat K` runs each case K times per endpoint. `pass_hat_k` is 1.0 only
if **all** K attempts passed — tau-bench's sense, not an average, because
averaging is what let one model's high-frequency failure read as "the
benchmark is a bit flaky" for a day. `status` stays the first attempt's, so
repeated runs remain comparable with the single runs already in the history,
and `attempt_statuses` plus `failures_any_attempt` carry the rest.

pass^k does **not** affect the exit code unless `--require-pass-k` is
passed. A gate that cannot go green while a known defect is open stops being
read, and an unreliable case and a broken one deserve different responses.

Exit code 0 means the run is fit to decide something: every endpoint
answered, every routing case held, at least one case ran per endpoint, and
nothing failed or errored (`warn` and `skip` do not fail the run — one is
the soft check's tolerance zone, the other is an honestly-reported gap).
Anything else exits 1 and prints `FAIL <reason>` lines on stderr. Same
contract in `scripts/vllm/ab_bench.py`, where a run also fails if any
request errored or any prompt did not complete — a partial set skews every
per-second number in the report. Both write their report either way:
partial numbers are worth reading, just not worth deciding on.

Grading rules of note:

- **Numeric faithfulness (soft):** every ≥2-digit number in the final
  answer must appear in the prompt, a tool result, or an evidence entry.
  Soft because legitimately derived figures (differences, percentages)
  fail string matching; wholesale failure still means invented numbers.
- **Typographic normalization:** answers are canonicalized (curly quotes,
  unicode hyphens) before regex matching — a perfect refusal once failed
  grading on a U+2019 apostrophe.
- **Citations:** `citation_required` accepts either the inline
  `[source via tool]` format the system prompt mandates or a non-empty
  structured `evidence` list.
- **Fixtures:** 40.7282,-73.9942 (East Village; building flag `elevated`
  at methodology 3.10.0) and 40.8618,-73.8904 (Fordham; `watch`). Grading
  is structural (counts must match the trace), not pinned to today's
  values, so data refreshes do not rot the cases.
- **Run-to-run variance is signal:** at temperature 0.2 Nano failed
  different cases on consecutive baseline runs (compare-tool selection,
  then nothing). Compare models on the same number of runs.

## Maintenance

- Changing a case, a threshold, or a grader is an eval-set version event:
  bump `schema_version` in the corpus you changed and note it here.
  Comparisons are only valid within one version.
- Offline tests: `backend/tests/test_agent_business_eval.py` pins the
  service-level corpus and graders;
  `skills/urban_dossier_analyst/tests/test_business_eval.py` pins the
  model-level corpus schema and unit-tests every grader against synthetic
  responses. No model, no backend, no network.
- Model-level reports land in `/mnt/data/urban-dossier-state/evals/`
  (state, not git); headline numbers worth keeping go into
  `MODEL_CANDIDATES.md`.
