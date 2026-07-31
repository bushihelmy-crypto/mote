# Provider degradation runbook

## Diagnosis
Confirm typed availability evidence by provider, region, endpoint, operation and generation-pinned profile. Check pool saturation and live-certification freshness independently.

## Containment
Reject only the proven affected scope. Preserve caller policy, request constraints, receipt and decision trace.

## Recovery
Use only journaled policy failover with a fresh attempt, ordinal and permit. Restore the endpoint or activate a freshly certified profile generation.

## Verification
Verify one wire unit per authorized attempt, canonical failure mapping, healthy pool utilization and fresh signed certification.

## Escalation
Escalate to the provider resource owner for account, region, capability, outage or budget constraints and to transport operations for pool failures.

## Forbidden actions
Never issue an implicit second request, strip rejected fields and retry, infer health from content quality, or treat an unsigned status message as authority.
