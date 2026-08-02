"""Thread-safe canonical owner for one inference execution epoch pair."""

import threading

from mote.contracts.inference.epochs import ExecutionEpochSnapshot


class ExecutionEpochAuthority:
    def __init__(self, *, backup_epoch: int = 1, admission_epoch: int = 1) -> None:
        self._lock = threading.Lock()
        self._snapshot = ExecutionEpochSnapshot(backup_epoch, admission_epoch)

    def snapshot(self) -> ExecutionEpochSnapshot:
        with self._lock:
            return self._snapshot

    def pair(self) -> tuple[int, int]:
        return self.snapshot().pair()

    def replace(self, snapshot: ExecutionEpochSnapshot) -> ExecutionEpochSnapshot:
        with self._lock:
            if (
                snapshot.backup_epoch < self._snapshot.backup_epoch
                or snapshot.admission_epoch < self._snapshot.admission_epoch
            ):
                raise RuntimeError("execution epoch cannot move backwards")
            self._snapshot = snapshot
            return snapshot

    def advance_backup(self) -> ExecutionEpochSnapshot:
        with self._lock:
            self._snapshot = ExecutionEpochSnapshot(
                self._snapshot.backup_epoch + 1,
                self._snapshot.admission_epoch,
            )
            return self._snapshot

    def advance_admission(self) -> ExecutionEpochSnapshot:
        with self._lock:
            self._snapshot = ExecutionEpochSnapshot(
                self._snapshot.backup_epoch,
                self._snapshot.admission_epoch + 1,
            )
            return self._snapshot


__all__ = ["ExecutionEpochAuthority"]
