# AGENTS.md — Mote 当前架构与工程硬约束

本文件约束所有在 `/home/longert/mote` 内工作的开发者和 Agent。它描述当前必须保持的架构边界与工程纪律，不是历史说明，也不是未来愿景。

`zdocs/ARCHITECTURE.md` 含有旧目录与旧分层，只能作为历史资料，不能作为当前源码事实。开始任务时必须以本文件、当前源码、`ztest/architecture/` 门禁和与任务直接相关的已确认需求为准；任何文档路径、类型或行为都必须从源码重新核实。

---

## 0. 范围与事实来源

- 工作范围仅限 `mote/` 包及其 `ztest/`、`zdocs/`。不得读取、修改或借用 `mote/` 之外的同名项目文件。
- 当前事实的优先级为：
  1. 用户在当前任务中明确确认的产品决定；
  2. 本文件的架构硬约束；
  3. 当前生产源码和可执行架构门禁；
  4. 当前测试表达的既有行为；
  5. 需求与设计文档。
- 文档与源码冲突时，先报告冲突。源码决定“现在是什么”，已确认需求决定“必须变成什么”；不得擅自用旧文档覆盖当前实现。
- 修改前先检查工作区状态和相关 diff。已有改动默认属于用户；不得覆盖、回滚、格式化或顺手重构无关内容。

---

## 1. 十年零架构债务原则

所有设计以未来十年持续演进而不积累兼容债务、双真相和隐式边界为目标。

- 每个概念只有一个 canonical owner、一个 authoritative type、一个生产装配入口和一条状态真相链。
- 修复必须闭合 contract、owner、composition、lifecycle、persistence、observability 和 tests；不能只在调用点打补丁。
- 不建立 `legacy`、`compat`、临时 re-export、旧名 alias、双写、双读、fallback path 或第二套执行链。确认替代完成后直接删除旧路径。
- 不以 `Any`、`object`、裸 `dict`、字符串 discriminator、反射、`getattr/hasattr` 或 duck typing 掩盖尚未设计的正式边界。
- 不为“也许以后需要”增加 feature flag、抽象层或配置项。只为已经识别的变化轴预留最小、类型化、可验证的扩展面。
- 不在低层放高层概念的空壳，也不把未来能力伪装成已经交付。未接入的扩展点必须有明确 owner、consumer 和验收，否则不进入生产主路径。
- 不用注释解释错误架构。依赖环、owner 错置、生命周期混合或类型丢失必须通过拆包、移动 owner、提取 Port 或调整装配解决。
- 不接受“先兼容、以后清理”。每个合入切片应在自身范围内达到零遗留、零重复入口、零未使用类型和零迁移残渣。

---

## 2. 五层包分层是不可破坏的硬边界

生产依赖只能按下列方向单向流动：

```text
contracts <- kernel <- runtime <- orchestration <- product
```

允许上层 import 下层，禁止下层 import 上层，也禁止通过局部 import、动态 import、注册表副作用或类型注解绕开分层。

### 2.1 `contracts/`

- 只拥有跨边界 DTO、稳定 identity、枚举、错误、事件、配置 contract 和最小 Protocol/Port。
- 必须是纯数据或窄行为契约，不拥有 IO、调度、持久化实现、业务编排或 Product 默认值。
- 不得依赖 `kernel/`、`runtime/`、`orchestration/`、`product/`。
- 跨层调用必须优先在 `contracts/ports/` 定义消费方所需的最小 Protocol，而不是暴露具体实现或巨型 service interface。

### 2.2 `kernel/`

- 只拥有单 Agent、模型无关、IO 无关的执行语义。
- 当前核心边界包括 `kernel/execution/`、`kernel/inference/`、`kernel/commands/`、`kernel/tools/`、`kernel/output/` 和 `kernel/telemetry/`。
- 不拥有多 Agent、Workflow、BackgroundTask、进程、持久化、模型 provider、工具 IO 或 Product 策略。
- 只能通过 Contracts-owned Port 接收外部能力。

### 2.3 `runtime/`

- 拥有可复用运行机制：Role 装配、上下文、模型客户端、工具执行、权限、沙箱、会话、事件、日志、持久化、lease/fencing、artifact、交互 runtime 和恢复原语。
- 不拥有多 Agent/Workflow/BackgroundTask 的产品状态机、调度政策或 Product composition。
- Runtime 提供机制和 Port 实现；治理决定由 Orchestration 作出，可信默认装配由 Product 作出。

### 2.4 `orchestration/`

- 拥有跨运行单元的协调语义：多 Agent、BackgroundTask、Workflow、automation、调度、配额、预算、并发、supervision、placement 和 reconciliation。
- Agent 树治理、lineage、spawn admission、子树预算、级联取消和 worker placement 必须在此层闭合。
- Workflow durable 状态机和 BackgroundTask process-local 状态语义必须在此层闭合。
- 不得 import `product/`，不得依赖具体 CLI、内置工具或 Product 配置类型。

### 2.5 `product/`

- 拥有最终用户产品、Coding Agent、内置 Toolsets、Skills、模型/服务集成、配置加载、可信来源策略、CLI、gateway、presentation 和唯一 composition root。
- 当前入口位于 `product/entrypoints/`，不是旧的 `product/cli/`。
- Product 可以选择和装配下层实现，但不得复制下层状态机或建立平行 factory/control path。

### 2.6 包治理

