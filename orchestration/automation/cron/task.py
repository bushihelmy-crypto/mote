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
from enum import StrEnum
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from mote.contracts.artifact import ArtifactRef
from mote.orchestration.automation import AutomationTrigger


class DurableCronTaskId(str):
    """Stable identity for a task committed to the schedule store."""


class SessionCronTaskId(str):
    """Process-local identity that is never accepted by the durable codec."""


class CronMisfirePolicy(StrEnum):
    FIRE_ONCE = "fire_once"


class CronOverlapPolicy(StrEnum):
    FORBID = "forbid"


class CronDstPolicy(StrEnum):
    EARLIEST_FOLD_SKIP_GAP = "earliest_fold_skip_gap"


_MAX_INLINE_PROMPT_CHARS = 1_048_576


@dataclass(frozen=True, slots=True)
class CronTriggerIntent:
    """Versioned projection of one Cron occurrence into Automation dispatch."""

    schema_version: Literal[1]
    task_id: DurableCronTaskId | SessionCronTaskId
    task_revision: int
    target: str
    content: str
    scheduled_at_ms: int
    fired_at_ms: int
    attempt: int
    artifact_ref: ArtifactRef | None = None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported cron trigger schema version")
        if type(self.task_revision) is not int or self.task_revision < 0:
            raise ValueError("cron trigger task revision is invalid")
        if type(self.target) is not str or not self.target:
            raise ValueError("cron trigger target is invalid")
        if type(self.content) is not str or (not self.content and self.artifact_ref is None):
            raise ValueError("cron trigger content is invalid")
        if self.artifact_ref is not None and not isinstance(self.artifact_ref, ArtifactRef):
            raise TypeError("cron trigger artifact reference is invalid")
        if type(self.scheduled_at_ms) is not int or type(self.fired_at_ms) is not int:
            raise ValueError("cron trigger instants must be integers")
        if self.scheduled_at_ms < 0 or self.fired_at_ms < self.scheduled_at_ms:
            raise ValueError("cron trigger fired before its scheduled occurrence")
        if type(self.attempt) is not int or self.attempt < 1:
            raise ValueError("cron trigger attempt is invalid")

    @property
    def trigger_id(self) -> str:
        return f"cron:{self.task_id}:{self.task_revision}:{self.scheduled_at_ms}"

    def to_automation_trigger(self) -> AutomationTrigger:
        return AutomationTrigger(
            trigger_id=self.trigger_id,
            source_id=str(self.task_id),
            target=self.target,
            content=self.content,
            artifact_ref=self.artifact_ref,
            scheduled_at_ms=self.scheduled_at_ms,
            fired_at_ms=self.fired_at_ms,
            attempt=self.attempt,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "task_id": str(self.task_id),
            "task_revision": self.task_revision,
            "target": self.target,
            "content": self.content,
            "artifact_ref": None if self.artifact_ref is None else self.artifact_ref.to_dict(),
            "scheduled_at_ms": self.scheduled_at_ms,
            "fired_at_ms": self.fired_at_ms,
            "attempt": self.attempt,
        }

    @classmethod
    def from_dict(cls, value: object) -> "CronTriggerIntent":
        fields = {
            "schema_version",
            "task_id",
            "task_revision",
            "target",
            "content",
            "artifact_ref",
            "scheduled_at_ms",
            "fired_at_ms",
            "attempt",
        }
        if type(value) is not dict or set(value) != fields:
            raise ValueError("cron trigger wire shape is invalid")
        return cls(
            schema_version=value["schema_version"],
            task_id=DurableCronTaskId(value["task_id"]),
            task_revision=value["task_revision"],
            target=value["target"],
            content=value["content"],
            artifact_ref=None if value["artifact_ref"] is None else ArtifactRef.from_dict(value["artifact_ref"]),
            scheduled_at_ms=value["scheduled_at_ms"],
            fired_at_ms=value["fired_at_ms"],
            attempt=value["attempt"],
        )


