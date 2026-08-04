#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``mote.runtime.session.fork`` + ``Role.fork_session`` (Phase 4).

Carries both session projections across a fork: the full logical transcript is
seeded as message facts and the active model context is restored by an optional
compaction fact. Also covers parent lineage, copied cwd/model anchors,
independence from the parent, missing-source/existing-target rejection, Role
sibling construction, and lineage in session listing.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from mote.contracts.conversation import UserMessage
from mote.contracts.events.file.facts import (
    FileTransactionAbortedEvent,
    FileTransactionCommittedEvent,
    FileTransactionInDoubtEvent,
    FileTransactionPreparedEvent,
    HunkDetectedEvent,
)
from mote.contracts.file import ReviewStatus
from mote.contracts.file.errors import SnapshotDurabilityError
from mote.contracts.file.mutations import ReplaceMutation
from mote.contracts.tool import CommandProtocol, ToolsetIdentity
from mote.product.agents.factory import CodingAgentFactory
from mote.product.config.model_checkpoint import approved_model_checkpoint_policy
from mote.runtime.fileops.edit_plans import LiteralEditPlanRequest
from mote.runtime.fileops.identity import path_token
from mote.runtime.fileops.mutation.artifacts import FileMutationArtifactRepository
from mote.runtime.fileops.resource_limits import ARTIFACT_WRITE_TTL_SECONDS
from mote.runtime.fileops.transactions import ScopedMutationArtifacts
from mote.runtime.session.codec import decode_session_event, iter_file_operations_events
from mote.runtime.session.events import ContextCompactedFact, MessageEvent, SessionMetaEvent
from mote.runtime.session.fork import fork
from mote.runtime.session.history import diff_snapshot, file_history, restore
from mote.runtime.session.hunk_ops import HunkOps
from mote.runtime.session.listing import list_sessions
from mote.runtime.session.log import SessionLog
from mote.runtime.session.replay import replay
from mote.ztest.fileops_factory import FileOperations


def _seed(
    tmp_path,
    sid,
    *,
    working_dir="/w",
    project_root="/p",
    model="m",
    messages=(),
    toolset_manifest=None,
):
    log = SessionLog(sid, base_dir=str(tmp_path))
    _append(
        log,
        SessionMetaEvent(
            session_id=sid,
            working_dir=working_dir,
            project_root=project_root,
            model=model,
            toolset_manifest=toolset_manifest,
        ),
    )
    for content in messages:
        _append(log, MessageEvent(message=UserMessage(content=content)))
    return log


def _append(log: SessionLog, event) -> None:
    asyncio.run(log.append(event))


def _fork(*args, **kwargs) -> str:
    return asyncio.run(fork(*args, **kwargs))


def _record_file_transaction(log, root, target, source="Write"):
    operations = FileOperations(
        session_id=log.session_id,
        journal_path=log.path,
        get_project_root=lambda: str(root),
        flush_pending=log.writer.flush_inline,
        lock_root=root / "locks",
        event_sink=log.commit_offline,
        event_source=lambda: iter_file_operations_events(log.iter_events()),
    )
    snapshot, raw = operations.capture(str(target))
    scope = operations.artifacts.write_scope(
        owner="test-fork-transaction",
        maximum_bytes=len(raw),
        ttl_seconds=ARTIFACT_WRITE_TTL_SECONDS,
    )
    with scope:
        mutation = operations.mutation_factory.replacement(
            snapshot,
            raw,
            scope=scope,
        )
        operations.mutations.commit(
            operations.mutation_factory.mutation_set(
                source=source,
                mutations=(mutation,),
            ),
            ScopedMutationArtifacts(scope),
        )
    return operations


def _review_ops(operations):
    return HunkOps(
        operations.review,
        operations.artifacts,
        capture_snapshot=operations.capture,
        mutation_factory=operations.mutation_factory,
        commit_mutation_set=operations.mutations.commit,
        resource_lease=operations.mutations.lease,
    )


def _operations(log, root):
    return FileOperations(
        session_id=log.session_id,
        journal_path=log.path,
        get_project_root=lambda: str(root),
        flush_pending=log.writer.flush_inline,
        lock_root=root / "locks",
        event_sink=log.commit_offline,
        event_source=lambda: iter_file_operations_events(log.iter_events()),
    )


