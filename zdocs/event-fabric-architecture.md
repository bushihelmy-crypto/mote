# Mote Event Fabric：十年目标架构

> Status: target architecture
>
> Horizon: 10+ years
>
> Scope: control policy, durable facts, delivery, projection, telemetry, recovery,
> schema evolution, extension authority, and lifecycle.
>
> 本文描述最终收敛态，不是当前实现说明，也不以兼容旧 `EventBus` API 为设计目标。
> 对未来事件与控制基础设施的设计决策，本文取代旧的 EventBus/control-plane 方案；旧文档仅作历史记录。

## 1. 结论

Mote 的长期事件基础设施收敛为四个彼此独立、由一个 composition root 装配的子系统：

1. **领域专属 Policy Pipeline**：在动作发生前做改写、裁决和授权；
2. **Durable Event Journal**：记录已经发生且恢复所需的不可变事实；
3. **Reliable Event Stream + Projection**：从 committed facts 构建持久或可重建视图；
4. **Telemetry Stream**：承载 token、UI、trace 等有界、允许降级的实时信号。

它们可以由一个 `EventFabric` 门面统一持有，但不能共享模糊的 `emit()` 语义：

```text
                         ┌─────────────────────────────┐
 Intent / Request ──────▶│ Domain Policy Pipeline      │
                         │ transform → policy → gate   │
                         └──────────────┬──────────────┘
                                        │ typed decision
                                        ▼
                                  Domain operation
                                        │
                              immutable recoverable facts
                                        ▼
                         ┌─────────────────────────────┐
                         │ Durable Event Journal       │
                         │ CAS → append → flush/fsync  │
                         └──────────────┬──────────────┘
                                        │ committed envelope
                           ┌────────────┴────────────┐
                           ▼                         ▼
                Reliable Event Stream       Telemetry Stream
                ack/retry/checkpoint         bounded/lossy/live
                           │                         │
                    ┌──────┴──────┐          ┌──────┴──────┐
                    ▼             ▼          ▼             ▼
                Projection    Reconciler    CLI/LSP      Tracing
```

核心边界只有三句话：

- **Policy 回答“允不允许、以什么形式执行”；Fact 回答“什么已经发生”。**
- **恢复依赖的事实必须先持久化，再对外宣称成功。**
- **扩展可以收紧或修饰策略，但不能移除系统不变量。**

## 2. “零负债”的工程定义

绝对意义的零技术债不可证明。本文中的“零负债”是以下可自动验证的目标：

- 一个语义只有一个权威事实源；
- 没有新旧事件系统永久双轨；
- 没有构造函数自注册、隐藏后台任务或隐式资源 owner；
- 没有用日志和 counter 代替错误传播与健康状态；
- 没有无界队列、无界 replay、无界 retry 或无界 retention；
- 没有“理论 exactly-once”承诺，只有可验证的投递和幂等语义；
- 没有用通用 dict 表达安全决策；
- 没有允许插件绕过 permission、sandbox、commit 等核心约束；
- 所有 crash point 都有唯一、幂等、可测试的恢复结论；
- 所有持久 schema 都有版本、迁移、兼容测试和退役机制；
- 所有组件都遵守 `contracts <- kernel <- runtime <- orchestration <- product`；
- 最终迁移完成后删除旧入口、adapter、re-export 和旧测试。

优雅不等于类型或文件最少，而是概念边界少、语义单一、失败行为明确。

## 3. 术语

| 术语 | 定义 |
| --- | --- |
| Intent | 尚未执行的领域请求，可以被拒绝或约束 |
| Decision | Policy Pipeline 对 Intent 的类型化裁决 |
| Policy | 不产生领域副作用的裁决或受控变换 |
| Core Policy | 框架不变量，不能由插件移除、降级或重排 |
| Extension | 领域显式开放的受限 Policy 插槽 |
| Fact | 已经发生的不可变领域事实，不能被 veto 或 rewrite |
| Envelope | Fact 的稳定身份、顺序、因果和 schema 元数据 |
| Journal | recoverable fact 的权威 append-only 存储 |
| Projection | 由 committed facts 确定性构建的读模型 |
| Subscription | 一个有身份、有 mailbox、有 checkpoint 的消费者 |
| Telemetry | 不参与正确性、允许有界丢弃的运行信号 |
| Reconciliation | 外部世界与 journal 结论不确定时的确定性对账 |

禁止继续使用“控制事件”描述 Intent。动作发生前的是 Policy 输入，动作发生后才是 Event/Fact。

## 4. 总体不变量

### 4.1 Policy 不变量

1. 每个会改变系统状态的入口必须显式调用其领域 Policy Pipeline；
2. correctness-critical Policy 不能通过 ambient context 查找，必须由宿主显式持有；
3. Core Policy roster 在运行开始前 seal，运行中不能删除或重排；
4. Extension 只能注册到领域预先声明的 slot；
5. Extension 的最大 authority 由 slot 决定，而不是插件自行声明；
6. 所有 rewrite 都产生有序、可审计、脱敏的 provenance；
7. 最终安全检查基于所有 rewrite 之后的最终 Intent；
8. blocking decision 是单调的，后续步骤不能把 deny 改回 allow；
9. timeout、cancel、failure 分别建模，不用一个异常分支混为一谈；
10. fail-open 只允许用于 advisory extension，安全 gate 必须 fail-closed。

