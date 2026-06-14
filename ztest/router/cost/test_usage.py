"""TokenUsage adapters + accumulation."""
from openai.types import CompletionUsage

from metagpt.router.cost import EMPTY_USAGE, TokenUsage


def test_from_openai_dict_with_details():
    u = TokenUsage.from_openai(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "total_tokens": 1500,
            "prompt_tokens_details": {"cached_tokens": 200},
            "completion_tokens_details": {"reasoning_tokens": 120},
        }
    )
    assert u.input_tokens == 1000
    assert u.cached_input_tokens == 200
    assert u.output_tokens == 500
    assert u.reasoning_tokens == 120
    assert u.total_tokens == 1500
    assert u.non_cached_input() == 800
    assert u.blended_total() == 1300  # 800 non-cached input + 500 output


def test_from_openai_pydantic_model_and_missing_total():
    usage = CompletionUsage(prompt_tokens=300, completion_tokens=100, total_tokens=0)
    u = TokenUsage.from_openai(usage)
    assert u.input_tokens == 300
    assert u.output_tokens == 100
    # total backfilled from prompt+completion when provider reports 0
    assert u.total_tokens == 400


def test_from_anthropic_reconstructs_input_total():
    u = TokenUsage.from_anthropic(
        {
            "input_tokens": 500,  # anthropic reports UNcached prompt here
            "cache_read_input_tokens": 300,
            "cache_creation_input_tokens": 40,
            "output_tokens": 90,
        }
    )
    # input_tokens normalized to include cache reads → comparable to OpenAI
    assert u.input_tokens == 800
    assert u.cached_input_tokens == 300
    assert u.cache_creation_tokens == 40
    assert u.non_cached_input() == 500
    assert u.total_tokens == 800 + 40 + 90


def test_from_usage_autodetect():
    anth = TokenUsage.from_usage({"input_tokens": 10, "cache_read_input_tokens": 5, "output_tokens": 2})
    assert anth.cached_input_tokens == 5 and anth.input_tokens == 15

    oai = TokenUsage.from_usage({"prompt_tokens": 10, "completion_tokens": 2})
    assert oai.input_tokens == 10 and oai.output_tokens == 2

    assert TokenUsage.from_usage(None).is_zero()
    same = TokenUsage(input_tokens=7)
    assert TokenUsage.from_usage(same) is same


def test_accumulation():
    a = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)
    b = TokenUsage(input_tokens=3, cached_input_tokens=1, output_tokens=2, total_tokens=5)
    a += b
    assert a.input_tokens == 13
    assert a.cached_input_tokens == 1
    assert a.output_tokens == 7
    assert a.total_tokens == 20
    # __add__ is non-mutating
    c = a + b
    assert c.input_tokens == 16 and a.input_tokens == 13


def test_empty_usage_is_zero():
    assert EMPTY_USAGE.is_zero()
