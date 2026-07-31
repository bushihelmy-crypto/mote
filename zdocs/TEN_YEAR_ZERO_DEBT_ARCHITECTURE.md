# Mote 十年低负债全局拆分架构

> 状态：目标架构与迁移治理基线
>
> 范围：`contracts`、`kernel`、`runtime`、`orchestration`、`product`，以及未来的跨语言 Control Plane 与 Execution Cell。
>
> 本文中的“零负债”不是承诺永远不产生临时代码，而是要求：每个临时实现都有明确边界、唯一事实源、替换路径、删除条件和自动化约束；新增部署形态不得产生第二套业务语义。

---

## 1. 目标

Mote 未来十年的架构目标是：

1. 保持现有单向分层：

   ```text
   contracts <- kernel <- runtime <- orchestration <- product
   ```

2. 单机、Sidecar 和分布式部署共享同一套领域契约与执行语义。
3. Python 负责 Agent 语义与高频演进，Rust 负责主机执行与安全，Go 负责集群所有权与协调。
4. 跨语言事实由版本化 IDL 定义，持久事实由 Journal/CAS 保存。
5. 所有副作用均可标识、查询、恢复、对账和 fencing。
6. 每类状态只有一个权威来源，所有 Projection 均可重建。
7. 兼容代码只作为有期限的迁移设施存在，不形成永久双轨。

本文补充而不替代：

- [`ARCHITECTURE.md`](./ARCHITECTURE.md)：当前代码架构；
- [`FUTURE_EXECUTION_ARCHITECTURE.md`](./FUTURE_EXECUTION_ARCHITECTURE.md)：Execution Cell 与未来执行模型；
- 各 package governance 文档：具体包的治理和迁移约束。

---

## 2. 总体架构

```text
Product Plane
  CLI / TUI / Web / API / IDE / ACP
                         │
                         │ Session API
                         ▼
Control Plane（Go）
  Agent/Worker Registry / Placement / Quota / Lease
  Mailbox Routing / Heartbeat / Fleet Recovery / Drain
                         │
                         │ assignment + ownership
                         ▼
Worker
  Python Agent Runtime
    Kernel / Role / Context / Model / Tool Definition
    Session Semantics / Replay / Domain Adapters
                         │
                         │ local or remote versioned RPC
                         ▼
  Rust Execution Cell
    Workspace / File Transaction / Command / PTY / Sandbox
    File Watch / Artifact CAS / Operation Journal / Fencing
                         │
                         ▼
  Host OS / Container / IDE Host / Remote Environment

Shared Infrastructure
  SQL/KV / Object Store / Event Transport / Secret/KMS / OTel
```

### 2.1 逻辑分离，默认物理共置

Python Agent Runtime、Rust Execution Cell 和 Workspace 默认位于同一个 Worker。这样可保持代码搜索、Git、Shell、LSP、文件访问和终端交互的数据局部性。

Control Plane 可以独立高可用部署，但不得把细粒度文件 IO 集中到控制节点。跨机器边界表达高层领域操作，而不是模拟远程 POSIX。

### 2.2 单机也是正式部署形态

单机模式不是绕过正式契约的特殊路径，而是相同 Port 的共置实现：

```text
同一 Tool / Operation 语义
        ├── in-process backend
        ├── local sidecar backend
        └── remote worker backend
```

三个 backend 必须通过相同的契约、恢复和一致性测试。

---

## 3. 语言与职责边界

### 3.1 Python：Agent cognition and semantics

Python 长期负责：

- Kernel Flow、Think、Parser、Command Channel 和输出验证；
- Role、RoleComponents、incarnation 装配；
- Context、Prompt、历史压缩和 token 策略；
- Model provider adapter、模型语义和路由策略；
- Tool 定义、schema、能力声明与结果解释；
- Session history、replay、checkpoint 的领域语义；
- Product 配置、Skills、Toolsets 和用户交互。

Python 不负责集群所有权，也不直接成为主机安全边界。

### 3.2 Rust：host execution and safety

Rust Execution Cell 负责：

- Workspace 查询、revision、快照和事务；
- 文件 mutation、原子提交、锁和 stale-epoch rejection；
- Command、进程树、PTY、Terminal reconnect；
- sandbox、seccomp、namespace、资源限制；
- file watch 和平台句柄生命周期；
- Worker 本地 Artifact CAS；
- Operation journal、status 和 reconciliation；
- 最终副作用位置的 permit、digest、deadline 和 fencing 校验。

