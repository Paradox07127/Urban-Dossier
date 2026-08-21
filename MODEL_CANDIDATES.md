# Model Candidates — Nemotron 3.5 Lightning & Nemotron 3 Super

Decision record for the 2026-08-12 evaluation of two candidate models against
the production LLM, run side by side on the x86 workstation
(RTX PRO 6000 Blackwell, 96 GiB). The production image has been upgraded to
vLLM 0.27.1 and revalidated with Nano, but the production model remains Nano
until the promotion checklist at the bottom passes.

| Role | Model | Port | Weights |
|---|---|---|---|
| **Production (incumbent)** | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4` | 8000 | 19.3 GiB, `models/llm/` |
| **Candidate (replacement)** | `nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4` (+ DSpark drafter) | 8002 | 21.6 + 1.3 GiB, `models/llm-candidates/` |
| **Candidate (larger, reference)** | `nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4` | 8003 | 80.3 GiB, `models/llm-candidates/` |

## References

- Nemotron 3 Nano technical report — [arXiv:2512.20848](https://arxiv.org/abs/2512.20848) (the incumbent's report)
- Nemotron 3 white paper — [arXiv:2512.20856](https://arxiv.org/abs/2512.20856)
- Nemotron 3 Super technical report — [research.nvidia.com PDF](https://research.nvidia.com/labs/nemotron/files/NVIDIA-Nemotron-3-Super-Technical-Report.pdf)
- Nemotron 3.5 Lightning (released 2026-08-11) has **no standalone technical
  report yet** as of 2026-08-12; the model card is the primary source. Its
  speculative-decoding drafters have papers: DFlash
  [arXiv:2602.06036](https://arxiv.org/abs/2602.06036), DSpark
  [arXiv:2607.05147](https://arxiv.org/abs/2607.05147).
- Deployment recipes: each model card's Quick Start (Lightning validates
  `vllm/vllm-openai:v0.27.1`; Super documents v0.20.0 — we run both candidates
  on v0.27.1).

## Accuracy: model-card numbers, NVFP4 checkpoints

All three columns are NVIDIA's own NeMo Evaluator/Gym measurements from the
respective model cards. Harness versions differ between the Nano (Jan 2026)
and Lightning/Super cards, so treat single-digit deltas as indicative, not
exact — that is what the local A/B is for.

| Benchmark | Nano 3 30B (current) | Lightning 3.5 30B | Super 120B |
|---|---:|---:|---:|
| MMLU-Pro | 77.4 | 81.6 | 83.3 |
| GPQA (no tools) | 71.9 | 75.6 | 79.4 |
| SciCode | 30.7 | 31.4 | 40.8 |
| HLE (no tools) | 9.4 | 10.5 | 17.4 |
| IFBench¹ | 70.7 | 72.9 | 73.3 |
| AA-LCR (long context) | 33.3 | 49.2 | 58.1 |
| SWE-bench Verified | — | 52.8 | — |
| TauBench V2 avg² | 45.6 | — | 60.5 |

¹ Variants differ: Nano/Super report IFBench (prompt), Lightning reports
IFBench (loose). ² Lightning's card reports τ³-bench instead; not comparable.

Reading: **Lightning 3.5 beats the incumbent on every overlapping benchmark**
at the same 30B-total/3B-active footprint, with the long-context gap (AA-LCR
+15.9) most relevant to our multi-document dossier analyses. Super adds
another tier on reasoning-heavy tasks (SciCode +9.4, HLE +7.0 over Lightning)
at 4× the weight footprint.

## What changes operationally with Lightning 3.5

- **Serving stack:** requires vLLM ≥ 0.27.1 (production now runs 0.27.1).
  New
  flags: `--mamba-backend flashinfer`, `--mamba-cache-mode align`, built-in
  `nemotron_v3` reasoning parser (the `nano_v3_reasoning_parser.py` plugin is
  obsolete for this model). Tool calling stays `qwen3_coder`.
- **Speculative decoding:** ships DSpark (external 1.3 GiB drafter,
  recommended for low-concurrency/single-GPU), DFlash, and MTP. Our compose
  service uses DSpark with `num_speculative_tokens 3`.
- **No `--trust-remote-code`:** the 3.5 architecture is native in the
  container's transformers.
- **License changes** from the NVIDIA Nemotron Open Model License to
  [OpenMDW-1.1](https://openmdw.ai/license/1-1/). Review before promotion.
- **Sampling:** temperature 1.0 / top_p 0.95 recommended (same as current).
- No published recipe exists for RTX PRO 6000 Blackwell; our config adapts
  the DGX Spark recipe (marlin W4A16 MoE). Trying the native FP4 tensor-core
  path (`LIGHTNING_MOE_BACKEND=default`) is a follow-up experiment.

## Super 120B on this workstation is off-label

The card's stated minimum is 1× B200 (192 GiB) or DGX Spark (128 GiB
unified). 80.3 GiB of weights on our 96 GiB card leaves ~10 GiB for KV +
Mamba state at `--gpu-memory-utilization 0.92`. The on-card run completed
without OOM and measured 356,937 KV-cache tokens, so the compose defaults are
now 64K context and 4 sequences. It still cannot run concurrently with any
other GPU service. Treat it as a quality reference point / offline
second-opinion model, not a production candidate on this hardware.

## Running the comparison

```bash
# Production + Lightning side by side (0.45 + 0.38 GPU fractions):
docker compose --env-file /mnt/data/urban-dossier-state/runtime/gpu.env \
  -f deploy/compose.gpu.yml --profile inference --profile candidate up -d llm llm-lightning

