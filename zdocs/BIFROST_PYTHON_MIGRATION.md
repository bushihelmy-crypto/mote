# Bifrost 能力迁入 Mote 的 Python 实施设计

> 状态：核心架构终审通过，Gate 0设计有条件通过；仅批准生成和验证Gate 0工件，待全部签名证据与
> `GATE_0_APPROVED`完成后批准全面实施
>
> 目标：在 Mote 内用 Python 原生实现 Bifrost 全部网关能力与生态组件，保留并强化 Mote 已有的
> failover、journal、resume、流式提交和权限边界；所有能力通过同一个 Release Gate 后一次发布，
> 不保留长期双轨或待迁移能力。部署范围按最新决议为 Embedded + Shared Process；Cluster 仅冻结
> 前向兼容契约，生产实现与分布式 HA 不属于当前发布。
>
> 非目标：运行 Go Bifrost sidecar、包装 Bifrost HTTP API，或新建一套脱离 Mote 的远程网关。

## 0. 冻结的兼容基线

全量 parity 以本地 Bifrost 仓库 commit
`ec1dd920619955415bd6d61ab9ecff71f170ee22` 为唯一冻结基线，包括：

- 全部 provider 与其在该 commit 中真实支持或明确返回 unsupported 的 operation；
- 全部 RequestType、HTTP/WebSocket/WebRTC/MCP transport 和 SDK compatibility integrations；
- governance、logging、telemetry、OTel、semantic cache、compat、jsonparser、Maxim、mocker、
  model catalog resolver、prompts 等插件；
- provider/credential/MCP/session/config/cache/logging 管理面与 Web UI；
- recipes、容器与集群部署所表达的运行能力。

冻结 commit 的原因不是缩小范围，而是让“全部”成为可执行、可测试、可关闭的集合。开发期间
Bifrost 上游新增功能不改变本次 Release Gate；发布后由持续 parity 流程逐 commit 引入。若不冻结，
上游持续变化会使验收集合无穷增长，项目在工程上永远无法完成。

“零负债”在本文中只指：无重复语义、无临时兼容层、无隐藏 retry、无隐藏生命周期、无未清理
旧路径。它不意味着为未知需求制造抽象，也不意味着无视 Python 与分布式系统的客观边界。

## 1. 决策摘要

在 Mote 内新增一个长期存活、Application 级共享的内部 `GatewayDataPlane`，由四个窄 runtime facade
共同借用：

```text
RuntimeModelGateway | RuntimeServiceGateway | SessionGateway | existing Artifact transfer workflow
                    │ 四种显式 execution contracts
                    ▼
InferenceRuntime | ServiceCommandRuntime | SessionRuntime | ProviderArtifactTransferRuntime
                    │
                    ▼
GatewayDataPlane（Python）
  admission / fair queue / dispatcher / bulkheads / connectors / plugins / metrics
                    │
                    ▼
ProviderTransport（Python）
  OpenAI / Anthropic / Bedrock / Gemini / ...
```

当前正式发布交付 Embedded、Shared Process 两种部署形态；Shared Process 必须允许同机多个 Mote
进程通过一个 gRPC daemon 共享队列、连接池和治理状态。Cluster 的 mTLS gRPC、GenerationArtifact、
receipt 与配置契约在 Gate 0 冻结，但分布式 placement、生产实现和 HA certification 延后。Embedded 中一个 Mote
`Application` 只创建一个 runtime，所有 Role、Agent、Workflow 和后台任务共享它的连接池、
provider 队列、凭证健康状态与并发额度；远程形态只替换 `InferenceRuntime` adapter。

现有 `RuntimeModelGateway` 不删除。它继续拥有一次 logical model call 的重试、换凭证、换端点、
请求变换、journal 和 resume。新 runtime 承担 Bifrost 擅长的高并发数据面；每个显式授权的 wire
unit 最多产生一次 provider wire request/frame，且不同 operation lifecycle 不被压成同一个模型响应接口。

实施不是逐行翻译 Go，而是做功能迁移：

- 保留 Bifrost 的 provider bulkhead、有界队列、隔离调度、连接复用、过载保护、插件生命周期、
  多协议入口、治理、缓存和可观测性；
- 避免其巨型 `Bifrost` 类、胖 Provider interface、浅复制 fallback 和网关内隐藏 retry；
- 使用 `asyncio`、受控 HTTP/SSE/WebSocket/EventStream transport、Pydantic、Mote EventBus 与
  现有分层；
- 完整交付 HTTP/SDK compatibility，但 HTTP 不是内部调用的必经路径；
- 所有厂商保留原生语义：共享 wire-protocol family，不把所有模型商强制压成 OpenAI envelope。

选择“厂商 adapter + 协议族复用”的原因是：完全按厂商复制会造成协议代码重复，完全统一成
OpenAI 又会丢失 Anthropic cache control、Gemini cached content、Bedrock SigV4/EventStream、
OpenAI Responses/Realtime 等原生能力。正确复用单位是稳定 wire protocol，不是营销品牌，也
不是最小公分母 API。

---

## 2. 当前源码问题与迁移切入点

### 2.1 当前调用路径

```text
Role
 → LLMRouter / ModelRoute
 → RuntimeModelGateway.execute()
 → ProductModelEndpointResolver.resolve()
 → providers.create(config)
 → 新 ProductModelEndpointAdapter + 新 BaseLLM + 新 SDK client
 → execute_once()
```

`ProductModelEndpointResolver.resolve()` 当前为每个 endpoint/credential 解析创建一个新的 provider
实例。虽然 generation drain 会关闭 resolver，但 resolver 本身没有共享 adapter/client pool。
Agent Swarm 下会出现：

- SDK client 与连接池重复创建；
- 没有 provider 级 ingress queue 和稳定 worker 数；
- `ResourceAdmissionController` 主要保护单个 gateway 实例，没有公平等待队列；
- 所有 agent 同时进入 provider SDK，过载只能在较晚阶段暴露；
- provider、媒体生成、web search 分别持有网络客户端，生命周期与治理不统一。

迁移切入点不是 Kernel，也不是 Role，而是：

1. `contracts/ports/model_endpoint.py` 的单次 wire adapter seam；
2. `product/models/gateway.py` 的 resolver；
3. `product/models/bootstrap.py` 和 `ProductContainer` 的 Application 级装配；
4. `runtime/models/model_gateway.py` 调用 adapter 的位置。

### 2.2 必须保留的 Mote 语义

- `FailoverPlan` 与 attempt budget；
- `ModelCallJournal` 和 `resume()`；
- failure classification → retry/rotate/transform/switch/abort；
- generation lease 与 drain；
- attempt stream capture/commit/discard；
- opaque credential slot；
- capability/governance/region 计划期过滤；
- cost、quota 与完整 usage 统计。

任何 Bifrost 功能都不能绕过这些边界。

---

## 3. 唯一所有者与核心状态模型

### 3.1 唯一所有者矩阵

| 语义 | 唯一所有者 | 明确禁止 |
| --- | --- | --- |
| logical call、计划、fallback、retry、换凭证 | `RuntimeModelGateway` | runtime/transport 自行重试或换路由 |
| journal、resume、attempt budget | `RuntimeModelGateway` | 数据面维护第二本恢复账 |
| unary attempt ordinal/permit | `RuntimeModelGateway` | receipt/runtime 自行决定或扩大 unary wire budget |
| durable command ordinal/permit | `RuntimeServiceGateway` | ModelGateway 或 poll worker自行签发 |
| session message ordinal/permit | `SessionGateway` | session transport 自动重放/签发 |
| transfer part/range ordinal/permit | existing `runtime.artifacts` transfer workflow | upload worker 自行重试/签发 |
| 公平队列和容量调度 | 内部 `GatewayDataPlane` | 四个 runtime facade 各自排队和超卖 |
| wire in-flight、provider bulkhead | 内部 `GatewayDataPlane` | facade/transport 私有 semaphore |
| 单次 provider wire request | `ProviderTransport` | SDK、proxy、HTTP client 隐式 retry |
| routing | 现有 `RoutingService`/`FailoverPlanner` | governance/HTTP handler 建第二套路由 |
| credential secret 解析 | Product `CredentialBindingProvider` | Runtime、journal、queue 看见明文 secret |
| usage/cost/quota 原始提取 | `ProviderTransport` | 日志事后猜测原始 usage |
| tenant/project budget reserve/settle | `UsageLedger` | admission、插件和 handler 分别扣账 |
| provider request/token quota 与 retry-after | `ProviderQuotaAuthority` | UsageLedger 变成 quota 巨型组件 |
| credential quarantine/health | `CredentialHealthAuthority` | resolver 与 gateway 各自维护健康 |
| availability breaker | `BulkheadController` | transport 私有 breaker |
| 数据面不可逆 attempt receipt | `AttemptReceiptStore` | 复制 logical journal 或只靠事件流 |
| stream commit/discard | `RuntimeModelGateway` | transport 直接宣告 logical success |
| 外部实时流提交点 | compatibility handler | 首 chunk 后透明 fallback |
| audit/metrics export | EventBus subscriber | worker 热路径直接写 exporter |
| Tool/MCP agent loop | 现有 `ToolExecutor`/Agent Flow | gateway 复制第二个 tool loop |
| 配置构建与原子发布 | Product composition + `GatewayRuntimeGeneration` | 四类 owner 各自 reload |
| Shared artifact/activation log | Shared daemon configuration authority | caller 各自选择“最新配置” |

该矩阵是最重要的不变量。原因是高并发本身不会产生架构负债，多个组件同时拥有 retry、budget、
routing 或 lifecycle 才会。每个状态只允许一个写入者，其他组件通过 typed event/port 观察。

现有 `ResourceAdmissionController` 必须拆分：

- 公平排队、容量、wire in-flight 移入唯一 `GatewayDataPlane`；
- tenant/project 预算移入 `UsageLedger`；provider quota/cooldown 移入独立
  `ProviderQuotaAuthority`；
- credential quarantine 由 credential health authority 持有；
- availability breaker 按 `BulkheadIdentity` 由 runtime 持有；
- operator enable/drain/disable 进入统一 generation/control state。

“拆分”指从现有实现中提取 authority 与状态机、保持行为证据，不是删除后从空白重写。现有 operator、
breaker、credential quarantine、quota cooldown、in-flight 和 audit 测试迁移为各新 authority 的
conformance suite；在新旧测试等价通过前不得删除原 controller 路径。

迁移完成后不存在两个 controller 同时维护相同 resource 的容量、quota、breaker 或 cooldown。

### 3.2 Attempt 状态机

```text
PLANNED
  ├─ ADMISSION_REJECTED
  ├─ CACHE_HIT
  └─ QUEUED
       ├─ QUEUE_CANCELLED
       ├─ QUEUE_EXPIRED
       ├─ BUDGET_REJECTED
       └─ BUDGET_RESERVED
            ├─ RESERVATION_EXPIRED
            └─ WIRE_AUTHORIZED
                 └─ DISPATCHED
                      ├─ DISPATCH_FAILED
                      └─ WIRE_PREPARED
                           ├─ PRE_WIRE_FAILED
                           └─ SEND_COMMITTED
                                └─ WIRE_STARTED
                                     ├─ RESPONSE_STARTED
                                     │    ├─ SUCCEEDED
                                     │    ├─ FAILED
                                     │    └─ CANCELLED
                                     ├─ FAILED
                                     ├─ CANCELLED
                                     └─ IN_DOUBT
```

状态规则：

- `PLANNED` 后先写 journal，再允许 admission；
- admission 与 queue entry 只检查 budget feasibility，不长期占用完整预算；
- dispatch 前通过 `(attempt_id, generation_id)` 幂等键原子 reserve，生成带 expiry 与 fencing token
  的 `BUDGET_RESERVED`；
- admission、排队、dispatch、DNS、client acquisition、TCP/TLS 建连均不消耗 wire-attempt budget；
- 对 unary attempt，只有 `RuntimeModelGateway` 能原子 claim provider attempt ordinal、写 journal
  `WIRE_AUTHORIZED` 并签发唯一 `WirePermit`；签发即保守消耗 wire-attempt budget；其他 taxonomy
  由第 6.1 节对应 logical owner 在自己的 journal 中执行同构授权，绝不由数据面签发；
- permit 签发前取消可释放 reservation；签发后即使没有真正发送也不能释放并复用 ordinal；
- `SEND_COMMITTED` 只能消费已有 permit，不能 claim、生成或扩大 wire budget；其后只能 settle 或
  进入 reconciliation；
- settle 使用 attempt ID 幂等，reservation expiry 只能由持有更高 fencing token 的 authority 回收；
- queue/admission failure 是 logical-call decision fact，不伪装成 provider failure；
- `WIRE_STARTED` 后进程或连接丢失且无法证明 terminal，必须进入 `IN_DOUBT`；
- terminal 状态互斥且只写一次；
- 每个转换附带 generation ID、原因、时间和 actor；
- cache hit 有独立 journal event，不产生 `WIRE_STARTED`。

因此现有 `AttemptOrchestrator` 必须从“调用 execute_once 前递增 wire_attempt”改成显式授权：先创建
attempt candidate 并预留预算；选定可执行 candidate 后，由 `RuntimeModelGateway` 在同一 journal
commit 中 claim ordinal、记录 `WIRE_AUTHORIZED` 和 permit digest，再把 permit 交给 runtime。并发
hedge 的每个 attempt 必须分别取得不同 ordinal/permit，禁止共享或由多个 remote 竞争同一 ordinal。

```text
WirePermit(
  attempt_id,
  execution_taxonomy,
  owner_journal_id,
  wire_unit,
  generation_id,
  generation_artifact_digest,
  ordinal,
  nonce,
  issued_journal_revision,
  expires_at,
  issuer_key_id,
  backup_epoch,
  admission_epoch,
  signature,
)
```

permit 是版本化、签名且 audience-bound 的 capability，只能用于完全匹配的 taxonomy/owner journal/
wire unit/attempt-or-command/generation/artifact/ordinal。receipt store 以 permit digest 建唯一约束并 CAS 消费；重复投递同一 permit返回原
receipt，篡改、过期或绑定不符结构化拒绝。permit 可以在 caller 恢复后从 journal 重建并幂等重投，
但不能用于另一 attempt、换 generation 或产生第二个 send commit。issuer credential 按下节的
deployment trust model签发和轮换，remote只持验证材料，receipt authority无签发能力。

`backup_epoch` 与 `admission_epoch` 是 Shared application-consistent backup barrier 的强制绑定字段。
caller 确认 `PREPARE_BACKUP(epoch)` 后不得签发旧 epoch 或新 wire permit；daemon 在 barrier 生效后拒绝
首次消费 epoch 过旧或不匹配的 permit。已 `SEND_COMMITTED` 的 permit仍按 receipt事实恢复，不能因 barrier
回滚。未参与或未确认 barrier 的 caller 必须进入缺失参与者清单，不能把该 cut 标记为
`APPLICATION_CONSISTENT`。

#### 3.2.1 WirePermit trust model

保持同一个 `WirePermit` payload/verifier interface，但按部署使用最小信任域：

- **Embedded**：composition 创建进程内不可序列化 capability object；不需要签名服务，object identity
  与 journal revision共同授权；
- **Shared**：caller 通过 UDS peer credential + Application identity 完成 handshake，daemon 为该连接/
  caller incarnation 签发短期 session signing credential。`RuntimeModelGateway` 用它签发并 journal
  permit，daemon 本地 verifier 验证；credential 绑定 UID、daemon socket generation、caller PID/
  incarnation、tenant scope 和 expiry，断线或 daemon generation retire 后不能用于新 permit；
- **future_cluster**：只保留 issuer/key ID、audience、signature、trust revision envelope；configuration
  authority/HSM chain、跨节点 revocation/quorum 的具体协议不在当前 Gate 0 冻结，待 Cluster 实现前基于
  实测威胁模型另行评审。

- permit audience 当前绑定 deployment、daemon socket generation、runtime service、tenant scope、
  generation artifact digest 与 wire protocol；跨用户、跨 daemon、跨 runtime 或换 generation拒绝；
- canonical encoding 固定为 RFC 8785 JSON（禁止 float、重复 key 与未规范 Unicode），签名固定为
  Ed25519；算法、encoding version 和 domain-separation tag 都进入 signed payload，禁止 algorithm
  confusion；
- Shared issuer credential 使用短 TTL 和不可复用 serial；daemon 保存有界 session registry，admin 可按
  UID/caller incarnation立即 revoke。revoke 后新 permit 拒绝，已 `SEND_COMMITTED` 的事实不回滚；
- permit `not_before`/`expires_at` 使用 UTC，只允许配置中冻结的小 clock-skew tolerance，并设绝对最大
  lifetime；daemon 以本地 monotonic timer约束已验 permit 的剩余窗口，过期 permit不得首次消费；
- replay 由 `(issuer_id, permit_nonce)` 与 permit digest 的 receipt 唯一约束阻止；相同 digest 幂等返回，
  nonce 相同而 payload 不同视为安全事件并拒绝；
- 未知/过期/撤销 key、错误 audience/trust revision/signature/encoding 分别返回结构化
  `PERMIT_ISSUER_UNKNOWN`、`PERMIT_ISSUER_EXPIRED`、`PERMIT_ISSUER_REVOKED`、
  `PERMIT_AUDIENCE_MISMATCH`、`PERMIT_TRUST_STALE`、`PERMIT_SIGNATURE_INVALID`，且均不得发送 wire。

Shared daemon 的长期 identity key 存在 OS-protected per-user runtime state，caller 只持短期 session
credential，不要求部署 HSM、trust watch 或分布式 revocation service。这样保持 wire budget owner 与
receipt verifier 分离，同时不把 future Cluster machinery强加给单机多进程。

数据库事务与网络发送无法原子提交。为保证 crash recovery 和同一 attempt 最多一次发送，正式
语义采用两个保守点：caller 在 `WIRE_AUTHORIZED` 签发 permit 时先消耗 wire budget；transport 验证并
原子消费 permit，把 `SEND_COMMITTED` 写入 receipt authority，
提交成功后才允许进入协议特定 irreversible boundary；同一 `(attempt_id, generation_id)` 此后永不
重发。`WIRE_STARTED` 表示 transport 已进入该 boundary，但若 worker 在两者之间崩溃，恢复仍将
该 attempt 视为可能已发送并对账/`IN_DOUBT`，不能安全重试。

选择这一语义的原因是不存在同时原子覆盖 PostgreSQL 和外部 provider socket 的事务。声称能够
精确区分授权/提交/网络之间的崩溃窗口会制造虚假的 exactly-once；保守地偶尔消耗一个未实际发送
的 permit，换取
绝不因不确定性重复外部副作用，是正确的长期边界。

`WireLifecycleSink` 只允许 transport 发出一次 `wire_started()` 和一次可选
`response_started()`；重复、乱序或 terminal 后调用立即视为 transport contract violation。Shared/
Cluster adapter 原样转发这些 sequence，不得自行合成成功事实；连接丢失时由 receipt 对账，证据
不足即 `IN_DOUBT`。

选择严格边界而不是“调用 SDK 前就计数”的原因是后者会把 DNS/TLS/连接池失败错误计入 provider
wire budget，长期会扭曲恢复、成本和可靠性统计。官方 SDK可以作为协议参考、测试 oracle 或
开发工具，但正式路径必须关闭并验证其 retry，或完全不参与实际发送。

### 3.3 AttemptReceiptStore

`AttemptReceiptStore` 不是第二本 logical-call journal。它只保存数据面不可逆事实，供远程连接丢失、
worker crash 和 caller resume 对账：

```text
ReceiptKey = (attempt_id, generation_id)

ACCEPTED
→ SEND_INTENT_DURABLE
→ PERMIT_CONSUMED / SEND_COMMITTED
→ WIRE_STARTED_OBSERVED
→ PROVIDER_ACK(provider_request_id?)
→ TERMINAL_SUCCEEDED | TERMINAL_FAILED | TERMINAL_CANCELLED | IN_DOUBT
```

最小记录包含 attempt/generation/artifact digest、permit digest/ordinal、receipt revision、worker lease/fencing token、request digest、
operation/idempotency class、deadline、send intent、wire observation、provider request ID、terminal
digest、artifact references 和 timestamps；绝不保存明文 secret。

规则：

- Embedded 与单 active Shared daemon 使用本地 SQLite durable authority；Shared 可选 PostgreSQL，
  future Cluster 才强制 PostgreSQL；
- worker 接受请求先幂等写 `ACCEPTED`；发送前验证 permit 与 request/artifact binding，持久化 intent，
  再以 permit digest 唯一约束事务提交 `PERMIT_CONSUMED/SEND_COMMITTED`；
- receipt 状态只单调前进，所有写入 compare-and-swap revision + fencing token；
- `SEND_COMMITTED` 与 terminal 同事务写 transactional outbox，独立 publisher 向 caller 推送；
- outbox 至少一次投递，caller 按 receipt revision/event sequence 去重；
- provider request ID 得到后立即持久化，但其缺失不证明请求未发送；
- terminal response payload 放加密 artifact store，receipt 只保存 digest/reference；
- retention 严格长于 logical-call 最大 resume 窗口、最大 provider async lifecycle 和审计保留下限，
  清理前验证没有 open/in-doubt reconciliation；
- caller resume 先 replay logical journal，再查询 receipt authority，按更强数据面事实补写 journal；
- receipt 不做 routing、retry、budget decision，也不保存完整 conversation，因此不是第二本 journal。

当 caller 未收到 `SEND_COMMITTED` event 但 receipt 已提交时，以 receipt 为准。若 receipt 只有
`SEND_INTENT_DURABLE` 且旧 lease 已失效，因为协议保证 commit 前绝不发送，可以用更高 fencing token
和 journal 中同一个 permit 恢复提交；不得签发新 permit。若已 committed 且无 terminal，则不能把
同一 attempt 重新入网，只能查询 provider idempotency/request API、等待原 worker lease，或标记
`IN_DOUBT`。

### 3.4 Deadline 模型

