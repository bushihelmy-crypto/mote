# Mote Python 包治理总策略

- 状态：Draft v11；Phase -1A、-1B0、-1B1 可实现并隔离验证，-1B2～-1D 仅按 Control-plane MVP 顺序实现且 G6 硬关闭，-1E～-1G 仅可设计 schema/manifest/fixture；真实试点、Production cutover 与 Release Closure 尚未授权
- 范围：整个可安装、可测试、可构建和可发布的 Mote Python 项目，不限于五层源码目录
- 性质：包职责、所有权、依赖方向、迁移纪律和验收门禁
- 非目标：语言迁移、进程拆分、分布式部署实现、业务语义重写

本文只治理当前 Python 包和发行结构。它不引入 Go、Rust、RPC、Sidecar、远程 Worker 或第二套运行路径，也不以未来可能的部署方式为理由提前制造抽象。

治理 inventory 至少覆盖：

```text
source_roots
root_public_modules
build_metadata
package_data
published_docs
configuration_templates
license_and_notice_assets
tests_and_governance_tools
development_only_files
excluded_with_reason
```

根 `__init__.py`、`agent.py`、`engine.py`、`messages.py`、`model.py`、`output.py`、`tools.py`，以及 `pyproject.toml`、`setup.py`、`MANIFEST.in`、配置模板、README/CHANGELOG、许可证和第三方 NOTICE 都属于治理表面。排除项必须显式、可审计，扫描器不得因文件不在五层目录中而静默忽略。

### 规范性权威声明

本文件是当前纯 Python 包治理的规范性需求。[`TEN_YEAR_ZERO_DEBT_ARCHITECTURE.md`](./TEN_YEAR_ZERO_DEBT_ARCHITECTURE.md) 仅是非规范性远景，不构成当前新增 Port、DTO、进程边界、远程执行或语言迁移的需求证据。任何仅为远景部署而增加的抽象必须另有已批准的当前需求；发生冲突时，以本文件为准。

当前事实与目标规范的权威顺序为：

1. 本目录范围内的开发约定约束所有修改；
2. 源码与可独立执行、当前通过的架构测试描述当前事实；
3. 已通过局部 digest 验证且未失效的人工 decision 描述已批准裁决；
4. 本文件描述包治理需求和目标约束；
5. ADR 描述其明确范围内已接受且尚未 supersede 的决策；
6. `ARCHITECTURE.md` 描述已落地结构，必须随切片更新；
7. 非规范性远景不得覆盖当前 Python 治理约束。

若文档与源码事实不一致，先停止相关 cutover、登记 drift 并修正文档或决策；不得选择性引用冲突文档为迁移背书。

### 当前自动化可信度声明

现有治理产物和工具目前只能作为候选事实来源，不能批准大规模迁移。以下信号明确无效，修复前不得用于 gate：

- 硬编码的 coverage 100%；
- 仅因文件存在或 baseline ID 相同而宣称 inventory 完整；
- 空的 identity/event/error manifest；
- 由仓内 production consumer 推导的 public API；
- 由 canonical import 哈希生成的所谓 stable symbol ID；
- 只绑定全工作树 baseline、未绑定局部 source/closure digest 的 decision；
- 空 migration DAG 条件下的 Phase verified；
- 会在收集期导入完整 `mote`、Product 或可选依赖的静态架构测试结果。

当前只允许实现并隔离验证 Phase -1A、-1B0、-1B1；Phase -1B2～-1D 必须按第 8.52 节 MVP 顺序实现且 G6 硬关闭；Phase -1E～-1G 只可设计 schema/manifest/fixture。真实试点必须等 journal integrity、merged-unverified、原子 facts generation、完整 provenance、封闭 Gate applicability、三状态终态和 repair DAG 闭环获得机器验证；禁止以当前 governance 产物批准真实试点、Production cutover 或 Release Closure。

### 执行模型声明

五层是长期 owner Track，不是必须依次完成的全局迁移 Phase。跨层移动会同时涉及 source owner、target owner、consumer、composition 和 cleanup；实际执行单位是全局 Cutover DAG 中的节点。

```text
Track C  Contracts
Track K  Kernel
Track R  Runtime
Track O  Orchestration
Track P  Product
Track X  Internal/Release Closure
```

治理波次只用于风险、资源和发布规划，不替代节点级 `depends_on`，也不能要求某一整层全部结束后才允许另一层开始。

---

## 1. 治理目标

本轮治理的目标是建立一张长期稳定、可以由代码和测试验证的 Python 能力地图：开发者仅通过 import path 和包名，就能判断一段代码的领域 owner、所属层、允许依赖的方向，以及新增代码应该放在哪里。

治理完成后必须满足：

1. 依赖严格遵循：

   ```text
   contracts <- kernel <- runtime <- orchestration <- product
   ```

2. 每项业务能力、状态真相、生命周期资源、注册表和队列只有一个 owner。
3. 包名表达领域能力，不使用边界过宽或无 owner 的容器概念。
4. `Role` 保持普通 Python 基类体系中的组合式编排器，不是 Pydantic，也不吸收配置模型或可恢复运行状态；是否改为严格 `abc.ABC` 不属于包治理。
5. Kernel 只拥有单 Agent、模型无关的执行语义。
6. Runtime 只拥有一个 Agent 或一次执行所需的可靠运行机制。
7. Orchestration 只拥有多 Agent、后台任务、工作流和自动化协调。
8. Product 只拥有具体产品能力、集成、默认策略和 composition root。
9. Contracts 只保存真正跨边界共享的稳定语义，不成为通用模型仓库。
10. Internal/private 路径在迁移切片中原子删除；已发布 public API 按批准的版本兼容策略迁移，并在 removal release 原子删除。
11. 每个阶段都能独立测试、独立验收，并使依赖图和 owner 关系单调变清晰。
12. 包边界由 AST/manifest 测试强制执行，不依赖文档记忆或人工搜索。

### 1.1 “长期零负债”的可验收定义

这里的零负债不是目录永远不变，也不是代码零复杂度，而是：

- 没有已知包级循环依赖；
- 没有反向 import；
- 没有两个包共同拥有同一状态或生命周期；
- 没有 `common`、`shared`、`utils`、`misc`、`helpers` 等无领域 owner 包；
- 没有通过局部 import、未登记动态 import、滥用 `TYPE_CHECKING` 或字符串反射隐藏反向分层、运行时依赖或概念循环；
- 没有跨边界访问私有字段或依赖具体实现对象；
- 没有只依靠 import side effect 完成的隐式装配；
- 没有永久旧路径、迁移 facade 或双真相源；
- 每个持久化模型都有稳定身份和唯一 schema owner；
- 每个并发资源都有明确作用域、关闭顺序和取消语义；
- 所有架构例外都精确到 import site，并具有 owner、原因、到期阶段和删除测试。

---

## 2. 代码原则

以下原则适用于本治理及治理期间的所有生产代码修改。

### 2.1 分层优先于复用便利

代码不能仅因“多个地方会用”而下沉。下沉必须满足目标层的业务语义，并保持单向依赖。低层需要高层能力时，在 `contracts/ports/` 定义由低层需求方拥有的窄 Protocol，由高层在装配期注入。

### 2.2 领域 owner 优先于技术类型

数据类、Protocol、错误、事件、配置和 codec 应归其业务领域，不按 `models`、`helpers`、`managers` 等技术类型形成横向仓库。

`contracts/ports/`、`contracts/events/` 和 `contracts/config/` 是受架构约束保留的物理索引，内部仍必须按领域分区，不提供全局业务 facade。

### 2.3 组合优于继承和全局访问

- `Role` 是普通 Python 基类体系中的编排器；当前事实不要求其直接继承 `abc.ABC`；
- 配置进入 `RoleSchema`；
- 可序列化运行状态进入 `RoleState`；
- 组件通过 `RoleComponents` 惰性装配；
- 工具只能获得 `requires=(...)` 声明且由 `Role.tool_capabilities()` 发布的能力；
- 禁止通过 service locator、ambient singleton 或 `getattr(role, ...)` 绕过能力边界。

### 2.4 一个事实只有一个 owner

同一状态不能由多个包分别维护“最终版本”。缓存、Projection 和索引必须明确是派生状态，并能从权威来源重建。移动 Python 模块不得隐式改变事件 tag、错误 code、序列化 discriminator、Tool identity 或持久化路径语义。

### 2.5 机制与策略分离

- Kernel 拥有单 Agent 状态转换语义；
- Runtime 拥有执行机制与可靠性机制；
- Orchestration 拥有多实体协调策略；
- Product 拥有具体产品默认值和装配决策。

判断不清时，先问该代码是否必须同时观察多个 Agent 或多个任务：若是，通常属于 Orchestration；若只服务当前 Agent、Session、Operation 或本地能力，通常属于 Runtime。

### 2.6 显式装配，不依赖导入副作用

注册表、subscriber、provider、toolset 和可选能力必须有唯一明确的装配入口。允许框架既有的声明式 decorator 注册模式，但最终发现和实例化必须由 composition root 驱动，不能依赖偶然 import 顺序。

### 2.7 顶部 import 是硬约束

除 `ztest/` 外，import 必须位于模块顶部。可选或平台依赖使用模块顶部 `try/except ImportError`；纯类型依赖允许且推荐使用模块顶部的 `TYPE_CHECKING`。`TYPE_CHECKING` 不是违规，也不要求消除同一内聚领域内合理的 type-only 引用；但它不能用于隐藏反向分层、实际运行时依赖，或本应通过重新分层、拆模块、稳定 value object 或窄 Port 消除的概念循环。禁止使用函数内 import 掩盖循环依赖。

### 2.8 不为迁移保留兼容残渣

切换 private/internal import path，或执行 public API 的最终 removal cutover 时，在同一原子 cutover 中：

1. 建立目标定义；
2. 迁移全部生产和测试消费者；
3. 更新公开 facade 和 manifest；
4. 删除旧定义和旧导出；
5. 运行旧路径归零检查。

Private/internal 迁移不保留 forwarding module、re-export、deprecated alias、未使用的重命名变量或“removed”注释。已承诺 public API 必须先走版本和发布治理；批准的临时 deprecation shim 是有 owner、removal version 和发行测试的公共 API 资产，不属于仓内迁移捷径，到期必须删除。

### 2.9 只做必要改动

包治理不借机修改算法、默认值、错误文本、用户交互、持久化格式或业务语义。不因目录对称预建空包，不因单文件较长就机械拆分，不为尚无消费者的未来能力增加抽象。

### 2.10 测试和机器事实优先

`rg` 仅用于人工定位。正式门禁必须读取 Python AST 或机器 manifest，验证真实 import、公开符号、owner、依赖边和旧路径归零。文档中的表格不能替代可执行事实。

### 2.11 类型、泛型与公开签名是架构约束

包治理必须同时保持代码整洁和类型边界，不能只移动文件。生产代码强制遵守：

- 禁止在公开签名中使用裸 `dict`、`list`、`set`、`tuple`、裸 `Callable` 或未参数化的自定义泛型；
- 禁止用 `Any`、`object`、`dict[str, object]` 或字符串分派替代应有的领域 DTO、Protocol、联合类型或泛型参数；
- `Any` 只允许出现在无法静态描述的外部系统边界，必须局部化，并立即校验/转换为领域类型；
- TypeVar、ParamSpec 和返回泛型必须保留输入输出关系，不得为了通过类型检查退化成 `Any`；
- override 必须保持 Liskov 替换关系，不得缩窄输入或任意放宽输出；
- `cast`、`# type: ignore` 和 `# pyright: ignore` 只能用于已验证的第三方或类型系统边界，必须带精确错误码和原因；
- 禁止以 `hasattr`、`getattr`、duck-typed 私有字段访问替代正式 Protocol；
- Pydantic DTO、dataclass 和 Protocol 的职责不能混用：部署配置、可恢复状态、行为能力分别建模；
- tagged union 必须使用封闭 discriminator，禁止依赖类名、模块路径或任意 mapping 猜测类型；
- 可变容器的所有权必须清楚，跨边界优先使用不可变 tuple/frozenset 或显式 snapshot。

类型门禁至少包含 Pyright 和针对公开签名的 AST 检查。现有未类型化代码可作为精确基线，但任何治理切片不得扩大裸泛型、`Any`、ignore 或反射访问的数量；被切片触及的边界必须在同阶段清零。

### 2.12 Import 整洁度是硬门禁

除 `ztest/` 外：

- 所有 import 位于模块顶部；
- 禁止函数、方法、类体和条件分支中的局部 import；
- 可选依赖和平台依赖使用模块顶部 `try/except ImportError`；
- 纯类型依赖允许且推荐使用模块顶部 `TYPE_CHECKING`；
- `TYPE_CHECKING` 中的 import 单独记录为 type-only edge，仍须遵守五层依赖方向；
- 运行时需要构造、注册、`isinstance()`、访问类属性或执行方法的依赖不得伪装成 type-only import；
- import 必须使用 canonical path，不从深层实现路径偶然导入公开符号；
- 禁止星号 import、隐式 namespace 聚合和循环 re-export；
- 禁止通过 import side effect 完成未声明装配；
- import 排序和格式由既有 Black/Isort 配置统一，不在人工迁移中保留特殊排列。

合法插件动态加载不视为局部 import 例外。它必须由受治理 loader 通过 typed manifest 或 Python entry point 驱动，加载目标可枚举、可校验，并进入动态加载清单。

---

## 3. 五层所有权模型

## 3.1 Contracts：跨边界稳定语义

### 拥有

- 跨层 DTO、ID、错误和事件；
- 跨独立实现共享的 Protocol；
- 持久化边界需要稳定身份的数据模型；
- 部署期静态、跨层共享的配置契约；
- 封闭、可序列化的 tagged union。

### 不拥有

- 算法和 IO；
- 注册表实例和动态装配；
- SDK/provider adapter；
- 展示格式和产品文案；
- 只在单个实现内部使用的数据类；
- 仅因“以后可能复用”而提前抽取的类型。

### 分类准入矩阵

Contracts 符号按类型分别准入，不使用同一套序列化要求错误约束 Protocol：

