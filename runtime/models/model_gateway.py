"""Provider-neutral Runtime ModelGateway over immutable plans and adapters."""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from mote.contracts.artifact import ArtifactResolutionPolicy, ResolvedArtifact
from mote.contracts.events.envelope import JsonValue
from mote.contracts.events.model import (
    ModelAttemptAdmissionRejectedEvent,
    ModelAttemptFinishedEvent,
    ModelAttemptStartedEvent,
    ModelCallFinishedEvent,
    ModelCallPlannedEvent,
    ModelFallbackSelectedEvent,
)
from mote.contracts.foundation.errors.base import MoteError
from mote.contracts.inference.attempt import InferenceAttemptRequest
from mote.contracts.inference.deadline import CrossProcessDeadline
from mote.contracts.model.endpoint_binding import ResolvedEndpointBinding
from mote.contracts.model.errors import (
    ModelCallDeadlineExceededError,
    ModelCallExhaustedError,
    ModelCallInDoubtError,
    ModelCapabilityUnsatisfiedError,
    ModelRouteUnavailableError,
)
from mote.contracts.model.failover import (
    AttemptState,
    AttemptSummary,
    DecisionKind,
    EndpointDescriptor,
    FailoverDecision,
    FailoverPlan,
    FailureDisposition,
    FailureDomain,
    FailureReason,
    HealthVerdict,
    ModelCallState,
    ModelCallSummary,
    RequestTransform,
    ResourceIdentity,
    Retryability,
)
from mote.contracts.model.invocation import (
    CanonicalModelOutput,
    CanonicalModelResponse,
    GenerateOutput,
    ImageDescriptionInput,
    ModelInvocation,
    ModelUsage,
    ResolvedModelResponse,
)
from mote.contracts.model.model_journal import (
    ModelAttemptFinishedRecord,
    ModelAttemptStartedRecord,
    ModelCallFinishedRecord,
    ModelCallJournalRecord,
    ModelCallPlannedRecord,
    ModelCallRecovery,
    ModelDecisionRecord,
)
from mote.contracts.model.operations import ModelOperation
from mote.contracts.model.topology import RouteId
from mote.contracts.model.topology_codec import encode_route_id
from mote.contracts.ports.artifact.store import ArtifactResolver
from mote.contracts.ports.model.artifact import ModelResponseArtifactPublisher
from mote.contracts.ports.model.call_journal import ModelCallJournal
from mote.contracts.ports.model.recovery import ModelRecoveryDisposition, ModelRecoveryInspection
from mote.contracts.ports.model.request_transformer import ModelRequestTransformer
from mote.contracts.ports.session.facts import SessionFactSink
from mote.runtime.events.context import observe_event, observe_event_sync
from mote.runtime.events.stream import (
    capture_attempt_stream,
    commit_attempt_stream,
    discard_attempt_stream,
    interrupt_attempt_stream,
)
from mote.runtime.models.cost import CostTracker, TokenUsage
from mote.runtime.models.failover.compatibility import endpoints_are_projection_compatible
from mote.runtime.models.failover.model_journal import ModelCallJournalError
from mote.runtime.models.failover.orchestrator import AttemptOrchestrator, AttemptResumeSeed
from mote.runtime.models.failover.planner import FailoverPlanner
from mote.runtime.models.failover.runtime_state import ModelRuntimeGeneration
from mote.runtime.models.inference_attempt_executor import RuntimeAttemptFailure
from mote.runtime.resilience.admission import AdmissionRejectedError, AdmissionResult, ResourceAdmissionController
from mote.runtime.resilience.failover.classification import classify_failure


@dataclass(frozen=True)
class _AttemptTarget:
    endpoint: EndpointDescriptor
    credential_slot_id: str
    tenant_fingerprint: str
    resource: ResourceIdentity
    binding: ResolvedEndpointBinding


@dataclass
class _CallExecutionState:
    wire_attempts: int = 0
    resume_generation: int = 0
    successful_attempt_id: str = ""
    records: list[ModelCallJournalRecord] = field(default_factory=list)


class _ClassifiedEndpointFailure(Exception):
    def __init__(
        self,
        disposition: FailureDisposition,
        target: _AttemptTarget,
        cause: Exception | None = None,
    ) -> None:
        self.disposition = disposition
        self.target = target
        self.cause = cause
        super().__init__(disposition.reason.value)
        if cause is not None:
            self.__cause__ = cause


