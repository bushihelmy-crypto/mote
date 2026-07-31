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
from typing import List, Optional

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
        msg = UserMessage(content=self.content, sent_from=self.author.as_str())
        msg.add_metadata(MAILBOX_AUTHOR_PATH, self.author.as_str())
        msg.add_metadata(MAILBOX_RECIPIENT_PATH, self.recipient.as_str())
        msg.add_metadata(MAILBOX_KIND, self.kind.value)
        if self.channel is not None:
            msg.add_metadata(MAILBOX_CHANNEL, self.channel)
        return msg


class _MailboxItem:
    """An enqueued item: the message plus its trigger-turn flag."""

    __slots__ = ("message", "trigger_turn")

    def __init__(self, message: Message, trigger_turn: bool):
        self.message = message
        self.trigger_turn = trigger_turn


class Mailbox:
    """A per-runtime inbound queue drained only at turn boundaries.

    The ``_data_event`` is set whenever any item is enqueued and signals that the
    mailbox is non-empty (used for unloadability checks). It is independent of the
    runtime's ``wake_event`` (which gates *starting a turn*): a queue-only item
    sets ``_data_event`` but never the wake event.
    """

    def __init__(self):
        self._items: list[_MailboxItem] = []
        self._data_event = asyncio.Event()

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------
    def enqueue(self, message: Message, *, mode: DeliveryMode = DeliveryMode.TRIGGER_TURN) -> None:
        """Enqueue a raw message with the given delivery mode."""
        publication_id = message.metadata.get("output_publication_id")
        if publication_id and any(
            item.message.metadata.get("output_publication_id") == publication_id for item in self._items
        ):
            return
        self._items.append(_MailboxItem(message, mode is DeliveryMode.TRIGGER_TURN))
        self._data_event.set()

    def enqueue_communication(self, communication: InterAgentCommunication) -> None:
        """Enqueue an :class:`InterAgentCommunication` (trigger flag from the comm)."""
        self._items.append(_MailboxItem(communication.to_message(), communication.trigger_turn))
        self._data_event.set()

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
    def dump(self) -> list[dict]:
        """Serialize pending items to a plain list of ``{message, trigger_turn}``."""
        return [{"message": item.message.dump(), "trigger_turn": item.trigger_turn} for item in self._items]

    @staticmethod
    def load(data: Optional[list[dict]]) -> "Mailbox":
        """Rebuild a mailbox from :meth:`dump` output."""
        mailbox = Mailbox()
        for entry in data or []:
            msg = Message.load(entry["message"])
            if msg is None:
                continue
            mailbox._items.append(_MailboxItem(msg, bool(entry.get("trigger_turn"))))
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