python3 scripts/vllm/ab_bench.py \
  --endpoint current=http://127.0.0.1:8000 \
  --endpoint lightning=http://127.0.0.1:8002 \
  --output /tmp/ab_bench.json

# Super alone (stop everything else on the GPU first):
docker compose ... stop llm llm-lightning
docker compose ... --profile candidate-super up -d llm-super
python3 scripts/vllm/ab_bench.py \
  --endpoint super=http://127.0.0.1:8003 --output /tmp/super_bench.json
```

## Promotion checklist (Lightning → production)

1. ~~`ab_bench.py` shows TTFT/throughput ≥ incumbent at C1 and C4, zero
   errors.~~ Done 2026-08-12: +60%/+63%, zero errors.
2. ~~Quality review of captured completions.~~ Done 2026-08-12: clean
   separation, correct tool calls, stricter evidence discipline.
3. Real agent smoke test through the NemoClaw/OpenClaw gateway
   (`scripts/test_openclaw_gateway.py`) with the sandbox endpoint repointed
   to Lightning. The sandbox's inference route pins the model name
   (currently Nano) and was chosen in the interactive `nemoclaw onboard`
   wizard — repointing needs either a re-onboard or serving Lightning under
   the same served-name (discouraged: misleading).

   **2026-08-20: the harness this gate runs on is healthy again, but the
   gate itself is still open.** The gateway's mTLS material had drifted
   since 08-15 and the sandbox had been crash-looping on a policy generation
   its stale supervisor rejected; the NemoClaw upgrade to v0.0.111 refreshed
   the certs and `rebuild --force` brought the sandbox to OpenShell 0.0.101 /
   OpenClaw v2026.7.1. `test_openclaw_gateway.py` now returns
   `gateway-route-ok` — **against Nano**. Repointing to Lightning is what
   remains. See "Rebuilding the sandbox" in `DEPLOY_WORKSTATION.md`.
4. License review of OpenMDW-1.1 for our use (user decision).
5. ~~Pin the v0.27.1 image digest in `deploy/compose.gpu.yml`.~~ Done
   2026-08-12, Nano regression passed on it. Remaining: retarget the
   `models/llm` mount (or path) to the Lightning weights, update
   `LLM_SERVED_NAME`, record rollback here.

## Status

- 2026-08-12: Nano and Lightning weights are present; both services started
  successfully and completed the A/B below. Super also completed its isolated
  on-card run without OOM.
- The production vLLM image is upgraded and digest-pinned, but the served model
  and NemoClaw/OpenShell route still point to Nano. Lightning promotion is
  therefore **not complete**.
- The Super service is optional and stopped in the
  2026-08-12 review snapshot; Nano `:8000` and Lightning `:8002` were online.
- Validated image: `vllm/vllm-openai:v0.27.1` =
  `sha256:0a51ea5b4ae2dc5d81890e5173f54203d2a3ae0cfffe51b8fd2afd4391bfd967`
  (pull digest to pin at promotion time).

## 2026-08-12 local A/B results (Nano :8000 @ 0.45 vs Lightning :8002 @ 0.30)

`ab_bench.py`, 8 prompts, max_tokens 4096, prefix cache warm, both models
co-resident (steady VRAM ≈ 70 GiB total):

| Metric | Nano 3 (current) | Lightning 3.5 | Delta |
|---|---:|---:|---:|
| C1 output throughput | 309 tok/s | 493 tok/s | **+60%** |
| C4 output throughput | 748 tok/s | 1222 tok/s | **+63%** |
| C1 TTFT p95 (warm) | 0.060 s | 0.061 s | — |
| C4 TTFT p95 (warm) | 0.083 s | 0.088 s | — |
| Cold TTFT p50 (first run) | 1.5 s | 1.4 s | — |
| Errors | 0 | 0 | — |

The throughput gap is DSpark speculative decoding doing its job at low
concurrency — and it holds even at C4.

Qualitative, from the captured completions:

- Reasoning/content separation clean on both (vLLM 0.27 renames the response
  field `reasoning_content` → `reasoning`; `agent_loop.py` already handles
  both, `ab_bench.py` now does too).
- Tool calling: both emit identical, correct `score_neighborhood` calls
  through the `qwen3_coder` parser.
- **Stricter evidence discipline:** given the production scoring rubric and a
  question with no evidence table attached, Lightning refuses and says the
  evidence is missing, where Nano invents a plausible answer. For an
  evidence-cited dossier product this is the desired behavior, but prompts
  that *expect* the model to improvise must attach their evidence.
- Lightning thinks longer by default (mean ~1062 vs ~785 completion tokens).
  With `max_tokens` ≤ 1024 it can exhaust the budget inside the think block
  and return empty `content` — the agent path must keep its existing
  truncation fallback and a ≥4K completion budget. The two 1024-token
  wrap-up calls in `agent_loop.py` now set `enable_thinking: False`
  (they only ever want a direct answer); `agent_service.py`'s small-budget
  calls already did.

## 2026-08-12 Super-120B on-card results (:8003, everything else stopped)

It fits. Weights + non-torch 79.5 GiB, KV cache 5.68 GiB = 356,937 tokens
(10.9× concurrency at 32K), engine init 127 s, **no OOM**, zero errors.

| Metric | Super 120B | Lightning 3.5 (same harness) |
|---|---:|---:|
| C1 output throughput | 124 tok/s | 493 tok/s |
| C4 output throughput | 200 tok/s | 1222 tok/s |
| C1 TTFT p95 | 1.0 s | 0.061 s |
| C4 TTFT p50 | 6.5 s¹ | 0.071 s |

¹ Measured at `max_num_seqs 2`, so C4 queued; defaults now raised to 4.

Qualitatively: same clean reasoning separation, same strict
evidence-discipline refusals as Lightning, and notably *terser* thinking
(mean ~348 completion tokens at C1). Usable as a single-stream
second-opinion / offline batch model; not a serving replacement — Lightning
gives 4–6× the throughput on this card.

## 2026-08-12 framework updates applied

- Production `llm` service image bumped to
  `vllm/vllm-openai@sha256:0a51ea5b…` (v0.27.1); previous 0.23.0 digest kept
  in a comment as the rollback pair (rollback = old digest + Nano, both
  validated together). `--moe-backend flashinfer_cutlass` confirmed still a
  valid choice in 0.27.1 via `vllm serve --help`. **Nano-on-0.27.1 serve
  regression passed later the same day**: C1 308.5 tok/s, zero errors
  (0.23 baseline: 309), and the full agent stack (frontend 3456 → backend
  8090 → NemoClaw gateway → OpenClaw → vLLM :8000) came back green on it —
  `test_openclaw_gateway.py` returned `gateway-route-ok` and the 5
  `test_agent_service_nemoclaw.py` tests passed.
- vLLM 0.27.1 adds `--moe-backend flashinfer_b12x` — "FlashInfer CuteDSL
  fused MoE for SM12x (RTX Pro 6000 / DGX Spark)". The 0.23 comment about
  b12x rejecting the Nemotron-H layout may be obsolete; benchmarking it vs
  `marlin` for Lightning is the obvious next perf experiment.
- Context defaults retuned from measured KV capacity: Lightning 32K → 128K
  (1.34M KV tokens at 0.30 leaves ~10× at full length; long context is its
  headline gain), Super 32K/2 seqs → 64K/4 seqs (357K KV tokens measured).

## 2026-08-13 business eval (EXPANSION_PLAN 4.1) — first same-code side-by-side

The fixed business evaluation now exists (`evals/agent/model_cases.json` +
`scripts/vllm/business_eval.py`: the REAL production agent loop pointed at
each endpoint, tools dispatching against the live backend). Numbers below are
the post-fix run — after the eval's own day-one catches were fixed
(/api/search resurrection, strict tool args, range filters), so both models
ran the same healed tool layer. Report:
`/mnt/data/urban-dossier-state/evals/business_eval_20260813_final_nano_vs_lightning.json`.

| Metric (22 executed cases, including 4 deterministic routing cases, + 2 gated skips) | Nano 3 (current) | Lightning 3.5 |
|---|---:|---:|
| Hard-check result | 21 pass / **1 fail** | 20 pass / 2 warn / **0 fail** |
| Soft warns (derived-number faithfulness) | 0 | 2 |
| Case wall p50 | 14.9 s | **9.5 s** |
| Case wall max | 49.6 s | **25.1 s** |
| Completion tokens, whole set | 83.5k | **41.1k** |

- Nano's fail is `tool-compare-two-places` — it looped `search_address` and
  never reached `compare_neighborhoods`. Across four runs of that case this
  session (two pre-fix, two post-fix) Nano failed it twice and Lightning
  once: comparison-tool selection is flaky on BOTH models at temperature
  0.2. Single runs must not decide a promotion; run the set N times.
- Lightning emitted two out-of-contract tool calls (`final_answer`, gated
  `retrieve_dataset_docs`); the loop's not-released refusal bounced both and
  it recovered on its own — the guard behaves as designed under a model that
  probes it.
- Lightning's two warns are derived figures (differences/averages computed
  from cited counts), the soft check's designed tolerance zone, not
  invention.
- Both models refused the no-data cases (rent, schools, 2027 forecast)
  cleanly this run.

Business-behavior evidence now points the same way as the throughput A/B:
Lightning is faster, cheaper, and no less disciplined on this set.
Promotion still waits on the checklist above: gateway smoke with the sandbox
repointed (step 3) and the OpenMDW-1.1 license review (step 4, user
decision).

Operational note: the 128K context retune made the 0.30 GPU fraction
unbootable for the Lightning service (cache-blocks OOM, crash loop); compose
default is now 0.38 — 135 s to ready alongside production Nano, 76 GiB
steady total.

---

## 2026-08-14 — Qwen3.8-27B enters the comparison

The first non-Nemotron candidate, and the only dense one. Qwen3.8-27B is a
Gated DeltaNet hybrid — 16 × (3 × GDN → FFN, 1 × gated attention → FFN),
262K native context, MTP head, native vision — with **27B active on every
token** against Lightning's 30B-A3B MoE that activates 3B. Every number
below should be read against that asymmetry.

**Checkpoint.** `Inferact/Qwen3.8-27B-NVFP4`, the NVFP4 build vLLM's own
recipe page names for the single-GPU (TP1) profile. It quantizes every
Linear to W4A4 but excludes the `linear_attn` projections, `conv1d`,
`lm_head` and `embed_tokens` — the right call for a GDN hybrid, whose
recurrent state is precision-sensitive. 25.5 GiB, MTP head included.
Verified on download: all 7 shards' byte lengths match their safetensors
headers exactly, 2111 tensors, index agrees. (The repo's own `crc32.txt`
is stale — it describes a `layers-N.safetensors` packaging that the
published files do not use, and only 10 of its 76 entries name a real file.
Do not treat its mismatches as corruption.)

**Framework.** No vLLM change was needed. The pinned v0.27.1 image already
registers `Qwen3_5ForConditionalGeneration` *and* `Qwen3_5MTP` — verified by
querying the registry inside that exact image, not by reading release notes.
No `--trust-remote-code`. vLLM sets the Mamba cache to `align` mode by
itself when prefix caching is on, so the GDN state needs no explicit flag.
Parsers per the recipe: `--reasoning-parser qwen3`, `--tool-call-parser
qwen3_coder`, `--kv-cache-dtype fp8`. There is no Qwen3.8 arXiv technical
report — the model card and the vLLM recipe page are the primary sources.
MTP speculative decoding is left off pending a measurement of the base path.

**Throughput A/B** (`ab_bench`, 512 max tokens, zero errors both sides):

| | Lightning | Qwen3.8-27B |
|---|---|---|
| C1 | **505.6 tok/s** | 59.6 tok/s |
| C4 | **804.4 tok/s** | 204.1 tok/s |
| TTFT p50 (C1) | 0.066 s | 0.059 s |

8.5× single-stream. That is the dense-vs-3B-active gap plus Lightning's
DSpark drafter (measured 59.9% draft acceptance during these runs).

**Business eval** (`model_cases.json` v1.0, three same-code rounds at the
production temperature, plus one round at Qwen's own recommended sampling):

| | Lightning ×3 | Qwen3.8 @0.2 ×2 | Qwen3.8 @card temp |
|---|---|---|---|
| pass / warn / fail | 20 / 1 / 1 (all three rounds) | 17/4/1, 16/4/2 | **18 / 4 / 0** |
| pass_rate | 0.955 | 0.955, 0.909 | **1.0** |
| wall p50 | **5.6 – 7.3 s** | 25.7 – 26.6 s | 27.0 s |
| wall max | **14.1 – 17.0 s** | 52.1 – 84.4 s | 118.2 s |
| completion tokens | 31.2k – 39.9k | 27.2k – 31.6k | 35.6k |

Read this carefully, because the two models fail in different *kinds* of
ways:

- **Lightning fails `tool-compare-two-places` in all three rounds.** It
  substitutes two `score_neighborhood` calls for `compare_neighborhoods`
  every time. The 2026-08-13 note called comparison-tool selection "flaky on
  both models"; with three more rounds it is not flaky for Lightning, it is
  consistent. This is a reproducible capability gap on a first-class product
  feature.

  > **Corrected 2026-08-14, after trajectory persistence landed.** Both
  > sentences above are wrong, and the trace says so on its first use.
  > Lightning's reasoning on that case is right — *"I need to get
  > coordinates for both locations, then use compare_neighborhoods. First,
  > I need to geocode both places"* — and then
  > `search_address({"query": "Union Square Manhattan"})` returns an empty
  > result (79 chars), its next thought is *"the search_address returned
  > empty results, let me try a more specific query"*, and it burns the
  > iteration budget retrying the geocode. It never gets to choose the
  > comparison tool at all.
  >
  > The defect is in `search_address`, not the model:
  > `service.py:search_address_payload` matches `upper(address) LIKE
  > '%<entire query>%'` — one contiguous substring against the address
  > column, with the borough column never consulted. So `"Union Square"`
  > returns 3 hits and `"Union Square Manhattan"` returns 0; `"Astoria"`
  > works and `"Astoria Queens"` does not; `"350 5th Avenue"` finds nothing
  > because PLUTO stores `350 5 AVENUE`. Appending the borough — the most
  > natural thing a model or a user does — is what breaks it.
  >
  > A second run also produced one pass in five observations, so the case is
  > high-frequency-failing rather than deterministic. Both the
  > misattribution and the flakiness are exactly what pass^k and a stored
  > trajectory exist to prevent, and neither existed when the paragraph
  > above was written.
  >
  > **Settled 2026-08-14.** With `search_address` matching per token, on
  > word boundaries, against the borough column, the case passes **3 of 3
  > (pass^3 = 1.0)** and the trajectory is what the model always intended:
  > `search_address, search_address, compare_neighborhoods` in 6.2 s.
  > `tool-geocode-before-score` likewise goes 3 of 3 with two tool calls
  > where it previously burned five or six retrying the geocode. Lightning
  > never had a tool-selection defect on this set.
- **Qwen3.8 at its card sampling calls `compare_neighborhoods` correctly**
  and clears the whole set — the only configuration in this comparison with
  zero hard failures.
- **Qwen3.8 at our production 0.2 fails `format-three-sentences`** (7 and 6
  sentences against a limit of 4). Its verbosity is temperature-sensitive in
  the direction opposite to intuition: the *hotter*, card-recommended
  setting is the disciplined one. Qwen's card asks for 1.0 with thinking on
  and 0.7 with it off; 0.2 is well outside what it was tuned for.

**What the comparison exposed in our own code.** Round 1 scored Qwen at 2
fails; 3 of its 20 cases had actually died on `HTTP 400 "System message must
be at the beginning"`. `agent_loop` injected its reflection and final-answer
directives as `role="system"` mid-conversation — accepted by Nemotron
anywhere, rejected outright by Qwen's template. That is fixed (all
mid-conversation directives are `role="user"` now, with a test pinning it)
and rounds 2/3 show zero aborts. **Any candidate evaluated before
2026-08-14 was measured with this defect present**; it only ever penalised
models whose templates enforce the rule, so the Nemotron numbers stand, but
no cross-family comparison older than this date should be trusted.

