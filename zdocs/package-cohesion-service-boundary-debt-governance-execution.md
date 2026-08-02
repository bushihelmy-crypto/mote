# Package Cohesion 与 Service Boundary 债务治理执行规格

状态：唯一实施入口；批准按 fixed slice 与核心总账实时前置实施；R0.3/X3 在 ADR-D4 确认前仅允许取证，EVIDENCE_ONLY、NEEDS_EVIDENCE、DECISION_REQUIRED 或上游未 DONE 的切片继续阻断

日期：2026-08-01
范围：`contracts/`、`kernel/`、`runtime/`、`orchestration/`、`product/`、`ztest/`、`zdocs/`

## 1. 权威关系、范围与产品决定

事实优先级：当前用户决定 → `AGENTS.md` → 当前源码与可执行门禁 → 核心总账 → 专项需求 → package audit。`zdocs/core-architecture-debt-closure-requirements.md` 是唯一编号和完成状态 owner；本文件只规定依赖、切片与验收，不维护第二状态账本。review 已冻结为证据和反例，不是实施者必须拼接阅读的另一半规格。

硬边界为 `contracts <- kernel <- runtime <- orchestration <- product`。每个概念只有一个 canonical owner/type/state chain/composition entry；每个切片同批迁移全部消费者、删除旧入口并增加 gate，不存在 G4 公共面大扫除或 G5 后置清理。

已确认产品决定：

1. Product 是唯一 composition root；CLI/gateway/SDK/test 只作 adapter，不复制 object graph。
2. logical Agent 不等于进程；采用 supervisor/control plane 与有界 worker pool。
3. WorkflowRun 是跨进程 durable execution；所有 backend 满足相同最低 guarantee，Temporal/JSONL 显式选择失败均 fail closed，不达标的 JSONL 不得承载 WorkflowRun。
4. 每个 Agent/Role 独立拥有一个 process-local BackgroundTaskPool。按用户已确认决定，模型可见 TaskId 可在同 Agent/Pool/进程内 retry/resubmit，每次产生单调 AttemptId；旧 attempt 失去提交权，进程丢失后不可接管或自动重放。第十四轮 review 的“一个 TaskId 只执行一次”建议因与用户决定冲突而拒绝吸收。
5. worker crash 只 durable 记录 incarnation-loss；不建立逐 BackgroundTask 跨进程 registry。已提交 terminal fact 可重投影，否则返回 owner-lost/unknown。
6. Environment 旧 facade 无仓外 SDK 承诺；迁移仓内消费者后删除 `AgentEnvironment`、`BaseEnvironment`、`MoteEnv`，不留兼容面。
7. Session、Residency、Workflow、Cron、ErrorCode 旧持久数据获授权从零开始；只对精确旧 record 执行 `AUTHORIZED_DISCARD`，不实现旧 decoder/upcaster/双读。Artifact、workspace、secret、Agent lineage/delivery 和仓外 wire 不在授权内。
8. logical Agent 只有在 children、delivery、budget、effect、BackgroundTask 全部结算并 durable commit terminal/tombstone 后，才严格一次释放 active logical cap；AgentId 永不复用，tombstone 按 retention/pin/legal-hold 独立保留。
9. best effort 仅用于可从 durable canonical facts 无 LLM、费用、时变调用和副作用确定性重建的信息；wake/cache/telemetry 可丢，但不能成为唯一推进机制。
10. Session read model 归 `runtime/session`；Artifact projection pipeline 保持共享。Hook folding 保持内聚但外部命令复用 governed runner。FileOps 复用 canonical Artifact content storage。CodeMap/LSP 分包；Squilla 不新建伪 service；Agent OutputT 端到端保留。
11. Agent turn scheduler采用分层WDRR：当前没有独立Agent tenant identity，故 `tenant == root governance owner`；root间WDRR，持续积压的兄弟subtree使用第二级WDRR，turn cost固定1。weight为Product schema约束的有界正整数且默认1，extension不能提高；priority仅在root/subtree内生效，同priority按durable enqueue sequence FIFO。
12. deadline只CAS终止未claim item；deadline/cancel/claim只有一个赢家。capacity admission发生在durable accept前，accepted item不驱逐。claim绑定queue revision、scheduler fence与R2.16 permit；retry有`next_eligible_at`和terminal disposition；config generation只影响下一次未claim决定。

ADR-D5 产品阻断与复用审计均已闭合，R2.50 为 `CONFIRMED`：复用 Inference fair queue 的纯算法不变量与确定性测试模型，拒绝复用其 process-local tenant/project concrete queue；Agent durable queue复用R2.16 permit、R2.20 mailbox identity、R2.29 lease与R2.46 clock mechanism，不建立万能queue或第二mailbox。

## 2. 38 项 SB disposition

Disposition 仅为 `IMPLEMENT`、`MERGED_INTO`、`REJECTED`、`EVIDENCE_ONLY`。它不表示进度；状态只看核心总账。

