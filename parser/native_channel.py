"""NativeToolChannel + factory helpers."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, AsyncGenerator, Callable, Optional, TypeAlias

from mote.common.base.command_channel import CommandChannel, _collect_media, _media_message
from mote.common.config.config.llm_config import LLMType
from mote.common.const.llm import supports_native_tool_search
from mote.common.logs import logger
from mote.common.prompt.refs import Sym
from mote.common.schema import AIMessage, ToolMessage
from mote.parser.xml_channel import XmlCommandChannel
from mote.router.llm.llm_provider_registry import resolve_api_type

if TYPE_CHECKING:
    from mote.common.base import BaseThinkEngine
    from mote.common.interface import MessageStore

# Recorded-args size limiter: (tool_name, args, call_id) -> possibly-compressed
# args. ``tool_name`` lets a wrapper specialize per tool (e.g. an Edit whole-file
# write → structural AST summary) before falling back to the tool-agnostic
# large-blob persist (``ToolExecutor.persist_large_args``).
ArgsLimiter: TypeAlias = Callable[[str, Any, str | None], Any]


class NativeToolChannel(CommandChannel):
    """Provider-native tool-use: structured tool_calls, no XML format text."""

    def __init__(
        self,
        provider: str = "openai",
        model: str | None = None,
        args_limiter: ArgsLimiter | None = None,
    ) -> None:
        self._provider = provider
        # The resolved model name — threaded into get_native_tool_specs so the
        # catalog's server-side tool-search (defer_loading) decision is
        # capability-gated (supports_native_tool_search), not provider-only.
        self._model = model
        # Optional recorded-args size limiter (executor.persist_large_args). When
        # set, a giant tool-call ``args`` blob is persisted to disk and replaced
        # by a ``<persisted-output>`` envelope BEFORE the assistant message enters
        # memory — the arguments twin of the result-output cap, so an oversized
        # arg never lives in history uncompressed nor lands in a cached prefix.
        # None (tests / no executor) → args recorded verbatim.
        self._args_limiter = args_limiter

    @property
    def _server_side_tool_search(self) -> bool:
        """True when this transport does provider-native (server-side) tool search.

        Only then may a SearchTools discovery be rendered as ``tool_reference`` /
        ``tool_search`` blocks the API expands (the ``tool_references`` stamp on
        the recorded ToolMessage). An INCAPABLE native model runs the client-side
        SPLIT path instead: it CANNOT expand those blocks, so the stamp must be
        suppressed there (the discovery reveals via RoleState + the reminder-tail
        description menu, not via a wire reference block). Keyed on the exact same
        capability + provider gate the catalog's ``native_specs`` uses, so the
        record side never drifts from the wire projection.
        """
        return supports_native_tool_search(self._model) and self._provider in (
            "anthropic",
            "openai_responses",
        )

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
        # Both empty on native: a native model reaches its tools via the API
        # ``tools=`` param and ends a turn simply by making no tool call, so the
        # system prompt needs neither the "# Using commands" mechanics
        # (command_guide) nor catalog orientation (tool_usage_guide) — those are
        # XML-protocol concerns. (mirrors wants_tool_catalog() False.)
        return {
            "command_guide": "",
            "tool_usage_guide": "",
        }

    def wants_tool_catalog(self) -> bool:
        # Native tools already reach the model via the API ``tools=`` param
        # (tool_specs below), so the system prompt must NOT re-describe them.
        return False

    def tool_specs(self, executor) -> Optional[list[dict]]:
        return executor.get_native_tool_specs(provider=self._provider, model=self._model)

    async def iter_commands(self, think_engine: "BaseThinkEngine", valid_names: set[str]) -> AsyncGenerator[dict, None]:
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

    async def record_call(self, memory: "MessageStore", command_rsp: str, executed: list[dict]) -> None:
        # Compress each call's RECORDED args through the injected limiter (persist
        # a giant arg to disk + leave a <persisted-output> pointer) as the message
        # is built. This reads e["args"] into a NEW list, so the loop's execution
        # args (entry["args"], passed to run_command) stay untouched — recording
        # and execution never share the mutated value. Both the checkpoint path
        # (record_call before execution) and the single-shot record_turn path
        # funnel through here, so args are limited on exactly one seam.
        tool_calls = [
            {"id": e["id"], "name": e["name"], "args": self._limit_args(e["name"], e.get("args") or {}, e["id"])}
            for e in executed
            if e.get("id")
        ]
        await memory.add(AIMessage(content=command_rsp or "", tool_calls=tool_calls))

    def _limit_args(self, tool_name: str, args: Any, call_id: str | None) -> Any:
        """Run recorded args through the size limiter when one is wired.

        ``tool_name`` lets the limiter specialize per tool (an Edit whole-file
        write can fold into a structural summary) before the tool-agnostic
        large-blob persist fallback.
        """
        return self._args_limiter(tool_name, args, call_id) if self._args_limiter is not None else args

    async def record_results(self, memory: "MessageStore", executed: list[dict]) -> None:
        for e in executed:
            if not e.get("id"):
                continue
            # Server-side tool-search (capable native model): a SearchTools
            # result carries {tool_references: [...]} in its data → the discovered
            # names. Stamped onto the ToolMessage so the native wire renders the
            # tool_result as tool_reference / tool_search blocks the API expands.
            # Gated on the transport's ACTUAL server-side capability — an
            # incapable native model runs the client-side SPLIT path and cannot
            # expand those blocks, so the stamp is suppressed there (discovery
            # reveals via RoleState + the reminder-tail description menu instead).
            # Any other tool's data is ignored here (only this key is read).
            data = e.get("data")
            tool_references = (
                data.get("tool_references") if self._server_side_tool_search and isinstance(data, dict) else None
            )
            await memory.add(
                ToolMessage(
                    content=e["output"],
                    tool_call_id=e["id"],
                    retention=e.get("retention"),
                    resource_path=e.get("resource_path"),
                    tool_references=tool_references,
                )
            )
        media = _media_message(*_collect_media(executed))
        if media is not None:
            await memory.add(media)

    def turn_signature(self, think_engine: "BaseThinkEngine") -> str:
        calls = [
            {"name": c["command_name"], "args": c.get("args") or {}} for c in (think_engine.result.tool_calls or [])
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
    "anthropic"; the OpenAI Responses transport (a genuine OpenAI endpoint on a
    native-tool-search-capable gpt-5.4+ model) -> "openai_responses" (the flat
    Responses function envelope); everything else -> "openai". Keying on the
    model name alone is wrong: a Claude model reached via an OpenAI-compatible
    gateway still POSTs an OpenAI-shaped body that the gateway translates
    server-side, so emitting the Anthropic shape there yields a ``tools`` field
    the gateway silently drops — the model then receives no tools and falls back
    to inventing text commands. The Responses envelope is likewise keyed off
    ``resolve_api_type`` (which excludes gateways by host), not the raw model.
    """

    try:
        resolved = resolve_api_type(llm_config)
    except Exception:
        return "openai"
    if resolved == LLMType.ANTHROPIC:
        return "anthropic"
    if resolved == LLMType.OPENAI_RESPONSES:
        return "openai_responses"
    return "openai"


def make_command_channel(
    protocol: str,
    *,
    provider: str = "openai",
    model: str | None = None,
    args_limiter: ArgsLimiter | None = None,
) -> CommandChannel:
    """Build the channel for a RoleSchema.command_protocol value.

    "xml" -> XmlCommandChannel; "native" -> NativeToolChannel. Unknown values
    fall back to XML (the safe, model-agnostic default). ``provider`` is the
    native tool-spec envelope; pass the value from infer_native_tool_provider().
    ``model`` is the resolved model name, threaded into the tool-spec build so
    the server-side tool-search decision is capability-gated. ``args_limiter``
    (native only) is the recorded-args size limiter — pass
    ``executor.persist_large_args`` so a giant tool-call arg is persisted before
    it enters context. XML embeds args in the assistant text (no structured
    args list to limit), so it ignores it.
    """
    if protocol == "native":
        return NativeToolChannel(provider=provider, model=model, args_limiter=args_limiter)
    return XmlCommandChannel()
