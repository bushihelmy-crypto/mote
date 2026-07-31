# Kernel 分包长期治理计划

- 状态：Draft v8，总体架构已批准，可启动 Phase 0；Phase 1 代码迁移仍受 Phase 0 验收门禁约束
- 基线日期：2026-07-29
- 治理范围：`mote/kernel/`

本文定义 `mote.kernel` 的长期分包目标、所有权规则、迁移顺序和验收门禁，供架构评审与后续实施使用。

本文只治理 Kernel 层内部能力边界，并识别当前误放在 Kernel 中的上层策略；不重新评审
`contracts <- kernel <- runtime <- orchestration <- product` 五层架构，不借拆包改变 Agent 执行语义、模型 wire 协议、持久化格式或用户可见行为。

这里的“不改变持久化格式”指既有 rollout/session wire 必须继续可读，不要求新内存模型复刻旧结构。Runtime replay 负责把旧 wire 投影成新模型，禁止就地改写历史。这里的“不改变用户可见行为”包含 `mote`/`mote.tools` 已标记 stable 的顶级 API：兼容适配器可以保留在 Product facade，但旧模型不得反向进入 Kernel。源码 import 兼容层与持久化 wire reader、Product public adapter 是三类不同事物，治理规则分别处理。

目标不是一次性整理目录，也不是承诺目录十年不变，而是建立一套可以持续十年阻止结构债务无声增长的能力地图：不了解实现的人仅看包名，就能判断 Kernel 提供什么能力、一个新需求应由谁拥有，以及哪些依赖方向不可跨越。

历次评审意见、取舍理由和版本差异移至 [`kernel-package-governance-review-log.md`](./kernel-package-governance-review-log.md)。本文只保留终态约束、已决策事项、迁移阶段与验收门禁，避免长期规范退化为会议纪要。

---

## 1. 治理目标

### 1.1 总目标

治理完成后必须满足：

1. 一级包直接表达单 Agent Kernel 的业务能力，不以实现技术或历史阶段命名。
2. 执行循环、模型推理、命令协议、工具定义、结构化输出和观测语义各有唯一 owner；贫血 DTO 不为目录对称单独建包。
3. Kernel 只包含模型无关、IO 实现无关的单 Agent 执行语义和纯算法；可以通过 contracts port 编排异步外部能力，但不依赖具体 IO 技术、Runtime 实现或 Product 策略。
4. Prompt 跟随拥有其业务语义的能力，不建立“所有提示词统一存放”的中央仓库。
5. 模型 provider、认证、成本、故障转移、网络调用和持久化继续由 Runtime 拥有；Kernel 只通过 contracts port 调用。
6. XML/native 等 wire 实现集中在 commands；新增协议可以在 contracts identity、Runtime/Product 装配和旧 manifest codec 增加稳定映射，但不得复制 ToolCatalog、execution graph、output evaluation 或 inference engine。
7. 一级能力包间依赖形成可执行验证的有向无环图，不依赖 `TYPE_CHECKING` 或局部 import 掩盖概念循环。
8. 根包与各一级包的 `__init__.py` 只暴露经过评审的稳定门面，不聚合内部实现。
9. 不保留旧 import path、forwarding module、兼容 re-export、双实现或临时迁移 baseline；终态依赖矩阵、API 清单和禁止规则永久保留。
10. 每个提交保持仓库可构建、可测试；每个 Phase 可独立验收，并可回滚到进入 Phase 前的稳定点。
11. 普通功能需求原则上只修改一个 Kernel 能力包；跨三个以上一级能力包的改动必须在评审中说明原因。
12. 文件大小只作为变化轴审计信号，不为追求目录对称或低 LOC 机械拆包。

### 1.2 “零负债”的可验收定义

本文中的零负债不是零复杂度，也不是目录冻结，而是以下状态：

- 没有已知的 Kernel 内包级循环依赖。
- 没有两个包共同拥有同一状态真相源、registry、状态机或生命周期。
- 没有以 `common`、`shared`、`utils`、`misc`、`helpers` 命名的无领域 owner 包。
- 不以 `services`、`managers`、`engines` 作为一级横向技术分类。
- 没有通过私有字段、`getattr`、动态 import 或 import side effect 穿透能力边界。
- 没有“下一阶段再删除”的旧路径、alias 或兼容门面。
- Kernel/Runtime 内没有源码 import forwarding；Product stable facade adapter 与版本化持久化 reader 有明确 owner、测试和删除/保留策略，不视为架构残渣。
- 所有跨层依赖均指向 `contracts` 数据或 `contracts/ports` 中有业务语义的窄 Protocol。
- 持久化事件 tag、工具名、协议字段、输出 contract identity 不依赖 Python 模块路径。
- 每个公开符号都有明确稳定性级别；包内部实现不会因被偶然 import 而成为事实公共 API。
- 所有架构例外都必须精确到 import site，并具有同阶段删除条件；不允许可增长的包级白名单。

### 1.3 非目标

本治理不做以下事情：

- 不修改五层依赖方向。
- 不重写 ReAct、Review/Refine、输出校正或 durable recovery 算法。
- 不改变 XML/native 模型协议语义。
- 不改变模型路由、工具调用、事件流或结构化输出的用户可见行为。
- 不把 Runtime 的模型客户端、工具执行器、持久化、权限或会话能力下沉 Kernel。
- 不建设通用插件框架、通用 workflow 框架或第二套 Agent Engine。
- 不为减少单文件行数制造只有一个实现的抽象。
- 不在拆包阶段顺手修改 prompt 文案、评分规则或默认策略。
- 不用函数内延迟 import 规避正确的边界调整。

---

## 2. 当前基线

### 2.1 规模与现状

以 2026-07-29 当前工作树为基线：

| 当前区域 | Python LOC（约） | 当前主要职责 |
| --- | ---: | --- |
| `flow/` | 1,850 | 执行循环、图、恢复、状态、事件、领域操作 |
| `models/` | 970 | 模型调用、路由信号、产品级复杂度判定 |
| `output/` 与根部 output 模块 | 820 | 输出 contract、校验、迁移、绑定、流式快照 |
| `parser/` | 1,300 | 命令协议、解析、记录、媒体投影、协议 prompt |
| `prompt/` | 950 | Role、工具、压缩、Memory、后台任务等多领域提示词 |
| `think/` | 730 | 推理 Engine、请求、Prompt 组装 |
| `tools/` | 1,240 | 工具定义、schema 适配、Toolset 组合代数 |
| 根模块 | 340 | Agent 描述、运行状态、诊断、遥测 |
| 合计 | 约 7,796 | Kernel 当前全部能力 |

当前五层 import 方向健康：Kernel 未导入 Runtime、Orchestration 或 Product。主要问题发生在 Kernel 内部能力命名、变化轴和错误 owner，而不是五层方向违规。

### 2.2 当前内部依赖特征

当前主要关系为：

```text
flow -> think, parser, telemetry
think -> models, parser, prompt, output_stream
parser -> prompt, output_binding, think（类型引用）
models -> telemetry
```

其中 `think -> parser -> think` 即使部分边只在类型检查阶段存在，也说明推理和命令协议没有通过稳定 DTO/Protocol 解耦。`parser` 与 `prompt` 还共同承担协议渲染，使 Prompt 组装边界难以独立演进。

### 2.3 主要问题

#### P0：`AgentSpec` 不是 Kernel 叶子契约

当前 `AgentSpec` 同时包含四类所有权：

- 身份数据：`name`、`profile`。
- Kernel 执行选择：`command_protocol`、`think_kind`。
- Runtime 策略：`max_cost`、Memory 开关、消息观察方式。
- Product 策略：Coding Agent 默认 prompt、内置工具名、Skills、Browser、Canvas、子 Agent 等默认集合。

`RoleSchema` 又直接继承 `AgentSpec`，把这些字段固化成一个扁平部署 schema。将它原样搬到 `kernel.agent` 只会把错误 owner 包装成新目录。Phase 0 必须先形成字段级归属表：跨层稳定身份/序列化数据进入 contracts，Kernel 只消费执行必需 policy，Runtime 拥有运行策略，Product 组装默认 prompt 与能力集合。拆分后若 Kernel 只剩贫血 DTO，则不建立 `kernel.agent` 一级包；`AgentRunState` 跟随真正拥有其状态转换的 execution。

#### P0：`parser` 不能表达实际能力

`parser` 不仅解析文本，还负责：

- XML/native 协议选择。
- provider-independent `ModelTurn` 归一化。
- 工具调用与工具结果写回。
- 输出绑定能力协商。
- 多媒体结果投影。
- 协议相关 prompt section 和符号降级。

该包实际 owner 是“模型命令协议”，而不是 parser。继续沿用该名称会使未来的协议编码、记录或能力协商代码被误放到其他包。

更关键的是，当前 `CommandChannel` 直接消费 `BaseThinkEngine`、整个 executor 和 `MessageStore`，同时解析命令、生成 tool specs、读取 artifact、协商输出 binding、判定完成并写历史。它不是窄协议边界，而是次级编排器。目标必须改成纯数据边界：

```text
InferenceResult + DecodeContext
    -> ModelTurn

ModelTurn + tuple[ExecutedCommand, ...]
    -> HistoryProjection
```

其中：

- `InferenceResult` 是推理产生的原始文本、canonical tool calls 和可选 structured value，不带协议动作语义。
- `ModelTurn` 是 commands 唯一输出，继续承载 `TextAction`、`ToolCallAction`、`FinalCandidateAction`。
- commands 不认识 InferenceEngine，不等待后台推理任务。
- tool schema 输入是稳定 definition/catalog 数据，不是 ToolExecutor。
- history projection 返回消息 DTO，不调用 `MessageStore.add()`。
- execution 决定 record-call → effect → record-results → completion 的语义顺序；Runtime transaction port 决定原子写入、flush 和 durability frontier。
- artifact materialization 属于 Runtime 边界；commands 只投影已经解析的媒体引用/内容 DTO。

#### P0：`models` 混合 Kernel 语义与 Product 策略

`models/model_calls.py::generate` 是通用推理调用语义；`models/routing.py` 是纯路由信号构造，可属于 Kernel。但当前 `ModelRoute` 暴露 gateway、request transformer、session fact sink、artifact resolver 和 route metadata，是 Runtime 组合对象而非窄业务 port。

终态由 contracts 两阶段 `ModelInferencePort` 隔离 Runtime：

```python
class ModelInferencePort(Protocol):
    async def resolve(self, intent: InferenceIntent) -> ResolvedInferenceTarget: ...
    async def infer(
        self,
        target: ResolvedInferenceTarget,
        request: InferenceRequest,
    ) -> InferenceResult: ...
```

`InferenceIntent` 表达 resolve 前已知的语义能力需求：是否需要 tool calling、structured output、native schema、vision/PDF/multimodal、native tool search，streaming/continuation/resume 语义，以及 output contract 的表示需求和 routing signals；不得包含任何 endpoint/protocol 投影后的 wire 数据。Runtime 必须选择满足这些 requirements 的 target，不能先任意路由再依赖请求定稿阶段碰运气降级。

