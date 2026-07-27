"""Network env injection — point a sandboxed command at the egress proxy.

P1's pragmatic network confinement: rather than unsharing the network namespace
(which would also cut off the proxy), we leave the network shared and inject the
standard proxy environment variables so that proxy-honouring tools (curl / pip /
git / requests with trust_env) route through our local
:class:`~mote.runtime.sandbox.network.proxy.EgressProxy`. (A sandboxed Jupyter kernel
is orthogonal: its control channels run over ipc:// unix sockets, unaffected by
either the netns or the proxy env.)

⚠️ Documented limitation: this constrains tools that *respect* the proxy vars.
Code that deliberately opens a raw socket bypasses it. Hard封堵 requires seccomp
(P3) or a netns whose only egress is the proxy (P2). P1 accepts this trade-off.

When the policy is "deny all" (no allowed domains), we still inject the proxy —
the proxy then 403s every host, which is the desired closed-by-default stance
for proxy-honouring tools.
"""
from __future__ import annotations

# Both lower- and upper-case forms are injected: different tools read different
# casings (curl prefers lower-case, many libraries read upper-case).
_PROXY_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
)

# Hosts that must always bypass the proxy (loopback traffic for any
# proxy-honouring tool; the sandboxed kernel uses ipc:// sockets, not these).
_NO_PROXY = "127.0.0.1,localhost,::1"


def inject_proxy_env(env: dict[str, str], proxy_url: str) -> dict[str, str]:
    """Return a copy of *env* with the proxy variables pointed at *proxy_url*.

    Always returns a new dict (never mutates the caller's). ``NO_PROXY`` is set
    so loopback traffic from proxy-honouring tools is never sent through the
    proxy. (The sandboxed Jupyter kernel does not rely on this — its channels are
    ipc:// unix sockets, not loopback TCP.)
    """
    out = dict(env)
    for var in _PROXY_VARS:
        out[var] = proxy_url
    out["NO_PROXY"] = _NO_PROXY
    out["no_proxy"] = _NO_PROXY
    return out


def block_all_network_env(env: dict[str, str]) -> dict[str, str]:
    """Return a copy of *env* pointing proxies at an unroutable sink.

    For ``network='off'``: set the proxy vars to a closed loopback port so any
    proxy-honouring egress fails fast. (Not a hard block — see module note.)
    """
    out = dict(env)
    # Port 9 (discard) on loopback — connections refused immediately.
    dead = "http://127.0.0.1:9"
    for var in _PROXY_VARS:
        out[var] = dead
    return out


__all__ = ["inject_proxy_env", "block_all_network_env"]
