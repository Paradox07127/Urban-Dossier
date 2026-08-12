from __future__ import annotations

import logging
import threading
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from ..categories import CATEGORY_CONFIG
from ..config import (
    BOUNDARIES_DIR,
    CACHE_DIR,
    OVERVIEW_DEFAULT_WEIGHTS,
    PROCESSED_DIR,
    RAW_DATA_ROOT,
    READY_DATA_DIR,
)
from ..utils import bbox, compact_records, fast_distance_sq_m, haversine_m, is_within_days, parse_date, quote, to_float, to_int
from .base import DataProvider
from .gpu_queries import gpu_emergency_metrics, gpu_fetch_radius_rows, gpu_nearest_overview_cell, is_available as gpu_available, is_fallback

logger = logging.getLogger(__name__)


def _round_coords(value: Any, ndigits: int = 6) -> Any:
    """Round a nested GeoJSON coordinate structure in place-ish.

    Six decimals is about 0.1 m, well under a pixel at any zoom, and keeps the
    clipped shoreline from tripling the payload with float noise.
    """
    if isinstance(value, (int, float)):
        return round(float(value), ndigits)
    return [_round_coords(item, ndigits) for item in value]


SAFETY_311_TYPES = {"RODENT", "SANITATION CONDITION", "UNSANITARY CONDITION"}
POSITIVE_RODENT_TERMS = ("RAT", "FAILED", "ACTIVE")


def _complaint_breakdown(rows: list[dict], type_col: str, limit: int = 5) -> dict[str, int]:
    """Break down 311 complaints by type."""
    from collections import Counter
    types = Counter(str(row.get(type_col) or "").strip() for row in rows if row.get(type_col))
    return dict(types.most_common(limit))


def _restaurant_highlights(rows: list[dict]) -> dict[str, Any]:
    """Extract restaurant names and grade distribution."""
    graded = [r for r in rows if r.get("GRADE") in ("A", "B", "C")]
    a_list = [r.get("DBA", "?") for r in graded if r.get("GRADE") == "A"]
    return {
        "a_grade_count": len(a_list),
        "b_grade_count": sum(1 for r in graded if r.get("GRADE") == "B"),
        "c_grade_count": sum(1 for r in graded if r.get("GRADE") == "C"),
        "total_graded": len(graded),
        "sample_a_names": a_list[:4],
    }


def _collision_time_buckets(rows: list[dict]) -> dict[str, int]:
    """Bucket collisions by time of day."""
    buckets = {"morning_6_12": 0, "afternoon_12_18": 0, "evening_18_24": 0, "night_0_6": 0}
    for r in rows:
        t = r.get("CRASH TIME") or r.get("crash_time") or ""
        try:
            h = int(str(t).split(":")[0])
            if 6 <= h < 12: buckets["morning_6_12"] += 1
            elif 12 <= h < 18: buckets["afternoon_12_18"] += 1
            elif 18 <= h < 24: buckets["evening_18_24"] += 1
            else: buckets["night_0_6"] += 1
        except (ValueError, IndexError):
            pass
    return buckets


def _violation_age_summary(rows: list[dict]) -> dict[str, Any]:
    """Compute how long violations have been open."""
    ages = []
    for r in rows:
        d = parse_date(r.get("InspectionDate") or r.get("event_date"))
        if d:
            ages.append((date.today() - d).days)
    if not ages:
        return {}
    return {
        "avg_age_days": round(sum(ages) / len(ages)),
        "max_age_days": max(ages),
        "older_than_1yr": sum(1 for a in ages if a > 365),
        "older_than_2yr": sum(1 for a in ages if a > 730),
    }


def _tree_health_summary(rows: list[dict]) -> dict[str, int]:
    """Break down trees by health status."""
    from collections import Counter
    healths = Counter(str(r.get("health") or "Unknown").title() for r in rows)
    return dict(healths.most_common(5))


def _facility_type_breakdown(rows: list[dict]) -> dict[str, int]:
    """Break down facilities by group."""
    from collections import Counter
    groups = Counter(str(r.get("facgroup") or r.get("kind") or "Other").title() for r in rows)
    return dict(groups.most_common(6))

BoroughMap = {
    "MN": "MANHATTAN",
    "MANHATTAN": "MANHATTAN",
    "BK": "BROOKLYN",
    "BROOKLYN": "BROOKLYN",
    "BX": "BRONX",
    "BRONX": "BRONX",
    "QN": "QUEENS",
    "QUEENS": "QUEENS",
    "SI": "STATEN ISLAND",
    "STATEN ISLAND": "STATEN ISLAND",
}

PARQUET_ALIASES = {
    "location_index": ["location_index"],
    "pluto": ["pluto"],
    "parks": ["parks", "parks_properties"],
    "public_toilets": ["public_toilets"],
    "ems_dispatch": ["ems_dispatch"],
    "fire_dispatch": ["fire_dispatch"],
    "collisions": ["collisions"],
    "rodent_inspections": ["rodent_inspections"],
    "311_subset": ["311_subset"],
    "restaurant_inspections": ["restaurant_inspections"],
    "linknyc_locations": ["linknyc_locations"],
    "street_trees": ["street_trees"],
    "housing_violations": ["housing_violations"],
    "aep_buildings": ["aep_buildings"],
}

RAW_CSV_ALIASES = {
    "pluto": ["buildings/pluto.csv"],
    "parks": ["amenities/parks_properties.csv"],
    "public_toilets": ["amenities/public_toilets.csv"],
    "ems_dispatch": ["safety/ems_incident_dispatch.csv"],
    "fire_dispatch": ["safety/fire_incident_dispatch.csv"],
    "collisions": ["safety/motor_vehicle_collisions.csv"],
    "rodent_inspections": ["environment/rodent_inspections.csv"],
    "311_subset": ["quality_of_life/311_service_requests_2020_present.csv"],
    "restaurant_inspections": ["amenities/dohmh_restaurant_inspections.csv"],
    "linknyc_locations": ["amenities/linknyc_kiosk_locations.csv"],
    "street_trees": ["amenities/street_trees.csv"],
    "housing_violations": ["buildings/housing_code_violations.csv"],
    "aep_buildings": ["buildings/buildings_aep.csv"],
}

READY_DATASET_PATHS = {
    "location_index": "location/location_index.parquet",
    "collisions": "safety/collisions_indexed.parquet",
    "rodent_inspections": "safety/rodent_indexed.parquet",
    "311_subset": "safety/311_safety_indexed.parquet",
    "public_toilets": "amenities/toilets_indexed.parquet",
    "restaurant_inspections": "amenities/restaurants_indexed.parquet",
    "linknyc_locations": "amenities/linknyc_indexed.parquet",
    "street_trees": "amenities/trees_indexed.parquet",
    "parks": "amenities/parks_indexed.parquet",
    "ems_dispatch": "safety/ems_indexed.parquet",
    "fire_dispatch": "safety/fire_indexed.parquet",
    "housing_violations": "building/housing_violations_indexed.parquet",
    "aep_buildings": "building/aep_indexed.parquet",
}

READY_COLUMN_ALIASES = {
    "CRASH DATE": "event_date",
    "INSPECTION_DATE": "event_date",
    "Created Date": "event_date",
    "created_date": "event_date",
    "INSPECTION DATE": "event_date",
    "InspectionDate": "event_date",
    "AEP_START_DATE": "event_date",
}


# Thread-local DuckDB connections (see DirectQueryDataProvider._connect).
_THREAD_CONNECTIONS = threading.local()


