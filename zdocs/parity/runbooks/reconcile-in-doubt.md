# Reconcile IN_DOUBT

Verify execution/generation identity, receipt revision and owner journal first.
Collect provider query, verified webhook and artifact evidence without sending a
new business request. The daemon publishes a signed proposal; only the logical
owner may accept/reject it, append terminal state and settle usage. With no
evidence, retain `IN_DOUBT`, page the owner and never force success.