| 类型 | 必要条件 | 特有门禁 |
| --- | --- | --- |
| DTO/config | 存在真实跨层消费者；字段语义稳定；不携带实现对象 | 完整类型参数；边界校验；配置默认值 owner 明确 |
| 持久事件/错误 | 跨持久化或恢复边界；领域 owner 唯一 | 稳定 identity/code/tag、codec version、兼容策略、golden fixture |
| Protocol | 存在真实边界 consumer；由需求/抽象方领域拥有；签名窄；至少一个生产 implementer | 不要求序列化或第二个实现；禁止 service locator、裸容器和实现类型泄漏 |
| ID/value object | 跨层共享；相等性、hash 和规范化语义稳定 | 若持久化则明确 codec；不得携带生命周期资源 |

所有类型共同回答：

1. 语义 owner 是哪个领域？
2. 真实 production consumer 和 producer/implementer 是谁？
3. 为什么不能留在最低合法实现层？
4. 公开签名是否完整类型化并保持依赖方向？

“不进入 Contracts 已经造成反向依赖”不是准入必要条件；在反向依赖出现前建立真实的窄 Port 才是正确治理。没有真实 consumer 的预留契约仍禁止进入。

## 3.2 Kernel：单 Agent 执行语义

### 拥有

- Flow、Graph 和 transition；
- Think、Parser 和 Command Channel；
- Prompt 的模型无关语义；
- ReAct/Review 等执行拓扑；
- Tool call 的解释与统一 IR；
- 输出验证和 Kernel telemetry 注入点。

### 不拥有

- 模型客户端和 provider SDK；
- 文件、网络、终端、数据库等 IO；
- Session 持久化实现；
- 多 Agent、后台任务或全局调度；
- Product 默认策略和具体 Toolset；
- 进程本地生命周期资源。

### 约束

Kernel 只消费 Contracts 和注入能力。任何 Runtime 类型进入 Kernel 公开签名都视为边界违规。

## 3.3 Runtime：单 Agent 与可靠执行机制

### 拥有

- Role、RoleComponents、RoleState 和 incarnation；
- provider-neutral 模型客户端基类、请求执行、retry/stream、usage、路由执行、Gateway 和生命周期；
- Context history、compaction 和 turn context bus；
- Tool executor、权限执行链、MCP 和 ToolResult；
- Session journal、checkpoint、replay 和恢复机制；
- 文件操作、终端、sandbox、watching，以及 LSP process/client/session/buffer 等通用运行机制；
- EventFabric、本地订阅、telemetry 和日志；
- Artifact、ledger、persistence、resilience 等执行基础设施；
- 生命周期、资源释放和单实例 control primitive。

### 不拥有

- 多 Agent registry、parent/child tree；
- 跨 Agent mailbox routing；
- 后台任务产品语义和工作流编排；
- 多实体 admission、residency 和成本配额决策；
- Cron/automation 触发语义；
- Product 默认路径、`.mote` discovery 和用户配置优先级；
- Skills、Coding Agent 和 CLI 策略。

### 关键判定

Runtime 回答：

> 已经决定执行当前 Agent/Operation 后，怎样在当前 Python 运行环境中正确、可恢复地完成？

## 3.4 Orchestration：多实体协调

### 拥有

- 多 Agent identity catalog、registry 和 parent/child tree；
- Spawn admission、transaction 和 rollback；
- Agent residency、通信、mailbox routing；
- 多 Agent turn scheduling 和并发控制；
- 后台任务生命周期、通知、pause/resume/cancel；
- capability-agnostic workflow/DAG；
- automation/cron 触发与 durable schedule；
- 跨 Agent cost、token 和资源配额策略。

### 不拥有

- Role 私有结构和 Tool executor 实现；
- 文件、终端、模型客户端等 Runtime 机制；
- Product 的具体 Role factory、默认路径和 UI；
- Kernel Flow 语义；
- 为方便复用而下沉的通用工具函数。

### 关键判定

Orchestration 回答：

> 多个 Agent、任务或工作流之间，谁在什么时候运行，如何通信、限额、恢复和收敛？

## 3.5 Product：具体能力与装配

### 拥有

- CLI/TUI 和产品入口；
- Coding Agent、内置 Toolsets、Skills；
- OpenAI、Anthropic、DeepSeek 等具体 provider SDK adapter、provider catalog、凭证来源选择和用户配置映射；
- LSP 的语言选择、binary discovery、默认配置和 Coding Agent 集成；
- `.mote` discovery、默认路径和用户配置优先级；
- Role/Agent factory；
- Runtime 与 Orchestration 的 composition root；
- 用户可见文案、展示和审批交互。

### 不拥有

- Runtime 通用机制；
- Orchestration 通用工作流；
- Contracts 中的跨层稳定语义实现；
- Kernel 的模型无关执行逻辑。

Product 可以依赖所有下层，但只能在明确 composition root 装配协作者，不能把装配散落在功能模块中。

---

## 4. Runtime 与 Orchestration 的拆分规则

本治理最容易混淆的边界是 Runtime 与 Orchestration。使用以下矩阵：

| 能力 | Runtime owner | Orchestration owner |
| --- | --- | --- |
| Agent 执行 | 单个 Role 的 run/cleanup/recovery | 多个 Agent 的 admission、排队和调度 |
| Scheduler | 当前运行环境中的 execution primitive | 多 Agent turn、任务和 workflow 调度策略 |
| Cost | 模型调用 usage 采集 | Agent 树或任务域的配额决策 |
| Events | 产生、本地分发、背压和订阅机制 | 消费事件驱动多实体协调 |
| Recovery | replay、checkpoint、incarnation 重建机制 | 决定哪个 Agent/任务恢复以及恢复顺序 |
| Lease | 单次执行的 guard/fencing primitive | 多实体所有权和 admission 策略 |
| Persistence | 通用存储 primitive 和领域 repository 实现 | 多 Agent/任务状态的领域 owner |
| Background work | 可注入的等待、执行和生命周期 primitive | 后台任务身份、状态机、通知和 DAG |

### 4.1 不按名称机械搬迁

带 `control`、`scheduler`、`lease`、`recovery` 名称的代码不自动属于 Orchestration。必须查看它是否需要多个实体的全局视图。单 Agent、单 Session 或单 Runtime 的机制留在 Runtime。

### 4.2 不整体下沉 Orchestration

`orchestration` 可以消费 Runtime primitive，但不能因此把后台任务、Workflow 或 AgentControl 整体下沉 Runtime。向下依赖是健康分层，不是移动 owner 的理由。

### 4.3 Agent 绑定 adapter 上移 Product

当 Orchestration 通用能力需要构造具体 Role、访问具体 Toolset 或注入 Product 默认值时，绑定 adapter 应由 Product composition root 拥有。Orchestration 接收 typed build context 或窄 factory Port，不穿透 Role 私有字段。

---

## 5. 包设计规则

## 5.1 一级包准入

一个新的一级包必须同时满足：

- 能用一句话描述唯一领域 owner；
- 有明确职责和公开边界；若持有资源，则有独立且可说明的生命周期；
- 至少存在一个真实生产消费者；
- 不依赖同层未批准反向边；
- 不是原包中几个无关模块的剩余集合；
- 名称不使用 `common/shared/utils/misc/helpers/core/base` 作为含糊容器；
- 无需通过其他包私有字段才能工作。

不能满足时，应成为现有领域包的子模块，或继续留在当前 owner。

## 5.2 Facade 规则

- 根包可以提供小型、显式批准、由 public API manifest 约束的用户级 facade；不得聚合内部实现、领域内部符号或自动发现结果；
- 领域包可以提供精选、显式、机器清单约束的 facade；
- 内部模块路径不自动承诺稳定；
- 禁止动态 `__getattr__`、星号导出和自动扫描聚合；
- 删除符号时同步删除 facade 导出。

根 facade 是独立的 release-owned 治理对象，每个导出必须记录：

```text
export_manifest
canonical_import_path
source_symbol_identity
core_or_optional_dependency_class
import_side_effect_budget
deprecation_and_removal_policy
docs_and_type_surface
api_owner
```

根 `Engine`、`Agent`、`Model`、消息、输出和工具 facade 的当前公开承诺不得由领域包移动隐式改变。

## 5.3 文件拆分规则

文件大小只是评审信号，不是拆分理由。只有出现不同 owner、不同生命周期、不同依赖方向或可独立测试的职责时才拆分。

禁止：

- 为每个名词建立单文件；
- 为目录对称建立空包；
- 把类型、错误和 helper 机械拆成横向文件；
- 用 `_internal` 掩盖边界穿透；
- 把循环依赖双方塞入第三个“公共”模块。

## 5.4 Protocol 规则

- Protocol 由抽象需求方的领域拥有；
- 物理位置位于 `contracts/ports/<domain>/`；
- 接口必须窄且具有业务语义；
- 禁止 service locator；
- 禁止暴露整个 `Role`、`RoleState`、Environment 或任意 dict；
- 必须记录生产 consumer 和 implementer；
- 没有第二个实现不是拒绝 Port 的理由，但必须存在真实边界需求；
- 没有真实 consumer 的预留 Port 禁止合入。

## 5.5 生命周期规则

“唯一 owner”必须区分定义所有权和实例所有权，不能被解释成全局 singleton。每个持有 Task、锁、client、subscriber、文件句柄或后台 worker 的组件必须登记：

- `resource_type_owner`：资源类型与语义 owner；
- `factory_owner`：创建工厂 owner；
- `instance_scope`：application/session/agent/run/turn/operation；
- `instance_owner`：该实例的生命周期 owner；
- `close_authority`：唯一关闭权限；
- `shared_by`：允许共享的实例集合；
- `shutdown_order`：相对依赖资源的关闭顺序；
- 构造入口；
- 是否惰性；
- cancellation 行为；
- 失败后的可重试性；
- 测试隔离方式。

资源由创建它的 composition/lifecycle owner 关闭。禁止由消费者猜测或重复关闭共享资源。

## 5.6 动态加载治理

禁止的是利用动态 import 逃避分层、未登记的字符串路径、偶然 import 顺序和无 owner 自动扫描；由 typed manifest 或 Python entry point 驱动的插件加载可以合法存在。

每个合法加载点进入 `dynamic_import_manifest`：

```text
import_site
owner
allowed_module_prefixes
reason
loader_contract
failure_policy
lifecycle_scope
entry_point_group
```

架构测试解析 loader 和 manifest，验证加载目标不突破层级、没有任意用户字符串直达 `import_module`、失败不会留下半初始化 registry 或资源。未登记动态加载点为硬失败。

---

## 6. 状态与持久化治理

### 6.1 状态分类

每项状态必须被标记为以下之一：

| 类型 | 例子 | 约束 |
| --- | --- | --- |
| 配置 | `RoleSchema` | 按 mutability/pinning 分类，不混入 Role 实例属性 |
| 可恢复运行状态 | `RoleState` | 可序列化，不包含进程对象 |
| 持久事实 | rollout event、operation record | 唯一 schema owner，身份稳定 |
| Projection/cache | 列表、索引、统计 | 可删除并从持久事实重建 |
| 短暂资源 | Task、lock、client、fd | 由 incarnation/lifecycle owner 管理，不持久化 |

配置进一步分类：

```text
deployment_static
startup_resolved
reloadable_generation
session_pinned
```

配置 inventory 必须记录 `mutability`、`resolution_time`、`pinning_scope`、`generation_identity`、`reload_owner` 和 `secret_classification`。涉及配置的 cutover 必须验证 reload 不改变既有 Session 语义、generation identity 稳定、snapshot retention/GC owner 明确、resume 可重建原 generation、来源优先级不变且 secret 值不进入 facts/evidence。

### 6.2 Session truth

- rollout journal 是历史的崩溃安全真相源；
- Recorder 是 EventBus subscriber，不向 ContextManager 注入 sink；
- replay 单次正向扫描；
- `compacted` 事件重置为 replacement history；
- 未知事件按既有兼容策略处理；
- resume 向已构造 Role 灌入历史，不从 rollout 猜测 RoleSchema。

包移动不得改变这些语义。

### 6.3 持久身份与模块路径解耦

持久事件 tag、错误 code、Tool identity、discriminator 和存储 key 不得默认由 Python 模块路径生成。治理移动必须在迁移前冻结这些身份，并通过 golden fixture 验证移动前后相同。

### 6.4 Role identity 专项冻结

当前 Role/session identity 存在 `role_type_id` 与 `module.qualname` 并存的实际风险。在专项治理节点完成前，`runtime.agent.Role`、具体 Role 子类和 Product Agent 类型的类路径迁移属于 forbidden cutover。专项节点必须：

1. 枚举全部 role identity writer 和 reader；
2. 明确 canonical `role_type_id`；
3. 冻结旧 `module.qualname` reader；
4. 建立新旧 journal/session golden fixture；
5. 证明旧 Session 可恢复到新类；
6. 禁止新 writer 继续写模块路径；
7. 把旧 reader 标记为持久兼容 reader，而不是 internal forwarding 残渣。

---

## 7. 日志、事件与观测治理

### 7.1 三类边界

- Contracts 拥有事件事实和稳定身份；
- Runtime 拥有 EventFabric、投递、订阅、journal 和 telemetry 机制；
- 具体领域拥有 subscriber 的业务行为；
- Product composition root 装配跨能力 subscriber。

### 7.2 日志约束

- 不在 method body 新增零散 `logger.*`；
- 关键类使用 `@log_class`；
- 热路径和平凡 accessor 加入 `exclude`；
- tenacity `after_log` 保持为重试配置；
- 包迁移不能改变 trace/session 绑定语义。

### 7.3 注册约束

Event tag、subscriber identity 和 decoder table 必须有唯一注册入口。禁止多个包通过 import side effect 争夺同一 tag，禁止 Product 类型出现在 Runtime event union。

---

## 8. 治理事实与清单

正式迁移前应建立机器可读 inventory。每个生产模块至少记录：

