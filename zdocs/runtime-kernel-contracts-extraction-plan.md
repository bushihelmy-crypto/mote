# Runtime 目标架构与原子迁移规范

本文定义 `runtime/` 与 `contracts`、`kernel`、`product` 的十年目标架构和原子迁移规范。目标不是搬目录，而是删除错误抽象、消除产品策略下泄、避免把历史债务固化到新层。

## 1. 判定标准

### 1.1 下沉到 `contracts`

只允许放：

- 跨层共享 DTO。
- ID、事件、错误、枚举。
- Protocol / port。
- 无 IO、无路径默认值、无 discovery、无 runtime adapter 的纯数据校验。

禁止放：

- 策略执行。
- 聚合逻辑。
- 文件/网络/进程 IO。
- 默认路径、`.mote` discovery、配置加载。
- hook/permission/tool/model 的 runtime adapter。

### 1.2 下沉到 `kernel`

只允许放：

- 单 Agent 执行语义。
- 模型无关、IO 无关的状态机、reducer、parser、prompt 纯算法。
- 不依赖 `runtime`、`orchestration`、`product`。

禁止放：

- EventFabric、session log、workspace、durable journal。
- tool execution、permission engine、MCP、sandbox。
- model client、routing IO、cost tracker、auth。
- hook manager、LSP、file watcher、artifact store。

## 2. 总体结论

| 类别 | 结论 |
| --- | --- |
| 大规模 runtime 下沉 | 不做。runtime 主体职责已经正确。 |
| contracts 补充 | 删除 stringly config source provider；重建 background task build context；重建 typed hook DTO；CAS 拆成中性 `ContentIdentity` 与 artifact locator。 |
| kernel 补充 | 做少量纯算法：reserved token escaping、token truncation、结构化 task result pointer renderer、compaction prompt formatting、context-window budget math；output/context 必须先完成前置审计。 |
| product 迁移 | 产品默认路径、`.mote` discovery、应用级 config root/loader/bootstrap/report、hook/MCP/permission 文件源、Agent file-watch wiring、Skills prompt adapter、human console fallback 应上移 product。 |

### 2.1 当前代码基线

本计划基于当前工作树，不按“全未开始”处理。状态定义：

- `Not started`：目标接口和目标 owner 均未落地。
- `Partially migrated`：目标 owner 已存在，但旧 owner 仍有实现或调用方。
- `Completed`：目标 owner 已存在，旧 owner 和旧 import 已清理。
- `Cleanup pending`：功能已经迁出，但旧常量、兼容入口、测试 import 或文档入口仍残留。

| 条目 | 当前状态 | 证据 | 下一步 |
| --- | --- | --- | --- |
| `product.paths.RuntimePaths` / `default_runtime_paths` | `Partially migrated` | `product/paths.py` 已存在；同文件仍保留 `CONFIG_ROOT`、`DEFAULT_WORKSPACE_ROOT` 等全局默认常量；`runtime/config/discovery.py`、`locations.py` 仍存在。 | 迁移所有调用方到 `RuntimePaths` 字段，删除 runtime 旧路径模块和 product 全局默认常量。 |
| `contracts.ports.ConfigSourceProvider` | `Cleanup pending` | `contracts/ports/config_source_provider.py` 已存在，但 `files_for(name: str)` 是过渡口，会把产品文件名协议泄漏到下层。 | 删除该 port；product config adapters 自行解析文件源，runtime 只接收领域 config；product 构造 runtime-owned watch subscriptions。 |
| `contracts.background_tasks.BackgroundTaskBuildContext` | `Partially migrated` | `contracts/background_tasks.py` 已存在；当前签名中 `wake` 允许 `None`，`TaskResultRegistry.unload()` 返回 `object`，workspace port 仍用 `kind: str`。 | 重建 build context：目的型 task output location、typed result registry、non-null wake port、无 Role 私有字段。 |
| product Skills implementation | `Partially migrated` | `product/skills/*` 已存在；`runtime/context/turn/sources/skill_*` 仍是 Skills prompt adapter。 | Skills per-turn prompt sources 上移 `product.skills.turn_context`。 |
| product Code Map policy | `Partially migrated` | `product/code_map/*` 已存在；`product/code_map/paths.py` 仍读取 `product.paths.CONFIG_ROOT`。 | 改为消费 `RuntimePaths.codemap_root`；runtime/code_map 保持 indexing capability。 |
| product config root/loader | `Not started` | `runtime/config/schema.py`、`loader.py`、`sources.py`、`layers.py` 仍是主实现。 | 整体迁 `product.config`，不再保留 runtime config engine。 |

## 3. Contracts 下沉结论

### C1：移除 `ConfigSourceProvider` 过渡口

当前来源：

- `runtime.hook.config_source`
- `runtime.tools.mcp.config_source`
- `runtime.tools.permission.settings_source`
- `runtime.config.discovery`
- `contracts.ports.config_source_provider`

问题：

- 这些 runtime 模块目前已经部分改为接收 `ConfigSourceProvider`，但该 port 的 `files_for(name: str)` 仍然暴露产品文件名协议。
- 按路径治理方案，`.mote` discovery 唯一 owner 应为 `product.paths`。
- runtime capability 应只接收已解析领域配置，不接收“配置文件查找服务”。

目标：

```text
删除 contracts/ports/config_source_provider.py
```

当前过渡接口：

```python
class ConfigSourceProvider(Protocol):
    def files_for(self, name: str) -> Sequence[Path]: ...
```

终态设计：

- `product.paths` 只服务 product composition 和 product config-source adapters。
- `product.config_sources.hooks` 输出 `HookConfig`。
- `product.config_sources.mcp` 输出 `list[MCPServerConfig]`。
- `product.config_sources.permissions` 输出 `PermissionConfig`。
- product composition 构造 runtime-owned `WatchSubscription` / `FileWatchRequest`，并订阅 runtime file-change event。
- runtime 不接收 `ConfigSourceProvider`，不调用 `files_for(name)`，不持有产品文件名。

归属理由：

- `hooks.json`、`mcp.json`、`settings.local.json`、`config.yaml`、`SKILL.md` 都是产品文件约定。
- 将“按名字取产品配置文件”放进 contracts，会长期冻结产品目录协议到下层。
- runtime watch request 比 generic provider 更稳定：runtime watcher 只知道“监听这些 path 并发布 typed change event”，不知道这些 path 为什么存在，也不执行产品 reload callback。

不下沉内容：

- `load_mote_json_section` 不进 contracts；它是产品配置读取 helper，放 `product.paths`。
- `.mote` upward walk、git root 截断、用户优先级不进 contracts。

验收：

- `contracts/ports/config_source_provider.py` 删除。
- runtime 不 import `runtime.config.discovery` 或 `product.paths` 来做 `.mote` discovery。
- runtime 不出现 `files_for("hooks.json")`、`files_for("mcp.json")`、`files_for("settings.local.json")` 这类 stringly config 调用。
- hook/MCP/permission 的文件名、layering、JSON section 读取逻辑不留在 runtime。
- runtime domain capabilities 最终接收解析后的 `HookConfig`、`list[MCPServerConfig]`、`PermissionConfig`。

### C2：`BackgroundTaskBuildContext` 终态重建

当前来源：

- `contracts/background_tasks.py`
- `orchestration/tasks/role_component.py`
- `product/agents/factory.py`

问题：

