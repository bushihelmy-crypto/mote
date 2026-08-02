# 《Mote 核心架构债务闭合实施计划》实施就绪复评

评审对象：`zdocs/core-architecture-debt-closure-requirements.md`  
评审基线：2026-08-01 当前工作区版本  
评审方式：完整文档复读、生产源码抽样反查、引用路径与依赖总账检查  
复评结论：通过，实施计划已达到可开工状态

## 1. 总体结论

当前版本已经实质吸收上一轮评审意见，不再是原先不可执行的里程碑总账。工作包重新连续编号，跨 owner 的 Agent/Cron durable 工作已拆分，每个工作包独立占一行，里程碑状态改为派生值，安全前置被提前，ADR-D1 至 ADR-D3 已确认，并建立了分层验证 suite。

最新版本已把 R0.9 的 Temporal external-effect guarantee 完整补齐：history只拥有state mutation，activity按幂等键/receipt reconcile/non-replayable分型，visibility只作发现优化，未知结果进入 `IN_DOUBT`，generic retry被禁止。R0.9 可以保持 `CONFIRMED`。

台账当前 96 个工作包全部为 `CONFIRMED`。ADR-D4 已确认 Product-owned deterministic rename、远端请求前完整 target planning/authorization/reservation、逐资产原子 publication 与 typed partial settlement；没有虚构 multi-path all-or-nothing 保证。R0.3 已增加 R0.1 durable ToolResult contract 硬前置，消除了临时 dict/私有 codec 风险。

最终机械证据一致：96 个工作包标题与 96 行台账一一对应，122 条显式工作包依赖边记录为拓扑无环，所有 D1–D5 ADR 已确认；当前文档显式引用的 177 个唯一生产 Python 文件路径全部存在。

建议状态更新为“实施就绪，可开工”；首个工作包仍须遵守台账硬前置，依赖未达 `DONE` 时只能进行只读复核。

## 2. 已解决的上一轮意见

以下意见已在当前实施文档中得到有效处理，不再构成阻断：

1. 工作包编号已连续，旧 R1.4a 和编号缺口已消除，并提供历史编号映射。
2. 原 R2.15 已拆为 spawn contract、turn admission、nickname reservation 和 ChildAgentHandle seam。
3. 原 R2.16 已按 Residency、Mailbox、Cron 三个 durable owner 拆分。
4. 第 10.5 节已改为每个 R 一行，里程碑状态明确由工作包状态派生。
5. R0.6 Hook 治理已显式依赖 R1.9 extension trust 和 R2.30 typed runner。
6. API-key helper、配置 trust、extension trust、OAuth 与 runner 已进入 M1 生产安全基础，不再被推迟到最终治理阶段。
7. Generation 签名、Hosted Service deadline 和 MCP source failure 已分别进入 ADR-D1、ADR-D2、ADR-D3。
8. 同名 model contract 已先完成 lifecycle/consumer 调查，再分别选择合并或改名投影。
9. 治理控制项已按静态架构、contract/codec、component integration、fault injection 和 composition hermetic suite 分类。
10. typed plugin loader 已要求具有 catalog/manifest authority、typed factory、activation 和 AST allowlist。
11. ADR-D1 已正式选择 content-digest-only contract，R0.4、控制项 17 和完成定义第 15 项均已删除虚假 signature 要求。
12. ADR-D4 已把 R0.3 的重复 target 与 batch settlement 固定为显式产品决定；R0.3 已退回 `DECISION_REQUIRED`，不再允许实现者自行选择覆盖语义。
13. 原 R0.8 已拆为本地 durable commit（R0.8）与跨 backend operation ownership（R0.9），并明确 Workflow 只消费 Contracts-owned Port。
14. Hosted Service 持续 reconciliation 已抽为 R1.25，拥有 scan、claim、scheduler lifecycle 与 Product composition，并成为 R1.21 的硬前置。
15. 原 R1.23 已拆为 workspace maintenance fenced owner（R1.23）和 Artifact pin registry（R1.24），依赖方向为 maintenance 消费 pin snapshot。
16. R2.1 已改为依赖 R0.9，明确 Runtime 只提供 operation ownership 机制、Workflow 状态机仍归 Orchestration，并删除未定义的 W1–W3 标签。
17. R2.1 已进一步拆成 definition（R2.1）、durable run（R2.47）和 reconciliation/effect settlement（R2.48）三个独立工作包，各自拥有状态和依赖。
18. R1.20 已把 Agent-owned BackgroundTaskPool lifecycle 抽为 R1.26；Residency 只依赖 typed Pool admission/pin/drain seam。
19. R2.42 已明确不实现 hot reload，完整 generation swap/drain 已抽为 R2.49 并补齐 trust、MCP、Agent/Tool catalog identity 前置。
20. R3.1 已只保留 error owner/re-export 清理，durable ErrorCode inventory/migration 已抽为 R3.6 并保持 `NEEDS_EVIDENCE`。
21. R2.16 已收窄为 process-local atomic permit，durable queue/fair scheduler 抽为 R2.50；ADR-D5 产品语义已确认，R2.50 仅保持 `NEEDS_EVIDENCE` 等待基础设施复用审计。
22. R2.28 已只保留 lineage/spawn saga；capacity、budget 与 subtree cancellation 分别抽为 R2.51、R2.52、R2.53。
23. R2.10 已补齐 R1.8、R1.9、R2.42 前置，并明确 wiring 只消费 Product canonical owner 生成的 approved immutable projection。
24. Shared execution identity/variant/owner record 已先抽为 R2.54；R0.5、R2.26、R2.37 均显式依赖并消费该 contract/verifier。
25. R1.10 已补齐 R2.27、R2.34、R2.43 前置；remote adapter 只消费 verified-load/fork projection，不读取或重算三个 canonical owner 的 durable identity。
26. R2.46 已改为无实施硬前置；Cron、Residency、Hosted Service、maintenance、File lease、operation ownership 与 Workflow durable run 反向依赖 clock contract，domain 调研仅作为只读 evidence。

