# Mote 全包 Inventory 与高风险服务边界债务审计

状态：已按评审意见修订，待复审；仅作为候选证据，不是实施规格或第二份权威 backlog  
审计日期：2026-07-31  
范围：`contracts/`、`kernel/`、`runtime/`、`orchestration/`、`product/` 的全部一级、二级生产包及一级生产模块  
目标：面向十年零债务的模块化单体，保证未来可按明确边界演进为独立服务，但不提前制造分布式系统

## 1. 审计结论与使用边界

本轮在审计时枚举了 196 个一级、二级目录；其中 `kernel/flow` 及两个子目录只有未跟踪缓存，审计后已删除，当前为 193 个。矩阵为全包 inventory；对高风险类型反查了生产消费者、composition、持久化和运行路径，但不能据此宣称每个标记“保留”的包都已完成同等深度的服务边界证明。

本文不可直接作为实施需求。每个 `SB` 条目必须先映射到唯一债务总账，并按“当前事实、违反的不变量、产品决定、候选实现、验收”收敛；具体类名、Port 名和拆分方式默认只是候选设计，除非已由产品决定或完整调用链证明。

结论不是“所有子包都应成为微服务”。正确目标是：

> 每个 bounded context 有唯一 canonical owner；包内围绕共同不变量和共同变化原因高度内聚；包外只能通过最小、稳定、类型化服务面访问；相同基础设施复用 canonical owner；是否跨进程部署由未来明确的扩缩容、故障域、安全域或组织边界决定。

当前五层依赖方向总体成立，静态扫描未发现 Contracts/Kernel 向上层的真实生产 import，Product 一级/二级包也没有依赖环。但高风险抽样证明存量实现尚未达到上述目标，主要问题是：

1. Workflow、Agent delivery/lineage 与部分 durable execution 仍由进程内对象图承载，却暴露了跨恢复或 never-loss 外观。
2. Runtime 和 Product 存在巨型 service locator、万能 facade、重复的 per-session hosting 构造和隐式有状态 owner；完整 Application composition 已集中在 `build_engine`，不再误判为多个 Application root。
3. 多个包根 `__init__.py` 暴露 store、lock、registry、builder、backend 等内部对象，允许消费者绕过 owner。
4. 正式跨包边界仍大量使用 `Any`、`object`、裸 mapping、反射和未闭合泛型。
5. durable、ledger、persistence 与 config 已证明存在 guarantee/owner 混合或按技术类别聚合；file watching 是 declaration 与 Product reload policy 错置而非两套 mechanism；通用 artifact projection 管线保持内聚，但 Session replay/live read model 属于独立 Session owner。
6. 存在跨 bounded-context 导入 `_private` 符号、生产局部 import 和 optional backend eager import。
7. 现有债务总账覆盖了许多具体位置，但没有系统性执行逐包内聚、最小服务面和复用优先验收。

## 2. 判定规则

### 2.1 计入问题

至少满足一项：

- 同一状态、identity 或副作用存在多个 owner；
- 一个包承担多个独立生命周期或变化原因；
- 包外消费者可直接修改 registry/store/state 或依赖 lock/task/client/backend；
- 正式 service/Port 暴露内部对象图或用 `Any` 截断泛型关系；
- 已有基础设施可复用但另建平行实现；
- 公开 durability、delivery、permission 或 recovery 保证与实现不一致；
- composition、activation、shutdown 或 recovery 有多个生产入口；
- 跨包依赖私有符号，或通过反射/局部 import 绕过边界。

### 2.2 不机械计入

- 外部 JSON、第三方 SDK 和 wire adapter 内经过边界验证的局部动态值；
- 语义、生命周期或保证确实不同的同名/相似类型；
- 完全薄委托 canonical owner、没有状态和行为分叉的入口；
- 明确的 SDK/opt-in/future Port，只要 owner、contract 和生命周期清晰；
- 仅仅“当前没有独立部署”或“包内文件较多”。

### 2.3 严重度

- `P0`：真相源、跨进程恢复、安全、副作用或公开保证错误，实施其他边界前必须先处理。
- `P1`：canonical owner、服务面、composition 或泛型主链破坏，会持续放大改动面。
- `P2`：包内聚、公共面或内部耦合问题，应在相关子系统下一次变更时闭合。

### 2.4 证据与方案的边界

- “当前事实”和源码证据可以进入债务评审。
- “违反的不变量”必须引用 `AGENTS.md` 或已确认产品决定。
- “候选整改”不等于批准新增 service/Port/store/factory。实施前必须补齐 `AGENTS.md` 6.4 的复用证据、真实消费者、最小方法、scope、lifecycle、failure/recovery 和被删除旧 owner。
- 三个独立不变量不自动意味着三个独立 service object；公共面数量由调用链与共同变化原因决定。
- `P2` 矩阵中的“收窄/保留方向”是复审线索，不是已经证明的强制重构。

### 2.5 可复现 inventory

从仓库 `/home/longert/mote` 执行：

```bash
find contracts kernel runtime orchestration product \
  -mindepth 1 -maxdepth 2 -type d -not -name '__pycache__' | sort
```

审计纳入五层下一级、二级目录；排除 `__pycache__`。assets、catalog、语言配置等无 `.py` 目录仍作为合法资源包 inventory，不因无源码自动判债。审计时只有缓存且无受管文件的三个 `kernel/flow*` 目录已删除。矩阵逐项引用当前 193 个目录，并保留对这三个已删除目录的历史说明。

## 3. P0：真相源与进程边界

### SB0.1 Workflow 仍是进程内对象图，不是 durable service

证据：

- `orchestration/workflows/definition.py:73-85,114-187` 将 graph、checkpoint、run state 和完整执行 state 保存为 `Any`/内存对象；
- 同文件 `266-283` 通过 builder 反射冻结定义；run id 为运行时 UUID，owner 只有进程内 `_executing`；
- `product/workflows/continuation_registry.py:18-46` 另建内存 continuation registry，读取即 pop；
- `product/agents/background_tasks.py:74-94,152-170` 又从 graph metadata 构造 WorkflowDefinition，形成第二种 identity/恢复路径。

问题：Orchestration 与 Product 同时拥有 Workflow continuation 状态；没有版本化 checkpoint store、lease/fencing、严格 decoder 和唯一 run service。

稳定需求：

- `orchestration/workflows/` 保持唯一状态机 owner；
- 对外只暴露版本化 WorkflowDefinition/WorkflowRun identity 与满足真实消费者的 typed command/query；
- graph、coroutine、executor 留在包内；持久化、lease 和 effect runner 由 Runtime Port 注入；
- 删除 Product continuation 状态 owner，Product 只保留 tool/surface/composition adapter。

候选实现：可以在现有 Workflow owner 内形成最小 run service，也可以复用并扩展现有 control seam；`WorkflowRunService` 只是候选名称。定稿前必须列出现有 Workflow/continuation/background adapter 的全部消费者、方法矩阵、scope、activation/shutdown/recovery 和被删除入口。

### SB0.2 Agent tree 的三种容量、lineage 与 delivery 没有闭合

证据：

- `orchestration/agents/control.py:98-127,158-162` 用 `max_agents` 同时影响 turn limiter 和 residency；
- 同文件 `389-397` spawn 调用 `reserve_spawn_slot(None)`；当前实现根本没有独立 logical identity 总量 cap，而不是偶发绕过一项已经闭合的 cap；
- `orchestration/agents/identity/registry.py:62-97` lineage/path/nickname/count 仅是进程内 dict/set/counter；
- `orchestration/agents/messaging/pending.py:67-92,143-147` 是纯内存 queue，release 可 drop；
- `orchestration/agents/control.py:173-180` 却把 delivery 描述为 never fails/never drops。

问题：logical identity、resident incarnation、concurrent turn 三种不变量混合；lineage 无跨进程真相；进程内 parked delivery 冒充 durable accepted。

稳定需求：

- logical identity、resident incarnation、concurrent turn 必须成为三个独立、类型化不变量，分别定义计数对象、identity、原子 acquire/release、持久化和恢复语义；turn acquire/release 必须原子化；
- 建立 Orchestration-owned durable lineage capability，外部只见 immutable snapshot 和 typed command；
- canonical fact delivery 使用 durable intent/ack/dead-letter，观察信号才允许 best effort；
- AgentControl 不再公开可变 registry/comm graph/runtime map。

候选实现：TreeAdmission、Residency、TurnAdmission 和 Lineage 可以在一个 cohesive control owner 内闭合，也可以形成多个窄服务；上述 service 名称不是批准的 public API。定稿前必须给出共同变化原因、调用者、事务边界、scope 和复用现有 AgentControl/Registry/Residency/Limiter 的决定。Residency record 必须使用 strict versioned envelope，绑定 logical/root/parent/path、definition content identity、incarnation、session stream revision、record revision 与 materialization fence；可信 Product blueprint 先构造 Role，磁盘不得选择 class/backend。materialize/rehydrate/forget 的读写、claim 与删除均须 CAS/fence，旧 eviction 不得覆盖或删除新 generation。

### SB0.3 durable、ledger 与 persistence 的 owner 和保证重叠

证据：

- `runtime/durable/factory.py:12-17,32-56` 在 Temporal 不可用时记录 warning 后自动回退到 Jsonl；调用方没有 typed activation result 得知实际 backend 和实际 guarantee；
- `runtime/durable/plugins.py:13-25` 用动态 import/getattr 发现内部 backend，并直接绑定 RunJournal；
- `runtime/persistence/__init__.py:14-31` 同时公开 atomic IO、DiskWriter、Journal 和 RuntimeExecutionTransaction；
- `runtime/ledger/`、`runtime/durable/`、`runtime/persistence/` 分别承担 append fact、执行 backend 和写入事务，但消费者可直接跨过抽象层组合具体实现。