**Verdict.** Qwen3.8-27B is not a serving replacement on this hardware: 8.5×
slower single-stream and a 27 s case p50 against Lightning's 6 s is not a
trade this product can make for an interactive map. It is, however, the only
candidate that has cleared the business set outright, and it is right about
`compare_neighborhoods` where Lightning is consistently wrong. Two things
follow that are worth more than the verdict:

1. ~~Lightning's `compare_neighborhoods` failure is now a known,
   reproducible defect to fix in the prompt or tool description~~ — see the
   correction above: the failure is `search_address` refusing any query with
   a borough appended, and no prompt change will fix it. Fixing the geocoder
   is the real item, and it is a product bug well beyond the eval.
2. `format-three-sentences` and the temperature finding say our hardcoded
   0.2 is a Nemotron-era assumption. The knob now exists
   (`URBAN_DOSSIER_AGENT_TEMPERATURE`); any future candidate should be run
   at both its own recommended sampling and ours before it is judged.

Qwen3.8 stays on the bench as a second opinion (profile `candidate-qwen`,
port 8004), alongside Super. It does not change Lightning's promotion path.

**Not measured, and worth measuring before anyone revisits this:** MTP
speculative decoding (the head is in the checkpoint and vLLM registers the
class — this is the single biggest lever on that 59.6 tok/s), and the vision
tower, which we load and never use.