“端到端只存在一个 deadline”保留，但不能跨进程直接传绝对 monotonic 值，因为每个进程和机器
的 monotonic clock 原点不同。契约同时携带：

```text
deadline_utc
remaining_seconds_at_send
sent_at_utc
```

每个进程接收边界只转换一次：

```text
safe_remaining = min(
  remaining_seconds_at_send - measured_transport_elapsed,
  deadline_utc - local_utc_now - configured_clock_skew_guard,
)
local_deadline = local_monotonic_now + max(safe_remaining, 0)
```

进程内部所有 queue、connector、wire、stream 和 settlement 共享 `local_deadline`，不得逐层重新给
完整 timeout。采用 UTC + remaining 双信号的原因是 UTC 可跨进程但受 clock skew 影响，remaining
更稳定但传输会消耗预算；取保守值同时避免超时延长与不同 monotonic 原点错误。

### 3.5 单一 GatewayRuntimeGeneration

把现有 `AtomicModelRuntime` 演进为 Application 级原子 generation owner，不在旁边新建第二个 manager：

```text
GatewayRuntimeGeneration
├─ model planner/bindings
├─ service planner/bindings
├─ session capability/bindings
├─ transfer capability/bindings
├─ credential handles and versions
├─ transport registry revision
├─ canonical failure policy revision
├─ capability/model catalog/pricing snapshot
├─ governance and ledger policy revision
├─ cache policy revision
├─ plugin pipeline revision
└─ shared data-plane policy/connection-pool handles
```

四个 logical owner 只能取得同一 immutable generation 的窄 lease：`model_view()`、`service_view()`、
`session_view()`、`transfer_view()`。view 不可独立 reload、改变 revision 或延长自身 retention；未持有对应
view 的 owner 看不到其他领域内部 binding。

```python
class GatewayGenerationOwner(Protocol):
    def acquire(self) -> GatewayGenerationLease: ...

class GatewayGenerationLease(Protocol):
    generation_id: str
    artifact_digest: str
    def model_view(self) -> ModelGenerationView: ...
    def service_view(self) -> ServiceGenerationView: ...
    def session_view(self) -> SessionGenerationView: ...
    def transfer_view(self) -> TransferGenerationView: ...
```

Product composition 将同一个 owner 注入 `RuntimeModelGateway`、`RuntimeServiceGateway`、provider session
owner 和现有 artifact transfer workflow。`RuntimeServiceGateway` 不能继续旁路 generation 使用可变
resolver；每个 create/poll/cancel command 从 pinned `ServiceGenerationView` 解析 binding。四者都没有
reload API，只有演进后的 atomic generation owner 可以 activate。

配置流程固定为：一次读取 → 一次完整校验 → 一次构建 → 一次原子发布。旧 generation 持续处理
已接受和已排队请求继续使用 pinned view。model call 到 terminal、durable operation 从 create intent 到
resource terminal/reconciliation、provider session 到 close、transfer 到 digest verify/abort 都 pin 同一
generation ID。model/service/session/transfer 四类 lease、queue、receipt reconciliation 与 connection
handle 引用全部归零后才能 retire。connection pool 可以跨 generation 复用稳定 fingerprint lease，但
generation 不能原地修改 transport policy。

单 generation 的原因是 planner 已切换而 client/治理仍旧、或 queue 已新而 credential 仍旧都会
产生不可重放的撕裂状态。原子 generation 是 config revision 与实际执行事实能够对账的前提。

#### 3.5.1 Shared Generation Protocol 与 future envelope

进程内对象不是 Shared 多进程的共同配置真相。Shared daemon 发布不可变、内容寻址的
`GenerationArtifact`，各 caller 与 daemon 从同一 artifact 构建本地
`GatewayRuntimeGeneration`：

```text
GenerationArtifact(
  schema_version,
  generation_id,
  parent_generation_id,
  model_planner_and_bindings,
  service_planner_and_bindings,
  session_capability_and_bindings,
  transfer_capability_and_bindings,
  credential_versions,
  transport_registry_revision,
  failure_policy_revision,
  capability_catalog_pricing_snapshot,
  governance_cache_plugin_revisions,
  required_wire_contract_range,
  activation_policy,
  artifact_digest,
  signer_key_id,
  signature,
)
```

artifact 使用确定性 canonical encoding；digest 覆盖除 signature 外全部字段，签名覆盖 digest、用户/
环境和 daemon audience。artifact 不包含 secret、OAuth token、live connector/client 或节点地址；credential
只含 opaque slot/version，`CredentialBindingProvider` 在实际目标节点按 tenant/slot/version 本地解析。
本地 generation 是 artifact 的只读投影加引用计数 handle，不能改变 artifact 语义。

发布状态机固定为：

```text
DRAFT → BUILT → VALIDATED → STAGED → ACTIVE
                              └──────→ REJECTED
ACTIVE → DRAINING → RETIRED → GC_ELIGIBLE
```

规则：

1. 唯一 Shared daemon configuration authority 分配 generation ID、保存 artifact/signature 与 activation
   log；caller 不能自行修改或“补全” artifact；
2. stage 时 daemon 验证 schema、digest/signature、N/N-1 Shared RPC contract、provider capabilities、plugin
   provenance 和本地依赖；active caller 只接受 daemon 明确公布为 STAGED/ACTIVE 的 digest；
3. model journal、`ServiceReceipt/ServiceCallJournal`、provider-session journal、artifact-transfer journal、
   request、`WirePermit` 与数据面 receipt 必须记录完全相同的 generation ID 和 artifact digest；daemon
   未 staged、digest 不同或 contract range 不相交时返回结构化
   `UNKNOWN_GENERATION`/`GENERATION_DIGEST_MISMATCH`/`WIRE_CONTRACT_UNSUPPORTED`，且不得 admission、
   reserve budget 或发送 wire；
4. 四类 logical owner 按上述完整 lifecycle pin generation view；RPC 先协商 N/N-1 contract，再按 digest
   dispatch，禁止 remote 用“最近版本”代替；
5. activation 失败只写 `REJECTED`，旧 ACTIVE generation 保持 ready；升级创建新 socket generation，
   rollback 是重新激活已验证 artifact，不改写历史；
6. 旧 generation 停止接收四类新 execution 后进入 DRAINING。caller/daemon 分别维护四类 journal、
   queue、active session/operation/transfer、connection/credential lease 与 receipt reconciliation 引用；
   全部归零才能 RETIRED；
7. `max_residency` 到期不能强删活跃 generation：先停止 readiness、取消尚未 wire-authorized 的项，
   对已授权/已发送项 drain 或标记 reconciliation/`IN_DOUBT`。只有超过 receipt/resume/audit retention、
   无活跃引用且至少一个 N/N-1 兼容窗口结束后才 `GC_ELIGIBLE`；
8. artifact/blob GC 与 activation log GC 分离；activation、journal 和 receipt 中使用过的 digest 保留
    可审计 tombstone，不能因删 artifact 失去历史解释能力。

future Cluster 在 Gate 0 只冻结 generation ID namespace、schema-version envelope、artifact digest、
receipt ID、RPC service/message naming 和 migration extension point。跨 failure-domain quorum、placement、
canary、lease/fencing 与 Cluster rolling upgrade 不冻结为当前 contract，也不进入 Release Gate；实现前
必须以真实部署反馈重新设计和评审。当前协议只保证 Embedded/Shared 中 planner 不会针对 artifact A
规划、daemon 却用 artifact B 的 transport/pricing/plugin 执行。

#### 3.5.2 持久化 schema migration protocol

代码/contract 的 N/N-1 兼容不自动代表 PostgreSQL/SQLite 状态兼容。每个 GenerationArtifact 必须声明
`min_reader_version`、`min_writer_version`、各 journal/receipt/outbox/ledger schema version，以及已完成
的 migration set digest。节点 stage/readiness 会验证这些约束。

迁移只允许 expand → dual-compatible code → backfill/verify → activate new writer → contract：

1. **expand** 先执行可回滚、向后兼容的 additive migration；N 与 N-1 reader 都必须能读取，旧 writer
   写出的记录新 reader 也能读取；
2. **dual-compatible** 代码对新旧字段使用一个 canonical semantic owner；需要双写时必须在同一事务、
   有明确比较器与删除 deadline，不能形成两本 journal；
3. **backfill/verify** 使用带 checkpoint/fencing 的有界后台任务，完成率、错误和 hash reconciliation
   进入 readiness，但不阻塞现有 generation 的正常读取；
4. **new writer activation** 仅在所有 required failure domain 达到 compatible quorum、rollback 版本确认
   能读取新记录后开启；receipt/journal/outbox envelope 必须携带 schema version，未知高版本 fail closed
   并保持原记录；
5. **contract** 只有 N-1 全部 drain、最长 resume/receipt/outbox retention 结束、备份与恢复演练通过后
   才允许。drop/rename/type narrowing 等 destructive migration 禁止进入普通 generation rollout，必须
   走独立 maintenance approval，且不能作为线上 rollback 的前置条件。

migration 失败时新 generation `STAGED/REJECTED`、readiness 明确报告 migration reason，旧 ACTIVE
generation 和旧 schema writer 保持不变；禁止“部分节点继续 activation”。rollback 只能回到仍能读取
所有已写 record version 的代码。SQLite Embedded 同样执行该协议，只是 quorum 为单 writer，并在
expand/contract 前做 crash-safe backup。migration history 是 durable authority，不能随 generation GC。

### 3.6 Credential 生命周期

Runtime 永远只看 opaque `credential_slot_id` 与 `credential_version`。Product 提供：

```python
class CredentialBindingProvider(Protocol):
    async def resolve(
        self,
        endpoint: EndpointDescriptor,
        credential_slot_id: str,
        credential_version: str,
    ) -> CredentialHandle: ...
```

`CredentialHandle` 不允许序列化、repr secret 或进入 journal，提供受限的 header/signing/client-auth
能力。生命周期要求：

- OAuth refresh 按 `(issuer, subject, client, slot)` single-flight；
- refresh token 通过 encrypted credential store 持久化；
- access token 与 static secret 均有 immutable version；
- rotation 创建新 version，新 generation 引用新 handle，旧请求继续使用旧 handle至 drain；
- refresh failure 分类为 credential failure，由 `RuntimeModelGateway` 决定 rotate/fallback；
- redaction 覆盖 header、URL query、exception、trace、audit 和 core diagnostic；
- connector 仅在 proxy/TLS/authority 相同且不携带 auth state 时跨 credential 共享；
- SDK auth object、SigV4 signer、OAuth session 等可能含状态的对象按 credential/version 隔离；
- handle 引用归零才销毁 secret material 和 auth client。

明确区分 connector 与 auth client 的原因是“每 credential 一个完整连接池”浪费连接，而“所有
credential 共用一个带默认 header 的 client”会造成严重串密钥风险。

### 3.7 Canonical FailureDisposition

Gate 0 扩展现有 `contracts.model.failover.FailureDisposition`，不建立 gateway 私有错误体系。冻结 schema
至少包含：

```text
FailureDisposition(
  schema_version,
  domain,                    # transport|provider|credential|quota|policy|budget|protocol|internal
  reason,                    # versioned low-cardinality enum
  retryability,              # never|new_attempt|after_hint|reconcile_only
  health_verdict,            # neutral|success|degrade|open_breaker
  external_commit_state,     # not_committed|committed|unknown
  credential_verdict,        # neutral|refresh|quarantine|revoke
  quota_observation,         # none|limits|retry_after|exhausted|malformed
  provider_code,
  safe_message,
  reconcile_strategy,        # none|provider_query|webhook|receipt_wait|manual
  usage_observation,
  http_compatibility_class,
)
```

字段均为 typed enum/value object；`safe_message` 经过统一脱敏，原始 body/exception 只可进入受控加密
audit。`retryability` 只是给 logical owner 的输入，不授权 transport retry。`external_commit_state` 与
receipt/wire evidence 一致，provider error text 不能自行把 `unknown` 降为 `not_committed`。

每个 provider error fixture 必须恰好映射一个 canonical disposition，并在 conformance table 明确：

- `RuntimeModelGateway`/其他 logical owner 是否允许新 attempt、fallback 或只 reconcile；
- `BulkheadController` 是否计入 availability；
- `CredentialHealthAuthority` 是否 refresh/quarantine/revoke；
- `ProviderQuotaAuthority` 是否更新 limits/retry-after/cooldown；
- `UsageLedger` 是否 release、settle known usage 或 pending reconciliation；
- OpenAI/Anthropic 等 compatibility handler 的 HTTP/status/error envelope；
- receipt 的 terminal、cancel 或 `IN_DOUBT`/reconcile transition。

这些消费者只读取各自 verdict，不能从 provider code/message 二次推断。未知 provider 错误映射为安全
的 `provider/unknown`、保留 external commit evidence 并默认不伤害 credential/quota；只有明确的
availability evidence 才影响 breaker。canonical failure schema revision 是 Gate 0 冻结产物，provider
团队只能新增已评审 fixture/mapping，不能私加 reason 或改变既有语义。

---

## 4. 功能迁移矩阵

| Bifrost 能力 | Mote Python 落点 | 处理方式 |
| --- | --- | --- |
| provider 独立 queue/worker | `runtime/inference/{fair_queue,dispatcher,bulkhead}.py` | 迁移隔离能力，消除 worker+semaphore 双限流 |
| concurrency/buffer size | `contracts/config/inference.py` | endpoint/provider/全局三级配置 |
| drop excess requests | admission policy | 支持 reject、wait、deadline 三种明确策略 |
| SDK/HTTP 连接复用 | `product/models/transports/` | Application 级 client pool |
| key selection/rotation | 现有 failover + credential slots | 不在数据面重复实现 |
| core retry/fallback | 现有 `AttemptOrchestrator` | Bifrost retry 禁止迁入数据面 |
| provider 热更新/drain | 顶层 `GatewayRuntimeGeneration` | 演进现有 AtomicModelRuntime，不建第二 generation manager |
| pre/post hooks | typed plugin pipeline | 迁入，但禁止任意修改内部对象 |
| stream chunk hooks | typed stream observers | 迁入，不能破坏 commit/discard |
| OpenAI-compatible HTTP | `product/interfaces/inference_api/` | 首版正式入口 |
| Anthropic/Bedrock/Gemini compatibility | 同上 integrations | 外部 wire → canonical invocation |
| virtual keys | Product identity/store/admin → `InferencePrincipal` → Runtime policy | Runtime 不接触 key 格式/hash/管理 API |
| budget/rate limit | admission + usage ledger | 强制治理，不依赖日志反算 |
| governance routing | 现有 RoutingService + policy adapter | 不建立第二套路由器 |
| semantic cache | `runtime/inference/cache/` | canonical request digest；默认 opt-in |
| audit logging | EventBus subscriber | payload 与普通日志分离 |
| Prometheus/OTel | telemetry adapters | typed events → exporters |
| MCP gateway/agent loop | 现有 MCP/tool execution | 复用能力，不复制第二个 Agent loop |
| provider CRUD/UI | Product 管理 API 与 Web UI | 全量迁移 |
| batch/files/video 等 | operation capability plugins | 全量迁移，不污染 generate interface |
| WebSocket/Realtime | transport capability | 全量迁移，不伪装 generate |
| Go object pools | 不直接迁移 | Python 优先减少对象和复制，profile 后再池化 |

### 4.1 Mote 基础设施复用矩阵

| 新需求 | 必须复用的现有实现/语义 | 允许新增 | 明确禁止 |
| --- | --- | --- | --- |
| logical model call | `RuntimeModelGateway`、现有 failover/journal/stream commit | inference attempt seam | 第二个 logical-call gateway |
| durable operation | `RuntimeServiceGateway`、`ServiceInvocation/Plan/Receipt/CallJournal/ExecutionSemantics` | 单次 provider command runtime | 第二套 service plan/journal/poll/`IN_DOUBT` |
| artifacts | `ArtifactStore`、`ArtifactBlobStore`、`ArtifactResolver`、`ReliableArtifactPublisher`、现有 GC/ownership/transfer/session roots | 实现现有 port 的 object-store/remote blob backend与必要 contract extension | inference 私有 `ArtifactRef`、catalog、publisher、GC/store |
| lifecycle | `LifecycleStack`、`EngineServices` | `GatewayDataPlane.lifecycle_resources()` 聚合真实 owner resources | inference shutdown coordinator/lifecycle framework |
| config | 现有 Product Config、`YamlModel`、layered loader/override/secrets/watcher/unknown-key validation | `contracts/config/inference/` typed 子配置 | gateway config loader、override、env mapper或 watcher |
| events | `contracts/events` + `runtime/events` EventBus | inference tagged event variants/subscribers | 第二个 event bus/export pipeline |
| failures | 现有 `FailureDisposition` | 评审后的 enum/evidence 字段 | inference 私有 failure hierarchy |
| admission | 现有 `ResourceAdmissionController` 语义、状态迁移和测试 | 从现有实现提取的独立 authorities | 无行为证据的删除重写 |
| MCP tools | `ToolExecutor`、capability allowlist、permission classifier | gateway transport/catalog接入 | 第二个 MCP agent/tool loop |
| journal | Local model/service/session/event journal 的 append/recover/version/integrity 模式 | 各领域 tagged record variants/窄 store port | generic utils journal framework或各自 JSONL/SQLite writer |
| provider session | 现有 session event envelope、sequence validation、projection、artifact ref、lifecycle/持久化约定 | provider-transport session state machine | 复用用户会话业务模型或另造持久化框架 |

复用是 workgraph 的硬约束，不是实现建议。新增基础设施节点必须在 YAML 中提供
`existing_capability_gap`、被评估的现有 port/implementation、为何不能安全扩展以及 architecture approval
ID；否则 CI 拒绝该节点。未经批准不得创建第二套 store、artifact catalog、lifecycle、config loader、
event bus、journal framework、scheduler、service receipt 或 polling semantics。

---

## 5. 目标代码结构

```text
contracts/
  config/inference/
  inference/
    admission.py
    request.py
    principal.py
    events.py
    governance.py
    plugins.py
  ports/inference/
    inference_runtime.py
    provider_transport.py
    connection_pool.py
    identity.py
    receipt_store.py
    inference_cache.py
    usage_ledger.py
  ports/service/
    command_runtime.py
  ports/artifact/
    provider_transfer.py

runtime/
  inference/
    data_plane.py
    runtime.py
    fair_queue.py
    dispatcher.py
    bulkhead.py
    plugins.py
    streaming.py
    receipts.py
    usage.py
    governance/
      policy.py
      budgets.py
      rate_limits.py
    cache/
      service.py
      keys.py
    telemetry.py
  models/                         # existing RuntimeModelGateway
  service_gateway/                # existing RuntimeServiceGateway
  artifacts/                      # existing store/publisher/GC/transfer
  events/                         # existing EventBus
  control/                        # existing LifecycleStack

product/
  inference/
    backends/
      sqlite/
      postgres/
      redis/
      s3/
      qdrant/
    identity/
      virtual_keys.py
    daemon/
      server.py
      client.py
      supervisor.py
      protocol.py
    plugins/
      discovery.py
      trusted_python.py
      wasm.py
      subprocess.py
      provenance.py
  models/
    transports/
      connections/
        aiohttp.py
        httpx.py
        websocket.py
        eventstream.py
        webrtc.py
      registry.py
      base.py
      openai.py
      anthropic.py
      bedrock.py
      gemini.py
      openai_compatible.py
  interfaces/
    inference_api/
      app.py
      auth.py
      openai.py
      anthropic.py
      streaming.py
    inference_admin/
      virtual_keys.py
  composition/
    container.py

ztest/
  inference/
  product/inference/
  interfaces/inference_api/
```

依赖继续严格单向：

```text
contracts <- runtime <- orchestration <- product
```

provider SDK、aiohttp、SQLite/PostgreSQL driver、Redis/vector/object-store client 等实现只允许出现在
Product adapter。Runtime 只依赖 grouped ports 和 canonical contracts；`runtime/inference/cache/` 不得
import Redis/vector client。Shared daemon 是 Product 部署形态，不属于 Agent orchestration；当前版本
不得创建 `orchestration/inference/{placement,leases,cluster_scheduler}.py` 或任何未使用 Cluster 抽象。

---

## 6. Operation execution taxonomy 与核心 Python API

### 6.1 四类互斥执行模型

parity manifest 中每个 provider × atomic operation cell 必须且只能选择一种 execution taxonomy：

| taxonomy | 示例 | logical owner | 专用 port/result |
| --- | --- | --- | --- |
| `unary_finite_attempt` | generate、embedding、OCR、rerank、有限 image/audio | `RuntimeModelGateway` | `InferenceRuntime` / typed unary result |
| `durable_operation` | batch、video lifecycle、container、file mutation、Responses retrieve/delete/cancel | `RuntimeServiceGateway` | `ServiceCommandRuntime` / single-command result |
| `long_lived_session` | Realtime WebSocket/WebRTC、MCP session | `SessionGateway` | `SessionRuntime` / ordered session events |
| `artifact_transfer` | upload、download、multipart、large-payload passthrough | existing `runtime.artifacts` transfer workflow | `ProviderArtifactTransferRuntime` / provider part seam |

四个 logical owner 共享 `GenerationArtifact`、principal/governance、credential binding、
`ProviderQuotaAuthority`、`UsageLedger`、fair admission、bulkhead/connector、receipt storage primitives 和
telemetry，但 journal、状态机、result/event contract 与恢复策略互不冒充。共享的是窄 infrastructure
ports 和同一个 bounded data-plane scheduler，不是一个返回 `CanonicalModelResponse | Any` 的胖接口。

#### 6.1.1 每类执行语义