问题：配置选择的 guarantee 会在 observation-plane warning 后自动改变；日志不能作为 authoritative activation result。Temporal 与 JSONL 的具体 guarantee 仍须在 contract 中分别定义，不能仅凭 backend 名称推断其他差异。产品已经确认：显式选择 Temporal 代表硬 durability 要求，激活失败不得改用另一 backend。backend 选择还落在 Runtime 动态 discovery，三个包的公共面允许任意拼装。

稳定需求：

- persistence 只拥有 atomic/CAS/journal 等写入机制；
- ledger 拥有明确 append-fact 不变量；
- durable execution contract 放 Contracts，选择和 manifest 放 Product，Workflow 状态机留 Orchestration；
- 显式注入 typed factory，并返回实际 backend、实际 guarantee 和拒绝原因；显式选择 Temporal 时，依赖缺失、未实现或激活失败必须 fail closed，不得回退 JSONL；
- 每层只公开下一层需要的最小 Port，不公开具体 writer/store/transaction 对象图。

验收：Temporal 未安装、plugin 未实现、构造失败或 activation 失败时，不创建 Jsonl backend、不启动 run、不写入较弱 backend 状态，并返回可机器判别的 typed activation error；只有显式选择 JSONL 才可启动 JSONL backend。

### SB0.5 durable/wire facts 使用反射式宽 codec

证据：

- `contracts/events/_base.py:9-18` 用 `vars()` 与 `cls(**payload)` 实现通用 DurableFact codec；
- `contracts/events/output.py:34-130`、`contracts/events/model.py:191-199` 的 durable payload 含 `Any`/裸 dict；
- `contracts/inference/attempt.py:11-26`、`generation_artifact.py:8-30`、`transport.py:10-15` 的跨进程 schema 含 `dict[str, Any]`。

问题：只验证顶层字段存在，不能严格验证 primitive、tag、version 和嵌套 identity；损坏或旧数据可能被宽松解释为合法事实。

稳定需求：canonical fact 必须使用版本化 typed DTO/tagged union 和严格 codec；外部动态 JSON 只允许在 wire adapter 解码一次。候选实现不得为每个 fact 复制 codec，必须先检索并选择现有 canonical schema/codec 基础设施，在唯一 owner 内扩展。

## 4. P0 安全前置与 P1 服务面、composition、泛型主链

### SB0.4（安全 P0 前置）Runtime process runner 混合两个信任域

当前事实：`runtime/process.py:12-59` 对外提供 `aexecute(cmd: str, shell=True, sandbox_runtime: Any)`，非 shell 分支使用 `cmd.split()`，返回形状随 `wait` 变化；`product/toolsets/builtin/bash.py:27` 直接消费。

违反的不变量：固定内部 argv 与用户可控 shell 不能由 `shell: bool` 混合；sandbox plan 和 process receipt 必须类型化。

证据边界：runner边界混合本身已是Hook、Workflow、BackgroundTask与Tool安全链的共同前置；不能等到证明某个Bash旁路后才治理。必须同时证明definition→permission→intent→execute→receipt→audit保持同一EffectId，并用跨入口negative matrix验证deny/ask/sandbox失败时effect调用次数为零。

稳定整改：分别表达governed shell、fixed argv和interactive/daemon lifecycle，禁止trust-mode bool。复用同一底层spawn/sandbox mechanism但保持不同typed API；api-key helper只走trusted-source fixed argv；Agent workspace write统一FileOps；remote success/local settlement failure进入IN_DOUBT。ToolExecutor保持唯一chokepoint，不因依赖多拆散。

### SB1.1 Runtime composition lease 是巨型 service locator

证据：`contracts/runtime/application.py:130-185` 的 RuntimeCompositionLeasePort 将 route policy、default model、command/session/transfer runtime、permit issuer、artifact store/reader 全部作为 `Any` 暴露；实现和消费者见 `runtime/models/composition.py:100-140`、`runtime/agent/capabilities.py:163`、`product/interfaces/inference_api/composition.py:140-154`。

真实 capability/consumer：

| capability | 实现已有真实类型 | 生产消费者 |
| --- | --- | --- |
| generation/topology identity、`gateway` | 已类型化 | Role application scope、model execution |
| `route_policy` | `ModelRoutePolicy` | skill fork route admission：`runtime/agent/capabilities.py:162-165` |
| command/session/transfer runtime、permit issuer | 实现均有具体 Protocol/类型 | `product/interfaces/inference_api/composition.py:118-176` 构造公开 inference API |
| default model | `DefaultModelMetadata` | Role/tool default-model capability |
| artifact store/reader | store 已为 `ArtifactLookupIndex`，reader 返回仍未标注 | artifact lookup/read capability |

稳定需求：先把Contract属性替换为实现已有的authoritative类型，补齐artifact reader Port；lease保持同一generation的一致snapshot。lease contract明确resource identity、scope、generation、holder、acquire/typed transfer/release状态；borrower没有close权，旧holder不能release新generation。是否进一步拆分依据独立acquisition/lifecycle，不按property数量机械拆分。

### SB1.2 Runtime EngineServices 与 Runtime container 是第二个巨型 locator

证据：`runtime/services.py:27-67,115-136` 聚合 Context、scan gate、run lease、workspace cleanup、application composition/reloader，并把整个 services 对象通过 lease 暴露；`runtime/__init__.py` 再重导出。

要求：Product拥有composition；先建立逐组件consumer/capability与scope/lifecycle matrix，再按use case注入immutable narrow input/Port。Wiring可作为Product内部装配值，但不能成为Runtime locator；禁止`get_service`、string key、mapping或反射fallback，lifecycle lease只返回ownership handle而不携container。

### SB1.3 CLI bootstrap 混入 application composition，per-session Agent hosting 构造重复

证据：

- `product/entrypoints/cli/bootstrap.py:93-173` 的 `build_engine` 构造 ProductContainer、Context、model composition、EngineServices 和 Role factory，但同时拥有首次文件播种、locale、cwd、配置加载和同步 `asyncio.run` 等 CLI policy；
- `product/composition/bootstrap.py:16-27` 的 `build_application` 位于正确 composition 包，但当前只是接受已构造 container/services/factory 的薄 Application 构造函数；
- `product/entrypoints/cli/backend.py:261-284` 仍单独构造每 Session 的 AgentControl/AgentRuntime；
- `product/session_hosting/registry.py:34-69` 复制 control/role hosting；
- `orchestration/agents/environment_facade.py:53-89` 又在 facade 内构造 control/runtime 并直接注册 Role；
- `product/composition/service_gateway.py:21-49` 在缺参时隐式创建 registry/admission owner。

修正结论：不能把 `build_engine` 固化为 canonical application owner，否则 gateway/daemon/SDK 会依赖 CLI policy。应先按 `build_engine`、`build_application` 及其他 host 的真实消费者、scope 和 lifecycle 收敛 Product-owned application factory。另有 per-session Agent hosting 三条构造路径，facade 可以自行成为 control owner；ServiceGateway factory 还会隐式创建 application/process-scoped owner。

稳定需求：先形成application/process/session/Agent/incarnation/turn scope matrix，再由`product/composition/`提供canonical factory。composition分为validated declaration、pure construct、ordered async activation和reverse settlement；CLI只做source/presentation adaptation并在最外层拥有event-loop boundary，factory不得内嵌`asyncio.run()`。建立CLI、SessionRegistry和确认保留facade复用的typed per-session hosting seam。ServiceGateway等process/application owner由composition显式提供。reload构造完整candidate并在trust/content/capability校验后atomic generation swap，失败保持旧代。

### SB1.4 CLI backend 是万能 facade

证据：`product/entrypoints/cli/backend.py:73-126,157-167,261-339,406-486` 同时负责 config、Context、Role、AgentControl、history、rewind、runtime、session 和 usage，公开大量 `Any` 并用反射访问 Role。

稳定需求：entrypoint 只负责 wire/CLI adaptation，不拥有与 canonical Product composition 重复的状态和 lifecycle。ApplicationSessionService、AgentHostingService、HistoryService 只是候选分面；必须先按 backend.py 的真实消费者证明哪些能力共同变化、哪些已有 Port 可复用，避免拆成无语义薄层。

### SB1.5 Agent/Role spawn contract 暴露跨层对象图并截断泛型

证据：

- `contracts/agent/spawn.py:27-45,67-84` 的 SpawnContext 暴露 agent path、config、parent cost tracker 等 `Any`；RunnableAgent 同时暴露 state、context provisioning、cost tracker 和 control binding；
- `provision_spawned_child(RunnableAgent[object])` 截断 OutputT；
- `contracts/ports/agent/factory.py:20-27` 输入 `agent_cls: object`，AgentT 未进入返回关系；
- `contracts/agent/spawn.py:87-88` 的 `is_text_runnable_agent()` 只验证 nominal `RunnableAgent`，却声称把任意输出泛型收窄为 `RunnableAgent[str]`，运行时没有验证 `OutputT`，属于不安全 TypeGuard；
- `product/agents/factory.py:25-53,66-116` child 固定为 `RunnableAgent[str]`，hook/MCP 配置仍为 `Any`。

要求：保持`definition[OutputT] -> builder/request -> RunnableAgent[OutputT] -> AgentRuntime[OutputT] -> ChildAgentHandle[OutputT] -> RunOutcome[OutputT]`；Product text child只是显式specialization，删除不安全TypeGuard而非用cast替代。SpawnContext按Product construction request、Orchestration identity/admission、Runtime context provisioning、budget settlement和control capability拆分，不汇总为新locator；dynamic output manifest在adapter绑定authoritative OutputContract。

### SB1.6 Kernel inference/execution 接收万能 collaborator bundle

证据：

- `kernel/inference/request.py:11-30`、`prompt_builder.py:53-92` 将 request、tool specs、command channel、executor、skill manager、bus 等表示为 `Any`/裸 dict；
- `kernel/execution/engine.py:108-133` 的 executor/context provider/transaction/bus/report callback 为 `Any`；
- `kernel/execution/operations/container.py:15-30` 的 GraphAssemblyInputs 直接穿透 Runtime 对象图。

