# Contracts 分包长期治理与实施计划

- 状态：Draft v10；条件可执行：Phase 0A 可执行；Phase 0B 根据 0A facts 确定性生成冻结决策；Phase 0C 验证治理设施；Phase 1～8 仅执行 manifest 中 approved 且全部前置 verified 的 cutover
- 基线日期：2026-07-29
- 治理范围：`mote/contracts/`
- 关联分层：`contracts <- kernel <- runtime <- orchestration <- product`

本文定义 `mote.contracts` 的长期包治理目标、领域生成规则、迁移批次、机器门禁和公共 API 生命周期。逐模块事实生成视图见 [`contracts-package-governance-inventory.md`](./contracts-package-governance-inventory.md)。文档只定义规则；具体 Phase/cutover 的批准状态以机器 manifest 为准。未知事实可以在 Phase 0A 中被记录，但 Phase 0B 必须全局清零；未知事实不触发整份计划重新设计，却会通过 0B 总门禁阻止 0C 和全部生产迁移。

本文只治理 Contracts 层内部组织和误放能力，不重新评审五层架构，不借拆包改变模型 wire 协议、持久化格式、事件 tag、错误 code、配置默认值或用户可见行为。

目标不是承诺未来十年目录不变，也不是把现有横向目录机械改名，而是建立一套能长期阻止结构债务无声增长的领域地图：不了解实现的人仅看 import path，就能判断一个契约属于什么功能、由谁拥有、允许被谁依赖，以及新增需求应该进入哪里。

### v2 评审吸收摘要

| 评审意见 | v2 处理 |
| --- | --- |
| 目标领域未覆盖全部能力 | 采纳；增加覆盖当前全部 Python 模块的 disposition 附件，一级领域改为候选集合，Phase 0A/0B 复审后冻结 |
| Permission/Approval/Interaction/Tool owner 冲突 | 采纳；新增独立 `authorization` 候选领域，Interaction 只拥有人类请求/响应，Tool 只消费授权决策 |
| Runtime/Session/Interaction/Surface/Handoff 概念互相拥有 | 采纳；Handoff 改为 composition contract，基础领域只暴露窄 ID/value，Phase 0 用字段级草图验证依赖边 |
| 深层模块路径成为长期 API | 采纳；领域 `__init__.py` 提供显式、精选、快照化 facade，内部模块路径不承诺稳定；根 facade 仍禁止 |
| 原子删除旧路径缺少发布策略 | 修正采纳；`mote.contracts` 正式视为公共 API，通过 major version、迁移映射与机械工具迁移，不保留跨 major 永久 alias |
| Event registry 不可实施 | 采纳；补充唯一装配入口、manifest、不变量、未知/旧/重复 tag、静态 union 与产品一致性规则 |
| Foundation 应只允许标准库 | 采纳；增加 500 LOC、20 个公开符号绝对审计阈值 |
| 结构预算不应硬失败 | 采纳；数值全部降为评审触发器，硬门禁仅保留依赖、循环、身份和注册完整性 |
| “只改一个领域”可能诱导错误聚合 | 采纳；改为跨三领域时触发事务边界与上层组合评审 |
| File 不适合作为首个试点 | 采纳；Phase 1 后依据全量 inventory 选择低风险垂直切片，验证模板后再迁 File |
| 插件持久化事件边界不明 | 明确：核心 journal 事件集合封闭；插件可发 namespaced telemetry，必要持久数据使用核心拥有的 opaque `ExtensionFact` |

### v3 复审吸收摘要

| 评审意见 | v3 处理 |
| --- | --- |
| 模块 inventory 粒度不足 | 采纳；Phase 0A 必须生成逐公开符号 ownership inventory，模块清单只作为覆盖索引 |
| ContentIdentity 不应归 Artifact/Foundation | 采纳；增加独立极小 `content` 领域 |
| Service 缺少 owner 且反向依赖 Model failover | 采纳；增加 `service` 领域，通用 failure/retry 不借复用进入 Foundation，消除 service → model |
| Handoff composition 仍是跨域垃圾桶风险 | 采纳；取消通用 `composition/`，拆为 `interaction.handoff` 与 `runtime.handoff` 两种语义 |
| 一级矩阵粒度过粗 | 采纳；一级矩阵只作总览，永久门禁使用模块级允许边 |
| Protocol owner 规则不充分 | 修正采纳；语义 owner 归需求方，但按 AGENTS 硬约束物理存放于 `contracts/ports/<domain>/`，禁止全局 facade 和协作者聚合 port |
| ModelGateway 是 Runtime 装配对象 | 采纳；目标改为窄 model inference port，artifact/session/transformer 留 Runtime 编排 |
| WorkflowControl、RunContext 不满足准入 | 原则采纳；前者迁 Orchestration，后者按符号拆成稳定 identity 与进程内装配对象，禁止原样迁入 Contracts |
| Think/Action/Turn owner 未闭合 | 采纳；增加 `execution` 候选领域，但只有跨 Kernel/Runtime persistence boundary 的 DTO 可进入 |
| 配置不能因“部署配置”自动进入 | 采纳；deployment 只聚合已通过准入的领域 config，Product/Runtime 私有配置不上移 |
| 公共模型缺少质量门禁 | 采纳；增加 Any、裸容器、ID、时间、金额、enum、discriminator 和 codec version 规则 |
| Phase 0 任务混杂 | 采纳；拆为 0A 事实基线、0B 领域设计、0C 迁移门禁 |
| 彻底删除 `ports/events/config` | 不采纳；与当前 AGENTS 硬约束冲突，保留为按领域分区的受控基础设施索引，不允许恢复横向大仓语义 |

### v4 复审吸收摘要

| 评审意见 | v4 处理 |
| --- | --- |
| 状态误示 ownership 已闭合 | 采纳；明确只批准 Phase 0A，逐符号 inventory/模块矩阵待完成 |
| 缺少机器可读模块矩阵 | 采纳；增加 dependencies TOML，架构测试直接读取 |
| 逻辑领域分布于四棵目录 | 采纳；增加 domain manifest 连接 DTO/ports/events/config，禁止交叉 re-export |
| Candidate DAG 与源码不一致 | 采纳；补 Session→Content，并将 Model/Service 媒体引用边列为 Phase 0B 必决项 |
| Event owner 永久冻结过强 | 采纳；wire namespace/tag 稳定，semantic owner 可经 ADR 迁移并保留历史 |
| ExtensionFact 缺少安全契约 | 采纳；限定 bounded JSON 或 content-addressed payload ref，禁止 pickle/arbitrary object |
| Stable symbol 缺少 canonical import | 采纳；API manifest 为每个 symbol 指定唯一 canonical import 与稳定 symbol ID |
| Inventory 字段混杂 | 采纳；机器 inventory 拆 semantic owner/layer/module/status/evidence 与兼容资产集合 |
| `keep` 容易被理解为路径不动 | 采纳；统一为 `retain-contract` |
| Protocol 缺少消费方证据 | 采纳；记录 production/test consumer、implementer 与 consumer layer |
| Hook/Extension 命名不一致 | 采纳；当前统一为 `hook`，除非 Phase 0A 证明存在更广统一语义 |
| Phase 1 可能先破坏隐式 identity | 采纳；增加 discriminator/digest/marker/golden fixture 先决条件 |
| Phase 3/7 Agent 重复、Phase 8 命名不准 | 采纳；Phase 7 只清剩余跨域聚合，Phase 8 改为发布与旧结构清理 |

### v5 顺序可执行性复审摘要

| 评审意见 | v5 处理 |
| --- | --- |
| 脏工作树基线不可复现 | 采纳并收紧；基线覆盖 tracked/untracked/deleted、文件 mode 与内容，不能只 hash `git diff` |
| Phase 0B 混入实现 | 采纳；0B 只批准替代模型、字段草图、目标 Phase，不修改生产边界 |
| 中间 Phase 与 major release 脱节 | 采纳；0C 建立 next-major release train，Phase 1～7 不发布当前 major 正式版 |
| Authorization 先于 Interaction approval | 采纳；Phase 3 增加最小 Interaction approval 切片 |
| Agent spawn 先于 Conversation | 采纳；Phase 3 只迁 identity/catalog/factory，spawn 延至 Phase 5 |
| Conversation 先于 Tool identity | 采纳；Phase 5 先迁 Tool identity/call/result 叶子，再迁 Conversation |
| Phase 5 无法删除整个 schema | 采纳；Phase 5 只清精确子集，`schema/` 删除唯一归 Phase 7E |
| Runtime 先于 Surface identity | 采纳；Surface identity/frame/input 提前到 Phase 6B |
| Event registry 无实施阶段 | 采纳；增加独立 Phase 2B event infrastructure 垂直切片 |
| 旧目录重复删除 | 采纳；每个 legacy root 只有唯一 deletion owner，Phase 8 只验证归零 |
| move-up 目标仍含候选项 | 采纳；Phase 1 前必须冻结 target owner/module、消费迁移集和测试集 |
| 各领域缺少统一迁移协议 | 采纳；所有迁移切片强制使用同一七步协议 |
| ARCHITECTURE 同步过晚 | 采纳；Phase 0B 先修正上位规范，每个阶段同步快速定位，Phase 8 只做最终收尾 |

### v6 实施安全性复审摘要

| 评审意见 | v6 处理 |
| --- | --- |
| 七步协议产生短暂双真相源 | 采纳；改为准备、原子 cutover、验证三段，生产定义只在 cutover 中移动一次 |
| move-up 导致 Contracts 反向依赖 | 采纳；增加 Contracts consumer closure，非空则同切片处置或推迟 |
| 迁移工具出现过晚 | 采纳；0C 建工具骨架，每个 cutover 同步映射，Phase 8 只打包冻结 |
| Runtime 读取 zdocs 风险 | 采纳；治理 manifest 仅供 CI，Runtime 使用静态 Python 装配，测试双向比对 |
| 治理 manifest 无自身版本 | 采纳；所有文件共享 versioned envelope，生成字段与人工字段分权 |
| Phase 8 release gate 不完整 | 采纳；增加 wheel/sdist、隔离安装、恢复、迁移工具及分支验证 |
| public symbol 无可执行定义 | 采纳；明确证据集合与排除项，未知外部使用统一由 major 治理 |
| 根与索引 facade 收敛过晚 | 采纳；每个 cutover 同步删除对应导出，package shell 使用 `retain-package` |
| 每条依赖边强制 ADR | 修正；普通边引用 domain policy，高风险边才要求 ADR，临时边必须有退出 Phase |
| Inventory 具体 owner/disposition 错误 | 采纳；修正 event subscription、background task、transformer 和 journal 边 |
| 测试矩阵依赖人工判断 | 采纳；Phase 0A 生成 symbol/edge → test suites 的可执行矩阵 |
| 回滚窗口表述过宽 | 采纳；仅当前切片验收前可直接回滚，下游开始后按 DAG 逆序处理 |

### v7 D6 复审吸收摘要

| 阻断项 | v7 处理 |
| --- | --- |
| move-up 只检查 Contracts consumer | 采纳；增加 lowest consumer layer、目标层合法性和完整 consumer migration closure 双门禁 |
| tokenization/docstring 迁 Runtime 违法 | 采纳；按最低消费者拆分到 Kernel/Runtime，Phase 1 不再硬编码模块名单 |
| migration manifest 未定义 | 采纳；新增 `contracts-migrations.toml`，状态机、DAG、commit 与批准证据完整登记 |
| 治理工具无唯一入口 | 采纳；固定离线 CLI、子命令和退出码 |
| 事实与决策未物理隔离 | 采纳；生成 facts 与人工 decisions/migrations 分文件，生成器无权覆写裁决 |
| Phase 7D 过大 | 采纳；拆为 7D1～7D4，7E 只删除已清空旧目录 |
| error code 语义不足 | 采纳；新增独立错误身份 manifest，退役 code 永不复用 |

### v8 D7 复审吸收摘要

| 阻断项 | v8 处理 |
| --- | --- |
| 状态名称不一致 | 采纳；持久状态只保留 planned→approved→in_progress→verified，blocked 改为计算状态 |
| 三段提交缺少 commit/evidence 字段 | 采纳；增加 prepared/cutover/verified commit 与 verification evidence |
| 准备提交触发自身 drift | 采纳；先准备、重新 snapshot/check，再基于 prepared commit 与新 baseline 批准 |
| 退出码 2/4 冲突 | 采纳；无关联批准的 drift 返回 2，导致批准失效返回 4 |
| tests 子命令语义不确定 | 采纳；默认只输出命令，`--run` 才执行并保存证据 |
| split-contract 无法一源多目标 | 采纳；migration 改为逐符号 `moves`，每个符号唯一最终定义位置 |
| Phase 依赖只存在于文字 | 采纳；关键阶段关系必须成为 migration DAG 的显式 `depends_on` |
| Event manifest 术语歧义 | 采纳；Runtime 只接收静态 Python decoder tables，治理 TOML 仅供 CI |

### v9 D8 复审吸收摘要

