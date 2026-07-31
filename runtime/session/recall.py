"""Ledger recall — fetch a single message body back out of the rollout by id.

The rollout is append-only: a ``MessageEvent`` is persisted the instant a message
is added to the stored history, carrying its *original* full body. Later
compaction may fold that body to a placeholder or drop it from the *live* history
entirely, but the rollout line is never rewritten — the original text is still on
disk. This module is the read side of that fact: given a ``tool_call_id`` it
scans the rollout forward and hands back the message body as first recorded.

It is the shared key behind two capabilities built on top of it:

* *undo an erase* — an inverse tool can re-add a tool result the compaction layer
  pair-deleted, pulling the body from here rather than re-running the tool.
* *memory recall* — surface a specific past result on demand without keeping it
  resident in the window.

Like :mod:`mote.runtime.session.history` this is a single forward scan of the same
``rollout.jsonl`` the session already owns — no second index.
"""

from __future__ import annotations

from typing import Optional

from mote.contracts.conversation import Message
from mote.contracts.conversation.fields import TOOL_CALL_ID
from mote.runtime.session.codec import decode_session_event
from mote.runtime.session.events import MessageEvent
from mote.runtime.session.log import SessionLog


def body_for_tool_call(log: SessionLog, tool_call_id: str) -> Optional[Message]:
    """Return the originally-recorded message body for ``tool_call_id``.

    Scans the rollout forward for the ``MessageEvent`` whose message is the
    tool-result answering ``tool_call_id`` (its ``metadata[TOOL_CALL_ID]``), and
    returns that reconstructed :class:`Message` — the full body as first
    appended, before any in-place fold or pair-delete touched the live history.
    Returns ``None`` when no such record exists. The last match wins, so a
    re-added id resolves to its most recent recording.
    """
    if not tool_call_id:
        return None

    found: Optional[Message] = None
    for envelope in log.iter_events():
        event = decode_session_event(envelope)
        if not isinstance(event, MessageEvent):
            continue
        message = event.message
        if message.metadata.get(TOOL_CALL_ID) == tool_call_id:
            found = message
    return found


__all__ = ["body_for_tool_call"]
