"""Child builders accept only the explicit child construction request."""

from mote.contracts.agent import AgentBuilder, AgentConstructionRequest


class RootRequest:
    pass


def build(builder: AgentBuilder[AgentConstructionRequest, str]) -> None:
    builder.build(RootRequest())
