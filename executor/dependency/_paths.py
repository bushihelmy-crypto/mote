"""Shared relative-path resolution for the stateless file tools.

Read / Write / Edit / Glob / Grep all resolve a model-supplied
path the same way: an absolute path is used as-is, and a relative path is
resolved against the session's *stable* working directory (Codex-aligned — the
cwd is a fixed data value, not shell state that drifts with ``cd``).

The base directory comes from the Role's ``get_cwd`` capability, injected by
``bind()``. When a tool is used unbound (no Role injected the capability — some
unit tests and standalone use), it falls back to the process cwd
(``os.getcwd()``) so the tool keeps working standalone.
"""
from __future__ import annotations

import os
from typing import Callable, Optional

# A zero-arg callable returning the Role's stable working directory.
CwdProvider = Callable[[], str]


def base_cwd(get_cwd: Optional[CwdProvider]) -> str:
    """The stable base dir for default roots / relativization.

    ``get_cwd()`` when the capability is injected, else the process cwd (so a
    tool used unbound — some unit tests and standalone use — keeps working).
    """
    base = get_cwd() if get_cwd is not None else None
    return base or os.getcwd()


def resolve_path(get_cwd: Optional[CwdProvider], path: str) -> str:
    """Resolve ``path`` to an absolute path against the stable working directory.

    Absolute paths (after ``~`` expansion) are returned normalized as-is;
    relative paths are joined onto the base directory. The base directory is
    ``get_cwd()`` when the capability is injected, else the process cwd.
    """
    expanded = os.path.expanduser(path)
    if os.path.isabs(expanded):
        return os.path.abspath(expanded)
    return os.path.abspath(os.path.join(base_cwd(get_cwd), expanded))


__all__ = ["base_cwd", "resolve_path", "CwdProvider"]
