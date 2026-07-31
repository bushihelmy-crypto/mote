"""File-history queries over committed and explicitly imported typed facts."""

from __future__ import annotations

import difflib
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from mote.contracts.content.identity import ContentIdentity
from mote.contracts.events.file.facts import (
    FileHistoryImportedEvent,
    FileTransactionAbortedEvent,
    FileTransactionCommittedEvent,
    FileTransactionInDoubtEvent,
    FileTransactionPreparedEvent,
)
from mote.contracts.file.codec import mutation_to_dict
from mote.contracts.file.errors import SnapshotDurabilityError
from mote.contracts.file.mutations import CreateMutation, Mutation, MutationSet
from mote.runtime.artifacts import ArtifactRepositoryLayout
from mote.runtime.fileops import FileOperations
from mote.runtime.fileops.metadata_manifest import PreservedMetadata, encode_metadata_manifest
from mote.runtime.fileops.resource_limits import ARTIFACT_WRITE_TTL_SECONDS
from mote.runtime.fileops.transactions import ScopedMutationArtifacts
from mote.runtime.session.codec import decode_session_event, iter_file_operations_events
from mote.runtime.session.log import SessionLog


@dataclass
class SnapshotEntry:
    """One durable before-image in a file's chronological history.

    ``index`` is the position of this snapshot within the file's own history
    (0-based, chronological); ``ts`` is the event's ISO timestamp.
    """

    path: str
    operation: str  # create | update
    before: Optional[ContentIdentity]
    display_path: str
    tool: str
    ts: str
    index: int

    @property
    def pre_hash(self) -> Optional[str]:
        return self.before.digest if self.before is not None else None

    @property
    def pre_size(self) -> int:
        return self.before.size if self.before is not None else 0

    @property
    def existed(self) -> bool:
        """Whether the file existed before this mutation (``update`` vs ``create``)."""
        return self.operation != "create"


def file_history(log: SessionLog) -> Dict[str, List[SnapshotEntry]]:
    """Group every durable before-image fact by path in chronological order.

    Returns ``{path -> [SnapshotEntry, ...]}`` where each list is ordered oldest
    → newest and each entry's ``index`` is its position in that list.
    """
    history: Dict[str, List[SnapshotEntry]] = {}
    prepared: dict[str, tuple[FileTransactionPreparedEvent, str]] = {}
    for envelope in log.iter_events():
        event = decode_session_event(envelope)
        if isinstance(event, FileHistoryImportedEvent):
            bucket = history.setdefault(event.path, [])
            bucket.append(
                SnapshotEntry(
                    path=event.path,
                    operation=event.operation,
                    before=event.before,
                    display_path=event.display_path,
                    tool=event.source,
                    ts=event.recorded_at,
                    index=len(bucket),
                )
            )
            continue
        if isinstance(event, FileTransactionPreparedEvent):
            prepared[event.mutation_set.transaction_id] = (
                event,
                envelope.occurred_at.isoformat(),
            )
            continue
        if isinstance(event, (FileTransactionAbortedEvent, FileTransactionInDoubtEvent)):
            prepared.pop(event.transaction_id, None)
            continue
        if isinstance(event, FileTransactionCommittedEvent):
            pending = prepared.pop(event.transaction_id, None)
            if pending is None:
                continue
            mutation, timestamp = pending
            for item in mutation.mutation_set.mutations:
                path = item.requested_path.display
                before = None if isinstance(item, CreateMutation) else item.before.artifact
                bucket = history.setdefault(path, [])
                bucket.append(
                    SnapshotEntry(
                        path=path,
                        operation=("create" if isinstance(item, CreateMutation) else "update"),
                        before=before,
                        display_path=path,
                        tool=mutation.mutation_set.source,
                        ts=timestamp,
                        index=len(bucket),
                    )
                )
            continue
    return history


def _entry_at(log: SessionLog, path: str, index: int) -> Optional[SnapshotEntry]:
    entries = file_history(log).get(path)
    if not entries:
        return None
    try:
        return entries[index]
    except IndexError:
        return None


