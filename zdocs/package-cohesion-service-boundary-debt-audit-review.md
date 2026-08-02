# Mote 全包内聚与服务边界债务审计评审

状态：评审未通过  
评审日期：2026-07-31  
被评审文档：`zdocs/package-cohesion-service-boundary-debt-audit.md`  
治理依据：`AGENTS.md`  
评审范围：需求事实、严重度、canonical owner、服务边界、基础设施复用、实施顺序与验收证据

## 1. 评审结论

当前版本不应直接批准为实施需求或权威债务总账。

文档识别出了一批真实且重要的架构债务，包括 Workflow 进程内状态、Agent 三类容量混用、delivery 保证失真、durable backend 保证弱化、宽 codec、巨型 service locator、平行 composition 入口和包根公共面泄漏。这些发现适合作为后续需求收敛的审计证据。

但当前文档仍把以下三类内容混在同一份强制清单中：

1. 已由源码直接证明的当前缺陷；
2. 尚需产品决定或完整调用链证明的风险判断；
3. 尚未完成消费者、复用和生命周期论证的候选重构方案。

因此，本评审建议将其状态改为：

> 评审未通过；可作为架构债务候选证据保留，不可直接作为实施规格或第二份权威 backlog。

复审前必须校正事实与严重度，拆开“问题事实”和“候选设计”，为每个 P0 补齐 canonical contract、owner、lifecycle、durability、failure/recovery、真实消费者和现有基础设施复用证据，并与唯一债务总账建立逐项映射。

## 2. 阻断问题

### R1：候选服务拆分被提前写成强制架构

严重度：P0  
对应原文：SB0.1、SB0.2、SB1.3、SB1.4、SB1.14、整改顺序 S2-S7

原文直接要求建立 `TreeAdmissionService`、`ResidencyService`、`TurnAdmissionService`、`WorkflowRunService`、`ApplicationSessionService`、`AgentHostingService` 等新服务，但没有逐项给出：

- 仓内已经搜索过的同类 owner、Port、store、lease、scheduler、codec、composition 和消费者；
- 为什么不能在现有 canonical owner 内扩展最小能力；
- 每个新服务的唯一状态真相、最小方法和真实消费者；
- lifecycle scope、activation、shutdown、recovery 和 fencing owner；
- 拆分后如何避免无语义薄层、平行入口或第二套状态机。

这不满足 `AGENTS.md` 6.1、6.3 和 6.4 的复用与最小服务面要求。

Agent logical identity、resident incarnation 和 concurrent turn 三类容量必须成为三个独立、类型化的不变量，但这并不自动意味着必须建立三个独立 service object。需求应先固定三个概念的 identity、状态、原子操作和失败语义，再由真实调用链决定它们是在一个 cohesive control service 内闭合，还是形成多个窄服务。

整改要求：

1. 将所有具体新服务名从“要求”降为“候选设计”；
2. 对每个候选服务补齐 AGENTS.md 6.4 的六项评审证据；
3. 明确哪些现有 owner 被扩展、哪些被删除，以及为什么不会形成双 owner；
4. 在产品语义未确认前，不建立 public Port、manifest 或架构门禁固化候选方案。

### R2：SB0.4 没有证明用户命令绕过治理

严重度：P0  
对应原文：SB0.4

已确认的源码事实是：

- `runtime/process.py::aexecute` 同时接受字符串 shell 命令、非 shell 模式、sandbox adapter 和多种返回形状；
- `shell` 默认值为 `True`；
- 非 shell 分支使用 `cmd.split()`，不能正确表达结构化 argv；
- `sandbox_runtime` 和返回值缺少正式 typed contract；
- Product Bash 工具直接调用该宽 runner。

这些事实足以证明 runner 混合了两个信任域和多个生命周期语义，是需要整改的正式边界债务。

但原文仅以 `product/toolsets/builtin/bash.py` 导入并调用 `aexecute` 为依据，不能证明 classifier、permission、approval 或 ToolExecutor pipeline 已被绕过。Bash 工具本身处于工具治理链中，也会向 runner 注入 sandbox runtime。是否存在真实安全绕过，必须从模型工具调用开始，完整追踪 ToolExecutor、classifier、permission、approval、sandbox plan 到 process spawn 的控制链，并用测试证明某个 deny/ask 决策可以被旁路。

整改要求：

- 保留“Trusted argv 与 Governed shell 必须使用不同 typed runner”的设计方向；
- 在没有绕过证据前，将本项从安全 P0 降为 P1 边界债务；
- 若完整调用链证明用户可控命令绕过 permission/approval/sandbox，或固定内部 argv 进入默认 shell，则恢复为 P0，并补回归门禁；
- 分别定义结构化 argv plan、受治理 shell plan 和 typed process receipt，禁止以 `shell: bool` 混合信任域。

### R3：SB0.3 的“静默降级”不符合当前源码

严重度：P1  
对应原文：SB0.3

当前 `runtime/durable/factory.py` 在 Temporal dependency 缺失或 backend 未实现时会记录 warning，然后回退到 JSONL。因此“静默降级”并非准确事实。

真正的问题仍然成立：显式选择 Temporal 后，系统可以继续运行在较弱 backend 上；调用方得不到 typed activation result 来判断实际 backend、实际 guarantee 和拒绝原因。日志属于 observation plane，不能替代 authoritative composition result，也不能改变安全和 durability 路径应 fail closed 的要求。

建议改写为：

> 配置选择的 durability guarantee 会在仅记录 warning 后自动弱化；调用方无法通过 typed result 得知实际启用的 backend 和 guarantee，因而不能可靠地拒绝不满足要求的运行。

同时必须先确认 JSONL 与 Temporal 各自承诺的 durability、recovery、placement 和 effect guarantee。不能仅凭 backend 名称推断强弱，也不能在尚未定义 guarantee contract 前先决定 factory 的最终形状。

### R4：“全包审计”声明缺少可复现证据

严重度：P1  
对应原文：第 1 节、第 6 节

原文声称扫描了 196 个一级、二级生产目录，但没有提供：

- 196 个扫描目标的完整、可复现清单；
- 目录计数命令、排除规则和空目录处理规则；
- 每个包的 public surface、生产消费者和跨包依赖；
- owner、核心不变量、lifecycle、durability 和状态真相；
- 每个“保留”或“收窄”结论对应的检查证据。

当前覆盖矩阵更接近全包 inventory 加高风险抽样审计。矩阵中的“保留”“保留方向”只是结论标签，不能证明相应包已经完成服务边界和内聚审计。

整改要求二选一：

1. 将标题和结论改为“全包 inventory 与高风险服务边界审计”；或
2. 提供可生成的 package inventory，并为每个包记录 owner、不变量、public service、lifecycle、durability、允许依赖、真实消费者和证据位置。

### R5：拟议架构门禁包含不可可靠静态判断的语义

严重度：P1  
对应原文：第 7 节

以下规则不能仅依靠符号名称或 AST 形状可靠判断：

- 包根禁止导出所有 `store/lock/registry/task/client/backend`；
- 禁止所有跨包 concrete store/registry/state 使用；
- 根据同义名称判断平行 owner；
- 在所有正式边界无条件禁止 `object` 或裸 mapping；
- 根据构造类型名称判断是否为 composition root。

合法稳定服务也可能使用 `Store`、`Client` 或 `Task` 命名；两个相似 store 也可能因 bounded context、durability 或 lifecycle 不同而必须保持独立。反之，没有这些后缀的对象仍可能泄漏内部状态。使用名称启发式门禁会产生误报、白名单债务和形式主义架构。

S0 只应先加入可精确判定的门禁：

1. hermetic architecture test collection；
2. 生产局部 import；
3. 已确认 bounded context 之间的 `_private` import；
4. package-root import purity 和 optional dependency isolation；
5. 已确认唯一 composition root 后的具体禁止构造清单。

其余门禁必须由经过评审的 owner/public-surface manifest 驱动。manifest 应记录 authoritative module 和允许消费者，门禁验证明确关系，不能自行推断业务语义。

### R6：整改顺序可能在 contract 确认前固化候选 owner

严重度：P1  
对应原文：第 8 节

原文先建立 package/service manifest，再处理 durable facts、Workflow、Agent 和 composition。这会在以下 contract 尚未确认时，把候选 owner 和服务名固化成架构事实：

- Workflow definition version、deployment identity、run identity 和 checkpoint schema；
- delivery 中哪些消息属于 canonical fact，哪些只是 best-effort observation；
- Temporal 与 JSONL 是否承诺同一 durability guarantee；
- BackgroundTask scope、settlement、release 和 output ownership；
- Application、process、session、Agent、turn 各 scope 的 composition owner。

建议改为：

```text
S0  修复 hermetic architecture collection 与 import purity
S1  对每个 P0 固定产品语义、canonical contract 和唯一 owner
S2  在 canonical owner 内实现正常、失败、恢复和清理路径
S3  迁移全部生产消费者并删除旧入口、旧状态和旧 identity
S4  增加针对已确认边界的精确架构门禁
S5  再处理 P1/P2 公共面和包内聚债务
```

门禁应保护已经确认和实现的架构，而不是替代产品与 contract 设计。

## 3. 已确认成立的高风险发现

以下发现有直接源码证据，可继续进入后续 contract 评审。

### 3.1 Workflow 尚未满足 durable execution 硬约束

已确认：

- `WorkflowDefinition` 持有进程内 graph object；
- `WorkflowContinuation` 持有 definition、checkpoint 和 run state 对象；
- `WorkflowRun` 使用进程内 UUID、布尔 `_executing` 和 asyncio task 表达执行 ownership；
- Product 拥有进程内、consume 即 pop 的 continuation registry；
- BackgroundTask adapter 可以根据 graph metadata 构造另一种 `WorkflowDefinition` identity；
- 当前路径没有版本化 run/checkpoint envelope、strict decoder、durable checkpoint store、lease 或 monotonic fencing。

