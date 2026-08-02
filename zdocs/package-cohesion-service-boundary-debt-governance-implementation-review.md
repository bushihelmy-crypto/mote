# Package Cohesion 与 Service Boundary 债务治理实施文档评审

评审对象：`zdocs/package-cohesion-service-boundary-debt-governance-implementation.md`  
日期：2026-07-31  
结论：**暂不批准作为实施编排文档。** BackgroundTask、Workflow、Agent、类型和治理原则总体正确，但下列阻断项会导致错误 owner、超大切片或迁移残渣。修订完成后可复审。

阅读说明：第1–24节保留逐轮源码证据与推导；第25节给出实施依赖DAG；第27节取代第25.4节的待决列表并完成产品语义裁决；第28节是唯一最终阻断清单与审批结论。历史章节中的中间判断不得覆盖后续裁决。

## 1. 阻断问题

### 1.1 G5 与每切片 DoD 冲突，会制度化迁移残渣

原文第 80–105 行和第 356–368 行正确要求每个切片同步完成 consumer migration、old-path deletion 与 gates；但第 122–134 行和第 304–323 行又把“删除旧入口与迁移残渣”单列为后置 G5，第 370–383 行进一步把 package-root/private import/compat residue 删除排到第 9 批。

这违反 `AGENTS.md` 的零兼容债务原则。旧入口、alias、fallback、错误 export、旧 state 和对应门禁必须属于替代它们的同一个垂直切片。G5 只能做全局审计，发现残渣即退回原切片，不能成为计划内清理阶段。

### 1.2 P0 条目仍是横向“大项目”，不是可独立验收的垂直切片

第 159–173 行将 definition、run state machine、store、CAS、lease/fencing、effect reconciliation、Session resume、Residency recovery 和多个旧 owner 删除合为一个 Workflow 切片；Agent 条目同样聚合 capacity、lineage、placement、delivery、mailbox、dead-letter 和 cancellation。

这些能力虽属于同一最终状态机，但不能一次性并行落地。实施文档必须给出在不保留双路径前提下的纵向依赖切片。例如 Workflow 至少先固定 versioned definition/run identity 与 strict decoder，再闭合 fenced run ownership，再闭合 checkpoint/frontier recovery，最后接 effect intent/receipt reconciliation；每片必须有明确生产 consumer、激活入口、删除清单和 fault matrix。Agent 也应按 durable lineage/admission、incarnation fencing、delivery ledger/reconcile、cascade cancellation 分片，并明确共享 identity 如何贯穿。

### 1.3 `build_engine` 被错误指定为 application composition owner

第 223 行要求“保留 `build_engine` 作为 application composition owner”。当前 `product/entrypoints/cli/bootstrap.py::build_engine` 是 CLI-owned bootstrap，包含 first-run 文件播种、locale、cwd、配置加载，并通过 `asyncio.run` 装配服务；`product/composition/bootstrap.py::build_application` 才是 Product composition 包内入口。

把 CLI bootstrap 固化为 application canonical owner 会让 gateway/daemon/SDK 依赖 CLI policy，直接违反唯一 Product composition root。应先列出 `build_engine` 与 `build_application` 的真实消费者和 lifecycle，确认 Product-owned application factory 为 canonical owner；CLI 只做 source/presentation adaptation。完成迁移后删除重复构造链，而不是给现有函数名加权威地位。

### 1.4 Projection 结论已被源码证据推翻

第 291 行仍称 `runtime/projections` 只需收窄注册面、未证明 owner 应拆分。当前源码已经证明存在两个不同 bounded context：

- `RuntimeProjectionRegistry`/`RuntimeProjectionReconciler`：checkpoint → artifact publication → ack/retry/dead-letter；
- `SessionProjectionState`/`SessionLiveProjection`：Session event stream 的 replay/live read model。

它们的 input、state、lifecycle 和 output 均不同。通用 artifact projection/reconciliation 管线应保持内聚，Canvas/Notebook projector 不应按输出名称拆散；Session read model 应迁入 `runtime/session/`。迁移同时删除旧 package export/component key，不保留 alias 或第二 registry。

### 1.5 Hook 仍把已经确认的安全边界降为待证候选

第 248–258 行只要求 typed manifest，并称是否拆 runner 取决于后续证据。当前证据已足够：

- `contracts/ports/hook/runner.py` 使用 `event: str` 与裸 `dict`；
- `runtime/hook/command_handler.py` 直接执行配置中的 shell command；
- 该路径使用 `asyncio.create_subprocess_shell`，未经过 canonical classifier、permission、approval 和 sandbox runner；
- timeout/spawn failure 统一返回 `EMPTY`，无法区分 observation hook 的 best effort 与 `PreToolUse` 控制 hook 的 fail-closed disposition。

正确要求是保留内聚的 Hook registration/matching/folding owner，但把外部命令执行作为受治理的 trust adapter，复用现有 canonical command path，不新建平行 `HookCommandRunner`。内部调用改为 closed typed invocation；观察 hook 可显式 best effort，影响权限或执行的 control hook 必须 fail closed。配置中的 raw command 必须由 Product provenance/trust/approval 决策。

### 1.6 阶段顺序按严重度串行，未表达真实 contract 依赖

第 122–134 行和第 370–385 行把所有 P0 放在 composition、泛型和 BackgroundTask 之前。实际依赖不是单向瀑布：

- Workflow effect execution 依赖受治理 runner/permission seam；
- Workflow 与 Agent durable envelope 依赖各 domain authoritative schema，而不是先造共享 codec；
- BackgroundTask residency pin 依赖 Agent incarnation/residency identity；
- per-session hosting 与 Agent lifecycle owner 会共同修改 composition；
- import purity 是持续门禁，不应把所有无关 contract 设计完全阻塞。

应改成依赖 DAG：G0 先恢复可执行门禁；之后允许只读 capability/consumer 取证并行，但编码切片必须由其上游 contract/identity 是否已固定决定。批次编号不能替代逐切片 `requires`、`owns`、`deletes` 和 `unblocks`。

## 2. 重要修订

### 2.1 文档权威关系尚未闭合

第 3 行宣称可用于实施，第 24–28 行又声明核心总账是唯一状态 owner，本文件不维护状态。这个定位本身可行，但每个实施切片必须引用一个已更新、内容一致的 R 条目；不能仅“对应”多个旧 R 编号后直接开工。若核心总账仍保留旧 owner/方案，应先原子更新总账，再下发切片。

建议为每个切片增加固定字段：`ledger_owner`、`requires`、`canonical_owner`、`production_entry`、`consumer_set`、`deletes`、`tests`、`gates`。本文件只维护依赖模板，不维护完成状态。

### 2.2 G0 不应重复 local-import 误报

生产 import 必须位于模块顶部这一治理规则正确，static local-import gate 当前也已通过。此前标注的 `base_node.py:33` 是 docstring 示例，CLI 对 optional dependency 的导入是模块顶部 `try/except ImportError`。G0 的真实已复现缺陷是 package-root eager import 导致 optional `pyte` 污染 collection，以及随后暴露的 `Application` import cycle；实施文档应明确删除旧误报，避免执行者修改合法 guarded import。

### 2.3 Squilla 不应新建未经证明的 Model service

该文档没有明确撤销旧审计中的 `RoutingModelService/Decision` 方案。`product.routing.squilla.strategy`、predictor 和 postprocess 位于同一 bounded context，private import 是内部正式 seam 与宽类型问题，不足以证明独立 service/lifecycle。实施规格应明确：在 Squilla owner 内提取 immutable route identity/order、typed policy input/output 和公开内部 seam；只有模型 artifact activation/lifecycle 出现独立消费者时才建立 service。

### 2.4 测试矩阵必须按保证适用，不能机械套用

第 337–345 行把 fsync、lease、ABA、supervisor restart 等列为所有 durable/concurrent 切片的共同下限。不同机制并不都拥有这些保证，例如 process-local BackgroundTask 不应测试跨进程恢复。每个切片应从其 contract 声明的 durability、ownership 和 failure boundary选择适用矩阵；不适用项必须标记理由，不能为了满足模板给临时机制添加 durable state。

## 3. 已确认正确、应保留的内容

- 每 Agent/Role 一个 canonical `BackgroundTaskPool`；禁止 process singleton/shared task registry。
- pending BackgroundTask pin residency，release 只结算自己的 pool。
- Workflow durable state 与 BackgroundTask process-local state 严格分离。
- supervisor 只持有窄 admission/permit 能力，不拥有各 pool mutable state。
- Product 是唯一可信 composition root，Product 装配下层 concrete implementation 本身合法。
- 泛型从 definition 到 outcome 端到端保留。
- 不以 manager 大小、依赖数量、命名后缀或包根导出数量机械拆服务。
- 每切片要求复用证据、consumer migration、old-path deletion、测试和精确门禁。

## 4. 复审准入条件

修订稿至少应满足：

1. 删除后置“清理阶段”，把删除项和门禁归入各自垂直切片；
2. 把 Workflow、Agent 等大项拆成有依赖关系且可独立验收的切片；
3. 撤销 `build_engine` 的 canonical owner 指定，按 Product composition 与 CLI adapter 分界重建调用链；
4. 修正 Projection 与 Hook 的 owner、安全和失败语义；
5. 用依赖 DAG 代替单纯 P0→P1→P2 瀑布；
6. 明确 local-import 旧误报和 Squilla 不新建伪 service；
7. 保证每个可下发切片先有唯一核心总账 R owner，且总账内容与本文件一致。

满足以上条件前，只允许继续取证和修订需求，不应启动生产代码批量重构。

## 5. 第二轮：实施覆盖与删除安全性

### 5.1 实施文档没有覆盖全部已接受债务

审计共有 38 个 SB 条目，新文档只显式引用了少数编号；范围写法 `SB1.1–SB1.4`、`SB1.8–SB1.10` 虽扩大了名义覆盖，但没有为其中每项提供独立 disposition。以下已确认问题没有形成可下发切片：

- SB0.4：固定内部 argv 与用户可控 shell command 混合的 runner trust boundary；
- SB1.7：CodeMap 与 LSP 各自的 query/ingestion/context-source typed boundary；
- SB1.11–SB1.18：ToolExecutor public surface、provider registry snapshot、Environment 删除、error owner、presentation contract、tool config defaults 与 namespaced error ABI；
- SB2.1–SB2.15：多数只被 G4 的泛化 bullet 提到，没有 ledger owner、consumer set、删除清单与验收。

作为实施编排文档，不能靠“包根收窄”“private import 清理”等横向任务覆盖这些不同 owner。必须为全部 SB 给出 disposition matrix：`IMPLEMENT`、`MERGED_INTO <slice>`、`REJECTED`、`EVIDENCE_ONLY`。只有前两类可以进入实施，且必须指向唯一核心总账条目和具体切片。

### 5.2 SB0.4 的缺失会让 Workflow 与 Hook 重复建立进程执行路径

当前源码至少存在 `runtime/process.py::aexecute`、Hook 的 `create_subprocess_shell`、工具 permission/sandbox pipeline，以及多个固定 argv process adapter。它们的信任域和保证不同。新文档安排 Workflow effect runner、Hook command adapter 和 import/private cleanup，却没有先闭合 SB0.4。

需要先建立 capability matrix，区分：

- 用户可控 shell command：classifier、permission、approval、sandbox、typed receipt；
- 固定内部 argv：结构化 argv、明确 executable identity、必要的 sandbox/egress policy；
- 长驻交互进程：独立 lifecycle/PTY contract；
- Workflow external effect：durable intent/receipt 包裹受治理 runner，但不复制 runner。

这不表示建立一个万能 ProcessManager；各消费者复用其所需的最小 canonical runner seam。SB0.4 应成为 Hook 和 Workflow effect 切片的显式前置依赖。

### 5.3 按名称删除 `BgTaskResult`/`BgStatus` 不安全

第 309 行要求删除 `BgTaskResult` 和“同名 `BgStatus`”。当前至少存在：

