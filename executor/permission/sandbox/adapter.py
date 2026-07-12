"""Sandbox adapter — translate our policy objects into runtime inputs.

The *adapter* layer (this module + ``guard.py``) binds the runtime
(``metagpt.sandbox``) to our domain types. It is the only place that knows about
BOTH the executor's ``SandboxGuard`` / config AND the runtime's
``SandboxPolicy`` — keeping the runtime product-agnostic (it depends only on
``common``) and the executor unaware of bwrap argv details.

Two responsibilities:

  1. :func:`build_policy` — derive a fresh :class:`SandboxPolicy` from the live
     :class:`SandboxGuard` (writable roots, recomputed each call so a
     session-granted root takes effect) plus the metadata paths we always force
     read-only (``.git`` / ``config.yaml`` / ``.agent_sessions``) to stop a
     sandboxed command from rewriting its own escape hatch.

  2. :func:`build_runtime` — construct a configured :class:`SandboxRuntime` from
     a :class:`SandboxRuntimeConfig` + a ``get_cwd`` accessor + a ``SandboxGuard``
     factory, wiring ``build_policy`` as the runtime's per-call policy provider.
"""
from __future__ import annotations

import os
from typing import Callable, Optional

from metagpt.common.schema import SandboxRuntimeConfig
from metagpt.executor.permission.sandbox.guard import SandboxGuard
from metagpt.executor.permission.sandbox.resource_guard import ResourceGuard
from metagpt.sandbox import SandboxRuntime
from metagpt.sandbox.backend import SandboxPolicy

# Paths (relative to a writable root) that must stay read-only even inside the
# workspace: config + VCS metadata + the session rollout store. A sandboxed
# command writing these could persist an escape or rewrite its own policy.
_READONLY_BASENAMES = (".git", ".agent_sessions", ".agent_residency", "config.yaml")


def _metadata_overrides(writable_roots: list[str]) -> list[str]:
    """Resolve the forced-read-only metadata paths under each writable root."""
    overrides: list[str] = []
    for root in writable_roots:
        for name in _READONLY_BASENAMES:
            candidate = os.path.join(root, name)
            if os.path.exists(candidate):
                overrides.append(candidate)
    return overrides


def build_policy(guard: SandboxGuard, *, cwd: Optional[str] = None) -> SandboxPolicy:
    """Build a runtime :class:`SandboxPolicy` from the live *guard*.

    Recomputes writable roots every call (so an interactive "always" grant via
    ``SandboxGuard.add_session_root`` is honoured on the next command) and pins
    the sensitive metadata paths read-only.
    """
    roots = guard.writable_roots()
    return SandboxPolicy(
        writable_roots=list(roots),
        readonly_overrides=_metadata_overrides(roots),
        unshare_net=False,  # P1: rely on proxy env injection, not a netns.
        cwd=cwd,
    )


def build_runtime(
    config: SandboxRuntimeConfig,
    *,
    get_cwd: Callable[[], str],
    guard_factory: Callable[[], SandboxGuard],
    resource_guard: Optional[ResourceGuard] = None,
) -> SandboxRuntime:
    """Construct a configured :class:`SandboxRuntime`.

    Args:
        config: the declarative OS-level runtime policy.
        get_cwd: accessor for the Role's current working directory (the policy
            provider seeds the sandbox cwd from it).
        guard_factory: returns a fresh :class:`SandboxGuard` to read writable
            roots from. Called once here to bind the policy provider; the guard
            instance it returns is reused across calls so session grants stick.
        resource_guard: optional live resource-cap holder. When supplied, its
            ``limits()`` is wired as the runtime's ``limits_provider`` so a
            session-adjusted cap takes effect on the next command (mirroring how
            ``guard_factory``'s guard backs the policy provider). When omitted,
            a default :class:`ResourceGuard` seeded from *config* is used.
    """
    guard = guard_factory()
    rguard = resource_guard if resource_guard is not None else ResourceGuard(config)

    def policy_provider() -> SandboxPolicy:
        return build_policy(guard, cwd=get_cwd())

    return SandboxRuntime(
        backend=config.backend,
        fail_if_unavailable=config.fail_if_unavailable,
        harden_process=config.harden_process,
        seccomp=config.seccomp,
        network=config.network,
        network_enforcement=config.network_enforcement,
        allowed_domains=config.allowed_domains,
        policy_provider=policy_provider,
        limits_provider=rguard.limits,
    )


__all__ = ["build_policy", "build_runtime"]
