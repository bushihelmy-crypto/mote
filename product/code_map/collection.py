"""Working-set collection for Product CodeMap context."""

from __future__ import annotations

from collections.abc import Callable
from typing import Optional


def _read_paths(provider: Optional[Callable[[], list]]) -> list:
    if provider is None:
        return []
    try:
        return list(provider())
    except Exception:  # noqa: BLE001 - advisory context must not break a turn
        return []


def collect_code_map_files(
    get_touched_files: Optional[Callable[[], list]],
    get_glimpsed_files: Optional[Callable[[], list]],
) -> list:
    """Merge read and search-glimpsed paths, preserving working-set precedence."""
    touched = _read_paths(get_touched_files)
    glimpsed = _read_paths(get_glimpsed_files)
    seen = set(touched)
    return touched + [path for path in glimpsed if path not in seen]


__all__ = ["collect_code_map_files"]
