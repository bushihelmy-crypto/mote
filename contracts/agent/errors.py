"""Stable Agent control-plane errors.

Port of the relevant ``CodexErr`` variants used by the multi-agent control
plane (``codex-rs/core/src/agent``). Rooted on :class:`MoteError` so
control-plane failures carry a stable :class:`ErrorCode` and serialize via
``to_dict()`` like every other typed error. The custom ``__init__`` signatures
(``limit`` / ``agent_id`` / ``agent_path`` / ``reference``) are preserved
because the control plane reads those attributes; each is also mirrored into
``context`` so it surfaces in ``to_dict()``.
"""

from __future__ import annotations

from typing import ClassVar, Optional

from mote.contracts.foundation.errors.base import MoteError
from mote.contracts.foundation.errors.codes import ErrorCode


class AgentControlError(MoteError):
    """Base for every control-plane error raised inside ``mote.orchestration.agents``."""

    default_code: ClassVar[ErrorCode] = ErrorCode.AGENT_CONTROL


class AgentLimitReached(AgentControlError):
    """Raised when no execution / residency slot is available.

    The optional numeric limit is diagnostic only; typed receipts carry the
    authoritative capacity dimension and disposition.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.AGENT_LIMIT_REACHED

    def __init__(self, limit: Optional[int] = None, message: Optional[str] = None):
        self.limit = limit
        if message is None:
            message = f"Agent capacity reached (limit={limit})" if limit is not None else "Agent capacity reached"
        super().__init__(message, limit=limit)


class AgentNotFound(AgentControlError):
    """Raised when a referenced agent/session is not live.

    Mirrors codex ``CodexErr::ThreadNotFound``.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.AGENT_NOT_FOUND

    def __init__(self, agent_id: Optional[str] = None, message: Optional[str] = None):
        self.agent_id = agent_id
        if message is None:
            message = f"agent `{agent_id}` not found" if agent_id else "agent not found"
        super().__init__(message, agent_id=agent_id)


class AgentPathExists(AgentControlError):
    """Raised when reserving an agent path that is already taken.

    Mirrors codex ``CodexErr::UnsupportedOperation("agent path ... already exists")``.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.AGENT_PATH_EXISTS

    def __init__(self, agent_path: Optional[str] = None, message: Optional[str] = None):
        self.agent_path = agent_path
        if message is None:
            message = f"agent path `{agent_path}` already exists" if agent_path else "agent path already exists"
        super().__init__(message, agent_path=agent_path)


class AgentNotKnown(AgentControlError):
    """Raised when an agent reference cannot be resolved to a known agent.

    Mirrors codex ``CodexErr::UnsupportedOperation("live agent path ... not found")``
    / "no available agent nicknames".
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.AGENT_NOT_KNOWN

    def __init__(self, reference: Optional[str] = None, message: Optional[str] = None):
        self.reference = reference
        if message is None:
            message = f"agent reference `{reference}` is not known" if reference else "agent reference not known"
        super().__init__(message, reference=reference)
