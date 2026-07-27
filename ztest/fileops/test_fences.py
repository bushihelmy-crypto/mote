from __future__ import annotations

import json

import pytest

from mote.contracts.fileops import FileOperationKind, LockMode, RecoveryFenceError, RecoveryInDoubtError
from mote.runtime.fileops.fences import ProjectRecoveryFenceStore, RecoveryFence
from mote.runtime.fileops.identity import project_identity


def _mutation_fence(tmp_path):
    project = project_identity(tmp_path)
    return RecoveryFence.create(
        transaction_id="transaction",
        session_id="session",
        journal_path=str((tmp_path / "session" / "rollout.jsonl").absolute()),
        operation=FileOperationKind.MUTATION,
        artifact_root=str((tmp_path / "session" / "blobs").absolute()),
        project_mode=LockMode.SHARED,
        projects=(project,),
        resource_keys=("1:name",),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("format_version", True),
        ("unexpected", "field"),
    ],
)
def test_recovery_fence_reader_rejects_noncanonical_payload(
    tmp_path,
    field,
    value,
):
    project = project_identity(tmp_path)
    store = ProjectRecoveryFenceStore(tmp_path / "locks")
    fence = _mutation_fence(tmp_path)
    store.put(project, fence)
    path = store._path(project, fence.transaction_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[field] = value
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RecoveryInDoubtError):
        store.list(project)


def test_recovery_fence_writer_enforces_operation_scope_invariants(tmp_path):
    project = project_identity(tmp_path)
    store = ProjectRecoveryFenceStore(tmp_path / "locks")
    invalid = RecoveryFence.create(
        transaction_id="rewind",
        session_id="session",
        journal_path=str((tmp_path / "session" / "rollout.jsonl").absolute()),
        operation=FileOperationKind.REWIND,
        artifact_root=str((tmp_path / "session" / "blobs").absolute()),
        project_mode=LockMode.EXCLUSIVE,
        projects=(project,),
    )

    with pytest.raises(RecoveryFenceError):
        store.put(project, invalid)