这与 AGENTS.md 8.2 的 durable Workflow 产品决定冲突，应保持 P0。

但后续需求必须分别定义 definition identity、run identity、execution generation、checkpoint revision、pending frontier、effect intent/receipt 和 fenced owner，不能只用一个笼统的 `WorkflowRunService` 名称代替设计。

### 3.2 Agent 三类容量与 durable lineage 尚未闭合

已确认：

- `max_agents` 同时初始化 concurrent turn limiter，并作为默认 residency capacity；
- registry 的 identity reservation 明确传入 `None`，没有 logical identity 总量 cap；
- lineage、path、nickname 和 count 位于进程内 registry；
- pending delivery 是进程内 queue；
- 代码却公开声称 delivery `never fails and never drops`；
- `AgentControl` 公开 registry 等内部对象。

这与 AGENTS.md 8.1 已确认的三种容量、durable lineage、fencing 和 delivery 语义冲突，应保持 P0。

需要修正原文措辞：`reserve_spawn_slot(None)` 不是偶发地绕开了一次已经闭合的 logical cap，而是当前实现根本没有独立的 logical identity cap。需求应明确三类 cap 各自的 identity、计数对象、原子 acquire/release、持久化和恢复语义。

### 3.3 Durable fact codec 不满足严格 wire/durable decoder 要求

`DurableFact.from_payload()` 只验证顶层字段集合，再直接调用 dataclass constructor。它没有统一验证 schema version、tag、primitive type、nested DTO、unknown variant 和 durable identity。

这一发现与 AGENTS.md 4、9、14 的严格 decoder 与 fail-closed 要求直接冲突，应保持 P0。

后续方案不能简单为每个 fact 复制一套 codec。必须先搜索并选择 canonical schema/codec 基础设施，在唯一 owner 内扩展版本化、严格、可测试的 decoder；各 bounded context 只拥有自己的 schema 和 tagged union。

### 3.4 Runtime composition lease 暴露运行时对象图

`RuntimeCompositionLeasePort` 同时公开 route policy、default model、command/session/transfer runtime、permit issuer、artifact store/reader 等能力，其中多项为 `Any`。这既截断类型关系，也让 lifecycle lease 承担 service locator 职责。

该发现可保持 P1。但拆分前必须按真实消费者建立 capability matrix，优先复用现有窄 Port。不能为每个 property 机械新增一个无消费者或仅转发的 Protocol。

### 3.5 Architecture pytest collection 当前不可用

实际执行 `ztest/architecture` 时，测试在 collection 阶段失败。首个错误是基础 Product/composition import eager 加载 Terminal backend，而环境缺少 optional `pyte`；随后出现 partially initialized `Application` 循环导入错误。

这意味着架构门禁不能在声明的最小环境中执行，且基础 package import 会加载 Product composition 和 optional backend。SB2.14 成立，应作为最先修复的独立切片。

修复目标必须是 hermetic collection 和 import purity，不是通过安装 `pyte` 掩盖 eager import，也不能用局部 import 回避循环。

## 4. 需要重新定级或补证据的条目

### 4.1 暂时降为 P1

- SB0.4：确认 runner 信任域混合，但尚未证明 permission/approval/sandbox 绕过。

### 4.2 保持 P0，但修正事实表述

- SB0.3：不是静默降级，而是日志告警后自动弱化 guarantee；若配置选择代表硬 durability 要求，则仍为 fail-closed 违约。
- SB0.2：不是单次调用偶然绕过 logical identity cap，而是当前没有独立 logical identity cap。

### 4.3 保持候选，实施前必须补消费者和 owner 分析

- SB1.3、SB1.4：多个 composition/hosting 构造点是否都是真正 composition root，需要按对象 identity、scope 和 lifecycle 逐项证明；薄 surface adapter 不能仅因调用 factory 就计为第二 root。
- SB1.11：`ToolExecutor` 依赖多不自动等于巨型 manager；需要区分 Product composition 注入、包内 cohesive pipeline 和真正泄漏的 public control surface。
- SB1.12：公开 registry map 是否可变、消费者是否真实修改、snapshot 是否已有 canonical owner，需要列出生产调用点。
- SB2.1-SB2.12：公共面和低内聚判断需要真实包外消费者证据，不能只根据 `__init__.py` 导出数量或文件名称决定。

## 5. 复审准入条件

下一版至少满足以下条件后再进入实施评审：

1. 每个 SB 项区分“当前事实”“违反的不变量”“产品决定”“候选实现”和“验收”。
2. 每个 P0 给出完整生产调用链、canonical state、identity、owner、lifecycle、durability、failure、recovery 和 cleanup。
3. 每个新增 Port/service/store/codec/factory 给出 AGENTS.md 6.4 要求的复用与最小服务面证据。
4. 明确所有必须删除的旧入口、旧 identity、旧 state 和旧 consumer，不允许兼容层、双写或双读。
5. 将 196 包扫描变成可复现 inventory，或降低“全包审计”的声明范围。
6. 与 `core-architecture-debt-closure-requirements.md` 建立逐条映射，明确合并、扩充和新增项；不得形成第二份长期权威 backlog。
7. 架构门禁只验证已经确认的 authoritative owner 和依赖规则，不以命名启发式替代语义评审。
8. 每个实施切片列出直接子系统测试、消费者测试、architecture gate、Pyright 范围以及 fault-injection 场景。

## 6. 建议的需求模板

每个待实施 SB 项建议改写为独立垂直切片，并使用以下结构：

```text
编号与严重度
当前事实及源码证据
违反的已确认不变量
稳定面
允许变化轴
canonical owner 与 authoritative type
现有基础设施搜索与复用决定
真实生产消费者
最小 command/query/Port
identity、revision、lease/fencing
construct/activate/shutdown/recovery lifecycle
正常、失败、恢复、取消与清理语义
被删除的旧入口和旧状态
架构门禁
直接测试、消费者测试、Pyright 与 fault injection
待用户确认的产品决定
```

任何尚未确认的二选一语义必须列入“待用户确认”，不能由实现者通过类名、默认值或 fallback path 代选。

## 7. 验证记录

本次评审为只读检查，没有修改生产源码或测试。

实际执行：

```text
git status --short --branch
sed -n '1,760p' zdocs/package-cohesion-service-boundary-debt-audit.md
rg --files contracts kernel runtime orchestration product ztest/architecture
rg -n '<相关 owner、类型、调用点与保证>' contracts kernel runtime orchestration product --glob '*.py'
python -B -m pytest ztest/architecture -q --tb=short -p no:cacheprovider
```

测试结果：

- `ztest/architecture`：退出码 2；31 个 collection errors；0 个测试执行；
- 首个失败：`ModuleNotFoundError: No module named 'pyte'`；
- 后续主要失败：`mote.product.composition.application.Application` partially initialized 循环导入；
- 未安装缺失依赖；
- 未修改测试规避失败；
- 未运行各 SB 对应的子系统测试、消费者测试或 Pyright，因为本次任务仅评审需求，没有实施变更。

工作区在评审开始前已有修改和未跟踪文件；这些内容均视为用户改动，本次未覆盖、回滚或格式化。

## 8. 第二轮补充核验

本节补充核验原审计中尚未充分区分的 composition、ToolExecutor、registry 和 import boundary 项。以下结论应与前述评审结论一并作为复审输入。

### 8.1 SB1.3 可以确认：Session hosting 已复制 Agent control composition

`product/session_hosting/registry.py` 的模块说明声称，多 Session host 使用共享 `EngineBuild`，构造路径与单 Session host “byte-identical”且“no parallel bootstrap”。但该模块实际定义了自己的 `_build_control()`，并直接构造：

- `AgentControl`；
- `ResidencyStore`；
- `AgentRuntime`；
- root agent registration；
- `role.agent_control` binding。

`product/entrypoints/cli/backend.py::build_control()` 又独立执行同一组装配。两处当前字段基本相同，但已经是两份生产装配代码；其中一处未来增加 capacity、lineage store、incarnation factory、lifecycle activation 或 cleanup 时，另一处不会被类型系统或 composition contract 强制同步。

因此，这不是“调用同一 canonical factory 的两个 surface”，而是已经成立的平行 composition 路径。SB1.3 可保持 P1，且应优先于 facade 命名重构处理。

整改时不应再新增第三个 wrapper。应选择 Product 唯一 canonical session/agent hosting composition owner，把以下输入显式类型化：

- session identity；
- session workspace/root paths；
- durable lineage/residency store Port；
- capacity/admission policy；
- Role/Agent definition；
- process/application scoped shared services。

CLI 和 multi-session surface 只提交 typed request 并取得 lifecycle-owned session handle。迁移同一切片必须删除两处直接 `AgentControl(...)` 构造中的至少一处，最终由 architecture gate 只允许 canonical owner 构造该有状态 control plane。

### 8.2 `builtin_service_gateway()` 是隐式有状态 owner 构造点

`product/composition/service_gateway.py::builtin_service_gateway()` 在以下依赖缺失时自行构造默认 owner：

- media provider registry；
- web-search backend registry；
- resource admission controller；
- failover planner 和 endpoint resolver object graph。

其中 registry/catalog 的 Product 默认选择可以由 Product composition 拥有，但一个看似普通的 gateway factory 同时决定 provider catalog、admission state 和 failover runtime，会使调用者绕过 application composition generation 与 lifecycle ownership。

SB1.3 对该函数的判断方向成立，但需求需要进一步区分：

- immutable、无外部资源的 value assembly；
- application/process scoped mutable owner；
- session scoped gateway lease；
-仅用于测试的显式 fake composition。

不能机械禁止所有默认参数。应禁止的是：缺参时隐式创建带 mutable state、并发原语、durable store、lease 或外部资源生命周期的 owner。纯不可变默认 DTO 仍可合法存在。