## 3. 剩余阻断问题

### 3.1 R0.0 修复可能先于 extension trust 激活 checkout 能力

当前 `product/composition/container.py` 因不存在的 `MoteConfig` 导入而无法正常解析。R0.0 修复后，`ProductContainer.standard` 将重新进入当前 composition 中的 Agent、Hook 等 checkout discovery。与此同时，R1.9 的统一 extension provenance/trust gate 尚未实施，R0.6/R2.30 的 Hook runner 也尚未闭合。

当前关键依赖表只规定 R1.8 依赖 M0、R1.9 再依赖 R1.8，却没有限制 R0.0 修复后 standard composition 的激活范围。R0.0 的验收也只有最小依赖 import/construct，没有陌生 checkout 的 negative fixture。

这意味着 M0 可能把此前由 import failure 遮蔽的项目扩展路径重新变为生产可达，而安全 gate 要到 M1/M2 才完成。

修订要求：

- 将 R0.0 分成“模块可解析”和“standard composition 可激活”两个验收层次；
- 在 R1.9 完成前，canonical composition 对未经批准的 checkout Agent、Skill、Hook、MCP 必须显式 fail closed 或保持未激活；
- 增加陌生 checkout fixture，证明修复 MoteConfig 后不会注入模型内容、启动进程或建立外部连接；
- 将上述关系加入第 10.2 节，而不是只写在测试说明中。

### 3.2（已解决）R0.3 未定义多资产 canonical target 冲突与批次 settlement

源码中最终文件名在远端调用前已经可确定，但多个同类 item 可以使用相同 `filename`，缺省时还会共享固定默认文件名。当前实现通过 `asyncio.gather` 并发 materialize，因此多个资产可能映射到同一 canonical path 并同时写入。

R0.3 只要求枚举和授权所有目标，没有决定以下行为：

- 同一批次出现重复 canonical target 时拒绝、确定性改名还是显式覆盖；
- before-image、reservation 和 rollback 按单资产还是整个批次拥有；
- 远端全部成功而本地部分写入失败时，返回原子失败还是 typed partial settlement；
- 重试相同调用时如何避免覆盖另一资产或重复提交已成功文件。

只补 permission target 无法关闭这些并发和状态真相问题。

修订要求：在 R0.3 进入 `CONFIRMED` 前固定 target collision policy、transaction scope 和 tagged batch settlement，并增加重复 basename、默认名冲突、并发写、部分失败及重试测试。

### 3.3（已解决）R0.8 没有确定跨执行 backend 的 canonical ownership 实现范围

R0.8 同时要求 Tool pipeline、Jsonl backend 和 Temporal activity 使用 writer/operation lease、revision CAS 与 generation fence。目标不变量成立，但计划没有确定该 ownership primitive 的部署范围和实现 owner：

- workspace 文件 lease 只能自然覆盖共享同一文件系统的进程；
- Temporal worker 可能跨进程或跨主机，未必共享本地 ledger root；
- 若使用 Temporal、SQLite 或其他 durable backend，lease acquisition、renewal、expiry、takeover 和 fencing commit 的保证均不同；
- 当前文档没有记录应复用仓内哪个 lease primitive，或现有 FileLeaseCoordinator/其他 lease 为什么不能复用。

因此，“started durable commit fail closed”和“跨 backend operation ownership”目前不适合作为一个可直接实施的工作包。

修订要求：

1. 将本地 append/reap/decoder 原子性与 distributed operation ownership 拆成两个工作包；
2. 后者先明确 deployment scope、canonical storage、lease owner、renewal/takeover 和 fencing token lifecycle；
3. 通过消费方所需的最小 Contracts Port 连接 Jsonl 与 Temporal adapter；
4. 在复用决定中逐项核对现有 FileLeaseCoordinator、RunJournal、durable backend 和 Temporal ownership 能力。

### 3.4（已解决）ADR-D2 缺少持续推进 accepted receipt 的 canonical reconciler owner

ADR-D2 已明确：deadline 只停止本地等待，写入 `WAITING_REMOTE` 并返回 durable resume handle；canonical reconciler 必须继续拥有和结算 accepted receipt。R1.21 的验收也要求 reconciler 能重新发现并推进该调用。

但当前生产代码只有在 `RuntimeServiceGateway.execute()` 或显式恢复同一 call 时执行 `_reconcile_open_attempt`。计划尚未定义一个会持续或周期性扫描 `WAITING_REMOTE` 的生产 owner，也没有说明：

- 谁从 durable journal 枚举待 reconcile 的 call；
- reconciler 是 application、process、session 还是 workspace scope；
- construct/start/stop/recovery 的 composition owner；
- 多进程下如何取得 R1.22 的 per-call lease并续租；
- daemon/CLI 全部退出后，何时以及由谁重新发现远端作业；
- provider polling/backoff、预算、并发、公平性和永久失败如何结算。