- `common/` 已删除，禁止重建任何 `common`、`shared`、`utils`、`helpers`、`misc` 或其他无 owner 的泛用垃圾包。
- 新模块必须放入拥有其不变量的 bounded context；不能按“方便 import”或“很多地方会用”决定位置。
- 同一子系统中的 declaration、state、policy、service、store 和 adapter 应按职责拆分，但不得为了文件短小制造无语义薄层。
- 跨 bounded context 共享的是 Contracts-owned contract，不是任意实现类。
- `__init__.py` 只公开稳定公共面；不得靠 eager import 触发隐式注册、加载可选依赖或掩盖错误 owner。
- 包级 registry 必须有唯一 owner、显式生命周期和确定性 identity；不得依赖 import 顺序形成行为。

---

## 3. 导包与依赖规则

- 除 `ztest/` 外，所有 import 必须位于模块顶部。
- 禁止在函数、方法、类体、property、factory 或异常分支内局部 import。
- 可选依赖和平台依赖使用模块顶部 `try/except ImportError`；纯类型依赖使用模块顶部 `TYPE_CHECKING`。
- 如果模块顶部导入产生循环依赖，说明包边界或 owner 错误。必须通过移动类型、拆模块、反转依赖或在 `contracts/ports/` 提取 Protocol 消除，禁止用延迟 import 回避。
- 禁止使用动态字符串 import、`importlib` 或模块 `__getattr__` 作为正常内部依赖机制。确需 plugin/provider discovery 时，必须由 Product-owned 显式 catalog/manifest 驱动，并有稳定 identity 与门禁。
- 禁止跨层 re-export 具体实现来伪造低层 owner。错误必须从定义它的 authoritative package 导入。
- 导入模块不得隐式启动进程、线程、网络、文件扫描、配置执行或重量级可选 backend。构造与 activation 必须分离。

---

## 4. 类型系统与泛型是架构的一部分

- 正式边界必须类型化。`Any` 仅允许出现在真正的外部动态边界，并必须在 adapter 入口立即校验、解码为 canonical type。
- `object` 不能替代未设计的 Protocol；`dict[str, object]` 不能替代已知 shape 的 DTO；字符串不能替代已知状态、identity 或 disposition 的枚举/tagged union。
- 泛型关系必须端到端保留，不能在中间 facade、factory、handle、callback、collection 或 Port 中退化：

  ```text
  definition[OutputT]
      -> builder/request
      -> RunnableAgent[OutputT]
      -> AgentRuntime[OutputT]
      -> ChildAgentHandle[OutputT]
      -> RunOutcome[OutputT]
  ```

- 输入与输出存在关系时必须使用同一个 `TypeVar`/`ParamSpec`/Protocol 表达，禁止用 `Any` 切断关系后靠 cast 恢复。
- 协变、逆变和 invariant 必须按真实读写语义选择；可变容器不能伪装协变。
- 跨进程、持久化和 wire 数据必须使用显式版本化 schema、严格 decoder 和可判别联合；未知版本、未知 tag、缺字段、额外关键字段和错误 primitive 类型必须按协议 fail closed。
- durable domain改格式前必须明确选择直接保留、一次性migration或经用户授权丢弃。migration必须版本化、可审计、幂等、可从partial failure恢复并有旧decoder退出条件；它不是生产双读fallback。未经授权不得静默清空。
- 不允许宽松 `str()/int()/bool()` 强转 durable 或安全状态。
- `cast` 只能说明类型检查器无法推导但运行时不变量已经由同处验证；不能用来掩盖不安全构造或错误 API。
- `# type: ignore` 必须精确到错误码并有不可替代原因；不得用文件级 ignore 或无错误码 ignore 消音架构问题。
- factory、composition、registry 和 callback 的返回类型必须表达真实实现关系；测试 fake 的便利不能迫使生产接口退化为宽类型。
- Product 的 text-only Agent 是显式 specialization，不能把 canonical child/spawn contract 固定为 `str`；运行时 Protocol shape 不能证明泛型实参，禁止用 TypeGuard 伪造该关系。

---

## 5. 稳定面、变化轴与改动面预留

每次架构改动前必须区分：不会轻易变化的稳定面、未来明确会变化的轴，以及仅属于当前实现的细节。

### 5.1 必须稳定的面

- identity 与 owner；
- layer dependency direction；
- canonical state transition 与错误语义；
- durability、delivery、permission 和 effect guarantees；
- 输入/输出 contract 及其泛型关系；
- composition、activation、shutdown 和 recovery lifecycle；
- externally observable event/wire schema。

### 5.2 允许预留的变化轴

- provider/backend/transport；
- storage、lease 或 process executor 实现；
- policy extension 对已有权限的单调收窄；
- scheduler/placement strategy；
- graph topology 或 codec 的版本化演进；
- Product surface 和 presentation adapter。

变化轴必须通过窄 Protocol、不可变 policy input/output、版本化 definition、显式 factory 或 tagged union 表达。扩展不得获得完整 Role、RoleState、Context、环境或控制平面。

### 5.3 禁止的伪预留

- 为未知未来需求传递 `Any`、`**kwargs`、裸 mapping 或任意 callback；
- 在多个层各建一个“以后统一”的实现；
- 用可选字段同时表达互斥生命周期而没有 tagged union；
- 用 feature flag 永久保留新旧路径；
- 用 alias/re-export 假装兼容；
- 允许 extension 扩大权限、预算、深度、工具或信任范围；
- 为不可能发生的内部状态加 silent fallback。

新增变化轴时，必须回答：谁拥有 contract、谁选择实现、谁管理生命周期、identity 如何推进、失败如何恢复、旧版本如何退出、如何测试扩展不会破坏主路径。

---

