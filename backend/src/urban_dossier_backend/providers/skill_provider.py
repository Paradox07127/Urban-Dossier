from __future__ import annotations

from typing import Any

from .base import DataProvider


class SkillDataProvider(DataProvider):
    """Placeholder provider for future Skill-backed outputs."""

    def __init__(self) -> None:
        self.available = False

    def get_overview_layer(self, view_mode: str, category_id: str | None, viewport: dict | None, zoom: int | None) -> dict[str, Any]:
        raise RuntimeError("SkillDataProvider is not yet implemented")

    def get_point_signals(self, latitude: float, longitude: float, radius_m: int, time_window_days: int) -> dict[str, Any]:
        raise RuntimeError("SkillDataProvider is not yet implemented")

    def get_local_timeseries(self, latitude: float, longitude: float, radius_m: int, time_window_days: int) -> dict[str, Any]:
        raise RuntimeError("SkillDataProvider is not yet implemented")

    def get_baselines(self) -> dict[str, Any]:
        raise RuntimeError("SkillDataProvider is not yet implemented")

    def get_context_items(self, latitude: float, longitude: float, radius_m: int) -> dict[str, Any]:
        raise RuntimeError("SkillDataProvider is not yet implemented")

    def get_coverage(self) -> dict[str, Any]:
        return {
            "data_mode": "skill",
            "provider": "SkillDataProvider",
            "provider_ready": False,
            "skill_provider_available": False,
            "direct_provider_available": True,
        }
