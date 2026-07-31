#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Conversation document and causation contracts."""

from __future__ import annotations

import os.path
from enum import Enum
from typing import Dict, Iterable

from pydantic import BaseModel, Field


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