| SB | disposition | 唯一 ledger owner / slice | 边界 |
| --- | --- | --- | --- |
| SB0.1 | MERGED_INTO | R2.1/R2.47/R2.48 · WF1–WF3 | Workflow definition、run、recovery、effect/terminal |
| SB0.2 | MERGED_INTO | R2.28/R2.50–R2.53/R1.20/R1.13 · AG1–AG6 | lineage、scheduler、三类 cap、budget、cancel、residency、delivery |
| SB0.3 | IMPLEMENT | R0.8/R0.9 · X0/D1 | backend guarantee 与 operation fencing |
| SB0.4 | MERGED_INTO | R0.8/R2.30/R1.7/R0.3/R0.6/R2.36 · X0–X3/H1/T-TOOL | EffectId、typed runner/helper、Hook control、Tool chokepoint；`evidence_dependency=EV-WRITE-CLASSIFICATION`仅阻断尚未分类的workspace write consumer，不阻断其他已确认owner |
| SB0.5 | MERGED_INTO | R2.19–R2.21/R2.24–R2.25/R2.29/R2.31–R2.33/R2.41/R1.11–R1.12/R1.23–R1.24/R2.46 · C-RES/C-MAIL/AU3/C-OUTPUT/E1/C-LEASE/C-FILESEARCH/C-CHECKPOINT/C-SECRET/T-GEN/AU1–AU2/GC1–GC2 | strict schema分别归domain owner；禁止万能codec/transaction/clock owner |
| SB1.1 | IMPLEMENT | R1.1 · P3 | typed resource lease |
| SB1.2 | EVIDENCE_ONLY | R2.10 · EV-SERVICES | Wiring/Services scope与consumer，禁止换名 locator |
| SB1.3 | MERGED_INTO | R2.42 · P1/P2 | pure construct、async activate、唯一 factory |
| SB1.4 | MERGED_INTO | R2.11/R2.42 · P4 | 全 host/test 复用 composition |
| SB1.5 | MERGED_INTO | R2.4/R2.10/R2.15 · T-AGENT-CONTROL/T-AGENT-WIRING/T-AGENT-GENERICS | ambient control、wiring projection、spawn contract与OutputT端到端 |
| SB1.6 | MERGED_INTO | R2.2/R2.7/R2.14 · T-KERNEL-POLICY/T-INFERENCE-REQUEST/T-KERNEL-ASSEMBLY | node input、inference operation/request、graph assembly分owner类型化 |
| SB1.7 | MERGED_INTO | R2.5 · BC-CODEMAP | CodeMap typed面；`evidence_dependency=EV-LSP`仅约束LSP ingestion/query/context-source，不阻断CodeMap切片 |
| SB1.8 | MERGED_INTO | R2.25/R2.43/R2.44 · E1–E3 | SessionFact、subscriber、typed observation |
| SB1.9 | MERGED_INTO | R0.6/R0.2/R1.9/R2.7/R2.8 · H1/T-MODEL/T-TRUST/T-INFERENCE-REQUEST/T-SKILL | governed Hook、model、extension trust、inference request、Skill contracts |
| SB1.10 | MERGED_INTO | R2.3/R2.41 · T-QUEUE/T-GEN | queue/generation类型化；`evidence_dependency=EV-DAEMON`确认双owner后必须绑定R2.26或最新owner，不得在证据卡直接编码 |
| SB1.11 | MERGED_INTO | R2.36/R2.15/R2.40 · T-TOOL/T-AGENT-GENERICS/T-TOOL-ID | Tool lifecycle/chokepoint、spawn/control泛型、definition/catalog generation |
| SB1.12 | EVIDENCE_ONLY | EV-PROVIDER | 未证明包外mutation不建registry service |
| SB1.13 | IMPLEMENT | R1.2/R2.11 · DEL-ENV | 删除无外部承诺 Environment facade |
| SB1.14 | IMPLEMENT | R1.26/R1.4/R1.5 · BG1 | Agent-owned pool、deferred result/status identity、pin/drain、TaskId+AttemptId；subtree cancellation由SB0.2的R2.53/BG2承接 |
| SB1.15 | IMPLEMENT | R3.1 · BC-ERROR | authoritative error owner |
| SB1.16 | EVIDENCE_ONLY | EV-PRESENTATION | cross-host DTO consumer先取证 |
| SB1.17 | MERGED_INTO | R2.12/R2.40 · BC-CONFIG/T-TOOL-ID | Product defaults 与 Runtime mechanism 分治 |
| SB1.18 | IMPLEMENT | R3.6 · BC-ERROR-WIRE | serialized inventory、strict envelope、授权丢弃旧数据 |
| SB2.1 | EVIDENCE_ONLY | EV-FILEOPS | public command/query consumer matrix |
| SB2.2 | IMPLEMENT | R2.23 · BC-ARTIFACT-NAME | 两个不同 repository 精确命名，不误合并 |
| SB2.3 | MERGED_INTO | R2.1/R2.47 · WF1/WF2 | Workflow public surface随owner收口 |
| SB2.4 | EVIDENCE_ONLY | EV-SERVICE-GATEWAY | composition input 与业务泄漏分型 |
| SB2.5 | MERGED_INTO | R2.5 · BC-CODEMAP | CodeMap query/index最小面 |
| SB2.6 | IMPLEMENT | R2.43 · E2 | Session read model、replay/live reducer与durable subscriber迁入 `runtime/session`；R2.25只提供SessionEvent catalog |
| SB2.7 | IMPLEMENT | R2.12 · BC-CONFIG | 配置按domain/consumer归位 |
| SB2.8 | MERGED_INTO | R2.12/R1.14/R2.49 · BC-CONFIG/MCP-RELOAD/RELOAD | 唯一 watcher、MCP candidate swap、Product reload与generation swap |
| SB2.9 | EVIDENCE_ONLY | EV-ELISION；R3.4/MIGRATION-RESIDUE仅清残渣 | wording明确；elision owner待证据 |
| SB2.10 | IMPLEMENT | R3.5 · SQ1 | Squilla owner内typed seam，无伪service |
| SB2.11 | EVIDENCE_ONLY | EV-PRESENTATION-DISPATCH | wire adapter/内部union consumer取证 |
| SB2.12 | MERGED_INTO | R3.2 · H0/PACKAGE-FACADE | optional eager import进H0，其余已确认facade逐项迁移删除 |
| SB2.13 | IMPLEMENT | R3.5 · BC-PRIVATE | private consumer按owner或最小seam迁移 |
| SB2.14 | IMPLEMENT | R3.5 · H0 | hermetic collection；撤销旧local-import误报 |
| SB2.15 | EVIDENCE_ONLY | EV-PUBLIC-PER-OWNER | 只随真实owner变更，不做公共面大扫除 |

`EVIDENCE_ONLY` 不得编码。裁决只能更新为 IMPLEMENT、MERGED_INTO 或 REJECTED，并与核心总账和本表同片更新。

### 2.1 SB invariant 反向闭包矩阵

本矩阵防止“编号存在但 owner 缺失”。`EVIDENCE_ONLY` 行的 consumer/delete-gate 是取证目标，不是编码授权；其余行必须与第5节固定 contract 同步。

