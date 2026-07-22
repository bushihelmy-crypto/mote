"""XmlCommandChannel — text protocol with XML command blocks."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, AsyncGenerator, Optional

from mote.common.base.command_channel import CommandChannel, _collect_media, _media_message, join_command_outputs
from mote.common.logs import logger
from mote.common.prompt.output import XML_COMMAND_GUIDE, XML_TOOL_USAGE_GUIDE
from mote.common.prompt.refs import Sym
from mote.common.schema import (
    AIMessage,
    CauseBy,
    FinalCandidateAction,
    ModelTurn,
    OutputRepresentationCapabilities,
    TextAction,
    ToolCallAction,
    UserMessage,
)
from mote.common.utils.role_utils import parse_commands2

_END_MARKER = re.compile(r"<end(?:\s[^>]*)?>.*?</end\s*>", re.IGNORECASE | re.DOTALL)

if TYPE_CHECKING:
    from mote.common.base import BaseThinkEngine
    from mote.common.interface import MessageStore


class XmlCommandChannel(CommandChannel):
    """Text protocol: XML command blocks parsed out of the response text."""

    def vocabulary(self) -> dict:
        # Surfaces for the XML text protocol: <end></end> as the task terminator,
        # one command block per turn, dotted ClassName.method command names.
        return {
            Sym.CTL_FINISH: "emit <end></end>",
            Sym.CTL_ONE_BLOCK: "output ONE and ONLY ONE command block",
            Sym.CTL_SEPARATE_STEPS: "in separate command blocks",
            Sym.CAP_READ: "Editor.read",
            Sym.CAP_WRITE: "Editor.write",
            Sym.CAP_REPLY: "reply_to_user",
        }

    def prompt_vars(self) -> dict[str, str]:
        # XML supplies the <end></end> / command-tag "# Using commands" guidance,
        # plus the static orientation for the per-turn tool catalog (how the tool
        # categories relate / how to call them). The volatile catalog LIST itself
        # is injected per-turn by ToolCatalogContextSource, not here.
        return {
            "command_guide": XML_COMMAND_GUIDE,
            "tool_usage_guide": XML_TOOL_USAGE_GUIDE,
        }

    def output_capabilities(self) -> OutputRepresentationCapabilities:
        return OutputRepresentationCapabilities(
            supports_text=True,
            supports_prompted_json=True,
            protocol="xml",
        )

    def tool_specs(self, executor, output_contract=None) -> Optional[list[dict]]:
        return None

    async def model_turn(self, think_engine: "BaseThinkEngine") -> ModelTurn:
        """Parse XML commands once and normalize them into semantic actions."""
        if not think_engine.done:
            await think_engine.join()
        content = think_engine.result.content or ""
        actions = [TextAction(content=content)] if content else []
        if not content:
            return ModelTurn(content=content, actions=actions)
        try:
            command_list, error_msg = await parse_commands2(content, None)
        except Exception as exc:  # noqa: BLE001 — parsing is best-effort
            logger.error(f"Error parsing commands: {exc}")
            return ModelTurn(content=content, actions=actions)
        has_end_marker = bool(_END_MARKER.search(content))
        if error_msg and not has_end_marker:
            logger.error(f"Parse commands error: {error_msg}")
            return ModelTurn(content=content, actions=actions)
        ordinary_commands = [cmd for cmd in command_list or [] if str(cmd["command_name"]).lower() != "end"]
        actions.extend(
            ToolCallAction(name=cmd["command_name"], arguments=cmd.get("args") or {}) for cmd in ordinary_commands
        )
        if has_end_marker or len(ordinary_commands) != len(command_list or []):
            actions.append(
                FinalCandidateAction(
                    raw=_END_MARKER.sub("", content).strip(),
                    representation="xml_end",
                )
            )
        return ModelTurn(content=content, actions=actions)

    async def iter_commands(self, think_engine: "BaseThinkEngine", valid_names: set[str]) -> AsyncGenerator[dict, None]:
        if not think_engine.done:
            await think_engine.join()
        command_rsp = think_engine.result.content
        if not command_rsp:
            return
        try:
            command_list, error_msg = await parse_commands2(command_rsp, valid_names)
        except Exception as e:  # noqa: BLE001 — parsing is best-effort
            logger.error(f"Error parsing commands: {e}")
            return
        if error_msg:
            logger.error(f"Parse commands error: {error_msg}")
            return
        for cmd in command_list or []:
            yield {"id": None, **cmd, "status": "running", "error_msg": ""}

    def react_result(self, outputs: str) -> str:
        # XML's <end></end>-era contract: ask the orchestrator to mark the task
        # finished. The shared base default (plain outputs) is what native uses.
        return f"I have finished the task, please mark my task as finished. Outputs: {outputs}"

    async def record_call(self, memory: "MessageStore", command_rsp: str, executed: list[dict]) -> None:
        await memory.add(AIMessage(content=command_rsp))

    async def record_results(self, memory: "MessageStore", executed: list[dict]) -> None:
        outputs = join_command_outputs(executed)
        await memory.add(UserMessage(content=outputs, cause_by=CauseBy.RUN_COMMAND))
        media = _media_message(*_collect_media(executed))
        if media is not None:
            await memory.add(media)
