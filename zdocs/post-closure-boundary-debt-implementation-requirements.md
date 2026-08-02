# Mote 架构边界闭合实施需求

> 状态：`APPROVED / 实施基线`；所有production write必须等待S0关闭
>
> 当前实施事实：仓库虽已有多个domain的部分机制，但尚无任何本需求requirement完成S0治理登记并达到`VERIFIED`，严格已关闭数量为0；必须先实施并签收`R-W0-GOVERNANCE-001`，再按DAG复核、迁移和关闭后续需求。
>
> 适用范围：`contracts/`、`kernel/`、`runtime/`、`orchestration/`、`product/`及其`ztest/`、`zdocs/`
>
> 本文是直接实施依据。每个工作包必须独立登记owner、write set、依赖、验证证据和关闭状态；不得把本文作为一次跨域大重构执行。

## 1. 目标状态

实施完成后，Mote必须具备以下性质：

- 生产依赖严格遵循`contracts <- kernel <- runtime <- orchestration <- product`；
- 每个概念只有一个canonical owner、authoritative type、生产装配入口和状态真相链；
- 正式边界不使用`Any`、`object`、裸`dict`、`Callable[...]`省略参数、反射或duck typing代替已知contract；
- 外部动态值只在adapter入口存在，并立即经过严格、版本化、fail-closed投影；
- durable状态具有稳定identity、strict codec、revision/CAS、lease/fencing、迁移、恢复、retention和删除authority；
- 外部副作用在动作前持久化intent，动作后以receipt/evidence结算；未知结果进入`IN_DOUBT`，不得盲重试；
- projection、index、mailbox、wake、UI state和进程内cache均可从canonical facts重建，且不能反写权威状态；
- queue、payload、attempt、scan、retention、compaction和storage均有明确上界；
- Product是唯一composition root，不存在兼容层、平行factory、双读双写、fallback owner或第二执行链；
- 所有旧入口在对应迁移切片内退出，不保留alias、wrapper、feature flag或长期migration reader。

## 2. 实施纪律

### 2.1 每个工作包的准入材料

编码前必须提交：

1. canonical owner、核心不变量及真实consumer；
2. 现有DTO、Port、store、codec、lease、scheduler、artifact和composition检索结果；
3. authoritative identity/type/state、最小服务面和被隐藏的实现细节；
4. 精确write set、共享热点文件、唯一writer和consumer迁移清单；
5. schema、状态机、revision/generation/fence、失败与恢复语义；
6. migration inventory、cutover、partial failure、旧版本退出和retention；
7. effect、permission、authority、audit与不可逆动作边界；
8. 容量、payload、scan、retry、deadline、retention和storage上限；
9. production activation、shutdown、restart和rollback边界；
10. 正向、负向、corruption、crash、并发、takeover和架构门禁证据。

同一authoritative type、store schema或composition recipe同一时刻只能有一个writer工作包。上游contract revision改变时，下游证据必须重新base和验证。

### 2.2 通用迁移协议

所有durable格式切换均遵循：

```text
strict inventory
  -> conflict/dry-run report
  -> inactive candidate
  -> flush/fsync/read-back
  -> generation manifest/CAS cutover
  -> new writer activation
  -> consumer migration
  -> evidence window
  -> legacy retirement
```

- inventory不得调用LLM、Tool、远端provider或产生外部副作用；
- corruption、unknown version/tag、额外字段、错误primitive、重复identity和mixed generation均fail closed；
- cutover前保留原事实，cutover后生产不得双读双写；
- blocked数据保持只读并保留证据，不得清空、猜测修复或创建空的新事实；
- migration-only decoder不属于production fallback；production旧路径在cutover立即退出，decoder/source只按证据retention时钟保留，到期后必须删除。

### 2.3 通用外部副作用协议

每个effect必须声明：

- `NO_EXTERNAL_EFFECT`；
- `IDEMPOTENT_BY_KEY`；
- `RECONCILABLE_BY_RECEIPT`；
- `NON_REPLAYABLE`。

Logical EffectId绑定caller、run/turn、definition/config/permission generation、canonical arguments digest、provider/account/endpoint contract；attempt ordinal不进入logical identity。同identity不同preimage必须返回typed conflict。

动作前提交durable intent；动作后提交provider/process evidence与terminal settlement。Stale owner不能提交canonical结果；动作已发生而本地提交失败时，由current owner查询或消费immutable evidence进行对账，无法证明结果则保持`IN_DOUBT/OWNER_ACTION_REQUIRED`。

## 3. 工作包总览

| Workstream | 首节点 | 交付结果 |
|---|---|---|
| Governance与范围发现 | `R-W0-GOVERNANCE-001` | source baseline、ledger、production集合与机械门禁 |
| 确定性删除 | `R-W1-DEAD-SURFACES-001` | 删除无consumer入口、伪catalog和反射fallback |
| Tool/Permission/Sandbox | `R-W2-TOOL-BINDING-001` | 唯一compiled binding与ToolExecutor effect链 |
| Product typed surfaces | `R-W2-LSP-001` | LSP、Presentation、Notebook、Role最小服务面 |
| Agent ingress | `R-W3-AGENT-INGRESS-001` | delivery/turn双owner原子acceptance与恢复 |
| Workflow | `R-W3-WORKFLOW-EFFECT-001` | durable run、effect reconciliation与typed inspection |
| BackgroundTask | `R-W0-BGTASK-GOVERNANCE-VERIFY-001` | 保留per-Agent pool并闭合cleanup settlement |
| Cron | `R-W3-CRON-001` | v3 occurrence状态机、revision reconcile与delivery |
| OAuth | `R-W3-OAUTH-001` | metadata/SecretRef唯一真相与refresh/revoke对账 |
| RunJournal退役 | `R-W3-RUN-DOMAINS-001` | Tool effect、Model projection、Session timer分治 |
| Hosted ServiceCall | `R-W3-SERVICE-CALL-001` | v3 remote operation lifecycle与可重建index |
| Artifact/Session deletion | `R-W3-ARTIFACT-EDGE-001` | typed edge、完整reachability与fenced deletion |
| Session rollout | `R-W3-SESSION-STREAM-001` | v2 strict stream、Artifact binding与有界replay |
| Event subscription | `R-W3-EVENT-001` | fenced checkpoint、无replay DLQ与retention |
| Daemon | `R-W3-DAEMON-001` | strict discovery与single-generation升级 |

全局关键依赖：

```text
Governance/source baseline
  -> 每个workstream的VERIFY/contract首节点

Tool compiled binding
  -> Permission/Hook compiler
  -> Tool effect owner
  -> Workflow/ServiceCall ToolEffect edge

Artifact EDGE contract
  -> Session migration
  -> Workflow/BackgroundTask/Tool/Model/ServiceCall/delivery edge producer
  -> Artifact GC activation

Agent delivery/turn contract
  -> Cron delivery
  -> Workflow terminal delivery

ModelCall canonical terminal
  -> Session projection intent/ack
  -> RunJournal think退役
```

`R-W0-GOVERNANCE-001`是唯一全局production-write前置。S0关闭前只允许只读inventory、需求/治理文档、manifest/ledger/gate设计和先失败fixture，不得修改production语义、激活writer、执行migration cutover或destructive cleanup。S0关闭后，各domain按自身DAG流水推进；不要求所有S1完成后才能开始某个已经满足依赖的S2。本文所称“无全局barrier”只适用于S0之后，不能覆盖S0。

```text
S0 governance baseline / ledger / recipe catalog / failing gates
  -> S1 direct removals + foundational typed contracts
       -> S2 strict inventories + inactive migration candidates
            -> S3 canonical stores / commands / CAS / fences
                 -> S4 execution / reconciliation / projections / consumers
                      -> S5 production old-path retirement + evidence clocks
                           -> S6 integrated architecture / fault / scale acceptance
```

Stage内仅在write set不相交且前置contract已合入时并行。Production旧reader/writer/fallback在S3/S5对应cutover切片立即退出；migration-only source/decoder进入evidence clock，不能形成兼容期。

## 4. Governance与架构门禁

### 4.1 `R-W0-GOVERNANCE-001`

交付两个单向依赖的治理对象：

- source baseline manifest：冻结`AGENTS.md`、生产源码、测试和普通需求；
- governance evidence manifest：记录ledger、扫描和verification report，并单向引用source baseline。

Source baseline不得包含反向引用自身identity的ledger或报告。Stable requirement identity与content revision分离；OPEN记录可无owner，ASSIGNED后必须有唯一owner/writer。Git治理文件使用review/base revision保证一致性，不建设Runtime式lease服务。

### 4.2 Authoritative集合

分别由各owner生成：

- active durable authority set；
- active lease/CAS mutation authority set；
- public Port/factory/registry/callback/checkpoint/codec set；
- production entrypoint/composition recipe set；
- approved dynamic boundary set。

集合必须与production-capable对象图双向一致。Test fake、archive、migration-only reader和process-local cache使用typed classification排除；发现器失败或范围不完整时fail closed。

### 4.3 架构门禁

- 禁止五层逆向import和生产局部import；
- dynamic import只允许Product-owned manifest/catalog discovery；
- 正式边界禁止无界`Any/object/dict/Callable`和反射；
- durable decoder严格拒绝unknown/extra/wrong primitive/identity mismatch；
- capability、permission、effect、approval、audit、cleanup贯穿同一definition/generation identity；
- 禁止兼容alias、跨层re-export、平行catalog、第二output engine和第二durable decoder；
- 安全语义由真实composition负向/竞争/corruption测试证明，不以substring搜索自证；
- `ztest/architecture/`、全量Pyright、受影响bounded context测试和最终全仓测试形成分层证据。资源受限环境可分批执行并记录未覆盖范围，但不能永久豁免最终签收。

