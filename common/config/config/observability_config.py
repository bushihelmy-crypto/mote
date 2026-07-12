#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Observability: error tracking (Sentry) and LLM tracing (Langfuse)."""
from __future__ import annotations

from pydantic import Field

from mote.common.config.config.langfuse_config import LangfuseConfig
from mote.common.config.config.sentry_config import SentryConfig
from mote.common.utils.yaml_model import YamlModel


class ObservabilityConfig(YamlModel):
    """Groups the error-tracking and tracing backends under one section."""

    sentry: SentryConfig = Field(default_factory=SentryConfig)
    langfuse: LangfuseConfig = Field(default_factory=LangfuseConfig)
