#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ResidencyStore — materialize/rehydrate evicted agents to/from disk.

Port of codex ``rollout materialize`` + load-on-demand. When residency evicts an
idle agent, the control plane must persist *everything* not already covered by
``RoleState`` serialization — namely the ``msg_buffer`` (``exclude=True`` on
RoleState) and the per-runtime ``Mailbox`` — or the unload would silently drop
messages. A :class:`ResidencyRecord` bundles the three pieces:

    {role_dump, mailbox_dump, msg_buffer_dump}

Note ``MessageQueue.dump()`` is **async**, so :meth:`materialize` is async.
``ResidencyRecord`` is a plain JSON dict on disk (NOT a ``BaseSerialization``
with ``extra="forbid"``) so it stays forgiving across schema tweaks.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from metagpt.common.logs import logger
from metagpt.common.schema import MessageQueue
from metagpt.environment.mailbox import Mailbox
from metagpt.environment.runtime import AgentRuntime


@dataclass
class ResidencyRecord:
    """The on-disk shape of an unloaded agent."""

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


class ResidencyStore:
    """Reads/writes :class:`ResidencyRecord` files keyed by ``session_id``."""

    def __init__(self, base_dir: Optional[str] = None):
        if base_dir is None:
            from metagpt.common.const import DEFAULT_WORKSPACE_ROOT

            base_dir = str(Path(DEFAULT_WORKSPACE_ROOT) / ".agent_residency")
        self._base = Path(base_dir)

    def _path(self, session_id: str) -> Path:
        return self._base / f"{session_id}.json"

    def has(self, session_id: str) -> bool:
        return self._path(session_id).exists()

    # ------------------------------------------------------------------
    # Materialize (write)
    # ------------------------------------------------------------------
    async def materialize(self, runtime: AgentRuntime) -> ResidencyRecord:
        """Persist *runtime* to disk and return the written record."""
        record = ResidencyRecord(
            role_dump=runtime.role.dump(),
            mailbox_dump=runtime.mailbox.dump(),
            msg_buffer_dump=await runtime.msg_buffer.dump(),
        )
        self._base.mkdir(parents=True, exist_ok=True)
        self._path(runtime.session_id).write_text(record.to_json(), encoding="utf-8")
        logger.info(f"ResidencyStore: materialized agent {runtime.session_id}")
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
        polymorphic ``BaseRole.load``); tests inject a fake loader.
        """
        record = self.read_record(session_id)
        if record is None:
            return None
        loader = role_loader or _default_role_loader
        role = loader(record.role_dump)
        # The msg_buffer is excluded from RoleState serialization, so restore it
        # explicitly from the record (else unload = silent message loss).
        role.state.msg_buffer = MessageQueue.load(record.msg_buffer_dump)
        mailbox = Mailbox.load(record.mailbox_dump)
        return AgentRuntime(role, mailbox)

    def forget(self, session_id: str) -> None:
        """Delete a materialized record (e.g. after a successful rehydrate)."""
        path = self._path(session_id)
        if path.exists():
            try:
                path.unlink()
            except OSError as exc:
                logger.warning(f"ResidencyStore: failed to forget {session_id}: {exc}")


__all__ = ["ResidencyRecord", "ResidencyStore"]
