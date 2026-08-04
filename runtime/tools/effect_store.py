"""Canonical per-Session Tool effect lifecycle store."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

from mote.contracts.tool.effects import ToolEffect
from mote.contracts.tool.identity import ToolInvocationIdentity
from mote.runtime.ledger.append_ledger import AppendOnlyLedger, LedgerCommitReceipt
from mote.runtime.session.run_domain_activation import require_run_domain_activation
from mote.runtime.session.workspace import SessionSpace, SessionWorkspace

TOOL_EFFECT_SCHEMA = "mote.tool-effect/v1"
TOOL_EFFECT_FILE_NAME = "tool-effects.jsonl"


class ToolEffectState(StrEnum):
    INTENT_COMMITTED = "intent_committed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    IN_DOUBT = "in_doubt"


class ToolEffectIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ToolEffectRecord:
    invocation_id: str
    identity: ToolInvocationIdentity
    tool_name: str
    capability: ToolEffect
    state: ToolEffectState
    receipt: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema": TOOL_EFFECT_SCHEMA,
                "invocation_id": self.invocation_id,
                "identity": self.identity.to_payload(),
                "tool_name": self.tool_name,
                "capability": self.capability.value,
                "state": self.state.value,
                "receipt": self.receipt,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "ToolEffectRecord":
        fields = {"schema", "invocation_id", "identity", "tool_name", "capability", "state", "receipt"}
        if set(raw) != fields or raw.get("schema") != TOOL_EFFECT_SCHEMA:
            raise ToolEffectIntegrityError("Tool effect record has an unsupported schema or field set")
        invocation_id = raw["invocation_id"]
        tool_name = raw["tool_name"]
        identity = raw["identity"]
        receipt = raw["receipt"]
        if type(invocation_id) is not str or not invocation_id:
            raise ToolEffectIntegrityError("Tool effect invocation_id is invalid")
        if type(tool_name) is not str or not tool_name:
            raise ToolEffectIntegrityError("Tool effect tool_name is invalid")
        if type(identity) is not dict:
            raise ToolEffectIntegrityError("Tool effect identity is invalid")
        if receipt is not None and type(receipt) is not str:
            raise ToolEffectIntegrityError("Tool effect receipt is invalid")
        try:
            capability = ToolEffect(raw["capability"])
            state = ToolEffectState(raw["state"])
            decoded_identity = ToolInvocationIdentity.from_payload(identity)
        except (TypeError, ValueError) as exc:
            raise ToolEffectIntegrityError("Tool effect discriminator or identity is invalid") from exc
        if str(decoded_identity.invocation_id) != invocation_id:
            raise ToolEffectIntegrityError("Tool effect record identity does not match its key")
        if state is ToolEffectState.INTENT_COMMITTED and receipt is not None:
            raise ToolEffectIntegrityError("Unsettled Tool effect cannot carry a terminal receipt")
        if state is not ToolEffectState.INTENT_COMMITTED and receipt is None:
            raise ToolEffectIntegrityError("Terminal Tool effect requires a receipt")
        return cls(invocation_id, decoded_identity, tool_name, capability, state, receipt)


class ToolEffectStore(AppendOnlyLedger[ToolEffectRecord]):
    def __init__(self, session_id: str, store: SessionWorkspace) -> None:
        self._session_id = session_id
        path = store.space(session_id, SessionSpace.LEDGER) / TOOL_EFFECT_FILE_NAME
        require_run_domain_activation(path.parent)
        super().__init__(path)

    @property
    def session_id(self) -> str:
        return self._session_id

    def _parse_record(self, data: dict[str, object]) -> ToolEffectRecord:
        return ToolEffectRecord.from_dict(data)

    def _record_key(self, record: ToolEffectRecord) -> str:
        return record.invocation_id

    def _validate_transition(self, previous: ToolEffectRecord | None, record: ToolEffectRecord) -> None:
        if previous is None:
            if record.state is not ToolEffectState.INTENT_COMMITTED:
                raise ToolEffectIntegrityError("Tool effect must begin with a committed intent")
            return
        if previous.state is not ToolEffectState.INTENT_COMMITTED or record.state is ToolEffectState.INTENT_COMMITTED:
            raise ToolEffectIntegrityError("Tool effect lifecycle is terminal and monotonic")
        if (
            previous.identity != record.identity
            or previous.tool_name != record.tool_name
            or previous.capability is not record.capability
        ):
            raise ToolEffectIntegrityError("Tool effect settlement forks its committed preimage")

    def lookup(self, invocation_id: str) -> ToolEffectRecord | None:
        return self.get(invocation_id)

    def commit_intent(
        self,
        identity: ToolInvocationIdentity,
        tool_name: str,
        capability: ToolEffect,
    ) -> LedgerCommitReceipt:
        return self.append(
            ToolEffectRecord(
                str(identity.invocation_id), identity, tool_name, capability, ToolEffectState.INTENT_COMMITTED
            )
        )

    def settle(
        self,
        invocation_id: str,
        *,
        succeeded: bool,
        receipt: str,
    ) -> LedgerCommitReceipt:
        prior = self.get(invocation_id)
        if prior is None:
            raise ToolEffectIntegrityError("Tool effect settlement has no committed intent")
        state = ToolEffectState.SUCCEEDED if succeeded else ToolEffectState.FAILED
        return self.append(
            ToolEffectRecord(invocation_id, prior.identity, prior.tool_name, prior.capability, state, receipt)
        )

    def unresolved(self) -> tuple[ToolEffectRecord, ...]:
        return tuple(record for record in self.records() if record.state is ToolEffectState.INTENT_COMMITTED)


__all__ = [
    "TOOL_EFFECT_FILE_NAME",
    "TOOL_EFFECT_SCHEMA",
    "ToolEffectIntegrityError",
    "ToolEffectRecord",
    "ToolEffectState",
    "ToolEffectStore",
]