@dataclass(frozen=True, slots=True)
class CronTask:
    """A single scheduled prompt-injection task."""

    id: DurableCronTaskId | SessionCronTaskId
    #: 5-field cron string (local time). Validated on write, re-validated on read.
    cron: str
    #: Prompt enqueued into the target agent when the task fires.
    prompt: str
    #: Epoch ms when the task was created. Anchor for missed-task detection.
    created_at: int
    prompt_artifact_ref: ArtifactRef | None = None
    revision: int = 0
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
    timezone_name: str = "UTC"
    misfire_policy: CronMisfirePolicy = CronMisfirePolicy.FIRE_ONCE
    overlap_policy: CronOverlapPolicy = CronOverlapPolicy.FORBID
    dst_policy: CronDstPolicy = CronDstPolicy.EARLIEST_FOLD_SKIP_GAP

    def __post_init__(self) -> None:
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("cron task revision must be a non-negative integer")
        if not isinstance(self.id, (DurableCronTaskId, SessionCronTaskId)):
            identity = DurableCronTaskId(self.id) if self.durable else SessionCronTaskId(self.id)
            object.__setattr__(self, "id", identity)
        if self.durable is not isinstance(self.id, DurableCronTaskId):
            raise ValueError("cron task identity does not match its durability scope")
        if len(self.id) != 32 or any(character not in "0123456789abcdef" for character in self.id):
            raise ValueError("cron task id must be a 128-bit lowercase hexadecimal identity")
        if type(self.cron) is not str or not self.cron:
            raise ValueError("cron expression must be a non-empty string")
        if type(self.prompt) is not str or not self.prompt:
            raise ValueError("cron prompt must be a non-empty string")
        if len(self.prompt) > _MAX_INLINE_PROMPT_CHARS:
            raise ValueError("cron prompt exceeds inline bound; publish payload as an ArtifactRef")
        if self.prompt_artifact_ref is not None and not isinstance(self.prompt_artifact_ref, ArtifactRef):
            raise TypeError("cron prompt artifact reference is invalid")
        if type(self.created_at) is not int or self.created_at < 0:
            raise ValueError("cron created_at must be a non-negative integer")
        if self.last_fired_at is not None and (
            type(self.last_fired_at) is not int or self.last_fired_at < self.created_at
        ):
            raise ValueError("cron last_fired_at must not precede created_at")
        for name in ("recurring", "permanent", "durable"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"cron {name} must be a boolean")
        for name in ("agent_id", "target_session_id"):
            value = getattr(self, name)
            if value is not None and (type(value) is not str or not value):
                raise ValueError(f"cron {name} must be a non-empty string when present")
        if type(self.timezone_name) is not str or not self.timezone_name:
            raise ValueError("cron timezone identity is invalid")
        ZoneInfo(self.timezone_name)
        if self.misfire_policy is not CronMisfirePolicy.FIRE_ONCE:
            raise ValueError("unsupported cron misfire policy")
        if self.overlap_policy is not CronOverlapPolicy.FORBID:
            raise ValueError("unsupported cron overlap policy")
        if self.dst_policy is not CronDstPolicy.EARLIEST_FOLD_SKIP_GAP:
            raise ValueError("unsupported cron DST policy")

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
        timezone_name: str = "UTC",
    ) -> "CronTask":
        """Construct a task, minting a non-reusable 128-bit identity."""
        return cls(
            id=(DurableCronTaskId if durable else SessionCronTaskId)(uuid.uuid4().hex),
            revision=0,
            cron=cron,
            prompt=prompt,
            created_at=created_at,
            recurring=recurring,
            permanent=permanent,
            durable=durable,
            agent_id=agent_id,
            target_session_id=target_session_id,
            timezone_name=timezone_name,
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
        self.__post_init__()
        if not self.durable or not isinstance(self.id, DurableCronTaskId):
            raise ValueError("session-only cron task cannot enter the durable codec")
        out: dict = {
            "id": str(self.id),
            "revision": self.revision,
            "cron": self.cron,
            "prompt": self.prompt,
            "prompt_artifact_ref": None if self.prompt_artifact_ref is None else self.prompt_artifact_ref.to_dict(),
            "created_at": self.created_at,
            "last_fired_at": self.last_fired_at,
            "recurring": self.recurring,
            "permanent": self.permanent,
            "agent_id": self.agent_id,
            "target_session_id": self.target_session_id,
            "timezone_name": self.timezone_name,
            "misfire_policy": self.misfire_policy.value,
            "overlap_policy": self.overlap_policy.value,
            "dst_policy": self.dst_policy.value,
        }
        return out

    @classmethod
    def from_dict(cls, data: object) -> "CronTask":
        """Strictly decode the canonical v1 task record."""
        fields = {
            "id",
            "revision",
            "cron",
            "prompt",
            "prompt_artifact_ref",
            "created_at",
            "last_fired_at",
            "recurring",
            "permanent",
            "agent_id",
            "target_session_id",
            "timezone_name",
            "misfire_policy",
            "overlap_policy",
            "dst_policy",
        }
        if type(data) is not dict or set(data) != fields:
            raise ValueError("cron task fields are not canonical")
        assert isinstance(data, dict)
        if type(data["id"]) is not str:
            raise ValueError("cron task id must be a string")
        if type(data["revision"]) is not int:
            raise ValueError("cron task revision must be an integer")
        if type(data["cron"]) is not str or type(data["prompt"]) is not str:
            raise ValueError("cron task text fields are invalid")
        if type(data["created_at"]) is not int:
            raise ValueError("cron created_at must be an integer")
        if data["last_fired_at"] is not None and type(data["last_fired_at"]) is not int:
            raise ValueError("cron last_fired_at must be an integer or null")
        if type(data["recurring"]) is not bool or type(data["permanent"]) is not bool:
            raise ValueError("cron task flags must be booleans")
        for name in ("agent_id", "target_session_id"):
            if data[name] is not None and type(data[name]) is not str:
                raise ValueError(f"cron {name} must be a string or null")
        for name in ("timezone_name", "misfire_policy", "overlap_policy", "dst_policy"):
            if type(data[name]) is not str:
                raise ValueError(f"cron {name} must be a string")
        return cls(
            id=DurableCronTaskId(data["id"]),
            revision=data["revision"],
            cron=data["cron"],
            prompt=data["prompt"],
            prompt_artifact_ref=(
                None if data["prompt_artifact_ref"] is None else ArtifactRef.from_dict(data["prompt_artifact_ref"])
            ),
            created_at=data["created_at"],
            last_fired_at=data["last_fired_at"],
            recurring=data["recurring"],
            permanent=data["permanent"],
            durable=True,
            agent_id=data["agent_id"],
            target_session_id=data["target_session_id"],
            timezone_name=data["timezone_name"],
            misfire_policy=CronMisfirePolicy(data["misfire_policy"]),
            overlap_policy=CronOverlapPolicy(data["overlap_policy"]),
            dst_policy=CronDstPolicy(data["dst_policy"]),
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


__all__ = [
    "CronTask",
    "CronJitterConfig",
    "DEFAULT_CRON_JITTER_CONFIG",
    "DurableCronTaskId",
    "SessionCronTaskId",
    "CronTriggerIntent",
    "CronDstPolicy",
    "CronMisfirePolicy",
    "CronOverlapPolicy",
]
