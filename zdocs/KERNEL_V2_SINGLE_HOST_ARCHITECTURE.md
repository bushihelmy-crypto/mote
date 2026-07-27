# Mote Kernel V2：单机多进程十年架构审计与目标设计

> 状态：目标架构决策草案
>
> 日期：2026-07-25
>
> Mote 基线：`feaffe7` 及当前未提交五层迁移工作树
>
> 对照基线：Pydantic AI `3adb9b02`（v2.9.0）
>
> 范围：`kernel/`，以及为了落实“每个 Agent 一个进程”而必须稳定的
> `runtime/` 与 `orchestration/` 边界。

---

## 1. 结论

当前 Kernel 需要大改，改动级别应定义为“执行内核换代”，而不是继续扩建
`AgentFlowEngine`。

应保留现有的正确思想：

- `contracts <- kernel <- runtime <- orchestration <- product` 单向分层；
- provider-independent `ModelTurn`；
- capability allowlist 与工具最小权限；
- EffectLedger 的 `unknown-after-crash` 语义；
- OutputContract 与 schema fingerprint；
- system prompt 静态/动态缓存边界；
- Toolset manifest/version；
- 单 Agent 只有一个默认执行内核。

但这些正确零件目前没有被一个统一、可持久化、可重放的状态转换模型约束。
继续给现有 Engine 增加 checkpoint、callback、特殊恢复分支，会造成局部正确、整体
不可证明。

Kernel V2 的核心应收敛为纯 reducer：

```text
KernelDecision = reduce(KernelState, KernelEvent)

KernelDecision:
  next_state
  effect_intents[]
  domain_events[]
```

所有 IO 都由 Agent 进程内的 Runtime Durable Host 执行，并把结果作为
Settlement 重新提交给 Kernel。Agent 之间不直接通信，统一经过本机
`orchestration` 进程间控制面。

---

## 2. 已确定的部署范围

### 2.1 只考虑单机多进程

本设计的唯一目标部署模型是：

```text
Single Host
├── Orchestration Control Process
│   ├── Agent registry
│   ├── Process supervisor
│   ├── Durable mailbox/router
│   ├── Spawn/admission/quota/scheduling
│   └── Local IPC server
│
├── Agent Process A
│   ├── Runtime Durable Host
│   ├── Kernel V2 reducer
│   ├── Model/tool/output adapters
│   └── Per-agent session journal
│
├── Agent Process B
│   └── ...
│
└── Shared local storage
    ├── orchestration control database
    ├── per-agent journals
    └── artifacts/workspaces
```

硬约束：

1. 每个 Agent 是一个独立 OS 进程。
2. 一个 Agent 进程在任一时刻只执行该 Agent 的一个 Kernel transition loop。
3. Agent 不直接持有其他 Agent 的对象、socket 或进程句柄。
4. Agent 间交互全部由 `orchestration/` 路由、持久化和投递。
5. Kernel 不感知进程、IPC、PID、socket、SQLite 或其他 Agent。
6. Runtime 只依赖 `contracts/ports/` 中的窄控制面端口；具体 IPC client 由
   `orchestration` 实现并在装配期注入。

### 2.2 明确不做

未来十年设计不等于提前设计分布式集群。本方案明确不包含：

- 跨机器 Agent placement；
- 远程 Worker 与网络服务发现；
- Raft、分布式共识或跨节点 leader election；
- 跨机网络分区语义；
- 跨地域复制与容灾；
- 通用远程 POSIX/Execution Cell 协议；
- 宣称外部副作用 exactly-once。

本方案的“高可用”表示：单个 Agent 进程、模型调用、工具调用或 orchestration
控制进程崩溃后，可以在同一台机器上自动恢复，不丢已接受消息，不静默重复不可逆
副作用，不污染其他 Agent。

整机损坏、磁盘不可恢复损坏不属于高可用承诺；可通过备份单独解决。

---

## 3. 可验证的“低负债”定义

“零负债”不能是主观口号。本设计采用以下可测试定义：

