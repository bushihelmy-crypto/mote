# Recover outbox

Pause optional publication, verify receipt/outbox transaction integrity and
restart the idempotent publisher from its durable cursor. Never synthesize caller
logical events. Confirm backlog age returns below the frozen SLO.
