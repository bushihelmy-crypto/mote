from mote.contracts.agent import SpawnableAgentDefinition, SpawnPlan
from mote.orchestration.agents.control import AgentControl
from mote.orchestration.agents.lifecycle.handle import ChildAgentHandle


class ReviewReport:
    pass


async def preserve_output(
    definition: SpawnableAgentDefinition[ReviewReport],
    control: AgentControl,
) -> ChildAgentHandle[ReviewReport]:
    plan: SpawnPlan[ReviewReport] = SpawnPlan(request_id="review-request", definition=definition)
    handle: ChildAgentHandle[ReviewReport] = await control.spawn_agent(plan)
    return handle