```text
module
layer
semantic_owner
package_owner
public_symbols
production_consumers
test_consumers
runtime_dependencies
persistent_identities
lifecycle_resources
disposition
target_module
status
evidence
```

每个符号至少记录：

```text
symbol_id
canonical_import_current
canonical_import_history
visibility
stability
owner
consumer_layers
serialization_identity
migration_id
```

事实条目的分类状态统一为：

```text
confirmed
not_applicable
unknown
conflicted
```

只有 `confirmed` 和具有明确理由的 `not_applicable` 计入分类完成；`unknown` 和 `conflicted` 永不计入覆盖率。扫描到文件、AST 节点或 import 不代表已理解符号性质、owner、identity 或消费者。

### 8.1 覆盖率语义

所有 coverage 必须从事实集合计算，禁止写入常量或由 manifest 文件存在推导：

```text
coverage = (confirmed + not_applicable) / candidate_total
```

分母必须是相应候选扫描器产生并可审计的全集。分母为零时只能报告“候选为空”或“扫描器未建立”，不得自动写成 100%。至少分别计算：

- module discovery coverage；
- symbol classification coverage；
- visibility/stability coverage；
- production consumer coverage；
- test mapping coverage；
- persistent identity coverage；
- event tag/error code/discriminator coverage；
- lifecycle resource coverage；
- registry/queue/journal owner coverage；
- dynamic reference coverage。

每项输出 numerator、denominator、confirmed、not_applicable、unknown、conflicted 和候选生成器版本。没有候选扫描器时状态必须为 `unsupported`，不能声明完成。

### 8.2 API 可见性、稳定性与发布兼容

API 使用两个正交维度，不再用 production consumer 推导 public：

```text
visibility = private | package_internal | cross_layer_internal | plugin_api | public_api
stability = unstable | experimental | stable | deprecated
```

Public/plugin API 的证据来自人工批准 manifest、精选 facade、发布文档、entry point 或插件契约。仓内消费者只能证明内部使用；没有仓内消费者也不能否定已发布 API。

公开和插件 API 必须记录：

```text
compatibility_policy
first_public_version
deprecation_version
removal_version
replacement_import
release_owner
evidence
```

- `private/package_internal/cross_layer_internal`：仓内 consumer closure 完整后允许同切片原子删除；
- `plugin_api/public_api`：按 stability、发布承诺和 removal policy 处理；
- `stable public_api`：只能在批准的 breaking release 中删除，或经过有期限的 deprecation release；
- facade 的 public surface 与内部模块路径分别登记，内部移动不应无意扩大或缩小 facade；
- 外部插件、entry point 和已知下游项目进入兼容证据，不能以仓内扫描结果代替仓外决策。

`symbol_id` 必须一次分配且与路径和内容 digest 无关。移动模块只更新 `canonical_import_current/history`，不得改变 symbol ID。

仓内零兼容残渣适用于已获准的最终 cutover，不能凌驾于已发布 API 契约。若兼容窗口要求临时 re-export，它必须作为 release-owned、带 removal version 的公开 API 资产，而不是无期限的内部迁移残渣。

机器生成事实和人工决策必须物理分离：

- facts 文件由工具覆盖生成；
- decisions/migrations 文件由评审批准；
- 工具不得覆盖人工 owner 决策；
- 未知 owner、目标或持久身份会阻止生产 cutover。

### 8.3 身份候选与清单

Identity、event 和 error manifest 必须是事实清单，不能用空数组占位表示完成。自动扫描至少发现：Enum/error code、event tag、journal record type、`Literal[...]`/discriminator tag、codec mapping key、Tool name、Agent/Session/Run/Operation ID、durable envelope/version、store key prefix，以及 module-qualified pickle/discriminator/dotted path。

人工对每个候选确认是否持久、owner、compatibility level、fixture 和 migration policy。每个候选必须处于四种分类状态之一；只有候选扫描器证明全集为空时，空 manifest 才合法。

### 8.4 扫描器可信范围

治理扫描器分别生成三张图，不把启发式结果宣称为完整证明：

- import graph：分别记录 runtime edge 和 `TYPE_CHECKING` type-only edge；两者均检查分层，只有 runtime edge 参与模块初始化和运行时循环判定；
- symbol graph：可静态解析的符号定义与直接使用；
- re-export graph：`__all__`、facade 和 alias 链。

扫描器必须识别或显式报告：module alias、relative import、re-export、annotation string、entry point、配置 dotted path、动态加载和 module-qualified persistence identity。无法可靠解析的引用记录为 `unknown_reference`，包含位置、类别和原始表达式。

事实扫描器只在其声明的 soundness 范围内提供证明。相关切片存在 `unknown_reference` 时禁止 cutover，直到人工裁决进入 manifest 或扫描能力补齐。public API 以显式 manifest 为准，不以源码启发式 public definition 为准。

### 8.5 Digest 与决策失效

Digest 分为：

```text
repository_snapshot_digest
governed_fact_digest
source_module_digest
source_symbol_signature_digest
consumer_closure_digest
identity_fixture_digest
decision_schema_version
```

Repository digest 只作审计；governed fact digest 决定治理域是否漂移。每项人工 decision 绑定相关 source、signature、closure 和 identity digest。无关文件变化不得使 decision 失效；相关模块、签名、消费者、re-export、动态引用或 fixture 变化必须 fail closed，仅使受影响 decision 失效。

Decision 引用不存在的 module/symbol、旧 canonical import 或不匹配 digest 时必须报 drift，不能仅凭相同旧 baseline ID 继续执行。

### 8.6 Migration manifest 状态机

合法状态为：

```text
proposed → discovery_complete → classification_complete
         → identity_complete → ownership_complete
         → closure_complete → migration_ready → approved
         → cutting_over → verified → completed

approved/cutting_over → rolled_back
任意非 completed 状态 → superseded
cutting_over → verification_failed → contained
contained → repair_pending → repaired
contained → rollback_pending → rolled_back
```

每次转换记录 actor、UTC 时间、依据 commit、工作树 baseline、证据 digest 和备注。

- `discovery_complete`：治理域文件和 AST 候选发现完成；
- `classification_complete`：相关候选均为 confirmed/not_applicable，无 unknown/conflicted；
- `identity_complete`：身份候选均确认或明确 N/A，fixture 完整；
- `ownership_complete`：相关 owner 决策完成且无 multi-owner；
- `closure_complete`：静态、re-export、动态和仓外兼容边界已闭合；
- `migration_ready`：DAG、精确测试、发行和回滚策略已批准；
- `approved`：迁移 DAG、测试、发布策略和回滚策略批准；
- `cutting_over`：绑定唯一 cutover commit；
- `verified`：全部测试与门禁证据已保存；
- `completed`：节点要求的代码、文档和资产 closure 已在受保护分支真实提交上验证完成；这是不可回退的 execution 历史，不代表授权永远有效；
- `rolled_back`：记录 source/data/release 回滚结果；
- `superseded`：由新的 migration ID 替代，原记录不可覆写。
- `contained`：失败影响已冻结且没有继续扩散；
- `repair_pending`：已登记 forward-fix repair 节点但尚未完成；
- `repaired`：repair 节点已验证，原失败历史保留且受影响不变量恢复；
- `rollback_pending`：已登记 rollback 节点但尚未验证完成。

Migration ID 必须关联临时例外、测试选择、identity fixture、API 变更和 release artifact。

状态机还必须支持：

```text
blocked
verification_failed
aborted
```

- `blocked`：外部前置或必需人工裁决未完成，必须记录 blocker owner 和解除条件；
- `verification_failed`：已有 cutover commit 但验证失败，保留失败证据并进入 rollback 或 forward-fix；
- `aborted`：尚未产生 production cutover，决策被显式终止；
- 已产生 cutover commit 的 migration 不能通过 `superseded` 抹去历史，后继节点必须用 `repairs` 或 `supersedes` 引用原记录。

Git/CI 闭环分为三类不可混淆的 attestation：

1. prepare/approval attestation；
2. atomic production cutover commit；
3. verification/completion attestation。

“原子”只约束生产定义、批准消费者切换和 internal 旧路径删除位于同一个 production commit；不要求事后测试证据预先存在于该 commit。

### 8.7 Evidence 模型

每份验证证据至少记录：

```text
evidence_id
migration_id
gate_id
command_id
command_definition_digest
source_commit
environment_fingerprint
started_at
finished_at
result
stdout_digest
artifact_digests
runner_identity
attestation_format
data_classification
redaction_profile
contains_user_content
contains_secret_material
retention_policy
access_scope
```

Evidence append-only；失败记录不可覆盖。`verified` 只能引用不可变 evidence ID。命令定义、source commit 或必需环境发生变化后，旧 evidence 不满足新 Gate。

Facts 和 evidence 原则上只记录结构、仓库相对路径、hash 和类型，不保存 secret 值。Environment fingerprint 使用批准字段白名单，禁止 dump 全部环境变量；stdout/stderr 入库前必须脱敏，原始敏感输出采用受限访问和独立保留策略。用户 home、绝对路径、私有 endpoint、临时凭证、用户输入、私有插件名和仓库 URL 必须按 data classification 处理。

### 8.7.1 规范编码与并发写入

Digest 基于版本化 canonical encoding，至少冻结 UTF-8、LF、key/list 排序、路径规范化、UTC 时间格式、空值/缺省值、数值编码、symlink 和文件 mode 表示，以及 schema upgrade 后的 digest 规则。建议以 canonical JSON 作为 digest 输入，TOML 只作为人工评审视图。

生成器必须写临时文件、完整校验后原子替换。并行生成使用 lock 或 compare-and-swap，禁止最后写入者覆盖更新事实；人工 decisions 与生成 facts 物理分离，生成器没有覆盖人工文件的权限。

### 8.8 Packaging 与资源事实

事实扫描和迁移计划必须覆盖 `package_data`、`MANIFEST.in`、project scripts/entry points、`importlib.resources`、基于 `__file__` 的相对读取、资源 glob、平台二进制、optional import 和 extras。

移动包路径时必须明确哪些资源随包移动，并通过 wheel、sdist、clean install、CLI、资源读取和 public API snapshot 证明发行闭环。

### 8.8.1 文件来源类型

每个发布相关文件分类为：

```text
source_kind = authored | generated_source | vendored_source | generated_artifact
authority_path
generator_identity
generator_version
generation_command_id
generation_environment_digest
output_paths
determinism_policy
allow_manual_edit
owner
```

- authored source：执行完整架构、类型、格式、行为门禁；
- generated source：从权威输入和 generator 修改，验证 clean regeneration 零差异；格式问题修 generator/template，不手改输出；
- vendored source：验证 provenance、license、local patch 和更新方式，类型/格式债使用精确基线；
- generated artifact：验证输入、生成链和 digest，不参与普通源码 owner 推断，禁止手改。

来源类型不豁免依赖、安全和发行治理：任何进入 wheel 或在 `mote` 进程执行的代码均进入实际图和安全审查。若权威 schema/generator 不在仓内，登记外部 authority、固定版本和取得方式；不可再生输出不能标记治理完成。

### 8.8.2 Vendored 来源与许可证

每个 vendored subtree 记录：

```text
upstream_project
upstream_repository
upstream_revision_or_release
imported_path_set
license_identity
notice_paths
local_patch_series_or_digest
update_owner
update_method
security_advisory_owner
redistribution_constraints
```

普通包治理不得顺手重写 vendored 算法。纯路径移动由领域 owner 和 license reviewer 联合批准；内容变化分类为可重放 local patch 或独立 upstream-sync 节点，并验证 NOTICE/license 和行为 fixture。

### 8.9 Touched closure 分类

为避免包迁移扩张成无限类型重构，touched closure 区分：

```text
moved_definition
changed_signature
changed_import_only
direct_consumer
transitive_type_consumer
```

- 新增或改变的公开签名满足完整类型要求；
- 仅更新 import 的消费者不得新增类型债，但不强制清除整个文件的既有债；
- 被直接修改的违规表达式应修复；
- 未触及旧问题进入精确基线；
- 需要改变业务行为的类型修复拆成独立 migration。

### 8.10 审批角色与职责分离

治理 schema 至少定义：

```text
proposer
source_owner_reviewer
target_owner_reviewer
api_release_reviewer
persistence_reviewer
security_reviewer
executor
evidence_runner
completion_authority
```

最低约束：proposer 不能单独批准自己的 migration；跨 owner cutover 由 source/target 双方批准；public/plugin API 需要 release reviewer；journal/codec/identity/replay 需要 persistence reviewer；permission/sandbox/secret/command 路径需要 security reviewer；正式 evidence 由受信 CI runner 产生；completion authority 只能在全部必需 reviewer 和 Gate 满足后签发。

Actor identity 绑定代码评审身份、受保护分支提交签名或 CI attestation；普通 TOML 字符串不构成认证。

### 8.11 并行 Cutover Claim 与冲突图

每个节点在 approval 前声明：

```text
claimed_modules
claimed_symbols
claimed_facades
claimed_manifests
claimed_identities
claimed_resources
claimed_packaging_entries
```

Validator 据此构造 conflict graph。只有 predecessor 满足且 claim 集合不冲突的节点才能并行进入 `cutting_over`；composition root、公共 facade、codec/registry 和 packaging 等热点默认显式串行化。

Approval 绑定 base commit 和 merge policy。Rebase、merge、cherry-pick 或 governed facts 变化后默认重新计算 claim、closure 和 projected graph；只有相关局部 digest 未变时才能复用批准。两个节点不得同时删除或成为同一 symbol/identity 的 owner。

### 8.12 Crash-safe 状态转换与对账

每次状态转换记录：

```text
transition_id
expected_previous_state
observed_source_commit
resulting_commit
idempotency_key
evidence_set_digest
reconciliation_status
```

转换使用 compare-and-set 前置状态并保持幂等。恢复器根据 Git 历史、manifest 和 evidence store 对账，不盲目重放动作。相同 idempotency key 返回相同结果；冲突 transition fail closed。Production commit 已合入但 manifest/evidence 未完成、CI 重试或 rollback 已合入等情况必须可被 reconciliation 安全收敛。

### 8.13 Behavior Contract

