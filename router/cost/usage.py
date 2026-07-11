"""Unified token-usage accounting — a synthesis of Codex and Claude Code.

``TokenUsage`` is the single normalized record every provider's raw usage is
mapped into before it reaches the :class:`~mote.router.cost.tracker.CostTracker`.
Its field set is the *union* of what the two reference agents track:

- Codex's ``TokenUsage`` (``input``/``cached_input``/``output``/``reasoning_output``/
  ``total``) — note Codex folds cache *reads* into ``cached_input_tokens`` and
  never bills cache *writes* separately, and computes a single display metric
  ``blended_total = non_cached_input + output``.
- Claude Code's Anthropic usage (``input``/``output`` plus the two distinct cache
  buckets ``cache_read_input_tokens`` and ``cache_creation_input_tokens``), which
  are priced at *different* rates (read cheap, write expensive).

We keep cache reads and cache writes as separate fields so the cache-aware
pricing in ``pricing.py`` can bill them correctly, and we keep ``reasoning_tokens``
(a subset of ``output_tokens`` on reasoning models) for reporting.

Adapters (``from_openai`` / ``from_anthropic`` / ``from_usage``) accept either a
pydantic model (OpenAI ``CompletionUsage``, Anthropic usage objects) or a plain
dict, so the single call site in ``base_llm._update_costs`` can stay
provider-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Mapping, Union


def _get(obj: Any, key: str, default: Any = 0) -> Any:
    """Read ``key`` from a dict or an attribute off a pydantic/SDK object."""
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        val = obj.get(key, default)
    else:
        val = getattr(obj, key, default)
    return default if val is None else val


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


@dataclass
class TokenUsage:
    """Normalized per-call (or accumulated) token counts.

    All fields default to 0 so a partial provider payload still yields a valid
    record. ``input_tokens`` is the *total* prompt tokens as the provider counts
    them (cache reads INCLUDED, matching both OpenAI's ``prompt_tokens`` and
    Anthropic's separate accounting once normalized — see the adapters).
    """

    input_tokens: int = 0
    cached_input_tokens: int = 0  # cache READ (already-cached prompt, billed cheap)
    cache_creation_tokens: int = 0  # cache WRITE (Anthropic ephemeral, billed dear)
    output_tokens: int = 0
    reasoning_tokens: int = 0  # subset of output_tokens on reasoning models
    total_tokens: int = 0

    # -- derived metrics (Codex) -------------------------------------------
    def non_cached_input(self) -> int:
        """Prompt tokens that were NOT served from cache (billed at full rate)."""
        return max(0, self.input_tokens - max(0, self.cached_input_tokens))

    def blended_total(self) -> int:
        """Codex's single-number display metric: non-cached input + output."""
        return self.non_cached_input() + max(0, self.output_tokens)

    def is_zero(self) -> bool:
        return all(getattr(self, f.name) == 0 for f in fields(self))

    # -- accumulation -------------------------------------------------------
    def add(self, other: "TokenUsage") -> "TokenUsage":
        """In-place element-wise sum (Codex ``add_assign``). Returns self."""
        self.input_tokens += other.input_tokens
        self.cached_input_tokens += other.cached_input_tokens
        self.cache_creation_tokens += other.cache_creation_tokens
        self.output_tokens += other.output_tokens
        self.reasoning_tokens += other.reasoning_tokens
        self.total_tokens += other.total_tokens
        return self

    def __iadd__(self, other: "TokenUsage") -> "TokenUsage":
        return self.add(other)

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(**{f.name: getattr(self, f.name) for f in fields(self)}).add(other)

    def to_dict(self) -> dict[str, int]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    # -- adapters -----------------------------------------------------------
    @classmethod
    def from_openai(cls, usage: Any) -> "TokenUsage":
        """Map an OpenAI ``CompletionUsage`` (or dict) into a TokenUsage.

        Reads the nested ``prompt_tokens_details.cached_tokens`` and
        ``completion_tokens_details.reasoning_tokens`` when present (gpt-4o /
        o-series / cached-prompt responses). OpenAI has no cache-*write* notion,
        so ``cache_creation_tokens`` stays 0.
        """
        prompt = _as_int(_get(usage, "prompt_tokens"))
        completion = _as_int(_get(usage, "completion_tokens"))
        total = _as_int(_get(usage, "total_tokens")) or (prompt + completion)
        cached = _as_int(_get(_get(usage, "prompt_tokens_details", None), "cached_tokens"))
        reasoning = _as_int(_get(_get(usage, "completion_tokens_details", None), "reasoning_tokens"))
        return cls(
            input_tokens=prompt,
            cached_input_tokens=cached,
            output_tokens=completion,
            reasoning_tokens=reasoning,
            total_tokens=total,
        )

    @classmethod
    def from_anthropic(cls, usage: Any) -> "TokenUsage":
        """Map an Anthropic-style usage payload into a TokenUsage.

        Anthropic reports ``input_tokens`` as the *uncached* prompt tokens and
        the cache buckets separately, so we reconstruct the OpenAI-equivalent
        ``input_tokens`` (cache reads INCLUDED) to keep ``non_cached_input`` math
        consistent across providers.
        """
        raw_input = _as_int(_get(usage, "input_tokens"))
        cache_read = _as_int(_get(usage, "cache_read_input_tokens"))
        cache_creation = _as_int(_get(usage, "cache_creation_input_tokens"))
        output = _as_int(_get(usage, "output_tokens"))
        input_total = raw_input + cache_read
        return cls(
            input_tokens=input_total,
            cached_input_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            output_tokens=output,
            total_tokens=input_total + cache_creation + output,
        )

    @classmethod
    def from_usage(cls, usage: Union[Mapping, Any, None]) -> "TokenUsage":
        """Best-effort adapter that auto-detects the provider shape.

        The single entry point for ``base_llm._update_costs``: it inspects the
        keys/attrs to decide OpenAI vs Anthropic vs an already-normalized
        TokenUsage, falling back to the OpenAI mapping (the repo's lingua franca,
        since every model is reached through the OpenAI-compatible client).
        """
        if usage is None:
            return cls()
        if isinstance(usage, TokenUsage):
            return usage
        # Anthropic shape: has the distinctive cache buckets but no prompt_tokens.
        has_prompt = _get(usage, "prompt_tokens", None) is not None
        has_anthropic_cache = (
            _get(usage, "cache_read_input_tokens", None) is not None
            or _get(usage, "cache_creation_input_tokens", None) is not None
        )
        if not has_prompt and has_anthropic_cache:
            return cls.from_anthropic(usage)
        return cls.from_openai(usage)


EMPTY_USAGE: TokenUsage = TokenUsage()
