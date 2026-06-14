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


__all__ = [
    "Role",
    "RoleSchema",
    "RoleState"
]
