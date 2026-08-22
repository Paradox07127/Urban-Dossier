"""Agent service: orchestrates OpenClaw skills for deep analysis."""

from __future__ import annotations
import datetime
import html as html_lib
import json
import os
import subprocess
import sys
import tempfile
import logging
import re
import threading


def _md_to_html(text: str) -> str:
    """Render a small safe Markdown subset, escaping all raw HTML first."""
    lines = text.split("\n")
    out: list[str] = []
    in_ul = False
    for line in lines:
        stripped = html_lib.escape(line.strip(), quote=False)
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
from .report import _strip_thinking

logger = logging.getLogger(__name__)


# Paths to blocksense skill scripts
_REPO_SKILLS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
    "skills",
)
SKILL_BASE = os.environ.get("BLOCKSENSE_SKILL_PATH", _REPO_SKILLS)
POSTER_SCRIPTS = os.path.join(SKILL_BASE, "blocksense-poster", "scripts")
POSTER_TEMPLATES_DIR = os.path.join(SKILL_BASE, "blocksense-poster", "templates")

# This module has no OpenAI/vLLM client. Every model call goes through the
# OpenClaw gateway into the sandbox; there is no second code path to audit,
# which is a stronger guarantee than a mode flag that could be flipped.
#
# Agent backend mode. Only "nemoclaw" remains: reports and posters run
# through the OpenClaw sandbox. The "scripts" mode, which reached host
# vLLM directly, was removed on 2026-08-22 with the SymGen path it served.
AGENT_BACKEND = os.environ.get("URBAN_DOSSIER_AGENT_BACKEND", "nemoclaw")

# How an artifact's numbers were grounded, stamped on every report and poster.
#
# The audit called the missing SymGen resolver a release blocker because the
# product claimed deterministic numeric verification it did not perform. The
# resolver is gone (2026-08-22); what replaces it is not a weaker claim but an
# explicit one. A reader of a generated artifact could not previously tell a
# verified number from an unverified one, and neither could a caller: nothing
# in the payload said either way. Now everything says so, honestly, and a
# future grounding implementation has a field to set rather than a silence to
# break.
GROUNDING_NONE = {
    "verified": False,
    "method": "none",
    "note": (
        "Numbers come from the evidence supplied to the model and are not "
        "independently re-verified after generation."
    ),
}
GROUNDING_NOTICE_HTML = (
    "<hr><small>Generated via OpenClaw agent | Nemotron 30B<br>"
    "Figures are taken from the supplied evidence and are not independently "
    "re-verified after generation.</small>"
)

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
    # Poster rendering still shells out; report generation no longer does.
    scripts_ok = os.path.isfile(os.path.join(POSTER_SCRIPTS, "render_poster.py"))
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
    """Generate a neighborhood report through the OpenClaw sandbox.

    There is one backend. The direct-scripts path was removed on 2026-08-22:
    it was already unreachable in the shipped configuration, and when it did
    run it emitted literal ``{{placeholder}}`` text -- its prompt mandated
    ``{{ref}}`` syntax for every number and the resolver meant to substitute
    them has never existed in this repository.

    Returns dict with 'html', 'markdown', 'grounding', or 'error'.
    """
    if AGENT_BACKEND != "nemoclaw":
        return {
            "error": (
                f"Unsupported agent backend: {AGENT_BACKEND!r}. "
                "Only 'nemoclaw' remains."
            ),
            "error_code": "unsupported_agent_backend",
            "backend": AGENT_BACKEND,
        }
    try:
        result = _try_nemoclaw_report(payload, focus)
        if result is not None:
            return result
        logger.warning("OpenClaw report generation failed")
        return {
            "error": "OpenClaw report generation failed",
            "error_code": "openclaw_unavailable",
            "backend": "nemoclaw",
        }
    except Exception as exc:
        logger.exception("generate_report failed")
        return {"error": str(exc), "backend": AGENT_BACKEND}


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
{GROUNDING_NOTICE_HTML}
</body></html>"""

    return {
        "html": html,
        "markdown": md,
        "backend": "nemoclaw",
        "grounding": GROUNDING_NONE,
    }


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
    """Generate a poster through the OpenClaw sandbox.

    Highlight extraction and rendering still shell out to the poster skill --
    those run in both modes and are production dependencies, not leftovers.
    Only the headline/summary generation had a second path, and that one
    reached host vLLM directly; it went with the scripts mode on 2026-08-22.

    Returns dict with 'html', 'headline', 'summary', 'grounding', or 'error'.
    """
    if AGENT_BACKEND != "nemoclaw":
        return {
            "error": (
                f"Unsupported agent backend: {AGENT_BACKEND!r}. "
                "Only 'nemoclaw' remains."
            ),
            "error_code": "unsupported_agent_backend",
            "backend": AGENT_BACKEND,
        }
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

        # Step 2: headline + summary from the sandboxed agent
        nc_result = _try_nemoclaw_poster(payload, template)
        if not nc_result:
            logger.warning("OpenClaw poster generation failed")
            return {
                "error": "OpenClaw poster generation failed",
                "error_code": "openclaw_unavailable",
                "backend": "nemoclaw",
            }
        headline = nc_result["headline"]
        summary = nc_result["summary"]

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

        return {
            "html": html,
            "headline": headline,
            "summary": summary,
            "backend": "nemoclaw",
            "grounding": GROUNDING_NONE,
        }
    except Exception as exc:
        logger.exception("generate_poster failed")
        return {"error": str(exc), "backend": AGENT_BACKEND}
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

    One backend, same as ``generate_report``.
    Returns dict with 'html', 'markdown', 'grounding', or 'error'.
    """
    payload = session.analysis_payload

    if AGENT_BACKEND != "nemoclaw":
        return {
            "error": (
                f"Unsupported agent backend: {AGENT_BACKEND!r}. "
                "Only 'nemoclaw' remains."
            ),
            "error_code": "unsupported_agent_backend",
            "backend": AGENT_BACKEND,
        }

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
<p><em>User feedback: {html_lib.escape(feedback)}</em></p>
{_md_to_html(md)}
{GROUNDING_NOTICE_HTML}
</body></html>"""
        return {
            "html": html,
            "markdown": md,
            "backend": "nemoclaw",
            "refined": True,
            "grounding": GROUNDING_NONE,
        }
    logger.warning("OpenClaw refine failed")
    return {
        "error": "OpenClaw report refinement failed",
        "error_code": "openclaw_unavailable",
        "backend": "nemoclaw",
    }
