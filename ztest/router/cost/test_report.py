"""Reporting / formatting helpers."""
from mote.runtime.models.cost import (
    CostTracker,
    TokenUsage,
    final_output,
    format_cost,
    format_model_usage,
    format_total_cost,
    status_line_dict,
)


def _tracker():
    t = CostTracker()
    t.add(
        TokenUsage(
            input_tokens=1000,
            cached_input_tokens=200,
            output_tokens=500,
            reasoning_tokens=120,
            total_tokens=1500,
        ),
        "claude-opus-4",
    )
    t.add(TokenUsage(input_tokens=2000, output_tokens=300, total_tokens=2300), "gpt-4o")
    return t


def test_format_cost_precision():
    assert format_cost(1.2345) == "$1.23"
    assert format_cost(0.01234) == "$0.0123"


def test_format_total_cost_block():
    out = format_total_cost(_tracker())
    assert "Total cost:" in out
    assert "Total tokens: 3800" in out
    assert "claude-opus-4" in out
    assert "gpt-4o" in out


def test_format_model_usage_sorted_by_tokens():
    out = format_model_usage(_tracker())
    # gpt-4o (2300 tokens) sorts above claude-opus-4 (1500 tokens)
    assert out.index("gpt-4o") < out.index("claude-opus-4")


def test_final_output_codex_style():
    out = final_output(_tracker())
    assert out.startswith("Token usage: total=3800")
    assert "(+ 200 cached)" in out
    assert "reasoning 120" in out


def test_status_line_dict_shape():
    d = status_line_dict(_tracker())
    assert d["cost"]["total_input_tokens"] == 3000
    assert d["cost"]["total_output_tokens"] == 800
    assert d["cost"]["total_cache_read_tokens"] == 200
    ctx = d["context_window"]
    assert ctx["context_window_size"] == 128000  # last model = gpt-4o
    assert ctx["current_usage"] == 2300
    assert 0 <= ctx["remaining_percentage"] <= 100


def test_unknown_model_note_in_total():
    t = CostTracker()
    t.add(TokenUsage(input_tokens=10, output_tokens=2, total_tokens=12), "mystery")
    assert "estimated" in format_total_cost(t)