`ResolvedInferenceTarget` 只暴露 Kernel 完成请求定稿所需的稳定数据：route identity、command protocol identity/version、endpoint capability snapshot、response/tool representation capabilities、`capability_fingerprint`、`projection_compatibility_key` 及 target lease identity；不得暴露 gateway、transformer、credential、session fact sink 或 artifact resolver。fingerprint 必须覆盖 schema dialect/限制、tool name/description 限制、multimodal envelope、structured output、cache/resume 和 canonicalization 语义，不能只由若干 capability 布尔值拼成。路由、gateway、request transform、session facts、artifact resolution 均由 Runtime 实现内部处理。

固定时序为：Kernel 构造 `InferenceIntent`/routing signals → Runtime resolve capable target → Runtime 针对该 target 物化 `ToolBindingSnapshot` → commands 投影 tool/protocol sections → execution 协商 output binding → inference 组装最终 request → Runtime 对同一个 target 执行 infer。`resolve` 到 `infer` 期间 target identity 与 capability snapshot 不得静默变化；只有 `projection_compatibility_key` 完全相同且 continuation/resume 契约明确允许时，failover 才可复用 snapshot/projection/binding/request，否则必须返回 typed target-invalidated 结果，从重新 resolve、重新物化、重新投影和重新组装开始。route identity 不同即使 capability 相同也默认不得接续，除非 port 明确证明同一 `model_call_id`/resume identity 可跨 route 转移。Kernel 不遍历 Runtime service locator。

target lease identity、inference attempt fencing token 与 run transaction fencing token 是三个不同概念：lease 证明 resolved target 仍有效；attempt fencing 阻止过期 worker 接收/提交同一模型调用结果；run fencing 保护 Session/transaction 业务提交。inference checkpoint 必须记录 target identity、lease/attempt identity、capability/projection compatibility key、`model_call_id` 和调用状态。crash 后优先按 provider resume/idempotency 契约恢复未知响应；lease 失效时重新 resolve，但不得因新 target “看起来兼容”而重复付费或重复接受结果。是否可重试、查询、接续或必须进入人工/typed conflict，由 recovery matrix 对每种 provider capability 给出唯一结论。

`inference` 在本计划中只拥有“一次单 Agent 模型回合的生成语义”，不是所有 ML inference operation。Embedding、rerank、独立 image/audio processing、web search 不因调用模型而进入该包；multimodal、structured generation、reasoning/cached continuation 只有在属于 Agent model turn 时才进入。

但 `web_search` 和 `describe_image` 是具体任务能力，不属于通用推理。前者由 Product Web Search 拥有，后者由实际图像描述调用方拥有；压缩摘要等辅助任务也由各领域 owner 构造通用 inference request。Kernel 不为每种模型能力持续增加 convenience call，否则 audio、embedding、rerank 会自然堆入同一模块。

但 `models/complexity.py` 约 657 行，包含关键词、领域、可逆性、影响范围、评分、置信度和 tier 决策。其生产消费方是 `product.routing.squilla`，变化原因是产品模型选择策略，不是单 Agent 执行不变量，应迁至 Product。

#### P0：`prompt` 是中央字符串仓库

当前 `prompt/` 同时容纳：

- 单 Agent Role 与命令模板。
- Memory 指令。
- 上下文压缩格式和预算数学。
- Skills reserved-token 防护。
- 后台任务结果指针。
- 子 Agent 委派提示。
- 具体工具结果文案。

这些内容属于不同能力和不同层。按“它是字符串”集中，会让所有上层产品策略逐渐下泄 Kernel，也会令 `prompt` 成为修改热点。

#### P1：根目录存在领域孤儿

`output_binding.py` 属于结构化输出，`output_stream.py` 属于输出快照观测，`diagnostics.py` 与 `telemetry.py` 属于无 IO 的观测 seam。散落在根目录使能力地图不能表达所有权。

#### P1：`flow/services` 与 `container.py` 命名过泛

这些模块实际提供执行图节点使用的 observation、thinking、action、completion 和 output 操作，以及依赖集合。`services`/`container` 不能表达业务语义，也容易成为新增逻辑的默认收纳点。

#### P0：Tools 同时混合协议表示、catalog 与 Runtime 生命周期

该文件约 857 行，同时承担：

- XML/native nominal Toolset 类型。
- 只读 view 与 combined view。
- 组合与版本计算。
- rename/filter/prepare 等变换。
- approval policy 绑定。
- 协议与组合校验。
- manifest、索引和工具解析。

问题不只是文件过大：`XmlToolDefinition`/`NativeToolDefinition`、两套 Toolset/view/combine、schema renderer 和持久化 identity 中的 protocol 让 wire 协议成为工具的逻辑身份；同时 `for_run`、`for_run_step`、异步上下文、capability factory、permission binding 和 instruction blocks 又把 Runtime 生命周期与 Prompt 贡献塞入 Kernel。

目标不是拆文件，而是拆真相源：

- Kernel `ToolDefinition`/`ToolCatalog`：协议中立、不可变的语义定义、身份、组合、重命名与冲突检测。
- commands projection：把同一个 catalog 投影为 XML catalog 或 native tool schema，在此恢复协议级静态类型隔离。
- Runtime `ToolProvider`：capability factory、for-run/for-step、异步生命周期与刷新。
- Runtime permission binding：审批策略及 `RunContext`。
- Runtime/Product context source：静态或动态 tool instructions 的注入。
- 内存 `ToolCatalogIdentity`：不含 protocol；Runtime wire codec 继续读写现有 `{id, version, protocol}` manifest，将 catalog identity 与当前 command protocol 投影到旧 wire。

第三种命令协议的特有 decoder/projector 实现集中在 commands；contracts protocol identity、Runtime configuration/wiring、manifest codec 和 Product configuration surface 可以增加稳定身份值与装配映射，但不得复制 ToolDefinition、ToolCatalog、execution graph、output evaluation、inference engine 或组合代数。

#### P0：Output 混合 evaluation、delivery 与上层插件

当前 `kernel.output` 同时包含纯 contract/decoder/binding/migration 算法、Runtime turn-context source、跨层 graph commit Protocol，以及从 candidate 到 publication queued 的统一生命周期。它把三个 owner 合并为一个包：

- Kernel：candidate decode/validate/correct、binding selection、迁移路径算法。
- Runtime：transcript、commit、journal、publication delivery 与 crash consistency。
- Runtime context：每轮结构化输出 guidance 注入。

终态必须拆成 `OutputEvaluationState` 与 `OutputDeliveryState` 两个状态机。Kernel evaluation 产生 immutable contracts `AcceptedOutput`，包含 candidate value、contract ID/version、schema fingerprint、validator identity/version/provenance、migration provenance 和 correction count。Runtime transaction 先以幂等 operation stage 该 DTO，再原子提交 terminal frontier；它不重新 decode、validate 或调用模型，提交完成后返回 `CommittedOutput`。Kernel 不知道 publication，Runtime 不重新解释 candidate evaluation。

`AcceptedOutput` 一旦成功 stage 就成为该输出决策的不可变领域记录：commit 失败或进程崩溃时必须重试/恢复同一个 DTO，不得重新 evaluation。它作为 SessionEvent payload 或未决 TransactionRecord 的 staged payload 持有，不单独形成第三套历史。stage 前崩溃仍处于 evaluation frontier，可从已持久化 candidate 按原 validator/migration identity 恢复；若依赖版本不可用则明确阻塞恢复，禁止用当前版本静默重评。contract 版本漂移不能改变已接受值；需要显式、可审计的 migration 生成新的 immutable accepted record，并保留原 identity/provenance。

#### P0：恢复与事务边界通过 `Any`/callback 反向穿透

`FlowOutputService` 以 `memory: Any`、`drain_writes`、`reap_think` 等散参隐式表达同一个崩溃一致性事务；`ThinkCheckpoint` 又直接调用 Runtime journal runner。将 history、output、inference checkpoint 拆成三个独立 port 会制造跨 port 分布式事务，因此终态只暴露一个 run-scoped `ExecutionTransactionPort`：

- `record_model_turn(...)`
- `record_tool_results(...)`
- `reject_output(...)`
- `stage_accepted_output(AcceptedOutput, ...) -> StageResult`
- `commit_terminal_output(staged_output_id, ...) -> CommittedOutput`
- `recover_frontier(...) -> ExecutionRecoveryFrontier`

Runtime 实现可以组合 history、output、inference journal，但对 Kernel 暴露一个业务事务边界。`commit_terminal_output` 必须原子完成：协议历史投影、inference consumption、accepted output commit、checkpoint cleanup directive 与 terminal result；若存储技术无法单事务完成，Runtime 必须以可恢复 `TransactionRecord` 和唯一 reconciler 提供等价原子可见性，不能把部分提交暴露给 Kernel。`TransactionRecord` 只是 Runtime 完成未决提交的基础设施恢复真相，不是 Agent 业务历史；reconciler 完成后，业务 replay 只能观察对应 SessionEvent，不得读取 transaction record 推导第二套业务状态。Kernel 拥有事务意图与调用时机；Runtime 独占 flush、journal、checkpoint reap 和 reconciliation。

外部 Tool effect 无法与本地事务物理原子化，继续由 EffectLedger 的 precheck/started/completed 与 reconciliation 处理；`ExecutionTransactionPort` 不得伪装成远端 exactly-once。

所有 mutation 接收 `run_id`、`attempt_id`、稳定 `operation_id`、`fencing_token` 和适用时的 `expected_revision`，并返回 typed applied/already-applied/conflict/fenced/cancelled 结果。相同 operation ID 必须幂等；旧 lease worker 必须被 fencing 拒绝；取消与 commit 并发时由 recover frontier 给出唯一可继续状态。
- graph runner 只按 effect 分类触发生命周期 callback，命名为 `EffectAwareGraphRunner`。

#### P1：Telemetry 与 Output snapshot owner 不清

Kernel 不应导出 loguru logger；parse/协议问题应成为返回 diagnostics 或 typed observation，由 Runtime subscriber/backend 记录。trace/span ContextVar 可以保留为 run-scoped 传播机制。

`OutputSnapshotAccumulator` 则是增量 JSON、revision、value identity 和 invalidation 的输出投影状态机，归 `output/snapshots.py`；它通过显式 observer 发 snapshot event，不再用全局 active accumulator ContextVar。telemetry 只负责事件发射。

#### P1：`FlowServices` 是静态 service locator

把 `FlowServices` 改名为 `ExecutionDependencies` 不会降低耦合。每类 node 只能接收自己的窄依赖，例如 ObserveNode 接 ObservationPort、InferNode 接 inference dependency、ActNode 接 ActionPort、CommitNode 接 `ExecutionTransactionPort` 的窄 facade；graph builder 负责装配。禁止存在所有节点均可访问的“大依赖包”。

#### P2：公开入口稳定性不一致

`kernel.output` 和 `kernel.flow` 已形成明确 facade；`think`、`tools` 几乎没有包级门面，外层直接 import 内部模块。Runtime、Orchestration 和 Product 当前对 Kernel 内部路径存在较多直接引用，迁移前必须先定义目标公共面，避免目录重排后继续产生同类问题。

---

## 3. 所有权判定规则

### 3.1 允许进入 Kernel

只允许：

- 单 Agent 执行循环、状态转换和完成语义。
- provider-independent 的模型调用请求、响应归一化和纯路由信号。
- 模型命令协议的纯解析、格式化和历史投影。
- 协议中立的工具语义定义、不可变 catalog 和无 IO 组合代数。
- 结构化输出 contract、candidate evaluation/correction、binding selection、snapshot projection 和不可变迁移图算法。
- 不绑定具体 Product 能力的 prompt 模板与纯文本/token 算法。
- 通过注入 observer 发事件的无 IO telemetry seam。

