"""mote.runtime.sandbox.network — local egress proxy + domain/SSRF policy.

The OS-level sandbox's network half (P1). Two pieces:

  * :mod:`.policy` — host normalisation + SSRF rejection (private/loopback/
    link-local ranges) + a domain allowlist with glob support. A pure-data,
    dependency-free module ported from Codex's ``network-proxy/src/policy.rs``.
  * :mod:`.proxy` — a small asyncio HTTP/CONNECT proxy that consults the policy
    on every request, listening on a random loopback port. The sandboxed
    command is pointed at it via ``HTTP_PROXY``/``HTTPS_PROXY`` env injection
    (see :mod:`.netns`).

P1 limitation (documented): env-var proxy injection only constrains tools that
*honour* the proxy variables (curl/pip/git). It does NOT stop code that
deliberately opens a direct socket. True封堵 needs seccomp (P3) or a netns with
the proxy as the sole egress (P2).

P2 (``enforce`` + ``orchestrator``): closes the env-proxy hole by giving the
sandboxed process a fresh network namespace whose only route out is the proxy
(``bwrap --unshare-net`` + ``slirp4netns`` gateway + an ``nft`` default-drop
lock). Enforced for *all* egress, proxy-honouring or not. Degrades to the P1
env-proxy when the toolchain (slirp4netns / nft) is absent.
"""
from __future__ import annotations

from mote.runtime.sandbox.network.policy import NetworkPolicy, is_blocked_host

__all__ = ["NetworkPolicy", "is_blocked_host"]
