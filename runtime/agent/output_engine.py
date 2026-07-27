"""Run-scoped output candidate decoding and validation."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Generic, TypeVar, cast
from uuid import uuid4

from pydantic import ValidationError

from mote.contracts.output import (
    Accept,
    CommittedOutput,
    Corrected,
    OutputDecodeError,
    OutputEvaluation,
    OutputLifecycleState,
    Reject,
    RetryLater,
    RunKind,
    ValidationContext,
    ValidationIssue,
    ValidationStage,
    ValidatorProvenance,
)
from mote.contracts.ports import CommitFence, SessionFactSink
from mote.kernel.output import OutputContract
from mote.runtime.errors import (
    MoteError,
    OutputCommitStateError,
    OutputResumeContractMismatchError,
    OutputValidatorError,
    OutputValidatorUnavailableError,
)
from mote.runtime.events import (
    OutputAcceptedEvent,
    OutputCandidateReceivedEvent,
    OutputCommitStartedEvent,
    OutputCommittedEvent,
    OutputMigratedEvent,
    OutputValidationRejectedEvent,
    observe_event,
)

OutputT = TypeVar("OutputT")


async def _noop_async() -> None:
    return None


class OutputEngine(Generic[OutputT]):
    def __init__(
        self,
        contract: OutputContract[OutputT],
        restored_state: dict | None = None,
        run_id: str | None = None,
        run_kind: RunKind = RunKind.AGENT,
        commit_fence: CommitFence | None = None,
        fencing_token: int | None = None,
        drain_writes: Callable[[], Awaitable[None]] | None = None,
        session_fact_sink: SessionFactSink | None = None,
    ) -> None:
        self.contract = contract
        self.run_id = run_id or uuid4().hex
        self.run_kind = run_kind
        if (commit_fence is None) != (fencing_token is None):
            raise ValueError("commit_fence and fencing_token must be provided together")
        self._commit_fence = commit_fence
        self._fencing_token = fencing_token
        self._drain_writes = drain_writes or _noop_async
        self._session_fact_sink = session_fact_sink
        self.accepted_value: OutputT | None = None
        self.accepted_candidate_id = ""
        self.correction_attempts = 0
        self.committed_output: CommittedOutput[OutputT] | None = None
        self.validator_provenance: tuple[ValidatorProvenance, ...] = ()
        self.state = OutputLifecycleState.IDLE
        self.restored = False
        self._pending_migration: dict | None = None
        if restored_state is not None:
            self._restore(restored_state)

    async def _publish_session_fact(self, event: object) -> None:
        if self._session_fact_sink is not None:
            await self._session_fact_sink.commit_fact(event)
        await observe_event(event)

    def _restore(self, state: dict) -> None:
        """Restore the durable portion of one unfinished output lifecycle."""
        contract_id = str(self.contract.contract_id)
        fingerprint = self.contract.decoder.schema.fingerprint
        recorded_contract = state.get("contract_id")
        recorded_fingerprint = state.get("schema_fingerprint")
        if recorded_contract != contract_id or recorded_fingerprint != fingerprint:
            registry = self.contract.migration_registry
            try:
                if state.get("status") not in {
                    "accepted",
                    "commit_started",
                    "committed",
                    "publication_queued",
                }:
                    raise ValueError("only accepted output can be migrated")
                if registry is None:
                    raise ValueError("no migration registry")
                migrated_value, applied = registry.migrate(
                    state.get("value"),
                    source_contract_id=str(recorded_contract or ""),
                    source_schema_fingerprint=str(recorded_fingerprint or ""),
                    target_contract_id=contract_id,
                    target_schema_fingerprint=fingerprint,
                )
            except (TypeError, ValueError) as exc:
                raise OutputResumeContractMismatchError(
                    "cannot resume output under a different contract",
                    recorded_contract_id=recorded_contract,
                    current_contract_id=contract_id,
                    recorded_schema_fingerprint=recorded_fingerprint,
                    current_schema_fingerprint=fingerprint,
                ) from exc
            state = dict(state)
            state.update(
                status="accepted",
                contract_id=contract_id,
                schema_fingerprint=fingerprint,
                value=migrated_value,
            )
            self._pending_migration = {
                "source_contract_id": recorded_contract,
                "target_contract_id": contract_id,
                "steps": [asdict(item) for item in applied],
            }
            recorded_contract = contract_id
            recorded_fingerprint = fingerprint
        attempts = int(state.get("correction_attempts", 0))
        if attempts < 0 or attempts > self.contract.retry_policy.max_corrections:
            raise OutputResumeContractMismatchError(
                "persisted output correction budget is incompatible with the contract",
                correction_attempts=attempts,
                max_corrections=self.contract.retry_policy.max_corrections,
            )
        self.correction_attempts = attempts
        self.run_id = str(state.get("run_id") or self.run_id)
        try:
            recorded_run_kind = RunKind(state.get("run_kind", RunKind.AGENT.value))
        except ValueError as exc:
            raise OutputResumeContractMismatchError(
                "persisted output run kind is unknown",
                run_kind=state.get("run_kind"),
            ) from exc
        if recorded_run_kind is not self.run_kind:
            raise OutputResumeContractMismatchError(
                "persisted output belongs to a different run kind",
                recorded_run_kind=recorded_run_kind.value,
                current_run_kind=self.run_kind.value,
            )
        self.validator_provenance = tuple(ValidatorProvenance(**item) for item in state.get("validator_provenance", ()))
        target_validator_identities = {(validator.name, validator.version) for validator in self.contract.validators}
        recorded_validator_identities = {(item.name, item.version) for item in self.validator_provenance}
        if recorded_validator_identities != target_validator_identities:
            registry = self.contract.validator_migration_registry
            if registry is not None:
                try:
                    self.validator_provenance = registry.migrate(self.validator_provenance, target_validator_identities)
                except ValueError as exc:
                    raise OutputResumeContractMismatchError(
                        "persisted validator provenance cannot be migrated"
                    ) from exc
        try:
            restored_state = OutputLifecycleState(str(state.get("status") or ""))
        except ValueError as exc:
            raise OutputResumeContractMismatchError(
                "persisted output lifecycle state is unknown",
                status=state.get("status"),
            ) from exc
        self.state = restored_state
        self.restored = True
        if restored_state in {
            OutputLifecycleState.ACCEPTED,
            OutputLifecycleState.COMMIT_STARTED,
            OutputLifecycleState.COMMITTED,
            OutputLifecycleState.PUBLICATION_QUEUED,
        }:
            expected_validators = sorted(
                (
                    validator.name,
                    validator.version,
                    validator.stage.value,
                    validator.effect.value,
                    validator.determinism.value,
                )
                for validator in self.contract.validators
            )
            recorded_validators = sorted(
                (
                    item.name,
                    item.version,
                    item.stage,
                    item.effect,
                    item.determinism,
                )
                for item in self.validator_provenance
            )
            if recorded_validators != expected_validators:
                raise OutputResumeContractMismatchError(
                    "persisted validator provenance does not match the contract",
                    recorded_validators=recorded_validators,
                    expected_validators=expected_validators,
                )
            self.accepted_value = self.contract.decoder.decode(state.get("value"))
            self.accepted_candidate_id = str(state.get("candidate_id") or "")
        if restored_state in {
            OutputLifecycleState.COMMITTED,
            OutputLifecycleState.PUBLICATION_QUEUED,
        }:
            self.committed_output = CommittedOutput(
                candidate_id=self.accepted_candidate_id,
                contract_id=contract_id,
                schema_fingerprint=fingerprint,
                value=cast(OutputT, self.accepted_value),
                correction_attempts=attempts,
                validator_provenance=self.validator_provenance,
                run_id=self.run_id,
                run_kind=self.run_kind,
                fencing_token=int(state.get("fencing_token", 0)),
            )

    @property
    def accepted(self) -> bool:
        return self.state in {
            OutputLifecycleState.ACCEPTED,
            OutputLifecycleState.COMMIT_STARTED,
            OutputLifecycleState.COMMITTED,
            OutputLifecycleState.PUBLICATION_QUEUED,
        }

    @property
    def committed(self) -> bool:
        return self.state in {
            OutputLifecycleState.COMMITTED,
            OutputLifecycleState.PUBLICATION_QUEUED,
        }

    @property
    def has_restored_terminal_output(self) -> bool:
        """Whether resume can finish this lifecycle without another model call."""
        return self.restored and self.accepted

    async def evaluate(self, candidate) -> OutputEvaluation[OutputT]:
        if self.accepted:
            raise OutputCommitStateError(
                "cannot evaluate another candidate after output acceptance",
                state=self.state.value,
            )
        candidate_id = candidate.candidate_id or uuid4().hex
        contract_id = str(self.contract.contract_id)
        schema_fingerprint = self.contract.decoder.schema.fingerprint
        self.state = OutputLifecycleState.CANDIDATE_RECEIVED
        await self._publish_session_fact(
            OutputCandidateReceivedEvent(
                candidate_id=candidate_id,
                contract_id=contract_id,
                schema_fingerprint=schema_fingerprint,
                representation=candidate.representation,
                raw=candidate.raw,
                run_id=self.run_id,
                run_kind=self.run_kind.value,
            )
        )
        try:
            value = self.contract.decoder.decode(candidate.raw)
        except OutputDecodeError as exc:
            return await self._reject(candidate_id, contract_id, exc.issues)
        except ValidationError as exc:
            issues = tuple(
                ValidationIssue(
                    path=tuple(item.get("loc") or ()),
                    code=str(item.get("type") or "validation_error"),
                    message=str(item.get("msg") or "invalid output"),
                )
                for item in exc.errors(include_url=False)
            )
            return await self._reject(candidate_id, contract_id, issues)
        context = ValidationContext(
            candidate_id=candidate_id,
            contract_id=contract_id,
            correction_attempts=self.correction_attempts,
        )
        stage_order = {
            ValidationStage.STRUCTURAL: 0,
            ValidationStage.SEMANTIC: 1,
            ValidationStage.POLICY: 2,
        }
        validators = sorted(
            self.contract.validators,
            key=lambda validator: stage_order[validator.stage],
        )
        provenance: list[ValidatorProvenance] = []
        for validator in validators:
            try:
                decision = await validator.validate(value, context)
            except MoteError:
                raise
            except Exception as exc:
                raise OutputValidatorError(
                    f"output validator {validator.name} failed",
                    cause=exc,
                    validator=validator.name,
                    version=validator.version,
                ) from exc
            if isinstance(decision, (Accept, Corrected)):
                provenance.append(self._validator_provenance(validator, decision))
                value = decision.value
            elif isinstance(decision, Reject):
                provenance.append(self._validator_provenance(validator, decision))
                return await self._reject(
                    candidate_id,
                    contract_id,
                    decision.issues,
                    tuple(provenance),
                )
            elif isinstance(decision, RetryLater):
                raise OutputValidatorUnavailableError(
                    decision.reason,
                    validator=validator.name,
                    version=validator.version,
                    retry_after_seconds=decision.retry_after_seconds,
                )
            else:
                raise OutputValidatorError(
                    f"output validator {validator.name} returned an invalid decision",
                    validator=validator.name,
                    version=validator.version,
                )
        self.accepted_value = value
        self.accepted_candidate_id = candidate_id
        self.validator_provenance = tuple(provenance)
        self.state = OutputLifecycleState.ACCEPTED
        await self._publish_session_fact(
            OutputAcceptedEvent(
                candidate_id=candidate_id,
                contract_id=contract_id,
                schema_fingerprint=schema_fingerprint,
                value=self.contract.decoder.encode(value),
                correction_attempts=self.correction_attempts,
                validator_provenance=[asdict(item) for item in self.validator_provenance],
                run_id=self.run_id,
                run_kind=self.run_kind.value,
            )
        )
        return OutputEvaluation(
            accepted=True,
            candidate_id=candidate_id,
            value=value,
            correction_attempt=self.correction_attempts,
            corrections_remaining=(self.contract.retry_policy.max_corrections - self.correction_attempts),
            max_corrections=self.contract.retry_policy.max_corrections,
        )

    async def _reject(
        self,
        candidate_id: str,
        contract_id: str,
        issues: tuple[ValidationIssue, ...],
        validator_provenance: tuple[ValidatorProvenance, ...] = (),
    ) -> OutputEvaluation[OutputT]:
        correction_allowed = self.correction_attempts < self.contract.retry_policy.max_corrections
        if correction_allowed:
            self.correction_attempts += 1
        self.state = (
            OutputLifecycleState.AWAITING_CORRECTION
            if correction_allowed
            else OutputLifecycleState.CORRECTION_EXHAUSTED
        )
        evaluation = OutputEvaluation(
            accepted=False,
            candidate_id=candidate_id,
            issues=issues,
            correction_attempt=self.correction_attempts,
            corrections_remaining=(self.contract.retry_policy.max_corrections - self.correction_attempts),
            correction_allowed=correction_allowed,
            max_corrections=self.contract.retry_policy.max_corrections,
        )
        await self._publish_session_fact(
            OutputValidationRejectedEvent(
                candidate_id=candidate_id,
                contract_id=contract_id,
                issues=[
                    {
                        "path": list(issue.path),
                        "code": issue.code,
                        "message": issue.message,
                    }
                    for issue in issues
                ],
                correction_attempt=evaluation.correction_attempt,
                corrections_remaining=evaluation.corrections_remaining,
                correction_allowed=evaluation.correction_allowed,
                validator_provenance=[asdict(item) for item in validator_provenance],
                run_id=self.run_id,
                run_kind=self.run_kind.value,
            )
        )
        return evaluation

    async def commit(self) -> CommittedOutput[OutputT]:
        """Durably commit the accepted output after its transcript is recorded."""
        if not self.accepted or not self.accepted_candidate_id:
            raise OutputCommitStateError("cannot commit before an output is accepted")
        if self.committed:
            assert self.committed_output is not None
            return self.committed_output
        self._assert_current_fence()
        contract_id = str(self.contract.contract_id)
        accepted_value = cast(OutputT, self.accepted_value)
        encoded = self.contract.decoder.encode(accepted_value)
        if self._pending_migration is not None:
            await self._publish_session_fact(
                OutputMigratedEvent(
                    candidate_id=self.accepted_candidate_id,
                    source_contract_id=self._pending_migration["source_contract_id"],
                    target_contract_id=contract_id,
                    target_schema_fingerprint=self.contract.decoder.schema.fingerprint,
                    value=encoded,
                    steps=self._pending_migration["steps"],
                    run_id=self.run_id,
                    run_kind=self.run_kind.value,
                )
            )
            await self._drain_writes()
            self._pending_migration = None
        self.state = OutputLifecycleState.COMMIT_STARTED
        await self._publish_session_fact(
            OutputCommitStartedEvent(
                candidate_id=self.accepted_candidate_id,
                contract_id=contract_id,
                run_id=self.run_id,
                run_kind=self.run_kind.value,
                fencing_token=self._fencing_token or 0,
            )
        )
        await self._drain_writes()
        committed = CommittedOutput(
            candidate_id=self.accepted_candidate_id,
            contract_id=contract_id,
            schema_fingerprint=self.contract.decoder.schema.fingerprint,
            value=accepted_value,
            correction_attempts=self.correction_attempts,
            validator_provenance=self.validator_provenance,
            run_id=self.run_id,
            run_kind=self.run_kind,
            fencing_token=self._fencing_token or 0,
        )
        event = OutputCommittedEvent(
            candidate_id=committed.candidate_id,
            contract_id=committed.contract_id,
            schema_fingerprint=committed.schema_fingerprint,
            value=encoded,
            correction_attempts=committed.correction_attempts,
            validator_provenance=[asdict(item) for item in committed.validator_provenance],
            run_id=self.run_id,
            run_kind=self.run_kind.value,
            fencing_token=committed.fencing_token,
        )
        if self._commit_fence is None:
            await self._publish_session_fact(event)
            await self._drain_writes()
        else:
            assert self._fencing_token is not None
            with self._commit_fence.guard(self.run_id, self._fencing_token):
                await self._publish_session_fact(event)
                await self._drain_writes()
        self.committed_output = committed
        self.state = OutputLifecycleState.COMMITTED
        return committed

    def _assert_current_fence(self) -> None:
        if self._commit_fence is not None:
            assert self._fencing_token is not None
            self._commit_fence.assert_current(self.run_id, self._fencing_token)

    @staticmethod
    def _validator_provenance(validator, decision) -> ValidatorProvenance:
        return ValidatorProvenance(
            name=validator.name,
            version=validator.version,
            stage=validator.stage.value,
            effect=validator.effect.value,
            determinism=validator.determinism.value,
            decision=(
                "corrected"
                if isinstance(decision, Corrected)
                else "accept"
                if isinstance(decision, Accept)
                else "reject"
            ),
        )