- 零平行执行内核：迁移结束后只保留 Kernel V2；
- 零重复事实源：Kernel state 只能从 event journal 重建；
- 零永久兼容层：迁移 adapter 必须有删除条件；
- 零隐式副作用：Model、Tool、Output、Wait、IPC 都由 Intent 表达；
- 零隐式恢复：每个 Intent 在崩溃后只有明确的 replay、reconcile 或 in-doubt 结论；
- 零进程内跨 Agent 引用：Agent 交互只能使用稳定 ID 和 IPC envelope；
- 零静默消息丢失：已被 orchestration 接受的消息必须最终投递或进入可查询的失败状态；
- 零无界队列：journal、mailbox、event stream、parser、tool output 都有上限和 retention；
- 零协议泄漏：XML/OpenAI/Anthropic wire 格式不进入 Kernel state；
- 零不可验证分层：依赖方向由架构测试执行；
- 零慢观察者阻塞：日志、Telemetry、UI consumer 不得阻塞 Agent transition；
- 零未知 schema 演进：所有持久 envelope 都有版本和 upcaster。

---

## 4. 当前 Kernel 的关键问题

### 4.1 Engine 持有共享运行态，却允许重复运行

[`kernel/flow/engine.py`](../kernel/flow/engine.py) 中的 `AgentFlowEngine` 持有：

- `_ctx`；
- `_turn_channel`；
- `_current_run_id`；
- `_event_queues`；
- Think task/checkpoint 相关可变状态。

同一实例又公开 `run()` 和 `run_events()`。重复或并发调用会串联 context、channel、
run id 和事件。即使目标部署中每个 Agent 独占进程，也不能依赖调用者永远不并发来
维持正确性。

目标模型必须区分：

```text
AgentDefinition  不可变、可序列化、可复用
AgentProcess     一个 Agent 的进程 incarnation
AgentRun         一次独立运行
KernelState      一次会话可持久重放的业务状态
```

### 4.2 durability 是多个局部 checkpoint 的拼接

当前 Think、Tool、Output 分别拥有 checkpoint/reconcile 路径。
`DurableFlowRunner` 主要给节点附加 `EffectKind` 并调用 started/completed callback，
没有持久化以下统一状态：

- 当前 graph/state version；
- 已提交但未执行的 Intent；
- 已执行但未提交的 Settlement；
- 恢复指令的执行结果；
- Agent definition/graph version。

这无法证明“进程在任意 await 点崩溃后都能恢复到唯一正确状态”。

### 4.3 收件路径存在 pop-before-commit 窗口

`ObservationService.observe()` 先从 `msg_buffer.pop_all()` 移除消息，再写 memory。
两者之间崩溃会丢消息。

在新架构中，Kernel 不再主动 pop 一个进程内 buffer。消息必须由 orchestration
以稳定 `delivery_id` 投递，Agent Runtime 先把 `IngressAccepted` 持久化，再向
orchestration ACK。

### 4.4 工具结果与状态推进没有统一提交边界

`ActionExecutionService` 在记录结果后 reap think journal，但结果持久化、EffectLedger
settlement、Kernel state 推进不是一个统一的 append/CAS 事务。

V2 中工具必须遵循：

```text
ToolIntentRecorded
→ ToolExecutionStarted
→ ToolSettlementRecorded
→ Kernel consumes settlement
```

Kernel 只消费已持久化 Settlement，不直接等待或调用 ToolExecutor。

### 4.5 Graph 扩展表面开放、实际封闭

`NodeId` 是固定 Enum。新增 topology 必须修改核心枚举；`EffectKind` 又只是节点元数据，
没有成为 durable protocol。

V2 不以“任意 Python Node 类”作为核心扩展点。扩展优先通过：

- typed event；
- typed effect；
- policy；
- reducer module；
- versioned graph definition。

如果需要稳定步骤身份，使用 namespaced key，而不是中央 Enum：

```text
mote.react.observe@1
mote.react.request_model@2
product.coding.validate_output@1
```

### 4.6 Kernel 混入 Product、Runtime 与 wire 语义

当前 Kernel 中仍可见：

- Coding Agent 默认 prompt 和默认工具列表；
- PromptBuilder 对 config、executor、skill manager、turn context bus 的 `Any` 依赖；
- MEMORY.md 文件读取；
- LLM/provider 能力判断；
- XML/Native 的记录和 Toolset 投影差异；
- 对 Runtime 类型的 `TYPE_CHECKING` 引用。

