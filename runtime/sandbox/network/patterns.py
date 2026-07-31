"""Pure hostname normalization and domain-glob matching.

Leaf-layer text logic (stdlib only) shared by two owners that must agree on the
exact matching rule:

  * the egress proxy's live allow/deny decision
    (:mod:`mote.runtime.sandbox.network.policy`), and
  * the config-time subset check that a credential rule's domains fall within
    ``allowed_domains`` (:class:`mote.contracts.config.tool.SandboxRuntimeConfig`).

Keeping the matcher here (not in the sandbox runtime package) lets the schema
layer validate against the *same* function the proxy uses without reaching
upward into the runtime — one source of truth, one directional dependency.

Glob patterns (see :func:`matches_pattern`):
  * ``example.com``   — exact host match.
  * ``*.example.com`` — exactly one extra label (``api.example.com`` but not
    ``example.com`` nor ``a.b.example.com``).
  * ``**.example.com``— the apex + any number of subdomain labels.
"""

from __future__ import annotations


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


def matches_pattern(host: str, pattern: str) -> bool:
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


__all__ = ["normalize_host", "matches_pattern"]
