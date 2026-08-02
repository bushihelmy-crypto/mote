# Package Cohesion 与 Service Boundary 债务治理实施规格（重写稿）

状态：待复审；除 H0 与只读取证外，不批准生产状态机改造  
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

ADR-D5 产品阻断已经解除。R2.50 仍为 `NEEDS_EVIDENCE`：必须完成现有 Inference DRR、Agent admission/mailbox、lease/store 的复用或拒绝复用审计，不能直接复制 `runtime/inference/fair_queue.py` 或建立万能 queue。

## 2. 38 项 SB disposition

Disposition 仅为 `IMPLEMENT`、`MERGED_INTO`、`REJECTED`、`EVIDENCE_ONLY`。它不表示进度；状态只看核心总账。

| SB | disposition | 唯一 ledger owner / slice | 边界 |
| --- | --- | --- | --- |
| SB0.1 | MERGED_INTO | R2.1/R2.47/R2.48 · WF1–WF4 | Workflow definition、run、recovery、effect/terminal |
| SB0.2 | MERGED_INTO | R2.28/R2.50–R2.53/R1.20/R1.13 · AG1–AG5 | lineage、scheduler、三类 cap、budget、cancel、residency、delivery |
| SB0.3 | IMPLEMENT | R0.8/R0.9 · D1 | backend guarantee 与 operation fencing |
| SB0.4 | MERGED_INTO + EVIDENCE_ONLY | R0.8/R2.30/R1.7/R0.3 · X0–X3 | EffectId、typed runner/helper；workspace write逐consumer分类 |
| SB0.5 | MERGED_INTO | R2.19–R2.21/R2.24–R2.25/R2.29/R2.31–R2.33/R2.41 · C1 | 逐 domain strict schema；无万能 codec |
| SB1.1 | IMPLEMENT | R1.1 · P3 | typed resource lease |
| SB1.2 | EVIDENCE_ONLY | R2.10 · EV-SERVICES | Wiring/Services scope与consumer，禁止换名 locator |
| SB1.3 | MERGED_INTO | R2.42 · P1/P2 | pure construct、async activate、唯一 factory |
| SB1.4 | MERGED_INTO | R2.11/R2.42 · P4 | 全 host/test 复用 composition |
| SB1.5 | IMPLEMENT | R2.15 · T-AGENT | OutputT 与 SpawnContext 窄 capability |
| SB1.6 | IMPLEMENT | R2.14 · T-KERNEL | Kernel operation 与 consumer-owned Port |
| SB1.7 | MERGED_INTO + EVIDENCE_ONLY | R2.5 · BC-CODEMAP；EV-LSP | CodeMap typed面；LSP先补consumer matrix |
| SB1.8 | MERGED_INTO | R2.25/R2.43/R2.44 · E1–E3 | SessionFact、subscriber、typed observation |
| SB1.9 | MERGED_INTO | R0.6/R0.2/R2.8 · H1/T-MODEL/T-SKILL | governed Hook、model、Skill contracts |
| SB1.10 | MERGED_INTO + EVIDENCE_ONLY | R2.3/R2.41 · T-QUEUE/T-GEN；EV-DAEMON | queue/generation；daemon双owner先证明 |
| SB1.11 | IMPLEMENT | R2.36 · T-TOOL | 唯一 ToolExecutor，收窄 control/live catalog |
| SB1.12 | EVIDENCE_ONLY | EV-PROVIDER | 未证明包外mutation不建registry service |
| SB1.13 | IMPLEMENT | R1.2/R2.11 · DEL-ENV | 删除无外部承诺 Environment facade |
| SB1.14 | IMPLEMENT | R1.26/R1.4/R1.5 · BG1/BG2 | Agent-owned pool、pin/drain、TaskId+AttemptId |
| SB1.15 | IMPLEMENT | R3.1 · BC-ERROR | authoritative error owner |
| SB1.16 | EVIDENCE_ONLY | EV-PRESENTATION | cross-host DTO consumer先取证 |
| SB1.17 | MERGED_INTO | R2.12/R2.40 · BC-CONFIG/BC-TOOL-ID | Product defaults 与 Runtime mechanism 分治 |
| SB1.18 | IMPLEMENT | R3.6 · BC-ERROR-WIRE | serialized inventory、strict envelope、授权丢弃旧数据 |
| SB2.1 | EVIDENCE_ONLY | EV-FILEOPS | public command/query consumer matrix |
| SB2.2 | IMPLEMENT | R2.23 · BC-ARTIFACT-NAME | 两个不同 repository 精确命名，不误合并 |
| SB2.3 | MERGED_INTO | R2.1/R2.47 · WF1/WF2 | Workflow public surface随owner收口 |
| SB2.4 | EVIDENCE_ONLY | EV-SERVICE-GATEWAY | composition input 与业务泄漏分型 |
| SB2.5 | MERGED_INTO | R2.5 · BC-CODEMAP | CodeMap query/index最小面 |
| SB2.6 | IMPLEMENT | R2.25 · PR1 | Session read model迁 `runtime/session` |
| SB2.7 | IMPLEMENT | R2.12 · BC-CONFIG | 配置按domain/consumer归位 |
| SB2.8 | MERGED_INTO | R2.12/R1.14/R2.49 · BC-CONFIG/RELOAD | 唯一 watcher、Product reload与generation swap |
| SB2.9 | EVIDENCE_ONLY | EV-ELISION；R3.4仅清残渣 | wording明确；elision owner待证据 |
| SB2.10 | IMPLEMENT | R3.5 · SQ1 | Squilla owner内typed seam，无伪service |
| SB2.11 | EVIDENCE_ONLY | EV-PRESENTATION-DISPATCH | wire adapter/内部union consumer取证 |
| SB2.12 | MERGED_INTO | R3.2 · H0/各owner | optional eager import进H0，其余随owner删 |
| SB2.13 | IMPLEMENT | R3.5 · BC-PRIVATE | private consumer按owner或最小seam迁移 |
| SB2.14 | IMPLEMENT | R3.5 · H0 | hermetic collection；撤销旧local-import误报 |
| SB2.15 | EVIDENCE_ONLY | EV-PUBLIC-PER-OWNER | 只随真实owner变更，不做公共面大扫除 |

