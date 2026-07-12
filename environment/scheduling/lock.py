#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""SchedulerLock — single-writer lease lock for the durable schedule (port of cronTasksLock.ts).

When multiple sessions run in the same workspace, only one should drive the cron
scheduler for durable tasks — otherwise both would fire the same on-disk task.
The first session to acquire this lock becomes the writer; others stay passive
and periodically probe. If the owner dies (PID no longer running), a passive
session recovers the stale lock and takes over.

Pattern: ``os.open(O_CREAT | O_EXCL)`` atomic create, PID liveness probe,
stale-lock recovery, cleanup on :meth:`release`.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from metagpt.common.logs import log_class

#: Lock file name, alongside the schedule file in the schedules dir.
LOCK_FILENAME = "scheduled_tasks.lock"


def _process_running(pid: int) -> bool:
    """Best-effort liveness probe via ``os.kill(pid, 0)``."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another user — treat as alive.
        return True
    except OSError:
        return False
    return True


@log_class(level="DEBUG", exclude={"path", "is_held"})
class SchedulerLock:
    """An O_EXCL lease lock keyed by ``session_id`` + PID."""

    def __init__(self, session_id: str, base_dir: str):
        self.session_id = session_id
        self._dir = Path(base_dir)
        self._path = self._dir / LOCK_FILENAME
        self._held = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def is_held(self) -> bool:
        return self._held

    def _body(self) -> str:
        return json.dumps(
            {"session_id": self.session_id, "pid": os.getpid(), "acquired_at": int(time.time() * 1000)}
        )

    def _read(self) -> Optional[dict]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _try_create_exclusive(self) -> bool:
        """Atomic test-and-set create. Returns True on success, False if it exists."""
        self._dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        try:
            os.write(fd, self._body().encode("utf-8"))
        finally:
            os.close(fd)
        return True

    def acquire(self) -> bool:
        """Acquire the lock for this session.

        Returns True on success, False if another live session holds it. Idempotent
        re-acquire refreshes our PID. A stale lock (dead PID / corrupt) is unlinked
        and the exclusive create retried once.
        """
        if self._try_create_exclusive():
            self._held = True
            return True

        existing = self._read()

        # Already ours — refresh PID (e.g. after resume the session id is the same
        # but the process is new) so others see a live owner.
        if existing and existing.get("session_id") == self.session_id:
            if existing.get("pid") != os.getpid():
                self._path.write_text(self._body(), encoding="utf-8")
            self._held = True
            return True

        # Held by a live foreign session — blocked.
        if existing and _process_running(int(existing.get("pid", -1))):
            return False

        # Stale or corrupt — unlink and retry the exclusive create once.
        try:
            self._path.unlink()
        except OSError:
            pass
        if self._try_create_exclusive():
            self._held = True
            return True
        # Another session won the recovery race.
        return False

    def refresh(self) -> None:
        """Rewrite the lock body (PID + timestamp) if we hold it."""
        if self._held:
            try:
                self._path.write_text(self._body(), encoding="utf-8")
            except OSError:
                pass

    def release(self) -> None:
        """Release the lock if this session owns it."""
        if not self._held:
            return
        self._held = False
        existing = self._read()
        if existing and existing.get("session_id") != self.session_id:
            return
        try:
            self._path.unlink()
        except OSError:
            pass


__all__ = ["SchedulerLock", "LOCK_FILENAME"]
