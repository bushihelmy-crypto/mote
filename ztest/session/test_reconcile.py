from __future__ import annotations

from dataclasses import dataclass

from mote.contracts.conversation import AIMessage, ToolMessage, UserMessage
from mote.contracts.conversation.fields import TOOL_CALL_ID
from mote.contracts.tool.effects import ToolEffect
from mote.contracts.tool.identity import ToolAttemptOrdinal, ToolInvocationId, ToolInvocationIdentity
from mote.runtime.session.reconcile import reconcile_tool_calls
from mote.runtime.session.workspace import SessionWorkspace
from mote.runtime.tools.effect_store import ToolEffectState, ToolEffectStore


@dataclass(frozen=True)
class _Record:
    state: ToolEffectState
    receipt: str | None = None
    capability: ToolEffect = ToolEffect.EXTERNAL


class _Store:
    def __init__(self, records: dict[str, _Record] | None = None) -> None:
        self._records = records or {}

    def lookup(self, invocation_id: str) -> _Record | None:
        return self._records.get(invocation_id)


def _assistant(call_id: str, name: str = "Bash") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": name, "args": {}}])


def _tool_id(message: ToolMessage) -> str | None:
    return message.metadata.get(TOOL_CALL_ID)


def _identity(invocation_id: str) -> ToolInvocationIdentity:
    return ToolInvocationIdentity(
        invocation_id=ToolInvocationId(invocation_id),
        attempt_ordinal=ToolAttemptOrdinal(1),
        definition_identity="test.tool/v1",
        catalog_generation=1,
        arguments_digest="sha256-" + "0" * 64,
        owner_id="session-1",
        run_id="run-1",
    )


def test_terminal_dangling_calls_are_healed_verbatim() -> None:
    for state in (ToolEffectState.SUCCEEDED, ToolEffectState.FAILED):
        outcome = reconcile_tool_calls([_assistant("t1")], _Store({"t1": _Record(state, "receipt")}))
        assert isinstance(outcome.messages[1], ToolMessage)
        assert outcome.messages[1].content == "receipt"
        assert _tool_id(outcome.messages[1]) == "t1"
        assert outcome.healed == 1


def test_unsettled_external_call_fails_closed() -> None:
    outcome = reconcile_tool_calls(
        [_assistant("t1", "Curl")],
        _Store({"t1": _Record(ToolEffectState.INTENT_COMMITTED)}),
    )
    assert "<unknown-after-crash>" in outcome.messages[1].content
    assert outcome.flagged == 1


def test_unsettled_replay_safe_calls_are_replayable() -> None:
    for capability in (ToolEffect.PURE, ToolEffect.LOCAL):
        outcome = reconcile_tool_calls(
            [_assistant("t1", "Read")],
            _Store({"t1": _Record(ToolEffectState.INTENT_COMMITTED, capability=capability)}),
        )
        assert "<not-executed>" in outcome.messages[1].content
        assert outcome.replayable == 1


def test_missing_record_is_replayable() -> None:
    outcome = reconcile_tool_calls([_assistant("t1", "Read")], _Store())
    assert "<not-executed>" in outcome.messages[1].content
    assert outcome.replayable == 1


def test_already_paired_call_is_untouched() -> None:
    messages = [_assistant("t1"), ToolMessage(content="paired", tool_call_id="t1")]
    outcome = reconcile_tool_calls(
        messages,
        _Store({"t1": _Record(ToolEffectState.SUCCEEDED, "receipt")}),
    )
    assert outcome.messages == messages
    assert not outcome.changed


def test_synthetic_results_preserve_turn_order() -> None:
    assistant = AIMessage(
        content="",
        tool_calls=[
            {"id": "external", "name": "Curl", "args": {}},
            {"id": "pure", "name": "Read", "args": {}},
        ],
    )
    outcome = reconcile_tool_calls(
        [assistant, UserMessage(content="later")],
        _Store({"external": _Record(ToolEffectState.INTENT_COMMITTED)}),
    )
    assert [type(message) for message in outcome.messages] == [
        AIMessage,
        ToolMessage,
        ToolMessage,
        UserMessage,
    ]
    assert outcome.flagged == 1
    assert outcome.replayable == 1


def test_second_resume_does_not_replay_external_effect() -> None:
    store = _Store({"t1": _Record(ToolEffectState.INTENT_COMMITTED)})
    first = reconcile_tool_calls([_assistant("t1", "Curl")], store)
    second = reconcile_tool_calls([_assistant("t1", "Curl")], store)
    assert "<unknown-after-crash>" in first.messages[1].content
    assert "<unknown-after-crash>" in second.messages[1].content


def test_real_effect_store_heals_and_retains_canonical_fact(tmp_path) -> None:
    workspace = SessionWorkspace(tmp_path)
    store = ToolEffectStore("session-1", workspace)
    store.commit_intent(_identity("t1"), "Curl", ToolEffect.EXTERNAL)
    store.settle("t1", succeeded=True, receipt="network receipt")

    outcome = reconcile_tool_calls([_assistant("t1", "Curl")], store)

    assert outcome.messages[1].content == "network receipt"
    assert ToolEffectStore("session-1", workspace).lookup("t1") is not None


def test_real_effect_store_unsettled_external_is_unknown(tmp_path) -> None:
    workspace = SessionWorkspace(tmp_path)
    store = ToolEffectStore("session-1", workspace)
    store.commit_intent(_identity("t1"), "Curl", ToolEffect.EXTERNAL)
    outcome = reconcile_tool_calls([_assistant("t1", "Curl")], store)
    assert "<unknown-after-crash>" in outcome.messages[1].content