要求：区分两类边界：Kernel 内部 operation 之间直接使用 Kernel-owned typed operation type/Protocol；Kernel 消费 Runtime 能力时复用或收窄 Contracts-owned Port。按 node/use case 移除万能 subsystem/services bundle，但不得把纯 Kernel operation 为形式统一提升到 Contracts，也不得再造一个同义 `KernelServices` facade。

### SB1.7 Code intelligence Port 混合 query、factory 与 context source

证据：`contracts/ports/code_intelligence/code_map.py:8-21` 返回 object，并以 `build_turn_source(**kwargs: Any) -> object` 构造；`lsp.py:12-32` 同时构造 service 与 context provider。生产装配见 `runtime/agent/wiring.py:9-41`、`product/code_map/factory.py:36-55`。

要求：CodeMap 与 LSP 是两个独立 bounded context，不能作为一个服务拆分任务。CodeMap 复用现有 owner，按 query/index/context-source 真实消费者收窄 typed DTO/Port；LSP 分别核实 ingestion、diagnostics query 和 context-provider 消费者。Product adapter 解码配置后注入 typed factory；具体 Port 数量由方法矩阵决定。

### SB1.8 正式事件、telemetry 与 session facts 退化为 object

证据：`contracts/ports/events/telemetry.py:42-55`、`kernel/telemetry/events.py:14-45` 接受任意 object/Any；`contracts/ports/session/facts.py:10-12` 允许 `commit_fact(event: object)`。

要求：SessionFact 使用明确的 durable accepted union/typed command sink，删除 `object + class set + cast` 主链。Telemetry 按领域保持 typed event 与 emitter 的泛型关系；不要求建立一个全仓全局封闭 ObservationEvent union。观察事件与 durable fact 不能共享任意 object seam。

### SB1.9 Model/Skill/Hook 等 Port 仍是内部对象逃生口

证据：

- `contracts/ports/model/client.py:11-36` 使用 msg Any、tools list[dict] 和 `**kwargs`；
- `contracts/ports/skill/registry.py:8-70` 同时暴露 catalog、pool、injector、reload、source dirs，factory config 为 Any；
- `runtime/hook/manager.py:51-82,137-196` 用 Any/getattr 解释 config/handler，并同时负责注册、匹配、进程执行和 callback folding。

要求：无生产消费者的 legacy `LLMClient` 优先删除，不为它扩建第二个 model request facade；活跃 Model seam 使用 canonical typed request/response。Skill 复用现有 `SkillCatalog`、`SkillPromptProvider` 和 turn-context source contract，按真实消费者收窄或删除 `pool`/`injector` locator property，并类型化 factory config；不得重复创建同义 Catalog/Prompt Port。Hook registration/matching/folding owner 保持内聚，但 `contracts/ports/hook/runner.py:21-25` 的字符串/裸 dict fire seam 必须改为 closed typed invocation；`runtime/hook/command_handler.py:60-105` 直接 `create_subprocess_shell` 且失败统一 `EMPTY`，已证明绕过 canonical command governance。外部 command 必须复用受治理 classifier/permission/approval/sandbox path；observation hook 可显式 best effort，影响权限或执行的 control hook 必须 fail closed。

### SB1.10 Runtime inference 泛型丢失，daemon/embedded composition 待核验

证据：`runtime/inference/fair_queue.py:26-35,91-99` payload 为 Any；`generation.py:30-38` bindings 为 `Mapping[str, Any]`；`product/inference/daemon/application.py:27-32` 和 `product/models/runtime_generation.py:58-63` 直接拼装多个 Runtime concrete module。

要求：`FairAdmissionQueue[PayloadT]`、`QueueEntry[PayloadT]` 端到端保持 payload 类型，Generation binding 使用 canonical typed snapshot/tagged union。Product 直接选择并装配 Runtime implementation 符合 composition 方向，不构成债务；只有 shared daemon 与 embedded generation 在同一 application scope 形成双 owner，或 generation/quota/health/permit/receipt/lifecycle identity 漂移时，才要求统一 Runtime factory/manifest 和最小 typed command/receipt Port。

### SB1.11 ToolExecutor 构造唯一，但 public control surface 与泛型仍泄漏

证据：`runtime/tools/tool_executor.py:100-216` 注入二十余依赖并自行装配 catalog、MCP、telemetry、recovery、journal、policy、settlement 和 pipeline；`231-289` 暴露 catalog/private tools 供外部 introspection；BackgroundTask 以 Any callback 注入。

构造反查：全库只发现 `runtime/agent/components/action.py:202` 一条生产构造路径，因此“平行 composition root”不成立。当前 executor 是由 Role component graph 唯一构造的 cohesive pipeline facade。

稳定需求：保留唯一构造路径；只收窄真正泄漏的 catalog/live-map introspection、BackgroundTask Any seam 和 runtime-discovered tool 泛型。优先复用现有 `ToolBindingSnapshot`、`ToolExecutionPort`、tool views 和 BackgroundTask Port，不按依赖数量拆 manager。

### SB1.12 Product provider registry 暴露 mutable map，但未发现包外写入

证据：`product/models/registry.py:13-40`、`product/web_search/registry.py:44-92`、`product/media_generation/registry.py:11-49,62-93` 暴露 providers/backends map；部分 config 用 Any/getattr。

消费者反查：只发现 `product/composition/model_builder.py:56` 在包外读取 `LLMProviderRegistry.providers` 计算 catalog revision；未发现包外修改三个 map。Web Search 和 Media map 当前只在各自 registry 内部访问。

修正结论：不存在已证明的外部 mutation。Model registry 应提供稳定 revision/snapshot query，让 model builder 不读取 map；Web/Media 只需私有化内部 map并类型化 config/factory。不得仅因类型名为 Registry 拆包或新建 service。

### SB1.13 无生产实例化证据的 Agent facade 公开 Role/control 对象图

证据：`orchestration/agents/environment_facade.py:40-115,120-196` 自行构造 control/store，公开 control、roles 和 Role object，并大量使用 Any/getattr/hasattr。

要求：先核实 `AgentEnvironment`、`BaseEnvironment`、`MoteEnv` 是否存在仓外稳定 SDK/API 承诺。当前未发现生产实例化；若无外部承诺，应删除旧 facade、包根 export 及只验证旧路径的测试，不得为无消费者入口新建 Port。只有确认仍是生产能力时，才由唯一 Product composition 注入按真实消费者定义的最小 hosting/message/human Port，且不公开 Role/runtime/control internals。

### SB1.14 BackgroundTask 的职责混合、Residency pin 与公共面错误

证据：

- `product/agents/background_tasks.py:181-234` 随 Agent/Role 装配独立的 TaskOutputStore + BackgroundTaskPool；这符合每个逻辑 Agent 独占一个 canonical pool 的已确认 ownership，不是债务；
- `orchestration/background_tasks/pool.py:88-152` 同时承担 scheduler、metadata registry、delivery、store、resource callback 和 timeout；
- `orchestration/background_tasks/operation.py:27-54` 把 `ResumeRef` 放入后台 operation result，`pool.py:343-475` 又保留 resumable pause snapshot、`resume_tasks/GetNodeState` 消费语义和按原 task id `resubmit()`；这些 continuation/resume 责任使 process-local task lifecycle 与 durable WorkflowRun 边界未闭合。`promotion.py` 的前台协程自动转后台本身不是 durable Workflow promotion，不作为该债务证据；
- `orchestration/agents/lifecycle/runtime.py:155-174` 的 unloadability 与 `orchestration/agents/residency/manager.py:108-133` 的 eviction 路径未证明会检查该 Agent pool 是否存在未结算任务，因而缺少 BackgroundTask residency pin；
- `orchestration/background_tasks/__init__.py:11-89` 导出大量 store/monitor/decorator/operation 内部类型。

稳定需求：保留每个逻辑 Agent/Role 独立拥有一个 canonical `BackgroundTaskPool`；task registry、sequence、output、progress、notification、wake callback 和 cleanup 均由该 Agent pool 独占。跨 Agent/进程共享能力只能是 Orchestration-owned、窄且类型化的 admission/permit Port，不得形成第二个 task registry 或集中 task state。存在未结算任务时必须 pin residency；全部结算后才可 eviction。Agent incarnation/generation 拥有 ACTIVE→DRAINING→CLOSED gate，submit/work-pin 与 eviction/release begin原子互斥；不能以单次 pending snapshot规避 TOCTOU。release 有界结算自己的 pool并返回 typed disposition，失败保持 DRAINING/pin。需要跨进程、Session resume 或 eviction 后继续的工作必须在提交前进入 WorkflowRun，BackgroundTaskPool 不保留 Workflow definition、continuation、checkpoint、resume state 或 rebind registry。产品已确认保留同 Agent/Pool/进程内的同 TaskId retry：TaskId 对模型保持稳定，每次 resubmit 创建单调 AttemptId，旧 attempt 不得覆盖新 attempt，输出/notification/settlement 必须 attempt-scoped 且幂等；不得据此恢复 Workflow 或跨进程接管。跨 Pool reference 绑定 process/Agent/incarnation/local TaskId，worker loss返回 owner-gone且不自动重放。整改时应先复用现有 task Port，再按真实消费者收窄 command/query/settlement 服务面和包根导出；不得改造成 process singleton。

### SB1.15 Runtime errors 成为跨域垃圾桶

证据：`runtime/errors/__init__.py:17-115` 重导 agent/config/foundation/model/output/runtime/task/tool 等多个 bounded context 的错误，并混入 runtime-local 错误；消费者既从这里又从 Contracts authoritative module 导入。

要求：跨边界错误从各自 Contracts domain 直接导入；runtime/errors 只保留真正 Runtime-local adapter error；删除错误 owner re-export 和旧 `common` 注释。

