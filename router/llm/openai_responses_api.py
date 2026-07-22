# -*- coding: utf-8 -*-
"""Native OpenAI Responses API provider (whole-model takeover for gpt-5.4+).

Talks to ``responses.create`` instead of ``chat.completions.create`` — the only
OpenAI endpoint exposing native Tool Search (``tool_search`` with
``execution=client`` + ``defer_loading`` tool defs), which preserves the prompt
prefix cache across tool-reveals the way Anthropic's ``tool_reference`` blocks do
on the Messages API. Selected by :func:`resolve_api_type` for a genuine OpenAI
endpoint (``api.openai.com``) running a native-tool-search-capable model
(:func:`supports_native_tool_search`); every other OpenAI-family config stays on
:class:`OpenAILLM` (Chat Completions).

Like :class:`AnthropicLLM`, this is a NEW provider, not a rewrite: the rest of
the framework speaks the OpenAI wire shape (``Message.to_dict`` message dicts +
native tool specs), and this provider converts that into the Responses
``input`` items + ``instructions`` on the way out, then normalizes the Responses
output (``output`` items of ``message`` / ``function_call`` / ``tool_search_call``)
back into the agnostic ``get_choice_text`` / ``get_choice_tool_calls`` contract.

Tool Search (custom / client-execution path, mirroring the Anthropic custom
path): a ``SearchTools`` discovery result carries ``_tool_references`` on the
wire dict → converted to a ``tool_search_call`` + ``tool_search_output`` pair
whose embedded tool defs the API injects at context end (prefix stays byte
stable). A ``tool_search_call`` in the response is normalized back into a
``SearchTools`` tool call so it flows through the SAME executor + RoleState
reveal as every other path. NO server-hosted BM25 builtin — mote runs the match.
"""
from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, Optional, Union

from json_repair import repair_json
from tenacity import after_log, retry, retry_if_exception, stop_after_attempt

from mote.common.config.config.llm_config import LLMConfig, LLMType
from mote.common.const import USE_CONFIG_TIMEOUT
from mote.common.const.llm import supports_web_search
from mote.common.events import log_llm_stream
from mote.common.exception import LLMEmptyResponseError, LLMTimeoutError, classify_llm_error, is_retryable
from mote.common.logs import logger
from mote.common.model_profile import profile_for
from mote.common.utils.common import log_and_reraise
from mote.common.utils.token_counter import count_message_tokens, count_string_tokens
from mote.router.cost import CostTracker, TokenUsage
from mote.router.llm._retry import wait_retry_after
from mote.router.llm.base_llm import LLM_RETRY_ATTEMPTS, BaseLLM
from mote.router.llm.credentials import CredentialRotationMixin
from mote.router.llm.llm_provider_registry import register_provider
from mote.router.llm.llm_response import WebSearchHit
from mote.router.ratelimit.capture import install_rate_limit_hook

if TYPE_CHECKING:
    from openai import AsyncOpenAI

# The client-execution Tool Search tool injected into ``tools=`` when the request
# carries any ``defer_loading`` corpus member. Emitting it is what makes the model
# produce ``tool_search_call`` items (the Responses analog of Anthropic driving
# discovery through the custom SearchTools path). ``execution: client`` = mote
# runs the match (no server BM25 builtin).
_CLIENT_TOOL_SEARCH_SPEC: dict = {
    "type": "tool_search",
    "execution": "client",
    "description": "Search the deferred tool catalog by keyword to reveal matching tools.",
    "parameters": {
        "type": "object",
        "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
        "required": ["queries"],
    },
}

#: The canonical name the mote executor binds the tool-search meta-tool under.
_SEARCH_TOOLS_NAME = "SearchTools"


