"""Agent service: orchestrates OpenClaw skills for deep analysis."""

from __future__ import annotations
import datetime
import json
import os
import subprocess
import sys
import tempfile
import logging
import re
import threading


def _md_to_html(text: str) -> str:
    """Minimal markdown-to-HTML: headings, bold, italic, bullets, paragraphs."""
    lines = text.split("\n")
    out: list[str] = []
    in_ul = False
    for line in lines:
        stripped = line.strip()
        # Headings
        if stripped.startswith("### "):
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append(f"<h3>{stripped[4:]}</h3>")
            continue
        if stripped.startswith("## "):
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append(f"<h2>{stripped[3:]}</h2>")
            continue
        if stripped.startswith("# "):
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append(f"<h1>{stripped[2:]}</h1>")
            continue
        # Bullets
        if stripped.startswith("- ") or stripped.startswith("* "):
            if not in_ul:
                out.append("<ul>"); in_ul = True
            out.append(f"<li>{stripped[2:]}</li>")
            continue
        # Close list if non-bullet
        if in_ul:
            out.append("</ul>"); in_ul = False
        # Blank line
        if not stripped:
            continue
        # Paragraph
        out.append(f"<p>{stripped}</p>")
    if in_ul:
        out.append("</ul>")
    html = "\n".join(out)
    # Inline: bold, italic
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
    html = re.sub(r"\*(.+?)\*", r"<em>\1</em>", html)
    return html


class _SafeEncoder(json.JSONEncoder):
    """Handle date/datetime and other non-serializable types from DuckDB."""
    def default(self, obj):
        if isinstance(obj, (datetime.date, datetime.datetime)):
            return obj.isoformat()
        if hasattr(obj, '__float__'):
            return float(obj)
        return super().default(obj)

from .config import DEFAULT_OPENAI_BASE_URL, DEFAULT_OPENAI_API_KEY
from .report import _resolve_model_name, _strip_thinking

logger = logging.getLogger(__name__)

# SymGen anti-hallucination pipeline imports
# These scripts live in the blocksense-report skill directory
_symgen_imported = False
resolve_symgen = None  # type: ignore[assignment]
verify_narrative = None  # type: ignore[assignment]


def _ensure_symgen_imports():
    """Lazily import resolve_symgen and verify_narrative from blocksense scripts."""
    global _symgen_imported, resolve_symgen, verify_narrative
    if _symgen_imported:
        return
    scripts_dir = os.path.join(SKILL_BASE, "blocksense-report", "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    try:
        from resolve_symgen import resolve_symgen as _resolve  # type: ignore[import-not-found]
        from verify_narrative import verify_narrative as _verify  # type: ignore[import-not-found]
        resolve_symgen = _resolve
        verify_narrative = _verify
        _symgen_imported = True
        logger.info("SymGen pipeline loaded from %s", scripts_dir)
    except ImportError as exc:
        logger.warning("SymGen pipeline not available: %s", exc)
        _symgen_imported = True  # don't retry

# Paths to blocksense skill scripts
SKILL_BASE = os.environ.get("BLOCKSENSE_SKILL_PATH", os.path.expanduser("~/xhh_code"))
REPORT_SCRIPTS = os.path.join(SKILL_BASE, "blocksense-report", "scripts")
POSTER_SCRIPTS = os.path.join(SKILL_BASE, "blocksense-poster", "scripts")
REPORT_TEMPLATE = os.path.join(SKILL_BASE, "blocksense-report", "templates", "report.html")
POSTER_TEMPLATES_DIR = os.path.join(SKILL_BASE, "blocksense-poster", "templates")

# Agent backend mode — "nemoclaw" = OpenClaw via sandbox, "scripts" = direct skill scripts + vllm
AGENT_BACKEND = os.environ.get("URBAN_DOSSIER_AGENT_BACKEND", "nemoclaw")

# NemoClaw 0.0.100 exposes the selected in-sandbox agent through its host CLI.
# Keep the binary and sandbox configurable so development/test sandboxes do not
# require code changes.  Do not couple the backend to OpenShell's container
# names or runtime driver; those are deliberately private implementation details.
NEMOCLAW_BIN = os.environ.get("NEMOCLAW_BIN", "nemoclaw")
NEMOCLAW_SANDBOX = os.environ.get("NEMOCLAW_SANDBOX", "urban-dossier-agent")
OPENCLAW_AGENT_ID = os.environ.get("OPENCLAW_AGENT_ID", "urban-dossier")
OPENCLAW_TRANSPORT = os.environ.get("OPENCLAW_TRANSPORT", "gateway").strip().lower()
OPENCLAW_GATEWAY_URL = os.environ.get(
    "OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789"
).rstrip("/")
OPENCLAW_GATEWAY_TOKEN_FILE = os.environ.get(
    "OPENCLAW_GATEWAY_TOKEN_FILE",
    "/mnt/data/urban-dossier-state/runtime/openclaw-gateway.token",
)
AGENT_ENABLED = os.environ.get("URBAN_DOSSIER_AGENT_ENABLED", "1").strip() in ("1", "true", "yes")
# Output budget for one Gateway turn. This was a hardcoded 4096 and that is not
# enough for a reasoning model: Nemotron Nano spends the budget thinking before
# it writes a word of the answer, so a final turn over tool results ended at
# `stopReason=length` with the answer never started, and OpenClaw surfaced
# "Agent couldn't generate a response" (observed 2026-08-20; the tools had run
# and returned real data, which is what made it look like a routing or parsing
# fault rather than a budget one).
#
# The ceiling is the served context: prompt + output must fit vLLM's
# --max-model-len, 32768 today. 8192 doubles the budget while still leaving
# 24K for the prompt. Raise it only alongside --max-model-len.
OPENCLAW_MAX_OUTPUT_TOKENS = int(
    os.environ.get("URBAN_DOSSIER_OPENCLAW_MAX_OUTPUT_TOKENS", "8192")
)

_openclaw_gateway_client = None
_openclaw_gateway_client_lock = threading.Lock()

# Per-call LLM timeout (not total operation timeout)
# Nemotron 30B on DGX Spark can be slow; give it room
LLM_CALL_TIMEOUT = 60


def _get_openai_client():
    """Lazily create an OpenAI client for vllm calls."""
    from openai import OpenAI
    return OpenAI(
        base_url=os.getenv("OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL),
        api_key=os.getenv("OPENAI_API_KEY", DEFAULT_OPENAI_API_KEY),
        timeout=LLM_CALL_TIMEOUT,
    )


def _llm_chat(client, model_name: str, system_msg: str, user_msg: str,
              temperature: float = 0.3, max_tokens: int = 300,
              enable_thinking: bool = False) -> str:
    """Single LLM chat call. Returns content string, or empty string on failure."""
    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            extra_body={"chat_template_kwargs": {"enable_thinking": enable_thinking}},
        )
        content = response.choices[0].message.content
        return _strip_thinking(content)
    except Exception as exc:
        logger.warning("Agent LLM call failed: %s", exc)
        return ""


def _llm_chat_multi(client, model_name: str, messages: list[dict],
                    temperature: float = 0.4, max_tokens: int = 500,
                    enable_thinking: bool = False) -> str:
    """Multi-message LLM chat call (for conversation context). Returns content string."""
    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            extra_body={"chat_template_kwargs": {"enable_thinking": enable_thinking}},
        )
        content = response.choices[0].message.content
        return _strip_thinking(content)
    except Exception as exc:
        logger.warning("Agent LLM multi-call failed: %s", exc)
        return ""


