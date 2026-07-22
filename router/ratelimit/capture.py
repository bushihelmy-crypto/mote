"""The one universal seam that feeds :class:`RateLimitTracker` — an httpx hook.

Every provider SDK (Anthropic, OpenAI, OpenAI-Responses) drives its calls through
an internal ``httpx.AsyncClient`` reachable as ``sdk_client._client``, whose
``event_hooks["response"]`` is a mutable list httpx iterates on *every* response —
streaming or not, success or error, tool call or plain completion. Appending one
response hook there captures the ``*-ratelimit-*`` headers off the hot path with
zero per-call-site plumbing and zero extra request, preserving every SDK default.

The hook is fully lazy on purpose: it reads the tracker (and provider/model) off
a closure at *response* time, not install time, so it survives the two ordering
facts of provider construction — the client is built in ``__init__`` *before* the
shared tracker is injected, and credential rotation rebuilds the client (a fresh
empty-hooks client) mid-session. Install it inside ``_rebuild_client`` and it is
correct through both. It is also fully best-effort: any failure (missing tracker,
odd header shape) is swallowed so telemetry can never break a live call.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from mote.router.ratelimit.tracker import RateLimitTracker


def install_rate_limit_hook(
    sdk_client: Any,
    *,
    get_tracker: Callable[[], Optional[RateLimitTracker]],
    provider: str,
    model: str,
) -> None:
    """Append a response hook to *sdk_client*'s httpx client (best-effort, no-op on miss).

    ``get_tracker`` is consulted per response so a tracker injected *after* the
    client is built (the normal order) is still observed; when it returns ``None``
    the hook is inert. Reading ``response.headers`` never consumes a streaming
    body. Any error installing or firing the hook is swallowed — capture must
    never perturb the request path.
    """

    async def _hook(response: Any) -> None:
        tracker = get_tracker()
        if tracker is None:
            return
        tracker.observe_headers(provider, model, response.headers)

    try:
        sdk_client._client.event_hooks["response"].append(_hook)
    except Exception:  # noqa: BLE001 — telemetry install must never break client build
        return


__all__ = ["install_rate_limit_hook"]
