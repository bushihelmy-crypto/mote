"""Backend abstraction — the strategy that turns a policy into a wrapped argv.

A :class:`SandboxBackend` knows *how* to confine a command on a given host. The
runtime façade owns *what* to confine (policy translation) and delegates the
mechanism here. Two backends exist in P1:

  * :class:`NullBackend` — no isolation (passthrough). Used when no backend is
    available and ``fail_if_unavailable`` is False (graceful degrade), or when
    the backend is explicitly ``none``.
  * :class:`~mote.runtime.sandbox.bwrap.BwrapBackend` — bubblewrap namespaces.

A backend works on a :class:`SandboxPolicy` (a small, runtime-local data object,
NOT the executor's ``SandboxConfig`` — the adapter translates one into the
other) and produces a wrapped argv list ready for ``create_subprocess_exec`` or
to be joined into a ``sh -c`` string.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SandboxPolicy:
    """Runtime-local description of the confinement to apply to one command.

    Deliberately decoupled from the executor's ``SandboxConfig`` so the runtime
    package depends only on ``common``/stdlib. The adapter
    (``executor.permission.sandbox.adapter``) builds this from the live
    ``SandboxGuard`` + ``SandboxRuntimeConfig``.
    """

    # Absolute roots writable inside the sandbox (everything else is read-only).
    writable_roots: list[str] = field(default_factory=list)
    # Absolute paths to force read-only even if they sit inside a writable root
    # (e.g. ``.git`` / ``config.yaml`` / ``.agent_sessions`` — anti-escape).
    readonly_overrides: list[str] = field(default_factory=list)
    # When True the network namespace is unshared. With bwrap this leaves only
    # loopback up (external egress => ENETUNREACH). A sandboxed Jupyter kernel is
    # unaffected: its control channels run over ipc:// unix sockets (filesystem-
    # bound, not loopback TCP), so they survive the unshared netns — the hard
    # ``network="off"`` stance.
    unshare_net: bool = False
    # Working directory the command runs in (bind-mounted writable when set).
    cwd: str | None = None
    # When set, the inner command is constrained by a seccomp BPF program read
    # from this file descriptor number (``bwrap --seccomp N``). The fd itself is
    # supplied by the spawn site (a single-digit fd, dash-safe, redirected from
    # the compiled BPF file); the backend only emits the flag. None => no filter.
    seccomp_fd: int | None = None
    # Grant CAP_NET_ADMIN to the (userns-root) inner process so it can configure
    # its own netns (lo up / nft) — used by the proxy-whitelist netns chain.
    cap_net_admin: bool = False
    # Extra ``--dev-bind`` device paths (e.g. ``/dev/net/tun`` for slirp egress).
    dev_binds: list[str] = field(default_factory=list)
    # Map the inner process to userns-root (``--uid 0 --gid 0``). REQUIRED for
    # ``cap_net_admin`` to take effect: bwrap's default ``--unshare-user`` keeps
    # the caller's uid, which is NOT userns-root, so CAP_NET_ADMIN is dropped.
    # Only used by the netns sole-egress chain.
    uid_root: bool = False
    # When set, bwrap writes its runtime info (incl. ``child-pid``) as JSON to
    # this file descriptor (``--info-fd N``). The netns orchestrator reads the
    # child-pid off it to attach slirp4netns. None => no info pipe.
    info_fd: int | None = None
    # Extra writable bind mounts beyond ``writable_roots`` (e.g. a scratch dir
    # the orchestrator needs visible read-write inside the sandbox).
    extra_writable: list[str] = field(default_factory=list)
    # Absolute paths to MASK from the sandbox: each is bind-mounted over with
    # ``/dev/null`` so it reads as empty inside, no matter that the read-only
    # root baseline (``--ro-bind / /``) would otherwise expose it. Used to hide
    # secret material (the encrypted vault, ``vault.key``, the MITM CA private
    # key) so the "secret never enters the sandbox" guarantee is real.
    masked_paths: list[str] = field(default_factory=list)
    # Absolute DIRECTORY paths to MASK from the sandbox: each is overlaid with an
    # empty ``tmpfs`` so it reads as an empty directory inside, hiding whatever
    # the read-only root baseline would otherwise expose. The directory sibling
    # of ``masked_paths`` (which uses ``/dev/null`` for single files). Used to
    # hide the durable browser-profile store (encrypted logins) — a whole
    # directory of dynamically-named files that cannot be masked file-by-file.
    masked_dirs: list[str] = field(default_factory=list)


class SandboxBackend:
    """Abstract isolation strategy."""

    name = "base"

    @property
    def available(self) -> bool:  # pragma: no cover - trivial
        """Whether this backend can actually confine on this host."""
        return False

    def build_argv(self, policy: SandboxPolicy, inner_argv: list[str]) -> list[str]:
        """Wrap *inner_argv* per *policy*, returning the full argv to spawn.

        ``inner_argv`` is the command to run *inside* the sandbox (already
        including any hardening prelude shell wrapping). The base implementation
        is the identity (no confinement).
        """
        raise NotImplementedError


class NullBackend(SandboxBackend):
    """No-op backend: returns the inner argv unchanged (no isolation)."""

    name = "none"

    @property
    def available(self) -> bool:
        return True

    def build_argv(self, policy: SandboxPolicy, inner_argv: list[str]) -> list[str]:
        return list(inner_argv)


__all__ = ["SandboxBackend", "NullBackend", "SandboxPolicy"]