R1.22 只规定“谁能拥有某个 call”，不会自动产生一个负责推进 call 的调度者。若没有该 owner，deadline 返回 handle 后仍可能留下无人管理的付费作业，与 ADR-D2 的核心承诺冲突。

修订要求：新增独立的 Hosted Service reconciliation 工作包，明确 durable query/scan Port、scheduler/supervision owner、scope、activation/shutdown、lease takeover、backoff/budget 和 Product composition。R1.21 必须依赖该工作包；仅允许“下次调用同一 id 时顺便恢复”不能满足持续 reconcile 承诺。

### 3.5（已解决）R1.23 同时拥有 workspace maintenance 与 Artifact pin registry 两个生命周期

R1.23 把两项相关但不同 owner 的能力放在同一工作包：

1. workspace cleanup/GC 的跨进程 maintenance lease、live session 排除和 fenced tree deletion；
2. Artifact CAS 的统一 pin registry，覆盖 cursor、stage、publication、checkpoint、transfer 和打开的 payload lease。

前者拥有 maintenance execution lifecycle，后者拥有所有 artifact reference publication/read lifecycle。Pin registry 即使完成，也可以被 cleanup、常规 GC 和其他 collector 消费；maintenance lease 即使完成，也不能替代各生产者原子 acquire/release pin。两者的 contract、消费者、故障注入和 composition scope不同。

将它们绑定为一个工作包会使多个 bounded context 同时改造才能签收，也难以确定唯一 Owner；这与第 10.1 节“跨多个 store/lifecycle owner 先拆分”的规则冲突。

修订要求：拆为“Artifact pin closure/lease registry”和“workspace maintenance fenced owner”两个工作包。先固定 pin contract及所有真实生产者，再让所有 collector 和 maintenance owner 消费同一 snapshot；分别验证 pin publication/read 竞态与 sweeper lease/ABA/活跃 session 排除。

### 3.6（已解决）R2.1 将 Workflow durable engine 的 effect/terminal 闭环交给 R0.8，却没有依赖边和 owner 投影

当前 R2.1 已扩展为完整的 durable Workflow 执行状态机，覆盖 definition、checkpoint/frontier、pause/resume、deadline、reconcile、effect receipt 和 terminal delivery。正文最后又规定“所有 mutation 的 fencing 及 effect/terminal delivery 闭环由 R0.8 共同签收”。

但 R0.8 的当前 owner 是 Runtime RunJournal/AppendOnlyLedger，直接服务 Tool pipeline、Jsonl backend 与 Temporal activity；Workflow run state、frontier 和 effect intent 的 canonical owner按架构约束位于 `orchestration/workflows/`。当前计划没有说明两者是：

- 复用同一 Contracts-owned durable operation Port；
- Workflow store 与 RunJournal 的显式投影/事务协调；
- 还是让 Runtime ledger 直接拥有 Workflow effect 状态。

第三种会把高层 Workflow 状态机下沉给 Runtime，违反 owner 边界。前两种则需要明确 commit order、identity mapping、fencing token 和恢复责任。

同时，第 10.2 节和 R2.1 台账前置都没有列 R0.8；当前只依赖 R1.4、R1.5。这使 R2.1 可以在 R0.8 未完成时进入实施或 DONE，与正文“共同签收”冲突。

修订要求：先确定 R0.8 拆分后的最小 durable operation/effect Port及其 Runtime 实现，Workflow 仅消费该机制而继续拥有 run/effect state machine；明确 intent、external action、receipt、terminal 的 commit order和 identity/fence 投影。将真实硬依赖加入第 10.2 节与台账，不能以“共同签收”替代依赖边。

### 3.7（已解决）R2.1 引用了未定义的 W1–W3，且已经超过一个不可拆工作包

R2.1 的验收要求“W1–W3 逐片”删除旧 state、registry、entry 和 Product consumer，但全文没有定义 W1、W2、W3 的范围、owner、前置或各自提交点。因此该验收不可执行，也无法映射到唯一台账。

正文实际至少包含以下可独立验证的变化：

1. canonical WorkflowDefinition compiler 与 content identity；
2. durable WorkflowRun state/frontier/checkpoint store 与 fenced mutation；
3. cancel、deadline、pause、resume 的状态机和竞态；
4. reconcile scan、claim、fairness、backpressure 与 poison disposition；
5. effect intent/receipt/terminal delivery 与 R0.8 的机制投影；
6. Product continuation registry、内存 execution owner 和旧入口删除。

这些事项虽属于同一 Workflow bounded context，但具有不同 contract 固定顺序、故障注入和迁移提交点。用一个 `CONFIRMED` 状态和一个最终验收承载全部内容，会使任何增量垂直切片无法独立落地；而正文自己使用 W1–W3 已经承认需要分片。

修订要求：正式定义并编号至少三个 Workflow 工作包，例如 definition/identity、durable run state machine、reconciliation/effect settlement；在台账记录真实依赖。若坚持单一编号，则必须删除 W1–W3 表述并证明整个改动能在一个无中间双路径的变更集中完成，这对当前范围并不现实。

### 3.8（已解决）R3.1 把错误 re-export 清理与全局 durable ErrorCode 协议迁移合并

R3.1 的标题和首段是错误 owner re-export 清零：逐 symbol 迁移 Product/Orchestration 消费者并删除 `runtime.errors` facade。后续却新增了全局 ErrorCode 拆分，要求盘点 durable journal、event、API/wire、artifact metadata 和 snapshot，建立 `namespace + code + schema_version + typed context` envelope，并完成存量数据 migration。