| SB invariant | authoritative R owner → fixed slice | production consumer | delete / negative gate |
| --- | --- | --- | --- |
| SB0.1 Workflow唯一durable链 | R2.1/R2.47/R2.48 → WF1/WF2/WF3 | Product workflow tools、Session resume | 第二run/continuation/effect owner；唯一runner/store gate |
| SB0.2 Agent治理全链 | R2.28/R2.50–R2.53/R1.20/R1.13 → AG1–AG6/BG2 | Agent tools、hosting、messaging | memory lineage、混义cap、parked delivery；owner/fence gates |
| SB0.3 backend guarantee | R0.8/R0.9 → X0/D1 | Workflow/inference composition与WF2/WF3 consumers | fallback backend/无fence owner；activation guarantee与operation-owner negative gate |
| SB0.4 effect安全chokepoint | R0.8/R2.30/R1.7/R0.3/R0.6/R2.36 → X0–X3/H1/T-TOOL | Tool/Hook/BG/WF/media/helper | direct shell/write/executor；拒绝时调用次数为零 |
| SB0.5 durable domain闭包 | 每个domain schema R + R1.11/R1.12/R1.23/R1.24/R2.46 → C-*/AU/E1/T-GEN/GC | Session/Residency/Cron/Artifact/Workflow等各自消费者 | forgiving codec/PID lock/mtime GC；逐owner strict/fence/clock gates |
| SB1.1 resource lease | R1.1 → P3 | composition resource consumers | owned bool/borrower close；stale generation gate |
| SB1.2 services locator证据 | R2.10 → EV-SERVICES | Role/components/hosts | 换名locator禁止；consumer-capability evidence |
| SB1.3 composition lifecycle | R2.42 → P1/P2 | all Product hosts | CLI object graph/sync activation；constructor gate |
| SB1.4 host复用 | R2.11/R2.42 → P4/P2 | CLI/ACP/AGUI/SDK/tests | 重复factory；callsite allowlist |
| SB1.5 Agent泛型链 | R2.4/R2.10/R2.15 → T-AGENT-* | spawn tools/Runtime/Handle | ambient Any/wide wiring/cast；end-to-end Pyright |
| SB1.6 Kernel collaborator链 | R2.2/R2.7/R2.14 → T-KERNEL-*/T-INFERENCE-REQUEST | execution nodes/Runtime inference | duplicate policy/request Any/bundle；Pyright fixtures |
| SB1.7 CodeMap/LSP | R2.5 → BC-CODEMAP；EV-LSP | context/query/index/LSP hosts | manager合并禁止；各consumer matrix |
| SB1.8 facts与events | R2.25/R2.43/R2.44 → E1/E2/E3 | Session producers/subscribers/telemetry | object sink/bus；strict replay/type gates |
| SB1.9 Hook/model/trust/Skill | R0.6/R0.2/R1.9/R2.7/R2.8 → H1/T-* | Hook/Model/Skill/extension consumers | shell Hook/legacy model/discovery activation；trust/type gates |
| SB1.10 queue/generation/daemon | R2.3/R2.41 → T-QUEUE/T-GEN；EV-DAEMON→R2.26 | inference queue/stage/daemon composition | payload Any/dict binding；daemon证据不可直接编码 |
| SB1.11 Tool public控制链 | R2.36/R2.15/R2.40 → T-TOOL/T-AGENT-GENERICS/T-TOOL-ID | ToolExecutor/Agent spawn/ACP-AGUI | live catalog/弱identity/第二catalog；chokepoint gate |
| SB1.12 provider registry证据 | EV-PROVIDER → 最新domain owner | Model/Web/Media lookup/reload | 未证明外部mutation不建service；snapshot evidence |
| SB1.13 Environment删除 | R1.2/R2.11 → DEL-ENV/P4 | routing/human/hosting | 三个旧facade/export/tests；旧symbol negative gate |
| SB1.14 BackgroundTask边界 | R1.26/R1.4/R1.5 → BG1 | Role/Residency/tool | shared pool/Workflow resume/snapshot pin；drain/attempt gates |
| SB1.15 error owner | R3.1 → BC-ERROR | domain error consumers | runtime.errors re-export；authoritative import gate |
| SB1.16 presentation DTO证据 | EV-PRESENTATION → R2.22候选 | ACP/AGUI/Terminal/Textual | 未取证不扩Contracts；wire consumer inventory |
| SB1.17 config/tool identity | R2.12/R2.40 → BC-CONFIG/T-TOOL-ID | Product config/Runtime activation/Tool catalog | defaults下沉/宽config/identity漂移；schema-digest gates |
| SB1.18 serialized error | R3.6/R3.1 → BC-ERROR-WIRE/BC-ERROR | journal/event/wire/snapshot | 旧decoder/全局ErrorCode；strict envelope/inventory |
| SB2.1 FileOps surface证据 | EV-FILEOPS → R3.2候选 | cross-package FileOps consumers | mutable internals不得公开；command/query matrix |
| SB2.2 Artifact命名 | R2.23 → BC-ARTIFACT-NAME | FileOps transaction/Artifact consumers | 同名repository/第三facade；authoritative import gate |
| SB2.3 Workflow surface | R2.1/R2.47 → WF1/WF2 | authoring/Product/inspection | parallel API/private state；public allowlist |
| SB2.4 ServiceGateway证据 | EV-SERVICE-GATEWAY → R1.6/R2.6 | composition/media/search | backend record泄漏/无状态facade；consumer evidence |
| SB2.5 CodeMap public面 | R2.5 → BC-CODEMAP | query/index/context | store/extractor外泄；public allowlist |
| SB2.6 Session read model | R2.43 → E2 | replay/live、Agent component/key/accessor、Product governance | runtime.projections.session module/export/component identity；通用Artifact projection不得拥有Session state |
| SB2.7 config owner | R2.12 → BC-CONFIG | domain declaration/load/activation | runtime config grab-bag；layer/schema gates |
| SB2.8 watcher/reload | R2.12/R1.14/R2.49 → BC-CONFIG/MCP-RELOAD/RELOAD | Product reload/MCP/catalog | duplicate watcher/in-place mutate；generation gates |
| SB2.9 wording/elision证据 | EV-ELISION/R3.4 → evidence/MIGRATION-RESIDUE | Product presenter/tool output/context budget | 错owner wording与确认残渣；elision consumer/lifecycle inventory |
| SB2.10 Squilla seam | R3.5 → SQ1 | Squilla internal consumers | private cross-package import/伪service；typed seam gate |
| SB2.11 presentation dispatch证据 | EV-PRESENTATION-DISPATCH → R2.22 | surfaces/wire adapters | global string/getattr dispatch；consumer inventory |
| SB2.12 package import | R3.2 → H0/PACKAGE-FACADE | package-root consumers | eager optional/re-export；hermetic/public gates |
| SB2.13 private imports | R3.5 → BC-PRIVATE | each direct consumer | private cross-owner import；AST/import gate |
| SB2.14 collection purity | R3.5 → H0 | architecture test collection | optional eager import/真实local import；hermetic gate |
| SB2.15 per-owner public证据 | EV-PUBLIC-PER-OWNER | changed bounded-context consumers | 横向大扫除禁止；per-owner allowlist evidence |

## 3. Canonical scope/guarantee matrix

| capability | layer / scope / identity | lifecycle owner | durability / guarantee | production entry | 删除旧 owner |
| --- | --- | --- | --- | --- | --- |
| Application composition | Product；application/generation | Product Application | pure construct、ordered activate、reverse shutdown | `product/composition/` typed factory | CLI/gateway/SDK/test object graph |
| Agent lineage/spawn | Orchestration；root/tree/AgentId/SpawnRequestId | lineage/spawn saga | durable、幂等、worker前commit identity | typed spawn command/query | process registry/rollback closure |
| Agent scheduler | Orchestration；root/subtree/queue revision | Agent durable turn scheduler | WDRR、fenced claim/retry/settlement | typed enqueue/claim/settle | 无限park、Inference concrete queue、第二mailbox |
| Agent capacity | Orchestration；logical/resident/turn reservation | capacity projection owner | durable projection/CAS与typed receipts | reserve/settle/query | `max_agents`混义counter |
| Usage/budget | canonical UsageLedger + Product-selected fenced implementation；subject/dimension/revision | UsageLedger是真相owner；Orchestration只拥有Agent/root/subtree policy、subject projection与reserve/settle coordination | canonical BudgetReservation/UsageSettlement、SQLite fenced truth | canonical UsageLedger Port | 第二SQLite table/store、第二usage balance、复制Inference reservation state、telemetry truth |
| Incarnation/residency | Orchestration；Agent/incarnation/generation | placement/residency | strict record、CAS/fence、crash reconcile | materialize/rehydrate commands | disk选class、unfenced forget |
| Delivery | Orchestration；DeliveryId/target generation | delivery state machine | accepted前durable intent；fenced claim/ack/scan | send/query/cancel | parked queue、release drop |
| BackgroundTask | Orchestration；Agent/incarnation/TaskId/AttemptId | Agent-owned Pool | process-local；pin到settlement | submit/query/cancel/retry/release | shared pool、Workflow continuation、rebind |
| WorkflowRun | Orchestration；RunId/definition generation | Workflow run/reconciler | cross-process durable、all-mutation fencing | start/resume/query/cancel | Product continuation、第二engine |
| Automation | Orchestration；schedule/revision/occurrence | scheduler/reconciler | durable intent/settlement、CAS lease | schedule/claim/settle | PID lock、load-modify-save |
| Artifact/GC | Runtime；digest/ownership revision/pin generation | repository/collector | typed reachability、fenced snapshot/delete | publish/resolve/pin snapshot | 目录/mtime推断、第二storage |
| Session facts | Runtime；session/stream/revision | fact owner/subscriber | strict union、commit后projection/ack | append/read/subscribe | object sink、artifact projection拥有Session state |
| Effect/process | Runtime mechanism + Product policy；EffectId/attempt | governed executor | permission前零effect；intent/receipt/in-doubt | fixed-argv/shell/daemon typed seams | shell bool、direct spawn、Hook旁路 |
| Observation | Kernel seam + Runtime adapter；EventT/generation | emitting domain | best effort可drop，不驱动control/audit | typed emitter/binding | object bus、stale callback |
| Prompt/cache | Runtime context + Product generation；turn/source revision | prompt composition | same-turn generation一致；summary非truth | typed source/render/cache | 静态prompt易变内容、对象cache identity |

