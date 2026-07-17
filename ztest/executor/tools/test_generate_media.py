"""Unit tests for the GenerateMedia tool (executor/tools/generate_media/).

The tool is a direct fan-out (NOT a graph orchestrator): it calls the four
creators concurrently and blocks until every asset resolves, returning one
compact ``{kind: {...}}`` dict. All tests are offline — the creator classes are
monkeypatched so no network or run_rollout import is needed.
"""
from __future__ import annotations

from typing import Any

import pytest

from mote.common.exception import ToolNotConfiguredError
from mote.executor.tools.generate_media import generate_media_tool as gm
from mote.executor.tools.generate_media.generate_media_tool import GenerateMedia, _compact

pytestmark = pytest.mark.asyncio


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


def _patch_config(monkeypatch, **flags: bool):
    """Route the tool's config-check to a fake multimodal config."""
    from mote.common.config import loader

    fake = type("Cfg", (), {"multimodal": _FakeMultimodal(**flags)})()
    monkeypatch.setattr(loader, "load_config", lambda *a, **k: fake)


class _FakeCreator:
    """A creator whose generate_* method returns a canned poll-style result dict."""

    def __init__(self, label: str, result: Any = None, *, raise_batch: bool = False):
        self._label = label
        self._result = result if result is not None else {"summary": f"{label} ok", "results": []}
        self._raise_batch = raise_batch

    def __call__(self, output_dir=None):
        # Creators are constructed as Creator(output_dir); return self so the
        # bound generate_* method below runs.
        self.output_dir = output_dir
        return self

    async def _generate(self, items, **_):
        if self._raise_batch:
            raise RuntimeError(f"{self._label} batch failed")
        return self._result


def _patch_creators(monkeypatch, **creators: _FakeCreator):
    """Route each creator's generate_* method to a fake, keyed by kind."""
    mapping = {
        "images": ("ImageCreator", "generate_images"),
        "audios": ("AudioCreator", "generate_audios"),
        "music": ("MusicCreator", "generate_music"),
        "videos": ("VideoCreator", "generate_videos"),
    }
    for kind, fake in creators.items():
        cls_name, method = mapping[kind]

        # Bind ``method``/``fake`` as defaults so each iteration's values are
        # captured (avoids the late-binding closure trap).
        def _make_cls(_method=method, _fake=fake):
            class _Cls:
                def __init__(self, output_dir=None):
                    setattr(self, _method, _fake._generate)

            return _Cls

        monkeypatch.setattr(gm, cls_name, _make_cls())

    # Mark each patched kind's service as configured so the up-front config
    # check (``_check_configured``) lets these fan-out tests through.
    _patch_config(monkeypatch, **{kind: True for kind in creators})


class TestFanOut:
    async def test_metadata(self):
        assert GenerateMedia.name == "GenerateMedia"
        assert "generate_media" in GenerateMedia.aliases
        assert GenerateMedia.is_graph_tool is False

    async def test_empty_request(self):
        res = await GenerateMedia().call()
        assert "No media requested" in res["message"]

    async def test_two_kinds_compacted(self, monkeypatch):
        _patch_creators(
            monkeypatch,
            images=_FakeCreator(
                "images",
                {"summary": "1/1 images", "results": [{"status": "success", "filename": "cat.png", "url": "u1"}]},
            ),
            audios=_FakeCreator(
                "audios",
                {"summary": "1/1 audios", "results": [{"status": "success", "filename": "hi.mp3", "url": "u2"}]},
            ),
        )
        res = await GenerateMedia().call(
            images=[{"description": "a cat", "filename": "cat.png"}],
            audios=[{"text": "hello", "filename": "hi.mp3"}],
        )
        assert set(res.keys()) == {"images", "audios"}
        assert res["images"]["summary"] == "1/1 images"
        assert res["images"]["assets"] == [{"filename": "cat.png", "url": "u1"}]
        assert res["audios"]["assets"] == [{"filename": "hi.mp3", "url": "u2"}]

    async def test_all_kinds(self, monkeypatch):
        _patch_creators(
            monkeypatch,
            images=_FakeCreator("images"),
            audios=_FakeCreator("audios"),
            music=_FakeCreator("music"),
            videos=_FakeCreator("videos"),
        )
        res = await GenerateMedia().call(
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
            images=_FakeCreator(
                "images",
                {"summary": "1/1 images", "results": [{"status": "success", "filename": "x.png", "url": "u"}]},
            ),
            videos=_FakeCreator("videos", raise_batch=True),
        )
        res = await GenerateMedia().call(
            images=[{"description": "x", "filename": "x.png"}],
            videos=[{"prompt": "w", "filename": "w.mp4"}],
        )
        assert res["images"]["assets"] == [{"filename": "x.png", "url": "u"}]
        assert "videos batch failed" in res["videos"]["error"]

    async def test_all_failed_raises(self, monkeypatch):
        _patch_creators(
            monkeypatch,
            images=_FakeCreator("images", raise_batch=True),
            videos=_FakeCreator("videos", raise_batch=True),
        )
        with pytest.raises(RuntimeError, match="All media generation failed"):
            await GenerateMedia().call(
                images=[{"description": "x", "filename": "x.png"}],
                videos=[{"prompt": "w", "filename": "w.mp4"}],
            )


