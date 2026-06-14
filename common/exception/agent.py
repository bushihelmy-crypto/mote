"""Agent / role tier exceptions."""

from __future__ import annotations

from typing import ClassVar

from metagpt.common.exception.base import MetaGPTError
from metagpt.common.exception.codes import ErrorCode


class AgentError(MetaGPTError):
    """Base for Role/agent-layer failures."""


class RoleContextNotSetError(AgentError):
    """A Role was used before its ``context`` was set."""

    default_code: ClassVar[ErrorCode] = ErrorCode.AGENT_CONTEXT_NOT_SET
