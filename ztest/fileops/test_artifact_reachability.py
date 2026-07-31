from __future__ import annotations

import json

import pytest

from mote.contracts.content.identity import ContentIdentity
from mote.contracts.events.file.facts import (
    FileEditPlanStoredEvent,
    FileHistoryImportedEvent,
    FileTransactionAbortedEvent,
    FileTransactionCommittedEvent,
    FileTransactionPreparedEvent,
    HunkDetectedEvent,
)
from mote.contracts.file.codec import blob_to_dict, search_summary_to_dict, snapshot_to_dict
from mote.contracts.file.identity import PresentVersion, TargetIdentity
from mote.contracts.file.search import SearchOutputMode, SearchSummary
from mote.contracts.file.transactions import HunkRecord
from mote.contracts.file.views import ReadCursorKind
from mote.runtime.fileops.edit_plans import WholeFileEditPlanRequest
from mote.runtime.fileops.identity import path_token
from mote.runtime.fileops.mutation.artifact_roots import (
    ArtifactReachabilityError,
    ArtifactReachabilityProjector,
    ArtifactRoot,
    ArtifactRootKind,
)
from mote.runtime.fileops.resource_limits import ARTIFACT_WRITE_TTL_SECONDS, MAX_READ_MANIFEST_BYTES
from mote.ztest.fileops_factory import FileOperations


def _operations(tmp_path) -> FileOperations:
    return FileOperations(
        session_id="reachability",
        journal_path=tmp_path / "session" / "rollout.jsonl",
        get_project_root=lambda: str(tmp_path),
        lock_root=tmp_path / "locks",
    )


def _projector(operations: FileOperations) -> ArtifactReachabilityProjector:
    return ArtifactReachabilityProjector(
        repository=operations.artifacts,
        edit_plans=operations.edit_plan_store,
        journal=operations.journal,
    )


def _put(repository, content):
    reservation = repository.reserve(
        len(content),
        "artifact-reachability-test",
        60,
    )
    stage = repository.stage(reservation, len(content))
    artifact = repository.put(stage, (content,))
    repository.release(reservation)
    return artifact


