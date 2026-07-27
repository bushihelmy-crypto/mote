# Mote 面向未来十年的执行架构

> 状态：目标架构设计
>
> 范围：Agent Runtime、Execution Cell、Control Plane、Product Session，以及它们之间的稳定契约。
>
> 核心目标：长期保持低负债、高可用、可恢复、功能完整和可演进，使新增 OS、执行环境、模型、Agent Flow、存储或产品前端时，只增加局部实现，不破坏既有语义。

---

## 1. 背景与问题定义

Mote 当前已经具备较强的框架基础：分层架构、组合式 Role、类型化 Flow、能力注入、Tool Effect、权限和沙箱、File Operations、durable journal、AgentControl 与多 Agent 调度。

但若从未来十年的演进视角考虑，仍需进一步稳定以下边界：

- Tool 不应感知具体 OS、进程或本地路径语义；
- 本地运行只能是一个 Backend，而不能成为框架默认假设；
- Agent 的逻辑身份必须与某次进程内 incarnation 分离；
- 所有副作用必须具有统一、可恢复的状态语义；
- Product 不应绑定具体 Role 或 Flow 实现；
- 单进程和分布式部署必须共享同一套业务契约，不能演化成两套代码路径；
- 架构约束必须由自动化测试执行，而不是依赖维护者记忆。

本设计不追求提前实现所有分布式能力，而是要求从现在开始保证：

1. 任何本地实现都只是可替换 Backend；
2. 任何外部副作用都有明确状态、所有权和恢复语义；
3. 任何部署变化都不会渗透进 Kernel 与 Tool 定义；
4. 新能力通过实现稳定契约接入，而不是修改无关层。

---

## 2. 架构总原则

### 2.1 逻辑分离，默认物理共置

目标架构由五个逻辑平面组成：

```text
Product Plane
  CLI / TUI / ACP / AG-UI / Web / API
                │
                ▼
Control Plane
  Registry / Scheduler / Quota / Lease / Placement / Routing
                │
                ▼
Agent Runtime
  Kernel / Flow / Context / Policy / Durable State / Output
                │
                ▼
Execution Cell
  Workspace / Process / Terminal / LSP / Browser / Notebook
                │
                ▼
Host Backend
  Local POSIX / Windows / Container / Remote Worker / IDE Host
```

这些边界在代码和契约上必须明确，但默认部署仍应让 Agent Runtime 与 Execution Cell 共置：

```text
Worker
├── Agent Runtime
├── Execution Cell
└── Workspace
```

Coding Agent、代码仓库、Shell、Git 与 LSP 应尽量保持数据局部性。分布式部署优先拆分 Control Plane 与完整 Worker，而不是让中心 Agent 通过网络逐个执行文件 IO。

### 2.2 Local 是 Backend，不是默认语义

Kernel、Tool 和跨边界 Contract 不得假定：

- 当前进程 cwd 就是 workspace；
- 路径一定是本机 POSIX 或 Windows 路径；
- `subprocess` 一定运行在 Agent 进程所在机器；
- 本地 fd、inode、socket 或 asyncio Task 可以进入 durable state；
- 本地绝对路径可以作为 Artifact 或 Session 的稳定身份。

具体 OS API 只允许出现在 Runtime Backend 内。

### 2.3 抽象领域语义，不模拟远程 POSIX

禁止为跨环境执行建立 `open/stat/fsync/ioctl/kill` 等低层 RPC 镜像。不同 OS 对锁、路径、inode、PTY、metadata 和原子写的语义并不等价，低层镜像会制造一个永远补不完整的虚拟 OS。

跨边界接口必须表达 Agent 所需的高层语义，例如：

- 读取一个带版本的文件视图；
- 在不可变快照中搜索；
- 准备并提交编辑事务；
- 执行一个经过审批的命令；
- 创建、重连和控制一个 Terminal；
- 发布或解析一个 Artifact。

### 2.4 不宣称 Exactly Once

