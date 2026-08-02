# Package Cohesion 与 Service Boundary 债务治理实施规格 v2 评审

评审对象：`zdocs/package-cohesion-service-boundary-debt-governance-implementation-v2.md`  
日期：2026-08-01  
结论：**暂不批准作为生产实施规格。** 产品决定、38 项 SB disposition、迁移授权与 R2.50/ADR-D5 已基本闭合；当前阻断集中在 dependency DAG 和固定 slice contract 与核心总账不一致。修正本评审第 1 节后，可进入机械一致性复审，不需要重新讨论 ADR-D5。

本文件是 v2 的独立评审。已冻结的 `zdocs/package-cohesion-service-boundary-debt-governance-implementation-review.md` 不再追加，也不得用其中的历史中间判断覆盖当前用户决定、`AGENTS.md` 或核心总账。

## 1. 阻断问题

### 1.1 Agent 工作流依赖方向倒置并形成循环

v2 第 4 节定义：

```text
A0+P1+C1 -> AG1
AG1 -> AG2 -> AG3 + AG4
```

该顺序与核心总账的硬前置冲突：

- AG1/R2.28 lineage/spawn saga 必须先消费 R1.20、R2.17、R2.51 和 R2.52；
- AG3/R2.51 capacity 只依赖 R1.20、R2.16，不以 lineage 完成为前置；
- AG4/R2.52 当前仍为 `NEEDS_EVIDENCE`，必须先完成 cost/budget/quota/ledger 复用审计；
- AG2/R2.50 依赖 R2.16、R2.20、R2.29，以及尚未完成的 scheduler/queue 复用审计。

当前 DAG 把 AG1 放在 AG3/AG4 前，同时核心总账又要求 R2.28 消费 R2.51/R2.52 receipt，形成不可执行的环。应至少改为：

```text
R1.20 + R2.16 -> AG3/R2.51
budget reuse evidence -> AG4/R2.52
R1.20 + R2.17 + AG3 + AG4 -> AG1/R2.28

R2.16 + R2.20 + R2.29 + scheduler reuse evidence
  -> AG2/R2.50
```

AG1、AG2、AG3、AG4 是具有不同 owner 和前置的工作包，不能再表达成一条线性实施链。

### 1.2 `AG2–AG4` 错误合并三个 authoritative owner

v2 第 5 节把 scheduler、capacity 和 budget 合并为一个 `AG2–AG4` 固定切片，但三者的 canonical state、生命周期、复用结论、依赖和 ledger 状态均不同：

| slice | ledger owner | 当前状态 | 核心前置 |
| --- | --- | --- | --- |
| AG2 | R2.50 scheduler | NEEDS_EVIDENCE | R2.16、R2.20、R2.29、queue/scheduler 复用审计 |
| AG3 | R2.51 capacity | CONFIRMED | R1.20、R2.16 |
| AG4 | R2.52 budget | NEEDS_EVIDENCE | cost/budget/quota/ledger 复用审计 |

这也与同一行声明的“scheduler/cap/budget 独立 owners”自相矛盾。必须拆成三条固定 slice contract，分别列出 `requires`、owner、identity、consumer、删除项和门禁。workstream 可以并列展示，但不能共享签收状态或一次性迁移事务。

### 1.3 Workflow DAG 遗漏 durable operation ownership 与 clock 硬前置

v2 当前使用：

```text
C1+X1 -> WF1 -> WF2 -> WF3 -> WF4
```

核心总账明确要求：

- R2.47 Workflow durable run 依赖 R0.9、R2.1、R2.46；
- R2.48 reconciliation/effect 依赖 R0.9、R2.47；
- R0.9 当前仍为 `NEEDS_EVIDENCE`。

泛化的 `C1/X1` 不能替代 definition identity、operation fencing 和 durable clock contract。DAG 与 WF 固定切片必须显式引用这些 R owner，否则实施者可能在跨 backend operation ownership 尚未闭合时开始 durable run。

### 1.4 Residency、Automation 与 GC 被压成无共同 owner 的共享切片

v2 将 `RS1/AU1/GC1` 合为一行，但三者属于不同 bounded context，拥有不同 identity、状态机、消费者和失败边界：

- Residency/R1.20 依赖 R1.26、R2.19、R2.20、R2.27、R2.46；
- Automation/Cron 必须消费自身 schedule/occurrence schema、R2.21 transaction contract 与 R2.46 clock；
- cleanup/GC 必须消费 canonical pin、retention、lease、reachability 和 fenced deletion claim。

它们可以在总览中并列，但固定 contract 必须逐 owner 拆分。共享一行无法独立判定 `CONFIRMED/DONE`，也无法精确归属 consumer migration、旧入口删除和 fault matrix。

### 1.5 DAG 未完整传播核心总账的现存阻断

第 8 节正确说明 R2.50 的复用证据未完成，但第 4、5 节没有一致表达其他硬阻断：

- R0.9 阻断 Workflow durable run 与 reconciliation；
- R2.52 阻断 R2.28 lineage/spawn；
- R2.28 与 R2.50 阻断 R2.53 subtree cancellation；
- R3.6 必须先完成 serialized ErrorCode consumer inventory，并依赖 R3.1；
- R0.3 仍等待 ADR-D4 产品决定。

第 4 节 DAG、第 5 节固定 contract 和第 8 节开工规则必须给出相同结论。不能由实施者自行把压缩 workstream 名还原成核心总账依赖。

## 2. 重要修订

### 2.1 在固定 contract 中保留 R2.50 的有限无饥饿保证

产品决定摘要已正确包含 root/subtree 两级 WDRR，但固定 contract 主要通过测试名“有限无饥饿”间接表达。应明确声明：持续存在 execution capacity 时，每个持续 eligible root 和持续积压的兄弟 subtree 必须在由 active roots、subtrees 与有界 weight 推导的有限调度轮次内取得 claim。测试是该 guarantee 的证据，不能替代 contract 本身。

### 2.2 workstream 范围不能冒充固定实施切片

`P1–P4`、`X0–X3`、`AG1–AG5` 等名称可以用于导航，但第 5 节要求每片冻结单一 `ledger_owner`、owner、identity、依赖和删除项，因此凡是对应多个 R owner、不同状态或不同生命周期的范围，都必须逐 slice 展开。否则“CONFIRMED slice 固定 contract”仍不能直接用于下发。