### 4.4 Stable requirement identity映射

内容修订只推进reviewed revision，不改变stable identity。下表只使用整合前实际存在的exact ID；每个旧ID只能命中一行。`R-W2-LSP-`、`R-W2-PRESENTATION-`、`R-W3-AGENT-`等检索产生的截断family文本不是requirement identity，不进入ledger。

| 原requirement | 本文requirement | disposition | 说明与继承证据 |
|---|---|---|---|
| `R-W1-001` | `R-W1-001` | PRESERVED | provider moderation retirement；继承D21/public consumer证据 |
| `R-W1-002` | `R-W1-002` | PRESERVED | inference admin retirement |
| `R-W1-003` | `R-W1-003` | PRESERVED | Model client retirement |
| `R-W1-004` | `R-W1-004` | PRESERVED | AES registry retirement |
| `R-W1-005` | `R-W1-005` | PRESERVED | i18n retirement |
| `R-W1-006` | `R-W1-006-temporal`、`R-W1-006-squilla` | SPLIT_INTO | 两个子ID为新stable identity；backend owner不同，不存在聚合write lease |
| `R-W1-DEAD-SURFACES-001` | `R-W1-001`、`R-W1-002`、`R-W1-003`、`R-W1-004`、`R-W1-005`、`R-W1-006-temporal`、`R-W1-006-squilla` | SPLIT_INTO | 仅保留epic/index，无write lease |
| `R-W2-LSP-001` | 同名 | PRESERVED | LSP contract证据 |
| `R-W2-LSP-002` | 同名 | PRESERVED | LSP adapter证据 |
| `R-W2-LSP-003` | 同名 | PRESERVED | LSP consumer证据 |
| `R-W2-PRESENTATION-001` | 同名 | PRESERVED | scope contract证据 |
| `R-W2-PRESENTATION-002` | 同名 | PRESERVED | ViewEvent证据 |
| `R-W2-PRESENTATION-003` | 同名 | PRESERVED | wire/consumer证据 |
| `R-W2-NOTEBOOK-001` | `R-W2-NOTEBOOK-DOCUMENT-001`、`R-W2-NOTEBOOK-STDIN-001` | SPLIT_INTO | document codec与stdin lifecycle分属不同owner；原ID仅epic |
| `R-W2-001` | `R-W2-001` | PRESERVED | Hook freeze与Permission applicability compiler；Sandbox profile另由依赖节点实现 |
| `R-W3-WORKFLOW-EFFECT-002` | `R-W3-WORKFLOW-MIGRATION-001`、`R-W3-WORKFLOW-MIGRATION-002` | SUPERSEDED | 将旧聚合migration拆为inventory/candidate与atomic cutover；继承D01证据 |
| `R-W3-WORKFLOW-EFFECT-001`、`R-W3-WORKFLOW-EFFECT-003`、`R-W3-WORKFLOW-MIGRATION-001`、`R-W3-WORKFLOW-MIGRATION-002`、`R-W3-WORKFLOW-DELIVERY-001`、`R-W3-WORKFLOW-TEMPORAL-001` | 各同名 | PRESERVED | exact ID逐项保留；不包含EFFECT-002 |
| `R-W3-CRON-001`、`R-W3-CRON-002`、`R-W3-CRON-003`、`R-W3-CRON-004`、`R-W3-CRON-DELIVERY-001`、`R-W3-CRON-ARTIFACT-001` | 各同名 | PRESERVED | exact ID逐项保留 |
| `R-W3-AGENT-INGRESS-001`、`R-W3-AGENT-INGRESS-MIGRATION-001`、`R-W3-AGENT-INGRESS-MIGRATION-002`、`R-W3-AGENT-DELIVERY-001`、`R-W3-AGENT-TURN-001`、`R-W3-AGENT-INGRESS-RECONCILE-001`、`R-W3-AGENT-PROJECTION-001`、`R-W3-AGENT-INGRESS-SURFACES-001` | 各同名 | PRESERVED | exact ID逐项保留 |
| `R-W3-OAUTH-RETIRE-001` | `R-W3-OAUTH-SECRET-ERASURE-001`、`R-W3-OAUTH-PRODUCTION-PATH-RETIRE-001`、`R-W3-OAUTH-MIGRATION-EVIDENCE-RETIRE-001` | SPLIT_INTO | secret、production path与evidence具有不同retention/authority |
| `R-W3-OAUTH-001`、`R-W3-OAUTH-MIGRATION-001`、`R-W3-OAUTH-MIGRATION-002`、`R-W3-OAUTH-STORE-001`、`R-W3-OAUTH-EFFECT-001`、`R-W3-OAUTH-COMMAND-001`、`R-W3-OAUTH-CONSUMER-001` | 各同名 | PRESERVED | exact credential ID逐项保留 |
| `R-W3-RUN-DOMAINS-001`、`R-W3-TOOL-EFFECT-001`、`R-W3-MODEL-PROJECTION-001`、`R-W3-SESSION-TIMER-001`、`R-W3-RUNJOURNAL-MIGRATION-001`、`R-W3-RUNJOURNAL-MIGRATION-002`、`R-W3-RUNJOURNAL-CONSUMERS-001`、`R-W3-RUNJOURNAL-RETIRE-001`、`R-W3-TEMPORAL-RUNJOURNAL-RETIRE-001` | 各同名 | PRESERVED | exact RunJournal split/retire ID逐项保留 |
| `R-W3-RUNJOURNAL-001` | `R-W3-RUN-DOMAINS-001`、`R-W3-TOOL-EFFECT-001`、`R-W3-MODEL-PROJECTION-001`、`R-W3-SESSION-TIMER-001`、`R-W3-RUNJOURNAL-MIGRATION-001`、`R-W3-RUNJOURNAL-MIGRATION-002`、`R-W3-RUNJOURNAL-CONSUMERS-001`、`R-W3-RUNJOURNAL-RETIRE-001` | SUPERSEDED | 不加固通用RunJournal，按domain拆分并退役 |
| `R-W3-SERVICE-CALL-001`、`R-W3-SERVICE-CALL-MIGRATION-001`、`R-W3-SERVICE-CALL-MIGRATION-002`、`R-W3-SERVICE-CALL-STORE-001`、`R-W3-SERVICE-CALL-EXECUTION-001`、`R-W3-SERVICE-CALL-RECONCILE-001`、`R-W3-SERVICE-CALL-CONSUMERS-001`、`R-W3-SERVICE-CALL-RETIRE-001` | 各同名 | PRESERVED | exact ServiceCall ID逐项保留 |
| `R-W3-ARTIFACT-EDGE-001`、`R-W3-ARTIFACT-MIGRATION-001`、`R-W3-ARTIFACT-MIGRATION-002`、`R-W3-ARTIFACT-STORE-001`、`R-W3-ARTIFACT-DELETION-001`、`R-W3-ARTIFACT-CONSUMERS-001`、`R-W3-ARTIFACT-GC-001`、`R-W3-SESSION-DELETION-001`、`R-W3-WORKSPACE-CLEANUP-RETIRE-001` | 各同名 | PRESERVED | exact Artifact/deletion ID逐项保留 |
| `R-W3-SESSION-STREAM-001`、`R-W3-SESSION-MIGRATION-001`、`R-W3-SESSION-MIGRATION-002`、`R-W3-SESSION-STORE-001`、`R-W3-SESSION-RETENTION-001`、`R-W3-SESSION-PROJECTIONS-001`、`R-W3-SESSION-LEGACY-RETIRE-001` | 各同名 | PRESERVED | exact Session ID逐项保留 |
| `R-W0-GOVERNANCE-001`、`R-W0-WORKFLOW-GOVERNANCE-VERIFY-001`、`R-W0-BGTASK-GOVERNANCE-VERIFY-001`、`R-W3-EVENT-001`、`R-W3-DAEMON-001`、`R-W3-MODEL-PERSISTENCE-MIGRATION-001` | 各同名 | PRESERVED | 既有exact ID逐项保留 |

本文新增的`R-W1-006-temporal`、`R-W1-006-squilla`及owner-specific W2/Model requirements必须在S0以`NEW_ID` declaration登记创建依据和owner；`NEW_ID`不是旧ID mapping disposition，不能伪造为PRESERVED。

任何未在表中命中的既有`R-*`必须fail closed：先补精确PRESERVED/SPLIT/MERGED/SUPERSEDED记录和继承decision/evidence，再允许ASSIGNED。

### 4.5 Canonical write-set与唯一writer

