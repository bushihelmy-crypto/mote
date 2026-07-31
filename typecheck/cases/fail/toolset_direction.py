from typing import Protocol

from mote.kernel.output import text_output_contract
from mote.runtime.agent import AgentDependencies
from mote.runtime.tools.provider import NativeToolset


class CommonDeps(Protocol):
    cwd: str


class CodingDeps(CommonDeps, Protocol):
    language: str


class ConcreteCommonDeps:
    cwd = "/workspace"


coding_tools: NativeToolset[CodingDeps] = NativeToolset("coding", ())
common_deps: CommonDeps = ConcreteCommonDeps()
common_dependencies = AgentDependencies[CommonDeps, str](
    deps=common_deps,
    output_contract=text_output_contract(),
    toolsets=(coding_tools,),
)
