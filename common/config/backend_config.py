#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Desc   : backend config used for agent

from enum import Enum
from typing import Literal, Union

from pydantic import Field

from metagpt.common.config.funcsea_config import FuncseaConfig
from metagpt.common.config.supabase_config import SupabaseConfig
from metagpt.common.utils.yaml_model import YamlModel


class BackendType(str, Enum):
    FUNCSEA = "funcsea"
    SUPABASE = "supabase"


class BackendConfig(YamlModel):
    backend_type: Literal[BackendType.FUNCSEA, BackendType.SUPABASE, ""] = Field(
        default="", description="The backend type"
    )
    config: Union[FuncseaConfig, SupabaseConfig, None] = Field(default=None, description="The backend config")