正确归属：

| 能力 | 归属 |
| --- | --- |
| reducer、状态转换、完成语义 | Kernel |
| Provider-independent message/effect/output DTO | Contracts |
| Coding Agent prompt、默认工具、role charter | Product |
| MEMORY.md、Skills、turn context 的读取 | Runtime |
| Provider wire request/response | Runtime model adapter |
| XML 解析与 legacy 投影 | Runtime compatibility adapter |
| Agent 间路由、唤醒、spawn、quota | Orchestration |

### 4.7 类型穷尽性不足

`CompletionKind` 包含 `COMPLETE`，但默认 ReAct interpret 路径没有明确处理，最终落入
ACT。类似 union 分支必须使用 `match` 和 `assert_never`，使新增 variant 在类型检查期
强制所有 reducer 更新。

### 4.8 Event/Telemetry 可反向阻塞执行

当前 run event 对每个 bounded queue 执行 `await queue.put()`。慢消费者可以冻结
Agent。观察平面必须改成 journal cursor 或非阻塞 fan-out；溢出只能影响观察完整度，
不能改变 Kernel 语义。

### 4.9 XML parser 不是十年核心协议

当前 XML parser 的接收缓冲、命令数、参数长度和解析耗时缺少完整硬限制。
XML 应降级为 legacy adapter：

- 有总字节、单参数、命令数、嵌套和时间上限；
- 只产出统一 normalized response；
- 不参与 Toolset 逻辑身份；
- 不再承载新核心能力。

---

## 5. Pydantic AI 对照结论

Pydantic AI 是参考组，不是目标模板。

### 5.1 应直接借鉴

#### 独立 AgentRun

Pydantic AI 的每次执行由独立 `AgentRun` 持有状态，优于 Engine 实例共享 run state。
Mote 应进一步让 `AgentRun` 可通过 event journal 重建。

#### 统一 normalized messages

Pydantic AI 的 `messages.py` 使用 request/response/part/event discriminated union，所有
provider、UI 和持久化都围绕它 round-trip。Mote 应采用同一思想，但持久格式必须增加：

- envelope `schema_version`；
- event/part 独立版本；
- upcaster registry；
- 未知 optional part 的明确策略；
- golden fixture。

#### Model/Profile 分离

provider wire 行为放在 models/adapters，模型能力事实放在 profile。Kernel 不按 model
name 或 provider name 分支。

#### RunContext 泛型

`RunContext[DepsT]` 值得采用，但工具仍只能得到通过 projector 生成的窄
`ToolContext[ToolDepsT]`，不得获得完整 Agent state。

#### Toolset wrapper 代数

Filter、Prefix、Rename、Prepare、Approval、Combine 应组合在一套 Toolset 抽象上，
不能因 XML/Native 复制两套组合体系。

#### typed union 穷尽检查

所有 Kernel event、effect、settlement 和 completion 分支都必须 `assert_never`。

### 5.2 不应照搬

- `_agent_graph.py` 是少数大型节点，节点内部混合 model request、tool、output、retry、
  streaming，不适合作为纯 Kernel 范本；
- `GraphAgentDeps` 和 `GraphAgentState` 仍持有大量可变 manager/cache/history；
- durable execution 主要依赖 Temporal/DBOS/Prefect wrapper；
- Toolset 定义、lifecycle 和执行仍处于同一抽象；
- 消息历史没有面向十年存储的显式 schema version；
- ContextVar 与 `Any` 仍很多；
- AgentSpec 自身也没有完整的 definition/version/digest 治理。

### 5.3 Mote 必须超越的部分

- 内建本地 durable host，不依赖外部 workflow 平台；
- 所有外部交互统一 Intent/Settlement；
- durable mailbox 与 Agent ingress ACK；
- Agent process incarnation/epoch；
- 版本化 reducer/state/event；
- 从每个 event prefix 确定性 replay；
- 崩溃注入验证，而不是只验证正常路径。

---

## 6. Kernel V2 目标模型

### 6.1 Kernel 是纯 reducer

建议核心协议：

