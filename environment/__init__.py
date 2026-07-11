#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""mote.environment — codex-style multi-agent control plane.

A session-scoped control plane with hierarchical agent paths, per-agent
mailboxes with turn-atomic delivery, a concurrency limiter, and LRU residency
(idle agents unloaded to disk and rehydrated on demand). Replaces the old
single-broadcast-bus environment semantics: agents talk to the control plane,
not a broadcast loop.

Faithful Python/asyncio port of ``codex-rs/core/src/agent/*``.
"""

from __future__ import annotations

from mote.common.agent_control import (
    Lifecycle,
    SpawnContext,
    SpawnSpec,
    current_control,
    resolve_control,
    set_control,
    spawn_and_run,
)
from mote.common.exception import AgentControlError, AgentLimitReached, AgentNotFound, AgentNotKnown, AgentPathExists
from mote.environment.agent_path import AgentPath
from mote.environment.base_env import AgentEnvironment
from mote.environment.comms import CommGraph, CommKind
from mote.environment.control import AgentControl
from mote.environment.handle import ChildAgentHandle
from mote.environment.limiter import AgentExecutionLimiter
from mote.environment.mailbox import DeliveryMode, InterAgentCommunication, Mailbox
from mote.environment.mote.mote_env import MoteEnv
from mote.environment.registry import AgentMetadata, AgentRegistry
from mote.environment.residency import Residency
from mote.environment.runtime import AgentRuntime, AgentStatus
from mote.environment.scheduling import CronScheduler, CronService, CronTask
from mote.environment.spawn_gate import SpawnGate
from mote.environment.store import ResidencyStore
from mote.environment.turn_scheduler import EventDrivenScheduler
from mote.environment.watching import FileChangeEvent, FileWatcher, FileWatchService

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
    "MoteEnv",
    "Residency",
    "ResidencyStore",
    "SpawnContext",
    "SpawnGate",
    "SpawnSpec",
    "current_control",
    "resolve_control",
    "set_control",
    "spawn_and_run",
]
