"""Agent tool — spawn typed child agents for bounded subtasks."""

from __future__ import annotations

from string import Template

from metagpt.executor.agent_registry import registry as agent_registry
from metagpt.executor.base_tool import BaseTool
from metagpt.executor.tool_registry import register_tool
from metagpt.common.schema import UserMessage
from metagpt.common.prompt.agent import AGENT_TASK_PROMPT
from metagpt.common.prompt.tools import AGENT_DESCRIPTION

@register_tool
class Agent(BaseTool):
    """Spawn a typed child agent for bounded subtasks.

    Stateless tool — only needs session_id from ctx.
    Agent type's own RoleSchema defines its tools, config, etc.
    """

    name = "Agent"
    aliases = ["run_agent"]
    description = AGENT_DESCRIPTION

    async def call(self, *, agent_type: str, prompt: str, context: str = "") -> str:
        """Spawn a typed child agent and return its summary.

        Args:
            agent_type: Name of the registered agent type to spawn.
            prompt: The concrete instruction for the agent to execute.
            context: Background info the agent needs. Optional.
        """
        prompt = prompt.strip()
        context = context.strip()
        if not prompt:
            return "Error: 'prompt' cannot be empty."

        agent_registry.discover()
        agent_cls = agent_registry.get(agent_type)
        if agent_cls is None:
            available = ", ".join(sorted(agent_registry.all_agents().keys()))
            return f"Error: unknown agent_type '{agent_type}'. Available: {available}"

        # Agent type defines everything itself — we only pass parent session_id
        agent = agent_cls(parent_session_id=self.session_id)
        task_brief = Template(AGENT_TASK_PROMPT).safe_substitute(
            parent_name=self.session_id,
            context=context or "(no additional context)",
            task=prompt,
        )
        # The task brief carries protocol symbols (⟦...⟧); lower them through the
        # child agent's own channel so it receives its protocol's surface syntax
        # (e.g. native agents never see <end></end>). build-time assert in the
        # lowerer fails loudly on any unlowered symbol rather than leaking it.
        task_brief = agent.command_channel.lower(task_brief)
        msg = UserMessage(content=task_brief)
        await agent.run(with_message=msg)
        report = agent.state.last_end_output.strip()
        if not report:
            report = "Agent finished without a final summary."
        return report

    @classmethod
    def custom_schema(cls) -> dict | None:
        """Embed available agent types into the schema for LLM consumption."""
        agent_registry.discover()
        lines = []
        for name, agent_cls in agent_registry.all_agents().items():
            desc = agent_cls.get_schema()["description"]
            tools = ", ".join(getattr(agent_cls, "tools", None) or [])
            max_turns = getattr(agent_cls, "max_react_loop", "?")
            lines.append(f"- {name}: {desc} (tools: {tools}, max_turns: {max_turns})")
        agent_types_desc = "\n".join(lines) if lines else "No agent types registered."

        return {
            "name": cls.name,
            "description": f"{cls.description}\n\nAvailable agent types:\n{agent_types_desc}",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_type": {"type": "string", "description": "Name of the registered agent type to spawn."},
                    "prompt": {"type": "string", "description": "The concrete instruction for the agent to execute."},
                    "context": {"type": "string", "description": "Background info the agent needs. Optional."},
                },
                "required": ["agent_type", "prompt"],
            },
        }
