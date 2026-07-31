"""Exclusive, verified restore into a new isolated SQLite authority."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from mote.contracts.events.governance import RestoreCopyMetadata
from mote.product.inference.backends.sqlite import (
    INFERENCE_GATEWAY_CUTOVER_UNIT,
    INFERENCE_GATEWAY_LOGICAL_STORE,
    INFERENCE_GATEWAY_STORAGE_FORMAT_VERSION,
    INFERENCE_GATEWAY_STORE_GENERATION,
    SQLiteAttemptReceiptStore,
)


@dataclass(frozen=True, slots=True)
class RestoreApproval:
    approval_id: str
    approved_digest: str
    approved_logical_store: str = INFERENCE_GATEWAY_LOGICAL_STORE
    approved_cutover_unit_id: str = INFERENCE_GATEWAY_CUTOVER_UNIT
    approved_source_generation: int = INFERENCE_GATEWAY_STORE_GENERATION
    approved_storage_format_version: int = INFERENCE_GATEWAY_STORAGE_FORMAT_VERSION

    def __post_init__(self) -> None:
        if not self.approval_id:
            raise ValueError("restore approval id is required")
        if not self.approved_digest.startswith("sha256:"):
            raise ValueError("restore approval digest is invalid")
        if not self.approved_logical_store or not self.approved_cutover_unit_id:
            raise ValueError("restore approval store identity is required")
        if self.approved_source_generation < 1 or self.approved_storage_format_version < 1:
            raise ValueError("restore approval generations are invalid")


@dataclass(frozen=True, slots=True)
class RestoreResult:
    authority_path: Path
    backup_digest: str
    approval_id: str
    metadata: RestoreCopyMetadata


class IsolatedSQLiteRestoreService:
    """Restore service whose caller must exclusively own a stopped daemon."""

    def __init__(
        self,
        *,
        daemon_is_stopped: Callable[[], bool],
        audit: Callable[[str, str, dict[str, str]], Awaitable[None]],
    ) -> None:
        if audit is None:
            raise ValueError("restore audit authority is required")
        self._daemon_is_stopped = daemon_is_stopped
        self._audit = audit

    async def apply(
        self,
        source: Path,
        target_directory: Path,
        *,
        authority_name: str,
        approval: RestoreApproval,
    ) -> RestoreResult:
        if not source.is_absolute() or not target_directory.is_absolute():
            raise ValueError("restore paths must be absolute")
        if not authority_name or Path(authority_name).name != authority_name:
            raise ValueError("restore authority name must be one file name")
        if not self._daemon_is_stopped():
            raise RuntimeError("restore requires an exclusively stopped daemon")
        if not target_directory.is_dir():
            raise ValueError("restore target must be an existing isolated directory")
        if any(target_directory.iterdir()):
            raise ValueError("restore target directory must be empty")

        target = target_directory / authority_name
        store = SQLiteAttemptReceiptStore(target)
        metadata = await store.describe_backup(source)
        approved_identity = (
            approval.approved_digest,
            approval.approved_logical_store,
            approval.approved_cutover_unit_id,
            approval.approved_source_generation,
            approval.approved_storage_format_version,
        )
        actual_identity = (
            metadata.authority_digest,
            metadata.logical_store,
            metadata.cutover_unit_id,
            metadata.source_generation,
            metadata.storage_format_version,
        )
        if actual_identity != approved_identity:
            raise PermissionError("restore approval does not match verified backup metadata")
        try:
            restored_metadata = await store.restore_from(source)
            if restored_metadata != metadata:
                raise RuntimeError("restore metadata changed during admission")
            verified = await store.verify_backup(target)
            if verified != metadata.authority_digest:
                raise RuntimeError("restored authority digest does not match backup")
            await self._audit(
                "restore_apply",
                "committed",
                {
                    "approval_id": approval.approval_id,
                    "backup_digest": metadata.authority_digest,
                    "authority_name": authority_name,
                    "logical_store": metadata.logical_store,
                    "cutover_unit_id": metadata.cutover_unit_id,
                    "source_generation": str(metadata.source_generation),
                    "storage_format_version": str(metadata.storage_format_version),
                    "high_water_mark": metadata.high_water_mark,
                },
            )
        except BaseException:
            target.unlink(missing_ok=True)
            for sidecar in target_directory.glob(f".{authority_name}.restore.*"):
                if sidecar.is_file():
                    sidecar.unlink()
            raise
        return RestoreResult(target, metadata.authority_digest, approval.approval_id, metadata)


__all__ = [
    "IsolatedSQLiteRestoreService",
    "RestoreApproval",
    "RestoreResult",
]