### 4.2 Journal 不变量

1. Journal 只存不可变事实，不存待决策请求；
2. 同一 stream 的 sequence 严格单调且不重复；
3. append 使用 `expected_version` 防止并发覆盖；
4. `append` 成功即表示达到 backend 承诺的 crash-safe durable commit；
5. Journal 不提供可由调用方选择的弱持久等级；不需要恢复的信号必须走 Telemetry；
6. 未提交记录不会进入 reliable stream；
7. journal 内容带 checksum，损坏必须显式报错，不能跳过后伪装完整；
8. replay 和 live projection 使用同一 reducer；
9. snapshot 是缓存，不是第二事实源。

### 4.3 Delivery 不变量

1. 每个 subscriber 有独立有界 mailbox；
2. 同一 subscriber 的 per-stream 顺序稳定；
3. 不同 subscriber 之间允许并发，彼此故障隔离；
4. durable delivery 使用 at-least-once + idempotent handler + checkpoint；
5. ack 只能在 handler 的副作用持久化后推进；
6. retry 有上限、退避、jitter 和 poison-event 隔离；
7. 关键 subscriber 失败进入 degraded/unavailable，不允许静默吞掉；
8. live subscriber 可以 drop/coalesce，但必须计量；
9. emitter 不顺序 await 所有 UI、LSP、tracing subscriber；
10. shutdown 必须 stop producer、drain、checkpoint、close，不能遗留孤儿任务。

## 5. 领域专属 Policy Pipeline

### 5.1 为什么是领域专属

Tool、Spawn、Prompt、Compaction、Completion 回答的问题不同。它们不能共享一个字段不断膨胀的
`ControlOutcome`，也不应该靠字符串 event name 和任意 dict 传参。

领域 facade 暴露业务语言：

```python
tool_decision = await tool_policy.authorize(tool_intent)
spawn_decision = await spawn_policy.admit(spawn_intent)
prompt_decision = await prompt_policy.process(prompt_intent)
compaction_plan = await compaction_policy.plan(compaction_intent)
completion_decision = await completion_policy.decide(completion_intent)
```

底层可以复用同一个类型化执行内核，但 facade、Intent、Decision、Stage 和 authority 均由领域拥有。

### 5.2 通用执行内核

```python
IntentT = TypeVar("IntentT")
DecisionT = TypeVar("DecisionT")

class PolicyStep(Protocol[IntentT, DecisionT]):
    identity: PolicyIdentity

    async def evaluate(
        self,
        intent: IntentT,
        context: PolicyContext,
    ) -> PolicyContribution[IntentT, DecisionT] | None: ...

class PolicyPipeline(Protocol[IntentT, DecisionT]):
    async def evaluate(self, intent: IntentT) -> PolicyResult[IntentT, DecisionT]: ...
```

通用内核只负责：

- 校验 sealed manifest；
- 按领域 stage 和装配顺序确定性执行；
- 应用 host-provided timeout；
- 执行 host-provided failure policy；
- 组合领域定义的 contribution；
- 维护 deny 单调性；
- 记录 provenance；
- 返回最终 Intent、Decision 和 PolicyTrace。

它不知道什么是工具、prompt 或 spawn，也不包含领域 if/else。

### 5.3 Core、Extension 与配置

“领域专属”不等于“每个用户可以替换整条管线”。权限分为三层：

| 层级 | 所有者 | 可变性 |
| --- | --- | --- |
| Core Policy | framework/runtime | 不可移除、不可降级、不可重排越界 |
| Extension Slot | product/deployment | 只能安装满足 slot authority 的 typed extension |
| Policy Configuration | project/end user | 只能在已声明范围内配置参数 |

Extension authority 是闭集：

```python
class ExtensionAuthority(Enum):
    ADVISE = "advise"
    ENRICH = "enrich"
    REWRITE = "rewrite"
    NARROW = "narrow"
    DENY = "deny"
```

没有 `BYPASS`、`REMOVE_CORE` 或 `FORCE_ALLOW`。允许 rewrite 的 slot 必须声明可改字段集合；允许
deny 的 extension 可以收紧，但不能推翻 core deny。

### 5.4 Extension manifest

```python
@dataclass(frozen=True)
class PolicyExtensionSpec:
    identity: PolicyIdentity
    domain: PolicyDomain
    slot: str
    authority: frozenset[ExtensionAuthority]
    timeout: float
    failure_behavior: ExtensionFailureBehavior
    capabilities: frozenset[str]
```

约束：

- `authority` 必须是 slot 上限的子集；
- timeout 和 failure behavior 最终由 host 收紧；
- capability 采用白名单注入，extension 看不到 Role/RoleState；
- 同 identity/version 重复注册直接失败；
- 顺序由固定 stage + composition manifest 决定，不接受任意 magic priority；
- pipeline start 后 seal；
- provenance 记录 extension identity、版本、耗时、结论和脱敏 rewrite 摘要。

### 5.5 领域管线目录

#### ToolCallPolicy

