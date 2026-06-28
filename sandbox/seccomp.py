"""Seccomp BPF filters — syscall-level hardening for the sandboxed command.

bwrap accepts a compiled classic-BPF seccomp program on a file descriptor
(``--seccomp FD`` / ``--add-seccomp FD``) and installs it on the inner command
*after* it finishes its own namespace setup, so the filter constrains only the
sandboxed process (not bwrap's mount/pivot dance).

What this layer provides:

  * :func:`build_hardening_filter` — a **syscall blacklist** (default-allow,
    deny a curated set of dangerous syscalls with ``EPERM``): module loading,
    ``ptrace``, raw ``mount``/``pivot_root``, ``kexec``, the keyring, ``setns``,
    ``reboot``, ``swapon``, clock/time mutation, cross-process memory peek, etc.
    This is defence-in-depth *inside* an already-confined namespace and is
    orthogonal to the network stance.

Why classic BPF can't do the network allowlist
-----------------------------------------------
seccomp filters see only the *scalar* syscall arguments — it cannot dereference
the ``sockaddr *`` passed to ``connect()``, so it cannot allow/deny by IP or
host. It *can* match ``socket(domain, ...)`` because ``domain`` is a scalar, but
that is an all-or-nothing ``AF_INET`` switch, not a per-destination policy.
Therefore:

  * hard network-off is implemented by the **network namespace** (``bwrap
    --unshare-net`` leaves only loopback up — external egress is
    ``ENETUNREACH`` while a Jupyter kernel's loopback ZMQ still works), *not* by
    seccomp. See ``network/netns.py`` / the runtime's ``off`` stance.
  * the domain **allowlist** is enforced by the netns + nft DNAT + local proxy
    chain (see ``network/enforce.py``), again not by seccomp.

:func:`build_block_inet_filter` is still offered (blocks ``socket`` for the
``AF_INET``/``AF_INET6``/``AF_PACKET`` families) for hosts/back-ends where
``--unshare-net`` is unavailable but ``--seccomp`` is — a fallback hard-off.

Dependency: the optional ``pyseccomp`` (ctypes wrapper over ``libseccomp``). If
it is not importable, :func:`seccomp_available` is False and callers degrade
(no filter installed) rather than failing.
"""
from __future__ import annotations

import os
import tempfile
from typing import Optional

from metagpt.common.logs import logger

# Address families we block when emulating a hard network-off via seccomp (the
# fallback when --unshare-net is not usable). AF_UNIX (1) is deliberately left
# allowed so loopback IPC / Jupyter kernel ipc:// channels keep working.
_AF_INET = 2
_AF_INET6 = 10
_AF_PACKET = 17

# Dangerous syscalls denied by the hardening blacklist (default-allow filter).
# Curated for defence-in-depth inside an already-namespaced process: module
# loading, tracing, mount/namespace escapes, kexec, the keyring, time/clock
# mutation, and cross-process memory access. Names are resolved via libseccomp
# so arch-specific numbering is handled for us; unknown names are skipped.
_HARDENING_DENY = (
    # Kernel module (in)loading.
    "init_module",
    "finit_module",
    "delete_module",
    # Process tracing / cross-process memory.
    "ptrace",
    "process_vm_readv",
    "process_vm_writev",
    # Mount / namespace / root manipulation.
    "mount",
    "umount2",
    "pivot_root",
    "setns",
    # Kexec (load a new kernel).
    "kexec_load",
    "kexec_file_load",
    # Keyring.
    "add_key",
    "request_key",
    "keyctl",
    # System control.
    "reboot",
    "swapon",
    "swapoff",
    "acct",
    "_sysctl",
    "quotactl",
    "nfsservctl",
    # Time / clock mutation (anti-tamper).
    "settimeofday",
    "clock_settime",
    "adjtimex",
)


def seccomp_available() -> bool:
    """True when ``pyseccomp`` (libseccomp) can be imported on this host."""
    try:
        import pyseccomp  # noqa: F401
    except Exception:  # noqa: BLE001 — any import failure means unavailable
        return False
    return True


def _resolve_known(seccomp, names: tuple[str, ...]) -> list[str]:
    """Filter *names* down to those libseccomp can resolve on this arch."""
    known: list[str] = []
    for n in names:
        try:
            seccomp.resolve_syscall(seccomp.Arch.NATIVE, n)
            known.append(n)
        except Exception:  # noqa: BLE001 — name unknown on this kernel/arch
            continue
    return known


def _export_to_file(filt) -> Optional[str]:
    """Export a built filter to a temp file; return its path (or None on error).

    bwrap reads the BPF program from a file descriptor, which the spawn site
    obtains by redirecting from this path (``... --seccomp 9 -- <cmd> 9<path``).
    The file is persistent (the runtime reuses it for every command and unlinks
    it at shutdown) — unlike a one-shot fd, a path can be redirected from many
    times. Created ``0600`` in the system temp dir.
    """
    try:
        fd, path = tempfile.mkstemp(prefix="sbx-seccomp-", suffix=".bpf")
        with os.fdopen(fd, "wb") as fh:
            filt.export_bpf(fh)
        os.chmod(path, 0o600)
        return path
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"seccomp: failed to export filter ({exc}); skipping")
        return None


def build_hardening_filter() -> Optional[str]:
    """Build the dangerous-syscall blacklist; return the BPF file path or None.

    Default action is ALLOW; the curated :data:`_HARDENING_DENY` set is denied
    with ``EPERM``. Returns ``None`` (degrade, no filter) when ``pyseccomp`` is
    unavailable or the build fails — never raises. The caller owns the returned
    path (redirects bwrap's fd from it and unlinks it when done).
    """
    if not seccomp_available():
        return None
    try:
        import pyseccomp as seccomp

        filt = seccomp.SyscallFilter(defaction=seccomp.ALLOW)
        for name in _resolve_known(seccomp, _HARDENING_DENY):
            filt.add_rule(seccomp.ERRNO(seccomp.errno.EPERM), name)
        return _export_to_file(filt)
    except Exception as exc:  # noqa: BLE001 — best-effort hardening
        logger.warning(f"seccomp: failed to build hardening filter ({exc}); skipping")
        return None


def build_block_inet_filter() -> Optional[str]:
    """Build a filter blocking ``socket(AF_INET/AF_INET6/AF_PACKET)``.

    A fallback hard network-off for hosts where ``bwrap --unshare-net`` is not
    usable but ``--seccomp`` is. ``AF_UNIX`` is left allowed so loopback IPC and
    a Jupyter kernel's ``ipc://`` channels still function. Returns the BPF file
    path or ``None`` on unavailability/failure (never raises).
    """
    if not seccomp_available():
        return None
    try:
        import pyseccomp as seccomp

        filt = seccomp.SyscallFilter(defaction=seccomp.ALLOW)
        for family in (_AF_INET, _AF_INET6, _AF_PACKET):
            filt.add_rule(
                seccomp.ERRNO(seccomp.errno.EAFNOSUPPORT),
                "socket",
                seccomp.Arg(0, seccomp.EQ, family),
            )
        return _export_to_file(filt)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"seccomp: failed to build inet-block filter ({exc}); skipping")
        return None


__all__ = [
    "seccomp_available",
    "build_hardening_filter",
    "build_block_inet_filter",
]
