# Product 分包长期治理需求

本文定义 `mote.product` 的目标分包架构与实施验收标准，供需求评审、架构评审和后续拆包实施共同使用。

本文只治理 Product 层内部边界，不重新评审 `contracts <- kernel <- runtime <- orchestration <- product` 的五层划分，也不重新讨论已经完成审核的 Runtime 迁移。

目标不是让目录在短期内看起来整齐，而是建立一套未来十年仍能持续演进的所有权模型：新增模型供应商、工具、Agent 类型、终端界面、远程协议和部署形态时，都有唯一归属，不需要通过循环依赖、全局注册表、兼容转发或新的聚合杂物包完成接入。

---

## 1. 治理目标

### 1.1 总目标

Product 分包治理完成后必须满足：

1. 每项产品能力只有一个代码 owner、一个规范 import path 和一个组合入口。
2. Product 内部依赖是有向无环图，目录关系可以直接解释依赖方向。
3. 核心产品能力、组合装配、展示模型和交付 Interface 彼此分离。
4. 新增一种 Interface 不修改 Agent、Toolset、外部能力或核心 Container。
5. 新增一种外部能力不要求 Tool 实现反向提供 registry 或 provider catalog。
6. Agent 构造不自行发现配置文件，不隐式选择进程级默认值。
7. 包边界由 AST 架构测试强制执行，不依赖约定、注释或人工 `rg`。
8. 不保留内部 API 的 forwarding module、兼容 re-export、双路径实现或长期迁移层。
9. 分包按“变化原因和生命周期”进行，不以文件数量、行数或命名对称性作为首要依据。
10. 目标架构允许分阶段到达，但每个阶段都必须使依赖图更简单，不能引入临时反向依赖。
11. 一级目录必须是一张可读的产品能力地图；不了解实现的人仅浏览目录，就能判断产品提供哪些功能和交付形态。
12. 优先按业务能力纵向内聚；只有被多个能力真实复用、且拥有独立生命周期的机制才允许提升为横向包。

### 1.2 “零负债”的工程定义

本文中的零负债不是零代码、零复杂度或永不调整目录，而是以下可验证状态：

- 不存在已知的包级循环依赖。
- 不存在两个包共同拥有同一 registry、配置 discovery 或生命周期资源。
- 不存在以 `utils`、`common`、`shared`、`misc`、`helpers` 命名的无领域 owner 聚合包。
- 不存在“暂时留着”的旧 import path。
- 不存在只能靠 import side effect 才能完成的隐式全局装配。
- 不存在核心能力对具体 CLI、TUI、ACP、AG-UI 或未来 Interface 的依赖。
- 所有例外均有明确到期条件；没有永久 architecture baseline 白名单。
- 每个目标包都能用一句话说明 owner、输入、输出和禁止承担的职责。
- 一项普通功能需求原则上只修改一个能力包和至多一个薄适配入口；跨三个以上一级包的变更必须在评审中解释。

### 1.3 非目标

本治理不做以下事情：

- 不修改五层架构及其依赖方向。
- 不因为拆包顺手重写业务算法、协议或 UI。
- 不追求一级包数量最少。
- 不把 Product 能力下沉到 Runtime 以消除 Product 内部设计问题。
- 不为未知的第三方兼容需求预留抽象层。
- 不建立通用插件框架；现有 immutable catalog 足以覆盖的扩展继续使用 catalog。
- 不按单文件行数机械拆分稳定内聚实现。

---

## 2. 当前基线与主要问题

当前 `product/` 约 38K LOC。主要体量集中在：

| 包 | 文件数 | 约 LOC | 当前职责 |
| --- | ---: | ---: | --- |
| `product.cli` | 107 | 18,327 | CLI、Terminal、Textual、ACP、AG-UI、ViewEvent、渲染、Serving、输入端口 |
| `product.toolsets` | 31 | 7,387 | 内置工具、Toolset catalog、部分外部服务 registry |
| `product.integrations` | 14 | 3,605 | 模型 provider、外部服务 adapter、LSP 产品装配 |
| `product.routing` | 22 | 3,157 | Squilla 策略、模型和推理运行时 |
| `product.config` | 13 | 1,379 | 配置加载、层叠、诊断、激活 |

静态 import 图存在两个包级强连通分量：

```text
application <-> container <-> cli

cli.consumers <-> cli.io <-> cli.serving
```

当前关键问题按优先级排序：

### P0：核心组合根与交付 Interface 循环依赖

`ProductContainer` 持有 CLI 的 `CommandRegistry` 和 `ConsumerRegistry`，而 CLI bootstrap 又依赖 `Application` 和 `ProductContainer`。核心产品容器因此知道具体交付渠道，Interface 也无法作为最外层 adapter 独立替换。

### P0：`product.cli` 的命名和职责已不一致

ACP server、AG-UI server、多会话 serving、通用 ViewEvent、渲染状态机都不是命令行接口。继续以 `cli` 作为聚合根会让每种新交付形态都进入同一个包。

### P1：外部能力被隐藏在泛化的 Integration 包中

Models、Media、Web Search 和 LSP 是用户能够识别的不同产品能力，但当前共同放在泛化的 `integrations` 下。同时 Media provider registry 和 Web Search backend registry 位于 `toolsets/builtin`，导致 `integrations` 反向依赖工具实现。浏览一级目录既看不出产品具备哪些外部能力，也无法判断各能力的 owner。

