# Mote 与 Pydantic AI 差距核实报告

## 1. 审计结论

本文对“Mote 相比 Pydantic AI 存在的 11 项架构与工程差距”逐项核实。

总体结论是：**11 项判断的战略方向均有源码依据，但第 1、2、4、7、10 项需要收紧措辞。** 审计时最明确、风险最高且能动态复现的问题是资源生命周期不统一；最影响外部开发者体验的问题则是缺少稳定 facade、依赖类型没有贯穿 Agent 与 Tool，以及发行包仍然过重。

2026-07-24 已完成核心整改：引入 `EngineServices`、不可变 `AgentDependencies[DepsT, OutputT]` 与 `AgentWiring`，并以分阶段 `LifecycleStack` 统一 Agent、Provider、exporter、event subscriber 与 `DiskWriter` 的关闭链；建立顶层 `Engine → Agent[DepsT, OutputT] → RunResult[OutputT]` facade；并消除非测试代码中的函数/类内 import。下文保留发现时的证据，同时明确标注已经关闭或缩小的差距，避免把历史发现误写成当前事实。

本文是事实审计，不替代 [`pydantic-ai-architecture-lessons.md`](./pydantic-ai-architecture-lessons.md) 中的长期演进设计。

## 2. 审计基准

审计对象：

- Mote：`/home/longert/mote`，提交 `feaffe7`，包含尚未提交的五层迁移工作树；
- Pydantic AI：`/home/longert/run_rollout/pydantic-ai`，`main` 分支提交 `3adb9b02`，对应 `v2.9.0`；
- 判断基于当前工作树，而非只看 Mote 的 `HEAD`。

文中使用以下路径简称：

- `$MOTE`：`/home/longert/mote`；
- `$PYDANTIC_AI`：`/home/longert/run_rollout/pydantic-ai`。

由于 Mote 正在进行大规模五层迁移，本文中的目录、数量和测试状态是该工作树的快照，后续提交迁移后应重新执行核实。

## 3. 逐项核实

### 3.1 公开 API 仍不够稳定、简洁

**判断：基本属实，但原始概念清单和对 Pydantic AI 的描述需要修正。**

审计开始时，Mote 顶层 [`__init__.py`](../__init__.py) 几乎没有统一 facade。当前已新增可运行且由架构测试锁定的顶层常用路径，只公开 `Engine`、`Agent[DepsT, OutputT]`、`Model`、`ModelMessage`、`RunContext[DepsT]`、`ToolContext[ToolDepsT]`、协议显式的 `XmlToolset` / `NativeToolset`、只读共同概念 `Toolset`、`OutputContract[OutputT]` 和 `RunResult[OutputT]`。普通调用者不再需要构造或导入 `RoleSchema`、`ComponentGraph`、provider factory、routing builder 或 lease coordinator。

审计开始时，[`Role.__init__`](../runtime/agent/role.py) 直接接收以下装配能力：

- `routing_strategy_builders`；
- `background_task_pool_builder`；
- `run_lease_coordinator`；
- `toolsets`；
- Provider factory 间接依赖。

这些参数属于运行时装配或共享服务，不适合继续扩张普通框架使用者可见的构造入口。当前这些能力已经按生命周期拆进 [`EngineServices`](../runtime/services.py) 和不可变 [`AgentDependencies[DepsT, OutputT]`](../runtime/agent/wiring.py)，再由 `AgentWiring` 原子配对；`Role.__init__` 不再逐项暴露它们。fork 与 skill fork 整体继承依赖定义；隔离服务通过每个 incarnation 的引用计数 lease 继承，最后一个 owner 才关闭资源。公开 `Agent` facade 与 `DepsT` 泛型已经落地，剩余差距转为 Toolset 泛型/组合、公开 API 兼容政策和 wheel 级验证。

需要修正两点：

1. `Flow`、`RuntimeModules` 等并不都是当前工作树中的正式公开类型，原始概念清单已有部分过时；
2. Pydantic AI 顶层实际有约 155 个 `__all__` 符号，见 `$PYDANTIC_AI/pydantic_ai_slim/pydantic_ai/__init__.py`。它的优势不是“总共只公开六个符号”，而是完成基础任务时通常只需理解 `Agent`、`RunContext`、`Toolset`、`Model`、消息和运行结果等少数核心概念。

因此，更准确的历史结论是：**Mote 缺少的是稳定、极小的常用路径 facade，而不是内部类型数量本身过多。** 第一版 facade 现已补齐，但仍需发行安装和真实用户代码验证后才能称为长期稳定。

### 3.2 类型系统明显弱于 Pydantic AI

**判断：审计时核心判断属实；当前 Agent/Run/Output/validator 主链已补齐，内置 class-based 工具仍有动态边界。**