### 2.3 标题应与 v2 身份一致

文件名已经是 v2，正文标题仍称“重写稿”。建议改为“实施规格 v2”，避免旧实施稿、新实施稿和冻结 review 在交接、引用和测试证据中发生名称歧义。

## 3. 已通过部分

### 3.1 结构与覆盖

- 单一阅读路径已经建立，没有恢复旧 P0/P1/P2、G4 或 G5 阅读结构；
- 38 个 SB 编号各出现一次，无遗漏、无重复；
- disposition 只使用 `IMPLEMENT`、`MERGED_INTO`、`REJECTED`、`EVIDENCE_ONLY`；
- 核心总账仍是唯一编号和完成状态 owner，本文件没有建立第二进度账本；
- `EVIDENCE_ONLY` 未伪装为允许编码的实施切片。

### 3.2 已确认产品决定

- Product 是唯一 composition root；
- 每个 Agent/Role 独立拥有 process-local `BackgroundTaskPool`；
- 同 Agent/Pool/进程内允许同 TaskId retry/resubmit，每次产生单调 AttemptId，旧 attempt 失去提交权；
- BackgroundTask 不获得跨进程接管或自动重放语义；
- WorkflowRun 是跨进程 durable execution，JSONL backend 不得降低最低 guarantee；
- logical Agent、incarnation 与 turn 三类 cap 保持不同 identity；
- Session、Residency、Workflow、Cron、ErrorCode 五类精确旧格式获得丢弃授权；
- Artifact、workspace、secret、Agent lineage/delivery 和仓外 wire 未被越权纳入丢弃范围。

### 3.3 R2.50/ADR-D5

ADR-D5 已正确吸收并解除产品阻断：

- 当前 `tenant == root governance owner`；
- root 间 WDRR，持续积压的兄弟 subtree 使用第二级 WDRR；
- turn scheduling cost 固定为 1；
- weight 与 priority 有界，默认 weight 为 1，extension 不能提高；
- priority 不跨 root/subtree 越权，同 priority 使用 durable enqueue sequence FIFO；
- deadline/cancel/claim 以 CAS 决出唯一赢家；
- capacity admission 在 durable accept 前完成，accepted item 不驱逐；
- claim 绑定 queue revision、scheduler fence 与 R2.16 permit；
- retry 具有 `next_eligible_at` 和 terminal disposition，不制造 HOL blocking；
- config generation 只影响下一次尚未 claim 的决定。

R2.50 保持 `NEEDS_EVIDENCE` 是正确的：产品决定已确认不等于基础设施复用审计已经完成。

### 3.4 文档卫生

- `git diff --check -- zdocs/package-cohesion-service-boundary-debt-governance-implementation-v2.md` 通过；
- review 文件保持冻结；
- 本轮评审没有修改实施稿或生产代码。

## 4. 准入条件

v2 在满足以下条件后可批准为生产实施规格：

1. 按核心总账重画 Agent、Workflow、Residency、Automation、GC 的依赖 DAG；
2. 拆开 AG2、AG3、AG4，以及 RS1、AU1、GC1 的固定 slice contract；
3. 显式传播 R0.9、R2.50、R2.52、R3.6 和 R0.3 的当前阻断；
4. 让第 4 节 DAG、第 5 节 contract 和第 8 节开工规则对每个 ledger owner产生相同结论；
5. 保留已确认的 ADR-D5 与 BackgroundTask resubmit 产品语义，不重新引入旧 review 中与用户决定冲突的建议；
6. 再次运行 SB 唯一覆盖、R 编号/标题 fingerprint、依赖闭包和 `git diff --check` 校验。

满足以上条件后，下一轮只需核对编号、依赖、owner、状态和删除边界的一致性；不需要重新进行产品语义评审。

## 5. 第二轮反向覆盖复核

本节从原始 package audit 的每项债务反向追踪到 v2 的 ledger owner 和实施 slice。结论是：SB 编号数量完整，但仍存在“表格中出现了编号，实际 slice 没有承接全部不变量”的假覆盖。因此“38 项 SB 全覆盖”目前只能证明编号集合完整，不能证明实施闭包完整。

### 5.1 SB0.2 的 delivery owner 没有绑定实施 slice

SB0.2 行的边界包含 lineage、scheduler、三类 cap、budget、cancel、residency 和 delivery，并引用 R1.13；但 slice 只写 `AG1–AG5`。本文 DAG 已经把 delivery 定义为 AG6：

```text
AG1+AG3 -> AG5 residency -> AG6 delivery
```

因此当前 disposition 行声明覆盖 delivery，实际却没有把 AG6 纳入该 SB 的实施闭包。必须将 SB0.2 显式绑定 AG6，并在修正后的 DAG 中为 AG6 保留 R1.13、R1.20、R2.20、R2.28 等真实前置。仅列出 R1.13 编号不能替代 slice 承接。

### 5.2 SB0.4 遗漏 Hook、Tool chokepoint 与 effect settlement owner

原始审计把 SB0.4 的整改拆给以下既有 owner：

- R2.30：固定内部 argv 与用户命令 runner；
- R1.7：API-key helper；
- R0.3：媒体下载/FileOps 权限边界；
- R0.6：Hook command 治理；
- R0.8：effect/journal fail-closed；
- R2.36：Tool lifecycle identity 和唯一 chokepoint。

v2 的 SB0.4 行只列 R0.8、R2.30、R1.7、R0.3，并将范围压成 `X0–X3`，遗漏 R0.6 与 R2.36。正文 DAG 虽另有 `H1 governed Hook`，但 disposition 没有把 H1 绑定回 SB0.4，也没有说明 ToolExecutor identity/chokepoint 由哪个 X slice 承接。

必须补回 R0.6/H1 和 R2.36 对应切片，或者给出它们已被新的 authoritative R owner 完整替代的明确映射。否则 runner 完成后仍可能留下 Hook 旁路或第二 Tool control path。

### 5.3 SB0.5 的 durable domain 集合不完整

SB0.5 原始范围不仅是 codec 类型，还覆盖每个 durable domain 的 transaction、retention、fencing 和 clock 解释。原始归并至少包括：

