"""CostTracker aggregation + legacy CostManager shim."""
import pytest

from metagpt.router.cost import Costs, CostTracker, PricingMode, TokenUsage


def test_per_model_and_session_aggregation():
    t = CostTracker()
    t.add(TokenUsage(input_tokens=1000, output_tokens=500, total_tokens=1500), "gpt-4o")
    t.add(TokenUsage(input_tokens=2000, output_tokens=100, total_tokens=2100), "gpt-4o")
    t.add(TokenUsage(input_tokens=300, output_tokens=50, total_tokens=350), "claude-opus-4")

    assert set(t.model_usage) == {"gpt-4o", "claude-opus-4"}
    gpt = t.model_usage["gpt-4o"]
    assert gpt.usage.input_tokens == 3000
    assert gpt.usage.output_tokens == 600
    assert gpt.requests == 2

    total = t.total_token_usage()
    assert total.input_tokens == 3300
    assert total.output_tokens == 650
    assert t.total_cost > 0


def test_zero_usage_is_ignored():
    t = CostTracker()
    t.add(TokenUsage(), "gpt-4o")
    assert t.model_usage == {}
    assert t.total_cost == 0.0


def test_legacy_update_cost_and_get_costs():
    t = CostTracker()
    t.update_cost(1000, 500, "gpt-4o")
    costs = t.get_costs()
    assert isinstance(costs, Costs)
    assert costs.total_prompt_tokens == 1000
    assert costs.total_completion_tokens == 500
    assert costs.total_cost == pytest.approx(t.total_cost)
    # legacy property accessors
    assert t.total_prompt_tokens == 1000
    assert t.total_completion_tokens == 500
    assert t.get_total_prompt_tokens() == 1000


def test_unknown_model_flag():
    t = CostTracker()
    t.add(TokenUsage(input_tokens=100, output_tokens=10, total_tokens=110), "gpt-4o")
    assert not t.has_unknown_model_cost
    t.add(TokenUsage(input_tokens=100, output_tokens=10, total_tokens=110), "no-such-model")
    assert t.has_unknown_model_cost


def test_free_mode_tracks_tokens_without_cost():
    t = CostTracker(mode=PricingMode.FREE)
    t.add(TokenUsage(input_tokens=1000, output_tokens=500, total_tokens=1500), "local-llm")
    assert t.total_cost == 0.0
    assert t.total_token_usage().input_tokens == 1000


def test_context_remaining():
    t = CostTracker()
    t.add(TokenUsage(input_tokens=60000, output_tokens=2000, total_tokens=62000), "gpt-4o")
    ctx = t.context_remaining()
    assert ctx["window"] == 128000
    assert ctx["used"] == 62000
    assert 0 < ctx["percent_left"] < 100
    # explicit model override works too
    assert t.context_remaining("gpt-4o")["window"] == 128000


def test_on_record_sink():
    seen = []
    t = CostTracker(on_record=lambda u, m, c: seen.append((m, u.input_tokens, c)))
    t.add(TokenUsage(input_tokens=10, output_tokens=2, total_tokens=12), "gpt-4o")
    assert len(seen) == 1
    assert seen[0][0] == "gpt-4o" and seen[0][1] == 10