- `orchestration/background_tasks/model.py::BgTaskResult` 与 `background_tasks/status.py::BgStatus`；
- `orchestration/workflows/deferred.py` 的 `BgTaskResult` alias；
- `orchestration/workflows/types.py::BgStatus`；
- Product tools、Workflow engine、BackgroundTask pool 和大量测试分别消费这些类型。

真正不变量是 Workflow 与 BackgroundTask 各自拥有不同 authoritative result/status contract，不能相互冒充。实施要求应按 defining module、语义和 consumer 迁移：删除 Workflow 对 BackgroundTask 类型的借用、删除 alias 和跨域 decoder；保留或重命名各领域真正需要的 canonical type。禁止把字符串名称作为删除目标，也禁止靠 re-export 暂时维持同名 API。

### 5.4 `resubmit` 的删除目标必须限定为 Workflow continuation 语义

第 266 行笼统要求删除 `resubmit` ownership。当前 `BackgroundTaskPool.resubmit()` 同时被 Workflow resume adapter、Product Agent adapter 和测试消费。已确认应删除的是“把 Workflow continuation/checkpoint 放回原 BackgroundTask TaskId 恢复”的语义。

实施规格必须决定 process-local BackgroundTask 是否允许同一 task identity 的 retry/restart：

- 若不允许，失败后只能以新 TaskId submit，并删除整个 resubmit API；
- 若允许，必须定义 attempt identity、状态转换、输出覆盖/追加和通知幂等，且仍不得携带 Workflow continuation。

在该语义确认前，不能用方法名批量删除，也不能让旧 Workflow resume 测试反向定义产品 contract。

### 5.5 `NEEDS_EVIDENCE` 机制缺少退出规则

第 120 行允许证据不足时标记 `NEEDS_EVIDENCE`，但没有规定谁关闭、需要什么材料、关闭后更新哪个 authoritative document。这样会把候选设计长期混入实施队列。

应规定：`NEEDS_EVIDENCE` 条目不可分配编码；证据包必须包含 production consumers、owner/lifecycle matrix、现有基础设施检索和候选方案排除；裁决后原子更新核心总账与本文件的 disposition，随后删除该标记。若证据证明没有生产消费者，则进入删除切片，不为其设计 Port。

### 5.6 执行批次缺少文件冲突之外的状态冲突判定

第 385 行只以“修改文件不重叠”作为并行条件之一，但两个切片即使编辑不同文件，也可能同时改变同一 identity、wire schema、composition generation、store layout 或 lifecycle owner。

并行准入必须同时证明：

- 不修改同一 authoritative contract/identity/state transition；
- 不迁移同一 consumer 或 composition binding；
- 不依赖对方删除的入口；
- 不分别创建同义 Port/codec/store/factory；
- 合并顺序不会产生暂时双 owner或不可运行中间态。

文件重叠只能作为工程冲突信号，不能作为架构独立性的证明。

## 6. 第二轮后的审批结论

第一轮阻断项仍全部有效，并新增三项复审硬条件：

1. 为全部 38 个 SB 提供唯一 disposition 和 ledger/slice 映射；
2. 将 SB0.4 作为 Hook command 与 Workflow effect execution 的前置边界；
3. 用 authoritative module、语义和 consumer 清单替代对 `BgTaskResult`、`BgStatus`、`resubmit` 的按名称删除。

因此当前文档仍只能用于需求修订，不能直接下发生产实施。

## 7. 第三轮：BackgroundTask lifecycle 闭环

### 7.1 `work-pin snapshot` 不能保证 submit 与 eviction 原子互斥

第 269 行提出 unloadability 读取窄 work-pin snapshot。当前调用链是 Residency 先调用 `AgentRuntime.is_unloadable()`，随后依次 materialize、`Role.prepare_for_eviction()`、runtime shutdown 和 remove；而 `BackgroundTaskPool.submit()` 可直接增加 task map并创建 asyncio task。单次 snapshot 会产生 TOCTOU：检查为零后、真正关闭前仍可能接受新任务。

实施要求必须是 Agent incarnation-owned lifecycle gate，而不是只加查询：

```text
ACTIVE --begin_eviction/release--> DRAINING --settled--> CLOSED
```

- submit admission 与 `ACTIVE -> DRAINING` 在同一同步原语/代际状态下原子互斥；
- task 在返回 local accepted 前先取得 work pin；
- pin 只在 operation、output、terminal result/notification 和 permit 全部 settlement 后释放；
- DRAINING/CLOSED 拒绝新 submit，并返回 typed disposition；
- eviction 在持有 drain ownership 时复核 settlement，失败则原子恢复 ACTIVE 或保持明确 DRAINING，不能留下半关闭 Role；
- stale incarnation 不能向新 incarnation 的 pool提交、取消或回收结果。

只把 `has_pending()` 接进 `is_unloadable()` 不足以通过该切片。

### 7.2 worker crash 后的 BackgroundTask 可观察语义没有定义

文档确认 BackgroundTask process-local 且不跨进程恢复，这是正确的；但没有定义 worker/incarnation 崩溃时已返回 submit receipt 的任务如何终结。由于 supervisor 被禁止持有共享 task registry，它不能在进程外逐 task恢复或伪造结果；新 incarnation 也不能重建旧 pool。

实施规格必须固定：

- submit receipt 只承诺当前 incarnation 内接受，不得使用 durable `ACCEPTED` 语义；
- receipt/reference 必须绑定 process instance、Agent identity/incarnation 与 local TaskId；
- worker loss 使旧 incarnation 的所有未结算 task reference 失效，查询/取消返回 typed `incarnation_lost`/`owner_gone`，不能误查新 pool 中复用的同名 TaskId；
- 是否需要向 Agent/User durable 记录“后台工作因 incarnation loss 未得到结果”，必须由 Agent lifecycle/delivery ledger拥有，不能因此建立 supervisor task registry；
- 不允许自动重放 operation，因为它可能包含 LLM、费用、时变调用或副作用。

这里必须与“terminal result 不能 best effort”区分：已经产生的 terminal result 不能只靠易失通知；尚未产生且 owner崩溃的任务只能得到明确 unknown/lost settlement，不能伪造成功或重新执行。

### 7.3 result pointer 与通知仍缺少完整 ownership key

产品决定要求跨 Agent 引用携带稳定 Agent identity；仅 `AgentId + TaskId` 仍不足以防同一 Agent rehydrate 后 local sequence 从 `bg_1` 重新开始。文档第 65–72 行和 BackgroundTask 测试矩阵没有把 process instance/incarnation/generation 纳入引用和资源 ownership。

跨组件查询、取消、结果 pointer、progress、notification、resource retirement 至少应绑定：

```text
process_instance + agent_identity + incarnation/generation + local_task_id
```

pool 内部调用可使用 local TaskId；一旦离开 pool boundary 就必须使用不可混淆的 typed reference。decoder 对 owner/generation mismatch fail closed。该 reference 不代表 durable task identity，也不能用于进程重启恢复。

### 7.4 release settlement 没有失败和超时语义

第 270 行只说 release 等待 cleanup。实际 operation 可能忽略取消、外部进程可能不退出、output flush 可能失败、notification/resource retirement callback 也可能报错。如果没有明确 disposition，release 要么永久挂起，要么吞错后宣称 Agent 已释放。

需要定义 typed release result，至少区分：全部 settled、取消已请求但仍在 draining、cleanup failed、owner/incarnation lost。对正常 Agent release：

- 先关闭 submit admission；
- 请求取消所有本 pool operation；
- 等待有界 cleanup，并保持 residency pin直到 settlement；
- 超时/失败不得删除 owner identity或让同一 incarnation重新接受工作；
- 是否升级为强制终止 worker 是 supervisor policy，不能由 pool自行杀进程；
- process shutdown 可采用更强终止策略，但必须报告未完成 settlement，不能伪装成功。

DoD 应包含 non-cooperative coroutine、外部子进程、output flush failure、notification callback failure 和重复 release fault injection。

### 7.5 subtree cancellation 只有命令方向，没有 delivery/settlement 保证

文档要求 supervisor 向各 pool 发 typed cascade command，但没有规定目标 Agent正在运行、DRAINING、已丢失 incarnation或命令重复时的行为。需要让 Agent governance owner 返回逐 Agent typed receipt，并按 stable subtree snapshot/fence执行：

- 命令不能让 supervisor 直接读取或修改 pool map；
- 同一 cancel command 重复投递幂等；
- 新 spawn 必须与 subtree cancellation epoch/admission 原子协调，不能在 snapshot 后漏出 child；
- resident Agent由本地 pool settlement，lost incarnation返回明确 owner-lost；
- 聚合结果区分 settled、already terminal、owner lost、timeout，而不是一个 bool。

这属于 Agent tree governance 与 Agent-owned pool 的协作 contract，不能由 BackgroundTask 包单方面实现。

## 8. 第三轮后的新增复审条件

BackgroundTask 切片还必须补齐：

1. submit/work-pin 与 eviction/release 的原子 generation 状态机；
2. process/Agent/incarnation/local-task 完整 typed reference；
3. worker crash 后 owner-lost/unknown settlement，且禁止自动重放；
4. release 的有界 cleanup、失败 disposition 和 pin 保持规则；
5. subtree cancellation 的 fenced snapshot、幂等 delivery 与逐 Agent settlement。

每 Agent 一个 pool 的产品方向仍然正确；当前缺陷在 lifecycle contract 尚未闭合，而不是需要改回统一 process service。

## 9. 第四轮：Workflow durable execution 闭环

### 9.1 必须明确替换当前进程内 `WorkflowRun` owner，而不是在其外围加 store

当前 `orchestration/workflows/definition.py::WorkflowRun` 以随机 UUID、`_executing: bool`、当前 asyncio task 和内存 `_state` 作为 ownership；`WorkflowContinuation` 直接持有 definition object、任意 checkpoint 和 run_state；`product/workflows/inspection.py` 又持有 `WorkflowRun` 并直接读写其私有 state。这些正是必须退出的第二状态链。

新文档只说新增 Orchestration run command/query/state machine，没有明确禁止 durable service 与旧 `WorkflowRun.execute()/continuation()` 并存。实施要求应固定：

- durable run 只能通过 `RunId + expected revision/fencing token` command 驱动；
- 查询只返回 immutable durable projection，不返回 `WorkflowRun`、graph、coroutine 或 mutable checkpoint reference；
- Product inspection/continuation registry全部迁到 RunId query/command 后删除；
- 旧进程内 executor若仍用于纯本地 definition 单元测试，不能作为第二个生产入口或拥有生产 run identity；
- 每次 vertical slice 必须列出被删除的旧 state field、registry 和 Product tool consumer。

### 9.2 definition identity 没有覆盖完整执行语义

当前 `freeze_builder()` 的 digest只包含名称、版本、节点名和部分 edge mapping/prompt，没有绑定 node implementation/tool definition、input/output schema、retry/timeout、permission/effect policy、codec version等影响恢复与副作用的语义。仅保存 `definition_id` 字符串不能证明重启后加载的是同一定义。

实施规格应要求 versioned immutable definition envelope，至少绑定：graph topology、node kind与稳定 implementation identity、输入输出 schema、routing/condition semantics、retry/timeout/cancellation policy、effect classification、所需 tool/capability definition identity，以及 definition codec version。任何未编码 closure/callable必须由 Product registry用稳定 identity解析；unknown identity/version或 content digest mismatch fail closed，不能使用 import顺序、对象地址或 `inspect.getsource`。

### 9.3 fencing 必须覆盖所有 mutation，不只是 execute owner

“同一 run 只有一个 fenced execution owner”仍不足。旧 owner失去 lease 后必须在每一个 mutation point被拒绝，包括 checkpoint/frontier CAS、effect intent、effect receipt、terminal outcome、delivery ack、cancel settlement、lease refresh/release和GC/delete。

