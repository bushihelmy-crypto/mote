from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest

from mote.contracts.config.llm import LLMConfig
from mote.contracts.config.models import ModelsConfig
from mote.contracts.errors.models import ModelCallExhaustedError, ModelRouteUnavailableError
from mote.contracts.events.types import (
    LLMStreamCommittedEvent,
    LLMStreamDeltaEvent,
    LLMStreamDiscardedEvent,
    LLMStreamInterruptedEvent,
    ModelAttemptFinishedEvent,
    ModelAttemptStartedEvent,
    ModelCallFinishedEvent,
    ModelCallPlannedEvent,
)
from mote.contracts.models import (
    AttemptState,
    CanonicalMessage,
    CanonicalModelResponse,
    EndpointDescriptor,
    GenerateInput,
    GenerateOutput,
    ModelAttemptFinishedRecord,
    ModelAttemptStartedRecord,
    ModelCallFinishedRecord,
    ModelCallPlannedRecord,
    ModelCallState,
    ModelInvocation,
    ModelOperation,
    ModelQuotaObservation,
    ModelUsage,
    RequestRequirements,
    WebSearchOutput,
)
from mote.contracts.resilience import BreakerConfig
from mote.runtime.errors import ContextWindowExceededError, LLMAuthenticationError
from mote.runtime.events.stream import log_llm_stream
from mote.runtime.models.cost import CostTracker
from mote.runtime.models.failover import (
    CanonicalRequestTransformer,
    FailoverPlanner,
    LocalModelCallJournal,
    ModelCallJournalUnavailableError,
    ResourceAdmissionController,
    build_model_runtime_snapshot,
    classify_failure,
)
from mote.runtime.models.model_gateway import RuntimeModelGateway


def _models(*, single_attempt_timeout: float = 10) -> ModelsConfig:
    return ModelsConfig(
        default=LLMConfig(api_key="legacy", model="legacy"),
        endpoints={
            "primary": {
                "api_key": ["primary-a", "primary-b"],
                "model": "primary-model",
                "capabilities": {"context_tokens": 100_000},
            },
            "backup": {
                "api_key": "backup",
                "model": "backup-model",
                "capabilities": {"context_tokens": 100_000},
            },
        },
        failover_groups={
            "interactive": {
                "endpoints": ["primary", "backup"],
                "recovery_profile": "interactive",
            }
        },
        routes={"default": "interactive"},
        recovery_profiles={
            "interactive": {
                "max_wire_attempts": 3,
                "max_attempts_per_endpoint": 2,
                "max_endpoint_switches": 1,
                "max_credential_rotations": 1,
                "max_request_transforms": 1,
                "total_deadline_seconds": 30,
                "single_attempt_timeout_seconds": single_attempt_timeout,
                "max_backoff_seconds": 0,
            }
        },
    )


def _invocation(call_id: str = "call-1") -> ModelInvocation:
    return ModelInvocation(
        model_call_id=call_id,
        route_id="default",
        task="interactive",
        operation=ModelOperation.GENERATE,
        input=GenerateInput(messages=(CanonicalMessage(role="user", content="hello"),)),
        requirements=RequestRequirements(),
    )


class _Adapter:
    def __init__(
        self,
        endpoint_id: str,
        slot_id: str,
        behavior: Callable[[ModelInvocation], object],
    ) -> None:
        self.endpoint_id = endpoint_id
        self.credential_slot_id = slot_id
        self.tenant_fingerprint = f"tenant:{endpoint_id}"
        self._behavior = behavior
        self.calls: list[str] = []

    async def execute_once(
        self,
        invocation,
        endpoint,
        *,
        timeout_seconds,
        stream=False,
    ) -> CanonicalModelResponse:
        self.calls.append(invocation.model_call_id)
        outcome = self._behavior(invocation)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, CanonicalModelResponse):
            return outcome
        output = (
            outcome if isinstance(outcome, (GenerateOutput, WebSearchOutput)) else GenerateOutput(content=str(outcome))
        )
        return CanonicalModelResponse(output=output)

    def classify(self, exc):
        return classify_failure(exc)

    async def aclose(self) -> None:
        return None


