"""Regression gates for the final architecture-debt review findings."""

from pathlib import Path

from mote.runtime.context.turn.sources.changed_files import ChangedFilesContextSource
from mote.runtime.context.turn.sources.fold_pressure import FoldPressureContextSource
from mote.runtime.context.turn.sources.git import GitContextSource
from mote.runtime.context.turn.sources.timestamp import TimestampContextSource
from mote.runtime.context.turn.sources.token_pressure import TokenPressureContextSource

ROOT = Path(__file__).resolve().parents[2]


def test_time_varying_turn_context_is_request_only() -> None:
    sources = (
        GitContextSource(),
        TimestampContextSource(),
        TokenPressureContextSource(None),
        FoldPressureContextSource(None),
        ChangedFilesContextSource(),
    )
    assert all(source.save_to_context is False for source in sources)


def test_typed_telemetry_binding_has_no_runtime_typeguard_narrowing() -> None:
    source = (ROOT / "runtime/events/telemetry.py").read_text(encoding="utf-8")
    assert "TypeGuard" not in source
    assert "_EventNarrower" not in source
    assert "_NarrowingAsyncHandler" not in source
    assert "event_type: type[EventT]" in source


def test_agent_components_receive_explicit_composition_inputs() -> None:
    components = ROOT / "runtime/agent/components"
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in components.glob("*.py")
        if "wiring.dependencies" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_hosting_wire_does_not_reflect_view_event_capabilities() -> None:
    for relative in ("product/interfaces/acp/wire.py", "product/interfaces/agui/wire.py"):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert 'getattr(event, "kind"' not in source


def test_hosting_protocols_use_shared_wire_json_without_any() -> None:
    roots = (
        ROOT / "product/interfaces/acp",
        ROOT / "product/interfaces/agui",
        ROOT / "product/session_hosting",
    )
    offenders: list[str] = []
    for root in roots:
        for path in root.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            if "from typing import Any" in source or ": Any" in source or "getattr(" in source:
                offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []
    assert (ROOT / "product/presentation/wire_types.py").exists()


def test_agent_wiring_contains_no_paths_or_factory_bag() -> None:
    source = (ROOT / "runtime/agent/wiring.py").read_text(encoding="utf-8")
    for forbidden in (
        "Path",
        "AgentActivationInputs",
        "AgentStorageInputs",
        "AgentInteractiveInputs",
        "routing_strategy_builders",
        "skill_service_factory",
        "lsp_service_factory",
    ):
        assert forbidden not in source