- R1.11–R1.12 Cron receipt/transaction/fencing；
- R1.23–R1.24 cleanup deletion claim 与 Artifact pin snapshot；
- R2.19–R2.21、R2.24–R2.25、R2.29、R2.31–R2.33、R2.41 strict schema；
- R2.46 durable clock contract。

v2 当前只列 R2.19–R2.21、R2.24–R2.25、R2.29、R2.31–R2.33、R2.41，并统一绑定 C1。这样会漏掉 Cron 的 receipt/fencing、cleanup/Artifact 的删除与 pin 不变量，以及所有 durable 时间字段共同依赖的 R2.46。

C1 可以作为 schema workstream，但不能成为万能 codec/transaction/clock slice。应把各 domain owner 分别绑定回 AU、GC、Workflow、Residency 等切片，并把 R2.46 作为使用 durable 时间字段的反向硬前置。

### 5.4 SB1.5 与 SB1.6 只绑定最终类型条目，遗漏调用链前段

SB1.5 的原始问题包含 ambient control、wiring projection、spawn contract 和 `OutputT` 端到端关系，原始 owner 为 R2.4、R2.10、R2.15。v2 仅保留 R2.15/T-AGENT。

SB1.6 的原始问题包含 Kernel node 输入、inference request/operation 和 graph assembly，原始 owner 为 R2.2、R2.7、R2.14。v2 仅保留 R2.14/T-KERNEL。

如果 T-AGENT/T-KERNEL 是聚合 workstream，必须在固定 contract 中逐项列出被消费的 authoritative R owner和全部 direct typed consumers；如果 R2.15/R2.14 已在核心总账中正式吸收其他条目，也必须用 owner/contract fingerprint 证明，而不能静默省略。否则泛型或万能 collaborator 可能只在最后一个 factory 被修复，中间 facade 仍继续退化为宽类型。

### 5.5 SB1.9、SB1.10、SB1.11 的公共面迁移链不完整

这三项当前 disposition 都只保留了部分 owner：

- SB1.9 当前列 R0.6/R0.2/R2.8，遗漏 extension trust/R1.9 与 inference request/R2.7；
- SB1.10 当前列 R2.3/R2.41，daemon 仅作 evidence，但若证据确认双 owner，仍需要回到 R2.26 或其最新替代 owner，不能直接在 T-QUEUE/T-GEN 编码；
- SB1.11 当前只列 R2.36，遗漏 spawn/control 泛型 R2.15 与 Tool definition/catalog generation R2.40。

这些遗漏不必强行恢复旧编号，但 v2 必须给出“旧 owner → 最新 canonical owner → slice”的无损映射。当前第 8 节只要求生成本文件已引用 R 编号的标题 fingerprint，无法发现根本没有被引用的 owner。

### 5.6 当前映射校验算法不足以证明无遗漏

第 8 节提出从核心总账生成“R 编号→标题”映射并校验本文件中的 fingerprint。这只能发现：

- 引用了不存在的编号；
- 编号标题发生漂移；
- 已引用条目的 owner/contract 不一致。

它不能发现“某个 SB 应承接的 R owner 完全没写进 v2”。下发前还需要一张反向覆盖矩阵：

```text
SB invariant
  -> authoritative R owner
  -> fixed slice
  -> production consumer
  -> delete/gate
```

校验必须同时满足：每个已接受的 SB invariant 至少有一个 authoritative owner；每个 owner 都进入独立或明确归属的固定 slice；每个 slice 有真实 consumer、旧路径删除和 negative gate。仅做 SB 编号 `uniq` 和 R 标题 fingerprint 不足以签收“38 项全覆盖”。

## 6. 第二轮结论更新

第一轮“暂不批准”结论保持不变，并新增一个批准条件：**修复 disposition 的反向 owner/slice 覆盖。**

下一版不仅要拆正 DAG，还必须做到：

1. SB0.2 补入 AG6 delivery；
2. SB0.4 补齐 Hook 与 Tool chokepoint；
3. SB0.5 按 durable domain 补齐 Cron、cleanup/Artifact 和 clock owner；
4. SB1.5、SB1.6、SB1.9–SB1.11 给出无损的旧 owner 到最新 owner 映射；
5. 增加 invariant→R owner→slice→consumer→delete/gate 的反向闭包校验。

在这些映射修复前，不应把“38 个编号各出现一次”表述为“38 项债务实施闭合”。更准确的状态是：**38 项 disposition 编号覆盖完成，实施 owner 与 slice 闭包尚未完成。**

## 7. 第三轮：当前 265 行版本复审

评审期间 v2 已从最初的 176 行版本原位重写为 265 行版本。本节以当前磁盘内容为准，是本 review 的最新状态结论；第 1 节和第 5 节保留用于说明上一快照问题及其修复来源，不得把已经修复的旧发现继续视为当前阻断。

### 7.1 已解决的上一轮问题

当前版本已经完成以下修订：

- Agent DAG 不再使用 `AG1 -> AG2 -> AG3 + AG4` 的错误线性链；capacity、budget、lineage、scheduler 已按不同前置拆开；
- AG2、AG3、AG4 已拆成独立 fixed slice；
- Workflow 显式绑定 R0.9、R2.46；
- Residency、Cron、cleanup、Artifact pin 已拆成 AG5、AU1–AU3、GC1–GC2；
- SB0.2 已补入 AG6 delivery；
- SB0.4 已补入 R0.6/H1 与 R2.36/T-TOOL；
- SB0.5 已补入 Cron、cleanup/Artifact 和 R2.46；
- SB1.5、SB1.6、SB1.9–SB1.11 已恢复完整 owner 链；
- 已新增 `SB invariant -> R owner -> fixed slice -> consumer -> delete/gate` 反向闭包矩阵；
- 标题已明确为“实施规格 v2”。

这些问题不再阻断当前版本。

### 7.2 阻断：R2.50 与 R2.52 状态已经落后于核心总账

当前核心总账已经记录：

- R2.50：`CONFIRMED`。复用 Inference fair queue 的纯算法与测试不变量，拒绝复用其 process-local concrete queue；Agent durable queue 复用 canonical permit、mailbox、lease 和 clock mechanism；
- R2.52：`CONFIRMED`。复用 canonical `UsageLedger`、`BudgetReservation/UsageSettlement` 与现有 SQLite fenced implementation，不新建第二 budget ledger。