class TestCompact:
    def test_keeps_url_and_local_path(self):
        result = {
            "summary": "2/3 images generated.",
            "results": [
                {"status": "success", "filename": "a.png", "url": "ua", "local_path": "/tmp/a.png"},
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
        result = {"summary": "1/1", "results": [{"status": "success", "filename": "a.png", "url": "u"}]}
        compact = _compact(result)
        assert "failed" not in compact
        assert compact["assets"] == [{"filename": "a.png", "url": "u"}]


class TestNotConfigured:
    async def test_unconfigured_kind_raises(self, monkeypatch):
        # image service unset → up-front check refuses before any creator runs.
        _patch_config(monkeypatch, images=False)
        with pytest.raises(ToolNotConfiguredError) as excinfo:
            await GenerateMedia().call(images=[{"description": "a cat", "filename": "cat.png"}])
        msg = str(excinfo.value)
        assert "not configured" in msg.lower()
        assert "multimodal.image_generation" in msg

    async def test_only_requested_kind_checked(self, monkeypatch):
        # video unconfigured but not requested → an images-only request that is
        # configured succeeds (the check is scoped to requested kinds).
        _patch_creators(
            monkeypatch,
            images=_FakeCreator(
                "images",
                {"summary": "1/1 images", "results": [{"status": "success", "filename": "x.png", "url": "u"}]},
            ),
        )
        res = await GenerateMedia().call(images=[{"description": "x", "filename": "x.png"}])
        assert res["images"]["assets"] == [{"filename": "x.png", "url": "u"}]

    async def test_reports_every_unconfigured_kind(self, monkeypatch):
        _patch_config(monkeypatch, images=False, videos=False)
        with pytest.raises(ToolNotConfiguredError) as excinfo:
            await GenerateMedia().call(
                images=[{"description": "x", "filename": "x.png"}],
                videos=[{"prompt": "w", "filename": "w.mp4"}],
            )
        msg = str(excinfo.value)
        assert "multimodal.image_generation" in msg
        assert "multimodal.video_generation" in msg

    async def test_empty_request_skips_config_check(self, monkeypatch):
        # No kind requested → returns the guidance message without touching config.
        _patch_config(monkeypatch)  # nothing configured
        res = await GenerateMedia().call()
        assert "No media requested" in res["message"]

    async def test_endpoint_set_but_no_model_raises(self, monkeypatch):
        # base_url + api_key present but the generation model is unset → the
        # service can't pick a model, so refuse the same way as a missing endpoint.
        from mote.common.config import loader

        cfg = _FakeMultimodalCfg(True)
        cfg.model = ""  # image kind's single model field cleared
        fake = type("Cfg", (), {"multimodal": type("MM", (), {"image_generation": cfg})()})()
        monkeypatch.setattr(loader, "load_config", lambda *a, **k: fake)
        with pytest.raises(ToolNotConfiguredError) as excinfo:
            await GenerateMedia().call(images=[{"description": "x", "filename": "x.png"}])
        msg = str(excinfo.value)
        assert "no model configured" in msg.lower()
        assert "multimodal.image_generation.model" in msg

    async def test_video_missing_one_model_field_raises(self, monkeypatch):
        # Video carries TWO model fields; leaving either unset refuses and names it.
        from mote.common.config import loader

        cfg = _FakeMultimodalCfg(True)
        cfg.reference_guided_video_model = ""  # only one of the two cleared
        fake = type("Cfg", (), {"multimodal": type("MM", (), {"video_generation": cfg})()})()
        monkeypatch.setattr(loader, "load_config", lambda *a, **k: fake)
        with pytest.raises(ToolNotConfiguredError) as excinfo:
            await GenerateMedia().call(videos=[{"prompt": "w", "filename": "w.mp4"}])
        msg = str(excinfo.value)
        assert "multimodal.video_generation.reference_guided_video_model" in msg