def _tmp_path(suffix: str) -> str:
    """Create a temp file path with blocksense prefix."""
    fd, path = tempfile.mkstemp(prefix="blocksense-", suffix=suffix)
    os.close(fd)
    return path


def _cleanup_files(*paths: str):
    """Remove temp files, ignoring errors."""
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.unlink(p)
        except OSError:
            pass


def _run_script(script_path: str, args: list[str], timeout: int = 120) -> tuple[bool, str]:
    """Run a python script as subprocess. Returns (success, stderr_or_stdout)."""
    cmd = ["python3", script_path] + args
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            logger.warning("Script %s failed (rc=%d): %s", script_path, result.returncode, result.stderr[:500])
            return False, result.stderr[:500]
        return True, result.stdout
    except subprocess.TimeoutExpired:
        logger.warning("Script %s timed out after %ds", script_path, timeout)
        return False, f"Script timed out after {timeout}s"
    except FileNotFoundError:
        logger.warning("Script not found: %s", script_path)
        return False, f"Script not found: {script_path}"


def _build_dimension_prompt(dimension: str, segment: dict, focus: str | None) -> str:
    """Build a SymGen data-card prompt for one dimension.

    The LLM is instructed to use {{field_name}} placeholders for ALL numbers.
    These get resolved to real values by resolve_symgen after generation.
    """
    data_card = segment.get("data_card", {})
    score = segment.get("score")

    # Build reference list from data_card
    ref_lines = []
    for key, info in data_card.items():
        if not isinstance(info, dict):
            continue
        ann = f' ({info["annotation"]})' if info.get("annotation") else ""
        ref_lines.append(f"  {{{{{key}}}}} = {info.get('display', '?')}{ann}")

    refs = "\n".join(ref_lines) if ref_lines else "  (no data available)"

    prompt = (
        f"=== {dimension.upper()} DATA CARD (score: {score if score is not None else '?'}/100) ===\n"
        f"Available references -- you MUST use {{{{ref}}}} syntax for ALL numbers:\n"
        f"{refs}\n"
        f"\n"
        f"RULES:\n"
        f"- Write 2-3 sentences using ONLY the {{{{references}}}} listed above\n"
        f'- Example: "With {{{{rodent_count}}}} confirmed rodent sites, the area shows {{{{rodent_trend}}}} activity."\n'
        f"- Do NOT write any bare numbers -- always use {{{{field_name}}}}\n"
        f"- If you need a fact not listed above, say \"data not available\" instead of guessing\n"
    )
    if focus and focus.lower() == dimension.lower():
        prompt += f"\nFocus: Provide extra detail on {dimension} since the user specifically asked about it.\n"

    return prompt


def _score_band(score: int | float | None) -> str:
    """Return a human-readable label for a 0-100 score."""
    if score is None:
        return "no data"
    s = int(score)
    if s >= 76:
        return "excellent"
    if s >= 56:
        return "good"
    if s >= 41:
        return "average"
    if s >= 21:
        return "below average"
    return "poor"


