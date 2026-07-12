#!/usr/bin/env python
# -*- coding: utf-8 -*-

# NOTE: keep this package ``__init__`` a LEAF — import only lightweight,
# self-contained submodules here. Re-exporting heavyweight bases (e.g.
# ``base.singleton.Singleton``) used to pull the ``common.base`` → ``llm_config``
# chain into every ``import mote.common.utils.<anything>``; since ``llm_config``
# imports ``common.utils`` (YamlModel), that formed an import cycle. Callers use
# the submodule paths directly, so nothing heavyweight is re-exported here.
# ``count_string_tokens`` IS used via the package path, so token_counter stays.
from mote.common.utils.token_counter import TOKEN_COSTS, count_message_tokens, count_string_tokens

__all__ = [
    "TOKEN_COSTS",
    "count_message_tokens",
    "count_string_tokens",
]