后者不是 re-export 清理的附属步骤，而是跨多个 domain 的 durable/wire schema 迁移，拥有独立的兼容窗口、decoder/upcaster、发布顺序和外部 ABI 决策。即使所有 Python import 已迁移，durable ErrorCode migration 仍可独立未完成；反之亦然。把两者绑定会造成多个协议 owner 共用一个状态和提交证据。

修订要求：R3.1 只保留逐 symbol owner 迁移与 facade 删除；另建 ErrorCode durable envelope/inventory 工作包，并按真实 domain 拆分 codec/migration owner。若没有存量或外部 ABI，必须先用证据确认后直接切换，不能在通用 cleanup 工作包中预设全局 migration。

### 3.9（已解决）R1.20 将 Agent residency owner 与 BackgroundTaskPool lifecycle owner 合并

R1.20 前半部分的 canonical owner 是 Agent residency/incarnation 状态机：它协调 eviction、rehydration、capacity、runtime map、mailbox delivery 和 logical termination。后半部分却进一步规定“该 incarnation 状态机同时拥有 BackgroundTask submit/drain gate”，并把 Pool 的 task admission、work pin、output、notification、AttemptId、跨 Pool reference 和有界 drain全部纳入同一工作包。

这与已确认的 BackgroundTask 边界存在风险：BackgroundTaskPool 必须由每个逻辑 Agent 独立拥有其 task registry、result、notification 和 cleanup；supervisor/residency 只能通过窄 typed admission、pin 和 cancellation/settlement Port 协调，不能成为第二个 pool lifecycle owner。

Residency 确实需要与 Pool 的 pin/drain 原子协调，但“共享 generation/fence”不等于“由 incarnation 状态机拥有 Pool submit/drain 状态机”。当前表述和单一验收无法证明不会形成双 task state truth。

修订要求：拆分为 Agent incarnation/residency 状态机和 Agent-owned BackgroundTaskPool drain/incarnation binding 两个工作包。由 Pool 保持 `ACTIVE/DRAINING/CLOSED`、task/attempt/result/notification 真相；residency 只消费 typed pin snapshot、begin-drain command 与 settlement receipt。补一条架构 gate，禁止 supervisor/residency 直接读写 Pool registry 或 task map。

### 3.10（已解决）R2.42 把 hot reload generation swap 放入 M0，却缺少 trust/catalog 前置

R2.42 的主题是唯一 Product application composition root，但正文又要求 hot reload 在既有 trust/approval 范围内构造 candidate、验证 capability 不扩大、执行 atomic generation swap、让旧 holder drain，并在 source/content identity 变化时重新 trust decision。

这些语义直接依赖：

- R1.9 的 canonical extension source/trust descriptor；
- R1.14 的 MCP generation atomic swap；
- R2.35/R2.40 的 Agent/Tool catalog identity 与 generation；
- 可能还依赖配置 generation 和 ApplicationReloadCoordinator 的 canonical owner。

但 R2.42 位于 M0，只依赖 R0.0；上述工作包位于 M1/M9。按当前依赖规则，R2.42 可以先实现并被 M0 签收，只能复制临时 trust/catalog/reload seam，或留下未闭合的 hot reload 验收。

修订要求：R2.42 只负责 scope matrix、pure construction、activation/reverse settlement 和唯一 application factory；将 application hot reload/generation drain 抽成独立工作包并依赖 R1.9、R1.14、R2.35、R2.40 等 canonical identity owner。若 hot reload 必须留在 R2.42，则 M0 和依赖图必须整体后移，不能继续作为最早 composition 基线。

### 3.11（已解决）R2.16 从原子 turn permit 修复扩张为一套未完成复用审计的 durable scheduler

R2.16 的源码事实是清晰的局部并发缺陷：`has_capacity()` 与无条件 `guard()` 分离，导致实际运行数越过上限。最小闭环需要原子 acquire/release、严格一次 settlement、取消/异常释放和公平唤醒。

正文却进一步要求：有界 durable queue、稳定 admission receipt、owner crash reclaim、lease/fence、root/tenant/subtree 公平、priority、deadline、retry、overload policy、best-effort wake 丢失后的 durable scan，以及 supervisor restart 恢复。这已经是一套完整的 durable turn scheduler/control-plane 状态机，而不是 limiter 修复。

计划没有先说明：

- 当前 `turn_scheduler`、pending delivery、lineage store、Cron/Workflow scheduler 或其他 admission primitive 哪些可以复用；
- queued turn 是否已经有明确的跨 supervisor 重启 durable 产品承诺；
- queue intent、message delivery intent 与 Agent mailbox 的 canonical identity 如何避免双排队；
- scheduler lease、scan cursor 和 fairness projection 的 owner、store 与 Product composition；
- 为什么这些机制必须与 atomic permit 在同一变更集交付。

修订要求：把原子 turn permit/release 保留为独立工作包；durable turn queue与公平 scheduler 另立工作包，先完成产品承诺、现有调度基础设施复用审计和 canonical queue identity 设计。前者只通过窄 admission Port 被后者消费，不能让修复 limiter 成为暗中建设第二 scheduler 的入口。

### 3.12（已解决）R2.28 将 lineage、spawn saga、容量、预算与 subtree cancellation 五套状态机合并

R2.28 标题是持久化多 Agent lineage identity，但正文同时拥有：

