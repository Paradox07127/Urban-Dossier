"""Tool layer for the urban-dossier-analyst agent.

Exposes:
  - TOOLS:        list[dict] of 8 OpenAI-compatible tool schemas
  - dispatch_tool(name, args) -> dict: argument validation + execution wrapper

Hard contract (other agents depend on this surface):
  - Exactly 8 tool entries in TOOLS, names locked.
  - Every tool has a Pydantic args model.
  - dispatch_tool NEVER raises - errors are surfaced as
    {"error": str, "retry_hint": str} so the agent loop can feed them back to
    the LLM as observations.

Dispatch modes for tools 1, 3, 4, 7:
  - In-process Python (preferred): when ``urban_dossier_backend.service`` is
    importable (true when ``agent_loop.run_agent`` runs inside the FastAPI
    process), the tool calls the underlying service function directly. This
    avoids ~10-30 ms HTTP loopback per call - over 8 ReAct iterations and a
    handful of tool calls per turn this saves 100-300 ms per agent run.
  - HTTP loopback (sandbox fallback): when the backend module is NOT
    importable (e.g. inside a NemoClaw sandbox where only the skill ships),
    fall back to ``httpx`` against ``http://localhost:8090/api/...`` with a
    30s timeout and 2 retries. Same path used in the v1 implementation.

The eight schemas are stable, but each request receives only the subset whose
release artifacts pass validation. Optional RAG imports remain lazy so this
module can load without that stack.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import logging
import os
import time
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

from .schemas import NYC_LAT_MAX, NYC_LAT_MIN, NYC_LON_MAX, NYC_LON_MIN, Point


logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Backend dispatch: in-process Python first, HTTP loopback as sandbox fallback
# --------------------------------------------------------------------------- #

BACKEND_BASE_URL: str = os.environ.get(
    "URBAN_DOSSIER_BACKEND_URL",
    "http://localhost:8090",
).rstrip("/")
DEMO_TOKEN_HEADER: str = "X-Urban-Dossier-Token"
DEMO_TOKEN: str = os.environ.get("URBAN_DOSSIER_DEMO_TOKEN", "")
HTTP_TIMEOUT_SECONDS: float = 30.0
HTTP_RETRIES: int = 2

# Cached on first import attempt. ``None`` = not probed yet, ``True`` = the
# backend service module is importable in this process, ``False`` = it is not
# (we will fall back to HTTP loopback for tools 1/3/4/7).
_BACKEND_IN_PROCESS: bool | None = None
_BACKEND_SERVICE: ModuleType | None = None
_DISPATCH_MODE_LOGGED: bool = False


def _resolve_backend_module() -> ModuleType | None:
    """Probe whether ``urban_dossier_backend.service`` is importable.

    Result is cached for the lifetime of the process. Safe to call from any
    tool implementation - it does NOT raise on ImportError.
    """

    global _BACKEND_IN_PROCESS, _BACKEND_SERVICE
    if _BACKEND_IN_PROCESS is not None:
        return _BACKEND_SERVICE
    try:
        from urban_dossier_backend import service as backend_service  # type: ignore[import-not-found]
    except ImportError:
        _BACKEND_IN_PROCESS = False
        _BACKEND_SERVICE = None
        return None
    _BACKEND_IN_PROCESS = True
    _BACKEND_SERVICE = backend_service
    return backend_service


def _log_dispatch_mode_once() -> None:
    """Emit a single INFO log line on the first tool call describing the mode."""

    global _DISPATCH_MODE_LOGGED
    if _DISPATCH_MODE_LOGGED:
        return
    _DISPATCH_MODE_LOGGED = True
    module = _resolve_backend_module()
    if module is not None:
        logger.info(
            "urban_dossier_analyst tools: dispatching IN-PROCESS via "
            "urban_dossier_backend.service (HTTP loopback bypassed)."
        )
    else:
        logger.info(
            "urban_dossier_analyst tools: dispatching via HTTP loopback to %s "
            "(urban_dossier_backend not importable in this process).",
            BACKEND_BASE_URL,
        )


def _backend_client() -> httpx.Client:
    """Construct the shared httpx.Client with retry transport.

    httpx.HTTPTransport supports `retries=N` for transport-level retries on
    connection errors. Status-code retries are applied per-call by callers
    that need them.
    """

    transport = httpx.HTTPTransport(retries=HTTP_RETRIES)
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if DEMO_TOKEN:
        headers[DEMO_TOKEN_HEADER] = DEMO_TOKEN
    return httpx.Client(
        base_url=BACKEND_BASE_URL,
        timeout=HTTP_TIMEOUT_SECONDS,
        headers=headers,
        transport=transport,
    )


def _backend_post(path: str, json_body: dict[str, Any]) -> dict[str, Any]:
    """POST helper used as the HTTP-loopback fallback for tools 1, 3, 4, 7.

    Only invoked when the in-process import is unavailable. Raises
    ConnectionError / RuntimeError on failure - dispatch_tool catches these
    and converts them to {"error": ..., "retry_hint": ...}.
    """

    with _backend_client() as client:
        try:
            resp = client.post(path, json=json_body)
        except httpx.HTTPError as exc:
            raise ConnectionError(
                f"Backend unreachable at {BACKEND_BASE_URL}{path}: {exc}"
            ) from exc
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Backend {path} returned HTTP {resp.status_code}: "
                f"{resp.text[:300]}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise RuntimeError(f"Backend {path} returned non-JSON body") from exc


# --------------------------------------------------------------------------- #
# Pydantic argument schemas (one per tool)
# --------------------------------------------------------------------------- #

DEFAULT_PRIORITY_ORDER: list[str] = ["amenities", "transit", "safety"]
ALLOWED_INTERVENTIONS = Literal["bike_lane", "park", "toilet", "linknyc", "bus_stop"]


class ScoreNeighborhoodArgs(BaseModel):
    latitude: float = Field(ge=NYC_LAT_MIN, le=NYC_LAT_MAX)
    longitude: float = Field(ge=NYC_LON_MIN, le=NYC_LON_MAX)
    radius_m: int = Field(default=500, ge=50, le=2000)


class CompareNeighborhoodsArgs(BaseModel):
    point_a: Point
    point_b: Point
    radius_m: int = Field(default=500, ge=50, le=2000)


class QueryDatasetArgs(BaseModel):
    dataset_id: str = Field(min_length=2, max_length=64)
    filters: dict[str, Any] = Field(default_factory=dict)
    limit: int = Field(default=100, ge=1, le=1000)


class FindSimilarNeighborhoodsArgs(BaseModel):
    latitude: float = Field(ge=NYC_LAT_MIN, le=NYC_LAT_MAX)
    longitude: float = Field(ge=NYC_LON_MIN, le=NYC_LON_MAX)
    k: int = Field(default=5, ge=1, le=25)


class WalkingIsochroneArgs(BaseModel):
    latitude: float = Field(ge=NYC_LAT_MIN, le=NYC_LAT_MAX)
    longitude: float = Field(ge=NYC_LON_MIN, le=NYC_LON_MAX)
    minutes: int = Field(default=10, ge=1, le=60)


class SimulateInterventionArgs(BaseModel):
    latitude: float = Field(ge=NYC_LAT_MIN, le=NYC_LAT_MAX)
    longitude: float = Field(ge=NYC_LON_MIN, le=NYC_LON_MAX)
    intervention_type: ALLOWED_INTERVENTIONS
    count: int = Field(default=1, ge=1, le=20)


class SearchAddressArgs(BaseModel):
    query: str = Field(min_length=3, max_length=200)
    limit: int = Field(default=5, ge=1, le=10)


class RetrieveDatasetDocsArgs(BaseModel):
    query: str = Field(min_length=3, max_length=500)
    dataset_filter: list[str] | None = None
    top_k: int = Field(default=5, ge=1, le=20)


# --------------------------------------------------------------------------- #
# Tool implementations
# --------------------------------------------------------------------------- #


def _score_neighborhood(args: ScoreNeighborhoodArgs) -> dict[str, Any]:
    """Resolve to backend ``analyze_point`` (in-process) or ``/api/analyze-point``.

    The backend service requires ``radius_m`` to be one of {200, 500, 1000};
    snap any incoming value to the nearest allowed bin.
    """

    _log_dispatch_mode_once()
    radius_m = _snap_radius(args.radius_m)
    backend = _resolve_backend_module()
    if backend is not None:
        # In-process path: skip JSON serialization + HTTP loopback entirely.
        # We disable LLM brief generation (use_llm=False) because the agent
        # itself will turn the structured payload into prose.
        return backend.analyze_point(
            latitude=args.latitude,
            longitude=args.longitude,
            radius_m=radius_m,
            priority_order=list(DEFAULT_PRIORITY_ORDER),
            time_window_days=365,
            use_llm=False,
            report_mode="individual",
        )
    body = {
        "latitude": args.latitude,
        "longitude": args.longitude,
        "radius_m": radius_m,
        "priority_order": DEFAULT_PRIORITY_ORDER,
        "time_window_days": 365,
        "report_mode": "individual",
        "include_report": False,
    }
    return _backend_post("/api/analyze-point", body)


def _compare_neighborhoods(args: CompareNeighborhoodsArgs) -> dict[str, Any]:
    """Score two points and return the per-category delta (b - a).

    In-process via ``service.compare_points`` when the backend is importable,
    otherwise HTTP loopback to ``POST /api/compare-points``.
    """

    _log_dispatch_mode_once()
    radius_m = _snap_radius(args.radius_m)
    backend = _resolve_backend_module()
    if backend is not None:
        return backend.compare_points(
            point_a=args.point_a.model_dump(),
            point_b=args.point_b.model_dump(),
            radius_m=radius_m,
            priority_order=list(DEFAULT_PRIORITY_ORDER),
            time_window_days=365,
        )
    body = {
        "point_a": args.point_a.model_dump(),
        "point_b": args.point_b.model_dump(),
        "radius_m": radius_m,
        "priority_order": DEFAULT_PRIORITY_ORDER,
        "time_window_days": 365,
    }
    return _backend_post("/api/compare-points", body)


def _query_dataset(args: QueryDatasetArgs) -> dict[str, Any]:
    """Filtered raw-row query.

    Routed through ``get_overview_payload`` (in-process) or ``/api/overview``
    for now: the existing endpoint accepts a category-scoped viewport query.
    Until a generic dataset query endpoint lands, we surface a structured
    error if dataset_id is not one of the five category aliases the overview
    endpoint understands.
    """

    _log_dispatch_mode_once()
    allowed_categories = {"safety", "transit", "amenities", "building", "overall"}
    normalized = args.dataset_id.strip().lower()
    if normalized not in allowed_categories:
        # Real per-dataset row query against the published ready Parquet.
        # Category aliases keep the cell-aggregate path below because they are
        # score layers, not source datasets.
        backend = _resolve_backend_module()
        if backend is not None:
            return backend.query_dataset_rows(
                dataset_id=normalized,
                filters=args.filters,
                limit=args.limit,
            )
        return _backend_post(
            "/api/dataset/query",
            {
                "dataset_id": normalized,
                "filters": args.filters,
                "limit": args.limit,
            },
        )
    view_mode = "category" if normalized != "overall" else "overall"
    category_id = normalized if normalized != "overall" else None
    backend = _resolve_backend_module()
    if backend is not None:
        payload = backend.get_overview_payload(
            view_mode=view_mode,
            category_id=category_id,
            viewport=None,
            zoom=None,
        )
    else:
        body = {
            "view_mode": view_mode,
            "category_id": category_id,
            "render_mode": "h3_cells",
        }
        payload = _backend_post("/api/overview", body)
    cells = payload.get("cells", [])[: args.limit]
    return {
        "dataset_id": args.dataset_id,
        "rows": cells,
        "total": len(cells),
        "note": "Routed through /api/overview; cell-level aggregation only.",
    }


def _find_similar_neighborhoods(args: FindSimilarNeighborhoodsArgs) -> dict[str, Any]:
    """Watchlist-style nearest-neighbor lookup using the seed point.

    The backend's ``run_watchlist`` accepts a list of seeds and returns ranked
    nearby points. We treat it as the closest existing approximation of
    "find K similar" until a dedicated endpoint is built. Routes in-process
    when the backend module is importable, otherwise falls back to
    ``POST /api/watchlist/run`` over HTTP.
    """

    _log_dispatch_mode_once()
    seeds: list[dict[str, Any]] = [
        {"latitude": args.latitude, "longitude": args.longitude, "title": "seed"}
    ]
    backend = _resolve_backend_module()
    if backend is not None:
        payload = backend.run_watchlist(
            seeds=seeds,
            radius_m=500,
            time_window_days=365,
            priority_order=list(DEFAULT_PRIORITY_ORDER),
        )
    else:
        body = {
            "seeds": seeds,
            "priority_order": DEFAULT_PRIORITY_ORDER,
            "radius_m": 500,
            "time_window_days": 365,
        }
        payload = _backend_post("/api/watchlist/run", body)
    items = payload.get("results", payload.get("items", []))[: args.k]
    return {
        "seed": {"latitude": args.latitude, "longitude": args.longitude},
        "neighbors": items,
        "k": args.k,
        "note": "Routed through /api/watchlist/run; replace with a dedicated "
                "/api/similar endpoint once available.",
    }


def _walking_isochrone(args: WalkingIsochroneArgs) -> dict[str, Any]:
    """Street-network walking isochrone as a GeoJSON Feature.

    In-process via ``scenarios.walking_isochrone`` when the backend is
    importable, otherwise HTTP loopback to ``POST /api/isochrone``.
    """

    _log_dispatch_mode_once()
    backend = _resolve_backend_module()
    if backend is not None:
        from urban_dossier_backend.scenarios import walking_isochrone

        return walking_isochrone(
            latitude=args.latitude,
            longitude=args.longitude,
            minutes=args.minutes,
        )
    body = {
        "latitude": args.latitude,
        "longitude": args.longitude,
        "minutes": args.minutes,
        "mode": "walk",
    }
    return _backend_post("/api/isochrone", body)


def _simulate_intervention(args: SimulateInterventionArgs) -> dict[str, Any]:
    """Project scores after adding assets, using fitted count->score curves.

    Correlational, not causal; the response carries that caveat and the fit
    quality so the agent can qualify what it reports.
    """

    _log_dispatch_mode_once()
    backend = _resolve_backend_module()
    if backend is not None:
        from urban_dossier_backend.scenarios import simulate_intervention

        return simulate_intervention(
            latitude=args.latitude,
            longitude=args.longitude,
            intervention_type=args.intervention_type,
            count=args.count,
        )
    body = {
        "latitude": args.latitude,
        "longitude": args.longitude,
        "intervention_type": args.intervention_type,
        "count": args.count,
    }
    return _backend_post("/api/simulate", body)


def _search_address(args: SearchAddressArgs) -> dict[str, Any]:
    """Geocode an address. In-process via ``service.search_address_payload``
    when the backend module is importable, otherwise HTTP loopback to
    ``POST /api/search``."""

    _log_dispatch_mode_once()
    backend = _resolve_backend_module()
    if backend is not None:
        return backend.search_address_payload(query=args.query, limit=args.limit)
    body = {"query": args.query, "limit": args.limit}
    return _backend_post("/api/search", body)


def _retrieve_dataset_docs(args: RetrieveDatasetDocsArgs) -> dict[str, Any]:
    """RAG retrieval against the dataset documentation index.

    The rag package is built by a parallel agent. Import lazily so this
    module loads even when rag/ is empty during early development. At
    runtime, raise a clear error if rag.retrieve is unavailable.
    """

    try:
        from rag import retrieve  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NotImplementedError(
            "Tool retrieve_dataset_docs requires the rag package "
            "(rag.retrieve) to be importable. The rag package is being built "
            "by a parallel agent. Once available, expose: "
            "rag.retrieve(query: str, dataset_filter: list[str] | None, "
            "top_k: int) -> {'hits': list[{dataset_id, snippet, score}], "
            "'query': str}."
        ) from exc

    chunks = retrieve(
        query=args.query,
        dataset_filter=args.dataset_filter,
        top_k=args.top_k,
    )
    # rag.retrieve returns list[RetrievedChunk] (dataclass). The agent loop
    # serializes tool results to JSON for the LLM, so flatten into the
    # documented {"hits": [...], "query": str} contract here.
    return {
        "query": args.query,
        "hits": [dataclasses.asdict(chunk) for chunk in chunks],
    }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _snap_radius(radius_m: int) -> int:
    """Snap an arbitrary radius to the {200, 500, 1000} bins the backend allows."""

    if radius_m <= 350:
        return 200
    if radius_m <= 750:
        return 500
    return 1000


# --------------------------------------------------------------------------- #
# OpenAI-compatible tool schemas (the wire format vLLM emits to the model)
# --------------------------------------------------------------------------- #


def _fn(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """Wrap a function declaration in OpenAI's function-tool envelope."""

    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