每条 durable record需绑定 run generation/fencing token与revision；CAS比较 identity、revision和fence。lease expiry后即便旧 coroutine继续运行，也不能提交结果。测试必须覆盖 lease在 node执行中、intent后、effect后、receipt前、terminal commit前丢失，以及旧 owner试图 refresh/release新 owner lease。

### 9.4 terminal outcome 与 delivery 没有 durable 闭环

新文档关注 checkpoint/effect，却没有规定 Workflow terminal result如何从 authoritative run state交付给 Agent/User。当前 progress sink和BackgroundTask notification均为进程内路径，不能承担 durable terminal truth。

需要区分：

- terminal outcome commit：run owner以 fence/CAS写入唯一 durable run state；
- delivery intent/ack：可由 durable mailbox/event delivery机制承接，重复投递幂等；
- progress/telemetry：best effort observation，不驱动状态；
- Product query：按 RunId读取 immutable terminal projection/result reference。

terminal commit成功但通知前崩溃时，scan/reconcile必须重新发现未ack delivery；通知成功但ack前崩溃时允许重复投递但不得重复effect或改变outcome。

### 9.5 cancellation、timeout 与 pause 仍缺少 authoritative transition

当前实现把 asyncio cancellation/TimeoutError转换成内存 `Cancelled/TimedOut`，并可生成持有内存 checkpoint的 continuation。durable service中，cancel request、deadline expiry和pause必须是可重放的命令/事实，而不是仅由 coroutine异常决定。

实施规格至少要固定：

- cancel command identity、请求revision、幂等和与terminal commit的竞态；
- deadline使用持久化绝对时间/clock contract，重启后继续生效；
- cancelling状态下是否等待正在执行的外部effect settlement；
- pause reason、pending frontier和所需外部输入的versioned durable schema；
- resume command只能消费匹配definition/run revision的pause token，重复resume幂等或明确拒绝；
- cancel/timeout后迟到的node/effect receipt如何在fence下settle而不复活run。

### 9.6 Temporal 与 JSONL 不能只作为两个可互换 backend 名称

“显式选择 Temporal失败时不回退 JSONL”是正确产品决定，但实施文档还需分别声明二者必须提供的相同 contract和允许不同的部署保证。若 JSONL被允许承载 WorkflowRun，它也必须满足同一run单owner、CAS/fencing、严格codec、crash recovery、effect reconciliation和durable scan；否则它不能被标为同等级 durable Workflow backend。

typed activation result至少返回backend identity、process/host scope、durability、fencing、transaction/commit和recovery guarantee。Product只能在请求的最低保证被满足时激活。不得因显式选择 JSONL 就自动接受缺少跨进程ownership或fsync语义的较弱实现。

### 9.7 durable scan/reconcile 需要明确 owner 与公平/背压策略

文档要求 scan/reconcile恢复，但没有说明谁扫描、如何claim、如何避免多个进程重复推进、如何处理大规模 pending frontier。应由 Orchestration拥有reconciliation policy和run admission，Runtime提供store/lease机制；扫描结果不是直接执行权，必须先取得fenced claim。

至少定义分页游标、排序/公平性、retry schedule、poison/dead-letter disposition、并发上限、backpressure和best-effort wake丢失后的周期scan。不得让每个Product tool或Session resume各自扫描并启动run。

## 10. 第四轮后的新增复审条件

Workflow实施部分还必须补齐：

1. 删除生产 `WorkflowRun`/continuation/inspection内存owner的明确迁移清单；
2. 完整、版本化且绑定执行语义的definition identity；
3. fencing覆盖所有commit/ack/release/delete mutation；
4. terminal outcome与durable delivery intent/ack/reconcile；
5. cancel、deadline、pause、resume的durable状态转换和竞态；
6. Temporal/JSONL逐项guarantee profile与最低激活要求；
7. 单一Orchestration reconciliation owner及公平、背压、claim语义。

在这些 contract 固定前，不能先实现 store adapter或把现有 `WorkflowRun` 对象序列化入盘。

## 11. 第五轮：Agent governance、lineage 与 delivery 闭环

### 11.1 spawn 不能继续依赖单进程 rollback transaction 充当 durable 原子性

当前 `AgentControl.spawn_agent()` 通过内存 `SpawnTransaction` 依次取得 residency slot、registry reservation、nickname/path、构造 Role、注册 runtime/scheduler，最后 commit；rollback callback只能处理同进程异常，无法覆盖 supervisor在任一步骤崩溃。新文档虽写“spawn admission 与 lineage commit、placement、失败回滚可对账”，但没有定义 durable protocol。

实施规格应固定 versioned spawn intent和稳定 request identity，并至少经过：

```text
requested -> admitted -> lineage_committed -> placement_pending
          -> incarnation_started -> active | rejected/aborted
```

- logical identity、parent/path/nickname和预算reservation在任何worker启动前 durable commit；
- 重复SpawnRequestId返回同一结果或typed conflict，不得创建第二child；
- supervisor崩溃后reconciler从durable state决定继续placement或终止，不依赖rollback closure；
- Role构造/worker启动失败不能删除已经公开的logical identity，而应推进lifecycle/attempt；
- 未公开的reservation可由有fence的reconciler回收，且防止旧owner释放新generation reservation；
- admission、lineage与placement不必强行塞进一个数据库事务，但中间状态必须可对账且无不可见外部副作用。

### 11.2 三类 cap 仍缺少 identity、作用域和释放事件

文档正确区分 logical identity、resident incarnation、concurrent turn，但没有为每类cap定义scope和何时释放。至少需要固定：

- logical cap按root/tree还是application计数；terminal logical Agent是否保留identity并占cap，何时tombstone/GC；
- resident cap绑定incarnation/generation，eviction只释放resident slot，不释放logical slot；
- turn cap绑定一次admission receipt，从queued/admitted/running到terminal/cancel严格一次release；
- fan-out、subtree、root total分别使用哪个durable counter/projection，如何避免scan后并发spawn超限；
- permit丢失、重复release和owner crash后的reclaim/fencing语义。

当前实现仍把 `max_agents` 默认解释为residency ceiling且 registry没有logical cap。实施切片必须删除这个含义模糊的旧配置/路径，不能保留alias把一个值同时喂给多个cap。

### 11.3 lineage identity 必须防 path/nickname/agent-id ABA

当前 Registry是session-scoped内存dict，path与nickname可以释放后复用；ResidencyStore又主要按session_id命名记录。durable lineage若只保存字符串值，会让旧delivery、lease或result误命中新Agent。

logical Agent identity应不可复用；path/nickname是可变或可回收索引时必须绑定lineage revision/tombstone。所有placement、mailbox、budget、delivery和residency record至少验证logical Agent ID、root ID、definition identity、incarnation/generation和revision。删除/GC只能在无pin、无pending delivery/effect、retention到期且持有当前fence时执行。路径重用不能让旧parent/subtree snapshot包含新Agent。

### 11.4 logical release、incarnation eviction 与 worker crash 是三个不同 transition

当前 `release_child()` 同时从runtime、residency、registry、comm graph和incarnation factory删除；这会把“停止当前incarnation”“从内存evict”“删除logical Agent”混成一个动作。实施文档必须拆清语义，但不一定拆成多个巨型service：

- eviction：logical Agent保持active/known，仅当前resident incarnation卸载；
- worker crash：incarnation转lost，logical Agent保留并可placement新generation；
- logical release/terminate：拒绝新turn/delivery/spawn，结算child/background/effect后进入terminal/tombstone；
- purge/GC：retention后删除可重建材料和索引，不得由普通release直接执行。

每个command返回typed receipt；旧incarnation的late completion不能把terminal logical Agent复活。

### 11.5 delivery 的 accepted、processed 与 acknowledged 未分层

当前 `send_input()`/broadcast把进程内 enqueue或park称为never-drop/accepted，`PendingDeliveryQueue`却是易失内存。新文档要求durable commit后才能返回accepted，但尚未定义delivery状态机与调用面。

至少需要：

```text
intent_committed -> available -> claimed(fenced) -> delivered
                 -> processed/terminal -> acked | dead_letter
```

- `accepted`只表示durable intent commit，不表示target已处理；
- delivery identity绑定source、target logical Agent、target lifecycle generation、payload schema和dedupe key；
- mailbox enqueue是claim后的本地投影，不是canonical truth；
- worker crash/eviction后未ack item可重新claim，stale incarnation ack被拒绝；
- target terminal、unknown definition、poison payload和超过retry budget进入typed dead-letter；
- broadcast/subtree必须为每个目标建立可对账delivery identity，不能仅返回agent id list冒充统一成功。

### 11.6 delivery payload 本身必须严格持久化，不能只持有进程对象

Message、InterAgentCommunication、用户输入、审批结果和control command的durable envelope必须是versioned tagged union。未知tag/version、额外/缺失字段和错误primitive fail closed；不能把Python Message对象、callback、Role reference或裸dict直接写入queue/store。大payload通过canonical artifact/reference机制，并把artifact ownership与delivery retention绑定。

敏感payload还需要permission/trust/audit边界；dead-letter和日志不得泄漏secret。delivery codec应复用现有strict envelope基础设施，但各domain保留authoritative payload union，不能建立万能object codec。

### 11.7 budget 与 policy extension 需要原子reservation/settlement

新文档列出Token、成本、深度和能力预算，但Agent切片没有说明预算何时reserve、实际消耗如何settle、失败如何refund。spawn/turn/tool/LLM admission应使用不可变policy input与typed receipt；extension只能收窄parent授权。并发child不能基于各自读取的旧subtree total同时超额；budget reservation必须与admission原子，settlement幂等且受fence保护。

未知成本不能用零值继续；owner crash后未结算reservation需按明确lease/reconcile规则处理。Observation telemetry不能成为budget truth。

### 11.8 调度公平与背压不能由 parked queue 的“永不失败”承诺代替

容量耗尽时应返回typed queued/backpressured/rejected disposition，并由durable scheduler按root/tenant/subtree公平推进。无限park会把容量问题转成内存增长和无界延迟。实施规格需定义queue bound、优先级、fairness、deadline、取消、admission retry和overload策略；best-effort wake全部丢失后仍由durable scan发现工作。

测试应覆盖一个root大量fan-out不能饿死其他root、target长期不resident、queue满、deadline到期、取消与claim竞态，以及supervisor restart后顺序/去重不变量。

## 12. 第五轮后的新增复审条件

Agent governance部分还必须补齐：

1. durable spawn intent与可恢复状态机，替代内存rollback作为原子性；
2. 三类cap各自scope、identity、释放/GC事件与配置退出方案；
3. logical/path/nickname/incarnation的ABA防护；
4. eviction、worker loss、logical terminate和purge的独立transition；
5. durable delivery intent/claim/process/ack/dead-letter状态机；
6. strict payload envelope、artifact ownership和敏感数据边界；
7. budget原子reservation/settlement及单调收窄policy；
8. 有界、公平、可恢复的backpressure与scheduler guarantee。

这些要求应拆成共享identity贯穿的垂直切片，不能再合并成一个“Agent governance”大改动。

## 13. 第六轮：Composition、scope、activation 与泛型闭环

### 13.1 缺少完整 scope/lifecycle matrix

新文档要求唯一composition，却没有逐对象声明application/process/session/Agent/incarnation/turn scope。当前 `EngineServices`、ProductContainer、model composition/reloader、ServiceGateway、AgentControl、Context、Role wiring、BackgroundTaskPool、tool/session resources和generation lease容易在一个bundle中混用。

实施前必须形成authoritative matrix，至少记录：type/capability、scope、construct owner、activation owner、shutdown owner、是否可跨session共享、是否可跨incarnation继承、是否durable、required Ports。没有matrix不能凭“共享服务”或“每session”命名决定singleton。

每个对象只能由一个上级scope拥有；低scope不能关闭共享高scope资源，高scope也不能把mutable session/Agent state缓存成singleton。

### 13.2 construct 与 activate 仍然混合