| 语义 | Unary/finite | Durable operation | Long-lived session | Artifact transfer |
| --- | --- | --- | --- | --- |
| truth source | model-call/attempt journal + attempt receipt | service-operation journal + provider resource receipt | session journal + ordered frame/transport receipt | transfer journal + part/byte-range receipt |
| permit 粒度 | 每个 provider request 一个 `WirePermit` | create/mutate/delete/poll 每个 provider request 独立 permit | handshake/signaling request及每个有副作用的 application message 独立 permit | initiate/finalize及每个 provider HTTP part/range request独立 permit |
| idempotency | attempt ID + provider key（若支持） | operation ID、provider resource ID、command sequence | session ID + outbound application sequence | transfer ID + immutable content digest + part/range sequence |
| retry/fallback | ModelGateway 仅在未 external commit 时以新 attempt/permit 执行 | 创建 resource 前可 fallback；获得 resource ID/accepted receipt 后永久 pin provider，poll 不 fallback | 建连/首个外部 frame 前可换 endpoint；session committed 后不透明 fallback/replay | 首个 provider part或首个外部 byte commit 前可 fallback；之后 pin transfer/provider |
| polling owner | 不适用 | `RuntimeServiceGateway` durable scheduler；数据面每次只执行一次 poll | session owner 处理 heartbeat/reconnect policy | existing artifact transfer owner调度 resume/status；数据面只执行一个 part/range |
| cancel/reconcile | attempt terminal 或 `IN_DOUBT` | provider cancel/query/webhook 对账；无证据为 `IN_DOUBT` | close/cancel best effort；序列缺口对账，禁止盲重放 | abort/multipart list/range verification；未知 part 为 `IN_DOUBT` |
| generation pin | logical call/stream terminal | create intent 到 resource terminal、取消或 reconciliation 关闭 | session open intent 到 close/reconciliation 关闭 | transfer intent 到 digest 验证、abort 或 reconciliation 关闭 |
| usage settle | attempt ID 幂等 | operation + command/receipt ID 幂等，异步 usage 可延迟 reconcile | session + metering epoch/sequence 幂等 | transfer + part/range ID，区分 provider cost 与 storage/egress |

Session 的 TCP/WebSocket control ping、ACK 和 WebRTC media packet不逐包签发 `WirePermit`：它们在已授权
session capability、硬 byte/time limits 和有序 session receipt 内运行，禁止由 transport 自动重放。
任何可能触发 provider 业务副作用的 application message、WebRTC signaling HTTP request 或重新建连
都必须有新 permit。该边界由 protocol manifest 声明和 request-count/frame oracle 验证。

Webhook 只作为已验证、去重的 provider evidence 写入对应 durable/session/transfer receipt；它不拥有
poll、fallback 或 terminal decision。resource 创建后的所有 query/cancel 必须使用原 provider binding，
不能因为 404/timeout 改路由到另一个 provider。

manifest 每个 cell 除 supported 状态外必须包含：`execution_taxonomy`、journal/receipt schema、
`wire_unit`、idempotency/fallback/commit boundary、poll owner、cancel/reconcile、generation pin、usage
settlement、terminal/`IN_DOUBT` oracle。缺任一字段即 Gate 0 失败，不能进入实现估算。

#### 6.1.2 独立 runtime ports

四类不共享 request/result union，只共享 lifecycle event envelope、wire authorization verifier 和调度
基础设施。除下节 unary port 外，另外三个最小 seam 为：

```python
class ServiceCommandRuntime(Protocol):
    async def start_command(self, request: DurableCommandRequest) -> CommandExecution: ...

class CommandExecution(Protocol):
    def __aiter__(self) -> AsyncIterator[CommandLifecycleEvent]: ...
    async def authorize_wire(self, permit: WirePermit) -> None: ...
    async def cancel(self, reason: str) -> None: ...

class SessionRuntime(Protocol):
    async def open(self, request: SessionOpenRequest) -> SessionExecution: ...

class SessionExecution(Protocol):
    def __aiter__(self) -> AsyncIterator[SessionLifecycleEvent]: ...
    async def authorize_open(self, permit: WirePermit) -> None: ...
    async def send(self, message: SessionApplicationMessage, permit: WirePermit) -> None: ...
    async def close(self, reason: str) -> None: ...

class ProviderArtifactTransferRuntime(Protocol):
    async def execute_part(self, request: TransferPartRequest) -> TransferPartExecution: ...

class TransferPartExecution(Protocol):
    def __aiter__(self) -> AsyncIterator[TransferLifecycleEvent]: ...
    async def authorize_wire(self, permit: WirePermit) -> None: ...
    async def cancel(self, reason: str) -> None: ...
```

所有 iterator 仍是各 execution 的唯一 terminal/result 通道。`RuntimeServiceGateway` 组合多个 command
（例如 create → poll → cancel），`SessionGateway` 拥有 application sequence，现有 artifact transfer
workflow 组合 part/range；runtime 只执行一个已绑定 generation、credential 和 permit 的 wire unit，不自行 polling、
fallback、拆 part 或重连。具体 typed result 放在各自 terminal event，绝不退化成 `Any`。

`SessionGateway` 表示 provider transport session 的窄 owner，不复用 Mote 用户 session 的业务状态模型；
实现必须复用现有 session tagged event envelope、sequence validation、projection、artifact roots、journal
integrity/versioning 和 lifecycle registration。只允许新增 provider-session event variants 与 projection，
不得另造 session persistence engine。

`ServiceCommandRuntime` port 固定放在 `contracts/ports/service/command_runtime.py`；
`ProviderArtifactTransferRuntime` 固定放在 `contracts/ports/artifact/provider_transfer.py`。后者只能表达
provider HTTP part/range 这一 wire seam，不能拥有 publication、revision transfer、resume、GC 或 receipt。
Gate 0 必须逐项对比现有 `ArtifactRevisionTransfer`/publication ports；若扩展现有 artifact port 已足够，
则不创建新 port。没有 infrastructure-reuse approval 时，默认选择扩展现有 port。

### 6.2 Unary InferenceRuntime port

```python
class InferenceRuntime(Protocol):
    async def start_attempt(
        self,
        request: InferenceAttemptRequest,
    ) -> AttemptExecution: ...

    async def drain(self, *, timeout_seconds: float) -> None: ...
    async def aclose(self) -> None: ...

class AttemptExecution(Protocol):
    def __aiter__(self) -> AsyncIterator[AttemptLifecycleEvent]: ...
    async def authorize_wire(self, permit: WirePermit) -> None: ...
    async def cancel(self, reason: str) -> None: ...
```

该 iterator 是每 attempt 唯一消费接口、单生产者、严格递增 sequence 的有序事实流，包含 queued、
budget-reserved、dispatched、wire-prepared、send-committed、wire-started、response-started 与恰好一个
terminal。成功 terminal 直接携带该 unary operation 的 typed result（model generation 才是
`CanonicalModelResponse`），失败 terminal 携带 normalized failure；不存在
独立 `result()`、terminal Future 或第二条结果通道。`RuntimeModelGateway` 持续消费并先写 journal，再
把结果交给 logical call；EventBus 只是 journal 后的观察面，不能作为恢复真相。

取消、deadline、远程断线和 receipt reconciliation 也通过同一有序流收敛。调用方必须持续 drain
直到 terminal，不能先等待另一个结果对象。这样有界 event buffer 不会因无人消费而死锁，也不会
由 events/result 分别宣告互相矛盾的 terminal。远程重连以 receipt revision/event cursor 续读，
重复事件按 sequence 去重。

queue 选中且 budget reserve 完成后，iterator 发出非 terminal `WIRE_AUTHORIZATION_REQUIRED`；
`RuntimeModelGateway` 原子写 `WIRE_AUTHORIZED` 后调用 `authorize_wire(permit)`。该动作按 permit digest
幂等，只解除这一个 execution 的 dispatch barrier，不产生结果事实；非法/冲突 permit 作为后续有序
terminal failure 返回，不能另开异常结果通道。授权请求或响应丢失时，caller 从 journal 重投同一
permit，remote 从 receipt/permit digest 返回相同状态。

`RuntimeModelGateway` 的 attempt closure 调用该 port；它不会把整个 logical call 交给 runtime，
因此数据面无法自行 fallback。使用 execution handle 而不是直接返回 response 的原因是严格
`WIRE_STARTED`、远程取消和 crash 对账都需要一等生命周期协议。

### 6.3 InferenceAttemptRequest

```python
class InferenceAttemptRequest(FrozenModel):
    schema_version: Literal[1] = 1
    model_call_id: str
    attempt_id: str
    generation_id: str
    generation_artifact_digest: str
    endpoint: EndpointDescriptor
    credential_slot_id: str
    credential_version: str
    invocation: ModelInvocation
    deadline_utc: datetime
    remaining_seconds_at_send: float
    sent_at_utc: datetime
    stream: bool
    artifact: ResolvedArtifact | None
    principal: InferencePrincipal
    scheduling: TrustedSchedulingClass
```

它只表示一次 attempt。没有 `fallbacks`、`max_retries` 或 `alternate_models` 字段。
`principal` 与 `scheduling` 由认证/内部 Application policy 产生，外部请求字段不能直接构造。

### 6.4 ProviderTransport

避免 Bifrost 的胖 Provider interface，按 operation 细分：

```python
class GenerateTransport(Protocol):
    async def generate_once(
        self,
        request: GenerateWireRequest,
        *,
        local_deadline: float,
        lifecycle: WireLifecycleSink,
        stream: StreamSink | None,
    ) -> ProviderResponse: ...

    async def aclose(self) -> None: ...
```

Embedding、image、speech、durable command、session handshake/message、artifact part/range 分别拥有
窄 Protocol。transport registry 以
`(wire_protocol, operation, schema_version)` 注册，不能要求每个 provider 实现所有操作。

### 6.5 ResolvedEndpointBinding seam

正式迁移删除 `ModelEndpointAdapter` 与 `execute_once()`。Product 只实现无网络行为的 binding resolver：

```python
class ModelEndpointResolver(Protocol):
    def resolve(
        self,
        endpoint: EndpointDescriptor,
        credential_slot_id: str,
    ) -> ResolvedEndpointBinding | None: ...

class ResolvedEndpointBinding(FrozenModel):
    endpoint: EndpointDescriptor
    credential_slot_id: str
    credential_version: str
    tenant_fingerprint: str
    classification_policy_id: str
    transport_identity: str
    capability_identity: str
```

binding 不含 client、secret、principal、scheduling、deadline、attempt/generation ID 或 mutable runtime
状态。`RuntimeModelGateway` 作为 attempt owner 合并 binding 与其显式拥有的 journal、principal、
generation、deadline 和 scheduling capability，构造完整 `InferenceAttemptRequest`，直接调用
`InferenceRuntime.start_attempt()`。failure classification 由 generation-pinned provider error policy/
transport manifest 完成，不再依附一个可执行 endpoint object。

这样 resolver 只解析 Product binding，RuntimeModelGateway 才能合法创建 attempt；禁止用 contextvar、
读取 Role/RoleState 或 service locator 偷渡缺失字段。旧 adapter 不是兼容层，Release Gate 前直接删除。

### 6.6 ResolvedServiceEndpointBinding seam

durable operation 不新增 logical gateway/state machine。现有 `RuntimeServiceGateway` 继续唯一拥有
`ServiceInvocation`、`ServicePlan`、`ServiceReceipt`、`ServiceCallJournal`、poll/reconcile/cancel、bounded
failover 与 `ServiceExecutionSemantics`。Product 将现有可执行 `ServiceEndpointAdapter` 收敛为无网络的
`ResolvedServiceEndpointBinding`；`RuntimeServiceGateway` 基于现有 plan/journal 构造一个
`DurableCommandRequest`，调用 `ServiceCommandRuntime` 执行恰好一个 provider command。

禁止新增第二种 service accepted record、service receipt、polling scheduler、journal 或 `IN_DOUBT`。
command lifecycle event 只是现有 `ServiceCallJournal` 的新 tagged record/evidence；provider resource ID、
cancel/reconcile 和 terminal 仍写现有 `ServiceReceipt`。该迁移与 model binding seam 对称，但不把 model
attempt 类型强塞给 service operation。

---

## 7. Application 级装配与生命周期

### 7.1 唯一所有者

`GatewayDataPlane` 是 Runtime 内部唯一组合根，不是跨层 port，也不暴露给 Kernel/Product 业务代码：

```python
class GatewayDataPlane:
    scheduler: FairScheduler
    bulkheads: BulkheadController
    connection_pool: TransportConnectionPool
    receipts: ReceiptServices
    governance: GovernanceServices
    task_registry: BoundedTaskRegistry
    artifact_store: ArtifactStore
    artifact_publisher: ReliableArtifactPublisher
    artifact_resolver: ArtifactResolver
```

`ProductContainer.standard()`/daemon composition 只创建一个 data plane，再从它构造
`InferenceRuntime`、`ServiceCommandRuntime`、`SessionRuntime` 和 `ProviderArtifactTransferRuntime` 四个窄
facade，注入各 logical gateway。facade 只能借用同一 scheduler、bulkhead、connection-pool port、receipt、quota/
ledger/health 和 artifact authority，禁止各自 new capacity semaphore、connector pool、quota tracker、
receipt store 或 background supervisor。

Embedded 每个 Application 一个实例；Shared daemon 每 daemon 一个；Cluster 每 worker process 一个，
其中 durable authority 指向 Product 注入的同一 SQLite/PostgreSQL 与现有 ArtifactStore adapter，进程本地 scheduler/connector 仍只有一
份。`GatewayDataPlane` 提供 start/drain/aclose 行为，但不拥有生命周期框架；四个 facade 不拥有共享资源的关闭权。architecture test
扫描 composition 和 constructors，发现第二个 authority/pool 即失败。

### 7.2 复用 LifecycleStack

`GatewayDataPlane.lifecycle_resources() -> tuple[LifecycleResource, ...]` 只汇总真实 owner 提供的多个
resource，并注册到现有 `EngineServices`/`LifecycleStack`。一个 `LifecycleResource` 只属于一个 phase，
禁止用一个巨型 close callback跨 phase。复用现有 phase ordering、失败重试、cancellation shielding 与
聚合错误；禁止创建 `runtime/inference/lifecycle.py`、shutdown coordinator 或私有 signal handler。

阶段映射固定为：

| Lifecycle phase | Gateway action |
| --- | --- |
| `STOP_PRODUCERS` | scheduler 的 `gateway-admission`：readiness false，关闭 admission/dispatcher |
| `CLOSE_RESOURCES` | execution registry 的 `gateway-executions`：按 deadline drain/cancel；Product connection pool另注册自身 close resource |
| `FLUSH_EXPORTERS` | telemetry/audit subscriber 的 `gateway-exporters`：drain EventBus 有界 spool |
| `FLUSH_DURABILITY` | receipt/outbox 与现有 artifact publisher 的 `gateway-durability` resources |
| `RELEASE_CONTAINER` | Product composition 的 `gateway-generation-handles`：释放四类 generation view与最终 Context |

每个真实 owner 的 resource 必须幂等且 cancellation-safe；LifecycleStack 决定 retry/继续/聚合，data
plane 只返回 tuple，不重新包装 owner 行为或复制策略。

### 7.3 启停顺序

启动：

1. validate config；
2. 构建 transport registry；
3. 构建 generation 与 credential bindings；
4. 创建共享 connectors、auth clients/signers 与 protocol transports；官方 SDK 不参与 production wire；
5. 启动 dispatcher 与有界 execution task registry；
6. 接受请求。

关闭：

1. 停止 admission；
2. 拒绝新请求；
3. 等待队列和 active attempts 到 deadline；
4. 未开始请求返回 admission failure，不计 wire attempt；
5. 取消仍执行的 model/service/session/transfer execution，由各现有 logical owner journal 记录；
6. 关闭 dispatcher 与 execution tasks；
7. 关闭 auth clients/signers/connectors。

禁止依赖 `__del__` 或 event loop 关闭时隐式清理。

### 7.4 热更新

reload 只按第 3.5/3.5.1 节一次构建并发布 `GatewayRuntimeGeneration`。四类 queued/active execution 均归属其
进入时的 generation view；旧 generation 只有四类 view、connection/client/credential handle 与
reconciliation 引用全部归零后才能排空。不存在第二个 reload owner。

---

## 8. 高并发核心实现

### 8.1 资源身份拆分

禁止用一个 `resource_key` 同时代表连接、quota、bulkhead 和 credential。定义四种 frozen identity：

```text
ConnectionIdentity(authority, proxy, tls_policy, http_version, network_zone)
QuotaIdentity(provider_account, project, region, deployment, model_quota_domain)
BulkheadIdentity(provider, operation, endpoint_failure_domain)
CredentialIdentity(slot_id, version, tenant_scope)
```

- connector pool 按 `ConnectionIdentity` 引用计数共享；
- request/token reservation 与 cooldown 按 `QuotaIdentity`；
- `InFlightCapacityPermit`、breaker 与隔离队列按 `BulkheadIdentity`；
- auth/signing/client state 按 `CredentialIdentity`；
- attempt 同时记录四种 identity 的非敏感 fingerprint。

拆分原因是同一 authority 可能有多个 quota account，同一 quota account 可能跨多个 model endpoint，
同一 provider operation 需要共同熔断，而 credential rotation 又不能重建所有 connector。混成一个
key 会导致过度隔离、错误共享或凭证泄漏。

### 8.2 为什么不用一个 asyncio.Queue

Bifrost 每 provider 一个 FIFO channel 能隔离 provider，但不能防止单个 tenant 填满整个 queue。
Mote 使用两级结构：

```text
GatewayDataPlane
  ├─ HierarchicalFairQueue
  ├─ Dispatcher
  ├─ BulkheadState[BulkheadIdentity]
  ├─ QuotaState[QuotaIdentity]
  └─ TransportConnectionPool port[ConnectionIdentity]
```

不同 bulkhead 故障互不阻塞，但 dispatcher 可以在全局、workload 和 tenant 边界执行公平调度。

### 8.3 FairAdmissionQueue

纯 Python、单 event loop 状态机，不为每个请求创建额外调度 task：

- 层级固定为 workload class → tenant/project → quota/bulkhead flow → flow 内 FIFO；
- active flow 使用 Deficit Round Robin；
- cost 由服务端 tokenizer/estimator 计算：
  `clamp(input_tokens + reserved_output_tokens, minimum, maximum)`；
- 调用方不能声明 cost、高优先级或 workload class，身份与策略从受信 principal 推导；
- interactive/control/background/evaluation 使用独立 class share，control 保留最小容量；
- 空闲 capacity 可借用，保证 work-conserving；
- aging 逐步降低长期等待请求的有效 cost/提高调度资格，避免大请求永久饥饿；
- deadline 使用最小堆索引，出队前剔除过期请求；
- cancel 通过 request node 状态和 Future 完成，不线性扫描所有队列。

复杂度目标：enqueue/dequeue/cancel 摊销 O(1)，deadline expiry O(log n)。

### 8.4 有界性

配置必须同时提供：

- `global_max_queued`；
- `resource_max_queued`；
- `tenant_max_queued`；
- `resource_max_in_flight`；
- `tenant_max_in_flight`；
- `max_queue_wait_seconds`；
- `overload_policy = wait | reject`。

所有限制有非零安全默认值。任何路径都不能创建 unbounded queue。

### 8.5 Dispatcher 与唯一 InFlightCapacityPermit

固定 worker tasks 与同义 semaphore 不并存。采用一个逻辑 dispatcher（可实现为一个调度 task）
加有界 request tasks：

- dispatcher 从公平队列选择 request；
- 对 `BulkheadIdentity` 申请唯一 `InFlightCapacityPermit`；它只表示本地容量，不具有 wire 授权能力；
- permit 成功才创建受 task registry 硬上限约束的 execution task；
- connector 自己限制 socket，不承担业务 in-flight 语义；
- task 完成后恰好一次归还 permit；
- CPU conversion 提交给独立有界 executor admission，不直接使用默认无限 work queue。

这样只有一份 wire concurrency 计数。固定 worker + semaphore 会产生两个相同容量旋钮，配置不一致
时形成空闲、死锁或难以解释的吞吐上限。

```python
async def _dispatch(runtime: InferenceGatewayRuntime) -> None:
    while request := await runtime.queue.take():
        if request.expired:
            request.reject(DEADLINE)
            continue
        capacity_permit = runtime.bulkheads.acquire(request.bulkhead_identity)
        if capacity_permit.rejected:
            request.reject(capacity_permit.verdict)
            continue
        runtime.tasks.start_bounded(_execute(request, capacity_permit))
```

实际实现必须在 `finally` 中做到 Future completion、in-flight decrement 和 capacity-permit settlement
恰好一次。execution task 不捕获异常后 retry。

### 8.6 Python 性能策略

- 使用 Mote 自有可观测 HTTP/SSE/WebSocket/EventStream transport；官方 provider SDK 不进入正式
  wire path；
- 为每 base URL 配置 connection limits、keepalive、HTTP/2；
- 避免 request/response 深复制；canonical contracts frozen；
- 大媒体用 `ArtifactRef`，不在多个队列中复制 bytes；
- stream 使用有界 `asyncio.Queue` 或直接 sink，明确 backpressure；
- telemetry 异步批量，但安全/预算状态同步结算；
- 不预先实现对象池。CPython 小对象池、GC 与引用生命周期不同于 Go，只有 profile 证明后才加；
- CPU 密集 JSON/schema/media conversion 进入受控 executor，不能阻塞主 event loop；
- uvloop 作为部署优化交付并验收，核心语义不依赖它。

---

## 9. 连接池与 Provider transport

### 9.1 TransportConnectionPool port 与 Product 实现

Contracts 只定义窄 port，Runtime 通过注入使用：

```python
class TransportConnectionPool(Protocol):
    async def acquire(self, identity: ConnectionIdentity) -> ConnectionLease: ...
    async def drain(self, identity: ConnectionIdentity) -> None: ...
    async def aclose(self) -> None: ...
```

`ConnectionLease` 只暴露 operation transport 所需的窄 send/stream/session handle、identity fingerprint、
release/drain，不暴露 `aiohttp.ClientSession`、httpx client、TLS context、proxy、DNS resolver 或 socket。
Runtime 只决定何时 acquire/release/drain，不能构造、类型判断或配置具体 client。

