# Mote Python 包治理策略架构审核报告

- 审核对象：`zdocs/PYTHON_PACKAGE_GOVERNANCE_STRATEGY.md`
- 审核范围：五层包治理、迁移协议、治理事实、自动化门禁、阶段编排与发布闭环
- 审核结论：架构原则通过；Phase -1 经拆分后可执行；当前不得直接顺序执行 Phase 0～6
- 审核性质：需求与执行方案审核，不代表任何生产迁移已获批准

---

## 1. 总体裁决

策略确立的核心方向正确，应作为后续治理保留：

```text
contracts <- kernel <- runtime <- orchestration <- product
```

它正确强调了领域 owner、单一状态真相、生命周期归属、显式装配、窄 Port、持久身份稳定、原子 cutover 和机器门禁。Draft v3 也已经正确吸收以下关键要求：

- 不再信任硬编码 coverage 和旧 `Phase 0A = verified`；
- Contracts 按 DTO、持久事件、Protocol、ID/value object 分类准入；
- public API 与仓内 API 分开治理；
- symbol identity 与 Python 路径解耦；
- facts 与人工 decision 分离；
- decision 绑定局部 source、signature、closure 和 identity digest；
- 动态加载、生命周期、持久身份和发行资源进入治理清单；
- 静态架构测试必须 hermetic；
- Runtime 与 Product 的 provider、LSP 职责得到明确；
- public API 兼容窗口与 internal 原子删除得到区分。

但是，当前 Phase 1～5 仍按层线性排列，而实际迁移大量跨层；多份分层治理计划又描述了已经部分落地的旧源码状态。因此，现版本还不能作为“从 Phase 0 顺序执行到 Phase 6”的执行授权。

最终评级：

| 审核项 | 结论 |
| --- | --- |
| 五层架构与 owner 原则 | 通过 |
| Contracts/Kernel/Runtime/Orchestration/Product 职责 | 有条件通过 |
| 治理事实模型 | 方向通过，现有实现不可信 |
| Phase -1 | 拆分后可执行 |
| Phase 0 | 补齐真实 Gate 后可执行 |
| Phase 1～5 | 不得按整层串行执行 |
| Phase 6 | 必须拆分 internal closure 与 release closure |
| 全局生产迁移授权 | 暂不通过 |

---

## 2. 当前事实与主要证据

### 2.1 现有治理事实存在假闭环

当前 Contracts 治理生成器直接写入多个 `coverage = 100`，但实际治理产物中仍存在大量 unknown，identity/event/error 清单也没有真实覆盖。文件存在和 baseline ID 相同只能证明产物格式存在，不能证明语义事实完整。

当前实现还存在以下问题：

- public API 由“是否有仓内 production consumer”推导，混淆内部 API 与已发布 API；
- `stable_symbol_id` 由 canonical import 哈希生成，移动模块会改变所谓稳定 ID；
- consumer scanner 不能完整覆盖 alias、re-export、动态引用、entry point 和配置 dotted path；
- 全工作树 digest 会因无关文件变化使 Contracts decision 失效；
- 旧 decisions 仍引用已不存在的模块路径；
- migration manifest 为空时，历史 Phase 仍出现 `verified` 状态。

因此，现有 facts、coverage、decision 和 phase status 只能作为候选输入，不能批准生产 cutover。

### 2.2 静态架构测试尚未 hermetic

静态 AST 架构测试在 pytest 收集阶段会触发根 `mote`、Product composition、Toolsets 和终端依赖导入。在当前环境中，收集会因可选依赖或部分初始化循环失败。

静态治理门禁应满足：

- 从文件路径加载 scanner/validator；
- 不执行 `mote/__init__.py`；
- 不导入 Product composition、CLI、TUI、PTY 或 provider SDK；
- 标准库加 pytest 即可运行；
- 静态检查、Runtime import smoke、public API clean-install smoke 和 optional-extra smoke 分开。

### 2.3 旧分层计划与当前源码漂移

多项旧计划动作已经在源码中落地或部分落地，例如：

- `contracts.surface`、`contracts.output` 已存在；
- `orchestration.environment`、`orchestration.tasks` 已删除；
- `orchestration.agents`、`orchestration.background_tasks` 已建立；
- `product.entrypoints`、`product.composition`、`product.models` 已建立；
- `product.cli`、`product.integrations`、`runtime.tools.dependency`、`runtime.paths.py` 已删除。

与此同时，代码注释、docstring、测试描述和旧 governance manifests 仍出现旧路径。必须先区分“已完成”“只移动目录但未闭合 owner”“已实施但未验证”“已过时”，否则会重复执行旧迁移。

### 2.4 Runtime 图指标需要统一解释

现有 Runtime 指标工具报告多个非平凡 SCC，而另一套 import-cycle 测试使用不同图语义。两者可能在 package facade、`__init__`、type-only edge、最近模块解析等方面不同。

在统一图算法前，不能以任一 SCC 数字作为 Phase 3 的批准或完成证据。应分别定义：

```text
runtime_initialization_scc
type_dependency_scc
domain_package_scc
```

---

## 3. 阻止顺序执行全部 Phase 的问题

### P0-1：层级 Phase 与跨层 cutover 相互等待

当前顺序是 Contracts → Kernel → Runtime → Orchestration → Product，但实际迁移关系包括：

- Contracts 实现上移 Kernel、Runtime、Orchestration 或 Product；
- Kernel 策略上移 Runtime/Product；
- Runtime 产品策略上移 Product，多实体策略上移 Orchestration；
- Orchestration 的具体 Role/Toolset adapter 上移 Product；
- Product 又依赖下层先提供稳定 Port 和运行机制。

这会形成阶段级等待环。五层应作为 owner Track，而非全局互斥阶段。实际执行单位必须是全局 DAG 中的 cutover 节点。

### P0-2：Phase 1～6 没有机器可判定的成员与出口

每个阶段都必须有：

```text
scope_manifest
entry_gates
member_cutover_ids
required_predecessors
exit_gates
allowed_deferred_items
forbidden_residuals
verification_commands
evidence_artifacts
completion_authority
```

仅有目标和任务列表，无法判断阶段是否完成，也无法避免把遗漏迁移推给最终清理阶段。

### P0-3：migration 状态机与 Git/CI 流程不闭合

生产原子 cutover、commit hash 和事后验证不可能全部写在同一个 commit 中。应明确三类提交/证明：

1. prepare/approval attestation；
2. atomic production cutover commit；
3. verification/completion attestation。

“原子”只约束生产定义、消费者切换和 internal 旧路径删除必须在同一个 production commit 中完成。

状态机还需增加：

```text
blocked
verification_failed
aborted
```

已经产生 cutover commit 的失败记录不得通过 `superseded` 被改写成未发生；后继修复必须以 `repairs` 或 `supersedes` 关联旧记录。

### P0-4：Phase 6 与公共 API 兼容窗口冲突

Internal 旧路径可以在当前治理周期归零，但 stable public API 可能必须经历 deprecation release 和 removal release。两者不能用同一个 Phase 完成条件。

必须拆分：