def _artifact_bytes(operations, digest):
    return operations.artifacts.read_bytes(operations.artifacts.resolve_live(digest))


def _replacement_refs(event):
    assert len(event.mutation_set.mutations) == 1
    mutation = event.mutation_set.mutations[0]
    assert isinstance(mutation, ReplaceMutation)
    return mutation.before.artifact, mutation.before.metadata, mutation.after


def _events(log):
    return [decode_session_event(envelope) for envelope in log.iter_events()]


def test_fork_seeds_history_and_lineage(tmp_path):
    identity = ToolsetIdentity("workspace", "2", CommandProtocol.NATIVE)
    _seed(
        tmp_path,
        "parent",
        working_dir="/repo",
        model="gpt-4",
        messages=["a", "b"],
        toolset_manifest=(identity,),
    )
    child_id = _fork("parent", new_session_id="child", base_dir=str(tmp_path))
    assert child_id == "child"

    child = SessionLog("child", base_dir=str(tmp_path))
    events = _events(child)
    meta = events[0]
    assert isinstance(meta, SessionMetaEvent)
    assert meta.session_id == "child"
    assert meta.parent_session_id == "parent"
    assert meta.working_dir == "/repo"
    assert meta.model == "gpt-4"
    assert meta.toolset_manifest == (identity,)
    # Inherited history replays to the parent's final state.
    result = replay(child)
    assert [m.content for m in result.transcript_messages] == ["a", "b"]
    assert [m.content for m in result.model_context_messages] == ["a", "b"]


def test_fork_is_independent_of_parent(tmp_path):
    parent = _seed(tmp_path, "parent", messages=["a"])
    _fork("parent", new_session_id="child", base_dir=str(tmp_path))
    # Mutating the child must not touch the parent's log.
    child = SessionLog("child", base_dir=str(tmp_path))
    _append(child, MessageEvent(message=UserMessage(content="child-only")))
    parent_msgs = [event for event in _events(parent) if isinstance(event, MessageEvent)]
    assert [event.message.content for event in parent_msgs] == ["a"]


def test_fork_generates_id_when_omitted(tmp_path):
    _seed(tmp_path, "parent", messages=["a"])
    child_id = _fork("parent", base_dir=str(tmp_path))
    assert child_id and child_id != "parent"
    assert SessionLog(child_id, base_dir=str(tmp_path)).exists()


def test_fork_missing_source_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        _fork("nope", base_dir=str(tmp_path))


def test_fork_existing_target_raises(tmp_path):
    _seed(tmp_path, "parent", messages=["a"])
    _seed(tmp_path, "taken", messages=["x"])
    with pytest.raises(FileExistsError):
        _fork("parent", new_session_id="taken", base_dir=str(tmp_path))


def test_fork_preserves_transcript_and_model_context_projections(tmp_path):
    log = _seed(tmp_path, "parent", messages=["pre"])
    source = replay(log).transcript_messages[0]
    _append(
        log,
        ContextCompactedFact(
            model_context_messages=[UserMessage(content="kept")],
            source_message_ids=[str(source.id)],
            summary="s",
        ),
    )
    _append(log, MessageEvent(message=UserMessage(content="after")))
    _fork("parent", new_session_id="child", base_dir=str(tmp_path))
    result = replay(SessionLog("child", base_dir=str(tmp_path)))
    assert [m.content for m in result.transcript_messages] == ["pre", "after"]
    assert [m.content for m in result.model_context_messages] == ["kept", "after"]


def test_fork_listing_surfaces_parent(tmp_path):
    _seed(tmp_path, "parent", messages=["a"])
    _fork("parent", new_session_id="child", base_dir=str(tmp_path))
    infos = {i.session_id: i for i in list_sessions(base_dir=str(tmp_path))}
    assert infos["child"].parent_session_id == "parent"
    assert infos["parent"].parent_session_id is None


