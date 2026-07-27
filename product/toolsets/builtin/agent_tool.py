"""Agent tool — spawn typed child agents for bounded subtasks."""

from __future__ import annotations

import inspect
import json
from string import Template
from typing import ClassVar

from pydantic import TypeAdapter

from mote.contracts.introspection.docstrings import description_body
from mote.contracts.schema import UserMessage
from mote.contracts.spawn import Lifecycle, SpawnContext, SpawnSpec
from mote.kernel.prompt.agent import AGENT_TASK_PROMPT
from mote.runtime.agent.control import spawn_and_run
from mote.runtime.logging import logger
from mote.runtime.tools.agent_registry import AgentCatalog
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.capability_types import BuildChildAgent
from mote.runtime.tools.tool_registry import register_tool

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
    requires = ("build_child_agent",)
    build_child_agent: BuildChildAgent
    # Recall synonyms for tool-search: ways a model expresses "hand this off"
    # that the summary ("spawn a typed child agent") does not literally contain.
    keywords: ClassVar[list[str]] = [
        "delegate",
        "subagent",
        "sub-agent",
        "spawn",
        "hand off",
        "dispatch task",
        "委派",
        "子代理",
        "分派",
        "子任务",
        "并发",
    ]

    def __init__(self, agent_catalog: AgentCatalog) -> None:
        super().__init__()
        self._agent_catalog = agent_catalog

    async def call(self, *, agent_type: str, prompt: str, context: str = "") -> str:
        """Spawn a typed child agent for a bounded subtask — returns its summary.

        Delegate a self-contained subtask to a fresh child of the named type. The
        child runs its own react loop with its own toolset and config, then
        returns only its final summary — so its long working process never
        pollutes your history. Pick an ``agent_type`` from the list in this tool's
        schema; put the instruction in ``prompt`` and any background in
        ``context``.

        Args:
            agent_type: Name of the registered agent type to spawn.
            prompt: The concrete instruction for the agent to execute.
            context: Background info the agent needs. Optional.
        """
        prompt = prompt.strip()
        context = context.strip()
        if not prompt:
            return _MSG_PROMPT_EMPTY

        agent_cls = self._agent_catalog.get(agent_type)
        if agent_cls is None:
            available = ", ".join(sorted(self._agent_catalog.all_agents()))
            return _MSG_UNKNOWN_AGENT.format(agent_type=agent_type, available=available)

        # Agent type defines everything itself — we only pass the parent linkage.
        # The child is born through the single spawn authority (resolved via the
        # ambient control plane), which enforces the cap / depth / lineage and
        # rolls its cost up to the parent. The handle always tears the child down
        # (its own terminal/kernel PTY, LSP servers, file-watch loop — all
        # session-scoped OS resources that leak if dropped without cleanup()).
        def role_factory(spawn_ctx: SpawnContext):
            return self.build_child_agent(
                agent_cls,
                parent_session_id=spawn_ctx.parent_id or self.session_id,
            )

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

        async def _seed(role):
            # Spawn-time seed floor: decide an initial tier from this first
            # prompt and record it as a raise-only floor for the child's step
            # routing. ``getattr`` guards keep this a safe no-op for rule-based
            # children (no ``seed_session``), independent of the config switch.
            if not role.config.router.spawn_routing:
                return
            # Seed is only ever *consumed* by a child that runs step routing.
            # The presence of ``seed_session`` (squilla strategy installed) already
            # implies routing is enabled for this child — this is a belt-and-suspenders
            # guard against a routing-disabled router that somehow exposes a seed hook.
            if not role.router.routing_enabled:
                logger.warning(
                    f"Agent '{agent_type}': router.spawn_routing is on but this "
                    f"agent_type does not route (router.sub_agent.strategy is null) — "
                    f"the seed floor would never be read. Set sub_agent.strategy to "
                    f"'squilla' to consume it."
                )
                return
            await role.router.seed_session(role.session_id, prompt)

        report = await spawn_and_run(spec, build_message, on_spawn=_seed)
        if report is None:
            return _MSG_SPAWN_FAILED.format(agent_type=agent_type)
        output = report.output
        if isinstance(output, str):
            return output or _MSG_NO_SUMMARY
        encoded = TypeAdapter(type(output)).dump_python(output, mode="json")
        return json.dumps(encoded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def description_for(cls, catalog: AgentCatalog) -> str:
        """Render the immutable Agent roster owned by one Application."""

        lines = []
        for name, agent_cls in catalog.all_agents().items():
            desc = agent_cls.get_schema()["description"]
            tools = ", ".join(getattr(agent_cls, "tools", None) or [])
            lines.append(f"- {name}: {desc} (tools: {tools})")
        agent_types_desc = "\n".join(lines) if lines else "No agent types registered."

        # Base description comes from the call() docstring body (docstring-native
        # prose); we only APPEND the live agent-type roster. summary() reads this
        # description's first line, so it stays the docstring summary sentence.
        base = description_body(inspect.getdoc(cls.call) or "")

        return f"{base}\n\nAvailable agent types:\n{agent_types_desc}"
