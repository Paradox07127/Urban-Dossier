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

30 cases across eight categories (schema 1.2), run through the REAL
production loop (production system prompt, tool schemas, iteration caps,
observation budgets) pointed at **any OpenAI-compatible endpoint** — the
seam the service-level runner cannot reach, because the sandbox pins its
model. Tools dispatch over HTTP loopback to the live backend (`:8090`).

| Category | Cases | What a failure means |
|---|---|---|
| `routing` | 4 | the deterministic intent gate drifted (no model involved) |
| `tool_call` | 7 | wrong tool, wrong arguments, or geocoding skipped |
| `evidence` | 7 | uncited claims, invented numbers, places named from memory, or fake confidence where data is absent |
| `multi_step` | 2 | cannot chain two tools into one comparison |
| `multi_turn` | 3 | loses the referent, the correction, or the format across turns |
| `fault` | 3 | invents numbers when a tool breaks, or gives up when a retry would have worked |
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

# Decision-grade run: 3 attempts per case. Trajectories are kept
# automatically, next to --output as <output>.responses.jsonl:
python3 scripts/vllm/business_eval.py \
  --endpoint lightning=http://127.0.0.1:8002 \
  --repeat 3 \
  --output /mnt/data/urban-dossier-state/evals/run.json

# Re-grade that run after changing a grader -- no GPU, no model drift:
python3 scripts/vllm/business_eval.py \
  --regrade /mnt/data/urban-dossier-state/evals/run.responses.jsonl

# Same candidate at the sampling ITS OWN model card asks for:
python3 scripts/vllm/business_eval.py \
  --endpoint qwen38=http://127.0.0.1:8004 \
  --sampling qwen38=qwen3.8-card

# Head-to-head, each model at its own card's numbers, in one report:
python3 scripts/vllm/business_eval.py \
  --endpoint lightning=http://127.0.0.1:8002 --sampling lightning=nemotron-card \
  --endpoint qwen38=http://127.0.0.1:8004  --sampling qwen38=qwen3.8-card \
  --repeat 3 --output /mnt/data/urban-dossier-state/evals/headtohead.json
```

**Run every candidate twice: once at our production temperature (0.2, the
default) and once at whatever its card recommends.** The first answers "can
it drop into the service as-is", the second answers "is our sampling
hiding what it can do" — and the two can disagree sharply. Qwen3.8-27B
failed `format-three-sentences` at 0.2 and cleared the entire set at its
recommended 1.0 (2026-08-14). A candidate judged only at 0.2 is judged
under a Nemotron-era assumption.

`--sampling NAME=PROFILE` takes a built-in profile name (`production`,
`qwen3.8-card`, `nemotron-card`), inline JSON, or `@file.json`, and `NAME=*`
applies one profile to every endpoint. The resolved profile is written into
the report under each endpoint — a comparison that does not record its
sampling cannot be reproduced or defended, and for one day the only record
of which knobs produced which numbers was a `_cardtemp` suffix on a
filename.

A profile's nested `wrapup` key covers the two wrap-up calls, which run with
thinking disabled; cards that distinguish thinking from instruct mode give
them different numbers (Qwen3.8: 1.0/0.95/top_k 20 thinking, 0.7/0.80 with
presence_penalty 1.5 instruct). vLLM-only knobs like `top_k` are routed
through `extra_body` automatically.

Statuses: `pass` / `warn` (only a soft check missed — numeric or place
faithfulness) / `fail` / `skip` (case needs an availability-gated tool that
is not released — `find_similar_neighborhoods` today) / `error` (harness
or endpoint failure).

Each endpoint summary states its own denominator and cost: `cases_executed`
and `skipped_ids` alongside `pass_rate`, and `wall_total_s` with
`output_tok_per_s`. On a single-tenant GPU wall-clock *is* the cost of the
run, and without it a dense candidate and a sparse one get compared on
quality alone — while the 8.5× that actually decides the question sits in a
separate benchmark nobody reads next to the pass rate.

## What every run records

Each case keeps, alongside its status and metrics:

- `trace` — the **action** record. One entry per tool call with the
  arguments in full, the latency, and the result's shape plus a head.
  Arguments are never truncated: "what radius did it actually query" is the
  first question a surprising result raises.
- `turns` — the **deliberation** record. One entry per model call with the
  reasoning text (clipped, with the original length kept), what it said, the
  finish reason, and which tools it decided to call.

Full-fidelity responses go to JSONL — `<output>.responses.jsonl` by default,
`--responses PATH` to place it, `--no-responses` to opt out — and `--regrade`
replays that file through the current graders without calling a model. A
grader bug found next month can then be re-run over the decision instead of
re-run against models that have since changed underneath it.

Persistence is the default because opting in did not work: the flag existed
throughout the 2026-08-14 Qwen3.8/Lightning comparison and was never passed,
so every artifact from that day holds a verdict with no way to see what the
model did to earn it.

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
answered, every routing case held, at least one non-skipped model case ran
per endpoint, and nothing failed or errored (`warn` and `skip` do not fail the run — one is
the soft check's tolerance zone, the other is an honestly-reported gap).
Anything else exits 1 and prints `FAIL <reason>` lines on stderr. Same
contract in `scripts/vllm/ab_bench.py`, where a run also fails if any
request errored or any prompt did not complete — a partial set skews every
per-second number in the report. Both write their report either way:
partial numbers are worth reading, just not worth deciding on.

## Known open defects the set reproduces

Cases that fail for a reason in the product, not in the model. Keep them
failing rather than relaxing them: that is the point of having them.

- **`fault-score-tool-flaky` — the agent usually does not retry a tool that
  errors once.** The case injects one error on the first `score_neighborhood`
  call and expects a second attempt (`min_tool_calls: 2`). The agent
  typically makes one call and gives up, and the answer that follows has no
  citation either. Failure is **model- and config-independent**: 3/3 on both
  endpoints of the FP8/BF16 A/B and 2/3 on both endpoints of the
  cutlass/marlin A/B, 2026-08-20 — four different serving configurations,
  10 of 12 attempts failed.

  The first write-up of this entry called it deterministic on the strength of
  the FP8 run alone; the second run passed once per endpoint, so it is a
  high-frequency failure rather than a certain one. A single transient tool
  error is an ordinary production event, so this is a real robustness gap
  either way — but "usually" is what the evidence supports.

This is why `--require-pass-k` is off by default.

## LLM judge (semantic assertions)

The deterministic graders check what a regex can see, and that is not the
same as the answer being right: in the 2026-08-15 3-way run, an
otherwise-correct refusal that called East Village coordinates "Upper West
Side" passed every check. Cases opt into a semantic check with:

```json
"expect": {
  "judge": {"criteria": "<what must hold, in plain language>",
            "mode": "soft"}
}
```

and a run supplies the judge endpoint:

```bash
# live: judge each attempt as it is graded
python3 scripts/vllm/business_eval.py --endpoint a=... --judge http://127.0.0.1:8000