class _Resolver:
    def __init__(self, adapters: list[_Adapter]) -> None:
        self.adapters = {(adapter.endpoint_id, adapter.credential_slot_id): adapter for adapter in adapters}

    def resolve(self, endpoint, credential_slot_id):
        return self.adapters.get((endpoint.endpoint_id, credential_slot_id))


class _ClosableResolver(_Resolver):
    def __init__(self, adapters: list[_Adapter]) -> None:
        super().__init__(adapters)
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _FailingJournal:
    def __init__(self, fail_kind: str) -> None:
        self.fail_kind = fail_kind
        self.committed = []

    async def append(self, record) -> None:
        if record.kind == self.fail_kind:
            raise ModelCallJournalUnavailableError("simulated journal failure")
        self.committed.append(record)


def _gateway(
    adapters: list[_Adapter],
    *,
    single_attempt_timeout: float = 10,
    cost_tracker: CostTracker | None = None,
    admission_controller: ResourceAdmissionController | None = None,
    model_call_journal=None,
) -> RuntimeModelGateway:
    planner = FailoverPlanner(build_model_runtime_snapshot(_models(single_attempt_timeout=single_attempt_timeout)))
    return RuntimeModelGateway(
        planner,
        _Resolver(adapters),
        cost_tracker=cost_tracker,
        admission_controller=admission_controller,
        model_call_journal=model_call_journal,
    )


def _adapters(
    primary_a: Callable[[ModelInvocation], object],
    primary_b: Callable[[ModelInvocation], object],
    backup: Callable[[ModelInvocation], object],
) -> list[_Adapter]:
    return [
        _Adapter("primary", "primary:0", primary_a),
        _Adapter("primary", "primary:1", primary_b),
        _Adapter("backup", "backup:0", backup),
    ]


def test_endpoint_fingerprint_tracks_physical_endpoint_not_config_revision() -> None:
    first = EndpointDescriptor(
        endpoint_id="alias-a",
        transport="openai",
        provider="openai",
        model="model",
        base_url_identity="https://example.test/v1",
        credential_pool_id="pool-a",
        lifecycle_revision="revision-a",
    )
    second = first.model_copy(
        update={
            "endpoint_id": "alias-b",
            "credential_pool_id": "pool-b",
            "lifecycle_revision": "revision-b",
        }
    )
    changed_model = first.model_copy(update={"model": "other-model"})

    assert RuntimeModelGateway._endpoint_fingerprint(first) == (RuntimeModelGateway._endpoint_fingerprint(second))
    assert RuntimeModelGateway._endpoint_fingerprint(first) != (
        RuntimeModelGateway._endpoint_fingerprint(changed_model)
    )


@pytest.mark.asyncio
async def test_gateway_rotates_credentials_then_switches_endpoint() -> None:
    adapters = _adapters(
        lambda _invocation: LLMAuthenticationError("bad-a"),
        lambda _invocation: LLMAuthenticationError("bad-b"),
        lambda invocation: f"ok:{invocation.model_call_id}",
    )

    result = await _gateway(adapters).execute(_invocation())

    assert result.output == GenerateOutput(content="ok:call-1")
    assert result.endpoint_id == "backup"
    assert result.credential_slot_id == "backup:0"
    assert result.tenant_fingerprint == "tenant:backup"
    assert [adapter.calls for adapter in adapters] == [
        ["call-1"],
        ["call-1"],
        ["call-1"],
    ]


@pytest.mark.asyncio
async def test_gateway_emits_canonical_call_and_attempt_lifecycle(monkeypatch) -> None:
    emitted = []

    async def capture(event):
        emitted.append(event)

    monkeypatch.setattr("mote.runtime.models.model_gateway.observe_event", capture)
    monkeypatch.setattr(
        "mote.runtime.models.model_gateway.observe_event_sync",
        emitted.append,
    )
    adapters = _adapters(
        lambda _invocation: "ok",
        lambda _invocation: "unused",
        lambda _invocation: "unused",
    )

    await _gateway(adapters).execute(_invocation())

    planned = next(event for event in emitted if isinstance(event, ModelCallPlannedEvent))
    started = next(event for event in emitted if isinstance(event, ModelAttemptStartedEvent))
    finished = next(event for event in emitted if isinstance(event, ModelAttemptFinishedEvent))
    terminal = next(event for event in emitted if isinstance(event, ModelCallFinishedEvent))
    assert planned.model_call_id == started.model_call_id == "call-1"
    assert started.attempt_id == finished.attempt_id == "call-1:1"
    assert started.model == "primary-model"
    assert started.input["messages"][0]["content"] == "hello"
    assert finished.state == "succeeded"
    assert finished.output["content"] == "ok"
    assert terminal.state == "succeeded"