def test_fork_inherits_file_history_and_blobs(tmp_path):
    log = _seed(tmp_path, "parent", messages=["a"])
    target = tmp_path / "f.txt"
    target.write_text("v1")
    _record_file_transaction(log, tmp_path, target)
    target.write_text("v2")  # current on-disk now differs from the before-image

    _fork("parent", new_session_id="child", base_dir=str(tmp_path))
    child = SessionLog("child", base_dir=str(tmp_path))

    # The child sees the inherited snapshot event...
    hist = file_history(child)
    assert str(target) in hist
    assert hist[str(target)][0].pre_hash is not None
    # ...and can diff/restore using its own (copied) blob store.
    diff = diff_snapshot(child, str(target))
    assert "v1" in diff and "v2" in diff
    assert restore(child, str(target)) is True
    assert target.read_text() == "v1"


def test_fork_file_history_independent_of_parent(tmp_path):
    log = _seed(tmp_path, "parent", messages=["a"])
    target = tmp_path / "f.txt"
    target.write_text("v1")
    parent = _record_file_transaction(log, tmp_path, target)

    _fork("parent", new_session_id="child", base_dir=str(tmp_path))
    child_log = SessionLog("child", base_dir=str(tmp_path))
    child = _operations(child_log, tmp_path)
    child_prepared = next(event for event in _events(child_log) if isinstance(event, FileTransactionPreparedEvent))

    assert child.artifacts.root == parent.artifacts.root
    assert child.artifacts.catalog.root != parent.artifacts.catalog.root
    for ref in _replacement_refs(child_prepared):
        assert child.artifacts.read_bytes(child.artifacts.resolve_live(ref.digest)) == (
            parent.artifacts.read_bytes(parent.artifacts.resolve_live(ref.digest))
        )


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_fork_fails_closed_before_transaction_event_on_bad_source_artifact(
    tmp_path,
    damage,
):
    log = _seed(tmp_path, "parent")
    target = tmp_path / "f.txt"
    target.write_text("v1", encoding="utf-8")
    parent = _record_file_transaction(log, tmp_path, target)
    prepared = next(event for event in _events(log) if isinstance(event, FileTransactionPreparedEvent))
    ref = _replacement_refs(prepared)[0]
    payload = parent.artifacts.root / ref.digest[:2] / ref.digest
    if damage == "missing":
        payload.unlink()
    else:
        payload.write_bytes(b"x" * ref.size)

    with pytest.raises(SnapshotDurabilityError):
        _fork("parent", new_session_id="child", base_dir=str(tmp_path))

    child = SessionLog("child", base_dir=str(tmp_path))
    assert not any(
        isinstance(
            event,
            (FileTransactionPreparedEvent, FileTransactionCommittedEvent),
        )
        for event in _events(child)
    )


def test_fork_fails_closed_before_hunk_event_on_missing_source_artifact(tmp_path):
    log = _seed(tmp_path, "parent")
    target = tmp_path / "f.txt"
    target.write_text("base\n", encoding="utf-8")
    parent = _operations(log, tmp_path)
    snapshot, _ = parent.capture(str(target), encoding="utf-8")
    old = "old\n"
    new = "new\n"
    with parent.artifacts.write_scope(
        owner="test-fork-hunk",
        maximum_bytes=len(old.encode()) + len(new.encode()),
        ttl_seconds=ARTIFACT_WRITE_TTL_SECONDS,
    ) as scope:
        records = parent.review.record_delta(
            path=str(target),
            old=old,
            new=new,
            source="external",
            turn_index=1,
            id_base="external-change",
            expected_digest=snapshot.artifact.digest,
            scope=scope,
        )
    assert len(records) == 1
    missing_digest = records[0].post_hash
    (parent.artifacts.root / missing_digest[:2] / missing_digest).unlink()

    with pytest.raises(SnapshotDurabilityError):
        _fork("parent", new_session_id="child", base_dir=str(tmp_path))

    child = SessionLog("child", base_dir=str(tmp_path))
    assert not any(isinstance(event, HunkDetectedEvent) for event in _events(child))


