from pathlib import Path
from typing import get_type_hints

from mote.runtime.agent.wiring import AgentDependencies
from mote.runtime.models.clients.context import Context

ROOT = Path(__file__).resolve().parents[2]


def test_runtime_model_context_consumes_only_contracts_projection() -> None:
    hints = get_type_hints(Context)
    assert "config" not in hints
    assert hints["activation"].__name__ == "RuntimeClientActivationSpec"
    source = (ROOT / "runtime/models/clients/context.py").read_text(encoding="utf-8")
    assert "from typing import Any" not in source
    assert "mote.product.config" not in source


def test_agent_wiring_exposes_only_narrow_component_projection() -> None:
    annotations = AgentDependencies.__annotations__
    assert set(annotations) == {"deps", "output_contract", "component_projection", "run_lease_policy"}
    source = (ROOT / "runtime/agent/wiring.py").read_text(encoding="utf-8")
    assert "Path" not in source
    assert "routing_strategy_builders" not in source
    assert "AgentActivationInputs" not in source
    assert "AgentStorageInputs" not in source
    assert "AgentInteractiveInputs" not in source


def test_runtime_never_reads_product_root_config() -> None:
    offenders = []
    for path in (ROOT / "runtime").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "mote.product.config" in source and not source.lstrip().startswith('"""'):
            offenders.append(path)
        assert "context.config" not in source, path
    assert offenders == []