### P1：`CodingAgentFactory` 同时承担装配和 discovery

Agent factory 当前读取 hooks、MCP、配置 source，创建 Skills、Code Map、LSP、Toolsets 和后台任务能力。它既是 Agent builder，又是第二个 composition root，还承担文件系统 discovery。

### P2：配置核心与产品配置 adapter 分成相邻一级包

`config_sources` 仅包含 hooks、MCP 和 permissions 三种约定文件 adapter，并依赖 `config.layered_json`。两者属于同一个配置领域，不需要两个顶层 owner。

### P2：若干大文件包含多个独立变化轴

`toolsets/builtin/read.py` 同时处理文本、Notebook、图片、PDF 和视频；CLI projector 和 renderer 也按全部事件类型集中增长。应在包边界稳定后按领域切片拆分，而不是先做机械文件拆分。

---

## 3. 目标架构

### 3.1 能力优先，而非技术分层优先

一级包优先回答“产品有什么功能”，二级包才回答“该功能如何实现”。例如：

```text
media_generation/
  providers/       # 外部供应商
  registry.py      # registry 数据结构
  catalog.py       # 内置 provider definition
  service.py       # hosted service adapter
  policy.py        # 产品选择和错误分类

toolsets/builtin/generate_media/
  tool.py          # 模型可见的薄 Tool adapter
```

不能反过来建立顶层 `providers/`、`registries/`、`services/`，再把所有业务能力横向切碎。判断是否需要独立一级包的标准是：

1. 用户或产品需求能否独立描述该能力。
2. 是否有独立配置、外部依赖、生命周期或扩展目录。
3. 是否能够被不同 Tool、Agent 或 Interface 复用。
4. 移除该能力时，是否可以整体删除一个目录而不拆解其他能力包。

### 3.2 Product 内部依赖方向

目标依赖图：

```text
foundation: config / paths / i18n
       |                         |
       v                         v
capabilities                 presentation
  models / media_generation    events / projection
  web_search / lsp             state / rich_rendering
  routing / skills                   |
  code_map                           |
       |                             |
       v                             |
model-facing product                 |
  toolsets / agents                  |
       |                             |
       v                             |
composition                          |
  application / container            |
  bootstrap / lifecycle              |
       |                             |
       v                             |
application use cases                |
  interaction / session_hosting      |
       |                             |
       +--------------+--------------+
                      v
interfaces: terminal / textual / acp / agui
                      |
                      v
entrypoints: cli / cron
```

图中的层级是依赖约束，不要求为每个层级创建同名目录。禁止反向 import，也禁止同层包通过双向 import 形成环。

### 3.3 目标目录

建议目标结构：

```text
product/
  paths/
    __init__.py
    model.py
    defaults.py
    discovery.py

  composition/
    application.py
    container.py
    bootstrap.py
    lifecycle.py

  config/
    __init__.py
    bootstrap.py
    diagnostics.py
    env.py
    layered_json.py
    layers.py
    loader.py
    overrides.py
    report.py
    schema.py
    secrets.py
    sources.py
    watcher.py
    adapters/
      hooks.py
      mcp.py
      permissions.py

  models/
    providers/
      openai.py
      anthropic.py
      deepseek.py
    registry.py
    catalog.py
    endpoint.py
    gateway.py

  media_generation/
    providers/
    registry.py
    catalog.py
    service.py
    policy.py

  web_search/
    backends/
    registry.py
    catalog.py
    service.py

  lsp/
    factory.py

  routing/
  skills/
  code_map/
  toolsets/
  agents/

  interaction/
    turn.py
    driver.py
    commands/
      core.py
      builtin.py
      catalog.py
    human_channel.py
    approvals.py

  session_hosting/
    registry.py
    connection.py
    prompt_broker.py
    routing.py
    delivery.py

  presentation/
    events/
    projection/
    state/
    rich_rendering/

  interfaces/
    terminal/
    textual/
    acp/
    agui/

  entrypoints/
    cli/
    cron/
```

该结构是目标职责图，不要求在一个提交中整体搬迁。实施可以先建立 canonical owner，再逐个迁移调用方。

### 3.4 内聚性验收场景

目标结构必须通过“需求落点”检查。以下典型需求的主要改动范围应当稳定：

| 需求 | 主要 owner | 允许的伴随改动 | 不应修改 |
| --- | --- | --- | --- |
| 新增模型供应商 | `models/providers` | `models/catalog.py` | Agent、Toolset、Interface |
| 新增图片生成供应商 | `media_generation/providers` | `media_generation/catalog.py` | Web Search、Presentation、Agent |
| 修改 GenerateMedia 参数 | `toolsets/builtin/generate_media` | 必要时扩展 `media_generation` 的窄请求 DTO | Models、Interface |
| 新增搜索 backend | `web_search/backends` | `web_search/catalog.py` | Tool schema、Agent、Presentation |
| 新增 Agent 声明格式 | `agents` | `config.adapters` 仅在增加新配置源时 | Toolset、Interface |
| 新增一种 ViewEvent | `presentation` | Interface 仅在需要专属交互时 | Agent、Container、外部能力 |
| 新增交付协议 | `interfaces/<name>` | 复用 Presentation；必要时增加公开资产 | 核心 Container、其他 Interface |
| 新增 CLI 启动模式 | `entrypoints/cli` | 选择并装配已有 Interface | Interface 内部实现、Agent |
| 修改单会话 turn/command 语义 | `interaction` | 对应 Interface 的薄接线 | Session hosting、外部能力 |
| 修改远程会话驻留/路由 | `session_hosting` | ACP/AG-UI 接线 | Terminal、Textual、Agent |
| 新增内置 Tool | `toolsets/builtin` | 对应能力包仅在确有新业务能力时 | Presentation、无关能力包 |