def _search_manifest(repository, *, rows_artifact, skipped_artifact):
    return _put(
        repository,
        json.dumps(
            {
                "format_version": 2,
                "rows_artifact": blob_to_dict(rows_artifact),
                "row_count": 1,
                "skipped_artifact": blob_to_dict(skipped_artifact),
                "summary": search_summary_to_dict(
                    SearchSummary(
                        discovered_files=1,
                        scanned_files=1,
                        matched_files=1,
                        total_occurrences=1,
                        skipped_files=0,
                    )
                ),
                "skipped_preview": [],
                "output_mode": SearchOutputMode.ONLY_MATCHING.value,
                "content_search": True,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii"),
    )


def test_edit_plan_event_closes_only_its_typed_manifest_graph(tmp_path):
    operations = _operations(tmp_path)
    target = tmp_path / "created.txt"
    plan = operations.plan_file_edit(
        WholeFileEditPlanRequest(
            path=path_token(target),
            content="new content\n",
        )
    )

    reachability = _projector(operations).project_events(operations.journal.iter_events())
    digests = {artifact.digest for artifact in reachability.artifacts}

    assert reachability.roots == (
        ArtifactRoot(
            plan.manifest_artifact,
            ArtifactRootKind.EDIT_PLAN_MANIFEST,
            plan.plan_id,
        ),
    )
    assert plan.manifest_artifact.digest in digests
    assert plan.request_artifact.digest in digests
    assert plan.mutation_set.mutations[0].after.digest in digests
    assert plan.mutation_set.mutations[0].metadata.digest in digests
    assert plan.review_facts[0].before_utf8.digest in digests
    assert plan.review_facts[0].after_utf8.digest in digests


def test_edit_plan_event_identity_must_match_the_manifest(tmp_path):
    operations = _operations(tmp_path)
    plan = operations.plan_file_edit(
        WholeFileEditPlanRequest(
            path=path_token(tmp_path / "created.txt"),
            content="content",
        )
    )

    projector = _projector(operations)
    roots = projector.event_roots([FileEditPlanStoredEvent("0" * 64, plan.manifest_artifact)])

    with pytest.raises(ArtifactReachabilityError, match="identity"):
        projector.close(roots)


def test_read_manifest_closure_contains_snapshot_metadata_and_text(tmp_path):
    operations = _operations(tmp_path)
    target = tmp_path / "source.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    snapshot, _ = operations.capture(target, encoding="utf-8")
    text_artifact = _put(operations.artifacts, b"alpha\nbeta\n")
    with operations.artifacts.write_scope(
        owner="artifact-reachability-test:read-manifest",
        maximum_bytes=MAX_READ_MANIFEST_BYTES,
        ttl_seconds=ARTIFACT_WRITE_TTL_SECONDS,
    ) as scope:
        manifest = operations.read_cursors.persist(
            scope,
            ReadCursorKind.TEXT,
            {
                "snapshot": snapshot_to_dict(snapshot),
                "text_artifact": blob_to_dict(text_artifact),
                "mode": "text",
            },
        )
        scope.complete(durability_root=operations.journal.path.parent)

    closed = _projector(operations).close([_projector(operations).read_manifest_root(manifest)])

    assert {item.digest for item in closed} == {
        manifest.digest,
        snapshot.artifact.digest,
        snapshot.metadata.digest,
        text_artifact.digest,
    }


def test_read_manifest_rejects_noncanonical_fields(tmp_path):
    operations = _operations(tmp_path)
    manifest = _put(
        operations.artifacts,
        json.dumps(
            {
                "format_version": 1,
                "kind": "raw",
                "payload": {"snapshot": {}, "unexpected": True},
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii"),
    )

    with pytest.raises(ArtifactReachabilityError, match="not canonical"):
        _projector(operations).close([ArtifactRoot(manifest, ArtifactRootKind.READ_MANIFEST)])


def test_search_manifest_closure_contains_rows_and_skipped_artifacts(tmp_path):
    operations = _operations(tmp_path)
    rows = _put(operations.artifacts, b'{"matched_text":"needle"}\n')
    skipped = _put(operations.artifacts, b"")
    manifest = _search_manifest(
        operations.artifacts,
        rows_artifact=rows,
        skipped_artifact=skipped,
    )

    closed = _projector(operations).close([_projector(operations).search_result_root(manifest)])
    digests = {item.digest for item in closed}

    assert manifest.digest in digests
    assert rows.digest in digests
    assert skipped.digest in digests
    assert len(digests) == 3


def test_search_manifest_missing_child_fails_closed(tmp_path):
    operations = _operations(tmp_path)
    skipped = _put(operations.artifacts, b"")
    forged = _search_manifest(
        operations.artifacts,
        rows_artifact=ContentIdentity("f" * 64, 1),
        skipped_artifact=skipped,
    )

    with pytest.raises(ArtifactReachabilityError, match="missing or corrupt"):
        _projector(operations).close([ArtifactRoot(forged, ArtifactRootKind.SEARCH_RESULT_MANIFEST)])


def test_aborted_transaction_releases_its_prepared_artifact_roots(tmp_path):
    operations = _operations(tmp_path)
    plan = operations.plan_file_edit(
        WholeFileEditPlanRequest(
            path=path_token(tmp_path / "created.txt"),
            content="content",
        )
    )
    transaction_id = plan.mutation_set.transaction_id

    roots = _projector(operations).event_roots(
        [
            FileTransactionPreparedEvent(plan.mutation_set),
            FileTransactionAbortedEvent(transaction_id, "not published"),
        ]
    )

    assert roots == ()


def test_imported_history_before_image_is_a_permanent_typed_root(tmp_path):
    operations = _operations(tmp_path)
    before = _put(operations.artifacts, b"imported before")
    event = FileHistoryImportedEvent(
        import_id="a" * 64,
        source_ordinal=2,
        recorded_at="2026-01-01T00:00:00",
        path="file.txt",
        display_path="file.txt",
        operation="update",
        before=before,
        source="Edit",
        source_schema_version=1,
    )

    operations.journal.append(event)
    reachability = _projector(operations).scan()

    assert reachability.roots == (ArtifactRoot(before, ArtifactRootKind.LEAF, event.import_id),)
    assert reachability.artifacts == (before,)


def test_committed_transaction_versions_are_validated(tmp_path):
    operations = _operations(tmp_path)
    plan = operations.plan_file_edit(
        WholeFileEditPlanRequest(
            path=path_token(tmp_path / "created.txt"),
            content="content",
        )
    )

    with pytest.raises(ArtifactReachabilityError, match="versions"):
        _projector(operations).event_roots(
            [
                FileTransactionPreparedEvent(plan.mutation_set),
                FileTransactionCommittedEvent(
                    plan.mutation_set.transaction_id,
                    (),
                ),
            ]
        )


def test_committed_version_bytes_must_match_the_prepared_artifact(tmp_path):
    operations = _operations(tmp_path)
    plan = operations.plan_file_edit(
        WholeFileEditPlanRequest(
            path=path_token(tmp_path / "created.txt"),
            content="content",
        )
    )
    mutation = plan.mutation_set.mutations[0]
    forged = PresentVersion(
        name_identity=mutation.expected_version.name_identity,
        target_identity=TargetIdentity("synthetic-target", "test"),
        size=mutation.after.size,
        mtime_ns=0,
        digest="e" * 64,
        metadata_digest=mutation.metadata.digest,
    )

    with pytest.raises(ArtifactReachabilityError, match="content"):
        _projector(operations).event_roots(
            [
                FileTransactionPreparedEvent(plan.mutation_set),
                FileTransactionCommittedEvent(
                    plan.mutation_set.transaction_id,
                    (forged,),
                ),
            ]
        )


def test_prepared_transaction_keeps_uncommitted_hunk_artifacts_reachable(tmp_path):
    operations = _operations(tmp_path)
    plan = operations.plan_file_edit(
        WholeFileEditPlanRequest(
            path=path_token(tmp_path / "created.txt"),
            content="content",
        )
    )
    before = _put(operations.artifacts, b"before")
    after = _put(operations.artifacts, b"after")
    mutation_after = plan.mutation_set.mutations[0].after
    record = HunkRecord(
        hunk_id="pending-hunk",
        path="created.txt",
        session_id="reachability",
        tool_call_id="",
        turn_index=1,
        source="agent",
        old_range=(1, 1),
        new_range=(1, 1),
        pre_hash=before.digest,
        post_hash=after.digest,
        expected_digest=mutation_after.digest,
    )

    roots = _projector(operations).event_roots([FileTransactionPreparedEvent(plan.mutation_set, (record,))])

    assert {root.artifact.digest for root in roots}.issuperset({before.digest, after.digest, mutation_after.digest})


def test_terminal_transaction_without_prepare_fails_closed(tmp_path):
    operations = _operations(tmp_path)

    with pytest.raises(ArtifactReachabilityError, match="no unique preparation"):
        _projector(operations).event_roots([FileTransactionAbortedEvent("missing", "")])


def test_hunk_digest_is_resolved_as_a_typed_leaf_not_json_scanned(tmp_path):
    operations = _operations(tmp_path)
    missing = "a" * 64
    record = HunkRecord(
        hunk_id="hunk",
        path="file.txt",
        session_id="reachability",
        tool_call_id="",
        turn_index=1,
        source="agent",
        old_range=(1, 1),
        new_range=(1, 1),
        pre_hash=missing,
        post_hash=missing,
        expected_digest=missing,
    )

    with pytest.raises(ArtifactReachabilityError, match="missing or corrupt"):
        _projector(operations).event_roots([HunkDetectedEvent(record)])


def test_unknown_event_object_fails_closed(tmp_path):
    operations = _operations(tmp_path)

    with pytest.raises(ArtifactReachabilityError, match="unknown"):
        _projector(operations).event_roots([object()])
