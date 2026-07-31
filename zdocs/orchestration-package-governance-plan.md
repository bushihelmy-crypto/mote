# Orchestration 分包长期治理计划

- 状态：ADR-0001 至 ADR-0006 均已 Accepted；按 Phase 0 至 Phase 5 实施
- 最近评审吸收：2026-07-29

本文定义 `mote.orchestration` 的目标分包架构、迁移顺序和验收门禁，供需求评审、架构评审与后续拆包实施共同使用。

本文只治理 Orchestration 层内部所有权，不重新评审
`contracts <- kernel <- runtime <- orchestration <- product` 五层架构，也不借拆包修改多 Agent、后台任务、图执行或 Cron 的业务语义。

目标不是短期减少文件或目录，而是建立一张未来十年仍可读的能力地图：不了解实现的人仅看到包名，就能判断哪里负责多 Agent 控制、后台任务、工作流和自动化触发；新增能力时也不需要继续扩张 `environment`、`tasks` 或巨型协调器。

---

## 1. 治理目标

### 1.1 总目标

治理完成后必须满足：

1. 一级包直接表达业务能力，不使用 `environment`、`tasks` 等边界过宽的容器词。
2. Agent 控制平面、后台任务生命周期、通用工作流和自动化触发各有唯一 owner。
3. 四个一级能力包之间没有直接运行时依赖；跨能力 adapter 归具体 Product 能力，实例化与 wiring 只发生在 Product composition root。
4. `AgentControl`、`BackgroundTaskPool`、`WorkflowDefinition` 可以作为稳定门面，但不再亲自实现所有子职责。
5. 工作流执行不依赖后台任务宿主；后台运行通过通用 deferred operation adapter 接入。
6. Cron 只负责时间触发和任务持久化，通过 Automation 自有 `TriggerSink` 发出结构化触发，不拥有 Agent 控制或消息投递语义。
7. Spawn 的准入、身份、驻留、成本和回滚仍由单一事务入口保证，不因拆包产生部分提交。
8. 包边界由 AST 架构测试强制执行，不依赖文档约定或人工 `rg`。
9. 不保留旧 import path、forwarding module、兼容 re-export、双实现或永久 baseline。
10. 每个迁移阶段均可独立测试、独立提交，并且使依赖图单调变简单。
11. 每个目标包都能用一句话说明 owner、输入、输出及禁止承担的职责。
12. 普通功能需求原则上只修改一个能力包和至多一个组合入口；跨三个以上一级能力包的变更必须在评审中解释。

### 1.2 “零负债”的可验收定义

本文中的零负债不是零复杂度，也不承诺目录永不变化，而是以下状态：

- 没有已知包级循环依赖。
- 没有两个包共同拥有同一状态真相源、registry、队列或生命周期资源。
- 没有以 `common`、`shared`、`utils`、`misc`、`helpers` 命名的无领域 owner 包。
- 没有通过 `getattr`、私有字段或动态 import 穿透另一个能力边界。
- 没有只能靠 import side effect 完成的装配。
- 没有“下一阶段再删除”的旧路径或 alias。
- 所有跨层能力依赖均通过现有契约或 `contracts/ports` 中有业务语义的窄 Protocol。
- 每项持久化数据都有唯一 schema owner；移动 Python 模块不改变持久化身份。
- 每项并发资源都有明确 owner、作用域、关闭顺序和取消语义。
- 所有例外都有精确位置、删除条件和同阶段测试，不存在可增长的架构白名单。

### 1.3 非目标

本治理不做以下事情：

- 不修改五层依赖方向。
- 不重写 Agent 调度算法、图执行算法、后台任务协议或 Cron 语义。
- 不以单文件行数作为唯一拆分依据。
- 不为对称目录结构制造无业务价值的抽象。
- 不把 Orchestration 问题下沉到 Runtime，也不把通用编排能力上推到 Product。
- 不建设通用插件框架、分布式队列或远程调度系统。
- 不在拆包阶段更改对用户可见的工具名、消息内容或持久化格式。
- 不为了避免正确搬迁而在函数内增加延迟 import。

---

## 2. 当前基线

### 2.1 规模与现状

以 2026-07-29 当前工作树为基线：

| 当前包 | Python LOC | 当前主要职责 |
| --- | ---: | --- |
| `orchestration.environment` | 约 5,116 | Agent 身份、Spawn、通信、驻留、执行调度、Cron、产品环境适配 |
| `orchestration.tasks` | 约 6,828 | 后台任务、结果落盘、进度通知、停滞检测、图 DSL 与图执行 |
| 合计 | 约 11,945 | Orchestration 层全部能力 |

当前五层 import 方向健康：没有批准中的低层反向依赖，现有层级架构测试通过。主要问题不是层间违规，而是 Orchestration 层内部能力地图、所有权和模块变化轴不清晰。

### 2.2 主要问题

#### P0：一级包名不能表达能力

`environment` 同时表示控制平面、运行时对象、驻留、通信和 Cron；`tasks` 同时表示后台协程、持久化结果、通知和通用 DAG。新成员无法仅凭包名判断正确归属。

#### P0：工作流错误从属于后台任务

`tasks.bggraph` 已拥有独立构图 API、DSL、编译、校验、执行、恢复和前台 `arun`。它不是后台任务的内部实现；当前路径导致工作流必须依赖 `BgTaskResult`、后台进度语义和 `tasks` 命名。

#### P0：三个模块包含多个独立变化轴

| 模块 | 约 LOC | 混合职责 |
| --- | ---: | --- |
| `environment/control.py` | 1,182 | Spawn、装配、成本树、通信、驻留恢复、watcher、控制面生命周期 |
| `tasks/pool.py` | 968 | 注册、执行、等待、取消、重提、领养、交付、唤醒、格式化、回收 |
| `tasks/bggraph/from_spec.py` | 945 | DSL binding、predicate、表达式、节点工厂、依赖分析、连边、编译 |

这些文件不是单纯“太长”，而是不同需求会修改同一模块，造成长期冲突和隐式耦合。

#### P1：跨边界对象探测仍存在

Agent Spawn 上下文构造会探测 Role 的 `_config`、`wiring.services.context` 等实现细节。该做法没有违反五层 import 方向，却使 Orchestration 知道 Runtime Agent 的内部对象图，未来 Role 装配变化会迫使控制平面同步修改。

#### P1：Cron 被藏在 Agent Environment 下

Cron 是自动化触发能力，其变化原因是表达式、时间、锁、持久化和触发，而不是 Agent 驻留或生命周期。`CronService` 可以调用 Agent 控制面的公开投递能力，但不应被控制平面拥有。

#### P1：公开聚合面过宽

`environment.__init__` 和 `tasks.__init__` 同时导出门面、内部模型、存储、错误、调度器和辅助函数。根聚合导入扩大 eager-import 面，也让内部类型在无意中成为事实 API。

#### P2：产品适配与通用控制面混放

`environment.mote.MoteEnv` 是 Mote 产品交互适配，而非通用多 Agent 控制机制。它不应决定 `orchestration.agents` 的公共边界。