- Internal closure：internal 旧路径、双实现、临时例外、文档漂移和发行物问题归零；
- Release closure：deprecation 发布、兼容窗口、breaking/removal 发布和 public 旧路径最终归零。

项目可先达到 `internally_complete`，待发布条件满足后达到 `release_complete`。

### P0-5：旧计划没有完成当前状态对账

旧 Contracts、Kernel、Runtime、Orchestration、Product 治理计划中的每个动作必须分类为：

```text
planned
already_implemented_unverified
implemented_verified
partially_implemented
obsolete
superseded
still_required
```

未经对账的旧计划不能直接进入全局 DAG。

---

## 4. 治理元模型必须补齐的内容

### 4.1 唯一的包边界权威

建立机器可读的 `package-boundaries.toml`，统一定义：

```text
layers
domains
capabilities
allowed_edges
forbidden_edges
facades
dynamic_loaders
temporary_exceptions
```

架构测试读取该 manifest；AST 工具生成实际依赖图。Manifest 只表达架构规则，不手工复制所有实际 import 边。临时例外必须独立记录，并绑定 migration ID、owner 和到期节点。

### 4.2 统一图语义

所有扫描器必须共享同一 module normalization 和 edge model，至少记录：

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

必须统一处理 relative import、alias、`__init__`、facade re-export、annotation string、literal dynamic import 和 package module。

### 4.3 真实 coverage

Coverage 必须按真实候选集合计算：

```text
(confirmed + not_applicable) / candidate_total
```

每项必须输出 numerator、denominator、unknown、conflicted、unsupported 和 scanner version。分母为零不自动等于 100%。

