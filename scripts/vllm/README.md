# vLLM startup configs for Urban Dossier

Three launch profiles for `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4` on
**NVIDIA DGX Spark (GB10 Grace Blackwell, SM 12.1, 128 GiB unified LPDDR5X)**.

The launcher is `start_vllm.sh`. It accepts `--profile {demo|balanced|long-context}`
(default `balanced`) and `--dry-run`. Tunables live in `vllm.env` (copy from
`vllm.env.example`).

## Quick start

```bash
chmod +x scripts/vllm/start_vllm.sh

# Default — daily use
scripts/vllm/start_vllm.sh

# Hack Fair: multiple judges hitting the demo concurrently
scripts/vllm/start_vllm.sh --profile demo

# Single long-document deep-dive
scripts/vllm/start_vllm.sh --profile long-context

# Inspect the resolved command without launching
scripts/vllm/start_vllm.sh --profile balanced --dry-run
```

## Profile comparison

| profile        | `--max-model-len` | `--max-num-seqs` | `--max-num-batched-tokens` | when to use |
| -------------- | -----------------:| ----------------:| --------------------------:| ----------- |
| `demo`         |             8 192 |               16 |                     32 768 | Hack Fair, parallel judges, short prompts |
| `balanced`     |            32 768 |                8 |                     32 768 | **Default**: daily Urban Dossier API + agent loops |
| `long-context` |           131 072 |                1 |                     16 384 | Single long-document narrative / report run |

All three keep `--gpu-memory-utilization 0.7` (deliberately conservative on
GB10's shared 128 GiB pool — leaves ~38 GiB for the cuDF service, NemoClaw
sandbox, Node/Python backend, and OS), `--kv-cache-dtype fp8`,
`--enable-prefix-caching`,
`--enable-chunked-prefill`, `--async-scheduling`, FlashInfer FP4 MoE
(`throughput` backend), and the Nemotron `nano_v3` reasoning + `qwen3_coder`
tool-call parsers.

## Why these numbers — KV cache math

Nemotron-3-Nano-30B is a **hybrid Mamba2 / Transformer** model. Out of 52
layers, only **6** are attention. From the model's `config.json`:

```
num_hidden_layers       = 52
hybrid_override_pattern = "MEMEM*EMEMEM*EMEMEM*EMEMEM*EMEMEM*EMEMEMEM*EMEMEMEME"
                          (M = Mamba2, E = MoE/MLP, * = attention)
                          → 23 Mamba, 23 MoE, 6 attention
num_key_value_heads     = 2     # GQA
head_dim                = 128
```

### Per-token KV cache (attention layers only)

```
per_token_kv = 2 (K and V)
             × num_attn_layers   (6)
             × num_kv_heads      (2)
             × head_dim          (128)
             × bytes_per_element (1 for FP8, 2 for BF16)
```

| dtype | bytes/token | KiB/token |
| ----- | -----------:| ---------:|
| FP8   |       3 072 |      3.00 |
| BF16  |       6 144 |      6.00 |

Compare a "naive" dense LLM with all 52 layers being attention and 8 KV heads:
~106 KiB/token at FP8 — **~35× larger**. The hybrid architecture is why the
KV-cache budget is barely a knob on this model; the dominant per-sequence cost
is the **fixed-size Mamba SSM state**.

### Per-sequence Mamba state (does NOT scale with tokens)

```
conv_state/layer = (expand·hidden + 2·n_groups·ssm_state_size) × (conv_kernel-1)
                 = (2·2688 + 2·8·128) × 3 = 22,272 elements
ssm_state/layer  = mamba_num_heads × mamba_head_dim × ssm_state_size
                 = 64 × 64 × 128       = 524,288 elements
per_layer_per_seq = (22272 + 524288) × 4 bytes (fp32) ≈ 2.13 MiB
per_seq           = 23 mamba layers × 2.13 MiB        ≈ 47.95 MiB
```

### Profile budgets (KV + Mamba — model weights ~15 GiB extra)

| profile        | KV (FP8)   | Mamba state | KV+Mamba total |
| -------------- | ----------:| -----------:| --------------:|
| `demo`         |   0.38 GiB |   767 MiB   |       1.12 GiB |
| `balanced`     |   0.75 GiB |   384 MiB   |       1.12 GiB |
| `long-context` |   0.38 GiB |    48 MiB   |       0.42 GiB |