| 阻断项 | v9 处理 |
| --- | --- |
| Interaction/Runtime handoff 被错误合并 | 采纳；拆为 6E1、6E2 与独立 handoff legacy deletion cutover |
| 关键 DAG 缺边 | 采纳；补 Tool remainder、Interaction handoff、Surface lifecycle、release 依赖；Task 依赖由字段扫描冻结 |
| Phase 8 入口门禁不完整 | 采纳；加入全 cutover、blocked、release facts/manifests、deletion 与 major version 门禁 |
| Phase 8 清理游离于 DAG 外 | 采纳；新增 `release-finalization` cutover 和独立只读 release attestation |

### v10 最终顺序执行修订摘要

本版新增机器 Phase Gate、`governance-bootstrap`、类型化入口/出口、facts 完整性、projected graph、mapping/deletion 门禁和完整 Phase 2～8 DAG；固定 Content 试点、一级领域集合和外部 release attestation。Phase 0A 允许显式 unknown；Phase 0B 必须清零全部 unknown、candidate、multi-owner 和非法 move-up，因而任何 unknown 都会经 0B 总门禁阻塞 0C 及全部迁移阶段，但不使计划回到开放设计状态。

---

## 1. 治理目标

### 1.1 总目标

治理完成后必须满足：

1. 业务契约以稳定领域组织；`ports/`、`events/`、`config/` 是 AGENTS 指定的三类物理基础设施索引，必须在其内部按领域分区，除此之外不以技术类型横向分组。
2. 每个领域共同拥有自己的数据模型、身份、事件、错误、策略决策和窄端口；理解一个领域不需要遍历整个 `contracts/`。
3. Contracts 只保存确实跨层、跨进程、跨持久化边界或跨独立实现共享的稳定语义，不成为新的 `common/utils`。
4. 算法、IO、SDK adapter、provider 生命周期、注册表、动态装配、展示格式和产品文案均由实际实现层拥有。
5. 每项契约有唯一领域 owner；同一状态真相、事件身份或 wire shape 不在多个包重复表达。
6. 领域间依赖形成可执行验证的有向无环图；未在封闭矩阵中声明的边默认禁止。
7. `Protocol` 的语义 owner 归定义需求和抽象的一方、不归实现方；物理路径遵循 `contracts/ports/<domain>/`，不得把多个领域协作者聚合为 service locator。
8. 持久化身份独立于 Python 模块路径；移动模块不得隐式改变事件 tag、错误 code、tool identity 或序列化 discriminator。
9. 根包不导出业务符号；领域包只提供机器清单约束的精选 facade，不使用动态 `__getattr__`、自动聚合或 import side effect 掩盖边界问题。
10. 仓内目标态不保留旧 import path、forwarding module、兼容 alias、双实现或永久迁移白名单；对外变更通过 major release、机械迁移工具和有截止期的前一 major 维护策略治理。
11. 每个提交保持仓库可构建、可测试；每个 Phase 可独立验收，迁移指标单调收敛。回滚只在当前切片验收前可独立执行；下游开始后必须按依赖 DAG 逆序处理。
12. 普通需求通常只新增或修改一个领域的契约；涉及三个以上领域时必须说明业务事务边界、依赖方向，以及为什么不能由上层组合完成。该条件是评审触发器，不是质量指标。

### 1.2 “未来十年零负债”的可验收定义

本文中的零负债不是零复杂度，也不是目录冻结，而是以下状态：

- 没有已知包级循环依赖。
- 没有两个领域共同拥有同一 ID、事件、状态机、wire schema 或错误身份。
- 没有 `common`、`shared`、`utils`、`misc`、`helpers` 等无领域 owner 包。
- `ports/`、`events/`、`config/` 只作为按领域分区的受控索引，不提供全局业务 facade；不存在 `schema`、`errors` 等其他横切仓库。
- 没有仅因“多个调用方都需要”而下沉的通用算法或展示函数。
- 没有 Contracts 对 `kernel`、`runtime`、`orchestration` 或 `product` 的反向 import。
- 没有除数据建模基础外的第三方运行时依赖；具体允许集合由架构测试锁定。
- 没有通过 `TYPE_CHECKING`、动态 import、局部 import 或字符串反射掩盖的概念循环。
- 所有持久化契约都有稳定身份和兼容性测试；破坏性变更必须附显式 migration。
- 所有稳定公共符号都从精选领域 facade 导入；领域内部模块路径不承诺稳定，也不因偶然 import 成为事实公共 API。
- 所有稳定公共 API 都进入机器可读 manifest，记录 owner、稳定性、引入版本和兼容策略。
- 所有架构例外都精确到 import site，带 owner、原因和同阶段退出条件；禁止可增长的包级白名单。

### 1.3 非目标

本治理不做以下事情：

- 不修改五层依赖方向。
- 不重写 Pydantic 模型、事件总线、文件事务、模型路由或权限算法。
- 不改变 rollout、journal、checkpoint、output、fileops 等持久化格式。
- 不为目录对称给每个领域预建空的 `events.py`、`errors.py`、`ports.py`。
- 不以单文件 LOC 作为唯一拆分依据。
- 不把所有纯函数都视为 Contracts；“纯”不是“契约”的充分条件。
- 不建立第二套公共 API、插件体系或序列化框架。
- 不在迁移阶段顺手修改默认配置、文案、错误信息或行为语义。
- 不用函数内延迟 import 规避正确的领域拆分。

---

## 2. 当前基线

### 2.1 规模

以 2026-07-29 当前工作树为基线：

当前工作树是脏工作树，因此日期和模块数仅是说明性数据，不构成可复现基线。Phase 0A 必须先生成不可变 baseline record，至少包含：

```json
{
  "baseline": {
    "git_commit": "<HEAD commit>",
    "git_tree": "<HEAD tree>",
    "dirty_worktree": true,
    "dirty_patch_digest": "sha256:<canonical tracked/untracked/deleted delta>",
    "inventory_generated_at": "<UTC RFC3339>",
    "generator_version": "<tool version or source digest>",
    "contracts_file_digest": "sha256:<canonical contracts snapshot>"
  }
}
```

`dirty_patch_digest` 不能直接等同于普通 `git diff`：其 canonical 输入必须覆盖 staged/unstaged 修改、删除、未跟踪文件、相对路径、文件 mode、executable bit、symlink target 与原始 bytes，并排除生成物自身，避免自引用。新增、删除和内容相同的路径移动必须作为不同事实显式记录；移动由生成器比较内容 digest/path 得出，不依赖 Git rename heuristic 或本机 Git 配置。`contracts_file_digest` 对排序后的 `contracts/` 文件 manifest 计算，记录每个路径、类型、mode、size 和 content digest。生成器算法/version、排除规则和 canonical serialization 必须登记；相同文件快照应在不同机器上得到相同 digest。后续 inventory diff 必须同时指出“治理变更”与“baseline drift”，发现未授权漂移即停止关联 cutover。

| 当前区域 | Python 文件数 | 主要内容 |
| --- | ---: | --- |
| 根目录 | 28 | Agent、Artifact、Runtime、Output、Permission、Surface、Service、Execution、Inference、Prompt 等模型 |
| `ports/` | 51 | 所有领域的 Protocol 和部分 journal/error/value 类型 |
| `config/` | 18 | 模型、上下文、MCP、OAuth、观测、路由、工具、UI、Workspace 配置 |
| `errors/` | 15 | 多领域错误及报告渲染 |
| `models/` | 11 | 模型调用、路由、failover、profile、tokenization |
| `text/` | 11 | ANSI、HTML、diff、路径、文案、截断、marker 等算法 |
| `settings/` | 8 | 权限、沙箱、Hook、LSP、Watching、Web Search 等配置 |
| `schema/` | 8 | Message、Queue、Context、Document、Gym、Tool 配置 |
| `fileops/` | 7 | 文件领域模型、事件、端口、错误和 codec |
| 其他子包 | 25 | events、tools、policy、hooks、constants、introspection 等 |
| 合计 | 182 | 约 18,025 LOC |

当前静态分析未发现 Contracts 内运行时 import SCC，说明现有代码尚未形成实际循环；治理必须保留这一优点。

### 2.2 当前依赖特征

全仓对 `mote.contracts...` 约有 1,811 处 import。高频入口包括：

| 入口 | import 次数（约） | 判断 |
| --- | ---: | --- |
| `contracts.schema` | 145 | 聚合了不相关模型，并用懒加载维持门面 |
| `contracts.ports` | 89 | 全局 Protocol service catalog |
| `contracts.output` | 59 | 独立领域，适合形成一级包 |
| `contracts.runtimes` | 56 | 单文件承担多个 runtime 子领域 |
| `contracts.fileops.models` | 51 | 已有领域雏形，但内部文件过宽 |
| `contracts.constants.messages` | 49 | Message wire 字段与横向常量混放 |
| `contracts.artifacts` | 47 | 独立领域仍停留在顶层单文件 |
| `contracts.text` | 47 | 实现型通用工具门面 |
| `contracts.events.types` | 45 | 全域事件巨型联合 |

`ports/__init__.py` 自身直接聚合约 47 个子模块。该文件声称 `ports` 是只依赖 typing 的叶子包，但实际端口模块跨域引用顶层模型、`models`、`policy`、`events` 和 `fileops`。问题不在 Protocol 技术，而在所有业务领域被集中进同一个横向命名空间。

### 2.3 主要问题

#### P0：目录按技术种类组织，无法表达领域

以模型调用为例，其契约散落在：

```text
config/llm.py
config/model_failover.py
config/models.py
errors/models.py
models/capabilities.py
models/failover.py
models/invocation.py
models/model_journal.py
models/profile.py
models/responses.py
models/routing.py
ports/llm_client.py
ports/model_call_journal.py
ports/model_endpoint.py
ports/model_gateway.py
ports/model_operator.py
ports/model_request_transformer.py
events/types.py
```

新增或评审一个模型契约时无法通过目录判断完整影响范围。同类问题存在于 Runtime、Session、Tool、Output、Artifact、Agent 和 Interaction。

#### P0：Contracts 正在演化为 `common/utils`

以下模块包含可执行算法、展示规则或具体第三方依赖，而不是跨边界语义：

- `text/html.py`：依赖 `markdownify` 的转换实现。
- `models/tokenization.py`：依赖 `tiktoken`，并维护易过时的 provider 价格表。
- `text/humanize.py`、`text/plural.py`：产品展示和英文文案规则。
- `text/ansi.py`：终端输出清洗。
- `introspection/docstrings.py`：工具描述解析算法。
- `net.py`：host normalize 与 glob 匹配算法。
- `text/hunks.py`：完整 diff/hunk 运算。
- `serialization.py`：Pydantic 多态注册和反序列化机制。

这些代码可能是纯函数或低依赖，但低依赖不意味着它们是契约。继续容纳会重建已经禁止的 generic utilities 层。

#### P0：全局事件文件已成为领域总仓

`events/types.py` 约 996 行，同时拥有 session、turn、message、model、compaction、file、diagnostics、recovery、task、resource、lifecycle、trace、tool、output、runtime 等事件。

事件是开放 tagged union 的设计可以保留，但事件定义必须跟随事件所描述的领域；全局 registry/codec 可以在 Runtime 汇聚，不应要求所有事件同处一个模块。

#### P0：配置存在三套分类法

当前同时存在：

- `config/`：部署配置。
- `settings/`：同样是部署配置。
- `schema/tool_config.py`、`schema/context.py`：再次包含配置。

分类依据是历史来源而不是业务所有权。例如权限配置同时依赖顶层 `permissions.py` 和 `settings/sandbox.py`；工具配置又位于 `schema/`。未来字段应跟随领域 owner，最终只允许部署聚合层组合领域配置。

#### P1：公共门面扩大事实 API

以下入口批量 re-export：

```text
contracts/__init__.py
contracts/ports/__init__.py
contracts/schema/__init__.py
contracts/text/__init__.py
contracts/errors/__init__.py
contracts/fileops/__init__.py
```

`schema.__init__` 甚至通过动态 `__getattr__` 和字符串映射打破初始化循环。该做法降低了 import path 可读性，并使目录重排必须维护第二套符号索引。

#### P1：巨型模块包含多个变化轴

| 模块 | LOC（约） | 混合职责 |
| --- | ---: | --- |
| `events/types.py` | 996 | 所有领域事件 |
| `fileops/models.py` | 796 | identity、view、mutation、recovery、search、review |
| `config/llm.py` | 604 | endpoint、provider、sampling、token、API 配置 |
| `fileops/events.py` | 574 | 文件读取、编辑、事务、恢复等事件 |
| `fileops/serialization.py` | 473 | 多种文件值对象的手写 codec |
| `runtimes.py` | 445 | runtime identity、checkpoint、operation、projection、durability |
| `artifacts.py` | 395 | identity、retention、sensitivity、publication、resolution |
| `schema/messages.py` | 382 | 全部消息类型及转换逻辑 |

LOC 不是单独拆分理由，但这些文件确实包含独立变化轴，应在领域内部按概念继续拆分。

#### P1：领域错误被横向收集

`errors/` 同时包含 model、routing、service、runtime、task、tool、output、artifact、graph 等错误。调用方经常导入全局 `ErrorCode` 和基类后再导入领域错误，导致领域边界无法从异常路径体现。

全局只应保留极小的错误身份基础；具体错误跟随领域。

---

## 3. 所有权判定规则

### 3.1 允许进入 Contracts

一个类型或常量必须至少满足以下一项：