### 8.3 SB1.11 部分成立：ToolExecutor 明确保留了第二 composition 入口

`runtime/tools/tool_executor.py` 已经把 catalog、MCP lifecycle、recovery、journal、settlement、policy 和 pipeline 拆为多个内部协作者。这说明“构造函数依赖很多”本身不能证明它仍是一个未拆分的巨型 manager；这些对象围绕一次 Tool execution 的共同不变量协作，可能合法地由一个 cohesive package service 持有。

但当前实现存在三项已确认问题：

1. 构造函数注释明确写出“standalone executor is its own composition root”；
2. 未注入 `tool_call_policy` 时，Runtime 内部根据 Role、toolset 反射和默认值自行构造 policy；
3. executor 公开 `catalog`、`journal`、config 以及 live `_tools` map，其中 `_tools` 明确为兼容外部 introspection 和测试而保留。

第一项与“唯一 Product composition root”直接冲突；第二项允许 Runtime 在缺少可信 Product composition 时选择治理默认；第三项泄漏内部 catalog/journal/state，使消费者可以依赖内部对象图。

因此 SB1.11 应改写，不应以“二十余依赖”作为主要判据，而应聚焦：

- 删除 standalone production composition path；
- Product 显式注入完整 typed ToolExecutionPolicy/manifest；
- Runtime ToolExecutor 只拥有 execution lifecycle，不选择 Product 默认或信任策略；
- 对外提供 immutable tool catalog snapshot/query，不返回 live catalog/map；
- journal、settlement、MCP lifecycle 和 pipeline 保持 package internal；
- 测试通过正式 fake Port 验证，不迫使生产对象公开 `_tools`。

在完成真实消费者搜索前，不建议把每个内部协作者再提升为跨包 Port。包内具体类型协作符合 AGENTS.md，只有真实跨 bounded-context 消费才需要最小公共服务面。

### 8.4 SB1.12 可以确认 mutable registry 泄漏，但三类 registry 不应被强行合并

当前以下属性是 public mutable `dict`：

- `product/models/registry.py::ProviderRegistry.providers`；
- `product/web_search/registry.py::SearchBackendRegistry.backends`；
- `product/media_generation/registry.py::MediaProviderRegistry.providers`。

调用者可以绕过 `register()` 的 duplicate identity 检查，直接增加、替换或删除 entry。因此“private registry + immutable catalog snapshot”的整改方向成立，应保持 P1 或在对应 Product provider 子系统下一次变更中闭合。

但三者仅仅 shape 相似，不代表应抽象为一个 shared registry。它们分别拥有模型 provider、搜索 backend 和媒体 provider 的 identity、factory contract、配置 decoder 与生命周期。正确做法是在各自 bounded context 内封装 mutable map，并只向包外暴露 typed lookup/query 或 immutable snapshot；不得为去重几行代码建立跨域通用 registry。

实施前还应搜索所有直接 `.providers`、`.backends` 生产访问，区分只读消费者和真实 mutation，并在同一切片迁移完毕后将字段私有化。

### 8.5 SB2.13 的 private import 清单成立，但需要按 owner 分别解决

第二轮核验确认以下生产 import 直接依赖 `_private` 符号：

- Cron service 导入 `_next_cron_run_ms`；
- Squilla strategy 导入 `_CLASS_TO_IDX` 和 `_apply_flag_overrides`；
- Gateway CLI 导入 `_shared_application_identity`；
- Config report 和 model startup 导入 diagnostics 私有函数；
- CLI `__main__` 导入 `_HAS_TEXTUAL`。

这些导入违反包公共面纪律，但不能统一通过去掉下划线解决。每项必须先判断共同变化关系：

- 若仅是同一 bounded context 内错误拆文件，可合并 owner 或建立 package-internal public module；
- 若是跨 bounded context 的稳定能力，应提升为最小 typed query/DTO；
- 若仅为 presentation 或 optional dependency 探测，应由 Product surface 提供显式 capability query；
- 若符号不应被外部消费，则删除消费者路径。

禁止通过包根 re-export、旧名 alias 或把 `_name` 机械改成 `name` 掩盖 owner 错置。

### 8.6 SB2.14 的生产局部 import 证据不成立

原审计把以下位置列为生产局部 import：

- `orchestration/workflows/base_node.py:33`；
- `product/entrypoints/cli/__main__.py:24,29,34`。

逐行核验后，这一结论不成立：

- `base_node.py:33` 位于类 docstring 的示例代码中，不是 Python AST import 节点；
- `__main__.py:24,29,34` 均位于模块顶层 `try/except ImportError`，不是函数、方法、类体、property、factory 或异常处理函数内部的延迟 import；
- 模块顶层 `try/except ImportError` 正是 AGENTS.md 3 对 optional dependency 和平台依赖允许的形式。

实际重新执行：

```text
python -B ztest/architecture/static_governance.py local-imports
```

结果为退出码 0：`local-imports architecture invariant is closed`。结合 AST 和源码逐行检查，这是正确结果，不是门禁漏检。

因此，SB2.14 必须删除“生产局部 import”指控，不能为这些位置新增回归失败。该项只保留另一个独立且已经复现的问题：基础 package/composition eager import 导致 architecture pytest collection 加载 optional Terminal backend，并进一步触发 partially initialized `Application` 循环错误。

`product/entrypoints/cli/__main__.py` 导入 `_HAS_TEXTUAL` 仍属于 SB2.13 的 private symbol dependency，但它是公共面/owner 问题，不是 local import 问题。

## 9. 第二轮后的优先级调整

综合两轮核验，建议将前置实施顺序进一步收窄为：

```text
A0  修复 architecture collection 和 package import purity
A1  固定 Workflow durable contract 与 Agent 三类容量/lineage/delivery contract
A2  收敛唯一 Agent/session composition，删除重复 AgentControl 构造
A3  删除 ToolExecutor standalone production composition 与 Runtime policy 默认选择
A4  收窄 RuntimeCompositionLease、ToolExecutor 和 provider registry 公共面
A5  再按真实消费者处理其他 P1/P2 包内聚项
```

其中 A0 只修复门禁可执行性和已确认的精确违规，不提前建立覆盖所有 store、registry、service 名称的泛化门禁。A1 的产品 contract 经确认后，A2-A4 才能以 authoritative owner manifest 固化边界。

第二轮额外执行的四项 hermetic 静态门禁均通过：

```text
python -B ztest/architecture/static_governance.py local-imports
python -B ztest/architecture/static_governance.py product-scc
python -B ztest/architecture/static_governance.py dynamic-discovery
python -B ztest/architecture/static_governance.py governed-boundary
```

结果：4 项退出码均为 0。它们证明现有静态规则在各自范围内通过；其中 local-import 结果还直接反证了原审计 SB2.14 对两个源码位置的误读。它们不覆盖 pytest collection 的 eager optional import 问题，也不证明 P1/P2 服务边界已经闭合。

## 10. 第三轮补充核验

### 10.1 “196 个目录/模块”可以复现，但原文应补生成口径

第三轮按原文“全部一级、二级生产包及一级生产模块”的口径重建 inventory：

- 五层下一级、二级目录中，目录本层至少包含一个 `*.py` 的目录共 188 个；
- 五层 package root 下除 `__init__.py` 外的一级生产模块共 8 个；
- 合计恰好为 196。

8 个一级生产模块均位于 Runtime：

```text
runtime/content_hashing.py
runtime/engine.py
runtime/file_paths.py
runtime/presentation.py
runtime/process.py
runtime/run_context.py
runtime/services.py
runtime/terminal_ansi.py
```

因此，原文“扫描 196 个”的数字本身可以复现，不应作为事实错误否定。前述 R4 应收窄为证据可追溯性问题：原文没有记录生成命令、纳入条件和 196 项完整清单，读者无法仅从报告判断计数口径，也无法复查每个“保留”结论的消费者与不变量证据。

建议原审计补充一个由 `rg --files`/确定性脚本生成的 inventory artifact，至少记录：

- package/module path；
- 是否包含生产 Python；
- authoritative public module；
- 直接生产消费者；
- 对应 SB 编号或“未发现新债务”的证据状态。

这不要求为每个包创建长期手写文档；生成结果应服务审计复现，canonical owner manifest 仍只在边界经评审确认后建立。

### 10.2 SB1.16 成立，而且源码注释直接承认 owner 错位

`product/presentation/events/events.py` 定义跨 Terminal、Web、IM host 共用的 `ViewEvent` human protocol。模块 docstring 明确写道：

> This lives in the shared `contracts` layer (not a single host)

但文件实际位于 `product/presentation/events/`。`capabilities.py` 也声称自己位于 shared contracts layer，实际仍在 Product。ACP、AGUI、Structured、Terminal、Textual 等多个 Product host 直接消费这些 DTO。

因此，“跨 host 稳定 DTO 的 authoritative owner 错放 Product”不是仅由目录命名推断，而是当前自述 contract 与真实源码 owner 的直接冲突。SB1.16 可保持 P1。

迁移时需要拆开三类内容，不能把整个目录机械移动到 Contracts：

1. `ViewEvent`、approval decision、host-neutral capability DTO 等跨 host 纯数据 contract，应进入 Contracts-owned presentation/surface bounded context；
2. `CapabilityAdapter` 带有 buffering、stream folding 和 downgrade 行为，属于 Product presentation policy，不应因同文件而下沉 Contracts；
3. `TERMINAL_CAPS`、`TEXTUAL_CAPS`、`STRUCTURED_CAPS` 是具体 Product surface 默认值，应留在对应 Product composition/surface，而不是进入 Contracts。

此外，当前 `ViewEvent` 自称 tagged union，但使用 `ClassVar[str] kind`、开放继承和多个 `object` 字段。若它进入正式跨 host/wire 边界，还必须确认：

