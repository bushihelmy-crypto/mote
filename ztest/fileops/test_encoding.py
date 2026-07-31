from __future__ import annotations

import codecs

import pytest

from mote.contracts.file import EncodingRejectedError, EncodingSource
from mote.runtime.fileops.encoding import decode_text, editable_text


def test_bom_wins_and_boundaries_include_it():
    raw = codecs.BOM_UTF16_LE + "甲\r\n乙".encode("utf-16-le")

    text, decision = decode_text(raw)
    editable = editable_text(raw, decision)

    assert text == "甲\r\n乙"
    assert decision.source == EncodingSource.BOM
    assert decision.label == "utf-16-le"
    assert editable.logical_to_raw_boundaries[0] == len(codecs.BOM_UTF16_LE)
    assert editable.logical_to_raw_boundaries[-1] == len(raw)
    assert editable.newline_profile.crlf == 1


def test_explicit_gbk_roundtrips_and_maps_every_character():
    raw = "中文abc".encode("gbk")

    text, decision = decode_text(raw, explicit="gbk")
    editable = editable_text(raw, decision)

    assert text == "中文abc"
    assert decision.source == EncodingSource.EXPLICIT
    assert editable.logical_to_raw_boundaries == (0, 2, 4, 5, 6, 7)


def test_unknown_or_lossy_encoding_is_rejected():
    with pytest.raises(EncodingRejectedError):
        decode_text(b"\xff\x00\x81", explicit="utf-8")


def test_stateful_encoding_is_not_editable_by_fragment():
    raw = "日本語".encode("iso2022_jp")
    _, decision = decode_text(raw, explicit="iso2022_jp")

    with pytest.raises(EncodingRejectedError, match="stateful"):
        editable_text(raw, decision)