### 4.4 Evidence 模型

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
```

证据必须 append-only；失败证据不能被覆盖。`verified` 只引用不可变 evidence ID。命令定义变化后，旧证据不再满足新 Gate。

### 4.5 Packaging 与资源事实

治理候选必须覆盖：

- `package_data`；
- `MANIFEST.in`；
- project scripts 与 entry point；
- `importlib.resources`；
- 基于 `__file__` 的相对读取；
- glob 资源；
- 平台二进制；
- optional import 和 extras。

移动包路径时，必须验证 wheel、sdist、clean install、CLI、资源读取和 public API snapshot。

### 4.6 Touched closure 与类型门禁

为避免包迁移扩张成无限类型重构，区分：

```text
moved_definition
changed_signature
changed_import_only
direct_consumer
transitive_type_consumer
```

规则：

- 新增或改变的公开签名必须满足完整类型要求；
- 仅更新 import 的消费者不得新增类型债，但不强制清除整个文件的既有债；
- 被直接修改的违规表达式应修复；
- 未触及的旧问题进入精确基线；
- 需要改变业务行为的类型修复必须拆为独立 migration。

---

## 5. 修订后的可执行总路线

### Phase -1A：撤销旧治理授权

动作：

- 将旧 `Phase 0A = verified` 标为 `untrusted_legacy`；
- 禁止旧硬编码 coverage、空 manifest 和 stale decision 参与批准；
- 暂停生产包 cutover；
- 保留旧产物用于审计，不把它们伪装成当前事实。

退出条件：任何执行路径都无法使用旧证据批准迁移。

### Phase -1B：冻结治理 schema

定义并版本化：

- facts；
- classification；
- decision；
- migration；
- phase/track；
- evidence；
- exception；
- API/release；
- identity；
- lifecycle；
- dynamic loading；
- packaging。

退出条件：所有 schema 有自校验和兼容策略。

### Phase -1C：建立 hermetic CLI

建议命令：

```text
governance discover
governance graph
governance classify-check
governance decision-check
governance migration-check
governance evidence-check
governance release-check
```

退出条件：在不 import `mote`、不安装 Product optional dependencies 的最小环境中稳定执行。

### Phase -1D：统一扫描器与图

顺序建立：

1. module/import graph；
2. re-export graph；
3. symbol graph；
4. dynamic/string reference candidates；
5. identity/event/error/discriminator candidates；
6. lifecycle/registry/queue/journal candidates；
7. public/plugin API evidence candidates；
8. packaging/resource candidates。

退出条件：所有工具使用同一图模型，unknown/unsupported 显式可见。

### Phase -1E：当前状态对账

对五份旧分层计划逐项标记实施状态，生成新的 remaining cutover candidates。

退出条件：不存在会被重复执行的旧动作；已实施未验证项进入补验收节点。

### Phase -1F：建立全局 Cutover DAG

五层改为 Track：

```text
Track C: Contracts
Track K: Kernel
Track R: Runtime
Track O: Orchestration
Track P: Product
Track X: Internal/Release Closure
```

每个节点至少记录：

```text
cutover_id
owner_track
source_layer
target_layer
depends_on
source_symbols
target_symbols
target_owner_readiness
consumer_closure
identity_fixtures
api_policy
test_plan
packaging_plan
rollback_or_forward_fix
cleanup_node
```

退出条件：DAG 无环；每个节点 owner、目标和 cleanup 唯一；迁移后虚拟图合法。

### Phase -1G：试点域冻结与执行

试点要求：

- 10～30 个模块；
- 无持久 identity；
- 无 Task、PTY 或后台资源；
- public API 已明确分类；
- consumer closure 可人工复核；
- 不涉及 breaking release。

顺序：

1. 先冻结试点域 facts；
2. 完成 classification、identity、ownership、closure；
3. 批准测试、发行和失败处理；
4. 提交 prepare attestation；
5. 完成 atomic production cutover；
6. 保存 verification evidence；
7. 提交 completion attestation；
8. 故意制造 drift、unknown 和测试失败，证明门禁 fail closed。

退出条件：试点闭环真实完成，且治理工具能正确拒绝无效证据。

### Phase 0：扩展可信事实到全仓

退出条件：

- 所有生产文件成功解析；
- 所有扫描器支持的候选类别完成枚举；
- unsupported 与 unknown 显式记录；
- 首批 DAG 节点的 Classification、Identity、Ownership、Closure Gate 全部通过；
- 其他域为明确 `unreviewed`；
- 不使用笼统的“facts 100%”。

### Execution Waves：按 DAG 拓扑执行

不再要求整层完成后才进入下一层。建议风险波次为：

1. 叶子契约与低风险 internal move；
2. Kernel/Runtime 语义边界；
3. Runtime/Orchestration 单实体机制与多实体策略边界；
4. Product composition、provider、LSP、Toolset、Skills、Paths 和 Config；
5. Session/replay、event/error/discriminator、Tool identity、public/plugin API 等高风险资产。

波次只用于发布和资源规划，不替代节点级 `depends_on`。

### Internal Closure

完成：

- internal 旧 import/path 归零；
- 双实现和双真相源归零；
- Runtime/Kernel/Product 等目标图 SCC 归零；
- 临时例外归零；
- 架构文档与源码一致；
- wheel/sdist clean-install 验证。

达到状态：`internally_complete`。

### Release Closure

完成：

- deprecation release；
- 批准的兼容窗口；
- breaking/removal release；
- public/plugin 旧路径归零；
- 迁移工具、release notes 与外部兼容证据完成。

达到状态：`release_complete`。

---

## 6. Cutover 的标准入口与出口

### 6.1 入口

一个节点只有同时满足下列条件才能进入 `approved`：

- source/target symbol 与 owner 唯一；
- source、signature、closure 和 identity digest 当前有效；
- 所有 `unknown_reference` 已清零或经显式裁决；
- target owner 已准备好接收；
- projected graph 无反向边和新增 SCC；
- API visibility/stability 与 release policy 已批准；
- identity fixture 已建立或明确 N/A；
- lifecycle 和关闭责任已确认；
- test/packaging/evidence plan 已批准；
- rollback 或 forward-fix 策略明确；
- 所有 predecessor 已达到要求状态。

### 6.2 原子生产切换

同一个 production cutover commit 必须完成：

- 移动唯一生产定义；
- 切换全部批准的仓内消费者；
- 更新 composition root 和 facade；
- 更新 package data/entry point；
- 删除 internal 旧定义、旧导出和旧路径；
- 不引入未批准的兼容层。

### 6.3 验证

至少验证：

- 旧 internal import 和定义归零；
- 实际图与 projected graph 一致；
- owner 包、直接消费者和架构测试通过；
- Pyright、Black、Isort 与 import gate 通过；
- identity fixture、replay/codec 按适用范围通过；
- clean-install wheel/sdist 按适用范围通过；
- 工作树无未登记残渣；
- ARCHITECTURE 和快速定位同步。

### 6.4 统一 Phase/Track 出口

```text
all_member_cutovers_terminal
no_failed_or_cutting_over_member
no_overdue_exception
no_new_upward_edge
no_new_scc
all_touched_unknown_references_resolved
all_touched_public_api_classified
all_touched_identities_verified
all_required_distribution_smokes_passed
documentation_current
```

---

## 7. 最终批准条件

只有以下条件全部满足，才批准按全局 DAG 顺序执行生产治理：

1. 旧 Phase 0A、硬编码 coverage 和空 manifest 已从授权链移除；
2. 治理 schema 与 evidence 模型已冻结；
3. hermetic CLI 可在最小环境运行；
4. 所有图工具共享统一 edge/module 语义；
5. public API、identity、lifecycle、dynamic loading 和 packaging 有真实候选清单；
6. 五份旧分层计划已完成现状对账；
7. Phase 1～5 已改为 Track + 全局 Cutover DAG；
8. migration 状态机支持 verification failure、不可变历史和 Git/CI attestation；
9. internal closure 与 release closure 已分离；
10. touched closure 和类型治理范围已冻结；
11. Runtime 现有 SCC 已按统一图语义得到 disposition；
12. 试点完整通过，并证明 drift、unknown、测试失败会 fail closed；
13. 首批生产节点完成事实 Gate、projected graph、精确测试和发行审查；
14. 每个生产节点都可按 DAG 独立批准、执行、验证和失败处置。

满足这些条件后，审核结论可升级为：

> 架构治理系统可信；允许按全局 Cutover DAG 的拓扑顺序执行全部治理节点。

在此之前，允许执行的范围仅包括治理工具、事实模型、测试隔离、现状对账和人工可完全验证的试点，不允许依据旧 governance 状态批量移动生产包。

---

## 8. 最终建议

下一步不应继续增加抽象原则，而应提交四个具体产物：

1. `package-boundaries.toml`：唯一机器边界权威；
2. `governance-schema/`：facts、decision、migration、evidence、release 等 schema；
3. `current-state-reconciliation`：旧计划与当前源码对账结果；
4. `global-cutover-dag`：首批节点、依赖、Gate、测试和 cleanup。

这四项完成后，再选择低风险试点跑通从事实发现到验证 attestation 的全过程。只有试点证明治理系统不会给出假 coverage、假 verified 或错误 consumer closure，才应启动规模化包治理。

---

## 9. 第四轮审核补充：治理控制面与运行安全

Draft v4 已经吸收 Track、Cutover DAG、Evidence、失败状态和 Internal/Release Closure 等核心意见。继续审查后，剩余阻断项集中在治理控制面本身：谁有权批准、并行迁移如何隔离、持久模块身份如何处置，以及何时能安全恢复中断的 cutover。

### 9.1 P0：批准者与职责分离尚未定义

策略记录了 `actor`、`runner_identity` 和 `completion_authority`，但没有定义角色、权限和互斥关系。如果同一主体可以提出 decision、批准 migration、执行 cutover、产生证据并标记 completed，attestation 只能证明“有人写了记录”，不能形成可信控制。

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

最低规则：

- proposer 不能单独批准自己的 migration；
- source 和 target 跨 owner 时，两侧 owner 都必须批准；
- 涉及 public/plugin API 时必须有 release owner；
- 涉及 journal、codec、identity 或 replay 时必须有 persistence reviewer；
- 涉及权限、sandbox、secret 或命令执行路径时必须有 security reviewer；
- executor 不能伪造 evidence，正式 evidence 由受信 CI runner 产生；
- completion authority 只能在所有必需 reviewer 和 Gate 满足后签发 completion attestation。

还必须明确 actor identity 的信任来源。普通 TOML 字符串不能作为审批认证；至少应绑定代码评审身份、受保护分支提交签名或 CI 平台 attestation。

### 9.2 P0：并行 Cutover 缺少冲突与占用协议

全局 DAG 允许无依赖节点并行，但“DAG 中没有依赖”不等于文件和语义上无冲突。两个节点可能同时修改：

- 同一个 facade 或 `__all__`；
- composition root；
- package-boundary manifest；
- session codec 或 event registry；
- `pyproject.toml`、`setup.py`、`MANIFEST.in`；
- 同一个 public symbol 的 re-export chain；
- 同一 lifecycle resource 或 registry。

每个 cutover 必须在 approval 前声明写集合：

```text
claimed_modules
claimed_symbols
claimed_facades
claimed_manifests
claimed_identities
claimed_resources
claimed_packaging_entries
```

调度器或 validator 应构造 conflict graph。只有 `depends_on` 已满足且 claim 集合不冲突的节点才能并行进入 `cutting_over`。共享 composition root 等热点必须显式串行化，不能依赖 Git 最后合并时发现冲突。

Approval 还应绑定 base commit 和 merge policy：

- base commit 或 governed facts 变化后重新计算 claim/closure/projected graph；
- rebase、merge 或 cherry-pick 后旧 approval 默认失效；
- 只有证明相关局部 digest 未变时才可复用批准；
- 不允许两个 cutover 同时声称删除或成为同一 symbol/identity 的 owner。

### 9.3 P0：切换过程缺少 crash-safe 恢复协议

`cutting_over`、`verification_failed` 和 rollback/forward-fix 已有定义，但尚未描述治理进程或 CI 在状态转换中断时如何恢复。例如：

- production commit 已合入，但 manifest 仍是 `approved`；
- verification job 已通过，但 completion attestation 未写入；
- 一部分 evidence 上传成功，另一部分丢失；
- rollback commit 已合入，但原 migration 状态未更新；
- CI 重试导致重复 attestation。

状态转换必须是幂等、可对账的。建议增加：

```text
transition_id
expected_previous_state
observed_source_commit
resulting_commit
idempotency_key
evidence_set_digest
reconciliation_status
```

恢复器根据 Git 历史、manifest 和 evidence store 对账，而不是盲目重放动作。重复 transition 使用同一 idempotency key 必须得到相同结果；冲突 transition 必须 fail closed。

### 9.4 P0：当前 Role 持久身份仍存在模块路径耦合风险

策略正确要求持久身份不依赖模块路径，但当前源码仍有需要优先冻结的高风险事实：

- `BaseRole` registry 已支持稳定 `role_type_id`；
- `session_meta.role_class` 被用于 resume identity；
- 某些写入路径使用 `role_type_id`，另一些路径仍写入 `module.qualname`；
- `_qualified_name()` 仍直接生成模块限定名。

因此，“module-qualified persistence identity”不是抽象候选，而是当前实际迁移风险。任何移动 `runtime.agent.Role`、具体 Role 子类或 Product Agent 类型的 cutover，必须先：

1. 枚举所有 role identity 写入路径；
2. 明确 canonical `role_type_id`；
3. 冻结旧 `module.qualname` reader；
4. 添加新旧 journal golden fixture；
5. 证明旧 session 可恢复到新类；
6. 禁止新 writer 继续写模块路径；
7. 将旧 reader 作为持久兼容 reader，而不是 internal forwarding 残渣。

在完成该专项节点前，Agent/Role 类路径迁移必须列为 forbidden cutover。

### 9.5 P1：配置状态分类过于粗糙

策略把配置统一描述为“部署期静态”，但当前系统存在至少四类配置：

```text
deployment_static
startup_resolved
reloadable_generation
session_pinned
```

例如模型 composition 可能在运行中产生新 generation，而已有 Session 必须继续引用其固定 snapshot。包移动不能仅验证字段和默认值，还必须验证：

- reload 是否改变既有 Session 语义；
- generation identity 是否稳定；
- snapshot 的 retention/GC owner；
- resume 时如何重建原 generation；
- 配置来源优先级是否保持；
- secret 值不会进入治理 facts/evidence。

配置 inventory 应记录 `mutability`、`resolution_time`、`pinning_scope`、`generation_identity`、`reload_owner` 和 `secret_classification`。

### 9.6 P1：治理事实可能泄漏 secret 与本机路径

Facts、environment fingerprint、stdout 和 worktree manifest 可能包含：

- API key 或 secret 配置值；
- 用户 home 和绝对路径；
- 私有 endpoint；
- 临时凭证；
- 日志中的用户输入；
- 私有插件名称或仓库 URL。

Evidence schema 必须增加数据分级和脱敏规则：

```text
data_classification
redaction_profile
contains_user_content
contains_secret_material
retention_policy
access_scope
```

治理扫描器原则上只记录结构、相对路径、哈希和类型，不读取或持久化 secret 值。Environment fingerprint 应使用批准字段白名单，不能 dump 全部环境变量。stdout 在入库前必须经过脱敏，并对原始敏感输出设置受限保留。

### 9.7 P1：Schema 的规范编码与并发写入未定义

Digest 只有在规范编码稳定时才有意义。治理 schema 必须定义：

- UTF-8 与换行格式；
- key/list 排序规则；
- 路径规范化；
- 时间格式与时区；
- 空值、缺省值和浮点数编码；
- symlink 与文件 mode 表示；
- schema upgrade 后 digest 是否重算；
- facts 生成的原子写入和锁；
- 人工 decisions 不被生成器覆盖。

建议 digest 基于版本化 canonical JSON，而 TOML 只作为人工评审视图。生成器先写临时文件、校验完整性，再原子替换；并行生成必须通过 lock 或 compare-and-swap 防止最后写入者覆盖新事实。

### 9.8 P1：源码当前状态与“Role 是普通 ABC”表述不完全一致

策略沿用“Role 是普通 ABC”的设计表述，但当前 `Role` 实际继承 `BaseRole`，`BaseRole` 也不是 `ABC`，而是通过 `NotImplementedError` 提供名义基类和持久 registry。该差异不必立即重构，但治理文档必须区分：

- 目标不变量：Role 不是 Pydantic，不承担配置/状态模型职责；
- 当前事实：Role 是普通 Python 基类体系，不是严格 `abc.ABC`；
- 是否改为 ABC 属于独立设计决策，不应在包迁移中顺手改变。

否则架构测试若机械检查 `ABC`，会把当前合法实现误判为违规。

### 9.9 P1：根公共 facade 的 eager import 是发行与测试风险

`mote.__init__` 直接导入 `Engine`，而 `Engine` 会导入 Product composition 和大量 Runtime 能力。这已经导致静态 pytest 收集被可选依赖阻断。除了让架构测试隔离加载，还必须单独决定公共 facade 的 import budget：

- `import mote` 允许加载哪些模块；
- 是否允许读取配置、用户目录或创建 registry；
- core install 与 optional extras 的边界；
- public facade 是否必须在缺少 UI/PTY/provider extra 时可导入；
- import latency、线程、task、socket 和文件写入预算。

该问题不能只通过测试绕过根 facade。应建立 subprocess import smoke，并记录 loaded modules、线程、task、socket 和文件系统副作用。若公共 API 承诺 `import mote` 在 core 环境可用，则 Product eager import 必须实际收窄。

### 9.10 P2：试点规模不应仅以模块数定义

“10～30 个模块”只是粗略上限。一个 10 模块切片也可能包含大量消费者、public API 或资源。试点选择还应满足量化风险预算：

```text
max_changed_symbols
max_direct_consumers
max_claimed_hotspots
max_public_api_changes = 0
max_persistent_identity_changes = 0
max_lifecycle_resources = 0
max_packaging_entries
```

试点的首要条件应是 closure 可穷举和风险低，而不是目录大小。

---

## 10. 更新后的批准前置条件

在第 7 节已有条件之外，还必须满足：

1. 审批角色、职责分离和 actor 信任来源已机器化；
2. Cutover claim/conflict graph 和并行调度规则已建立；
3. 状态转换具备幂等键、CAS 前置状态和 crash reconciliation；
4. Role/module-qualified 持久身份专项已冻结并有兼容 fixture；
5. 配置 mutability、generation、pinning 和 reload owner 已分类；
6. Governance facts/evidence 有 secret、用户内容和本机路径脱敏策略；
7. Canonical encoding、原子写入和并发生成规则已冻结；
8. Role 的“普通 Python 基类”当前事实与目标约束已统一表述；
9. 根 public facade 的 import budget 和 core/optional dependency 契约已批准；
10. 试点满足 closure 风险预算，而不只满足模块数量范围。

完成这些条件后，治理系统才同时具备架构正确性、审批可信性、并行安全性、崩溃可恢复性和发行可用性。

---

## 11. 第五轮审核补充：行为等价、测试证据与发布矩阵

继续审核后，Draft v5 在治理控制面方面已经更完整，但仍缺少三类决定“能否安全迁移”的硬事实：行为等价的定义、测试证据的稳定性，以及支持平台/依赖组合的发布矩阵。

### 11.1 P0：非目标中的“不改变行为”尚不可验证

策略规定治理不改变用户可见行为、默认值、协议和持久化格式，但当前 Verify 主要检查 import、依赖图、API snapshot、identity fixture 和测试是否通过。它没有定义需要冻结的行为表面，因此“测试通过”不能自动证明行为未变。

每个 cutover 必须生成 `behavior_contract`，至少按适用范围登记：

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

其中哪些是稳定契约、哪些只是内部实现必须显式分类。只有与本节点相关的稳定行为需要等价，不应把所有实现细节都冻结。

验证方式分三层：

1. characterization fixture：冻结迁移前行为；
2. differential test：在同一输入集上比较迁移前后的结果、事件和副作用；
3. invariant test：验证 owner、顺序、幂等、取消和持久化不变量。

若节点有意改变任何稳定行为，它就不再是纯包治理 cutover，必须拆成独立功能/兼容 migration 并单独批准。

### 11.2 P0：测试通过缺少 flaky、重试与确定性政策

Evidence 记录了命令和结果，但没有规定测试重试如何解释。若失败后重跑通过即可记为 PASS，会把偶发竞争、PTY loop 泄漏、时间依赖或资源未关闭隐藏在治理证据中。

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

建议规则：

- architecture、manifest、codec、golden 和 projected-graph 测试必须 deterministic，失败后重跑通过仍视为 Gate 失败，直到原因解释并生成新 evidence；
- PTY、async、platform 和 subprocess smoke 可按批准策略重试，但所有尝试都进入 evidence；
- 不能用 pytest rerun 插件把失败尝试从正式证据中隐藏；
- 使用真实时间、随机数、网络或用户目录的测试必须显式标记并注入可控边界；
- known flaky test 不能作为唯一 Gate 证据；
- quarantine 必须有 owner、问题 ID 和到期节点，且切片不能扩大 quarantine。

### 11.3 P0：旧实现/新实现的 differential harness 尚未定义

Atomic cutover 不允许长期双实现，但 differential test 又需要比较迁移前后。应明确比较机制，避免为了测试在生产包里保留旧实现。

可接受方式：

- 在 prepare commit 上生成版本化 golden fixture；
- 在两个独立 Git worktree/构建产物中运行同一测试向量；
- 使用旧 wheel 与新 wheel 的隔离子进程比较 public 行为；
- 对持久格式使用迁移前 fixture，由新 reader 重放；
- 对纯函数使用测试目录中的冻结参考向量，而非生产 forwarding module。

禁止为了 differential test 把旧生产定义复制到新代码或保留运行时 feature flag。

### 11.4 P0：支持版本与平台矩阵没有进入 Gate

项目声明支持 Python 3.11～3.14，并包含 Linux 专有 sandbox、POSIX/Windows 文件锁、macOS/Windows GUI 路径和平台二进制资源。单一开发环境通过不能证明包移动保持发布能力。

每个 cutover 必须根据 touched closure 计算适用矩阵：

```text
python_versions
operating_systems
architectures
dependency_extras
optional_dependency_absence
filesystem_capabilities
locale_and_encoding
```

最低要求：

- 纯静态 Gate 在所有支持 Python 版本至少执行语法/AST 验证；
- 触及运行时代码时至少覆盖最低和最高支持 Python 版本；
- 触及 fileops、locking、path identity 时覆盖 POSIX 与 Windows；
- 触及平台资产时验证 wheel 中正确平台文件和失败语义；
- 触及 optional capability 时同时验证“extra 已安装”和“extra 缺失”；
- 无法运行的平台必须有独立受信 CI evidence，不能由 reviewer 口头豁免。

如果项目实际上没有相应 CI 能力，应先缩窄公开支持矩阵，而不是让治理声明无法兑现。

### 11.5 P1：依赖锁定与供应链变化会污染治理结论

Clean-install smoke 若每次从不受控索引解析最新依赖，失败可能来自供应链漂移，而不是包治理；反之，新依赖版本也可能掩盖迁移问题。Evidence 应绑定：

```text
resolver_input_digest
resolved_dependency_manifest
artifact_hashes
package_index_identity
build_backend_version
build_isolation_mode
```

包治理 cutover 原则上不得顺带升级依赖。若必须更新依赖或 build backend，应拆成独立节点，并分别验证旧、新依赖集合。正式 wheel/sdist evidence 应保存 artifact digest 和安装后的文件清单。

### 11.6 P1：public deprecation 需要可观察、可测试的用户契约

策略允许有期限的 public deprecation shim，但尚未定义 shim 行为。Release manifest 应记录：

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

必须验证：

- 旧 import 在兼容窗口内仍可用；
- 每个用户调用点的 warning 行为稳定，不在内部循环刷屏；
- warning 指向用户代码而不是 shim 内部；
- 新 import 不触发旧路径 eager import；
- removal release 中旧路径确实消失；
- `DeprecationWarning` 是否默认隐藏是明确产品决定，不能假设用户一定能看到。

Public shim 与持久兼容 reader 必须继续分开：前者有 removal version，后者可能因历史数据保留策略长期存在。

### 11.7 P1：Error 与 Event 兼容不只等于 identity 不变

保持 error code 或 event tag 只是兼容的一部分。迁移还可能改变：

- payload 字段和默认值；
- emitted ordering；
- terminal/non-terminal 分类；
- unknown event handling；
- retryability/recovery action；
- exception wrapping 和 cause chain；
- subscriber delivery plane；
- journal codec 的规范字节。

Identity manifest 应扩展为 compatibility contract：

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
```

