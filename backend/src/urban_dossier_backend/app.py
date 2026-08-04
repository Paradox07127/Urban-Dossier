from __future__ import annotations

import hmac
import logging
import os
import sys
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import ALLOWED_CORS_ORIGINS, DEMO_TOKEN, DEMO_TOKEN_HEADER
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Cross-skill import path injection
#
# The v2 agent loop lives outside the backend package at
# ``Urban-Dossier/skills/urban_dossier_analyst/`` (underscores so it loads as a
# real Python package — the directory used to be hyphenated which broke
# relative imports inside the skill modules).
#
# We add the *parent* of the skill onto ``sys.path`` so we can import it as a
# package (``from urban_dossier_analyst.agent_loop import run_agent``). This
# preserves the relative imports inside the skill (``from .schemas import``).
# ---------------------------------------------------------------------------
SKILLS_ROOT = Path(__file__).resolve().parents[3] / "skills"
if str(SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILLS_ROOT))
SKILL_PATH = SKILLS_ROOT / "urban_dossier_analyst"

logger = logging.getLogger(__name__)
from .schemas import DetailPreviewRequest, DetailRequest, OverviewRequest, WatchlistRequest
from .service import (
    analyze_point,
    get_categories_payload,
    get_coverage_payload,
    get_health_payload,
    get_overview_payload,
    preview_point,
    run_watchlist,
    search_address_payload,
)


app = FastAPI(title="Urban Dossier Backend", version="3.7.8")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_CORS_ORIGINS or ["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:3456", "http://127.0.0.1:3456"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Urban-Dossier-Token"],
)


@app.middleware("http")
async def require_demo_token(request: Request, call_next):
    if not DEMO_TOKEN:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response
    if request.url.path in ("/api/health", "/api/agent/status"):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response
    provided = request.headers.get(DEMO_TOKEN_HEADER, "") or request.headers.get("x-urban_dossier-token", "")
    if not hmac.compare_digest(provided.encode(), DEMO_TOKEN.encode()):
        return JSONResponse(status_code=401, content={"detail": "Missing or invalid Urban Dossier demo token"})
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/health")
def health() -> dict:
    return get_health_payload()


@app.get("/api/categories")
def categories() -> dict:
    return get_categories_payload()


@app.get("/api/coverage")
def coverage() -> dict:
    return get_coverage_payload()


@app.post("/api/overview")
def overview(request: OverviewRequest) -> dict:
    viewport = request.viewport.model_dump() if request.viewport else None
    return get_overview_payload(
        view_mode=request.view_mode,
        category_id=request.category_id,
        viewport=viewport,
        zoom=request.zoom,
    )


@app.post("/api/detail/preview")
def detail_preview(request: DetailPreviewRequest) -> dict:
    return preview_point(
        latitude=request.latitude,
        longitude=request.longitude,
        radius_m=request.radius_m,
        priority_order=request.priority_order,
        time_window_days=request.time_window_days,
    )


@app.post("/api/analyze-point")
def analyze(request: DetailRequest) -> dict:
    return analyze_point(
        latitude=request.latitude,
        longitude=request.longitude,
        radius_m=request.radius_m,
        priority_order=request.priority_order,
        time_window_days=request.time_window_days,
        report_mode=request.report_mode,
    )


class SearchRequest(BaseModel):
    query: str
    limit: int = 5

@app.post("/api/search")
def search_address(request: SearchRequest) -> dict:
    """Search PLUTO / location index for addresses matching the query.

    Delegates to ``service.search_address_payload`` so the in-process agent
    dispatcher can reuse the same code path without round-tripping HTTP.
    """
    return search_address_payload(query=request.query, limit=request.limit)


@app.post("/api/watchlist/run")
def watchlist(request: WatchlistRequest) -> dict:
    return run_watchlist(
        seeds=request.seeds,
        priority_order=request.priority_order,
        radius_m=request.radius_m,
        time_window_days=request.time_window_days,
    )