## 6. 基础设施复用、包内聚与最小服务面

### 6.1 复用现有基础设施，不重复造轮子

- 新增任何 contract、Port、store、codec、registry、lease、fencing、scheduler、queue、event、artifact、process runner、permission、sandbox、lifecycle 或 composition 机制之前，必须先用 `rg --files` 和 `rg` 搜索仓内已有 owner、实现和消费者。
- 如果现有基础设施已经拥有相同的核心不变量、identity、生命周期和失败语义，必须直接复用，或在其 canonical owner 内扩展最小能力；禁止换名重造、复制实现、套一层同义 facade 或建立平行 service。
- “调用不方便”“类型暂时对不上”“另一个目录已经在使用”都不是复制基础设施的理由。应收窄调用方、补齐 canonical contract、保留泛型关系，或在原 owner 中增加最小扩展面。
- 只有在核心不变量、bounded context、生命周期、durability 或安全边界确实不同的情况下，才允许新增机制。实施说明必须逐项写明为什么现有基础设施不能复用，以及新机制不会形成双 owner 或双真相。
- 复用的是拥有不变量的 canonical 能力，不是复制代码、继承具体实现、共享内部可变状态或跨包调用私有方法。
- 不得为了表面统一强行复用语义不同的机制。若两个概念只是字段相似但生命周期或保证不同，应保留不同名称和显式投影，不制造错误抽象。
- 测试 fake、CLI adapter、Product facade、兼容 wrapper 和一次性 migration 不能演变为第二套生产基础设施。
- 引入第三方库前同样先核实仓内能力；新增依赖必须证明必要性、owner、维护边界和退出策略，并获得用户授权。

### 6.2 包必须围绕共同不变量高度内聚

- 一个包或子包只拥有一个明确 bounded context，以及围绕同一组核心不变量共同变化的 contract、policy、state 和实现。
- 应当一起变化、共享同一 identity、生命周期和状态真相源的代码，应放在同一个 canonical owner 包内。
- 变化原因不同的代码必须分离。contract、执行机制、治理 policy、Product composition 和 presentation 不能因为服务同一功能名称就堆进一个包。
- 仅因名称相似、字段相似、代码短小、多个调用者会用或可以减少 import，不能判定属于同一个包。
- 一个包若同时承担多个独立状态机、多个 lifecycle owner 或多个无关变化轴，必须按 bounded context 拆分；不得用巨型 manager、facade 或 `components.py` 隐藏低内聚。
- 拆包必须按 owner 和不变量，不按文件行数。禁止为了“每个文件更短”制造只有转发、别名或单一调用的无语义薄层。
- 包名和模块名必须表达业务或运行语义；禁止新增 `common`、`shared`、`utils`、`helpers`、`misc`、`base_utils` 等无 owner 容器。
- 包内部可以通过具体类型协作；跨包后必须通过该包承诺的公共服务面，不能让外部消费者拼装其内部对象图。
- 私有 state、lock、task、client、registry、store implementation 和 mutable collection 不得因调用方便暴露到包外。

### 6.3 包对外只暴露最小、稳定、类型化的服务面

- 包对外暴露的是稳定业务或运行能力，不是内部数据结构、对象图、存储布局、并发原语或 provider 实现。
- 跨层能力由消费方在 `contracts/ports/` 定义其真正需要的最小 Protocol，由实现方实现并由上层 composition 注入。Protocol 的形状由消费者需求决定，具体类不能反向成为契约。
- 同层跨 bounded context 调用也必须优先经过被调用包的公共 service/Port；不得直接读写另一个包的 registry、store、私有 state 或内部模块。
- 一个 Protocol 或 service 只服务一个明确用例集合，只包含完成这些用例所需的方法。禁止巨型 `Manager`、`Context`、`Services` 或万能 control interface。
- 命令与查询要保持明确语义：命令通过 owner 执行并返回 typed receipt/result，查询返回不可变 snapshot/projection；调用方不得取得可变引用后绕过 owner 修改状态。
- 对外输入输出必须使用 canonical DTO、identity、泛型结果、typed disposition 和 typed error；不得接受或返回 `Any`、裸 dict、内部 ORM/store record、锁、task、client 或具体 backend。
- 服务面必须隐藏存储、调度、进程、网络和 provider 选择，使实现变化不会扩散到消费者；需要暴露的保证应写进 contract，而不是泄漏实现类型。
- `__init__.py` 只导出该包明确承诺的公共面。禁止为缩短 import 路径 re-export 内部类型、错误 owner 的类型或可选 backend；内部消费者从 authoritative module 导入。
- 新增公开方法、事件字段或 Port 能力前，必须证明现有服务面无法表达用例，并检查其是否扩大权限、泄漏生命周期、破坏泛型或锁死未来实现。
- 删除或替换公共面时，在同一迁移切片内迁移所有仓内消费者并删除旧入口，不保留长期双 API。

### 6.4 复用与服务面的评审证据

每个新增基础设施或跨包服务改动必须在实施说明中给出：

1. 搜索过的现有实现及为什么复用、扩展或拒绝复用；
2. 新能力的 canonical owner 和核心不变量；
3. 包内共同变化原因与包外隔离边界；
4. 最小 Protocol/服务方法及每个方法的真实消费者；
5. 被隐藏的内部实现、生命周期和并发细节；
6. 不会产生平行入口、双状态或越层依赖的架构测试。

---

## 7. Role、组件与运行状态

