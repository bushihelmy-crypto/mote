"""Contract-level tests for the Product Squilla routing policy."""

from __future__ import annotations

import pytest

from mote.contracts.models.routing import (
    RecentRoutingDecision,
    RouteCandidate,
    RoutingDegradedReason,
    RoutingInput,
    RoutingSessionState,
    RoutingSignals,
    SeedFloor,
)
from mote.product.routing.squilla.ml.runtime import RoutingModelRuntime
from mote.product.routing.squilla.strategy import (
    SquillaStrategy,
    detect_complaint,
    route_class_for_index,
    route_index,
    score_to_probs,
)


@pytest.fixture
def squilla(tmp_path):
    async def inline(function, *args):
        return function(*args)

    return SquillaStrategy(
        RoutingModelRuntime(model_dir=tmp_path / "no-bundle"),
        inference_runner=inline,
    )


@pytest.fixture
def candidates():
    return tuple(
        RouteCandidate(
            route_id=f"route-{rank}",
            quality_class=f"R{rank}",
            quality_rank=rank,
            context_tokens=200_000,
        )
        for rank in range(4)
    )


def routing_input(
    prompt: str,
    *,
    estimated_tokens: int = 0,
    flags: frozenset[str] = frozenset(),
) -> RoutingInput:
    return RoutingInput(
        decision_id="decision",
        session_id="session",
        task="interactive",
        signals=RoutingSignals(
            prompt_text=prompt,
            estimated_tokens=estimated_tokens,
            flags=flags,
        ),
    )


class TestScoreToProbs:
    def test_returns_four_class_normalized(self):
        probs = score_to_probs(6.0)
        assert len(probs) == 4
        assert pytest.approx(sum(probs), abs=1e-9) == 1.0
        assert all(probability >= 0 for probability in probs)

    def test_low_and_high_scores_peak_at_the_expected_class(self):
        assert score_to_probs(0.0)[0] == max(score_to_probs(0.0))
        assert score_to_probs(12.0)[3] == max(score_to_probs(12.0))

    def test_score_is_clamped_to_span(self):
        assert score_to_probs(999.0) == score_to_probs(12.0)


class TestRouteClassHelpers:
    def test_round_trip_and_bounds(self):
        for index, route_class in enumerate(("R0", "R1", "R2", "R3")):
            assert route_index(route_class) == index
            assert route_class_for_index(index) == route_class
        assert route_index("unknown") == 1
        assert route_class_for_index(-1) == "R0"
        assert route_class_for_index(99) == "R3"


class TestComplaintDetection:
    def test_multilingual_terms(self):
        assert "完全不对" in detect_complaint("这个答案完全不对，重写")
        assert "wrong" in detect_complaint("this is wrong, try again")

    def test_clean_or_long_message_is_ignored(self):
        assert detect_complaint("please summarize this document") == []
        assert detect_complaint("完全不对 " + "x" * 200, max_chars=160) == []


class TestSquillaPolicy:
    @pytest.mark.asyncio
    async def test_unavailable_ml_returns_typed_degraded_proposal(self, squilla, candidates):
        assert await squilla.runtime.prewarm() is False
        proposal = await squilla.propose(routing_input("好的，谢谢"), candidates, RoutingSessionState())
        assert proposal.degraded_reason is RoutingDegradedReason.ML_UNAVAILABLE
        assert proposal.policy_revision.endswith("@v4.2_phase3_inference")
        assert proposal.final_class in {"R0", "R1"}
        assert proposal.selected_route_id in {"route-0", "route-1"}

    @pytest.mark.asyncio
    async def test_complex_prompt_and_risk_flag_escalate(self, squilla, candidates):
        prompt = "请重新设计整个系统架构，迁移生产数据库，这是不可逆的高风险操作，" "需要跨所有模块进行重构并评估安全影响。"
        proposal = await squilla.propose(
            routing_input(prompt, flags=frozenset({"high_risk"})),
            candidates,
            RoutingSessionState(),
        )
        assert proposal.final_class in {"R2", "R3"}

    @pytest.mark.asyncio
    async def test_seed_is_an_explicit_raise_only_transition(self, squilla, candidates):
        state = RoutingSessionState(seed_floor=SeedFloor(route_class="R3", turns_remaining=2))
        proposal = await squilla.propose(routing_input("ok"), candidates, state)
        assert proposal.final_class == "R3"
        assert proposal.state_transition.consume_seed is True
        assert state.seed_floor is not None
        assert state.seed_floor.turns_remaining == 2

    @pytest.mark.asyncio
    async def test_recent_state_prevents_downgrade_without_hidden_store(self, squilla, candidates):
        state = RoutingSessionState(
            recent_decisions=(
                RecentRoutingDecision(
                    decision_id="previous",
                    selected_route_id="route-3",
                    final_class="R3",
                    turn_id=1,
                ),
            )
        )
        proposal = await squilla.propose(routing_input("ok"), candidates, state)
        assert proposal.final_class == "R3"
        assert not hasattr(squilla, "history")
        assert not hasattr(squilla, "seed_floors")
        assert not hasattr(squilla, "control_holds")

    @pytest.mark.asyncio
    async def test_large_context_floor_uses_typed_token_signal(self, squilla, candidates):
        proposal = await squilla.propose(
            routing_input("continue", estimated_tokens=90_000),
            candidates,
            RoutingSessionState(),
        )
        assert proposal.final_class == "R3"
