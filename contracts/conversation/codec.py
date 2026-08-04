"""Strict canonical codec for durable conversation messages."""

from __future__ import annotations

import json
from json import JSONDecodeError

from mote.contracts.conversation.messages import Message
from mote.contracts.events.envelope import freeze_json, thaw_json


def encode_message(message: Message) -> dict[str, object]:
    payload = message.model_dump(mode="json", exclude_none=True, warnings=False)
    if type(payload) is not dict:
        raise TypeError("message encoder did not produce an object")
    return payload


def decode_message(data: object) -> Message:
    if type(data) is not dict:
        raise ValueError("message payload must be an object")
    required = {"id", "timestamp", "content", "role", "cause_by", "sent_from", "send_to", "metadata"}
    allowed = required | {"instruct_content"}
    if not required <= set(data) or not set(data) <= allowed:
        raise ValueError("message payload fields are not canonical")
    for field in ("id", "timestamp", "content", "role", "cause_by", "sent_from"):
        if type(data[field]) is not str:
            raise ValueError(f"message {field} must be a string")
    if not data["id"]:
        raise ValueError("message id must not be empty")
    send_to = data["send_to"]
    if (
        type(send_to) is not list
        or not send_to
        or any(type(item) is not str or not item for item in send_to)
        or len(send_to) != len(set(send_to))
    ):
        raise ValueError("message send_to must contain unique non-empty strings")
    metadata = data["metadata"]
    if type(metadata) is not dict or any(type(key) is not str for key in metadata):
        raise ValueError("message metadata must be an object with string keys")
    frozen_metadata = freeze_json(metadata, path="message.metadata")
    instruct_content = data.get("instruct_content")
    if instruct_content is not None:
        if type(instruct_content) is not dict:
            raise ValueError("message instruct_content must be an object")
        freeze_json(instruct_content, path="message.instruct_content")
    return Message(
        id=data["id"],
        timestamp=data["timestamp"],
        content=data["content"],
        instruct_content=instruct_content,
        role=data["role"],
        cause_by=data["cause_by"],
        sent_from=data["sent_from"],
        send_to=set(send_to),
        metadata=thaw_json(frozen_metadata),
    )


def dump_message(message: Message) -> str:
    return json.dumps(encode_message(message), ensure_ascii=False, separators=(",", ":"))


def load_message(value: str) -> Message:
    if type(value) is not str or not value:
        raise ValueError("message payload must be a non-empty JSON string")
    try:
        payload = json.loads(value)
    except JSONDecodeError as exc:
        raise ValueError("message payload is not valid JSON") from exc
    return decode_message(payload)


__all__ = ["decode_message", "dump_message", "encode_message", "load_message"]
