from __future__ import annotations

import asyncio

import pytest

from mote.contracts.conversation import UserMessage
from mote.contracts.model.inference import (
    FinalizedGenerateRequest,
    FinalizedInferenceRequest,
    InferenceAttemptFence,
    TargetInvalidated,
)
from mote.contracts.model.invocation import GenerateOutput
from mote.runtime.models import inference_port as inference_port_module
from mote.runtime.models.inference_port import RuntimeModelInferencePort, TargetCapacityError

from .conftest import FakeLLM


class _RuntimeLease:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


def _request(call_id: str = "call") -> FinalizedInferenceRequest:
    return FinalizedInferenceRequest(
        call_id,
        FinalizedGenerateRequest(messages=(UserMessage("hello"),), task="interactive"),
    )


def test_finalized_request_rejects_wrong_operation_payload() -> None:
    with pytest.raises(TypeError, match="only accepts a generate"):
        FinalizedInferenceRequest("call", object())  # type: ignore[reportArgumentType]
    with pytest.raises(TypeError, match="canonical Message"):
        FinalizedGenerateRequest(  # type: ignore[arg-type]
            messages=("hello",),
            task="interactive",
        )


@pytest.mark.asyncio
async def test_late_inference_attempt_is_fenced(monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def generate(_route, _request, **_payload):
        entered.set()
        await release.wait()
        return GenerateOutput(content="old"), None

    monkeypatch.setattr(inference_port_module, "generate_finalized", generate)
    port = RuntimeModelInferencePort()
    llm = FakeLLM()
    target = port.pin_route(llm.route)
    request = _request()
    old = asyncio.create_task(port.infer(target, request, InferenceAttemptFence("call", "old", 1)))
    await entered.wait()
    newer = asyncio.create_task(port.infer(target, request, InferenceAttemptFence("call", "new", 2)))
    release.set()
    old_result, new_result = await asyncio.gather(old, newer)

    assert isinstance(old_result, TargetInvalidated)
    assert new_result.content == "old"


@pytest.mark.asyncio
async def test_same_attempt_returns_cached_result(monkeypatch):
    calls = 0

    async def generate(_route, _request, **_payload):
        nonlocal calls
        calls += 1
        return GenerateOutput(content="done"), None

    monkeypatch.setattr(inference_port_module, "generate_finalized", generate)
    port = RuntimeModelInferencePort()
    llm = FakeLLM()
    target = port.pin_route(llm.route)
    request = _request()
    attempt = InferenceAttemptFence("call", "attempt", 1)

    first = await port.infer(target, request, attempt)
    second = await port.infer(target, request, attempt)

    assert second is first
    assert calls == 1


@pytest.mark.asyncio
async def test_concurrent_same_attempt_shares_one_provider_call(monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def generate(_route, _request, **_payload):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return GenerateOutput(content="done"), None

    monkeypatch.setattr(inference_port_module, "generate_finalized", generate)
    port = RuntimeModelInferencePort()
    target = port.pin_route(FakeLLM().route)
    request = _request()
    attempt = InferenceAttemptFence("call", "attempt", 1)
    first = asyncio.create_task(port.infer(target, request, attempt))
    await entered.wait()
    second = asyncio.create_task(port.infer(target, request, attempt))
    await asyncio.sleep(0)
    release.set()

    first_result, second_result = await asyncio.gather(first, second)
    assert first_result is second_result
    assert calls == 1


@pytest.mark.asyncio
async def test_attempt_and_request_identity_must_match(monkeypatch):
    async def fail_generate(_route, _request, **_payload):
        raise AssertionError("provider must not be called")

    monkeypatch.setattr(inference_port_module, "generate_finalized", fail_generate)
    port = RuntimeModelInferencePort()
    target = port.pin_route(FakeLLM().route)
    result = await port.infer(
        target,
        _request("request-call"),
        InferenceAttemptFence("attempt-call", "attempt", 1),
    )

    assert isinstance(result, TargetInvalidated)
    assert "identity mismatch" in result.reason


@pytest.mark.asyncio
async def test_release_waits_for_active_inference_before_closing_lease(monkeypatch):
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def generate(_route, _request, **_payload):
        entered.set()
        await finish.wait()
        return GenerateOutput(content="done"), None

    monkeypatch.setattr(inference_port_module, "generate_finalized", generate)
    port = RuntimeModelInferencePort()
    lease = _RuntimeLease()
    target = port.pin_route(FakeLLM().route, runtime_lease=lease)
    inference = asyncio.create_task(
        port.infer(
            target,
            _request(),
            InferenceAttemptFence("call", "attempt", 1),
        )
    )
    await entered.wait()
    release = asyncio.create_task(port.release(target))
    await asyncio.sleep(0)

    assert not lease.closed
    assert not release.done()

    finish.set()
    await inference
    await release
    assert lease.closed


@pytest.mark.asyncio
async def test_release_ready_target_is_idempotent():
    port = RuntimeModelInferencePort()
    lease = _RuntimeLease()
    target = port.pin_route(FakeLLM().route, runtime_lease=lease)

    await port.release(target)
    await port.release(target)

    assert lease.closed


@pytest.mark.asyncio
async def test_target_capacity_is_bounded():
    port = RuntimeModelInferencePort()
    port._TARGET_CAPACITY = 1
    target = port.pin_route(FakeLLM().route)

    with pytest.raises(TargetCapacityError):
        port.pin_route(FakeLLM().route)

    await port.release(target)


@pytest.mark.asyncio
async def test_expired_ready_target_closes_its_runtime_lease():
    port = RuntimeModelInferencePort()
    port._TARGET_TTL_SECONDS = 0
    lease = _RuntimeLease()
    target = port.pin_route(FakeLLM().route, runtime_lease=lease)

    result = await port.infer(
        target,
        _request(),
        InferenceAttemptFence("call", "attempt", 1),
    )

    assert isinstance(result, TargetInvalidated)
    assert lease.closed
