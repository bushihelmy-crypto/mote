#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``mote.runtime.session.reconcile`` — heal dangling tool calls on resume.

A crash between an assistant ``tool_calls`` message being flushed and its results
being recorded leaves a dangling call (requested, no paired result). The next
provider request would 400. ``reconcile_tool_calls`` splices a synthetic result
after the owning assistant message, choosing content from the ledger:

- terminal record -> heal verbatim (effect not re-run)
- ``started`` no key -> ``<unknown-after-crash>`` (verify before retry)
- ``started`` with key -> safe-retry note (tool dedups its own effect)
- no record -> safe-replay note (PURE/LOCAL, nothing happened)

A call that already has its result present is untouched. Only such already-paired
calls (result durable in the rollout, ledger record now stale) are returned for
the caller to reap; a just-healed dangling call is NOT reaped — its record must
survive to re-heal it on a later resume, else a second resume would re-run the
(possibly EXTERNAL) effect.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from mote.contracts.conversation import AIMessage, ToolMessage, UserMessage
from mote.contracts.conversation.fields import TOOL_CALL_ID
from mote.runtime.session.reconcile import reconcile_tool_calls


@dataclass
class _Rec:
    """Duck-typed StepRecord slice the reconciler reads.

    ``effect`` defaults to EXTERNAL — the only class the ledger historically
    recorded — so a ``started`` record with no explicit effect is still flagged
    unknown-after-crash (the fail-closed bias the reconciler preserves).
    """

    status: str
    payload: Optional[str] = None
    effect: str = "external"


class _FakeLedger:
    """Structural LedgerView over an id->record dict."""

    def __init__(self, records: Optional[dict] = None):
        self._records = records or {}
        self.reaped: set = set()

    def replay(self, tool_call_id: str):
        return self._records.get(tool_call_id)

    def reap(self, ids) -> None:
        self.reaped |= set(ids)


def _assistant(call_id: str, name: str = "Bash", args: Optional[dict] = None) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": name, "args": args or {}}])


def _tool_id(m) -> Optional[str]:
    return (getattr(m, "metadata", None) or {}).get(TOOL_CALL_ID)


def test_completed_dangling_is_healed_from_ledger():
    ledger = _FakeLedger({"t1": _Rec(status="completed", payload="the real output")})
    msgs = [_assistant("t1")]  # dangling: no tool_result present

    out = reconcile_tool_calls(msgs, ledger)

    # A tool_result was spliced right after the assistant message, healed verbatim.
    assert len(out.messages) == 2
    healed = out.messages[1]
    assert isinstance(healed, ToolMessage)
    assert _tool_id(healed) == "t1"
    assert healed.content == "the real output"
    assert out.healed == 1 and out.flagged == 0 and out.replayable == 0
    # The healed result lives only in the in-memory history — the rollout still
    # lacks it — so the record is NOT reaped: it must survive to re-heal the same
    # dangling call on a second resume (see the two-resume regression below).
    assert out.resolved_ids == set()
    assert out.changed is True


def test_failed_dangling_heals_from_recorded_error():
    ledger = _FakeLedger({"t1": _Rec(status="failed", payload="boom trace")})
    out = reconcile_tool_calls([_assistant("t1")], ledger)
    assert out.messages[1].content == "boom trace"
    assert out.healed == 1
    # Healed but dangling (result not yet in the rollout) -> record kept, not reaped.
    assert out.resolved_ids == set()


def test_started_external_flags_unknown_after_crash():
    # A ``started`` record means the call was in flight when the crash hit; its
    # EXTERNAL outcome is unknowable, so it is always flagged unknown-after-crash
    # (never silently retried) — the model decides what to do next.
    ledger = _FakeLedger({"t1": _Rec(status="started")})
    out = reconcile_tool_calls([_assistant("t1", name="Curl")], ledger)

    warn = out.messages[1]
    assert isinstance(warn, ToolMessage)
    assert _tool_id(warn) == "t1"
    assert "<unknown-after-crash>" in warn.content
    assert "Curl" in warn.content and "t1" in warn.content
    assert out.flagged == 1 and out.healed == 0 and out.replayable == 0
    # Dangling started record -> kept so a second resume re-flags it, not reaped.
    assert out.resolved_ids == set()


