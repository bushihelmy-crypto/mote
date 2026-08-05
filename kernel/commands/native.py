"""NativeToolChannel + factory helpers."""

from __future__ import annotations

import json
from typing import Any, Callable, Optional, TypeAlias, cast

from mote.contracts.conversation import AIMessage, ToolMessage
from mote.contracts.events.envelope import JsonValue, thaw_json
from mote.contracts.model.failover import EndpointDescriptor
from mote.contracts.model.inference import InferenceResult
from mote.contracts.model.turn import FinalCandidateAction, ModelTurn, TextAction, ToolCallAction
from mote.contracts.output import OutputBindingKind, OutputRepresentationCapabilities
from mote.kernel.commands.channel import CommandChannel, MediaMaterializer, _media_message
from mote.kernel.commands.contracts import ExecutedCommand, ToolProjectionContext
from mote.kernel.commands.symbols import Sym
from mote.kernel.commands.tool_projection import project_tools
from mote.kernel.commands.xml.channel import XmlCommandChannel
from mote.kernel.output.binding import FINAL_OUTPUT_TOOL_NAME

# Recorded-args size limiter: (tool_name, args, call_id) -> possibly-compressed
# args. ``tool_name`` lets a wrapper specialize per tool (e.g. an Edit whole-file
# write → structural AST summary) before falling back to the tool-agnostic
# large-blob persist (``ToolExecutor.persist_large_args``).
ArgsLimiter: TypeAlias = Callable[[str, Any, str | None], Any]


