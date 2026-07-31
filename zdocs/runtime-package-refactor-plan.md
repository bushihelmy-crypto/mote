# Runtime 拆包与内聚化计划

本文是 `mote.runtime` 的拆包评审稿。目标不是把现有目录机械搬成更多小目录，而是把当前已经膨胀的 runtime 按稳定业务边界重划，顺带修正已有的不合理依赖。

## 1. 背景与目标

`runtime/` 当前约 500 个 Python 文件，已经承担了多类职责：

- Agent/Role 装配与运行时状态
- 工具执行、工具 catalog、权限、MCP、工具输出压缩
- 浏览器、终端、Jupyter kernel、canvas、device 等持久交互运行时
- 上下文管理、压缩、turn reminder、skills、code map
- 会话 JSONL、checkpoint、runtime projection、hunk history
- 文件读写、文档抽取、artifact 生命周期和 mutation review
- 模型客户端、路由、failover、cost、rate limit
- event fabric、logging、sandbox、secrets、workspace、durable journal

拆包目标：

1. 每个包有单一、可命名的 bounded context。
2. 包之间依赖方向清晰，避免 runtime 内部形成双向依赖。
3. 下层服务不认识 `Role`，需要 Agent 信息时通过 contracts port 或装配期 provider 注入。
4. `runtime.tools` 只表达工具执行框架，不再承载浏览器/终端/kernel/canvas/device 的具体运行时。
5. `runtime.context` 只表达模型上下文历史与 per-turn 注入，不再囊括 skills/code map 这类独立子系统。
6. 拆包过程中同步清理不合理设计，不以兼容当前目录结构为主要约束。

跨层归属总原则：

- `product` 拥有用户可见定义、声明来源、默认策略选择、配置发现和启用策略。
- `runtime` 拥有执行机制、装配执行器、IO、安全 primitive、持久化和运行时服务。
- `orchestration` 拥有多 Agent control plane、spawn catalog 的运行时视图、调度和并发控制。
- `contracts` 只拥有跨边界数据与 port。
- `kernel` 只拥有单 Agent 的纯执行语义；无 IO、无配置发现、无持久化、无 runtime event fabric。

## 2. 当前源码证据

按文件数量看，runtime 中最大的热点包如下：

| 包 | Python 文件数 | 主要问题 |
| --- | ---: | --- |
| `runtime/tools` | 85 | 混合工具框架、MCP、权限、输出压缩、浏览器/终端/kernel/canvas/device 后端 |
| `runtime/context` | 78 | 混合历史上下文、压缩、turn context、skills、code map |
| `runtime/models` | 63 | 模型客户端、路由、failover、rate limit、cost、auth 混在同一层级 |
| `runtime/fileops` | 48 | 文本/文档读写、mutation review、artifact repository/lifecycle 混在一起 |
| `runtime/agent` | 40 | Role 本体、组件图、装配 manifest、LSP、context provider、output engine 混合 |
| `runtime/session` | 27 | JSONL 事件、checkpoint、hunk history、runtime projection/handoff/operation 混合 |

超大文件也集中在这些边界不清的域：

| 文件 | 大小 | 观察 |
| --- | ---: | --- |
| `runtime/tools/dependency/_browser.py` | 85 KB | 浏览器 session 后端，不应在 tools 框架包内 |
| `runtime/artifacts/store.py` | 73 KB | artifact store 是独立持久化域 |
| `runtime/agent/role.py` | 69 KB | Role 门面过厚，部分职责可下沉到专门 service |
| `runtime/fileops/artifact_lifecycle.py` | 61 KB | artifact 生命周期与 `runtime/artifacts` 重叠 |
| `runtime/interactive/host.py` | 52 KB | interactive host 已是独立域，应接收更多持久交互后端 |
| `runtime/models/model_gateway.py` | 51 KB | gateway/failover/routing 边界需继续收敛 |
| `runtime/fileops/edit_plans.py` | 49 KB | mutation planning/review 应从普通文件读写中拆出 |
| `runtime/tools/dependency/_kernel.py` | 44 KB | kernel runtime 后端，不属于工具执行框架 |
| `runtime/fileops/cursor_registry.py` | 40 KB | artifact/file cursor 生命周期域 |
| `runtime/session/events.py` | 38 KB | session event schema 可再按事件族拆分或移动到 contracts |

当前 runtime 内部跨包依赖的主要热点：

| 依赖 | 次数 | 判断 |
| --- | ---: | --- |
| `runtime.agent -> runtime.session` | 21 | 正常，Role 装配依赖会话持久化 |
| `runtime.session -> runtime.fileops` | 19 | 偏强，session 的 hunk/history 过度知道 fileops 内部 |
| `runtime.agent -> runtime.context` | 17 | 正常，Role 装配依赖 context 服务 |
| `runtime.agent -> runtime.tools` | 16 | 正常，Role 装配依赖工具执行 |
| `runtime.tools -> runtime.interactive` | 12 | 说明 interactive 后端被 tools 承载，应反向整理为 tool 调用 interactive |
| `runtime.artifacts -> runtime.fileops` | 9 | 不合理，artifact 域依赖 fileops 的 artifact_* 细节，说明域切错 |
| `runtime.tools -> runtime.sandbox` | 5 | 可以接受，但应限制在 permission/sandbox adapter |
| `runtime.tools -> runtime.agent` | 1 | 不合理，工具框架不应知道 Agent/Role |
| `runtime.context -> runtime.agent` | 1 | 不合理，context source 不应直接读 Agent control |

还存在一个明确分层违规：

- `contracts/ports/hook_runner.py` 在 `TYPE_CHECKING` 中 import `mote.runtime.hook.types.HookOutcome`。即使是 type-only，也让 contracts 命名 runtime 类型。`HookOutcome` 等纯数据类型应移到 contracts。

