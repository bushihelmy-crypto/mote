#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Stable declarative model capability profiles.

This is the keystone the LLM-provider layer hangs off. It consolidates the four
formerly-scattered per-model capability substring lists (vision / PDF input /
native tool search / native web search) into ONE mergeable, model-name-keyed
:class:`ModelProfile` registry, and adds two new capability facets that had no
home before: ``supports_thinking`` (the reasoning/thinking gate) and
``json_schema_transformer`` (a per-model tool-schema rewrite hook).

Design (mirrors pydantic-ai's ModelProfile merge, adapted to mote):

* A :class:`ModelProfile` is a frozen dataclass of capability facets, every one
  defaulting to the "off" value (``False`` / ``None``).
* The registry is an ORDERED list of ``(marker_substring, fragment)`` rows, where
  each ``fragment`` is a :class:`ModelProfile` carrying ONLY the facets that
  marker turns on. :func:`profile_for` folds every fragment whose marker is a
  substring of the (lower-cased) model name over :data:`DEFAULT_PROFILE` via
  :func:`merge_profile`. Because a fragment only ever sets facets away from their
  default, the fold is an OR over capability flags — byte-equivalent to the old
  ``any(marker in model for marker in LIST)`` checks, but now composable (a model
  name matching several markers accumulates all their capabilities).

``common`` is a leaf layer, so BOTH ``common/const/llm.py`` (whose ``supports_*``
functions are now thin delegates) AND the ``router/llm/*`` providers can import
this with no cycle.

Case: the old ``supports_vision`` was case-sensitive while the other three
lower-cased. :func:`profile_for` normalises to ``.lower()`` for ALL facets — this
is behaviour-preserving for every real model id (all markers are lower-case).
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Callable, Optional

__all__ = ["ModelProfile", "DEFAULT_PROFILE", "merge_profile", "profile_for"]


@dataclass(frozen=True)
class ModelProfile:
    """Capability facets for one model, resolved by name-substring markers.

    Every facet defaults to "off" (``False`` / ``None``) so a bare
    :class:`ModelProfile` (== :data:`DEFAULT_PROFILE`) is the safe, capability-less
    baseline. Registry fragments set only the facets their marker enables; the
    fold in :func:`profile_for` ORs them together.
    """

    # Accepts IMAGE input (OpenAI-compatible ``image_url`` block). PER-VARIANT,
    # not per-brand (a family ships a text-only flagship + a separate vision
    # variant), so the Chinese markers are the narrow "-vl"/"-v"/"vision" ones.
    supports_vision: bool = False
    # Accepts native PDF (document) input (Anthropic Messages ``document`` block).
    supports_pdf_input: bool = False
    # PROVIDER-NATIVE Tool Search (server-side ``defer_loading``): a deferred
    # tool's definition rides the wire so the API excludes it from the cached
    # prefix until discovery, keeping the ``tools=`` prefix byte-stable.
    supports_native_tool_search: bool = False
    # PROVIDER-NATIVE server-side web search (Anthropic ``web_search_20250305`` /
    # OpenAI Responses ``web_search``).
    supports_web_search: bool = False
    # Extended thinking / reasoning-effort: gates whether ``reasoning_effort`` is
    # translated into the provider's native thinking shape (Anthropic
    # ``thinking={...}`` / OpenAI ``reasoning_effort`` / Responses ``reasoning``).
    supports_thinking: bool = False
    supports_native_structured_output: bool = False
    # Optional per-model rewrite of each tool's JSON Schema before it is wrapped
    # in the provider envelope (for a model that rejects a schema construct other
    # models accept). ``None`` == identity (the common case). The exception layer
    # never imports transformers; this is a pure ``dict -> dict`` value hook.
    json_schema_transformer: Optional[Callable[[dict], dict]] = None


DEFAULT_PROFILE = ModelProfile()


def merge_profile(base: ModelProfile, override: Optional[ModelProfile]) -> ModelProfile:
    """Overlay ``override``'s non-default facets onto ``base`` (override wins).

    Only the facets ``override`` sets AWAY from their default are applied, so a
    fragment carrying a single ``supports_vision=True`` contributes exactly that
    one facet and leaves everything else in ``base`` intact (the pydantic-ai
    ModelProfile merge pattern). ``override=None`` returns ``base`` unchanged.
    """
    if override is None:
        return base
    changed = {f.name: getattr(override, f.name) for f in fields(override) if getattr(override, f.name) != f.default}
    return replace(base, **changed) if changed else base


# ---------------------------------------------------------------------------
# The registry: ordered ``(marker_substring, fragment)`` rows. A marker is a
# lower-case model-name substring; its fragment carries ONLY the facets that
# marker enables. ``profile_for`` folds every matching fragment, so a model name
# hitting several markers accumulates all their capabilities (e.g.
# "claude-opus-4-8" hits "opus" → vision, "claude" → pdf, and "opus-4" →
# native-tool-search + web-search + thinking).
#
# Grouped to mirror the four former lists in common/const/llm.py plus the new
# thinking facet. Extend a row (or add one) as new capable models land; to fix a
# per-model schema quirk, set ``json_schema_transformer=`` on the relevant row
# (none needed today — the hook stays inert until a real quirk appears).
# ---------------------------------------------------------------------------

_VISION = ModelProfile(supports_vision=True)
_PDF = ModelProfile(supports_pdf_input=True)
# gpt-5 / o3 / o4 are vision + native web search + reasoning-capable.
_OPENAI_REASONING = ModelProfile(
    supports_vision=True, supports_web_search=True, supports_thinking=True, supports_native_structured_output=True
)
# gpt-4o / gpt-4.1 are vision + native web search (no reasoning effort).
_OPENAI_VISION_WEB = ModelProfile(
    supports_vision=True, supports_web_search=True, supports_native_structured_output=True
)
# Claude 4 family (opus-4 / sonnet-4 / haiku-4): native tool search + native web
# search + extended thinking. Vision + PDF arrive via the "opus"/"sonnet"/"claude"
# markers each real id also matches.
_CLAUDE4 = ModelProfile(supports_native_tool_search=True, supports_web_search=True, supports_thinking=True)

_PROFILE_REGISTRY: list[tuple[str, ModelProfile]] = [
    # ── Vision (image input) ────────────────────────────────────────────────
    # OpenAI vision + web-search families (gpt-5/o3/o4 also reasoning-capable).
    ("gpt-4o", _OPENAI_VISION_WEB),
    ("gpt-4o-mini", _OPENAI_VISION_WEB),
    ("gpt-4.1", _OPENAI_VISION_WEB),
    ("gpt-5", _OPENAI_REASONING),
    ("o3", _OPENAI_REASONING),
    ("o4", _OPENAI_REASONING),
    # Anthropic (Claude 3+ Sonnet/Opus are multimodal; Haiku deliberately out).
    ("sonnet", _VISION),
    ("opus", _VISION),
    # Google.
    ("gemini", _VISION),
    # Chinese vision variants — narrow markers only.
    ("-vl", _VISION),
    ("vision", _VISION),
    ("glm-4v", _VISION),
    ("glm-4.1v", _VISION),
    ("glm-4.5v", _VISION),
    ("glm-4.6v", _VISION),
    ("kimi-vl", _VISION),
    # ── Native PDF (document) input ─────────────────────────────────────────
    ("claude", _PDF),
    # ── Native Tool Search + web search + thinking (Claude 4 family) ─────────
    ("opus-4", _CLAUDE4),
    ("sonnet-4", _CLAUDE4),
    ("haiku-4", _CLAUDE4),
    # ── Native Tool Search (OpenAI gpt-5.4+ Responses; vision/web/thinking
    #    arrive via the "gpt-5" marker each id also matches) ─────────────────
    ("gpt-5.4", ModelProfile(supports_native_tool_search=True)),
    ("gpt-5.5", ModelProfile(supports_native_tool_search=True)),
]


def profile_for(model: Optional[str]) -> ModelProfile:
    """Resolve the merged :class:`ModelProfile` for a model name.

    Folds every registry fragment whose marker is a substring of the lower-cased
    ``model`` over :data:`DEFAULT_PROFILE`. ``None`` / unknown model → the
    all-off :data:`DEFAULT_PROFILE`. THE single authority every capability check
    delegates to (``common/const/llm.py``'s ``supports_*`` functions, the
    provider ``_cons_kwargs`` thinking gate, the tool-schema transformer lookup).
    """
    low = (model or "").lower()
    profile = DEFAULT_PROFILE
    for marker, fragment in _PROFILE_REGISTRY:
        if marker in low:
            profile = merge_profile(profile, fragment)
    return profile