`EVIDENCE_ONLY` 不得编码。裁决只能更新为 IMPLEMENT、MERGED_INTO 或 REJECTED，并与核心总账和本表同片更新。

## 3. Canonical scope/guarantee matrix

| capability | layer / scope / identity | lifecycle owner | durability / guarantee | production entry | 删除旧 owner |
| --- | --- | --- | --- | --- | --- |
| Application composition | Product；application/generation | Product Application | pure construct、ordered activate、reverse shutdown | `product/composition/` typed factory | CLI/gateway/SDK/test object graph |
| Agent lineage/spawn | Orchestration；root/tree/AgentId/SpawnRequestId | lineage/spawn saga | durable、幂等、worker前commit identity | typed spawn command/query | process registry/rollback closure |
| Agent scheduling/cap/budget | Orchestration；root/subtree/reservation revision | 各独立 scheduler/cap/budget owner | durable queue/projection/ledger；fenced settlement | typed reserve/claim/settle | `max_agents`、telemetry truth、全能governance store |
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
 ├─ A0 ledger/SB mapping
 ├─ P1 scope matrix -> P2 composition lifecycle -> P3 lease -> P4 host migration
 ├─ X0 effect identity -> X1 runners -> H1 governed Hook
 └─ C1 domain schema + migration decision

A0+P1+C1 -> AG1 lineage/spawn
AG1 -> AG2 scheduler(R2.50, ADR-D5 confirmed/reuse evidence required) -> AG3 cap(R2.51) + AG4 budget(R2.52)
AG1+AG3 -> AG5 residency -> AG6 delivery
AG1+AG5 -> BG1 pool lifecycle -> BG2 release/cascade(R1.26/R2.53)

C1+X1 -> WF1 definition -> WF2 durable run -> WF3 recovery -> WF4 effect/terminal
C1+AG1+AG5 -> RS1 residency record protocol
C1+X1 -> AU1 automation occurrence
AG5+AG6+BG2+WF4+RS1+AU1 -> GC1 cleanup/reachability/retention/clock

