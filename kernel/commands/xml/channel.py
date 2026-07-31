"""XmlCommandChannel — text protocol with XML command blocks."""
from __future__ import annotations

import re
from typing import AsyncGenerator, Optional

from mote.contracts.conversation import AIMessage, CauseBy, UserMessage
from mote.contracts.model.inference import InferenceResult
from mote.contracts.model.turn import AgentAction, FinalCandidateAction, ModelTurn, TextAction, ToolCallAction
from mote.contracts.output import OutputRepresentationCapabilities
from mote.kernel.commands.channel import CommandChannel, MediaMaterializer, _media_message, join_command_outputs
from mote.kernel.commands.contracts import ExecutedCommand
from mote.kernel.commands.prompts import XML_COMMAND_GUIDE, XML_TOOL_USAGE_GUIDE
from mote.kernel.commands.symbols import Sym
from mote.kernel.commands.xml.recovery import parse_commands as parse_commands2

_END_MARKER = re.compile(r"<end(?:\s[^>]*)?>.*?</end\s*>", re.IGNORECASE | re.DOTALL)


class XmlCommandChannel(CommandChannel):
    """Text protocol: XML command blocks parsed out of the response text."""

    def __init__(self, media_materializer: MediaMaterializer | None = None) -> None:
        super().__init__(media_materializer)

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
        # plus the static orientation for the tool catalog. Built-in definitions
        # are rendered into the system prompt; hot MCP/pipeline definitions are
        # injected by ToolCatalogContextSource.
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

    def tool_specs(self, catalog, output_contract=None) -> Optional[list[dict]]:
        return None

    async def model_turn(self, result: InferenceResult) -> ModelTurn:
        """Parse XML commands once and normalize them into semantic actions."""
        content = result.content or ""
        actions: list[AgentAction] = [TextAction(content=content)] if content else []
        if not content:
            return ModelTurn(content=content, actions=actions)
        try:
            command_list, error_msg = await parse_commands2(content, None)
        except Exception:  # noqa: BLE001 — parsing is represented as no semantic action
            return ModelTurn(content=content, actions=actions)
        has_end_marker = bool(_END_MARKER.search(content))
        if error_msg and not has_end_marker:
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

    async def iter_commands(self, result: InferenceResult, valid_names: set[str]) -> AsyncGenerator[dict, None]:
        command_rsp = result.content
        if not command_rsp:
            return
        try:
            command_list, error_msg = await parse_commands2(command_rsp, valid_names)
        except Exception:  # noqa: BLE001 — parsing is represented as no command
            return
        if error_msg:
            return
        for cmd in command_list or []:
            yield {"id": None, **cmd, "status": "running", "error_msg": ""}

    def react_result(self, outputs: str) -> str:
        # XML's <end></end>-era contract: ask the orchestrator to mark the task
        # finished. The shared base default (plain outputs) is what native uses.
        return f"I have finished the task, please mark my task as finished. Outputs: {outputs}"

    async def project_call(self, command_rsp: str, executed: list[ExecutedCommand]):
        return self.history_projection([AIMessage(content=command_rsp)])

    async def project_results(self, executed: list[ExecutedCommand]):
        outputs = join_command_outputs(executed)
        messages = [UserMessage(content=outputs, cause_by=CauseBy.RUN_COMMAND)]
        media = _media_message(*(await self._media_materializer(executed)))
        if media is not None:
            messages.append(media)
        return self.history_projection(messages)
