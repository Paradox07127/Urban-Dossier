#!/usr/bin/env bash
# Build a compact, current NYC OpenMapTiles MBTiles archive for Urban Dossier.
#
# Source: BBBike's NewYork city extract (OSM/ODbL), updated regularly.
# Output: OpenMapTiles 3.16-compatible MVT, z0-z14, English labels, without the
# unused housenumber layer. The output is written atomically so a failed refresh
# never replaces the last known-good map.
#
# Usage:
#   bash scripts/maps/build_nyc_mbtiles.sh [state_root]
# Default state_root: /mnt/data/urban-dossier

set -euo pipefail

STATE_ROOT="${1:-/mnt/data/urban-dossier}"
MAP_ROOT="${STATE_ROOT}/maps"
SOURCE_DIR="${MAP_ROOT}/source"
SUPPORT_DIR="${MAP_ROOT}/sources"
OUTPUT_DIR="${MAP_ROOT}/output"
TMP_DIR="${MAP_ROOT}/tmp"

PBF_URL="https://download.bbbike.org/osm/bbbike/NewYork/NewYork.osm.pbf"
CHECKSUM_URL="https://download.bbbike.org/osm/bbbike/NewYork/CHECKSUM.txt"
PLANETILER_IMAGE="ghcr.io/onthegomap/planetiler:0.10.2@sha256:cf32202dbc001a9ab4bc11534b642b13de3798179817da8558e567a3d13dd403"
JAVA_HEAP="${URBAN_DOSSIER_MAP_JAVA_HEAP:-16g}"
MAP_THREADS="${URBAN_DOSSIER_MAP_THREADS:-32}"

PBF_PATH="${SOURCE_DIR}/NewYork.osm.pbf"
PBF_PARTIAL="${PBF_PATH}.part"
CHECKSUM_PATH="${SOURCE_DIR}/CHECKSUM.txt"
OUTPUT_PATH="${OUTPUT_DIR}/new-york-openmaptiles.mbtiles"
OUTPUT_PARTIAL="${OUTPUT_DIR}/new-york-openmaptiles.part.mbtiles"

mkdir -p "${SOURCE_DIR}" "${SUPPORT_DIR}" "${OUTPUT_DIR}" "${TMP_DIR}"

curl --fail --location --retry 4 --retry-delay 5 \
  --output "${CHECKSUM_PATH}" "${CHECKSUM_URL}"

rm -f "${PBF_PARTIAL}"
curl --fail --location --retry 4 --retry-delay 5 \
  --output "${PBF_PARTIAL}" "${PBF_URL}"
mv "${PBF_PARTIAL}" "${PBF_PATH}"

(
  cd "${SOURCE_DIR}"
  grep -E '[[:space:]]NewYork\.osm\.pbf$' CHECKSUM.txt | md5sum --check -
)

rm -f "${OUTPUT_PARTIAL}"
docker run --rm \
  --env "JAVA_TOOL_OPTIONS=-Xmx${JAVA_HEAP}" \
  --volume "${MAP_ROOT}:/data" \
  "${PLANETILER_IMAGE}" \
  --osm-path=/data/source/NewYork.osm.pbf \
  --output=/data/output/new-york-openmaptiles.part.mbtiles \
  --download \
  --download-dir=/data/sources \
  --tmpdir=/data/tmp \
  --force \
  --languages=en \
  --exclude-layers=housenumber \
  --maxzoom=14 \
  --render-maxzoom=14 \
  --use-wikidata=false \
  --threads="${MAP_THREADS}"

mv "${OUTPUT_PARTIAL}" "${OUTPUT_PATH}"
echo "NYC MBTiles ready: ${OUTPUT_PATH}"
