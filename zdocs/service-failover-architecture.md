# Hosted Tool Service Failover 十年形态

> 状态：核心、媒体与 WebSearch reference implementation 已落地（2026-07-26）  
> 范围：由 Tool 调用的外部托管能力，例如媒体生成、搜索、云浏览器、转写和远程执行服务  
> 部署边界：单 Python 进程；不同 Agent、session 和 tool call 不共享重试游标  
> 分层：`contracts <- kernel <- runtime <- orchestration <- product`

---

## 1. 最终结论

外部 Tool 服务不进入 `ModelGateway`，也不为图片、视频、搜索分别建立故障恢复框架。长期形态固定为两个领域 Gateway 加一套共享资源事实：

```text
ModelGateway
  └─ token、context window、stream、tool-use envelope 等 LLM 语义

ServiceGateway
  └─ submit/poll/reconcile/cancel、receipt 和副作用安全语义

Shared failover primitives
  └─ AttemptBudget、FailureDisposition、FailoverDecision、policy、
     ResourceIdentity、admission、availability、credential 与 operator facts
```

`GenerateMedia` 和 `WebSearch` 是前两个 reference implementation。以后新增转写、OCR 或云浏览器，只新增 Product endpoint adapter、配置到 snapshot 的投影以及 Tool 的 `invoke_service` 调用，不复制 retry loop、breaker、credential cursor 或 journal。

Service failover 是 opt-in。`Edit`、`Bash`、发消息、派生 Agent 等普通 Tool 不会因为存在 ServiceGateway 而自动重试；只有显式声明执行安全语义并通过 `invoke_service` 窄能力调用的托管服务才进入该控制面。

---

## 2. 唯一调用路径

```text
Tool
  -> Role.invoke_service
       derive stable service_call_id + idempotency_key
  -> contracts.ports.ServiceGateway
  -> RuntimeServiceGateway
       immutable ServicePlan
       request-local lifecycle state
       admission / policy / bounded failover
       durable receipt journal
  -> Product ServiceEndpointAdapter
       start_once / poll_once / reconcile_once / cancel_once
  -> one provider lifecycle wire
  -> ResolvedServiceResponse
```

关键边界：

- Tool 不拿 `RoleState`、credential、provider client 或 journal；
- Role 只发布 `invoke_service`，并在能力内部读取 ambient `tool_call_id`；
- Runtime 不 import 媒体 SDK 或 Product provider；
- Product adapter 不持有 retry、fallback、deadline 或 poll loop；
- Provider 的每个 lifecycle 方法只推进一次远端状态；
- pub-sub、直接调用、后台任务或前台 Tool 只是调用入口差异，不改变 Gateway 语义。

---

## 3. 执行安全语义

每个 `ServiceInvocation` 必须显式选择一种语义，并携带稳定 idempotency key：

| 语义 | 未知 submit 结果后允许的动作 | 典型能力 |
|---|---|---|
| `PURE` | 可在同 endpoint 重试，也可 fallback | 只读查询 |
| `IDEMPOTENT` | 仅在相同幂等域内用同 key 重试；未知结果时不跨 endpoint | 支持幂等键的媒体 submit |
| `RECEIPT_BASED` | receipt 前未知则 `IN_DOUBT`；receipt 后只 poll/reconcile | 不支持幂等键的异步任务 |
| `NON_REPEATABLE` | 未知即 `IN_DOUBT`，禁止自动重试 | 支付、发送、不可撤回动作 |

`IDEMPOTENT` 不等于“随便换 Provider”。幂等键通常只在一个 provider/base URL 的幂等域内成立。只有明确 `REJECTED`，或者调用是 `PURE`，Gateway 才允许跨 endpoint fallback。

媒体内建 adapter 向兼容 API 发送稳定 `Idempotency-Key`，所以当前媒体调用使用 `IDEMPOTENT`。如果未来某媒体厂商不承诺幂等键，它的 adapter/config 必须把调用改为 `RECEIPT_BASED`，不能由框架猜测安全性。

---

## 4. Lifecycle-neutral endpoint contract

```text
start_once(invocation, endpoint, timeout)
  -> ServiceCompleted(response)
  -> ServiceAccepted(receipt)
  -> ServiceFailed(definitive failure)

poll_once(receipt, endpoint, timeout)
  -> ServiceCompleted(response)
  -> ServiceAccepted(updated receipt)
  -> ServiceFailed(definitive remote terminal failure)

reconcile_once(invocation, endpoint, timeout)
  -> completed / accepted / failed / not found

cancel_once(receipt, endpoint, timeout)
```

