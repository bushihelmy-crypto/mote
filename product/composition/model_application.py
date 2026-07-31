"""Product-owned atomic application generation lifecycle.

This module is deliberately not wired into a production composition root until
the model consumers can switch as one unit.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Protocol
from uuid import uuid4

from mote.contracts.events.application import (
    ApplicationActivationCasConflict,
    ApplicationActivationCommitted,
    ApplicationActivationRejected,
    ApplicationActivationRequested,
    ApplicationActivationStale,
    ApplicationShutdownTimedOut,
    CompositionCloseFailed,
    GenerationDrainCompleted,
    GenerationDrainTimedOut,
    RetiredGenerationCapacityReached,
)
from mote.contracts.runtime.application import (
    ActivationReceipt,
    ActivationToken,
    ApplicationClosedError,
    ApplicationGenerationId,
    ApplicationHealth,
    ApplicationNotReadyError,
    ApplicationShuttingDownError,
    ApplicationState,
    ExpectedActive,
    ExpectedApplicationState,
    ExpectedEmpty,
    ExpectedStateMismatchError,
    ReloadSequence,
    RetiredGenerationCapacityError,
    RuntimeCompositionLeasePort,
    RuntimeGenerationId,
    RuntimeRoleConfigView,
    SourceRevision,
    StaleReloadError,
)
from mote.runtime.events import observe_event_sync
from mote.runtime.telemetry.logging import log_class


class AsyncResource(Protocol):
    async def aclose(self) -> None:
        ...


class SharedRuntimeCompositionHandle(Protocol):
    @property
    def runtime_generation_id(self) -> RuntimeGenerationId:
        ...

    @property
    def topology_revision(self) -> str:
        ...

    def retain(self) -> "SharedRuntimeCompositionHandle":
        ...

    async def acquire(self) -> RuntimeCompositionLeasePort:
        ...

    async def release(self) -> None:
        ...


class CandidateState(str, Enum):
    NEW = "new"
    COMMITTED = "committed"
    REJECTED = "rejected"


class CandidateConsumedError(RuntimeError):
    pass


class CompositionCloseError(RuntimeError):
    def __init__(self, errors: tuple[BaseException, ...]) -> None:
        super().__init__(f"failed to close {len(errors)} composition resource(s)")
        self.errors = errors


async def _close_resources(resources: tuple[AsyncResource, ...], model: SharedRuntimeCompositionHandle) -> None:
    errors: list[BaseException] = []
    for resource in resources:
        try:
            await resource.aclose()
        except BaseException as exc:
            errors.append(exc)
    try:
        await model.release()
    except BaseException as exc:
        errors.append(exc)
    if errors:
        raise CompositionCloseError(tuple(errors))


class ApplicationCompositionCandidate:
    """Move-only candidate whose resources have exactly one owner."""

    __slots__ = (
        "source_revision",
        "reload_sequence",
        "model",
        "runtime_role_config",
        "product_config",
        "product_resources",
        "_state",
    )

    def __init__(
        self,
        *,
        source_revision: SourceRevision,
        reload_sequence: ReloadSequence,
        model: SharedRuntimeCompositionHandle,
        runtime_role_config: RuntimeRoleConfigView,
        product_config: Any = None,
        product_resources: tuple[AsyncResource, ...] = (),
    ) -> None:
        self.source_revision = source_revision
        self.reload_sequence = reload_sequence
        self.model = model
        self.runtime_role_config = runtime_role_config
        self.product_config = product_config
        self.product_resources = product_resources
        self._state = CandidateState.NEW

    @property
    def state(self) -> CandidateState:
        return self._state

    def __copy__(self):
        raise TypeError("application composition candidates cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("application composition candidates cannot be copied")

    def __reduce__(self):
        raise TypeError("application composition candidates cannot be serialized")

    def _commit(self) -> None:
        if self._state is not CandidateState.NEW:
            raise CandidateConsumedError(f"candidate is already {self._state.value}")
        self._state = CandidateState.COMMITTED

    async def reject(self) -> None:
        if self._state is CandidateState.COMMITTED:
            raise CandidateConsumedError("committed candidate is owned by the container")
        if self._state is CandidateState.REJECTED:
            return
        self._state = CandidateState.REJECTED
        await _close_resources(self.product_resources, self.model)

    async def aclose(self) -> None:
        await self.reject()


@dataclass(slots=True)
class _Generation:
    generation_id: ApplicationGenerationId
    source_revision: SourceRevision
    reload_sequence: ReloadSequence
    model: SharedRuntimeCompositionHandle
    runtime_role_config: RuntimeRoleConfigView
    product_config: Any
    product_resources: tuple[AsyncResource, ...]
    leases: int = 0
    retired_at: float | None = None
    closed: bool = False

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        await _close_resources(self.product_resources, self.model)


class ApplicationLease:
    __slots__ = ("_container", "_generation", "_released")

    def __init__(self, container: "AtomicApplicationComposition", generation: _Generation) -> None:
        self._container = container
        self._generation = generation
        self._released = False

    @property
    def application_generation_id(self) -> ApplicationGenerationId:
        return self._generation.generation_id

    @property
    def runtime_generation_id(self) -> RuntimeGenerationId:
        return self._generation.model.runtime_generation_id

    @property
    def runtime_role_config(self) -> RuntimeRoleConfigView:
        return self._generation.runtime_role_config

    @property
    def product_config(self) -> Any:
        """Product-owned immutable view; absent from the contracts lease port."""
        return deepcopy(self._generation.product_config)

    async def __aenter__(self) -> "ApplicationLease":
        if self._released:
            raise RuntimeError("application lease was released")
        return self

    async def acquire_runtime(self) -> RuntimeCompositionLeasePort:
        if self._released:
            raise RuntimeError("application lease was released")
        return await self._generation.model.acquire()

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._released:
            return
        self._released = True
        await self._container._release(self._generation)


class ActivationDisposition(str, Enum):
    COMMITTED = "committed"
    NOT_COMMITTED = "not_committed"
    PENDING = "pending"
    EXPIRED_CALLER_MUST_NOT_CLOSE = "expired_caller_must_not_close"
    UNKNOWN_TOKEN = "unknown_token"


@dataclass(frozen=True, slots=True)
class ActivationResult:
    disposition: ActivationDisposition
    receipt: ActivationReceipt | None = None
    error: BaseException | None = None


@dataclass(slots=True)
class _ActivationRecord:
    future: asyncio.Future[ActivationResult]
    finalized_at: float | None = None


@dataclass(frozen=True, slots=True)
class ShutdownLeaseTimeout:
    pending_generation_ids: tuple[ApplicationGenerationId, ...]


class ActivationLedgerCapacityError(RuntimeError):
    pass


@log_class(
    level="DEBUG",
    exclude={
        "state",
        "health",
        "current_generation_id",
        "acquire",
        "activate",
        "activation_result",
    },
)
class AtomicApplicationComposition:
    """The sole current-pointer owner for immutable application generations."""

    def __init__(
        self,
        *,
        retired_limit: int = 2,
        ledger_limit: int = 4096,
        ledger_retention_seconds: float = 86_400.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._state = ApplicationState.EMPTY
        self._current: _Generation | None = None
        self._retired: list[_Generation] = []
        self._retired_limit = retired_limit
        self._ledger_limit = ledger_limit
        self._ledger_retention_seconds = ledger_retention_seconds
        self._clock = clock
        self._lock = asyncio.Lock()
        self._latest_requested_sequence = 0
        self._latest_source_revision: SourceRevision | None = None
        self._last_committed_sequence = 0
        self._token_secret = uuid4().hex
        self._issued_token_nonces: set[str] = set()
        self._activation_results: dict[str, _ActivationRecord] = {}
        self._close_errors: list[CompositionCloseError] = []

    @property
    def state(self) -> ApplicationState:
        return self._state

    @property
    def health(self) -> ApplicationHealth:
        if self._state is ApplicationState.EMPTY:
            return ApplicationHealth.NOT_READY
        if self._state is ApplicationState.SHUTTING_DOWN:
            return ApplicationHealth.SHUTTING_DOWN
        if self._state is ApplicationState.CLOSED:
            return ApplicationHealth.CLOSED
        if self._close_errors or len(self._retired) >= self._retired_limit:
            return ApplicationHealth.DEGRADED
        return ApplicationHealth.READY

    @property
    def current_generation_id(self) -> ApplicationGenerationId | None:
        return self._current.generation_id if self._current is not None else None

    async def retain_current_model(self) -> SharedRuntimeCompositionHandle:
        """Retain the active model handle for one Product-side reuse decision."""

        async with self._lock:
            if self._state is ApplicationState.EMPTY:
                raise ApplicationNotReadyError("application composition is empty")
            if self._state is ApplicationState.SHUTTING_DOWN:
                raise ApplicationShuttingDownError("application composition is shutting down")
            if self._state is ApplicationState.CLOSED:
                raise ApplicationClosedError("application composition is closed")
            assert self._current is not None
            return self._current.model.retain()

    def accept_reload_request(self, source_revision: SourceRevision) -> ReloadSequence:
        self._latest_requested_sequence += 1
        self._latest_source_revision = source_revision
        return ReloadSequence(self._latest_requested_sequence)

    def issue_activation_token(self) -> ActivationToken:
        self._prune_ledger()
        if len(self._activation_results) >= self._ledger_limit:
            raise ActivationLedgerCapacityError("activation result ledger capacity reached")
        nonce = uuid4().hex
        signature = hmac.digest(self._token_secret.encode(), nonce.encode(), "sha256").hex()
        token = ActivationToken(f"{nonce}.{signature}")
        self._issued_token_nonces.add(nonce)
        loop = asyncio.get_running_loop()
        self._activation_results[token.value] = _ActivationRecord(loop.create_future())
        return token

    async def acquire(self) -> ApplicationLease:
        async with self._lock:
            if self._state is ApplicationState.EMPTY:
                raise ApplicationNotReadyError("application composition is empty")
            if self._state is ApplicationState.SHUTTING_DOWN:
                raise ApplicationShuttingDownError("application composition is shutting down")
            if self._state is ApplicationState.CLOSED:
                raise ApplicationClosedError("application composition is closed")
            assert self._current is not None
            self._current.leases += 1
            return ApplicationLease(self, self._current)

    async def activate(
        self,
        candidate: ApplicationCompositionCandidate,
        token: ActivationToken,
        expected: ExpectedApplicationState,
    ) -> ActivationReceipt:
        observe_event_sync(
            ApplicationActivationRequested(
                token_fingerprint=hashlib.sha256(token.value.encode()).hexdigest()[:16],
                reload_sequence=candidate.reload_sequence.value,
                source_revision=candidate.source_revision.value,
            )
        )
        record = self._activation_results.get(token.value)
        if record is None or not self._is_authentic(token):
            raise ValueError("activation token was not issued by this container")
        if record.future.done():
            raise ValueError("activation token was already consumed")
        try:
            await self._lock.acquire()
        except asyncio.CancelledError as exc:
            self._finalize_record(
                record,
                ActivationResult(ActivationDisposition.NOT_COMMITTED, error=exc),
            )
            # A queued cancellation never transfers or closes caller ownership.
            raise
        try:
            if candidate.state is not CandidateState.NEW:
                raise CandidateConsumedError("candidate can only be installed once")
            try:
                self._ensure_activation_allowed(expected)
            except ExpectedStateMismatchError:
                observe_event_sync(
                    ApplicationActivationCasConflict(
                        expected_generation_id=(
                            expected.generation_id.value if isinstance(expected, ExpectedActive) else "empty"
                        ),
                        current_generation_id=(
                            self._current.generation_id.value if self._current is not None else "empty"
                        ),
                    )
                )
                raise
            if candidate.reload_sequence.value != self._latest_requested_sequence:
                observe_event_sync(
                    ApplicationActivationStale(
                        candidate_sequence=candidate.reload_sequence.value,
                        latest_sequence=self._latest_requested_sequence,
                        source_revision_mismatch=(candidate.source_revision != self._latest_source_revision),
                    )
                )
                raise StaleReloadError("candidate is not the latest accepted reload")
            if candidate.source_revision != self._latest_source_revision:
                observe_event_sync(
                    ApplicationActivationStale(
                        candidate_sequence=candidate.reload_sequence.value,
                        latest_sequence=self._latest_requested_sequence,
                        source_revision_mismatch=True,
                    )
                )
                raise StaleReloadError("candidate source revision is no longer current")
            if candidate.reload_sequence.value <= self._last_committed_sequence:
                raise StaleReloadError("candidate sequence was already superseded")
            if self._current is not None and len(self._retired) >= self._retired_limit:
                observe_event_sync(
                    RetiredGenerationCapacityReached(
                        retired_count=len(self._retired),
                        limit=self._retired_limit,
                        oldest_age_bucket="long_lived",
                    )
                )
                raise RetiredGenerationCapacityError("retired generation capacity reached")
            generation = _Generation(
                generation_id=ApplicationGenerationId(uuid4().hex),
                source_revision=candidate.source_revision,
                reload_sequence=candidate.reload_sequence,
                model=candidate.model,
                runtime_role_config=candidate.runtime_role_config,
                product_config=deepcopy(candidate.product_config),
                product_resources=candidate.product_resources,
            )
            old = self._current
            candidate._commit()
            self._current = generation
            self._state = ApplicationState.ACTIVE
            self._last_committed_sequence = candidate.reload_sequence.value
            if old is not None:
                old.retired_at = self._clock()
                self._retired.append(old)
            receipt = ActivationReceipt(
                generation.generation_id,
                generation.model.runtime_generation_id,
                generation.source_revision,
                generation.reload_sequence,
            )
        except BaseException as exc:
            self._lock.release()
            close_error: BaseException | None = None
            if candidate.state is CandidateState.NEW:
                try:
                    await candidate.reject()
                except BaseException as close_exc:
                    close_error = close_exc
            result_error = close_error or exc
            self._finalize_record(
                record,
                ActivationResult(ActivationDisposition.NOT_COMMITTED, error=result_error),
            )
            observe_event_sync(
                ApplicationActivationRejected(
                    reload_sequence=candidate.reload_sequence.value,
                    reason_code=type(exc).__name__,
                )
            )
            raise
        else:
            self._lock.release()
            self._finalize_record(
                record,
                ActivationResult(ActivationDisposition.COMMITTED, receipt=receipt),
            )
            observe_event_sync(
                ApplicationActivationCommitted(
                    application_generation_id=receipt.application_generation_id.value,
                    runtime_generation_id=receipt.runtime_generation_id.value,
                    topology_revision=generation.model.topology_revision,
                    source_revision=receipt.source_revision.value,
                )
            )
        await self._close_drained()
        return receipt

    async def activation_result(self, token: ActivationToken) -> ActivationResult:
        if not self._is_authentic(token):
            return ActivationResult(ActivationDisposition.UNKNOWN_TOKEN)
        record = self._activation_results.get(token.value)
        if record is None:
            return ActivationResult(ActivationDisposition.EXPIRED_CALLER_MUST_NOT_CLOSE)
        return await asyncio.shield(record.future)

    def _finalize_record(self, record: _ActivationRecord, result: ActivationResult) -> None:
        if record.future.done():
            return
        record.finalized_at = self._clock()
        record.future.set_result(result)

    def _is_authentic(self, token: ActivationToken) -> bool:
        try:
            nonce, signature = token.value.split(".", 1)
        except ValueError:
            return False
        expected = hmac.digest(self._token_secret.encode(), nonce.encode(), "sha256").hex()
        return nonce in self._issued_token_nonces and hmac.compare_digest(signature, expected)

    def _prune_ledger(self) -> None:
        now = self._clock()
        expired = [
            token
            for token, record in self._activation_results.items()
            if record.finalized_at is not None and now - record.finalized_at >= self._ledger_retention_seconds
        ]
        for token in expired:
            del self._activation_results[token]

    def _ensure_activation_allowed(self, expected: ExpectedApplicationState) -> None:
        if self._state is ApplicationState.CLOSED:
            raise ApplicationClosedError("application composition is closed")
        if self._state is ApplicationState.SHUTTING_DOWN:
            raise ApplicationShuttingDownError("application composition is shutting down")
        if self._state is ApplicationState.EMPTY:
            if not isinstance(expected, ExpectedEmpty):
                raise ExpectedStateMismatchError("empty container requires ExpectedEmpty")
            return
        if not isinstance(expected, ExpectedActive) or self._current is None:
            raise ExpectedStateMismatchError("active container requires ExpectedActive")
        if expected.generation_id != self._current.generation_id:
            raise ExpectedStateMismatchError("active generation CAS mismatch")

    async def _release(self, generation: _Generation) -> None:
        async with self._lock:
            if generation.leases > 0:
                generation.leases -= 1
        await self._close_drained()

    async def _close_drained(self) -> None:
        drained: list[_Generation] = []
        async with self._lock:
            retired = self._retired
            retained: list[_Generation] = []
            for generation in retired:
                (drained if generation.leases == 0 else retained).append(generation)
            self._retired = retained
            if self._state is ApplicationState.SHUTTING_DOWN and not self._retired:
                if self._current is None or self._current.leases == 0:
                    if self._current is not None:
                        drained.append(self._current)
                    self._current = None
                    self._state = ApplicationState.CLOSED
        for generation in drained:
            try:
                await generation.aclose()
                observe_event_sync(
                    GenerationDrainCompleted(
                        generation_id=generation.generation_id.value,
                        duration_bucket="completed",
                    )
                )
            except CompositionCloseError as exc:
                self._close_errors.append(exc)
                observe_event_sync(
                    CompositionCloseFailed(
                        resource_kind="application_generation",
                        resource_identity=generation.generation_id.value,
                        error_code=type(exc).__name__,
                        error_count=len(exc.errors),
                    )
                )

    async def shutdown(self, timeout: float = 30.0) -> ShutdownLeaseTimeout | None:
        async with self._lock:
            if self._state is ApplicationState.CLOSED:
                return None
            if self._state is ApplicationState.EMPTY:
                self._state = ApplicationState.CLOSED
                self._terminate_pending_activations()
                return None
            self._state = ApplicationState.SHUTTING_DOWN
            self._terminate_pending_activations()
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            await self._close_drained()
            if self._state is ApplicationState.CLOSED:
                return None
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.01, remaining))
        async with self._lock:
            generations = tuple(self._retired) + ((self._current,) if self._current is not None else ())
            pending = tuple(g.generation_id for g in generations if g.leases > 0)
            lease_count = sum(g.leases for g in generations)
        observe_event_sync(
            ApplicationShutdownTimedOut(
                generation_count=len(generations),
                lease_count=lease_count,
                oldest_age_bucket="timeout",
            )
        )
        for generation in generations:
            if generation.leases:
                observe_event_sync(
                    GenerationDrainTimedOut(
                        generation_id=generation.generation_id.value,
                        lease_count=generation.leases,
                        age_bucket="timeout",
                    )
                )
        return ShutdownLeaseTimeout(pending)

    def _terminate_pending_activations(self) -> None:
        error = ApplicationClosedError("application composition is shutting down")
        for record in self._activation_results.values():
            if not record.future.done():
                self._finalize_record(
                    record,
                    ActivationResult(
                        ActivationDisposition.NOT_COMMITTED,
                        error=error,
                    ),
                )

    async def aclose(self) -> None:
        pending = await self.shutdown()
        if pending is not None:
            raise RuntimeError(f"application composition shutdown timed out: {pending.pending_generation_ids!r}")


__all__ = [
    "ActivationDisposition",
    "ActivationLedgerCapacityError",
    "ActivationResult",
    "ApplicationCompositionCandidate",
    "ApplicationLease",
    "AtomicApplicationComposition",
    "CandidateConsumedError",
    "CandidateState",
    "CompositionCloseError",
    "SharedRuntimeCompositionHandle",
    "ShutdownLeaseTimeout",
]