_POINT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "latitude": {"type": "number", "minimum": NYC_LAT_MIN, "maximum": NYC_LAT_MAX},
        "longitude": {"type": "number", "minimum": NYC_LON_MIN, "maximum": NYC_LON_MAX},
    },
    "required": ["latitude", "longitude"],
}


TOOLS: list[dict[str, Any]] = [
    _fn(
        "score_neighborhood",
        "Compute the four NYC category scores (safety, transit, amenities, "
        "building) for the area within radius_m metres of a lat/lon point. "
        "Use for ranked or qualitative neighborhood questions.",
        {
            "type": "object",
            "properties": {
                "latitude": {"type": "number", "minimum": NYC_LAT_MIN, "maximum": NYC_LAT_MAX},
                "longitude": {"type": "number", "minimum": NYC_LON_MIN, "maximum": NYC_LON_MAX},
                "radius_m": {"type": "integer", "minimum": 50, "maximum": 2000, "default": 500},
            },
            "required": ["latitude", "longitude"],
        },
    ),
    _fn(
        "compare_neighborhoods",
        "Side-by-side comparison of two points across all four categories. "
        "Use when the user names two locations and wants to know which is "
        "better for some purpose.",
        {
            "type": "object",
            "properties": {
                "point_a": _POINT_SCHEMA,
                "point_b": _POINT_SCHEMA,
                "radius_m": {"type": "integer", "minimum": 50, "maximum": 2000, "default": 500},
            },
            "required": ["point_a", "point_b"],
        },
    ),
    _fn(
        "query_dataset",
        "Run a filtered query against one of the 18 NYC Open Data sources. "
        "Use only when the user wants a literal count or list of records.",
        {
            "type": "object",
            "properties": {
                "dataset_id": {
                    "type": "string",
                    "description": "Dataset identifier from the catalog "
                                   "(e.g. 'safety', 'transit', 'amenities', "
                                   "'building', 'overall', or a specific "
                                   "sub-dataset name like 'subway_stations').",
                },
                "filters": {"type": "object", "default": {}},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
            },
            "required": ["dataset_id"],
        },
    ),
    _fn(
        "find_similar_neighborhoods",
        "Return the K most similar NYC locations to the seed point, ranked "
        "by score-vector distance. Use for 'find me a neighborhood like X'.",
        {
            "type": "object",
            "properties": {
                "latitude": {"type": "number", "minimum": NYC_LAT_MIN, "maximum": NYC_LAT_MAX},
                "longitude": {"type": "number", "minimum": NYC_LON_MIN, "maximum": NYC_LON_MAX},
                "k": {"type": "integer", "minimum": 1, "maximum": 25, "default": 5},
            },
            "required": ["latitude", "longitude"],
        },
    ),
    _fn(
        "walking_isochrone",
        "Return a GeoJSON polygon of the area reachable on foot within the "
        "given number of minutes from the seed point. Use for walkability "
        "and coverage questions.",
        {
            "type": "object",
            "properties": {
                "latitude": {"type": "number", "minimum": NYC_LAT_MIN, "maximum": NYC_LAT_MAX},
                "longitude": {"type": "number", "minimum": NYC_LON_MIN, "maximum": NYC_LON_MAX},
                "minutes": {"type": "integer", "minimum": 1, "maximum": 60, "default": 10},
            },
            "required": ["latitude", "longitude"],
        },
    ),
    _fn(
        "simulate_intervention",
        "Project the score impact of adding `count` instances of an "
        "intervention near a point. intervention_type must be one of "
        "{bike_lane, park, toilet, linknyc, bus_stop}.",
        {
            "type": "object",
            "properties": {
                "latitude": {"type": "number", "minimum": NYC_LAT_MIN, "maximum": NYC_LAT_MAX},
                "longitude": {"type": "number", "minimum": NYC_LON_MIN, "maximum": NYC_LON_MAX},
                "intervention_type": {
                    "type": "string",
                    "enum": ["bike_lane", "park", "toilet", "linknyc", "bus_stop"],
                },
                "count": {"type": "integer", "minimum": 1, "maximum": 20, "default": 1},
            },
            "required": ["latitude", "longitude", "intervention_type"],
        },
    ),
    _fn(
        "search_address",
        "Geocode an address, building number, or place name to a list of "
        "candidate {address, borough, zip, latitude, longitude} matches. "
        "Always run this first when the user provides a name instead of "
        "coordinates.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 3, "maxLength": 200},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
            },
            "required": ["query"],
        },
    ),
    _fn(
        "retrieve_dataset_docs",
        "Semantic search across the dataset documentation index. Use this "
        "whenever you are unsure which dataset to query, or you need column "
        "semantics. Optional dataset_filter restricts the search to a list "
        "of dataset ids.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 3, "maxLength": 500},
                "dataset_filter": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": None,
                },
                "top_k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            },
            "required": ["query"],
        },
    ),
]


