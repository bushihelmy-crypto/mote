#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Provider-neutral model capability checks.

The four ``supports_*`` capability checks are now THIN DELEGATES to the single
declarative :mod:`mote.contracts.model.profile` registry — the former scattered
substring lists (``MULTI_MODAL_MODELS`` / ``PDF_INPUT_MODELS`` /
``NATIVE_TOOL_SEARCH_MODELS`` / ``WEB_SEARCH_MODELS``) were consolidated into that
one mergeable, model-name-keyed profile table. The public API here is preserved
byte-for-byte so every existing caller stays unchanged; the profile registry is
now the ONE place to edit when a new capable model lands.
"""

from typing import Optional

from mote.contracts.model.profile import profile_for


def supports_vision(model: Optional[str]) -> bool:
    """Whether ``model`` accepts image input (name-substring match).

    Delegates to the provider-neutral profile registry.
    """
    return profile_for(model).supports_vision


def supports_pdf_input(model: Optional[str]) -> bool:
    """Whether ``model`` accepts native PDF (document) input (name-substring match).

    THE single authority for the PDF-capability check (was a bare
    ``"claude" in model`` inline in ``_user_msg_with_media``). Delegates to the
    profile registry.
    """
    return profile_for(model).supports_pdf_input


def supports_native_tool_search(model: Optional[str]) -> bool:
    """Whether ``model`` supports provider-native Tool Search (``defer_loading``).

    THE single capability authority every tool-search path decision reads:
    the ToolCatalog's native-defer gate, the ``OpenAIResponsesLLM`` transport
    selection, and the deferred-menu suppression all key off this. When True the
    provider takes over tool search on its native wire (Anthropic
    ``tool_reference`` blocks / OpenAI Responses ``tool_search``); when False the
    role falls back to the shared client-side withhold/reveal path. Delegates to
    the profile registry.
    """
    return profile_for(model).supports_native_tool_search


def supports_web_search(model: Optional[str]) -> bool:
    """Whether ``model`` can drive provider-native server-side web search.

    THE single capability authority the ``WebSearch`` tool reads to decide
    whether to attempt the server-side secondary call (Anthropic
    ``web_search_20250305`` / OpenAI Responses ``web_search``) or degrade to a
    "search unavailable, use WebBrowser" notice. Delegates to the profile
    registry.
    """
    return profile_for(model).supports_web_search