## 3. 目标包结构

目标结构按领域边界划分：

```text
runtime/
  agent/
    role.py
    role_schema.py
    role_state.py
    components/
    capabilities.py
    wiring.py

  lsp/

  output/

  context/
    history/
    compaction/
    turn/

  code_map/

  prompt/

  tools/
    execution/
    catalog/
    policy/
    result/
    mcp/
    permission/
    compress/

  interactive/
    host/
    browser/
    terminal/
    kernel/
    canvas/
    device/
    video/

  fileops/
    facade.py
    text/
    documents/
    mutation/
    journal/

  artifacts/
    repository/
    store/
    publication/
    lifecycle/
    gc/

  session/
    log/
    events/
    checkpoint/
    history/
    runtime_state/

  models/
    clients/
    routing/
    failover/
    accounting/
    ratelimit/
    auth/

  events/
  sandbox/
  secrets/
  workspace/
  durable/
  observability/
  config/
```

该结构表达的依赖方向：

```text
agent composition
  -> context/history, context/turn, injected skill/code-map providers
  -> tools/execution
  -> session, artifacts, fileops
  -> models
  -> interactive

tools/execution
  -> tools/catalog, tools/policy, tools/result
  -> events, ledger, workspace
  -> interactive only through explicit runtime driver protocols

context/history
  -> context/compaction
  -> tools/result persistence only through an output-spill port

session
  -> events, artifacts, fileops/mutation public facade

artifacts
  -> disk, workspace paths
  -> must not depend on fileops artifact_* modules after migration

fileops
  -> artifacts public repository/store APIs
  -> disk, paths
```

## 4. 必须先改的设计问题

### 4.1 Hook 类型上移到 contracts

当前问题：

- `contracts/ports/hook_runner.py` 引用 `runtime.hook.types.HookOutcome`。
- `runtime.hook.types` 自称纯数据、dependency-free，但实际放在 runtime。

目标：

- 新增 `contracts/hooks/types.py`。
- 只移动跨边界数据契约：`HookEvent`、`HookBehavior`、`HookOutcome`。
- `EMPTY`、`fold`、外部命令 wire 编解码和 handler 聚合策略留在 `runtime.hook`。这些是运行时执行策略，不属于 contracts。
- `HookInput` 是外部命令 handler 的 wire adapter DTO，留在 `runtime.hook`。
- `runtime.hook.types` 改为导入 contracts 数据类型并承载 runtime 聚合函数，或拆成 `runtime/hook/outcome.py`、`runtime/hook/wire.py`。
- product 只负责 hook 配置来源和用户声明；`HookManager`、matcher、command handler executor、timeout、wire adapter、fold/fail-closed 聚合都留在 `runtime.hook`。

验收：

- `contracts/` 不 import `mote.runtime.*`。
- 扩展 `ztest/architecture/test_layer_dependencies.py`，让它检查 `TYPE_CHECKING` 下的 import；禁止 `contracts -> runtime`。

### 4.2 消除 `tools -> agent`

当前问题：

- `runtime/tools/agent_registry.py` import `runtime.agent.base.BaseRole`，用于校验注册的 agent 是否可运行。
- 这让工具框架知道 Role 基类，方向不对。
- `runtime/tools/agent_registry.py` 混合了两类职责：Python agent declaration 收集/装饰器，以及 spawnable role catalog。

目标：

- 明确拆分 ownership：
  - `register_agent`、Python declaration 收集、Markdown agent discovery 归 `product.agents`。
  - spawn catalog、agent descriptor、agent factory 归 `orchestration.environment`，因为它服务多 Agent control plane 的 spawn。
  - `contracts` 只定义 `AgentDescriptor`、`AgentFactory`、`SpawnableAgentCatalog` 等 port/data，不用 runtime-checkable class Protocol 模拟 `issubclass`。
  - `runtime.agent` 只拥有单 Agent `Role`、`RoleSchema`、`RoleState`、capability publication 和 component wiring；不拥有全局/应用级“有哪些 Agent 可 spawn”的 catalog。
- `runtime.tools` 不再 import `runtime.agent`。

验收：

- `runtime/tools/**` 中无 `mote.runtime.agent` import。
- product agent declaration 不依赖 runtime.tools。
- orchestration spawn 只依赖 contracts catalog/factory port 和 product 注入的 factory implementation。

### 4.3 消除 `context -> agent`

当前问题：

- `runtime/context/turn_context/sources/team.py` import `runtime.agent.control.resolve_control`。
- Team context source 是上下文渲染源，但它直接知道 Agent control 的 ambient 解析。

目标：

- 定义 `contracts/ports/team_roster.py`，暴露 `TeamRosterProvider`。
- `TeamContextSource` 构造时接收 provider；Role/Environment 装配期负责把 control adapter 注入进去。
- `context` 不 import `agent`，也不 import `orchestration`。

验收：

- `runtime/context/**` 中无 `mote.runtime.agent` import。
- 单 Agent 场景 provider 为空时继续 self-suppress。

### 4.4 拆出 interactive runtime drivers

当前问题：

- `runtime/tools/dependency` 中的 `_browser.py`、`_browser_runtime.py`、`_kernel.py`、`_terminal.py`、`_canvas.py`、`_device/*`、`_video.py` 实际是持久运行时/设备后端。
- product 内置工具大量直接 import `runtime.tools.dependency.*`。

目标：

- 迁移到 `runtime/interactive/{browser,terminal,kernel,canvas,device,video}`。
- `product/toolsets/builtin/*` 直接依赖 `runtime.interactive.*` driver。
- `runtime/tools` 只保留工具执行框架和与工具协议直接相关的类型。

验收：