1. 被两个以上架构层共同读写，并代表同一业务语义。
2. 跨进程、RPC、持久化、event journal 或恢复边界传输。
3. 是低层消费、高层实现的窄 Protocol。
4. 是必须跨实现保持一致的稳定 identity、enum、wire field 或 immutable decision。
5. 是部署期静态配置，且被多个层共同消费。

典型允许内容：

- frozen dataclass、Pydantic DTO、enum、Literal、NewType/ID。
- 事件事实、请求、响应、intent、decision、result。
- 带业务语义的 Protocol。
- 稳定错误 code 和边界异常。
- 明确属于 wire/persistence contract 的纯 codec shape。

### 3.2 禁止进入 Contracts

以下内容默认禁止：

- IO、网络、文件系统、线程、进程、数据库和环境访问。
- provider SDK、tokenizer、markdown converter 等具体第三方实现。
- manager、engine、service 实现、可变 registry、singleton 和资源生命周期。
- 策略计算、路由算法、diff 算法、权限匹配算法、重试或调度算法。
- CLI/TUI 展示格式、人类可读文案、国际化和 terminal 清洗。
- 只被一个实现模块使用的内部数据类。
- 仅为消除重复而下沉的 helper。
- 通过 import side effect 完成的注册。
- 虽被多层 import、但只服务同进程装配、生命周期或依赖注入的 context/container/service locator。

### 3.3 归属判定顺序

新增契约按以下顺序判定：

1. 它描述什么业务事实或能力？该答案决定领域。
2. 哪两个边界需要共同认识它？无法回答则不进入 Contracts。
3. 谁拥有兼容性和版本演进？该 owner 必须唯一。
4. 它是事实、请求、决策、错误、配置还是端口？该答案只决定领域内部文件，不决定一级目录。
5. 它是否包含算法或资源生命周期？如有，拆出实现，仅保留边界输入输出。

Protocol 的 owner 由“谁定义需求与抽象语义”决定，而不是谁实现：Model inference port 归模型调用语义，Surface presentation port 归 Surface，Session fact sink 归 Session，Artifact resolver 归 Artifact。若一个 Protocol 同时暴露 gateway、transformer、fact sink、resolver 或其他跨域协作者，它是装配对象而不是窄端口，必须拆除。

---

## 4. 领域生成规则与冻结结果

### 4.1 三类所有权层次

一级 Contracts 基础领域集合固定为以下 16 个。Phase 0B 可仅在下述证据规则满足时生成 Execution/Hook/Observability/Code Intelligence 之一；一旦生成即写入冻结结果，不允许以 candidate 状态退出：

1. `foundation`：没有业务 owner 的最小语言基元。
2. capability domains：拥有稳定业务语言、端口与兼容性的能力领域。
3. deployment aggregate：物理位于 `contracts/config/deployment/`，只组合已经通过 Contracts 准入的领域配置，不承载业务 DTO。

冻结目录：

```text
contracts/
├── foundation/       # 极小的跨领域稳定身份与错误根；禁止成为 common
├── agent/            # Agent identity、catalog、spawn/lifecycle contract
├── artifact/         # Artifact identity、retention、resolution、publication contract
├── authorization/    # 风险、规则、事实、授权 intent/decision；不负责人机 UI
├── content/          # content-addressed digest/size/algorithm identity
├── conversation/     # Message、history、context、compaction contract
├── file/             # 文件 identity、view、mutation、transaction、recovery contract
├── interaction/      # 人类问题、确认请求与响应；不拥有授权规则
├── model/            # 模型调用、endpoint、routing、failover、usage contract
├── output/           # 结构化输出 evaluation、commit、migration contract
├── runtime/          # 可管理 Runtime、checkpoint、operation、projection contract
├── service/          # 非模型外部服务 invocation/endpoint/failover contract
├── session/          # Session、journal、lease、fact stream contract
├── surface/          # Terminal、Notebook、Canvas、Window presentation contract
├── task/             # Background task、workflow progress、completion contract
├── tool/             # Tool identity、calls、effects、results；消费 authorization decision
├── ports/            # AGENTS 指定；内部按 model/session/artifact/... 分区
├── events/           # AGENTS 指定；内部按 session/model/tool/... 分区
└── config/           # AGENTS 指定；领域配置 + config/deployment 聚合
```

基础集合为 `foundation/content/authorization/interaction/agent/artifact/file/tool/conversation/model/service/output/surface/session/runtime/task`。物理 `ports/events/config` 是 AGENTS 要求的领域索引，不计入业务领域数量。`execution` 只有在 facts 证明存在跨 Kernel/Runtime 持久化 recovery DTO 时生成，否则归 Kernel。Hook 只有稳定 invocation/outcome 跨 Kernel/Runtime 时生成，runner/registry/lifecycle 上移 Runtime。Observability 只有 typed observation/bounded envelope 形成稳定跨层 API 时生成，backend/subscription lifecycle 上移 Runtime。Code Intelligence 只有真实 Kernel 生产消费者时生成，否则整体上移 Product/Runtime。Resilience 永不生成横向领域，分别归 Model、Service、Runtime。

目录不为对称性预建；只有对应 cutover approved 时才创建。Phase 0B 出口的 `contracts-domains.toml` 必须与上述冻结集合一致。

### 4.2 领域内部结构

领域内部按需要使用概念文件：

```text
contracts/model/
├── identity.py
├── request.py
├── response.py
├── endpoint.py
├── routing.py
├── failover.py
├── usage.py
├── errors.py
└── policy.py

contracts/ports/model/
└── inference.py

contracts/events/model/
└── calls.py

contracts/config/model/
├── endpoints.py
└── routing.py
```

规则：

- 不为目录对称创建空文件。
- 文件名优先表达业务概念；只有无法进一步区分时才使用 `models.py`。
- 业务领域内部不再创建 `ports.py`、`events.py`、`config.py` 的第二份真相源；对应内容分别进入 `contracts/ports/<domain>/`、`contracts/events/<domain>/`、`contracts/config/<domain>/`。
- `errors.py`、identity、request/result、policy 等仍跟随业务领域。
- 每个基础设施索引内部以领域为一级分区；例如 `ports/model/inference.py`、`events/session/facts.py`、`config/tool/limits.py`，禁止重新建立单文件全域集合。
- `__init__.py` 默认只含包说明；确需稳定 facade 时必须经过 API 评审并有快照测试。

### 4.3 领域依赖矩阵的冻结方法

终态默认规则是领域之间不互相依赖。下表是 Phase 0B 生成模块级矩阵的领域政策上限，不可直接放宽整个领域。`ports/events/config` 中的模块按其语义领域参与同一矩阵，例如 `ports.model` 视为 Model owner，不因物理索引获得额外依赖权限。

| 来源 | 允许依赖 | 理由 |
| --- | --- | --- |
| 所有领域 | `foundation` | 共享稳定 ID/error 基元 |
| `artifact` | `content` | Artifact 引用 content-addressed identity |
| `file` | `content` | File version/snapshot 引用 content identity |
| `conversation` | `artifact`、`tool` | Message 可引用 artifact 与 tool call identity |
| `model` | `conversation`、`tool` | 模型请求承载消息与 tool specification |
| `hook` | `authorization` | pre-action hook 可表达授权行为，但不得引用授权 engine |
| `interaction` | `authorization` | 人类响应可解析为授权选择；授权规则不得反向依赖 Interaction |
| `output` | `artifact` | committed output 可引用 transcript/artifact |
| `session` | `agent`、`conversation`、`content` | Session facts 记录 agent/message identity；event envelope 引用 ContentDigest |
| `runtime` | `session`、`surface` | runtime checkpoint 与 handoff durability 引用窄 identity |
| `task` | `agent`、`artifact` | task owner/result 可引用 agent/artifact identity |
| `config.deployment` | 已通过准入的领域 `config` | configuration-only 聚合；禁止业务 DTO |

一级矩阵只用于总览，不能直接放宽整个领域。Phase 0B 对每条允许边画出字段级模型草图并冻结模块级边，例如 `runtime.handoff -> runtime.checkpoint`、`runtime.handoff -> surface.identity`、`interaction.handoff -> runtime.identity`、`session.facts -> conversation.message`、`session.event_envelope -> content.identity`、`model.request -> tool.specification`。Model/Service 媒体引用必须依据 0A 字段事实分别冻结为 Artifact identity 或更窄的 Content identity；`contracts-decisions.toml` 与实际 DAG 中只能出现一个结果。永久门禁检查模块级边；一级允许不代表任意子模块可互相引用。

模块级边的机器真相源为 `zdocs/architecture/contracts-dependencies.toml`，每条记录至少包含 `source`、`target`、`reason`、`approval_kind` 和 `approval_ref`。架构测试直接读取该文件；Markdown 表格只由它生成或与它校验。例如：

```toml
[[edges]]
source = "contracts.model.request"
target = "contracts.conversation.message"
reason = "Model request contains conversation messages"
approval_kind = "domain-policy"
approval_ref = "contracts-governance-v6"
```

`approval_kind` 只允许 `domain-policy | adr | temporary-exception`：符合已批准领域矩阵的普通边引用 domain policy；新增跨领域边必须设计评审；双向风险、Foundation 新增和破坏性 wire 变化必须引用 ADR；临时边必须记录 owner 与退出 Phase。禁止为每条普通内部边机械创建 ADR。

每条边必须证明引用的是窄 ID/value 而不是对方完整聚合。发现双向语义时，不通过 `TYPE_CHECKING` 规避，必须采用以下顺序处理：

1. 将被双方引用的稳定 identity 下沉至实际 owner 的 `identity.py`。
2. 把引用方字段改成更窄的 ID/value，而非整个领域对象。
3. 将进程内跨域聚合移到 Runtime/Product composition root，而不是 Contracts 横向包。
4. 只有真正无业务 owner 的基元才进入 `foundation`。

### 4.4 `foundation` 的硬限制

`foundation` 是最危险的潜在债务入口，必须满足：

- 只允许标准库，明确禁止 Pydantic。
- 不允许业务名词、渲染函数、path helper、text helper、codec registry。
- 新增符号必须证明至少三个领域需要，并且语义完全相同。
- 不提供 catch-all `models.py` 或 `utils.py`。
- 500 LOC、20 个公开符号和 Contracts 总 LOC 的 5% 都是强制架构复审触发器；超过并非自动判错，但未获 ADR 不得合入。

### 4.5 公共 API 与 import 规范

`mote.contracts` 是对第三方用户和插件正式承诺的公共 API。公共承诺止于精选业务 facade 和三类基础设施的领域 facade：

- `mote.contracts.<domain>`：稳定公共边界，显式 import、显式 `__all__`，有 API 快照。
- `mote.contracts.ports.<domain>`、`events.<domain>`、`config.<domain>`：AGENTS 指定物理索引下的稳定领域边界；顶层 `ports/events/config` 不聚合全部符号。
- `mote.contracts.<domain>._internal` 及领域内部模块：组织细节，不承诺稳定。
- `mote.contracts` 根：只含包说明和版本信息，不 re-export 业务符号。
- 禁止 `import *`、动态 `__getattr__` 和自动扫描导出。
- `zdocs/architecture/contracts-api.toml` 记录每个 stable/provisional 符号的稳定 symbol ID、owner、引入版本、替代策略和唯一 `canonical_import`；稳定 symbol ID 不使用 Python 对象身份。
- 同一符号不得由多个 facade 导出：业务 DTO、Protocol、Event、Config 分别只由其 canonical 领域 facade 导出，`contracts.model` 不得再次导出 `contracts.ports.model.ModelInferencePort`。兼容 alias 仅允许存在于前一 major 维护分支。
- `__all__`、API manifest 与实际导出必须双向一致。
- 每个 facade 必须在隔离解释器中独立 import，并以不同 import 顺序验证无循环和无注册副作用。

逻辑领域与四类物理路径的对应关系由 `zdocs/architecture/contracts-domains.toml` 维护，例如：

```toml
[domains.model]
facade = "mote.contracts.model"
ports = "mote.contracts.ports.model"
events = "mote.contracts.events.model"
config = "mote.contracts.config.model"
owner = "model-platform"
```

Domain manifest 是领域导航索引，不是第二套聚合 API；允许由它生成按领域聚合的文档页，但业务 facade 不得跨 `ports/events/config` 重新导出符号。

Package shell 的处置是 `retain-package`，不是删除模块。每个 cutover 必须同步移除 `contracts/__init__.py` 中对应业务导出；`ports/__init__.py`、`events/__init__.py`、`config/__init__.py` 同步收敛为包说明/版本，不承载业务映射。Event envelope 的 canonical API 属于 `mote.contracts.events.session`，不得长期由 `events/__init__.py` 导出。Phase 8 只验证最终状态，不集中修复前序已失效的 facade。

终态推荐：

```python
from mote.contracts.model import RoutingDecision
from mote.contracts.ports.model import ModelInferencePort
from mote.contracts.events.file import FileMutated
from mote.contracts.authorization import AuthorizationDecision
from mote.contracts.interaction import ApprovalRequest, ApprovalResponse
```

禁止：

```python
from mote.contracts import RoutingDecision
from mote.contracts.ports import ModelGateway
from mote.contracts.schema import Message
from mote.contracts.model.routing import RoutingDecision  # 不稳定内部路径
```

精选领域 facade 同时保留领域语义和领域内部重构自由。

### 4.6 Authorization、Interaction 与 Tool

