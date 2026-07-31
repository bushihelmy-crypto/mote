"""File-domain value contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from mote.contracts.file.identity import ProjectIdentity
from mote.contracts.file.transactions import TransactionStatus


@dataclass(frozen=True)
class RewindRecord:
    transaction_id: str
    session_id: str
    status: TransactionStatus
    project_identity: ProjectIdentity
    working_dir: str
    safety_commit: str
    target_commit: str
    prompt_index: int
    source_epoch: int
    external_paths: Tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class RewindResult:
    transaction_id: str
    status: TransactionStatus
    safety_commit: str
    target_commit: str
    external_paths: Tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class FileOperationsHealth:
    """Read-only readiness projection for one session's file-operation plane."""

    lock_backend: str
    journal_readable: bool
    journal_writable: bool
    artifact_readable: bool
    artifact_writable: bool
    artifact_catalog_readable: bool
    recovery_backlog: int
    in_doubt_transactions: Tuple[str, ...] = ()
    affected_paths: Tuple[str, ...] = ()
    cursor_registry_readable: bool = True
    timeline_epoch: int = 0
    active_cursor_leases: int = 0
    expired_cursor_leases: int = 0
    pinned_artifacts: int = 0
    pinned_bytes: int = 0
    nearest_cursor_expiry_ns: Optional[int] = None
    observed_snapshots: int = 0
    artifact_hard_limit_bytes: int = 0
    artifact_physical_bytes: int = 0
    artifact_reserved_bytes: int = 0
    artifact_staged_bytes: int = 0
    artifact_active_reservations: int = 0
    artifact_open_stages: int = 0
    artifact_catalog_generation: int = 0
    artifact_staging_objects: int = 0
    artifact_quarantined_objects: int = 0
    artifact_deleting_objects: int = 0
    artifact_quota_pressure: float = 0.0

    @property
    def ready(self) -> bool:
        return (
            self.journal_readable
            and self.journal_writable
            and self.artifact_readable
            and self.artifact_writable
            and self.artifact_catalog_readable
            and self.cursor_registry_readable
            and self.recovery_backlog == 0
            and not self.in_doubt_transactions
        )