## 4. Dependency DAG

```text
H0 hermetic collection
 ├─ C0 canonical config/R0.0
 └─ X0 effect identity/R0.8 -> X1 runners

H0 + C0 -> P1 scope matrix/R2.42 -> P2 composition lifecycle -> P3 lease -> P4 host migration

C0/R0.0 -> T-SOURCE/R1.8
P1 + T-SOURCE/R1.8 -> T-TRUST/R1.9
T-INFERENCE-REQUEST/R2.7 + T-MODEL/R0.2 -> T-GEN/R2.41
C0/R0.0 + P1 + T-TRUST -> BC-CONFIG/R2.12

R1.4 + R1.5 + X1 -> BG1 pool-lifecycle/R1.26
BG1 + R2.19 + R2.20 + R2.27 + R2.46 -> AG5 residency/R1.20
AG5 + R2.16 -> AG3 capacity/R2.51
AG4 budget-coordination/R2.52
AG5 + R2.17 + AG3 + AG4 -> AG1 lineage-spawn/R2.28
R2.16 + R2.20 + R2.29 + R2.46 -> AG2 scheduler/R2.50
AG1 + AG5 + R2.20 -> AG6 delivery/R1.13
AG1 + AG2 + AG5 + BG1 + R2.16 -> BG2 subtree settlement/R2.53

R0.8 + R2.46 -> D1 operation ownership/R0.9
WF1 definition/R2.1 + D1 + R2.46 -> WF2 durable run+recovery/R2.47
WF2 + D1 + X0/X1 -> WF3 reconciliation/effect/terminal/R2.48

R2.46 -> AU3 Cron schema/R2.21
AU3 + R2.46 -> AU2 Cron transaction+fence/R1.12
AU2 + X0/X1 -> AU1 occurrence settlement/R1.11
C-OUTPUT/R2.24 -> GC2 Artifact reachability+pin/R1.24
AG5 + AG6 + BG1 + WF3 + AU1 + GC2 + C-RES/R2.19 + C-LEASE/R2.29 + R2.46 -> GC1 cleanup/R1.23

P1 + X1 -> E1 SessionFact/R2.25 -> E2 subscriber/R2.43
E1 -> E3 observation/R2.44
P1 + E1 -> E4 context/R2.8
E4 + R0.1 + R2.40 -> E5 prompt-cache/R2.45

X1 -> X2/R1.7
X0 + X1 -> X3/R0.3
X0 + X1 + T-TRUST/R1.9 -> H1/R0.6
X0 + X1 + T-TOOL-ID/R2.40 -> T-TOOL/R2.36
P1 -> T-AGENT-CONTROL/R2.4
P1 + T-AGENT-CONTROL -> T-AGENT-WIRING/R2.10
T-AGENT-CONTROL + T-AGENT-WIRING -> T-AGENT-GENERICS/R2.15
H0 -> T-KERNEL-POLICY/R2.2 -> T-INFERENCE-REQUEST/R2.7 -> T-MODEL/R0.2
T-KERNEL-POLICY + T-INFERENCE-REQUEST -> T-KERNEL-ASSEMBLY/R2.14
T-TRUST + E4 -> T-SKILL/R2.8
H0 -> T-QUEUE/R2.3
T-TRUST + T-GEN -> T-TOOL-ID/R2.40
R2.46 -> C-RES/R2.19
H0 -> C-MAIL/R2.20
R0.1 -> C-OUTPUT/R2.24
R2.46 -> C-LEASE/R2.29
R0.7 -> C-FILESEARCH/R2.31
H0 -> C-CHECKPOINT/R2.32
R1.15 -> C-SECRET/R2.33
P1 + P4 -> DEL-ENV/R1.2
R3.1 -> BC-ERROR-WIRE/R3.6
C0 + P1 + T-TRUST -> BC-CONFIG/R2.12
R1.9 + R1.16 + R2.40 -> MCP-RELOAD/R1.14
R2.42 + R1.9 + R1.14 + R2.35 + R2.40 -> RELOAD/R2.49
H0 -> PACKAGE-FACADE/R3.2
H0 -> SQ1/R3.5
H0 -> BC-PRIVATE/R3.5

independent fixed roots: AG4/R2.52, BC-ERROR/R3.1,
                         BC-ARTIFACT-NAME/R2.23, BC-CODEMAP/R2.5,
                         MIGRATION-RESIDUE/R3.4

external ledger prerequisites referenced as direct edges:
R0.1, R0.7, R1.4, R1.5, R1.15, R1.16, R2.16, R2.17,
R2.27, R2.35, R2.46
```

`independent fixed roots` 的 `requires` 为无；`external ledger prerequisites` 不是本文件切片，但其核心总账状态是对应入边的权威前置。每个 fixed slice 的 consumer migration、old-path deletion 和 gate 是该 slice 的组成部分，不是后继节点。全量 fault/security/architecture 验证是发布门禁，不是可替代直接依赖的汇总节点。只读取证可以并行；编码只有在 `requires` 全部 CONFIRMED/DONE 且不共享 authoritative contract、identity、state、wire、store、composition generation 或 consumer migration 时并行。文件不重叠不能证明独立。

## 5. Fixed slice contract 与受阻取证卡

每个切片必须填写并冻结下列实施卡；字段不得省略，也不得用“见旧稿”“同上”或自然语言伪依赖代替。下表是已批准 baseline，实施者在开始切片时把对应行展开为实施卡并随该切片证据提交，不另建状态账本。

