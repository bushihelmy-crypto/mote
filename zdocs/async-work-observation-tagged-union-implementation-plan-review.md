# Mote 异步工作统一观测面严格 Tagged Union 实施计划评审

评审对象：`zdocs/async-work-observation-tagged-union-implementation-plan.md`  
评审依据：仓库根目录 `AGENTS.md`、当前生产源码、相关架构门禁与测试  
评审结论：**产品方向通过，当前实施计划不通过；完成阻塞项修订后方可进入实施。**

## 1. 总体评价

计划坚持“只统一用户如何观察异步工作，不统一异步工作如何执行”，正确保留了 BackgroundTask 与 WorkflowRun 在 identity、owner、lifecycle、durability、recovery 和 command path 上的边界。严格 tagged union、禁止裸字符串猜测 variant、禁止将 presentation phase 反向解释为 domain state 等决定，符合 `AGENTS.md` 的单一 owner、单一状态真相和正式边界类型化原则。

当前版本仍不能直接实施。主要缺口不在 tagged union 的语法设计，而在以下架构语义尚未闭合：

- Port 的 canonical owner 与目录位置；
- 产品级查询与 Agent-owned Pool 之间的跨 Agent 路由；
- Workflow command 的 revision/CAS 并发条件；
- observation 各字段的 authoritative fact 来源；
- process restart 后 local owner-lost 的事实与 retention owner；
- 两个异构数据源的稳定全局分页；
- Workflow identity 的 nominal type；
- wire/replay 边界与旧 durable 数据处置；
- 基础设施复用和最小服务面的评审证据。

如果在这些决定未闭合时直接新增 `contracts/async_work/` 和 Product aggregator，极易形成第二套 observation 状态、跨 Pool registry、非原子的拼接快照或 Product 对具体实现的直接依赖，违反十年零架构债务原则。

## 2. 已确认正确、应保留的决定

以下内容符合 `AGENTS.md`，后续修订不应削弱：

1. 只统一观测体验，不统一执行、调度或持久化状态机。
2. BackgroundTask 保持 Agent-owned、process-local，不跨进程接管。
3. WorkflowRun 保持 Orchestration-owned、durable、fenced、可恢复。
4. 禁止 BackgroundTask 自动升级为 Workflow，禁止 Workflow 进入 Pool。
5. 直接复用 `contracts/task/lifecycle.py::LocalTaskReference`，保留 process、Agent、incarnation、TaskId 和 AttemptId 的完整绑定。
6. 删除 Workflow `run_id` 被投影为 `task_id` 的语义折叠。
7. 删除 `BackgroundTaskProgressEvent.origin: WorkflowProgressEvent` 的错误 domain 嵌套。
8. query、cancel、progress、terminal 和 result 全链保留 variant identity。
9. presentation phase 只能由 domain state 单向投影，不能反向驱动 domain transition。
10. 统一入口不接受裸字符串或 ID prefix 猜测，不返回统一 `bool`。
11. 不建立 `AsyncWorkManager`、第三套任务状态机、兼容 alias、双 decoder 或 fallback path。
12. Activity topology 仅作为嵌套展示数据，不承担异步工作 identity 或 lifecycle。

## 3. 阻塞问题

### 3.1 Query/command Port 的 owner 与放置位置错误

原计划在 `contracts/async_work/command.py` 中同时放置 typed request、disposition 和最小 query/command Port。按照 `AGENTS.md`，跨层调用 Port 应由消费方按最小需求定义在 `contracts/ports/`，不能混入 domain DTO 包。

应拆分为：

```text
contracts/async_work/
├── identity.py
├── observation.py
├── command.py       # command/query DTO 与 typed receipt/disposition
├── codec.py
└── __init__.py

contracts/ports/async_work/
├── observation.py   # 最小只读查询 Protocol
├── local.py         # local owner routing/query/cancel 所需最小 Protocol
├── workflow.py      # durable workflow query/command 所需最小 Protocol
└── __init__.py
```

具体文件可以根据最终 bounded context 调整，但 DTO 与 Port owner 必须分离。不能为了少建文件制造巨型 `AsyncWorkService` 或 `AsyncWorkManager`。

`project_background_task_phase(BackgroundTaskStatus)` 和 `project_workflow_phase(WorkflowRunPhase)` 也不能由 Contracts 实现，因为输入类型由 Orchestration domain 拥有。两个 bounded context 应分别拥有总投影函数，只输出 Contracts-owned `AsyncWorkPresentationPhase`。

### 3.2 产品级统一列表没有闭合跨 Agent ownership 与路由

计划同时提出：

- Background adapter 只查询“当前 Agent-owned Pool”；
- Product 提供统一的 `list_async_work/get_async_work/cancel_async_work`；
- local reference 携带 process、Agent 和 incarnation identity。

这三点目前不能共同成立。只访问当前 Pool 无法提供 Agent tree 或 Product scope 的统一列表，也无法根据任意完整 reference 路由到所属 Pool。若 Product aggregator 保存所有 Pool 或 task 的可变 registry，则会形成第二个 task registry，直接违反 BackgroundTask 的分散 ownership 约束。

计划必须先确认统一查询的 scope：

- 仅当前 Agent；
- 当前 subtree；
- 当前 root；
- 整个 Product/application。

随后明确：

1. 谁根据 `process_instance_id + agent_id + incarnation_id` 路由 typed query/command；
2. 路由层如何只持有窄 capability，而不持有 Pool 私有状态；
3. process 或 incarnation 已失效时，谁返回 typed `OWNER_LOST/INCARNATION_LOST`；
4. supervisor 如何聚合 immutable snapshot，而不读取或修改 Pool task map；
5. stale attempt、owner mismatch 和 route unavailable 如何 fail closed；
6. aggregation 是否允许跨进程，以及对应 transport/timeout/partial result 语义。

在这些问题关闭前，不能新增产品级统一 list/cancel 入口。

### 3.3 Workflow cancel 缺失 expected revision，和 CAS 要求冲突

原计划规定 stable Workflow reference 不包含 revision，这是正确的；但统一 cancel 示例只接受 `reference + reason`，测试矩阵又要求验证 stale revision。

当前 canonical Workflow command 明确携带 `expected_revision`，`WorkflowRunControl.cancel()` 也以 revision/CAS 执行状态转换。Product adapter 不能先查询最新 revision 再代调用方无条件取消，否则会隐藏调用者观察之后发生的并发 transition。

稳定 identity 与 command precondition 应分离：

```python
@dataclass(frozen=True, slots=True)
class CancelDurableWorkflowRun:
    reference: DurableWorkflowRunReference
    expected_revision: int
    reason: WorkflowCancelReason
```

local cancel 同样必须绑定完整 `LocalTaskReference`，包括 active/latest attempt 语义；不能继续接受裸 `TaskId`。两个 variant 返回各自的 typed receipt，不得把 revision conflict、terminal idempotency、owner lost 和 rejection 压缩为 `bool`。

### 3.4 Observation 字段缺少 authoritative fact 来源

计划要求 observation 包含：

- accepted、started、updated、terminal absolute instant；
- summary；
- progress entries；
- typed result/artifact pointer；
- available actions；
- Workflow terminal delivery state。

当前 `WorkflowRunProjection` 并不拥有大部分字段。Terminal delivery 又位于独立 reconciliation store，并且同一个 run 可以对应多个 destination。因此 adapter 不能仅凭现有 run projection 构造一个 authoritative observation，也不能把多个 store 的当前值随意拼接为“同一 revision”的快照。

实施前必须为每个 observation 字段给出下表所示的明确设计：

| 字段 | canonical owner | durable | revision/原子性 | 缺失语义 |
|---|---|---:|---|---|
| accepted/started/terminal instant | 待确认 | 待确认 | 与哪个 transition 同 commit | absent 或 typed unknown |
| summary | 待确认 | 待确认 | 是否属于 terminal fact | 不允许 adapter 推测 |
| result/artifact pointer | 待确认 canonical contract | 按 domain 保证 | 与 terminal settlement 的关系 | lost/in-doubt disposition |
| progress entries | 待确认 | local 与 durable 分别决定 | sequence/revision | best-effort 或 canonical |
| available actions | domain projection | 可重建 | 基于同一 snapshot revision | 不持久化 UI 状态 |
| delivery state | Workflow reconciliation owner | 是 | 多 destination 聚合与 torn read | per-destination typed projection |

如果 observation 需要跨 run store 与 reconciliation store，必须定义一致性模型，例如 revisioned projection、明确的非原子 projection guarantee，或由 canonical owner 在 transaction 内产出。不得新建 mutable observation store 双写 domain state。

### 3.5 Process restart 后 local `OWNER_LOST` 没有 canonical fact owner

BackgroundTask 是 process-local；进程退出后 Pool 及其 mutable task state 不再存在。计划同时要求 Session resume 后仍显示 typed owner-lost terminal fact，但没有说明该事实由谁持久化、保留和清理。

这里必须二选一并形成单一 contract：

1. **Durable observation fact 路径**：在不持久化执行状态机的前提下，持久记录 local submission/reference、process incarnation loss 和已提交 terminal result pointer；明确 canonical owner、schema、commit 顺序、retention 与 GC edge。
2. **非 durable local 路径**：重启后不恢复 local observation；仅当调用方拿着旧 reference 查询时，由 process/incarnation authority 返回 typed owner-gone，列表中旧 local task按已确认规则退出。

不能同时使用“恢复 owner-lost terminal fact”和“可能按 retention 消失”作为未定义 fallback。若引入 durable observation fact，必须证明它不是第二套 BackgroundTask lifecycle state machine，也不能产生自动重放或接管语义。

### 3.6 异构分页不足以保证稳定全局排序

计划要求按 observation instant 统一排序，但仅提出 cursor 包含 `kind + domain cursor`。单一 kind cursor 无法在两个持续变化的数据源上完成稳定 merge pagination，会产生重复、遗漏或顺序漂移。

稳定 cursor 至少需要：

- schema/version；
- local continuation；
- workflow continuation；
- 排序 watermark 或各 domain snapshot generation；
- stable reference tie-breaker；
- 查询 filter 的 canonical digest；
- local source 在翻页期间 owner lost/消失的确定语义；
- cursor 过期、generation mismatch 和 malformed input 的 typed disposition。

如果当前产品需求不需要跨 domain 全局分页，应收窄第一版服务面，而不是承诺一个没有一致性模型的 cursor。