评审规则：

- 一项需求修改一个一级能力包：理想状态。
- 修改“一个能力包 + 一个薄 Tool/Interface adapter”：正常状态。
- 修改三个及以上一级包：必须说明是跨能力业务需求，而不是分包错误。
- 同一组文件在连续三个独立需求中总是共同修改：应重新评估是否属于同一 bounded context。
- 一个包内部存在两组长期从不共同修改的文件：应重新评估是否需要拆成两个功能包。

---

## 4. 包级所有权

### 4.1 `product.config`

Owner：Product 配置文件发现、层叠、环境覆盖、诊断、监听和激活。

允许：

- 读取 Product 约定的 JSON/YAML/env 配置源。
- 构造 Contracts 中定义的配置 DTO。
- 实现 hooks、MCP、permissions 的约定文件 adapter。

禁止：

- 构造 Agent、Tool、Gateway 或 Interface。
- import `agents`、`toolsets`、`presentation`、`interfaces` 或 composition root。
- 承担 Runtime 配置执行语义。

处置：删除顶层 `product.config_sources`，内容迁入 `product.config.adapters`，不保留转发包。

### 4.2 `product.paths`

Owner：产品默认目录、`.mote` discovery 和 immutable `RuntimePaths` 的构造。

要求：

- 默认路径只在这里求值。
- capability 接收所需的具体 Path，不接收整个路径上帝对象。
- `RuntimePaths` 在本轮保持现名；更名涉及 API 语义，必须单独评审，不与分包迁移绑定。

当前 `paths.py` 已经存在多个独立变化轴，不属于为空间对称而预建包：

- `model.py`：immutable `RuntimePaths` DTO。
- `defaults.py`：用户根、workspace 根、package data root 等默认值构造。
- `discovery.py`：`.mote` 目录/文件向上发现、Git root 边界和层叠顺序。
- Product Paths 不拥有 session 持久化格式。当前 `ROLLOUT_FILENAME`、`.agent_sessions`、default session bucket 等名称归 `runtime.session` 的 `SessionLayout`/session log owner；Product 中的重复定义直接删除。Product 只把默认 workspace/session root 作为构造参数注入 Runtime，Runtime 不 import Product Paths。

`product.paths.__init__` 只导出稳定 facade。当前 `load_mote_json_section` 是配置解析而非路径语义，迁入 `product.config.layered_json`，不得进入新 Paths 包。

### 4.3 `product.models`

Owner：模型供应商接入、provider catalog、endpoint 解析和 Product 模型 Gateway 装配。

允许：

- OpenAI、Anthropic、DeepSeek 等具体 provider adapter。
- Application-owned provider registry。
- 模型 endpoint 和 failover snapshot 的产品装配。

禁止：

- import `product.toolsets`。
- 承担 Media、Web Search 或 LSP 接入。
- 定义模型可见 Tool schema。
- 构造完整 Application。

### 4.4 `product.media_generation`

Owner：图片、音频、音乐和视频生成能力，包括供应商目录、服务 adapter、产品策略和错误分类。

要求：

- Media generation provider registry、creator 和 endpoint resolver 全部位于本包。
- `GenerateMedia` Tool 只保留参数解析、ToolResult 映射和调用 Media capability 的薄适配。
- 生成错误不再放在泛化的 `product.errors`；错误分类与生成能力共同演进，应归 `product.media_generation.errors`。
- 本包不 import `product.toolsets`、Agent、Presentation 或 Interface。

本包明确不拥有图片/PDF/视频读取、媒体展示、编码或传输。若未来这些能力增长，应使用 `document_reading`、`media_viewing` 等真实功能名称，而不是扩张 `media_generation`。

### 4.5 `product.web_search`

Owner：托管 Web Search 能力，包括 backend catalog、endpoint resolver、配置到运行快照的转换和错误映射。

要求：

- Search backend registry 从 Toolset 移入本包。
- `WebSearch` Tool 是调用该能力的模型适配器，不拥有 backend lifecycle。
- 浏览器自动化属于 `WebBrowser`/interactive 能力，不因为名称相近并入 Web Search。

### 4.6 `product.lsp`

Owner：Coding Agent 产品对 Runtime LSP capability 的默认选择和构造策略。

当前体量较小时仍保留独立功能包，因为 LSP 有独立配置、生命周期、可选依赖和未来扩展空间。禁止将其重新藏入 `agents` 或泛化 `integrations`。

### 4.7 `product.toolsets`

Owner：模型可见工具、Toolset 分组、内置 Tool catalog 和工具级产品策略。

要求：

