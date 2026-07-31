# Runtime 三阶段分包治理实施需求

本文是 P1-P6 拆包和二阶段 namespace 收束后的实施需求。它回答两个问题：

1. 还有哪些 runtime 入口必须继续拆、合或清理。
2. 删除旧聚合模块后，如何避免默认路径、配置 discovery、后台去重逻辑散落到全仓。

结论：方向继续保持窄范围治理。三阶段不是继续压缩顶层包数量，而是补齐迁移契约、composition root 设计、AST 架构验收和 breaking-change 说明。

## 1. 批准前置条件

进入实施前必须满足：

1. 每个当前 `runtime/` 顶层入口都有唯一 disposition。
2. `runtime.paths` 的替代设计自包含，不依赖读者跳转到其他计划。
3. `product.paths` 是默认根目录和 `.mote` discovery 的唯一实现 owner。
4. 路径默认值只在 `product.paths` 求值，runtime capability 只接收具体路径字段、领域 config 或窄 Protocol。
5. `runtime.maintenance` 拆成领域 gate，明确生命周期、共享范围、key 和释放语义。
6. `orchestration.tasks` 保持编排层 owner，不整体下沉 runtime；Agent 绑定 adapter 移到 product composition，并通过 typed build context 解耦 Role 私有结构。
7. AST 架构测试是正式门禁；`rg` 只作为人工迁移辅助。
8. 旧路径 compatibility re-export、alias、forwarding module 禁止。
9. 产品层在实施前确认 `runtime.*` 和 `orchestration.tasks` public API 是否属于外部承诺 API；若是，版本策略、release note 和迁移表先完成，再删除旧入口。

## 2. 当前顶层 Disposition

以下清单以当前工作树为准，覆盖当前 `runtime/` 顶层目录和顶层 `.py` 文件。

### 2.1 顶层目录

| 当前入口 | 处置 | 目标 | 理由 |
| --- | --- | --- | --- |
| `runtime.agent` | retain | `runtime.agent` | Role、component wiring、agent runtime adapter，职责清晰。 |
| `runtime.artifacts` | retain | `runtime.artifacts` | 通用 artifact/CAS/store/publication/GC，不吸收 FileOps reachability。 |
| `runtime.code_map` | retain | `runtime.code_map` | 索引和 tree-sitter 执行机制；路径和启用策略由 composition 注入。 |
| `runtime.config` | retain | `runtime.config` | 配置读取和 layering；不拥有所有默认路径语义。 |
| `runtime.context` | retain | `runtime.context` | history、compaction、turn bus，不重新吸收 Skills/CodeMap 产品策略。 |
| `runtime.control` | retain | `runtime.control` | 只承载 lifecycle、leases、scheduling 等 control primitives。 |
| `runtime.durable` | retain | `runtime.durable` | ThinkJournal、Temporal、runner 等执行语义，不是纯 persistence。 |
| `runtime.errors` | retain | `runtime.errors` | runtime 错误分类。 |
| `runtime.events` | retain | `runtime.events` | EventFabric、subscription、journal、backpressure，是事件投递平面。 |
| `runtime.fileops` | retain | `runtime.fileops` | 文件读写、mutation、review、FileOps roots/pins。 |
| `runtime.hook` | retain | `runtime.hook` | hook runtime aggregation 和 adapter；配置源由 composition 注入。 |
| `runtime.interactive` | retain | `runtime.interactive` | browser、terminal、kernel、canvas、device 等交互后端。 |
| `runtime.ledger` | retain | `runtime.ledger` | run/tool/think/timer idempotency 语义，不是纯 append primitive。 |
| `runtime.lsp` | retain | `runtime.lsp` | 通用 runtime capability，不是 Coding Agent product adapter。 |
| `runtime.media` | retain | `runtime.media` | media 处理能力，暂不合并。 |
| `runtime.models` | retain | `runtime.models` | 模型客户端、路由、cost、ratelimit、auth、模型 adapter。 |
| `runtime.output` | retain | `runtime.output` | runtime output commit/event wrapper；纯语义在 kernel。 |
| `runtime.persistence` | retain | `runtime.persistence` | 只放 disk/storage/path primitive，不承载 session layout。 |
| `runtime.projections` | retain | `runtime.projections` | projection registry 和 durable projection 语义。 |
| `runtime.prompt` | retain | `runtime.prompt` | prompt admission、sanitization、runtime prompt policy 执行。 |
| `runtime.resilience` | retain | `runtime.resilience` | 通用 admission/failover/retry/failure policy。 |
| `runtime.resources` | retain | `runtime.resources` | resource accounting、spill 等 runtime resource 能力。 |
| `runtime.sandbox` | retain | `runtime.sandbox` | sandbox、network、seccomp、resource isolation。 |
| `runtime.secrets` | retain | `runtime.secrets` | vault、cipher、secret refs、TOTP。 |
| `runtime.service_gateway` | retain | `runtime.service_gateway` | externally hosted tool/service gateway，不属于 models。 |
| `runtime.session` | retain | `runtime.session` | session log、replay、checkpoint、session workspace。 |
| `runtime.telemetry` | retain | `runtime.telemetry` | logging + observability substrate，不承载 event fabric。 |
| `runtime.tools` | retain | `runtime.tools` | 工具执行、permission、result、MCP adapter。 |
| `runtime.vcs` | retain | `runtime.vcs` | VCS probe/state 能力，供 product 和 runtime adapter 使用。 |
| `runtime.watching` | retain | `runtime.watching` | file/watch runtime capability。 |