- `runtime/agent/role.py::Role` 是组合式运行编排器；基础注册与恢复 seam 位于 `runtime/agent/base.py::BaseRole`。
- `RoleSchema` 是部署期静态配置，`RoleState` 是可序列化运行状态。配置不得塞入 Role 临时属性，运行时 service/锁/task/client 不得塞入 `RoleState`。
- 组件通过 `RoleComponents`、wiring 和窄 Port 装配；新增组件必须明确 owner、scope、lifecycle、是否 durable、是否 opt-in。
- opt-in 子系统默认不构造；导入和普通 Role 构造不得启动外部资源。
- Runtime 组件不能反向持有完整 Role 以获取未声明能力。通过最小 Port、不可变 services bundle 或显式 capability 注入。
- Session/Residency resume 是向已由 Product 可信 blueprint 构造的 Role 恢复经过验证的历史/状态；磁盘 record 只能选择 state 与已批准 definition identity，不能选择任意 Role class、backend、provider 或可执行 factory，也不得吞掉 definition/config identity mismatch。

---

## 8. Agent、Workflow 与 BackgroundTask 治理

下列产品决策已经确认，是硬约束。

### 8.1 Agent 与进程

- 逻辑 Agent 不等于 OS 进程，不建立递归进程树。
- Orchestration 采用 supervisor/control plane + 有界 worker 进程池；Agent 可放置、卸载和重新放置。
- Agent 治理至少覆盖树深、fan-out、root 总量、子树驻留、执行并发、Token、成本、时限、能力预算、递归委派检测、公平调度、背压和级联取消。
- logical identity cap、resident incarnation cap 和 concurrent turn cap 是三个不同维度，禁止复用含义模糊的单一 `max_agents`。
- turn admission 必须原子 acquire/release；不能把 `has_capacity()` 和无条件 `guard()` 分开。
- Agent lineage 必须跨 supervisor 进程重启持久恢复，至少绑定 agent/root/parent/path/nickname/definition/incarnation/lifecycle/placement/budget identity。
- Spawn 使用稳定 request identity 与可恢复 durable 状态机；logical identity、parent/path/nickname 和预算 reservation 必须在 worker 启动前提交。进程内 rollback closure 不能充当崩溃原子性，重复 request 不得创建第二 child。
- logical Agent identity 不可复用；可回收 path/nickname 索引必须绑定 lineage revision/tombstone，防止旧 delivery、lease、result 或 subtree snapshot 发生 ABA。
- logical Agent 进入 terminal 后立即且严格一次释放 active logical cap，但 AgentId 永不复用。释放容量不等于删除身份事实；tombstone 只有在 Product retention 到期且 delivery、effect、pin、legal hold 全部结算后，才能由当前 fenced owner purge。
- worker 崩溃不能删除逻辑 Agent；旧 incarnation 失去 fence 后不得提交状态、结果或 delivery ack。
- eviction、worker loss、logical terminate/tombstone 与 retention purge 是不同 transition；普通 release 不得直接删除 logical identity 或 durable 证据。
- logical、resident、turn cap 分别定义 scope、receipt 和释放事实；fan-out/root/subtree counter 与 spawn admission 原子。Token、成本、深度和能力预算使用原子 reservation/幂等 settlement，extension 只能单调收窄父级授权。
- 容量耗尽必须返回 typed queued/backpressured/rejected disposition并进入有界、公平、可恢复调度；禁止用无限 parked queue 或“永不失败”文案掩盖背压。
- Agent durable turn scheduler 使用分层 weighted deficit round-robin。当前 Agent governance 没有独立 tenant identity，`tenant == root governance owner`；root 间WDRR，持续积压的兄弟subtree使用第二级WDRR。eligible turn cost固定为1，不引入Token预测成本；root weight默认1且为Product schema约束的有界正整数，Runtime/extension不能提高。持续有容量时，每个eligible root/subtree必须在有限轮次内获得claim。
- priority只决定同一root/subtree内顺序，使用有界enum/range；同priority按durable enqueue sequence稳定FIFO。单root priority flood不得越过其他root，单subtree不得饿死兄弟subtree。
- deadline只作用于未claim item，并与cancel/claim以expected revision CAS竞争唯一终态；已claim超时由turn execution settlement处理。capacity admission必须发生在durable accept前；queue-full返回typed backpressure/rejection且不写accepted，已accepted item不得驱逐。
- turn claim必须绑定queue revision、scheduler fence和R2.16 execution permit；scan不等于执行权，stale fence不得claim、ack、settle或retry。retry必须有`next_eligible_at`、有界backoff/attempt与terminal disposition，poison/未到期item不得阻塞后续eligible工作。
- root取消、terminal或配置删除必须逐项结算已accepted请求。weight热更新使用新config generation，只影响下一次尚未claim的调度决定，不重写历史acceptance、enqueue sequence或已有claim。新增Agent tenant层必须版本化为tenant→root→subtree调度，不能原地改变root含义。

### 8.2 Workflow

- Workflow 是跨进程 durable execution 单元，必须支持进程崩溃、Session resume 和 Residency eviction 后恢复。
- 恢复版本化 definition、run state、checkpoint 和 pending frontier，不恢复 coroutine、closure 或进程对象。
- 同一 run 同时只能有一个 fenced execution owner。
- 外部副作用必须先有 durable intent，再执行，再以 receipt/settlement 对账；未知结果不得盲目重放。
- Workflow 状态机由 `orchestration/workflows/` 拥有，不建立第二套 Agent execution engine。

### 8.3 BackgroundTask

