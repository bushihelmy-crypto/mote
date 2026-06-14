from typing import Any

from pydantic import BaseModel


class ToolSchema(BaseModel):
    description: str


class Tool(BaseModel):
    name: str
    path: str
    schemas: dict = {}
    code: str = ""
    tags: list[str] = []
    tool_class: Any = None  # Original class reference stored by @register_tool
    aliases: dict[str, str] = {}  # e.g. {"Terminal": "run", "Terminal.run_command": "run"}
    session_aware: bool = False  # Whether methods accept session_id kwarg
    instantiable: bool = True  # False = skip auto-instantiation (e.g. Agent, Plan)
