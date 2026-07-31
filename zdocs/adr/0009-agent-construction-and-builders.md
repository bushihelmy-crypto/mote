# ADR 0009: Runtime construction requests and Product builders

Status: Accepted

## Decision

Root construction remains a Product composition-root concern with its own explicit request. Child construction uses immutable `AgentConstructionRequest` values and a pre-bound `AgentBuilder[RequestT_contra, OutputT]`; concrete agent classes and Product configuration remain private to the builder. Catalogs return immutable `SpawnableAgentDefinition[OutputT]`, never classes. `OutputT` is invariant through definition, plan, control, runnable, runtime, and handle. Dynamic name lookup uses a real object-output wrapper and is registered as a dynamic boundary.

The child transaction order is fixed:

1. admission decision;
2. residency reservation;
3. identity, nickname, and path reservations;
4. construction request creation from final reserved values;
5. builder invocation;
6. immediate cleanup registration with the transaction;
7. Runtime service provisioning;
8. inert, externally invisible runtime registration;
9. supervision, cost-node, communication-graph, and incarnation attachment;
10. transaction commit;
11. external visibility and spawned publication.

Every acquired resource registers its inverse immediately. Failure rolls back in strict reverse order. Agent cleanup therefore runs after failed construction handoff and before reservation release. Provisioning, registration, or supervision failure cannot expose the child. Only `AgentControl` commits and publishes spawned state.

`AgentConstructionRequest` contains stable value/ID fields only: parent session identity, reserved child identity/path/nickname, cwd, and context policy. It contains no wiring, services, mutable/Product config, ownership objects, concrete orchestration path implementation, extras, or kwargs. `Engine` depends on an explicit closeable-agent protocol rather than `getattr`.

## Rejected alternatives

- `type[AgentT] + **kwargs: Any` cannot preserve construction invariants.
- A global root/child DTO leaks application concerns into spawn.
- Tool-side construction bypasses admission and final reservation values.
- Covariant assignment or cast-based heterogeneous lookup is unsound because run outcomes are invariant.

## Compatibility and migration tests

Pyright cases prove request contravariance, output invariance, plan/builder compatibility, and root-request rejection. Transaction tests inject failure at construction, provisioning, inert registration, and supervision and assert reverse cleanup, released reservations, no visibility, and no spawned event.