Rust 不决定 Agent 应做什么，也不复制 Prompt、Flow、placement 或产品策略。

### 3.3 Go：distributed ownership and coordination

Go Control Plane 负责：

- Agent/Worker registry；
- Worker heartbeat、capability advertisement 和 drain；
- Agent placement、workspace affinity 和 residency；
- mailbox routing 与 durable cursor 协调；
- 全局并发、token、cost 和服务配额；
- lease authority、takeover 和 fleet recovery；
- 多 Worker scheduler 与滚动升级协调。

Go 不解释 Agent 历史、Prompt 或 ToolResult，也不执行 Workspace 的最终 mutation。

### 3.4 IDL 与 durable truth

- Protobuf：内部跨进程和跨语言 RPC 的唯一 wire truth；
- OpenAPI：外部 HTTP API 的唯一 wire truth；
- Journal：Agent 历史和 Operation 状态的持久 truth；
- CAS：Artifact bytes 的持久 truth；
- Python `Protocol`：进程内窄能力接口的 truth。

禁止手工同步维护 Pydantic、Go struct、Rust struct 三套 wire 模型。各语言 wire 类型必须从 IDL 生成，并通过 adapter 转换成领域模型。

---

## 4. 五层的最终职责

## 4.1 `contracts/`

`contracts/` 只描述边界，不执行策略：

- 可序列化领域 DTO、ID、错误码和事件；
- Python 进程内 `Protocol`；
- 配置 schema；
- wire/domain adapter 所需的稳定语义；
- 跨语言 IDL 的领域归属和兼容规则。

跨进程、跨语言或持久化数据必须具有明确 schema version。Python callback、异常实例、`ContextManager`、socket、fd 和任意对象不得成为 wire contract。

建议长期形成：

```text
contracts/
  agent/
  conversation/
  execution/
  session/
  events/
  ports/                 # Python-only narrow ports
  wire/                  # 若最终治理决定置于包内
    proto/
    openapi/
    compatibility/
```

物理目录需服从当时的 package governance 决策；无论位于何处，IDL 均不得依赖上层实现。

## 4.2 `kernel/`

`kernel/` 是纯 Python、模型无关、部署无关的 Agent 状态机。它负责决定 Agent 如何思考和推进状态，不负责可靠 IO。

Kernel 必须满足：

- 不感知 Worker 地址、数据库或 RPC transport；
- 不依赖本地路径、进程、fd、socket 或锁；
- 不在 durable state 中保存 `asyncio.Task`、callback 或 client；
- 所有外部动作表现为可序列化 intent；
- 外部能力只经 `contracts/ports/` 注入；
- 可从 checkpoint 和事件确定性重建领域状态。

Kernel 不整体迁往 Go/Rust。经 profiling 证实的稳定纯计算热点，可以使用粗粒度 Rust 扩展，但不得让 Flow 控制频繁穿越 FFI。

## 4.3 `runtime/`

`runtime/` 负责一个 Agent 或一个 Worker 在任务已分配后如何可靠执行。

保留在 Python Runtime：

- `agent/`；
- `context/`、`prompt/`；
- `models/` 的 provider adapter 与领域策略；
- Tool definition、binding 和 ToolResult 解释；
- Session history/replay/checkpoint 语义；
- 本地事件产生、订阅和 domain adapter；
- output 语义和恢复策略。

逐步由 Rust Execution Cell 承接：

- `fileops/` 的主机执行部分；
- Command、`interactive/terminal/`；
- `sandbox/`；
- file watch；
- Workspace transaction 和 fencing；
- Worker-local artifact CAS；
- Operation journal/status/reconcile；
- LSP 进程宿主。

这里的“承接”指在 Runtime 层 Port 后增加 Rust backend，不表示把 Execution Cell 错误归类为 Orchestration。

## 4.4 `orchestration/`

`orchestration/` 负责多个 Agent、多个 Worker 和全局资源之间的决策：

- registry、parent/child tree；
- placement、residency、workspace affinity；
- heartbeat、takeover、fleet recovery；
- mailbox routing；
- 多实体 scheduler；
- 全局 concurrency/token/cost quota；
- lease ownership policy。

Python 可长期保留：

- 单机 in-memory 实现；
- Control Plane client/adapter；
- 多 Agent 领域策略；
- Role spawn/communication 的 Python 适配。

集群级实现可迁到 Go，但只能实现同一套 Orchestration 语义，不能在 Python 与 Go 中各维护一套 placement 或 takeover 规则。

## 4.5 `product/`

`product/` 负责：

