"""Telemetry carries observations; control can only live in domain Policies."""

from __future__ import annotations

from pathlib import Path

from mote.contracts.events.session import TurnEndEvent
from mote.product.code_map.turn_context import CodeMapContextSource
from mote.runtime.context import ContextManager
from mote.runtime.context.turn.sources.compaction import CompactionNoticeContextSource
from mote.runtime.context.turn.sources.git import GitContextSource
from mote.runtime.context.turn.sources.skill_listing import SkillListingContextSource
from mote.runtime.context.turn.sources.team import TeamContextSource
from mote.runtime.context.turn.sources.tool_catalog import ToolCatalogContextSource
from mote.runtime.hook.subscriber import HookSubscriber
from mote.runtime.session.log import SessionLog

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_ROOTS = ("contracts", "kernel", "runtime", "orchestration", "product")
FORBIDDEN_CONTROL_SYMBOLS = (
    "CompactOutcome",
    "ControlEvent",
    "ControlOutcome",
    "ControlStage",
    "ControlSubscriber",
    "PostToolUseEvent",
    "PreCompactEvent",
    "PreToolUseEvent",
    "ResourceReconcileSubscriber",
    "TurnOutcome",
)

MODEL_CONTEXT_REBUILD_SOURCES = (
    ToolCatalogContextSource,
    SkillListingContextSource,
    TeamContextSource,
    CodeMapContextSource,
    CompactionNoticeContextSource,
    GitContextSource,
)


def test_removed_event_control_types_cannot_return():
    violations: list[str] = []
    for root_name in PRODUCTION_ROOTS:
        for path in (PACKAGE_ROOT / root_name).rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            for symbol in FORBIDDEN_CONTROL_SYMBOLS:
                if symbol in source:
                    relative = path.relative_to(PACKAGE_ROOT).as_posix()
                    violations.append(f"{relative}: {symbol}")

    assert violations == []
    assert not (PACKAGE_ROOT / "contracts" / "events" / "outcomes.py").exists()


def test_turn_end_and_hook_adapter_are_observation_only():
    assert not hasattr(TurnEndEvent, "outcome_type")
    assert hasattr(HookSubscriber, "handle")
    assert not hasattr(HookSubscriber, "evaluate")


def test_live_session_mutations_have_no_direct_journal_bypass():
    assert not hasattr(SessionLog, "append_sync")
    production_wiring = (PACKAGE_ROOT / "runtime" / "agent" / "components" / "session.py").read_text(encoding="utf-8")
    assert "commit_offline" not in production_wiring


def test_correctness_projections_cannot_return_to_lossy_telemetry():
    assert not hasattr(ContextManager, "handle")
    for source in MODEL_CONTEXT_REBUILD_SOURCES:
        assert not hasattr(source, "handle")
        assert not hasattr(source, "telemetry_observer")
