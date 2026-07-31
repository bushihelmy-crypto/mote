#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ResidencyStore — materialize/rehydrate evicted agents."""

import types

import pytest

from mote.contracts.conversation.messages import UserMessage
from mote.contracts.conversation.queue import MessageQueue
from mote.orchestration.agents.lifecycle.runtime import AgentRuntime
from mote.orchestration.agents.messaging.mailbox import Mailbox
from mote.orchestration.agents.residency.store import ResidencyRecord, ResidencyStore, _strip_history
from mote.runtime.agent.base import BaseRole
from mote.runtime.session import SessionLog
from mote.runtime.session.events import MessageEvent, SessionMetaEvent


class FakeRole:
    """Duck-typed Role for store round-trips.

    Mirrors the real RoleState shape closely enough to exercise history
    stripping/refilling: ``state.context.messages`` is a real list.
    """

    def __init__(self, session_id="sess-1", payload=None, messages=None):
        self._session_id = session_id
        self.state = types.SimpleNamespace(
            msg_buffer=MessageQueue(),
            context=types.SimpleNamespace(messages=list(messages or [])),
        )
        self.payload = payload or {}

    @property
    def session_id(self):
        return self._session_id

    def dump(self):
        return {
            "session_id": self._session_id,
            "payload": self.payload,
            "state": {
                "context": {"messages": [m.dump() for m in self.state.context.messages]},
            },
        }


class ValidatingFakeRole(FakeRole, BaseRole):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.validated_meta = None

    def validate_resume_identity(self, meta):
        self.validated_meta = dict(meta)


def make_role_loader():
    """A role_loader that rebuilds a FakeRole from its dump."""

    def loader(role_dump):
        state = role_dump.get("state", {})
        context = state.get("context", {})
        from mote.contracts.conversation import Message

        messages = [Message.load(m) for m in context.get("messages", [])]
        return FakeRole(
            session_id=role_dump.get("session_id", "sess-1"),
            payload=role_dump.get("payload", {}),
            messages=[m for m in messages if m is not None],
        )

    return loader


def seed_rollout(sessions_dir, session_id, contents):
    """Create a rollout.jsonl with session_meta + the given message contents."""
    log = SessionLog(session_id, base_dir=str(sessions_dir))
    log.commit_offline(
        SessionMetaEvent(
            session_id=session_id,
            role_class="FakeRole",
            toolset_manifest=(),
        )
    )
    for text in contents:
        log.commit_offline(MessageEvent(message=UserMessage(text)))
    return log


def test_record_json_round_trip():
    rec = ResidencyRecord(
        role_dump={"a": 1},
        mailbox_dump=[{"m": 2}],
        msg_buffer_dump="[]",
    )
    again = ResidencyRecord.from_json(rec.to_json())
    assert again.role_dump == {"a": 1}
    assert again.mailbox_dump == [{"m": 2}]
    assert again.msg_buffer_dump == "[]"


def test_record_from_json_tolerates_missing_keys():
    rec = ResidencyRecord.from_json("{}")
    assert rec.role_dump == {}
    assert rec.mailbox_dump == []
    assert rec.msg_buffer_dump == "[]"


def test_has_and_read_missing(tmp_path):
    store = ResidencyStore(base_dir=str(tmp_path), sessions_base_dir=str(tmp_path / "sessions"))
    assert not store.has("nope")
    assert store.read_record("nope") is None
    assert store.rehydrate("nope", role_loader=make_role_loader()) is None


@pytest.mark.asyncio
async def test_materialize_writes_record(tmp_path):
    store = ResidencyStore(base_dir=str(tmp_path), sessions_base_dir=str(tmp_path / "sessions"))
    role = FakeRole(session_id="abc", payload={"k": "v"})
    role.state.msg_buffer.push(UserMessage("buffered"))
    mailbox = Mailbox()
    mailbox.enqueue(UserMessage("mail"))
    rt = AgentRuntime(role, mailbox)

    rec = await store.materialize(rt)
    assert store.has("abc")
    # No rollout for this session, so history is not stripped.
    assert rec.role_dump["session_id"] == "abc"
    assert rec.role_dump["payload"] == {"k": "v"}
    assert rec.mailbox_dump  # non-empty
    assert "buffered" in rec.msg_buffer_dump

    read_back = store.read_record("abc")
    assert read_back.role_dump == rec.role_dump


@pytest.mark.asyncio
async def test_rehydrate_restores_buffer_and_mailbox(tmp_path):
    store = ResidencyStore(base_dir=str(tmp_path), sessions_base_dir=str(tmp_path / "sessions"))
    role = FakeRole(session_id="abc", payload={"k": "v"})
    role.state.msg_buffer.push(UserMessage("buffered"))
    mailbox = Mailbox()
    mailbox.enqueue(UserMessage("mail"))
    rt = AgentRuntime(role, mailbox)
    await store.materialize(rt)

    restored = store.rehydrate("abc", role_loader=make_role_loader())
    assert isinstance(restored, AgentRuntime)
    assert restored.session_id == "abc"
    assert restored.role.payload == {"k": "v"}
    # msg_buffer restored (excluded from RoleState serialization)
    assert not restored.msg_buffer.empty()
    buffered = restored.msg_buffer.pop()
    assert buffered.content == "buffered"
    # mailbox restored
    assert not restored.mailbox.empty()