- 是进程内 presentation DTO，还是需要持久化/跨进程的 wire DTO；
- 是否需要封闭 union 和严格 decoder；
- `scope: tuple[object, ...]`、output `value: object` 如何替换为 canonical identity/content contract；
- 新事件对未知 consumer 的兼容语义是否属于 wire versioning，而不是依赖“ignore unknown kind”的口头约定。

在这些 contract 未确认前，不应只移动文件并增加 re-export；同一切片要迁移全部消费者并删除 Product 中的旧 contract owner。

### 10.3 SB1.17 部分成立：Product builtin 默认值明确污染 Contracts

`contracts/config/tool/models.py` 直接定义按 `Read`、`Search`、`Bash`、`Edit`、`Sleep` 名称索引的结果大小默认值。这些名称属于 `product/toolsets/builtin/` 的具体 Product catalog，Contracts 不应知道哪些 builtin tool 存在，更不能拥有它们的默认 cap。

该部分与 AGENTS.md 2.1、2.5 和 13 的 owner 规则直接冲突，应保持 P1。整改应把 per-builtin override 迁到 Product tool manifest/composition，并让 Runtime 只接收已经解析的 immutable typed limit policy。

但原审计进一步要求把 `ToolResultLimitConfig`、`RunJournalConfig`、`LoopGuardConfig` 按 settlement/effect/loop bounded context 全部拆开，当前证据仍不足。它们目前由同一个 ToolExecutor execution path 消费，可能是同一 Product tool execution policy 的多个 typed 子配置；多个 class 放在一个模块不自动构成多 owner。

正确的切分依据应是：

- result limiting/compression 的机制 owner 与 lifecycle；
- external effect journal 的 durability 和 settlement owner；
- loop guard 的模型治理 owner；
- BackgroundTask 为什么直接消费 `ToolResultLimitConfig`，其输出是否复用同一 canonical spill mechanism。

如果三者 lifecycle、状态和消费者确实独立，应迁到各自 authoritative config module；如果只是在 Product composition 中共同构成一个 ToolExecutionPolicy，则可以保留聚合 DTO，但 Contracts 只能拥有稳定 shape，默认值由实际机制 owner或 Product composition 提供。

### 10.4 SB1.18 方向成立，但不能直接拆 enum 而忽略 serialized ABI

`contracts/foundation/errors/codes.py::ErrorCode` 当前聚合 model、service、router、tool、file operations、media、graph、background task、OAuth、config、agent、output、runtime、artifact 和 resource 等多个 bounded context。新增任一领域错误都会修改同一个全局枚举，且注释仍包含已移动或错误 owner 路径。这是明确的共同修改点和 owner 聚合债务。

但 `ErrorCode` 也是现有 `MoteError.to_dict()` 的稳定 machine-readable identifier。直接拆成多个 enum 会改变异常构造、序列化、decoder、日志、wire consumer 和测试快照，不是单纯移动常量。

在批准 SB1.18 的具体方案前，必须先重建：

- 所有 157 个生产 `ErrorCode` 引用点；
- error serialization/deserialization 和外部 wire consumers；
- code string 是否已有外部稳定承诺；
- namespace 是独立字段，还是 code 的规范前缀；
- unknown namespace/code 的 fail-closed 或 display fallback 语义；
- 各 bounded context authoritative typed error 与跨边界 `ErrorReport` 的投影关系。

建议需求先固定 `ErrorIdentity(namespace, code)`/等价 canonical contract，再在单一迁移切片迁移全部仓内 producer/consumer 并删除全局 `ErrorCode`。不得保留旧 enum alias、双格式 decoder 或长期兼容 re-export。若外部 wire 已有稳定承诺，则其版本退出属于必须由用户确认的产品决定。

### 10.5 SB2.2 是命名和公共面债务，不是两套相同 Artifact store

当前确实存在两个名为 `ArtifactRepository` 的类型：

- `runtime/artifacts/repository.py::ArtifactRepository`：immutable content-addressed repository；
- `runtime/fileops/mutation/artifacts.py::ArtifactRepository`：FileOps mutation lifecycle admission、reservation、stage 和 lock 的组合 owner。

但第二个类型显式注入并强制复用第一个 repository，甚至以 `ContentRepository` alias 表明其内容存储 owner。它没有复制 immutable blob storage，而是在 FileOps mutation bounded context 上增加 reservation、quota、stage、lock 和 write-scope lifecycle。

因此，原审计把它描述为“名称冲突”是准确的；如果暗示存在平行 Artifact storage owner，则证据不足。整改应保持复用关系，优先把 FileOps 类型重命名为表达其真实不变量的名称，例如 mutation artifact lifecycle/staging service，并将其保留为 FileOps internal。不能把两者强行合并，也不能新建第三个 repository facade。

同样，`runtime/artifacts/__init__.py` 导出 GC、layout、repository、publisher、resolver 和 transfer，是否全部属于不合法公共面，必须根据包外生产消费者判断。已确认 Product model artifacts 和 Runtime agent components 会从包根取得具体 layout/publisher/resolver，这证明公共面偏宽；但最终最小 Port 应由这些消费者的真实用例推导，而不是按类名统一禁止。

## 11. 第三轮后的需求修订清单

原审计复审稿还应追加以下明确修改：

1. 保留 196 数字，同时附生成口径和 inventory；
2. SB1.16 拆成 Contracts DTO、Product capability adaptation policy、各 surface 默认值三个 owner；
3. SB1.17 先迁出 Product builtin tool cap，其他 config 是否拆包按 lifecycle/consumer 另证；
4. SB1.18 增加 serialized ABI 与外部 consumer 产品决定，不把 enum 拆分当作机械重构；
5. SB2.2 明确 FileOps repository 已复用 canonical content repository，问题是命名和公共面，不是重复存储实现；
6. 所有“包根导出过多”条目补真实包外生产消费者和拟替换的最小 query/command Port。

## 12. 第四轮补充核验

### 12.1 SB1.5 成立，并包含一个不安全的 TypeGuard

Agent spawn 泛型主链当前只有前半段被正确表达：

```text
SpawnableAgentDefinition[OutputT]
  -> AgentBuilder[AgentConstructionRequest, OutputT]
  -> RunnableAgent[OutputT]
```

但随后存在以下断点：

- `RunnableAgent.provision_spawned_child()` 固定接收 `RunnableAgent[object]`；
- `AgentFactory.child_builder()` 接受 `agent_cls: object`，返回固定 `AgentBuilder[..., str]`；
- 文件声明的 `AgentT` 完全没有进入 Protocol 签名；
- Product `_ChildAgentClass`、`_ChildBuilder` 和 `construct_child` 路径固定为 string output；
- `SpawnContext` 暴露 config、agent path 和 parent cost tracker 的 `Any`；
- `RunnableAgent` 同时暴露 state、context provisioning、cost tracker 和 control binding。

其中 `is_text_runnable_agent(candidate: object) -> TypeGuard[RunnableAgent[str]]` 是更直接的类型安全错误。它只执行 `isinstance(candidate, RunnableAgent)`；runtime-checkable Protocol 最多验证所需属性/方法的运行时形状，不能验证泛型输出是 `str`。因此任意满足形状的 `RunnableAgent[OtherOutput]` 都会被错误收窄为 `RunnableAgent[str]`。

整改不能靠另一个 `cast` 或反射检查返回 annotation。需求应选择并明确：

- 如果当前 Product 只允许 text child Agent，则 authoritative child definition 本身必须是 `SpawnableAgentDefinition[str]`，由注册/catalog 边界保证类型，删除伪 TypeGuard；
- 如果 child Agent 应支持任意 `OutputT`，则 `AgentFactory`、agent class constructor Protocol、builder、control、runtime、handle 和 outcome 必须端到端泛型化。

这一选择影响真实产品能力，不能由实现者自行决定。无论选择哪一种，provision、cost rollup、state query 和 control binding 都应从 `RunnableAgent` 的稳定 run/cleanup 面拆出窄 capability Port；但拆分数量仍需由真实消费者决定。

### 12.2 SB1.8 的 SessionFact 问题成立，且当前运行时检查不能替代 typed Port

`SessionFactSink.commit_fact(event: object)` 被 history、compaction、model routing、Role 和 output 路径用于提交 canonical session facts。Runtime committer 在入口调用 `is_rollout_event()`，其实现通过 `type(event) in ROLLOUT_EVENT_TYPES` 检查一个 class set，然后用 `isinstance` 链投影为 `SessionEvent`；未单独处理的类型最后通过 `cast(SessionEvent, event)` 进入 codec。

运行时 class allowlist 能拒绝未登记对象，但仍存在以下架构问题：

- Contracts Port 无法静态表达哪些 durable fact 合法；
- 新增 class 到 allowlist 与 committer/codec 支持不是同一个 exhaustive type relationship；
- `cast` 依赖人工保持 allowlist 和 `SessionEvent` union 同步；
- consumer 可以向正式 Port 传任意 object，只在运行时失败；
- observation event 到 persisted session event 的投影关系没有由 authoritative typed command 表达。

因此 SB1.8 的 SessionFact 部分应保持 P1；与 SB0.5 strict codec 联动时可升为 durable 主链的前置切片。建议 Contracts 定义明确的 `SessionFactInput` 封闭 union，或按真实 use case 定义少量 typed commit command。Runtime committer 负责从 accepted input 到 canonical persisted `SessionEvent` 的 exhaustive projection，codec 只接收 canonical union。

不能把所有 observation event 都自动变成 durable fact，也不能复用宽 telemetry emitter 作为 commit seam。

### 12.3 Telemetry 的 `object/Any` seam 违反内部 typed-event 纪律，但不必强制一个全局封闭 union

`TelemetryEmitter.emit(event: object)`、Kernel observer callback 和 `emit_event(event: Any)` 确实让 typed events 在内部链路退化为宽类型。原审计指出的问题成立。

