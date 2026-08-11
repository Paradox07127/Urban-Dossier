"""Category and sub-dataset configuration.

`CATEGORY_CONFIG` used to be a hand-maintained literal. It is now derived from
the metric registry in [`metrics.py`](metrics.py), which carries the same
weights plus the definition, unit, direction, grain and methodology version
that the literal had nowhere to put. The shape is unchanged, so every existing
consumer reads it exactly as before; `test_metric_registry.py` pins that
against a frozen copy of the original literal.

Edit weights in `metrics.py`, not here.
"""
from __future__ import annotations

from .metrics import build_category_config


CATEGORY_CONFIG = build_category_config()

DEFAULT_PRIORITY_ORDER = ["amenities", "transit", "safety"]


def signal_to_category_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for category_id, cfg in CATEGORY_CONFIG.items():
        for signal in cfg["signals"]:
            mapping[signal] = category_id
    return mapping
