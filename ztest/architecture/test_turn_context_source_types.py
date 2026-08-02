from __future__ import annotations

import asyncio
from pathlib import Path

from mote.contracts.conversation.context import FoldState, TokenState
from mote.contracts.events.conversation import HistoryEditedEvent
from mote.contracts.ports.conversation.turn_context import EphemeralContextSource, ModelContextRebuildSource
from mote.runtime.context.turn.sources.fold_pressure import FoldPressureContextSource
from mote.runtime.context.turn.sources.token_pressure import TokenPressureContextSource


class TokenProvider:
    def token_state(self) -> TokenState:
        return TokenState(90, "model", 100, 95, 10, True, False, False, False)


class FoldProvider:
    def __init__(self) -> None:
        self.state = FoldState(True, 8, 10, 4)

    def fold_state(self) -> FoldState:
        return self.state


class BasicSource:
    name = "basic"
    priority = 1
    save_to_context = False

    async def render(self, *, cwd: str | None = None) -> str | None:
        return None


class RebuildSource(BasicSource):
    async def on_model_context_rebuilt(self, event: HistoryEditedEvent) -> None:
        return None


def test_pressure_sources_consume_canonical_state_dtos() -> None:
    token = asyncio.run(TokenPressureContextSource(TokenProvider()).render())
    fold = asyncio.run(FoldPressureContextSource(FoldProvider()).render())

    assert token is not None and "10%" in token
    assert fold is not None and "4 most recent" in fold


def test_fold_warning_is_deterministically_rising_edge_only() -> None:
    source = FoldPressureContextSource(FoldProvider())

    assert asyncio.run(source.render()) is not None
    assert asyncio.run(source.render()) is None


def test_basic_and_rebuild_capabilities_are_explicit_protocols() -> None:
    assert isinstance(BasicSource(), EphemeralContextSource)
    assert not isinstance(BasicSource(), ModelContextRebuildSource)
    assert isinstance(RebuildSource(), ModelContextRebuildSource)


def test_turn_context_bus_and_pressure_sources_have_no_capability_reflection() -> None:
    for relative in (
        "runtime/context/turn/bus.py",
        "runtime/context/turn/sources/token_pressure.py",
        "runtime/context/turn/sources/fold_pressure.py",
        "runtime/context/turn/sources/skill_activation.py",
        "runtime/context/turn/sources/skill_listing.py",
        "runtime/context/turn/sources/tool_catalog.py",
        "runtime/context/turn/sources/changed_files.py",
    ):
        source = Path(relative).read_text(encoding="utf-8")
        assert "getattr(" not in source
        assert "-> object" not in source