1. durable spawn request/saga 与 placement reconciliation；
2. lineage/path/nickname/incarnation/tombstone canonical facts；
3. logical、resident、turn 三类 capacity projection；
4. Token、成本、深度和能力 budget reservation/settlement/refund ledger；
5. subtree cancellation epoch、snapshot、逐 Agent command 与聚合 settlement。

这些能力共享 Agent/root/lineage identity，但并不共享全部状态机、store、生命周期和失败恢复。容量释放、预算退款和 cancellation reconciliation 都可以在 lineage 已固定后独立实现和验收。把它们合在一个 `CONFIRMED` 工作包中，会产生一个巨型 Agent governance owner，并使任何垂直切片都无法独立到达 DONE。

这也掩盖了真实依赖：turn cap 应依赖 R2.16；BackgroundTask cancellation 应依赖拆分后的 Pool drain Port；nickname reservation 应依赖 R2.17；resident incarnation 应依赖 R1.20；budget ledger 需要先搜索现有 cost/budget/lease infrastructure。

修订要求：至少拆为 durable lineage/spawn saga、capacity projections、budget ledger、subtree cancellation 四个工作包，并显式依赖 R2.16、R2.17、R1.20 和 BackgroundTask owner seam。共享的是 Contracts-owned identity/receipt，不是一个全能 Agent governance manager 或共享 mutable store。

### 3.13（已解决）R2.10 的 source trust/approved path 投影缺少 R1.8、R1.9、R2.42 前置

R2.10 前半部分是 Agent wiring 和 Runtime model-client context 类型化。正文后半部分又要求 Product 在注入前为 primary config path、secret predicate、用户/session/browser/oauth roots、Hook/MCP config 绑定 canonical source/path ownership、content digest、trust decision 与 approval，并解码为 approved path handle；Runtime/Agent 不得重新发现 checkout extension。

这些不变量不是 wiring 自己拥有的：

- canonical config source/path/trust 由 R1.8 拥有；
- Agent/Skill/Hook/MCP extension provenance/approval 由 R1.9 拥有；
- scope matrix、construction/activation owner 和 Application object graph 由 R2.42 拥有。

但 R2.10 台账只依赖 R1.2、R1.17。按当前顺序，它可以在上述 owner 完成前实施，只能自行构造 source digest、approval 或 path handle，形成第二套 trust truth；或者无法满足自己的 checkout negative fixture。

修订要求：R2.10 只定义消费者所需的窄 typed activation spec/Port，不自行拥有 trust/source discovery。将 R1.8、R1.9 和拆分后的 R2.42 construction scope列为硬前置；由 Product canonical owner产出已批准 immutable projection，Runtime 只消费。若 approved path/source projection需要独立 contract，放入其真实 Product/Contracts owner并由所有 wiring consumer复用。

### 3.14（已解决）R0.5、R2.26、R2.37 的 Shared execution contract 与授权依赖倒置

R0.5 要求 execution registry 原子保存 principal、credential/application scope、generation 和 artifact digest，并让每个 object RPC 及 WirePermit 使用同一 owner-binding verifier。完成该安全工作需要一个正式的 execution identity、variant、registry record 和 credential/permit binding contract。

但这些类型化边界被放在 R2.26：它才负责把 Shared backend、execution registry、finite/session execution variant 和 RPC adapter 从 `Any`/`hasattr` 收口。台账却让 R2.26 依赖 R0.5，即先在宽动态 registry 上实现安全 owner binding，再迁移为 typed registry，违反“先固定 Contracts，再实现 owner”的顺序，并扩大遗漏某个 variant/RPC 的风险。

同时，R0.5 明确要求 WirePermit 与 registry owner 共用验证函数，但 R0.5 没有依赖 R2.37 的 canonical epoch owner；R2.37 也没有依赖 R0.5 的 execution owner binding。两项可以分别被标记 DONE，却仍没有共同 verifier/identity chain。

修订要求：

1. 先从 R2.26 抽出最小 Shared execution identity/variant/owner-record contract；
2. R0.5 基于该 typed contract实现对象级授权和 durable ownership metadata；
3. R2.26 再完成 protobuf adapter、client/backend 和 variant dispatch 迁移；
4. R2.37 连接真实 epoch owner后，与 R0.5 共同完成 permit principal/generation/execution binding；
5. 在依赖表明确上述顺序，避免 R0.5 与 R2.26 形成事实上的循环迁移。

### 3.15（已解决）R1.10 的 remote Session load/fork 缺少 canonical identity owner 前置

R1.10 已明确要求 `load_existing` 通过 Session、Residency 和 definition identity 校验，并指出当前 mint-or-return 行为会掩盖 R2.27/R2.34 的 identity fail-closed。这个判断是正确的，但台账仍只让 R1.10 依赖 R1.2、R1.3，没有依赖真正拥有这些不变量的 R2.27 与 R2.34。

R2.27 才负责证明 residency file key、record、Role、rollout stream 与唯一 Session meta identity 一致；R2.34 才负责把 Markdown Agent 的 definition/source identity绑定到 Session/Residency 恢复身份。在二者完成前，ACP/AG-UI adapter 无法可靠区分 unknown、corrupt、definition mismatch 和 migration-required。若 R1.10 自行实现校验，会在 Product remote protocol 中复制 Session/Residency decoder 和 definition validator；若只增加 typed error 外观，则无法满足其自身 fail-closed 验收。