涉及事件或错误的 cutover 必须验证 schema、语义和序列，而不只是字符串 ID。

### 11.8 P1：Protocol 迁移缺少方差、运行时检查和 mock 兼容审计

Protocol 的签名即边界。移动或收窄 Protocol 时，不仅要记录 consumer 和 implementer，还要检查：

- 参数位置、关键字名称和默认值；
- sync/async 语义；
- covariant/contravariant 类型关系；
- property 与 method 的区别；
- `runtime_checkable`/`isinstance` 使用；
- test fake、mock 和 plugin implementer；
- exception、cancellation 和 context-manager 语义。

`source_symbol_signature_digest` 必须覆盖完整规范化签名，而不是只哈希 symbol name 或源码文本。Protocol cutover 的 projected closure 还要包含结构化实现者，即使它们没有显式继承 Protocol。

### 11.9 P1：Target readiness 需要可验证定义

`target_owner_readiness` 目前只是 DAG 字段名，容易退化为人工布尔值。它应由以下证据计算：

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

若目标包尚不存在，它的创建、owner、边界 manifest 和最小测试必须位于同一节点或 verified predecessor 中。不能先创建空目录再把它视为 ready。

### 11.10 P2：性能和资源使用需要非回归预算

包移动可能通过 eager import、重复 registry、重复 codec、额外 projection 或资源加载造成性能回退，而功能测试仍通过。对于热点边界，test plan 应按适用范围记录：