# cheapest use: re-judge a finished run's stored answers -- one LLM call
# per judged case, no agent runs
python3 scripts/vllm/business_eval.py --regrade run.responses.jsonl \
    --judge http://127.0.0.1:8000 --output rejudged.json
```

Rules, all deliberate:

- **`grade_case` stays pure.** The judge is a separate layer; a run without
  `--judge` grades exactly as before, and the graders stay unit-testable
  without a model.
- **Escalation only tightens.** `mode: soft` (default) downgrades a `pass`
  to `warn`; `mode: hard` fails the case and records `judge: <reason>` as a
  failure. A judge *pass* never upgrades anything, and a judge *error*
  (endpoint down, malformed verdict) never changes status — an unavailable
  judge cannot turn a run green or red. The verdict or error is recorded
  under `judge` in the case record either way.
- **Verdicts persist.** Live-run verdicts are written into the responses
  JSONL, so a plain `--regrade` (no `--judge`) carries them at zero model
  cost.
- **The judge model is recorded** in the report header (`judge: {url,
  model}`). Judging a model with itself is acceptable for
  criterion-checking (the criterion names the ground truth), but say so
  when reading results.
- Single-turn cases only for now; a judge block on a multi-turn case is
  ignored.

Pilot cases: `robust-out-of-coverage` (must not attribute the LA
coordinates to a named place) and `robust-ambiguous-place` (must not
resolve "Main Street" without a tool actually resolving it).

## Multi-turn and fault injection (schema 1.1)

A case carries either `prompt` or `turns`, never both. `turns` is a list of
`{prompt, expect}`; the runner threads the conversation through `history`, so
a follow-up like *"and how does that compare with Union Square?"* is only a
test if the agent has to resolve "that" from its own previous answer. Each
turn is graded against its own `expect`, and the case status is the **worst**
turn — an agent that answers turn 1 and loses the thread on turn 2 has failed
the conversation, and a mean would hide exactly what the case exists to find.

`fault_injection: {tool, mode, on_call}` forces a named tool to misbehave for
the duration of the case: `mode` is `error`, `empty`, or `timeout`; `on_call`
is an integer to break only that call, or `"all"` to break every one. The two
settings test different things and both cases are in the set:

- `"all"` tests **honesty** — the number is unreachable, so the only correct
  answer says so. `fault-score-tool-down` forbids a score in the answer.
- an integer tests **recovery** — one failure then success, where giving up is
  as wrong as inventing. `fault-score-tool-flaky` requires ≥2 tool calls.

Before this, error honesty rested on a single case that happened to hit a
tool that happened to be gated off — which tests the release gate, not the
model.

Grading rules of note:

- **Numeric faithfulness (soft):** every ≥2-digit number in the final answer
  must be traceable to the prompt or a tool result. A claim is accepted as
  `literal`, `rounded` (within half a unit of a supported value), or
  `derived` (one arithmetic step — difference, sum, or share-of — from a
  supported pair); anything else is `unsupported` and warns. All four
  buckets are recorded, so a later tightening can be argued from evidence.
  Products are deliberately *not* a derivation rule: over a large pool they
  would span enough of the number line to explain away real inventions.
  Before derivations were recognised, both models warned on
  `multi-two-point-violations` in all three 2026-08-14 runs for doing the
  arithmetic the question asked for.
- **Place faithfulness (soft, `place_faithfulness`):** a neighborhood or
  borough name in the answer that appears in no tool result is flagged
  against the frozen NTA-2020 vocabulary (`nyc_neighborhoods.json`, rebuilt
  by `scripts/vllm/build_neighborhood_vocab.py`). Set it to `"hard"` to fail
  instead. This catches the answer that passes every structural check and
  still puts the user four miles from where they are — a model given East
  Village coordinates produced a correct, well-cited refusal that called the
  location "Upper West Side". No regex over answer text can see that,
  because the sentence is only wrong relative to the trace.
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

## What the loop does that the scores depend on

Two loop behaviours change what a failure means, so they belong here rather
than only in `agent_loop.py`:

- **Repeat guard** — three identical `(tool, args)` calls in a row abort the
  run. It only sees identical arguments.
- **No-progress guard** — three consecutive iterations calling only the
  lookup tool (`search_address`) with no analysis call between them injects
  a directive naming the analysis tools; ignoring it
  twice more forces an honest wrap-up instead of burning the rest of the
  budget. This is the failure the repeat guard cannot see: on 2026-08-14
  Qwen3.8 geocoded the same place five times with a different spelling each
  time, so every hash differed and the guard stayed silent while the whole
  iteration budget went on lookups.

Cases whose grading depends on a tool actually running should therefore be
read together with `metrics.tools_called` and the turn `kind` — a
`wrapup_no_progress` turn means the agent was stopped, not that it chose to
answer.

## Retired: the RAG cases (2026-08-20)

RAG was retired, so the six cases that asserted `retrieve_dataset_docs` went
with it — one model-level (`tool-dataset-docs-rag`) and five service-level
(`dataset_column_semantics`, `dataset_discovery_noise`,
`thin_coverage_explanation`, `methodology_version`,
`uncertainty_not_confidence`).

Two of them were genuinely about dataset retrieval and are gone for good. The
other three — coverage, methodology version, sensitivity-vs-confidence — were
never really retrieval cases; they ask the agent to explain its own
methodology, and were wired to `retrieve_dataset_docs` only because the RAG
corpus was going to carry the methodology docs. That intent is worth having
back as no-tool cases answered from the prompt and the payload. They were not
rewritten here because inventing expectations nobody has measured is the
failure mode this corpus exists to prevent: a new case has to be written
against observed behaviour, not against hope.

## Maintenance

- Changing a case, a threshold, or a grader is an eval-set version event:
  bump `schema_version` in the corpus you changed and note it here.
  Comparisons are only valid within one version. Current: **1.2** (drops
  the RAG-gated case when RAG was retired on 2026-08-20; 1.1 added `turns`,
  `fault_injection`, `place_faithfulness`); harness **2.0**, which the report
  records as `harness_version`.

  1.2 changes the denominator: 31 cases became 30, so a 1.2 `pass_rate` is
  not comparable with a 1.1 one even though no surviving case was touched.
  The service-level corpus moved 1.0 → 1.1 in the same change, 24 cases to
  19.
- Numbers from schema 1.0 runs are **not** comparable with 1.1: the
  numeric-faithfulness grader was loosened to recognise derived figures, and
  every stored 1.0 artifact predates both the trajectory capture and the
  `search_address` per-token fix.
- Offline tests: `backend/tests/test_agent_business_eval.py` pins the
  service-level corpus and graders;
  `skills/urban_dossier_analyst/tests/test_business_eval.py` pins the
  model-level corpus schema and unit-tests every grader against synthetic
  responses. No model, no backend, no network.
- Model-level reports land in `/mnt/data/urban-dossier-state/evals/`
  (state, not git); headline numbers worth keeping go into
  `MODEL_CANDIDATES.md`.
