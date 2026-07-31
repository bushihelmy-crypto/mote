#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``mote.runtime.durable.inference_journal`` — the durable think façade.

``InferenceJournal`` memoizes each think round's post-dedup :class:`InferenceResult` in
the shared :class:`RunJournal` so a resume can *reinstate* a completed think
(skip the model) instead of re-paying it — closing the G1 re-pay window. The two
module functions are the single matching authority shared by the flow
(:func:`reinstate` decision) and the resume guard (:func:`reconcile_think_journal`
reap decision), so they can never drift.
"""
from __future__ import annotations

from mote.contracts.conversation import AIMessage, UserMessage
from mote.contracts.conversation.fields import TOOL_CALLS
from mote.contracts.model.inference import InferenceResult
from mote.runtime.durable import (
    InferenceJournal,
    assistant_message_present,
    begin_timer,
    complete_timer,
    reconcile_think_journal,
    resume_timer,
)
from mote.runtime.durable.backend import JsonlBackend
from mote.runtime.events import JournalEvent, bind_telemetry
from mote.runtime.ledger import COMPLETED, KIND_THINK, KIND_TIMER, STARTED, RunJournal
from mote.runtime.session.workspace import SessionWorkspace
from mote.ztest.telemetry import InlineTelemetry


def _journal(tmp_path, session_id="sess") -> RunJournal:
    return RunJournal(session_id, store=SessionWorkspace(root=str(tmp_path)))


def _runner(tmp_path, session_id="sess") -> InferenceJournal:
    return InferenceJournal(JsonlBackend(_journal(tmp_path, session_id)))


# ----------------------------------------------------------------------
# assistant_message_present — the single matching authority
# ----------------------------------------------------------------------


def test_native_result_matched_by_tool_call_id():
    result = InferenceResult(content="", tool_calls=[{"id": "c1", "command_name": "Read", "args": {}}])
    msgs = [AIMessage(content="", tool_calls=[{"id": "c1", "name": "Read", "args": {}}])]
    assert assistant_message_present(msgs, result) is True


def test_native_result_absent_when_id_missing():
    result = InferenceResult(content="", tool_calls=[{"id": "c1", "command_name": "Read", "args": {}}])
    msgs = [AIMessage(content="", tool_calls=[{"id": "other", "name": "Read", "args": {}}])]
    assert assistant_message_present(msgs, result) is False


def test_native_result_requires_all_ids_present():
    # A turn with two calls is only "present" when BOTH ids are durable.
    result = InferenceResult(
        content="",
        tool_calls=[
            {"id": "c1", "command_name": "Read", "args": {}},
            {"id": "c2", "command_name": "Bash", "args": {}},
        ],
    )
    partial = [AIMessage(content="", tool_calls=[{"id": "c1", "name": "Read", "args": {}}])]
    assert assistant_message_present(partial, result) is False


def test_xml_result_matched_by_exact_content():
    result = InferenceResult(content="<command>End</command>", tool_calls=None)
    msgs = [AIMessage(content="<command>End</command>")]
    assert assistant_message_present(msgs, result) is True


def test_xml_result_absent_when_content_differs():
    result = InferenceResult(content="the thought", tool_calls=None)
    msgs = [AIMessage(content="a different thought")]
    assert assistant_message_present(msgs, result) is False


def test_empty_callless_result_is_degenerately_present():
    # Nothing meaningful to reinstate → treated as already present.
    assert assistant_message_present([], InferenceResult(content="", tool_calls=None)) is True


def test_user_message_never_matches():
    result = InferenceResult(content="hello", tool_calls=None)
    assert assistant_message_present([UserMessage(content="hello")], result) is False


# ----------------------------------------------------------------------
# InferenceJournal — begin / complete / reap / reinstate
# ----------------------------------------------------------------------


def test_begin_think_records_started_and_self_anchors_seq(tmp_path):
    runner = _runner(tmp_path)
    step_id = runner.begin_think()
    assert step_id == "think:1"
    rec = runner.journal.replay(step_id)
    assert rec is not None and rec.status == STARTED and rec.kind == KIND_THINK and rec.seq == 1


def test_started_think_persists_model_call_identity_for_resume(tmp_path):
    runner = _runner(tmp_path)
    step_id = runner.begin_think("model-call-1")

    resumed = runner.resume_candidate()
    assert resumed is not None
    assert resumed[0] == step_id
    assert resumed[1].model_call_id == "model-call-1"


def test_reconcile_preserves_resumable_started_think(tmp_path):
    runner = _runner(tmp_path)
    step_id = runner.begin_think("model-call-1")

    reconcile_think_journal(runner.journal, [])

    resumed = runner.resume_candidate()
    assert resumed is not None
    assert resumed[0] == step_id
    assert resumed[1].model_call_id == "model-call-1"


def test_begin_think_seq_increments_across_unreaped_records(tmp_path):
    runner = _runner(tmp_path)
    first = runner.begin_think()
    runner.complete_think(first, InferenceResult(content="one"))
    # Not reaped → next seq is 1 + max existing think seq.
    second = runner.begin_think()
    assert second == "think:2"


def test_complete_think_records_completed_payload(tmp_path):
    runner = _runner(tmp_path)
    step_id = runner.begin_think()
    result = InferenceResult(content="the answer", tool_calls=None)
    runner.complete_think(step_id, result)
    rec = runner.journal.replay(step_id)
    assert rec is not None and rec.status == COMPLETED
    import json

    payload = json.loads(rec.payload or "{}")
    assert InferenceResult.model_validate(payload["result"]).content == "the answer"


def test_reap_think_drops_the_record(tmp_path):
    runner = _runner(tmp_path)
    step_id = runner.begin_think()
    runner.complete_think(step_id, InferenceResult(content="x"))
    runner.reap_think(step_id)
    assert runner.journal.replay(step_id) is None


def test_fail_think_records_failed_then_reaps_in_process(tmp_path):
    # A6: a think that ultimately failed records a ``failed`` terminal AND reaps
    # it in the same process (boundedness — no dangling ``started`` record leaks
    # for a long-lived agent that never resumes).
    runner = _runner(tmp_path)
    step_id = runner.begin_think()
    runner.fail_think(step_id)
    assert runner.journal.replay(step_id) is None


def test_fail_think_frees_seq_for_a_fresh_rethink(tmp_path):
    # After a failed think is reaped, the next begin_think re-anchors seq from the
    # folded journal — the failed round left nothing behind, so the retry think is
    # a clean new record (not stalled behind a dangling failed one).
    runner = _runner(tmp_path)
    first = runner.begin_think()
    runner.fail_think(first)
    second = runner.begin_think()
    # The reaped failure left no think record, so seq recomputes to 1 again.
    assert second == "think:1"
    rec = runner.journal.replay(second)
    assert rec is not None and rec.status == STARTED


def test_reinstate_candidate_returns_completed_unmatched(tmp_path):
    runner = _runner(tmp_path)
    step_id = runner.begin_think()
    result = InferenceResult(content="unrecorded thought", tool_calls=None)
    runner.complete_think(step_id, result)
    # History has no matching assistant message → this is the reinstate target.
    found = runner.reinstate_candidate([])
    assert found is not None
    got_id, got_result = found
    assert got_id == step_id and got_result.content == "unrecorded thought"


def test_reinstate_candidate_none_when_started(tmp_path):
    runner = _runner(tmp_path)
    runner.begin_think()  # only started, never completed
    assert runner.reinstate_candidate([]) is None


def test_reinstate_candidate_none_when_assistant_present(tmp_path):
    runner = _runner(tmp_path)
    step_id = runner.begin_think()
    result = InferenceResult(content="done", tool_calls=None)
    runner.complete_think(step_id, result)
    # The assistant message already reached history → nothing to reinstate.
    assert runner.reinstate_candidate([AIMessage(content="done")]) is None


def test_reinstate_candidate_survives_journal_rebuild(tmp_path):
    # Crash simulation: record completed, then rebuild the runner in a fresh
    # process — the folded journal still surfaces the reinstate candidate.
    runner = _runner(tmp_path)
    step_id = runner.begin_think()
    runner.complete_think(step_id, InferenceResult(content="carried", tool_calls=None))
    rebuilt = _runner(tmp_path)
    found = rebuilt.reinstate_candidate([])
    assert found is not None and found[0] == step_id and found[1].content == "carried"


# ----------------------------------------------------------------------
# reconcile_think_journal — resume guard
# ----------------------------------------------------------------------


def test_reconcile_reaps_started_think(tmp_path):
    journal = _journal(tmp_path)
    journal.record_started("think:1", KIND_THINK, "pure", seq=1)
    reconcile_think_journal(journal, [])
    assert journal.replay("think:1") is None


def test_reconcile_reaps_completed_already_in_history(tmp_path):
    # The core double-record guard: a completed think whose assistant message
    # is already durable MUST be reaped (reinstating would double-record it).
    journal = _journal(tmp_path)
    journal.record_started("think:1", KIND_THINK, "pure", seq=1)
    journal.record_completed("think:1", payload=InferenceResult(content="done").model_dump_json())
    reconcile_think_journal(journal, [AIMessage(content="done")])
    assert journal.replay("think:1") is None


def test_reconcile_leaves_the_single_unmatched_completed(tmp_path):
    journal = _journal(tmp_path)
    journal.record_started("think:1", KIND_THINK, "pure", seq=1)
    journal.record_completed("think:1", payload=InferenceResult(content="unrecorded").model_dump_json())
    reconcile_think_journal(journal, [])
    rec = journal.replay("think:1")
    assert rec is not None and rec.status == COMPLETED


def test_reconcile_reaps_unparseable_payload(tmp_path):
    journal = _journal(tmp_path)
    journal.record_started("think:1", KIND_THINK, "pure", seq=1)
    journal.record_completed("think:1", payload="not valid json {")
    reconcile_think_journal(journal, [])
    assert journal.replay("think:1") is None


def test_reconcile_mixed_reaps_matched_keeps_unmatched(tmp_path):
    journal = _journal(tmp_path)
    # think:1 completed + present in history (reap); think:2 completed + absent (keep).
    journal.record_started("think:1", KIND_THINK, "pure", seq=1)
    journal.record_completed("think:1", payload=InferenceResult(content="in-history").model_dump_json())
    journal.record_started("think:2", KIND_THINK, "pure", seq=2)
    journal.record_completed("think:2", payload=InferenceResult(content="lost-turn").model_dump_json())
    reconcile_think_journal(journal, [AIMessage(content="in-history")])
    assert journal.replay("think:1") is None
    assert journal.replay("think:2") is not None


def test_reconcile_noop_on_empty_journal(tmp_path):
    journal = _journal(tmp_path)
    reconcile_think_journal(journal, [])  # must not raise
    assert list(journal.records()) == []


def test_reconcile_ignores_non_think_records(tmp_path):
    # A dangling EXTERNAL tool step is the effect-ledger reconcile's business,
    # not the think journal's — it must be left untouched here.
    journal = _journal(tmp_path)
    journal.record_started("tool-call-1", "tool", "external", tool_call_id="tool-call-1")
    reconcile_think_journal(journal, [])
    assert journal.replay("tool-call-1") is not None


# ----------------------------------------------------------------------
# Durable timer (G4) — begin / resume / complete
# ----------------------------------------------------------------------


def test_begin_timer_records_started_with_deadline(tmp_path):
    journal = _journal(tmp_path)
    step_id, deadline = begin_timer(journal, 30.0)
    assert step_id == "timer:1"
    rec = journal.replay(step_id)
    assert rec is not None
    assert rec.kind == KIND_TIMER
    assert rec.status == STARTED
    assert rec.effect == "pure"  # dangling timer reconciles as replay-safe
    # The wall-clock deadline is stamped in the started record's payload.
    assert abs(float(rec.payload or "") - deadline) < 1e-6


def test_begin_timer_seq_self_anchors_across_rebuild(tmp_path):
    # A fresh journal instance (mimicking a resumed process) assigns the next
    # timer seq from the folded log, never colliding with a recorded one.
    j1 = _journal(tmp_path)
    begin_timer(j1, 10.0)
    j2 = _journal(tmp_path)  # rebuilt: folds the on-disk log
    step_id, _ = begin_timer(j2, 10.0)
    assert step_id == "timer:2"


def test_resume_timer_returns_inflight_deadline(tmp_path):
    journal = _journal(tmp_path)
    step_id, deadline = begin_timer(journal, 30.0)
    resumed = resume_timer(journal)
    assert resumed is not None
    assert resumed[0] == step_id
    assert abs(resumed[1] - deadline) < 1e-6


def test_resume_timer_none_when_no_timer(tmp_path):
    journal = _journal(tmp_path)
    assert resume_timer(journal) is None


def test_resume_timer_skips_completed(tmp_path):
    # A completed timer's countdown is over — nothing to resume.
    journal = _journal(tmp_path)
    step_id, _ = begin_timer(journal, 30.0)
    complete_timer(journal, step_id)
    assert resume_timer(journal) is None


def test_resume_timer_skips_unparseable_payload(tmp_path):
    # A started timer with a missing/garbage deadline cannot be resumed from.
    journal = _journal(tmp_path)
    journal.record_started("timer:1", KIND_TIMER, "pure", seq=1, payload="not-a-float")
    assert resume_timer(journal) is None


def test_complete_timer_marks_terminal(tmp_path):
    journal = _journal(tmp_path)
    step_id, _ = begin_timer(journal, 30.0)
    complete_timer(journal, step_id)
    rec = journal.replay(step_id)
    assert rec is not None and rec.status == COMPLETED
    assert rec.kind == KIND_TIMER  # kind carried forward on the terminal


# ----------------------------------------------------------------------
# A6 — journal lifecycle events land on active telemetry
# ----------------------------------------------------------------------


class _CaptureObserver:
    """A minimal observer that records every JournalEvent it is handed."""

    def __init__(self) -> None:
        self.seen: list[JournalEvent] = []

    async def handle(self, event) -> None:  # pragma: no cover - sync path used
        if isinstance(event, JournalEvent):
            self.seen.append(event)

    def handle_sync(self, event) -> None:
        if isinstance(event, JournalEvent):
            self.seen.append(event)


def _phases(observer: _CaptureObserver) -> list[tuple[str, str, str]]:
    return [(e.kind, e.phase, e.step_id) for e in observer.seen]


def test_think_lifecycle_emits_started_completed_reaped(tmp_path):
    obs = _CaptureObserver()
    telemetry = InlineTelemetry(obs)
    runner = _runner(tmp_path)
    with bind_telemetry(telemetry):
        step_id = runner.begin_think()
        runner.complete_think(step_id, InferenceResult(content="x"))
        runner.reap_think(step_id)
    assert _phases(obs) == [
        (KIND_THINK, "started", step_id),
        (KIND_THINK, "completed", step_id),
        (KIND_THINK, "reaped", step_id),
    ]


def test_fail_think_emits_failed_then_reaped(tmp_path):
    obs = _CaptureObserver()
    telemetry = InlineTelemetry(obs)
    runner = _runner(tmp_path)
    with bind_telemetry(telemetry):
        step_id = runner.begin_think()
        runner.fail_think(step_id)
    # begin → started; fail → failed then reaped (in-process boundedness).
    assert _phases(obs) == [
        (KIND_THINK, "started", step_id),
        (KIND_THINK, "failed", step_id),
        (KIND_THINK, "reaped", step_id),
    ]


def test_timer_lifecycle_emits_started_completed(tmp_path):
    obs = _CaptureObserver()
    telemetry = InlineTelemetry(obs)
    journal = _journal(tmp_path)
    with bind_telemetry(telemetry):
        step_id, _ = begin_timer(journal, 30.0)
        complete_timer(journal, step_id)
    assert _phases(obs) == [
        (KIND_TIMER, "started", step_id),
        (KIND_TIMER, "completed", step_id),
    ]


def test_journal_event_carries_effect_and_seq(tmp_path):
    obs = _CaptureObserver()
    telemetry = InlineTelemetry(obs)
    runner = _runner(tmp_path)
    with bind_telemetry(telemetry):
        runner.begin_think()
    started = obs.seen[0]
    assert started.effect == "pure" and started.seq == 1


def test_no_telemetry_bound_is_silent_noop(tmp_path):
    runner = _runner(tmp_path)
    step_id = runner.begin_think()
    runner.complete_think(step_id, InferenceResult(content="x"))
    runner.reap_think(step_id)  # must not raise