```text
NormalizeArgs              core
  → TrustedTransform       restricted extension slot
  → OrganizationPolicy    deny/narrow extension slot
  → PermissionPolicy      core, fail-closed
  → SandboxPolicy         core, fail-closed
  → QuotaPolicy           core, fail-closed
  → FinalAuthorization    core, sealed
```

规则：

- permission facts 从最终 rewritten args 重新计算；
- credential 只在授权完成后的执行边界注入，extension 永远看不到明文；
- extension 可以收紧 target 或 deny，不能扩大 permission target；
- effectful call 的执行前必须记录 durable intent/idempotency key；
- authorization 通过不代表执行已经发生，不产生 `ToolCompleted` fact。

#### ToolSettlementPolicy

工具已经执行后不能再声称“阻止了工具”。后处理拆成两个语义：

1. effect settlement：记录成功、失败或 unknown-after-crash；
2. presentation policy：决定模型和 UI 能看到的安全结果。

```text
RawResult
  → SecretClassification     core
  → VaultSensitivePayload    core
  → SafeRepresentation       core
  → ResultEnrichment         extension slot
  → ModelVisibilityPolicy    core
```

Journal 只写安全 representation 或加密 vault reference，不把 secret 明文写入 event、trace 或 provenance。

#### PromptPolicy

```text
NormalizePrompt
  → CaptureAndVaultSecrets   core
  → SafePromptView
  → ProjectEnrichment        extension slot
  → OrganizationPolicy      deny/narrow extension slot
  → FinalLeakCheck           core
```

原始 secret 在任何第三方 hook、日志、模型或 journal 之前被 vault。用户可扩展上下文，但不能关闭最终泄漏检查。

#### SpawnAdmissionPolicy

```text
ResolveLineage
  → FrameworkDepthLimit     core
  → ResidencyCapacity       core
  → FleetCostBudget         core
  → OrganizationPolicy      deny/narrow extension slot
  → ReserveIdentity         domain operation
```

extension 只能收紧限制。身份、nickname、residency reservation 仍由唯一 birth authority 原子管理。

#### CompactionPolicy

```text
MeasurePressure
  → PreservationInvariant   core
  → StrategySelection       named-profile extension slot
  → BudgetValidation        core
  → BuildPlan
```

extension 只能选择命名 profile 或受限参数，不能任意组合 destructive reducer。compaction 执行后产生
`ContextCompacted` fact；它修改 context projection，不删除 journal 原始事实。

#### CompletionPolicy

```text
CandidateState
  → OutputValidationState   core
  → BackgroundWorkState     core
  → AutoContinuePolicy      bounded extension slot
  → CommitEligibility       core
```

“继续运行”是 completion decision，不再由 `TurnEnd` hook 反向阻止一个已经发生的结束事件。只有输出完成持久提交后才产生 `RunCompleted` fact。

### 5.6 Hook 的最终定位

Hook 是 Policy Extension 的一种 adapter，而不是第二套控制系统：

```text
Hook configuration
  → typed domain adapter
  → declared extension slot
  → capability-limited PolicyStep
```

禁止：

- `hooks.fire("PreToolUse", arbitrary_dict)` 承载权限正确性；
- hook 自己声明为 core；
- hook 改写 core-only 字段；
- post hook 假装撤销已经发生的外部副作用；
- hook 未经 provenance 直接 mutate Intent；
- hook callback 持有整个 Role、memory、env 或 journal writer。

## 6. Durable Event Journal

### 6.1 Event Envelope

```python
@dataclass(frozen=True)
class EventEnvelope(Generic[PayloadT]):
    event_id: EventId
    event_type: EventType
    schema_version: int

    stream_id: StreamId
    sequence: int

    occurred_at: datetime
    recorded_at: datetime

    session_id: SessionId | None
    run_id: RunId | None
    turn_id: TurnId | None

    correlation_id: CorrelationId | None
    causation_id: EventId | None
    trace_id: str | None
    span_id: str | None

    payload: PayloadT
    metadata: Mapping[str, JsonValue]
```

要求：

- envelope 和 payload 均不可变；
- `event_id` 全局唯一；
- `sequence` 由 journal 分配，不由 producer 猜测；
- `occurred_at` 是领域发生时间，`recorded_at` 是 journal 接纳时间；
- `causation_id` 指向直接原因，`correlation_id` 串联完整操作；
- metadata 只允许 JSON-safe、大小有界、无 secret 的值；
- event type 使用稳定领域名，如 `mote.tool.completed`，不使用 Python qualname；
- schema version 独立于代码版本。

### 6.2 Journal Port

```python
class EventJournal(Protocol):
    async def append(
        self,
        stream_id: StreamId,
        facts: Sequence[UncommittedFact],
        *,
        expected_version: int,
    ) -> AppendResult: ...

    async def read(
        self,
        stream_id: StreamId,
        *,
        after: int = 0,
    ) -> AsyncIterator[EventEnvelope[Any]]: ...

    async def verify(self, stream_id: StreamId) -> VerificationReport: ...
```

`append` 的成功语义只有一种：事实已经达到 backend 契约定义的 crash-safe durable commit。本地文件
backend 必须越过所需的 fsync/原子替换屏障；事务数据库必须完成满足其 HA 契约的 transaction commit。
backend 可以内部 group commit，但不得在真正 commit 前向调用方返回成功。