def _build_condensed_context(payload: dict) -> str:
    """Build an interpretive context prompt from the analysis payload.

    Goes beyond raw numbers: each metric gets a human-readable band label
    and brief comparative note so the LLM can reason about *meaning*, not
    just parrot digits.
    """
    target = payload.get("target", {})
    scores = payload.get("scores", {})
    actions = payload.get("priority_actions", [])
    current_state = payload.get("current_state", {})

    location = target.get("matched_address") or target.get("borough", "NYC")
    zip_code = target.get("zip", "")
    radius = target.get("radius_m", 500)
    overall = scores.get("overall")

    lines = [
        f"Location: {location}" + (f", ZIP {zip_code}" if zip_code else "") + f", {radius}m radius",
        f"Overall livability score: {overall}/100 ({_score_band(overall)})",
    ]

    # Dimension scores with interpretation
    dim_labels = {"safety": "Safety", "transit": "Transit & mobility",
                  "amenities": "Amenities & daily life", "building": "Building quality"}
    for dim, label in dim_labels.items():
        val = scores.get(dim)
        if val is not None:
            lines.append(f"  {label}: {val}/100 ({_score_band(val)})")

    # Interpretive metrics
    safety = current_state.get("safety", {})
    transit = current_state.get("transit", {})
    amenities = current_state.get("amenities", {})
    building = current_state.get("building", {})

    lines.append("")
    lines.append("Key facts (use these to support your answers):")

    collisions = transit.get("collision_count_500m")
    if collisions is not None:
        severity = "high" if collisions > 30 else ("moderate" if collisions > 10 else "low")
        lines.append(f"  - {collisions} traffic collisions within 500m ({severity} for NYC)")

    rodent = safety.get("rodent_positive_500m")
    if rodent is not None:
        severity = "heavy" if rodent > 20 else ("moderate" if rodent > 5 else "light")
        lines.append(f"  - {rodent} confirmed rodent sites nearby ({severity} activity)")

    ems = safety.get("ems_avg_response_seconds")
    if ems is not None:
        ems_min = round(ems / 60, 1)
        quality = "fast" if ems < 360 else ("typical" if ems < 480 else "slow")
        lines.append(f"  - EMS average response: {ems_min} min ({quality} for NYC, citywide avg ~7 min)")

    violations_c = building.get("open_class_c_250m")
    if violations_c is not None:
        severity = "concerning" if violations_c > 10 else ("some" if violations_c > 3 else "few")
        lines.append(f"  - {violations_c} open Class C housing violations within 250m ({severity}; Class C = immediately hazardous)")

    trees = amenities.get("tree_count_500m")
    if trees is not None:
        density = "well-treed" if trees > 200 else ("moderate canopy" if trees > 80 else "sparse tree coverage")
        lines.append(f"  - {trees} street trees within 500m ({density})")

    restaurants = amenities.get("restaurant_count_500m")
    if restaurants is not None:
        density = "very high" if restaurants > 150 else ("good" if restaurants > 50 else "limited")
        lines.append(f"  - {restaurants} restaurants within 500m ({density} dining density)")

    parks = amenities.get("park_count_500m") or amenities.get("park_acres_zip")
    if parks is not None:
        lines.append(f"  - Park access: {parks}")

    subway = transit.get("subway_count_500m")
    if subway is not None:
        access = "excellent subway access" if subway > 3 else ("good access" if subway > 1 else ("one station nearby" if subway == 1 else "no subway stations nearby"))
        lines.append(f"  - {subway} subway entrance(s) within 500m ({access})")

    bus = transit.get("bus_stop_count_500m")
    if bus is not None:
        lines.append(f"  - {bus} bus stops within 500m")

    # Top concerns
    if actions:
        lines.append("")
        lines.append("Top issues to be aware of:")
        for a in actions[:3]:
            lines.append(f"  - {a['action']}")

    lines.append("")
    lines.append("Source: NYC Open Data (all data is local, verified, no estimates).")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_agent_available() -> dict:
    """Check if agent mode is available."""
    if not AGENT_ENABLED:
        return {"enabled": False, "reason": "disabled by config"}
    # Check if skill scripts exist
    scripts_ok = os.path.isfile(os.path.join(REPORT_SCRIPTS, "extract_segments.py"))
    # Check if NemoClaw CLI available (optional)
    nemoclaw_ok = False
    try:
        result = subprocess.run([NEMOCLAW_BIN, "--version"], capture_output=True, timeout=5)
        nemoclaw_ok = result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    try:
        from urban_dossier_analyst.tools import tool_availability

        raw_tools = tool_availability()
        # /api/agent/status is intentionally unauthenticated for the UI toggle.
        # Publish capability decisions, never absolute artifact paths.
        tools = {
            name: {
                key: value
                for key, value in state.items()
                if key in {"available", "reason", "release_gate", "interventions"}
            }
            for name, state in raw_tools.items()
        }
    except Exception as exc:  # noqa: BLE001 - status must remain available
        logger.warning("Could not resolve agent tool availability: %s", exc)
        tools = {}

    return {
        "enabled": True,
        "backend": AGENT_BACKEND,
        "transport": OPENCLAW_TRANSPORT,
        "agent_id": OPENCLAW_AGENT_ID,
        "scripts_available": scripts_ok,
        "nemoclaw_available": nemoclaw_ok,
        "model": os.environ.get("URBAN_DOSSIER_MODEL", "auto"),
        "tools": tools,
        "available_tools": [name for name, state in tools.items() if state.get("available")],
        "unavailable_tools": [name for name, state in tools.items() if not state.get("available")],
    }


