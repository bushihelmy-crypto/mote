#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Control-plane exceptions, re-exported for the ``environment`` package.

The canonical definitions live in :mod:`metagpt.common.exception`; this module
re-exports them so callers (and tests) can import them from the environment
package without pulling the package ``__init__`` into a control-plane cycle.
"""

from __future__ import annotations

from metagpt.common.exception import (
    AgentControlError,
    AgentLimitReached,
    AgentNotFound,
    AgentNotKnown,
    AgentPathExists,
)

__all__ = [
    "AgentControlError",
    "AgentLimitReached",
    "AgentNotFound",
    "AgentNotKnown",
    "AgentPathExists",
]