def diff_snapshot(log: SessionLog, path: str, *, index: int = -1) -> str:
    """Unified diff of a captured before-image against the current on-disk file.

    ``index`` selects which snapshot of ``path`` to compare (default ``-1`` =
    the most recent before-image). Returns the unified diff text, or ``""`` when
    there is no difference. Raises ``KeyError`` if the path has no history and
    ``IndexError`` if ``index`` is out of range.
    """
    entry = _entry_at(log, path, index)
    if entry is None:
        raise KeyError(f"no file-history for '{path}'")

    operations = _file_operations(log, path)
    if entry.before is None:
        before = b""  # file did not exist before this mutation (a create)
    else:
        before = operations.artifacts.read_bytes(entry.before)

    try:
        _, current = operations.capture(path)
    except OSError:
        current = b""

    before_text = before.decode("utf-8", errors="replace").splitlines(keepends=True)
    current_text = current.decode("utf-8", errors="replace").splitlines(keepends=True)

    diff = difflib.unified_diff(
        before_text,
        current_text,
        fromfile=f"{entry.display_path} (before {entry.tool or 'mutation'})",
        tofile=f"{entry.display_path} (current)",
    )
    return "".join(diff)


def restore(
    log: SessionLog,
    path: str,
    *,
    index: int = -1,
) -> bool:
    """Restore ``path`` on disk to one of its captured before-images.

    ``index`` selects which snapshot to restore (default ``-1`` = the most
    recent before-image). A ``create`` before-image (the file did not yet
    exist) is restored by removing the file. Returns ``True`` on success,
    ``False`` if the referenced blob is missing. Raises ``KeyError`` if the
    path has no history and ``IndexError`` if ``index`` is out of range.
    """
    entry = _entry_at(log, path, index)
    if entry is None:
        raise KeyError(f"no file-history for '{path}'")
    operations = _file_operations(log, path)

    if entry.before is None:
        try:
            snapshot, _ = operations.capture(path)
        except FileNotFoundError:
            return True
        scope = operations.artifacts.write_scope(
            owner="history-restore-delete",
            maximum_bytes=0,
            ttl_seconds=ARTIFACT_WRITE_TTL_SECONDS,
        )
        with scope:
            mutation = operations.mutation_factory.deletion(snapshot)
            operations.mutations.commit(
                _restore_set(log, entry, operations, mutation),
                ScopedMutationArtifacts(scope),
            )
        return True

    try:
        content = operations.artifacts.read_bytes(entry.before)
    except SnapshotDurabilityError:
        return False

    try:
        snapshot, _ = operations.capture(path)
    except FileNotFoundError:
        maximum_bytes = len(content) + len(encode_metadata_manifest(PreservedMetadata.for_create()))
        snapshot = None
    else:
        maximum_bytes = len(content)
    scope = operations.artifacts.write_scope(
        owner="history-restore-write",
        maximum_bytes=maximum_bytes,
        ttl_seconds=ARTIFACT_WRITE_TTL_SECONDS,
    )
    with scope:
        if snapshot is None:
            mutation = operations.mutation_factory.creation(
                path,
                content,
                scope=scope,
            )
        else:
            mutation = operations.mutation_factory.replacement(
                snapshot,
                content,
                scope=scope,
            )
        operations.mutations.commit(
            _restore_set(log, entry, operations, mutation),
            ScopedMutationArtifacts(scope),
        )
    return True


def _restore_set(
    log: SessionLog,
    entry: SnapshotEntry,
    operations: FileOperations,
    mutation: Mutation,
) -> MutationSet:
    identity = json.dumps(
        {
            "session_id": log.session_id,
            "path": entry.path,
            "index": entry.index,
            "timestamp": entry.ts,
            "mutation": mutation_to_dict(mutation),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return operations.mutation_factory.mutation_set(
        transaction_id=hashlib.sha256(b"mote-history-restore\0" + identity).hexdigest(),
        source="history_restore",
        mutations=(mutation,),
    )


def _file_operations(log: SessionLog, path: str) -> FileOperations:
    log.exists()
    layout = ArtifactRepositoryLayout(log.workspace_root)
    repository = layout.open(
        layout.ownership(session_id=log.session_id, project_root=Path(path).resolve().parent)
    ).repository
    return FileOperations(
        session_id=log.session_id,
        journal_path=log.path,
        get_project_root=lambda: str(Path(path).resolve().parent),
        artifact_repository=repository,
        artifact_lifecycle_root=log.path.parent / "artifact-lifecycle",
        flush_pending=log.writer.flush_inline,
        lock_root=log.runtime_root / "file-locks",
        event_sink=log.commit_offline,
        event_source=lambda: iter_file_operations_events(log.iter_events()),
    )


__all__ = ["SnapshotEntry", "file_history", "diff_snapshot", "restore"]