- CLI/TUI/Web/API/IDE；
- Coding Agent 产品定义；
- Skills、Toolsets 和集成；
- 配置发现和 composition root；
- 用户授权交互和展示。

Product 只在装配期选择实现。Runtime 不得 import Product，CLI 不得直接修改 Runtime 私有存储，Go/Rust 服务不得复制 Product 策略。

---

## 5. Runtime 与 Orchestration 的判定规则

最稳定的判定问题是：该组件是否必须拥有多个实体的全局视图。

| 问题 | 归属 |
| --- | --- |
| 当前 Agent/Operation 如何安全完成？ | Runtime |
| 当前 Worker 如何管理本地资源？ | Runtime / Execution Cell |
| 哪个 Worker 应获得执行权？ | Orchestration |
| 多个 Agent 如何调度和通信？ | Orchestration |
| 两层之间交换什么数据或能力？ | Contracts |
| Agent 如何思考和转换状态？ | Kernel |

边界能力需拆成“决策”和“强制”两半：

- Lease：Orchestration 分配所有权；Runtime/Execution Cell 在最终 commit 时强制 fencing。
- Recovery：Orchestration 决定接管；Runtime 重建 incarnation 并 reconcile。
- Cost：Runtime 采集 usage；Orchestration 执行 fleet quota。
- Events：Runtime 产生和本地分发；Orchestration 消费事件作全局决策。
- Scheduler：Worker 内 turn driver 属执行机制；跨 Agent/Worker 调度属于 Orchestration。

不要为了目录纯洁性提前搬动仍属于单 Worker 机制的代码。只有职责稳定成为跨实体协调后，才物理上移。

---

## 6. 长期服务边界

## 6.1 Control Plane API

```text
WorkerControl
  RegisterWorker
  Heartbeat
  AdvertiseCapabilities
  DrainWorker

Placement
  AcquireAssignment
  RenewAssignment
  ReleaseAssignment
  QueryAssignment

AgentControl
  CreateAgent
  DeliverMessage
  QueryAgent
  CancelAgent
  StreamAgentEvents

LeaseAuthority
  AcquireLease
  RenewLease
  FenceLease
  AssertLease
```

## 6.2 Execution Cell API

```text
WorkspaceQuery
  ReadView
  SearchSnapshot
  ResolveMetadata

WorkspaceMutation
  PrepareMutation
  CommitMutation
  QueryMutation
  ReconcileMutation

CommandExecution
  PrepareCommand
  StartCommand
  StreamCommandEvents
  CancelCommand
  QueryCommand

Terminal
  OpenTerminal
  AttachTerminal
  SendInput
  ResizeTerminal
  ReadFromCursor
  CloseTerminal
```

## 6.3 Artifact API

```text
Artifact
  Publish
  Resolve
  ReadRange
  PromoteRetention
  Release
  VerifyDigest
```

大内容传 Artifact reference、digest 和 range，不进入控制消息。

## 6.4 Session/Durable API

```text
Journal
  AppendEvents
  ReadFromCursor
  CommitCheckpoint

Mailbox
  Enqueue
  Claim
  Acknowledge
  ReadFromCursor

Operation
  QueryStatus
  Reconcile
```

这些 API 表达领域语义，不建立 `open/stat/fsync/ioctl/kill` 等远程 POSIX 镜像。

---

## 7. 统一 Operation 模型

所有可能产生副作用的 Tool、文件 mutation、命令和外部服务调用统一为 Operation：

```text
CREATED
  → PREPARED
  → AUTHORIZED
  → STARTED
  → COMMITTED
  → PUBLISHED

终止或异常：
  REJECTED / FAILED / CANCELLED / IN_DOUBT
```

每个 Operation 至少具有：

```text
operation_id
idempotency_key
run_id
agent_id
workspace_id
owner_id
lease_epoch
effect_kind
intent_digest
expected_revision
deadline
created_at
```

Effect 分类：

- `PURE`：read/search/stat，可安全重试和并发；
- `LOCAL_MUTATION`：edit/rename/delete/workspace-changing command，需要 revision、单写 lease 和 fencing；
- `EXTERNAL_EFFECT`：deploy/send/publish，需要幂等键、receipt 或人工 reconcile。

标准执行链：

```text
Tool Intent
  → Prepare：规范化目标、计算 digest、读取 revision
  → Permission：依据规范化事实 allow/ask/deny
  → Journal PREPARED/AUTHORIZED
  → Execute(operation_id, lease_epoch, permit)
  → Backend 重验 digest/deadline/fence/revision
  → Commit + Receipt
  → Publish Event/ToolResult
```

