"""Bounded hierarchical deficit-round-robin admission queue."""

from __future__ import annotations

import asyncio
import itertools
import math
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Generic, TypeVar

from mote.contracts.inference.identity import TrustedSchedulingClass

PayloadT = TypeVar("PayloadT")


class QueueClosedError(RuntimeError):
    pass


class QueueFullError(RuntimeError):
    pass


class QueueDeadlineExceededError(TimeoutError):
    pass


@dataclass(frozen=True, slots=True)
class QueueEntry(Generic[PayloadT]):
    entry_id: int
    tenant_id: str
    project_id: str
    payload: PayloadT
    cost_units: int
    priority: int
    enqueued_at: float
    deadline: float

    def __post_init__(self) -> None:
        for name in ("entry_id", "cost_units"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"queue entry {name} must be a positive integer")
        if type(self.priority) is not int:
            raise ValueError("queue entry priority must be an integer")
        for name in ("tenant_id", "project_id"):
            value = getattr(self, name)
            if type(value) is not str or not value:
                raise ValueError(f"queue entry {name} must be a non-empty string")
        for name in ("enqueued_at", "deadline"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f"queue entry {name} must be finite")
        if self.deadline <= self.enqueued_at:
            raise ValueError("queue entry deadline must be after enqueue time")


@dataclass
class _ProjectQueue(Generic[PayloadT]):
    weight: int
    deficit: int = 0
    entries: deque[QueueEntry[PayloadT]] = field(default_factory=deque)


@dataclass
class _TenantQueue(Generic[PayloadT]):
    weight: int
    deficit: int = 0
    projects: dict[str, _ProjectQueue[PayloadT]] = field(default_factory=dict)
    active_projects: deque[str] = field(default_factory=deque)