@register_provider([LLMType.OPENAI_RESPONSES])
class OpenAIResponsesLLM(CredentialRotationMixin, BaseLLM):
    """Provider for OpenAI's native Responses API (gpt-5.4+, native Tool Search)."""

    # Narrow the base class's Optional[AsyncOpenAI]: this provider always builds
    # its own client in _init_client() and never leaves it None.
    aclient: "AsyncOpenAI"

    def __init__(self, config: LLMConfig):
        self.config = config
        self._init_client()
        self.auto_max_tokens = False
        self.cost_manager: Optional[CostTracker] = None

    # -- client lifecycle ---------------------------------------------------
    def _init_client(self):
        self.model = self.config.model  # used in _cons_kwargs / cost
        self.max_completion_token = self.config.max_token
        self.pricing_plan = self.config.pricing_plan or self.model
        self._init_credentials()
        self.aclient = self._rebuild_client()

    def _rebuild_client(self) -> "AsyncOpenAI":
        from openai import AsyncOpenAI

        kwargs: dict[str, Any] = {"base_url": self.config.base_url}
        if self._oauth is not None:
            kwargs["api_key"] = self._oauth.get_valid_token()
            if self.config.oauth and self.config.oauth.headers_extra:
                kwargs["default_headers"] = dict(self.config.oauth.headers_extra)
        else:
            kwargs["api_key"] = self._current_api_key()
        if self.config.proxy:
            try:
                from openai._base_client import AsyncHttpxClientWrapper

                kwargs["http_client"] = AsyncHttpxClientWrapper(proxy=self.config.proxy, base_url=self.config.base_url)
            except Exception:  # noqa: BLE001 — proxy is best-effort
                logger.warning("httpx wrapper unavailable; ignoring proxy for OpenAIResponsesLLM.")
        client = AsyncOpenAI(**kwargs)
        install_rate_limit_hook(
            client,
            get_tracker=lambda: self.rate_limit_tracker,
            provider=self.provider_label,
            model=self.model or "unknown",
        )
        return client

    # -- message conversion (OpenAI wire shape -> Responses input items) ----
    def _convert_messages(
        self, messages: list[dict], *, render_tool_references: bool = False
    ) -> tuple[str, list[dict]]:
        """Split the system prompt and convert the rest into Responses input items.

        Returns ``(instructions, input_items)``. System messages join into the
        top-level ``instructions`` string; user/assistant text become ``message``
        items; assistant ``tool_calls`` become ``function_call`` items; ordinary
        tool results become ``function_call_output`` items.

        A tool result carrying ``_tool_references`` (a SearchTools discovery)
        becomes a ``tool_search_call`` + ``tool_search_output`` PAIR replayed
        every turn (Responses is stateless without ``previous_response_id``): the
        output embeds each referenced tool's full definition stamped
        ``defer_loading: true`` so the API injects them at context end — keeping
        the request prefix byte-stable so the prompt cache survives.

        ``render_tool_references`` gates that discovery rendering: the pair is only
        valid when the SAME request carries the deferred-tool corpus it expands
        against, so the caller (``_cons_kwargs``) passes True only when the request
        deferred a corpus member (``native_defer``). When False (any toolless
        ``aask`` — summarize, dedup guards, routing), a result carrying
        ``_tool_references`` degrades to an ordinary ``function_call_output``
        instead of emitting an orphaned search pair the API would reject. The
        private ``_tool_references`` routing key never reaches the wire either way.
        """
        instructions_parts: list[str] = []
        items: list[dict] = []

        for msg in messages:
            role = msg.get("role")
            if role == "system":
                text = self._stringify(msg.get("content"))
                if text:
                    instructions_parts.append(text)
                continue
            if role == "tool":
                refs = msg.get("_tool_references")
                if refs and render_tool_references:
                    items.extend(self._tool_search_pair(msg.get("tool_call_id", ""), refs))
                else:
                    items.append(
                        {
                            "type": "function_call_output",
                            "call_id": msg.get("tool_call_id", ""),
                            "output": self._stringify(msg.get("content")),
                        }
                    )
                continue
            if role == "assistant":
                text = self._stringify(msg.get("content"))
                if text:
                    items.append(self._message_item("assistant", text))
                for call in msg.get("tool_calls") or []:
                    fn = call.get("function") or {}
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": call.get("id", ""),
                            "name": fn.get("name", ""),
                            "arguments": self._stringify_arguments(fn.get("arguments")),
                        }
                    )
                continue
            # user (default): plain string or a multimodal content-block list.
            text = self._stringify(msg.get("content"))
            if text:
                items.append(self._message_item("user", text))

        return "\n\n".join(instructions_parts), items

    def _tool_search_pair(self, call_id: str, refs: list[str]) -> list[dict]:
        """Build the ``tool_search_call`` + ``tool_search_output`` discovery pair.

        The output embeds each revealed tool's FULL definition (flat Responses
        function shape) stamped ``defer_loading: true`` — the API injects those
        defs at context end, so the cached request prefix stays byte-stable.
        Definitions are looked up from the specs the executor built for this
        request (stashed on ``_defer_specs`` by ``_cons_kwargs``); a name with no
        matching spec is skipped (it will simply not expand).
        """
        specs_by_name = getattr(self, "_defer_specs", {}) or {}
        tools: list[dict] = []
        for name in refs:
            spec = specs_by_name.get(name)
            if spec is not None:
                tools.append({**spec, "defer_loading": True})
        return [
            {
                "type": "tool_search_call",
                "execution": "client",
                "call_id": call_id,
                "arguments": {"queries": []},
                "status": "completed",
            },
            {
                "type": "tool_search_output",
                "execution": "client",
                "call_id": call_id,
                "status": "completed",
                "tools": tools,
            },
        ]

    @staticmethod
    def _message_item(role: str, text: str) -> dict:
        """A Responses ``message`` item (``input_text`` for user, ``output_text`` for assistant)."""
        content_type = "output_text" if role == "assistant" else "input_text"
        return {"type": "message", "role": role, "content": [{"type": content_type, "text": text}]}

    @staticmethod
    def _stringify(content: Any) -> str:
        """Coerce a message ``content`` (str or block list) into plain text."""
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") in ("text", "input_text", "output_text"):
                    parts.append(item.get("text", ""))
                elif isinstance(item, str):
                    parts.append(item)
            return "".join(parts)
        return str(content)

    @staticmethod
    def _stringify_arguments(arguments: Any) -> str:
        """Coerce a tool call's ``arguments`` into the JSON STRING Responses wants."""
        if arguments is None:
            return "{}"
        if isinstance(arguments, str):
            return arguments
        try:
            return json.dumps(arguments, ensure_ascii=False)
        except (TypeError, ValueError):
            return "{}"

    # -- tool spec / tool_choice conversion ---------------------------------
    @staticmethod
    def _to_responses_tool(tool: dict) -> dict:
        """Coerce any inbound tool spec into the FLAT Responses function shape.

        ``native_specs("openai_responses")`` already emits the flat shape
        (``{"type":"function","name","description","parameters"}``); a Chat
        Completions nested spec (``{"type":"function","function":{...}}``) is
        flattened as a safety net. ``defer_loading`` (corpus stamp) is preserved.
        """
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            fn = tool["function"]
            flat = {
                "type": "function",
                "name": fn.get("name", ""),
                "description": fn.get("description", "") or "",
                "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
            }
            if tool.get("defer_loading"):
                flat["defer_loading"] = True
            return flat
        return tool

    @staticmethod
    def _convert_tool_choice(tool_choice: Union[str, dict]) -> Any:
        """Map an OpenAI ``tool_choice`` directive to the Responses shape.

        Responses takes ``"auto"``/``"required"``/``"none"`` strings directly, or a
        forced ``{"type":"function","name":...}`` object (flat, unlike Chat
        Completions' nested ``function``).
        """
        if isinstance(tool_choice, str):
            return tool_choice if tool_choice in ("auto", "required", "none") else "auto"
        if isinstance(tool_choice, dict):
            fn = tool_choice.get("function") or {}
            name = fn.get("name") or tool_choice.get("name")
            if name:
                return {"type": "function", "name": name}
        return "auto"

    def _cons_kwargs(self, messages: list[dict], timeout=USE_CONFIG_TIMEOUT, **extra_kwargs) -> dict:
        extra_kwargs.pop("raise_if_empty", None)
        # Tools FIRST: indexing the deferred-corpus defs into ``_defer_specs`` must
        # happen before ``_convert_messages`` runs, because a SearchTools discovery
        # result there embeds each referenced tool's full def (read from that index)
        # into its ``tool_search_output`` item.
        tools = extra_kwargs.pop("tools", None)
        native_defer = False
        self._defer_specs: dict[str, dict] = {}
        responses_tools: Optional[list[dict]] = None
        if tools:
            responses_tools = [self._to_responses_tool(t) for t in tools]
            for spec in responses_tools:
                if spec.get("defer_loading") and spec.get("name"):
                    native_defer = True
                    self._defer_specs[spec["name"]] = {k: v for k, v in spec.items() if k != "defer_loading"}
            # When any corpus member is deferred, inject the client tool-search
            # tool so the model can emit ``tool_search_call`` items.
            if native_defer:
                responses_tools.append(dict(_CLIENT_TOOL_SEARCH_SPEC))

        # A ``tool_search`` discovery pair is only valid when this request deferred
        # a corpus member for it to expand against; a toolless aask degrades the
        # discovery result to plain ``function_call_output`` (see _convert_messages).
        instructions, items = self._convert_messages(messages, render_tool_references=native_defer)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": items,
            "timeout": self.get_timeout(timeout),
        }
        if instructions:
            kwargs["instructions"] = instructions
        if self.max_completion_token:
            kwargs["max_output_tokens"] = self.max_completion_token
        # temperature only when explicitly set (mirrors AnthropicLLM): the default
        # 0.0 can't be told apart from an intentional value, and newer OpenAI
        # models reject the parameter outright.
        if "temperature" in getattr(self.config, "model_fields_set", set()):
            kwargs["temperature"] = self.config.temperature
        # Reasoning effort: the Responses API takes a ``reasoning.effort`` block.
        # Gated by the model's declared thinking capability so an incapable model
        # never receives it.
        effort = self.config.reasoning_effort
        if effort and profile_for(self.model).supports_thinking:
            kwargs["reasoning"] = {"effort": effort}
        if responses_tools is not None:
            kwargs["tools"] = responses_tools

        if extra_kwargs.get("tool_choice") is not None:
            kwargs["tool_choice"] = self._convert_tool_choice(extra_kwargs.pop("tool_choice"))
        else:
            extra_kwargs.pop("tool_choice", None)

        kwargs.update(extra_kwargs)
        # Strip private wire keys the framework threads on messages but the API
        # never accepts (mirrors OpenAILLM._strip_cache_intent / Anthropic's
        # _cache_intent strip). They were consumed in _convert_messages already.
        kwargs.pop("_cache_intent", None)
        kwargs.pop("_tool_references", None)
        # Responses rejects an empty ``input`` with no ``previous_response_id``;
        # a tool-only history could reduce to zero message items, so guard it.
        if not kwargs["input"] and not kwargs.get("previous_response_id"):
            kwargs["input"] = [self._message_item("user", "")]
        return kwargs

    # -- completion calls ---------------------------------------------------
    async def _acreate(self, **kwargs):
        try:
            return await self.aclient.responses.create(**kwargs)
        except Exception as e:
            raise classify_llm_error(e) or e

    async def _achat_completion(
        self, messages: list[dict], timeout=USE_CONFIG_TIMEOUT, raise_if_empty: bool = True, **chat_configs
    ):
        kwargs = self._cons_kwargs(messages, timeout=self.get_timeout(timeout), **chat_configs)
        rsp = await self._acreate(**kwargs)
        if raise_if_empty and not (self.get_choice_text(rsp) or self.get_choice_tool_calls(rsp)):
            raise LLMEmptyResponseError("The LLM's response is empty.")
        self._update_costs(getattr(rsp, "usage", None))
        return rsp

    async def acompletion(self, messages: list[dict], timeout=USE_CONFIG_TIMEOUT, raise_if_empty: bool = True):
        return await self._achat_completion(messages, timeout=self.get_timeout(timeout), raise_if_empty=raise_if_empty)

    async def _consume_stream(self, kwargs: dict) -> tuple[list[str], Any]:
        """Open a streaming Responses request, drain it, return (text deltas, final response).

        Factored out so callers can bound it with ``asyncio.wait_for`` (mirrors
        AnthropicLLM). Text deltas mirror to ``log_llm_stream`` for the live
        console; the terminal ``response.completed`` event carries the assembled
        ``Response`` object which ``get_choice_*`` parse exactly like the blocking
        path.
        """
        from openai.types.responses import ResponseCompletedEvent, ResponseTextDeltaEvent

        collected: list[str] = []
        final_response: Any = None
        stream = await self.aclient.responses.create(**{**kwargs, "stream": True})
        async for event in stream:
            if isinstance(event, ResponseTextDeltaEvent):
                log_llm_stream(event.delta)
                collected.append(event.delta)
            elif isinstance(event, ResponseCompletedEvent):
                final_response = event.response
        return collected, final_response

    async def _achat_completion_stream(
        self, messages: list[dict], timeout=USE_CONFIG_TIMEOUT, raise_if_empty: bool = True, **chat_configs
    ) -> str:
        kwargs = self._cons_kwargs(messages, timeout=self.get_timeout(timeout), **chat_configs)
        deadline = self.get_timeout(timeout)
        try:
            collected, final_response = await asyncio.wait_for(self._consume_stream(kwargs), timeout=deadline)
        except asyncio.TimeoutError as e:
            raise LLMTimeoutError(f"Streaming response stalled: no completion within {deadline}s", cause=e)
        except Exception as e:
            raise classify_llm_error(e) or e

        log_llm_stream("\n")
        full = "".join(collected)
        if raise_if_empty and not full:
            raise LLMEmptyResponseError("The LLM's response is empty.")
        self._update_costs(getattr(final_response, "usage", None))
        return full

    async def _achat_completion_stream_tool(
        self, messages: list[dict], timeout=USE_CONFIG_TIMEOUT, raise_if_empty: bool = True, **chat_configs
    ):
        """Streaming native tool-use completion: stream text, return the full Response.

        The Responses SDK assembles the complete ``Response`` (``output`` items:
        text + ``function_call`` + ``tool_search_call``) as it consumes the event
        stream; we mirror the text deltas for the live console and return the
        assembled response so ``get_choice_text`` / ``get_choice_tool_calls``
        parse it exactly as the blocking path.
        """
        kwargs = self._cons_kwargs(messages, timeout=self.get_timeout(timeout), **chat_configs)
        deadline = self.get_timeout(timeout)
        try:
            _, final_response = await asyncio.wait_for(self._consume_stream(kwargs), timeout=deadline)
        except asyncio.TimeoutError as e:
            raise LLMTimeoutError(f"Streaming response stalled: no completion within {deadline}s", cause=e)
        except Exception as e:
            raise classify_llm_error(e) or e

        log_llm_stream("\n")
        if raise_if_empty and not (self.get_choice_text(final_response) or self.get_choice_tool_calls(final_response)):
            raise LLMEmptyResponseError("The LLM's response is empty.")
        self._update_costs(getattr(final_response, "usage", None))
        return final_response

    @retry(
        wait=wait_retry_after(),
        stop=stop_after_attempt(LLM_RETRY_ATTEMPTS),
        after=after_log(logger, logger.level("WARNING").name),  # type: ignore[arg-type]  # loguru logger + str level vs tenacity stdlib-logging stub
        retry=retry_if_exception(is_retryable),
        retry_error_callback=log_and_reraise,
    )
    async def acompletion_text(
        self, messages: list[dict], stream=False, timeout=USE_CONFIG_TIMEOUT, raise_if_empty: bool = True
    ) -> str:
        if stream:
            return await self._achat_completion_stream(messages, timeout=timeout, raise_if_empty=raise_if_empty)
        rsp = await self._achat_completion(messages, timeout=self.get_timeout(timeout), raise_if_empty=raise_if_empty)
        return self.get_choice_text(rsp)

    # -- response normalization --------------------------------------------
    def get_choice_text(self, rsp) -> str:
        """Concatenate all ``output_text`` from a Responses ``output``.

        The SDK exposes ``response.output_text`` as a convenience aggregate; fall
        back to walking ``output`` message items when it's absent (e.g. a
        hand-built test double).
        """
        text = getattr(rsp, "output_text", None)
        if isinstance(text, str) and text:
            return text
        parts: list[str] = []
        for item in getattr(rsp, "output", None) or []:
            if getattr(item, "type", None) != "message":
                continue
            for block in getattr(item, "content", None) or []:
                if getattr(block, "type", None) == "output_text":
                    parts.append(getattr(block, "text", "") or "")
        return "".join(parts)

    def get_choice_tool_calls(self, rsp) -> list[dict]:
        """Normalize Responses ``output`` items to the agnostic tool-call list.

        ``function_call`` → an ordinary tool call (arguments parsed from the JSON
        string, with a json_repair fallback for a malformed tail).
        ``tool_search_call`` (execution=client) → a ``SearchTools`` call whose
        ``query`` is the space-joined ``queries`` — so discovery flows through the
        SAME executor + RoleState reveal as every other path.
        """
        out: list[dict] = []
        for item in getattr(rsp, "output", None) or []:
            itype = getattr(item, "type", None)
            if itype == "function_call":
                raw_args = getattr(item, "arguments", None)
                out.append(
                    {
                        "id": getattr(item, "call_id", "") or getattr(item, "id", ""),
                        "name": getattr(item, "name", ""),
                        "arguments": self._parse_arguments(raw_args),
                    }
                )
            elif itype == "tool_search_call":
                out.append(self._tool_search_call_to_search_tools(item))
        return out

    def _tool_search_call_to_search_tools(self, item: Any) -> dict:
        """Map a Responses ``tool_search_call`` into a mote ``SearchTools`` call."""
        args = getattr(item, "arguments", None)
        parsed = self._parse_arguments(args)
        queries = parsed.get("queries") if isinstance(parsed, dict) else None
        query = " ".join(queries) if isinstance(queries, list) else str(queries or "")
        return {
            "id": getattr(item, "call_id", "") or getattr(item, "id", ""),
            "name": _SEARCH_TOOLS_NAME,
            "arguments": {"query": query},
        }

    @staticmethod
    def _parse_arguments(arguments: Any) -> dict:
        """Parse a tool call's ``arguments`` (JSON string or dict) into a dict.

        Falls back to ``json_repair`` on a malformed string (a model emitting a
        large multi-line argument sometimes leaks invalid JSON), matching the
        OpenAI Chat Completions provider's recovery.
        """
        if isinstance(arguments, dict):
            return arguments
        if not arguments:
            return {}
        try:
            parsed = json.loads(arguments)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            try:
                repaired = repair_json(arguments, return_objects=True)
            except Exception:  # noqa: BLE001 — repair is best-effort
                return {}
            return repaired if isinstance(repaired, dict) else {}

    # -- server-side web search --------------------------------------------
    async def aweb_search(
        self,
        query: str,
        *,
        allowed_domains: Optional[list[str]] = None,
        blocked_domains: Optional[list[str]] = None,
        max_uses: int = 8,
    ) -> list[WebSearchHit]:
        """Run OpenAI's Responses ``web_search`` built-in and return the hits.

        Issues an isolated ``responses.create`` carrying the ``web_search``
        built-in tool so the API performs the search + crawl. Results surface as
        ``url_citation`` annotations on the assistant message's ``output_text``
        blocks; we collect those into :class:`WebSearchHit`. ``allowed_domains`` is
        passed through the tool's ``filters`` when supported; ``blocked_domains``
        has no Responses analog and is applied as a post-filter.

        Raises ``NotImplementedError`` when the routed model does not support
        server-side search, so the WebSearch tool degrades cleanly instead of
        firing a request the API would reject with an uncatchable opaque error.
        """
        if not supports_web_search(self.model):
            raise NotImplementedError(f"{self.model} does not support server-side web search.")

        tool_spec: dict[str, Any] = {"type": "web_search"}
        if allowed_domains:
            tool_spec["filters"] = {"allowed_domains": allowed_domains}

        messages = [
            {"role": "system", "content": "You are an assistant for performing a web search tool use"},
            {"role": "user", "content": f"Perform a web search for the query: {query}"},
        ]
        kwargs = self._cons_kwargs(messages, tools=[tool_spec], raise_if_empty=False)
        rsp = await self._acreate(**kwargs)
        hits = self._extract_web_search_hits(rsp)
        if blocked_domains:
            blocked = tuple(blocked_domains)
            hits = [h for h in hits if not any(b in h.url for b in blocked)]
        return hits

    @staticmethod
    def _extract_web_search_hits(rsp: Any) -> list[WebSearchHit]:
        """Pull ``{title, url}`` from ``url_citation`` annotations in a Responses reply.

        Walks the response ``output`` for ``message`` items, then each
        ``output_text`` content block's ``annotations`` for ``url_citation``
        entries (each carrying ``url`` + ``title``). Dedupes by URL, preserving
        first-seen order.
        """
        hits: list[WebSearchHit] = []
        seen: set[str] = set()
        for item in getattr(rsp, "output", None) or []:
            if getattr(item, "type", None) != "message":
                continue
            for block in getattr(item, "content", None) or []:
                if getattr(block, "type", None) != "output_text":
                    continue
                for ann in getattr(block, "annotations", None) or []:
                    atype = ann.get("type") if isinstance(ann, dict) else getattr(ann, "type", None)
                    if atype != "url_citation":
                        continue
                    url = ann.get("url") if isinstance(ann, dict) else getattr(ann, "url", None)
                    title = ann.get("title") if isinstance(ann, dict) else getattr(ann, "title", None)
                    if url and url not in seen:
                        seen.add(url)
                        hits.append(WebSearchHit(title=title or "", url=url))
        return hits

    # -- token / usage helpers ---------------------------------------------
    def _calc_usage(self, messages: list[dict], rsp: str):
        if not self.config.calc_usage:
            return TokenUsage()
        try:
            prompt = count_message_tokens(messages, self.pricing_plan)
            completion = count_string_tokens(rsp, self.pricing_plan)
            return TokenUsage(input_tokens=prompt, output_tokens=completion, total_tokens=prompt + completion)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"usage calculation failed: {e}")
            return TokenUsage()

    def count_tokens(self, messages: list[dict]) -> int:
        try:
            return count_message_tokens(messages, self.model)
        except Exception:  # noqa: BLE001
            return super().count_tokens(messages)
