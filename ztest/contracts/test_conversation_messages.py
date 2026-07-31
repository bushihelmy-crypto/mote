from __future__ import annotations

import pytest
from pydantic import ValidationError

from mote.contracts.conversation import Message


@pytest.mark.parametrize(
    "instruct_content",
    [
        {"type": "conversation.document", "version": 1},
        {
            "type": "conversation.document",
            "version": 1,
            "value": {},
            "legacy": True,
        },
        {"type": "conversation.document", "version": True, "value": {}},
        {"type": "conversation.document", "version": 1, "value": []},
    ],
)
def test_message_rejects_noncanonical_instruct_content_envelope(
    instruct_content: object,
) -> None:
    with pytest.raises(ValidationError):
        Message(content="invalid", instruct_content=instruct_content)


def test_message_rejects_module_class_instruct_content_discriminator() -> None:
    with pytest.raises(ValidationError, match="discriminator"):
        Message(
            content="legacy",
            instruct_content={
                "type": "mote.contracts.conversation.document.Document",
                "version": 1,
                "value": {},
            },
        )
