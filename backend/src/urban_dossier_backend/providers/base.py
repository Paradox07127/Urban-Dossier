from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class DataProvider(ABC):
    def get_overview_context(self, latitude: float, longitude: float) -> dict[str, Any] | None:
        return None

    @abstractmethod
    def get_overview_layer(self, view_mode: str, category_id: str | None, viewport: dict | None, zoom: int | None) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def get_point_signals(self, latitude: float, longitude: float, radius_m: int, time_window_days: int) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def get_local_timeseries(self, latitude: float, longitude: float, radius_m: int, time_window_days: int) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def get_baselines(self) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def get_context_items(self, latitude: float, longitude: float, radius_m: int) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def get_coverage(self) -> dict[str, Any]:
        raise NotImplementedError