当前 Mote 已有：

- `Role[DepsT, OutputT]` 与公开 `Agent[DepsT, OutputT]`；
- `AgentDependencies[DepsT, OutputT]`；
- `RunContext[DepsT]` 与显式投影的 `ToolContext[ToolDepsT]`；
- `OutputContract[OutputT]`，见 [`kernel/output/contract.py`](../kernel/output/contract.py)；
- `OutputDecoder[OutputT]`、`OutputValidator[OutputT]` 与
  `ValidatorDecision[OutputT]`；
- `OutputEvaluation[OutputT]` 与 Runtime `OutputEngine[OutputT]`；
- `RunResult[OutputT]`，见 [`contracts/output.py`](../contracts/output.py)；
- `FlowState[OutputT]` 与 `FlowResult[OutputT]`；
- `CapabilityMap` `TypedDict`，见 [`runtime/tools/capability_types.py`](../runtime/tools/capability_types.py)。

因此 `DepsT` 未贯穿 Agent 与运行上下文已不再是事实。当前主要缺口是：

- 工具执行主路径仍通过字符串 `requires`、动态 `setattr` 和运行时 capability 查找注入，公开 `ToolContext` 尚未贯穿所有函数工具；
- 一些内部动态组件装配边界仍退化到 `Any`；
- 尚未建立仓库级严格 Pyright/Mypy 基线来防止新增类型退化。

Pydantic AI 则明确建立了：

- `Agent[DepsT, OutputT]`；
- `RunContext[DepsT]`；
- `AgentRunResult[OutputT]`；
- 工具、validator 和 output function 对 `DepsT`、`OutputT` 的一致推导。

相关源码位于：

- `$PYDANTIC_AI/pydantic_ai_slim/pydantic_ai/agent/__init__.py`；
- `$PYDANTIC_AI/pydantic_ai_slim/pydantic_ai/_run_context.py`；
- `$PYDANTIC_AI/pydantic_ai_slim/pydantic_ai/run.py`；
- `$PYDANTIC_AI/tests/typed_agent.py`。

当前实现明确保留了不同安全边界：`RunContext[DepsT].for_tool(projector)` 必须通过显式 projector 生成更窄的 `ToolContext[ToolDepsT]`，工具不会自动获得完整应用依赖。该链路现已接入 `XmlFunctionToolset` / `NativeFunctionToolset` 的实际注册与调用；剩余工作是继续减少内置 class-based capability 的动态 `requires` / `setattr` 边界。

输出端也已从“泛型外壳、内部 Any”改为完整的同型约束：contract、decoder、validator、validator decision、evaluation、commit record 和 Runtime engine 共享同一个 `OutputT`。[`typecheck/typed_output_api.py`](../typecheck/typed_output_api.py) 锁定公开推导，错误输出类型不能再从 validator 静默流入 commit。

### 3.3 缺少统一的 Engine 生命周期容器

**判断：审计时强烈属实；当前已通过分阶段生命周期协议完成整改。**

[`runtime/models/clients/context.py`](../runtime/models/clients/context.py) 中的 `Context` 持有：

- `DiskWriter`；
- health registry；
- rate-limit tracker；
- maintenance coordinator；
- provider factory。

审计时它没有统一的 `__aenter__`、`__aexit__` 或 `aclose()` 协议。

Mote 并非完全没有清理逻辑：[`Role.cleanup()`](../runtime/agent/role.py) 会关闭部分 LSP、MCP/tool session、sandbox、runtime host、maintenance 和 index 资源；[`product/cli/driver.py`](../product/cli/driver.py) 又负责关闭 scheduler、port、control 和 Role。问题在于关闭权分散，且 `Context` 持有的 writer、Provider HTTP client、exporter 等资源没有统一所有者。

运行以下定向测试：

```bash
python -m pytest \
  mote/ztest/architecture/test_public_api.py \
  mote/ztest/architecture/test_layer_dependencies.py \
  mote/ztest/executor/test_toolset.py \
  mote/ztest/roles/test_runtime_maintenance.py \
  mote/ztest/session/test_fork.py \
  -q --tb=short
```

23 个测试全部通过，但 pytest 退出时仍报告：

```text
Task was destroyed but it is pending!
```

残留任务是 `DiskWriter._run()`，并伴随 event loop 已关闭警告。这说明统一生命周期容器不是理论上的架构洁癖，而是当前可复现的资源泄漏问题。

Pydantic AI 的 `Agent` 自身支持 async context manager，并在退出时管理 Toolset、Model 和 Provider HTTP client。Mote 更适合把范围扩大为 Engine/Application 级：

```python
async with Engine(...) as engine:
    agent = engine.agent(...)
    result = await agent.run(...)
```