def test_started_local_is_safe_replay():
    # Once ALL tools are ledgered, a dangling LOCAL call carries a ``started``
    # record — but LOCAL effects are replay-safe (before-image protected), so it
    # must be a safe replay, NOT flagged unknown-after-crash.
    ledger = _FakeLedger({"t1": _Rec(status="started", effect="local")})
    out = reconcile_tool_calls([_assistant("t1", name="Write")], ledger)

    note = out.messages[1]
    assert "<not-executed>" in note.content
    assert "<unknown-after-crash>" not in note.content
    assert out.replayable == 1 and out.flagged == 0


def test_started_pure_is_safe_replay():
    # A dangling PURE read step is likewise replay-safe.
    ledger = _FakeLedger({"t1": _Rec(status="started", effect="pure")})
    out = reconcile_tool_calls([_assistant("t1", name="Read")], ledger)
    assert "<not-executed>" in out.messages[1].content
    assert out.replayable == 1


def test_started_unknown_effect_fails_closed_to_unknown():
    # A record whose effect is neither PURE nor LOCAL (an unrecognised/garbled
    # value) is treated as EXTERNAL — never blindly replayed.
    ledger = _FakeLedger({"t1": _Rec(status="started", effect="weird")})
    out = reconcile_tool_calls([_assistant("t1", name="Mystery")], ledger)
    assert "<unknown-after-crash>" in out.messages[1].content
    assert out.flagged == 1


def test_completed_local_heals_verbatim():
    # A completed LOCAL step heals from its recorded result (no re-run of the
    # possibly-expensive local write) — status terminal wins over effect.
    ledger = _FakeLedger({"t1": _Rec(status="completed", payload="wrote 3 files", effect="local")})
    out = reconcile_tool_calls([_assistant("t1", name="Write")], ledger)
    assert out.messages[1].content == "wrote 3 files"
    assert out.healed == 1


def test_no_ledger_record_is_safe_replay():
    # PURE/LOCAL calls are never ledgered -> status returns None -> safe replay.
    ledger = _FakeLedger({})
    out = reconcile_tool_calls([_assistant("t1", name="Read")], ledger)

    note = out.messages[1]
    assert "<not-executed>" in note.content
    assert "Read" in note.content
    assert out.replayable == 1
    # No record -> nothing to reap.
    assert out.resolved_ids == set()


def test_already_paired_call_is_untouched():
    # The result is present in history -> not dangling -> no injection, but a
    # ledger record still marks the id resolved so it gets reaped.
    ledger = _FakeLedger({"t1": _Rec(status="completed", payload="x")})
    msgs = [_assistant("t1"), ToolMessage(content="already here", tool_call_id="t1")]

    out = reconcile_tool_calls(msgs, ledger)

    assert len(out.messages) == 2  # nothing spliced
    assert out.messages[1].content == "already here"
    assert out.healed == 0 and out.flagged == 0 and out.replayable == 0
    assert out.resolved_ids == {"t1"}  # stale record still reaped
    assert out.changed is False


def test_mixed_turn_external_and_pure_both_dangling():
    # One assistant turn requesting two calls: an EXTERNAL one (started, no key)
    # and a PURE one (no record). Both dangling -> both get a synthetic result,
    # spliced in order after the owning message.
    a = AIMessage(
        content="",
        tool_calls=[
            {"id": "ext", "name": "Curl", "args": {}},
            {"id": "pure", "name": "Read", "args": {}},
        ],
    )
    ledger = _FakeLedger({"ext": _Rec(status="started")})

    out = reconcile_tool_calls([a], ledger)

    assert len(out.messages) == 3
    assert _tool_id(out.messages[1]) == "ext"
    assert "<unknown-after-crash>" in out.messages[1].content
    assert _tool_id(out.messages[2]) == "pure"
    assert "<not-executed>" in out.messages[2].content
    assert out.flagged == 1 and out.replayable == 1
    # Both dangling (neither result in the rollout) -> neither reaped.
    assert out.resolved_ids == set()


