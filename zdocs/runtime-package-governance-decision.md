# Runtime package governance decision

- Decision: `internal-delete`.
- Scope: `mote.runtime.*` wiring paths and
  `mote.orchestration.tasks.build_background_task_pool` are internal composition APIs.
- Compatibility policy: canonical owners replace old imports directly; no forwarding
  modules, aliases, or compatibility re-exports are retained.
- Serialization audit: runtime session data stores typed facts and message payloads,
  not Python module-qualified wiring helpers. No pickle/dill/cloudpickle dependency on
  the removed paths is supported.
