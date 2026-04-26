"""Data providers for Urban Dossier backend."""
from .direct_provider import DirectQueryDataProvider
from .skill_provider import SkillDataProvider

__all__ = ["DirectQueryDataProvider", "SkillDataProvider"]