### 3.7 Workflow identity 仍是裸字符串，无法满足 nominal identity 验收

计划中的 `DurableWorkflowRunReference` 使用 `run_id: str` 和 `definition_id: str`，同时又要求 codec round-trip 保留 nominal identity。这两个要求不一致。

应先确认 authoritative `WorkflowRunId` 和 `WorkflowDefinitionId` owner，并端到端用于：

```text
definition/create request
    -> WorkflowRunProjection
    -> store/codec
    -> AsyncWorkReference
    -> query/command
    -> receipt/event/wire
```

不能靠 `wfr_` 前缀或运行时字符串约定充当 identity type。迁移时必须同步更新仓内消费者并删除旧字符串入口，不保留 alias。

### 3.8 Wire codec 与 replay 决策不能保持条件式

计划写成“若 observation 进入 app-server、session event 或 durable UI replay”才使用 versioned codec，但后续 Slice 已明确要求 ACP、AG-UI 和 replay/resume 消费统一 observation，因此 wire boundary 实际已经存在。

实施前必须明确：

1. 哪些边界传递 in-process DTO；
2. 哪些边界使用 versioned wire envelope；
3. event family/schema 的 canonical owner；
4. replay 是保存 observation，还是从 canonical facts 确定性重建；
5. unknown schema/tag、额外字段和错误 primitive 的 fail-closed 行为；
6. 是否已有 durable `task_id`-for-Workflow payload。

旧 durable 数据必须在 Slice 1 前完成盘点，并明确选择直接保留、一次性 migration 或经用户授权丢弃。不能在实施中途引入双读 fallback。

### 3.9 基础设施复用与最小服务面证据不足

按照 `AGENTS.md` 6.4，新增基础设施或跨包服务改动必须说明搜索过的现有实现、canonical owner、最小方法及真实消费者、隐藏的实现细节和防止双入口的门禁。当前计划只列出部分可复用类型，证据仍不完整。

修订版至少应补充：

1. typed result/artifact pointer 复用哪个现有 canonical contract；
2. Background query/cancel 是扩展哪个 Port，为什么不复用现有 lifecycle service；
3. Workflow query/command Port 由哪个消费方定义，为什么 Product concrete durability 不能成为 contract；
4. terminal fact 与 Workflow delivery reconciliation 的关系；
5. progress event 与现有 event/presentation pipeline 的迁移清单；
6. 每个新公开方法的实际消费者；
7. aggregator 隐藏哪些 store、routing、lease、task 和 lifecycle 实现；
8. 如何证明不存在平行 registry、双 phase、双 command path 或跨层 import。

## 4. 必须新增的 Slice 0

在原 Contract-first Slice 之前新增“Slice 0：架构决定闭合”，且其结论必须进入计划正文，不得只留在实现 PR 说明中。

### Slice 0 验收项

1. 明确统一 query/list 的产品 scope。
2. 明确 local reference 的跨 Agent/process 路由 owner 和最小 Port。
3. 明确 local owner-gone 在 restart 后的 canonical 语义与 retention。
4. 确定 `WorkflowRunId`、`WorkflowDefinitionId` 和 result/artifact pointer 的 authoritative type。
5. 给 Workflow cancel/resume 等命令保留 expected revision/CAS。
6. 为 observation 每个字段指定 canonical source、durability 和一致性保证。
7. 明确多 destination terminal delivery 的投影结构。
8. 设计双源稳定 pagination cursor，或收窄第一版列表能力。
9. 确定 wire/replay 边界并完成旧 durable payload 盘点。
10. 给出 AGENTS.md 6.4 所要求的复用与服务面证据。
11. 明确 production composition root、activation、shutdown 和 owner loss lifecycle。
12. 明确架构门禁如何证明没有第二 registry、第二状态机或跨层具体依赖。

Slice 0 未全部通过前，不应新增生产 contract、adapter 或 Product 工具入口。

## 5. 对原实施切片的修订要求

### 5.1 Contract-first

- DTO 放在 `contracts/async_work/`，Port 放在 `contracts/ports/async_work/`。
- 先建立 authoritative nominal identity，再建立 wrapper union。
- command DTO 必须携带真实 CAS/generation 条件。
- strict codec 必须对应已确认的真实 wire boundary。
- 不允许先创建无消费者类型；下一切片 consumer 与 composition path 必须明确。

### 5.2 Domain projection adapters

- phase projection 函数分别由两个 Orchestration bounded context 拥有。
- adapter 只能从同一 canonical snapshot 投影，不缓存 phase。
- local adapter 不能暴露 Pool、task、lock 或内部 mutable map。
- Workflow adapter 不能直接泄漏 store/reconciler concrete implementation。
- 多 store projection 必须声明一致性语义。

### 5.3 Submission 与 notification

- `DeferredToolSettlement` 的动态输入边界与 typed submission receipt 必须分离。
- Workflow receipt 使用 `run_id`，local receipt 使用 `LocalTaskReference`。
- terminal notification 必须由 canonical terminal fact 渲染。
- 大结果只引用 canonical result/artifact pointer。
- 远端/外部动作已发生而本地 terminal commit 失败时必须保留 typed in-doubt 语义。

### 5.4 Product query/command

- aggregator 无状态仅表示不保存 canonical domain state；其 routing capability 和 lifecycle 仍须显式装配。
- local command 必须路由给 reference 所属 Agent incarnation。
- Workflow command 必须保留 expected revision。
- partial list、route unavailable、owner lost、stale attempt、definition mismatch 和 cursor expired 均返回 typed disposition。

### 5.5 Presentation surfaces

- surface 只消费 observation DTO/wire projection。
- `available_actions` 由同一 domain snapshot 总函数计算。
- presentation 不从 display ID 构造 command reference。
- replay 只能来自已确认的 canonical durable fact，不能持久化 UI reducer 状态作为第二真相。

### 5.6 旧面归零

除原计划列出的旧面外，还应检查并删除：

- Product/Orchestration 中接受 Workflow `task_id` 的参数与文案；
- local cancel/query 的裸 `TaskId` 跨 Pool 入口；
- `run_id: str` 与新 nominal ID 并存的生产路径；
- Product concrete durability 被当作跨包 contract 的路径；
- presentation/replay 自建的 observation cache；
- 新旧 progress、terminal、submission receipt 双事件链；
- 以 `Any/object` 将正式 observation identity 带过 adapter 的路径。

## 6. 架构门禁补充

在原计划门禁基础上，至少增加：

1. `contracts/async_work` 不定义跨层 service Protocol；相关 Port 只位于 `contracts/ports/async_work`。
2. phase projection 实现不位于 Contracts。
3. Product aggregator 不持有 Pool、task、store、reconciler 或跨 Pool mutable registry。
4. local query/cancel 必须携带完整 `LocalTaskReference`。
5. Workflow mutation command 必须携带 expected revision 或等价明确 CAS 条件。
6. Workflow reference 使用 canonical nominal RunId/DefinitionId。
7. observation 字段只能来自列入白名单的 canonical projection source。
8. 不存在 observation mutable store 与 domain state 双写。
9. local owner-lost 不产生 recovery、replay 或接管 BackgroundTask 的执行路径。
10. 全局分页 cursor 同时绑定两个 domain continuation 和查询 generation/watermark；若首版不支持，则门禁禁止暴露伪稳定分页 API。
11. Workflow terminal delivery 按 destination 保留 identity，不折叠为无来源的单一 bool/state。
12. wire decoder 对 unknown schema/tag、extra/missing field、错误 primitive 和 cross-variant field fail closed。
13. production composition 只有 Product 一个入口，不存在第二 factory/control path。
14. presentation、ACP、AG-UI、terminal 不 import domain concrete owner。

## 7. 测试策略评审

原计划的定向测试矩阵方向正确，但“禁止运行全量测试”不应成为永久完成条件。该改动横跨 Contracts、Orchestration、Runtime event transport、Product composition 和多个 surface，风险较高。

建议验证顺序：

1. 每个切片运行对应的小范围 contract、codec、adapter 和 architecture tests；
2. 迁移完成后运行完整 `ztest/architecture/` 门禁；
3. 运行 BackgroundTask、Workflow、presentation、ACP/AG-UI/terminal 的相关测试集；
4. 在环境允许且定向验证稳定后，运行覆盖受影响生产路径的更大回归集；
5. 若不运行全量测试，应在交付说明中明确未运行范围和原因，而不是由计划预先禁止。

必须增加的关键场景包括：

- 同一 TaskId 在不同 Agent Pool 中不会冲突；
- stale process/incarnation/attempt 全部 fail closed；
- process crash 后 local reference 不被新 Pool 接管；
- Workflow cancel 与 concurrent settle/pause 发生 CAS 竞争；
- Workflow definition mismatch 不返回其他 definition 的 run；
- 多 destination terminal delivery 投影保持各自 identity；
- 两个 domain 在分页期间新增、终止或 owner lost 时无重复/遗漏，或返回明确 cursor invalid disposition；
- durable observation/replay 不恢复 local execution state；
- malformed wire 数据不能通过宽松转换进入 canonical DTO。

## 8. 最终判定

| 评审维度 | 判定 |
|---|---|
| 产品方向 | 通过 |
| BackgroundTask/Workflow domain 分离 | 通过 |
| Tagged union 基本方向 | 通过 |
| Canonical owner 闭合 | 不通过 |
| Port 与服务面 | 不通过 |
| 跨 Agent routing/lifecycle | 不通过 |
| Durable fact 与 replay | 不通过 |
| CAS/fencing 并发语义 | 不通过 |
| Wire/migration | 待明确 |
| 基础设施复用证据 | 不通过 |
| 可直接实施性 | 不通过 |

最终结论：**保留产品决定与严格 tagged union 方向，退回实施计划修订。新增 Slice 0 并关闭全部阻塞问题后，再进行 Contract-first 实施。**

---

## 9. 二次评审（修订版计划）

二次评审对象：修订后的 `zdocs/async-work-observation-tagged-union-implementation-plan.md`  
二次评审结论：**修订版已关闭第一轮的大部分问题，但仍有六个实施阻塞项；当前不应标记为“评审修订完成”。**

### 9.1 已关闭的第一轮问题

修订版已经正确完成以下收敛：