---

## 2026-08-15 — rerun on eval 1.1 / harness 2.0: the quality gap closed

> **The 2026-08-14 section above is superseded on quality.** Every number in
> it was collected before three fixes that each moved results: the
> `search_address` per-token rewrite, a system prompt that had been telling
> both non-Nemotron models they were Nemotron, and two grader defects. The
> throughput findings there still stand — nothing about serving changed.

Same 31-case set, same loop, both endpoints co-resident, 3 attempts per case,
`business_eval_20260815_v2_3way.json` (+ `_regraded`) in
`/mnt/data/urban-dossier-state/evals/`. 25 cases executed; 2 skipped on both
(`find_similar_neighborhoods`, `retrieve_dataset_docs` still unreleased).

| | Lightning @ 0.2 | Qwen3.8 @ 0.2 | Qwen3.8 @ its card |
|---|---|---|---|
| pass / warn / fail | 23 / 1 / 1 | **24 / 0 / 1** | 23 / 2 / **0** |
| pass_rate | 0.96 | 0.96 | **1.00** |
| pass^3 | 0.96 | 0.96 | 0.96 |
| case wall p50 | **6.8 s** | 24.7 s | 21.4 s |
| case wall max | **19.3 s** | 61.2 s | 107.3 s |
| set wall total | **175.7 s** | 702.8 s | 742.6 s |