- `authorization` 拥有 risk、rule、facts、intent、decision、behavior 和 policy extension contract，可覆盖 tool、file、network、device 与 runtime 操作。
- `interaction` 拥有向人发出的 approval/question request 以及人的 response/choice，不决定风险和准入规则。
- `tool` 只描述 Tool call/effect/result，并消费 `AuthorizationDecision`；它不拥有审批 UI 或通用授权策略。
- Runtime authorization engine 评估事实并产出 decision；Product surface 负责向人展示 request。

### 4.7 Handoff 的两种语义

不建立通用 `composition/` 包。现有 Handoff 按事务 owner 拆分：

- `interaction.handoff`：`HandoffRequest`、`HandoffStatus`、`HumanHandoffOutcome`、`DriverHandoffHandle`，描述人类接管请求与结果。
- `runtime.handoff`：`RuntimeHandoffIntent`、`RuntimeHandoffResolution`、`RuntimeHandoffRecovery`、`PendingRuntimeHandoff`，描述 checkpoint、epoch、revision、fencing 和 durable ownership transfer。
- `runtime.identity`：`RuntimeRef` 等窄稳定身份；Interaction 如确需关联 Runtime，只允许依赖该模块。
- `surface.identity`：Surface descriptor/ref；Runtime handoff 不得依赖 live session 生命周期对象。

Runtime driver 实现可在上层 composition root 组合两类契约，但 Contracts 不建立同时持有多个完整领域对象的聚合 DTO。Phase 0B 必须逐字段拆解当前 `handoff.py`，确认 interaction 不因单个 handoff 用例获得对整个 runtime 领域的依赖权限。

### 4.8 Service 与 Model 的边界

`service` 是 provider-neutral 外部能力调用领域，拥有 invocation、endpoint、outcome、receipt、idempotency、journal 和 service failure。Web Search、Media Generation 等 Product 能力可以调用它，但不把产品 payload 语义下沉 Service。

当前 `services.py` 对 `models.failover.AttemptBudget`、`FailureDisposition` 的依赖必须消除：

- 真正通用且由 Service 承诺的 retry/failure 语义迁入 Service。
- Model 特有语义留在 Model。
- 仅结构相似则分别定义，不为复用放入 Foundation 或制造横向 Resilience 包。

Model 目标端口收窄为稳定 invocation/result，例如 `ModelInferencePort.infer(request) -> result`。Artifact resolution、request transformer、Session fact sink、route/gateway composition 均由 Runtime 内部编排；`ModelGateway` 不能原样迁入 Model facade。

### 4.9 Content 小领域

`content` 只拥有 content-addressed identity，例如 digest、size 和 algorithm/version。它具有明确业务语义，不属于 Foundation。允许的主要边为 `artifact -> content`、`file -> content`，以及确需内容引用的 event/session payload；禁止在各领域重复定义 digest recipe 或把 hashing 实现混入 identity DTO。

### 4.10 Surface DTO 与生命周期端口

Surface 内部必须分离：

- `identity.py`、`frame.py`、`input.py`：稳定 DTO。
- `ports/live_session.py`：异步资源生命周期 Protocol。

Live session port 不得携带具体 Window、Browser、Terminal 实现，也不得因 convenience 聚合 presenter、backend、input handler 等多个协作者。

---

## 5. 现有内容的目标归属

### 5.1 领域迁移映射

| 当前区域 | 目标 owner |
| --- | --- |
| `agents.py`、agent catalog/factory ports | `agent/` |
| `spawn.py`、spawn policy/ports | `agent/` |
| `content.py` | `content/identity.py` |
| `artifacts.py`、artifact store ports/errors | `artifact/`，依赖 `content` |
| `schema/messages.py`、message ports/constants | `conversation/` |
| `schema/context.py`、compaction policy/ports | `conversation/` |
| `fileops/*`、file change/snapshot ports | `file/` |
| `permissions.py`、tool authorization policy/ports | `authorization/`；human request/response 拆至 `interaction/` |
| `interaction.py` | `interaction/` |
| `handoff.py`、runtime handoff ports | 按符号拆至 `interaction.handoff` 与 `runtime.handoff` |
| `models/*`、LLM config、model ports/errors | `model/` |
| `services.py`、service journal/gateway/endpoint ports | `service/`，消除对 `model.failover` 的依赖 |
| `output.py`、output ports/errors/policy | `output/` |
| `runtimes.py`、runtime driver/checkpoint/projection ports | `runtime/` |
| event journal、run lease、session facts | `session/` |
| `canvas.py`、`notebook.py`、`surfaces.py`、`terminal.py` | `surface/` |
| `background_tasks.py`、completion policy | 稳定 Task ID/result/outcome 归 `task/`；构建 context、factory、wake/lifecycle 与 pool control move-up |
| `tools/*`、tool identity/call/effect/result ports | `tool/`；authorization policy 不归 Tool |
| `workflow_control.py` | 迁 `orchestration/workflows/control.py`；确需持久化时重新定义 checkpoint DTO |
| `run_context.py` | 禁止原样迁入；稳定 run identity 按 owner 拆分，进程内 DI/lifecycle context 留 Kernel/Runtime |
| `execution.py`、`model_actions.py` | 只保留有持久化/recovery 证据的 execution DTO；思考与动作算法归 Kernel，不能按名称机械归 Model |
| `config/*`、`settings/*` | 重组至 `config/<domain>/`；跨域聚合仅进入 `config/deployment/` |
| `errors/*` | 各领域；仅错误根和稳定通用 code 可进入 `foundation/` |
| `events/types.py` | 按事件业务语义拆入 `events/<domain>/` |

### 5.2 必须迁出 Contracts 的实现

| 当前模块 | 目标层/owner | 保留在 Contracts 的内容 |
| --- | --- | --- |
| `models/tokenization.py` | 拆分：模型无关估算归 `kernel/models/`，价格/cost 归 `runtime/models/cost/` | token usage DTO 如确需跨层则留 `model/usage.py` |
| `text/html.py` | parsing/content adapter 归 Runtime；presentation/rendering 归 Product | 无 |
| `text/humanize.py`、`plural.py` | `product/` 展示 owner | 无 |
| `text/ansi.py` | `runtime/tools/` 或 `product/cli/` | 无 |
| `introspection/docstrings.py` | 通用解析归最低消费者 `kernel/tools/spec/`；Workflow 特有部分可归 Orchestration | 结构化 ToolSpec DTO 如满足跨层准入才保留 |
| `net.py` | Phase 7D 与 sandbox settings 同切片迁 `runtime/sandbox/network/` | DomainPattern value 如确需 wire 化 |
| `text/hunks.py` | Phase 4 与 File 同切片迁 `runtime/fileops/` | Hunk value 若进入 journal 则留 `file/` |
| `serialization.py` | Phase 7E 与 document/env/gym 消费闭包同切片迁 `runtime/serialization/` | 稳定 discriminator 字段/值定义 |
| `text/paths.py` | display helper 可 Phase 1；URI value 跟随 File/Surface | wire URI/path value 仅在确需跨边界时保留 |
| `text/markers.py` | Phase 5 跟随 Conversation/Tool | 稳定 wire marker 常量分别归其 owner |

---

## 6. 实施阶段

### Phase 0A：事实基线

目标：只记录现状，不冻结目标目录。

工作项：

1. 先建立并自测唯一治理 CLI；CLI 可写入 `ztest/architecture/`，但 Phase 0A 不创建任何目标 Contracts 领域包。随后由 CLI 生成模块、符号、LOC、第三方依赖和完整 import graph。
2. 为每个公开符号生成机器 inventory，至少记录 `symbol/current_module/semantic_owner/target_layer/target_module/disposition/decision_status/decision_evidence`。
3. 分别记录 `all_production_consumers/test_only_consumers/implementers/consumer_layers/lowest_consumer_layer`；重点核对 LSP、code map、skills、resource loader、telemetry、hook runner、model operator 和 request transformer。Protocol owner 与目标层必须由真实最低生产消费者证明。
4. 对每个 move-up 符号计算 `target_layer/remaining_contracts_consumers/legal_dependency_after_move/consumer_migration_closure`。Contracts 消费者必须同一 cutover 迁出、改用保留的稳定 DTO，或将该符号推迟；所有其他生产消费者也必须在移动后保持五层单向依赖。硬门禁同时要求 `remaining_contracts_consumers == []` 与 `legal_dependency_after_move == true`。
5. 将兼容资产拆为 `public_api/wire_schema/persistent_identity/event_tag/error_code/config_compatibility/module_discriminator/protocol_signature/fixture_status`，不得继续塞入一个“stable identity”文本列。
6. 扫描仓库、发布文档、示例、已知插件和可获得的使用证据；区分明确 stable API 和历史偶然路径。Phase 0A 不要求证明未知外部用户无人使用，未知风险由目标 major 统一治理。
7. 清点 event tag、error code、tool/output/content identity、journal/checkpoint discriminator、pickle/Pydantic/module-qualified discriminator。
8. 清点配置字段及其真实消费层、默认值、加载/解析/secret resolution owner。
9. 清点当前事件 registry、decoder、unknown-version 行为和历史 fixture。
10. 生成并锁定可复现 baseline metadata、完整 Contracts file manifest 与 digest；Phase 0A 的所有产物引用同一个 baseline ID。
11. 生成 `symbol -> production consumers -> owning test suites` 与 `module edge -> required test suites` 测试矩阵，验证命令和测试路径当前真实存在；每个 cutover 从矩阵自动生成最低测试命令。
12. 对无法由静态/运行证据判定的字段写入 `fact_status = "unknown"` 和缺失证据，不得留空、猜测或写候选 owner。Unknown 允许 Phase 0A 结束；Phase 0B 必须清零全部 unknown 才能 verified，因此任一未解决项都会阻塞 0C 和全部迁移阶段，直到补证据并重新 snapshot。

首次 Phase 0A `snapshot` 以实际文件系统为事实来源，不把 bootstrap inventory seed 当作不可变输入。Seed mismatch 必须输出稳定排序的 added/removed/content-identical-moved/symlink/mode diff，但不得令首次 snapshot 无法生成新 facts；生成器不得依赖 Git rename heuristic。随后以一次受审提交更新生成 inventory，在此之前 `check` 返回漂移失败。完成 bootstrap 后，所有后续 `check` 都要求 seed/generated view 与同 baseline facts 精确一致。

“公开符号”的扫描集合包括：root/领域 `__all__`、顶级 facade re-export、其他生产模块 import 的非私有符号、文档声明 API、Protocol、公共类型别名、动态 `__getattr__` 映射、已知插件/示例 import，以及 pickle/module discriminator 引用。以下不因扫描本身自动成为 public：仅测试引用、`_private`、同模块内部引用、仅被 AST 定义扫描发现且未导出/跨模块使用的 implementation detail。冲突证据进入人工裁决，不由名称规则覆盖真实发布证据。

Phase 0A/0B 的机器真相源统一位于 `zdocs/architecture/`，生成事实与人工裁决物理分离：

- `contracts-facts.json`：工具生成的逐符号事实、全部消费者/实现者、层级、当前依赖与兼容资产；生成器可覆盖。
- `contracts-decisions.toml`：人工批准的 semantic owner、target layer/module、disposition、领域政策、例外和证据；生成器只读。
- `contracts-migrations.toml`：人工批准的 cutover DAG 与执行状态；生成器只校验和写独立执行证据，不覆写批准字段。
- `contracts-domains.toml`：逻辑领域及 DTO/ports/events/config 物理路径。
- `contracts-dependencies.toml`：当前边与批准后的目标模块边。
- `contracts-api.toml`：stable symbol ID 与唯一 canonical import。
- `contracts-identities.toml`：wire identity、版本、recipe、owner 与 fixture。
- `contracts-events.toml`：核心 durable event 的 tag/version/wire owner/current owner/decoder/fixture；Phase 0A 先记录现状，Phase 2B 才切换 Runtime registry。
- `contracts-errors.toml`：稳定错误身份、owner 历史、版本、恢复动作与 fixture。
- `contracts-tests.toml`：symbol/edge 到实际测试套件与最低命令的映射。
- `contracts-phase-gates.toml`：Phase 级依赖、状态和统一 entry/exit checks；Phase 标题顺序不构成执行授权。

Markdown inventory 必须由 facts 生成并与 decisions/migrations 校验，不得成为第二份人工真相源。Markdown 与 manifest 冲突时 CLI 必须失败并返回 4，不得选择任一方继续执行。Phase 0A 可以创建事实 manifest，但不得创建目标 Python 包。

`contracts-phase-gates.toml` 至少包含：

```toml
[[phase]]
id = "0A"
status = "planned"
depends_on = []
expected_cutover_ids = []
entry_checks = []
exit_checks = ["facts_complete", "baseline_reproducible"]

[[phase]]
id = "0B"
status = "planned"
depends_on = ["0A"]
expected_cutover_ids = []
exit_checks = ["all_symbols_decided", "migration_dag_valid"]

[[phase]]
id = "0C"
status = "planned"
depends_on = ["0B"]
expected_cutover_ids = ["governance-bootstrap"]
exit_checks = ["governance_bootstrap_verified"]

[[phase]]
id = "3"
status = "planned"
depends_on = ["2"]
expected_cutover_ids = [
  "3a-authorization",
  "3b-interaction-approval",
  "3c-artifact",
  "3d-agent-identity",
]
```

