#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RoleState — serializable runtime snapshot for checkpoint/recovery.
"""

from __future__ import annotations

from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, SerializeAsAny

from metagpt.common.const import DEFAULT_WORKSPACE_ROOT
from metagpt.common.schema import LLMCallContext, Message, MessageQueue, SerializationMixin


class RoleState(SerializationMixin):
    """Serializable runtime snapshot, used for cross-machine restoration."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # The message sequence fed to the LLM on the last think round — the `req`
    # ContextProvider assembles for aask (history + current user prompt). Kept
    # here so it survives checkpoint/recovery and is the data model a future
    # ContextManager operates on to manage historical context.
    context: LLMCallContext = Field(default_factory=LLMCallContext)

    msg_buffer: MessageQueue = Field(default_factory=MessageQueue, exclude=True)

    # Session
    session_id: str = Field(default_factory=lambda: uuid4().hex)
    # Fork lineage: the session_id this session was forked from (None for roots).
    # Recorded on the rollout's session_meta first line so listing can show the tree.
    parent_session_id: Optional[str] = None
    # Three working-directory paths, aligned with Claude Code (cwd/originalCwd/projectRoot):
    #   working_dir          — live cwd, follows `cd` (updated by the Bash tool each run)
    #   original_working_dir — set at startup, fallback only; never follows `cd`
    #   project_root         — project identity anchor (skills/context-protocol/memory); never follows `cd`
    working_dir: str = Field(default_factory=lambda: str(DEFAULT_WORKSPACE_ROOT.resolve()))
    original_working_dir: str = Field(default_factory=lambda: str(DEFAULT_WORKSPACE_ROOT.resolve()))
    project_root: str = Field(default_factory=lambda: str(DEFAULT_WORKSPACE_ROOT.resolve()))

    # Execution tracking
    latest_observed_msg: Optional[Message] = None
    recovered: bool = False
    last_end_output: str = Field(default="", exclude=True)

    # Routing
    addresses: set[str] = set()
    watch: set[str] = Field(default_factory=set)

    # Environment (not serialized)
    env: Optional[Any] = Field(default=None, exclude=True)

    # Internal flags
    _active: bool = PrivateAttr(default=False)
    _memory_ready: bool = PrivateAttr(default=False)

    # Shared file-read state, aligned with Claude Code's readFileState. Maps an
    # absolute path -> the file's mtime_ns at the moment it was last read. The
    # Read tool records here; the Write/Edit tools consult it to enforce
    # read-before-overwrite and to detect files changed since the last read.
    # Runtime-only (not part of the serialized checkpoint).
    _file_read_state: dict[str, int] = PrivateAttr(default_factory=dict)


class RoleStateController:
    """Behaviour over a :class:`RoleState` — keeps the DTO pure.

    ``RoleState`` is a plain serializable snapshot (a transport DTO): it carries
    fields only, no logic. This controller owns the small invariants that guard
    those fields (cwd fallback, falsy-message guard, the active-signal toggle,
    the file-read map). The Role exposes thin delegators onto these methods as
    its capability surface for tools and the framework, so tools never touch the
    raw state and the state stays free of behaviour.

    Holds the state by reference; the reference is stable for a Role's lifetime
    (``RoleState`` is mutated in place, never reassigned).
    """

    def __init__(self, state: "RoleState"):
        self._state = state

    @property
    def state(self) -> "RoleState":
        return self._state

    def get_cwd(self) -> str:
        """Live working directory, falling back to the startup dir; never empty."""
        try:
            return self._state.working_dir or self._state.original_working_dir
        except Exception:
            return self._state.original_working_dir

    def set_cwd(self, path: str) -> None:
        """Persist the live working directory (follows `cd`)."""
        self._state.working_dir = path

    def record_file_read(self, path: str, mtime_ns: int) -> None:
        """Record the mtime_ns observed when `path` was last read."""
        self._state._file_read_state[path] = mtime_ns

    def get_file_read_mtime(self, path: str) -> Optional[int]:
        """Return the mtime_ns recorded when `path` was last read, else None."""
        return self._state._file_read_state.get(path)

    def is_active(self) -> bool:
        """Read the react-loop active signal."""
        return self._state._active

    def set_active(self, value: bool) -> None:
        """Write the react-loop active signal."""
        self._state._active = value

    def deactivate(self) -> None:
        """Clear the active signal so the react loop stops after the current step."""
        self._state._active = False

    def put_message(self, message) -> None:
        """Push a message into the private buffer (falsy messages are ignored)."""
        if not message:
            return
        self._state.msg_buffer.push(message)

    @property
    def is_idle(self) -> bool:
        """A role is idle when its message buffer is empty."""
        return self._state.msg_buffer.empty()
