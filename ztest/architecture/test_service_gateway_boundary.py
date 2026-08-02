"""Architecture locks for hosted Tool service failover."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]


def _imports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    return tuple(modules)


def test_service_contracts_only_depend_on_contracts() -> None:
    paths = (
        ROOT / "contracts/service/models.py",
        ROOT / "contracts/service/journal.py",
        ROOT / "contracts/ports/service/endpoint.py",
        ROOT / "contracts/ports/service/gateway.py",
        ROOT / "contracts/ports/service/call_journal.py",
    )
    violations = [
        f"{path.relative_to(ROOT)}: {module}"
        for path in paths
        for module in _imports(path)
        if module.startswith(("mote.kernel", "mote.runtime", "mote.orchestration", "mote.product"))
    ]
    assert violations == []


def test_runtime_service_gateway_never_imports_product() -> None:
    violations = [
        f"{path.relative_to(ROOT)}: {module}"
        for path in (ROOT / "runtime/service_gateway").rglob("*.py")
        for module in _imports(path)
        if module.startswith("mote.product")
    ]
    assert violations == []


def test_product_endpoint_resolver_uses_typed_close_contract() -> None:
    source = (ROOT / "product/composition/service_endpoints.py").read_text(encoding="utf-8")
    assert "await resolver.aclose()" in source
    assert "getattr(" not in source
    assert "isawaitable" not in source


def test_media_providers_do_not_own_retry_or_poll_loops() -> None:
    root = ROOT / "product/toolsets/builtin/generate_media"
    sources = {path.relative_to(ROOT): path.read_text(encoding="utf-8") for path in root.rglob("*.py")}
    forbidden = (
        "_generate_one_with_retry",
        "_poll_until_done",
        "RecoveryRunner",
        "async def generate(",
        "task_id = None",
    )
    violations = [f"{path}: {token}" for path, source in sources.items() for token in forbidden if token in source]
    assert violations == []


def test_generate_media_uses_only_the_service_capability() -> None:
    path = ROOT / "product/toolsets/builtin/generate_media/generate_media_tool.py"
    source = path.read_text(encoding="utf-8")
    assert '"invoke_service",' in source
    assert "ServiceExecutionSemantics.IDEMPOTENT" in source
    assert "_provider_factory" not in source


def test_web_search_uses_only_the_service_capability() -> None:
    path = ROOT / "product/toolsets/builtin/web_search.py"
    source = path.read_text(encoding="utf-8")
    assert 'requires: ClassVar[tuple[str, ...]] = ("invoke_service",)' in source
    assert "ServiceExecutionSemantics.PURE" in source
    assert "WebSearchCapability" not in source
    assert "_backend_factory" not in source


def test_web_search_adapter_does_not_own_retry_loops() -> None:
    path = ROOT / "product/web_search/service.py"
    source = path.read_text(encoding="utf-8")
    forbidden = ("RecoveryRunner", "while True", "for attempt in", "tenacity")
    assert [token for token in forbidden if token in source] == []


def test_hosted_service_governance_names_the_real_application_gateway() -> None:
    from mote.contracts.composition import InstanceScope
    from mote.product.composition.governance import CAPABILITY_DECLARATIONS

    declaration = next(item for item in CAPABILITY_DECLARATIONS if item.capability_id == "hosted-service-gateway")
    assert declaration.implementation == ("mote.runtime.service_gateway.gateway.RuntimeServiceGateway")
    assert declaration.canonical_factory == ("mote.product.composition.service_gateway.builtin_service_gateway")
    assert declaration.required_ports == ("mote.contracts.ports.service.gateway.ServiceGateway",)
    assert declaration.instance_scope is InstanceScope.APPLICATION
    assert declaration.lifecycle_owner == "product-composition"

    bootstrap = (ROOT / "product/composition/bootstrap.py").read_text(encoding="utf-8")
    assert "service_gateway = builtin_service_gateway(" in bootstrap
    assert "context.service_gateway = service_gateway" in bootstrap
    assert 'name="hosted-service:reconciler-gateway"' in bootstrap
    assert "RuntimeModelInferencePort" not in declaration.implementation
