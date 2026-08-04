#!/usr/bin/env python
# -*- coding: utf-8 -*-

import asyncio
import re
from ast import literal_eval
from enum import Enum
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union

from pydantic import BaseModel, Field, field_validator

from mote.contracts.events.envelope import JsonValue


class LexerState(int, Enum):
    SEARCH_FUNCTION = 1
    PARSE_ARG_NAME = 2
    PARSE_ARG_VALUE = 3


class ArgObject(BaseModel):
    variable_name: str = Field(description="Parameter Name")
    variable_value: Optional[Union[str, int, float]] = Field(default=None, description="Parameter Value")
    variable_type: str = Field(default="str", description="Parameter Type")

    @field_validator("variable_name")
    @classmethod
    def _strip_props(cls, v: str) -> str:
        return v.split(" ", maxsplit=1)[0]

    @property
    def end_variable_name(self) -> str:
        return f"</{self.variable_name}>"


class FunctionObject(BaseModel):
    function_name: str = Field(description="function name")
    args: List[ArgObject] = Field(default_factory=list, description="Parameter List")

    @field_validator("function_name")
    @classmethod
    def _strip_props(cls, v: str) -> str:
        return v.split(" ", maxsplit=1)[0]

    @property
    def end_function_name(self) -> str:
        return f"</{self.function_name}>"


class Command(BaseModel):
    command_name: str
    args: dict[str, JsonValue]

    @field_validator("args")
    @classmethod
    def _fill_none(cls, args: dict) -> dict:
        kvs = {}
        for k, v in args.items():
            if v == "None":
                kvs[k] = None
            else:
                kvs[k] = v
        return kvs


EOS = (-1, None)


