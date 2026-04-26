from pydantic import BaseModel, Field
from typing import Literal


class AgentSessionRequest(BaseModel):
    latitude: float
    longitude: float
    radius_m: Literal[200, 500, 1000] = 500
    priority_order: list[str]
    time_window_days: int = 365


class AgentChatRequest(BaseModel):
    session_id: str
    message: str = Field(max_length=2000)


class AgentReportRequest(BaseModel):
    session_id: str
    focus: Literal["safety", "transit", "amenities", "building"] | None = None


class AgentPosterRequest(BaseModel):
    session_id: str
    template: Literal["card", "offline", "horizontal", "analytical"] = "card"


class AgentRefineRequest(BaseModel):
    session_id: str
    feedback: str = Field(max_length=1000)
