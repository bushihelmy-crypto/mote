#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LoginCallbacks: UI hooks a CLI/TUI provides to drive an interactive login.

Mirrors pi's ``OAuthLoginCallbacks``. All hooks are optional; flows call the
small wrapper methods which no-op when a hook isn't supplied, so flows never
have to None-check.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from mote.runtime.models.auth.oauth.models import DeviceCodeInfo


@dataclass
class LoginCallbacks:
    """Presentation hooks for interactive OAuth flows.

    - ``on_url``: receive the authorization URL to open in a browser
      (authorization_code flow).
    - ``on_device_code``: receive the device/user code + verification URL
      (device_code flow).
    - ``on_progress``: receive human-readable progress messages.
    """

    on_url: Optional[Callable[[str], None]] = None
    on_device_code: Optional[Callable[[DeviceCodeInfo], None]] = None
    on_progress: Optional[Callable[[str], None]] = None

    def url(self, authorize_url: str) -> None:
        if self.on_url:
            self.on_url(authorize_url)

    def device_code(self, info: DeviceCodeInfo) -> None:
        if self.on_device_code:
            self.on_device_code(info)

    def progress(self, message: str) -> None:
        if self.on_progress:
            self.on_progress(message)


__all__ = ["LoginCallbacks"]
