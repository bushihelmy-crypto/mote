"""Process hardening — drop privileges / dangerous inheritance in the child.

Two delivery mechanisms, in order of preference:

1. :func:`hardening_prelude` — a small ``sh`` snippet prepended to the command
   that runs *inside* the sandboxed shell. This is the preferred path: it is
   thread-safe (unlike ``preexec_fn``) and works uniformly whether or not bwrap
   is in play, because it executes as the first thing the child shell does.

2. :func:`apply_in_child` — a ctypes/``resource`` callback for
   ``subprocess``'s ``preexec_fn``, used ONLY as a fallback when no shell
   prelude can be injected (e.g. a raw ``exec`` path). ⚠️ ``preexec_fn`` runs
   between ``fork`` and ``exec`` in a multi-threaded parent and is therefore
   not async-signal-safe; keep it minimal and prefer the prelude.

What hardening does (P1, deliberately minimal):
  * ``ulimit -c 0`` / ``RLIMIT_CORE = 0`` — no core dumps (avoid leaking memory
    contents of a crashed sandboxed process to disk).
  * unset ``LD_PRELOAD`` / ``LD_LIBRARY_PATH`` / ``LD_AUDIT`` — strip loader
    hijacking vectors inherited from the parent environment.
  * ``PR_SET_DUMPABLE = 0`` (ctypes path) — make the process non-dumpable so
    ``/proc/<pid>/mem`` etc. are not readable by a same-uid attacker.

seccomp syscall filtering is explicitly out of scope for P1 (see the package
docstring / plan): it is deferred to a later phase.
"""
from __future__ import annotations

# Environment variables that let a parent inject code into a freshly-exec'd
# child via the dynamic loader. Stripped in both the prelude and the env.
_DANGEROUS_LD_VARS = (
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "LD_AUDIT",
)


def hardening_prelude() -> str:
    """Return a ``sh`` prefix that hardens the current shell, then runs on.

    Intended to be prepended to the user command with ``; `` so it executes as
    the first statements of the sandboxed shell::

        sh -c '<hardening_prelude()>; <user command>'

    The snippet is intentionally POSIX-``sh`` compatible (no bashisms) so it is
    safe under ``/bin/sh`` as well as ``bash``.
    """
    parts = ["ulimit -c 0 2>/dev/null || true"]
    for var in _DANGEROUS_LD_VARS:
        parts.append(f"unset {var}")
    return "; ".join(parts)


def harden_env(env: dict[str, str]) -> dict[str, str]:
    """Return a copy of *env* with the dangerous ``LD_*`` vars removed.

    Belt-and-braces alongside :func:`hardening_prelude`: even before the shell
    runs, the spawned process should not carry a loader-hijack variable. Always
    returns a new dict (never mutates the caller's).
    """
    return {k: v for k, v in env.items() if k not in _DANGEROUS_LD_VARS}


def apply_in_child() -> None:
    """``preexec_fn`` callback: harden the child between fork and exec.

    Best-effort and dependency-free. Each step is guarded so a missing symbol /
    unsupported platform degrades silently rather than killing the spawn.

    ⚠️ Not async-signal-safe; only use when the ``sh`` prelude is unavailable.
    """
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception:  # noqa: BLE001 — best-effort; never block the spawn
        pass

    try:
        import ctypes

        # PR_SET_DUMPABLE = 4; prctl(PR_SET_DUMPABLE, 0) marks us non-dumpable.
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(4, 0, 0, 0, 0)
    except Exception:  # noqa: BLE001
        pass


__all__ = ["hardening_prelude", "harden_env", "apply_in_child"]