```python
@dataclass(frozen=True, slots=True)
class KernelDecision(Generic[StateT]):
    state: StateT
    effects: tuple[EffectIntent, ...] = ()
    events: tuple[KernelDomainEvent, ...] = ()


class KernelReducer(Protocol[StateT]):
    definition_id: str
    definition_version: str
    state_schema_version: int

    def reduce(
        self,
        state: StateT,
        event: KernelEvent,
    ) -> KernelDecision[StateT]: ...
```

硬要求：

- `reduce()` 无 async、无 IO、无 clock、无 random、无环境变量读取；
- 输入完全决定输出；
- state 可序列化；
- event 可序列化；
- effect intent 可序列化；
- 每个 decision 有稳定 state hash；
- replay 不执行 effect；
- 所有非确定值由 Runtime 先生成事件再交给 Kernel。

例如时间和 UUID：

```text
错误：Kernel 内调用 datetime.now()/uuid4()
正确：Runtime 提交 ClockObserved/IdAllocated 事件
```

### 6.2 KernelState

State 只保存推进决策必需的数据：

```text
KernelState
  definition_id/version
  state_schema_version
  run_id
  phase
  model_context projection/cursor
  pending_intents
  accepted_ingress_ids
  output_state
  budget_state
  terminal_state
```

禁止保存：

- LLM client、ToolExecutor、ContextManager；
- asyncio Task/Event/Queue/Lock；
- file descriptor、PID、socket；
- observer/subscriber；
- live Toolset lifecycle；
- Runtime service locator；
- 其他 Agent 的对象。

完整历史不一定复制进 state。大对象可由 artifact/message reference 表达，但引用内容必须
不可变且由 digest 校验。

### 6.3 KernelEvent

最小事件族：

```text
RunRequested
IngressAccepted
ModelSettled
ToolSettled
OutputValidated
TimerFired
HumanInputSettled
InterAgentSendSettled
BudgetUpdated
CancellationRequested
RecoveryResolved
```

事件表达已经发生且被 durable host 接受的事实；命令意图不能伪装成事件。

### 6.4 EffectIntent

最小 effect 族：

```text
RequestModel
InvokeTool
ValidateOutput
WaitUntil
RequestHumanInput
SendInterAgentMessage
PublishArtifact
EmitTerminalOutput
```

每个 Intent 至少包含：

```text
intent_id             确定性、会话内唯一
run_id
kind
payload
payload_schema_version
idempotency_key
effect_class          pure/local/external/waitable
timeout_policy
retry_policy
```

重试策略属于 Runtime policy；Kernel 只接收最终 settlement 或需要业务决定的失败事件。

### 6.5 EffectSettlement

```text
EffectSettlement
  intent_id
  attempt_id
  status: succeeded | failed | cancelled | in_doubt
  result/error/artifact refs
  started_at/settled_at observations
  executor identity
  schema_version
```

外部副作用无法证明结果时必须是 `in_doubt`，不能自动重试。现有 EffectLedger 的
`unknown-after-crash` 应收敛成这一正式状态。

---

## 7. Agent 进程内 Runtime Durable Host

每个 Agent 进程只有一个 Durable Host，职责是：

1. 从 per-agent journal replay KernelState；
2. 从 orchestration 接收 ingress envelope；
3. 先持久化 `IngressAccepted`，再 ACK delivery；
4. 调用 reducer；
5. 原子追加 decision/state hash/Intent；
6. 调度未 settlement 的 Intent；
7. 持久化 Settlement；
8. 将 Settlement 作为事件再次 reduce；
9. 发布只读 run events；
10. 在 shutdown 时停止接收新 ingress、结算可结算工作并安全退出。

核心循环：

```text
receive external event
  → append event with expected_version
  → reduce(previous_state, event)
  → append decision + intents + state_hash
  → commit/fsync boundary
  → ACK external producer if applicable
  → execute ready intents
  → append settlements
  → repeat
```

现有 `runtime/events/` 已有 append、stream version、checksum、CAS conflict 等基础，
应扩展为 Durable Host 的唯一 append authority，不再创建独立 think journal、flow journal、
output journal 等平行事实源。

Snapshot 只是 replay 加速器，不是事实源：