当前已新增内部 [`runtime/engine.py`](../runtime/engine.py)、公开 [`engine.py`](../engine.py) 和通用 [`runtime/lifecycle.py`](../runtime/lifecycle.py)。`LifecycleStack` 提供命名资源、阶段屏障、阶段内逆序关闭、同阶段失败全收集、仅保留失败资源重试、并发幂等和调用者取消隔离。关闭顺序现为：

1. Engine 停止并关闭其拥有的 Agent；
2. Role 同阶段关闭 file watcher、后台任务、EventBus 生命周期订阅者、ToolExecutor/MCP、managed runtime、Sandbox、maintenance、repo index 和 session log；
3. Role 私有资源全部成功后才释放 `EngineServicesLease`；
4. Context 关闭所有 Provider HTTP clients；
5. Langfuse exporter 先 flush 再 shutdown；
6. 最后 drain 并关闭 `DiskWriter`。

EventBus 通过 nominal `AsyncCloseSubscriber` 契约拥有 LSP、title task 和 tracing subscriber，不再由 `Role.cleanup()` 逐个识别具体类型。Langfuse 不再读取 SDK process-global client，而由每个 Context 构造并持有独立 client。后台任务池新增正式 `aclose()`，会取消并 join 所有任务、waiter 与输出 drain；MCP teardown 只移除成功关闭的 client，失败连接保留到下一次 shutdown retry。`SessionRegistry.evict()` 也通过 `engine.release(role)` 解除 Engine 强引用，避免长服务持续积累已淘汰 Agent。

CLI `SessionDriver`、`BaseProjector` 和服务端 `SessionRegistry` 同样改用该协议。Projector
关闭所有 consumer transport；Driver 依次停止订阅、scheduler、port、projector、control 与
Engine；SessionRegistry 只有在 control/Agent ownership 确认关闭后才移除 resident session，
失败 session 保留以供重试，不再把异常吞掉后伪装成成功 eviction。

隔离 spawn/fork 不再复制 `owns_services: bool`。`EngineServices.acquire()` 为每个 incarnation 分配独立 lease，只有最后一个 lease 释放才关闭共享的隔离 Context；Engine-managed Agent 则借用 services，由 Engine 在全部 Agent 关闭后统一回收。这关闭了 fork 双重关闭或父 Agent 提前关闭子 Agent 资源的隐患。

MCP/LSP/Sandbox 仍保持正确的 session 私有 ownership；它们没有为了“统一”而错误提升为 Engine 单例。共享扩展资源可通过 `EngineServices.register_resource()` 或 Context 的显式 lifecycle resource 注册进入同一阶段协议。由此，关闭权已统一，但资源作用域仍按 session/shared 两级隔离。

### 3.4 Provider 覆盖面和协议测试不足

**判断：基本属实，但“只覆盖四家 Provider”不准确。**

Mote 当前有四种专用模型实现：

- OpenAI Chat Completions；
- OpenAI Responses；
- Anthropic；
- DeepSeek。

装配入口见 [`product/integrations/models/__init__.py`](../product/integrations/models/__init__.py)。此外，配置层存在约 36 个 Provider brand preset，多数映射到 OpenAI-compatible transport，因此 Mote 实际支持的品牌入口多于四个。

真正差距是系统化兼容层：

- [`ModelProfile`](../contracts/models/profile.py) 只有约七类通用 facet；
- 许多能力仍依赖模型名 substring 表；
- 缺少 Provider-specific profile 扩展；
- 缺少参数、structured output、tool choice、streaming、reasoning token、native tool 和 web search 的兼容矩阵；
- 没有 VCR/cassette 请求响应测试。

当前 Pydantic AI 仓库快照约有：

- 31 个 provider 模块；
- 20 个 model 模块；
- 14 个 profile 模块；
- 1168 个 cassette 文件。

因此，这一差距应描述为“专用 transport、capability profile 和协议回归测试不足”，而不只是 Provider 品牌数量较少。

### 3.5 Toolset 的公开组合能力不完整

**判断：审计时属实；当前公开核心组合代数、动态生命周期与协议隔离均已落地，差距已显著缩小。**

审计时 Mote 的 [`kernel/tools/toolset.py`](../kernel/tools/toolset.py) 主要是 Registry 视图。当前已经替换为名义隔离的：

