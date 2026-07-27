from __future__ import annotations

from dataclasses import dataclass

import pytest

from mote.contracts.run_context import RunContext, ToolContext
from mote.runtime.run_context import bind_run_context
from mote.runtime.tools.function_toolset import NativeFunctionToolset, XmlFunctionToolset
from mote.runtime.tools.tool_executor import ToolExecutor


@dataclass(frozen=True)
class AppDependencies:
    database: object
    secret: str


@dataclass(frozen=True)
class SearchDependencies:
    database: object


@pytest.mark.asyncio
async def test_native_function_tool_receives_only_projected_dependencies() -> None:
    database = object()
    deps = AppDependencies(database=database, secret="agent-only")
    tools = NativeFunctionToolset[AppDependencies]("search")
    observed: list[ToolContext[SearchDependencies]] = []

    @tools.tool(name="Search", project=lambda value: SearchDependencies(database=value.database))
    async def search(context: ToolContext[SearchDependencies], query: str, limit: int = 3) -> str:
        """Search the application database.

        Args:
            query: Search query.
            limit: Maximum result count.
        """

        observed.append(context)
        return f"{query}:{limit}"

    executor = ToolExecutor(
        "session",
        tools=["Search"],
        toolsets=(tools,),
        command_protocol="native",
    )
    schema = executor.native_tool_specs()[0]
    assert set(schema["input_schema"]["properties"]) == {"query", "limit"}

    with bind_run_context(RunContext(deps=deps, session_id="session", run_id="run")):
        result = await executor.run_command("Search", {"query": "mote", "limit": 2})

    assert result.success is True
    assert observed[0].deps == SearchDependencies(database=database)
    assert not hasattr(observed[0].deps, "secret")


def test_xml_function_registration_rejects_structured_or_typed_json_arguments() -> None:
    tools = XmlFunctionToolset[object]("xml")

    with pytest.raises(TypeError, match="parameter 'items'.*NativeFunctionToolset"):

        @tools.tool(name="Collect", project=lambda deps: deps)
        def collect(context: ToolContext[object], items: list[str]) -> str:
            return ",".join(items)


@pytest.mark.asyncio
async def test_xml_function_tool_is_an_explicit_separate_registration() -> None:
    tools = XmlFunctionToolset[str]("legacy")

    @tools.tool(name="Echo", project=lambda deps: deps)
    def echo(context: ToolContext[str], value: str) -> str:
        """Echo a scalar value.

        Args:
            value: Value to echo.
        """

        return context.deps + value

    executor = ToolExecutor(
        "session",
        tools=["Echo"],
        toolsets=(tools,),
        command_protocol="xml",
    )
    assert set(executor.all_xml_tool_schemas()) == {"Echo"}
    with bind_run_context(RunContext(deps="a", session_id="session", run_id="run")):
        result = await executor.run_command("Echo", {"value": "b"})
    assert result.output == "ab"