```text
import_latency
peak_memory
task_thread_count
open_fd_count
event_queue_bound
startup_io
wheel_size
```

这些不是全仓统一绝对阈值。只对被触及的热点建立迁移前基线和允许回归比例；超出预算时必须解释或拆分性能变更，不能把包治理当作无审查的性能变化通道。

---

## 12. 第五轮后的执行判定

在前述批准条件之外，还需要：

1. 每个 cutover 都有明确且有限的 `behavior_contract`；
2. 测试证据定义 deterministic/flaky/retry/timeout 政策；
3. differential harness 不在生产代码保留旧实现；
4. Python、OS、architecture、extras 与 optional-absence 矩阵可执行；
5. build 和 clean-install evidence 绑定解析后的依赖与 artifact hash；
6. public deprecation shim 有可观察、可测试的 warning 契约；
7. event/error compatibility 覆盖 schema、顺序、terminality 和 recovery 语义；
8. Protocol signature digest 覆盖结构化实现者与运行时语义；
9. target readiness 由证据计算，不是人工布尔字段；
10. 热点 cutover 有适用的性能与资源非回归预算。

这批条件完成后，单个节点才具备“结构正确且行为等价”的批准基础。

---

## 13. 第六轮审核补充：治理范围、根 Facade、生成与 Vendored 代码

