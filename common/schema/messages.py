#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Message classes for the LLM conversation pipeline."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from json import JSONDecodeError
from typing import Any, Dict, List, Optional, Union

from pydantic import (
    BaseModel,
    Field,
    SerializeAsAny,
    create_model,
    field_serializer,
    field_validator,
)

from metagpt.common.schema.document import CauseBy, Resource
from metagpt.common.const import (
    AGENT,
    MESSAGE_ROUTE_CAUSE_BY,
    MESSAGE_ROUTE_FROM,
    MESSAGE_ROUTE_TO,
    MESSAGE_ROUTE_TO_ALL,
    TOOL_CALL_ID,
    TOOL_CALLS,
)
from metagpt.common.logs import logger
from metagpt.common.utils.common import (
    CodeParser,
    any_to_str,
    any_to_str_set,
    import_class,
)
from metagpt.common.utils.exceptions import handle_exception
from metagpt.common.utils.serialize import (
    actionoutout_schema_to_mapping,
    actionoutput_mapping_to_str,
    actionoutput_str_to_mapping,
)


class Message(BaseModel):
    """list[<role>: <content>]"""

    id: str = Field(default="", validate_default=True)
    timestamp: str = Field(default_factory=lambda: datetime.now().strftime("%Y%m%d%H%M%S%f")[:-3])
    content: str
    instruct_content: Optional[BaseModel] = Field(default=None, validate_default=True)
    role: str = "user"
    cause_by: str = Field(default="", validate_default=True)
    sent_from: str = Field(default="", validate_default=True)
    send_to: set[str] = Field(default={MESSAGE_ROUTE_TO_ALL}, validate_default=True)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("id", mode="before")
    @classmethod
    def check_id(cls, id: str) -> str:
        return id if id else uuid.uuid4().hex

    @field_validator("instruct_content", mode="before")
    @classmethod
    def check_instruct_content(cls, ic: Any) -> BaseModel:
        if ic and isinstance(ic, dict) and "class" in ic:
            if "mapping" in ic:
                mapping = actionoutput_str_to_mapping(ic["mapping"])
                actionnode_class = import_class("ActionNode", "metagpt.common.utils.action_node")
                ic_obj = actionnode_class.create_model_class(class_name=ic["class"], mapping=mapping)
            elif "module" in ic:
                ic_obj = import_class(ic["class"], ic["module"])
            else:
                raise KeyError("missing required key to init Message.instruct_content from dict")
            ic = ic_obj(**ic["value"])
        return ic

    @field_validator("cause_by", mode="before")
    @classmethod
    def check_cause_by(cls, cause_by: Any) -> str:
        if not cause_by:
            return CauseBy.USER_REQUIREMENT.value
        return any_to_str(cause_by)

    @field_validator("sent_from", mode="before")
    @classmethod
    def check_sent_from(cls, sent_from: Any) -> str:
        return any_to_str(sent_from if sent_from else "")

    @field_validator("send_to", mode="before")
    @classmethod
    def check_send_to(cls, send_to: Any) -> set:
        return any_to_str_set(send_to if send_to else {MESSAGE_ROUTE_TO_ALL})

    @field_serializer("send_to", mode="plain")
    def ser_send_to(self, send_to: set) -> list:
        return list(send_to)

    @field_serializer("instruct_content", mode="plain")
    def ser_instruct_content(self, ic: BaseModel) -> Union[dict, None]:
        ic_dict = None
        if ic:
            schema = ic.model_json_schema()
            ic_type = str(type(ic))
            if "<class 'metagpt.common.utils.action_node" in ic_type:
                mapping = actionoutout_schema_to_mapping(schema)
                mapping = actionoutput_mapping_to_str(mapping)
                ic_dict = {"class": schema["title"], "mapping": mapping, "value": ic.model_dump()}
            else:
                ic_dict = {"class": schema["title"], "module": ic.__module__, "value": ic.model_dump()}
        return ic_dict

    def __init__(self, content: str = "", **data: Any):
        data["content"] = data.get("content", content)
        super().__init__(**data)

    def __setattr__(self, key, val):
        """Override `@property.setter`, convert non-string parameters into string parameters."""
        if key == MESSAGE_ROUTE_CAUSE_BY:
            new_val = any_to_str(val)
        elif key == MESSAGE_ROUTE_FROM:
            new_val = any_to_str(val)
        elif key == MESSAGE_ROUTE_TO:
            new_val = any_to_str_set(val)
        else:
            new_val = val
        super().__setattr__(key, new_val)

    def __str__(self):
        if self.instruct_content:
            return f"{self.role}: {self.instruct_content.model_dump()}"
        return f"{self.role}: {self.content}"

    def __repr__(self):
        return self.__str__()

    def rag_key(self) -> str:
        """For search"""
        return self.content

    def to_dict(self) -> dict:
        """Return a dict for the LLM call."""
        if self.metadata.get(TOOL_CALL_ID):
            return {
                "role": "tool",
                "tool_call_id": self.metadata[TOOL_CALL_ID],
                "content": self.content,
            }
        if self.metadata.get(TOOL_CALLS):
            tool_calls = [
                {
                    "id": c["id"],
                    "type": "function",
                    "function": {
                        "name": c["name"],
                        "arguments": c["args"] if isinstance(c["args"], str) else json.dumps(c.get("args") or {}),
                    },
                }
                for c in self.metadata[TOOL_CALLS]
            ]
            return {"role": self.role, "content": self.content or "", "tool_calls": tool_calls}
        return {"role": self.role, "content": self.content}

    def dump(self) -> str:
        """Convert the object to json string"""
        return self.model_dump_json(exclude_none=True, warnings=False)

    @staticmethod
    @handle_exception(exception_type=JSONDecodeError, default_return=None)
    def load(val):
        """Convert the json string to object."""
        try:
            m = json.loads(val)
            id = m.get("id")
            if "id" in m:
                del m["id"]
            msg = Message(**m)
            if id:
                msg.id = id
            return msg
        except JSONDecodeError as err:
            logger.error(f"parse json failed: {val}, error:{err}")
        return None

    async def parse_resources(self, llm: "BaseLLM", key_descriptions: Dict[str, str] = None) -> Dict:
        """Parse resources from message content using LLM."""
        if not self.content:
            return {}
        content = f"## Original Requirement\n```text\n{self.content}\n```\n"
        return_format = (
            "Return a markdown JSON object with:\n"
            '- a "resources" key contain a list of objects. Each object with:\n'
            '  - a "resource_type" key explain the type of resource;\n'
            '  - a "value" key containing a string type of resource content;\n'
            '  - a "description" key explaining why;\n'
        )
        key_descriptions = key_descriptions or {}
        for k, v in key_descriptions.items():
            return_format += f'- a "{k}" key containing {v};\n'
        return_format += '- a "reason" key explaining why;\n'
        instructions = ['Lists all the resources contained in the "Original Requirement".', return_format]
        rsp = await llm.aask(msg=content, system_msgs=instructions)
        json_data = CodeParser.parse_code(text=rsp, lang="json")
        m = json.loads(json_data)
        m["resources"] = [Resource(**i) for i in m.get("resources", [])]
        return m

    def add_metadata(self, key: str, value: str):
        self.metadata[key] = value

    @staticmethod
    def create_instruct_value(kvs: Dict[str, Any], class_name: str = "") -> BaseModel:
        """Dynamically creates a Pydantic BaseModel subclass based on a given dictionary."""
        if not class_name:
            class_name = "DM" + uuid.uuid4().hex[0:8]
        dynamic_class = create_model(class_name, **{key: (value.__class__, ...) for key, value in kvs.items()})
        return dynamic_class.model_validate(kvs)

    def is_user_message(self) -> bool:
        return self.role == "user"

    def is_ai_message(self) -> bool:
        return self.role == "assistant"


class UserMessage(Message):
    """Facilitate support for OpenAI messages"""

    def __init__(self, content: str, **kwargs):
        kwargs.pop("role", None)
        super().__init__(content=content, role="user", **kwargs)


class SystemMessage(Message):
    """Facilitate support for OpenAI messages"""

    def __init__(self, content: str, **kwargs):
        kwargs.pop("role", None)
        super().__init__(content=content, role="system", **kwargs)


class AIMessage(Message):
    """Facilitate support for OpenAI messages"""

    def __init__(self, content: str, **kwargs):
        kwargs.pop("role", None)
        super().__init__(content=content, role="assistant", **kwargs)

    def with_agent(self, name: str):
        self.add_metadata(key=AGENT, value=name)
        return self

    @property
    def agent(self) -> str:
        return self.metadata.get(AGENT, "")


class LLMCallContext(BaseModel):
    """The message sequence fed to the LLM on the last think round."""

    messages: list[SerializeAsAny[Message]] = Field(default_factory=list)
