"""Model-independent local token estimation used by inference semantics."""

from __future__ import annotations

import warnings
from typing import Any, Optional

import tiktoken


def count_message_tokens(messages: list[dict[str, Any]], model: Optional[str] = "gpt-3.5-turbo-0125") -> int:
    """Estimate tokens used by provider-neutral messages."""
    model = model or "gpt-3.5-turbo-0125"
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        encoding = tiktoken.get_encoding("cl100k_base")
    if model in {
        "gpt-3.5-turbo-0613",
        "gpt-3.5-turbo-16k-0613",
        "gpt-35-turbo",
        "gpt-35-turbo-16k",
        "gpt-3.5-turbo-16k",
        "gpt-3.5-turbo-1106",
        "gpt-3.5-turbo-0125",
        "gpt-4-0314",
        "gpt-4-32k-0314",
        "gpt-4-0613",
        "gpt-4-32k-0613",
        "gpt-4-turbo-preview",
        "gpt-4-0125-preview",
        "gpt-4-1106-preview",
        "gpt-4-vision-preview",
        "gpt-4-1106-vision-preview",
        "gpt-4o-2024-05-13",
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4o-mini-2024-07-18",
    }:
        tokens_per_message, tokens_per_name = 3, 1
    elif model == "gpt-3.5-turbo-0301":
        tokens_per_message, tokens_per_name = 4, -1
    elif model == "gpt-3.5-turbo":
        warnings.warn(
            "gpt-3.5-turbo may update over time; using gpt-3.5-turbo-0125 token rules", RuntimeWarning, stacklevel=2
        )
        return count_message_tokens(messages, model="gpt-3.5-turbo-0125")
    elif model == "gpt-4":
        warnings.warn("gpt-4 may update over time; using gpt-4-0613 token rules", RuntimeWarning, stacklevel=2)
        return count_message_tokens(messages, model="gpt-4-0613")
    elif model == "open-llm-model":
        tokens_per_message, tokens_per_name = 0, 0
    else:
        tokens_per_message, tokens_per_name = 3, 1
    num_tokens = 0
    for message in messages:
        num_tokens += tokens_per_message
        for key, value in message.items():
            content = value
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and item.get("type") == "text":
                        content = item.get("text", "")
            try:
                num_tokens += len(encoding.encode(content))
            except ValueError:
                num_tokens += len(encoding.encode(content, allowed_special="all"))
            if key == "name":
                num_tokens += tokens_per_name
    num_tokens += 3
    if model.startswith(("claude", "anthropic")):
        num_tokens *= 1.26
    return int(num_tokens)


def count_string_tokens(string: str, model_name: Optional[str]) -> int:
    """Estimate tokens in a text string."""
    model_name = model_name or "gpt-3.5-turbo-0125"
    try:
        encoding = tiktoken.encoding_for_model(model_name)
    except KeyError:
        encoding = tiktoken.get_encoding("cl100k_base")
    try:
        return len(encoding.encode(string))
    except ValueError:
        return len(encoding.encode(string, allowed_special="all"))


__all__ = ["count_message_tokens", "count_string_tokens"]