class DirectQueryDataProvider(DataProvider):
    def __init__(self) -> None:
        self.processed_dir = PROCESSED_DIR
        self.ready_dir = READY_DATA_DIR
        self.cache_dir = CACHE_DIR
        self.overview_dir = self.cache_dir / "overview" if self.cache_dir else None
        self._use_gpu = gpu_available()

    def _connect(self):
        """One DuckDB connection per thread, reused across requests.

        Every call used to build a fresh in-memory connection, which is cheap
        to create but forfeits DuckDB's per-connection object cache -- so each
        request re-parsed the footer of every Parquet file it touched. The
        server's worker threads are long-lived; giving each its own cached
        connection lets repeat reads of the score tables skip straight to the
        row groups. Per-thread rather than shared because DuckDB connections
        must not be used from two threads at once, and the point-signal path
        deliberately fans out across threads.

        Everything on these connections is read-only, which is what makes
        reuse safe with no invalidation story: a data refresh is a file
        replace plus a service restart, never an in-place mutation.
        """
        try:
            import duckdb
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("duckdb is required for direct mode") from exc
        con = getattr(_THREAD_CONNECTIONS, "con", None)
        if con is None:
            con = duckdb.connect()
            try:
                con.execute("PRAGMA enable_object_cache=true")
            except Exception:  # noqa: BLE001 - the pragma is an optimisation, not a need
                logger.debug("duckdb object cache pragma unavailable")
            _THREAD_CONNECTIONS.con = con
        return con

    def _parquet_path(self, name: str) -> Path:
        if not self.processed_dir:
            return Path(name)
        for candidate in PARQUET_ALIASES.get(name, [name]):
            path = self.processed_dir / f"{candidate}.parquet"
            if path.exists():
                return path
        return self.processed_dir / f"{name}.parquet"

    def _query_rows(self, con: Any, sql: str, params: list[Any] | tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
        cursor = con.execute(sql, params or [])
        columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def _ready_path(self, relative_path: str) -> Path:
        return self.ready_dir / Path(relative_path)

    def _ready_exists(self, relative_path: str) -> bool:
        return self._ready_path(relative_path).exists()

    def _source_sql(self, name: str) -> str | None:
        path = self._parquet_path(name)
        if path.exists():
            return f"read_parquet('{path.as_posix()}')"
        raw = self._raw_csv_path(name)
        if raw and raw.exists():
            return f"read_csv_auto('{raw.as_posix()}', ignore_errors=true)"
        return None

    def _dataset_available(self, name: str) -> bool:
        ready_path = READY_DATASET_PATHS.get(name)
        return (
            bool(ready_path and self._ready_exists(ready_path))
            or self._parquet_path(name).exists()
            or bool(self._raw_csv_path(name))
        )

    def _load_overview_rows(self, path: Path, limit: int = 5000) -> list[dict[str, Any]]:
        # No close: the connection is the thread's cached one, and closing it
        # here would evict the object cache the reuse exists to keep.
        con = self._connect()
        rows = self._query_rows(con, f"SELECT * FROM read_parquet('{path.as_posix()}') LIMIT {limit}")
        return self._attach_cell_boundaries(rows)

    @staticmethod
    def _attach_cell_boundaries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Give every overview cell its true H3 boundary, clipped to dry land.

        Two problems are solved here.

        The map used to receive only a centre point and had the Node layer
        synthesise a hexagon from it with a hardcoded 0.0025-degree radius. An
        r8 cell is about 0.0048 degrees across, so every cell was drawn at 52%
        of its size and covered 27% of its own area -- the contiguous grid
        appeared as scattered dots with most of the city uncoloured between
        them. It also put geometry construction in the proxy, which is supposed
        to forward what this service computes rather than derive anything.

        The second is that the grid does not know where the city ends. Cells
        run up to 4.7 km offshore, and a cell over open water still gets a
        score: 170 amenities cells sit off land and 134 of them fall below 40,
        so the East River and Jamaica Bay were painted the same alarming red as
        an actually underserved block. That number is not a finding. There are
        no bodegas in the harbour because nobody lives there, not because the
        harbour is underserved, and a reader cannot tell those apart from the
        colour. Clipping to the shoreline removes the claim rather than
        restyling it.

        Emitted as GeoJSON [lng, lat] coordinates with an explicit type, since
        clipping an island-bearing cell yields a MultiPolygon.
        """
        try:
            import h3
        except ImportError:  # pragma: no cover - h3 is a hard dependency in practice
            return rows

        land = DirectQueryDataProvider._land_mask()

        kept: list[dict[str, Any]] = []
        for row in rows:
            cell = row.get("h3") or row.get("cell_id")
            if not cell:
                kept.append(row)
                continue
            try:
                ring = [[round(lng, 6), round(lat, 6)] for lat, lng in h3.cell_to_boundary(cell)]
            except Exception:
                # An unparseable index is a data problem, not a reason to fail
                # the whole overview; the client falls back for this one cell.
                kept.append(row)
                continue
            ring.append(ring[0])  # GeoJSON polygons must close

            if land is None:
                row["boundary"] = [ring]
                row["boundary_type"] = "Polygon"
                kept.append(row)
                continue

            clipped = DirectQueryDataProvider._clip_ring_to_land(ring, land)
            if clipped is None:
                # Essentially all water. Dropped rather than dimmed: keeping it
                # with a muted colour would still be asserting a score for a
                # patch of river.
                continue
            coords, geom_type, land_fraction = clipped
            row["boundary"] = coords
            row["boundary_type"] = geom_type
            row["land_fraction"] = round(land_fraction, 3)
            kept.append(row)
        return kept

    # Below this share of the cell on land, the cell is treated as water. Not
    # zero: floating-point slivers along the shoreline would otherwise keep
    # cells that are visually entirely river.
    _MIN_LAND_FRACTION = 0.03

    @staticmethod
    @lru_cache(maxsize=1)
    def _land_mask():
        """NYC land as one simplified geometry, or None if unavailable.

        The NTA layer is the city's own land partition -- all 262 polygons are
        dry land, including the park, cemetery, airport and Rikers types, and
        none of them are water -- so their union is the coastline.

        Simplified to ~11 m. The published shoreline carries 80,551 vertices,
        and clipping against it makes the overview payload nine times larger
        for detail finer than a pixel at any zoom the overlay is visible at;
        at 11 m the payload is roughly double the unclipped hexagons and the
        two are indistinguishable on screen.

        Returns None when the boundary file or geo stack is missing, in which
        case callers keep the unclipped hexagons -- a map with water cells is
        worse than one without, but far better than no overview at all.
        """
        path = Path(BOUNDARIES_DIR) / "nta_2020.geojson" if BOUNDARIES_DIR else None
        if path is None or not path.exists():
            logger.warning("Land mask unavailable: %s not found; overview cells will not be clipped", path)
            return None
        try:
            import geopandas as gpd

            nta = gpd.read_file(path)
            return nta.geometry.union_all().simplify(0.0001, preserve_topology=True)
        except Exception as exc:
            logger.warning("Land mask could not be built (%s); overview cells will not be clipped", exc)
            return None

    @staticmethod
    def _clip_ring_to_land(ring: list[list[float]], land) -> tuple[Any, str, float] | None:
        """Intersect one cell with the coastline.

        Returns (coordinates, geometry type, land fraction), or None when the
        cell is water. ``coordinates`` is always well-formed GeoJSON for the
        returned type -- a ring list for Polygon, a polygon list for
        MultiPolygon -- so a consumer can hand the pair straight to shape()
        without knowing whether anything was cut. Returning a bare ring for the
        untouched case would make the same declared type mean two different
        shapes, which is how the caller ends up guessing.
        """
        try:
            from shapely.geometry import Polygon, mapping
        except ImportError:  # pragma: no cover
            return [ring], "Polygon", 1.0

        try:
            hexagon = Polygon(ring)
            if not hexagon.is_valid:
                hexagon = hexagon.buffer(0)
            piece = hexagon.intersection(land)
            if piece.is_empty:
                return None
            fraction = piece.area / hexagon.area if hexagon.area else 0.0
            if fraction < DirectQueryDataProvider._MIN_LAND_FRACTION:
                return None
            # Fully inland cells are the common case and keep the original
            # hexagon: the intersection can add collinear vertices along a
            # nearby simplified edge, and there is no reason to pay for them
            # when nothing was cut.
            if fraction > 0.999:
                return [ring], "Polygon", 1.0
            geo = mapping(piece)
            coords = _round_coords(geo["coordinates"])
            return coords, geo["type"], fraction
        except Exception:
            return [ring], "Polygon", 1.0

    def _normalize_borough(self, value: Any) -> str | None:
        if value is None or str(value).strip() == "":
            return None
        text = str(value).strip().upper()
        return BoroughMap.get(text, text)

    def _normalize_zip(self, value: Any) -> str | None:
        if value is None or str(value).strip() == "":
            return None
        text = str(value).strip()
        if "." in text:
            text = text.split(".", 1)[0]
        digits = "".join(ch for ch in text if ch.isdigit())
        if not digits:
            return None
        return digits[:5].zfill(5)

    def _normalize_target_row(self, row: dict[str, Any]) -> dict[str, Any]:
        canonical = row.get("canonical_location_id")
        if canonical is None:
            bbl = row.get("BBL") or row.get("bbl") or row.get("appbbl")
            canonical = f"pluto_{bbl}" if bbl not in (None, "") else None
        matched_address = row.get("matched_address") or row.get("address")
        zip_code = (
            self._normalize_zip(row.get("zip"))
            or self._normalize_zip(row.get("zipcode"))
            or self._normalize_zip(row.get("postcode"))
        )
        return {
            "latitude": to_float(row.get("latitude")),
            "longitude": to_float(row.get("longitude")),
            "matched_address": matched_address,
            "borough": self._normalize_borough(row.get("borough")),
            "zip": zip_code,
            "canonical_location_id": canonical,
        }

    def _nearest_overview_cell(self, rows: list[dict[str, Any]], latitude: float, longitude: float) -> dict[str, Any] | None:
        # GPU path
        if self._use_gpu and rows:
            result = gpu_nearest_overview_cell(rows, latitude, longitude)
            if result is not None:
                return result
        best_row = None
        best_dist_sq = float("inf")
        for row in rows:
            cell_lat = to_float(row.get("latitude") or row.get("lat") or row.get("center_lat") or row.get("centroid_lat"))
            cell_lon = to_float(row.get("longitude") or row.get("lng") or row.get("center_lng") or row.get("centroid_lng"))
            if cell_lat is None or cell_lon is None:
                continue
            dist_sq = fast_distance_sq_m(latitude, longitude, cell_lat, cell_lon)
            if dist_sq < best_dist_sq:
                best_dist_sq = dist_sq
                best_row = row
        return best_row

    def get_overview_context(self, latitude: float, longitude: float) -> dict[str, Any] | None:
        if self.overview_dir is None or not self.overview_dir.exists():
            return None

        category_context: dict[str, Any] = {}
        overall_context: dict[str, Any] | None = None

        expected = {
            "overall": self.overview_dir / "overview_overall_h3_r8.parquet",
            **{key: self.overview_dir / f"overview_{key}_h3_r8.parquet" for key, value in CATEGORY_CONFIG.items() if value["map_driving"]},
        }

        for category_id, path in expected.items():
            if not path.exists():
                continue
            rows = self._load_overview_rows(path, limit=5000)
            nearest = self._nearest_overview_cell(rows, latitude, longitude)
            if not nearest:
                continue
            score = to_float(
                nearest.get("overall_score")
                if category_id == "overall"
                else nearest.get(f"{category_id}_score")
            )
            if score is None:
                score = to_float((nearest.get("category_scores") or {}).get(category_id)) if isinstance(nearest.get("category_scores"), dict) else None
            payload = {
                "score": score,
                "cell_id": nearest.get("h3") or nearest.get("cell_id"),
                "level": nearest.get("risk_level") or nearest.get("level"),
            }
            if category_id == "overall":
                overall_context = payload
            else:
                category_context[category_id] = payload

        if not overall_context and not category_context:
            return None

        return {
            "overall": overall_context,
            "categories": category_context,
        }

    def _raw_csv_path(self, name: str) -> Path | None:
        for relative in RAW_CSV_ALIASES.get(name, []):
            candidate = RAW_DATA_ROOT / relative
            if candidate.exists():
                return candidate
        return None

    def _query_ready_radius_rows(
        self,
        con: Any,
        relative_path: str,
        latitude: float,
        longitude: float,
        radius_m: float,
        columns: list[str],
        limit: int = 50000,
    ) -> list[dict[str, Any]]:
        path = self._ready_path(relative_path)
        if not path.exists():
            return []
        # Use H3 pre-filter to dramatically reduce scan scope
        h3_cells = self._h3_cells_for_radius(latitude, longitude, int(radius_m))
        min_lat, max_lat, min_lon, max_lon = bbox(latitude, longitude, radius_m)
        available_columns = {
            row[0]
            for row in con.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{path.as_posix()}')"
            ).fetchall()
        }
        selected_parts: list[str] = []
        for requested in columns:
            actual = READY_COLUMN_ALIASES.get(requested, requested)
            if actual in available_columns:
                selected_parts.append(f"{quote(actual)} AS {quote(requested)}")
            else:
                # Some display-only address fields were intentionally omitted
                # from the compact ready layer. Preserve the response key while
                # avoiding a multi-gigabyte raw CSV fallback.
                selected_parts.append(f"NULL AS {quote(requested)}")
        selected = ", ".join(selected_parts)
        if selected:
            selected += ", "
        if h3_cells:
            h3_placeholders = ", ".join(["?"] * len(h3_cells))
            sql = f"""
                SELECT {selected}
                       try_cast(latitude AS DOUBLE) AS __lat,
                       try_cast(longitude AS DOUBLE) AS __lon,
                       h3_r9
                FROM read_parquet('{path.as_posix()}')
                WHERE h3_r9 IN ({h3_placeholders})
                LIMIT {limit}
            """
            rows = self._query_rows(con, sql, h3_cells)
        else:
            sql = f"""
                SELECT {selected}
                       try_cast(latitude AS DOUBLE) AS __lat,
                       try_cast(longitude AS DOUBLE) AS __lon,
                       h3_r9
                FROM read_parquet('{path.as_posix()}')
                WHERE try_cast(latitude AS DOUBLE) BETWEEN ? AND ?
                  AND try_cast(longitude AS DOUBLE) BETWEEN ? AND ?
                LIMIT {limit}
            """
            rows = self._query_rows(con, sql, [min_lat, max_lat, min_lon, max_lon])
        radius_sq = radius_m * radius_m
        return [
            row
            for row in rows
            if row.get("__lat") is not None
            and row.get("__lon") is not None
            and fast_distance_sq_m(latitude, longitude, float(row["__lat"]), float(row["__lon"])) <= radius_sq
        ]

    def _query_ready_h3_score(self, con: Any, relative_path: str, h3_cells: list[str]) -> int | None:
        path = self._ready_path(relative_path)
        if not path.exists() or not h3_cells:
            return None
        placeholders = ", ".join(["?"] * len(h3_cells))
        sql = f"SELECT avg(try_cast(score AS DOUBLE)) AS avg_score FROM read_parquet('{path.as_posix()}') WHERE h3_r9 IN ({placeholders})"
        rows = self._query_rows(con, sql, h3_cells)
        if not rows or rows[0].get("avg_score") is None:
            return None
        return round(float(rows[0]["avg_score"]))

    def _query_ready_zip_score(self, con: Any, relative_path: str, zip_code: str | None) -> int | None:
        path = self._ready_path(relative_path)
        if not path.exists() or not zip_code:
            return None
        sql = f"SELECT avg(try_cast(score AS DOUBLE)) AS avg_score FROM read_parquet('{path.as_posix()}') WHERE zip = ?"
        rows = self._query_rows(con, sql, [zip_code])
        if not rows or rows[0].get("avg_score") is None:
            return None
        return round(float(rows[0]["avg_score"]))

    def _query_ready_quarterly(self, con: Any, relative_path: str, h3_cells: list[str]) -> dict[str, Any] | None:
        path = self._ready_path(relative_path)
        if not path.exists() or not h3_cells:
            return None
        placeholders = ", ".join(["?"] * len(h3_cells))
        sql = f"""
            SELECT quarter, sum(try_cast(count AS DOUBLE)) AS count
            FROM read_parquet('{path.as_posix()}')
            WHERE h3_r9 IN ({placeholders})
            GROUP BY quarter
            ORDER BY quarter
        """
        rows = self._query_rows(con, sql, h3_cells)
        if not rows:
            return None
        quarterly_values = [int(round(to_float(row.get("count")) or 0)) for row in rows]
        return {
            "last_30d": None,
            "prev_30d": None,
            "last_90d": None,
            "same_90d_last_year": None,
            "quarterly_values": quarterly_values,
        }

    def _h3_cells_for_radius(self, latitude: float, longitude: float, radius_m: int) -> list[str]:
        """Compute nearby H3 cells using h3 library instead of scanning large tables."""
        try:
            import h3
            center = h3.latlng_to_cell(latitude, longitude, 9)
            k = max(1, radius_m // 174)  # H3 res 9 ~ 174m per cell
            return list(h3.grid_disk(center, k))
        except Exception:
            return []

    def _collect_prepared_scores(self, con: Any, latitude: float, longitude: float, radius_m: int, zip_code: str | None) -> dict[str, dict[str, int | None]]:
        prepared_scores: dict[str, dict[str, int | None]] = {}
        # Pre-compute H3 cells once for all datasets — no large table scanning needed
        h3_cells_by_radius: dict[int, list[str]] = {}
        for category_id, category_cfg in CATEGORY_CONFIG.items():
            category_scores: dict[str, int | None] = {}
            for sub_name, sub_cfg in category_cfg.get("sub_datasets", {}).items():
                query_by = sub_cfg.get("query_by")
                score_table = sub_cfg.get("score_table")
                if not score_table:
                    category_scores[sub_name] = None
                    continue
                if query_by == "zip":
                    category_scores[sub_name] = self._query_ready_zip_score(con, score_table, zip_code)
                    continue
                # Use H3 grid_disk instead of scanning indexed tables
                radius_for_table = min(max(radius_m, 100), 250) if category_id == "building" else radius_m
                if radius_for_table not in h3_cells_by_radius:
                    h3_cells_by_radius[radius_for_table] = self._h3_cells_for_radius(latitude, longitude, radius_for_table)
                nearby_cells = h3_cells_by_radius[radius_for_table]
                category_scores[sub_name] = self._query_ready_h3_score(con, score_table, nearby_cells)
            if any(value is not None for value in category_scores.values()):
                prepared_scores[category_id] = category_scores
        return prepared_scores

    def _fetch_radius_rows(
        self,
        con: Any,
        parquet_name: str,
        lat_col: str,
        lon_col: str,
        lat: float,
        lon: float,
        radius_m: float,
        columns: list[str],
        limit: int = 50000,
    ) -> list[dict[str, Any]]:
        ready_relative = READY_DATASET_PATHS.get(parquet_name)
        if ready_relative and self._ready_exists(ready_relative):
            return self._query_ready_radius_rows(
                con,
                ready_relative,
                lat,
                lon,
                radius_m,
                columns,
                limit,
            )

        # GPU path: cuDF reads parquet directly, all filtering on GPU
        if self._use_gpu:
            path = self._parquet_path(parquet_name)
            result = gpu_fetch_radius_rows(path, lat_col, lon_col, lat, lon, radius_m, columns, limit)
            if not is_fallback(result):
                return result
            # GPU couldn't handle this file — fall through to DuckDB

        source_sql = self._source_sql(parquet_name)
        if source_sql is None:
            return []
        min_lat, max_lat, min_lon, max_lon = bbox(lat, lon, radius_m)
        selected = ", ".join(quote(col) for col in columns)
        if selected:
            selected += ", "
        sql = f"""
            SELECT {selected}
                   try_cast({quote(lat_col)} AS DOUBLE) AS __lat,
                   try_cast({quote(lon_col)} AS DOUBLE) AS __lon
            FROM {source_sql}
            WHERE try_cast({quote(lat_col)} AS DOUBLE) BETWEEN ? AND ?
              AND try_cast({quote(lon_col)} AS DOUBLE) BETWEEN ? AND ?
            LIMIT {limit}
        """
        rows = self._query_rows(con, sql, [min_lat, max_lat, min_lon, max_lon])
        radius_sq = radius_m * radius_m
        return [
            row
            for row in rows
            if row.get("__lat") is not None
            and row.get("__lon") is not None
            and fast_distance_sq_m(lat, lon, float(row["__lat"]), float(row["__lon"])) <= radius_sq
        ]

    def _nearest_location(self, con: Any, latitude: float, longitude: float) -> dict[str, Any]:
        min_lat, max_lat, min_lon, max_lon = bbox(latitude, longitude, 2500)
        location_index_path = self._parquet_path("location_index")
        if location_index_path.exists():
            sql = f"""
                SELECT *,
                       pow(latitude - ?, 2) + pow(longitude - ?, 2) AS __dist
                FROM read_parquet('{location_index_path.as_posix()}')
                WHERE latitude BETWEEN ? AND ?
                  AND longitude BETWEEN ? AND ?
                ORDER BY __dist
                LIMIT 1
            """
            rows = self._query_rows(con, sql, [latitude, longitude, min_lat, max_lat, min_lon, max_lon])
            if rows:
                normalized = self._normalize_target_row(rows[0])
                if normalized.get("zip") and normalized.get("borough"):
                    return normalized

        pluto_source = self._source_sql("pluto")
        if pluto_source:
            pluto_sql = f"""
                SELECT address, borough, postcode, BBL, latitude, longitude,
                       pow(latitude - ?, 2) + pow(longitude - ?, 2) AS __dist
                FROM {pluto_source}
                WHERE latitude BETWEEN ? AND ?
                  AND longitude BETWEEN ? AND ?
                ORDER BY __dist
                LIMIT 1
            """
            rows = self._query_rows(con, pluto_sql, [latitude, longitude, min_lat, max_lat, min_lon, max_lon])
            if rows:
                return self._normalize_target_row(rows[0])

        return {
            "latitude": latitude,
            "longitude": longitude,
            "matched_address": None,
            "borough": None,
            "zip": None,
            "canonical_location_id": None,
        }

    def _query_emergency_zip(self, con: Any, zip_code: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
        """Query EMS and Fire dispatch data aggregated by ZIP.

        Priority: ready score tables (tiny) > processed parquet > raw CSV.
        """
        metrics = {"ems_avg_response_seconds": None, "fire_avg_response_seconds": None}
        evidence: list[dict[str, Any]] = []
        gaps: list[str] = []
        if not zip_code:
            gaps.append("No ZIP context available for EMS/Fire aggregate lookup.")
            return metrics, evidence, gaps

        # Try ready tables first (7KB each — instant)
        ems_ready = self._ready_path("safety/ems_scores_zip.parquet")
        if ems_ready.exists():
            try:
                row = con.execute(
                    f"SELECT avg_response_seconds, incident_count FROM read_parquet('{ems_ready.as_posix()}') WHERE zip = ? LIMIT 1",
                    [zip_code],
                ).fetchone()
                if row and row[0]:
                    metrics["ems_avg_response_seconds"] = round(float(row[0]))
                    evidence.append({
                        "evidence_id": "ems_response",
                        "source": "NYC EMS Incident Dispatch Data (ready)",
                        "date": "local extract",
                        "summary": f"ZIP {zip_code} EMS average incident response is about {metrics['ems_avg_response_seconds']} seconds across {int(row[1] or 0)} record(s).",
                        "record_ref": zip_code,
                    })
            except Exception:
                pass

        fire_ready = self._ready_path("safety/fire_scores_zip.parquet")
        if fire_ready.exists():
            try:
                row = con.execute(
                    f"SELECT avg_response_seconds, incident_count FROM read_parquet('{fire_ready.as_posix()}') WHERE zip = ? LIMIT 1",
                    [zip_code],
                ).fetchone()
                if row and row[0]:
                    metrics["fire_avg_response_seconds"] = round(float(row[0]))
                    evidence.append({
                        "evidence_id": "fire_response",
                        "source": "NYC Fire Incident Dispatch Data (ready)",
                        "date": "local extract",
                        "summary": f"ZIP {zip_code} Fire average incident response is about {metrics['fire_avg_response_seconds']} seconds across {int(row[1] or 0)} record(s).",
                        "record_ref": zip_code,
                    })
            except Exception:
                pass

        # If ready tables provided both, return early — skip large table scanning
        if metrics["ems_avg_response_seconds"] is not None and metrics["fire_avg_response_seconds"] is not None:
            return metrics, evidence, gaps

        # Fall back to processed/raw tables only for missing metrics
        ems_path = self._parquet_path("ems_dispatch")
        if metrics["ems_avg_response_seconds"] is None and ems_path.exists():
            # GPU path: cuDF aggregation on parquet (skip DuckDB if successful)
            if self._use_gpu:
                avg, cnt = gpu_emergency_metrics(ems_path, zip_code,
                                                 response_col="INCIDENT_RESPONSE_SECONDS_QY",
                                                 zip_col="ZIPCODE")
                if avg is not None:
                    metrics["ems_avg_response_seconds"] = round(avg)
                    evidence.append({
                        "evidence_id": "ems_response",
                        "source": "NYC EMS Incident Dispatch Data (GPU)",
                        "date": "local extract",
                        "summary": f"ZIP {zip_code} EMS average incident response is about {metrics['ems_avg_response_seconds']} seconds across {cnt} record(s).",
                        "record_ref": zip_code,
                    })

            # DuckDB fallback (runs if GPU unavailable or returned None)
            if metrics["ems_avg_response_seconds"] is None:
                # Try raw dispatch format first (INCIDENT_RESPONSE_SECONDS_QY, ZIPCODE)
                try:
                    row = con.execute(
                        f"""SELECT avg(try_cast(INCIDENT_RESPONSE_SECONDS_QY AS DOUBLE)) as avg_resp,
                                   count(*) as cnt
                            FROM read_parquet('{ems_path.as_posix()}')
                            WHERE try_cast(ZIPCODE AS VARCHAR) = ?
                              AND try_cast(INCIDENT_RESPONSE_SECONDS_QY AS DOUBLE) > 0""",
                        [zip_code],
                    ).fetchone()
                except Exception:
                    # Fall back to pre-aggregated format
                    row = con.execute(
                        f"SELECT avg_incident_response_seconds, incident_count FROM read_parquet('{ems_path.as_posix()}') WHERE zipcode = ? LIMIT 1",
                        [zip_code],
                    ).fetchone()
                if row and row[0]:
                    metrics["ems_avg_response_seconds"] = round(to_float(row[0]) or 0)
                    evidence.append({
                        "evidence_id": "ems_response",
                        "source": "NYC EMS Incident Dispatch Data",
                        "date": "local extract",
                        "summary": f"ZIP {zip_code} EMS average incident response is about {metrics['ems_avg_response_seconds']} seconds across {to_int(row[1])} record(s).",
                        "record_ref": zip_code,
                    })
                else:
                    gaps.append(f"No EMS data found for ZIP {zip_code}.")
        else:
            raw_ems = self._raw_csv_path("ems_dispatch")
            if raw_ems and raw_ems.exists():
                # GPU path for raw CSV converted to parquet won't work — CSV stays DuckDB only
                row = con.execute(
                    f"""SELECT avg(try_cast(INCIDENT_RESPONSE_SECONDS_QY AS DOUBLE)) as avg_resp,
                               count(*) as cnt
                        FROM read_csv_auto('{raw_ems.as_posix()}', ignore_errors=true)
                        WHERE try_cast(ZIPCODE AS VARCHAR) = ?
                          AND try_cast(INCIDENT_RESPONSE_SECONDS_QY AS DOUBLE) > 0""",
                    [zip_code],
                ).fetchone()
                if row and row[0]:
                    metrics["ems_avg_response_seconds"] = round(to_float(row[0]) or 0)
                    evidence.append({
                        "evidence_id": "ems_response",
                        "source": "NYC EMS Incident Dispatch Data raw CSV",
                        "date": "local extract",
                        "summary": f"ZIP {zip_code} EMS average incident response is about {metrics['ems_avg_response_seconds']} seconds across {to_int(row[1])} record(s).",
                        "record_ref": zip_code,
                    })
                else:
                    gaps.append(f"No EMS data found for ZIP {zip_code}.")
            else:
                gaps.append("EMS dispatch parquet is missing.")

        fire_path = self._parquet_path("fire_dispatch")
        if fire_path.exists():
            # GPU path: cuDF aggregation on parquet (skip DuckDB if successful)
            if self._use_gpu and metrics["fire_avg_response_seconds"] is None:
                avg, cnt = gpu_emergency_metrics(fire_path, zip_code,
                                                 response_col="INCIDENT_RESPONSE_SECONDS_QY",
                                                 zip_col="ZIPCODE")
                if avg is not None:
                    metrics["fire_avg_response_seconds"] = round(avg)
                    evidence.append({
                        "evidence_id": "fire_response",
                        "source": "NYC Fire Incident Dispatch Data (GPU)",
                        "date": "local extract",
                        "summary": f"ZIP {zip_code} Fire average incident response is about {metrics['fire_avg_response_seconds']} seconds across {cnt} record(s).",
                        "record_ref": zip_code,
                    })

            # DuckDB fallback (runs if GPU unavailable or returned None)
            if metrics["fire_avg_response_seconds"] is None:
                try:
                    row = con.execute(
                        f"""SELECT avg(try_cast(INCIDENT_RESPONSE_SECONDS_QY AS DOUBLE)) as avg_resp,
                                   count(*) as cnt
                            FROM read_parquet('{fire_path.as_posix()}')
                            WHERE try_cast(ZIPCODE AS VARCHAR) = ?
                              AND try_cast(INCIDENT_RESPONSE_SECONDS_QY AS DOUBLE) > 0""",
                        [zip_code],
                    ).fetchone()
                except Exception:
                    row = con.execute(
                        f"SELECT avg_incident_response_seconds, incident_count FROM read_parquet('{fire_path.as_posix()}') WHERE zipcode = ? LIMIT 1",
                        [zip_code],
                    ).fetchone()
                if row and row[0]:
                    metrics["fire_avg_response_seconds"] = round(to_float(row[0]) or 0)
                    evidence.append({
                        "evidence_id": "fire_response",
                        "source": "NYC Fire Incident Dispatch Data",
                        "date": "local extract",
                        "summary": f"ZIP {zip_code} Fire average incident response is about {metrics['fire_avg_response_seconds']} seconds across {to_int(row[1])} record(s).",
                        "record_ref": zip_code,
                    })
                else:
                    gaps.append(f"No Fire data found for ZIP {zip_code}.")
        else:
            raw_fire = self._raw_csv_path("fire_dispatch")
            if raw_fire and raw_fire.exists():
                # GPU path for raw CSV converted to parquet won't work — CSV stays DuckDB only
                row = con.execute(
                    f"""SELECT avg(try_cast(INCIDENT_RESPONSE_SECONDS_QY AS DOUBLE)) as avg_resp,
                               count(*) as cnt
                        FROM read_csv_auto('{raw_fire.as_posix()}', ignore_errors=true)
                        WHERE try_cast(ZIPCODE AS VARCHAR) = ?
                          AND try_cast(INCIDENT_RESPONSE_SECONDS_QY AS DOUBLE) > 0""",
                    [zip_code],
                ).fetchone()
                if row and row[0]:
                    metrics["fire_avg_response_seconds"] = round(to_float(row[0]) or 0)
                    evidence.append({
                        "evidence_id": "fire_response",
                        "source": "NYC Fire Incident Dispatch Data raw CSV",
                        "date": "local extract",
                        "summary": f"ZIP {zip_code} Fire average incident response is about {metrics['fire_avg_response_seconds']} seconds across {to_int(row[1])} record(s).",
                        "record_ref": zip_code,
                    })
                else:
                    gaps.append(f"No Fire data found for ZIP {zip_code}.")
            else:
                gaps.append("Fire dispatch parquet is missing.")
        return metrics, evidence, gaps

    def _query_collision(self, con: Any, latitude: float, longitude: float, radius_m: int, time_window_days: int) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        rows_500 = self._fetch_radius_rows(
            con,
            "collisions",
            "LATITUDE",
            "LONGITUDE",
            latitude,
            longitude,
            radius_m,
            ["CRASH DATE", "COLLISION_ID", "ON STREET NAME", "NUMBER OF PEDESTRIANS INJURED", "NUMBER OF CYCLIST INJURED"],
        )
        recent_500 = [row for row in rows_500 if is_within_days(row.get("CRASH DATE"), time_window_days)]
        rows_1000 = self._fetch_radius_rows(
            con,
            "collisions",
            "LATITUDE",
            "LONGITUDE",
            latitude,
            longitude,
            max(radius_m, 1000),
            ["CRASH DATE", "COLLISION_ID", "ON STREET NAME", "NUMBER OF PEDESTRIANS INJURED", "NUMBER OF CYCLIST INJURED"],
        )
        recent_1000 = [row for row in rows_1000 if is_within_days(row.get("CRASH DATE"), time_window_days)]
        injuries = sum(to_int(row.get("NUMBER OF PEDESTRIANS INJURED")) + to_int(row.get("NUMBER OF CYCLIST INJURED")) for row in recent_1000)
        evidence = []
        if recent_500:
            evidence.append({
                "evidence_id": "collisions_500m",
                "source": "NYC Motor Vehicle Collisions",
                "date": f"last {time_window_days} days in local extract",
                "summary": f"{len(recent_500)} collision record(s) within {radius_m}m.",
                "record_ref": f"radius_{radius_m}m",
            })
        detail = {
            "map_points": compact_records(
                (
                    {
                        "kind": "transit",
                        "latitude": float(row["__lat"]),
                        "longitude": float(row["__lon"]),
                        "summary": f"Collision {row.get('COLLISION_ID', '')} on {row.get('CRASH DATE', '')}",
                        "score_hint": max(0, min(100, 60
                            - min(int(row.get("NUMBER OF PERSONS INJURED") or 0), 5) * 10
                            - min(int(row.get("NUMBER OF PERSONS KILLED") or 0), 1) * 30
                        )),
                    }
                    for row in recent_500
                ),
                25,
            ),
            "recent_incidents": compact_records(
                (
                    {
                        "kind": "collision",
                        "date": row.get("CRASH DATE"),
                        "summary": f"Collision near {row.get('ON STREET NAME') or 'the selected point'}",
                    }
                    for row in recent_500
                ),
                10,
            ),
        }
        detail["_raw_rows"] = rows_500  # for enriched context extraction
        return {"collision_count_500m": len(recent_500), "ped_cyclist_injuries_1km": injuries}, evidence, detail

    def _query_rodent(self, con: Any, latitude: float, longitude: float, radius_m: int, time_window_days: int) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        rows = self._fetch_radius_rows(
            con,
            "rodent_inspections",
            "LATITUDE",
            "LONGITUDE",
            latitude,
            longitude,
            radius_m,
            ["INSPECTION_DATE", "RESULT", "JOB_ID", "HOUSE_NUMBER", "STREET_NAME"],
        )
        recent = [row for row in rows if is_within_days(row.get("INSPECTION_DATE"), time_window_days)]
        positive = [row for row in recent if any(term in str(row.get("RESULT") or "").upper() for term in POSITIVE_RODENT_TERMS)]
        evidence = []
        if positive:
            evidence.append({
                "evidence_id": "rodent_500m",
                "source": "NYC Rodent Inspections",
                "date": f"last {time_window_days} days in local extract",
                "summary": f"{len(positive)} nearby rodent inspection record(s) were flagged positive or active within {radius_m}m.",
                "record_ref": f"radius_{radius_m}m",
            })
        detail = {
            "map_points": compact_records(
                (
                    {
                        "kind": "safety",
                        "latitude": float(row["__lat"]),
                        "longitude": float(row["__lon"]),
                        "summary": f"Rodent inspection: {row.get('RESULT') or 'result unavailable'}",
                        "score_hint": 20 if "RAT" in str(row.get("RESULT") or "").upper() else 35,
                    }
                    for row in positive
                ),
                20,
            ),
            "recent_incidents": compact_records(
                (
                    {
                        "kind": "rodent",
                        "date": row.get("INSPECTION_DATE"),
                        "summary": f"Rodent inspection at {row.get('HOUSE_NUMBER') or ''} {row.get('STREET_NAME') or ''}".strip(),
                    }
                    for row in positive
                ),
                10,
            ),
        }
        return {"rodent_positive_500m": len(positive), "rodent_total_500m": len(recent)}, evidence, detail

    def _query_sanitation_311(self, con: Any, latitude: float, longitude: float, radius_m: int, time_window_days: int) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        schema_variants = [
            {
                "lat_col": "Latitude",
                "lon_col": "Longitude",
                "date_col": "Created Date",
                "type_col": "Problem (formerly Complaint Type)",
                "desc_col": "Problem Detail (formerly Descriptor)",
                "addr_col": "Incident Address",
                "id_col": "Unique Key",
            },
            {
                "lat_col": "latitude",
                "lon_col": "longitude",
                "date_col": "created_date",
                "type_col": "complaint_type",
                "desc_col": "descriptor",
                "addr_col": "incident_address",
                "id_col": "unique_key",
            },
        ]
        rows: list[dict[str, Any]] = []
        active_variant: dict[str, str] | None = None

        # The published ready table is already reduced to the supported safety
        # complaint types. Query it before the raw CSV-specific SQL variants.
        ready_311 = READY_DATASET_PATHS.get("311_subset")
        if ready_311 and self._ready_exists(ready_311):
            variant = schema_variants[0]
            rows = self._fetch_radius_rows(
                con,
                "311_subset",
                variant["lat_col"],
                variant["lon_col"],
                latitude,
                longitude,
                radius_m,
                [variant["date_col"], variant["type_col"], variant["desc_col"], variant["addr_col"], variant["id_col"]],
                limit=10000,
            )
            active_variant = variant

        # GPU-accelerated path: _fetch_radius_rows has cuDF path, then filter type in Python
        if active_variant is None and self._use_gpu:
            for variant in schema_variants:
                try:
                    gpu_rows = self._fetch_radius_rows(
                        con,
                        "311_subset",
                        variant["lat_col"],
                        variant["lon_col"],
                        latitude,
                        longitude,
                        radius_m,
                        [variant["date_col"], variant["type_col"], variant["desc_col"], variant["addr_col"], variant["id_col"]],
                        limit=10000,
                    )
                    if gpu_rows and not is_fallback(gpu_rows):
                        # Filter by sanitation complaint types in Python (GPU path doesn't support WHERE)
                        rows = [r for r in gpu_rows if str(r.get(variant["type_col"]) or "").upper() in SAFETY_311_TYPES]
                        active_variant = variant
                        break
                except Exception:
                    continue

        # DuckDB path: pre-filtered SQL query (pushes type filter to parquet/DuckDB)
        if active_variant is None:
            safety_types_sql = ", ".join(f"'{t}'" for t in SAFETY_311_TYPES)
            min_lat, max_lat, min_lon, max_lon = bbox(latitude, longitude, radius_m)
            source_sql = self._source_sql("311_subset")
            if source_sql:
                for variant in schema_variants:
                    try:
                        sql = f"""
                            SELECT {quote(variant['date_col'])}, {quote(variant['type_col'])},
                                   {quote(variant['desc_col'])}, {quote(variant['addr_col'])},
                                   {quote(variant['id_col'])},
                                   try_cast({quote(variant['lat_col'])} AS DOUBLE) AS __lat,
                                   try_cast({quote(variant['lon_col'])} AS DOUBLE) AS __lon
                            FROM {source_sql}
                            WHERE try_cast({quote(variant['lat_col'])} AS DOUBLE) BETWEEN ? AND ?
                              AND try_cast({quote(variant['lon_col'])} AS DOUBLE) BETWEEN ? AND ?
                              AND upper(try_cast({quote(variant['type_col'])} AS VARCHAR)) IN ({safety_types_sql})
                            LIMIT 10000
                        """
                        raw = self._query_rows(con, sql, [min_lat, max_lat, min_lon, max_lon])
                        radius_sq = radius_m * radius_m
                        rows = [
                            row for row in raw
                            if row.get("__lat") is not None and row.get("__lon") is not None
                            and fast_distance_sq_m(latitude, longitude, float(row["__lat"]), float(row["__lon"])) <= radius_sq
                        ]
                        active_variant = variant
                        break
                    except Exception:
                        continue

        # Final fallback: unfiltered radius query (slow but works for any schema)
        if active_variant is None:
            for variant in schema_variants:
                try:
                    rows = self._fetch_radius_rows(
                        con,
                        "311_subset",
                        variant["lat_col"],
                        variant["lon_col"],
                        latitude,
                        longitude,
                        radius_m,
                        [variant["date_col"], variant["type_col"], variant["desc_col"], variant["addr_col"], variant["id_col"]],
                        limit=10000,
                    )
                    active_variant = variant
                    break
                except Exception:
                    continue
        if active_variant is None:
            return {"sanitation_311_recent_count": 0}, [], {"recent_incidents": []}
        recent = [
            row
            for row in rows
            if str(row.get(active_variant["type_col"]) or "").upper() in SAFETY_311_TYPES
            and is_within_days(row.get(active_variant["date_col"]), time_window_days)
        ]
        evidence = []
        if recent:
            evidence.append({
                "evidence_id": "311_sanitation_500m",
                "source": "NYC 311 Service Requests",
                "date": f"last {time_window_days} days in local extract",
                "summary": f"{len(recent)} rodent/sanitation 311 complaint(s) within {radius_m}m.",
                "record_ref": f"radius_{radius_m}m",
            })
        detail = {
            "map_points": compact_records(
                (
                    {
                        "kind": "311",
                        "latitude": float(row["__lat"]),
                        "longitude": float(row["__lon"]),
                        "summary": f"311: {row.get(active_variant['type_col'])} — {row.get(active_variant['desc_col']) or 'No descriptor'}",
                        "score_hint": 25,
                    }
                    for row in recent
                    if "__lat" in row and "__lon" in row
                ),
                15,
            ),
            "recent_incidents": compact_records(
                (
                    {
                        "kind": "311",
                        "date": row.get(active_variant["date_col"]),
                        "summary": f"{row.get(active_variant['type_col'])}: {row.get(active_variant['desc_col']) or 'No descriptor'}",
                    }
                    for row in recent
                ),
                10,
            )
        }
        detail["_raw_rows"] = rows  # for enriched context extraction
        return {"sanitation_311_recent_count": len(recent)}, evidence, detail

    def _query_facilities(self, con: Any, latitude: float, longitude: float, radius_m: int, zip_code: str, time_window_days: int) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, list[dict[str, Any]]], list[str]]:
        metrics = {
            "park_acres_zip_proxy": 0.0,
            "tree_count_500m": 0,
            "toilet_count_1km": 0,
            "linknyc_count_500m": 0,
            "restaurant_count_500m": 0,
            "restaurant_critical_rate_500m": 0.0,
        }
        evidence: list[dict[str, Any]] = []
        gaps: list[str] = []
        nearby_facilities: list[dict[str, Any]] = []

        _parks_rows: list[dict[str, Any]] = []
        parks_source = self._source_sql("parks")
        if parks_source and zip_code:
            rows = self._query_rows(
                con,
                f"SELECT ACRES, NAME311, SIGNNAME, WATERFRONT, ZIPCODE FROM {parks_source} WHERE try_cast(ZIPCODE AS VARCHAR) = ? LIMIT 1000",
                [zip_code],
            )
            _parks_rows = rows
            metrics["park_acres_zip_proxy"] = round(sum(to_float(row.get("ACRES")) or 0 for row in rows), 2)
            if metrics["park_acres_zip_proxy"] > 0:
                evidence.append({
                    "evidence_id": "parks_zip",
                    "source": "NYC Parks Properties",
                    "date": "current extract",
                "summary": f"{metrics['park_acres_zip_proxy']} park acre(s) found in ZIP {zip_code} as an amenities proxy.",
                    "record_ref": zip_code,
                })
        else:
            gaps.append("Parks ZIP proxy unavailable for the selected point.")

        tree_rows = self._fetch_radius_rows(con, "street_trees", "latitude", "longitude", latitude, longitude, radius_m, ["tree_id", "spc_common", "health", "status"])
        alive_trees = [row for row in tree_rows if str(row.get("status") or "").upper() == "ALIVE"]
        metrics["tree_count_500m"] = len(alive_trees)
        if alive_trees:
            evidence.append({
                "evidence_id": "trees_500m",
                "source": "NYC Street Tree Census",
                "date": "current extract",
                "summary": f"{len(alive_trees)} living street tree record(s) within 500m.",
                "record_ref": "radius_500m",
            })
            _health_scores = {"good": 85, "fair": 60, "poor": 30, "unknown": 50}
            for row in alive_trees[:15]:
                _h = str(row.get("health") or "unknown").lower()
                nearby_facilities.append({
                    "kind": "tree",
                    "latitude": float(row["__lat"]),
                    "longitude": float(row["__lon"]),
                    "summary": f"{row.get('spc_common') or 'Tree'} ({_h})",
                    "score_hint": _health_scores.get(_h, 50),
                })

        toilet_rows = self._fetch_radius_rows(con, "public_toilets", "Latitude", "Longitude", latitude, longitude, max(radius_m, 1000), ["Facility Name", "Status", "Location Type"])
        toilet_rows = [
            row for row in toilet_rows
            if str(row.get("Status") or "").strip().upper() == "OPERATIONAL"
        ]
        metrics["toilet_count_1km"] = len(toilet_rows)
        if toilet_rows:
            evidence.append({
                "evidence_id": "toilets_1km",
                "source": "NYC Public Toilets",
                "date": "current extract",
                "summary": f"{len(toilet_rows)} public toilet location(s) found within 1km.",
                "record_ref": "radius_1km",
            })
            for row in toilet_rows[:10]:
                nearby_facilities.append({
                    "kind": "toilet",
                    "latitude": float(row["__lat"]),
                    "longitude": float(row["__lon"]),
                    "summary": f"Public toilet: {row.get('Facility Name') or 'facility'}",
                    "score_hint": 75,
                })

        link_rows = self._fetch_radius_rows(con, "linknyc_locations", "Latitude", "Longitude", latitude, longitude, radius_m, ["Site ID", "Street Address", "Installation Status"])
        link_rows = [
            row for row in link_rows
            if str(row.get("Installation Status") or "").strip().upper() == "LIVE"
        ]
        metrics["linknyc_count_500m"] = len(link_rows)
        if link_rows:
            evidence.append({
                "evidence_id": "linknyc_500m",
                "source": "LinkNYC Locations",
                "date": "current extract",
                "summary": f"{len(link_rows)} LinkNYC kiosk location(s) within 500m.",
                "record_ref": "radius_500m",
            })
            for row in link_rows[:10]:
                nearby_facilities.append({
                    "kind": "linknyc",
                    "latitude": float(row["__lat"]),
                    "longitude": float(row["__lon"]),
                    "summary": f"LinkNYC kiosk at {row.get('Street Address') or 'nearby street'}",
                    "score_hint": 70,
                })

        restaurant_rows = self._fetch_radius_rows(con, "restaurant_inspections", "Latitude", "Longitude", latitude, longitude, radius_m, ["CAMIS", "DBA", "INSPECTION DATE", "CRITICAL FLAG", "GRADE"])
        recent_restaurants = [row for row in restaurant_rows if is_within_days(row.get("INSPECTION DATE"), time_window_days)]
        distinct_restaurant_ids = {
            str(row.get("CAMIS") or row.get("DBA") or "").strip()
            for row in recent_restaurants
            if row.get("CAMIS") or row.get("DBA")
        }
        metrics["restaurant_count_500m"] = len(distinct_restaurant_ids)
        if recent_restaurants:
            critical = [row for row in recent_restaurants if str(row.get("CRITICAL FLAG") or "").strip().upper() == "CRITICAL"]
            metrics["restaurant_critical_rate_500m"] = round(len(critical) / max(1, len(recent_restaurants)), 3)
            evidence.append({
                "evidence_id": "restaurants_500m",
                "source": "NYC Restaurant Inspection Results",
                "date": f"last {time_window_days} days in local extract",
                "summary": f"{len(distinct_restaurant_ids)} distinct restaurant(s) across {len(recent_restaurants)} nearby inspection record(s), with critical rate {metrics['restaurant_critical_rate_500m']:.0%}.",
                "record_ref": "radius_500m",
            })
            representative_restaurants: list[dict[str, Any]] = []
            seen_restaurant_ids: set[str] = set()
            for row in recent_restaurants:
                entity_id = str(row.get("CAMIS") or row.get("DBA") or "").strip()
                if not entity_id or entity_id in seen_restaurant_ids:
                    continue
                seen_restaurant_ids.add(entity_id)
                representative_restaurants.append(row)
                if len(representative_restaurants) == 10:
                    break
            for row in representative_restaurants:
                _grade = str(row.get("GRADE") or "").strip().upper()
                _critical = str(row.get("CRITICAL FLAG") or "").strip().upper() == "CRITICAL"
                _rhint = 80 if _grade == "A" else 60 if _grade == "B" else 40
                if _critical:
                    _rhint = max(20, _rhint - 25)
                nearby_facilities.append({
                    "kind": "restaurant",
                    "latitude": float(row["__lat"]),
                    "longitude": float(row["__lon"]),
                    "summary": f"{row.get('DBA') or 'Restaurant'} grade {row.get('GRADE') or 'n/a'}",
                    "score_hint": _rhint,
                })

        return metrics, evidence, {
            "nearby_facilities": nearby_facilities,
            "map_points": compact_records(nearby_facilities, 20),
            "_raw_restaurant_rows": restaurant_rows,
            "_raw_tree_rows": tree_rows,
            "_raw_facility_rows": [],
            "_park_names": [r.get("NAME311") or r.get("SIGNNAME") or "unnamed park" for r in _parks_rows if r.get("NAME311") or r.get("SIGNNAME")],
        }, gaps

    def _query_building(self, con: Any, latitude: float, longitude: float, radius_m: int) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        building_radius = min(max(radius_m, 100), 300)
        metrics = {"open_class_c_250m": 0, "open_class_b_250m": 0, "open_class_a_250m": 0, "aep_count_250m": 0}
        evidence: list[dict[str, Any]] = []
        building_flags: list[dict[str, Any]] = []

        hv_rows = self._fetch_radius_rows(con, "housing_violations", "Latitude", "Longitude", latitude, longitude, building_radius, ["Class", "InspectionDate", "ViolationStatus", "HouseNumber", "StreetName", "NOVDescription"], limit=5000)
        open_rows = [row for row in hv_rows if "CLOSE" not in str(row.get("ViolationStatus") or "").upper()]
        for row in open_rows:
            violation_class = str(row.get("Class") or "").upper()
            if violation_class == "C":
                metrics["open_class_c_250m"] += 1
            elif violation_class == "B":
                metrics["open_class_b_250m"] += 1
            elif violation_class == "A":
                metrics["open_class_a_250m"] += 1
        if open_rows:
            evidence.append({
                "evidence_id": "housing_violations_250m",
                "source": "NYC HPD Housing Maintenance Code Violations",
                "date": "current extract",
                "summary": f"{len(open_rows)} open housing violation record(s) within {building_radius}m, including {metrics['open_class_c_250m']} Class C.",
                "record_ref": f"radius_{building_radius}m",
            })
            for row in open_rows[:10]:
                _vclass = str(row.get("Class") or "").strip().upper()
                _vhint = 25 if _vclass == "C" else 45 if _vclass == "B" else 65
                building_flags.append({
                    "kind": "housing_violation",
                    "latitude": float(row["__lat"]),
                    "longitude": float(row["__lon"]),
                    "summary": f"Class {row.get('Class') or '?'} violation at {row.get('HouseNumber') or ''} {row.get('StreetName') or ''}".strip(),
                    "score_hint": _vhint,
                })

        aep_rows = self._fetch_radius_rows(con, "aep_buildings", "Latitude", "Longitude", latitude, longitude, building_radius, ["CURRENT_STATUS", "NUMBER", "STREET", "AEP_START_DATE"], limit=5000)
        active_aep = [row for row in aep_rows if "DISCHARG" not in str(row.get("CURRENT_STATUS") or "").upper()]
        metrics["aep_count_250m"] = len(active_aep)
        if active_aep:
            evidence.append({
                "evidence_id": "aep_250m",
                "source": "NYC HPD Alternative Enforcement Program Buildings",
                "date": "current extract",
                "summary": f"{len(active_aep)} active AEP building record(s) within {building_radius}m.",
                "record_ref": f"radius_{building_radius}m",
            })
            for row in active_aep[:10]:
                building_flags.append({
                    "kind": "aep",
                    "latitude": float(row["__lat"]),
                    "longitude": float(row["__lon"]),
                    "summary": f"AEP building at {row.get('NUMBER') or ''} {row.get('STREET') or ''}".strip(),
                    "score_hint": 15,
                })
        return metrics, evidence, {"building_flags": building_flags, "_raw_violation_rows": open_rows}

    def _signal_timeseries_from_rows(self, rows: list[dict[str, Any]], date_field: str, row_filter: Callable[[dict[str, Any]], bool] | None = None) -> dict[str, Any]:
        filtered = rows if row_filter is None else [row for row in rows if row_filter(row)]
        parsed_dates = [parse_date(row.get(date_field)) for row in filtered]
        parsed_dates = [item for item in parsed_dates if item is not None]

        def count_window(start_days: int, end_days: int) -> int:
            total = 0
            for parsed in parsed_dates:
                diff = (date.today() - parsed).days
                if start_days < diff <= end_days:
                    total += 1
            return total

        quarterly_values = []
        for idx in range(4):
            start = idx * 90
            end = (idx + 1) * 90
            quarterly_values.append(count_window(start, end))
        quarterly_values.reverse()
        return {
            "last_30d": count_window(0, 30),
            "prev_30d": count_window(30, 60),
            "last_90d": count_window(0, 90),
            "same_90d_last_year": count_window(365, 455),
            "quarterly_values": quarterly_values,
        }

    def _overview_artifact_version(self) -> str | None:
        """The methodology version the overview artifacts were built under.

        None when the manifest is absent -- which is itself the pre-versioning
        signature. The August 2026 incident this guards against: the artifacts
        kept serving weights three versions of scoring ago, and with no stamp
        nothing could tell. Checked once per process; a mismatch logs loudly
        and is reported in the payload, but the layer still serves -- a stale
        overview labelled stale beats a blank map.
        """
        cached = getattr(self, "_overview_version_cache", "unset")
        if cached != "unset":
            return cached
        version: str | None = None
        try:
            import json

            manifest = self.overview_dir / "overview.manifest.json"
            if manifest.exists():
                version = json.loads(manifest.read_text()).get("methodology_version")
        except (OSError, ValueError) as exc:
            logger.warning("overview manifest unreadable: %s", exc)
        from ..metrics import METHODOLOGY_VERSION

        if version != METHODOLOGY_VERSION:
            logger.warning(
                "overview artifacts are methodology %s but the code is %s; "
                "re-run backend/scripts/build_overview_tiles.py and build_overview_nta.py",
                version or "pre-versioning", METHODOLOGY_VERSION,
            )
        self._overview_version_cache = version
        return version

    def get_overview_layer(self, view_mode: str, category_id: str | None, viewport: dict | None, zoom: int | None) -> dict[str, Any]:
        requested = "overall" if view_mode == "overall" else (category_id or "unknown")
        if view_mode != "overall" and category_id not in CATEGORY_CONFIG:
            return {
                "schema_version": "v3.7.8",
                "mode": "overview",
                "view_mode": view_mode,
                "category_id": category_id,
                "layer_mode": "h3_r8",
                "cells": [],
                "coverage": {
                    "overview_ready": False,
                    "available_categories": ["overall", *[k for k, v in CATEGORY_CONFIG.items() if v["map_driving"]]],
                    "missing_categories": [requested],
                    "ui_message": f"Overview layer '{requested}' is unsupported.",
                },
                "data_mode": "direct",
            }
        missing_all = ["overall", *[k for k, v in CATEGORY_CONFIG.items() if v["map_driving"]]]
        if self.overview_dir is None or not self.overview_dir.exists():
            return {
                "schema_version": "v3.7.8",
                "mode": "overview",
                "view_mode": view_mode,
                "category_id": category_id,
                "layer_mode": "h3_r8",
                "cells": [],
                "coverage": {
                    "overview_ready": False,
                    "available_categories": [],
                    "missing_categories": missing_all,
                    "ui_message": f"Overview layer '{requested}' is not ready yet. Click a point for realtime detail analysis.",
                },
                "data_mode": "direct",
            }
        layer_name = "overview_overall_h3_r8.parquet" if view_mode == "overall" else f"overview_{category_id}_h3_r8.parquet"
        path = self.overview_dir / layer_name
        if not path.exists():
            return {
                "schema_version": "v3.7.8",
                "mode": "overview",
                "view_mode": view_mode,
                "category_id": category_id,
                "layer_mode": "h3_r8",
                "cells": [],
                "coverage": {
                    "overview_ready": False,
                    "available_categories": [],
                    "missing_categories": [requested],
                    "ui_message": f"Overview layer '{requested}' is not ready yet. Click a point for realtime detail analysis.",
                },
                "data_mode": "direct",
            }
        rows = self._load_overview_rows(path, limit=5000)
        return {
            "schema_version": "v3.7.8",
            "mode": "overview",
            "view_mode": view_mode,
            "category_id": category_id,
            "layer_mode": "h3_r8",
            "cells": rows,
            "coverage": {
                "overview_ready": True,
                "available_categories": ["overall", *[k for k, v in CATEGORY_CONFIG.items() if v["map_driving"]]],
                "missing_categories": [],
            },
            # None means artifacts predate versioning; a value that differs
            # from /api/metrics' methodology_version means they are stale.
            "overview_methodology_version": self._overview_artifact_version(),
            "data_mode": "direct",
        }

    def _build_enriched_context(
        self, con: Any, latitude: float, longitude: float, radius_m: int,
        zip_code: str, time_window_days: int,
        traffic_detail: dict, sanitation_detail: dict,
        facilities_detail: dict, building_detail: dict,
    ) -> dict[str, Any]:
        """Extract richer dimensions from already-queried data for report context."""
        context: dict[str, Any] = {}

        # 311 complaint breakdown by type
        try:
            complaint_rows = sanitation_detail.get("_raw_rows", [])
            if complaint_rows:
                type_col = next((c for c in ["Problem (formerly Complaint Type)", "complaint_type"] if any(r.get(c) for r in complaint_rows[:3])), None)
                if type_col:
                    context["complaint_breakdown"] = _complaint_breakdown(complaint_rows, type_col)
        except Exception:
            pass

        # Collision time-of-day distribution
        try:
            collision_rows = traffic_detail.get("_raw_rows", [])
            if collision_rows:
                context["collision_time_buckets"] = _collision_time_buckets(collision_rows)
        except Exception:
            pass

        # Restaurant highlights
        try:
            restaurant_rows = facilities_detail.get("_raw_restaurant_rows", [])
            if restaurant_rows:
                context["restaurant_highlights"] = _restaurant_highlights(restaurant_rows)
        except Exception:
            pass

        # Violation age summary
        try:
            violation_rows = building_detail.get("_raw_violation_rows", [])
            if violation_rows:
                context["violation_age"] = _violation_age_summary(violation_rows)
        except Exception:
            pass

        # Tree health
        try:
            tree_rows = facilities_detail.get("_raw_tree_rows", [])
            if tree_rows:
                context["tree_health"] = _tree_health_summary(tree_rows)
        except Exception:
            pass

        # Nearest park names
        try:
            park_names = facilities_detail.get("_park_names", [])
            if park_names:
                context["nearest_parks"] = park_names[:3]
        except Exception:
            pass

        # Facility type breakdown
        try:
            facility_rows = facilities_detail.get("_raw_facility_rows", [])
            if facility_rows:
                context["facility_types"] = _facility_type_breakdown(facility_rows)
        except Exception:
            pass

        return context

    def get_point_signals(self, latitude: float, longitude: float, radius_m: int, time_window_days: int) -> dict[str, Any]:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        con = self._connect()
        target = self._nearest_location(con, latitude, longitude)
        zip_code = str(target.get("zip") or "")[:5]
        prepared_scores = self._collect_prepared_scores(con, latitude, longitude, radius_m, zip_code)

        # Run the 6 heavy query groups in parallel (each gets its own DuckDB connection)
        def _run_emergency():
            return self._query_emergency_zip(self._connect(), zip_code)

        def _run_collision():
            return self._query_collision(self._connect(), latitude, longitude, radius_m, time_window_days)

        def _run_rodent():
            return self._query_rodent(self._connect(), latitude, longitude, radius_m, time_window_days)

        def _run_sanitation():
            return self._query_sanitation_311(self._connect(), latitude, longitude, radius_m, time_window_days)

        def _run_facilities():
            return self._query_facilities(self._connect(), latitude, longitude, radius_m, zip_code, time_window_days)

        def _run_building():
            return self._query_building(self._connect(), latitude, longitude, radius_m)

        with ThreadPoolExecutor(max_workers=6) as pool:
            f_emergency = pool.submit(_run_emergency)
            f_traffic = pool.submit(_run_collision)
            f_rodent = pool.submit(_run_rodent)
            f_sanitation = pool.submit(_run_sanitation)
            f_facilities = pool.submit(_run_facilities)
            f_building = pool.submit(_run_building)

        emergency, emergency_ev, emergency_gaps = f_emergency.result()
        traffic, traffic_ev, traffic_detail = f_traffic.result()
        rodent, rodent_ev, rodent_detail = f_rodent.result()
        sanitation, sanitation_ev, sanitation_detail = f_sanitation.result()
        facilities, facilities_ev, facilities_detail, facilities_gaps = f_facilities.result()
        building, building_ev, building_detail = f_building.result()
        return {
            "target": {
                "latitude": latitude,
                "longitude": longitude,
                "radius_m": radius_m,
                "matched_address": target.get("matched_address"),
                "borough": target.get("borough"),
                "zip": target.get("zip"),
                "canonical_location_id": target.get("canonical_location_id"),
            },
            "current_state": {
                "safety": {**rodent, **sanitation, **emergency},
                "transit": traffic,
                "amenities": facilities,
                "building": building,
            },
            "detail_items": {
                "map_points": compact_records(
                    traffic_detail.get("map_points", [])
                    + rodent_detail.get("map_points", [])
                    + sanitation_detail.get("map_points", [])
                    + facilities_detail.get("map_points", [])
                    + building_detail.get("building_flags", []),
                    60,
                ),
                "nearby_facilities": compact_records(facilities_detail.get("nearby_facilities", []), 20),
                "building_flags": compact_records(building_detail.get("building_flags", []), 20),
                "recent_incidents": compact_records(
                    traffic_detail.get("recent_incidents", [])
                    + rodent_detail.get("recent_incidents", [])
                    + sanitation_detail.get("recent_incidents", []),
                    20,
                ),
            },
            "query_evidence": emergency_ev + traffic_ev + rodent_ev + sanitation_ev + facilities_ev + building_ev,
            "data_gaps": emergency_gaps + facilities_gaps,
            "prepared_scores": prepared_scores,
            "enriched_context": self._build_enriched_context(
                con, latitude, longitude, radius_m, zip_code, time_window_days,
                traffic_detail, sanitation_detail, facilities_detail, building_detail,
            ),
        }

    def get_local_timeseries(self, latitude: float, longitude: float, radius_m: int, time_window_days: int) -> dict[str, Any]:
        con = self._connect()
        building_radius = min(max(radius_m, 100), 300)

        # Use H3 grid_disk to compute nearby cells — NO large table scanning
        h3_cells = self._h3_cells_for_radius(latitude, longitude, radius_m)
        h3_cells_building = self._h3_cells_for_radius(latitude, longitude, building_radius)

        # Read small pre-aggregated quarterly tables directly with H3 cells
        ready_collision_trend = self._query_ready_quarterly(con, "safety/collisions_quarterly_h3.parquet", h3_cells)
        ready_rodent_trend = self._query_ready_quarterly(con, "safety/rodent_quarterly_h3.parquet", h3_cells)
        ready_311_trend = self._query_ready_quarterly(con, "safety/311_quarterly_h3.parquet", h3_cells)
        ready_housing_trend = self._query_ready_quarterly(con, "building/housing_violations_quarterly_h3.parquet", h3_cells_building)

        # Only fall back to bbox scanning if quarterly tables don't exist
        result: dict[str, Any] = {}

        if ready_collision_trend:
            result["collision"] = ready_collision_trend
        else:
            fetch_days = max(time_window_days, 540)
            collision_rows = [row for row in self._fetch_radius_rows(con, "collisions", "LATITUDE", "LONGITUDE", latitude, longitude, radius_m, ["CRASH DATE"], limit=50000) if is_within_days(row.get("CRASH DATE"), fetch_days)]
            result["collision"] = self._signal_timeseries_from_rows(collision_rows, "CRASH DATE")

        if ready_rodent_trend:
            result["rodent"] = ready_rodent_trend
        else:
            fetch_days = max(time_window_days, 540)
            rodent_rows = [row for row in self._fetch_radius_rows(con, "rodent_inspections", "LATITUDE", "LONGITUDE", latitude, longitude, radius_m, ["INSPECTION_DATE", "RESULT"], limit=50000) if is_within_days(row.get("INSPECTION_DATE"), fetch_days)]
            result["rodent"] = self._signal_timeseries_from_rows(rodent_rows, "INSPECTION_DATE", lambda row: any(term in str(row.get("RESULT") or "").upper() for term in POSITIVE_RODENT_TERMS))

        if ready_311_trend:
            result["311_sanitation"] = ready_311_trend
        else:
            fetch_days = max(time_window_days, 540)
            complaint_rows: list[dict[str, Any]] = []
            complaint_date_col = "Created Date"
            complaint_type_col = "Problem (formerly Complaint Type)"
            for variant in [
                ("Latitude", "Longitude", "Created Date", "Problem (formerly Complaint Type)"),
                ("latitude", "longitude", "created_date", "complaint_type"),
            ]:
                try:
                    lat_col, lon_col, date_col, type_col = variant
                    complaint_rows = [
                        row
                        for row in self._fetch_radius_rows(con, "311_subset", lat_col, lon_col, latitude, longitude, radius_m, [date_col, type_col], limit=80000)
                        if is_within_days(row.get(date_col), fetch_days)
                    ]
                    complaint_date_col = date_col
                    complaint_type_col = type_col
                    break
                except Exception:
                    continue
            result["311_sanitation"] = self._signal_timeseries_from_rows(complaint_rows, complaint_date_col, lambda row: str(row.get(complaint_type_col) or "").upper() in SAFETY_311_TYPES)

        if ready_housing_trend:
            result["housing_violations"] = ready_housing_trend
        else:
            fetch_days = max(time_window_days, 540)
            building_rows = [row for row in self._fetch_radius_rows(con, "housing_violations", "Latitude", "Longitude", latitude, longitude, building_radius, ["InspectionDate", "Class", "ViolationStatus"], limit=5000) if is_within_days(row.get("InspectionDate"), fetch_days)]
            result["housing_violations"] = self._signal_timeseries_from_rows(building_rows, "InspectionDate", lambda row: "CLOSE" not in str(row.get("ViolationStatus") or "").upper() and str(row.get("Class") or "").upper() in {"B", "C"})

        result["ems_response"] = {}
        result["fire_response"] = {}
        return result

    def get_baselines(self) -> dict[str, Any]:
        path = self._ready_path("baselines/baselines.json")
        if not path.exists():
            path = self._parquet_path("baselines").with_suffix(".json")
        baselines: dict[str, Any] = {}
        if path.exists():
            import json
            baselines = json.loads(path.read_text(encoding="utf-8"))
        return {
            "collision": baselines.get("collisions_scores_h3", baselines.get("emergency", {}).get("collision_count_500m", {"p25": 8, "p50": 18, "p75": 35})),
            "ems_response": baselines.get("ems_scores_zip", baselines.get("emergency", {}).get("ems_response", {"p25": 672, "p50": 807, "p75": 992})),
            "fire_response": baselines.get("fire_scores_zip", baselines.get("emergency", {}).get("fire_response", {"p25": 325, "p50": 357, "p75": 389})),
            "rodent": baselines.get("rodent_scores_h3", baselines.get("food", {}).get("rodent_positive_count_500m", {"p25": 2, "p50": 6, "p75": 14})),
            "311_sanitation": baselines.get("311_scores_h3", {"p25": 2, "p50": 6, "p75": 12}),
            "facility_access": {"p25": 25, "p50": 50, "p75": 70},
            "housing_violations": baselines.get("housing_violations_scores_h3", {"p25": 1, "p50": 3, "p75": 6}),
        }

    def get_context_items(self, latitude: float, longitude: float, radius_m: int) -> dict[str, Any]:
        return self.get_point_signals(latitude, longitude, radius_m, 365)["detail_items"]

    def get_coverage(self) -> dict[str, Any]:
        required = [
            "location_index",
            "collisions",
            "rodent_inspections",
            "311_subset",
            "public_toilets",
            "restaurant_inspections",
            "linknyc_locations",
            "street_trees",
            "parks",
            "ems_dispatch",
            "fire_dispatch",
            "housing_violations",
            "aep_buildings",
        ]
        available_datasets = [dataset for dataset in required if self._dataset_available(dataset)]
        overview_ready = False
        available_overview_categories: list[str] = []
        missing_overview_categories = ["overall", *[k for k, v in CATEGORY_CONFIG.items() if v["map_driving"]]]
        if self.overview_dir and self.overview_dir.exists():
            expected = {
                "overall": self.overview_dir / "overview_overall_h3_r8.parquet",
                **{key: self.overview_dir / f"overview_{key}_h3_r8.parquet" for key, value in CATEGORY_CONFIG.items() if value["map_driving"]},
            }
            available_overview_categories = [key for key, path in expected.items() if path.exists()]
            missing_overview_categories = [key for key, path in expected.items() if not path.exists()]
            overview_ready = "overall" in available_overview_categories
        return {
            "data_mode": "direct",
            "provider": "DirectQueryDataProvider",
            "provider_ready": True,
            "overview_ready": overview_ready,
            "ready_data_root": str(self.ready_dir),
            "ready_baselines_available": self._ready_path("baselines/baselines.json").exists(),
            "available_overview_categories": available_overview_categories,
            "missing_overview_categories": missing_overview_categories,
            "available_datasets": available_datasets,
            "overview_default_weights": OVERVIEW_DEFAULT_WEIGHTS,
        }