- `XmlToolDefinition` / `NativeToolDefinition`；
- `XmlToolset` / `NativeToolset`；
- 同协议 `filter`、`prefix`、`rename`、`prepared`、类型化 `with_approval`、`with_instructions`、`combine`；
- approval policy 可读取 `RunContext[DepsT]`、协议专属 definition 与本次调用参数；无 active context 时 fail-closed，实际审批仍只经中央 Permission gate；
- Toolset instructions 按缓存稳定性分流：session-static 内容进入 SP，dynamic per-run/per-step 内容进入 request-only SR；Combined 分别传播两类内容；
- `XmlDynamicToolset[DepsT]` / `NativeDynamicToolset[DepsT]`，factory 接收类型化 `RunContext[DepsT]`，支持 per-run 与 per-step 更新；
- wrapper 与 combined view 会传播 run/step 变化标记、run-scoped lifecycle 和 async context lifecycle；
- readiness 阶段一次物化 definition snapshot，同时完成重复 Toolset ID、跨 Toolset dispatch name 冲突和 capability 解析，避免 discovery 重复执行；
- 跨协议静态类型拒绝与 Runtime fail-fast；
- `XmlFunctionToolset[DepsT]` / `NativeFunctionToolset[DepsT]`；
- XML/Native 独立 catalog，删除 `BaseTool.tool_schema/native_schema` 自动双投影；
- MCP 的 XML 与 Native 显式 Toolset/definition adapter：共享连接 owner、discovery 与 capability 调用，但不共享 wire definition；两协议的 MCP catalog 都通过 `<system-reminder>` 承载热加载，XML 只接受可忠实解码的标量参数。
- `ToolsetIdentity(id, version, protocol)` 与有序 durable manifest：新会话记录在唯一 `SessionMetaEvent` 真相源，fork 原样继承，resume 对已知 id/version/protocol/顺序不匹配 fail-closed，旧日志缺字段单向兼容。

完整设计与禁止状态见 [`TOOLSET_PROTOCOL_ARCHITECTURE.md`](./TOOLSET_PROTOCOL_ARCHITECTURE.md)。

Pydantic AI 已提供：

- Combined；
- Filtered；
- Prefixed；
- Renamed；
- Prepared；
- ApprovalRequired；
- Function；
- Dynamic factory；
- MCP Toolset。

其 fluent API 实际使用 `.filtered()`、`.prefixed()`、`.prepared()`、`.renamed()`、`.approval_required()` 等方法，位于 `$PYDANTIC_AI/pydantic_ai_slim/pydantic_ai/toolsets/abstract.py`。原始示例的方法名并不完全一致，但产品方向正确。

当前组合仍通过同一 Runtime permission、effect ledger、snapshot、settlement 和 audit 流水线。动态 Toolset 在 Agent run 开始/结束时进入和退出资源，在每次 prompt/spec 生成前刷新 per-step view；切换时先退出旧 inner 再进入新 inner。MCP 的 connection owner 与动态 Toolset lifecycle 独立，刷新 declared Toolset 不会清除 MCP category。两协议 MCP catalog 都进入 `<system-reminder>` 以支持热加载；XML builtin schema 仍只进入 system prompt，Native MCP 在 wire 上仍是普通 `NativeToolDefinition`，没有额外 `NativeMcpSpec`。

相较 Pydantic AI，当前剩余差距主要是 provider-native server tools 与 wheel/cassette 级公共 API 验证。durable Toolset identity/version 已进入 session meta；动态/MCP catalog 本身不被冻结，语义变化通过应用维护的 Toolset version 表达。因此本项尚不能标为完全关闭，但不应再描述为“缺少组合代数、Dynamic Toolset、Toolset instructions、细粒度 approval policy 或恢复身份契约”。

### 3.6 发布包仍然太重

**判断：完全属实。**

[`pyproject.toml`](../pyproject.toml) 的默认依赖包含：

- OpenAI、Anthropic 等 Provider SDK；
- MCP、FastMCP；
- Textual；
- Playwright；
- Jupyter；
- 多种 Office/PDF 文档处理库；
- Sentence Transformers、ONNX Runtime 等 ML 依赖；
- Langfuse、Sentry。

可选依赖目前主要只有 `dev` 和 `temporal`。[`setup.py`](../setup.py) 仍将五层代码打包为一个 `mote` wheel。因此，“代码分层已完成大部分，发行边界尚未对应拆开”的判断成立。

需要注意：Pydantic AI 默认主包本身也包含 OpenAI、Anthropic、Google、CLI、MCP、Evals、Web 等能力；真正的轻量对照物是 `pydantic-ai-slim`。其 Provider、CLI、MCP、Evals、Temporal 等主要通过 extras 组合，见 `$PYDANTIC_AI/pydantic_ai_slim/pyproject.toml`。

Mote 可以选择多 distribution，也可以先通过严格 extras 实现同等边界。关键验收条件不是包名数量，而是：

- kernel 最小安装不拉入 Product 依赖；
- 每个 extra 有独立安装测试；
- wheel 安装后验证公开 API；
- 核心层在缺少可选 SDK 时仍可 import 和运行。

