# Credential runbook

## Diagnosis
Inspect only redacted slot/version metadata, authoritative credential verdicts and redaction-violation evidence. Identify the immutable binding and affected scopes.

## Containment
Quarantine the affected binding or diagnostic artifact. Keep unrelated credential revisions and providers available.

## Recovery
Provision a new secret revision through the credential store, stage and validate a generation, and activate atomically.

## Verification
Confirm the healthy revision is active, the old binding remains quarantined, and logs, traces, receipts, metrics and artifacts contain no secret.

## Escalation
Escalate suspected exposure to security operations and credential provisioning failures to the resource owner.

## Forbidden actions
Never print or export credential material or place it in argv, environment, discovery, receipts, diagnostics or artifacts.