系统只承诺：

- at-least-once delivery；
- 幂等执行；
- lease fencing；
- status query；
- durable reconciliation。

只有能够证明未执行的 Operation 才允许重新提交。无法证明成功或失败时必须进入 `IN_DOUBT`，通用 retry 不得自动重试未知外部副作用。

---

## 8. 状态所有权

| 状态 | 唯一权威来源 |
| --- | --- |
| Agent 历史 | Session journal |
| Agent checkpoint | Durable checkpoint store |
| Tool/外部副作用 | Operation journal |
| Workspace 内容 | Workspace backend |
| Artifact bytes | CAS |
| Artifact metadata | Artifact index |
| Agent placement | Control Plane assignment |
| 当前执行所有权 | Lease authority |
| Mailbox 消费位置 | Durable cursor |
| UI/查询视图 | 可重建 Projection |
| 进程、PTY、fd、socket | 当前 Worker incarnation，非 durable |

禁止：

- 同一事实由 JSONL、SQLite 和内存对象分别作最终决定；
- Projection 成为唯一事实源；
- checkpoint 保存 client、Task、socket、fd、lock 或 Backend 实例；
- 本地绝对路径成为跨 Worker 稳定身份；
- Python 类名成为持久事件类型。

Projection 必须通过“删除后从 Journal 重建”测试。

---

## 9. Identity、Lease 与恢复

长期区分：

```text
Agent ID        逻辑身份，跨恢复稳定
Incarnation ID  某次运行实例
Worker ID       当前承载节点
Run ID          一次可恢复执行
Operation ID    一次副作用意图
```

这些 ID 不得继续由一个 `session_id` 隐式兼任。

恢复流程固定为：

```text
Worker A 心跳失效
  → Control Plane fencing 旧 lease
  → Placement 选择 Worker B
  → Worker B 获取新 epoch
  → 读取 checkpoint + journal
  → 重建 Python Agent incarnation
  → reconcile STARTED/IN_DOUBT operations
  → 恢复 mailbox cursor
  → 继续执行
```

Lease 的职责跨两层：

```text
Go Control Plane     签发和更新所有权 epoch
Python Runtime       在 Operation 中传播 epoch
Rust Execution Cell  在最终 commit 处拒绝 stale epoch
```

只在 Python 调用前检查 lease 不构成 fencing。Mutation、Shell/Terminal 写操作和 output commit 的最终 Backend 必须校验 fencing token。

Agent 只在安全边界迁移：turn boundary、显式 checkpoint、无不可迁移本地 Operation，或本地资源已经拥有可重连引用。

---

## 10. 权限与安全

权限决策和执行强制分离：

```text
Python Permission Engine
  基于 Tool intent 与用户上下文作出 allow/ask/deny
                 │
                 ▼
短期 Execution Permit
                 │
                 ▼
Rust Execution Cell
  重新规范化目标并验证 permit/digest/deadline/lease/principal
```

Permit 至少绑定：

- principal；
- operation ID；
- canonical intent digest；
- workspace；
- capability；
- resource scope；
- deadline；
- lease epoch；
- 使用次数。

安全不变量：

- deny/ask 不存在旁路；
- Execution Cell 不信任调用者传入的“已规范化”路径或命令；
- Tool 只能取得声明过的窄能力；
- Control Plane 不持有业务 secret 明文；
- Worker 只获得任务所需的短期 credential；
- 网络、文件、进程能力分别授权；
- 所有外部 effect 可审计。

---

## 11. Wire 协议治理

建议按领域独立版本：

```text
mote.control.v1
mote.execution.v1
mote.session.v1
mote.artifact.v1
```

强制规则：

1. Protobuf enum 的 `0` 为 `UNSPECIFIED`。
2. 字段号废弃后必须 `reserved`，永不复用。
3. 新字段必须具有安全默认语义。
4. 破坏性变化创建新 package version。
5. error code 稳定，错误文本不参与程序判断。
6. fencing token 使用 `uint64`。
7. 金额不得使用浮点数。
8. 时间统一为 UTC；deadline 与 timeout 语义分开。
9. 流式事件必须携带 sequence、cursor 和 terminal receipt。
10. 大 payload 使用 Artifact reference。
11. 客户端只能使用 capability negotiation 后确认的能力。
12. 未知字段和未知事件必须具备前向兼容策略。

