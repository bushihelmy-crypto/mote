#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for Runtime message-repair recovery handlers."""

from __future__ import annotations

import base64
import io

import pytest

from mote.contracts.foundation.errors.codes import RecoveryAction
from mote.contracts.model.provider_errors import (
    LLMImageTooLargeError,
    LLMInvalidRequestStateError,
    LLMMultimodalToolContentError,
)
from mote.runtime.models.clients.transformers import (
    _IMAGE_TARGET_BYTES,
    DEFAULT_MESSAGE_TRANSFORMERS,
    downgrade_tool_content,
    shrink_image,
    strip_request_state,
)


def _big_image_data_url() -> str:
    """A real, oversized PNG data URL (random noise so it won't compress away)."""
    Image = pytest.importorskip("PIL.Image")
    import os

    # noise so PNG can't shrink it to nothing
    img = Image.frombytes("RGB", (2048, 2048), os.urandom(2048 * 2048 * 3))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


class TestDefaultRegistry:
    def test_registry_keys(self):
        assert set(DEFAULT_MESSAGE_TRANSFORMERS) == {
            RecoveryAction.SHRINK_IMAGE,
            RecoveryAction.DOWNGRADE_TOOL_CONTENT,
            RecoveryAction.STRIP_REQUEST_STATE,
        }


class TestShrinkImage:
    @pytest.mark.asyncio
    async def test_empty_messages_returns_none(self):
        assert await shrink_image([], LLMImageTooLargeError("x")) is None

    @pytest.mark.asyncio
    async def test_no_image_parts_returns_none(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        assert await shrink_image(msgs, LLMImageTooLargeError("x")) is None

    @pytest.mark.asyncio
    async def test_shrinks_oversized_dict_image(self):
        pytest.importorskip("PIL.Image")
        url = _big_image_data_url()
        assert len(url) > _IMAGE_TARGET_BYTES
        msgs = [
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": url}}],
            }
        ]
        result = await shrink_image(msgs, LLMImageTooLargeError("x"))
        assert result is msgs
        new_url = msgs[0]["content"][0]["image_url"]["url"]
        assert len(new_url) <= _IMAGE_TARGET_BYTES

    @pytest.mark.asyncio
    async def test_unshrinkable_garbage_returns_none(self):
        # An oversized but undecodable data URL can't be shrunk → None (re-raise).
        garbage = "data:image/png;base64," + ("A" * (_IMAGE_TARGET_BYTES + 10))
        msgs = [
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": garbage}}],
            }
        ]
        assert await shrink_image(msgs, LLMImageTooLargeError("x")) is None


class TestDowngradeToolContent:
    @pytest.mark.asyncio
    async def test_empty_returns_none(self):
        assert await downgrade_tool_content([], LLMMultimodalToolContentError("x")) is None

    @pytest.mark.asyncio
    async def test_salvages_text_parts(self):
        msgs = [
            {
                "role": "tool",
                "tool_call_id": "1",
                "content": [
                    {"type": "text", "text": "part-a"},
                    {"type": "image_url", "image_url": {"url": "data:..."}},
                    {"type": "text", "text": "part-b"},
                ],
            }
        ]
        result = await downgrade_tool_content(msgs, LLMMultimodalToolContentError("x"))
        assert result is msgs
        assert msgs[0]["content"] == "part-a\n\npart-b"

    @pytest.mark.asyncio
    async def test_image_only_gets_placeholder(self):
        msgs = [
            {
                "role": "tool",
                "tool_call_id": "1",
                "content": [{"type": "image_url", "image_url": {"url": "data:..."}}],
            }
        ]
        result = await downgrade_tool_content(msgs, LLMMultimodalToolContentError("x"))
        assert result is msgs
        assert "image content removed" in msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_text_only_list_no_change(self):
        # No image part → stripping doesn't fix anything → None.
        msgs = [{"role": "tool", "tool_call_id": "1", "content": [{"type": "text", "text": "x"}]}]
        assert await downgrade_tool_content(msgs, LLMMultimodalToolContentError("x")) is None

    @pytest.mark.asyncio
    async def test_non_tool_role_skipped(self):
        msgs = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:"}}]}]
        assert await downgrade_tool_content(msgs, LLMMultimodalToolContentError("x")) is None


class TestStripRequestState:
    @pytest.mark.asyncio
    async def test_empty_returns_none(self):
        assert await strip_request_state([], LLMInvalidRequestStateError("x")) is None

    @pytest.mark.asyncio
    async def test_strips_non_canonical_keys(self):
        msgs = [{"role": "user", "content": "hi", "cache_control": {"type": "ephemeral"}}]
        result = await strip_request_state(msgs, LLMInvalidRequestStateError("x"))
        assert result is msgs
        assert "cache_control" not in msgs[0]
        assert msgs[0]["role"] == "user"

    @pytest.mark.asyncio
    async def test_strips_opaque_content_parts(self):
        msgs = [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "keep"},
                    {"type": "thinking", "text": "drop"},
                    {"type": "redacted_thinking", "data": "drop"},
                ],
            }
        ]
        result = await strip_request_state(msgs, LLMInvalidRequestStateError("x"))
        assert result is msgs
        types = [p["type"] for p in msgs[0]["content"]]
        assert types == ["text"]

    @pytest.mark.asyncio
    async def test_clean_messages_return_none(self):
        msgs = [{"role": "user", "content": "hi"}]
        assert await strip_request_state(msgs, LLMInvalidRequestStateError("x")) is None
