"""Shared RPC adapter over the four Embedded inference runtime facades."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from mote.contracts.inference.attempt import InferenceAttemptRequest
from mote.contracts.inference.events import AttemptLifecycleEvent, SessionLifecycleEvent
from mote.contracts.inference.executions import BoundExecutionRequest, SessionApplicationMessage, TransferPartRequest
from mote.contracts.inference.persisted_event import PersistedLifecycleEvent
from mote.contracts.inference.shared import SharedSessionCredential
from mote.contracts.inference.wire_permit import WirePermit
from mote.contracts.ports.inference.attempt_receipt import AttemptReceiptStore
from mote.contracts.ports.inference.lifecycle_event import LifecycleEventStore
from mote.contracts.ports.inference.session_receipt import SessionReceiptStore
from mote.product.inference.daemon.rpc import gateway_v1_pb2 as _pb
from mote.runtime.inference.command_runtime import EmbeddedServiceCommandRuntime
from mote.runtime.inference.runtime import EmbeddedInferenceRuntime
from mote.runtime.inference.session_runtime import EmbeddedSessionRuntime
from mote.runtime.inference.transfer_runtime import EmbeddedArtifactTransferRuntime

pb: Any = _pb


@dataclass(slots=True)
class _EventJournal:
    events: list[Any] = field(default_factory=list)
    terminal: bool = False
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)

    async def append(self, event: Any) -> None:
        async with self.condition:
            self.events.append(event)
            self.terminal = event.terminal
            self.condition.notify_all()


class SharedEmbeddedExecutionBackend:
    """Thin deployment adapter; it owns no routing, retry, budget, or wire policy."""

    def __init__(
        self,
        *,
        unary: EmbeddedInferenceRuntime,
        commands: EmbeddedServiceCommandRuntime,
        sessions: EmbeddedSessionRuntime,
        transfers: EmbeddedArtifactTransferRuntime,
        receipts: AttemptReceiptStore,
        session_receipts: SessionReceiptStore,
        events: LifecycleEventStore,
    ) -> None:
        self._unary = unary
        self._commands = commands
        self._sessions = sessions
        self._transfers = transfers
        self._receipts = receipts
        self._session_receipts = session_receipts
        self._event_store = events
        self._executions: dict[str, Any] = {}
        self._journals: dict[str, _EventJournal] = {}
        self._pumps: set[asyncio.Task[None]] = set()

    async def start_unary(self, request: Any, credential: SharedSessionCredential) -> int:
        canonical = InferenceAttemptRequest.model_validate_json(request.canonical_request)
        self._validate_start(request, canonical, credential, canonical.attempt_id)
        execution = await self._unary.start_attempt(canonical)
        return await self._register(
            canonical.attempt_id,
            canonical.generation_id,
            execution,
            session=False,
        )

    async def start_durable_command(self, request: Any, credential: SharedSessionCredential) -> int:
        canonical = BoundExecutionRequest.model_validate_json(request.canonical_request)
        self._validate_start(request, canonical, credential, canonical.execution_id)
        execution = await self._commands.start_command(canonical)
        return await self._register(
            canonical.execution_id,
            canonical.generation_id,
            execution,
            session=False,
        )

    async def open_session(self, request: Any, credential: SharedSessionCredential) -> int:
        canonical = BoundExecutionRequest.model_validate_json(request.canonical_request)
        self._validate_start(request, canonical, credential, canonical.execution_id)
        execution = await self._sessions.open(canonical)
        return await self._register(
            canonical.execution_id,
            canonical.generation_id,
            execution,
            session=True,
        )

    async def execute_transfer_part(self, request: Any, credential: SharedSessionCredential) -> int:
        canonical = TransferPartRequest.model_validate_json(request.start.canonical_request)
        self._validate_start(request.start, canonical, credential, canonical.execution_id)
        if (
            request.part_number != canonical.part_number
            or request.offset != canonical.offset
            or request.length != canonical.length
            or request.content_digest != canonical.content_digest
        ):
            raise ValueError("transfer RPC fields disagree with canonical request")
        execution = await self._transfers.execute_part(canonical)
        return await self._register(
            canonical.execution_id,
            canonical.generation_id,
            execution,
            session=False,
        )

    async def authorize(self, request: Any, credential: SharedSessionCredential) -> None:
        execution = self._require_execution(request.execution_id)
        permit = WirePermit.model_validate_json(request.wire_permit)
        if hasattr(execution, "authorize_open"):
            await execution.authorize_open(permit)
        else:
            await execution.authorize_wire(permit)

    async def cancel(self, request: Any, credential: SharedSessionCredential) -> None:
        execution = self._require_execution(request.execution_id)
        if hasattr(execution, "close"):
            await execution.close(request.reason)
        else:
            await execution.cancel(request.reason)

    async def stream_events(self, request: Any, credential: SharedSessionCredential) -> AsyncIterator[Any]:
        journal = self._journals.get(request.execution_id)
        cursor = request.after_sequence
        if journal is None:
            found = False
            while True:
                persisted = await self._event_store.read_events(request.execution_id, after_sequence=cursor)
                if not persisted:
                    break
                found = True
                for event in persisted:
                    cursor = event.sequence
                    yield self._stored_rpc_event(event)
                if len(persisted) < 256 or persisted[-1].terminal:
                    break
            if not found:
                raise KeyError(f"unknown execution {request.execution_id}")
            return
        while True:
            async with journal.condition:
                available = [event for event in journal.events if event.sequence > cursor]
                if not available and not journal.terminal:
                    await journal.condition.wait()
                    continue
            for event in available:
                cursor = max(cursor, event.sequence)
                yield self._rpc_event(event)
            if journal.terminal:
                return

    async def query_receipt(self, request: Any, credential: SharedSessionCredential) -> Any:
        generation_id = request.envelope.generation_id
        if not generation_id:
            raise ValueError("receipt query requires generation id")
        session = await self._session_receipts.get(request.execution_id, generation_id)
        if session is not None:
            return pb.Receipt(
                execution_id=request.execution_id,
                revision=session.revision,
                state=session.state.value,
            )
        receipt = await self._receipts.get(request.execution_id, generation_id)
        if receipt is None:
            raise KeyError(f"unknown execution {request.execution_id}")
        return pb.Receipt(
            execution_id=request.execution_id,
            revision=receipt.revision,
            state=receipt.state.value,
            terminal_artifact_reference=receipt.terminal_artifact_reference or "",
        )

    def reconcile(self, request: Any, credential: SharedSessionCredential) -> AsyncIterator[Any]:
        return self.stream_events(request, credential)

    async def session(
        self,
        requests: AsyncIterator[Any],
        credential: SharedSessionCredential,
    ) -> AsyncIterator[Any]:
        execution_id: str | None = None

        async def consume() -> None:
            nonlocal execution_id
            async for request in requests:
                if execution_id is None:
                    execution_id = request.execution_id
                elif request.execution_id != execution_id:
                    raise ValueError("session stream changed execution id")
                execution = self._require_execution(request.execution_id)
                message = SessionApplicationMessage.model_validate_json(request.payload)
                permit = WirePermit.model_validate_json(request.wire_permit)
                await execution.send(message, permit)

        consumer = asyncio.create_task(consume())
        try:
            while execution_id is None and not consumer.done():
                await asyncio.sleep(0)
            if execution_id is None:
                await consumer
                return
            request = pb.CursorRequest(
                execution_id=execution_id,
                after_sequence=0,
            )
            async for event in self.stream_events(request, credential):
                yield event
            await consumer
        finally:
            if not consumer.done():
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)

    async def aclose(self) -> None:
        await self._unary.aclose()
        await self._commands.aclose()
        await self._sessions.aclose()
        await self._transfers.aclose()
        for task in tuple(self._pumps):
            task.cancel()
        await asyncio.gather(*self._pumps, return_exceptions=True)

    async def drain(self, *, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Shared backend drain timeout must be positive")
        async with asyncio.timeout(timeout_seconds):
            await asyncio.gather(
                self._unary.drain(timeout_seconds=timeout_seconds),
                self._commands.drain(timeout_seconds=timeout_seconds),
                self._sessions.drain(timeout_seconds=timeout_seconds),
                self._transfers.drain(timeout_seconds=timeout_seconds),
            )

    async def _register(
        self,
        execution_id: str,
        generation_id: str,
        execution: Any,
        *,
        session: bool,
    ) -> int:
        existing = self._executions.get(execution_id)
        if existing is not None and existing is not execution:
            raise ValueError("execution id is already registered")
        self._executions[execution_id] = execution
        if execution_id not in self._journals:
            journal = _EventJournal()
            self._journals[execution_id] = journal
            task = asyncio.create_task(self._pump(execution, journal))
            self._pumps.add(task)
            task.add_done_callback(self._pumps.discard)
        if session:
            receipt = await self._session_receipts.get(execution_id, generation_id)
        else:
            receipt = await self._receipts.get(execution_id, generation_id)
        if receipt is None:
            raise RuntimeError("runtime accepted execution without durable receipt")
        return receipt.revision

    async def _pump(self, execution: Any, journal: _EventJournal) -> None:
        async for event in execution:
            await self._event_store.append_event(self._persisted_event(event))
            await journal.append(event)

    def _validate_start(
        self,
        request: Any,
        canonical: Any,
        credential: SharedSessionCredential,
        execution_id: str,
    ) -> None:
        envelope = request.envelope
        if request.execution_id != execution_id:
            raise ValueError("RPC execution id disagrees with canonical request")
        if canonical.principal != credential.principal:
            raise PermissionError("canonical principal is not authenticated principal")
        if (
            envelope.generation_id != canonical.generation_id
            or envelope.generation_artifact_digest != canonical.generation_artifact_digest
        ):
            raise ValueError("RPC generation binding disagrees with canonical request")

    def _require_execution(self, execution_id: str) -> Any:
        try:
            return self._executions[execution_id]
        except KeyError as exc:
            raise KeyError(f"unknown execution {execution_id}") from exc

    def _require_journal(self, execution_id: str) -> _EventJournal:
        try:
            return self._journals[execution_id]
        except KeyError as exc:
            raise KeyError(f"unknown execution {execution_id}") from exc

    @staticmethod
    def _persisted_event(
        event: AttemptLifecycleEvent | SessionLifecycleEvent,
    ) -> PersistedLifecycleEvent:
        execution_id = getattr(event, "attempt_id", None) or getattr(event, "session_id")
        return PersistedLifecycleEvent(
            execution_id=execution_id,
            sequence=event.sequence,
            receipt_revision=event.receipt_revision,
            event_type=event.event_type.value,
            payload=event.model_dump_json().encode(),
            terminal=event.terminal,
        )

    @staticmethod
    def _stored_rpc_event(event: PersistedLifecycleEvent) -> Any:
        return pb.LifecycleEvent(
            execution_id=event.execution_id,
            sequence=event.sequence,
            receipt_revision=event.receipt_revision,
            event_type=event.event_type,
            payload=event.payload,
        )

    @staticmethod
    def _rpc_event(event: AttemptLifecycleEvent | SessionLifecycleEvent) -> Any:
        execution_id = getattr(event, "attempt_id", None) or getattr(event, "session_id")
        return pb.LifecycleEvent(
            execution_id=execution_id,
            sequence=event.sequence,
            receipt_revision=event.receipt_revision,
            event_type=event.event_type.value,
            payload=event.model_dump_json().encode(),
        )
