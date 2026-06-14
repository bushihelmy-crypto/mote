"""Session rollout event schema — the on-disk record types.

Each line of a ``rollout.jsonl`` is one JSON object::

    {"type": <event-type>, "ts": <iso8601>, "payload": {...}}

The event set is a small tagged union (Codex ``RolloutItem`` style), aggregating
data from two layers:

* ``message`` / ``compacted`` originate from the context layer
  (``ContextManager``), streamed through the injected ``SessionRecorder``.
* ``session_meta`` / ``turn_context`` / ``meta_update`` originate from the roles
  layer (``Role`` / ``RoleState``).

``session_meta`` is always the first line of a fresh log; ``schema_version``
guards future migrations. Message payloads reuse ``Message.dump()`` (parsed to a
dict) so they round-trip losslessly through ``Message.load()`` on resume.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from metagpt.common.schema import Message

#: Bump when the persisted event shape changes incompatibly (drives migration).
SCHEMA_VERSION = 1


def _now_iso() -> str:
    return datetime.now().isoformat()


def _message_to_payload(message: Message) -> Dict[str, Any]:
    """Serialize a Message to a JSON-object payload (round-trips via load)."""
    return json.loads(message.dump())


def _payload_to_message(payload: Dict[str, Any]) -> Optional[Message]:
    """Reconstruct a Message from a payload dict (Phase 2 replay)."""
    return Message.load(json.dumps(payload))


# ---------------------------------------------------------------------------
# Event types (tagged union, discriminated by ``type``)
# ---------------------------------------------------------------------------

#: Event-type discriminators.
SESSION_META = "session_meta"
MESSAGE = "message"
COMPACTED = "compacted"
TURN_CONTEXT = "turn_context"
META_UPDATE = "meta_update"
FILE_SNAPSHOT = "file_snapshot"


@dataclass
class SessionMetaEvent:
    """First-line metadata identifying the session (Codex ``SessionMeta``)."""

    session_id: str
    schema_version: int = SCHEMA_VERSION
    parent_session_id: Optional[str] = None
    created_at: str = field(default_factory=_now_iso)
    working_dir: str = ""
    original_working_dir: str = ""
    project_root: str = ""
    model: Optional[str] = None
    role_class: Optional[str] = None

    type = SESSION_META

    def payload(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class MessageEvent:
    """A single message appended to the stored history."""

    message: Message

    type = MESSAGE

    def payload(self) -> Dict[str, Any]:
        return _message_to_payload(self.message)


@dataclass
class CompactedEvent:
    """A compaction checkpoint: the rebuilt history + its summary.

    ``replacement_history`` is the full post-compaction message list, acting as
    a replay checkpoint (resume starts from the latest one — Codex style).
    """

    messages: List[Message]
    summary: str = ""

    type = COMPACTED

    def payload(self) -> Dict[str, Any]:
        return {
            "summary": self.summary,
            "replacement_history": [_message_to_payload(m) for m in self.messages],
        }


@dataclass
class TurnContextEvent:
    """Per-turn runtime snapshot written at the turn boundary."""

    turn_id: str
    working_dir: str = ""
    model: Optional[str] = None
    token_state: Optional[Dict[str, Any]] = None

    type = TURN_CONTEXT

    def payload(self) -> Dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "working_dir": self.working_dir,
            "model": self.model,
            "token_state": self.token_state,
        }


@dataclass
class MetaUpdateEvent:
    """Mutable metadata appended at the tail (fast-read window — Phase 3)."""

    title: Optional[str] = None
    last_prompt: Optional[str] = None

    type = META_UPDATE

    def payload(self) -> Dict[str, Any]:
        return {"title": self.title, "last_prompt": self.last_prompt}


@dataclass
class FileSnapshotEvent:
    """A before-image snapshot of a file the agent is about to mutate.

    Records the file's pre-write state (Claude Code ``fileHistory`` style): the
    content itself is stored content-addressed in the session blob store and
    referenced here by ``pre_hash`` (``None`` when the file did not yet exist,
    i.e. a create). This is the truth source for diff / undo / rollback.
    """

    path: str
    operation: str = "update"  # one of: create | update
    pre_hash: Optional[str] = None
    pre_size: int = 0
    display_path: str = ""
    tool: str = ""
    backend: str = "blob"  # which store holds the blob: "blob" | "git"

    type = FILE_SNAPSHOT

    def payload(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "operation": self.operation,
            "pre_hash": self.pre_hash,
            "pre_size": self.pre_size,
            "display_path": self.display_path or self.path,
            "tool": self.tool,
            "backend": self.backend,
        }


#: Any concrete event.
SessionEvent = Any  # union of the dataclasses above (all expose .type/.payload())


# ---------------------------------------------------------------------------
# Line (de)serialization
# ---------------------------------------------------------------------------


def to_line(event: SessionEvent) -> str:
    """Serialize an event to a single JSONL line (no trailing newline)."""
    record = {"type": event.type, "ts": _now_iso(), "payload": event.payload()}
    return json.dumps(record, ensure_ascii=False)


def parse_line(line: str) -> Optional[Dict[str, Any]]:
    """Parse a JSONL line into a raw ``{type, ts, payload}`` dict.

    Returns ``None`` for blank/corrupt lines so a single bad line never aborts a
    whole-log scan (forgiving read, Codex style). Typed reconstruction (into
    Messages etc.) is a Phase 2 concern and uses the helpers below.
    """
    line = line.strip()
    if not line:
        return None
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict) or "type" not in record:
        return None
    return record


__all__ = [
    "SCHEMA_VERSION",
    "SESSION_META",
    "MESSAGE",
    "COMPACTED",
    "TURN_CONTEXT",
    "META_UPDATE",
    "FILE_SNAPSHOT",
    "SessionMetaEvent",
    "MessageEvent",
    "CompactedEvent",
    "TurnContextEvent",
    "MetaUpdateEvent",
    "FileSnapshotEvent",
    "SessionEvent",
    "to_line",
    "parse_line",
]