当前 CLI `build_engine()`在构造过程中执行home播种、配置加载、locale mutation、cwd读取，并通过 `asyncio.run()`安装model composition。新文档未给出把这些side effect迁出constructor的切片。

Product composition应分为typed declaration/validated config、pure object graph construction、ordered async activation和reverse settlement。文件创建、secret resolution、provider/network client、watcher、daemon、process和journal open只能在明确activation phase发生。activation中途失败按已完成阶段逆序关闭，并返回typed result；不得留下半激活Application。

同步CLI可以在最外层拥有一次event-loop boundary，但canonical Product factory本身不能内嵌 `asyncio.run()`，否则gateway/ASGI/Notebook已有loop时无法复用同一入口。

### 13.3 `EngineServices`/`AgentWiring` 仍可能成为换名后的 locator

当前 `AgentDependencies`/`AgentWiring`包含toolsets、Skill、CodeMap、Hook Any config、MCP Any、paths、secret predicate、LSP factory、BackgroundTask builder和routing builders；`EngineServices`再承载Context和application composition。仅将字段类型化仍可能把完整对象图暴露给Role组件。

实施规格必须按真实consumer给出 capability matrix：每个组件只接收其窄immutable input或Port，不能通过wiring取得无关path/config/client/factory。Wiring可以作为Product内部原子装配值，但不能成为Runtime组件跨bounded context查服务的公共API；不得增加 `get_service()`、string key、任意mapping或反射fallback。

### 13.4 lifecycle lease 必须表达资源所有权，而不只是“是否 owned”布尔语义

当前 `AgentWiring.with_services(..., owned=False)` 和 `for_incarnation()`依赖隐含语义决定是否acquire新的 `EngineServicesLease`。实施文档只说先替换Any，再按lifecycle决定是否拆lease，仍不足以防double-close、leak或eviction错误转移。

lease contract需明确resource identity、scope、generation、holder identity、acquire/transfer/release状态和幂等/错误语义。incarnation replacement如果转移共享service ownership，应使用typed transfer receipt；旧incarnation失去generation后不能release新holder资源。非owner只能持borrowed capability且API上没有close权。

### 13.5 hot reload/generation swap 没有进入composition切片

当前ApplicationReloadCoordinator与model composition存在generation lifecycle。新文档没有说明reload如何与active session/Agent、tool definition、Workflow definition、provider client和lease关联。

允许的reload只能在既有trust/approval范围内构造完整candidate，验证content identity和capability不扩大后原子swap generation。旧generation由现有lease holder继续使用直至drain；新session/turn取得新generation。不得原地mutate共享registry/config，也不得让同一turn混用两代tool/model/policy。来源、权限或内容identity改变必须重新trust decision，失败保持旧generation且无部分更新。

### 13.6 泛型修复不能把“所有child都是str”写进canonical contract

当前 canonical `RunnableAgent[OutputT]`被 `provision_spawned_child(RunnableAgent[object])`截断，`is_text_runnable_agent()`又用runtime Protocol形状伪称验证了 `OutputT=str`。Product `_ChildBuilder`和`_ChildAgentClass`固定为str，这是当前Coding Agent specialization，但不能反向定义跨层spawn contract。

修订应让definition/builder/request/runtime/handle/outcome完整携带同一OutputT；Product可以提供显式 `TextChildAgentDefinition` 或静态类型已知的text builder。运行时无法从Protocol方法shape验证泛型实参，因此应删除该TypeGuard，不用另一个cast替代。若外部dynamic manifest声明output schema，必须在adapter解码并绑定authoritative OutputContract，而不是检查Python class nominal shape。

### 13.7 SpawnContext 拆分必须按consumer capability，不能只缩字段

当前SpawnContext携带config、cost tracker、cwd、path等Any，并由RunnableAgent暴露provision/control/state/cost方法。实施文档说只保留stable identity/request，但没有说明context share、cost rollup和control binding由谁接管。

应分别建立：Product-owned construction request、Orchestration-ownedspawn identity/admission receipt、Runtime context provisioning所需窄Port、budget settlement Port和control capability。它们不应重新汇总为 `SpawnServices`。parent向child传递的policy只能收窄，且每项capability有stable identity；child builder不能取得完整parent Role/Context/CostTracker。

### 13.8 config path与secret/trust信息不能作为普通Agent依赖传播

当前AgentDependencies包含primary config path、secret predicate、user/session/browser/oauth roots和raw Hook/MCP config。把路径放进immutable dataclass不等于可信。Product必须先绑定canonical source/path ownership/content digest与approval，解码为最小typed activation spec；Runtime/Agent只接收已批准capability和必要路径handle，不能自行重新读取配置或扩大root。

checkout内Agent/Skill/Hook/MCP发现不能自动触发model injection、process/network或tool activation。composition切片必须包括provenance/trust/approval和hot-reload重新决策测试。

### 13.9 测试不能各自重建“精简版”production composition

唯一composition root约束也适用于测试：integration/entrypoint测试应调用同一Product factory并注入fake Ports/backend；不能复制Role/AgentControl/Runtime构造链。单元测试可以直接构造bounded-context内部对象，但不得让测试便利迫使production API公开internal store/registry或增加wide optional参数。

architecture gate应扫描已确认的production constructor调用点与测试bootstrap allowlist，验证不存在第二个生产factory；不能简单禁止Product composition导入concrete下层实现。

## 14. 第六轮后的新增复审条件

Composition与泛型部分还必须补齐：

1. application/process/session/Agent/incarnation/turn完整scope matrix；
2. pure construct、async activate、逆序settlement及中途失败协议；
3. `build_engine`中的CLI side effect和 `asyncio.run()`退出canonical factory；
4. Wiring/Services按组件consumer收窄且不形成locator；
5. resource lease identity/generation/transfer/release contract；
6. atomic hot-reload generation与trust重新决策；
7. OutputT端到端泛型及text-only Product specialization；
8. SpawnContext按construction、identity、context、budget、control capability拆边界；
9. config/secret/path provenance不直接传播给Agent；
10. 测试复用production composition并通过fake Port隔离。

在scope matrix与activation顺序固定前，不能先“统一factory”，否则容易把现有CLI副作用和巨型service bundle固化成新的canonical root。

## 15. 第七轮：包内聚、配置、错误与公共服务面

### 15.1 G4 仍是按目录聚合的横向批次，不是 bounded-context 切片

第 276–294 行把 errors、presentation、config、watching、wording、elision、private imports、package roots、CodeMap、ToolExecutor、registries和ServiceGateway放在同一阶段。它们没有共享identity、state machine或lifecycle，不能作为一个“公共面治理”任务下发。

每项必须回到拥有不变量的owner并单独列consumer migration、旧入口删除和gate。允许同批并行不等于允许共享一个facade/manifest；尤其不能建立全局 `PublicServices`、`ConfigService`、`ErrorService` 或公共面registry统一处理。

### 15.2 `runtime/errors` 迁移必须区分 error definition、classification 与 presentation

当前 `runtime/errors/__init__.py`同时重导Contracts domain error、Runtime adapter error、retry/classification、recovery runner和rendering。执行者若只把import改到Contracts，仍可能把Runtime classification或Product wording放错owner。

应逐symbol分类：

- stable cross-boundary code/context/DTO归对应Contracts domain；
- provider/transport exception normalization归具体Runtime adapter；
- retryability来自authoritative typed error/disposition，不能靠字符串或全局tuple；
- recovery orchestration归拥有重试/补偿状态机的bounded context；
- human-facing render归Product presenter，不由error contract携带英文措辞。

迁移所有消费者后删除 `runtime.errors` 聚合入口；不能保留re-export。Orchestration当前从Runtime errors导入Agent/Graph error也构成向错误owner依赖，应直接迁回authoritative Contracts/Orchestration domain。

### 15.3 namespaced ErrorCode 迁移缺少 wire/存量数据退出方案

全局ErrorCode拆分会影响durable journal、event envelope、API/wire、日志、artifact metadata和测试snapshot。实施文档没有要求先建立现有code的serialized consumer inventory，也没有定义namespace/version。

应先固定稳定 `namespace + code + schema_version + typed context` envelope，再由各domain拥有code enum。迁移必须在同一切片更新全部仓内encoder/decoder和fixtures；若存在必须读取的存量durable数据，应通过明确的一次性version migration完成并删除旧decoder，不能长期双读或alias。若无外部/存量ABI，直接切换并删除旧code。

### 15.4 Presentation 迁移不能把 Product policy 一并下沉 Contracts

当前 `product/presentation/events`混合cross-host DTO、CapabilityAdapter、Terminal/Textual/Structured默认能力和folding/wording policy。真正跨host且稳定的event/capability DTO可迁Contracts；但capability downgrade、fold mode、tool grouping、surface defaults和rendering属于Product。

迁移需明确内部closed typed union、Product projector、各surface adapter和ACP/AGUI wire codec四层。内部不使用开放字符串/getattr分派；wire层严格encode/decode。Contracts不得拥有Product默认能力、英文文案或host选择。旧Product DTO入口在所有consumer迁移后删除，不保留re-export。

### 15.5 config owner 迁移必须按“声明—来源—解析—secret—activation”分阶段

`runtime/config`技术grab bag和Contracts tool config中的Product默认值确实需要治理，但“把model移动到某目录”不能闭合配置边界。每个domain应区分：

- Contracts：确有跨层消费者的纯typed declaration；
- Product：source precedence、canonical path/provenance、默认backend/tool、trust/approval和secret resolution；
- Runtime：对应mechanism的validated activation spec；
- adapter：外部动态input在入口严格decode，secret stdout不记录。

当前 `contracts/config/tool/models.py`硬编码Read/Search/Bash/Edit/Sleep默认cap，并把result limit、compression、effect journal、loop guard和Temporal policy聚在一起。它们的owner/lifecycle不同，必须按真实consumer迁移；不能再创建一个 `ToolRuntimeConfig` 巨型包。disable journal等配置若会降低durability guarantee，必须由Product policy决定是否允许，不能把安全保证当普通布尔开关。

### 15.6 FileOps/Artifact 是命名和公共面债务，不是重复storage owner

当前 FileOps mutation `ArtifactRepository`内部复用 `runtime.artifacts.repository.ArtifactRepository` 作为content repository。实施文档没有显式纠正“两个Artifact repository就一定双owner”的误解。

应保留canonical artifact content storage owner；FileOps内部类型按其真实语义重命名为mutation artifact staging/catalog/scope能力，并隐藏lock、journal、catalog和mutable scope。外部consumer通过最小FileOperations command/immutable receipt或明确artifact Port访问。不能强行合并两个生命周期不同的概念，也不能再套第三个Repository facade。

### 15.7 CodeMap 与 LSP 必须分别收窄，不能共享“code intelligence service”

CodeMap围绕repository extraction/index/query高度内聚，应保留现有bounded context；LSP拥有server lifecycle、diagnostics和workspace ingestion。它们字段相似但state/lifecycle不同。

CodeMap切片应定义immutable query DTO与最小index command，store/extractor/language provider留包内；Product turn-context只消费query/source Port。LSP切片按启动/关闭、document ingestion、diagnostics query和turn-context consumer分别收窄。不得把二者合成万能CodeIntelligenceManager，也不得让Contracts Port返回object或build source的 `**kwargs`。

### 15.8 ServiceGateway public surface 必须由composition与业务consumer分别推导

Product composition导入Runtime planner/implementation本身合法；真正风险是Product media/search直接依赖Runtime internal failover snapshot/layout。实施规格需先判断snapshot是稳定immutable capability manifest还是Runtime record。

若是跨composition正式input，Contracts只拥有最小capability declaration，Runtime内部构造planner；若只在canonical Product root使用，可保留typed Runtime factory input但不向业务consumer公开。Local journal path/layout、merge function和planner state不得从包根作为稳定API。禁止为隐藏planner再造无状态ServiceGateway facade。