@pytest.mark.asyncio
async def test_output_kind_mismatch_fails_over_without_adapter_wire_retry() -> None:
    adapters = _adapters(
        lambda _invocation: WebSearchOutput(),
        lambda _invocation: "unused",
        lambda _invocation: "backup-ok",
    )

    result = await _gateway(adapters).execute(_invocation())

    assert result.endpoint_id == "backup"
    assert [adapter.calls for adapter in adapters] == [["call-1"], [], ["call-1"]]


@pytest.mark.asyncio
async def test_terminal_error_aggregates_all_wire_attempts() -> None:
    adapters = _adapters(
        lambda _invocation: LLMAuthenticationError("bad-a"),
        lambda _invocation: LLMAuthenticationError("bad-b"),
        lambda _invocation: LLMAuthenticationError("bad-backup"),
    )

    with pytest.raises(ModelCallExhaustedError) as raised:
        await _gateway(adapters).execute(_invocation())

    assert raised.value.context["wire_attempts"] == 3
    assert [fact["endpoint_id"] for fact in raised.value.context["attempts"]] == [
        "primary",
        "primary",
        "backup",
    ]
    assert raised.value.context["last_failure"]["reason"] == "auth_rejected"


@pytest.mark.asyncio
async def test_concurrent_calls_do_not_share_credential_cursor_or_budget() -> None:
    async def reject_after_both_calls_are_admitted(_invocation):
        await asyncio.sleep(0)
        return LLMAuthenticationError("rotate")

    adapters = _adapters(
        reject_after_both_calls_are_admitted,
        lambda invocation: f"slot-b:{invocation.model_call_id}",
        lambda _invocation: "unused",
    )
    gateway = _gateway(adapters)

    first, second = await asyncio.gather(
        gateway.execute(_invocation("call-a")),
        gateway.execute(_invocation("call-b")),
    )

    assert first.output == GenerateOutput(content="slot-b:call-a")
    assert second.output == GenerateOutput(content="slot-b:call-b")
    assert first.credential_slot_id == second.credential_slot_id == "primary:1"
    assert sorted(adapters[0].calls) == ["call-a", "call-b"]
    assert sorted(adapters[1].calls) == ["call-a", "call-b"]
    assert adapters[2].calls == []


@pytest.mark.asyncio
async def test_missing_adapter_fails_before_any_wire_attempt() -> None:
    adapters = _adapters(
        lambda _invocation: "ok",
        lambda _invocation: "ok",
        lambda _invocation: "ok",
    )[:-1]

    with pytest.raises(ModelRouteUnavailableError):
        await _gateway(adapters).execute(_invocation())

    assert all(adapter.calls == [] for adapter in adapters)


@pytest.mark.asyncio
async def test_single_attempt_timeout_is_bounded_then_falls_back() -> None:
    async def slow(_invocation):
        await asyncio.sleep(1)
        return "late"

    adapters = _adapters(
        slow,
        lambda _invocation: "unused",
        lambda _invocation: "backup-ok",
    )

    result = await _gateway(
        adapters,
        single_attempt_timeout=0.01,
    ).execute(_invocation())

    assert result.endpoint_id == "backup"
    assert adapters[0].calls == ["call-1", "call-1"]
    assert adapters[1].calls == []
    assert adapters[2].calls == ["call-1"]