分布式系统不能普遍保证 exactly-once 副作用。Mote 的目标是：

- at-least-once delivery；
- 幂等执行；
- lease fencing；
- operation status query；
- durable reconciliation；
- 无法证明结果时显式进入 `IN_DOUBT`。

---

## 3. 代码分层与物理模块

保持现有五层依赖方向：

```text
contracts <- kernel <- runtime <- orchestration <- product
```

建议在各层内形成以下模块：

```text
contracts/
  execution/
    ids.py
    resources.py
    capabilities.py
    operations.py
    workspace.py
    process.py
    terminal.py
    artifacts.py
    errors.py
  session_protocol/
    commands.py
    events.py
    requests.py
    negotiation.py

kernel/
  agent_spec.py
  flow/
  output/
  tools/

runtime/
  execution/
    cell.py
    workspace/
    process/
    terminal/
    backends/
      local/
      windows/
      in_memory/
      remote/
  operations/
  durable/
  session/
  policy/

orchestration/
  control_plane/
  placement/
  residency/
  scheduling/
  workers/

product/
  application.py
  session/
  cli/
  integrations/
```

其中：

- `contracts` 只包含可序列化 DTO、ID、错误、事件和 Protocol；
- `kernel` 只决定 Agent 如何思考、解释和推进 Flow；
- `runtime` 实现可靠执行、状态、权限和 Execution Cell；
- `orchestration` 决定 Agent 在哪里运行以及由谁持有；
- `product` 负责用户连接、配置发现和具体产品体验。

---

## 4. Execution Cell

### 4.1 定义

Execution Cell 表示一个可供 Agent 使用的执行环境。它是窄能力的集合，不是向 Tool 暴露的 Service Locator。

```text
ExecutionCell
├── descriptor
├── WorkspaceQuery
├── WorkspaceMutation
├── CommandRunner
├── TerminalService
├── ArtifactService
├── FileWatchService       optional
├── CodeIntelligence       optional
├── BrowserService         optional
└── NotebookService        optional
```

Tool 只能声明并接收自己需要的能力。例如 `Edit` 只能获得 `WorkspaceMutation`，不能获得整个 `ExecutionCell`。

### 4.2 能力协商

Execution Cell 在装配期发布带版本的能力描述：

```text
ExecutionCapabilities
  filesystem:
    read: true
    search: true
    atomic_replace: true
    durable_fsync: true
    symlink_identity: true
    case_sensitive: false
  process:
    shell: true
    argv_exec: true
    resource_limits: true
    network_isolation: true
  terminal:
    supported: true
    reattach: true
    max_sessions: 8
  lsp:
    supported: true
```

AgentSpec、Toolset 和 Runtime Module 声明 capability requirements。装配期若无法满足必须明确失败，不允许运行时隐藏降级。

禁止 Kernel 或 Tool 使用 `os.name`、backend 名称或 transport 类型进行分支。

### 4.3 推荐的窄 Port

```text
WorkspaceQuery
  resolve_path(request) -> ResolvedResource
  read_view(request) -> FileView
  search(request) -> SearchResult
  capture_snapshot(request) -> SnapshotRef

WorkspaceMutation
  prepare_edit(request) -> PreparedEdit
  commit_edit(plan_id, operation_context) -> EditOutcome
  rewind(request, operation_context) -> RewindOutcome

CommandRunner
  prepare(request) -> PreparedCommand
  execute(prepared_id, operation_context) -> CommandHandle
  status(operation_id) -> OperationStatus
  cancel(operation_id) -> CancelOutcome

TerminalService
  open(request, operation_context) -> TerminalHandle
  attach(terminal_id, cursor) -> TerminalStream
  write(terminal_id, input) -> None
  resize(terminal_id, size) -> None
  close(terminal_id) -> None
```

接口应优先异步，即使 Local Backend 当前使用同步实现。同步本地实现通过受控线程执行，不得迫使未来 Remote Backend 伪装成同步调用。

