#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Mailbox — per-agent inbound message queue with turn-atomic delivery.

Port of codex's ``InputQueue`` mailbox half (``codex-rs/core/src/session/
input_queue.rs``) plus ``InterAgentCommunication``. Each ``AgentRuntime`` owns
exactly one ``Mailbox``. The **scheduler** (not the react loop) drains it at turn
boundaries via :meth:`Mailbox.drain_for_turn`, so deferral of mid-turn-injected
mail falls out for free and ``ExecutionEngine`` stays untouched.

Delivery mode:
  * ``DeliveryMode.TRIGGER_TURN`` — enqueue + wake the runtime to start a turn.
  * ``DeliveryMode.QUEUE_ONLY``   — enqueue only; counts for unloadability but
    does not start a turn (delivered at the next boundary the agent runs).

The mailbox stores full :class:`Message` items so that any message kind
(``UserMessage``/``AIMessage``/...) can be routed through the control plane.
``InterAgentCommunication`` is the structured codex-style form used by
``send_inter_agent_communication``; it converts to a ``UserMessage`` on enqueue.
"""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Any, List, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from mote.contracts.conversation import Message, UserMessage
from mote.orchestration.agents.identity.path import AgentPath
from mote.orchestration.agents.messaging.routing import CommKind

# Metadata keys carried on the staged UserMessage so the recipient can see who
# wrote it, which path it was addressed to, what kind of message it is, and which
# named channel (if any) it arrived on.
MAILBOX_AUTHOR_PATH = "mailbox_author_path"
MAILBOX_RECIPIENT_PATH = "mailbox_recipient_path"
MAILBOX_KIND = "mailbox_kind"
MAILBOX_CHANNEL = "mailbox_channel"


class DeliveryMode(str, Enum):
    """Whether an enqueued item should wake the runtime to start a turn."""

    QUEUE_ONLY = "queue_only"
    TRIGGER_TURN = "trigger_turn"


class InterAgentCommunication(BaseModel):
    """A structured agent->agent message (port of codex ``InterAgentCommunication``)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    communication_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1)
    author: AgentPath
    recipient: AgentPath
    attachments: list[object] = Field(default_factory=list)
    content: str = ""
    trigger_turn: bool = False
    # The semantic kind of the message (orthogonal to ``trigger_turn``, which is
    # only the wake-a-turn flag); ``channel`` names the broadcast group it
    # arrived on, when delivered via a named channel.
    kind: CommKind = CommKind.TASK
    channel: Optional[str] = None

    @classmethod
    def new(
        cls,
        author: AgentPath,
        recipient: AgentPath,
        attachments: Optional[list[object]] = None,
        content: str = "",
        trigger_turn: bool = False,
        kind: CommKind = CommKind.TASK,
        channel: Optional[str] = None,
    ) -> "InterAgentCommunication":
        """Positional constructor matching the rust ``::new`` signature."""
        return cls(
            author=author,
            recipient=recipient,
            attachments=list(attachments or []),
            content=content,
            trigger_turn=trigger_turn,
            kind=kind,
            channel=channel,
        )

    def to_message(self) -> UserMessage:
        """Materialize this communication as a ``UserMessage`` for delivery."""
        msg = UserMessage(id=self.communication_id, content=self.content, sent_from=self.author.as_str())
        msg.add_metadata(MAILBOX_AUTHOR_PATH, self.author.as_str())
        msg.add_metadata(MAILBOX_RECIPIENT_PATH, self.recipient.as_str())
        msg.add_metadata(MAILBOX_KIND, self.kind.value)
        if self.channel is not None:
            msg.add_metadata(MAILBOX_CHANNEL, self.channel)
        return msg


class _MailboxItem:
    """An enqueued item: the message plus its trigger-turn flag."""

    __slots__ = ("sequence", "delivery_id", "message", "trigger_turn")

    def __init__(self, sequence: int, delivery_id: str, message: Message, trigger_turn: bool):
        self.sequence = sequence
        self.delivery_id = delivery_id
        self.message = message
        self.trigger_turn = trigger_turn