- 工具执行基础设施、安全、权限、快照和结算继续由 Runtime 强制。
- Tool 可以依赖 Models/Media Generation/Web Search/LSP 等能力的窄 service 或 snapshot；能力包不能反向依赖 Tool。
- Tool discovery 结果必须冻结为 Application-owned catalog。
- import side effect 仅可作为 catalog 构建的封装实现，不可成为进程级可变全局注册机制。

### 4.8 `product.skills`

Owner：Skill 定义、Markdown 解析、pool、选择、注入和 Product factory。

要求：

- source roots 由 composition/paths 注入。
- 不读取 Agent 私有状态。
- 不依赖具体 Interface 或 presentation。

### 4.9 `product.code_map`

Owner：Code Map 的产品启用策略、持久化路径策略和 per-turn prompt adapter。

要求：

- execution mechanism 通过 Runtime capability 使用。
- `CodeMapContextSource` 后续按 collection、enrichment、rendering 三个变化轴拆分，但保持一个公开 factory。
- 不依赖具体 Agent 类、Interface 或 CLI。

### 4.10 `product.routing`

Owner：Mote 产品选择的 routing policy、Squilla 模型资产和 Application-owned routing lifecycle。

要求：

- `RoutingModelRuntime` 生命周期由 composition 持有。
- 策略不得读取 CLI 或 Interface 状态。
- ML inference 内部可以继续细分，但不对外泄漏内部 feature/head 类型。

### 4.11 `product.agents`

Owner：Product Agent 类型、Markdown Agent 声明发现、Agent catalog 和最终 Agent builder。

目标拆分：

```text
agents/
  definitions/      # 内置和 Markdown Agent 声明
  discovery.py      # 声明发现，不做完整装配
  catalog.py        # immutable Agent catalog
  factory.py        # 消费已装配 dependencies，构造 Agent
```

`CodingAgentFactory` 必须改为消费已经解析的依赖：

- Toolset factory
- Skill service factory
- Code Map factory
- LSP factory
- Background task factory
- hooks/MCP/config reload provider
- concrete paths

禁止由 Agent factory 自行执行 `.mote` discovery、读取 hooks/MCP 文件或选择进程默认根目录。

### 4.12 `product.composition`

Owner：完整 Product 对象图、Application 生命周期、catalog generation、reload 和 shutdown 装配。

Canonical path 固定为：

```text
product.composition.application
product.composition.container
product.composition.bootstrap
product.composition.lifecycle
```

`mote.product.Application` 可以作为经评审确认的唯一公开 facade；Product 内部一律使用 canonical path，不通过根包反向导入。

`ProductContainer` 只允许持有：

- Agent catalog/factory
- Tool catalog
- Model provider catalog
- Hosted service catalogs
- Routing runtime
- Product paths
- 其他与 Interface 无关的 Application-owned capability

禁止持有：

- `CommandRegistry`
- `ConsumerRegistry`
- Terminal/Textual 对象
- ACP/AG-UI server
- 具体 input/output port
- Interface session registry

`Application` 保持 Runtime Engine 与 ProductContainer 的生命周期组合。Composition 是真实 bounded context，不因禁止“全能 bootstrap”而继续散落为根目录文件；其边界通过依赖门禁限制，而不是通过拒绝建包限制。

### 4.13 `product.presentation`

Owner：把领域事件投影为人机展示语义，并由具体展示 adapter 消费。

数据流与 import 方向必须分别表达，禁止混用箭头语义。

数据流：

```text
domain event -> projection -> view event -> state
                                      |
                                      +-> rich rendering
```

import 依赖：

```text
projection     -> events
state          -> events
rich_rendering -> events
```

说明：

- `events` 是 Product 内部展示事件，不是五层中的 `mote.contracts`；使用具体名称避免第二个含混的 `contracts` 根。
- `ViewEvent` 属于 presentation contract，不属于 CLI。
- Transcript reducer 属于 `state`，不依赖颜色、Rich、Pillow 或具体 UI widget。
- `rich_rendering` 明确承载 Rich/Pillow/Markdown 等可选展示技术，可以直接消费 ViewEvent 或 projection 输出，不强制经过 transcript state。
- ACP/AG-UI 可以直接消费结构化 ViewEvent，不依赖 state 或 rich_rendering。

禁止：

- import `ProductContainer`、`Application` 或 Agent factory。
- 创建 Runtime Engine。
- 读取产品配置文件。
- 启动 server 或处理进程信号。

### 4.14 `product.interaction`

Owner：与交付协议无关的单会话应用用例，包括 turn 仲裁、命令分派、human channel、prompt 和 approval 协调。

来源包括当前 `SessionDriver` 的无 I/O/无 rendering 核心、slash command definitions 和 `PortHumanChannel`。要求：

- `interaction.turn` 是单 turn 执行语义的唯一 owner：发布用户 ViewEvent、`control.send_input`、等待 quiescent、提取并发布 turn 错误。
- `TurnCoordinator`/`TurnRunner` 不拥有持续输入循环、连接生命周期或具体 transport。
- `driver` 只拥有持续输入循环、turn lock、steering 和 scheduler 生命周期，并调用 `interaction.turn`。
- 命令是输入/use-case 语义，归 `interaction.commands`，不进入 Presentation。
- Driver 只依赖窄 input/output/agent-control 接口，不 import Terminal、Textual、ACP 或 AG-UI。
- Presentation 事件作为输出，不由 Interaction 渲染。
- 不拥有多会话驻留、连接路由或网络 delivery。

