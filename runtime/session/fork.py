"""Branch a durable session from the final state of another rollout.

The child receives an independent rollout and artifact repository. Committed
file transactions and review facts are copied event by event; every copied
event owns one exact artifact write scope whose lifecycle is completed only
after the child event is durably flushed.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from typing import Optional

from mote.contracts.content.identity import ContentIdentity
from mote.contracts.events.file.facts import (
    FileHistoryImportedEvent,
    FileTransactionAbortedEvent,
    FileTransactionCommittedEvent,
    FileTransactionInDoubtEvent,
    FileTransactionPreparedEvent,
    HunkDetectedEvent,
    HunkReviewTransitionedEvent,
)
from mote.contracts.file.errors import SnapshotDurabilityError
from mote.contracts.file.mutations import CreateMutation, DeleteMutation, ReplaceMutation
from mote.contracts.file.transactions import HunkRecord
from mote.contracts.tool import parse_toolset_manifest
from mote.runtime.artifacts import ArtifactRepositoryLayout
from mote.runtime.fileops.mutation import ArtifactRepository
from mote.runtime.fileops.resource_limits import ARTIFACT_HARD_LIMIT_BYTES, ARTIFACT_WRITE_TTL_SECONDS
from mote.runtime.persistence import DiskWriter
from mote.runtime.session.codec import decode_session_event
from mote.runtime.session.events import ContextCompactedFact, MessageEvent, SessionEvent, SessionMetaEvent
from mote.runtime.session.ids import new_session_id as mint_session_id
from mote.runtime.session.log import SessionLog
from mote.runtime.session.replay import replay
from mote.runtime.telemetry.logging import log_call


@log_call(level="DEBUG")
async def fork(
    source_session_id: str,
    *,
    new_session_id: Optional[str] = None,
    base_dir: Optional[str] = None,
    writer: DiskWriter | None = None,
) -> str:
    """Branch ``source_session_id`` into a new independent durable session."""
    writer = writer or DiskWriter()
    source = SessionLog(source_session_id, base_dir=base_dir, writer=writer)
    if not source.exists():
        raise FileNotFoundError(f"no rollout to fork for session {source_session_id!r}")

    child_id = new_session_id or mint_session_id()
    child = SessionLog(child_id, base_dir=base_dir, writer=writer)
    if child.exists():
        raise FileExistsError(f"fork target session {child_id!r} already exists")

    result = replay(source)
    meta = result.meta or {}
    toolset_manifest = meta["toolset_manifest"]
    await child.append(
        SessionMetaEvent(
            session_id=child_id,
            parent_session_id=source_session_id,
            working_dir=meta["working_dir"] if "working_dir" in meta else "",
            original_working_dir=(meta["original_working_dir"] if "original_working_dir" in meta else ""),
            project_root=meta["project_root"] if "project_root" in meta else "",
            model=meta["model"] if "model" in meta else None,
            role_class=meta["role_class"],
            toolset_manifest=parse_toolset_manifest(toolset_manifest),
        )
    )
    for message in result.transcript_messages:
        await child.append(MessageEvent(message=message))
    if result.model_context_messages != result.transcript_messages:
        latest = result.latest_compaction
        await child.append(
            ContextCompactedFact(
                model_context_messages=list(result.model_context_messages),
                source_message_ids=[str(message.id) for message in result.transcript_messages],
                summary=latest.summary if latest is not None else "",
                strategy="fork_projection",
                trigger="fork",
            )
        )
    await _inherit_file_history(source, child)
    return child_id


async def _inherit_file_history(source: SessionLog, child: SessionLog) -> None:
    """Copy committed transactions and review facts into the child."""
    layout = ArtifactRepositoryLayout(source.workspace_root)
    content_repository = layout.open(
        layout.ownership(session_id=source.session_id, project_root=source.path.parent)
    ).repository
    source_repository = ArtifactRepository(
        content_repository,
        lifecycle_root=source.path.parent / "artifact-lifecycle",
        hard_limit_bytes=ARTIFACT_HARD_LIMIT_BYTES,
    )
    child_repository = ArtifactRepository(
        content_repository,
        lifecycle_root=child.path.parent / "artifact-lifecycle",
        hard_limit_bytes=ARTIFACT_HARD_LIMIT_BYTES,
    )
    prepared: dict[str, FileTransactionPreparedEvent] = {}
    inherited_hunk_ids: dict[str, str] = {}

    for envelope in source.iter_events():
        event = decode_session_event(envelope)
        if isinstance(event, FileHistoryImportedEvent):
            await _append_with_artifacts(
                child,
                child_repository,
                source_repository,
                event,
                (event.before,) if event.before is not None else (),
                owner=(f"fork-{source.session_id}-{child.session_id}-" f"history-{event.import_id}"),
            )
            continue
        if isinstance(event, FileTransactionPreparedEvent):
            transaction_id = event.mutation_set.transaction_id
            if transaction_id in prepared:
                raise ValueError(f"source transaction {transaction_id!r} was prepared more than once")
            prepared[transaction_id] = event
            continue
        if isinstance(event, (FileTransactionAbortedEvent, FileTransactionInDoubtEvent)):
            prepared.pop(event.transaction_id, None)
            continue
        if isinstance(event, FileTransactionCommittedEvent):
            if event.transaction_id not in prepared:
                raise ValueError(f"source transaction {event.transaction_id!r} committed without prepare")
            parent_prepared = prepared.pop(event.transaction_id)
            inherited_id = f"fork:{source.session_id}:" f"{parent_prepared.mutation_set.transaction_id}"
            inherited_hunks = tuple(
                replace(
                    record,
                    hunk_id=f"fork:{source.session_id}:{record.hunk_id}",
                    session_id=child.session_id,
                )
                for record in parent_prepared.hunks
            )
            inherited_hunk_ids.update(
                {
                    original.hunk_id: inherited.hunk_id
                    for original, inherited in zip(
                        parent_prepared.hunks,
                        inherited_hunks,
                        strict=True,
                    )
                }
            )
            inherited_prepared = FileTransactionPreparedEvent(
                mutation_set=replace(
                    parent_prepared.mutation_set,
                    transaction_id=inherited_id,
                    session_id=child.session_id,
                ),
                hunks=inherited_hunks,
            )
            await _append_with_artifacts(
                child,
                child_repository,
                source_repository,
                inherited_prepared,
                (*_mutation_refs(parent_prepared), *_hunk_refs(parent_prepared.hunks)),
                owner=f"fork-{source.session_id}-{child.session_id}-prepared",
            )
            await _append_with_artifacts(
                child,
                child_repository,
                source_repository,
                FileTransactionCommittedEvent(
                    transaction_id=inherited_id,
                    versions=event.versions,
                ),
                (),
                owner=f"fork-{source.session_id}-{child.session_id}-committed",
            )
            continue
        if isinstance(event, HunkDetectedEvent):
            inherited_hunk_id = f"fork:{source.session_id}:{event.record.hunk_id}"
            inherited_hunk_ids[event.record.hunk_id] = inherited_hunk_id
            inherited_event = HunkDetectedEvent(
                replace(
                    event.record,
                    hunk_id=inherited_hunk_id,
                    session_id=child.session_id,
                )
            )
            await _append_with_artifacts(
                child,
                child_repository,
                source_repository,
                inherited_event,
                _hunk_refs((event.record,)),
                owner=f"fork-{source.session_id}-{child.session_id}-hunk-detected",
            )
            continue
        if isinstance(event, HunkReviewTransitionedEvent):
            if event.hunk_id not in inherited_hunk_ids:
                raise ValueError(f"source hunk transition {event.hunk_id!r} has no inherited hunk")
            child_transaction_id = event.child_transaction_id
            if child_transaction_id:
                child_transaction_id = f"fork:{source.session_id}:{child_transaction_id}"
            inherited_event = replace(
                event,
                hunk_id=inherited_hunk_ids[event.hunk_id],
                child_transaction_id=child_transaction_id,
            )
            await _append_with_artifacts(
                child,
                child_repository,
                source_repository,
                inherited_event,
                (event.post_hash, event.expected_digest),
                owner=f"fork-{source.session_id}-{child.session_id}-hunk-transition",
            )


def _mutation_refs(event: FileTransactionPreparedEvent) -> tuple[ContentIdentity, ...]:
    refs: list[ContentIdentity] = []
    for mutation in event.mutation_set.mutations:
        if isinstance(mutation, CreateMutation):
            refs.extend((mutation.after, mutation.metadata))
        elif isinstance(mutation, ReplaceMutation):
            refs.extend((mutation.before.artifact, mutation.before.metadata, mutation.after))
        elif isinstance(mutation, DeleteMutation):
            refs.extend((mutation.before.artifact, mutation.before.metadata))
        else:
            raise TypeError("source transaction contains an unknown mutation")
    return tuple(refs)


def _hunk_refs(records: Iterable[HunkRecord]) -> tuple[str, ...]:
    refs: list[str] = []
    for record in records:
        refs.extend((record.pre_hash, record.post_hash, record.expected_digest))
    return tuple(refs)


async def _append_with_artifacts(
    child: SessionLog,
    child_repository: ArtifactRepository,
    source_repository: ArtifactRepository,
    event: SessionEvent,
    refs: Iterable[ContentIdentity | str],
    *,
    owner: str,
) -> None:
    source_refs = _resolve_unique_refs(source_repository, refs)
    with child_repository.write_scope(
        owner=owner,
        maximum_bytes=sum(ref.size for ref in source_refs),
        ttl_seconds=ARTIFACT_WRITE_TTL_SECONDS,
    ) as scope:
        for ref in source_refs:
            copied = scope.put_bytes(source_repository.read_bytes(ref))
            if copied != ref:
                raise SnapshotDurabilityError(
                    "forked artifact does not match its source reference",
                    digest=ref.digest,
                    expected_size=ref.size,
                    actual_size=copied.size,
                    actual_digest=copied.digest,
                )
        await child.append(event)
        scope.complete(durability_root=child.path.parent)


def _resolve_unique_refs(
    repository: ArtifactRepository,
    refs: Iterable[ContentIdentity | str],
) -> tuple[ContentIdentity, ...]:
    unique: dict[str, ContentIdentity] = {}
    for ref in refs:
        digest = ref.digest if isinstance(ref, ContentIdentity) else ref
        live = repository.resolve_live(digest)
        if isinstance(ref, ContentIdentity) and live != ref:
            raise SnapshotDurabilityError(
                "source artifact reference conflicts with its live object",
                digest=ref.digest,
                expected_size=ref.size,
                actual_size=live.size,
            )
        if digest in unique and unique[digest] != live:
            raise SnapshotDurabilityError(
                "source event contains conflicting artifact references",
                digest=digest,
            )
        unique[digest] = live
    return tuple(unique[digest] for digest in sorted(unique))


__all__ = ["fork"]