### 2.3 当前测试基线

调研时以下测试集合全部通过：

```bash
python -B -m pytest \
  ztest/architecture/test_layer_dependencies.py \
  ztest/architecture/test_runtime_governance.py \
  ztest/environment \
  ztest/tasks \
  -q --tb=short -p no:cacheprovider
```

测试存在少量既有 Pytest 标记和未 await coroutine 警告；它们不作为本次分包的失败基线，也不能在迁移中新增。

---

## 3. 目标架构

### 3.1 一级能力包

目标一级目录：

```text
orchestration/
├── agents/              # 多 Agent 控制平面
├── background_tasks/    # 异步任务生命周期与结果交付
├── workflows/           # 通用有向图、声明式工作流和恢复执行
└── automation/          # 基于时间或外部信号的自动化触发
```

一级目录回答“Orchestration 提供什么能力”，二级目录回答“能力内部如何分工”。不建立顶层 `services`、`managers`、`stores` 或 `engines` 等横向技术分类。

### 3.2 目标依赖方向与组合位置

四个一级能力包彼此没有直接运行时依赖：

```text
                    product.composition (只实例化/wiring)
                   /           |             \
                  v            v              v
        product.workflows    product.automation   product.agents
        WorkflowTaskAdapter  AgentTriggerAdapter  Spawn adapters
             |       |          |        |          |
             v       v          v        v          v
        workflows  background_tasks   automation   agents
             \________ lower-layer contracts/runtime ________/
```

固定规则：

- `workflows` 不依赖 `background_tasks`、`agents` 或 `automation`。
- `background_tasks` 接收自己的通用 `DeferredOperation`，不 import 或探测 workflow 类型。
- `WorkflowTaskAdapter` 的代码归 `product.workflows`，把 `WorkflowRun`/`WorkflowContinuation` 转成 background-owned operation；由 Product composition 实例化。
- `agents` 不依赖 `background_tasks`、`workflows` 或 `automation`。
- `automation` 不依赖 `agents`；它只调用自己定义的 `TriggerSink`。
- `AgentTriggerAdapter` 的代码归 `product.automation`，把 `AutomationTrigger` 映射为 Agent public command；由 Product composition 实例化。
- 四个能力包都可以依赖低层稳定契约与 Runtime capability。

Product composition 是唯一跨一级 Orchestration 能力装配位置，但不是 adapter 代码的横向收纳目录。Orchestration 包内不得建立第二个 composition root。

### 3.3 目标目录

```text
orchestration/
  agents/
    __init__.py                 # 只导出稳定控制面门面和公共请求/结果
    control.py                  # 薄 AgentControl facade、跨子服务事务编排
    identity/
      path.py
      registry.py
    lifecycle/
      runtime.py
      handle.py
      spawn.py                  # SpawnCoordinator 与原子提交/回滚
      admission.py
      supervision.py            # completion/TTL watcher
    messaging/
      model.py
      mailbox.py
      routing.py
      pending.py
      delivery.py
    residency/
      manager.py
      store.py
    execution/
      limiter.py
      turn_scheduler.py
    costing/
      fleet.py

  background_tasks/
    __init__.py                 # BackgroundTaskPool 等稳定 API
    pool.py                     # 薄门面
    registry.py                 # TaskMeta 与活动任务真相源
    execution.py                # semaphore、timeout、runner
    lifecycle.py                # cancel/resubmit/adopt/reap/close
    delivery.py                 # wake、terminal result、notification
    decorators.py
    promotion.py
    results/
      model.py
      store.py
      attachment.py
    monitoring/
      progress.py
      stall.py
      turn_context.py

  workflows/                    # capability-agnostic graph execution kernel
    __init__.py                 # WorkflowDefinition、WorkflowOutcome 等稳定 API
    builder.py                  # mutable declaration builder
    definition.py               # frozen reusable definition
    run.py                      # per-execution mutable owner
    nodes.py
    channels.py
    topology.py
    validation.py
    state.py
    execution/
      driver.py
      node_runner.py
      retry.py
      resume.py
      result.py
    events.py                    # 只含结构化 workflow facts 与 ProgressSink

  automation/
    cron/
      expression.py
      task.py
      scheduler.py
      service.py                 # 只依赖 TriggerSink
      store.py
      lock.py
```

模型可见 `run_graph` DSL 不属于通用 Workflow 内核，目标位于 Product：

```text
product/
  workflows/
    background_adapter.py       # WorkflowOutcome -> OperationOutcome
    continuation_registry.py    # session-scoped opaque resume/run refs
    inspection.py               # immutable RunSnapshot query port
    run_graph/
      tool.py                   # 模型可见 Tool
      spec.py                   # GraphSpec JSON Schema
      compiler.py               # Tool/Agent operation -> WorkflowDefinition
      bindings.py
      operations.py
      presentation.py
      resume_tasks.py
      get_node_state.py
```

目录是目标所有权，不要求一次性创建所有文件。若某个候选模块拆出后只有转发逻辑，应与相邻 owner 合并，不为图形对称保留空壳。

### 3.4 命名决策

- `environment` 更名为 `agents`：前者描述运行场所，后者描述被编排的领域实体。
- `tasks` 更名为 `background_tasks`：明确只拥有异步任务生命周期，不再占用通用 `tasks` 词义。
- `bggraph` 更名为 `workflows`：图既可前台运行也可后台运行，`Bg` 不是领域本质。
- `scheduling` 不作为一级包：Turn scheduling 属于 `agents.execution`，Cron scheduling 属于 `automation.cron`，二者没有共同 owner。
- 公共构图 API 固化为 `WorkflowBuilder.build() -> WorkflowDefinition`。`BgGraph` 是 repository-internal 名称，同一 Slice 更新仓内调用方并删除，不保留双命名。

---

## 4. 包级所有权

### 4.1 `orchestration.agents`

一句话职责：维护一个 session 范围内多 Agent fleet 的身份、生命周期、通信、驻留和 turn 执行控制。

拥有：

- Agent identity、path、nickname、lineage registry。
- Spawn admission、资源预留、原子提交与回滚。
- Agent runtime handle、状态和 completion/TTL supervision。
- Mailbox、通信图、延迟投递和背压。
- Live residency、驱逐、恢复及其持久化。
- Turn concurrency limiter 和 event-driven scheduler。
- Fleet cost attribution tree。

不拥有：

- Role/Agent 的具体产品构造策略。
- 后台任务池和工作流执行。
- Cron 表达式、Cron store 或进程入口。
- 用户界面、approval 展示或 Product 默认路径。

`AgentControl` 继续作为跨子服务的唯一事务门面。拆分后不能让调用方自行串联 registry、residency 和 scheduler，从而绕过 Spawn 不变量。

### 4.2 `orchestration.background_tasks`

一句话职责：管理进程内异步任务从提交到终态交付的完整生命周期。

拥有：