- snapshot 带 source event version 和 state schema version；
- 校验失败时丢弃并从 journal replay；
- compaction 必须保留 audit/recovery 所需 settlement；
- snapshot 写入失败不得影响 journal 正确性。

---

## 8. 本机 Orchestration 进程间控制面

### 8.1 所有权

`orchestration` 控制进程拥有：

- Agent logical identity 与 path；
- Agent process incarnation；
- spawn/admission policy；
- 进程启动、终止、重启和状态；
- durable mailbox；
- Agent 间地址、channel、subtree routing；
- trigger-turn/queue-only 投递语义；
- 全机 Agent 数量、并发运行数、成本和资源 quota；
- orchestration IPC server；
- delivery retry 与 dead-letter 状态。

Agent 进程拥有：

- 自己的 Kernel state/journal；
- 自己的模型、工具和 output effect；
- 自己的 Runtime 生命周期；
- 对已接受 `delivery_id` 的 durable dedup；
- 自己的 terminal outcome。

### 8.2 身份必须分层

```text
agent_id         逻辑 Agent，跨重启稳定
agent_path       orchestration 路由名，可映射到 agent_id
session_id       持久会话身份
incarnation_id   一次 Agent 进程实例
epoch            incarnation 单调递增 fencing token
pid              仅诊断，不作为稳定身份
run_id           一次 Kernel run
delivery_id      一次消息投递
message_id       一条逻辑消息
intent_id        一次 Kernel effect
```

即使只有一台机器也需要 epoch。旧进程可能在被判定超时后恢复运行；它提交的 heartbeat、
ACK、状态和消息都必须因 epoch 过期而被拒绝，避免两个 incarnation 同时代表同一 Agent。

这不是分布式共识，而是本机进程 fencing。

### 8.3 IPC 边界

Runtime 不得 import `orchestration`。在 `contracts/ports/` 定义窄端口，例如：

```python
class AgentControlClient(Protocol):
    async def register(self, hello: AgentHello) -> AgentWelcome: ...
    async def receive(self, cursor: DeliveryCursor) -> DeliveryBatch: ...
    async def ack(self, receipt: DeliveryReceipt) -> None: ...
    async def publish(self, message: OutboundAgentMessage) -> PublishReceipt: ...
    async def heartbeat(self, status: AgentHeartbeat) -> HeartbeatAck: ...
```

具体本机 IPC transport 位于 `orchestration/`，可使用 Unix domain socket 或平台等价
实现，但 transport 不进入业务协议。

所有 frame 必须：

- length-prefixed；
- 有 protocol version；
- 有最大 frame/batch/attachment metadata 大小；
- 有 request id、deadline 和明确错误码；
- 携带 agent_id/incarnation_id/epoch；
- 校验本机 peer 身份或启动时下发的随机 capability token；
- 大内容只传 ArtifactRef，不内联进 IPC frame。

### 8.4 Agent 到 Agent 的可靠消息流

Agent A 发送到 Agent B：

```text
1. Kernel A 生成 SendInterAgentMessage intent(message_id)
2. Runtime A 调 orchestration.publish(message_id, recipient, payload_ref)
3. Orchestration 在本地事务中：
   - 幂等插入 message
   - 解析 recipient
   - 创建 delivery(delivery_id, target_agent_id)
4. Orchestration commit 后 ACK A
5. Runtime A 记录 InterAgentSendSettled
6. Orchestration 向 B 当前 incarnation 投递 delivery
7. Runtime B 将 IngressAccepted(delivery_id, message_id) 写入自己的 journal
8. Runtime B 完成 durable barrier 后 ACK delivery
9. Orchestration 将 delivery 标记为 acknowledged
```

任意位置崩溃的结果：

- A 在第 2/3 步附近崩溃：按 `message_id` 重试，orchestration 幂等返回同一逻辑结果；
- orchestration 在第 3/4 步附近崩溃：本地事务决定消息存在或不存在，不产生半条 delivery；
- B 在第 6/7 步附近崩溃：未 ACK，重启后重投；
- B 在第 7/8 步附近崩溃：重投后由 `delivery_id` durable dedup；
- 第 8/9 步附近 orchestration 崩溃：重投仍由 B dedup。

