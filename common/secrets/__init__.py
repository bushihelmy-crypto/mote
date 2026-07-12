"""common.secrets — secret-redaction primitives (spike).

Pure, dependency-light building blocks for masking known secret values in any
text that flows to the model:

* :mod:`.policy` — the pure ``redact(text, {value: label})`` function (no I/O).
* :mod:`.store` — a :class:`~mote.common.secrets.store.SecretStore` that collects
  known secret *values* from two sources: a loaded config dict (any leaf the
  config center judges secret via ``_is_secret``) and a user vault file at
  ``~/.mote/secrets.json``.

The executor wires these into a PostToolUse control subscriber
(``mote.executor.secrets.subscriber``) so every tool's model-facing output is
redacted at the single control-plane choke point — not per file tool.
"""