“包治理不改变行为”必须成为节点级可验证契约，而不是由普通测试通过间接推断。每个 cutover 按适用范围登记：

```text
callable_inputs_outputs
exception_type_and_error_code
default_values
ordering_and_deduplication
cancellation_semantics
timeout_and_retry_semantics
resource_cleanup_order
event_sequence
logging_or_telemetry_identity
permission_decision_path
serialization_bytes_or_canonical_payload
cli_exit_code_stdout_stderr
```

每个条目必须标记为 stable contract 或 internal implementation；只冻结与节点相关的稳定行为，不冻结无关实现细节。验证分为：

1. characterization fixture：冻结迁移前行为；
2. differential test：对同一输入比较迁移前后结果、事件和副作用；
3. invariant test：验证 owner、顺序、幂等、取消、权限和持久化不变量。

有意改变稳定行为的节点不属于纯包治理 cutover，必须拆成独立功能或兼容 migration。

### 8.14 Test Determinism Policy

Test plan 必须声明：

```text
determinism_class
random_seed_policy
clock_policy
network_policy
retry_policy
timeout_policy
quarantine_status
known_flake_id
required_consecutive_passes
```

- architecture、manifest、codec、golden 和 projected-graph 测试必须 deterministic；失败后重跑通过仍视为 Gate 失败，直到原因解释并生成新 evidence；
- PTY、async、platform 和 subprocess smoke 可按批准策略重试，但所有尝试均进入 evidence；
- pytest rerun 不得隐藏失败尝试；
- 真实时间、随机数、网络和用户目录必须显式标记并通过可控边界注入；
- known flaky test 不能成为唯一 Gate 证据；
- quarantine 必须有 owner、issue ID 和到期节点，且切片不得扩大 quarantine。

### 8.15 Differential Harness

Atomic cutover 不允许为了比较长期保留双实现。允许：

- 在 prepare commit 生成版本化 golden fixture；
- 在两个独立 Git worktree 或构建产物运行相同测试向量；
- 用旧 wheel 和新 wheel 的隔离子进程比较 public 行为；
- 用迁移前持久 fixture 验证新 reader；
- 对纯函数使用 `ztest/` 中的冻结参考向量。

禁止复制旧生产定义、增加长期 runtime feature flag 或 forwarding implementation 只为 differential test。

### 8.16 支持平台与依赖矩阵

每个 cutover 根据 touched closure 计算：

```text
python_versions
operating_systems
architectures
dependency_extras
optional_dependency_absence
filesystem_capabilities
locale_and_encoding
```

最低要求：静态 Gate 覆盖所有声明支持的 Python 版本的语法/AST；运行时代码覆盖最低和最高 Python 版本；fileops/locking/path identity 覆盖 POSIX 与 Windows；平台资产验证 wheel 内容和缺失时失败语义；optional capability 同时验证 extra present/absent。无法本地执行的平台必须由受信 CI evidence 覆盖，不能口头豁免。若支持矩阵无法兑现，应先通过独立发布决策缩窄公开支持范围。

### 8.17 依赖解析与供应链证据

Clean-install/build evidence 记录：

```text
resolver_input_digest
resolved_dependency_manifest
artifact_hashes
package_index_identity
build_backend_version
build_isolation_mode
installed_file_manifest
```

包治理原则上不顺带升级依赖或 build backend；确需变更时拆成独立节点，并分别验证旧、新依赖集合。正式 wheel/sdist evidence 保存 artifact digest 和安装文件清单，避免供应链漂移污染治理结论。

### 8.18 Public Deprecation Contract

有期限的 public deprecation shim 必须记录：

```text
warning_category
warning_message_id
stacklevel
first_warning_version
removal_version
replacement_import
documentation_url
telemetry_policy
```

验证旧 import 在兼容窗口可用、warning 不在内部循环刷屏、stacklevel 指向用户调用点、新 import 不触发旧路径 eager import，并在 removal release 证明旧路径消失。`DeprecationWarning` 是否默认可见属于明确产品决策。Public shim 有 removal version；持久兼容 reader 根据历史数据保留策略治理，两者不得混同。

### 8.19 Error/Event Compatibility Contract

Error code 或 event tag 相同不足以证明兼容。身份条目扩展为：

```text
identity
schema_digest
codec_version
canonical_fixture_digest
ordering_constraints
terminality
retryability
unknown_handling
reader_versions
writer_version
delivery_plane
exception_cause_policy
```

涉及 error/event 的 cutover 必须验证 payload 字段及默认值、顺序、terminal/non-terminal、unknown handling、retry/recovery、exception wrapping/cause、subscriber delivery plane 和 journal canonical bytes。

### 8.20 Protocol Signature 与结构化 Closure

Protocol 的 `source_symbol_signature_digest` 覆盖：参数位置、名称、默认值、sync/async、泛型方差、property/method、context manager、exception/cancellation 语义和 `runtime_checkable` 行为。

Consumer closure 必须发现结构化 implementer、test fake、mock 和 plugin，即使其没有显式继承 Protocol；Projected closure 验证 covariant/contravariant 关系和 `isinstance` 使用。仅哈希 symbol name 或未经规范化的源码文本不合格。

### 8.21 Target Owner Readiness

`target_owner_readiness` 必须由证据计算：

```text
target_package_exists_or_is_created_in_node
target_owner_approved
target_dependency_edges_legal
target_facade_policy_known
target_lifecycle_capacity_known
target_tests_present
target_packaging_rules_known
target_no_competing_definition
```

目标包尚不存在时，其创建、owner、边界 manifest 和最小测试必须位于同一节点或 verified predecessor 中；空目录不构成 ready。

### 8.22 性能与资源非回归预算

触及热点时按适用范围记录迁移前基线和允许回归比例：

```text
import_latency
peak_memory
task_thread_count
open_fd_count
event_queue_bound
startup_io
wheel_size
```

不设脱离场景的全仓统一阈值。超预算必须解释并批准，或拆成独立性能变更；包治理不能成为 eager import、重复 registry/codec、无界队列或资源加载回退的通道。

### 8.23 Public API / Composition、Packaging 与 Release Owner

根 `engine.py`、`agent.py`、`model.py`、`messages.py`、`output.py`、`tools.py` 和根 facade 由 Product 层的 Public API / Composition capability（或等价的明确 release-owned capability）治理，而不是从被导出符号的原始层自动推断 owner。

触及根 facade、transitive eager import、entry point、公开签名或 build metadata 的节点必须具有：

```text
source_domain_owner
target_domain_owner
public_api_composition_owner
packaging_owner
release_reviewer
```

并 claim 对应 facade 和 build manifest，与其他触及同一热点的节点串行化。

### 8.24 Security Touch Classification

安全触达按调用语义、数据流和信任边界分类，不只按目录、函数名或 AST 词法命中：

```text
shell_command_boundary
argv_process_boundary
permission_classifier_boundary
sandbox_wrapper_boundary
dynamic_code_evaluation
dynamic_import_or_plugin_boundary
private_capability_reflection
benign_structural_introspection
credential_or_secret_boundary
filesystem_path_boundary
```

每类定义批准 API、输入信任级别、必经 classifier/validator、允许例外和 security reviewer 触发条件。迁移若把 argv 改成 shell、改变 quoting、绕开 classifier，或将用户输入接入动态属性/代码执行，必须 fail closed。

`getattr`/`hasattr` 不按词法一律禁止：跨 owner 私有能力穿透属于违规；Protocol、optional integration、serialization 和展示中的良性结构反射可在明确 owner、typed boundary 和安全分类下存在。

### 8.25 Phase Artifact Contract

每个 Phase/Track 必须有机器化输入输出契约：

```text
phase_id
authority_version
control_journal_revision
facts_generation_id
provenance_root_ids
required_input_artifacts
required_input_states
produced_artifacts
artifact_schema_versions
producer_command_ids
verification_command_ids
required_evidence_ids
allowed_mutations
forbidden_mutations
failure_state
resume_rule
completion_attestation
next_phase_unlocks
```

Eligibility 由 phase manifest 针对唯一 journal revision、完整 facts generation 和 provenance roots 计算，不靠操作者阅读章节判断。Completed 阶段的相关输入、schema、command 或 digest 漂移时，历史 execution completion 保持不变；其 authorization 转为 `stale/revoked/unknown`，相关 invariant 重新计算，下游 eligibility 进入 blocked。界面和机器输出必须同时展示三类状态，不得把“历史已完成”误显示为“当前仍可授权”。

### 8.26 Gate Registry

顶级 Gate 使用唯一、封闭、版本化 registry：

```text
G1_DISCOVERY
G2_CLASSIFICATION
G3_IDENTITY
G4_OWNERSHIP
G5_CLOSURE
G6_MIGRATION_READINESS
```

每项 Gate 记录：

```text
gate_id
gate_schema_version
applies_when
required_inputs
validator_command_id
pass_predicate
not_applicable_predicate
required_reviewers
produced_evidence_kind
invalidated_by
applicability_rule_id
facts_generation_id
provenance_root_ids
freshness_policy_id
```

Security、packaging、public API、persistence、platform、behavior 和 performance 是 `G6_MIGRATION_READINESS` 的条件化子门禁；前五项的领域专项检查归相应顶级 Gate。Applicability 必须由第 8.43 节规则计算，unknown fail closed。文档不得再使用未注册的开放式“六个 Gate”。

### 8.27 Schema Evolution

所有治理 schema 使用 expand/contract 迁移并登记：

```text
reader_min_version
reader_max_version
writer_version
schema_migration_id
from_version
to_version
lossless_or_lossy
backup_digest
forward_transform
rollback_transform_or_forward_only
mixed_version_read_policy
mixed_version_write_policy
sunset_condition
```

先部署可读旧/新且只写旧的 reader，再迁移并双格式校验，然后切换 writer，最后停止旧读。有损迁移必须 forward-only 并保留原始不可变审计副本。旧 runner 不得静默丢弃新字段，新 generator 不得覆盖人工 decision。

### 8.28 CLI Command Contract

每个命令冻结：

```text
exit_code_0
exit_code_for_drift
exit_code_for_unknown_or_conflict
exit_code_for_invalid_schema
exit_code_for_policy_rejection
exit_code_for_internal_error
stdout_format_and_schema
stderr_contract
write_set
dry_run_behavior
atomicity
idempotency
network_policy
clock_and_locale_policy
structured_execution_descriptor
shell_boundary_or_none
```

写命令与 `*-check` 读命令分离；check 不修改 facts、mtime 或 lock state。机器 JSON 使用版本化 envelope，人类文本走独立通道。Command definition digest 覆盖结构化 argv、环境白名单、工具版本、工作目录、副作用与 shell boundary；禁止执行 manifest 中的自由命令字符串。

### 8.29 Governance Lease 与 Fencing

正式写操作必须在洁净、隔离 workspace/worktree 中持有操作租约：

```text
operation_id
workspace_id
base_commit
lease_owner
fencing_token
acquired_at
expires_at
heartbeat_policy
claimed_write_set_digest
release_result
```

每次发布写入校验 fencing token；过期 runner 不得覆盖新执行者。Facts 可在临时目录生成并校验后 CAS 发布；用户脏工作树不能作为正式 cutover workspace，也不能被生成器改写。

### 8.30 Trust Policy Lifecycle

Actor/runner 信任根记录：

```text
trust_policy_version
trusted_issuer
subject_mapping
allowed_role_claims
protected_branch_or_environment
attestation_signature_algorithm
key_or_identity_rotation
revocation_source
issued_at
expires_at
repository_and_workflow_identity
replay_protection
```

Approval/evidence 保存并验证 policy digest。撤权后尚未 cutover 的批准失效；历史 evidence 保留当时信任上下文。Actor 名称或 runner 字符串不能单独构成信任。

### 8.31 Execution、Authorization 与 Invariant 三状态

历史执行事实、未来授权可用性和当前不变量满足度必须分离：

```text
execution_state = completed | rolled_back | aborted | verification_failed | ...
authorization_state = current | stale | revoked | superseded | unknown
invariant_state = satisfied | violated | unknown
```

`execution_state=completed` 是 append-only 历史，不因后续漂移回退。相关事实、证据或 trust policy 漂移只改变 authorization，并触发 invariant recheck。不变量被后续提交破坏时创建 violation/repair 节点，不篡改原 migration。每个 predecessor 条件明确要求三类状态的组合。

### 8.32 Merge Queue 与 Post-merge Authorization

批准和完成分别绑定候选与受保护分支真实提交：

```text
approved_base_commit
candidate_commit
merge_commit
merge_tree_digest
protected_branch_identity
merge_queue_identity
pre_merge_gate_set
post_merge_gate_set
post_merge_drift_result
completion_commit
```

只有 merge commit 的 governed tree 与 projected result 一致、post-merge Gates 通过，才能进入 verified/completed。Merge queue 邻接提交导致 claim、closure 或 graph 变化时进入 `verification_failed/blocked`；候选分支 evidence 不能替代主线 completion evidence。

### 8.33 Failure、Containment 与 Repair DAG

失败记录必须可收敛但不可擦除：

```text
failure_id
failed_migration_id
failure_scope
containment_state
repair_migration_id
repair_strategy = rollback | forward_fix
repair_predecessors
repair_completion_evidence
original_resolution = rolled_back | repaired
residual_risk
```

合法序列包括：

```text
cutting_over → verification_failed → contained
contained → repair_pending → repaired
contained → rollback_pending → rolled_back
```

Track closure 可接受失败节点已被 verified repair 完整解决，但保留原失败历史。Repair 继承原节点 claim、安全、持久化和 release reviewers，并使受影响下游重新计算 authorization/invariant。

### 8.34 Approval 与 Evidence Freshness

Gate evidence 按风险定义新鲜度：

```text
issued_at
valid_until
max_age
freshness_basis
refresh_command_id
refresh_required_before_state
external_fact_digest
revocation_event_refs
```

结构性 AST/golden evidence 可有效至输入漂移；security、dependency、platform、license、clean-install 和外部插件证据必须有风险窗口。超过窗口只刷新相关 Gate，不重做无关人工决策。