保证的是 at-least-once delivery + exactly-once acceptance，不宣称 handler 或外部副作用
exactly-once。

### 8.5 Durable mailbox

内存 `list + asyncio.Event` 不能作为进程间真相源。建议 orchestration 使用单个本机
SQLite WAL 数据库保存控制面状态：

```text
agents
  agent_id, session_id, path, desired_state, definition_ref

incarnations
  agent_id, incarnation_id, epoch, pid, status, heartbeat_at

messages
  message_id, sender_id, payload_ref, created_at

deliveries
  delivery_id, message_id, target_id, mode, status, attempt, available_at

routes/channels
  stable routing metadata

spawn_requests
  idempotent spawn state
```

选择 SQLite 是单机约束下的优雅解：事务、WAL、唯一约束、崩溃恢复、可查询性都已成熟，
无需引入 broker 或分布式数据库。

原则：

- orchestration DB 是 Agent identity/mailbox/process desired state 的唯一事实源；
- per-agent journal 是该 Agent Kernel state 的唯一事实源；
- 两者不做伪跨库事务，通过稳定 ID、ACK 和幂等协议收敛；
- payload 大于阈值时落 ArtifactStore，只存 digest/reference；
- queue 有配额、TTL、dead-letter 和诊断接口；
- `QUEUE_ONLY` 与 `TRIGGER_TURN` 是 delivery 属性，重启后仍保留。

### 8.6 Process supervision

Agent 生命周期建议：

```text
DECLARED
→ STARTING
→ ONLINE
→ QUIESCENT / RUNNING
→ STOPPING
→ EXITED
→ RESTARTING | TERMINAL
```

监督规则：

- orchestration 创建进程前先持久化 desired state 和新 epoch；
- Agent 完成 IPC handshake 后才进入 ONLINE；
- 进程退出由 OS child-process primitive 检测，heartbeat 只补充卡死检测；
- 意外退出按 policy 重启并递增 epoch；
- restart backoff 有上限和熔断，防止 crash loop；
- 正常 terminal Agent 不自动重启；
- shutdown 先停止投递新 trigger，再请求 Agent drain，超时后升级终止；
- PID 不复用为身份；所有更新同时校验 incarnation_id 与 epoch。

### 8.7 调度语义

每 Agent 独立进程后，orchestration 不再直接调用 `Role.run()` 或把消息 push 进其
`msg_buffer`。调度器只做：

- 决定何时发送 `RunRequested`/wake envelope；
- 控制同时 RUNNING 的 Agent 数量；
- 对进程施加本机资源 quota；
- 观察 run status；
- 保留 queue-only/trigger-turn 语义。

Agent 进程内部的 Durable Host 决定如何从 durable ingress 推进 Kernel。这样
orchestration 不依赖 Kernel node、Role 或 ContextManager。

---

## 9. Prompt、Model、Toolset 与 Output 边界

### 9.1 Prompt

Kernel 只拥有 provider-independent `PromptPlan`：

```text
PromptPlan
  static_instruction_blocks
  dynamic_instruction_blocks
  model_context_refs
  current_user_input
  ephemeral_context_blocks
  output_contract_ref
  tool_catalog_ref
```

Product 提供 Coding Agent prompt；Runtime 读取 MEMORY.md、Skills、git、LSP 和其他
per-turn context；model adapter 决定 provider 缓存标记和 wire 投影。

### 9.2 Model

Kernel 生成 `RequestModel` intent，不持有 LLM client。Runtime 根据 model/profile 解析：

- tool 支持；
- structured output；
- thinking/reasoning；
- prompt caching；
- streaming；
- provider-specific request/response。

流式 token 是观察事件，不直接改变 Kernel state；完整 normalized response settlement
才推进 reducer。

### 9.3 Toolset

逻辑身份与协议投影必须分离：

```text
ToolCatalogSnapshot
  toolset_id
  semantic_version
  definition_digest
  tool definitions

ProtocolProjection
  protocol
  projection_version
  catalog_digest
```

不应继续把 XML/Native 放入同一 Toolset 的逻辑 identity。Toolset wrapper 只改变 catalog
view 或 call policy，不绕开 Runtime permission/effect/settlement 流水线。