```text
slice_id:
ledger_owner:
ledger_title_fingerprint:
ledger_status_at_start:
requires:                 # 直接硬前置；使用 canonical slice/R 编号
unblocks:
canonical_owner:
canonical_identity:
authoritative_state:
fence_or_revision:
production_entries:
production_consumers:
reuse_decision:           # REUSE / EXTEND_CANONICAL / NEW，并列搜索证据与拒绝项
scope:
lifecycle_owner:
durability_and_guarantees:
expected_files:
deletes:
acceptance_cases:
fault_and_race_cases:
tests_and_gates:
pyright_scope:
migration_or_discard_targets:
```

`ledger_title_fingerprint` 必须由核心总账当前标题生成；`ledger_status_at_start` 只作开工快照，状态仍只由核心总账拥有。`expected_files` 是审查边界，不授权覆盖用户已有改动。表中保留 `NEEDS_EVIDENCE/DECISION_REQUIRED` owner 的固定取证边界仅用于传播阻断，不构成编码准入；扩大范围先重审，不靠临时 seam。

| slice / ledger owner | requires | owner/identity/consumers/guarantee | deletes | tests/gates |
| --- | --- | --- | --- | --- |
| H0 / R3.5 | 无 | Product/package import与architecture collection | eager optional root import、真实非法import | hermetic collect/import、AST/layer/SCC negative fixtures |
| C0 / R0.0 | H0 | canonical config declaration/materialization/activation identity；Product composition consumer | `MoteConfig`断链、第二配置入口、construct期间extension activation | hermetic config construct、identity/activation fail-closed |
| P1 / R2.42 | H0/C0 | Product scope matrix消费canonical config identity；Application/resource identities | 凭名称决定singleton/scope、第二config入口 | config-to-scope identity、ownership/close-authority gate |
| P2 / R2.42 | P1 | Product Application消费同一canonical config；pure construct/ordered activate/reverse shutdown | CLI object graph、sync activation/配置旁路 | 逐activation fault、无half-active、单config入口 |
| P3 / R1.1 | P2 | typed lease identity/generation/holder；composition consumers | `owned: bool`、borrower close、stale release | double-close/transfer/stale generation + Pyright |
| P4 / R2.11 | P3 | CLI/gateway/daemon/SDK/session hosting/test consumer migration | 重复host factory/test production旁路 | constructor callsite与host integration gate |
| X0 / R0.8 | H0 | EffectId、RunJournal strict envelope、intent/receipt/in-doubt；全部effect producers | 无identity effect/audit分裂、宽journal decoder | commit fault、strict decode与effect次数 |
| D1 / R0.9 | R0.8/R2.46 | Contracts typed operation-ownership Port；local JSONL复用Runtime FileLease+strict RunJournal owner；Temporal使用workflow/run/activity attempt identity、history、visibility/query、stable typed activity handler与versioned serializable command/result DTO | `StepHandlerRegistry`生产跨主机路径、closure-based Temporal `run_step(execute=...)`、local RunJournal冒充Temporal canonical owner、Temporal失败回退JSONL、Runtime拥有Workflow状态机、Product复制operation owner | JSONL/FileLease与Temporal guarantee matrix；双进程/host claim、worker/lease loss、effect后receipt前crash、stale mutation、typed activity round-trip、activation fail-closed |
| X1 / R2.30 | X0 | governed-shell/fixed-argv/daemon seams；Tool/Hook/WF/BG | shell bool、cmd split、direct spawn | injection/deny/ask/timeout/zero-effect |
| X2 / R1.7 | X1 | USER/MANAGED helper argv与secret resolution | shell helper、secret stdout | source/argv/redaction negative tests |
| X3 / R0.3 | X0/X1；ADR-D4待确认 | media/workspace target transaction | overwrite/last-writer隐式策略 | 未确认前只取证，不编码 |
| H1 / R0.6 | X0/X1/R1.9 | Hook registration/matching/folding owner；control Hook复用governed runner并fail closed | Hook独立shell/direct spawn、异常折叠allow | deny/timeout/malformed/zero-effect与runner chokepoint |
| T-TOOL / R2.36 | X0/X1/R2.40 | ToolExecutor唯一lifecycle/chokepoint；Tool/ACP/AG-UI consumers | live catalog/control旁路、第二executor、弱invocation id | start/terminal identity、旧入口negative gate、Pyright |
| T-AGENT-CONTROL / R2.4 | P1 | Contracts AgentControlPort与typed ambient context；spawn/message consumers | `Any` ambient control、反射ctx | typed command/query与wrong-binding Pyright fixture |
| T-AGENT-WIRING / R2.10 | P1/T-AGENT-CONTROL | Agent wiring最小capability projection；Role/components | EngineServices locator、string/reflection lookup | consumer-capability矩阵、无完整Role/Context gate |
| T-AGENT-GENERICS / R2.15 | T-AGENT-CONTROL/T-AGENT-WIRING | SpawnRequest/Runtime/Handle/Outcome保持OutputT | text TypeGuard/cast、宽SpawnContext | definition→outcome端到端Pyright与text specialization |
| T-KERNEL-POLICY / R2.2 | H0 | consumer-owned completion Port；Kernel execution | 重复completion policy定义 | lifecycle区分、authoritative import gate |
| T-INFERENCE-REQUEST / R2.7 | T-KERNEL-POLICY | typed model/inference request贯穿Kernel→Runtime | payload Any、adapter中段擦除 | provider adapter strict decode + Pyright |
| T-KERNEL-ASSEMBLY / R2.14 | T-KERNEL-POLICY/T-INFERENCE-REQUEST | graph assembly最小typed collaborators | collaborator Any/object/万能bundle | node/assembly negative typing fixtures |
| T-SOURCE / R1.8 | C0 | canonical config source/path/ownership/content identity与trust projection | caller source enum提升trust、重复root加载 | path/source identity、ownership、untrusted source tests |
| T-TRUST / R1.9 | P1/T-SOURCE | Product extension provenance/trust/approval；Agent/Skill/Hook/MCP | discovery即activation、source enum冒充trust | untrusted checkout零activation/capability gate |
| T-MODEL / R0.2 | T-INFERENCE-REQUEST | canonical invocation/capability types与Product projection | legacy model seam、同名混义type | defining-module consumer migration + Pyright |
| T-SKILL / R2.8 | T-TRUST/E4 | Skill catalog/prompt consumer使用typed context source | Role/locator与宽payload | catalog/source consumer矩阵、suppression tests |
| T-QUEUE / R2.3 | H0 | inference/work queue payload泛型连续性 | QueueEntry payload Any/cast | enqueue→dispatcher Pyright/runtime contract |
| T-GEN / R2.41 | T-INFERENCE-REQUEST/T-MODEL | typed GenerationArtifact domain bindings消费canonical model contract并拥有自身versioned schema | dict[str,Any] binding、unknown variant接受、第二model binding | strict stage/restore/activate round-trip |
| T-TOOL-ID / R2.40 | T-TRUST/T-GEN | canonical Tool definition/catalog generation identity | inspect-source/import-order identity、平行catalog | digest/generation reload与consumer migration |
| C-RES / R2.19 | R2.46 | Residency strict record identity/revision/fence schema；AG5 consumer | forgiving defaults、磁盘class/backend字段 | strict shape/version/identity/corruption fixtures |
| C-MAIL / R2.20 | H0 | Mailbox strict payload/envelope identity；AG6/AG2/AG5 consumers | 裸list/dict、skip malformed message | strict union/version/round-trip/poison fixtures |
| C-OUTPUT / R2.24 | R0.1 | durable output candidate/accepted/committed schema；publication consumers | payload Any、第二result pointer | strict codec、commit/replay/Artifact retention |
| C-LEASE / R2.29 | R2.46 | FileLeaseCoordinator versioned fence state；D1/AG2/cleanup consumers | primitive coercion、corrupt reset | token monotonic/subject/version/corruption |
| C-FILESEARCH / R2.31 | R0.7 | File search durable row/skipped schema；search/replay consumers | `.get`和str/int/bool coercion | strict decoder/unknown field/version fixtures |
| C-CHECKPOINT / R2.32 | H0 | Runtime interactive checkpoint schema；Terminal/Browser/Canvas/Kernel | payload裸dict、忽略schema_version | driver strict restore/version/replay ordering |
| C-SECRET / R2.33 | R1.15 | Secret vault version/revision/CAS schema；credential consumers | corrupt置空、固定tmp、整文件丢更新 | concurrent section update/corruption/redaction |
| AG3 / R2.51 | R1.20/R2.16 | logical/resident/turn capacity projections与typed receipts | `max_agents`混义counter | 并发reserve/settle/rebuild/stale revision |
| AG4 / R2.52 | — | Orchestration只拥有Agent/root/subtree policy、subject/dimension projection与typed reserve/settle coordination；已确认复用canonical UsageLedger、BudgetReservation/UsageSettlement和Product-selected SQLite fenced implementation | 第二SQLite table/store、第二usage balance、复制Inference reservation state、telemetry truth | canonical ledger contract、并发reserve/settle/refund、lease loss、crash reconcile |
| AG1 / R2.28 | R1.20/R2.17/AG3/AG4 | lineage/spawn saga；AgentId/SpawnRequestId；Agent tools/hosting | memory registry、rollback原子性 | 每状态crash、dedupe、ABA、restart |
| AG2 / R2.50 | R2.16/R2.20/R2.29/R2.46 | durable turn queue；tenant==root；root+subtree WDRR；cost=1；持续capacity下每个持续eligible root/subtree在由active集合与bounded weight推导的有限轮次内claim；复用Inference fair queue纯算法/测试不变量及canonical permit/mailbox/lease/clock mechanism，拒绝concrete queue | 无限park、Inference concrete queue复制、accepted eviction、第二mailbox/万能queue | priority隔离/FIFO/CAS/scan/fence/config/retry与canonical clock门禁 |
| AG5 / R1.20 | R1.26/R2.19/R2.20/R2.27/R2.46 | incarnation/residency state与placement fence；AgentControl/hosting | unfenced runtime map/store/forget | eviction/rehydrate全fault点 |
| AG6 / R1.13 | AG1/AG5/R2.20 | DeliveryId/target generation；messaging/hosting consumers | parked accepted、release drop | accept/claim/process/ack/dead-letter crash矩阵 |
| BG1 / R1.26 | R1.4/R1.5/X1 | Agent-owned Pool lifecycle、pin/drain、TaskId/AttemptId；向Residency暴露窄typed Port | shared registry、Workflow resume、snapshot-only pin | drain barrier、Attempt fencing、owner-loss/release |
| BG2 / R2.53 | R1.20/R1.26/R2.16/R2.28/R2.50 | cancellation epoch、revisioned subtree、逐Agent settlement | supervisor读取Pool/runtime mutable state | spawn/cancel race、stale epoch、partial settlement |
| WF1 / R2.1 | H0 | immutable definition identity/versioned schema与compiler consumers | callable snapshot、随机第二identity | cross-process digest/unknown version |
| WF2 / R2.47 | R0.9/R2.1/R2.46 | RunId command/store、checkpoint/frontier、cancel/deadline/pause/resume | process-local run owner/continuation registry | R0.9已CONFIRMED；dual owner/restart/recovery/typed backend contract |
| WF3 / R2.48 | R0.9/R2.47/X0/X1 | reconciler、effect/terminal/delivery settlement | 第二scan/runner/effect registry | intent/effect/receipt/terminal/delivery fault矩阵 |
| AU3 / R2.21 | R2.46 | strict schedule/occurrence schema与store identity | forgiving decoder、skip corruption | corruption/quarantine/version/CAS |
| AU2 / R1.12 | AU3/R2.46 | Cron transaction、lease/fence owner | PID lock、load-modify-save、CLI旁路 | concurrent mutation/ABA/stale fence |
| AU1 / R1.11 | AU2/X0/X1 | occurrence intent与delivery/effect receipt | receipt丢弃、notification推进state | misfire/restart/unknown outcome/settlement |
| GC2 / R1.24 | C-OUTPUT | Artifact ownership edge/pin schema、pin generation、collector snapshot；逐项迁入Session/Workflow/BG/tool/model/FileOps/legal-hold producer | 私有map扫描、minimum-age证明reachability | producer接线清单、re-pin/stale collector/publication race |
| GC1 / R1.23 | AG5/AG6/BG1/WF3/AU1/GC2/C-RES/C-LEASE/R2.46 | cleanup deletion claim消费strict Residency/lease/pin/retention/hold facts | mtime/stamp、万能delete、宽lease record | active/pin/hold、partial delete、stale owner/fence |
| E1 / R2.25 | P1/X1 | SessionEvent catalog/schema、SessionFact strict union与append receipt | object sink/宽decoder | append failure、strict shape、single reducer |
| E2 / R2.43 | E1 | canonical `runtime/session` read model；replay/live共用authoritative reducer/state；durable subscription cursor/effect/ack；Agent component/key/accessor、Product governance与测试消费者 | best-effort唯一wake、ack-before-effect、`runtime.projections.session` module/export/component identity、第二reducer/registry | replay/live等价、gap/poison/crash；旧module negative gate；通用Artifact projection registry不拥有Session state |
| E3 / R2.44 | E1 | EventT observation binding；Kernel seam/Runtime adapter | object bus、telemetry驱动control | type continuity/drop-all/stale callback |
| E4 / R2.8 | P1/E1 | typed turn-context sources | Role locator、静态prompt易变内容 | ordering/suppression/failure disposition |
| E5 / R2.45 | E4/R0.1/R2.40 | prompt/cache/compaction generation | summary truth、对象/cache identity | reload/reprojection/secret canary |
| DEL-ENV / R1.2 | P1/P4 | routing与human interaction真实消费者迁各自最小Port | `AgentEnvironment`/`BaseEnvironment`/`MoteEnv`、package export、旧测试 | production consumer归零、旧symbol negative gate、Pyright |
| BC-ERROR / R3.1 | — | 各domain error定义；Runtime normalization；Product presentation；已完成serialized consumer inventory写入本R contract | `runtime.errors`聚合/re-export、字符串retry、英文contract | authoritative import、typed code/context、旧入口negative gate |
| BC-ERROR-WIRE / R3.6 | R3.1 | ToolResult/BG/Session ErrorReport strict envelope与精确discard；ACP/AG-UI/OpenAPI/public DTO为negative targets | 旧ErrorReport decoder/alias；不得触及Artifact/Secret/Workspace或外部shape | resolved destructive roots、typed discard receipt/audit、negative-target isolation、strict round-trip |
| BC-ARTIFACT-NAME / R2.23 | — | Runtime content repository与FileOps mutation repository不同owner；实施首项重新生成Artifact/FileOps consumer清单 | 同名歧义、第三repository facade、错误re-export | defining-module imports、consumer清单、content/transaction分别测试 |
| BC-CODEMAP / R2.5 | — | `runtime/code_map` extraction/index/query与immutable DTO；实施首项重新生成query/index/context consumers | store/extractor/provider外泄、与LSP合并manager | consumer清单、typed query/index/stable ordering/public allowlist |
| BC-CONFIG / R2.12 | C0/P1/T-TRUST | Contracts declaration、Product source/default/trust、Runtime activation spec消费canonical root config | runtime config grab-bag、万能config、secret stdout、第二root config | source→activation matrix、layer/schema/secret gates |
| MCP-RELOAD / R1.14 | R1.9/R1.16/R2.40 | MCP complete candidate compile/publish generation；Product catalog consumers | 先删旧catalog、partial publish、原地mutation | candidate failure保留旧代、identity/conflict/atomic swap |
| RELOAD / R2.49 | R2.42/R1.9/R1.14/R2.35/R2.40 | Product candidate generation/swap/drain；host/catalog consumers | duplicate watcher/reload trigger、in-place registry mutate | trust/capability/generation/stale reload/drain tests |
| PACKAGE-FACADE / R3.2 | H0 | context/OAuth/sandbox/interactive/presentation各自authoritative module与package-root consumers | 已确认shim/re-export/eager optional backend | 逐symbol before/after consumer、old import negative、hermetic import |
| MIGRATION-RESIDUE / R3.4 | — | 已确认无consumer的常量、注释、Kernel parallel catalog、ConfigWatcher；实施首项重跑external/wire/durable exact scan | 失效compat承诺与旧export；不得删除未来Port | scan产物、exact consumer归零、旧symbol negative、external target isolation |
| SQ1 / R3.5 | H0 | Squilla owner内正式typed seam与现有internal consumers | cross-package private import、`RoutingModelService`伪service | import/public seam/Pyright fixtures |
| BC-PRIVATE / R3.5 | H0 | 每个private consumer按共同owner证据迁入同owner或建立其消费方所需最小public seam | 跨owner private import、替代re-export | AST/import graph、逐consumer before/after evidence |