| Canonical write-set | 唯一writer requirement族 | 消费方允许的改动 | 互斥/顺序 |
|---|---|---|---|
| governance schema/ledger/validator | `R-W0-GOVERNANCE-001` | 只提交typed evidence | S0唯一writer；测试不能推进状态 |
| Contracts公共DTO/Port/codec | 对应domain首个`*-001` | contract合入后迁移调用，不重复定义 | 同module串行；shared identity先行 |
| Tool binding/effect pipeline | `R-W2-TOOL-BINDING-001`、`R-W3-TOOL-EFFECT-001`按阶段 | Workflow/ServiceCall只提交typed command | 与Permission/Hook/Sandbox触及同文件时串行 |
| Workflow run/effect store | `R-W3-WORKFLOW-*` | Tool/Temporal只写自己的adapter/edge | contract→migration→writer→consumer→retire |
| Cron store | `R-W3-CRON-*` | delivery/artifact只绑定Port | 001→002→003/004→retire |
| Agent delivery store | `R-W3-AGENT-DELIVERY-001` | Cron/Workflow/Product提交command | migration cutover前无新writer |
| Agent turn queue | `R-W3-AGENT-TURN-001` | delivery只实现prepare/bind receipt | 与delivery共享contract、不共享store writer |
| ModelCall/checkpoint/store | `R-W2-MODEL-CONTRACT-001`后由`R-W3-MODEL-CHECKPOINT-001`、`R-W3-MODEL-COMPOSITION-001`按阶段持有 | Session只提交projection intent/ack；RunJournal think migration只读legacy并写inactive candidate | Model owner独占target state；migration不得修改Model状态机源码；RECOVERY只写fixture/adapter，不成为store writer |
| OAuth metadata/secret/effect | `R-W3-OAUTH-STORE/EFFECT/COMMAND-*` | MCP/Model只持borrow | 触及manager/store文件时串行；consumer最后 |
| RunJournal legacy source | `R-W3-RUNJOURNAL-MIGRATION/RETIRE-*` | target owner写自己的target state | migration不修改target状态机源码 |
| Hosted ServiceCall | `R-W3-SERVICE-CALL-*` | Tool/Product经Port | contract→migration→store→execution/reconcile→consumer→retire |
| Artifact metadata/edge/CAS/GC | `R-W3-ARTIFACT-*` | 每个domain owner修改自己的producer源码 | `ARTIFACT-CONSUMERS-001`仅协调验收，无跨域write lease |
| Session rollout/lifecycle | `R-W3-SESSION-*` | FileOps/Notebook/wire由自身consumer单迁移 | `SESSION-DELETION-001`与`SESSION-RETENTION-001`共用一个Session writer lease，不并行 |
| workspace cleanup | `R-W3-WORKSPACE-CLEANUP-RETIRE-001` | 仅删除已替代旁路 | 等待Session retention、Artifact GC、producer edges |
| Event subscription | `R-W3-EVENT-001` | Product只composition/config | 与其他`runtime/events`写入串行 |
| Presentation/LSP/Notebook/Connection | 各owner-specific W2 requirement | 各external adapter只写自己的wire/consumer | Product composition热点由S0登记串行窗口 |

Migration requirement读取legacy source并写inactive candidate，不取得target状态机源码writer权。跨owner原子性由Contracts-owned prepare/commit/receipt表达；integration requirement只协调Port、fixture和验收，不成为第三writer。同一module同一时刻只能有一个active writer lease。

### 4.6 Requirement状态与执行记录

每个非epic requirement必须有一条独立、可机械校验的记录。状态只能按下列闭合集合推进：

```text
OPEN -> ASSIGNED -> IN_PROGRESS -> IMPLEMENTED -> VERIFIED
           ^          |
           |          v
           +------- BLOCKED
```

- `OPEN`：已登记stable identity、owner domain、依赖和验收，但尚未分配writer，可无执行owner；
- `ASSIGNED`：已绑定唯一执行owner、精确write set、source baseline revision和writer lease，不得修改production；
- `IN_PROGRESS`：所有前置requirement为`VERIFIED`，S0已关闭，且writer lease仍匹配当前revision，才可产生production write；
- `IMPLEMENTED`：代码、migration candidate、consumer和旧production路径已在本requirement范围内闭合，但批准authority尚未签署verification；
- `VERIFIED`：批准authority声明与机械门禁同时通过，且证据绑定同一source baseline、requirement revision和production composition generation；
- `BLOCKED`：保存typed reason、阻断事实和恢复条件；不得将blocked migration data、未结算effect或失败cleanup解释为部分成功。

`BLOCKED -> ASSIGNED`是唯一恢复边：阻断事实已经消失、source baseline重新验证、completion dependencies仍为`VERIFIED`、write set重新取得唯一writer lease后才能恢复。阻断前lease立即失效；禁止`BLOCKED -> IN_PROGRESS/IMPLEMENTED/VERIFIED`。源码漂移证明requirement不再适用时使用独立`OBSOLETE` disposition及批准证据，不得伪装成BLOCKED恢复或VERIFIED。

依赖分为两类：

- completion dependency：前置必须`VERIFIED`，后继才能进入`IN_PROGRESS`；
- activation cohort dependency：schema中显式列出的成员分别持不相交writer lease达到`IMPLEMENTED`，但都不得独立发布writer；唯一cutover requirement验证完整cohort，在同一generation原子激活candidate、target writer、consumer和legacy exit。之后每个成员绑定同一integrated generation并分别进入`VERIFIED`。

不得把普通前置条件降低为`IMPLEMENTED`。Activation cohort必须登记cohort identity、唯一cutover requirement、成员ID、prepare receipt、共同generation、abort规则和all-or-nothing验证；任一成员失败则全体保持inactive，旧production truth不变。

状态推进使用Git review/base revision和schema validator，不建立Runtime式CAS/lease服务。任何source baseline、write set、owner、scoped decision或验收合同改变，都使旧的`IMPLEMENTED/VERIFIED`证据失配；记录必须推进revision并重新验证，不得原地覆盖证据。Epic/index没有writer lease、production状态或`IMPLEMENTED`状态，只聚合子requirement的投影。

每条记录至少包含：stable requirement ID、reviewed revision、owner domain、执行owner、状态、前置ID、精确write set、source baseline digest、scoped decision IDs、production recipe IDs、migration disposition、activation/cutover generation、旧路径退出receipt、证据清单、批准authority和verification instant。Ledger只能引用manifest identity，不得内嵌会造成摘要自引用的manifest。

### 4.7 Confirmed scoped decision绑定

本节列出的每个identity状态均为`CONFIRMED`。既有instance的reviewed revision是原评审第29–39节中声明该exact identity为CONFIRMED的段落revision；本轮新增instance的reviewed revision统一为`post-closure-implementation-r3-mechanical-closure`。Affected requirements是同行workstream在第5–16节列出的exact requirement集合；ledger必须逐ID展开保存，不能只保存workstream名称或本表行号。若找不到对应immutable reviewed revision，该instance按未确认处理并阻断相关requirement进入`ASSIGNED`。

| Workstream | 必须绑定的CONFIRMED instances |
|---|---|
| Permission/Hook/Sandbox/runner | `D14-published-tool-permission-applicability-v1`、`D15-runtime-hook-generation-v1`、`D04-tool-process-sandbox-profiles-v1`、`D13-fixed-internal-argv-execution-v1` |
| Connection/Notebook | `D16-product-connection-close-settlement-v1`、`D20-notebook-stdin-incarnation-v1`、`D07-product-connection-and-notebook-bounds-v1` |
| LSP/Presentation | `D09-lsp-3.17-code-map-profile-v1`、`D12-presentation-view-event-closed-generation-v1`、`D07-lsp-code-map-profile-bounds-v1`、`D07-product-code-map-projection-bounds-v1` |
| Event/Daemon | `D01-event-subscription-state-v2`、`D02-event-subscription-dlq-retention-v1`、`D03-event-subscription-dlq-delete-authority-v1`、`D07-event-subscription-bounds-v1`、`D19-event-subscription-dlq-replay-v1`、`D06-inference-daemon-single-generation-upgrade-v1`、`D07-inference-daemon-local-cleanup-bounds-v1`、`D10-inference-daemon-discovery-corruption-v1` |
| Workflow | `D11-workflow-effect-reconciliation-v1`、`D11-workflow-effect-run-journal-separation-v1`、`D01-workflow-reconciliation-v2-to-v3`、`D01-workflow-effect-run-journal-cutover-v1`、`D02-workflow-effect-retention-v1`、`D03-workflow-effect-disposition-and-purge-authority-v1`、`D07-workflow-effect-reconciliation-bounds-v1` |
| Cron | `D01-cron-schedule-v2-to-v3`、`D02-cron-occurrence-retention-v1`、`D03-cron-occurrence-disposition-and-purge-authority-v1`、`D07-cron-schedule-and-occurrence-bounds-v1`、`D08-cron-trusted-local-store-v1` |
| Agent ingress | `D19-agent-ingress-owner-separation-v1`、`D23-agent-delivery-turn-atomic-acceptance-v1`、`D01-agent-delivery-v1-to-v2`、`D01-agent-turn-queue-v1-to-v2`、`D01-agent-mailbox-projection-cutover-v1`、`D02-agent-delivery-retention-v1`、`D02-agent-turn-retention-v1`、`D03-agent-delivery-turn-disposition-and-purge-authority-v1`、`D07-agent-delivery-turn-bounds-v1`、`D07-agent-scheduler-weight-priority-bounds-v1` |
| Model execution/checkpoint | `D01-model-checkpoint-persistence-v1`、`D02-model-call-checkpoint-retention-v1`、`D03-model-call-checkpoint-purge-authority-v1`、`D07-model-call-checkpoint-bounds-v1`、`D01-run-journal-think-cutover-v1`、`D02-model-call-session-projection-retention-v1` |
| OAuth | `D21-oauth-credential-owner-and-backend-v1`、`D11-oauth-refresh-and-revocation-effect-v1`、`D01-oauth-credential-v1-to-v2`、`D02-oauth-credential-retention-v1`、`D03-oauth-credential-command-and-purge-authority-v1`、`D07-oauth-credential-bounds-v1` |
| Run domains | `D34-run-journal-domain-split-v1`、`D11-runtime-tool-effect-reconciliation-v1`、`D01-run-journal-tool-cutover-v1`、`D01-run-journal-think-cutover-v1`、`D01-run-journal-timer-cutover-v1`、`D02-runtime-tool-effect-retention-v1`、`D02-model-call-session-projection-retention-v1`、`D02-session-timer-retention-v1`、`D03-run-journal-cutover-and-purge-authority-v1`、`D07-runtime-run-domain-bounds-v1` |
| ServiceCall | `D11-hosted-service-execution-capability-v1`、`D01-hosted-service-call-v2-to-v3`、`D02-hosted-service-call-retention-v1`、`D03-hosted-service-call-command-and-purge-authority-v1`、`D07-hosted-service-call-bounds-v1` |
| Artifact/Session deletion | `D01-artifact-ownership-and-deletion-v1-to-v2`、`D02-artifact-retention-v2`、`D03-artifact-and-session-deletion-authority-v1`、`D07-artifact-reachability-and-gc-bounds-v1` |
| Session rollout | `D01-session-rollout-v1-to-v2`、`D02-session-rollout-retention-v2`、`D03-session-rollout-command-and-purge-authority-v1`、`D07-session-rollout-bounds-v2` |

