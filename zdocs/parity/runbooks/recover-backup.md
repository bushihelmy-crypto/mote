# Recover backup or restore

Preserve all failed inputs. Verify RecoverySet signature, participant list,
epochs, component digests and key versions. Restore daemon authority before caller
journals, then reconcile every open receipt. A missing caller or invalid component
may only yield the lower declared consistency level; it never upgrades to
`APPLICATION_CONSISTENT`.