### SB1.16 跨 host presentation contract 错放在 Product

证据：`product/presentation/events/__init__.py:3-10,13-152` 自称 cross-host/shared contracts，但 DTO/capability 与大规模 re-export 留在 Product，并被 ACP、AGUI、Terminal、Textual 消费。

要求：真正跨 host 的 DTO/Port 下沉 Contracts-owned presentation/surface boundary；Product 只保留 projector、renderer 和 wire adapter。

### SB1.17 Contracts tool config 混入 Product 默认值与多个 Runtime policy

证据：`contracts/config/tool/models.py:37-46` 写死 Read/Search/Bash/Edit/Sleep 等 Product 内置工具名及默认 cap；`61-115` 又聚合 result limiting、compression、effect journal 和 loop guard。

要求：Contracts 只保留经跨层消费者证明的 typed config shape；Product builtin 默认值归 tool catalog/composition；Runtime mechanism option 归对应 owner。result limiting、compression、effect journal 和 loop guard 是否形成不同公开 config，必须按共同变化原因和消费者决定，不按字段分组机械拆包。

### SB1.18 全局 ErrorCode 枚举聚合所有业务域

证据：`contracts/foundation/errors/codes.py:80-159` 同时收纳 file、media、background、agent、OAuth/config、output/runtime/artifact 等错误，并保留已删除 owner 注释。

要求：先列出durable journal/event、wire/API、artifact metadata、日志、测试快照和外部surface对现有ErrorCode的全部消费者，固定`namespace + code + schema_version + typed context` envelope与迁移/退出边界；再让各bounded context拥有namespaced typed code。必须读取的存量数据使用一次性version migration并删除旧decoder；无存量/外部ABI则直接切换。不得保留alias、长期双decoder或失配历史编码。error definition、Runtime adapter normalization、retry/recovery classification和Product human presentation分别归真实owner。

## 5. P2：包内聚与公共面收窄

本节是复审候选，不得仅根据 `__init__.py` 导出数量、文件名或类型后缀直接实施。每项必须补齐包外生产消费者、是否可修改内部状态、现有 canonical public surface 和共同变化原因；没有这些证据时只保留为调查线索。

### SB2.1 FileOps 公共面泄漏内部控制面

`runtime/fileops/__init__.py:3-74` 暴露 lock level、HierarchicalLockManager、cursor registry、具体 store、journal、publisher、mutation coordinator 等。应只公开 FileOperations 与少量 typed query/result/Port；session 消费者不得通过聚合面调用内部文本/存储函数。

### SB2.2 Artifact 公共面与 FileOps mutation repository 名称冲突

`runtime/artifacts/__init__.py:3-16` 同时公开 GC、layout、ownership、publisher、blob store、transfer、resolver；`runtime/fileops/mutation/artifacts.py` 又有不同语义的 ArtifactRepository。两者不是已证明的重复 Artifact store：前者是 artifact bounded context，后者是 FileOps mutation 内部 repository。应按真实跨包消费者收窄已有 Contracts Artifact Port，并为 mutation 内部类型采用不会伪装成 canonical Artifact owner 的精确名称；具体名称仍是候选。

### SB2.3 Workflow 公共面混合 definition builder 与 runtime state

`orchestration/workflows/__init__.py:16-71` 导出 builder、graph state、node record、error、execution run 等大量符号。应拆 definition authoring 面与 run service 面，包根只承诺稳定 identity、DTO 和 service。

### SB2.4 ServiceGateway 公共面需区分 composition 输入与业务泄漏

`runtime/service_gateway/__init__.py:3-9` 同时暴露 gateway、journal、planner 和 snapshot/merge。Product composition 导入 Runtime concrete implementation 本身合法；应分别核实 media/search 是否依赖 planner 内部 failover layout，以及 snapshot 是稳定 capability manifest 还是 Runtime internal record。公共面偏宽成立，但“只留下 gateway/snapshot”仍是候选；不得为隐藏 planner 套无状态转发 facade。

### SB2.5 CodeMap facade 在 `__init__.py` 中定义并暴露实现

`runtime/code_map/__init__.py:1-136` 同时公开 extractor、store、AST model、FileNeighborhood 和 CodeMap concrete service。该包围绕 repository code graph extraction/index/query 高度内聚，应保留 bounded context；先按真实消费者收窄 typed query/index service 与 immutable DTO，将 store/extractor/provider 留在包内，并把实现移出包根。是否保留 concrete `CodeMap` 公共 service 由其方法矩阵决定，不新建同义 facade。

### SB2.6 通用 artifact projection 与 Session read model owner 混合

`runtime/projections/registry.py:19-108` 的 registry/reconciler 拥有 checkpoint→artifact publication→journal ack/retry/dead-letter 管线；Canvas/Notebook/Artifact projector 可共享该不变量，不应按输出名称拆散。`runtime/projections/session.py:63-171` 的 `SessionProjectionState/SessionLiveProjection` 则消费 Session event stream，拥有 replay/live read-model state、sequence 与 subscription lifecycle；其 input、state 和 output 均不同，且 `runtime/session/replay.py` 已直接消费。要求把 Session read model 迁入 `runtime/session/`，同片迁移 agent component/key/accessor、Product event governance 和测试，删除旧 module/package export，不保留 alias 或第二 registry；通用 artifact projection owner 保持内聚并收窄 typed registration/package-root 面。

### SB2.7 Runtime config 是按技术类别形成的 grab bag

`runtime/config/` 同时容纳 LSP、device、MCP、hook、Langfuse、Sentry 等不同变化原因。应逐 consumer 迁移：稳定跨层纯数据 shape 才迁 `contracts/config/<domain>`；实现局部 option 回所属 Runtime bounded context；source precedence、可信路径、secret resolution、backend/default choice 留 Product。禁止把所有 config 机械下沉 Contracts或保留聚合 re-export。

### SB2.8 watcher declaration 混入 Product reload policy

`runtime/file_watch/config.py` 只有 config shape，`runtime/watching/` 是唯一 watcher mechanism，并非两套 watcher state owner。保留 `runtime/watching/`；为 mechanism 使用窄 typed activation spec，Skills/config/MCP reload choice、trusted roots 和默认值归 Product composition，由 Product 注入批准后的 subscription/callback binding。删除 `runtime/file_watch` 一级包且不保留 alias；配置是否进 Contracts 仍按跨层消费者决定。

### SB2.9 英文 presentation owner 错置，elision mechanism 应保留 Runtime owner

`runtime/presentation.py` 的 plural/count/verb 不拥有 Runtime invariant，应把具体 wording 迁回各 Product surface/tool presenter，不能再建通用 presentation helper。`runtime/text/elision.py` 则拥有模型/运行输出预算下的 typed truncation fact、strategy 和确定性 omitted marker，应迁入现有 Runtime context/resources/tool-compression 中真正拥有该不变量的 bounded context，而不是归 Product；禁止合成新的 shared utils/text。

### SB2.10 Product routing/squilla 泄漏 ML pipeline internals

`product/routing/squilla/strategy.py:45-56` 直接 import predictor 私有常量/函数，ML runtime/artifact 使用 Any mapping。应建立 typed RoutingModelService/Decision，strategy 不拼装 ML 内部阶段。

### SB2.11 Product presentation 内部仍以动态事件分派

`product/presentation/consumer.py:19-32,74,116-130` 用动态 `getattr(on_<kind>)` 和 Dict payload；state topology 也含 Any。wire dict 可留在外部 adapter，内部必须用封闭 tagged union/exhaustive handler。

### SB2.12 包根 eager import/re-export 面过大

重点包括：

- `product/toolsets/__init__.py` eager import 全部 builtin 和 Workflow tool；
- `product/presentation/events`、`projection`、`rich_rendering`、`state` 及 textual widgets 大规模重导；
- `runtime/fileops`、`runtime/artifacts`、`runtime/errors`；
- `orchestration/background_tasks`、`orchestration/workflows`。

候选方向：包根只承诺经过评审的稳定 service/DTO/Port；optional backend 与具体实现通过显式 Product composition 选择。不能用 `Store/Client/Task/Backend` 名称后缀机械判定是否允许公开。

### SB2.13 跨包私有符号依赖

已确认：

- `orchestration/automation/cron/service.py:16` 导入 `_next_cron_run_ms`；
- `product/routing/squilla/strategy.py:55` 导入 predictor 私有常量/函数；
- `product/entrypoints/gateway/cli.py:25` 导入 `_shared_application_identity`；
- `product/config/report.py:21`、`product/composition/model_startup.py:12` 导入 diagnostics 私有函数；
- `product/entrypoints/cli/__main__.py:21` 导入 `_HAS_TEXTUAL`。

若共同变化则合并 owner；若确为跨包能力则提升为最小公共 service/DTO，禁止继续依赖 `_private`。

### SB2.14 架构门禁 collection 不可执行；旧 local-import 命中不成立

- `orchestration/workflows/base_node.py` 的 import 命中位于 docstring 示例；`product/entrypoints/cli/__main__.py` 的 optional import 位于模块顶部 `try/except ImportError`。二者均不是生产 local import，现有 static local-import gate 已通过；
- 全量 `ztest/architecture` 收集因 `product/__init__.py` eager import composition，再加载 terminal/pyte 而失败；随后产生 partially initialized Application 循环错误。

真实债务是基础 package import 受 optional backend 污染，导致 pytest architecture collection 不可执行并暴露 Application import cycle。必须建立 hermetic architecture gate：不加载 Product composition、TUI、PTY、browser/provider SDK 也能运行；不得为旧误报修改合法 docstring 或顶部 guarded import。

### SB2.15 合法但需收窄的 Runtime 公共服务

以下 bounded context 的 owner 基本正确，不建议拆成微服务，只需在相关改动中收窄 service：