- Task ID、TaskMeta、状态、活动任务 registry。
- 通用 `DeferredOperation` 提交协议；它只有执行、协作式停止、关闭和结构化终态，不携带 workflow 内部字段。
- 并发限制、timeout、等待、取消、重提、领养和关闭。
- 终态结果交付、Agent wake 和任务通知。
- 大结果存储、读取位置、attachment 和 turn reminder。
- 后台任务停滞检测。

不拥有：

- 图拓扑、节点调度、DSL、图恢复算法。
- Agent mailbox、Agent residency 或 fleet scheduler。
- Runtime Role 的组件装配。

`BackgroundTaskPool` 保留为稳定 facade；`TaskRegistry` 等内部组件不通过根 `__init__.py` 暴露。Pool 不接收 `graph_ref`、`run_state`、node names 或 workflow resume metadata。

### 4.3 `orchestration.workflows`

一句话职责：定义、验证、执行和恢复本地强类型工作流图。

拥有：

- Graph/node/edge/channel/state 模型。
- 构图 API、拓扑校验、参数校验和 schema introspection。
- Frontier driver、节点并发、retry、pause、skip 和 resume。
- 通用 WorkflowBuilder、frozen definition 和 operation topology；不拥有模型可见 GraphSpec。
- 工作流级结构化执行事实与稳定引用。

不拥有：

- asyncio task 的全局池化、跨工作流任务 ID 或 Agent wake。
- Agent 生命周期、通信和驻留。
- 产品 Tool catalog；具体 Tool executor 由调用方注入。

工作流拥有 `WorkflowBuilder`、`WorkflowDefinition`、`WorkflowRun`、`WorkflowOutcome` 和 `WorkflowContinuation` 五个不同概念。其精确定义见 [ADR-0001](./adr/0001-workflow-execution-model.md)。工作流只发出结构化 `WorkflowEvent`；不渲染面向模型或用户的文本，详见 [ADR-0002](./adr/0002-workflow-progress-boundary.md)。

`product.workflows.WorkflowTaskAdapter` 把 `WorkflowOutcome` 转成 background-owned `OperationOutcome`，并把 run 包装成 single-use `DeferredOperation`。Pool 只观察自己的 outcome union，禁止读取 workflow 类型或内部字段。

### 4.4 `orchestration.automation`

一句话职责：根据持久化触发规则，在合适时间调用公开编排入口。

首个子能力是 `automation.cron`，拥有：

- Cron expression parse、next-run 和 human rendering。
- Cron task、jitter、store、进程锁、scheduler 和 service。
- `AutomationTrigger`、`TriggerSink` 和到期触发结果记录。

不拥有：

- AgentRuntime、mailbox 或 registry 内部状态。
- 通用 PeriodicLoop primitive。
- CLI 参数解析和 Product 默认 store 路径。

Cron 不知道触发目标是否为 Agent。它向 `TriggerSink` 提交包含稳定 trigger ID、opaque target、content、scheduled time 和 attempt 的 `AutomationTrigger`。Product 的 `AgentTriggerAdapter` 才负责映射为 Agent command；详见 [ADR-0003](./adr/0003-automation-trigger-port.md)。

### 4.5 一级包输入/输出契约

| 能力包 | 输入 | 输出 | 明确禁止泄漏 |
| --- | --- | --- | --- |
| `agents` | Spawn request、稳定 parent snapshot、注入的 child/provision/cost capabilities、agent command | handle、status、team view、delivery/interrupt outcome、lifecycle event | Role 私有字段、Runtime Context、mailbox 实现、mutable registry |
| `background_tasks` | `DeferredOperation`、StopReason/StopDisposition、task submission options、result/wake sinks | task ID、五类 task outcome pointer、五类 terminal TaskEvent、task lifecycle event | graph ref、node state、workflow continuation、Role |
| `workflows` | builder declarations、initial input、operation callables、`ProgressSink` | frozen definition、run、structured event、outcome、continuation | ToolResult、background task ID、Agent、GraphSpec、model-facing text |
| `automation` | schedule、`AutomationTrigger`、`TriggerSink`、clock/store | trigger receipt、schedule state、automation event | AgentControl、UserMessage、DeliveryMode、mailbox/runtime |

---

## 5. 当前模块处置表

### 5.1 `environment`

| 当前模块 | 目标 owner |
| --- | --- |
| `_scope.py` | 就近内联到 `agents.identity.registry` / `agents.residency.manager` / `agents.execution.limiter`；禁止形成 shared helper |
| `agent_catalog.py` | 迁至 `product.agents` declaration catalog；fleet identity 不拥有 spawnable type discovery |
| `agent_path.py` | `agents.identity.path` |
| `registry.py` | `agents.identity.registry` |
| `runtime.py` | `agents.lifecycle.runtime` |
| `handle.py` | `agents.lifecycle.handle` |
| `spawn_policy.py` | `agents.lifecycle.admission` |
| `control.py` | `agents.control` 门面 + lifecycle/messaging/costing 子服务 |
| `comms.py` | `agents.messaging.routing` |
| `mailbox.py` | `agents.messaging.model` + `agents.messaging.mailbox` |
| `pending_delivery.py` | `agents.messaging.pending` |
| `limiter.py` | `agents.execution.limiter` |
| `turn_scheduler.py` | `agents.execution.turn_scheduler` |
| `residency.py` | `agents.residency.manager` |
| `store.py` | `agents.residency.store` |
| `scheduling/*` | `automation.cron/*` |
| `base_env.py` | 保留并收窄为 `agents.environment_facade`：它是旧 `BaseEnvironment` 契约到控制面的 adapter；替代契约出现前不机械迁移 |
| `mote/mote_env.py` | 迁入具体 Product interaction/agent adapter；代码不进入 `product.composition` |
| `exceptions.py` | 删除聚合转发；错误继续由其真实 contracts/runtime owner 导出 |

### 5.2 `tasks`

| 当前模块 | 目标 owner |
| --- | --- |
| `pool.py` | `background_tasks.pool` 门面 + registry/execution/lifecycle/delivery |
| `types.py`、`status.py` | 按语义拆入 `background_tasks` model 与 `workflows.state/result` |
| `disk_output.py` | `background_tasks.results.store` |
| `attachment.py` | `background_tasks.results.attachment` |
| `decorators.py` | `background_tasks.decorators` |
| `promotion.py` | `background_tasks.promotion` |
| `stall_detector.py` | `background_tasks.monitoring.stall` |
| `turn_context_source.py` | `background_tasks.monitoring.turn_context` |
| `constants.py` | 常量跟随真实 owner，删除横向 constants 模块 |
| `bggraph/base_node.py` | Operation/NodeDefinition/InputRef 归 `workflows.nodes`；docstring、Annotated/From、JSON Schema 和模型 metadata 归 `product.workflows.run_graph` |
| `bggraph/channels.py` | `workflows.channels` |
| `bggraph/graph.py` | `workflows.graph` + topology/validation |
| `bggraph/engine.py` | `workflows.execution/*` |
| `bggraph/types.py` | `workflows.state` + topology + execution result |
| `bggraph/spec.py` | `product.workflows.run_graph.spec`；这是模型可见 Tool DSL，不是通用 Workflow definition |
| `bggraph/from_spec.py` | 通用 topology/build 部分归 `orchestration.workflows`；ToolResult、tool/map/fold/compute、模型失败收集归 `product.workflows.run_graph.compiler/operations` |
| `bggraph/report.py` | 结构化事实归 `workflows.events`；outcome adapter 归 `product.workflows`；task delivery 归 `background_tasks` |
| `bggraph/notify.py` | 删除 Orchestration 文本渲染；结构化事实归 workflow/task owner，模型/UI 文本归 Product presentation |
| `bggraph/marker.py` | 删除 marker/实例扫描；`contracts.tools.ToolExecutionKind` + frozen ToolDefinition 显式分类 ATOMIC/WORKFLOW_FOREGROUND/WORKFLOW_DEFERRED |