### 2.2 顶层文件

| 当前入口 | 处置 | 目标 | 理由 |
| --- | --- | --- | --- |
| `runtime/__init__.py` | retain | package marker | 不提供旧路径兼容转发。 |
| `runtime/engine.py` | retain | `runtime.engine` | runtime engine 入口。 |
| `runtime/paths.py` | split/delete | `product.paths.RuntimePaths` + capability-specific inputs | 路径上帝模块，必须删除。 |
| `runtime/process.py` | retain | `runtime.process` | process primitive，暂不移动。 |
| `runtime/reporting.py` | retain | `runtime.reporting` | reporting facade，暂不并入 telemetry。 |
| `runtime/run_context.py` | retain | `runtime.run_context` | run context primitive。 |
| `runtime/services.py` | retain | `runtime.services` | service wiring primitive。 |

已完成或不得恢复的旧入口：

| 旧入口 | 状态 | 约束 |
| --- | --- | --- |
| `runtime.workspace` | 已下沉 | 归 `runtime.session.workspace`，不得恢复顶层 facade。 |
| `runtime.disk` | 已合并 | 归 `runtime.persistence`，不得恢复顶层 facade。 |
| `runtime.logging` | 已合并 | 归 `runtime.telemetry.logging`。 |
| `runtime.observability` | 已合并 | 归 `runtime.telemetry.observability`。 |
| `runtime.completion` | 已下沉 | 归 `runtime.agent.completion`。 |
| `runtime.reconciliation.py` | 已拆分 | failure policy 在 `runtime.resilience`，具体恢复归 owner。 |
| `runtime.lifecycle.py` / `runtime/leases.py` / `runtime.scheduling` | 已合并 | 归 `runtime.control`。 |
| `runtime.maintenance.py` | 已删除 | 不得恢复泛化 maintenance coordinator。 |

## 3. 路径治理设计

### 3.1 目标模型

`runtime.paths` 删除后，不允许各模块散落实现：

```python
Path.home() / ".mote"
```

也不允许通过改名重新制造路径上帝模块。禁止以下名称和同类语义模块：

```text
runtime.config.locations
runtime.config.discovery
runtime.defaults
runtime.locations
runtime.discovery
```

替代模型分两层：

1. **`product.paths`** 决定默认根目录、`.mote` discovery 策略、优先级、Git root 截断、用户目录优先级和测试替换。
2. **Runtime capability 层**只接收已经解析好的 `Path`、领域 config 或窄 Protocol。

runtime 不能 import product。product/composition 可以 import runtime 并注入依赖。

### 3.2 `RuntimePaths` DTO

引入唯一的 product-owned immutable DTO：

