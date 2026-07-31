"""Unit tests for the GenerateMedia tool (executor/tools/generate_media/).

The tool is a direct fan-out (NOT a graph orchestrator): it resolves each
requested kind's provider via ``create_media_provider`` and calls their
``generate`` concurrently, blocking until every asset resolves, returning one
compact ``{kind: {...}}`` dict. All tests are offline — the provider factory is
monkeypatched so no network or run_rollout import is needed.
"""
from __future__ import annotations

from typing import Any

import pytest

from mote.contracts.tool.execution import ToolExecutionKind
from mote.product.toolsets.builtin.generate_media.generate_media_tool import GenerateMedia, _compact
from mote.runtime.errors import ToolNotConfiguredError


class _FakeMultimodalCfg:
    """A configured-or-not stand-in for one multimodal.*_generation sub-config."""

    def __init__(self, configured: bool):
        self.base_url = "https://example.com" if configured else ""
        self.api_key = "k" if configured else ""  # pragma: allowlist secret
        # Single-model kinds (image/audio/music) read ``model``; the video kind
        # reads two fields — set all three so a *configured* fake also passes the
        # up-front "a model is named" check, not just the base_url/api_key check.
        self.model = "m"
        self.text_to_video_model = "m"
        self.reference_guided_video_model = "m"


class _FakeMultimodal:
    """multimodal config with each service marked configured or not."""

    def __init__(self, **flags: bool):
        self.image_generation = _FakeMultimodalCfg(flags.get("images", False))
        self.audio_generation = _FakeMultimodalCfg(flags.get("audios", False))
        self.music_generation = _FakeMultimodalCfg(flags.get("music", False))
        self.video_generation = _FakeMultimodalCfg(flags.get("videos", False))


_multimodal = _FakeMultimodal()
_providers: dict[str, "_FakeProvider"] = {}


def _tool() -> GenerateMedia:
    to_plural = {
        "image": "images",
        "audio": "audios",
        "music": "music",
        "video": "videos",
    }

    async def invoke_service(*, route_id, capability, operation_key, payload, semantics):
        kind = capability.rsplit(".", 1)[-1]
        fake = _providers[to_plural[kind]]
        outcome = await fake.generate([payload["item"]])
        results = outcome.get("results") or []
        if results:
            return results[0]
        return {
            "status": "success",
            "filename": payload["item"].get("filename"),
            "url": "",
        }

    tool = GenerateMedia(_multimodal)
    tool.invoke_service = invoke_service
    return tool


def _patch_config(_monkeypatch, **flags: bool):
    """Replace the explicitly injected multimodal config."""
    global _multimodal
    _multimodal = _FakeMultimodal(**flags)


class _FakeProvider:
    """A media provider whose ``generate`` returns a canned poll-style result dict."""

    def __init__(self, label: str, result: Any = None, *, raise_batch: bool = False):
        self._label = label
        self._result = result if result is not None else {"summary": f"{label} ok", "results": []}
        self._raise_batch = raise_batch
        self.output_dir = None

    async def generate(self, items):
        if self._raise_batch:
            raise RuntimeError(f"{self._label} batch failed")
        return self._result


def _patch_creators(monkeypatch, **providers: _FakeProvider):
    """Route the ``create_media_provider`` factory to fakes, keyed by (plural) kind.

    The tool dispatches by singular kind (``image``/``audio``/``music``/``video``);
    tests key by plural (matching the tool's list params) for readability, so the
    fake factory bridges singular→plural.
    """
    global _providers
    _providers = dict(providers)

    # Mark each patched kind's service as configured so the up-front config
    # check (``_check_configured``) lets these fan-out tests through.
    _patch_config(monkeypatch, **{kind: True for kind in providers})


@pytest.mark.asyncio
class TestFanOut:
    async def test_metadata(self):
        assert GenerateMedia.name == "GenerateMedia"
        assert "generate_media" in GenerateMedia.aliases
        assert GenerateMedia.execution_kind is ToolExecutionKind.ATOMIC
        assert _tool().can_resume_started_call("call-id") is True

    async def test_empty_request(self):
        res = await _tool().call()
        assert "No media requested" in res["message"]

    async def test_two_kinds_compacted(self, monkeypatch):
        _patch_creators(
            monkeypatch,
            images=_FakeProvider(
                "images",
                {
                    "summary": "1/1 images",
                    "results": [{"status": "success", "filename": "cat.png", "url": "u1"}],
                },
            ),
            audios=_FakeProvider(
                "audios",
                {
                    "summary": "1/1 audios",
                    "results": [{"status": "success", "filename": "hi.mp3", "url": "u2"}],
                },
            ),
        )
        res = await _tool().call(
            images=[{"description": "a cat", "filename": "cat.png"}],
            audios=[{"text": "hello", "filename": "hi.mp3"}],
        )
        assert set(res.keys()) == {"images", "audios"}
        assert res["images"]["summary"] == "1/1 images generated."
        assert res["images"]["assets"] == [{"filename": "cat.png", "url": "u1"}]
        assert res["audios"]["assets"] == [{"filename": "hi.mp3", "url": "u2"}]

    async def test_all_kinds(self, monkeypatch):
        _patch_creators(
            monkeypatch,
            images=_FakeProvider("images"),
            audios=_FakeProvider("audios"),
            music=_FakeProvider("music"),
            videos=_FakeProvider("videos"),
        )
        res = await _tool().call(
            images=[{"description": "x", "filename": "x.png"}],
            audios=[{"text": "y", "filename": "y.mp3"}],
            music=[{"prompt": "z", "filename": "z.mp3"}],
            videos=[{"prompt": "w", "filename": "w.mp4"}],
        )
        assert set(res.keys()) == {"images", "audios", "music", "videos"}

    async def test_partial_kind_failure_kept(self, monkeypatch):
        # One kind's whole batch raises; the sibling's success is preserved.
        _patch_creators(
            monkeypatch,
            images=_FakeProvider(
                "images",
                {
                    "summary": "1/1 images",
                    "results": [{"status": "success", "filename": "x.png", "url": "u"}],
                },
            ),
            videos=_FakeProvider("videos", raise_batch=True),
        )
        res = await _tool().call(
            images=[{"description": "x", "filename": "x.png"}],
            videos=[{"prompt": "w", "filename": "w.mp4"}],
        )
        assert res["images"]["assets"] == [{"filename": "x.png", "url": "u"}]
        assert "videos batch failed" in res["videos"]["error"]

    async def test_all_failed_raises(self, monkeypatch):
        _patch_creators(
            monkeypatch,
            images=_FakeProvider("images", raise_batch=True),
            videos=_FakeProvider("videos", raise_batch=True),
        )
        with pytest.raises(RuntimeError, match="All media generation failed"):
            await _tool().call(
                images=[{"description": "x", "filename": "x.png"}],
                videos=[{"prompt": "w", "filename": "w.mp4"}],
            )