_CORE_TOOLS = {
    "score_neighborhood",
    "compare_neighborhoods",
    "query_dataset",
    "search_address",
}


def _artifact_state(available: bool, reason: str, **details: Any) -> dict[str, Any]:
    return {"available": available, "reason": reason, **details}


def _walking_state() -> dict[str, Any]:
    graph_dir = Path(
        os.getenv("URBAN_DOSSIER_WALK_GRAPH_DIR", "/mnt/data/urban-dossier-state/maps/walk")
    )
    manifest_path = graph_dir / "walk_graph.manifest.json"
    required = [
        graph_dir / "walk_nodes.parquet",
        graph_dir / "walk_edges.parquet",
        manifest_path,
    ]
    missing = [
        str(path)
        for path in required
        if not path.is_file() or path.stat().st_size == 0
    ]
    manifest_valid = False
    if not missing:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_valid = (
                manifest.get("network_type") == "walking"
                and int(manifest.get("node_count", 0)) > 0
                and int(manifest.get("edge_count", 0)) > 0
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            manifest_valid = False
    available = not missing and manifest_valid
    return _artifact_state(
        available,
        "ready"
        if available
        else ("walking_graph_missing" if missing else "walking_manifest_invalid"),
        release_gate="walking_graph",
        required_files=[str(path) for path in required],
        missing_files=missing,
    )


def _elasticity_state() -> dict[str, Any]:
    override = os.getenv("URBAN_DOSSIER_ELASTICITY_PATH")
    if override:
        path = Path(override)
    else:
        data_root = Path(os.getenv("URBAN_DOSSIER_DATA_ROOT", "data"))
        path = data_root / "cache" / "simulation" / "elasticity.json"
    if not path.is_file():
        return _artifact_state(
            False,
            "elasticity_artifact_missing",
            release_gate="elasticity_artifact",
            artifact=str(path),
            interventions=[],
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _artifact_state(
            False,
            "elasticity_artifact_invalid",
            release_gate="elasticity_artifact",
            artifact=str(path),
            detail=str(exc),
            interventions=[],
        )
    interventions = sorted(
        name
        for name, entry in (payload.get("interventions") or {}).items()
        if isinstance(entry, dict) and entry.get("available") is True
    )
    return _artifact_state(
        bool(interventions),
        "ready" if interventions else "no_fitted_interventions",
        release_gate="elasticity_artifact",
        artifact=str(path),
        interventions=interventions,
    )


def _rag_state() -> dict[str, Any]:
    base = Path(os.getenv("RAG_INDEX_DIR", "index"))
    explicit = os.getenv("RAG_INDEX_FILENAME")
    candidates = (
        [base / explicit]
        if explicit
        else [base / "corpus.cuvs", base / "corpus.faiss"]
    )
    for path in candidates:
        meta = path.with_suffix(path.suffix + ".meta.json")
        index_exists = (path.is_file() and path.stat().st_size > 0) or (
            path.suffix == ".cuvs"
            and Path(str(path) + ".vectors.npy").is_file()
            and Path(str(path) + ".vectors.npy").stat().st_size > 0
        )
        metadata_valid = False
        if meta.is_file():
            try:
                payload = json.loads(meta.read_text(encoding="utf-8"))
                metadata_valid = (
                    int(payload.get("dim", 0)) > 0
                    and isinstance(payload.get("metadata"), list)
                    and bool(payload["metadata"])
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                metadata_valid = False
        if index_exists and metadata_valid:
            return _artifact_state(
                True,
                "ready",
                release_gate="rag_index",
                artifact=str(path),
                metadata=str(meta),
            )
    return _artifact_state(
        False,
        "rag_index_missing",
        release_gate="rag_index",
        required_any=[str(path) for path in candidates],
    )


def tool_availability() -> dict[str, dict[str, Any]]:
    """Return the runtime release decision for every stable tool name."""

    states = {name: _artifact_state(True, "ready", release_gate="core") for name in _CORE_TOOLS}
    states.update(
        {
            "find_similar_neighborhoods": _artifact_state(
                False,
                "dedicated_similarity_not_implemented",
                release_gate="dedicated_similarity_index",
            ),
            "walking_isochrone": _walking_state(),
            "simulate_intervention": _elasticity_state(),
            "retrieve_dataset_docs": _rag_state(),
        }
    )
    return {name: states[name] for name in sorted(states)}


def released_tool_names() -> list[str]:
    """Just the names of tools passing their release gate -- for the intent
    router's meta-help answer, which must describe what is actually callable
    rather than the full aspirational registry."""
    return [
        schema["function"]["name"] for schema in get_available_tools()
    ]


def get_available_tools(
    states: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return copied schemas for tools that pass their release gate."""

    states = states or tool_availability()
    active: list[dict[str, Any]] = []
    for schema in TOOLS:
        name = schema["function"]["name"]
        state = states[name]
        if not state["available"]:
            continue
        published = copy.deepcopy(schema)
        if name == "simulate_intervention":
            intervention = published["function"]["parameters"]["properties"][
                "intervention_type"
            ]
            intervention["enum"] = state["interventions"]
        active.append(published)
    return active


def tool_availability_prompt(states: dict[str, dict[str, Any]] | None = None) -> str:
    """Align the system prose with the schemas published for this request."""

    states = states or tool_availability()
    active = [name for name, state in states.items() if state["available"]]
    inactive = [
        f"{name} ({state['reason']})"
        for name, state in states.items()
        if not state["available"]
    ]
    return (
        "\n\n# Runtime tool release gates\n"
        f"Active tools for this request: {', '.join(active)}.\n"
        f"Unavailable tools: {', '.join(inactive) or 'none'}. "
        "Do not claim to run or cite an unavailable tool; explain the limitation instead."
    )


# --------------------------------------------------------------------------- #
# Dispatcher
# --------------------------------------------------------------------------- #


_TOOL_REGISTRY: dict[str, tuple[type[BaseModel], Callable[[Any], dict[str, Any]]]] = {
    "score_neighborhood": (ScoreNeighborhoodArgs, _score_neighborhood),
    "compare_neighborhoods": (CompareNeighborhoodsArgs, _compare_neighborhoods),
    "query_dataset": (QueryDatasetArgs, _query_dataset),
    "find_similar_neighborhoods": (FindSimilarNeighborhoodsArgs, _find_similar_neighborhoods),
    "walking_isochrone": (WalkingIsochroneArgs, _walking_isochrone),
    "simulate_intervention": (SimulateInterventionArgs, _simulate_intervention),
    "search_address": (SearchAddressArgs, _search_address),
    "retrieve_dataset_docs": (RetrieveDatasetDocsArgs, _retrieve_dataset_docs),
}


def dispatch_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate args, run the implementation, and return a JSON-serializable dict.

    Never raises. All exceptions are caught and surfaced as
    {"error": str, "retry_hint": str, ...} so the agent loop can pass the
    failure back to the model as an observation.
    """

    started = time.perf_counter()

    if name not in _TOOL_REGISTRY:
        return {
            "error": f"Unknown tool '{name}'. "
                     f"Allowed: {sorted(_TOOL_REGISTRY)}.",
            "retry_hint": "Pick a tool from the allowed list and re-issue.",
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }

    arg_model, impl = _TOOL_REGISTRY[name]

    try:
        validated = arg_model(**(args or {}))
    except ValidationError as exc:
        return {
            "error": f"Argument validation failed for {name}: {exc.errors()}",
            "retry_hint": "Re-issue the call with arguments that match the "
                          "tool schema (check types and required fields).",
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }

    try:
        result = impl(validated)
    except NotImplementedError as exc:
        return {
            "error": str(exc),
            "retry_hint": "This tool depends on a backend endpoint that is "
                          "not yet deployed. Skip it or pick an alternative "
                          "tool to gather similar evidence.",
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }
    except ConnectionError as exc:
        return {
            "error": f"Backend unreachable while calling {name}: {exc}",
            "retry_hint": "The FastAPI backend is down or the URL is wrong. "
                          "Surface this gap to the user instead of guessing.",
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:  # noqa: BLE001 - dispatcher contract: never raise
        return {
            "error": f"Unhandled exception in {name}: {type(exc).__name__}: {exc}",
            "retry_hint": "Try a simpler argument set, or pick a different "
                          "tool to obtain the same evidence.",
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }

    if not isinstance(result, dict):
        return {
            "error": f"Tool {name} returned a non-dict result of type "
                     f"{type(result).__name__}",
            "retry_hint": "Internal contract violation - file a bug.",
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }

    result.setdefault("latency_ms", int((time.perf_counter() - started) * 1000))
    # The payload policy is the last gate before a result becomes model
    # context. Errors above bypass it deliberately: an error string carries no
    # data rows, and the model needs it verbatim to pivot.
    from .payload_policy import apply_policy, resolve_policy

    return apply_policy(result, resolve_policy())