### 8.35 Temporary Exception Lifecycle

例外和 deferred item 必须引用正式记录：

```text
exception_id
rule_id
exact_import_or_symbol_site
scope_digest
reason
risk_class
owner
approvers
introduced_in_commit
expires_at_or_phase
removal_migration_id
non_expansion_predicate
compensating_controls
verification_command_id
status
```

例外精确到 site，禁止 package-wide wildcard；scope 扩大必须新审批。过期例外阻止相关节点和 closure。Security、permission、persistence 的明确硬不变量不可豁免。`allowed_deferred_items` 只能引用 exception ID。

### 8.36 Evidence Store 可用性与恢复

Completion 引用的 evidence 必须长期可寻址、可验签、可恢复：

```text
evidence_store_id
content_address
replication_policy
retention_until
legal_hold_or_delete_policy
integrity_check_schedule
encryption_key_version
key_retention_policy
restore_test_evidence
availability_class
missing_artifact_policy
```

最小证据集合至少保留至相关 public compatibility、持久数据和审计窗口结束。证据丢失或无法验签不改写历史 execution，但 authorization 转为 `unknown/stale`；高风险后续动作必须重新生成等价证据或取得显式风险裁决。

### 8.37 Release Transaction

Release closure 绑定唯一版本事务：

```text
release_id
version
source_commit
source_tree_digest
build_evidence_id
wheel_and_sdist_digests
artifact_signature_or_attestation
release_manifest_digest
tag_identity
package_index_identity
published_artifact_observation
release_notes_digest
deprecation_obligations_closed
```

实际上传制品必须与已验证制品逐字节一致，版本不可覆盖，tag/commit 唯一，并从索引下载 clean-install。部分上传为 `release_partial_failure`；只能完成剩余不可变制品或发布新版本，不能覆盖已发布版本。

### 8.38 Asset-level Closure Class

每项旧资产独立分类：

```text
closure_class = internal_only | public_deprecation | plugin_compatibility | persistent_reader
internal_complete_conditions
release_complete_conditions
long_lived_compatibility_obligation
removal_release
reader_retention_horizon
```

Internal-only 可直接闭合；public/plugin 经发行窗口闭合；persistent reader 可合法跨越 removal release，但必须有 reader coverage、数据保留 horizon 和最终清理策略。全局状态从资产 closure class 聚合，不假设所有旧形态同时删除。

### 8.39 Emergency Freeze 与 Trust Recovery

信任基础受损时，紧急状态优先于 lease 和既有 approval：

```text
emergency_state = normal | freeze_new_approvals | freeze_all_writes | recovery
incident_id
affected_authority_versions
affected_attestations
freeze_actor
recovery_authority
revalidation_scope
unfreeze_evidence
```

冻结阻止相应新转换；恢复不能由同一受损身份单独批准。历史 evidence 按 issuer 和时间窗标记 suspect，并按范围重验。

### 8.40 Control-plane Journal 与唯一真相源

受保护分支中的 append-only Git control journal 是治理状态转换的唯一授权真相源。本策略明确选择 Git-journal 方案，不保留“外部数据库也可作为同级 truth”的双实现空间。CI attestation、evidence store、lease backend、package index 和状态页只提供不可变附件、并发协调或外部观察，不得独立改变 migration/phase/release 状态。Journal event 至少记录：

```text
control_event_id
control_sequence
authority_id
aggregate_type
aggregate_id
expected_revision
resulting_revision
transition_kind
payload_digest
actor_attestation
source_commit
recorded_at
```

每个 aggregate 的 revision 单调递增；写入使用 expected revision、受保护分支真实 head 和 fencing token 做 CAS。Manifest、状态页和汇总报告均为 journal projection，可由 journal 与内容寻址附件确定性重建。出现双写分叉、缺失 sequence、未知 authority 或 projection digest 不一致时 fail closed；禁止通过直接编辑 projection 绕过状态转换。

### 8.41 原子 Facts Generation

正式 facts 针对单一冻结 Git tree 生成，并通过 generation index 一次发布：

```text
facts_generation_id
source_commit
source_tree_digest
scanner_suite_digest
schema_set_digest
started_at
finished_at
input_file_manifest_digest
component_artifact_digests
generation_complete
```

扫描必须运行在只读隔离 worktree；所有 graph、symbol、identity、packaging、security、dynamic reference 和 repository capability facts 使用同一 generation。任一 component 缺失、scanner/schema 版本不一致、tree 漂移或 generation index 未 CAS 发布，整个 generation 均不可授权。禁止跨 generation 拼接 facts。

### 8.42 Provenance Graph 与失效闭包

每项 artifact、decision、Gate evidence 和 migration 必须进入可反向查询的 provenance graph：

```text
artifact_id
artifact_revision
derived_from_artifact_ids
derived_from_source_digests
consumed_by_decision_ids
consumed_by_gate_evidence_ids
consumed_by_migration_ids
invalidation_rule_id
```

Source、schema、command、trust、time-sensitive external fact 或 authority 变化时，从反向索引计算完整影响闭包并更新 authorization/invariant。无 provenance 的 artifact 不得授权；环、漏边、重复边及跨 generation 边必须由完整性 Gate 和 mutation tests 覆盖。

### 8.43 Gate Applicability 的封闭判定

条件 Gate 的适用性只能由版本化 registry rule 和完整 facts generation 计算：

```text
applicability_rule_id
rule_version
input_fact_kinds
triggered_by_fact_ids
applicability_result = required | not_applicable | unknown
not_applicable_evidence
override_policy
```

`unknown` fail closed。人工只能将 N/A 升级为 required，不能把 required 降为 N/A。Public API、persistent identity、security/command、packaging、lifecycle 和 release 触达必须联合扫描 facts 与 claimed assets 判定；migration author 不得自行填写自由文本 N/A。

### 8.44 治理 Identity Policy

Symbol、artifact、decision、migration、evidence、exception、release 和 control event 共用版本化 ID 政策：

```text
id_namespace
id_kind
id_value
allocation_authority
allocated_at_revision
aliases
predecessor_ids
successor_ids
tombstone_status
reuse_forbidden
collision_check
```

ID 一次分配、与可变路径/名称无关、删除后永久 tombstone 且不可复用。Rename 只更新 canonical name/history；split/merge 显式记录 predecessor/successor，默认不保持同一 identity。分支可分配高熵 ID，但合入时仍需 journal revision CAS 和全局碰撞检查。

### 8.45 Repository Object 与 Capability Profile

正式扫描以 Git tree entry 为对象身份，filesystem 只作受验证 materialization。Repository profile 至少记录：

```text
case_sensitivity
unicode_normalization
symlink_policy
hardlink_observation_policy
submodule_policy
lfs_policy
sparse_checkout_policy
shallow_clone_policy
file_mode_policy
repository_root_identity
```

大小写或 Unicode 等价碰撞、symlink 越界/循环、无法验证的 submodule/LFS、sparse/shallow 导致的不完整 tree 以及平台不可表达 mode 均 fail closed 或显式 `unsupported`。Case-only/Unicode-only rename 作为特殊原子操作验证；不得把 LFS pointer 或 submodule gitlink 当普通文件内容。

### 8.46 Structured Command Registry

正式命令保存结构化执行描述：

```text
executable_identity
argv
working_directory_id
environment_allowlist
stdin_policy
timeout
network_policy
expected_outputs
```

Runner 使用无 shell 的 argv 执行，禁止将 manifest、路径、symbol 或用户输入拼接成 shell 字符串。确需 shell 语义时必须进入 `shell_command_boundary`：引用固定脚本 artifact digest、无自由参数拼接、触发 security reviewer，并对脚本及结构化调用计算 command digest。展示文本不构成可执行输入。

### 8.47 可信时间与偏差政策

Freshness、lease、exception、trust 和 release window 使用统一时间契约：

```text
trusted_time_source
observed_at_authority
max_clock_skew
monotonic_duration_source
wall_clock_timestamp
expiry_comparison_rule
uncertain_time_policy
```

Lease 依赖权威 backend revision/fencing 和单调时长，不单独相信 runner wall clock；attestation 时间由可信 issuer 验证。时间源不可用或偏差超限时，新 approval/cutover fail closed，但已授权的紧急 containment 可执行并必须补记 journal evidence。

### 8.48 Active Authority 升级协议

Scanner、graph、Gate predicate、invalidator、reconciler、validator 或 trust verifier 的升级属于 control migration：

```text
control_upgrade_id
old_authority_version
new_authority_version
dual_read_comparison
shadow_evaluation_scope
decision_diff_report
false_allow_count
false_deny_count
activation_commit
rollback_boundary
post_activation_monitor
```

新版本先在冻结 generation 上 shadow evaluation；任何新增 allow 必须逐项解释。随后进入无授权能力 canary，最后通过 journal event 原子切换唯一 active authority。不可逆 schema 或 transition 已写入后禁止简单回滚 binary，必须 forward repair。任一时点只能有一个 authority 产生 production authorization。

### 8.49 Manual Ruling 的封闭权限

人工裁决只能属于：

```text
manual_ruling_kind = classification | provenance_resolution | risk_acceptance
scope
cannot_override_rule_ids
required_reviewers
expires_at
replacement_evidence_plan
```

人工可补充语义 owner、解释静态不可解析引用，或限时接受非硬性 availability 风险；不能伪造测试/发布成功、把 required Gate 降为 N/A、忽略签名或 provenance 失败、覆盖 security/permission/persistence 硬不变量，也不能把缺失制品视为已发布。Risk acceptance 必须限时、有替代证据计划，并进入 exception、provenance 和 journal。

### 8.50 Git Journal Integrity Policy

Git 的内容寻址不等于 append-only。Journal ref 必须冻结：

```text
journal_ref
genesis_commit
expected_ancestor
branch_protection_policy_digest
force_push_forbidden
ref_deletion_forbidden
required_commit_or_tag_signatures
journal_path_codeowners
required_independent_reviewers
linearization_policy
mirror_refs
backup_frequency
history_rewrite_detection
admin_bypass_policy
```

Validator 验证 genesis 到当前 head 的连续祖先链、签名、sequence、review 和 protection policy digest。Journal 使用 merge queue 线性化；journal path 需要独立 CODEOWNERS 审批。远端镜像和 sealed backup 保存独立 ref。检测到 force-push、ref deletion、ancestor 断裂、签名失败或未登记管理员 bypass 时立即 `freeze_all_writes`；历史恢复和解冻必须由独立 recovery authority 批准并记录新 journal event。

### 8.51 Merge 与 Completion 的可恢复协议

代码 merge、post-merge evidence 和 completion event 不是单一原子事务，不得表述为同时提交。合法协议状态为：

```text
prepared
candidate
merged_unverified
verification_attached
completed
verification_failed
contained
```

每次协议记录：

```text
expected_source_ref
expected_journal_ref
candidate_commit
merge_commit
merged_at
verification_attachment_ids
reconciliation_deadline
timeout_containment_policy
completion_event_id
```

`merged_unverified` 是正式可观察状态：生产代码可能已进入主线，但不得授权任何下游节点或 Release Closure。Post-merge verification 成功后先发布内容寻址附件，再以 expected journal revision 提交 completion event。失败、附件缺失或超过 reconciliation deadline 自动进入 containment；reconciler 可幂等恢复，不能把中间状态推断为 completed。

### 8.52 Control-plane MVP 与非能力边界

MVP 的可执行输入、逐步退出条件、CLI 最小契约和禁止能力由
[`PYTHON_PACKAGE_GOVERNANCE_MVP_EXECUTION.md`](./PYTHON_PACKAGE_GOVERNANCE_MVP_EXECUTION.md)
规范。本文仍是授权边界与硬不变量的上位规范；MVP 文档只能收窄实现范围，不能开启 G6、真实试点、Production cutover 或 Release Closure。两者冲突时取更严格规则，并登记 drift 后修复，不能选择宽松解释继续执行。

控制面按最小闭环顺序实现：

```text
MVP-1  撤销 legacy authority
MVP-2  验证 canonical journal、integrity、schema 和 ID references
MVP-3  为一个冻结 Git tree 生成原子 static-facts generation
MVP-4  计算 provenance 与 G1～G5；无 production authorization
MVP-5  通过 negative、rebuild、determinism、soundness 和 crash fixtures
MVP-6  完成 trust、lease、post-merge、repair 与 operations 后启用 G6
```

每项 MVP 必须有 executable demo、机器测试、输入/输出 artifact 和显式 non-capabilities。未实现的 scanner、Gate、repository capability 或外部 evidence 类别标为 `unsupported`，不得降为 N/A。MVP-1～MVP-5 的 authority 永远不能产生 production approval/cutover/completion；G6 默认硬关闭，只有 MVP-6 的独立 activation event 才能开启。

### 8.53 控制面容量预算与性能 SLO

每个基准 generation 记录：

```text
repository_entry_count
symbol_count
artifact_count
journal_event_count
evidence_bytes
full_scan_duration
incremental_scan_duration
provenance_recompute_duration
journal_replay_duration
projection_rebuild_duration
peak_memory
ci_concurrency
```

具体阈值由基线 artifact 批准，不在需求中虚构固定数字；但必须分别定义 blocking SLO、warning budget 和容量上限。允许缓存与增量计算，正式授权结论必须能由无缓存全量重建得到相同 digest；缓存命中只影响性能，不得改变结论。超过 blocking SLO 或容量上限时禁止新 production authorization，并进入容量修复节点，不能靠跳过 Gate 降时。

### 8.54 Snapshot、Archive 与 Reachability GC

Journal/provenance snapshot 只用于加速重建：

```text
snapshot_id
through_control_sequence
state_digest
covered_event_range
archive_location_and_digest
genesis_replay_evidence
attachment_reachability_roots
gc_candidate_set
gc_proof
minimum_retention_horizon
```

Journal event、genesis、ID tombstone 和仍被 active/public/persistence/audit roots 引用的附件不得删除。只有超过 retention、从全部 reachability roots 不可达且有机器 GC proof 的附件可清理。Snapshot 不替代原始历史；定期从 genesis 和最近 snapshot 各自重建并比较 state digest。

