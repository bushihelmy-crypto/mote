from mote.contracts.agent import AgentBuilder, AgentConstructionRequest


def require_text_builder(
    builder: AgentBuilder[AgentConstructionRequest, str],
) -> None:
    del builder


def reject_wrong_output(
    integer_builder: AgentBuilder[AgentConstructionRequest, int],
) -> None:
    require_text_builder(integer_builder)