Product 在 `product/models/transports/connections/{aiohttp,httpx,websocket,eventstream,webrtc}.py` 实现
pool，按稳定 `ConnectionIdentity` 引用计数共享。generation 持有 connection lease；只有 generation
refs、active requests 和 queued reservations 全部归零才关闭。Auth client/signer 按
`CredentialIdentity` 隔离但可引用相同底层 connector。所有 TLS/proxy/DNS/HTTP2/socket limits 与可选
SDK auth object 都封装在 Product；SDK auth object不得获得 retry或直接执行 provider request。

### 9.2 自有 transport 与 retry 禁令

正式实现不依赖 SDK 执行 wire request，而由 Mote transport 实现 OpenAI/Anthropic/Google/AWS 等
协议。这使 `WIRE_STARTED`、stream backpressure、连接生命周期和实际 request count 可验证。

retry 禁令覆盖 SDK、HTTP client、proxy、service mesh 和 transport。基础连接层只允许在
`WIRE_STARTED` 之前更换未使用连接；之后任何重发必须返回 RuntimeModelGateway 产生新 attempt。
conformance test 用故障 server 与 packet/proxy/frame counter 验证每个 manifest `wire_unit` 实际
provider request/frame 为 0 或 1。

选择自有 transport 的原因不是排斥 SDK，而是十年期系统不能把最关键的 retry 与 wire commit
边界委托给不可观测、可能升级改变默认行为的黑盒。SDK 保留为 schema/行为 oracle 和兼容测试。

### 9.3 Provider adapter 与 wire family

所有冻结基线 provider 在一次发布中完成。结构按原生厂商 adapter 组合共享 wire family：

| Wire family | Provider adapter |
| --- | --- |
| OpenAI native | openai、azure |
| OpenAI-compatible | cerebras、deepseek、fireworks、groq、mistral、ollama、opencode、openrouter、parasail、perplexity、sgl、vllm、xai，以及基线中复用该 wire 的 provider |
| Anthropic native | anthropic |
| AWS SigV4/EventStream | bedrock、bedrockmantle |
| Google GenAI/Vertex | gemini、vertex |
| Cohere native | cohere |
| Media/specialized HTTP | elevenlabs、huggingface、nebius、replicate、runware、runway、sarvam、wafer |

每个品牌 adapter 独立声明 endpoint、auth、capability、参数、错误、quota、model catalog 和 pricing；
共享 family 只复用编码、SSE/EventStream parser 与 transport。即使两个厂商都说 OpenAI-compatible，
也不能假定 unsupported params、usage、tool calls 或错误码完全一致。

Provider adapter 通过显式 registry + capability manifest 注册，不修改 runtime 中心 dispatch。冻结
Bifrost 基线规定首版最低全集；任意新模型商只需新增 Product adapter、catalog/pricing/capability
和 conformance fixture，不新增 Runtime 分支。这样“覆盖所有模型商”体现为完整基线与开放扩展面，
而不是再次把未知厂商强制伪装成 OpenAI。

每个 provider 必须交付：generate、stream、tools、structured output、usage、quota、error mapping、
cancel、client lifecycle 和 conformance tests；并对 Bifrost 基线的全部其他 operations 逐项实现或
精确复现其 unsupported contract。功能不支持时通过 capability 声明，禁止静默降级。

### 9.4 协议级 irreversible contract

`SEND_COMMITTED` 是所有协议共有的 durable “此 attempt 不再发送”边界；`WIRE_STARTED` 则是协议特定
的可观测事实，不能统一简化为“把 header/body 交给 socket”。parity manifest 对每个 wire protocol
必须机器可读声明并由故障注入验证：

| 字段 | 要求 |
| --- | --- |
| `irreversible_boundary` | HTTP/1.1、HTTP/2 stream、SSE、WebSocket、WebRTC、SigV4/EventStream、multipart/large-payload 各自何时可能产生 provider 副作用 |
| `request_count_oracle` | 测试代理、provider request ID、frame/stream counter 或签名 request counter 的权威证据 |
| `cancellation_boundary` | 哪个阶段可证明未发送，哪个阶段只能 best-effort cancel/reconcile |
| `idempotency_key` | provider 是否支持、作用域、TTL、冲突和查询语义；不得据此启用 transport retry |
| `partial_body_recovery` | 部分 upload/frame/body 后属于 safe failure、cancel、provider-managed 还是 `IN_DOUBT` |
| `terminal_receipt_source` | response、provider lifecycle API、webhook、request-status API 或只能本地观察 |

例如 HTTP/2 的连接写入不等于特定 stream 已被 peer 接收，WebSocket handshake 不等于业务 frame，
WebRTC session 建立也不等于 media side effect；具体 adapter 必须给出边界和 oracle。共享状态机只消费
规范化 lifecycle event，不伪造跨协议一致的网络事实。

---

## 10. 流式处理

### 10.1 内部 provisional contract

provider transport 把 delta 发到 `AttemptStreamSink`：

```python
class AttemptStreamSink(Protocol):
    async def emit(self, delta: ModelStreamDelta) -> None: ...
    async def finish(self, response: CanonicalModelResponse) -> None: ...
```

sink 最终仍进入现有 `capture_attempt_stream()`。失败 attempt discard，成功 attempt commit。
Bifrost 的 HTTP stream chunk hook 迁成 commit barrier 之前的 typed observer，不能直接写用户 socket。

内部事件严格携带 `attempt_id` 与 sequence：

```text
provisional_delta(attempt_id, sequence)
→ stream_committed(attempt_id)
或
→ stream_discarded(attempt_id, reason)
```

内部 consumer 必须声明 rollback capability；不支持者只能等待 commit 后回放。

### 10.2 外部 SSE/WebSocket commit contract

实时外部流不能同时保证首 token 低延迟和失败流完全不可见，正式语义选择：

1. 首个有效 chunk 前允许当前 attempt 失败并由 RuntimeModelGateway fallback；
2. compatibility handler 第一次向外部 socket 写有效 chunk 时原子提交该 attempt；
3. commit 后禁止透明 fallback；
4. 后续错误作为已提交 stream error/terminal frame 返回；
5. disconnect 触发 cooperative cancel，但不能假装 provider 未收到请求。

正式配置名为 `commit_on_first_chunk`。另提供 `buffered_commit` 模式：完整结果成功后再按协议回放。
后者必须明确标注为 buffered replay，
不得宣传为实时流式。作此取舍的原因是已发送到外部客户端的 token 无法撤回，任何“继续 fallback
并拼接”都会把两个模型的输出伪装成一个响应。

### 10.3 Backpressure

- 每 attempt 有 `max_buffered_chunks` 与 `max_buffered_bytes`；
- 支持 rollback 的内部 UI 可实时消费 provisional event；
- 外部实时 consumer 遵守首 chunk commit 语义；`buffered_commit` 使用有界 spool 后回放；
- consumer stall 超时后取消 attempt；
- 超大流落 artifact spool，不能无限占内存。

### 10.4 First-chunk gate

迁移 Bifrost 的 first-chunk gate：transport 在把 stream 标记为 established 前读取首个有效 chunk，
将建立阶段错误作为普通 attempt failure。它不能替代内部 commit barrier 或外部首 chunk commit。

### 10.5 Provider response protocol validation

HTTP 2xx、收到字节或能解析单个chunk都不自动构成成功。每个translation profile定义provider response
protocol validator，在外部commit前验证content-type/envelope、必需结构、error-in-200、HTML/challenge、
tool/reasoning字段与stream lifecycle；stream还要区分keepalive、合法空结果、结构化活动、terminal event、
截断和只有垃圾字节。未知event按profile的`reject|ignore|preserve_namespaced`规则处理。

validator是Product provider protocol adapter的一部分，只向Runtime返回typed event或canonical failure；Runtime
仍是commit/fallback决策owner。validator只判断协议完整性，不评价回答内容好坏，不以“空文本”否定合法tool call、usage-only或显式空结果。
非流式可在既有有界buffer内验证；流式使用第10.4节有界first-chunk gate和增量state machine，禁止clone/tee
完整无限stream或为了校验无限延迟commit。commit前invalid映射canonical provider failure，由logical owner决定
是否fallback；commit后发现截断只能向客户端报告terminal stream error并reconcile，不能透明重试。

每个operation/profile冻结validator automaton、accepted content types/envelopes、初始/terminal states、event
transition与unknown-event规则，并配置`max_validation_bytes`、`max_validation_frames`、`max_frame_bytes`、
`validation_timeout`和`max_precommit_delay`。达到任一上限仍不能判定时返回结构化
`PROVIDER_RESPONSE_VALIDATION_LIMIT`，不得fail open；buffer/spool继续服从第10.3节全局上限。

validator verdict附带evidence digest、bytes/frames observed、state和commit boundary。协议invalid/limit只产生
canonical failure evidence：未commit且无可计费usage证据时释放reservation；已`WIRE_STARTED`仍按provider
request结算/reconcile。只有明确availability failure影响breaker，schema/protocol drift进入provider capability
告警但不自动quarantine credential；认证、quota和credential verdict仍完全来自第3.7节canonical failure，
validator不得自行推断或回写health authority。

Gate 0 fixtures覆盖HTML 200、JSON error 200、malformed envelope、keepalive-only、合法empty/tool-only、未知
event、缺terminal、partial frame、disconnect和超验证窗口，并验证任何fallback都使用新的journaled attempt与
`WirePermit`。

## 11. Typed Plugin Pipeline

Bifrost 插件能力迁入，但不允许任意插件拿到整个 mutable request/context。

### 11.1 阶段

```text
admission observers
request policies
request transforms
transport observers
stream observers
response observers
audit subscribers
```

### 11.2 Protocol

```python
class RequestPolicy(Protocol):
    async def evaluate(self, view: RequestPolicyView) -> PolicyDecision: ...

class RequestTransformer(Protocol):
    async def transform(self, request: CanonicalRequest) -> CanonicalRequest: ...

class AttemptObserver(Protocol):
    async def started(self, event: AttemptStarted) -> None: ...
    async def finished(self, event: AttemptFinished) -> None: ...
```

- policy 可 allow/deny，不原地修改；
- transformer 输入输出 immutable contract；
- observer 只读，异常进入 telemetry，不能改变执行结果；
- 安全 policy fail closed；observer fail open；
- 执行顺序由显式 priority + registration index 决定；
- 同一发布交付 trusted Python entry point、隔离子进程与 WASM capability adapter；不承诺加载 Go `.so`
  二进制，但冻结基线全部插件行为必须有 Python/WASM/隔离进程等价实现。

RoutingService、failover policy、permission policy 不作为通用插件重新实现。

### 11.3 信任与隔离边界

进程内 Python entry point 明确定义为 **trusted code**：它拥有 gateway 进程的操作系统权限，typed
Protocol 与 immutable view 只能减少误用，不能阻止恶意代码 import 标准库、读取内存或访问网络，
因此绝不宣传为 sandbox。安装或启用它需要 trusted-plugin 管理权限、签名/provenance 校验与审计。

不可信插件只能选择：

- WASM runtime：默认无网络、文件系统、时钟、随机数和 secret，按 capability 显式导入；
- 隔离子进程：通过版本化 typed RPC 获取窄 capability，使用独立 OS identity、filesystem/network
  sandbox、CPU/内存/进程数限制、调用 deadline、输出上限和强制 kill。

secret 不进入插件配置或环境，只能按一次调用、tenant/slot/用途绑定的 capability 请求短暂返回，
并禁止持久化。插件 supervisor 与 inference worker 隔离；插件 crash、OOM、超时或畸形 RPC 只能使
对应 policy 按声明 fail-open/fail-closed，不能终止 dispatcher 或破坏主数据面。管理 API/UI 必须显示
`trusted_in_process`、`isolated_process` 或 `wasm_sandboxed`、实际 capabilities、签名、资源限额和最近
故障。原因是 Python 对象封装不是安全边界，十年设计必须诚实表达信任模型。

### 11.4 Plugin 分层边界

`contracts/inference/plugins.py` 只定义 capability、policy decision、transform/observer event；
`runtime/inference/plugins.py` 只实现 immutable typed pipeline、调用顺序、deadline/timeout、结果归一化和
fail-open/fail-closed。Runtime 接收 Product 已构造并收窄的 plugin capability，不得 import entry-point、
WASM runtime、subprocess/OS sandbox、filesystem/network capability 或依赖安装器。

`product/inference/plugins/{discovery,trusted_python,wasm,subprocess,provenance}.py` 分别拥有 entry-point
discovery、签名/provenance、WASM engine、隔离进程/OS policy、plugin config 与依赖装配。architecture
test 禁止这些 Product implementation依赖泄漏进 Runtime；插件异常只能通过 Contracts event/failure
返回 pipeline。

---

## 12. 治理功能

### 12.1 Principal 与 Virtual Key

HTTP gateway 的 virtual key 解析为：

```text
InferencePrincipal(
  tenant_id,
  project_id,
  allowed_routes,
  allowed_models,
  budget_ids,
  rate_limit_ids,
  data_policy,
)
```

创建、一次明文展示、Argon2id/HMAC verifier、rotation/revoke、store 与管理 API 全部位于 Product
`product/inference/identity/` 和 admin surface。它通过 `contracts/ports/inference/identity.py` 只向 Runtime
返回 `InferencePrincipal`；Runtime 不知道 HTTP key 格式、hash 参数或管理操作。内部 Role 不使用
virtual key，由 Application identity 直接产生 principal。

### 12.2 UsageLedger

预算不是请求后日志分析，而是 reserve/settle：

1. exact/semantic response-cache lookup 前不预留 provider generation cost；cache miss 进入 queue 时只做
   feasibility check，queue 等待不长期占用完整预算；
2. dispatch 前按 token 上限与价格原子 reserve，reservation 带 attempt ID、expiry 和 fencing token；
3. wire 前取消或 reservation 到期安全释放；`SEND_COMMITTED` 后只能 settle/reconcile；
4. 完成后按实际 usage/cost、以 attempt ID 幂等结算；失败但已计费的 attempt 仍结算已知 usage；
5. usage 不明时记录 pending reconciliation；hard budget 超限拒绝，soft budget 只告警/降级路由；
6. semantic lookup 所需 embedding 是独立 attempt、独立预算预留，不能借用原 generation reservation。

Embedded 与单 daemon Shared Process 默认使用 SQLite WAL；Shared 也支持 PostgreSQL，未来 Cluster
只允许 PostgreSQL。SQLite/PostgreSQL backend 均在当前发布交付，通过同一 `UsageLedger` port 和
conformance suite；Runtime 不 import
数据库驱动。

### 12.3 Provider quota、credential health 与 bulkhead

`UsageLedger` 只拥有 tenant/project budget，不成为治理巨型权威。三个独立 port/state machine 分别为：

- `ProviderQuotaAuthority`：本地 request/token token-bucket、provider quota headers 与 retry-after；key
  至少包含 tenant、provider account 和 model quota domain；
- `CredentialHealthAuthority`：credential failure 分类、quarantine、probe 与恢复；
- `BulkheadController`：按故障域维护 in-flight availability 与 breaker。

它们可复用 PostgreSQL transaction/outbox 基础设施，但不能共享领域接口、状态枚举或隐式互相改写。
429 可更新 provider quota/cooldown；只有 availability-class failure 更新 breaker；认证失败只进入
credential health。该拆分避免把预算、供应商流控、凭证健康和故障隔离重新聚成第二个 admission。

### 12.4 公平性

virtual key、内部 agent、background task 全部进入同一个 FairAdmissionQueue，不能让 HTTP 入口和
内部调用各自拥有独立并发池而互相超卖。

### 12.5 复用现有模型路由控制面

OmniRoute 的 auto-combo/LKGP/scoring 只作为需求证据，不迁入第二套路由器。扩展现有
`contracts/ports/model/routing.py`、Squilla、`RoutingService` 与 Product routing composition，提供 typed
目标（quality、cost、latency、reliability、reasoning capability）、capability/category与context-window
硬过滤、tenant/key候选排除、region/data-policy、权重和 reasoning effort/budget约束。

路由计算是无副作用的 candidate decision：不得发 provider请求、retry/fallback、轮换 credential、修改
breaker/quarantine/quota或直接 settle budget。最终 failover仍由 `RuntimeModelGateway`/`FailoverPlanner`
拥有。空候选的 `deny|use_explicit_default` 行为必须由冻结 policy声明，默认 fail closed；禁止退回全 provider
池而突破权限、地域、能力或预算边界。

每次 decision生成 typed trace：输入 policy/generation、初始候选、每项过滤理由、采用的 telemetry freshness、
归一化评分输入/权重、tie-break与最终选择。trace经过脱敏后进入 audit artifact；未知、NaN、过期或缺失评分
输入必须按 policy确定性处理。Product提供不签发 `WirePermit`、不改变健康状态的 dry-run simulator和
decision-matrix fixtures。session/context/cache affinity与 last-known-good只能是有界 routing hint，必须绑定
tenant、generation和候选集合，不能变成隐式 sticky failover或跨会话状态。

每个动态评分输入使用统一 `RoutingObservation` envelope：`metric/value/unit/source/observed_at/expires_at/
sample_window/confidence/revision`。cost/catalog来自签名revision，quota/health只能读取对应authority的只读
projection，latency/error来自明确窗口的telemetry；路由器不得自行探测或回写authority。policy逐指标声明
`freshness_limit`与`missing|stale|invalid`行为（exclude、conservative default或deny），禁止把0、NaN、未来
时间戳或无限旧值当正常样本。decision trace必须记录实际采用的observation digest和降级原因。

一次路由决策必须先封存不可变输入视图，而不是在评分过程中逐项读取变化中的projection：

```text
RoutingObservationSnapshot(
  snapshot_id,
  generation_id,
  policy_revision,
  captured_at,
  observation_digests,
  authority_revisions,
  digest,
)
```

各authority通过只读revision/cursor提供可验证投影；snapshot的一致性承诺是“同一决策使用固定revision
集合”，不是跨authority同一时刻的分布式事务。builder要么得到满足policy freshness的完整集合，要么按冻结
missing/stale策略显式排除或拒绝，禁止混合后伪称同一时点强一致。`RoutingDecision`、初始
`FailoverPlan`和journal fact绑定同一snapshot digest。执行中需要重新路由时必须捕获新snapshot、生成新
decision/plan revision并追加journal记录；不得在旧decision内静默替换telemetry、candidate或权重。

generation激活前执行离线counterfactual routing evaluation：只重放脱敏、版本化的历史decision inputs，
比较旧/新generation的候选排除、provider/model分布、预计成本/延迟、reasoning/tool/context能力、地域与
policy violation；不得读取原始prompt、调用provider、签发permit或更新LKGP/health/quota。冻结policy为每项
变化设阻断阈值，报告digest进入generation activation evidence；这是shadow validation，不是shadow traffic。

counterfactual dataset只允许白名单字段（request capability/category、token/size buckets、tenant-scoped
policy labels、原snapshot digests与脱敏outcome），禁止prompt、reasoning、tool arguments、secret或可逆用户
内容。dataset使用现有typed Artifact、tenant/project ownership、encryption、region、TTL、deletion与legal
hold；不同tenant默认分别计算，不允许为凑样本合并。evaluation contract冻结cohort定义、最小样本量、时间窗、
缺失率与置信区间，以及cost/latency/provider concentration/capability loss/policy violation阈值。样本不足、
cohort漂移或置信区间越界不能自动通过；override必须双人审批、理由/evidence、有限generation/expiry、不可变
审计与显式风险状态，过期后重新评估，不能成为永久豁免。

---

## 13. 缓存体系

缓存位于 logical-call planning 与数据面 admission 之间，由 RuntimeModelGateway 编排的
`InferenceCache` port 提供；不能隐藏在 transport worker 中。原因是 cache lookup 不是 provider
attempt，cache backend failure 也不应触发 endpoint fallback。

迁移 Bifrost semantic cache，但默认只对显式允许的调用启用。

### 13.1 四类缓存

- exact cache：canonical request digest；
- semantic cache：embedding + vector backend；
- provider prompt cache：只表达 provider 原生 cache intent/observation，不缓存最终 response；
- HTTP response cache：仅用于可安全缓存的管理/catalog GET，不缓存推理 POST。

四类缓存全部在首版实现并明确 owner，不能用一个“cache”开关混淆不同一致性和安全语义。

### 13.2 Cache key 与生命周期

包括：

- canonical operation/input digest；
- system prompt、tools、output schema；
- route/model capability identity；
- tenant/data classification；
- relevant generation/policy version；
- cache namespace。

不包括 credential slot。不同 tenant 默认绝不共享。tools、实时数据、非确定性高或敏感请求默认
bypass。

cache lookup 不消耗 wire attempt；hit 写独立 `ModelCacheHitRecord`，`provider_request_id=None`，
usage/cost 明确为 cache values，不能伪装成 provider attempt。cache backend failure作为 cache
degraded event 后继续正常 admission，不进入 endpoint failover。

semantic cache 的 embedding 请求本身必须经过 principal、governance、budget、fair queue、bulkhead
和独立 attempt journal。它由系统内部生成不可伪造的 `cache_mode=bypass`、
`origin=semantic_cache_lookup` 标记，跳过所有 response-cache lookup，防止 embedding 再触发 semantic
cache 形成递归；外部 schema 不接受这两个 privileged 字段。缓存同时支持 TTL、按 tenant/namespace 删除、encryption at rest、region
placement 与 audit；tenant 默认绝对隔离。

### 13.3 Reasoning Replay Continuity

对明确声明严格多轮 replay contract 的 provider/model，持有外部conversation scope的Product compatibility
surface负责恢复被兼容客户端剥离的原始 reasoning content。它不是 response cache，不进入
`GatewayDataPlane`、ContextManager或Mote会话业务状态，也不得改变logical history。surface在构造
`ModelInvocation`前解析并验证typed `ReasoningReplayBinding`，将恢复内容作为明确的canonical assistant
reasoning字段交给logical owner；后续provider translator只做provider-native字段映射。Shared daemon不能按
tool-call ID或“最近记录”自行查找caller会话。