不预设 `interaction.session` 或泛化 `interaction.prompts`。前者容易与 Session Hosting 混淆；普通 prompt/approval 行为只有形成独立实现 owner 时才增加具体模块。

### 4.15 `product.session_hosting`

Owner：远程、多连接和多会话交付共享的应用能力，包括 resident session registry、connection scope、prompt correlation、inbound routing 和 outbound delivery reliability。

源码依据：当前 `SessionRegistry` 管理跨请求驻留及关闭，`ConnectionScope` 管理每连接/每 turn 绑定，`PromptBroker` 管理异步 back-channel 关联；它们与本地单会话 `SessionDriver` 的生命周期和变化原因不同。

要求：

- 可以依赖 Composition、Interaction 和 Presentation 的窄入口。
- `connection` 只管理连接/请求绑定并调用 `interaction.turn`，不得复制 send-input、quiescent 等待或 turn 错误发布语义。
- 不依赖 ACP 或 AG-UI 具体 wire type。
- Terminal/Textual 不依赖本包。
- 未实现的 `SessionRouter`、`DeliveryManager` 不因已有 stub 自动获得长期 owner；实现前仍需按实际需求验证是否属于本包。

### 4.16 `product.interfaces`

Owner：具体本地界面或远程协议 adapter，不负责选择进程启动模式。

依赖矩阵：

```text
Terminal / Textual -> Interaction + Presentation
ACP / AG-UI        -> Interaction + Session Hosting + Presentation
```

Interface 不要求直接依赖 Composition。若 Interface bootstrap 确实负责对象构造，可以依赖 Composition；否则由 EntryPoint 注入已构造的 Application/use-case 对象。

- `interfaces.terminal`：滚动终端的输入、输出和 Session driver 装配。
- `interfaces.textual`：Textual App、widgets、input/output adapter 和进程生命周期。
- `interfaces.acp`：ACP wire mapper、port、server、registry assets。
- `interfaces.agui`：AG-UI wire mapper、port、server。

Interface 之间禁止互相 import。共享展示语义归 Presentation，单会话应用用例归 Interaction，远程多会话能力归 Session Hosting；不能建立 `interfaces.shared`。

### 4.17 `product.entrypoints`

Owner：解析进程参数、选择 Interface 并执行最终顶层装配。

- `entrypoints.cli` 可以显式依赖 Terminal、Textual、ACP、AG-UI Interface，并根据参数选择其一。
- `entrypoints.cron` 只拥有 cron 命令入口，不吸收 scheduling 业务实现。
- EntryPoint 可以依赖 Interface 和 Composition；Interface 禁止反向依赖 EntryPoint。
- `python -m mote.product.cli` 等旧入口在迁移时直接更新为 canonical entrypoint，不保留 forwarding package。

### 4.18 不保留泛化 `product.integrations` 与 `product.errors`

治理完成后删除这两个顶层包：

- `integrations` 隐藏了 Models、Media Generation、Web Search、LSP 四种独立功能。
- `errors` 只包含 Media Generation 错误，缺乏独立 owner；错误应和产生、分类、处理它的能力共同演进。

未来新增外部能力时直接使用功能名称，例如 `notifications`、`issue_tracker`，不能再次建立泛化 Integration 收纳箱。

---

## 5. 关键架构决策

### 5.1 Command 与 Consumer 分属输入和输出语义

所有权固定为：

```text
Command definition / use case -> interaction.commands
Consumer Protocol             -> presentation
具体 Consumer 实现            -> interfaces/<name>
具体 Consumer catalog         -> 对应 Interface bootstrap
```

Command/Consumer 都不进入 ProductContainer。禁止建立同时枚举 Terminal、Textual、ACP、AG-UI 实现的统一 ConsumerRegistry；EntryPoint 选择 Interface，Interface bootstrap 只构造自己的 Consumer catalog。

### 5.2 Registry 归具体能力 owner

Registry 只实现注册、冻结、查询等数据结构，不枚举内置 provider。内置 provider 列表归能力包的 `catalog.py` 或 `bootstrap.py`。新增 provider 修改 provider adapter 与 catalog definition，不修改 Registry 实现。

`MediaProviderRegistry` 和 `SearchBackendRegistry` 分别归 `product.media_generation`、`product.web_search`。工具只消费这些 catalog，不拥有它们。

### 5.3 Composition 只有一个方向

核心 Composition 构造 Agent execution 所需能力；Interaction/Session Hosting 提供应用用例；Interface 追加具体交互或 transport；EntryPoint 负责最终选择与启动。低层禁止调用高层 factory。

### 5.4 不新增全能 `bootstrap` 包

允许存在：

- `product.composition.bootstrap`：核心 Application 构造。
- `interfaces/<interface>/bootstrap`：一个 Interface 的最终装配。
- capability 内局部 bootstrap：构造该 capability 的 immutable catalog。

禁止创建包含所有初始化逻辑的顶层 `product/bootstrap/` 杂物目录。Composition bootstrap 只能构造核心对象图，Interface 选择留在 EntryPoint。

### 5.5 不以 Protocol 掩盖错误所有权