# ---------------------------------------------------------------------------
# Agent endpoints
# ---------------------------------------------------------------------------
from .agent_schemas import (
    AgentChatRequest,
    AgentPosterRequest,
    AgentRefineRequest,
    AgentReportRequest,
    AgentSessionRequest,
)
from .agent_service import (
    chat_with_context,
    generate_poster,
    generate_report,
    is_agent_available,
    refine_report,
)
from .agent_session import store
from .service import _build_detail_payload


@app.get("/api/agent/status")
def agent_status() -> dict:
    return is_agent_available()


@app.post("/api/agent/session")
def agent_create_session(request: AgentSessionRequest) -> dict:
    payload = _build_detail_payload(
        latitude=request.latitude,
        longitude=request.longitude,
        radius_m=request.radius_m,
        priority_order=request.priority_order,
        time_window_days=request.time_window_days,
    )
    session_id = store.create(payload)
    return {
        "session_id": session_id,
        "scores": payload.get("scores", {}),
        "location": payload.get("target", {}),
    }


@app.post("/api/agent/report")
def agent_report(request: AgentReportRequest) -> dict:
    session = store.get(request.session_id)
    if not session:
        return JSONResponse(status_code=404, content={"detail": "Session not found"})
    result = generate_report(session.analysis_payload, focus=request.focus)
    if result.get("html"):
        session.add_report(result["html"])
    return result


@app.post("/api/agent/poster")
def agent_poster(request: AgentPosterRequest) -> dict:
    session = store.get(request.session_id)
    if not session:
        return JSONResponse(status_code=404, content={"detail": "Session not found"})
    return generate_poster(session.analysis_payload, template=request.template)


@app.post("/api/agent/chat")
def agent_chat(request: AgentChatRequest) -> dict:
    session = store.get(request.session_id)
    if not session:
        return JSONResponse(status_code=404, content={"detail": "Session not found"})
    response = chat_with_context(session, request.message)
    return {"response": response, "session_id": request.session_id}


@app.post("/api/agent/refine")
def agent_refine(request: AgentRefineRequest) -> dict:
    session = store.get(request.session_id)
    if not session:
        return JSONResponse(status_code=404, content={"detail": "Session not found"})
    result = refine_report(session, request.feedback)
    if result.get("html"):
        session.add_report(result["html"])
    return result


# ---------------------------------------------------------------------------
# v2 agent loop endpoint -- wires the urban-dossier-analyst skill into FastAPI.
#
# The agent loop module is owned by a parallel agent and lives at
# ``Urban-Dossier/skills/urban-dossier-analyst/agent_loop.py``. We import it
# lazily inside the request handler so:
#   * import errors do not kill the FastAPI process at startup, and
#   * the parallel agent can land its module on its own schedule.
# ---------------------------------------------------------------------------


class AskRequest(BaseModel):
    """Request body for ``POST /api/agent/ask``."""

    message: str = Field(min_length=1, max_length=4000)
    history: list[dict] | None = None  # OpenAI-style messages [{role, content}]
    max_iterations: int = Field(default=8, ge=1, le=32)
    session_id: str | None = None  # if provided, reuse existing AgentSession history


class AskResponse(BaseModel):
    """Response body for ``POST /api/agent/ask``."""

    answer: str
    evidence: list[dict]
    tools_called: list[dict]
    iterations: int
    trace: list[dict]
    session_id: str


def _normalize_tools_called(raw: object) -> list[dict]:
    """Adapt the skill's ``list[str]`` tool log to this API's ``list[dict]``.

    ``schemas.AgentResponse.tools_called`` is a locked cross-agent contract and
    yields bare tool names in dispatch order. ``AskResponse`` publishes richer
    objects so callers can gain fields without another breaking change, so the
    conversion happens here at the boundary instead of in the skill.

    Accepts both shapes: a plain name becomes ``{"name": ...}``, a dict is
    passed through. Anything else is stringified rather than dropped, so a
    future skill change degrades into data instead of a 500.
    """

    if not raw:
        return []
    normalized: list[dict] = []
    for entry in raw if isinstance(raw, (list, tuple)) else [raw]:
        if isinstance(entry, dict):
            normalized.append(entry)
        else:
            normalized.append({"name": str(entry)})
    return normalized


