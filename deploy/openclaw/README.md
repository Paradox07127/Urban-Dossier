# Dedicated Urban Dossier OpenClaw agent

This directory defines the production text-analysis agent used by FastAPI. The
agent receives deterministic neighborhood scores and evidence from the backend;
it does not own data access, scoring, or arbitrary tool execution.

Validated workstation runtime (2026-08-02):

- NemoClaw 0.0.100;
- OpenShell 0.0.85 with the Docker driver;
- OpenClaw 2026.7.1;
- sandbox `urban-dossier-agent`;
- local compatible inference route to the vLLM-served Nemotron model.

## Boundary and request path

```text
FastAPI process
  -> authenticated OpenResponses HTTP on 127.0.0.1:18789
  -> SSH forward created by `nemoclaw ... recover`
  -> Gateway inside the OpenShell container
  -> `urban-dossier` OpenClaw agent
  -> OpenShell-compatible inference route
  -> host vLLM :8000
```

The persistent FastAPI HTTP client removes per-request NemoClaw/OpenClaw CLI
startup. It does not run OpenClaw on the host and does not bypass OpenShell.

## Least-privilege policy

- workspace contains only `AGENTS.md` and `SOUL.md`;
- `skills: []` prevents general OpenClaw skill discovery;
- tool profile is `minimal`, with only `session_status` allowed;
- filesystem, runtime, web, messaging, browser, canvas, agent-spawn and Tool
  Search capabilities are explicitly denied;
- URL/file/image fetching on the OpenResponses endpoint is disabled;
- the Gateway bearer token is written to a host file with mode `0600` and is
  never printed by the smoke test or placed in systemd configuration.

The current OpenClaw schema exposes Tool Search globally rather than per agent,
so `tools.toolSearch=false` applies to this entire dedicated sandbox. Do not add
unrelated agents to the same sandbox.

## Files

| File | Purpose |
| --- | --- |
| `agents.yaml` | rebuild/onboard roster and tool policy |
| `urban-dossier/AGENTS.md` | task contract and grounding rules |
| `urban-dossier/SOUL.md` | minimal response style |
| `../../scripts/configure_openclaw_agent.sh` | idempotent post-onboard reconciliation |
| `../../scripts/refresh_openclaw_gateway_token.sh` | protected token refresh |
| `../../scripts/test_openclaw_gateway.py` | authenticated routing smoke test |

## First-time onboard

Start and validate vLLM before this step. Then run the interactive NemoClaw
wizard:

```bash
cd /mnt/data/Urban-Dossier

nemoclaw onboard \
  --name urban-dossier-agent \
  --agent openclaw \
  --agents deploy/openclaw/agents.yaml
```

Choose a compatible endpoint backed by local vLLM and model
`nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4`. The sandbox does not need direct
GPU access because all inference is performed by the separate vLLM container.

This wizard is the only expected manual step. After it completes:

```bash
bash scripts/configure_openclaw_agent.sh
```

The script:

1. reconciles `agents.yaml`;
2. uploads the minimal workspace contents;
3. makes `urban-dossier` the default agent;
4. disables thinking/reasoning defaults, Skills, Tool Search and irrelevant
   tools;
5. enables the protected OpenResponses endpoint with URL fetching disabled;
6. restarts/reconnects the Gateway;
7. refreshes the mode-0600 host token file.

## Default-agent compatibility workaround

OpenResponses accepts `model=openclaw/<agent-id>` and an agent header. The
packaged OpenClaw 2026.7.1 runtime was observed routing those requests to
`main`, so this dedicated sandbox currently sets `urban-dossier` as its default.
The FastAPI client still sends both selectors for forward compatibility.

NemoClaw rebuild/onboard manifests create `main` as the default, so the
post-onboard script must be rerun after every sandbox rebuild. Re-test selector
routing after an OpenClaw upgrade; remove this workaround only after the actual
session key is recorded under `agent:urban-dossier:*` without relying on the
default.

## Backend configuration

```text
URBAN_DOSSIER_AGENT_BACKEND=nemoclaw
NEMOCLAW_SANDBOX=urban-dossier-agent
OPENCLAW_TRANSPORT=gateway
OPENCLAW_AGENT_ID=urban-dossier
OPENCLAW_GATEWAY_URL=http://127.0.0.1:18789
OPENCLAW_GATEWAY_TOKEN_FILE=/mnt/data/urban-dossier/runtime/openclaw-gateway.token
```

Set `OPENCLAW_TRANSPORT=cli` for explicit rollback. A Gateway request failure
also falls back to `nemoclaw <sandbox> agent --agent urban-dossier`.

## Validation

```bash
nemoclaw urban-dossier-agent status
nemoclaw urban-dossier-agent agents list

.venv/bin/python scripts/test_openclaw_gateway.py

curl -fsS http://127.0.0.1:8090/api/agent/status \
  | python3 -m json.tool
```

Expected smoke output contains status 200 and `gateway-route-ok`. The dedicated
agent should report no Skills and only `session_status` in its tool schema.

## Recovery and rebuild

Recover a stopped forward/container without recreating state:

```bash
nemoclaw urban-dossier-agent recover
bash scripts/refresh_openclaw_gateway_token.sh
systemctl --user restart urban-dossier-backend.service
```

After an upgrade/rebuild:

```bash
nemoclaw backup-all
nemoclaw urban-dossier-agent rebuild --yes
bash scripts/configure_openclaw_agent.sh
systemctl --user restart urban-dossier-backend.service
.venv/bin/python scripts/test_openclaw_gateway.py
```

Use `nemoclaw urban-dossier-agent stop` for a recoverable stop. `destroy` is a
destructive sandbox deletion and is not part of routine shutdown.
