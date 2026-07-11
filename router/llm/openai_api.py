# -*- coding: utf-8 -*-
"""
@Time    : 2023/5/5 23:08
@Author  : alexanderwu
@File    : openai.py
@Modified By: mashenquan, 2023/11/21. Fix bug: ReadTimeout.
@Modified By: mashenquan, 2023/12/1. Fix bug: Unclosed connection caused by openai 0.x.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Optional, Union

from json_repair import repair_json
from openai import AsyncOpenAI, AsyncStream
from openai._base_client import AsyncHttpxClientWrapper
from openai.types import CompletionUsage, Image
from openai.types.chat import ChatCompletion, ChatCompletionChunk
from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_message import ChatCompletionMessage
from openai.types.chat.chat_completion_message_tool_call import ChatCompletionMessageToolCall, Function
from tenacity import after_log, retry, retry_if_exception, stop_after_attempt, wait_random_exponential

from mote.common.config.config.llm_config import LLMConfig, LLMType
from mote.common.const import USE_CONFIG_TIMEOUT
from mote.common.events import log_llm_stream
from mote.common.exception import LLMEmptyResponseError, LLMResponseParseError, classify_llm_error, is_retryable
from mote.common.logs import logger
from mote.common.utils.common import CodeParser, decode_image, log_and_reraise
from mote.common.utils.exceptions import handle_exception
from mote.common.utils.token_counter import count_message_tokens, count_string_tokens, get_max_completion_tokens
from mote.router.cost import CostTracker
from mote.router.llm.base_llm import LLM_RETRY_ATTEMPTS, BaseLLM
from mote.router.llm.constant import GENERAL_FUNCTION_SCHEMA
from mote.router.llm.credentials import CredentialRotationMixin
from mote.router.llm.llm_provider_registry import register_provider

# Models that reject standard chat params. Keyed by model name → the set of
# request kwargs to drop. Data-driven so adding a model is a table edit, not a
# new ``if self.model == ...`` branch in ``_cons_kwargs``.
_UNSUPPORTED_REQUEST_PARAMS: dict[str, frozenset] = {
    "gpt-5": frozenset({"max_tokens", "temperature"}),  # GPT-5: only default temperature, no max_tokens
    "claude-opus-4-8": frozenset({"temperature"}),  # claude-opus-4-8: only default temperature
}


@register_provider(
    [
        LLMType.OPENAI,
        LLMType.FIREWORKS,
        LLMType.OPEN_LLM,
        LLMType.MOONSHOT,
        LLMType.MISTRAL,
        LLMType.YI,
        LLMType.OPEN_ROUTER,
        LLMType.SILICONFLOW,
    ]
)
class OpenAILLM(CredentialRotationMixin, BaseLLM):
    """Check https://platform.openai.com/examples for examples"""

    # Narrow the base class's Optional[AsyncOpenAI]: this provider always builds
    # a client in _init_client(), so it is never None on the request paths.
    aclient: AsyncOpenAI

    def __init__(self, config: LLMConfig):
        self.config = config
        self._init_client()
        self.auto_max_tokens = False
        self.cost_manager: Optional[CostTracker] = None

    def _init_client(self):
        """https://github.com/openai/openai-python#async-usage"""
        self.model = self.config.model  # Used in _calc_usage & _cons_kwargs
        self.max_completion_token = self.config.max_token
        self.pricing_plan = self.config.pricing_plan or self.model
        # Normalize credentials (api_key list / OAuth) via the shared mixin, then
        # build the client from the active one.
        self._init_credentials()
        self.aclient = self._rebuild_client()

    def _rebuild_client(self) -> AsyncOpenAI:
        return AsyncOpenAI(**self._make_client_kwargs())

    def _make_client_kwargs(self) -> dict:
        kwargs: dict[str, Any]
        if self._oauth is not None:
            # OAuth path: inject the (proactively refreshed) bearer token as the
            # OpenAI SDK api_key and merge any provider-specific extra headers.
            kwargs = {"api_key": self._oauth.get_valid_token(), "base_url": self.config.base_url}
            if self.config.oauth and self.config.oauth.headers_extra:
                kwargs["default_headers"] = dict(self.config.oauth.headers_extra)
        else:
            kwargs = {"api_key": self._current_api_key(), "base_url": self.config.base_url}

        # to use proxy, openai v1 needs http_client
        if proxy_params := self._get_proxy_params():
            kwargs["http_client"] = AsyncHttpxClientWrapper(**proxy_params)

        return kwargs

    def _get_proxy_params(self) -> dict:
        params = {}
        if self.config.proxy:
            params = {"proxy": self.config.proxy}
            if self.config.base_url:
                params["base_url"] = self.config.base_url

        return params

    def _extract_stream_usage(self, chunk, choice=None) -> Optional[CompletionUsage]:
        """Pull usage off a streaming chunk across provider shapes.

        Chunk-level usage covers OpenAI/Fireworks/OpenRouter; choice-level usage
        covers Moonshot. Handles dict-vs-object forms and returns ``None`` when the
        chunk carries no usage so callers can keep an earlier value
        (``usage = self._extract_stream_usage(...) or usage``).
        """
        chunk_usage = getattr(chunk, "usage", None)
        if chunk_usage:
            return CompletionUsage(**chunk_usage) if isinstance(chunk_usage, dict) else chunk_usage
        target = choice if choice is not None else (chunk.choices[0] if getattr(chunk, "choices", None) else None)
        choice_usage = getattr(target, "usage", None) if target is not None else None
        if choice_usage:
            return CompletionUsage(**choice_usage) if isinstance(choice_usage, dict) else choice_usage
        return None

    async def _achat_completion_stream(
        self, messages: list[dict], timeout=USE_CONFIG_TIMEOUT, raise_if_empty: bool = True
    ) -> str:
        usage = None
        collected_messages = []
        # Provider errors can surface at create() or mid-stream; translate both
        # into typed LLMErrors so the retry predicate works off our hierarchy.
        try:
            response: AsyncStream[ChatCompletionChunk] = await self.aclient.chat.completions.create(
                **self._cons_kwargs(messages, timeout=self.get_timeout(timeout)), stream=True
            )
            async for chunk in response:
                chunk_message = chunk.choices[0].delta.content or "" if chunk.choices else ""  # extract the message
                log_llm_stream(chunk_message)
                collected_messages.append(chunk_message)
                usage = self._extract_stream_usage(chunk) or usage
        except Exception as e:
            raise classify_llm_error(e) or e

        log_llm_stream("\n")
        full_reply_content = "".join(collected_messages)
        if raise_if_empty and not full_reply_content:
            raise LLMEmptyResponseError("The LLM's response is empty.")
        if not usage:
            # Some services do not provide the usage attribute, such as OpenAI or OpenLLM
            usage = self._calc_usage(messages, full_reply_content)

        self._update_costs(usage)
        return full_reply_content

    async def _achat_completion_stream_tool(
        self, messages: list[dict], timeout=USE_CONFIG_TIMEOUT, raise_if_empty: bool = True, **chat_configs
    ) -> ChatCompletion:
        """Streaming native tool-use completion: stream text, rebuild the response.

        OpenAI streams the assistant text in ``delta.content`` and the tool calls
        as ``delta.tool_calls`` fragments keyed by ``index`` (id + name arrive once,
        the JSON ``arguments`` stream in pieces). We mirror the text to
        ``log_llm_stream`` for the live console while accumulating the fragments,
        then reassemble a ``ChatCompletion`` so ``get_choice_text`` /
        ``get_choice_tool_calls`` parse it exactly like the blocking path.
        """
        content_parts: list[str] = []
        # index -> {"id", "name", "args"}; ``order`` preserves first-seen ordering.
        tool_state: dict[int, dict] = {}
        order: list[int] = []
        finish_reason = None
        usage = None
        try:
            response: AsyncStream[ChatCompletionChunk] = await self.aclient.chat.completions.create(
                **self._cons_kwargs(messages, timeout=self.get_timeout(timeout), **chat_configs), stream=True
            )
            async for chunk in response:
                if not chunk.choices:
                    # A usage-only trailer chunk (when stream_options.include_usage is on).
                    usage = self._extract_stream_usage(chunk) or usage
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                if delta and delta.content:
                    log_llm_stream(delta.content)
                    content_parts.append(delta.content)
                for tc in (delta.tool_calls or []) if delta else []:
                    slot = tool_state.get(tc.index)
                    if slot is None:
                        slot = {"id": "", "name": "", "args": ""}
                        tool_state[tc.index] = slot
                        order.append(tc.index)
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            slot["name"] = tc.function.name
                        if tc.function.arguments:
                            slot["args"] += tc.function.arguments
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
                usage = self._extract_stream_usage(chunk, choice) or usage
        except Exception as e:
            raise classify_llm_error(e) or e

        log_llm_stream("\n")
        full_text = "".join(content_parts)
        tool_calls = [
            ChatCompletionMessageToolCall(
                id=tool_state[i]["id"] or f"call_{i}",
                type="function",
                function=Function(name=tool_state[i]["name"], arguments=tool_state[i]["args"]),
            )
            for i in order
        ]
        if raise_if_empty and not full_text and not tool_calls:
            raise LLMEmptyResponseError("The LLM's response is empty.")
        if not usage:
            usage = self._calc_usage(messages, full_text)
        self._update_costs(usage)
        message = ChatCompletionMessage(role="assistant", content=full_text or None, tool_calls=tool_calls or None)  # type: ignore[arg-type]  # list invariance: ChatCompletionMessageToolCall is a union member
        rsp_choice = Choice(index=0, finish_reason=finish_reason or "stop", message=message)
        return ChatCompletion(
            id="stream",
            choices=[rsp_choice],
            created=int(time.time()),
            model=self.model or "",
            object="chat.completion",
        )

    def _cons_kwargs(self, messages: list[dict], timeout=USE_CONFIG_TIMEOUT, **extra_kwargs) -> dict:
        # Drop the declarative prompt-cache intent key: OpenAI-compatible providers
        # cache prefixes automatically (no client marker), and the volatile tail
        # naturally falls past the auto-detected divergence point — so the intent
        # needs no translation here, only removal so the API sees canonical messages.
        messages = self._strip_cache_intent(messages)
        kwargs = {
            "messages": messages,
            "max_tokens": self._get_max_tokens(messages),
            # "n": 1,  # Some services do not provide this parameter, such as mistral
            # "stop": None,  # default it's None and gpt4-v can't have this one
            "temperature": self.config.temperature,
            "model": self.model,
            "timeout": self.get_timeout(timeout),
        }
        if extra_kwargs:
            kwargs.update(extra_kwargs)

        for param in _UNSUPPORTED_REQUEST_PARAMS.get(self.model or "", frozenset()):
            kwargs.pop(param, None)

        return kwargs

    @staticmethod
    def _strip_cache_intent(messages: list[dict]) -> list[dict]:
        """Return ``messages`` with the private ``_cache_intent`` key removed.

        Copy-on-strip: only the messages that carry the key are rebuilt, so the
        caller's list/dicts are never mutated. Absent the key (the common path)
        the input list is returned unchanged.
        """
        if not isinstance(messages, list):
            return messages
        out = []
        touched = False
        for msg in messages:
            if isinstance(msg, dict) and "_cache_intent" in msg:
                msg = {k: v for k, v in msg.items() if k != "_cache_intent"}
                touched = True
            out.append(msg)
        return out if touched else messages

    async def _acreate(self, **kwargs):
        """Call the OpenAI chat-completions endpoint, translating provider errors.

        Any raw OpenAI/transport exception is mapped to a typed ``LLMError`` (via
        :func:`classify_llm_error`) so the retry predicate and recovery hints work
        off our own hierarchy; unrecognized errors propagate unchanged.
        """
        try:
            return await self.aclient.chat.completions.create(**kwargs)
        except Exception as e:
            raise classify_llm_error(e) or e

    async def _achat_completion(
        self, messages: list[dict], timeout=USE_CONFIG_TIMEOUT, raise_if_empty: bool = True, **chat_configs
    ) -> ChatCompletion:
        kwargs = self._cons_kwargs(messages, timeout=self.get_timeout(timeout), **chat_configs)
        rsp: ChatCompletion = await self._acreate(**kwargs)
        if raise_if_empty and (
            not rsp or not rsp.choices or not "".join([i.message.content or "" for i in rsp.choices])
        ):
            raise LLMEmptyResponseError("The LLM's response is empty.")
        self._update_costs(rsp.usage)
        return rsp

    async def acompletion(
        self, messages: list[dict], timeout=USE_CONFIG_TIMEOUT, raise_if_empty: bool = True
    ) -> ChatCompletion:
        return await self._achat_completion(messages, timeout=self.get_timeout(timeout), raise_if_empty=raise_if_empty)

    @retry(
        wait=wait_random_exponential(min=1, max=60),
        stop=stop_after_attempt(LLM_RETRY_ATTEMPTS),
        after=after_log(logger, logger.level("WARNING").name),  # type: ignore[arg-type]  # loguru logger + str level vs tenacity stdlib-logging stub
        retry=retry_if_exception(is_retryable),
        retry_error_callback=log_and_reraise,
    )
    async def acompletion_text(
        self, messages: list[dict], stream=False, timeout=USE_CONFIG_TIMEOUT, raise_if_empty: bool = True
    ) -> str:
        """when streaming, print each token in place."""
        if stream:
            return await self._achat_completion_stream(messages, timeout=timeout, raise_if_empty=raise_if_empty)

        rsp = await self._achat_completion(messages, timeout=self.get_timeout(timeout), raise_if_empty=raise_if_empty)
        return self.get_choice_text(rsp)

    async def _achat_completion_function(
        self, messages: list[dict], timeout: int = USE_CONFIG_TIMEOUT, **chat_configs
    ) -> ChatCompletion:
        messages = self.format_msg(messages)
        kwargs = self._cons_kwargs(messages=messages, timeout=self.get_timeout(timeout), **chat_configs)
        rsp: ChatCompletion = await self._acreate(**kwargs)
        self._update_costs(rsp.usage)
        return rsp

    async def aask_code(self, messages: list[dict], timeout: int = USE_CONFIG_TIMEOUT, **kwargs) -> dict:
        """Use function of tools to ask a code.
        Note: Keep kwargs consistent with https://platform.openai.com/docs/api-reference/chat/create

        Examples:
        >>> llm = OpenAILLM()
        >>> msg = [{'role': 'user', 'content': "Write a python hello world code."}]
        >>> rsp = await llm.aask_code(msg)
        # -> {'language': 'python', 'code': "print('Hello, World!')"}
        """
        if "tools" not in kwargs:
            configs = {"tools": [{"type": "function", "function": GENERAL_FUNCTION_SCHEMA}]}
            kwargs.update(configs)
        rsp = await self._achat_completion_function(messages, **kwargs)
        return self.get_choice_function_arguments(rsp)

    def _parse_arguments(self, arguments: str) -> dict:
        """parse arguments in openai function call"""
        if "language" not in arguments and "code" not in arguments:
            return {"language": "python", "code": arguments}

        # 匹配language
        language_pattern = re.compile(r'[\"\']?language[\"\']?\s*:\s*["\']([^"\']+?)["\']', re.DOTALL)
        language_match = language_pattern.search(arguments)
        language_value = language_match.group(1) if language_match else "python"

        # 匹配code
        code_pattern = r'(["\'`]{3}|["\'`])([\s\S]*?)\1'
        try:
            code_value = re.findall(code_pattern, arguments)[-1][-1]
        except Exception:
            code_value = None

        if code_value is None:
            raise LLMResponseParseError(f"Parse code error for {arguments}")
        # arguments只有code的情况
        return {"language": language_value, "code": code_value}

    # @handle_exception
    def get_choice_function_arguments(self, rsp: ChatCompletion) -> dict:
        """Required to provide the first function arguments of choice.

        :param dict rsp: same as in self.get_choice_function(rsp)
        :return dict: return the first function arguments of choice, for example,
            {'language': 'python', 'code': "print('Hello, World!')"}
        """
        message = rsp.choices[0].message
        first_call = message.tool_calls[0] if message.tool_calls else None
        if (
            first_call is not None
            and isinstance(first_call, ChatCompletionMessageToolCall)
            and first_call.function.arguments is not None
        ):
            # reponse is code
            try:
                return json.loads(first_call.function.arguments, strict=False)
            except json.decoder.JSONDecodeError:
                return self._parse_arguments(first_call.function.arguments)
        elif message.tool_calls is None and message.content is not None:
            # reponse is code, fix openai tools_call respond bug,
            # The response content is `code``, but it appears in the content instead of the arguments.
            code_formats = "```"
            if message.content.startswith(code_formats) and message.content.endswith(code_formats):
                code = CodeParser.parse_code(text=message.content)
                return {"language": "python", "code": code}
            # reponse is message
            return {"language": "markdown", "code": self.get_choice_text(rsp)}
        else:
            raise LLMResponseParseError(f"Failed to parse \n {rsp}\n")

    def get_choice_text(self, rsp: ChatCompletion) -> str:
        """Required to provide the first text of choice"""
        return (rsp.choices[0].message.content or "") if rsp.choices else ""

    def get_choice_tool_calls(self, rsp: ChatCompletion) -> list[dict]:
        """Normalize OpenAI ``tool_calls`` (object form) to the agnostic list.

        Reads ``choices[0].message.tool_calls`` off the ChatCompletion object
        (attribute access, unlike the base method's dict access) and parses each
        function's JSON ``arguments`` into a dict. Returns [] for a text-only
        response so the native channel can fall back to text handling.
        """
        if not rsp.choices:
            return []
        tool_calls = getattr(rsp.choices[0].message, "tool_calls", None) or []
        out: list[dict] = []
        for call in tool_calls:
            fn = call.function
            raw_args = getattr(fn, "arguments", None)
            try:
                args = json.loads(raw_args, strict=False) if isinstance(raw_args, str) else (raw_args or {})
            except json.JSONDecodeError:
                # A model emitting a large multi-line string argument (e.g.
                # ApplyPatch's whole patch) sometimes produces invalid JSON —
                # unescaped newlines/quotes or a truncated tail. Try json_repair
                # to recover the call rather than dropping the entire argument.
                args = self._repair_tool_arguments(raw_args or "")
            out.append({"id": getattr(call, "id", ""), "name": getattr(fn, "name", ""), "arguments": args})
        return out

    @staticmethod
    def _repair_tool_arguments(raw_args: str) -> dict:
        """Best-effort recover a tool call's malformed JSON ``arguments``.

        Returns the repaired argument dict, or ``{}`` when even repair fails
        (``repair_json`` returns a non-dict — e.g. ``""`` — for unsalvageable
        input). Used only on the ``JSONDecodeError`` fallback path; well-formed
        arguments never reach here.
        """
        try:
            repaired = repair_json(raw_args, return_objects=True)
        except Exception:  # noqa: BLE001 — repair is best-effort, never raise
            return {}
        return repaired if isinstance(repaired, dict) else {}

    def _calc_usage(self, messages: list[dict], rsp: str) -> CompletionUsage:
        usage = CompletionUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)
        if not self.config.calc_usage:
            return usage

        try:
            usage.prompt_tokens = count_message_tokens(messages, self.pricing_plan)
            usage.completion_tokens = count_string_tokens(rsp, self.pricing_plan)
        except Exception as e:
            logger.warning(f"usage calculation failed: {e}")

        return usage

    def _get_max_tokens(self, messages: list[dict]):
        if not self.auto_max_tokens:
            return self.max_completion_token
        # FIXME
        # https://community.openai.com/t/why-is-gpt-3-5-turbo-1106-max-tokens-limited-to-4096/494973/3
        return min(get_max_completion_tokens(messages, self.model, self.max_completion_token), 4096)

    @handle_exception
    async def amoderation(self, content: Union[str, list[str]]):
        """Moderate content."""
        return await self.aclient.moderations.create(input=content)

    async def atext_to_speech(self, **kwargs):
        """text to speech"""
        return await self.aclient.audio.speech.create(**kwargs)

    async def aspeech_to_text(self, **kwargs):
        """speech to text"""
        return await self.aclient.audio.transcriptions.create(**kwargs)

    async def gen_image(
        self,
        prompt: str,
        size: str = "1024x1024",
        quality: str = "standard",
        model: Optional[str] = None,
        resp_format: str = "url",
    ) -> list["Image"]:
        """image generate"""
        assert resp_format in ["url", "b64_json"]
        model = model or self.model or ""
        res = await self.aclient.images.generate(
            model=model,
            prompt=prompt,
            size=size,  # type: ignore[arg-type]  # runtime str vs SDK size Literal
            quality=quality,  # type: ignore[arg-type]  # runtime str vs SDK quality Literal
            n=1,
            response_format=resp_format,  # type: ignore[arg-type]  # runtime str vs SDK response_format Literal
        )
        imgs = []
        for item in res.data or []:
            img_url_or_b64 = item.url if resp_format == "url" else item.b64_json
            imgs.append(decode_image(img_url_or_b64 or ""))
        return imgs

    def count_tokens(self, messages: list[dict]) -> int:
        try:
            return count_message_tokens(messages, self.model)
        except Exception:
            return super().count_tokens(messages)