当前 `resume_tasks.py` 与 `get_node_state.py` 迁入 `product.workflows.run_graph`，改为只依赖 `BackgroundTaskQuery`、`WorkflowContinuationRegistry` 和 `WorkflowInspectionPort`；禁止读取 Pool TaskMeta 或 live graph internals。完整迁移契约见 [ADR-0006](./adr/0006-bggraph-migration-contract.md)。

---

## 6. 关键架构决策

### 6.1 门面与实现分离

`AgentControl`、`BackgroundTaskPool` 和 `WorkflowDefinition` 是面向调用方的行为门面，不是所有状态和算法必须集中在一个类中的理由。

门面要求：

- 保持唯一合法业务入口。
- 编排内部 owner，不复制其状态。
- 不公开内部 service 实例供调用方绕过不变量。
- 构造参数显式，生命周期由创建它的 composition root 管理。

### 6.2 Spawn 原子性不可拆散

Spawn 当前同时涉及 admission、residency slot、identity reservation、path/nickname、Role 构造、cost node、runtime/route 注册和 watcher。物理拆模块后由资源型 `SpawnTransaction` 维护显式状态机，而不是由多个 service 共享布尔标志或嵌套 `try/finally`。

状态按单向顺序推进：

```text
NEW -> ADMITTED -> RESIDENCY_RESERVED -> IDENTITY_RESERVED
    -> CHILD_BUILT -> PROVISIONED -> REGISTERED_INERT
    -> SUPERVISED -> RUNNABLE/COMMITTED
    -> ROLLED_BACK
```

规则：

- 每个 reserve/build/register 步骤返回 transaction-owned lease。
- lease 在 `commit()` 前只由 transaction 释放；commit 后所有权一次性转移给 Agent lifecycle owner。
- `rollback()` 幂等、并尝试释放所有已获得资源；一次释放失败不得阻止其余资源释放。
- 任意 await 点收到 `CancelledError` 都必须 shield/完成 rollback 后再重新抛出取消。
- inert registration 对外不可寻址、不可调度；supervisor subscription/TTL 必须在首次 runnable 前安装。
- commit/activate 是不可取消、无 IO、无 await 的短临界区，一次性发布 route/runtime 并转移所有权；成功后 Agent 才可寻址和调度。
- supervisor 安装失败必须 rollback，不能把 child 标为 degraded 后继续执行。
- 当前纯内存 commit 必须全量完成或因内部不变量失败而使控制面故障；reconciliation 只属于未来 durable/cross-process ADR。
- commit 后 release/shutdown 归 Agent lifecycle owner，transaction 不再释放资源。

完整决定见 [ADR-0004](./adr/0004-spawn-transaction.md)。

验收必须覆盖每个失败点的回滚：

- admission 拒绝不创建任何预留。
- Role factory 失败释放 identity 与 residency。
- context/service provision 失败不留下 cost node 或 comm route。
- runtime 注册失败不留下可寻址 Agent。
- supervisor 安装失败时 Agent 从未 runnable。
- 任意阶段的 `CancelledError` 不留下 reservation、route、cost node、runtime 或 watcher。
- EPHEMERAL/MANAGED 生命周期继续拥有不同 slot 释放语义。

### 6.3 用窄能力替代 Role 内部探测

Orchestration 不应读取 `_config`、`_context` 或嵌套 wiring 私有结构。Spawn 输入拆成稳定值和窄能力：

- `ParentSpawnSnapshot`：parent ID、cwd、agent path 及稳定继承选择数据；不含 Runtime Context/config object。
- `ChildFactory`：根据已批准的 Spawn request 和 snapshot 创建 opaque child capability。
- `ChildContextProvisioner`：执行 FRESH/SHARE_PARENT policy，不向 Orchestration 返回 Context。
- `CostAttributionPort`：登记、查询预算视图和释放 cost attribution，不暴露 `CostTracker`/`CostNode`。

Port 放置遵循消费方所有权：只被 Orchestration/Product 组合使用的接口定义在 `orchestration.agents.lifecycle`；低层也必须依赖时才进入 `contracts/ports`。Contracts 中的类型只能包含稳定 DTO/Protocol，禁止引用 Runtime Context、Role、RoleState、EngineServices、CostTracker、CostNode 或任意 `dict`。

### 6.4 Workflow 与后台宿主解耦

目标调用关系：

```text
WorkflowBuilder.build() -> WorkflowDefinition
WorkflowDefinition.start(input, progress_sink) -> WorkflowRun
WorkflowRun.execute() -> WorkflowOutcome
WorkflowContinuation.resume(request) -> WorkflowRun
WorkflowTaskAdapter.defer(run) -> DeferredOperation[OperationOutcome]
BackgroundTaskPool.submit(operation) -> TaskId
```

`WorkflowBuilder` 是可变声明器；`build()` 完成复制、校验、freeze 和稳定 definition identity 生成。`WorkflowDefinition` 不保存 bound builder 或可变拓扑，并可并发创建多个 run。

`WorkflowOutcome` 是 `Succeeded | Paused | Failed | Cancelled | TimedOut` 的 workflow-owned 封闭 union；所有明确可恢复终态均可携带 ResumeCapability。Product adapter 将它转换成同样五类的 background-owned `OperationOutcome`，非成功 outcome 只暴露 optional opaque ResumeRef。Pool 不得接收、判断或探测 `WorkflowOutcome`。

Pool 的 user cancel/timeout 通过 `request_stop(reason, disposition)` 协作停止并取得终态；只有超过 grace period才强制 cancel，强制终态无 ResumeRef。shutdown 默认 DISCARD，user cancel/timeout 默认 CHECKPOINT；execute/request_stop 竞争由 operation 内部线性化。五类 OperationOutcome、对应 TaskStatus/terminal TaskEvent 和必要 Stop 枚举从 `background_tasks.__init__` 稳定导出，内部 operation 状态机不导出。

当前 `BgTaskResult.hybrid` 的“即时提示 + 后续执行”在 Product Tool adapter 中分成 presentation acknowledgement 和 deferred submission，不能进入 Workflow 领域模型。

工作流可以独立前台运行、单测和恢复；后台池只看到 deferred operation。进度通过 `ProgressSink` 注入，不使用 workflow 对后台池的 ambient global 查找。

### 6.5 持久化身份先审计后移动

