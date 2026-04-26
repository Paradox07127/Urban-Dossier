#!/usr/bin/env bash
# start_vllm.sh — vLLM launcher for nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4
# on NVIDIA DGX Spark (GB10 Grace Blackwell, SM 12.1, 128 GiB unified LPDDR5X).
#
# Usage:
#   ./start_vllm.sh                              # uses 'balanced' profile
#   ./start_vllm.sh --profile demo               # high concurrency, short ctx
#   ./start_vllm.sh --profile long-context       # single seq, full 128K
#   ./start_vllm.sh --profile balanced --dry-run # print resolved command
#   PORT=8001 MODEL_PATH=/data/models/nemotron ./start_vllm.sh
#
# Env vars (also overridable via vllm.env / vllm.env.example):
#   PROFILE        demo | balanced | long-context  (default: balanced)
#   MODEL_PATH     local model path                (default: /model)
#   SERVED_NAME    name advertised on /v1/models   (default: model HF id)
#   HOST / PORT    bind address                    (default: 0.0.0.0 / 8000)
#   EXTRA_ARGS     appended verbatim to vllm CLI   (default: empty)
#   PARSER_PLUGIN  path to nano_v3_reasoning_parser.py (default: $MODEL_PATH/...)
#
# Sources for choices:
#   - vLLM Recipes — Nemotron-3-Nano-30B-A3B (DGX Spark section, vLLM 0.12.0+)
#       https://docs.vllm.ai/projects/recipes/en/latest/NVIDIA/Nemotron-3-Nano-30B-A3B.html
#   - HF model card NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4 (FlashInfer FP4 MoE)
#       https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4
#   - Brev cookbook (NVFP4 cell): VLLM_USE_FLASHINFER_MOE_FP4=1, throughput backend
#       https://github.com/NVIDIA-NeMo/Nemotron/blob/main/usage-cookbook/Nemotron-3-Nano/vllm_cookbook.ipynb
#   - vLLM docs — Hybrid KV Cache Manager (NemotronH = Mamba2 + GQA attention)
#       https://docs.vllm.ai/en/latest/design/hybrid_kv_cache_manager/

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
PROFILE="${PROFILE:-balanced}"
MODEL_PATH="${MODEL_PATH:-/model}"
SERVED_NAME="${SERVED_NAME:-nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
EXTRA_ARGS="${EXTRA_ARGS:-}"
DRY_RUN=0

# Optional .env file beside this script (sourced if present)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${SCRIPT_DIR}/vllm.env" ]]; then
  # shellcheck disable=SC1091
  source "${SCRIPT_DIR}/vllm.env"
fi

# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------
usage() {
  cat <<EOF
Usage: $0 [--profile demo|balanced|long-context] [--dry-run] [--help]

Profiles (see README.md in this dir for KV-cache math):
  demo          max_model_len=8192   max_num_seqs=16   (Hack Fair / multi-judge)
  balanced      max_model_len=32768  max_num_seqs=8    (default — daily use)
  long-context  max_model_len=131072 max_num_seqs=1    (single long-doc analysis)

Env overrides: PROFILE, MODEL_PATH, SERVED_NAME, HOST, PORT, EXTRA_ARGS, PARSER_PLUGIN
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)      PROFILE="${2:?--profile needs a value}"; shift 2 ;;
    --profile=*)    PROFILE="${1#*=}"; shift ;;
    --dry-run)      DRY_RUN=1; shift ;;
    -h|--help)      usage; exit 0 ;;
    *)              echo "unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done

# ---------------------------------------------------------------------------
# Profile resolution
# ---------------------------------------------------------------------------
case "${PROFILE}" in
  demo)
    MAX_MODEL_LEN=8192
    MAX_NUM_SEQS=16
    # why: demo profile feeds many short prompts; batch larger than ctx*seqs
    #      so the scheduler can fully pack; 4x max_model_len is a safe ceiling.
    MAX_NUM_BATCHED_TOKENS=32768
    ;;
  balanced)
    MAX_MODEL_LEN=32768
    MAX_NUM_SEQS=8
    # why: 1x max_model_len is vLLM's default heuristic — keeps prefill latency
    #      bounded while still permitting decode batching across 8 sequences.
    MAX_NUM_BATCHED_TOKENS=32768
    ;;
  long-context)
    MAX_MODEL_LEN=131072
    MAX_NUM_SEQS=1
    # why: long-context only ever sees one big request; chunked-prefill carves
    #      it into 16K slices so we never schedule a 131K monolithic prefill.
    MAX_NUM_BATCHED_TOKENS=16384
    ;;
  *)
    echo "unknown profile: ${PROFILE}" >&2; usage; exit 2 ;;
esac

# Default reasoning-parser plugin path lives next to the weights
PARSER_PLUGIN="${PARSER_PLUGIN:-${MODEL_PATH}/nano_v3_reasoning_parser.py}"

# ---------------------------------------------------------------------------
# Environment variables (must be exported BEFORE vllm starts)
# ---------------------------------------------------------------------------
# why: NVFP4 MoE on Blackwell uses FlashInfer's FP4 grouped-GEMM kernels.
# Source: HF model card "Use with vLLM" + Brev NVFP4 cookbook cell.
export VLLM_USE_FLASHINFER_MOE_FP4="${VLLM_USE_FLASHINFER_MOE_FP4:-1}"

# why: 'throughput' backend favors batch decode over single-stream latency,
# matching all three of our concurrency-oriented profiles. Source: same.
export VLLM_FLASHINFER_MOE_BACKEND="${VLLM_FLASHINFER_MOE_BACKEND:-throughput}"

