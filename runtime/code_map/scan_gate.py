"""Process-local deduplication gate for CodeMap repository scans."""
from __future__ import annotations

from contextlib import asynccontextmanager
from threading import Lock
from typing import AsyncIterator


class CodeMapScanGate:
    def __init__(self) -> None:
        self._lock = Lock()
        self._active: set[str] = set()

    def try_acquire(self, key: str) -> bool:
        with self._lock:
            if key in self._active:
                return False
            self._active.add(key)
            return True

    def release(self, key: str) -> None:
        with self._lock:
            self._active.discard(key)

    @asynccontextmanager
    async def claim(self, key: str) -> AsyncIterator[bool]:
        acquired = self.try_acquire(key)
        try:
            yield acquired
        finally:
            if acquired:
                self.release(key)


__all__ = ["CodeMapScanGate"]
