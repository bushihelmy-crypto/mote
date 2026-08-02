import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from mote.contracts.config.base import ConfigModel


class _RepresentativeConfig(ConfigModel):
    enabled: bool = True
    limit: int = 3


def test_config_model_has_one_canonical_definition() -> None:
    root = Path(__file__).parents[2]
    definitions = []
    for layer in ("contracts", "runtime", "product"):
        for path in (root / layer).rglob("*.py"):
            if "class ConfigModel(" in path.read_text(encoding="utf-8"):
                definitions.append(path.relative_to(root).as_posix())
    assert definitions == ["contracts/config/base.py"]


def test_canonical_config_model_preserves_forbid_extra_defaults_and_dump() -> None:
    assert _RepresentativeConfig().model_dump() == {"enabled": True, "limit": 3}
    with pytest.raises(ValidationError):
        _RepresentativeConfig(enabled=True, unknown="rejected")


def test_production_config_declarations_do_not_inherit_pydantic_base_directly() -> None:
    root = Path(__file__).parents[2]
    violations: list[str] = []
    for package in ("contracts/config", "runtime/config", "product/config"):
        for path in (root / package).rglob("*.py"):
            if path == root / "contracts/config/base.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in tree.body:
                if isinstance(node, ast.ClassDef) and any(
                    isinstance(base, ast.Name) and base.id == "BaseModel" for base in node.bases
                ):
                    violations.append(f"{path.relative_to(root)}:{node.lineno}")
    assert violations == []


def test_runtime_deployment_config_types_use_canonical_base() -> None:
    from mote.runtime.agent.role_schema import BrowserClientCert, RoleSchema
    from mote.runtime.file_watch.config import FileWatchConfig
    from mote.runtime.sandbox.config import CredentialConfig, SandboxRuntimeConfig
    from mote.runtime.tools.permission.config import PermissionConfig, SandboxConfig

    declarations = (
        BrowserClientCert,
        CredentialConfig,
        FileWatchConfig,
        PermissionConfig,
        RoleSchema,
        SandboxConfig,
        SandboxRuntimeConfig,
    )
    assert all(issubclass(declaration, ConfigModel) for declaration in declarations)

    root = Path(__file__).parents[2]
    for relative in (
        "runtime/agent/role_schema.py",
        "runtime/file_watch/config.py",
        "runtime/sandbox/config.py",
        "runtime/tools/permission/config.py",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        assert "class " in source
        assert "(BaseModel)" not in source