每个版本维护 golden fixtures，并由 Python、Go、Rust 进行交叉编码、解码和语义一致性测试。服务端至少在明确兼容窗口内支持当前版本和上一版本；废弃必须有 telemetry、期限和删除负责人。

Wire DTO 不直接渗入 Kernel：

```text
Generated Wire DTO → Adapter → Domain DTO / Python Port
```

Python `Protocol` 与 RPC 不要求逐方法对应。Adapter 可以组合分页、认证、重试、cursor 和多个 RPC，为上层暴露稳定领域能力。

---

## 12. 仓库与所有权形态

跨语言初期优先 monorepo，以便原子修改 IDL、adapter 和兼容测试。目标形态可为：

```text
mote/
  contracts/                    Python domain + ports
  kernel/                       Python
  runtime/                      Python Agent Runtime
  orchestration/                Python domain/client/local backend
  product/                      Python product
  ztest/

  api/                          versioned IDL（候选）
  services/control-plane/       Go（候选）
  workers/execution-cell/       Rust（候选）
  sdk/generated/                generated only（候选）
```

以上新增顶层目录只是目标候选。真正创建前必须先更新 package governance 和架构测试，不能绕过当前五层约束。也可以使用独立仓库；物理仓库选择不得改变逻辑依赖方向。

每个领域必须有明确 owner，而不是按语言划成三个互不负责的团队：

- Workspace owner 同时负责 Python Port、Proto 和 Rust backend；
- Placement owner 同时负责领域规则、Proto 和 Go implementation；
- Session owner 同时负责 Journal schema、Python replay 和恢复测试。

这样可以避免按语言形成新的组织烟囱。

---

## 13. 自动化架构门禁

文档不是约束的最终载体。以下检查必须逐步成为合并门槛。

### 13.1 分层检查

- 验证 `contracts <- kernel <- runtime <- orchestration <- product`；
- 禁止 Contracts 依赖其他层；
- 禁止 Kernel import OS、数据库和 RPC client；
- 禁止 Runtime import Orchestration/Product；
- 禁止上层类型出现在低层公开签名；
- 禁止 durable DTO 包含任意 Python 对象；
- 禁止重建 generic `common/utils` 包。

### 13.2 协议检查

- protobuf breaking-change 检测；
- Python/Go/Rust golden fixture；
- unknown-field/unknown-event 测试；
- enum forward compatibility；
- capability negotiation；
- error mapping；
- deadline/cancellation 一致性。

### 13.3 崩溃恢复矩阵

每种有副作用 Operation 至少覆盖：

```text
prepare 前
prepare 后
authorize 后
start 前
start 后
commit 前
commit 后
publish 前
```

每个故障点验证：是否可重试、是否需要 `IN_DOUBT`、是否产生重复副作用、stale lease 是否被拒绝、Projection 是否可重建。

### 13.4 分布式故障测试

- Worker `kill -9`；
- 网络分区；
- 双 Worker 竞争同一 ownership；
- heartbeat 延迟和 coordinator 暂时不可用；
- event 重复、乱序和重连；
- mailbox 重复投递；
- artifact 上传中断；
- terminal stream cursor 恢复；
- Control Plane 滚动升级和 rollback。

### 13.5 部署一致性测试

相同场景分别运行 in-process、local sidecar 和 remote worker backend，比较：

- 最终 Session journal；
- Operation terminal state；
- ToolResult；
- Workspace digest；
- Artifact digest；
- Agent output。

---

## 14. 迁移路线

## Phase 0：事实清点与职责冻结

不搬代码、不引入新语言：

- 标记每个模块的 plane owner；
- 列出全部持久状态及唯一事实源；
- 列出全部副作用和 effect class；
- 列出穿越边界的 Python 对象；
- 建立五层 dependency test；
- 禁止新增没有 operation ID 的副作用。

退出条件：任何状态和副作用均有 owner、truth 和恢复语义。

## Phase 1：Execution Cell 契约

- 建立 Python Execution Cell Ports；
- 定义 execution IDL；
- 定义 Operation envelope、status 和 reconcile；
- 用现有 Python 实现提供 local backend；
- 建立 golden fixture 和 backend parity tests；
- Tool 经 Port 调用，不再直接依赖具体 OS 实现。

退出条件：替换 backend 不需要修改 Kernel 或 Tool 定义。

## Phase 2：Rust Command 垂直切片

- command prepare；
- permit 验证；
- process start；
- stdout/stderr sequence；
- cancellation；
- terminal receipt；
- operation status/reconcile；
- crash journal 和 fencing。