### 15.9 provider registry 应封装snapshot/revision，不应统一成共享Registry基础设施

当前Model registry有包外读取mutable map计算revision的消费者；Web/Media尚未证明包外mutation。整改应由各Product domain分别提供immutable catalog snapshot/revision和typed lookup，内部map私有化。不同provider类型、config、activation和health lifecycle不同，不能继承共享concrete registry或建立全局provider registry。

registry revision必须绑定影响composition语义的definition/content identity；hot reload使用candidate generation原子swap，不依赖dict迭代/import顺序。

### 15.10 `runtime/text/elision.py` owner 仍未裁决，不能直接进入实施

文档只说在context/resources/tool-compression中按共同不变量确定owner，这仍是 `NEEDS_EVIDENCE`。应列出全部生产consumer，判断它拥有的是模型上下文预算、tool output settlement还是跨Runtime通用的typed elision value/strategy。只有一个owner负责marker和保证，其他bounded context通过稳定value/service面消费。

在裁决前不得移动文件或新建 `runtime/text`替代包；旧 `common` 注释可以独立删除，但不能把注释清理伪装成owner迁移完成。

### 15.11 package-root 收窄必须以 import purity 和 consumer contract 验收

包根导出数量本身不是问题。应删除eager optional backend、错误owner re-export和internal mutable implementation exposure；稳定且轻量的domain service/DTO可以保留。`__init__.py`不能通过dynamic import或module `__getattr__`规避。

每个包根切片需提供before/after consumer list、optional import test、public symbol allowlist和authoritative module。allowlist只保护已确认API，不得按类名后缀判定。Product toolset尤其要保证基础import不加载Terminal/pyte、browser/device/media backend，且manifest discovery不自动激活capability。

## 16. 第七轮后的新增复审条件

G4部分还必须补齐：

1. 每个bounded context独立切片，撤销“公共面大扫除”式任务；
2. errors按definition/classification/recovery/presentation分owner，并给出ErrorCode ABI迁移；
3. presentation只下沉cross-host DTO，Product policy/default/render保留上层；
4. config按declaration/source/trust/secret/activation分阶段；
5. FileOps复用canonical Artifact storage，只治理命名和服务面；
6. CodeMap/LSP分别定义typed consumer boundary；
7. ServiceGateway区分合法composition input与业务representation泄漏；
8. 各provider domain独立registry snapshot/revision，不建共享Registry；
9. elision owner完成consumer/lifecycle证据后才实施；
10. package-root以import purity和真实consumer验收，不按导出数量整改。

这些条目必须分别映射核心总账；不能用一个G4完成状态代表全部子系统已经闭合。

## 17. 第八轮：测试、架构门禁与实施交接

### 17.1 owner/public-surface manifest 可能成为第二份架构真相

新文档多次要求建立manifest，但仓内已经存在Contracts/Product composition governance declaration、生成式artifact、静态allowlist和多个JSON/TOML baseline。若再为本治理新增一套owner/public-surface manifest，会同时维护源码、核心总账、Product declaration和测试artifact四份关系。

必须先给现有governance机制做capability matrix并决定唯一authoritative declaration。推荐原则：生产typed declaration仅在确有runtime/composition consumer时存在；纯架构约束直接由测试读取authoritative module/public API；生成artifact必须可由单一source确定性重建并在diff中校验，不得手工维护语义。核心总账跟踪债务状态，不复制symbol allowlist。

任何手工allowlist新增项必须指向已确认contract和consumer；不能为让gate通过而批准现状。删除owner/API时同切片删除declaration、artifact和gate条目。

### 17.2 现有gate含启发式与硬编码，不能直接扩展成最终门禁

`static_governance.py`包含固定路径集合、approved encoder集合和对特定symbol/Any的扫描；其他架构测试也包含固定source字符串与manifest baseline。这些适合保护已确认局部关系，但不能证明完整owner、lifecycle或运行保证。

新门禁应按风险选择：AST/import graph验证layer/local import/private dependency；类型检查验证Protocol/generic；运行测试验证construct/activation/cleanup；双进程/fault injection验证durability/fencing。不得用字符串不存在来证明状态机删除，不得用类名/目录名推断owner，也不得把所有 `Any` 一刀切到外部adapter内部。

### 17.3 G0 通过不能成为后续所有切片唯一baseline

恢复architecture collection是首要切片，但当前31个collection errors中首个optional `pyte`和后续Application cycle可能是多个独立根因。G0必须逐一归因并记录：修复哪个import chain、哪些错误随首因消失、哪些仍独立存在。不能只以最终“0 errors”掩盖过程中安装依赖、skip测试或减少collection范围。

完成判据应包括同一命令实际collected/executed数量、无新增skip/xfail、基础package import矩阵和optional dependency缺失矩阵。静态四项通过只是补充证据。

### 17.4 每切片测试模板过宽，也缺少直接消费者与负向兼容测试

第 327–345 行要求子系统、architecture和Pyright，但没有强制运行直接生产消费者，也没有要求证明旧入口已经消失。每切片测试清单应从consumer matrix生成，至少包括：owner单元/故障测试、直接consumer、entrypoint/composition、相关architecture gate、Pyright和旧API负向import/constructor检查。

删除切片必须证明旧module/symbol/alias不可导入且仓内无consumer；不能继续运行只验证旧行为的测试。Product contract确实改变时同步迁移测试并删除旧断言，不保留双expectation。

### 17.5 fault injection 必须绑定commit protocol的精确阶段

通用“write/flush/fsync/replace”列表不够。每个durable owner需给出状态转换表和注入点编号，例如intent commit前/后、effect前/后、receipt commit前/后、lease loss前/后。测试fake必须能确定性暂停并协调两个owner，不能靠sleep和概率race。

每个case验证durable bytes/state、公开receipt、是否发生外部effect、reconciler决定和stale owner拒绝。文件协议同时验证parent fsync；数据库/Temporal按其transaction/ack语义使用对应fault，不机械模拟文件步骤。

### 17.6 migration 测试与“禁止compat”需要明确区分

零兼容债务不表示可以忽略现有durable数据。若确认必须迁移，migration是有版本窗口、可审计、可重复执行的一次性工具，不是生产双读fallback。测试应覆盖old fixture→new canonical state、重复migration幂等、partial failure恢复、unknown version拒绝和migration完成后旧decoder从生产主路径删除。

若数据无需保留，应明确destructive boundary和用户授权，而不是静默清空。文档必须逐domain说明保留/迁移/丢弃决定，不能统一套用。

### 17.7 Pyright 验收需要固定检查边界与无新增抑制

`pyright <changed paths>`可漏掉反向consumer和跨文件generic关系。泛型/Protocol/public API切片至少检查authoritative package及全部直接typed consumers，必要时运行仓库配置的完整Pyright。报告需记录版本、命令和error count。

DoD应禁止新增无精确错误码的ignore、文件级ignore和为通过类型检查引入的Any/cast。外部dynamic adapter的Any必须在同函数/模块入口验证并转canonical type；类型通过不能替代runtime decoder测试。

### 17.8 性能/规模场景必须来自明确容量contract

“1024 logical Agents”可以作为压力样例，但不能成为隐含产品上限或成功标准。测试需使用配置的logical/resident/turn cap，验证logical数量可远大于resident worker数量、公平和内存增长有界。规模测试应区分功能确定性测试与性能benchmark，避免CI时间波动导致架构结论不稳定。

### 17.9 预存失败必须有baseline owner和退出条件

当前工作区和architecture suite已有预存失败。每个切片开始时记录确切命令、失败case和首因；结束时不得新增失败。若切片宣称修复某baseline，必须更新唯一baseline记录并删除临时豁免。不能在本实施文档维护第二份动态失败清单，也不能用“与之前一样”代替证据。

最终报告必须分别列通过、失败、skip/xfail、未运行范围和环境缺依赖；collection error计为未执行测试，不得报告为部分通过。

### 17.10 实施交接缺少机器可核验的 slice contract

当前章节和批次是人类叙述，执行者仍需自行判断边界。每个可下发slice应有固定schema：

```text
slice_id
ledger_owner
requires / unblocks
canonical_contract / owner / identity
production_entries / consumers
scope / lifecycle / durability / guarantees
reuse_decision
files_expected_to_change
deletes
normal/failure/recovery/cancel/cleanup acceptance
tests / gates / pyright scope
decision_status
```

schema可以存在于核心总账或其受控附件，但只有一个状态owner。`decision_status`不是进度状态：必须在编码前为CONFIRMED；NEEDS_EVIDENCE和DECISION_REQUIRED不可下发。完成状态仍只回填核心总账。

## 18. 第八轮后的新增复审条件

测试与交接部分还必须补齐：

1. 选择并复用唯一governance declaration，禁止新增平行manifest真相；
2. gate按import/type/runtime/durability风险分层，不以字符串启发式证明架构；
3. G0记录真实collection数量、独立根因和optional缺失矩阵；
4. 每切片运行owner、直接consumer、composition、architecture、Pyright及旧API负向测试；
5. fault injection绑定精确commit transition并确定性协调并发owner；
6. 每个durable domain明确保留、一次性migration或授权丢弃；
7. Pyright覆盖public API反向consumer且禁止新增抑制；
8. 规模测试从cap contract推导，不把1024写成隐含上限；
9. 预存失败使用唯一baseline并报告collection/execution/skip详情；
10. 所有可下发工作使用固定slice contract且CONFIRMED后才能编码。

没有上述交接结构，即使治理原则正确，也无法保证多个实施者得到同一owner、删除范围和验收结论。

## 19. 第九轮：工具、权限、Hook、进程与副作用

### 19.1 实施文档缺少统一的 effect identity 主链

Tool definition、permission target、approval request、sandbox decision、durable intent、执行receipt和审计事件必须指向同一稳定effect identity。当前文档分别讨论Workflow effect、Hook runner和ToolExecutor，却没有要求这条identity端到端保持；执行者可能在每层生成新ID，导致批准的是A、执行和对账的是B。

每次effect至少绑定调用者Agent/incarnation、turn/run、tool/operation definition generation、canonical arguments digest、permission target、effect classification和attempt identity。重试/恢复不能换掉logical effect identity；不同attempt有独立ordinal/receipt。任何definition/config generation变化都要求重新permission/trust decision。

### 19.2 固定argv与用户可控shell必须是不同typed runner

当前 `runtime/process.py::aexecute`以 `shell: bool`混合两类信任域，Hook直接执行shell string，其他LSP/media/device/daemon路径使用固定argv或长驻process。实施规格必须禁止用布尔参数表达信任边界：

- governed shell command接收明确CommandSpec，经classifier/permission/approval/sandbox；
- fixed internal argv接收结构化argv与approved executable identity，不接受shell expansion；
- interactive/daemon process拥有独立start/health/stop lifecycle；
- process result为typed receipt，区分spawn denied、spawn failed、exit、timeout、signal和output reference。

这些runner可复用底层spawn/sandbox mechanism，但不能暴露一个可切换trust mode的万能API。Hook和Workflow effect必须消费已有正确seam，不各自再建runner。

### 19.3 `api_key_helper` 当前 `shell=True` 是明确安全阻断

`product/config/secrets.py`使用 `subprocess.run(..., shell=True)`解析helper。治理原则已确认helper仅允许USER/MANAGED来源、结构化argv、禁止shell执行，且stdout是secret不能记录。

实施文档必须把它列为独立安全切片：Product验证source/provenance与helper declaration，解析为argv；Runtime fixed-argv runner执行并限制cwd/env/timeout/output；stdout只进入secret value adapter，stderr/error需redact。unknown source、空argv、非零退出、超时、超限输出和decode错误fail closed。不得为了兼容旧字符串配置保留shell fallback。

### 19.4 Hook 控制面必须先于Tool effect执行，且decision只能收窄

PreToolUse/control Hook如果能allow/deny/modify，必须在canonical arguments和permission target固定后、任何effect intent/外部动作前执行。Hook extension不能扩大基础权限、sandbox、budget、tool或network范围；修改arguments后必须重新分类/permission decision，不能沿用原approval。