- `runtime/tools/dependency` 不存在，或只剩 `_paths.py` 这种真正工具参数解析 helper。更推荐把 `_paths.py` 移到 `product/toolsets/builtin/_paths.py`，因为它服务内置文件工具。
- `runtime/tools` 文件数显著下降，且无 browser/kernel/terminal/canvas/device 实现。

## 5. 跨层归属调整：哪些不该继续留在 runtime

拆包不能只在 `runtime/` 内部搬目录。按当前源码看，确实有一部分 runtime 代码应该上移到 `product`，也有少量纯执行语义可以考虑下沉到 `kernel`。判断标准如下：

- 依赖用户目录、CLI 配置、内置工具、产品约定、提示文案、`.mote/*` 文件布局：上移 `product`。
- 纯单 Agent 执行语义、协议无关、无 IO、无配置、无持久化、无事件总线依赖：可下沉 `kernel`。
- 涉及 IO、session、workspace、权限、sandbox、secrets、模型客户端、event fabric、durable state：留在 `runtime`。

### 5.1 应上移 product 的内容

| 当前路径 | 建议目标 | 原因 |
| --- | --- | --- |
| `runtime/agent/agents/markdown_loader.py` | `product/agents/markdown_loader.py` | 它读取 `.mote/agents/*.md` 和 `~/.mote/agents`，并动态构造具体 `Role` 子类。这是产品层 agent declaration/discovery，不是 runtime 基础设施。当前 `product/agents/discovery.py` 已经只是在调用它。 |
| `runtime/tools/agent_registry.py` 的 declaration/装饰器部分 | `product/agents/registry.py` | Python/Markdown agent declaration 是产品级扩展点。运行时不应持有进程全局 declaration collector。 |
| `runtime/tools/dependency/_paths.py` | `product/toolsets/builtin/_paths.py` | 该模块服务 `read/search/edit/canvas` 等内置工具的参数解析和权限路径解析，不是通用 runtime 能力。 |
| `runtime/tools/permission/prompts.py` | `product/cli/io/approval_prompt.py` | 这是 fallback console 的英文 approval 文案与 free-text parse。结构化权限决策是 contracts/runtime，具体人机文案是 product。 |
| `runtime/prompt/policy.py` 的用户定义/默认策略选择部分 | `product/prompt/policy.py` | Prompt policy 的用户定义、扩展 roster 和默认安全姿态属于 product。runtime 保留 policy runner、secret capture/redaction 调用、hook firing、timeout/fail-closed 等执行机制。 |
| `runtime/tools/mcp/config_source.py` | `product/integrations/mcp/config_source.py` | MCP 配置文件发现/加载更像产品集成配置。runtime 可保留 MCP lifecycle/adapter 抽象，但文件路径策略应由 product 注入。 |
| `runtime/context/skills/*` | `product/skills/*` | Skills 是用户/产品可见能力：`SKILL.md` definition/discovery、audit、pool、injector、启用策略和模型可见文案归 product。runtime.context 只消费注入的 prompt fragment/provider。 |

### 5.1.1 LSP 是 runtime capability

`runtime/agent/lsp/*` 当前由 `runtime/agent/runtime_modules/integrations.py` 直接构造，`product/cli/consumers/acp/server.py` 还引用了 LSP 私有函数 `_rpc_error`。目标不是单纯改路径，而是把 LSP 变成通过 port/factory 注入的 runtime capability。

确定方向：

- LSP 是通用 runtime capability，最终归 `runtime/lsp`。
- product 负责 LSP 配置、启用策略、CLI/ACP 暴露和 composition root 注入。
- 定义 `LspServiceFactory` / `DiagnosticsProvider` 等窄 port。
- Product composition root 注入默认 runtime LSP implementation。
- ACP 自己实现 ACP JSON-RPC error 编码，停止引用 LSP 私有 `_rpc_error`。
- `runtime.agent` 只持有 port，不 import 具体 LSP server/manager。

上移 product 后的约束：

- `runtime` 通过 contracts port 接收这些能力，不 import product。
- `product` 在 composition root 装配默认实现。
- 内置工具继续在 `product/toolsets/builtin`，但它们依赖 runtime 的稳定 executor/result/capability contracts。

### 5.2 可下沉 kernel 的内容

这些内容不能直接搬。必须先剥离 runtime 副作用，确保 kernel 不 import runtime。

| 当前路径 | 建议目标 | 前置条件 |
| --- | --- | --- |
| `runtime/tools/tool_convert.py` 和 `runtime/tools/docstring_parser.py` | `kernel/tools/docstring_schema.py` | 目前只做 inspect + docstring parsing，`GoogleDocstringParser` 只依赖 contracts text helper，是纯 schema 生成逻辑，可一起下沉。 |
| `runtime/tools/definitions.py` 的 `render_*_capability` / `xml_definition` / `native_definition` | `kernel/tools/capability_rendering.py` | 这些把 capability class 渲染成 XML/native tool definition，语义靠近 `kernel.tools.definitions`。前提是不要依赖 runtime tool implementation。 |
| `runtime/agent/context_provider/request.py` | `kernel/think/request.py` | `ThinkRequest` 只依赖 contracts output decision 和泛型字段，是一次 think 的纯输入 DTO。 |
| `runtime/agent/context_provider/base.py` | `kernel/flow/context_provider.py` | Flow 实际消费的是窄接口；该 ABC 依赖 kernel flow context 与 contracts model route，不应在 runtime.agent。 |
| `runtime/agent/output_engine.py` 的纯状态机部分 | `kernel/output/engine.py` | 当前 `OutputEngine` 依赖 runtime errors、events、session fact sink，不能整体下沉。可拆成 kernel 纯 validation/lifecycle reducer + runtime 持久化/事件 wrapper。 |
| `runtime/agent/output_migration.py` | `kernel/output/migration.py` | 该模块只依赖 contracts output/ports 与 dataclass，属于 output contract/schema migration 的纯执行语义。 |
| `runtime/agent/graph_output_service.py` 的纯 graph-output contract glue | `kernel/output/graph_service.py` | 当前绑定 runtime lease/session fact，不能整体下沉；应拆出纯提交协议，runtime wrapper 处理 lease 和 durable commit。 |

