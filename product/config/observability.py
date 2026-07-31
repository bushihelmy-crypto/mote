#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Observability: error tracking (Sentry) and LLM tracing (Langfuse)."""
from __future__ import annotations

from pydantic import Field

from mote.product.config.base import ConfigModel as YamlModel
from mote.runtime.config.langfuse import LangfuseConfig
from mote.runtime.config.sentry import SentryConfig


class ObservabilityConfig(YamlModel):
    """Groups the error-tracking and tracing backends under one section."""

    sentry: SentryConfig = Field(default_factory=SentryConfig)
    langfuse: LangfuseConfig = Field(default_factory=LangfuseConfig)