@pytest.mark.asyncio
async def test_gateway_records_authoritative_cost_once_on_shared_tracker() -> None:
    tracker = CostTracker()
    response = CanonicalModelResponse(
        output=GenerateOutput(content="ok"),
        usage=ModelUsage(
            input_tokens=11,
            output_tokens=7,
            total_tokens=18,
            cache_read_tokens=3,
            cache_write_tokens=2,
            reasoning_tokens=5,
        ),
        cost_usd=Decimal("0.0123"),
    )
    adapters = _adapters(
        lambda _invocation: response,
        lambda _invocation: "unused",
        lambda _invocation: "unused",
    )

    result = await _gateway(adapters, cost_tracker=tracker).execute(_invocation())

    assert result.cost_usd == Decimal("0.0123")
    assert tracker.total_cost == pytest.approx(0.0123)
    assert tracker.model_usage["primary-model"].requests == 1
    assert tracker.last_usage.input_tokens == 11
    assert tracker.last_usage.cached_input_tokens == 3
    assert tracker.last_usage.cache_creation_tokens == 2
    assert tracker.last_usage.reasoning_tokens == 5


@pytest.mark.asyncio
async def test_success_quota_observation_blocks_next_primary_without_wire() -> None:
    primary_response = CanonicalModelResponse(
        output=GenerateOutput(content="primary-ok"),
        quota=ModelQuotaObservation(
            remaining_requests=0,
            reset_requests_after_seconds=30.0,
        ),
    )
    adapters = _adapters(
        lambda _invocation: primary_response,
        lambda _invocation: "unused credential",
        lambda _invocation: "backup-ok",
    )
    gateway = _gateway(adapters)

    first = await gateway.execute(_invocation("call-a"))
    second = await gateway.execute(_invocation("call-b"))

    assert first.endpoint_id == "primary"
    assert second.endpoint_id == "backup"
    assert adapters[0].calls == ["call-a"]
    assert adapters[1].calls == []
    assert adapters[2].calls == ["call-b"]


@pytest.mark.asyncio
async def test_cancellation_never_starts_another_attempt() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def wait_forever(_invocation):
        started.set()
        await release.wait()
        return "late"

    adapters = _adapters(
        wait_forever,
        lambda _invocation: "credential-retry",
        lambda _invocation: "endpoint-retry",
    )
    task = asyncio.create_task(_gateway(adapters).execute(_invocation()))
    await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [adapter.calls for adapter in adapters] == [["call-1"], [], []]


@pytest.mark.asyncio
async def test_concurrent_canonical_transforms_use_call_local_reducers() -> None:
    attempts: dict[str, int] = {}

    def overflow_once(invocation):
        count = attempts.get(invocation.model_call_id, 0)
        attempts[invocation.model_call_id] = count + 1
        if count == 0:
            return ContextWindowExceededError("too large")
        return invocation.input.messages[0].content

    class Reducer:
        def __init__(self, replacement: str) -> None:
            self.replacement = replacement

        async def reduce(self, messages, *, target_tokens):
            return [{"role": "user", "content": self.replacement}]

    adapters = _adapters(
        overflow_once,
        lambda _invocation: "unused",
        lambda _invocation: "unused",
    )
    gateway = _gateway(adapters)

    first, second = await asyncio.gather(
        gateway.execute(
            _invocation("call-a"),
            request_transformer=CanonicalRequestTransformer(Reducer("agent-a")),
        ),
        gateway.execute(
            _invocation("call-b"),
            request_transformer=CanonicalRequestTransformer(Reducer("agent-b")),
        ),
    )

    assert first.output == GenerateOutput(content="agent-a")
    assert second.output == GenerateOutput(content="agent-b")
    assert sorted(adapters[0].calls) == ["call-a", "call-a", "call-b", "call-b"]
    assert adapters[1].calls == []
    assert adapters[2].calls == []


@pytest.mark.asyncio
async def test_credential_quarantine_is_shared_without_sharing_retry_cursor() -> None:
    adapters = _adapters(
        lambda _invocation: LLMAuthenticationError("bad credential"),
        lambda invocation: f"healthy:{invocation.model_call_id}",
        lambda _invocation: "unused",
    )
    gateway = _gateway(adapters)

    first = await gateway.execute(_invocation("call-a"))
    second = await gateway.execute(_invocation("call-b"))

    assert first.output == GenerateOutput(content="healthy:call-a")
    assert second.output == GenerateOutput(content="healthy:call-b")
    assert adapters[0].calls == ["call-a"]
    assert adapters[1].calls == ["call-a", "call-b"]
    assert adapters[2].calls == []