### 8.55 Governance Operations RACI

运维清单至少覆盖 journal/validator/CLI、evidence store、lease backend、trusted time、dependency/security/license refresh、emergency recovery、过期 exception 和 partial release：

```text
component
service_owner
backup_owner
responsible_role
accountable_role
consulted_roles
informed_roles
response_channel
severity_model
response_target
recovery_target
runbook_id
restore_drill_frequency
escalation_authority
```

Owner 缺失、runbook 不可执行或恢复演练过期时，相应 production Gate 不可授权。职责分离适用于日常恢复和 emergency recovery，不因系统基于 Git/CI 而省略。

### 8.56 Degraded-mode Matrix

对 Git hosting、CI、evidence store、lease backend、trusted time、package index 和镜像分别定义：

```text
dependency
failure_mode
read_current_state
new_approval
new_cutover
containment
rollback
evidence_buffering
maximum_degraded_duration
reconciliation_after_recovery
```

默认禁止新 approval/cutover；允许 freeze、containment 和经预授权且不扩大风险的 rollback。只有已验证本地副本可用于只读诊断。离线 evidence 标记为 pending attachment，恢复后按原始时间与 runner identity 验证，不能事后伪装为在线 attestation；超过最大降级时长升级 incident。

### 8.57 Bootstrap Genesis Ceremony

Phase -1B0 的创世动作至少由两个独立角色在干净环境完成：

```text
bootstrap_ceremony_id
participants_and_separation
input_digests
independent_rebuild_results
genesis_event
genesis_commit_and_tag_signatures
sealed_backup_locations
mirror_observation
activation_observation
```

双方独立重建 canonical digest 并一致后才能签署 genesis commit/tag。镜像和 sealed backup 必须验证可恢复；后续 authority、journal event 和 trust rotation 均可追溯到 genesis。创世参与者不能单人同时承担 activation 与 recovery authority。

### 8.58 Scanner Soundness Contract

每个 scanner 发布：

```text
supported_python_syntax_and_versions
soundness_claim
known_blind_spots
unknown_emission_rules
false_negative_corpus
false_positive_baseline
mutation_operators
corpus_version
```

关键 Gate 优先保证不漏报；无法可靠解析时输出 unknown。固定语料覆盖 alias、relative import、`TYPE_CHECKING`、annotation string、dynamic loader、re-export、module-qualified identity、generated source 和支持的 Python 语法差异。Scanner 升级必须重跑语料和 mutation tests；新增 false negative 是 hard failure，false positive 只能通过精确基线和修复计划管理。

### 8.59 规范文档分层与 Rule ID

本文件保留治理目标、硬不变量、授权边界和规范索引。实现时可拆分：

```text
governance-core
governance-control-plane
governance-schema-registry
governance-gates
governance-operations
governance-release
```

拆分不是当前生产代码或包迁移授权。每条规范规则只在一个 owner 文档定义并分配 stable rule ID；其他文档只引用 rule ID，不复制正文。主策略维护规范依赖图、owner、版本与冲突优先级；引用失效、重复 owner 或循环规范依赖 fail closed。

---

## 9. 迁移协议

每个包治理切片统一采用三段协议。

## 9.1 Prepare

- 冻结当前事实和工作树基线；
- 确定源符号、目标 owner 和目标模块；
- 计算完整 production/test consumer closure；
- 检查目标层是否合法；
- 冻结持久身份、公开 API 和 golden fixture；
- 列出同切片需要删除的旧导出；
- 分类 public/internal/experimental/private API 并批准 compatibility/release policy；
- 建立有限 behavior contract、characterization fixture 和 differential/invariant test plan；
- 识别 package data、entry point、`importlib.resources`、可选依赖和资源相对路径影响；
- 生成精确测试集，并冻结 determinism/retry/quarantine policy 与支持平台矩阵；
- 冻结 resolver input、依赖 manifest、build backend 和 artifact evidence 要求；
- 计算 target readiness 和适用的性能/资源非回归预算；
- 批准 migration record。

Prepare 不修改生产定义。

## 9.2 Atomic cutover

在一个可评审提交中完成：

- 移动唯一生产定义；
- 更新全部消费者；
- 更新领域 facade、manifest 和架构矩阵；
- 更新 composition root；
- 删除旧定义、旧导出和空目录；
- private/internal 不增加兼容转发；public 只按已批准 release manifest 增加有期限的 deprecation shim。

## 9.3 Verify

- AST 旧 import path 归零；
- 源符号在旧模块归零；
- 新依赖边符合 manifest；
- 公开 API snapshot 符合批准决策；
- golden fixture 和持久身份不变；
- 目标子系统及直接依赖测试通过；
- 工作树不存在未登记的迁移残渣。
- 涉及包路径、公开 import、entry point 或非 Python 资源时，构建 wheel/sdist，在干净临时环境安装并验证 import、CLI、资源读取和 public API snapshot。
- behavior characterization、differential 和 invariant evidence 满足节点契约；
- 支持平台/依赖矩阵、测试确定性和性能资源预算按适用范围通过。
- pre-merge evidence 仍在 freshness 窗口内，候选提交进入受保护分支后，post-merge Gate 在真实 merge commit 上通过；
- 所有适用例外均为有效、未扩张状态，引用证据可寻址、可验签且满足 availability policy；
- 每项 touched asset 已声明 closure class 并达到本节点要求的 internal/release/reader 条件；
- `emergency_state=normal`；涉及发行时，release transaction 已绑定唯一版本、制品、tag、commit 和索引观察。

Atomic cutover 合入后先记录 `merged_unverified`，不能直接标记完成。只有 Verify 在受保护分支真实提交上完成、verification attachment 已内容寻址发布且 completion journal event 以当前 expected revision 提交后，切片才能记录 `execution_state=completed`。该历史状态不可回退；后续证据过期、输入漂移或不变量破坏分别更新 authorization/invariant，并按需创建 repair 节点。

### 9.4 回滚规则

回滚按影响分类：

- source rollback：尚未产生下游依赖和持久事实时，可整体回滚 cutover commit；
- data rollback：只有存在经过测试的逆向 migration 且不会丢失事实时才允许；
- release rollback：撤回或替换已发布制品，并保留版本不可变原则；
- forward fix：涉及已写入持久数据、已发布 public API、外部插件消费或不可逆副作用时的默认方式。

下游切片开始后，源码依赖必须按 migration DAG 逆序处理。禁止只恢复旧 facade、建立双真相源或用永久 re-export 假装回滚成功。

任何 `verification_failed` 必须先进入 contained，再由显式 rollback 或 forward-fix repair 节点收敛。原失败记录不可删除或改写为成功；只有 repair/rollback evidence 验证完成、受影响资产不变量恢复且所有下游 authorization 重算后，才可解除阻塞。

---

## 10. 可执行治理路线

### Phase -1A：撤销旧治理授权

- 将旧 `Phase 0A = verified` 标为 `untrusted_legacy`；
- 禁止硬编码 coverage、空 manifest 和 stale decision 参与批准；
- 暂停批量生产 cutover；
- 旧产物只保留作审计；
- 建立 authority revocation 清单：

```text
legacy_authority_id
legacy_entrypoints
legacy_manifest_paths
legacy_ci_jobs
legacy_status_fields
revocation_commit
replacement_authority_id_or_none
audit_retention_paths
negative_tests
```

退出条件：Negative tests 证明旧 CLI、phase gate、CI job 和 manifest 无法产生 `approved/cutting_over/completed`；旧文件只能被新 reader 作为 `untrusted_legacy` 审计输入。

### Phase -1B0：冻结 Bootstrap Envelope 与 Trust Policy

通过第 8.57 节双人 bootstrap ceremony，冻结 canonical bootstrap envelope、encoding、schema reference、唯一 active authority ID、Git control journal、journal integrity policy、治理 ID policy、trusted time policy 和 trust policy。Bootstrap 层没有 production 授权能力。

建立唯一机器边界权威 `package-boundaries.toml`，只定义 layers、domains、capabilities、allowed/forbidden edges、facades、dynamic loaders 和 temporary exceptions；实际 import 边由 AST 工具生成，不在 manifest 中手工复制。Schema 的物理目录可以在实现评审时确定，但不得分散出多份相互竞争的边界清单。

退出条件：Bootstrap envelope、canonical encoding、journal event/integrity schema、ID/time policy 和 trust policy 已由独立角色重建并签署；genesis commit/tag、镜像、sealed backup 和 activation observation 完整；projection 不能独立授权。

### Phase -1B1：实现最小 Hermetic Validator

实现只依赖标准库、只读、不能批准 production cutover 的 validator，只验证 schema、canonical encoding、引用完整性、版本兼容、authority revocation、journal sequence/revision、ID tombstone 和 structured command descriptor。

退出条件：Validator 不 import `mote` 或旧治理工具，并通过 journal ancestor/signature/protection、history rewrite、projection、revision CAS、ID collision/tombstone、command injection 和正反向 mutation conformance fixtures。

### Phase -1B2：迁移并验证 Control Manifests

版本化并迁移 journal、facts generation、provenance、classification、decision、migration、track、evidence、exception、API/release、identity、lifecycle、dynamic loading、repository capability 和 packaging schema；建立 expand/contract 与混合版本规则。

退出条件：全部 control manifests 由 B1 validator 验证；旧产物只作审计；所有 schema 有兼容和升级策略；只完成 MVP-2 范围且 G6 硬关闭。

审批角色、actor 信任、claim/conflict graph、CAS 状态转换、evidence 脱敏、canonical encoding、原子写入和并发生成均属于 schema 冻结范围，不能推迟到试点后补充。

### Phase -1C：建立 Hermetic CLI

至少提供真实存在且文档一致的命令：

```text
governance discover
governance graph
governance classify-check
governance decision-check
governance migration-check
governance evidence-check
governance release-check
```

正式 CLI 必须复用 B1 的解析/验证库，不建立第二套语义，并只通过 structured command registry 调用外部命令。退出条件：最小环境中不 import `mote` 即可稳定执行；所有命令符合稳定 CLI contract、无 shell 注入且通过 conformance test；MVP-1～MVP-5 的 CLI 明确拒绝 G6 和 production transition。

### Phase -1D：统一扫描器与图

先冻结 repository capability profile，并枚举整个可安装/可发布 Git tree，把每个 entry 分类或加入 `excluded_with_reason`。随后建立 module/import、re-export、symbol、dynamic/string reference、identity/event/error/discriminator、lifecycle/registry/queue/journal、public/plugin API evidence、security touch、packaging/resource 和 generated/vendored provenance 候选扫描，并生成唯一 Gate Registry、原子 facts generation 与 provenance 反向索引。

所有工具共享 module normalization 和 edge model：

```text
source_module
target_module
source_line
edge_kind
runtime_or_type_only
static_or_dynamic
via_facade
resolution_status
```

SCC 分别报告 `runtime_initialization_scc`、`type_dependency_scc` 和 `domain_package_scc`，不得混为一个指标。

退出条件：所有发布相关 Git tree entry 已枚举、分类或显式排除；unknown/unsupported 显式可见；图算法和解析语义唯一；不存在跨 generation 混用；每项 artifact 均有完整 provenance；Gate applicability 可重复计算；scanner soundness contract、固定语料和 mutation test 通过。此阶段只完成 MVP-3～MVP-5，G6 与 production authorization 保持硬关闭。

### Phase -1E：全部治理来源与当前状态对账

先生成治理需求来源 inventory，再对全部曾具有规范性或授权能力的文档、ADR、发布/兼容计划、专项治理和五层旧计划逐项分类：

```text
source_document_id
source_digest
normative_status
action_id
affected_assets
current_fact_refs
disposition
superseding_decision_id
residual_obligation
```

Action disposition 为：

```text
planned
already_implemented_unverified
implemented_verified
partially_implemented
obsolete
superseded
still_required
```

退出条件：不存在会被重复执行的旧动作；已实施未验证项进入补验收节点。

### Phase -1F：建立全局 Cutover DAG

五层作为 Track，每个节点至少记录：

```text
cutover_id
owner_track
source_layer
target_layer
depends_on
source_symbols
target_symbols
target_owner_readiness
public_api_composition_owner
packaging_owner
release_reviewer
consumer_closure
identity_fixtures
api_policy
test_plan
packaging_plan
source_kind_plan
security_touch_plan
rollback_or_forward_fix
cleanup_node
required_predecessor_execution_state
required_predecessor_authorization_state
required_predecessor_invariant_state
closure_class_by_asset
repair_or_rollback_nodes
```

每个 Phase/Track 同时引用 8.25 的 artifact contract，绑定同一完整 facts generation、provenance root、journal revision、reconciliation 和 Gate Registry。退出条件：DAG 无环；每个节点 owner、目标和 cleanup 唯一；projected graph 合法；所有 required input artifact 当前有效，适用性无 unknown，失效闭包可从反向索引重建。

### Phase -1G：试点域冻结与执行

试点首要条件是 closure 可穷举且风险低，模块数只作辅助上限。试点预算必须明确：

```text
max_changed_modules
max_changed_symbols
max_direct_consumers
max_claimed_hotspots
max_public_api_changes = 0
max_persistent_identity_changes = 0
max_lifecycle_resources = 0
max_packaging_entries
```

同时不得涉及 Task、PTY、后台资源或 breaking release，API 必须已分类。

试点依次完成事实冻结、G1～G6、prepare attestation、atomic production cutover、merge queue/post-merge verification evidence 和 completion attestation。每项 Gate evidence 必须满足 freshness，completion 必须绑定受保护分支真实提交，且执行期间 `emergency_state=normal`。故障注入必须在隔离 fixture/repository/worktree 中执行：

```text
stale_digest
unknown_reference
conflicting_owner
missing_predecessor
claim_conflict
invalid_attestation
expired_lease
clock_skew_or_time_source_loss
partial_manifest_write
mixed_facts_generation
missing_provenance_edge
gate_applicability_unknown
id_collision_or_tombstone_reuse
repository_capability_mismatch
manifest_command_injection
authority_upgrade_false_allow
cutover_commit_without_evidence
verification_failure
runner_crash_before_and_after_CAS
```