```python
# product/paths.py
@dataclass(frozen=True)
class RuntimePaths:
    user_config_root: Path
    workspace_root: Path
    session_workspace_root: Path
    browser_profiles_root: Path
    sandbox_ca_root: Path
    secrets_root: Path
    oauth_root: Path
    package_data_root: Path
```

要求：

- DTO 只放在 `product.paths.RuntimePaths`。
- `.mote` discovery 实现只放在 `product.paths`。
- standalone/bootstrap 默认值只由 `product.paths.default_runtime_paths(...)` 创建。
- `product.container.ProductContainer.standard(...)` 接收或创建 `RuntimePaths`，并把字段注入 runtime/orchestration/product capabilities。
- runtime capability 不接收整个 DTO；只接收单个 `Path` 字段、领域 config 或 `ConfigSourceProvider`。
- `Path.home()` 只在 `product.paths.default_runtime_paths(...)` 中求值，不在 runtime/orchestration 模块 import 时求值。
- 测试必须能传入临时 `RuntimePaths` 或直接传入 capability path。
- embedded 调用方必须显式传入 `RuntimePaths` 或使用 `product.paths.default_runtime_paths(...)`。

默认值优先级：

1. 显式构造参数。
2. CLI/env/config 文件解析后传入 `product.paths.default_runtime_paths(...)` 的 overrides。
3. `product.paths.default_runtime_paths(...)` 内部默认值。

runtime package 内部不提供隐式 home-dir 默认 discovery。standalone runtime 必须经 `product.paths.default_runtime_paths(...)` + product/bootstrap factory 创建，不能绕回 runtime 默认值。

runtime 禁止：

- 定义用户配置根、workspace 根、browser profile 根、secret/oauth 根等产品默认值。
- 执行 `.mote` 向上搜索。
- 调用 git root 截断来决定 `.mote` discovery 边界。
- 计算用户目录优先级、项目目录优先级或 layered config 优先级。

### 3.3 `.mote` Discovery 和配置源

当前使用者不只 product，还包括 hook、MCP、permission、config、agent、orchestration 等。因此 discovery helper 不能简单搬到 product 后再让 runtime 调 product。

目标是注入 discovery 结果或窄 Protocol：

| 当前使用场景 | 新输入 | owner |
| --- | --- | --- |
| Markdown agents | resolved agent source dirs | `product.paths` -> product.agents |
| Skills | resolved skill source dirs | `product.paths` -> product.skills |
| CodeMap | `store_path` + language policy | `product.paths` -> product.code_map |
| Hook config | `ConfigSourceProvider` | `product.paths` provider 注入 runtime.hook |
| MCP config | `ConfigSourceProvider` | `product.paths` provider 注入 runtime.tools.mcp |
| Permission settings | `ConfigSourceProvider` | `product.paths` provider 注入 runtime.tools.permission |
| Agent file watching | watched files/dirs 列表 | `product.paths` -> runtime.agent adapter |
| orchestration stores | explicit workspace/store root | orchestration composition |

唯一 Protocol 位置：

```python
# contracts/ports/config_source_provider.py
class ConfigSourceProvider(Protocol):
    def files_for(self, name: str) -> Sequence[Path]: ...
```

Protocol 只表达“给我已解析路径”，不表达如何向上搜索 `.mote`。`product.paths.ProductConfigSourceProvider` 是默认实现。

### 3.4 C1a-C1e 自包含 Slices

| Slice | 范围 | 完成条件 | 验收 |
| --- | --- | --- | --- |
| C1a | product `.mote` discovery | agents、skills、code_map 的 discovery 由 `product.paths` 完成 | runtime 不 import project discovery helper；runtime 不实现 `.mote` upward walk |
| C1b | hook/MCP/permission config sources | runtime.hook、runtime.tools.mcp、runtime.tools.permission 接收 `ConfigSourceProvider` | 这些包不 import `runtime.paths`，不定义 layered config 优先级 |
| C1c | session/workspace/durable roots | session workspace root、durable root、store root 由 `RuntimePaths` 字段或构造参数注入 | runtime 不在模块 import 时求 `Path.home()`，不定义 product 默认根 |
| C1d | secrets/oauth/browser/sandbox paths | secrets、oauth、browser profile、sandbox CA path 由字段注入 | capability 不读取 `CONFIG_ROOT` |
| C1e | 删除旧入口 | 删除 `runtime.paths`，旧 import 归零 | AST import 检查通过，文件不存在，runtime 无 discovery/default roots 替代模块 |

