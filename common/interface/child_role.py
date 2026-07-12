"""ChildRoleBuilder protocol — the read-only child-Role construction slice.

The narrow face the code-review pipeline (and any other ``executor`` caller that
needs a short-lived helper agent) uses to construct a read-only, bypass-permission
child :class:`Role` *without importing the concrete ``roles`` stack*.

Why a Protocol + registration holder here (not a direct import): the ``executor``
layer must never import the ``roles`` layer (the strict downward-only layering
rule). Building a Role is inherently a ``roles`` concern, so the concrete builder
lives in ``roles`` and *registers itself* into this common-layer holder at import
time (mirroring the polymorphic ``BaseRole`` registry and ``agent_registry``).
The executor calls :func:`build_child_role` through this module, so no upward
import — at import time *or* runtime — is introduced.

Like the other ``common.interface`` Protocols (e.g.
:class:`~metagpt.common.interface.FileSnapshotStore`), this is a leaf module that
imports only ``typing``, importable from anywhere without risking a cycle.
"""

from __future__ import annotations

from typing import Any, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class ChildRoleBuilder(Protocol):
    """Builds an unstarted, read-only, bypass-permission child role.

    Implemented in the ``roles`` layer and registered via
    :func:`register_child_role_builder`. Returns an unstarted role ready to be
    spawned through the control plane (``spawn_and_run``).
    """

    def __call__(
        self,
        *,
        name: str,
        system_prompt: str,
        repo_dir: str = "",
        parent_session_id: str = "",
        tools: Optional[List[str]] = None,
    ) -> Any:
        ...


_builder: Optional[ChildRoleBuilder] = None


def register_child_role_builder(builder: ChildRoleBuilder) -> None:
    """Register the concrete child-role builder (called once by ``roles`` at import)."""
    global _builder
    _builder = builder


def build_child_role(
    *,
    name: str,
    system_prompt: str,
    repo_dir: str = "",
    parent_session_id: str = "",
    tools: Optional[List[str]] = None,
) -> Any:
    """Build a read-only child role through the registered ``roles`` builder.

    Raises:
        RuntimeError: if no builder has been registered. In production the
            ``roles`` package is always imported before any child is built (the
            pipeline runs inside a live agent), so a missing builder is a wiring
            bug rather than a recoverable condition.
    """
    if _builder is None:
        raise RuntimeError(
            "build_child_role: no ChildRoleBuilder registered; import "
            "`metagpt.roles` so its concrete builder registers itself."
        )
    return _builder(
        name=name,
        system_prompt=system_prompt,
        repo_dir=repo_dir,
        parent_session_id=parent_session_id,
        tools=tools,
    )


__all__ = [
    "ChildRoleBuilder",
    "register_child_role_builder",
    "build_child_role",
]