修订要求：将 R2.27、R2.34 登记为 R1.10 的硬前置。canonical Session/Residency/definition owner负责严格读取与身份判定，R1.10 只把 typed `load_existing`/`fork_existing` result 和 error 投影到 ACP/AG-UI 协议；不得在 remote adapter 中按路径重读 durable record、重算 definition identity 或建立第二套恢复 validator。若 R2.43 最终拥有统一 Session read model，应再明确 R1.10 是直接消费该 read model，还是由 R2.27 提供更窄的 verified-load Port，避免第三个查询入口。

### 3.16（已解决）R2.46 durable clock 的依赖方向与正文承诺相反

R2.46 的正文要求“在各 domain 固定时间字段前统一 absolute/monotonic、restart、rollback 与 DST 语义”，并将 clock 定义为 Contracts-owned 窄 Port。它是 Workflow deadline、Cron occurrence、lease expiry 和 cleanup/retention 的基础 contract，而不是这些 domain 状态机完成后的派生清理。

但第 10.2 节和台账当前让 R2.46 依赖 R1.12、R1.20、R1.23、R2.1、R2.21。按该顺序，Cron transaction/schema、Residency lifecycle、maintenance retention 与 Workflow definition 均可先于 clock contract 进入实施，各自固定 timestamp shape、restart 与 DST 行为；R2.46 随后只能做兼容迁移或重写已完成 contract。文档对 R2.47 的处理反而是正确的：Workflow durable run 依赖 R2.46。

修订要求：先区分真正需要参与 clock contract 设计的 evidence source 与实施硬前置，不能用依赖边表达“调研这些 domain”。R2.46 应在只读复核相关 owner 后先固定窄 clock Port、absolute instant/monotonic 使用规则和 record identity；R1.12、R1.20、R1.23、R2.21 及其他实际写入 deadline/expiry/retention/occurrence 的工作包反向依赖 R2.46。若某工作包只消费 monotonic process-local timeout而不持久化时间，应明确排除，避免把所有 timer 强行耦合到 durable clock。

### 3.17（已解决）R0.9 把 Temporal control ownership 误当成 external-effect fencing

新增 guarantee matrix 和 local/Temporal adapter 拆分方向正确，删除 process-local `StepHandlerRegistry` 也是必要修订。但当前文本仍把 workflow ID/run ID、history、activity attempt 和 visibility/query 组合描述为“跨 worker ownership、recovery 和 scan 事实”，随后要求两个 host 竞争时只有当前 owner“可执行并提交”。这里混合了两种不同保证：

- Temporal workflow history 可以串行化 authoritative command/state transition，并拒绝旧 attempt 的 completion；
- 已发出的 activity 是 at-least-once。旧 attempt 在 timeout、worker partition、cancellation 或 retry 竞态中仍可能已经调用外部 provider，workflow 后续拒绝它的 completion并不能撤销副作用。

`activity attempt identity` 不是外部系统可验证的 fencing token；visibility/search attribute 也通常是发现投影，不是 effect commit 的 authoritative store。若 provider 不支持稳定 idempotency key或按 EffectId 查询结果，仅以新的 activity attempt 接管会重复扣费/写入；若一律不接管，又必须把状态推进为可查询的 `IN_DOUBT`，而不是宣称新 owner可安全执行。

修订要求：

1. 将 Temporal workflow history/query 定义为 command/state owner；visibility 只作可丢失发现优化，恢复推进由仍在 Temporal server中的 workflow timer/retry或明确的 durable enumeration保证。
2. 每个 external-effect activity 在 intent commit后按 provider capability选择封闭策略：`IDEMPOTENT_BY_KEY`、`RECONCILABLE_BY_RECEIPT` 或 `NON_REPLAYABLE`。前两者分别要求稳定 EffectId key或先查询/对账；第三种在 dispatch 结果未知时只进入 `IN_DOUBT`，禁止自动 activity retry和新 owner重放。
3. workflow generation/attempt/revision只 fence 本地/Temporal state mutation。若声称能 fence 外部执行，必须有 provider或外部 durable effect store实际校验该 token；否则删除“旧 owner不得执行”的过强承诺，改为“迟到 mutation被拒绝，外部未知结果被对账”。
4. 明确 activity retry policy：只有无外部 effect、provider幂等或可先reconcile的 command允许自动 retry。generic activity 不得用统一 retry policy覆盖所有 effect classification。
5. fault-injection 增加“provider成功后worker断联、Temporal重试前旧activity迟到、无幂等provider timeout”三类场景，证明不双执行、不伪报未执行，或稳定返回 `IN_DOUBT`。

### 3.18（已解决）R0.3 typed partial settlement 缺少 R0.1 durable ToolResult contract 前置

ADR-D4 当前推荐逐资产 `committed/failed/in_doubt` tagged settlement，成功资产还要携带 canonical path/version/artifact reference，失败与 in-doubt 必须在 replay、resume 和 compaction 后保持语义。该结果最终由 GenerateMedia 的 ToolResult 生产路径承载。

R0.1 正在拆分 `ToolResult.data: Any`、durable payload、ephemeral value、deferred control result 与 artifact reference，并要求未注册类型 fail closed。若 R0.3 不依赖 R0.1，可以先定义另一套媒体专用 receipt codec、继续返回裸 dict，或先把 typed settlement塞进仍为 `Any` 的 ToolResult；三种路径都会在 R0.1 实施时产生二次迁移或双 contract。

修订要求：