## 4. Maintenance Gate 设计

### 4.1 不保留泛化 `MaintenanceCoordinator`

旧 `runtime.maintenance` 协调 repo scan 和 workspace cleanup 去重，但这两个动作的 owner、key 和生命周期不同。恢复一个泛化 coordinator 会再次形成 junk drawer。

目标拆分：

```text
runtime.code_map.scan_gate.CodeMapScanGate
runtime.session.workspace.cleanup_gate.WorkspaceCleanupGate
```

三阶段不新增公开的 generic `singleflight` primitive。两个 gate 先各自封装自己的去重状态；后续只有在出现第三个同构 gate 且能证明没有领域泄漏时，才单独写 ADR 讨论 `runtime.control.singleflight`。

### 4.2 生命周期和共享范围

| Gate | 去重范围 | Key | 释放时机 | 跨进程 |
| --- | --- | --- | --- | --- |
| `CodeMapScanGate` | process + workspace/repo | canonical repo root 或 explicit workspace id | scan task 完成、失败或取消后 `finally` 释放 | 不做跨进程；需要时另写 file lock ADR |
| `WorkspaceCleanupGate` | process + session workspace root | canonical session workspace root | cleanup task 完成、失败或取消后 `finally` 释放 | 不做跨进程；cleanup 本身必须幂等 |

共享方式：

- product/runtime composition root 构造 gate 实例。
- 同一 Engine/Role graph 内共享同一 gate。
- models context 不拥有 maintenance coordinator；需要 gate 的组件通过构造参数接收窄 gate。

实现要求：

- gate API 使用 async context manager 或 `try_acquire` handle，保证 cancellation 后释放。
- key 必须在进入 gate 前 canonicalize。
- gate 不 import agent、models、session 外的调用方；领域 gate 可以 import 自己 owner 内部类型。

## 5. Orchestration Tasks 边界

### 5.1 不整体迁入 runtime

`orchestration/tasks` 不应整体迁入 `runtime`。它的主体职责是后台任务和任务图编排，而不是 runtime primitive。

保留在 `orchestration.tasks`：

| 模块 | 保留理由 |
| --- | --- |
| `pool.py` | 管理后台任务生命周期、通知投递、pause/resume/cancel、结果消费，是任务编排语义。 |
| `bggraph/*` | 节点顺序、重试、暂停、恢复、路由、结果提交是工作流编排。 |
| `status.py` / `types.py` | task status、task meta、background notification 是任务域契约。 |
| `attachment.py` | 将 task 状态和输出组织成模型可见附件，是 task result presentation adapter。 |
| `promotion.py` / `decorators.py` | 将工具执行提升为后台任务，是 orchestration 策略。 |
| `stall_detector.py` | 监测后台任务停滞并发出 task notification，依赖 runtime scheduling primitive 但 owner 是 task orchestration。 |
| `turn_context_source.py` | 将后台任务状态注入 turn context，是 task orchestration 的上下文 adapter。 |
| `disk_output.py` | task output store，消费 `runtime.persistence` 和 `runtime.session.workspace`，但内容 owner 是 task。 |

不迁入 runtime 的原因：

- runtime 当前不依赖 `orchestration.tasks`，依赖方向健康。
- 若整体下沉，runtime 会拥有 workflow、任务图、Agent 唤醒和模型上下文回注策略，破坏 `contracts <- kernel <- runtime <- orchestration <- product`。
- `disk_output.py` 使用 runtime persistence/session workspace 是向下依赖，不构成下沉理由。

### 5.2 移出 Agent 绑定 adapter

需要调整的是 `orchestration/tasks/role_component.py` 和 `orchestration.tasks.__init__` 暴露的 `build_background_task_pool`。

当前问题：

- `role_component.py` 读取 `ctx.role`。
- 它绑定 `role.state.msg_buffer`、`role._capabilities`、`role.resource_registry`。
- 它构造 `TaskOutputStore` 并注册 task result resource。
- 这些都是 Coding Agent composition/wiring 语义，不属于 task orchestration core。

