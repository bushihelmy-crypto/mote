import asyncio
from pathlib import Path
from types import SimpleNamespace

import grpc

from mote.product.inference.daemon.reconnecting_client import ReconnectingSharedGrpcClient


class _TransportFailure(grpc.aio.AioRpcError):
    def __init__(self):
        Exception.__init__(self)


class _Supervisor:
    def __init__(self):
        self.generation = "one"

    def discover_ready_socket(self):
        return (SimpleNamespace(socket_generation=self.generation), Path(f"/tmp/gateway-{self.generation}.sock"))


class _Authenticator:
    def handshake(self, socket_generation):
        return SimpleNamespace(socket_generation=socket_generation)


class _Client:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.authenticated = None
        self.closed = 0
        self.start_calls = 0
        self.resume_calls = []

    async def authenticate(self, handshake):
        self.authenticated = handshake.socket_generation
        return SimpleNamespace(socket_generation=handshake.socket_generation)

    async def resume_events(self, execution_id, **values):
        self.resume_calls.append((execution_id, values))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        for event in outcome:
            if isinstance(event, tuple):
                yield SimpleNamespace(
                    execution_id=execution_id,
                    sequence=event[0],
                    receipt_revision=event[1],
                    event_type="chunk",
                    payload=b"",
                )
            else:
                yield event

    async def query_receipt(self, execution_id, **values):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def start_unary(self, request, *, timeout=None):
        self.start_calls += 1
        return SimpleNamespace(execution_id="unexpected", receipt_revision=1)

    async def close(self):
        self.closed += 1

    def envelope(self, **fields):
        return SimpleNamespace(**fields)

    def permit_issuer(self):
        return SimpleNamespace()

    async def stage_generation(self, artifact, *, generation_id, artifact_digest):
        return SimpleNamespace(generation_id=generation_id, artifact_digest=artifact_digest, state="ready")

    async def start_durable_command(self, request, *, timeout=None):
        return await self.start_unary(request, timeout=timeout)

    async def open_session(self, request, *, timeout=None):
        return await self.start_unary(request, timeout=timeout)

    async def execute_transfer_part(self, request, *, timeout=None):
        return await self.start_unary(request, timeout=timeout)

    async def authorize_wire(self, execution_id, wire_permit, *, generation_id, timeout=None):
        return None

    async def cancel(self, execution_id, reason, *, generation_id, timeout=None):
        return None

    def session(self, requests):
        async def empty():
            if False:
                yield None

        return empty()


def test_rebind_resumes_cursor_without_replaying_business_start():
    async def scenario():
        supervisor = _Supervisor()
        first = _Client([_TransportFailure()])
        second = _Client([[(8, 12)]])
        clients = {"one": first, "two": second}
        manager = ReconnectingSharedGrpcClient(
            supervisor,
            _Authenticator(),
            lambda socket_path: clients[socket_path.stem.removeprefix("gateway-")],
        )
        await manager.connect()
        supervisor.generation = "two"
        events = [
            event
            async for event in manager.resume_events(
                "execution",
                generation_id="model-generation",
                after_sequence=7,
                receipt_revision=11,
                timeout=5,
            )
        ]
        await manager.close()
        return events, first, second

    events, first, second = asyncio.run(scenario())
    assert [event.sequence for event in events] == [8]
    assert first.authenticated == "one"
    assert second.authenticated == "two"
    assert first.start_calls == second.start_calls == 0
    assert second.resume_calls[0][1]["after_sequence"] == 7
    assert second.resume_calls[0][1]["receipt_revision"] == 11
    assert first.closed == second.closed == 1


def test_rebind_continues_from_last_yielded_cursor():
    async def scenario():
        supervisor = _Supervisor()
        first = _Client([[(8, 12), _TransportFailure()]])
        second = _Client([[(9, 13)]])

        async def first_events(execution_id, **values):
            first.resume_calls.append((execution_id, values))
            yield SimpleNamespace(
                execution_id=execution_id,
                sequence=8,
                receipt_revision=12,
                event_type="chunk",
                payload=b"",
            )
            supervisor.generation = "two"
            raise _TransportFailure()

        first.resume_events = first_events
        clients = {"one": first, "two": second}
        manager = ReconnectingSharedGrpcClient(
            supervisor,
            _Authenticator(),
            lambda socket_path: clients[socket_path.stem.removeprefix("gateway-")],
        )
        await manager.connect()
        events = [
            event
            async for event in manager.resume_events(
                "execution",
                generation_id="model-generation",
                after_sequence=7,
                receipt_revision=11,
                timeout=5,
            )
        ]
        await manager.close()
        return events, second

    events, second = asyncio.run(scenario())
    assert [event.sequence for event in events] == [8, 9]
    assert second.resume_calls[0][1]["after_sequence"] == 8
    assert second.resume_calls[0][1]["receipt_revision"] == 12