@pytest.mark.asyncio
async def test_open_availability_plane_switches_without_extra_primary_wire() -> None:
    controller = ResourceAdmissionController(
        breaker_config=BreakerConfig(
            min_samples=1,
            error_rate_threshold=1.0,
            open_seconds=100,
        )
    )
    adapters = _adapters(
        lambda _invocation: ConnectionError("down"),
        lambda _invocation: "unused credential",
        lambda _invocation: "backup-ok",
    )

    result = await _gateway(
        adapters,
        admission_controller=controller,
    ).execute(_invocation())

    assert result.endpoint_id == "backup"
    assert [adapter.calls for adapter in adapters] == [["call-1"], [], ["call-1"]]


@pytest.mark.asyncio
async def test_bulkhead_rejection_fails_over_without_primary_wire() -> None:
    controller = ResourceAdmissionController(max_in_flight_per_endpoint=1)
    started = asyncio.Event()
    release = asyncio.Event()

    async def primary(invocation):
        if invocation.model_call_id == "call-a":
            started.set()
            await release.wait()
        return f"primary:{invocation.model_call_id}"

    adapters = _adapters(
        primary,
        lambda _invocation: "unused credential",
        lambda invocation: f"backup:{invocation.model_call_id}",
    )
    gateway = _gateway(adapters, admission_controller=controller)
    first_task = asyncio.create_task(gateway.execute(_invocation("call-a")))
    await started.wait()

    second = await gateway.execute(_invocation("call-b"))
    release.set()
    first = await first_task

    assert first.endpoint_id == "primary"
    assert second.endpoint_id == "backup"
    assert adapters[0].calls == ["call-a"]
    assert adapters[2].calls == ["call-b"]


@pytest.mark.asyncio
async def test_cancellation_abandons_bulkhead_permit() -> None:
    controller = ResourceAdmissionController(max_in_flight_per_endpoint=1)
    started = asyncio.Event()
    release = asyncio.Event()

    async def primary(invocation):
        if invocation.model_call_id == "call-a":
            started.set()
            await release.wait()
        return f"primary:{invocation.model_call_id}"

    adapters = _adapters(
        primary,
        lambda _invocation: "unused",
        lambda _invocation: "unused",
    )
    gateway = _gateway(adapters, admission_controller=controller)
    cancelled = asyncio.create_task(gateway.execute(_invocation("call-a")))
    await started.wait()
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    result = await gateway.execute(_invocation("call-b"))

    assert result.endpoint_id == "primary"
    assert adapters[0].calls == ["call-a", "call-b"]
    assert adapters[2].calls == []


@pytest.mark.asyncio
async def test_journal_records_complete_success_before_return(tmp_path: Path) -> None:
    journal = LocalModelCallJournal(tmp_path)
    adapters = _adapters(
        lambda _invocation: "ok",
        lambda _invocation: "unused",
        lambda _invocation: "unused",
    )

    result = await _gateway(adapters, model_call_journal=journal).execute(_invocation())

    records = journal.records("call-1")
    assert result.output == GenerateOutput(content="ok")
    assert [record.kind for record in records] == [
        "call_planned",
        "attempt_started",
        "attempt_finished",
        "call_finished",
    ]
    assert isinstance(records[0], ModelCallPlannedRecord)
    assert isinstance(records[1], ModelAttemptStartedRecord)
    assert isinstance(records[2], ModelAttemptFinishedRecord)
    assert records[2].state is AttemptState.SUCCEEDED
    assert isinstance(records[3], ModelCallFinishedRecord)
    assert records[3].state is ModelCallState.SUCCEEDED