但 v2 仍在以下位置把两者写成 `NEEDS_EVIDENCE`：

- 第 1 节 ADR-D5 后的状态说明；
- DAG 中的 `scheduler reuse evidence`、`budget reuse evidence`；
- AG2、AG4 fixed contract；
- 第 8 节阻断传播。

这会错误阻断 AG2/AG4，以及其下游 R2.28/R2.53。必须从核心总账实时同步为 `CONFIRMED`，把已经完成的复用审计结论写入 AG2/AG4 的 `reuse_decision`，不能继续保留虚假的 evidence node。

同时 R2.50 的核心硬前置已更新为 R2.16、R2.20、R2.29、R2.46；v2 DAG 和 AG2 contract 漏掉 R2.46。Agent queue 的 durable deadline、lease expiry 与 retry eligibility 必须使用 canonical clock contract。

### 7.3 阻断：R0.9/D1 没有可实施的 fixed slice

SB0.3 disposition 把 R0.8/R0.9 绑定到 D1，但：

- DAG 中没有 D1；
- fixed contract 表中没有 `D1 / R0.9`；
- 反向闭包矩阵却把 R0.9 指向 `X0/WF2`；
- X0 只拥有 R0.8 的 EffectId、intent/receipt/in-doubt；
- WF2 消费 R0.9，并不拥有或实施跨 backend operation ownership。

这造成一个永远不能被完成的前置：WF2 等待 R0.9，但文档没有任何切片负责完成 R0.9。

必须新增独立的 `D1 / R0.9` fixed slice，至少写明：

- 前置 R0.8、R2.46；
- Contracts-owned typed operation-ownership Port；
- deployment/backend identity、scope、holder、monotonic fence、revision、renew/takeover/release receipt；
- local JSONL/FileLease 与 Temporal adapter 的逐 backend guarantee matrix；
- activation failure fail closed，禁止 Temporal 失败后回退 JSONL；
- 两进程/host claim、lease loss、effect 后 receipt 前 crash、stale mutation 的故障矩阵；
- Runtime 不拥有 Workflow 状态机，Product 不复制 operation owner 的 negative gate。

然后 DAG 应表达：

```text
R0.8 + R2.46 -> D1/R0.9
WF1 + D1 + R2.46 -> WF2/R2.47
WF2 + D1 + X0/X1 -> WF3/R2.48
```

### 7.4 阻断：R2.43 被错误建模为普通 subscriber，SB2.6 仍指向旧 owner

核心总账 R2.43 的 authoritative 任务是“将 Session read model 迁入 canonical Session owner”，同时闭合 replay/live reducer、subscription cursor、effect/ack 与旧 `runtime.projections.session` 删除。

v2 当前存在三套不一致表达：

- SB1.8 把 R2.43 放入 E1–E3，方向基本正确；
- SB2.6 仍只引用 R2.25/PR1；
- 反向矩阵把 SB2.6 指向 R2.25/PR1/E1；
- fixed contract 把 E2/R2.43 仅描述为 durable subscriber cursor/effect/ack，没有写 Session read-model owner、replay/live authoritative reducer及旧 module 删除；
- PR1 在当前 fixed contract 和 DAG 中不存在。

R2.25 拥有 SessionEvent catalog 的 canonical declaration/generation，不拥有 Session read model 迁移。应删除悬空 PR1，把 SB2.6、反向矩阵和 fixed contract 统一绑定到 R2.43/E2；E2 必须同时承接：

- `SessionProjectionState/SessionLiveProjection` 迁入 `runtime/session/`；
- replay/live 共用 authoritative reducer/state；
- durable subscription cursor/effect/ack；
- Agent component/key/accessor、Product governance 和测试消费者迁移；
- 删除 `runtime.projections.session` module/export/component identity；
- 通用 Artifact projection registry 不拥有 Session read model 的 negative gate。

### 7.5 阻断：budget ledger 的唯一真相 owner 表述仍可能建立第二 ledger

scope/guarantee matrix 当前把 “Agent scheduling/cap/budget” 整体写为 Orchestration，并称其拥有 durable queue/projection/ledger。AG4 又写“Token/cost/depth/capability budget ledger”，容易被执行者解释为在 Orchestration 新建 Agent budget ledger。

这与最新 R2.52 复用结论冲突：现有 `UsageLedger` contract 和 SQLite fenced implementation 是唯一 usage/budget truth；Orchestration 只拥有 Agent/root/subtree 治理 policy、subject/dimension projection 和 typed reservation coordination，Product composition 选择同一个 ledger implementation。

scope matrix 应拆开表达：

| capability | canonical truth | Orchestration owner |
| --- | --- | --- |
| Agent scheduler | Orchestration durable queue | WDRR、claim、retry、settlement |
| capacity | Orchestration capacity projections | logical/resident/turn reservation receipts |
| usage/budget | canonical UsageLedger + Product-selected implementation | Agent/root/subtree policy、subject projection、reserve/settle coordination |

AG4 的删除项还应明确包括“第二 SQLite table/store、第二 usage balance、复制 inference reservation state”，而不只是 telemetry 余额。

### 7.6 重要修订：X0 的 DAG 与 fixed requires 不一致

DAG 把 `X0 effect identity` 画成 H0 的直接后继，但 fixed contract 又要求 X0 依赖 `H0/C1`。必须二选一并与核心总账一致：如果 EffectId/envelope 依赖 C1 已冻结的 schema/migration decision，DAG 应写 `H0 + C1 -> X0`；如果不依赖，则从 X0 fixed contract 删除 C1。不能让图和下发表对同一切片给出不同开工条件。

### 7.7 当前批准结论

当前 265 行版本较上一快照已经显著收敛，但仍不批准，剩余阻断缩减为四项：

1. 同步 R2.50/R2.52 最新 `CONFIRMED` 状态、复用结论与 R2.46 前置；
2. 新增 D1/R0.9 独立 fixed slice 并接入 Workflow DAG；
3. 删除悬空 PR1，将 SB2.6 和 Session read model 全部归 R2.43/E2；
4. 明确 canonical UsageLedger 是唯一预算真相，Orchestration 不新建第二 ledger。

另需修正 X0 对 C1 的 DAG/contract 小型不一致。完成后即可进入下一轮一致性核验。