不建议下沉 kernel 的内容：

- `runtime.models.clients.*`：即使 `BaseLLM` 抽象看起来通用，也涉及客户端、credentials、retry、provider 运行时行为，留 runtime；kernel 只保留 provider-neutral invocation DTO/selector 语义。
- `runtime.context.compaction.reducers.summarize`：调用 `kernel.models.generate`，但它需要模型调用和上下文持久策略，留 runtime/context。
- `runtime.events.*`：虽然 kernel 有 telemetry hook，但 runtime event fabric 是运行时观察/持久化设施，不能下沉。
- `runtime.tools.base_tool` / `tool_executor`：工具执行涉及 capability injection、permission、ledger、result persistence，留 runtime。

### 5.3 应保持 runtime 的核心

以下内容即使被 product 高频 import，也不应上移：

- `runtime.engine`、`runtime.services`、`runtime.lifecycle`
- `runtime.models.clients`、`runtime.models.failover`、`runtime.models.ratelimit`、`runtime.models.cost/accounting`
- `runtime.tools.execution`、`runtime.tools.result`、`runtime.tools.permission` 的执行时策略部分
- `runtime.session`、`runtime.artifacts`、`runtime.fileops`
- `runtime.sandbox`、`runtime.secrets`、`runtime.disk`、`runtime.workspace`
- `runtime.events`、`runtime.durable`、`runtime.observability`

这些是产品装配会使用的运行时服务，而不是产品特性本身。

### 5.4 应上移 orchestration 的内容

| 当前职责 | 建议目标 | 原因 |
| --- | --- | --- |
| spawnable agent catalog、agent descriptor lookup、agent factory | `orchestration/environment/agent_catalog.py` 和 `orchestration/environment/factory.py` | Catalog 服务多 Agent control plane 的 spawn/resume/lineage，不是工具框架，也不是单 Agent runtime。 |

product 负责声明和发现 agent 类型；orchestration 负责把声明冻结成可 spawn catalog 并执行 factory；contracts 定义二者之间的数据和 port。

## 6. 详细迁移计划

### Phase 0：建立边界测试与指标

目的：先让后续拆包有护栏。

工作项：

1. 扩展现有 `ztest/architecture/test_layer_dependencies.py`，不要在 `ztest/runtime` 另写一套平行 AST 扫描。
2. 修改 `_RuntimeImports` visitor，使其同时记录 `TYPE_CHECKING` 下的 import；type-only import 也不得违反层级方向。
3. 在现有架构测试中增加 runtime 内部边界规则，至少覆盖：
   - `contracts/**` 禁止 import `mote.runtime.*`。
   - `runtime.tools/**` 禁止 import `mote.runtime.agent`、`mote.runtime.context`。
   - `runtime.context/**` 禁止 import `mote.runtime.agent`、`mote.runtime.tools`，例外只允许经过明确 port 的输出 spill。
   - `runtime.artifacts/**` 禁止 import `mote.runtime.fileops.artifact_*`。
4. 记录当前白名单，Phase 1-4 逐步消除白名单，而不是扩大白名单。
5. 增加结构化指标脚本或测试输出，作为每个 phase 的前后对比：
  - runtime 内部 import graph 的非平凡 SCC 数量、非平凡 SCC 节点总数、最大非平凡 SCC size。
   - 非法边数量。
   - 每个 public package `__all__` / `__init__.py` re-export 数量。
   - 跨包 fan-out top N 文件和 top N 包。

验收命令：

```bash
python -B -m pytest ztest/architecture -q --tb=short
```

### Phase 1：修正 contracts 与反向依赖

目的：先消掉会阻碍拆包的设计问题。

工作项：

1. 移动 hook pure data types 到 contracts：
   - 上移 `HookEvent`、`HookBehavior`、`HookOutcome`。
   - `HookInput`、`EMPTY`、`fold`、command handler executor、wire adapter 留在 `runtime.hook`。
   - product hook 配置来源注入 runtime hook manager。
2. 拆掉 `runtime/tools/agent_registry.py`：
   - `register_agent`、Python declaration collector、Markdown discovery 迁到 `product.agents`。
   - spawn catalog、agent descriptor lookup、agent factory 迁到 `orchestration.environment`。
   - contracts 定义 `AgentDescriptor`、`AgentFactory`、`SpawnableAgentCatalog`。
   - 同步更新 product/orchestration 中所有 agent catalog import。
3. 给 TeamContextSource 引入 `TeamRosterProvider`：
   - contracts 定义 provider protocol 和 data shape。
   - environment/agent control adapter 实现 provider。
   - Role component 装配 provider，context source 只依赖 provider。

风险：

- `AgentCatalog`、`declared_agent_catalog`、`register_agent` 已被 product/CLI/tests 直接引用；需要一次性全局替换到 product declarations 与 orchestration spawn catalog。
- Team roster 的增量持久化行为不能改变，尤其是 compaction 后 frontier reset。

验收：

```bash
python -B -m pytest ztest/hook ztest/roles ztest/context -q --tb=short
python -B -m pytest ztest/architecture ztest/product ztest/environment ztest/turn_context/test_team.py -q --tb=short
```

### Phase 2：拆 `runtime.tools.dependency`

目的：把工具执行框架与交互后端分开。

迁移映射：

