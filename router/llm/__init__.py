#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LLM abstraction layer (formerly metagpt.provider)."""

from metagpt.router.llm.openai_api import OpenAILLM
from metagpt.router.llm.base_llm import BaseLLM

__all__ = [
    "OpenAILLM",
    "BaseLLM",
]