def generate_report(payload: dict, focus: str | None = None) -> dict:
    """Generate a neighborhood report via skill scripts + LLM.

    Tries NemoClaw CLI first if configured, falls back to direct script execution + vllm.
    Returns dict with 'html', 'markdown', or 'error'.
    """
    payload_path = _tmp_path(".json")
    segments_path = _tmp_path("-segments.json")
    narratives_path = _tmp_path("-narratives.json")
    html_path = _tmp_path("-report.html")
    md_path = _tmp_path("-report.md")
    temps = [payload_path, segments_path, narratives_path, html_path, md_path]

    try:
        # Write payload to temp file
        with open(payload_path, "w") as f:
            json.dump(payload, f, cls=_SafeEncoder)

        # --- NemoClaw path (OpenClaw agent inside sandbox) ---
        if AGENT_BACKEND == "nemoclaw":
            result = _try_nemoclaw_report(payload, focus)
            if result is not None:
                return result
            logger.warning("NemoClaw/OpenClaw report failed, falling back to scripts path")

        # --- Scripts fallback path ---
        return _fallback_script_report(
            payload, payload_path, segments_path, narratives_path,
            html_path, md_path, focus,
        )
    except Exception as exc:
        logger.exception("generate_report failed")
        return {"error": str(exc)}
    finally:
        _cleanup_files(*temps)


def _decode_nemoclaw_payload(stdout: str) -> str | None:
    """Return the first text payload from current or legacy NemoClaw JSON.

    NemoClaw may print a gateway-selection status line before the JSON object.
    Scan for a decodable object instead of assuming stdout begins with ``{``.
    v0.0.100 nests agent output below ``result``; older builds returned
    ``payloads`` at the top level.
    """
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", stdout):
        try:
            data, _ = decoder.raw_decode(stdout[match.start():])
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        result = data.get("result", data)
        if not isinstance(result, dict):
            continue
        payloads = result.get("payloads", [])
        if not isinstance(payloads, list):
            continue
        for payload in payloads:
            if isinstance(payload, dict) and isinstance(payload.get("text"), str):
                return payload["text"]
    return None


def _read_openclaw_gateway_token() -> str | None:
    """Load the Gateway bearer token without ever logging it.

    Environment injection is convenient for containers.  A mode-0600 file is
    preferred on the workstation so the secret is not visible in ``ps`` or a
    systemd unit definition.
    """
    token = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "").strip()
    if token:
        return token
    try:
        with open(OPENCLAW_GATEWAY_TOKEN_FILE, encoding="utf-8") as token_file:
            return token_file.read().strip().strip('"') or None
    except OSError:
        return None


def _get_openclaw_gateway_client(*, refresh: bool = False):
    """Return one process-wide OpenAI client backed by a persistent pool."""
    global _openclaw_gateway_client
    with _openclaw_gateway_client_lock:
        if refresh:
            _openclaw_gateway_client = None
        if _openclaw_gateway_client is not None:
            return _openclaw_gateway_client
        token = _read_openclaw_gateway_token()
        if not token:
            return None
        from openai import OpenAI
        _openclaw_gateway_client = OpenAI(
            base_url=f"{OPENCLAW_GATEWAY_URL}/v1",
            api_key=token,
            timeout=LLM_CALL_TIMEOUT,
            max_retries=1,
        )
        return _openclaw_gateway_client


def _response_output_text(response) -> str | None:
    """Extract text from OpenResponses SDK objects and compatible test doubles."""
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            text = getattr(content, "text", None)
            if isinstance(text, str) and text.strip():
                return text.strip()
    return None


def _openclaw_gateway_agent(message: str, session_id: str) -> str | None:
    """Call the in-OpenShell Gateway without starting a CLI process per turn."""
    for attempt in range(2):
        client = _get_openclaw_gateway_client(refresh=attempt > 0)
        if client is None:
            logger.warning("OpenClaw Gateway token is unavailable; using CLI fallback")
            return None
        try:
            response = client.responses.create(
                # Encoding the agent in ``model`` is the least ambiguous route
                # supported by OpenResponses. Keep the header as an explicit
                # compatibility hint for older Gateway builds.
                model=f"openclaw/{OPENCLAW_AGENT_ID}",
                input=message,
                max_output_tokens=OPENCLAW_MAX_OUTPUT_TOKENS,
                extra_headers={
                    "x-openclaw-agent-id": OPENCLAW_AGENT_ID,
                    "x-openclaw-session-key": session_id,
                },
            )
            return _response_output_text(response)
        except Exception as exc:  # SDK exception types vary across versions
            status_code = getattr(exc, "status_code", None)
            if status_code == 401 and attempt == 0:
                continue
            logger.warning(
                "OpenClaw Gateway request failed (%s); using CLI fallback",
                exc.__class__.__name__,
            )
            return None
    return None


def _openclaw_agent(message: str, session_id: str = "blocksense",
                    timeout: int = 120) -> str | None:
    """Send a message to OpenClaw agent inside the NemoClaw sandbox.

    Returns the agent text response, or None on failure.
    Command chain: backend host → NemoClaw CLI → OpenShell → OpenClaw.
    """
    if OPENCLAW_TRANSPORT == "gateway":
        response = _openclaw_gateway_agent(message, session_id)
        if response:
            return response

    cmd = [
        NEMOCLAW_BIN, NEMOCLAW_SANDBOX, "agent",
        "--agent", OPENCLAW_AGENT_ID,
        "--session-id", session_id,
        "--thinking", "off",
        "-m", message,
        "--json",
    ]
    try:
        child_env = os.environ.copy()
        child_env["NO_COLOR"] = "1"
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 30,
            env=child_env,
        )
        if result.returncode != 0:
            logger.warning("OpenClaw agent returned rc=%d: %s", result.returncode, result.stderr[:500])
            return None
        return _decode_nemoclaw_payload(result.stdout)
    except subprocess.TimeoutExpired:
        logger.warning("OpenClaw agent timed out after %ds", timeout + 30)
        return None
    except (FileNotFoundError, OSError) as exc:
        logger.warning("OpenClaw agent call failed: %s", exc)
        return None