- `BackgroundTaskBuildContext` 已存在，`BackgroundTaskServiceFactory` 已不再是 `Callable[[object], ...]`。
- product builder 已不读取 Role。
- 剩余问题在 runtime agent adapter：`runtime/agent/components/action.py` 构造 context 时仍直接传入 `role._capabilities` 作为 `TaskResultRegistry`。
- `SessionWorkspacePort.space(..., kind: str)` 仍是 stringly typed workspace 协议，不能作为终态。
- 用公共 `SessionWorkspaceSpace` enum 也不是终态；它会把 ledger/tool_results/task_outputs 等无关领域集中到 contracts，形成中央枚举耦合。

目标：

```text
contracts/background_tasks.py
```

当前接口：

```python
class TaskResultRegistry(Protocol):
    def register_task_result(self, task_id: str, content: str) -> None: ...
    def unload(self, task_id: str) -> object: ...

class SessionWorkspacePort(Protocol):
    def space(self, session_id: str, kind: str) -> Path: ...

@dataclass(frozen=True)
class BackgroundTaskBuildContext:
    message_sink: MessageSink
    wake: Callable[[], None] | None
    workspace: SessionWorkspacePort
    session_id: str
    result_registry: TaskResultRegistry

BackgroundTaskServiceFactory = Callable[[BackgroundTaskBuildContext], BackgroundTaskService]
```

归属理由：

- 这是 runtime.agent 和 product builder 之间的跨层构建契约。
- contracts 可以依赖 `MessageSink` port，但不能依赖 concrete `SessionWorkspace`。
- product builder 只读取 typed context，不读取 Role。

不下沉内容：

- `BackgroundTaskPool` 不进 contracts；它是 orchestration implementation。
- `TaskOutputStore` 不进 contracts；它是 orchestration task output domain。

收敛要求：

- 删除跨层 `SessionWorkspacePort.space(...)`。
- `contracts/background_tasks.py` 定义目的型 port：

```python
class TaskOutputLocationPort(Protocol):
    def output_directory(self, session_id: SessionId) -> Path: ...

class TaskResultRegistry(Protocol):
    def register_task_result(self, task_id: TaskId, content: str) -> None: ...
    def unload(self, task_id: TaskId) -> TaskResultRecord | None: ...

class AgentWakePort(Protocol):
    def wake(self) -> None: ...

class DetachedTaskLifecycle(Protocol):
    def on_task_terminal(self, task_id: TaskId) -> None: ...

@dataclass(frozen=True, slots=True)
class BackgroundTaskBuildContext:
    message_sink: BackgroundMessageSink
    wake: AgentWakePort
    output_locations: TaskOutputLocationPort
    session_id: SessionId
    result_registry: TaskResultRegistry
```

- `runtime.session.workspace.SessionSpace` 留在 runtime.session.workspace 内部，作为 session layout owner 的内部 enum。
- `orchestration.tasks.disk_output.TaskOutputStore` 接收 `TaskOutputLocationPort` 或已解析 task output root，不知道 session workspace 的其他 spaces。
- interactive/background任务完成后需要唤醒 Agent 是 `BackgroundTaskBuildContext` 的不变量；禁止 no-op wake。
- 真正 detached、无需唤醒的后台任务必须使用单独的 `DetachedBackgroundTaskBuildContext`，不得复用 interactive context。
- product background task builder 只接收 `BackgroundTaskBuildContext`，不得读取 `Role`、`role._capabilities`、`role.resource_registry`、`role.state`。

验收：

- `BackgroundTaskServiceFactory` 不再是 `Callable[[object], ...]`。
- product builder 不访问 `ctx.role`、`role._capabilities`、`role.resource_registry`。
- contracts 不暴露 `SessionWorkspaceSpace` 或通用 `space(kind)`。
- `TaskResultRegistry.unload()` 返回 `TaskResultRecord | None`，不返回 `object`。
- `BackgroundTaskBuildContext.wake` 是 non-null `AgentWakePort`，无 no-op 实现。
- detached 背景任务使用独立 context/factory 类型。

### C3：Typed Hook DTO 与版本化 wire adapter

当前来源：

- `runtime/hook/types.py::HookInput`
- `contracts/hooks/types.py::HookOutcome` / `HookEvent` / `HookBehavior`

现状：

- `HookOutcome`、`HookEvent`、`HookBehavior` 已正确在 `contracts.hooks`。
- `runtime.hook.types.HookInput` 是外部 hook wire payload DTO，但它使用 `payload: dict`、裸字符串 event/permission mode、snake/camel 双字段，并在序列化时 merge payload。
- `EMPTY` 和 `fold()` 是 runtime aggregation 快路径和策略，必须留 runtime。

目标：

```text
contracts/hooks/invocation.py
  HookInvocation
  HookPayload
  typed event payloads

runtime/hook/wire.py
  HookWireSerializer
```

归属理由：

- contracts 只承载 typed internal hook invocation，不承载外部 wire 兼容格式。
- snake_case/camelCase 双写、外部协议版本、legacy 字段兼容只能存在于 runtime/product wire serializer。
- payload 不能覆盖 session/event/permission identity。

终态模型：

```python
@dataclass(frozen=True, slots=True)
class PreToolUseInvocation:
    session_id: SessionId
    permission_mode: PermissionMode
    payload: PreToolUsePayload

@dataclass(frozen=True, slots=True)
class StopInvocation:
    session_id: SessionId
    payload: StopPayload

HookInvocation = PreToolUseInvocation | StopInvocation | ...
```

每种 hook event 对应一个 invocation variant 和封闭 payload DTO。canonical internal invocation 不包含 `protocol_version`；版本解析止于 `HookWireSerializer`。

不下沉内容：

- `EMPTY` 留 runtime。
- `_behavior_rank` 留 runtime。
- `fold()` 留 runtime。
- hook command execution 留 runtime。
- legacy wire serializer 留 runtime hook adapter。

验收：

- `contracts.hooks` 不 import runtime。
- `runtime.hook.types` 只保留 aggregation/runtime helpers。
- contracts 不包含 `payload: dict` 的 hook envelope。
- `HookInvocation` 是判别联合，不允许 `event` 与 `payload` 分离组合。
- wire serializer 禁止 payload 覆盖 envelope 字段。
- snake/camel 双字段只出现在 runtime/product wire adapter。

### C4：中性 ContentIdentity 与 Artifact locator 拆分

当前状态：

- `BlobRef` 表达 `digest + size`，是内容寻址身份。
- `ArtifactContentRef` 表达 `content_ref + digest + size`，同时包含 artifact repository locator。
- `runtime.fileops.mutation.artifacts` 已通过 adapter 做二者转换。

判断：

- FileOps 与 artifacts 应共享中性内容身份值对象，而不是共享 artifact repository locator。
- 强制 FileOps 使用 `ArtifactContentRef` 会让 FileOps 事件、事务、快照永久携带 artifact repository 定位语义。

目标：

- `contracts.content.ContentDigest` 和 `contracts.content.ContentIdentity` 是跨 FileOps/artifacts 的唯一内容身份值对象。
- `contracts.artifacts.ArtifactContentRef` 拆成 `identity: ContentIdentity` + `locator: ContentLocator`。
- FileOps 事件、事务、snapshot 只使用 `ContentIdentity`。
- Artifact repository/publication 只在 artifact 边界使用 `ArtifactContentRef`。

归属理由：

- digest/size 是跨边界内容身份，属于 contracts。
- artifact locator 是 artifact repository 语义，不能进入 FileOps 领域事件。

