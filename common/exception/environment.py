"""Agent control-plane tier exceptions (``metagpt.environment``).

Port of the relevant ``CodexErr`` variants used by the multi-agent control
plane (``codex-rs/core/src/agent``). Reparented onto :class:`MetaGPTError` so
control-plane failures carry a stable :class:`ErrorCode` and serialize via
``to_dict()`` like every other typed error. The custom ``__init__`` signatures
(``max_agents`` / ``agent_id`` / ``agent_path`` / ``reference``) are preserved
because the control plane reads those attributes; each is also mirrored into
``context`` so it surfaces in ``to_dict()``.
"""

from __future__ import annotations

from typing import ClassVar, Optional

from metagpt.common.exception.base import MetaGPTError
from metagpt.common.exception.codes import ErrorCode


class AgentControlError(MetaGPTError):
    """Base for every control-plane error raised inside ``metagpt.environment``."""

    default_code: ClassVar[ErrorCode] = ErrorCode.AGENT_CONTROL


class AgentLimitReached(AgentControlError):
    """Raised when no execution / residency slot is available.

    Mirrors codex ``CodexErr::AgentLimitReached { max_threads }``.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.AGENT_LIMIT_REACHED

    def __init__(self, max_agents: Optional[int] = None, message: Optional[str] = None):
        self.max_agents = max_agents
        if message is None:
            message = (
                f"agent limit reached (max_agents={max_agents})"
                if max_agents is not None
                else "agent limit reached"
            )
        super().__init__(message, max_agents=max_agents)


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