@app.post("/api/agent/ask", response_model=AskResponse)
async def ask(request: AskRequest) -> AskResponse | JSONResponse:
    """Run the v2 analyst agent loop and return a structured answer.

    The DEMO_TOKEN middleware applies automatically; this handler does not
    bypass it. Latency is logged in milliseconds for every call.
    """
    start_ms = time.perf_counter()

    # Resolve session: reuse existing AgentSession history if session_id given,
    # else create a thin placeholder session so callers always get a session_id
    # back. We do NOT reach into _build_detail_payload here -- the agent loop
    # is responsible for fetching whatever analytical context it needs.
    session = None
    history: list[dict] = []
    if request.session_id:
        session = store.get(request.session_id)
        if session is None:
            return JSONResponse(status_code=404, content={"detail": "Session not found"})
        history = list(session.chat_history)
    if request.history:
        history.extend(request.history)

    if request.session_id:
        session_id = request.session_id
    else:
        session_id = store.create({"mode": "agent_ask"})

    # Lazy import so a missing or in-flight skill module does not crash startup.
    # Imported as a package (urban_dossier_analyst) so the relative imports
    # inside the skill's modules (`from .schemas import ...`) resolve correctly.
    try:
        from urban_dossier_analyst.agent_loop import run_agent  # type: ignore[import-not-found]
    except ImportError as exc:
        logger.error("agent_loop import failed (skill not yet available?): %s", exc)
        return JSONResponse(
            status_code=503,
            content={
                "detail": "Agent loop module unavailable",
                "error": str(exc),
                "skill_path": str(SKILL_PATH),
            },
        )

    # ``run_agent`` is a *stateless* pure function: it takes the conversation it
    # should reason over and returns a structured result. Session ownership
    # stays here, in FastAPI -- we load ``history`` from the AgentSession above
    # and persist the new turn below. Do not add ``session_id`` to the skill's
    # signature; the skill deliberately knows nothing about our session store.
    #
    # The endpoint/model come from the same env vars the rest of the backend
    # uses (see deploy/backend.env.example) so /api/agent/ask cannot silently
    # talk to a different vLLM than the one the deployment configured.
    try:
        result = run_agent(
            user_message=request.message,
            history=history,
            max_iterations=request.max_iterations,
            vllm_base_url=os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:8000/v1"),
            model=os.environ.get(
                "URBAN_DOSSIER_MODEL", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4"
            ),
        )
    except Exception as exc:  # noqa: BLE001 -- structured error surface for clients
        elapsed_ms = (time.perf_counter() - start_ms) * 1000.0
        logger.exception("run_agent failed after %.1fms: %s", elapsed_ms, exc)
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Agent loop execution failed",
                "error": str(exc),
                "error_type": exc.__class__.__name__,
                "latency_ms": round(elapsed_ms, 1),
                "session_id": session_id,
            },
        )

    elapsed_ms = (time.perf_counter() - start_ms) * 1000.0
    logger.info(
        "agent_ask session=%s iterations=%s latency_ms=%.1f",
        session_id,
        (result or {}).get("iterations"),
        elapsed_ms,
    )

    # Persist the exchange in the session so subsequent /api/agent/ask calls
    # with the same session_id pick up the new turn.
    if session is not None:
        session.add_chat("user", request.message)
        answer_text = (result or {}).get("answer", "")
        if answer_text:
            session.add_chat("assistant", answer_text)

    payload = result if isinstance(result, dict) else {}
    return AskResponse(
        answer=str(payload.get("answer", "")),
        evidence=list(payload.get("evidence", []) or []),
        tools_called=_normalize_tools_called(payload.get("tools_called")),
        iterations=int(payload.get("iterations", 0) or 0),
        trace=list(payload.get("trace", []) or []),
        session_id=session_id,
    )
