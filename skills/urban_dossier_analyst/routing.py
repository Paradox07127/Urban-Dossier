"""Deterministic intent routing for /api/agent/ask -- EXPANSION_PLAN 3.4.

Four intents, decided by rules rather than by the model, because the router's
job is to keep whole categories of request out of the analysis chain and a
prompt cannot promise that. The acceptance criterion is specifically that
``out_of_scope`` never reaches the agent loop; here that is enforced by the
caller short-circuiting before the skill is even imported, so the gate holds
on hosts with no sandbox and no model at all.

The rules are deliberately conservative. Misrouting a real analysis question
to a refusal is far worse than letting an odd question through to the agent,
so anything ambiguous falls through to ``NEW_ANALYSIS`` and only unmistakable
patterns short-circuit. This is a coarse net in front of the model, not a
classifier trying to be clever.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Intent(str, Enum):
    ASK_FROM_EVIDENCE = "ask_from_evidence"
    NEW_ANALYSIS = "new_analysis"
    META_HELP = "meta_help"
    OUT_OF_SCOPE = "out_of_scope"


@dataclass(frozen=True)
class Route:
    intent: Intent
    # The rule that fired, for the trace. Auditability is the point: a routed
    # request must be able to say why it went where it went.
    rule: str


# Unmistakably not this product's job. Each pattern should be embarrassing to
# argue about; the moment one needs a debate it does not belong here.
_OUT_OF_SCOPE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in [
        ("code_request", r"\b(write|debug|fix|refactor)\b.{0,40}\b(code|function|script|program|bug)\b"),
        ("code_request_zh", r"(写|调试|修复).{0,20}(代码|函数|脚本|程序)"),
        ("creative_writing", r"\b(write|compose)\b.{0,30}\b(poem|story|song|essay|lyrics)\b"),
        ("creative_writing_zh", r"(写|作).{0,10}(诗|小说|歌|散文|作文)"),
        ("weather_forecast", r"\b(weather|forecast|temperature)\b.{0,30}\b(today|tomorrow|this week|now)\b"),
        ("weather_forecast_zh", r"(今天|明天|本周).{0,10}(天气|气温)|天气预报"),
        ("other_city", r"\b(in|for|about)\s+(boston|chicago|los angeles|san francisco|london|paris|tokyo|beijing|shanghai|houston|miami|seattle)\b"),
        ("news_politics", r"\b(election|president|stock market|crypto|bitcoin)\b"),
    ]
)

# Questions about the tool itself, answerable from the registry with no model.
_META_HELP_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in [
        ("capabilities", r"\b(what|which)\b.{0,20}\b(can you|do you|are you able)\b"),
        ("capabilities_2", r"\bhow (do|does|can) (i|you|this|it)\b.{0,30}\b(work|use)\b"),
        ("capabilities_zh", r"(你能|你会|怎么用|如何使用|有什么功能|支持什么)"),
        ("data_inventory", r"\b(what|which)\b.{0,20}\b(data|datasets?|sources?|metrics?)\b.{0,20}\b(have|use|available)\b"),
        ("data_inventory_zh", r"(有|用了?)(哪些|什么)(数据|指标)"),
        ("bare_help", r"^\s*(help|\?+|帮助|怎么办)\s*$"),
    ]
)

# References to an existing result, meaningful only when history exists.
_EVIDENCE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in [
        ("anaphora", r"\b(that|this|those|these|it)\b.{0,20}\b(score|number|result|rating|value)\b"),
        ("why_probe", r"^\s*(why|how come)\b"),
        ("anaphora_zh", r"(这个|那个|上面|刚才|刚刚).{0,10}(分|数|结果|评分)"),
        ("why_probe_zh", r"^\s*(为什么|为啥|怎么会)"),
        ("explain_more", r"\b(explain|elaborate|break (that|this) down|more detail)\b"),
        ("explain_more_zh", r"(解释|展开|详细说|细说)"),
    ]
)

# A location mention pulls a message back toward fresh analysis even when it
# also contains evidence-flavoured words.
_LOCATION_HINT = re.compile(
    r"\b(street|avenue|ave|blvd|boulevard|square|park|brooklyn|queens|manhattan|bronx|"
    r"staten island|harlem|williamsburg|astoria|address|near|around)\b|"
    r"\d{1,4}\s+\w+\s+(st|street|ave)|大道|街|附近|地址",
    re.IGNORECASE,
)


def route_intent(message: str, has_history: bool = False) -> Route:
    """Route one user message. Pure and deterministic: same input, same route."""
    text = (message or "").strip()
    if not text:
        return Route(Intent.META_HELP, "empty_message")

    for name, pattern in _OUT_OF_SCOPE_PATTERNS:
        if pattern.search(text):
            # A location mention rescues it: "weather near Astoria" is odd but
            # arguably about a place we cover; let the agent disappoint them
            # with context rather than refusing a borderline case.
            if _LOCATION_HINT.search(text) and name in ("weather_forecast", "weather_forecast_zh"):
                break
            return Route(Intent.OUT_OF_SCOPE, name)

    for name, pattern in _META_HELP_PATTERNS:
        if pattern.search(text):
            if _LOCATION_HINT.search(text):
                break  # "what can you tell me about Astoria" is analysis, not meta
            return Route(Intent.META_HELP, name)

    if has_history and not _LOCATION_HINT.search(text):
        for name, pattern in _EVIDENCE_PATTERNS:
            if pattern.search(text):
                return Route(Intent.ASK_FROM_EVIDENCE, name)

    return Route(Intent.NEW_ANALYSIS, "default")


OUT_OF_SCOPE_ANSWER = (
    "I analyze New York City neighborhoods: safety, transit access, amenities "
    "and building conditions around a location, from the city's open data. "
    "That question falls outside what I can answer with evidence, so rather "
    "than guess I'll pass. Ask me about a place in NYC."
)


def meta_help_answer(tool_names: list[str]) -> str:
    """The capabilities answer, built from the released tool list at call time
    so it cannot drift from what is actually available."""
    tools = ", ".join(sorted(tool_names)) if tool_names else "none currently released"
    return (
        "I answer questions about New York City locations using city open "
        "data: overall/safety/transit/amenity scores with uncertainty "
        "intervals, comparisons between places, walking isochrones, trends "
        "and intervention simulations. Available tools this release: "
        f"{tools}. Ask about an address or neighborhood to start."
    )