Wave 0 ledger必须保存上表每个instance的完整精确identity、reviewed revision、authority和affected requirements；不得使用通配符、family缩写或自然语言数量代替identity。

本轮新增scoped instances均为`CONFIRMED`，来源和scope固定如下，不得跨owner套用：

| Scoped decision | 来源与owner | 确认范围 | affected requirements |
|---|---|---|---|
| `D07-agent-scheduler-weight-priority-bounds-v1` | 已确认WDRR产品语义 + 当前Product scheduler profile；Product Agent governance owner | root weight default 1/hard 64、priority closed 0..3；只约束scheduler policy generation，不进入delivery wire codec | `R-W3-AGENT-INGRESS-001`、`R-W3-AGENT-TURN-001` |
| `D07-lsp-code-map-profile-bounds-v1` | D09有限LSP 3.17 profile + 当前adapter安全边界；Product LSP profile owner | frame16 MiB、decode depth64、response items10,000、pending1,024、query30秒；超限关闭/拒绝对应request，不把合法response按展示数量拒绝 | `R-W2-LSP-001`、`R-W2-LSP-002` |
| `D07-product-code-map-projection-bounds-v1` | 当前Product code-map展示配置；Product presentation owner | diagnostics每文件10/总30、render每文件12 symbols仅为projection truncation并返回truncated disposition，不是LSP decoder admission | `R-W2-LSP-003`、`R-W2-PRESENTATION-002`、`R-W2-PRESENTATION-003` |
| `D07-product-connection-and-notebook-bounds-v1` | D16/D20既有合同与当前lifecycle profile；Product connection/notebook owner | 第17.3节Connection、Notebook stdin数值；超限保持DRAINING或typed timeout，不能伪造关闭/回复成功 | `R-W2-CONNECTION-LIFECYCLE-001`、`R-W2-NOTEBOOK-STDIN-001` |
| `D01-model-checkpoint-persistence-v1` | D01 run-journal think cutover与ModelCall canonical terminal事实的scoped revision；Runtime ModelCall owner | canonical result/effect forward migrate、非authoritative projection retire、corrupt/unknown/conflict blocked；ABSENT仅在完整inventory证明无call/attempt时成立 | `R-W3-MODEL-CHECKPOINT-001`、`R-W3-MODEL-PERSISTENCE-MIGRATION-001` |
| `D02-model-call-checkpoint-retention-v1` | D02 model-call/session-projection retention的Model checkpoint scope；Runtime ModelCall owner | active/IN_DOUBT无TTL，完整terminal 90天，最小call/attempt/cost tombstone 1年，migration evidence 180天 | `R-W3-MODEL-CHECKPOINT-001`、`R-W3-MODEL-RECOVERY-001` |
| `D03-model-call-checkpoint-purge-authority-v1` | D03 RunJournal cutover/purge authority的Model scope；Runtime ModelCall owner + Product maintenance command | Model owner结算/compact/purge；Session与RunJournal无删除权；unknown只允许evidence-based closed owner action | `R-W3-MODEL-CHECKPOINT-001`、`R-W3-MODEL-COMPOSITION-001`、`R-W3-MODEL-RECOVERY-001` |
| `D07-model-call-checkpoint-bounds-v1` | D07 runtime run-domain bounds的Model scope；Product Runtime Model policy owner | 第17.3节Model行的capacity、inline response、scan、stream和compaction上限；AttemptBudget只可收窄且RunJournal不得增加 | `R-W2-MODEL-CONTRACT-001`、`R-W3-MODEL-CHECKPOINT-001`、`R-W3-MODEL-PERSISTENCE-MIGRATION-001`、`R-W3-MODEL-COMPOSITION-001`、`R-W3-MODEL-RECOVERY-001`、`R-W3-MODEL-PROJECTION-001` |

### 4.8 Workstream准入、cutover与关闭矩阵

下表是domain流水的最小DAG，不替代各requirement自身更细的依赖。`准入`全部隐含S0=`VERIFIED`；只读inventory可在S0前准备，但其结果必须在S0 source baseline上重新生成或验证后才能用于cutover。

| Workstream | production write准入 | cutover/activation前置 | 旧production路径退出点 | domain关闭条件 |
|---|---|---|---|---|
| Direct retirement | 独立consumer/export/write-set审计完成 | 所有真实consumer为零，删除不会改变已发布能力 | 删除切片本身，不建替代stub | public/export/docs/plugin残留为零且每个子requirement独立`VERIFIED` |
| Model | canonical request/result contract先`VERIFIED` | strict inventory、blocked report、inactive candidate read-back通过 | candidate manifest切换时立即停旧restore reader/writer | composition/recovery/crash与费用receipt证据通过，migration-only source按时钟单独管理 |
| Tool/Permission/Sandbox | compiled binding contract先行；触及共享pipeline按writer矩阵串行 | definition、permission、hook、sandbox、effect generation一致 | 新binding generation发布时删除raw/live/反射入口 | ToolExecutor为唯一chokepoint，deny/ask及`IN_DOUBT`负向证据通过 |
| Product surfaces | 对应typed contract和generation先行 | adapter strict decode与consumer迁移完成 | 新generation激活时删除裸mapping、fallback和无人观察task入口 | LSP/Presentation/Notebook/Connection分别满足第17.3节bounds及shutdown证据 |
| Agent ingress | delivery/turn共享contract先`VERIFIED`，两个store writer分别持lease | cross-store candidate、capacity reservation和prepare/bind/commit恢复通过 | v2 generation切换时立即停v1 reader/writer与mailbox truth | 两个owner的原子acceptance、stale fence、retention与投影重建均`VERIFIED` |
| Workflow | effect identity/capability先行 | v2/RunJournal inventory、inactive candidate和atomic manifest通过 | v3切换时停v2 recipe与application RunJournal writer | reconciliation、inspection、delivery、Temporal evidence和restart/takeover均通过 |
| BackgroundTask | W0只读验证通过后，W2分别取得自身write set | query snapshot、cleanup settlement、pin/generation gate完整 | 不存在durable旧store cutover；替代API合入时删除mutable/旁路入口 | W0、QUERY、CLEANUP、INTEGRATION全部独立`VERIFIED` |
| Cron | v3 identity/codec先行 | v2 inventory/candidate、occurrence policy、delivery Port通过 | v3 activation立即停v2 writer、mtime控制与旧identity生产入口 | command/reconcile/delivery/artifact及retention/bounds通过；migration-only source按180天管理 |
| OAuth | credential contract与backend binding先行 | secret-safe inventory、inactive vault/metadata、effect settlement通过 | v2 cutover立即停fallback、nullable-token writer与v1普通reader | secret最长24小时擦除、production退出和180天证据分别有receipt |
| RunJournal retirement | 三个target contract和单manifest协议先行 | tool/model/timer candidate均read-back，target owner分别准备 | 单manifest激活三个target后立即停legacy reader/writer/export | 三个target owner签收且legacy source仅剩migration evidence |
| ServiceCall | v3 identity/capability先行 | candidate/index/manifest、cancel/effect reconciliation通过 | v3切换立即停v2 reader/writer及owner/cancel/index旁路 | store/execution/reconcile/consumer及remote unknown结果证据通过 |
| Artifact/Session deletion | Edge/Hold/Deletion contract先行 | completeness manifest、inactive edge candidate和producer fixtures通过 | v2切换立即停v1 mutation/GC；workspace旁路在canonical GC可用后删除 | producer completeness、closure、fenced deletion、Session edge release全部通过 |
| Session rollout | v2 envelope/lifecycle先行，且Artifact edge contract可用 | inactive stream、edge、projection digest和manifest通过 | v2切换立即停v1 reader/writer与旁路恢复truth | append/replay/retention/projection、torn-write和blocked-read-only证据通过 |
| Event/Daemon | 各自strict contract/generation先行 | Event checkpoint/DLQ candidate或Daemon discovery generation通过 | 新generation激活时停旧writer/discovery cleanup路径 | Event无replay DLQ与Daemon single-generation cleanup分别`VERIFIED` |

Evidence retention到期只触发migration-only source/decoder的typed retirement command，不重新打开已退出的production recipe，也不作为domain进入`VERIFIED`的等待条件；domain只需证明retention clock、authority和到期删除路径已经可执行。

## 5. 确定性删除与局部边界收敛

### 5.1 确定性删除requirements

`R-W1-DEAD-SURFACES-001`仅作为无production-write权限的epic/index。实际写入必须由下列stable requirements分别取得writer lease：

