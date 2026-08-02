"""Owner-bound, process-local rendezvous for AG-UI human prompts."""

from __future__ import annotations

import asyncio
import hmac
import secrets
import time
import uuid
from dataclasses import dataclass
from enum import Enum

from mote.contracts.events.envelope import JsonValue, freeze_json


class PromptKind(str, Enum):
    QUESTION = "question"
    APPROVAL = "approval"


@dataclass(frozen=True)
class PromptScope:
    principal: str
    agent_id: str
    thread_id: str
    run_id: str
    kind: PromptKind


@dataclass(frozen=True)
class PromptHandle:
    prompt_id: str
    nonce: str
    scope: PromptScope


class PromptResolveDisposition(str, Enum):
    RESOLVED = "resolved"
    STALE = "stale"
    FOREIGN = "foreign"
    WRONG_KIND = "wrong_kind"


@dataclass
class _PendingPrompt:
    handle: PromptHandle
    expires_at: float
    future: asyncio.Future[JsonValue]


class PromptBroker:
    """Prompt owner; correlation ids alone never authorize a reply."""

    def __init__(self) -> None:
        self._pending: dict[str, _PendingPrompt] = {}

    def open(self, scope: PromptScope, *, ttl_seconds: float) -> tuple[PromptHandle, asyncio.Future[JsonValue]]:
        prompt_id = f"{scope.kind.value}-{uuid.uuid4().hex}"
        handle = PromptHandle(prompt_id=prompt_id, nonce=secrets.token_urlsafe(24), scope=scope)
        future: asyncio.Future[JsonValue] = asyncio.get_running_loop().create_future()
        self._pending[prompt_id] = _PendingPrompt(
            handle=handle,
            expires_at=time.monotonic() + ttl_seconds,
            future=future,
        )
        return handle, future

    def resolve(self, handle: PromptHandle, payload: JsonValue) -> PromptResolveDisposition:
        pending = self._pending.get(handle.prompt_id)
        if pending is None or pending.expires_at <= time.monotonic():
            self.discard(handle.prompt_id)
            return PromptResolveDisposition.STALE
        expected = pending.handle
        if expected.scope.kind is not handle.scope.kind:
            return PromptResolveDisposition.WRONG_KIND
        if expected.scope != handle.scope or not hmac.compare_digest(expected.nonce, handle.nonce):
            return PromptResolveDisposition.FOREIGN
        self._pending.pop(handle.prompt_id)
        if pending.future.done():
            return PromptResolveDisposition.STALE
        pending.future.set_result(freeze_json(payload, path="prompt_reply"))
        return PromptResolveDisposition.RESOLVED

    def discard(self, prompt_id: str) -> None:
        pending = self._pending.pop(prompt_id, None)
        if pending is not None and not pending.future.done():
            pending.future.cancel()

    def cancel_scope(self, scope: PromptScope) -> None:
        for prompt_id, pending in tuple(self._pending.items()):
            if pending.handle.scope == scope:
                self.discard(prompt_id)

    def cancel_thread(self, *, principal: str, thread_id: str) -> None:
        for prompt_id, pending in tuple(self._pending.items()):
            scope = pending.handle.scope
            if scope.principal == principal and scope.thread_id == thread_id:
                self.discard(prompt_id)

    def cancel_all(self, reason: str | None = None) -> None:
        for prompt_id in tuple(self._pending):
            self.discard(prompt_id)

    @property
    def pending_ids(self) -> list[str]:
        return list(self._pending)


__all__ = [
    "PromptBroker",
    "PromptHandle",
    "PromptKind",
    "PromptResolveDisposition",
    "PromptScope",
]