replay entry使用现有加密 ArtifactStore、publisher、TTL、retention、legal hold、audit与GC，禁止新建
reasoning cache数据库/GC。索引至少绑定 tenant/project、conversation ID、assistant-turn canonical digest、
provider protocol/model capability identity、generation ID/digest与可选 tool-call IDs；tool-call ID不能单独
作为 authority。内容和digest完整保存，超过大小上限必须显式拒绝或使用现有分块 artifact，禁止静默截断后
重放。非流式capture只接受已通过response validator的terminal结果；流式可将reasoning增量写入现有有界/
artifact spool，但只有收到合法terminal后才原子publish replay artifact与索引。provisional、discarded、
truncated或terminal-error输出不得成为可重放entry；已对客户端commit不等于允许replay publish。

查询映射的唯一owner仍是通用Artifact基础设施：优先扩展现有artifact metadata/ownership query；复用审计
证明现有port无法表达强键查找时，才允许在`contracts/ports/artifact/`增加通用`ArtifactLookupIndex`，由
现有ArtifactStore backend实现。禁止在inference/Product compatibility下建立reasoning私有表、SQLite模块、
catalog或cleanup job。lookup key是上述scope的canonical digest，value只保存`ArtifactRef`、generation、
publication revision与retention metadata，不复制reasoning内容。

发布采用可恢复的publication state machine：`BLOB_DURABLE → ARTIFACT_COMMITTED → LOOKUP_INDEXED →
GC_ROOT_REGISTERED → REPLAY_VISIBLE`。visibility只能在前三项及GC root全部可验证后开启；每步以artifact
revision/idempotency key幂等，crash后由现有ReliableArtifactPublisher/publication outbox继续。反向删除先
撤销visibility/index，再按ownership/legal hold解除root并由现有GC删除blob。索引/blob/root部分成功、key
destruction或hold变化进入现有artifact publication/reconciliation，不由translator修补。

仅 parity manifest 标记 `reasoning_replay=required` 的目标可查询；命中还必须验证全部 scope、generation、
artifact digest与原始 assistant turn。跨租户、跨会话、跨模型/协议、跨不兼容 generation或篡改一律
fail closed并产生安全审计。缺失/过期的处理由 provider contract明确为 canonical failure或明确允许的
provider-native空值，不能用“最近一条”猜测。客户端已经提供合法 reasoning时以其经过digest验证的原值为准，
不得被旧 cache覆盖。

管理面只显示 metadata、命中率、age、size和digest，不返回完整 reasoning；内容访问遵守敏感 audit artifact
的 purpose-bound审批。Gate 0 conformance覆盖 capture/hit/miss/expiry/tamper、重启恢复、客户端剥离、多个
tool call、相同 tool-call ID跨 tenant/session冲突、generation切换、provisional stream discard与GC。

replay artifact与索引遵守原会话/tenant的删除和合规关系：conversation删除、tenant erasure、retention到期
必须通过现有Artifact ownership/reachability流程级联；legal hold可阻止blob物理删除但必须立即撤销普通replay
可见性。加密key销毁后entry不可恢复且索引不得继续宣称可命中；审计导出默认只含metadata/digest，内容导出
需要与敏感artifact相同的purpose-bound审批。删除、hold、key-loss与索引/blob部分失败都进入现有publication/
GC reconciliation，禁止compatibility translator私建清理任务。

---

## 14. HTTP、SDK 与生态兼容入口

首版交付 aiohttp Application，但 Markdown 路由示例不作为协议真相。Gate 0 必须从版本化 typed wire
models 生成、冻结并提交：

```text
zdocs/parity/openapi/inference-v1.json
zdocs/parity/openapi/admin-v1.json
zdocs/parity/asyncapi/realtime-v1.yaml
zdocs/parity/asyncapi/webhooks-v1.yaml
zdocs/parity/rpc/gateway-v1.proto
zdocs/parity/wire-fixtures/<surface>/<operation-id>/...
```

这些产物覆盖 OpenAI/Anthropic compatibility、Responses lifecycle、Batch、File、Container、image/audio/
video、Realtime/WebSocket/WebRTC control、MCP、provider lifecycle、health/readiness 和全部管理 API。每个
HTTP operation 固定 path/method、query/header allowlist、request/response/error schema、status、auth
scope、pagination/idempotency、multipart/size limits、disconnect/cancel；每个 SSE/AsyncAPI operation 固定
event name、ordering、`[DONE]`/terminal、frame schema、backpressure/reconnect；webhook 固定 signature、
timestamp/replay window、delivery ID 和 acknowledgement。vendor extension 必须是 namespaced typed field，
不能依赖透传任意 JSON/header。

handler 只负责：认证 → wire schema 校验 → canonical `ModelInvocation` → shared ModelGateway → wire
response。它不直接调用 provider transport，因此内部与外部流量共享同一治理和可靠性路径。

冻结基线中的 OpenAI、Anthropic、Bedrock、Google GenAI、Cohere、LiteLLM、PydanticAI、Cursor 等
integration routes 和 schema mapping 全部进入 parity matrix。兼容指请求、响应、stream、错误、
header、模型解析和生命周期行为等价，不只是路由存在。

每个 parity cell 必须引用 frozen route/operation ID、request/response schema digest、stream/session
protocol、canonical mapping、canonical failure mapping、auth/RBAC scope、size limits 和至少一个 wire
fixture digest。引用缺失则 manifest 无效。wire fixtures 同时包含合法、边界和恶意输入，不得只保存
happy path。

### 14.1 Provider translation contract

每个 provider/operation cell还必须引用版本化 translation profile，机器可读声明：请求字段
`preserve|drop|rename|derive|reject`规则、reasoning/tool/cache-control原生字段round-trip、stream event映射、
未知event策略、forbidden/upstream header、client compatibility profile与canonical failure。字段处理必须在
首次 wire authorization前确定；禁止收到 provider 400后偷偷删除字段并自动产生第二次 wire request。
若显式 fallback允许再次尝试，必须由 logical owner创建新 attempt并取得新 `WirePermit`。

client compatibility profile不能仅凭`User-Agent`猜测。profile由明确listener/route、认证principal允许的
client profile ID、API version header与negotiated protocol revision共同选择，并绑定当前generation；SDK
版本只作为可审计hint，不能扩大字段、权限或provider候选。未知、冲突或不支持的profile默认使用严格
canonical profile或结构化拒绝，行为由route contract冻结。响应回显实际profile/revision（不泄露内部策略），
Shared gRPC capability negotiation同样执行N/N-1规则；resume期间不得无记录地切换profile。

冻结实体为`ClientCompatibilityProfile(profile_id, revision, surface, api_versions, request_decoder_revision,
response_encoder_revision, stream_mapping_revision, failure_envelope_revision, allowed_extensions,
min_reader_revision, generation_compatibility, retire_after)`。logical owner在接收请求时把profile ID/revision/
digest写入journal、execution request与receipt关联；resume必须读取原revision或通过显式、已journal的
兼容迁移，不能重新协商后静默切换。profile decoder/encoder至少保留到所有引用它的journal、receipt、session、
replay与N/N-1最大恢复窗口结束；retire前必须证明引用归零，未知旧revision返回结构化
`CLIENT_PROFILE_REVISION_UNAVAILABLE`，不得猜测解码。

model/catalog entry记录source URI/commit或fixture、observed-at、freshness、signature与provenance。目录更新先
生成结构化diff，执行schema/capability/translation shadow validation，再作为同一
`GatewayRuntimeGeneration`激活；handler不得读取未激活的实时目录。context-window、max-output、reasoning与
tool能力是路由eligibility硬约束，不只是评分因子。

Product管理面提供无副作用协议转换模拟器：只接受脱敏输入或冻结fixture，展示canonicalization、字段变换、
event/failure mapping与generation/profile revision，不调用provider、不签发permit、不写健康/usage状态；可显式
选择受支持client profile/revision，但不得模拟未授权能力。

### 14.2 只读 Traffic Inspector

管理面提供 pipeline-native只读诊断视图，展示脱敏后的canonical request、provider request结构、响应事件
映射、translation profile与routing decision trace。它通过现有typed events、audit artifacts和fixture
replay构建，不部署MITM CA、TLS/browser fingerprint伪装、系统代理或透明解密。

默认不保存完整prompt、reasoning、secret、credential/header或binary payload；capture必须显式采样并绑定
tenant/project、purpose、TTL、region、encryption key、RBAC和不可变访问审计。导出使用独立权限和二次
redaction，保留schema/profile/generation digest。允许离线fixture replay，禁止从Inspector重放真实请求、
调用mutation endpoint或制造外部副作用。

SSE 使用有界 writer 和 disconnect cancellation。请求体、header、model、tool schema 和 multipart
都有明确大小上限。错误通过现有 `MoteError` code 映射到 OpenAI/Anthropic envelope，不向客户端
泄露 provider secret 或内部 exception。

管理 API 与 inference API 分离鉴权、CORS、CSRF 和 network binding；默认只监听 loopback。

OpenAI/Anthropic 官方客户端只作为黑盒 compatibility test driver：对冻结 server API 发真实调用并比对
wire/result/error/stream 行为；不得被 production handler 或 provider transport import 为发送路径。

### 14.3 管理面 RBAC

Gate 0 冻结 `zdocs/parity/admin-rbac-v1.yaml`。角色基线为 `platform_admin`、`security_admin`、
`tenant_admin`、`project_admin`、`operator`、`auditor`、`developer`、`viewer`；角色不是代码中的 if/else，
矩阵按 `action × resource_type × scope` 声明 allow/deny、审批、敏感字段和审计要求。

每个 provider、credential、tenant/team/customer、budget/quota、routing、generation、plugin、session、
receipt/reconciliation、cache/audit 与 deployment 操作必须覆盖 read/create/update/delete/rotate/drain/
reconcile 中适用 action，绑定 platform/tenant/project/self scope。默认 deny；tenant principal 不能引用或
推断其他 tenant 的 resource ID、count、error detail 或 audit payload。

查看 secret 原文永久禁止；敏感 audit payload 需要 purpose-bound short lease、双人审批和不可变访问
事件。break-glass 使用短期、单次、reason/ticket 绑定 credential，独立告警、全量审计、自动到期且
不能绕过 tenant scope/legal hold。管理 API middleware、service method 和 UI visibility 都由同一矩阵
生成/验证；UI 隐藏按钮不构成授权。每个 OpenAPI admin operation 必须引用 RBAC action/resource，CI
检查无孤立 operation 或权限规则。

---

## 15. MCP、媒体与其他 Bifrost 能力

### 15.1 MCP

Mote 已有 MCP client、tool registry、权限、快照和 Agent ReAct loop，因此不迁移 Bifrost 的第二套
MCP agent loop。迁移的是：

- MCP connection health/pooling；
- OAuth credential lifecycle 中可复用部分；
- gateway HTTP 暴露 MCP server 的能力；
- MCP metrics 与治理映射。

工具调用继续经过 Mote ToolExecutor、capability allowlist、permission classifier 和 journal。

### 15.2 Media

当前 `product/media_generation/providers/openai.py` 多处每次新建 `aiohttp.ClientSession`。迁移时
统一进入 operation-specific transport pool；image/audio/video 不再维护独立连接生命周期。

### 15.3 Batch/File/Container/Realtime

这些不是 generate 的简单变体：

- batch/video/container/file mutation 与 Responses lifecycle 使用第 6.1 节 `durable_operation`；
- realtime/WebRTC/MCP session 使用 `long_lived_session`，upload/download 使用 `artifact_transfer`；
- 四类通过共同 admission、principal、usage ledger 和 telemetry 共享治理，但使用各自 journal/receipt；
- `ModelEndpointAdapter` 被删除，不扩张为包含几十个方法的胖接口。

---

## 16. 配置设计

在现有 Product 顶层 `Config` 中新增版本化 `InferenceConfig` typed 子配置；不创建
`GatewayApplicationConfig` 根、loader、source、override、secret resolver 或 watcher。子模型放在
`contracts/config/inference/`，复用现有 `YamlModel`、layered config、unknown-key rejection 和 Product
composition。顶层闭合模型为：

```text
Product Config.inference: InferenceConfig
├─ schema_version
├─ deployment              # Embedded / Shared / Cluster、node/zone/placement
├─ listeners               # inference/admin/health/UI、TLS、CORS、CSRF
├─ rpc                     # UDS/mTLS gRPC、limits、flow-control、version
├─ providers               # typed endpoint/catalog/protocol/capability bindings
├─ credentials             # stores、slot policy、OAuth、rotation
├─ generation             # Shared artifact/signing/activation/retention
├─ permit_trust            # root、issuer TTL、revocation、audience/skew
├─ scheduling              # queues、DRR classes、bulkheads、overload
├─ governance              # principal、RBAC、virtual key、budget/quota/health
├─ persistence             # SQLite/PostgreSQL、receipt/journal/outbox/migrations
├─ artifacts               # local/object stores、KMS、region、retention/GC
├─ caches                  # exact/semantic/vector/prompt/HTTP
├─ plugins                 # trusted Python、WASM、isolated subprocess limits
├─ security                # proxy、TLS/mTLS、egress/private zones、redaction
├─ observability           # metrics、OTel、audit sinks、sampling/labels
├─ lifecycle               # readiness、shutdown/drain、reload、SLO revision
└─ compatibility           # OpenAI/Anthropic/media/MCP/realtime/UI feature surfaces
```

provider/model字段优先扩展现有 `ModelsConfig`，媒体字段优先扩展现有 `MultimodalConfig`；只有真正属于
共享 gateway data plane 的字段进入 `InferenceConfig`，禁止复制已有 endpoint/failover/resilience 配置。

不得出现 `resources: {}`、`extra` 或 provider/plugin 任意 dict 来承载未建模语义。可扩展集合使用
discriminated union + versioned typed entry；未知 discriminator/field 一律拒绝，不能静默忽略。

Gate 0 从现有 Product Config 中的 Pydantic model 确定性生成并冻结：

- `zdocs/parity/inference-config-v1.schema.json`：类型、required/default、范围、单位、enum、条件约束；
- `zdocs/parity/inference-config-semantics-v1.yaml`：每个 JSON Pointer 对应的 owner/lifecycle/security
  语义，至少包含：

Pydantic config 是唯一手写真相；JSON Schema 只能由它确定性生成并做 drift check，禁止反向加载或
手工维护第二份 schema。

```yaml
path: /security/private_network_zones/items
owner_layer: product
secret_reference: false
generation_artifact: true
hot_update: generation_reload
drain_required: true
restart_required: false
deployments: [embedded, shared, cluster]
release_scope: [current_embedded, current_shared]
env_mapping: null
admin_mutability: approval_required
audit_event: gateway.security.private_zone.changed
redaction: structural
```

每个字段必须明确类型、默认值、合法范围和单位；是否 secret reference（配置只允许 opaque URI/slot，
不允许明文）；是否进入 `GenerationArtifact`；支持 hot update、generation reload、resource drain 或 process
restart 中哪一种且只能有确定组合；三种部署的适用性；环境变量映射；admin API 可变性；审计事件与
审批等级；以及 current/future release scope。没有 semantics entry 的 schema leaf 使 Gate 0 失败。

precedence、layer provenance、CLI override 与环境变量行为完全复用现有 Product config loader；本文不
定义第二套顺序。admin activation 生成现有 config source 能消费的签名 revision，不旁路 loader。
secret resolution 复用现有 secrets config，发生在校验后的目标节点本地，不把 secret 写回 resolved
config。所有序列化默认 redacted，diagnostic dump 也使用同一 serializer。

配置 schema 使用整数 `schema_version` 和第 3.5.2 节 expand/contract migration；未知高版本 fail closed。
现有 config watcher 触发 GenerationArtifact build/validate/stage/activate，不新增 gateway watcher；
restart-only 字段不得伪装热更新。环境变量只使用现有 loader 支持且 semantics 文件显式列出的 mapping，
禁止另加通用前缀把任意树路径注入。

跨字段校验至少包括：

- 所有 queue/capacity 为正且有硬上限；
- reserved fractions 总和 ≤ 1；
- stream mode 只能是 `commit_on_first_chunk` 或 `buffered_commit`；两者都允许首个外部 chunk 前 fallback，
  唯一禁止的是 chunk 已发送后仍透明 fallback；
- HTTP 非 loopback 必须显式 auth 与 TLS/reverse-proxy trust policy；
- provider transport、gRPC client 与 proxy retry 必须声明 disabled；
- Shared 必须是 UDS gRPC，Cluster 必须是 mTLS gRPC；Cluster 禁止 SQLite hard truth；
- receipt retention ≥ resume window，artifact GC ≥ 所有引用 retention，permit issuer TTL ≤ trust policy max；
- private endpoint 必须引用已审批 network zone，不能直接放宽 CIDR；
- enabled compatibility surface 必须有对应 frozen OpenAPI/AsyncAPI operation、RBAC scope 和 size limits。

config schema/semantics digest 进入 `GenerationArtifact`、Gate 0 approval record 与 workgraph inputs。

---

## 17. 可观测性

Embedded 通过现有进程内 EventBus 发布。Shared 保留 caller 与 daemon 两个独立的进程内 EventBus，禁止
新建跨进程 EventBus 或把任一平面冒充另一平面：

- caller plane 发布 logical call、journal、plan/failover 与用户可见 terminal；
- daemon plane 发布 queue、wire、receipt、connector 与 provider evidence；daemon 不得合成 caller logical event；
- gRPC 传播 W3C trace context、model-call ID、attempt/execution ID 与 generation ID，用于关联而非复制事件；
- metrics 强制带受控 `service=caller|daemon` 标签；audit 可由 durable sink 汇聚，但必须保留 source、actor
  与原始事件 ID；alert catalog 的每个 signal必须声明来自 caller、daemon或两者的关联规则。

各自通过 EventBus 发布，不在 worker 热路径手写 logger：

- request queued/dequeued/rejected；
- queue wait；
- dispatcher lag、execution task active 与 bulkhead utilization；
- client pool connection wait；
- attempt TTFT/latency/stream bytes；
- admission/budget/rate-limit verdict；
- cache hit/miss；
- generation activated/drained；
- plugin latency/failure。

Prometheus/OTel exporter 是 subscriber。metric labels 仅使用受控低基数字段：provider、transport、
region、workload class、failure reason、cache outcome；call/session/tenant ID 只进入 trace/audit。

Bifrost logging plugin 的 raw request/response 能力迁成独立加密 audit sink，默认关闭并带 TTL，
绝不混入普通日志。

---

## 18. 部署与 HA 一致性

当前发布交付前两种部署，第三种保留已冻结契约但延后生产实现：

1. **Embedded**：默认，直接函数调用，适合 CLI 和单机 swarm；
2. **Shared Process**：同机多个 Mote 进程通过 Unix domain socket + gRPC/HTTP2 共享一个 Python gateway daemon；
3. **Cluster（后续）**：多个 Python gateway 实例 + PostgreSQL durable state + 分布式 placement，
   通过 mTLS gRPC/HTTP2 调用；不属于当前 Release Gate。

第二、三种形态只是四个 runtime facade port 的 gRPC remote adapter。四个 logical gateway、canonical
contracts、provider transports、fair queue 和治理逻辑不分叉。

一致性规则：

- routing/failover/journal 仍只在调用方 RuntimeModelGateway；远程 gateway 不复制；
- Shared adapter 传 attempt/generation/deadline/principal，不传 fallback list；future Cluster envelope
  保留相同字段名；
- Shared daemon 只接受本 daemon configuration authority 已 staged/activated 的 artifact digest；caller
  不存在自行选择“最新版本”或 quorum。future Cluster quorum 语义未冻结；
- SQLite 允许 Embedded 或单 Shared daemon 单写；Shared 多 daemon/未来 Cluster 的 hard budget 和 lease
  必须使用 PostgreSQL 原子事务；当前 Shared 不允许同时启动两个 active daemon 操作同一 SQLite；
- worker ownership 用 lease + fencing token，旧实例不能在失去 lease 后接收新 dispatch；
- queue item 仅在 receipt 尚未到 `SEND_COMMITTED` 且旧 lease 失效时可安全重派；此后 worker 丢失进入
  receipt/provider 对账或 `IN_DOUBT`；
- rolling upgrade 同时支持 N 与 N-1 wire contract，generation pin 到具体 capability revision；
- readiness 分别报告 generation、dispatcher、ledger、credential store、connector 与 provider probe；
- ledger/cache/telemetry 的 fail-open/closed 行为见故障矩阵，禁止异常偶然决定。

Embedded 与 Shared 必须通过同一 conformance suite。未来 Cluster 复用同一 suite 并增加 placement/
partition/HA certification；进程共享不是另一套产品，只是四个 runtime facade 的 gRPC adapter。

### 18.1 冻结的远程 RPC contract

当前正式远程协议是 UDS 上的 gRPC/HTTP2：Shared listener 只绑定受权限保护的 UDS，并结合 peer
process identity。IDL 唯一真相为 `zdocs/parity/rpc/gateway-v1.proto`，禁止实现另一套 HTTP JSON
internal API。future Cluster 只预留 service/message namespace 与 version envelope；mTLS 是未来最低安全
要求，但证书、LB、quorum 与跨节点 flow-control contract 当前不冻结。WebRTC media/data 不穿普通 unary RPC，但 session 创建、授权、signaling、状态
和关闭仍使用同一 IDL；大 artifact 只传第 18.5 节 artifact reference/digest，不塞入 RPC message。

IDL 必须覆盖：start unary/durable/session/transfer execution、server lifecycle event stream、permit
authorization、cancel、receipt query/reconcile、generation stage/readiness/activation observation、capability
negotiation、health/readiness、session bidirectional application stream 和 transfer-part command。每个 request
携带 schema/protocol version、generation/artifact digest、principal proof、absolute+remaining deadline、
trace context 和幂等 identity；每个 event 携带 sequence/cursor 与 receipt revision。

RPC policy 固定为：

- Shared UDS 校验文件权限、peer UID/process identity 和 Application credential；tenant/project 来自认证
  后的 principal delegation，不能信任 metadata 自报值；future Cluster workload identity细节不冻结；
- 每个 method 固定 authorization scope，并由 server 重新验证 permit audience、generation 与 tenant；
- protobuf message、metadata、单 frame、stream buffered bytes 和 concurrent streams 都有 schema 中的硬
  上限；artifact/media 超阈值必须改传 reference；