移动前必须建立“稳定身份清单”，覆盖所有外部可观察 identity，而不只检查持久化 JSON：

- 持久化 JSON 字段、schema version 和 discriminator。
- `module`/`qualname`/source 派生 hash（当前 `AgentCatalog.version` 已命中）。
- registry key、Agent/Tool/Graph canonical name。
- error/event type tag、telemetry identity 和 cache key。
- Residency record、Role loader、task result pointer、graph run state 和 Cron store。

`AgentCatalog` 是 Product declaration catalog，不是 fleet identity；迁移到 `product.agents`。其 version 不得再由 Python 模块路径决定，改由稳定 agent name、显式 `definition_version` 和经评审的 definition digest 生成。

任何移动造成 identity 变化前，必须先引入稳定 type ID/schema version 和一次性迁移。不能靠保留旧 Python 模块解决兼容。清单、发布和中断恢复策略见 [ADR-0005](./adr/0005-public-api-and-stable-identity.md)。

### 6.6 Public API 政策

最终源码状态中，仓内调用方统一切到新 canonical path，并删除：

```text
mote.orchestration.environment
mote.orchestration.tasks
mote.orchestration.tasks.bggraph
```

已与产品负责人确认：仓库外没有 Python import 调用方，这些路径均为 repository-internal。同一 Slice 原子更新包括 `run_graph`、`resume_tasks`、`get_node_state` 在内的仓内调用方并删除旧路径，不建立兼容发布窗口或 forwarding package。

`run_graph` 的模型可见 Tool 名称、参数 JSON Schema、核心行为和 durable data identity 仍是外部可观察契约；删除 `BgGraph` Python path 不授权改变这些契约。

详细策略见 [ADR-0005](./adr/0005-public-api-and-stable-identity.md)。

### 6.7 Progress 与 Presentation 边界

```text
WorkflowEvent    workflows 拥有的执行事实
ProgressSink     workflows 定义的输出 Port
TaskEvent        background_tasks 拥有的任务生命周期事实
Task delivery    background_tasks 负责结构化交付/幂等
Presentation     Product 将 WorkflowEvent/TaskEvent 渲染为模型或用户文本
```

Orchestration 不拥有面向模型、CLI 或用户的 progress 文本模板。`WorkflowEvent` 与 `TaskEvent` 可以由 Product projector 合并展示，但不能合并成一个领域事件类型。

`ProgressSink.emit()` 固定为 async；单一 run 严格保序。Run 创建时固定 durable 或 observational policy：durable sink 背压且失败产生 `ProgressDeliveryFailure`，observational sink 可丢弃但必须累计 dropped count。Terminal event 成功交付后 `execute()` 才返回 outcome；sink callback 禁止重入控制同一 run。

### 6.8 二级包依赖矩阵

#### `agents`

| 子包 | 可依赖的同能力子包 |
| --- | --- |
| `identity` | 无 |
| `messaging` | `identity` |
| `execution` | `identity`、`lifecycle.model` |
| `residency` | `identity`、`lifecycle.model` |
| `costing` | `identity` |
| `lifecycle` | `identity`、`messaging`、`execution`、`residency`、`costing` |
| `control` | 上述全部；只做门面和事务编排 |

`lifecycle.model` 必须是无副作用叶子模块；`execution`/`residency` 不得导入 spawn coordinator 或 supervisor。

#### `background_tasks`

| 子包 | 可依赖的同能力子包 |
| --- | --- |
| `model` | 无 |
| `results` | `model` |
| `registry` | `model`、`results` |
| `execution` | `model` |
| `monitoring` | `model`、只读 registry Port |
| `lifecycle` | `model`、`registry`、`execution`、`results`、`monitoring` |
| `delivery` | `model`、`results` |
| `pool` | 上述全部；只做 facade |

#### `workflows`

| 子包 | 可依赖的同能力子包 |
| --- | --- |
| `model/state/events` | 无 |
| `nodes/channels` | `model/state` |
| `topology` | `model`、`nodes` |
| `validation` | `model`、`nodes`、`channels`、`topology` |
| `execution` | `model/state/events`、`nodes`、`channels`、`topology` |
| `builder` | `model`、`nodes`、`channels`、`topology`、`validation` |
| `definition` | frozen `model/topology`；不依赖 builder mutable state |
| `run` | `definition`、`execution`、`events` |

#### `automation.cron`

| 模块 | 可依赖的同能力模块 |
| --- | --- |
| `model/trigger` | 无 |
| `expression` | `model` |
| `store/lock` | `model`，二者互不依赖 |
| `scheduler` | `model`、`expression`、`store`、`lock`、`TriggerSink` |
| `service` | 上述全部；不依赖任何具体 trigger adapter |

### 6.9 BgGraph 迁移契约

[ADR-0006](./adr/0006-bggraph-migration-contract.md) 是 Phase 1 的执行合同，额外固定：

- session-scoped、single-consume ContinuationRegistry 与 immutable RunSnapshot。
- Paused 以及明确可恢复的 Failed/TimedOut/Cancelled 均通过 optional ResumeCapability 生成 opaque ResumeRef；fatal definition/schema failure 不可恢复。
- DeferredOperation 以 `request_stop(StopReason, StopDisposition)` 协作产生 cancel/timeout outcome；execute/stop 竞争线性化，grace period 后强制终止不带 ResumeRef。
- ResumeCapability 携带 allowed actions；无安全 checkpoint 但 Definition/initial input 有效时可只允许 FULL_RESTART，显式 DISCARD 不保留旁路恢复。
- result presentation、continuation 和 inspection 使用三个独立 retention 生命周期；RunSnapshot 深冻结、按 visibility/query 投影，大值只给稳定引用。
- SnapshotValueProjector 区分 inspection projection 与恢复 checkpoint；unsupported inspection value 可 unavailable，恢复必需字段不可冻结则不得签发局部 capability。
- `resume_tasks/get_node_state` 不再穿透 BackgroundTaskPool metadata。
- BaseNode 的通用 operation 与 Product introspection 拆分。
- ToolDefinition 上的名义化 `ToolExecutionKind`，在 rename/prefix/copy/alias/dynamic composition 中保持传播，彻底删除 marker/实例字段扫描。
- 前台 `run_graph` 与后台 compiled Workflow 两条行为路径。
- 通用 Workflow 只拥有 DecisionPoint/DecisionPolicy/Continuation；Product adapter 才拥有 ModelRoute/HumanQuestion/Approval DTO。后台交互暂停、释放 worker、经 Agent turn 决策后恢复。
- 顶层单 effect-ledger receipt、节点无 result_id 的崩溃恢复不变量；节点外部副作用在 receipt commit 前崩溃仍是 at-least-once。
- GraphSpec canonical definition_id、compiler semantic version、独立 execution_revision 与手写 Workflow 显式 identity。

ADR-0006 未 Accepted 前，不得创建目标目录或开始移动 `bggraph` 文件。

---

## 7. 实施阶段

### Phase 0：建立可执行基线

范围：