退出条件：Python 与 Rust backend 通过相同恢复矩阵和 parity tests。

## Phase 3：Rust Terminal 与 Sandbox

- durable terminal ID；
- attach/cursor/reconnect；
- stdin sequencing；
- process-tree cleanup；
- sandbox profile；
- resource quota；
- credential injection。

退出条件：Worker 重启、断流和取消不会产生孤儿进程或越权执行。

## Phase 4：Rust Workspace/FileOps

- versioned read view；
- snapshot search；
- prepare/commit mutation；
- revision conflict；
- snapshot/before-image；
- stale epoch rejection；
- artifact publication。

退出条件：所有 mutation 经统一 Operation 模型，崩溃点测试完备。

## Phase 5：Control Plane 契约化

从当前 Python `AgentControl` 后抽出：

- registry；
- mailbox；
- quota；
- placement；
- lease；
- checkpoint locator；
- worker lifecycle。

保留 Python in-memory implementation 作为单机正式 backend。

退出条件：AgentControl 不再要求直接持有所有远程 Worker 的进程内对象。

## Phase 6：Go Control Plane

- Worker registry/heartbeat；
- assignment；
- mailbox cursor；
- global quota；
- lease authority；
- takeover；
- rolling drain。

退出条件：单机与分布式使用相同领域策略，Python 和 Go 不存在重复 placement/takeover 逻辑。

## Phase 7：共享持久化

最后替换本地 file/SQLite 协调实现：

- Session/Operation journal；
- lease；
- artifact metadata；
- durable mailbox；
- event stream。

本地 backend 继续作为开发和单机部署能力，但必须满足相同 Port、恢复和一致性测试。

---

## 15. 迁移与删除纪律

每个迁移必须记录：

- 被替代接口；
- 当前唯一事实源；
- shadow/双写范围；
- cutover 条件；
- rollback 方法；
- 兼容读取期限；
- telemetry；
- 删除日期和 owner。

允许短期双写，但只能有一个权威来源。禁止长期保留：

- 永久 `legacy_*` adapter；
- local/remote 两套业务判断；
- Python/Go 两套 placement policy；
- Python/Rust 两套 permission 语义；
- 新旧事件永久双写；
- 无使用指标的兼容分支；
- 用 feature flag 无限期隐藏未完成迁移。

任何兼容层在合入时必须回答：

```text
当前 source of truth 是谁？
何时切换？
失败如何回滚？
旧路径何时删除？
```

---

## 16. 非目标

本架构明确不追求：

- 把全部 Python 重写为 Go/Rust；
- 为追求“微服务化”拆分低内聚网络服务；
- 通过网络模拟完整 POSIX；
- 宣称普遍 exactly-once；
- 将每次文件读取或 Flow transition 变成 RPC；
- 让 Protobuf 类型直接控制 Kernel；
- 在没有 profiling 的情况下用 FFI 重写纯 Python 逻辑；
- 为未来可能性提前建立无消费者的抽象；
- 在迁移期永久维护两套语义。

---

## 17. 十条不可违反的不变量

1. Kernel 永远不感知部署拓扑。
2. Tool 表达意图，不直接拥有主机资源。
3. Control Plane 决定所有权，Execution Cell 在最终副作用位置强制 fencing。
4. 单机和分布式共享同一业务契约。
5. 跨语言以 IDL 为真相源，进程内以窄 Port 为真相源。
6. 所有副作用都是有稳定 ID、可查询状态的 Operation。
7. 不确定的外部结果进入 `IN_DOUBT`，不得被通用 retry 掩盖。
8. Durable state 不含进程本地对象。
9. 每类状态只有一个权威来源，Projection 必须可重建。
10. 每个兼容层都有删除期限，每条架构规则都有自动化验证。

---

## 18. 近期决策顺序

在引入 Go/Rust 前，优先完成：

1. 状态所有权清点；
2. 副作用清点和统一 Operation 模型；
3. Execution Cell Python Port；
4. Wire IDL 与兼容测试；
5. 让现有 local backend 先经过新 Port；
6. Rust Command 垂直切片；
7. Control Plane Port；
8. Go Control Plane。

最终稳定分工是：

```text
Python    Agent cognition and semantics
Rust      Host execution and safety
Go        Distributed ownership and coordination
Protobuf  Cross-language truth
Journal   Durable event and operation truth
CAS       Durable content truth
```

只有当协议、状态所有权和恢复语义先稳定下来，语言迁移才是可替换实现；否则语言迁移只会把已有耦合复制成跨语言负债。