1. `R-W1-001`：provider moderation retirement，删除无consumer方法和专用万能异常decorator；
2. `R-W1-002`：inference HTTP admin retirement，删除未激活surface及projection；
3. `R-W1-003`：Model client retirement，删除无consumer `LLMClient`；
4. `R-W1-004`：Runtime AES registry retirement，删除单项registry与反射默认；
5. `R-W1-005`：i18n registry retirement，删除import副作用和public mutation；
6. `R-W1-006-temporal`与`R-W1-006-squilla`：分别删除固定伪catalog/loader。

每项独立完成consumer、export、docs/example、stub、plugin和semver审计，独立登记D21 authority、write set和VERIFIED状态；不得创建替代Port、兼容stub或未来能力占位。

### 5.2 Model execution与checkpoint

1. `R-W2-MODEL-CONTRACT-001`：以现有finalized inference request/result和ModelCall identity为唯一模型边界，迁移所有consumer并删除同义client/Port；
2. `R-W3-MODEL-CHECKPOINT-001`：strict versioned checkpoint、timer和attempt state，区分ABSENT、CORRUPT、UNSUPPORTED、LEGACY、IDENTITY_MISMATCH与IN_DOUBT；
3. `R-W3-MODEL-PERSISTENCE-MIGRATION-001`：inventory每个inference JSONL/SQLite restore source，并执行唯一disposition：canonical result/effect evidence forward migrate；可确定重建且非authoritative的projection退役；corrupt、unknown或identity conflict保存为blocked evidence且禁止activation；candidate经read-back和manifest cutover后立即退出production旧reader，migration source/decoder只在evidence窗口存在；禁止清空、completed-result双读或新建空记录；
4. `R-W3-MODEL-COMPOSITION-001`：Product generation、routing、credential health、attempt budget和ModelCall journal使用同一request/attempt identity；
5. `R-W3-MODEL-RECOVERY-001`：验证wire action前后crash、stale owner、费用/usage receipt及Session projection，不因缺assistant message重新调用模型。

模型泛型关系必须从request/output contract保持到runtime/result，不得在gateway、factory、callback或checkpoint中退化后靠cast恢复。只有ABSENT可以创建新调用；损坏、未知或动作结果不明均fail closed。

## 6. Tool、Permission、Hook、Sandbox与进程

### 6.1 `R-W2-TOOL-BINDING-001`

Catalog只接收immutable compiled executable binding。Definition compiler在发布前完成schema、identity、generation、effect capability、permission targets、cleanup、argument decoder和invocation adapter验证。

- 删除raw tool、`wrapped_tool`、live mutable map和shape反射；
- ToolExecutor只消费generation-bound snapshot并成为唯一调用chokepoint；
- snapshot从Product批准的composition generation重建，不持有live closure作为唯一执行权；
- graph output走`GraphOutputContractSpec -> strict JsonValue contract -> OutputEngine -> ToolResult`，删除无consumer committer seam。

### 6.2 `R-W2-001`

- 所有published Tool进入authorization/sandbox/effect链；
- Product提供versioned permission baseline，缺失或malformed时deny；
- `NOT_APPLICABLE`仅用于无IO、无secret、无state mutation、非模型可选的内部纯计算；
- Hook在activation前编译为immutable generation snapshot，activation后禁止register mutation；
- control Hook只能单调收窄；timeout、crash、malformed和unknown decision fail closed。

### 6.3 `R-W2-SANDBOX-PROCESS-001`

固定argv、用户命令、interactive/daemon process使用不同typed runner。Sandbox profiles固定为：

- workspace read/write governed；
- networked governed；
- isolated compute。

Required control在activation后、spawn前或运行中失效均停止新动作并产生typed settlement；不得以trust bool、shell bool或fallback runner混合边界。Fixed argv仅保留async verified入口，环境变量使用最小Product schema。

## 7. Product typed surfaces

### 7.1 LSP

1. `R-W2-LSP-001`：定义LSP 3.17有限code-map profile、typed query/result/receipt；
2. `R-W2-LSP-002`：实现strict JSON-RPC/LSP adapter与capability activation；
3. `R-W2-LSP-003`：迁移code-map consumer，删除裸mapping和failure→空成功fallback。

### 7.2 Presentation与Notebook

1. `R-W2-PRESENTATION-001`：建立canonical scope declaration与strict codec；
2. `R-W2-PRESENTATION-002`：建立closed generation-bound ViewEvent catalog/disposition；
3. `R-W2-PRESENTATION-003`：迁移Structured、ACP、AG-UI和Textual adapter，删除`default=str`、反射和unknown silent drop；
4. `R-W2-NOTEBOOK-DOCUMENT-001`：Canvas/Notebook document/output使用strict discriminated union并绑定schema/document revision；
5. `R-W2-NOTEBOOK-STDIN-001`：stdin request/reply绑定document revision、kernel epoch、connection/human generation与expected revision，闭合cancel/restart/secret lifecycle；
6. `R-W2-NOTEBOOK-001`仅作为上述两个owner-specific requirements的epic/index，无production write lease；
7. `R-W2-ROLE-SURFACE-001`：Role只暴露最小typed command/query capability，不泄漏RoleComponents、wiring、store、registry和backend。

ViewEvent、widget tree、wire event和Notebook/ipynb均不是独立durable truth。恢复只能从verified Session stream、domain fact、connection cursor和Artifact edge重建。

### 7.3 Skill source与activation

`R-W2-SKILL-ACTIVATION-001`交付三个分离的authoritative对象：

- strict/frozen Product-owned SkillManifest；
- 绑定canonical path、content digest、trust decision和approval generation的source evidence；
- 绑定model/effort、tool binding generation、tokenizer identity和token cost的activated snapshot。

Prompt、fork和tool selection只消费activated snapshot。Raw frontmatter/metadata不能授权能力；非法context、model、effort、allowed-tools、path或digest在activation前fail closed。Fork只能单调收窄父级能力。不得由manifest构造过程硬编码模型计算token cost。

### 7.4 Connection与authoritative control

1. `R-W2-CONNECTION-LIFECYCLE-001`：Connection采用owner-local分阶段DRAINING cleanup，分别结算telemetry、human binding、projector与port；失败保留generation token并拒绝rebind/new turn；
2. `R-W2-AGENT-CONTROL-001`：interrupt、cancel、steer通过Agent/session control owner的typed command/receipt执行，Presentation不得使用无人观察的`ensure_future`伪装成功；
3. `R-W2-MCP-LIFECYCLE-001`：MCP generation activation、restore与prior-generation cleanup分别结算，失败保持不可接受新工作的状态；
4. `R-W2-CONNECTION-INTEGRATION-001`：各surface验证close、timeout、forced termination policy和shutdown，无吞异常或本地标记提前清除。

Connection delivery、Session control与Agent delivery保持不同owner；字段相似不得合并状态机。

## 8. Agent ingress v2

1. `R-W3-AGENT-INGRESS-001`：冻结delivery v2、turn v2、assignment/settlement transaction及strict codec；
2. `R-W3-AGENT-INGRESS-MIGRATION-001`：inventory delivery、Residency mailbox和turn v1；
3. `R-W3-AGENT-INGRESS-MIGRATION-002`：构造cross-store candidate与generation cutover；
4. `R-W3-AGENT-DELIVERY-001`：实现delivery command/query、capacity、bind/ack、retention和owner action；
5. `R-W3-AGENT-TURN-001`：实现PREPARED、ACCEPTED、claim/retry/cancel和settlement-prepared；
6. `R-W3-AGENT-INGRESS-RECONCILE-001`：覆盖prepare/bind/commit/execute/ack/terminal所有crash point；
7. `R-W3-AGENT-PROJECTION-001`：Mailbox、Residency snapshot、pending queue和wake降为有界可重建投影；
8. `R-W3-AGENT-INGRESS-SURFACES-001`：迁移Product、Cron、Workflow terminal和Agent communication；
9. V2 cutover与consumer迁移同一阶段立即退役v1 production reader/writer、裸enqueue/notify和旧mailbox truth；migration-only source/decoder脱离production recipe保留180天，到期后物理删除。

Acceptance协议固定为：

```text
turn PREPARE_ACCEPTANCE
  -> delivery BIND_TO_TURN
  -> turn COMMIT_ACCEPTANCE
```

Execution settlement固定为：

```text
turn EXECUTION_SETTLEMENT_PREPARED
  -> delivery ACK batch
  -> turn terminal commit
```

Delivery与turn是两个owner，不合并store；Mailbox不能推进canonical state。所有业务输入先成为durable delivery，TurnRequestId绑定target/root/subtree、generation、有序delivery tuple、payload digest和scheduler generation。

## 9. Workflow与BackgroundTask

### 9.1 Workflow

保留现有WorkflowRunStore/Control、definition catalog、create admission、caller fence、ProductWorkflowDurability和独立execution ownership。

1. `R-W0-WORKFLOW-GOVERNANCE-VERIFY-001`：证明现有owner、composition和唯一writer；
2. `R-W3-WORKFLOW-EFFECT-001`：冻结EffectId、四类capability、evidence/settlement；
3. `R-W3-WORKFLOW-MIGRATION-001`：完成v2/RunJournal只读inventory、candidate和conflict报告；
4. `R-W3-WORKFLOW-MIGRATION-002`：完成atomic cutover、receipt和activation barrier；
5. `R-W3-WORKFLOW-EFFECT-003`：在migration cutover后激活v3 canonical store、fenced attempts、retention和owner-action command；
6. `R-W3-WORKFLOW-RECONCILIATION-001`：strict codec、identity conflict、stale evidence和bounded reconciliation；
7. `R-W3-WORKFLOW-INSPECTION-001`：Product query返回immutable snapshot，resume只通过approved definition与fenced command；
8. `R-W3-WORKFLOW-DELIVERY-001`：terminal outbox调用destination canonical delivery Port；
9. `R-W3-WORKFLOW-TEMPORAL-001`：Temporal只提供attempt evidence并退出RunJournal writer；
10. `R-W3-WORKFLOW-GOVERNANCE-INTEGRATION-001`：验证restart、takeover、cancel、effect reconciliation和shutdown。