### 3.7 静态质量门禁不足

**判断：基本属实，但 Mote 并非没有质量门禁，Pydantic AI 的 Mypy 使用范围也不宜夸大。**

Mote 当前工作树约有 429 个 `test_*.py`，已经具备：

- 自定义 AST 分层守卫；
- 公开 API 守卫；
- 构造纯度等架构测试；
- 若干并发和恢复测试；
- Python 3.10–3.12 CI，见 [`.github/workflows/ci.yml`](../.github/workflows/ci.yml)。

主要差距是：

- [`pyrightconfig.json`](../pyrightconfig.json) 仍为 `basic`，排除测试，且残留迁移前路径；
- Ruff 只配置少量规则，CI 主要使用 flake8、Black 和 isort；
- 没有全仓库 Mypy 基线；
- 没有 coverage 下限；
- 没有 pytest-xdist 门禁；
- 没有 Python 3.9、3.13、3.14 矩阵；
- 没有 extras 最小安装矩阵；
- 没有 wheel 安装后 API 测试；
- 没有 Provider cassette 矩阵；
- 并发测试已经存在，但尚未形成系统化 fault-injection 矩阵。

Pydantic AI 当前配置了 Pyright strict、coverage `fail_under = 100`、Python 3.10–3.14，以及 slim/evals/standard/all-extras/lowest-direct 等安装组合。其 Mypy 并不是全仓库第二套严格检查，主要用于 `$PYDANTIC_AI/tests/typed_agent.py`，验证公开泛型 API 的推导结果。

因此，准确结论是：**Mote 的架构守卫很有价值，但通用静态分析、覆盖率和发行矩阵尚未达到同等级别。**

### 3.8 Evals 不是独立的一等子系统

**判断：属实。**

Mote 当前没有稳定的框架级 `Dataset`、`Case`、`Evaluator`、`EvaluationContext`、报告和比较 API。搜索到的 evaluation 相关逻辑主要属于内部输出判断、权限判断、rollout 或测试脚本。

Pydantic AI 有独立的 `pydantic-evals` distribution，见 `$PYDANTIC_AI/pydantic_evals/pyproject.toml`，当前提供：

- `Case`；
- `Dataset`；
- `EvaluatorContext`；
- `Evaluator`；
- 报告与渲染；
- Span tree；
- agentic tool correctness 等 evaluator。

原始建议中的 `TraceAssertion`、`ToolCallAssertion`、`RegressionReport` 是合理的 Mote 设计候选，但不是 Pydantic Evals 当前完全同名的公共类型。应把它们描述为待建设能力，而不是 Pydantic AI API 的逐字复刻。

### 3.9 文档差距很大

**判断：强烈属实，而且当前问题不仅是文档少，也包括文档错误。**

Mote 当前约有 26 个 Markdown 文件，其中多数位于 `zdocs`；没有成熟的 MkDocs、API reference 和正式 examples 系统。

更严重的是 [`README.md`](../README.md) 仍在描述已经删除的 `common/context/executor/router/loop/roles` 旧架构，部分测试命令也继续引用旧路径。外部开发者按 README 无法准确理解当前 `contracts <- kernel <- runtime <- orchestration <- product` 五层结构。

当前 Pydantic AI 仓库约有：

- 170 个文档 Markdown；
- 41 个 Python examples；
- MkDocs strict；
- mkdocstrings API reference；
- 文档代码和示例测试；
- Provider、Toolset、Graph、MCP、Durable Execution、Evals、迁移与版本策略专题。

因此文档重写应在 facade 确定后立即开始，并以可执行示例和 wheel 安装后的公开 API 为事实源，而不是等所有内部重构完成后再集中补写。

### 3.10 Product 层仍有默认可变目录

**判断：审计时属实；原清单中的 Product 默认目录现已收口。**

审计时确认存在：

- `DEFAULT_COMMAND_REGISTRY`，见 [`product/cli/commands/core.py`](../product/cli/commands/core.py)；
- `CONSUMER_REGISTRY`，见 [`product/cli/consumers/core.py`](../product/cli/consumers/core.py)；
- `MEDIA_REGISTRY`，见 [`product/toolsets/builtin/generate_media/registry.py`](../product/toolsets/builtin/generate_media/registry.py)；
- `SEARCH_REGISTRY`，见 [`product/toolsets/builtin/web_search_registry.py`](../product/toolsets/builtin/web_search_registry.py)；
- `coding_agent_factory`，见 [`product/agents/factory.py`](../product/agents/factory.py)。

此外还有 Runtime tool registry 与 agent registry。审计时存在的
`contracts/ports/child_role.py` 模块级可变 builder 已删除；它只服务于现已由
`RunGraph` 取代并整体删除的 `CodeReview`。

