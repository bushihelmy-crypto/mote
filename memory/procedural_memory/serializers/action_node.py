"""ActionNode Serializer."""

from __future__ import annotations

from typing import Type

from mote.common.utils.action_node import ActionNode
from mote.memory.procedural_memory.serializers.simple import SimpleSerializer


class ActionNodeSerializer(SimpleSerializer):
    def serialize_resp(self, resp: ActionNode) -> str:
        return resp.instruct_content.model_dump_json()

    def deserialize_resp(self, resp: str) -> ActionNode:
        """Customized deserialization, it will be triggered when a perfect experience is found.

        ActionNode cannot be serialized, it throws an error 'cannot pickle 'SSLContext' object'.
        """

        class InstructContent:
            def __init__(self, json_data):
                self.json_data = json_data

            def model_dump_json(self):
                return self.json_data

        action_node = ActionNode(key="", expected_type=Type[str], instruction="", example="")
        # InstructContent is a minimal duck type exposing only model_dump_json (the
        # sole method the resp path calls); the field is declared BaseModel.
        action_node.instruct_content = InstructContent(resp)  # type: ignore[assignment]

        return action_node
