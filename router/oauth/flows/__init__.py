#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Interactive OAuth login flows (authorization_code + PKCE, device_code)."""
from __future__ import annotations

from metagpt.router.oauth.flows.auth_code import run_auth_code_flow
from metagpt.router.oauth.flows.callbacks import LoginCallbacks
from metagpt.router.oauth.flows.device_code import run_device_code_flow

__all__ = ["LoginCallbacks", "run_auth_code_flow", "run_device_code_flow"]