| 当前路径 | 目标路径 |
| --- | --- |
| `runtime/tools/dependency/_browser.py` | `runtime/interactive/browser/session.py` |
| `runtime/tools/dependency/_browser_runtime.py` | `runtime/interactive/browser/driver.py` |
| `runtime/tools/dependency/browser_profile.py` | `runtime/interactive/browser/profile.py` |
| `runtime/tools/dependency/_terminal.py` | `runtime/interactive/terminal/driver.py` |
| `runtime/tools/dependency/terminal_vt.py` | `runtime/interactive/terminal/vt.py` |
| `runtime/tools/dependency/_kernel.py` | `runtime/interactive/kernel/driver.py` |
| `runtime/tools/dependency/notebook_export.py` | `runtime/interactive/kernel/notebook_export.py` |
| `runtime/tools/dependency/_canvas.py` | `runtime/interactive/canvas/driver.py` |
| `runtime/tools/dependency/canvas_*` | `runtime/interactive/canvas/*` |
| `runtime/tools/dependency/canvas_backends/*` | `runtime/interactive/canvas/backends/*` |
| `runtime/tools/dependency/_device/*` | `runtime/interactive/device/*` |
| `runtime/tools/dependency/_video.py` | `runtime/interactive/video.py` |
| `runtime/tools/dependency/_session_state.py` | `runtime/interactive/session_state.py` |
| `runtime/tools/dependency/_paths.py` | `product/toolsets/builtin/_paths.py` |

代码设计要求：

- runtime driver 不 import `BaseTool`、`ToolExecutor`、tool catalog。
- product 内置工具负责把 tool args 转成 driver 调用。
- driver 返回领域结果；工具层再包装成 `ToolResult`。
- video URL/path 判断已被 curl compressor 复用，移到更中性的 `runtime/media/video.py`，不要让 compressor import interactive driver。

验收：

```bash
python -B -m pytest ztest/runtime ztest/executor/dependency ztest/executor/tools ztest/product -q --tb=short
```

### Phase 3：重划 context、skills、code_map

目的：让 context 表达“模型上下文”，把 Skills 上移 product，并把 Code Map 拆成 runtime 执行机制与 product 策略/展示 adapter。

迁移映射：

| 当前路径 | 目标路径 |
| --- | --- |
| `runtime/context/manager.py` | `runtime/context/history/manager.py` |
| `runtime/context/budget.py` | `runtime/context/history/budget.py` |
| `runtime/context/prompt.py` | `runtime/context/history/prompt.py` |
| `runtime/context/sanitization.py` prompt injection / model-facing sanitization | `runtime/prompt/sanitization.py` |
| `runtime/context/sanitization.py` token truncation / token budget helpers | `runtime/context/token_budget.py` |
| `runtime/context/visibility.py` | `runtime/context/history/visibility.py` |
| `runtime/context/turn_context/*` | `runtime/context/turn/*` |
| `runtime/context/skills/*` | `product/skills/*` |
| `runtime/context/code_map/*` indexing/store/tree-sitter execution | `runtime/code_map/*` |
| `runtime/context/code_map/*` path/language enablement policy | `product/code_map/*` |
| `runtime/context/turn_context/sources/code_map.py` prompt adapter | product-controlled adapter injected into runtime turn bus |

代码设计要求：

- `ContextManager` 只负责 stored conversation、token budgeting、compaction 调度。
- Turn context source 不直接依赖 Role、ToolExecutor、Environment；需要 live 数据时通过 provider 注入。
- 不新增 generic `runtime/text` 包；prompt injection marker 与模型可见 sanitization 归 `runtime/prompt/sanitization.py`，token truncation/budget helper 归 `runtime/context/token_budget.py`。
- Skills 归 product：`product/skills` 拥有 definition、discovery、audit、pool、injector、启用策略和模型可见文案。
- `contracts/ports` 定义 `SkillCatalog` / `SkillResolver` / `SkillPromptProvider`；runtime.context 只消费 prompt fragment/provider。
- `runtime.agent` 只装配注入的 skill port，不返回具体 `SkillPool`。
- `product/toolsets/builtin/skill_tool.py` 继续是产品内置工具，通过 skill port 调用 product skills。
- Code Map 的 SQLite、tree-sitter、indexer/store 等执行机制留 `runtime/code_map`。
- Code Map 的 `~/.mote`/项目路径策略、语言启用策略、turn prompt adapter 由 product 控制；runtime turn bus 只接收 provider。
- `runtime.code_map` 从构造参数接收 `store_path` / enabled language set；禁止读取 `CONFIG_ROOT`、执行 `.mote` discovery 或自行决定语言启用策略。

需要特别处理：

- `runtime/context/compaction/reducers/spill.py` 当前依赖 `runtime.tools.tool_result_limit`。这说明“历史压缩”复用了“工具输出落盘”能力。抽出 `runtime/resources/spill.py`，让 context 和 tools 都依赖中性 spill service。

验收：

```bash
python -B -m pytest ztest/context ztest/skills ztest/product ztest/roles -q --tb=short
python -B -m pytest ztest/architecture ztest/turn_context -q --tb=short
```

### Phase 4：统一 artifact 域，瘦身 fileops

目的：先拆清 artifact 与 fileops 的边界，再迁移真正通用的 artifact 基础设施。禁止把 `fileops/artifact_*` 整体搬到 `runtime.artifacts` 后留下更多 `artifacts -> fileops` 引用。

当前判断：

- `runtime/fileops/artifact_repository.py` 依赖 fileops lifecycle/locking，不能直接视为通用 artifact repository。
- `runtime/fileops/artifact_reachability.py` 依赖 edit plan 与 journal，属于 FileOps reachability roots，不应整体进入通用 artifacts。
- `runtime/fileops/artifact_gc.py` 依赖 cursor registry，说明 GC 需要 pin/root source 抽象后才能通用化。
- `runtime/fileops/artifact_budgets.py` 依赖 metadata manifest，预算常量需要按 artifact 物理存储和 file metadata manifest 分拆。

