"""Small value-only merge primitives shared by Product config domains."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def union_dedupe(base: list[Any], overlay: list[Any]) -> list[Any]:
    result: list[Any] = []
    for item in [*base, *overlay]:
        if item not in result:
            result.append(deepcopy(item))
    return result


def deep_merge(base: Any, overlay: Any) -> Any:
    if isinstance(base, dict) and isinstance(overlay, dict):
        result = deepcopy(base)
        for key, value in overlay.items():
            result[key] = deep_merge(result[key], value) if key in result else deepcopy(value)
        return result
    if isinstance(base, list) and isinstance(overlay, list):
        return union_dedupe(base, overlay)
    return deepcopy(overlay)


__all__ = ["deep_merge", "union_dedupe"]