class RuntimeModelGateway:
    """Plan and execute one logical model call through bound Product adapters."""

    def __init__(
        self,
        planner: FailoverPlanner,
        cost_tracker: CostTracker | None = None,
        admission_controller: ResourceAdmissionController | None = None,
        model_call_journal: ModelCallJournal | None = None,
        response_artifact_publisher: ModelResponseArtifactPublisher | None = None,
    ) -> None:
        self._cost_tracker = cost_tracker
        self._admission_controller = admission_controller or ResourceAdmissionController()
        self._model_call_journal = model_call_journal
        self._response_artifact_publisher = response_artifact_publisher

    def inspect_recovery(self, model_call_id: str) -> ModelRecoveryInspection:
        if not model_call_id:
            raise ValueError("ModelCall recovery requires an identity")
        journal = self._model_call_journal
        if journal is None:
            return ModelRecoveryInspection(model_call_id, ModelRecoveryDisposition.ABSENT)
        try:
            if not journal.records(model_call_id):
                return ModelRecoveryInspection(model_call_id, ModelRecoveryDisposition.ABSENT)
            recovery = journal.recover(model_call_id)
        except Exception as exc:
            return ModelRecoveryInspection(
                model_call_id,
                ModelRecoveryDisposition.CORRUPT,
                detail=type(exc).__name__,
            )
        if recovery.model_call_id != model_call_id:
            return ModelRecoveryInspection(model_call_id, ModelRecoveryDisposition.IDENTITY_MISMATCH)
        if recovery.state is ModelCallState.IN_DOUBT:
            disposition = ModelRecoveryDisposition.IN_DOUBT
        elif recovery.state in {ModelCallState.PLANNED, ModelCallState.RUNNING}:
            disposition = ModelRecoveryDisposition.RECOVERABLE
        else:
            disposition = ModelRecoveryDisposition.TERMINAL
        return ModelRecoveryInspection(model_call_id, disposition, recovery)

    async def _journal_output(self, output: CanonicalModelOutput) -> CanonicalModelOutput:
        if not isinstance(output, GenerateOutput):
            return output
        content = output.content.encode("utf-8")
        if len(content) <= 64 * 1024:
            return output
        publisher = self._response_artifact_publisher
        if publisher is None:
            raise RuntimeError("oversized Model response requires the Product Artifact publisher")
        ref = await publisher(content, "text/plain", "model-response.txt")
        return output.model_copy(update={"content": "", "content_artifact": ref})

    async def execute_generation(
        self,
        generation: ModelRuntimeGeneration,
        invocation: ModelInvocation,
        *,
        request_transformer: ModelRequestTransformer | None = None,
        stream: bool = False,
        session_fact_sink: SessionFactSink | None = None,
        artifact_resolver: ArtifactResolver | None = None,
        runtime_generation_id: str,
    ) -> ResolvedModelResponse:
        plan = generation.planner.plan(invocation)
        state = _CallExecutionState()
        await self._append_record(
            state,
            self._plan_record(
                invocation,
                plan,
                generation=0,
                runtime_generation_id=runtime_generation_id,
                topology_revision=generation.revision,
            ),
        )
        return await self._run_generation(
            invocation,
            plan,
            state,
            runtime_generation=generation,
            request_transformer=request_transformer,
            stream=stream,
            session_fact_sink=session_fact_sink,
            resume_seed=None,
            artifact_resolver=artifact_resolver,
            runtime_generation_id=runtime_generation_id,
        )

    async def resume_generation(
        self,
        generation: ModelRuntimeGeneration,
        invocation: ModelInvocation,
        *,
        request_transformer: ModelRequestTransformer | None = None,
        stream: bool = False,
        session_fact_sink: SessionFactSink | None = None,
        artifact_resolver: ArtifactResolver | None = None,
        runtime_generation_id: str,
    ) -> ResolvedModelResponse:
        if self._model_call_journal is None:
            raise ModelCallInDoubtError(
                "model call resume requires a durable model-call journal",
                model_call_id=invocation.model_call_id,
            )
        if not self._model_call_journal.records(invocation.model_call_id):
            return await self.execute_generation(
                generation,
                invocation,
                request_transformer=request_transformer,
                stream=stream,
                session_fact_sink=session_fact_sink,
                artifact_resolver=artifact_resolver,
                runtime_generation_id=runtime_generation_id,
            )
        recovery = self._model_call_journal.recover(invocation.model_call_id)
        if recovery.state is ModelCallState.SUCCEEDED:
            return self._reinstate(recovery)
        if recovery.terminal is not None:
            raise ModelCallInDoubtError(
                "terminal model call cannot start another resume generation",
                model_call_id=invocation.model_call_id,
                state=recovery.state.value,
            )

        state = _CallExecutionState(
            wire_attempts=recovery.attempts_started,
            resume_generation=recovery.plan.resume_generation + 1,
            records=list(self._model_call_journal.records(invocation.model_call_id)),
        )
        finished_ids = {record.attempt_id for record in recovery.attempt_finishes}
        for start in recovery.attempt_starts:
            if start.attempt_id in finished_ids:
                continue
            await self._append_record(
                state,
                ModelAttemptFinishedRecord(
                    model_call_id=invocation.model_call_id,
                    attempt_id=start.attempt_id,
                    ordinal=start.ordinal,
                    resume_generation=start.resume_generation,
                    state=AttemptState.IN_DOUBT,
                ),
            )
        recovery = self._model_call_journal.recover(invocation.model_call_id)
        candidate = generation.planner.plan(invocation)
        plan, seed = self._resume_plan(candidate, recovery)
        await self._append_record(
            state,
            self._plan_record(
                invocation,
                plan,
                generation=state.resume_generation,
                root_started_at=recovery.original_plan.root_started_at or recovery.original_plan.occurred_at,
                runtime_generation_id=runtime_generation_id,
                topology_revision=generation.revision,
            ),
        )
        return await self._run_generation(
            invocation,
            plan,
            state,
            runtime_generation=generation,
            request_transformer=request_transformer,
            stream=stream,
            session_fact_sink=session_fact_sink,
            resume_seed=seed,
            artifact_resolver=artifact_resolver,
            runtime_generation_id=runtime_generation_id,
        )

    async def _run_generation(
        self,
        invocation: ModelInvocation,
        plan: FailoverPlan,
        execution_state: _CallExecutionState,
        *,
        runtime_generation: ModelRuntimeGeneration,
        request_transformer: ModelRequestTransformer | None,
        stream: bool,
        session_fact_sink: SessionFactSink | None,
        resume_seed: AttemptResumeSeed | None,
        artifact_resolver: ArtifactResolver | None,
        runtime_generation_id: str,
    ) -> ResolvedModelResponse:
        call_terminal = False

        async def commit_terminal(record: ModelCallFinishedRecord) -> None:
            nonlocal call_terminal
            task = asyncio.create_task(self._append_record(execution_state, record))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await asyncio.shield(task)
                call_terminal = True
                raise
            call_terminal = True

        self._observe_planned(
            invocation,
            plan,
            execution_state.resume_generation,
            runtime_generation_id=runtime_generation_id,
            topology_revision=runtime_generation.revision,
        )
        try:
            result = await self._execute_unclosed(
                invocation,
                plan,
                execution_state,
                request_transformer,
                stream,
                resume_seed,
                artifact_resolver,
                runtime_generation,
            )
            summary = self._build_summary(
                execution_state,
                plan,
                ModelCallState.SUCCEEDED,
                selected_endpoint_id=result.endpoint_id,
            )
            terminal = ModelCallFinishedRecord(
                model_call_id=invocation.model_call_id,
                state=ModelCallState.SUCCEEDED,
                selected_endpoint_id=result.endpoint_id,
                wire_attempts=summary.wire_attempts_used,
                usage=self._usage_from_summary(summary),
                cost_usd=summary.known_cost_usd,
                accepted_response=CanonicalModelResponse(
                    output=await self._journal_output(result.output),
                    usage=result.usage,
                    cost_usd=result.cost_usd,
                ),
                successful_attempt_id=execution_state.successful_attempt_id,
                endpoint_fingerprint=result.endpoint_fingerprint,
                model_or_deployment=result.model_or_deployment,
                provider=result.provider,
                transport=result.transport,
                tenant_fingerprint=result.tenant_fingerprint,
                credential_slot_id=result.credential_slot_id,
            )
            await commit_terminal(terminal)
            await self._publish_finished(terminal, session_fact_sink)
            return result.model_copy(update={"summary": summary})
        except asyncio.CancelledError:
            if not call_terminal:
                summary = self._build_summary(execution_state, plan, ModelCallState.CANCELLED)
                terminal = ModelCallFinishedRecord(
                    model_call_id=invocation.model_call_id,
                    state=ModelCallState.CANCELLED,
                    wire_attempts=summary.wire_attempts_used,
                    usage=self._usage_from_summary(summary),
                    cost_usd=summary.known_cost_usd,
                )
                await commit_terminal(terminal)
                await self._publish_finished(terminal, session_fact_sink)
            raise
        except Exception as exc:
            if not call_terminal:
                failure = self._classify_chain(exc)
                summary = self._build_summary(
                    execution_state,
                    plan,
                    ModelCallState.FAILED,
                    last_failure=failure,
                )
                terminal = ModelCallFinishedRecord(
                    model_call_id=invocation.model_call_id,
                    state=ModelCallState.FAILED,
                    wire_attempts=summary.wire_attempts_used,
                    usage=self._usage_from_summary(summary),
                    cost_usd=summary.known_cost_usd,
                    failure=failure,
                )
                await commit_terminal(terminal)
                if isinstance(exc, MoteError):
                    exc.context["summary"] = summary.model_dump(mode="json")
                await self._publish_finished(terminal, session_fact_sink)
            raise

    async def _execute_unclosed(
        self,
        invocation: ModelInvocation,
        plan: FailoverPlan,
        execution_state: _CallExecutionState,
        request_transformer: ModelRequestTransformer | None,
        stream: bool,
        resume_seed: AttemptResumeSeed | None,
        artifact_resolver: ArtifactResolver | None,
        runtime_generation: ModelRuntimeGeneration,
    ) -> ResolvedModelResponse:
        if resume_seed is not None and resume_seed.wire_attempts >= plan.budget.max_wire_attempts:
            raise ModelCallExhaustedError(
                "model call resume has no remaining wire-attempt budget",
                model_call_id=invocation.model_call_id,
                wire_attempts=resume_seed.wire_attempts,
                config_revision=plan.config_revision,
            )
        resolved_artifact = await self._resolve_artifact(
            invocation,
            artifact_resolver,
        )
        targets = self._resolve_targets(
            plan.endpoints,
            runtime_generation=runtime_generation,
        )
        primary = targets[plan.endpoints[0].endpoint_id][0]
        inherited_elapsed = resume_seed.elapsed_seconds if resume_seed is not None else 0.0
        deadline = time.monotonic() + max(
            plan.budget.total_deadline_seconds - inherited_elapsed,
            0.0,
        )
        if deadline <= time.monotonic():
            raise ModelCallDeadlineExceededError(
                "model call resume has no remaining deadline",
                model_call_id=invocation.model_call_id,
                config_revision=plan.config_revision,
            )
        attempt_facts: list[dict[str, object]] = []
        successful_target: _AttemptTarget | None = None

        async def execute_once(
            target: _AttemptTarget,
            current_invocation: ModelInvocation,
        ) -> CanonicalModelResponse:
            nonlocal successful_target
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("logical model call deadline exceeded")
            timeout_seconds = min(
                remaining,
                plan.budget.single_attempt_timeout_seconds,
            )
            ordinal = execution_state.wire_attempts + 1
            attempt_id = f"{invocation.model_call_id}:{ordinal}"
            started_at = time.monotonic()
            started = ModelAttemptStartedRecord(
                model_call_id=invocation.model_call_id,
                attempt_id=attempt_id,
                ordinal=ordinal,
                endpoint_id=target.endpoint.endpoint_id,
                endpoint_fingerprint=target.resource.endpoint_fingerprint,
                credential_slot_id=target.credential_slot_id,
                resume_generation=execution_state.resume_generation,
                timeout_seconds=timeout_seconds,
            )
            await self._append_record(execution_state, started)
            await observe_event(
                ModelAttemptStartedEvent(
                    model_call_id=invocation.model_call_id,
                    attempt_id=attempt_id,
                    ordinal=ordinal,
                    resume_generation=execution_state.resume_generation,
                    endpoint_id=target.endpoint.endpoint_id,
                    credential_slot_id=target.credential_slot_id,
                    model=target.endpoint.model,
                    provider=target.endpoint.provider,
                    input=invocation.input.model_dump(mode="json"),
                    timeout_seconds=timeout_seconds,
                    parent_span_id=invocation.trace.parent_span_id,
                    trace_id=invocation.trace.trace_id,
                )
            )
            execution_state.wire_attempts = ordinal
            try:
                with capture_attempt_stream(
                    stream,
                    model_call_id=invocation.model_call_id,
                    attempt_id=attempt_id,
                ) as stream_buffer:
                    async with asyncio.timeout(timeout_seconds):
                        attempt_executor = runtime_generation.attempt_executor
                        principal = runtime_generation.principal
                        scheduling = runtime_generation.scheduling
                        if (
                            attempt_executor is None
                            or principal is None
                            or scheduling is None
                            or not runtime_generation.generation_id
                            or not runtime_generation.generation_artifact_digest
                        ):
                            raise RuntimeError("model runtime generation is not executable")
                        now = datetime.now(timezone.utc)
                        request = InferenceAttemptRequest(
                            model_call_id=invocation.model_call_id,
                            owner_journal_id=invocation.model_call_id,
                            attempt_id=attempt_id,
                            generation_id=runtime_generation.generation_id,
                            generation_artifact_digest=(runtime_generation.generation_artifact_digest),
                            endpoint=target.endpoint,
                            credential_slot_id=target.credential_slot_id,
                            credential_version=target.binding.credential_version,
                            invocation=current_invocation.model_dump(mode="json"),
                            deadline=CrossProcessDeadline(
                                deadline_utc=now + timedelta(seconds=timeout_seconds),
                                remaining_seconds_at_send=timeout_seconds,
                                sent_at_utc=now,
                            ),
                            stream=stream,
                            artifact_reference=(
                                resolved_artifact.ref.content_ref if resolved_artifact is not None else None
                            ),
                            principal=principal,
                            scheduling=scheduling,
                        )

                        async def append_authorization(record):
                            await self._append_record(execution_state, record)

                        runtime_result = await attempt_executor.execute(
                            request,
                            ordinal=ordinal,
                            resume_generation=execution_state.resume_generation,
                            issued_journal_revision=len(execution_state.records) + 1,
                            append_authorization=append_authorization,
                        )
                        response = runtime_result.response
                if response.quota is not None:
                    self._admission_controller.observe_quota(
                        target.resource,
                        response.quota,
                    )
                if response.output.kind != invocation.operation.value:
                    attempt_facts.append(
                        {
                            "endpoint_id": target.endpoint.endpoint_id,
                            "credential_slot_id": target.credential_slot_id,
                            "failure_reason": FailureReason.PROTOCOL_INCOMPATIBLE.value,
                        }
                    )
                    raise _ClassifiedEndpointFailure(
                        FailureDisposition(
                            reason=FailureReason.PROTOCOL_INCOMPATIBLE,
                            domain=FailureDomain.PROTOCOL,
                            retryability=Retryability.NEW_ATTEMPT,
                            health_verdict=HealthVerdict.NEUTRAL,
                            provider_code="OUTPUT_KIND_MISMATCH",
                        ),
                        target,
                    )
            except asyncio.CancelledError:
                interrupt_attempt_stream(
                    stream_buffer,
                    model_call_id=invocation.model_call_id,
                    attempt_id=attempt_id,
                )
                finished = ModelAttemptFinishedRecord(
                    model_call_id=invocation.model_call_id,
                    attempt_id=attempt_id,
                    ordinal=ordinal,
                    resume_generation=execution_state.resume_generation,
                    state=AttemptState.CANCELLED,
                )
                await self._append_record(execution_state, finished)
                await self._observe_attempt_finished(invocation, target, finished, started_at)
                raise
            except _ClassifiedEndpointFailure as exc:
                discard_attempt_stream(
                    stream_buffer,
                    model_call_id=invocation.model_call_id,
                    attempt_id=attempt_id,
                    reason=exc.disposition.reason.value,
                )
                finished = ModelAttemptFinishedRecord(
                    model_call_id=invocation.model_call_id,
                    attempt_id=attempt_id,
                    ordinal=ordinal,
                    resume_generation=execution_state.resume_generation,
                    state=AttemptState.FAILED,
                    failure=exc.disposition,
                )
                await self._append_record(execution_state, finished)
                await self._observe_attempt_finished(invocation, target, finished, started_at)
                raise
            except Exception as exc:  # noqa: BLE001 — transport classification boundary
                if isinstance(exc, RuntimeAttemptFailure):
                    raw_disposition = exc.terminal.payload.get("disposition")
                    disposition = (
                        FailureDisposition.model_validate(raw_disposition)
                        if isinstance(raw_disposition, dict)
                        else self._classify(exc)
                    )
                else:
                    disposition = self._classify(exc)
                discard_attempt_stream(
                    stream_buffer,
                    model_call_id=invocation.model_call_id,
                    attempt_id=attempt_id,
                    reason=disposition.reason.value,
                )
                attempt_facts.append(
                    {
                        "endpoint_id": target.endpoint.endpoint_id,
                        "credential_slot_id": target.credential_slot_id,
                        "failure_reason": disposition.reason.value,
                    }
                )
                finished = ModelAttemptFinishedRecord(
                    model_call_id=invocation.model_call_id,
                    attempt_id=attempt_id,
                    ordinal=ordinal,
                    resume_generation=execution_state.resume_generation,
                    state=AttemptState.FAILED,
                    failure=disposition,
                )
                await self._append_record(execution_state, finished)
                await self._observe_attempt_finished(invocation, target, finished, started_at)
                raise _ClassifiedEndpointFailure(
                    disposition,
                    target,
                    cause=exc,
                ) from exc
            else:
                finished = ModelAttemptFinishedRecord(
                    model_call_id=invocation.model_call_id,
                    attempt_id=attempt_id,
                    ordinal=ordinal,
                    resume_generation=execution_state.resume_generation,
                    state=AttemptState.SUCCEEDED,
                    usage=response.usage,
                    cost_usd=response.cost_usd,
                )
                await self._append_record(execution_state, finished)
                await self._observe_attempt_finished(
                    invocation,
                    target,
                    finished,
                    started_at,
                    output=response.output.model_dump(mode="json"),
                )
                successful_target = target
                execution_state.successful_attempt_id = attempt_id
                commit_attempt_stream(
                    stream_buffer,
                    model_call_id=invocation.model_call_id,
                    attempt_id=attempt_id,
                )
                return response

        async def transform_request(
            target: _AttemptTarget,
            current_invocation: ModelInvocation,
            transform: RequestTransform,
            disposition: FailureDisposition,
            _exc: Exception,
        ) -> ModelInvocation | None:
            if request_transformer is None:
                return None
            return await request_transformer.transform(
                current_invocation,
                transform,
                disposition,
                target.endpoint,
            )

        def next_credential(target: _AttemptTarget) -> _AttemptTarget | None:
            pool = targets[target.endpoint.endpoint_id]
            index = next(
                (index for index, candidate in enumerate(pool) if candidate is target),
                -1,
            )
            return pool[index + 1] if 0 <= index < len(pool) - 1 else None

        def endpoint_selector_factory():
            index = 1

            def next_endpoint() -> _AttemptTarget | None:
                nonlocal index
                if index >= len(plan.endpoints):
                    return None
                endpoint = plan.endpoints[index]
                index += 1
                return targets[endpoint.endpoint_id][0]

            return next_endpoint

        def admit(target: _AttemptTarget, remaining_seconds: float) -> AdmissionResult:
            result = self._admission_controller.acquire(
                target.resource,
                remaining_seconds=min(
                    remaining_seconds,
                    plan.budget.single_attempt_timeout_seconds,
                ),
            )
            if result.rejection is not None:
                attempt_facts.append(
                    {
                        "endpoint_id": target.endpoint.endpoint_id,
                        "credential_slot_id": target.credential_slot_id,
                        "admission_gate": result.rejection.gate.value,
                        "failure_reason": (result.rejection.disposition.reason.value),
                    }
                )
                observe_event_sync(
                    ModelAttemptAdmissionRejectedEvent(
                        model_call_id=invocation.model_call_id,
                        resume_generation=execution_state.resume_generation,
                        endpoint_id=target.endpoint.endpoint_id,
                        credential_slot_id=target.credential_slot_id,
                        gate=result.rejection.gate.value,
                        reason=result.rejection.disposition.reason.value,
                        trace_id=invocation.trace.trace_id,
                    )
                )
            return result

        async def observe_decision(
            ordinal: int,
            decision: FailoverDecision,
            applied: FailoverDecision,
            before: _AttemptTarget,
            after: _AttemptTarget,
        ) -> None:
            actual = applied.model_copy(update={"target_endpoint_id": after.endpoint.endpoint_id})
            await self._append_record(
                execution_state,
                ModelDecisionRecord(
                    model_call_id=invocation.model_call_id,
                    resume_generation=execution_state.resume_generation,
                    after_attempt_ordinal=execution_state.wire_attempts,
                    decision=actual,
                    from_endpoint_id=before.endpoint.endpoint_id,
                    to_endpoint_id=after.endpoint.endpoint_id,
                    transform=actual.transform,
                ),
            )
            if actual.kind is DecisionKind.SWITCH_ENDPOINT:
                await observe_event(
                    ModelFallbackSelectedEvent(
                        model_call_id=invocation.model_call_id,
                        resume_generation=execution_state.resume_generation,
                        from_endpoint_id=before.endpoint.endpoint_id,
                        to_endpoint_id=after.endpoint.endpoint_id,
                        reason=actual.reason,
                        wire_attempts_used=execution_state.wire_attempts,
                        trace_id=invocation.trace.trace_id,
                    )
                )

        orchestrator = AttemptOrchestrator(
            budget=plan.budget,
            classifier=self._classify,
            provider_key=lambda target: target.endpoint.endpoint_id,
        )
        try:
            response = await orchestrator.run(
                execute_once=execute_once,
                primary=primary,
                request=invocation,
                next_credential=next_credential,
                endpoint_selector_factory=endpoint_selector_factory,
                request_transformer=transform_request,
                admit=admit,
                resume_seed=resume_seed,
                observe_decision=observe_decision,
            )
        except Exception as exc:  # noqa: BLE001 — aggregate one logical-call failure
            disposition = self._classify(exc)
            error_type = ModelCallDeadlineExceededError if time.monotonic() >= deadline else ModelCallExhaustedError
            raise error_type(
                f"model call {invocation.model_call_id!r} did not produce a response",
                cause=exc,
                model_call_id=invocation.model_call_id,
                plan_id=plan.plan_id,
                config_revision=plan.config_revision,
                wire_attempts=execution_state.wire_attempts,
                attempts=attempt_facts,
                last_failure=disposition.model_dump(mode="json"),
            ) from exc

        if successful_target is None:
            raise AssertionError("successful model call has no attempt target")
        target = successful_target
        if self._cost_tracker is not None:
            self._cost_tracker.record_settled(
                TokenUsage(
                    input_tokens=response.usage.input_tokens,
                    cached_input_tokens=response.usage.cache_read_tokens,
                    cache_creation_tokens=response.usage.cache_write_tokens,
                    output_tokens=response.usage.output_tokens,
                    reasoning_tokens=response.usage.reasoning_tokens,
                    total_tokens=response.usage.total_tokens,
                ),
                target.endpoint.model,
                float(response.cost_usd),
                context_window=target.endpoint.capabilities.context_tokens,
            )
        return ResolvedModelResponse(
            output=response.output,
            usage=response.usage,
            cost_usd=response.cost_usd,
            endpoint_id=target.endpoint.endpoint_id,
            endpoint_fingerprint=self._endpoint_fingerprint(target.endpoint),
            model_or_deployment=target.endpoint.model,
            tenant_fingerprint=target.tenant_fingerprint,
            credential_slot_id=target.credential_slot_id,
            provider=target.endpoint.provider,
            transport=target.endpoint.transport,
            model_call_id=invocation.model_call_id,
            successful_attempt_id=execution_state.successful_attempt_id,
        )

    def _resolve_targets(
        self,
        endpoints: tuple[EndpointDescriptor, ...],
        *,
        runtime_generation: ModelRuntimeGeneration,
    ) -> dict[str, tuple[_AttemptTarget, ...]]:
        resolved: dict[str, tuple[_AttemptTarget, ...]] = {}
        snapshot = runtime_generation.planner.snapshot
        for endpoint in endpoints:
            slot_ids = snapshot.slots_for_endpoint(endpoint.endpoint_id)
            if not slot_ids:
                raise ModelRouteUnavailableError(
                    f"endpoint {endpoint.endpoint_id!r} has no credential slots",
                    endpoint_id=endpoint.endpoint_id,
                    config_revision=snapshot.revision,
                )
            pool: list[_AttemptTarget] = []
            for slot_id in slot_ids:
                if runtime_generation.binding_resolver is None:
                    raise ModelRouteUnavailableError(
                        "model runtime generation has no binding resolver",
                        endpoint_id=endpoint.endpoint_id,
                        config_revision=snapshot.revision,
                    )
                binding = runtime_generation.binding_resolver.resolve(endpoint, slot_id)
                if binding is None:
                    raise ModelRouteUnavailableError(
                        f"endpoint {endpoint.endpoint_id!r} slot {slot_id!r} has no binding",
                        endpoint_id=endpoint.endpoint_id,
                        credential_slot_id=slot_id,
                        config_revision=snapshot.revision,
                    )
                resolved_endpoint = binding.endpoint.endpoint_id
                resolved_slot = binding.credential_slot_id
                tenant_fingerprint = binding.tenant_fingerprint
                if resolved_endpoint != endpoint.endpoint_id or resolved_slot != slot_id or not tenant_fingerprint:
                    raise ModelRouteUnavailableError(
                        "resolved endpoint identity does not match endpoint binding",
                        endpoint_id=endpoint.endpoint_id,
                        credential_slot_id=slot_id,
                        config_revision=snapshot.revision,
                    )
                pool.append(
                    _AttemptTarget(
                        endpoint=endpoint,
                        credential_slot_id=slot_id,
                        tenant_fingerprint=tenant_fingerprint,
                        binding=binding,
                        resource=ResourceIdentity(
                            endpoint_id=endpoint.endpoint_id,
                            transport=endpoint.transport,
                            endpoint_fingerprint=self._endpoint_fingerprint(endpoint),
                            model_or_deployment=endpoint.model,
                            tenant_fingerprint=tenant_fingerprint,
                            credential_slot_id=slot_id,
                        ),
                    )
                )
            resolved[endpoint.endpoint_id] = tuple(pool)
        return resolved

    @staticmethod
    def _classify(exc: Exception) -> FailureDisposition:
        if isinstance(exc, ModelCallJournalError):
            return FailureDisposition(
                reason=FailureReason.UNKNOWN,
                domain=FailureDomain.INTERNAL,
                retryability=Retryability.NEVER,
                health_verdict=HealthVerdict.NEUTRAL,
                provider_code="MODEL_CALL_JOURNAL_UNAVAILABLE",
            )
        if isinstance(exc, _ClassifiedEndpointFailure):
            return exc.disposition
        if isinstance(exc, AdmissionRejectedError):
            return exc.disposition
        return classify_failure(exc)

    @classmethod
    def _classify_chain(cls, exc: Exception) -> FailureDisposition:
        current: BaseException | None = exc
        while current is not None:
            if isinstance(current, Exception):
                disposition = cls._classify(current)
                if isinstance(current, ModelCallJournalError) or (disposition.reason is not FailureReason.UNKNOWN):
                    return disposition
            current = current.__cause__ or current.__context__
        return cls._classify(exc)

    async def _append_record(
        self,
        state: _CallExecutionState,
        record: ModelCallJournalRecord,
    ) -> None:
        if self._model_call_journal is not None:
            await self._model_call_journal.append(record)
        state.records.append(record)

    @staticmethod
    async def _resolve_artifact(
        invocation: ModelInvocation,
        resolver: ArtifactResolver | None,
    ) -> ResolvedArtifact | None:
        if not isinstance(invocation.input, ImageDescriptionInput):
            return None
        if resolver is None:
            raise ModelRouteUnavailableError(
                "image-description invocation requires an artifact resolver",
                model_call_id=invocation.model_call_id,
                artifact_id=invocation.input.artifact.artifact_id,
            )
        artifact = invocation.input.artifact
        return await resolver.resolve(
            artifact,
            ArtifactResolutionPolicy(
                max_bytes=artifact.size,
                allowed_sensitivities=frozenset({artifact.sensitivity}),
            ),
        )

    @staticmethod
    def _plan_record(
        invocation: ModelInvocation,
        plan: FailoverPlan,
        *,
        generation: int,
        root_started_at: datetime | None = None,
        runtime_generation_id: str,
        topology_revision: str,
    ) -> ModelCallPlannedRecord:
        occurred_at = datetime.now(timezone.utc)
        return ModelCallPlannedRecord(
            model_call_id=invocation.model_call_id,
            routing_decision_id=invocation.routing_decision_id,
            plan_id=plan.plan_id,
            route_id=encode_route_id(invocation.route_id),
            runtime_generation_id=runtime_generation_id,
            topology_revision=topology_revision,
            config_revision=plan.config_revision,
            endpoint_ids=tuple(endpoint.endpoint_id for endpoint in plan.endpoints),
            budget=plan.budget,
            policy_id=plan.policy_id,
            resume_generation=generation,
            root_started_at=root_started_at or occurred_at,
            occurred_at=occurred_at,
        )

    @staticmethod
    def _observe_planned(
        invocation: ModelInvocation,
        plan: FailoverPlan,
        generation: int,
        *,
        runtime_generation_id: str,
        topology_revision: str,
    ) -> None:
        observe_event_sync(
            ModelCallPlannedEvent(
                model_call_id=invocation.model_call_id,
                routing_decision_id=invocation.routing_decision_id or "",
                plan_id=plan.plan_id,
                route_id=encode_route_id(invocation.route_id),
                runtime_generation_id=runtime_generation_id,
                topology_revision=topology_revision,
                config_revision=plan.config_revision,
                policy_id=plan.policy_id,
                resume_generation=generation,
                endpoint_ids=[endpoint.endpoint_id for endpoint in plan.endpoints],
                budget=plan.budget.model_dump(mode="json"),
                trace_id=invocation.trace.trace_id,
            )
        )

    @staticmethod
    async def _observe_attempt_finished(
        invocation: ModelInvocation,
        target: _AttemptTarget,
        record: ModelAttemptFinishedRecord,
        started_at: float,
        *,
        output: JsonValue = None,
    ) -> None:
        await observe_event(
            ModelAttemptFinishedEvent(
                model_call_id=invocation.model_call_id,
                attempt_id=record.attempt_id,
                ordinal=record.ordinal,
                resume_generation=record.resume_generation,
                endpoint_id=target.endpoint.endpoint_id,
                state=record.state.value,
                failure_reason=(record.failure.reason.value if record.failure is not None else ""),
                latency_ms=max(time.monotonic() - started_at, 0.0) * 1000.0,
                usage=record.usage.model_dump(mode="json"),
                cost_usd=float(record.cost_usd),
                output=output,
                trace_id=invocation.trace.trace_id,
            )
        )

    def _build_summary(
        self,
        state: _CallExecutionState,
        plan: FailoverPlan,
        terminal_state: ModelCallState,
        *,
        selected_endpoint_id: str | None = None,
        last_failure: FailureDisposition | None = None,
    ) -> ModelCallSummary:
        starts = {
            record.attempt_id: record for record in state.records if isinstance(record, ModelAttemptStartedRecord)
        }
        finishes = {
            record.attempt_id: record for record in state.records if isinstance(record, ModelAttemptFinishedRecord)
        }
        decisions = tuple(record for record in state.records if isinstance(record, ModelDecisionRecord))
        attempts: list[AttemptSummary] = []
        known_usage = ModelUsage()
        known_cost = Decimal("0")
        in_doubt: list[str] = []
        for start in sorted(starts.values(), key=lambda item: item.ordinal):
            finish = finishes.get(start.attempt_id)
            attempt_state = finish.state if finish is not None else AttemptState.IN_DOUBT
            if attempt_state is AttemptState.IN_DOUBT:
                in_doubt.append(start.attempt_id)
            usage = finish.usage if finish is not None else ModelUsage()
            cost = finish.cost_usd if finish is not None else Decimal("0")
            known_usage = self._add_usage(known_usage, usage)
            known_cost += cost
            latency = max((finish.occurred_at - start.occurred_at).total_seconds(), 0.0) if finish is not None else 0.0
            attempts.append(
                AttemptSummary(
                    attempt_id=start.attempt_id,
                    ordinal=start.ordinal,
                    endpoint_id=start.endpoint_id,
                    credential_slot_id=start.credential_slot_id,
                    resume_generation=start.resume_generation,
                    state=attempt_state,
                    failure=finish.failure if finish is not None else None,
                    latency_seconds=latency,
                    usage=usage.model_dump(mode="json"),
                    cost_usd=cost,
                )
            )
        latest_plan = next(record for record in reversed(state.records) if isinstance(record, ModelCallPlannedRecord))
        root_plan = next(record for record in state.records if isinstance(record, ModelCallPlannedRecord))
        root_started_at = root_plan.root_started_at or root_plan.occurred_at
        if last_failure is None:
            last_failure = next(
                (finish.failure for finish in reversed(tuple(finishes.values())) if finish.failure is not None),
                None,
            )
        return ModelCallSummary(
            model_call_id=plan.model_call_id,
            routing_decision_id=latest_plan.routing_decision_id,
            plan_id=latest_plan.plan_id,
            config_revision=latest_plan.config_revision,
            policy_id=latest_plan.policy_id,
            resume_generation=latest_plan.resume_generation,
            state=terminal_state,
            attempts=tuple(attempts[-64:]),
            wire_attempts_used=len(starts),
            endpoint_switches=sum(record.decision.kind is DecisionKind.SWITCH_ENDPOINT for record in decisions),
            credential_rotations=sum(record.decision.kind is DecisionKind.ROTATE_CREDENTIAL for record in decisions),
            request_transforms=sum(record.decision.kind is DecisionKind.TRANSFORM_REQUEST for record in decisions),
            elapsed_seconds=max((datetime.now(timezone.utc) - root_started_at).total_seconds(), 0.0),
            selected_endpoint_id=selected_endpoint_id,
            known_usage=known_usage.model_dump(mode="json"),
            known_cost_usd=known_cost,
            in_doubt_attempt_ids=tuple(in_doubt),
            possible_duplicate_billing=bool(in_doubt),
            last_failure=last_failure,
        )

    @staticmethod
    def _add_usage(left: ModelUsage, right: ModelUsage) -> ModelUsage:
        return ModelUsage(**{name: getattr(left, name) + getattr(right, name) for name in ModelUsage.model_fields})

    @staticmethod
    def _usage_from_summary(summary: ModelCallSummary) -> ModelUsage:
        return ModelUsage.model_validate(summary.known_usage)

    @staticmethod
    async def _publish_finished(
        terminal: ModelCallFinishedRecord,
        sink: SessionFactSink | None,
    ) -> None:
        summary = terminal.summary
        if summary is None:
            return
        event = ModelCallFinishedEvent(
            model_call_id=terminal.model_call_id,
            state=terminal.state.value,
            selected_endpoint_id=terminal.selected_endpoint_id or "",
            wire_attempts=terminal.wire_attempts,
            usage=terminal.usage.model_dump(mode="json"),
            cost_usd=float(terminal.cost_usd),
        )
        await observe_event(event)
        if sink is not None:
            await sink.commit_fact(event)

    @staticmethod
    def _reinstate(recovery: ModelCallRecovery) -> ResolvedModelResponse:
        terminal = recovery.terminal
        if terminal is None or terminal.accepted_response is None:
            raise ModelCallInDoubtError(
                "successful model call has no durable accepted response checkpoint",
                model_call_id=recovery.model_call_id,
            )
        required = {
            "selected_endpoint_id": terminal.selected_endpoint_id,
            "endpoint_fingerprint": terminal.endpoint_fingerprint,
            "model_or_deployment": terminal.model_or_deployment,
            "tenant_fingerprint": terminal.tenant_fingerprint,
            "credential_slot_id": terminal.credential_slot_id,
        }
        if any(not value for value in required.values()):
            raise ModelCallInDoubtError(
                "accepted response checkpoint lacks resolved endpoint identity",
                model_call_id=recovery.model_call_id,
            )
        response = terminal.accepted_response
        return ResolvedModelResponse(
            output=response.output,
            usage=response.usage,
            cost_usd=response.cost_usd,
            endpoint_id=terminal.selected_endpoint_id or "",
            endpoint_fingerprint=terminal.endpoint_fingerprint or "",
            model_or_deployment=terminal.model_or_deployment or "",
            tenant_fingerprint=terminal.tenant_fingerprint or "",
            credential_slot_id=terminal.credential_slot_id or "",
            provider=terminal.provider or "unknown",
            transport=terminal.transport or "unknown",
            model_call_id=recovery.model_call_id,
            successful_attempt_id=terminal.successful_attempt_id,
        )

    def _resume_plan(
        self,
        candidate: FailoverPlan,
        recovery: ModelCallRecovery,
    ) -> tuple[FailoverPlan, AttemptResumeSeed]:
        original = recovery.original_plan.budget
        current = candidate.budget
        root_started_at = recovery.original_plan.root_started_at or recovery.original_plan.occurred_at
        elapsed = max((datetime.now(timezone.utc) - root_started_at).total_seconds(), 0.0)
        switches = sum(record.decision.kind is DecisionKind.SWITCH_ENDPOINT for record in recovery.decisions)
        rotations = sum(record.decision.kind is DecisionKind.ROTATE_CREDENTIAL for record in recovery.decisions)
        transforms = sum(record.decision.kind is DecisionKind.TRANSFORM_REQUEST for record in recovery.decisions)
        consumed = recovery.attempts_started
        max_wire = consumed + min(
            max(original.max_wire_attempts - consumed, 0),
            current.max_wire_attempts,
        )
        max_changes = max(max_wire - 1, 0)
        total_deadline = elapsed + min(
            max(original.total_deadline_seconds - elapsed, 0.0),
            current.total_deadline_seconds,
        )
        effective_budget = original.model_copy(
            update={
                "max_wire_attempts": max(max_wire, 1),
                "max_attempts_per_endpoint": min(
                    original.max_attempts_per_endpoint,
                    current.max_attempts_per_endpoint,
                    max(max_wire, 1),
                ),
                "max_endpoint_switches": min(
                    switches
                    + min(
                        max(original.max_endpoint_switches - switches, 0),
                        current.max_endpoint_switches,
                    ),
                    max_changes,
                ),
                "max_credential_rotations": min(
                    rotations
                    + min(
                        max(original.max_credential_rotations - rotations, 0),
                        current.max_credential_rotations,
                    ),
                    max_changes,
                ),
                "max_request_transforms": min(
                    transforms
                    + min(
                        max(original.max_request_transforms - transforms, 0),
                        current.max_request_transforms,
                    ),
                    max_changes,
                ),
                "total_deadline_seconds": max(total_deadline, 0.001),
                "single_attempt_timeout_seconds": min(
                    original.single_attempt_timeout_seconds,
                    current.single_attempt_timeout_seconds,
                    max(total_deadline, 0.001),
                ),
                "max_backoff_seconds": min(
                    original.max_backoff_seconds,
                    current.max_backoff_seconds,
                ),
            }
        )
        generation = recovery.plan.resume_generation + 1
        plan_id = hashlib.sha256(f"{candidate.plan_id}\0resume\0{generation}".encode("utf-8")).hexdigest()[:32]
        plan = candidate.model_copy(update={"plan_id": plan_id, "budget": effective_budget})
        attempts_by_endpoint: dict[str, int] = {}
        for start in recovery.attempt_starts:
            attempts_by_endpoint[start.endpoint_id] = attempts_by_endpoint.get(start.endpoint_id, 0) + 1
        return plan, AttemptResumeSeed(
            wire_attempts=consumed,
            attempts_by_provider=tuple(attempts_by_endpoint.items()),
            endpoint_switches=switches,
            credential_rotations=rotations,
            request_transforms=transforms,
            elapsed_seconds=elapsed,
        )

    @staticmethod
    def _endpoint_fingerprint(endpoint: EndpointDescriptor) -> str:
        physical_identity = "\0".join(
            (
                endpoint.transport,
                endpoint.provider,
                endpoint.base_url_identity,
                endpoint.model,
            )
        )
        return hashlib.sha256(physical_identity.encode("utf-8")).hexdigest()[:24]


