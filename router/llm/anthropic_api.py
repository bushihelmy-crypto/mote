# -*- coding: utf-8 -*-
"""Native Anthropic Messages API provider.

Talks directly to ``/v1/messages`` via the ``anthropic`` SDK, instead of routing
Claude through the OpenAI-compatible client. Selected by ``api_type: anthropic``
or auto-detected when ``base_url`` points at ``anthropic.com`` (see
``create_llm_instance``).

The rest of the framework speaks the OpenAI wire shape: message dicts come in as
``{"role", "content"[, "tool_calls"]}`` / ``{"role": "tool", "tool_call_id"}``
(produced by ``Message.to_dict``), and tool specs/``tool_choice`` follow the
OpenAI or already-Anthropic envelope. This provider converts that shape into the
Anthropic ``messages`` + ``system`` + content-block format on the way out, and
normalizes the Anthropic response (``content`` blocks of ``text`` / ``tool_use``)
back into the agnostic ``get_choice_text`` / ``get_choice_tool_calls`` contract.
"""
from __future__ import annotations

import base64
import binascii
import json
from typing import Any, Optional, Union

from tenacity import (
    after_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

from metagpt.common.config.config.llm_config import LLMConfig, LLMType
from metagpt.common.const import USE_CONFIG_TIMEOUT
from metagpt.common.exception import (
    LLMEmptyResponseError,
    classify_llm_error,
    is_retryable,
)
from metagpt.common.logs import log_llm_stream, logger
from metagpt.common.utils.common import log_and_reraise
from metagpt.common.utils.token_counter import count_message_tokens, count_string_tokens
from metagpt.router.cost import CostTracker
from metagpt.router.llm.base_llm import BaseLLM
from metagpt.router.llm.llm_provider_registry import register_provider


@register_provider([LLMType.ANTHROPIC])
class AnthropicLLM(BaseLLM):
    """Provider for Anthropic's native Messages API (Claude models)."""

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
        # Normalize api_key into a rotatable list; index points at the active key.
        keys = self.config.api_key
        self._api_keys: list[str] = list(keys) if isinstance(keys, list) else [keys]
        self._api_key_index: int = 0
        self._oauth = self._build_oauth_manager()
        self.aclient = self._make_client()

    def _build_oauth_manager(self):
        """Construct an OAuthManager when ``config.oauth`` is set, else None."""
        if not getattr(self.config, "oauth", None):
            return None
        from metagpt.router.oauth import OAuthManager

        return OAuthManager(self.config.oauth)

    def _current_api_key(self) -> str:
        return self._api_keys[self._api_key_index]

    def _make_client(self):
        from anthropic import AsyncAnthropic

        kwargs: dict[str, Any] = {}
        if self.config.base_url:
            kwargs["base_url"] = self.config.base_url
        if self._oauth is not None:
            # OAuth path: send the bearer token via auth_token and merge any
            # provider-specific extra headers (e.g. the anthropic-beta opt-in).
            kwargs["auth_token"] = self._oauth.get_valid_token()
            if self.config.oauth.headers_extra:
                kwargs["default_headers"] = dict(self.config.oauth.headers_extra)
        else:
            kwargs["api_key"] = self._current_api_key()
        if http_client := self._make_http_client():
            kwargs["http_client"] = http_client
        return AsyncAnthropic(**kwargs)

    def _make_http_client(self):
        """Build an httpx async client for proxy support, or None."""
        if not self.config.proxy:
            return None
        try:
            import httpx
        except Exception:  # noqa: BLE001 — proxy is best-effort
            logger.warning("httpx unavailable; ignoring proxy for AnthropicLLM.")
            return None
        return httpx.AsyncClient(proxy=self.config.proxy)

    def rotate_credential(self) -> bool:
        """Advance to the next configured API key (or refresh OAuth) and rebuild.

        Consumed by the recovery loop on ROTATE_CREDENTIAL. Returns False when no
        further credential remains.
        """
        if self._oauth is not None:
            token = self._oauth.force_refresh()
            if token is None:
                return False
            self.aclient = self._make_client()
            return True
        if self._api_key_index + 1 >= len(self._api_keys):
            return False
        self._api_key_index += 1
        self.aclient = self._make_client()
        return True

    # -- message conversion (OpenAI wire shape -> Anthropic) ----------------
    def _convert_messages(self, messages: list[dict]) -> tuple[str, list[dict]]:
        """Split out the system prompt and convert the rest to Anthropic messages.

        Returns ``(system_text, anthropic_messages)``. System messages are joined
        into the top-level ``system`` string; ``tool`` results become ``tool_result``
        blocks inside a user turn; assistant ``tool_calls`` become ``tool_use``
        blocks. Consecutive same-role turns are merged so the user/assistant
        alternation the API expects is preserved (e.g. parallel tool results).
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
                block = {
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": self._stringify(msg.get("content")),
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
            # data:<media_type>;base64,<data>
            try:
                header, data = url.split(",", 1)
                media_type = header.split(";")[0][len("data:"):] or "image/jpeg"
            except ValueError:
                return None
            # The declared media type is often wrong (e.g. a PNG labelled as
            # JPEG); Anthropic rejects mismatches, so sniff the real type from
            # the decoded bytes and prefer it when recognised.
            sniffed = AnthropicLLM._sniff_image_media_type(data)
            if sniffed:
                media_type = sniffed
            return {
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": data},
            }
        return {"type": "image", "source": {"type": "url", "url": url}}

    @staticmethod
    def _sniff_image_media_type(b64_data: str) -> Optional[str]:
        """Detect an image's media type from its leading bytes (magic numbers).

        Returns ``None`` when the data can't be decoded or the format isn't
        recognised, leaving the declared media type untouched.
        """
        try:
            header = base64.b64decode(b64_data[:64], validate=False)
        except (binascii.Error, ValueError):
            return None
        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if header.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if header.startswith(b"GIF87a") or header.startswith(b"GIF89a"):
            return "image/gif"
        if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
            return "image/webp"
        return None

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
        system, converted = self._convert_messages(messages)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self._get_max_tokens(),
            "messages": converted,
            "timeout": self.get_timeout(timeout),
        }
        # Only send ``temperature`` when the user set it explicitly: it carries a
        # default (0.0) that can't be distinguished from an intentional value,
        # and newer Claude models reject/deprecate the parameter outright.
        if "temperature" in getattr(self.config, "model_fields_set", set()):
            kwargs["temperature"] = self.config.temperature
        if system:
            kwargs["system"] = system
        # raise_if_empty is a control flag for the caller, never a wire param.
        extra_kwargs.pop("raise_if_empty", None)
        if "tools" in extra_kwargs:
            tools = extra_kwargs.pop("tools")
            if tools:
                kwargs["tools"] = self._convert_tools(tools)
        if extra_kwargs.get("tool_choice") is not None:
            kwargs["tool_choice"] = self._convert_tool_choice(extra_kwargs.pop("tool_choice"))
        else:
            extra_kwargs.pop("tool_choice", None)
        kwargs.update(extra_kwargs)
        return kwargs

    # -- completion calls ---------------------------------------------------
    async def _acreate(self, **kwargs):
        try:
            return await self.aclient.messages.create(**kwargs)
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

    async def _achat_completion_stream(
        self, messages: list[dict], timeout=USE_CONFIG_TIMEOUT, raise_if_empty: bool = True, **chat_configs
    ) -> str:
        kwargs = self._cons_kwargs(messages, timeout=self.get_timeout(timeout), **chat_configs)
        collected: list[str] = []
        usage = None
        try:
            async with self.aclient.messages.stream(**kwargs) as stream:
                async for text in stream.text_stream:
                    log_llm_stream(text)
                    collected.append(text)
                final_message = await stream.get_final_message()
                usage = getattr(final_message, "usage", None)
        except Exception as e:
            raise classify_llm_error(e) or e

        log_llm_stream("\n")
        full_reply_content = "".join(collected)
        if raise_if_empty and not full_reply_content:
            raise LLMEmptyResponseError("The LLM's response is empty.")
        self._update_costs(usage)
        return full_reply_content

    async def _achat_completion_stream_tool(
        self, messages: list[dict], timeout=USE_CONFIG_TIMEOUT, raise_if_empty: bool = True, **chat_configs
    ):
        """Streaming native tool-use completion: stream text, return the full Message.

        Anthropic's streaming context assembles the complete ``Message`` (text +
        ``tool_use`` blocks) as it consumes the event stream, so we stream the text
        deltas to ``log_llm_stream`` for the live console and return the assembled
        message — letting ``get_choice_text`` / ``get_choice_tool_calls`` parse the
        tool calls exactly as in the blocking path.
        """
        kwargs = self._cons_kwargs(messages, timeout=self.get_timeout(timeout), **chat_configs)
        final_message = None
        try:
            async with self.aclient.messages.stream(**kwargs) as stream:
                async for text in stream.text_stream:
                    log_llm_stream(text)
                final_message = await stream.get_final_message()
        except Exception as e:
            raise classify_llm_error(e) or e

        log_llm_stream("\n")
        if raise_if_empty and not (self.get_choice_text(final_message) or self.get_choice_tool_calls(final_message)):
            raise LLMEmptyResponseError("The LLM's response is empty.")
        self._update_costs(getattr(final_message, "usage", None))
        return final_message

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

    # -- token / usage helpers ---------------------------------------------
    def _get_max_tokens(self) -> int:
        # Anthropic requires an explicit, positive max_tokens on every request.
        return self.max_completion_token or 4096

    def _calc_usage(self, messages: list[dict], rsp: str):
        from metagpt.router.cost import TokenUsage

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
