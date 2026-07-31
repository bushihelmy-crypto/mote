"""InferenceRuntime adapter for the authenticated Shared Process gRPC client."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from mote.contracts.inference.attempt import InferenceAttemptRequest
from mote.contracts.inference.events import AttemptLifecycleEvent, SessionLifecycleEvent
from mote.contracts.inference.executions import BoundExecutionRequest, SessionApplicationMessage, TransferPartRequest
from mote.contracts.inference.wire_permit import WirePermit
from mote.product.inference.daemon.client_port import SharedRuntimeClient
from mote.product.inference.daemon.grpc_client import pb

SharedClient = SharedRuntimeClient[object, object, object]


class SharedInferenceRuntime:
    def __init__(self, client: SharedClient, *, owns_client: bool = True) -> None:
        self._client = client
        self._owns_client = owns_client
        self._executions: dict[str, _SharedAttemptExecution] = {}
        self._draining = False
        self._idle = asyncio.Event()
        self._idle.set()

    async def start_attempt(self, request: InferenceAttemptRequest) -> "_SharedAttemptExecution":
        if self._draining:
            raise RuntimeError("Shared inference runtime is draining")
        existing = self._executions.get(request.attempt_id)
        if existing is not None:
            if existing.request != request:
                raise ValueError("attempt id reused with a different Shared request")
            return existing
        response = await self._client.start_unary(
            pb.StartRequest(
                envelope=self._client.envelope(
                    generation_id=request.generation_id,
                    generation_artifact_digest=request.generation_artifact_digest,
                    idempotency_key=request.attempt_id,
                    deadline_utc=request.deadline.deadline_utc.isoformat(),
                    remaining_seconds_at_send=(request.deadline.remaining_seconds_at_send),
                    sent_at_utc=request.deadline.sent_at_utc.isoformat(),
                ),
                execution_id=request.attempt_id,
                operation=str(request.invocation.get("operation", "generate")),
                canonical_request=request.model_dump_json().encode(),
                artifact_reference=request.artifact_reference or "",
            ),
            timeout=request.deadline.remaining_seconds_at_send,
        )
        if response.execution_id != request.attempt_id:
            raise RuntimeError("Shared daemon changed attempt identity")
        execution = _SharedAttemptExecution(
            self._client,
            request,
            receipt_revision=response.receipt_revision,
            on_terminal=self._execution_terminal,
        )
        self._executions[request.attempt_id] = execution
        self._idle.clear()
        return execution

    async def drain(self, *, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("drain timeout must be positive")
        self._draining = True
        try:
            async with asyncio.timeout(timeout_seconds):
                await self._idle.wait()
        except TimeoutError as exc:
            raise TimeoutError("Shared inference runtime drain timed out") from exc

    async def aclose(self) -> None:
        self._draining = True
        if self._owns_client:
            await self._client.close()

    def _execution_terminal(self, attempt_id: str) -> None:
        self._executions.pop(attempt_id, None)
        if not self._executions:
            self._idle.set()


class _SharedAttemptExecution:
    def __init__(
        self,
        client: SharedClient,
        request: InferenceAttemptRequest,
        *,
        receipt_revision: int,
        on_terminal,
    ) -> None:
        self._client = client
        self.request = request
        self._receipt_revision = receipt_revision
        self._cursor = 0
        self._iterator: AsyncIterator[AttemptLifecycleEvent] | None = None
        self._on_terminal = on_terminal
        self._terminal_seen = False

    def __aiter__(self) -> "_SharedAttemptExecution":
        return self

    async def __anext__(self) -> AttemptLifecycleEvent:
        if self._iterator is None:
            self._iterator = self._events()
        return await anext(self._iterator)

    async def authorize_wire(self, permit: WirePermit) -> None:
        await self._client.authorize_wire(
            self.request.attempt_id,
            permit.model_dump_json().encode(),
            generation_id=self.request.generation_id,
            timeout=self.request.deadline.remaining_seconds_at_send,
        )

    async def cancel(self, reason: str) -> None:
        await self._client.cancel(
            self.request.attempt_id,
            reason,
            generation_id=self.request.generation_id,
            timeout=self.request.deadline.remaining_seconds_at_send,
        )

    async def _events(self) -> AsyncIterator[AttemptLifecycleEvent]:
        async for raw in self._client.resume_events(
            self.request.attempt_id,
            generation_id=self.request.generation_id,
            after_sequence=self._cursor,
            receipt_revision=self._receipt_revision,
            timeout=self.request.deadline.remaining_seconds_at_send,
        ):
            event = AttemptLifecycleEvent.model_validate_json(raw.payload)
            if (
                event.attempt_id != raw.execution_id
                or event.sequence != raw.sequence
                or event.receipt_revision != raw.receipt_revision
                or event.event_type.value != raw.event_type
                or event.generation_id != self.request.generation_id
            ):
                raise RuntimeError("Shared lifecycle envelope disagrees with event")
            self._cursor = event.sequence
            self._receipt_revision = max(self._receipt_revision, event.receipt_revision)
            if event.terminal and not self._terminal_seen:
                self._terminal_seen = True
                self._on_terminal(self.request.attempt_id)
            yield event


class SharedServiceCommandRuntime:
    """Durable command adapter over the Shared execution RPC contract."""

    def __init__(self, client: SharedClient, *, owns_client: bool = True) -> None:
        self._client = client
        self._owns_client = owns_client
        self._executions: dict[str, _SharedCommandExecution] = {}
        self._draining = False
        self._idle = asyncio.Event()
        self._idle.set()

    async def start_command(self, request: BoundExecutionRequest) -> "_SharedCommandExecution":
        return await self._start(request, transfer=False)

    async def _start(self, request: BoundExecutionRequest, *, transfer: bool) -> "_SharedCommandExecution":
        if self._draining:
            raise RuntimeError("Shared command runtime is draining")
        existing = self._executions.get(request.execution_id)
        if existing is not None:
            if existing.request != request:
                raise ValueError("execution id reused with a different Shared request")
            return existing
        start = pb.StartRequest(
            envelope=self._client.envelope(
                generation_id=request.generation_id,
                generation_artifact_digest=request.generation_artifact_digest,
                idempotency_key=request.execution_id,
                deadline_utc=request.deadline.deadline_utc.isoformat(),
                remaining_seconds_at_send=request.deadline.remaining_seconds_at_send,
                sent_at_utc=request.deadline.sent_at_utc.isoformat(),
            ),
            execution_id=request.execution_id,
            operation=request.operation,
            canonical_request=request.model_dump_json().encode(),
        )
        if transfer:
            assert isinstance(request, TransferPartRequest)
            response = await self._client.execute_transfer_part(
                pb.TransferPartRequest(
                    start=start,
                    part_number=request.part_number,
                    offset=request.offset,
                    length=request.length,
                    content_digest=request.content_digest,
                ),
                timeout=request.deadline.remaining_seconds_at_send,
            )
        else:
            response = await self._client.start_durable_command(
                start, timeout=request.deadline.remaining_seconds_at_send
            )
        if response.execution_id != request.execution_id:
            raise RuntimeError("Shared daemon changed execution identity")
        execution = _SharedCommandExecution(
            self._client,
            request,
            receipt_revision=response.receipt_revision,
            on_terminal=self._execution_terminal,
        )
        self._executions[request.execution_id] = execution
        self._idle.clear()
        return execution

    async def drain(self, *, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("drain timeout must be positive")
        self._draining = True
        try:
            async with asyncio.timeout(timeout_seconds):
                await self._idle.wait()
        except TimeoutError as exc:
            raise TimeoutError("Shared command runtime drain timed out") from exc

    async def aclose(self) -> None:
        self._draining = True
        if self._owns_client:
            await self._client.close()

    def _execution_terminal(self, execution_id: str) -> None:
        self._executions.pop(execution_id, None)
        if not self._executions:
            self._idle.set()


class SharedArtifactTransferRuntime(SharedServiceCommandRuntime):
    async def execute_part(self, request: TransferPartRequest) -> "_SharedCommandExecution":
        return await self._start(request, transfer=True)


class _SharedCommandExecution:
    def __init__(self, client, request, *, receipt_revision, on_terminal) -> None:
        self._client = client
        self.request = request
        self._receipt_revision = receipt_revision
        self._cursor = 0
        self._iterator: AsyncIterator[AttemptLifecycleEvent] | None = None
        self._on_terminal = on_terminal
        self._terminal_seen = False

    def __aiter__(self) -> "_SharedCommandExecution":
        return self

    async def __anext__(self) -> AttemptLifecycleEvent:
        if self._iterator is None:
            self._iterator = self._events()
        return await anext(self._iterator)

    async def authorize_wire(self, permit: WirePermit) -> None:
        await self._client.authorize_wire(
            self.request.execution_id,
            permit.model_dump_json().encode(),
            generation_id=self.request.generation_id,
            timeout=self.request.deadline.remaining_seconds_at_send,
        )

    async def cancel(self, reason: str) -> None:
        await self._client.cancel(
            self.request.execution_id,
            reason,
            generation_id=self.request.generation_id,
            timeout=self.request.deadline.remaining_seconds_at_send,
        )

    async def _events(self) -> AsyncIterator[AttemptLifecycleEvent]:
        async for raw in self._client.resume_events(
            self.request.execution_id,
            generation_id=self.request.generation_id,
            after_sequence=self._cursor,
            receipt_revision=self._receipt_revision,
            timeout=self.request.deadline.remaining_seconds_at_send,
        ):
            event = AttemptLifecycleEvent.model_validate_json(raw.payload)
            if (
                event.attempt_id != raw.execution_id
                or event.sequence != raw.sequence
                or event.receipt_revision != raw.receipt_revision
                or event.event_type.value != raw.event_type
                or event.generation_id != self.request.generation_id
            ):
                raise RuntimeError("Shared command lifecycle envelope disagrees with event")
            self._cursor = event.sequence
            self._receipt_revision = max(self._receipt_revision, event.receipt_revision)
            if event.terminal and not self._terminal_seen:
                self._terminal_seen = True
                self._on_terminal(self.request.execution_id)
            yield event


class SharedSessionRuntime:
    """SessionRuntime adapter over one authenticated Shared gRPC client."""

    def __init__(self, client: SharedClient, *, owns_client: bool = True) -> None:
        self._client = client
        self._owns_client = owns_client
        self._executions: dict[str, _SharedSessionExecution] = {}
        self._draining = False
        self._idle = asyncio.Event()
        self._idle.set()

    async def open(self, request: BoundExecutionRequest) -> "_SharedSessionExecution":
        if self._draining:
            raise RuntimeError("Shared session runtime is draining")
        existing = self._executions.get(request.execution_id)
        if existing is not None:
            if existing.request != request:
                raise ValueError("session id reused with a different Shared request")
            return existing
        response = await self._client.open_session(
            pb.StartRequest(
                envelope=self._client.envelope(
                    generation_id=request.generation_id,
                    generation_artifact_digest=request.generation_artifact_digest,
                    idempotency_key=request.execution_id,
                    deadline_utc=request.deadline.deadline_utc.isoformat(),
                    remaining_seconds_at_send=request.deadline.remaining_seconds_at_send,
                    sent_at_utc=request.deadline.sent_at_utc.isoformat(),
                ),
                execution_id=request.execution_id,
                operation=request.operation,
                canonical_request=request.model_dump_json().encode(),
            ),
            timeout=request.deadline.remaining_seconds_at_send,
        )
        if response.execution_id != request.execution_id:
            raise RuntimeError("Shared daemon changed session identity")
        execution = _SharedSessionExecution(
            self._client,
            request,
            receipt_revision=response.receipt_revision,
            on_terminal=self._execution_terminal,
        )
        self._executions[request.execution_id] = execution
        self._idle.clear()
        return execution

    async def drain(self, *, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("drain timeout must be positive")
        self._draining = True
        try:
            async with asyncio.timeout(timeout_seconds):
                await self._idle.wait()
        except TimeoutError as exc:
            raise TimeoutError("Shared session runtime drain timed out") from exc

    async def aclose(self) -> None:
        self._draining = True
        for execution in tuple(self._executions.values()):
            await execution.close("Shared session runtime closing")
        if self._owns_client:
            await self._client.close()

    def _execution_terminal(self, session_id: str) -> None:
        self._executions.pop(session_id, None)
        if not self._executions:
            self._idle.set()


class _SharedSessionExecution:
    def __init__(
        self,
        client: SharedClient,
        request: BoundExecutionRequest,
        *,
        receipt_revision: int,
        on_terminal,
    ) -> None:
        self._client = client
        self.request = request
        self._receipt_revision = receipt_revision
        self._cursor = 0
        self._initial_events: AsyncIterator[SessionLifecycleEvent] | None = None
        self._stream_events: AsyncIterator[SessionLifecycleEvent] | None = None
        self._requests: asyncio.Queue[object | None] = asyncio.Queue(maxsize=256)
        self._stream_started = False
        self._on_terminal = on_terminal
        self._terminal_seen = False

    def __aiter__(self) -> "_SharedSessionExecution":
        return self

    async def __anext__(self) -> SessionLifecycleEvent:
        iterator = self._stream_events if self._stream_started else self._initial_events
        if iterator is None:
            iterator = self._stream_event_source() if self._stream_started else self._initial_event_source()
            if self._stream_started:
                self._stream_events = iterator
            else:
                self._initial_events = iterator
        return await anext(iterator)

    async def authorize_open(self, permit: WirePermit) -> None:
        await self._client.authorize_wire(
            self.request.execution_id,
            permit.model_dump_json().encode(),
            generation_id=self.request.generation_id,
            timeout=self.request.deadline.remaining_seconds_at_send,
        )

    async def send(self, message: SessionApplicationMessage, permit: WirePermit) -> None:
        if message.session_id != self.request.execution_id:
            raise ValueError("Shared session message changed session identity")
        if not self._stream_started:
            self._stream_started = True
            self._stream_events = self._stream_event_source()
        await self._requests.put(
            pb.SessionMessage(
                envelope=self._client.envelope(
                    generation_id=self.request.generation_id,
                    generation_artifact_digest=self.request.generation_artifact_digest,
                    idempotency_key=(f"session:{message.session_id}:{message.sequence}"),
                ),
                execution_id=message.session_id,
                application_sequence=message.sequence,
                payload=message.model_dump_json().encode(),
                wire_permit=permit.model_dump_json().encode(),
            )
        )

    async def close(self, reason: str) -> None:
        if not reason:
            raise ValueError("session close reason is required")
        await self._client.cancel(
            self.request.execution_id,
            reason,
            generation_id=self.request.generation_id,
            timeout=self.request.deadline.remaining_seconds_at_send,
        )
        if self._stream_started:
            await self._requests.put(None)

    async def _request_source(self):
        while True:
            request = await self._requests.get()
            if request is None:
                return
            yield request

    async def _initial_event_source(self) -> AsyncIterator[SessionLifecycleEvent]:
        async for raw in self._client.resume_events(
            self.request.execution_id,
            generation_id=self.request.generation_id,
            after_sequence=self._cursor,
            receipt_revision=self._receipt_revision,
            timeout=self.request.deadline.remaining_seconds_at_send,
        ):
            event = self._validated_event(raw)
            yield event
            if event.event_type.value == "opened":
                return

    async def _stream_event_source(self) -> AsyncIterator[SessionLifecycleEvent]:
        async for raw in self._client.session(self._request_source()):
            event = self._validated_event(raw)
            yield event

    def _validated_event(self, raw) -> SessionLifecycleEvent:
        event = SessionLifecycleEvent.model_validate_json(raw.payload)
        if (
            event.session_id != raw.execution_id
            or event.sequence != raw.sequence
            or event.receipt_revision != raw.receipt_revision
            or event.event_type.value != raw.event_type
            or event.generation_id != self.request.generation_id
            or event.sequence <= self._cursor
        ):
            raise RuntimeError("Shared session lifecycle envelope disagrees with event")
        self._cursor = event.sequence
        self._receipt_revision = max(self._receipt_revision, event.receipt_revision)
        if event.terminal and not self._terminal_seen:
            self._terminal_seen = True
            self._on_terminal(self.request.execution_id)
        return event


__all__ = [
    "SharedArtifactTransferRuntime",
    "SharedInferenceRuntime",
    "SharedServiceCommandRuntime",
    "SharedSessionRuntime",
]
