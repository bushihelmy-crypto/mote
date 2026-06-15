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
from typing import Optional, Union

from json_repair import repair_json
from openai import AsyncStream
from openai._base_client import AsyncHttpxClientWrapper
from openai.types import CompletionUsage
from openai.types.chat import ChatCompletion, ChatCompletionChunk
from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_message import ChatCompletionMessage
from openai.types.chat.chat_completion_message_tool_call import (
    ChatCompletionMessageToolCall,
    Function,
)
from tenacity import (
    after_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

from metagpt.common.config.config.llm_config import LLMConfig, LLMType
from metagpt.common.exception import (
    LLMEmptyResponseError,
    LLMResponseParseError,
    classify_llm_error,
    is_retryable,
)
from metagpt.common.const import USE_CONFIG_TIMEOUT
from metagpt.common.logs import log_llm_stream, logger
from metagpt.common.observability.langfuse_integration import make_async_openai
from metagpt.router.llm.base_llm import BaseLLM
from metagpt.router.llm.constant import GENERAL_FUNCTION_SCHEMA
from metagpt.router.llm.llm_provider_registry import register_provider
from metagpt.common.utils.common import CodeParser, decode_image, log_and_reraise
from metagpt.common.utils.exceptions import handle_exception
from metagpt.router.cost import CostTracker
from metagpt.common.utils.token_counter import (
    count_message_tokens,
    count_string_tokens,
    get_max_completion_tokens,
)


@register_provider(
    [
        LLMType.OPENAI,
        LLMType.FIREWORKS,
        LLMType.OPEN_LLM,
        LLMType.MOONSHOT,
        LLMType.MISTRAL,
        LLMType.YI,
        LLMType.OPEN_ROUTER,
        LLMType.DEEPSEEK,
        LLMType.SILICONFLOW,
    ]
)
class OpenAILLM(BaseLLM):
    """Check https://platform.openai.com/examples for examples"""

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
        # Normalize api_key into a rotatable list; index points at the active key.
        keys = self.config.api_key
        self._api_keys: list[str] = list(keys) if isinstance(keys, list) else [keys]
        self._api_key_index: int = 0
        # Opt-in OAuth: when configured, the bearer token comes from the OAuth
        # manager (proactive-refresh) instead of the static api_key list.
        self._oauth = self._build_oauth_manager()
        kwargs = self._make_client_kwargs()
        self.aclient = make_async_openai(**kwargs)

    def _build_oauth_manager(self):
        """Construct an OAuthManager when ``config.oauth`` is set, else None."""
        if not getattr(self.config, "oauth", None):
            return None
        from metagpt.router.oauth import OAuthManager

        return OAuthManager(self.config.oauth)

    def _current_api_key(self) -> str:
        return self._api_keys[self._api_key_index]

    def _make_client_kwargs(self) -> dict:
        if self._oauth is not None:
            # OAuth path: inject the (proactively refreshed) bearer token as the
            # OpenAI SDK api_key and merge any provider-specific extra headers.
            kwargs = {"api_key": self._oauth.get_valid_token(), "base_url": self.config.base_url}
            if self.config.oauth.headers_extra:
                kwargs["default_headers"] = dict(self.config.oauth.headers_extra)
        else:
            kwargs = {"api_key": self._current_api_key(), "base_url": self.config.base_url}

        # to use proxy, openai v1 needs http_client
        if proxy_params := self._get_proxy_params():
            kwargs["http_client"] = AsyncHttpxClientWrapper(**proxy_params)

        return kwargs

    def rotate_credential(self) -> bool:
        """Advance to the next configured API key and rebuild the client.

        Consumed by the recovery loop on ROTATE_CREDENTIAL (auth/billing errors).
        Returns False when no further key remains (rotation exhausted).

        In OAuth mode, "rotation" means force-refreshing the bearer token: a new
        token rebuilds the client and returns True; a permanently failed refresh
        returns False.
        """
        if self._oauth is not None:
            token = self._oauth.force_refresh()
            if token is None:
                return False
            self.aclient = make_async_openai(**self._make_client_kwargs())
            return True
        if self._api_key_index + 1 >= len(self._api_keys):
            return False
        self._api_key_index += 1
        self.aclient = make_async_openai(**self._make_client_kwargs())
        return True

    def _get_proxy_params(self) -> dict:
        params = {}
        if self.config.proxy:
            params = {"proxy": self.config.proxy}
            if self.config.base_url:
                params["base_url"] = self.config.base_url

        return params

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
                finish_reason = (
                    chunk.choices[0].finish_reason
                    if chunk.choices and hasattr(chunk.choices[0], "finish_reason")
                    else None
                )
                log_llm_stream(chunk_message)
                collected_messages.append(chunk_message)
                if finish_reason:
                    if hasattr(chunk, "usage"):
                        # Some services have usage as an attribute of the chunk, such as Fireworks
                        usage = CompletionUsage(**chunk.usage) if isinstance(chunk.usage, dict) else chunk.usage
                    elif hasattr(chunk.choices[0], "usage"):
                        # The usage of some services is an attribute of chunk.choices[0], such as Moonshot
                        usage = CompletionUsage(**chunk.choices[0].usage)
                    if "openrouter.ai" in self.config.base_url and hasattr(chunk, "usage") and chunk.usage is not None:
                        # due to it get token cost from api
                        usage = chunk.usage
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
                    if getattr(chunk, "usage", None):
                        usage = chunk.usage
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
                    if getattr(chunk, "usage", None):
                        usage = CompletionUsage(**chunk.usage) if isinstance(chunk.usage, dict) else chunk.usage
                    elif hasattr(choice, "usage") and choice.usage:
                        usage = CompletionUsage(**choice.usage)
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
        message = ChatCompletionMessage(role="assistant", content=full_text or None, tool_calls=tool_calls or None)
        rsp_choice = Choice(index=0, finish_reason=finish_reason or "stop", message=message)
        return ChatCompletion(
            id="stream", choices=[rsp_choice], created=int(time.time()), model=self.model, object="chat.completion"
        )

    def _cons_kwargs(self, messages: list[dict], timeout=USE_CONFIG_TIMEOUT, **extra_kwargs) -> dict:
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

        if self.model == "gpt-5":
            kwargs.pop("max_tokens", None)  # GPT-5 doesn't support max_tokens
            kwargs.pop("temperature", None)  # GPT-5 doesn't support temperature, only default

        if self.model == "claude-opus-4-8":
            kwargs.pop("temperature", None)  # claude-opus-4-8 doesn't support temperature

        return kwargs

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
        if raise_if_empty and (not rsp or not rsp.choices or not "".join([i.message.content or "" for i in rsp.choices])):
            raise LLMEmptyResponseError("The LLM's response is empty.")
        self._update_costs(rsp.usage)
        return rsp

    async def acompletion(
        self, messages: list[dict], timeout=USE_CONFIG_TIMEOUT, raise_if_empty: bool = True
    ) -> ChatCompletion:
        return await self._achat_completion(messages, timeout=self.get_timeout(timeout), raise_if_empty=raise_if_empty)

    @retry(
        wait=wait_random_exponential(min=1, max=60),
        stop=stop_after_attempt(6),
        after=after_log(logger, logger.level("WARNING").name),
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
        except Exception as e:
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
        if (
            message.tool_calls is not None
            and message.tool_calls[0].function is not None
            and message.tool_calls[0].function.arguments is not None
        ):
            # reponse is code
            try:
                return json.loads(message.tool_calls[0].function.arguments, strict=False)
            except json.decoder.JSONDecodeError as e:
                return self._parse_arguments(message.tool_calls[0].function.arguments)
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
        return rsp.choices[0].message.content if rsp.choices else ""

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
                args = self._repair_tool_arguments(raw_args)
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
        model: str = None,
        resp_format: str = "url",
    ) -> list["Image"]:
        """image generate"""
        assert resp_format in ["url", "b64_json"]
        if not model:
            model = self.model
        res = await self.aclient.images.generate(
            model=model, prompt=prompt, size=size, quality=quality, n=1, response_format=resp_format
        )
        imgs = []
        for item in res.data:
            img_url_or_b64 = item.url if resp_format == "url" else item.b64_json
            imgs.append(decode_image(img_url_or_b64))
        return imgs

    def count_tokens(self, messages: list[dict]) -> int:
        try:
            return count_message_tokens(messages, self.model)
        except:
            return super().count_tokens(messages)
