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
from .metrics import METRICS_BY_ID, metric_to_dict, registry_to_dict
from .presentation import bivariate_geojson, presentation_contract
from .schemas import DetailPreviewRequest, DetailRequest, OverviewRequest, WatchlistRequest
from .service import (
    analyze_point,
    compare_points,
    query_dataset_rows,
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


@app.get("/api/land-outline")
def land_outline() -> dict:
    """The city's landmass as one polygon, for the 3D view's base slab.

    Reuses the same coastline the overview cells are clipped against, so the
    slab the buildings stand on and the edge the choropleth stops at are the
    same line rather than two shapes that nearly agree.
    """
    from shapely.geometry import mapping

    from .providers.direct_provider import DirectQueryDataProvider

    land = DirectQueryDataProvider._land_mask()
    if land is None:
        return JSONResponse(
            status_code=503,
            content={"detail": "Land boundary unavailable", "available": False},
        )
    # Coarser than the 11 m used for clipping: the slab is seen edge-on from a
    # distance, where a metre of shoreline detail costs payload and shows
    # nothing.
    simplified = land.simplify(0.0004, preserve_topology=True)
    return {
        "type": "Feature",
        "properties": {"name": "New York City"},
        "geometry": mapping(simplified),
    }


@app.get("/api/categories")
def categories() -> dict:
    return get_categories_payload()


@app.get("/api/coverage")
def coverage() -> dict:
    return get_coverage_payload()


# The methodology behind every number, addressable by metric id.
#
# `/api/categories` answers "what are the groupings and their weights"; this
# answers "what is this particular score, in what unit, measured at what
# geography, which way is good, and by which version of the method". Serving it
# from the registry rather than a document is what keeps a published
# methodology page from drifting away from the code that scores.
@app.get("/api/metrics")
def metrics() -> dict:
    return registry_to_dict()


@app.get("/api/metrics/{metric_id}")
def metric_detail(metric_id: str) -> dict:
    definition = METRICS_BY_ID.get(metric_id)
    if definition is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"Unknown metric '{metric_id}'", "known": sorted(METRICS_BY_ID)},
        )
    return metric_to_dict(definition)


@app.get("/api/presentation/classes")
def presentation_classes(
    x_category: str = "safety",
    y_category: str = "transit",
) -> dict:
    try:
        return presentation_contract(x_category, y_category)
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.get("/api/presentation/bivariate")
def presentation_bivariate(
    x_category: str = "safety",
    y_category: str = "transit",
) -> dict:
    try:
        return bivariate_geojson(x_category, y_category)
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})


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
    AgentPosterRequest,
    AgentRefineRequest,
    AgentReportRequest,
    AgentSessionRequest,
)
from .agent_service import (
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


# /api/agent/chat stood here: a second way into the same agent, taking a bare
# message and returning a bare string, beside /api/agent/ask's structured
# request with trace and evidence. Maintaining both was the thing PROJECT_PLAN
# P0-01 set out to stop, and the frontend had already moved off it. Removed
# rather than deprecated, because an unused endpoint that still works is an
# endpoint someone will wire up again.


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


# ---------------------------------------------------------------------------
# Agent tool endpoints
#
# These back the urban-dossier-analyst tools that previously raised
# NotImplementedError. Request/response shapes follow the contracts the tool
# layer already documented, so tools.py needs no translation layer.
# ---------------------------------------------------------------------------


class PointModel(BaseModel):
    latitude: float = Field(ge=40.4, le=40.95)
    longitude: float = Field(ge=-74.3, le=-73.7)


class ComparePointsRequest(BaseModel):
    point_a: PointModel
    point_b: PointModel
    radius_m: int = Field(default=500, ge=50, le=2000)
    priority_order: list[str] | None = None
    time_window_days: int = Field(default=365, ge=1, le=3650)


class DatasetQueryRequest(BaseModel):
    dataset_id: str = Field(min_length=2, max_length=64)
    filters: dict = Field(default_factory=dict)
    limit: int = Field(default=100, ge=1, le=1000)


@app.post("/api/compare-points")
def compare_points_endpoint(request: ComparePointsRequest) -> dict:
    return compare_points(
        point_a=request.point_a.model_dump(),
        point_b=request.point_b.model_dump(),
        radius_m=request.radius_m,
        priority_order=request.priority_order,
        time_window_days=request.time_window_days,
    )


@app.post("/api/dataset/query")
def dataset_query_endpoint(request: DatasetQueryRequest) -> dict:
    return query_dataset_rows(
        dataset_id=request.dataset_id,
        filters=request.filters,
        limit=request.limit,
    )


class IsochroneRequest(BaseModel):
    latitude: float = Field(ge=40.4, le=40.95)
    longitude: float = Field(ge=-74.3, le=-73.7)
    minutes: int = Field(default=10, ge=1, le=60)
    mode: str = Field(default="walk")


class SimulateRequest(BaseModel):
    latitude: float = Field(ge=40.4, le=40.95)
    longitude: float = Field(ge=-74.3, le=-73.7)
    intervention_type: str
    count: int = Field(default=1, ge=1, le=20)
    radius_m: int = Field(default=500, ge=50, le=2000)


@app.post("/api/isochrone")
def isochrone_endpoint(request: IsochroneRequest) -> dict:
    from .scenarios import walking_isochrone

    if request.mode != "walk":
        return JSONResponse(
            status_code=400,
            content={"detail": f"Unsupported mode '{request.mode}'; only 'walk' is built."},
        )
    result = walking_isochrone(
        latitude=request.latitude,
        longitude=request.longitude,
        minutes=request.minutes,
    )
    if result.get("error"):
        return JSONResponse(status_code=503, content=result)
    return result


@app.post("/api/simulate")
def simulate_endpoint(request: SimulateRequest) -> dict:
    from .scenarios import simulate_intervention

    result = simulate_intervention(
        latitude=request.latitude,
        longitude=request.longitude,
        intervention_type=request.intervention_type,
        count=request.count,
        radius_m=request.radius_m,
    )
    if result.get("error"):
        return JSONResponse(status_code=503, content=result)
    return result


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
        from urban_dossier_analyst.gateway import gateway_client_factory
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
    # Transport: agent traffic goes through the authenticated OpenClaw Gateway
    # inside OpenShell, which is the deployment's policy/network boundary. The
    # direct-to-vLLM path this endpoint used to take bypassed that boundary.
    # Setting URBAN_DOSSIER_ASK_TRANSPORT=vllm restores it for local debugging
    # on a host with no sandbox; it is not a supported production setting.
    #
    # The test is written against the bypass value rather than the sandboxed one
    # so that it fails closed: an unset, misspelled or empty setting keeps
    # traffic inside the boundary, and only a deliberate, correctly spelled
    # opt-out leaves it. Matching on "gateway" instead would turn any typo into
    # a silent bypass of the very boundary this endpoint exists to enforce.
    transport = os.environ.get("URBAN_DOSSIER_ASK_TRANSPORT", "gateway").strip().lower()
    client_factory = None
    if transport != "vllm":
        # One Gateway session per ask-session keeps server-side conversation
        # state aligned with our AgentSession.
        client_factory = gateway_client_factory(session_key=f"ask-{session_id[:12]}")

    try:
        result = run_agent(
            user_message=request.message,
            history=history,
            max_iterations=request.max_iterations,
            vllm_base_url=os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:8000/v1"),
            model=os.environ.get(
                "URBAN_DOSSIER_MODEL", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4"
            ),
            client_factory=client_factory,
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