### 3.2 必须留在或迁往 Runtime

- 模型 client、endpoint、认证、成本、重试、failover 和网络 IO。
- ToolExecutor、权限、MCP、sandbox、外部副作用与 EffectLedger。
- ToolProvider 的 for-run/for-step/async lifecycle、capability factory、permission binding 与 instruction context source。
- 会话、历史、压缩执行、资源存储和持久化。
- Output transaction、delivery/publication 状态机、migration registry 装配和 output turn-context source。
- EventBus 实现、日志 backend、trace exporter。
- Artifact resolver、文件和媒体读取。

### 3.3 必须迁往 Orchestration

- 多 Agent 身份、spawn、通信与调度。
- 后台任务生命周期、结果投递和 task result pointer 的领域格式。
- 通用 workflow、自动化触发和并发配额。

### 3.4 必须迁往 Product

- Coding Agent 默认 prompt 与产品人格。
- 子 Agent 委派策略和文案。
- Skills 注入策略及其安全策略入口。
- 内置工具专用文案。
- Squilla 等模型复杂度评分与 tier 决策。
- 具体 provider/tool/service adapter。

### 3.5 Prompt 归属与统一组装规则

不建立 `kernel.prompting` 一级包。Prompt 不因“是文本”而共享 owner，判定方式是：

1. 文本描述谁的业务规则，就归谁。
2. 单 Agent 执行不可缺少、且与具体产品无关的模板跟随 `execution` 或 `inference` 的具体能力模块。
3. Runtime 生成的 per-turn 内容由 Runtime source 拥有，通过 port 注入，不进入 Kernel 静态模板上半区。
4. Product 能力描述由 Product 组装，不通过 Kernel 中央 registry 注册。
5. 跨协议符号、vocabulary 和协议 prompt block 全部由 `commands` 拥有；inference 只消费已经选择的渲染数据/函数。
6. 通用 token 算法若只是 Runtime context 消费则归 Runtime；只有形成跨层稳定 DTO 时才进入 contracts，算法实现不因“被多处调用”进入 contracts。

Prompt 文案分散，但最终组装不分散。contracts 定义 `PromptSection` 和 `ProtocolVocabulary` 数据契约。section identity 不是自由字符串，而是稳定的 `(owner_capability, section_key, region, version)`；内容、order 与 history policy 是该 identity 的数据。装配期显式注册 owner capability 与允许贡献的 section namespace，只有登记 owner 能替换自己的同 identity 贡献；重复 identity、跨 owner 冒名或 region 漂移必须失败。这里不建立通用插件 registry，只定义 PromptAssembler 可验证的所有权表。inference `PromptAssembler` 独占：

- section identity 冲突检测与确定性排序。
- `STATIC`、`DYNAMIC`、`PER_TURN` 区域及缓存边界。
- STATIC 区域 placeholder/易变内容禁令。
- per-turn section 不进入 history 的约束。
- 全部 section 合并完成后的最终 protocol symbol lowering。

数据路径固定为：

```text
Runtime resolves ResolvedInferenceTarget
  └─ selects matching commands protocol bundle
       ├─ decoder/projectors -> execution
       └─ PromptSection + ProtocolVocabulary DTO -> inference.PromptAssembler
                                                  -> final InferenceRequest
```

inference 不 import commands；Runtime 只做 bundle 选择和数据注入，不拥有组装顺序。

protocol bundle 必须具有稳定 identity/version。最终 `InferenceRequest` 与 inference checkpoint 同时记录 protocol identity/version、vocabulary fingerprint、tool projection fingerprint、PromptSection set fingerprint 及 target `projection_compatibility_key`。恢复时任一 fingerprint 不一致都必须重新定稿请求；已经发出的未知结果调用则按 recovery matrix 处理，禁止用新 bundle 静默解释旧响应或续接旧历史。

`ProtocolContext` 不作为公共万能上下文。commands 的三个变化轴使用独立不可变输入：

- `DecodeContext`：protocol identity、valid dispatch names、output representation 和本轮 endpoint capability snapshot。
- `ToolProjectionContext`：protocol vocabulary 与 materialized tool definitions，不含 executable capability。
- `HistoryProjectionContext`：protocol identity、已执行 command/result facts 和媒体引用投影规则。

字段若只被一个操作使用则保持显式参数；只有生命周期和变化轴相同的字段才进入对应 context。三个 context 均不得携带 service、registry、callback 或可变 Runtime 对象。

### 3.6 DTO 晋升到 contracts 的准入规则

DTO 至少满足以下一项才可进入 contracts：

- 被 Kernel 与 Runtime 共同实现或消费。
- 需要持久化、事件化或跨进程传输。
- 是正式 contracts port 的参数或返回值。
- 生命周期明确长于任一 Kernel 包实现。

“为了消除 Kernel 同层 import”不是晋升理由。否则 DTO 归生产它或定义其输入语义的最低 owner，上层通过批准矩阵直接依赖。

按此规则，当前目标归属调整为：

- `InferenceResult`：进入 contracts，因为 inference checkpoint 需要 Runtime 持久化/恢复。
- `ModelTurn`：保留 contracts，因为它进入执行、历史与恢复边界。
- `DecodeContext`、`ToolProjectionContext`、`HistoryProjectionContext`、`ExecutedCommand`、`HistoryProjection`：归 commands；execution 已合法依赖 commands。
- `MaterializedToolCatalog`：进入 contracts，作为 Runtime ToolProvider 输出与 commands projection 输入，也是恢复校验数据。

### 3.7 Inference 的准入边界

`kernel.inference` 只拥有一次单 Agent 模型回合的生成语义，包括文本/多模态输入、结构化生成、reasoning continuation、缓存响应续接及流式输出归一化。它不是所有 ML inference operation 的总包；embedding、rerank、搜索、图片描述、音频处理等任务由各自 owner 通过 Runtime model capability 实现。

### 3.8 三个事件平面

三个平面可以由同一业务动作派生，但只能有一个状态真相源。三类事件必须携带可关联的
`run_id`、`attempt_id`、`operation_id` 和适用的 revision；关联 ID 只用于追踪同一事实的不同投影，不能把非 durable 事件提升为恢复依据：

| 平面 | 用途 | 可靠性/背压 | 是否参与恢复 | Owner |
| --- | --- | --- | --- | --- |
| `RunEvent` | 调用方可消费的稳定执行进度流 | run-scoped 有界队列；生产者受背压；取消随 run | 否，只是进度投影 | Kernel execution |
| `ObservationEvent` | trace、metrics、diagnostic、性能观测 | 可采样、可丢；关键 diagnostic 必须先进入语义结果 | 否 | Kernel 定义 observation，Runtime backend |
| `SessionEvent` | journal/replay 的持久化业务事实 | durable、按 revision/fencing 提交 | 是 | contracts 事件 owner + Runtime transaction |

同一事实允许产生三种投影，例如 terminal commit 先由 Runtime transaction 写 `SessionEvent`，成功后 execution 发 `RunEvent`，observer 再派生 `ObservationEvent`。转换 owner 是完成该状态转换的组件；Observation/Run event 不得反向成为状态真相源，也不得据此重建 Session。若 durable commit 失败，不得先发出表示成功的 RunEvent；ObservationEvent 可以记录失败尝试，但不得宣告业务完成。

`DecodeResult` 显式分为 `protocol_issues` 与 `observation_diagnostics`：

- `ProtocolIssue`：影响语义，例如 unknown command、invalid arguments、unsupported representation；execution 必须做出 reject、feedback、skip 或 terminate 等明确语义决策，必要时写入 history/SessionEvent，完成语义处理后才可附带派生 ObservationEvent。
- `ObservationDiagnostic`：不影响语义，只供 trace/log/metrics，可直接转换为 ObservationEvent，可采样、可丢弃，不进入模型反馈或恢复状态。

`RecoveryApplied` 若改变解析结果或后续动作，属于 ProtocolIssue/Session fact；只有采用了语义等价路径时才属于 ObservationDiagnostic。禁止把 `ProtocolIssue` 降级为普通 telemetry 后继续执行。

### 3.9 SLO 与硬不变量归属

`kernel.execution` 只拥有保证算法终止/内存有界所必需的硬安全上限，例如 graph 最大 transition 和 RunEvent 有界缓冲的不变量。recovery 秒数、disk barrier、shutdown timeout、资源配额、性能目标和告警阈值归 Runtime/Product/运维配置。Phase 0 必须对现有 `flow/slo.py` 逐字段归属，禁止原样迁移 `RuntimeSLO`。

---

## 4. 目标架构

### 4.1 一级能力包

建议目标结构：

```text
kernel/
├── execution/                # 执行循环、图、恢复、结果与运行事件
├── inference/                # provider-independent 模型推理语义
├── commands/                 # 模型命令协议及历史投影
├── tools/                    # 协议中立工具 catalog 与纯组合代数
├── output/                   # 输出评估、绑定、快照投影与迁移算法
└── telemetry/                # 无 IO 的 trace/span/typed event seam
```

不预设 `agent/` 或 `prompting/`。Agent 配置先按 owner 拆分；Prompt 跟随 commands、inference、execution 或上层具体能力。二级包只在已经证明存在两个以上独立且长期稳定的子领域时建立。

### 4.2 目标目录

```text
kernel/
  __init__.py                 # 只导出经过评审的顶级稳定门面

  execution/
    __init__.py               # AgentFlowEngine、FlowResult、公开运行事件
    engine.py                 # 薄执行门面与 run lifecycle
    context.py                # FlowContext、预算裁决
    state.py                  # 单次 flow 内部状态
    result.py
    events.py
    limits.py                 # 仅算法终止/有界缓冲硬上限
    graph_runner.py           # EffectAwareGraphRunner
    recovery.py               # 只依赖 ExecutionTransactionPort/frontier DTO
    graph/
      core.py
      nodes.py
      react.py
      review_refine.py

  inference/
    __init__.py               # 稳定请求与 Engine API
    engine.py                 # 原 ThinkEngine
    request.py
    invocation.py             # 仅调用 ModelInferencePort
    routing_signals.py
    prompt_assembly.py
    templates.py              # 仅推理组装自身拥有的模板片段

  commands/
    __init__.py               # 协议转换与 factory 的稳定入口
    decoder.py                # CommandDecoder: InferenceResult -> DecodeResult
    tool_projection.py        # ToolProjector Protocol/公共输入语义
    history.py                # HistoryProjector，不写 store
    diagnostics.py            # UnknownCommand/RecoveryApplied 等纯 DTO
    factory.py
    symbols.py                # 协议 vocabulary 与 prompt block
    native/
      protocol.py
      tool_projection.py
    xml/
      protocol.py
      tool_projection.py
      lexer.py
      recovery.py

  tools/
    __init__.py               # 稳定 ToolCatalog/ToolDefinition API
    definition.py             # 单一协议中立 ToolDefinition
    catalog.py                # ToolCatalog
    composition.py
    transformations.py
    validation.py
    resolution.py

  output/
    __init__.py
    contract.py
    binding.py
    evaluation.py             # OutputEvaluationState
    snapshots.py              # 增量解析/revision/invalidation；显式 observer
    migration_graph.py        # 不可变路径图，不拥有部署 registry

  telemetry/
    __init__.py
    context.py                # trace/span contextvars
    spans.py                  # observer 注入与 span helper
    events.py                 # typed observation 发射，不公开 logger
```

