from __future__ import annotations

import codecs
import json
import multiprocessing

import pytest

from mote.contracts.content.identity import ContentIdentity
from mote.contracts.events.file.facts import FileEditPlanStoredEvent
from mote.contracts.file import CreateMutation, MutationSet, ReplaceMutation, TextViewMode
from mote.contracts.file.errors import SnapshotDurabilityError, StaleSnapshotError
from mote.runtime.fileops.edit_plans import (
    MAX_EDIT_PLAN_MANIFEST_BYTES,
    MAX_EDIT_PLAN_OUTPUT_BYTES,
    AbsentEditPlanSource,
    EditPlanManifestError,
    EditPlanner,
    EditPlanOutputLimitError,
    EditPlanSourceError,
    EditPlanStore,
    ExistingEditPlanSource,
    LiteralEditPlanRequest,
    RegexEditPlanRequest,
    ReplacementLimitExceededError,
    WholeFileEditPlanRequest,
)
from mote.runtime.fileops.identity import path_token
from mote.runtime.fileops.journal import DurableFileOperationsJournal
from mote.runtime.fileops.locking import HierarchicalLockManager
from mote.runtime.fileops.mutation.artifacts import ArtifactWriteScope, ArtifactWriteScopeState
from mote.runtime.fileops.query_semantics import CandidateDiscovery, RegexProgram
from mote.runtime.fileops.resource_limits import snapshot_budget
from mote.runtime.fileops.text_sources import MaterializedText
from mote.ztest.fileops_factory import FileOperations


def _operations(tmp_path, *, session_id="session") -> FileOperations:
    return FileOperations(
        session_id=session_id,
        journal_path=tmp_path / "session" / "rollout.jsonl",
        get_project_root=lambda: str(tmp_path),
        lock_root=tmp_path / "locks",
    )


def _request(
    root,
    *,
    pattern=r"(?P<word>item)-(\d+)",
    replacement=r"\2-\g<word>",
    encoding=None,
    max_replacements=100,
) -> RegexEditPlanRequest:
    return RegexEditPlanRequest(
        root=path_token(root),
        globs=("*.txt",),
        pattern=pattern,
        replacement=replacement,
        encoding=encoding,
        max_replacements=max_replacements,
    )


def _observe_text(operations, path, *, encoding=None):
    scope = operations.artifacts.write_scope(
        owner="test-observe-text",
        maximum_bytes=snapshot_budget(path.stat().st_size),
        ttl_seconds=60,
    )
    with scope:
        materialized = operations.text_sources.materialize(
            path,
            scope=scope,
            encoding=encoding,
        )
        operations.observe(materialized.snapshot)
        scope.complete(durability_root=operations.cursor_registry.path.parent)
    return materialized.snapshot


def _put_artifact(operations, data, *, owner="test-forged-artifact"):
    scope = operations.artifacts.write_scope(
        owner=owner,
        maximum_bytes=len(data),
        ttl_seconds=60,
    )
    with scope:
        artifact = scope.put_bytes(data)
        scope.complete(durability_root=operations.journal.path.parent)
    return artifact


def _publish_plan_worker(
    journal_path,
    lock_root,
    plan_id,
    digest,
    ready,
    start,
    outcomes,
):
    journal = DurableFileOperationsJournal(
        journal_path,
        session_id="session",
        locks=HierarchicalLockManager(lock_root),
    )
    ready.put(True)
    start.wait(20)
    try:
        published = journal.publish_edit_plan(
            plan_id,
            ContentIdentity(digest=digest, size=1),
        )
    except Exception as exc:
        outcomes.put(("error", type(exc).__name__, str(exc)))
    else:
        outcomes.put(("published", published.digest))


def test_file_operations_owns_the_only_edit_planner_and_store(tmp_path):
    operations = _operations(tmp_path)

    assert isinstance(operations.edit_planner, EditPlanner)
    assert isinstance(operations.edit_plan_store, EditPlanStore)
    assert operations.edit_planner.sources is operations.text_sources
    assert operations.edit_planner.store is operations.edit_plan_store