1. 首版 scope 收窄为当前 Agent，不再承诺伪全局列表或双源全局分页。
2. Local owner-lost 选择非 durable 语义，不新增 local observation store 或第二 BackgroundTask lifecycle。
3. DTO 与跨层 Port 分离到 `contracts/async_work/` 和 `contracts/ports/async_work/`。
4. Workflow stable reference 与 mutation revision precondition 分离，cancel 保留 CAS。
5. Workflow terminal delivery 保留 per-destination identity/revision，不折叠为 run 级 bool。
6. durable Workflow observation 从 canonical facts 重建，不持久化 Product reducer state。
7. phase/action projection 明确由两个 Orchestration bounded context 分别拥有。
8. Product aggregator 收窄为无状态 dispatcher，不持有 Pool、store、reconciler 或 mutable registry。
9. Workflow nominal identity、strict terminal result 与旧字符串入口的迁移被纳入独立实施切片。
10. 旧 durable wire payload 在实施前盘点，不允许生产双读 fallback。

上述决定应继续保留，不应在后续修订中重新扩张首版能力。

### 9.2 阻塞项一：Contracts observation 仍引用 Orchestration-owned 状态类型

修订版将 observation DTO 放在 `contracts/async_work/`，但其结构仍直接包含：

```text
LocalBackgroundTaskObservation
  BackgroundTaskStatus

DurableWorkflowRunObservation
  WorkflowRunPhase
```

当前源码中：

- `BackgroundTaskStatus` 由 `orchestration/background_tasks/status.py` 拥有；
- `WorkflowRunPhase` 由 `orchestration/workflows/durable/model.py` 拥有。

因此 Contracts DTO 无法合法引用这两个类型。禁止使用字符串、`object`、`TYPE_CHECKING`、局部 import 或 re-export 绕过分层。

修订版必须明确选择一种正式方案：

1. observation 只携带 Contracts-owned `AsyncWorkPresentationPhase`，domain 原始状态不越过 Orchestration 边界；或
2. 将真正稳定的 Workflow/local lifecycle enum 移到各自 Contracts owner，由 Orchestration 拥有状态机和 transition 实现。

若 Product 确实需要展示 domain-specific 状态，优先定义 observation-owned typed detail，而不是让 Contracts 依赖 Orchestration concrete enum。该决定必须同步覆盖 codec、available-actions projection、测试和旧状态投影退出路径。

### 9.3 阻塞项二：“当前 Agent scope”缺少 Workflow 授权事实

修订版声称 durable variant 只查询与当前 Agent/Session destination 关联的 RunId，但 `DurableWorkflowRunReference` 仅包含：

```text
WorkflowRunId + WorkflowDefinitionId
```

当前 `WorkflowRunProjection` 也没有 caller Agent、Session、root 或 access-scope binding。因此，仅凭 reference 无法证明当前 Agent 有权查询或取消一个已知 RunId。

修订版必须补齐：

1. Workflow creation 时持久提交哪个 caller/owner/access-scope identity；
2. identity 是否绑定 Agent、Session、root governance owner 或明确的授权主体；
3. query/cancel Port 如何通过已绑定 capability/runtime context自动获得当前 caller identity；
4. authorization mismatch 的 typed disposition；
5. definition match 只能证明定义身份，不能证明访问授权；
6. terminal delivery destination 不能反向充当 run access authority；
7. Product 不得建立 mutable `allowed_run_ids` registry 作为授权真相。

该 access binding 必须属于 durable canonical run facts，随 create 原子提交并进入 strict codec。否则“当前 Agent scope”只是 Product 文案，不能形成安全边界。

### 9.4 阻塞项三：Progress revision/sequence 尚无 canonical source

修订版新增：

- local progress 的 `local_sequence`；
- Workflow progress 的 `run_revision + event_sequence`；
- “Workflow 按已提交 revision”的 live progress 语义。

当前 `WorkflowProgressEvent` 没有 revision 或 sequence，实际发射路径只是向当前 best-effort sink 发送事件。这不是单纯替换 DTO，而是新增顺序、一致性和可能的 durability 机制。

实施前必须明确：

1. sequence 的 canonical owner；
2. sequence 的 scope 是 reference、attempt、run、node 还是 process；
3. local sequence 是否仅在 process/incarnation/attempt 内单调，重启后是否失效；
4. Workflow event 对应 committed run revision，还是执行方观察到的 revision；
5. run state commit 与 progress emit 的顺序；
6. crash 发生在 commit 与 emit 之间时是否允许缺事件；
7. event sequence 是否 durable，若不是，不得把它描述为 durable history；
8. stale execution owner/fence 是否仍可能提交 progress；
9. replay 是否从 canonical run facts重建，而不是重放 best-effort progress。

若首版只需要 live presentation，可将 progress 明确定义为 incarnation-scoped best-effort signal，并携带来源 snapshot revision，而不承诺连续 sequence。若需要 durable、无缺口的 Workflow progress，则必须由 Workflow durable owner建立正式 event/journal contract，不能由 observation adapter临时编号。

### 9.5 阻塞项四：Workflow cancel receipt 与 canonical transition 不一致

修订版将 Workflow cancel disposition 写为 `CANCELLED`，但当前 `WorkflowRunControl.cancel()` 只把状态从 `CREATED/RUNNING/PAUSED` 转换到 `CANCELLING`。接受 cancel intent 不表示 cancellation 已完成。

应至少区分：

```text
CANCEL_REQUESTED
ALREADY_CANCELLING
ALREADY_TERMINAL
REVISION_CONFLICT
DEFINITION_MISMATCH
CONTROL_UNAVAILABLE
FENCE_LOST
NOT_FOUND
```

最终 `CANCELLED` 只能来自 canonical terminal settlement，不应由 cancel command receipt提前承诺。

同时，`OWNER_LOST` 不适合作为 durable Workflow control 的通用 disposition。Workflow worker/process loss 不删除 durable logical run；无法取得或维持 control lease/fence 时，应返回准确的 `CONTROL_UNAVAILABLE`、`CLAIM_CONFLICT` 或 `FENCE_LOST` 等 typed 语义，不能复用 local owner disappearance 的含义。

### 9.6 阻塞项五：Workflow terminal result 错误包含 pause

修订版允许 `WorkflowTerminalResult` 包含 pause，但 `WorkflowRunPhase.PAUSED` 是非 terminal lifecycle。将 pause 放入 terminal union 会导致：

- 同一个 run 同时表现为 paused 与 terminal；
- terminal fact、available actions 和 resume command 相互矛盾；
- terminal delivery可能错误投递可恢复的中间状态；
- retention/GC 把仍可恢复的 checkpoint 当作终态处理。

必须严格分离：

```text
WorkflowRunObservation
  pause_reason
  checkpoint/frontier
  resume precondition/action

WorkflowTerminalResult
  success
  failure
  cancelled
  timed_out
  in_doubt（仅当正式 lifecycle 已定义相应 terminal/settlement 语义）
```

如果 `IN_DOUBT` 不是 run terminal phase，而只是 effect reconciliation 状态，也不能直接放入 run terminal result；应保留在 effect/delivery projection 中，除非先正式扩展 Workflow run 状态机。

### 9.7 阻塞项六：Local large-result 复用决定互相矛盾

修订版同时规定：

- Local terminal 继续复用 `TaskResultPointer`；
- 大输出统一复用 `ArtifactRef`，不新增 locator identity。

但当前 `CompletedStoredTaskResultPointer` 使用 `StoredTaskOutput(locator: str)`，其 locator 是 `task-output:` identity，并不是 `ArtifactRef`。因此当前源码仍存在两个不同的大结果引用语义。

修订版必须明确：

1. 是否将 `CompletedStoredTaskResultPointer.output` 一次性迁移为 `ArtifactRef`；
2. 若保留 `StoredTaskOutput`，其与 Artifact 的核心不变量、lifecycle、retention、授权和 GC edge 有何不同；
3. streaming process output 与 canonical terminal result 是否是两个不同事实；
4. terminal fact 应分别引用哪个事实，不能用一个 locator兼任两者；
5. 旧 `task-output:` locator 是否 durable，是否需要一次性 migration；
6. 新旧 locator 不得永久双轨或由 renderer fallback解析。

如果 `StoredTaskOutput` 只是历史上的第二套大结果 locator，应直接迁移到 canonical artifact publication；如果它代表 process-local streaming log，则必须改名并限制语义，不能再作为 canonical terminal result pointer。

### 9.8 非阻塞但必须修正文档的问题

1. 文档称新增“五个窄 Port”，但目录只有三个文件，表格中的 Product observation query、subscription 与 domain Port 关系也未完全展开。应列出准确 Protocol 名称、方法、consumer 和 implementation owner。
2. `WorkflowCancelReason` 尚无当前 canonical type。需指定 owner、允许值、是否用户可控、wire codec 和 audit 语义。
3. “受影响的 96 项台账重新签收”范围过宽。应只重签 R1.5、R2.9 及通过 canonical dependency ledger机械计算出的硬依赖闭包，并在计划中列出具体工作包 ID。
4. 不得由本计划手工维护第二份依赖闭包或自行改变所有 96 项状态；台账更新必须通过其 canonical owner和既有门禁。
5. 文档声称用户已明确禁止大规模测试。若该决定来自当前任务之外已确认的用户要求，应记录来源；否则应改为“按环境资源约束分批运行”，避免把未经当前证据确认的限制写成永久产品决定。
6. `IN_DOUBT` 分别可能属于 effect、delivery、tool settlement 或 run lifecycle，必须按 authoritative owner区分，不能因展示相似上提为一个模糊状态。

### 9.9 二次评审后的必备修订

在进入 Slice 1 前，修订版计划还必须完成：

1. 消除 Contracts DTO 对 Orchestration status enum 的依赖。
2. 为 Workflow run 增加 durable caller/access-scope binding，并定义 query/cancel authorization Port。
3. 明确 progress revision/sequence 的 owner、scope、durability、fencing 和 crash gap 语义。
4. 将 cancel receipt 对齐 `CANCELLING` transition，删除提前承诺的 `CANCELLED`。
5. 从 Workflow terminal result 中删除 pause；审计 `IN_DOUBT` 是否真正属于 run terminal lifecycle。
6. 关闭 `TaskResultPointer` stored locator 与 `ArtifactRef` 的双真相。
7. 精确列出全部 Port、consumer、implementation 和 composition binding。
8. 将台账重签范围收窄到 R1.5、R2.9 及机械 dependency closure。

### 9.10 二次评审判定

