# Agent business evaluation

`business_cases.json` is the versioned release corpus for EXPANSION_PLAN 4.1.
It contains 24 fixed trajectories across evidence lookup, new analysis, product
help and out-of-scope handling. Expectations cover tool presence and order,
forbidden tools, structured evidence and a small number of answer guard terms.

Validate the corpus without a model:

```bash
python scripts/evaluate_agent_business.py --validate-only
```

Collect and grade a live local service:

```bash
python scripts/evaluate_agent_business.py \
  --base-url http://127.0.0.1:8001 \
  --output evals/results/agent-business.jsonl
```

Regrade a captured JSONL artifact deterministically:

```bash
python scripts/evaluate_agent_business.py \
  --responses evals/results/agent-business.jsonl \
  --output evals/results/agent-business-replay.jsonl
```

The report records the corpus SHA-256, per-check results and pass rates by
business intent. Cases with `release_gate` intentionally remain in the fixed
set when an artifact or implementation is unavailable; benchmarks must expose
those gaps rather than silently dropping the cases.