- 每个逻辑 Agent/Role 独立拥有一个 canonical `BackgroundTaskPool`；task registry、task sequence、输出、进度、通知、wake callback 和 cleanup 不得跨 Agent 共享。
- BackgroundTask 是 process-local 临时并发，不承诺跨进程恢复；跨进程工作必须进入 WorkflowRun。
- Agent Swarm 采用集中治理、分散 ownership：supervisor 可以提供进程级/树级并发、资源、预算、公平性、背压和级联取消 admission，但不得集中拥有各 Agent 的 task、result、notification 或 mutable pool state。
- 进程级共享能力只能是窄的 typed admission/permit Port；permit owner 负责全局资源治理，Agent-owned pool 负责 task lifecycle，两者不得形成双 task registry 或双状态机。
- 存在未结算 BackgroundTask 的 Agent 必须 pin residency，不得 eviction；任务全部结算后才允许卸载。不得为了 eviction 把 pool 提升为进程 singleton 或建立 rebind registry。
- BackgroundTask lifecycle gate 由 Agent incarnation/generation 拥有，至少包含 `ACTIVE -> DRAINING -> CLOSED`。submit admission/work-pin acquire 与 begin_eviction/begin_release 必须在同一同步原语和 generation 下原子互斥；单次 `has_pending()` snapshot 不能作为 eviction 安全保证。
- task 返回 local accepted receipt 前必须取得 pin；pin 仅在 operation、permit、output、terminal result/notification 和 resource retirement 全部 settlement 后释放。DRAINING/CLOSED 拒绝新 submit；stale incarnation/generation 不能提交、取消、通知或 cleanup。
- Agent release 必须结算或取消自己的 pool，等待 operation/output/notification cleanup 完成，且不得影响其他 Agent。
- release 使用有界 cleanup 和 typed result，至少区分 settled、draining timeout、cleanup failed、owner/incarnation lost。失败或超时时保持 DRAINING/pin，不删除 owner identity或重新开放 submit；强制终止 worker是 supervisor policy，不由 pool自行决定。
- root/subtree cancellation 由 supervisor 向目标 Agent scope 发出 typed 级联取消命令，各 pool 在自身 owner 内执行并返回 typed settlement；禁止 supervisor 直接修改 pool 内部 map/task。
- `TaskId` 只在所属 Agent pool 内唯一；跨 Agent 查询、取消、结果引用或通知必须同时携带稳定 Agent identity，不得把裸 task sequence 当作进程级全局 identity。
- 跨 Pool boundary 的 task reference 必须绑定 process instance、Agent identity、incarnation/generation 和 local TaskId；attempt-scoped mutation再绑定 AttemptId。owner/generation mismatch fail closed；该引用不表示 durable task identity。
- 模型和同 Pool 工具调用继续只使用稳定 TaskId；Agent/process/incarnation/AttemptId 由已绑定的工具 capability/runtime context 自动补齐并校验，禁止把内部 ownership key 转嫁给模型手工维护。
- 纯 process-local retry 复用模型可见的 TaskId，每次 submit/resubmit 创建所属 Pool 内单调 AttemptId。旧 attempt 失去状态、输出、progress、result pointer 和 notification 提交权；query/cancel 默认作用于 active/latest attempt，历史只暴露 typed immutable settlement，terminal notification 以 AgentId + TaskId + AttemptId 幂等。
- resubmit 只允许同 Agent、同 Pool、同进程；进程终止后旧 TaskId/AttemptId 失效，新进程不得接管。不得用 resubmit 承载 Workflow definition、continuation、checkpoint、resume 或 durable effect reconciliation。
- submit receipt 只能表达当前 incarnation 的 typed local acceptance，不能表达 durable `ACCEPTED`。worker/incarnation loss 后旧引用返回 owner-gone/incarnation-lost，不自动重放 operation；已生成 terminal result 必须在 pin 释放前进入 canonical fact，未生成或未提交结果只能结算为明确 lost/unknown。
- subtree cancellation 使用 fenced stable subtree snapshot 和 cancellation epoch，并与 spawn admission 原子协调；各 Agent pool在自身 owner内幂等结算，supervisor 只聚合逐 Agent typed result，不读取或修改 pool task map。
- Workflow definition、continuation、checkpoint、resume 和 durable run state 不得放入 BackgroundTaskPool；需要跨进程恢复或 residency eviction 后继续的工作必须在提交前进入 WorkflowRun。

---

## 9. Durable state、消息与副作用

