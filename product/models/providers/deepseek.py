# -*- coding: utf-8 -*-
"""DeepSeek provider — OpenAI-compatible transport + DSML tool-call salvage.

DeepSeek models are reached through an OpenAI-compatible gateway, so they use the
same wire transport as :class:`OpenAILLM` (``chat.completions.create``, streaming,
cost accounting, error classification). This subclass changes ONLY the tool-call
read side.

Why: DeepSeek occasionally "falls out" of the structured tool-call channel and
emits its internal DSML tool-call markup as plain assistant ``content`` instead
of the gateway-translated ``tool_calls`` field. The gateway only translates the
structured channel, so the leaked block reaches us as text — never executed,
polluting history and spinning the loop. We salvage it: when the standard
``tool_calls`` come back empty BUT ``content`` carries a DSML block, parse the
block back into structured calls and strip it from the visible text. The salvage
runs only on the empty-``tool_calls`` path, so a well-formed response is never
touched.
"""
from __future__ import annotations

from mote.product.models.providers.openai_chat import OpenAILLM
from mote.runtime.models.clients.dsml import contains_dsml, parse_dsml_tool_calls
from mote.runtime.presentation import plural
from mote.runtime.telemetry.logging import logger


class DeepSeekLLM(OpenAILLM):
    """OpenAI-compatible DeepSeek provider that recovers leaked DSML tool calls."""

    def get_choice_tool_calls(self, rsp) -> list[dict]:
        """Return structured tool calls, salvaging leaked DSML when none arrive.

        First tries the standard OpenAI ``tool_calls`` path (the common case).
        Only when that yields nothing AND the text content holds a DSML block do
        we parse the block, minting an ``id`` per call (DSML carries none) so the
        downstream tool/result pairing in NativeToolChannel still works.
        """
        calls = super().get_choice_tool_calls(rsp)
        if calls:
            return calls
        content = super().get_choice_text(rsp) or ""
        if not contains_dsml(content):
            return calls
        salvaged, _ = parse_dsml_tool_calls(content)
        if not salvaged:
            return calls
        logger.warning(
            f"DeepSeek leaked {len(salvaged)} tool {plural('call', len(salvaged))} as DSML text; salvaged from content."
        )
        for i, call in enumerate(salvaged):
            if not call.get("id"):
                call["id"] = f"dsml_{i}"
        return salvaged

    def get_choice_text(self, rsp) -> str:
        """Return assistant text, stripping any DSML block we salvaged as calls.

        Mirrors :meth:`get_choice_tool_calls`: when the standard ``tool_calls``
        are empty and the content holds DSML, return the text with the DSML block
        removed so the leaked markup never reaches history or the console. The
        common (no-leak) path returns the content unchanged.
        """
        content = super().get_choice_text(rsp) or ""
        if super().get_choice_tool_calls(rsp):
            return content
        if not contains_dsml(content):
            return content
        salvaged, remaining = parse_dsml_tool_calls(content)
        return remaining if salvaged else content


__all__ = ["DeepSeekLLM"]
