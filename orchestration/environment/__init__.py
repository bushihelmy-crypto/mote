#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""mote.orchestration.environment — codex-style multi-agent control plane.

A session-scoped control plane with hierarchical agent paths, per-agent
mailboxes with turn-atomic delivery, a concurrency limiter, and LRU residency
(idle agents unloaded to disk and rehydrated on demand). Agents talk to the
control plane, not a broadcast loop.

Faithful Python/asyncio port of ``codex-rs/core/src/agent/*``.
"""

from __future__ import annotations

from mote.contracts.spawn import Lifecycle, SpawnContext, SpawnSpec
from mote.orchestration.environment.agent_path import AgentPath
from mote.orchestration.environment.base_env import AgentEnvironment
from mote.orchestration.environment.comms import CommGraph, CommKind
from mote.orchestration.environment.control import AgentControl
from mote.orchestration.environment.handle import ChildAgentHandle
from mote.orchestration.environment.limiter import AgentExecutionLimiter
from mote.orchestration.environment.mailbox import DeliveryMode, InterAgentCommunication, Mailbox
from mote.orchestration.environment.mote.mote_env import MoteEnv
from mote.orchestration.environment.registry import AgentMetadata, AgentRegistry
from mote.orchestration.environment.residency import Residency
from mote.orchestration.environment.runtime import AgentRuntime, AgentStatus
from mote.orchestration.environment.scheduling import CronScheduler, CronService, CronTask
from mote.orchestration.environment.spawn_policy import DefaultSpawnAdmissionPolicy, build_spawn_admission_policy
from mote.orchestration.environment.store import ResidencyStore
from mote.orchestration.environment.turn_scheduler import EventDrivenScheduler
from mote.runtime.agent.control import current_control, resolve_control, set_control, spawn_and_run
from mote.runtime.errors import AgentControlError, AgentLimitReached, AgentNotFound, AgentNotKnown, AgentPathExists

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
    "InterAgentCommunication",
    "Lifecycle",
    "Mailbox",
    "MoteEnv",
    "Residency",
    "ResidencyStore",
    "SpawnContext",
    "DefaultSpawnAdmissionPolicy",
    "SpawnSpec",
    "build_spawn_admission_policy",
    "current_control",
    "resolve_control",
    "set_control",
    "spawn_and_run",
]
