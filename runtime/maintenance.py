"""Engine-scoped coordination for advisory Runtime maintenance tasks."""

from __future__ import annotations

from threading import Lock


class MaintenanceCoordinator:
    """Deduplicate maintenance work inside one explicitly owned runtime scope."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._repo_scans: set[str] = set()
        self._workspace_cleanup = False

    def acquire_repo_scan(self, key: str) -> bool:
        with self._lock:
            if key in self._repo_scans:
                return False
            self._repo_scans.add(key)
            return True

    def release_repo_scan(self, key: str) -> None:
        with self._lock:
            self._repo_scans.discard(key)

    def acquire_workspace_cleanup(self) -> bool:
        with self._lock:
            if self._workspace_cleanup:
                return False
            self._workspace_cleanup = True
            return True

    def release_workspace_cleanup(self) -> None:
        with self._lock:
            self._workspace_cleanup = False


__all__ = ["MaintenanceCoordinator"]
