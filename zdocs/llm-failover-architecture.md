# LLM Failover 十年形态架构

> 状态：Implemented / 已收口（2026-07-25）  
> 范围：LLM 路由、重试、凭据轮换、跨 endpoint 切换、准入、流式输出、审计、崩溃恢复与热更新  
> 部署边界：单 Python 进程；不设计跨进程重试协调、远端 health store 或分布式 lease  
> 分层：`contracts <- kernel <- runtime <- orchestration <- product`

外部 Tool 服务不进入本 Gateway；媒体、搜索、转写等托管能力的统一控制面见
[`service-failover-architecture.md`](./service-failover-architecture.md)。

---

## 1. 最终结论

Mote 的 LLM 调用只有一条生产控制路径：

```text
Kernel ModelInvocation
  -> contracts.ports.ModelGateway
  -> RuntimeModelGateway
  -> immutable FailoverPlan
  -> request-local AttemptOrchestrator
  -> ProductModelEndpointAdapter.execute_once
  -> one provider wire call
  -> ResolvedModelResponse
```

职责已经固定：

- Router 只选择语义 route，不持有 Provider 实例、fallback 集合或游标；
- FailoverPlanner 从不可变 snapshot 生成合法候选与预算；
- AttemptOrchestrator 是 retry、credential rotation、request transform、endpoint switch、deadline 和 backoff 的唯一所有者；
- Product adapter 为每次 attempt 重新投影 provider wire envelope；
- Provider client 只做一次 wire 调用、provider-specific 参数、响应解析与 SDK 错误翻译；
- Kernel 只消费 canonical output，不依赖 Runtime 或 Product。

不同 Agent 或并发调用可以共享只读 snapshot 和真实外部资源的健康事实，但不会共享 candidate cursor、credential cursor、attempt counter、deadline、transform state、stream buffer 或 resume generation。一个 Agent 的重试不会推进、耗尽或重置另一个 Agent 的调用状态。

Provider 没有被整体重做。OpenAI Chat、OpenAI Responses、Anthropic、DeepSeek 的成熟 SDK 客户端和 wire 逻辑被保留；变化是删除 Provider 的控制权，让它固定在一个 endpoint 和一个 opaque credential slot 上执行一次。

---

## 2. 已执行的不变量

以下不是建议，而是代码与测试锁定的约束：

1. 一个 logical call 的 wire 次数不超过 `AttemptBudget.max_wire_attempts`；
2. 每个 attempt 最多执行一次 provider wire；SDK 自带 retry 被设为 0；
3. 每次 `execute/resume` 在栈内新建 mutable call state；
4. 每个 attempt 切换 endpoint 后从 canonical invocation 重新投影 wire；
5. fallback endpoint 必须先满足 capability、governance、region 和 context window 要求；
6. task route 与 failover group 是两套配置关系，注册 task 不会让它自动成为 interactive fallback；
7. availability、quota、credential quarantine 与 bulkhead 是相互独立的 admission plane；
8. admission rejection 不计 wire attempt；permit 只会 `succeed/fail/abandon` 一次；
9. `CancelledError` 不分类为 Provider 故障，取消后不产生下一 attempt；
10. `Retry-After` 由 policy 传给 orchestrator，并受剩余 deadline 与 `max_backoff_seconds` 限制；
11. content-policy 默认 `ABORT`，禁止以跨 Provider fallback 绕过治理；
12. 失败 stream 的 provisional delta 不进入 transcript；
13. accepted response checkpoint 已存在时，resume 不再触网；
14. open attempt 在崩溃恢复时标为 `IN_DOUBT`，并声明 `possible_duplicate_billing`；
15. reload 中的 in-flight call 固定观察旧 generation，新调用只观察新 generation；
16. Provider、事件、错误、snapshot 和 resource identity 不携带 credential secret；
17. Kernel 架构测试禁止 import Runtime/Product，也禁止旧控制入口复活。

---

## 3. 分层与组件

### 3.1 Contracts

Contracts 定义稳定、provider-neutral、可序列化的边界：

- `ModelInvocation` 与 tagged input：`GenerateInput`、`WebSearchInput`、`ImageDescriptionInput`；
- `CanonicalMessage`、`CanonicalToolDefinition`、`CanonicalModelResponse`；
- `EndpointDescriptor`、`FailoverPlan`、`FailureDisposition`、`FailoverDecision`；
- `AttemptSummary`、`ModelCallSummary` 与 versioned journal records；
- `ModelGateway`、`ModelEndpointAdapter`、`ModelCallJournal`、`ModelOperatorControl` ports；
- typed model-call/attempt telemetry。