def test_plain_history_without_tool_calls_is_a_noop():
    ledger = _FakeLedger({})
    msgs = [UserMessage(content="hi"), AIMessage(content="hello")]
    out = reconcile_tool_calls(msgs, ledger)
    assert [m.content for m in out.messages] == ["hi", "hello"]
    assert out.changed is False
    assert out.resolved_ids == set()


def test_synthetic_result_precedes_later_messages():
    # The synthetic result is spliced immediately after its owning assistant
    # message, keeping provider pairing order even when more turns follow.
    ledger = _FakeLedger({"t1": _Rec(status="completed", payload="R")})
    msgs = [_assistant("t1"), UserMessage(content="next turn")]

    out = reconcile_tool_calls(msgs, ledger)

    assert [type(m).__name__ for m in out.messages] == ["AIMessage", "ToolMessage", "UserMessage"]
    assert out.messages[1].content == "R"


def test_second_resume_does_not_re_run_external_effect():
    # REGRESSION (the core bug the scheme-B fix closes): a dangling EXTERNAL call
    # left ``started`` by the crash, flagged on the FIRST resume, must NOT become
    # a "safe replay" on the SECOND resume. Since the flag lives only in the
    # in-memory history (never re-recorded to the rollout), the second resume
    # replays the SAME dangling call from the rollout. If the first resume had
    # reaped the record, the ledger would be empty here and reconcile would emit
    # a safe-replay note -> the model re-runs the external effect (duplicate!).
    # Keeping the record makes the second pass flag <unknown-after-crash> again.
    ledger = _FakeLedger({"t1": _Rec(status="started")})

    # First resume: heal (flag), record kept (dangling).
    first = reconcile_tool_calls([_assistant("t1", name="Curl")], ledger)
    assert "<unknown-after-crash>" in first.messages[1].content
    assert first.resolved_ids == set()  # nothing reaped

    # Simulate the manager reaping resolved ids (a no-op here) between resumes.
    ledger.reap(first.resolved_ids)

    # Second resume: the rollout still has only the dangling call; the record
    # survived, so it is flagged again — NOT downgraded to a safe replay.
    second = reconcile_tool_calls([_assistant("t1", name="Curl")], ledger)
    assert "<unknown-after-crash>" in second.messages[1].content
    assert "<not-executed>" not in second.messages[1].content
    assert second.flagged == 1 and second.replayable == 0


# ---------------------------------------------------------------------------
# RoleSessionManager wiring — resume() calls reconcile then reaps the ledger
# ---------------------------------------------------------------------------


class _FakeExecutor:
    def __init__(self, ledger):
        self.ledger = ledger


class _FakeRole:
    def __init__(self, ledger):
        self.executor = _FakeExecutor(ledger)


def test_manager_reconciles_and_reaps_when_ledger_present():
    from mote.runtime.agent.session_manager import RoleSessionManager

    # Two calls: t1 is dangling (healed but result NOT in the rollout -> kept),
    # t2 is already paired in the rollout (its stale record IS reaped). Proves
    # the manager reconciles AND reaps only the durable-result record.
    ledger = _FakeLedger(
        {
            "t1": _Rec(status="completed", payload="healed"),
            "t2": _Rec(status="completed", payload="stale"),
        }
    )
    mgr = RoleSessionManager(_FakeRole(ledger))  # type: ignore[arg-type]

    msgs = [
        _assistant("t1"),  # dangling
        _assistant("t2"),
        ToolMessage(content="paired in rollout", tool_call_id="t2"),
    ]
    out = mgr._reconcile_dangling_calls(msgs)

    # t1 healed and spliced (kept in ledger); t2 untouched, its stale record reaped.
    assert out[1].content == "healed"
    assert ledger.reaped == {"t2"}


def test_manager_is_noop_when_ledger_disabled():
    from mote.runtime.agent.session_manager import RoleSessionManager

    mgr = RoleSessionManager(_FakeRole(None))  # type: ignore[arg-type]
    msgs = [_assistant("t1")]

    out = mgr._reconcile_dangling_calls(msgs)

    # No ledger -> history returned unchanged (still dangling; nothing spliced).
    assert out is msgs