契约使用远端生命周期事实，而不是 `sync_provider` / `async_provider` 两套接口。因此 one-shot 与 submit→poll 对 Tool 完全透明：one-shot 在 `start_once` 直接返回 completed；异步服务返回 receipt，Gateway 驱动 poll。

Adapter 方法不能：

- 内部循环 poll；
- 捕获 poll 异常后重新 submit；
- 自己轮换 key；
- 自己选择 fallback provider；
- 使用 SDK 默认 retry。

图片 reference URL 的输入获取和最终 URL 的本地 materialization 属于数据搬运，不属于生成任务的 submit/poll 状态机。它们失败不能触发重新生成；本地下载失败作为 `materialization_error` 返回，远端生成结果仍保持成功。

---

## 5. Receipt 不变量与崩溃窗口

Service journal 是 per-`service_call_id` 的 append-only、fsync JSONL：

```text
service_call_planned
service_attempt_started
service_receipt_accepted       # 可重复，poll_ordinal 连续
service_decision_applied
service_attempt_finished
service_call_finished
```

不变量：

1. submit 前先持久化 attempt started；
2. 收到远端 operation ID 后，先持久化 receipt，再开始 poll；
3. receipt 已存在时，poll 异常只能重试 poll，不能 start、rotate credential 或 fallback；
4. completed response 与 provider provenance 先持久化为 terminal checkpoint，再返回 Tool；
5. terminal success 的同一调用直接从 journal 返回，不再触网；
6. journal 不保存 API key，只保存 opaque credential slot 与 fingerprint；
7. deadline 跨进程恢复时从 durable `root_started_at` 继续计算，不在 resume 时重置。

崩溃恢复矩阵：

| Durable 状态 | Resume 动作 |
|---|---|
| terminal success | 直接返回 checkpoint |
| open attempt + receipt | 解析原 endpoint/slot，只 poll |
| open attempt、无 receipt | 先 `reconcile_once` |
| reconcile not found + `PURE/IDEMPOTENT` | 关闭旧 attempt 后按原安全语义重试 |
| reconcile not found + `RECEIPT_BASED/NON_REPEATABLE` | terminal `IN_DOUBT` |
| terminal failed/cancelled/in-doubt | 不自动触网 |

---

## 6. EffectLedger 协同

EffectLedger 保护顶层 Tool 调用；Service journal 保护 Tool 内部的远端服务生命周期。两层不能互相替代：

```text
tool_call_id
  -> EffectLedger: GenerateMedia started
  -> Service journal: N 个 asset service_call_id
       -> receipt / poll / terminal
  -> EffectLedger: GenerateMedia completed
```

`GenerateMedia.can_resume_started_call()` 返回 `True`。因此进程在 Tool body 中崩溃后，同一 durable tool call 会重新进入 Tool，再用完全相同的 asset operation key 进入 ServiceGateway；Gateway 对已完成 asset 返回 checkpoint，对有 receipt 的 asset 继续 poll，对从未开始的 asset正常执行。EffectLedger 不会先把它拦成 `<unknown-after-crash>`。

`service_call_id` 由以下稳定输入派生：

```text
session_id + tool_call_id + route + capability + operation_key
```

idempotency key 再加入 canonical payload hash。结果是：

- 同一 Agent、同一 tool call、同一 asset 重放得到相同 ID；
- 不同 Agent 即使 provider tool call ID 恰好相同，也得到不同 ID；
- 同一批媒体中每个 asset 有独立 journal、receipt、预算和重试游标；
- 一个 asset 或 Agent 的失败不会推进另一个调用的 cursor。

---

## 7. 媒体 reference implementation

`GenerateMedia` 的执行粒度从“每种媒体一个 batch retry”改成“每个 asset 一个 logical service call”。图片、TTS、音乐和视频仍可并发，部分成功仍保留。

Product 组成：

```text
MultimodalConfig
  -> build_media_service_snapshot
       media.image / audio / music / video routes
  -> MediaServiceEndpointResolver
  -> MediaProviderRegistry
  -> ImageCreator / AudioCreator / MusicCreator / VideoCreator
```

旧 `_generate_one_with_retry`、`_poll_until_done`、poll 失败时清空 task ID 以及 provider batch `generate()` 已删除。当前 creator 只提供：