Journal 不暴露 `MEMORY`、`BUFFERED`、`FLUSHED` 等可被调用方误选的弱等级。无需恢复的高频信号走
Telemetry Stream；需要恢复的事实不允许以产品配置降低持久保证。

### 6.3 Stream 划分

推荐稳定 stream：

```text
session/{session_id}                 会话历史、turn、output 生命周期
runtime/{session_id}/{kind}/{alias}  managed runtime checkpoint/operation
agent-fleet/{root_session_id}        spawn、residency、fleet quota facts
fileops/{project_id}                 文件事务、rewind、hunk 生命周期
```

stream 是一致性边界，不是 UI topic。需要跨 stream 原子性时，优先重新划分 consistency boundary；确实无法避免时由支持事务的 backend 提供 transaction，不在应用层伪造双写原子性。

### 6.4 外部副作用

外部副作用使用 intent/settlement/reconciliation：

```text
Policy authorized
  → durably append EffectIntentRecorded
  → execute external effect with idempotency key
  → durably append EffectCompleted | EffectFailed
```

进程在 execute 与 settlement 之间崩溃时状态为 `UNKNOWN`：

- 支持 provider idempotency/read-back：执行 reconciliation；
- 不支持确认：禁止自动重放，交给显式恢复策略；
- 绝不因“journal 没写 completed”就推断外部动作没发生。

因此不承诺不可实现的通用 exactly-once，而是提供可证明的 at-most-once intent 与幂等 reconciliation。

### 6.5 Schema 演进

每个持久 fact 必须有：

- owner domain；
- stable event type；
- schema version；
- JSON schema 或等价机器可读描述；
- encoder/decoder；
- upcaster；
- golden fixtures；
- unknown-field policy；
- retention 和迁移策略。

Upcaster 只在 journal read boundary 工作，projection 永远接收当前内存模型。未知 event 必须被 journal 保留；不关心它的 projection 可以跳过，但不能把“跳过未知事实”误报成完整恢复。

历史兼容不是永久债务的同义词。upcaster 可以在所有存量 journal 完成离线迁移并验证后退役；退役条件必须有可执行审计，不凭时间猜测。

## 7. Reliable Event Stream

### 7.1 发布模型

Dispatcher 只读取 journal 已提交的 envelope：

```text
append committed
  → stream cursor advances
  → route by typed filter
  → enqueue subscriber mailbox
  → handle
  → persist checkpoint
```

Journal 是事实源，dispatcher 不是第二份日志。dispatcher 崩溃后从 subscriber checkpoint 继续读取。

### 7.2 Subscription Spec

```python
@dataclass(frozen=True)
class SubscriptionSpec:
    identity: SubscriptionIdentity
    filter: EventFilter
    reliability: Reliability
    ordering: Ordering
    capacity: int
    overflow: OverflowPolicy
    retry: RetryPolicy
    checkpoint: CheckpointPolicy
```

可靠等级：

| 等级 | 典型消费者 | 满队列行为 | 恢复 |
| --- | --- | --- | --- |
| DURABLE | session/output/runtime projection | backpressure，永不 drop | checkpoint + replay |
| RELIABLE | audit export、重要 reporting | bounded retry，必要时 DLQ | checkpoint 或 durable inbox |
| LIVE | CLI、LSP、web connection | coalesce/latest-wins/drop-oldest | snapshot + resume cursor |
| LOSSY | token delta、debug telemetry | drop/sample | 不恢复 |

约束：

- DURABLE 不设置“超时后吞掉”；超时进入明确 unhealthy 状态；
- RELIABLE 超过 retry policy 后进入 DLQ，不阻塞其他 subscriber；
- LIVE 必须有断线后的 snapshot/resync 协议；
- LOSSY 不允许被任何 correctness path await；
- priority 不跨 subscriber 表达全局因果，因果必须通过 journal sequence 或显式 barrier 表达。

### 7.3 Mailbox 与并发

每个 subscriber 一个 owner task 和有界 mailbox：

```text
                       ┌─ durable projection mailbox ─ worker ─ checkpoint
committed dispatcher ──┼─ tracing mailbox ─────────── worker ─ retry/DLQ
                       ├─ LSP mailbox ─────────────── worker ─ coalesce
                       └─ CLI mailbox ─────────────── worker ─ snapshot
```

默认保持 `PER_STREAM` 顺序；跨 stream 可按 subscription 声明并发。handler 不允许自行 spawn 未跟踪任务。

需要等待某个 projection 可见时，调用显式 barrier：

```python
await projections.wait_until(
    subscription="session-state",
    stream_id=session_stream,
    sequence=append_result.last_sequence,
)
```

不能靠“把 recorder priority 调前一点”表达 correctness。

### 7.4 Ack、幂等与 DLQ

Durable handler 的标准事务顺序：

```text
receive envelope N
  → inspect idempotency/inbox
  → apply projection side effect
  → persist projection state + checkpoint N atomically
  → ack in memory
```

如果 backend 无法原子保存 projection 和 checkpoint，则使用 durable inbox/outbox 或让 reducer 纯函数化后整快照替换，不能先 checkpoint 再写状态。

DLQ 记录：subscriber identity、event identity、attempt、error report、first/last failure time 和可重放状态。修复后必须通过受审计的 redrive 操作恢复，不能静默丢弃。

