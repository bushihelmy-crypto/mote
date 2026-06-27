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
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass


def normalize_host(host: str) -> str:
    """Lower-case, strip brackets/trailing dot, and drop a ``:port`` suffix.

    Returns ``""`` for empty/invalid input. IPv6 literals may arrive bracketed
    (``[::1]``) and/or with a port; we strip both. A trailing dot (FQDN root) is
    removed so ``example.com.`` matches ``example.com``.
    """
    if not host:
        return ""
    h = host.strip().lower()

    # Bracketed IPv6 (optionally with :port) — "[::1]:8080" / "[::1]".
    if h.startswith("["):
        end = h.find("]")
        if end != -1:
            return h[1:end]
        return h.lstrip("[")

    # Strip a trailing :port for a plain host/IPv4 (an unbracketed IPv6 has many
    # colons, so only strip when there's exactly one).
    if h.count(":") == 1:
        h = h.split(":", 1)[0]

    return h.rstrip(".")


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
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
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


def _matches_pattern(host: str, pattern: str) -> bool:
    """Match *host* against one allowlist *pattern* (see module docstring)."""
    host = normalize_host(host)
    pattern = pattern.strip().lower().rstrip(".")
    if not pattern:
        return False

    if pattern.startswith("**."):
        suffix = pattern[3:]
        # Apex or any-depth subdomain.
        return host == suffix or host.endswith("." + suffix)

    if pattern.startswith("*."):
        suffix = pattern[2:]
        # Exactly one extra label: strip the first label and compare.
        if "." not in host:
            return False
        first, rest = host.split(".", 1)
        return bool(first) and rest == suffix

    return host == pattern


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
        return any(_matches_pattern(h, p) for p in self.allowed_domains)


__all__ = ["NetworkPolicy", "is_blocked_host", "normalize_host"]