class GenerationBoundRuntimeModelGateway:
    """Gateway view fenced to one immutable model Runtime generation."""

    def __init__(
        self,
        executor: RuntimeModelGateway,
        generation: ModelRuntimeGeneration,
        runtime_generation_id: str,
    ) -> None:
        self._executor = executor
        self._generation = generation
        self._runtime_generation_id = runtime_generation_id

    @property
    def topology_revision(self) -> str:
        return self._generation.revision

    def supports_route(self, route_id: RouteId) -> bool:
        return self._generation.planner.snapshot.group_for_route(route_id) is not None

    def route_profile(self, route_id: RouteId) -> EndpointDescriptor | None:
        snapshot = self._generation.planner.snapshot
        group = snapshot.group_for_route(route_id)
        if group is None or not group.endpoint_ids:
            return None
        profiles = tuple(
            endpoint
            for endpoint_id in group.endpoint_ids
            if (endpoint := snapshot.endpoint(endpoint_id)) is not None
            and ModelOperation.GENERATE in endpoint.capabilities.supported_operations
        )
        if not profiles:
            return None
        if not endpoints_are_projection_compatible(profiles, ModelOperation.GENERATE):
            raise ModelCapabilityUnsatisfiedError(
                f"route {route_id!r} mixes projection-incompatible endpoints",
                route_id=encode_route_id(route_id),
                group_id=group.group_id,
                operation=ModelOperation.GENERATE.value,
                candidates=[endpoint.endpoint_id for endpoint in profiles],
                config_revision=snapshot.revision,
            )
        return min(profiles, key=lambda endpoint: endpoint.endpoint_id)

    def route_profiles(self, route_id: RouteId) -> tuple[EndpointDescriptor, ...]:
        snapshot = self._generation.planner.snapshot
        group = snapshot.group_for_route(route_id)
        if group is None:
            return ()
        return tuple(
            endpoint for endpoint_id in group.endpoint_ids if (endpoint := snapshot.endpoint(endpoint_id)) is not None
        )

    def inspect_recovery(self, model_call_id: str) -> ModelRecoveryInspection:
        return self._executor.inspect_recovery(model_call_id)

    async def execute(
        self,
        invocation: ModelInvocation,
        *,
        request_transformer: ModelRequestTransformer | None = None,
        stream: bool = False,
        session_fact_sink: SessionFactSink | None = None,
        artifact_resolver: ArtifactResolver | None = None,
    ) -> ResolvedModelResponse:
        return await self._executor.execute_generation(
            self._generation,
            invocation,
            request_transformer=request_transformer,
            stream=stream,
            session_fact_sink=session_fact_sink,
            artifact_resolver=artifact_resolver,
            runtime_generation_id=self._runtime_generation_id,
        )

    async def resume(
        self,
        invocation: ModelInvocation,
        *,
        request_transformer: ModelRequestTransformer | None = None,
        stream: bool = False,
        session_fact_sink: SessionFactSink | None = None,
        artifact_resolver: ArtifactResolver | None = None,
    ) -> ResolvedModelResponse:
        return await self._executor.resume_generation(
            self._generation,
            invocation,
            request_transformer=request_transformer,
            stream=stream,
            session_fact_sink=session_fact_sink,
            artifact_resolver=artifact_resolver,
            runtime_generation_id=self._runtime_generation_id,
        )


__all__ = ["GenerationBoundRuntimeModelGateway", "RuntimeModelGateway"]
