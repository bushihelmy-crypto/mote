from __future__ import annotations

from mote.runtime.fileops.resource_limits import ARTIFACT_WRITE_TTL_SECONDS
from mote.runtime.fileops.review import ACCEPTED, AGENT, PENDING
from mote.runtime.session.attribution import HunkAttribution
from mote.ztest.fileops_factory import FileOperations


def _review(tmp_path):
    operations = FileOperations(
        session_id="attribution",
        journal_path=tmp_path / "session" / "rollout.jsonl",
        get_project_root=lambda: str(tmp_path),
        lock_root=tmp_path / "locks",
    )
    return operations, HunkAttribution(operations.review, operations.artifacts)


def _record_delta(operations: FileOperations, **kwargs):
    scope = operations.artifacts.write_scope(
        owner="test-attribution-delta",
        maximum_bytes=(len(kwargs["old"].encode("utf-8")) + len(kwargs["new"].encode("utf-8"))),
        ttl_seconds=ARTIFACT_WRITE_TTL_SECONDS,
    )
    with scope:
        return operations.review.record_delta(scope=scope, **kwargs)


def test_attribution_rehydrates_both_sides_from_sealed_artifacts(tmp_path):
    operations, attribution = _review(tmp_path)
    record = _record_delta(
        operations,
        path=str(tmp_path / "missing-live-file.txt"),
        old="a\nold\n",
        new="a\nnew\n",
        source=AGENT,
        turn_index=3,
        tool_call_id="call",
        id_base="call:path",
        expected_digest="digest",
    )[0]

    view = attribution.all_hunks()[0]

    assert view.hunk_id == record.hunk_id
    assert view.old_text == "old\n"
    assert view.new_text == "new\n"
    assert view.turn_index == 3
    assert view.is_pending


def test_attribution_grouping_and_summary_follow_rollout_projection(tmp_path):
    operations, attribution = _review(tmp_path)
    first = _record_delta(
        operations,
        path="a.py",
        old="a\n",
        new="A\n",
        source=AGENT,
        turn_index=1,
        id_base="a",
        expected_digest="a-digest",
    )[0]
    _record_delta(
        operations,
        path="b.py",
        old="b\n",
        new="B\n",
        source=AGENT,
        turn_index=2,
        id_base="b",
        expected_digest="b-digest",
    )
    operations.review.transition(first, status=ACCEPTED)

    summary = attribution.session_summary()

    assert summary.total == 2
    assert summary.accepted == 1
    assert summary.pending == 1
    assert summary.by_status == {ACCEPTED: 1, PENDING: 1}
    assert [view.path for view in attribution.hunks_for_turn(2)] == ["b.py"]
