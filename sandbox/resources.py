"""Group-level resource limits via ``systemd-run --user --scope`` (cgroups v2).

The per-process hardening (``hardening.apply_in_child`` setting ``RLIMIT_CORE=0``)
plugs a core-dump *leak*, not a DoS vector. A sandboxed payload can still fork a
bomb (exhaust PIDs), balloon memory (OOM the host), or spin every core — and
per-process ``RLIMIT_*`` cannot cap a whole **process tree** (each fork resets).

cgroups v2 caps the *group*: a single limit covers the command and every child
it forks. But on a typical unprivileged Linux/WSL2 host the process lives in the
root cgroup (``0::/``) and ``/sys/fs/cgroup`` is read-only to the user — so we
cannot ``mkdir`` a cgroup directly.

The escape hatch is the **systemd user manager**: ``systemd-run --user --scope``
asks ``user@<uid>.service`` (which systemd delegates a writable sub-hierarchy to)
to create a transient *scope* cgroup, drop the command into it, and translate the
``-p Property=…`` flags into that scope's cgroup files. Empirically on this host
(WSL2): ``MemoryMax`` OOM-kills a memory bomb (rc=137), ``TasksMax`` caps
``pids.max`` — but ``CPUQuota`` is a **no-op** because the ``cpu`` controller is
not delegated to the user scope (``user@<uid>.service/cgroup.subtree_control``
lists only ``memory pids``). Enabling it needs root:

    # /etc/systemd/system/user@.service.d/delegate.conf
    [Service]
    Delegate=cpu cpuset io memory pids
    # then: systemctl daemon-reexec

So we *probe* delegation and only emit ``CPUQuota`` when ``cpu`` is delegated,
warning otherwise (a delivered-but-ignored limit is a false sense of safety).

This module is the **pure, dependency-light half**: the limit dataclass, the
host probes (all best-effort, never raise — a missing systemd / cgroup2 just
means the command runs unlimited with a warning, mirroring
``network.enforce.enforcement_available`` and the bwrap-degrade path), and the
``systemd-run`` argv prefix builder. The runtime (:mod:`.runtime`) prepends the
prefix as the **outermost** wrapper of both the ``wrap_command`` shell string
and the ``wrap_exec`` argv, so the scope encloses the whole launcher → bwrap →
payload tree. ``systemd-run --scope`` forwards inherited fds (the seccomp BPF
``9<path`` redirect, the netns ``pass_fds``), stdin/stdout/stderr and the PTY —
verified — so it composes cleanly with every existing seam.
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from typing import Optional

# The systemd transient-unit launcher. ``--user`` targets the per-user manager
# (``user@<uid>.service``), which is the only manager an unprivileged process can
# ask to create a delegated cgroup.
SYSTEMD_RUN = "systemd-run"

# Root of the unified cgroup v2 hierarchy. The user manager's delegated scope
# lives under ``user.slice/user-<uid>.slice/user@<uid>.service`` here.
_CGROUP_ROOT = "/sys/fs/cgroup"


@dataclass
class ResourceLimits:
    """Group-level (process-tree) resource caps for a sandboxed command.

    Values are systemd property *strings* (forwarded verbatim to ``-p``):
      * ``memory_max`` / ``memory_swap_max`` — byte specs ("4G", "512M", "0").
      * ``cpu_quota`` — a percentage where 100% == one core ("200%" == 2 cores).
      * ``pids_max`` — an integer task cap (fork-bomb backstop).

    When memory is capped we default ``memory_swap_max="0"`` so a payload cannot
    sidestep the RSS cap by swapping (``MemorySwapMax=0`` makes ``MemoryMax`` a
    hard ceiling rather than a soft one backed by swap).
    """

    memory_max: Optional[str] = None
    memory_swap_max: Optional[str] = "0"
    cpu_quota: Optional[str] = None
    pids_max: Optional[int] = None

    @property
    def is_empty(self) -> bool:
        """True when no actual cap is set → the runtime skips the wrapper.

        ``memory_swap_max`` alone does not count: it only takes effect alongside
        ``memory_max`` (capping swap with no RSS cap is meaningless), so an
        otherwise-empty limit set is treated as a no-op.
        """
        return (
            self.memory_max is None
            and self.cpu_quota is None
            and self.pids_max is None
        )


def cgroup_limits_available() -> bool:
    """True when ``systemd-run --user --scope`` can apply cgroup limits here.

    Requires Linux + the ``systemd-run`` binary + a mounted cgroup v2 hierarchy
    + a reachable systemd *user* manager (``systemctl --user is-system-running``
    must answer, even with a degraded state — we only care that the manager is
    live enough to host a transient scope). Never raises; any missing piece means
    the runtime degrades to running the command unlimited (with a warning),
    mirroring the bwrap / netns degrade paths.
    """
    if not sys.platform.startswith("linux"):
        return False
    if shutil.which(SYSTEMD_RUN) is None:
        return False
    if not _cgroup2_mounted():
        return False
    return _user_manager_reachable()


def cpu_controller_delegated() -> bool:
    """True when the ``cpu`` controller is delegated to the user scope.

    Reads ``user@<uid>.service/cgroup.subtree_control``: only when it lists
    ``cpu`` will a ``CPUQuota`` applied to a transient scope actually take
    effect. On a stock host this is False (systemd delegates only ``memory``
    + ``pids`` to the user manager by default), so we suppress ``CPUQuota`` and
    warn rather than deliver a silently-ignored limit. Best-effort: any read
    failure returns False.
    """
    path = _user_service_subtree_control()
    if path is None:
        return False
    try:
        with open(path, "r", encoding="utf-8") as fh:
            controllers = fh.read().split()
    except OSError:
        return False
    return "cpu" in controllers


def _parse_bytes_to_kb(spec: str) -> Optional[int]:
    """Parse a systemd byte spec ("4G"/"512M"/"1024K"/raw bytes) into kibibytes.

    ``ulimit -v`` wants a KiB count; systemd byte specs use a K/M/G/T suffix
    (1024-based) or a bare integer (bytes). Returns None on an unparseable spec
    so the caller simply omits the ``-v`` cap rather than emitting garbage.
    """
    spec = spec.strip()
    if not spec:
        return None
    units = {"K": 1, "M": 1024, "G": 1024 * 1024, "T": 1024 * 1024 * 1024}
    suffix = spec[-1].upper()
    try:
        if suffix in units:
            return int(float(spec[:-1]) * units[suffix])
        # No recognised suffix → treat as raw bytes, convert to KiB.
        return int(spec) // 1024
    except (ValueError, IndexError):
        return None


def rlimit_prelude(limits: ResourceLimits) -> str:
    """Build a per-process ``ulimit`` fallback snippet for *limits* (POSIX ``sh``).

    The weaker per-process analogue of :func:`systemd_run_prefix`, for hosts
    where the cgroup path degrades to a no-op (no systemd user manager / cgroup
    v2 — e.g. a bare WSL or a stripped container). Prepended to the sandboxed
    shell's command so it caps the shell and its descendants. Maps:

      * ``memory_max`` → ``ulimit -v <kb>`` (``RLIMIT_AS``, address space).
      * ``pids_max``   → ``ulimit -u <n>`` (``RLIMIT_NPROC``, per-UID procs).

    ``cpu_quota`` has no ``ulimit`` *rate* equivalent (``-t`` is total CPU
    seconds, not a share), so it is dropped — CPU capping requires the cgroup
    path. Each ``ulimit`` sets BOTH the soft and hard limit (no ``-S``/``-H``),
    so the value can be lowered-but-not-raised by the payload — the "set then
    can't undo" backstop. Best-effort (``|| true``): a ulimit that can't apply
    (e.g. asking to raise above the inherited hard limit) is skipped, never
    aborting the command.

    Caveats vs the cgroup cap (a backstop, not an equal): ``-u`` is **per-UID**
    (counts ALL the user's processes, not just this tree) and ``-v`` caps
    **virtual** not resident memory (programs reserving large unused address
    space may trip it early). Returns "" for an empty limit set.
    """
    if limits.is_empty:
        return ""
    parts: list[str] = []
    if limits.memory_max is not None:
        kb = _parse_bytes_to_kb(limits.memory_max)
        if kb is not None and kb > 0:
            parts.append(f"ulimit -v {kb} 2>/dev/null || true")
    if limits.pids_max is not None:
        parts.append(f"ulimit -u {limits.pids_max} 2>/dev/null || true")
    return "; ".join(parts)


def systemd_run_prefix(limits: ResourceLimits, *, with_cpu: bool) -> list[str]:
    """Build the ``systemd-run --user --scope`` argv prefix for *limits*.

    Returns ``[]`` for an empty limit set (the runtime then prepends nothing).
    Otherwise an argv like::

        systemd-run --user --scope --quiet
            -p MemoryMax=4G -p MemorySwapMax=0 -p TasksMax=512 [-p CPUQuota=200%]

    ``--quiet`` suppresses the "Running as unit …" banner so it does not leak
    into the command's stderr. ``CPUQuota`` is emitted ONLY when *with_cpu* (the
    caller passes :func:`cpu_controller_delegated`): an undelegated cpu
    controller would make the quota a no-op, so we leave it off to avoid a false
    sense of safety. ``MemorySwapMax`` is emitted only alongside ``MemoryMax``.
    """
    if limits.is_empty:
        return []
    prefix = [SYSTEMD_RUN, "--user", "--scope", "--quiet"]
    if limits.memory_max is not None:
        prefix += ["-p", f"MemoryMax={limits.memory_max}"]
        if limits.memory_swap_max is not None:
            prefix += ["-p", f"MemorySwapMax={limits.memory_swap_max}"]
    if limits.pids_max is not None:
        prefix += ["-p", f"TasksMax={limits.pids_max}"]
    if with_cpu and limits.cpu_quota is not None:
        prefix += ["-p", f"CPUQuota={limits.cpu_quota}"]
    return prefix


# --- host probes (private helpers) -----------------------------------------


def _cgroup2_mounted() -> bool:
    """True when a unified cgroup v2 hierarchy is mounted at the standard root.

    ``cgroup.controllers`` exists only on a cgroup v2 (unified) hierarchy, so its
    presence at the root is a cheap, reliable v2 signal (v1 has no such file).
    """
    return os.path.isdir(_CGROUP_ROOT) and os.path.exists(
        os.path.join(_CGROUP_ROOT, "cgroup.controllers")
    )


def _user_manager_reachable() -> bool:
    """True when the systemd *user* manager answers ``is-system-running``.

    Any answer (``running`` / ``degraded`` / even a non-zero rc with output)
    means the manager is live enough to host a transient scope; only a missing
    binary or a hard failure to invoke it counts as unreachable. Best-effort.
    """
    try:
        import subprocess

        proc = subprocess.run(
            ["systemctl", "--user", "is-system-running"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    # `is-system-running` prints a state word even when rc != 0 (e.g.
    # "degraded"). An empty answer (no user manager) is the unreachable case.
    return bool((proc.stdout or "").strip())


def _user_service_subtree_control() -> Optional[str]:
    """Path to ``user@<uid>.service/cgroup.subtree_control`` (or None).

    The standard systemd layout nests the user manager at
    ``{root}/user.slice/user-<uid>.slice/user@<uid>.service``. Returns the
    subtree_control path when that directory exists, else None.
    """
    uid = os.getuid() if hasattr(os, "getuid") else None
    if uid is None:
        return None
    path = os.path.join(
        _CGROUP_ROOT,
        "user.slice",
        f"user-{uid}.slice",
        f"user@{uid}.service",
        "cgroup.subtree_control",
    )
    return path if os.path.exists(path) else None


__all__ = [
    "SYSTEMD_RUN",
    "ResourceLimits",
    "cgroup_limits_available",
    "cpu_controller_delegated",
    "systemd_run_prefix",
    "rlimit_prelude",
]