目标边界：

| 边界 | 归属 | 内容 |
| --- | --- | --- |
| 通用 artifact 基础设施 | `runtime.artifacts` | CAS/blob store、artifact id/layout、reservation primitive、generic lifecycle state、物理 GC executor、publication/resolver |
| FileOps artifact roots/pins | `runtime.fileops` | edit-plan/read/search manifests、cursor registry、hunk/read roots、FileOps reachability source |
| 桥接 port | `runtime.artifacts.ports` | `ArtifactRootSource`、`ArtifactPinSource`、`ArtifactReservationJournal`、`ArtifactMetadataSource`。这是 runtime 内部域间 port，不放 contracts。 |
| CAS public reference type | `contracts.artifacts.ArtifactContentRef` | 通用 artifact repository/store API 统一使用 `ArtifactContentRef`；`contracts.fileops.BlobRef` 不进入 `runtime.artifacts` public API。FileOps 在自身边界做 adapter。 |

迁移顺序：

1. 通用化 CAS 引用类型：
   - `runtime.artifacts.repository` public API 使用 `ArtifactContentRef`。
   - `contracts.fileops.BlobRef` 只保留为 FileOps 内部/history 视图。
   - FileOps 通过 adapter 将自己的 blob/history 视图映射到 `ArtifactContentRef`。
   - `runtime/artifacts/repository_blobs.py` 改为通用 adapter 或迁到 `runtime/fileops/artifacts/blob_adapter.py`，不能让通用 artifacts 依赖 FileOps DTO。
2. 定义 `ArtifactRootSource` / `ArtifactPinSource`：
   - artifacts GC 只消费 roots/pins port。
   - fileops 实现 roots/pins provider，读取 edit plans、journal、cursor registry。
3. 拆 `artifact_reachability.py`：
   - 通用 reachability graph traversal 放 `runtime.artifacts.gc.reachability`。
   - FileOps root discovery 留在 `runtime.fileops.mutation.artifact_roots`。
4. 拆 `artifact_gc.py`：
   - 物理删除、ttl/budget enforcement 放 `runtime.artifacts.gc.collector`。
   - FileOps cursor/pin collection 留在 fileops provider。
5. 拆 `artifact_repository.py`：
   - CAS/blob/layout/reservation primitive 放 `runtime.artifacts.repository`，API 使用 `ArtifactContentRef`。
   - 依赖 fileops locking/journal 的 scoped mutation repository 留在 `runtime.fileops.mutation.artifacts`，通过 artifacts repository API 写 blob。
6. 拆 `artifact_budgets.py`：
   - artifact store hard limits/ttl 放 `runtime.artifacts.budgets`。
   - metadata manifest 相关限制留 `runtime.fileops.metadata_manifest`。

禁止的迁移：

| 当前路径 | 目标路径 |
| --- | --- |
| `runtime/fileops/artifact_reachability.py` 整体迁入 artifacts | 会把 edit plan/journal 依赖带入通用 artifacts，违反验收条件 |
| `runtime/fileops/artifact_gc.py` 整体迁入 artifacts | 会把 cursor registry/pin discovery 带入 artifacts |
| `runtime/fileops/artifact_repository.py` 整体迁入 artifacts | 会把 fileops locking/lifecycle 混入 artifacts repository |

`fileops` 目标子域：

```text
runtime/fileops/
  facade.py
  text/
    encoding.py
    sources.py
    text_views.py
    byte_views.py
    pdf_views.py
    search.py
    ripgrep.py
  documents/
    extraction.py
    budgets.py
    adapters/
  mutation/
    edit_plans.py
    mutation_factory.py
    transactions.py
    review.py
    rewind.py
    hunk_projection.py
  journal/
    journal.py
    locking.py
    control.py
    checkpoints.py
    snapshots.py
    read_cursors.py
```

代码设计要求：

- `runtime.artifacts` 不 import `runtime.fileops`。
- `runtime.artifacts` public API 不使用 `contracts.fileops.BlobRef`。
- `runtime.fileops` 可以 import `runtime.artifacts` 的公开 CAS/repository/store API，并实现 artifacts 所需 roots/pins port。
- `session/hunk_ops.py` 不能直接拼装 artifact repository 内部状态；应通过 mutation/artifact façade。
- `artifact_reachability.py` 的 FileOps root discovery 不进入通用 artifacts。

验收：

```bash
python -B -m pytest ztest/fileops ztest/runtime ztest/session ztest/executor/test_document_adapters.py -q --tb=short
python -B -m pytest ztest/architecture -q --tb=short
```

### Phase 5：瘦身 agent 装配层

目的：`runtime.agent` 只保留 Role 运行门面、状态、组件图和 capability publication。

迁移映射：

| 当前路径 | 目标路径 |
| --- | --- |
| `runtime/agent/runtime_modules/*` | `runtime/agent/components/*` |
| `runtime/agent/lsp/*` | `runtime/lsp/*` |
| `runtime/agent/context_provider/request.py` | `kernel/think/request.py` |
| `runtime/agent/context_provider/base.py` | `kernel/flow/context_provider.py` |
| `runtime/agent/context_provider/provider.py` | `runtime/agent/components/context_provider.py`，作为持有 Role 的 concrete composition adapter |
| `runtime/agent/output_migration.py` | `kernel/output/migration.py` |
| `runtime/agent/output_engine.py` | 拆成 `kernel/output/engine.py` 纯 reducer/state machine + `runtime/output/engine.py` commit/event wrapper |
| `runtime/agent/graph_output_service.py` | 拆成 `kernel/output/graph_service.py` 纯提交协议 + `runtime/output/graph_service.py` lease/session wrapper |
| `runtime/agent/agents/markdown_loader.py` | `product/agents/markdown_loader.py` |