---

## 5. 资源身份与路径模型

### 5.1 禁止裸路径跨边界

跨层和 durable 数据不得使用裸 `str`、`pathlib.Path` 或宿主绝对路径作为资源身份。

基础身份模型：

```text
EnvironmentId
WorkspaceId
ResourceId
Revision
ArtifactId
OperationId
RunId
AgentId
LeaseEpoch
```

路径模型：

```text
WorkspaceRef
  environment_id
  workspace_id

WorkspacePath
  workspace_id
  relative_path
  path_flavor

ResolvedResource
  path
  resource_id
  revision
  kind
  permission_facts
```

规则：

- Tool 可以接收用户提供的路径字符串；
- Backend 负责解析、规范化和 workspace containment；
- 跨边界只传 `WorkspacePath` 和资源身份；
- 宿主绝对路径不离开 Backend；
- `ResourceId` 不直接等同 inode；
- `Revision` 必须可序列化并可用于乐观并发控制；
- Windows/POSIX 差异由 Backend 内部吸收。

### 5.2 Artifact 不是路径

统一 Artifact 模型：

```text
ArtifactRef
  artifact_id
  owner
  media_type
  size
  digest
  storage_scope
  locator
  preview
```

大工具输出、图片、PDF、snapshot、patch、Terminal transcript 和跨 Worker 结果都使用 `ArtifactRef`。本地 Backend 可以将其映射为文件，分布式 Backend 可以映射为对象存储，但 Agent 和 Product 不感知其物理位置。

---

## 6. 统一 Operation 模型

### 6.1 状态机

所有可能产生副作用的动作统一为 Operation：

```text
CREATED
  → PREPARED
  → AUTHORIZED
  → STARTED
  → COMMITTED
  → PUBLISHED

终止或异常状态：
  REJECTED
  FAILED
  CANCELLED
  IN_DOUBT
```

每个 Operation 至少携带：

```text
operation_id
run_id
agent_id
workspace_id
lease_epoch
idempotency_key
effect_kind
expected_revision
created_at
deadline
```

### 6.2 标准执行链

```text
Tool
  → prepare(intent)
  → 获得 canonical target、revision、effect 和 permission facts
  → PermissionEngine 决策
  → journal PREPARED / AUTHORIZED
  → execute(operation_id, lease_epoch)
  → journal STARTED / COMMITTED
  → 发布事件、Projection 和 ToolResult
```

Prepare 结果必须绑定：

- 解析后的目标；
- 当前资源 revision；
- operation intent digest；
- 权限判断所使用的事实；
- 有效期和 lease epoch。

Commit 必须重新验证这些条件，避免 prepare 与 execute 之间发生 TOCTOU。

### 6.3 Effect 分类

```text
PURE
  可重试、可并行、可缓存，例如 read/search/stat

LOCAL_MUTATION
  需要 revision、事务和 workspace 单写者 lease，例如 edit/rename/delete

EXTERNAL_EFFECT
  需要 idempotency 或人工 reconcile，例如 deploy/send/publish
```

每个 Operation Definition 必须声明：

- effect class；
- replay policy；
- concurrency policy；
- permission subjects；
- recovery policy；
- cancellation semantics。

### 6.4 不确定状态

网络断开或 Worker 崩溃后，调用方必须通过 `status(operation_id)` 查询实际状态。只有能够证明未执行的 Operation 才允许重新执行。

无法证明成功或失败时进入 `IN_DOUBT`，由领域专属 reconciler 处理。任何通用 retry 装饰器都不得自动重试未知的外部副作用。

---

## 7. Agent Identity、Incarnation 与恢复

### 7.1 Durable Identity 与运行实例分离

Agent durable state 只允许包含可恢复数据：

```text
AgentDefinitionRef
AgentState
Context checkpoint
Pending operations
Mailbox cursor
WorkspaceRef
Lease epoch
Output state
```

