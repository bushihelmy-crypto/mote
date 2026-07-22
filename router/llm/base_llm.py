#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Mapping, Optional, Union
from uuid import uuid4

from openai import AsyncOpenAI
from pydantic import BaseModel
from tenacity import after_log, retry, retry_if_exception, stop_after_attempt

from mote.common.config.config.llm_config import LLMConfig
from mote.common.const import IMAGES, LLM_API_TIMEOUT, PDFS, USE_CONFIG_TIMEOUT
from mote.common.const.llm import supports_pdf_input, supports_vision
from mote.common.events import (
    LLMErrorEvent,
    LLMRequestEvent,
    LLMResponseEvent,
    LLMRetryEvent,
    current_span_id,
    observe_event,
    observe_event_sync,
)
from mote.common.exception import (
    LLMEmptyResponseError,
    LLMResourceUnavailableError,
    LLMUnusableResponseError,
    RecoveryAction,
    RecoveryRunner,
    is_retryable,
)
from mote.common.interface import ContextReducer
from mote.common.logs import current_trace_id, logger
from mote.common.resilience import ResourceHealthRegistry
from mote.common.schema import Message
from mote.common.utils.common import build_data_url, log_and_reraise, pdfs_within_limits
from mote.common.utils.token_counter import TOKEN_MAX, count_message_tokens
from mote.router.cost import Costs, CostTracker, TokenUsage
from mote.router.llm._retry import wait_retry_after
from mote.router.llm.health import counts_as_health_failure, resource_key
from mote.router.llm.llm_response import LLMResponse, LLMToolCall, WebSearchHit
from mote.router.llm.recovery import build_llm_strategies
from mote.router.llm.transformers import DEFAULT_MESSAGE_TRANSFORMERS
from mote.router.ratelimit import RateLimitTracker

# ── LLM retry budget: two composed tiers, co-located so the total is surveyable ──
# A single LLM call may touch the wire at most
#     LLM_RETRY_ATTEMPTS * (LLM_MAX_RECOVERIES + 1)
# times. The two tiers are deliberately distinct mechanisms, not one merged loop:
#   • LLM_RETRY_ATTEMPTS — the inner tenacity tier: a *transient* (``is_retryable``)
#     failure is backed-off and re-issued IN PLACE (same resource). Shared by the
#     base ``acompletion_text`` and every provider override so the count isn't
#     silently different depending on which provider you land on.
#   • LLM_MAX_RECOVERIES — the outer ``RecoveryRunner`` tier: each *condition-
#     changing* recovery (COMPRESS / ROTATE_CREDENTIAL / FALLBACK) re-enters the
#     tenacity tier once. Bounds a pathological recover-fail-recover cycle.
# Keeping both numbers here (rather than one in base_llm and the other buried as a
# RecoveryRunner default) is the single pane of glass for "how many times can one
# aask hit the network".
LLM_RETRY_ATTEMPTS = 6
LLM_MAX_RECOVERIES = 3