**Qwen3.8 is no longer behind on quality.** At our own production sampling it
now scores level with Lightning, and at its card's numbers it is the only
column with zero hard failures. The earlier reading — 16–17 of 22, "unstable,
no two runs alike" — was an artifact of the three defects above, not a
property of the model. Both are now at pass^3 0.96: exactly one case each
that did not pass all three attempts.

**The decision is now purely cost.** Lightning finishes the set in 176 s
against Qwen's 703 s — 4.0× on this workload, against 8.5× on pure
generation, because tool time is shared. A 24.7 s median case, with a 61 s
tail, is not a trade an interactive map can make. Lightning stays the
promotion path; Qwen3.8 stays on the bench (`candidate-qwen`, port 8004).

What each model still gets wrong, from the trajectories:

- **Lightning, `multi-two-point-violations` (1 of 3).** Spent all 8
  iterations on `query_dataset`, hit the cap, and the forced wrap-up returned
  empty — so the user got the "loop terminated without producing a final
  answer" fallback. Not a no-progress case (`query_dataset` is an analysis
  tool, so that guard correctly stayed out of it); it is iteration
  exhaustion plus a wrap-up that came back with nothing.
- **Lightning, `evidence-place-grounded` (soft, 1 of 3).** Asked to describe
  the area at East Village coordinates, it recommended "the East Village,
  West Village, or Upper West Side" for green space — three neighborhoods it
  never scored, offered inside an answer that otherwise cites
  `score_neighborhood`, to a user who is already in the first one. Every
  hard check passed. This is the failure `place_faithfulness` was added for,
  and it found it on the first live run.