退出条件：每项证明 fail closed，并经 reconciliation 回到预期 execution/authorization/invariant 组合；失败节点由 verified repair/rollback 收敛且历史保留；真实 facts、人工 decisions 和 production source 无未登记漂移。真实低风险试点在本条件及第 13 节新增控制面条件关闭前不获授权。

### Phase 0：扩展可信事实到全仓

- 所有发布相关文件已枚举、分类或显式排除且说明理由；
- 所有受支持候选类别完成枚举；
- unsupported 和 unknown 显式记录；
- 首批 DAG 节点通过 Classification、Identity、Ownership 和 Closure Gate；
- 其他域保持明确 `unreviewed`；
- 不使用笼统的 facts 100%。

### Execution Waves：按 DAG 拓扑执行

风险波次建议为：

1. 叶子契约和低风险 internal move；
2. Kernel/Runtime 语义边界；
3. Runtime/Orchestration 的单实体机制与多实体策略边界；
4. Product composition、provider、LSP、Toolset、Skills、Paths 和 Config；
5. Session/replay、event/error/discriminator、Tool identity、public/plugin API 等高风险资产。

波次不构成整层 barrier，节点只服从 DAG predecessor。

### Internal Closure

聚合 `closure_class=internal_only` 资产：旧 import/path、双实现、双真相源、临时例外和文档漂移归零；目标图满足 SCC disposition；wheel/sdist clean-install 通过。Public/plugin shim 和持久 reader 不计为 internal 残渣，但必须存在于各自已批准 closure record 中。达到 `internally_complete`。

### Release Closure

按资产 closure class 完成 deprecation release、兼容窗口、breaking/removal release、public/plugin 旧路径清理、迁移工具、release notes 和外部兼容证据；persistent reader 达到 reader coverage、retention horizon 与最终清理策略要求。唯一 release transaction 必须绑定 source commit、版本、tag、已验证制品及索引端实际观察，并通过下载后的 clean-install。部分发布失败不构成 closure。全部资产各自完成后才聚合为 `release_complete`。

### 10.1 文档权威与同步节奏

治理期间的权威顺序为：

1. 源码和通过的架构测试描述当前事实；
2. 已批准的 governance manifest 描述当前迁移裁决；
3. 本策略描述治理规则和目标约束；
4. `ARCHITECTURE.md` 描述已落地架构，不能保留已删除路径作为当前事实。

`ARCHITECTURE.md` 必须在 Phase -1 先修正明显过时的层级和路径，并在每个 cutover 同步相关章节和快速定位表。Internal Closure 只做全局一致性验收，不是第一次更新架构文档。

### 10.2 架构指标与停止条件

每个阶段记录：

```text
upward_edges
scc_count
largest_scc
unresolved_owner_count
multi_owner_count
unknown_reference_count
unregistered_dynamic_import_count
cross_domain_private_access_count
unclassified_public_api_count
unclassified_lifecycle_resource_count
bare_generic_count
unbounded_any_count
unscoped_type_ignore_count
legacy_path_count
migration_exception_count
overdue_exception_count
```

指标原则是“不得恶化 + 当前阶段目标下降”，不以 LOC、文件数或目录数作为成功指标。任何 upward edge、未登记动态加载、相关域 unknown reference、未分类 public API、裸泛型新增或逾期例外都会停止当前切片。

Coverage 只报告第 8.1 节定义的真实 numerator/denominator；不得把 `unsupported`、unknown、conflicted、空占位 manifest 或仅 discovery 到候选计为完成。

### 10.3 Track/Closure 统一出口

每个 Track 或 closure 状态只能在机器验证下列条件后完成：

```text
all_member_cutovers_terminal
all_member_execution_states_resolved
all_failed_members_have_verified_repair_or_rollback
all_required_authorizations_current
all_required_invariants_satisfied
all_predecessor_state_triplets_satisfied
control_journal_consistent_and_projection_rebuildable
single_complete_facts_generation
provenance_invalidation_closure_complete
all_gate_applicability_known
no_overdue_exception
no_expanded_or_unverifiable_exception
no_new_upward_edge
no_unresolved_scc_disposition
all_touched_unknown_references_resolved
all_touched_public_api_classified
all_touched_identities_verified
all_required_distribution_smokes_passed
all_gate_evidence_fresh_and_available
post_merge_completion_bound_to_protected_branch_commit
all_assets_meet_declared_closure_class
release_transaction_complete_if_applicable
trusted_time_within_skew_policy
single_active_authority_version
emergency_state_normal
documentation_current
```

每个 Track scope manifest 必须声明 `entry_gates`、`member_cutover_ids`、带三状态要求的 `required_predecessors`、`control_journal_revision`、`facts_generation_id`、`provenance_root_ids`、`exit_gates`、仅引用正式 exception ID 的 `allowed_deferred_items`、`forbidden_residuals`、`verification_commands`、`evidence_artifacts`、`closure_class_by_asset` 和 `completion_authority`。

`all_member_cutovers_terminal` 不表示“从未失败”：`completed`、`rolled_back`、`aborted` 或被 verified repair 完整解决的失败历史均可成为已解析执行结果；未 contained 的失败、仍在 `cutting_over/repair_pending/rollback_pending` 的节点或 invariant 为 `violated/unknown` 的资产不能闭合。历史 `completed` 不因 authorization 过期而改写，但新 closure 必须使用当前 authorization 和 satisfied invariant。最终 closure 不能接收前序 Track 遗漏的生产迁移。

---

## 11. 自动化验收

### 11.1 分层门禁

- Contracts 不依赖其他层；
- Kernel 只依赖 Contracts；
- Runtime 不依赖 Orchestration/Product；
- Orchestration 不依赖 Product；
- 禁止函数、方法和类体内 import；
- 禁止新增 `common/shared/utils/misc/helpers` 包；
- 禁止通过测试专用路径掩盖生产违规。
- 合法动态加载必须与 manifest 一致。

### 11.2 同层依赖门禁

- 层间反向边默认禁止；
- 同层跨 domain 使用 domain/package 级允许图；
- domain 内不手工复制逐模块 allowlist，只检测 SCC、私有穿透和 facade 回流；
- 实际模块依赖图由 AST 工具生成，不在 manifest 手工重复；
- 临时边必须精确到 import site，并有 migration ID 和到期阶段；
- 不允许可增长的包级白名单。

### 11.3 Owner 门禁

- 每个模块和公开符号只有一个 owner；
- 每个事件 tag、错误 code、Tool identity 只有一个定义；
- 每个 registry、queue、journal 和 lifecycle resource 的 type owner、factory owner、instance owner 与 close authority 均已登记且各自唯一；同类型的多个合法 scoped instance 不视为 multi-owner；
- Projection 必须记录其权威来源；
- Protocol 必须有真实 consumer 和 implementer。

### 11.4 迁移门禁

- migration 必须处于 approved 状态才能 cutover；
- consumer closure 在扫描器声明的可信范围内必须完整，相关 `unknown_reference` 必须清零或经显式人工裁决；
- 目标层必须不高于最低消费者允许层；
- 旧路径、旧导出和旧定义必须同切片删除；
- golden fixture 和 persistent identity 必须验证；
- 未登记工作树漂移阻止验收；
- 当前 legacy `Phase 0A = verified`、空 cutover manifest 和硬编码 coverage 不满足任何迁移前置条件。

### 11.5 测试范围

每个切片至少运行：

1. 目标包测试；
2. 直接生产消费者测试；
3. 架构测试；
4. import/public API snapshot；
5. 涉及持久模型时的 replay/codec/golden fixture；
6. 涉及 Task/PTY/kernel 时按仓库约定进行单 event-loop 隔离测试；
7. 涉及包路径、entry point 或资源时执行 wheel/sdist clean-install smoke；
8. 执行 Pyright；切片触及的公开边界不得保留新裸泛型、无界 `Any` 或无理由 ignore。

测试证据按切片建模，禁止给每个 symbol 复制同一条全量命令：

```text
architecture_checks
owner_package_tests
direct_consumer_tests
identity_golden_tests
distribution_smoke
optional_extra_matrix
```

路径和测试候选来自当前 consumer graph，再由评审批准。不存在的旧测试路径、无法收集的命令或未绑定当前 digest 的历史测试记录均不能作为证据。

### 11.6 Hermetic 架构测试

静态治理测试必须：

- 以文件路径或隔离模块加载 scanner/validator，不执行根 `mote/__init__.py`；
- 不导入 Product composition、Toolsets、TUI、PTY、provider SDK 或 optional backend；
- 在标准库和 pytest 的最小环境中完成 AST、manifest、digest 和 dependency gate；
- 提供独立、稳定的 CLI 子命令，文档命令与真实 parser 一致；
- 将静态 architecture job 与 Runtime import smoke 分开；
- 将 public API clean-install smoke 放在核心发行依赖环境；
- 将 optional extra 按矩阵单独验证。

pytest 收集因 `pyte`、provider SDK 或 Product 初始化循环失败，视为静态治理门禁不 hermetic，不能解释成架构通过或跳过。

### 11.6.1 根 Public Facade Import Budget

测试隔离不能替代修复公共 facade 的 eager-import 风险。必须单独批准 `import mote` 的 core/optional dependency 契约和 import budget：

- 允许加载的模块集合；
- 是否允许读取配置或用户目录；
- 是否允许创建 registry；
- 缺少 UI/PTY/provider optional extra 时是否必须可导入；
- 最大 import latency；
- 新建线程、async task、socket 和文件写入预算。

使用独立 subprocess smoke 记录 loaded modules、线程、task、socket、文件系统副作用和耗时。若 public API 承诺 core 环境下可 `import mote`，Product eager import 必须满足该预算，不能只让 architecture test 绕开根 facade。

### 11.7 代码整洁门禁

对切片 touched closure 强制检查：

- Black、Isort 和 Pyright；
- 顶部 import 和 import canonicality；
- 裸泛型、无界 `Any`、`dict[str, object]` 边界逃逸；
- 无错误码/原因的 type ignore；
- `getattr`/`hasattr` 私有边界穿透；
- `__all__` 与 public API manifest 一致；
- Protocol 不泄漏实现类型；
- Pydantic 配置、RoleState 和生命周期对象没有混装；
- 新增 exception、event、ID 和 tagged union 满足其领域 identity 规则。

现有问题采用精确基线，不允许全仓泛化豁免。触及某个违规边界时必须在同切片修复，除非修复会改变业务语义；后者需要独立 migration ID 和明确前置阶段。

---

## 12. 评审清单

每个包治理 PR 必须回答：

### Owner

- 被移动能力的一句话 owner 是什么？
- 为什么当前包不是正确 owner？
- 目标包是否已经拥有相同状态或生命周期？

### Dependency

- 移动前后的依赖边是什么？
- 是否产生新的同层边或循环？
- 是否存在隐藏的动态/类型依赖？
- 低层需要的能力是否通过窄 Port 注入？

### State

- 状态真相源在哪里？
- 是否涉及事件 tag、错误 code、discriminator 或存储 key？
- Projection/cache 是否仍可重建？
- 是否错误地把进程对象放入可恢复状态？

### Lifecycle

- 谁构造、共享和关闭资源？
- cancellation 和失败时如何释放？
- 测试之间如何隔离？

### Migration

- consumer closure 是否完整？
- 旧路径是否同切片删除？
- 是否新增兼容转发？Private/internal 一律拒绝；public 仅允许 manifest 已批准、带 removal version 和发行测试的 deprecation shim。
- 验证命令和证据是什么？
- 是否引入裸泛型、无界 `Any`、未限定 ignore、非 canonical import 或反射边界穿透？

---

## 13. 生产 Cutover 的批准条件

任何节点进入 `approved` 前必须同时满足：

- source/target symbol 与 owner 唯一；
- approval 基于唯一 active authority 的 Git control journal 当前 revision，所有 projection 可从 journal 重建且无分叉；
- 所有输入来自同一 `generation_complete` facts generation，source tree、scanner suite 与 schema set digest 一致；
- decision、Gate evidence、migration 和附件均进入 provenance graph，反向失效闭包完整；
- Gate applicability 由版本化 rule 计算，无 unknown，人工未将 required 降为 N/A；
- 所有治理 ID 通过 namespace/collision/tombstone 检查，rename/split/merge 映射明确；
- repository capability profile 与实际 Git tree/materialization 一致，无未裁决 symlink、case/Unicode、submodule、LFS 或 sparse/shallow 风险；
- source、signature、closure 和 identity digest 当前有效；
- 可安装/可发布 touched files 均有 source kind、owner 或显式排除理由；
- 所有 `unknown_reference` 已清零或有显式裁决；
- target owner 已准备接收；
- projected graph 无反向边和未处置新增 SCC；
- API visibility/stability 和 release policy 已批准；
- identity fixture 已建立或明确 N/A；
- lifecycle、instance owner 和 close authority 已确认；
- 必需审批角色均已由可信 identity 签发且满足职责分离；
- 触及根 public facade/build metadata 时，Public API / Composition、Packaging 和 Release owner 已批准；
- generated/vendored 内容的 authority、regeneration 或 provenance/license evidence 已完成；
- security touch 已按调用语义和信任边界分类并通过必需 reviewer；
- claim/conflict graph 证明当前节点可独占或安全并行；
- base commit、CAS 前置状态与 reconciliation/idempotency 信息完整；
- behavior contract、differential harness 和测试确定性策略已批准；
- 支持版本/平台/extras 矩阵与 dependency resolver evidence 已冻结；
- Protocol signature/structural closure、event/error compatibility 和 deprecation contract 按适用范围完成；
- target readiness 由证据计算，热点性能/资源预算已建立；
- test、packaging 和 evidence plan 已批准；
- rollback 或 forward-fix 策略明确；
- predecessor 已达到节点声明的 execution、authorization、invariant 三状态组合；
- phase artifact contract 当前有效，G1～G6 的适用 Gate evidence 完整；
- 适用 Gate evidence 在风险定义的 freshness 窗口内，且可寻址、可验签、可恢复；
- 所有适用 temporary exception 有精确 scope、owner、期限、non-expansion predicate 和移除节点，且未过期、未扩张；
- 每项 touched asset 已声明 closure class 和本节点必须满足的 closure 条件；
- 正式 workspace lease/fencing 有效，runner trust policy 未过期且未撤销；
- 所有 command evidence 来自 structured argv registry；不存在未经批准的 shell boundary 或自由字符串执行；
- trusted time 可用且 clock skew 在政策范围内；
- active authority 未处于未完成升级，或升级已完成 shadow/canary、diff 审查与原子 activation；
- manual ruling 属于封闭类型、未越过 `cannot_override_rule_ids`、未过期且有 replacement evidence plan；
- `emergency_state=normal`，且批准记录明确其在 merge queue 后必须重新验证的 Gate；
- 涉及发行时，唯一 release transaction、partial-failure 处置和索引端 clean-install 计划已批准。

