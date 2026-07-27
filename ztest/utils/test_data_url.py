"""Tests for the shared data-URL codec in common.utils.common.

Covers the single authority for the ``data:<media_type>;base64,<data>`` wire
shape and the sniff-vs-declared media-type precedence, previously duplicated
(and drifted) across base_llm / anthropic_api / transformers.
"""

import base64

import pytest

from mote.runtime.models.media import build_data_url, parse_data_url, resolve_image_media_type

# Minimal byte payloads carrying the magic numbers sniff_image_media_type reads.
_PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16).decode()
_JPEG_B64 = base64.b64encode(b"\xff\xd8\xff" + b"\x00" * 16).decode()
# Payload with no recognized magic number -> sniff returns None.
_UNKNOWN_B64 = base64.b64encode(b"not-an-image-header").decode()


class TestResolveImageMediaType:
    def test_sniff_wins_over_declared(self):
        # Declared JPEG but the bytes are PNG -> sniffed PNG must win.
        assert resolve_image_media_type(_PNG_B64, "image/jpeg") == "image/png"

    def test_falls_back_to_declared_when_sniff_fails(self):
        assert resolve_image_media_type(_UNKNOWN_B64, "image/webp") == "image/webp"

    def test_final_fallback_is_jpeg(self):
        assert resolve_image_media_type(_UNKNOWN_B64, None) == "image/jpeg"
        assert resolve_image_media_type(_UNKNOWN_B64, "") == "image/jpeg"


class TestBuildDataUrl:
    def test_wraps_with_sniffed_type(self):
        assert build_data_url(_PNG_B64) == f"data:image/png;base64,{_PNG_B64}"

    def test_declared_used_when_sniff_fails(self):
        url = build_data_url(_UNKNOWN_B64, "image/gif")
        assert url == f"data:image/gif;base64,{_UNKNOWN_B64}"

    def test_roundtrips_through_parse(self):
        url = build_data_url(_JPEG_B64)
        media_type, data = parse_data_url(url)
        assert media_type == "image/jpeg"
        assert data == _JPEG_B64


class TestParseDataUrl:
    def test_splits_media_type_and_data(self):
        assert parse_data_url("data:image/png;base64,ABC") == ("image/png", "ABC")

    def test_strips_extra_params_after_media_type(self):
        # Only the media type (before the first ';') is returned.
        assert parse_data_url("data:image/png;charset=utf-8;base64,ABC") == ("image/png", "ABC")

    def test_bare_data_prefix_yields_empty_media_type(self):
        assert parse_data_url("data:,ABC") == ("", "ABC")

    @pytest.mark.parametrize(
        "bad",
        [
            "https://example.com/x.png",  # not a data URL
            "data:image/png;base64",  # no comma separator
            "",  # empty
        ],
    )
    def test_returns_none_for_malformed(self, bad):
        assert parse_data_url(bad) is None

    def test_returns_none_for_non_string(self):
        assert parse_data_url(None) is None
        assert parse_data_url(123) is None