# why: NemotronH custom modeling code lives in the model repo. vLLM picks it up
# only with HF_HUB / transformers trust mode + the --trust-remote-code CLI flag.
export TRANSFORMERS_OFFLINE="${TRANSFORMERS_OFFLINE:-0}"

# why (TODO verify on hardware): on SM 12.1 (DGX Spark), NVFP4 path historically
# benefits from disabling the cutlass MoE/scaled-mm kernels in favor of the
# FlashInfer / Marlin ones. See docs/nvidia-stack-dgx-spark.md L423.
# Leave commented unless we've reproduced the bug.
# export VLLM_DISABLED_KERNELS="cutlass_moe_mm,cutlass_scaled_mm"  # TODO verify on hardware

# ---------------------------------------------------------------------------
# Build command
# ---------------------------------------------------------------------------
CMD=(
  python3 -m vllm.entrypoints.openai.api_server
  --model                 "${MODEL_PATH}"
  --served-model-name     "${SERVED_NAME}"
  --host                  "${HOST}"
  --port                  "${PORT}"
  --tensor-parallel-size  1
  # why: NemotronH config.json declares NemotronHConfig + custom modeling code.
  --trust-remote-code
  # why: only 6 of 52 layers are attention; FP8 halves the per-token KV cache
  # (3072 -> already tiny). Recipe explicitly recommends fp8 for FP8/NVFP4 ckpts.
  --kv-cache-dtype        fp8
  # why: official recipe — "We recommend always adding this flag for best
  # performance" (overlaps host scheduling with GPU decode).
  --async-scheduling
  # why: GB10's 128 GiB is shared with the OS, cuDF service, NemoClaw sandbox,
  # and the Node/Python backend. 0.7 leaves ~38 GiB headroom for those co-tenants
  # while still being a +5 pt bump over the original 0.65.
  --gpu-memory-utilization 0.7
  --max-model-len         "${MAX_MODEL_LEN}"
  --max-num-seqs          "${MAX_NUM_SEQS}"
  # why: explicitly cap prefill batch — defaults can be surprisingly large on
  # GPUs with lots of memory and starve concurrent decode steps.
  --max-num-batched-tokens "${MAX_NUM_BATCHED_TOKENS}"
  # why: re-uses the prefix KV blocks across requests with shared system prompts
  # (Urban Dossier sends the same scoring rubric on every /api/analyze-point).
  --enable-prefix-caching
  # why: avoids one-shot 131K prefills hogging the GPU; lets decode interleave.
  # vLLM v1 enables this by default but we set it explicitly so the intent is
  # legible regardless of upstream defaults.
  --enable-chunked-prefill
  # why: NemotronH ships a custom reasoning parser that splits <think>...</think>
  # into message.reasoning_content. Required for tool-calling agent loops.
  --reasoning-parser-plugin "${PARSER_PLUGIN}"
  --reasoning-parser        nano_v3
  # why: agent endpoints (/api/agent/*) issue OpenAI tool-calls; the model uses
  # qwen3-style XML-tagged tool blocks per the official recipe.
  --enable-auto-tool-choice
  --tool-call-parser        qwen3_coder
  # why: SSM cache dtype — keep fp32 for accuracy. Recipe notes float16 trades
  # ~negligible accuracy for speed; revisit if decode is host-bound.
  # # TODO verify on hardware whether --mamba-ssm-cache-dtype float16 is faster.
  --mamba-ssm-cache-dtype   float32
)

# REMOVED vs. previous config:
#   --enforce-eager   — Mamba2 layers benefit massively from CUDA graph capture
#                       (PyTorch blog "Hybrid Models as First-Class Citizens in
#                       vLLM"). vLLM 0.12.0 supports full graphs for NemotronH.
#                       # TODO verify on hardware that capture finishes in <2 min
#                       and doesn't OOM during warmup; if it does, re-add.
#   --moe-backend marlin — superseded by VLLM_FLASHINFER_MOE_BACKEND=throughput
#                       env var, which is what the FP4 path actually consumes.

# Append user-supplied extras
if [[ -n "${EXTRA_ARGS}" ]]; then
  # shellcheck disable=SC2206
  EXTRA=( ${EXTRA_ARGS} )
  CMD+=( "${EXTRA[@]}" )
fi

# ---------------------------------------------------------------------------
# Emit / execute
# ---------------------------------------------------------------------------
echo "# vLLM launch — profile=${PROFILE}"
echo "# MODEL_PATH=${MODEL_PATH}"
echo "# Resolved env:"
echo "#   VLLM_USE_FLASHINFER_MOE_FP4=${VLLM_USE_FLASHINFER_MOE_FP4}"
echo "#   VLLM_FLASHINFER_MOE_BACKEND=${VLLM_FLASHINFER_MOE_BACKEND}"
echo "# Resolved command:"
printf '  %q' "${CMD[@]}"
echo
echo

if [[ "${DRY_RUN}" -eq 1 ]]; then
  echo "(dry-run — not executing)"
  exit 0
fi

# Sanity check
if [[ ! -e "${MODEL_PATH}" ]]; then
  echo "ERROR: MODEL_PATH '${MODEL_PATH}' does not exist." >&2
  echo "       Set MODEL_PATH=... or symlink the weights into /model." >&2
  exit 3
fi
if [[ ! -f "${PARSER_PLUGIN}" ]]; then
  echo "WARN: reasoning parser plugin not found at: ${PARSER_PLUGIN}" >&2
  echo "      Download with:" >&2
  echo "      wget -O ${PARSER_PLUGIN} \\" >&2
  echo "        https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4/resolve/main/nano_v3_reasoning_parser.py" >&2
fi

exec "${CMD[@]}"
