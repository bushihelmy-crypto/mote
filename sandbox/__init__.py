"""mote.sandbox — OS-level sandbox runtime (the *runtime* layer).

A product-agnostic, reusable isolation capability — deliberately decoupled from
our ``PermissionConfig`` / ``SandboxGuard`` (those live in the *adapter* layer,
``mote.executor.permission.sandbox``). This package only depends on
``mote.common`` (and the stdlib), mirroring the layering of the ``session``
package: it can be imported from anywhere on top of ``common`` without a cycle.

Threat model targets Codex-grade isolation; the architecture mirrors Claude
Code's split (adapter in the main repo + an independent runtime). We do NOT
write our own namespace/mount code — we shell out to the system ``bwrap``
(bubblewrap) on Linux/WSL2; process hardening runs inside the sandboxed child
via a ``sh`` prelude; the network policy is a small local Python proxy.

Public surface:
  * :class:`SandboxRuntime` — the façade the executor injects and calls.
  * :func:`detect_backend` — best-effort host probe (``bwrap`` available?).
  * :func:`wrap_command` — module-level convenience over a default runtime
    (rarely needed; the executor uses a configured ``SandboxRuntime`` instance).
"""
from __future__ import annotations

from mote.sandbox.detect import detect_backend
from mote.sandbox.runtime import SandboxRuntime
from mote.sandbox.violations import SandboxViolation

__all__ = ["SandboxRuntime", "detect_backend", "SandboxViolation"]