不下沉内容：

- `ArtifactRepository` 留 runtime。
- GC、publication、resolver 留 runtime。

验收：

- `runtime.artifacts` 不 import `contracts.fileops`。
- `runtime.fileops` 不 import `contracts.artifacts.ArtifactContentRef`，只 import `ContentIdentity`。
- `ArtifactContentRef` 不再有裸 `digest` / `size` 字段，统一通过 `identity` 暴露。
- FileOps -> artifacts 的转换只存在于明确 adapter。

## 4. Kernel 下沉结论

### K1：Reserved model-token escaping

当前来源：

```text
runtime/prompt/sanitization.py
```

内容：

- `DANGEROUS_PATTERNS`
- `sanitize(text: str) -> str`

判断：

- 该模块是纯文本算法，无 IO、无 runtime import。
- 现有名称 `sanitize` 过宽，容易制造“完整安全清洗”的虚假保证。
- 终态只承认它是 reserved model token escaping，不承载信任边界策略。
- 真正的 prompt injection / trust boundary policy 由 product/security policy 决定。

目标：

```text
kernel/prompt/reserved_tokens.py
  escape_reserved_model_tokens(text: str) -> str
```

归属理由：

- kernel 已有 `kernel.prompt`。
- reserved token escaping 是 prompt assembly 的纯语义。
- runtime.prompt 不保留含混 `sanitization` facade。

验收：

- `kernel.prompt.reserved_tokens` 不 import runtime。
- `runtime.prompt.sanitization` 删除，不保留 compatibility re-export。
- 文档和 public API 不把该函数描述为完整 security sanitization。

### K2：Token truncation helper

当前来源：

```text
runtime/context/token_budget.py
```

内容：

- `count_tokens`
- `truncate_to_tokens`
- 依赖 `contracts.models.tokenization.count_string_tokens`
- 依赖 `contracts.text.Elision`

判断：

- 这是纯文本/token 预算算法，无 IO。
- 目前硬编码 `_TOKEN_MODEL = "gpt-4o"`，这是风险点。
- kernel 不应接收 `model: str` 再解析 tokenizer；模型名到 tokenizer/window 的映射属于 runtime/product 模型目录。

目标：

```text
kernel/prompt/token_budget.py
```

必要修改：

```python
class Tokenizer(Protocol):
    def count_text(self, text: str) -> int: ...

def count_tokens(text: str, *, tokenizer: Tokenizer) -> int: ...
def truncate_to_tokens(text: str, max_tokens: int, *, tokenizer: Tokenizer) -> str: ...
```

归属理由：

- token budget 是 prompt/context 纯算法。
- 具体 model name、provider tokenizer、fallback tokenizer 由 runtime/product 解析后注入。

不下沉内容：

- LLM compaction 调用留 runtime。
- history manager、spill、session persistence 留 runtime。
- `model -> tokenizer`、`model -> context_window`、`TOKEN_MAX` fallback 留 runtime/product。

验收：

- kernel 模块不含 `"gpt-4o"` 默认值。
- kernel 模块不接收 `model: str`。
- runtime.context 调用方显式传入 tokenizer policy。

### K3：结构化 TaskResultPointer renderer

当前来源：

```text
runtime/resources/task_pointer.py
```

内容：

- `build_task_result_pointer(...) -> str`
- 纯 XML-ish model-facing pointer 文本构造。
- 只依赖 `contracts.schema.PERSISTED_OUTPUT_OPEN_TAG` 和 XML escaping。

判断：

- 现实现把任务结果状态编码进英文文本，并通过 “Full output saved to:” 反解析路径。
- 现接口接收裸 `status: str`、`command_name: str`，以及可冲突的 `result` / `result_file` / `output_path` 参数组合。
- 不能把现有 builder 原样迁入 kernel；必须先删除文本反解析和非法组合。

目标：

```text
contracts/background_tasks.py
  CompletedInlineTaskResultPointer
  CompletedStoredTaskResultPointer
  FailedTaskResultPointer
  PausedTaskResultPointer
  TaskResultPointer

kernel/prompt/task_result_pointer.py
  render_task_result_pointer(pointer: TaskResultPointer) -> str
```

终态模型：

```python
@dataclass(frozen=True, slots=True)
class CompletedInlineTaskResultPointer:
    task_id: TaskId
    command_name: CommandName
    summary: str
    output: InlineTaskOutput

@dataclass(frozen=True, slots=True)
class CompletedStoredTaskResultPointer:
    task_id: TaskId
    command_name: CommandName
    summary: str
    output: StoredTaskOutput

@dataclass(frozen=True, slots=True)
class FailedTaskResultPointer:
    task_id: TaskId
    command_name: CommandName
    summary: str
    error: TaskFailure

@dataclass(frozen=True, slots=True)
class PausedTaskResultPointer:
    task_id: TaskId
    command_name: CommandName
    summary: str
    reason: PauseReason

TaskResultPointer = (
    CompletedInlineTaskResultPointer
    | CompletedStoredTaskResultPointer
    | FailedTaskResultPointer
    | PausedTaskResultPointer
)
```

`StoredTaskOutput` 不保存裸本地文件路径；它只保存 `ResourceRef`、`ArtifactRef` 或受控 opaque locator。

归属理由：

- 结构化 task result pointer 是 background task 和 prompt renderer 的跨层 DTO。
- kernel 只负责确定性渲染，不负责从旧文本反解析状态或路径。
- runtime.resources 继续拥有 ResourceRegistry、spill、resource units。

验收：

- 不存在从 `<persisted-output>` 或英文提示语反解析路径的 production code。
- builder 不接受互斥参数组合；非法组合由类型系统表达不出来。
- task 状态编码在 variant 类型中，不存在外层 `status` + 内层 `result` 双状态源。
- stored result 不泄漏绝对路径或 `file://`。
- product/orchestration 构造 `TaskResultPointer` DTO，kernel 只渲染。

### K4：Compaction prompt formatting

当前来源：

```text
runtime/context/history/prompt.py
```

内容：

- `get_compact_prompt`
- `get_partial_compact_prompt`
- `format_compact_summary`
- `get_compact_user_summary_message`

判断：

- 该模块只依赖 `kernel.prompt.compaction` 常量、`re` 和 `string.Template`。
- 无 IO、无 session、无 runtime event、无 LLM 调用。
- 语义是 compaction prompt 纯格式化，属于 kernel prompt。

目标：

```text
kernel/prompt/compaction_format.py
```

归属理由：

- kernel 已拥有 `kernel.prompt.compaction`。
- 这些函数是 prompt rendering 纯语义，应与 compaction prompt body 同层。

验收：

- `runtime.context.history.prompt` 删除。
- `kernel.prompt.compaction_format` 不 import runtime。

### K5：Context window budget pure math

当前来源：

```text
runtime/context/history/budget.py
```

内容拆分：

- 纯函数：`count_tokens`、`context_window`、`effective_window`、`autocompact_buffer`、`autocompact_threshold`、`evaluate`
- runtime wrapper：`TokenAccountant`

判断：

- 纯函数中不涉及 provider state 的阈值计算可下沉。
- 依赖 `TOKEN_MAX`、模型窗口默认值或 provider token accounting 的部分不能直接下沉。
- `TokenAccountant` 读取 `llm.cost_manager.last_usage`，依赖 runtime model client 状态，必须留 runtime。

目标：

```text
kernel/prompt/context_budget.py
```

必要抽象：

