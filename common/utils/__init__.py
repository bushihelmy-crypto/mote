#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2023/4/29 15:50
@Author  : alexanderwu
@File    : __init__.py
"""

# NOTE: keep this package ``__init__`` a LEAF — import only lightweight,
# self-contained submodules here. It used to eagerly re-export
# ``base.singleton.Singleton`` (and ``read_document.read_docx``), which pulled the
# heavyweight ``common.base`` → ``postprocess_plugin`` → ``config2`` → ``llm_config``
# chain into every ``import mote.common.utils.<anything>``. Since ``llm_config``
# imports ``common.utils`` (YamlModel), that formed a ``llm_config ↔ config2`` import
# cycle. Neither ``Singleton`` nor ``read_docx`` had any package-level importer
# (callers use the submodule paths directly), so they were dropped from here.
# ``count_string_tokens`` IS used via the package path, so token_counter stays.
from mote.common.utils.token_counter import TOKEN_COSTS, count_message_tokens, count_string_tokens

__all__ = [
    "TOKEN_COSTS",
    "count_message_tokens",
    "count_string_tokens",
]