Phase 持久状态只允许 `planned → in_progress → verified`。Phase gate 与 cutover 四态相互独立。Phase 0B 必须为每个 Phase 冻结完整 `expected_cutover_ids`；标题、编号和 DAG 邻接关系都不隐含成员资格。Phase verified 的硬条件是：

```text
actual_cutover_ids == expected_cutover_ids
all_expected_cutovers_verified == true
no_unassigned_cutovers == true
no_symbol_without_cutover == true
```

`actual_cutover_ids` 定义为 `contracts-migrations.toml` 中 `owner_phase` 等于该 Phase ID 的全部 cutover ID 集合；比较采用 canonical 排序后的集合等价，不接受重复 ID。实际 manifest 的 `expected_cutover_ids` 不得包含占位符、通配符或由运行时自动扩展的表达式。

每个需要动作的 symbol disposition 必须属于且只属于一个 symbol-move cutover 或完整 deletion closure；无需移动但必须冻结保留结果的资产进入 asset-finalization cutover。冻结后增加、删除或转移 Phase 成员，会使该 Phase 及全部下游 Phase/cutover 的批准失效，必须在新 baseline 上重新评审。Phase 1～8 只能运行自身 Phase gate 已 in_progress、cutover 已 approved 且 `depends_on` 全部 verified 的节点。

实际文件必须显式枚举 `0A/0B/0C/1/2/3/4/5/6/7/8`，其中 0B←0A、0C←0B、1←0C，后续 Phase N←Phase N-1；不得由文档标题顺序推断。Cutover DAG 可以表达 Phase 内更细依赖，但不能绕过 Phase gate。

所有治理 JSON/TOML 共享同一个 envelope：`schema_version/baseline_id/generated_by/generated_at/canonicalization_version`。未知 schema version 默认拒绝；升级由 `zdocs/architecture/migrations/` 中显式、可测试的 manifest migration 完成。Facts 与 decisions/migrations 通过 baseline 关联；JSON 使用 UTF-8、排序 key 和确定性数组规则；TOML table/array 的 canonical 顺序由 canonicalization version 定义。Markdown 是生成物或校验视图，不是权威输入。

`contracts-migrations.toml` 每个 cutover 至少记录：`cutover_id/cutover_type/owner_phase/deletion_phase/status/depends_on/import_mapping/fixtures/required_tests/approval_baseline_id/prepared_commit/cutover_commit/verified_commit/verification_evidence/approved_by/approved_at/evidence`。`cutover_type` 只允许 `symbol-move | infrastructure | legacy-deletion | asset-finalization`。迁移内容使用逐符号映射而非单一 source/target：

```toml
[[cutover.moves]]
symbol = "mote.contracts.models.tokenization.count_string_tokens"
source = "mote.contracts.models.tokenization"
target = "mote.kernel.models.tokenization"
disposition = "move-up"
```

一个 cutover 可以包含多个 target，但每个 stable symbol 在整个 migration DAG 中只能有一个最终定义位置；拆分后的新符号必须记录由哪个旧符号派生，不能用复制产生两个 canonical owner。

持久 `status` 只允许 `planned | approved | in_progress | verified`，唯一前向转换是 `planned → approved → in_progress → verified`：

- `planned`：可以提交准备期 fixture、测试、mapping 和 proposed manifest moves，但不得创建新生产定义。
- 准备提交完成后重新运行 `snapshot`/`check`；随后的批准/attestation 提交记录 `prepared_commit`，人工基于该 commit 的新 facts 写入 `approval_baseline_id/approved_by/approved_at`，状态才变为 `approved`。
- `approved → in_progress`：所有 `depends_on` 已 verified，HEAD/事实与 `prepared_commit/approval_baseline_id` 匹配；原子 cutover 落地后写入 `cutover_commit`。cutover manifest 已声明的移动属于批准范围，不视为未声明 drift。
- `in_progress → verified`：验证内容提交完成后，由随后的 attestation 提交写入 `verified_commit` 和不可变 `verification_evidence`，且测试、API/identity/dependency diff 全部通过；没有 `verified_commit` 不得标记 verified。

Commit hash 字段永不引用包含其自身的 commit：`prepared_commit/cutover_commit/verified_commit` 都由目标提交之后的 governance attestation 写入。Attestation 只记录状态与证据，不得夹带生产语义变化；CLI 校验引用 commit 可达、内容类型符合对应阶段且链路顺序正确。

`approval_baseline_id` 的 source scope 覆盖生产代码、测试、fixture、import mapping 和会影响 cutover 的生成 facts；不包含随后写入的批准/状态/commit hash 等 governance attestation 字段，否则批准会因记录自身而立即漂移。被排除的人工 TOML 仍单独做 content digest、签批和 schema 校验。CLI 判断 HEAD 是否匹配 prepared baseline 时允许其后的纯 attestation commits，但任何超出声明字段的文件变化都按 drift 处理。

`blocked` 不是持久主状态，而是 CLI 根据 `blocked_reasons` 计算的 effective state。未声明 drift、依赖未满足、层级违法或证据缺失均使当前状态暂时 blocked。解除后显示原持久状态：planned 可直接修正准备材料；approved 可恢复批准 baseline，或生成新的 prepared commit/baseline 并替换人工批准；in_progress 只能恢复到已批准 cutover 范围后继续，不能在执行中扩大批准范围；verified 的任何漂移都是新 cutover。生成器不得自动修改持久状态或批准字段。

`contracts-errors.toml` 每个 error code 至少记录 `code/wire_namespace/current_owner/ownership_history/introduced_version/retired_version/recovery_action/replacement/fixture`。退役 code 永不复用；owner 迁移保留 namespace、历史和 fixture，replacement 不改变旧 code 的解码语义。

层级合法性按 `contracts=0 < kernel=1 < runtime=2 < orchestration=3 < product=4` 计算。移动后，对每个生产消费者必须满足 `target_layer_rank <= consumer_layer_rank`；同一 consumer migration closure 则在 cutover 后的图上重新计算。`lowest_consumer_layer` 只是审计摘要，最终门禁遍历全部生产消费者，不能靠单个最小值替代。

治理工具只有一个可执行入口，必须离线、无网络、确定性运行：

```bash
python -B -m ztest.architecture.contracts_governance snapshot
python -B -m ztest.architecture.contracts_governance check
python -B -m ztest.architecture.contracts_governance diff
python -B -m ztest.architecture.contracts_governance tests <cutover-id>
python -B -m ztest.architecture.contracts_governance tests <cutover-id> --run
```

退出码固定为：`0` 成功且无漂移；`1` 工具运行/IO 错误；`2` facts/baseline 漂移且没有关联批准；`3` manifest schema/canonicalization 错误；`4` drift 导致一个或多个批准失效，或 DAG/层级门禁、cutover 状态非法。若同时满足 2 和 4 的条件，返回 4。`snapshot` 只更新生成事实，不写人工 decisions/migrations；`check` 零写入；`diff` 输出稳定排序的事实与决策差异。

`tests <cutover-id>` 只输出稳定排序、shell-safe 的批准测试命令，不执行测试、不写证据；`tests <cutover-id> --run` 才顺序执行并把结果写入 execution evidence。确定性承诺只覆盖相同输入下的测试选择和命令序列；实际测试退出码受运行环境影响，不承诺跨机器相同，但环境指纹和每条结果必须进入 verification evidence。

Phase 0A 的硬出口为：

```text
facts_files_complete == true
facts_baseline_ids_identical == true
inventory_module_coverage == 100%
public_symbol_coverage == 100%
production_consumer_coverage == 100%
persistent_identity_coverage == 100%
test_mapping_coverage == 100%
baseline_reproducible == true
```

Coverage 100% 表示每个对象都有事实记录，允许记录的结论为 unknown，不表示所有事实已知。唯一治理 CLI 的子命令与退出码必须通过离线测试；在同一快照上重复运行得到相同 digest 和等价 facts；此阶段不创建目标领域包。

### Phase 0B：领域设计

目标：对相同 0A facts 确定性地产生唯一、可审计的冻结决策；人工批准确认证据与政策应用，不允许输出候选结果。

工作项：

1. 为每个符号确定唯一 owner 与 `retain-contract/split-contract/move-up/delete`；包壳模块单独使用 `retain-package`，不得与业务契约 disposition 混淆。
2. 将每个符号映射到冻结的 16 个一级领域或明确 move-up/delete；Execution/Hook/Observability/Code Intelligence 按 4.1 规则裁决，不生成候选一级领域。
3. 形成字段级模型草图和封闭的模块级允许边，再汇总一级领域 DAG。
4. 完成 Foundation、Handoff、Model inference port、Surface DTO/port、Event registry 与 facade API 设计。
5. 设计并批准 Service → Model failover、ModelGateway service locator、RunContext DI container 的替代模型、目标 Protocol、字段草图、消费者迁移集和实施 Phase；Phase 0B 不修改生产代码。实际消除 Service → Model 与 ModelGateway 归 Phase 5；RunContext 按 Execution 裁决结果进入对应实施切片或 move-up 专项。
6. 审定全部 Domain/API/Dependency/Identity manifest、旧→新 import 映射和目标 major release 方案。
7. 为每个 `move-up` 符号冻结 `all_production_consumers/consumer_layers/lowest_consumer_layer/target_layer/target_module/target_owner/projected_legal_dependency_after_move/consumer_migration_closure/required_tests`；不得保留“Product 或 Runtime”“各实际 owner”、TBD、二选一或候选路径。目标层高于任何未迁移消费者时必须缩窄/拆分或随 closure 同迁，不能批准。
8. 修正 `zdocs/ARCHITECTURE.md` 中与实际五层架构冲突的历史 `common/*` 描述，标明当前结构、目标结构与迁移状态；迁移开始前上位规范必须正确。
9. 设计并评审完整 `contracts-migrations.toml` DAG：Phase 0B 中各切片保持 `planned`，具有唯一 owner/deletion Phase、入口/出口要求、依赖、测试和 import mapping。具体 cutover 只有在其准备提交完成、重新 snapshot/check 后，才基于 `prepared_commit/approval_baseline_id` 单独批准。

Phase 0B 的硬出口为：

```text
unknown_facts == []
undecided_symbols == []
candidate_domains == []
candidate_targets == []
multi_owner_symbols == []
illegal_move_up == []
unowned_legacy_paths == []
all_projected_remaining_contracts_consumers == []
all_projected_legal_dependency_after_move == true
migration_dag_valid == true
```

这里检查的是模拟完成各 cutover/closure 后的 `projected_remaining_contracts_consumers` 与 `projected_legal_dependency_after_move`，不是尚未执行迁移的当前图。Unknown 可以按关联 symbol 定位和补证据，但 0B 出口采用全局严格门禁：只要 `unknown_facts` 非空，0B 就不能 verified，0C 及全部迁移阶段均不能启动。补齐 facts 后重新确定性生成 decisions；Phase gate 满足出口后自动允许进入 0C，不需要重新设计整份计划。

### Phase 0C：迁移门禁

目标：在目录迁移前阻止结构继续恶化。

工作项：

1. 新增架构测试：禁止新增顶层孤儿、横向目录、非法模块边、动态/局部 import、import-side-effect registry 和未批准第三方依赖。
2. 建立 stable identity/discriminator/event registry/API manifest 门禁。
3. 锁定临时 baseline；非法项只能减少，每项例外精确到 import site/symbol 并有退出阶段。
4. 将新增契约评审模板纳入开发流程。
5. 建立 next-major release train：Phase 0A～0C 可从当前 major 正常发布治理资产与门禁；Phase 1 起冻结当前 major 的 Contracts 功能演进，当前-major 功能分支不得再修改 Contracts。Phase 1～7 只进入 next-major migration branch，不从该分支发布当前 major 正式版；当前 major 仅由独立 maintenance branch 发布安全与兼容性修复。
6. 固定 migration branch 的合并策略、Contracts 代码所有权和冲突处理：其他功能分支不得绕过 manifest/矩阵修改 Contracts；当前 major 修复经兼容性审计后回合并，禁止把维护分支 forwarding layer 带入 next-major。
7. 明确 Phase 1～7 只允许发布带破坏性预告的 next-major preview，preview 不形成额外兼容承诺；Phase 8 是唯一目标 major 正式发布点。
8. 定义版本化旧→新 import mapping 格式并建立 AST/CST 迁移工具骨架；必须支持 dry-run、幂等重复运行、无法转换报告和旧 import fixture。每个 cutover 同步增加映射并测试，Phase 8 只负责打包、发布和冻结完整工具，不临时实现转换逻辑。
9. 建立 mapping 完整性硬门禁：`every_removed_public_import_has_mapping/every_mapping_target_is_canonical/mapping_has_no_cycles/mapping_is_idempotent/mapping_does_not_target_internal_module` 全部为 true。
10. 建立 deletion owner 硬门禁：`every_legacy_module_has_one_deletion_cutover/every_legacy_root_has_one_final_deletion_cutover/no_two_cutovers_delete_same_path/deletion_depends_on_all_symbol_moves` 全部为 true。
11. 对每个待批准 cutover 构造迁移后虚拟图，验证五层方向、Contracts 模块矩阵、SCC=0、canonical imports、closure 完整以及删除后无悬空 import；只检查当前源码图不足以批准。
12. 将 CLI、manifest schemas、architecture tests、baseline drift gate、migration DAG validator 和 release-train checks 汇聚为 manifest 节点 `governance-bootstrap`。只有上述设施全部通过并有 immutable evidence 时该节点才 verified；所有 Phase 1 cutover 必须显式 `depends_on = ["governance-bootstrap"]`。

