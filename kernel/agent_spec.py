"""Pure definition of one Agent's model-facing identity and behavior."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from mote.kernel.prompt.role import CMD_PROMPT, ROLE_INFO, SYSTEM_PROMPT


class AgentSpec(BaseModel):
    """Static Kernel inputs that determine how one Agent thinks and acts.

    Runtime reliability policy—permissions, persistence, sandbox, hooks, LSP,
    browser processes—does not belong here. ``RoleSchema`` extends this model at
    the Runtime boundary while retaining a flat serialized representation.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str = "Zero"
    profile: str = "Role"

    system_prompt: str = SYSTEM_PROMPT
    cmd_prompt: str = CMD_PROMPT
    role_info: str = ROLE_INFO

    command_protocol: Literal["xml", "native"] = "native"
    max_cost: float = 0.0
    think_kind: str = "default"
    max_auto_continue: int = 0

    deferred_tools: list[str] = Field(
        default_factory=lambda: [
            "Terminal",
            "Jupyter",
            "Agent",
            "RunGraph",
            "Sleep",
            "WebBrowser",
            "WebSearch",
            "GenerateMedia",
            "Skill",
            "DeviceUse",
            "Canvas",
        ]
    )
    tools: list[str] = Field(
        default_factory=lambda: [
            "Read",
            "Edit",
            "Search",
            "Bash",
            "AskUserQuestion",
            "SearchTools",
            "GenerateMedia",
            "Terminal",
            "Jupyter",
            "Agent",
            "RunGraph",
            "Sleep",
            "WebBrowser",
            "WebSearch",
            "Skill",
            "DeviceUse",
            "Canvas",
        ]
    )
    mcps: list[str] = Field(default_factory=list)
    agents: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)

    enable_memory: bool = True
    observe_all_msg_from_buffer: bool = True

    @property
    def display_name(self) -> str:
        return f"{self.name}({self.profile})" if self.profile else self.name


__all__ = ["AgentSpec"]