class NativeToolChannel(CommandChannel):
    """Provider-native tool-use: structured tool_calls, no XML format text."""

    def __init__(
        self,
        args_limiter: ArgsLimiter | None = None,
        output_is_text: bool = True,
        supports_native_schema: bool = False,
        supports_native_tool_search: bool = False,
        model: str | None = None,
        media_materializer: MediaMaterializer | None = None,
    ) -> None:
        super().__init__(media_materializer)
        self._model = model
        # Optional recorded-args size limiter (executor.persist_large_args). When
        # set, a giant tool-call ``args`` blob is persisted to disk and replaced
        # by a ``<persisted-output>`` envelope BEFORE the assistant message enters
        # memory — the arguments twin of the result-output cap, so an oversized
        # arg never lives in history uncompressed nor lands in a cached prefix.
        # None (tests / no executor) → args recorded verbatim.
        self._args_limiter = args_limiter
        self._output_is_text = output_is_text
        self._supports_native_schema = supports_native_schema
        self._supports_native_tool_search = supports_native_tool_search

    @property
    def _server_side_tool_search(self) -> bool:
        """True when this transport does provider-native (server-side) tool search.

        Only then may a SearchTools discovery be rendered as ``tool_reference`` /
        ``tool_search`` blocks the API expands (the ``tool_references`` stamp on
        the recorded ToolMessage). An INCAPABLE native model runs the client-side
        client-side path instead: it CANNOT expand those blocks, so the stamp
        must be suppressed there (the discovery reveals via RoleState; the next
        turn then adds the full schema to ``tools=``). Keyed on the exact same
        capability + provider gate the schema projection uses, so the record
        side never drifts from the wire projection.
        """
        return self._supports_native_tool_search

    def for_model(
        self,
        endpoint: EndpointDescriptor,
        *,
        output_schema=None,
    ) -> "NativeToolChannel":
        return NativeToolChannel(
            args_limiter=self._args_limiter,
            output_is_text=self._output_is_text,
            supports_native_schema=(endpoint.capabilities.supports_native_schema and output_schema is not None),
            supports_native_tool_search=(endpoint.capabilities.supports_native_tool_search),
            model=endpoint.model,
            media_materializer=self._media_materializer,
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
        # Native built-in/pipeline specs already reach the model via ``tools=``.
        # The turn-context source still announces hot-reloadable MCP definitions.
        return False

    def output_capabilities(self) -> OutputRepresentationCapabilities:
        return OutputRepresentationCapabilities(
            supports_text=True,
            supports_native_schema=self._supports_native_schema,
            supports_semantic_tool=True,
            supports_prompted_json=True,
            protocol="native",
            provider="canonical",
            model=self._model or "",
        )

    def tool_specs(self, catalog, output_contract=None) -> Optional[list[dict]]:
        projection = project_tools(
            catalog,
            ToolProjectionContext("native", "1", catalog.fingerprint),
        )
        specs = list(projection.definitions)
        if output_contract is None:
            return specs
        decision = self.output_binding_decision(is_text=output_contract.is_text)
        if decision.binding.kind is not OutputBindingKind.NATIVE_TOOL:
            return specs
        final_schema = {
            FINAL_OUTPUT_TOOL_NAME: {
                "name": FINAL_OUTPUT_TOOL_NAME,
                "description": (
                    "Submit the final answer. Call this exactly once and do not " "combine it with any other tool call."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"output": output_contract.decoder.schema.canonical},
                    "required": ["output"],
                    "additionalProperties": False,
                },
            }
        }
        return specs + list(final_schema.values())

    async def project_call(self, command_rsp: str, executed: list[ExecutedCommand]):
        # Compress each call's RECORDED args through the injected limiter (persist
        # a giant arg to disk + leave a <persisted-output> pointer) as the message
        # is built. This reads e["args"] into a NEW list, so the loop's execution
        # args (entry["args"], passed to run_command) stay untouched — recording
        # and execution never share the mutated value. Both the checkpoint path
        # (record_call before execution) and the single-shot record_turn path
        # funnel through here, so args are limited on exactly one seam.
        tool_calls = [
            {
                "id": entry.action_id,
                "name": entry.name,
                "args": self._limit_args(entry.name, entry.arguments, entry.action_id),
            }
            for entry in executed
            if entry.action_id
        ]
        return self.history_projection([AIMessage(content=command_rsp or "", tool_calls=tool_calls)])

    def _limit_args(self, tool_name: str, args: Any, call_id: str | None) -> Any:
        """Run recorded args through the size limiter when one is wired.

        ``tool_name`` lets the limiter specialize per tool (an Edit whole-file
        write can fold into a structural summary) before the tool-agnostic
        large-blob persist fallback.
        """
        return self._args_limiter(tool_name, args, call_id) if self._args_limiter is not None else args

    async def project_results(self, executed: list[ExecutedCommand]):
        messages = []
        for entry in executed:
            if not entry.action_id:
                continue
            # Server-side tool-search (capable native model): a SearchTools
            # result carries {tool_references: [...]} in its data → the discovered
            # names. Stamped onto the ToolMessage so the native wire renders the
            # tool_result as tool_reference / tool_search blocks the API expands.
            # Gated on the transport's ACTUAL server-side capability — an
            # incapable native model runs the client-side path and cannot expand
            # those blocks, so the stamp is suppressed there (RoleState reveal
            # makes the complete schema appear in ``tools=`` next turn instead).
            # Any other tool's data is ignored here (only this key is read).
            data = entry.data
            tool_references = (
                data.get("tool_references") if self._server_side_tool_search and isinstance(data, dict) else None
            )
            messages.append(
                ToolMessage(
                    content=entry.output,
                    tool_call_id=entry.action_id,
                    retention=entry.retention,
                    resource_path=entry.resource_path,
                    tool_references=tool_references,
                )
            )
        media = _media_message(*(await self._media_materializer(executed)))
        if media is not None:
            messages.append(media)
        return self.history_projection(messages)

    async def project_output_candidate(
        self,
        content: str,
        candidate,
        *,
        accepted: bool,
        feedback=None,
    ):
        if candidate.representation != "native_output_tool":
            return await super().project_output_candidate(
                content,
                candidate,
                accepted=accepted,
                feedback=feedback,
            )
        return self.history_projection(
            [
                AIMessage(
                    content=content,
                    tool_calls=[
                        {
                            "id": candidate.candidate_id,
                            "name": FINAL_OUTPUT_TOOL_NAME,
                            "args": {"output": candidate.raw},
                        }
                    ],
                ),
                ToolMessage(
                    content=(
                        self.render_output_feedback(feedback)
                        if feedback is not None
                        else ("Output accepted." if accepted else "Output rejected; correction budget exhausted.")
                    ),
                    tool_call_id=candidate.candidate_id,
                ),
            ]
        )

    def turn_signature(self, result: InferenceResult) -> str:
        calls = [
            {"name": call.name, "args": thaw_json(cast(JsonValue, call.arguments))}
            for call in (result.tool_calls or [])
        ]
        return json.dumps(calls, sort_keys=True, ensure_ascii=False)

    async def model_turn(self, result: InferenceResult) -> ModelTurn:
        """Translate native text/tool calls into semantic actions."""
        actions = []
        for call in result.tool_calls or []:
            arguments = thaw_json(cast(JsonValue, call.arguments))
            if not isinstance(arguments, dict):
                raise TypeError("native tool-call arguments must be a JSON object")
            if call.name == FINAL_OUTPUT_TOOL_NAME:
                actions.append(
                    FinalCandidateAction(
                        candidate_id=call.id,
                        raw=arguments.get("output"),
                        representation="native_output_tool",
                    )
                )
            else:
                actions.append(
                    ToolCallAction(
                        action_id=call.id,
                        name=call.name,
                        arguments=arguments,
                    )
                )
        content = result.content or ""
        if result.tool_calls is not None and not result.tool_calls:
            binding = self.output_binding(is_text=self._output_is_text)
            if binding.kind in {
                OutputBindingKind.TEXT,
                OutputBindingKind.NATIVE_SCHEMA,
            }:
                actions.append(
                    FinalCandidateAction(
                        raw=content,
                        representation=("native_text" if binding.kind is OutputBindingKind.TEXT else "native_schema"),
                    )
                )
            elif content:
                actions.insert(0, TextAction(content=content))
        elif content:
            actions.insert(0, TextAction(content=content))
        return ModelTurn(content=content, actions=actions)


def make_command_channel(
    protocol: str,
    *,
    args_limiter: ArgsLimiter | None = None,
    output_is_text: bool = True,
    media_materializer: MediaMaterializer | None = None,
) -> CommandChannel:
    """Build the channel for a RoleSchema.command_protocol value.

    "xml" -> XmlCommandChannel; "native" -> NativeToolChannel. Unknown values
    fall back to XML (the safe, model-agnostic default). Provider wire envelopes
    are projected later by the Product endpoint adapter. ``args_limiter``
    (native only) is the recorded-args size limiter — pass
    ``executor.persist_large_args`` so a giant tool-call arg is persisted before
    it enters context. XML embeds args in the assistant text (no structured
    args list to limit), so it ignores it.
    """
    if protocol == "native":
        return NativeToolChannel(
            args_limiter=args_limiter,
            output_is_text=output_is_text,
            media_materializer=media_materializer,
        )
    return XmlCommandChannel(media_materializer=media_materializer)
