"""Function-tool dependency projectors preserve the tool context type."""

from dataclasses import dataclass

from mote.kernel.execution.run_context import ToolContext
from mote.runtime.tools.function_toolset import NativeFunctionToolset


@dataclass
class AgentDeps:
    token: str


@dataclass
class ToolDeps:
    token: str


tools = NativeFunctionToolset[AgentDeps]("case")


@tools.tool(project=lambda deps: deps.token)
async def wrong(ctx: ToolContext[ToolDeps]) -> str:
    return ctx.deps.token
