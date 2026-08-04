#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Product vault configuration.

Executable credential resolution is owned by the model credential-source
catalog during application activation, never by config parsing.
"""

from typing import Optional

from pydantic import Field

from mote.contracts.config.base import ConfigModel as YamlModel


class SecretsConfig(YamlModel):
    """Storage knobs; core prompt/result protection cannot be disabled."""

    vault_path: Optional[str] = Field(
        default=None,
        description="Override the encrypted vault file location (default ~/.mote/secrets.json).",
    )
    secrets_config_path: Optional[str] = Field(
        default=None,
        description=(
            "Override the plaintext, human-edited named-secret file location "
            "(default ~/.mote/secrets_config.json). A flat {name: value} JSON; "
            "edits/deletes hot-sync into the encrypted vault by mtime."
        ),
    )