class FairAdmissionQueue(Generic[PayloadT]):
    """Hard-bounded tenant/project DRR queue with deadline and aging.

    Capacity is reserved only while an entry is queued.  No budget or in-flight
    permit is held here; dispatch owns that later transition.
    """

    def __init__(
        self,
        *,
        capacity: int,
        base_quantum: int = 1,
        aging_seconds: float = 30.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if (
            type(capacity) is not int
            or capacity <= 0
            or type(base_quantum) is not int
            or base_quantum <= 0
            or type(aging_seconds) not in (int, float)
            or not math.isfinite(aging_seconds)
            or aging_seconds <= 0
        ):
            raise ValueError("queue capacity, quantum and aging must be positive")
        self._capacity = capacity
        self._base_quantum = base_quantum
        self._aging_seconds = aging_seconds
        self._clock = clock
        self._condition = asyncio.Condition()
        self._tenants: dict[str, _TenantQueue[PayloadT]] = {}
        self._active_tenants: deque[str] = deque()
        self._cancelled: set[int] = set()
        self._ids = itertools.count(1)
        self._size = 0
        self._unfinished = 0
        self._closed = False

    @property
    def size(self) -> int:
        return self._size

    @property
    def capacity(self) -> int:
        return self._capacity

    async def enqueue(
        self,
        payload: PayloadT,
        *,
        tenant_id: str,
        project_id: str,
        scheduling: TrustedSchedulingClass,
        deadline: float,
    ) -> QueueEntry[PayloadT]:
        if not tenant_id or not project_id:
            raise ValueError("tenant_id and project_id are required")
        async with self._condition:
            now = self._now()
            if self._closed:
                raise QueueClosedError("admission queue is closed")
            if deadline <= now:
                raise QueueDeadlineExceededError("queue deadline already expired")
            if self._size >= self._capacity:
                raise QueueFullError("admission queue is full")
            entry = QueueEntry(
                entry_id=next(self._ids),
                tenant_id=tenant_id,
                project_id=project_id,
                payload=payload,
                cost_units=scheduling.cost_units,
                priority=scheduling.priority,
                enqueued_at=now,
                deadline=deadline,
            )
            tenant = self._tenants.get(tenant_id)
            if tenant is None:
                tenant = _TenantQueue(weight=scheduling.tenant_weight)
                self._tenants[tenant_id] = tenant
                self._active_tenants.append(tenant_id)
            elif tenant.weight != scheduling.tenant_weight:
                raise ValueError("tenant weight cannot change while tenant has queued work")
            project = tenant.projects.get(project_id)
            if project is None:
                project = _ProjectQueue(weight=scheduling.project_weight)
                tenant.projects[project_id] = project
                tenant.active_projects.append(project_id)
            elif project.weight != scheduling.project_weight:
                raise ValueError("project weight cannot change while project has queued work")
            project.entries.append(entry)
            self._size += 1
            self._unfinished += 1
            self._condition.notify()
            return entry

    async def dequeue(self) -> QueueEntry[PayloadT]:
        async with self._condition:
            while True:
                self._discard_invalid_heads()
                if self._size:
                    entry = self._select()
                    if entry is not None:
                        return entry
                if self._closed:
                    raise QueueClosedError("admission queue is closed")
                await self._condition.wait()

    async def cancel(self, entry_id: int) -> bool:
        async with self._condition:
            if entry_id <= 0:
                return False
            self._cancelled.add(entry_id)
            before = self._size
            self._discard_invalid_heads()
            self._condition.notify_all()
            return self._size < before

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._condition.notify_all()

    async def task_done(self) -> None:
        async with self._condition:
            if self._unfinished <= 0:
                raise RuntimeError("admission queue task_done underflow")
            self._unfinished -= 1
            self._condition.notify_all()

    async def join(self) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: self._unfinished == 0)

    def _select(self) -> QueueEntry[PayloadT] | None:
        rounds = max(self._size * 2, len(self._active_tenants), 1)
        for _ in range(rounds):
            if not self._active_tenants:
                return None
            tenant_id = self._active_tenants[0]
            tenant = self._tenants[tenant_id]
            tenant.deficit += self._base_quantum * tenant.weight
            entry = self._select_project(tenant)
            self._active_tenants.rotate(-1)
            if entry is None:
                continue
            effective_cost = self._effective_cost(entry)
            if tenant.deficit < effective_cost:
                continue
            tenant.deficit -= effective_cost
            project = tenant.projects[entry.project_id]
            project.deficit -= effective_cost
            project.entries.popleft()
            self._size -= 1
            self._remove_empty(entry.tenant_id, entry.project_id)
            return entry
        return None

    def _select_project(
        self,
        tenant: _TenantQueue[PayloadT],
    ) -> QueueEntry[PayloadT] | None:
        for _ in range(max(len(tenant.active_projects), 1)):
            if not tenant.active_projects:
                return None
            project_id = tenant.active_projects[0]
            project = tenant.projects[project_id]
            project.deficit += self._base_quantum * project.weight
            entry = project.entries[0]
            tenant.active_projects.rotate(-1)
            if project.deficit >= self._effective_cost(entry):
                return entry
        return None

    def _effective_cost(self, entry: QueueEntry[PayloadT]) -> int:
        age_steps = int(max(self._now() - entry.enqueued_at, 0.0) / self._aging_seconds)
        priority_credit = max(entry.priority, 0) + age_steps
        return max(entry.cost_units - priority_credit, 1)

    def _discard_invalid_heads(self) -> None:
        now = self._now()
        for tenant_id in tuple(self._active_tenants):
            tenant = self._tenants[tenant_id]
            for project_id in tuple(tenant.active_projects):
                project = tenant.projects[project_id]
                while project.entries and (
                    project.entries[0].entry_id in self._cancelled or project.entries[0].deadline <= now
                ):
                    self._cancelled.discard(project.entries.popleft().entry_id)
                    self._size -= 1
                    self._unfinished -= 1
                self._remove_empty(tenant_id, project_id)

    def _remove_empty(self, tenant_id: str, project_id: str) -> None:
        tenant = self._tenants.get(tenant_id)
        if tenant is None:
            return
        project = tenant.projects.get(project_id)
        if project is not None and not project.entries:
            del tenant.projects[project_id]
            self._remove_once(tenant.active_projects, project_id)
        if not tenant.projects:
            del self._tenants[tenant_id]
            self._remove_once(self._active_tenants, tenant_id)

    @staticmethod
    def _remove_once(items: deque[str], value: str) -> None:
        try:
            items.remove(value)
        except ValueError:
            pass

    def _now(self) -> float:
        value = self._clock() if self._clock is not None else asyncio.get_running_loop().time()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("admission queue clock must return a finite number")
        return value