Contracts 不包含 SDK client、callback、`asyncio.Task`、lock 或秘密明文。

### 3.2 Kernel

Kernel 的模型调用集中在 `kernel/models/model_calls.py`：

- `generate`；
- `web_search`；
- `describe_image`；
- Message/tool 到 canonical contract 的转换。

ThinkEngine、ThinkService、duplicate-turn guard、Context compression、session title、routing judge、WebSearch 和 image description 全部通过 `ModelRoute.gateway` 调用。Native CommandChannel 只产生 canonical tool definitions，不再推断 OpenAI/Anthropic envelope。

### 3.3 Runtime

Runtime 拥有完整控制面：

```text
runtime/models/gateway.py                 semantic routing
runtime/models/model_gateway.py           execution + journal + summary
runtime/models/failover/snapshot.py        immutable activation snapshot
runtime/models/failover/planner.py         capability/governance planning
runtime/models/failover/orchestrator.py    only retry state machine
runtime/models/failover/policy.py          disposition -> decision
runtime/models/failover/admission.py       admission planes + permits
runtime/models/failover/availability.py    epoch/probe breaker
runtime/models/failover/transforms.py      canonical request transforms
runtime/models/failover/model_journal.py   crash-safe local journal
runtime/models/failover/runtime_state.py   atomic generation reload/drain
runtime/models/failover/operator.py        audited operator control
```

Runtime 不 import Product provider SDK。

### 3.4 Product

Product composition root 提供：

- `ProductModelEndpointResolver`：把 endpoint + opaque slot 绑定成 fresh adapter；
- `ProductModelEndpointAdapter`：canonical/wire 双向投影；
- OpenAI Chat、OpenAI Responses、Anthropic、DeepSeek clients；
- `builtin_model_gateway` 与 `reload_builtin_model_gateway`；
- CLI model-call journal、operator control 和 Gateway 注入。

### 3.5 Orchestration

Failover 不依赖 Orchestration。多 Agent 层只消费结果和观察事件，不拥有 retry cursor 或模型预算。当前单进程约束下，不存在跨 Agent retry 协调，也不需要存在。

---

## 4. 请求、路由与候选规划

### 4.1 Semantic route

`LLMRouter` 回答“本次任务使用哪个 route”。内建 task 包括：

| 调用面 | task |
|---|---|
| 主 Think | `default` / interactive route |
| Context summarize | `compression` |
| session title | `session_title` |
| LLM 路由裁判 | `routing_judge` |
| provider-native 搜索 | `web_search` |
| 图片描述 | `image_description` |

Compression route 的 `request_transformer=None`，因此它无法递归触发自身 compression。

### 4.2 Immutable plan

Planner 从一个 `ModelRuntimeSnapshot.revision` 生成 `FailoverPlan`。Plan 固定：

- ordered endpoint candidates；
- policy ID；
- capability/governance requirements；
- wire、endpoint、credential、transform、deadline 与 timeout budgets；
- plan/config identity。

Planner 在触网前过滤：tools、native schema、server web search、vision、PDF、native tool search、minimum context、governance domain 和 allowed region。能力不足是不可选候选，不是运行时“降级成功”。

### 4.3 Legacy 配置

没有声明 `models.endpoints` 时，`models.default` 与 `models.tasks` 也会编译成 singleton failover groups，并总是构造 canonical Gateway。Legacy 配置只是一种输入形式，不再对应旧执行路径。

---

## 5. Attempt 状态机与默认策略

每次 `execute/resume` 都创建一个私有 `_ModelCallState`：

```text
provider target
current immutable invocation
attempts_by_endpoint
endpoint_switches
credential_rotations
request_transforms
started_at / remaining deadline
resume seed
```

默认决策矩阵：

| Failure | 决策 |
|---|---|
| connection / timeout / overload / server error / rate limit | bounded same-endpoint retry；耗尽后 switch |
| auth rejected / billing exhausted | rotate credential；耗尽后 switch endpoint |
| model unavailable | switch endpoint |
| context exceeded / payload too large | canonical compression transform；无进展则 switch |
| image too large | shrink image transform |
| incompatible tool content | downgrade tool content transform |
| invalid provider request state | strip request state transform |
| empty response | bounded retry/switch，不污染 availability |
| protocol incompatible | switch capability-compatible endpoint |
| content policy / unknown | abort |