本轮以当前 `Draft v6` 为准复核。v6 已吸收前轮提出的 packaging/resource 扫描、wheel/sdist、clean-install、根 public facade import budget 等要求；这些改进有效，但仍偏重“切片执行后如何验证”，尚未完整回答“发布单元内哪些文件属于治理对象、它们属于哪类来源、由谁批准修改”。在这个缺口关闭前，Phase -1D 不能宣称全仓枚举完成，Phase 0 也不能宣称治理事实覆盖完整。

### 13.1 P0：策略声明的范围小于真实可安装、可发布表面

策略首页只把以下目录列为范围：

```text
contracts/
kernel/
runtime/
orchestration/
product/
ztest/
```

但当前安装布局由包目录内的 `setup.py` 把本目录映射为顶层 `mote` 包；因此下列根文件不是仓库杂项，而是发布单元的一部分：

```text
__init__.py
agent.py
engine.py
messages.py
model.py
output.py
tools.py
pyproject.toml
setup.py
MANIFEST.in
config.example.yaml
README.md / README.zh-CN.md / CHANGELOG.md
LICENSE / NOTICE / zthird_party_licenses/
```

其中根 Python 模块直接构成 `mote`、`mote.output`、`mote.tools` 等公开 import surface；构建文件决定 package discovery、依赖、extras、entry point 与 package data；README、配置模板及许可文件会进入发布行为或分发制品。它们如果不在文件 inventory 与 claim/write-set 中，可能在没有 owner、没有 conflict serialization、没有 public/release review 的情况下改变发行契约。

建议把范围改为“整个可安装/可发布项目”，再按文件类别施加不同规则，而不是把治理范围等同于五个层目录。至少新增：

```text
source_roots
root_public_modules
build_metadata
package_data
published_docs
configuration_templates
license_and_notice_assets
development_only_files
excluded_with_reason
```

`excluded_with_reason` 必须是显式、可审计集合；扫描器不能因为文件不在五层目录内就静默忽略。

### 13.2 P0：根 Facade 规范与当前公开契约存在文字冲突

策略 5.2 写的是“根包不做全局业务符号聚合”，而当前根 `__init__.py` 明确声明自己是 small, stable framework facade，并精选导出 `Agent`、`Engine`、`Model`、`RunResult`、Toolset 等跨领域用户概念。`ztest/architecture/test_public_api.py` 也把这组 `__all__` 固定为稳定公开表面。

若按字面执行 5.2，最终动作应删除这些聚合导出；若按现有 public API 与 v6 的 root import budget 执行，则精选根 facade 是被认可的。两者不能同时作为规范。建议改成：

> 根包可以提供小型、显式批准并由 public API manifest 约束的用户级 facade；不得聚合内部实现、领域内部符号或自动发现结果。每个导出必须有稳定性等级、API owner、依赖预算和发布策略。

根 facade 应作为独立治理对象，而不是归属某个被导出符号的原始层。其验证至少同时覆盖：

```text
export_manifest
canonical_import_path
source_symbol_identity
core_or_optional_dependency_class
import_side_effect_budget
deprecation_and_removal_policy
docs_and_type_surface
```

### 13.3 P1：全局 DAG 缺少根 Public API / Composition owner

`engine.py` 是真实的公共生命周期与 composition facade，同时 import Product composition 和多个 Runtime 机制。它不是五层目录中的普通 source module，不能只靠 source/target Track 自动得出 owner。类似地，`agent.py`、`model.py`、`messages.py`、`output.py`、`tools.py` 是面向用户的适配或 facade。

建议为这些根模块明确一个 Product 下的 `Public API / Composition` owner（或等价的 release-owned capability），并在 Cutover DAG 中作为显式参与方：

```text
source_domain_owner
target_domain_owner
public_api_composition_owner
packaging_owner
release_reviewer
```

任何触及根 facade、其 transitively eager-import 的模块、entry point 或公开类型签名的节点，必须 claim 对应 facade/build manifest，并与其他同类节点串行化。否则两个互不冲突的领域移动仍可能同时修改 `__init__.py`、`engine.py` 或构建元数据，绕开现有 file claim 的架构意图。

### 13.4 P1：必须区分 authored、generated、vendored 与 generated artifact

当前代码库至少包含：

- `product/inference/daemon/rpc/gateway_v1_pb2.py` 与 `gateway_v1_pb2_grpc.py`：明确标注 `DO NOT EDIT` 的生成源码；
- `product/routing/squilla/ml/`：多个文件明确标注 vendored from opensquilla；
- `router.runtime.yaml`：随包发布的 vendored/config resource；
- wheel、sdist、API snapshot、OpenAPI/AsyncAPI 或治理 facts：由权威输入派生的生成制品。

当前策略要求生成 facts 原子写入，也会扫描 package data，但没有给 production source 建立来源类型模型。这会导致治理工具把生成文件当作普通手写文件做 import/type/touched-closure 修复，或者直接移动输出而没有同步其 schema、generator 与再生成命令。

文件 inventory 至少增加：

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

生成源码的 cutover 必须以权威 schema/generator 为修改入口，提交可复现的 regeneration evidence，并证明 clean regeneration 后工作树为零差异。若权威 `.proto` 或 generator 不在仓内，必须登记外部 authority、版本和取得方式；不能把不可再生输出当作治理完成。

### 13.5 P1：Vendored 代码需要来源、许可证与本地补丁治理

`NOTICE` 已识别 OpenSquilla 等第三方归属，源码也标注 vendored，但这还不足以支持安全 cutover。每个 vendored subtree 应记录：

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

普通包治理节点不应顺手重写 vendored 算法。路径移动若不改变内容，可由 Product owner 与 license reviewer 联合批准；内容修改应分类为本地 patch 或独立 upstream-sync 节点，并验证 NOTICE/license、行为 fixture 与补丁可重放性。否则“包移动不改行为”无法区分真实上游代码、Mote 适配层和已经漂移的本地 fork。

