#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2023/5/5 23:04
@Author  : alexanderwu
@File    : base_llm.py
@Desc    : mashenquan, 2023/8/22. + try catch
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Callable, Mapping, Optional, Union

from openai import AsyncOpenAI
from pydantic import BaseModel
from tenacity import (
    after_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

from metagpt.common.config.compress_msg_config import CompressType
from metagpt.common.config.llm_config import LLMConfig
from metagpt.common.const import IMAGES, LLM_API_TIMEOUT, PDFS, USE_CONFIG_TIMEOUT
from metagpt.common.exception import RecoveryAction, is_retryable
from metagpt.common.logs import logger
from metagpt.router.llm.constant import MULTI_MODAL_MODELS
from metagpt.router.llm.recovery import RecoveryRunner
from metagpt.router.llm.transformers import DEFAULT_MESSAGE_TRANSFORMERS
from metagpt.router.llm.request_context_builder import RequestContextBuilder
from metagpt.common.utils.common import log_and_reraise, pdfs_within_limits
from metagpt.common.utils.cost_manager import CostManager, Costs
from metagpt.common.utils.token_counter import count_message_tokens


class BaseLLM(ABC):
    """LLM API abstract class, requiring all inheritors to provide a series of standard capabilities"""

    config: LLMConfig
    use_system_prompt: bool = True
    system_prompt = "You are a helpful assistant."

    # OpenAI / Azure / Others
    aclient: Optional[Union[AsyncOpenAI]] = None
    cost_manager: Optional[CostManager] = None
    # Maintain model name in own instance in case the global config has changed,
    # Should always use model not config.model within this class
    model: Optional[str] = None
    max_completion_token: int = 4096
    pricing_plan: Optional[str] = None
    _request_context_builder: Optional[RequestContextBuilder] = None
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

    @abstractmethod
    def __init__(self, config: LLMConfig):
        pass

    def _user_msg(
        self,
        msg: str,
        images: Optional[Union[str, list[str]]] = None,
        pdfs: Optional[Union[str, list[str]]] = None,
    ) -> dict[str, Union[str, dict]]:
        if (images or pdfs) and self.support_image_input():
            return self._user_msg_with_media(msg, images=images, pdfs=pdfs)
        return {"role": "user", "content": msg}

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
        content = [{"type": "text", "text": msg}]
        # images
        for image in images or []:
            url = image if isinstance(image, str) and image.startswith("http") else f"data:image/jpeg;base64,{image}"
            content.append({"type": "image_url", "image_url": {"url": url}})
        # pdfs (Anthropic-compatible document input)
        is_anthropic_pdf_supported = "claude" in self.model.lower()
        if is_anthropic_pdf_supported:
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

    def support_image_input(self) -> bool:
        return any([m in self.model for m in MULTI_MODAL_MODELS])

    def format_msg(self, messages: Union[str, "Message", list[dict], list["Message"], list[str]]) -> list[dict]:
        """convert messages to list[dict]."""
        from metagpt.common.schema import Message

        if not isinstance(messages, list):
            messages = [messages]

        processed_messages = []
        for msg in messages:
            if isinstance(msg, str):
                processed_messages.append({"role": "user", "content": msg})
            elif isinstance(msg, dict):
                assert set(msg.keys()) == set(["role", "content"])
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

    def _update_costs(self, usage: Union[dict, BaseModel], model: str = None, local_calc_usage: bool = True):
        """update each request's token cost
        Args:
            model (str): model name or in some scenarios called endpoint
            local_calc_usage (bool): some models don't calculate usage, it will overwrite LLMConfig.calc_usage
        """
        calc_usage = self.config.calc_usage and local_calc_usage
        model = model or self.pricing_plan
        model = model or self.model
        usage = usage.model_dump() if isinstance(usage, BaseModel) else usage
        if calc_usage and self.cost_manager:
            try:
                prompt_tokens = int(usage.get("prompt_tokens", 0))
                completion_tokens = int(usage.get("completion_tokens", 0))
                self.cost_manager.update_cost(prompt_tokens, completion_tokens, model)
            except Exception as e:
                logger.error(f"{self.__class__.__name__} updates costs failed! exp: {e}")

    def get_costs(self) -> Costs:
        if not self.cost_manager:
            return Costs(0, 0, 0, 0)
        return self.cost_manager.get_costs()

    @property
    def request_context_builder(self) -> RequestContextBuilder:
        if self._request_context_builder is None:
            self._request_context_builder = RequestContextBuilder(self)
        return self._request_context_builder

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
        compressed_message = self.compress_messages(message, compress_type=self.config.compress_type)

        async def _send(llm: "BaseLLM", messages: list[dict]) -> str:
            return await llm.acompletion_text(messages, stream=stream, timeout=self.get_timeout(timeout))

        return await self._run_with_recovery(_send, compressed_message)

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
    ) -> "LLMResponse":
        """Native tool-use counterpart to aask: returns text + structured tool calls.

        Unlike aask (which returns a plain str the XML protocol parses), this
        passes ``tools`` (provider-native specs) to the API and normalizes the
        completion into an LLMResponse carrying both the assistant text and any
        structured tool calls. Non-streaming: native tool calls are only well
        formed once the completion is complete.

        Args:
            tools: Provider-native tool specs (see ToolExecutor.get_native_tool_specs).
            tool_choice: Provider tool_choice directive ("auto"/"required"/dict).
        """
        from metagpt.router.llm.llm_response import LLMResponse, LLMToolCall

        message = self._build_messages(msg, system_msgs, format_msgs, images, pdfs)
        compressed_message = self.compress_messages(message, compress_type=self.config.compress_type)

        extra: dict = {}
        if tools:
            extra["tools"] = tools
        if tool_choice is not None:
            extra["tool_choice"] = tool_choice
        # A tool-only completion has empty text content, which is valid here —
        # disable the empty-response guard so it is not mistaken for a failure.
        extra["raise_if_empty"] = False

        async def _send(llm: "BaseLLM", messages: list[dict]) -> "LLMResponse":
            rsp = await llm._achat_completion(messages, timeout=self.get_timeout(timeout), **extra)
            content = llm.get_choice_text(rsp) or ""
            raw_calls = llm.get_choice_tool_calls(rsp)
            tool_calls = [
                LLMToolCall(id=c.get("id", ""), name=c["name"], arguments=c.get("arguments") or {})
                for c in raw_calls
            ]
            return LLMResponse(content=content, tool_calls=tool_calls)

        return await self._run_with_recovery(_send, compressed_message)

    def rotate_credential(self) -> bool:
        """Rotate to the next configured credential, rebuilding the client.

        Consumed by the recovery loop on ROTATE_CREDENTIAL (auth/billing errors).
        Default: no rotation available (single key / unsupported provider) → False.
        Providers backed by a multi-key ``LLMConfig.api_key`` override this.
        """
        return False

    async def _run_with_recovery(self, send, compressed_message: list[dict]):
        """Run ``send(llm, messages)`` under the recovery loop — the single LLM-call chokepoint.

        ``send`` takes the active provider (``self`` normally, or the fallback after a
        FALLBACK) plus the (possibly re-compressed) messages. Transient RETRY stays with
        the tenacity ``@retry`` inside ``acompletion_text``; this loop owns the
        "change conditions then retry" strategies:

        - COMPRESS — re-compress on context overflow,
        - ROTATE_CREDENTIAL — advance to the next API key on the active provider,
        - FALLBACK — swap to the provider from the injected ``_fallback_supplier``.

        Each callback degrades to a no-op when unavailable (no extra key / no fallback
        supplier), so with the default single-key, single-model config this is
        behaviourally equivalent to calling ``send(self, messages)`` directly.
        """
        state = {"messages": compressed_message}
        runner: Optional[RecoveryRunner] = None

        def _active() -> "BaseLLM":
            return (runner.fallback_llm if runner else None) or self

        async def _compress(messages: list[dict]) -> list[dict]:
            state["messages"] = self.compress_messages(
                messages, compress_type=CompressType.POST_CUT_BY_TOKEN
            )
            return state["messages"]

        def _rotate() -> bool:
            return _active().rotate_credential()

        def _wrap_transformer(transform):
            # Mirror ``_compress``: persist the repaired messages into ``state`` so the
            # next ``_call`` issues the request with the transformed payload.
            async def _wrapped(messages, exc):
                repaired = await transform(messages, exc)
                if repaired is None:
                    return None
                state["messages"] = repaired
                return repaired

            return _wrapped

        transformers = {
            action: _wrap_transformer(transform)
            for action, transform in (self._message_transformers or {}).items()
        }

        runner = RecoveryRunner(
            compressor=_compress,
            credential_rotator=_rotate,
            fallback_supplier=self._fallback_supplier,
            message_transformers=transformers or None,
        )

        async def _call():
            return await send(_active(), state["messages"])

        return await runner.run(_call, messages=state["messages"])

    def _extract_assistant_rsp(self, context):
        return "\n".join([i["content"] for i in context if i["role"] == "assistant"])

    async def aask_batch(self, msgs: list, timeout=USE_CONFIG_TIMEOUT) -> str:
        """Sequential questioning"""
        context = []
        for msg in msgs:
            umsg = self._user_msg(msg)
            context.append(umsg)
            rsp_text = await self.acompletion_text(context, timeout=self.get_timeout(timeout))
            context.append(self._assistant_msg(rsp_text))
        return self._extract_assistant_rsp(context)

    async def aask_code(
        self, messages: Union[str, "Message", list[dict]], timeout=USE_CONFIG_TIMEOUT, **kwargs
    ) -> dict:
        raise NotImplementedError

    @abstractmethod
    async def _achat_completion(self, messages: list[dict], timeout=USE_CONFIG_TIMEOUT, **kwargs):
        """_achat_completion implemented by inherited class.

        ``**kwargs`` carries provider chat params (e.g. ``tools``/``tool_choice``
        for native tool-use) straight through to the request. Every Claude model
        in this fork is reached via the OpenAI-compatible client (a ``base_url``
        on OpenAILLM), so there is no separate Anthropic-SDK impl to update: the
        native specs ride this passthrough into _cons_kwargs.
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

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_random_exponential(min=1, max=60),
        after=after_log(logger, logger.level("WARNING").name),
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
        return rsp.get("choices")[0]["message"]["content"]

    def get_choice_delta_text(self, rsp: dict) -> str:
        """Required to provide the first text of stream choice"""
        return rsp.get("choices", [{}])[0].get("delta", {}).get("content", "")

    def get_choice_function(self, rsp: dict) -> dict:
        """Required to provide the first function of choice
        :param dict rsp: OpenAI chat.comletion respond JSON, Note "message" must include "tool_calls",
            and "tool_calls" must include "function", for example:
            {...
                "choices": [
                    {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": null,
                        "tool_calls": [
                        {
                            "id": "call_Y5r6Ddr2Qc2ZrqgfwzPX5l72",
                            "type": "function",
                            "function": {
                            "name": "execute",
                            "arguments": "{\n  \"language\": \"python\",\n  \"code\": \"print('Hello, World!')\"\n}"
                            }
                        }
                        ]
                    },
                    "finish_reason": "stop"
                    }
                ],
                ...}
        :return dict: return first function of choice, for exmaple,
            {'name': 'execute', 'arguments': '{\n  "language": "python",\n  "code": "print(\'Hello, World!\')"\n}'}
        """
        return rsp.get("choices")[0]["message"]["tool_calls"][0]["function"]

    def get_choice_function_arguments(self, rsp: dict) -> dict:
        """Required to provide the first function arguments of choice.

        :param dict rsp: same as in self.get_choice_function(rsp)
        :return dict: return the first function arguments of choice, for example,
            {'language': 'python', 'code': "print('Hello, World!')"}
        """
        return json.loads(self.get_choice_function(rsp)["arguments"], strict=False)

    def get_choice_tool_calls(self, rsp: dict) -> list[dict]:
        """Normalize all tool calls in a completion to a provider-agnostic list.

        Returns a list of ``{"id", "name", "arguments"}`` where ``arguments`` is
        an already-parsed dict. The default reads the OpenAI chat-completions
        shape (``choices[0].message.tool_calls[*].function``); providers with a
        different wire format (Anthropic content blocks) override this.

        Returns [] when the response carries no tool calls (e.g. plain text),
        so the native channel can fall back to text handling.
        """
        try:
            tool_calls = rsp.get("choices")[0]["message"].get("tool_calls") or []
        except (AttributeError, IndexError, TypeError, KeyError):
            return []
        out: list[dict] = []
        for call in tool_calls:
            fn = call.get("function") or {}
            raw_args = fn.get("arguments")
            try:
                args = json.loads(raw_args, strict=False) if isinstance(raw_args, str) else (raw_args or {})
            except json.JSONDecodeError:
                args = {}
            out.append({"id": call.get("id", ""), "name": fn.get("name", ""), "arguments": args})
        return out

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

    def get_content_under_limit_token(
        self, msg: dict, target_token_count: int, from_end: bool = True, delta: int = 2
    ) -> dict:
        """use binary search to truncate the content to meet the target token count
        Args:
            content: original content
            target_token_count: target token count
            from_end: whether to truncate from the end
        Returns:
            str: truncated content
        """

        def binary_search_truncate(text: str) -> str:
            total_token_count = self.count_tokens([{"role": msg["role"], "content": text}])
            if total_token_count <= target_token_count:
                return text
            left, right = 0, len(text)
            while left <= right:
                mid = (left + right) // 2
                mid_content = text[-mid:] if from_end else text[:mid]
                token_count = self.count_tokens([{"role": msg["role"], "content": mid_content}])
                if target_token_count > token_count and target_token_count - token_count <= delta:
                    return mid_content
                elif token_count < target_token_count:
                    left = mid + 1
                else:
                    right = mid - 1
            return ""

        # Handle GPT-4V case where content might be a list of dicts
        if isinstance(msg["content"], list):
            truncated_content = []
            # Find the text content in the list
            for item in msg["content"]:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_content = item.get("text", "")
                    truncated_content.append({"type": "text", "text": binary_search_truncate(text_content)})
                else:
                    truncated_content.append(item)
            return {"role": msg["role"], "content": truncated_content}

        # for normal text content
        return {"role": msg["role"], "content": binary_search_truncate(msg["content"])}

    def get_content_under_limit_token_balanced(
        self, msg: dict, target_token_count: int, delta: int = 8, head_ratio: float = 0.5
    ) -> dict:
        """Truncate long content by preserving both head and tail with a middle marker."""
        if target_token_count <= 0:
            return {"role": msg["role"], "content": "" if not isinstance(msg["content"], list) else []}

        placeholder = "\n\n...[TRUNCATED MIDDLE]...\n\n"

        def balanced_truncate(text: str) -> str:
            if not text:
                return ""
            probe = {"role": msg["role"], "content": text}
            if self.count_tokens([probe]) <= target_token_count:
                return text

            placeholder_tokens = self.count_tokens([{"role": msg["role"], "content": placeholder}])
            if placeholder_tokens >= target_token_count:
                # Budget is too tight to even hold the truncation marker. Fall back to
                # head-only truncation (from_end=False keeps the prefix) rather than emit
                # a message that is just the placeholder with no real content.
                return self.get_content_under_limit_token(
                    {"role": msg["role"], "content": text},
                    target_token_count=target_token_count,
                    from_end=False,
                    delta=delta,
                )["content"]

            left, right = 0, len(text)
            best = ""
            while left <= right:
                mid = (left + right) // 2
                head_len = max(1, int(mid * head_ratio))
                tail_len = max(1, mid - head_len)
                candidate = text[:head_len] + placeholder + text[-tail_len:]
                token_count = self.count_tokens([{"role": msg["role"], "content": candidate}])
                if token_count <= target_token_count:
                    best = candidate
                    if target_token_count - token_count <= delta:
                        return candidate
                    left = mid + 1
                else:
                    right = mid - 1

            return best or self.get_content_under_limit_token(
                {"role": msg["role"], "content": text},
                target_token_count=target_token_count,
                from_end=False,
                delta=delta,
            )["content"]

        if isinstance(msg["content"], list):
            truncated_content = []
            for item in msg["content"]:
                if isinstance(item, dict) and item.get("type") == "text":
                    truncated_content.append({"type": "text", "text": balanced_truncate(item.get("text", ""))})
                else:
                    truncated_content.append(item)
            return {"role": msg["role"], "content": truncated_content}

        return {"role": msg["role"], "content": balanced_truncate(msg["content"])}

    def compress_messages(
        self,
        messages: list[dict],
        compress_type: CompressType = CompressType.NO_COMPRESS,
        max_token: int = 128000,
        threshold: float = 0.8,
    ) -> list[dict]:
        """Compress messages to fit within the token limit.
        Args:
            messages (list[dict]): List of messages to compress.
            compress_type (CompressType, optional): Compression strategy. Defaults to CompressType.NO_COMPRESS.
            max_token (int, optional): Maximum token limit. Defaults to 128000. Not effective if token limit can be found in TOKEN_MAX.
            threshold (float): Token limit threshold. Defaults to 0.8. Reserve 20% of the token limit for completion message.
        """
        return self.request_context_builder.build(
            messages,
            compress_type=compress_type,
            max_token=max_token,
            threshold=threshold,
        )