@pytest.mark.asyncio
async def test_journal_records_each_failed_over_wire_attempt(tmp_path: Path) -> None:
    journal = LocalModelCallJournal(tmp_path)
    adapters = _adapters(
        lambda _invocation: WebSearchOutput(),
        lambda _invocation: "unused",
        lambda _invocation: "backup-ok",
    )

    result = await _gateway(adapters, model_call_journal=journal).execute(_invocation())

    records = journal.records("call-1")
    starts = [record for record in records if record.kind == "attempt_started"]
    finishes = [record for record in records if record.kind == "attempt_finished"]
    assert result.endpoint_id == "backup"
    assert [record.ordinal for record in starts] == [1, 2]
    assert [record.ordinal for record in finishes] == [1, 2]
    assert [record.state for record in finishes] == [
        AttemptState.FAILED,
        AttemptState.SUCCEEDED,
    ]


@pytest.mark.asyncio
async def test_cancellation_commits_attempt_and_call_terminals(tmp_path: Path) -> None:
    journal = LocalModelCallJournal(tmp_path)
    started = asyncio.Event()

    async def wait_forever(_invocation):
        started.set()
        await asyncio.Event().wait()

    adapters = _adapters(
        wait_forever,
        lambda _invocation: "unused",
        lambda _invocation: "unused",
    )
    task = asyncio.create_task(_gateway(adapters, model_call_journal=journal).execute(_invocation()))
    await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    records = journal.records("call-1")
    assert [record.kind for record in records] == [
        "call_planned",
        "attempt_started",
        "attempt_finished",
        "call_finished",
    ]
    assert records[2].state is AttemptState.CANCELLED
    assert records[3].state is ModelCallState.CANCELLED


@pytest.mark.asyncio
async def test_attempt_start_journal_failure_prevents_wire() -> None:
    journal = _FailingJournal("attempt_started")
    adapters = _adapters(
        lambda _invocation: "must-not-run",
        lambda _invocation: "must-not-run",
        lambda _invocation: "must-not-run",
    )

    with pytest.raises(ModelCallExhaustedError) as raised:
        await _gateway(adapters, model_call_journal=journal).execute(_invocation())

    assert [adapter.calls for adapter in adapters] == [[], [], []]
    assert raised.value.context["wire_attempts"] == 0
    assert [record.kind for record in journal.committed] == [
        "call_planned",
        "call_finished",
    ]
    assert journal.committed[-1].failure.provider_code == ("MODEL_CALL_JOURNAL_UNAVAILABLE")


@pytest.mark.asyncio
async def test_attempt_terminal_journal_failure_prevents_fallback_wire() -> None:
    journal = _FailingJournal("attempt_finished")
    adapters = _adapters(
        lambda _invocation: ConnectionError("primary down"),
        lambda _invocation: "must-not-run",
        lambda _invocation: "must-not-run",
    )

    with pytest.raises(ModelCallExhaustedError):
        await _gateway(adapters, model_call_journal=journal).execute(_invocation())

    assert [adapter.calls for adapter in adapters] == [["call-1"], [], []]
    assert [record.kind for record in journal.committed] == [
        "call_planned",
        "attempt_started",
        "call_finished",
    ]


@pytest.mark.asyncio
async def test_crash_after_started_commit_recovers_as_in_doubt(tmp_path: Path) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    class CrashAfterStartedJournal(LocalModelCallJournal):
        async def append(self, record) -> None:
            await super().append(record)
            if isinstance(record, ModelAttemptStartedRecord):
                raise SimulatedProcessCrash

    journal = CrashAfterStartedJournal(tmp_path)
    adapters = _adapters(
        lambda _invocation: "must-not-run",
        lambda _invocation: "must-not-run",
        lambda _invocation: "must-not-run",
    )

    with pytest.raises(SimulatedProcessCrash):
        await _gateway(adapters, model_call_journal=journal).execute(_invocation())

    recovery = journal.recover("call-1")
    assert [adapter.calls for adapter in adapters] == [[], [], []]
    assert recovery.state is ModelCallState.IN_DOUBT
    assert recovery.in_doubt_attempt_ids == ("call-1:1",)


