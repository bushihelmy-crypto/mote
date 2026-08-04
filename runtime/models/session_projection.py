"""Canonical ModelCall-to-Session projection intent and acknowledgement store."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import StrEnum

from pydantic import TypeAdapter, ValidationError

from mote.contracts.execution.models import InferenceCheckpointAttemptState, InferenceCheckpointState
from mote.contracts.model.checkpoint import ModelCheckpointPolicy
from mote.contracts.model.invocation import GenerateOutput
from mote.runtime.ledger.append_ledger import AppendOnlyLedger, LedgerCommitReceipt
from mote.runtime.session.run_domain_activation import require_run_domain_activation
from mote.runtime.session.workspace import SessionSpace, SessionWorkspace

MODEL_SESSION_PROJECTION_SCHEMA = "mote.model-session-projection/v1"
_OUTPUT = TypeAdapter(GenerateOutput)


class ModelSessionProjectionState(StrEnum):
    CALL_STARTED = "call_started"
    INTENT_COMMITTED = "intent_committed"
    ACKNOWLEDGED = "acknowledged"
    OWNER_ACTION_REQUIRED = "owner_action_required"


@dataclass(frozen=True, slots=True)
class ModelSessionProjectionRecord:
    model_call_id: str
    checkpoint: InferenceCheckpointState
    state: ModelSessionProjectionState
    output: GenerateOutput | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema": MODEL_SESSION_PROJECTION_SCHEMA,
                "model_call_id": self.model_call_id,
                "checkpoint": asdict(self.checkpoint),
                "state": self.state.value,
                "output": None if self.output is None else self.output.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "ModelSessionProjectionRecord":
        if (
            set(raw) != {"schema", "model_call_id", "checkpoint", "state", "output"}
            or raw.get("schema") != MODEL_SESSION_PROJECTION_SCHEMA
        ):
            raise ValueError("Model Session projection record is not strict v1")
        call_id, checkpoint, output = raw["model_call_id"], raw["checkpoint"], raw["output"]
        if type(call_id) is not str or not call_id or type(checkpoint) is not dict:
            raise ValueError("Model Session projection identity or checkpoint is invalid")
        try:
            state = ModelSessionProjectionState(raw["state"])
            checkpoint_values = dict(checkpoint)
            checkpoint_values["attempt_state"] = InferenceCheckpointAttemptState(checkpoint_values.get("attempt_state"))
            decoded_checkpoint = InferenceCheckpointState(**checkpoint_values)
            decoded_output = None if output is None else _OUTPUT.validate_python(output)
        except (TypeError, ValueError, ValidationError) as exc:
            raise ValueError("Model Session projection discriminator or payload is invalid") from exc
        if decoded_checkpoint.model_call_id != call_id:
            raise ValueError("Model Session projection checkpoint identity conflicts")
        if (state is ModelSessionProjectionState.CALL_STARTED) != (decoded_output is None):
            if state is not ModelSessionProjectionState.OWNER_ACTION_REQUIRED:
                raise ValueError("Model Session projection lifecycle payload is inconsistent")
        return cls(call_id, decoded_checkpoint, state, decoded_output)


class ModelSessionProjectionStore(AppendOnlyLedger[ModelSessionProjectionRecord]):
    def __init__(
        self,
        session_id: str,
        workspace: SessionWorkspace,
        policy: ModelCheckpointPolicy,
    ) -> None:
        self._policy = policy
        path = workspace.space(session_id, SessionSpace.LEDGER) / "model-session-projections.jsonl"
        require_run_domain_activation(path.parent)
        super().__init__(path)

    def _parse_record(self, data: dict[str, object]) -> ModelSessionProjectionRecord:
        return ModelSessionProjectionRecord.from_dict(data)

    def _record_key(self, record: ModelSessionProjectionRecord) -> str:
        return record.model_call_id

    def _validate_transition(
        self, previous: ModelSessionProjectionRecord | None, record: ModelSessionProjectionRecord
    ) -> None:
        if previous is None:
            if record.state is not ModelSessionProjectionState.CALL_STARTED:
                raise ValueError("Model Session projection must begin with call_started")
            return
        if previous.checkpoint != record.checkpoint:
            if previous.state is not ModelSessionProjectionState.CALL_STARTED or record.state is not previous.state:
                raise ValueError("Model Session projection checkpoint forked")
            if (
                previous.checkpoint.model_call_id != record.checkpoint.model_call_id
                or previous.checkpoint.request_fingerprint != record.checkpoint.request_fingerprint
                or record.checkpoint.inference_fencing_token < previous.checkpoint.inference_fencing_token
            ):
                raise ValueError("Model Session projection checkpoint identity or fence forked")
            allowed_attempt_states = {
                InferenceCheckpointAttemptState.INTENT_COMMITTED: {
                    InferenceCheckpointAttemptState.INTENT_COMMITTED,
                    InferenceCheckpointAttemptState.WIRE_STARTED,
                },
                InferenceCheckpointAttemptState.WIRE_STARTED: {
                    InferenceCheckpointAttemptState.INTENT_COMMITTED,
                    InferenceCheckpointAttemptState.SETTLED,
                    InferenceCheckpointAttemptState.IN_DOUBT,
                },
            }
            if record.checkpoint.attempt_state not in allowed_attempt_states.get(
                previous.checkpoint.attempt_state, set()
            ):
                raise ValueError("Model Session projection attempt lifecycle forked")
            return
        allowed = {
            ModelSessionProjectionState.CALL_STARTED: {
                ModelSessionProjectionState.INTENT_COMMITTED,
                ModelSessionProjectionState.OWNER_ACTION_REQUIRED,
            },
            ModelSessionProjectionState.INTENT_COMMITTED: {
                ModelSessionProjectionState.ACKNOWLEDGED,
                ModelSessionProjectionState.OWNER_ACTION_REQUIRED,
            },
        }
        if record.state not in allowed.get(previous.state, set()) or (
            previous.output is not None and record.output != previous.output
        ):
            raise ValueError("Model Session projection lifecycle is terminal or forked")

    def begin(self, checkpoint: InferenceCheckpointState) -> LedgerCommitReceipt:
        active = sum(
            record.state in {ModelSessionProjectionState.CALL_STARTED, ModelSessionProjectionState.INTENT_COMMITTED}
            for record in self.records()
        )
        if self.get(checkpoint.model_call_id) is None and active >= self._policy.active_per_session:
            raise RuntimeError("Model checkpoint Session capacity is exhausted")
        return self.append(
            ModelSessionProjectionRecord(checkpoint.model_call_id, checkpoint, ModelSessionProjectionState.CALL_STARTED)
        )

    def commit_intent(self, model_call_id: str, output: GenerateOutput) -> LedgerCommitReceipt:
        prior = self.get(model_call_id)
        if prior is None:
            raise ValueError("Model Session projection intent has no call_started fact")
        return self.append(
            ModelSessionProjectionRecord(
                model_call_id, prior.checkpoint, ModelSessionProjectionState.INTENT_COMMITTED, output
            )
        )

    def refresh(self, checkpoint: InferenceCheckpointState) -> LedgerCommitReceipt:
        prior = self.get(checkpoint.model_call_id)
        if prior is None or prior.state is not ModelSessionProjectionState.CALL_STARTED:
            raise ValueError("Model Session projection refresh has no active call")
        return self.append(
            ModelSessionProjectionRecord(
                checkpoint.model_call_id,
                checkpoint,
                ModelSessionProjectionState.CALL_STARTED,
            )
        )

    def acknowledge(self, model_call_id: str) -> LedgerCommitReceipt:
        prior = self.get(model_call_id)
        if prior is None or prior.output is None:
            raise ValueError("Model Session projection acknowledgement has no intent")
        return self.append(
            ModelSessionProjectionRecord(
                model_call_id, prior.checkpoint, ModelSessionProjectionState.ACKNOWLEDGED, prior.output
            )
        )

    def require_owner_action(self, model_call_id: str) -> LedgerCommitReceipt:
        prior = self.get(model_call_id)
        if prior is None:
            raise ValueError("Model Session projection owner action has no call fact")
        return self.append(
            ModelSessionProjectionRecord(
                model_call_id,
                prior.checkpoint,
                ModelSessionProjectionState.OWNER_ACTION_REQUIRED,
                prior.output,
            )
        )

    def pending(self) -> tuple[ModelSessionProjectionRecord, ...]:
        return tuple(
            record
            for record in self.records()
            if record.state in {ModelSessionProjectionState.CALL_STARTED, ModelSessionProjectionState.INTENT_COMMITTED}
        )


__all__ = [
    "MODEL_SESSION_PROJECTION_SCHEMA",
    "ModelSessionProjectionRecord",
    "ModelSessionProjectionState",
    "ModelSessionProjectionStore",
]
