# ADR 0010: Heterogeneous telemetry core and typed edges

Status: Accepted

## Decision

`TelemetryRuntime` is an honest heterogeneous `object` transport responsible only for bounded capacity, backpressure, isolation, delivery, and lifecycle. Typed publishers and consumers live at adjacent edges. `EventNarrower[EventT_co]` produces a type through `TypeGuard` and is covariant; handlers consume contravariantly; `TypedTelemetryBinding[EventT]` is invariant because it both produces and consumes the type.

A typed binding owns one narrower, async handler, and optional sync handler. It creates the sole erased adapter admitted to the runtime. Both delivery paths run the same narrower first. Async delivery invokes only the async handler. Sync delivery invokes the sync handler when present and otherwise skips that binding; it never reflects for a method, blocks on async work, or redirects into an async mailbox.

Presentation owns `PresentationInputEvent`, the closed union it actually supports. `Projector[InputEventT_contra, ViewEventT_co]` maps that input to a read-only sequence. View consumers consume only their declared view union, and wire adapters consume view events rather than raw telemetry. Domain events, view events, and wire payloads remain separate.

## Rejected alternatives

- A global event union violates layer direction and still cannot cover Product events.
- A generic heterogeneous runtime merely disguises erasure.
- Runtime-checkable generic protocols do not narrow an arbitrary object.
- `Any + getattr` in projectors or sync dispatch loses the closed edge contract.

## Compatibility and migration tests

Pyright cases reject mismatched narrower/handler, projector, and consumer combinations. Runtime tests verify wrong event filtering, shared async/sync narrowing, missing-sync skip behavior, backpressure, isolation, presentation filtering before projection, and unchanged wire payloads.
