#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tool-facing runtime knobs (network + browser fingerprint)."""
from __future__ import annotations

from mote.common.utils.yaml_model import YamlModel


class ToolsConfig(YamlModel):
    """Settings shared by tools (not by the LLM clients)."""

    # Global proxy for tools such as the browser (the LLM clients use
    # ``models.default.proxy`` instead). Keep it consistent with the
    # ``browser_locale`` exit-IP region (a zh-CN locale on a US IP is a bot tell).
    proxy: str = ""

    # Browser locale/region bundle for the WebBrowser stealth fingerprint:
    # "auto" (default) infers zh vs en from the host env; "en" / "zh" force a
    # coherent locale + timezone + Accept-Language. A per-role
    # ``role_schema.browser_locale`` (when not "auto") overrides this.
    browser_locale: str = "auto"