当两个 Product 包循环依赖时，优先修正 owner 和装配方向。只有确实跨五层边界、或存在多个稳定实现时，才在 `contracts/ports` 定义 Protocol。不能为了保留 Product 内部错误目录而抽象一个无业务意义接口。

---

## 6. 实施阶段

### Phase 0：建立门禁和基线

范围：

1. 增加 Product import graph AST 测试。
2. 记录当前一级、二级包依赖图和强连通分量。
3. 为目标包定义允许依赖集合。
4. 完成 `Application` public API 审计，决定根 facade、版本策略和发布说明。

完成条件：测试能准确捕获新增 Product 包级回边；当前已知环以显式待清理断言表达，不建立可无限增长的 baseline 列表；`Application` API 决策已批准并记录。未满足时不得进入 Phase 1。

### Phase 1：打破核心组合环

范围：

1. 建立 `product.composition`，移动 Application、Container、bootstrap 和 lifecycle owner。
2. 从 `ProductContainer` 移除 CommandRegistry 和 ConsumerRegistry。
3. 当前 CLI/Interface 自行构造各自的 presentation catalog，不新增统一跨 Interface registry。
4. Composition 不再 import `product.cli`。
5. 暂时保留现有 CLI bootstrap 作为调用方，但禁止新增逻辑。

完成条件：`composition/cli` 强连通分量消失；核心 Product import 不加载 CLI 可选依赖；Product 内部不再使用根目录旧 canonical path。

### Phase 2A：抽取纯 Presentation

范围：

1. 移动 ViewEvent、projector、transcript state 和 Rich rendering 到 `presentation`。
2. 建立分叉依赖，确保结构化协议不依赖 transcript 或 Rich。
3. 保持现有 Interface 行为和入口不变，仅更新 import。

完成条件：

- presentation 不 import composition。
- state 不 import rich_rendering。
- ACP/AG-UI 的结构化 mapper 可在不安装 Rich/Textual/Pillow 时导入和测试。

### Phase 2B：抽取 Interaction 与 Session Hosting

范围：

1. 先从 `SessionDriver._run_turn()` 与 `ConnectionScope.run_turn()` 抽取唯一的 `interaction.turn`。
2. 将单会话 driver、commands、human channel、approval 用例移入 `interaction`，两条调用路径都复用 TurnRunner。
3. 将 resident registry、connection scope 和 prompt broker 移入 `session_hosting`。
4. 对尚未实现的 router/delivery stub 单独确认去留，不把设计占位符当成迁移事实。

完成条件：Interaction 不依赖具体 Interface；Session Hosting 不依赖 ACP/AG-UI wire type；本地 Interface 不依赖 Session Hosting；全仓只有 `interaction.turn` 实现 send-input、quiescent 等待和 turn 错误发布序列。

### Phase 2C：逐个迁移本地 Interface

依次迁移 Terminal、Textual。每次只迁移一个 Interface，保留独立 smoke test，并在迁移完成后禁止另一个 Interface import 它。

### Phase 2D：逐个迁移远程 Interface

依次迁移 ACP、AG-UI。每次验证多会话驻留、prompt/approval round-trip、连接关闭和 Application shutdown。

### Phase 2E：建立 EntryPoint 并删除旧 CLI

1. 建立 `entrypoints.cli` 和 `entrypoints.cron`。
2. CLI 显式选择 Terminal/Textual/ACP/AG-UI Interface。
3. 删除 `product.cli`，不保留 forwarding package。
4. 消除原 `consumers/io/serving` 强连通分量的所有旧节点。

完成条件：Interface 之间无依赖；EntryPoint 是唯一进程启动选择层；Terminal、Textual、ACP、AG-UI 核心行为测试全部通过。

### Phase 3：按功能提升外部能力

范围：

1. Models provider 和 gateway 装配移到 `product.models`。
2. Media generation registry/provider/service/error 移到 `product.media_generation`。
3. Search registry/backend/service 移到 `product.web_search`。
4. LSP 产品构造移到 `product.lsp`。
5. 能力包不再 import Toolset，Tool 构造通过 application-owned catalog 注入具体 capability。
6. 删除 `product.integrations` 与 `product.errors`，不保留兼容入口。

完成条件：一级目录可以直接看到 Models、Media Generation、Web Search、LSP；这些能力包对 `product.toolsets` import 为零。

### Phase 4：收窄 Agent composition

范围：

1. 将 config discovery 从 `CodingAgentFactory` 移到 Product bootstrap。
2. 工厂接收 hooks、MCP、reload provider 和 concrete paths。
3. 区分 Agent declaration discovery、catalog 和 instance construction。
4. 确保 CLI 创建、子 Agent 创建和 resume 使用同一 canonical factory。

完成条件：`product.agents` 不 import `product.config` 或 `product.config.adapters`；Agent factory 单测不需要真实用户配置目录。

### Phase 5A：Paths 职责拆分

范围：将 `paths.py` 拆为 model、defaults、discovery；把 JSON 解析迁到 Config owner；删除 Product 重复的 Runtime session layout 常量；建立无副作用的稳定 facade。

完成条件：默认值和 discovery 可以独立测试；Paths 不解析配置内容；调用方不依赖内部模块；`product.paths` 不拥有 Runtime 持久化格式常量，Runtime 不 import Product Paths。

