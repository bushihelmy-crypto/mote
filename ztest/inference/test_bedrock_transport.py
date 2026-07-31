import base64
import json
import struct
import zlib
from datetime import datetime, timezone

import pytest

from mote.product.models.transports.bedrock import (
    AwsCredentials,
    AwsEventStreamDecoder,
    _bedrock_url,
    _decode_bedrock_event,
    _sign_sigv4,
)
from mote.product.models.transports.openai import ProviderProtocolError


def _string_header(name: str, value: str) -> bytes:
    encoded_name = name.encode()
    encoded_value = value.encode()
    return (
        bytes([len(encoded_name)]) + encoded_name + bytes([7]) + struct.pack(">H", len(encoded_value)) + encoded_value
    )


def _frame(headers: bytes, payload: bytes) -> bytes:
    total = 16 + len(headers) + len(payload)
    prelude = struct.pack(">II", total, len(headers))
    with_prelude_crc = prelude + struct.pack(">I", zlib.crc32(prelude) & 0xFFFFFFFF)
    message = with_prelude_crc + headers + payload
    return message + struct.pack(">I", zlib.crc32(message) & 0xFFFFFFFF)


def test_sigv4_is_deterministic_and_signs_exact_body():
    headers = _sign_sigv4(
        method="POST",
        url="https://bedrock-runtime.us-east-1.amazonaws.com/model/anthropic.claude/invoke",
        headers={"content-type": "application/json", "accept": "application/json"},
        body=b'{"x":1}',
        credentials=AwsCredentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"),
        region="us-east-1",
        service="bedrock",
        now=datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
    )
    assert headers["authorization"].endswith(
        "Signature=c4a81842bbfb8fd4159c8606333def27fb51ca11ff35058cb9697d57f5410108"
    )
    assert headers["x-amz-content-sha256"] == ("5041bf1f713df204784353e82f6a4a535931cb64f1f4b4a5a" "eaffcb720918b22")


def test_eventstream_decoder_checks_crc_and_decodes_bedrock_chunk():
    inner = {"type": "message_stop"}
    envelope = json.dumps({"bytes": base64.b64encode(json.dumps(inner).encode()).decode()}).encode()
    headers = _string_header(":message-type", "event") + _string_header(":event-type", "chunk")
    frame = _frame(headers, envelope)
    decoder = AwsEventStreamDecoder(max_frame_bytes=4096)
    assert decoder.feed(frame[:7]) == ()
    messages = decoder.feed(frame[7:])
    decoder.finish()
    assert _decode_bedrock_event(messages[0]) == inner

    corrupted = bytearray(frame)
    corrupted[-1] ^= 1
    with pytest.raises(ProviderProtocolError, match="message CRC"):
        AwsEventStreamDecoder(max_frame_bytes=4096).feed(bytes(corrupted))


def test_bedrock_url_encodes_model_and_selects_stream_operation():
    url = _bedrock_url(
        "https://bedrock-runtime.us-east-1.amazonaws.com",
        "vendor/model",
        stream=True,
    )
    assert url.endswith("/model/vendor%2Fmodel/invoke-with-response-stream")
