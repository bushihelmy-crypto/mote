"""Agent tool — spawn typed child agents for bounded subtasks."""

from __future__ import annotations

from string import Template

from mote.common.agent_control import Lifecycle, SpawnContext, SpawnSpec, spawn_and_run
from mote.common.prompt.agent import AGENT_TASK_PROMPT
from mote.common.prompt.tools import AGENT_DESCRIPTION
from mote.common.schema import UserMessage
from mote.executor.agent_registry import registry as agent_registry
from mote.executor.base_tool import BaseTool
from mote.executor.tool_registry import register_tool

# Complete model-facing message sentences, hoisted to module-top templates so the
# wording lives in one place (fill via ``.format(...)`` at the return site).
_MSG_PROMPT_EMPTY = "Error: 'prompt' cannot be empty."
_MSG_UNKNOWN_AGENT = "Error: unknown agent_type '{agent_type}'. Available: {available}"
_MSG_SPAWN_FAILED = "Error: could not spawn agent '{agent_type}' (agent limit reached)."
_MSG_NO_SUMMARY = "Agent finished without a final summary."


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
            return _MSG_PROMPT_EMPTY

        agent_registry.discover()
        agent_cls = agent_registry.get(agent_type)
        if agent_cls is None:
            available = ", ".join(sorted(agent_registry.all_agents().keys()))
            return _MSG_UNKNOWN_AGENT.format(agent_type=agent_type, available=available)

        # Agent type defines everything itself — we only pass the parent linkage.
        # The child is born through the single spawn authority (resolved via the
        # ambient control plane), which enforces the cap / depth / lineage and
        # rolls its cost up to the parent. The handle always tears the child down
        # (its own terminal/kernel PTY, LSP servers, file-watch loop — all
        # session-scoped OS resources that leak if dropped without cleanup()).
        def role_factory(spawn_ctx: SpawnContext):
            return agent_cls(parent_session_id=spawn_ctx.parent_id or self.session_id)

        def build_message(agent):
            task_brief = Template(AGENT_TASK_PROMPT).safe_substitute(
                parent_name=self.session_id,
                context=context or "(no additional context)",
                task=prompt,
            )
            # The brief carries protocol symbols (⟦...⟧); lower them through the
            # child's own channel so it receives its protocol's surface syntax
            # (e.g. native agents never see <end></end>). A build-time assert in
            # the lowerer fails loudly on any unlowered symbol.
            return UserMessage(content=agent.command_channel.lower(task_brief))

        spec = SpawnSpec(
            role_factory=role_factory,
            nickname=agent_type,
            agent_role=agent_type,
            parent_id=self.session_id,
            lifecycle=Lifecycle.EPHEMERAL,
        )
        report = await spawn_and_run(spec, build_message)
        if report is None:
            return _MSG_SPAWN_FAILED.format(agent_type=agent_type)
        return report or _MSG_NO_SUMMARY

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