- **Qwen3.8 @ 0.2, `robust-out-of-coverage` (1 of 3).** Still the one case it
  misses, and still a wording drift rather than a bad action: it called
  `score_neighborhood` three times on out-of-NYC coordinates instead of
  declining. At its card sampling the case passes 3/3.
- **Qwen3.8 @ card, `format-json-object` (1 of 3)** produced no parseable
  JSON, and `tool-literal-count` claimed one unsupported number. Higher
  temperature buys the refusals and costs a little format discipline.

Two grader defects were found by this run and fixed mid-flight; both were
caught **only** because the run kept its trajectories, and both were repaired
by `--regrade` at zero GPU cost:

1. **U+202F.** Lightning failed `multiturn-followup-referent` 3/3 on "answer
   missing required pattern: union square" while naming Union Square eight
   times off a correctly resolved referent. The separator between the two
   words was a NARROW NO-BREAK SPACE. `_TYPOGRAPHIC_MAP` normalised U+00A0
   and nothing else in the family. Regraded: 3/3 pass, and Lightning's
   pass^3 went 0.793 → 0.96.
2. **"green space" as a neighborhood claim.** The vocabulary builder split
   every hyphenated NTA name, so Green-Wood Cemetery contributed a bare
   "Green" and Co-op City contributed "op City".

And one fabricated constant, caught before it was used: the `nemotron-card`
sampling profile shipped with temperature 0.6 from memory. Lightning's
`generation_config.json` says 1.0. A test now cross-checks every `*-card`
profile against the checkpoint on disk.

**Still not measured:** MTP speculative decoding, unchanged as the biggest
available lever on Qwen's throughput and the only thing that could reopen
this comparison.

---

## 2026-08-15 — Lightning's serving config, settled against the vendor sources

Lightning stays the default. This section pins what we serve and why, and
lists what is left on the table. Sources at the end.

### The hardware moved, and the recipe did not follow

Urban Dossier was built on **DGX Spark** (GB10, ARM64, 128 GB unified) — the
original design, the Nemotron-3-Nano deployment and the early benchmarks all
come from there, and the DGX history stays in the docs. Development now runs
on an **RTX PRO 6000 Blackwell Workstation Edition, 96 GB, compute capability
12.0**.

That migration matters more than it looks. NVIDIA publishes vLLM recipes for
DGX Spark (GB10), H100, GB200, Ampere and 8×H100 — and **none for SM 12.0**.
We inherited the DGX Spark profile, which is the closest validated
single-GPU one, but two of its choices were made for a machine we are no
longer on: the MoE backend and the draft model.

### `--moe-backend`: is the SM 12.0 FP4 bug fixed yet? No — and it would not help

Asked directly, checked upstream and then measured on this box.

**Upstream: not fixed.** vLLM issue #31085 (native NVFP4 MoE kernels for
SM120) is **still open** — the kernels are compiled into the tree
(`nvfp4_scaled_mm_sm120_kernels.cu`, `nvfp4_blockwise_moe_kernel.cu`) but the
backend-selection logic does not treat SM120 as eligible, so it falls back to
Marlin. CUTLASS #3096 is marked closed with a fix, but that fix is **not
upstreamed**: it needs CUDA 13.0 with `compute_120f` (the `f` matters —
`compute_120a` on CUDA 12.8 falls back to slower tactics), roughly ten hand
patches to FlashInfer 0.6.5, and modified vLLM capability checks.

**And even working, it loses.** The person who got native FP4 running on
SM120 measured **39 tok/s against Marlin's 46–49** — about 20% slower, from
activation-quantisation overhead. There is no upside on the other side of
that work.

**Measured here, vLLM 0.27.1, 2026-08-15:**

| `--moe-backend` | Result |
|---|---|
| `auto` | selects `MARLIN` itself, with "Your GPU does not have native support for FP4 computation". Same backend we pin explicitly. |
| `marlin` | what we ship |
| `default` | **not a valid value** — argparse rejects it, container never starts |
| `flashinfer_b12x` | loads, then dies: `RuntimeError: shape '[344064, 116]' is invalid for input of size 41287680` |

So the previous comment in `compose.gpu.yml` — "try `default` if the SM 12.0
native-FP4 path proves stable" — pointed at a value that has never existed in
this vLLM. And the widely reported garbage output on SM 12.0 comes from
**forcing** `flashinfer_cutlass` / `vllm_cutlass`; `auto` never picks them.
The risk is in overriding the flag, not in leaving it alone.

> **Scope, added 2026-08-20.** The sentence above is about **Lightning**
> (Nemotron 3.5) and must not be read as a statement about SM 12.0 in
> general. Production Nano forces `flashinfer_cutlass` on this same card and
> is fine — measured, not assumed: an eval-1.1 A/B at 3 attempts, cutlass vs
> marlin, all flags otherwise byte-identical, put cutlass **ahead** on
> quality (pass_rate 0.931 vs 0.862, pass^3 0.793 vs 0.655) with marlin
> faster (274.6 vs 241.8 tok/s) and carrying the run's only deterministic
> split — `multiturn-followup-referent` 0/3 on marlin, 3/3 on cutlass — plus
> a fabricated "Midtown" that tripped the place check. Report
> `moe_backend_ab_20260820.json`. Different checkpoint, different answer:
> Nemotron-H's MLP layout is not Lightning's, and the compose comment on the
> production service already said CUTLASS is the supported path for it.
> Verify per checkpoint; do not generalise this either way.

