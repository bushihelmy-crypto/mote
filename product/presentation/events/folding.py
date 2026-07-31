"""Host-neutral transcript folding semantics."""

from __future__ import annotations

from enum import Enum

GROUP_SEARCH_TOOLS = frozenset({"Search"})
GROUP_READ_TOOLS = frozenset({"Read"})
_FOLD_NONE = {"Edit"}


class FoldMode(Enum):
    """How a tool row folds in transcript state."""

    NONE = "none"
    GROUP = "group"
    DETAIL = "detail"


def fold_mode(name: str) -> FoldMode:
    if name in GROUP_SEARCH_TOOLS or name in GROUP_READ_TOOLS:
        return FoldMode.GROUP
    if name in _FOLD_NONE:
        return FoldMode.NONE
    return FoldMode.DETAIL


__all__ = ["FoldMode", "GROUP_READ_TOOLS", "GROUP_SEARCH_TOOLS", "fold_mode"]
