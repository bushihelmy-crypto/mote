# Upgrade and rollback runbook

## Diagnosis
Validate schema N/N-1, migrations, generation artifact, resource readiness, plugin isolation and drain state. Identify the last verified compatible generation.

## Containment
Stop activation, reject new pins to a draining generation, and preserve resumability, activation history and plugin evidence.

## Recovery
Stage without mutating active state, activate atomically and drain old pins. On failure reactivate the verified prior artifact or atomically repoint Shared discovery.

## Verification
Confirm receipts, reconciliation, queues, artifact roots, plugin isolation, negotiation and zero generation tear before retirement.

## Escalation
Escalate incompatible schema or migration failures to architecture ownership and stuck drains to daemon operations.

## Forbidden actions
Never rewrite activation history, force-retire live pins, restore a deleted decoder, or activate an unverified plugin/generation.
