#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LLM-related constants — model capability lists and checks."""

from typing import Optional

# Model-name substrings whose models accept IMAGE input. Coarse substring match
# (a hit means "can see"). This ONLY governs whether images are attached to the
# user message as an OpenAI-compatible ``image_url`` block — every provider mote
# talks to (incl. the OpenAI-compatible gateways the Chinese models use) accepts
# that shape, so vision is NOT gated on a specific transport (unlike web search /
# tool search, which need a first-party Anthropic/OpenAI-Responses server tool).
#
# IMPORTANT — vision is a PER-VARIANT fact, not per-brand: a family ships both a
# text-only flagship and a separate vision variant (deepseek-chat can't see but
# deepseek-vl can; qwen-max can't but qwen-vl / qwen3-vl can; glm-4 can't but
# glm-4v / glm-4.x-v can). So the Chinese entries are the NARROW vision markers
# ("-vl"/"-v"/"vision"), NEVER the bare brand — a bare "qwen"/"glm" substring
# would wrongly flag the text-only variants.
MULTI_MODAL_MODELS = [
    # OpenAI
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4.1",
    "gpt-5",
    "o3",
    "o4",
    # Anthropic (Claude 3+ Sonnet/Opus are all multimodal; Haiku deliberately out)
    "sonnet",
    "opus",
    # Google
    "gemini",
    # Chinese vision variants — narrow markers only (see note above)
    "-vl",  # qwen-vl / qwen2-vl / qwen3-vl, deepseek-vl, minimax-vl
    "vision",  # hunyuan-vision, *-vision
    "glm-4v",  # zhipu GLM-4V / GLM-4.1V / GLM-4.5V / GLM-4.6V
    "glm-4.1v",
    "glm-4.5v",
    "glm-4.6v",
    "kimi-vl",  # moonshot Kimi-VL
]

# Model-name substrings whose models accept native PDF (document) input. Only
# Anthropic Claude models do today via the Messages ``document`` block.
PDF_INPUT_MODELS = ["claude"]

# Model-name substrings whose models support PROVIDER-NATIVE Tool Search
# (server-side ``defer_loading``): a deferred tool's definition rides the wire
# with ``defer_loading:true`` so the API excludes it from the cached prefix until
# discovery, keeping the ``tools=`` prefix byte-stable (prompt cache preserved).
#   - Anthropic: Tool Search is GA on the opus-4 / sonnet-4 / haiku-4.5+ family
#     (NOT the older claude-3-* models).
#   - OpenAI: native tool_search is exposed only on the Responses API for the
#     gpt-5.4+ family (NOT gpt-4* or the gpt-5.0–5.3 models).
# Coarse substring match by design (mirrors MULTI_MODAL_MODELS / PDF_INPUT_MODELS);
# extend the list as new capable models land.
NATIVE_TOOL_SEARCH_MODELS = ["opus-4", "sonnet-4", "haiku-4", "gpt-5.4", "gpt-5.5"]

# Model-name substrings whose models can drive PROVIDER-NATIVE server-side web
# search (the WebSearch tool's secondary call carries a server tool the API
# executes — Anthropic ``web_search_20250305`` / OpenAI Responses ``web_search``
# — and returns structured result blocks). Only the first-party native providers
# support this; a model outside this list makes ``WebSearch`` degrade to a clear
# "server-side search unavailable" notice (steering the model to WebBrowser).
#   - Anthropic: web search is GA on the opus-4 / sonnet-4 / haiku-4.5+ family.
#   - OpenAI: the web_search built-in tool rides the Responses API MUCH more
#     widely than tool_search — per OpenAI docs it is available across the
#     GPT-4o series, the GPT-4.1 series, the o-series reasoning models (o3/o4),
#     and the GPT-5 family. (Do NOT gate it on gpt-5.4+ like tool_search — that
#     was a stale over-narrow assumption; web search predates gpt-5.)
# Coarse substring match by design (mirrors NATIVE_TOOL_SEARCH_MODELS); extend as
# new capable models land.
WEB_SEARCH_MODELS = [
    "opus-4",
    "sonnet-4",
    "haiku-4",
    "gpt-4o",
    "gpt-4.1",
    "gpt-5",
    "o3",
    "o4",
]


def supports_vision(model: Optional[str]) -> bool:
    """Whether ``model`` accepts image input (name-substring match).

    THE single authority for the image-capability check. Both
    ``BaseLLM.support_image_input`` and ``ModelCard.supports_vision`` delegate
    here so the two never drift. Coarse by design: a substring hit against
    ``MULTI_MODAL_MODELS`` (e.g. any ``gpt-*`` or ``*sonnet*``).
    """
    return any(m in (model or "") for m in MULTI_MODAL_MODELS)


def supports_pdf_input(model: Optional[str]) -> bool:
    """Whether ``model`` accepts native PDF (document) input (name-substring match).

    THE single authority for the PDF-capability check (was a bare
    ``"claude" in model`` inline in ``_user_msg_with_media``). Substring match
    against ``PDF_INPUT_MODELS``.
    """
    return any(m in (model or "").lower() for m in PDF_INPUT_MODELS)


def supports_native_tool_search(model: Optional[str]) -> bool:
    """Whether ``model`` supports provider-native Tool Search (``defer_loading``).

    THE single capability authority every tool-search path decision reads:
    the ToolCatalog's native-defer gate, the ``OpenAIResponsesLLM`` transport
    selection, and the deferred-menu suppression all key off this. When True the
    provider takes over tool search on its native wire (Anthropic
    ``tool_reference`` blocks / OpenAI Responses ``tool_search``); when False the
    role falls back to the shared client-side withhold/reveal path. Coarse
    substring match against ``NATIVE_TOOL_SEARCH_MODELS`` (mirrors
    ``supports_vision`` / ``supports_pdf_input``).
    """
    return any(m in (model or "").lower() for m in NATIVE_TOOL_SEARCH_MODELS)


def supports_web_search(model: Optional[str]) -> bool:
    """Whether ``model`` can drive provider-native server-side web search.

    THE single capability authority the ``WebSearch`` tool reads to decide
    whether to attempt the server-side secondary call (Anthropic
    ``web_search_20250305`` / OpenAI Responses ``web_search``) or degrade to a
    "search unavailable, use WebBrowser" notice. Coarse substring match against
    ``WEB_SEARCH_MODELS`` (mirrors ``supports_native_tool_search``).
    """
    return any(m in (model or "").lower() for m in WEB_SEARCH_MODELS)