EVIDENCE_ONLY 卡不在本表伪装成可实施切片。裁决为实施后必须绑定一个具体 R owner并新增独立 fixed contract。各卡禁止共享 PublicServices/ConfigService/ErrorService/全局 registry。

### 5.1 Evidence card registry

Evidence card 只能读取源码、测试、导入图和当前 diff，不得修改生产代码。每张卡都必须产出完整 consumer inventory、当前 owner/identity/lifecycle/state 证据、候选 R 及 disposition。唯一允许的 disposition 是 `IMPLEMENT`、`MERGED_INTO`、`REJECTED`；裁决时必须同片更新核心总账的 owner/状态/证据、本文件第 2 节 disposition、第 2.1 节反向闭包、DAG 入边和 fixed slice contract。没有可执行 owner 或仍存在产品二选一时保持阻断。

| evidence_id | owner / candidate R | 必扫 production consumers | 必答 owner、identity、lifecycle 问题 | 裁决前禁止 |
| --- | --- | --- | --- | --- |
| EV-WRITE-CLASSIFICATION | R0.3、R0.8、R2.30、R2.36 候选分流 | workspace/media/FileOps/Tool/Hook/Workflow/BackgroundTask 的全部写入入口及 Product composition | 每个写入由谁授权、EffectId/target revision/runner identity 是什么，事务与失败恢复归谁 | 未分类 consumer 接入新写路径、复制 runner/FileOps、替尚未确认的冲突策略编码 |
| EV-SERVICES | R2.10 | `runtime/agent` Role/components/wiring、全部 hosts、composition 与测试 production factory | 每项 capability 的真实消费者、scope/lifecycle owner、是否需要跨包 Port；locator 是否只是泄漏对象图 | 换名 locator、建立万能 Services/Context、把完整 Role 注入组件 |
| EV-LSP | R2.5 或取证后新增 owner | CodeMap、LSP ingestion/query、turn-context source、Product LSP host | CodeMap 与 LSP 是否共享 identity/state/lifecycle；索引、进程和查询分别由谁拥有 | 合并为 manager、让 CodeMap 拥有 LSP 进程、在证据卡中新增 service |
| EV-DAEMON | R2.26 或取证确定的最新 owner | daemon/process runner、long-lived provider、composition start/stop、reload 与 health consumers | daemon identity/generation、activation/shutdown/fence owner；是否确有第二 owner | 直接编码 daemon service、复制 process runner、用 import/global singleton 管生命周期 |
| EV-PROVIDER | 各 Model/Web/Media domain 的最新 R owner | provider lookup/catalog/reload、Product selection、Runtime adapters 和所有包外 mutation callers | registry 的 canonical owner/identity/generation；外部是否需要 mutation，snapshot/query 是否足够 | 建统一 ProviderRegistryService、暴露 mutable registry、复制 catalog/reload |
| EV-PRESENTATION | R2.22 候选 | ACP、AG-UI、Terminal、Textual、CLI/gateway wire adapters | 哪些 DTO 真正跨 host/wire；canonical identity/version 和 presentation lifecycle 归谁 | 因字段相似扩张 Contracts、共享内部 view model、把 host policy 下沉 |
| EV-FILEOPS | R3.2 或取证确定的 domain owner | 所有跨包 FileOps command/query、Artifact content、workspace mutation、Tool consumers | command 与 immutable query 的最小面；transaction/target revision/lifecycle owner；哪些内部对象泄漏 | 暴露 mutable internals、建立第二 FileOps facade/store、绕过 canonical write path |
| EV-SERVICE-GATEWAY | R1.6、R2.6 或取证确定的 domain owner | composition inputs、media/search/service clients、gateway/CLI adapters | gateway 是 composition input、业务 service 还是无状态 facade；backend identity/lifecycle 谁拥有 | 保留无状态转发 facade、泄漏 backend record、建立万能 gateway |
| EV-ELISION | 取证后绑定具体 R；R3.4 只清确认残渣 | Product presenter、Tool output、turn context、prompt/cache/compaction consumers | elision 是 presentation、budget 还是 durable projection；原文与重建依据归谁 | 用 wording 决定 owner、丢 durable truth、建立全局 elision service |
| EV-PRESENTATION-DISPATCH | R2.22 候选 | surfaces、wire adapters、内部 union dispatch、事件/结果 presenter | dispatch tag/version 的 authoritative owner；内部 typed union 与外部 wire decoder 边界 | string/getattr 全局分派、把 wire tag 当内部 identity、未经 inventory 改公共 DTO |
| EV-PUBLIC-PER-OWNER | 每个发生变更的 bounded-context R owner | 该 owner 的 package-root、跨包 imports、所有 production consumers 与 extension points | 哪些 symbol 是承诺面、每个消费者需要的最小能力、owner/lifecycle 是否一致 | 横向公共面大扫除、为缩短 import 做 re-export、无消费者地扩 API |

