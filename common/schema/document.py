#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Document-related schema classes."""

from __future__ import annotations

import os.path
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Union

from pydantic import BaseModel, Field

from metagpt.common.schema.serialization import BaseSerialization
from metagpt.common.const import SERDESER_PATH
from metagpt.common.utils.common import aread, read_json_file, write_json_file
from metagpt.common.utils.exceptions import handle_exception
from metagpt.common.utils.serialize import (
    actionoutput_mapping_to_str,
    actionoutput_str_to_mapping,
    actionoutout_schema_to_mapping,
)


class CauseBy(str, Enum):
    """Message causation tags — replaces heavyweight Action subclasses used purely as markers."""
    USER_REQUIREMENT = "UserRequirement"
    RUN_COMMAND = "RunCommand"
    ACTION = "Action"


class ActionOutput:
    content: str
    instruct_content: BaseModel

    def __init__(self, content: str, instruct_content: BaseModel):
        self.content = content
        self.instruct_content = instruct_content


class SerializationMixin(BaseSerialization):
    @handle_exception
    def serialize(self, file_path: str = None) -> str:
        """Serializes the current instance to a JSON file."""
        file_path = file_path or self.get_serialization_path()
        serialized_data = self.model_dump()
        write_json_file(file_path, serialized_data, use_fallback=True)
        return file_path

    @classmethod
    @handle_exception
    def deserialize(cls, file_path: str = None) -> BaseModel:
        """Deserializes a JSON file to an instance of cls."""
        file_path = file_path or cls.get_serialization_path()
        data: dict = read_json_file(file_path)
        model = cls(**data)
        return model

    @classmethod
    def get_serialization_path(cls) -> str:
        """Get the serialization path for the class."""
        return str(SERDESER_PATH / f"{cls.__qualname__}.json")


class Document(BaseModel):
    """Represents a document."""

    root_path: str = ""
    filename: str = ""
    content: str = ""

    def get_meta(self) -> Document:
        """Get metadata of the document."""
        return Document(root_path=self.root_path, filename=self.filename)

    @property
    def root_relative_path(self):
        """Get relative path from root of git repository."""
        return os.path.join(self.root_path, self.filename)

    def __str__(self):
        return self.content

    def __repr__(self):
        return self.content

    @classmethod
    async def load(
        cls, filename: Union[str, Path], project_path: Optional[Union[str, Path]] = None
    ) -> Optional["Document"]:
        """Load a document from a file."""
        if not filename or not Path(filename).exists():
            return None
        content = await aread(filename=filename)
        doc = cls(content=content, filename=str(filename))
        if project_path and Path(filename).is_relative_to(project_path):
            doc.root_path = Path(filename).relative_to(project_path).parent
            doc.filename = Path(filename).name
        return doc


class Documents(BaseModel):
    """A class representing a collection of documents."""

    docs: Dict[str, Document] = Field(default_factory=dict)

    @classmethod
    def from_iterable(cls, documents: Iterable[Document]) -> Documents:
        """Create a Documents instance from a list of Document instances."""
        docs = {doc.filename: doc for doc in documents}
        return Documents(docs=docs)

    def to_action_output(self) -> "ActionOutput":
        """Convert to action output string."""
        return ActionOutput(content=self.model_dump_json(), instruct_content=self)


class Resource(BaseModel):
    """Used by `Message`.`parse_resources`"""

    resource_type: str
    value: str
    description: str
