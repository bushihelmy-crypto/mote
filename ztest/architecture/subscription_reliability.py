"""Static companion to focused subscription transaction tests."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _class(tree: ast.Module, name: str) -> ast.ClassDef:
    return next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name)


def _method(owner: ast.ClassDef, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    return next(
        node for node in owner.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


def main() -> int:
    violations: list[str] = []
    worker_path = ROOT / "runtime/events/subscription.py"
    worker_tree = ast.parse(worker_path.read_text(encoding="utf-8"))
    process = ast.unparse(_method(_class(worker_tree, "SubscriptionWorker"), "_process"))
    if "Reliability.RELIABLE" not in process or "await self._quarantine" not in process:
        violations.append("RELIABLE failure does not quarantine before continuing")
    durable_branch = process.split("Reliability.RELIABLE", 1)[-1]
    if "await self._mark_failed(last_error)" not in durable_branch:
        violations.append("DURABLE failure does not stop without acknowledgement")

    state_path = ROOT / "runtime/events/backends/subscription_state.py"
    state_tree = ast.parse(state_path.read_text(encoding="utf-8"))
    quarantine = ast.unparse(_method(_class(state_tree, "SQLiteSubscriptionStateStore"), "_quarantine_sync"))
    for required in ("BEGIN IMMEDIATE", "_save_in_transaction", "connection.commit()", "connection.rollback()"):
        if required not in quarantine:
            violations.append(f"quarantine transaction is missing {required}")

    composition = (ROOT / "runtime/agent/components/session.py").read_text(encoding="utf-8")
    if "SideEffectPolicy.IDEMPOTENT_EXTERNAL_EFFECT" not in composition:
        violations.append("recoverable LSP external effect lacks typed policy")
    if "effect_identity=" not in composition:
        violations.append("recoverable LSP external effect lacks stable identity")

    if violations:
        print("\n".join(violations), file=sys.stderr)
        return 1
    print("subscription reliability invariants are closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