## 8. Projection 与恢复

### 8.1 Projection 原则

- projection 是 fact 的确定性函数；
- live 与 replay 走同一 reducer；
- reducer 尽量纯函数；
- 外部读取必须通过显式 port 注入，并记录所依据的版本；
- projection schema 独立版本化；
- 任意 projection 都能从 journal + 可选 snapshot 重建；
- 删除 projection 不会删除事实；
- projection failure 不污染 journal。

### 8.2 Snapshot

Snapshot 包含：

```python
@dataclass(frozen=True)
class ProjectionSnapshot(Generic[StateT]):
    projection: ProjectionIdentity
    stream_id: StreamId
    through_sequence: int
    schema_version: int
    reducer_fingerprint: str
    state: StateT
    checksum: str
```

恢复时先验证 checksum、schema 和 reducer compatibility，再从 `through_sequence + 1` replay。验证失败时丢弃 snapshot，从 journal 重建；不能让坏 snapshot 覆盖好 journal。

### 8.3 Context compaction

Context history 是 journal 的 projection，不是 journal 本身：

```text
Message facts ──reduce──▶ Full logical transcript
                           │
                           ├─compact──▶ Model context projection
                           └─select───▶ UI transcript projection
```

`ContextCompacted` 记录压缩计划、输入范围、summary、策略版本和输出引用。它不 RESET 或删除底层原始事实。需要恢复被折叠内容时通过 projection/query 读取，而不是重新执行 stateful tool。

### 8.4 Session 恢复

```text
verify journal integrity
  → load compatible snapshots
  → replay remaining facts through current reducers
  → reconcile UNKNOWN external effects
  → rebuild subscriber checkpoints/mailboxes
  → expose session as ready
```

恢复过程可重复调用，任何步骤失败均保留可诊断状态；禁止“跳过坏记录继续运行”后仍声称完整恢复。

在没有兼容 snapshot 时，session live projection 必须先从已验证 journal
完整重建，再启动稳定身份 `mote.session.projection` 的 DURABLE subscription。
只有完成这一步后，worker 才能读取持久 checkpoint 并 replay tail；否则旧
checkpoint 会让一个新建的内存 projection 跳过事实。该启动顺序是 correctness
不变量，不是性能优化。未来加入 snapshot 后也必须先验证 snapshot 的
`through_sequence/schema/reducer fingerprint/checksum`，失败即回退完整重建。

### 8.5 提交后本地正确性投影

一个进程内的下一次领域操作若必须立即看到某个派生状态，不能等待
durable subscriber，更不能借道 lossy telemetry。该路径必须在领域内显式注入，
并严格按以下顺序执行：

```text
durable fact commit
  → synchronous local correctness projections
  → live state publication
  → lossy telemetry observation
```

Mote 的 context domain 依此保证：

- history clear/delete 的 fact 提交后，直接重建 `ResourceRegistry`；
- clear/delete/compaction 的 fact 提交后，通过 `TurnContextBus`
  的显式 rebuild 入口重置 tool/skill/team/git/code-map/compaction-notice
  frontier，再替换 live model context；
- 当前可折叠工具集每次从 live executor pull，不依赖
  `ToolsChangedEvent` 维护正确性副本。

这些本地投影是已提交 fact 的进程内派生状态，不是第二事实源。
持久会话投影仍由稳定身份的 DURABLE subscription 维护；需要它的可见性时
使用 sequence barrier。Telemetry mailbox 可以丢弃或滞后，不得改变下一次
模型请求所见的正确状态。

## 9. Telemetry Stream

Telemetry 与可靠事件流分离，典型内容包括：

- LLM token delta；
- transient phase progress；
- debug diagnostics；
- UI animation/activity；
- high-cardinality tracing samples。

Telemetry 规则：

- 允许通过 `ContextVar` 注入窄的 async/sync observer capability；
- 无绑定 observer 时是 no-op；
- 永远不能返回 Policy Decision；
- 永远不能承担 permission、commit、checkpoint 或恢复正确性；
- buffer 有界，明确 drop-oldest/drop-newest/coalesce/sample；
- token stream 不写 durable journal；
- 关键 usage/cost 结算若影响配额，应另产 durable accounting fact；
- sync emit 必须是常数时间 enqueue，不能同步调用任意 subscriber；
- drop、lag、queue depth 必须可观测。

Kernel 只依赖 `contracts/ports/` 中的 telemetry capability，由 Runtime 在执行 scope 注入；Kernel 不 import Runtime fabric。

## 10. Composition、作用域与生命周期

### 10.1 Fabric 作用域

- 每个 Role/session 拥有 session-scoped fabric handle；
- Runtime Event Fabric 严格位于单个进程内，不提供 IPC、broker 或跨进程订阅；
- 跨进程 Agent 协调、投递和所有权由 `orchestration/` 的显式协议负责；
- orchestration 可以把已接收的跨进程消息提交为本进程的领域 Intent/Fact，但不能共享或桥接 mutable EventBus；
- 不存在进程全局 mutable EventBus；
- 跨 Agent 事实通过明确 fleet/session stream 和 correlation id 关联；
- sub-agent 继承 telemetry context，不继承父级 correctness authority。