class TestCompact:
    def test_keeps_url_and_local_path(self):
        result = {
            "summary": "2/3 images generated.",
            "results": [
                {
                    "status": "success",
                    "filename": "a.png",
                    "url": "ua",
                    "local_path": "/tmp/a.png",
                },
                {"status": "success", "filename": "b.png", "urls": ["ub"]},
                {"status": "failed", "filename": "c.png", "error": "boom"},
            ],
            "failed": [{"filename": "c.png", "error": "boom"}],
        }
        compact = _compact(result)
        assert compact["summary"] == "2/3 images generated."
        assert compact["assets"] == [
            {"filename": "a.png", "url": "ua", "local_path": "/tmp/a.png"},
            {"filename": "b.png", "url": "ub"},
        ]
        assert compact["failed"] == [{"filename": "c.png", "error": "boom"}]

    def test_no_failed_key_when_all_ok(self):
        result = {
            "summary": "1/1",
            "results": [{"status": "success", "filename": "a.png", "url": "u"}],
        }
        compact = _compact(result)
        assert "failed" not in compact
        assert compact["assets"] == [{"filename": "a.png", "url": "u"}]


@pytest.mark.asyncio
class TestNotConfigured:
    async def test_unconfigured_kind_raises(self, monkeypatch):
        # image service unset → up-front check refuses before any creator runs.
        _patch_config(monkeypatch, images=False)
        with pytest.raises(ToolNotConfiguredError) as excinfo:
            await _tool().call(images=[{"description": "a cat", "filename": "cat.png"}])
        msg = str(excinfo.value)
        assert "not configured" in msg.lower()
        assert "multimodal.image_generation" in msg

    async def test_only_requested_kind_checked(self, monkeypatch):
        # video unconfigured but not requested → an images-only request that is
        # configured succeeds (the check is scoped to requested kinds).
        _patch_creators(
            monkeypatch,
            images=_FakeProvider(
                "images",
                {
                    "summary": "1/1 images",
                    "results": [{"status": "success", "filename": "x.png", "url": "u"}],
                },
            ),
        )
        res = await _tool().call(images=[{"description": "x", "filename": "x.png"}])
        assert res["images"]["assets"] == [{"filename": "x.png", "url": "u"}]

    async def test_reports_every_unconfigured_kind(self, monkeypatch):
        _patch_config(monkeypatch, images=False, videos=False)
        with pytest.raises(ToolNotConfiguredError) as excinfo:
            await _tool().call(
                images=[{"description": "x", "filename": "x.png"}],
                videos=[{"prompt": "w", "filename": "w.mp4"}],
            )
        msg = str(excinfo.value)
        assert "multimodal.image_generation" in msg
        assert "multimodal.video_generation" in msg

    async def test_empty_request_skips_config_check(self, monkeypatch):
        # No kind requested → returns the guidance message without touching config.
        _patch_config(monkeypatch)  # nothing configured
        res = await _tool().call()
        assert "No media requested" in res["message"]

    async def test_endpoint_set_but_no_model_raises(self, monkeypatch):
        # base_url + api_key present but the generation model is unset → the
        # service can't pick a model, so refuse the same way as a missing endpoint.
        cfg = _FakeMultimodalCfg(True)
        cfg.model = ""  # image kind's single model field cleared
        global _multimodal
        _multimodal = type("MM", (), {"image_generation": cfg})()
        with pytest.raises(ToolNotConfiguredError) as excinfo:
            await _tool().call(images=[{"description": "x", "filename": "x.png"}])
        msg = str(excinfo.value)
        assert "no model configured" in msg.lower()
        assert "multimodal.image_generation.model" in msg

    async def test_video_missing_one_model_field_raises(self, monkeypatch):
        # Video carries TWO model fields; leaving either unset refuses and names it.
        cfg = _FakeMultimodalCfg(True)
        cfg.reference_guided_video_model = ""  # only one of the two cleared
        global _multimodal
        _multimodal = type("MM", (), {"video_generation": cfg})()
        with pytest.raises(ToolNotConfiguredError) as excinfo:
            await _tool().call(videos=[{"prompt": "w", "filename": "w.mp4"}])
        msg = str(excinfo.value)
        assert "multimodal.video_generation.reference_guided_video_model" in msg
