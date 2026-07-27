"""Network policy — SSRF rejection + domain allowlist (ported from codex).

Two independent checks compose into the proxy's allow/deny decision:

1. :func:`is_blocked_host` — refuse connections to non-routable / internal
   address space (SSRF defence): loopback, RFC1918 private, link-local, CGNAT
   (100.64/10), multicast, TEST-NET, and the unspecified address. Applies to
   both literal IPs and resolved hostnames.

2. :class:`NetworkPolicy.allows` — a domain allowlist with glob support:
     * ``example.com``   — exact host match.
     * ``*.example.com`` — match one label (``api.example.com`` but not
       ``example.com`` nor ``a.b.example.com``).
     * ``**.example.com``— match the apex + any number of subdomain labels.

Ported from Codex ``network-proxy/src/policy.rs``; our blocked-range table is a
touch more conservative.

``normalize_host`` and the glob matcher live in :mod:`mote.contracts.net`
(a pure leaf module) so the config layer can validate against the exact same
matcher without reaching up into this runtime package; they are re-exported here
for the proxy's own use and backward compatibility.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from mote.contracts.net import matches_pattern, normalize_host


def is_blocked_host(host: str) -> bool:
    """True when *host* is an internal / non-routable address (SSRF risk).

    Hostnames (non-IP) are NOT blocked here — they are gated by the allowlist
    instead; this function only rejects IP literals that fall in a private /
    loopback / link-local / reserved range. An unparseable value is treated as
    a hostname (not blocked by this check).
    """
    h = normalize_host(host)
    if not h:
        return True  # empty host is never a valid egress target

    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False  # a hostname — allowlist decides, not the SSRF filter

    # Standard non-routable / internal categories.
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return True

    # CGNAT 100.64.0.0/10 — shared address space, not caught by is_private.
    if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"):
        return True

    # TEST-NET ranges (documentation/example space) — not real egress.
    if isinstance(ip, ipaddress.IPv4Address):
        for net in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"):
            if ip in ipaddress.ip_network(net):
                return True

    return False


@dataclass
class NetworkPolicy:
    """A domain allowlist + SSRF gate for the egress proxy.

    ``allowed_domains`` empty means *deny all* (the proxy rejects every host).
    Set a wildcard ``**.*``-style entry to widen; there is intentionally no
    implicit allow-all so a misconfigured policy fails closed.
    """

    allowed_domains: list[str]

    def allows(self, host: str) -> bool:
        """True when *host* passes the SSRF gate AND matches the allowlist."""
        h = normalize_host(host)
        if not h:
            return False
        if is_blocked_host(h):
            return False
        return any(matches_pattern(h, p) for p in self.allowed_domains)


__all__ = ["NetworkPolicy", "is_blocked_host", "normalize_host", "matches_pattern"]