- `runtime/media/`：保留 VideoProbe/Decompose typed service，扩展名/UI policy 留 Product；
- `runtime/telemetry/`：增加 typed ObservationEnvelope/SpanHandle，Langfuse/HTTP Any 限制在 adapter；
- `runtime/hook/`、`runtime/service_gateway/`、`runtime/code_map/`：保持 owner，缩小包根和跨包面。

## 6. P0 复审卡

本节固定 P0 的已确认边界，不批准具体类名。实施需求仍须把每张卡拆成可独立验收的垂直切片。

### 6.1 SB0.1 Workflow durable execution

| 维度 | 复审结论 |
| --- | --- |
| 当前事实 | Workflow graph、continuation、checkpoint、run state 和 execution ownership 主要是进程内对象；Product 有 consume-and-pop continuation registry 和第二 definition 构造路径。 |
| 违反的不变量 | `AGENTS.md` 8.2；已确认 Workflow 必须跨进程、Session resume 和 Residency eviction 恢复，且同一 run 只有一个 fenced owner。 |
| 稳定面 | definition identity、run identity、execution generation、checkpoint revision、pending frontier、effect intent/receipt、terminal outcome。 |
| 允许变化轴 | graph compiler、checkpoint store/backend、lease 实现、scheduler/placement、Product tool/surface。 |
| canonical owner | Workflow 状态机归 `orchestration/workflows/`；schema/Port 归 Contracts；store/lease/effect mechanism 归 Runtime；选择和装配归 Product。 |
| 已检索基础设施 | 现有 `WorkflowBuilder/WorkflowDefinition/WorkflowRun`；`contracts/ports/execution/checkpoint.py`；`contracts/ports/session/run_lease.py`；`runtime/session/run_lease.py`；`runtime/persistence/execution_transaction.py`；`runtime/ledger/`；`runtime/durable/`。优先扩展这些 authoritative seam，禁止复制 lease、journal 或 transaction。 |
| 真实消费者 | `product/workflows/run_graph/`、`product/workflows/background_adapter.py`、`product/agents/background_tasks.py`、Workflow 工具及 BackgroundTask completion adapter。定稿前生成方法级 consumer matrix。 |
| 生命周期 | construct definition → create durable run → acquire fenced incarnation → load/validate checkpoint → execute/settle effect → commit checkpoint/terminal → release；resume 生成新 incarnation，不恢复 coroutine。 |
| 失败与恢复 | definition mismatch、unknown codec、lost lease、checkpoint CAS 冲突和 in-doubt effect 均 fail closed；旧 owner 不能提交；恢复保留最后有效证据。 |
| 必删旧状态/入口 | 生产 `WorkflowRun` 的随机 UUID、`_executing`、asyncio task和内存`_state` owner；`WorkflowContinuation`；Product continuation registry；inspection对private state/graph的直接访问；从graph metadata构造第二Workflow identity的路径；旧`resume_tasks/get_node_state` canonical恢复外观及进程对象恢复旁路。 |
| 验收 | 完整definition identity；所有mutation fencing；durable cancel/deadline/pause/resume；terminal outcome与delivery intent/ack；单一Orchestration reconcile owner、公平/背压/fenced claim；backend最低guarantee profile；专项需求中的crash matrix、双进程resume、Session/Residency恢复。 |
| 待确认 | 无产品二选一；具体 Port/service 形状仍待消费者矩阵评审。 |

### 6.2 SB0.2 Agent governance、lineage 与 delivery

| 维度 | 复审结论 |
| --- | --- |
| 当前事实 | logical identity 没有独立 cap；`max_agents` 混合 turn/residency；lineage 仅内存；pending delivery 仅内存却宣称 never-drop；control 暴露内部 registry/graph。 |
| 违反的不变量 | `AGENTS.md` 8.1、9；三类容量必须独立，lineage 跨 supervisor 重启 durable，canonical fact delivery 必须 durable。 |
| 稳定面 | SpawnRequestId；不可复用logical Agent/root/parent identity；path/nickname revision/tombstone；definition/incarnation/lifecycle/placement identity；三类cap及budget reservation receipt；delivery intent/claim/process/ack/dead-letter identity。 |
| 允许变化轴 | admission policy、scheduler、worker placement、lineage/delivery store、residency strategy、best-effort observation transport。 |
| canonical owner | Agent tree 与治理状态归 `orchestration/agents/`；durable store/lease mechanism 由 Runtime Port 提供；Product 只装配 policy/default。 |
| 已检索基础设施 | `AgentControl`、`AgentRegistry`、`AgentExecutionLimiter`、`Residency`、`CommGraph`、`PendingDeliveryQueue`、cost tree、`runtime/control/leases.py`、session run lease、Runtime execution transaction。必须扩展/替换这些 owner，不另建平行 tree/queue/lease。 |
| 真实消费者 | Product Agent tool/factory、CLI/session hosting、automation Agent trigger、Agent communication/team roster、completion watcher、ACP/AG-UI session paths。定稿前列出对 control/registry/runtime map 的直接访问。 |
| 生命周期 | durable spawn state machine → cap/budget reservation → lineage commit → placement/incarnation；eviction、worker-loss、logical terminate/tombstone、retention purge分离；delivery intent→claim→process→ack/dead-letter。 |
| 失败与恢复 | spawn任一步崩溃、重复request、cap/budget exhaustion、ABA、worker/supervisor crash、evict/terminate/purge、delivery各commit点崩溃、poison payload和stale generation均有typed settlement/reconcile。 |
| 必删旧状态/入口 | 内存`SpawnTransaction` rollback充当durable原子性；`reserve_spawn_slot(None)`；混合含义`max_agents`及alias；`has_capacity()+guard()`；普通release同时删除全部owner；进程内lineage truth；never-drop/无限park文案；裸对象delivery；包外可变registry/runtime map。 |
| 验收 | 可恢复spawn状态机；三类cap scope/release/GC；path/nickname/agent-id ABA；四类lifecycle transition；strict delivery envelope与artifact retention；预算原子reserve/settle；有界公平scheduler；supervisor冷启动十代树及best-effort wake全丢仍可reconcile。 |
| 待确认 | 无产品二选一；一个 cohesive control 或多个 service object 由调用链评审决定。 |

### 6.3 SB0.3 Durable backend activation 与基础设施 owner

| 维度 | 复审结论 |
| --- | --- |
| 当前事实 | Temporal 激活失败会 warning 后回退 JSONL；调用方没有 authoritative typed activation result；durable/ledger/persistence 公共面可被消费者自由拼装。 |
| 违反的不变量 | 显式 durability 选择必须与实际 guarantee 一致；日志不能改变控制语义；同一机制只能有一个 canonical owner。 |
| 产品决定 | 显式选择 Temporal 是硬要求；缺依赖、未实现、构造或 activation 失败必须 fail closed，禁止回退 JSONL。 |
| 稳定面 | backend identity、process/host scope、durability、fencing、transaction/commit、recovery guarantee、activation result/error、journal/transaction identity。 |
| 允许变化轴 | Temporal/JSONL/未来 backend 实现；atomic writer、ledger store 和 deployment placement。 |
| canonical owner | Product 选择 backend；Contracts 定义 backend/guarantee/activation Port；Runtime durable 提供实现；persistence 只拥有写入机制，ledger 只拥有 append-fact 语义。 |
| 已检索基础设施 | `runtime/durable/factory.py`、`runtime/durable/plugins.py`、`runtime/ledger/append_ledger.py`、`runtime/ledger/run_journal.py`、`runtime/persistence/`、Contracts journal/checkpoint/lease Ports。不得再建第四套 journal/transaction owner。 |
| 生命周期 | select → validate availability/guarantee → construct → activate → publish typed lease/result → drain/close；失败前不得发布 owner 或启动 run。 |
| 失败与恢复 | activation 失败无 fallback、无较弱 backend 写入、无部分 owner；lost lease/fence 拒绝 commit；所有Workflow backend共同满足single fenced owner、revision CAS、strict codec、crash recovery、effect reconciliation与durable scan，backend-specific差异只能在共同最低contract之上声明。 |
| 必删旧状态/入口 | warning-and-fallback 路径；Runtime 动态 internal discovery 作为 Product selection；包根对具体 writer/store/transaction 的无界组合面。 |
| 验收 | Temporal 缺失/未实现/构造/激活四类失败；显式JSONL仅在共同最低Workflow guarantee全部满足时成功；activation result逐项可机器判别backend/scope/durability/fencing/commit/recovery；任何不足或失败后无run/state side effect。 |

### 6.4 SB0.5 Strict durable/wire codec

| 维度 | 复审结论 |
| --- | --- |
| 当前事实 | `DurableFact` 通用 codec 只检查顶层字段后反射构造；多类 nested payload 使用 Any/裸 mapping。 |
| 违反的不变量 | `AGENTS.md` 4、9、14；unknown version/tag、错误 primitive、identity mismatch 和损坏数据必须 fail closed。 |
| 稳定面 | envelope format owner、schema/version/tag、canonical identity、strict primitive/nested DTO rules、migration window。 |
| 允许变化轴 | 各 bounded-context fact union、codec implementation、存储 backend和有期限 upcaster。 |
| canonical owner | Contracts 拥有 schema/tagged union；Runtime event/session adapter 拥有 encode/decode 执行；Product wire adapter 只做外部格式投影。 |
| 已检索基础设施 | `contracts/events/governance.py::EventCodecEntry`、`contracts/events/envelope.py`、`contracts/events/file/codec.py`、`runtime/session/codec.py`、`runtime/events/journal.py`、`product/inference/daemon/operations_audit_codec.py`、model topology codec。先选可复用的 strict primitive/envelope机制，不为每个 fact 复制完整 codec。 |
| 生命周期 | register versioned codec → encode canonical event → durable append → strict decode → identity/stream validation → projection；migration 只在明确支持窗口发生。 |
| 失败与恢复 | unknown/malformed/middle corruption fail closed并保留证据；允许的 tail torn write 必须协议化区分；不能降级为空事实。 |
| 必删旧状态/入口 | `vars()+cls(**payload)` 作为 durable canonical decoder；宽松 primitive coercion；多个不一致的通用事实编码入口。 |
| 验收 | 每个 union round-trip；unknown version/tag、额外/缺失字段、bool-as-int、数字字符串、nested identity mismatch、中间损坏和 tail torn fixtures。 |
| 待确认 | 选择/扩展哪一套现有 codec mechanism 需做 capability matrix；产品语义无待决项。 |