```python
@dataclass(frozen=True)
class ContextBudgetPolicy:
    context_window: int
    summary_reserve: int
    autocompact_threshold: int
```

kernel 只接收 `ContextBudgetPolicy` 和 token 数值，不解析 model name，不读取 `TOKEN_MAX`。`autocompact_threshold` 是单一真相源；ratio/buffer/default-window 计算留在 runtime/product policy resolver。

不变量：

- `0 < summary_reserve < context_window`
- `0 < autocompact_threshold <= context_window - summary_reserve`
- 所有 policy 构造入口必须验证以上条件。

保留：

```text
runtime/context/history/budget.py
  TokenAccountant
  model/window/tokenizer policy resolver
```

归属理由：

- context-window threshold math 是单 Agent prompt/context 纯语义。
- provider-reported token usage reader 是 runtime adapter。

验收：

- kernel budget 模块不 import runtime。
- `TokenAccountant` 继续留 runtime。
- kernel budget 模块不 import `contracts.models.tokenization.TOKEN_MAX`，不接收 `model: str`。
- invalid `ContextBudgetPolicy` 构造失败，不进入 runtime execution。

### K6：Output pure logic audit

当前状态：

- `kernel.output.engine.OutputStateMachine` 已存在。
- `runtime.output.engine.OutputEngine` 依赖 events、session fact sink、commit fence、runtime errors，是 runtime wrapper。

判断：

- 主体已经正确拆分。
- 不建议把 `runtime.output.engine` 继续下沉。

审计规则：

- 若 `runtime.output.engine._restore` 中存在不依赖 runtime errors/events/session fact sink 的纯 migration/provenance validation，则抽到 `kernel.output.restore`。
- 若纯函数抽取后仍需要 runtime errors/events/session fact sink，则不迁移。

当前结论：

- 未经 T8 前置审计批准，不迁移。

### K7：Context history pure reducers audit

当前风险面：

```text
runtime/context/history/*
runtime/context/compaction/*
```

判断：

- history manager、compaction engine、rehydrate、spill 和 transcript 与 runtime state、LLM、session/persistence 绑定，不能整体下沉。
- 只有纯 reducer / visibility / prompt formatting 算法可能进 kernel。

执行方式：

- 逐文件审计 imports。
- 只有不 import runtime、无 IO、无 LLM 调用、无 session/event 的函数才能迁。

当前结论：

- 不列入立即迁移，只作为 T7 audit。

## 5. 不应下沉的内容

| runtime 内容 | 不下沉理由 |
| --- | --- |
| `runtime.events` | EventFabric、dispatcher、journal、subscription 是 runtime event plane；contracts 只放 event schema。 |
| `runtime.session` | session log/replay/checkpoint/workspace 是持久化和恢复。 |
| `runtime.tools` | tool execution、permission、MCP、result persistence 是 runtime。 |
| `runtime.models` | clients、routing、auth、cost、ratelimit 是 runtime IO/resource 能力。 |
| `runtime.resilience` | admission/failover/retry/classification 是 runtime operational policy。 |
| `runtime.artifacts` | repository、publication、GC、resolver 是 runtime implementation。 |
| `runtime.fileops` | file mutation/read/search/review 是 workspace IO domain。 |
| `runtime.hook.manager` / command adapter | hook execution and aggregation runtime behavior。 |
| `runtime.config.discovery` / `locations` | 应迁 product.paths，不是 kernel/contracts。 |
| `runtime.session.workspace` | session footprint layout 和 cleanup lifecycle owner。 |
| `runtime.lsp` | runtime capability，含 server/process/protocol state。 |
| `runtime.sandbox` / `secrets` / `interactive` | IO、安全、进程和外部系统能力。 |

## 5.1 Runtime Package 妥协清算

之前 runtime-package 计划里为了降低迁移风险保留过若干过渡设计。按“十年零负债”目标，本文件将它们改成终态要求。

### 必须推翻的过渡设计

| 旧妥协 | 终态 |
| --- | --- |
| `runtime.config` retain，作为配置读取和 layering owner | 推翻。应用配置中心整体归 `product.config`；runtime 不保留 config package。 |
| `ConfigSourceProvider.files_for(name: str)` 作为跨层 port | 推翻。删除该 port；product 解析文件源并构造 runtime-owned watch subscriptions。 |
| hook/MCP/permission runtime loader 接收 provider | 推翻。product config-source adapter 输出领域配置，runtime execution 只接收领域配置。 |
| runtime agent watcher 通过 provider 取配置文件 | 推翻。product 构造 `WatchSubscription`，runtime watcher 只发布 file-change event；product reload coordinator 执行 reload。 |
| `SessionWorkspacePort.space(session_id, kind: str)` | 推翻。不使用公共 session space enum；改为目的型 `TaskOutputLocationPort`。 |
| `product.paths.CONFIG_ROOT` 等全局默认常量 | 推翻。默认值只由 `default_runtime_paths(...)` 创建并显式注入。 |
| `runtime.config.locations` / `runtime.config.discovery` 作为 runtime.paths 替代物 | 推翻并禁止。路径 discovery 只能在 product paths。 |
| `runtime.persistence.paths` 承载默认根目录或领域 layout | 禁止。persistence 只放 path-safe/atomic IO primitive。 |
| `runtime.control.singleflight` 预留为泛化协调器 | 不建。只有出现第三个同构 gate 且 ADR 证明无领域泄漏时才允许。 |
| `runtime.reporting.py` 暂不并入 telemetry | 推翻。拆分为 `runtime.telemetry.reporting` substrate 和领域 owner report adapter；删除含混顶层文件。 |
| `runtime.media` 暂不合并 | 推翻。保留 `runtime.media.video` 作为本地视频分解 runtime capability；产品展示、CLI error、surface adapter 上移 product。 |

### 保持的最终边界

| 边界 | 终态 |
| --- | --- |
| `runtime.events` 不进 telemetry | 保持。它是事件投递平面，不是 observability backend。 |
| `runtime.telemetry` | 只承载 logging/observability/reporting substrate，不承载 event fabric 和 human console input。 |
| `runtime.persistence` | 只承载 disk/storage/atomic/path-safe primitive，不承载 session layout、workspace cleanup、durable/ledger 领域语义。 |
| `runtime.control` | 只承载 lifecycle、leases、scheduling 等 runtime control primitive，不吸收 maintenance/completion/reconciliation。 |
| `runtime.session.workspace` | 是 session footprint layout 和 cleanup lifecycle 的唯一 owner。 |
| `runtime.durable` / `runtime.ledger` | 保持领域顶层，除非完成语义拆分 ADR 并证明依赖边减少。 |
| `runtime.completion` | 不做顶层；Agent completion policy 归 `runtime.agent.completion`。 |
| `runtime.reconciliation` | 不做顶层；failure classification 归 `runtime.resilience`，artifact/durable 恢复归各 owner。 |
| `orchestration.tasks` | 不整体下沉 runtime；Agent 绑定 adapter 上移 product composition。 |

## 6. Product 上移结论

这些内容不应下沉 `kernel/contracts`。它们属于产品策略、应用级 composition、用户文件约定或人机界面，应上移 `product`，runtime 只保留 capability implementation 和注入端口。

### P1：产品路径与 `.mote` discovery

当前来源：

```text
runtime/config/discovery.py
runtime/config/locations.py
```

内容：