| 评审维度 | 二次判定 |
|---|---|
| 首版 current-Agent scope | 通过 |
| Local restart/owner-lost 语义 | 通过 |
| DTO 与 Port 分离 | 通过 |
| Workflow mutation CAS | 基本通过 |
| Multi-destination delivery | 通过 |
| Product composition 方向 | 通过 |
| Contracts 分层可实现性 | 不通过 |
| Workflow access authorization | 不通过 |
| Progress truth/sequence | 不通过 |
| Cancel receipt 状态语义 | 不通过 |
| Workflow terminal union | 不通过 |
| Result/artifact 单一真相 | 不通过 |
| 可进入 Slice 1 | 不通过 |

二次最终结论：**修订版已经解决第一轮的大部分设计缺口，但仍有六个架构阻塞项。关闭这些问题并更新计划正文后，方可进入实施评审终审。**

---

## 10. 三次评审（终审候选版）

三次评审对象：状态为“二次评审修订完成，待终审”的最新实施计划  
三次评审结论：**二次评审的六个阻塞项已有五项闭合，result/artifact 双真相也给出了可执行的退出方案；但新增的 Workflow authority 设计与恢复语义、分层和可信凭证边界冲突，实施切片顺序也不满足每片零遗留要求，因此尚不能终审通过。**

### 10.1 二次阻塞项关闭情况

| 二次阻塞项 | 三次判定 | 说明 |
|---|---|---|
| Contracts DTO 引用 Orchestration enum | 已关闭 | observation 只携带 Contracts-owned presentation phase/detail DTO |
| Workflow access authority | 未关闭 | 已新增设计，但与跨 incarnation 恢复及可信凭证边界冲突 |
| Progress revision/sequence | 已关闭 | 首版明确为 best-effort，无连续 sequence/durable history 承诺 |
| Cancel receipt 状态语义 | 已关闭 | receipt 改为 `CANCEL_REQUESTED`，最终 `CANCELLED` 只来自 settlement |
| Workflow terminal union | 已关闭 | pause 和 effect/delivery `IN_DOUBT` 已退出 run terminal union |
| Result/artifact 单一真相 | 基本关闭 | stored terminal output迁移到 `ArtifactRef`，streaming log明确为不同边界 |

以下已关闭决定应保留：

1. `contracts/async_work` 不引用 `BackgroundTaskStatus` 或 `WorkflowRunPhase`。
2. local/Workflow observation detail 是 observation-owned frozen DTO，不复制 domain transition enum。
3. progress 只表示 live best-effort signal，Workflow revision 是发射方已观察 snapshot revision，不是假 durable event revision。
4. cancel intent 与 terminal cancellation 明确分离。
5. pause 只属于非 terminal observation；effect/delivery `IN_DOUBT` 不冒充 run phase。
6. `StoredTaskOutput` 退出 canonical terminal result；process-local streaming log使用独立、attempt-bound reference。
7. 首版准确收窄为四个跨层 Protocol，不新增 subscription Port 或第二 event bus。
8. 台账重签范围由 canonical dependency ledger 机械计算，不扩张为全部 96 项。

### 10.2 阻塞项一：immutable incarnation authority 与 durable recovery 冲突

最新计划要求 Workflow create 与 run facts 原子提交不可变授权绑定，其中包含：

```text
caller_agent_id
caller_incarnation_id
session_id
root_governance_id
authority_generation
```

同时又规定 query/cancel 将 ambient caller 的这些字段与 run authority 比较，并把 stale incarnation/generation 作为拒绝条件。

这与 Workflow 的 durable recovery 保证冲突：

- worker/process crash 后，同一个 logical Agent 会产生新 incarnation；
- Session resume 或 Residency reload 后，执行上下文 incarnation 也可能推进；
- durable Workflow 必须能由合法的新 fenced execution/control owner继续查询、恢复和控制；
- 若 stored `caller_incarnation_id` 不可变且要求相等，新 incarnation 必然被拒绝；
- 若忽略 stored incarnation，则该字段不是 access authority，只是创建审计事实。

必须拆分两个不同概念：

```text
WorkflowRunCreationProvenance
  creator logical Agent
  creator incarnation/generation
  creator Session/root
  created_at / audit identity

WorkflowRunAccessBinding
  stable authorized principal/scope
  access revision/generation
  lifecycle/transfer/revocation policy
```

创建 provenance 可以不可变记录原 incarnation；access binding 则必须以稳定 logical identity 为主体，并明确合法 incarnation 推进时的验证方式。不能要求新 incarnation 等于创建 incarnation。

计划还必须决定：

1. 同一 logical Agent 的新 incarnation 是否自动保留 run access；
2. 如何通过 canonical lineage/residency authority验证它是当前合法 incarnation；
3. Agent terminal、root cancellation、Session termination时 Workflow 是继续、取消、转交 root supervisor，还是进入其他 typed lifecycle；
4. access binding 若可 transfer/revoke，由谁以 revision/CAS/fence 更新；
5.旧 incarnation 何时失去 query、cancel、resume、result/delivery 访问权；
6. Session 与 root scope 同时存在时是 AND、OR 还是不同命令权限；
7. Product retention/purge 后 access fact 如何退出。

在这些语义未关闭前，不能把 `WorkflowRunAuthority` 直接加入 durable run codec。

### 10.3 阻塞项二：显式 `WorkflowCallerAuthority` DTO 不是可信 capability

计划称 `WorkflowCallerAuthority` 由 runtime capability 从当前上下文生成，并作为 query/cancel Port 的显式参数传入。若它只是 Contracts 中可构造的 frozen DTO，则任何进程内调用方都可以构造同样字段，不能证明调用来自受信 runtime context。

“模型或 wire 不接受该字段”只能缩小外部攻击面，不能使普通 DTO 成为不可伪造 authorization capability。正式授权不能依赖调用方自报 identity。

推荐的服务面是绑定 caller 的窄 Port：

```python
class WorkflowAsyncWorkObservationPort(Protocol):
    def get(
        self,
        reference: DurableWorkflowRunReference,
    ) -> WorkflowObservationQueryResult: ...
```

该 Port implementation 在 Product composition 时绑定可信 caller context，并在内部通过 canonical lineage/authority verifier取得和校验当前 logical Agent/incarnation/fence。surface、tool、模型只得到已绑定 Port，不接触或传递 caller credential。

如果确实需要显式 credential，则必须设计真实的不可伪造/可验证 capability identity、issuer、scope、expiry、generation、revocation 和 verifier；不能用普通 dataclass字段相等代替。当前需求没有证明需要引入如此复杂的新 capability系统，因此应优先复用已绑定 runtime context和窄 Port。

### 10.4 阻塞项三：Runtime 不应生成 Workflow-specific authority

计划写明 `WorkflowCallerAuthority` 是“runtime capability”生成的 Workflow credential。根据五层边界，Runtime 不拥有 Workflow、BackgroundTask 的产品状态机或治理语义，也不能 import Orchestration-owned Workflow concept。

合法依赖方向应为：

```text
Runtime/Agent execution context
  -> 提供 Contracts-owned通用 caller execution identity/fence

Orchestration Workflow access adapter
  -> 消费通用 identity + lineage/authority verifier
  -> 校验 WorkflowRunAccessBinding

Product composition
  -> 为当前 surface/tool绑定窄 Workflow Port
```

不能让 Runtime 构造、签发或理解 `WorkflowCallerAuthority`。如果仓内已有 generic execution owner、operation ownership、lineage authorization 或 Agent incarnation contract，必须复用或在其 canonical owner最小扩展；修订计划需记录搜索结果和选择理由。

### 10.5 阻塞项四：实施切片顺序会制造阶段性旧类型和二次迁移

当前 Slice 1 要求：

- Workflow notify/tool/message迁移到 RunId；
- 拆分 `AgentBackgroundTasks`；
- Workflow tool capability迁移到窄 Workflow-specific Port；
- progress union迁移。

但 nominal `WorkflowRunId`、durable authority 和 terminal result到 Slice 2 才建立，async-work DTO/Port到 Slice 3 才建立。这意味着 Slice 1 只能：

- 先使用旧 `run_id: str`，Slice 2 再迁移一次；或
- 创建临时 Port/DTO，Slice 3 再替换；或
- 让拆分后的 Product service暂时直接依赖 concrete implementation。

三种结果都违反每个合入切片自身“零遗留、零重复入口、零迁移残渣”的要求。

应改为可独立闭合的纵向切片。例如：

1. **Workflow foundation vertical slice**：一次建立 nominal identity、creation provenance/access binding、typed terminal result及最小 Workflow-specific Port，同时迁移 run store/control/codec 和直接 Product consumer，删除旧字符串/裸 terminal payload入口。
2. **Background/Workflow split vertical slice**：在正式 Port 已存在的前提下拆分 `AgentBackgroundTasks`、tool capability、notify/message/progress，并在同片删除混合 facade和旧测试。
3. **Async-work observation vertical slice**：建立 async-work DTO/Port、两个 domain projection、Product dispatcher及至少一个 production surface consumer，同片接入，避免无消费者公共类型。
4. **Remaining surfaces and zeroing slice**：迁移其余 surface、wire/replay和旧事件链，完成门禁与台账重签。

若变更规模要求更细，应按完整 identity/owner/composition链切分，而不是按目录或“先改调用点、后补 contract”切分。

### 10.6 需要澄清但不单独阻塞的问题

1. `WorkflowRunAuthority`、`WorkflowCallerAuthority`、Agent/Session/root/incarnation identity 必须使用仓内 authoritative nominal type；不能新增同义字符串 identity。当前部分既有 Agent lineage contract仍使用字符串，这不授权本计划复制新一套字符串 owner。
2. `authority_generation` 的含义必须唯一：它不能同时表示创建 incarnation、access policy revision、lineage generation和fencing token。
3. `AUTHORITY_MISMATCH`、`INCARNATION_LOST`、`STALE_GENERATION`、`FENCE_LOST` 应分别对应可验证事实，不能用一个 disposition覆盖多种失败。
4. progress 的“发射前验证 fence”不能被解释为 authoritative commit guarantee。它仍是 best-effort observation；consumer 应能按 execution owner/generation丢弃显然 stale 的事件，或文档明确 race 可接受。
5. `LocalTaskOutputLogReference` 是新 identity。必须明确它是否真的需要跨 package公开；若仅由 Pool内部 writer/reader消费，应保持私有，避免为 process-local实现细节新增 Contracts 公共类型。
6. `WorkflowCancelReason.AGENT_REQUEST` 与 `USER_REQUEST` 的选择必须来自可信 caller/source contract；模型自由文本 detail不得改变 authorization、priority或audit authority。

