"""XmlCommandChannel — legacy text protocol with XML command blocks."""
from __future__ import annotations

from typing import TYPE_CHECKING, AsyncGenerator, Optional

from metagpt.common.logs import logger
from metagpt.common.schema import AIMessage, CauseBy, UserMessage
from metagpt.common.utils.role_zero_utils import parse_commands2
from metagpt.common.base.command_channel import CommandChannel, _collect_media, _media_message
from metagpt.parser.prompts import OUTPUT_SECTION

if TYPE_CHECKING:
    from metagpt.common.base import BaseThinkEngine
    from metagpt.common.interface import MessageStore


class XmlCommandChannel(CommandChannel):
    """Legacy text protocol: XML command blocks parsed out of the response text."""

    def output_format(self) -> str:
        return OUTPUT_SECTION

    def tool_specs(self, executor) -> Optional[list[dict]]:
        return None

    async def iter_commands(
        self, think_engine: "BaseThinkEngine", valid_names: set[str]
    ) -> AsyncGenerator[dict, None]:
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

    def record_turn(self, memory: "MessageStore", command_rsp: str, executed: list[dict]) -> None:
        outputs = "\n\n".join(e["output"] for e in executed) if executed else (
            "No valid commands found for execution, pay attention to the output format."
        )
        memory.add(AIMessage(content=command_rsp))
        memory.add(UserMessage(content=outputs, cause_by=CauseBy.RUN_COMMAND))
        media = _media_message(*_collect_media(executed))
        if media is not None:
            memory.add(media)