目录中的文件名表达已确认的职责，不是机械建包清单。尤其不预建 `operations/`：节点私有逻辑留在 graph/node，只有跨拓扑复用且具有独立不变量的执行语义才能提升为具名模块。

### 4.3 目标内部依赖方向

终态批准矩阵如下，行是 import 发起方，列是目标包：

| From \\ To | `commands` | `tools` | `output` | `telemetry` | `inference` | `execution` |
| --- | :---: | :---: | :---: | :---: | :---: | :---: |
| `commands` | — |  |  |  |  |  |
| `tools` |  | — |  |  |  |  |
| `output` |  |  | — |  |  |  |
| `telemetry` |  |  |  | — |  |  |
| `inference` |  |  | ✓ | ✓ | — |  |
| `execution` | ✓ |  | ✓ | ✓ | ✓ | — |

矩阵规则：

- 同包内部依赖不检查；未列出的跨包边一律禁止。
- `contracts` 与标准库不计入矩阵；其他三方库按各包职责审计。
- production 和 test 中的 `TYPE_CHECKING` import 同样计入概念依赖。
- 测试可以白盒 import internal 模块，但不能引入生产代码禁止的包间边。
- commands 只消费 `InferenceResult` 数据并产出 `ModelTurn`；inference 不反向依赖 commands。
- commands 不依赖 telemetry；`CommandDecoder` 返回分别包含 `protocol_issues` 与 `observation_diagnostics` 的 `DecodeResult`。execution 必须先处理 ProtocolIssue 的业务语义，只有 ObservationDiagnostic 或完成语义决策后的附带投影可以直接发 observation event。
- commands 只消费 contracts `MaterializedToolCatalog`，不依赖 kernel.tools；XML/native projection 类型只存在于 commands。若 Phase 0 发现必须调用 ToolCatalog 算法，须以具体不变量重新评审依赖矩阵，不能预留该边。
- commands 只声明 output representation capabilities；execution 调用 output negotiation，commands 不依赖 output implementation。
- inference 可以使用 output 的 snapshot projection 与 binding 数据；output 不反向依赖 inference，因此不形成循环。
- telemetry 只依赖 contracts、标准库及批准的无 IO tracing API；禁止导出具体 logger/backend。
- 该矩阵是永久架构契约，不属于迁移 baseline，Phase 7 不删除。

### 4.4 稳定公共面

公共 API 分为三类：

- `stable`：允许跨层生产依赖；兼容策略必须在发布策略中明确。
- `provisional`：允许生产试用，但必须标注评审期限，最长一个发布周期后升为 stable 或退回 internal。
- `internal`：外层生产代码禁止 import；不因 Python 可访问或测试白盒使用而成为公共 API。

候选公共入口（新架构符号初始均为 `provisional`）：

```python
from mote.kernel.execution import AgentFlowEngine, FlowResult, RunEvent
from mote.kernel.inference import InferenceEngine, InferenceRequest
from mote.kernel.commands import CommandDecoder, HistoryProjector, ToolProjector
from mote.kernel.tools import ToolCatalog, ToolDefinition
from mote.kernel.output import OutputContract, text_output_contract
```

`InferenceResult`、`ModelTurn`、`MaterializedToolCatalog` 和跨层 `ToolBindingSnapshot` identity 满足 contracts 晋升条件；`DecodeContext`、`ToolProjectionContext`、`HistoryProjectionContext`、`ExecutedCommand`、`HistoryProjection` 与 decode diagnostics 由 commands 拥有。内部 graph node、XML lexer、catalog view 和模板常量均为 internal。

`ThinkEngine` 同步改名为 `InferenceEngine`，不保留旧类 alias。`__all__` 只控制便捷导出，真正边界由外层 production import 扫描强制执行。

只有迁移前已经对外承诺且兼容行为明确的 API 保持 `stable`。上述新符号至少经过一个完整迁移 Phase、直接 consumer contract tests 和 wheel 隔离验证后，才能通过评审升为 stable；未形成真实外层消费面的 internal 类型不为清单对称而公开。

### 4.5 四类兼容表面

治理必须分别审计，禁止用一条“无兼容层”规则覆盖四类表面：

| 表面 | 示例 | 策略 |
| --- | --- | --- |
| Kernel internal API | graph node、XML lexer、catalog view | 可原子删除，不保留 alias/forwarder |
| Monorepo 跨层 API | `mote.kernel.*` 被 Runtime/Product 生产代码使用 | 按 stable/provisional/internal 清单治理 |
| 顶级用户 API | `mote.NativeToolset`、`mote.tools.NativeFunctionToolset` | 保持兼容；Product facade adapter 转换到新 ToolProvider/ToolCatalog |
| 持久化 wire API | rollout `toolset_manifest` | 旧格式持续可读且继续按原结构写出；Runtime codec 投影，不改写历史 |

Product facade adapter 是公共 API 的反腐层，不是 Kernel 兼容残渣：

- 保留现有构造、组合和传入行为，内部产生新 ToolProvider/ToolCatalog。
- 标记 deprecated 时必须给出新 API、迁移文档和明确的 major-version 删除条件。
- 未经独立 major-version 发布决策，不得从 `mote`、`mote.tools` 的 stable facade 删除旧类型。
- Kernel、Runtime domain implementation 不得 import Product adapter，也不得围绕旧类型继续设计。

---

## 5. 当前内容的目标归属

### 5.1 Kernel 内部迁移

| 当前路径 | 目标路径 | 说明 |
| --- | --- | --- |
| `agent_spec.py` | 按字段拆至 contracts/Runtime/Product；Kernel policy 由消费能力拥有 | 禁止原样搬迁 |
| `run_state.py` | `execution/state.py` 或 contracts checkpoint DTO | 先区分状态转换 owner 与序列化载体 |
| `flow/*` | `execution/*` | 执行循环能力 |
| `flow/services/*` | graph/node 或经证明的具名 execution 模块 | 不预建横向 operations 包 |
| `flow/services/container.py` | 删除，由 graph builder 按 node 注入窄依赖 | 禁止保留 service locator |
| `think/*` | `inference/*` | provider-independent 推理 |
| `models/model_calls.py::generate` | `inference/invocation.py` | 收敛为 ModelInferencePort 调用 |
| `models/routing.py` | `inference/routing_signals.py` | 纯信号构造 |
| `parser/*` | `commands/*` | 命令协议而非纯 parser |
| `output_binding.py` | `output/binding.py` | 输出能力协商 |
| `output_stream.py` | `output/snapshots.py` + telemetry observer | 状态归 output，事件发射归 telemetry |
| `diagnostics.py`、`telemetry.py` | `telemetry/context.py`、`spans.py`、`events.py` | 删除 logger，只保留无 IO 观测 seam |
| `output/engine.py` | `output/evaluation.py` | 仅保留 candidate/correction 状态 |
| `output/migration.py` | `output/migration_graph.py` | 不可变路径图，不拥有部署 registry |
| `flow/recovery.py::DurableFlowRunner` | `execution/graph_runner.py::EffectAwareGraphRunner` | 名称匹配实际职责 |
| `flow/think_checkpoint.py` | `execution/recovery.py` | 只消费 ExecutionTransactionPort/frontier DTO |
| `prompt/refs.py` | `commands/symbols.py` | 协议符号 vocabulary 由协议 owner 拥有 |
| Kernel 自有推理模板片段 | `inference/templates.py` | 只保留推理组装自身语义 |

### 5.2 迁出 Kernel

| 当前内容 | 目标 owner | 原因 |
| --- | --- | --- |
| `models/complexity.py` | `product/routing/squilla/` | 产品模型选择策略 |
| `models/model_calls.py::web_search` | `product/web_search/` | 搜索任务能力 |
| `models/model_calls.py::describe_image` | 实际图像描述 owner | 具体任务能力，不属于通用 infer |
| `prompt/agent.py` 的委派文案 | `product/agents/` | 多 Agent 产品能力 |
| `prompt/tools.py` 的具体工具文案 | `product/toolsets/` | 内置工具产品语义 |
| `prompt/task_result_pointer.py` | Orchestration task owner 或 Product adapter | 后台任务领域格式 |
| `prompt/compaction_format.py` | `runtime/context/compaction/` | 压缩执行能力 |
| `prompt/context_budget.py` | `runtime/context/history/` | 上下文窗口管理策略 |
| `prompt/memory.py` | Runtime context 或 Product agent policy | 需按静态协议与产品文案拆分评审 |
| Skills reserved-token 策略入口 | `product/skills/` | Skills 产品策略；纯算法 owner 由实际跨层消费决定 |
| `output/context_source.py` | `runtime/context/turn/` | Runtime per-turn 插件 |
| `output/graph_service.py::GraphOutputCommitter` | `contracts/ports` | 跨层装配 Protocol |
| output commit/publication lifecycle | Runtime output/session owner | delivery、journal 与 publication 生命周期 |
| Tool capability factory 与 run/step lifecycle | `runtime/tools/` | Runtime 资源生命周期 |
| Tool approval predicate/binding | `runtime/tools/permission/` | 权限执行策略 |
| Tool instruction 注入 | Runtime/Product context source | Prompt 贡献跟随实际 owner |

### 5.3 Tools 概念拆分边界

Kernel 终态只保留：

- `ToolDefinition`：协议中立的逻辑名称、aliases、description、调用 identity 和可选静态参数 schema；不含 capability factory、approval 或协议 renderer。动态 schema 不在此处成为真相源。
- `ToolCatalog`：不可变逻辑 definitions 集合、组合、rename、显式 `select(names)`、冲突检测和逻辑 version。
- `ToolCatalogIdentity`：内存中的逻辑/recovery identity，不含 XML/native protocol；wire codec 仍投影旧 manifest shape。

动态工具采用两阶段模型：

```text
Product/Runtime ToolProvider
    + logical ToolCatalog
    -> BoundTool（Runtime only，可执行句柄）
    -> MaterializedToolCatalog（本 run/step 的确定 schema，contracts DTO）
    -> commands ToolProjector
    -> XML/native wire representation
```

Runtime Provider 是本轮可执行集合与动态 schema 的真相源；commands 只消费已物化 catalog，不触碰 capability。permission、feature flag、user/model/session 策略在 Runtime/Product 物化阶段执行。Kernel `select(names)` 只是确定性 value transform，不接受隐藏外部状态的 policy predicate。

每个模型回合使用一个不可变 `ToolBindingSnapshot`，平行关联：

- `MaterializedToolCatalog`：本轮模型可见定义，不含执行句柄。
- Runtime `BoundToolRegistry`：dispatch name 到 executable capability。
- snapshot identity：catalog version、binding revision、session/run/turn identity。

`BoundToolRegistry` 和 capability 句柄始终留在 Runtime，不通过 snapshot DTO 暴露给 Kernel。Kernel 接收 `ToolBindingSnapshot` 的 immutable identity/catalog 部分；执行时调用 `ToolExecutionPort.execute(snapshot_id, command)`，由 Runtime 在 pinned registry 中解析。

