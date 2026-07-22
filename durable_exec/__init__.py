"""Optional durable-execution backends that carry a third-party dependency.

The always-on Tier-1 backend (``mote.loop.durable.JsonlBackend``, zero-dep) lives
under ``loop/durable``. This package holds the OPT-IN Tier-2 backends whose
dependency the core must never carry — today just ``temporal`` (requires the
``[temporal]`` extra). The core reaches a backend only through
``mote.loop.durable.make_durable_backend``, which imports a subpackage here
lazily and degrades to the JSONL tier when the optional dependency is absent, so
importing ``mote`` never requires anything in this package.
"""