每张 evidence card 的交付记录固定为：`evidence_id`、`scan_revision`、`files_and_symbols_scanned`、`production_consumer_inventory`、`current_owner_identity_lifecycle`、`reuse_findings`、`candidate_R`、`disposition`、`required_DAG_edges`、`required_fixed_slice`、`negative_targets`。若 disposition 为 `REJECTED`，必须写明“不实施”的架构理由和保持不变的 owner；若为另两种 disposition，未完成核心总账与本文件的同步前仍不得编码。

## 6. Migration / retention 授权矩阵

| domain | canonical facts | 决定 | owner / 删除边界 | 旧路径退出 |
| --- | --- | --- | --- | --- |
| Session | conversation/input/output/session facts | AUTHORIZED_DISCARD 旧格式 | Runtime Session；只删精确旧record | 新strict schema落地同片删旧decoder |
| Residency | blueprint state/mailbox projection | AUTHORIZED_DISCARD 旧格式 | Orchestration Residency；不删Agent lineage/Artifact | 可信Product构造 + 新record，同片删旧loader |
| Workflow | definition/run/checkpoint/frontier | AUTHORIZED_DISCARD 旧格式 | Orchestration Workflow；不删effect外部事实 | 新Run schema唯一，无双读 |
| Cron | task/schedule/occurrence | AUTHORIZED_DISCARD 旧格式 | Orchestration Automation | 新store唯一，无skip/fallback |
| ErrorCode durable envelope | ToolResult receipt、BackgroundTask attachment/notification、Session ErrorReport variants | AUTHORIZED_DISCARD 精确本地旧格式 | 各domain error owner；typed discard receipt/audit。ACP、AG-UI、Inference OpenAPI、公共DTO、Artifact metadata、Secret、Workspace为明确negative target并保持原样 | 新namespace/version/context唯一；旧decoder/alias删除；未来其他wire由各自owner独立版本化，不重新阻断R3.6 |
| Agent lineage/delivery | identity、spawn、delivery settlement | PRESERVE/新建迁移矩阵 | Orchestration Agent | 未获丢弃授权 |
| Artifact ownership/content | digest、ownership edge、legal hold | PRESERVE | Runtime Artifact | 未获丢弃授权；GC只删证明unreachable |
| Workspace/secret | 用户文件、credential | PRESERVE | FileOps/Secret owner | “从零开始”不适用 |
| derived cache/projection | 可确定性重建数据 | REBUILDABLE_BY_EVIDENCE | 各consumer projection | 必须绑定canonical revision，无LLM/effect |