同一 session 的 Runtime writer 只有一个进程 owner；第二个写进程在 session admission 阶段直接拒绝。
这个所有权约束属于 orchestration/lifecycle，不进入 EventBus 或 Journal append 热路径。只读 list/replay 不取得写权限。

### 10.2 唯一装配点

```python
fabric = EventFabric(
    journal=journal,
    policies=PolicyManifest(...),
    subscriptions=SubscriptionManifest(...),
    telemetry=TelemetryRuntime(...),
    health=FabricHealth(...),
)
```

禁止：

- subscriber 在 `__init__` 内注册自己；
- component getter 触发 sibling wiring；
- core policy 动态发现后自动获得权限；
- 用 import side effect 注册 correctness-critical component；
- 同一核心 subscriber 同时由内部组件和 composition root 注册；
- 以裸 callback 无 identity 地进入生命周期。

Session composition root 的 production manifest 至少包含
`mote.session.projection`：`DURABLE + PER_STREAM + BACKPRESSURE`，checkpoint
存放在 session-owned SQLite state store。测试或离线 journal utility 可以显式
使用空 manifest，但 Role 运行时不得退化为空 manifest。

同步事务域（当前是 File Operations）不能直接写 session journal。它们必须在
受管磁盘工作线程中执行，并通过 Event Fabric 记录的 owner loop 提交；thread
bridge 必须等待 journal commit 和 committed dispatch 入队后才返回，且从 owner
loop 调用时立即拒绝以避免死锁。需要 projection 可见性时继续以返回的 sequence
调用显式 barrier。`SessionLog.commit_offline` 只服务于未绑定 live
Fabric 的 fork、迁移和测试构造；一旦绑定 production fact sink，offline commit
必须强制失败。

动态 UI connection 可以创建 LIVE subscription，但必须拿到 `SubscriptionHandle` 并在 connection scope 关闭。

### 10.3 生命周期状态机

```text
NEW → STARTING → RUNNING → DRAINING → CLOSED
                  │            │
                  └──FAILED────┘
```

启动顺序：

1. open/verify journal backend；
2. restore durable checkpoints；
3. start durable/reliable subscriber workers；
4. start live/telemetry workers；
5. unseal producers，进入 RUNNING。

关闭顺序：

1. stop accepting new Intent；
2. cancel/settle in-flight domain operations；
3. flush pending fact append；
4. drain durable/reliable mailboxes to barrier；
5. persist checkpoints；
6. stop live/telemetry workers；
7. close backend and mark CLOSED。

每一步有 deadline 和结构化失败结果；取消等待者不能取消实际 cleanup。失败后重试只执行未完成阶段。

## 11. Health 与高可用语义

### 11.1 Health 状态

```python
class FabricHealthState(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    READ_ONLY = "read_only"
    UNAVAILABLE = "unavailable"
```

| 故障 | 状态 | 行为 |
| --- | --- | --- |
| LOSSY/LIVE subscriber 失败 | DEGRADED | 主流程继续，允许重连/重建 |
| RELIABLE exporter/DLQ 积压 | DEGRADED | 主流程继续并告警 |
| 非关键 projection 落后 | DEGRADED | 相关读模型标 stale |
| journal 暂时不可写 | READ_ONLY | 禁止产生 recoverable mutation/effect |
| 核心 projection 无法恢复 | UNAVAILABLE | session 不进入 ready |
| journal checksum/sequence 损坏 | UNAVAILABLE | 隔离 stream，要求修复 |
| core policy 无法装配 | UNAVAILABLE | 拒绝启动 |

健康状态必须被 Role/Engine/CLI 读取并影响 readiness；不能只记录一个无人消费的失败 counter。

### 11.2 Backend 演进

首个实现可以继续是本地 append-only 文件，但必须遵守同一个 `EventJournal` port。未来可以在同一进程边界内替换为 SQLite 等本地 backend，领域代码不变化。

Event Fabric 不以 backend 扩展的名义引入分布式一致性、broker 或跨进程 writer。真实跨进程需求统一进入 `orchestration/`，Runtime 只负责进程内 stream concurrency、checkpoint 和 reconciliation 契约。

### 11.3 过载保护

- mailbox capacity 固定且可观测；
- durable backlog 超阈值时对 producer 施加明确背压；
- live stream 采用 coalesce/drop，不把压力传给 journal；
- telemetry 采样率可动态降低，但正确性 fact 不采样；
- 单 event payload、batch、metadata 和 DLQ 都有大小上限；
- retention/compaction 不得删除仍被 checkpoint 引用的事实。

## 12. 可观测性与运维面

最低指标：

```text
policy_evaluation_latency
policy_timeout_total
policy_fail_closed_total
policy_rewrite_total
journal_append_latency
journal_fsync_latency
journal_current_sequence
journal_verification_failure_total
subscriber_queue_depth
subscriber_lag
subscriber_retry_total
subscriber_drop_total
subscriber_dlq_total
projection_checkpoint_sequence
projection_rebuild_latency
telemetry_drop_total
fabric_health_state
```

管理接口至少支持：

- 查看 sealed policy manifest；
- 查看 subscription topology 和每个 mailbox；
- 查看 stream version、checkpoint 和 lag；
- verify journal；
- rebuild projection；
- inspect/redrive DLQ；
- drain subscription；
- 导出脱敏 PolicyTrace 和 causation chain。