- 默认不压缩；仅对显式 allowlist 的非敏感、大且可压缩 message 启用 gzip，设置解压后上限和 ratio
  guard，credential/secret payload 禁止压缩；
- 使用 HTTP/2 flow control + bounded application queue，禁止 unbounded grpc task；keepalive interval、
  timeout、max age 和 abuse limit 全部冻结在 config semantics；
- reconnect 使用 execution ID + last event cursor 查询 receipt并续流，event 至少一次、按 sequence 去重；
  不自动重新执行 start/authorize/send；
- 跨进程 deadline 只按第 3.4 节转换一次；gRPC deadline 取更早值，不能在 retry/reconnect 时重置；
- gRPC status/detail 通过冻结 mapping 唯一转换为 canonical `FailureDisposition`，业务 terminal 不借 transport
  status 另开事实通道；
- 握手协商 N/N-1 proto/service/capability range；无交集返回结构化错误且不 admission；
- server drain 先从 LB/readiness 摘除、拒绝新 execution、允许 pinned streams 到 deadline，再写确定 terminal；
- future Cluster LB 字段只冻结 service/message extension point，具体 selection/sticky 算法不在当前 Gate 0；
- gRPC service config、client、sidecar/service mesh 的 request retry/hedging 全部禁用；只有 logical owner
  可以凭新 permit 发起新 wire unit，RPC reconnect 不能重放业务；
- OTel trace/baggage 使用 allowlist，禁止 secret/高基数 principal 原文；metrics 区分 RPC transport 与
  provider wire latency。

Shared remote adapter、fault simulator、N/N-1 test 和 SLO 都从同一 proto生成；future Cluster 只能扩展
预留 envelope/namespace，不得破坏 Shared contract。proto/API digest
属于 GenerationArtifact 与 Gate 0 approval。任何 breaking RPC 变更走第 21.1 节 change-control。

### 18.2 Shared daemon ownership 与恢复

Shared daemon 生命周期是显式状态机：

```text
ABSENT → STARTING → READY → DRAINING → STOPPED
           │          │
           └──────────┴→ CRASHED → RECONCILING → READY
```

- Product CLI/Application supervisor 是唯一 launcher；使用 OS 规范的 per-user runtime directory，路径不得
  来自普通请求。启动以 lock file + OS advisory lock single-flight，多个 caller 同时冷启动只有 lock
  winner 创建 daemon，其他 caller 等待带 deadline 的 readiness；
- runtime directory、lock、PID metadata、UDS 与 daemon identity key 均归当前 OS user，目录 `0700`、
  socket/metadata 最小权限；accept 后校验 peer credentials。不同 UID 永远不能连接，即使知道 socket path；
- PID file 不是所有权证据。只有持有 advisory lock，并证明记录的 process incarnation不存在、socket
  connect/probe 失败，才能原子移走 stale socket；禁止看到旧文件就 unlink；
- daemon socket 使用 generation-suffixed path，stable discovery record 原子指向 READY generation。升级先
  启动/验证新 socket，再切 discovery；旧 daemon DRAINING 到引用归零。rollback 重新指向仍兼容的旧
  generation，绝不覆盖运行中 socket；
- SQLite、receipt、ledger、outbox、credential store 和 generation activation 只能由 daemon 写；caller、
  admin API 和 UI 一律通过 gRPC，不能直接打开数据库或绕过治理；
- caller 保存 execution ID、last event cursor、receipt revision 和 generation digest。断线后先 discovery/
  version negotiation，再 query receipt + cursor 续读；event 至少一次并按 sequence 去重，禁止自动重放
  start/permit/application message；
- daemon restart 进入 RECONCILING，先恢复 SQLite WAL、receipt/outbox、generation 与 artifact references，
  再开放 readiness。`SEND_COMMITTED` 无 terminal 的 execution 查询 provider或进入 `IN_DOUBT`；未 commit
  且 caller journal 有 permit 的 execution 按第 3.3 节恢复；
- caller 与 daemon 的 proto range、config schema digest、GenerationArtifact digest 不兼容时 fail closed；
  daemon shutdown 先发布 DRAINING event、readiness false 和 deadline，caller 得到确定 terminal或可续读
  receipt，不靠 EOF 猜状态；
- 最后一个 client 退出不自动停止 daemon。lifecycle 由 typed idle policy 控制，默认保持运行；idle stop
  必须确认无 queued/active/reconcile/admin migration，写 STOPPED 后释放 lock。

Gate 0 生成 `zdocs/parity/shared-daemon-v1.md`，并在 simulator 覆盖：双 caller 冷启动、STARTING crash、
stale PID/lock/socket、每个 attempt/permit crash point、caller permit 前后 crash、daemon upgrade/rollback、
磁盘满、SQLite busy/corrupt、UDS owner/mode/peer attack、慢 event consumer和 restart cursor恢复。

### 18.3 Shared SQLite authority

Shared SQLite 只允许持有 daemon advisory lock 的进程打开写连接；caller/admin 禁止以任何模式打开 DB。
数据库、`-wal`、`-shm`、backup 与 migration lock 均位于 per-user local filesystem，目录 `0700`、文件
`0600`，禁止 NFS、SMB、FUSE/network filesystem。

- 使用 WAL、foreign keys、明确 busy timeout；写事务短且有界，禁止在事务内等待 provider/network；
- durability 默认 `synchronous=FULL`，receipt `SEND_COMMITTED`、permit consumption、ledger settlement、
  outbox append 不得降级；checkpoint 使用被监控的 size/time 阈值，shutdown 做 bounded checkpoint；
- receipt、ledger reservation/settlement、outbox 与现有 artifact publication/reference records 默认同库，在需要原子顺序
  的转换中使用一个事务。若未来拆库，必须先通过新设计评审，不能用异步双写假装原子；
- 配置 hard/soft 磁盘水位；soft 停止非必要 spool/cache并告警，hard 在任何新 wire authorization 前
  fail closed，但允许 bounded reconciliation/cleanup；
- 使用 SQLite online backup API 生成带 schema/generation/digest 的加密备份，定期 restore drill；启动和
  checkpoint 后运行 quick_check，维护窗口运行 integrity_check；
- corruption 时停止 readiness并保留原文件，按最近验证备份 + WAL/receipt evidence恢复；不能自动删库。
  恢复后所有 open receipt 对账，证据不足进入 `IN_DOUBT`；
- migration 取得独占 migration lock、验证空间/备份/schema digest；失败保持旧 daemon ACTIVE并回滚未
  commit transaction。进程 crash 后由 SQLite WAL recovery，再执行 receipt/outbox reconciliation。

这些字段全部进入 `persistence.shared_sqlite` typed config 与 conformance tests。

### 18.4 Future Cluster extension points

Gate 0 仅在 schema/proto 中保留 future_cluster scope、ID namespace、version envelope、receipt ID、
generation/artifact digest 和 migration extension point。跨机器 queue ownership、lease/fencing、placement、
quorum、mTLS trust/LB、network partition、split brain 与 rolling upgrade 均不形成冻结行为，也不接受
生产实现。原先详细 Cluster queue/quorum 方案撤回为研究材料，避免在没有真实部署反馈时制造兼容债。

### 18.5 ArtifactStore durability

receipt terminal response、media、stream spool、audit payload 与 artifact transfer 禁止引用 worker 临时
目录。这里的“artifact services”只是现有 `ArtifactStore` + `ArtifactBlobStore` + `ArtifactResolver` +
`ReliableArtifactPublisher` 的组合，不是新类族或 store。stream spool、terminal response、media、audit
全部使用现有 typed `ArtifactRef`、publication state、retention、sensitivity、ownership 和 session roots。
需要 object store 时实现现有 artifact/blob port；需要 region、encryption key version 等 metadata 时扩展
现有 artifact contract和 migration，禁止 inference 私有 catalog。future Cluster backend 不属于当前实现。

发布顺序固定为：

```text
write bounded staging object
→ finalize immutable object version
→ read-after-write/HEAD + size + content digest verify
→ existing artifact publication state COMMITTED
→ receipt/outbox transaction may reference ArtifactRef + digest
```

object store 与数据库无跨系统原子事务，因此复用现有 `ReliableArtifactPublisher` 的
durable-before-reference/publication outbox：publication 未 `COMMITTED` 的 `ArtifactRef` 不能写入 terminal
receipt。finalize 后、receipt 前 crash 只产生无引用 orphan，由现有 GC 的 grace period 回收；绝不允许
先提交 receipt 再补上传。daemon crash/restart 后必须能通过现有 resolver 按 ref、scope 和 version读取
相同 bytes，digest 不符立即 quarantine publication并把 receipt置为
reconciliation/security failure，不能静默返回损坏内容。

生命周期规则：

- envelope encryption 每 artifact 使用 data key，现有 artifact metadata 记录 KMS key/version；rotation 不改变 content
  digest，重加密产生受审计的新 object version；worker 不持久化 plaintext key；
- authorization 同时校验 tenant、project、principal、region/residency 和 purpose，知道 digest 不等于
  获得读取权限；跨 region 复制必须由 policy 明确允许；
- reachability 来自 journal、receipt、open session/transfer、resume checkpoint、audit/legal hold；使用
  transactional reference record 或可证明的 mark-and-sweep，不依赖易丢内存 refcount；
- retention 取所有引用者恢复窗口、provider async lifecycle、审计/法规期限的最大值；GC 只能在引用
  全部关闭、retention 到期、无 legal hold 且完成两阶段 mark/delete 后执行；
- tenant 删除通过现有 ownership/GC publication record 生成 tombstone并撤销访问，再按 legal hold/审计
  策略异步 crypto-shred/object delete；删除失败可重试且可对账，不能假报已删除；
- staging、spool、multipart upload、download range 都有 byte/time/part 上限；abandoned multipart 由
  transfer journal + backend listing 对账，不能无限累积。

现有 artifact contract/store schema 同样受第 3.5.2 节 expand/contract 约束。故障测试必须覆盖
finalize/verify/publication/receipt 每个 crash point、object store 短暂不可用、key revoke、跨进程/daemon restart resume、
GC 与 legal hold。跨机器 resume 标为 `future_cluster`。

---

## 19. 高可用故障规范

### 19.1 故障矩阵

| 故障 | 新请求 | queued | WIRE_STARTED 后 | 恢复/降级 |
| --- | --- | --- | --- | --- |
| 单 provider/bulkhead hang | 其他 bulkhead 正常 | 本 flow deadline/aging | timeout 后 classified failure | breaker 隔离，不级联 |
| dispatcher/daemon crash | readiness false | 内存 queue 丢失，由 caller journal + receipt 幂等重交 | execution receipt 对账 | Embedded process resume；Shared supervisor restart + RECONCILING |
| worker/process crash | 停止 dispatch | wire 前可重派 | 无 terminal 证据则 IN_DOUBT | journal + receipt + provider idempotency 对账 |
| UsageLedger 不可用 | hard budget fail closed | 不继续 admission | 已发请求完成，settlement 写 durable outbox | 恢复后幂等 settle |
| cache 不可用 | cache degraded，继续 provider path | 不影响 | 不影响 | 不触发 fallback |
| telemetry subscriber 崩溃 | 继续 | 继续 | 继续 | 有界 spool；审计强制场景单独 fail closed |
| credential store 不可用 | 需要新 handle 的请求拒绝 | 已有有效 handle 可继续 | 不撤销正在使用的 handle | refresh single-flight 恢复 |
| PostgreSQL 短暂不可用（配置该 backend 时） | hard-governed admission 关闭 | 已接收项按 receipt 状态处理 | settlement outbox | 不允许超卖预算 |
| Redis/vector backend 不可用 | semantic cache degraded | 不影响 | 不影响 | Redis 不作为 hard truth |
| generation 构建失败 | 旧 generation 继续 ready | 不迁移 | 不影响 | 新 revision 不发布 |
| rolling upgrade | capability negotiation | generation pinned | 原 worker 完成 | N/N-1 contract，drain 后退出 |
| shutdown | admission closed | 每项 QUEUE_CANCELLED 或安全转移 | deadline 内 drain，之后 CANCELLED/IN_DOUBT | 所有 Future 有 terminal |

所有外部副作用 operation 必须按第 6.1 节 taxonomy 进入对应 Gateway；不得笼统塞入
`ServiceGateway`。没有 provider reconcile API 时必须保留 `IN_DOUBT`，禁止通过盲重试伪造恢复。

配置发布严格使用第 3.5.1 节 Generation Protocol。rollback 发布先前 generation 的新 activation
record，不把正在执行的 generation 原地改回。

### 19.2 安全规范

- endpoint authority 只能来自受管理 catalog；普通 inference request 不能携带任意 base URL；
- 普通请求指定的任意 private address 默认拒绝；受管理 catalog 只能引用经 security approval 的
  `PrivateNetworkZone`，zone 精确绑定 tenant、provider、environment、CIDR、DNS suffix、scheme、port、
  proxy/route 和 egress identity，普通请求不能创建、选择或扩大 zone；
- metadata、loopback、link-local、unspecified、multicast 地址在所有 zone 中始终拒绝；本机 Ollama/vLLM
  必须通过受管 UDS/sidecar service identity 暴露，不能把 loopback 加入 HTTP egress allowlist；
- DNS resolve 后每个 A/AAAA 必须同时满足 catalog authority 与对应 public/private zone policy；
- 每次 redirect 和 DNS re-resolution 都重验，连接时 pin 已验证地址，防 DNS rebinding；
- passthrough 仍受 provider-specific path/method/header allowlist，不能成为通用 SSRF proxy；
- proxy、CA、client certificate、TLS minimum、SNI 和 verify policy 属于 versioned
  `ConnectionIdentity`；禁止请求级关闭 TLS verification；
- admin 与 inference 使用独立 listener、principal、CORS/CSRF 和 rate limit；admin 默认不监听公网；
- virtual key 只存 keyed hash/verifier，明文只返回一次，支持 scoped rotation、overlap 与 revoke；
- request body、JSON depth、array、tool/schema、header、multipart part、upload、chunk、stream bytes、
  spool、task、queue、connector 和 redirect 次数都有硬上限；
- credential、authorization、cookie、signed URL、OAuth code/token、proxy secret 在结构化错误进入
  EventBus 前统一 redaction；禁止依靠 exporter 二次清洗；
- raw audit payload 独立 envelope encryption，tenant/project RBAC、region placement、TTL 与访问审计；
- trusted in-process Python 插件明确拥有进程权限；不可信插件仅允许 WASM/隔离子进程，并以
  capability manifest、typed RPC、OS sandbox 和 CPU/内存/超时/kill 限制资源；
- management/config 变更带 actor、revision、审批与 immutable audit record；
- 所有依赖和 UI assets 生成 SBOM，镜像签名并验证 build provenance。

这些限制位于 canonical boundary、catalog build 和 transport 三层，不能只靠 HTTP middleware；否则
内部 Agent 或 Shared Process adapter 会绕过同一安全策略。

### 19.3 双备份域、MoteRecoverySet 与恢复协议

Shared 的 daemon 与 Mote application 是两个备份真相域，禁止 daemon coordinator 声称冻结或包含任意
caller-local journal。

`GatewayDaemonBackup` 由 daemon Product 运维层生成，只编排现有 checkpoint ports，不复制 store。其范围是
generation/config activation、receipt/outbox、ledger/quota/health、credential/virtual-key/OAuth store、
provider resource evidence、artifact publication、daemon audit 与 plugin revision：

```text
GatewayDaemonBackupManifest(
  schema_version, backup_id, created_at,
  config_revision, generation_id, generation_artifact_digest,
  database_snapshot_revision,
  receipt_outbox_high_water_marks,
  artifact_publication_revision,
  blob_object_checkpoint,
  credential_store_checkpoint,
  encryption_key_ids,
  schema_migration_revision,
  component_digests,
  signer_key_id, signature, status,
)
```

`MoteApplicationBackup` 继续由现有 Mote session/journal/artifact体系拥有，范围是 model/service logical
journal、rollout/session history、workspace、caller-owned checkpoints 与 logical-owner decisions。daemon
不得读取、flush或改写这些 journal；离线 caller也不因 daemon backup被隐式覆盖。

顶层签名组合清单为 `MoteRecoverySet`：

```text
MoteRecoverySet(
  recovery_set_id,
  daemon_backup_id,
  application_backup_ids[],
  artifact_checkpoint,
  compatibility_revisions,
  missing_participants[],
  cut_consistency_class,
  component_digests,
  signer_key_id, signature, status,
)
```

`cut_consistency_class` 只能是：`APPLICATION_CONSISTENT`（列出的指定 caller全部参与同一 barrier）、
`DAEMON_CONSISTENT`（只保证 daemon cut）或 `CRASH_CONSISTENT`（依靠 receipt/evidence reconciliation）。
不得把 daemon-only backup或缺失 caller 的 cut提升为 `APPLICATION_CONSISTENT`。

需要 application-consistent cut 时执行可验证 Shared barrier：

```text
PREPARE_BACKUP(epoch)
→ daemon 停止新 admission并推进 admission_epoch
→ 已注册 caller 停止签发 WirePermit并 flush自身 journal
→ caller 返回 journal high-water mark与最后签发 permit revision
→ daemon校验 backup_epoch/admission_epoch 与 permit/receipt边界
→ COMMIT_BACKUP(epoch)
```

未响应、离线或 incarnation变化的 caller记录在 `missing_participants`，backup只能降级或失败。daemon
backup流程固定为：暂停 generation activation/credential rotation → flush receipt/outbox与现有 artifact
publication → 确认引用的 ArtifactRef 已 COMMITTED且 blob checkpoint覆盖 → checkpoint credential/OAuth/key
metadata → SQLite online backup → 签名 manifest → readability/digest/key验证 → 隔离目录 restore drill。各 manifest
状态为 `CREATING → VERIFIED → RESTORE_DRILLED → RESTORABLE`，失败进入 `FAILED`；禁止拼接不同 cut伪造成功。

Gate 0 冻结：

- `zdocs/parity/gateway-daemon-backup-v1.schema.json`；
- `zdocs/parity/mote-application-backup-v1.schema.json`（复用现有 Mote backup owner的集成 envelope）；
- `zdocs/parity/mote-recovery-set-v1.schema.json`；
- `zdocs/parity/backup-restore-v1.md` 中的 barrier/epoch、consistency-class与恢复 contract。

并在 SLO/config
中给出数值 RPO、RTO、backup interval、retention 和 restore-drill frequency。恢复协议覆盖：

- 空目录恢复与原地灾难恢复；后者先隔离损坏状态并保留 forensic copy，不覆盖唯一证据；
- 校验 manifest/signature、所有 component digest、schema N/N-1 reader 和 encryption/KMS key availability；
- object/blob 缺失或 digest 错误时不 ready，列出受影响 receipt/artifact；禁止返回部分成功数据；
- credential version/refresh token缺失时，对引用它的 generation fail closed，不回退到其他 secret；
- outbox 按 event ID 幂等重放，允许重复投递但不重复 settle/publication；
- open/`IN_DOUBT` execution进入第 19.4 节 evidence/owner reconciliation，不能假装失败后重试；
- 从 manifest generation/config revision重新 build、shadow validate、activate；验证完成前 readiness false；
- restore verify失败保留诊断与审计，禁止自动尝试更旧备份，除非 operator明确选择并记录潜在 RPO损失。

backup 本身使用现有 typed artifact/publication机制保存 manifest和报告；secret/material不嵌入 manifest。

### 19.4 IN_DOUBT reconciliation control plane

`IN_DOUBT` 不是 terminal 垃圾桶。Shared reconciliation拆成数据面证据与 caller逻辑决策两段，daemon
永远不能替 caller追加 logical terminal。

`DaemonEvidenceReconciler` 有界扫描 receipt并调用 Product provider query/webhook/cancel/cleanup evidence
port。它可以验签 webhook、查询 provider、收集 request/resource/usage事实、保存不可变
`ResolutionEvidence`，并通过 durable outbox发布 `ResolutionProposal`；它不能 settle logical result、写 caller
journal或发布用户 terminal。

对应的 `CallerLogicalReconciler` 是 model/service/session/transfer logical owner的一部分，在 caller运行或
resume时读取自身 journal与 daemon evidence，验证 strategy/generation后追加唯一 decision/terminal，完成
logical usage/cost settlement与用户状态，并向 daemon确认 owner action。

```text
OPEN → AUTO_RECONCILING → EVIDENCE_AVAILABLE → OWNER_ACTION_REQUIRED
                                              ├─ OWNER_APPLIED
                                              └─ OWNER_REJECTED
```

- durable record包含 owner taxonomy、attempt/operation/session/transfer ID、generation、provider resource/
  request ID、strategy ID、next scan、attempt count、evidence digests和retention；
- scan interval、global/provider concurrency、backoff、最大自动等待时间和provider query budget均有硬上限；
- evidence优先级固定为 provider authoritative query/terminal receipt > 验签且去重 webhook > durable
  provider acknowledgement > local receipt/wire observation；日志、trace、超时推测不能证明成功；
- 达到 strategy deadline、provider无查询能力、证据冲突、key/artifact缺失或需要 logical decision时进入
  `OWNER_ACTION_REQUIRED`；daemon不得用新 permit盲重做原 operation；
- 人工 action只来自冻结 `reconciliation-actions-v1.yaml`。operator action由 daemon记录审批/evidence并写入
  owner-command durable outbox；caller/resume消费后由 logical owner追加 terminal，再返回 owner acknowledgement；
  涉及财务或 success override需要双人审批，success必须附权威 provider/artifact evidence；
- provider cancel、resource/artifact cleanup等纯数据面动作可由 daemon直接执行；logical success/failure、
  budget settlement与用户 terminal只能由 owner应用；
- 永久离线或过期 caller保持 `OWNER_ACTION_REQUIRED`/明确 unresolved状态与审计，按法规和最大恢复窗口
  retention，不得强行成功、伪造 terminal或普通 GC；
