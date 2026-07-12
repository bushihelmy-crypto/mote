#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""metagpt.environment.scheduling — cron / scheduled-task subsystem.

Register a 5-field cron string + prompt against a target agent; when the cron
fires, the prompt is injected as a fresh turn (as if the user sent a message at
that instant). Disk-backed durable tasks survive restarts; session-only tasks
live in memory. A single-writer lock prevents double-fires across sessions in one
workspace, deterministic jitter spreads load, and idle gating avoids interrupting
a running turn.

Layering: the core :class:`CronScheduler` is control-plane-agnostic (pure
``on_fire`` callback); :class:`CronService` is the glue that wires fires to
``AgentControl.send_input``.
"""

from __future__ import annotations

from metagpt.environment.scheduling.cron import (
    compute_next_cron_run,
    cron_to_human,
    parse_cron_expression,
)
from metagpt.environment.scheduling.lock import SchedulerLock
from metagpt.environment.scheduling.scheduler import CronScheduler
from metagpt.environment.scheduling.service import CronService
from metagpt.environment.scheduling.store import CronTaskStore
from metagpt.environment.scheduling.task import (
    DEFAULT_CRON_JITTER_CONFIG,
    CronJitterConfig,
    CronTask,
)

__all__ = [
    "CronTask",
    "CronJitterConfig",
    "DEFAULT_CRON_JITTER_CONFIG",
    "parse_cron_expression",
    "compute_next_cron_run",
    "cron_to_human",
    "CronTaskStore",
    "SchedulerLock",
    "CronScheduler",
    "CronService",
]
