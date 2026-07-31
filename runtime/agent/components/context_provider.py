"""ContextProvider — assembles everything one think() cycle needs.

Owns think-request assembly so the react loop never reaches into the Role to
build a think request. ContextProvider is a stateful Role subsystem: it holds RoleState
(for env views, memory, project_root) and the live collaborators PromptBuilder
queries, and produces a InferenceRequest — the complete input set for
InferenceEngine.start().

Dependency direction is one-way: this module imports PromptBuilder (a stateless
pure-function assembler in mote/prompts/), never the reverse.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mote.contracts.conversation import Message, to_role_content_dicts
from mote.contracts.conversation.fields import IMAGES, PDFS
from mote.contracts.events.agent import BudgetEvent
from mote.contracts.model.inference import InferenceIntent, InferenceRequirements
from mote.contracts.output import OutputBindingKind
from mote.kernel.commands.prompts import BUDGET_EXHAUSTED
from mote.kernel.execution.context import PROCEED, BudgetVerdict, ExecutionContext
from mote.kernel.execution.context_provider import BaseContextProvider
from mote.kernel.inference import build_routing_signals
from mote.kernel.inference.prompt_builder import InferenceContext, InferenceInputs, InferenceSubsystems, PromptBuilder
from mote.kernel.inference.request import InferenceRequest
from mote.runtime.run_context import current_run_context

if TYPE_CHECKING:
    from mote.runtime.agent.role import Role


class ContextProvider(BaseContextProvider):
    """Packs the per-flow parameters one react cycle needs — the Role's "dirty work".

    Holds the Role and does nothing but READ it: it reads identity data +
    subsystem outputs, renders prompts, formats the LLM request, resolves tool
    specs (``prepare()``), and packs the static observe + loop-control knobs
    (``execution_context()``). It never WRITES the Role (no RoleState mutation) and
    never lazy-inits components — component ownership stays in the Role, which
    remains the sole assembler. The provider is the only place that holds the
    whole Role; everything downstream (the flow) sees only the narrow
    ``BaseContextProvider`` face, so it cannot reach the Role through here.

    Dependencies are read through ``@property`` forwarders (not snapshotted in
    ``__init__``) so the provider always sees the Role's current value — e.g. a
    component lazy-initialised after the provider was constructed.
    """

    def __init__(self, role: "Role", inference_port, tool_snapshot_manager):
        self._role = role
        self._inference_port = inference_port
        self._tool_snapshot_manager = tool_snapshot_manager
        # Budget-gate latches: each threshold event fires at most once per run,
        # so a soft warning isn't re-emitted every think once spend stays above
        # 80%, and the hard-stop event isn't re-emitted after the flow halts.
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

    async def resolve_inference_target(
        self,
        request: InferenceRequest | None = None,
        *,
        model_call_id: str = "",
    ):
        """Resolve the think LLM via the router (the conduit for flag + llmconfig).

        - The router is routing-enabled (its per-agent-kind ``router`` config
          selected a concrete ``strategy`` — not ``None``) AND there are
          messages → intelligent routing (the router picks a model card per
          request from the request signals).
        - Otherwise → the default route in the active Runtime composition.

        Routing on/off lives solely in the ``router`` config block (per agent
        kind), resolved once at router-build time into ``router.routing_enabled``.
        """
        messages = request.req if request is not None else None
        wire = to_role_content_dicts(messages or ())
        signals = build_routing_signals(wire)
        binding_kind = request.output_binding.binding.kind if request is not None else OutputBindingKind.TEXT
        tool_specs = (
            self._executor.canonical_tool_specs(include_hidden=True)
            if request is not None and self._role.role_schema.command_protocol == "native"
            else ()
        )
        intent = InferenceIntent(
            model_call_id=model_call_id,
            requirements=InferenceRequirements(
                tool_calling=bool(request is not None and self._schema.tools),
                structured_output=binding_kind is not OutputBindingKind.TEXT,
                native_schema=binding_kind is OutputBindingKind.NATIVE_SCHEMA,
                multimodal=tuple(
                    kind
                    for kind, needed in (
                        (
                            "image",
                            any(
                                isinstance(message, Message) and bool(message.metadata.get(IMAGES))
                                for message in messages or ()
                            ),
                        ),
                        (
                            "pdf",
                            any(
                                isinstance(message, Message) and bool(message.metadata.get(PDFS))
                                for message in messages or ()
                            ),
                        ),
                    )
                    if needed
                ),
                native_tool_search=any(
                    bool(spec.get("defer_loading")) for spec in tool_specs or () if isinstance(spec, dict)
                ),
                resume=True,
            ),
            routing_messages=tuple((str(item.get("role", "user")), str(item.get("content", ""))) for item in wire),
            estimated_tokens=signals.estimated_tokens,
        )
        return await self._inference_port.resolve(intent)

    async def release_inference_target(self, target) -> None:
        await self._inference_port.release(target)

    def resolve_task_model_route(self, task: str):
        return self._role.router.model_route_for_task(task)

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

    def execution_context(self) -> ExecutionContext:
        """Pack the static observe + loop-control parameters for one run().

        Pulls the
        RoleSchema knobs (react limits, identity, tool list, memory toggles) and
        the live msg_buffer/watch off RoleState into one pure-data bundle, so
        the flow never reaches into the Role or schema directly. Re-evaluated
        per run() (the engine calls it once at the top), so a recovered/edited
        schema or a swapped msg_buffer is always reflected.
        """
        schema = self._schema
        state = self._state
        return ExecutionContext(
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
                await self._role.telemetry.emit(BudgetEvent(spend=spend, limit=limit, fraction=fraction, stopped=True))
            return BudgetVerdict(stop=True, message=BUDGET_EXHAUSTED)

        if fraction >= self._BUDGET_WARN_FRACTION and not self._budget_warned:
            self._budget_warned = True
            await self._role.telemetry.emit(
                BudgetEvent(
                    spend=spend,
                    limit=limit,
                    fraction=fraction,
                    stopped=False,
                )
            )
        return PROCEED

    async def prepare(self) -> InferenceRequest:
        """Build the full InferenceEngine.start() input set for this cycle.

        Pipeline: collect context → render prompts → assemble the request from
        the managed history + the rendered command prompt → resolve tool specs
        from the command channel.

        The request is built by the ContextManager: it runs the history-level
        compaction passes (fold then summarize) over the stored history,
        then returns ``managed_history + [user_prompt]`` (the command prompt is
        appended to the request only, never stored) — proper context-window
        management, not a crude tail truncation.
        """
        run_context = current_run_context()
        if run_context is not None:
            await self._executor.prepare_run_step(run_context)
        ctx = await self._collect()
        prompt = PromptBuilder.assemble(self._schema.system_prompt, self._schema.cmd_prompt, ctx)

        req = await self._context_manager.prepare_request(prompt.user_prompt)

        output_binding = self._channel.output_binding_decision(is_text=self._role.output_contract.is_text)
        return InferenceRequest(
            req=req,
            system_prompt=prompt.system_prompt,
            tool_specs=None,
            output_binding=output_binding,
            command_channel=self._channel,
            output_schema=self._role.output_contract.decoder.schema.canonical,
            schema_fingerprint=self._role.output_contract.decoder.schema.fingerprint,
            vocabulary_fingerprint=prompt.vocabulary_fingerprint,
            prompt_section_set_fingerprint=prompt.section_set_fingerprint,
        )

    def finalize_for_model(self, request: InferenceRequest, target) -> InferenceRequest:
        channel = self._channel.for_model(
            self._inference_port.profile(target),
            output_schema=request.output_schema,
        )
        request.output_binding = channel.output_binding_decision(is_text=self._role.output_contract.is_text)
        snapshot = self._tool_snapshot_manager.materialize(
            target,
            include_hidden=target.capabilities.supports_native_tool_search,
        )
        request.tool_snapshot = snapshot
        request.tool_specs = channel.tool_specs(snapshot.catalog, self._role.output_contract)
        request.tool_projection_fingerprint = snapshot.catalog.fingerprint
        request.protocol_fingerprint = f"{target.command_protocol}:{target.command_protocol_version}"
        request.command_channel = channel
        return request

    async def _collect(self) -> InferenceContext:
        """Delegate context collection to PromptBuilder."""
        return await PromptBuilder.collect_context(self._think_inputs(), self._think_subsystems())

    def _think_inputs(self) -> InferenceInputs:
        """The field set published for one think() cycle — pure data.

        One explicit bundle of the state-derived values (cwd, project root)
        PromptBuilder reads from instead of reaching into the Role or its schema.
        No identity fields flow through — name/profile drive routing/signing on
        the Role, not the prompt.
        """
        return InferenceInputs(
            working_dir=self._get_cwd(),
            original_working_dir=self._state.original_working_dir,
            project_root=self._state.project_root,
            role_info=self._schema.role_info,
        )

    def _think_subsystems(self) -> InferenceSubsystems:
        """The live collaborators handed to PromptBuilder for one think().

        Delegates to the graph's ``think_subsystems`` factory so the wiring
        lives in exactly one place (the composition root). The provider stays a
        pure reader: it never assembles subsystems itself.
        """
        return self._role._components.make_think_subsystems()