验收：`governance-bootstrap` verified；mapping、deletion owner 和 projected graph 门禁全部通过；所有外层 import 都可映射到 canonical facade；Phase 0A/0B 资产可重复生成；release train 与维护修复回合并检查可执行。Phase gate 0C verified 后，Phase 1 仍只执行 individually approved 且 depends_on verified 的 cutover。

### 所有 Phase/Cutover 的统一 Gate

每个 Phase/cutover 开始前必须通过以下统一入口；领域规则只能增加检查，不能删除：

```text
all_depends_on_verified
baseline_current
effective_blocked_reasons_empty
moves_have_unique_targets
required_fixtures_exist
required_tests_exist
projected_layering_legal
projected_graph_acyclic
rollback_order_known
```

每个 cutover 完成必须通过以下通用出口：

```text
dependency_diff_approved
required_tests_pass
no_new_exceptions
facts_regenerated
verification_evidence_immutable
```

此外必须按 `cutover_type` 通过类型化出口：

- `symbol-move`：`old_definitions_removed`、`old_imports_removed`、`canonical_api_current`、`identity_fixtures_pass`；适用于普通领域迁移和 move-up。
- `infrastructure`：`old_mechanism_disabled`、`unique_composition_entry`、`behavior_equivalence_verified`、`governance_runtime_registration_consistent`；`governance-bootstrap` 与 2B Event infrastructure 属于此类。
- `legacy-deletion`：`declared_paths_absent`、`imports_absent`、`mappings_complete`、`all_symbol_moves_verified`；6E3、7E 和每个 legacy root 最终删除节点属于此类。
- `asset-finalization`：`declared_deletes_absent`、`declared_retained_assets_present_and_current`、`no_undeclared_asset_mutation`；`release-finalization`、retain-only 资产冻结以及只收敛测试/静态资产且不替换运行机制的切片属于此类。

只有通用出口和对应类型出口全部通过，cutover 才能写入 verified；不再对不移动定义的基础设施节点强制检查旧定义或旧 import。Phase 出口还必须满足 `contracts-phase-gates.toml` 中声明的全部 `exit_checks` 以及冻结成员集合检查。领域特有验收只作为附加条件；任何入口或适用出口失败均使 effective state blocked，并返回退出码 4。

### 所有迁移切片的三段完成协议

Phase 1～7 的每个领域或 move-up 切片都必须使用三段协议；准备阶段不得创建第二份生产定义或 forwarding module。

#### A. 准备提交

- 冻结符号闭包、canonical import、owner、依赖边和 Contracts consumer closure。
- 增加 wire/API/golden fixture、目标架构测试、import mapping 与反向消费方测试。
- 更新 `planned` cutover 的 moves/fixtures/tests/import mapping，但不创建重复生产定义、不改变公共 import；形成准备提交后重新 snapshot/check，随后的批准 attestation 再记录该 `prepared_commit`。
- 人工审查 prepared commit 的新 facts；无未声明变化后写入 `approval_baseline_id` 和批准证据，将状态从 planned 改为 approved。

#### B. 原子 Cutover 提交

将状态改为 in_progress，并在同一个可构建、可测试提交中完成：创建或移动目标定义；更新全部仓内消费者、facade 与 AST/CST mapping；同步非自引用 manifest 内容；删除旧定义、旧 module import 和该闭包的临时例外。随后由 governance attestation 记录 `cutover_commit`。生产定义在此提交前后都只有一份，不允许以 re-export 连接新旧路径。

不能在一个提交安全完成的大模块必须按独立“符号闭包”拆为多个 cutover；闭包包含其定义、强关联 codec/identity、全部 Contracts 消费者和使旧定义可删除的最小仓内消费者集合，不能先复制完整模块再逐步切换。

#### C. 验证提交

- 运行由测试矩阵生成的架构、领域、直接及反向消费方测试并保存证据。
- 可补充不改变契约语义的测试、文档和验收记录；不得再次修改本切片契约定义。
- AST/CST 工具在旧 import 快照上 dry-run/执行/重复执行，验证转换完整、幂等且无法转换项显式报告。
- 验证内容提交完成后，由 governance attestation 写入 `verified_commit/verification_evidence`，再将状态从 in_progress 改为 verified。

任何一步失败都不得宣布切片完成。当前切片验收前可回滚到准备前稳定点；下游切片开始后不得单独回滚上游，必须按依赖 DAG 逆序回滚。已发布 preview 后不得恢复旧 wire 写法；Phase 8 正式发布后只通过新版本修复，不再称为迁移回滚。

硬依赖顺序为：Content → Artifact/File；Authorization → Tool/Interaction approval；Tool identity → Conversation → Model；Agent identity + Conversation → Spawn；Surface identity + Session → Runtime → Runtime handoff。阶段编号不能覆盖这组依赖，Phase 0B 若发现新增反向边，必须先重排阶段或缩窄字段，不得用临时 import/alias 穿越。

上述关系必须落成 `contracts-migrations.toml` 的显式 `depends_on`：

- 每个 Phase 1 cutover `<- governance-bootstrap`。
- `2A Content <- governance-bootstrap`；`2B Event infrastructure <- governance-bootstrap`。
- `3A Authorization <- governance-bootstrap`；`3B Interaction approval <- 3A`；`3C Artifact <- 2A`；`3D Agent identity <- governance-bootstrap`。
- `4 File <- 2A + 2B + 3A + 3C`。
- `5A Tool identity <- 3A`；`5B Conversation <- 5A`；`5C Tool remainder <- 5A + 5B`；`5D Model <- 5B + 5C`；`5F Spawn <- 3D + 5B`。
- `5E Service` 必须在 Phase 0B 按最终字段选择且只选择一个稳定前置：引用 Artifact identity 则 `<- 3C`，只引用 Content identity 则 `<- 2A`；Phase 0B 出口不允许保留该条件表达，实际 TOML 必须写入唯一 cutover ID。
- `6A Output <- 3C + 5A + 5D`；`6B Surface identity <- governance-bootstrap`；`6C Session <- 2A + 2B + 5B`；`6D Runtime <- 3C + 6B + 6C`。
- `6E1 Interaction handoff <- 3B + 6D`；`6E2 Runtime durable handoff <- 6D`；`6E3 Handoff legacy deletion <- 6E1 + 6E2`。若 0B 从 Interaction handoff 删除 Runtime identity 字段，才可在实际 TOML 中移除 6D 依赖，并须由 projected graph 证明。
- `7B Surface lifecycle <- 3B + 6B + 6D`。
- `7D1 Config fragments <- 所有被聚合领域 contract cutover`；`7D2 Private config move-up <- 对应全部生产消费者 cutover`；`7D3 Deployment aggregate <- 7D1 + 7D2`；`7D4 RoleSchema compatibility <- 7D3`；`7E Legacy config deletion <- 7D1 + 7D2 + 7D3 + 7D4`。
- `7C cleanup` 的每个实际 cutover依赖其全部生产消费者 cutover；不得用一个总括节点掩盖不同 closure。
- `release-finalization` 的实际 TOML 必须逐项枚举全部 business cutover、legacy deletion cutover 和 `7E` 的 cutover ID，不允许写通配符、“所有 Phase 1～7”或只依赖 Phase gate。

CLI 必须检测 DAG 无环、缺边、字段依赖未映射和未 verified 前置；Phase 编号不提供任何隐式依赖。

上述文字中的“所有/对应”只是生成规则；Phase 0B 写入 TOML 时必须展开为稳定、排序后的具体 cutover ID 数组，任何通配符、Phase 名替代或运行时目录扫描均为 schema 错误。

Phase 7A Task 不预设固定依赖。Phase 0A/0B 必须从字段和 Protocol 消费证据判断其是否依赖 Agent、Artifact、Session、Conversation 或其他领域，并把每条真实前置写入该 Task cutover 的 `depends_on`；缺少已发现字段依赖对应的 DAG 边属于退出码 4。

### Phase 1：迁出实现型公共工具

目标：恢复 Contracts “只放契约”的可信边界。

先决条件：所有 module-qualified discriminator、digest recipe 和 marker literal 已登记；已证明待迁模块不承担未显式记录的 wire compatibility；对应 golden fixture 已存在；所有 move-up 条目的 target layer/module/owner、consumer migration closure 和 required tests 均已唯一确定；虚拟 cutover 图满足 `projected_remaining_contracts_consumers == []` 且 `projected_legal_dependency_after_move == true`。任一项缺失、仍含 unknown/TBD/候选路径、closure 有未处置成员或 projected graph 门禁失败时不得迁移；合法 closure 可以非空，但必须全部包含在同一原子 cutover。

Phase 1 不硬编码模块名单。唯一输入是 `contracts-migrations.toml` 中 `owner_phase = "1"`、`status = "approved"`、baseline 未漂移、全部依赖已 verified，且通过双硬门禁的符号闭包。文中举例不构成迁移授权。

已知裁决约束：

- `models/tokenization.py` 必须拆分：Kernel 使用的模型无关 token 估算归 `kernel/models`，价格表/成本实现归 Runtime，跨层 usage DTO 才可留 Model Contracts；也可改为 Kernel 接收已计算 token 数，但不得形成 Kernel → Runtime。
- `introspection/docstrings.py` 的最低消费者是 Kernel；通用 tool spec/docstring parsing 归 `kernel/tools/spec`，Workflow 特有解析可另归 Orchestration，不能整体迁 Runtime。
- HTML、ANSI、humanize、plural、path display 等仅在 migration manifest 证明完整 consumer closure 与层级合法后才可进入 Phase 1。

以下内容明确推迟，避免 Contracts 反向依赖：

- `serialization.py` 与 `schema/document.py`、`schema/env.py`、gym 等消费者在 Phase 7E 同切片处置；若先拆，只能先保留独立稳定 wire DTO，不能 import Runtime。
- `net.py` 与 `settings/sandbox.py` 在 Phase 7D 同切片迁移。
- `text/hunks.py` 的 Hunk value/算法在 Phase 4 一次拆开。
- URI/path value 跟随 File/Surface 的对应 Phase；Phase 1 只迁 display 实现。
- `text/markers.py` 跟随 Conversation/Tool 在 Phase 5 迁移。

每项迁移必须先确认真实 owner，禁止统一搬入新的 `runtime/utils`。

验收：

- Contracts 不再 import `tiktoken`、`markdownify`。
- Contracts 不再拥有展示文案或 terminal 格式算法。
- 每个已迁符号的 projected 与实际结果均满足 Contracts consumer 清零和层级合法；全仓层级图不新增反向边。
- 各调用方行为测试无变化。

### Phase 2A：Content 最小垂直切片试点

目标：固定以 `content.identity` 作为首个低风险垂直切片，验证完整迁移方法。

Content 试点必须满足：

- 外部消费者少且明确。
- 不参与核心 replay/checkpoint。
- 没有跨三个以上领域的字段依赖。
- 能覆盖精选领域 facade、`__all__` 快照、旧→新 import 映射和架构矩阵。

试点固定为 `content.identity`，cutover ID 固定为 `2A-content`。若 facts 发现未解决的持久化或外部 API 风险，该 cutover 保持 blocked，补齐事实和 fixture 后继续；不得改选其他领域改变 DAG 身份。

验收：

- 一个领域完成端到端迁移且无永久兼容 alias。
- facade、内部模块、API 快照和迁移工具边界可复用。
- 依赖矩阵、API/identity manifest 和测试模板得到实际验证。
- 试点复盘通过后才允许批量迁移。

### Phase 2B：Event infrastructure 垂直切片

目标：在 File 之前独立验证事件基础设施，避免让高持久化风险领域成为 registry 首次试验。

工作项：

1. 建立机器 event manifest，接入全部现有核心 durable event，不改变 tag、payload version 或 wire namespace。
2. 实现 Runtime 唯一显式 `build_core_event_registry()`，用普通 Python 常量静态列举各领域 decoder；禁止扫描、自注册 decorator、import side effect，以及生产运行读取 `zdocs/architecture/contracts-events.toml`。
3. 建立治理 manifest 与 Runtime 静态注册项的双向一致性测试，并覆盖重复 tag、未知 tag/version、历史 fixture 和 import 无副作用。
4. 将当前 registry 切换为显式 builder；该切片只改变装配机制，不重写事件业务语义。

验收：核心事件覆盖率 100%，旧 fixture 全部可解码，registry 集合与切换前等价。Phase 2B 未通过不得迁移 File。

### Phase 3：Authorization、Interaction Approval、Artifact 与 Agent 叶子

目标：在 File 之前稳定其授权、内容引用和 Agent identity 依赖。

顺序：

