from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


ALLOWED_CATEGORY_ALIASES = {
    "general",
    "overall",
    "amenities",
    "facilities",
    "transit",
    "traffic",
    "safety",
    "building",
}


class WatchlistSeed(BaseModel):
    latitude: float = Field(ge=40.4, le=40.95)
    longitude: float = Field(ge=-74.3, le=-73.7)
    title: str | None = None


class Viewport(BaseModel):
    north: float
    south: float
    east: float
    west: float


class OverviewRequest(BaseModel):
    view_mode: Literal["overall", "category"] = "overall"
    category_id: str | None = None
    viewport: Viewport | None = None
    zoom: int | None = None
    render_mode: Literal["h3_cells"] = "h3_cells"

    @field_validator("category_id")
    @classmethod
    def validate_category_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if normalized not in ALLOWED_CATEGORY_ALIASES:
            raise ValueError("Unsupported category_id")
        return value.strip()


class DetailPreviewRequest(BaseModel):
    latitude: float = Field(ge=40.4, le=40.95)
    longitude: float = Field(ge=-74.3, le=-73.7)
    radius_m: Literal[200, 500, 1000] = 500
    priority_order: list[str] = Field(min_length=1, max_length=4)
    time_window_days: int = Field(default=365, ge=30, le=1095)

    @field_validator("priority_order")
    @classmethod
    def validate_priority_order(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            item = value.strip()
            lowered = item.lower()
            if lowered not in ALLOWED_CATEGORY_ALIASES - {"general", "overall", "building"}:
                raise ValueError("Unsupported priority category")
            if lowered not in seen:
                normalized.append(item)
                seen.add(lowered)
        if not normalized:
            raise ValueError("priority_order must contain at least one supported category")
        return normalized


class DetailRequest(DetailPreviewRequest):
    include_report: bool = True
    report_mode: Literal["individual", "organization"] = "individual"


class WatchlistRequest(BaseModel):
    seeds: list[WatchlistSeed] = Field(max_length=10)
    priority_order: list[str] | None = None
    radius_m: Literal[200, 500, 1000] = 500
    time_window_days: int = Field(default=365, ge=30, le=1095)

    @field_validator("priority_order")
    @classmethod
    def validate_optional_priority_order(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return values
        return DetailPreviewRequest.validate_priority_order(values)