class Mailbox:
    """A per-runtime inbound queue drained only at turn boundaries.

    The ``_data_event`` is set whenever any item is enqueued and signals that the
    mailbox is non-empty (used for unloadability checks). It is independent of the
    runtime's ``wake_event`` (which gates *starting a turn*): a queue-only item
    sets ``_data_event`` but never the wake event.
    """

    def __init__(self, owner_agent_id: str):
        if type(owner_agent_id) is not str or not owner_agent_id:
            raise ValueError("mailbox owner_agent_id must be a non-empty string")
        self.owner_agent_id = owner_agent_id
        self._items: list[_MailboxItem] = []
        self._next_sequence = 1
        self._data_event = asyncio.Event()

    def _append(self, message: Message, trigger_turn: bool, *, delivery_id: str | None = None) -> None:
        delivery_id = delivery_id or str(message.id)
        if not delivery_id:
            raise ValueError("mailbox message delivery identity is required")
        self._items.append(_MailboxItem(self._next_sequence, delivery_id, message, trigger_turn))
        self._next_sequence += 1
        self._data_event.set()

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------
    def enqueue(
        self, message: Message, *, mode: DeliveryMode = DeliveryMode.TRIGGER_TURN, delivery_id: str | None = None
    ) -> None:
        """Enqueue a raw message with the given delivery mode."""
        publication_id = message.metadata.get("output_publication_id")
        if publication_id and any(
            item.message.metadata.get("output_publication_id") == publication_id for item in self._items
        ):
            return
        if delivery_id is not None and any(item.delivery_id == delivery_id for item in self._items):
            return
        self._append(message, mode is DeliveryMode.TRIGGER_TURN, delivery_id=delivery_id)

    def enqueue_communication(self, communication: InterAgentCommunication) -> None:
        """Enqueue an :class:`InterAgentCommunication` (trigger flag from the comm)."""
        self._append(communication.to_message(), communication.trigger_turn)

    # ------------------------------------------------------------------
    # Drain (turn boundary only)
    # ------------------------------------------------------------------
    def drain_for_turn(self) -> List[Message]:
        """Drain every pending item in delivery order, returning their messages.

        Clears ``_data_event``. Called by the scheduler between ``run()`` calls,
        never mid-turn, so the loop never sees mail injected during a turn.
        """
        drained = [item.message for item in self._items]
        self._items.clear()
        self._data_event.clear()
        return drained

    def drain_for_processing(self) -> tuple[List[Message], tuple[str, ...]]:
        messages = [item.message for item in self._items]
        delivery_ids = tuple(item.delivery_id for item in self._items)
        self._items.clear()
        self._data_event.clear()
        return messages, delivery_ids

    def restore_processing(self, messages: List[Message], delivery_ids: tuple[str, ...]) -> None:
        """Restore an unaccepted turn batch without losing its wake authority."""
        if len(messages) != len(delivery_ids) or not messages:
            raise ValueError("mailbox processing batch is invalid")
        restored = [
            _MailboxItem(index, delivery_id, message, index == len(messages))
            for index, (message, delivery_id) in enumerate(zip(messages, delivery_ids, strict=True), start=1)
        ]
        self._items = [*restored, *self._items]
        self._next_sequence = max(self._next_sequence, len(restored) + 1)
        self._data_event.set()

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    def has_trigger_turn(self) -> bool:
        return any(item.trigger_turn for item in self._items)

    def empty(self) -> bool:
        return not self._items

    async def wait_for_data(self) -> None:
        """Await until at least one item is pending."""
        await self._data_event.wait()

    # ------------------------------------------------------------------
    # Persistence helpers (used by ResidencyStore)
    # ------------------------------------------------------------------
    def dump(self) -> dict[str, Any]:
        """Serialize pending items to the canonical Mailbox v1 envelope."""
        return {
            "schema": "mote.agent-mailbox/v1",
            "schema_version": 1,
            "owner_agent_id": self.owner_agent_id,
            "next_sequence": self._next_sequence,
            "items": [
                {
                    "sequence": item.sequence,
                    "delivery_id": item.delivery_id,
                    "message": item.message.dump(),
                    "trigger_turn": item.trigger_turn,
                }
                for item in self._items
            ],
        }

    @staticmethod
    def load(data: object, *, expected_owner_agent_id: str) -> "Mailbox":
        """Strictly rebuild a mailbox from the canonical v1 envelope."""
        if type(data) is not dict or set(data) != {
            "schema",
            "schema_version",
            "owner_agent_id",
            "next_sequence",
            "items",
        }:
            raise ValueError("mailbox envelope fields are not canonical")
        assert isinstance(data, dict)
        if data["schema"] != "mote.agent-mailbox/v1" or type(data["schema_version"]) is not int:
            raise ValueError("mailbox schema is unsupported")
        if data["schema_version"] != 1:
            raise ValueError("mailbox schema version is unsupported")
        owner = data["owner_agent_id"]
        if type(owner) is not str or not owner or owner != expected_owner_agent_id:
            raise ValueError("mailbox owner identity mismatch")
        next_sequence = data["next_sequence"]
        if type(next_sequence) is not int or next_sequence < 1:
            raise ValueError("mailbox next_sequence is invalid")
        items = data["items"]
        if type(items) is not list:
            raise ValueError("mailbox items must be a list")
        mailbox = Mailbox(owner)
        seen_sequences: set[int] = set()
        seen_deliveries: set[str] = set()
        previous_sequence = 0
        for entry in items:
            if type(entry) is not dict or set(entry) != {
                "sequence",
                "delivery_id",
                "message",
                "trigger_turn",
            }:
                raise ValueError("mailbox item fields are not canonical")
            sequence = entry["sequence"]
            delivery_id = entry["delivery_id"]
            if type(sequence) is not int or sequence < 1 or sequence >= next_sequence:
                raise ValueError("mailbox item sequence is invalid")
            if sequence in seen_sequences or sequence <= previous_sequence:
                raise ValueError("mailbox item sequences must be unique and ordered")
            if type(delivery_id) is not str or not delivery_id or delivery_id in seen_deliveries:
                raise ValueError("mailbox delivery identity is invalid or duplicated")
            if type(entry["trigger_turn"]) is not bool:
                raise ValueError("mailbox trigger_turn must be a boolean")
            msg = Message.load(entry["message"])
            if msg is None or str(msg.id) != delivery_id:
                raise ValueError("mailbox message delivery identity mismatch")
            mailbox._items.append(_MailboxItem(sequence, delivery_id, msg, entry["trigger_turn"]))
            seen_sequences.add(sequence)
            seen_deliveries.add(delivery_id)
            previous_sequence = sequence
        mailbox._next_sequence = next_sequence
        if mailbox._items:
            mailbox._data_event.set()
        return mailbox


__all__ = [
    "DeliveryMode",
    "InterAgentCommunication",
    "Mailbox",
    "MAILBOX_AUTHOR_PATH",
    "MAILBOX_RECIPIENT_PATH",
    "MAILBOX_KIND",
    "MAILBOX_CHANNEL",
]