- rollout、checkpoint、lineage、lease、ledger、mailbox、artifact metadata 等 durable 数据必须有唯一真相源、版本化 envelope、严格 decoder、稳定 identity、revision/CAS 和明确迁移边界。
- 写入顺序必须与公开承诺一致。声称 durable 的 `started`/`accepted`/`committed` 在 fsync/transaction commit 失败时必须 fail closed，不能更新内存后继续外部动作。
- 所有跨进程 owner 使用 lease + monotonic fencing token；旧 owner 失去 lease 后不得 refresh、release、commit、ack、delete 或覆盖新 generation。
- recovery、rehydration、delivery、eviction、capacity ownership 和 runtime map 必须共享 incarnation/generation 状态机，禁止各自检查后修改。
- canonical facts 必须 durable。只有完全依赖 durable canonical state 可确定性重建，且重建无 LLM、无费用、无时变外部调用、无副作用的信息才允许 best effort。
- best-effort signal 不得是唯一唤醒或推进机制；必须存在 durable scan/reconcile 重新发现工作。
- durable `ACCEPTED` 只表示 intent 已持久提交，不表示目标已处理。进程内 queue/park 不得返回 durable accepted。
- Agent delivery 必须有 durable intent/claim/process/ack/dead-letter 状态机；claim 和 ack 绑定目标 logical identity、lifecycle generation 与 fence，Mailbox enqueue 只是本地投影，broadcast/subtree 对每个目标分别结算。
- durable delivery payload 使用 domain-owned versioned tagged union 和严格 decoder；禁止持久化 Python 对象、callback、Role reference 或裸 dict。大 payload 复用 canonical artifact reference，并把 artifact retention 与 delivery lifecycle 绑定。
- LLM 输出、用户输入、审批、权限、预算、lineage、terminal result、任务依赖和 effect receipt 不能以“可以再算”为由降级。
- 文件替换遵循 write/flush/fsync/atomic replace/parent fsync 的协议；损坏中间记录与允许的尾部 torn write 必须区分。
- 持久 deadline、occurrence、expiry 与 retention boundary 使用 timezone-aware absolute instant 和版本化 clock identity；进程内 elapsed timeout/backoff 使用 monotonic clock。不得持久化 monotonic 值或用 wall clock 计算进程内耗时；restart、NTP 回拨/跃进与 DST fold/gap 必须有明确 fail-closed/typed 语义和 deterministic fake-clock 测试。
- Artifact GC 必须基于 committed typed ownership edge、完整 reachability、retention class 与 revisioned pin generation；Session、Workflow、BackgroundTask terminal pointer、tool/model result、FileOps snapshot、stage/publication 和 legal hold 都必须纳入闭包。minimum age、目录、mtime 或 collector 扫描私有 map 不能证明 unreachable，旧 collector 不得删除已被新 generation re-pin 的 digest。
- cleanup/delete 必须先取得 fenced deletion claim，并在每个不可逆阶段复核 canonical lifecycle、lease、pin 与 hold facts；不得用 mtime、stamp 或“当前进程未见活跃对象”证明可删。TTL、用户删除、安全清除、legal hold 与测试临时数据是不同 typed command、authority、receipt 和审计语义。

---

## 10. 工具、权限与外部进程

- 内置工具位于 `product/toolsets/builtin/`；工具执行机制位于 `runtime/tools/`；工具 contract 位于 `contracts/tool/` 和 `contracts/ports/tool/`。
- 工具只能获得显式声明并由 Role 白名单发布的最小 capability；不得获得整个 Role、RoleState、memory、env、Context 或 AgentControl。
- 文件修改必须经过 canonical FileOps/文件变更工具路径，执行读后写校验、路径/symlink 校验和 before-image snapshot；不得建立旁路写盘。
- 命令执行必须经过 classifier、permission、approval 与 sandbox policy；deny/ask 不得因 fallback、hook、helper、MCP 或内部 facade 被绕过。
- 固定内部 argv 与用户可控命令是两个不同 typed runner，禁止用 `shell` 布尔开关混合信任边界。
- interactive/daemon process 使用独立 start/health/stop lifecycle；不得用一次性 runner 或 trust-mode bool 伪装。process receipt 必须区分 deny、spawn failure、exit、timeout、signal 和 output reference。
- `api_key_helper` 只允许 USER/MANAGED 来源，使用结构化 argv，禁止 `shell=True`；配置解析与 secret resolution 分阶段，secret stdout 永不记录。
- ToolResult 和媒体使用 canonical typed contract；大输出走统一持久化/引用机制，不得自行截断或发明第二种 artifact identity。
- 工具副作用、definition、permission target 和审计 identity 必须一致；远端成功后不得吞掉本地拒绝或伪造失败前未发生副作用。
- 每个 effect 的 definition generation、caller Agent/incarnation、turn/run、canonical arguments digest、permission targets、classification、logical EffectId 与 attempt ordinal必须贯穿approval、sandbox、durable intent、execute、receipt、audit和terminal settlement。重试保留logical identity；definition/generation/arguments改变必须重新决策。
- control Hook 在effect intent和外部动作前运行，只能单调收窄权限、sandbox、预算、工具和网络；修改arguments后重新classification/permission/approval。timeout、crash、malformed或unknown decision fail closed；observation Hook可best effort但不能修改authoritative result。
- 外部动作已发生而本地receipt/artifact/audit/terminal commit失败时必须进入typed IN_DOUBT并保留provider/process receipt，禁止伪装未执行或盲重试。
- ToolExecutor是唯一Tool执行chokepoint；外部只获得immutable binding snapshot与typed command/query/receipt，不得取得tool instance、live catalog或pipeline stage绕过permission/effect链。

---

## 11. Prompt、上下文与缓存

