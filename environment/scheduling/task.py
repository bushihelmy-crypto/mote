#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Task model + jitter tuning for the cron subsystem (port of cronTasks.ts types).

A :class:`CronTask` pairs a 5-field cron string with a prompt to inject into a
target agent when it fires. Tasks come in two flavors:

  * **one-shot** (``recurring=False``) — fire once, then auto-delete,
  * **recurring** (``recurring=True``) — fire on schedule, reschedule from now,
    persist until explicitly deleted or auto-expired after
    ``recurring_max_age_ms``.

Orthogonally, ``durable=True`` tasks are written to disk (survive restart) while
``durable=False`` tasks live only in process memory. ``permanent=True`` tasks are
exempt from age-based auto-expiry.

:class:`CronJitterConfig` holds the deterministic-jitter tuning knobs used by the
scheduler to spread a thundering herd of identically-scheduled tasks.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional


@dataclass
class CronTask:
    """A single scheduled prompt-injection task."""

    id: str
    #: 5-field cron string (local time). Validated on write, re-validated on read.
    cron: str
    #: Prompt enqueued into the target agent when the task fires.
    prompt: str
    #: Epoch ms when the task was created. Anchor for missed-task detection.
    created_at: int
    #: Epoch ms of the most recent fire. Written back after each recurring fire so
    #: next-fire computation survives restarts. Never set for one-shots.
    last_fired_at: Optional[int] = None
    #: When true, reschedule after firing instead of deleting.
    recurring: bool = False
    #: When true, exempt from ``recurring_max_age_ms`` auto-expiry.
    permanent: bool = False
    #: When true, write to disk; otherwise hold in process memory only.
    durable: bool = True
    #: Runtime-only. When set, the task was created by an in-process teammate.
    agent_id: Optional[str] = None
    #: Session id of the agent that should receive the injected prompt.
    target_session_id: Optional[str] = None

    @classmethod
    def new(
        cls,
        cron: str,
        prompt: str,
        created_at: int,
        *,
        recurring: bool = False,
        permanent: bool = False,
        durable: bool = True,
        agent_id: Optional[str] = None,
        target_session_id: Optional[str] = None,
    ) -> "CronTask":
        """Construct a task, minting a fresh 8-hex-char id.

        8 hex chars is plenty for the 50-task cap and gives the jitter hash a
        clean u32 to read (see :func:`cron._jitter_frac`).
        """
        return cls(
            id=uuid.uuid4().hex[:8],
            cron=cron,
            prompt=prompt,
            created_at=created_at,
            recurring=recurring,
            permanent=permanent,
            durable=durable,
            agent_id=agent_id,
            target_session_id=target_session_id,
        )

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        """Serialize to the on-disk shape.

        The runtime-only ``durable`` flag is dropped (everything on disk is
        durable by definition) and ``None`` optionals are omitted to keep the
        file shape compact, mirroring ``writeCronTasks``.
        """
        out: dict = {
            "id": self.id,
            "cron": self.cron,
            "prompt": self.prompt,
            "created_at": self.created_at,
        }
        if self.last_fired_at is not None:
            out["last_fired_at"] = self.last_fired_at
        if self.recurring:
            out["recurring"] = True
        if self.permanent:
            out["permanent"] = True
        if self.agent_id is not None:
            out["agent_id"] = self.agent_id
        if self.target_session_id is not None:
            out["target_session_id"] = self.target_session_id
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "CronTask":
        """Rebuild from :meth:`to_dict` output. Disk tasks are durable."""
        return cls(
            id=data["id"],
            cron=data["cron"],
            prompt=data["prompt"],
            created_at=int(data["created_at"]),
            last_fired_at=(int(data["last_fired_at"]) if data.get("last_fired_at") is not None else None),
            recurring=bool(data.get("recurring", False)),
            permanent=bool(data.get("permanent", False)),
            durable=bool(data.get("durable", True)),
            agent_id=data.get("agent_id"),
            target_session_id=data.get("target_session_id"),
        )


@dataclass(frozen=True)
class CronJitterConfig:
    """Deterministic-jitter tuning knobs (port of cronTasks.ts ``CronJitterConfig``)."""

    #: Recurring forward delay as a fraction of the interval between fires.
    recurring_frac: float = 0.1
    #: Upper bound on recurring forward delay regardless of interval length.
    recurring_cap_ms: int = 15 * 60 * 1000
    #: One-shot backward lead: maximum ms a task may fire early.
    one_shot_max_ms: int = 90 * 1000
    #: One-shot backward lead: minimum ms a task fires early when the gate matches.
    one_shot_floor_ms: int = 0
    #: Jitter fires landing on minutes where ``minute % N == 0`` (30 -> :00/:30).
    one_shot_minute_mod: int = 30
    #: Recurring tasks auto-expire this many ms after creation. ``0`` = unlimited.
    recurring_max_age_ms: int = 7 * 24 * 60 * 60 * 1000


#: The pre-config defaults preserved from upstream.
DEFAULT_CRON_JITTER_CONFIG = CronJitterConfig()


__all__ = ["CronTask", "CronJitterConfig", "DEFAULT_CRON_JITTER_CONFIG"]