Hook timeout、handler crash、malformed output和unknowndecision对control Hook fail closed；Post/observation Hook可best effort但不能修改authoritative result。多个Hook folding顺序必须确定、identity稳定，冲突采用最严格decision。Hook日志不得包含完整secret payload/command stdout。

### 19.5 permission/approval/sandbox 的拒绝不能被fallback绕过

所有调用面——普通Tool、Workflow node/effect、BackgroundTask shell operation、Hook command、MCP、internal facade——必须最终经过其信任域对应的canonical gate。deny/ask未完成时不得spawn或写intent为已执行；approval绑定effect/arguments/generation和有效范围，不能因重试、hot reload或resume复用到不同effect。

若sandbox backend缺失或activation失败，必须按Product policy返回typed unavailable/deny；不能静默unsandboxed执行。测试需验证每个入口在deny、ask rejected、approval timeout、sandbox failure时没有subprocess/file/network side effect。

### 19.6 文件修改必须区分Product配置写入与Agent工具FileOps

源码存在大量直接文件写入，其中持久化backend、secret store、config bootstrap和Product rendering artifact不一定属于Agent workspace mutation。不能以扫描 `write_text/write_bytes` 一刀切；但所有由Agent/tool请求修改workspace的路径必须经过canonical FileOps，包含read-before-write、path/symlink/TOCTOU校验、before-image snapshot、permission/effect identity和durable settlement。

每个直接写盘consumer需分类：owner-internal durable protocol、Product-managed config/secret、ephemeral cache/artifact、Agent-requested workspace mutation。最后一类不得旁路；前三类各自满足atomicity、权限和ownership，不应强行调用高层FileOps造成循环或错误owner。

### 19.7 远端effect成功后本地settlement失败必须进入in-doubt

外部API/process可能已成功，但receipt、artifact publication或local permission/audit commit失败。不能向调用者报告“未执行”并允许盲目重试。状态机必须在外部动作前durable commit intent，动作后记录provider/process receipt；本地commit失败保留in-doubt，由reconciler按provider idempotency/status API或人工确认处理。

unknown outcome不等于failed；ToolResult、Workflow outcome和用户presentation都需typed区分。远端返回成功但本地输出被size limit/persistence拒绝时，effect仍已发生，不能伪造成安全失败。

### 19.8 ToolExecutor 公共面收窄必须保留唯一执行chokepoint

当前ToolExecutor只有一条生产构造路径，不能因为依赖多就拆散。真正要隐藏的是catalog/live mutable map、内部pipeline stage和BackgroundTask Any callback。外部消费者应获得immutableToolBindingSnapshot、typed execute command/receipt和必要query，不得取得tool instance后绕过permission/effect pipeline直接调用。

runtime-discovered tool definition必须在执行前绑定catalog generation和permission identity；catalog reload后旧turn继续使用其snapshot，新turn取得新generation。MCP/optional tool activation失败不能修改基础catalog或扩大capability。

### 19.9 ToolResult与大输出必须保持canonical artifact/reference owner

普通Tool、BackgroundTask、Workflow和Hook不能各自发明截断、文件路径或result pointer。大输出经过统一artifact/content repository和typed reference；preview明确不是完整result，reference绑定owner、content digest、retention和permission。terminal result已经产生后，notification丢失可以从canonical state/reference重新投影。

secret、credential、helper stdout和敏感payload不得进入artifact preview、异常、trace或测试snapshot。媒体使用canonical typed contract；外部wire adapter才编码动态payload。

### 19.10 安全切片需要跨入口负向矩阵

至少为shell、fixed argv、workspace write、network/MCP、Hook control和Workflow external effect建立矩阵，逐入口验证：untrusted source、deny、ask/reject、stale approval、generation mismatch、sandbox unavailable、path traversal/symlink replacement、command injection、timeout/cancel、receipt commit failure和secret redaction。

architecture gate只能证明入口依赖关系；真正“未发生side effect”必须由fake runner/FileOps/network provider记录调用次数并在fault test断言为零。

## 20. 第九轮后的新增复审条件

安全与副作用部分还必须补齐：

1. definition→permission→intent→execute→receipt→audit同一effect identity；
2. shell、fixed argv和interactive process不同typed runner；
3. `api_key_helper`删除shell执行且绑定trusted source/secret redaction；
4. control Hook fail closed、单调收窄，argument修改后重新permission；
5. 所有入口复用canonical gate，sandbox失败不fallback；
6. 直接文件写入按owner分类，Agent workspace mutation统一走FileOps；
7. remote success/local failure进入in-doubt而非伪失败；
8. ToolExecutor保持唯一chokepoint，只收窄public surface；
9. ToolResult/媒体/大输出复用canonical artifact/reference；
10. 跨入口安全负向矩阵证明拒绝时effect调用次数为零。

SB0.4不能再作为普通P1清理项；它是Workflow、Hook、BackgroundTask和Tool安全切片的共同前置contract。

## 21. 第十轮：事件、SessionFact、Prompt、缓存与可观测性

### 21.1 `SessionFactSink.commit_fact(object)` 是 durable 主链阻断

当前Contracts正式Port允许任意object进入durable session journal，调用方与sink之间没有authoritative union、schema version或accepted语义。实施文档只写“typed durable accepted union/command sink”，但未列具体迁移顺序和旧入口删除。

应先由Session domain定义closed versioned fact command union，每个variant绑定stream/session identity、event identity、schema version和strict payload。sink在durable commit成功后返回typed append receipt；失败不得更新内存projection或触发外部动作。所有producer迁移后删除object seam、class-set admission和cast恢复。未知fact/version/extra field fail closed。

### 21.2 telemetry泛型必须端到端保持，不能在emitter/observer处再次擦除

当前 `TelemetryEmitter.emit(event: object)`、EventNarrower(object)和Kernel observer `Callable[[Any], ...]`仍会把typed event在内部链路擦除。修复不能建立一个全仓封闭ObservationEvent union，也不能靠runtime TypeGuard把object重新猜回类型。

每个subscription binding保持 `EventT` 从typed emitter到typed handler；内部erasure如为异构存储所必需，只能封装在Runtime owner私有层，并由构造时验证的typed binding保证关系。Kernel通过窄注入seam发Kernel-owned observation event，Runtime adapter映射/路由；Kernel不能依赖Runtime event bus。

### 21.3 control、audit 与observation event必须分开

日志、progress、span和telemetry允许有界丢失，但permission、budget、effect、delivery ack、Workflow/Agent lifecycle settlement不能以telemetry event作为唯一事实或触发器。新文档没有给出事件分类矩阵，容易让已有event bus继续同时承担control和observation。

每个event family需声明authoritative source、durability、delivery guarantee、consumer side-effect policy、replay/idempotency和retention。Control command返回typed receipt；audit event从已commit fact投影；observation可drop/coalesce但不能反向推进状态。禁止subscriber回调直接修改owner内部map绕过command service。

### 21.4 durable subscription需要checkpoint、effect与ack的统一协议

Session/Workflow投影和外部subscriber若有side effect，必须先有stable subscription identity、durable cursor/checkpoint和effect identity。处理顺序应与保证一致：读取committed envelope、执行pure reducer或durable intent包裹的effect、commit projection/receipt、最后ack checkpoint。ack前崩溃允许重投，handler必须幂等或有effect ledger。

poison event不能无限阻塞stream；需要typed dead-letter/quarantine和人工/自动reconcile。checkpoint ahead、gap、wrong stream/generation fail closed。best-effort wake丢失后dispatcher scan仍能发现未处理envelope。

### 21.5 dynamic turn context必须通过typed source，不得重新膨胀PromptBuilder bundle

当前PromptBuilder仍接收Any `turn_context_bus`和宽subsystem bundle。每个turn context source应有稳定name、priority、typed dependencies、suppression和deterministic render；source不得持有完整Role/Context/Environment。cwd、time、git、token pressure、BackgroundTask/LSP通知等易变内容只能进入user prompt的system-reminder，不写入static system prefix或durableconversation history。

Product选择启用哪些source，Runtime拥有收集mechanism，具体domain提供最小query。source失败的disposition按信息性质声明；安全/approval事实不能因context source失败而消失。

### 21.6 prompt cache identity必须绑定最终wire语义与generation

`SYSTEM_PROMPT_DYNAMIC_BOUNDARY`上方必须保持byte-stable且无placeholder/运行态数据；下方内容虽可变化，但cache identity仍需绑定影响模型请求的最终system prefix、tool definitions、command protocol、model/provider capability、output schema和policy generation。

cache key不能依赖对象地址、import顺序或 `inspect.getsource`。tool/hook/skill/MCP/hot reload后若语义identity变化，新turn必须使用新cache generation；旧in-flight turn保持原snapshot。Provider adapter只能在最终wire shape确定后添加provider-specific cache marker，不能改变canonical request identity而不失效cache。

### 21.7 compaction不能把durable truth降级为模型摘要

LLM summary只是一种derived context，不是用户输入、Tool/LLM output、approval、budget、lineage、terminal result、task dependency或 effect receipt的authoritative state。压缩前必须确保这些事实已在各自canonical store/ledger；summary引用大result时使用typedartifact/reference，不能只保存自然语言转述。

只有明确声明reconstructable且可从canonical state无LLM/费用/副作用确定性重建的tool result才能折叠。reprojection绑定source revision/generation，避免把旧结果投到新task/run。unknown或不可重建结果保留必要typed fact/reference，不能为token预算静默删除。

### 21.8 prompt文本中的“最终回复是durable record”不能替代系统durability

当前Compaction prompt告诉模型最终回复是任务durable record。这只能指导输出质量，不能成为持久化保证。最终LLM output必须先经过output validation/commit状态机和durable fact，再向用户surface报告committed；模型没有写出某字段不能使系统事实丢失。

实施文档应明确prompt是policy提示而非control plane。任何durability、permission或cleanup不变量都必须由typed owner执行和测试，不能用prompt措辞补偿架构缺口。

### 21.9 cache/compaction/telemetry不得泄漏secret

prompt cache key与trace可包含digest但不能包含api key、helper stdout、approval token或完整敏感payload；日志/span error不能直接格式化外部exception的secret body。Compaction summary和artifact preview同样经过redaction policy，且redaction不能改变canonical audit fact。

测试需注入canary secret并验证日志、telemetry、prompt render、cache metadata、summary、dead-letter、exception和snapshot均不出现明文；canonical secret store仍按owner保留，不因redaction丢失必要值。

### 21.10 event/Prompt/cache切片也需要generation一致性测试

并发hot reload、compaction、subscriber replay和Agent rehydrate时，一个turn必须看到一致的model/tool/prompt/policy generation。测试应在边界暂停：旧turn构建prefix后reload、新subscriber从checkpoint replay、compaction后资源reprojection、stale telemetry callback。验证旧turn完成于旧snapshot，新turn用新generation，stale callback不能修改新state。

## 22. 第十轮后的新增复审条件

事件与上下文部分还必须补齐：

1. SessionFact closed versioned command union、typed append receipt和object seam删除；
2. telemetry `EventT`端到端关系及私有erasure边界；
3. control/audit/observation事件分类与authoritative source；
4. durable subscriber checkpoint/effect/ack/dead-letter协议；
5. typed turn-context source与易变信息只进system-reminder；
6. prompt/cache identity绑定最终wire语义和generation；
7. compaction只折叠可从canonical state确定性重建的信息；
8. prompt不承担durability/control保证；
9. secret redaction覆盖prompt/cache/telemetry/summary/dead-letter；
10. hot reload、replay、compaction、rehydrate下的generation一致性测试。

这些内容不能并入一个“EventBus重构”大切片；Session durable facts、observation telemetry、turn context和prompt cache分别有不同owner和guarantee。

## 23. 第十一轮：Durable storage、Residency、Artifact、GC 与 Automation