实际删除前仍须列出 resolved targets、preview、authority、typed receipt/audit；不得以本表执行 broad recursive delete。

## 7. Tests、gates、fault 与 security matrix

每片运行 owner 测试、全部直接 consumer、composition/entrypoint、相关 architecture gate；Protocol/泛型运行 Pyright 覆盖 authoritative package 与全部 direct typed consumers。报告 collected/executed/pass/fail/skip/xfail/collection errors 和未运行范围。

Durable fault 测试按真实协议阶段编号：intent 前/后、effect 前/后、receipt 前/后、lease loss、restart/takeover；分别断言 durable bytes/state、公开 receipt、effect 次数、reconciler 决定和 stale owner 拒绝。文件协议测 flush/fsync/replace/parent-fsync，数据库/Temporal 测自身 transaction/ack，不机械套文件步骤。

安全矩阵覆盖 shell/fixed argv/workspace/network/MCP/Hook/Workflow effect 的 deny、ask/reject/timeout、stale approval、generation mismatch、sandbox unavailable、path traversal/symlink/TOCTOU、injection、cancel、receipt commit failure 与 secret redaction，并断言拒绝时外部调用次数为零。

架构 gate 只保护已确认关系：layer/import、唯一 composition/owner、public allowlist、禁止旧symbol、generic/Protocol、optional isolation。不得用名称后缀、文件数量或全局 Any 文本扫描猜语义。generated artifact 只能由一个 authoritative declaration 确定性生成并 diff 校验，不成为第二真相。

## 8. 核心总账映射、下发与交接

1. 下发前从核心总账实时生成 R 编号→标题映射，校验本文件每个 slice 的 owner/contract fingerprint；仅编号存在不够。另以 package audit 的 accepted invariants 为输入，逐项校验第2节 disposition 与第2.1节反向矩阵均存在 `invariant -> authoritative R -> fixed slice -> production consumer -> delete/negative gate`，任一链为空即阻断，不能用SB编号 `uniq` 代替。
   依赖闭包必须双向校验：DAG 的每条入边都必须出现在目标 fixed row 的 `requires`；fixed row 的每个硬前置也必须在 DAG 中存在同一 R/slice 入边；DAG 与 fixed registry 的 slice 节点集合也必须完全相等。`—/无` 不生成边并必须列入 `independent fixed roots`；外部 R 前置必须列入 `external ledger prerequisites`。别名如 `AG5/R1.20` 必须通过编号→slice映射归一后比较；传递可达不能替代正文明确声明的直接硬前置。
2. 核心总账为 `NEEDS_EVIDENCE`、`DECISION_REQUIRED` 或依赖未完成的切片不得编码。当前至少包括 R0.3 等待 ADR-D4；R0.9、R2.50、R2.52、R3.6 已同步为 `CONFIRMED`，其下游只受明确前置和核心总账实时状态约束，不得保留虚假 evidence node。R3.6 仍必须在 R3.1 完成后实施，但 inventory 本身不再是阻断。
3. 每片先提交固定 contract，再闭合 owner、composition、normal/failure/recovery/cancel/cleanup；同片迁消费者、删旧入口、加gate。
4. 不建立 compat、alias、双读写、fallback、临时 facade 或后置 cleanup。发现残渣退回其 ledger owner。
5. 完成后只更新核心总账状态和证据；本文件不写百分比或第二状态。
6. 最终交接包含：实际diff、复用证据、删除清单、migration/delete receipts、测试命令与计数、Pyright范围、预存失败、未运行项及剩余阻断。

本 v2 已通过上述规格级准入：第2–8节一致，38项SB disposition与逐不变量反向owner/slice闭包完整，正式切片绑定核心owner，产品决定与候选证据边界已分离。批准只授权满足实时核心前置的fixed slice开工，不解除R0.3/ADR-D4、EVIDENCE_ONLY或其他未就绪切片的局部阻断。