不得通过`graph_meta`、live WorkflowRun、state setter或process-local graph重新选择continuation；Workflow执行不得进入BackgroundTaskPool。

### 9.2 BackgroundTask

保留每Agent/process-local `BackgroundTaskPool`、BackgroundTaskOwner、LocalTaskReference、AttemptId、`ACTIVE -> DRAINING -> CLOSED`、work pin和Role eviction gate。

1. `R-W0-BGTASK-GOVERNANCE-VERIFY-001`：只读证明每Agent唯一pool、composition及现有lifecycle；该验证通过不代表query/cleanup/integration已经交付；
2. `R-W2-BGTASK-QUERY-001`：mutable TaskMeta改为frozen snapshot/settlement；
3. `R-W2-BGTASK-CLEANUP-001`：typed result retirement、output ownership、notification/resource settlement后释放pin；
4. `R-W2-BGTASK-GOVERNANCE-INTEGRATION-001`：验证supervisor只通过窄admission/cancellation Port协作。

不得建立共享task registry、进程singleton pool或durable task store。跨进程恢复工作在提交前进入WorkflowRun。

## 10. Cron v3

1. `R-W3-CRON-001`：定义v3 TaskId/generation、occurrence、tombstone、strict codec和policy；
2. `R-W3-CRON-002`：v2→v3 forward-only migration；
3. `R-W3-CRON-003`：typed command/query、deletion/operator authority与retention；
4. `R-W3-CRON-004`：revision-driven reconcile、bounded claim，删除mtime hot reload；
5. `R-W3-CRON-DELIVERY-001`：trigger接入Agent delivery并保存receipt；
6. `R-W3-CRON-ARTIFACT-001`：大payload/evidence接入Artifact edge；
7. v3 activation同一切片立即删除v2 production reader/writer、旧identity入口和mtime控制路径；只允许migration-only source/decoder脱离production recipe保留180天，到期后由typed retirement command物理删除。

Cron store为当前OS identity独占的trusted-local authority；不承诺抵抗同identity或root离线修改。Scheduler使用5-field cron、IANA timezone、fold取最早、gap跳过、misfire fire-once、overlap forbid。Occurrence、retry、scan、storage和retention严格执行第17.3节Cron合同。

## 11. OAuth credential v2

1. `R-W3-OAUTH-001`：冻结CredentialSubjectId、metadata closed state、SecretRef、backend binding和strict provider projection；
2. `R-W3-OAUTH-MIGRATION-001`：inventory selector/file/keyring/config/vault并生成secret-safe conflict报告；
3. `R-W3-OAUTH-MIGRATION-002`：构造inactive vault/metadata candidate并atomic cutover；
4. `R-W3-OAUTH-STORE-001`：metadata唯一owner、CAS/fence、vault inactive→published generation及borrow/pin；
5. `R-W3-OAUTH-EFFECT-001`：login/refresh/revoke intent、attempt evidence和IN_DOUBT；
6. `R-W3-OAUTH-COMMAND-001`：logout、backend migration、conflict、TTL、hold和security clear；
7. `R-W3-OAUTH-CONSUMER-001`：MCP/LLM只获得provider/account/scope/consumer-bound短时borrow；
8. `R-W3-OAUTH-SECRET-ERASURE-001`：新generation发布且borrow/refresh/revoke/rollback结算后，过期、rotated和v1可解密secret material立即eligible crypto-erase，正常最长24小时；hold不得无故延长非必要bearer plaintext；
9. `R-W3-OAUTH-PRODUCTION-PATH-RETIRE-001`：v2 cutover同一迁移切片立即删除production fallback、nullable-token writer和v1普通reader；不得等待migration evidence retention window；
10. `R-W3-OAUTH-MIGRATION-EVIDENCE-RETIRE-001`：只保留secret-safe source digest、manifest、conflict和secure-erasure receipt，deployment cutover后180天再物理退休migration-only decoder/source evidence；不得保留可解密token。

OAuth JSON不得保存token或raw claims。ABSENT只为query结果；metadata状态固定为ACTIVE、REFRESHING、REAUTH_REQUIRED、REVOCATION_PENDING、REVOKED、MATERIAL_LOST、IN_DOUBT、OWNER_ACTION_REQUIRED、RETIRED。Refresh/revoke默认NON_REPLAYABLE，stale response不得发布generation。

ABSENT必须通过四项机械门禁：metadata strict decoder遇到持久化`ABSENT`必须拒绝为unknown state；query只能在canonical metadata、vault binding、legacy inventory和未结算effect均证明不存在subject时返回typed `ABSENT`；migration的零source disposition只生成inventory receipt，不写ABSENT record或tombstone；logout/erase/purge必须写对应terminal metadata或retirement tombstone，不得用ABSENT掩盖曾存在的identity、费用、revoke或secure-erasure事实。任一inventory不完整、backend不可读或identity冲突时返回typed unavailable/conflict并fail closed，不能返回ABSENT。

## 12. RunJournal拆分与退役

1. `R-W3-RUN-DOMAINS-001`：冻结Tool effect、ModelCall→Session projection、Session timer contract；
2. `R-W3-TOOL-EFFECT-001`：ToolExecutor拥有typed effect lifecycle；
3. `R-W3-MODEL-PROJECTION-001`：ModelCall terminal到Session message使用intent/ack，禁止恢复重付费；
4. `R-W3-SESSION-TIMER-001`：独立timer identity、deadline、misfire和recovery；
5. `R-W3-RUNJOURNAL-MIGRATION-001`：完整strict inventory与三类conflict报告；
6. `R-W3-RUNJOURNAL-MIGRATION-002`：三个inactive candidate与单Session manifest cutover；
7. `R-W3-RUNJOURNAL-CONSUMERS-001`：删除RunJournal config、StepRecord、kind/status字符串API和全量reap；
8. `R-W3-TEMPORAL-RUNJOURNAL-RETIRE-001`：删除application Workflow writer；
9. `R-W3-RUNJOURNAL-RETIRE-001`：cutover时立即退出production reader/writer、module export和composition入口；source及migration-only decoder作为evidence保留180天后物理删除。

`AppendOnlyLedger`若保留，只能作为domain-private存储mechanism。FileOps、Session event、ModelCall、ServiceCall和Event journal不因名称相似而合并。

## 13. Hosted ServiceCall v3

1. `R-W3-SERVICE-CALL-001`：冻结CallId/preimage、capability、closed lifecycle、receipt、fence和index projection；
2. `R-W3-SERVICE-CALL-MIGRATION-001`：inventory JSONL/owner/cancel/index并生成capability downgrade/conflict报告；
3. `R-W3-SERVICE-CALL-MIGRATION-002`：构造inactive v3 candidate/index/manifest并single-generation cutover；
4. `R-W3-SERVICE-CALL-STORE-001`：canonical command/query、CAS/fenced claim、cancel facts、retention和Artifact edge；
5. `R-W3-SERVICE-CALL-EXECUTION-001`：capability-controlled submit/poll/cancel及stale evidence handoff；
6. `R-W3-SERVICE-CALL-RECONCILE-001`：bounded canonical scan、可重建pending index与owner action；
7. `R-W3-SERVICE-CALL-CONSUMERS-001`：迁移ToolExecutor/Product，删除caller-controlled semantics与public journal mutation；
8. `R-W3-SERVICE-CALL-RETIRE-001`：cutover时立即退出v2 production reader/writer、owner/cancel旁路和旧index；migration-only decoder/source脱离production recipe保留180天，到期后物理删除。

Caller deadline只终止等待，不终止remote operation。Cancel是独立effect；timeout或unknown保持CANCELLATION_IN_DOUBT。Pending index只保存CallId/revision/state/cursor，不保存独立payload/receipt truth。

## 14. Artifact ownership与Session deletion

1. `R-W3-ARTIFACT-EDGE-001`：冻结Artifact、closed typed Edge、Hold、DeletionCommand/Claim/Receipt和completeness manifest；
2. `R-W3-ARTIFACT-MIGRATION-001`：联合inventory SQLite/CAS/Session/FileOps/outbox/root/pin并生成orphan/conflict报告；
3. `R-W3-ARTIFACT-MIGRATION-002`：构造inactive edges/holds/tombstones/completeness manifest并v2 cutover；
4. `R-W3-ARTIFACT-STORE-001`：在现有store实现edge、CAS、hold、retention和bounded index；
5. `R-W3-ARTIFACT-DELETION-001`：实现fenced staged deletion与IN_DOUBT恢复；
6. `R-W3-SESSION-DELETION-001`：Session lifecycle fence与自身edge release；
7. `R-W3-ARTIFACT-CONSUMERS-001`：仅协调Artifact Port、producer completeness fixture和集成验收；每个Workflow/BackgroundTask/Tool/Model/ServiceCall/delivery/FileOps/publication owner通过自己的consumer requirement修改本domain producer源码，本项无跨域write lease；
8. `R-W3-ARTIFACT-GC-001`：generation-complete closure与bounded cursor GC；
9. `R-W3-WORKSPACE-CLEANUP-RETIRE-001`：删除mtime/stamp/参数式hold和直接remove_tree/reclaim。

