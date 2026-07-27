"""Netns sole-egress enforcement — the P2 building blocks (pure / testable).

P1's network confinement injects ``HTTP_PROXY`` env vars: it only constrains
tools that *honour* them. A program that opens a raw socket bypasses the proxy
entirely. P2 closes that hole by making the local :class:`EgressProxy` the
**sole egress** of the sandboxed process:

  1. ``bwrap --unshare-net`` gives the inner command a fresh, empty network
     namespace (no interfaces but loopback).
  2. ``slirp4netns`` (an unprivileged userspace network stack) attaches to that
     namespace via the bwrap child-pid and provides a tap device + a gateway at
     ``10.0.2.2`` that reaches the **host's** ``127.0.0.1`` — i.e. the proxy.
  3. An ``nft`` ruleset inside the namespace (default-drop on output) permits
     only loopback, established flows, DNS, and TCP to ``10.0.2.2:<proxy_port>``.
     Every other destination is dropped at the kernel — a raw socket to a public
     IP simply times out.

So the proxy's allowlist is enforced for *all* egress, honoured or not. The
``HTTP_PROXY`` env is still injected (pointed at the gateway) so proxy-aware
tools route cleanly; the nft lock is the backstop for everything else.

This module is the **pure, dependency-light half**: toolchain detection plus the
argv / ruleset / inner-prelude *builders*. The stateful coordination (spawn
bwrap, read the child-pid, spawn slirp, lifecycle) lives in
:mod:`.orchestrator`. Splitting them keeps the builders unit-testable without
root or a network namespace.

Empirically validated on this host (bwrap 0.6.1 / slirp4netns 1.0.1 / nft
1.0.2): the gateway reaches host loopback, ``--uid 0 --gid 0`` is REQUIRED for
``--cap-add CAP_NET_ADMIN`` to take effect (bwrap's default ``--unshare-user``
keeps the caller's uid, which is not userns-root), and the inner can self-wait
on slirp's default route before installing the nft lock.
"""
from __future__ import annotations

import os
import shutil
import sys

# The slirp4netns gateway address. slirp4netns always provisions the tap
# interface as 10.0.2.100/24 with the gateway (which forwards to the host's
# loopback) at 10.0.2.2 — the same convention QEMU's user-mode networking uses.
SLIRP_GATEWAY = "10.0.2.2"

# The tap interface name we ask slirp4netns to create inside the namespace.
TAP_DEVICE = "tap0"

# The character device slirp4netns needs to open a userspace tap.
TUN_DEVICE = "/dev/net/tun"

# Large MTU (slirp4netns max is 65521) — loopback-bound traffic benefits from
# big frames; matches the value used in the validation probes.
_SLIRP_MTU = 65520


def enforcement_available() -> bool:
    """True when the netns sole-egress chain can run on this host.

    Requires Linux + the ``bwrap``, ``slirp4netns`` and ``nft`` binaries on
    PATH, plus the ``/dev/net/tun`` character device slirp needs. Never raises;
    a missing piece simply means the runtime falls back to env-only proxying.
    """
    if not sys.platform.startswith("linux"):
        return False
    for binary in ("bwrap", "slirp4netns", "nft"):
        if shutil.which(binary) is None:
            return False
    return os.path.exists(TUN_DEVICE)


def build_slirp_argv(child_pid: int, *, ready_fd: int) -> list[str]:
    """Argv to attach ``slirp4netns`` to the bwrap child's network namespace.

    Args:
        child_pid: the pid bwrap reports on ``--info-fd`` (the inner command's
            pid, whose netns slirp joins via ``setns``).
        ready_fd: an inheritable pipe write-end; slirp writes one byte to it once
            the tap is configured, letting the orchestrator know egress is live.

    ``--configure`` brings the tap up + installs the default route; the gateway
    becomes :data:`SLIRP_GATEWAY`.
    """
    return [
        "slirp4netns",
        "--configure",
        f"--mtu={_SLIRP_MTU}",
        f"--ready-fd={ready_fd}",
        str(child_pid),
        TAP_DEVICE,
    ]


def build_nft_ruleset(proxy_port: int) -> str:
    """The nftables ruleset locking egress to loopback + the proxy gateway.

    A single ``inet`` table with a default-drop ``output`` hook. Permits:
      * anything on ``lo`` (the namespace's own loopback / IPC),
      * established/related flows (so accepted connections get their replies),
      * UDP/53 (DNS — the proxy still applies the host allowlist to the actual
        connection, so leaking DNS does not widen the egress policy),
      * TCP to ``10.0.2.2:<proxy_port>`` — the slirp gateway forwarding to the
        host proxy.
    Everything else is dropped, so a raw socket to a public IP times out.
    """
    return (
        "table inet sbx {\n"
        "  chain out {\n"
        "    type filter hook output priority 0; policy drop;\n"
        '    oif "lo" accept\n'
        "    ct state established,related accept\n"
        "    udp dport 53 accept\n"
        f"    ip daddr {SLIRP_GATEWAY} tcp dport {proxy_port} accept\n"
        "  }\n"
        "}\n"
    )


