"""RateLimitSnapshot — one provider's rate-limit quota as of the last response.

A provider-agnostic, normalized view of the ``*-ratelimit-*`` HTTP headers every
LLM response carries. Two independent quotas are tracked (requests and tokens),
each as a ``limit`` / ``remaining`` / ``reset`` triple, plus the ``retry-after``
hint providers send on a 429 and the local ``observed_at`` wall-clock stamp.

Unlike token *usage* (which accumulates), rate-limit state is a rolling
*snapshot*: each response overwrites the prior one for its ``(provider, model)``
key (last-write-wins) — the newest response is the freshest quota truth. So there
is no summing and no lineage tree (contrast :mod:`router.cost`): a flat keyed map
of latest snapshots is the whole model.

Header dialects handled:

* **OpenAI** — ``x-ratelimit-{limit,remaining,reset}-{requests,tokens}`` (reset is
  a human duration like ``"1s"`` / ``"6m0s"``, parsed to seconds).
* **Anthropic** — ``anthropic-ratelimit-{requests,tokens}-{limit,remaining,reset}``
  (reset is an RFC-3339 timestamp; kept as the raw string, not re-parsed).

Missing headers leave the corresponding fields ``None`` — a snapshot is only
built when at least one recognized header is present (``from_headers`` returns
``None`` otherwise), so a provider that sends nothing never pollutes the map.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Mapping, Optional

from mote.common.exception.handlers import _parse_retry_after


def _to_int(value: Optional[str]) -> Optional[int]:
    """Best-effort ``str -> int`` (handles plain integer header values)."""
    if value is None:
        return None
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return None


def _parse_duration(value: Optional[str]) -> Optional[float]:
    """Parse OpenAI's reset duration (e.g. ``"1s"``, ``"6m0s"``, ``"88ms"``) to seconds.

    OpenAI expresses ``x-ratelimit-reset-*`` as a compact duration. A bare number
    (no unit) is treated as seconds. Unparseable input yields ``None``.
    """
    if value is None:
        return None
    text = value.strip().lower()
    if not text:
        return None
    # A bare number is already seconds.
    try:
        return float(text)
    except ValueError:
        pass
    units = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
    total = 0.0
    num = ""
    i = 0
    matched = False
    while i < len(text):
        ch = text[i]
        if ch.isdigit() or ch == ".":
            num += ch
            i += 1
            continue
        # Longest-unit-first so "ms" beats "m".
        unit = None
        for u in ("ms", "s", "m", "h", "d"):
            if text.startswith(u, i):
                unit = u
                break
        if unit is None or not num:
            return None
        total += float(num) * units[unit]
        num = ""
        i += len(unit)
        matched = True
    if num:  # trailing bare number = seconds
        total += float(num)
        matched = True
    return total if matched else None


@dataclass
class RateLimitSnapshot:
    """The latest known rate-limit quota for one ``(provider, model)`` endpoint."""

    provider: str = ""
    model: str = ""
    #: Requests-quota triple.
    limit_requests: Optional[int] = None
    remaining_requests: Optional[int] = None
    #: Seconds until the requests quota resets (OpenAI) — None if not sent.
    reset_requests_seconds: Optional[float] = None
    #: Raw reset marker as sent (Anthropic RFC-3339 timestamp) — provider-shaped.
    reset_requests_raw: Optional[str] = None
    #: Tokens-quota triple.
    limit_tokens: Optional[int] = None
    remaining_tokens: Optional[int] = None
    reset_tokens_seconds: Optional[float] = None
    reset_tokens_raw: Optional[str] = None
    #: The server's back-off hint on a 429 (seconds), else None.
    retry_after_seconds: Optional[float] = None
    #: Local wall-clock (epoch seconds) when this snapshot was observed.
    observed_at: float = 0.0

    @staticmethod
    def from_headers(provider: str, model: str, headers: Mapping[str, str]) -> Optional["RateLimitSnapshot"]:
        """Build a snapshot from response headers, or ``None`` if none are present.

        ``headers`` is any case-insensitive mapping (httpx ``Headers`` qualifies).
        Dispatches on ``provider`` to read only that dialect's keys (the Anthropic
        header names can never appear on an OpenAI response and vice versa), so a
        response pays one dialect's lookups, not both. An unknown provider tries
        both dialects. Returns ``None`` when no recognized rate-limit header is
        found so callers can skip the observe.
        """
        get = headers.get
        prov = (provider or "").lower()

        limit_requests = remaining_requests = None
        limit_tokens = remaining_tokens = None
        reset_requests_seconds = reset_tokens_seconds = None
        reset_requests_raw = reset_tokens_raw = None

        if prov != "anthropic":  # OpenAI dialect (and the unknown-provider fallback).
            limit_requests = _to_int(get("x-ratelimit-limit-requests"))
            remaining_requests = _to_int(get("x-ratelimit-remaining-requests"))
            limit_tokens = _to_int(get("x-ratelimit-limit-tokens"))
            remaining_tokens = _to_int(get("x-ratelimit-remaining-tokens"))
            reset_requests_seconds = _parse_duration(get("x-ratelimit-reset-requests"))
            reset_tokens_seconds = _parse_duration(get("x-ratelimit-reset-tokens"))

        if limit_requests is None and remaining_requests is None and prov != "openai":
            # Anthropic dialect (also tried when OpenAI keys were absent + unknown).
            limit_requests = _to_int(get("anthropic-ratelimit-requests-limit"))
            remaining_requests = _to_int(get("anthropic-ratelimit-requests-remaining"))
            limit_tokens = _to_int(get("anthropic-ratelimit-tokens-limit"))
            remaining_tokens = _to_int(get("anthropic-ratelimit-tokens-remaining"))
            reset_requests_raw = get("anthropic-ratelimit-requests-reset")
            reset_tokens_raw = get("anthropic-ratelimit-tokens-reset")

        # ``retry-after`` is RFC 7231 (delta-seconds | HTTP-date), NOT a provider
        # duration dialect, so reuse the shared RFC parser rather than _parse_duration.
        raw_retry = get("retry-after")
        retry_after = _parse_retry_after(raw_retry) if raw_retry else None

        # A snapshot is meaningful only if it carries at least one quota signal.
        if all(
            v is None
            for v in (
                limit_requests,
                remaining_requests,
                limit_tokens,
                remaining_tokens,
                retry_after,
            )
        ):
            return None

        return RateLimitSnapshot(
            provider=provider,
            model=model,
            limit_requests=limit_requests,
            remaining_requests=remaining_requests,
            reset_requests_seconds=reset_requests_seconds,
            reset_requests_raw=reset_requests_raw,
            limit_tokens=limit_tokens,
            remaining_tokens=remaining_tokens,
            reset_tokens_seconds=reset_tokens_seconds,
            reset_tokens_raw=reset_tokens_raw,
            retry_after_seconds=retry_after,
            observed_at=time.time(),
        )


__all__ = ["RateLimitSnapshot"]