### 23.1 ResidencyRecord当前是宽松未版本化磁盘协议

`orchestration/agents/residency/store.py`明确使用plain JSON dict并称对schema tweak“forgiving”；decoder对缺字段用空dict/list默认，role dump与mailbox/msg buffer未绑定version、logical identity、definition、incarnation或revision。这与durable/wire fail-closed原则冲突。

Residency是可重建incarnation的正式边界，必须有versioned envelope和strict decoder；unknown/extra/missing field、wrong primitive、identity mismatch和corruption明确失败。record至少绑定logical Agent、root/parent/path、definition/content identity、incarnation generation、source session stream revision和materialization fence。不能把损坏record降级为空Role状态继续运行。

### 23.2 磁盘数据不能决定构造任意Role实现

当前默认loader调用 `BaseRole.load(role_dump)`，存在仅凭磁盘polymorphic data选择Role的风险。正确resume流程是Product先按可信、已批准definition/config构造Role/Agent blueprint，再把通过identity/schema验证的历史与运行projection恢复进去。

disk record不能携带可执行class path、hook/MCP/provider/backend选择并自动激活。definition mismatch、config content identity变化或trust失效时fail closed并要求显式Product decision；不能吞错或fallback默认Role。

### 23.3 materialize、rehydrate与forget必须共享incarnation fence

当前ResidencyStore按session_id写单文件，rehydrate后可 `forget()` unlink；如果旧eviction/materialize与新rehydrate并发，旧owner可能覆盖或删除新generation record。所有write/read-claim/delete需绑定revision和monotonic fence：

- materialize只由当前incarnation owner以expected revision提交；
- rehydrate先fenced claim，再构造新incarnation并commit placement；
- record只在新incarnation成功接管且对应revision仍匹配时删除/归档；
- stale owner的late write/forget/release被拒绝；
- crash在materialize、remove runtime、rehydrate、placement commit各阶段可reconcile。

不能以文件存在/mtime或进程内顺序推断ownership。

### 23.4 Cron SchedulerLock不是跨进程fenced lease

当前cron lock使用session_id + PID、PID liveness和unlink抢占；同session新进程可直接重写，PID复用/容器namespace/权限错误会产生ABA，旧owner在失锁后仍可save/fire/release。corrupt lock还会被直接当stale删除。

durable automation需要lease record、expiry、monotonic fencing token和CAS。每次schedule mutation、due-claim、fire intent、delivery/effect receipt、refresh/release均校验fence。旧owner即使仍运行也不能触发或删除新owner任务。lock corruption fail closed并进入repair/reconcile，不得静默抢占。

### 23.5 CronTaskStore跳过损坏记录会造成durable任务静默丢失

`CronTaskStore.load()`对损坏文件返回空列表、对单个bad entry直接skip；下一次save可能永久覆盖丢失任务。这违反durable canonical facts。store需要versioned strict envelope、record identity/revision、CAS和明确corruption policy。允许的tail torn write与中间损坏分开；坏记录quarantine/报告，不能把“读不到”解释为“没有任务”。

durable schedule mutation应通过single owner command/receipt，不做load-modify-save竞态。session-only automation与durable automation使用不同typed identity/guarantee，不能合并list后丢失来源。

### 23.6 Automation fire需要durable intent与delivery settlement

到期并不等于已执行。scheduler在向Agent mailbox、Workflow或外部effect派发前必须commit fire intent，绑定task revision、scheduled occurrence identity和fence；然后通过对应durable delivery/Workflow/effect路径结算receipt。进程崩溃后reconciler区分未派发、已派发未ack和unknown external outcome，不能按当前时间盲目再次fire。

misfire、catch-up、重叠执行、时区/DST、任务更新/删除与已claim occurrence竞态需要typed policy。Observation通知不能成为下一次schedule推进依据。

### 23.7 Workspace cleanup以mtime判断liveness且无fenced owner，可能删除其他进程活跃状态

当前cleanup用rollout/session目录mtime、排除单个session id和stamp节流，并以best effort直接释放artifact ownership/remove tree。在多进程、rehydrate、pending delivery/Workflow/BackgroundTask、legal hold或时钟回拨下，mtime不足以证明dead。

cleanup必须由fenced process/application owner执行，并查询canonical lifecycle/pin/lease/retention facts。删除前取得deletion claim，复核无active incarnation、Workflow/effect/delivery、BackgroundTask residency pin、artifact/legal hold；删除每阶段以revision/fence保护。失败保留可reconcile tombstone，不能部分release artifact后把session当正常active。

### 23.8 Artifact GC必须基于完整reachability与pin generation

Artifact bytes可能被SessionFact、Workflow checkpoint/result、BackgroundTask terminal pointer、tool result、model generation、FileOps snapshot/legal hold引用。GC只看session scope或目录无法证明unreachable。

canonical artifact repository应维护typed ownership edge、retention class、source identity/revision和pin generation。producer先commit reference/ownership再公开pointer；删除owner先移除reference fact再GC。collector以fenced snapshot扫描，minimum age只是安全缓冲，不是reachability证明。stale collector不得删除新generation重新pin的content digest。

### 23.9 retention、legal hold与用户删除需要不同command

TTL cleanup、用户明确删除、security purge、legal hold和测试临时目录的权限/审计不同，不能共用一个best-effort remove函数。实施规格需定义谁能发起、目标projection、preview/approval、receipt、可恢复性和audit。legal hold优先于TTL/GC；用户删除若包含durable历史或artifact必须明确范围和不可逆性。

配置中的 `<=0 means never expire` 等默认属于Product policy，不应硬编码在Runtime mechanism。Runtime接收validated retention command/spec并执行fenced deletion。

### 23.10 durable clock语义需要集中说明

lease expiry、Workflow deadline、cron occurrence、retention TTL和retry backoff不能混用wall-clock/monotonic时间。持久化deadline/occurrence使用带时区绝对时间与clock source identity；进程内elapsed timeout用monotonic clock。重启、NTP回拨/前跳和DST需要确定行为。

测试注入fake clock，不通过修改系统时间或sleep。clock anomaly不能让旧lease复活、重复cron effect或提前删除retained data。

## 24. 第十一轮后的新增复审条件

Storage与cleanup部分还必须补齐：

1. Residency versioned strict envelope及完整identity/revision/fence；
2. Product可信构造后恢复state，磁盘不能选择任意Role实现；
3. materialize/rehydrate/forget统一incarnation fencing与crash reconcile；
4. Cron PID lock替换为CAS lease + monotonic fence；
5. Cron durable store对corruption fail closed，不跳过后覆盖；
6. Automation occurrence intent/delivery/effect settlement与misfire policy；
7. workspace cleanup基于canonical lifecycle/pin并由fenced owner执行；
8. Artifact GC使用完整reachability、retention和pin generation；
9. TTL、legal hold、用户删除和security purge使用不同typed command；
10. wall-clock、timezone与monotonic clock的明确边界和fault tests。

这些要求应加入独立Automation、Residency与Cleanup/Artifact切片；不能留给最终G5/G6顺手清理。

## 25. 第十二轮：总体收敛、依赖 DAG 与复审准入

### 25.1 当前文档应重写，而不是继续局部补丁

原实施文档的治理原则多数正确，但结构仍按G0–G6和P0/P1/P2横向阶段组织，且只覆盖部分SB。前十一轮发现涉及composition owner、durable identity、runner trust、Workflow/Agent/BackgroundTask状态机、storage、event、GC和测试交接。继续在原章节追加例外会产生互相覆盖的规则，执行者仍无法知道先改哪个contract、同切片删除什么。

建议保留第2–4节中已确认产品决定与证据模板，重写第5节以后为：全SB disposition matrix、scope matrix、dependency DAG、逐slice contract和最终验证。旧G5“统一清理阶段”删除；每个slice自己迁consumer、删旧入口和加gate。

### 25.2 推荐的实施依赖 DAG

以下仅是切片依赖建议，不是第二份进度总账；最终slice id和状态由核心总账拥有。

```text
H0  hermetic import/architecture collection
 A0  authoritative governance declaration + 38 SB disposition
 S0  scope/lifecycle matrix + pure Product composition contract
 T0  runner trust domains + effect/permission identity
 D0  strict envelope/codec conventions + migration decisions

 A0,S0,D0 -> AG1 logical Agent identity + durable lineage/spawn intent
 AG1      -> AG2 incarnation/placement/residency fencing
 AG1,T0,D0-> AG3 durable delivery + budget/admission
 AG1,AG2  -> BG1 Agent-owned pool lifecycle/work-pin/reference
 BG1,T0   -> BG2 settlement/release/subtree cancellation

 D0,T0    -> WF1 Workflow definition/run identity + durable command/query
 WF1,AG2  -> WF2 fenced checkpoint/frontier recovery
 WF1,T0   -> WF3 effect intent/receipt/in-doubt reconcile
 WF2,WF3,AG3 -> WF4 terminal delivery/cancel/pause/resume/reconciler

 D0,AG1,AG2 -> RS1 strict Residency materialize/rehydrate protocol
 D0,T0       -> AU1 fenced Automation store/occurrence execution
 AG2,WF4,BG2,RS1 -> GC1 fenced cleanup/artifact reachability

 S0,T0,D0 -> EV1 SessionFact/control/audit/observation boundaries
 S0,EV1   -> CT1 typed turn context/prompt/cache/compaction

 各owner切片完成后 -> 对应package/public-surface迁移与旧入口删除
 全部切片完成后   -> 全量consumer/fault/security/architecture验证
```

H0恢复可执行门禁后，A0/S0/T0/D0的只读取证和contract设计可以并行；生产编码必须按箭头等待上游identity/guarantee确认。D0不是万能codec实现，而是严格envelope规则、可复用primitive和逐domainauthoritative schema决定。

### 25.3 第一批真正可下发的切片不应包含业务状态机重写

复审通过后第一批建议只包含：

1. H0：移除Product package-root eager optional import，恢复完整architecture collection；
2. A0：更新唯一核心总账，为38个SB给出disposition和slice mapping；
3. S0：产出scope/lifecycle matrix，确定Product composition factory与CLI adapter边界；
4. T0：固定shell/fixed-argv/interactive runner contract、effect identity和permission链；
5. D0：列出各durable/wire domain schema、现有codec复用和migration/retention决定。

其中A0/S0/T0/D0在contract确认前主要是需求与测试seam切片，不能提前大规模迁移production owner。H0必须自身闭合并删除eager旧入口，不等待最终cleanup。

### 25.4 产品语义待决清单（已由第 27 节裁决取代）

多数问题可从治理原则和源码自行收敛，剩余真正会改变产品行为的决策至少有：

1. process-local BackgroundTask是否允许同一local TaskId重试/resubmit；若允许需attempt contract，若不允许失败后只能新TaskId；
2. `AgentEnvironment`/`BaseEnvironment`/`MoteEnv`是否存在仓外稳定SDK承诺；无承诺则删除，有承诺才设计最小Port；
3. JSONL是否被批准承载与Temporal相同最低Workflow guarantee；若做不到则只能用于保证更低且名称不同的场景，不能作为Workflow durable backend；
4. 现有Session/Residency/Workflow/Cron/ErrorCode等磁盘数据分别要求保留迁移，还是允许经授权丢弃；
5. worker crash造成BackgroundTask owner-lost时，是否需要产生durable用户/Agent通知；无论选择哪项都禁止恢复/自动重放task；
6. logical Agent terminal后占用logical cap直到何时，以及默认retention/tombstone policy由什么Product场景决定。

本小节是第十二轮形成的中间清单。第27节已基于治理原则完成其中五项裁决，仅逐domain不可逆数据丢弃仍需授权；下一版实施稿以第27节为准。其余文件名、类名、Protocol方法形状和内部算法不需要用户选择。

### 25.5 必须从新实施稿删除的错误或模糊表述