1. Phase 3A：建立 Authorization facts/rules/decision/config/ports；不包含人机通道。
2. Phase 3B：迁移最小 Interaction approval request/response，使 Authorization 与 human choice 可从旧 `permissions.py` 完整拆开；question、surface integration 等留 Phase 7。
3. 删除 `permissions.py` 旧混合真相源；该删除只归 Phase 3B。
4. Phase 3C：Artifact identity/store contracts 依赖 Content，不复制 digest。
5. Phase 3D：仅迁 Agent identity、catalog 与 factory ports；调度和 admission 算法留 Orchestration。

Agent spawn request/context、spawn policy 及 conversation/message reference 不在 Phase 3 完成，统一由 Phase 5F 迁移；不得提前改变固定 DAG 身份。

验收：Tool/File/Runtime 只能消费 Authorization contract；Authorization 不依赖 Interaction；Artifact/Agent 不形成反向依赖。

### Phase 4：迁移 File

目标：在小切片验证模板后，治理当前最接近 bounded context 但持久化风险较高的 `fileops`。

目标拆分：

```text
file/
├── identity.py
├── views.py
├── mutations.py
├── transactions.py
├── recovery.py
├── search.py
├── errors.py
└── codec.py

events/file/
└── facts.py

ports/file/
└── operations.py
```

工作项：

- 拆分 `fileops/models.py`、`events.py`、`serialization.py` 的独立变化轴。
- 更新全部生产和测试 import。
- 删除 `fileops/`，不保留 re-export。
- 将 hunk 算法与 journal value 分离。
- 固化领域包模板和领域内依赖规则。

验收：

- `file/` 内无循环。
- file codec round-trip 与历史 fixture 全部通过。
- 旧 `mote.contracts.fileops` 路径不存在。

### Phase 5：迁移 Tool、Conversation、Model、Service 与 Agent Spawn

目标：拆除当前最高 fan-in 的 `schema`、`models`、`ports` 核心横向入口。

顺序：

1. Phase 5A：Tool identity、ToolCall、ToolResult 基础 DTO，只消费 Authorization decision。
2. Phase 5B：Conversation Message、history、context、compaction，依赖已稳定的 Tool 基础 DTO。
3. Phase 5C：Tool effects、policy 与 ports 等剩余能力。
4. Phase 5D：Model request、response、endpoint、routing、failover、usage，并以窄 inference port 替代 ModelGateway 装配对象。
5. Phase 5E：Service invocation、endpoint、outcome、receipt、failure、journal、ports；实际消除对 Model failover 的依赖。
6. Phase 5F：Agent spawn request/context、spawn policy 与 conversation/message reference。

原因：Conversation → Tool、Model → Conversation/Tool、Spawn → Agent/Conversation；先迁被依赖叶子，避免目标模块引用尚不存在的 canonical contract。

验收：

- `schema/messages.py`、`schema/context.py` 及明确属于 Tool/Conversation/Model 的 schema 内容已迁出；`schema/` 只允许保留 Phase 0B 登记、由 Phase 7E 删除的精确 baseline。
- model/conversation/tool/service 只通过模块级允许矩阵连接。
- native/XML wire shape 和模型请求快照不变。
- Context 压缩、Tool 调用、Model 路由相关 ztest 全部通过。

### Phase 6：迁移 Output、Surface 基础、Session、Runtime 与 Handoff

目标：整理持久化和恢复相关契约，建立严格兼容性门禁。

顺序：

1. Phase 6A：Output。
2. Phase 6B：Surface identity、frame、input 等无资源生命周期 DTO。
3. Phase 6C：Session。
4. Phase 6D：Runtime identity、checkpoint、operation、projection，依赖已稳定的 Surface identity 与 Session。
5. Phase 6E1：只迁 Interaction handoff，依赖 Phase 3B Interaction approval 与 Phase 6D Runtime；仅允许引用窄 Runtime identity，不引用 checkpoint/durability 聚合对象。若 0B facts 证明可删除 Runtime identity 字段，实际 manifest 可在 projected graph 通过后移除 6D 边。
6. Phase 6E2：只迁 Runtime durable handoff，依赖 Phase 6D Runtime；不吞并人类请求/响应语义。
7. Phase 6E3：独立删除旧 `contracts/handoff.py`，仅在 6E1 与 6E2 均 verified、全部旧 import 清零后执行。来自同一旧模块不构成同一原子闭包，任一业务分支不得提前删除共享旧文件。

必须先建立或补齐：

- Event tag 快照。
- Journal/replay fixture。
- Runtime checkpoint fixture。
- Output contract migration fixture。
- Artifact identity/publication round-trip。

验收：

- 现有 rollout 和 checkpoint fixture 可无迁移读取。
- Python 模块路径变化不进入持久化 payload。
- output evaluation 与 runtime delivery 的 owner 不重叠。

### Phase 7：迁移 Task、Surface 生命周期、0B 生成领域与配置

目标：清理剩余顶层孤儿模块，并统一部署配置语义。

顺序：

1. Phase 7A：Task、background task/completion/workflow progress；清理 Agent/Interaction/Task 剩余聚合。
2. Phase 7B：Surface live-session、presenter/backend ports，以及 Terminal/Notebook/Canvas 剩余契约；迁移 Interaction question 与 surface integration，approval 基础已由 Phase 3B 完成。
3. Phase 7C：只实施 Phase 0B 已冻结生成的 Execution、Observability、Hook、Code Intelligence cutover；未生成者的全部内容按 decisions move-up，不再现场裁决。
4. Phase 7D1：只迁多层共同消费、需要稳定序列化的领域 config fragments 到 `config/<domain>/`；逐片保持 alias/default/schema 兼容。
5. Phase 7D2：将仅 Product/Runtime 消费的加载、环境变量、OAuth/secret lifecycle、adapter 配置按合法 consumer closure move-up；禁止 Contracts 反向 import。
6. Phase 7D3：在 fragments 稳定后建立只做组合的 `config/deployment/`，领域不得反向依赖 aggregate。
7. Phase 7D4：迁移并验证 RoleSchema 聚合及 field alias、required/default、extra policy、discriminator、JSON schema、serialization mode 兼容性。
8. Phase 7E：仅在 7D1～7D4 全部 verified 后删除已清空的 `schema/`、`settings/` 旧目录及全部 import；两者的唯一 deletion owner 是 Phase 7E。

验收：

- `settings/`、`schema/` 不再存在；`config/` 只保留领域分区和精选 facade。
- `config.deployment` 只能依赖领域配置，任何领域不得反向依赖 deployment aggregate。
- RoleSchema 等部署聚合保持序列化与默认值兼容。

### Phase 8：Major release、旧结构拆除与迁移设施清理

Phase 0B 必须在 `contracts-migrations.toml` 中为每个 legacy root（包括 `errors/policy/schema/settings/constants/text/models` 及 inventory 发现的其他横向目录）指定唯一 deletion owner Phase；不得留到 Phase 8 决定。Phase 8 不重复认领前置 Phase 已删除的目录。进入 `release-finalization` 前必须满足：

```text
legacy_roots_remaining == []
legacy_imports_remaining == []
temporary_exceptions == []
all_phase_1_7_business_and_deletion_cutovers_verified == true
effective_blocked_cutovers == []
api_identity_event_error_manifests_current == true
deletion_cutovers_verified == true
release_version_is_next_major == true
```

`ports/`、`events/`、`config/` 不删除，终态为按领域分区的受控索引且无全局聚合 facade。Phase 8 若发现残留，只能退回其 deletion owner Phase 修复，不能在发布阶段临时搬迁未知内容。

Phase 8 本身必须进入 migration DAG，使用最终 `release-finalization` cutover：

- `depends_on` 精确列出全部 Phase 1～7 business cutover 与 deletion cutover，且全部必须 verified；不得用 Phase 编号代替枚举后的 cutover ID。
- 使用 `[[cutover.assets]]` 记录临时 import baseline、alias、迁移白名单等资产的 `delete` 动作，以及永久 dependency/API/identity/event/error governance、兼容性测试和架构门禁的 `retain` 动作。
- 该 cutover 仍遵循 prepared → approved → in_progress → verified 状态机；任何清理不得作为 manifest 外的发布脚本执行。
- `release-finalization` 不删除 `contracts-migrations.toml` 的历史、verified evidence 或永久治理资产，只将迁移期可增长例外归零并冻结历史视图。

`release-finalization` verified 后重新 snapshot/check；正式构建发布物前还必须满足最终发布门禁：

```text
all_migration_cutovers_verified == true
effective_blocked_cutovers == []
facts_match_release_head == true
api_identity_event_error_manifests_current == true
deletion_cutovers_verified == true
release_version_is_next_major == true
```

这里 `all_migration_cutovers_verified` 包含 `release-finalization` 自身，避免在其执行前形成自相矛盾的入口条件。

其资产结果必须满足：

- 验证 `contracts/__init__.py` 已随各 cutover 收敛为包说明和版本，不在 Phase 8 集中修复业务 re-export。
- 删除临时 import baseline、alias 和迁移白名单。
- 保留永久依赖矩阵、API 规则、持久化身份测试和架构门禁。

Major release 硬验收还必须全部通过：

1. 构建 wheel 与 sdist，并在不引用源码树的隔离环境安装。
2. 从安装产物导入 `contracts-api.toml` 的全部 canonical API；确认 legacy/internal modules 未被打包，package data 与发布清单一致。
3. 对 golden rollout/journal/checkpoint 执行真实恢复，并验证 Runtime 静态 event registry 与治理 manifest 一致。
4. 对完整旧 import 样例运行迁移工具 dry-run、执行和二次执行；迁移后样例项目必须通过 import/compile smoke test。
5. 生成 changelog、升级指南、旧→新映射和删除清单；校验目标版本确实提升 major。
6. 验证前一 major maintenance branch 的兼容层、版本文件和提交未被错误合并进目标 major。
7. 对相同 release commit 在清洁隔离环境构建两次，要求 `wheel_digest_build_1 == wheel_digest_build_2` 且 `sdist_digest_build_1 == sdist_digest_build_2`。若工具链存在已登记的非确定字段而无法 byte-for-byte 一致，必须比较解包后的 canonical file manifest 完全一致，并在证据中列出被排除字段、原因和 owner；不得笼统豁免。

`release-finalization` verified 后生成外部不可变发布产物 `dist/contracts-release-attestation-<version>.json`，至少记录 `release_commit/wheel_digest/sdist_digest/isolated_install_evidence/recovery_evidence/migration_tool_evidence/reproducible_build_evidence`。它不提交回被证明的源码树、不进入 wheel/sdist，也不改变 release HEAD；发布系统以不可变 artifact/签名保存。未生成或任一 digest/evidence 不匹配时不得发布。

公共 API 发布策略：

1. 当前 `mote.contracts` 视为已发布公共 API，但区分“明确 facade”与“历史偶然路径”。
2. 在目标 major version 打包并冻结 Phase 0C 起持续维护的完整旧→新路径映射、升级指南和 AST/CST 机械迁移工具；不得到 Phase 8 才编写转换逻辑。
3. 仓内在每个原子迁移阶段立即更新，不保留永久 alias。
4. 如发布流程要求兼容窗口，只允许在前一 major 的维护分支提供有截止版本、带 deprecation test 的 forwarding layer；目标 major 不携带。
5. 发布前扫描 pickle、Pydantic discriminator、日志、配置和动态加载中的全限定模块名；发现路径持久化必须先提供显式稳定 tag/migration。

验收：

- 全仓不存在旧 import path。
- Contracts 根目录无业务 `.py` 孤儿。
- 依赖图与目标矩阵一致。
- 所有临时例外归零。
- `release-finalization` 与全部前置 cutover verified，blocked 集合为空，release attestation 完整且与构建产物一致。

---

## 7. 永久治理门禁

### 7.1 AST 依赖检查

新增 `ztest/architecture/test_contracts_governance.py`，至少验证：

- Contracts 不 import 上层包。
- 所有模块只能使用封闭模块级矩阵中的边；一级领域矩阵仅作汇总。
- 禁止函数、方法和类体内 import。
- 禁止动态 `importlib`、模块级 `__getattr__` 和 import side-effect registry。
- 禁止从 Contracts 根或 `ports/events/config` 顶层聚合 facade 导入业务符号；只允许其领域子 facade。
- 禁止未批准第三方依赖。
- 检测运行时和 `TYPE_CHECKING` 边组成的概念图循环。
- 检测重复稳定身份和未登记持久化 discriminator。
- 校验 `contracts-api.toml`、领域 `__all__` 和实际稳定导出双向一致。

### 7.2 Event manifest 与 Runtime registry

核心持久化事件采用封闭集合。治理 TOML 中每个事件必须具有唯一审计记录：

```text
event class -> stable tag -> payload version -> owner domain -> decoder -> compatibility fixture
```

规则：