- `CONFIG_ROOT = Path.home() / ".mote"`
- `DEFAULT_WORKSPACE_ROOT`
- `SOURCE_ROOT`
- `MOTE_DIR_NAME`
- `browser_profiles_dir`
- `mote_project_dirs`
- `mote_project_files`
- `mote_layered_files`
- `mote_source_dirs`
- `load_mote_json_section`

目标：

```text
product/paths.py
  RuntimePaths
  default_runtime_paths(...)
  mote_project_dirs(...)
  mote_project_files(...)
  mote_layered_files(...)
  load_mote_json_section(...)
```

归属理由：

- `.mote` 是 Mote 产品约定，不是 runtime primitive。
- Git root 截断、用户目录优先级、project/workdir layering 是产品策略。
- runtime capability 不能主动 discovery 产品路径。

runtime 保留：

- capability 构造参数。
- capability 构造器接收显式 `Path` 或领域 config。
- runtime watcher 接收 `WatchSubscription` / `FileWatchRequest`，不接收配置 discovery provider。
- runtime capability 不接收完整 `RuntimePaths` DTO；product composition 只传 capability-specific path value object 或具体 `Path`。

验收：

- runtime 不定义 `CONFIG_ROOT`、`WORKSPACE_ROOT`、`SOURCE_ROOT`。
- runtime 不执行 `.mote` upward walk。
- runtime 不导入 `product.paths`；product/composition 注入字段。

### P2：应用级 config root、source discovery、loader、watcher、report

当前来源：

```text
runtime/config/schema.py
runtime/config/sources.py
runtime/config/loader.py
runtime/config/watcher.py
runtime/config/bootstrap.py
runtime/config/report.py
runtime/config/env.py
runtime/config/overrides.py
runtime/config/secrets.py
runtime/config/layers.py
runtime/config/diagnostics.py
```

目标：

```text
product/config/schema.py
product/config/sources.py
product/config/loader.py
product/config/watcher.py
product/config/bootstrap.py
product/config/report.py
product/config/env.py
product/config/overrides.py
product/config/secrets.py
product/config/layers.py
product/config/diagnostics.py
```

runtime 不保留：

```text
runtime/config/*
```

判断：

- `runtime/config/layers.py` 当前只服务应用配置 loader/tests；没有独立 runtime capability consumer。
- `CREDENTIAL_DENYLIST`、trusted/untrusted filtering、profile/managed precedence 都是产品配置安全策略。
- 因此不做“runtime generic config engine”；整体归 `product.config` 更内聚。

归属理由：

- `Config` 聚合 models、tools、context、multimodal、observability、ui、secrets、workspace、resilience、router，是应用级产品配置根。
- source discovery 包含 `/etc/mote`、`~/.mote`、`<cwd>/.mote`、profile、CLI flags、env precedence，是产品启动策略。
- bootstrap 创建 `config.yaml`、`mcp.json`、`hooks.json`、`secrets_config.json`、`skills/`，明显是 first-run product scaffolding。
- report 是 CLI/human-readable config dump，不是 runtime capability。

不移动到 contracts：

- `contracts/config/*` 已承载各子配置 DTO，继续保留。
- 应用级 `Config` root 不进 contracts；它聚合产品选项和启动策略。

验收：

- `python -m mote.runtime.config.report` 不再是入口。
- product CLI 使用 `product.config.report`。
- runtime capability 接收已解析 config 子对象，不调用 product config loader。
- 全仓不存在 `mote.runtime.config.*` import，`ztest/config` 全部改测 `product.config`。

### P2.1：Product config 调用方闭包

当前 runtime/orchestration 直接依赖 `runtime.config` 的调用方必须逐项迁移，不能靠最后批量搜索兜底。

| 当前调用方 | 当前依赖 | 目标 |
| --- | --- | --- |
| `product/cli/backend.py` | `runtime.config.loader.load_config` | 改为 `product.config.loader.load_config`。 |
| `runtime/models/clients/context.py` | `runtime.config.loader` / `schema.Config` | 不再加载全局 config；由 product/model integration 传入 model client context 或子配置。 |
| `runtime/agent/runtime_maintenance.py` | `load_config` / `discover_source_files` | reload 动作上移 product agent maintenance；runtime maintenance 只暴露 runtime reload hooks。 |
| `runtime/agent/components/integrations.py` | `discover_source_files` | 由 product composition 计算 config source roots 并注入 watcher roots。 |
| `runtime/session/log.py`、`runtime/agent/role_state.py`、`orchestration/environment/*` | `WORKSPACE_ROOT` | 接收 `SessionWorkspace`/workspace root path；不读取 default root。 |
| `runtime/fileops/locking.py` | `CONFIG_ROOT` | 构造器接收 lock root。 |
| `runtime/sandbox/network/tls.py`、permission sandbox adapter | `CONFIG_ROOT` / `browser_profiles_dir` | 构造器接收 sandbox CA root、browser profile root、read-only mask。 |
| `runtime/models/auth/oauth/*` | `CONFIG_ROOT` | OAuth store/manager 接收 oauth root。 |
| `runtime/models/failover/model_journal.py`、`runtime/resilience/failover/operator.py`、`runtime/service_gateway/journal.py` | `WORKSPACE_ROOT` | 构造器接收 journal root。 |

配置 reload 责任：

- `runtime.watching` 只发布 typed file-change event。
- `product.reload.ReloadCoordinator` 负责 debounce、串行化、失败隔离和 reload lifecycle。
- `product.config.loader` 产出不可变 `ConfigSnapshot(version, config, provenance)`。
- `product.container` 基于 snapshot 构建新的 capability graph generation。
- 新 generation 完整验证和启动成功后，product 通过原子引用切换 active generation。
- 旧 generation drain 后统一 close；失败时保留旧 generation，不做半更新。
- `ConfigSnapshot` 必须深不可变：内部 list/dict 转换为 tuple/mapping proxy/immutable model，或在构造时 deep copy 后冻结。
- 新 generation 启动失败：丢弃新 generation 并保留旧 generation。
- drain 超时：停止接收新请求，记录诊断并强制 close 到隔离错误通道。
- close 失败：聚合错误并上报，但不得回滚到半切换状态。
- runtime capability 不提供 ad-hoc `apply_config(...)`；需要配置变化的 capability 通过 generation 重建获得一致快照。
- runtime 不持有 app config cache，不主动调用 config loader。

### P3：Hook / MCP / Permission 的产品配置文件源

当前来源：

```text
runtime/hook/config_source.py
runtime/tools/mcp/config_source.py
runtime/tools/permission/settings_source.py
```

目标：

```text
product/config_sources/hooks.py
product/config_sources/mcp.py
product/config_sources/permissions.py
```

runtime 改造：

- `runtime.hook` 接收合并后的 `HookConfig`。
- `runtime.tools.mcp` 接收 `list[MCPServerConfig]` 和 OAuth runtime config。
- `runtime.tools.permission` 接收合并后的 `PermissionConfig`。
- `product.config_sources.*` 负责读取、合并、校验产品配置文件；runtime execution 层不接收文件列表 provider。

归属理由：

- `hooks.json`、`mcp.json`、`settings.local.json` 的文件名、layering、ecosystem compatibility 都是产品配置约定。
- runtime hook/MCP/permission 是执行能力，不应知道 `.mote` 文件发现策略。

保留 runtime：

- hook manager、command handler、parser。
- MCP lifecycle、adapter、OAuth runtime。
- permission classifier、engine、sandbox guard。

验收：

- runtime 不 import `mote_layered_files` / `mote_project_files`。
- runtime config source modules 删除，不保留 injected-provider adapter。

