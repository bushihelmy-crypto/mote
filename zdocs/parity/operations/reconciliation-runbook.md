# Reconciliation runbook

## Diagnosis
Identify execution, generation, receipt revision and logical owner from redacted evidence. Verify receipt, journal, provider evidence and artifact digests.

## Containment
Keep the receipt unresolved, stop unbounded polling, preserve evidence and prevent duplicate owner commands or usage settlement.

## Recovery
Run only the bounded evidence strategy. Submit idempotent proposals to the logical owner and apply the owner acknowledgement defined by `reconciliation-actions-v1.yaml`.

## Verification
Confirm owner acknowledgement, valid terminal transition, exactly-once usage settlement and backlog/oldest-age recovery.

## Escalation
Escalate stuck owner actions or conflicting evidence; success overrides require the contractually required approvals.

## Forbidden actions
Never infer success without evidence, bypass the logical owner, settle twice, or expose secret or raw principal data.