### Phase 5B：配置包收束

范围：将 `config_sources` 迁入 `config.adapters`，更新调用方并直接删除旧包。

完成条件：只有 `product.config` 拥有 Product 配置 discovery 和约定文件解析。

### Phase 6：内部大文件治理

仅在包图稳定后实施：

- `Read`：按文本/Notebook/media adapter 拆分，保留一个 Tool 类入口。
- `ViewProjector`：按事件族拆 projection handler。
- `CodeMapContextSource`：拆 collection、enrichment、rendering。
- render builders：按 message/tool/activity/session/usage 拆分。

完成条件不是行数下降，而是每个模块只有一个变化原因，公开入口数量不增加。

---

## 7. 架构门禁

必须新增 `ztest/architecture/test_product_dependencies.py`，至少覆盖：

### 7.1 禁止包级环

AST 扫描绝对 import 和相对 import，先把相对路径解析为完整模块名，再以两种粒度执行 SCC 检测：

1. 一级 bounded context，例如 Composition、Interaction、Session Hosting、Presentation、各能力包和 Interface。
2. 二级 package，捕获 Presentation、单个 Interface 或能力包内部的循环。

目标图中任何包含多个节点的 SCC 直接失败。迁移期不使用普通 baseline 放行任意边；当前图单独精确断言只存在两个已知 SCC：

```text
application / container / cli
cli.consumers / cli.io / cli.serving
```

每消除一个 SCC 就在同一 Slice 删除对应临时断言，禁止扩充成员或添加第三个例外。

### 7.2 固定禁止边

```text
config            -X-> agents, toolsets, composition, interaction, session_hosting, presentation, interfaces, entrypoints
models            -X-> toolsets, agents, composition, interaction, session_hosting, presentation, interfaces, entrypoints
media_generation  -X-> toolsets, agents, composition, interaction, session_hosting, presentation, interfaces, entrypoints
web_search        -X-> toolsets, agents, composition, interaction, session_hosting, presentation, interfaces, entrypoints
lsp               -X-> toolsets, agents, composition, interaction, session_hosting, presentation, interfaces, entrypoints
routing           -X-> agents, composition, interaction, session_hosting, presentation, interfaces, entrypoints
skills            -X-> agents, composition, interaction, session_hosting, presentation, interfaces, entrypoints
code_map          -X-> agents, composition, interaction, session_hosting, presentation, interfaces, entrypoints
toolsets          -X-> agents, composition, interaction, session_hosting, presentation, interfaces, entrypoints
agents            -X-> config, composition, interaction, session_hosting, presentation, interfaces, entrypoints
composition       -X-> interaction, session_hosting, presentation, interfaces, entrypoints
presentation      -X-> agents, composition, interaction, session_hosting, interfaces, entrypoints
interaction       -X-> session_hosting, interfaces, entrypoints
session_hosting   -X-> interfaces, entrypoints
interfaces/*      -X-> interfaces/*, entrypoints  # 不同 Interface 之间
```

EntryPoint 位于最外层，可以依赖 Composition、Interaction、Session Hosting、Presentation 和任一 Interface。

Presentation 内部额外约束：`state` 和 `rich_rendering` 都只依赖 `events` 等窄展示契约，互不依赖；结构化 Interface 不 import `state` 或 `rich_rendering`。Session Hosting 可以依赖 Interaction，反向依赖禁止。

### 7.3 禁止旧路径

迁移完成后断言以下目录不存在：

```text
product/cli
product/config_sources
product/integrations
product/errors
```

并扫描所有 Python import，确保没有旧路径引用、re-export 或字符串动态 import。

动态 import 规则：

- AST 识别 `importlib.import_module("literal")` 和 `__import__("literal")`，将静态字符串目标加入依赖图。
- 非静态字符串的 Product 动态 import 默认禁止。
- Tool discovery 等确有必要的动态导入按“调用点 + 目标 package 范围”精确允许，并用运行测试证明只能加载该范围；不得用模块前缀 baseline 泛化放行。

### 7.4 构造纯度与模块副作用

核心包 import 必须：

- 不访问用户 home。
- 不启动线程、task、server 或 watcher。
- 不创建进程级 mutable registry。
- 不要求 Rich、Textual、Pillow、ACP 或 AG-UI 可选依赖存在。

AST 只能识别明显的模块级调用，不能证明运行时无副作用。因此门禁分两部分：

1. AST 禁止已知副作用 API 在模块顶层调用。
2. 隔离子进程 import smoke test 观测线程、async task、server socket、用户目录写入和全局 catalog 变化。

### 7.5 Public surface

每个目标包 `__init__.py` 只导出稳定 facade。内部类型从具体模块导入，禁止通过多层 `__init__` 级联造成 eager import 和循环风险。

### 7.6 公共类型所有权

跨两个以上 Product 能力包使用的 DTO 仍必须有语义 owner：

- 由生产方定义且表达生产方事实的 DTO，归生产方。
- 由消费方解释的请求/策略 DTO，归消费方。
- 真正跨五层的稳定契约才进入 `mote.contracts`。

禁止创建 `product.schemas`、`product.types`、`product.models_common` 等无领域 owner 的类型收纳包。架构评审必须检查新增跨包 DTO 的 owner 和 import 方向；同一 DTO 不得在多个能力包复制定义。