1. 新增 Orchestration import graph AST 测试。
2. 记录当前一级、二级模块依赖图和 SCC。
3. 固化四个目标能力包及禁止边。
4. 前五份 ADR 已 Accepted；评审并接受 ADR-0006 后才允许进入 Phase 1。
5. 审计 public API 和完整稳定身份清单，不限于持久化 module-qualified identity。
6. 为当前关键行为补齐 characterization tests，不改业务实现。

完成条件：

- 新增回边会被测试准确捕获。
- 当前依赖例外是精确断言，不能扩充为普通 baseline。
- ADR-0005 已确认旧 Python 路径为 repository-internal；`run_graph` Tool Schema/名字和 durable data 仍按稳定身份清单保护。
- ADR-0001 至 ADR-0006 均为 Accepted，且不存在由实现者临场决定的核心契约。
- 未满足时不得开始移动模块。

### Phase 1：抽取 `workflows`

顺序：

1. 先移动纯模型、node、channel、topology 和 validation。
2. 落地 mutable Builder -> frozen Definition -> per-run state，禁止 `_prepare()` 修改 definition。
3. 拆 `engine.py` 为 driver、node runner、retry、resume 和 result。
4. 将 `GraphSpec/from_spec` 的模型可见 DSL 与 Tool operation compiler 迁到 `product.workflows.run_graph`；只保留 capability-agnostic graph kernel。
5. 建立 ContinuationRegistry、InspectionPort 和深不可变 RunSnapshot，分离 presentation/continuation/inspection retention；先迁移 `resume_tasks/get_node_state`，再删除 Pool graph metadata。
6. 以 `ToolExecutionKind` 替换 marker/实例字段扫描，覆盖全部 definition 变换、pipeline gate 与 graph-in-graph policy。
7. 落地五类 workflow/background outcome、协作式 stop、ResumeCapability 与对应 tagged terminal event，移除对 `BgTaskResult` 的核心依赖。
8. 在 `product.workflows` 建立 `WorkflowTaskAdapter`，由 Product composition wiring，分别切换前台 run_graph 与后台 compiled Workflow。
9. 按 ADR-0005 的 repository-internal 策略原子更新仓内调用方并删除 `tasks/bggraph`。

完成条件：

- `workflows` 对 `background_tasks`、`agents`、`automation` import 为零。
- 同一 frozen definition 可并发创建多个 run，run state 不互相污染。
- `run_graph` 可通过统一 Tool dispatch 顺序或并行执行普通 Tool 与 Agent Tool，但 Workflow 内核不 import Tool/Agent 类型。
- ADR-0006 行为矩阵、查询恢复、分类、identity 和 effect-ledger 验收全部通过。
- 前台 `arun`、后台提交、pause/resume/skip、DSL 构图行为测试全部通过。
- 同一 Slice 完成后旧 `bggraph` 路径不存在；`run_graph` Tool contract 行为等价测试通过。

### Phase 2：治理 `background_tasks`

顺序：

1. 将结果存储和 attachment 抽入 `results`。
2. 将 progress、stall 和 turn context 抽入 `monitoring`。
3. 从 `BackgroundTaskPool` 抽出 registry、execution、lifecycle 和 delivery。
4. 接收通用 deferred operation 和 background-owned operation outcome；Workflow adapter 代码归 `product.workflows`，由 composition wiring。
5. 将 Product Agent 的后台任务构造留在 Product composition。
6. 删除 `orchestration.tasks`。

完成条件：

- `BackgroundTaskPool` 仍是唯一调用门面。
- submit/wait/cancel/resubmit/adopt/timeout/close/result spill 行为等价。
- `background_tasks` 不 import Runtime Agent 私有实现。
- 旧 `tasks` 路径不存在。

### Phase 3：建立 `agents` 控制平面

顺序：

1. 迁移 identity、runtime、mailbox、limiter、residency 等已有内聚模块。
2. 建立明确的 messaging、execution、lifecycle 子包边界。
3. 落地 `SpawnTransaction` 状态机，并用普通异常、`CancelledError` 和 commit 中断测试固化所有权转移。
4. 抽取 messaging delivery、completion/TTL supervision 和 fleet costing。
5. 用窄 Port 替换 Role 私有字段探测。
6. 保留薄 `AgentControl` facade，更新所有 Product 调用方。
7. 删除 `environment` 中非 Cron 部分。

完成条件：

- `AgentControl` 不直接读取 Role 私有字段。
- 调用方不能绕过 facade 组合 Spawn 事务。
- Agent 控制面全部现有行为测试通过。
- `agents` 不依赖 `background_tasks` 或 `workflows`。

### Phase 4：抽取 `automation.cron`

顺序：

1. 迁移 expression、task、store、lock 和 scheduler。
2. 定义 Automation-owned `TriggerSink`，删除 service 对 AgentControl、UserMessage 和 DeliveryMode 的依赖。
3. 在 `product.automation` 建立 `AgentTriggerAdapter`，由 Product composition wiring。
4. Product EntryPoint 负责注入路径、store、TriggerSink 和 lifecycle owner。
5. 迁移 Cron 测试并删除 `environment/scheduling`。

完成条件：

- `automation` 对 `agents` import 为零；Agent 触发仅存在于 Product adapter。
- Cron parser、jitter、lock、aging、store 和 service 测试通过。
- TriggerSink fake 可在不构造 AgentControl 时完成全部 Cron service 测试。

### Phase 5：处理 Environment facade 与最终清理

范围：

1. 根据评审结论迁移 `MoteEnv` 与 `AgentEnvironment`。
2. 收窄四个根 `__init__.py` 的公开面。
3. 删除旧目录、旧 import、旧文档术语和临时断言。
4. 更新 `zdocs/ARCHITECTURE.md` 的多 Agent、后台任务和工作流章节。
5. 生成最终依赖图和量化报告。

完成条件：最终验收全部通过，且旧 `environment`、`tasks`、`bggraph` 目录不存在。

### 生命周期关闭顺序

Product composition 注册和关闭共享资源，固定顺序为：

```text
停止接受新 automation trigger
-> 停止 Cron scheduler/periodic loop
-> 阻止新 Spawn 与新 background submission
-> 等待或取消 active background operations
-> 停止 Agent turn scheduler 与 watcher
-> shutdown live Agent runtimes/handles
-> flush task results、residency、cron stores
-> 关闭 telemetry/persistence substrates
```

规则：

- 每阶段尽力关闭全部资源并聚合失败，不能因一个 close 失败跳过后续资源。
- 重复 `aclose()` 幂等；调用方取消不能中断所有权资源的必要清理。
- WorkflowRun 由前台调用者或 background operation owner 关闭，不能同时被两者取消。
- DeferredOperation 是 single-use；Pool 从 submit 成功起独占。cancel/timeout 先协作 `request_stop`，grace period 失败后才强制 cancel/join；所有路径最终在 cancellation isolation 下调用幂等 `aclose()`；resubmit 必须由 factory 创建新 operation。
- Product 使用现有 lifecycle stack 注册资源，不在 Orchestration 引入第二套全局 shutdown manager。

---