但“ObservationEvent 使用封闭 union”只是一个候选方案。Telemetry 是跨 bounded context 的开放 observation plane；一个全局封闭 union可能重新制造所有领域共同修改点。更适合的方向可能是：

- `TelemetryEmitter[EventT_contra]`/typed subscription 保持事件类型关系；
- 每个 subscription manifest 声明自己接受的 canonical event family；
- Runtime fabric 在 typed handler/narrower 边界保持类型，不把事件存为裸 object；
- 只有最终外部 telemetry SDK/wire adapter 才编码动态 payload。

需求应先列出现有 emitter、subscriber、fan-out 和 external adapter 调用链，再决定采用分领域 union、泛型 emitter 或二者组合。不能为追求“封闭”建立新的全局 ObservationEvent 枚举。

### 12.4 SB1.15 成立：Runtime error 包是跨层 re-export 入口

`runtime/errors/__init__.py` 从 Contracts 的 agent、config、foundation、model、output、runtime、task 和 tool bounded context 导入大量 authoritative error，再统一从 Runtime 包根导出。生产消费者已经依赖该错误 owner 假象：

- Orchestration agent/workflow/background-task 模块从 `runtime.errors` 导入 Contracts-owned agent/task/graph error；
- Product builtin tools、media、web search 和 model registry 从 `runtime.errors` 导入 Contracts-owned tool/model error；
- Runtime 自己也混合从该聚合面取得 Contracts error 与 Runtime-local classifier/recovery implementation。

虽然 Orchestration/Product 依赖 Runtime 在五层方向上允许，但从错误 owner 导入违反 authoritative package 和最小公共面规则，并使 `runtime.errors` 成为所有领域共同入口。

SB1.15 应保持 P1。迁移应分两类：

1. 跨边界 domain error 由消费者直接从对应 `contracts.<domain>.errors` 导入；
2. Runtime-local adapter/classification/recovery error 留在拥有其语义的 Runtime bounded context，不继续通过全局 `runtime.errors` 聚合。

原文件仍含 `common.utils`、`common.logs`、`config2`、`llm_config` 等已删除或旧 owner 注释，进一步证明该聚合面具有历史迁移残渣。删除时不得在 Contracts 或 Product 再建立同义总入口，也不得保留 re-export alias。

### 12.5 SB1.7 成立，但 CodeMap 与 LSP 不应被当成同一个拆分任务

CodeMap Port 当前存在明确宽面：

- `scan_all_async() -> object`；
- `build_turn_source(**kwargs: Any) -> object`；
- 同一个 factory 同时构造 indexer 和 turn-context source。

LSP Port 则同时定义 diagnostics provider、committed-event handler 和 service factory，并以 `config: object` 接收 Product 解码后的配置。两者都混合 construction 与 query/context source，但它们不是同一 bounded context，仅因都属于 code intelligence 不应合成一个巨型 service。

原审计提出的 `CodeMapQueryPort`、typed `ScanResult` 与 context source factory 方向合理。实施前仍需核实 `runtime/context/turn_context` 已有的 source contract，优先让 Product factory返回该 canonical type，而不是新建同义 source Protocol。LSP 应分别确认 ingestion event family、diagnostics snapshot 和 provider lifecycle；不能只把 `object` 改成另一个 facade。

### 12.6 SB1.9 的 legacy `LLMClient` 当前没有生产消费者，应优先删除而不是扩建

`contracts/ports/model/client.py::LLMClient` 使用 `msg: Any`、`tools: list[dict]` 和 `**kwargs: Any`，从签名看确实不是合格的正式模型边界。但全仓搜索只找到它自身的定义，没有生产消费者。

与此同时，当前模型执行主路径已经使用 `FinalizedInferenceRequest`、`ResolvedInferenceTarget`、`InferenceAttemptFence` 和 typed `infer()` Port。虽然 `FinalizedInferenceRequest.payload/messages` 本身仍含 `Any`/裸 dict，需要另行闭合 canonical request schema，但不能以此为由保留第二个未使用 `LLMClient`。

因此 SB1.9 对 Model Port 的整改应改为：

1. 确认没有测试或公开 SDK 承诺依赖 `LLMClient`；
2. 若无真实消费者，直接删除该 Port 和包根导出；
3. 将 canonical request/response 类型化工作并入 SB1.10/当前 inference execution 主链；
4. 不创建新的 `CanonicalModelRequest` facade 与既有 `FinalizedInferenceRequest` 平行存在。

这正是“复用 canonical owner、删除未使用类型”原则的具体应用。

### 12.7 Skill Port 已经部分拆分，原审计不能要求重复建立同义服务

`contracts/ports/skill/registry.py` 已经分别声明：

- `SkillCatalog`；
- `SkillPromptProvider`；
- `SkillService`；
- `SkillServiceFactory`。

原审计要求“Skill 拆 CatalogQuery、PromptProvider、Lifecycle”，其中 Catalog 和 PromptProvider 已经存在。真实问题是 `SkillService` 又通过 `pool`/`injector` property 暴露内部 service graph，同时承担 ready/enabled/reload/source dirs；factory config 仍为 `Any`。

因此整改应复用现有 `SkillCatalog` 和 `SkillPromptProvider`，而不是新建改名后的 `CatalogQuery`/`PromptProvider`。需要判断 Runtime 的真实消费者究竟需要：

- prompt index query；
- skill lookup；
- explicit activation/reload command；
- immutable status/source snapshot。

然后收窄或删除 `SkillService` locator property，并为 Product factory 输入使用 canonical typed config。`source_dirs()` 是否属于 public lifecycle query，也必须由 file-watching/reload consumer 证明。

## 13. 第四轮后的新增阻断修订

原审计进入复审前还必须：

1. 将不安全 `is_text_runnable_agent` TypeGuard 单列为 SB1.5 的具体缺陷；
2. 对 child Agent 输出能力作出 `str-only` 或端到端泛型的明确产品决定；
3. 为 SessionFact 定义 typed accepted union/commands，删除 `object + class set + cast` 主链；
4. 不用一个全局封闭 ObservationEvent union替代 telemetry 的宽类型，应先设计分领域泛型关系；
5. 将 Runtime error re-export 的生产消费者纳入 SB1.15 迁移清单；
6. 删除无生产消费者的 legacy `LLMClient`，不要再造平行 model request facade；
7. 复用已存在的 `SkillCatalog`、`SkillPromptProvider` 和 turn-context source contract，避免同义 Port。

## 14. 已确认产品决定：BackgroundTask 采用 Agent-owned pool

本节记录用户在 2026-07-31 明确确认的产品决定。该决定优先于原审计和此前 AGENTS.md 中的进程单例描述，并已同步写入 `AGENTS.md` 8.3。

### 14.1 最终 owner 与 scope

最终方案是：

> 每个逻辑 Agent/Role 独立拥有一个 canonical `BackgroundTaskPool`；Agent Swarm 集中治理，但不集中 BackgroundTask ownership。

具体不变量：

- 每个 Agent pool 独立拥有 task registry、task sequence、asyncio task、operation、output、progress、notification、wake callback、result pointer 和 cleanup；
- 不同 Agent 不共享 mutable pool state；
- `TaskId` 只要求在所属 Agent pool 内唯一；跨 Agent 引用必须携带稳定 Agent identity；
- Agent release 只结算或取消自己的 pool，不得影响 sibling、parent 或 child Agent；
- BackgroundTask 保持 process-local，不承诺进程崩溃恢复；
- 需要跨进程恢复、residency eviction 后继续或 durable effect reconciliation 的工作必须在提交前进入 WorkflowRun；
- Workflow continuation、checkpoint、definition 和 resume state 不得寄存在 BackgroundTaskPool。

### 14.2 Swarm 的集中治理边界

Swarm supervisor 可以集中拥有：

- 进程/树/子树总并发 admission；
- CPU、内存、Token、成本和时限预算；
- 公平调度和背压政策；
- root/subtree cancellation command；
- admission permit 的 acquire/release accounting。

Supervisor 不得拥有：

- 各 Agent 的 task map 或 operation object；
- task output/result/notification；
- Agent pool 内部 sequence；
- pool cleanup 或 task settlement state。

若需要进程级并发控制，应注入窄 typed admission/permit Port。permit owner 只治理资源，Agent pool 仍是 task lifecycle 唯一 owner。禁止建立共享 task registry、共享 result store 或第二套 task state machine。

### 14.3 Residency 决定

存在未结算 BackgroundTask 的 Agent 必须 pin residency，不得 eviction。任务全部结算后才允许卸载。

当前实现尚未满足这一点：

- `AgentRuntime.is_unloadable()` 只检查 final status、active turn、mailbox 和 message buffer，没有检查 Agent-owned BackgroundTaskPool；
- residency manager 在判断 unloadable 后调用 `Role.prepare_for_eviction()`；
- Role eviction cleanup 会调用 `bg_pool.aclose()`，从而取消并结算当前 pool。

因此当前路径可能把“residency eviction”错误地变成 BackgroundTask cancellation。后续实施必须让 unloadability query 读取一个窄的 Agent-work pin/snapshot，而不是让 Orchestration 取得具体 pool。最小语义是：

```text
background_pending == true  -> not unloadable
background_pending == false -> 继续检查既有 turn/mailbox/message 条件
```

不需要为此建立进程级 pool 或 rebind registry。

### 14.4 对原审计 SB1.14 的修正

原审计要求“Orchestration process service + AgentScope handle；Product 只装配一次”。这一目标已经被明确否决，应从需求中删除。

当前“每个 Role component 调用 Product builder 创建独立 pool”的 scope 方向与新产品决定一致，不再是债务。真正需要整改的是：