def _try_nemoclaw_report(payload: dict, focus: str | None) -> dict | None:
    """Generate report by asking OpenClaw agent to analyze data and write a report.

    The agent runs inside the NemoClaw sandbox with access to local Nemotron 30B.
    Returns dict with html/markdown, or None on failure.
    """
    # Build a condensed data summary for the agent (full payload is too large for prompt)
    context = _build_condensed_context(payload)
    focus_str = f" Focus especially on {focus}." if focus else ""

    prompt = (
        f"You are analyzing a NYC neighborhood. Here is the data:\n\n"
        f"{context}\n\n"
        f"Write a detailed neighborhood analysis report based ONLY on the data above.{focus_str} "
        f"Structure it with sections for Safety, Transit, Amenities, and Building. "
        f"Cite specific numbers from the data. Do not invent any statistics. "
        f"Use 2-3 sentences per section, then a synthesis paragraph."
    )

    session_id = f"report-{os.getpid()}"
    response = _openclaw_agent(prompt, session_id=session_id, timeout=45)

    if not response or len(response.strip()) < 50:
        return None

    # OpenClaw returns text, not HTML. Convert markdown and wrap.
    md = response.strip()
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>BlockSense Report</title>
<style>body{{font-family:system-ui;max-width:800px;margin:2em auto;padding:0 1em;color:#2a3439;line-height:1.6}}
h1{{color:#565e74}}h2{{color:#0053dc;border-bottom:1px solid #e8e6dc;padding-bottom:4px}}
ul{{padding-left:1.5em}}li{{margin-bottom:0.3em}}
</style></head><body>
<h1>BlockSense NYC — Neighborhood Report</h1>
{_md_to_html(md)}
<hr><small>Generated via OpenClaw agent | Nemotron 30B</small>
</body></html>"""

    return {"html": html, "markdown": md, "backend": "nemoclaw"}


def _resolve_dimension_narratives(narratives: dict, segment_list: list, segments_data: dict) -> None:
    """Resolve {{ref}} placeholders in each dimension narrative in place.

    Run before synthesis so the synthesis call sees real numbers instead of
    placeholders. No-op if the SymGen pipeline isn't importable.
    """
    _ensure_symgen_imports()
    if resolve_symgen is None:
        return
    overall_card = segments_data.get("overall_data_card", {})
    for dim in list(narratives.keys()):
        seg = next((s for s in segment_list if s.get("dimension") == dim), {})
        card = dict(seg.get("data_card", {}))
        card.update(overall_card)
        resolved, _ = resolve_symgen(narratives[dim], card)
        narratives[dim] = resolved


def _build_synth_prompt(narratives: dict, segments_data: dict, payload: dict) -> str:
    """Build the synthesis prompt body shared by report and refine flows.

    Combines all data_cards into a reference list, then concatenates the
    pre-resolved per-dimension narratives so the model can stitch them together.
    """
    segment_list = segments_data.get("segments", [])
    synth_card = dict(segments_data.get("overall_data_card", {}))
    for seg in segment_list:
        synth_card.update(seg.get("data_card", {}))

    synth_ref_lines = []
    for key, info in synth_card.items():
        if not isinstance(info, dict):
            continue
        ann = f' ({info["annotation"]})' if info.get("annotation") else ""
        synth_ref_lines.append(f"  {{{{{key}}}}} = {info.get('display', '?')}{ann}")
    synth_refs = "\n".join(synth_ref_lines) if synth_ref_lines else "  (no data available)"

    synth_parts = [f"[{dim.upper()}] {text}" for dim, text in narratives.items()]
    scores = payload.get("scores", {})
    score_str = ", ".join(f"{k}={v}" for k, v in scores.items() if v is not None)
    return (
        f"Location: {{{{location_name}}}}\nScores: {score_str}\n\n"
        f"Available references for synthesis:\n{synth_refs}\n\n"
        + "\n".join(synth_parts)
        + "\n\nCombine into a cohesive report using {{ref}} for numbers. "
        "Prose paragraphs, no bullets, no headers."
    )


def _render_or_fallback_md(
    narratives: dict,
    synthesis: str,
    location: str,
    segments_path: str,
    narratives_path: str,
    html_path: str,
    md_path: str,
    title_prefix: str,
    backend: str,
    extra: dict | None = None,
) -> dict:
    """Write narratives, run the render script, and return {html, markdown, backend}.

    Falls back to a markdown stitched from narratives if the render script
    fails or produces no output. `extra` merges into the success result so
    callers can add flags like `refined: True`.
    """
    try:
        with open(narratives_path, "w") as f:
            json.dump(narratives, f, cls=_SafeEncoder)
    except OSError as exc:
        return {"error": f"Failed to write narratives: {exc}"}

    render_script = os.path.join(REPORT_SCRIPTS, "render_report.py")
    render_args = [
        "--segments", segments_path,
        "--narratives", narratives_path,
        "--template", REPORT_TEMPLATE,
        "--output-html", html_path,
        "--output-md", md_path,
    ]
    ok, _ = _run_script(render_script, render_args)
    if not ok:
        md_fallback = f"# {title_prefix} for {location}\n\n{synthesis}\n\n"
        for dim, text in narratives.items():
            if dim != "synthesis":
                md_fallback += f"## {dim.title()}\n{text}\n\n"
        return {"html": "", "markdown": md_fallback, "backend": f"{backend}-fallback"}

    html = ""
    md = ""
    try:
        if os.path.isfile(html_path):
            with open(html_path) as f:
                html = f.read()
        if os.path.isfile(md_path):
            with open(md_path) as f:
                md = f.read()
    except OSError:
        pass

    if not html and not md:
        md = f"# {title_prefix} for {location}\n\n{synthesis}\n"
        for dim, text in narratives.items():
            if dim != "synthesis":
                md += f"\n## {dim.title()}\n{text}\n"

    result = {"html": html, "markdown": md, "backend": backend}
    if extra:
        result.update(extra)
    return result


def _apply_symgen_pipeline(narratives: dict, segments_data: dict) -> dict:
    """Apply SymGen resolve + verify to all narratives. Modifies and returns narratives."""
    _ensure_symgen_imports()
    if resolve_symgen is None or verify_narrative is None:
        logger.warning("SymGen pipeline unavailable, skipping resolve+verify")
        return narratives

    segment_list = segments_data.get("segments", [])
    overall_card = segments_data.get("overall_data_card", {})

    for dim in list(narratives.keys()):
        # Build the data_card for this dimension
        if dim == "synthesis":
            # Synthesis uses overall_data_card merged with all dimension cards
            card = dict(overall_card)
            for seg in segment_list:
                card.update(seg.get("data_card", {}))
        else:
            seg = next((s for s in segment_list if s.get("dimension") == dim), {})
            card = dict(seg.get("data_card", {}))
            # Also include overall card entries for cross-references
            card.update(overall_card)

        # Step A: Resolve {{ref}} placeholders
        resolved, stats = resolve_symgen(narratives[dim], card)
        narratives[dim] = resolved
        logger.info(
            "SymGen %s: %d refs resolved, %d unresolved",
            dim, stats.get("resolved", 0), stats.get("unresolved", 0),
        )

    # Step B: Verify remaining bare numbers
    for dim in list(narratives.keys()):
        cleaned, report = verify_narrative(narratives[dim], segments_data, strict=True)
        narratives[dim] = cleaned
        score = report.get("grounding_score", 0)
        if score < 1.0:
            logger.warning(
                "Narrative %s grounding: %.0f%% (%d ungrounded numbers)",
                dim, score * 100, report.get("ungrounded", 0),
            )

    return narratives


def _fallback_script_report(payload: dict, payload_path: str, segments_path: str,
                            narratives_path: str, html_path: str, md_path: str,
                            focus: str | None) -> dict:
    """Generate report using skill scripts directly + vllm HTTP + SymGen pipeline."""
    # Step 1: Extract segments
    extract_script = os.path.join(REPORT_SCRIPTS, "extract_segments.py")
    ok, err = _run_script(extract_script, [payload_path, "--output", segments_path])
    if not ok:
        return {"error": f"extract_segments failed: {err}"}

    # Read segments
    try:
        with open(segments_path) as f:
            segments_data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        return {"error": f"Failed to read segments: {exc}"}

    segment_list = segments_data.get("segments", [])

    # Step 2: LLM calls for each dimension using SymGen data-card prompts
    try:
        client = _get_openai_client()
        model_name = _resolve_model_name(client)
    except Exception as exc:
        return {"error": f"Failed to initialize LLM client: {exc}"}

    system_msg = (
        "You are a NYC neighborhood data analyst. Write concise analysis using ONLY "
        "the {{reference}} placeholders provided. Never write bare numbers."
    )

    narratives = {}
    for seg in segment_list:
        dim = seg.get("dimension", "")
        if not dim:
            continue
        prompt = _build_dimension_prompt(dim, seg, focus)
        text = _llm_chat(client, model_name, system_msg, prompt, max_tokens=200)
        narratives[dim] = text if text else f"Analysis for {dim} is not available."

    # Step 3: Synthesis — resolve dimension narratives first so synthesis sees real numbers
    _resolve_dimension_narratives(narratives, segment_list, segments_data)
    target = payload.get("target", {})
    location = target.get("matched_address") or target.get("borough", "NYC")
    synth_system = (
        "You are a NYC neighborhood data analyst. Combine dimension analyses into a "
        "cohesive 2-paragraph summary. Use ONLY {{reference}} placeholders for numbers. "
        "Lead with the most concerning findings. Use location name, not coordinates."
    )
    synth_prompt = _build_synth_prompt(narratives, segments_data, payload)
    synthesis = _llm_chat(client, model_name, synth_system, synth_prompt, max_tokens=500)
    narratives["synthesis"] = synthesis if synthesis else "Report synthesis unavailable."

    # Step 4: Apply full SymGen pipeline (resolve + verify) on all narratives
    narratives = _apply_symgen_pipeline(narratives, segments_data)

    # Step 5: Write narratives, render, and read outputs (with markdown fallback)
    return _render_or_fallback_md(
        narratives, synthesis, location,
        segments_path, narratives_path, html_path, md_path,
        title_prefix="Report", backend="scripts",
    )


def _try_nemoclaw_poster(payload: dict, template: str) -> dict | None:
    """Generate poster by asking OpenClaw agent. Returns dict or None."""
    context = _build_condensed_context(payload)
    target = payload.get("target", {})
    location = target.get("matched_address") or target.get("borough", "NYC")
    scores = payload.get("scores", {})
    overall = scores.get("overall", "?")

    prompt = (
        f"Create a community poster headline and summary for this NYC neighborhood:\n\n"
        f"{context}\n\n"
        f"1. Write a punchy headline under 15 words about {location} (score {overall}/100).\n"
        f"2. Write a summary under 50 words citing one key number.\n"
        f"Reply in this exact format:\n"
        f"HEADLINE: your headline here\n"
        f"SUMMARY: your summary here"
    )

    response = _openclaw_agent(prompt, session_id=f"poster-{os.getpid()}", timeout=30)
    if not response:
        return None

    # Parse headline and summary from response
    headline = f"{location}: Score {overall}/100"
    summary = f"Analysis of {location} based on NYC Open Data."
    for line in response.strip().splitlines():
        line = line.strip()
        if line.upper().startswith("HEADLINE:"):
            headline = line.split(":", 1)[1].strip().strip('"')
        elif line.upper().startswith("SUMMARY:"):
            summary = line.split(":", 1)[1].strip().strip('"')

    return {"headline": headline, "summary": summary}


def generate_poster(payload: dict, template: str = "offline") -> dict:
    """Generate a poster from the analysis payload.

    Tries OpenClaw agent for headline/summary, falls back to direct vllm.
    Returns dict with 'html' or 'error'.
    """
    payload_path = _tmp_path(".json")
    highlights_path = _tmp_path("-highlights.json")
    html_path = _tmp_path("-poster.html")
    temps = [payload_path, highlights_path, html_path]

    try:
        with open(payload_path, "w") as f:
            json.dump(payload, f, cls=_SafeEncoder)

        # Step 1: Extract highlights
        extract_script = os.path.join(POSTER_SCRIPTS, "extract_highlights.py")
        ok, err = _run_script(extract_script, [payload_path, "--output", highlights_path])
        if not ok:
            return {"error": f"extract_highlights failed: {err}"}

        # Step 2: LLM for headline + summary
        headline = None
        summary = None

        # --- NemoClaw path ---
        if AGENT_BACKEND == "nemoclaw":
            nc_result = _try_nemoclaw_poster(payload, template)
            if nc_result:
                headline = nc_result["headline"]
                summary = nc_result["summary"]
                logger.info("Poster headline/summary via OpenClaw agent")

        # --- Direct vllm fallback ---
        if not headline or not summary:
            if AGENT_BACKEND == "nemoclaw":
                logger.warning("OpenClaw poster failed, falling back to direct vllm")
            try:
                client = _get_openai_client()
                model_name = _resolve_model_name(client)
            except Exception as exc:
                return {"error": f"Failed to initialize LLM client: {exc}"}

            target = payload.get("target", {})
            location = target.get("matched_address") or target.get("borough", "NYC")
            scores = payload.get("scores", {})
            overall = scores.get("overall", "?")
            actions = payload.get("priority_actions", [])
            top_action = actions[0]["action"] if actions else "neighborhood overview"

            if not headline:
                headline_prompt = (
                    f"Location: {location}, overall score {overall}/100.\n"
                    f"Top issue: {top_action}.\n"
                    "Write a poster headline in under 15 words. Punchy, factual, no clickbait."
                )
                headline = _llm_chat(
                    client, model_name,
                    "You write short poster headlines for NYC neighborhood data.",
                    headline_prompt, max_tokens=30,
                )
                if not headline:
                    headline = f"{location}: Score {overall}/100"

            if not summary:
                summary_prompt = (
                    f"Location: {location}, score {overall}/100.\n"
                    f"Top issue: {top_action}.\n"
                    "Write a poster summary in under 50 words. Clear, factual, cite one number."
                )
                summary = _llm_chat(
                    client, model_name,
                    "You write short poster summaries for NYC neighborhood data.",
                    summary_prompt, max_tokens=80,
                )
                if not summary:
                    summary = f"Analysis of {location} based on NYC Open Data."

        # Step 3: Render poster
        render_script = os.path.join(POSTER_SCRIPTS, "render_poster.py")
        render_args = [
            "--highlights", highlights_path,
            "--headline", headline,
            "--summary", summary,
            "--template", template,
            "--output", html_path,
        ]
        ok, err = _run_script(render_script, render_args)
        if not ok:
            return {"error": f"render_poster failed: {err}"}

        html = ""
        try:
            if os.path.isfile(html_path):
                with open(html_path) as f:
                    html = f.read()
        except OSError:
            pass

        if not html:
            return {"error": "Poster render produced no output"}

        return {"html": html, "headline": headline, "summary": summary}
    except Exception as exc:
        logger.exception("generate_poster failed")
        return {"error": str(exc)}
    finally:
        _cleanup_files(*temps)


# chat_with_context stood here, backing the removed /api/agent/chat.
#
# Worth recording why its loss is a gain rather than a subtraction: after
# trying the OpenClaw sandbox it fell through to a direct vLLM client on
# any failure, unconditionally. /api/agent/ask was made fail-closed so a
# missing or misspelled transport setting cannot route around the sandbox;
# this function was never covered by that switch and would have kept a
# second, quieter way out. Removing the endpoint removed the path.


def refine_report(session, feedback: str) -> dict:
    """Re-generate a report with user feedback incorporated.

    Tries OpenClaw agent first if AGENT_BACKEND=nemoclaw, falls back to scripts path.
    Returns dict with 'html', 'markdown', or 'error'.
    """
    payload = session.analysis_payload

    # --- NemoClaw path ---
    if AGENT_BACKEND == "nemoclaw":
        context = _build_condensed_context(payload)
        prompt = (
            f"You previously analyzed this NYC neighborhood:\n\n"
            f"{context}\n\n"
            f"The user requests a refined report with this feedback: {feedback}\n\n"
            f"Write an updated neighborhood analysis report incorporating the feedback. "
            f"Structure: Safety, Transit, Amenities, Building, Synthesis. "
            f"Cite specific numbers from the data. Do not invent statistics."
        )
        session_id = f"refine-{os.getpid()}"
        response = _openclaw_agent(prompt, session_id=session_id, timeout=45)
        if response and len(response.strip()) >= 50:
            md = response.strip()
            html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>BlockSense Report (Refined)</title>
<style>body{{font-family:system-ui;max-width:800px;margin:2em auto;padding:0 1em;color:#2a3439;line-height:1.6}}
h1{{color:#565e74}}h2{{color:#0053dc;border-bottom:1px solid #e8e6dc;padding-bottom:4px}}
ul{{padding-left:1.5em}}li{{margin-bottom:0.3em}}
</style></head><body>
<h1>BlockSense NYC — Refined Report</h1>
<p><em>User feedback: {feedback}</em></p>
{_md_to_html(md)}
<hr><small>Generated via OpenClaw agent | Nemotron 30B</small>
</body></html>"""
            return {"html": html, "markdown": md, "backend": "nemoclaw"}
        logger.warning("OpenClaw refine failed, falling back to scripts path")

    # --- Scripts fallback path ---
    payload_path = _tmp_path(".json")
    segments_path = _tmp_path("-segments.json")
    narratives_path = _tmp_path("-narratives.json")
    html_path = _tmp_path("-report.html")
    md_path = _tmp_path("-report.md")
    temps = [payload_path, segments_path, narratives_path, html_path, md_path]

    try:
        with open(payload_path, "w") as f:
            json.dump(payload, f, cls=_SafeEncoder)

        # Extract segments
        extract_script = os.path.join(REPORT_SCRIPTS, "extract_segments.py")
        ok, err = _run_script(extract_script, [payload_path, "--output", segments_path])
        if not ok:
            return {"error": f"extract_segments failed: {err}"}

        try:
            with open(segments_path) as f:
                segments_data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            return {"error": f"Failed to read segments: {exc}"}

        segment_list = segments_data.get("segments", [])

        # LLM calls with feedback context, using SymGen data-card prompts
        try:
            client = _get_openai_client()
            model_name = _resolve_model_name(client)
        except Exception as exc:
            return {"error": f"Failed to initialize LLM client: {exc}"}

        # Include previous report context if available
        previous_report = ""
        if session.generated_reports:
            previous_report = f"\nPrevious report was generated. User feedback: {feedback}"
        else:
            previous_report = f"\nUser requested focus: {feedback}"

        system_msg = (
            "You are a NYC neighborhood data analyst. Write concise analysis using ONLY "
            "the {{reference}} placeholders provided. Never write bare numbers."
            f"{previous_report}"
        )

        narratives = {}
        for seg in segment_list:
            dim = seg.get("dimension", "")
            if not dim:
                continue
            prompt = _build_dimension_prompt(dim, seg, None)
            prompt += f"\n\nUser feedback to incorporate: {feedback}"
            text = _llm_chat(client, model_name, system_msg, prompt, max_tokens=200)
            narratives[dim] = text if text else f"Analysis for {dim} is not available."

        # Resolve dimension narratives before synthesis sees them
        _resolve_dimension_narratives(narratives, segment_list, segments_data)

        target = payload.get("target", {})
        location = target.get("matched_address") or target.get("borough", "NYC")

        # Synthesis with feedback, using SymGen
        synth_system = (
            "You are a NYC neighborhood data analyst. Combine dimension analyses into a "
            "cohesive 2-paragraph summary. Use ONLY {{reference}} placeholders for numbers. "
            "Lead with the most concerning findings. Use location name, not coordinates. "
            f"Incorporate this user feedback: {feedback}"
        )
        synth_prompt = _build_synth_prompt(narratives, segments_data, payload)
        synthesis = _llm_chat(client, model_name, synth_system, synth_prompt, max_tokens=500)
        narratives["synthesis"] = synthesis if synthesis else "Report synthesis unavailable."

        # Apply full SymGen pipeline (resolve + verify) on all narratives
        narratives = _apply_symgen_pipeline(narratives, segments_data)

        # Write narratives, render, and read outputs (with markdown fallback)
        return _render_or_fallback_md(
            narratives, synthesis, location,
            segments_path, narratives_path, html_path, md_path,
            title_prefix="Refined Report", backend="scripts", extra={"refined": True},
        )
    except Exception as exc:
        logger.exception("refine_report failed")
        return {"error": str(exc)}
    finally:
        _cleanup_files(*temps)