目标：

```text
product.agents.background_tasks
  build_background_task_pool(ctx)
```

同时修改底层契约，不再使用 `Callable[[object], BackgroundTaskService]`：

```python
# contracts/background_tasks.py
class TaskResultRegistry(Protocol):
    def register_task_result(self, task_id: str, content: str) -> None: ...
    def unload(self, task_id: str) -> None: ...

class SessionWorkspacePort(Protocol):
    def space(self, session_id: str, kind: str) -> Path: ...

@dataclass(frozen=True)
class BackgroundTaskBuildContext:
    message_sink: MessageSink
    wake: Callable[[], None]
    workspace: SessionWorkspacePort
    session_id: str
    result_registry: TaskResultRegistry

BackgroundTaskServiceFactory = Callable[[BackgroundTaskBuildContext], BackgroundTaskService]
```

要求：

- `runtime.agent` 从 Role 内部状态组装 `BackgroundTaskBuildContext`，但 product builder 不接收 Role、不接收裸 `ctx`。
- `product.agents.factory.CodingAgentFactory` 默认从 `product.agents.background_tasks` 注入 builder。
- `orchestration.tasks` 不再从 `__init__.py` 暴露 `build_background_task_pool`。
- `orchestration.tasks` core 不 import Role、不访问 `_capabilities`、不访问 `resource_registry`。
- `runtime.agent` 仍只依赖 `contracts.background_tasks.BackgroundTaskServiceFactory`，不得 import `orchestration.tasks`。
- product builder 只读取 `BackgroundTaskBuildContext` 字段，不读取 Role 私有成员。

验收：

- AST 证明 `runtime/` 不 import `mote.orchestration.tasks`。
- AST 证明 `orchestration/tasks` 不 import `mote.runtime.agent`。
- AST 证明 `product/agents/background_tasks.py` 和 `orchestration/tasks` 不访问 `role._capabilities`、`role.resource_registry`、`ctx.role`。
- type check 证明 `BackgroundTaskServiceFactory` 参数是 `BackgroundTaskBuildContext`。
- `product/agents/factory.py` 仍能默认装配 background task pool。

### 5.3 Public API 影响

`mote.orchestration.tasks.build_background_task_pool` 如果已经被外部使用，移出 `__init__.py` 是 breaking change。按本计划默认内部 API 可以直接删除；若产品 owner 判定它是外部 API，必须进入 release note 和迁移表。

## 6. Public API 与 Breaking Change

### 6.1 内部 API 假设

本计划默认 `runtime.*` 与 `orchestration.tasks` 的 wiring helpers 是项目内部 Python API，不对第三方插件承诺稳定导入路径。因此允许一次性删除旧入口，不保留 compatibility re-export。

实施前必须由产品 owner 确认该假设。如果存在外部插件、用户脚本或动态配置引用 `mote.runtime.*` 旧路径，则本计划变成 breaking change。breaking-change 决策必须在 T0 完成，不能等删除旧入口后补记录。

若判定为内部 API：

- T1/T2/T4 直接删除旧入口。
- 不保留 compatibility re-export。

若判定为外部 API：

- release note。
- 版本升级策略。
- 迁移表。
- 动态 import / pickle / 配置字符串扫描。
- 上述材料完成后，才能执行删除旧入口的阶段。

扫描范围还必须包含 `mote.orchestration.tasks.build_background_task_pool`。

### 6.2 禁止项

禁止：

- 旧路径 compatibility re-export。
- alias package。
- forwarding module。
- 空目录保留。
- “下一阶段再删”的 import shim。

允许：

- canonical package API。
- canonical API 必须位于新 owner。
- `__init__.py` 只暴露稳定公共类型，不为迁移隐藏内部模块路径。
- `__all__` 必须准确、可 import、可测试。

## 7. 验收设计

### 7.1 AST 架构测试是正式门禁

`rg` 可以作为人工迁移辅助，但不能作为唯一验收。正式门禁必须在 `ztest/architecture` 中实现 AST import 检查。

必须覆盖：