## 8. 架构门禁

必须新增 `ztest/architecture/test_orchestration_dependencies.py`，至少覆盖以下规则。

### 8.1 五层方向继续成立

沿用现有全仓层级测试；Orchestration 可以依赖 contracts/kernel/runtime，不得被这些低层反向依赖。

### 8.2 Orchestration 内部禁止边

```text
workflows        -X-> background_tasks, agents, automation
background_tasks -X-> workflows, agents, automation
agents           -X-> background_tasks, workflows, automation
automation       -X-> background_tasks, workflows, agents
```

四个一级能力包直接 import 必须为零。跨能力 adapter 代码只允许位于具体 Product 能力包，实例化/wiring 只允许位于 Product composition；新增任何一级包间边必须经过新的 ADR，而不是扩充白名单。

### 8.3 SCC 检测

AST 扫描绝对 import 和相对 import，解析成完整模块路径，并在两种粒度运行强连通分量检测：

1. 一级能力包。
2. 二级子包，例如 `agents.messaging`、`workflows.execution`。

包含多个节点的 SCC 直接失败。`TYPE_CHECKING` import 仍计入静态设计依赖；需要共享类型时应修正 owner，而不是用类型导入掩盖循环。

### 8.4 禁止旧路径

最终断言以下路径不存在：

```text
orchestration/environment
orchestration/tasks
```

并扫描：

- 普通 import；
- 相对 import；
- `importlib.import_module("literal")`；
- `__import__("literal")`；
- 文档和注册表中的旧 canonical module 字符串。

非静态动态 import 默认禁止，不能用它规避依赖图。

### 8.5 Public surface

四个一级包的 `__init__.py` 只导出稳定门面、稳定请求/结果和必要枚举。以下类型默认不从根导出：

- Store 实现。
- Scheduler/driver 内部类型。
- Registry mutable state。
- Reservation/guard。
- 私有 notification renderer。
- Spec compiler 内部节点。

### 8.6 跨边界私有访问

AST 门禁检查 Orchestration 对 Runtime Agent 的私有属性访问，并禁止以下模式继续存在或换名出现：

```text
role._config
role._context
role._capabilities
role.wiring.services.<internal>
getattr(role, "_<name>")
```

允许调用明确公开方法和注入的窄 Protocol。测试不得用宽泛名字黑名单代替对实际边界的结构检查。

### 8.7 构造与 import 纯度

导入任一 Orchestration 一级包不得：

- 启动 asyncio task、线程、scheduler 或 watcher。
- 打开 lock/store/session 文件。
- 访问用户 home 或执行 Product path discovery。
- 创建进程级 mutable registry。
- 加载 Product Interface 或可选 UI 依赖。

AST 检查明显模块级副作用，隔离进程 smoke test 验证实际 import 行为。

### 8.8 资源与中断门禁

仅验证 import purity 不足以证明生命周期正确。每个阶段还必须有资源泄漏测试：

- `asyncio.all_tasks()` 不遗留 watcher、poll、scheduler、driver task。
- 重复 close 不重复释放 lease、slot、lock 或 store writer。
- `CancelledError` 注入每个 await boundary 后资源计数回到基线。
- 模拟 commit/落盘中断后，重启扫描可以完成或明确拒绝恢复，不静默接受半状态。
- 测试使用唯一 session/run/task ID，避免 singleton 或持久目录相互污染。

---

## 9. 行为验收

### 9.1 Agent 控制平面

- Spawn admission、depth/cost/token cap。
- 每个 Spawn 失败点的 reservation rollback。
- MANAGED 与 EPHEMERAL 生命周期。
- Mailbox turn-atomic delivery、broadcast、channel 和 pending backpressure。
- Residency eviction、rehydration 和容量竞争。
- Completion notification、interrupt、TTL 和 shutdown。
- Fleet cost attribution 与 subtree 统计。
- inert child 不可寻址/调度，supervisor 安装后才能原子 activate；首次快速完成不会丢 completion。

### 9.2 后台任务

- submit ID、metadata、concurrency 和 timeout。
- wait-one、wait-any、wait-all。
- cancel、resubmit、adopt、agent-scoped cancellation。
- terminal delivery、wake、result retirement 和 retrieval。
- 大结果 spill、offset read、attachment 和 compaction 后恢复。
- Stall detection 与关闭时无 orphan coroutine/task。
- Pool 只处理 OperationOutcome；测试对象若返回 WorkflowOutcome 或携带 graph attribute，不会触发任何特殊路径。
- single-use operation 在正常、失败、timeout、pre-start cancel、shutdown 后均进入 CLOSED；resubmit 创建新实例。
- TaskMeta/公开查询模型中不存在 graph_meta、run_state、node_names 或 state_snapshot。

### 9.3 工作流

- Builder build 后 Definition 冻结，Builder 后续修改不影响 Definition；同一 Definition 并发 runs 隔离。
- 构图、fan-out、join、conditional 和 LLM route。
- 编译期 topology/param/schema 校验。
- 节点并发、retry、timeout、recursion limit 和 batch failure。
- pause、resume、skip、resume-from 和 run-state。
- Failed/TimedOut/Cancelled 只在一致 checkpoint 上产生 ResumeCapability；fatal error 不产生；timeout/cancel 先 cancel/join siblings。
- full restart 创建新 Run；node retry budget 与跨终态 resume budget 分属 Workflow 和 Product Registry。
- 前台运行不依赖后台池；后台运行只通过 adapter。
- continuation 可被前台直接恢复或包装为 deferred operation，二者结果一致且不暴露 graph internals。
- structured event 在无 Product renderer 时仍可完整执行。
- ProgressSink durable/observational policy、严格顺序、terminal-before-return、dropped count 和重入拒绝。

### 9.4 Product RunGraph adapter

- GraphSpec binding、predicate、expression、map、fold 和 output contract。
- Tool name/excluded/graph nesting 策略仍经过 live Tool catalog。
- 普通 Tool 与 Agent Tool 都可作为 operation，由同一 Workflow 内核顺序或有界并行执行。
- Agent Tool 的 Spawn cap、permission、hooks 和 observability 继续经过原有 chokepoint；Workflow 不 import AgentControl。
- 模型可见 `run_graph` 名称、参数 Schema 和核心输出行为保持等价。
- Tool JSON Schema golden test 保持稳定。
- `resume_tasks/get_node_state` 只通过 opaque ref、ContinuationRegistry 和 immutable RunSnapshot 工作。
- Snapshot 深冻结并执行 visibility/大小限制；result presentation、ResumeRef 和 inspection retention 互不代替。
- pipeline enable/graph nesting 只读取 ToolDefinition.execution_kind，不扫描 capability instance；rename/prefix/copy/alias/dynamic composition 不丢分类。
- `run_graph` 只有一个顶层 effect-ledger receipt；节点 dispatch 不传 result_id、不产生逐节点 started ledger，但节点外部副作用允许 at-least-once replay，不承诺 exactly-once。
- 通用 Workflow 只暴露 Decision API，不依赖 AskUserQuestion/Approval DTO；前台 Approval/AskUser 可直接交互，后台 compiled Workflow 通过 interaction/decision edge 暂停并释放 worker。
- 模型可直接选路、先调用 AskUserQuestion 后选路，或暂不调用工具并让 Workflow 保持 PAUSED。
- 顶层取消会 join/close 全部节点 task。