### 10.7 三次评审要求的最终修订

终审通过前，计划正文必须：

1. 将 immutable creation provenance 与可验证的 access binding 分离。
2. 定义新 incarnation/session resume 对既有 Workflow run 的合法访问与旧 incarnation失权规则。
3. 删除由调用方显式传入普通 `WorkflowCallerAuthority` DTO 的授权方式，改为 composition-bound Port；或完整证明可验证 capability设计的必要性。
4. 禁止 Runtime 拥有或签发 Workflow-specific authority；改为复用通用 caller execution/lineage事实。
5. 明确 Agent terminal、root cancel、Session termination与 durable Workflow lifecycle 的关系。
6. 重排实施切片，使 nominal identity、Port、owner、composition和消费者在同一可合入切片闭合，不产生临时字符串/具体依赖/二次迁移。
7. 为 authority identity、generation、revision和fence分别指定 canonical type与唯一含义。

### 10.8 三次评审判定

| 评审维度 | 三次判定 |
|---|---|
| Product scope与两类执行语义分离 | 通过 |
| Contracts observation分层 | 通过 |
| Local restart与result/artifact边界 | 通过 |
| Progress best-effort语义 | 通过 |
| Cancel/terminal lifecycle语义 | 通过 |
| Multi-destination delivery | 通过 |
| Workflow creation provenance | 方向通过 |
| Workflow access/recovery lifecycle | 不通过 |
| Caller credential可信边界 | 不通过 |
| Runtime/Orchestration authority owner | 不通过 |
| 实施切片零遗留 | 不通过 |
| 可终审通过 | 不通过 |

三次最终结论：**最新修订已经使统一 observation contract本身基本成立，剩余阻塞集中在 Workflow access authority及实施切片原子性。完成上述四类阻塞修订后，可进行下一轮终审；在此之前不得进入 Slice 1。**

---

## 11. 四次评审（终审候选版第二轮）

四次评审对象：状态为“三次评审修订完成，待终审”的最新实施计划  
四次评审结论：**第三轮提出的 authority/recovery、可信 caller、分层 owner和纵向切片问题已经得到实质响应，但 revisioned access binding 新增的 lifecycle 与 Agent lineage、WorkflowRunPhase 和 cancel intent 重复，形成新的双真相与跨状态机崩溃窗口；终审仍不通过。**

### 11.1 第三轮阻塞项关闭情况

| 第三轮阻塞项 | 四次判定 | 说明 |
|---|---|---|
| creation provenance 与 access binding混合 | 已关闭 | 已拆成 immutable provenance 与 access binding |
| 新 incarnation无法恢复访问 | 基本关闭 | 改为 stable logical Agent + canonical lineage verifier |
| caller DTO 可伪造 | 已关闭 | query/cancel Port instance在composition时绑定caller context |
| Runtime生成Workflow authority | 已关闭 | Runtime只提供通用caller execution identity/fence |
| 切片产生临时字符串/二次迁移 | 已关闭 | 已改为 identity→owner→store→Port→composition→consumer的纵向切片 |

以下修订方向正确，应继续保留：

1. creation provenance 只承担 immutable audit，不参与恢复后 incarnation相等判断。
2. Workflow access以 stable logical Agent为 principal，并通过 canonical lineage verifier验证当前 active incarnation/revision/fence。
3. Product/surface不传入caller identity DTO，只取得composition-bound窄 Port。
4. Runtime不理解或签发Workflow-specific authority。
5. Session只属于 creation provenance，不自动成为 durable run access principal。
6. root supervisor使用独立受信governance入口，不通过普通Agent字段 OR 判断扩大权限。
7. 实施切片改为纵向闭环，正式identity/Port与consumer同片迁移。
8. local streaming log reference默认保持Pool包内私有。

### 11.2 阻塞项一：AccessBinding lifecycle复制三个 canonical状态真相

最新计划给 `WorkflowRunAccessBinding` 增加：

```text
lifecycle:
  ACTIVE
  CREATOR_REVOKED
  ROOT_CANCEL_REQUESTED
  CLOSED
```

这四个值并不是一个独立、内聚的 authorization 状态机：

- `CREATOR_REVOKED` 复制 logical Agent lineage terminal/not-active 事实；
- `ROOT_CANCEL_REQUESTED` 复制 root cancellation epoch、Workflow cancel intent或 `WorkflowRunPhase.CANCELLING`；
- `CLOSED` 复制 `WorkflowRunPhase` terminal事实；
- 只有 `ACTIVE` 表示初始access grant，但首版又明确不提供任意transfer API。

结果是同一个事实需要跨多个 owner同步：

```text
Agent lineage lifecycle
    ↔ WorkflowRunAccessBinding.lifecycle

root cancellation epoch / cancel intent
    ↔ WorkflowRunAccessBinding.ROOT_CANCEL_REQUESTED
    ↔ WorkflowRunPhase.CANCELLING

WorkflowRunPhase terminal
    ↔ WorkflowRunAccessBinding.CLOSED
```

这违反“每个概念一个 canonical owner、一条状态真相链”，并引入组合爆炸：Agent 已 terminal但binding仍ACTIVE、run已terminal但binding未CLOSED、root cancel已提交但run尚未CANCELLING等。

首版没有transfer需求时，建议将 access binding 收窄为 immutable grant：

```text
WorkflowRunAccessGrant
  authorized_logical_agent_id
  root_governance_agent_id
```

授权查询实时组合 canonical facts：

- logical caller是否active、incarnation/revision/fence是否当前：由lineage owner回答；
- root/subtree是否取消：由canonical cancellation epoch/snapshot owner回答；
- run是否terminal/cancelling：由 `WorkflowRunProjection` 回答；
- ordinary caller是否匹配grant：由Workflow access adapter比较stable principal。

不要把这些投影结果写回第二个 mutable lifecycle。如果未来确有transfer/revoke access grant的产品需求，再引入单独revisioned grant transition，并只表达grant自身变化，不镜像Agent或Workflow lifecycle。

### 11.3 阻塞项二：Root cancel 的跨状态机写入缺少 durable reconciliation

计划规定 root cancellation 先以 access revision CAS写入 `ROOT_CANCEL_REQUESTED`，再向非terminal run提交Workflow cancel intent。这是两个 canonical transition；当前没有说明它们处于同一事务，也没有 durable reconciliation owner。

崩溃可能发生在：

1. access binding已经写入 `ROOT_CANCEL_REQUESTED`，run仍是RUNNING；
2. run已经进入CANCELLING，access binding仍ACTIVE；
3. cancellation命令revision conflict，但access binding已经推进；
4. root cancellation重试时旧access revision导致永远无法再次提交run cancel；
5. run先terminal，随后迟到的access lifecycle写入覆盖或制造无意义状态。

不能依赖进程内顺序或rollback closure解决。应选择一个 canonical intent owner：

- root/subtree cancellation继续由Agent governance cancellation epoch/snapshot拥有；
- Workflow cancel intent/phase由WorkflowRunControl拥有；
- supervisor以stable request identity向目标Workflow提交幂等cancel command；
- 若跨owner投递需要崩溃恢复，使用已有durable delivery/reconciliation机制或在其canonical owner扩展最小typed command delivery，不把access binding变成第二cancel ledger。

计划必须明确 request identity、claim/fence、revision conflict后的重试、terminal idempotency和reconcile scan。删除 `ROOT_CANCEL_REQUESTED` mirror并不能省略跨ownerdelivery保证。

### 11.4 阻塞项三：Agent terminal后的Workflow治理仍是未决policy

计划写明 logical Agent terminal 后，由root supervisor按canonical policy“继续、取消或结算”。这包含三个 materially different产品行为，但没有选择哪一个，也没有指向当前已存在并确认的policy contract。

该未决决定影响：

- run是否继续执行；
- 谁拥有resume/cancel/query权限；
- result向谁delivery；
- budget、artifact pin和retention是否继续；
- root取消与普通creator terminal如何区分；
- supervisor是否允许直接settle，还是只能提交typed command给Workflow owner。

根据AGENTS.md，supervisor不得伪造terminal或直接修改owner状态；“结算”只能经Workflow canonical control并满足execution/effect/delivery事实。

计划必须明确首版行为。例如可以选择：

```text
creator logical Agent terminal
  -> ordinary creator-bound Port立即失权
  -> durable Workflow保持canonical run state
  -> root supervisor通过governance Port成为唯一控制主体
  -> supervisor依据已确认policy提交continue/cancel命令
  -> 不直接伪造settlement
```

但“依据policy”仍须指出policy owner、typed input/output、默认选择和真实consumer；不能把三选一留给实现者。

### 11.5 阻塞项四：Root governance Port 尚未进入服务面与装配证据

正文依赖“独立受信 governance Port”完成级联取消或接管结算，但当前 Protocol表只列四个普通observation/cancel Port，没有列出：

- governance Port定义位置；
- 消费者（Agent supervisor/cancellation owner）；
- implementation owner；
- command/receipt类型；
- root containment与cancellation epoch如何验证；
- lifecycle/activation/shutdown；
- 是否复用当前Agent control/cancellation基础设施；
- 如何避免形成第二Workflow control path。

若该Port是完成首版authority lifecycle所必需，就必须进入最小服务面证据和production composition；若本任务不准备实现它，就不能在access设计中依赖尚不存在的治理入口。

优先方案不是新增一个万能governance manager，而是复用现有Agent cancellation/control owner，通过消费方最小typed command delivery Port把root cancellation投递给WorkflowRunControl。

### 11.6 文档内部仍需修正的矛盾

以下问题不一定各自阻塞，但必须在终审前归零：

1. 架构门禁第16项要求“无stale-fence emit”，而正文明确承认fence校验后立即失权的竞态信号可能到达。门禁应验证“stale signal不能推进authoritative state，且被后续canonical snapshot覆盖/丢弃”，不能声明物理上零到达。
2. 测试矩阵仍写“caller/agent/session/generation mismatch fail closed”，但正文已规定Session只属于provenance、不参与授权。应删除session mismatch access test，改为Session close/resume后仍由当前logical Agent lineage决定权限。
3. `access_revision` 在没有transfer/revoke grant需求后没有存在理由；若仅用于镜像外部lifecycle，应删除。
4. `creator_lineage_revision` 是创建审计值，恢复查询不能要求它等于当前lineage revision。
5. `root_governance_agent_id` 必须使用canonical stable Agent identity；不能与root path、root owner、tenant或Session owner混用。
6. `WorkflowCancelReason.SESSION_SHUTDOWN_POLICY` 虽被限制为显式Product policy，但正文又决定Session close不终止Workflow。若首版没有真实consumer，应删除该enum成员，禁止为可能未来需求预留。
7. Slice 5与Slice 4都承担“旧面归零”，需要重新划清职责，确保Slice 4结束时不存在已迁移surface的双wire/event路径，Slice 5不是延迟清理兼容残渣的兜底阶段。

