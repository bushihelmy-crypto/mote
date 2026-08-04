"""Offline legacy RunJournal inventory and three-domain Session cutover."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path

from mote.contracts.clock import AbsoluteInstant
from mote.contracts.execution.models import InferenceCheckpointAttemptState, InferenceCheckpointState
from mote.contracts.model.failover import ModelCallState
from mote.contracts.model.invocation import GenerateOutput
from mote.contracts.ports.model.recovery import ModelCallRecoveryQuery
from mote.contracts.tool.effects import ToolEffect
from mote.contracts.tool.identity import ToolInvocationIdentity
from mote.runtime.models.session_projection import ModelSessionProjectionRecord, ModelSessionProjectionState
from mote.runtime.persistence.atomic import atomic_write
from mote.runtime.session.run_domain_activation import RUN_DOMAIN_MANIFEST_FILE, RUN_DOMAIN_MANIFEST_SCHEMA
from mote.runtime.session.timers import SessionTimerRecord, SessionTimerState
from mote.runtime.tools.effect_store import ToolEffectRecord, ToolEffectState

_FIELDS = {
    "step_id",
    "kind",
    "effect",
    "status",
    "seq",
    "name",
    "tool_call_id",
    "started_at",
    "ended_at",
    "payload",
    "success",
    "invocation_identity",
}


class RunDomainConflict(StrEnum):
    TOOL_IDENTITY = "tool_identity"
    MODEL_EVIDENCE = "model_evidence"
    TIMER_DEADLINE = "timer_deadline"


@dataclass(frozen=True, slots=True)
class LegacyRunStep:
    step_id: str
    kind: str
    effect: str
    status: str
    seq: int
    name: str
    tool_call_id: str | None
    payload: str | None
    success: bool
    invocation_identity: ToolInvocationIdentity | None


@dataclass(frozen=True, slots=True)
class RunDomainInventory:
    session_id: str
    source_digest: str
    records: tuple[LegacyRunStep, ...]


@dataclass(frozen=True, slots=True)
class RunDomainCandidates:
    inventory: RunDomainInventory
    tool_path: Path
    model_path: Path
    timer_path: Path
    digests: dict[str, str]


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _decode_line(line: bytes, line_number: int) -> LegacyRunStep:
    try:
        raw = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"legacy run journal line {line_number} is corrupt") from exc
    if type(raw) is not dict or set(raw) != _FIELDS:
        raise ValueError(f"legacy run journal line {line_number} has unsupported fields")
    if (
        type(raw["step_id"]) is not str
        or not raw["step_id"]
        or raw["kind"] not in {"tool", "think", "timer"}
        or raw["effect"] not in {"pure", "local", "external"}
        or raw["status"] not in {"started", "completed", "failed"}
        or type(raw["seq"]) is not int
        or raw["seq"] < 0
        or type(raw["name"]) is not str
        or (raw["tool_call_id"] is not None and type(raw["tool_call_id"]) is not str)
        or (raw["payload"] is not None and type(raw["payload"]) is not str)
        or type(raw["success"]) is not bool
    ):
        raise ValueError(f"legacy run journal line {line_number} primitive is invalid")
    identity_payload = raw["invocation_identity"]
    if identity_payload is not None and type(identity_payload) is not dict:
        raise ValueError("legacy Tool invocation identity is invalid")
    try:
        identity = None if identity_payload is None else ToolInvocationIdentity.from_payload(identity_payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("legacy Tool invocation identity is invalid") from exc
    return LegacyRunStep(
        raw["step_id"],
        raw["kind"],
        raw["effect"],
        raw["status"],
        raw["seq"],
        raw["name"],
        raw["tool_call_id"],
        raw["payload"],
        raw["success"],
        identity,
    )


def inventory_legacy(source: Path, session_id: str) -> RunDomainInventory:
    data = source.read_bytes()
    records: list[LegacyRunStep] = []
    lifecycle: dict[str, str] = {}
    for line_number, line in enumerate(data.splitlines(keepends=True), start=1):
        if not line.endswith(b"\n"):
            raise ValueError(f"legacy run journal line {line_number} is incomplete")
        record = _decode_line(line, line_number)
        previous = lifecycle.get(record.step_id)
        if previous is None and record.status != "started":
            raise ValueError("legacy run journal lifecycle has no started fact")
        if previous is not None and (previous != "started" or record.status == "started"):
            raise ValueError("legacy run journal lifecycle is forked or non-monotonic")
        lifecycle[record.step_id] = record.status
        records.append(record)
    if not records:
        raise ValueError("legacy run journal is empty")
    return RunDomainInventory(session_id, _digest(data), tuple(records))


def build_candidates(
    inventory: RunDomainInventory,
    candidate_dir: Path,
    model_calls: ModelCallRecoveryQuery,
    *,
    now: AbsoluteInstant,
) -> RunDomainCandidates:
    latest = {record.step_id: record for record in inventory.records}
    started = {record.step_id: record for record in inventory.records if record.status == "started"}
    tool_lines: list[str] = []
    model_lines: list[str] = []
    timer_lines: list[str] = []
    for record in latest.values():
        initial = started[record.step_id]
        if record.kind == "tool":
            _map_tool(initial, record, tool_lines)
        elif record.kind == "think":
            _map_model(initial, record, model_calls, model_lines)
        else:
            _map_timer(initial, record, now, timer_lines)
    paths = {
        "tool": candidate_dir / "tool-effects.jsonl",
        "model": candidate_dir / "model-session-projections.jsonl",
        "timer": candidate_dir / "session-timers.jsonl",
    }
    encoded = {
        "tool": _write_lines(paths["tool"], tool_lines),
        "model": _write_lines(paths["model"], model_lines),
        "timer": _write_lines(paths["timer"], timer_lines),
    }
    return RunDomainCandidates(
        inventory,
        paths["tool"],
        paths["model"],
        paths["timer"],
        {name: _digest(data) for name, data in encoded.items()},
    )


def _map_tool(initial: LegacyRunStep, latest: LegacyRunStep, lines: list[str]) -> None:
    identity = initial.invocation_identity
    if identity is None or str(identity.invocation_id) != initial.step_id:
        raise ValueError(f"{RunDomainConflict.TOOL_IDENTITY.value}:{initial.step_id}")
    capability = ToolEffect(initial.effect)
    intent = ToolEffectRecord(
        initial.step_id, identity, initial.name or "legacy-tool", capability, ToolEffectState.INTENT_COMMITTED
    )
    lines.append(intent.to_json())
    if latest.status != "started" or capability is ToolEffect.EXTERNAL:
        state = (
            ToolEffectState.IN_DOUBT
            if latest.status == "started"
            else (
                ToolEffectState.SUCCEEDED if latest.status == "completed" and latest.success else ToolEffectState.FAILED
            )
        )
        receipt = latest.payload or (
            "legacy-external-outcome-unknown" if state is ToolEffectState.IN_DOUBT else "legacy-empty-result"
        )
        lines.append(
            ToolEffectRecord(initial.step_id, identity, intent.tool_name, capability, state, receipt).to_json()
        )


def _checkpoint(payload: str | None, step_id: str) -> InferenceCheckpointState:
    try:
        raw = json.loads(payload or "")
        checkpoint = raw["checkpoint"]
        if (
            type(raw) is not dict
            or set(raw) not in ({"checkpoint"}, {"checkpoint", "result"})
            or type(checkpoint) is not dict
        ):
            raise ValueError
        checkpoint_values = dict(checkpoint)
        checkpoint_values["attempt_state"] = InferenceCheckpointAttemptState(checkpoint_values.get("attempt_state"))
        state = InferenceCheckpointState(**checkpoint_values)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{RunDomainConflict.MODEL_EVIDENCE.value}:{step_id}") from exc
    return state


def _map_model(
    initial: LegacyRunStep,
    latest: LegacyRunStep,
    model_calls: ModelCallRecoveryQuery,
    lines: list[str],
) -> None:
    checkpoint = _checkpoint(initial.payload, initial.step_id)
    recovery = model_calls.inspect_recovery(checkpoint.model_call_id).recovery
    if recovery is None:
        raise ValueError(f"{RunDomainConflict.MODEL_EVIDENCE.value}:{initial.step_id}")
    started = ModelSessionProjectionRecord(
        checkpoint.model_call_id, checkpoint, ModelSessionProjectionState.CALL_STARTED
    )
    lines.append(started.to_json())
    if recovery.state is ModelCallState.SUCCEEDED and recovery.terminal is not None:
        response = recovery.terminal.accepted_response
        if response is None or not isinstance(response.output, GenerateOutput):
            raise ValueError(f"{RunDomainConflict.MODEL_EVIDENCE.value}:{initial.step_id}")
        lines.append(
            ModelSessionProjectionRecord(
                checkpoint.model_call_id,
                checkpoint,
                ModelSessionProjectionState.INTENT_COMMITTED,
                response.output,
            ).to_json()
        )
    elif recovery.terminal is not None or latest.status != "started":
        lines.append(
            ModelSessionProjectionRecord(
                checkpoint.model_call_id,
                checkpoint,
                ModelSessionProjectionState.OWNER_ACTION_REQUIRED,
            ).to_json()
        )


def _map_timer(initial: LegacyRunStep, latest: LegacyRunStep, now: AbsoluteInstant, lines: list[str]) -> None:
    try:
        deadline_seconds = float(initial.payload or "")
        deadline = AbsoluteInstant.from_datetime(datetime.fromtimestamp(deadline_seconds, timezone.utc))
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"{RunDomainConflict.TIMER_DEADLINE.value}:{initial.step_id}") from exc
    lines.append(SessionTimerRecord(initial.step_id, deadline, SessionTimerState.PENDING).to_json())
    if latest.status != "started":
        state = SessionTimerState.COMPLETED
    elif deadline.epoch_nanoseconds <= now.epoch_nanoseconds:
        state = SessionTimerState.MISFIRED
    else:
        return
    lines.append(SessionTimerRecord(initial.step_id, deadline, state).to_json())


def _write_lines(path: Path, lines: list[str]) -> bytes:
    encoded = (("\n".join(lines) + "\n") if lines else "").encode()
    atomic_write(path, encoded, mode=0o600)
    if path.read_bytes() != encoded:
        raise RuntimeError("run-domain migration candidate read-back failed")
    return encoded


def activate_candidates(
    candidates: RunDomainCandidates,
    source: Path,
    ledger_directory: Path,
    *,
    expected_source_digest: str,
) -> None:
    if (
        candidates.inventory.source_digest != expected_source_digest
        or _digest(source.read_bytes()) != expected_source_digest
    ):
        raise ValueError("run-domain migration source changed after inventory")
    candidate_paths = {
        "tool": candidates.tool_path,
        "model": candidates.model_path,
        "timer": candidates.timer_path,
    }
    for name, path in candidate_paths.items():
        if _digest(path.read_bytes()) != candidates.digests[name]:
            raise ValueError("run-domain migration candidate changed after read-back")
    for path in candidate_paths.values():
        atomic_write(ledger_directory / path.name, path.read_bytes(), mode=0o600)
    retention = (datetime.now(timezone.utc) + timedelta(days=180)).isoformat()
    manifest = {
        "schema": RUN_DOMAIN_MANIFEST_SCHEMA,
        "session_id": candidates.inventory.session_id,
        "source_digest": expected_source_digest,
        "candidate_digests": candidates.digests,
        "retention_until": retention,
    }
    # The manifest is the final activation fact. Until it commits, all three
    # production owners fail closed while the legacy source remains present.
    atomic_write(
        ledger_directory / RUN_DOMAIN_MANIFEST_FILE,
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
        mode=0o600,
    )


__all__ = [
    "RunDomainCandidates",
    "RunDomainConflict",
    "RunDomainInventory",
    "activate_candidates",
    "build_candidates",
    "inventory_legacy",
]