全局生产治理只有在以下基础设施条件全部满足后才能获批：

1. 旧 Phase 0A、硬编码 coverage 和空 manifest 已从授权链移除；
2. governance schema 和 evidence 模型已冻结；
3. hermetic CLI 可在最小环境运行；
4. 图工具共享唯一 edge/module 语义；
5. API、identity、lifecycle、dynamic loading 和 packaging 有真实候选清单；
6. 五层旧治理计划已完成当前状态对账；
7. 五层已改为 Track，并建立全局 Cutover DAG；
8. migration 状态机支持 verification failure、不可变历史和 Git/CI attestation；
9. internal closure 与 release closure 已分离；
10. touched closure 和类型治理范围已冻结；
11. Runtime SCC 已按统一图语义得到 disposition；
12. 试点完整通过并证明 drift、unknown 和失败测试会 fail closed；
13. 首批节点完成事实 Gate、projected graph、精确测试和发行审查；
14. 每个节点可以独立批准、执行、验证和失败处置；
15. 审批角色、职责分离和 actor 信任来源已经机器化；
16. Claim/conflict graph 与并行调度规则已经建立；
17. 状态转换具备 idempotency key、CAS 前置状态和 crash reconciliation；
18. Role/module-qualified 持久身份专项已冻结并有兼容 fixture；
19. 配置 mutability、generation、pinning 和 reload owner 已分类；
20. Facts/evidence 的 secret、用户内容和本机路径脱敏策略已执行；
21. Canonical encoding、原子写入和并发生成规则已冻结；
22. Role 的普通 Python 基类当前事实与目标不变量已统一；
23. 根 public facade import budget 与 core/optional dependency 契约已批准；
24. 试点满足 closure 风险预算，而非只满足模块数；
25. 每个节点具有明确且有限的 behavior contract；
26. 测试 evidence 定义 deterministic/flaky/retry/timeout/quarantine 政策；
27. Differential harness 不在生产代码保留旧实现；
28. Python、OS、architecture、extras 和 optional-absence 矩阵可执行；
29. Build/clean-install evidence 绑定依赖解析结果和 artifact hash；
30. Public deprecation shim 有可观察、可测试的 warning contract；
31. Event/error compatibility 覆盖 schema、顺序、terminality 和 recovery；
32. Protocol signature digest 覆盖结构化 implementer 和运行时语义；
33. Target readiness 由证据计算，不是人工布尔值；
34. 热点 cutover 有适用的性能与资源非回归预算；
35. 治理范围覆盖整个可安装/可发布项目，所有相关文件已分类或显式排除；
36. 根 public modules、build metadata、package data、发布文档、配置模板和许可证资产进入 inventory；
37. 根 Public API / Composition、Packaging 和 Release owner 已进入 DAG 和 claim 模型；
38. Inventory 区分 authored、generated source、vendored source 和 generated artifact；
39. Generated source 绑定权威输入、generator、命令和可复现 regeneration evidence；
40. Vendored subtree 绑定 upstream revision、license/NOTICE、本地 patch、更新和安全 owner；
41. Security touch 按执行语义、数据流和信任边界分类；
42. 各 source kind 使用对应质量门禁，同时均受依赖、安全和发行治理；
43. Phase -1D 证明所有发布相关文件被枚举、分类或有审计排除理由；
44. 旧 CLI、phase gate、CI job 和 manifest 已被机器化撤权并通过 negative test；
45. Bootstrap validator 不依赖 `mote` 或旧工具，且无 production 授权能力；
46. 每个 Phase 有版本化 artifact contract、eligibility 和失效传播；
47. G1～G6 Gate Registry 唯一、封闭且版本化；
48. 所有 governance schema 有 expand/contract、混合版本和有损迁移策略；
49. CLI 退出码、机器输出、write set、幂等性和环境契约已冻结；
50. 正式写操作使用隔离 workspace、lease 和 fencing；
51. Actor/runner attestation 绑定可轮换、可撤销、会过期的 trust policy；
52. Phase -1E 对账覆盖全部曾具规范性或授权能力的治理来源；
53. 隔离试点故障注入同时证明 fail closed、crash reconciliation 和环境清理；
54. execution、authorization、invariant 三类状态已分离，历史执行事实不可改写且 predecessor 使用显式三状态条件；
55. merge queue 与 post-merge 协议绑定受保护分支真实 commit，候选分支 evidence 不能直接产生主线 completion；
56. verification failure 通过 containment 和可追踪 rollback/forward-fix repair DAG 收敛，失败历史不会被擦除；
57. 各 Gate 已按风险冻结 freshness、refresh 和 revocation 规则，过期证据不能授权后续状态；
58. temporary exception/deferred item 使用精确、到期、不可扩张的正式记录，且硬不变量不可豁免；
59. evidence store 的内容寻址、保留、验签、密钥保留、复制和 restore test 已达到相应 availability class；
60. Release Closure 使用唯一 release transaction 绑定版本、tag、source commit、实际制品及索引观察，部分发布失败不会被标为完成；
61. 每项旧资产已分类为 internal、public deprecation、plugin compatibility 或 persistent reader，并使用对应 closure 条件聚合；
62. emergency freeze/recovery 可优先于普通 approval、lease 和状态转换，且恢复满足独立授权和重验要求；
63. 第 15 节治理控制面最终不变量均有机器 Gate、negative test 或恢复 fixture；
64. 受保护分支 Git control journal 是唯一事务真相源，所有 projection 可确定性重建且外部系统不能独立授权；
65. 每次正式判定只使用一个原子、完整、冻结 tree 的 facts generation，禁止跨 generation 混用；
66. 每项可授权 artifact 有完整 provenance 和反向失效索引，变化影响闭包通过 mutation test；
67. Gate applicability/N/A 由版本化封闭规则计算，unknown fail closed，人工不能降低 required Gate；
68. 所有治理 ID 有统一 namespace、分配 authority、碰撞检查和永久 tombstone，split/merge 不隐式继承 identity；
69. Repository capability profile 覆盖 Git tree、case/Unicode、symlink、submodule、LFS、sparse/shallow 和 mode 语义；
70. 正式 command registry 使用结构化 argv，manifest 自由文本不得经 shell 执行；
71. Freshness、lease、exception 和 trust expiry 使用可信时间、clock-skew 与 uncertain-time fail-closed 政策；
72. Active authority 升级通过 shadow comparison、非授权 canary、原子 activation 和明确 rollback/forward-repair 边界；
73. Manual ruling 权限封闭，不能伪造 evidence、降低 Gate、覆盖硬不变量或虚构发布事实。
74. Journal ref 禁止 force-push/ref deletion，祖先链、签名、CODEOWNERS、线性化、镜像和历史改写检测均已机器验证；
75. Production merge 与 completion 使用 `merged_unverified` 可恢复协议，中间状态阻止下游且超时自动 containment；
76. Control-plane MVP-1～MVP-5 有 executable demo、测试和 non-capabilities，G6 默认硬关闭且只能由 MVP-6 activation 开启；
77. Full/incremental scan、provenance、journal replay、projection rebuild、内存和 evidence 增长已有容量基线与 blocking SLO，全量无缓存重建 digest 一致；
78. Journal/provenance snapshot、archive、reachability roots、retention 和 GC proof 已冻结并通过 genesis/snapshot 双重重建；
79. Governance Operations RACI 覆盖所有控制面服务、故障、runbook、响应/恢复目标和 restore drill；
80. Git/CI/evidence/lease/time/index 故障均有 degraded-mode matrix，默认禁止新批准和 cutover，只允许不扩大风险的 containment/rollback；
81. Bootstrap genesis 经双人职责分离、独立重建、签名 commit/tag、镜像和 sealed backup 完成；
82. 每个 scanner 有 soundness contract、固定语料、unknown 规则与漏报 mutation test，新增 false negative 是 hard failure；
83. 规范拆分使用唯一 owner 和 stable rule ID，主策略维护依赖图与优先级，不复制或分叉规则。

当前授权边界为：Phase -1A、-1B0、-1B1 可以实现并隔离验证；Phase -1B2、-1C、-1D 只能按 MVP 顺序实现且 G6 硬关闭；Phase -1E～-1G 只可设计 schema/manifest/fixture。真实试点须先关闭 journal integrity、`merged_unverified`、MVP-6、运维/降级闭环，以及既有 journal、facts、provenance、Gate、三状态和 repair 条件；Production cutover 与 Release Closure 均不可开始。

---

## 14. 非目标

本治理明确不做：

- 不引入或规划 Go/Rust 迁移；
- 不建立 RPC、Sidecar、远程 Worker 或分布式队列；
- 不重写 Kernel、Runtime 或 Orchestration 算法；
- 不改变用户可见行为、默认值、协议和持久化格式；
- 不因想象中的未来消费者提前抽象；
- 不为了减少目录数合并不同 owner；
- 不为了目录美观拆分内聚能力；
- 不通过兼容层延后正确迁移；
- 不将文档目标误当成已经完成的源码事实。

如未来需要改变语言或部署边界，必须单独立项、重新批准，不得自动继承为本包治理计划的一部分。

---

## 15. 最终不变量

1. 分层依赖只能向下。
2. 一个能力、状态、注册表和生命周期只有一个 owner。
3. Contracts 只保存跨边界稳定语义。
4. Kernel 只决定单 Agent 如何思考和推进。
5. Runtime 只负责当前 Agent/执行的可靠机制。
6. Orchestration 只负责多实体协调。
7. Product 负责具体产品策略和唯一装配入口。
8. Role 通过组件组合，不演化成上帝对象。
9. 低层通过窄 Port 请求高层能力，不反向 import。
10. 持久身份不依赖 Python 模块路径。
11. 旧路径在原子 cutover 中删除，不保留双轨。
12. 所有边界和例外由机器测试验证。
13. 只有一个 active governance authority；旧 authority 只能作为不可授权的审计输入。
14. 历史 execution state append-only；authorization freshness 与 invariant satisfaction 独立计算。
15. 任何主线 completion 都绑定受保护分支上的真实 commit 和 post-merge evidence。
16. 失败节点只能经可追踪、可验证的 rollback 或 repair 收敛，原失败历史不可擦除。
17. 临时例外必须精确、有期限、不可静默扩大，且不得豁免安全、权限和持久化硬不变量。
18. Completion evidence 必须可寻址、可验签、可恢复，并满足适用的保留与 freshness 要求。
19. Release 状态必须绑定唯一版本、source commit、tag 和实际发布制品；已发布版本与制品不可覆盖。
20. 每项资产按自身 closure class 闭合；长期兼容 reader 必须有 coverage、保留期限和清理策略。
21. Emergency freeze 优先于普通 authorization、lease 和状态转换；恢复不得由受损身份单独批准。
22. 受保护分支 Git control journal 是唯一治理事务真相源，projection 与外部系统均无独立授权权力。
23. 正式治理结论只基于单一、原子、完整且绑定冻结 Git tree 的 facts generation。
24. 所有授权输入都有完整 provenance；失效沿反向依赖闭包传播，unknown 或无来源 artifact 不得授权。
25. Gate applicability 由封闭、版本化规则计算；人工只能提高要求，不能降低 required Gate。
26. 治理 ID 与路径、名称解耦，删除后永久不可复用，split/merge 显式保留谱系。
27. Git tree 是 repository object 身份基础；不支持或无法一致 materialize 的仓库能力必须 fail closed。
28. 正式命令使用结构化 argv；shell 只存在于固定、已验签且经安全审批的显式边界。
29. 所有 expiry/freshness/lease 判定使用可信时间和明确 skew 政策；时间不确定时禁止新授权。
30. Active governance authority 的升级本身是受治理迁移，任何时刻只有一个版本能产生 production authorization。
31. Manual ruling 不能伪造事实、证据或发布结果，也不能覆盖不可豁免的硬不变量。
32. Git journal 的 genesis 祖先链、签名、保护策略和镜像必须连续可验证；历史改写触发全局写冻结。
33. Production merge 与 completion 不是伪原子动作；`merged_unverified` 必须显式、可恢复并阻止下游。
34. MVP-1～MVP-5 永远没有 production authorization 能力；G6 只能由满足全部条件的 MVP-6 激活。
35. 缓存和增量计算不得改变授权结论；无缓存全量重建必须产生相同 digest。
36. Snapshot 只加速重建，不替代原始 journal；tombstone、历史和可达附件不得被 GC。
37. 每个控制面依赖都有 service owner、backup owner、runbook、恢复目标和演练证据。
38. 依赖降级默认关闭新 approval/cutover，只允许预先批准且不扩大风险的 freeze、containment 或 rollback。
39. Bootstrap genesis 必须由两个独立角色可复现重建、签名并封存，后续 authority 可追溯至 genesis。
40. Scanner 对无法可靠解析的输入输出 unknown；新增关键漏报永远不能由基线接受。
41. 每条规范规则只有一个 owner 和 stable rule ID，拆分文档不得复制规范正文。

这套策略的最终成果不是某个固定目录树，而是一个可以持续自我校验的 Python 包系统：新增能力有明确落点，移动能力有统一协议，错误依赖无法进入主线，任何临时例外都能按期归零。