def test_fork_uses_one_exact_unique_artifact_budget_per_transaction_event(
    tmp_path,
    monkeypatch,
):
    log = _seed(tmp_path, "parent")
    target = tmp_path / "f.txt"
    target.write_text("v1", encoding="utf-8")
    _record_file_transaction(log, tmp_path, target)
    prepared = next(event for event in _events(log) if isinstance(event, FileTransactionPreparedEvent))
    refs = _replacement_refs(prepared)
    assert refs[0] == refs[2]
    expected_budget = sum(ref.size for ref in {ref.digest: ref for ref in refs}.values())

    observed_budgets = []
    original_write_scope = FileMutationArtifactRepository.write_scope

    def record_write_scope(repository, *, owner, maximum_bytes, ttl_seconds):
        if repository.catalog.root == tmp_path / "child" / "artifact-lifecycle":
            observed_budgets.append(maximum_bytes)
        return original_write_scope(
            repository,
            owner=owner,
            maximum_bytes=maximum_bytes,
            ttl_seconds=ttl_seconds,
        )

    monkeypatch.setattr(FileMutationArtifactRepository, "write_scope", record_write_scope)
    _fork("parent", new_session_id="child", base_dir=str(tmp_path))

    assert observed_budgets == [expected_budget, 0]


def test_fork_remaps_committed_transaction_id(tmp_path):
    log = _seed(tmp_path, "parent")
    target = tmp_path / "f.txt"
    target.write_text("v1")
    _record_file_transaction(log, tmp_path, target)

    parent_events = _events(log)
    parent_prepared = next(e for e in parent_events if isinstance(e, FileTransactionPreparedEvent))
    _fork("parent", new_session_id="child", base_dir=str(tmp_path))
    child_events = _events(SessionLog("child", base_dir=str(tmp_path)))
    child_prepared = next(e for e in child_events if isinstance(e, FileTransactionPreparedEvent))
    child_committed = next(e for e in child_events if isinstance(e, FileTransactionCommittedEvent))

    expected_id = f"fork:parent:{parent_prepared.mutation_set.transaction_id}"
    assert child_prepared.mutation_set.transaction_id == expected_id
    assert child_prepared.mutation_set.session_id == "child"
    assert child_committed.transaction_id == expected_id


@pytest.mark.parametrize("final_status", [ReviewStatus.ACCEPTED, ReviewStatus.REJECTED])
def test_fork_inherits_final_review_status_and_artifacts(tmp_path, final_status):
    parent_log = _seed(tmp_path, "parent")
    target = tmp_path / "reviewed.txt"
    target.write_text("before\n", encoding="utf-8")
    parent = _operations(parent_log, tmp_path)
    snapshot, _ = parent.capture(str(target), encoding="utf-8")
    parent.observe(snapshot)
    plan = parent.plan_file_edit(
        LiteralEditPlanRequest(
            path=path_token(target),
            old="before",
            new="after",
        )
    )
    parent.commit_edit_plan(plan.plan_id, review_turn_index=4)
    parent_record = parent.review.records()[0]
    if final_status == ReviewStatus.ACCEPTED:
        parent.review.transition(parent_record, status=ReviewStatus.ACCEPTED)
    else:
        assert _review_ops(parent).reject(parent_record.hunk_id).ok

    _fork("parent", new_session_id="child", base_dir=str(tmp_path))
    child_log = SessionLog("child", base_dir=str(tmp_path))
    child = _operations(child_log, tmp_path)
    child_record = child.review.records()[0]

    assert child_record.hunk_id == f"fork:parent:{parent_record.hunk_id}"
    assert child_record.session_id == "child"
    assert child_record.status == final_status
    assert child.artifacts.root == parent.artifacts.root
    assert child.artifacts.catalog.root != parent.artifacts.catalog.root
    assert _artifact_bytes(child, child_record.pre_hash) == _artifact_bytes(
        parent,
        parent_record.pre_hash,
    )
    assert _artifact_bytes(child, child_record.post_hash)


def test_fork_review_transition_is_independent_of_parent(tmp_path):
    parent_log = _seed(tmp_path, "parent")
    target = tmp_path / "pending.txt"
    target.write_text("before\n", encoding="utf-8")
    parent = _operations(parent_log, tmp_path)
    snapshot, _ = parent.capture(str(target), encoding="utf-8")
    parent.observe(snapshot)
    plan = parent.plan_file_edit(
        LiteralEditPlanRequest(
            path=path_token(target),
            old="before",
            new="after",
        )
    )
    parent.commit_edit_plan(plan.plan_id, review_turn_index=5)
    parent_record = parent.review.records()[0]

    _fork("parent", new_session_id="child", base_dir=str(tmp_path))
    child_log = SessionLog("child", base_dir=str(tmp_path))
    child = _operations(child_log, tmp_path)
    child_record = child.review.records()[0]
    child.review.transition(child_record, status=ReviewStatus.ACCEPTED)

    assert child.review.status(child_record.hunk_id).status == ReviewStatus.ACCEPTED
    assert parent.review.status(parent_record.hunk_id).status == ReviewStatus.PENDING


