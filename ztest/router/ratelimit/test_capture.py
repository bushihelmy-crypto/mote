"""install_rate_limit_hook: lazy tracker read, best-effort install, header capture."""

from __future__ import annotations

import asyncio

from mote.router.ratelimit import RateLimitTracker
from mote.router.ratelimit.capture import install_rate_limit_hook


class _FakeHttpx:
    def __init__(self):
        self.event_hooks = {"request": [], "response": []}


class _FakeSDKClient:
    """Mimics an SDK client exposing its httpx client as ``._client``."""

    def __init__(self):
        self._client = _FakeHttpx()


class _FakeResponse:
    def __init__(self, headers):
        self.headers = headers


def _fire(hook, headers):
    asyncio.new_event_loop().run_until_complete(hook(_FakeResponse(headers)))


class TestInstall:
    def test_appends_response_hook(self):
        client = _FakeSDKClient()
        install_rate_limit_hook(client, get_tracker=lambda: RateLimitTracker(), provider="openai", model="gpt-4")
        assert len(client._client.event_hooks["response"]) == 1

    def test_bad_client_shape_is_noop(self):
        # A client without ._client must not raise at install time.
        class NoInner:
            pass

        install_rate_limit_hook(
            NoInner(), get_tracker=lambda: RateLimitTracker(), provider="openai", model="gpt-4"
        )  # no exception


class TestCapture:
    def test_hook_observes_into_tracker(self):
        client = _FakeSDKClient()
        tracker = RateLimitTracker()
        install_rate_limit_hook(client, get_tracker=lambda: tracker, provider="openai", model="gpt-4")
        hook = client._client.event_hooks["response"][0]
        _fire(hook, {"x-ratelimit-remaining-requests": "4999"})
        snap = tracker.get("openai", "gpt-4")
        assert snap is not None and snap.remaining_requests == 4999

    def test_tracker_read_lazily_after_install(self):
        # The tracker may be injected AFTER the client is built (the normal order).
        client = _FakeSDKClient()
        holder = {"tracker": None}
        install_rate_limit_hook(client, get_tracker=lambda: holder["tracker"], provider="anthropic", model="claude")
        hook = client._client.event_hooks["response"][0]
        # No tracker yet → inert, no raise.
        _fire(hook, {"anthropic-ratelimit-requests-remaining": "5"})
        # Now inject and fire again.
        holder["tracker"] = RateLimitTracker()
        _fire(hook, {"anthropic-ratelimit-requests-remaining": "4"})
        assert holder["tracker"].get("anthropic", "claude").remaining_requests == 4

    def test_none_tracker_never_raises(self):
        client = _FakeSDKClient()
        install_rate_limit_hook(client, get_tracker=lambda: None, provider="openai", model="gpt-4")
        hook = client._client.event_hooks["response"][0]
        _fire(hook, {"x-ratelimit-remaining-requests": "1"})  # no exception
