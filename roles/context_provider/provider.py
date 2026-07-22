"""ContextProvider — assembles everything one think() cycle needs.

Owns think-request assembly so the react loop never reaches into the Role to
build a think request. ContextProvider is a stateful Role subsystem: it holds RoleState
(for env views, memory, project_root) and the live collaborators PromptBuilder
queries, and produces a ThinkRequest — the complete input set for
ThinkEngine.start().

Dependency direction is one-way: this module imports PromptBuilder (a stateless
pure-function assembler in mote/prompts/), never the reverse.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mote.common.base import PROCEED, BudgetVerdict, LoopContext
from mote.common.events import BudgetEvent
from mote.common.prompt.output import BUDGET_EXHAUSTED
from mote.common.schema import to_role_content_dicts
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
        # Budget-gate latches: each threshold event fires at most once per run,
        # so a soft warning isn't re-emitted every think once spend stays above
        # 80%, and the hard-stop event isn't re-emitted after the loop halts.
        self._budget_warned = False
        self._budget_stopped = False

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

        - The router is routing-enabled (its per-agent-kind ``router`` config
          selected a concrete ``strategy`` — not ``None``) AND there are
          messages → intelligent routing (the router picks a model card per
          request from the request signals).
        - Otherwise → the fixed configured ``config.models.default``.

        Routing on/off lives solely in the ``router`` config block (per agent
        kind), resolved once at router-build time into ``router.routing_enabled``.
        """
        role = self._role
        if role.router.routing_enabled and messages:
            # Key routing state (history / holds / seed floor) to this role's
            # stable session id so per-agent state stays isolated — without it
            # every agent shares the ``"default"`` key and their histories mix.
            # RoutingRequest.messages is the {role, content} wire shape; the loop
            # hands us Message objects. to_role_content_dicts drops tool_calls so
            # the strategy's token estimate / text extraction read plain dicts
            # without the counter choking on a nested list.
            wire = to_role_content_dicts(messages)
            return await role.router.aroute(RoutingRequest(messages=wire, session_key=role.session_id))
        return role.router.route(llm_config=role.config.models.default)

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
            name=schema.name,
            display_name=schema.display_name,
            tools=schema.tools,
            msg_buffer=state.msg_buffer,
            watch=state.watch,
            enable_memory=schema.enable_memory,
            observe_all=schema.observe_all_msg_from_buffer,
        )

    #: Soft-warning threshold — surface a CLI notice once spend crosses this
    #: fraction of the cap (the hard cap is 1.0).
    _BUDGET_WARN_FRACTION = 0.8

    async def enforce_budget(self) -> BudgetVerdict:
        """Rule on this agent's own spend against ``schema.max_cost``.

        Reads this agent's accrued spend (``context.cost_manager.total_cost``)
        against the configured cap. Emits — on the observation plane, so it can
        only inform the UI/recorder, never fold the turn — a soft ``BudgetEvent``
        once at 80% and a hard-stop ``BudgetEvent`` once at 100%, each latched so
        it fires at most once per run. Returns a stop verdict only when the hard
        cap is crossed. A non-positive cap disables the gate: return ``PROCEED``
        immediately, emitting nothing, so an unbudgeted agent is silent.
        """
        limit = self._schema.max_cost
        if limit <= 0:
            return PROCEED

        spend = self._role.context.cost_manager.total_cost
        fraction = spend / limit

        if spend >= limit:
            if not self._budget_stopped:
                self._budget_stopped = True
                await self._role.event_bus.observe(
                    BudgetEvent(spend=spend, limit=limit, fraction=fraction, stopped=True)
                )
            return BudgetVerdict(stop=True, message=BUDGET_EXHAUSTED)

        if fraction >= self._BUDGET_WARN_FRACTION and not self._budget_warned:
            self._budget_warned = True
            await self._role.event_bus.observe(BudgetEvent(spend=spend, limit=limit, fraction=fraction, stopped=False))
        return PROCEED

    async def prepare(self) -> ThinkRequest:
        """Build the full ThinkEngine.start() input set for this cycle.

        Pipeline: collect context → render prompts → assemble the request from
        the managed history + the rendered command prompt → resolve tool specs
        from the command channel.

        The request is built by the ContextManager: it runs the history-level
        compaction passes (fold then summarize) over the stored history,
        then returns ``managed_history + [user_prompt]`` (the command prompt is
        appended to the request only, never stored) — proper context-window
        management, not a crude tail truncation.
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

        One explicit bundle of the state-derived values (cwd, project root)
        PromptBuilder reads from instead of reaching into the Role or its schema.
        No identity fields flow through — name/profile drive routing/signing on
        the Role, not the prompt.
        """
        return ThinkInputs(
            working_dir=self._get_cwd(),
            original_working_dir=self._state.original_working_dir,
            project_root=self._state.project_root,
            role_info=self._schema.role_info,
        )

    def _think_subsystems(self) -> ThinkSubsystems:
        """The live collaborators handed to PromptBuilder for one think().

        Delegates to the graph's ``think_subsystems`` factory so the wiring
        lives in exactly one place (the composition root). The provider stays a
        pure reader: it never assembles subsystems itself.
        """
        return self._role._components.make_think_subsystems()