Toolset lifecycle 属于 Runtime；Kernel 只看到某个不可变 catalog snapshot reference。

### 9.4 Output

保留并强化现有 OutputContract：

- contract id/version；
- schema fingerprint；
- decoder/validator version；
- representation 与 provider capability 分离；
- `ValidateOutput` 也是 effect intent；
- committed output 是 Kernel terminal event；
- output schema 变化需要显式 migration，不允许静默 reinterpret 历史结果。

---

## 10. 观察、流式与背压

稳定公开 RunEvent 与 durable KernelEvent 不是同一概念：

```text
KernelEvent   恢复所需事实，持久、严格版本化
RunEvent      SDK/UI 语义事件，可由 Kernel journal 投影
Telemetry     诊断观察，允许采样或丢弃
```

规则：

- Kernel transition 不 await UI/Telemetry consumer；
- RunEvent consumer 使用 cursor，慢消费者自行追赶；
- 实时队列溢出时返回 gap/cursor，不阻塞 producer；
- terminal event 在 durable commit 后发布；
- IPC event stream 有单独配额，不能挤占 mailbox/control frame；
- observer 异常不改变 Agent outcome。

---

## 11. 版本治理

十年演进的持久类型至少带以下版本：

```text
AgentDefinition.version
KernelReducer.definition_version
KernelState.schema_version
KernelEvent.schema_version
EffectIntent.payload_schema_version
EffectSettlement.schema_version
ToolCatalogSnapshot.semantic_version + digest
OutputContract.version + schema fingerprint
Orchestration IPC protocol_version
Orchestration DB schema_version
Artifact manifest version
```

升级规则：

1. 旧 event 通过纯 upcaster 升级到 reducer 当前输入版本；
2. upcaster 不做 IO；
3. upcaster 链有 golden fixture；
4. 不支持的版本 fail closed，不猜测；
5. reducer 行为变更必须提升 definition version；
6. 活跃会话升级前先做 replay compatibility check；
7. migration adapter 达成删除条件后直接删除，不长期双轨。

---

## 12. 迁移策略

### Phase 0：冻结与止血

- 冻结 `AgentFlowEngine` 新特性；
- 增加 per-Agent single-flight；
- 修复 mailbox/observation 的 pop-before-commit；
- 工具结果 durable barrier 后才能 reap；
- `CompletionKind` 分支穷尽；
- RunEvent/Telemetry 不得阻塞执行；
- XML parser 增加硬上限；
- 为当前行为建立 replay/crash 基线测试。

### Phase 1：建立 V2 Contracts

- 新增版本化 KernelState/Event/Intent/Settlement；
- 新增 AgentDefinition 与 digest；
- 新增 Orchestration IPC DTO/ports；
- 新增 ToolCatalogSnapshot；
- 明确 OutputContract migration；
- 所有 union 加 `assert_never`。

此阶段不再增加第二套业务事实源；V2 journal 必须落在现有 Runtime event fabric 上。

### Phase 2：Orchestration 进程化

- 将 AgentControl 变成本机独立控制进程；
- 将 `AgentRuntime(role, asyncio.Task)` 替换为 process descriptor/incarnation；
- 将 Mailbox/PendingDelivery 迁移到 SQLite durable delivery；
- 建立 IPC handshake、epoch、heartbeat、ACK 和 publish；
- 每个 Agent 启动为独立进程；
- 保持现有 AgentPath、CommGraph、spawn policy、quota 的领域语义。

### Phase 3：Kernel V2 shadow replay

对同一已持久输入，让旧 Engine 和 V2 reducer 产生语义比较结果：

- model request；
- tool intent；
- output candidate；
- terminal outcome；
- budget usage。

Shadow 不执行第二遍外部 effect。差异写诊断报告，不自动改变生产结果。

### Phase 4：切换执行权

建议顺序：

1. ingress + run lifecycle；
2. model intent；
3. pure/local tool；
4. external tool + in-doubt reconcile；
5. output commit；
6. wait/human input；
7. inter-agent send intent。

### Phase 5：删除旧内核

删除：

