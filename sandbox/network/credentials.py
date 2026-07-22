"""Credential broker — per-domain auth injection at the egress proxy.

The trusted-runtime half of eve/Vercel-style credential brokering. A sandboxed
tool (``git`` / ``curl`` / ``wget``) needs to reach an authenticated endpoint,
but the secret must **never enter the sandbox process** (an adversarial process
could exfiltrate a materialised ``.netrc`` / env var through an allowed domain).
So the secret stays in the trusted app runtime and is injected as an HTTP header
at mote's single egress chokepoint — the proxy — which is the only component
that ever sees both the secret and the request.

Two collaborators, no I/O of its own:

  * a list of :class:`CredentialRule` (compiled from
    :class:`~mote.common.schema.sandbox_runtime_config.CredentialConfig`), each
    binding a set of host globs to a secret **key** + a header shape;
  * a ``secret_lookup`` closure resolving that key to a plaintext value lazily,
    per request (so a hot-rotated secret is honoured without a restart) — the
    value lives only in the encrypted vault, never in this object's state.

Fail-closed everywhere: no matching rule, or a rule whose secret is missing /
empty, yields ``None`` (no partial or empty header is ever emitted). The broker
is entirely inert when no rules are configured (the proxy is constructed with
``broker=None``).
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Callable, Optional

from mote.common.net.host_match import matches_pattern, normalize_host

#: Resolve a secret *key* to its plaintext value (or ``None`` if unknown).
SecretLookup = Callable[[str], Optional[str]]


@dataclass(frozen=True)
class CredentialRule:
    """One compiled credential-brokering rule (domains → secret key + shape)."""

    domains: tuple[str, ...]
    secret_key: str
    scheme: str = "bearer"  # bearer | basic | header
    header: str = "Authorization"
    username: Optional[str] = None

    def matches(self, host: str) -> bool:
        """True when *host* matches any of this rule's domain globs."""
        h = normalize_host(host)
        if not h:
            return False
        return any(matches_pattern(h, pattern) for pattern in self.domains)


class CredentialBroker:
    """Resolve a per-host auth header from configured rules + a secret lookup."""

    def __init__(self, rules: list[CredentialRule], secret_lookup: SecretLookup) -> None:
        self._rules = list(rules)
        self._lookup = secret_lookup

    def match(self, host: str) -> Optional[CredentialRule]:
        """Return the first rule matching *host*, or ``None``."""
        for rule in self._rules:
            if rule.matches(host):
                return rule
        return None

    def header_for(self, host: str) -> Optional[tuple[str, str]]:
        """Build the ``(name, value)`` auth header for *host*, or ``None``.

        Fail-closed: returns ``None`` when no rule matches OR the referenced
        secret is missing/empty (never a partial or empty-valued header). The
        secret is resolved lazily via the injected lookup, so a rotated value is
        picked up on the next request.
        """
        rule = self.match(host)
        if rule is None:
            return None
        secret = self._lookup(rule.secret_key)
        if not secret:
            return None

        if rule.scheme == "bearer":
            return rule.header, f"Bearer {secret}"
        if rule.scheme == "basic":
            raw = f"{rule.username or ''}:{secret}".encode("utf-8")
            return rule.header, "Basic " + base64.b64encode(raw).decode("ascii")
        # scheme == "header": the raw secret as the header value.
        return rule.header, secret

    def should_intercept(self, host: str) -> bool:
        """True iff a rule matches *host* (Phase 2 uses this to decide MITM).

        Note this is intentionally lookup-independent: interception is decided by
        rule *configuration*, not by whether the secret currently resolves. A
        missing secret still MITMs (then :meth:`header_for` fail-closes to no
        header) rather than silently raw-splicing an intended-credentialed host.
        """
        return self.match(host) is not None

    @property
    def intercept_hosts(self) -> list[str]:
        """All configured domain globs across every rule (for trust-anchor env)."""
        hosts: list[str] = []
        for rule in self._rules:
            hosts.extend(rule.domains)
        return hosts


__all__ = ["CredentialBroker", "CredentialRule", "SecretLookup"]