1. Contracts 各领域定义事件事实和稳定 tag，不执行注册。
2. Runtime 提供唯一 `build_core_event_registry()` composition 入口；其唯一运行输入是随 wheel 发布的静态 Python decoder tables。禁止 import side effect、自注册 decorator、模块扫描和读取 `zdocs`。
3. `zdocs/architecture/contracts-events.toml` 仅供 CI 治理审计；测试将治理 TOML 与静态 Python decoder tables 双向比较。Runtime 构建不以 TOML 为输入；静态 tables 出现重复 tag、重复 `(tag, version)` decoder 或缺少 decoder 时启动失败。
4. registry 覆盖率必须为 100%：所有核心 durable event 均已登记，所有登记项均有 owner 和 fixture。
5. wire namespace 与 stable tag 发布后不得无迁移改变；`current_owner` 可以通过 ADR 调整，但必须保持版本兼容、fixture 和 manifest 历史。Manifest 分别记录 `wire_owner`、`current_owner`、`ownership_history`；decoder 实现路径与 Python 模块路径不稳定。
6. 已知 tag 的未知新版本必须返回明确的 unsupported-version 结果或经过显式 upcaster，不得误按当前版本解析。
7. 未知 tag 在通用 journal 扫描时可保留并跳过；进入要求完整语义的核心恢复流时必须由该流明确决定拒绝还是忽略。
8. “开放 tagged union”只表示 envelope/tag 集合可向后兼容地扩展，不表示一个无限静态 Python `Union` 或 import-time 自注册。领域可发布自己的静态 union；Runtime core registry 是显式 manifest 的只读运行时集合。
9. 所有官方 Product 使用同一组核心静态 Python decoder tables，不允许因产品装配不同而改变核心恢复事实集合；治理 TOML 只证明其一致性。

插件规则：

- 插件不得向核心 durable registry 注册任意 Python event class。
- 插件可以发布 namespaced telemetry observation；telemetry 不参与核心恢复。
- 插件确需持久数据时，只能使用核心拥有并版本化的 `ExtensionFact` 信封，字段至少包括 namespaced `plugin_id/schema_name/schema_version`、`media_type`、bounded `JsonValue` 或 `payload_ref`、`payload_size`、覆盖规范 payload 的 `digest` 和 `sensitivity`。
- Runtime 只保证 ExtensionFact 的保存、读取、跳过和配额/安全边界；插件负责解释 payload。
- ExtensionFact 不得参与核心 Session/Runtime 恢复正确性，缺少插件时仍必须可以扫描核心历史。
- 禁止 pickle、Python class/module discriminator、arbitrary object、inline secret 和无大小上限的 dict/string；敏感内容只能使用受授权控制的 `payload_ref`，`sensitivity` 不得替代访问控制。压缩格式必须显式登记，大小限制按解压后 payload 计算；decoder 失败只隔离该 ExtensionFact，不中断核心 journal 扫描。Runtime 按 plugin namespace 执行配额、卸载后的保留/清理策略，payload ref 的生命周期与 digest 校验必须可审计。

永久测试至少包括：tag 全局唯一、owner 合法、manifest/registry 双向覆盖 100%、旧 fixture 全部可解码、未知/旧版本行为、重复注册拒绝，以及 import 无注册副作用。

### 7.3 结构审计信号

数值只触发人工评审或 ADR，不作为测试无条件失败依据，也不直接证明内聚性：

| 指标 | 评审触发值 | 评审问题 |
| --- | ---: | --- |
| 单文件 LOC | 500 | 是否仍只有一个变化轴；wire schema 可说明后保留 |
| 单模块直接 Contracts 依赖 | 8 | 是否是合理聚合，能否改为窄 ID/value |
| 单领域 facade 公开符号 | 60 | 是否需要子领域 facade，稳定事件集合可说明后保留 |
| 单个 Protocol 方法数 | 7 | 消费方是否真的同时需要全部方法 |
| `__init__.py` LOC | 30 | 是否仍为精选 facade，有无内部符号泄漏 |
| `foundation` | 500 LOC、20 个公开符号或总 LOC 5% | 是否正在重建 common；未获 ADR 不得继续新增 |

真正的硬门禁仅包括非法层依赖、非法领域边、SCC、动态/局部 import、未批准第三方依赖、重复稳定身份、未登记持久化 discriminator，以及核心 event registry 覆盖不完整。

### 7.4 兼容性门禁

以下契约必须有机器可执行的快照或 round-trip 测试：

- Event name/tag 与 payload schema。
- Error code 与 recovery action。
- Message、ToolCall、ModelResponse wire shape。
- rollout/journal/checkpoint payload。
- Output contract ID、version 与 migration path。
- Artifact、Runtime、Session、Agent 等稳定 identity。
- 配置字段、默认值和 extra policy。

破坏性变更必须：

1. 显式提升 contract version。
2. 提供迁移器或明确拒绝旧版本的边界行为。
3. 增加旧 fixture 到新模型的测试。
4. 不依赖 Python 模块路径自动推断版本。

### 7.5 新增契约评审模板

每个新增 Contracts 类型必须回答：

1. 它属于哪个领域？
2. 哪两个层、进程或持久化边界需要共同认识它？
3. 谁拥有其版本和兼容性？
4. 为什么现有契约不能表达？
5. 它是否包含算法、展示或资源生命周期？如有，如何拆出？
6. 它新增了哪条领域依赖边？该边是否已在矩阵中允许？
7. 需要哪些序列化、兼容性和架构测试？

无法回答第 2 项的类型，默认不进入 Contracts。

### 7.6 公共模型质量门禁

所有 stable 或持久化模型必须满足：

- `Any` 必须逐字段说明无法收窄的边界理由；无说明禁止。
- 持久化/wire 模型禁止用裸 `dict`/`set` 表达 schema；stable 内存 API 可使用限定 key/value 的 `Mapping[str, JsonValue]`、`AbstractSet[str]` 等抽象容器。Set 进入 wire 时必须 canonical 排序；开放 JSON payload 必须使用 bounded `JsonValue` 和明确 envelope。
- ID 声明字符集、大小写、最大长度、命名空间和规范化 owner。
- Enum/Literal 声明未知值策略；不得假设新增枚举值对旧消费者天然兼容。
- 时间值必须声明 UTC/offset-aware 语义；禁止持久化 naive datetime。
- 金额声明币种和十进制定点精度，不使用二进制浮点表示结算值。
- Protocol 参数不得接收跨域 container、Role、state、environment 或 service locator。
- Python class/module 全限定名不得作为新 discriminator；历史 module discriminator 必须有显式迁移映射。
- 每个 codec 声明 schema version、支持版本、upcaster/拒绝策略和 canonical round-trip fixture。
- Pydantic 模型明确 frozen/extra policy，但不强制全部 frozen；配置 DTO 与运行状态 DTO 不复用同一模型。配置兼容快照必须覆盖 field alias、required/default、extra policy、discriminator、JSON schema 和 serialization mode。

这些是硬门禁；现有违反项进入精确 baseline，只能减少并必须绑定退出 Phase。

---

## 8. 测试与验证策略

### 8.1 每阶段最低验证

每个迁移批次至少执行：

```bash
python -B -m pytest \
  ztest/architecture \
  ztest/contracts \
  -q --tb=short -p no:cacheprovider
```

并执行目标领域及其直接消费方测试。例如：

- File：`ztest/fileops`、`ztest/session`、`ztest/executor`。
- Conversation：`ztest/context`、`ztest/session`、`ztest/roles`。
- Model：`ztest/router`、`ztest/oauth`、模型 provider 测试。
- Tool：`ztest/executor`、`ztest/parser`、`ztest/roles`。
- Runtime/Session：`ztest/session`、`ztest/cli`、`ztest/roles`。

上述列表只是人类示例，不是机器真相源。Phase 0A 的 `contracts-tests.toml` 必须为每个 symbol/edge 记录当前存在的测试路径和最低命令；cutover 时由工具生成实际命令。最低覆盖至少包括：Phase 2B 的 events/session/replay/runtime events；Phase 3B 的 permission/interaction/roles；Phase 4 的 fileops/session/executor；Phase 5A～C 的 parser/executor/context；Phase 6 的 output/session/runtime/handoff；Phase 7D/E 的 config/roles/CLI 或 Product composition。测试路径移动或删除时必须在 cutover 前更新并验证 manifest，不允许静默跳过。

### 8.2 静态验证

每阶段必须保存并比较：

- Contracts 模块 DAG。
- 一级领域边集合。
- SCC 数量，必须始终为 0。
- 旧 import path 数量，必须单调减少。
- 临时例外数量，必须单调减少。
- 第三方依赖集合，不得增长。
- 根目录业务模块数量，必须单调减少。

### 8.3 提交粒度

提交边界服从三段迁移协议：准备提交只增加事实、fixture 和门禁；原子 cutover 提交必须同时移动一个符号闭包、更新全部 import/facade/manifest 并删除旧入口；验证提交不再改契约定义。这里的“原子变化”指一个语义不变的 cutover 闭包，不禁止为保持唯一真相源而在同一提交完成移动、import 更新与旧入口删除。

禁止在路径搬迁中顺带改变契约语义；确需语义升级时必须先以独立 version/ADR 提交完成兼容设计，再执行路径 cutover。

---

## 9. 风险与控制

| 风险 | 后果 | 控制措施 |
| --- | --- | --- |
| 模块路径被写入持久化数据 | 旧 rollout/checkpoint 无法恢复 | Phase 0A 清点 discriminator；fixture 验证；显式稳定 tag |
| 领域拆分产生双向依赖 | 用 `TYPE_CHECKING` 掩盖概念循环 | 检查包含类型边的完整图；下沉窄 identity 或上移聚合 |
| `foundation` 变成新 common | 债务换名 | 标准库限定、三领域证明、500 LOC/20 public symbol/5% 复审触发器 |
| 迁移期 re-export 永久化 | 形成双公共 API | 同阶段更新全部 import 并删除旧模块 |
| 配置字段迁移改变默认行为 | 部署回归 | 配置 dump/schema/default 快照 |
| 事件按领域拆分后 registry 漏注册 | replay/telemetry 丢事件 | Runtime 显式装配；事件身份清单与覆盖测试 |
| 机械拆文件降低可读性 | 目录更深但内聚性未提升 | 以变化轴和 owner 拆分，不追求目录对称 |
| 多批次并行修改同一门面 | 冲突和临时状态扩散 | 按领域串行迁移公共入口；迁移 owner 独占批次 |

---

## 10. 已决策事项与机器执行权限

### 10.1 已决策

1. 采用领域优先；`ports/events/config` 按 AGENTS 作为领域分区索引保留，其他技术类型不作为一级组织。
2. 禁止 Contracts 根级及 `ports/events/config` 全局聚合 facade，允许显式、精选、快照化的领域 facade。
3. `config/deployment` 只做领域配置聚合，不建立根级 `deployment/`。
4. `foundation` 只允许标准库。
5. 核心 durable event 由 Runtime 唯一入口显式装配，插件不得扩展核心 registry。
6. Hunk value 留在 File，apply/revert/diff 算法迁 Runtime。
7. `mote.contracts` 是公共 API；目标结构通过 major release、映射和机械工具迁移，不保留跨 major 永久 forwarding module。
8. 数值预算只触发评审；非法依赖、循环、身份冲突和 registry 不完整才是硬门禁。
9. Authorization 独立于 Tool 和 Interaction；Handoff 拆为 Interaction handoff 与 Runtime durable handoff，不建立 composition 包。
10. Content、Service 是独立领域；Service 不依赖 Model failover。
11. WorkflowControl 原样迁出 Contracts；RunContext 和 ModelGateway 不得作为跨域装配对象原样保留。

### 10.2 执行权限来源

执行权限只来自 `contracts-phase-gates.toml` 与 `contracts-migrations.toml`：Phase gate 满足依赖和入口后可进入 in_progress；具体生产变更只允许执行 status=approved、baseline current、适用类型入口/出口通过且全部前置 verified 的 cutover。Unknown 可以关联到具体 symbol 供诊断，但任一 unknown 都使 Phase 0B 无法 verified，继而全局阻塞 0C 与 Phase 1～8；其他 blocked reason 或已完成 0B 后新出现的 cutover 局部失败，按 manifest DAG 传播。

Markdown 不保存“当前批准范围”。Phase 0A/0B/0C 按各自机器出口推进；Phase 1～8 不因文档版本、标题顺序或人工口头结论获得权限。需要 ADR 的高风险决策将 ADR 引用写入 decisions/migrations 后，由同一 CLI 验证。

---

## 11. 完成定义

只有同时满足以下条件，本治理才算完成：

- 所有 Contracts 内容都有唯一领域 owner。
- 所有现存公开符号均有 disposition、消费层、稳定身份和目标模块记录。
- 除 AGENTS 指定且已按领域分区的 `ports/events/config` 外，横向技术目录和顶层业务孤儿全部删除。
- Contracts 中不存在实现型通用工具和未批准第三方依赖。
- 领域依赖图符合封闭矩阵且无循环。
- 所有跨领域 import 精确到模块级批准；`ports/events/config` 物理索引不放宽语义依赖。
- 全仓只通过精选业务 facade 或 `ports/events/config` 的领域子 facade 使用稳定公共 API；深层内部模块不作为跨领域 API。
- 持久化身份、wire shape、错误 code 和配置默认值通过兼容性测试。
- 临时 baseline、alias、forwarding module 和例外全部清零。
- 永久架构测试、兼容性测试、依赖矩阵和新增契约评审模板进入日常开发流程。
- 所有 Protocol 有明确需求方，不存在跨域 service locator；所有 stable API 进入机器可读 manifest。
- 新成员可从 `contracts/` 一级目录识别能力，从 Domain Manifest/生成导航查看跨 DTO/ports/events/config 的完整领域，从各 canonical facade 理解稳定公共能力。

达到上述状态后，Contracts 不再是“公共类型放置处”，而是整个 Mote 五层架构最稳定、最可审计的领域语言地图。
