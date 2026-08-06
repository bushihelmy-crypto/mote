from __future__ import annotations

import ast
from dataclasses import fields
from pathlib import Path

from mote.contracts.execution.pending_act import PendingAction
from mote.contracts.interaction.approval import ApprovalState
from mote.contracts.tool.external_effect import ExternalEffectState

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION = ("contracts", "kernel", "runtime", "orchestration", "product")


def test_pending_act_has_one_session_owner_and_no_effect_store() -> None:
    assert not (ROOT / "runtime/tools/effect_store.py").exists()
    production = "\n".join(
        path.read_text(encoding="utf-8") for owner in PRODUCTION for path in (ROOT / owner).rglob("*.py")
    )
    assert "ToolEffectStore" not in production
    assert "tool-effects.jsonl" not in production


def test_pending_action_does_not_mix_owner_state_machines() -> None:
    names = {field.name for field in fields(PendingAction)}
    assert "approval_request_id" not in names
    assert "approval_state" not in names
    assert "external_effect_state" not in names
    assert set(ApprovalState).isdisjoint(set(ExternalEffectState))


def test_pending_act_production_modules_have_no_local_imports() -> None:
    paths = (
        ROOT / "contracts/events/pending_act.py",
        ROOT / "runtime/session/pending_act.py",
        ROOT / "runtime/session/pending_act_acceptance.py",
        ROOT / "runtime/session/pending_act_claim.py",
        ROOT / "runtime/session/run_interrupt.py",
        ROOT / "runtime/session/execution_restore.py",
        ROOT / "runtime/session/execution_reconciliation.py",
    )
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                assert node.col_offset == 0, f"local import in {path}:{node.lineno}"


def test_receipt_only_reconciler_cannot_reach_the_invoke_pipeline() -> None:
    path = ROOT / "runtime/session/execution_reconciliation.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None}
    assert "mote.runtime.tools.tool_executor" not in imports
    assert "mote.runtime.tools.tool_pipeline" not in imports
    assert "mote.contracts.tool.catalog" not in imports
    assert "dispatch" not in source


def test_guarded_append_has_one_combined_stream_and_run_authority() -> None:
    journal = (ROOT / "runtime/events/journal.py").read_text(encoding="utf-8")
    authority = (ROOT / "runtime/session/writer_guard.py").read_text(encoding="utf-8")
    contract = (ROOT / "contracts/ports/events/journal.py").read_text(encoding="utf-8")

    assert "class GuardedAppendAuthority" in contract
    assert "guard_append(writer)" in journal
    assert "guard_writer(writer)" not in journal
    assert "def guard_append(" in authority
    assert "guard_many(" in authority
    assert "self._stream_subject" in authority
    assert "self._run_subject(writer.run_id)" in authority