execution 必须用同一 snapshot 生成 prompt/tool specs并解析、执行该轮命令。Provider 只允许在下一次 inference 前刷新；action 完成前 snapshot 不变。dispatch 必须携带 snapshot revision，禁止回退到“当前最新” registry。durable inference/turn checkpoint 记录 snapshot identity；resume 无法恢复同一 revision 时返回 typed recovery conflict，绝不拿 v2 capability 执行模型基于 v1 schema 生成的调用。

Runtime 必须为 snapshot 定义有界保留与重建策略：至少保留到所属 run terminal 且所有 checkpoint/effect reconciliation 完成；session 仍可 resume 时，持久化足够的 provider/catalog descriptor 与 target capability identity，以重建相同可见 schema 和 dispatch binding。MCP/provider 已消失或 executable identity 无法安全重建时返回 typed unrecoverable-binding conflict，不得绑定到同名最新工具。只有不存在活跃 run、未决 checkpoint、effect reconciliation、transaction record 或 publication reference 时才可 GC pinned registry/snapshot；长会话按已完成 run 回收，不按 session 生命周期无限保留。Phase 0 必须定义 descriptor 最小字段、retention lease、GC 扫描与恢复测试。

Product legacy Toolset adapter 只能单向产出新 `ToolCatalog`/Runtime `ToolProvider`；传入 Kernel 边界的对象必须已经是新模型。架构测试禁止 Kernel import legacy adapter，隔离测试禁止 legacy mutable object、subclass hook 或 XML/native 双类型泄漏进 ToolCatalog、ToolBindingSnapshot 和 commands API。

commands 分别拥有 XML/native tool projection 及其静态结果类型。Runtime 拥有 `ToolProvider` lifecycle、`BoundTool`、capability binding 和 permission decoration。Tool instructions 由 Runtime/Product context source 投影。

当前 `XmlToolset`/`NativeToolset`、两套 definition/view/combine 不进入 Kernel 终态 API。Runtime session wire 继续保持现有 `toolset_manifest=[{id, version, protocol}]`：读取时拆成 catalog identity 与 protocol 校验输入，写入时重新投影为同一结构。禁止改写既有 rollout，也不新增第二个并行 manifest 字段。

### 5.4 Tool 与 Workflow 的参数 schema 边界

`contracts` 只提供跨包交换的 schema DTO；现有 `SchemaDocument`、`NativeToolSchema` 是否足够由 Phase 0 审计决定。`annotation_to_json_schema` 是编译算法，不是 DTO，不因两个包共同调用就进入 contracts。

终态规则：

- 静态 Tool 参数 schema 的纯构造算法可由 `kernel.tools` 拥有；run/step 动态 schema 由 Runtime ToolProvider 物化，`MaterializedToolCatalog` 是本轮权威；XML/native wire 包装由 commands projection 拥有。
- Workflow schema 构造由 Orchestration workflow owner 拥有，并承诺 workflow input/node 参数语义。
- 两者交换或持久化时使用 contracts 中的中性 schema DTO/JSON Schema dialect 标识。
- 不复制一份声称相同的 helper；各 owner 优先直接使用 Pydantic/JSON Schema 标准能力表达自己的规则。
- 若实施审计证明两者确有完全相同的规范化、引用展开和错误语义，必须另立 ADR 决定一个中性算法 owner；在此之前不把实现塞入 contracts，也不让 Orchestration import `kernel.tools` internal adapter。

---

## 6. 分阶段迁移计划

### Phase 0：边界设计与事实冻结

目标：先闭合领域真相源、端口归属和数据流，再允许任何目录迁移。

工作项：

1. 逐字段拆解 `AgentSpec`，形成 contracts、Kernel execution policy、Runtime policy、Product defaults 的归属表及新组合方式。
2. 在 contracts 定义 `InferenceResult`：原始文本、canonical tool calls、可选 structured value；它是 commands 唯一推理输入。
3. 固化 `InferenceResult -> ModelTurn`：`ModelTurn` 只表示 commands 产生的 provider-independent 语义动作。
4. 在 commands 定义独立 `DecodeContext`、`ToolProjectionContext`、`HistoryProjectionContext` 及 `ExecutedCommand`、`HistoryProjection` DTO；commands 返回消息数据，不操作 MessageStore，不建立万能 context。
5. 定义单一 `ExecutionTransactionPort` 聚合 history、accepted output、inference consumption/checkpoint 与 terminal result 的共同 crash frontier；execution 决定意图，Runtime 独占原子性和 durability。
6. 审计 artifact/media：Runtime 完成 byte materialization，commands 只处理可投影数据。
7. 审计 Tool/Workflow schema DTO 是否足够，并记录各自算法 owner。
8. 定义协议中立 `ToolDefinition`/`ToolCatalog`、Runtime `ToolProvider`/pinned `BoundToolRegistry`、`MaterializedToolCatalog`、由 Runtime 物化的 contracts `ToolBindingSnapshot`、按 snapshot 执行的 `ToolExecutionPort` 与 commands tool projection 数据边界；Kernel 不接触 registry/capability。
9. 定义 snapshot 隔离与保留：target resolve 后、每次 inference 前物化一次，action 完成前冻结，dispatch 校验 revision，checkpoint 记录 snapshot identity；明确 durable descriptor、至少保留至 run terminal/reconciliation 完成的 lease、GC 安全条件及 provider 消失时的 typed conflict。
10. 定义不含 protocol 的内存 catalog identity，并定义旧 manifest wire codec：继续读写 `{id, version, protocol}`，分别校验 catalog 与当前 command protocol。
11. 将 `OutputLifecycleState` 拆为 contracts `OutputEvaluationState` 与 `OutputDeliveryState`，定义 immutable `AcceptedOutput` 及 stage/commit frontier；事务端口只提交已 stage 的 AcceptedOutput 并返回 `CommittedOutput`。
12. 在 contracts 定义 `ExecutionTransactionPort`、`ExecutionOperationContext`、typed applied/already-applied/conflict/fenced/cancelled result 与 `ExecutionRecoveryFrontier`；所有 mutation 携带 run/attempt/operation identity、fencing token 和适用的 expected revision，不暴露 memory、drain、journal runner 或 reap_think。
13. 产出 `zdocs/architecture/execution-recovery-matrix.md`：逐步列出 model resolve、request finalized、model call issued/response unknown/resumed/completed、tool result、reject、AcceptedOutput stage、terminal commit、checkpoint cleanup、terminal result 前后 crash 的恢复结果、幂等键、fencing、reconciliation owner、原子/最终一致边界和 cancellation 竞态；覆盖 target lease 失效、route 变化、同一 model_call_id 查询/接续、避免重复付费、双 worker resume、迟到 worker、重复结果和 publication retry。
14. 明确 output snapshot 的纯状态/observer 接口，以及 migration graph 与 Runtime registry 装配边界。
15. 生成 Kernel 文件、LOC、内部依赖边、外层生产消费方和公开 re-export 基线。
16. 将 4.3 节矩阵实现为架构测试；临时边精确到 import site，不设包级 wildcard。
17. 建立 stable/provisional/internal API 清单和外层 production import 门禁。
18. 修复 `ztest/prompts` 收集路径并移除 pytest 默认 ignore；在此之前它不计入永久最低门禁。
19. 完成四类兼容表面审计：Kernel internal、monorepo 跨层、顶级用户 API、持久化 wire；为每个现有 Toolset 导出和 manifest fixture 标记策略。
20. 固化 RunEvent/ObservationEvent/SessionEvent 可靠性矩阵、转换 owner、身份关联和禁止反向控制规则；commands `DecodeResult` 分离 ProtocolIssue 与 ObservationDiagnostic，并为每类 issue 固定允许的语义决策。
21. 对 `flow/slo.py` 逐字段审计，只保留 Kernel 硬安全上限，其余迁 Runtime/Product/运维配置。
22. 定义两阶段 `ModelInferencePort.resolve(InferenceIntent) -> ResolvedInferenceTarget` 与 `infer(target, request)`：产出 InferenceIntent 语义能力需求表；固定 target capability fingerprint、projection compatibility key、lease/attempt fencing DTO、resolve→tool materialization→projection→output negotiation→request assembly→infer 时序，以及不兼容 failover 必须整轮重建的 typed 结果；确认 gateway、transformer、credential、session fact 和 artifact resolver 不出现在 Kernel 可见接口。
23. 定义 `PromptSection` 的 namespaced identity、`ProtocolVocabulary`、唯一 `PromptAssembler` 及 resolved-target 驱动的 protocol bundle 注入路径；protocol bundle、最终 request 和 checkpoint 记录 protocol/vocabulary/tool projection/section set fingerprints；用契约测试固定 owner 注册、冒名/冲突、排序、缓存边界、placeholder、history policy 与恢复不漂移。
24. 产出 contracts DTO 晋升清单：逐个记录 DTO 跨越的边界、producer/consumer、是否持久化、版本兼容 owner，以及不能留在最低 owner 的理由；架构评审拒绝仅为消除同层 import 而晋升的 DTO。

验收：

- 基线可以由脚本重复生成。
- 新增向上依赖或未批准的 Kernel 内部边会直接失败。
- 每条临时边都有对应迁移 Phase 和删除条件。
- `AgentSpec` 字段归属、推理/命令 DTO、ToolCatalog/Provider/snapshot、两个 output 状态机、事务/恢复端口和三事件平面均有可测试接口定义。
- recovery matrix 对每个 crash point 给出唯一恢复结果，所有 mutation 的 idempotency/fencing/cancellation 语义有 consumer contract tests。
- InferenceIntent requirements、capability/projection fingerprints、target lease/model call crash matrix、protocol bundle/request fingerprints 和 contracts DTO 晋升清单均已形成可评审产物。
- ModelInferencePort 不暴露 Runtime 组合结构；resolve 与 infer 使用同一 target lease，只有明确兼容且允许接续的 target 才能复用请求资产；PromptAssembler 的唯一 owner 与不依赖 commands 的注入路径有契约测试。
- Phase 0 未完成前，Phase 1 及后续目录迁移阻塞。

### Phase 1：迁出错误 owner

目标：先消除策略下泄，不在错误内容仍存在时重命名容器。

工作项：

1. 将 `models/complexity.py` 迁至 `product.routing.squilla`。
2. 将后台任务 result pointer renderer 迁至其领域 owner。
3. 将子 Agent 委派、具体工具、Skills 策略 prompt 迁至 Product。
4. 将 compaction format 与 context budget 迁至 Runtime context owner。
5. 审计 `memory.py`，将 Kernel 执行协议与产品/Runtime 文案分开。

约束：

- 同一提交更新生产调用方和测试。
- 不保留 Kernel 旧模块或 re-export。
- 不在本阶段修改文案或评分算法。

验收：

- `kernel.models` 不含产品 tier/complexity 决策。
- `kernel.prompt` 不再承载上层能力文本。
- Kernel 的 Product/Orchestration 特有名词审计无遗留。

### Phase 2：建立叶子能力包与协议中立 ToolCatalog

目标：先稳定低耦合 owner，为后续高 fan-in 包迁移提供目标入口。

工作项：