这些 Registry 的类通常允许创建独立实例，说明代码已经具备一定隔离意图；问题是部分默认执行路径仍依赖模块级共享实例。`coding_agent_factory` 更像 CLI 当时使用的默认 composition root，不完全等同于可变 Registry。

Agent 工具的构造链已经改为正式依赖注入：低层
[`AgentFactory`](../contracts/ports/agent_factory.py) Protocol 只定义构造端口，
[`AgentDependencies`](../runtime/agent/wiring.py) 原子持有具体 factory，Product 的
[`CodingAgentFactory`](../product/agents/factory.py) 在 composition root 注入自身，Runtime
只通过 `build_child_agent` 能力调用端口。Product 的 Agent 工具不再导入
`coding_agent_factory` 单例，`runtime/` 也没有反向依赖 `product/`。构造与调度保持分离：
factory 只产生未启动 Agent，限额、谱系、Context provisioning、执行和关闭仍全部由
`AgentControl`/`spawn_and_run` 掌权。

当前已新增 [`ProductContainer`](../product/container.py) 与
[`Application`](../product/application.py)。每个 Application 独立持有：

- `CodingAgentFactory`；
- `LLMProviderRegistry`；
- CLI `CommandRegistry` 与 `ConsumerRegistry`；
- `MediaProviderRegistry` 与 `SearchBackendRegistry`。
- 内容寻址且不可变的 `ToolCatalog` 与 `AgentCatalog`。

命令和 consumer decorator 现在只声明 immutable definition，不再在 import 时写模块单例；`coding_agent_factory`、`DEFAULT_COMMAND_REGISTRY`、`CONSUMER_REGISTRY`、`MEDIA_REGISTRY`、`SEARCH_REGISTRY` 均已删除。Media/Search 工具通过 Tool definition 已有的零参 `capability_factory` 获得所属容器的后端 factory，Runtime 只解释该端口，不 import Product。跨容器测试同时验证：向一个租户注册 command、consumer、media provider 或 search backend，不会改变另一个租户的解析结果。

Runtime 工具类与 Agent 类型目录也已完成第二阶段收口。装饰器目录只在 Product bootstrap
期间收集 Python 声明；`ProductContainer.standard()` 随后冻结为内容寻址的 `ToolCatalog` 与
`AgentCatalog`，实际 Agent、CLI 和 Agent 工具只读取所属 Application 的快照。Markdown Agent
被直接投影到该快照，不再注册进进程目录；Agent 工具通过 definition 的正式
`capability_factory` 同时获得 Agent catalog 和 `build_child_agent` 窄能力。`with_plugins()`
产生新的 catalog generation 与 `CodingAgentFactory`，旧容器、已构造 Toolset 和运行中 session
保持原版本。Toolset identity 使用 catalog 内容版本；XML 与 Native 仍从同一 capability-type
快照分别生成各自的 nominal definition，两个 definition/catalog 不合并，MCP 的协议隔离和
system-reminder 热加载路径也未改变。

### 3.11 Fork 能力继承仍靠手工字段复制

**判断：审计时完全属实；第一阶段已通过不可变 wiring 关闭该问题。**

[`RoleSessionManager.fork()`](../runtime/agent/session_manager.py) 明确逐字段传递：

- `output_contract`；
- `run_lease_coordinator`；
- `run_lease_policy`；
- `toolsets`；
- `background_task_pool_builder`；
- `routing_strategy_builders`。

这与 [`Role.__init__`](../runtime/agent/role.py) 的扩张直接耦合。以后新增构造能力时，fork、spawn、resume、rehydrate 很容易再次遗漏。

另一条 skill fork 路径 [`runtime/agent/capabilities.py`](../runtime/agent/capabilities.py) 直接调用：

```python
type(role)(role_schema, state, config)
```

它没有显式继承 output contract、toolsets、routing、lease 等能力，说明问题不只存在于一个 fork 实现中。

当前 [`AgentDependencies`](../runtime/agent/wiring.py) 已原子持有并冻结上述 per-Agent 能力与策略，`AgentWiring` 将其与 `EngineServices` 配对；普通 fork 与 skill fork 不再逐字段同步构造参数。`CodingAgentFactory` 同时拒绝冲突的 wiring/services/dependencies 输入，防止基础设施落入 schema kwargs。

长期仍不宜把所有对象不加区分地放入单一杂项依赖包。后续公开 API 建议进一步区分：

- `EngineServices`：共享、长生命周期、需要关闭的资源；
- `AgentDependencies[DepsT, OutputT]`：deps、toolsets、output contract、routing 和 policy 等不可变定义（避免与 Kernel 已有行为定义 `AgentSpec` 重名）；
- `RunContext[DepsT]`：单次运行依赖、usage、cancel token；
- `SessionState`：fork/resume/rehydrate 时复制或恢复的数据。

