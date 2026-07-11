#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LLM-related constants — function schemas, model lists.

Moved from mote/common/llm/constant.py.
"""

from typing import Optional

# function in tools, https://platform.openai.com/docs/api-reference/chat/create#chat-create-tools
# Reference: https://github.com/KillianLucas/open-interpreter/blob/v0.1.14/interpreter/llm/setup_openai_coding_llm.py
GENERAL_FUNCTION_SCHEMA = {
    "name": "execute",
    "description": "Executes code on the user's machine, **in the users local environment**, and returns the output",
    "parameters": {
        "type": "object",
        "properties": {
            "language": {
                "type": "string",
                "description": "The programming language (required parameter to the `execute` function)",
                "enum": [
                    "python",
                    "R",
                    "shell",
                    "applescript",
                    "javascript",
                    "html",
                    "powershell",
                ],
            },
            "code": {"type": "string", "description": "The code to execute (required)"},
        },
        "required": ["language", "code"],
    },
}


# tool_choice value for general_function_schema
# https://platform.openai.com/docs/api-reference/chat/create#chat-create-tool_choice
GENERAL_TOOL_CHOICE = {"type": "function", "function": {"name": "execute"}}


MULTI_MODAL_MODELS = ["gpt-4o", "gpt-4o-mini", "sonnet", "opus", "gemini", "gpt"]

# Model-name substrings whose models accept native PDF (document) input. Only
# Anthropic Claude models do today via the Messages ``document`` block.
PDF_INPUT_MODELS = ["claude"]


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
