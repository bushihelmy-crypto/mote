# Mote 架构产品决策实施交接

状态：产品决策已确认，待实施  
基线日期：2026-07-31  
适用范围：`contracts <- kernel <- runtime <- orchestration <- product`  
关联需求：`core-architecture-debt-closure-requirements.md`、`durable-workflow-recovery-requirements.md`

## 1. 文档目的

本文固定本轮评审中已经达成共识的五项产品决策，供后续 Agent 直接据此设计、拆解和实施。下列决定不是待讨论建议；实施者不得重新选择相反语义，也不得用兼容 alias、进程内旁路或宽类型临时规避。

实施仍须遵守仓库分层。治理策略及多 Agent、Workflow、BackgroundTask 的高层状态语义归 `orchestration/`；`contracts/` 只定义跨边界 DTO、错误和 Port；`runtime/` 提供持久化、进程、IPC、模型、工具与恢复机制；`product/` 负责可信装配和默认策略；`kernel/` 不反向拥有 Orchestration 状态机。

## 2. 决策一：Workflow 是跨进程可恢复执行单元

### 2.1 已确认语义

- 与 LangGraph durable execution 的能力目标对齐，但不复制其内部实现。
- Workflow 必须支持进程崩溃、Session resume 和 Agent Residency eviction 后继续同一逻辑 run。
- 恢复对象是版本化的 Workflow definition、durable state、checkpoint 和 pending frontier，不是 Python coroutine、closure、线程栈或进程内对象。
- 同一逻辑 run 任一时刻只能存在一个持有有效 lease/fencing token 的 execution owner。
- 外部副作用必须使用 durable intent/receipt 对账；恢复不得盲目重放未知结果的副作用。
- canonical 查询和恢复工具为面向 WorkflowRun 的工具，不保留旧 BackgroundTask 风格的恢复 alias。

### 2.2 实施约束

- Workflow durable 状态机由 `orchestration/workflows/` 拥有。
- identity、checkpoint envelope、状态、错误及存储 Port 放在 `contracts/`。
- Runtime 只实现被注入的持久化、lease、effect execution 等机制，不拥有 Workflow 产品语义。
- definition identity 必须覆盖拓扑、节点契约、codec、重试/超时/interrupt policy 和能力绑定；定义变化后不得按同名 Workflow 静默恢复。

### 2.3 完成判据

- 在 checkpoint 提交前后、节点执行前后、effect intent/receipt 前后分别注入崩溃，恢复结果确定且不重复副作用。
- 两个进程同时 resume 时只有 fenced owner 可以推进状态。
- Session resume 与 Residency eviction 后能从 durable state 恢复，而不依赖旧进程对象。

## 3. 决策二：BackgroundTask 由每个 Agent 独占，治理集中但状态不集中

### 3.1 已确认语义

- 每个逻辑 Agent/Role 独立拥有一个 canonical `BackgroundTaskPool`。任务 registry、计数器、输出、进度、通知、wake callback、取消和 cleanup 均不得跨 Agent 共享或串线。
- supervisor 可以集中治理进程级/树级并发、资源、预算、公平性、背压和级联取消 admission，但只能向 Agent pool 提供窄的 typed permit/admission Port；不得集中拥有 task、result、notification 或 mutable pool state。
- 存在未结算 BackgroundTask 的 Agent 必须 pin residency，不得 eviction；任务全部结算后才可卸载。不得建立 process singleton、scope registry 或 rebind registry。
- Pool lifecycle 由 Agent incarnation/generation 拥有，使用 ACTIVE→DRAINING→CLOSED gate；submit/work-pin acquire 与 eviction/release begin原子互斥，不能只读一次 pending snapshot。DRAINING/CLOSED 拒绝 submit，pin 等 operation/permit/output/terminal notification/resource settlement 全部完成后才释放。
- BackgroundTask 不承诺跨进程恢复。进程结束后旧任务失效；需要跨进程恢复的工作必须建模为 WorkflowRun。
- Agent release 必须显式取消或结算自己的 pool，等待 operation/output/notification cleanup 完成，且不能影响其他 Agent。

### 3.2 身份与安全边界

- `TaskId` 只在所属 Agent pool 内唯一；跨 Agent 查询、取消、结果引用或通知必须同时携带稳定 Agent identity。
- 模型可见的 `TaskId` 在同一 Agent pool 内表示稳定逻辑任务；纯 process-local retry 复用 TaskId，每次执行使用 Pool 内单调 `AttemptId`。旧 attempt 不能覆盖新 attempt，query/cancel 默认指向 active/latest attempt，输出、settlement 和 terminal notification 均携带 attempt identity并保持幂等。
- 新进程或不同 Agent 不得仅凭可控 session id/task id 接管旧任务。
- 跨 Pool task reference 绑定 process instance、Agent identity、incarnation/generation 和 local TaskId；attempt mutation再绑定 AttemptId。worker loss 后旧 reference返回 owner-gone/incarnation-lost，不自动重放，也不能误命中新 incarnation的同名 TaskId。
- 模型仍只看到稳定 TaskId；完整 owner/attempt reference 由绑定当前 Agent pool 的工具与 runtime context 自动构造和验证。
- 磁盘残留输出只能作为带 owner identity 的历史 artifact，不能被解释为仍在运行的任务。