不得进入 durable state：

- subprocess；
- fd、socket；
- asyncio Task；
- provider client；
- lock 实例；
- Backend 实例；
-进程本地回调。

运行时通过以下公式构建 incarnation：

```text
Durable Agent Identity
        +
Worker-local Services
        =
Agent Incarnation
```

### 7.2 Workspace 单写者与 Fencing

```text
WorkspaceLease
  workspace_id
  owner_id
  epoch
  expires_at
```

每个 mutation、Shell、Terminal 写操作都携带 lease epoch。Backend 必须拒绝 stale epoch。

例如 Worker A 持有 epoch 7，Worker B 接管后获得 epoch 8；即使 A 恢复并继续发送请求，Backend 也必须拒绝 epoch 7，从而避免 split-brain。

默认并发语义：

- Query 可并行；
- Workspace mutation 单写；
- Agent 迁移只发生在安全 checkpoint；
- 多 Agent 修改同一项目优先使用独立 worktree 或 branch；
- merge 是显式 Operation，而不是共享目录上的偶然结果。

### 7.3 Worker 故障恢复

Control Plane 通过 heartbeat 和 lease 判定 Worker 失效：

1. fencing 旧 incarnation；
2. 选择新 Worker；
3. 从 durable log/checkpoint 重建 Agent；
4. reconcile 所有 `STARTED` 或 `IN_DOUBT` Operation；
5. 恢复 mailbox cursor；
6. 在新的 lease epoch 下继续运行。

Projection 必须是可删除、可重建的派生状态，不能成为唯一真相源。

---

## 8. Control Plane 与分布式部署

Control Plane 负责：

- Agent registry；
- parent/child tree；
- mailbox routing；
- scheduler；
- concurrency、token 和 cost quota；
- worker capability 与 placement；
- residency；
- workspace affinity；
- lease/fencing；
- incarnation recovery。

目标部署：

```text
Product Clients
       │
       ▼
Highly Available Control Plane
       │
       ├── Worker A
       │   ├── Agent A
       │   └── Local Execution Cell / Workspace A
       │
       ├── Worker B
       │   ├── Agent B
       │   └── Local Execution Cell / Workspace B
       │
       └── Worker C
           ├── Agent C
           └── Local Execution Cell / Workspace C
```

Placement 依据能力而不是硬编码机器类型，例如：

- OS 和 CPU 架构；
- GPU；
-浏览器；
- workspace location；
- sandbox 强度；
- network policy；
-模型可达性；
-数据驻留要求。

单进程部署使用同一套 Control Plane Contract 的 in-process 实现；分布式部署替换 transport 和 storage，不改变调度、身份和操作语义。

---

## 9. Product Session Protocol 与 AgentDriver

### 9.1 AgentDriver

Product 不直接依赖 `Role`、`RoleComponents` 或具体 Flow。定义顶层行为边界：

```text
AgentDriver
  run(input, event_sink) -> RunOutcome
  status() -> AgentStatus
  steer(input) -> None
  cancel(reason) -> None
  resume(cursor) -> ResumeOutcome
  close() -> None
```

默认实现为 `RoleDriver`。未来可接入其他 Agent Runtime、回放 Agent、测试 Agent 或远端托管 Agent，而不修改 CLI、ACP、AG-UI 和 Web。

### 9.2 Session Protocol

Product Session 只承诺：

- Prompt、Steer、Cancel；
- Turn/Step/Tool/Compaction 事件；
- Approval、Question 和 Handoff；
- Status、Usage、Agent tree；
- Artifact、Surface；
- resume cursor；
- capability negotiation。

协议不得暴露：

- Graph node class；
- RoleComponents；
- ContextManager 实现；
- ToolExecutor 实例；
- Worker 内部地址；
-宿主绝对路径。

协议必须具备：

- `protocol_version`；
- feature negotiation；
- request/response correlation；
- deadline 与 cancellation；
- streaming sequence；
- reconnect/resume cursor；
- unknown-field tolerance；
-显式 schema migration。