- absolute import。
- relative import 解析后的完整模块名。
- `TYPE_CHECKING` import。
- 顶层 `__all__` import smoke test。
- 文件/目录存在性。
- allowed dependency whitelist。
- runtime 禁止 `.mote` upward walk、git root 截断 discovery、用户目录优先级计算。
- runtime 禁止定义产品默认根目录常量，包括 user config root、workspace root、browser profile root、secrets/oauth root。
- runtime 禁止新增 `runtime.config.locations`、`runtime.config.discovery`、`runtime.defaults`、`runtime.locations`、`runtime.discovery` 等替代路径上帝模块。

动态 import 无法完全静态证明，必须单独扫描：

- `importlib.import_module`
- `__import__`
- pickle/dill/cloudpickle references
- 配置字符串中的模块路径

### 7.2 边界白名单

| 边界 | 允许依赖 | 禁止依赖 |
| --- | --- | --- |
| `runtime.persistence` | stdlib、contracts、runtime errors/process primitives | session、tools、artifacts、fileops、orchestration、product |
| `runtime.control` | stdlib、contracts、runtime errors/process primitives | agent、models、session、artifacts、product、orchestration |
| `runtime.telemetry` | contracts events/telemetry DTO、stdlib、observability SDK adapters | runtime.events event fabric、session、agent business logic |
| `runtime.events` | contracts/events、persistence primitives if needed | telemetry backends as required dependency |
| `runtime.session.workspace` | persistence primitives、session constants | tools/tasks private layout policy |
| `runtime.hook` / MCP / permission config | injected `ConfigSourceProvider` | `.mote` discovery implementation |
| `orchestration.tasks` | contracts、kernel、runtime primitives、orchestration internals | runtime.agent Role internals、product |
| `product.agents.background_tasks` | runtime.agent contracts、orchestration.tasks、runtime resources | 无；这是 product composition adapter |

### 7.3 文件存在性检查

这些检查应作为 architecture test 或 deterministic shell gate：

```bash
test ! -e runtime/paths.py
test ! -e runtime/workspace
test ! -e runtime/disk
test ! -e runtime/maintenance.py
test ! -e runtime/reconciliation.py
test ! -e runtime/completion
test ! -e runtime/logging
test ! -e runtime/observability
test ! -e runtime/scheduling
test ! -e orchestration/tasks/role_component.py
```

### 7.4 `rg` 的位置

`rg` 只用于迁移辅助，例如快速查旧字符串。不得把宽泛规则作为唯一验收，例如：

```bash
rg -n 'compat|deprecated|alias' runtime --glob '*.py'
```

该类规则容易误伤合法业务注释。旧路径检查必须用 AST import + 文件存在性。

## 8. 执行阶段

每个 T 阶段必须可独立提交，并且本阶段新增或修改的架构测试必须同阶段通过。T5 只做全量审计、补漏和记录，不首次建立核心门禁。

### T0：确认兼容性政策

范围：

- 确认 `runtime.*` 是否为内部 API。
- 确认 `mote.orchestration.tasks.build_background_task_pool` 是否为内部 wiring helper。
- 扫描插件、动态 import、pickle/序列化模块路径、配置字符串。
- 若属于外部 API，先完成 release note、版本策略和迁移表。

验收：

- 形成明确结论：`internal-delete` 或 `external-breaking-change`。
- 该结论记录在实施 PR/ADR 中。
- 没有 T0 结论，不允许执行 T1/T2/T4 的删除旧入口动作。

### T1：固化已完成迁移

范围：

- 确认 `runtime.workspace`、`runtime.disk`、`runtime.logging`、`runtime.observability`、`runtime.completion`、`runtime.scheduling`、`runtime/reconciliation.py` 不存在。
- 清理旧 import。
- 修正 canonical API 和 `__all__`。
- 同阶段增加旧入口文件存在性和旧 import AST 门禁。

验收：

- 文件存在性检查通过。
- AST 旧 import 检查通过。
- `__all__` import smoke test 通过。

### T2：替换 `runtime.paths`

范围：

