"""Process-level crash contracts for the durable flow boundaries."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from mote.contracts.conversation import AIMessage
from mote.contracts.conversation.fields import TOOL_CALLS
from mote.kernel.output import text_output_contract
from mote.runtime.durable import InferenceJournal, reconcile_think_journal
from mote.runtime.durable.backend import JsonlBackend
from mote.runtime.ledger import COMPLETED, KIND_TOOL, STARTED, RunJournal
from mote.runtime.output.engine import OutputEngine
from mote.runtime.session.log import SessionLog
from mote.runtime.session.replay import replay
from mote.runtime.session.workspace import SessionWorkspace

_CRASH_EXIT = 91
_PACKAGE_PARENT = Path(__file__).resolve().parents[4]

_CHILD = r"""
import asyncio
import os
import sys
from pathlib import Path

from mote.runtime.events import EventFabric, SubscriptionManifest
from mote.contracts.conversation import AIMessage
from mote.contracts.model.inference import InferenceResult
from mote.contracts.model.turn import FinalCandidateAction
from mote.runtime.session.workspace import SessionWorkspace
from mote.runtime.ledger import KIND_TOOL, RunJournal
from mote.runtime.durable import InferenceJournal
from mote.runtime.durable.backend import JsonlBackend
from mote.kernel.output import text_output_contract
from mote.runtime.output.engine import OutputEngine
from mote.runtime.session.log import SessionLog
from mote.runtime.session.committer import SessionFactCommitter
from mote.runtime.session.events import MessageEvent, SessionMetaEvent

scenario, root = sys.argv[1], Path(sys.argv[2])

if scenario.startswith("effect-"):
    async def effect_crash():
        log = SessionLog("session", base_dir=str(root))
        await log.append(SessionMetaEvent(session_id="session", role_class="mote.agent.role.v1", toolset_manifest=()))
        await log.append(
            MessageEvent(
                message=AIMessage(
                    content="",
                    tool_calls=[{"id": "call-1", "name": "ExternalTool", "args": {}}],
                )
            )
        )
        await log.writer.drain()
        ledger = RunJournal("session", SessionWorkspace(root=str(root)))
        ledger.record_started("call-1", KIND_TOOL, "external", name="ExternalTool", tool_call_id="call-1")
        if scenario != "effect-before-body":
            (root / "effect-happened").write_text("1", encoding="utf-8")
        if scenario == "effect-after-result":
            ledger.record_completed("call-1", payload="done")
        os._exit(91)

    asyncio.run(effect_crash())

if scenario == "think-after-result":
    journal = InferenceJournal(JsonlBackend(RunJournal("session", SessionWorkspace(root=str(root)))))
    step_id = journal.begin_think()
    journal.complete_think(step_id, InferenceResult(content="carried-result"))
    os._exit(91)

async def output_crash():
    log = SessionLog("session", base_dir=str(root))
    fabric = EventFabric(
        journal=log.event_journal,
        streams=(log.stream_id,),
        subscriptions=SubscriptionManifest(()),
        on_commit=log.accept_commit,
    )
    await fabric.start()
    committer = SessionFactCommitter(log, fabric)
    await committer.commit_event(SessionMetaEvent(session_id="session", role_class="mote.agent.role.v1", toolset_manifest=()))
    engine = OutputEngine(
        text_output_contract(),
        run_id="run-1",
        session_fact_sink=committer,
    )
    await engine.evaluate(FinalCandidateAction(raw="answer", representation="native_text"))
    await log.writer.drain()
    if scenario == "output-after-accept":
        os._exit(91)
    await engine.commit()
    await log.writer.drain()
    os._exit(91)

asyncio.run(output_crash())
"""


def _crash(scenario: str, root: Path) -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(_PACKAGE_PARENT), env.get("PYTHONPATH", ""))))
    completed = subprocess.run(
        [sys.executable, "-c", _CHILD, scenario, str(root)],
        cwd=_PACKAGE_PARENT,
        env=env,
        check=False,
        timeout=20,
    )
    assert completed.returncode == _CRASH_EXIT


def _run_journal(root: Path) -> RunJournal:
    return RunJournal("session", SessionWorkspace(root=str(root)))


def test_crash_after_call_record_before_effect_requires_reconcile(tmp_path):
    _crash("effect-before-body", tmp_path)

    record = _run_journal(tmp_path).replay("call-1")
    assert record is not None and record.status == STARTED
    assert record.effect == "external"
    assert not (tmp_path / "effect-happened").exists()
    messages = replay(SessionLog("session", base_dir=str(tmp_path))).transcript_messages
    assert len(messages) == 1
    assert messages[0].metadata[TOOL_CALLS][0]["id"] == "call-1"


def test_crash_after_effect_before_result_never_blindly_replays(tmp_path):
    _crash("effect-after-body", tmp_path)

    ledger = _run_journal(tmp_path)
    record = ledger.replay("call-1")
    assert (tmp_path / "effect-happened").read_text(encoding="utf-8") == "1"
    assert record is not None and record.status == STARTED
    assert [item.tool_call_id for item in ledger.unresolved()] == ["call-1"]


def test_crash_after_result_before_journal_reap_reuses_terminal(tmp_path):
    _crash("effect-after-result", tmp_path)

    record = _run_journal(tmp_path).replay("call-1")
    assert record is not None and record.status == COMPLETED
    assert record.payload == "done"


def test_crash_after_think_result_before_reap_reinstates_without_model(tmp_path):
    _crash("think-after-result", tmp_path)

    journal = InferenceJournal(JsonlBackend(_run_journal(tmp_path)))
    candidate = journal.reinstate_candidate([])
    assert candidate is not None
    step_id, result = candidate
    assert result.content == "carried-result"

    reconcile_think_journal(journal.journal, [AIMessage(content="carried-result")])
    assert journal.journal.replay(step_id) is None


@pytest.mark.asyncio
async def test_crash_after_output_accept_before_commit_resumes_commit(tmp_path):
    _crash("output-after-accept", tmp_path)

    log = SessionLog("session", base_dir=str(tmp_path))
    state = replay(log).output_state
    assert state is not None and state["status"] == "accepted"

    engine = OutputEngine(text_output_contract(), restored_state=state, run_id="ignored")
    assert engine.has_restored_terminal_output is True
    committed = await engine.commit()
    assert committed.value == "answer"
    assert committed.run_id == "run-1"


@pytest.mark.asyncio
async def test_crash_after_commit_before_publish_is_idempotently_committed(tmp_path):
    _crash("output-after-commit", tmp_path)

    state = replay(SessionLog("session", base_dir=str(tmp_path))).output_state
    assert state is not None and state["status"] == "committed"
    engine = OutputEngine(text_output_contract(), restored_state=state)
    first = engine.committed_output
    second = await engine.commit()
    assert first is not None and second == first
    assert second.value == "answer"