1. Product `AgentBackgroundTasks` 把 Workflow continuation/inspection/run adapter 混入 Agent-owned BackgroundTask facade；
2. pool metadata 的 `agent_id` 成为可选字段，而在 Agent-owned pool 内它应由 owner 固定或根本无需逐 task 重复传入；
3. Contracts `BackgroundTaskService` 只声明 wait/status/close，却不声明真实 submit/query/cancel use case，导致生产代码依赖 concrete duck-typed 方法；
4. `submit()`、`resubmit()`、`graph_meta` 和 `**options` 使用宽动态边界；
5. eviction 没有对 pending BackgroundTask 建立 pin；
6. package root 导出 pool、store、monitor、decorator、operation 等大量内部类型。

修订后的 SB1.14 应表述为：

> 保持每 Agent 一个 canonical BackgroundTaskPool；从该 pool 删除 Workflow durable/resume ownership，补齐 typed Agent-owned command/query/settlement 面和 residency pin，并收窄包根公共面。Swarm 资源治理通过独立窄 admission Port 注入，不集中 task ownership。

### 14.5 必要验收

实施验收至少覆盖：

- 两个 Agent 的 task id 可以同序号存在，查询/取消/结果不会串 scope；
- Agent A release 只取消或结算 A 的任务，Agent B 不受影响；
- pending task 期间 residency eviction 被拒绝，任务结算后可 eviction；
- root/subtree cancellation 经 typed command 到达目标 pool，不直接修改内部 task map；
- permit acquire/release 在成功、失败、超时、取消和异常 cleanup 下严格配对；
- pool cleanup 等待 operation stop、asyncio task、output drain 和 terminal notification settlement；
- BackgroundTask API 无 Workflow definition、continuation、checkpoint 或 resume state；
- 跨进程工作只能提交 WorkflowRun；
- 不存在进程级 BackgroundTask singleton、scope registry 或共享 result map。

## 15. 第五轮补充核验

### 15.1 SB1.6 成立，但应在现有 Kernel operation owner 内类型化

Kernel 当前存在两类宽 bundle：

- `InferenceSubsystems` 接收 Runtime/Product config、executor、skill manager、turn-context bus 和 command channel，全部或大部分为 `Any`；
- `GraphAssemblyInputs` 将 observation、inference、actions、context provider、completion policy、channel、inference engine 和 background service 以 `Any` 传给 graph builder。

这让 Kernel 通过 `Any` 隐藏对上层具体对象形状的依赖，违反“Kernel 只能通过 Contracts-owned Port 接收外部能力”。但原审计所称 `GraphAssemblyInputs` “直接穿透 Runtime 对象图”需要更精确：其中 observation/inference/actions/output 已经是 Kernel-owned operation object，问题主要是类型丢失和 assembly contract 过宽，不是这些 operation 都应迁到 Contracts。

整改应分两类：

1. Kernel 内部 operation 之间的关系，用 Kernel-owned具体 operation type/Protocol 表达；
2. Kernel 需要 Runtime 能力的 seam，例如 tool execution、background query、turn context、transaction/checkpoint，复用现有 Contracts Port 或在消费方定义最小 Port。

不能把整个 `InferenceSubsystems`/`GraphAssemblyInputs` 改名为一个新 `KernelServices` facade，也不能把纯 Kernel operation 为了形式统一全部提升到 Contracts。

### 15.2 SB1.10 的 queue 泛型问题成立，但“Product 拼装 Runtime concrete”不是天然违规

`FairAdmissionQueue` 和 `QueueEntry` 使用 `payload: Any`，而同一 queue 被 inference、command、session 等不同 runtime 使用。这里存在清晰变化关系，应该把 queue 泛型化为 `FairAdmissionQueue[PayloadT]` 和 `QueueEntry[PayloadT]`，让 enqueue/dequeue/dispatcher 保持 payload 类型。

`GenerationView.bindings: Mapping[str, Any]` 也把 generation artifact 的具体 binding schema退化为动态 mapping，需要按 domain 使用 canonical typed binding snapshot 或 tagged union。

但 Product daemon/application 直接构造 Runtime inference、command、session、transfer runtime 本身符合五层 composition 方向：Product 的职责正是选择并装配下层实现。原审计要求“Runtime 提供 cohesive hosted service，Product 只选择 backend 和装配”不能被解释为把 Product composition decision 下移 Runtime。

Shared daemon 与 embedded generation 是否属于平行 composition root，必须比较：

- 是否是两个明确不同的 deployment mode/application root；
- 是否复用同一 Runtime factory/manifest；
- generation、quota、health、permit、receipt 和 lifecycle identity 是否一致；
- 是否可能在同一 application scope 同时构造两套 owner。

只有后两项出现双 owner或语义漂移才成立为 SB1.10 composition 债务。不能仅因 Product 文件显式 import 多个 Runtime concrete module 就判错。

### 15.3 SB1.13 应先判定旧 Environment 路径是否仍为生产能力

`AgentEnvironment` 确实自行构造 `AgentControl`/`ResidencyStore`，公开 control、Role map 和完整 Role，并通过 `getattr/hasattr` 解释 Role。`product.interaction.MoteEnv` 继承该路径。

但生产搜索没有发现 `AgentEnvironment(...)` 或 `MoteEnv(...)` 的实例化；当前 CLI 将 typed human channel直接绑定到 `role.state.env`。现有直接构造点位于 ztest。由此看，Environment facade 很可能是残留旧入口，而不是需要重新设计的活跃 composition surface。

因此，原审计要求为它注入新的 `AgentHostingPort`、`MessageDeliveryPort` 和 `HumanInteractionPort` 可能反而保留无消费者架构。正确顺序是：

1. 确认是否有仓外稳定 SDK/API 承诺；
2. 若没有生产/外部消费者，删除 `AgentEnvironment`、`BaseEnvironment`、`MoteEnv` 和对应 package-root export，并迁移/删除只验证旧路径的测试；
3. 若确有已确认消费者，再基于该消费者定义最小 Port，并由唯一 Product composition 注入 control。

不得为了测试便利给无生产消费者的 facade 建立新服务面。

## 16. 第六轮补充核验

### 16.1 SB2.7 成立：`runtime/config` 是 owner 聚合目录

`runtime/config/` 当前同时包含：

- Device/ADB backend 配置；
- Hook command/matcher 配置；
- LSP server process 配置；
- MCP transport/OAuth/server 配置；
- Langfuse 配置；
- Sentry 配置。

这些类型没有共同 identity、lifecycle、状态机或消费者，只因“都是配置”聚合在一个目录。其生产消费者也分别位于 device backend、hook、LSP、MCP、telemetry observability 和 Product config。这符合按技术类别形成 grab bag 的判定，SB2.7 成立。

但整改不能统一把所有配置迁到 `contracts/config`。应逐项判断边界：

- 跨层、部署期稳定且由多个层引用的纯数据 shape，可以归 `contracts/config/<domain>`；
- 只服务一个 Runtime implementation、不会跨包承诺的 option，归对应 Runtime bounded context；
- source precedence、默认 backend、可信路径、secret resolution 和 Product builtin choice，归 `product/config`/composition；
- wire/plugin 输入在 Product adapter 解码后，Runtime 只接收 canonical typed config。

已确认的具体问题包括：

- `DeviceConfig` 写入 `auto/android/none`、ADB、emulator 和 AVD 等具体 backend/Product 默认；
- `HookCommandHandler.command` 是 shell 字符串，涉及 Product trust/permission policy，不能只作为普通 Runtime config 直通；
- `MCPServerConfig` 同时携带 transport、command、env、OAuth 和 timeout，需要在 Product provenance/trust gate 后形成 Runtime activation manifest；
- Langfuse/Sentry config 应靠近各自 telemetry adapter，而不是共享一个 `ConfigModel` 别名目录；
- 多个 docstring 仍声称文件位于已删除的 `common/schema`，属于迁移残渣。

实施必须逐 consumer 迁移并删除 `runtime/config` 聚合入口，不能新建 `shared config` 包或保留 re-export。

### 16.2 SB2.8 部分成立：watch mechanism 唯一，但 config 混入 Product reload policy

`runtime/file_watch/config.py` 与 `runtime/watching/` 目前不是两套 watcher 实现。前者只有 config shape，后者拥有 `FileWatcher` 和 `FileWatchService` mechanism。因此“两个 watcher state owner”并未成立，不能描述为重复实现。

真正问题是边界拆分错误：

- `enabled`、watch roots、ignore patterns、poll interval 描述 watcher mechanism/deployment input；
- `reload_skills`、`reload_config`、`reload_mcp` 描述 Product extension/config lifecycle policy；
- config docstring 仍引用已删除 `common/schema`；
- Product CLI 和 Runtime RoleSchema 直接依赖错误位置的 config type。

修订后的要求应是：

1. 保持 `runtime/watching` 为唯一 watcher mechanism owner；
2. 为 watcher mechanism 使用窄 typed activation spec；
3. Skills/config/MCP reload choice、trusted roots 和默认值由 Product composition 拥有；
4. Product 将批准后的 watch subscriptions/callback bindings 注入 Runtime；
5. 删除 `runtime/file_watch` 一级包，不保留 alias。

这属于 declaration owner 错置和 Product policy 混入，不是两套 watcher 真相源。

### 16.3 SB2.6 初步核验（已由第 19.1 节最终结论取代）

`runtime/projections/registry.py` 的 registry/reconciler 拥有明确共同流程：

```text
RuntimeProjectionRequest
  -> resolve exact projector/schema
  -> materialize projection
  -> durable artifact publication intent
  -> projection journal ack/retry/dead-letter
```

Canvas、Notebook 和 Artifact projector 都参与该 checkpoint→artifact projection pipeline；Session projection也由 runtime session replay/agent component 消费。仅因输出领域分别叫 session/canvas/notebook/artifact，不能推断它们拥有独立 lifecycle 或必须迁出。