### 9.5 Automation/Cron

- Cron expression、next run、timezone/local formatting 和 jitter。
- Store 原子性、mtime reload、process lock 和 stale recurring task。
- Scheduler start/stop、one-shot/recurring 和最大任务数。
- 只调用 fake `TriggerSink` 即可验证触发；Automation 不导入 Agent 类型。
- Product `AgentTriggerAdapter` 单独验证 trigger 到 Agent command 的映射。
- 当前 delivery guarantee 明确为 at-least-once；模拟 accepted-before-receipt crash 时允许重复且不虚假断言 effectively-once。

### 9.6 中断、崩溃与迁移恢复

- Spawn 每个 await 点注入 `CancelledError`，验证 transaction 幂等回滚。
- Background operation、WorkflowRun、Cron scheduler 在 shutdown cancellation 下无 orphan task。
- 对需要 durable 的 store 写入模拟临时文件、rename/fsync 前后中断，验证重启恢复语义。
- 稳定身份迁移可重复执行；迁移中断后重跑不生成第二身份或丢失旧记录。
- repository-internal 旧 import 在同一 Slice 更新后反向断言失败；不存在兼容发布分支。

### 9.7 最终量化指标

| 指标 | 目标 |
| --- | ---: |
| Orchestration 一级能力包 SCC | 0 |
| Orchestration 二级子包 SCC | 0 |
| 低层到 Orchestration 的反向 import | 0 |
| `workflows` 到其他 Orchestration 能力包的 import | 0 |
| `agents` 到其他 Orchestration 能力包的 import | 0 |
| Orchestration 一级能力包之间的直接 import | 0 |
| Orchestration 对 Role 私有实现探测 | 0 |
| 旧路径兼容模块 | 0 |
| 未说明 owner 的一级包 | 0 |
| Spawn 事务入口 | 1 |
| 后台任务生命周期真相源 | 1 |
| 工作流执行内核 | 1 |
| Cron store owner | 1 |

---

## 10. 迁移纪律

1. 每个 Slice 只改变一个 owner 边界，不混入业务算法重写和无关格式化。
2. 移动后同阶段更新全部调用方并删除旧路径，不提交长期 forwarding package。
3. 每个 Slice 同阶段加入架构门禁和行为回归测试。
4. 遇到循环依赖必须调整 owner、拆状态或抽取有业务意义的 Port；禁止局部 import。
5. 不创建 `shared`、`common`、`utils`、`base` 聚合包接收暂时无处安放的代码。
6. 常量、错误和 DTO 跟随其状态真相源，不建立横向 `constants.py`、`types.py` 收纳层；局部且内聚时可保留领域 model 模块。
7. 不因拆文件复制 mutable state、lock、queue、registry 或 lifecycle ownership。
8. 持久化格式迁移先于 Python 路径删除，并有 crash-safe、可重复执行的迁移测试。
9. 所有非测试 import 保持模块顶部。
10. `rg` 仅用于迁移辅助，正式验收必须是 AST、import smoke test 和行为测试。
11. 用户工作树中的并行改动必须保留；实施 Slice 开始前重新生成基线，不能假设本文 LOC 永久不变。
12. 本次 repository-internal 路径迁移不设置兼容期；未来若发布 committed external API，必须另立 ADR。

---

## 11. 已批准结论与唯一待审 ADR

1. 一级能力地图为 `agents`、`background_tasks`、`workflows`、`automation`，四者零直接依赖。
2. `bggraph` 不整体搬迁：通用并行图内核归 `orchestration.workflows`；模型可见 GraphSpec、Tool/Agent operation compiler 和展示归 `product.workflows.run_graph`。
3. Workflow 固定为 mutable Builder -> frozen Definition -> mutable Run；后台池只接收 background-owned outcome。
4. 跨能力 adapter 代码归 `product.workflows`、`product.automation`、`product.agents` 等具体能力，Product composition 只实例化和 wiring。
5. `AgentEnvironment` 暂保留为 `agents.environment_facade`；在旧 BaseEnvironment 契约有正式替代前不机械迁移。
6. `MoteEnv` 的 human input/approval presentation 迁入具体 Product adapter。
7. 旧 Orchestration Python 路径为 repository-internal；仓内 Tool 同 Slice 更新，不设置兼容包。
8. `run_graph` Tool 名称、参数 Schema、核心行为及 durable identity 继续作为稳定契约保护。
9. ADR-0001 至 ADR-0005 已 Accepted；实施不得用临时兼容层、动态 import 或宽 Protocol 改写其决定。
10. Phase 1 首先抽取 Workflow 内核，同时迁移 Product RunGraph DSL，而不是机械更名整个 `bggraph` 目录。

ADR-0006 已 Accepted，固定五类终态、协作式 stop、restart-only capability、SnapshotValueProjector、通用 Decision 边界、三类 retention、ToolExecutionKind 传播、definition/execution 双身份及 at-least-once 副作用边界；“禁止后台人类路由”的方案已被明确否决。Phase 1 门禁已解除。

Phase 0 的稳定身份审计、SCC/import 可执行基线、characterization tests 和迁移责任分配已随实现落地；身份不再由 Python module/qualname 派生，最终依赖指标由 `ztest/architecture/test_orchestration_dependencies.py` 持续验证。

实现状态（2026-07-29）：Phase 0 至 Phase 5 的源码迁移和常规测试已完成。`zdocs/ARCHITECTURE.md` 按维护者要求留待后续整体重写，不属于本次变更。

---

## 12. 完成定义

当且仅当以下条件全部满足，本治理才完成：

- 四个目标能力包及目标依赖方向落地。
- ADR-0006 已 Accepted 且全部迁移验收进入常规测试。
- 当前所有 Orchestration 模块均有唯一 disposition，没有遗留孤儿模块。
- `AgentControl`、`BackgroundTaskPool`、`WorkflowDefinition` 是薄门面且保持唯一业务入口。
- Spawn、任务生命周期、工作流执行和 Cron 各只有一个状态真相源。
- AST 架构门禁、import smoke test 和行为测试进入常规测试集。
- 旧目录、旧 import、动态兼容、临时 baseline 和 re-export 全部删除。
- repository-internal Python 路径已原子迁移；模型可见 Tool 与 durable data 契约保持兼容或完成显式迁移。
- `zdocs/ARCHITECTURE.md` 与源码使用相同的五层、包名和调用关系，不再描述已删除的 `common` 或旧目录。
- 新增 Agent 控制能力、后台任务能力、工作流节点/DSL 能力或自动化触发时，都有不修改无关 owner 的标准接入路径。

最终标准不是完成一次目录重排，而是让 Orchestration 层拥有稳定、可执行的所有权语言：未来功能增长只增加领域能力，不重新积累结构性负债。