1. 按 Phase 0 设计拆除混合 `AgentSpec`；不创建贫血 `agent/`。
2. 将 `AgentRunState` 的状态转换部分归 execution，跨层 checkpoint DTO 归 contracts。
3. 将 `output_binding.py` 收入 `output/binding.py`；commands 只返回 capabilities，execution 调用 negotiation。
4. 将 output 生命周期拆为 Kernel evaluation 与 Runtime delivery；迁出 context source 和 graph commit port。
5. 将 output snapshot 累积状态迁入 `output/snapshots.py`，通过构造参数显式注入 observer。
6. 将 migration registry 重构为不可变 Kernel graph；Runtime 负责 migration 收集与 deployment scope。
7. 建立 `telemetry/`，删除 diagnostics/logger，只迁移 trace/span 与 typed event seam。
8. 将协议符号迁至 `commands/symbols.py`，其他 prompt/token 内容按 owner 分散。
9. 收紧各包 `__all__` 并标注 API 稳定性。
10. 在旧类型仍可工作的前提下建立 `ToolDefinition`/`ToolCatalog`、`MaterializedToolCatalog`、由 Runtime 物化的 contracts `ToolBindingSnapshot`、Runtime pinned `BoundToolRegistry` 与按 snapshot dispatch 的 `ToolExecutionPort`；registry/capability 不进入 Kernel。
11. 先在现有 parser 路径内实现 XML/native ToolProjector，使新 catalog 已有完整协议投影能力。
12. 建立 Runtime ToolProvider/BoundTool，迁入 capability factory、run/step lifecycle、permission 和动态 schema 物化；tool instructions 迁实际 context source。
13. 在非生产启用的准备提交中完成新 catalog、projection、Provider、Product adapter 与 manifest codec 测试；准备期旧实现仍是唯一生产真相源。
14. 用一个原子 cutover 提交同时切换 Runtime executor、MCP、内置工具、Product composition、顶级 facade 和 manifest codec，并删除 Kernel 旧 Toolset 真相源；旧顶级 API 从该提交起由 Product adapter 接入新路径。
15. 保持 durable manifest wire 字节结构不变，在 cutover 前后均通过历史 fixture；禁止出现新旧实现按调用方长期分流的双 owner 状态。
16. 将每轮 inference、commands projection 和 action dispatch 绑定同一 snapshot revision；增加热更新、并发刷新、crash/resume 与缺失旧 revision 的 TOCTOU 测试。
17. 增加 Product legacy adapter 单向依赖与内存模型隔离测试；Kernel 边界拒绝 legacy Toolset、subclass hook 和 mutable provider object。

验收：

- Kernel 根目录只保留 `__init__.py` 和一级能力目录。
- telemetry 不依赖其他 Kernel 能力包。
- output 不依赖 execution/inference。
- Kernel output 不含 TurnContextSource、publication/delivery state 或部署 registry。
- output snapshot 不依赖全局 active accumulator ContextVar。
- 不存在 `kernel.agent` 或 `kernel.prompting` 中央容器。
- Kernel tools 中不存在协议类型、`RunContext`、capability factory、permission 或 async lifecycle。
- Runtime ToolProvider 可以从同一 ToolCatalog 为一次 run 提供可执行 capability 集。
- Phase 2 每个提交均可构建、可运行；不存在“旧 Toolset 已删但新 projection 尚未可用”的窗口。
- 顶级旧 Toolset API smoke test 与历史 manifest golden fixture 在切换前后结果一致。
- Product adapter 的构造签名、组合方法、泛型/类型行为和已承诺的 subclassability 由 conformance test 固定；兼容不只等于 import 成功。
- 模型看到的 schema/catalog revision 与 action dispatch registry revision 始终相同。

### Phase 3：`parser` 收敛为 `commands`

目标：建立准确的命令协议边界，并消除与 Think Engine 的概念循环。

工作项：

1. 落实 Phase 0 已批准的 contracts DTO，不再为读取 InferenceEngine 抽取行为 Protocol。
2. 先将 channel 改造成 `InferenceResult + DecodeContext -> DecodeResult` 的无状态转换；tool/history projection 分别接收自己的窄 context。
3. 将历史记录改造成 `ModelTurn + tuple[ExecutedCommand, ...] + HistoryProjectionContext -> HistoryProjection`；execution 按语义顺序调用统一 `ExecutionTransactionPort`，不直接写入或 flush store。
4. tool schema 输入改为 definition/catalog DTO，移除 executor 依赖。
5. 将 Phase 2 已上线的 XML/native typed projection 随 parser 原子迁入 commands；协议差异不回流 ToolDefinition/identity。
6. artifact byte resolution 移出 commands。
7. 将 `parser` 改名为 `commands`，XML lexer/recovery 下沉 `commands/xml/`。
8. 将 factory 从 `native_channel.py` 独立为 `commands/factory.py`。
9. 协议 prompt block 与 symbols 由 commands 提供，inference assembly 只消费渲染结果。

验收：

- `commands` 不 import inference Engine、execution、MessageStore 或 ArtifactResolver。
- `InferenceResult` 是唯一推理输入，`ModelTurn` 是唯一语义动作输出。
- commands 不控制持久化生命周期或完成时序。
- 新增命令协议的实现集中于 commands；其他层只增加 protocol identity/config/wiring/manifest validation，不复制 ToolCatalog、execution、output 或 inference 语义。
- 不存在 `parser/` 旧目录与 import。
- XML/native 协议隔离测试继续通过。

### Phase 4：`think + models` 收敛为 `inference`

目标：形成完整、provider-independent 的模型推理能力。

工作项：

1. 迁移 Think Engine、request、prompt assembly、通用 generate invocation 和 routing signals。
2. 将 web search、image description 等具体任务调用迁往各自 owner。
3. 将 `ThinkSubsystems` 等装配输入改为清晰的依赖对象；不持有 Runtime Role。
4. 统一采用 `Inference*` 命名，不保留 Think alias。
5. Prompt assembly 不 import concrete commands implementation，只消费 protocol rendering data/callable。
6. 用 contracts 两阶段 `ModelInferencePort` 替换 `ModelRoute` service locator；execution 先 resolve target，再按其 capability 完成工具物化、协议投影、output binding 和 request assembly，最后对同一 target infer。Runtime 内部拥有 route/gateway/transformer/fact/artifact 装配。

验收：

- 删除 `think/` 与 `models/`。
- `inference` 不依赖 execution。
- `inference` 内没有 provider、credential、cost 或 network implementation。
- `inference` 只提供通用 infer，不包含 web search、image description、audio、embedding 或 rerank convenience API。
- Kernel production code 不访问 `ModelRoute.gateway`、request transformer、session fact sink 或 artifact resolver。
- capability-changing failover 不复用旧 ToolBindingSnapshot、protocol projection、output binding 或 InferenceRequest。

### Phase 5：`flow` 收敛为 `execution`

目标：在叶子边界稳定后迁移最高层执行能力。

工作项：

1. 迁移 engine、graph、state、result、events，以及 Phase 0 认定的硬安全 limits；Runtime/Product SLO 不进入 Kernel。
2. 将 `DurableFlowRunner` 改为 `EffectAwareGraphRunner`，只表达 effect/recovery directive 与 node callback。
3. 以单一 `ExecutionTransactionPort` 重写 inference/history/output recovery coordinator，移除跨 port 提交窗口、journal runner `Any` 和 Runtime 方法名。
4. 由 `AcceptedOutput` 冻结已接受值与 provenance；幂等 stage 成功后禁止重评，terminal commit 原子提交共同 frontier 并幂等返回 `CommittedOutput`。
5. 删除 `FlowServices`；graph builder 为每类 node 构造窄依赖，节点不得访问完整 execution 世界。
6. 保持单一 Agent Engine；新拓扑继续通过 graph builder 注入。
7. 公共运行事件只暴露稳定 phase，不泄漏 graph/node 内部身份。
8. 删除 `flow/` 旧路径。

验收：

- 执行语义、durable recovery 和 event backpressure 测试通过。
- production 调用方只从 `kernel.execution` 稳定入口导入门面。
- graph internals 不进入根公共 API。
- Kernel 恢复代码不出现 journal、flush、reap 等 Runtime 存储术语。
- 核心 node 依赖不存在未说明的 `Any` 或共享 service locator。
- RunEvent、ObservationEvent、SessionEvent 不互相替代状态真相，转换顺序和可靠性通过契约测试。
- recovery matrix 中每个 crash 点、重复请求、过期 fencing token、取消/提交竞态均有唯一结果。

### Phase 6：边界收紧与迁移基线清零

目标：在 execution 迁移完成后清理残余穿透，完成公共 API 治理。

工作项：

1. Orchestration 不再消费 schema adapter 内部函数；按 5.4 节使用 contracts schema DTO，并由 Tool/Workflow 各自拥有构造策略。
2. 扫描并删除外层 production 对 Kernel internal module 的直接 import。
3. 清理所有跨 owner `Any`、裸 callback 集合和 service locator 残留。
4. 验证 tools/commands/output/recovery 的 owner 与依赖矩阵一致。
5. 清零所有临时 import baseline。

验收：

- ToolCatalog 组合、manifest、Runtime Provider lifecycle 和 commands projection 隔离测试通过。
- Kernel tools 中不存在 `XmlToolDefinition`、`NativeToolDefinition`、`XmlToolset`、`NativeToolset` 或 `RunContext`。
- 外层生产代码不 import Kernel internal module。
- 所有临时迁移 baseline 清零；终态依赖矩阵与 API 契约持续执行。

### Phase 7：删除迁移设施并完成文档同步

目标：终态无 internal 迁移残渣，同时保留批准的 Product public adapter 与持久化 wire reader。

工作项：

1. 删除所有临时架构例外、迁移脚本和 Kernel/Runtime import 兼容代码；保留批准的 Product public adapter、持久化 wire reader 及终态治理规则。
2. 更新 `zdocs/ARCHITECTURE.md`、入口速查表和包 docstring。
3. 生成终态依赖图与基线指标。
4. 全量运行 Kernel 直接依赖方测试。
5. 按 8.3 节构建 wheel 并完成隔离安装验证。

验收：

- 没有 Kernel/Runtime legacy module、alias、forwarder 或 stale baseline；批准的 Product adapter 与 wire reader 均有独立测试和 owner。
- 文档路径与源码一致。
- 依赖矩阵为 DAG，所有规则由测试执行。

---

## 7. 治理门禁

### 7.1 架构测试

至少增加以下自动检查：

Phase 0 先建立 `ztest/architecture/import_scanner.py`，统一解析绝对/相对 import、alias、facade re-export、`TYPE_CHECKING` 和可静态识别的 dynamic import，输出标准化边：`source_module`、`target_module`、`line`、`kind`、`type_checking`、`dynamic`。该模块是 architecture tests 自有基础设施，不进入生产包，也不复制到各条测试中。

1. Kernel 不得 import Runtime、Orchestration、Product。
2. Kernel 一级包依赖必须符合批准矩阵。
3. Kernel 不得出现 `common/shared/utils/misc/helpers` 包。
4. Kernel 根目录不得新增领域实现模块。
5. 外层生产代码不得 import 标记为 internal 的 Kernel 模块；扫描直接模块路径，不只检查 `__all__`。
6. `__init__.py` 导出必须与 stable/provisional API 清单一致。
7. 禁止函数、方法或类体内 import；遵循仓库统一规则。
8. 禁止 dynamic import 和以 import side effect 完成注册/装配，除非另有明确架构批准。
9. 一级能力公共 API、contracts port 方法、跨包 dataclass 和 node 依赖字段禁止无说明的 `Any`。

### 7.2 变化轴审计