def test_plan_uses_sealed_text_source_and_match_expand_capture_semantics(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "one.txt"
    original = b"prefix item-42 suffix"
    target.write_bytes(original)
    operations = _operations(tmp_path)
    materialize = operations.text_sources.materialize
    for_edit = RegexProgram.for_edit
    expand_replacement = RegexProgram.expand_replacement
    materialized = []
    compiled = []
    expanded = []

    def record_materialization(path, **kwargs):
        source = materialize(path, **kwargs)
        assert operations.artifacts.read_bytes(source.snapshot.artifact) == original
        materialized.append(source)
        return source

    def record_compilation(cls, pattern, **kwargs):
        compiled.append((pattern, kwargs))
        return for_edit(pattern, **kwargs)

    def record_expansion(program, match, replacement):
        expanded.append((match.group(0), replacement))
        return expand_replacement(program, match, replacement)

    monkeypatch.setattr(
        operations.text_sources,
        "materialize",
        record_materialization,
    )
    monkeypatch.setattr(RegexProgram, "for_edit", classmethod(record_compilation))
    monkeypatch.setattr(
        RegexProgram,
        "expand_replacement",
        record_expansion,
    )

    plan = operations.plan_file_edit(_request(tmp_path))

    assert len(materialized) == 1
    assert len(compiled) == 1
    assert compiled[0][0] == r"(?P<word>item)-(\d+)"
    assert expanded == [("item-42", r"\2-\g<word>")]
    assert materialized[0].mode == TextViewMode.TEXT
    assert plan.preview.total_replacements == 1
    assert plan.preview.affected_files[0].path == path_token(target)
    assert plan.preview.affected_files[0].replacement_count == 1
    assert operations.artifacts.read_bytes(plan.mutation_set.mutations[0].after) == (b"prefix 42-item suffix")
    assert target.read_bytes() == original


def test_document_extracted_text_is_never_an_editable_source(tmp_path, monkeypatch):
    target = tmp_path / "report.docx"
    target.write_bytes(b"sealed document bytes")
    operations = _operations(tmp_path)
    snapshot, _ = operations.capture(str(target))
    calls = []

    def extracted_document(path, **kwargs):
        calls.append((path, kwargs))
        return MaterializedText(
            snapshot=snapshot,
            mode=TextViewMode.DOCUMENT,
            text="item-42",
        )

    monkeypatch.setattr(
        operations.text_sources,
        "materialize",
        extracted_document,
    )
    request = RegexEditPlanRequest(
        root=path_token(tmp_path),
        globs=("*.docx",),
        pattern=r"item-(\d+)",
        replacement=r"\1-item",
        max_replacements=1,
    )

    with pytest.raises(EditPlanSourceError, match="document.*not editable"):
        operations.plan_file_edit(request)

    assert len(calls) == 1
    assert target.read_bytes() == b"sealed document bytes"


@pytest.mark.parametrize(
    ("encoding", "bom", "encode_as"),
    [
        (None, codecs.BOM_UTF16_LE, "utf-16-le"),
        ("gbk", b"", "gbk"),
        ("shift_jis", b"", "shift_jis"),
    ],
)
def test_plan_preserves_encoding_bom_and_all_unmodified_bytes(
    tmp_path,
    encoding,
    bom,
    encode_as,
):
    target = tmp_path / "localized.txt"
    prefix = "日本|".encode(encode_as)
    matched = "item-42".encode(encode_as)
    suffix = "|日本\r\n".encode(encode_as)
    original = bom + prefix + matched + suffix
    expected = bom + prefix + "42-item".encode(encode_as) + suffix
    target.write_bytes(original)
    operations = _operations(tmp_path)

    plan = operations.plan_file_edit(_request(tmp_path, encoding=encoding, max_replacements=1))

    mutation = plan.mutation_set.mutations[0]
    assert isinstance(mutation, ReplaceMutation)
    assert operations.artifacts.read_bytes(mutation.after) == expected
    assert target.read_bytes() == original

    result = operations.commit_edit_plan(plan.plan_id)

    assert result.result.transaction_id == plan.transaction_id
    assert target.read_bytes() == expected


def test_candidate_set_and_dry_run_preview_are_frozen_before_commit(
    tmp_path,
    monkeypatch,
):
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text("item-1", encoding="utf-8")
    second.write_text("item-2 item-3", encoding="utf-8")
    operations = _operations(tmp_path)

    plan = operations.plan_file_edit(_request(tmp_path, max_replacements=3))

    assert isinstance(plan.discovery, CandidateDiscovery)
    assert plan.discovery.candidates == (path_token(first), path_token(second))
    assert plan.preview.total_replacements == 3
    assert tuple((item.path, item.replacement_count) for item in plan.preview.affected_files) == (
        (path_token(first), 1),
        (path_token(second), 2),
    )
    assert first.read_text(encoding="utf-8") == "item-1"
    assert second.read_text(encoding="utf-8") == "item-2 item-3"

    late = tmp_path / "c.txt"
    late.write_text("item-4", encoding="utf-8")

    def forbidden(*args, **kwargs):
        raise AssertionError("commit reinterpreted an immutable edit plan")

    monkeypatch.setattr(RegexProgram, "for_edit", classmethod(forbidden))
    monkeypatch.setattr(RegexProgram, "expand_replacement", forbidden)
    monkeypatch.setattr(operations.text_sources, "materialize", forbidden)

    operations.commit_edit_plan(plan.plan_id)

    assert first.read_text(encoding="utf-8") == "1-item"
    assert second.read_text(encoding="utf-8") == "2-item 3-item"
    assert late.read_text(encoding="utf-8") == "item-4"


def test_max_replacements_is_one_global_plan_limit(tmp_path):
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text("item-1 item-2", encoding="utf-8")
    second.write_text("item-3", encoding="utf-8")
    operations = _operations(tmp_path)

    with pytest.raises(ReplacementLimitExceededError) as caught:
        operations.plan_file_edit(_request(tmp_path, max_replacements=2))

    assert caught.value.maximum == 2
    assert caught.value.actual == 3
    assert first.read_text(encoding="utf-8") == "item-1 item-2"
    assert second.read_text(encoding="utf-8") == "item-3"


def test_plan_is_durable_and_idempotently_reuses_its_transaction_id(tmp_path):
    target = tmp_path / "one.txt"
    target.write_text("item-42", encoding="utf-8")
    operations = _operations(tmp_path)
    request = _request(tmp_path, max_replacements=1)

    first = operations.plan_file_edit(request)
    retry = operations.plan_file_edit(request)

    assert retry.plan_id == first.plan_id
    assert retry.transaction_id == first.transaction_id
    assert retry.mutation_set == first.mutation_set

    reopened = _operations(tmp_path)
    restored = reopened.edit_plan_store.load(first.plan_id)

    assert restored == first
    assert restored.transaction_id == first.mutation_set.transaction_id


def test_plan_manifest_uses_exact_schema(tmp_path):
    target = tmp_path / "one.txt"
    target.write_text("item-42", encoding="utf-8")
    operations = _operations(tmp_path)
    plan = operations.plan_file_edit(_request(tmp_path, max_replacements=1))
    payload = json.loads(operations.artifacts.read_bytes(plan.manifest_artifact).decode("ascii"))
    payload["unexpected"] = True
    forged = _put_artifact(
        operations,
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii"),
    )

    with pytest.raises(EditPlanManifestError, match="canonical"):
        operations.edit_plan_store.load_manifest(forged)


def test_plan_manifest_is_bounded_before_json_decode(tmp_path, monkeypatch):
    operations = _operations(tmp_path)
    oversized = _put_artifact(operations, b"x" * (MAX_EDIT_PLAN_MANIFEST_BYTES + 1))
    calls = []

    def forbidden_loads(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("oversized manifest reached JSON decoding")

    monkeypatch.setattr(
        "mote.runtime.fileops.edit_plans.json.loads",
        forbidden_loads,
    )

    with pytest.raises(EditPlanManifestError, match="size limit"):
        operations.edit_plan_store.load_manifest(oversized)
    assert calls == []


def test_one_plan_produces_one_canonical_mutation_set(tmp_path):
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text("item-1", encoding="utf-8")
    second.write_text("item-2", encoding="utf-8")
    operations = _operations(tmp_path)

    plan = operations.plan_file_edit(_request(tmp_path, max_replacements=2))

    assert isinstance(plan.mutation_set, MutationSet)
    assert plan.mutation_set.transaction_id == plan.transaction_id
    assert plan.mutation_set.session_id == "session"
    assert plan.mutation_set.source == "EditPlanner"
    assert len(plan.mutation_set.mutations) == 2
    assert tuple(mutation.requested_path for mutation in plan.mutation_set.mutations) == (
        path_token(first),
        path_token(second),
    )
    assert all(isinstance(mutation, ReplaceMutation) for mutation in plan.mutation_set.mutations)


def test_literal_request_has_native_unique_and_replace_all_semantics(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "literal.txt"
    target.write_bytes(b"before same middle same after")
    operations = _operations(tmp_path)
    _observe_text(operations, target)

    def forbidden(*args, **kwargs):
        raise AssertionError("literal planning fresh-captured an observed file")

    monkeypatch.setattr(operations.text_sources, "materialize", forbidden)
    unique = LiteralEditPlanRequest(
        path=path_token(target),
        old="same",
        new="changed",
    )
    with pytest.raises(EditPlanSourceError, match="found 2 matches"):
        operations.plan_file_edit(unique)

    plan = operations.plan_file_edit(
        LiteralEditPlanRequest(
            path=path_token(target),
            old="same",
            new="changed",
            replace_all=True,
        )
    )

    assert isinstance(plan.request, LiteralEditPlanRequest)
    assert isinstance(plan.sources[0], ExistingEditPlanSource)
    assert plan.preview.total_replacements == 2
    assert operations.artifacts.read_bytes(plan.mutation_set.mutations[0].after) == (
        b"before changed middle changed after"
    )


def test_whole_file_existing_uses_observed_encoding_bom_and_newlines(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "whole.txt"
    original = codecs.BOM_UTF16_LE + "old\r\nline\r\n".encode("utf-16-le")
    expected = codecs.BOM_UTF16_LE + "new\r\ntext\r\n".encode("utf-16-le")
    target.write_bytes(original)
    operations = _operations(tmp_path)
    snapshot = _observe_text(operations, target)

    def forbidden(*args, **kwargs):
        raise AssertionError("whole-file planning fresh-captured an observed file")

    monkeypatch.setattr(operations.text_sources, "materialize", forbidden)
    plan = operations.plan_file_edit(
        WholeFileEditPlanRequest(
            path=path_token(target),
            content="new\ntext\n",
        )
    )

    source = plan.sources[0]
    mutation = plan.mutation_set.mutations[0]
    assert isinstance(source, ExistingEditPlanSource)
    assert isinstance(mutation, ReplaceMutation)
    assert source.snapshot == snapshot
    assert source.newline_profile.crlf == 2
    assert operations.artifacts.read_bytes(mutation.after) == expected
    assert operations.artifacts.read_bytes(plan.review_facts[0].before_utf8) == (b"old\nline\n")
    assert operations.artifacts.read_bytes(plan.review_facts[0].after_utf8) == (b"new\ntext\n")


def test_whole_file_create_freezes_formal_absence_and_review_facts(tmp_path):
    target = tmp_path / "created.txt"
    operations = _operations(tmp_path)

    plan = operations.plan_file_edit(
        WholeFileEditPlanRequest(
            path=path_token(target),
            content="日本\n",
            encoding="gbk",
        )
    )

    source = plan.sources[0]
    mutation = plan.mutation_set.mutations[0]
    assert isinstance(source, AbsentEditPlanSource)
    assert isinstance(mutation, CreateMutation)
    assert source.expected_version == mutation.expected_version
    assert source.metadata == mutation.metadata
    assert source.encoding.label == "gbk"
    assert not target.exists()
    assert operations.artifacts.read_bytes(mutation.after) == "日本\n".encode("gbk")
    assert operations.artifacts.read_bytes(plan.review_facts[0].before_utf8) == b""
    assert operations.artifacts.read_bytes(plan.review_facts[0].after_utf8) == ("日本\n".encode("utf-8"))

    outcome = operations.commit_edit_plan(plan.plan_id, review_turn_index=7)

    assert target.read_bytes() == "日本\n".encode("gbk")
    assert outcome.changes[0].old == ""
    assert outcome.changes[0].new == "日本\n"
    assert outcome.changes[0].post_digest == mutation.after.digest
    observed = operations.observed(str(target))
    assert observed is not None
    assert observed.encoding == source.encoding
    record = operations.journal.get(plan.transaction_id)
    assert record is not None
    assert record.hunks
    assert record.hunks[0].pre_hash == plan.review_facts[0].before_utf8.digest
    assert record.hunks[0].post_hash == plan.review_facts[0].after_utf8.digest


def test_whole_file_runtime_owns_parent_and_existence_decisions(tmp_path):
    existing = tmp_path / "existing.txt"
    existing.write_text("not observed", encoding="utf-8")
    operations = _operations(tmp_path)

    with pytest.raises(StaleSnapshotError, match="has not been read this session"):
        operations.plan_file_edit(
            WholeFileEditPlanRequest(
                path=path_token(existing),
                content="replacement",
            )
        )
    with pytest.raises(StaleSnapshotError, match="parent directory does not exist"):
        operations.plan_file_edit(
            WholeFileEditPlanRequest(
                path=path_token(tmp_path / "missing" / "new.txt"),
                content="replacement",
            )
        )


def test_whole_file_output_limit_is_measured_on_raw_b1_bytes(tmp_path):
    operations = _operations(tmp_path)
    accepted = tmp_path / "accepted.txt"
    rejected = tmp_path / "rejected.txt"

    plan = operations.plan_file_edit(
        WholeFileEditPlanRequest(
            path=path_token(accepted),
            content="a" * MAX_EDIT_PLAN_OUTPUT_BYTES,
        )
    )

    mutation = plan.mutation_set.mutations[0]
    assert mutation.after.size == MAX_EDIT_PLAN_OUTPUT_BYTES
    assert plan.manifest_artifact.size <= MAX_EDIT_PLAN_MANIFEST_BYTES

    with pytest.raises(EditPlanOutputLimitError) as caught:
        operations.plan_file_edit(
            WholeFileEditPlanRequest(
                path=path_token(rejected),
                content="a" * (MAX_EDIT_PLAN_OUTPUT_BYTES + 1),
            )
        )
    assert caught.value.maximum == MAX_EDIT_PLAN_OUTPUT_BYTES
    assert caught.value.actual == MAX_EDIT_PLAN_OUTPUT_BYTES + 1


def test_commit_edit_plan_is_idempotent_by_transaction_id(tmp_path):
    target = tmp_path / "one.txt"
    target.write_text("item-42", encoding="utf-8")
    operations = _operations(tmp_path)
    plan = operations.plan_file_edit(_request(tmp_path, max_replacements=1))

    first = operations.commit_edit_plan(plan.plan_id)
    retry = operations.commit_edit_plan(plan.plan_id)

    assert retry == first
    assert target.read_text(encoding="utf-8") == "42-item"
    assert len(operations.journal.records()) == 1


def test_plan_artifacts_share_one_scope_completed_after_journal(
    tmp_path,
    monkeypatch,
):
    operations = _operations(tmp_path)
    target = tmp_path / "created.txt"
    events = []
    scopes = []
    original_write_scope = operations.artifacts.write_scope
    original_put_bytes = ArtifactWriteScope.put_bytes
    original_complete = ArtifactWriteScope.complete
    original_publish = operations.journal.publish_edit_plan

    def track_write_scope(**kwargs):
        scope = original_write_scope(**kwargs)
        scopes.append(scope)
        return scope

    def track_put_bytes(scope, data):
        events.append("put")
        return original_put_bytes(scope, data)

    def track_publish(plan_id, manifest):
        events.append("journal")
        return original_publish(plan_id, manifest)

    def track_complete(scope, *, durability_root):
        events.append("complete")
        return original_complete(scope, durability_root=durability_root)

    monkeypatch.setattr(operations.artifacts, "write_scope", track_write_scope)
    monkeypatch.setattr(ArtifactWriteScope, "put_bytes", track_put_bytes)
    monkeypatch.setattr(operations.journal, "publish_edit_plan", track_publish)
    monkeypatch.setattr(ArtifactWriteScope, "complete", track_complete)

    plan = operations.plan_file_edit(
        WholeFileEditPlanRequest(
            path=path_token(target),
            content="created\n",
        )
    )

    assert len(scopes) == 1
    assert scopes[0].state == ArtifactWriteScopeState.RELEASED
    assert events.count("put") == 6
    assert events[-2:] == ["journal", "complete"]
    assert operations.journal.edit_plan_manifest(plan.plan_id) == (plan.manifest_artifact)


def test_plan_scope_aborts_when_journal_publication_fails(tmp_path, monkeypatch):
    operations = _operations(tmp_path)
    target = tmp_path / "created.txt"
    scopes = []
    original_write_scope = operations.artifacts.write_scope

    def track_write_scope(**kwargs):
        scope = original_write_scope(**kwargs)
        scopes.append(scope)
        return scope

    def fail_publish(plan_id, manifest):
        raise RuntimeError("journal unavailable")

    monkeypatch.setattr(operations.artifacts, "write_scope", track_write_scope)
    monkeypatch.setattr(operations.journal, "publish_edit_plan", fail_publish)

    with pytest.raises(RuntimeError, match="journal unavailable"):
        operations.plan_file_edit(
            WholeFileEditPlanRequest(
                path=path_token(target),
                content="created\n",
            )
        )

    assert len(scopes) == 1
    assert scopes[0].state == ArtifactWriteScopeState.ABORTED
    assert not target.exists()


def test_edit_plan_binding_is_atomic_across_processes(tmp_path):
    context = multiprocessing.get_context("spawn")
    journal_path = tmp_path / "session" / "rollout.jsonl"
    lock_root = tmp_path / "locks"
    plan_id = "a" * 64
    digests = ("1" * 64, "2" * 64)
    ready = context.Queue()
    start = context.Event()
    outcomes = context.Queue()
    processes = [
        context.Process(
            target=_publish_plan_worker,
            args=(
                str(journal_path),
                str(lock_root),
                plan_id,
                digest,
                ready,
                start,
                outcomes,
            ),
        )
        for digest in digests
    ]
    for process in processes:
        process.start()
    for _ in processes:
        assert ready.get(timeout=20) is True
    start.set()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0

    results = [outcomes.get(timeout=5) for _ in processes]
    assert sum(result[0] == "published" for result in results) == 1
    assert (
        sum(
            result[0] == "error" and result[1] == "JournalDurabilityError" and "conflicting manifests" in result[2]
            for result in results
        )
        == 1
    )

    journal = DurableFileOperationsJournal(
        journal_path,
        session_id="session",
        locks=HierarchicalLockManager(lock_root),
    )
    manifest = journal.edit_plan_manifest(plan_id)
    assert manifest is not None
    assert manifest.digest in digests
    assert tuple(event for event in journal.iter_events() if isinstance(event, FileEditPlanStoredEvent)) == (
        FileEditPlanStoredEvent(plan_id, manifest),
    )


def test_plan_wide_artifact_budget_fails_before_durable_reachability(
    tmp_path,
    monkeypatch,
):
    operations = _operations(tmp_path)
    target = tmp_path / "created.txt"
    monkeypatch.setattr(
        "mote.runtime.fileops.edit_plans.MAX_EDIT_PLAN_ARTIFACT_BYTES",
        3,
    )

    with pytest.raises(SnapshotDurabilityError, match="remaining total budget"):
        operations.plan_file_edit(
            WholeFileEditPlanRequest(
                path=path_token(target),
                content="abc",
            )
        )

    assert tuple(operations.journal.iter_events()) == ()
    assert not target.exists()


def test_review_fact_has_an_explicit_bounded_policy(tmp_path, monkeypatch):
    target = tmp_path / "whole.txt"
    target.write_text("before", encoding="utf-8")
    operations = _operations(tmp_path)
    _observe_text(operations, target)
    monkeypatch.setattr(
        "mote.runtime.fileops.edit_plans.MAX_EDIT_PLAN_REVIEW_FACT_BYTES",
        3,
    )

    with pytest.raises(EditPlanOutputLimitError) as caught:
        operations.plan_file_edit(
            WholeFileEditPlanRequest(
                path=path_token(target),
                content="new",
            )
        )

    assert caught.value.maximum == 3
    assert caught.value.actual == len(b"before")
    assert tuple(operations.journal.iter_events()) == ()
    assert target.read_text(encoding="utf-8") == "before"
