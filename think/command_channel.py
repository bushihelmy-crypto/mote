"""CommandChannel — the protocol seam between the react loop and the wire format.

A Role decides *what* to do; a CommandChannel decides *how* that intent is
expressed to (and parsed back from) the LLM. Two protocols are supported behind
one interface so the react loop, memory, ToolExecutor and ThinkEngine never
branch on protocol:

- XmlCommandChannel: the legacy text protocol. The model emits ``<Tool.method>``
  command blocks; we inject OUTPUT_SECTION to teach that format and parse the
  text back with loads_xml.
- NativeToolChannel: the provider's native tool-use. We pass JSON-Schema tool
  specs (Layer 1) to the API, the model returns structured tool_calls, and we
  read them straight off ThinkEngine.result.tool_calls.

Each channel answers three questions:
  1. how to PROMPT  -> output_format(): the OUTPUT_SECTION text, or "" when the
     API already constrains output (native).
  2. how to CALL    -> tool_specs(executor): native specs to hand the LLM, or
     None for the text channel.
  3. how to PARSE   -> iter_commands(think_engine, valid_names): yield the
     unified IR {command_name, args, status, error_msg} both modes share.

The unified IR is the contract: whatever the channel, _act consumes the same
dicts, so adding a future protocol means adding a channel, not touching Role.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import AsyncGenerator, Optional

from const import IMAGES, PDFS, TOOL_CALL_ID, TOOL_CALLS
from logs import logger
from metagpt.think.role_zero import OUTPUT_SECTION
from schema import AIMessage, CauseBy, UserMessage
from utils.role_zero_utils import parse_commands2


class CommandChannel(ABC):
    """Protocol-specific prompt/call/parse strategy for the react loop."""

    @abstractmethod
    def output_format(self) -> str:
        """System-prompt OUTPUT section text for this protocol ("" if none)."""

    @abstractmethod
    def tool_specs(self, executor) -> Optional[list[dict]]:
        """Native tool specs to pass to the LLM, or None for the text channel."""

    @abstractmethod
    async def iter_commands(
        self, think_engine, valid_names: set[str]
    ) -> AsyncGenerator[dict, None]:
        """Yield unified-IR commands from a completed ThinkEngine output.

        Each item: ``{command_name, args, id, status, error_msg}``. ``id`` is the
        provider tool-call id for native mode (used to pair tool results), or
        None for XML. Unknown command names (not in valid_names) are filtered
        out. Both channels block on the think task being done before reading.
        """
        raise NotImplementedError
        yield  # pragma: no cover — makes this an async generator for typing

    @abstractmethod
    def record_turn(self, memory, command_rsp: str, executed: list[dict]) -> None:
        """Record one think→act round into memory in this protocol's shape.

        Args:
            memory: the Role's memory (has .add()).
            command_rsp: the assistant's text for this turn.
            executed: list of ``{id, name, output, success}`` for the commands
                that ran this turn (in order).

        XML records a single assistant text + one merged user message of outputs.
        Native records an assistant message carrying tool_calls + one tool-result
        message per executed call (paired by id), as the API requires.
        """

    def turn_signature(self, think_engine) -> str:
        """A stable string identifying this turn, for duplicate detection.

        XML uses the raw response text; native uses the structured calls (text
        may be empty or repeat while the actual calls differ). Default returns
        the response text — overridden by channels that have a better signal.
        """
        return think_engine.result.content or ""

    def is_terminal(self, think_engine) -> bool:
        """Whether the react loop should stop after this think round.

        Each protocol signals "done" differently:
          - XML: the model emits an ``End`` command, which deactivates the Role;
            the loop already stops when the next ``_think`` returns False, so the
            channel itself never reports a terminal turn (default False).
          - native: the model finishes by replying with plain text and no
            tool_calls — see NativeToolChannel.
        """
        return False


def _collect_media(executed: list[dict]) -> tuple[list[str], list[str]]:
    """Gather base64 images / PDFs from this turn's executed commands.

    Returns (images, pdfs). Tools that read media (e.g. Read on an image or
    PDF) put a textual placeholder in their tool_result output and the actual
    base64 bytes here, so the model receives them as a separate multimodal
    message rather than stuffed into a tool_result string.
    """
    images: list[str] = []
    pdfs: list[str] = []
    for e in executed:
        images.extend(e.get("images") or [])
        pdfs.extend(e.get("pdfs") or [])
    return images, pdfs


def _media_message(images: list[str], pdfs: list[str]):
    """Build the supplemental user message carrying media, or None if empty.

    Media rides in metadata[IMAGES]/[PDFS]; the LLM client's format_msg renders
    those into multimodal content blocks. A short text body anchors the message
    so providers that require non-empty content stay happy.
    """
    if not images and not pdfs:
        return None
    msg = UserMessage(
        content="Attached media from the tool result(s) above.",
        cause_by=CauseBy.RUN_COMMAND,
    )
    if images:
        msg.metadata[IMAGES] = images
    if pdfs:
        msg.metadata[PDFS] = pdfs
    return msg


class XmlCommandChannel(CommandChannel):
    """Legacy text protocol: XML command blocks parsed out of the response text."""

    def output_format(self) -> str:
        # The text protocol must teach the model the <Tool.method> block format.
        return OUTPUT_SECTION

    def tool_specs(self, executor) -> Optional[list[dict]]:
        # Text channel: no native tools; the model writes command blocks instead.
        return None

    async def iter_commands(
        self, think_engine, valid_names: set[str]
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

    def record_turn(self, memory, command_rsp: str, executed: list[dict]) -> None:
        # Historical XML shape: one assistant text + one merged user message of
        # all command outputs. Byte-identical to the pre-channel _act behavior.
        outputs = "\n\n".join(e["output"] for e in executed) if executed else (
            "No valid commands found for execution, pay attention to the output format."
        )
        memory.add(AIMessage(content=command_rsp))
        memory.add(UserMessage(content=outputs, cause_by=CauseBy.RUN_COMMAND))
        media = _media_message(*_collect_media(executed))
        if media is not None:
            memory.add(media)


class NativeToolChannel(CommandChannel):
    """Provider-native tool-use: structured tool_calls, no XML format text."""

    def __init__(self, provider: str = "openai") -> None:
        # Which native envelope to emit (OpenAI / Anthropic). Defaults to the
        # OpenAI shape since every model in this fork is served by the
        # OpenAI-compatible client; ToolExecutor.get_native_tool_specs handles both.
        self._provider = provider

    def output_format(self) -> str:
        # Native tool-use constrains output via the API's tool schema, so no
        # XML format instructions are needed (and would only confuse the model).
        return ""

    def tool_specs(self, executor) -> Optional[list[dict]]:
        return executor.get_native_tool_specs(provider=self._provider)

    async def iter_commands(
        self, think_engine, valid_names: set[str]
    ) -> AsyncGenerator[dict, None]:
        if not think_engine.done:
            await think_engine.join()
        # result.tool_calls is the structured IR produced by ThinkEngine's native
        # branch (None would mean the think ran in text mode — a wiring bug here).
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

    def record_turn(self, memory, command_rsp: str, executed: list[dict]) -> None:
        # Native protocol: the assistant turn carries the structured tool_calls in
        # metadata (Message.to_dict renders them into the OpenAI tool_calls array),
        # then one tool-result message per executed call, paired by id. This is the
        # shape the API requires: every tool_call must have a matching tool result.
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
        # Media goes in a separate user message AFTER every tool-result: the API
        # requires each tool_call's result to immediately follow the assistant
        # turn, so base64 image/PDF blocks can't live inside a tool_result.
        media = _media_message(*_collect_media(executed))
        if media is not None:
            memory.add(media)

    def turn_signature(self, think_engine) -> str:
        # Native text may be empty or repeat while the actual calls differ, so the
        # structured calls (name + args) are the real duplicate-detection signal.
        calls = [
            {"name": c["command_name"], "args": c.get("args") or {}}
            for c in (think_engine.result.tool_calls or [])
        ]
        return json.dumps(calls, sort_keys=True, ensure_ascii=False)

    def is_terminal(self, think_engine) -> bool:
        # Native tool-use ends the way the API itself signals completion: the
        # model stops requesting tools and returns a plain text reply. An empty
        # tool_calls list (not None — None would mean the round ran in XML mode)
        # is that terminal turn, so the react loop should stop after it.
        return think_engine.result.tool_calls == []


def infer_native_tool_provider(llm_config) -> str:
    """Infer the native tool-spec envelope from the LLM model name.

    The envelope must match what the model expects, so we key on the model name
    rather than api_type: in this fork every provider is served by the
    OpenAI-compatible client (openai_api.py → chat.completions.create), so
    api_type is always "openai" and carries no signal — but a Claude model
    reached via that client still speaks the Anthropic tool shape. A model name
    containing "claude" → "anthropic"; everything else (and a missing name) →
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
