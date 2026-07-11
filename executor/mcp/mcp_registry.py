#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2023/01/12 17:07
@Author  : garylin2099
@File    : tool_registry.py
"""
from __future__ import annotations

import contextlib
import functools
import inspect
import os
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Optional

from mcp.types import Tool as MCPTool
from mote.common.const import TOOL_SCHEMA_PATH
from mote.common.logs import logger
from mote.executor.mcp.tool_data_type import Tool, ToolSchema
from mote.executor.tool_convert import convert_code_to_tool_schema, convert_code_to_tool_schema_ast
from pydantic import BaseModel


class ToolRegistry(BaseModel):
    tools: dict = {}
    tools_by_tags: dict = defaultdict(dict)  # two-layer k-v, {tag: {tool_name: {...}, ...}, ...}
    instances: dict = {}  # tool_name -> singleton instance cache

    def register_tool(
        self,
        tool_name: str,
        tool_path: str,
        schemas: Optional[dict] = None,
        schema_path: str = "",
        tool_code: str = "",
        tags: Optional[list[str]] = None,
        tool_source_object=None,  # can be any classes or functions
        include_functions: Optional[list[str]] = None,
        verbose: bool = False,
    ):
        if self.has_tool(tool_name):
            return

        schema_path = schema_path or str(TOOL_SCHEMA_PATH / f"{tool_name}.yml")

        if not schemas:
            schemas = make_schema(tool_source_object, include_functions, schema_path)

        if not schemas:
            return

        schemas["tool_path"] = tool_path  # corresponding code file path of the tool
        try:
            ToolSchema(**schemas)  # validation
        except Exception:
            pass
            # logger.warning(
            #     f"{tool_name} schema not conforms to required format, but will be used anyway. Mismatch: {e}"
            # )
        tags = tags or []
        tool = Tool(name=tool_name, path=tool_path, schemas=schemas, code=tool_code, tags=tags)
        self.tools[tool_name] = tool
        for tag in tags:
            self.tools_by_tags[tag].update({tool_name: tool})

    def register_mcp_tool(self, tool: MCPTool):
        if self.has_tool(tool.name):
            return

        schema = {"description": tool.description, "parameters": tool.inputSchema}
        registered = Tool(name=tool.name, schemas=schema, path="")
        self.tools[tool.name] = registered

    def has_tool(self, key: str) -> bool:
        return key in self.tools

    def get_tool(self, key) -> Optional[Tool]:
        return self.tools.get(key)

    def get_tools_by_tag(self, key) -> dict[str, Tool]:
        return self.tools_by_tags.get(key, {})

    def get_all_tools(self) -> dict[str, Tool]:
        return self.tools

    def has_tool_tag(self, key) -> bool:
        return key in self.tools_by_tags

    def get_tool_tags(self) -> list[str]:
        return list(self.tools_by_tags.keys())

    def get_instance(self, tool_name: str) -> Any:
        """Get or create the singleton instance for a tool.

        All tools are singletons. Per-session state isolation is handled
        internally by the tool via session_id routing.

        Returns None if the tool cannot be auto-instantiated.
        """
        if tool_name in self.instances:
            return self.instances[tool_name]

        tool = self.tools.get(tool_name)
        if not tool or not tool.tool_class or not tool.instantiable:
            return None

        try:
            instance = tool.tool_class()
            self.instances[tool_name] = instance
            return instance
        except TypeError:
            return None

    def get_executors(self, tool_names: list[str], session_id: str = "default") -> dict[str, Callable]:
        """Auto-generate {ClassName.method -> callable} map from a list of tool names.

        Only session_aware tools get session_id injected via functools.partial.

        Args:
            tool_names: List of tool specs ("ToolName" or "ToolName:method1,method2")
            session_id: Identifier for the calling Role's session (used for state isolation)
        """
        executors: dict[str, Callable] = {}
        for name in tool_names:
            base_name = name.split(":")[0]
            tool = self.tools.get(base_name)
            if not tool or not tool.tool_class:
                continue  # Plan, Role, Agent etc. — not registry-backed tools

            instance = self.get_instance(base_name)
            if instance is None:
                continue

            # Determine which methods to expose
            if ":" in name:
                methods = name.split(":")[1].split(",")
            else:
                methods = list((tool.schemas.get("methods") or {}).keys())

            for method_name in methods:
                fn = getattr(instance, method_name, None)
                if fn and callable(fn):
                    if tool.session_aware:
                        executors[f"{base_name}.{method_name}"] = functools.partial(fn, session_id=session_id)
                    else:
                        executors[f"{base_name}.{method_name}"] = fn

            # Expand aliases
            for alias, target_method in tool.aliases.items():
                fn = getattr(instance, target_method, None)
                if fn and callable(fn):
                    if tool.session_aware:
                        executors[alias] = functools.partial(fn, session_id=session_id)
                    else:
                        executors[alias] = fn

        return executors

    def cleanup_session(self, session_id: str):
        """Clean up per-session state across all tool instances when a Role exits."""
        for tool_name, instance in self.instances.items():
            if hasattr(instance, "cleanup_session"):
                instance.cleanup_session(session_id)


# Registry instance
TOOL_REGISTRY = MCP_REGISTRY = ToolRegistry()


def register_tool(
    tags: Optional[list[str]] = None,
    schema_path: str = "",
    aliases: Optional[dict[str, str]] = None,
    session_aware: bool = False,
    instantiable: bool = True,
    **kwargs,
):
    """register a tool to registry

    Args:
        tags: Tool category tags for discovery/filtering.
        schema_path: Path to external schema file (optional).
        aliases: Shorthand names mapping to methods, e.g. {"Terminal": "run"}.
        session_aware: If True, methods accept session_id kwarg for state isolation.
        instantiable: If False, get_instance/get_executors will skip auto-instantiation.
                      Use for tools that require per-Role context (e.g. Agent, Plan).
    """

    def decorator(cls):
        # Get the file path where the function / class is defined and the source code
        file_path = inspect.getfile(cls)
        if "mote" in file_path:
            # split to handle ../mote/mote/tools/... where only metapgt/tools/... is needed
            file_path = "mote" + file_path.split("mote")[-1]
        source_code = ""
        with contextlib.suppress(OSError):
            source_code = inspect.getsource(cls)

        TOOL_REGISTRY.register_tool(
            tool_name=cls.__name__,
            tool_path=file_path,
            schema_path=schema_path,
            tool_code=source_code,
            tags=tags,
            tool_source_object=cls,
            **kwargs,
        )

        # Store class reference and flags on the registered Tool
        tool = TOOL_REGISTRY.get_tool(cls.__name__)
        if tool:
            tool.tool_class = cls
            tool.session_aware = session_aware
            tool.instantiable = instantiable
            if aliases:
                tool.aliases = aliases

        return cls

    return decorator


def make_schema(tool_source_object, include, path):
    try:
        schema = convert_code_to_tool_schema(tool_source_object, include=include)
    except Exception as e:
        logger.error(f"Fail to make schema: {e}")
        schema = {}

    return schema


def validate_tool_names(tools: list[str]) -> dict[str, Tool]:
    assert isinstance(tools, list), "tools must be a list of str"
    valid_tools = {}
    for key in tools:
        # one can define either tool names OR tool tags OR tool path, take union to get the whole set
        # if tool paths are provided, they will be registered on the fly
        if os.path.isdir(key) or os.path.isfile(key):
            valid_tools.update(register_tools_from_path(key))
        elif TOOL_REGISTRY.has_tool(key.split(":")[0]):
            if ":" in key:
                # handle class tools with methods specified, such as Editor:read,write
                class_tool_name = key.split(":")[0]
                method_names = key.split(":")[1].split(",")
                class_tool = TOOL_REGISTRY.get_tool(class_tool_name)
                if class_tool is None:
                    continue

                methods_filtered = {}
                for method_name in method_names:
                    if method_name in class_tool.schemas["methods"]:
                        methods_filtered[method_name] = class_tool.schemas["methods"][method_name]
                    else:
                        pass
                class_tool_filtered = class_tool.model_copy(deep=True)
                class_tool_filtered.schemas["methods"] = methods_filtered

                valid_tools.update({class_tool_name: class_tool_filtered})

            else:
                valid_tools.update({key: TOOL_REGISTRY.get_tool(key)})
        elif TOOL_REGISTRY.has_tool_tag(key):
            valid_tools.update(TOOL_REGISTRY.get_tools_by_tag(key))
        else:
            pass
    return valid_tools


def register_tools_from_file(file_path) -> dict[str, Tool]:
    file_name = Path(file_path).name
    if not file_name.endswith(".py") or file_name == "setup.py" or file_name.startswith("test"):
        return {}
    registered_tools = {}
    code = Path(file_path).read_text(encoding="utf-8")
    tool_schemas = convert_code_to_tool_schema_ast(code)
    for name, schemas in tool_schemas.items():
        tool_code = schemas.pop("code", "")
        TOOL_REGISTRY.register_tool(
            tool_name=name,
            tool_path=file_path,
            schemas=schemas,
            tool_code=tool_code,
        )
        registered_tools.update({name: TOOL_REGISTRY.get_tool(name)})
    return registered_tools


def register_tools_from_path(path) -> dict[str, Tool]:
    tools_registered = {}
    if os.path.isfile(path):
        tools_registered.update(register_tools_from_file(path))
    elif os.path.isdir(path):
        for root, _, files in os.walk(path):
            for file in files:
                file_path = os.path.join(root, file)
                tools_registered.update(register_tools_from_file(file_path))
    return tools_registered