以下任一条件触发设计评审，但不自动要求拆文件：

- 单文件超过 500 LOC。
- 一个模块被三个以上一级能力包修改或消费。
- 一个模块同时拥有两种以上状态真相源或生命周期。
- 一个普通需求需要修改三个以上 Kernel 一级包。
- 新增抽象只有一个实现且没有测试替身、协议边界或近期第二实现需求。
- 新增包名无法用一句领域能力描述。
- 一个跨 owner 依赖通过 `Any`、裸 callback 集合或超过五个能力字段的依赖容器表达。

`Any` 门禁不是全仓禁用 `Any`：候选值 payload、JSON value 或第三方无类型边界可以使用。边界例外必须精确到字段，记录原因、owner 和退出条件；不得对整个文件或包豁免。

### 7.3 公共 API 规则

- 机器可读清单位于 `zdocs/architecture/kernel-api.toml`，是架构测试输入，不靠 Markdown 人工同步。
- 每个候选符号必须标记 stable、provisional 或 internal；provisional 带到期发布周期。
- 公共 API 只从一级包 `__init__.py` 导出。
- Kernel 根 `__init__.py` 只放极少数跨能力稳定门面。
- 公共 API 变更必须在同一评审中说明迁移影响；内部文件移动不应影响外层调用方。
- 架构测试扫描 Runtime、Orchestration、Product 的全部 production import，直接引用 internal 路径立即失败。
- 不以 `_internal` 目录代替架构边界；真正边界仍由 import 测试执行。
- 测试可对白盒内部模块直接测试，但测试 import 不定义公共稳定性。

清单格式至少包含：

```toml
[symbols."mote.kernel.execution.AgentFlowEngine"]
stability = "stable"

[symbols."mote.kernel.commands.CommandDecoder"]
stability = "provisional"
expires = "2026.10"
```

架构测试验证导出与清单一致、provisional 必有期限且到期自动失败、internal 不被外层 production import、stable 删除具有明确 major-version/发布决策。`mote`/`mote.tools` 顶级 facade 使用独立 public API 清单或现有精确 facade test，不能混入 Kernel internal 清单。

### 7.4 Prompt 治理检查

- 静态可缓存前缀继续遵守 `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 约束。
- 不建立 Kernel 通用 prompt 包；模板跟随 commands、inference、execution 或上层具体 owner。
- Product/Runtime/Orchestration 特有名词不得重新进入 Kernel 模板。
- per-turn 内容必须通过注入的 context source/bus 进入请求，不拼入 Kernel 静态模板。
- 协议差异通过 command channel 的 block rendering 与 symbol vocabulary 表达。
- Prompt 迁移必须做快照/协议隔离测试，禁止在纯移动提交中顺手改文案。

---

## 8. 测试与提交策略

### 8.1 每阶段最低测试

每阶段至少运行：

```bash
python -B -m pytest \
  ztest/architecture \
  ztest/kernel \
  ztest/flow \
  ztest/think \
  ztest/parser \
  ztest/prompts \
  ztest/executor/test_toolset.py \
  ztest/executor/test_tool_spec_adapter.py \
  -q --tb=short -p no:cacheprovider
