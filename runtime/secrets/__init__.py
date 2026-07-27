"""Runtime secret storage, reference expansion, and redaction primitives.

Pure, dependency-light building blocks for masking known secret values in any
text that flows to the model:

* :mod:`.policy` — the pure ``redact(text, {value: label})`` function (no I/O).
* :mod:`.store` — a :class:`~mote.runtime.secrets.store.SecretStore` that collects
  known secret *values* from two sources: a loaded config dict (any leaf the
  config center judges secret via ``_is_secret``) and a user vault file at
  ``~/.mote/secrets.json``.

ToolResultPolicy consumes these primitives so every tool's output is redacted
before hooks, observers, persistence, or the model can receive it.
"""