代码设计要求：

- `RoleComponents` 仍是 composition root，但 component manifests 应按领域从目标包导入。
- `agent/components/session.py` 这种 manifest 可以依赖很多服务，因为它是装配层；领域服务本身不能反向依赖 agent。
- `Role` 中与 hunk/file history、session listing、runtime handoff 相关的 delegation helper 迁到 `RoleSessionManager` 或更小 façade。
- LSP 迁移必须先定义 `LspServiceFactory` / `DiagnosticsProvider` port；product 注入启用策略和默认 runtime implementation。
- ACP JSON-RPC error 编码移到 product ACP，不再引用 runtime LSP 私有 `_rpc_error`。
- Markdown loader 上移 product 后，runtime.agent 不再包含 `agents/` declaration discovery 子包。
- concrete `ContextProvider` 在去除 Role 依赖前不搬到 `runtime.context`；它是 agent composition adapter，目标路径为 `runtime/agent/components/context_provider.py`。

验收：

```bash
python -B -m pytest ztest/roles ztest/runtime ztest/flow ztest/cli -q --tb=short
```

### Phase 6：通用 failover/admission 抽取

目的：把 provider-neutral admission/failover 从模型域抽到 `runtime.resilience`，让 models 和 service_gateway 共同依赖同一 runtime resilience 能力。

当前问题：

- `runtime/service_gateway/gateway.py` 明确服务 externally hosted Tool capabilities，不是模型专属 gateway。
- 它当前依赖 `runtime.models.failover.admission` 和 `runtime.models.failover.policy`，说明通用 admission/failover 实现放在 models 下，导致 service gateway 反向借用模型域能力。
- `runtime/models` 已经有 clients/routing/failover/cost/ratelimit/auth 子包；单纯 `cost -> accounting` 命名搬迁收益不足。

迁移范围：

- `runtime.models.failover.admission.ResourceAdmissionController` -> `runtime.resilience.admission.ResourceAdmissionController`
- `runtime.models.failover.admission.AdmissionRejectedError` -> `runtime.resilience.admission.AdmissionRejectedError`
- `runtime.models.failover.policy.DefaultFailoverPolicy` / `FailoverPolicy` -> `runtime.resilience.failover.policy`
- provider-neutral `classify_failure` -> `runtime.resilience.failover.classification`
- `runtime.models` 和 `runtime.service_gateway` 共同依赖 `runtime.resilience`，各自保留模型或外部服务的 adapter/snapshot/planner。
- `runtime.models.failover` 保留模型特有 planning/snapshot/operator/state，不再承载通用 admission/failure policy。

验收：

- `runtime.service_gateway` 不依赖 `runtime.models.failover.*`。
- `runtime.models.failover` 可以依赖 `runtime.resilience`，反向禁止。
- 不使用不存在的 `ztest/models`。模型相关验证用现有 `ztest/router`、`ztest/oauth`、相关 `ztest/roles` 和 product integration 测试。

```bash
python -B -m pytest ztest/architecture ztest/router ztest/oauth ztest/roles ztest/product/integrations -q --tb=short
```

## 7. 迁移策略

每个 phase 使用相同流程：

1. 先加边界测试，当前违规可临时白名单。
2. 引入目标包与目标 public API。
3. 移动代码并更新 imports。
4. 删除旧路径，不保留长期 re-export。
5. 跑相关 ztest。
6. 移除对应白名单。

不建议做“全仓一次性移动”。原因不是迁就当前代码，而是这些域有真实行为风险：session replay、artifact migration、interactive runtime checkpoint、tool result persistence 都有持久化兼容要求。分 phase 能把每次变更的行为面控制住。

## 8. Import 边界规则

建议拆包后固定以下规则：

```text
contracts
  may import: stdlib, pydantic, typing
  must not import: runtime, kernel, orchestration, product

runtime.tools.execution
  may import: contracts, runtime.events, runtime.ledger, runtime.workspace, runtime.tools.result/catalog/policy
  must not import: runtime.agent, runtime.context.history, product

runtime.interactive
  may import: contracts, runtime.events, runtime.secrets, runtime.sandbox where needed
  must not import: runtime.tools.execution, product

runtime.context.history
  may import: contracts, runtime.context.compaction, runtime.events
  must not import: runtime.agent, runtime.tools.execution

runtime.prompt
  owns prompt admission runner/sanitization execution primitives
  must not import: product

runtime.context.token_budget
  owns token truncation/budget helpers used by context compaction/history
  must not import: product, runtime.agent

runtime.code_map
  may import: contracts and tree-sitter/sqlite execution dependencies
  receives store_path/language enablement via constructor
  must not import: runtime.agent, runtime.tools, runtime.paths, product

product.skills
  owns Skill definition/discovery/audit/pool/injector/default enablement
  runtime consumes only contracts skill ports

product.code_map
  owns path/language enablement policy and turn prompt adapter registration
  runtime.code_map owns indexing/store/extraction execution

runtime.artifacts
  may import: contracts, runtime.disk, runtime.paths, runtime.artifacts internal ports
  must not import: runtime.fileops

runtime.fileops
  may import: contracts, runtime.artifacts, runtime.disk, runtime.paths
  must not import: runtime.agent

runtime.lsp
  may import: contracts, runtime.events/logging/process as needed
  must not import: product, runtime.agent

orchestration.environment
  owns spawn catalog/factory runtime view
  may depend on contracts and runtime.agent public Role factory surface

product
  owns user declarations/config/default policies and injects them downward
  runtime must not import product

runtime.agent
  may import: runtime services as composition root
  must not be imported by lower runtime services
```

## 9. Public API 原则

每个包只暴露一个小的 public surface：