@pytest.mark.asyncio
async def test_forget_deletes_record(tmp_path):
    store = ResidencyStore(base_dir=str(tmp_path), sessions_base_dir=str(tmp_path / "sessions"))
    role = FakeRole(session_id="abc")
    rt = AgentRuntime(role)
    await store.materialize(rt)
    assert store.has("abc")
    store.forget("abc")
    assert not store.has("abc")
    store.forget("abc")  # idempotent / safe on missing


@pytest.mark.asyncio
async def test_materialize_empty_buffer_and_mailbox(tmp_path):
    store = ResidencyStore(base_dir=str(tmp_path), sessions_base_dir=str(tmp_path / "sessions"))
    rt = AgentRuntime(FakeRole(session_id="empty"))
    rec = await store.materialize(rt)
    assert rec.msg_buffer_dump == "[]"
    assert rec.mailbox_dump == []
    restored = store.rehydrate("empty", role_loader=make_role_loader())
    assert restored.msg_buffer.empty()
    assert restored.mailbox.empty()


# ---------------------------------------------------------------------------
# History stripping + rollout-backed refill
# ---------------------------------------------------------------------------


def test_strip_history_clears_messages():
    dump = {"state": {"context": {"messages": [{"a": 1}, {"b": 2}]}}, "other": "kept"}
    stripped = _strip_history(dump)
    assert stripped["state"]["context"]["messages"] == []
    assert stripped["other"] == "kept"
    # original is not mutated
    assert dump["state"]["context"]["messages"] == [{"a": 1}, {"b": 2}]


def test_strip_history_tolerates_missing_shape():
    assert _strip_history({}) == {}
    assert _strip_history({"state": "nope"}) == {"state": "nope"}
    assert _strip_history({"state": {"context": {}}}) == {"state": {"context": {}}}


@pytest.mark.asyncio
async def test_materialize_strips_history_when_rollout_exists(tmp_path):
    sessions = tmp_path / "sessions"
    store = ResidencyStore(base_dir=str(tmp_path / "residency"), sessions_base_dir=str(sessions))
    seed_rollout(sessions, "sid", ["hello", "world"])

    role = FakeRole(session_id="sid", messages=[UserMessage("hello"), UserMessage("world")])
    rec = await store.materialize(AgentRuntime(role))

    # History dropped from the record — the rollout owns it.
    assert rec.role_dump["state"]["context"]["messages"] == []


@pytest.mark.asyncio
async def test_rehydrate_refills_history_from_rollout(tmp_path):
    sessions = tmp_path / "sessions"
    store = ResidencyStore(base_dir=str(tmp_path / "residency"), sessions_base_dir=str(sessions))
    seed_rollout(sessions, "sid", ["first", "second", "third"])

    role = FakeRole(session_id="sid", messages=[UserMessage("first"), UserMessage("second"), UserMessage("third")])
    await store.materialize(AgentRuntime(role))

    restored = store.rehydrate("sid", role_loader=make_role_loader())
    contents = [m.content for m in restored.role.state.context.messages]
    assert contents == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_rehydrate_validates_identity_before_refilling_history(tmp_path):
    sessions = tmp_path / "sessions"
    store = ResidencyStore(
        base_dir=str(tmp_path / "residency"),
        sessions_base_dir=str(sessions),
    )
    seed_rollout(sessions, "sid", ["durable"])
    await store.materialize(AgentRuntime(ValidatingFakeRole(session_id="sid")))

    def loader(_role_dump):
        return ValidatingFakeRole(session_id="sid")

    restored = store.rehydrate("sid", role_loader=loader)

    assert restored.role.validated_meta["session_id"] == "sid"
    assert [message.content for message in restored.role.state.context.messages] == ["durable"]


@pytest.mark.asyncio
async def test_rehydrate_no_rollout_keeps_loaded_history(tmp_path):
    """With no rollout, history isn't stripped and replay refill is a no-op."""
    sessions = tmp_path / "sessions"
    store = ResidencyStore(base_dir=str(tmp_path / "residency"), sessions_base_dir=str(sessions))

    role = FakeRole(session_id="sid", messages=[UserMessage("kept")])
    await store.materialize(AgentRuntime(role))

    restored = store.rehydrate("sid", role_loader=make_role_loader())
    contents = [m.content for m in restored.role.state.context.messages]
    assert contents == ["kept"]


@pytest.mark.asyncio
async def test_rehydrate_refill_uses_model_context_projection(tmp_path):
    from mote.runtime.session.events import ContextCompactedFact
    from mote.runtime.session.replay import replay

    sessions = tmp_path / "sessions"
    store = ResidencyStore(base_dir=str(tmp_path / "residency"), sessions_base_dir=str(sessions))
    log = seed_rollout(sessions, "sid", ["pre1", "pre2"])
    source_ids = [str(message.id) for message in replay(log).transcript_messages]
    await log.append(
        ContextCompactedFact(
            model_context_messages=[UserMessage("summary")],
            source_message_ids=source_ids,
            summary="s",
        )
    )
    await log.append(MessageEvent(message=UserMessage("after")))

    role = FakeRole(session_id="sid")
    await store.materialize(AgentRuntime(role))

    restored = store.rehydrate("sid", role_loader=make_role_loader())
    contents = [m.content for m in restored.role.state.context.messages]
    assert contents == ["summary", "after"]
