#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Model configuration: the default LLM, task-routed LLMs, and routing switch.

Groups everything about *which model runs* under one roof:

- ``default``       — the main LLM every request uses unless routed elsewhere.
- ``tasks``         — task-name -> LLMConfig overrides (e.g. ``compression`` /
                      ``summary``); each inherits transport + credentials from
                      ``default`` unless the user set them explicitly.
- ``router_enabled``— when True the LLMRouter picks a model per request from the
                      registered cards; when False ``default`` is a fixed model.
- ``api_key_helper``— shell command that prints an API key on stdout, used to
                      fill ``default.api_key`` at load time when no static/env
                      key is present (trusted layers only; see CREDENTIAL_DENYLIST).
"""
from __future__ import annotations

from typing import Dict

from pydantic import Field, model_validator

from mote.common.config.config.llm_config import LLMConfig
from mote.common.utils.yaml_model import YamlModel


def _default_tasks() -> Dict[str, LLMConfig]:
    """Built-in task-routed models. Keys are router task names."""
    return {
        "compression": LLMConfig(model="claude-sonnet-4-8"),
        "summary": LLMConfig(model="claude-sonnet-4-8"),
    }


class ModelsConfig(YamlModel):
    """All LLM selection knobs (default model, task overrides, routing switch)."""

    default: LLMConfig

    # Intelligent LLM routing. When True, the router picks a model per request
    # from the registered model cards (ContextProvider triggers it in the react
    # loop); when False, ``default`` is used as a fixed model.
    router_enabled: bool = False

    # Optional shell command that prints an API key on stdout. Used to fill
    # ``default.api_key`` at load time only when no static/env key is present
    # (precedence: env > static config > helper). Trusted layers only — the
    # untrusted workdir layer cannot inject it (see CREDENTIAL_DENYLIST).
    api_key_helper: str = ""

    # Forced reply language. When set, the system prompt's ``# Language`` section
    # tells the model to always respond in this language regardless of the user's
    # input language; empty means no override (mirror the user). Single source for
    # response language (replaces the old per-role ``RoleSchema.language``).
    response_language: str = "chinese"

    # Task-name -> per-task model override. The router registers each as a card
    # named by its task and maps the task to it. Transport + credentials inherit
    # from ``default`` unless set explicitly (see ``_inherit_task_transport``).
    tasks: Dict[str, LLMConfig] = Field(default_factory=_default_tasks)

    @model_validator(mode="after")
    def _inherit_task_transport(self):
        """Let each task-routed llm inherit transport/credentials from ``default``.

        By default only the model differs on a task override; the endpoint, key
        and api type come from ``default`` unless the user set them explicitly.
        """
        base = LLMConfig()
        for task_llm in self.tasks.values():
            if task_llm.base_url == base.base_url:
                task_llm.base_url = self.default.base_url
            if task_llm.api_key == base.api_key:
                task_llm.api_key = self.default.api_key
            if task_llm.api_type == base.api_type:
                task_llm.api_type = self.default.api_type
        return self