### 6.5 P0 实施验证下限

| 切片 | 直接测试 | 消费者测试 | 静态检查 | fault injection |
| --- | --- | --- | --- | --- |
| SB0.1 | `ztest/workflows/`、新增 durable Workflow tests | `ztest/product/` 中 run-graph/Agent tool；BackgroundTask adapter tests | `ztest/architecture/` Workflow owner/依赖门禁；相关 Pyright | checkpoint 前后、effect intent/receipt 前后、lost lease、双 resume、definition mismatch |
| SB0.2 | `ztest/agents/`、residency/messaging/admission tests | Agent tool、automation、session hosting、ACP/AG-UI 相关 tests | Agent 泛型/Port Pyright；lineage/control owner gate | 并发 spawn、三类 cap、accept 后 crash、evict/rehydrate race、worker/supervisor restart、stale generation |
| SB0.3 | `ztest/runtime/durable/`、ledger/persistence tests | Workflow 和 inference backend activation consumers | backend manifest/import purity gate；factory/Port Pyright | dependency missing、not implemented、construct/activate failure、partial owner publish、lost fence |
| SB0.5 | event/session/file/inference codec tests | replay、projection、resume、journal consumers | durable union exhaustiveness与公开 signature Pyright | unknown version/tag、错误 primitive、nested identity mismatch、中间损坏、tail torn、migration failure |

路径必须在实施时从当前 `rg --files ztest` 重新核实；表中的目录是测试责任域，不是对不存在路径的承诺。每个切片还必须运行受影响的完整 `ztest/architecture`，且先完成 hermetic collection 修复。

## 7. 全包覆盖矩阵

说明：`问题` 后列出本报告编号；`收窄` 表示 bounded context 基本正确但公共面需在相关变更中收紧；`保留` 表示本轮未发现足以成立的新服务边界债务，不代表永久豁免。空目录不算生产实现。

### 7.1 Contracts

| 包 | 结论 |
| --- | --- |
| `contracts/agent` | 问题：SB1.5 |
| `contracts/artifact` | 保留，identity/DTO 边界清晰 |
| `contracts/authorization` | 保留 |
| `contracts/composition` | 保留，需随 SB1.1/SB1.3 验证 composition contract |
| `contracts/config` | 收窄：SB1.17、SB2.7 |
| `contracts/config/conversation` | 保留 |
| `contracts/config/inference` | 保留，随 SB1.10 校验 generation contract |
| `contracts/config/model` | 保留 |
| `contracts/config/tool` | 问题：SB1.17 |
| `contracts/content` | 保留 |
| `contracts/conversation` | 保留 |
| `contracts/events` | 问题：SB0.5、SB1.8 |
| `contracts/events/file` | 保留，随 strict codec 统一验收 |
| `contracts/execution` | 保留 |
| `contracts/file` | 收窄：包根重导 `contracts.ports.file`，应从 authoritative Port 导入 |
| `contracts/foundation` | 问题：SB1.18 |
| `contracts/foundation/errors` | 问题：SB1.18 |
| `contracts/hook` | 收窄：FileChangedPayload 的 version/attribution 仍为 object；包根 star re-export |
| `contracts/inference` | 问题：SB0.5；并与 `contracts/model`、`contracts/service` 的部分 request/transport/service identity 相邻，需逐类型按消费者与 authoritative owner 核实，不能仅因字段相似判定重叠 |
| `contracts/interaction` | 保留 |
| `contracts/model` | 收窄：与 inference 的 invocation/failover 边界需明确 |
| `contracts/output` | 保留，OutputContract 泛型主链应保留 |
| `contracts/ports` | 收窄：禁止聚合式 service locator |
| `contracts/ports/agent` | 问题：SB1.5 |
| `contracts/ports/artifact` | 保留 |
| `contracts/ports/code_intelligence` | 问题：SB1.7 |
| `contracts/ports/content` | 保留 |
| `contracts/ports/conversation` | 保留 |
| `contracts/ports/events` | 问题：SB1.8 |
| `contracts/ports/execution` | 保留 |
| `contracts/ports/file` | 保留 |
| `contracts/ports/hook` | 保留方向，随 SB1.9 收窄 runner/catalog |
| `contracts/ports/inference` | 保留方向，补 SB1.10 最小 service |
| `contracts/ports/interaction` | 保留 |
| `contracts/ports/model` | 问题：SB1.9 |
| `contracts/ports/output` | 保留 |
| `contracts/ports/runtime` | 收窄：SB1.1、SB1.2 |
| `contracts/ports/service` | 保留 |
| `contracts/ports/session` | 问题：SB1.8 |
| `contracts/ports/skill` | 问题：SB1.9 |
| `contracts/ports/surface` | 保留，承接 SB1.16 时避免巨型 surface Port |
| `contracts/ports/task` | 保留方向；承接 Agent-owned BackgroundTask 最小 command/query/settlement Port，并与 supervisor admission/permit Port 分离 |
| `contracts/ports/tool` | 收窄：policy facts resolver 仍含动态 dict |
| `contracts/runtime` | 问题：SB1.1 |
| `contracts/service` | 收窄：明确与 inference/model 的 identity 边界 |
| `contracts/session` | 保留 |
| `contracts/surface` | 保留，承接跨 host DTO |
| `contracts/task` | 保留方向 |
| `contracts/tool` | 收窄：Tool event/input/scope 中仍有 Any/object |

### 7.2 Kernel

| 包 | 结论 |
| --- | --- |
| `kernel/commands` | 收窄：history projection 用 `list[Any] + hasattr/model_dump/default=str`，fingerprint 不稳定 |
| `kernel/commands/xml` | 保留 |
| `kernel/execution` | 问题：SB1.6；graph/result/state 泛型主链保留 |
| `kernel/execution/graph` | 保留方向，节点依赖改最小 Protocol |
| `kernel/execution/operations` | 问题：SB1.6 |
| `kernel/inference` | 问题：SB1.6 |
| `kernel/output` | 保留 |
| `kernel/telemetry` | 问题：SB1.8 |
| `kernel/tools` | 保留，catalog/definition 公共面较小 |
| `kernel/flow`、`kernel/flow/graph`、`kernel/flow/services` | 审计时仅残留未跟踪的 `__pycache__`；已在审计后删除，不再是当前包 |

### 7.3 Runtime

| 包 | 结论 |
| --- | --- |
| `runtime/agent` | 收窄：受 SB1.2、SB1.5、SB1.11 牵连 |
| `runtime/agent/completion` | 保留 |
| `runtime/agent/components` | 保留方向，禁止继续接收万能 collaborator bundle |
| `runtime/agent/runtime_modules` | 保留 |
| `runtime/artifacts` | 问题：SB2.2 |
| `runtime/code_map` | 问题：SB2.5 |
| `runtime/code_map/_langconfigs` | 内部资源，保留且不得成为公共面 |
| `runtime/code_map/providers` | 保留为内部 provider |
| `runtime/config` | 问题：SB2.7 |
| `runtime/context` | 保留 |
| `runtime/context/compaction` | 保留 |
| `runtime/context/history` | 保留 |
| `runtime/context/turn` | 保留 |
| `runtime/context/turn_context` | 保留 |
| `runtime/control` | 保留方向 |
| `runtime/control/scheduling` | 保留 |
| `runtime/durable` | 问题：SB0.3 |
| `runtime/durable/temporal` | 保留 backend；显式选择后激活失败不得自动回退 |
| `runtime/errors` | 问题：SB1.15 |
| `runtime/events` | 保留方向，随 SB0.5/SB1.8 类型化 |
| `runtime/events/backends` | 保留 adapter |
| `runtime/file_watch` | 问题：SB2.8 |
| `runtime/fileops` | 问题：SB2.1、SB2.2 |
| `runtime/fileops/assets` | 保留 internal |
| `runtime/fileops/document_adapters` | 保留 internal adapter |
| `runtime/fileops/mutation` | 保留 internal，重命名 mutation artifact repository |
| `runtime/hook` | 问题：SB1.9；owner 可保留 |
| `runtime/inference` | 问题：SB1.10 |
| `runtime/interactive` | 保留 mechanism，包根不得 eager optional backend |
| `runtime/interactive/assets` | 内部资源，保留 |
| `runtime/interactive/browser` | 保留 |
| `runtime/interactive/canvas` | 保留 |
| `runtime/interactive/device` | 保留 |
| `runtime/interactive/kernel` | 保留 |
| `runtime/interactive/terminal` | 保留，optional `pyte` 不得污染基础 import |
| `runtime/ledger` | 问题：SB0.3，明确 append-fact owner |
| `runtime/lsp` | 保留 mechanism，config/source policy 外移 |
| `runtime/media` | 收窄：SB2.15 |
| `runtime/models` | 保留总体 owner，跨 Product 只走最小 model service |
| `runtime/models/auth` | 保留 |
| `runtime/models/clients` | 保留 adapter |
| `runtime/models/cost` | 保留 |
| `runtime/models/failover` | 保留 |
| `runtime/models/ratelimit` | 保留 |
| `runtime/models/routing` | 保留 |
| `runtime/output` | 保留 |
| `runtime/persistence` | 问题：SB0.3 |
| `runtime/projections` | 问题：SB2.6 |
| `runtime/prompt` | 保留 |
| `runtime/resilience` | 保留 |
| `runtime/resilience/failover` | 保留 |
| `runtime/resources` | 保留 |
| `runtime/sandbox` | 保留 |
| `runtime/sandbox/network` | 保留 |
| `runtime/secrets` | 保留 owner，仍需既有跨进程 vault 原子性债务 |
| `runtime/service_gateway` | 问题：SB2.4 |
| `runtime/session` | 保留总体 owner，durable codec 随 SB0.5 统一 |
| `runtime/session/migrations` | 保留 |
| `runtime/session/workspace` | 保留，仍需既有 fenced cleanup/GC 债务 |
| `runtime/telemetry` | 收窄：SB2.15；存在中段 `requests` import |
| `runtime/telemetry/logging` | 保留 |
| `runtime/telemetry/observability` | 收窄外部 SDK Any 到 adapter |
| `runtime/text` | 问题：SB2.9 |
| `runtime/tools` | 问题：SB1.11 |
| `runtime/tools/compress` | 保留 |
| `runtime/tools/loop_guard` | 保留 |
| `runtime/tools/mcp` | 保留 adapter，外部 schema 在入口解码 |
| `runtime/tools/permission` | 保留 |
| `runtime/tools/secrets` | 保留 |
| `runtime/vcs` | 保留 |
| `runtime/watching` | 保留 canonical watcher mechanism，吸收 SB2.8 的 Runtime 部分 |
| `runtime/process.py` | 问题：SB0.4 |
| `runtime/services.py` | 问题：SB1.2 |
| `runtime/presentation.py` | 问题：SB2.9 |
| `runtime/engine.py`、`run_context.py`、`terminal_ansi.py` | 保留方向，跨包面需经各自 service/DTO |

