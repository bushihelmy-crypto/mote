"""Strict, shared encoding decisions and byte-boundary mapping."""

from __future__ import annotations

import codecs
from typing import Optional

import chardet

from mote.contracts.file.errors import EncodingRejectedError
from mote.contracts.file.identity import EditableTextSnapshot, EncodingDecision, EncodingSource, NewlineProfile

_MIN_DETECTION_CONFIDENCE = 0.90
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)


def _canonical(label: str) -> str:
    try:
        return codecs.lookup(label).name
    except LookupError as exc:
        raise EncodingRejectedError(f"unknown text encoding: {label}", encoding=label, cause=exc) from exc


def _strict_roundtrip(raw: bytes, label: str, bom: bytes = b"") -> str:
    payload = raw[len(bom) :]
    try:
        text = payload.decode(label, errors="strict")
        encoded = text.encode(label, errors="strict")
    except UnicodeError as exc:
        raise EncodingRejectedError(
            f"bytes are not valid {label} text",
            encoding=label,
            cause=exc,
        ) from exc
    if encoded != payload:
        raise EncodingRejectedError(
            f"{label} cannot round-trip the original bytes exactly",
            encoding=label,
        )
    return text


def decode_text(
    raw: bytes,
    *,
    explicit: Optional[str] = None,
    fallback: Optional[str] = None,
) -> tuple[str, EncodingDecision]:
    for bom, label in _BOMS:
        if raw.startswith(bom):
            text = _strict_roundtrip(raw, label, bom)
            return text, EncodingDecision(label=label, bom=bom, source=EncodingSource.BOM, confidence=1.0)

    if explicit:
        label = _canonical(explicit)
        text = _strict_roundtrip(raw, label)
        return text, EncodingDecision(label=label, bom=b"", source=EncodingSource.EXPLICIT, confidence=1.0)

    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        pass
    else:
        return text, EncodingDecision(label="utf-8", bom=b"", source=EncodingSource.UTF8, confidence=1.0)

    detected = chardet.detect(raw)
    detected_label = detected.get("encoding")
    confidence = float(detected.get("confidence") or 0.0)
    if detected_label and confidence >= _MIN_DETECTION_CONFIDENCE:
        label = _canonical(str(detected_label))
        try:
            text = _strict_roundtrip(raw, label)
        except EncodingRejectedError:
            pass
        else:
            return text, EncodingDecision(
                label=label,
                bom=b"",
                source=EncodingSource.DETECTED,
                confidence=confidence,
            )

    if fallback:
        label = _canonical(fallback)
        text = _strict_roundtrip(raw, label)
        return text, EncodingDecision(label=label, bom=b"", source=EncodingSource.FALLBACK)

    raise EncodingRejectedError(
        "text encoding could not be determined without loss; provide an explicit encoding or use hex view",
        detected_encoding=detected_label,
        confidence=confidence,
    )


def editable_text(raw: bytes, decision: EncodingDecision) -> EditableTextSnapshot:
    text = _strict_roundtrip(raw, decision.label, decision.bom)
    chunks: list[bytes] = []
    boundaries = [len(decision.bom)]
    cursor = len(decision.bom)
    try:
        for character in text:
            chunk = character.encode(decision.label, errors="strict")
            chunks.append(chunk)
            cursor += len(chunk)
            boundaries.append(cursor)
    except UnicodeError as exc:
        raise EncodingRejectedError(
            f"{decision.label} does not provide stable editable byte boundaries",
            encoding=decision.label,
            cause=exc,
        ) from exc
    if b"".join(chunks) != raw[len(decision.bom) :]:
        raise EncodingRejectedError(
            f"{decision.label} is stateful and cannot be edited with byte-preserving fragments",
            encoding=decision.label,
        )

    crlf = text.count("\r\n")
    profile = NewlineProfile(
        lf=text.count("\n") - crlf,
        crlf=crlf,
        cr=text.count("\r") - crlf,
    )
    return EditableTextSnapshot(
        text=text,
        logical_to_raw_boundaries=tuple(boundaries),
        encoding=decision,
        newline_profile=profile,
    )


__all__ = ["decode_text", "editable_text"]