当前 fork 只需继承 `AgentDependencies`、引用同一组 `EngineServices`、为隔离服务取得新的 ownership lease，并创建新的 session state，不再同步构造函数字段列表。

继续核查还发现一个不能混入“已关闭”的独立恢复边界：ResidencyStore 的默认 `BaseRole.load(role_dump)` 只能恢复 schema/state，无法从 JSON 重建包含 factory、Toolset 和服务 lease 的 `AgentWiring`。此前它会静默得到默认 wiring。

当前已由 `AgentControl` 持有正式、可注入的 `AgentIncarnationFactory`。每个实际 `BaseRole` 注册一个 immutable blueprint，只捕获具体 Role 类、配置和原子 wiring，不捕获 live state；Residency JSON 只保存 JSON-native schema/state/mailbox，rollout 仍是历史真相源。rehydrate 必须同时满足 session manifest、snapshot `type_id` 和 blueprint 具体类三重身份校验，然后用原 wiring 重建 replacement，并恢复 `agent_control` 与 agent path。没有内存 blueprint 时明确失败，因为 Residency snapshot 被定义为进程内 LRU 介质，不伪装成跨进程 Application 构造记录。

资源所有权也按语义拆开：并发 fork/spawn 使用 `AgentWiring.for_incarnation()` 获取独立 lease；Residency 是顺序 replacement，eviction 关闭旧 incarnation 的 watcher、LSP、ToolExecutor、managed runtime、sandbox、maintenance 与索引，但不释放共享 services lease，而是把同一个 ownership token 随 blueprint 转移给 replacement。这样既不重复计数，也不会在无 live Agent 的短暂窗口提前关闭 Provider/Context。`Role.dump()` 同时改为 JSON mode，避免 set/enum 等 Python 对象让 Residency 写盘失败。

## 4. 判定汇总

| 项目 | 判定 | 主要修正 |
| --- | --- | --- |
| 1. 公开 API | 审计时基本属实，核心已整改 | 已有 12 个符号的可运行根 facade；仍需兼容政策与 wheel 验证 |
| 2. 类型系统 | 审计时核心属实，主链已补齐 | Agent/Deps/Run/Tool 投影/Output/validator/Flow 已泛型化；内置 class-based 工具仍有动态边界 |
| 3. 生命周期 | 审计时强烈属实，已整改 | 分阶段 LifecycleStack 已覆盖 Agent、后台任务、MCP/LSP/Sandbox、Provider、exporter、event subscriber 与 DiskWriter |
| 4. Provider/Profile | 基本属实 | Mote 不止四个品牌入口，但专用 transport/profile/cassette 不足 |
| 5. Toolset | 审计时属实，核心已整改 | 协议隔离组合代数、动态生命周期、审批策略与 durable identity 已落地；剩 provider-native tools 和发行级验证 |
| 6. 发布包 | 完全属实 | 五层代码仍随一个重型默认安装发布 |
| 7. 质量门禁 | 基本属实 | 已有架构守卫和并发测试，但严格类型、coverage、发行矩阵不足 |
| 8. Evals | 属实 | 尚无独立稳定的框架级 eval API |
| 9. 文档 | 强烈属实 | 文档少且 README 已与当前结构冲突 |
| 10. 默认 Registry | 审计时属实，已整改 | ProductContainer/Application 持有 backend 目录及不可变 Tool/Agent catalog；插件更新生成新版本，不污染运行中 session |
| 11. 构造能力继承 | 审计时完全属实，已整改 | fork/skill fork 整体继承不可变依赖；Residency 用正式 incarnation factory 顺序转移 wiring/lease 并做三重身份校验 |

## 5. 建议的优先级

建议将原始优先级修订为：

1. 发行拆分、严格类型检查、wheel/extras 测试和新文档并行推进；
2. 建立 Provider/Profile/cassette 兼容矩阵及 provider-native server tools；
3. 继续消除内置 class-based capability 的动态依赖边界，并补齐 catalog generation 的插件加载/签名/卸载策略；
4. 建立独立 Evals 子项目。

生命周期容器和构造蓝图应视为同一个 P0 架构问题的两个侧面：前者明确资源所有权，后者明确 Agent 定义与运行状态的继承规则。facade 和泛型 API 应建立在这两个边界之上，避免先公开一套随后再次推翻的构造模型。

推荐的最小公共概念面可以是：

```text
Engine
Agent[DepsT, OutputT]
RunContext[DepsT]
Toolset[DepsT]
Model
ModelMessage
RunResult[OutputT]
```