### 11.7 建议的最小收敛方案

为避免把统一观测面扩张成第二套Workflow supervision系统，建议首版authority收敛为：

```text
WorkflowRunCreationProvenance（immutable audit）
WorkflowRunAccessGrant（immutable stable principal/root binding）

BoundWorkflowQueryPort
BoundWorkflowCommandPort
  - caller identity由composition绑定
  - lineage verifier校验当前incarnation/revision/fence
  - grant校验stable logical principal/root containment
  - WorkflowRunProjection决定run lifecycle
```

Agent terminal与root cancellation不写access lifecycle：

```text
Agent terminal
  -> lineage canonical fact使ordinary bound Port失权

Root cancellation
  -> canonical cancellation owner提交stable typed cancel request
  -> durable delivery/reconcile
  -> WorkflowRunControl以run revision/fence推进CANCELLING

Run terminal
  -> WorkflowRunPhase本身证明关闭
```

这样可以删除 `CREATOR_REVOKED`、`ROOT_CANCEL_REQUESTED`、`CLOSED` 三个mirror状态，并把每个事实留在原canonical owner。

### 11.8 四次评审要求的最终修订

下轮终审前，计划必须：

1. 删除或重构 `WorkflowRunAccessBinding.lifecycle`，不镜像lineage、root cancellation和run terminal状态。
2. 将root cancellation建模为从canonical governance owner到WorkflowRunControl的stable、typed、幂等、可reconcile command delivery。
3. 明确creator Agent terminal后的唯一首版policy及其owner，不保留“继续/取消/结算”三选一。
4. 将root governance所需Port、command、receipt、consumer、implementation和composition写入服务面证据；或明确复用的现有Port及最小扩展。
5. 修正stale progress gate、Session authorization test、无consumer cancel reason和Slice 4/5职责重叠。
6. 保留第三轮已闭合的composition-bound caller、generic Runtime identity和纵向切片设计。

### 11.9 四次评审判定

| 评审维度 | 四次判定 |
|---|---|
| Async-work observation contract | 通过 |
| Contracts/Port分层 | 通过 |
| Caller可信绑定 | 通过 |
| Incarnation恢复访问方向 | 通过 |
| Runtime/Orchestration owner | 通过 |
| Result/artifact单一真相 | 通过 |
| 纵向实施切片方向 | 通过 |
| Access lifecycle单一真相 | 不通过 |
| Root cancellation崩溃恢复 | 不通过 |
| Agent terminal治理policy | 不通过 |
| Governance服务面闭合 | 不通过 |
| 可终审通过 | 不通过 |

四次最终结论：**统一观测面本身已经基本满足AGENTS.md；当前剩余阻塞来自为访问控制新增的平行lifecycle和未闭合的root supervision路径。将access收窄为stable grant、复用canonical lineage/run/cancellation事实并闭合typed durable command delivery后，才可终审通过。**

---

## 12. 五次评审（终审候选版第三轮）

五次评审对象：状态为“四次评审修订完成，待终审”的最新实施计划  
五次评审结论：**第四轮的四个主阻塞已基本关闭；当前剩余问题集中在 subtree cancellation durable intent 的目标集合、acceptance/settlement 分离和 cancellation/create 原子协调。修订这些协议细节后可进入最终终审。**

### 12.1 第四轮阻塞项关闭情况

| 第四轮阻塞项 | 五次判定 | 说明 |
|---|---|---|
| AccessBinding lifecycle双真相 | 已关闭 | 改为immutable access grant，不镜像lineage/cancel/run状态 |
| Root cancellation崩溃恢复 | 基本关闭 | 已定义stable request、durable inbox/reconciler、claim/fence/retry |
| Agent terminal policy未决 | 已关闭 | 首版固定为ordinary creator失权、run继续、不自动取消/转移/settle |
| Governance Port未闭合 | 已关闭 | 已列明Port、consumer、implementation、composition和lifecycle |

以下修订正确，应保持：

1. `WorkflowRunAccessGrant` 只保存stable logical Agent/root binding，无lifecycle/revision。
2. Agent active/terminal继续由lineage owner证明，run cancelling/terminal继续由WorkflowRunProjection证明。
3. root cancellation通过durable typed delivery进入唯一 `WorkflowRunControl`。
4. durable accept失败不返回ACCEPTED，pending intent由scan/reconciler重新发现。
5. cancel reconciliation绑定claim revision和operation ownership fence，revision conflict有界重读重试。
6. fixed-continue policy不伪造settlement、不修改grant、不建立feature flag。
7. governance Port只注入Agent cancellation coordinator，不暴露普通Agent入口。
8. stale progress门禁已修正为“不能推进authoritative state并由snapshot覆盖/丢弃”。
9. Session authorization测试已改为Session close/resume不改变grant。
10. `SESSION_SHUTDOWN_POLICY` 已从无真实consumer的cancel reason中删除。
11. Slice 4负责旧面归零，Slice 5只复核证据和重签台账，不再延迟生产清理。

### 12.2 阻塞项一：Durable cancellation intent没有保存稳定target集合

当前 `WorkflowGovernanceCancelRequest` 只包含：

```text
root_agent_id
subtree_agent_id
cancellation_epoch
```

但AGENTS.md要求subtree cancellation使用 fenced stable subtree snapshot，并与spawn admission原子协调。仅保存subtree root和epoch不足以让Workflow reconciler在重启后确定目标：

- `WorkflowRunAccessGrant` 只有authorized logical Agent和root identity，没有agent path或subtree membership；
- Workflow owner不应读取Agent lineage私有store；
- cancellation accepted后lineage可能继续变化、Agent可能terminal、path index可能回收；
- reconciler重启后若重新计算subtree，会得到不同target集合；
- 仅按root枚举会错误取消同root下不属于目标subtree的Workflow；
- 仅比较authorized Agent与subtree root会漏掉全部descendant。

durable request必须绑定取消时已经fenced的stable target snapshot，例如：

```text
WorkflowGovernanceCancelRequest
  request_id
  root_agent_id
  subtree_agent_id
  lineage_snapshot_revision
  cancellation_epoch
  target_agent_ids: tuple[AgentId, ...]
```

若target集合可能很大，可以使用canonical artifact/reference，但该reference必须是typed、durable、受retention/pin保护的snapshot fact，不能只保存可重新查询的subtree ID。

Workflow owner只能按request中已提交的target AgentId匹配immutable run grant。unknown、duplicate、wrong-root target、snapshot revision mismatch和stale epoch必须strict reject。该snapshot与spawn admission/cancellation epoch的原子关系应复用现有lineage cancellation owner保证。

### 12.3 阻塞项二：Submit receipt混合durable acceptance与最终run settlement

当前 `WorkflowGovernanceCancelDeliveryReceipt` 同时包含：

```text
disposition: ACCEPTED | IDEMPOTENT | ...
run_settlements[]
```

但submit返回durable `ACCEPTED` 时，reconciler尚未必完成per-run cancel。AGENTS.md明确规定durable accepted只表示intent已提交，不表示目标已处理。因此一个同步submit receipt不能可靠携带最终 `run_settlements[]`。

应拆成两个阶段：

```text
WorkflowGovernanceCancelAcceptance
  request_id
  disposition: ACCEPTED | IDEMPOTENT | ...
  accepted_revision
  target_count

WorkflowGovernanceCancelSettlementSnapshot
  request_id
  revision
  lifecycle: PENDING | RECONCILING | SETTLED | PARTIAL | DEAD_LETTER
  per_run_settlements[]
```

若 `IDEMPOTENT` 请求已经terminal，可以另由query返回settlement snapshot；submit仍不应让同一字段在有时表示空pending、有时表示最终结果。必须定义settlement query/notification的consumer、Port、retention和strict codec，或者明确Agent cancellation coordinator只需要acceptance，不同步等待最终settlement。

### 12.4 阻塞项三：Scope acceptance与per-run intent枚举的崩溃边界未闭合

计划称Workflow owner“内部按immutable grant枚举scope run”，但没有明确：

- scope request durable accept发生在枚举之前还是之后；
- target run set是否在accept transaction中冻结；
- accept后、per-run intent全部创建前crash如何恢复缺失项；
- 新run在同一Agent cancellation epoch之后创建时是否会被漏掉；
- 重复scan如何证明不会创建第二per-run intent。

建议由canonical inbox owner保存：

```text
scope request + stable target Agent snapshot
```

reconciler每次scan从Workflow canonical run store按immutable grant进行确定性join，并以：

```text
per_run_request_id = hash(scope_request_id, WorkflowRunId)
```

幂等提交per-run intent。scope settlement只有在所有属于已冻结target集合且满足明确cutoff规则的run都terminal/idempotently settled后才能完成。

必须明确run inclusion cutoff：

- 推荐以Workflow create authority验证cancellation epoch，在epoch已取消后拒绝目标Agent创建新run；
- 已与run create原子提交且早于取消snapshot的run纳入本次request；
- 取消snapshot之后合法创建是否可能发生，若不可能由lineage/create admission门禁证明；
- 不能使用当前scan时间或内存可见集合决定历史scope。

### 12.5 阻塞项四：Fixed-continue receipt是无副作用的第二投影机制

计划新增：

```text
AgentTerminalFact
  -> WorkflowCreatorTerminalReceipt(
       disposition=CONTINUING,
       affected_run_ids,
     )
```

但fixed-continue的实际语义是“不修改run、不修改grant，由lineage NOT_ACTIVE使ordinary Port失权”。这个结论已经可以从canonical lineage和immutable grant确定性投影，不需要额外terminal consumer、run枚举或receipt。

新增该receipt会带来：

- 为了返回 `affected_run_ids` 扫描Workflow run store；
- Agent terminal与Workflow bounded context之间新增无状态变化的耦合；
- receipt是否持久、是否重放、是否遗漏run的额外问题；
- 一个仅用于证明“什么都不做”的第二projection path。

