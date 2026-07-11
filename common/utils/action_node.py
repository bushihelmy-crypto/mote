#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2023/12/11 18:45
@Author  : alexanderwu
@File    : action_node.py

NOTE: You should use typing.List instead of list to do type annotation. Because in the markdown extraction process,
  we can use typing to extract the type of the node, but we cannot use built-in list to extract.
"""
from __future__ import annotations

import json
import typing
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Type, Union

from pydantic import BaseModel, Field, create_model, model_validator

from mote.common.const import MARKDOWN_TITLE_PREFIX
from mote.common.logs import logger

if TYPE_CHECKING:
    from mote.router import BaseLLM


TAG = "CONTENT"

LANGUAGE_CONSTRAINT = "Language: Please use the same language as Human INPUT."
FORMAT_CONSTRAINT = f"Format: output wrapped inside [{TAG}][/{TAG}] like format example, nothing else."

SIMPLE_TEMPLATE = """
## context
{context}

-----

## format example
{example}

## nodes: "<node>: <type>  # <instruction>"
{instruction}

## constraint
{constraint}

## action
Follow instructions of nodes, generate output and make sure it follows the format example.
"""


def dict_to_markdown(d, prefix=MARKDOWN_TITLE_PREFIX, kv_sep="\n", postfix="\n"):
    markdown_str = ""
    for key, value in d.items():
        markdown_str += f"{prefix}{key}{kv_sep}{value}{postfix}"
    return markdown_str


class ActionNode:
    """ActionNode is a tree of nodes."""

    schema: str  # raw/json/markdown, default: ""

    # Action Context
    context: str  # all the context, including all necessary info
    llm: BaseLLM  # LLM with aask interface
    children: dict[str, "ActionNode"]

    # Action Input
    key: str  # Product Requirement / File list / Code
    func: typing.Callable  # 与节点相关联的函数或LLM调用
    params: Dict[str, Type]  # 输入参数的字典，键为参数名，值为参数类型
    expected_type: Type  # such as str / int / float etc.
    # context: str  # everything in the history.
    instruction: str  # the instructions should be followed.
    example: Any  # example for In Context-Learning.

    # Action Output
    content: str
    instruct_content: BaseModel

    # For ActionGraph
    prevs: List["ActionNode"]  # previous nodes
    nexts: List["ActionNode"]  # next nodes

    def __init__(
        self,
        key: str,
        expected_type: Any,
        instruction: str,
        example: Any,
        content: str = "",
        children: Optional[dict[str, "ActionNode"]] = None,
        schema: str = "",
    ):
        self.key = key
        self.expected_type = expected_type
        self.instruction = instruction
        self.example = example
        self.content = content
        self.children = children if children is not None else {}
        self.schema = schema
        self.prevs = []
        self.nexts = []

    def __str__(self):
        return (
            f"{self.key}, {repr(self.expected_type)}, {self.instruction}, {self.example}"
            f", {self.content}, {self.children}"
        )

    def __repr__(self):
        return self.__str__()

    def add_prev(self, node: "ActionNode"):
        """增加前置ActionNode"""
        self.prevs.append(node)

    def add_next(self, node: "ActionNode"):
        """增加后置ActionNode"""
        self.nexts.append(node)

    def add_child(self, node: "ActionNode"):
        """增加子ActionNode"""
        self.children[node.key] = node

    def get_child(self, key: str) -> Union["ActionNode", None]:
        return self.children.get(key, None)

    def add_children(self, nodes: List["ActionNode"]):
        """批量增加子ActionNode"""
        for node in nodes:
            self.add_child(node)

    @classmethod
    def from_children(cls, key, nodes: List["ActionNode"]):
        """直接从一系列的子nodes初始化"""
        obj = cls(key, str, "", "")
        obj.add_children(nodes)
        return obj

    def _get_children_mapping(self, exclude=None) -> Dict[str, Any]:
        """获得子ActionNode的字典，以key索引，支持多级结构。"""
        exclude = exclude or []

        def _get_mapping(node: "ActionNode") -> Dict[str, Any]:
            mapping = {}
            for key, child in node.children.items():
                if key in exclude:
                    continue
                # 对于嵌套的子节点，递归调用 _get_mapping
                if child.children:
                    mapping[key] = _get_mapping(child)
                else:
                    mapping[key] = (child.expected_type, Field(default=child.example, description=child.instruction))
            return mapping

        return _get_mapping(self)

    def _get_self_mapping(self) -> Dict[str, Tuple[Type, Any]]:
        """get self key: type mapping"""
        return {self.key: (self.expected_type, ...)}

    def get_mapping(self, mode="children", exclude=None) -> Dict[str, Tuple[Type, Any]]:
        """get key: type mapping under mode"""
        if mode == "children" or (mode == "auto" and self.children):
            return self._get_children_mapping(exclude=exclude)
        return {} if exclude and self.key in exclude else self._get_self_mapping()

    @classmethod
    def create_model_class(cls, class_name: str, mapping: Dict[str, Tuple[Type, Any]]):
        """基于pydantic v2的模型动态生成，用来检验结果类型正确性"""

        def check_fields(cls, values):
            required_fields = set(mapping.keys())
            missing_fields = required_fields - set(values.keys())
            if missing_fields:
                raise ValueError(f"Missing fields: {missing_fields}")

            unrecognized_fields = set(values.keys()) - required_fields
            if unrecognized_fields:
                logger.warning(f"Unrecognized fields: {unrecognized_fields}")
            return values

        validators = {"check_missing_fields_validator": model_validator(mode="before")(check_fields)}

        new_fields = {}
        for field_name, field_value in mapping.items():
            if isinstance(field_value, dict):
                # 对于嵌套结构，递归创建模型类
                nested_class_name = f"{class_name}_{field_name}"
                nested_class = cls.create_model_class(nested_class_name, field_value)
                new_fields[field_name] = (nested_class, ...)
            else:
                new_fields[field_name] = field_value

        new_class = create_model(class_name, __validators__=validators, **new_fields)  # type: ignore[call-overload]  # dynamic field defs vs create_model reserved-kw stub
        return new_class

    def create_class(self, mode: str = "auto", class_name: Optional[str] = None, exclude=None):
        class_name = class_name if class_name else f"{self.key}_AN"
        mapping = self.get_mapping(mode=mode, exclude=exclude)
        return self.create_model_class(class_name, mapping)

    def _create_children_class(self, exclude=None):
        """使用object内有的字段直接生成model_class"""
        class_name = f"{self.key}_AN"
        mapping = self._get_children_mapping(exclude=exclude)
        return self.create_model_class(class_name, mapping)

    def to_dict(self, format_func=None, mode="auto", exclude=None) -> Dict:
        """将当前节点与子节点都按照node: format的格式组织成字典"""
        nodes = self._to_dict(format_func=format_func, mode=mode, exclude=exclude)
        if not isinstance(nodes, dict):
            nodes = {self.key: nodes}
        return nodes

    def _to_dict(self, format_func=None, mode="auto", exclude=None) -> Any:
        """将当前节点与子节点都按照node: format的格式组织成字典"""

        # 如果没有提供格式化函数，则使用默认的格式化函数
        def _default_format(node):
            return node.instruction

        fmt = format_func if format_func is not None else _default_format

        # 使用提供的格式化函数来格式化当前节点的值
        formatted_value = fmt(self)

        # 创建当前节点的键值对
        node_value: Any
        if (mode == "children" or mode == "auto") and self.children:
            node_value = {}
        else:
            node_value = formatted_value

        if mode == "root":
            return {self.key: node_value}

        # 递归处理子节点
        exclude = exclude or []
        for child_key, child_node in self.children.items():
            if child_key in exclude:
                continue
            # 递归调用 to_dict 方法并更新节点字典
            child_dict = child_node._to_dict(fmt, mode, exclude)
            node_value[child_key] = child_dict

        return node_value

    def update_instruct_content(self, incre_data: dict[str, Any]):
        assert self.instruct_content
        origin_sc_dict = self.instruct_content.model_dump()
        origin_sc_dict.update(incre_data)
        output_class = self.create_class()
        self.instruct_content = output_class(**origin_sc_dict)

    def keys(self, mode: str = "auto") -> list:
        if mode == "children" or (mode == "auto" and self.children):
            keys = []
        else:
            keys = [self.key]
        if mode == "root":
            return keys

        for _, child_node in self.children.items():
            keys.append(child_node.key)
        return keys

    def compile_to(self, i: Dict, schema, kv_sep) -> str:
        if schema == "json":
            return json.dumps(i, indent=4, ensure_ascii=False)
        elif schema == "markdown":
            return dict_to_markdown(i, kv_sep=kv_sep)
        else:
            return str(i)

    def tagging(self, text, schema, tag="") -> str:
        if not tag:
            return text
        return f"[{tag}]\n{text}\n[/{tag}]"

    def _compile_f(self, schema, mode, tag, format_func, kv_sep, exclude=None) -> str:
        nodes = self.to_dict(format_func=format_func, mode=mode, exclude=exclude)
        text = self.compile_to(nodes, schema, kv_sep)
        return self.tagging(text, schema, tag)

    def compile_instruction(self, schema="markdown", mode="children", tag="", exclude=None) -> str:
        """compile to raw/json/markdown template with all/root/children nodes"""

        def format_func(i):
            return f"{i.expected_type}  # {i.instruction}"

        return self._compile_f(schema, mode, tag, format_func, kv_sep=": ", exclude=exclude)

    def compile_example(self, schema="json", mode="children", tag="", exclude=None) -> str:
        """compile to raw/json/markdown examples with all/root/children nodes"""

        # 这里不能使用f-string，因为转译为str后再json.dumps会额外加上引号，无法作为有效的example
        # 错误示例："File list": "['main.py', 'const.py', 'game.py']", 注意这里值不是list，而是str
        def format_func(i):
            return i.example

        return self._compile_f(schema, mode, tag, format_func, kv_sep="\n", exclude=exclude)

    def compile(self, context, schema="json", mode="children", template=SIMPLE_TEMPLATE, exclude=[]) -> str:
        """
        mode: all/root/children
            mode="children": 编译所有子节点为一个统一模板，包括instruction与example
            mode="all": NotImplemented
            mode="root": NotImplemented
        schmea: raw/json/markdown
            schema="raw": 不编译，context, lang_constaint, instruction
            schema="json"：编译context, example(json), instruction(markdown), constraint, action
            schema="markdown": 编译context, example(markdown), instruction(markdown), constraint, action
        """
        if schema == "raw":
            return f"{context}\n\n## Actions\n{LANGUAGE_CONSTRAINT}\n{self.instruction}"

        # 直接使用 pydantic BaseModel 生成 instruction 与 example，仅限 JSON
        # child_class = self._create_children_class()
        # node_schema = child_class.model_json_schema()
        # defaults = {
        #     k: str(v)
        #     for k, v in child_class.model_fields.items()
        #     if k not in exclude
        # }
        # instruction = node_schema
        # example = json.dumps(defaults, indent=4)

        # FIXME: json instruction会带来格式问题，如："Project name": "web_2048  # 项目名称使用下划线",
        # compile example暂时不支持markdown
        instruction = self.compile_instruction(schema="markdown", mode=mode, exclude=exclude)
        example = self.compile_example(schema=schema, tag=TAG, mode=mode, exclude=exclude)
        # nodes = ", ".join(self.to_dict(mode=mode).keys())
        constraints = [LANGUAGE_CONSTRAINT, FORMAT_CONSTRAINT]
        constraint = "\n".join(constraints)

        prompt = template.format(
            context=context,
            example=example,
            instruction=instruction,
            constraint=constraint,
        )
        return prompt

    def get(self, key):
        return self.instruct_content.model_dump()[key]

    def set_recursive(self, name, value):
        setattr(self, name, value)
        for _, i in self.children.items():
            i.set_recursive(name, value)

    def set_llm(self, llm):
        self.set_recursive("llm", llm)

    def set_context(self, context):
        self.set_recursive("context", context)

    @classmethod
    def from_pydantic(cls, model: Type[BaseModel], key: Optional[str] = None):
        """
        Creates an ActionNode tree from a Pydantic model.

        Args:
            model (Type[BaseModel]): The Pydantic model to convert.

        Returns:
            ActionNode: The root node of the created ActionNode tree.
        """
        key = key or model.__name__
        root_node = cls(key=key, expected_type=Type[model], instruction="", example="")

        for field_name, field_info in model.model_fields.items():
            field_type = field_info.annotation
            description = field_info.description
            default = field_info.default

            # Recursively handle nested models if needed. A parametrized generic
            # (e.g. list[int]) has a non-None ``get_origin`` and is not a class,
            # so skip the issubclass probe for those (it would raise on non-types).
            if (
                typing.get_origin(field_type) is None
                and isinstance(field_type, type)
                and issubclass(field_type, BaseModel)
            ):
                child_node = cls.from_pydantic(field_type, key=field_name)
            else:
                child_node = cls(
                    key=field_name,
                    expected_type=field_type or Any,
                    instruction=description or "",
                    example=default,
                )

            root_node.add_child(child_node)

        return root_node
