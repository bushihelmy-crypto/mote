"""Runtime Agent and Role failures owned by the Agent bounded context."""

from __future__ import annotations

from typing import ClassVar

from mote.contracts.foundation.errors.base import MoteError
from mote.contracts.foundation.errors.codes import ErrorCode


class AgentError(MoteError):
    """Base for Role/agent-layer failures."""


class RoleContextNotSetError(AgentError):
    """A Role was used before its ``context`` was set."""

    default_code: ClassVar[ErrorCode] = ErrorCode.AGENT_CONTEXT_NOT_SET


class SessionResumeIdentityError(AgentError):
    """A session is being resumed into a Role of a different recorded identity.

    The rollout's ``session_meta`` records the ``role_class`` that created the
    session; resuming that log into an incompatible Role class would replay a
    history the new role was never designed for. Refused fail-closed rather than
    blindly replayed (mirrors the model that only the *same* agent identity may
    resume its own session).
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.SESSION_RESUME_IDENTITY_MISMATCH
