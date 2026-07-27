#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ref1: https://openai.com/pricing
ref2: https://github.com/openai/openai-cookbook/blob/main/examples/How_to_count_tokens_with_tiktoken.ipynb
ref3: https://github.com/Significant-Gravitas/Auto-GPT/blob/master/autogpt/llm/token_counter.py
ref4: https://github.com/hwchase17/langchain/blob/master/langchain/chat_models/openai.py
ref5: https://ai.google.dev/models/gemini
"""
import warnings
from typing import Optional

import tiktoken

TOKEN_COSTS = {
    "anthropic/claude-3.5-sonnet": {"prompt": 0.003, "completion": 0.015},
    "gpt-3.5-turbo": {"prompt": 0.0015, "completion": 0.002},
    "gpt-3.5-turbo-0301": {"prompt": 0.0015, "completion": 0.002},
    "gpt-3.5-turbo-0613": {"prompt": 0.0015, "completion": 0.002},
    "gpt-3.5-turbo-16k": {"prompt": 0.003, "completion": 0.004},
    "gpt-3.5-turbo-16k-0613": {"prompt": 0.003, "completion": 0.004},
    "gpt-35-turbo": {"prompt": 0.0015, "completion": 0.002},
    "gpt-35-turbo-16k": {"prompt": 0.003, "completion": 0.004},
    "gpt-3.5-turbo-1106": {"prompt": 0.001, "completion": 0.002},
    "gpt-3.5-turbo-0125": {"prompt": 0.001, "completion": 0.002},
    "gpt-4-0314": {"prompt": 0.03, "completion": 0.06},
    "gpt-4": {"prompt": 0.03, "completion": 0.06},
    "gpt-4-32k": {"prompt": 0.06, "completion": 0.12},
    "gpt-4-32k-0314": {"prompt": 0.06, "completion": 0.12},
    "gpt-4-0613": {"prompt": 0.06, "completion": 0.12},
    "gpt-4-turbo-preview": {"prompt": 0.01, "completion": 0.03},
    "gpt-4-0125-preview": {"prompt": 0.01, "completion": 0.03},
    "gpt-4-1106-preview": {"prompt": 0.01, "completion": 0.03},
    "gpt-4-vision-preview": {"prompt": 0.01, "completion": 0.03},  # TODO add extra image price calculator
    "gpt-4-1106-vision-preview": {"prompt": 0.01, "completion": 0.03},
    "gpt-4o": {"prompt": 0.005, "completion": 0.015},
    "gpt-4o-2024-05-13": {"prompt": 0.005, "completion": 0.015},
    "gpt-4o-mini": {"prompt": 0.00015, "completion": 0.0006},
    "gpt-4o-mini-2024-07-18": {"prompt": 0.00015, "completion": 0.0006},
    "text-embedding-ada-002": {"prompt": 0.0004, "completion": 0.0},
    "glm-3-turbo": {"prompt": 0.0007, "completion": 0.0007},  # 128k version, prompt + completion tokens=0.005￥/k-tokens
    "glm-4": {"prompt": 0.014, "completion": 0.014},  # 128k version, prompt + completion tokens=0.1￥/k-tokens
    "gemini-pro": {"prompt": 0.00025, "completion": 0.0005},
    "moonshot-v1-8k": {"prompt": 0.012, "completion": 0.012},  # prompt + completion tokens=0.012￥/k-tokens
    "moonshot-v1-32k": {"prompt": 0.024, "completion": 0.024},
    "moonshot-v1-128k": {"prompt": 0.06, "completion": 0.06},
    "open-mistral-7b": {"prompt": 0.00025, "completion": 0.00025},
    "open-mixtral-8x7b": {"prompt": 0.0007, "completion": 0.0007},
    "mistral-small-latest": {"prompt": 0.002, "completion": 0.006},
    "mistral-medium-latest": {"prompt": 0.0027, "completion": 0.0081},
    "mistral-large-latest": {"prompt": 0.008, "completion": 0.024},
    "claude-instant-1.2": {"prompt": 0.0008, "completion": 0.0024},
    "claude-2.0": {"prompt": 0.008, "completion": 0.024},
    "claude-2.1": {"prompt": 0.008, "completion": 0.024},
    "claude-3-sonnet-20240229": {"prompt": 0.003, "completion": 0.015},
    "claude-3-opus-20240229": {"prompt": 0.015, "completion": 0.075},
    "claude-4-sonnet": {"prompt": 0.003, "completion": 0.015},
    "yi-34b-chat-0205": {"prompt": 0.0003, "completion": 0.0003},
    "yi-34b-chat-200k": {"prompt": 0.0017, "completion": 0.0017},
    "openai/gpt-4": {"prompt": 0.03, "completion": 0.06},  # start, for openrouter
    "openai/gpt-4-turbo": {"prompt": 0.01, "completion": 0.03},
    "openai/gpt-4o": {"prompt": 0.005, "completion": 0.015},
    "openai/gpt-4o-2024-05-13": {"prompt": 0.005, "completion": 0.015},
    "openai/gpt-4o-mini": {"prompt": 0.00015, "completion": 0.0006},
    "openai/gpt-4o-mini-2024-07-18": {"prompt": 0.00015, "completion": 0.0006},
    "google/gemini-pro-1.5": {"prompt": 0.0025, "completion": 0.0075},
    "google/gemini-flash-1.5": {"prompt": 0.00025, "completion": 0.00075},
    "deepseek/deepseek-coder": {"prompt": 0.00014, "completion": 0.00028},
    "deepseek/deepseek-chat": {"prompt": 0.00014, "completion": 0.00028},  # end, for openrouter
    "deepseek-chat": {"prompt": 0.00014, "completion": 0.00028},
    "deepseek-coder": {"prompt": 0.00014, "completion": 0.00028},
}


FIREWORKS_GRADE_TOKEN_COSTS = {
    "-1": {"prompt": 0.0, "completion": 0.0},  # abnormal condition
    "16": {"prompt": 0.2, "completion": 0.8},  # 16 means model size <= 16B; 0.2 means $0.2/1M tokens
    "80": {"prompt": 0.7, "completion": 2.8},  # 80 means 16B < model size <= 80B
    "mixtral-8x7b": {"prompt": 0.4, "completion": 1.6},
}

# https://platform.openai.com/docs/models/gpt-4-and-gpt-4-turbo
TOKEN_MAX = {
    "gpt-4o-2024-05-13": 128000,
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000,
    "gpt-4o-mini-2024-07-18": 128000,
    "gpt-4-0125-preview": 128000,
    "gpt-4-turbo-preview": 128000,
    "gpt-4-1106-preview": 128000,
    "gpt-4-vision-preview": 128000,
    "gpt-4-1106-vision-preview": 128000,
    "gpt-4-turbo": 128000,
    "gpt-4": 8192,
    "gpt-4-0613": 8192,
    "gpt-4-32k": 32768,
    "gpt-4-32k-0613": 32768,
    "gpt-3.5-turbo-0125": 16385,
    "gpt-3.5-turbo": 16385,
    "gpt-3.5-turbo-1106": 16385,
    "gpt-3.5-turbo-instruct": 4096,
    "gpt-3.5-turbo-16k": 16385,
    "gpt-3.5-turbo-0613": 4096,
    "gpt-3.5-turbo-16k-0613": 16385,
    "text-embedding-ada-002": 8192,
    "text-embedding-3-small": 8192,
    "glm-3-turbo": 128000,
    "glm-4": 128000,
    "gemini-pro": 32768,
    "moonshot-v1-8k": 8192,
    "moonshot-v1-32k": 32768,
    "moonshot-v1-128k": 128000,
    "open-mistral-7b": 8192,
    "open-mixtral-8x7b": 32768,
    "mistral-small-latest": 32768,
    "mistral-medium-latest": 32768,
    "mistral-large-latest": 32768,
    "claude-instant-1.2": 100000,
    "claude-2.0": 100000,
    "claude-2.1": 200000,
    "claude-3-sonnet-20240229": 200000,
    "claude-3-opus-20240229": 200000,
    "yi-34b-chat-0205": 4000,
    "yi-34b-chat-200k": 200000,
    "openai/gpt-4": 8192,  # start, for openrouter
    "openai/gpt-4-turbo": 128000,
    "openai/gpt-4o": 128000,
    "openai/gpt-4o-2024-05-13": 128000,
    "openai/gpt-4o-mini": 128000,
    "openai/gpt-4o-mini-2024-07-18": 128000,
    "anthropic/claude-3.5-sonnet": 200000,
    "google/gemini-pro-1.5": 2800000,
    "google/gemini-flash-1.5": 2800000,
    "deepseek/deepseek-coder": 128000,
    "deepseek/deepseek-chat": 128000,  # end, for openrouter
    "deepseek-chat": 128000,
    "deepseek-coder": 128000,
    "deepseek-ai/DeepSeek-Coder-V2-Instruct": 32000,  # siliconflow
    "claude-3-5-sonnet-v2": 200000,  # aws
    "claude-3-7-sonnet": 200000,  # aws
    "claude-4-sonnet": 200000,  # aws
}


def count_message_tokens(messages, model: Optional[str] = "gpt-3.5-turbo-0125"):
    """Return the number of tokens used by a list of messages."""
    model = model or "gpt-3.5-turbo-0125"
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        # logger.debug("model not found. Using cl100k_base encoding.")
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
        tokens_per_message = 3  # # every reply is primed with <|start|>assistant<|message|>
        tokens_per_name = 1
    elif model == "gpt-3.5-turbo-0301":
        tokens_per_message = 4  # every message follows <|start|>{role/name}\n{content}<|end|>\n
        tokens_per_name = -1  # if there's a name, the role is omitted
    elif "gpt-3.5-turbo" == model:
        warnings.warn(
            "gpt-3.5-turbo may update over time; using gpt-3.5-turbo-0125 token rules",
            RuntimeWarning,
            stacklevel=2,
        )
        return count_message_tokens(messages, model="gpt-3.5-turbo-0125")
    elif "gpt-4" == model:
        warnings.warn("gpt-4 may update over time; using gpt-4-0613 token rules", RuntimeWarning, stacklevel=2)
        return count_message_tokens(messages, model="gpt-4-0613")
    elif "open-llm-model" == model:
        """
        For self-hosted open_llm api, they include lots of different models. The message tokens calculation is
        inaccurate. It's a reference result.
        """
        tokens_per_message = 0  # ignore conversation message template prefix
        tokens_per_name = 0
    else:
        # raise NotImplementedError(
        #     f"num_tokens_from_messages() is not implemented for model {model}. "
        #     f"See https://cookbook.openai.com/examples/how_to_count_tokens_with_tiktoken "
        #     f"for information on how messages are converted to tokens."
        # )
        # logger.debug(f"num_tokens_from_messages() is not implemented for model {model}. Using default values.")
        # Using OpenAI's token calculation method (tiktoken) to approximate token count for all models
        # Default to use gpt-4o's token calculation rules:
        tokens_per_message = 3
        tokens_per_name = 1
    num_tokens = 0
    for message in messages:
        num_tokens += tokens_per_message
        for key, value in message.items():
            content = value
            if isinstance(value, list):
                # for gpt-4v
                for item in value:
                    if isinstance(item, dict) and item.get("type") in ["text"]:
                        content = item.get("text", "")
            # Handle special tokens like <|endoftext|> that may appear in user messages
            # tiktoken by default disallows these tokens to prevent unintended behavior
            try:
                num_tokens += len(encoding.encode(content))
            except ValueError:
                # If encoding fails due to special tokens, allow them as regular text
                num_tokens += len(encoding.encode(content, allowed_special="all"))
            if key == "name":
                num_tokens += tokens_per_name

    num_tokens += 3  # every reply is primed with <|start|>assistant<|message|>

    if model.startswith(("claude", "anthropic")):
        # Testing shows Claude's input tokens are 20%+ higher than cl100k_base counts.
        # We're using the observed peak ratio of 1.26 to correct Claude's token estimation.
        # logger.warning("Use cl100k_base * 1.26 to estimate the input tokens of the claude model.")
        num_tokens = num_tokens * 1.26
    return int(num_tokens)


def count_string_tokens(string: str, model_name: Optional[str]) -> int:
    """
    Returns the number of tokens in a text string.

    Args:
        string (str): The text string.
        model_name (str): The name of the encoding to use. (e.g., "gpt-3.5-turbo")

    Returns:
        int: The number of tokens in the text string.
    """
    model_name = model_name or "gpt-3.5-turbo-0125"
    try:
        encoding = tiktoken.encoding_for_model(model_name)
    except KeyError:
        encoding = tiktoken.get_encoding("cl100k_base")
    # Handle special tokens like <|endoftext|> that may appear in text
    # tiktoken by default disallows these tokens to prevent unintended behavior
    try:
        return len(encoding.encode(string))
    except ValueError:
        # If encoding fails due to special tokens, allow them as regular text
        return len(encoding.encode(string, allowed_special="all"))


def get_max_completion_tokens(messages: list[dict], model: Optional[str], default: int) -> int:
    """Calculate the maximum number of completion tokens for a given model and list of messages.

    Args:
        messages: A list of messages.
        model: The model name.

    Returns:
        The maximum number of completion tokens.
    """
    if model not in TOKEN_MAX:
        return default
    return TOKEN_MAX[model] - count_message_tokens(messages, model) - 1
