"""Workspace layout ownership.

:class:`SessionWorkspace` is the single owner of the on-disk workspace layout (all
per-session artifacts co-located under one session directory). Destructive
retention is owned by the Session and Artifact lifecycle services.
"""

from mote.runtime.session.layout import SessionLayout
from mote.runtime.session.workspace.store import SessionSpace, SessionWorkspace

__all__ = [
    "SessionLayout",
    "SessionSpace",
    "SessionWorkspace",
]
