"""Stable child-Agent spawn request contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional


class Lifecycle(Enum):
    MANAGED = "managed"
    EPHEMERAL = "ephemeral"


class ContextPolicy(Enum):
    FRESH = "fresh"
    SHARE_PARENT = "share_parent"


@dataclass
class SpawnContext:
    parent_id: Optional[str] = None
    agent_path: Optional[Any] = None
    cwd: Optional[str] = None
    config: Optional[Any] = None
    parent_cost_tracker: Optional[Any] = None
    parent_session_id: str = ""


@dataclass
class SpawnSpec:
    role_factory: Callable[[SpawnContext], Any]
    nickname: Optional[str] = None
    parent_id: Optional[str] = None
    lifecycle: Lifecycle = Lifecycle.EPHEMERAL
    cost_rollup: bool = True
    watch_completion: bool = True
    max_depth: Optional[int] = None
    timeout_seconds: Optional[float] = None
    agent_role: str = ""
    context_policy: ContextPolicy = ContextPolicy.FRESH


__all__ = ["ContextPolicy", "Lifecycle", "SpawnContext", "SpawnSpec"]
