#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ResidencyStore — materialize/rehydrate evicted agents to/from disk.

Port of codex ``rollout materialize`` + load-on-demand. When residency evicts an
idle agent, the control plane must persist everything not already covered by the
session's append-only ``rollout.jsonl`` — namely the parts that are runtime-only:
the ``msg_buffer`` (``exclude=True`` on RoleState) and the per-runtime
``Mailbox`` — plus the role's *configuration* (``role_schema``) and the residual
``RoleState``. A :class:`ResidencyRecord` bundles the three pieces:

    {role_dump, mailbox_dump, msg_buffer_dump}

**Conversation history is NOT stored here.** The rollout log
(``session/rollout.jsonl``) is the single truth source for history; it is written
incrementally every turn. So materialize *strips* ``state.context.messages`` from
``role_dump`` (when a rollout exists), and rehydrate refills it via
:func:`metagpt.session.replay`. This eliminates the old double-write of the full
message history into the residency record.

``MessageQueue.dump()`` is **async**, so :meth:`materialize` is async.
``ResidencyRecord`` is a plain JSON dict on disk (NOT a ``BaseSerialization``
with ``extra="forbid"``) so it stays forgiving across schema tweaks.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from metagpt.common.disk import atomic_write, get_disk_writer
from metagpt.common.logs import logger
from metagpt.common.schema import MessageQueue
from metagpt.environment.mailbox import Mailbox
from metagpt.environment.runtime import AgentRuntime
from metagpt.session import SessionLog, replay


@dataclass
class ResidencyRecord:
    """The on-disk shape of an unloaded agent.

    ``role_dump`` carries the role's config + residual state with the
    conversation history stripped out (the rollout owns history). The other two
    fields cover runtime-only state the rollout never records.
    """

    role_dump: dict
    mailbox_dump: list
    msg_buffer_dump: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @staticmethod
    def from_json(data: str) -> "ResidencyRecord":
        raw = json.loads(data)
        return ResidencyRecord(
            role_dump=raw.get("role_dump", {}),
            mailbox_dump=raw.get("mailbox_dump", []),
            msg_buffer_dump=raw.get("msg_buffer_dump", "[]"),
        )


def _default_role_loader(role_dump: dict) -> Any:
    # Lazy import to keep environment -> roles dependency out of module load.
    from metagpt.common.base import BaseRole

    return BaseRole.load(role_dump)


def _strip_history(role_dump: dict) -> dict:
    """Return a copy of ``role_dump`` with ``state.context.messages`` emptied.

    The rollout is the truth source for history, so the residency record drops
    the (potentially large) message list to avoid a redundant second copy. Only
    the messages are cleared; the rest of the context/state is preserved.
    """
    state = role_dump.get("state")
    if not isinstance(state, dict):
        return role_dump
    context = state.get("context")
    if not isinstance(context, dict) or "messages" not in context:
        return role_dump
    role_dump = dict(role_dump)
    state = dict(state)
    context = dict(context)
    context["messages"] = []
    state["context"] = context
    role_dump["state"] = state
    return role_dump


class ResidencyStore:
    """Reads/writes :class:`ResidencyRecord` files keyed by ``session_id``.

    ``sessions_base_dir`` locates the rollout logs that own conversation history
    (defaults to the standard ``.agent_sessions`` workspace root); injected in
    tests to redirect both the residency records and the rollout logs.
    """

    def __init__(self, base_dir: Optional[str] = None, *, sessions_base_dir: Optional[str] = None):
        if base_dir is None:
            from metagpt.common.const import DEFAULT_WORKSPACE_ROOT

            base_dir = str(Path(DEFAULT_WORKSPACE_ROOT) / ".agent_residency")
        self._base = Path(base_dir)
        self._sessions_base_dir = sessions_base_dir

    def _path(self, session_id: str) -> Path:
        return self._base / f"{session_id}.json"

    def _session_log(self, session_id: str) -> SessionLog:
        return SessionLog(session_id, base_dir=self._sessions_base_dir)

    def has(self, session_id: str) -> bool:
        return self._path(session_id).exists()

    # ------------------------------------------------------------------
    # Materialize (write)
    # ------------------------------------------------------------------
    async def materialize(self, runtime: AgentRuntime) -> ResidencyRecord:
        """Persist *runtime* to disk and return the written record.

        Strips conversation history from the role dump when a rollout exists for
        the session, since the rollout already holds it as the truth source.
        """
        role_dump = runtime.role.dump()
        if self._session_log(runtime.session_id).exists():
            role_dump = _strip_history(role_dump)
        record = ResidencyRecord(
            role_dump=role_dump,
            mailbox_dump=runtime.mailbox.dump(),
            msg_buffer_dump=await runtime.msg_buffer.dump(),
        )
        self._base.mkdir(parents=True, exist_ok=True)
        # Atomic + ordered: a crash mid-write never leaves a half-written record
        # (the old non-atomic write_text could corrupt it), and awaiting submit
        # guarantees the record is on disk before this returns.
        path = self._path(runtime.session_id)
        data = record.to_json().encode("utf-8")
        await get_disk_writer().submit(str(path), lambda: atomic_write(path, data))
        return record

    # ------------------------------------------------------------------
    # Rehydrate (read + rebuild)
    # ------------------------------------------------------------------
    def read_record(self, session_id: str) -> Optional[ResidencyRecord]:
        path = self._path(session_id)
        if not path.exists():
            return None
        return ResidencyRecord.from_json(path.read_text(encoding="utf-8"))

    def rehydrate(
        self,
        session_id: str,
        *,
        role_loader: Optional[Callable[[dict], Any]] = None,
    ) -> Optional[AgentRuntime]:
        """Rebuild an :class:`AgentRuntime` from disk, or ``None`` if absent.

        ``role_loader`` reconstructs a Role from ``role_dump`` (defaults to the
        polymorphic ``BaseRole.load``); tests inject a fake loader. Conversation
        history is replayed from the rollout (the truth source) and written back
        onto ``role.state.context.messages``; the runtime-only ``msg_buffer`` and
        ``Mailbox`` come from the record.
        """
        record = self.read_record(session_id)
        if record is None:
            return None
        loader = role_loader or _default_role_loader
        role = loader(record.role_dump)
        self._refill_history(role, session_id)
        # The msg_buffer is excluded from RoleState serialization, so restore it
        # explicitly from the record (else unload = silent message loss).
        role.state.msg_buffer = MessageQueue.load(record.msg_buffer_dump)
        mailbox = Mailbox.load(record.mailbox_dump)
        return AgentRuntime(role, mailbox)

    def _refill_history(self, role: Any, session_id: str) -> None:
        """Replay the rollout and write its history onto the role's context.

        No-op when the rollout has no messages (nothing to restore) or the role
        is duck-typed without a ``state.context.messages`` list — in which case
        whatever the loader produced is left untouched.
        """
        # replay scans via iter_raw, whose drain flushes queued rollout writes first.
        replayed = replay(self._session_log(session_id))
        if not replayed.messages:
            return
        try:
            role.state.context.messages[:] = replayed.messages
        except AttributeError:
            logger.debug(f"ResidencyStore: role for {session_id} has no context.messages; skip refill")

    def forget(self, session_id: str) -> None:
        """Delete a materialized record (e.g. after a successful rehydrate)."""
        path = self._path(session_id)
        if path.exists():
            try:
                path.unlink()
            except OSError as exc:
                logger.warning(f"ResidencyStore: failed to forget {session_id}: {exc}")


__all__ = ["ResidencyRecord", "ResidencyStore"]