@pytest.mark.asyncio
async def test_success_checkpoint_resume_performs_zero_wire_calls(
    tmp_path: Path,
) -> None:
    journal = LocalModelCallJournal(tmp_path)
    adapters = _adapters(
        lambda _invocation: "checkpointed",
        lambda _invocation: "unused",
        lambda _invocation: "unused",
    )
    gateway = _gateway(adapters, model_call_journal=journal)
    first = await gateway.execute(_invocation("checkpoint-call"))
    for adapter in adapters:
        adapter.calls.clear()

    resumed = await gateway.resume(_invocation("checkpoint-call"))

    assert resumed.output == first.output
    assert resumed.successful_attempt_id == first.successful_attempt_id
    assert [adapter.calls for adapter in adapters] == [[], [], []]


@pytest.mark.asyncio
async def test_resume_uses_current_revision_without_expanding_original_budget(
    tmp_path: Path,
) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    class CrashAfterStartedJournal(LocalModelCallJournal):
        async def append(self, record) -> None:
            await super().append(record)
            if isinstance(record, ModelAttemptStartedRecord):
                raise SimulatedProcessCrash

    crash_journal = CrashAfterStartedJournal(tmp_path)
    first_adapters = _adapters(
        lambda _invocation: "must-not-run",
        lambda _invocation: "must-not-run",
        lambda _invocation: "must-not-run",
    )
    gateway = _gateway(first_adapters, model_call_journal=crash_journal)
    with pytest.raises(SimulatedProcessCrash):
        await gateway.execute(_invocation("resume-call"))

    current_models = _models().model_copy(deep=True)
    current_models.endpoints["primary"].model = "primary-model-v2"
    current_models.recovery_profiles["interactive"].max_wire_attempts = 6
    current_snapshot = build_model_runtime_snapshot(current_models)
    resumed_adapters = _adapters(
        lambda _invocation: LLMAuthenticationError("still bad"),
        lambda _invocation: LLMAuthenticationError("still bad"),
        lambda _invocation: LLMAuthenticationError("still bad"),
    )
    gateway._model_call_journal = LocalModelCallJournal(tmp_path)
    await gateway.reload(
        FailoverPlanner(current_snapshot),
        _Resolver(resumed_adapters),
    )

    with pytest.raises(ModelCallExhaustedError):
        await gateway.resume(_invocation("resume-call"))

    recovery = gateway._model_call_journal.recover("resume-call")
    assert recovery.plans[-1].config_revision == current_snapshot.revision
    assert recovery.plans[-1].resume_generation == 1
    assert recovery.terminal is not None
    assert recovery.terminal.summary is not None
    assert recovery.terminal.summary.wire_attempts_used == 3
    assert recovery.terminal.summary.in_doubt_attempt_ids == ("resume-call:1",)
    assert recovery.terminal.summary.possible_duplicate_billing is True


@pytest.mark.asyncio
async def test_reload_keeps_inflight_generation_and_drains_old_resolver() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def old_call(_invocation):
        entered.set()
        await release.wait()
        return "old-generation"

    old_adapters = _adapters(
        old_call,
        lambda _invocation: "unused",
        lambda _invocation: "unused",
    )
    old_resolver = _ClosableResolver(old_adapters)
    gateway = RuntimeModelGateway(
        FailoverPlanner(build_model_runtime_snapshot(_models())),
        old_resolver,
    )
    inflight = asyncio.create_task(gateway.execute(_invocation("old-call")))
    await entered.wait()

    new_models = _models().model_copy(deep=True)
    new_models.endpoints["primary"].model = "primary-model-v2"
    new_snapshot = build_model_runtime_snapshot(new_models)
    new_adapters = _adapters(
        lambda _invocation: "new-generation",
        lambda _invocation: "unused",
        lambda _invocation: "unused",
    )
    new_resolver = _ClosableResolver(new_adapters)
    await gateway.reload(FailoverPlanner(new_snapshot), new_resolver)

    assert old_resolver.closed is False
    fresh = await gateway.execute(_invocation("new-call"))
    assert fresh.output == GenerateOutput(content="new-generation")
    assert fresh.model_or_deployment == "primary-model-v2"

    release.set()
    old = await inflight
    assert old.output == GenerateOutput(content="old-generation")
    assert old.model_or_deployment == "primary-model"
    assert old_resolver.closed is True
    assert new_resolver.closed is False


