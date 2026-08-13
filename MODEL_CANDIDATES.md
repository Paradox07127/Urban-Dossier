# Model Candidates — Nemotron 3.5 Lightning & Nemotron 3 Super

Decision record for the 2026-08-12 evaluation of two candidate models against
the production LLM, run side by side on the x86 workstation
(RTX PRO 6000 Blackwell, 96 GiB). Production serving is unchanged until the
promotion checklist at the bottom passes.

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

- **Serving stack:** requires vLLM ≥ 0.27.1 (production runs 0.23.0). New
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
Mamba state at `--gpu-memory-utilization 0.92`, hence the compose defaults of
32K context and 2 sequences. It cannot run concurrently with any other GPU
service. Treat it as a quality reference point / offline second-opinion
model, not a production candidate on this hardware.

## Running the comparison

```bash
# Production + Lightning side by side (0.45 + 0.30 GPU fractions):
docker compose --env-file /mnt/data/urban-dossier-state/runtime/gpu.env \
  -f deploy/compose.gpu.yml --profile inference --profile candidate up -d llm llm-lightning

python3 scripts/vllm/ab_bench.py \
  --endpoint current=http://127.0.0.1:8000 \
  --endpoint lightning=http://127.0.0.1:8002 \
  --output /tmp/ab_bench.json

# Super alone (stop everything else on the GPU first):
docker compose ... stop llm llm-lightning embeddings
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
4. License review of OpenMDW-1.1 for our use (user decision).
5. ~~Pin the v0.27.1 image digest in `deploy/compose.gpu.yml`.~~ Done
   2026-08-12, Nano regression passed on it. Remaining: retarget the
   `models/llm` mount (or path) to the Lightning weights, update
   `LLM_SERVED_NAME`, record rollback here.

## Status

- 2026-08-12: weights downloading; compose services and benchmark harness
  added. Local A/B results to be appended below.
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
