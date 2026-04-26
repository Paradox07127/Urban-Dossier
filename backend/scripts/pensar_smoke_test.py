from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


BASE_URL = os.getenv("URBAN_DOSSIER_TEST_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
DEMO_TOKEN = os.getenv("URBAN_DOSSIER_DEMO_TOKEN", "").strip()


def request_json(path: str, payload: dict | None = None, headers: dict[str, str] | None = None, method: str | None = None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=body,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method or ("POST" if payload is not None else "GET"),
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, dict(response.headers), json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body.startswith("{") else {"raw": body}
        return exc.code, dict(exc.headers), parsed


def expect(name: str, condition: bool, detail: str) -> None:
    if condition:
        print(f"[PASS] {name}: {detail}")
    else:
        print(f"[FAIL] {name}: {detail}")
        raise SystemExit(1)


def main() -> None:
    status, _, payload = request_json("/api/health")
    expect("health", status == 200 and payload.get("status") == "ok", f"status={status}")

    overview_status, _, _ = request_json(
        "/api/overview",
        {"view_mode": "category", "category_id": "../evil", "render_mode": "h3_cells"},
    )
    expect("overview category whitelist", overview_status == 422, f"status={overview_status}")

    preview_status, _, _ = request_json(
        "/api/detail/preview",
        {
            "latitude": 40.758,
            "longitude": -73.9855,
            "radius_m": 777,
            "priority_order": ["Amenities", "Transit", "Safety"],
            "time_window_days": 365,
        },
    )
    expect("radius whitelist", preview_status == 422, f"status={preview_status}")

    watchlist_status, _, _ = request_json(
        "/api/watchlist/run",
        {
            "seeds": [{"latitude": 40.75, "longitude": -73.99}] * 11,
            "priority_order": ["Amenities", "Transit", "Safety"],
            "radius_m": 500,
            "time_window_days": 365,
        },
    )
    expect("watchlist seed cap", watchlist_status == 422, f"status={watchlist_status}")

    if DEMO_TOKEN:
        unauthorized_status, _, _ = request_json(
            "/api/detail/preview",
            {
                "latitude": 40.758,
                "longitude": -73.9855,
                "radius_m": 500,
                "priority_order": ["Amenities", "Transit", "Safety"],
                "time_window_days": 365,
            },
        )
        expect("token required", unauthorized_status == 401, f"status={unauthorized_status}")

        authorized_status, _, _ = request_json(
            "/api/detail/preview",
            {
                "latitude": 40.758,
                "longitude": -73.9855,
                "radius_m": 500,
                "priority_order": ["Amenities", "Transit", "Safety"],
                "time_window_days": 365,
            },
            headers={"X-Urban-Dossier-Token": DEMO_TOKEN},
        )
        expect("token accepted", authorized_status == 200, f"status={authorized_status}")

    print("Pensar-oriented smoke test passed.")


if __name__ == "__main__":
    main()