P1+X1+C1 -> E1 SessionFact -> E2 subscriber -> E3 observation -> E4 context -> E5 prompt/cache
每个owner完成 -> 同片consumer migration + old-path deletion + gate
全部完成 -> 全量fault/security/architecture验证
```

只读取证可以并行；编码只有在 `requires` 全部 CONFIRMED/DONE 且不共享 authoritative contract、identity、state、wire、store、composition generation 或 consumer migration 时并行。文件不重叠不能证明独立。

## 5. CONFIRMED slice 固定 contract

每个切片必须填写并冻结：`ledger_owner`、`requires/unblocks`、`contract/owner/identity`、`entries/consumers`、`scope/lifecycle/guarantee`、`reuse_decision`、`deletes`、`tests/gates/pyright`。扩大范围先重审，不靠临时 seam。

| slice | requires | owner/consumer/guarantee | deletes | tests/gates |
| --- | --- | --- | --- | --- |
| H0 | 无 | Product package import；architecture collection | eager optional root import、真实非法import | hermetic collect/import矩阵、AST/layer/SCC negative fixtures |
| P1–P4 | H0 | Product Application；CLI/gateway/daemon/SDK/test；pure construct/activate/shutdown | CLI object graph、sync activation旁路、重复host factory | scope matrix、阶段fault、constructor allowlist |
| X0–X3 | H0/C1 | EffectId + typed runners；Tool/Hook/WF/BG/media | shell bool、cmd split、direct spawn、permission旁路 | deny/ask/stale approval/injection/zero-effect |
| AG1 | H0/C1/P1 | lineage/spawn saga；Agent tools/hosting | memory registry、rollback原子性 | 每状态crash、dedupe、ABA、restart |
| AG2–AG4 | AG1；ADR-D5已确认；AG2需复用审计 | scheduler/cap/budget独立owners；Agent turn/Tool/LLM；root+subtree WDRR、cost=1、tenant==root、bounded weight/priority、FIFO、fenced claim | `max_agents`、无限park、telemetry余额、Inference concrete queue复制、accepted eviction | 有限无饥饿、priority隔离、FIFO、deadline/cancel/claim CAS、queue-full零accepted、restart/scan、stale fence、config generation、poison/retry HOL |
| AG5–AG6 | AG1/AG3/C1 | residency + delivery；AgentControl/hosting | unfenced map/store、parked accepted/drop | eviction/rehydrate与accept/ack全fault点 |
| BG1–BG2 | AG1/AG5/X1 | Agent-owned pool；Role/Residency/subtree cancel | shared registry、Workflow resume、snapshot-only pin | drain barrier、Attempt fencing、owner-loss、release |
| WF1–WF4 | C1/X1/AG5/AG6 | Workflow owner；Product tools/Session resume | random/parallel run owner、callable snapshot、continuation registry | dual owner、checkpoint/effect/terminal crash矩阵 |
| RS1/AU1/GC1 | C1/AG/WF/BG/X1 | Residency/Automation/Artifact各自owner | PID lock、forgiving store、mtime GC、万能delete | CAS/fence/corruption/DST/pin-generation |
| E1–E5 | P1/C1/X1 | Session fact/subscriber/event/context/prompt owners | object sink/bus、summary truth、semantic cache漂移 | replay/ack、generation barrier、secret canary |
| BC-* | 对应owner slice | 每bounded context真实command/query consumers | facade/re-export/private API/错误owner | before/after consumer、public negative gate、Pyright |

EVIDENCE_ONLY 卡不在本表伪装成可实施切片。每个 BC 卡独立签收，禁止共享 PublicServices/ConfigService/ErrorService/全局 registry。

## 6. Migration / retention 授权矩阵

| domain | canonical facts | 决定 | owner / 删除边界 | 旧路径退出 |
| --- | --- | --- | --- | --- |
| Session | conversation/input/output/session facts | AUTHORIZED_DISCARD 旧格式 | Runtime Session；只删精确旧record | 新strict schema落地同片删旧decoder |
| Residency | blueprint state/mailbox projection | AUTHORIZED_DISCARD 旧格式 | Orchestration Residency；不删Agent lineage/Artifact | 可信Product构造 + 新record，同片删旧loader |
| Workflow | definition/run/checkpoint/frontier | AUTHORIZED_DISCARD 旧格式 | Orchestration Workflow；不删effect外部事实 | 新Run schema唯一，无双读 |
| Cron | task/schedule/occurrence | AUTHORIZED_DISCARD 旧格式 | Orchestration Automation | 新store唯一，无skip/fallback |
| ErrorCode durable envelope | journal/event/snapshot中的旧error编码 | AUTHORIZED_DISCARD 本地旧格式 | 各domain error owner；仓外wire另取证 | 新namespace/version/context唯一 |
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

1. 下发前从核心总账实时生成 R 编号→标题映射，校验本文件每个 slice 的 owner/contract fingerprint；仅编号存在不够。
2. 核心总账为 `NEEDS_EVIDENCE`、`DECISION_REQUIRED` 或依赖未完成的切片不得编码。ADR-D5 已确认，但 R2.50 的基础设施复用证据未完成，因此 AG2 及其依赖仍不得编码。
3. 每片先提交固定 contract，再闭合 owner、composition、normal/failure/recovery/cancel/cleanup；同片迁消费者、删旧入口、加gate。
4. 不建立 compat、alias、双读写、fallback、临时 facade 或后置 cleanup。发现残渣退回其 ledger owner。
5. 完成后只更新核心总账状态和证据；本文件不写百分比或第二状态。
6. 最终交接包含：实际diff、复用证据、删除清单、migration/delete receipts、测试命令与计数、Pyright范围、预存失败、未运行项及剩余阻断。

本重写稿只有在第2–8节彼此一致、38项SB全覆盖、全部可实施切片绑定最新核心owner，且复审确认不存在候选决定伪装成contract后，才可批准为生产实施规格。