### 3.3 完成判据

- 两个 Agent 使用相同局部 task sequence 时，查询、输出、进度和取消仍完全隔离。
- Agent 有未结算任务时不可 eviction；全部结算后才可卸载，release 只结算自身 pool。
- supervisor permit 的 acquire/release 不创建第二套 task lifecycle 或 mutable registry。
- release 返回 typed settled/draining-timeout/cleanup-failed/owner-lost；失败保持 DRAINING/pin，强制终止 worker由 supervisor policy决定。
- 进程重启后旧 task id 不能被接管，durable 工作明确转入 WorkflowRun。

## 4. 决策三：API-key helper 使用受治理 argv 契约

### 4.1 已确认语义

- `api_key_helper` 只允许来自 USER 或 MANAGED 管理来源。
- PROJECT、WORKDIR、ENV、CLI 和普通 PROGRAMMATIC 输入不得提供或覆盖 helper。
- helper 使用结构化 argv/executable contract，禁止 `shell=True`，禁止 shell expansion。
- 配置 parse/validate/provenance 与外部 secret resolution 是两个阶段；普通配置读取、schema inspection 和 dry-run 不执行 helper。
- stdout 是 secret，只能进入受控 secret resolution 结果，不得进入日志、异常、审计详情、provenance 或普通持久缓存。
- 可审计内容仅包括来源、canonical executable/argv 的非秘密表示、治理决定、时间、退出状态和错误类别。

### 4.2 实施约束

- 建立唯一受治理 helper runner；不得在 loader、adapter 或 model input 中建立平行 subprocess 入口。
- 来源信任必须绑定 canonical path、ownership 和 permission，而不是只相信调用方传入的枚举标签。
- 超时、输出上限、环境变量白名单、cwd 与 executable resolution 必须显式定义。

### 4.3 完成判据

- 陌生 checkout 和项目配置无法启动 helper 进程。
- metacharacter 只作为 argv 字面值，不触发 shell 解释。
- helper 失败可定位且不会泄漏 secret stdout。

## 5. 决策四：逻辑 Agent 树由 Orchestration 治理，不映射为 OS 进程树

### 5.1 已确认语义

- 一个逻辑 Agent 不等于一个 OS 进程；十代 spawn 不得生成十代进程树。
- `orchestration/` 是 Agent 树治理、supervision、预算、调度、驻留和 placement 的 owner。
- 目标模型为一个 supervisor/control plane 加有界 worker 进程池。Agent 是可放置、可卸载、可重新放置的逻辑执行实体。
- 1024 个逻辑 Agent 的基准容量规划为 1 个 supervisor + 16 个 worker，每个 worker 最多驻留约 64 个 Agent；这不是固定协议常量，最终 worker 数必须由内存、活跃并发和单 worker 资源预算计算。
- 驻留数不等于并发执行数。每个 worker 的并发 turn 必须有独立且更小的原子 admission 上限。
- 只有明确需要故障域或安全隔离的 Agent 才能由 placement policy 分配独占进程。

### 5.2 必须治理的维度

- 最大树深；
- 每个父 Agent 的直接子节点数（fan-out）；
- 每个 root run 的逻辑 Agent 总量；
- 每个子树的驻留数、并发数、Token、成本、时限和能力预算；
- 子预算继承、消费和回收；
- 重复任务/递归委派检测；
- 公平调度、背压、超时；
- 父级取消、失败或 release 时的结构化子树级联策略；
- Agent 到 worker 的 placement、重新放置与故障域。

当前的 `max_depth`、fleet cost/token 和 Residency 机制可以复用，但不能独立构成十代树治理。Registry 的 identity 数量上限、turn admission 和 Residency 上限必须分别表达，不能共用一个含义模糊的 `max_agents`。

### 5.3 跨进程 lineage

- Agent lineage 必须跨 supervisor/control-plane 进程重启持久恢复，否则会丢失父子关系和治理权属。
- durable lineage 至少保存 agent id、root/run id、parent id、path、nickname、definition identity、incarnation/generation、lifecycle state、placement 与预算归属。
- lineage mutation 使用版本化 fact/envelope、严格 decoder、原子 revision/CAS 和 fencing。
- 冷启动必须先重建并校验 lineage，再恢复/重新放置 Agent；duplicate path、orphan parent、cycle、nickname 冲突和 incarnation rollback 必须 fail closed。
- worker 崩溃只终止旧 execution incarnation，不删除逻辑 Agent 或 lineage；supervisor 根据 durable 状态决定重放、对账、失败或重新放置。

