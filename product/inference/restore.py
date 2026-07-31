"""Exclusive, verified restore into a new isolated SQLite authority."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from mote.product.inference.backends.sqlite import SQLiteAttemptReceiptStore


@dataclass(frozen=True, slots=True)
class RestoreApproval:
    approval_id: str
    approved_digest: str

    def __post_init__(self) -> None:
        if not self.approval_id:
            raise ValueError("restore approval id is required")
        if not self.approved_digest.startswith("sha256:"):
            raise ValueError("restore approval digest is invalid")


@dataclass(frozen=True, slots=True)
class RestoreResult:
    authority_path: Path
    backup_digest: str
    approval_id: str


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
        digest = await store.verify_backup(source)
        if digest != approval.approved_digest:
            raise PermissionError("restore approval does not match verified backup")
        try:
            await store.restore_from(source)
            verified = await store.verify_backup(target)
            if verified != digest:
                raise RuntimeError("restored authority digest does not match backup")
            await self._audit(
                "restore_apply",
                "committed",
                {
                    "approval_id": approval.approval_id,
                    "backup_digest": digest,
                    "authority_name": authority_name,
                },
            )
        except BaseException:
            target.unlink(missing_ok=True)
            for sidecar in target_directory.glob(f".{authority_name}.restore.*"):
                if sidecar.is_file():
                    sidecar.unlink()
            raise
        return RestoreResult(target, digest, approval.approval_id)


__all__ = [
    "IsolatedSQLiteRestoreService",
    "RestoreApproval",
    "RestoreResult",
]