所有决策都在预算内执行。相同请求重试、credential rotation、request transform 和 endpoint switch 不存在第二个 owner。

---

## 6. Credential 与 OAuth

### 6.1 静态 key

静态 key pool 在 activation 时拆成 immutable opaque slots。Resolver 每次只把一个 slot 的 secret 交给一个 Provider client；Provider 收到多 key 会拒绝构造，避免重新形成 `_api_key_index` 控制面。

### 6.2 OAuth

OAuth endpoint 被编译为两个 opaque slots：

```text
endpoint:oauth-current
endpoint:oauth-refresh
```

第一个 slot 使用当前有效 token。只有第一个 attempt 返回 auth failure、Gateway 选择第二个 slot 后，Product adapter 才在首次 `execute_once` 前 force refresh 并重建绑定 client。刷新失败仍表现为 credential failure，Gateway 可以按剩余预算切换 endpoint。

因此边界是：

- Gateway 决定何时换 slot；
- Product 决定如何实现 OAuth refresh slot；
- Provider 只暴露刷新绑定能力，不自行捕获异常、轮换、重试或 fallback。

OAuth store 的 file lock 仍可防止同一进程内线程或未来外部进程同时刷新 token；它不承担 model-call retry 协调。

---

## 7. Admission 与共享资源事实

Admission 顺序固定：

```text
operator enabled/draining/disabled
  -> logical deadline
  -> credential quarantine
  -> quota reservation/cooldown
  -> availability breaker
  -> endpoint bulkhead
  -> AdmissionPermit
```

`ResourceIdentity` 由 endpoint、transport、公开 endpoint fingerprint、model/deployment、tenant fingerprint 和 credential slot 组成。共享状态只描述真实外部资源：

- availability epoch、half-open probe 和 quorum；
- quota remaining/reset/retry-after；
- credential quarantine；
- endpoint in-flight/bulkhead；
- operator state。

这些事实可以影响每个调用自己的 admission verdict，但不能修改任何调用的 plan cursor 或预算。

Operator transition 使用 expected revision、actor、reason 和 fsynced audit。`draining` 拒绝新 permit，已有调用继续结算；旧 runtime generation 也遵守相同 drain 语义。

---

## 8. Provider Adapter 边界

`ProductModelEndpointAdapter.execute_once()` 的职责严格限定为：

1. 校验 adapter 与 endpoint binding；
2. 必要时激活 Product-controlled OAuth refresh slot；
3. 把 canonical messages/tools/schema/artifact 投影到本 transport；
4. 调用 Provider 一个非重试 wire primitive；
5. 归一 output、usage、cost、quota 和 provider request ID；
6. 把 SDK 错误归一为稳定 `FailureDisposition`。

跨 transport fallback 会重新投影完整请求：

```text
CanonicalToolDefinition
  -> OpenAI Chat function tool
  -> OpenAI Responses function/tool_search
  -> Anthropic input_schema/defer_loading
```

Provider 不拥有 Router、fallback candidate、Context reducer、attempt budget、journal 或 resume state。

---

## 9. Streaming

每个 delta 带 `model_call_id / attempt_id / sequence / provisional=true`。Attempt 最终产生：

- committed：接受该 attempt，delta 可进入正式输出；
- discarded：该 attempt 失败并继续 fallback，撤销或丢弃 delta；
- interrupted：调用取消，无替代响应。

不同 attempt 和并发调用用 `ContextVar` buffer 隔离。支持 rollback 的 Textual consumer 可以实时显示并撤回；不支持 rollback 的 terminal/network consumer 在 commit 前缓冲。Rollout 和 Kernel message history 只接收 committed output。

---

## 10. Durable journal 与 resume

LLM request 的 effect 语义是 `BILLABLE_READ`：可能计费、非确定，但通常没有业务写副作用。系统不伪造 exactly-once。

Journal 顺序：

```text
call_planned
  -> attempt_started        # wire 前 write-ahead + fsync
  -> attempt_finished
  -> ...
  -> call_finished          # 含 accepted response checkpoint
```

`model_call_id` 在 Think started record、所有 attempts、resume generations 和最终 ThinkResult 中稳定。

Resume 规则：