- admin API/UI 显示 age、impact、evidence、下一动作、审批与cleanup，不显示secret；backlog/oldest age/
  failure rate进入告警。

Gate 0 冻结 `zdocs/parity/reconciliation-v1.md` 和
`zdocs/parity/reconciliation-actions-v1.yaml`，包括 evidence、proposal、owner-command/ack contract。每个
parity cell 的 `reconcile_strategy` 必须引用 evidence acquisition与owner action strategy ID及参数schema，
禁止自由文本。Shared daemon启动必须先恢复 evidence cursor；caller resume必须先处理适用 proposal，再开放
对应 logical operation。daemon readiness不得把 `OWNER_ACTION_REQUIRED`伪装成已完成。

### 19.5 Alert catalog、readiness 与 Runbook

Gate 0 必须提交：

```text
zdocs/parity/operations/alert-catalog-v1.yaml
zdocs/parity/operations/readiness-v1.md
zdocs/parity/operations/shared-daemon-runbook.md
zdocs/parity/operations/provider-degradation-runbook.md
zdocs/parity/operations/storage-runbook.md
zdocs/parity/operations/credential-runbook.md
zdocs/parity/operations/reconciliation-runbook.md
zdocs/parity/operations/upgrade-rollback-runbook.md
```

每条 alert 包含 `id/severity/signal/signal_plane/condition/for/scope/user_impact/automatic_action/runbook/owner/
dedupe_key/recovery_condition`，`signal_plane` 只能是 caller、daemon或明确的关联规则；condition使用冻结
metric/label，runbook路径必须存在且包含诊断、止损、恢复、验证、
升级和禁止动作。catalog至少覆盖 queue saturation、dispatch/event-loop lag、breaker、credential
quarantine、ledger、receipt/outbox/reconciliation backlog、artifact publication/GC、disk/SQLite、backup/
restore drill、daemon crash loop/stale UDS、connection pool exhaustion、audit/redaction、plugin、generation
drain 和 live certification freshness。

`/health/ready` 返回整体 verdict + 组件数组，而不是裸 bool。generation、scheduler、receipt/outbox、ledger、
credential store、artifact store、connection pool、audit policy、migration、disk capacity 每项声明
owner plane、required/optional、`ready|degraded|failed`、fail-open/closed、是否接新请求、受影响provider/operation、
稳定错误码和脱敏信息。整体判定由 `readiness-v1.md` 的机器表生成；局部 provider故障只能拒绝对应
scope，hard budget/receipt corruption/audit强制策略等 required failure 则全局 fail closed。

### 19.6 安装、升级、回滚与卸载

当前发布必须产品化首次安装、旧 Mote升级、Shared N/N-1 daemon协议、UI/API asset revision、config
dry-run、database expand、shadow validation、新 socket启动、caller原子切换、旧 daemon drain、contract
migration与rollback。卸载默认保留数据、backup和key references；完全删除必须是独立危险命令，展示
精确路径/tenant、要求二次确认和审计，不能由普通 package uninstall触发。

CLI 至少提供 `mote gateway validate`、`migrate --dry-run`、`backup`、`restore --verify-only`、`doctor`、
`reconcile`、`drain`、`upgrade-status`。每个命令有版本化JSON输出、稳定退出码、human rendering和不可变
审计；非交互模式不降级安全审批。upgrade/rollback runbook必须引用这些命令而非临时 SQL/shell。
Shared 的 `backup` 必须显式选择 daemon-only或application-consistent模式；后者必须声明目标 caller集合、
barrier deadline与缺失参与者策略，输出实际 consistency class，禁止默认宣称全局一致。

### 19.7 Lifecycle 与恢复测试

Gate 0 fault simulator覆盖每个 lifecycle phase任意resource失败、sibling仍被尝试、失败phase可重试、
durability phase不提前、取消等待者不取消shutdown、并发 `aclose()`、daemon在flush中崩溃及第二次启动
恢复未完成close。测试直接运行现有 `LifecycleStack`，禁止用测试替身重新解释phase语义。

---

## 20. 全量 parity matrix

### 20.1 Provider

首版 Release Gate 包含以下 29 个冻结基线 provider（`utils` 不算 provider）：

```text
anthropic, azure, bedrock, bedrockmantle, cerebras, cohere, deepseek,
elevenlabs, fireworks, gemini, groq, huggingface, mistral, nebius, ollama,
openai, opencode, openrouter, parasail, perplexity, replicate, runware,
runway, sarvam, sgl, vertex, vllm, wafer, xai
```

“支持”严格复现 Bifrost frozen commit 的真实成功能力；“不支持”也必须复现其 capability/error
contract，不能因为 Go interface 存在 stub 就宣称支持。parity manifest 由测试扫描源码、provider
fixtures 和 live-contract suite 生成并提交版本控制，人工文档不作为唯一真相。

机器可读清单固定落在 `zdocs/parity/bifrost-ec1dd920.yaml`，枚举 29 providers × 20.2 的每个原子
operation，状态只能是：

```text
supported          # Bifrost 基线成功支持，Mote 必须等价实现
unsupported        # Bifrost 基线明确不支持，Mote 必须等价拒绝
conditional        # 依模型/region/account capability，列出可判定条件
provider_managed   # provider 异步生命周期，列出 receipt/reconcile 规则
```

每个 cell 必须携带 Bifrost source/test evidence、Mote contract test、所需 auth、streaming、side-effect、
idempotency，以及第 6.1 节规定的 execution taxonomy/lifecycle metadata；还必须携带 translation profile、
字段preserve/drop/transform规则、reasoning/tool/cache-control round-trip、stream unknown-event policy、
forbidden/upstream headers、client compatibility profiles、catalog provenance/freshness/signature、
`reasoning_replay=required|forbidden|not_applicable`及fixture digest。该 manifest 是 Workstream 6
的输入和 Release Gate 的机器 oracle；不能在
实现过程中凭印象修改范围。之所以不把 29×数十 operation 的静态表手抄进 Markdown，是手工表会
与源码漂移且无法驱动测试，反而形成文档债。

每个 provider-operation cell 与每个 20.3 生态/分发 item 还必须包含非空集合：

```yaml
release_scope:
  - current_embedded
  - current_shared
# 或仅：
# - future_cluster
```

当前 Release Gate 只选择 `current_embedded | current_shared`；`future_cluster` 不计入完成率、parity
缺口或发布阻断。一个 item 同时含 current/future 行为时必须拆成两个原子 item，禁止用同一状态让
Cluster 能力隐式回流当前范围。scope 变更需要架构审批并改变 parity digest。

#### 20.1.1 Provider certification inventory

Gate 0 必须生成并评审 `zdocs/parity/provider-certification-v1.yaml`。它按 parity cell 引用，不保存
secret，但必须声明：

```yaml
provider: string
operation: string
credential_class: string
account_project_region_capabilities: []
test_model_or_deployment: string
estimated_test_budget: {currency: USD, per_run: 0, monthly_cap: 0}
side_effects: []
cleanup: {procedure: string, deadline: string, verifier: string}
fixture_freshness: {recorded_at: timestamp, max_age_days: 0, schema_digest: string}
outage_evidence: {required_fields: [], max_age_hours: 0}
latest_live_certification_digest: string | null
conditional_capability_rule: string | null
resource_owner: string
resource_ready: bool
```

测试分三层且不能互相替代：

1. **Offline protocol conformance**：fake server、golden request/response/frame、canonical failure mapping、
   retry disabled 与每 wire unit 0/1 request/frame oracle；每次提交运行；
2. **Recorded contract**：从授权 live run 生成脱敏、加密、带 schema/digest/freshness 的 fixture，验证
   streaming、usage、quota、error 和 parser；过期必须重录，不能永久固化旧行为；
3. **Live certification**：使用清单指定的真实 credential class、account/project/region/model/capability，
   覆盖 create/poll/cancel/delete、stream/session/transfer 与 cleanup，输出签名 certification digest。

current-scope supported/conditional/provider-managed cell 发布前必须三层全部满足其适用项；缺账号、
region、模型权限或测试预算不是豁免理由。future_cluster cell 只做资源需求盘点，不要求当前 live
certification。Gate 0 必须确认所有 current required resource 有明确 owner、审批路径、预算上限和
可获得状态；secret 仅进入 credential store。provider outage 只能用 provider status/incidence ID、时间窗、
探测证据和受影响 region/model 形成限时 `outage_evidence`，用于解释本次失败和延迟 certification，不能
把 supported cell 改成通过或绕过 Release Gate。副作用 cleanup 失败会阻断对应 certification，并进入
可对账 cleanup queue。

### 20.2 Operation

| Family | 必须覆盖的 operation |
| --- | --- |
| Model/catalog | list models、count tokens、compaction |
| Text/generate | text completion/stream、chat completion/stream、Responses/stream |
| Responses lifecycle | retrieve/retrieve stream、delete、cancel、input items、WebSocket mode |
| Vector/ranking | embedding、rerank |
| Document | OCR |
| Audio | speech/stream、transcription/stream |
| Image | generation/stream、edit/stream、variation |
| Video | generation、retrieve、download、delete、list、remix |
| Batch | create、list、retrieve、cancel、delete、results |
| File | upload、list、retrieve、delete、content |
| Cached content | create、list、retrieve、update、delete |
| Container | create、list、retrieve、delete |
| Container file | create、list、retrieve、content、delete |
| Realtime | WebSocket、WebRTC、client secrets、sessions/calls |
| Passthrough | buffered、streaming large payload |
| MCP | ping、list tools、execute、chat tool call、Responses tool call、server transports/OAuth |

每个 operation 都有 canonical contract、窄 transport Protocol、provider capability manifest、HTTP/SDK
compatibility fixtures、usage/cost/error/cancel/lifecycle tests。不得把这些方法重新塞进一个胖 Provider。

### 20.3 生态组件

| 类别 | 冻结基线 |
| --- | --- |
| Plugins | compat、governance、jsonparser、logging、maxim、mocker、modelcatalogresolver、otel、prompts、semanticcache、telemetry |
| Integrations | OpenAI、Anthropic、Bedrock、Google GenAI、Cohere、LiteLLM、PydanticAI、Cursor及源码注册的全部 route config |
| Transport | HTTP、SSE、WebSocket、WebRTC、large-payload streaming、webhook |
| Management | providers、credentials、MCP、virtual keys、teams、customers、budgets、rate limits、routing rules、cache、logs、config、plugins、sessions |
| UI/Operations | Web UI、health/readiness、profiling 等价能力、Docker/cluster deployment、metrics dashboards |
| Persistence/framework | configstore、logstore、kvstore、objectstore、vectorstore、migration、encryption、PostgreSQL connection management |
| Catalog/control | model catalog、MCP catalog/headers、routing support、feature flags、temporary tokens、OAuth2、webhooks |
| Vector/object backends | Redis、Weaviate、Qdrant、Pinecone 与 S3、GCS 等冻结基线 backend |
| Distribution | CLI、NPM/NPX launcher、Docker、Helm、Terraform、recipes、Nix 的安装和部署能力 |
| Documentation/examples | API、provider、plugin、治理、部署文档与可运行 examples |

分发范围必须拆成原子项：Python package、CLI、Shared daemon launcher、Docker 单机镜像、本地 config/
migration/backup 工具、UI assets、经基线确认的 NPM/NPX 单机 launcher、Nix/单机 recipes 标为 current；
跨机器 placement、多 active daemon、distributed lease/fencing、cross-failure-domain quorum、Cluster mTLS
LB、Kubernetes HA/multi-replica、Cluster rolling upgrade、分布式 PostgreSQL/object/vector store HA、HA
Helm 与 Cluster Terraform 标为 `future_cluster`。若当前交付 Helm，只允许明确标注 unsupported-HA 的
single-replica chart，不能暗示 Cluster ready。

等价能力不要求复制 Go ABI 或 React 文件结构，但用户可观察功能、API、权限、安全和运维结果必须
达到 parity。Python profiler/diagnostics 可以替代 pprof，但必须覆盖同一诊断目的。

`framework/` 能力不能被插件列表掩盖：冻结基线中的 configstore、encrypt、envutils、
featureflags、kvstore、logstore、MCP catalog/headers、migrator、modelcatalog、OAuth2、objectstore、
query scope、routing、streaming、temporary token、tracing、vectorstore 和 webhooks 均进入对应
Workstream 与 Release Gate。Sidekiq 等语言/运行时特定机制可以用 Python 等价 job backend 替代，
但其 durable async execution 行为必须保留。

### 20.4 依赖与供应链计划

Gate 0 生成 `zdocs/parity/dependency-plan-v1.yaml`，在任何 production package 实现前冻结 aiohttp、
gRPC asyncio runtime、Ed25519/crypto、PostgreSQL driver、WASM runtime、Redis/vector/object-store clients、
WebRTC、UI toolchain 和 provider protocol辅助库的选择。每项至少包含：

```yaml
name: string
version_range: string
lockfile: string
license: string
purpose: string
optional: true
release_scope: [current_embedded, current_shared]
import_layer: product
cve_policy: {max_severity: string, remediation_sla_days: 0}
alternatives_considered: []
sbom_component: string
native_binary: false
platforms: []
```

同一协议/后端只能有一个 production client owner；工作包不能自行引入第二个 HTTP/gRPC/crypto/vector
client。可选依赖使用顶部 guarded import并由 typed config discriminator启用，不得渗入 Runtime contract。
license incompatibility、无锁版本、无 SBOM mapping、超 CVE policy 或缺平台 wheel 会阻断 Gate 0。
future Cluster-only 依赖单独标 scope，不得成为当前安装的强制依赖。

---

## 21. Gate 0 与后续 Workstream

### 21.1 Machine-readable implementation workgraph

Gate 0 生成 `zdocs/parity/inference-workgraph-v1.yaml`，它是 Workstream 2–8 的唯一调度与验收 DAG，
Markdown 编号不表示可以平行启动。每个工作包 schema 至少为：

```yaml
id: string
owner_layer: contracts | runtime | orchestration | product
inputs: []
outputs: []
depends_on: []
contracts_read: []
contracts_write: []
acceptance_tests: []
performance_budget: {slo_revision: string, dimensions: {}}
migration_steps: []
rollback: {preconditions: [], procedure: string, evidence: []}
deletes_legacy: []
release_scope: current | future_cluster
reuses_existing: []
existing_capability_gap: null | string
infrastructure_exception_approval: null | string
```

所有 path、contract revision、test ID、SLO dimension 和 deletion target 必须可由 CI 解析；DAG 必须无环、
每个 output 只有一个 owner、contracts_write 冲突为错误、被删除 legacy path 必须存在且最终全部覆盖。
任何创建 store/lifecycle/config loader/event bus/journal/scheduler/service receipt/poller 的节点必须列出
`reuses_existing`；若 gap 非空则必须有 architecture approval，否则 graph validation失败。
最小依赖骨架为：

```text
gate0.contracts_and_evidence
├─ reuse.existing_mote_foundations
│  ├─ RuntimeModelGateway / RuntimeServiceGateway
│  ├─ ArtifactStore / ReliableArtifactPublisher / GC
│  ├─ LifecycleStack / EngineServices / EventBus
│  └─ Product Config / journals / admission tests
├─ durable.receipt_outbox
├─ durable.ledger_quota_health
├─ adapter.artifact_remote_backend
├─ adapter.credential_store
├─ core.scheduler_generation
│  ├─ generation.owner_and_views
│  ├─ generation.migrate_model_gateway
│  ├─ generation.migrate_service_gateway
│  ├─ generation.attach_session_transfer
│  └─ generation.delete_legacy_reload
├─ transport.protocol_families
│  └─ provider.adapters
├─ gateway.unary
├─ seam.durable_single_command
├─ gateway.session
├─ gateway.transfer
├─ product.routing_policy_and_simulator
├─ product.reasoning_replay_compatibility
├─ product.translation_profiles_and_inspector
├─ adapter.artifact_lookup_index_if_approved
├─ contract.response_validator_automata
├─ failure.compatibility_fixtures
├─ operations.backup_restore
├─ operations.reconciliation
└─ operations.alerts_runbooks

reuse.* + durable.* + core.* + transports + gateways/seams
└─ compatibility_apis_plugins
   └─ management_ui_deployment
      └─ shared_process_certification
         └─ legacy_deletion_release

future.cluster_ha_certification   # 非当前 Release Gate
```

实际 YAML 必须细化 join dependency：provider adapter 同时依赖其 protocol family、canonical failure、
certification resource；四 gateway 依赖 shared scheduler/receipts/governance；Shared certification 依赖
artifact durability、migration、frozen UDS gRPC/config/private-network policy、Embedded/Shared conformance
和 provider certification；compatibility/API/UI 包依赖对应 OpenAPI/AsyncAPI/RBAC/wire fixture digest。
future Cluster 节点保留依赖但不阻塞当前 release。任何包不得只因同属一个
Workstream 而绕过 `depends_on`。

generation子图严格按 owner/view contract → ModelGateway → ServiceGateway → session/transfer → 删除旧
reload入口执行；最后节点增加 architecture test，禁止独立 resolver/generation owner或四类 gateway暴露
reload API。不得合成一个巨型 migration commit。

Gate 0 contract freeze 后，普通下游 package 对 frozen contract 只有 read 权限。变更必须提交 versioned
change request，列出影响节点、compat/migration、SLO 变化、重新运行的 acceptance/fault/certification
集合，并由 contract owner + architecture owner批准后更新 workgraph digest；provider 团队不得临时
扩字段、枚举或错误语义。CI 在 package 声明 revision 与 approved graph 不一致时 fail closed。

当前只批准实施前 Gate 0，不批准全功能并行编码。Gate 0 必须严格按以下顺序完成：

1. 从冻结 Bifrost commit 的源码、tests 与 fixtures 生成并评审
   `zdocs/parity/bifrost-ec1dd920.yaml`，把每个 cell/生态/分发 item 拆为 current/future scope；禁止先手填
   结论再寻找证据；
2. 生成 `provider-certification-v1.yaml`，确认所有 current-scope supported/conditional/provider-managed
   cell 的账号、region、model/deployment、预算、cleanup owner 和 credential provisioning 可获得；
   future_cluster 只登记需求；
3. 按第 6.1 节定稿 execution taxonomy，并冻结第 3.7 节 canonical `FailureDisposition` revision；
4. 生成并评审 config schema/semantics 与 private-network policy，确保全部部署、治理、安全、存储、
   lifecycle 和 compatibility surface 都有 typed、可迁移配置；
5. 冻结 `shared-daemon-v1.md`、`persistence.shared_sqlite` semantics 和 Embedded/Shared 分发矩阵；
6. 生成并冻结 `dependency-plan-v1.yaml`，完成 license/CVE/platform/SBOM 审查；
7. 生成并冻结 inference/admin OpenAPI、realtime/webhook AsyncAPI、Shared gateway proto、admin RBAC 与 wire
   fixtures，为所有 parity cell 建立可验证引用；
8. 冻结 provider translation/client-profile实体、decoder保留与resume规则，reasoning replay lookup/publication/
   retention/erasure contract，catalog provenance、`RoutingObservationSnapshot`、decision trace/dry-run与完整
   counterfactual evaluation contract、逐operation response-validator automata/resource budget、只读Traffic
   Inspector contract；禁止这些能力引入新router、私有index/cache store、MITM或第二次隐式wire request；
9. 定稿并评审 attempt、operation、session、transfer journal/receipt、`WirePermit`、
   `GenerationArtifact`、migration、artifact durability、`GatewayDataPlane` 和 event schema contracts；同时
   产出基础设施复用审计，逐项证明 Artifact/Lifecycle/Config/ServiceGateway/EventBus/journal/admission
   使用现有 owner；
10. 冻结 daemon/application backup域、`MoteRecoverySet`、barrier/permit epoch、evidence/proposal/
   owner-command reconciliation、双观测面、alert/readiness/runbooks 与 CLI/upgrade contracts，并为
   RPO、RTO、drill频率和 reconciliation backlog/age 定义待测 SLO dimensions；
11. 建立不修改生产路径的 reference prototype、deterministic fake provider、fault simulator 与
   request-count/frame oracle；prototype 只验证 contracts，不成为临时生产实现；
12. 不建立或运行 Bifrost 性能对比基线；Bifrost 只作为功能与协议证据，不作为运行时、构建依赖或
   性能门槛；
13. 在 Mote Python 实现具备可测闭环后，为 journal/receipt transaction、permit canonicalization/signature/
   verification、跨进程 hop、bounded queue 与 event persistence 实测 p99/p99.9 overhead，并冻结
   `zdocs/parity/inference-slo-v1.md`；同时冻结数值 RPO/RTO/restore drill与reconciliation SLO；总预算
   必须可由分项合成，不能用 provider latency 掩盖，也不与 Bifrost/Go 比较；
14. 用 fault simulator 验证：每 wire unit 为 0/1 provider request/frame、caller/daemon 在每个转换点崩溃、
   event 丢失/重复/乱序、receipt reconciliation、hedge permit
   隔离、Shared session credential revoke/replay、unknown generation 拒绝、generation digest pin、N/N-1
   schema/wire negotiation、daemon cold-start/stale socket/upgrade、SQLite failure、gRPC reconnect/drain、
   artifact crash/GC/legal hold、backup每个barrier/checkpoint、缺失 caller、caller在 journal flush前后崩溃、
   daemon-only backup、stale permit epoch拒绝、三种 cut consistency恢复、proposal重复/丢失/重放、永久离线
   owner、owner apply/reject acknowledgement、人工 owner-command approval、caller/daemon trace关联、
   replay blob/index/GC-root每个发布与删除crash点、routing snapshot authority revision变化、client profile
   retire/resume、validator byte/frame/time limit、lifecycle各phase与 migration failure/rollback；
15. 执行质量棘轮：translator、response protocol validator与failure mapping mutation tests、provider
   manifest completeness、
   OpenAPI/AsyncAPI/proto breaking-change detector、routing decision matrix、recorded fixture freshness、
   counterfactual routing activation thresholds、性能历史基线/退化阈值。coverage只作辅助信号，不能替代
   协议、wire-count和状态机oracle；