### 5.4 完成判据

- 1024 个 Agent 不产生 1024 个 OS 进程，且驻留/执行均不越过配置上限。
- fan-out、全树、子树和预算限制在并发 spawn 下原子生效。
- supervisor 重启后可确定性重建十代树、寻址、预算和未结算子任务。
- 父级终止与 worker 故障不会产生无 owner 的孤儿 Agent。

## 6. 决策五：canonical facts durable，可确定性重建的派生信息 best effort

### 6.1 分类规则

如果消息或状态丢失、重复或乱序会改变 Agent 树的可恢复状态、治理决定、用户可见最终结果、费用归属或外部副作用，它必须 durable。

只有满足以下全部条件的信息才允许 best effort：

1. 完整输入和依赖已经存在于 durable canonical state；
2. 可通过已识别版本的算法确定性重建、重新计算或重新发现；
3. 重建不依赖已丢失的进程内状态、瞬时时钟或未持久化随机状态；
4. 重建不会重新调用 LLM、收费/限流/时变外部 API，或产生任何外部副作用；
5. 丢失不改变 lineage、权限、预算、任务状态、审批或最终结果；
6. 系统存在真实的 reconcile/重建入口，而不只是理论上可以重算。

### 6.2 必须 durable 的 canonical facts

- spawn intent、接受/拒绝结果和 lineage mutation；
- 任务委派、接受、依赖关系、terminal state 和 result reference；
- 用户输入、审批、interrupt/resume；
- 权限与能力委派、预算分配/消费/回收；
- LLM 输出以及有费用、限流、时变性或副作用的外部结果；
- Workflow checkpoint、pending frontier、effect intent/receipt；
- 调用方收到 `ACCEPTED` 后会停止自行重试的任何 delivery intent。

durable `ACCEPTED` 只表示 intent 已经持久提交，不表示目标已处理。重复 delivery 必须通过稳定 message id 和 recipient incarnation 幂等结算；dead/released target 必须形成明确 rejected 或 dead-letter 结果。

### 6.3 允许 best effort 的派生信息

- UI 进度、typing、heartbeat、在线状态和重复状态刷新；
- 非审计 telemetry、调试日志；
- 可从 durable facts 重建的索引、缓存、聚合视图和只读 projection；
- 提示性完成通知和唤醒信号，前提是 terminal state、result reference 和 pending dependency 已 durable，恢复或 reconcile 可以重新发现工作。

best-effort 消息不得成为唯一唤醒或推进机制。进程内 queue/park 不能返回 durable `ACCEPTED`；API 必须返回明确的 best-effort disposition，例如 delivered、dropped、target-unavailable 或 backpressured。

### 6.4 完成判据

- accept 后任意崩溃点不会丢失 canonical intent，也不会重复业务消费或副作用。
- 丢弃全部 best-effort 队列后，系统仅凭 durable state 能恢复到相同治理和业务结果。
- LLM 输出、外部结果、权限、预算和 lineage 不会因“可以再算”被错误降级为 best effort。

## 7. 后续 Agent 的实施顺序

1. 先将上述五项决策回填到对应需求条目和验收矩阵，清除相关 `DECISION_REQUIRED` 分支。
2. 在 `contracts/` 固定 identity、envelope、receipt/disposition、policy input/output 和最小 Port。
3. 在 `orchestration/` 建立唯一 Workflow owner、Agent-owned BackgroundTaskPool、独立 typed admission permit、Agent supervision/budget/placement 和 delivery reconciliation。
4. 在 `runtime/` 实现被 Port 约束的 durable store、lease/fencing、process/IPC 与 effect runner。
5. 在 `product/` 建立唯一 composition root、可信默认配置和 helper runner 装配；不得复制 control factory。
6. 最后删除旧 alias、反射 seam、无条件 never-drop 文案和进程内恢复旁路，并运行分层、composition、崩溃点、并发与恢复测试。

## 8. 禁止重新引入的错误模型

- 一 Agent 一进程；
- 以 Residency eviction 代替逻辑 Agent 总量治理；
- 以进程内 BackgroundTask 代替 durable Workflow；
- 以可再次调用 LLM/外部 API 解释“可重新计算”；
- 以进程内 PendingDeliveryQueue 表示 durable accepted delivery；
- supervisor 重启后从零开始建立一棵同名 Agent 树；
- 通过 `shell=True` 或可执行的 PROJECT/ENV/CLI 配置解析 secret；
- 在 Runtime 或 Product 建立第二套 Orchestration 状态机。