原审计要求“各 projection adapter 归所属 owner”目前缺少以下证据：

- 每个 projector 是否共享同一 request、journal、publication 和 retry guarantee；
- projector 是否只包含领域投影逻辑，还是拥有独立状态机/store/lifecycle；
- 迁移后谁注册 projector、谁拥有 schema identity、谁执行 reconciliation；
- 是否会造成 interactive canvas/notebook 包反向依赖 Runtime projection control plane。

本轮只能得出暂缓拆包的中间结论；第 19.1 节在继续核对 input、state、lifecycle 和 consumer 后已完成裁决，以第 19.1 节及第 20 节矩阵为准。

### 16.4 SB2.9 应拆成两个不同结论

`runtime/presentation.py` 只包含英文 `plural/count_noun/verb_agree`，生产消费者主要是 Product builtin tools/model provider，以及少量 Runtime UI/tool wording和 Orchestration background decorator。它不拥有 Runtime invariant，而是在低层提供英文 presentation helper。该文件属于错误 owner，应迁移具体 wording 到各 Product surface/tool presenter；不能在 Product 再建一个通用 `presentation helpers` 垃圾模块。

`runtime/text/elision.py` 则不同。它定义模型内容截断的 typed fact、strategy 和确定性 marker，消费者包括 Runtime context token budget、resource spill、tool compression、terminal/kernel/browser output。它围绕“模型/运行输出在预算下保留哪些部分、如何标记 omitted truth”的机制共同变化，不应迁到 Product presentation。

真实问题是 `runtime/text` 包名过于泛化且 docstring 仍称自己位于已删除 `common`。应把 elision 放入拥有预算/输出压缩不变量的 Runtime bounded context，优先评估现有 `runtime/resources`、`runtime/context` 或 `runtime/tools/compress` 中哪个是 canonical owner；若多个消费者共享同一机制，则通过明确的 Runtime service/value contract 暴露，而不是创建新的 `utils/text`。

所以原 SB2.9 的“一并归 Product”方向必须删除。

### 16.5 SB2.5 的包根问题成立，但 `runtime/code_map` 本身高度内聚

`runtime/code_map` 内部的 extractor、language provider、scope graph、model、SQLite store、indexer 和 query facade 共同围绕 repository code graph 的 extraction/index/query identity 工作。从当前依赖看，它是一个真实 bounded context，不是应拆散的技术 grab bag。

问题集中在公共面和 composition：

- `__init__.py` 本身定义 concrete `CodeMap` facade，并导入 extractor、provider、store 和 model internals；
- Product turn-context 直接消费 `CodeMap`/`FileNeighborhood` concrete type；
- Product factory直接消费 Runtime `RepoIndexer` 和 language registry；
- Contracts CodeMap Port 仍返回 `object` 并混合 indexer factory 与 turn source factory。

整改应保留 `runtime/code_map` owner，先定义真实消费者需要的 typed query/index service 和 immutable DTO，再把 store/extractor/provider 留在包内。不能把每个内部模块拆为独立服务，也不能复制 code graph DTO 到 Product。

是否保留一个 concrete `CodeMap` package public service，取决于其方法是否已经是最小 query 面；原审计不能仅因 class 定义位于 `__init__.py` 就要求新建同义 facade。可以确定的是实现应移出包根，包根只导出经确认的稳定 service/DTO。

### 16.6 SB2.4 需要区分 Product composition access 与业务消费者泄漏

`runtime/service_gateway` 包根当前导出 gateway、local journal、planner、snapshot types 和 merge function。Product composition 使用 planner/merge 来装配 gateway；Product media/search service 直接依赖 snapshot representation。

Product composition 导入 Runtime concrete implementation 本身合法。真正需要审查的是：

- media/search 是否只是提供 typed capability contribution，还是依赖 planner 内部 failover layout；
- `ServiceRuntimeSnapshot`/`ServiceFailoverGroup` 是稳定 Contracts DTO，还是 Runtime internal record；
- local journal path/layout 是否被非 composition consumer 使用；
- planner、merge 和 gateway 是否共享同一 failover invariant/lifecycle。

如果 snapshot 是跨 Product→Runtime composition 的正式输入，应将最小 immutable capability manifest 放入 Contracts，并由 Runtime gateway内部构造 planner。若 snapshot 只在 Product canonical composition root 使用，则可以作为 explicit Runtime factory input，但不应通过包根向任意业务 consumer 承诺。

因此 SB2.4 的“公共面偏宽”成立；“包根只能留下 gateway 和 snapshot”仍是候选 API，必须由 media/search/composition 三类真实消费者推导。不要为隐藏 planner 再套一层无状态转发 facade。

## 17. 第六轮后的修订要求

原审计复审稿应继续修正：

1. SB2.7 按 domain 逐项迁移 config，不把所有 config 机械下沉 Contracts；
2. SB2.8 改为 declaration/policy owner 错置，撤销“两套 watcher owner”表述；
3. SB2.6 本轮暂缓裁决；后续证据与最终迁移边界见第 19.1 节；
4. SB2.9 将英文 Product wording 与 Runtime elision mechanism 分开处理；
5. SB2.5 保留 CodeMap bounded context，只收窄 public query/index service；
6. SB2.4 依据 composition 与业务消费者分别设计 public surface，不因 concrete import 本身判错。

## 18. 第七轮补充核验

### 18.1 SB2.10/SB2.13 的 Squilla 证据需要降格

`product/routing/squilla/strategy.py` 确实导入 predictor 的 `_CLASS_TO_IDX` 和 `_apply_flag_overrides`。但 strategy、`ml.predictor` 和 `ml.inference.postprocess` 均位于同一个 `product.routing.squilla` bounded context，共同实现同一 routing decision pipeline。这不是跨层或跨 bounded-context implementation leak。

真实问题是：

- `_apply_flag_overrides` 已有多个同包消费者，却仍被命名为 private；
- route class ordering/index 在 predictor 和 postprocess 中重复派生；
- `ROUTE_CLASSES` 使用可变 `list[str]` 和字符串 identity；
- predictor 内部仍广泛使用裸 config mapping、裸 probability dict 和 string tier/policy。

整改应在 Squilla canonical owner 内提取一个公开、类型化、不可变的 routing policy seam，例如 authoritative route class enum/order、flag-floor function 和 typed policy input/output。不要仅去掉下划线，也不要通过包根 re-export。

原审计由此要求建立跨包 `RoutingModelService/Decision` 证据不足。ML model loading/inference runtime 是否需要独立 service，应由模型 artifact lifecycle、lease、optional dependency activation 和并发消费者决定，不能由一次同包 private import 推导。

### 18.2 SB2.11 成立，但已有 exhaustive dispatcher 可复用

`product/presentation/consumer.py::BaseConsumer` 使用 `getattr(self, f"on_{ev.kind}")` 做反射式 handler dispatch；`ViewEvent.kind` 是开放 `ClassVar[str]`。这使新增/拼错 kind、handler 命名和事件类型关系无法被类型检查器穷尽验证，内部 typed event chain退化为字符串约定。

与此同时，`product/presentation/state/driver.py::apply_op()` 已经通过 `isinstance` exhaustive chain 将 closed `TranscriptOp` union 映射到 typed `RenderSurface` 方法。整改应复用这一模式或建立单一 typed visitor/handler registry，而不是再建第二套 dispatcher。

需要分别处理：

- 进程内 `ViewEvent` union 和 consumer dispatch；
- `TranscriptOp` union 和 surface dispatch；
- ACP/AGUI 等 wire adapter 的 kind/payload codec。

只有最后一层可以使用动态 dict/string wire representation，并必须在 adapter 边界严格 encode/decode。内部不应继续以 `getattr(on_<kind>)` 作为正式扩展机制。

不过 Product consumer 是否必须处理所有事件，与 union 是否 closed 是两个问题。可以保留 typed `on_unhandled(ViewEvent)`/capability filtering 语义，让不支持某类事件的 host 显式忽略；不能以“允许忽略”作为开放字符串 discriminator 的理由。

### 18.3 SB2.12 成立：Toolset 包根 eager import 是已复现 collection failure 的直接链路

`product/toolsets/__init__.py` 在模块导入时加载全部 builtin tool class，包括 Terminal、Device、browser、media 和 Workflow tool。Terminal import 继续加载 Runtime terminal driver/VT，并最终无条件 import optional `pyte`。Product factory/container 又从 package root 导入 catalog/toolsets，因此仅导入 Product composition 就加载 optional backend。

这正是 `ztest/architecture` collection 首个 `ModuleNotFoundError: pyte` 的直接路径。SB2.12 对 `product/toolsets` 的判断成立，且不只是“导出太多”的风格问题，而是 import purity、optional dependency isolation 和 construct/activation 分离违约。

整改必须同时满足：

- package root 不 eager import具体 builtin/optional backend；
- Product canonical composition 显式选择 catalog manifest；
- manifest identity 稳定、确定，不依赖 import 顺序；
-发现 tool declaration 不自动激活进程、设备、browser、MCP 或其他 capability；
- optional implementation 只在批准且被选择的 activation path 加载；
-缺少 optional dependency 只影响对应 capability activation，不污染基础 package import和 architecture gates。

AGENTS.md 禁止以普通动态字符串 import 规避内部依赖，因此实现不能随意把 class path 塞进 dict 后 `importlib`。如果使用 Product-owned manifest/discovery，必须作为明确批准的 plugin/provider catalog，有 typed declaration、stable identity、activation factory 和门禁。另一个可行方向是把轻量 tool declaration与重 backend activation adapter分离，但不能留下两套 tool identity。

### 18.4 Presentation package root 还混合 contract、policy 和 Product 默认值

`product/presentation/events/__init__.py` 同时导出：

- cross-host event DTO；
- capability DTO；
- stateful `CapabilityAdapter` policy；
- Terminal/Textual/Structured Product defaults；
- folding policy/constants。

