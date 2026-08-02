"""R3.2 gates for retired compatibility facades and optional backends."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

RETIRED = (
    "runtime/context/turn/format.py",
    "runtime/interactive/__init__.py",
    "runtime/models/auth/oauth/__init__.py",
    "runtime/models/auth/oauth/registry.py",
    "product/models/__init__.py",
    "product/presentation/projection/__init__.py",
)

RETIRED_IMPORTS = frozenset(
    {
        "mote.runtime.context.turn.format",
        "mote.runtime.interactive",
        "mote.runtime.models.auth.oauth",
        "mote.runtime.models.auth.oauth.registry",
        "mote.product.models",
        "mote.product.presentation.projection",
    }
)


def test_compatibility_facade_files_are_deleted() -> None:
    assert [relative for relative in RETIRED if (ROOT / relative).exists()] == []


def test_consumers_use_defining_modules_not_retired_facades() -> None:
    violations: list[str] = []
    for package in ("contracts", "kernel", "runtime", "orchestration", "product", "ztest"):
        for path in (ROOT / package).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in RETIRED_IMPORTS:
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
                if isinstance(node, ast.Import) and any(alias.name in RETIRED_IMPORTS for alias in node.names):
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert violations == []


def test_sandbox_facade_does_not_claim_nonexistent_wrap_command() -> None:
    sandbox = (ROOT / "runtime" / "sandbox" / "__init__.py").read_text(encoding="utf-8")
    assert "wrap_command" not in sandbox
    assert "SandboxRuntime" not in sandbox
    assert "detect_backend" not in sandbox
    assert "SandboxViolation" not in sandbox


def test_oauth_classifier_does_not_reexport_error_hierarchy() -> None:
    path = ROOT / "runtime" / "models" / "auth" / "oauth" / "errors.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names}
    assert imported & {"OAuthConfigError", "OAuthError", "OAuthHTTPError"} == set()