除非存在已确认的真实消费者需要这份不可由现有query得到的receipt，否则应删除 `WorkflowCreatorTerminalReceipt` 和专门的Workflow supervision adapter。首版policy可以直接写成：

```text
lineage terminal fact使ordinary creator-bound access fail closed；
Workflow owner不订阅terminal做状态写入，run按自身canonical lifecycle继续。
```

若产品需要向用户展示“creator已terminal但Workflow继续”，由observation adapter读取lineage authorization结果和run facts做只读typed projection，不新增canonical fact。

### 12.6 必须修正的残余矛盾

1. Workflow cancel receipt列表仍包含 `ACCESS_REVISION_CONFLICT`，但access grant已经immutable且无revision，应删除。
2. governance acceptance中的 `BACKPRESSURED` 必须明确是“未accepted且未写intent”，还是“已accepted但暂未处理”；不能让同一disposition跨durability边界。
3. `run_settlements` 中每项需要typed run reference、per-run request revision和terminal/idempotent disposition，不能使用裸tuple或裸字符串。
4. fixed-continue情况下paused run可能长期无人resume。该产品决定已经明确，可接受，但observation应向有权surface准确展示“creator unavailable/root cancellation only”等available actions，不能继续显示普通resume。
5. root governance首版没有query/resume公共面，因此governance cancellation settlement至少需要内部可观测性和审计，避免accepted intent永久卡住而无人发现。
6. Slice 1已经很大，包含全仓Agent/Session nominal identity迁移、Workflow store/control/result、lineage verifier、governance durable inbox和真实surface consumer。实施时若拆分，必须继续按完整vertical invariant拆分，不能回退到临时alias或旧字符串并存。

### 12.7 五次评审要求的修订

最终终审前，计划必须：

1. 将fenced stable subtree target集合或其canonical durable reference写入governance cancel request。
2. 分离scope durable acceptance与异步per-run settlement，不在submit receipt中混合两种承诺。
3. 明确scope accept、run-set冻结/确定性join、per-run intent创建和crash recovery协议。
4. 将Workflow create admission与Agent cancellation epoch协调，证明取消snapshot后不会漏入新run。
5. 删除无真实副作用/consumer的 `WorkflowCreatorTerminalReceipt`，或证明其必要性、durability和唯一consumer。
6. 删除已失效的 `ACCESS_REVISION_CONFLICT` disposition，并澄清BACKPRESSURED的durability语义。

### 12.8 五次评审判定

| 评审维度 | 五次判定 |
|---|---|
| Async-work observation union | 通过 |
| Contracts/Port/owner分层 | 通过 |
| Workflow access grant单一真相 | 通过 |
| Caller/incarnation恢复语义 | 通过 |
| Fixed-continue产品决定 | 通过 |
| Governance Port方向 | 通过 |
| Root cancellation target snapshot | 不通过 |
| Durable acceptance/settlement分离 | 不通过 |
| Cancellation/create原子协调 | 不通过 |
| Terminal receipt最小必要性 | 不通过 |
| 可最终终审通过 | 不通过 |

五次最终结论：**主体架构已经接近可实施，剩余阻塞不再是统一观测面的domain设计，而是root/subtree cancellation durable协议的精确性及一个无必要receipt。补齐stable target snapshot、acceptance/settlement、create-admission协调并删除多余terminal projection后，可进行最终终审。**

---

## 13. 六次评审（最终终审候选）

六次评审对象：状态为“五次评审修订完成，待最终终审”的最新实施计划  
六次评审结论：**第五轮四个阻塞项均已正面修订；当前只剩两个底层contract/recovery阻塞：Workflow governance反向依赖async-work observation reference，以及Workflow create RESERVED admission缺少可恢复intent或安全abort协议。关闭后可终审通过。**

### 13.1 第五轮阻塞项关闭情况

| 第五轮阻塞项 | 六次判定 | 说明 |
|---|---|---|
| Stable subtree target snapshot | 已关闭 | request持久化target AgentId、lineage revision、epoch和admitted create IDs |
| Acceptance/settlement混合 | 已关闭 | submit只返回acceptance，settlement由独立query Port读取 |
| Cancellation/create原子协调 | 基本关闭 | 新增Agent-governance-owned Workflow create admission并与snapshot互斥 |
| Fixed-continue terminal receipt | 已关闭 | 删除terminal订阅、receipt和affected-run枚举，只做确定性只读投影 |

以下修订已达到预期，应保留：

1. cancellation request保存fenced frozen target Agent集合，不在recovery时重算历史subtree。
2. 同一snapshot冻结已RESERVED/COMMITTED Workflow create admission identity，避免半完成create漏取消。
3. `BACKPRESSURED` 明确表示未accepted且未写intent。
4. acceptance、settlement snapshot和per-run settlement是三个职责分明的typed contract。
5. scope accept transaction原子保存request及frozen cutoff，不要求同步枚举全部run。
6. per-run request identity由scope request + RunId确定性派生，partial creation crash可由scan补齐。
7. settlement只有在frozen admission和per-run intent全部结算后才能完成，poison不会伪装成功。
8. creator terminal不再产生Workflow mirror fact或无副作用receipt。
9. creator unavailable时available actions通过run/grant/lineage facts只读投影。
10. governance acceptance与settlement query已有独立真实consumer和lifecycle。
11. `ACCESS_REVISION_CONFLICT` 已从普通Workflow cancel disposition删除。

### 13.2 阻塞项一：Workflow governance反向依赖async-work observation reference

最新计划在 `WorkflowGovernanceRunSettlement` 中使用：

```text
reference: DurableWorkflowRunReference
```

但该类型当前定义在 `contracts/async_work`，是 `AsyncWorkReference` 的durable variant。Workflow governance是canonical Workflow control/delivery语义，不应依赖统一观测包的variant类型，否则依赖关系会变成：

```text
contracts/workflow governance
    -> contracts/async_work observation identity
```

这使presentation-neutral aggregator package反向成为Workflow domain identity owner，也会迫使未来非observation Workflow command、reconciliation和store使用async-work类型。

应建立Workflow-owned canonical reference：

```python
# contracts/workflow/identity.py
@dataclass(frozen=True, slots=True)
class WorkflowRunReference:
    run_id: WorkflowRunId
    definition_id: WorkflowDefinitionId
```

然后async-work只包装它：

```python
@dataclass(frozen=True, slots=True)
class DurableWorkflowRunReference:
    kind: Literal[AsyncWorkKind.DURABLE_WORKFLOW_RUN]
    reference: WorkflowRunReference
```

Workflow governance settlement、Workflow-specific query/control、resume/node inspection和terminal result全部使用 `WorkflowRunReference`。只有Product统一观测/command入口使用外层 `DurableWorkflowRunReference`。

必须同时门禁：

- `contracts/workflow` 不import `contracts/async_work`；
- Orchestration Workflow canonical owner不import async-work variant作为内部identity；
- async-work wrapper不复制RunId/DefinitionId字段形成第二identity shape；
- Product dispatcher只在边界解包一次，不靠cast或字符串重建。

### 13.3 阻塞项二：RESERVED admission无法确定性恢复或安全abort

计划中的 `WorkflowCreateAdmission` 当前只保存：

```text
admission_id
logical_agent_id
root_agent_id
lineage_revision
cancellation_epoch
lifecycle
```

若进程在admission变为RESERVED之后、Workflow run commit之前崩溃，reconciler只能看到“有一个Workflow create reservation”，却不知道：

- 目标 `WorkflowRunId`；
- `WorkflowDefinitionId`/definition digest；
- canonical create request identity；
- 初始checkpoint/frontier/deadline；
- 是否已有另一个owner正在合法继续create；
- 应重放create还是abort reservation。

“reconcile为已提交run或ABORTED”目前不是确定性协议。直接按“未找到run”abort会与仍在执行的合法creator竞争；盲目重建又缺少create intent payload。

必须选择并写清一种方案。

方案A：admission只承担治理reservation，不负责重建run：

- `admission_id` 与确定性 `WorkflowRunId`/create request identity绑定；
- create owner在operation ownership fence下执行reserve→run commit→admission settle；
- admission reconciler先claim reservation并验证原create owner fence已失效；
- 若按admission identity能查到已commit run，则幂等COMMITTED；
- 若确认不存在run、旧owner已失权且无外部动作，则幂等ABORTED；
- ABORTED不重建run，原调用方只能以同一stable create request重试并取得新/幂等admission语义；
- cancellation snapshot等待RESERVED被上述fenced reconciliation结算。

方案B：admission同时是durable Workflow create intent：

- 严格、版本化保存完成create所需的canonical payload或其durable reference；
- reconciler可以从intent确定性构造相同RunId并调用唯一Workflow create chokepoint；
- definition、checkpoint、artifact retention和未知effect语义全部闭合；
- 这不能建立第二create store/state machine，需证明复用现有Workflow durable create owner。

首版优先方案A，服务面更小，也不会把Agent lineage admission store扩张成Workflow definition/checkpoint owner。但无论选择哪一个，都必须补充：

1. admission与RunId/create request的stable映射；
2. reserve/commit/abort的expected revision和operation fence；
3.旧creator与reconciler竞争的唯一胜者；
4. run commit成功、admission settle失败的幂等恢复；
5. admission abort后同request重试的语义；
6. cancellation snapshot等待与dead-letter/poison处置；
7. strict codec和fault-injection tests。

### 13.4 终审前应一并修正的细节

1. `WorkflowGovernanceRunSettlement.disposition=CANCEL_REQUESTED` 是delivery settlement，不是run terminal settlement。建议命名为 `CANCEL_INTENT_APPLIED` 或在文档中明确scope `SETTLED`只表示所有cancel command已结算，不表示所有run已进入terminal。
2. `WorkflowGovernanceCancelSettlementSnapshot.lifecycle=PARTIAL` 与 `DEAD_LETTER` 的terminal性、retry资格和retention应明确；避免PARTIAL既可重试又被当作terminal。
3. `target_count` 应说明统计Agent target、admission还是run，建议使用不含糊的 `target_agent_count`。
4. frozen target/admission tuple虽受治理cap限制，strict decoder仍需验证最大长度，防止wire/durable payload绕过cap。
5. 旧durable Workflow run migration必须生成或明确缺失provenance/access grant/admission的处置；不能只迁移RunId和terminal result。
6. Slice 1范围仍很大。若实施中拆分，必须保持canonical Workflow reference与其所有直接consumer同片迁移，禁止临时re-export。

### 13.5 六次评审要求的最后修订