### 13.6 P1：安全敏感触达不能只由目录或词法命中判断

当前 shell/process 边界并不只存在于 terminal toolset。例如：

```text
runtime/process.py
runtime/hook/command_handler.py
runtime/sandbox/...
runtime/fileops/...
runtime/interactive/...
```

其中存在 `create_subprocess_shell`、`create_subprocess_exec` 和 `subprocess.run` 等不同执行语义。大量合法的 `getattr` 则用于 protocol/optional integration/serialization/展示；简单禁止所有反射会产生高噪声，也无法识别真正绕过能力边界的调用。

因此 security touch classification 应基于“调用语义 + 数据流 + 信任边界”，而不是仅看包名、函数名字符串或 AST 节点类别。建议至少分类：

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

每类定义批准 API、输入信任级别、必须经过的 classifier/validator、允许例外和 security reviewer 触发条件。迁移若改变 command 从 argv 到 shell、改变 quoting、绕开 classifier，或把用户输入接入动态属性/代码执行，即使依赖图不变也必须 fail closed。

### 13.7 P2：不同来源类型不能使用完全相同的质量门禁

统一的 import/type/format/touched-closure 门禁不适用于所有文件：

- authored source：执行完整架构、类型、格式、行为门禁；
- generated source：检查权威输入、generator、determinism、再生成零差异，格式问题应在 generator/template 修复；
- vendored source：检查 provenance/license/local patch，类型或格式债务按精确基线管理，避免无意义的大规模改写；
- generated artifact：验证内容 digest、构建来源和不可手工修改，不参与普通源码 owner 推断。

但来源类型不能成为逃避分层和安全约束的豁免：任何会在 `mote` 进程中执行或进入 wheel 的代码仍必须进入依赖图、安全审计和发布闭环。差异只在“如何修”和“由谁批准”，不在是否受治理。

---

## 14. 第六轮后的执行判定

Draft v6 在前五轮问题上已有明显补强，但要使 Phase -1 到 Execution Waves 真正按顺序执行，还需把以下条件加入总批准清单：

1. 治理范围覆盖整个可安装/可发布项目，不再只列五层目录；
2. 根 public modules、build metadata、package data、published docs、配置模板和许可证资产全部进入 inventory；
3. “根包不聚合”修订为与当前精选稳定 facade 一致的可执行规范；
4. 根 Public API / Composition、Packaging 与 Release owner 已进入 DAG 审批和 claim 模型；
5. inventory 区分 authored、generated source、vendored source 与 generated artifact；
6. generated source 绑定权威输入、generator identity/version、命令和可复现 regeneration evidence；
7. vendored subtree 绑定 upstream revision、license/NOTICE、本地 patch、更新和安全 owner；
8. security touch detection 按执行语义和信任边界分类，不能只按路径或词法扫描；
9. 不同来源类型使用各自可执行的门禁，同时都进入依赖、安全与发行治理；
10. Phase -1D 的“所有生产文件成功解析”扩展为“所有发布相关文件已枚举、分类或显式排除并说明理由”。

在这些条件关闭前，结论仍是：可以继续建设 Phase -1 的 schema、scanner、inventory 与试点；不能批准全仓生产 cutover，也不能把 packaging smoke 通过等同于治理范围完整。

---

## 15. 第七轮审核补充：阶段契约、控制面引导与可顺序执行性

本轮以当前 `Draft v7` 为准。v7 已实质吸收第六轮关于全项目范围、根 public facade、generated/vendored source、security touch 与 source-kind gate 的建议。继续沿“能否从 Phase -1A 顺序执行到全部 Waves”向下审核后，剩余核心风险已从目标架构转移到治理控制面本身：策略列出了阶段和最终条件，但阶段之间还没有足够严格的机器输入/输出协议，也没有完整定义从现有不可信控制面迁移到新控制面的过程。

### 15.1 P0：Phase -1A 不能只靠声明撤销旧授权

当前仓内仍存在可执行且可能被误用的旧治理链：

- `zdocs/architecture/contracts-phase-gates.toml` 仍把 `0A` 标为 `verified`；
- `ztest/architecture/contracts_governance.py` 仍硬编码多项 coverage 为 `100`；
- `contracts-identities.toml`、events/errors 等旧产物仍可由旧 CLI 生成空集合；
- `test_contracts_governance.py` 仍明确断言 coverage 全为 `100`；
- 旧 CLI 只提供 `snapshot/check/diff/tests`，并不是 v7 规定的统一 governance CLI。

因此，“将旧 Phase 0A 标为 untrusted_legacy”不足以满足“任何执行路径都无法使用旧证据批准迁移”。Phase -1A 必须定义可验证的 authority revocation：

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

退出测试必须证明：旧 CLI、旧 phase gate、旧 CI job 和旧 manifest 无法生成 `approved/cutting_over/completed` 转换；旧文件若保留，只能被新 reader 识别为 `untrusted_legacy` 审计输入。仅修改 Markdown 标题、另写一份否定声明，或依赖操作者记忆，都不构成撤权。

### 15.2 P0：Phase -1B 与 -1C 存在 bootstrap 循环

Phase -1B 要冻结 schema，Phase -1C 才建立验证 schema 的 Hermetic CLI；但 -1B 的退出条件又要求“所有 schema 自校验”。如果自校验工具属于 -1C，-1B 无法独立退出；如果 -1B 继续用旧 CLI 验证，新控制面会建立在已撤权的工具上。

需要显式区分 bootstrap validator 与正式 CLI：

```text
Phase -1B0: freeze canonical bootstrap envelope and validator contract
Phase -1B1: implement minimal hermetic schema validator
Phase -1B2: validate/migrate all control manifests
Phase -1C: build full command surface on the same library
```

最小 validator 可以只依赖标准库，且只能验证 schema、canonical encoding、引用完整性与版本兼容；它不能批准 production cutover。正式 CLI 必须复用同一解析/验证库，并用 conformance test 证明没有第二套语义。这样 -1B 的退出证据才不是由尚不存在的 -1C 命令提供。

### 15.3 P0：各 Phase 缺少机器化 artifact contract

当前阶段主要由自然语言动作和退出条件连接。要保证“顺序执行所有 Phase”，每阶段必须声明：

```text
phase_id
authority_version
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

例如 -1D 的 graph 不能只说“共享语义唯一”，而要绑定 -1B schema digest、-1C scanner command digest、source snapshot、输出 graph digest 和 soundness declaration；-1E 对账必须消费该同一 facts generation；-1F DAG 必须绑定对账结果；-1G 试点不得在任一 predecessor artifact 发生漂移后继续执行。

建议新增 phase manifest，由状态机计算 `eligible`，而不是由操作者根据章节顺序手工判断。`completed` 阶段的输出若发生相关漂移，其下游应变为 `stale/blocked`，不能继续显示完成。

### 15.4 P0：“六个 Gate”没有规范定义

v7 在试点中要求依次完成“六个 Gate”，但全文没有给出封闭的六项列表。文中能明确找到 Classification、Identity、Ownership、Closure，以及 projected graph、测试/发行等门禁，但它们没有统一 gate ID、输入、成功条件和适用性规则。

这会使不同执行器对“六个”产生不同解释，也无法稳定填写 evidence 的 `gate_id`。应建立唯一 gate registry，例如：

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
```