### 7.4 Orchestration

| 包 | 结论 |
| --- | --- |
| `orchestration/agents` | 问题：SB0.2、SB1.13 |
| `orchestration/agents/costing` | 保留内部治理模块，预算事实需进入 durable lineage |
| `orchestration/agents/execution` | 问题：SB0.2，turn admission 原子化 |
| `orchestration/agents/identity` | 问题：SB0.2，升级为 durable lineage service |
| `orchestration/agents/lifecycle` | 收窄：handle 的 control/residency slot 仍为 Any |
| `orchestration/agents/messaging` | 问题：SB0.2 |
| `orchestration/agents/residency` | 问题：SB0.2，需 generation-fenced state machine |
| `orchestration/automation` | 保留方向；跨层 Trigger/Receipt 应迁 Contracts Port |
| `orchestration/automation/cron` | 基本内聚；修复 SB2.13 private import，仍需既有跨进程 store/fence 债务 |
| `orchestration/background_tasks` | 问题：SB1.14 |
| `orchestration/background_tasks/monitoring` | 保留 internal，不从包根暴露 |
| `orchestration/background_tasks/results` | 保留 internal/typed projection |
| `orchestration/workflows` | 问题：SB0.1、SB2.3 |

### 7.5 Product

| 包 | 结论 |
| --- | --- |
| `product/agents` | 问题：SB0.1、SB1.5、SB1.14 |
| `product/automation` | 保留 adapter |
| `product/code_map` | 保留 bounded context，消费 SB1.7 typed service |
| `product/composition` | 问题：SB1.3；另有 SB2.13 private import |
| `product/config` | 收窄：来源/默认 owner 正确，内部 public/private 边界需修 |
| `product/config/adapters` | 保留外部 adapter，统一 provenance/trust gate |
| `product/config/model` | 保留，外部动态值在此解码 |
| `product/entrypoints` | 问题：SB1.3、SB1.4 |
| `product/entrypoints/cli` | 问题：SB1.4、SB2.13、SB2.14 |
| `product/entrypoints/cron` | 保留 adapter，不复制 Cron service mutation owner |
| `product/entrypoints/gateway` | 收窄：SB2.13 |
| `product/i18n` | 保留 |
| `product/i18n/catalog` | 保留 internal resource |
| `product/inference` | 收窄：SB1.10 queue/generation contract；Product 选择并装配 Runtime implementation 本身合法 |
| `product/inference/backends` | 保留 adapter |
| `product/inference/daemon` | 待核验：SB1.10；只有与 embedded generation 形成双 owner 或 lifecycle identity 漂移才是 composition 债务 |
| `product/inference/security` | 保留 |
| `product/interaction` | 保留 bounded context |
| `product/interaction/commands` | 保留 |
| `product/interfaces` | 保留 surface grouping，共享 session/presentation 只能经服务面 |
| `product/interfaces/acp` | 保留 wire adapter |
| `product/interfaces/agui` | 保留 wire adapter，prompt ownership 仍受既有安全债务约束 |
| `product/interfaces/inference_admin_api` | 保留 opt-in surface |
| `product/interfaces/inference_api` | 保留 surface，composition 改走 SB1.1/SB1.10 Port |
| `product/interfaces/inference_inspector_api` | 保留 opt-in surface |
| `product/interfaces/inference_reasoning_replay_api` | 保留 opt-in surface |
| `product/interfaces/inference_webhook_api` | 保留 opt-in surface |
| `product/interfaces/structured` | 保留 wire adapter |
| `product/interfaces/terminal` | 保留 surface |
| `product/interfaces/textual` | 保留 surface，optional dependency hermetic |
| `product/lsp` | 保留 opt-in Product factory |
| `product/media_generation` | 收窄：registry internal/typed，service owner 保留 |
| `product/media_generation/providers` | 保留 provider adapter |
| `product/models` | 收窄：SB1.12；runtime generation 走最小 service |
| `product/models/providers` | 保留 provider adapter |
| `product/models/transports` | 保留 wire adapter；私有跨 transport helper 应提升内部公共 seam或合并 |
| `product/paths` | 保留 Product path policy owner |
| `product/presentation` | 问题：SB1.16、SB2.11、SB2.12 |
| `product/presentation/events` | 问题：SB1.16 |
| `product/presentation/projection` | 收窄包根和动态 dispatch |
| `product/presentation/rich_rendering` | 收窄 eager/re-export；保持 Product owner |
| `product/presentation/state` | 收窄内部 Any topology |
| `product/routing` | 保留 Product routing policy owner |
| `product/routing/squilla` | 问题：SB2.10、SB2.13 |
| `product/session_hosting` | 问题：SB1.3；bounded context 保留但只暴露 typed session service |
| `product/skills` | 保留 bounded context，消费 SB1.9 typed service |
| `product/toolsets` | 问题：SB2.12，catalog 必须显式 manifest |
| `product/toolsets/builtin` | 保留具体 Product tools，不能成为 import-time registry owner |
| `product/web_search` | 收窄：SB1.12；service owner 保留 |
| `product/workflows` | 问题：SB0.1，只保留 Product adapter |
| `product/workflows/run_graph` | 收窄：模型-facing compiler/tool 留 Product，run state/resume owner 回 Orchestration |

## 8. 架构门禁：只保护已确认边界

现有门禁主要检查层级、循环、局部 import 和部分动态边界。门禁不能以名称或 AST 启发式替代业务语义评审。

S0 只加入可以精确判定的门禁：

1. hermetic architecture test collection，不 import Product package root 或 optional backend；
2. 生产局部 import；
3. 已确认 bounded context 之间的 `_private` import；
4. package-root import purity 和 optional dependency isolation；
5. 在唯一 composition root 经事实确认后，对明确 concrete constructor 建立禁止构造清单。

后续门禁由经过评审的 owner/public-surface manifest 驱动。manifest 记录 authoritative module、允许消费者、public signature 和具体禁止关系，再精确验证：

- 已批准边界的泛型 continuity；
- 已批准 Port 中未授权的 `Any/object/**kwargs/裸 mapping`；
- 具体 store/registry/state 的非法消费者或 mutation；
- 已确认 canonical infrastructure owner 的平行构造；
- package public surface 与 manifest 的一致性。

禁止仅根据 `Store/Lock/Registry/Task/Client/Backend` 后缀、相似名称或构造类型名称生成违规；这种启发式会制造误报和白名单债务。

## 9. 建议收敛与整改顺序

```text
S0  修复 hermetic architecture collection 与 import purity
S1  对每个 P0 固定产品语义、canonical contract、唯一 owner 与复用决定
S2  在 canonical owner 内闭合正常、失败、恢复、取消和清理路径
S3  迁移全部生产消费者，删除旧入口、旧 identity、旧 state 和平行 owner
S4  为已确认且已实现的边界增加精确 architecture gate/manifest
S5  逐项补齐 P1/P2 消费者证据，再处理公共面、泛型和包内聚债务
```

门禁保护已经确认和实现的架构，不能替代产品与 contract 设计。每一步必须迁移全部生产消费者并删除旧入口，不允许长期双路径。

## 10. 验证记录

已执行：

```text
python -B ztest/architecture/static_governance.py local-imports
python -B ztest/architecture/static_governance.py product-scc
python -B ztest/architecture/static_governance.py dynamic-discovery
python -B ztest/architecture/static_governance.py governed-boundary
```

四项静态脚本均通过。这只能证明其现有规则闭合，不能否定本报告发现的服务边界问题。

复跑 inventory：

```text
find contracts kernel runtime orchestration product -mindepth 1 -maxdepth 2 -type d -not -name '__pycache__' | sort | wc -l
```

当前输出为 `193`。该计数与第 2.5 节口径一致；三个已删除的 `kernel/flow*` 缓存目录不计入当前 inventory。目录计数只证明覆盖集合完整，不证明每个“保留”结论都完成了同等深度的 consumer/lifecycle 审计。

总账映射核验必须同时比较 R 编号与当前标题/owner语义，不能只做编号集合差集。核心总账曾发生插号/拆项，证明“编号仍存在”不能防止语义错配；第 11.1 节已按当前标题重映射，后续总账重排必须在同一文档切片同步本表与实施文档。

