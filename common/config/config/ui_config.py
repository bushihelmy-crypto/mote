#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Human display-layer UI configuration (currently just the display language)."""
from pydantic import Field

from mote.common.utils.yaml_model import YamlModel


class UIConfig(YamlModel):
    """Human-facing display preferences (does not affect model-facing text)."""

    language: str = Field(
        default="auto",
        description=(
            "Human display language: 'auto' (infer from LC_ALL/LC_MESSAGES/LANG), "
            "'en', 'zh', … Model-facing text stays English regardless."
        ),
    )
