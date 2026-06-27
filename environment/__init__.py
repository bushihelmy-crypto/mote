#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""metagpt.environment — codex-style multi-agent control plane.

A session-scoped control plane with hierarchical agent paths, per-agent
mailboxes with turn-atomic delivery, a concurrency limiter, and LRU residency
(idle agents unloaded to disk and rehydrated on demand). Replaces the old
single-broadcast-bus environment semantics: agents talk to the control plane,
not a broadcast loop.

Faithful Python/asyncio port of ``codex-rs/core/src/agent/*``.
"""

from __future__ import annotations

from metagpt.common.agent_control import (
    Lifecycle,
    SpawnContext,
    SpawnSpec,
    current_control,
    resolve_control,
    set_control,
    spawn_and_run,
)
from metagpt.environment.agent_path import AgentPath
from metagpt.environment.base_env import AgentEnvironment
from metagpt.environment.comms import CommGraph, CommKind
from metagpt.environment.control import AgentControl
from metagpt.common.exception import (
    AgentControlError,
    AgentLimitReached,
    AgentNotFound,
    AgentNotKnown,
    AgentPathExists,
)
from metagpt.environment.handle import ChildAgentHandle
from metagpt.environment.limiter import AgentExecutionLimiter
from metagpt.environment.mailbox import (
    DeliveryMode,
    InterAgentCommunication,
    Mailbox,
)
from metagpt.environment.mgx.mgx_env import MGXEnv
from metagpt.environment.registry import AgentMetadata, AgentRegistry
from metagpt.environment.residency import Residency
from metagpt.environment.runtime import AgentRuntime, AgentStatus
from metagpt.environment.turn_scheduler import EventDrivenScheduler
from metagpt.environment.scheduling import CronScheduler, CronService, CronTask
from metagpt.environment.store import ResidencyStore
from metagpt.environment.watching import FileChangeEvent, FileWatcher, FileWatchService

__all__ = [
    "AgentControl",
    "AgentControlError",
    "AgentEnvironment",
    "AgentExecutionLimiter",
    "AgentLimitReached",
    "AgentMetadata",
    "AgentNotFound",
    "AgentNotKnown",
    "AgentPath",
    "AgentPathExists",
    "AgentRegistry",
    "AgentRuntime",
    "AgentStatus",
    "ChildAgentHandle",
    "CommGraph",
    "CommKind",
    "CronScheduler",
    "CronService",
    "CronTask",
    "DeliveryMode",
    "EventDrivenScheduler",
    "FileChangeEvent",
    "FileWatcher",
    "FileWatchService",
    "InterAgentCommunication",
    "Lifecycle",
    "Mailbox",
    "MGXEnv",
    "Residency",
    "ResidencyStore",
    "SpawnContext",
    "SpawnSpec",
    "current_control",
    "resolve_control",
    "set_control",
    "spawn_and_run",
]
