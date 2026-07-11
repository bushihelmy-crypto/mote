#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for PendingDeliveryQueue — the plane-level park-and-fulfil buffer."""

import asyncio

import pytest
from mote.common.schema.messages import UserMessage
from mote.environment.agent_path import AgentPath
from mote.environment.mailbox import CommKind, DeliveryMode, InterAgentCommunication
from mote.environment.pending_delivery import PendingDelivery, PendingDeliveryQueue


def test_park_then_take_all_fifo():
    q = PendingDeliveryQueue()
    q.park("a", PendingDelivery(message=UserMessage("1")))
    q.park("a", PendingDelivery(message=UserMessage("2")))
    q.park("b", PendingDelivery(message=UserMessage("x")))
    batch = q.take_all("a")
    assert [d.message.content for d in batch] == ["1", "2"]
    # take_all removes the agent's queue entirely
    assert not q.has_pending("a")
    assert q.has_pending("b")


def test_take_all_empty_agent_returns_empty_list():
    q = PendingDeliveryQueue()
    assert q.take_all("ghost") == []


def test_agents_with_pending_snapshot():
    q = PendingDeliveryQueue()
    assert q.agents_with_pending() == []
    q.park("a", PendingDelivery(message=UserMessage("1")))
    q.park("b", PendingDelivery(message=UserMessage("2")))
    assert set(q.agents_with_pending()) == {"a", "b"}


def test_has_pending_specific_and_any():
    q = PendingDeliveryQueue()
    assert not q.has_pending()
    assert not q.has_pending("a")
    q.park("a", PendingDelivery(message=UserMessage("1")))
    assert q.has_pending()
    assert q.has_pending("a")
    assert not q.has_pending("b")


def test_drop_forgets_agent_queue():
    q = PendingDeliveryQueue()
    q.park("a", PendingDelivery(message=UserMessage("1")))
    q.drop("a")
    assert not q.has_pending("a")
    # dropping an unknown agent is a no-op
    q.drop("ghost")


def test_note_back_pressure_counts_consecutive_passes():
    q = PendingDeliveryQueue()
    q.park("a", PendingDelivery(message=UserMessage("1")))
    assert q.note_back_pressure("a") == 1
    assert q.note_back_pressure("a") == 2
    assert q.note_back_pressure("a") == 3


def test_take_all_resets_back_pressure():
    q = PendingDeliveryQueue()
    q.park("a", PendingDelivery(message=UserMessage("1")))
    q.note_back_pressure("a")
    q.note_back_pressure("a")
    q.take_all("a")  # delivered → counter cleared
    q.park("a", PendingDelivery(message=UserMessage("2")))
    assert q.note_back_pressure("a") == 1


def test_drop_resets_back_pressure():
    q = PendingDeliveryQueue()
    q.park("a", PendingDelivery(message=UserMessage("1")))
    q.note_back_pressure("a")
    q.note_back_pressure("a")
    q.drop("a")  # target gone → counter cleared
    q.park("a", PendingDelivery(message=UserMessage("2")))
    assert q.note_back_pressure("a") == 1


def test_is_communication_discriminator():
    msg_delivery = PendingDelivery(message=UserMessage("m"))
    comm = InterAgentCommunication.new(
        author=AgentPath.from_string("/root/p"),
        recipient=AgentPath.from_string("/root/c"),
        content="hi",
        kind=CommKind.TASK,
    )
    comm_delivery = PendingDelivery(communication=comm)
    assert msg_delivery.is_communication is False
    assert comm_delivery.is_communication is True


def test_default_mode_is_trigger_turn():
    d = PendingDelivery(message=UserMessage("m"))
    assert d.mode is DeliveryMode.TRIGGER_TURN


def test_has_trigger_pending_message_modes():
    q = PendingDeliveryQueue()
    assert not q.has_trigger_pending()
    # a queue-only park is not, on its own, outstanding turn work
    q.park("a", PendingDelivery(message=UserMessage("later"), mode=DeliveryMode.QUEUE_ONLY))
    assert not q.has_trigger_pending()
    # a trigger-turn park is
    q.park("b", PendingDelivery(message=UserMessage("now"), mode=DeliveryMode.TRIGGER_TURN))
    assert q.has_trigger_pending()


def test_has_trigger_pending_communication_flag():
    q = PendingDeliveryQueue()
    notify = InterAgentCommunication.new(
        author=AgentPath.from_string("/root/c"),
        recipient=AgentPath.from_string("/root/p"),
        content="done",
        trigger_turn=False,
        kind=CommKind.NOTIFICATION,
    )
    q.park("p", PendingDelivery(communication=notify))
    assert not q.has_trigger_pending()  # queue-only notification
    task = InterAgentCommunication.new(
        author=AgentPath.from_string("/root/p"),
        recipient=AgentPath.from_string("/root/c"),
        content="go",
        trigger_turn=True,
        kind=CommKind.TASK,
    )
    q.park("c", PendingDelivery(communication=task))
    assert q.has_trigger_pending()


@pytest.mark.asyncio
async def test_waker_releases_on_park():
    q = PendingDeliveryQueue()
    waiter = asyncio.create_task(q.wait_for_pending())
    await asyncio.sleep(0)  # let the waiter park on the event
    assert not waiter.done()
    q.park("a", PendingDelivery(message=UserMessage("1")))
    await asyncio.wait_for(waiter, timeout=1.0)  # released by the park


@pytest.mark.asyncio
async def test_clear_waker_rearms():
    q = PendingDeliveryQueue()
    q.park("a", PendingDelivery(message=UserMessage("1")))
    # waker is set from the park → wait returns immediately
    await asyncio.wait_for(q.wait_for_pending(), timeout=1.0)
    q.clear_waker()
    # after clearing, a fresh waiter blocks again until the next park
    waiter = asyncio.create_task(q.wait_for_pending())
    await asyncio.sleep(0)
    assert not waiter.done()
    q.park("b", PendingDelivery(message=UserMessage("2")))
    await asyncio.wait_for(waiter, timeout=1.0)