---

## 10. 扩展机制边界

扩展类型必须分工明确：

```text
Tool 能力扩展       → Toolset / MCP
Prompt 工作流扩展   → Skill
Agent 定义扩展      → AgentSpec
执行环境扩展        → Execution Backend
宿主产品扩展        → Session Protocol / Consumer
完整 Agent 内核扩展 → AgentDriver
```

不同扩展机制不得重复发明另一套工具 RPC 或生命周期模型。

声明式 AgentSpec 最终解析为不可变的 `ResolvedAgentDefinition`，可以组合：

- prompt；
-模型与 routing strategy；
- Toolset；
- Skill；
- Subagent；
- Flow/Graph；
- OutputContract；
- capability requirements；
- durable policy。

继承、路径发现和配置 merge 只发生在解析期；运行期间只使用已验证、不可变的 resolved definition。

若未来需要分发复合扩展，可定义 Agent Package：

```text
AgentPackage
  manifest
  agents/
  skills/
  prompts/
  schemas/
  migrations/
```

单纯的进程外 Tool 扩展继续采用 MCP，不另造协议。

---

## 11. 安全模型

安全分为两层：

```text
Agent Runtime
  Policy、用户审批、组织规则、意图审计

Execution Backend
  路径 containment、resource limits、sandbox、network enforcement、fencing
```

规则：

- Tool 只能接收显式发布的窄能力；
- Execution Cell 不能通过一个宽对象整体注入 Tool；
- secret 使用引用和 broker，不直接进入 Tool 参数、事件或日志；
- 外部 Backend 必须重新校验 operation digest、lease epoch 和 prepared token；
- Backend 的拒绝不能被上层 policy 覆盖；
- 外部扩展只能增加约束，不能推翻核心安全拒绝；
-所有权限决策和执行结果必须进入审计事件。

---

## 12. 可观测性与 SLO

所有事件使用稳定 ID 关联：

```text
trace_id
session_id
agent_id
run_id
operation_id
tool_call_id
workspace_id
lease_epoch
```

必须可观测：

- 排队时间、调度时间；
-模型时间、首 token 时间；
- Tool prepare/authorize/execute/publish 时间；
- transport latency；
- Backend latency；
- journal flush latency；
- operation retry/reconcile；
- lease contention；
- Worker recovery；
- projection lag；
- token、cost 和 artifact 使用量。

日志、指标和 trace 是事件流的不同投影，不应分别定义互不一致的生命周期语义。

---

## 13. 自动化架构约束

长期低负债必须依靠强制检查。建议加入以下架构测试：

1. `kernel/` 禁止 import runtime、orchestration、product；
2. `product/toolsets/` 禁止 import `os`、`pathlib`、`subprocess`；
3. OS API 只能出现在指定 Backend 目录；
4. Contract DTO 禁止包含 `Path`、fd、socket、Task、client 或 callback；
5. 所有 Backend 必须通过同一 conformance suite；
6. 所有 Contract 和事件必须通过序列化 round-trip；
7. 所有 mutation 必须携带 operation ID 和 lease epoch；
8. 所有 effect 必须声明 recovery policy；
9. 禁止 process-global mutable registry；
10. ContextVar 只能承载 trace/run correlation，不能作为权限能力传递通道；
11. fault injection 覆盖每个 `PREPARED/STARTED/COMMITTED` 崩溃点；
12. replay 必须确定性；
13. projection 删除后必须能从日志重建；
14. transport 重复、乱序、断连、超时和迟到响应必须有测试；
15. capability 缺失必须装配期失败；
16. 任何 fallback 都必须是契约明确的策略，禁止静默降级。

Backend conformance suite 至少验证：

- 路径 containment；
- revision 冲突；
- atomic mutation；
- cancellation；
- deadline；
- idempotency；
- stale lease rejection；
- duplicate request；
- crash recovery；
- large output/artifact；
-权限事实一致性。

