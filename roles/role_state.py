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