- `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 位于 `kernel/inference/prompts.py`。边界上方是稳定可缓存前缀，禁止放 placeholder、cwd、时间、git 状态、memory、token、bg/lsp 通知等易变内容。
- per-turn 易变信息通过 `runtime/context/turn_context/` 的类型化 source 注入 user prompt 的 system reminder，不写入静态 system prompt，也不伪装成 durable conversation history。
- 新 context source 必须有稳定 name、priority、typed dependencies、明确 suppression 和确定性 render；不得持有完整 Role。
- 压缩、摘要和工具结果折叠必须保留 durable truth 与可重建边界。只有工具显式声明并可从 canonical state 重建的结果才允许折叠。
- prompt template、tool definition、model request 和 cache identity 必须绑定影响语义的内容；不得依赖 `inspect.getsource`、对象地址或 import 顺序生成 durable identity。
- cache identity 绑定最终 system prefix、tool/command definition、model/provider capability、output schema 和 policy generation；同一 turn 固定一个 generation，reload 后旧 turn 使用旧 snapshot，新 turn 使用新代。
- compaction summary 只是 derived context，不能替代用户输入、Tool/LLM output、approval、budget、lineage、terminal result、dependency 或 effect receipt。只有从 canonical state 无需 LLM、费用、时变调用或副作用即可确定性重建的结果允许折叠，并绑定 source revision/generation。
- prompt 措辞不是 control plane，不能承担 durability、permission 或 cleanup 保证。prompt/cache/summary/telemetry/dead-letter/exception/artifact preview 均执行 secret redaction，但不得改写 canonical audit fact。

---

## 12. 事件、日志与可观测性

- 跨边界事件 DTO 位于 `contracts/events/`；Runtime event/telemetry 实现位于 `runtime/events/`；Kernel 只通过 `kernel/telemetry/` 的注入 seam 发观察事件。
- control plane 与 observation plane 分离。日志、progress 或 telemetry 不得反向驱动 authoritative 状态。
- typed event 进入内部链路后不得退化为 `object`、裸 dict 或字符串；动态编码只发生在外部 wire adapter。
- observation subscription 必须保持 `EventT` 从 emitter 到 handler；异构 erasure 只能封装在 Runtime owner 私有层并由 typed binding 验证，不能建立全仓 ObservationEvent union 或用 TypeGuard 猜回类型。
- durable subscriber 必须有 stable subscription identity、cursor/checkpoint、EffectId 和 ack 协议；读取 committed fact 后先 commit reducer/effect receipt 再 ack。gap/wrong generation fail closed，poison 进入 typed dead-letter/quarantine，wake 丢失由 scan 恢复。
- 关键类优先使用 `@log_class`；热路径和平凡 accessor 必须排除。不得在普通 method body 随意增加 inline `logger.*`。
- retry decorator 的 `after_log` 等声明式配置可保留。
- secret、凭据、helper stdout、授权 token、完整敏感 payload 不得进入日志、异常、trace、provenance 或测试快照。
- best-effort telemetry 可以丢失，但审计、权限、预算、effect 和 durable settlement 事件必须来自 authoritative facts 并可对账。

---

## 13. Composition、配置与生命周期

- 每个应用/会话/共享 daemon 只有一个 canonical Product composition root；CLI、gateway、ACP/AG-UI 和测试不能各自复制 Role/AgentControl/Runtime factory。
- composition 必须显式声明 implementation、required Port、scope、lifecycle owner 和 activation 顺序。
- composition 开工前必须形成 application/process/session/Agent/incarnation/turn scope matrix，列明 construct/activate/shutdown owner、共享与继承边界、durability 和 required Ports；不能凭“共享服务”或“每 Session”命名决定 singleton。
- construct 必须纯净；外部资源在显式 start/activate 阶段建立，在 stop/release 阶段按逆序结算。
- canonical Product factory 不得内嵌 `asyncio.run()`；同步入口只能在最外层建立一次 event-loop boundary。activation 中途失败必须按已完成阶段逆序结算，不得发布半激活 Application。
- application-scoped、process-scoped、session-scoped、Agent-scoped、turn-scoped 对象不得混用 singleton 或存入错误状态对象。
- lifecycle lease 必须携带 resource identity、scope、generation、holder 和 typed acquire/transfer/release receipt；borrower 没有 close 权，旧 generation holder 不能释放新 holder 资源。
- 配置来源的 trust 必须绑定 canonical source/path/ownership/content identity，不得只相信调用者提供的 source enum。
- 配置按 declaration、source/provenance、parse、secret resolution、validated activation spec 分阶段；Contracts 不拥有 Product 默认值或信任策略，Runtime 不接收 Product root config、不得自行重读源文件，secret stdout 不进入日志。
- checkout 中的 Agent、Skill、Hook、MCP 和其他扩展不得因被发现就自动获得模型注入、进程执行、网络连接或工具能力；必须经过统一 provenance/trust/approval gate。
- hot reload 只能在既有批准与 identity 范围内原子 generation swap；能力、来源或内容 identity 改变必须重新决策。
- generation swap 前必须完整构造和验证 candidate；旧 holder 使用旧代直至 drain，新 session/turn 取得新代，同一 turn 不得混代。失败保持完整旧代，禁止原地修改共享 registry/config。
- cleanup、GC、scheduler 和 shared service 在多进程下必须使用 fenced ownership；cleanup 只消费 canonical lifecycle/pin/retention snapshot 与 fenced deletion claim，不得信任 mtime/stamp，也不得删除其他进程仍活跃、被 pin 或受 legal hold 的状态。

---

## 14. 错误、安全与边界校验

- 只在真实系统边界校验：用户输入、配置、磁盘 durable data、wire、外部 API、plugin/extension、进程与权限边界。
- 内部不变量破坏应明确失败，不能 silent fallback、吞异常或降级为空状态。
- 错误由拥有该语义的层定义，并以 typed code/context 跨边界；不得从错误层 re-export 或靠字符串匹配控制流程。
- error definition、provider/transport normalization、retry/recovery classification 和 human presentation 分属各自 owner；error contract 不携带 Product 英文文案。持久化/wire error 使用版本化 `namespace + code + typed context`，迁移后删除旧 decoder，不长期双读。
- 安全相关路径默认 fail closed：权限、trust、认证、fencing、durable identity、effect ownership、unknown schema/version。
- 路径必须 canonicalize 并防 traversal、symlink replacement 和 TOCTOU；命令必须防 shell/argv 注入；外部 URL/credential 必须经过 egress 与 secret policy。
- destructive 或不可逆操作在目标不明确时必须先询问；禁止 broad recursive delete、`reset --hard`、force push 或覆盖用户工作。

---

## 15. 测试与架构门禁

- 测试统一位于 `ztest/<subsystem>/`，不新建 `tests/`。
- 从 `/home/longert/mote` 运行测试时使用：

  ```bash
  python -B -m pytest ztest/<subsystem> -q --tb=short -p no:cacheprovider
  ```

- 改动某子系统至少运行：该子系统测试、直接消费者测试、相关 `ztest/architecture/` 门禁。
- 每个切片的测试范围从consumer matrix生成，至少覆盖owner、全部直接生产consumer、entrypoint/composition、相关architecture gate、Pyright authoritative package及反向typed consumers，以及旧module/symbol/alias不可用的负向检查。
- 分层、import、composition、public API 或 owner 变化时，至少运行：

  ```bash
  python -B -m pytest ztest/architecture -q --tb=short -p no:cacheprovider
  ```

- durable/concurrent 代码必须包含确定性 fault injection：写入各阶段、崩溃重启、重复请求、并发 owner、lease 丢失、ABA、取消和异常释放。
- fault injection必须绑定编号化commit/state transition，并用barrier、controlled clock或fault injector协调，禁止依赖sleep/概率race；每点同时断言durable state、公开receipt、effect次数、reconciler决定和stale owner拒绝。
- 泛型/Protocol 改动必须运行 Pyright，并通过测试证明运行时 decoder/adapter 与静态 contract 一致。
- Pyright范围覆盖authoritative package和全部direct typed consumers，报告版本/命令/error count；禁止新增文件级ignore、无精确错误码ignore或为过检引入`Any`/掩盖关系的cast。
- PTY、terminal、kernel 等 loop-bound 测试的多次调用必须位于同一个 `asyncio.run`，每个测试使用唯一 session id 并 cleanup。
- 不以“现有测试通过”证明架构正确。测试未覆盖已确认不变量时必须补门禁。
- 规模测试必须从已确认cap contract推导；`1024 logical Agents`等数字只是压力样例，不是隐含上限。确定性功能测试与性能benchmark分离，不以CI时间波动证明架构正确。
- 不修改测试来迁就错误实现；只有产品 contract 已明确改变时才同步更新测试，并删除旧行为断言。
- 最终报告必须列出实际执行的命令、通过/失败数量、预存失败证据和未运行范围，禁止只说“应该通过”。
- 报告分别列collected/executed/pass/fail/skip/xfail/collection error；collection error计为未执行。预存失败使用唯一baseline owner，不在实施文档复制动态失败清单。

---

## 16. 改动工作流

### 16.1 审核或诊断

- 用户只要求审核、核实或讨论时，只读检查并提交证据，不修改代码或文档。
- 区分事实、风险、产品决定和实施建议；未经确认的二选一语义不得由实现者代选。

### 16.2 实施

1. 从当前源码重建真实调用链、owner、composition 和状态真相源。
2. 明确稳定面、变化轴、泛型关系、lifecycle、durability 和错误语义。
3. 先固定或收窄 Contracts，再实现 owner，最后接通 Product composition。
4. 使用可验证的垂直切片；每个切片同时闭合正常、失败、恢复和清理路径。
5. 删除被替代入口、alias、反射 seam、宽类型和未使用代码。
6. 运行直接测试和架构门禁，检查 diff 未夹带无关改动。

### 16.3 改动范围

- 只做用户要求及为闭合该要求不可缺少的改动。
- 不顺手格式化、改名、补 docstring、增加配置化或重构邻近代码。
- 但“最小改动”不等于局部补丁：如果 contract、owner、composition 或 cleanup 不闭合，必须扩大到必要边界并明确说明原因。
- 文件删除、依赖安装/移除、CI 修改、branch 操作、force push、PR/外部消息等危险或外部动作，除非用户明确授权，否则先确认。

---

## 17. 当前快速定位

| 目标 | 当前目录 |
| --- | --- |
| CLI / gateway 入口 | `product/entrypoints/` |
| Product composition | `product/composition/`、`product/entrypoints/cli/bootstrap.py` |
| Role / wiring / components | `runtime/agent/` |
| Agent 执行图 | `kernel/execution/` |
| 模型无关推理与 prompt | `kernel/inference/` |
| Command channel / XML / native | `kernel/commands/` |
| 模型 runtime 与 provider | `runtime/models/`、`product/models/` |
| 工具 contract / 执行 / 内置工具 | `contracts/tool/`、`runtime/tools/`、`product/toolsets/builtin/` |
| 权限与沙箱 | `runtime/tools/permission/`、`runtime/sandbox/` |
| 上下文与 turn context | `runtime/context/` |
| Session / rollout / replay | `runtime/session/` |
| Event / telemetry | `contracts/events/`、`runtime/events/`、`kernel/telemetry/` |
| 多 Agent / residency / scheduling | `orchestration/agents/` |
| BackgroundTask | `orchestration/background_tasks/` |
| Workflow | `orchestration/workflows/`、`product/workflows/` |
| Automation / Cron | `orchestration/automation/`、`product/automation/` |
| 配置 contract / runtime / Product loader | `contracts/config/`、`runtime/config/`、`product/config/` |
| Artifact / FileOps | `runtime/artifacts/`、`runtime/fileops/` |
| 跨层 Port | `contracts/ports/` |
| 架构门禁 | `ztest/architecture/` |

路径和类型仍须在每次任务开始时用 `rg --files`、`rg` 从当前源码核实；本表是导航，不是绕过事实核验的许可。
