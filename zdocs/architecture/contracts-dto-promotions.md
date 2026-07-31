# Contracts DTO promotion register

| DTO | Boundary | Producer → consumer | Persisted | Compatibility owner | Why Contracts |
| --- | --- | --- | --- | --- | --- |
| `InferenceResult` | Runtime/Kernel | ModelInferencePort → commands/execution | checkpoint | Runtime model + Kernel commands | normalized model response crosses implementation boundary |
| `InferenceIntent` / `ResolvedInferenceTarget` | Kernel/Runtime | execution → Runtime inference | checkpoint identity | Runtime inference | two-stage routing lease and capability snapshot |
| `MaterializedToolCatalog` / `ToolBindingSnapshot` | Runtime/Kernel | ToolProvider → commands/execution | descriptor/checkpoint | Runtime tools | pins visible schema to executable revision |
| `AcceptedOutput` / `CommittedOutput` | Kernel/Runtime | output evaluation → transaction/delivery | SessionEvent payload | Runtime session | immutable accepted decision crosses atomic commit boundary |
| `ExecutionOperationContext` / mutation results/frontier | Kernel/Runtime | execution → transaction implementation | transaction record | Runtime transaction | fenced idempotent business mutation contract |
| `PromptSection` / `ProtocolVocabulary` | owner/inference | Runtime/Product/commands → assembler | fingerprints only | inference assembler | multiple owners contribute validated immutable data |

Command decode contexts, protocol issues, executed-command and history projection remain in `kernel.commands`; graph nodes and migration algorithms remain in their lowest owner.
