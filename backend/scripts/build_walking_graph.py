#!/usr/bin/env python3
"""Build the pedestrian routing graph used by POST /api/isochrone.

Parses the OSM extract once and publishes two Parquet files:

    walk_nodes.parquet   node_id, lat, lon
    walk_edges.parquet   u, v, length_m

Why Parquet instead of a pickled networkx graph
-----------------------------------------------
The full NYC walking network is far too large to hold resident in the FastAPI
process, and the deployment notes are explicit about not growing the backend's
footprint. A walk of even 60 minutes only reaches a few kilometres, so the
runtime never needs the whole city: it selects the nodes inside a bounding box
with DuckDB, builds a small local graph, and runs Dijkstra on that. Same
storage engine as the rest of the analytical layer, bounded memory per request.

Edges are undirected: sidewalks are traversable both ways on foot, and OSM
``oneway`` applies to vehicles.

Usage:
    python backend/scripts/build_walking_graph.py \
        --pbf /mnt/data/urban-dossier-state/maps/source/NewYork.osm.pbf \
        --out /mnt/data/urban-dossier-state/maps/walk
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


DEFAULT_PBF = "/mnt/data/urban-dossier-state/maps/source/NewYork.osm.pbf"
DEFAULT_OUT = "/mnt/data/urban-dossier-state/maps/walk"


def build(pbf_path: Path, out_dir: Path) -> dict:
    from pyrosm import OSM

    if not pbf_path.exists():
        raise SystemExit(f"OSM extract not found: {pbf_path}")

    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.time()
    print(f"Parsing {pbf_path} (this takes several minutes for a city extract)...")
    osm = OSM(str(pbf_path))
    nodes, edges = osm.get_network(network_type="walking", nodes=True)
    parse_seconds = time.time() - started
    print(f"  parsed in {parse_seconds:.1f}s: {len(nodes)} nodes, {len(edges)} edges")

    required_edge_cols = {"u", "v", "length"}
    missing = required_edge_cols - set(edges.columns)
    if missing:
        raise SystemExit(f"pyrosm returned edges without {sorted(missing)}")

    edge_frame = edges[["u", "v", "length"]].rename(columns={"length": "length_m"})
    edge_frame = edge_frame.dropna(subset=["u", "v", "length_m"])
    # Zero-length edges add nothing but slow the search down.
    edge_frame = edge_frame[edge_frame["length_m"] > 0]
    edge_frame["u"] = edge_frame["u"].astype("int64")
    edge_frame["v"] = edge_frame["v"].astype("int64")
    edge_frame["length_m"] = edge_frame["length_m"].astype("float64")

    node_frame = nodes[["id", "lat", "lon"]].rename(columns={"id": "node_id"})
    node_frame = node_frame.dropna(subset=["node_id", "lat", "lon"])
    node_frame["node_id"] = node_frame["node_id"].astype("int64")
    node_frame["lat"] = node_frame["lat"].astype("float64")
    node_frame["lon"] = node_frame["lon"].astype("float64")
    node_frame = node_frame.drop_duplicates(subset=["node_id"])

    # Drop edges whose endpoints were filtered out; a dangling endpoint would
    # silently truncate the reachable set at runtime.
    known = set(node_frame["node_id"].tolist())
    before = len(edge_frame)
    edge_frame = edge_frame[edge_frame["u"].isin(known) & edge_frame["v"].isin(known)]
    dropped = before - len(edge_frame)

    nodes_path = out_dir / "walk_nodes.parquet"
    edges_path = out_dir / "walk_edges.parquet"
    node_frame.to_parquet(nodes_path, index=False, compression="zstd")
    edge_frame.to_parquet(edges_path, index=False, compression="zstd")

    manifest = {
        "source_pbf": str(pbf_path),
        "source_bytes": pbf_path.stat().st_size,
        "node_count": int(len(node_frame)),
        "edge_count": int(len(edge_frame)),
        "edges_dropped_dangling": int(dropped),
        "parse_seconds": round(parse_seconds, 1),
        "network_type": "walking",
        "directed": False,
        "bbox": {
            "min_lat": float(node_frame["lat"].min()),
            "max_lat": float(node_frame["lat"].max()),
            "min_lon": float(node_frame["lon"].min()),
            "max_lon": float(node_frame["lon"].max()),
        },
        "files": {"nodes": nodes_path.name, "edges": edges_path.name},
    }
    (out_dir / "walk_graph.manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2))
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pbf", default=DEFAULT_PBF)
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args()

    build(Path(args.pbf), Path(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