- AgentFlowEngine 共享运行态路径；
- ThinkCheckpoint/局部 flow recovery；
- Runtime 直接调用 graph node 的路径；
- 进程内跨 Agent Role/AgentRuntime 引用；
- 内存 Mailbox/PendingDelivery 真相源；
- XML/Native 重复 Toolset 组合体系；
- 迁移期 dual-write/shadow adapter。

只有完成删除，才能认为“单执行内核、零永久兼容层”达成。

---

## 13. 测试与验收门槛

### 13.1 Kernel determinism

- 同一 state/event 得到 byte-equivalent decision；
- 从每个 event prefix replay 得到相同 state hash；
- reducer 禁止 IO/clock/random 的架构测试；
- event union 分支静态穷尽；
- state/event/upcaster property tests。

### 13.2 Crash matrix

在每个 durable boundary 前后注入进程终止：

- ingress append/ACK；
- decision append；
- effect start；
- effect settlement；
- output commit；
- inter-agent publish；
- delivery/accept ACK；
- snapshot/compaction。

每个 crash point 必须得到唯一结论：安全 replay、幂等 retry、reconcile 或 `in_doubt`。

### 13.3 Orchestration 多进程

- 同时启动多个 Agent，每个 PID 不同；
- orchestration 重启后 registry/mailbox 恢复；
- Agent 崩溃后 epoch 增长并重启；
- 旧 incarnation 的 heartbeat/ACK/publish 被拒绝；
- delivery 在 Agent 崩溃窗口不丢失；
- 重复 publish/message/delivery 幂等；
- queue-only 不错误触发 turn；
- trigger-turn 在重启后仍触发；
- crash loop/backoff/quota 生效；
- IPC frame 超限、畸形和身份不匹配 fail closed。

### 13.4 Effect safety

- pure/local effect 可安全 replay；
- external effect started 后崩溃进入 reconcile/in-doubt；
- 没有 settlement 时 Kernel 不推进；
- settlement 重复提交幂等；
- tool permission 无法被 wrapper/IPC/recovery 绕过。

### 13.5 Backpressure 与资源

- 慢 RunEvent/UI/Telemetry consumer 不阻塞 Kernel；
- mailbox、journal、artifact、tool output 有硬上限；
- Agent process 退出后无孤儿 subprocess/task/socket；
- orchestration shutdown/restart 不丢 committed delivery；
- 磁盘满、SQLite busy/corrupt、journal checksum 错误显式失败。

### 13.6 最终验收清单

- [ ] 每 Agent 一个 OS 进程；
- [ ] Agent 之间只经 orchestration IPC；
- [ ] orchestration mailbox durable；
- [ ] Kernel 是纯 reducer；
- [ ] Runtime 统一执行所有 Intent；
- [ ] 所有 Settlement 先持久化再推进；
- [ ] 任意 journal prefix 可确定性 replay；
- [ ] 外部副作用未知结果显式 `in_doubt`；
- [ ] 所有持久 schema 可迁移；
- [ ] 慢观察者不阻塞执行；
- [ ] XML 只是 bounded legacy adapter；
- [ ] Product prompt 和默认工具不在 Kernel；
- [ ] 旧 Engine/checkpoint/mailbox 双轨已删除。

---

## 14. 最终架构判断

Pydantic AI 证明了 normalized messages、AgentRun、ModelProfile、RunContext 和 Toolset
wrapper 的价值，但它的 graph 与 durable wrapper 不应成为 Mote 的终局。

Mote 的差异化应是：在单机多进程这一明确范围内，把 Agent 执行和 Agent 间通信都做成
可恢复、可证明、可演进的本地系统，同时保持 Kernel 极纯、Runtime 有唯一 durable
authority、Orchestration 有唯一进程间控制权。

因此最终决策是：

> 保留当前正确契约，冻结并替换现有 AgentFlowEngine；建设纯 Kernel reducer、每 Agent
> 独立 Runtime Durable Host，以及本机独立 Orchestration IPC 控制面。不要继续通过局部
> checkpoint 和进程内对象共享扩建现有执行模型。

这次改动可以很大，但最终概念必须更少：

```text
Definition
State + Event
Intent + Settlement
Agent Process
Orchestration Delivery
```

除此之外的执行状态、恢复状态和跨 Agent 状态都应当是这些概念的投影，而不是新的事实源。
