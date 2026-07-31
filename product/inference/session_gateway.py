"""Product owner for public realtime sessions over a SessionRuntime."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta, timezone
from typing import Protocol

from mote.contracts.inference.events import SessionEventType, SessionLifecycleEvent
from mote.contracts.inference.executions import BoundExecutionRequest, SessionApplicationMessage
from mote.contracts.inference.wire_permit import ExecutionTaxonomy
from mote.contracts.ports.inference.session_runtime import SessionExecution, SessionRuntime
from mote.contracts.ports.inference.wire_permit import WirePermitIssuer


class RealtimeSession(Protocol):
    def __aiter__(self) -> AsyncIterator[SessionLifecycleEvent]:
        ...

    async def send(self, *, sequence: int, message_type: str, payload: dict[str, object]) -> None:
        ...

    async def close(self, reason: str) -> None:
        ...


class RealtimeSessionOwner(Protocol):
    async def open(self, payload: dict[str, object]) -> RealtimeSession:
        ...


SessionRequestFactory = Callable[[dict[str, object]], BoundExecutionRequest]
EpochProvider = Callable[[], tuple[int, int]]


class RuntimeSessionGateway:
    """Own sequencing and authorization while delegating wire state to runtime."""

    def __init__(
        self,
        runtime: SessionRuntime,
        permit_issuer: WirePermitIssuer,
        request_factory: SessionRequestFactory,
        *,
        permit_audience: str,
        epoch_provider: EpochProvider,
        permit_lifetime_seconds: float = 30.0,
    ) -> None:
        if not permit_audience or permit_lifetime_seconds <= 0:
            raise ValueError("realtime permit configuration is invalid")
        self._runtime = runtime
        self._permit_issuer = permit_issuer
        self._request_factory = request_factory
        self._permit_audience = permit_audience
        self._epoch_provider = epoch_provider
        self._permit_lifetime_seconds = permit_lifetime_seconds

    async def open(self, payload: dict[str, object]) -> "_RuntimeRealtimeSession":
        request = self._request_factory(payload)
        execution = await self._runtime.open(request)
        session = _RuntimeRealtimeSession(
            execution,
            request,
            self._permit_issuer,
            permit_audience=self._permit_audience,
            epoch_provider=self._epoch_provider,
            permit_lifetime_seconds=self._permit_lifetime_seconds,
        )
        await session.authorize_and_wait_until_opened()
        return session


class _RuntimeRealtimeSession:
    def __init__(
        self,
        execution: SessionExecution,
        request: BoundExecutionRequest,
        permit_issuer: WirePermitIssuer,
        *,
        permit_audience: str,
        epoch_provider: EpochProvider,
        permit_lifetime_seconds: float,
    ) -> None:
        self._execution = execution
        self._events = execution.__aiter__()
        self._request = request
        self._permit_issuer = permit_issuer
        self._permit_audience = permit_audience
        self._epoch_provider = epoch_provider
        self._permit_lifetime_seconds = permit_lifetime_seconds
        self._initial_events: list[SessionLifecycleEvent] = []
        self._receipt_revision = 1
        self._next_sequence = 1
        self._terminal = False
        self._send_lock = asyncio.Lock()

    def __aiter__(self) -> "_RuntimeRealtimeSession":
        return self

    async def __anext__(self) -> SessionLifecycleEvent:
        if self._initial_events:
            return self._initial_events.pop(0)
        event = await anext(self._events)
        await self._observe(event)
        return event

    async def authorize_and_wait_until_opened(self) -> None:
        authorization_seen = False
        async for event in self._events:
            await self._observe(event)
            self._initial_events.append(event)
            if event.event_type is SessionEventType.OPEN_AUTHORIZATION_REQUIRED:
                if authorization_seen:
                    raise RuntimeError("runtime requested session authorization twice")
                authorization_seen = True
                await self._execution.authorize_open(
                    self._permit(
                        attempt_id=self._request.execution_id,
                        wire_unit=self._request.operation,
                        ordinal=1,
                    )
                )
            elif event.event_type is SessionEventType.OPENED:
                if not authorization_seen:
                    raise RuntimeError("runtime opened session without authorization")
                return
            elif event.terminal:
                raise RuntimeError(str(event.payload.get("reason", event.event_type.value)))
        raise RuntimeError("session runtime ended before opening")

    async def send(self, *, sequence: int, message_type: str, payload: dict[str, object]) -> None:
        if not message_type:
            raise ValueError("realtime message type is required")
        async with self._send_lock:
            if self._terminal:
                raise RuntimeError("realtime session is terminal")
            if sequence != self._next_sequence:
                raise ValueError("realtime message sequence is not next")
            message = SessionApplicationMessage(
                session_id=self._request.execution_id,
                sequence=sequence,
                message_type=message_type,
                payload=payload,
            )
            await self._execution.send(
                message,
                self._permit(
                    attempt_id=f"{self._request.execution_id}:{sequence}",
                    wire_unit=message_type,
                    ordinal=sequence + 1,
                ),
            )
            self._next_sequence += 1

    async def close(self, reason: str) -> None:
        await self._execution.close(reason)

    async def _observe(self, event: SessionLifecycleEvent) -> None:
        if event.session_id != self._request.execution_id:
            raise RuntimeError("session runtime changed session identity")
        self._receipt_revision = max(self._receipt_revision, event.receipt_revision)
        if event.event_type is SessionEventType.MESSAGE_SENT:
            sequence = event.payload.get("application_sequence")
            if not isinstance(sequence, int):
                raise RuntimeError("message receipt omitted application sequence")
        if event.terminal:
            self._terminal = True

    def _permit(self, *, attempt_id: str, wire_unit: str, ordinal: int):
        now = datetime.now(timezone.utc)
        backup_epoch, admission_epoch = self._epoch_provider()
        return self._permit_issuer.issue(
            attempt_id=attempt_id,
            execution_taxonomy=ExecutionTaxonomy.LONG_LIVED_SESSION,
            owner_journal_id=self._request.owner_journal_id,
            wire_unit=wire_unit,
            generation_id=self._request.generation_id,
            generation_artifact_digest=self._request.generation_artifact_digest,
            ordinal=ordinal,
            issued_journal_revision=self._receipt_revision,
            not_before=now,
            expires_at=now + timedelta(seconds=self._permit_lifetime_seconds),
            audience=self._permit_audience,
            backup_epoch=backup_epoch,
            admission_epoch=admission_epoch,
        )


__all__ = ["RealtimeSession", "RealtimeSessionOwner", "RuntimeSessionGateway"]