所有管理动作都需要权限、审计和 bounded scope。

## 13. 分层与模块落位

目标目录不是建议新增 generic utils，而是明确领域 ownership：

```text
contracts/
  policy/
    base.py                 Intent/Decision/Trace 基础数据
    tool.py                 ToolCallIntent/Decision
    prompt.py               PromptIntent/Decision
    compaction.py           CompactionIntent/Plan
    completion.py           CompletionIntent/Decision
    spawn.py                SpawnIntent/Decision
  events/
    envelope.py             EventEnvelope 与稳定 ID
    facts/                  按领域拆分的不可变 payload
  ports/
    policy.py               PolicyStep/PolicyPipeline 窄接口
    event_journal.py        append/read/verify
    event_subscription.py   subscription/checkpoint ports
    telemetry.py            loss-tolerant capability

kernel/
  telemetry.py              仅消费注入 capability

runtime/
  policy/
    engine.py               通用 typed runner
    manifest.py             seal/validate/composition
    provenance.py           脱敏 trace
  events/
    journal.py              runtime journal facade
    dispatcher.py           committed stream dispatcher
    mailbox.py              bounded per-subscriber mailbox
    subscription.py         worker/ack/retry/checkpoint
    health.py               readiness/degradation
    backends/               local/sql 等实现
  projections/
    session.py
    context.py
    output.py
    runtime.py
  tools/
    policy.py               Tool core policy 实现
    settlement.py           effect/result settlement

orchestration/
  policy/
    spawn.py                SpawnAdmission core policy
    fleet.py                fleet quota policy
  projections/
    fleet.py

product/
  policy/
    extensions/             产品明确开放的 extension adapters
  cli/
    projections/            View projection + LIVE subscription
```

依赖规则：

- contracts 不依赖其他层；
- kernel 只看到 contracts capability；
- runtime 实现 journal、policy runner 和 session/runtime projections；
- orchestration 使用 runtime 能力实现 fleet/spawn 领域；
- product 装配 extension、CLI 和集成；
- 低层需要高层实现时只在 `contracts/ports/` 定义窄 Protocol；
- 不创建 `common/` 或无 ownership 的 utils 包。

## 14. 测试与形式化验证

### 14.1 Contract tests

- 每个 Core Policy manifest 完整且不可移除；
- extension authority 不超过 slot 上限；
- deny 单调、rewrite 字段受限；
- event type/schema registry 唯一；
- subscription identity 唯一；
- 所有 queue 和 retry policy 有界。

### 14.2 Property-based tests

- 同一 stream sequence 严格单调；
- 任意重复 delivery 后 projection 不变；
- replay 结果等于 live reduction；
- snapshot + tail replay 等于 full replay；
- 任意 PolicyStep 排列都不能越过 stage boundary；
- 任意 extension contribution 不能把 deny 变 allow；
- rewrite provenance 与最终 Intent 一致。

### 14.3 Fault injection

在以下每个点 kill process 并恢复：

```text
before append
during append
after flush before fsync
after intent before external effect
after external effect before settlement
after fact commit before dispatch
during handler side effect
after handler state before checkpoint
during snapshot replacement
during shutdown drain
```

每个 crash point 都必须得到唯一结论：not-started、committed、unknown-needs-reconcile 或 safe-to-retry。

### 14.4 Compatibility tests

- 每个历史 schema 的 golden fixture；
- upcast 后模型稳定；
- unknown field/event 行为明确；
- journal corruption 和 sequence hole 被发现；
- backend contract suite 对所有实现复用。

### 14.5 Soak 与 SLO

- 百万级 fact replay/快照基准；
- 长时间 slow subscriber 不导致内存增长；
- telemetry flood 不影响 policy/journal latency；
- shutdown 在有 backlog、retry 和断连时仍有界；
- projection lag、DLQ、drop 和 health transition 可观测。

SLO 数值必须由代码常量和测试共同定义，文档不复制会漂移的数字。

## 15. 迁移策略

迁移采用领域纵切，不采用永久双轨：

### Phase 0：冻结事实与不变量

- 为当前 tool、prompt、compaction、completion、spawn 行为建立 characterization tests；
- 为 rollout/replay、output commit、external effect crash point 建 fault tests；
- 列出所有 producer、subscriber、持久事件和动态注册点；
- 定义目标 fact registry 与 policy manifest。

### Phase 1：建立叶子契约

- 引入 typed Intent/Decision；
- 引入 EventEnvelope、EventJournal 和 Subscription ports；
- 引入 health、reliability 和 journal commit 契约；
- 不增加第二套公开 API。

### Phase 2：迁移领域 Policy

按顺序迁移：

1. ToolCallPolicy + ToolSettlementPolicy；
2. PromptPolicy；
3. SpawnAdmissionPolicy；
4. CompactionPolicy；
5. CompletionPolicy。

每个领域完成后删除对应 control event/outcome/subscriber 路径。旧控制路径不能继续作为 fallback。

### Phase 3：Journal 成为唯一 recoverable fact source

