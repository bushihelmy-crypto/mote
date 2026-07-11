"""Session rollout event schema — the on-disk record types.

Each line of a ``rollout.jsonl`` is one JSON object::

    {"type": <event-type>, "ts": <iso8601>, "payload": {...}}

The event set is a small tagged union (Codex ``RolloutItem`` style). Most events
are funneled through the unified event bus and persisted by
:class:`~mote.session.subscribers.RecorderSubscriber`:

* ``message`` / ``compacted`` originate from the context layer
  (``ContextManager`` emits ``MessageAppendedEvent`` / ``CompactionCheckpointEvent``).
* ``turn_context`` originates from the roles layer (``Role`` / ``RoleState`` emit
  ``TurnEndEvent``).

``session_meta`` is the exception: it is the first line of a fresh log, written
directly by the ``session_log`` property when it builds the log (not via a bus
event), so the rollout's identity has a single source of truth.
``schema_version`` guards future migrations. Message payloads reuse ``Message.dump()`` (parsed to a
dict) so they round-trip losslessly through ``Message.load()`` on resume.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from mote.common.schema import Message


def _dataclass_kwargs(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only the keys of ``payload`` that name a field of ``cls``.

    Lets a field-shaped event reconstruct via ``cls(**_dataclass_kwargs(...))``
    while tolerating unknown/extra keys (forward-compatible reads).
    """
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in payload.items() if k in names}


#: Bump when the persisted event shape changes incompatibly (drives migration).
SCHEMA_VERSION = 1


def _now_iso() -> str:
    return datetime.now().isoformat()


def _message_to_payload(message: Message) -> Dict[str, Any]:
    """Serialize a Message to a JSON-object payload (round-trips via from_dict).

    ``mode="json"`` yields JSON-native types directly (running the same field
    serializers as :meth:`Message.dump`), so we skip the ``dump``→``json.loads``
    string round-trip.
    """
    return message.model_dump(mode="json", exclude_none=True, warnings=False)


def _payload_to_message(payload: Dict[str, Any]) -> Optional[Message]:
    """Reconstruct a Message from a payload dict (forgiving: None on failure)."""
    try:
        return Message.from_dict(payload)
    except Exception:  # noqa: BLE001 — one bad payload must not abort a replay
        return None


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
LLM_CALL = "llm_call"
TERMINAL_STATE = "terminal_state"
KERNEL_STATE = "kernel_state"
BROWSER_STATE = "browser_state"


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

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "SessionMetaEvent":
        return cls(**_dataclass_kwargs(cls, payload))


@dataclass
class MessageEvent:
    """A single message appended to the stored history."""

    message: Message

    type = MESSAGE

    def payload(self) -> Dict[str, Any]:
        return _message_to_payload(self.message)

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "MessageEvent":
        # ``message`` is None when the payload fails to reconstruct; callers
        # treat a None message as a skipped (unloadable) record.
        return cls(message=_payload_to_message(payload))


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

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "CompactedEvent":
        # Drop any unloadable message in the checkpoint (forgiving read).
        messages = [
            m for m in (_payload_to_message(item) for item in payload.get("replacement_history", [])) if m is not None
        ]
        return cls(messages=messages, summary=payload.get("summary", ""))


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

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "TurnContextEvent":
        return cls(**_dataclass_kwargs(cls, payload))


@dataclass
class MetaUpdateEvent:
    """Mutable metadata appended at the tail (fast-read window — Phase 3)."""

    title: Optional[str] = None
    last_prompt: Optional[str] = None

    type = META_UPDATE

    def payload(self) -> Dict[str, Any]:
        return {"title": self.title, "last_prompt": self.last_prompt}

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "MetaUpdateEvent":
        return cls(**_dataclass_kwargs(cls, payload))


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

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "FileSnapshotEvent":
        return cls(**_dataclass_kwargs(cls, payload))


