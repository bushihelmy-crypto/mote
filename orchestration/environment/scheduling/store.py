#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CronTaskStore — durable JSON persistence + session-only in-memory tasks.

Disk is the source of truth for ``durable=True`` tasks: they live in a single
workspace-global file::

    {DEFAULT_WORKSPACE_ROOT}/.agent_schedules/scheduled_tasks.json

with shape ``{"tasks": [...]}``. Writes are atomic (tmp file + ``os.replace``) so
a crash mid-write never corrupts the file. ``durable=False`` tasks are held in
process memory only — they fire this session but die with the process.

:meth:`list` merges both sources; :meth:`mtime` exposes the file modification
time so the scheduler can detect external edits and hot-reload.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from mote.orchestration.environment.scheduling.task import CronTask
from mote.runtime.disk import atomic_write, mtime_seconds
from mote.runtime.logging import log_class
from mote.runtime.paths import DEFAULT_WORKSPACE_ROOT

#: Directory name under the workspace root holding the schedule file.
SCHEDULES_DIRNAME = ".agent_schedules"
#: The schedule file name inside that directory.
SCHEDULES_FILENAME = "scheduled_tasks.json"


def _default_base_dir() -> Path:
    return Path(DEFAULT_WORKSPACE_ROOT) / SCHEDULES_DIRNAME


@log_class(level="DEBUG", exclude={"path", "mtime"})
class CronTaskStore:
    """Reads/writes durable tasks to JSON and holds session-only tasks in memory."""

    def __init__(self, base_dir: Optional[str] = None):
        base = Path(base_dir) if base_dir is not None else _default_base_dir()
        self._dir = base
        self._path = base / SCHEDULES_FILENAME
        # Session-only (durable=False) tasks, keyed by id.
        self._session: Dict[str, CronTask] = {}

    @property
    def path(self) -> Path:
        return self._path

    def mtime(self) -> Optional[float]:
        """File modification time, or ``None`` when the file does not exist."""
        return mtime_seconds(self._path)

    # ------------------------------------------------------------------
    # Durable load / save
    # ------------------------------------------------------------------
    def load(self) -> List[CronTask]:
        """Load durable tasks from disk, skipping malformed entries.

        A missing/empty/corrupt file yields an empty list (best-effort); a single
        bad entry never blocks the whole file.
        """
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return []
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return []
        if not isinstance(parsed, dict):
            return []
        entries = parsed.get("tasks")
        if not isinstance(entries, list):
            return []

        out: List[CronTask] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                task = CronTask.from_dict(entry)
            except (KeyError, TypeError, ValueError):
                continue
            task.durable = True
            out.append(task)
        return out

    def save(self, tasks: List[CronTask]) -> None:
        """Atomically overwrite the durable schedule file with ``tasks``.

        Only durable tasks are written. An empty list writes an empty file
        (rather than deleting) so a watcher still sees a change on last-removed.
        """
        durable = [t for t in tasks if t.durable]
        body = {"tasks": [t.to_dict() for t in durable]}
        self._dir.mkdir(parents=True, exist_ok=True)
        text = json.dumps(body, indent=2, ensure_ascii=False) + "\n"
        # Synchronous atomic write via the consolidated L0 primitive (same
        # tmp+fsync+replace as before). This store runs inside the scheduler's
        # async tick and relies on synchronous read-after-write (``load`` and
        # ``mtime`` are read right after ``save``), so the write must complete
        # before returning — it is intentionally NOT routed through the
        # DiskWriter's deferred queue.
        atomic_write(self._path, text.encode("utf-8"))

    # ------------------------------------------------------------------
    # Mutation API
    # ------------------------------------------------------------------
    def add(self, task: CronTask) -> CronTask:
        """Add a task. Durable tasks are persisted; session-only stay in memory."""
        if task.durable:
            tasks = self.load()
            tasks.append(task)
            self.save(tasks)
        else:
            self._session[task.id] = task
        return task

    def remove(self, ids: List[str]) -> int:
        """Remove tasks by id from both stores. Returns the count removed."""
        id_set = set(ids)
        if not id_set:
            return 0
        removed = 0

        # Session store first.
        for tid in list(id_set):
            if self._session.pop(tid, None) is not None:
                removed += 1

        # Durable store.
        tasks = self.load()
        remaining = [t for t in tasks if t.id not in id_set]
        if len(remaining) != len(tasks):
            removed += len(tasks) - len(remaining)
            self.save(remaining)
        return removed

    def session_tasks(self) -> List[CronTask]:
        """The session-only (in-memory, ``durable=False``) tasks."""
        return list(self._session.values())

    def list(self) -> List[CronTask]:
        """Durable tasks (from disk) merged with session-only tasks (from memory)."""
        return [*self.load(), *self._session.values()]

    def get(self, task_id: str) -> Optional[CronTask]:
        """Look up a single task by id across both stores."""
        for task in self.list():
            if task.id == task_id:
                return task
        return None


__all__ = ["CronTaskStore", "SCHEDULES_DIRNAME", "SCHEDULES_FILENAME"]
