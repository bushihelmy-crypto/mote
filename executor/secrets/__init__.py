"""executor.secrets — wire secret redaction onto the control plane.

Home of :class:`~mote.executor.secrets.subscriber.RedactionSubscriber`, the
PostToolUse control subscriber that rewrites a finished tool's model-facing
output through :func:`mote.common.secrets.policy.redact`. Living in the executor
layer (like ``executor/permission``) lets it import the event/outcome types and
the pure ``common.secrets`` primitives while the bus underneath stays tool-free.
"""