- 在第 10.2 节与台账增加 `R0.3 -> R0.1` 硬前置；
- R0.1 先提供可承载 domain-owned tagged settlement 与 canonical artifact/path reference 的 durable ToolResult variant/codec seam，不把媒体状态机下沉给 ToolResult owner；
- R0.3 在 Product/FileOps owner中定义媒体 publication settlement，只把已注册 canonical DTO投影进 ToolResult；
- 增加 round-trip fixture，证明 committed/failed/in-doubt、asset identity、target version与artifact reference跨 receipt、rollout、resume 保持一致。

## 4. 进入实施前的最低修订条件

无。review 3.1–3.18 的所有结构、owner、拆包、产品决定、证据与依赖问题均已写回实施文档；96 个工作包全部为 `CONFIRMED`。

### 4.1 本轮证据审计处置

以下三个 `NEEDS_EVIDENCE` 已获得足够的只读结论，可在把证据写回实施文档后转为 `CONFIRMED`：

1. **R0.0**：`ProductContainer.standard` 在构造 dataclass 参数时立即求值 `agent_composition.factory` 与 `agent_composition.agents`；前者执行 source、Hook、MCP discovery，后者扫描 Markdown Agent。结论不是“现有 construct 已纯净”，而是必须先把 approved extension declaration/snapshot 与 activation/materialization 拆开，再修复 `MoteConfig -> Config`。该调用链已经把实施范围和 negative fixture 固定，不再需要额外产品决定。
2. **R2.50**：`runtime/inference/fair_queue.py::FairAdmissionQueue` 是进程内 `asyncio.Condition + deque`，使用 inference tenant/project identity、动态 cost/aging、float deadline，并且没有 durable revision、CAS、fence、claim receipt 或 restart scan。它不能成为 Agent queue backend；可以抽取的只有不持有 mutable queue state、以 immutable eligible snapshot 为输入的纯 WDRR selection policy。Mailbox、Cron 和 Workflow scheduler 的 identity/lifecycle 也不同，均拒绝直接复用。按此结论实施不会建立第二 mailbox 或跨层依赖 Inference concrete queue。
3. **R2.52**：仓内已存在 Contracts `UsageLedger`、`BudgetReservation/UsageSettlement` 与 Product SQLite implementation，拥有 reserve、settle、release、pending reconciliation、expiry reclaim 和 fencing 等核心事务不变量；`CostTracker` 只是 session 内累计/展示，`BudgetEvent` 是 observation，二者都不能作为 admission truth。Agent 不得重新实现同义 reservation ledger。应把可复用的 canonical reservation/settlement mechanism 抽到不绑定 inference tenant/project/attempt 的正确 Contracts/Runtime owner，再由 Inference 与 Orchestration 分别使用 domain identity/policy projection；Token、USD、depth、capability 使用封闭 dimension/unit，而非压成含义不明的单一 `units`。

R0.9 当时仍需决定跨主机 backend；最新文本现已选择 local FileLease/RunJournal 与 Temporal typed activity/history 两个 adapter，但其 external-effect replay safety 仍按第 3.17 节补齐。R3.6 的 ABI inventory 已在最新文本闭合。

### 4.2 R3.6 外部 ABI inventory 结论

R3.6 已获得足够证据，可在写回以下边界后转为 `CONFIRMED`：

- ACP/AG-UI 的 tool completion wire 不发送结构化 `ErrorReport` 或 ErrorCode enum；当前仅将 presentation `error_code` 作为失败文本 fallback。ACP JSON-RPC 的整数 error code 属于 ACP 协议自身，不是 Mote ErrorCode migration 对象。
- canonical inference OpenAPI 的 `error.code` 是 nullable string，但当前 `product/interfaces/inference_api/application.py` 固定发送 `code: None`；不存在已发布的旧 Mote ErrorCode enum 值需要兼容。字段本身作为 v1 wire schema 保留，R3.6 不借机删除或改变 shape。
- `contracts/events/application.py`、`contracts/events/inference.py` 与 presentation DTO 的 `error_code: str` 属于独立公共 contract candidate，应在本工作包中原样保留；未来若改为 domain envelope，必须由对应 wire/event owner单独版本化，不能被本地 durable discard 授权覆盖。
- destructive migration 只命中 ToolResult receipt、BackgroundTask attachment/notification 与精确识别的 Session ErrorReport variant；Artifact metadata、Secret、Workspace、ACP/AG-UI wire 和公共 DTO 均为 negative target，并用隔离测试证明未删除。

据此，R3.6 不再需要仓外产品决定；它是“本地 durable ErrorReport 直接切 strict envelope + 外部 surface 明确排除”的实施工作包。

### 4.3 R0.9 backend 决策建议

R0.9 不能用一个实现假装同时覆盖 local JSONL 与跨 host Temporal。建议固定两个 typed adapter，并让 Product 按 deployment 显式选择：

1. `local_shared_fs`：复用完成 R2.29/R2.46 后的 FileLeaseCoordinator、RunJournal expected revision 与 atomic append/rewrite，只承诺同一共享文件系统上的跨进程 ownership；filesystem identity 不同或锁能力不足时 activation fail closed。
2. `temporal`：删除 process-local closure/`StepHandlerRegistry` 作为 durable execution seam，改为 manifest 登记的 typed activity command。以稳定 EffectId/WorkflowId、Temporal history 和 attempt identity拥有调度；activity 前先有 durable intent，provider 调用携带稳定 idempotency key，result/receipt 以 expected revision/fence提交。activity timeout/worker loss而 provider结果未知时进入 `IN_DOUBT` 并 reconcile，绝不因 Temporal retry盲目重复外部动作。

