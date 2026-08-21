"""GPU-accelerated spatial clustering for incident hotspot detection.

Uses cuML DBSCAN on managed memory (unified memory) to share the 128GB pool
with vllm. Falls back to scikit-learn on CPU if cuML is unavailable.
"""
from __future__ import annotations

import logging
import math
from typing import Any

from .utils import haversine_m

logger = logging.getLogger(__name__)

_CUML_AVAILABLE = False
_RMM_INITIALIZED = False

def _ensure_rmm():
    """Initialize RMM with managed memory once."""
    global _RMM_INITIALIZED
    if _RMM_INITIALIZED:
        return
    try:
        import rmm
        rmm.reinitialize(managed_memory=True)
        _RMM_INITIALIZED = True
    except Exception as e:
        logger.warning("RMM managed memory init failed: %s", e)
        _RMM_INITIALIZED = True  # don't retry

try:
    _ensure_rmm()
    from cuml.cluster import DBSCAN as cuDBSCAN
    import cudf
    _CUML_AVAILABLE = True
    logger.info("cuML DBSCAN available (managed memory)")
except ImportError:
    pass


def detect_hotspots(
    incidents: list[dict[str, Any]],
    eps_meters: float = 100.0,
    min_samples: int = 3,
    lat_key: str = "latitude",
    lon_key: str = "longitude",
    kind_key: str = "kind",
) -> list[dict[str, Any]]:
    """Cluster nearby incidents into spatial hotspots.

    Args:
        incidents: List of dicts with at least lat/lon keys
        eps_meters: Clustering radius in meters (default 100m)
        min_samples: Minimum incidents to form a cluster
        lat_key/lon_key: Field names for coordinates
        kind_key: Field name for incident type (for dominant_type)

    Returns:
        List of hotspot dicts:
        {
            "center_lat": float,
            "center_lon": float,
            "incident_count": int,
            "radius_m": float (approximate),
            "dominant_type": str or None,
            "types": {"collision": 5, "rodent": 3},
            "label": int (cluster ID)
        }
        Sorted by incident_count descending.
    """
    if eps_meters <= 0 or min_samples < 1:
        raise ValueError("eps_meters and min_samples must be positive")

    # Parse once and reject malformed, non-finite, or out-of-range coordinates.
    # Passing NaN through makes both DBSCAN implementations reject the whole
    # request instead of merely dropping the bad incident.
    valid: list[dict[str, Any]] = []
    lats: list[float] = []
    lons: list[float] = []
    for incident in incidents:
        try:
            lat = float(incident.get(lat_key))
            lon = float(incident.get(lon_key))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        valid.append(incident)
        lats.append(lat)
        lons.append(lon)

    if len(valid) < min_samples:
        return []

    # Project to local metres before clustering. A degree of longitude is only
    # cos(latitude) times a degree of latitude; treating them as equal
    # under-clustered east-west NYC incidents by roughly 24 percent.
    mean_lat = sum(lats) / len(lats)
    mean_lon = sum(lons) / len(lons)
    cos_lat = max(math.cos(math.radians(mean_lat)), 1e-12)
    earth_radius_m = 6_371_008.8
    points_m = [
        (
            earth_radius_m * math.radians(lon - mean_lon) * cos_lat,
            earth_radius_m * math.radians(lat - mean_lat),
        )
        for lat, lon in zip(lats, lons)
    ]

    labels = _cluster_gpu(points_m, eps_meters, min_samples)
    if labels is None:
        labels = _cluster_cpu(points_m, eps_meters, min_samples)

    if labels is None:
        return []

    # Aggregate clusters
    clusters: dict[int, dict] = {}
    for idx, label in enumerate(labels):
        if label < 0:  # noise point
            continue
        if label not in clusters:
            clusters[label] = {
                "lats": [], "lons": [], "types": {},
                "label": label
            }
        c = clusters[label]
        c["lats"].append(lats[idx])
        c["lons"].append(lons[idx])
        kind = valid[idx].get(kind_key, "unknown")
        c["types"][kind] = c["types"].get(kind, 0) + 1

    # Build output
    result = []
    for label, c in clusters.items():
        n = len(c["lats"])
        center_lat = sum(c["lats"]) / n
        center_lon = sum(c["lons"]) / n

        # Exact great-circle distance keeps the reported radius consistent
        # with the clustering units and works outside the hard-coded NYC latitude.
        max_dist_m = max(
            haversine_m(center_lat, center_lon, lat, lon)
            for lat, lon in zip(c["lats"], c["lons"])
        )

        dominant_type = max(c["types"], key=c["types"].get) if c["types"] else None

        result.append({
            "center_lat": round(center_lat, 6),
            "center_lon": round(center_lon, 6),
            "incident_count": n,
            "radius_m": round(max_dist_m, 1),
            "dominant_type": dominant_type,
            "types": c["types"],
            "label": label,
        })

    result.sort(key=lambda x: x["incident_count"], reverse=True)
    return result


def _cluster_gpu(points_m, eps_meters, min_samples):
    """GPU DBSCAN via cuML. Returns list of labels or None."""
    if not _CUML_AVAILABLE:
        return None
    try:
        _ensure_rmm()
        df = cudf.DataFrame(points_m, columns=["x_m", "y_m"])
        db = cuDBSCAN(eps=eps_meters, min_samples=min_samples)
        labels_series = db.fit_predict(df[["x_m", "y_m"]])
        return labels_series.to_numpy().tolist()
    except Exception as e:
        logger.warning("cuML DBSCAN failed, falling back to CPU: %s", e)
        return None


def _cluster_cpu(points_m, eps_meters, min_samples):
    """CPU DBSCAN via scikit-learn. Returns list of labels or None."""
    try:
        from sklearn.cluster import DBSCAN
        db = DBSCAN(eps=eps_meters, min_samples=min_samples)
        return db.fit_predict(points_m).tolist()
    except ImportError:
        logger.warning("scikit-learn not available for CPU fallback")
        return None
    except Exception as e:
        logger.warning("CPU DBSCAN failed: %s", e)
        return None
