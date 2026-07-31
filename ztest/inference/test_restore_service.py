import asyncio

import pytest

from mote.product.inference.backends.sqlite import SQLiteAttemptReceiptStore
from mote.product.inference.restore import IsolatedSQLiteRestoreService, RestoreApproval


def test_restore_requires_stopped_daemon_empty_directory_and_digest_approval(tmp_path):
    async def scenario():
        audit = []

        async def record(operation, outcome, details):
            audit.append((operation, outcome, details))

        authority = tmp_path / "source" / "gateway.sqlite3"
        authority.parent.mkdir()
        source_store = SQLiteAttemptReceiptStore(authority)
        await source_store.initialize()
        backup = tmp_path / "backup.sqlite3"
        await source_store.backup_to(backup)
        digest = await source_store.verify_backup(backup)

        target = tmp_path / "restore"
        target.mkdir()
        running = IsolatedSQLiteRestoreService(daemon_is_stopped=lambda: False, audit=record)
        with pytest.raises(RuntimeError, match="stopped daemon"):
            await running.apply(
                backup,
                target,
                authority_name="gateway.sqlite3",
                approval=RestoreApproval("approval", digest),
            )

        service = IsolatedSQLiteRestoreService(daemon_is_stopped=lambda: True, audit=record)
        with pytest.raises(PermissionError, match="does not match"):
            await service.apply(
                backup,
                target,
                authority_name="gateway.sqlite3",
                approval=RestoreApproval("approval", "sha256:" + "0" * 64),
            )
        assert list(target.iterdir()) == []

        async def broken_audit(operation, outcome, details):
            raise RuntimeError("audit unavailable")

        fail_closed = IsolatedSQLiteRestoreService(daemon_is_stopped=lambda: True, audit=broken_audit)
        with pytest.raises(RuntimeError, match="audit unavailable"):
            await fail_closed.apply(
                backup,
                target,
                authority_name="gateway.sqlite3",
                approval=RestoreApproval("approval", digest),
            )
        assert list(target.iterdir()) == []

        result = await service.apply(
            backup,
            target,
            authority_name="gateway.sqlite3",
            approval=RestoreApproval("approval", digest),
        )
        assert result.backup_digest == digest
        assert result.authority_path.is_file()
        assert await source_store.verify_backup(result.authority_path) == digest
        assert audit == [
            (
                "restore_apply",
                "committed",
                {
                    "approval_id": "approval",
                    "backup_digest": digest,
                    "authority_name": "gateway.sqlite3",
                },
            )
        ]

        with pytest.raises(ValueError, match="must be empty"):
            await service.apply(
                backup,
                target,
                authority_name="second.sqlite3",
                approval=RestoreApproval("approval", digest),
            )

    asyncio.run(scenario())


def test_restore_service_requires_audit_authority():
    with pytest.raises(ValueError, match="audit authority"):
        IsolatedSQLiteRestoreService(daemon_is_stopped=lambda: True, audit=None)