- 在 `product.paths` 引入 `RuntimePaths`、`ProductConfigSourceProvider`、`default_runtime_paths(...)` 和 `.mote` discovery 实现。
- 在 `contracts/ports/config_source_provider.py` 引入 `ConfigSourceProvider`。
- 为 hook/MCP/permission 注入 `ConfigSourceProvider`。
- 为 session/durable/secrets/oauth/browser/sandbox 注入明确 path 字段。
- 删除 `runtime.paths`。
- 同阶段增加 path discovery/default roots AST 门禁。

验收：

- AST 证明 runtime 不 import `runtime.paths`。
- AST 证明 runtime capability 不 import product。
- `Path.home()` 不在 runtime 模块 import 时求值。
- AST 证明 runtime 不执行 `.mote` upward walk、git root 截断 discovery、产品默认根定义。
- 测试可传入临时 root。

### T3：拆掉 `runtime.maintenance`

范围：

- 若仍存在 repo scan 去重逻辑，引入 `CodeMapScanGate`。
- 若仍存在 workspace cleanup 去重逻辑，引入 `WorkspaceCleanupGate`。
- 确认 `runtime/maintenance.py` 不存在。
- 确认 models context 不持有泛化 maintenance coordinator。

验收：

- AST 证明没有 `runtime.maintenance` import。
- cancellation 测试覆盖 gate 释放。
- 同一 composition graph 内 gate 共享测试覆盖。

### T4：迁移 background task Agent adapter

范围：

- 在 `contracts/background_tasks.py` 引入 `BackgroundTaskBuildContext`、`TaskResultRegistry`、`SessionWorkspacePort`。
- 新增 `product.agents.background_tasks.build_background_task_pool`。
- 从 `orchestration/tasks/__init__.py` 移除 `build_background_task_pool`。
- 删除 `orchestration/tasks/role_component.py`。
- `product.agents.factory` 默认注入新的 product builder。
- `runtime.agent` 组装 typed `BackgroundTaskBuildContext`，不把 Role 或裸 `ctx` 传给 product builder。
- 同阶段增加 tasks adapter AST 门禁。

验收：

- AST 证明 `runtime/` 不 import `mote.orchestration.tasks`。
- AST 证明 `orchestration/tasks` 不访问 Role 私有能力或 resource registry。
- AST 证明 `product/agents/background_tasks.py` 不访问 `ctx.role`、`role._capabilities`、`role.resource_registry`。
- type check 证明 background task builder 只接收 `BackgroundTaskBuildContext`。
- `ztest/tasks`、`ztest/roles` 中相关用例更新到新 owner。

### T5：全量审计和补漏

范围：

- 运行全量 `ztest/architecture`。
- 审计 T1-T4 已新增的 persistence/control/telemetry/session workspace/path discovery/orchestration tasks 门禁是否覆盖 `TYPE_CHECKING`、相对 import 和 `__all__`。
- 执行动态 import 和配置字符串扫描。
- 补齐漏测，不首次建立核心边界规则。

验收：

- `python -B -m pytest ztest/architecture -q --tb=short`
- 受影响子系统测试按变更范围运行。

## 9. 最终判定标准

完成后必须满足：

- 每个 runtime 顶层入口都有明确 owner。
- 没有 `runtime.paths`。
- 没有顶层 `runtime.workspace`、`runtime.disk`、`runtime.maintenance`。
- 没有旧路径 compatibility re-export、alias、forwarding module。
- `runtime.events` 顶层保留，不进入 telemetry。
- `runtime.control` 只承载 control primitives。
- `runtime.persistence` 只承载 storage/path primitives。
- `runtime.telemetry` 只承载 logging/observability substrate。
- `runtime.session.workspace` 是同一 session footprint 的唯一布局和 lifecycle owner。
- runtime capability 不主动 discovery 产品 `.mote` 路径。
- `product.paths` 是默认根目录、`.mote` discovery 和配置源策略的唯一实现 owner。
- runtime 不定义产品默认根目录，不实现 discovery 替代模块。
- `orchestration.tasks` 保持编排 owner，不整体迁入 runtime。
- `build_background_task_pool` 位于 product composition，不在 `orchestration.tasks` public API。
- background task builder 只接收 `BackgroundTaskBuildContext`，不接收 Role 或裸 `ctx`。
- breaking-change 决策在删除旧入口前完成。
- AST 架构测试覆盖上述边界。
