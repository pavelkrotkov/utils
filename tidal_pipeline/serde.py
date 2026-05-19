"""Small serialization helpers for TIDAL record models."""

from __future__ import annotations

from typing import Any, Dict, List


def parse_list(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [s for item in value if item is not None and (s := str(item).strip())]
    s = str(value).strip()
    return [s] if s else []


def parse_float_dict(value: Any) -> Dict[str, float]:
    if not isinstance(value, dict):
        return {}
    parsed: Dict[str, float] = {}
    for key, item in value.items():
        try:
            parsed[str(key)] = float(item)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"features[{key!r}] must be a number, got {item!r}") from exc
    return parsed


def parse_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def parse_string(value: Any) -> str:
    return str(value) if value is not None else ""