## 8. 第四轮：当前 269 行版本复审

本节以当前 269 行 v2 和当前核心总账为准，取代第 7.7 节作为最新批准结论。

### 8.1 已解决的第三轮阻断

当前版本已经修复：

- R2.50、R2.52 在正文、DAG、fixed contract 和第 8 节均同步为 `CONFIRMED`；
- AG2 已补入 R2.46，并写明只复用 Inference fair queue 的纯算法/测试不变量；
- AG4 已明确复用 canonical UsageLedger 和 Product-selected fenced implementation，Orchestration 不拥有第二余额真相；
- D1/R0.9 已进入 DAG、反向矩阵与 fixed contract；
- SB2.6 已删除 PR1，统一归 R2.43/E2；
- E2 已承接 Session read model、replay/live reducer、subscriber cursor/effect/ack、消费者迁移与旧 module 删除；
- X0 对 C1 的 DAG 与 fixed requires 已统一。

因此第 7.7 节列出的四项阻断均已解决。

### 8.2 阻断：R0.9 已 CONFIRMED，但 v2 仍把它作为证据阻断

当前核心总账已将 R0.9 更新为 `CONFIRMED`，并固定 backend 决定：

- local JSONL 使用 Runtime FileLease + strict RunJournal operation owner；
- Temporal 使用 Temporal-owned workflow/run/activity attempt identity、history 与 visibility/query；
- Temporal 跨主机路径改为稳定注册的 typed activity handler 与版本化 serializable command/result DTO；
- 删除生产 `StepHandlerRegistry` 和 closure-based `run_step(execute=...)` 跨主机路径；
- local RunJournal 不得冒充两种 backend 的共同跨主机真相。

v2 第 5 节的 D1 行虽已存在，但仍只写通用 operation Port，没有把上述 canonical backend 决定、typed activity、版本化 DTO 和旧 closure registry 删除写入；WF2 测试列仍标注 `R0.9 NEEDS_EVIDENCE`，第 8 节也继续把 R0.9 当作 WF2/WF3 的证据阻断。

应同步为：R0.9 contract 已确认，D1 可以在 R0.8、R2.46 完成后实施；crash/worker-loss proof 是 D1 的 `DONE` 验收，不再是计划证据未知。D1 删除项必须增加：

- `StepHandlerRegistry` 生产跨主机路径；
- closure-based Temporal `run_step(execute=...)`；
- local RunJournal 作为 Temporal canonical owner 的承诺；
- Temporal 激活失败后 JSONL fallback。

### 8.3 阻断：R3.6 已 CONFIRMED，但 v2 仍保留错误的外部 inventory 阻断

当前核心总账已完成 R3.6 consumer inventory 并确认 destructive target：

- 允许精确丢弃 ToolResult receipt、BackgroundTask attachment/notification、Session ErrorReport variants；
- ACP、AG-UI、Inference OpenAPI、公共 DTO 原样保留；
- Artifact metadata、Secret、Workspace 是 negative target；
- 不存在需要另行裁决的旧 Mote ErrorCode enum 仓外 ABI。

因此 R3.6 已为 `CONFIRMED`，只依赖 R3.1。v2 第 8 节仍写“等待 serialized ErrorCode consumer/仓外 wire inventory”，已经过期。迁移矩阵中“仓外 wire 另取证”也过宽，应改成：已盘点的 ACP/AG-UI/OpenAPI/公共 DTO 是明确 negative target，未来其他 wire 只由各自 owner 独立版本化，不能重新阻断本地 R3.6。

同时 BC-ERROR-WIRE contract 应固定精确 destructive roots、typed discard receipt/audit、negative-target isolation tests 和旧 decoder/alias 删除清单。

### 8.4 阻断：fixed contract 表仍有大量占位 slice，没有真正冻结

第 5 节宣称每个切片必须填写并冻结 `ledger_owner`、依赖、owner/identity、consumer、guarantee、复用决定、删除项和测试；但当前表仍缺少以下在 disposition、DAG 或反向矩阵中被当作正式实施 slice 的条目：

- A0 ledger/SB mapping；
- C1 domain schema + migration decision；
- DEL-ENV；
- BC-ERROR、BC-ERROR-WIRE；
- BC-ARTIFACT-NAME、BC-CODEMAP、BC-CONFIG、BC-PRIVATE；
- RELOAD；
- SQ1；
- 各其他实际进入 disposition 的 BC 卡。

表尾的：

```text
BC-* / 各自R | 对应owner完成 | ...
```

不是 fixed contract。它没有逐卡 ledger owner、真实前置、consumer set、删除符号、复用结论和测试，无法判定哪一张卡可开工或完成。这会让多个 `IMPLEMENT/MERGED_INTO` disposition 虽然在第 2 节有 slice 名，却在第 5 节没有可下发工作包。

必须采用一种且仅一种方式闭合：

1. 在第 5 节逐卡展开全部真实 slice；或
2. 删除这些伪 slice 名，将其迁移/删除工作合并进对应 authoritative R slice，并在该 R 行明确 consumer 与 deletes。

不能保留 `BC-*`、`对应owner完成`、`各自R` 这类无法机器校验的占位表达。

### 8.5 阻断：C1 是多个状态机的前置，却没有 canonical owner

C1 当前同时前置 X0、AG5、WF1、AU3、GC2、E1、T-GEN，并在 SB0.5 中代表多个 durable domain strict schema。但文档又正确声明“不建立万能 codec”。如果 C1 是一个编码切片，它会成为跨 domain schema owner；如果只是 workstream/evidence gate，就不能作为一个统一的 `requires` 状态。

应把 C1 明确定义为只读/治理型 schema-and-migration decision gate，并将实际 schema 实施归各 domain R owner；或者完全展开为各 domain 的具体 R 前置。推荐后者，例如：

- X0 绑定其实际 effect/journal schema owner；
- AG5 直接依赖 R2.19/R2.20/R2.27/R2.46；
- WF1 直接依赖 definition schema owner；
- AU3 自己拥有 Cron strict schema；
- GC2 自己拥有 ownership/pin schema；
- E1 自己拥有 SessionEvent catalog/schema；
- T-GEN 自己拥有 GenerationArtifact schema。

不能让一个无 ledger owner、无 fixed contract 的 C1 成为七条生产链的共同开工锁。