最终终审前，计划只需关闭：

1. 建立 `contracts/workflow/identity.py::WorkflowRunReference`，让async-work variant包装而非复制，并禁止Workflow owner反向依赖async-work包。
2. 明确 `WorkflowCreateAdmission` 的fenced recovery方案，补足stable RunId/create request映射与reserve/commit/abort竞争语义。
3. 澄清governance scope settlement不等于run terminal，并明确PARTIAL/DEAD_LETTER状态语义。
4. 将旧run的provenance/grant/admission migration纳入durable数据盘点。

### 13.6 六次评审判定

| 评审维度 | 六次判定 |
|---|---|
| Async-work产品与observation contract | 通过 |
| BackgroundTask/Workflow执行边界 | 通过 |
| Identity/CAS/result/progress/wire方向 | 通过 |
| Caller/access/lineage恢复 | 通过 |
| Root cancellation stable snapshot | 通过 |
| Durable acceptance/settlement | 通过 |
| Fixed-continue与available actions | 通过 |
| Workflow canonical reference owner | 不通过 |
| Workflow create admission recovery | 不通过 |
| 可最终终审通过 | 暂不通过 |

六次最终结论：**需求的产品、domain、观测、授权与级联取消设计已经基本闭合。剩余两个问题都是底层owner/recovery精度问题，不需要再改变产品体验。完成canonical WorkflowRunReference与fenced create admission recovery后，可预期下一轮终审通过。**

---

## 14. 七次评审（最终确认）

七次评审对象：状态为“六次评审修订完成，待最终确认”的最新实施计划  
七次评审结论：**终审通过。计划已达到可以按既定纵向切片进入实施的条件；通过仅表示需求与实施设计闭合，不表示生产实现、迁移或测试已经完成。**

### 14.1 第六轮阻塞项关闭情况

| 第六轮阻塞项 | 七次判定 | 说明 |
|---|---|---|
| Workflow canonical reference owner | 已关闭 | `WorkflowRunReference` 归属 `contracts/workflow/identity.py`，async-work variant只包装 |
| Workflow create admission recovery | 已关闭 | 选择治理reservation方案A，补齐stable映射、revision/fence、claim、COMMITTED/ABORTED恢复 |
| Governance scope settlement语义 | 已关闭 | `CANCEL_INTENT_APPLIED` 明确只表示cancel command结算，不表示run terminal |
| PARTIAL/DEAD_LETTER语义 | 已关闭 | PARTIAL可有界重试且非terminal，DEAD_LETTER为retry exhausted terminal delivery settlement |
| 旧run authority/admission migration | 已关闭 | 纳入证据驱动的一次性幂等migration，无法证明的authority fail closed |

### 14.2 最终通过理由

#### 14.2.1 产品决定闭合

计划始终保持且最终闭合了核心产品边界：

```text
统一的是用户观察与操作入口；
不统一BackgroundTask与Workflow的执行、状态机、durability和recovery。
```

首版scope收窄为当前Agent，不承诺尚无治理基础设施支持的跨Agent全局列表或分页，避免以伪统一体验制造第二registry。

#### 14.2.2 Identity只有一个canonical owner

- Local identity复用完整 `LocalTaskReference`；
- WorkflowRunId、WorkflowDefinitionId和二者关系由Workflow-owned `WorkflowRunReference`唯一表达；
- `DurableWorkflowRunReference` 只是async-work tagged variant包装，不复制ID字段；
- Workflow governance、query/control、resume/node inspection、terminal result和Orchestration内部链不依赖async-work observation package；
- Product只在统一边界解包一次，不使用prefix、cast或字符串重建。

因此domain identity与presentation variant的owner方向已经正确。

#### 14.2.3 状态真相链闭合

- BackgroundTask state只由Agent-owned Pool拥有；
- Workflow run state只由canonical WorkflowRunProjection/WorkflowRunControl拥有；
- access grant不镜像Agent lineage、root cancellation或run lifecycle；
- creator terminal只由lineage NOT_ACTIVE证明，不产生Workflow mirror fact；
- root cancellation intent由Agent governance cancellation snapshot拥有，通过durable delivery进入唯一Workflow control chokepoint；
- presentation phase/action只做总投影，不能反向驱动domain state；
- best-effort progress不冒充durable history。

不存在第二mutable phase、第二cancel ledger或observation state双写。

#### 14.2.4 Durable create/cancel恢复语义闭合

Workflow create admission已经明确选择最小方案A：

```text
admission只负责治理reservation；
不保存checkpoint/frontier/deadline；
不盲目重建Workflow run；
不成为第二create store。
```

同时具备：

- stable admission/create request/RunId/definition映射；
- expected revision和operation fence；
- reserve→run create→commit唯一顺序；
- run已commit但admission未settle的幂等恢复；
- 原owner失权后reconciler唯一claim；
- 无run且无外部动作时的fenced ABORTED；
- 同request在ABORTED后的typed rejection；
- 与subtree cancellation snapshot的原子协调。

Root cancellation也已具备frozen target/admission cutoff、durable acceptance、异步settlement、stable per-run request identity、partial creation crash scan、revision retry、terminal idempotency和dead-letter可观测性。

#### 14.2.5 Authorization边界闭合

- creation provenance与access grant分离；
- provenance只用于审计，不要求恢复后的incarnation等于creator incarnation；
- access principal使用stable logical Agent；
- 当前incarnation/revision/fence由canonical lineage verifier验证；
- caller context在Product composition时绑定，不由模型、wire或普通DTO自报；
- Runtime只提供通用execution caller identity/fence，不理解Workflow authority；
- Session、definition和delivery destination均不被误用为access authority；
- root governance只走独立受信Port。

这满足恢复后新incarnation合法访问、旧incarnation立即失权及越权fail closed的要求。

#### 14.2.6 Result、artifact和replay边界闭合

- local stored terminal result迁移到canonical `ArtifactRef`；
- process-local streaming log与terminal result明确分离，默认保持Pool内部实现；
- Workflow terminal result使用strict tagged union和versioned codec；
- pause不进入terminal union；
- effect/delivery IN_DOUBT不冒充run terminal phase；
- local observation/progress不进入durable replay；
- durable Workflow observation从run/result/delivery canonical facts重建；
- 旧durable数据按证据迁移，不生成缺省authority或双读fallback。

#### 14.2.7 服务面与composition闭合

计划明确了：

- 四个async-work observation/command最小Port；
- 两个Workflow governance acceptance/settlement Port；
- 每个Port的consumer、implementation owner和lifecycle；
- Product是唯一composition root；
- aggregator只做无状态dispatcher；
- Pool、store、reconciler、lease、task、lock和routing实现全部隐藏；
- live progress复用现有event bus，不新增subscription系统。

没有巨型Manager或平行production入口。

#### 14.2.8 实施切片满足零遗留原则

切片已经按纵向不变量组织：

```text
identity
-> owner
-> codec/store
-> Port
-> composition
-> production consumer
-> 删除旧面
```

正式nominal identity、authority、store/control和真实consumer同片迁移，不再出现先使用裸字符串/临时concrete dependency、后续二次迁移的阶段性债务。Slice 4负责生产旧面归零，Slice 5只复核证据和台账，不承担延迟兼容清理。

### 14.3 实施阶段必须保持的条件

以下不是新的评审阻塞，而是终审通过所依赖的实施条件；任何实现偏离都应重新评审：

1. `WorkflowRunReference` 必须是Workflow domain唯一canonical reference；不得为了调用方便重新复制RunId/DefinitionId shape。
2. Workflow create admission不得演化为保存definition/checkpoint并重放run的第二create engine。
3. admission reconciler只有在证明旧owner fence失效后才能claim/abort，不能用超时snapshot代替ownership事实。
4. `ABORTED` admission不得通过相同request identity静默重开。
5. cancellation request必须持久保存frozen target AgentId和admission IDs；recovery不得重算历史subtree。
6. governance `ACCEPTED` 只表示intent durable commit，不表示per-run cancel已执行。
7. scope `SETTLED` 只表示所有cancel command delivery已结算，不表示所有Workflow run已terminal。
8. PARTIAL/DEAD_LETTER必须保持计划定义的retry/terminal语义，不能被UI折叠成成功。
9.旧run migration不得伪造creator/access事实；证据不足时必须fail closed并请求用户处置。
10. creator terminal的fixed-continue不得新增mirror state、terminal subscriber或无副作用receipt。
11. root supervisor不得直接修改run store或伪造terminal settlement。
12. progress event不得推进authoritative state或成为唯一recovery依据。
13.任何切片结束时不得保留alias、re-export、双decoder、双event/control path或无consumer DTO。
14. 按用户确认的资源限制分批运行测试，但所有列明架构门禁和受影响节点必须有逐项结果记录。

### 14.4 非阻塞文字修正建议

实施前可顺手提高文档精度，但不影响终审通过：

1. 在 `CancelDurableWorkflowRun` 示例中明确其属于async-work Product command wrapper；Workflow内部control command使用 `WorkflowRunReference`，避免读者误以为canonical control依赖外层variant。
2. 将 `WorkflowGovernanceCancelSettlementSnapshot.PARTIAL` 注释为“non-terminal/retryable”，将 `DEAD_LETTER` 注释为“terminal delivery failure”。
3. 为frozen target/admission tuple在contract表中直接写出最大cap来源，便于strict decoder实现。
4. 在admission codec说明中注明 `owner_operation_subject` 和 `owner_fencing_token` 使用现有canonical operation ownership type，而非新增字符串字段。
5. Slice 1如因规模拆分，需在拆分说明中逐项证明每个子切片仍有真实consumer且不存在临时compat面。

### 14.5 七次评审最终判定

| 评审维度 | 最终判定 |
|---|---|
| 产品目标与用户体验 | 通过 |
| BackgroundTask/Workflow执行边界 | 通过 |
| Canonical identity与owner | 通过 |
| Contracts分层与最小Port | 通过 |
| Workflow access与incarnation恢复 | 通过 |
| Workflow create admission recovery | 通过 |
| Root/subtree cancellation durability | 通过 |
| Observation/progress/result/replay | 通过 |
| Composition与production唯一入口 | 通过 |
| Migration与旧面退出 | 通过 |
| 定向测试与架构门禁定义 | 通过 |
| 实施计划可开工 | **通过** |

七次最终结论：**终审通过。该计划可以进入实施；实施必须严格保持canonical owner、fenced recovery、durable acceptance语义和每切片零遗留要求。**