内部的 `Role`、`RoleSchema`、`RoleState`、ComponentGraph、ContextManager、ToolExecutor、OutputEngine、SessionLog 和 control plane 仍可保留为 Runtime 或高级扩展概念，但不应成为普通单 Agent 用户的必经路径。

## 6. 其他迁移风险

本次核实还发现两个应尽快处理的问题：

1. [`pyproject.toml`](../pyproject.toml) 声明 Python `>=3.9`，但当前 CI 没有测试 Python 3.9；
2. [`.github/workflows/publish.yml`](../.github/workflows/publish.yml) 的 wheel 检查仍引用旧路径 `mote/router/ml/router.runtime.yaml`，而当前代码已迁移到新的 Product routing 路径，迁移完成后发布流程可能直接失败。

## 7. 已完成整改与验证

本轮已经落地：

- `EngineServices`、不可变 `AgentDependencies[DepsT, OutputT]` 与 `AgentWiring`，替代 Role 构造和 fork 的散装依赖字段；
- 低层 `AgentFactory` 构造端口与 `build_child_agent` 窄能力；Product Agent 工具不再读取模块级 factory，Runtime 保持零 Product 反向依赖；
- 删除已由 `RunGraph` 取代的 `CodeReview` 及其专属 child-role 全局注册器；
- `Engine[AgentT]` 异步生命周期容器；
- 顶层 `Engine → Agent[DepsT, OutputT] → RunResult[OutputT]` facade；
- `RunContext[DepsT] → ToolContext[ToolDepsT]` 显式最小权限投影；
- `FlowState[OutputT]` / `FlowResult[OutputT]`；
- 协议显式 `ToolsetIdentity`、session manifest、fork 继承与 resume mismatch fail-closed；
- 普通 resume 与 Residency rehydrate 统一走 `BaseRole.validate_resume_identity()`，错误 wiring 不再静默恢复；
- `AgentIncarnationFactory`、JSON-native Role snapshot、顺序 wiring/lease 转移及 eviction incarnation cleanup；
- fork/spawn 隔离服务的引用计数 ownership lease；
- Agent → Provider → `DiskWriter` 的有序关闭；
- Engine、Context、Role 的共享关闭任务、取消隔离与失败重试；
- `SessionRegistry` 淘汰时释放 Engine ownership；
- `LLMRouter` 对默认及等价临时配置复用 Provider client，避免 Context 追踪列表按 turn 无界增长；
- 所有非 `ztest` import 上移到模块顶部，可选依赖统一使用模块级 `try/except ImportError`；
- 零基线 AST 守卫，禁止函数、方法、类体内 import，并在 [`AGENTS.md`](../AGENTS.md) 固化规则。

验证命令：

```bash
python -m pytest \
  mote/ztest/architecture/test_local_imports.py \
  mote/ztest/architecture/test_public_api.py \
  mote/ztest/architecture/test_layer_dependencies.py \
  mote/ztest/architecture/test_constructor_purity.py \
  mote/ztest/runtime/test_engine_lifecycle.py \
  mote/ztest/product/test_agent_factory.py \
  mote/ztest/router/test_router.py \
  mote/ztest/cli/serving/test_session_registry.py \
  -q --tb=short
```

结果：上述核心回归 `67 passed`，完整架构守卫 `9 passed`，且不再出现 `DiskWriter._run()` pending-task warning。随后按用户授权补齐当前环境缺失的 `anthropic`、`pylatexenc`、`json-repair`、`asteval`，补充验证结果为：

- Provider、OpenAI Responses、数学渲染、媒体工具和 Squilla 定向套件：`239 passed`；
- 完整 `ztest/roles`：`291 passed`。

安装仅补齐 [`pyproject.toml`](../pyproject.toml) 已声明的项目依赖，没有改变依赖清单。

## 8. 最终结论

Mote 当前最主要的差距已经不是内部职责或常用 facade 是否存在，而是以下边界尚未全部产品化：

```text
内部五层能力
    ↓
已落地的统一资源所有权、构造蓝图与强类型 facade
    ↓
可独立安装、可静态验证、可执行文档化的发行产品
```

因此，原始结论可以保留，但应避免三种过度表述：

- 不应把 Pydantic AI 描述成实际只公开六个符号；
- 不应把 Mote 描述成完全没有泛型、Provider 品牌入口或质量门禁；
- 不应把所有生命周期资源、Agent 定义和 per-run 状态混装进一个无边界的依赖容器。

Mote 已经具备成熟的 Runtime、安全能力注入、持久化、多 Agent 基础，以及第一版可运行的强类型公开接口。下一阶段的重点是 Toolset/ToolContext 类型链、发行拆分、严格静态门禁、可执行文档和 Provider/Evals 生态，而不是继续扩张 `Role` 或重新暴露内部构造参数。