### 8.6 重要修订：DAG 中的嵌套 H0 表达不规范

当前 DAG 在 `H0 hermetic collection` 的子节点中写：

```text
├─ H0+C1 -> X0
```

这把父节点 H0 再次写入自己的子边，语义虽可猜测，但不利于依赖图解析。若保留 C1，应在 H0 与 C1 各自完成后汇合到 X0；若按 8.5 删除 C1 统一 gate，则直接写 X0 的真实 R 前置。

### 8.7 最新批准结论

当前版本仍不批准，剩余阻断为：

1. 同步 R0.9 最新 `CONFIRMED` 状态与 Temporal/local backend canonical 决定；
2. 同步 R3.6 最新 `CONFIRMED` inventory、精确 destructive target 与 negative target；
3. 清除 `BC-*` 等 fixed contract 占位，保证每个正式 slice 真正可下发；
4. 删除或拆解无 owner 的 C1 共同前置，避免建立万能 schema owner。

完成后再核对 DAG 的机器可解释性。当前产品决定本身没有新增阻断；BackgroundTask 每 Agent/Role 独立 pool 和 ADR-D5 均保持正确。

## 9. 第五轮：当前 285 行版本复审

本节以当前 285 行 v2 为准，取代第 8.7 节作为最新批准结论。

### 9.1 已解决的第四轮阻断

当前版本已经完成：

- R0.9 同步为 `CONFIRMED`；D1 固定了 local JSONL/FileLease 与 Temporal workflow/history/visibility 两套 backend owner，并明确删除 `StepHandlerRegistry`、closure-based Temporal path 和 fallback；
- R3.6 同步为 `CONFIRMED`；精确 destructive targets 与 ACP/AG-UI/OpenAPI/公共 DTO/Artifact/Secret/Workspace negative targets 已进入 fixed contract 和迁移矩阵；
- C1 已删除，各 durable schema 回到 C-RES、C-MAIL、C-OUTPUT、C-LEASE、C-FILESEARCH、C-CHECKPOINT、C-SECRET、AU、E1、T-GEN、GC 等真实 domain owner；
- DEL-ENV、BC-ERROR、BC-ERROR-WIRE、BC-ARTIFACT-NAME、BC-CODEMAP、BC-CONFIG、MCP-RELOAD、RELOAD、PACKAGE-FACADE、MIGRATION-RESIDUE、SQ1、BC-PRIVATE 均已有独立 fixed contract；
- `BC-* / 各自R` 占位行已删除；
- DAG 不再出现嵌套 `H0+C1` 表达。

第四轮四个阻断全部解决，当前没有新的产品决定或 canonical owner 争议。

### 9.2 阻断：disposition 声明为封闭枚举，实际使用未声明复合值

第 2 节声明 disposition 仅允许：

```text
IMPLEMENT | MERGED_INTO | REJECTED | EVIDENCE_ONLY
```

但 SB0.4、SB1.7、SB1.10 实际使用 `MERGED_INTO + EVIDENCE_ONLY`。这既不是声明的枚举成员，也让“EVIDENCE_ONLY 不得编码”无法判断是阻断整项还是只阻断其中一个子不变量。

应选择一种确定表达：

- 推荐把 disposition 保持为 `MERGED_INTO`，在边界列增加 `evidence_dependency=EV-*`，明确已确认子项可按各自 R owner实施，只有 evidence 子项不得编码；或
- 把一个 SB 拆成逐 invariant 子行，每行只使用一个 disposition。

不应扩展为任意字符串组合，否则机器校验、状态传播和下发规则再次失去封闭性。

### 9.3 阻断：SB0.3 disposition 漏掉 X0/R0.8 slice

SB0.3 的 owner 列写：

```text
R0.8/R0.9 · D1
```

但反向矩阵正确写成 `R0.8/R0.9 -> X0/D1`，fixed contract 也明确 X0/R0.8 与 D1/R0.9 是两个不同切片。当前 disposition 会让 R0.8 没有对应 slice，违反 invariant→owner→slice 闭包。

应改为：

```text
R0.8/R0.9 · X0/D1
```

### 9.4 阻断：SB1.14 的 owner 与 slice 集合不一致

SB1.14 当前绑定：

```text
R1.26/R1.4/R1.5 · BG1/BG2
```

但 BG2 的 authoritative owner 是 R2.53 subtree cancellation，而反向矩阵又只把 SB1.14 指向 BG1。三处表达互相冲突。

如果 SB1.14 只负责 BackgroundTask pool、deferred result、status identity、pin/drain，则应删除 BG2，固定为：

```text
R1.26/R1.4/R1.5 -> BG1
```

R2.53/BG2 已由 SB0.2 的 Agent governance/cancellation 链承接，无需在 SB1.14 重复。如果确实要让 SB1.14 同时覆盖 subtree cancellation，则 owner 列必须补 R2.53，反向矩阵也必须补 BG2；但这会混合 Pool owner 与 supervisor cancellation owner，不推荐。

### 9.5 重要修订：已完成的 evidence 不能继续作为无状态伪前置

以下 fixed rows 的 `requires` 使用了没有 ledger identity 或完成状态的自然语言：

- AG4：`canonical UsageLedger`；
- GC2：`ownership producers`；
- BC-ERROR：`serialized consumer inventory`；
- BC-ARTIFACT-NAME：`Artifact/FileOps consumer matrix`；
- BC-CODEMAP：`query/index/context consumer matrix`；
- MIGRATION-RESIDUE：`exact external/wire/durable scan`。

其中 UsageLedger、ErrorCode inventory 等证据已经写回核心总账并使对应 R 项进入 `CONFIRMED`，不应继续作为无法判断 `CONFIRMED/DONE` 的伪节点。应区分：

- 已完成、已写入 R 条目的 evidence：移入 `reuse_decision/contract`，不留在 `requires`；
- 实施时必须重新生成的 consumer/delete inventory：写成该 slice 的第一项实施任务和验收产物，不作为外部前置；
- 真正的其他 ledger 前置：只使用明确 R 编号或 fixed slice id。

GC2 的 ownership producers 属于需要逐 producer 接线的 consumer set，不是一个先于 GC2 完成的单一 owner；应在 contract/consumer 列展开，避免形成无法完成的虚拟节点。

