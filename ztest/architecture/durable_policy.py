"""Validate durable policy completeness and sensitive audit minimization."""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_authorities():
    helper_spec = importlib.util.spec_from_file_location(
        "mote_governance_artifact",
        ROOT / "ztest/architecture/governance_artifact.py",
    )
    if helper_spec is None or helper_spec.loader is None:
        raise RuntimeError("governance artifact loader is unavailable")
    helper = importlib.util.module_from_spec(helper_spec)
    helper_spec.loader.exec_module(helper)
    helper._modules()
    event_governance = helper._load("mote.contracts.events.governance", "contracts/events/governance.py")
    helper._load("mote.contracts.ports", "contracts/ports/__init__.py")
    helper._load("mote.contracts.ports.events", "contracts/ports/events/__init__.py")
    helper._load("mote.contracts.ports.events.journal", "contracts/ports/events/journal.py")
    helper._load("mote.contracts.events.file", "contracts/events/file/__init__.py")
    helper._load("mote.contracts.events.file.facts", "contracts/events/file/facts.py")
    helper._load("mote.runtime.session.events", "runtime/session/events.py")
    session = helper._load("mote.runtime.session.codec", "runtime/session/codec.py")
    audit = helper._load(
        "mote.product.inference.daemon.operations_audit_codec",
        "product/inference/daemon/operations_audit_codec.py",
    )
    return event_governance, (
        *session.SESSION_ACTIVE_CODECS,
        audit.OPERATIONS_AUDIT_ACTIVE_CODEC,
    )


def main() -> int:
    governance, codecs = _load_authorities()
    violations: list[str] = []
    for entry in codecs:
        policy = entry.policy
        if policy.sensitivity is governance.Sensitivity.RESTRICTED and not policy.redaction_at_source:
            violations.append(f"{entry.logical_store}/{entry.event_family}: restricted data is not redacted at source")
        if policy.semantic_inline_size_limit < 1:
            violations.append(f"{entry.logical_store}/{entry.event_family}: missing size bound")
        if not policy.retention_requirement or not policy.legal_hold_behavior or not policy.secondary_copy_policy:
            violations.append(f"{entry.logical_store}/{entry.event_family}: incomplete retention policy")
        if (
            entry.logical_store == "inference-operations-audit"
            and "complete daemon authority lifetime" not in policy.retention_requirement
        ):
            violations.append("operations audit retention is not tied to its physical authority lifetime")

    conversation = ast.parse((ROOT / "contracts/events/conversation.py").read_text(encoding="utf-8"))
    rejected = next(
        node for node in conversation.body if isinstance(node, ast.ClassDef) and node.name == "PromptRejectedEvent"
    )
    fields = {
        node.target.id
        for node in rejected.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    if "prompt" in fields or not {"prompt_digest", "redacted_excerpt", "classification"} <= fields:
        violations.append("PromptRejectedEvent does not enforce data-minimized audit fields")
    role = (ROOT / "runtime/agent/role.py").read_text(encoding="utf-8")
    if "redacted_excerpt=decision.prompt[:160]" not in role:
        violations.append("prompt rejection excerpt lacks its source bound")
    log = (ROOT / "runtime/events/log_subscriber.py").read_text(encoding="utf-8")
    prompt_log = log.split("PromptRejectedEvent:", 1)[-1].split("ToolInvocationStartedEvent:", 1)[0]
    if "e.reason" in prompt_log or "redacted_excerpt" in prompt_log:
        violations.append("prompt rejection log duplicates restricted payload")
    cleanup = (ROOT / "runtime/session/workspace/cleanup.py").read_text(encoding="utf-8")
    maintenance = (ROOT / "runtime/agent/runtime_maintenance.py").read_text(encoding="utf-8")
    if cleanup.count("session_id not in legal_hold_session_ids") != 1 or (
        "session_id in legal_hold_session_ids" not in cleanup
    ):
        violations.append("session retention does not enforce legal hold in both cleanup paths")
    if "legal_hold_session_ids=config.legal_hold_session_ids" not in maintenance:
        violations.append("runtime maintenance does not inject legal hold policy")

    if violations:
        print("\n".join(violations), file=sys.stderr)
        return 1
    print("durable policy and restricted audit payloads are closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