def test_manager_no_reap_when_nothing_resolved():
    from mote.runtime.agent.session_manager import RoleSessionManager

    # PURE dangling call: no ledger record -> safe replay note, nothing to reap.
    ledger = _FakeLedger({})
    mgr = RoleSessionManager(_FakeRole(ledger))  # type: ignore[arg-type]

    out = mgr._reconcile_dangling_calls([_assistant("t1", name="Read")])

    assert len(out) == 2
    assert "<not-executed>" in out[1].content
    assert ledger.reaped == set()


# ---------------------------------------------------------------------------
# Real RunJournal integration
# ---------------------------------------------------------------------------


def test_real_ledger_heals_dangling_but_keeps_record(tmp_path):
    # Prove reconcile composes with the real RunJournal:
    # completed record on disk heals a dangling call, but the record is KEPT
    # (result not yet in the rollout) so a later resume can re-heal it.
    from mote.runtime.ledger import KIND_TOOL, RunJournal
    from mote.runtime.session.workspace import SessionWorkspace

    store = SessionWorkspace(root=str(tmp_path))
    ledger = RunJournal("sess_real", store=store)
    ledger.record_started("t1", KIND_TOOL, "external", name="Curl", tool_call_id="t1")
    ledger.record_completed("t1", payload="real network output")

    out = reconcile_tool_calls([_assistant("t1", name="Curl")], ledger)

    assert out.messages[1].content == "real network output"
    assert out.healed == 1
    # Dangling heal -> not reaped; a fresh ledger still sees the record.
    assert out.resolved_ids == set()
    ledger.reap(out.resolved_ids)  # no-op
    assert RunJournal("sess_real", store=store).replay("t1") is not None


def test_real_ledger_reaps_paired_stale_record(tmp_path):
    # The reap path with the real ledger: a call already paired in the rollout
    # has a stale record -> reconcile marks it resolved and reap() clears it.
    from mote.runtime.ledger import KIND_TOOL, RunJournal
    from mote.runtime.session.workspace import SessionWorkspace

    store = SessionWorkspace(root=str(tmp_path))
    ledger = RunJournal("sess_paired", store=store)
    ledger.record_started("t1", KIND_TOOL, "external", name="Curl", tool_call_id="t1")
    ledger.record_completed("t1", payload="real network output")

    msgs = [_assistant("t1", name="Curl"), ToolMessage(content="paired", tool_call_id="t1")]
    out = reconcile_tool_calls(msgs, ledger)

    assert out.resolved_ids == {"t1"}
    ledger.reap(out.resolved_ids)
    assert RunJournal("sess_paired", store=store).replay("t1") is None


def test_real_ledger_started_no_key_flags_unknown(tmp_path):
    from mote.runtime.ledger import KIND_TOOL, RunJournal
    from mote.runtime.session.workspace import SessionWorkspace

    store = SessionWorkspace(root=str(tmp_path))
    ledger = RunJournal("sess_started", store=store)
    ledger.record_started("t1", KIND_TOOL, "external", name="Curl", tool_call_id="t1")

    out = reconcile_tool_calls([_assistant("t1", name="Curl")], ledger)

    assert "<unknown-after-crash>" in out.messages[1].content
    assert out.flagged == 1
    # Dangling started record -> kept (not reaped) for a later resume to re-flag.
    assert out.resolved_ids == set()


def test_real_ledger_started_local_is_safe_replay(tmp_path):
    # The real ledger persists ``effect`` — a started LOCAL record survives a
    # rebuild and reconcile reads it back as replay-safe (not unknown).
    from mote.runtime.ledger import KIND_TOOL, RunJournal
    from mote.runtime.session.workspace import SessionWorkspace

    store = SessionWorkspace(root=str(tmp_path))
    ledger = RunJournal("sess_local", store=store)
    ledger.record_started("t1", KIND_TOOL, "local", name="Write", tool_call_id="t1")

    # Rebuild in a fresh instance (post-crash) then reconcile.
    rebuilt = RunJournal("sess_local", store=store)
    out = reconcile_tool_calls([_assistant("t1", name="Write")], rebuilt)

    assert "<not-executed>" in out.messages[1].content
    assert out.replayable == 1