```

再根据迁移内容运行直接消费方：

- inference/model calls：`ztest/router`、相关 Runtime model gateway 测试。
- output：`ztest/roles`、`ztest/integration`、`ztest/tasks/bggraph`。
- output 状态拆分：Runtime session publication、恢复、event replay 与 transaction consumer contract tests。
- tools：`ztest/executor`、MCP、Runtime ToolProvider lifecycle、commands XML/native projection 与 session manifest recovery。
- recovery ports：durable process-crash、inference repayment window、node effect recovery 和 output commit crash-frontier 测试。
- Prompt owner 上移：对应 Product Skills、Agent、Toolsets 与 Runtime context 测试。

Phase 0 必须先修复 `ztest/prompts` 收集并移除 `pyproject.toml` 中的默认 ignore；完成后上述命令必须稳定执行，不再接受“预存失败”作为永久例外。Phase 0 完成前使用现有可收集的 prompt/protocol 替代套件并明确记录覆盖范围。

### 8.2 原子提交规则

每个提交必须满足：

- 一个提交只改变一个架构不变量；为保持该不变量始终成立，允许同时修改 contracts、Kernel、Runtime 和 Product 多个 owner。
- 生产代码、测试、架构规则和文档路径同步更新。
- Kernel/Runtime internal 不保留旧 import 路径兼容层；批准的 Product public adapter 与持久化 wire reader 不受此条禁止。
- 不混入业务行为修改。
- 可独立回滚且不会留下半迁移双 owner。

“依赖更简单”使用可计算指标，不比较总边数：

- 非批准跨包边数量不得增加。
- SCC 数量和最大循环规模不得增加，终态均为 0。
- 外层对 internal module 的直接 import 数不得增加。
- 新增跨包边必须已存在于终态批准矩阵。
- 临时迁移边总数按 Phase 单调下降；Phase 内若原子切换短暂增加，必须在同一提交清除。

推荐提交顺序：架构门禁 → owner 迁出 → output/telemetry 叶子 → ToolCatalog/Runtime Provider → commands → inference → execution → 边界清理 → 文档收尾。

### 8.3 发布包验证

Phase 7 必须构建 wheel 并安装到不引用源码树的隔离环境，验证：

1. `kernel-api.toml` 中全部 stable/provisional import 可用，且 provisional 均未过期。
2. `mote`/`mote.tools` 旧 Toolset stable API 可导入并通过最小行为 smoke test。
3. Kernel 已删除的 internal/legacy 模块未被 wheel 打包。
4. Product facade adapter 实际转换到新 ToolProvider/ToolCatalog，而非加载旧 Kernel 实现。
5. package data 清单完整，目录迁移未丢失配置、registry、前端资源或 runtime assets。
6. 从 golden rollout/session fixture 恢复旧 manifest 成功。

---

## 9. 已决策事项索引与剩余审计

以下是已经成为 v8 规范约束的决策索引，不是开放选项，也不按评审轮次解释规范。完整意见来源和版本变化见
[`kernel-package-governance-review-log.md`](./kernel-package-governance-review-log.md)。涉及字段级 DTO、端口签名、crash matrix 和兼容行为的内容仍须在 Phase 0 形成可执行产物后复审。

### D1：是否采用 `execution` 替代 `flow`

结论：采用。

理由：`flow` 容易被理解为通用 workflow 或数据流，而此包实际拥有单 Agent 执行循环、恢复、运行事件和结果。`execution` 更准确，也与 Orchestration workflow 区分。

### D2：是否采用 `inference` 替代 `think`

结论：采用包名 `inference`，类名同步改为 `InferenceEngine`，不保留 alias。

理由：该能力包含请求组装、模型调用和响应归一化，不只是主观“思考”。`inference` 更接近稳定技术语义。

### D3：`commands` 还是 `command_protocols`

结论：`commands`。

理由：简短且能覆盖解析、记录、历史投影与协议能力协商；二级模块已足以表达 XML/native protocol。

### D4：Prompt 是否保留一级包

结论：不保留。协议符号和协议 prompt 归 commands；推理组装及其模板归 inference；执行模板归 execution；上层业务文案归实际 owner。

### D5：复杂度评分的最终 owner

结论：整体迁入 `product.routing.squilla`，不抽取所谓通用 Kernel complexity framework。

理由：当前只有一个产品策略消费方；提前抽象会冻结产品启发式为 Kernel 契约。

### D6：Orchestration 对 Tool schema 算法的消费

现状：`orchestration.tasks.bggraph` 直接使用 `kernel.tools.spec_adapter.annotation_to_json_schema`。

结论：contracts 只提供跨包 schema DTO，不承载 annotation 编译算法。Tool 与 Workflow 各自拥有构造策略，不接受 Orchestration 继续 import `kernel.tools` internal adapter，也不预先建立跨领域 shared compiler。Phase 0 必须结合 [`ADR-0006`](./adr/0006-bggraph-migration-contract.md) 审计现有 DTO 是否足够；若证明两者算法不变量完全一致，另立 ADR 决定中性 owner。

### D7：`memory.py` 的拆分归属

需逐项审计：

- 单 Agent 对 Memory 内容的抽象引用可留 Kernel。
- MEMORY.md 文件约定、资源注入与 per-turn 展示归 Runtime/Product。
- Coding Agent 的具体操作指令归 Product。

实施前需列出常量级归属表，不能整文件机械搬迁。

### D8：`AgentSpec` 的处理

结论：禁止原样迁入新包。Phase 0 逐字段拆为 contracts 稳定身份/配置 DTO、Kernel execution policy、Runtime policy 和 Product defaults；拆分后没有独立 Kernel 领域行为则不建立 `kernel.agent`。

### D9：commands 的唯一输入与输出

结论：采用 `InferenceResult -> ModelTurn`。

- `InferenceResult` 表示解析前的 provider-independent 推理结果。
- `ModelTurn` 表示 commands 解析后的语义动作，不能同时承担解析前输入。
- 当前 `ThinkResult` 在 Phase 0 决定是被 `InferenceResult` 替换还是仅作为 internal 过渡类型；终态不允许两个等价真相源。
- 数据流先 inference 后 commands，不等于 inference import commands；execution 依次调用二者，双方只通过 contracts DTO 连接，因此依赖矩阵中不存在 inference ↔ commands 边。

### D10：history projection 与持久化 owner

结论：commands 只生成 `HistoryProjection` 消息 DTO；execution 独占 call-before-effect、result-after-effect、恢复配对和完成时序。Runtime `ExecutionTransactionPort` 统一 history、inference、accepted output 与 terminal result 的共同 crash frontier；Runtime 内部可以拆 journal/store，但不能向 Kernel 暴露部分提交。

### D11：Toolset 是否协议中立

结论：是。Kernel 只保留单一 `ToolDefinition`/`ToolCatalog`；XML/native 静态隔离移动到 commands projection 结果类型。内存 `ToolCatalogIdentity` 不含 protocol，但 Runtime 继续读写现有包含 protocol 的 manifest wire，并在 codec 边界投影/校验。

这不是删除静态安全，而是把静态安全放回协议真正发生的边界。新增第三种协议可增加跨层稳定 identity 与装配映射，但不得复制 ToolCatalog、execution、output 或 inference 类型体系。

### D12：Tool provider lifecycle 的 owner

结论：迁往 Runtime。capability factory、`for_run`、`for_run_step`、async context、permission binding 都需要运行上下文或资源生命周期，不属于 Kernel value algebra。Tool instructions 由实际 Runtime/Product context source 拥有。

### D13：Output evaluation 与 delivery 生命周期

结论：拆分。

- Kernel `OutputEvaluationState`：`IDLE -> CANDIDATE_RECEIVED -> AWAITING_CORRECTION/CORRECTION_EXHAUSTED/ACCEPTED`。
- Runtime `OutputDeliveryState`：`COMMIT_STARTED -> COMMITTED -> PUBLICATION_QUEUED -> PUBLISHED/FAILED`。
- Kernel 构造 immutable `AcceptedOutput`；Runtime transaction 幂等 stage 后，它成为该输出决策不可变的 staged payload，但不形成 SessionEvent 之外的第二套业务历史。
- `ExecutionTransactionPort.commit_terminal_output()` 只接收 staged output identity 并返回 `CommittedOutput`；提交完成后只有 `CommittedOutput` 返回 Kernel/下游。

现有 contracts `OutputLifecycleState` 必须删除并由两个封闭枚举替代，避免继续保留双 owner 总枚举。

### D14：恢复与输出事务端口

结论：三个独立 mutation port 会制造跨 port 分布式事务，因此 Kernel 只看到 run-scoped `ExecutionTransactionPort`。它表达 record turn/result、reject、stage accepted、terminal commit 和 recover frontier，不暴露 journal runner、MessageStore、drain、flush 或 reap 方法。所有 mutation 具有 operation identity、fencing、expected revision、幂等和 typed conflict/cancellation 语义。Runtime 内部可组合专用 store/port，并由唯一 reconciler 提供原子可见的 crash frontier。

`ThinkCheckpoint` 改为 `InferenceRecoveryCoordinator`；`DurableFlowRunner` 改为 `EffectAwareGraphRunner`，因为后者自身不提供 durability。

### D15：ContextVar observer 是否全部取消

结论：不全部取消。

- Output snapshot observer 改为 run-scoped 显式构造依赖；累积器归 output。
- trace/span correlation 的 ContextVar 保留，它表达异步调用链上下文传播，而非隐藏领域依赖。
- Kernel telemetry 不公开 logger；diagnostic 通过 typed result/event 发出，Runtime subscriber/backend 决定日志记录。

### D16：Output migration registry 的 owner

结论：Kernel 只提供构造后不可变的 `OutputMigrationGraph`/`ValidatorMigrationGraph` 和路径唯一性算法；Runtime 负责 migration 收集、配置、部署作用域与注入。contracts 保留稳定 migration identity/Protocol，不承担 registry 实现。

### D17：FlowServices 的处理

结论：删除，不改名保留。Graph builder 为不同 node 装配窄依赖；跨拓扑复用语义可以有自己的 Protocol，但不存在所有节点共享的 `ExecutionDependencies` service locator。

### D18：顶级 Toolset API 与持久化 wire 兼容

结论：保持兼容。

- `mote`/`mote.tools` 的现有 stable Toolset API 由 Product facade adapter 保留，内部转换为新 ToolProvider/ToolCatalog。
- Kernel 不保留旧 Toolset 实现、alias 或 forwarding module。
- 删除顶级 adapter 必须另有 major-version 发布决策，不能作为本治理的“清理残渣”。
- Runtime 继续读写现有 `toolset_manifest=[{id, version, protocol}]` wire；reader 投影到新内存模型，禁止改写旧 rollout。

### D19：commands 稳定接口拆分

结论：不设置承担全部职责的 `CommandCodec`。公共概念拆为：

- `CommandDecoder`：`InferenceResult -> DecodeResult(ModelTurn, diagnostics)`。
- `ToolProjector`：`MaterializedToolCatalog -> protocol wire definitions/catalog`。
- `HistoryProjector`：turn/executed result -> `HistoryProjection`。

XML/native protocol bundle 可以组合三者，但组合对象默认 provisional，不强迫三个变化轴共享同一稳定生命周期。

### D20：动态 Tool schema 真相源

结论：Runtime ToolProvider 是本 run/step 实际工具集合与动态 schema 的唯一真相源。Kernel ToolCatalog 只描述逻辑定义；Runtime 产生 pinned `BoundToolRegistry` 与 contracts `ToolBindingSnapshot`/`MaterializedToolCatalog`，commands 只投影后者。Kernel 通过 `ToolExecutionPort` 携 snapshot identity 执行，不接触 registry/capability。permission、feature flag、用户、模型和 session 策略均在物化前执行，不进入 Kernel catalog filter。

### D21：ModelInferencePort 的两阶段解析

结论：采用 `resolve(InferenceIntent) -> ResolvedInferenceTarget` 与 `infer(target, request)`。Intent 先声明语义能力需求；target 通过 capability fingerprint、projection compatibility key 和 lease identity 冻结请求定稿条件。只有 compatibility key 相同且 resume 契约明确允许时才可复用请求资产，否则必须重新物化、投影、协商和组装。

### D22：commands diagnostics 分流

结论：`DecodeResult` 分离 `ProtocolIssue` 与 `ObservationDiagnostic`。前者必须由 execution 作出 reject、feedback、skip 或 terminate 等语义决策，必要时持久化；后者才可直接进入可采样、可丢弃的 observation plane。

### D23：SessionEvent 与 TransactionRecord

结论：SessionEvent 是 Agent 业务状态与 replay 的唯一恢复事实；TransactionRecord 仅是 Runtime 完成未决原子提交的基础设施恢复真相。AcceptedOutput 可以作为 staged/Session payload，但 reconciler 完成后业务 replay 不读取 transaction record，也不存在第二套 Agent 历史。

### D24：commands 与 tools 的依赖关系

结论：commands 只消费 contracts `MaterializedToolCatalog`，终态不依赖 kernel.tools。未来若确需调用 ToolCatalog 算法，必须用具体不变量重新评审矩阵，不能为可能性预留依赖边或把算法搬进 contracts。

### D25：ToolBindingSnapshot 保留策略

结论：Runtime pinned registry/snapshot 至少保留到 run terminal 且 checkpoint、effect reconciliation、transaction/publication 引用全部结束。可恢复 session 持久化最小 descriptor；provider/capability 无法安全重建时返回 typed conflict，不绑定同名最新工具。GC 按已完成 run 和引用安全条件执行，避免长 session 无限增长。

---

## 10. 风险与控制

| 风险 | 控制措施 |
| --- | --- |
| 大规模改 import 导致行为回归 | 先迁 owner 与叶子包，最高 fan-in 的 execution 最后迁 |
| 目录整理掩盖业务修改 | 每提交只改变一个架构不变量；允许跨 owner 原子切换，Prompt 文案和算法保持行为等价 |
| 新 facade 再次过度导出 | stable/provisional/internal 清单、外层 import 扫描与 `__all__` 审查共同约束 |
| 为消除循环创建错误 contracts DTO | 只提升真正跨边界稳定数据；实现状态留 owner 包 |
| 迁移中出现双 owner | 新纵向数据流先完整上线，消费方切完后同提交删除旧内部真相源；Product facade adapter 不算 domain owner |
| 当前工作树并行重构造成冲突 | 各 Phase 开始前重新生成基线，按实际 owner 迁移，不依赖本文旧路径机械执行 |
| 持久化恢复受模块移动影响 | 明确验证事件 tag、contract ID、tool name、checkpoint schema 与 Python 路径解耦 |
| Tool identity 去 protocol 后误恢复 | 保持旧 manifest wire，Runtime codec 分别校验 catalog 与 protocol；golden fixture 覆盖历史 rollout |
| Output 状态机拆分后出现缝隙 | immutable AcceptedOutput 先幂等 stage；统一 transaction 原子提交 terminal frontier，契约测试覆盖 stage/commit 全部 crash point |
| 动态工具发生 TOCTOU | 每轮 immutable ToolBindingSnapshot；Runtime pinned registry；dispatch 必须校验 snapshot revision |
| 三事件平面形成多真相源 | SessionEvent 是 Agent 业务 replay 的唯一事实；TransactionRecord 仅协调未决提交；RunEvent/ObservationEvent 仅为投影 |
| Product legacy adapter 反向塑造 Kernel | 单向依赖门禁与对象隔离测试；Kernel 边界只接收新 ToolCatalog/Provider/snapshot 模型 |
| Port 只把散参换成巨型 Protocol | 端口方法按业务事务设计，AST 门禁限制 `Any`，consumer contract test 验证最小表面 |

---

## 11. 完成标准

治理完成需要同时满足：

1. 目标一级能力包已经落地，旧 `flow`、`think`、`models`、`parser` 路径按评审决定删除。
2. Kernel 中不存在 Product、Orchestration 或 Runtime 专有策略。
3. Kernel 内依赖图是由架构测试验证的 DAG。
4. 所有外层生产消费方只使用批准的 stable/provisional 公共入口；治理完成时不存在过期 provisional。
5. 根目录无领域孤儿模块。
6. Prompt 按业务 owner 分布，不存在 `kernel.prompting` 中央字符串仓库。
7. Kernel 只有协议中立 ToolCatalog；Provider lifecycle 在 Runtime，协议 projection 在 commands。
8. Output evaluation 与 delivery 是两个 owner 清晰的状态机，Kernel 不含 publication/turn-context/deployment registry。
9. 单一 ExecutionTransactionPort 连接 Runtime 共同 crash frontier；全部 mutation 有幂等、fencing、revision、冲突和取消语义，不存在跨 port 分布式提交或存储术语散参。
10. Graph node 按需接收窄依赖，不存在 FlowServices/ExecutionDependencies service locator。
11. Kernel 不公开 logger，snapshot observer 显式注入，trace/span ContextVar 仅用于调用链传播。
12. 核心公共 API、跨包 DTO、port 和 node 依赖不存在未说明的 `Any`。
13. 所有临时 baseline、Kernel/Runtime 旧路径、alias 和迁移注释已删除；批准的 Product public adapter 与持久化 reader 保留。
14. 直接依赖方、架构、prompt 和历史恢复测试全部通过，无永久忽略的治理门禁。
15. wheel 隔离安装、stable import、顶级兼容 API、legacy exclusion 与 package data smoke test 通过。
16. `ARCHITECTURE.md`、包 docstring 和源码路径一致。
17. 每轮 ToolBindingSnapshot 从 inference 到 action 不可变，Kernel 不接触 BoundToolRegistry/capability，旧 revision 不可恢复时显式冲突。
18. SessionEvent 是 Agent 业务状态与 replay 的唯一恢复事实；TransactionRecord 只协调未决基础设施提交，完成后不得形成第二套 Agent 历史；RunEvent 与 ObservationEvent 的背压/丢弃策略不能改变执行真相。
19. AcceptedOutput stage 后不可重新评估；terminal commit 失败、重试或恢复始终引用同一 immutable accepted identity。
20. 推理遵循 resolve target → materialize/project/negotiate/assemble → infer 的两阶段时序；target capability 变化必须完整重建请求。
21. ProtocolIssue 均有显式语义决策，ObservationDiagnostic 才允许直接进入可丢弃 telemetry。

### 11.1 量化终态指标

| 指标 | 目标 |
| --- | ---: |
| Kernel 内 SCC | 0 |
| 非批准 Kernel 跨包边 | 0 |
| Kernel 对 Runtime/Orchestration/Product import | 0 |
| 外层 production 对 Kernel internal import | 0 |
| 核心边界未说明 `Any` | 0 |
| 临时 migration baseline | 0 |
| stable/provisional API 清单与实际导出差异 | 0 |
| 顶级 public facade conformance 差异 | 0 |
| provisional 过期项 | 0 |
| 持久化恢复 golden fixtures | 全部通过 |
| wheel stable/public import smoke test | 通过 |

普通变更触及三个以上 Kernel 一级包的比例作为持续趋势指标记录，不作为一次性验收数字；每次发生都必须在评审中解释架构不变量。

完成后的 Kernel 应能用一句话描述：

> Kernel 定义单 Agent 如何组装一次模型推理、解释命令、推进执行图并接受结构化输出；它可以通过 contracts port 编排异步外部能力，但所有具体 IO 实现、事务持久化、输出交付、多 Agent 编排和产品策略均由上层注入。