### P4：Agent file-watch wiring

当前来源：

```text
runtime/agent/components/watching.py
```

问题：

- 该模块决定监听 `SKILL.md`、`config.yaml/config2.yaml`、`mcp.json`。
- 它读取 `.mote` project files、`CONFIG_ROOT`、MCP 文件名、CodeMap registered extensions。
- 这些是 product skills、product config、product MCP config、product code map wiring 语义。

目标：

```text
product/agents/watching.py
  build_agent_watch_subscriptions(...)
  AgentReloadCoordinator

runtime/watching/
  WatchSubscription
  FileWatchRequest
  FileChangedNotification
```

runtime 保留：

- `runtime.watching.FileWatchService`。
- runtime-owned `WatchSubscription` / `FileWatchRequest` DTO。
- typed file-change publication，不执行产品 reload callback。

归属理由：

- 文件 watcher runtime 能力可以保留。
- “监听哪些产品文件并触发 reload_skills/reload_mcp/reindex_code_map”是 Coding Agent product composition。
- debounce、串行化、失败隔离属于 product reload coordinator，不属于 runtime watcher。

验收：

- `runtime.agent.components.watching` 不 import `runtime.config.discovery`、MCP config source、code map product policy。
- product.agents 构造 runtime-owned watch subscriptions。
- runtime.watching 不 import product，不持有 reload callbacks。

### P5：Human console fallback 和 approval prompt rendering

当前来源：

```text
runtime/telemetry/logging/human_input.py
runtime/tools/permission/prompts.py
```

目标：

```text
product/cli/io/human_input.py
product/cli/surfaces/approval_prompt.py
```

runtime 保留：

- `contracts.ports.human_interaction.HumanInteractionPort`。
- `contracts.permissions.ApprovalRequest` / `ApprovalChoice`。
- permission engine 的 structured request/decision flow。

归属理由：

- `input(prompt)` 是 console product fallback，不是 telemetry substrate。
- English approval prompt wording 和 free-text parsing 是 human-facing product surface。
- runtime permission engine 应只发 structured request。

验收：

- `runtime.telemetry.logging` 不导出 `get_human_input`。
- runtime permission engine 不依赖 prose prompt renderer。

### P6：Skills per-turn prompt adapters

当前来源：

```text
runtime/context/turn/sources/skill_listing.py
runtime/context/turn/sources/skill_activation.py
```

目标：

```text
product/skills/turn_context.py
  SkillListingContextSource
  SkillActivationContextSource
```

runtime 改造：

- `runtime.context.turn` 保留 `TurnContextBus`、formatting、source protocol 和通用 sources。
- `runtime.agent.components.context` 不再直接 import/construct skill-specific sources。
- product agent composition 根据 `SkillService` 构造 skills turn sources 并注入 bus。
- `contracts/ports/skills.py::SkillPromptProvider` 保持窄接口；product 内部 source 可以读取 concrete `SkillInjector`，runtime 不读取 `_index_skills` 私有方法。

归属理由：

- Skills 已经是 product capability：definition、discovery、audit、pool、injector 均在 `product/skills`。
- `SkillListingContextSource` 和 `SkillActivationContextSource` 输出的是模型可见 Skills 使用语义，包含 `Skill(...)` 调用文案、路径触发规则、index 增量策略。
- 这不是通用 turn-context substrate；它是 product Skills 的 prompt adapter。

保留 runtime：

- `runtime.context.turn.bus.TurnContextBus`。
- 与 Skills 无关的 runtime sources：token pressure、changed files、compaction notice、git state、tool catalog、team roster 等。
- skill tool execution capability 仍通过 `contracts.ports.skills.SkillCatalog` 获取最小视图。

验收：

- `runtime/context/turn/sources/` 不包含 `skill_*` 模块。
- runtime 不 import `product.skills`。
- runtime 不访问 `SkillInjector._index_skills` 或其他 product private member。
- product composition 将 skills context source 注入 `TurnContextBus`。

### P7：Code Map product policy already belongs in product; runtime implementation 不上移

当前状态：

```text
product/code_map/factory.py
product/code_map/paths.py
product/code_map/turn_context.py
runtime/code_map/*
```

判断：

- `runtime/code_map` 的 tree-sitter、SQLite store、extractor、language provider、scan gate 是 indexing runtime capability。
- `product/code_map/paths.py` 已拥有 `~/.mote/codemap/<repo-hash>/codemap.db` 路径策略。
- `product/code_map/factory.py` 已拥有默认排除目录、enabled extensions 和 turn-context adapter 装配。
- `product/code_map/turn_context.py` 已拥有模型可见 Code Map prompt adapter。

必要收敛：

- `runtime.code_map.RepoIndexer` 继续只接收 `store_path`、`enabled_extensions`、`excluded_directories`。
- `runtime.code_map` 禁止 import `product.paths`、`CONFIG_ROOT` 或执行 `.mote/codemap` 默认路径推导。
- product code map path policy 改用 `RuntimePaths.codemap_root`，不直接读取全局 `CONFIG_ROOT` 常量。

归属理由：

- Code Map 的索引执行是 runtime capability。
- “索引文件放在 `~/.mote/codemap`、默认跳过哪些产品目录、何时把结构图推给模型”是 product policy。
- 当前边界基本正确，目标是固定该边界，不做 runtime/code_map 整体搬迁。

验收：

- `runtime/code_map` 不出现 `CONFIG_ROOT`、`Path.home()`、`.mote/codemap`。
- Code Map store path 只由 product factory/composition 传入。
- Code Map turn-context source 继续位于 product。

### P8：注入 capability-specific path value，不移动实现的 runtime capabilities

以下模块不应上移 product；只需移除产品默认路径，改为接收 `product.paths.RuntimePaths` 字段：

| 模块 | 保留 runtime 理由 | product 注入内容 |
| --- | --- | --- |
| `runtime.secrets.store` / `cipher` | vault、cipher、TOTP 是 runtime security capability | `secrets_root`、vault key path、named secret file path |
| `runtime.sandbox.network.tls` | CA 生成和 sandbox TLS 是 runtime security capability | `sandbox_ca_root` |
| `runtime.interactive.browser.profile` | browser profile store 是 interactive runtime capability | `browser_profiles_root` |
| `runtime.tools.permission.sandbox.adapter` | sandbox permission adapter 是 runtime enforcement | read-only mask paths、CA/profile paths |
| `runtime.service_gateway.journal` | service call journal 是 runtime service gateway persistence | journal root |
| `runtime.fileops.locking` | file lock runtime coordination | lock root |
| `runtime.agent.role` logging bind path | role runtime logging | logs root |

这些不属于 product，因为实现涉及安全、IO、runtime state 或 capability lifecycle。product 只决定默认路径。

## 7. 统一迁移 DAG

每个 slice 必须原子完成：更新所有调用方、删除旧入口、补齐测试，提交点保持可运行。不设置 compatibility re-export、alias 或 forwarding package。

### T0：基线与门禁

实施：

1. 扩展架构测试：`contracts` 不 import `kernel/runtime/orchestration/product`；`kernel` 不 import `runtime/orchestration/product`；runtime 不 import product。
2. 增加 AST import 规则，覆盖 TYPE_CHECKING import、相对 import 和 `__all__` canonical API。
3. 记录当前妥协残留调用方清单：`runtime.config.*`、`runtime.config.discovery/locations`、`product.paths.CONFIG_ROOT`、`ConfigSourceProvider`、`space(..., kind: str)`、`runtime/context/turn/sources/skill_*`、runtime product file-name constants。

