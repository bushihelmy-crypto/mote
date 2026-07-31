#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Canonical request-repair primitives used by the ModelGateway.

These are the concrete handlers for the "repair the outgoing request, then make
another budgeted attempt" family (ported conceptually from hermes-agent's
``conversation_compression.try_shrink_image_parts_in_messages`` /
``run_agent._try_strip_image_parts_from_tool_messages``). Each is keyed by the
:class:`RecoveryAction` it serves and matches the request-transform seam::

    async (messages: list[dict], exc: MoteError) -> Optional[list[dict]]

Return repaired canonical wire messages or ``None`` when the transform cannot
make progress. ``CanonicalRequestTransformer`` owns conversion back to the
immutable invocation; ``AttemptOrchestrator`` alone decides whether to continue.

Pillow is a soft dependency: if it is missing, ``shrink_image`` returns ``None``
and the Gateway policy can switch endpoint or terminate without hidden retries.

Canonical wire-format reminders:

- image part: ``{"type": "image_url", "image_url": {"url": "data:image/...;base64,..."}}``
- text part: ``{"type": "text", "text": "..."}``
- tool result: ``{"role": "tool", "tool_call_id": ..., "content": ...}``
- assistant tool call: ``{"role": "assistant", "content": ..., "tool_calls": [...]}``
"""
from __future__ import annotations

import base64
import io
from typing import Any, Optional

try:
    from PIL import Image
except Exception as _pillow_import_error:  # Pillow is optional
    Image = None
else:
    _pillow_import_error = None

from mote.runtime.errors import RecoveryAction
from mote.runtime.models.media import parse_data_url
from mote.runtime.telemetry.logging import logger

# 4 MB target leaves comfortable headroom under Anthropic's hard 5 MB per-image
# ceiling once the data-URL header + JSON escaping overhead is accounted for.
_IMAGE_TARGET_BYTES = 4 * 1024 * 1024

# Content-part types carrying opaque provider state that a different backend may
# reject when replayed (Anthropic thinking blocks, reasoning traces).
_OPAQUE_PART_TYPES = frozenset({"thinking", "redacted_thinking", "reasoning"})

# The only message-level keys an OpenAI-compatible provider needs. Anything else
# (cache_control, provider-specific signatures, replay blobs) is request state we
# strip on a STRIP_REQUEST_STATE recovery.
_CANONICAL_MESSAGE_KEYS = frozenset({"role", "content", "name", "tool_calls", "tool_call_id"})


# ── SHRINK_IMAGE ─────────────────────────────────────────────────────────────


def _shrink_data_url(url: str, *, target_bytes: int = _IMAGE_TARGET_BYTES) -> Optional[str]:
    """Re-encode a ``data:image/...;base64,...`` URL under ``target_bytes``.

    Returns a smaller data URL, or ``None`` when the URL isn't an oversized data
    image, Pillow is unavailable, or shrinking can't bring it under the target.
    """
    if not isinstance(url, str) or len(url) <= target_bytes:
        return None  # not a string, or this image isn't the oversized one

    parsed = parse_data_url(url)
    if parsed is None:
        return None  # not a data URL
    declared, data = parsed

    if Image is None:
        logger.warning(f"shrink_image: Pillow unavailable — {_pillow_import_error}")
        return None

    # Only an ``image/*`` declaration drives PIL's format choice; anything else
    # (or a bare ``data:``) falls back to JPEG.
    mime = declared if declared.startswith("image/") else "image/jpeg"
    try:
        raw = base64.b64decode(data)
        img = Image.open(io.BytesIO(raw))
    except Exception as exc:
        logger.warning(f"shrink_image: cannot decode image — {exc}")
        return None

    pil_format = "PNG" if mime == "image/png" else "JPEG"
    out_mime = "image/png" if pil_format == "PNG" else "image/jpeg"
    if pil_format == "JPEG" and img.mode in {"RGBA", "P"}:
        img = img.convert("RGB")

    # Halve dimensions (and step JPEG quality down) until the data URL fits, up
    # to 5 rounds. PNG ignores quality so only dimension reduction helps it.
    quality_steps = (85, 70, 50) if pil_format == "JPEG" else (None,)
    prev_dims = (img.width, img.height)
    try:
        for attempt in range(5):
            if attempt > 0:
                new_w = max(int(img.width * 0.5), 64)
                new_h = max(int(img.height * 0.5), 64)
                if (new_w, new_h) == prev_dims:
                    break
                img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                prev_dims = (new_w, new_h)
            for quality in quality_steps:
                buf = io.BytesIO()
                save_kwargs: dict[str, Any] = {"format": pil_format}
                if quality is not None:
                    save_kwargs["quality"] = quality
                img.save(buf, **save_kwargs)
                encoded = base64.b64encode(buf.getvalue()).decode("ascii")
                candidate = f"data:{out_mime};base64,{encoded}"
                if len(candidate) <= target_bytes:
                    return candidate
    except Exception as exc:
        logger.warning(f"shrink_image: re-encode failed — {exc}")
        return None
    return None  # couldn't get under the target


async def shrink_image(
    messages: list[dict],
    exc: BaseException,
) -> Optional[list[dict]]:
    """Re-encode oversized native image parts smaller (recovery for SHRINK_IMAGE).

    Mutates ``messages`` in place. Returns ``messages`` only when every
    over-target image was actually brought under the target — if any oversized
    image can't be shrunk, retrying would re-send the rejected payload, so we
    return ``None`` and let the caller surface the original error.
    """
    if not messages:
        return None

    shrunk = 0
    unshrinkable = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") not in {
                "image_url",
                "input_image",
            }:
                continue
            image_value = part.get("image_url")
            if isinstance(image_value, dict):  # chat.completions: {"url": "data:..."}
                url = image_value.get("url", "")
                resized = _shrink_data_url(url)
                if resized:
                    image_value["url"] = resized
                    shrunk += 1
                elif isinstance(url, str) and url.startswith("data:") and len(url) > _IMAGE_TARGET_BYTES:
                    unshrinkable += 1
            elif isinstance(image_value, str):  # Responses-style: {"image_url": "data:..."}
                resized = _shrink_data_url(image_value)
                if resized:
                    part["image_url"] = resized
                    shrunk += 1
                elif image_value.startswith("data:") and len(image_value) > _IMAGE_TARGET_BYTES:
                    unshrinkable += 1

    if unshrinkable:
        return None
    if shrunk:
        return messages
    return None


# ── DOWNGRADE_TOOL_CONTENT ───────────────────────────────────────────────────


async def downgrade_tool_content(
    messages: list[dict],
    exc: BaseException,
) -> Optional[list[dict]]:
    """Downgrade list-type tool messages to plain text (recovery for DOWNGRADE_TOOL_CONTENT).

    Some OpenAI-compatible providers require a tool message's ``content`` to be a
    string and reject list-type (text + image) content with a 400. Walk every
    ``role: "tool"`` message whose content is a list containing image parts,
    salvage the text parts (or a placeholder), and replace the content. Returns
    ``messages`` when at least one tool message was downgraded, else ``None``.
    """
    if not messages:
        return None

    changed = False
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue

        text_parts: list[str] = []
        had_image = False
        for part in content:
            if not isinstance(part, dict):
                if isinstance(part, str) and part.strip():
                    text_parts.append(part.strip())
                continue
            ptype = part.get("type")
            if ptype in {"image_url", "input_image"}:
                had_image = True
            elif ptype in {"text", "input_text"}:
                text = str(part.get("text") or "").strip()
                if text:
                    text_parts.append(text)

        if not had_image:
            # Text-only list — stripping it doesn't resolve the rejection.
            continue
        msg["content"] = (
            "\n\n".join(text_parts)
            if text_parts
            else "[image content removed — provider does not accept list-type tool message content]"
        )
        changed = True

    if changed:
        return messages
    return None


# ── STRIP_REQUEST_STATE ──────────────────────────────────────────────────────


async def strip_request_state(
    messages: list[dict],
    exc: BaseException,
) -> Optional[list[dict]]:
    """Strip opaque request state a provider can't replay (recovery for STRIP_REQUEST_STATE).

    Removes provider-specific artifacts that trigger "invalid signature" /
    "encrypted content" rejections: opaque content parts (thinking / reasoning
    blocks) and any non-canonical message-level keys (cache_control, replay
    blobs, signatures). Returns ``messages`` when something was stripped, else
    ``None``.
    """
    if not messages:
        return None

    changed = False
    for msg in messages:
        if not isinstance(msg, dict):
            continue

        # Drop non-canonical message-level keys (signatures, cache_control, ...).
        for key in [k for k in msg if k not in _CANONICAL_MESSAGE_KEYS]:
            del msg[key]
            changed = True

        # Drop opaque content parts (thinking / reasoning) from list content.
        content = msg.get("content")
        if isinstance(content, list):
            kept = [part for part in content if not (isinstance(part, dict) and part.get("type") in _OPAQUE_PART_TYPES)]
            if len(kept) != len(content):
                msg["content"] = kept
                changed = True

    if changed:
        return messages
    return None


# Stable mapping retained for callers that need to inspect the supported repair
# family; Runtime's CanonicalRequestTransformer invokes the same functions.
DEFAULT_MESSAGE_TRANSFORMERS = {
    RecoveryAction.SHRINK_IMAGE: shrink_image,
    RecoveryAction.DOWNGRADE_TOOL_CONTENT: downgrade_tool_content,
    RecoveryAction.STRIP_REQUEST_STATE: strip_request_state,
}

__all__ = [
    "shrink_image",
    "downgrade_tool_content",
    "strip_request_state",
    "DEFAULT_MESSAGE_TRANSFORMERS",
]
