#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ResidencyStore — materialize/rehydrate evicted agents."""

import types

import pytest

from metagpt.common.schema.messages import UserMessage
from metagpt.common.schema.queue import MessageQueue
from metagpt.environment.mailbox import Mailbox
from metagpt.environment.runtime import AgentRuntime
from metagpt.environment.store import ResidencyRecord, ResidencyStore


class FakeRole:
    """Duck-typed Role for store round-trips."""

    def __init__(self, session_id="sess-1", payload=None):
        self._session_id = session_id
        self.state = types.SimpleNamespace(msg_buffer=MessageQueue())
        self.payload = payload or {}

    @property
    def session_id(self):
        return self._session_id

    def dump(self):
        return {"session_id": self._session_id, "payload": self.payload}


def make_role_loader():
    """A role_loader that rebuilds a FakeRole from its dump."""

    def loader(role_dump):
        return FakeRole(
            session_id=role_dump.get("session_id", "sess-1"),
            payload=role_dump.get("payload", {}),
        )

    return loader


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
    store = ResidencyStore(base_dir=str(tmp_path))
    assert not store.has("nope")
    assert store.read_record("nope") is None
    assert store.rehydrate("nope", role_loader=make_role_loader()) is None


@pytest.mark.asyncio
async def test_materialize_writes_record(tmp_path):
    store = ResidencyStore(base_dir=str(tmp_path))
    role = FakeRole(session_id="abc", payload={"k": "v"})
    role.state.msg_buffer.push(UserMessage("buffered"))
    mailbox = Mailbox()
    mailbox.enqueue(UserMessage("mail"))
    rt = AgentRuntime(role, mailbox)

    rec = await store.materialize(rt)
    assert store.has("abc")
    assert rec.role_dump == {"session_id": "abc", "payload": {"k": "v"}}
    assert rec.mailbox_dump  # non-empty
    assert "buffered" in rec.msg_buffer_dump

    read_back = store.read_record("abc")
    assert read_back.role_dump == rec.role_dump


@pytest.mark.asyncio
async def test_rehydrate_restores_buffer_and_mailbox(tmp_path):
    store = ResidencyStore(base_dir=str(tmp_path))
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
    store = ResidencyStore(base_dir=str(tmp_path))
    role = FakeRole(session_id="abc")
    rt = AgentRuntime(role)
    await store.materialize(rt)
    assert store.has("abc")
    store.forget("abc")
    assert not store.has("abc")
    store.forget("abc")  # idempotent / safe on missing


@pytest.mark.asyncio
async def test_materialize_empty_buffer_and_mailbox(tmp_path):
    store = ResidencyStore(base_dir=str(tmp_path))
    rt = AgentRuntime(FakeRole(session_id="empty"))
    rec = await store.materialize(rt)
    assert rec.msg_buffer_dump == "[]"
    assert rec.mailbox_dump == []
    restored = store.rehydrate("empty", role_loader=make_role_loader())
    assert restored.msg_buffer.empty()
    assert restored.mailbox.empty()
