#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Side-effect import: registers the concrete read-only child-Role builder into
# the common-layer holder so the executor can build helper agents without
# importing the roles stack (keeps executor a true leaf w.r.t. roles).
from mote.roles import child_role  # noqa: F401
from mote.roles.role import Role
from mote.roles.role_schema import RoleSchema
from mote.roles.role_state import RoleState

__all__ = ["Role", "RoleSchema", "RoleState"]
