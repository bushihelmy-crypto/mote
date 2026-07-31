#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from typing import Any, Optional, Union

from openai import AsyncOpenAI
from pydantic import BaseModel

from mote.contracts.config.model.llm import LLMConfig
from mote.contracts.conversation import Message
from mote.contracts.conversation.fields import IMAGES, PDFS
from mote.contracts.model import LLMResponse, LLMToolCall, WebSearchHit
from mote.contracts.model.capabilities import supports_pdf_input, supports_vision
from mote.contracts.model.constants import LLM_API_TIMEOUT, USE_CONFIG_TIMEOUT
from mote.kernel.inference.tokenization import count_message_tokens
from mote.runtime.errors import LLMEmptyResponseError
from mote.runtime.models.cost import Costs, CostTracker, TokenUsage
from mote.runtime.models.media import build_data_url, pdfs_within_limits
from mote.runtime.models.ratelimit import RateLimitTracker
from mote.runtime.telemetry.logging import logger


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

    @abstractmethod
    def __init__(self, config: LLMConfig):
        pass

    async def aclose(self) -> None:
        """Close active and credential-rotation-retired SDK clients."""

        clients = list(getattr(self, "_retired_clients", ()))
        active = getattr(self, "aclient", None)
        if active is not None:
            clients.append(active)
        self._retired_clients = []
        self.aclient = None

        seen: set[int] = set()
        for client in reversed(clients):
            if id(client) in seen:
                continue
            seen.add(id(client))
            close = getattr(client, "close", None)
            if close is None:
                continue
            result = close()
            if inspect.isawaitable(result):
                await result

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
            return {
                "role": "user",
                "content": msg + self._unreadable_media_notice(images, pdfs),
            }
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

        return await _send(self, message)

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
        output_schema: Optional[dict] = None,
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
            tools: Provider-native tool specs (see ToolExecutor.native_tool_specs).
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
        if output_schema is not None:
            native_schema = self.native_schema_request(output_schema)
            if native_schema is None:
                raise ValueError("resolved LLM does not support native schema output")
            extra.update(native_schema)

        async def _send(llm: "BaseLLM", messages: list[dict]) -> "LLMResponse":
            if stream:
                rsp = await llm._achat_completion_stream_tool(messages, timeout=self.get_timeout(timeout), **extra)
            else:
                rsp = await llm._achat_completion(messages, timeout=self.get_timeout(timeout), **extra)
            content = llm.get_choice_text(rsp) or ""
            raw_calls = llm.get_choice_tool_calls(rsp)
            tool_calls = [
                LLMToolCall(
                    id=c.get("id", ""),
                    name=c["name"],
                    arguments=c.get("arguments") or {},
                )
                for c in raw_calls
            ]
            if not content.strip() and not tool_calls:
                raise LLMEmptyResponseError("The LLM's response is empty.")
            return LLMResponse(content=content, tool_calls=tool_calls)

        return await _send(self, message)

    def supports_native_schema_output(self) -> bool:
        return False

    def native_schema_request(self, schema: dict) -> dict | None:
        return None

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
        return await self.aask(
            ask,
            images=[image_b64],
            stream=False,
            timeout=timeout,
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

    async def acompletion_text(
        self,
        messages: list[dict],
        stream: bool = False,
        timeout: int = USE_CONFIG_TIMEOUT,
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
