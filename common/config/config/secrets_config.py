#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Secret redaction / vault configuration.

Opt-in (``enabled=False`` by default): when off, no secret subscriber is wired
and the redaction/upload seam is entirely absent. When on, the two subscribers
drive one encrypted vault whose key comes from the selected ``cipher`` strategy.
"""
from typing import Optional

from pydantic import Field

from mote.common.utils.yaml_model import YamlModel


class SecretsConfig(YamlModel):
    """Secret system knobs (all model-agnostic; purely a hygiene layer)."""

    enabled: bool = Field(
        default=False,
        description="Wire the secret redaction/upload seam. Off = no subscriber, no vault touched.",
    )
    cipher: str = Field(
        default="aes",
        description="Vault key/cipher strategy name. 'aes' = AES-256-GCM with a ~/.mote/vault.key key file.",
    )
    vault_path: Optional[str] = Field(
        default=None,
        description="Override the encrypted vault file location (default ~/.mote/secrets.json).",
    )
