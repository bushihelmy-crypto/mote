# Mote

**English** | [简体中文](./README.zh-CN.md)

`mote/` (the `mote.*` package) is a **composable, event-driven, layered** agent
runtime framework. It splits an agent's execution into a think/act ReAct loop and
adds a multi-agent runtime, session persistence, unified LLM routing, and a
permission sandbox — all built on a zero-back-dependency `common` foundation layer.


## Highlights

- **Composition-over-inheritance Role**: `Role` is a pure orchestrator — static
  `RoleSchema` config + serializable `RoleState` runtime + lazily-assembled
  components, keeping only a thin attribute surface and a minimal capability surface.
- **think / act ReAct loop**: `loop` drives observe→think→act→finish; `think`
  assembles the prompt and calls the LLM in the background, and `executor` is the
  single chokepoint that dispatches tool calls.
- **Dual-protocol command channel**: XML and native tool-use are unified into one
  command IR, with the envelope chosen automatically per endpoint wire protocol
  (OpenAI / Anthropic).
- **Unified LLM routing**: explicit / task-map / smart-policy routing over a
  multi-provider abstraction, wiring in cost accounting, OAuth credentials, error
  recovery, and context compaction.
- **Two-axis permissions**: an approval axis (should we ask the user?) × a sandbox
  axis (may we touch this path?), combined orthogonally; `deny`/`ask` need no
  bypass, and command execution must pass the classifier.
- **Crash-safe session persistence**: an append-only `rollout.jsonl` is the single
  source of truth, supporting replay recovery, fork lineage, and file-history
  snapshots (blob / git dual backends).
- **Multi-agent runtime**: event-driven scheduler + hierarchical agent paths +
  per-agent mailboxes + LRU residency eviction + cron scheduling + file watching.
- **Event-bus spine**: the screen (renderer) and disk (recorder) are fed by the
  same event stream; hooks can intercept at lifecycle seams.
- **Zero-intrusion observability & logging**: loguru + trace-id + decorator/mixin
  auto-instrumentation; Langfuse tracing is off by default and lazily loaded.

## Install

```bash
pip install mote                   # from PyPI
pip install -e ".[dev]"            # from a checkout, with dev tooling
```

The browser tool needs a Chromium runtime the first time you use it:

```bash
python -m playwright install chromium
```

## Quick start

Launch the interactive REPL:

```bash
mote                               # console entry point (default Assistant + toolset)
python -m mote.cli                 # equivalent module form
python -m mote.cli --model <name> --tools Read,Edit,Search,Bash --cwd .
```

- Ctrl+C: mid-turn → interrupt the current turn; double-press at the prompt → exit.
  Ctrl+D: exit.
- Type `/help` inside the REPL for slash commands (agents / sessions / resume / fork).
- Models and credentials are read from `config.yaml` + the layered config (below).

## Layout (layered, bottom-up)

| Package | Layer | One-line responsibility |
|------|------|-----------|
| `common` | 0 base | Abstract base classes / cross-layer Protocols / data models / config / exceptions / events / hooks / logging / prompts / utilities |
| `context` | 1 | What the LLM sees: message CRUD + two-level compaction + skill injection (`skills/`) + per-turn volatile context (`turn_context/`) |
| `executor` | 1 | Action-side engine: single-chokepoint tool dispatch + permission sandbox + background tasks (`tasks/`) + MCP |
| `router` | 1 | LLM model selection + multi-provider abstraction + cost / OAuth / recovery |
| `session` | 1 | Crash-safe persistence of session history (rollout/replay/snapshot/fork) |
| `parser` | 2 | Command-protocol channel: XML ⇄ native tool-use unified into a command IR |
| `think` | 2 | Think side: assemble prompt + background LLM call → `ThinkResult` |
| `loop` | 2 | The ReAct main loop (observe/think/act/finish) |
| `roles` | 3 | Role orchestration core (schema + state + lazy component assembly) |
| `environment` | 4 | Multi-agent control plane + scheduling + residency + cron + file watching |
| `cli` | 5 entry | Interactive REPL / command-line entry point |
| `memory` | — | Procedural / semantic / episodic memory (planned) |

Dependencies flow strictly downward; cross-layer calls always go through Protocols
in `common/interface/` for dependency inversion:

```
common  ◀──  context / executor / router / session  ◀──  parser / think / loop  ◀──  roles  ◀──  environment  ◀──  cli
```

## Configuration

A layered configuration center (`common/config/`, a 9-tier priority stack, low → high):

```
DEFAULT → SYSTEM(/etc) → USER(~/.mote) → PROJECT(mote/config.yaml)
→ WORKDIR(<cwd>/.mote, untrusted → credentials stripped) → PROFILE → ENV(MOTE_)
→ CLI_FLAG(-c key=value) → PROGRAMMATIC → MANAGED(locked)
```

Dicts deep-merge, lists union-dedupe, scalars are won by the higher tier.
Diagnostics: `python -m mote.common.config.diagnostics --strict`.

See [`.env.example`](./.env.example) for the available environment variables.

## Tests

```bash
python -m pytest mote/ztest/{roles,loop,executor,think,context,skills,router,tasks,environment} -q
```

Tests live under `mote/ztest/<subsystem>/` (not `tests/`).

## Further reading

- [`zdocs/ARCHITECTURE.md`](./zdocs/ARCHITECTURE.md) — detailed per-package
  architecture docs + a bird's-eye map + the full data-flow of a single turn.
- [`AGENTS.md`](./AGENTS.md) — conventions for writing code in this repo (layering,
  tool development, protocols, testing, change discipline).