验收：

- 旧入口清单和调用方清单可由脚本复现。
- 架构测试先失败于已知违规，后续 slice 逐项消除。

### T1A：Content identity schema migration

实施：

1. 新增 `contracts.content.ContentDigest`、`DigestAlgorithm`、`ContentIdentity`。
2. `ContentDigest` 固定算法枚举、hex/base encoding、大小写规范化和验证规则。
3. `contracts.artifacts.ArtifactContentRef` 改为 `identity: ContentIdentity` + opaque `ContentLocator`。
4. FileOps 事件、事务、snapshot schema 从 `BlobRef` 迁到 `ContentIdentity`。
5. 保留明确 adapter 读取旧 journal/session schema，读入后转换为 `ContentIdentity`；adapter 删除条件写入本阶段验收。

持久化策略：

- 需要 schema version bump。
- 旧 session/journal 必须可读；读取时做 in-memory migration，不做 compatibility re-export。
- 不做离线强制迁移；开发期数据可通过显式 cleanup 命令丢弃，但 production reader 不能崩溃。
- 旧 `BlobRef` reader adapter 在两个连续 minor release 或项目指定 cutoff 后删除，删除前 architecture test 禁止新增写入。

验收：

- FileOps production write path 不再写 `BlobRef`。
- Artifact repository write path 只在 artifact 边界创建 `ArtifactContentRef`。
- `ContentLocator` 是 opaque locator，禁止绝对路径和 `file://`。
- schema migration golden tests 覆盖旧 BlobRef record。

### T1B：Contracts 与构造 seam 对齐

实施：

1. 删除 `ConfigSourceProvider`；product config-source adapters 直接消费 `product.paths` discovery 结果。
2. 保留 `BackgroundTaskBuildContext`，但收紧依赖：不暴露 Role 私有结构，不暴露裸 workspace kind。
3. 新增目的型 `TaskOutputLocationPort`，替代 `space(..., kind: str)`。
4. 拆分 interactive 与 detached background task context，禁止 no-op wake。

验收：

- contracts 不包含 runtime strategy。
- contracts 不包含 product config file discovery provider。
- product background task builder 不读取 Role 私有字段。
- runtime agent adapter 不直接传 `role._capabilities`；通过 typed `TaskResultRegistry` component/port 注入。
- contracts 不暴露 `SessionWorkspacePort.space`。
- interactive background task context 必须注入真实 `AgentWakePort`。

### T1C：Typed hook invocation migration

实施：

1. 新增 `contracts/hooks/invocation.py`，定义 `HookInvocation` 判别联合和每个 event 的 payload DTO。
2. `runtime.hook.wire.HookWireSerializer` 负责协议版本、snake/camel legacy 字段和外部 dict payload。
3. 删除旧 `HookInput` 作为 core DTO 的用途；只允许 wire adapter 内部读取旧 wire shape。
4. 更新 hook manager、hook runner port、tests，确保 payload 无法覆盖 envelope。

验收：

- hook 旧 `HookInput` 不进入 contracts。
- `HookInvocation` 没有外层 `event` + 开放 `payload` 组合。
- wire serializer golden tests 覆盖 legacy snake/camel wire。
- hook 反向 type import 被架构测试覆盖。

### T1D：Structured task-result protocol migration

实施：

1. 新增 task result pointer 判别联合 DTO。
2. 更新 background task producer、TaskOutputStore、ResourceRegistry 注册路径，产出结构化 DTO。
3. 更新 session/event/persistence schema，记录结构化 task result pointer 或 opaque locator。
4. 删除从 `<persisted-output>` 或英文提示语反解析路径的代码。
5. `kernel.prompt.task_result_pointer` 只渲染结构化 DTO。

持久化策略：

- 需要 schema version bump。
- 旧 session 中的文本 pointer 只在 legacy reader 中 best-effort 转换；新写入禁止文本反解析。
- legacy reader 有明确删除 cutoff；删除前 architecture test 禁止新增 legacy writer。

验收：

- `TaskResultPointer` variant 不存在外层 status。
- stored result 使用 `ResourceRef` / `ArtifactRef` / opaque locator，不使用绝对路径。
- task pointer renderer golden tests 覆盖 completed inline、completed stored、failed、paused。

### T2：Product paths 与默认根目录倒置

实施：

1. `product.paths.RuntimePaths` 增加缺失字段：`codemap_root`、`logs_root`、`file_locks_root`、service/model journal roots。
2. 删除 `product.paths.CONFIG_ROOT`、`DEFAULT_WORKSPACE_ROOT`、`SERDESER_PATH` 等全局默认常量；默认值只由 `default_runtime_paths(...)` 创建。
3. runtime/session/orchestration/secrets/sandbox/oauth/fileops/service_gateway/model failover 统一接收显式 root/path 或 session workspace port。
4. 删除 `runtime/config/discovery.py`、`runtime/config/locations.py`，全仓 import 原子更新。

验收：

- runtime 不出现 `Path.home()` + `.mote` 默认根组合。
- runtime 不执行 `.mote` upward discovery、git-root 截断、用户/项目优先级计算。
- session/workspace/root 相关行为测试通过。

### T3：Product config center 迁移

实施：

1. `runtime/config/*` 整体迁到 `product/config/*`。
2. product config loader 继续产出 `Config` 和 provenance；runtime 只接收 contracts/config 子配置或显式构造参数。
3. `product.container` 成为 config loading/cache owner；`product.reload.ReloadCoordinator` 成为 reload owner。
4. `runtime.models.clients.context`、`runtime.agent.runtime_maintenance`、`runtime.agent.components.integrations` 等调用方按 P2.1 闭包迁移。

验收：

- 全仓不存在 `mote.runtime.config.*` import。
- `ztest/config` 改测 `product.config`。
- config precedence、managed/profile/env/CLI/programmatic 顺序 golden tests 通过。
- trusted/untrusted credential filtering golden tests 通过。
- config reload 通过 `ConfigSnapshot` generation 原子切换；不存在 ad-hoc `apply_config(...)`。

### T4：Product config-source adapters

实施：

1. `runtime/hook/config_source.py` -> `product/config_sources/hooks.py`。
2. `runtime/tools/mcp/config_source.py` -> `product/config_sources/mcp.py`。
3. `runtime/tools/permission/settings_source.py` -> `product/config_sources/permissions.py`。
4. runtime hook/MCP/permission execution 接收解析后的 `HookConfig`、`list[MCPServerConfig]`、`PermissionConfig`。

验收：

- runtime 不包含 `hooks.json`、`mcp.json`、`settings.local.json` 文件发现策略。
- `.mote` upward discovery 与 git-root 截断测试覆盖 `product.paths`。
- hook/MCP/permission loader 行为 golden tests 迁到 product。

### T5：Agent product composition

实施：

1. `runtime/agent/components/watching.py` 的产品 watch manifest -> `product.agents.watching` reload coordinator。
2. `runtime/context/turn/sources/skill_listing.py`、`skill_activation.py` -> `product.skills.turn_context`。
3. Code Map path policy 改为 `RuntimePaths.codemap_root`；runtime/code_map 继续只接收 `store_path`。
4. human input fallback 和 approval prose prompt -> product CLI surface。

验收：