`humming` (the card's H100/Ampere choice) is in the valid list and remains
untested here. Given that the failure modes on this silicon are "silent
garbage" and "shape error at load", it needs a correctness check first.

### DFlash: measured, and unusable (2026-08-20)

The section below argued DFlash *might* be the right drafter for this box —
its card names the RTX 5090, i.e. this silicon class, while DSpark targets
DGX Spark's GB10. We inherited DSpark from the DGX days and had never
measured the alternative. Now we have. **DSpark stays, and not on a
tiebreak.**

| Drafter | draft len | C1 tok/s | C4 tok/s | errors |
|---|---|---|---|---|
| DSpark | 3 (ours) | **518.1** | **820.2** | 0 |
| DSpark | 1 | 322.2 | 801.2 | 0 |
| **DFlash** | **3** | — | — | **engine dead** |
| DFlash | 1 | 178.2 | 450.0 | 0 |

`ab_bench --warmup`, same card, every other flag byte-identical, one
endpoint at a time. Reports `drafter_*_20260820.json`.

**At the vendor's own draft length 3, DFlash kills the engine**:
`torch.AcceleratorError: CUDA error: device-side assert triggered`, then
`EngineDeadError` and every subsequent request 500s. The scheduler dump names
the cause — `scheduled_spec_decode_tokens={...: [-1, -1, -1]}`, i.e. the
draft proposed token id −1 three times and something indexed with it. Not an
unsupported-architecture problem: vLLM 0.27.1 registers **both**
`Qwen3DSparkModel` and `DFlashDraftModel`, and the weights load cleanly.

Two things worth carrying forward:

- **A smoke test would have passed it.** One short non-streaming completion
  through DFlash at draft 3 returned a correct answer; the engine died on
  the benchmark's sustained generation. `--async-scheduling` is *not* the
  trigger — removing it produced the identical assert, which is what that
  single successful request briefly made it look like.
- **Draft length 1 is the only length that survives, and there DSpark is
  1.8× faster** at both C1 (322.2 vs 178.2) and C4 (801.2 vs 450.0). The
  matched-length control matters: comparing DFlash@1 against DSpark@3 gives
  2.9×, which would have charged the draft-length difference to the drafter.

So DFlash is not merely blocked by a bug — at the one setting where it runs
it is behind. Revisit only if a later vLLM fixes the assert, and re-measure
rather than assuming the card's acceptance numbers transfer.

### The three draft paths, and why ours may be the wrong one

Lightning ships three, and they are not interchangeable:

| | What it is | Built for | SPEED-Bench acceptance @ draft 7 |
|---|---|---|---|
| **MTP** | multi-token prediction baked into the checkpoint at pretraining; no second model to download or hold in memory | medium-to-high concurrency, optimal draft length falling as concurrency rises | — |
> **2026-08-21 retraction affecting the numbers below.** Every `pass^k`
> figure on this page is understated by roughly 13.8 points: the four
> deterministic routing cases counted in the denominator and could never
> count in the numerator. A/B *comparisons* on this page stand — both sides
> carried the same bias — but absolute `pass^k` values do not, and were not
> recomputed. Separately, `fault-score-tool-flaky`'s failures were caused by
> a contradictory hint in the fault injector, not by the model: after the
> fix it passes 3/3. Details in `evals/agent/README.md` § Retracted.

| **DSpark** | separate 967M dense drafter (615M non-embedding, GQA, sliding window 1024 on every layer) | compact Blackwell — **DGX Spark GB10** — and low-concurrency data centre | **3.75** |
| **DFlash** | the other separate drafter | data centre GPUs and high-end local systems; its card names **GeForce RTX 5090** — consumer/workstation Blackwell, this box's silicon class | 3.16 |

The two disagree about which is right for us. DSpark accepts more tokens per
verification step; DFlash is the one whose target list includes this
hardware, and DSpark's does not. Acceptance length is not throughput once the
drafter's own forward pass is paid for, and a drafter tuned for GB10's
unified memory is tuned for a fraction of this card's bandwidth. NVIDIA's own
guidance is to benchmark DFlash per workload rather than assume.

**We run DSpark because the project started on DGX Spark.** That was the
right choice on GB10 and is an untested inheritance here.

### Config as shipped

| Flag | Value | Why |
|---|---|---|
| image | `vllm/vllm-openai:v0.27.1` | the card's validated container |
| `--moe-backend` | `marlin` | only backend correct on SM 12.0 (above) |
| `--mamba-backend` | `flashinfer` | card, every profile |
| `--mamba-cache-mode` | `align` | card; also what prefix caching requires |
| `--kv-cache-dtype` | `fp8` | card, DGX Spark profile |
| `--enable-prefix-caching` | on | card, universal |
| `--async-scheduling` | on | card, universal — **was missing** |
| `--reasoning-parser` | `nemotron_v3` | card; built into 0.27, no plugin file |
| `--tool-call-parser` | `qwen3_coder` | card |
| `--speculative_config.model` | DSpark | card, DGX Spark + H100-interactive |
| `--speculative_config.num_speculative_tokens` | 3 | same (GB200 uses 5) |
| `--max-model-len` | 131072 | ours; card validates 1M, we do not need it |
| `--max-num-seqs` | 8 | ours: one interactive user, not a throughput farm |
| `--gpu-memory-utilization` | 0.38 | ours: co-residency for A/B. Card says 0.91 for a sole model — that is the promotion step, not this one |

`--async-scheduling` was the only recommended-everywhere flag we did not
have. It overlaps CPU-side scheduling with GPU execution; it was incompatible
with speculative decoding in vLLM ≤0.10, which is the likely reason it never
got added here. Verified accepted by 0.27.1 alongside the DSpark drafter, and
**it changes nothing measurable at our concurrency** (cold C1 492 vs a 505
baseline, cold C4 795 vs 804 — one sample each, inside noise). That is the
expected result: at one to four in-flight requests there is little scheduling
to overlap. Kept because it is the vendor's universal recommendation and
costs nothing measurable, not because it bought us anything.

### A benchmark caveat found while measuring that

`ab_bench.py` sends a **fixed** prompt set at servers running
`--enable-prefix-caching`, so its numbers depend on cache state and two
reports can look comparable while measuring different regimes. Same build,
same flags, 2026-08-15:

| | C1 | C4 | TTFT p50 @C4 |
|---|---|---|---|
| freshly started server | 492 tok/s | 795 tok/s | 0.586 s |
| third run, same process | 518 tok/s | 1380 tok/s | 0.090 s |

**1.7× at C4 from cache state alone.** `--warmup` now pins the warm regime
and the report records which one it used. Every stored `ab_bench` number
before today is a cold-start number; do not compare one to a warmed run.

### Optimization space, ranked, none of it measured

1. ~~**DFlash instead of DSpark**~~ — **MEASURED 2026-08-20, and the answer
   is no. DSpark stays.** See "DFlash: measured, and unusable" below.
2. **Draft length above 3.** Both drafter cards report acceptance at draft
   length **7**; we run 3, and the GB200 profile uses 5. Free to try.
   First data point, from the DFlash work: dropping DSpark from 3 to 1 costs
   **38% at C1** (518.1 → 322.2 tok/s) and **nothing at C4** (820.2 → 801.2).
   Speculation is worth most exactly where we live — one interactive user —
   so going *up* from 3 is the direction worth measuring, and C1 is the
   number that will move.
3. **Native MTP.** Ships inside the checkpoint, no download, no second model
   resident. The card recommends it for medium-to-high concurrency, so at our
   single-user load DSpark should win — but the SM 12.0 report saw MTP cost
   22% on Marlin (different model, TP=4), worth knowing before assuming.
4. **`--moe-backend humming`.** The card's H100 and Ampere choice, untested
   on SM 12.0. The other non-Marlin paths here fail either silently (forced
   CUTLASS → garbage) or loudly (`flashinfer_b12x` → shape error at load),
   so this needs a **correctness** check before a speed one.
5. **`--mamba-ssm-cache-dtype float16`.** In the H100 profiles, absent from
   DGX Spark's. Halves the Mamba state; it is a precision change to a
   recurrent state, so it needs an eval run, not a throughput run.
6. **`--gpu-memory-utilization 0.91`** and dropping the co-residency
   reservation — part of the production switch (§4.5), with a rollback, not
   a tuning knob.

For reference, someone benchmarked this exact GPU and model publicly and got
935 / 1730 / 1176 tok/s on prompt-heavy / decode-heavy / balanced
`vllm bench serve` at 16 prompts, with speculative decoding on. They did not
publish their flags, so it is a sanity check on the order of magnitude and
nothing more.

**Sources.** [NVIDIA Nemotron 3.5 Lightning launch blog](https://developer.nvidia.com/blog/nvidia-nemotron-3-5-lightning-delivers-fast-accurate-specialized-task-execution-for-long-running-agents/) ·
[NVFP4 model card and vLLM recipes](https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4) ·
[DSpark drafter card](https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4-DSpark) ·
[DFlash drafter card](https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4-DFlash) ·
[SM120 / RTX PRO 6000 NVFP4 MoE report (vLLM forums)](https://discuss.vllm.ai/t/sm120-rtx-pro-6000-nvfp4-moe-performance-report-qwen3-5-397b/2536) ·
[DGX Spark vs RTX PRO 6000 on this model (NVIDIA forums)](https://forums.developer.nvidia.com/t/nvidia-nemotron-3-5-lightning-30b-a3b-nvfp4-dgx-spark-vs-rtx-pro-6000-blackwell-performance/379921) ·
[CUTLASS #3096 — SM120 grouped GEMM, fix not upstreamed](https://github.com/NVIDIA/cutlass/issues/3096) ·
[vLLM #31085 — native NVFP4 MoE for SM120, still open](https://github.com/vllm-project/vllm/issues/31085)

No arXiv paper exists for Nemotron 3.5 Lightning; the launch blog and the
model cards are the primary sources.
