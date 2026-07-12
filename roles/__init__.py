#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2023/5/11 14:43
@Author  : alexanderwu
@File    : __init__.py
"""

from metagpt.roles.role import Role
from metagpt.roles.role_schema import RoleSchema
from metagpt.roles.role_state import RoleState

# Side-effect import: registers the concrete read-only child-Role builder into
# the common-layer holder so the executor can build helper agents without
# importing the roles stack (keeps executor a true leaf w.r.t. roles).
from metagpt.roles import child_role  # noqa: F401


__all__ = [
    "Role",
    "RoleSchema",
    "RoleState"
]