@dataclass
class LLMCallEvent:
    """A single LLM completion's token usage + cost (compact telemetry record).

    Persisted per LLM call so a rollout carries per-request token/cost without
    duplicating the prompt/completion (those already land as ``message`` records
    and as the live ``MessageAppendedEvent`` stream). Purely telemetry: ignored
    by :func:`~mote.session.replay.replay` (not part of the history rebuild).
    """

    request_id: str = ""
    model: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    cost_usd: float = 0.0
    latency_ms: float = 0.0

    type = LLM_CALL

    def payload(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "model": self.model,
            "usage": self.usage,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "LLMCallEvent":
        return cls(**_dataclass_kwargs(cls, payload))


@dataclass
class TerminalStateEvent:
    """The persistent terminal's final environment state (resume restore point).

    Captures the live PTY shell's cwd plus the env diff relative to the shell's
    launch baseline (``env`` = added/changed vars, ``unset`` = vars present at
    launch but removed since). On resume, the latest such event lets the freshly
    started shell be re-seeded (``cd`` + ``export`` + ``unset``) to the saved
    state — *without* re-running any of the original user commands. Last-write-
    wins: replay keeps only the most recent one.
    """

    cwd: str = ""
    env: Dict[str, str] = field(default_factory=dict)
    unset: List[str] = field(default_factory=list)
    tool: str = ""

    type = TERMINAL_STATE

    def payload(self) -> Dict[str, Any]:
        return {
            "cwd": self.cwd,
            "env": self.env,
            "unset": self.unset,
            "tool": self.tool,
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "TerminalStateEvent":
        return cls(**_dataclass_kwargs(cls, payload))


@dataclass
class KernelStateEvent:
    """The persistent kernel's final environment state (resume restore point).

    The Python sibling of :class:`TerminalStateEvent`. Captures the live Jupyter
    kernel process's cwd plus the env diff relative to the kernel's launch
    baseline (``env`` = added/changed vars, ``unset`` = vars present at launch
    but removed since). On resume, the latest such event lets the freshly started
    kernel be re-seeded (``os.chdir`` + ``os.environ.update`` + ``pop``) to the
    saved state — *without* re-running any of the original user code. Only cwd +
    env are restored; the Python namespace (variables/imports/functions) is not.
    Last-write-wins: replay keeps only the most recent one.

    Tracked as a distinct event from :class:`TerminalStateEvent` (not a shared
    ``tool``-tagged record) so the kernel and terminal restores stay independent
    on replay and never clobber each other.
    """

    cwd: str = ""
    env: Dict[str, str] = field(default_factory=dict)
    unset: List[str] = field(default_factory=list)
    tool: str = ""

    type = KERNEL_STATE

    def payload(self) -> Dict[str, Any]:
        return {
            "cwd": self.cwd,
            "env": self.env,
            "unset": self.unset,
            "tool": self.tool,
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "KernelStateEvent":
        return cls(**_dataclass_kwargs(cls, payload))


@dataclass
class BrowserStateEvent:
    """The persistent browser's final browsing state (resume restore point).

    Captures the live Playwright session's open-tab URLs (in page order, plus
    the active tab index) and an optional ``storage_state`` dict ({cookies,
    origins}) carrying the logged-in session. On resume, the latest such event
    lets a freshly launched browser re-open the same tabs (re-navigate to each
    URL) seeded with the saved cookies / localStorage — *without* re-running any
    of the original navigation/click actions. Only the page URLs + storage are
    restored; live DOM state, scroll position, and in-flight JS are not. Last-
    write-wins: replay keeps only the most recent one.

    Tracked as a distinct event from the terminal/kernel state events (not a
    shared ``tool``-tagged record) so the browser restore stays independent on
    replay and never clobbers the others (a session may run a shell + kernel +
    browser, each restored separately).

    Privacy note: ``storage_state`` may include sensitive cookies; it is only
    written when the role's ``record_browser_state`` flag is enabled.
    """

    urls: List[str] = field(default_factory=list)
    active: int = 0
    storage_state: Optional[Dict[str, Any]] = None
    tool: str = ""

    type = BROWSER_STATE

    def payload(self) -> Dict[str, Any]:
        return {
            "urls": self.urls,
            "active": self.active,
            "storage_state": self.storage_state,
            "tool": self.tool,
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "BrowserStateEvent":
        return cls(**_dataclass_kwargs(cls, payload))


#: Any concrete event (tagged union; every member exposes ``.type`` and
#: ``.payload()`` for writing, and a ``from_payload`` classmethod for reading).
SessionEvent = Union[
    SessionMetaEvent,
    MessageEvent,
    CompactedEvent,
    TurnContextEvent,
    MetaUpdateEvent,
    FileSnapshotEvent,
    LLMCallEvent,
    TerminalStateEvent,
    KernelStateEvent,
    BrowserStateEvent,
]

#: Discriminator -> event class, for typed reconstruction from a raw record.
_EVENT_TYPES = {
    SESSION_META: SessionMetaEvent,
    MESSAGE: MessageEvent,
    COMPACTED: CompactedEvent,
    TURN_CONTEXT: TurnContextEvent,
    META_UPDATE: MetaUpdateEvent,
    FILE_SNAPSHOT: FileSnapshotEvent,
    LLM_CALL: LLMCallEvent,
    TERMINAL_STATE: TerminalStateEvent,
    KERNEL_STATE: KernelStateEvent,
    BROWSER_STATE: BrowserStateEvent,
}


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
    whole-log scan (forgiving read, Codex style). This is the line-level concern;
    :func:`parse_event` turns the resulting record into a typed event.
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


def parse_event(record: Dict[str, Any]) -> Optional[SessionEvent]:
    """Reconstruct a typed event from a raw ``{type, ts, payload}`` record.

    Dispatches on the ``type`` discriminator to the matching event class's
    ``from_payload``. Returns ``None`` for an unknown type or a payload that
    cannot be reconstructed, so readers never branch on raw payload keys and one
    bad record never aborts a scan (forgiving, like :func:`parse_line`).
    """
    if not isinstance(record, dict):
        return None
    cls = _EVENT_TYPES.get(record.get("type") or "")
    if cls is None:
        return None
    try:
        return cls.from_payload(record.get("payload") or {})
    except Exception:  # noqa: BLE001 — a malformed record must not abort a scan
        return None


__all__ = [
    "SCHEMA_VERSION",
    "SESSION_META",
    "MESSAGE",
    "COMPACTED",
    "TURN_CONTEXT",
    "META_UPDATE",
    "FILE_SNAPSHOT",
    "LLM_CALL",
    "TERMINAL_STATE",
    "KERNEL_STATE",
    "BROWSER_STATE",
    "SessionMetaEvent",
    "MessageEvent",
    "CompactedEvent",
    "TurnContextEvent",
    "MetaUpdateEvent",
    "FileSnapshotEvent",
    "LLMCallEvent",
    "TerminalStateEvent",
    "KernelStateEvent",
    "BrowserStateEvent",
    "SessionEvent",
    "to_line",
    "parse_line",
    "parse_event",
]