删除状态机：

```text
REQUESTED -> CLAIMED -> REFERENCES_RELEASING -> METADATA_TOMBSTONED
          -> BLOBS_RECLAIMING -> DIRECTORY_RETIRING -> SETTLED
```

Artifact owner独占edge/hold/claim/blob GC；Session及其他domain只释放自己的edge。Unknown producer、incomplete generation、active edge、hold或stale claim均阻断删除。EPHEMERAL/SESSION默认保留24小时/30天，PROJECT不按无访问自动删除；deletion tombstone保留1年。

## 15. Session rollout v2

1. `R-W3-SESSION-STREAM-001`：冻结v2 envelope、lifecycle generation、closed SessionEvent、Artifact binding与typed errors；
2. `R-W3-SESSION-MIGRATION-001`：inventory rollout/directory/lease/checkpoint/Artifact root并生成strict conflict报告；
3. `R-W3-SESSION-MIGRATION-002`：构造inactive v2 stream/Artifact edges/projection digest并manifest cutover；
4. `R-W3-SESSION-STORE-001`：append CAS/fence、bounds、typed query和bounded replay；
5. `R-W3-SESSION-RETENTION-001`：terminal eligibility、compaction、tombstone、delete authority与recovery；
6. `R-W3-SESSION-PROJECTIONS-001`：checkpoint、FileOps、machine events、connection和Notebook只从verified v2重建；
7. `R-W3-SESSION-LEGACY-RETIRE-001`：cutover时立即删除v1 production reader/writer和旁路恢复truth；v1 source与migration-only decoder脱离production recipe保留180天，到期后物理删除。

Session rollout是唯一Session durable truth。SessionMetaEvent只能为sequence 1；stream identity不能被目录覆盖。大payload通过ArtifactRef与typed edge持有。Active/draining/recovery或任一effect/delivery/Workflow/approval未结算时无TTL；terminal且闭合后完整stream保留30天，tombstone保留1年。

## 16. Event、Daemon与Runtime maintenance

### 16.1 Event subscription

`R-W3-EVENT-001`交付subscriber lease/fence、strict v2 codec、checkpoint/effect settlement、无replay DLQ、30天完整内容/180天tombstone、bounded scan与Product maintenance authority。DLQ不提供re-admit、checkpoint rollback或旧handler重放。

### 16.2 Inference daemon

`R-W3-DAEMON-001`交付strict discovery、generation-safe local cleanup、single-generation upgrade、30秒drain和10秒terminate。Discovery corruption不建立业务quarantine；只有current owner可清理stale local resource。

### 16.3 Runtime maintenance拆分

删除generic `RuntimeMaintenance`和`schedule_reconciliation(name, callback)`：

- code-map scan归repository advisory owner；
- Artifact/Session cleanup归各自destructive authority；
- Workflow、ServiceCall、Event等reconciliation归各domain；
- Skill/MCP reload归Product activation generation；
- 不建立新的MaintenanceManager或通用CoordinationGate。

## 17. 全局容量与时间规则

### 17.1 Durable migration disposition

| Domain/source | 唯一target disposition | Conflict/corruption | production退出 |
|---|---|---|---|
| Agent delivery v1 | ACCEPTED保持eligible；CLAIMED→DELIVERY_IN_DOUBT；ACKED/DLQ保留terminal | generation不明→OWNER_ACTION_REQUIRED | v2 manifest cutover立即停v1 reader/writer |
| Residency mailbox | 唯一匹配delivery→cursor/evidence；无delivery但current且唯一→LEGACY_IMPORTED+OWNER_ACTION_REQUIRED | stale只作evidence；冲突阻断该Agent | projection cutover立即停旧mailbox truth |
| Turn v1 | terminal保留；完整ACCEPTED补assignment；CLAIMED→EXECUTION_IN_DOUBT | 缺delivery→ACCEPTANCE_IN_DOUBT；重复归属阻断 | v2 cutover立即停旧turn reader/writer |
| Workflow reconciliation v2 | strict terminal保留；AVAILABLE无contract→LEGACY_CONTRACT_REBIND_REQUIRED；CLAIMED/open→IN_DOUBT | unknown/duplicate/invalid全量rollback | v3 cutover立即停v2生产recipe |
| application RunJournal | 唯一receipt→attempt evidence；合法无关联→ARCHIVE_ONLY | conflicting identity/receipt→IN_DOUBT/OWNER_ACTION_REQUIRED | Temporal writer在cutover时退出 |
| Cron v2 | 新TaskId≥128-bit；legacy进入`legacy-v2` namespace；ACCEPTED/REJECTED/INTENT/DEFERRED保留；DISPATCHING→IN_DOUBT | orphan/duplicate/unknown/invalid阻断全量 | v3 activation立即退出mtime/v2生产路径 |
| OAuth selector/file/keyring/config/vault | 零source→query ABSENT；单合法source冻结binding；`token=None`→REVOKED；一致多source按Product policy保留一个 | material missing→MATERIAL_LOST；差异→BACKEND_CONFLICT；config差异→CONFIG_STORE_CONFLICT | cutover同切片退出fallback/writer/read path |
| RunJournal tool/think/timer | Tool STARTED默认IN_DOUBT；think关联ModelCall；timer future→PENDING、past→MISFIRE | unknown kind/status、fork、坏中段阻断整个Session | 单manifest激活三个target后停旧reader/writer |
| ServiceCall v2 | PLANNED无attempt→INTENT_COMMITTED待activation；STARTED无receipt→IN_DOUBT；receipt open→WAITING_REMOTE；terminal保留；`.cancel`→legacy CancelCommand；owner→evidence/min generation | unknown receipt state→OWNER_ACTION_REQUIRED；orphan cancel/identity冲突阻断call | v3 cutover立即停JSONL/owner/cancel/index生产路径 |
| Artifact v1 | 可证明owner→typed edge；未知owner→ORPHAN_QUARANTINED | missing blob/digest/owner/outbox/manifest conflict fail closed | v2 generation切换立即停v1 mutation/GC |
| Session rollout v1 | 每event strict decode/re-encode；大payload→Artifact candidate+edge；保留EventId/sequence/instant | checksum、committed corruption、identity/meta冲突→MIGRATION_BLOCKED只读 | manifest/CAS cutover立即停v1 production reader/writer |
| Event subscription v1 | checkpoint/DLQ迁入fenced generation；DLQ无replay | unknown/corrupt阻断该subscription | v2 cutover立即停v1 writer |

Migration source与migration-only decoder可以在证据窗口内保留，但不能进入production recipe。物理retirement期限见下表。

### 17.2 Retention与command authority

| Domain | Active/unknown | terminal完整事实 | tombstone/evidence | command/purge authority |
|---|---|---|---|---|
| Agent delivery/turn | active、IN_DOUBT、owner action无TTL | delivery 30天；turn 90天 | tombstone 1年；migration proof 180天 | current delivery/turn owner；Product maintenance、hold、security分别授权 |
| Workflow effect | unresolved/IN_DOUBT/hold无TTL | 90天 | tombstone/irreplaceable evidence 1年；archive/source 180天 | current Workflow owner；operator action closed set；maintenance/hold/security分离 |
| Cron | active task/unsettled/IN_DOUBT无TTL | occurrence 30天 | occurrence/task tombstone 180天；proof 180天 | Product Cron command；TTL/hold/security/offline repair分离 |
| OAuth | active/lost/IN_DOUBT无TTL | terminal non-secret metadata 90天 | tombstone 1年；proof 180天；secret正常≤24小时 | Runtime metadata owner；login/refresh/logout/revoke/migration/TTL/hold/security分别授权 |
| ServiceCall | unresolved/IN_DOUBT/cancel unknown无TTL | 90天 | tombstone 1年；v2 proof 180天 | current ServiceCall owner；Tool/Product只提交command；maintenance/hold/security分离 |
| Artifact | active edge/hold/IN_DOUBT无TTL | EPHEMERAL 24小时；SESSION 30天；PROJECT无自动TTL | deletion tombstone 1年；orphan/proof 180天 | Artifact owner物理删除；各domain只释放自己的edge |
| Session | active/draining/recovery/pending refs无TTL | 完整stream 30天 | tombstone 1年；v1 proof 180天 | Session lifecycle owner；user/TTL/security/hold/test分别授权；CAS由Artifact owner删 |
| Event subscription/DLQ | active effect/IN_DOUBT/hold无TTL | 完整内容30天 | tombstone180天 | Product maintenance command，Runtime current fenced owner执行；无用户逐条删/replay |
| RunJournal target domains | 按Tool/Model/timer各domain | Tool/Model 90天；timer30天 | Tool/Model 1年；timer180天；source180天 | 各target owner；migration authority只退役legacy source |

Owner action均为closed typed command，不存在`retry_anyway`、`force=True`、force-ack、delete-unknown或通过目录缺失宣称成功。

### 17.3 Versioned D07 bounds

所有表项由对应versioned Product policy拥有；Runtime、consumer和extension只能收窄。表中同时给出default与hard max时，Product配置只能处于该闭区间；只给出一个数值时，该值是当前profile的固定上限，不是留给实现者选择的默认。超限返回与操作语义匹配的typed `BACKPRESSURED/REJECTED/LIMIT_EXCEEDED`且不写durable accepted，不截断，不驱逐既有事实。