16. 最后以冻结的 contract/API/config/RBAC/SLO/fault/operations revisions 生成并验证
   `inference-workgraph-v1.yaml`，
   确认 DAG、owner、migration、rollback、legacy deletion 和每包 acceptance tests 完整；
17. 全部产物入库、contracts conformance/恢复模型全绿并经架构评审签字后，记录不可变
   `GATE_0_APPROVED`；此前 Workstream 2–8 不得启动生产实现。

Gate 0 的不可省略退出证据为：parity manifest digest、provider certification digest、canonical failure
schema revision、execution contracts revision、config schema/semantics digest、OpenAPI/AsyncAPI/RPC IDL
digest、admin RBAC digest、wire-fixture digest、private-network policy revision、shared-daemon contract
digest、Shared SQLite semantics/conformance digest、deployment/distribution scope digest、dependency-plan/SBOM
digest、translation/client-profile decoder-retention/catalog-provenance digest、reasoning-replay lookup/publication/
retention/erasure contract digest、routing-observation-snapshot/policy/decision/counterfactual-evaluation digest、
response-validator automata/resource-budget digest、Traffic Inspector contract digest、
quality-ratchet result digest、infrastructure-reuse audit digest、
daemon backup/application integration/RecoverySet schema digest、
backup barrier/permit epoch与restore-drill digest、evidence/proposal/owner-command reconciliation digest、
dual-plane observability contract digest、alert/readiness/runbook digest、release CLI/upgrade contract digest、workgraph digest、
SLO appendix digest、fault-matrix result digest
和签名 `GATE_0_APPROVED` record。record 同时绑定 generation/
permit/migration/artifact contract revisions；任一 digest 变化会使批准失效并触发 change-control。当前
这些执行产物尚未生成，本文档不能以文字清单代替它们。

### 21.2 全面实施获批后的 Workstream

以下是内部工作流，不是可独立发布的裁剪版本：

1. **核心不变量与基准**：attempt state、wire counter、deadline、deterministic simulation、当前性能；
2. **generation/client/transport**：单一 generation、credential handles、connector refs、自有 wire；
3. **queue/bulkhead/admission**：层级 DRR、aging、唯一 capacity permit、breaker、overload；
4. **journal/stream/recovery**：新状态 records、内外流 contract、receipt、crash reconciliation；
5. **governance/ledger/virtual key**：SQLite/PostgreSQL、tenant/project/team/customer、预算限流；
6. **全 provider/operation**：29 providers 与 20.2 全 operation matrix；
7. **HTTP/MCP/管理面/插件/UI**：全部兼容入口、catalog/control 与生态组件；
8. **cache/framework/deploy/HA**：四类缓存、全部 persistence/vector/object backends、
   Embedded/Shared Process、CLI/部署工件、daemon failover/drain；Cluster production/HA 延后；
9. **旧路径删除与总体验收**：删除所有 SDK wire/双轨/feature flag，执行 Release Gate。

只有 `GATE_0_APPROVED` 后，Workstream 2–8 中 `depends_on` 已满足的 ready nodes 才可并行，不允许
整个 Workstream 同时开工；共享 contracts 必须先由 owner 审批。Workstream 1 即上述 Gate 0，不与
生产功能实现并跑。主分支持续集成不等于对外发布；在 Release
Gate 之前不得宣称新网关为正式产品，也不保留多个长期产品版本。

### 21.3 Mote 实现 SLO Freeze Gate

量化性能目标不能由设计者脱离硬件、payload 和 provider 延迟凭空填写，也不建立 Bifrost/Go 对比
基线。Workstream 2–8 可以按冻结 contracts 开始实现；当 Mote Python 数据面、receipt、签名、journal
和 Shared RPC 形成可测闭环后，在固定硬件、资源配额、fake provider 延迟分布、payload/stream 分布和
真实 provider 样本下冻结版本化 `zdocs/parity/inference-slo-v1.md`。SLO 未冻结前不得通过最终 Release
Gate，也不得把“尽可能快”作为验收标准。

附录必须给出数值、测试环境、置信区间、样本数、回归容差和以下 hard gate：gateway dispatch
p99/p99.9、admission p99、最大 event-loop lag、多租户 fairness deviation、Shared daemon 多进程复用效率、
reload connector 峰值、空闲及稳态 RSS、允许 gateway-attributable error rate、24/72 小时 soak 的 RSS/
task/socket/spool 最大增长率，以及 BackupSet RPO/RTO、backup/restore-drill成功率与频率、
reconciliation backlog/oldest-age/resolve-latency、outbox/publication backlog age和daemon crash-loop上限。
provider latency 必须与 gateway overhead 分开。Release Gate 只引用该
固定 revision 的确切数值；修改阈值等同架构变更，需要重新评审，不能为通过测试临时放宽。

采用完整 Mote Python 实现实测后再冻结，而非此处臆造数字，是为了让 SLO 可判定、
覆盖 receipt/signature/journal/RPC 的目标架构开销，又不把某台开发机的偶然值固化为十年契约；
SLO Gate 是实施前门槛，不是可延后的产品功能。

### 文件级核心改动

新增至少包括：

- `contracts/inference/{request,attempt,operation,session,transfer,wire_permit,generation_artifact,admission,
  identity,events,plugins}.py`
- `contracts/ports/inference/{inference_runtime,session_runtime,provider_transport,connection_pool,
  credential_binding,inference_cache,usage_ledger,provider_quota,credential_health,
  attempt_receipt}.py`
- `contracts/ports/service/command_runtime.py`；
- `contracts/ports/artifact/provider_transfer.py`（仅在复用审计证明现有 artifact port 不足时新增）；同理，
  `ArtifactLookupIndex`只可在审计证明metadata query不足后作为通用artifact port新增；
- `runtime/inference/{data_plane,runtime,fair_queue,dispatcher,bulkhead,streaming,plugins,cache}.py`；artifact
  继续使用 `runtime/artifacts/`，不得新增 inference store/catalog/GC；
- `product/models/transports/` 全 wire families 与 provider adapters；
- `product/models/transports/connections/` 的 aiohttp/httpx/WebSocket/EventStream/WebRTC pool 实现；
- `product/inference/backends/{sqlite,postgres}.py`；
- `product/interfaces/inference_api/`、Shared Process gRPC daemon/adapter；Cluster adapter 延后；
- `product/interfaces/inference_admin/` 与 UI assets；
- `ztest/inference/`、compatibility fixtures、chaos/soak/performance suites。

修改现有 `contracts/model/failover.py::FailureDisposition`、`runtime/models/model_gateway.py`、
`runtime/service_gateway/`、`runtime/artifacts/` 的必要 contract extension、failover journal/contracts、
`product/models/`、`product/composition/`、media/web-search/MCP 与现有 config composition；不得在函数内用局部 import 规避
循环依赖。

---

## 22. 一次性 Release Gate

正式发布只有一个 Gate，以下条件必须同时成立：

### 功能与 parity

- 29 provider manifest 与冻结 Bifrost baseline 一致；
- Gate 0 approval record 中 parity、provider-certification、failure/contracts、workgraph、SLO 与 fault-matrix
  digest 均匹配发布源码和测试工件；
- 所有 current-scope supported/conditional/provider-managed cell 完成 offline、recorded 与适用 live
  certification，账号缺失或 provider outage 不构成豁免；future_cluster cell 不计入当前 Gate；
- parity manifest 中所有 `current_embedded`/`current_shared` 的 20.2 operation 与 20.3
  integration/plugin/management/UI/distribution item 通过；`future_cluster` 明确排除；
- Embedded、Shared Process 两种部署通过同一 conformance suite；同机多个 Mote 进程共享一个 daemon
  时不得重复创建 data plane、连接池或治理 authority；
- SQLite 与 PostgreSQL 通过相同领域 conformance suite：状态转换、幂等、reserve/settle、CAS、outbox
  和 crash recovery 一致；允许并发规模、锁实现、备份、HA 与性能指标不同；
- exact、semantic、prompt、HTTP cache contract 全部通过。
- strict provider的reasoning replay在客户端剥离、restart、expiry、tamper与generation切换下满足冻结契约，
  且不跨tenant/session/model、不静默截断、不暴露完整reasoning；conversation/tenant删除、legal hold、
  key destruction、index/blob/GC-root每个部分失败与审计导出满足Artifact publication/ownership/GC契约；没有
  reasoning私有数据库或清理器，通用`ArtifactLookupIndex`若存在必须有复用审计批准；
- translation profile逐字段和逐stream event通过round-trip/golden/property/mutation tests，catalog provenance、
  freshness、diff与generation activation可验证；client profile选择不依赖User-Agent猜测，未知/冲突revision
  严格降级或拒绝，decoder/encoder保留覆盖最大恢复窗口，resume不发生未记录切换；
- routing decision matrix覆盖硬过滤、reasoning budget、空候选、过期telemetry和dry-run，且不产生wire、
  retry/fallback或authority mutation；decision/failover plan绑定同一不可变snapshot，重新路由有新journal fact；
  counterfactual dataset满足白名单、tenant隔离、样本量/cohort/置信区间与阈值，override限时审批且不构成
  shadow traffic；

### 正确性与恢复

- 每个 parity cell 声明的 wire unit 实际 provider request/frame 数为 0 或 1；
- SDK、proxy、service mesh、transport 均无隐式 retry；
- 只有 journaled `WIRE_AUTHORIZED`/`WirePermit` 签发 claim wire budget；`SEND_COMMITTED` 只能消费
  permit；即使随后未观测到 `WIRE_STARTED` 也不得复用 ordinal；
- reload 不产生跨 generation 撕裂或连接峰值泄漏；
- usage/cost/budget reserve 与 settle 恰好一次；
- 所有外部副作用可追踪、恢复、取消或明确 IN_DOUBT；
- cache hit/failure 不污染 attempt/fallback；
- queued、active、stream 在 crash/shutdown 后都有确定状态。
- 每个 provider error fixture 唯一映射冻结的 canonical failure，并由各 authority 只消费自身 verdict；
- receipt 引用的 artifact 均已 durable/verified/publication committed，Shared caller/daemon 跨进程与 daemon
  restart 后可 resume，retention、GC、删除与 legal hold 符合第 18.5 节；不要求跨机器 Cluster；
- `GatewayDaemonBackup`、`MoteApplicationBackup` 与签名 `MoteRecoverySet` 分别通过适用的空目录和灾难
  恢复演练，准确声明 `APPLICATION_CONSISTENT`/`DAEMON_CONSISTENT`/`CRASH_CONSISTENT`；缺失 caller
  不得提升一致性等级，component/digest/key/artifact任一验证失败时 readiness保持 false；
- application-consistent barrier验证 `backup_epoch`/`admission_epoch`，daemon拒绝 barrier后的 stale permit；
- 所有 `IN_DOUBT` 均有 evidence acquisition与owner-action strategy；daemon只产出 evidence/proposal，
  logical owner才可追加 terminal和 settle。离线 owner保持 unresolved，禁止无 evidence或绕过 owner标记成功；
  proposal/owner-command幂等重放与 acknowledgement、reconciliation backlog/oldest age满足冻结 SLO；
- Shared caller/daemon各用现有进程内 EventBus并通过 trace与execution IDs关联；daemon不伪造 caller logical
  events，metrics/audit/alerts保留明确 source plane；
- provider字段拒绝不得触发隐藏的strip-and-retry；任何额外provider request都必须是logical owner授权、
  独立journal/ordinal/`WirePermit`的attempt；
- HTTP 2xx/任意stream bytes不等于成功；每个provider profile的pre-commit response validator必须拒绝
  error-in-200、HTML、畸形envelope与不完整stream，同时接受契约允许的empty/tool-only/keepalive；逐operation
  automaton及byte/frame/time/precommit上限通过，usage/health verdict不越权；

### HA 与安全

- 单 provider hang 不影响其他 bulkhead；
- dispatcher、worker、subscriber、ledger、cache、credential store 故障符合第 19 节；
- PostgreSQL、Redis/vector backend 短暂不可用行为经过 chaos test；
- rolling upgrade、rollback、无损 drain 通过；
- readiness 按冻结矩阵准确反映 generation、scheduler、receipt/outbox、ledger、credential/artifact store、
  connection pool、audit policy、migration和disk，并正确执行局部/全局 fail-open/closed；
- SSRF、DNS rebinding、redirect 重验、egress allowlist、proxy/TLS trust policy通过；
- admin/inference listener 隔离；virtual key 安全存储与轮换通过；
- body/header/schema/multipart/chunk/bytes/spool/task/connection 全部有硬上限；
- audit payload encryption、TTL、RBAC、region policy通过；
- secret 不出现在日志、trace、exception、journal、metrics 或 diagnostic dump。
- trusted/untrusted 插件等级可见；WASM/隔离进程 escape、资源耗尽、crash 与 secret capability 测试通过。
- alert catalog 每项可触发/去重/恢复并链接已演练 runbook；gateway validate/migrate/backup/restore/doctor/
  reconcile/drain/upgrade-status 命令的结构化输出、退出码与审计通过验收。
- Traffic Inspector只读、默认脱敏且不能重放副作用；无MITM CA、TLS/browser fingerprint spoofing、系统代理
  解密或绕过RBAC的诊断路径；

### 性能与稳定性

- 1/10/100/1000/10000 并发基准完整；
- 报告 RPS、TTFT、p50/p95/p99/p99.9、RSS、CPU、socket、task、queue wait、reject；
- reload 连接峰值有界；慢 consumer 与超长 stream 有界；
- 多租户公平性、aging、大请求不饥饿通过 deterministic 和压力测试；
- dispatch/admission/event-loop lag、fairness、Shared 多进程复用、reload、RSS、错误率全部达到冻结的
  `inference-slo-v1` 数值；
- 24/72 小时 soak 的 task、connector、credential handle、queue node、spool 和内存增长率不超过
  同一冻结附录阈值。
- 性能历史退化阈值、fixture freshness、manifest completeness、IDL breaking-change和mutation score棘轮
  全部通过；coverage百分比不能抵消任一协议或状态机失败。

### 代码清理

- 所有旧调用路径和迁移 flag 已删除；
- architecture/import tests 继续满足分层；
- 没有 TODO、兼容 re-export、重复 registry、第二套路由/fallback/budget/tool loop；
- 文档、配置 schema、管理 API 与发布工件完整。

一次 Gate 的原因是部分能力先发布会迫使旧/new provider、budget、routing 或 lifecycle 长期共存，
正好制造本项目要消除的双重语义。内部增量研发仍是必要工程手段，但不能转化为长期双轨产品。

---

## 23. 正式发布前必须删除的旧路径

- `ProductModelEndpointResolver.resolve()` 内每次 `providers.create()`；
- `ModelEndpointAdapter`、`ProductModelEndpointAdapter` 与 `execute_once()` executable seam，以
  `ResolvedEndpointBinding` port 取代；
- `BaseLLM` 及 provider SDK 的生产 wire execution path；
- provider class 私有且不可共享的 client lifecycle；
- 所有 SDK/proxy/transport 默认 retry；
- media provider 每请求创建 `aiohttp.ClientSession`；
- 绕过 shared admission、ledger 或 generation 直接调用 provider 的路径；
- 重复 provider/brand/model catalog registry；
- 旧 `ResourceAdmissionController` 中已迁移的容量/quota/breaker/cooldown authority；
- HTTP/Shared/Cluster adapter 自己的 routing、retry、fallback、budget 或 provider client；
- migration feature flags、legacy aliases、兼容 re-export 和双写 journal。

研发分支可以短期存在适配层，但每一层有 owner 和删除 Workstream；Release Gate 检查仓库中已无
上述代码。正式版本不提供 old/new 开关。

---

## 24. 风险与已批准取舍

1. **范围巨大**：接受全量一次发布；用冻结 commit 和并行 Workstream 让集合可关闭，不做功能裁剪。
2. **Python event loop 上限**：当前交付 Embedded 与进程共享 gRPC daemon；可通过多个独立 Shared
   daemon 分片但不承诺分布式协调，Cluster 横向扩展与 HA 延后，不改用 Go。
3. **自有 transport 成本**：接受，以换取严格 WIRE_STARTED、无隐藏 retry 和长期协议控制权。
4. **公平队列复杂性**：接受层级 DRR + aging，使用 virtual clock/property tests，不用 Semaphore 冒充。
5. **缓存风险**：四类缓存分 owner，tenant 默认隔离，tool/realtime/sensitive 默认 bypass。
6. **插件安全**：完整迁移行为，不复制 mutable context；进程内 Python 明确为 trusted code，不可信
   代码只走 WASM 或 OS 隔离子进程，避免把对象封装误称为 sandbox。
7. **durable hard truth**：SQLite 可用于 Embedded 或单 active Shared daemon；Shared 多 daemon和未来
   Cluster 使用 PostgreSQL ledger/lease authority；Redis 只做可丢缓存，不承担 hard budget。
8. **生态 parity**：复制用户能力而非 Go ABI；Python diagnostics 等价替代 pprof。
9. **OmniRoute能力取舍**：吸收reasoning continuity、typed routing目标、translation profiles、catalog
   provenance、无副作用模拟器、只读诊断与质量棘轮；不迁入TLS/browser fingerprint spoofing、CLI身份伪装、
   tool cloaking、MITM/system proxy、网关内prompt/tool-result压缩、独立fallback/circuit-breaker/model lockout。
   prompt压缩继续由ContextManager和工具结果体系拥有，failover/health继续由Mote唯一owner拥有。
10. **Shadow routing**：当前禁止。它天然产生第二次wire调用；未来若批准，只能作为独立execution taxonomy，
    有独立principal、budget、journal、WirePermit、数据隔离、用户授权与不可影响主请求的result contract。
11. **Redis边界**：继续只承载可丢cache/加速投影，不承担hard quota、ledger、receipt或replay artifact真相。

---

## 25. 源码依据

### Mote

- `runtime/models/model_gateway.py`
- `runtime/models/failover/orchestrator.py`
- `runtime/resilience/admission.py`
- `runtime/events/stream.py`
- `product/models/{bootstrap,gateway,endpoint}.py`
- `product/models/providers/`
- `product/composition/{container,lifecycle}.py`
- `product/media_generation/providers/openai.py`
- `product/interfaces/agui/server.py`

### Bifrost

- `/home/longert/run_rollout/bifrost/core/bifrost.go`
- `/home/longert/run_rollout/bifrost/core/schemas/{provider,plugin}.go`
- `/home/longert/run_rollout/bifrost/core/keyselectors/weightedrandom.go`
- `/home/longert/run_rollout/bifrost/framework/streaming/`
- `/home/longert/run_rollout/bifrost/framework/`
- `/home/longert/run_rollout/bifrost/plugins/{governance,logging,telemetry,otel,semanticcache}/`
- `/home/longert/run_rollout/bifrost/transports/bifrost-http/`
- `/home/longert/run_rollout/bifrost/{cli,ui,community,docs,examples,helm-charts,terraform,recipes,nix,npx}/`

### OmniRoute（能力对照，不作为生产依赖）

- `/home/longert/run_rollout/OmniRoute/docs/routing/{REASONING_REPLAY,AUTO-COMBO,REASONING_ROUTING}.md`
- `/home/longert/run_rollout/OmniRoute/open-sse/services/{reasoningCache,autoCombo/scoring,autoCombo/taskFitness}.ts`
- `/home/longert/run_rollout/OmniRoute/open-sse/services/combo/validateQuality.ts`
- `/home/longert/run_rollout/OmniRoute/open-sse/translator/`
- `/home/longert/run_rollout/OmniRoute/src/lib/db/reasoningCache.ts`
- `/home/longert/run_rollout/OmniRoute/src/mitm/inspector/` 与
  `/home/longert/run_rollout/OmniRoute/tests/integration/traffic-inspector-*.test.ts`
- `/home/longert/run_rollout/OmniRoute/scripts/quality/` 与
  `/home/longert/run_rollout/OmniRoute/tests/unit/correctness/`

这些源码证明需求与故障形态，不授权复制其OpenAI-first内部模型、SQLite私有cache、隐式字段降级重试、
MITM/fingerprint/identity spoofing、fallback或quota authority。

本设计迁移这些实现中的能力，不在 Python 代码中依赖 Bifrost Go 类型、进程或配置格式。

---

## 26. 最终实施建议

研发只能从第 21 节 Gate 0 开始：先生成 parity/certification inventory 与 execution taxonomy，再冻结
canonical failure 和核心 contracts，建立 reference fault model；在 Mote 实现形成闭环后实测并冻结 overhead budget、
冻结 SLO，最后生成绑定这些 revision 的 workgraph。核心 contracts 必须覆盖四类 execution lifecycle、
journal/receipt、`WirePermit`、四种 resource identity、`GatewayDataPlane`、artifact durability、
`TransportConnectionPool` port、multi-resource lifecycle、`GatewayRuntimeGeneration`/`GenerationArtifact`、
deadline、schema migration 和 credential handle；生产契约还必须冻结 daemon/application backup域、
`MoteRecoverySet` 与 barrier epoch，evidence/proposal/owner-command reconciliation、Shared双观测面、
reasoning replay、translation/response validation profiles、routing decision trace/simulator、只读诊断、
quality ratchet、readiness/alerts/runbooks和upgrade CLI。`GATE_0_APPROVED` 前不启动
Workstream 2–8；获批后也只能调度 workgraph 中 dependency-ready 且通过基础设施复用审计的节点。
实现优先扩展 RuntimeModelGateway、RuntimeServiceGateway、Artifact、Lifecycle、Product Config、EventBus
和现有 journal/admission，不得用 inference 命名空间复制这些基础件。

当前范围内所有 Workstream 都是同一发布的一部分；HTTP、UI、virtual key、cache、全 provider/operation
与 Embedded/Shared Process 一个也不能留到发布后。Cluster production/HA 是明确批准的唯一延期项，
Gate 0 只为其保留 ID namespace、version envelope、receipt/generation digest、RPC naming 与 migration
extension point，不冻结 quorum/placement/HA 行为。最终只在第 22 节
Release Gate 全绿且第 23 节旧路径清零后发布。