---

## 14. 分阶段迁移路线

迁移必须保持单一权威路径。每完成一个阶段就删除旧路径，不保留永久双实现或旁路。

### 阶段一：建立目标 Contract

新增：

```text
contracts/execution/
contracts/session_protocol/
```

定义纯 DTO、ID、Protocol、状态机和不变量，暂不改变用户行为。

### 阶段二：封装当前本地实现

建立：

```text
LocalExecutionCell
  LocalWorkspaceQuery     → 当前 FileOperations
  LocalWorkspaceMutation  → 当前 FileOperations
  LocalCommandRunner      → 当前 aexecute
  LocalTerminalService    → 当前 TerminalRuntimeDriver
```

所有现有测试应保持通过。

### 阶段三：切断 Tool 的 OS 依赖

建议迁移顺序：

1. Edit；
2. Search；
3. 文本 Read；
4. Bash；
5. Terminal；
6. 媒体与文档 Read；
7. LSP、watch、browser、notebook。

每迁移一个模块：

- 将其改为仅依赖窄 Port；
- 增加 Backend contract tests；
- 增加架构 import guard；
- 删除旧 OS 访问路径。

### 阶段四：统一 Operation、Lease 与 Journal 语义

将文件事务、Tool Effect、Runtime Operation、Output Commit 对齐到统一的：

- ID；
-状态词汇；
- fencing；
- idempotency；
- event envelope；
- recovery contract。

它们可以保留领域专属实现，但不能继续拥有互不兼容的生命周期语义。

### 阶段五：完成 AgentDriver 与 Session Protocol

使 Product 不再依赖具体 Role，实现本地 in-process transport，并将现有 CLI、ACP、AG-UI 统一投影到 Session Protocol。

### 阶段六：增加第二 Backend

优先实现 `InMemoryExecutionCell`，用于验证 Contract 是否真正独立于 OS，并提供快速、确定性的测试环境。

随后根据实际需求选择 Container 或 Remote Worker Backend。不要为了证明抽象而优先实现 SSH 文件系统。

### 阶段七：分布式 Worker

将完整 Agent incarnation 与 Local Execution Cell 部署到 Worker：

```text
Control Plane
  → placement
  → lease/fencing
  → Worker
      → Agent Incarnation
      → Local Execution Cell
```

此阶段只替换 transport、storage 和 placement，不修改 Tool 或 Kernel。

---

## 15. 明确非目标

本设计不主张：

- 现在立即实现所有远端 Backend；
- 将 Python `os` API 逐个包装成框架接口；
- 默认让 Agent 与 workspace 跨网络分离；
- 为每个 Tool 单独发明远端执行协议；
- 用一个全能 `WorkspaceRuntime` 暴露全部宿主能力；
- 通过序列化 live Python 对象实现 Agent 迁移；
- 声称任意外部副作用 exactly-once；
- 通过静默 fallback 掩盖 Backend 能力不足；
- 为未来假设提前实现没有消费者的功能。

---

## 16. 最终架构定义

Mote 的长期架构可以概括为：

> Kernel 决定 Agent 如何思考；Runtime 决定状态如何可靠运行；Execution Cell 决定能力如何实现；Control Plane 决定 Agent 在哪里运行；Product Session 决定用户如何连接。五者只通过版本化、可序列化、可测试的契约交互。

对应的长期不变量是：

```text
Tool 不感知 OS
Kernel 不感知部署
Product 不感知 Role 内部结构
Control Plane 不执行业务副作用
Backend 不决定 Agent 策略
Projection 不是事实源
路径不是资源身份
进程不是 Agent 身份
重试不是恢复策略
本地实现不是默认语义
```

只要这些不变量被自动化约束，Mote 就能在保持当前本地效率和功能完整性的同时，为未来的多 OS、多 Backend、高可用 Control Plane 和分布式 Worker 留出稳定、低负债的演进路径。
