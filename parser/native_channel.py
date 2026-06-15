"""NativeToolChannel + factory helpers."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, AsyncGenerator, Optional

from metagpt.common.const import TOOL_CALL_ID, TOOL_CALLS
from metagpt.common.logs import logger
from metagpt.common.schema import AIMessage, CauseBy, UserMessage
from metagpt.common.base.command_channel import CommandChannel, _collect_media, _media_message
from metagpt.common.prompt.output import NATIVE_COMMAND_GUIDE
from metagpt.parser.xml_channel import XmlCommandChannel

if TYPE_CHECKING:
    from metagpt.common.base import BaseThinkEngine
    from metagpt.common.interface import MessageStore


class NativeToolChannel(CommandChannel):
    """Provider-native tool-use: structured tool_calls, no XML format text."""

    def __init__(self, provider: str = "openai") -> None:
        self._provider = provider

    def output_format(self) -> str:
        return ""

    def command_guide(self) -> str:
        return NATIVE_COMMAND_GUIDE

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

    def record_turn(self, memory: "MessageStore", command_rsp: str, executed: list[dict]) -> None:
        tool_calls = [
            {"id": e["id"], "name": e["name"], "args": e.get("args") or {}}
            for e in executed
            if e.get("id")
        ]
        assistant = AIMessage(content=command_rsp or "")
        assistant.metadata[TOOL_CALLS] = tool_calls
        memory.add(assistant)
        for e in executed:
            if not e.get("id"):
                continue
            result = UserMessage(content=e["output"], cause_by=CauseBy.RUN_COMMAND)
            result.metadata[TOOL_CALL_ID] = e["id"]
            memory.add(result)
        media = _media_message(*_collect_media(executed))
        if media is not None:
            memory.add(media)

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
    """Infer the native tool-spec envelope from the LLM model name.

    The envelope must match what the model expects, so we key on the model name
    rather than api_type: in this fork every provider is served by the
    OpenAI-compatible client (openai_api.py -> chat.completions.create), so
    api_type is always "openai" and carries no signal -- but a Claude model
    reached via that client still speaks the Anthropic tool shape. A model name
    containing "claude" -> "anthropic"; everything else (and a missing name) ->
    "openai", the safe default for this fork's transport.
    """
    model = (getattr(llm_config, "model", None) or "").lower()
    if "claude" in model:
        return "anthropic"
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