def proxy_url_in_netns(proxy_port: int) -> str:
    """The proxy URL as seen from *inside* the namespace (via the gateway).

    The host proxy listens on ``127.0.0.1:<port>``, but inside the netns that
    loopback is the namespace's own — the host is reachable only through the
    slirp gateway. So proxy-aware tools must target ``10.0.2.2:<port>``.
    """
    return f"http://{SLIRP_GATEWAY}:{proxy_port}"


def build_inner_prelude(proxy_port: int) -> str:
    """A POSIX-sh prelude that sets up the locked netns, then ``exec``s ``"$@"``.

    Run as the inner command's first action (before the real payload). Steps:
      1. bring loopback up,
      2. busy-wait (≤~10s) for slirp to install the default route via the
         gateway — the signal that egress is live,
      3. install the nft lock (default-drop, proxy-only),
      4. **drop all capabilities** (incl. CAP_NET_ADMIN) so the payload cannot
         tamper with the lock, then
      5. ``exec "$@"`` — replace the shell with the real payload so signal/PTY
         routing is preserved (no lingering wrapper process).

    The caller passes the real argv as the positional parameters (``sh -c
    '<prelude>' sbx <payload...>``). nft/ip failures are tolerated best-effort:
    if the lock can't be installed the payload still runs, but egress is then
    only env-proxy constrained (degraded, never a hard break).

    Tamper-proofing (step 4): the inner process is userns-root WITH
    ``CAP_NET_ADMIN`` (needed to bring lo up + install the nft rules). That same
    capability would let a malicious payload run ``nft flush ruleset`` and
    dismantle the lock — re-opening direct egress through slirp's still-live NAT,
    collapsing P2 back to P1's honour-system. So once the lock is installed the
    capability is no longer needed and MUST be surrendered before handing control
    to the payload. ``capsh --caps=""`` clears the effective/permitted/inheritable
    AND ambient sets (the ambient set is the one that survives ``execve`` — a
    plain ``--drop`` only touches the bounding set and is NOT enough: the cap
    revives through ambient on exec). Clearing one's *own* caps needs no privilege
    (only ``--drop`` of the bounding set would need CAP_SETPCAP), so no extra
    capability is granted. The residual bounding-set entry is inert: with
    ambient/permitted empty and no CAP_SETPCAP, neither root nor any re-exec can
    raise it back (empirically verified). If ``capsh`` is unavailable the prelude
    degrades to a direct ``exec "$@"`` (lock installed but tamper-droppable) — a
    best-effort posture consistent with the rest of the chain.
    """
    ruleset = build_nft_ruleset(proxy_port)
    # Heredoc-fed nft ruleset; `|| true` keeps a partial setup from aborting the
    # payload. The route-wait caps iterations so a slirp failure can't hang.
    #
    # The capability surrender uses ``exec capsh --caps="" -- -c 'exec "$@"'``:
    # the OUTER exec replaces the prelude shell with capsh; capsh clears all caps
    # then the INNER exec replaces capsh with the real payload. The double
    # ``"$@"`` threads the positional params (the real argv) through both layers
    # untouched (verified: ``... sbx "$@"`` re-passes them to capsh's -c body).
    return (
        "ip link set lo up 2>/dev/null || true\n"
        "i=0\n"
        f"while ! ip route show default 2>/dev/null | grep -q '{SLIRP_GATEWAY}'; do\n"
        "  i=$((i+1)); [ $i -gt 200 ] && break; sleep 0.05\n"
        "done\n"
        "nft -f - <<'__SBX_NFT__' 2>/dev/null || true\n"
        f"{ruleset}"
        "__SBX_NFT__\n"
        "if command -v capsh >/dev/null 2>&1; then\n"
        '  exec capsh --caps="" -- -c \'exec "$@"\' sbx "$@"\n'
        "fi\n"
        'exec "$@"\n'
    )


__all__ = [
    "SLIRP_GATEWAY",
    "TAP_DEVICE",
    "TUN_DEVICE",
    "enforcement_available",
    "build_slirp_argv",
    "build_nft_ruleset",
    "build_inner_prelude",
    "proxy_url_in_netns",
]
