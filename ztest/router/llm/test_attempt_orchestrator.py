"""Request-scoped AttemptOrchestrator policy and state tests."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from mote.contracts.models.failover import AttemptBudget, RequestTransform
from mote.runtime.errors import (
    ContextWindowExceededError,
    LLMAuthenticationError,
    LLMImageTooLargeError,
    LLMRateLimitError,
    LLMUnusableResponseError,
)
from mote.runtime.models.failover import AttemptOrchestrator


@dataclass(frozen=True)
class _Provider:
    model: str


@pytest.mark.asyncio
async def test_compress_replaces_request_state_before_next_attempt() -> None:
    orchestrator = AttemptOrchestrator(max_wire_attempts=2)
    provider = _Provider("primary")
    seen: list[list[dict]] = []

    async def execute(active, messages):
        seen.append(messages)
        if len(seen) == 1:
            raise ContextWindowExceededError("too large")
        return messages[0]["content"]

    async def transform(active, messages, kind, disposition, exc):
        assert active is provider
        assert kind is RequestTransform.COMPRESS
        return [{"role": "user", "content": "compressed"}]

    result = await orchestrator.run(
        execute_once=execute,
        primary=provider,
        request=[{"role": "user", "content": "original"}],
        request_transformer=transform,
    )

    assert result == "compressed"
    assert seen[0][0]["content"] == "original"
    assert seen[1][0]["content"] == "compressed"


@pytest.mark.asyncio
async def test_rotate_credential_switches_request_local_provider_reference() -> None:
    orchestrator = AttemptOrchestrator(max_wire_attempts=2)
    provider = _Provider("primary-key-a")
    rotated = _Provider("primary-key-b")
    calls = 0
    rotations = 0

    async def execute(active, messages):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise LLMAuthenticationError("bad key")
        return active.model

    def next_credential(active):
        nonlocal rotations
        assert active is provider
        rotations += 1
        return rotated

    result = await orchestrator.run(
        execute_once=execute,
        primary=provider,
        request=[],
        next_credential=next_credential,
    )

    assert result == "primary-key-b"
    assert calls == 2
    assert rotations == 1


@pytest.mark.asyncio
async def test_retry_honors_policy_retry_after(monkeypatch) -> None:
    sleeps = []

    async def capture_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(
        "mote.runtime.models.failover.orchestrator.asyncio.sleep",
        capture_sleep,
    )
    calls = 0

    async def execute(active, messages):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise LLMRateLimitError("slow down", retry_after=3.0)
        return active.model

    result = await AttemptOrchestrator(max_wire_attempts=2).run(
        execute_once=execute,
        primary=_Provider("primary"),
        request=[],
    )

    assert result == "primary"
    assert sleeps == [3.0]


@pytest.mark.asyncio
async def test_fallback_switches_only_current_call_state() -> None:
    orchestrator = AttemptOrchestrator(max_wire_attempts=2)
    primary = _Provider("primary")
    fallback = _Provider("fallback")

    async def execute(active, messages):
        if active is primary:
            raise LLMUnusableResponseError("refusal")
        return active.model

    def supplier_factory():
        yielded = False

        def supplier():
            nonlocal yielded
            if yielded:
                return None
            yielded = True
            return fallback

        return supplier

    result = await orchestrator.run(
        execute_once=execute,
        primary=primary,
        request=[],
        endpoint_selector_factory=supplier_factory,
    )

    assert result == "fallback"


@pytest.mark.asyncio
async def test_transform_replaces_request_state() -> None:
    orchestrator = AttemptOrchestrator(max_wire_attempts=2)
    provider = _Provider("primary")
    calls = 0

    async def execute(active, messages):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise LLMImageTooLargeError("too large")
        return messages[-1]["content"]

    async def shrink(active, messages, kind, disposition, exc):
        assert isinstance(exc, LLMImageTooLargeError)
        assert kind is RequestTransform.SHRINK_IMAGE
        return [*messages, {"role": "user", "content": "shrunk"}]

    result = await orchestrator.run(
        execute_once=execute,
        primary=provider,
        request=[],
        request_transformer=shrink,
    )

    assert result == "shrunk"


@pytest.mark.asyncio
async def test_missing_recovery_capability_aborts_without_second_attempt() -> None:
    orchestrator = AttemptOrchestrator(max_wire_attempts=6)
    provider = _Provider("primary")
    calls = 0

    async def execute(active, messages):
        nonlocal calls
        calls += 1
        raise ContextWindowExceededError("too large")

    with pytest.raises(ContextWindowExceededError):
        await orchestrator.run(
            execute_once=execute,
            primary=provider,
            request=[],
        )

    assert calls == 1


def test_attempt_budget_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        AttemptOrchestrator(max_wire_attempts=0)


def _two_attempt_budget(*, attempts_per_endpoint: int = 2) -> AttemptBudget:
    return AttemptBudget(
        max_wire_attempts=2,
        max_attempts_per_endpoint=attempts_per_endpoint,
        max_endpoint_switches=1,
        max_credential_rotations=1,
        max_request_transforms=1,
        total_deadline_seconds=30,
        single_attempt_timeout_seconds=20,
        max_backoff_seconds=0,
    )


@pytest.mark.asyncio
async def test_exhausted_credential_chain_escalates_to_endpoint_fallback() -> None:
    primary = _Provider("primary")
    backup = _Provider("backup")
    calls: list[str] = []

    async def execute(active, messages):
        calls.append(active.model)
        if active is primary:
            raise LLMAuthenticationError("bad key")
        return active.model

    result = await AttemptOrchestrator(budget=_two_attempt_budget()).run(
        execute_once=execute,
        primary=primary,
        request=[],
        next_credential=lambda active: None,
        endpoint_selector_factory=lambda: lambda: backup,
    )

    assert result == "backup"
    assert calls == ["primary", "backup"]


@pytest.mark.asyncio
async def test_per_endpoint_attempt_cap_switches_without_shared_cursor() -> None:
    primary = _Provider("primary")
    backup = _Provider("backup")

    async def execute(active, messages):
        if active is primary:
            raise ConnectionError("down")
        return active.model

    def factory():
        yielded = False

        def next_provider():
            nonlocal yielded
            if yielded:
                return None
            yielded = True
            return backup

        return next_provider

    orchestrator = AttemptOrchestrator(budget=_two_attempt_budget(attempts_per_endpoint=1))
    first = await orchestrator.run(
        execute_once=execute,
        primary=primary,
        request=[],
        endpoint_selector_factory=factory,
    )
    second = await orchestrator.run(
        execute_once=execute,
        primary=primary,
        request=[],
        endpoint_selector_factory=factory,
    )

    assert first == second == "backup"


@pytest.mark.asyncio
async def test_request_transform_budget_prevents_repeated_mutation() -> None:
    budget = AttemptBudget(
        max_wire_attempts=3,
        max_attempts_per_endpoint=3,
        max_endpoint_switches=2,
        max_credential_rotations=2,
        max_request_transforms=1,
        total_deadline_seconds=30,
        single_attempt_timeout_seconds=20,
        max_backoff_seconds=0,
    )
    calls = 0
    transforms = 0

    async def execute(active, request):
        nonlocal calls
        calls += 1
        raise ContextWindowExceededError("still too large")

    async def transform(active, request, kind, disposition, exc):
        nonlocal transforms
        transforms += 1
        return [*request, {"role": "user", "content": "reduced"}]

    with pytest.raises(ContextWindowExceededError):
        await AttemptOrchestrator(budget=budget).run(
            execute_once=execute,
            primary=_Provider("primary"),
            request=[],
            request_transformer=transform,
        )

    assert calls == 2
    assert transforms == 1
