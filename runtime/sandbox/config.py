"""Sandbox runtime config — deploy-time settings for OS-level isolation.

Lives in the sandbox bounded context; Product composition projects it into ``RoleSchema``
(via ``PermissionConfig.runtime``) can declare it without importing the runtime
package (``mote.runtime.sandbox``). This is *only* the declarative shape; the
enforcement lives in ``mote.runtime.sandbox`` (the runtime layer) and the
``SandboxPolicy`` translation lives in ``mote.runtime.tools.permission.sandbox``
(the adapter layer).

Relationship to ``SandboxConfig`` (also in ``permission_config.py``):
  * ``SandboxConfig`` is the *logical* path-checking boundary (axis B of the
    permission model) — it decides which paths a tool may write.
  * ``SandboxRuntimeConfig`` is the *OS-level* enforcement layer — when enabled,
    the executor wraps the command in ``bwrap`` (filesystem + pid namespaces),
    applies process hardening, and routes network through a local proxy.

Default: ``runtime=None`` (the default on ``PermissionConfig``) means no
OS-level sandbox, only the logical boundary.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field, model_validator

from mote.contracts.config.base import ConfigModel
from mote.runtime.sandbox.network.patterns import matches_pattern

# Which OS-level isolation backend to use. ``auto`` probes the host
# (``shutil.which("bwrap")`` + platform); ``bwrap`` forces bubblewrap; ``none``
# disables OS-level isolation (the runtime becomes a passthrough).
SandboxBackendKind = Literal["auto", "bwrap", "none"]

# How to shape the injected credential into an HTTP request header:
#   * ``bearer`` — ``Authorization: Bearer <secret>`` (OAuth / GitHub PAT).
#   * ``basic``  — ``Authorization: Basic base64(<username>:<secret>)``.
#   * ``header`` — a raw ``<header>: <secret>`` (e.g. ``X-Api-Key``).
CredentialScheme = Literal["bearer", "basic", "header"]


class CredentialConfig(ConfigModel):
    """Per-domain credential-brokering rule for the sandbox egress proxy.

    Injects an authentication header at the trusted egress proxy so a sandboxed
    tool (``git`` / ``curl`` / ``wget``) reaches an authenticated endpoint while
    the secret **never enters the sandbox process** — it is referenced *by key*
    into the :class:`~mote.runtime.secrets.store.SecretStore` and resolved lazily
    in the app runtime. The model never authors the value.

    Every rule ``domains`` entry must also appear in
    :attr:`SandboxRuntimeConfig.allowed_domains` (validated fail-closed) — a
    credential is meaningless for a host the proxy would refuse anyway.
    """

    domains: list[str] = Field(
        description=(
            "Hosts this credential applies to (glob: '*.x' / '**.x' / exact — "
            "reuses the NetworkPolicy matcher). Must be a subset of the runtime's "
            "allowed_domains."
        ),
    )
    secret: str = Field(
        description=(
            "KEY into the SecretStore (NOT the value) — the named secret whose "
            "plaintext value the proxy injects. Kept out of config.yaml."
        ),
    )
    scheme: CredentialScheme = Field(
        default="bearer",
        description="Header shape: bearer | basic | header.",
    )
    header: str = Field(
        default="Authorization",
        description="Header name (used by 'header' and 'bearer'; default Authorization).",
    )
    username: Optional[str] = Field(
        default=None,
        description="Username for 'basic' auth (base64(username:secret)); ignored otherwise.",
    )


class SandboxRuntimeConfig(ConfigModel):
    """OS-level sandbox runtime policy, nested under :class:`PermissionConfig`.

    When ``enabled`` and a backend is available, the executor wraps every
    command-execution tool (Bash / terminal / python) in an OS-level sandbox:
    a ``bwrap`` filesystem + pid namespace confining writes to the workspace,
    process hardening (core dumps off, ``LD_*`` stripped), and — when
    ``network`` is set — a local proxy enforcing a domain allowlist + SSRF
    rejection.
    """

    enabled: bool = Field(
        default=False,
        description="Master switch for OS-level isolation (off by default — opt-in).",
    )
    backend: SandboxBackendKind = Field(
        default="auto",
        description="Isolation backend: auto (probe host) | bwrap | none.",
    )
    fail_if_unavailable: bool = Field(
        default=False,
        description=(
            "When the requested backend is unavailable: True raises (hard fail), "
            "False runs the command unsandboxed with a warning (graceful degrade)."
        ),
    )
    harden_process: bool = Field(
        default=True,
        description="Apply the process-hardening prelude (core dumps off, strip LD_*).",
    )
    seccomp: bool = Field(
        default=True,
        description=(
            "Attach a seccomp BPF filter denying dangerous syscalls (module load, "
            "ptrace, raw mount/pivot_root, kexec, keyring, setns, clock mutation). "
            "Defence-in-depth inside the namespace; degrades silently when "
            "libseccomp/pyseccomp is unavailable."
        ),
    )
    network: Literal["off", "proxy", "open"] = Field(
        default="proxy",
        description=(
            "Network stance: off (no egress) | proxy (HTTP(S) via local allowlist "
            "proxy) | open (no network restriction)."
        ),
    )
    network_enforcement: bool = Field(
        default=True,
        description=(
            "With network='proxy': when True and the host has the toolchain "
            "(bwrap + slirp4netns + nft), confine egress to a network namespace "
            "whose ONLY route out is the proxy — so even code that opens a raw "
            "socket is forced through the allowlist. When False (or the "
            "toolchain is missing), fall back to HTTP_PROXY env injection, which "
            "only constrains proxy-honouring tools."
        ),
    )
    allowed_domains: list[str] = Field(
        default_factory=list,
        description=(
            "Domain allowlist for the network proxy (glob: '*.x' / '**.x' / exact). "
            "Empty + network='proxy' means deny all egress."
        ),
    )

    memory_max: Optional[str] = Field(
        default="4G",
        description=(
            "Group-level memory cap (systemd byte spec: '4G' / '512M'). Applied "
            "via 'systemd-run --user --scope -p MemoryMax=…' so it covers the "
            "whole process tree (a memory bomb is OOM-killed). Swap is disabled "
            "alongside it so the cap can't be sidestepped. None disables the cap. "
            "Degrades to no-op (with a warning) when the systemd user manager / "
            "cgroup v2 is unavailable."
        ),
    )
    pids_max: Optional[int] = Field(
        default=512,
        description=(
            "Group-level task cap (fork-bomb backstop). Applied via "
            "'-p TasksMax=…' on the transient scope. None disables the cap."
        ),
    )
    cpu_quota: Optional[str] = Field(
        default=None,
        description=(
            "Group-level CPU cap (systemd percentage: '200%' == 2 cores). "
            "Default None: on most hosts the 'cpu' controller is NOT delegated to "
            "the user scope, so a quota would be silently ignored — we leave it "
            "off to avoid a false sense of safety. To use it, set this AND have "
            "root delegate cpu to the user manager "
            "(/etc/systemd/system/user@.service.d/delegate.conf: "
            "'Delegate=cpu …' + 'systemctl daemon-reexec'). When set but the "
            "controller is undelegated, the runtime drops the quota and warns."
        ),
    )

    credentials: list[CredentialConfig] = Field(
        default_factory=list,
        description=(
            "Per-domain credential-brokering rules. When non-empty the egress "
            "proxy injects the referenced secret's auth header for matching hosts "
            "(HTTP inline; HTTPS via per-domain MITM). Empty = no brokering "
            "(the proxy behaves exactly as before). Each rule's domains must be a "
            "subset of allowed_domains."
        ),
    )

    def network_enforced(self) -> bool:
        """True when the local network proxy should run (``network == 'proxy'``)."""
        return self.network == "proxy"

    @model_validator(mode="after")
    def _validate_credential_domains(self) -> "SandboxRuntimeConfig":
        """Fail closed: every credential domain must match an allowed domain.

        A credentialed host the proxy would refuse anyway is a misconfiguration
        that could mask an intended-but-unlisted domain — reject at config time.
        Reuses the shared :mod:`mote.runtime.sandbox.network.patterns` glob matcher so this
        subset check is the exact matching the proxy uses (one source of truth,
        no upward dependency on the sandbox runtime package).
        """
        if not self.credentials:
            return self

        for rule in self.credentials:
            for domain in rule.domains:
                if not any(matches_pattern(domain, allowed) for allowed in self.allowed_domains):
                    raise ValueError(
                        f"credential domain {domain!r} is not covered by allowed_domains "
                        f"{self.allowed_domains!r} — add it to allowed_domains or remove the rule"
                    )
        return self


__all__ = ["SandboxRuntimeConfig", "SandboxBackendKind", "CredentialConfig", "CredentialScheme"]
