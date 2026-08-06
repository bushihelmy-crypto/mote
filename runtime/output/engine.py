"""Run-scoped output candidate decoding and validation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict
from typing import Generic, TypeVar, cast
from uuid import uuid4

from pydantic import ValidationError

from mote.contracts.conversation import Message
from mote.contracts.events.envelope import JsonValue, freeze_json
from mote.contracts.events.output import (
    FinalOutputCommittedEvent,
    OutputCandidateReceivedEvent,
    OutputValidationRejectedEvent,
)
from mote.contracts.execution.restore import CommittedExecution
from mote.contracts.foundation.errors.base import MoteError
from mote.contracts.output import (
    Accept,
    CommittedOutput,
    Corrected,
    OutputDecodeError,
    OutputEvaluation,
    Reject,
    RetryLater,
    RunKind,
    ValidatedCandidate,
    ValidationContext,
    ValidationIssue,
    ValidationStage,
    ValidatorProvenance,
)
from mote.contracts.output.errors import (
    OutputCommitStateError,
    OutputResumeContractMismatchError,
    OutputValidatorError,
    OutputValidatorUnavailableError,
)
from mote.contracts.ports.execution.commit_fence import CommitFence
from mote.contracts.ports.session.facts import RolloutSourceEvent, SessionFactSink
from mote.kernel.output import OutputContract
from mote.runtime.events.context import observe_event
from mote.runtime.output.state_machine import OutputLifecycleState, OutputStateMachine

OutputT = TypeVar("OutputT")


async def _noop_async() -> None:
    return None


def _json_record(value: object, *, path: str) -> Mapping[str, JsonValue]:
    frozen = freeze_json(value, path=path)
    if not isinstance(frozen, Mapping):
        raise TypeError(f"{path} must be a JSON object")
    return cast(Mapping[str, JsonValue], frozen)


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
        self.candidate_id = ""
        self.validated_candidate: ValidatedCandidate[OutputT] | None = None
        self._lifecycle = OutputStateMachine()
        self.committed_output: CommittedOutput[OutputT] | None = None
        self.restored_message: Message | None = None
        self.validator_provenance: tuple[ValidatorProvenance, ...] = ()
        self.restored = False
        if restored_state is not None:
            self._restore(restored_state)

    async def _publish_session_fact(self, event: RolloutSourceEvent) -> None:
        if self._session_fact_sink is not None:
            await self._session_fact_sink.commit_fact(event)
        await observe_event(event)

    def _restore(self, state: dict) -> None:
        """Strictly restore the one canonical committed terminal fact."""
        contract_id = str(self.contract.contract_id)
        fingerprint = self.contract.decoder.schema.fingerprint
        if state.get("status") != "committed":
            raise OutputResumeContractMismatchError("only committed output can be restored", status=state.get("status"))
        if state.get("contract_id") != contract_id or state.get("schema_fingerprint") != fingerprint:
            raise OutputResumeContractMismatchError(
                "committed output contract identity mismatch",
                recorded_contract_id=state.get("contract_id"),
                current_contract_id=contract_id,
            )
        attempts = state.get("correction_attempts", 0)
        if type(attempts) is not int or attempts < 0 or attempts > self.contract.retry_policy.max_corrections:
            raise OutputResumeContractMismatchError("persisted correction count is invalid")
        self.run_id = str(state.get("run_id") or self.run_id)
        self.validator_provenance = tuple(ValidatorProvenance(**item) for item in state.get("validator_provenance", ()))
        expected_validators = {
            (
                validator.name,
                validator.version,
                validator.stage.value,
                validator.effect.value,
                validator.determinism.value,
            )
            for validator in self.contract.validators
        }
        recorded_validators = {
            (item.name, item.version, item.stage, item.effect, item.determinism) for item in self.validator_provenance
        }
        if recorded_validators != expected_validators:
            raise OutputResumeContractMismatchError("committed validator provenance does not match the contract")
        value = self.contract.decoder.decode(state.get("value"))
        self.candidate_id = str(state.get("candidate_id") or "")
        if not self.candidate_id:
            raise OutputResumeContractMismatchError("committed output candidate identity is missing")
        self._lifecycle.restore(OutputLifecycleState.COMMITTED, attempts)
        self.committed_output = CommittedOutput(
            candidate_id=self.candidate_id,
            contract_id=contract_id,
            schema_fingerprint=fingerprint,
            value=value,
            correction_attempts=attempts,
            validator_provenance=self.validator_provenance,
            run_id=self.run_id,
            run_kind=self.run_kind,
            fencing_token=int(state.get("fencing_token", 0)),
        )
        message = state.get("message")
        if not isinstance(message, Message):
            raise OutputResumeContractMismatchError("committed output terminal message is missing")
        self.restored_message = message
        self.restored = True

    @property
    def validated(self) -> bool:
        return self._lifecycle.validated

    @property
    def committed(self) -> bool:
        return self._lifecycle.committed

    @property
    def state(self) -> OutputLifecycleState:
        return self._lifecycle.state

    @property
    def correction_attempts(self) -> int:
        return self._lifecycle.correction_attempts

    @property
    def has_restored_terminal_output(self) -> bool:
        """Whether resume can finish this lifecycle without another model call."""
        return self.restored and self.committed_output is not None

    def restored_committed_execution(self) -> CommittedExecution[OutputT] | None:
        """Return the immutable terminal fact, or fail closed on partial restore."""
        if not self.has_restored_terminal_output:
            return None
        committed = self.committed_output
        presentation = self.restored_message
        if committed is None or presentation is None:
            raise OutputResumeContractMismatchError("restored output omitted its terminal value")
        return CommittedExecution(committed, presentation)

    async def evaluate(self, candidate) -> OutputEvaluation[OutputT]:
        if self.validated:
            raise OutputCommitStateError(
                "cannot evaluate another candidate after output acceptance",
                state=self.state.value,
            )
        candidate_id = candidate.candidate_id or uuid4().hex
        contract_id = str(self.contract.contract_id)
        schema_fingerprint = self.contract.decoder.schema.fingerprint
        raw_payload = freeze_json(candidate.raw, path="output candidate raw")
        self._lifecycle.receive_candidate()
        await self._publish_session_fact(
            OutputCandidateReceivedEvent(
                candidate_id=candidate_id,
                contract_id=contract_id,
                schema_fingerprint=schema_fingerprint,
                representation=candidate.representation,
                raw=raw_payload,
                run_id=self.run_id,
                run_kind=self.run_kind.value,
            )
        )
        try:
            value = self.contract.decoder.decode(raw_payload)
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
        self.candidate_id = candidate_id
        self.validator_provenance = tuple(provenance)
        self._lifecycle.validate()
        self.validated_candidate = ValidatedCandidate(
            candidate_id=candidate_id,
            contract_id=contract_id,
            schema_fingerprint=schema_fingerprint,
            value=value,
            encoded_value=self.contract.decoder.encode(value),
            correction_attempts=self.correction_attempts,
            validator_provenance=self.validator_provenance,
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
        correction_allowed = self._lifecycle.reject(self.contract.retry_policy.max_corrections)
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
                issues=tuple(
                    _json_record(
                        {
                            "path": list(issue.path),
                            "code": issue.code,
                            "message": issue.message,
                        },
                        path="output validation issue",
                    )
                    for issue in issues
                ),
                correction_attempt=evaluation.correction_attempt,
                corrections_remaining=evaluation.corrections_remaining,
                correction_allowed=evaluation.correction_allowed,
                validator_provenance=tuple(
                    _json_record(asdict(item), path="output validator provenance") for item in validator_provenance
                ),
                run_id=self.run_id,
                run_kind=self.run_kind.value,
            )
        )
        return evaluation

    async def commit_final(
        self,
        message: Message,
        *,
        companion_facts: tuple[RolloutSourceEvent, ...] = (),
        fact_sink: SessionFactSink | None = None,
    ) -> CommittedOutput[OutputT]:
        """Atomically commit validated output, terminal message, and settlement facts."""
        if not self.validated or not self.candidate_id:
            raise OutputCommitStateError("cannot commit before output validation")
        if self.validated_candidate is None:
            raise OutputCommitStateError("cannot commit before output validation")
        if self.committed:
            assert self.committed_output is not None
            return self.committed_output
        self._assert_current_fence()
        contract_id = str(self.contract.contract_id)
        value = self.validated_candidate.value
        encoded = self.validated_candidate.encoded_value
        committed = CommittedOutput(
            candidate_id=self.candidate_id,
            contract_id=contract_id,
            schema_fingerprint=self.contract.decoder.schema.fingerprint,
            value=value,
            correction_attempts=self.correction_attempts,
            validator_provenance=self.validator_provenance,
            run_id=self.run_id,
            run_kind=self.run_kind,
            fencing_token=self._fencing_token or 0,
        )
        event = FinalOutputCommittedEvent(
            candidate_id=committed.candidate_id,
            contract_id=committed.contract_id,
            schema_fingerprint=committed.schema_fingerprint,
            value=encoded,
            message=message,
            correction_attempts=committed.correction_attempts,
            validator_provenance=tuple(
                _json_record(asdict(item), path="output validator provenance")
                for item in committed.validator_provenance
            ),
            run_id=self.run_id,
            run_kind=self.run_kind.value,
            fencing_token=committed.fencing_token,
        )
        if self._commit_fence is None:
            await self._publish_session_facts((event, *companion_facts), fact_sink=fact_sink)
            await self._drain_writes()
        else:
            assert self._fencing_token is not None
            with self._commit_fence.guard(self.run_id, self._fencing_token):
                await self._publish_session_facts((event, *companion_facts), fact_sink=fact_sink)
                await self._drain_writes()
        self.committed_output = committed
        self._lifecycle.complete_commit()
        return committed

    async def _publish_session_facts(
        self,
        events: tuple[RolloutSourceEvent, ...],
        *,
        fact_sink: SessionFactSink | None,
    ) -> None:
        sink = fact_sink or self._session_fact_sink
        if sink is not None:
            await sink.commit_facts(events)
        for event in events:
            await observe_event(event)

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
                else "accept" if isinstance(decision, Accept) else "reject"
            ),
        )