这进一步支持 SB1.16/SB2.12 的组合判断：问题不是简单减少 `__all__`，而是 authoritative owner 混合。迁移应按第 10.2 节的三类 owner完成，并删除旧 Product contract入口，不保留 re-export。

## 19. 第八轮补充核验

### 19.1 SB2.6 修订后成立：`runtime/projections` 混合了两个不同 bounded context

源码显示该包并非单一 projection lifecycle，而是至少包含两组不同不变量：

- `RuntimeProjectionRegistry`、`RuntimeProjectionReconciler` 及 Canvas/Notebook projector 共同完成 runtime checkpoint 到 artifact materialization、ack 与 reconcile；
- `SessionProjectionState`、`SessionLiveProjection`、`reduce_session_envelope` 从 Session event stream 维护进程内 read model。

两者的 authoritative input、state、lifecycle 和 output 均不同。前者应继续作为通用 runtime artifact projection/reconciliation 能力保持内聚；Canvas 与 Notebook projector 不能仅按输出名称散落，因为它们共享 checkpoint→artifact→ack/reconcile 管线。后者则应迁入 `runtime/session/` 的 canonical owner，由 Session event schema 和 replay/read-model 语义共同演进。

迁移必须同时收窄包根公共面，并以 typed projector registration manifest 替代字符串 composition；不得保留旧 import alias 或第二套 projection registry。

### 19.2 SB1.9/SB2.15 的 Hook 结论需要按控制面风险收窄

`HookManager` 的 registration、matching、execution 与 result folding 可以共享同一 Runtime lifecycle；“一个 manager 做多件事”本身不足以证明需要拆成多个服务。真正成立的问题是：

- Hook contract 仍使用 `config: Any`、字符串 event、裸 payload/result dict 与 `getattr`；
- `HookSubscriber` 通过字符串映射和 lambda 串接内部事件，typed event chain 在边界处丢失；
- command hook 直接调用 `asyncio.create_subprocess_shell`，绕开 canonical command classifier、permission、approval 与 sandbox path；
- 所有 hook failure 都折叠为 `EMPTY`。对纯观察型 post-event hook 可以是明确声明的 best effort，但对 `PreToolUse` 等影响执行许可的 control hook 会形成 fail-open 风险；
- raw shell command 来自配置，必须经过 Product provenance/trust/approval，而不能由 Runtime 将配置文本直接当作可信命令执行。

整改方向不是新造一个平行 `HookCommandRunner`：保留一个内聚的 Hook policy/runtime owner，将外部命令执行作为独立 trust adapter 注入，并复用 canonical governed command runner。跨边界输入改为 closed、typed `HookInvocation` union 和 typed handler manifest；观察 hook 与控制 hook分别声明 failure disposition，安全相关控制 hook默认 fail closed。旧注释中对已删除 `common` 的引用应随同切片清除。

## 20. 全部 SB 项最终裁决矩阵

说明：

- **接受**：问题事实和方向成立，可进入需求收敛；
- **修订后接受**：核心债务成立，但事实、严重度或实施方案必须按本评审修改；
- **驳回并替换**：原目标与已确认产品决定冲突；

| 编号 | 裁决 | 最终说明 |
| --- | --- | --- |
| SB0.1 | 接受 | Workflow 当前不是 durable execution service；需先固定 versioned definition/run/checkpoint/effect/fence contract。 |
| SB0.2 | 修订后接受 | 三类 capacity、durable lineage、delivery 保证问题成立；当前是缺少 logical identity cap，不是一次偶发绕过。 |
| SB0.3 | 修订后接受 | 不是 silent fallback，而是 warning 后自动弱化 guarantee；需 typed activation result 与 fail-closed。 |
| SB0.4 | 修订后接受 | Runner 混合 trust domain 成立；未证明 permission bypass 前降为 P1。 |
| SB0.5 | 接受 | Durable/wire codec 不严格；必须复用 canonical codec 基础设施，不能每 fact 复制实现。 |
| SB1.1 | 接受 | Runtime composition lease 是 typed relation 丢失的 service locator；按真实 consumer 收窄。 |
| SB1.2 | 接受 | EngineServices/container 聚合 lifecycle 与对象图；不得改名为另一 locator。 |
| SB1.3 | 接受 | CLI 与 session hosting 确实复制 AgentControl composition；gateway 默认 owner 需按 mutable lifecycle 判断。 |
| SB1.4 | 接受 | CLI backend 同时承担 config/composition/session/history/control facade；按已确认 application/session service迁移。 |
| SB1.5 | 修订后接受 | 泛型断链和不安全 TypeGuard 成立；canonical spawn contract 必须端到端保留 OutputT，text-only 只能是 Product specialization，不能反向收窄全局 contract。 |
| SB1.6 | 修订后接受 | Kernel 宽 bundle 成立；Kernel-owned operation 留在 Kernel，只有外部 capability seam 走 Contracts Port。 |
| SB1.7 | 修订后接受 | CodeMap/LSP Port 均过宽，但为不同 bounded context；复用既有 turn-context source contract。 |
| SB1.8 | 修订后接受 | SessionFact object seam 必须闭合；Telemetry 可用分领域泛型 emitter，不建立全局事件共同修改点。 |
| SB1.9 | 修订后接受 | 无消费者 LLMClient 应删除；Skill 复用既有 catalog/provider；Hook manager 可保持内聚，但 invocation 与外部命令 trust boundary 必须类型化、受治理。 |
| SB1.10 | 修订后接受 | Fair queue/bindings 类型问题成立；Product 拼 Runtime concrete 本身合法，是否平行 root 需按 deployment/lifecycle 证明。 |
| SB1.11 | 修订后接受 | ToolExecutor standalone composition、policy defaults 和内部 map泄漏成立；依赖数量本身不是判据。 |
| SB1.12 | 接受 | Product registry 公开 mutable dict；各领域分别封装，不建立 shared registry。 |
| SB1.13 | 修订后接受 | Facade 边界错误，但疑似无生产实例；优先确认删除，不为旧路径新建 Ports。 |
| SB1.14 | 驳回并替换 | 明确采用每 Agent/Role 一个 pool；删除 process singleton/AgentScope 方案，按第 14 节整改。 |
| SB1.15 | 接受 | Runtime errors 是跨领域 re-export 总入口；消费者迁回 authoritative domain error。 |
| SB1.16 | 修订后接受 | Cross-host DTO 下沉 Contracts；CapabilityAdapter 留 Product；surface defaults 归各 Product surface。 |
| SB1.17 | 修订后接受 | Product builtin tool defaults 必须迁出 Contracts；其他 config 是否拆分按 lifecycle/consumer 判断。 |
| SB1.18 | 修订后接受 | 全局 ErrorCode 是共同修改点；先确认 serialized ABI/namespace/version，再单切片迁移。 |
| SB2.1 | 修订后接受 | FileOps 包根偏宽方向成立；必须用真实包外 consumer 推导最小面。 |
| SB2.2 | 修订后接受 | 是命名/公共面问题；FileOps 已复用 canonical content repository，不是重复 storage owner。 |
| SB2.3 | 修订后接受 | Workflow 包根过宽随 SB0.1 一并处理；definition authoring 与 run service边界由 durable contract决定。 |
| SB2.4 | 修订后接受 | Service gateway 公共面偏宽；Product composition import concrete 合法，业务 consumer 不应依赖 internal snapshot/layout。 |
| SB2.5 | 修订后接受 | CodeMap bounded context 内聚；只收窄 query/index public service，不拆散内部模块。 |
| SB2.6 | 修订后接受 | artifact projection/reconcile 保持内聚；Session event read model 迁回 `runtime/session`，不得按 Canvas/Notebook 名称拆散共享管线。 |
| SB2.7 | 接受 | Runtime config 是技术 grab bag；按 domain/owner 迁移，不全部下沉 Contracts。 |
| SB2.8 | 修订后接受 | 不是两个 watcher owner；是 config declaration 与 Product reload policy 错置。 |
| SB2.9 | 修订后接受 | Product wording 与 Runtime elision 是不同问题；禁止整体迁 Product或新建 text utils。 |
| SB2.10 | 修订后接受 | Squilla 宽 ML/policy type 需闭合；同 bounded context private import 不足以要求新 service。 |
| SB2.11 | 接受 | 内部字符串/反射 dispatch 应改 typed exhaustive path；复用现有 apply_op 模式。 |
| SB2.12 | 接受 | Toolset/presentation 等 eager root import成立；已直接导致 architecture collection failure。 |
| SB2.13 | 修订后接受 | 多项 private import 成立，但逐 owner 处理；Squilla 是同 context internal seam，不算跨 bounded-context。 |
| SB2.14 | 修订后接受 | 删除“生产局部 import”误报；只保留 hermetic collection/eager optional import 问题。 |
| SB2.15 | 修订后接受 | Hook owner 可保留，但 command adapter 必须复用受治理 runner，控制 hook fail closed；其他 owner 只按真实 boundary 收窄。 |

## 21. 最终审批建议

基于八轮核验和已确认 BackgroundTask 产品决定，原审计仍不应整体批准为实施规格。建议按以下方式处理：

1. 原文保留为审计输入，不直接成为权威 backlog；
2. 按第 20 节矩阵修订每个 SB 的事实、严重度和目标；
3. SB1.14 必须以 Agent-owned pool 方案完全替换原 process service方案；
4. 删除 SB2.14 local-import 误报；
5. SB2.6 只拆出 Session event read model；保留通用 artifact projection/reconciliation 管线的单一 owner；
6. 每个接受项进入唯一债务总账时，按独立 vertical slice 固定 contract、owner、consumer、lifecycle、删除路径和 gates；
7. 不把本评审继续演变为第二份执行状态账本。需求修订完成后，本文件作为评审证据冻结。
