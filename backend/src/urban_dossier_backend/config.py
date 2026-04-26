from __future__ import annotations

import os
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
SRC_ROOT = PACKAGE_ROOT.parent
BACKEND_ROOT = SRC_ROOT.parent
REPO_ROOT = BACKEND_ROOT.parent


def _discover_data_root() -> Path | None:
    env = os.getenv("URBAN_DOSSIER_DATA_ROOT")
    if env:
        candidate = Path(env)
        if candidate.exists():
            return candidate

    parent = REPO_ROOT.parent
    # Search legacy Drive-download directory names (older envs).
    for pattern in ["drive-download*/urban_dossier/data", "drive-download*/blocksense/data"]:
        for candidate in sorted(parent.glob(pattern)):
            if (candidate / "processed").exists():
                return candidate

    # Fallback: repo-local data directory (v3.7.7+ preprocessing-first layout).
    # Even if processed/ is absent, we want CACHE_DIR to resolve so overview
    # tiles can be generated into repo_root/data/cache/overview.
    local_data = REPO_ROOT / "data"
    if local_data.exists():
        return local_data
    return None


DATA_ROOT = _discover_data_root()
PROCESSED_DIR = DATA_ROOT / "processed" if DATA_ROOT and (DATA_ROOT / "processed").exists() else None
CACHE_DIR = DATA_ROOT / "cache" if DATA_ROOT else None
if CACHE_DIR is not None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / "overview").mkdir(parents=True, exist_ok=True)
PRECOMPUTED_DIR = CACHE_DIR / "precomputed" if CACHE_DIR else None
REPORT_CACHE_DIR = CACHE_DIR / "reports" if CACHE_DIR else None
OVERVIEW_DIR = CACHE_DIR / "overview" if CACHE_DIR else None
DEMO_DIR = DATA_ROOT / "demo" if DATA_ROOT else None
READY_DATA_DIR = Path(os.getenv("URBAN_DOSSIER_READY_ROOT", str(REPO_ROOT / "data" / "ready")))
RAW_DATA_ROOT = Path(os.getenv("URBAN_DOSSIER_RAW_DATA_ROOT", str(Path.home() / "nyc_open_data" / "_claw_out" / "gpu_cleaned")))

URBAN_DOSSIER_DATA_MODE = os.getenv("URBAN_DOSSIER_DATA_MODE", "direct").lower()
DEFAULT_MODEL = os.getenv("URBAN_DOSSIER_MODEL", "auto")
DEFAULT_OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "http://localhost:8000/v1")
DEFAULT_OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "not-needed")
DEFAULT_TIME_WINDOW_DAYS = int(os.getenv("URBAN_DOSSIER_TIME_WINDOW_DAYS", "365"))
DEFAULT_RADIUS_M = int(os.getenv("URBAN_DOSSIER_DEFAULT_RADIUS_M", "500"))
DEMO_TOKEN = os.getenv("URBAN_DOSSIER_DEMO_TOKEN", "").strip()
DEMO_TOKEN_HEADER = "x-urban-dossier-token"
ALLOWED_CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "URBAN_DOSSIER_ALLOWED_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000,http://localhost:3456,http://127.0.0.1:3456,http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if origin.strip()
]

OVERVIEW_DEFAULT_WEIGHTS = {
    "safety": 0.40,
    "transit": 0.30,
    "amenities": 0.30,
}

PRIORITY_DECAY = float(os.getenv("URBAN_DOSSIER_PRIORITY_DECAY", "0.72"))

AGENT_ENABLED = os.environ.get("URBAN_DOSSIER_AGENT_ENABLED", "1").strip() in ("1", "true", "yes")
AGENT_BACKEND = os.environ.get("URBAN_DOSSIER_AGENT_BACKEND", "scripts")