@pytest.mark.parametrize("terminal_event", [FileTransactionAbortedEvent, FileTransactionInDoubtEvent])
def test_fork_drops_non_committed_transaction(tmp_path, terminal_event):
    log = _seed(tmp_path, "parent")
    target = tmp_path / "f.txt"
    target.write_text("v1")
    _record_file_transaction(log, tmp_path, target)
    prepared = next(event for event in _events(log) if isinstance(event, FileTransactionPreparedEvent))
    transaction_id = f"unfinished-{terminal_event.__name__}"
    _append(
        log,
        FileTransactionPreparedEvent(
            mutation_set=replace(
                prepared.mutation_set,
                transaction_id=transaction_id,
                session_id="parent",
                source="test",
            ),
        ),
    )
    if terminal_event is FileTransactionInDoubtEvent:
        _append(
            log,
            terminal_event(transaction_id=transaction_id, detail="external state"),
        )
    else:
        _append(
            log,
            terminal_event(transaction_id=transaction_id, detail="cancelled"),
        )

    _fork("parent", new_session_id="child", base_dir=str(tmp_path))
    child_events = _events(SessionLog("child", base_dir=str(tmp_path)))
    inherited_ids = {
        (event.mutation_set.transaction_id if isinstance(event, FileTransactionPreparedEvent) else event.transaction_id)
        for event in child_events
        if isinstance(event, (FileTransactionPreparedEvent, FileTransactionCommittedEvent))
    }
    assert f"fork:parent:{transaction_id}" not in inherited_ids


@pytest.mark.asyncio
async def test_role_fork_session_inherits_history_and_lineage(tmp_path, monkeypatch):
    from mote.kernel.output import text_output_contract
    from mote.product.paths import default_runtime_paths
    from mote.runtime.agent import AgentDependencies, AgentWiring, Role
    from mote.runtime.models.clients.context import Context
    from mote.ztest.model_fakes import FakeModelGateway, offline_config

    class OfflineLLM:
        def __init__(self, model):
            self.model = model
            self.cost_manager = None
            self.rate_limit_tracker = None
            self.context_reducer = None

        async def aask(self, _msg, system_msgs=None, stream=True, **_kwargs):
            return "offline-summary"

    monkeypatch.setattr(
        "mote.runtime.session.log._default_base_dir",
        lambda: tmp_path / ".agent_sessions",
    )

    from mote.ztest.model_fakes import FakeApplicationComposition, bind_fake_runtime

    context = Context()
    composition = FakeApplicationComposition(FakeModelGateway(OfflineLLM("test")))
    paths = default_runtime_paths(
        user_config_root=tmp_path / "config",
        workspace_root=tmp_path,
    )

    parent = Role(
        name="P",
        wiring=AgentWiring.for_context(
            context,
            application_composition=composition,
            dependencies=CodingAgentFactory(
                model_checkpoint_policy=approved_model_checkpoint_policy(),
                paths=paths,
                routing_strategy_builders_factory=lambda: {"squilla": object},
            ).dependencies(deps=None, output_contract=text_output_contract()),
        ),
    )
    bind_fake_runtime(parent, OfflineLLM("test"))
    await parent._components.start_event_fabric()
    await parent._emit_session_start()
    await parent.context_manager.add(UserMessage(content="one"))
    await parent.context_manager.add(UserMessage(content="two"))

    child = await parent.fork_session()
    bind_fake_runtime(child, OfflineLLM("test"))
    assert child.state.parent_session_id == parent.session_id
    assert child.session_id != parent.session_id
    assert child.state.recovered is True
    assert [m.content for m in child.context_manager.messages] == ["one", "two"]
    assert child.wiring is parent.wiring
    await child.cleanup()
    await parent.cleanup()
    await parent.context.aclose()