1. Think 已完成：直接 reinstate `ThinkResult`，零 wire；
2. Think started 且 call journal 为空：按 generation 0 execute；
3. call 已成功且 accepted response durable：直接 reinstate canonical response；
4. started attempt 无 terminal：补记 `IN_DOUBT`，不声称未触网；
5. 使用当前 runtime revision 生成新 resume generation；
6. 新旧 profile 取更严格剩余 wire/deadline budget，配置更新不能扩容旧调用；
7. summary 聚合已知 usage/cost，并对未知 attempt 标记 `possible_duplicate_billing`。

Journal 使用按 secret-opaque call hash 分流的 append-only JSONL，并校验 record identity、ordinal、配对与 terminal 顺序。Journal 持久化失败是本地 non-retryable failure，绝不能因此额外触网。

---

## 11. Atomic reload 与生命周期

`AtomicModelRuntime` 维护 generation：

```text
generation = immutable planner(snapshot) + endpoint resolver + lease count
```

- `execute/resume` 开始时获取一次 generation lease；
- in-flight call 在完成前固定使用该 planner/resolver；
- reload 先完整构建并验证新 generation，再原子交换引用；
- 新调用只看到新 revision；
- 旧 resolver 在 lease 清零后异步关闭；
- resume 使用当前 revision，但继承原调用更严格的剩余预算。

RoleSchema 的可变对象不是运行中 Gateway 的事实来源。能力、route 与 credential slots 都来自 activation snapshot。

---

## 12. 可观测与 rollout

canonical 事件族：

| Event | 用途 |
|---|---|
| `ModelCallPlannedEvent` | plan/revision/route/budget |
| `ModelAttemptAdmissionRejectedEvent` | 未触网的 gate rejection |
| `ModelAttemptStartedEvent` | attempt、endpoint、slot、model、trace input |
| `ModelAttemptFinishedEvent` | state、failure、latency、usage、cost、output |
| `ModelFallbackSelectedEvent` | from/to endpoint 与原因 |
| `ModelCallFinishedEvent` | logical terminal、summary、aggregate usage/cost |

Tracing 直接用 attempt started/finished 创建一条 generation；rollout 只持久化 `ModelCallFinishedEvent` 投影出的有界 `LLMCallEvent/ModelCallSummary`，不重复持久化 wire response。旧 `LLMRequestEvent/LLMResponseEvent/LLMErrorEvent/LLMRetryEvent` 已删除。

高基数 call/attempt ID 只进入 trace/log，不作为 metrics label。事件、summary 和错误只保存 stable reason 与 secret-opaque identity。

---

## 13. 成本与预算取舍

Gateway 聚合每个已结算 attempt 的 usage/cost；失败但已计费的已知 usage 也进入 summary，`IN_DOUBT` 单列未知计费风险。成功调用只向共享 CostTracker 结算一次权威 aggregate。

当前不做 speculative per-call cost reservation。不同 Provider 对失败请求、cache、reasoning 和 output 上限的计价缺少统一可信预估；伪精确 reservation 会制造错误拒绝。Agent 的 `max_cost` 在 turn boundary 做真实已结算成本 gate，Gateway 负责不可突破的 wire/deadline/transform budgets 和实际成本归集。未来若 Provider 提供可靠 estimate contract，再以独立 typed budget 扩展，不把猜测塞进 retry loop。

---

## 14. 配置形态

```yaml
models:
  credential_pools:
    primary-keys:
      slots:
        - id: primary-a
          secret_ref: env://PRIMARY_API_KEY

  endpoints:
    primary:
      provider: anthropic
      model: claude-sonnet-4-8
      credential_pool: primary-keys
      governance_domain: corp
      capabilities:
        context_tokens: 200000

    backup:
      provider: openai
      model: gpt-5.4
      api_key: ${OPENAI_API_KEY}
      governance_domain: corp

  failover_groups:
    interactive:
      endpoints: [primary, backup]
      recovery_profile: interactive

  routes:
    default: interactive
    tasks:
      compression: interactive
      session_title: interactive

  recovery_profiles:
    interactive:
      max_wire_attempts: 6
      max_attempts_per_endpoint: 2
      max_endpoint_switches: 1
      max_credential_rotations: 2
      max_request_transforms: 2
      total_deadline_seconds: 600
      single_attempt_timeout_seconds: 180
      max_backoff_seconds: 60
```

加载期校验 group/endpoint/profile/route 引用、重复 ID、disabled endpoint、credential pool、OAuth/static pool 冲突、局部预算上界以及 compression transform recursion。

---

## 15. 已删除的旧路径

以下生产路径已物理删除，不保留 wrapper、re-export 或 feature flag：

