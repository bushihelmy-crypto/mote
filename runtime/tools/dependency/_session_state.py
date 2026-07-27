#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Shared env-state diffing for persistent sessions (kernel / terminal).

The Jupyter :class:`~mote.runtime.tools.dependency._kernel.KernelSession` and the PTY
:class:`~mote.runtime.tools.dependency._terminal.TerminalSession` both snapshot a
launch-baseline environment and, on demand, diff the live environment against it
to persist ``(cwd, env_diff, unset)``. The *probe* differs per backend (a Python
cell vs a shell command) but the diff arithmetic that follows is identical — this
module holds that one shared step so the two sessions never drift.
"""

from __future__ import annotations

from typing import AbstractSet, Optional, Tuple


def diff_env_state(
    probed: Optional[Tuple[str, dict[str, str]]],
    baseline_env: dict[str, str],
    noise_keys: AbstractSet[str],
) -> Optional[Tuple[str, dict[str, str], list[str]]]:
    """Diff a freshly probed ``(cwd, env)`` against the launch baseline.

    Returns ``(cwd, env_diff, unset)`` where ``env_diff`` holds keys added or
    changed since launch and ``unset`` holds keys that were present at launch but
    are now gone. Keys in *noise_keys* (per-process / launch bookkeeping) are
    filtered from both. ``probed is None`` (the backend probe failed) → ``None``,
    so the caller stays best-effort.
    """
    if probed is None:
        return None
    cwd, env = probed
    diff: dict[str, str] = {}
    for key, value in env.items():
        if key in noise_keys:
            continue
        if baseline_env.get(key) != value:
            diff[key] = value
    unset = [key for key in baseline_env if key not in env and key not in noise_keys]
    return (cwd, diff, unset)


__all__ = ["diff_env_state"]