class PythonObjectParser(BaseModel):
    tokens: list = Field(default_factory=list, description="Python objects for debugging")
    functions: List[FunctionObject] = Field(
        default_factory=list, description="The parameter list of the current function"
    )
    rcv_buf: str = Field(default="", description="Receiving buffer")
    is_rcv_buf_ready: bool = Field(default=False, description="Is the receive buffer readable?")
    state: LexerState = Field(default=LexerState.SEARCH_FUNCTION, description="The state of the lexer")
    element_regx: str = Field(default=r"<([^<>]+)>", description="Regular expression for XML element names")
    ignore_text: bool = Field(default=False, description="Ignore the text outside the command.")
    types: dict = {
        str: "string",
        int: "number",
        float: "number",
        bool: "boolean",
        dict: "map",
        list: "array",
        Optional[str]: "string",
        Optional[int]: "number",
        Optional[float]: "number",
        Optional[bool]: "boolean",
        list[str]: "array",
        list[int]: "array",
        list[float]: "array",
        list[bool]: "array",
        Optional[list[str]]: "array",
        Optional[list[int]]: "array",
        Optional[list[float]]: "array",
        Optional[list[bool]]: "array",
        Union[str, Path]: "string",
    }
    thinking_buf: List[str] = Field(default_factory=list, description="thinking buffer")
    commands: List[dict] = Field(default_factory=list, description="The command that was successfully parsed")
    valid_names: Optional[set] = Field(default=None, description="Valid command names for tag filtering")

    async def xml_lexer(self, queue: asyncio.Queue):
        while True:
            chunk = await queue.get()
            async for i in self.parse_chunk(chunk=chunk):
                yield i
                if i == EOS:
                    return

    async def parse_chunk(self, chunk: str | None):
        if chunk is None:
            if self.state != LexerState.SEARCH_FUNCTION:
                raise ValueError("Invalid XML")
            if not self.functions:
                tokens = self._make_ask_user()
            else:
                # If the queue receives None, it indicates that there is no more data, and the parsing should be terminated.
                tokens = [("end_array", None), EOS]
            for token in tokens:
                yield token
            self.tokens.extend(tokens)
            return

        self.rcv_buf += chunk
        self.is_rcv_buf_ready = True
        while self.is_rcv_buf_ready and self.rcv_buf:
            if self.state == LexerState.SEARCH_FUNCTION:
                async for i in self._search_function_name():
                    yield i
                    self.tokens.append(i)
            elif self.state == LexerState.PARSE_ARG_NAME:
                async for i in self._search_arg_name():
                    yield i
                    self.tokens.append(i)
            elif self.state == LexerState.PARSE_ARG_VALUE:
                async for i in self._search_arg_value():
                    yield i
                    self.tokens.append(i)

    async def _search_function_name(self):
        function_blocks = re.finditer(self.element_regx, self.rcv_buf)
        function_block = None
        for i in function_blocks:
            name = i.group(1)
            if not name or name[0] == "/":
                continue
            if self.valid_names is not None and name not in self.valid_names:
                continue
            function_block = i
            break
        if not function_block:
            self.is_rcv_buf_ready = False
            return
        function_name = function_block.group(1)
        idx = function_block.end()
        self._add_thinking_buf(function_block.start())
        self.rcv_buf = self.rcv_buf[idx:]
        tokens: List[Tuple[str, Any]] = [("start_array", None)] if not self.functions else []
        function_object = FunctionObject(function_name=function_name)
        self.functions.append(function_object)
        tokens.extend(
            [
                ("start_map", None),
                ("map_key", "command_name"),
                ("start_string", None),
                ("string", function_name),
                ("end_string", None),
                ("map_key", "args"),
                ("start_map", None),
            ]
        )
        for token in tokens:
            yield token
        self.state = LexerState.PARSE_ARG_NAME

    async def _search_arg_name(self):
        # Search function end symbol
        end_function_name = self.functions[-1].end_function_name
        idx = self.rcv_buf.find(end_function_name)
        # Search parameter name
        variable_block = None
        variable_blocks = re.finditer(self.element_regx, self.rcv_buf)
        for i in variable_blocks:
            variable_exists, variable_idx = self._detect_variable(i)
            if variable_exists:
                variable_block = i
                break
            if idx >= 0 and (not variable_exists or variable_idx > idx):
                self.rcv_buf = self.rcv_buf[idx + len(end_function_name) :]
                tokens = [("end_map", None), ("end_map", None)]
                for token in tokens:
                    yield token
                self.state = LexerState.SEARCH_FUNCTION
                return
        if not variable_block:
            self.is_rcv_buf_ready = False
            return
        variable_name = variable_block.group(1)
        idx = variable_block.end()
        self.rcv_buf = self.rcv_buf[idx:]
        arg_object = ArgObject(variable_name=variable_name)
        function_object = self.functions[-1]
        function_object.args.append(arg_object)
        tokens = [("map_key", variable_name)]
        # The XML protocol carries every argument as a string; structured (list/dict/
        # model) params are only supported on the native tool-use channel. See the
        # BaseTool docstring "Channel limitation".
        arg_object.variable_type = "string"
        for token in tokens:
            yield token
        self.state = LexerState.PARSE_ARG_VALUE

    async def _search_arg_value(self):
        # Search variable end symbol
        function_object = self.functions[-1]
        arg_object = function_object.args[-1]
        end_variable_name = arg_object.end_variable_name
        idx = self.rcv_buf.find(end_variable_name)
        meet_end = idx >= 0
        if idx < 0:
            idx = max(0, len(self.rcv_buf) - len(end_variable_name))
        variable_value = self.rcv_buf[:idx]
        if not variable_value and not meet_end:  # The data in the receive buffer is not sufficient for processing.
            self.is_rcv_buf_ready = False
            return
        if arg_object.variable_type == "string":
            self.rcv_buf = self.rcv_buf[idx + len(end_variable_name) :] if meet_end else self.rcv_buf[idx:]
            meet_beginning, value = await self._handle_str_type_value(
                arg_object=arg_object, value=variable_value, meet_end=meet_end
            )
            if meet_beginning:
                yield "start_string", None
            if value:
                yield arg_object.variable_type, value
            if meet_end:
                yield "end_string", None
        else:
            if meet_end:
                self.rcv_buf = self.rcv_buf[idx + len(end_variable_name) :]
            else:
                self.is_rcv_buf_ready = False
                return
            type_, value = await self._handle_other_type_value(arg_object=arg_object, value=variable_value)
            if type_ == "array":
                tokens = self._recursive_tokenize_array(value)
                for i in tokens:
                    yield i
            elif type_ == "map":
                tokens = self._recursive_tokenize_map(value)
                for i in tokens:
                    yield i
            else:
                yield type_, value
        if meet_end:
            self.state = LexerState.PARSE_ARG_NAME

    async def _handle_str_type_value(self, arg_object: ArgObject, value: str, meet_end: bool) -> Tuple[bool, str]:
        meet_beginning = arg_object.variable_value is None
        if arg_object.variable_value is None:
            arg_object.variable_value = ""
        if meet_beginning:
            value = self._strip_empty_line_prefix(value)
        if meet_end:
            value = self._strip_empty_line_postfix(value)
        if value:
            current = arg_object.variable_value if isinstance(arg_object.variable_value, str) else ""
            arg_object.variable_value = current + value
        else:
            arg_object.variable_value = value
        return meet_beginning, value

    @staticmethod
    async def _handle_other_type_value(arg_object: ArgObject, value: str) -> Tuple[str, Any]:
        default_values = {"number": 0, "array": [], "map": {}}
        value = value.strip()
        if value.lower() == "null":
            return arg_object.variable_type, None
        if arg_object.variable_type in default_values:
            if value == "":
                return arg_object.variable_type, default_values[arg_object.variable_type]
            try:
                val = PythonObjectParser.safe_literal_eval(value)
                return arg_object.variable_type, val
            except Exception as e:
                raise ValueError(f"`{value}` eval error: {e}")
        if arg_object.variable_type == "boolean":
            return arg_object.variable_type, value.lower() == "true"
        raise ValueError(f"Unknown type: {arg_object}, {value}")

    @staticmethod
    def _strip_empty_line_prefix(value: str) -> str:
        lines = value.splitlines(keepends=True)
        size = len(lines)
        for i in range(size):
            txt = lines[i].strip()
            if txt or lines[i][-1] != "\n":  # Do not perform operations on incomplete lines to avoid accidental damage.
                return "".join(lines[i:])
        return ""

    @staticmethod
    def _strip_empty_line_postfix(value: str) -> str:
        lines = value.splitlines()
        size = len(lines)
        for i in range(size):
            ix = size - 1 - i
            txt = lines[ix].strip()
            if txt:
                return "\n".join(lines[0 : ix + 1])
        return ""

    def _make_ask_user(self):
        if self.ignore_text:
            self.rcv_buf = ""
            return [EOS]
        tokens = [
            ("start_array", None),
            ("start_map", None),
            ("map_key", "command_name"),
            ("start_string", None),
            ("string", "reply_to_user"),
            ("end_string", None),
            ("map_key", "args"),
            ("start_map", None),
            ("map_key", "content"),
            ("start_string", None),
            ("string", self.rcv_buf),
            ("end_string", None),
            ("end_map", None),
            ("end_map", None),
            ("end_array", None),
            EOS,
        ]
        self.rcv_buf = ""
        return tokens

    @staticmethod
    def _detect_variable(variable_block) -> Tuple[bool, int]:
        if not variable_block:
            return False, -1
        variable_name = variable_block.group(1)
        if variable_name[0] == "/":
            return False, -1

        if variable_name.startswith('?xml version="1.0" '):
            return False, -1

        return True, variable_block.end()

    def read_thinking_buf(self) -> str:
        if len(self.thinking_buf) > 1:  # The last one is the dirty buffer.
            val = self.thinking_buf.pop(0)
            return val
        return ""

    def _add_thinking_buf(self, idx: int):
        thinking = self.rcv_buf[:idx]
        if not thinking:
            return
        if not self.thinking_buf:
            self.thinking_buf.append(thinking)
        else:
            self.thinking_buf[-1] += thinking
        self._new_line_thinking_buf()

    def _new_line_thinking_buf(self):
        self.thinking_buf.append("")

    async def loads_xml(self, xml: str):
        q = asyncio.Queue()
        q.put_nowait(xml)
        q.put_nowait(None)

        NOT_SET = object()
        tmp = NOT_SET
        map_keys = []
        path = []
        async for event, value in self.xml_lexer(queue=q):
            if event == "string":
                path[-1].append(value)
                continue
            elif event == "map_key":
                path[-1][value] = None
                map_keys.append(value)
            elif event == "start_map":
                path.append({})
            elif event == "end_map":
                tmp = path.pop()
                if "command_name" in tmp and "args" in tmp:
                    self.commands.append(tmp)
            elif event == "start_array":
                path.append([])
            elif event == "end_array":
                tmp = path.pop()
            elif event == "start_string":
                path.append([])
            elif event == "end_string":
                values = path.pop()
                tmp = "".join(values)
            else:
                tmp = value

            if tmp is not NOT_SET:
                if path:
                    c_path = path[-1]
                    if isinstance(c_path, list):
                        c_path.append(tmp)
                    elif isinstance(c_path, dict):
                        c_path[map_keys.pop()] = tmp
                    tmp = NOT_SET

    def get_commands(self) -> list[Command]:
        result: list[Command] = []
        for i in self.commands:
            result.append(Command.model_validate(i))
        return result

    def _recursive_tokenize_map(self, value: dict) -> list:
        # There is no need to implement recursive calls for the time being.
        tokens = [("start_map", None)]
        for k, v in value.items():
            tokens.append(("map_key", k))
            type_i = self.types[type(v)]
            if type_i == "string":
                tokens.extend([("start_string", None), ("string", v), ("end_string", None)])
            else:
                tokens.append((type_i, v))
        tokens.append(("end_map", None))
        return tokens

    def _recursive_tokenize_array(self, value: list) -> list:
        # There is no need to implement recursive calls for the time being.
        tokens = [("start_array", None)]
        for i in value:
            type_i = self.types[type(i)]
            if type_i == "string":
                tokens.extend([("start_string", None), (type_i, i), ("end_string", None)])
            else:
                tokens.append((type_i, i))
        tokens.append(("end_array", None))
        return tokens

    @staticmethod
    def safe_literal_eval(s):
        # Handle JSON- or YAML-style literals
        replacements = {"true": "True", "false": "False", "null": "None"}

        # Apply replacements (case-sensitive to avoid issues)
        for val, python_val in replacements.items():
            s = s.replace(val, python_val)

        return literal_eval(s)