- `GatewayLLMClient` / `_RoutedLLMClient`；
- `runtime/models/gateway_client.py`；
- `BaseLLM._run_with_recovery()`；
- Provider 内 credential rotation 与 `_api_key_index`；
- Router provider instance/cache/fallback cursor；
- `_fallback_supplier`；
- `infer_native_tool_provider`；
- BaseLLM response validator/health control fields；
- `runtime/models/clients/validators.py`；
- `runtime/models/clients/health.py`；
- 旧 LLM request/response/error/retry telemetry 与 rollout 双写。

`RecoveryRunner` 仍可服务 Tool/Graph 等非 LLM 领域，但不参与模型调用。

---

## 16. 实际代码量

早期估算是生产新增/重写 3,750–5,800 LOC、测试 2,500–4,000 LOC。完成态使用可复现的“专用核心 physical LOC”口径统计，而不是在本仓库大规模目录迁移的 dirty worktree 上伪造净 diff：

| 范围 | 文件数 | 当前 physical LOC |
|---|---:|---:|
| Contracts + Kernel canonical calls + Runtime Gateway/failover + Product endpoint composition | 28 | 6,214 |
| failover/adapter/journal/admission/architecture 专用测试 | 12 | 3,559 |
| 合计核心 footprint | 40 | 9,773 |

生产核心比原估算上界多 414 LOC（约 7.1%），主要来自 crash-safe journal、atomic generation drain、operator audit、provisional streaming 和 OAuth refresh-slot 的完整闭环；测试仍在原估算区间。Role、Context、session、tracing、CLI 和四个 Provider 的接线修改未重复算入“专用核心文件”，所以该数字适合作为维护 footprint，不冒充相对旧分支的精确新增行数。

统计文件清单由本设计对应的 Contracts ports/models/config、`kernel/models/model_calls.py`、`runtime/models/{gateway,model_gateway,failover/*}`、Product endpoint adapter/resolver/bootstrap 及 12 个专用测试组成，可用 `wc -l` 重算。

---

## 17. 验收与已知非本任务问题

已覆盖的关键测试：

- policy、attempt budget、retry-after、credential rotation、endpoint fallback；
- capability/governance planner 与 secret-opaque snapshot；
- 四 transport canonical projection、single-wire conformance；
- concurrent call/Agent cursor 隔离；
- availability epoch/probe/quorum、quota reservation、credential quarantine、bulkhead；
- operator audit/drain；
- model-call journal failure windows、accepted checkpoint、`IN_DOUBT`；
- resume generation、预算不扩容、atomic reload/drain；
- provisional stream commit/discard/interruption 与 Textual rollback；
- Kernel/Runtime/Product layering 与旧路径禁入。

全链回归中仍有一个与 failover 无关的预存测试错误：`ztest/flow/durable/test_process_crash_recovery.py` 仍访问已删除的 `ReplayResult.messages` 属性。该失败不改变 model-call journal 或 resume 测试结果，也未在本任务中顺手修改历史 replay API。

---

## 18. 未来扩展规则

### 新 Provider

新增 Product Provider client/adapter 注册与 conformance cases，不修改 orchestrator 状态机。

### 新 model-backed operation

增加 canonical input/output tagged-union variant、requirements 与 adapter projection，不创建第二套 retry loop。

### Hedging

只有明确需求时作为独立 policy 实现：并发调用全部计费、全部占 budget、每个 hedge 都有 attempt ID，winner commit、loser cancel/discard。默认不启用。

### 多进程

当前不预留半成品分布式接口。部署模型真的变为多进程时，基于实际一致性/SLO 另立 ADR；不得让远端 lease 或共享 cursor 侵入当前单进程主路径。

---

## 19. 完成定义

十年形态已经达到以下完成条件：

- 单一 `ModelGateway` 控制面；
- request-local retry state 与 Agent 间完全隔离；
- Provider 单 wire、跨 transport 重新投影；
- typed policy、统一预算与 admission planes；
- stable Think/model-call identity；
- crash resume、`IN_DOUBT` 与 possible duplicate billing；
- provisional streaming commit/discard；
- atomic reload/drain；
- canonical telemetry、rollout summary 与 tracing；
- legacy 与 declarative 配置都进入同一 Gateway；
- 旧双控制面和兼容残渣已删除；
- 架构测试阻止回流。

未来功能只在稳定 seam 上扩展，不再改写 Agent Flow、Router 或 Provider 的控制边界。