### 9.6 重要修订：顶部状态需要与最终批准动作联动

当前标题仍写：

```text
状态：待复审；除 H0 与只读取证外，不批准生产状态机改造
```

在本轮剩余一致性问题修复前，这个状态正确。若下一轮复审通过，必须在同一次文档切片中更新为明确的批准状态，并保留 R0.3/ADR-D4 等逐 slice 阻断；不能出现正文已批准、顶部仍禁止全部生产实施的双重指令。

### 9.7 最新批准结论

当前版本仍暂不批准，但已经从架构阻断收敛为三类机械一致性问题：

1. 消除未声明的 `MERGED_INTO + EVIDENCE_ONLY` 复合 disposition；
2. 修正 SB0.3 的 X0/D1 映射和 SB1.14 的 BG1/BG2 映射；
3. 把自然语言 evidence/consumer 集合从 `requires` 中移到 contract、实施任务或验收证据，所有真实前置只使用可判定的 R/slice identity。

完成后可进行最终批准复核。当前 Dependency DAG、BackgroundTask 每 Role 独立 pool、Workflow backend、ADR-D5、公平调度、迁移授权和预算唯一真相均未发现新的实质问题。

## 10. 第六轮：当前 285 行版本最终依赖复核

本节取代第 9.7 节作为最新批准结论。

### 10.1 已解决的第五轮问题

当前版本已经：

- 删除未声明的复合 disposition，SB0.4、SB1.7、SB1.10 使用单一 `MERGED_INTO` 并以 `evidence_dependency` 表达局部取证；
- 将 SB0.3 修正为 X0/D1；
- 将 SB1.14 收口为 R1.26/R1.4/R1.5 → BG1，R2.53/BG2 只归 SB0.2；
- 把已完成 evidence 和实施首项 consumer scan 从 `requires` 移入 contract/tests；
- GC2、BC-ERROR、BC-ARTIFACT-NAME、BC-CODEMAP、MIGRATION-RESIDUE 等不再使用无法判定状态的自然语言前置。

第五轮三类问题已经解决。

### 10.2 阻断：DAG 仍残留 `canonical ownership producers` 伪前置，且漏掉 R1.24 的 R2.24 硬前置

GC2 fixed contract 已把 `requires` 改成 `—`，但 DAG 仍写：

```text
canonical ownership producers -> GC2 Artifact reachability+pin/R1.24
```

这既让 DAG 与 fixed contract 不一致，也保留了无法判定完成状态的自然语言节点。核心总账明确规定 R1.24 的硬前置是 R2.24。因此应统一为：

```text
C-OUTPUT/R2.24 -> GC2/R1.24
```

Session、Workflow、BackgroundTask、Tool、model、FileOps、legal-hold 等 ownership producers 是 GC2 的实施消费者/接线清单，不是一个先完成的共同 owner。

### 10.3 阻断：Product composition 漏掉 R0.0 canonical config 前置

核心总账规定 R2.42 依赖 R0.0；v2 的 P1/R2.42 只依赖 H0，DAG 也从 H0 直接进入 P1。这样可能在 canonical config declaration/materialization/activation 断链尚未修复时冻结 Product composition scope 和 factory。

必须：

- 为 R0.0 建立明确 fixed slice，或将其作为本规格外但可判定的 R 前置；
- DAG 改为 `H0 + R0.0 -> P1/R2.42`；
- P1/P2 contract 明确消费 R0.0 的 canonical config identity，不重新建立第二配置入口。

R0.0 不能被泛化成 H0；一个是配置/activation identity，另一个是 hermetic import gate。

### 10.4 阻断：cleanup/GC 漏掉 R2.29 lease schema 前置

核心总账规定 R1.23 依赖 R1.24、R2.19、R2.29、R2.46。当前 GC1 fixed contract包含 GC2、AG5、R2.46，但没有 R2.29/C-LEASE；AG5 也不依赖 R2.29，因此不能通过传递依赖补足。

GC1 的 DAG 和 fixed contract必须显式增加 C-LEASE/R2.29，确保 deletion claim、owner lease、stale release/delete 都使用 strict monotonic fencing schema。

### 10.5 阻断：trust、GenerationArtifact 与 config slice 漏掉核心前置

核心总账还明确规定：

- R1.9 trust/provenance 依赖 R1.8 canonical path/source trust；
- R2.41 GenerationArtifact binding 依赖 R0.2 canonical model contract；
- R2.12 config owner 依赖 M0/R0.0。

v2 当前：

- T-TRUST/R1.9 只依赖 P1；
- T-GEN/R2.41 只依赖 T-INFERENCE-REQUEST；
- BC-CONFIG/R2.12 只依赖 P1/T-TRUST。

必须分别补入 R1.8、T-MODEL/R0.2、R0.0。若 R1.8/R0.0 不属于本 package audit 的独立实施 slice，也必须作为明确的外部 R 前置出现，不能省略或用自然语言替代。

### 10.6 重要修订：DAG 与 fixed contract 的依赖校验必须双向

当前第 8 节主要校验 R 编号、contract fingerprint 和 invariant 反向闭包，还需增加两条机械门禁：

1. DAG 每条边必须在目标 fixed row 的 `requires` 中出现；
2. fixed row 每个硬前置必须能在 DAG 中找到同一 R/slice 边。

这能直接发现本轮的 GC2、P1、GC1、T-TRUST、T-GEN、BC-CONFIG 漂移，而不是依靠人工逐轮阅读。

### 10.7 最新批准结论

当前仍暂不批准。产品语义、owner、迁移授权和 slice 内容已经闭合，剩余阻断集中在五条核心依赖同步：

1. GC2/R1.24 增加 C-OUTPUT/R2.24，删除 `canonical ownership producers` 伪节点；
2. P1/R2.42 增加 R0.0；
3. GC1/R1.23 增加 C-LEASE/R2.29；
4. T-TRUST/R1.9 增加 R1.8；
5. T-GEN/R2.41 增加 R0.2，BC-CONFIG/R2.12 增加 R0.0。

修正并加入 DAG↔fixed requires 双向校验后，可以进行最终批准。顶部“待复审；仅 H0/取证”状态在最终批准时同步更新。

## 11. 第七轮：当前 323 行版本开工准入复核

