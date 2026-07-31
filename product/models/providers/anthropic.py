# -*- coding: utf-8 -*-
"""Product integration for the native Anthropic Messages API.

Talks directly to ``/v1/messages`` via the ``anthropic`` SDK, instead of routing
Claude through the OpenAI-compatible client. Selected by ``api_type: anthropic``
or auto-detected when ``base_url`` points at ``anthropic.com``.

The rest of the framework speaks the OpenAI wire shape: message dicts come in as
``{"role", "content"[, "tool_calls"]}`` / ``{"role": "tool", "tool_call_id"}``
(produced by ``Message.to_dict``), and tool specs/``tool_choice`` follow the
OpenAI or already-Anthropic envelope. This provider converts that shape into the
Anthropic ``messages`` + ``system`` + content-block format on the way out, and
normalizes the Anthropic response (``content`` blocks of ``text`` / ``tool_use``)
back into the agnostic ``get_choice_text`` / ``get_choice_tool_calls`` contract.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, Optional, Union

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic as AsyncAnthropicClient

try:
    import httpx
except ImportError:  # pragma: no cover - optional provider extra
    httpx = None

try:
    from anthropic import AsyncAnthropic
except ImportError:  # pragma: no cover - optional provider extra
    AsyncAnthropic = None

from mote.contracts.config.model.llm import LLMConfig
from mote.contracts.model import WebSearchHit
from mote.contracts.model.capabilities import supports_web_search
from mote.contracts.model.constants import USE_CONFIG_TIMEOUT
from mote.contracts.model.profile import profile_for
from mote.kernel.inference.tokenization import count_message_tokens, count_string_tokens
from mote.runtime.errors import LLMEmptyResponseError, LLMTimeoutError, classify_llm_error
from mote.runtime.events.stream import log_llm_stream
from mote.runtime.models.clients.base import BaseLLM
from mote.runtime.models.clients.credentials import CredentialBindingMixin
from mote.runtime.models.cost import CostTracker, TokenUsage
from mote.runtime.models.media import parse_data_url, resolve_image_media_type
from mote.runtime.models.ratelimit.capture import install_rate_limit_hook
from mote.runtime.telemetry.logging import logger

# Anthropic's ``thinking`` takes a concrete ``budget_tokens``, not an effort enum,
# so the provider owns the effort→budget mapping (its wire shape, its table). The
# 1024 floor is the API's minimum enabled-thinking budget.
_EFFORT_BUDGET: dict[str, int] = {
    "minimal": 1024,
    "low": 4096,
    "medium": 8192,
    "high": 16384,
}
# Thinking tokens and the visible answer draw from the SAME ``max_tokens`` envelope,
# and the API requires ``max_tokens > budget_tokens``. Reserve at least this much
# room for the visible answer ON TOP of the thinking budget, so a small configured
# ``max_tokens`` (e.g. the 4096 default) can never 400 against a larger effort budget.
_ANSWER_TOKEN_FLOOR = 4096


class AnthropicLLM(CredentialBindingMixin, BaseLLM):
    """Provider for Anthropic's native Messages API (Claude models)."""

    # Narrow the base class's Optional[AsyncOpenAI]: this provider builds its own
    # AsyncAnthropic client in _init_client() and never leaves it None.
    aclient: AsyncAnthropicClient

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
        # Normalize credentials (api_key list / OAuth) via the shared mixin, then
        # build the client from the active one.
        self._init_credentials()
        self.aclient = self._rebuild_client()

    def _rebuild_client(self):
        if AsyncAnthropic is None:
            raise RuntimeError("Anthropic support requires the 'anthropic' optional dependency.")

        kwargs: dict[str, Any] = {"max_retries": 0}
        if self.config.base_url:
            kwargs["base_url"] = self.config.base_url
        if self._oauth is not None:
            # OAuth path: send the bearer token via auth_token and merge any
            # provider-specific extra headers (e.g. the anthropic-beta opt-in).
            kwargs["auth_token"] = self._oauth.get_valid_token()
            if self.config.oauth and self.config.oauth.headers_extra:
                kwargs["default_headers"] = dict(self.config.oauth.headers_extra)
        else:
            kwargs["api_key"] = self._current_api_key()
        if http_client := self._make_http_client():
            kwargs["http_client"] = http_client
        client = AsyncAnthropic(**kwargs)
        install_rate_limit_hook(
            client,
            get_tracker=lambda: self.rate_limit_tracker,
            provider=self.provider_label,
            model=self.model or "unknown",
        )
        return client

    def _make_http_client(self):
        """Build an httpx async client for proxy support, or None."""
        if not self.config.proxy:
            return None
        if httpx is None:
            logger.warning("httpx unavailable; ignoring proxy for AnthropicLLM.")
            return None
        return httpx.AsyncClient(proxy=self.config.proxy)

    # -- message conversion (OpenAI wire shape -> Anthropic) ----------------
    def _convert_messages(
        self, messages: list[dict], *, render_tool_references: bool = False
    ) -> tuple[str, list[dict]]:
        """Split out the system prompt and convert the rest to Anthropic messages.

        Returns ``(system_text, anthropic_messages)``. System messages are joined
        into the top-level ``system`` string; ``tool`` results become ``tool_result``
        blocks inside a user turn; assistant ``tool_calls`` become ``tool_use``
        blocks. Consecutive same-role turns are merged so the user/assistant
        alternation the API expects is preserved (e.g. parallel tool results).

        ``render_tool_references`` gates the SearchTools-discovery rendering: a
        ``tool_reference`` block is only valid when the SAME request carries the
        deferred-tool corpus it expands against, so the caller (``_cons_kwargs``)
        passes True only when the request's ``tools`` include a ``defer_loading``
        member. When False (e.g. any toolless ``aask`` — summarize, dedup guards,
        routing), a result carrying ``_tool_references`` degrades to its plain
        stringified text instead of emitting an orphaned reference the API would
        reject with a 400. The private routing key never reaches the wire either way.
        """
        system_parts: list[str] = []
        converted: list[dict] = []

        for msg in messages:
            role = msg.get("role")
            if role == "system":
                text = self._stringify(msg.get("content"))
                if text:
                    system_parts.append(text)
                continue
            if role == "tool":
                # Server-side tool-search (custom path): when the result carries
                # ``_tool_references`` (a SearchTools discovery) AND this request
                # carries the deferred-tool corpus, render the tool_result content
                # as a list of ``tool_reference`` blocks — the API expands each into
                # the tool's full definition. Without the corpus (toolless aask) the
                # reference has nothing to expand against, so degrade to the ordinary
                # stringified text. The private routing key never reaches the wire.
                refs = msg.get("_tool_references")
                if refs and render_tool_references:
                    content: Any = [{"type": "tool_reference", "tool_name": n} for n in refs]
                else:
                    content = self._stringify(msg.get("content"))
                block = {
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": content,
                }
                self._append_blocks(converted, "user", [block])
                continue
            if role == "assistant":
                blocks: list[dict] = []
                text = self._stringify(msg.get("content"))
                if text:
                    blocks.append({"type": "text", "text": text})
                for call in msg.get("tool_calls") or []:
                    fn = call.get("function") or {}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call.get("id", ""),
                            "name": fn.get("name", ""),
                            "input": self._parse_arguments(fn.get("arguments")),
                        }
                    )
                if blocks:
                    self._append_blocks(converted, "assistant", blocks)
                continue
            # user (default): may be a plain string or a multimodal block list.
            blocks = self._user_content_to_blocks(msg.get("content"))
            if blocks:
                # Propagate the message's declarative cache intent onto its blocks
                # so ``_apply_cache_breakpoints`` can skip an ephemeral tail even
                # after ``_append_blocks`` merges it into the prior user turn. The
                # marker is a private block key stripped again before the request
                # goes out (see ``_apply_cache_breakpoints``).
                intent = msg.get("_cache_intent")
                if intent:
                    blocks = [{**b, "_cache_intent": intent} for b in blocks]
                self._append_blocks(converted, "user", blocks)

        return "\n\n".join(system_parts), converted

    @staticmethod
    def _append_blocks(converted: list[dict], role: str, blocks: list[dict]) -> None:
        """Append blocks, merging into the previous turn when the role matches."""
        if converted and converted[-1]["role"] == role:
            converted[-1]["content"].extend(blocks)
        else:
            converted.append({"role": role, "content": list(blocks)})

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
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif isinstance(item, str):
                    parts.append(item)
            return "".join(parts)
        return str(content)

    @staticmethod
    def _parse_arguments(arguments: Any) -> dict:
        """Parse a tool call's ``arguments`` (JSON string or dict) into a dict."""
        if isinstance(arguments, dict):
            return arguments
        if not arguments:
            return {}
        try:
            parsed = json.loads(arguments)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def _user_content_to_blocks(self, content: Any) -> list[dict]:
        """Convert OpenAI-style user content into Anthropic content blocks."""
        if content is None:
            return []
        if isinstance(content, str):
            return [{"type": "text", "text": content}] if content else []
        if not isinstance(content, list):
            return [{"type": "text", "text": str(content)}]

        blocks: list[dict] = []
        for item in content:
            if not isinstance(item, dict):
                blocks.append({"type": "text", "text": str(item)})
                continue
            itype = item.get("type")
            if itype == "text":
                blocks.append({"type": "text", "text": item.get("text", "")})
            elif itype == "image_url":
                block = self._image_url_to_block(item.get("image_url") or {})
                if block:
                    blocks.append(block)
            else:
                # ``document`` (already Anthropic-shaped) and any native block pass through.
                blocks.append(item)
        return blocks

    @staticmethod
    def _image_url_to_block(image_url: dict) -> Optional[dict]:
        """Convert an OpenAI ``image_url`` part into an Anthropic image block."""
        url = image_url.get("url") if isinstance(image_url, dict) else None
        if not url:
            return None
        if url.startswith("data:"):
            parsed = parse_data_url(url)
            if parsed is None:
                return None
            declared, data = parsed
            # The declared media type is often wrong (e.g. a PNG labelled as
            # JPEG); Anthropic rejects mismatches, so resolve_image_media_type
            # prefers the sniffed type and falls back to the declaration.
            media_type = resolve_image_media_type(data, declared)
            return {
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": data},
            }
        return {"type": "image", "source": {"type": "url", "url": url}}

    # -- tool spec / tool_choice conversion ---------------------------------
    @staticmethod
    def _convert_tools(tools: list[dict]) -> list[dict]:
        """Accept Anthropic-shaped specs as-is; convert OpenAI ``function`` specs."""
        converted: list[dict] = []
        for tool in tools or []:
            if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
                fn = tool["function"]
                converted.append(
                    {
                        "name": fn.get("name", ""),
                        "description": fn.get("description", "") or "",
                        "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
                    }
                )
            else:
                converted.append(tool)
        return converted

    @staticmethod
    def _convert_tool_choice(tool_choice: Union[str, dict]) -> dict:
        """Map an OpenAI ``tool_choice`` directive to the Anthropic shape."""
        if isinstance(tool_choice, str):
            return {
                "auto": {"type": "auto"},
                "required": {"type": "any"},
                "any": {"type": "any"},
                "none": {"type": "none"},
            }.get(tool_choice, {"type": "auto"})
        if isinstance(tool_choice, dict):
            if tool_choice.get("type") in ("auto", "any", "tool", "none"):
                return tool_choice  # already Anthropic-shaped
            # OpenAI forced choice: {"type": "function", "function": {"name": ...}}
            fn = tool_choice.get("function") or {}
            name = fn.get("name") or tool_choice.get("name")
            if name:
                return {"type": "tool", "name": name}
        return {"type": "auto"}

    def _cons_kwargs(self, messages: list[dict], timeout=USE_CONFIG_TIMEOUT, **extra_kwargs) -> dict:
        # raise_if_empty is a control flag for the caller, never a wire param.
        extra_kwargs.pop("raise_if_empty", None)
        # Resolve the tool corpus FIRST: a ``tool_reference`` block in history is
        # only valid when this request carries the deferred-tool corpus it expands
        # against, so ``_convert_messages`` must know whether tools are present
        # before it renders the tool-result blocks.
        converted_tools: Optional[list[dict]] = None
        if "tools" in extra_kwargs:
            tools = extra_kwargs.pop("tools")
            if tools:
                converted_tools = self._convert_tools(tools)
        render_tool_references = bool(converted_tools)
        system, converted = self._convert_messages(messages, render_tool_references=render_tool_references)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self._get_max_tokens(),
            "messages": converted,
            "timeout": self.get_timeout(timeout),
        }
        # Extended thinking: translate the unified effort enum into Anthropic's
        # ``thinking`` block, gated by the model's declared capability. The API
        # forbids ``temperature`` alongside thinking, so the branch is exclusive.
        effort = self.config.reasoning_effort
        thinking_on = bool(effort) and profile_for(self.model).supports_thinking
        if thinking_on:
            budget = _EFFORT_BUDGET.get(effort or "", _EFFORT_BUDGET["low"])
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
            # Thinking + answer share ``max_tokens`` and the API requires
            # ``max_tokens > budget_tokens``. Grow the envelope to guarantee answer
            # headroom ON TOP of the thinking budget — never shrink the budget to
            # fit a small configured ceiling (that would silently weaken the
            # requested effort, the same anti-pattern as clamping an effort enum).
            kwargs["max_tokens"] = max(kwargs["max_tokens"], budget + _ANSWER_TOKEN_FLOOR)
        # Only send ``temperature`` when the user set it explicitly: it carries a
        # default (0.0) that can't be distinguished from an intentional value,
        # and newer Claude models reject/deprecate the parameter outright. Never
        # sent while thinking is enabled (the API rejects the combination).
        elif "temperature" in self.config.model_fields_set:
            kwargs["temperature"] = self.config.temperature
        if system:
            kwargs["system"] = system
        if converted_tools is not None:
            kwargs["tools"] = converted_tools
        if extra_kwargs.get("tool_choice") is not None:
            kwargs["tool_choice"] = self._convert_tool_choice(extra_kwargs.pop("tool_choice"))
        else:
            extra_kwargs.pop("tool_choice", None)
        kwargs.update(extra_kwargs)
        # Place prompt-cache breakpoints last, after the wire shape is final, so
        # they land on the actual blocks being sent (system / tools / tail).
        self._apply_cache_breakpoints(kwargs)
        return kwargs

    # -- prompt caching (Anthropic manual cache_control) --------------------
    def _apply_cache_breakpoints(self, kwargs: dict) -> None:
        """Mark the stable request prefixes with ``cache_control`` (Claude-only).

        Unlike the OpenAI-compatible providers (automatic prefix caching, no
        markers, no write cost), Anthropic caches only where an explicit
        ``cache_control: {"type": "ephemeral"}`` breakpoint is placed. We use
        a strategy of at most three breakpoints, each protecting a
        successively longer stable prefix, so one downstream change still leaves
        the earlier prefixes hitting the cache:

        1. **system** — the last system block (large, session-stable);
        2. **tools** — the last tool schema (the catalog, stable across turns,
           cached right after the system prefix);
        3. **messages** — exactly one marker on the final content block of the
           last message (the rolling conversation tail: each turn the prior
           prefix is a cheap cache read, only the freshly-appended tail is
           written).

        Additive and safe: below the model's minimum cacheable size (~1024
        tokens) the API silently ignores a marker, and markers never change the
        response — only its billing. Gated by ``config.use_prompt_cache``.
        """
        if not getattr(self.config, "use_prompt_cache", True):
            return

        marker = {"type": "ephemeral"}

        # 1) system: normalize a plain string into a single text block so the
        #    marker has somewhere to live, then mark the last block. Built fresh
        #    here, so an in-place stamp aliases nothing.
        system = kwargs.get("system")
        if isinstance(system, str) and system:
            system = [{"type": "text", "text": system}]
            kwargs["system"] = system
        if isinstance(system, list) and system and isinstance(system[-1], dict):
            last_block: dict[str, Any] = system[-1]
            last_block["cache_control"] = dict(marker)

        # 2) tools: the catalog caches after the system prefix. Mark the last
        #    tool so a system-only change still reuses the cached tool section.
        #    Anthropic-shaped specs pass through ``_convert_tools`` by reference,
        #    so copy-on-mark to avoid stamping the caller's tool dict.
        #    A ``defer_loading: true`` tool (server-side tool-search corpus member)
        #    must NOT also carry ``cache_control`` — the API returns a 400 — so
        #    scan from the end for the last NON-deferred tool to anchor on. If
        #    every tool is deferred (shouldn't happen: SearchTools + core tools
        #    stay non-deferred, and the API itself forbids all-deferred), skip the
        #    tools breakpoint entirely (system + messages markers still apply).
        tools = kwargs.get("tools")
        if isinstance(tools, list):
            for ti in range(len(tools) - 1, -1, -1):
                tool = tools[ti]
                if not isinstance(tool, dict):
                    continue
                if tool.get("defer_loading"):
                    continue  # can't carry cache_control — keep scanning
                tools[ti] = {**tool, "cache_control": dict(marker)}
                break

        # 3) messages: exactly one marker, on the end of the *stable* conversation
        #    prefix — the last DURABLE block, not the final message by position.
        #    mote's think path assembles the request as ``stored_history +
        #    [ephemeral_tail]`` (context.manager.prepare_request): the tail is a
        #    request-only prompt carrying per-turn content (timestamp / cwd
        #    reminders) that changes every turn and is never stored. Anchoring on it
        #    would write a cache entry that never hits next turn AND leave the
        #    growing history with no breakpoint, forcing a full re-prefill each turn.
        #    A message-position heuristic (``[-2]``) is not enough: in native
        #    tool-use the tail is MERGED (``_append_blocks``) into the prior user
        #    turn that also holds the tool_result blocks, so the volatile text and a
        #    durable tool_result share one message. We therefore work at BLOCK
        #    granularity: the tail's blocks are tagged ``_cache_intent`` (see
        #    ``_convert_messages``); we anchor on the last block WITHOUT that tag —
        #    the true end of the cacheable prefix — then strip every ``_cache_intent``
        #    key so it never reaches the wire. A block may pass through by reference,
        #    so copy-on-mark to avoid stamping the caller's dict.
        messages = kwargs.get("messages")
        self._mark_last_durable_block(messages, marker)

    @staticmethod
    def _mark_last_durable_block(messages: Any, marker: dict) -> None:
        """Place the message-tail cache breakpoint on the last durable block.

        Scans blocks from the end, skipping any tagged ``_cache_intent`` (the
        per-turn ephemeral tail), and stamps ``cache_control`` on the first
        durable block found. Finally strips all ``_cache_intent`` tags so the
        private routing key never reaches the API. No-op on non-list content or
        an all-ephemeral request (nothing stable to cache).
        """
        if not (isinstance(messages, list) and messages):
            return

        anchored = False
        # Walk messages newest-first; within each, blocks newest-first.
        for msg in reversed(messages):
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for bi in range(len(content) - 1, -1, -1):
                block = content[bi]
                if not isinstance(block, dict):
                    continue
                if block.get("_cache_intent"):
                    continue  # ephemeral tail — never anchor here
                if not anchored:
                    content[bi] = {**block, "cache_control": dict(marker)}
                    anchored = True
                break  # only the LAST durable block of a turn can be the boundary
            if anchored:
                break

        # Strip the private routing key from every block (wire must stay canonical).
        for msg in messages:
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for bi, block in enumerate(content):
                if isinstance(block, dict) and "_cache_intent" in block:
                    stripped = dict(block)
                    del stripped["_cache_intent"]
                    content[bi] = stripped

    # -- completion calls ---------------------------------------------------
    async def _acreate(self, **kwargs):
        try:
            return await self.aclient.messages.create(**kwargs)
        except Exception as e:
            raise classify_llm_error(e) or e

    async def _achat_completion(
        self,
        messages: list[dict],
        timeout=USE_CONFIG_TIMEOUT,
        raise_if_empty: bool = True,
        **chat_configs,
    ):
        kwargs = self._cons_kwargs(messages, timeout=self.get_timeout(timeout), **chat_configs)
        rsp = await self._acreate(**kwargs)
        if raise_if_empty and not (self.get_choice_text(rsp) or self.get_choice_tool_calls(rsp)):
            raise LLMEmptyResponseError("The LLM's response is empty.")
        self._update_costs(getattr(rsp, "usage", None))
        return rsp

    async def acompletion(
        self,
        messages: list[dict],
        timeout=USE_CONFIG_TIMEOUT,
        raise_if_empty: bool = True,
    ):
        return await self._achat_completion(messages, timeout=self.get_timeout(timeout), raise_if_empty=raise_if_empty)

    async def _consume_stream(self, kwargs: dict) -> tuple[list[str], Any]:
        """Open a streaming request and drain it, returning (text deltas, final Message).

        Factored out so callers can bound it with ``asyncio.wait_for``. The SDK's
        own ``timeout`` is a *per-read* deadline; Anthropic sends periodic SSE
        ``ping`` keepalives during long generations, and each ping resets that
        read timer — so a stream that stays connected but emits no content deltas
        would otherwise hang here forever (observed: a request wedged ~6 min past
        the 300 s timeout until manually interrupted). A wall-clock bound in the
        caller is what actually caps that.
        """
        collected: list[str] = []
        async with self.aclient.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                log_llm_stream(text)
                collected.append(text)
            final_message = await stream.get_final_message()
        return collected, final_message

    async def _achat_completion_stream(
        self,
        messages: list[dict],
        timeout=USE_CONFIG_TIMEOUT,
        raise_if_empty: bool = True,
        **chat_configs,
    ) -> str:
        kwargs = self._cons_kwargs(messages, timeout=self.get_timeout(timeout), **chat_configs)
        deadline = self.get_timeout(timeout)
        try:
            collected, final_message = await asyncio.wait_for(self._consume_stream(kwargs), timeout=deadline)
        except asyncio.TimeoutError as e:
            # asyncio.TimeoutError is not the builtin TimeoutError before 3.11, so
            # classify_llm_error would miss it; raise the retryable type directly.
            raise LLMTimeoutError(f"Streaming response stalled: no completion within {deadline}s", cause=e)
        except Exception as e:
            raise classify_llm_error(e) or e

        log_llm_stream("\n")
        full_reply_content = "".join(collected)
        if raise_if_empty and not full_reply_content:
            raise LLMEmptyResponseError("The LLM's response is empty.")
        self._update_costs(getattr(final_message, "usage", None))
        return full_reply_content

    async def _achat_completion_stream_tool(
        self,
        messages: list[dict],
        timeout=USE_CONFIG_TIMEOUT,
        raise_if_empty: bool = True,
        **chat_configs,
    ):
        """Streaming native tool-use completion: stream text, return the full Message.

        Anthropic's streaming context assembles the complete ``Message`` (text +
        ``tool_use`` blocks) as it consumes the event stream, so we stream the text
        deltas to ``log_llm_stream`` for the live console and return the assembled
        message — letting ``get_choice_text`` / ``get_choice_tool_calls`` parse the
        tool calls exactly as in the blocking path.
        """
        kwargs = self._cons_kwargs(messages, timeout=self.get_timeout(timeout), **chat_configs)
        deadline = self.get_timeout(timeout)
        try:
            _, final_message = await asyncio.wait_for(self._consume_stream(kwargs), timeout=deadline)
        except asyncio.TimeoutError as e:
            raise LLMTimeoutError(f"Streaming response stalled: no completion within {deadline}s", cause=e)
        except Exception as e:
            raise classify_llm_error(e) or e

        log_llm_stream("\n")
        if raise_if_empty and not (self.get_choice_text(final_message) or self.get_choice_tool_calls(final_message)):
            raise LLMEmptyResponseError("The LLM's response is empty.")
        self._update_costs(getattr(final_message, "usage", None))
        return final_message

    async def acompletion_text(
        self,
        messages: list[dict],
        stream=False,
        timeout=USE_CONFIG_TIMEOUT,
        raise_if_empty: bool = True,
    ) -> str:
        if stream:
            return await self._achat_completion_stream(messages, timeout=timeout, raise_if_empty=raise_if_empty)
        rsp = await self._achat_completion(messages, timeout=self.get_timeout(timeout), raise_if_empty=raise_if_empty)
        return self.get_choice_text(rsp)

    # -- response normalization --------------------------------------------
    def get_choice_text(self, rsp) -> str:
        """Concatenate all ``text`` content blocks from an Anthropic response."""
        parts = []
        for block in getattr(rsp, "content", None) or []:
            if getattr(block, "type", None) == "text":
                parts.append(getattr(block, "text", "") or "")
        return "".join(parts)

    def get_choice_tool_calls(self, rsp) -> list[dict]:
        """Normalize Anthropic ``tool_use`` blocks to the agnostic tool-call list."""
        out: list[dict] = []
        for block in getattr(rsp, "content", None) or []:
            if getattr(block, "type", None) == "tool_use":
                args = getattr(block, "input", None)
                out.append(
                    {
                        "id": getattr(block, "id", ""),
                        "name": getattr(block, "name", ""),
                        "arguments": args if isinstance(args, dict) else {},
                    }
                )
        return out

    # -- server-side web search --------------------------------------------
    async def aweb_search(
        self,
        query: str,
        *,
        allowed_domains: Optional[list[str]] = None,
        blocked_domains: Optional[list[str]] = None,
        max_uses: int = 8,
    ) -> list[WebSearchHit]:
        """Run Anthropic's server-side ``web_search_20250305`` and return the hits.

        Issues an isolated (non-streaming) ``messages.create`` carrying the
        server tool so the API performs the search + crawl and streams back
        ``web_search_tool_result`` content blocks. We collect those blocks and
        extract ``{title, url}`` (mirroring Claude Code's ``extractSearchResults``).
        The server tool spec passes through ``_convert_tools`` untouched (it only
        rewrites OpenAI ``function`` specs).

        Raises ``NotImplementedError`` when the routed model does not support
        server-side search (e.g. an old Claude on the anthropic transport), so the
        WebSearch tool degrades cleanly instead of firing a request the API would
        reject with an opaque error the tool cannot catch.
        """
        if not supports_web_search(self.model):
            raise NotImplementedError(f"{self.model} does not support server-side web search.")

        tool_spec: dict[str, Any] = {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": max_uses,
        }
        if allowed_domains:
            tool_spec["allowed_domains"] = allowed_domains
        if blocked_domains:
            tool_spec["blocked_domains"] = blocked_domains

        messages = [
            {
                "role": "system",
                "content": "You are an assistant for performing a web search tool use",
            },
            {"role": "user", "content": f"Perform a web search for the query: {query}"},
        ]
        rsp = await self._achat_completion(
            messages,
            tools=[tool_spec],
            raise_if_empty=False,
        )
        return self._extract_web_search_hits(rsp)

    @staticmethod
    def _extract_web_search_hits(rsp: Any) -> list[WebSearchHit]:
        """Pull ``{title, url}`` out of an Anthropic ``web_search_tool_result`` response.

        Walks the response ``content`` for ``web_search_tool_result`` blocks; each
        carries a list of result items with ``title`` / ``url`` (and possibly an
        error block, which has no list content and is skipped).
        """
        hits: list[WebSearchHit] = []
        for block in getattr(rsp, "content", None) or []:
            if getattr(block, "type", None) != "web_search_tool_result":
                continue
            content = getattr(block, "content", None)
            if not isinstance(content, list):
                continue
            for item in content:
                title = getattr(item, "title", None) if not isinstance(item, dict) else item.get("title")
                url = getattr(item, "url", None) if not isinstance(item, dict) else item.get("url")
                if url:
                    hits.append(WebSearchHit(title=title or "", url=url))
        return hits

    # -- token / usage helpers ---------------------------------------------
    def _get_max_tokens(self) -> int:
        # Anthropic requires an explicit, positive max_tokens on every request.
        return self.max_completion_token or 4096

    def _calc_usage(self, messages: list[dict], rsp: str):
        if not self.config.calc_usage:
            return TokenUsage()
        try:
            prompt = count_message_tokens(messages, self.pricing_plan)
            completion = count_string_tokens(rsp, self.pricing_plan)
            return TokenUsage(
                input_tokens=prompt,
                output_tokens=completion,
                total_tokens=prompt + completion,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"usage calculation failed: {e}")
            return TokenUsage()

    def count_tokens(self, messages: list[dict]) -> int:
        try:
            return count_message_tokens(messages, self.model)
        except Exception:  # noqa: BLE001
            return super().count_tokens(messages)