@pytest.mark.asyncio
async def test_failed_attempt_stream_is_discarded_before_fallback_commit(
    monkeypatch,
) -> None:
    emitted = []
    fed = []
    monkeypatch.setattr(
        "mote.runtime.events.stream.observe_event_sync",
        emitted.append,
    )
    monkeypatch.setattr("mote.runtime.events.stream.feed_output_stream", fed.append)

    def incompatible(_invocation):
        log_llm_stream("discarded-primary")
        return WebSearchOutput()

    def accepted(_invocation):
        log_llm_stream("accepted-backup")
        return "ok"

    adapters = _adapters(
        incompatible,
        lambda _invocation: "unused",
        accepted,
    )

    result = await _gateway(adapters).execute(_invocation(), stream=True)

    assert result.endpoint_id == "backup"
    deltas = [event for event in emitted if isinstance(event, LLMStreamDeltaEvent)]
    discarded = [event for event in emitted if isinstance(event, LLMStreamDiscardedEvent)]
    committed = [event for event in emitted if isinstance(event, LLMStreamCommittedEvent)]
    assert [event.token for event in deltas] == [
        "discarded-primary",
        "accepted-backup",
    ]
    assert all(event.model_call_id == "call-1" for event in deltas)
    assert [event.attempt_id for event in deltas] == ["call-1:1", "call-1:2"]
    assert all(event.sequence == 1 for event in deltas)
    assert all(event.provisional is True for event in deltas)
    assert [(event.attempt_id, event.chunk_count) for event in discarded] == [("call-1:1", 1)]
    assert [(event.attempt_id, event.chunk_count) for event in committed] == [("call-1:2", 1)]
    assert fed == ["accepted-backup"]


@pytest.mark.asyncio
async def test_cancelled_attempt_stream_is_discarded(monkeypatch) -> None:
    emitted = []
    started = asyncio.Event()
    monkeypatch.setattr(
        "mote.runtime.events.stream.observe_event_sync",
        emitted.append,
    )

    async def interrupted(_invocation):
        log_llm_stream("partial")
        started.set()
        await asyncio.Event().wait()

    adapters = _adapters(
        interrupted,
        lambda _invocation: "unused",
        lambda _invocation: "unused",
    )
    task = asyncio.create_task(_gateway(adapters).execute(_invocation(), stream=True))
    await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    interrupted_events = [event for event in emitted if isinstance(event, LLMStreamInterruptedEvent)]
    assert len(interrupted_events) == 1
    assert interrupted_events[0].attempt_id == "call-1:1"
    assert interrupted_events[0].chunk_count == 1


@pytest.mark.asyncio
async def test_concurrent_attempt_stream_buffers_do_not_cross_calls(
    monkeypatch,
) -> None:
    emitted = []
    monkeypatch.setattr(
        "mote.runtime.events.stream.observe_event_sync",
        emitted.append,
    )

    async def stream_own_identity(invocation):
        log_llm_stream(f"{invocation.model_call_id}:first")
        await asyncio.sleep(0)
        log_llm_stream(f"{invocation.model_call_id}:second")
        return invocation.model_call_id

    adapters = _adapters(
        stream_own_identity,
        lambda _invocation: "unused",
        lambda _invocation: "unused",
    )
    gateway = _gateway(adapters)

    await asyncio.gather(
        gateway.execute(_invocation("call-a"), stream=True),
        gateway.execute(_invocation("call-b"), stream=True),
    )

    by_call = {
        call_id: [
            event.token
            for event in emitted
            if isinstance(event, LLMStreamDeltaEvent) and event.model_call_id == call_id
        ]
        for call_id in ("call-a", "call-b")
    }
    assert by_call == {
        "call-a": ["call-a:first", "call-a:second"],
        "call-b": ["call-b:first", "call-b:second"],
    }
    assert all(
        event.attempt_id == f"{event.model_call_id}:1" for event in emitted if isinstance(event, LLMStreamDeltaEvent)
    )
