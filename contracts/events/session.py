"""Domain-owned event contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Optional

if TYPE_CHECKING:
    pass

SESSION_START = "session_start"

SESSION_END = "session_end"

TURN_START = "turn_start"

TURN_END = "turn_end"

RUN_LEASE = "run_lease"

RUNTIME_DURABILITY_CHANGED = "runtime_durability_changed"


@dataclass
class SessionStartEvent:
    """A session began (or resumed). Carries the identity to seed the rollout."""

    session_id: str = ""
    parent_session_id: Optional[str] = None
    working_dir: str = ""
    original_working_dir: str = ""
    project_root: str = ""
    model: Optional[str] = None
    role_class: Optional[str] = None
    source: str = "startup"  # SessionStart "source" matcher (startup|resume|...)

    name: ClassVar[str] = SESSION_START


@dataclass
class SessionEndEvent:
    """The session is tearing down."""

    session_id: str = ""

    name: ClassVar[str] = SESSION_END


@dataclass
class TurnStartEvent:
    """A react turn is starting."""

    turn_id: str = ""

    name: ClassVar[str] = TURN_START


@dataclass
class TurnEndEvent:
    """A react turn finished. Carries the per-turn runtime snapshot."""

    turn_id: str = ""
    working_dir: str = ""
    model: Optional[str] = None
    token_state: Optional[dict] = None

    name: ClassVar[str] = TURN_END


@dataclass
class RuntimeDurabilityChangedEvent:
    """A managed runtime's recoverable revision fell behind or caught up."""

    runtime_id: str = ""
    runtime_kind: str = ""
    alias: str = "default"
    state: str = "not_configured"
    current_revision: int = 0
    recoverable_revision: int = 0
    detail: str = ""

    name: ClassVar[str] = RUNTIME_DURABILITY_CHANGED


@dataclass
class RunLeaseEvent:
    """Low-frequency ownership lifecycle telemetry; never a truth source."""

    phase: str = ""
    run_id: str = ""
    owner_id: str = ""
    fencing_token: int = 0
    expires_at: float = 0.0
    reason: str = ""

    name: ClassVar[str] = RUN_LEASE