---

## 8. 测试与验收

每个 Phase 必须满足：

1. 对应 AST 架构测试通过。
2. 被移动领域的单元测试同步迁移到其 owner 对应的 `ztest` 目录。
3. CLI、Textual、ACP、AG-UI 至少各有一条 composition smoke test。
4. 核心 `import mote.product` 不加载任何 Interface 或 UI 可选依赖。
5. `ProductContainer.standard()` 构造不创建 presentation/Interface 对象。
6. Catalog 构造具有 Application 隔离性；两个 Application 不共享可变注册状态。
7. Plugin catalog generation 保持 immutable snapshot 语义，运行中 Session 不被新 generation 改写。
8. 没有新增本地 import；除测试外所有 import 保持模块顶部。
9. Registry 单测只验证数据结构；内置 provider 集合由独立 catalog/bootstrap 测试验证。
10. 跨包 DTO 的语义 owner、生产方和消费方在测试或模块文档中可核对。
11. Turn contract test 对持续输入 Driver 与 ConnectionScope 运行同一组 send-input/quiescent/error 场景，证明两者调用同一个 TurnRunner。
12. Consumer ownership test 证明导入一个 Interface 不构造或导入其他 Interface 的 Consumer catalog。
13. Runtime session layout 测试只从 `runtime.session` 取得持久化格式名称；全仓禁止 Runtime import `mote.product.paths`。

最终量化验收：

| 指标 | 目标 |
| --- | ---: |
| Product 一级 bounded-context SCC | 0 |
| Product 二级 package SCC | 0 |
| Composition 到 Interaction/Presentation/Interface/EntryPoint 的 import | 0 |
| Models/Media Generation/Web Search/LSP 到 `toolsets` 的 import | 0 |
| `agents` 到 Product 配置 discovery 的 import | 0 |
| Interface 之间的 import | 0 |
| Interface 到 EntryPoint 的 import | 0 |
| Turn 执行语义实现 owner | 1 (`interaction.turn`) |
| 统一跨 Interface ConsumerRegistry | 0 |
| Product Paths 中 Runtime 持久化格式常量 | 0 |
| Runtime 到 Product Paths 的 import | 0 |
| 旧路径兼容模块 | 0 |
| 进程级 mutable Product registry | 0 |
| 未说明 owner 的顶层包 | 0 |

---

## 9. 迁移纪律

1. 文件移动后直接更新所有调用方，不保留 compatibility re-export。
2. 不在一个 Slice 中混入行为重写和无关格式化。
3. 每个提交只改变一个 owner 边界，并包含对应架构测试。
4. 遇到循环依赖不得使用局部 import 绕过。
5. 新包名称必须表达领域或交付形态，不能表达“暂存”“共享”或实现偶然性。
6. 删除包前必须审计持久化数据是否保存 Python module-qualified path；存在时先设计稳定 type ID 迁移。
7. 可选依赖只能由其 capability adapter/Interface 顶层 `try/except ImportError` 处理，核心 Product import 不得触发。
8. 不通过 `sys.modules`、动态 import 字符串或 TYPE_CHECKING 掩盖运行时错误依赖。

---

## 10. 需求评审决策项

需求评审必须明确批准或否决以下事项：

1. 是否批准将 `product.cli` 拆为 Presentation、Interaction、Session Hosting、Interfaces 与 EntryPoints，并最终删除原包。
2. 是否批准 CommandRegistry/ConsumerRegistry 不再属于 ProductContainer。
3. 是否批准删除泛化 `integrations`，将 Models、Media Generation、Web Search、LSP 提升为一级功能包。
4. 是否批准 Agent factory 不再自行读取 hooks、MCP 和配置 source。
5. 是否批准 `config_sources` 并入 `config.adapters`。
6. `Application` 是否属于对外承诺 API；若是，拆包前需要何种版本与发布说明。
7. ACP/AG-UI wire mapper 是否完全归各自 Interface；只有出现非 UI 复用方时才另行评审协议发布包。
8. 是否接受“不保留内部兼容 import path”作为整个治理的统一迁移政策。

`RuntimePaths` 更名不属于本次分包治理，保持现名，不与目录迁移绑定；如需更名必须单独提交 API 评审。

评审未决项不得靠实施者自行添加临时兼容层解决。

---

## 11. 完成定义

当且仅当以下条件全部满足，本治理才算完成：

- 目标目录和依赖方向落地。
- 两个当前强连通分量均消失。
- 所有现有一级包都有唯一 disposition。
- 核心 composition、presentation 和 Interface 可以独立测试与替换。
- AST 架构门禁覆盖目标边界并进入常规测试。
- 旧目录、旧 import、迁移 alias 和临时 baseline 全部删除。
- 文档中的目标结构与实际源码一致。
- 同步更新 `zdocs/ARCHITECTURE.md`，删除其中已经失效的 `common/*`、旧 roles/flow/executor 结构，保证代码、治理文档和总架构文档使用同一套术语。
- 新增一种 Tool、外部能力、Agent 或 Interface 均有不修改无关 owner 的标准接入路径。

最终标准不是“完成一次重构”，而是 Product 层从此具备稳定的所有权语言和自动化约束，使未来功能增长只增加领域能力，不重新积累结构性负债。