本节取代第 10.7 节作为最新批准结论。

### 11.1 已解决的第六轮阻断

当前版本已经完成全部五组核心依赖同步：

- 新增 C0/R0.0，并让 P1/R2.42 消费 C0；
- 新增 T-SOURCE/R1.8，并让 T-TRUST/R1.9 消费 T-SOURCE；
- T-GEN/R2.41 已消费 T-MODEL/R0.2；
- BC-CONFIG/R2.12 已消费 C0/R0.0；
- GC2/R1.24 已消费 C-OUTPUT/R2.24，删除 `canonical ownership producers` 伪前置；
- GC1/R1.23 已显式消费 C-RES/R2.19、C-LEASE/R2.29、GC2/R1.24 与 R2.46；
- 第 8 节已增加 DAG 入边与 fixed `requires` 的双向归一校验规则。

核心总账中的 R0.0、R1.8、R1.9、R1.23、R1.24、R2.12、R2.24、R2.41、R2.42 依赖已被正确吸收。

### 11.2 唯一剩余依赖漏边：C0 → T-SOURCE

T-SOURCE/R1.8 的 fixed contract 正确写明 `requires=C0`，但 DAG 只写：

```text
P1 + T-SOURCE/R1.8 -> T-TRUST/R1.9
```

没有定义 T-SOURCE 如何取得 C0 前置。这会使刚新增的 DAG↔fixed 双向门禁立即失败。应补：

```text
C0/R0.0 -> T-SOURCE/R1.8
P1 + T-SOURCE/R1.8 -> T-TRUST/R1.9
```

这只是漏边，不涉及重新设计 trust/source owner。

### 11.3 最终批准时必须更新顶部状态

顶部仍为：

```text
状态：待复审；除 H0 与只读取证外，不批准生产状态机改造
```

因此即使其他内容闭合，当前文字仍明确禁止正式开工。补完 C0→T-SOURCE 后，应在同一文档切片将状态改为类似：

```text
状态：批准按 fixed slice 与核心总账实时前置实施；R0.3/X3 在 ADR-D4 确认前仅允许取证
```

`EVIDENCE_ONLY` 卡仍只允许取证；任何 `NEEDS_EVIDENCE/DECISION_REQUIRED` 或上游未 `DONE` 的 slice 继续阻断。批准实施规格不等于所有 slice 同时开工。

### 11.4 当前结论

当前版本已经达到**有条件批准**：

1. 补充 DAG 的 `C0/R0.0 -> T-SOURCE/R1.8`；
2. 将顶部状态由“待复审/仅 H0”改为按 fixed slice 批准，并保留 R0.3/ADR-D4 和 evidence cards 的局部阻断。

完成这两项后即可开工，不需要再进行架构或产品语义评审。建议只做一次机械检查：`git diff --check`、38 个 SB 唯一覆盖、R fingerprint、DAG↔fixed requires 双向校验。BackgroundTask 每 Agent/Role 独立 pool、Workflow backend、Agent WDRR、公平性、唯一 UsageLedger、迁移/保留授权均已通过本轮复核。

## 12. 第八轮：最终批准

评审对象当前为 324 行版本。本节是 v2 review 的最终有效结论，取代前述各轮“暂不批准”或“有条件批准”的阶段性结论。

### 12.1 最后两项条件已完成

- DAG 已增加 `C0/R0.0 -> T-SOURCE/R1.8`，与 T-SOURCE fixed `requires=C0` 一致；
- 顶部状态已改为按 fixed slice 与核心总账实时前置批准实施，并继续阻断 R0.3/X3、`EVIDENCE_ONLY`、`NEEDS_EVIDENCE`、`DECISION_REQUIRED` 和上游未 `DONE` 的切片。

实施稿末尾也已明确：规格批准不是全量并行开工授权，不解除任何局部阻断。

### 12.2 最终机械核验

实际执行：

```bash
git diff --check -- zdocs/package-cohesion-service-boundary-debt-governance-implementation-v2.md
rg -o 'SB[0-9]+\.[0-9]+' zdocs/package-cohesion-service-boundary-debt-governance-implementation-v2.md | sort -u
comm -23 <(rg -o 'R[0-9]+\.[0-9]+' zdocs/package-cohesion-service-boundary-debt-governance-implementation-v2.md | sort -u) <(rg -o '^### R[0-9]+\.[0-9]+' zdocs/core-architecture-debt-closure-requirements.md | sed 's/^### //' | sort -u)
```

结果：

- `git diff --check` 通过；
- 唯一 SB 集合为 38 项，覆盖 SB0.1–SB0.5、SB1.1–SB1.18、SB2.1–SB2.15；
- 未发现实施稿引用但核心总账不存在的 R 编号；
- C0/T-SOURCE、composition、trust、GenerationArtifact、Artifact pin、cleanup lease 等上一轮硬前置已经同时进入 DAG 与 fixed contract；
- 没有重新出现 C1、`BC-*`、自然语言 evidence prerequisite 或复合 disposition。

本轮是文档规格评审，未运行生产代码测试、Pyright 或 architecture pytest；这些属于各实施 slice 的验收，不是本次文档批准证据。

### 12.3 最终结论

**批准 `zdocs/package-cohesion-service-boundary-debt-governance-implementation-v2.md` 作为生产实施编排规格，可以开始实施满足实时前置的 fixed slice。**

开工纪律保持不变：

- 逐 slice 从核心总账重新确认状态和直接前置；
- 同片闭合 contract、owner、composition、正常/失败/恢复/取消/清理、consumer migration、旧入口删除和 gate；
- 不建立 compat、alias、双读写、fallback、临时 facade 或第二状态真相；
- R0.3/X3 在 ADR-D4 确认前只取证；所有 evidence cards 在绑定具体 R owner并新增 fixed contract 前不得编码；
- BackgroundTask 继续采用每 Agent/Role 独立 `BackgroundTaskPool`，不得回退为进程 singleton、共享 registry 或跨进程 rebind；
- R2.50 继续采用已确认的 root/subtree WDRR 与有限无饥饿保证；预算继续复用唯一 canonical UsageLedger。

后续不再需要重复进行本规格的架构产品评审；只有核心总账、产品决定、owner、guarantee 或依赖 DAG 发生实质变化时才重新打开评审。
