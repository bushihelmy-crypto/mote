# Kernel ownership decisions

## Agent configuration

| Field group | Owner |
| --- | --- |
| spawnable name/description/version | `contracts.agents.AgentDescriptor` |
| active flag and last inference result | `kernel.execution.AgentRunState` |
| command protocol, cost/continuation and deployed capability selection | `runtime.agent.RoleSchema` |
| permissions, hooks, LSP, watching, persistence and browser policy | `runtime.agent.RoleSchema` |
| Coding Agent tool/deferred-tool defaults | `product.agents.defaults` |
| Coding Agent/tool/skills prose | owning Product capability |

There is no `kernel.agent` package and no inherited mixed `AgentSpec`.

## SLO fields

Kernel retains only `graph_transitions` and `run_event_buffer`, because they bound algorithm termination and memory. Recovery timing/record counts, disk barriers and shutdown timing are Runtime performance policy and are not Kernel contracts.

## Tool snapshot retention

A durable descriptor contains provider identity/version, catalog identity/version/fingerprint, target capability fingerprint, executable semantic identities, registry revision and retention lease. A snapshot remains pinned until its run is terminal and no checkpoint, effect reconciliation, transaction or publication references it. Missing providers or executable identities produce `unrecoverable_binding`; name-based rebinding is forbidden.

## Schema algorithms

`MaterializedToolCatalog` is a cross-boundary DTO. Static tool schema construction stays in `kernel.tools`; dynamic materialization/lifecycle stays in Runtime. Workflow annotation compilation stays with Orchestration workflows; Contracts contains no shared schema compiler.