class BaseLLM(ABC):
    """LLM API abstract class, requiring all inheritors to provide a series of standard capabilities"""

    config: LLMConfig
    use_system_prompt: bool = True
    system_prompt = "You are a helpful assistant."

    # OpenAI / Azure / Others
    aclient: Optional[AsyncOpenAI] = None
    cost_manager: Optional[CostTracker] = None
    # Shared, fleet-wide rate-limit state (account quota, not per-agent spend), set
    # by the router's Context alongside ``cost_manager``. The provider's response
    # hook reads it LAZILY at response time, so injecting it after the SDK client
    # is built (the normal order) is fine. ``None`` leaves rate-limit capture inert.
    rate_limit_tracker: Optional["RateLimitTracker"] = None
    # Maintain model name in own instance in case the global config has changed,
    # Should always use model not config.model within this class
    model: Optional[str] = None
    max_completion_token: int = 4096
    pricing_plan: Optional[str] = None
    # Injected by the upper layer (Role, via the router) to enable COMPRESS
    # recovery: a boundary-safe reducer that shrinks the outgoing wire payload
    # when a context-overflow error survives the transient-retry budget. ``None``
    # means "no reducer wired" and COMPRESS degrades to a re-raise.
    context_reducer: Optional[ContextReducer] = None
    # Injected by the upper layer (e.g. LLMRouter) to enable FALLBACK recovery:
    # a no-arg supplier returning the next provider to fail over to, or None.
    _fallback_supplier: Optional[Callable[[], Optional["BaseLLM"]]] = None
    # Injection point for the "transform the request, then retry" recovery family
    # (SHRINK_IMAGE / DOWNGRADE_TOOL_CONTENT / STRIP_REQUEST_STATE). Maps a
    # RecoveryAction to an ``async (messages, exc) -> messages | None`` transformer.
    # Defaults to the built-in repairs (shrink oversized images, downgrade list-type
    # tool content, strip opaque request state); an upper layer may override per
    # provider. A transformer that returns None leaves that recovery a re-raise.
    _message_transformers: Optional[Mapping[RecoveryAction, Callable]] = DEFAULT_MESSAGE_TRANSFORMERS
    # Injected by the upper layer (LLMRouter) to enable circuit-breaking: the
    # shared per-resource :class:`ResourceHealthRegistry`. When set, each call is
    # gated by the resource's breaker (an OPEN breaker sheds the call → FALLBACK)
    # and its outcome recorded. ``None`` (the default for a directly-constructed
    # provider / tests) leaves the path inert — behaviourally identical to today.
    _health_registry: "Optional[ResourceHealthRegistry]" = None
    # Injected by the upper layer (LLMRouter) to enable RESPONSE-based fallback:
    # a validator ``(result) -> Optional[str]`` run AFTER a successful ``send()``.
    # Returning a non-empty rejection reason means the HTTP-200 response is unusable
    # (a refusal / empty body / wrong shape); the caller raises a FALLBACK-classified
    # :class:`LLMUnusableResponseError` so the recovery loop sheds to another
    # provider. ``None`` (the default) leaves the path inert — every send passes.
    _response_validator: Optional[Callable[[Any], Optional[str]]] = None

    @abstractmethod
    def __init__(self, config: LLMConfig):
        pass

    def _user_msg(
        self,
        msg: str,
        images: Optional[Union[str, list[str]]] = None,
        pdfs: Optional[Union[str, list[str]]] = None,
    ) -> dict[str, Union[str, dict]]:
        if images or pdfs:
            if self.support_image_input():
                return self._user_msg_with_media(msg, images=images, pdfs=pdfs)
            # Non-vision model: the attachments cannot ride the wire. Rather than
            # silently drop them (which leaves the model reading text like
            # "Shown below." with nothing below), tell it plainly they were
            # withheld so it doesn't hallucinate having seen them.
            return {"role": "user", "content": msg + self._unreadable_media_notice(images, pdfs)}
        return {"role": "user", "content": msg}

    @staticmethod
    def _unreadable_media_notice(
        images: Optional[Union[str, list[str]]],
        pdfs: Optional[Union[str, list[str]]],
    ) -> str:
        """Honest notice appended when a non-vision model is handed attachments.

        The current model cannot read images (``supports_vision`` is False), so
        the media never reaches it. Announce that plainly instead of letting the
        model believe an image was shown.
        """
        n_img = 1 if isinstance(images, str) else len(images or [])
        n_pdf = 1 if isinstance(pdfs, str) else len(pdfs or [])
        parts: list[str] = []
        if n_img:
            parts.append(f"{n_img} image{'s' if n_img > 1 else ''}")
        if n_pdf:
            parts.append(f"{n_pdf} PDF{'s' if n_pdf > 1 else ''}")
        what = " and ".join(parts) if parts else "attachment(s)"
        return f"\n\n[{what} attached but the current model cannot read images; the attachment was not shown.]"

    def _user_msg_with_media(
        self,
        msg: str,
        images: Optional[Union[str, list[str]]] = None,
        pdfs: Optional[Union[str, list[str]]] = None,
    ):
        """Compose a multimodal user message with optional images and PDFs.

        images can be http(s) urls or base64 strings; pdfs should be base64 strings.
        """
        if isinstance(images, str):
            images = [images]
        if isinstance(pdfs, str):
            pdfs = [pdfs]
        # Guardrail for PDFs using PyMuPDF: skip attaching if total size > 15MB or total pages > 80
        if pdfs:
            ok_to_attach, total_pdf_bytes, total_pdf_pages = pdfs_within_limits(pdfs)
            if not ok_to_attach:
                pdfs = []
        content: list[dict[str, Any]] = [{"type": "text", "text": msg}]
        # images
        for image in images or []:
            if isinstance(image, str) and image.startswith("http"):
                url = image
            else:
                # Raw base64: build_data_url resolves the correct media type by
                # sniffing the bytes (a declared type is often wrong and
                # Bedrock/Anthropic reject mismatches).
                url = build_data_url(image)
            content.append({"type": "image_url", "image_url": {"url": url}})
        # pdfs (Anthropic-compatible document input)
        if supports_pdf_input(self.model):
            for pdf_b64 in pdfs or []:
                content.append(
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64,
                        },
                        "citations": {"enabled": True},
                        # "cache_control": {"type": "ephemeral"}
                    }
                )
        return {"role": "user", "content": content}

    def _assistant_msg(self, msg: str) -> dict[str, str]:
        return {"role": "assistant", "content": msg}

    def _system_msg(self, msg: str) -> dict[str, str]:
        return {"role": "system", "content": msg}

    def system_role(self) -> str:
        """The role name used for system messages (public accessor for collaborators)."""
        return self._system_msg("")["role"]

    def support_image_input(self) -> bool:
        return supports_vision(self.model)

    def format_msg(self, messages: Union[str, "Message", list[dict], list["Message"], list[str]]) -> list[dict]:
        """convert messages to list[dict]."""
        if not isinstance(messages, list):
            messages = [messages]

        processed_messages = []
        for msg in messages:
            if isinstance(msg, str):
                processed_messages.append({"role": "user", "content": msg})
            elif isinstance(msg, dict):
                assert (
                    "role" in msg and "content" in msg
                ), f"dict message must have 'role' and 'content', got keys: {list(msg.keys())}"
                processed_messages.append(msg)
            elif isinstance(msg, Message):
                images = msg.metadata.get(IMAGES)
                pdfs = msg.metadata.get(PDFS)
                processed_msg = (
                    self._user_msg(msg=msg.content, images=images, pdfs=pdfs) if (images or pdfs) else msg.to_dict()
                )
                processed_messages.append(processed_msg)
            else:
                raise ValueError(
                    f"Only support message type are: str, Message, dict, but got {type(messages).__name__}!"
                )
        return processed_messages

    def _system_msgs(self, msgs: list[str]) -> list[dict[str, str]]:
        return [self._system_msg(msg) for msg in msgs]

    def _default_system_msg(self):
        return self._system_msg(self.system_prompt)

    def _update_costs(
        self,
        usage: Union[dict, BaseModel, None],
        model: Optional[str] = None,
        local_calc_usage: bool = True,
    ):
        """update each request's token cost
        Args:
            model (str): model name or in some scenarios called endpoint
            local_calc_usage (bool): some models don't calculate usage, it will overwrite LLMConfig.calc_usage
        """
        if usage is None:
            return
        calc_usage = self.config.calc_usage and local_calc_usage
        model = model or self.pricing_plan
        model = model or self.model
        if calc_usage and self.cost_manager:
            try:
                # Normalize any provider usage shape (incl. nested cache/reasoning
                # token details) into the unified TokenUsage before recording.
                token_usage = TokenUsage.from_usage(usage)
                self.cost_manager.add(token_usage, model)
            except Exception as e:
                logger.error(f"{self.__class__.__name__} updates costs failed! exp: {e}")

    def get_costs(self) -> Costs:
        if not self.cost_manager:
            return Costs.zero()
        return self.cost_manager.get_costs()

    @property
    def provider_label(self) -> str:
        """Human-facing provider name for rate-limit keying (e.g. ``anthropic``).

        The configured ``api_type`` value — the same identity the cost/health
        layers key on — so a rate-limit snapshot reads under the provider the
        user recognizes. Falls back to ``unknown`` when unset.
        """
        api_type = getattr(getattr(self, "config", None), "api_type", "")
        return getattr(api_type, "value", api_type) or "unknown"

    def _build_messages(
        self,
        msg: Union[str, list[dict[str, str]], list["Message"]],
        system_msgs: Optional[list[str]] = None,
        format_msgs: Optional[list[dict[str, str]]] = None,
        images: Optional[Union[str, list[str]]] = None,
        pdfs: Optional[Union[str, list[str]]] = None,
    ) -> list[dict]:
        """Assemble the message list (system + format + user) for a completion.

        Shared by aask (text channel) and aask_tool (native tool-use channel) so
        both build the conversation identically; only the completion call differs.
        """
        message: list[dict]
        if system_msgs:
            message = self._system_msgs(system_msgs)
        else:
            message = [self._default_system_msg()]
        if not self.use_system_prompt:
            message = []
        if format_msgs:
            message.extend(format_msgs)
        if isinstance(msg, str):
            message.append(self._user_msg(msg, images=images, pdfs=pdfs))
        else:
            message.extend(self.format_msg(msg))
        return message

    async def aask(
        self,
        msg: Union[str, list[dict[str, str]], list["Message"]],
        system_msgs: Optional[list[str]] = None,
        format_msgs: Optional[list[dict[str, str]]] = None,
        images: Optional[Union[str, list[str]]] = None,
        pdfs: Optional[Union[str, list[str]]] = None,
        timeout=USE_CONFIG_TIMEOUT,
        stream=True,
    ) -> str:
        message = self._build_messages(msg, system_msgs, format_msgs, images, pdfs)

        async def _send(llm: "BaseLLM", messages: list[dict]) -> str:
            return await llm.acompletion_text(messages, stream=stream, timeout=self.get_timeout(timeout))

        return await self._run_with_recovery(_send, message)

    async def aask_tool(
        self,
        msg: Union[str, list[dict[str, str]], list["Message"]],
        system_msgs: Optional[list[str]] = None,
        format_msgs: Optional[list[dict[str, str]]] = None,
        images: Optional[Union[str, list[str]]] = None,
        pdfs: Optional[Union[str, list[str]]] = None,
        tools: Optional[list[dict]] = None,
        tool_choice: Optional[Union[str, dict]] = None,
        timeout=USE_CONFIG_TIMEOUT,
        stream: bool = True,
    ) -> "LLMResponse":
        """Native tool-use counterpart to aask: returns text + structured tool calls.

        Unlike aask (which returns a plain str the XML protocol parses), this
        passes ``tools`` (provider-native specs) to the API and normalizes the
        completion into an LLMResponse carrying both the assistant text and any
        structured tool calls. When ``stream`` is set (the default, mirroring
        aask) the assistant text is streamed token-by-token via ``log_llm_stream``
        while the structured tool calls are accumulated, then the completed
        response is normalized — so the live console sees output as it arrives.

        Args:
            tools: Provider-native tool specs (see ToolExecutor.get_native_tool_specs).
            tool_choice: Provider tool_choice directive ("auto"/"required"/dict).
            stream: Stream assistant text live (default True). Providers without a
                streaming tool path transparently fall back to a blocking call.
        """
        message = self._build_messages(msg, system_msgs, format_msgs, images, pdfs)

        extra: dict = {}
        if tools:
            extra["tools"] = tools
        if tool_choice is not None:
            extra["tool_choice"] = tool_choice
        # A tool-only completion has empty text content, which is valid here —
        # disable the empty-response guard so it is not mistaken for a failure.
        extra["raise_if_empty"] = False

        async def _send(llm: "BaseLLM", messages: list[dict]) -> "LLMResponse":
            if stream:
                rsp = await llm._achat_completion_stream_tool(messages, timeout=self.get_timeout(timeout), **extra)
            else:
                rsp = await llm._achat_completion(messages, timeout=self.get_timeout(timeout), **extra)
            content = llm.get_choice_text(rsp) or ""
            raw_calls = llm.get_choice_tool_calls(rsp)
            tool_calls = [
                LLMToolCall(id=c.get("id", ""), name=c["name"], arguments=c.get("arguments") or {}) for c in raw_calls
            ]
            if not content.strip() and not tool_calls:
                raise LLMEmptyResponseError("The LLM's response is empty.")
            return LLMResponse(content=content, tool_calls=tool_calls)

        return await self._run_with_recovery(_send, message)

    async def aweb_search(
        self,
        query: str,
        *,
        allowed_domains: Optional[list[str]] = None,
        blocked_domains: Optional[list[str]] = None,
        max_uses: int = 8,
    ) -> list[WebSearchHit]:
        """Run a provider-native server-side web search and return the hits.

        This is the single seam behind the ``WebSearch`` tool: it issues an
        ISOLATED secondary request carrying the provider's server-side web-search
        tool (Anthropic ``web_search_20250305`` / OpenAI Responses ``web_search``)
        — the API server performs the actual search + crawl and returns structured
        result blocks, which the provider normalizes into :class:`WebSearchHit`.
        It is not an agent loop: one request, one parse, no tool dispatch.

        Args:
            query: The natural-language search query.
            allowed_domains: If set, restrict results to these domains.
            blocked_domains: If set, exclude results from these domains.
            max_uses: Cap on how many searches the API may run for this request.

        Returns:
            A list of :class:`WebSearchHit` (may be empty).

        Raises:
            NotImplementedError: The provider has no server-side web search
                (e.g. Chat Completions / third-party endpoints). The tool catches
                this and degrades to a "search unavailable" notice.
        """
        raise NotImplementedError("This provider does not support server-side web search.")

    async def adescribe_image(
        self,
        image_b64: str,
        *,
        prompt: str = "",
        timeout=USE_CONFIG_TIMEOUT,
    ) -> str:
        """Describe an image as text via an isolated multimodal completion.

        The single seam behind ``WebBrowser``'s ``read_image`` action: it feeds
        one image to a vision-capable model and returns the model's textual
        reading of it. The browser has no wire to hand an in-page ``<img>`` to
        the main model as media, so the image is routed to a vision task model
        instead and its content reaches the agent as text. Unlike ``aweb_search``
        there is no provider-specific
        server tool: any vision provider handles this through the ordinary
        multimodal user message, so it lives once here on the base class, gated
        only on model capability.

        Args:
            image_b64: The image bytes, base64-encoded (no data-uri prefix).
            prompt: Optional extra instruction steering what to extract/describe;
                falls back to a general "describe this image" ask when empty.

        Returns:
            The model's textual description of the image.

        Raises:
            NotImplementedError: The routed model is not vision-capable, so it
                cannot read the image at all. The tool catches this and degrades
                to a clear "image understanding unavailable" notice.
        """
        if not self.support_image_input():
            raise NotImplementedError(f"{self.model} does not support image input.")
        ask = prompt.strip() or (
            "Describe this image in detail. Transcribe any text verbatim, and note "
            "any diagrams, charts, tables or UI elements and their content."
        )
        return await self.aask(ask, images=[image_b64], stream=False, timeout=timeout)

    def rotate_credential(self) -> bool:
        """Rotate to the next configured credential, rebuilding the client.

        Consumed by the recovery loop on ROTATE_CREDENTIAL (auth/billing errors).
        Default: no rotation available (single key / unsupported provider) → False.
        Providers backed by a multi-key ``LLMConfig.api_key`` override this.
        """
        return False

    async def _run_with_recovery(self, send, message: list[dict]):
        """Run ``send(llm, messages)`` under the recovery loop — the single LLM-call chokepoint.

        ``send`` takes the active provider (``self`` normally, or the fallback after a
        FALLBACK) plus the (possibly re-compressed) messages. Transient RETRY is owned by
        the tenacity ``@retry`` wrapping ``_call`` below (the same policy as
        ``acompletion_text``): an ``is_retryable`` error is backed-off and re-issued in
        place, each attempt tracing independently under a fresh ``request_id``. Only once
        that budget is exhausted (or the error is non-transient) does it surface to this
        loop, which owns the "change conditions then retry" strategies:

        - COMPRESS — re-compress on context overflow,
        - ROTATE_CREDENTIAL — advance to the next API key on the active provider,
        - FALLBACK — swap to the provider from the injected ``_fallback_supplier``.

        Each callback degrades to a no-op when unavailable (no extra key / no fallback
        supplier), so with the default single-key, single-model config this is
        behaviourally equivalent to calling ``send(self, messages)`` directly.

        The two retry tiers compose to a bounded worst case of
        ``LLM_RETRY_ATTEMPTS * (LLM_MAX_RECOVERIES + 1)`` wire attempts (see the
        budget constants at module top). Each wire attempt is additionally
        admission-gated + outcome-recorded by :meth:`_resource_health` when a
        circuit-breaker registry is wired.
        """
        # Shared request state the strategy closures mutate across recovery attempts:
        # ``messages`` (re-compressed / repaired) and ``llm`` (swapped on FALLBACK).
        state: dict[str, Any] = {"messages": message, "llm": self}

        def _active() -> "BaseLLM":
            return state["llm"]

        async def _compress() -> bool:
            # Context overflow survived the retry budget: hand the outgoing wire
            # payload to the injected boundary-safe reducer (HARD fold→summarize→drop)
            # and re-issue. ``None`` slot (no reducer wired) or nothing-freed → return
            # False so the loop re-raises instead of spinning on an identical body.
            # The reducer's summarize step issues its own inner aask(), but that
            # runs on the router's reducer-less COMPRESSION instance (see
            # LLMRouter._build LLMVariant.COMPRESSION), so it lands here with
            # context_reducer=None and returns False — no re-entrant recursion.
            reducer = self.context_reducer
            if reducer is None:
                return False
            target = int(TOKEN_MAX.get(self.model or "", 128000) * 0.8)
            reduced = await reducer.reduce(state["messages"], target_tokens=target)
            if reduced is None:
                return False
            state["messages"] = reduced
            return True

        def _rotate() -> bool:
            return _active().rotate_credential()

        def _on_fallback(provider: "BaseLLM") -> None:
            state["llm"] = provider

        def _wrap_transformer(transform):
            # Adapt an ``(messages, exc) -> messages | None`` transformer into a
            # recovery strategy ``(exc) -> bool``: persist the repaired messages into
            # ``state`` so the next ``_call`` issues the transformed payload.
            async def _strategy(exc) -> bool:
                repaired = await transform(state["messages"], exc)
                if repaired is None:
                    return False
                state["messages"] = repaired
                return True

            return _strategy

        transformers = {
            action: _wrap_transformer(transform) for action, transform in (self._message_transformers or {}).items()
        }

        strategies = build_llm_strategies(
            compress=_compress,
            rotate=_rotate,
            fallback=self._fallback_supplier,
            on_fallback=_on_fallback,
            transformers=transformers or None,
        )
        runner = RecoveryRunner(strategies, max_recoveries=LLM_MAX_RECOVERIES)

        # The transient-RETRY tier: an ``is_retryable`` failure (transport hiccup, 5xx,
        # rate-limit, or a relay gateway middling an upstream 401/429 as an overloaded
        # marker) is backed-off and re-issued in place — landing on a healthy gateway
        # channel on the next attempt rather than surfacing to the recovery loop, which
        # has no RETRY strategy and would ``give_up``. Same policy as ``acompletion_text``
        # so the retry budget is identical regardless of which call path (text vs native
        # tool-use) a caller lands on. Non-transient errors (auth/billing/context/…) are
        # passed straight through to the recovery loop's condition-changing strategies.
        def _before_sleep(retry_state) -> None:
            # tenacity fires this ONLY when it is about to sleep+re-issue (the
            # error was retryable and the budget isn't exhausted) — so it maps
            # 1:1 to the transient "retrying in Ns" state. The final,
            # budget-exhausted failure raises without a ``before_sleep`` and is
            # surfaced by the turn-level error path instead. Sync, fire-and-forget
            # (same spine path as streaming deltas), no-op when no bus is bound.
            outcome = getattr(retry_state, "outcome", None)
            exc = outcome.exception() if outcome is not None else None
            next_action = getattr(retry_state, "next_action", None)
            observe_event_sync(
                LLMRetryEvent(
                    model=_active().model or "unknown",
                    attempt=retry_state.attempt_number,
                    max_attempts=LLM_RETRY_ATTEMPTS,
                    delay_ms=getattr(next_action, "sleep", 0.0) * 1000.0,
                    error_type=type(exc).__name__ if exc is not None else "",
                    error=str(exc) if exc is not None else "",
                    trace_id=current_trace_id() or "",
                )
            )

        @retry(
            stop=stop_after_attempt(LLM_RETRY_ATTEMPTS),
            wait=wait_retry_after(),
            after=after_log(logger, logger.level("WARNING").name),  # type: ignore[arg-type]  # loguru logger + str level vs tenacity stdlib-logging stub
            retry=retry_if_exception(is_retryable),
            retry_error_callback=log_and_reraise,
            before_sleep=_before_sleep,
        )
        async def _call():
            llm = _active()
            msgs = state["messages"]
            # The circuit breaker is ONE cohesive unit — admission gate on entry,
            # outcome recording on exit — owned by ``_resource_health`` rather than
            # smeared through this call. Tracing (request/response/error spine
            # events) is a separate concern, kept inline within the guarded body.
            async with self._resource_health(llm):
                # Open the LLM-call observation on the shared event spine. One
                # request → response|error pair per recovery attempt (so retries /
                # rotations / fallbacks each trace independently), correlated by
                # ``request_id``. ``observe_event`` is a no-op when no bus is bound
                # (standalone client use / tests), so this stays zero-cost there.
                request_id = uuid4().hex
                model = llm.model or "unknown"
                await observe_event(
                    LLMRequestEvent(
                        request_id=request_id,
                        model=model,
                        provider=self._provider_label(llm),
                        messages=msgs,
                        parent_span_id=current_span_id(),
                        trace_id=current_trace_id() or "",
                    )
                )
                started = time.monotonic()
                try:
                    result = await send(llm, msgs)
                except Exception as exc:  # noqa: BLE001 — mirror the failure, then re-raise
                    await observe_event(
                        LLMErrorEvent(
                            request_id=request_id,
                            model=model,
                            error_type=type(exc).__name__,
                            error=str(exc),
                            latency_ms=(time.monotonic() - started) * 1000.0,
                        )
                    )
                    raise
                await observe_event(
                    self._build_response_event(request_id, llm, result, (time.monotonic() - started) * 1000.0)
                )
            # Outside ``_resource_health``: the wire call succeeded, so the resource
            # is already recorded HEALTHY. A response-CONTENT rejection is a separate
            # concern — the model answered, it's just unusable — so it must NOT impugn
            # the resource's health; it sheds via FALLBACK. Being a NonRetryableError,
            # LLMUnusableResponseError bypasses the inner transient-retry loop and goes
            # straight to the outer RecoveryRunner.
            if self._response_validator is not None:
                reason = self._response_validator(result)
                if reason:
                    raise LLMUnusableResponseError(reason)
            return result

        return await runner.run(_call)

    @asynccontextmanager
    async def _resource_health(self, llm: "BaseLLM") -> AsyncIterator[None]:
        """Circuit-breaker admission gate + outcome recording around one wire attempt.

        The single cohesive home for this provider's resilience touchpoints (was
        three raw ``admit`` / ``record`` statements smeared through ``_call``),
        levelled to the same abstraction as the recovery-strategy registry:

        - on entry, when a health registry is wired, SHED a call to a resource
          whose breaker is OPEN *before* touching the wire by raising
          :class:`LLMResourceUnavailableError` (FALLBACK-classified, so the
          recovery loop swaps to another provider whose own breaker is then gated
          in turn — a sustained outage sheds to a healthy resource instead of
          hammering the dead one);
        - on exit, record the outcome: success records health; a RESOURCE-health
          failure (transient / credential, per :func:`counts_as_health_failure`)
          is recorded so a sustained outage trips the breaker, while an our-fault
          error (context overflow, 400, content-policy) is NOT recorded (it would
          wrongly shed a healthy provider).

        Fully inert (no gate, no record) when no registry is wired — the default,
        so this is behaviourally transparent for the single-model config.
        """
        registry = self._health_registry
        if registry is None:
            yield
            return
        key = resource_key(llm)
        if not registry.admit(key):
            raise LLMResourceUnavailableError(f"circuit breaker open for {key}; failing over")
        try:
            yield
        except Exception as exc:  # noqa: BLE001 — record health, then re-raise
            if counts_as_health_failure(exc):
                registry.record(key, False)
            raise
        else:
            registry.record(key, True)

    @staticmethod
    def _provider_label(llm: "BaseLLM") -> str:
        """Best-effort wire-protocol label (``api_type`` value) for tracing.

        Delegates to the canonical :attr:`provider_label` so there is one source
        of truth for "what provider am I" across the tracing and rate-limit paths.
        """
        try:
            return llm.provider_label
        except Exception:  # noqa: BLE001
            return "unknown"

    @staticmethod
    def _build_response_event(request_id: str, llm: "BaseLLM", result, latency_ms: float) -> LLMResponseEvent:
        """Build an :class:`LLMResponseEvent` from a completed call.

        Pulls this call's token usage + USD cost off the cost tracker (set by
        the provider's ``_update_costs``) so a subscriber can persist or mirror
        per-request token/cost. Tolerant of both the text (``str``) and native
        tool-use (:class:`LLMResponse`) result shapes.
        """
        content = ""
        tool_calls: list = []
        if isinstance(result, LLMResponse):
            content = result.content or ""
            tool_calls = [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in result.tool_calls]
        elif isinstance(result, str):
            content = result
        usage = None
        cost = 0.0
        cm = llm.cost_manager
        if cm is not None and cm.last_usage is not None and not cm.last_usage.is_zero():
            usage = cm.last_usage.to_dict()
            cost = getattr(cm, "last_cost", 0.0)
        return LLMResponseEvent(
            request_id=request_id,
            model=llm.model or "unknown",
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            cost_usd=cost,
            latency_ms=latency_ms,
        )

    @abstractmethod
    async def _achat_completion(self, messages: list[dict], timeout=USE_CONFIG_TIMEOUT, **kwargs) -> dict:
        """_achat_completion implemented by inherited class.

        ``**kwargs`` carries provider chat params (e.g. ``tools``/``tool_choice``
        for native tool-use) straight through to the request. Each provider
        subclass (``OpenAILLM`` via the OpenAI-compatible client, ``AnthropicLLM``
        via the native Anthropic SDK) implements this and forwards the native
        specs into its own request builder.
        """

    @abstractmethod
    async def acompletion(self, messages: list[dict], timeout=USE_CONFIG_TIMEOUT):
        """Asynchronous version of completion
        All GPTAPIs are required to provide the standard OpenAI completion interface
        [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "hello, show me python hello world code"},
            # {"role": "assistant", "content": ...}, # If there is an answer in the history, also include it
        ]
        """

    @abstractmethod
    async def _achat_completion_stream(self, messages: list[dict], timeout: int = USE_CONFIG_TIMEOUT) -> str:
        """_achat_completion_stream implemented by inherited class"""

    async def _achat_completion_stream_tool(
        self, messages: list[dict], timeout=USE_CONFIG_TIMEOUT, **chat_configs
    ) -> dict:
        """Streaming counterpart to ``_achat_completion`` for the native tool channel.

        Streams the assistant text via ``log_llm_stream`` while accumulating any
        structured tool calls, then returns the *same* provider response object
        ``_achat_completion`` would — so ``get_choice_text`` / ``get_choice_tool_calls``
        normalize it unchanged. The default implementation has no streaming tool
        path and falls back to the blocking call; providers that can stream tool
        calls override it.
        """
        return await self._achat_completion(messages, timeout=self.get_timeout(timeout), **chat_configs)

    @retry(
        stop=stop_after_attempt(LLM_RETRY_ATTEMPTS),
        wait=wait_retry_after(),
        after=after_log(logger, logger.level("WARNING").name),  # type: ignore[arg-type]  # loguru logger + str level vs tenacity stdlib-logging stub
        retry=retry_if_exception(is_retryable),
        retry_error_callback=log_and_reraise,
    )
    async def acompletion_text(
        self, messages: list[dict], stream: bool = False, timeout: int = USE_CONFIG_TIMEOUT
    ) -> str:
        """Asynchronous version of completion. Return str. Support stream-print"""
        if stream:
            return await self._achat_completion_stream(messages, timeout=self.get_timeout(timeout))
        resp = await self._achat_completion(messages, timeout=self.get_timeout(timeout))
        return self.get_choice_text(resp)

    def get_choice_text(self, rsp: dict) -> str:
        """Required to provide the first text of choice"""
        return rsp["choices"][0]["message"]["content"] or ""

    def get_choice_delta_text(self, rsp: dict) -> str:
        """Required to provide the first text of stream choice"""
        return rsp.get("choices", [{}])[0].get("delta", {}).get("content", "")

    def get_choice_tool_calls(self, rsp) -> list[dict]:
        """Normalize all tool calls in a completion to a provider-agnostic list.

        Concrete providers return a list of ``{"id", "name", "arguments"}`` (with
        ``arguments`` an already-parsed dict) by parsing their own wire shape —
        OpenAI chat-completion objects, Anthropic content blocks, etc. The base
        has no wire format of its own, so it returns [] (no tool calls), letting
        the native channel fall back to text handling. Providers MUST override.
        """
        return []

    def messages_to_prompt(self, messages: list[dict]):
        """[{"role": "user", "content": msg}] to user: <msg> etc."""
        return "\n".join([f"{i['role']}: {i['content']}" for i in messages])

    def messages_to_dict(self, messages):
        """objects to [{"role": "user", "content": msg}] etc."""
        return [i.to_dict() for i in messages]

    def with_model(self, model: str):
        """Set model and return self. For example, `with_model("gpt-3.5-turbo")`."""
        self.model = model
        return self

    def get_timeout(self, timeout: int) -> int:
        return timeout or self.config.timeout or LLM_API_TIMEOUT

    def count_tokens(self, messages: list[dict]) -> int:
        """count the tokens of the messages
        Using OpenAI's token calculation method (tiktoken) as default for OpenAI models
        For non-OpenAI models, using a basic approximation (0.5 * character count)
        """
        # A very raw heuristic to count tokens, taking reference from:
        # https://help.openai.com/en/articles/4936856-what-are-tokens-and-how-to-count-them
        # https://platform.deepseek.com/api-docs/#token--token-usage
        # The heuristics is a huge overestimate for English text, e.g., and should be overwrittem with accurate token count function in inherited class
        # logger.warning("Base count_tokens is not accurate and should be overwritten.")

        return count_message_tokens(messages, self.model)
        # for non-OpenAI models
        # return sum([int(len(msg["content"]) * 0.5) for msg in messages])