源码路径新鲜度核验覆盖本报告、核心总账、Workflow 专项和产品决定交接文档：除本报告明确标记为已删除历史目录的三个 `kernel/flow*` 外，当前引用的生产源码文件/目录均存在；带首行定位的引用也未超出当前文件行数。该检查只排除悬空路径和明显越界行号，具体行段语义仍以条目中的调用链证据为准。

尝试执行：

```text
python -B -m pytest ztest/architecture -q --tb=short -p no:cacheprovider
```

结果：收集阶段 31 个错误，未进入测试执行。首个根因是 Product package/composition eager import Terminal backend，环境缺少 `pyte`；随后出现 `mote.product.composition.application.Application` partially initialized 的循环导入错误。这一结果已纳入 SB2.14，未安装依赖，也未修改测试规避。

## 11. 与现有债务总账的关系

本报告不替代 `core-architecture-debt-closure-requirements.md`。既有 R1.1、R1.2、R1.5、R2.4-R2.15、R3.2、R3.4 等已经覆盖本报告的一部分具体位置；本报告新增的是全包视角下的内聚、复用与最小服务面闭环。

实施前应逐条建立映射：

- 已有 R 能完整闭合的 SB 项，扩充其验收，不重复创建 owner；
- 只有部分覆盖的 SB 项，补充子包服务面和复用门禁；
- 完全未覆盖的 SB 项，作为新的横切架构债务进入总账；
- 不得为迁移本报告再建立第二份长期权威 backlog。最终仍由一个总账跟踪状态，本报告作为审计证据保留。

### 11.1 SB 到唯一总账的映射

| SB | 现有总账/专项需求 | 处理方式 |
| --- | --- | --- |
| SB0.1 | R2.1；`durable-workflow-recovery-requirements.md` | 专项需求承接 durable 产品语义；R2.1 扩充 identity/唯一入口和 Product 旧 owner 删除证据，不新增平行 backlog |
| SB0.2 | R1.13、R1.20、R2.4、R2.15-R2.20、R2.27-R2.28、R2.46 | 分别扩充 delivery、incarnation fence、typed control/spawn/turn/nickname/handle、strict Residency/Mailbox、可信 rehydration、durable lineage 与 clock semantics；fan-out/root/subtree cap与 cancellation epoch归 Agent governance |
| SB0.3 | R0.8；durable Workflow 专项 | R0.8 承接 journal/副作用 fail-closed；新增 backend activation/guarantee contract 与 Temporal fail-closed 产品决定 |
| SB0.4 | R2.30、R1.7、R0.3、R0.6、R0.8、R2.36 | 作为共同安全前置承接EffectId、typed runner/helper/FileOps/Hook/in-doubt/Tool chokepoint，不新增平行runner或安全总账 |
| SB0.5 | R1.11-R1.12、R1.23-R1.24、R2.19-R2.21、R2.24-R2.25、R2.29、R2.31-R2.33、R2.41、R2.46 | 由各 durable domain 条目承接 strict schema、Cron occurrence settlement、cleanup deletion claim、Artifact reachability/pin generation 与 clock contract；先做 capability matrix，不能另建万能 codec、store、GC 或 timer manager |
| SB1.1 | R1.1 | 直接扩充 capability matrix、lifecycle lease 与现有窄 Port 复用 |
| SB1.2 | R1.1、R2.10、R3.2 | 部分覆盖；先证明 EngineServices 真实消费者和 scope，再决定合并到 R1.1 或新增总账项 |
| SB1.3、SB1.4 | R0.0、R2.11 | 扩充唯一 Product composition、CLI/session hosting 构造 identity 与删除清单 |
| SB1.5 | R2.4、R2.10、R2.15 | 直接扩充 OutputT 泛型连续性、删除不安全 str TypeGuard 和 SpawnContext 对象图删除 |
| SB1.6 | R2.2、R2.7、R2.14 | 直接扩充节点最小消费 Protocol 与现有 Port 复用 |
| SB1.7 | R2.5 | 直接扩充 CodeMap/LSP query、ingestion、context source 的消费者矩阵 |
| SB1.8 | R2.24、R2.25、R2.43、R2.44 | durable payload/SessionFact、subscriber checkpoint/effect/ack及telemetry EventT连续性分别承接；不建全局ObservationEvent union |
| SB1.9 | R0.2、R0.6、R1.9、R2.7、R2.8 | Model contract/Hook execution/extension trust/inference request/turn-context 分别承接；删除无消费者 legacy LLMClient；Skill 复用现有 Catalog/Prompt Port并收窄 locator；Hook command 复用 canonical governed runner，control hook fail closed，不新建平行 runner |
| SB1.10 | R2.3、R2.26、R2.41 | 直接扩充 Queue 泛型和 generation binding；hosted service 仅在证明 daemon/embedded 双 owner 后进入实施 |
| SB1.11 | R2.15、R2.36、R2.40 | 扩充 ToolExecutor public control surface与唯一 composition；依赖数量本身不作为整改依据 |
| SB1.12 | R0.2、R1.6、R2.13、R2.35、R2.41、R3.2 | Web Search/AgentCatalog 已覆盖；Model revision/binding 并入 model/generation 条目；Media registry 仅在证明跨包 mutation、双 owner 或 representation 泄漏后并入 hosted-service/facade 条目 |
| SB1.13 | R1.2、R2.11 | 先确认仓外 API 承诺；无消费者则删除旧 Environment facade，有确认消费者才定义最小 hosting/message/human seam |
| SB1.14 | R1.4、R1.5、R1.20、R2.9；durable Workflow 专项第 14 节 | 产品决定由专项承接；总账扩充 Agent-owned pool、incarnation lifecycle gate、Workflow 语义剥离、residency pin、settlement/release 和包根最小面 |
| SB1.15 | R3.1 | 直接扩充 authoritative error consumer migration |
| SB1.16 | R2.22、R3.2 | Activity/presentation typed contract 部分覆盖；cross-host DTO owner 需补消费者后决定总账增补 |
| SB1.17 | R2.12、R2.40 | 配置 owner与 Tool identity 部分覆盖；内置默认值和多个 policy 的共同变化证据补入既有条目 |
| SB1.18 | R3.1 | R3.1同时承接error owner拆分与namespaced/versioned ABI；先列serialized consumer，再一次性迁移并删除旧decoder |
| SB2.1、SB2.2 | R1.24、R2.23、R3.2 | Artifact 双命名已有；FileOps 继续复用 canonical Artifact storage，只治理内部 repository 命名和包根 consumer；GC reachability/pin generation 归唯一 Artifact owner，不另造 storage/collector |
| SB2.3、SB2.4 | R2.1、R1.6、R2.6、R3.2 | Workflow 面由既有条目承接；ServiceGateway 先区分合法 composition 输入与业务 consumer representation 泄漏 |
| SB2.5 | R2.5、R3.2 | 保留 CodeMap bounded context，扩充 typed query/index service、包根实现迁移与 concrete consumer 清单 |
| SB2.6 | R2.25 | 保留通用 artifact projection/reconciliation owner；扩充 Session read-model 迁入 runtime/session、全部 consumer migration、旧 export 删除与 typed registration gate |
| SB2.7、SB2.8 | R1.14、R2.12、R3.4 | config 按 domain/consumer 逐项归位；保留唯一 watching mechanism，Product reload policy 归既有原子 reload/composition 语义，删除 file_watch declaration 聚合与迁移残渣 |
| SB2.9 | R3.4 部分覆盖；owner 迁移暂无精确总账项 | R3.4 只承接失效 `common` 注释；Product 英文 wording owner 错置和 Runtime elision canonical owner 须先补完整消费者/共同变化证据，再决定扩充现有 owner 条目或新增编号 |
| SB2.10 | R3.2、R3.5 | private import与optional ML boundary 承接；RoutingModelService 仅候选 |
| SB2.11、SB2.12 | R2.22、R3.2、R3.5 | typed presentation、facade、eager import 分别承接；禁止按导出数量机械整改 |
| SB2.13 | R3.2、R3.5 | 已确认 private import 逐条迁移到 authoritative public seam 或同 owner 内聚 |
| SB2.14 | R0.0、R3.5 | 独立首切片：hermetic collection/import purity；不得安装 `pyte` 掩盖问题 |
| SB2.15 | 对应 owner 的现有 R；无统一新条目 | 只在相关子系统变更时基于 consumer 证据收窄，不建立“公共面大扫除”项目 |

### 11.2 未映射项的处理纪律

- 标记“需补证据后决定”的内容仍是候选，不得进入实施计划。
- 证据充分后优先扩充现有 R；只有现有 R 的 owner、生命周期和验收均无法承接时，才在核心总账新增编号。
- 核心总账更新后，本表只保留 evidence link，不在此维护完成状态。

## 12. 复审准入状态

| 条件 | 当前状态 |
| --- | --- |
| 区分事实、不变量、产品决定、候选实现与验收 | P0 已完成；P1 高风险项已修正；P2 明确仍为候选线索 |
| P0 owner/identity/lifecycle/durability/failure/recovery/cleanup | 已在第 6 节补齐 |
| P0 基础设施检索与复用决定 | 已列现有 seam；最终选择仍须在实施切片做 capability matrix |
| 必删旧入口、identity、state、consumer | P0 已列；P1/P2 随核心总账条目补齐 |
| 可复现 package inventory | 已完成；当前 193 个目录全部出现在矩阵，已删除缓存目录另有历史说明 |
| 与唯一总账逐项映射 | 已完成初始映射；尚未把增补内容正式回填核心总账 |
| 门禁不使用命名启发式 | 已修正为精确规则 + authoritative manifest |
| 测试、Pyright 与 fault injection | P0 已列验证下限；实际实施路径需重新核实测试目录 |
| hermetic architecture collection | 尚未实施，当前仍为 31 个 collection errors；这是代码整改的首个切片 |

因此本文可以进入复审，但仍不能直接驱动 P1/P2 批量重构。复审通过后，应先把确认内容合并到核心总账，再以核心总账和专项需求下发实施任务。
