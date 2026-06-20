"""NativeToolChannel + factory helpers."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, AsyncGenerator, Optional

from metagpt.common.logs import logger
from metagpt.common.schema import AIMessage, ToolMessage
from metagpt.common.base.command_channel import CommandChannel, _collect_media, _media_message
from metagpt.common.prompt.output import NATIVE_COMMAND_GUIDE
from metagpt.common.prompt.refs import Sym
from metagpt.parser.xml_channel import XmlCommandChannel

if TYPE_CHECKING:
    from metagpt.common.base import BaseThinkEngine
    from metagpt.common.interface import MessageStore


class NativeToolChannel(CommandChannel):
    """Provider-native tool-use: structured tool_calls, no XML format text."""

    def __init__(self, provider: str = "openai") -> None:
        self._provider = provider

    def vocabulary(self) -> dict:
        # Surfaces for the provider-native tool-use protocol: a turn ends by
        # replying with plain text and no tool call (NO <end></end>); tools are
        # structured API calls, so "command block" mechanics become tool-call
        # mechanics and capability names become plain English.
        return {
            Sym.CTL_FINISH: "stop calling tools and reply with a plain text message",
            Sym.CTL_ONE_BLOCK: "make your tool calls for this turn",
            Sym.CTL_SEPARATE_STEPS: "in separate tool calls",
            Sym.CAP_READ: "the read tool",
            Sym.CAP_WRITE: "the write tool",
            Sym.CAP_REPLY: "a plain text reply",
        }

    def prompt_vars(self) -> dict[str, str]:
        # Native supplies only the tool-call "# Using commands" guidance; output is
        # API-constrained (no OUTPUT section) and there is no per-turn hint —
        # crucially neither carries the <end></end> marker the model would echo.
        return {
            "output_format": "",
            "command_guide": NATIVE_COMMAND_GUIDE,
            "command_hint": "",
        }

    def tool_specs(self, executor) -> Optional[list[dict]]:
        return executor.get_native_tool_specs(provider=self._provider)

    async def iter_commands(
        self, think_engine: "BaseThinkEngine", valid_names: set[str]
    ) -> AsyncGenerator[dict, None]:
        if not think_engine.done:
            await think_engine.join()
        for cmd in think_engine.result.tool_calls or []:
            name = cmd["command_name"]
            if valid_names and name not in valid_names:
                logger.warning(f"Skipping unknown command: {name}")
                continue
            yield {
                "id": cmd.get("id"),
                "command_name": name,
                "args": cmd.get("args") or {},
                "status": "running",
                "error_msg": "",
            }

    async def record_turn(self, memory: "MessageStore", command_rsp: str, executed: list[dict]) -> None:
        tool_calls = [
            {"id": e["id"], "name": e["name"], "args": e.get("args") or {}}
            for e in executed
            if e.get("id")
        ]
        await memory.add(AIMessage(content=command_rsp or "", tool_calls=tool_calls))
        for e in executed:
            if not e.get("id"):
                continue
            await memory.add(ToolMessage(content=e["output"], tool_call_id=e["id"]))
        media = _media_message(*_collect_media(executed))
        if media is not None:
            await memory.add(media)

    def turn_signature(self, think_engine: "BaseThinkEngine") -> str:
        calls = [
            {"name": c["command_name"], "args": c.get("args") or {}}
            for c in (think_engine.result.tool_calls or [])
        ]
        return json.dumps(calls, sort_keys=True, ensure_ascii=False)

    async def is_terminal(self, think_engine: "BaseThinkEngine") -> bool:
        # Join before reading so we observe *this* round's result. The loop calls
        # is_terminal right after launching the think task; without the join we
        # would read the previous round's completed result and lag one round
        # (issuing a wasted extra think and double-recording the final turn).
        if not think_engine.done:
            await think_engine.join()
        return think_engine.result.tool_calls == []


def infer_native_tool_provider(llm_config) -> str:
    """Infer the native tool-spec envelope from the resolved transport.

    The envelope must match the WIRE PROTOCOL of the endpoint that issues the
    request — not the underlying model. The OpenAI-compatible client
    (openai_api.py -> chat.completions.create) requires OpenAI-shaped tools
    ({"type":"function","function":{...}}); only the native Anthropic Messages
    client (anthropic_api.py) takes the Anthropic shape
    ({"name","description","input_schema"}).

    So we key on ``resolve_api_type`` (the same logic that selects the client):
    ANTHROPIC transport (api_type=anthropic or an anthropic.com base_url) ->
    "anthropic"; everything else -> "openai". Keying on the model name is wrong:
    a Claude model reached via an OpenAI-compatible gateway still POSTs an
    OpenAI-shaped body that the gateway translates server-side, so emitting the
    Anthropic shape there yields a ``tools`` field the gateway silently drops —
    the model then receives no tools and falls back to inventing text commands.
    """
    from metagpt.common.config.config.llm_config import LLMType
    from metagpt.router.llm.llm_provider_registry import resolve_api_type

    try:
        if resolve_api_type(llm_config) == LLMType.ANTHROPIC:
            return "anthropic"
    except Exception:
        pass
    return "openai"


def make_command_channel(protocol: str, *, provider: str = "openai") -> CommandChannel:
    """Build the channel for a RoleSchema.command_protocol value.

    "xml" -> XmlCommandChannel; "native" -> NativeToolChannel. Unknown values
    fall back to XML (the safe, model-agnostic default). ``provider`` is the
    native tool-spec envelope; pass the value from infer_native_tool_provider().
    """
    if protocol == "native":
        return NativeToolChannel(provider=provider)
    return XmlCommandChannel()