- watcher event flow 测试覆盖 config、MCP、Skills、Code Map file changes。
- product reload coordinator 测试覆盖 debounce、串行化、失败隔离、generation 切换和旧 generation drain。
- runtime watcher 接收 `WatchSubscription` / `FileWatchRequest`，发布 `FileChangedNotification`，不接收 config source provider，不执行 reload callback。
- Skills prompt rendering snapshot tests 覆盖 full index、incremental index、path activation。
- Code Map runtime 包不含产品默认路径。
- runtime permission 不含 human prose rendering。

### T6：Runtime package 妥协清理

实施：

1. `runtime.reporting.py` 拆分：substrate 进入 `runtime.telemetry.reporting`，领域 report adapter 回到对应 owner，删除顶层 `runtime/reporting.py`。
2. `runtime.media` 拆分：保留 `runtime.media.video` 的本地视频分解能力；产品展示、CLI surface、human-facing error 上移 product。
3. 确认 `runtime.control` 只包含 lifecycle、leases、scheduling；不得新增 `singleflight`、maintenance、completion、reconciliation。
4. 确认 `runtime.persistence` 只包含 storage/path-safe/atomic IO primitive；不得包含 session layout、workspace cleanup、durable/ledger 领域语义。

验收：

- 顶层 `runtime/reporting.py` 不存在。
- `runtime.media` 的 public API 只暴露 `runtime.media.video` 本地视频分解 capability。
- architecture tests 固化 `runtime.control`、`runtime.persistence` 的允许依赖白名单。
- 无 `runtime.maintenance`、`runtime.completion`、`runtime.reconciliation` 顶层入口或兼容转发。

### T7：Kernel prompt/token 纯逻辑迁移

实施：

1. `runtime/prompt/sanitization.py` -> `kernel/prompt/reserved_tokens.py`，API 命名为 `escape_reserved_model_tokens`。
2. `runtime/context/token_budget.py` -> `kernel/prompt/token_budget.py`，接口接收 `Tokenizer`，不接收 model name。
3. T1D 完成后，将结构化 task result pointer renderer 放入 `kernel/prompt/task_result_pointer.py`；不得移动旧文本 builder。
4. `runtime/context/history/prompt.py` -> `kernel/prompt/compaction_format.py`。
5. `runtime/context/history/budget.py` 中 policy math -> `kernel/prompt/context_budget.py`，接口接收 `ContextBudgetPolicy` 和数值 token count。

验收：

- kernel 不 import runtime，不读取 model catalog，不 import `TOKEN_MAX`。
- token truncation Unicode、XML escaping、极小预算边界测试通过。
- compaction threshold 边界测试通过。
- task pointer prompt snapshot tests 通过。

### T8：前置审计门禁

审计：

- `runtime/output/engine.py` restore/provenance 是否有可抽出的纯函数。
- `runtime/context/history/*` 除 T7 的 prompt/budget 外是否存在纯 reducer。
- `runtime/context/compaction/*` 是否存在不依赖 LLM/session/event 的纯算法。

要求：

- T8 不是实施后补审计；它是批准 output/context 额外迁移前的硬门禁。
- 每个新增迁移必须先列 import graph、side-effect audit 和行为测试。
- 审计未完成时，output/context 额外下沉不进入实施范围。

## 8. 行为等价验收

除架构 import 测试外，迁移必须保留业务语义。已确认的错误行为不得作为 compatibility contract；breaking correction 必须写入 ADR、协议 golden test 或 migration note。

- config precedence：DEFAULT、SYSTEM、USER、PROJECT、WORKDIR、PROFILE、ENV、CLI_FLAG、PROGRAMMATIC、MANAGED 顺序不变。
- security filtering：WORKDIR 层继续剥离 credential/endpoints 相关 key。
- `.mote` discovery：用户层最低、git root 到 cwd 的项目层顺序、缺失文件 best-effort 语义不变。
- watcher/reload：file-change publication、reload 去抖、异常隔离、取消后释放、generation 原子切换和多 reload 串行化语义正确。
- prompt rendering：Skills listing/activation、compaction prompt、token pressure 保持业务语义；TaskResultPointer 改为结构化 DTO 后以新协议 golden test 验收。
- token budget：Unicode、XML-ish content、空字符串、极小 token budget、超大输入边界满足新不变量；旧超预算行为不保留。
- background tasks：build context、wake、close、result registry unload、session workspace 输出路径生命周期不回归。
- hook wire：typed invocation 不允许 payload 覆盖 envelope；legacy snake/camel wire 仅在 serializer golden test 中验证。
- old paths：删除旧入口前必须全仓 AST import 扫描为零；删除后不得添加 compatibility re-export。

## 9. 最终验收

完成后必须满足：

- `contracts` 只包含 DTO/Protocol/错误/事件，不包含 runtime strategy。
- `contracts/ports/config_source_provider.py` 删除；contracts 不包含产品配置文件 discovery provider。
- CAS 内容身份使用 `contracts.content.ContentIdentity`；`ArtifactContentRef` 只在 artifact 边界包含 locator；FileOps 事件不携带 artifact locator。
- contracts 不暴露通用 `SessionWorkspacePort.space(kind)`；background tasks 使用目的型 `TaskOutputLocationPort`。
- hook contracts 使用 typed `HookInvocation` 和封闭 payload DTO；legacy snake/camel wire serializer 不进入 contracts。
- `TaskResultPointer` 是结构化 DTO；不存在从渲染文本反解析路径/状态的 production code。
- `kernel` 只包含单 Agent 纯语义，不包含 IO。
- kernel token/context budget 不接收 model name，不读取 `TOKEN_MAX`，`ContextBudgetPolicy` 只有一个阈值真相源。
- `runtime` 不保留已迁模块的 compatibility re-export。
- `runtime.config.*` 删除；应用配置中心唯一 owner 是 `product.config`。
- `runtime.config.discovery` / `locations` 删除；路径策略唯一 owner 是 `product.paths`。
- `product.paths` 不保留 `CONFIG_ROOT`、`DEFAULT_WORKSPACE_ROOT` 等全局默认根目录常量；默认路径只由 `default_runtime_paths(...)` 生成。
- runtime capability 不接收完整 `RuntimePaths` DTO，只接收专用 path value object 或具体 `Path`。
- config reload 使用 `ConfigSnapshot` generation 原子切换；runtime 不提供 ad-hoc `apply_config(...)`。
- runtime watcher 只接收 runtime-owned watch request 并发布 file-change event，不接收 product manifest，不执行 reload callback。
- output/context 只在审计通过后迁移纯函数，不做目录级搬迁。
- hook/MCP/permission 的产品文件名、layering、JSON section loader 不留在 runtime。
- Skills per-turn prompt adapter 不留在 `runtime.context.turn.sources`。
- Code Map store 默认路径和产品排除目录不留在 `runtime.code_map`。
- product-only 文件约定、默认根目录、first-run scaffolding、human console fallback 不留在 runtime。
- 顶层 `runtime/reporting.py` 删除；reporting substrate 在 `runtime.telemetry.reporting`，领域 report adapter 回到 owner。
- `runtime.media` 只保留 `runtime.media.video` 本地视频分解 capability；产品展示和 CLI surface 不留在 runtime media。
- `runtime.control` 不包含 maintenance、completion、reconciliation、singleflight。
- `runtime.persistence` 不包含 session layout、workspace cleanup、durable/ledger 领域语义。
- 每个 slice 完成后测试通过且旧 import AST 扫描为零，不允许最后统一补门禁。
