"""ContextProvider — assembles everything one think() cycle needs.

Extracted from Role so the react loop never reaches into the Role to build a
think request. ContextProvider is a stateful Role subsystem: it holds RoleState
(for env views, memory, project_root) and the live collaborators PromptBuilder
queries, and produces a ThinkRequest — the complete input set for
ThinkEngine.start().

Dependency direction is one-way: this module imports PromptBuilder (a stateless
pure-function assembler in mote/prompts/), never the reverse.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mote.common.base import LoopContext
from mote.roles.context_provider.base import BaseContextProvider
from mote.roles.context_provider.request import ThinkRequest
from mote.router.schema import RoutingRequest
from mote.think.prompt_builder import PromptBuilder, ThinkContext, ThinkInputs, ThinkSubsystems

if TYPE_CHECKING:
    from mote.roles.role import Role


class ContextProvider(BaseContextProvider):
    """Packs the per-flow parameters one react cycle needs — the Role's "dirty work".

    Holds the Role and does nothing but READ it: it reads identity data +
    subsystem outputs, renders prompts, formats the LLM request, resolves tool
    specs (``prepare()``), and packs the static observe + loop-control knobs
    (``loop_context()``). It never WRITES the Role (no RoleState mutation) and
    never lazy-inits components — component ownership stays in the Role, which
    remains the sole assembler. The provider is the only place that holds the
    whole Role; everything downstream (the loop) sees only the narrow
    ``BaseContextProvider`` face, so it cannot reach the Role through here.

    Dependencies are read through ``@property`` forwarders (not snapshotted in
    ``__init__``) so the provider always sees the Role's current value — e.g. a
    component lazy-initialised after the provider was constructed.
    """

    def __init__(self, role: "Role"):
        self._role = role

    # ------------------------------------------------------------------
    # Property forwarders — read the live value off the Role on demand.
    # ------------------------------------------------------------------

    @property
    def _schema(self):
        return self._role.role_schema

    @property
    def _state(self):
        return self._role.state

    async def resolve_llm(self, messages=None):
        """Resolve the think LLM via the router (the conduit for flag + llmconfig).

        - ``config.enable_router`` True → intelligent routing from the request
          messages (the router picks a model card per request).
        - Otherwise → the fixed configured ``config.llm``.
        """
        role = self._role
        if role.config.enable_router and messages:
            return await role.router.aroute(RoutingRequest(messages=messages))
        return role.router.route(llm_config=role.config.llm)

    @property
    def _executor(self):
        return self._role.executor

    @property
    def _channel(self):
        return self._role.command_channel

    @property
    def _get_cwd(self):
        return self._role.get_cwd

    @property
    def _context_manager(self):
        return self._role.context_manager

    def loop_context(self) -> LoopContext:
        """Pack the static observe + loop-control parameters for one run().

        Was Role._make_loop's hand-written ``LoopContext(...)``. Pulls the
        RoleSchema knobs (react limits, identity, tool list, memory toggles) and
        the live msg_buffer/watch off RoleState into one pure-data bundle, so
        the loop never reaches into the Role or schema directly. Re-evaluated
        per run() (the loop calls it once at the top), so a recovered/edited
        schema or a swapped msg_buffer is always reflected.
        """
        schema = self._schema
        state = self._state
        return LoopContext(
            max_react_loop=schema.max_react_loop,
            max_consecutive_react_limit=schema.max_consecutive_react_limit,
            memory_k=schema.memory_k,
            name=schema.name,
            display_name=schema.display_name,
            tools=schema.tools,
            msg_buffer=state.msg_buffer,
            watch=state.watch,
            enable_memory=schema.enable_memory,
            observe_all=schema.observe_all_msg_from_buffer,
        )

    async def prepare(self) -> ThinkRequest:
        """Build the full ThinkEngine.start() input set for this cycle.

        Pipeline (moved verbatim from Role._think's glue): collect context →
        render prompts → assemble the request from the managed history + the
        rendered command prompt → resolve tool specs from the command channel.

        The request is built by the ContextManager: it runs the history-level
        compaction passes (microcompact → autocompact) over the stored history,
        then returns ``managed_history + [user_prompt]`` (the command prompt is
        appended to the request only, never stored). This replaces the old crude
        memory_k truncation with proper context-window management.
        """
        ctx = await self._collect()
        system_prompt, user_prompt = PromptBuilder.build(self._schema.system_prompt, self._schema.cmd_prompt, ctx)

        req = await self._context_manager.prepare_request(user_prompt)

        tool_specs = self._channel.tool_specs(self._executor)
        return ThinkRequest(
            req=req,
            system_prompt=system_prompt,
            tool_specs=tool_specs,
        )

    async def _collect(self) -> ThinkContext:
        """Delegate context collection to PromptBuilder."""
        return await PromptBuilder.collect_context(self._think_inputs(), self._think_subsystems())

    def _think_inputs(self) -> ThinkInputs:
        """The field set published for one think() cycle — pure data.

        One explicit bundle of flat identity values (unpacked from RoleSchema)
        plus the values derived from state (env clause, team listing, cwd,
        project root, output format). PromptBuilder reads from this snapshot
        instead of reaching into the Role or its schema.
        """
        schema = self._schema
        return ThinkInputs(
            name=schema.name,
            profile=schema.profile,
            goal=schema.goal,
            constraints=schema.constraints,
            desc=schema.desc,
            example=schema.example,
            instruction=schema.instruction,
            language=schema.language,
            env_desc=self._env_desc(),
            other_role_names=self._other_role_names(),
            team_info=self._team_info(),
            working_dir=self._get_cwd(),
            original_working_dir=self._state.original_working_dir,
            project_root=self._state.project_root,
        )

    def _think_subsystems(self) -> ThinkSubsystems:
        """The live collaborators handed to PromptBuilder for one think().

        Delegates to the graph's ``think_subsystems`` factory so the wiring
        lives in exactly one place (the composition root). The provider stays a
        pure reader: it never assembles subsystems itself.
        """
        return self._role._components.make_think_subsystems()

    def _env_desc(self) -> str:
        """The env description used in the role prefix, or "" if none."""
        if self._state.env and self._state.env.desc:
            return self._state.env.desc
        return ""

    def _other_role_names(self) -> str:
        """Comma-joined names of the other roles in the env, or "" if none."""
        if not (self._state.env and self._state.env.desc):
            return ""
        all_roles = self._state.env.role_names()
        return ", ".join([r for r in all_roles if r != self._schema.name])

    def _team_info(self) -> str:
        if not self._state.env:
            return ""
        lines = []
        for role in self._state.env.roles.values():
            lines.append(f"{role.name}: {role.role_schema.profile}, {role.role_schema.goal}")
        return "\n".join(lines)
