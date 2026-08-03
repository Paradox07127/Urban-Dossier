#!/usr/bin/env bash
# Download and validate the current official NYC 2020 NTA boundary layer.
#
# The ArcGIS feature service is maintained by NYC Department of City Planning.
# The release recorded here is 26B (May 2026). GeoJSON is requested in WGS84
# with six decimal places, which is sufficient for web mapping while avoiding
# needlessly large coordinate payloads.
#
# Usage:
#   bash scripts/maps/download_nta_2020.sh [output_dir]
# Default output_dir: data/boundaries under this repository.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
OUTPUT_DIR="${1:-${REPO_ROOT}/data/boundaries}"

SERVICE_ROOT="https://services5.arcgis.com/GfwWNkhOj9bNBqoJ/ArcGIS/rest/services/NYC_Neighborhood_Tabulation_Areas_2020/FeatureServer/0"
GEOJSON_URL="${SERVICE_ROOT}/query?where=1%3D1&outFields=*&returnGeometry=true&outSR=4326&geometryPrecision=6&f=geojson"
LAYER_URL="${SERVICE_ROOT}?f=pjson"
METADATA_URL="https://s-media.nyc.gov/agencies/dcp/assets/files/pdf/data-tools/bytes/nynta2020_metadata.pdf"

GEOJSON_PATH="${OUTPUT_DIR}/nta_2020.geojson"
LAYER_PATH="${OUTPUT_DIR}/nta_2020.layer.json"
METADATA_PATH="${OUTPUT_DIR}/nta_2020_metadata_26B.pdf"
MANIFEST_PATH="${OUTPUT_DIR}/nta_2020.manifest.json"

mkdir -p "${OUTPUT_DIR}"

curl --fail --location --retry 4 --retry-delay 3 \
  --output "${GEOJSON_PATH}.part" "${GEOJSON_URL}"
curl --fail --location --retry 4 --retry-delay 3 \
  --output "${LAYER_PATH}.part" "${LAYER_URL}"
curl --fail --location --retry 4 --retry-delay 3 \
  --output "${METADATA_PATH}.part" "${METADATA_URL}"

python3 - "${GEOJSON_PATH}.part" "${LAYER_PATH}.part" "${METADATA_PATH}.part" <<'PY'
import json
import sys
from pathlib import Path

geojson_path, layer_path, metadata_path = map(Path, sys.argv[1:])
geojson = json.loads(geojson_path.read_text())
layer = json.loads(layer_path.read_text())

if geojson.get("type") != "FeatureCollection":
    raise SystemExit("NTA download is not a GeoJSON FeatureCollection")
features = geojson.get("features", [])
if len(features) < 250:
    raise SystemExit(f"NTA download is incomplete: only {len(features)} features")

codes = []
for feature in features:
    properties = feature.get("properties") or {}
    code = properties.get("NTA2020") or properties.get("nta2020")
    geometry_type = (feature.get("geometry") or {}).get("type")
    if not code:
        raise SystemExit("NTA feature is missing its NTA2020 code")
    if geometry_type not in {"Polygon", "MultiPolygon"}:
        raise SystemExit(f"Unexpected NTA geometry type: {geometry_type}")
    codes.append(code)

if len(codes) != len(set(codes)):
    raise SystemExit("NTA2020 codes are not unique")
if layer.get("geometryType") != "esriGeometryPolygon":
    raise SystemExit("ArcGIS layer metadata does not describe polygon geometry")
if metadata_path.read_bytes()[:4] != b"%PDF":
    raise SystemExit("NYC Planning metadata download is not a PDF")

print(f"validated {len(features)} unique NTA polygons")
PY

mv "${GEOJSON_PATH}.part" "${GEOJSON_PATH}"
mv "${LAYER_PATH}.part" "${LAYER_PATH}"
mv "${METADATA_PATH}.part" "${METADATA_PATH}"

python3 - "${GEOJSON_PATH}" "${LAYER_PATH}" "${METADATA_PATH}" "${MANIFEST_PATH}" \
  "${GEOJSON_URL}" "${LAYER_URL}" "${METADATA_URL}" <<'PY'
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

geojson_path, layer_path, metadata_path, manifest_path = map(Path, sys.argv[1:5])
geojson_url, layer_url, metadata_url = sys.argv[5:8]
geojson = json.loads(geojson_path.read_text())
features = geojson["features"]

def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()

def artifact(path: Path, url: str) -> dict:
    return {
        "filename": path.name,
        "source_url": url,
        "size_bytes": path.stat().st_size,
        "sha256": digest(path),
    }

property_names = sorted({key for item in features for key in (item.get("properties") or {})})
manifest = {
    "dataset": "NYC Neighborhood Tabulation Areas (NTA), 2020",
    "publisher": "NYC Department of City Planning",
    "release": "26B",
    "release_date": "2026-05",
    "downloaded_at": datetime.now(timezone.utc).isoformat(),
    "coordinate_reference_system": "EPSG:4326",
    "feature_count": len(features),
    "nta_code_count": len({(item.get("properties") or {}).get("NTA2020") for item in features}),
    "property_names": property_names,
    "artifacts": [
        artifact(geojson_path, geojson_url),
        artifact(layer_path, layer_url),
        artifact(metadata_path, metadata_url),
    ],
}
manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
print(f"wrote {manifest_path}")
PY

echo "NTA 2020 boundaries ready: ${GEOJSON_PATH}"
