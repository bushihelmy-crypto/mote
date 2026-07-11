"""Cache-aware pricing + pricing modes."""
import pytest
from mote.router.cost import DEFAULT_UNKNOWN_PRICING, PricingMode, TokenUsage, cost_of, lookup_pricing


def test_lookup_exact_and_prefix():
    p, known = lookup_pricing("gpt-4o")
    assert known and p.input == 5.0  # per-Mtok (0.005/1k * 1000)

    # dated/suffixed opus name resolves to the opus-4 tier by containment
    p2, known2 = lookup_pricing("claude-opus-4-20250101")
    assert known2 and p2.input == 15.0 and p2.output == 75.0


def test_lookup_unknown_falls_back():
    p, known = lookup_pricing("totally-made-up-model")
    assert not known
    assert p == DEFAULT_UNKNOWN_PRICING


def test_cost_of_cache_aware():
    # opus tier: input 15, output 75, cache_write 18.75, cache_read 1.5 per Mtok
    u = TokenUsage(
        input_tokens=1_000_000,
        cached_input_tokens=200_000,
        cache_creation_tokens=100_000,
        output_tokens=500_000,
    )
    cost, known = cost_of(u, "claude-opus-4", PricingMode.STANDARD)
    assert known
    # non-cached input = 800k
    expected = (
        0.8 * 15.0  # non-cached input (Mtok)
        + 0.2 * 1.5  # cache read
        + 0.1 * 18.75  # cache write
        + 0.5 * 75.0  # output
    )
    assert cost == pytest.approx(expected)


def test_unknown_model_still_billed():
    u = TokenUsage(input_tokens=1_000_000, output_tokens=0)
    cost, known = cost_of(u, "mystery-llm", PricingMode.STANDARD)
    assert not known
    assert cost == pytest.approx(DEFAULT_UNKNOWN_PRICING.input)  # 1 Mtok * input rate


def test_free_mode_is_zero():
    u = TokenUsage(input_tokens=10_000_000, output_tokens=10_000_000)
    cost, known = cost_of(u, "any-open-llm", PricingMode.FREE)
    assert known and cost == 0.0


def test_fireworks_mode_grades_by_size():
    u = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    small, _ = cost_of(u, "llama-v2-7b", PricingMode.FIREWORKS)
    big, _ = cost_of(u, "llama-v2-70b", PricingMode.FIREWORKS)
    mixtral, _ = cost_of(u, "mixtral-8x7b", PricingMode.FIREWORKS)
    # 7b → grade "16" (0.2/0.8); 70b → grade "80" (0.7/2.8)
    assert small == pytest.approx(0.2 + 0.8)
    assert big == pytest.approx(0.7 + 2.8)
    assert mixtral == pytest.approx(0.4 + 1.6)
