"""Sandbox adapter — translate our policy objects into runtime inputs.

The *adapter* layer (this module + ``guard.py``) binds the runtime
(``mote.runtime.sandbox``) to our domain types. It is the only place that knows about
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
from pathlib import Path
from typing import Callable, Optional

from mote.runtime.sandbox.backend import SandboxPolicy
from mote.runtime.sandbox.config import SandboxRuntimeConfig
from mote.runtime.sandbox.network.credentials import CredentialBroker, CredentialRule, SecretLookup
from mote.runtime.sandbox.runtime import SandboxRuntime
from mote.runtime.secrets.cipher import KeyFileProvider
from mote.runtime.secrets.store import secrets_path
from mote.runtime.tools.permission.sandbox.guard import SandboxGuard
from mote.runtime.tools.permission.sandbox.resource_guard import ResourceGuard

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


def _secret_masks(secrets_root: Path, sandbox_ca_root: Path) -> list[str]:
    """Absolute paths of secret material to mask from the sandbox.

    The read-only root baseline (``--ro-bind / /``) would otherwise expose the
    encrypted vault, its key, and the MITM CA private key to any sandboxed
    process. Masking them with ``/dev/null`` makes the credential-brokering
    guarantee real (the secret only ever exists in the trusted runtime) AND
    closes a pre-existing exposure of the vault itself. Never masks the *public*
    CA bundle (sandboxed tools must read it to trust the MITM leaf). Only paths
    that exist are returned (bwrap errors on a missing bind source).
    """
    candidates = [
        secrets_path(secrets_root),
        KeyFileProvider(secrets_root / "vault.key").path,
        sandbox_ca_root / "ca.key",
    ]
    return [str(p) for p in candidates if p.exists()]


def _secret_masked_dirs(browser_profiles_root: Path) -> list[str]:
    """Absolute directory paths of secret material to mask from the sandbox.

    The directory sibling of :func:`_secret_masks`. The durable browser-profile
    store holds encrypted logins in dynamically-named files, so it is masked as a
    whole directory (overlaid with an empty tmpfs) rather than file-by-file. The
    profiles are already encrypted with the (masked) vault key, so this is
    defense-in-depth: it also hides even the ciphertext + which profiles exist.
    Only returned when the directory actually exists (bwrap errors otherwise).
    """
    profiles = browser_profiles_root
    return [str(profiles)] if profiles.is_dir() else []


def build_policy(
    guard: SandboxGuard,
    *,
    cwd: Optional[str] = None,
    secrets_root: Path,
    browser_profiles_root: Path,
    sandbox_ca_root: Path,
) -> SandboxPolicy:
    """Build a runtime :class:`SandboxPolicy` from the live *guard*.

    Recomputes writable roots every call (so an interactive "always" grant via
    ``SandboxGuard.add_session_root`` is honoured on the next command), pins the
    sensitive metadata paths read-only, and masks secret material (vault + keys
    as files, the browser-profile store as a directory) so it never leaks into
    the sandbox despite the read-only root baseline.
    """
    roots = guard.writable_roots()
    return SandboxPolicy(
        writable_roots=list(roots),
        readonly_overrides=_metadata_overrides(roots),
        masked_paths=_secret_masks(secrets_root, sandbox_ca_root),
        masked_dirs=_secret_masked_dirs(browser_profiles_root),
        unshare_net=False,  # P1: rely on proxy env injection, not a netns.
        cwd=cwd,
    )


def _build_broker(
    config: SandboxRuntimeConfig,
    secret_lookup: Optional[SecretLookup],
) -> Optional[CredentialBroker]:
    """Compile the config's credential rules into a :class:`CredentialBroker`.

    Returns ``None`` when no rules are configured OR no secret lookup is wired
    (secrets disabled) — the proxy then behaves exactly as before. A rule whose
    secret is missing at request time still fail-closes at the broker, but with
    no lookup at all there is nothing to broker, so the whole feature is inert.
    """
    if not config.credentials or secret_lookup is None:
        return None
    rules = [
        CredentialRule(
            domains=tuple(rule.domains),
            secret_key=rule.secret,
            scheme=rule.scheme,
            header=rule.header,
            username=rule.username,
        )
        for rule in config.credentials
    ]
    return CredentialBroker(rules, secret_lookup)


def build_runtime(
    config: SandboxRuntimeConfig,
    *,
    get_cwd: Callable[[], str],
    guard_factory: Callable[[], SandboxGuard],
    resource_guard: Optional[ResourceGuard] = None,
    secret_lookup: Optional[SecretLookup] = None,
    secrets_root: Path,
    browser_profiles_root: Path,
    sandbox_ca_root: Path,
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
        secret_lookup: resolve a secret *key* to its plaintext value (the
            ``SecretStore.get`` accessor). Drives the credential broker so a
            sandboxed tool reaches authenticated endpoints without the secret
            ever entering the sandbox. ``None`` => no brokering.
    """
    guard = guard_factory()
    rguard = resource_guard if resource_guard is not None else ResourceGuard(config)
    broker = _build_broker(config, secret_lookup)

    def policy_provider() -> SandboxPolicy:
        return build_policy(
            guard,
            cwd=get_cwd(),
            secrets_root=secrets_root,
            browser_profiles_root=browser_profiles_root,
            sandbox_ca_root=sandbox_ca_root,
        )

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
        credential_broker=broker,
        sandbox_ca_root=sandbox_ca_root,
    )


__all__ = ["build_policy", "build_runtime"]