| Domain | Default | Hard max/固定上限 | Scan/retry/time | Storage/retention关联 |
|---|---|---|---|---|
| Agent ingress | delivery target/root 1,000/10,000；active turn queue 1,000；root weight1 | target/root 10,000/100,000；turn 100,000；inline payload1 MiB；batch100/4 MiB；root weight64；priority closed 0..3 | prepare lease30秒；reconcile500 tx、100 deliveries/tx、5秒；projection500/target、2,000 total；Mailbox500；hint5,000；turn3 attempts，1/5/30秒；maintenance500/100/5秒；cancel batch500；deadline只在claim前以expected revision竞争terminal；WDRR cost固定1，root→subtree两级公平 | store soft256 MiB/hard1 GiB；delivery30天、turn90天、tombstone1年 |
| Workflow effect | unresolved10,000 | unresolved100,000；command1 MiB；receipt64 KiB | NO_EXTERNAL/IDEMPOTENT 3 attempts、1/5/30秒；attempt timeout1秒–5分钟；RECONCILABLE execute1/query12/24小时、单backoff≤1小时；NON_REPLAYABLE execute1；scan500；claim30秒 | terminal90天、tombstone/evidence1年 |
| Run domain — Tool effect | active/Session1,000 | active10,000；arguments1 MiB；terminal result/receipt64 KiB；frame2 MiB | reconcile500/5秒；NO_EXTERNAL/IDEMPOTENT最多3次、1/5/30秒；RECONCILABLE execute1/query12/24小时；NON_REPLAYABLE execute1 | stream soft/hard64/256 MiB；compaction1,000 identities、candidate64 MiB/5秒；terminal90天/tombstone1年 |
| Run domain — ModelCall/projection | active/Session100 | active1,000；inline response64 KiB，超限ArtifactRef；frame2 MiB | reconcile200/5秒；attempt上限由批准AttemptBudget给出且RunJournal不得增加；unknown wire attempt不自动重付费 | stream soft/hard64/256 MiB；compaction1,000 identities、candidate64 MiB/5秒；terminal90天/tombstone1年 |
| Run domain — Session timer | active/Session1,000 | active10,000；不得携带任意callback/blob；frame2 MiB | scan500/5秒；每timer最多一次canonical submission，unknown不自动再触发 | stream soft/hard64/256 MiB；compaction1,000 identities、candidate64 MiB/5秒；terminal30天/tombstone180天 |
| Cron | durable/session-local task50/50；unsettled store1,000 | task10,000/1,000；unsettled10,000；每task1 unresolved；prompt1 MiB；receipt64 KiB；horizon366天 | DEFERRED 8次，1/2/4/8/16/32/60/60秒；tick claim/create各100/5秒；maintenance500/100/5秒 | snapshot64/256 MiB；terminal30天、tombstone180天 |
| OAuth | subjects/integration32；borrow默认绑定当前operation/attempt deadline | subjects1,000；每subject1 active generation、1 mutation、64 borrows；borrow hard30分钟且generation切换/logout立即撤销；secret/claims64 KiB；provider body1 MiB；scope256×256；metadata256 KiB | connect10秒/response30秒；interactive10分钟/hard30分钟；device poll1–30秒且总≤30分钟；refresh attempt1；reconcile200/5秒；retire100 generations；unknown expiry24小时revalidate | terminal metadata90天、tombstone1年；secret正常≤24小时 |
| ServiceCall | unresolved root/deployment1,000/10,000；index backlog10,000 | 10,000/100,000；index100,000；request1 MiB；receipt/response64 KiB；OperationId/key512 bytes；identity256 chars | submit NO_EXTERNAL/IDEMPOTENT 3次1/5/30秒，其余1次；poll12、5秒–1小时、24小时；cancel1/query12/24小时；submit10/60秒，poll/cancel30秒；caller deadline1秒–30分钟；scan500/page256/5秒，repair10,000 | stream frame2 MiB、soft/hard16/64 MiB；store10/100 GiB；terminal90天/1年 |
| Artifact | edges/artifact1,000；edges/Session100,000 | 10,000/1,000,000；metadata64 KiB；edge evidence16 KiB | closure10,000 edges/5秒；deletion500、transaction100/5秒；blob retry3；repair10,000 paths；claim30秒 | store10/100 GiB；orphan/proof180天；tombstone1年 |
| Session | append1 fact；stream soft256 MiB | record semantic/storage1/2 MiB；batch100 facts/8 MiB；stream hard1 GiB或1,000,000 facts；deployment10/100 GiB | replay10,000 facts/5秒；migration100 Sessions、10,000 facts/Session/5秒；listing500；repair10,000 dirs；append lease30秒 | complete30天、tombstone1年、proof180天 |
| Event subscription | retry3、timeout30秒、backoff0.1–5秒、jitter0.2 | subscriptions65,536；retry100；attempt300秒；error16 KiB；DLQ page1,000；persist_every10,000（external effect固定1）；inline DLQ1 MiB | maintenance1,000、transaction100；retry3次1/5/30秒 | complete30天、tombstone180天 |
| Daemon cleanup | — | discovery64 KiB；candidate batch128 | 每candidate3次，0.1/0.5/2秒；batch10秒；upgrade drain30秒、terminate10秒 | 不建业务backlog/quarantine |
| Connection | close budget10秒 | application shutdown30秒；一个generation只允许一个active close owner | 每失败阶段3次，0.1/0.5/2秒；超时保持DRAINING | secret-safe leak receipt；不杀共享Agent/process |
| Notebook stdin | 每execution1 pending request | 等待≤当前execution deadline且hard600秒；interrupt grace5秒 | reply claim一次；相同值幂等，不同值conflict；restart/cancel/close立即撤销 | password plaintext不持久化；普通input仅intent/receipt且不自动重放 |
| LSP protocol profile | initialize10秒 | stdio frame16 MiB；recursive decode depth64、单response10,000 items；pending requests1,024；query hard30秒 | shutdown/exit各2秒；malformed frame关闭endpoint并结算pending | 仅profile generation内cache；非SUCCESS不得写cache |
| Product code-map projection | diagnostics每文件10、总30；render每文件12 symbols | 固定展示上限；超过返回typed truncated projection并保留source count/cursor | 不影响LSP decode/admission；consumer可请求下一页或窄化query | 非durable presentation projection，不删除或改写canonical LSP response |

LSP新增method、transport、encoding或扩大frame/depth/items必须创建新profile generation；当前profile只允许Product收窄。Activation contract要求任何更宽源码配置在启用前收窄到表中合同，不能将现状反向提升为hard max。Product projection的10/30/12只控制展示，不得让LSP decoder拒绝第11条diagnostic、第13个symbol或其他仍在protocol上限内的合法response。

- Persistent instant使用timezone-aware `AbsoluteInstant`及clock identity；
- process timeout、lock wait和backoff使用monotonic clock；
- 不持久化monotonic值，不用wall clock计算进程内耗时；
- bool不得作为int/revision/token，NaN/Infinity不得进入durable state；
- 大payload必须走canonical ArtifactRef及ownership edge，不截断后继续处理；
- hard capacity只能阻止新admission，不得删除已接受、held、IN_DOUBT或active事实腾位；
- poison、locked和未到期item不得阻塞同partition后续eligible work；
- extension只能收窄权限、预算、容量、deadline和capability，不得扩大Product批准范围。

各workstream必须把本文数值编码进对应versioned Product policy；本文是这些数值的实施合同，不能引用一个尚未落地或内容不同的policy反向覆盖本文。Runtime、caller和extension不得扩大，只能按表中规则收窄。

## 18. 最终验收

每个工作包关闭必须满足：

1. authoritative contract、owner、composition、lifecycle、persistence、observability和tests全部闭合；
2. production consumer已迁移，旧入口、production reader/writer、alias、export和错误能力文档残留为零；依赖旧入口/schema的fixture删除，仍能证明canonical migration、corruption或反例的fixture必须迁移并保留；
3. strict codec覆盖unknown version/tag、extra/missing、wrong primitive、identity/generation mismatch；
4. 两个owner竞争与takeover fixture证明stale owner不能commit；
5. 外部动作前失权、动作后失权、receipt commit失败均有确定性结算；
6. corruption、partial write、torn boundary、mixed generation和migration rollback有证据；
7. 声明规模下的capacity、scan latency、compaction和storage bounds通过；
8. Product可信blueprint完成construct、activate、restart/resume、shutdown smoke；
9. Artifact edge、effect、delivery、retention、hold和purge引用闭包完整；
10. requirement record为VERIFIED，绑定source baseline、reviewed revision、scoped decision、integrated source identity及有效证据。

最终全仓签收还要求：

- 五层逆向依赖和生产局部import为零；
- 所有authoritative集合与production对象图双向一致；
- 正式边界无未批准宽类型、反射和动态import；
- durable authority均具备restart、CAS/fencing、corruption、migration和retention证据；
- `ztest/architecture/`、全量Pyright、受影响完整测试及最终全仓测试完成；
- 无第二owner、第二store、第二execution path、兼容层或migration残渣。

只有批准authority的verification declaration与机械门禁同时成立，工作包才可关闭。

## 19. 非目标

- 不机械清零所有Pydantic `BaseModel`、JSON mapping或第三方SDK动态类型；
- 不把所有event合成全仓巨型union或永久event journal；
- 不把Workflow与BackgroundTask合并；
- 不把Artifact owner变成理解所有业务状态机的万能manager；
- 不为Presentation、wire、Notebook或cache建立第二durable truth；
- 不为未知未来需求增加plugin、feature flag、通用callback或配置轴；
- 不以兼容层、双读、fallback或长期migration reader换取短期上线；
- 不在本需求中授权丢弃用户数据、扩大网络/权限暴露、引入新付费或第三方依赖。