- `start_once(item, idempotency_key, timeout)`；
- `poll_once(operation_id, state, timeout)`；
- 可选的 `reconcile_once`；
- 可选的 `cancel_once`。

配置尚只有每种 kind 一个 active provider，但 Runtime plan、resolver、credential slots 和 policy 已支持有序多 endpoint/多 credential。将来扩展配置时不改 Tool 或 Gateway。

### 7.1 WebSearch reference implementation

`WebSearch` 不再持有 backend factory，也不再依赖 Role 专用的 `web_search` 能力。它只提交一个稳定的服务调用：

```text
route = web.search
capability = web.search
operation_key = query
semantics = PURE
```

Product 将媒体与搜索各自编译的 snapshot 合并成一个无 secret 的 planner view，并通过组合 resolver 分发到对应 adapter family。route、group、endpoint 或 credential binding 重名会在装配期直接拒绝，不允许运行时出现模糊所有权。

provider-native 搜索仍由 ModelGateway 负责实际模型 endpoint/credential failover。WebSearch adapter 使用 `service_call_id` 派生稳定 `model_call_id`，把一次 ServiceGateway attempt 投影成同一个 ModelGateway logical call；因此崩溃对账或外层重入会命中模型 journal，不会重新建立第二套 LLM failover 游标。ServiceGateway 负责 Tool 级 identity、journal、恢复以及未来直连搜索 API 的 retry；ModelGateway 继续独占 LLM endpoint 选择。

直接搜索厂商通过 `SearchBackendRegistry` 注入 adapter。backend 的一次 `search()` 必须是一条 wire，不得包含 retry loop、credential rotation 或 provider fallback。由于查询声明为 `PURE`，连接失败可由 ServiceGateway 在预算内只重试失败请求；已成功的 terminal checkpoint 直接复用。

`WebSearch.can_resume_started_call()` 返回 `True`。进程在搜索中崩溃时，EffectLedger 允许同一 Tool call 重新进入 ServiceGateway；provider-native adapter 以相同 model call identity 从 ModelGateway journal 恢复，直连只读 backend 则按 `PURE` 语义安全 reconcile。

---

## 8. Pub-sub 无关性

Gateway 不订阅消息总线，也不要求 Tool 由某种调度器启动。它只依赖：

- 一个类型化 invocation；
- 一个稳定 logical call identity；
- 一个 durable journal；
- snapshot、resolver、policy 与 admission。

因此以下入口共享完全相同的故障语义：

```text
foreground Tool call
background task
pub-sub consumer
direct Application API
scheduled job
```

若入口没有 provider tool_call_id，组成层必须提供自己的稳定 operation identity；不能让 Gateway 读取 event bus、Agent mailbox 或 orchestration state。pub-sub 只是 transport，不是 failover domain。

---

## 9. 共享与隔离边界

允许跨调用共享的只有真实外部资源事实：

- endpoint availability breaker；
- credential quarantine；
- quota cooldown；
- endpoint bulkhead/in-flight；
- operator enable/drain/disable 状态。

必须 request-local 的状态：

- endpoint/credential cursor；
- attempt ordinal；
- receipt 与 poll ordinal；
- switch/rotation counters；
- deadline；
- last failure；
- response checkpoint。

当前部署只考虑单进程。不同 Agent 不需要分布式 lease，也不会发生 retry cursor 交叉；journal 的 stable ID 和进程内 per-call lock 防止同一 logical call 并发重复推进。

---

## 10. 扩展规则

新增一种托管 Tool 服务时：

1. 定义稳定 capability 与 route；
2. 明确四种 execution semantics 之一；
3. 在 Product 实现单次 wire adapter/provider；
4. 把无 secret endpoint 和 opaque credential slots 编译进 snapshot；
5. Tool 只通过 `invoke_service` 调用；
6. 覆盖 one-shot、accepted→poll、receipt resume、unknown submit、fallback、budget 和并发隔离测试；
7. 审计 provider 内不存在 retry loop、fallback cursor 或 SDK 默认 retry。

禁止：

- 给所有 BaseTool 套自动 retry；
- 以“媒体特殊”为理由复制第二套 gateway；
- 用 pub-sub topic 作为 receipt store；
- receipt 后因 poll 异常重新 submit；
- 用 model 名或 provider 名猜测幂等性；
- 把 API key、Authorization header 或完整 secret 写入 snapshot/journal。

这套边界使 ModelGateway 保持 LLM 专用，ServiceGateway 保持外部 Tool 服务通用，媒体只是一组 Product adapter，而不是新的架构中心。
