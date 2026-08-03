#!/usr/bin/env bash
# Download all 18 NYC Open Data datasets used by Urban Dossier.
# Usage: bash scripts/download_datasets.sh [output_dir]
# Default output: ~/nyc_open_data/

set -euo pipefail

OUT="${1:-$HOME/nyc_open_data}"

echo "=== Urban Dossier Dataset Downloader ==="
echo "Output directory: $OUT"
echo ""

mkdir -p "$OUT/safety" "$OUT/environment" "$OUT/quality_of_life" "$OUT/transit" "$OUT/amenities" "$OUT/buildings"

download() {
    local url="$1"
    local dest="$2"
    local name="$3"
    local partial="${dest}.part"

    if [ -f "$dest" ]; then
        local size
        size=$(stat -c%s "$dest" 2>/dev/null || stat -f%z "$dest" 2>/dev/null || echo 0)
        if [ "$size" -gt 1000 ]; then
            echo "[SKIP] $name — already exists ($(numfmt --to=iec "$size" 2>/dev/null || echo "${size} bytes"))"
            return 0
        fi
    fi

    echo "[DOWN] $name ..."
    rm -f "$partial"
    if curl -L -o "$partial" --progress-bar --fail --retry 4 --retry-delay 5 "$url"; then
        mv "$partial" "$dest"
        local size
        size=$(stat -c%s "$dest" 2>/dev/null || stat -f%z "$dest" 2>/dev/null || echo "?")
        echo "[DONE] $name — $(numfmt --to=iec "$size" 2>/dev/null || echo "${size} bytes")"
    else
        echo "[FAIL] $name — download failed, skipping"
        rm -f "$partial"
    fi
}

echo "--- Safety ---"

download \
    "https://data.cityofnewyork.us/api/views/h9gi-nx95/rows.csv?accessType=DOWNLOAD" \
    "$OUT/safety/motor_vehicle_collisions.csv" \
    "Motor Vehicle Collisions"

download \
    "https://data.cityofnewyork.us/api/views/76xm-jjuj/rows.csv?accessType=DOWNLOAD" \
    "$OUT/safety/ems_incident_dispatch.csv" \
    "EMS Incident Dispatch"

download \
    "https://data.cityofnewyork.us/api/views/8m42-w767/rows.csv?accessType=DOWNLOAD" \
    "$OUT/safety/fire_incident_dispatch.csv" \
    "Fire Incident Dispatch"

echo ""
echo "--- Environment ---"

download \
    "https://data.cityofnewyork.us/api/views/p937-wjvj/rows.csv?accessType=DOWNLOAD" \
    "$OUT/environment/rodent_inspections.csv" \
    "Rodent Inspections"

echo ""
echo "--- Quality of Life ---"

# 311 is huge (30M+ rows). This downloads the complete current export so the
# raw layer remains reproducible; downstream preprocessing performs filtering.
download \
    "https://data.cityofnewyork.us/api/views/erm2-nwe9/rows.csv?accessType=DOWNLOAD" \
    "$OUT/quality_of_life/311_service_requests_2020_present.csv" \
    "311 Service Requests (large file, may take a while)"

echo ""
echo "--- Transit ---"

download \
    "https://data.ny.gov/api/views/i9wp-a4ja/rows.csv?accessType=DOWNLOAD" \
    "$OUT/transit/mta_subway_entrances_exits_2024.csv" \
    "Subway Entrances/Exits"

download \
    "https://data.cityofnewyork.us/api/views/t4f2-8md7/rows.csv?accessType=DOWNLOAD" \
    "$OUT/transit/bus_stop_shelters.csv" \
    "Bus Stop Shelters"

download \
    "https://data.cityofnewyork.us/api/views/mzxg-pwib/rows.csv?accessType=DOWNLOAD" \
    "$OUT/transit/nyc_bike_routes.csv" \
    "Bike Routes"

download \
    "https://data.cityofnewyork.us/api/views/uiay-nctu/rows.csv?accessType=DOWNLOAD" \
    "$OUT/transit/open_streets_locations.csv" \
    "Open Streets Locations"

echo ""
echo "--- Amenities ---"

download \
    "https://data.cityofnewyork.us/api/views/43nn-pn8j/rows.csv?accessType=DOWNLOAD" \
    "$OUT/amenities/dohmh_restaurant_inspections.csv" \
    "Restaurant Inspections"

download \
    "https://data.cityofnewyork.us/api/views/enfh-gkve/rows.csv?accessType=DOWNLOAD" \
    "$OUT/amenities/parks_properties.csv" \
    "Parks Properties"

download \
    "https://data.cityofnewyork.us/api/views/uvpi-gqnh/rows.csv?accessType=DOWNLOAD" \
    "$OUT/amenities/street_trees.csv" \
    "Street Trees (2015 Census)"

download \
    "https://data.cityofnewyork.us/api/views/s4kf-3yrf/rows.csv?accessType=DOWNLOAD" \
    "$OUT/amenities/linknyc_kiosk_locations.csv" \
    "LinkNYC Kiosks"

download \
    "https://data.cityofnewyork.us/api/v3/views/i7jb-7jku/export.csv?accessType=DOWNLOAD" \
    "$OUT/amenities/public_toilets.csv" \
    "Public Toilets"

download \
    "https://data.cityofnewyork.us/api/views/ji82-xba5/rows.csv?accessType=DOWNLOAD" \
    "$OUT/amenities/facilities_database.csv" \
    "Facilities Database"

echo ""
echo "--- Buildings ---"

download \
    "https://data.cityofnewyork.us/api/views/wvxf-dwi5/rows.csv?accessType=DOWNLOAD" \
    "$OUT/buildings/housing_code_violations.csv" \
    "Housing Violations"

download \
    "https://data.cityofnewyork.us/api/views/hcir-3275/rows.csv?accessType=DOWNLOAD" \
    "$OUT/buildings/buildings_aep.csv" \
    "AEP Buildings"

download \
    "https://data.cityofnewyork.us/api/views/64uk-42ks/rows.csv?accessType=DOWNLOAD" \
    "$OUT/buildings/pluto.csv" \
    "PLUTO (land use reference)"

echo ""
echo "=== Download complete ==="
echo "Files saved to: $OUT"
echo ""
echo "Next steps:"
echo "  1. Export URBAN_DOSSIER_RAW_DATA_ROOT=$OUT"
echo "  2. Build the required ready Parquet files; see backend/scripts/README.md"
echo "  Note: the production urban-dossier Agent is analysis-only and does not ingest files."