- session meta 也作为正常 fact append，不再直接特殊写第一行；
- message、compaction、output、runtime、fileops 迁入 envelope journal；
- effect intent/settlement 使用稳定 idempotency key 和 causation 对齐；
- replay 改为统一 projection reducer；
- 旧 rollout schema 通过一次性 migration/upcast 读取。

### Phase 4：拆分可靠流和 telemetry

- durable/reliable subscriber 使用 mailbox + checkpoint；
- CLI/LSP 使用 LIVE subscription + snapshot/resync；
- token/phase/trace 高频信号迁入 Telemetry Stream；
- 删除 observer priority 串行循环和同步直接 fan-out。

### Phase 5：统一装配和生命周期

- composition root 声明完整 policy/subscription manifest；
- 删除构造期 self-subscribe；
- 引入 fabric health/readiness；
- 完成 drain、checkpoint 生命周期；
- 删除所有无 owner task。

### Phase 6：最终收敛

- 删除旧 `EventBus`、旧 control outcome、旧 recorder projection 和兼容 adapter；
- 删除旧 re-export、过期配置和双轨测试；
- 更新架构文档为现状说明；
- 全量 pyright、架构测试、故障注入、兼容测试和 SLO 通过；
- 用静态搜索证明旧入口为零。

迁移 adapter 只允许存在于迁移分支或明确的中间提交，不能进入最终发布态；不使用长期 feature flag 维持两套语义。

## 16. 明确拒绝的方案

### 16.1 用简单 Hook 替代核心控制

拒绝。字符串 + dict + callback 不能承载安全不变量。Hook 只能作为 typed、capability-limited Policy Extension adapter。

### 16.2 继续让一个 `emit()` 同时表示请求和事实

拒绝。它会让“可 veto 的意图”和“不可改变的事实”在类型和时序上持续混淆。

### 16.3 把所有 observer 放进 `asyncio.gather`

拒绝。它没有独立 mailbox、顺序、背压、ack、retry、checkpoint 和 shutdown ownership，只是把串行问题改成无治理并发。

### 16.4 由 Recorder 从观察流尽力复制真相

拒绝。恢复所需事实必须直接进入 journal；Recorder 不能是可能失败后继续执行的旁路。

### 16.5 宣称 Exactly-once

拒绝。进程崩溃和外部系统副作用无法通用保证 exactly-once。采用 at-least-once delivery、幂等 projection、effect intent 和 reconciliation。

### 16.6 现在引入 Kafka 式基础设施

拒绝。Event Fabric 永远保持进程内；真实跨进程需求进入 `orchestration/`，不在 Runtime 内重造 broker 或共识。

### 16.7 一个万能 Policy DTO

拒绝。每个领域拥有自己的 Intent、Decision、Stage 和 extension authority，通用内核只复用执行机制。

### 16.8 用 ContextVar 承载正确性

拒绝。ContextVar 只允许承载 telemetry、trace 和 scope；Policy 和 journal 必须显式注入。

## 17. Definition of Done

目标架构完成必须同时满足：

- [ ] Tool、Prompt、Spawn、Compaction、Completion 都使用领域专属 Policy facade；
- [ ] core policy 不可由配置或 extension 绕过；
- [ ] hook 只作为声明式 typed extension；
- [ ] 所有 recoverable fact 使用稳定 envelope 写入唯一 journal；
- [ ] 所有关键成功响应都有对应 durable fact barrier；
- [ ] external effect 有 intent、settlement、unknown 和 reconciliation；
- [ ] journal 支持 CAS、sequence 和 checksum；
- [ ] durable subscription 使用独立有界 mailbox、幂等处理和 checkpoint；
- [ ] live/telemetry 故障不会阻塞 journal 或 policy；
- [ ] health 状态被 Engine readiness 和产品面消费；
- [ ] replay 与 live projection 使用同一 reducer；
- [ ] snapshot 可丢弃并从 journal 完整重建；
- [ ] schema 有版本、upcaster、golden fixture 和退役流程；
- [ ] 没有构造期 self-subscribe 或无 owner background task；
- [ ] 没有 process-global mutable bus；
- [ ] 没有无界 queue/retry/retention；
- [ ] fault injection 覆盖所有关键 crash point；
- [ ] 所有 backend 通过同一 contract suite；
- [ ] 旧 EventBus/control outcome/recorder adapter 已删除；
- [ ] 静态 import 架构检查与 pyright 全绿；
- [ ] 文档描述与最终代码一致。

## 18. 最终判断标准

未来新增一个领域能力时，应满足：

1. 新增该领域的 Intent/Decision 或事实 payload；
2. 若是策略扩展，只注册到已有 slot，不修改通用 runner；
3. 若是新核心阶段，必须说明新不变量，不能伪装成普通 extension；
4. 若影响恢复，先定义 fact、durability、replay 和 crash semantics；
5. 若增加消费者，只新增 SubscriptionSpec，不修改 producer；
6. 若增加 UI/trace 信号，只进入 telemetry，不反向影响 correctness；
7. 若必须修改多个无关领域的中心 switch，设计应回炉。

最终架构不是“更强的 EventBus”，而是一套语义分离的运行时基础设施：

> **领域 Policy 保证动作可控，Durable Journal 保证事实可信，Reliable Stream 保证消费可恢复，Telemetry 保证实时观察不拖累正确性。**