策略应明确列出试点所称六个 gate 的稳定 ID。任何额外的 security、packaging、public API、persistence 或 platform gate 应说明是六个顶级 gate 的子门禁，还是独立 gate；不能同时使用“六个”与开放式清单。

### 15.5 P1：Schema 演进缺少 expand/contract 与混合版本规则

策略要求 schema 版本化、未知版本 fail closed，并提到 upgrade digest，但没有定义治理控制面的 schema migration 操作。现实中 facts、decisions、migration、evidence、track 和 release manifest 不会原子升级，CI runner 也可能短时间运行不同 CLI 版本。

至少要冻结：

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

推荐 expand/contract：先部署能读旧/新、只写旧的 reader；再迁移并双格式校验；然后切新 writer；最后停止旧读。任何有损 migration 都必须 forward-only 且保留原始不可变审计副本。不能让旧 runner 在新 manifest 上静默丢字段，也不能让新 generator 覆盖人工 decisions。

### 15.6 P1：命令契约缺少退出码、stdout 与副作用规范

Phase -1C 只列出命令名称。自动化要可靠组合这些命令，还需稳定定义：

```text
exit_code_0 = pass
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
```

`discover` 等写命令与 `*-check` 读命令必须分离；check 命令不得修改 facts、mtime 或 lock state。人类文本与 machine output 应分通道，机器 JSON 使用版本化 envelope。命令 definition digest 必须覆盖参数、环境白名单、工具版本和工作目录语义，否则 evidence 中相同 `command_id` 不能保证执行的是同一检查。

### 15.7 P1：治理执行缺少独占租约与陈旧执行者隔离

Claim/conflict graph 解决的是计划层冲突，CAS 解决状态更新冲突，但仍不足以阻止两个 runner 在同一 workspace 同时生成 facts、执行 cutover 或写 evidence。需要治理操作级 lease/fencing：

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

每次写入都校验 fencing token；租约过期的旧 runner 即使恢复，也不能覆盖新执行者的产物。Facts 生成可以在隔离临时目录完成，校验通过后 CAS 发布；production cutover 必须在洁净、唯一 base commit 的隔离 worktree 执行。用户已有脏工作树不能被当作正式 cutover workspace，也不能因生成器重写文件而污染未登记修改。

### 15.8 P1：可信 Actor/Runner 仍缺少信任根生命周期

v7 要求 actor 绑定评审身份、提交签名或 CI attestation，但没有定义信任根来自哪里，也没有处理 key/runner 泄露、撤销和过期。最低还需要：

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

Approval 与 evidence 验证必须使用“验证时有效且未撤销”的 policy，并保存 policy digest。权限撤销后，尚未进入 cutover 的批准应失效；已经完成的历史证据保留其当时信任上下文。仅保存 `runner_identity` 字符串或 actor 名称无法满足职责分离。

### 15.9 P1：Phase -1E 的对账对象仍局限于五个旧层计划

v7 已把治理范围扩大到整个可安装/可发布项目，但 -1E 仍只说“对 Contracts、Kernel、Runtime、Orchestration 和 Product 的旧治理计划逐项分类”。这会遗漏：

- 根 public API/composition 与 packaging 计划；
- 跨层 persistence、events、security、dynamic loading 等专项文档；
- ADR 中已经批准或 superseded 的迁移动作；
- 发布/兼容计划、generated/vendored 更新计划；
- 旧文档中指向已删除 `common/`、旧 `product/cli` 等路径的动作。

Phase -1E 应先生成“治理需求来源 inventory”，再对每条 action/decision 分类，而不是只按五个文件名对账：

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

只有全部规范性或曾可授权的来源被枚举，才能证明不会重复执行旧动作或遗漏跨层动作。非规范性远景可以整体标记不进入执行 DAG，但必须由规则判定，而不是静默忽略。

### 15.10 P1：试点故障注入需要隔离与恢复验收

Phase -1G 要故意制造 drift、unknown 和测试失败，这是正确方向，但必须说明这些失败发生在隔离 fixture/repository/worktree 中。不能在主治理 facts 或真实 migration 上制造不可区分的失败状态。

故障注入矩阵至少覆盖：

```text
stale_digest
unknown_reference
conflicting_owner
missing_predecessor
claim_conflict
invalid_attestation
expired_lease
partial_manifest_write
cutover_commit_without_evidence
verification_failure
runner_crash_before_and_after_CAS
```

每项不仅要证明 fail closed，还要证明 reconciliation 后回到预期 terminal state，且真实 facts、人工 decisions 和 production source 无未登记漂移。否则试点只能证明会拒绝，不能证明可恢复、可继续顺序执行。

---

## 16. 第七轮后的顺序执行判定

Draft v7 的领域与迁移原则已经相当完整；当前尚未达到“可以从头顺序执行所有 Phase”的原因，主要是控制面 bootstrap 与阶段协议仍未闭合。建议把 Phase -1 的硬前置顺序收敛为：

```text
-1A  机器化撤销旧 authority，并用 negative test 证明不可授权
-1B0 冻结 bootstrap envelope、canonical encoding 与 trust policy
-1B1 实现只读、hermetic、无 production 授权能力的最小 validator
-1B2 迁移并验证 control manifests，建立 schema evolution 规则
-1C  在同一验证库上实现正式 CLI 与稳定命令契约
-1D  生成全项目 facts、统一图、soundness 与 gate registry
-1E  对账全部治理来源和 action，不限五层计划
-1F  由同代 facts 构造全局 DAG 与 phase artifact contract
-1G  在隔离 workspace 执行低风险试点和恢复性故障注入
Phase 0 扩展人工分类覆盖，再按 DAG 进入 Execution Waves
```

进入生产 cutover 前还必须新增以下总批准条件：

1. 旧 CLI、旧 phase gate、旧 CI job 和旧 manifest 已被机器化撤权；
2. bootstrap validator 不依赖 `mote`、不依赖旧治理工具，且没有 production 授权能力；
3. 每个 Phase 有版本化 input/output artifact contract 与失效传播规则；
4. “六个 Gate”具有唯一、封闭、版本化 registry；
5. 所有治理 schema 有 expand/contract、混合版本和有损迁移政策；
6. CLI 的退出码、机器输出、write set、幂等性和网络/时间环境契约已冻结；
7. 正式写操作使用隔离 workspace、lease 和 fencing token；
8. actor/runner attestation 绑定可轮换、可撤销、有过期语义的 trust policy；
9. Phase -1E 对账覆盖全部曾具有规范性或授权能力的治理来源；
10. 试点故障注入同时证明 fail closed、crash reconciliation 和环境清理。

在这十项关闭之前，可以继续编写 schema、bootstrap validator、CLI 和隔离试点；仍不能批准批量 production cutover。它们关闭后，Phase -1 才具备严格顺序执行与失败后继续推进的控制面基础。