Temporal activity 本身是 at-least-once，不能宣称单靠 workflow history提供 exactly-once 外部副作用。若 provider 不支持幂等查询/键且没有外部分布式 receipt/fence store，该 operation 在 Temporal backend 上必须 capability-unavailable/fail closed；不得静默回退 local JSONL。这个约束无需现在引入新数据库，也不会虚构跨主机保证。

最新实施文档已经采纳双 adapter 与 typed activity方向，但尚未把上一段的 provider idempotency/reconciliation/`NON_REPLAYABLE -> IN_DOUBT` 约束写入正式 guarantee，因此本节不能仅凭 backend owner 已选择而视为完全解决。

## 5. 进一步建议

### 5.1 将依赖表变成可机器检查的数据

当前第 10.2 节已经比上一版清晰，但仍是人工维护的 Markdown。建议至少增加一个架构测试解析工作包、状态和依赖，检查：

- 所有正文 R 编号在台账中恰好出现一次；
- 所有依赖目标真实存在；
- 依赖图无环；
- `IN_PROGRESS/DONE` 工作包的硬前置均为 `DONE`；
- `DECISION_REQUIRED` 必须引用存在且未决的 ADR；
- `CONFIRMED` 必须有 Owner 和证据链接。

该检查只解析文档或结构化 sidecar，不应导入 Product composition。

### 5.2 区分产品安全修复与全计划最终关闭

整份计划覆盖 50 余个工作包，最终关闭周期会很长。建议为 M1/M2 的安全切片定义独立发布准入：一旦 R1.7–R1.9、R1.15–R1.16、R2.30、R0.3、R0.6、R0.8 完成，就可单独证明配置、checkout extension、命令和本地副作用边界已经闭合，不必等全部类型整洁工作完成后才获得安全收益。

### 5.3 避免将所有架构控制都做成全库扫描

当前 suite 分类已经解决了上一版的主要问题。实施时仍应坚持：静态 gate 只证明静态可判定事实；权限目标一致性、lease 丢失、崩溃恢复和远端 settlement 由 bounded-context integration/fault-injection 证明，不能用名称扫描代替行为保证。

## 6. 源码与文档证据抽查

本轮确认：

- 文档引用的 `contracts/`、`kernel/`、`runtime/`、`orchestration/`、`product/` Python 路径均存在；
- `ProductContainer` 仍导入不存在的 `MoteConfig`，实际根类型为 `Config`；
- GenerateMedia 的同类多个 item 可以映射到同一最终路径并并发 materialize；
- ToolResult receipt 对未知对象仍持久化 `{type, repr}`，无法恢复原语义；
- 配置层除 WORKDIR 外均被视为 trusted，typed Config 构造前可执行字符串 `api_key_helper`，底层仍使用 `shell=True`；
- 两套 `CanonicalToolCall` 和两套 `EndpointCapabilities` 的定义与当前文档所述 consumer/lifecycle 分流方向一致；
- 当前版本已将 Cron schedule schema 与 receipt/fencing 建立显式依赖，不再重复拥有同一 schema 工作；
- 当前版本已将 Agent durable identity、catalog compiler 与 extension trust 分成独立工作包。
- R0.0 已诚实保持 `NEEDS_EVIDENCE`；R1.21 的 reconciler lifecycle 已由 R1.25 闭合。Owner 可按第 10.5 节在实际实施领取时填写，Owner 为空本身不作为本轮阻断。
- 文档的 96 个工作包标题与 96 行台账一一对应且 identity 唯一。按当前反引号生产 Python 路径口径复算仍为 174 个唯一路径、227 次匹配；统计口径仍应由脚本固定，避免文档编辑后漂移。
- 最新台账实际为 90 个 `CONFIRMED`、5 个 `NEEDS_EVIDENCE`、1 个 `DECISION_REQUIRED`；R2.50 的 ADR-D5 已确认，非确认状态只来自基础设施复用证据。
- R0.0 的 `NEEDS_EVIDENCE` 有源码依据：`ProductContainer.standard` 构造返回值时立即求值 `agent_composition.factory` 与 `agent_composition.agents`；前者直接执行 config source、Hook、MCP discovery，后者经 `builtin_agent_catalog` 扫描 Markdown Agent。当前不存在“只改 Config 名称即可纯构造”的现成 seam，必须先把 approved declaration 与 activation/lazy materialization 边界设计清楚。

## 7. 最终签收意见

当前状态建议标记为：`实施就绪，可开工`。

review 3.1–3.18 与 R3.6 新增内容均已通过复评。最终机械检查满足：96 个标题/台账 identity 一致、122 条显式依赖边无环、所有硬前置与 ADR 存在、所有工作包为 `CONFIRMED`、ADR outcome 与正文一致、显式生产 Python 文件路径无缺失。实施计划可开工；实施完成与全量投产仍需逐工作包达到 `DONE` 并执行对应测试，不等同于计划评审通过。

## 8. 本次复评范围

- 已读取当前完整实施文档及其评审处置、ADR、依赖边、台账与完成定义；
- 已检查所有显式引用的生产 Python 文件路径存在性；
- 已抽查 composition、配置 helper、媒体落盘、ToolResult codec、重复 model contract 和 Hosted Service gateway/reconcile 链；
- 仅修改本评审文件，未修改实施文档和生产代码；
- 未运行测试或 Pyright，本次结论只评价实施计划的就绪程度。
