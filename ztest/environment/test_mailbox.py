#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for Mailbox — ported from input_queue.rs ``mod tests``."""

import asyncio

import pytest

from metagpt.environment.agent_path import AgentPath
from metagpt.environment.mailbox import (
    DeliveryMode,
    InterAgentCommunication,
    Mailbox,
    MAILBOX_AUTHOR_PATH,
    MAILBOX_RECIPIENT_PATH,
)


def make_mail(author: str, recipient: str, content: str, trigger_turn: bool) -> InterAgentCommunication:
    return InterAgentCommunication.new(
        AgentPath.from_string(author),
        AgentPath.from_string(recipient),
        [],
        content,
        trigger_turn,
    )


def test_mailbox_data_event_set_on_enqueue():
    mailbox = Mailbox()
    assert mailbox.empty()
    assert not mailbox._data_event.is_set()
    mailbox.enqueue_communication(make_mail("/root", "/root/worker", "one", False))
    assert mailbox.has_pending()
    assert mailbox._data_event.is_set()


def test_mailbox_drains_in_delivery_order():
    mailbox = Mailbox()
    mailbox.enqueue_communication(make_mail("/root", "/root/worker", "one", False))
    mailbox.enqueue_communication(make_mail("/root/worker", "/root", "two", True))

    drained = mailbox.drain_for_turn()
    assert [m.content for m in drained] == ["one", "two"]
    assert not mailbox.has_pending()
    assert not mailbox._data_event.is_set()
    # metadata carries author + recipient
    assert drained[0].metadata[MAILBOX_AUTHOR_PATH] == "/root"
    assert drained[0].metadata[MAILBOX_RECIPIENT_PATH] == "/root/worker"


def test_mailbox_tracks_pending_trigger_turn():
    mailbox = Mailbox()
    mailbox.enqueue_communication(make_mail("/root", "/root/worker", "queued", False))
    assert not mailbox.has_trigger_turn()
    mailbox.enqueue_communication(make_mail("/root", "/root/worker", "wake", True))
    assert mailbox.has_trigger_turn()


def test_enqueue_raw_message_modes():
    from metagpt.common.schema import UserMessage

    mailbox = Mailbox()
    mailbox.enqueue(UserMessage("queued"), mode=DeliveryMode.QUEUE_ONLY)
    assert not mailbox.has_trigger_turn()
    mailbox.enqueue(UserMessage("wake"), mode=DeliveryMode.TRIGGER_TURN)
    assert mailbox.has_trigger_turn()


def test_dump_load_roundtrip():
    mailbox = Mailbox()
    mailbox.enqueue_communication(make_mail("/root", "/root/worker", "one", False))
    mailbox.enqueue_communication(make_mail("/root/worker", "/root", "two", True))
    restored = Mailbox.load(mailbox.dump())
    drained = restored.drain_for_turn()
    assert [m.content for m in drained] == ["one", "two"]
    assert restored.has_trigger_turn() is False  # drained already


@pytest.mark.asyncio
async def test_wait_for_data():
    mailbox = Mailbox()

    async def producer():
        await asyncio.sleep(0.01)
        mailbox.enqueue_communication(make_mail("/root", "/root/worker", "hi", True))

    task = asyncio.create_task(producer())
    await asyncio.wait_for(mailbox.wait_for_data(), timeout=1.0)
    assert mailbox.has_pending()
    await task