- 删除“保留 `build_engine` 作为application composition owner”；
- 删除独立G5旧入口/残渣清理阶段；
- 删除“Projection仅需收窄注册面”的旧结论，迁Session read model；
- 删除Hook runner是否需要治理仍待证的表述；
- 删除按名称统一删除 `BgTaskResult`、`BgStatus`、`resubmit`；
- 删除对旧local-import误报的暗示；
- 删除未经证据的新 `RoutingModelService`方向；
- 删除把所有durable/concurrent fault case机械应用于每个机制的要求；
- 删除以文件不重叠证明可并行的表述；
- 删除用“G4公共面治理”作为多个bounded context共同完成状态。

### 25.6 必须新增的两个总表

修订稿需要两个静态表，但不维护进度：

1. **SB disposition表**：38个SB各自标记IMPLEMENT/MERGED/REJECTED/EVIDENCE_ONLY，指向唯一ledger owner和slice；
2. **scope/guarantee表**：每个canonical capability记录layer、bounded context、scope、identity、lifecycle owner、durability、guarantee、production entry和删除的旧owner。

表中不得写动态“完成百分比/当前状态”；完成状态只在核心总账。表内容改变必须与对应contract slice同一提交更新，防止文档与源码长期漂移。

### 25.7 复审通过的必要条件

下一版只有同时满足以下条件才可批准：

1. 38个SB全部有disposition，不再靠范围编号或泛化bullet覆盖；
2. Product composition canonical owner、scope matrix和activation顺序明确；
3. BackgroundTask、Workflow、Agent分别有独立状态机和相互引用contract；
4. runner/permission/effect identity作为共同前置边界；
5. durable schema、fencing、delivery、migration和GC保证逐domain闭合；
6. 每个slice有requires/consumers/deletes/tests/gates，且没有后置兼容清理；
7. Projection、Hook、local-import、Squilla、FileOps/Artifact等已知纠错全部进入正文；
8. governance declaration唯一，生成artifact不是第二真相；
9. 真正产品决策集中列出并确认，候选设计不伪装成决定；
10. 文档自身不再声明“可实施”同时保留NEEDS_EVIDENCE项进入执行批次。

## 26. 第十二轮审批意见（由第 28 节最终结论承接）

当前版本：**不批准作为生产实施规格。**

可保留并复用的核心内容：五层边界、唯一Product composition、Agent/Workflow治理方向、每Agent一个BackgroundTaskPool、best-effort边界、零compat原则、证据模板和DoD框架。

必须重写的部分：阶段/批次、composition owner、全部P0垂直切片、G4公共面治理、G5清理、测试交接，以及遗漏的security/storage/event/automation/GC contract。

修订策略应是生成一份新的自洽实施稿并替换当前版本，而不是让实施者同时阅读原文和本评审拼接真实需求。本评审在新稿吸收全部裁决后冻结为证据，不维护实施状态。

## 27. 第十三轮：剩余产品语义的推荐裁决

### 27.1 BackgroundTask 不保留同一 TaskId 的 resubmit

推荐裁决：**TaskId绑定一次submit和一次terminal settlement，不允许terminal/pause后以同一TaskId重新执行。**

理由：BackgroundTask是process-local临时并发，TaskId又只在Agent incarnation pool内唯一；复用同一ID会引入attempt、输出覆盖、通知幂等和旧reference混淆，并继续承载旧Workflow resume语义。用户或控制面需要再次执行时，提交新TaskId并可携带typed `caused_by/retry_of` reference；operation内部透明瞬时retry如确有contract，可作为同一task的attempt ordinal，但不得跨pause、terminal或owner loss，也不得改变logical effect identity。

因此删除public `resubmit()`、Workflow resume adapter和同ID恢复测试；不保留alias。Workflow pause/resume只通过durable RunId command。

### 27.2 Environment facade 按当前仓内事实直接删除

推荐裁决：**在没有用户明确确认仓外SDK承诺前，按无稳定外部承诺处理并删除 `AgentEnvironment`、`BaseEnvironment`、`MoteEnv` 生产入口。**

当前仓内没有生产实例化证据，且facade公开Role/control对象图并形成第二构造链。不能因“也许仓外有人用”建立compat。若产品确实存在已发布SDK承诺，应在实施前由用户提供明确API/版本/consumer证据；届时设计最小hosting/message/human Port，而不是保留原facade。

删除切片需迁移包根export、测试和文档引用，并验证唯一Product composition仍覆盖必要entrypoint。

### 27.3 JSONL 只有通过同一最低保证才可称为 Workflow durable backend

推荐裁决：**Workflow contract不因backend降低。JSONL若实现并通过跨进程CAS/fencing、严格codec、crash recovery、effect reconciliation、durable scan和commit protocol，即可作为显式backend；做不到则不能承载WorkflowRun。**

不为较弱JSONL保留同名Workflow fallback或“best effort durable”模式。显式Temporal选择失败继续fail closed；显式JSONL选择也必须在activation时校验guarantee profile。若需要process-local journal，应使用不同contract/名称，不冒充Workflow durable backend。

### 27.4 durable 数据默认迁移保留，丢弃必须逐domain授权

推荐裁决：**默认把现有用户可见/不可重建durable数据视为需要保留；只有用户逐domain明确授权才允许丢弃。**

至少Session conversation/input/output、approval、effect receipt、Workflow run、Agent lineage/delivery、Cron task和Artifact ownership默认迁移。纯cache、可确定性重建projection和确认无consumer的测试/development residue可重建或删除，但需证据。ErrorCode等wire/schema迁移随其authoritative fact一起处理。

实施前输出逐domain migration matrix：source version、target version、retention reason、migration owner、rollback/retry、旧decoder删除点。任何实际删除现有数据的操作仍需单独明确授权，不能由本推荐自动授权。

### 27.5 worker crash 记录 durable incarnation-loss，不建立逐task durable registry

推荐裁决：**Agent lifecycle ledger durable记录incarnation lost；不为BackgroundTask建立逐task跨进程registry或逐task durable terminal通知。**

旧pool的跨边界task reference绑定incarnation，后续查询得到typed owner-lost。若某个task在崩溃前已产生terminal result并完成canonical result/reference commit，则可从该canonical fact重新投影；否则结果为unknown/lost，不自动重放。用户surface可从incarnation-loss fact投影一次聚合通知，说明该incarnation的临时后台工作可能未完成，但不能枚举或伪造已丢失的task状态。

这同时满足“BackgroundTask不durable”和“已产生terminal truth不能best effort”，且不把supervisor变成task owner。

### 27.6 logical cap 在terminal settlement后释放，identity/tombstone继续保留

推荐裁决：**logical Agent达到terminal并完成children、delivery、budget、effect和BackgroundTask settlement，durable提交terminal/tombstone后释放logical-cap permit；identity与tombstone按retention继续保留且永不复用。**

cap衡量可继续占用治理资源的logical lifecycle，不应被历史记录永久占满。terminal前的DRAINING/CANCELLING仍占cap；仅status字段变final但settlement未完成不能释放。tombstone retention和artifact/legal hold是独立Product policy，不重新占logical cap。purge只删除允许删除的材料，不允许旧identity重生或被新Agent复用。

### 27.7 推荐裁决后的唯一待授权事项

以上六项中，1、2、3、5、6可直接由现有治理原则与源码事实收敛并写入下一版产品决定。第4项中的“具体哪些现存durable数据允许丢弃”仍属于不可逆数据授权，必须在migration matrix完成后逐domain确认。

下一版实施稿不应继续把其余五项标为 `DECISION_REQUIRED`；应采用上述裁决，并把类名/Port形状留给consumer evidence自行设计。

## 28. 第十四轮：唯一最终阻断清单与审批结论

### 28.1 必须修订的十二个阻断域

以下清单取代各轮末尾分散的“新增复审条件”，作为下一版复审的唯一顶层检查表。各历史章节仍提供详细证据和acceptance。

| 阻断域 | 下一版必须达到的结果 |
| --- | --- |
| 权威与覆盖 | 38个SB逐项disposition，唯一ledger owner与slice mapping；无第二状态账本或治理manifest真相。 |
| Composition | Product-owned canonical factory、完整scope matrix、pure construct/async activate/reverse shutdown；CLI只作adapter。 |
| Runner与安全 | shell/fixed argv/interactive process分typed runner；permission/effect identity贯穿Hook、Tool、Workflow、BackgroundTask。 |
| Schema与迁移 | 各domain closed versioned envelope、strict decoder、revision/CAS/fence；逐domain migration/retention matrix。 |
| Agent治理 | durable spawn/lineage、三类cap、placement/incarnation fencing、delivery/ack/dead-letter、budget settlement。 |
| BackgroundTask | 每Agent一个pool；原子work-pin/draining、完整incarnation task reference、无同TaskId resubmit、owner-lost/release/cascade settlement。 |
| Workflow | durable definition/run command-query、checkpoint/frontier、effect reconcile、terminal delivery、cancel/pause/resume与单一reconciler。 |
| Residency/Automation/GC | 可信Product构造后恢复、fenced materialize/rehydrate；Cron fenced occurrence；cleanup/artifact完整reachability。 |
| Event/Prompt/Cache | typedSessionFact、control/audit/observation分离、durable subscriber协议、typed turn context、cache/compaction generation。 |
| 包与服务面 | Projection/Hook/FileOps/Artifact/CodeMap/LSP/ServiceGateway/Error/Config/Presentation按真实owner分别迁移，无公共面大扫除。 |
| 测试与门禁 | hermetic完整collection；consumer/negative API/Pyright；确定性fault/security matrix；唯一derived governance artifact。 |
| 删除与交接 | 每slice同批迁consumer、删旧入口和加gate；固定slice contract；无后置G5、compat、alias、双读写或残渣。 |

### 28.2 已确认产品决定

下一版正文必须直接写入以下决定，不再标为候选：

- 每个逻辑Agent/Role独立拥有一个canonical BackgroundTaskPool；Swarm只集中治理admission，不集中task state；
- TaskId绑定一次submit/terminal settlement，同一TaskId不resubmit；
- worker crash只产生durable incarnation-loss，不建立逐BackgroundTask跨进程registry，不自动重放；
- Workflow backend必须满足同一最低durable guarantee，Temporal选择失败不fallback，JSONL不达标则不得承载WorkflowRun；
- logical cap在完整terminal settlement/tombstone commit后释放，identity永不复用；
- 无仓内production consumer且无明确外部SDK承诺的Environment facade删除；
- durable数据默认保留迁移，任何不可逆丢弃逐domain授权；
- Session projection read model归 `runtime/session`，artifact projection/reconciliation保持共享pipeline；
- Hook registration/matching/folding可保持内聚，外部命令必须复用governed runner，control Hook fail closed；
- FileOps复用canonical Artifact content repository，不新建storage owner；
- CodeMap与LSP保持不同bounded context；Squilla不因同包private import新建伪service；
- text-only child是Product specialization，canonical spawn contract保持OutputT端到端。

### 28.3 下一版文档结构

推荐修订稿只保留一条阅读路径：

1. 权威关系、范围、已确认产品决定；
2. 38项SB disposition；
3. scope/guarantee matrix；
4. dependency DAG；
5. 每个CONFIRMED slice的固定contract；
6. migration/retention授权矩阵；
7. tests/gates/fault/security matrix；
8. 与唯一核心总账的映射和交接规则。

不再保留P0/P1/P2按严重度串行批次、G4横向目录治理或G5统一清理。严重度用于排序，`requires`决定实施顺序。

### 28.4 最终审批

当前 `package-cohesion-service-boundary-debt-governance-implementation.md`：**拒绝批准为生产实施规格，允许作为重写输入。**

下一步应由文档作者依据第28.1–28.3节生成自洽修订稿；在修订稿通过复审前，只允许H0类hermetic import修复和只读证据收集，不下发Agent/Workflow/BackgroundTask/durable storage生产状态机重构。

本评审至此完成当前版本的需求审查。后续应评审修订后的实施稿，而不是继续给当前版本追加实施规则。
