# ADR 0008: Toolset variance, erasure, and run binding

Status: Accepted

## Decision

Public `XmlToolset[AgentDepsT_contra]` and `NativeToolset[AgentDepsT_contra]` are nominally separate and contravariant because they only consume agent dependencies through `RunContext`. Internal `DefinitionSource[DefinitionT]` alone owns heterogeneous definition snapshots, uniqueness, and composition mechanics. `AgentDependencies[DepsT, OutputT]` retains `tuple[XmlToolset[DepsT] | NativeToolset[DepsT], ...]`. Runtime protocol compatibility remains a construction-time check.

Definitions contain schema, decoder, capability factory, protocol, and static approval metadata only. Context-sensitive approval belongs to `ToolsetPolicy[AgentDepsT_contra]`. Run binding combines the policy with `RunContext[DepsT]` and produces a non-generic `BoundApprovalPolicy`; only that bound policy enters an erased Runtime registry. Missing or invalid binding fails closed. Filter, prefix, rename, combine, dynamic run views, and step views preserve the same policy exactly once.

Dynamic definitions are validated at registration for protocol, definition/capability kind, unique identity, and renderer compatibility. The stable failure is `ToolsetRegistrationError`, carrying toolset ID, definition name, and violated field; invalid startup configuration fails before execution. Valid existing definitions retain behavior.

## Rejected alternatives

- Adding Agent dependencies to each definition conflates static definition and run context.
- Recovering typed policy from ambient `RunContext[Any]` with a cast is unsound.
- Runtime recreation of Python structural assignability would diverge from pyright.
- A single multi-purpose generic base entangles storage, protocol, lifecycle, and policy.

## Compatibility and migration tests

Pyright cases cover contravariant nominal and structural dependency composition plus projector mismatch. Runtime tests cover protocol validation, lifecycle views, policy preservation, one-time binding, dynamic definitions, registration failure fields, and fail-closed approval.