`demo` and `balanced` happen to coincide at ~1.12 GiB — by design, both
profiles aim for the same activation footprint, just trading sequence count
for context length. None of these come close to saturating GB10's 128 GiB.

The previous config (`131072 × 1, FP8 KV`) used only **0.38 GiB of KV +
48 MiB of Mamba per request**. The "OOM at >1 seq" symptom from the old
README was almost certainly **FlashInfer's MoE workspace + activation
buffers** competing with the model weights inside `gpu-memory-utilization`,
*not* KV cache size. Bumping the bound from 0.65 to 0.70 (we deliberately
stop short of 0.85) recovers ~6 GiB of headroom while preserving room for
the cuDF service, NemoClaw sandbox, and FastAPI/Node co-tenants on the same
GB10.

Sources:

- HF model card config — `https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4/blob/main/config.json`
- vLLM Hybrid KV Cache design doc — `https://docs.vllm.ai/en/latest/design/hybrid_kv_cache_manager/`
- PyTorch blog "Hybrid Models as First-Class Citizens in vLLM"
- vLLM Recipes — Nemotron-3-Nano-30B-A3B (DGX Spark section)

## Optimizations applied vs. deferred

### Applied

| change | rationale |
| ------ | --------- |
| Bumped `--gpu-memory-utilization` 0.65 → 0.70 | conservative bump (~6 GiB more headroom) without starving cuDF / NemoClaw / Node co-tenants on the shared 128 GiB pool |
| Removed `--enforce-eager` | Mamba2 + CUDA graphs in vLLM 0.12 cuts CPU overhead substantially (PyTorch blog) |
| Replaced `--moe-backend marlin` with `VLLM_FLASHINFER_MOE_BACKEND=throughput` | NVFP4 path uses FlashInfer, not the marlin GEMM kernel |
| Added `VLLM_USE_FLASHINFER_MOE_FP4=1` | required for the NVFP4 MoE kernels to load |
| Added `--async-scheduling` | "always recommended" per official recipe |
| Added `--max-num-batched-tokens` per profile | makes prefill pacing explicit instead of relying on heuristics |
| Added `--reasoning-parser nano_v3` + plugin path | Nemotron 3 ships a custom parser; without it `reasoning_content` is empty |
| Added `--tool-call-parser qwen3_coder` + `--enable-auto-tool-choice` | required for the agent endpoints (`/api/agent/*`) to receive parsed tool calls |
| Set `--mamba-ssm-cache-dtype float32` | model card recommends fp32 for accuracy |

### Deferred (TODO verify on hardware)

| candidate | why deferred |
| --------- | ------------ |
| `--mamba-ssm-cache-dtype float16` | recipe says "minor accuracy loss"; want to A/B against scoring outputs first |
| `VLLM_DISABLED_KERNELS=cutlass_moe_mm,cutlass_scaled_mm` | only needed if SM 12.1 cutlass kernels crash; left commented in `vllm.env.example` |
| `--gpu-memory-utilization 0.85` | Brev cookbook value; only safe if cuDF service + NemoClaw sandbox are moved to a different host or measured to fit in <12 GiB |
| Re-adding `--enforce-eager` | only if CUDA graph capture OOMs during warmup or takes >2 min on first launch |
| 1M context (`VLLM_ALLOW_LONG_MAX_MODEL_LEN=1`) | model supports it but Urban Dossier never needs >131K |

## Verifying the server is up

```bash
# Liveness
curl -fsS http://localhost:8000/v1/models | python3 -m json.tool

# Smoke test — should print a haiku and reasoning_content
curl -fsS http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4",
    "messages": [{"role":"user","content":"Write a haiku about NYC."}],
    "max_tokens": 200,
    "temperature": 0.6
  }' | python3 -c '
import sys,json
r = json.load(sys.stdin)
m = r["choices"][0]["message"]
print("reasoning:", (m.get("reasoning_content") or "")[:120])
print("content  :", (m.get("content") or "")[:200])
'
```

If `reasoning_content` is empty, the `nano_v3` parser plugin did not load —
check the path in `PARSER_PLUGIN` and re-`wget` it from the HF repo (see
`vllm.env.example` for the URL).

## Switching profiles without restart? — no

vLLM bakes `max_model_len`, `max_num_seqs`, and the KV-cache layout at
startup. Changing profile requires a server restart. Plan accordingly:
during the demo, prefer `demo`; during dev, `balanced`.