- `runtime.tools`: `BaseTool`, `ToolExecutor`, `ToolResult`, `ToolCatalog`, `ToolPolicy`
- `runtime.interactive`: `RuntimeHost` 和各 driver 的 public class
- `runtime.lsp`: `LspService`、diagnostics buffer/provider、server manager public factory
- `runtime.context`: `ContextManager`, `ContextVisibility`, turn bus public types
- `runtime.prompt`: prompt admission runner and prompt sanitization primitives
- `runtime.code_map`: `CodeMap`, `RepoIndexer`, public model types
- `product.skills`: `SkillDefinition`, `SkillPool`, `SkillCatalog`, `SkillInjector`
- `runtime.fileops`: `FileOperations`，以及必须给 session/product 用的 text/mutation façade
- `runtime.artifacts`: `DurableArtifactStore`, `ArtifactRepository`, `ArtifactContentRef`-based CAS API, resolver/publisher
- `runtime.session`: `SessionLog`, event codec, replay/listing/checkpoint façade

禁止在 `__init__.py` 中重新导出整个内部实现集合。`runtime/fileops/__init__.py` 和 `runtime/session/__init__.py` 当前导出偏宽，迁移时应收窄。

## 10. 风险与处理

| 风险 | 影响 | 处理 |
| --- | --- | --- |
| import 路径大面积变化 | 容易漏改 product/ztest | 每 phase 用 `rg 'runtime.tools.dependency|runtime.context.skills|runtime.context.code_map|runtime.fileops.artifact_|contracts.fileops.BlobRef'` 验证 |
| session replay 行为变化 | resume/history/hunk 可能回归 | Phase 4 必跑 session + artifact + hunk 相关测试 |
| interactive checkpoint 兼容 | browser/kernel/terminal/canvas 恢复可能回归 | Phase 2 必跑 `ztest/runtime/test_*runtime*` 与 handoff/canvas/kernel/terminal 测试 |
| `RoleComponents` 装配过厚 | 迁移后仍像大泥球 | component manifest 按领域放置，RoleComponents 只汇总 specs |
| `__init__.py` 导出过宽 | 新包继续变成杂货铺 | public API 白名单写入测试或 review checklist |

## 11. 建议优先级

最高优先级：

1. Phase 0：边界测试。
2. Phase 1：contracts 反向 import、`tools -> agent`、`context -> agent`、AgentCatalog 拆分。
3. Phase 2：`runtime.tools.dependency` 迁入 `runtime.interactive`。

第二优先级：

4. Phase 3：Skills 上移 product，Code Map 拆成 runtime execution + product policy/adapter。
5. Phase 4：artifact 域统一，fileops 瘦身。

第三优先级：

6. Phase 5：agent 装配层瘦身。
7. Phase 6：通用 failover/admission 抽取。

## 12. 最终验收标准

结构验收：

- `runtime/tools` 不包含 browser/kernel/terminal/canvas/device/video 实现。
- `runtime/context` 不包含 skills/code_map 实现。
- `product/skills` 拥有 Skill definition/discovery/audit/pool/injector；runtime 只消费 skill ports。
- `runtime/code_map` 只拥有 indexing/store/extraction execution；product 控制路径策略、语言启用策略和 turn prompt adapter。
- `runtime/fileops` 不包含通用 artifact CAS/store/lifecycle/physical GC；只保留 FileOps roots/pins、scoped mutation artifacts 和 reachability provider。
- `runtime/agent` 不包含 LSP、output engine、ContextProvider contract/DTO；允许持有 Role 的 concrete ContextProvider composition adapter 留在 `runtime/agent/components/context_provider.py`，且 lower runtime services 不得 import 它。
- `runtime/lsp` 是通用 LSP capability 的实现包；product 只控制启用策略和 UI/ACP 暴露。
- `product/agents` 拥有 agent declaration/discovery；`orchestration/environment` 拥有 spawn catalog/factory runtime view。
- `contracts` 不 import runtime。

依赖验收：

- 架构测试禁止关键反向 import，并无临时白名单。
- lower runtime services 不 import `runtime.agent`。
- `runtime.artifacts` 不 import `runtime.fileops`。
- `runtime.artifacts` public API 不使用 `contracts.fileops.BlobRef`。
- `runtime.context` 不 import product skills/code_map concrete implementation。
- `runtime.service_gateway` 不依赖 `runtime.models.failover.*`。
- `runtime.code_map` 不 import `runtime.paths`，不读取 `CONFIG_ROOT`，不执行 `.mote` discovery。
- `product/cli/consumers/acp` 不引用 `runtime.lsp` 私有实现。

指标验收：

- 非平凡 SCC 数量不增加。
- 位于非平凡 SCC 中的节点总数下降。
- 最大非平凡 SCC size 下降。
- 非法边数量按 phase baseline 归零。
- 顶层 public API re-export 数量下降或有明确保留理由。
- 跨包 fan-out top N 文件减少，或每个高 fan-out 文件被标注为 composition root。

行为验收：

```bash
python -B -m pytest ztest/architecture ztest/runtime ztest/fileops ztest/session ztest/roles ztest/context ztest/turn_context ztest/skills ztest/executor ztest/router ztest/product ztest/flow ztest/tasks ztest/environment ztest/lsp -q --tb=short
```

如果全量测试受本机已知 pytest 问题影响，至少按 phase 运行对应子集，并保留失败用例的原因说明。

## 13. 评审关注点

评审时建议重点看四件事：

1. `runtime.interactive` 是否成为真正的运行时后端包，而不是从 `tools/dependency` 换个名字。
2. `context` 是否彻底脱离 Agent control、ToolExecutor、SkillManager 的具体实现。
3. artifact repository/lifecycle 是否只有一个权威归属。
4. Role 装配层是否只做 wiring，不再让领域服务为了拿配置或状态反向 import Role。
