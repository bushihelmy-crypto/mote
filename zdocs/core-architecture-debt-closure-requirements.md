# Mote 核心架构债务闭合实施计划

计划状态：产品决定已闭合，待实施就绪复评  
当前阶段：实施就绪复评  
性质：架构级整改实施总账  
基线日期：2026-07-31  
适用范围：`contracts <- kernel <- runtime <- orchestration <- product` 的生产代码

> 使用方式：第 4 至第 7 节的每个 `R*` 编号都是一个不可拆散验收语义的工作包；第 10 节是唯一实施顺序和状态总账。实施者先在总账更新状态，再按工作包中的源码证据、实施任务和验证与签收条件完成垂直切片。不得只改总账状态而没有可复核证据。

## 1. 背景

Mote 已建立单向分层、Product composition、动态边界治理和 Python 包治理规则，但源码中仍存在若干未闭合的迁移与类型边界。这些问题集中在多入口、历史兼容、重复 canonical 类型、正式 Port 的无界 `Any`、反射式 duck typing、持久化不可逆和错误 owner 重导出。

本计划以 2026-07-31 对五层生产代码的扩大审计结果为实施基线。测试和既有文档不属于整改对象，但可作为行为证据与验收门禁。问题不按文件大小或 `Any` 数量判定；只有处于正式边界、形成双真相、破坏分层或导致生产行为断链的项目才纳入。

## 2. 目标

1. 每项核心能力只有一条 canonical 调用、注册、装配和身份生成路径。
2. 删除已结束迁移遗留的 alias、re-export、兼容签名探测和不可达生产实现。
3. 正式跨层 Port 使用稳定 DTO、窄 Protocol 或有意义的泛型表达，不以 `Any`、裸容器或私有反射维持正确性。
4. Durable 数据可验证、可往返，不能把未知对象的 `repr` 当作可恢复业务状态。
5. 媒体、搜索和模型推理能力均被真实 composition declaration 覆盖，并从唯一 Product root 可达。
6. 扩充治理门禁，使重复 canonical 类型、工具目录断链、隐式 Protocol 和错误 capability declaration 能被自动发现。
7. 所有模型可触发的本地落盘和命令执行在副作用发生前经过与实际目标一致的授权，generation identity 能证明实际 artifact 内容。

本计划所称的长期“零负债”不是承诺未来十年不产生任何技术债务，而是关闭本基线已确认的核心债务，并建立可持续验证的不变量，使同类安全绕过、双 canonical path、不可逆持久化和跨层动态泄漏不能无声回归。完成状态只对明确基线、验收证据和持续门禁负责。

## 3. 非目标

- 不机械消灭所有 `Any`；外部 JSON、第三方 SDK、插件发现和私有 telemetry binding 可在受控 adapter 边界保留动态性。
- 不因类名相同就合并不同 bounded context 的类型。
- 不新建 `common/`、generic utils 或平行执行框架。
- 不以长期兼容 facade、双写或第二 canonical API 降低迁移难度。
- 不在本计划中重写测试或文档体系；实现切片必须增加必要的架构门禁和回归验证。

### 3.1 工作包状态与判债规则

本文件同时是架构债务总账和实施状态总账，不代表所有条目已经具备直接实施条件。每个编号进入开发前必须标记为以下一种状态，并记录对应证据：

- `TODO`：尚未开始基线复核；
- `CONFIRMED`：源码事实、生产可达性、风险和目标不变量均已验证；
- `DECISION_REQUIRED`：风险存在，但产品承诺或生命周期语义尚未决定；
- `NEEDS_EVIDENCE`：目前是高价值线索，仍需证明真实生产消费者、可达路径或失败后果；
- `IN_PROGRESS`：基线已经确认且正在实施，同一工作包只能有一个明确 owner；
- `BLOCKED`：已确认但受外部决定或前置工作阻塞，必须记录 blocker 和解除条件；
- `DONE`：实现、直接消费者、架构门禁、静态检查与证据记录均已完成；
- `REJECTED`：复核后属于合理 bounded-context 差异、受控 adapter 动态性或其他明确保留项，并已记录保留依据。

不得仅凭类名相同、出现 `Any`、存在多个公开方法、零默认消费者或模块体积判定架构债务。必须至少证明其形成双真相、破坏分层/最小权限、使 durable 语义不可逆、造成生产行为断链，或让同一身份/副作用出现多个权威 owner。不同 bounded context 的同名类型应先验证生命周期和语义；wire/provider/plugin adapter 内受控动态值不自动构成整改项；便利入口若完全薄委托 canonical owner，也不构成第二执行路径。

包含“二选一”产品语义的条目在事实确认后、ADR 决策前统一视为 `DECISION_REQUIRED`，不得由实现者在迁移中临时选择。R1.13 与 R2.28 的产品语义已经确认，不再属于该状态：已接受且不可从 canonical durable state 确定性重建的 Agent delivery 必须 durable；Agent lineage 必须跨 supervisor 进程重启持久恢复。其他尚未确定 durable/process-local、fail-open/fail-closed 或跨重启承诺的条目仍为 `DECISION_REQUIRED`。

兼容策略必须区分三类：已结束迁移的 API facade/alias 在消费者归零后删除；durable data 的版本化 decoder/upcaster 按明确支持窗口保留；外部 wire compatibility 由协议版本和退役策略管理。禁止以“清理兼容残渣”为由删除仍在支持窗口内的 durable 恢复能力。

本计划的存量数据决定已经确认：现有 Session、Residency、Workflow、Cron、ErrorCode 持久数据允许从零开始，各 domain 使用 `AUTHORIZED_DISCARD`，新 schema 直接成为唯一生产格式，不实现旧 decoder、upcaster、双读或兼容 migration。该授权只覆盖精确识别的上述旧 durable records，不覆盖 Artifact、用户 workspace、secret 或仓外 wire/API；实际删除仍须解析明确目标并产生 typed receipt/audit。

### 3.2 单个工作包的执行记录

每个工作包从 `TODO` 推进到 `DONE` 时，必须在第 10 节总账或紧邻该工作包的实施记录中补齐以下内容：

- `Owner`：唯一实施负责人；
- `状态`：只能使用 3.1 节定义的状态；
- `基线证据`：实施时重新核实的定义、生产消费者、composition 和状态真相源；文中行号只作为审计线索；
- `复用决定`：搜索过的既有 contract、Port、store、codec、lease、runner、registry 或 composition，以及复用、扩展或拒绝复用的理由；
- `稳定面/变化轴`：本切片必须保持的 identity、owner、状态机、durability、权限、泛型和外部 schema，以及允许变化的实现；
- `改动清单`：contract、canonical owner、composition、旧入口删除、正常/失败/恢复/清理路径和治理 gate；
- `验证证据`：实际命令、通过/失败数量、fault-injection 场景、Pyright 结果、预存失败和未运行范围；
- `提交证据`：对应 commit 或变更集标识；未提交阶段可填工作区 diff 摘要。

状态转换必须遵守：

```text
TODO -> CONFIRMED -> IN_PROGRESS -> DONE
  |          |             |
  |          +-> DECISION_REQUIRED
  |          +-> NEEDS_EVIDENCE
  +---------------------------> REJECTED
IN_PROGRESS -> BLOCKED -> IN_PROGRESS
```

`DECISION_REQUIRED` 必须链接 ADR 或产品决定后才能进入 `CONFIRMED`；`NEEDS_EVIDENCE` 必须补完真实生产可达性和失败后果才能进入 `CONFIRMED`；`DONE` 不允许带未设期限的 waiver、兼容旁路、双 API 或待清理项。

### 3.3 垂直切片实施流程

每个 `R*` 工作包按以下顺序实施，顺序不可通过调用点补丁倒置：

1. 重新检查工作区状态和相关 diff，确认不会覆盖用户改动；
2. 用 `rg --files`、`rg` 重建当前定义、消费者、composition、lifecycle、persistence 和错误传播链；
3. 确认状态为 `CONFIRMED`，或完成所需 ADR/证据；
4. 搜索并选择 canonical 基础设施，先固定 Contracts 和 owner，再接通 Runtime/Orchestration/Product composition；
5. 同一切片内迁移全部仓内消费者并删除旧入口、alias、反射 seam、双写/双读与未使用实现；
6. 补齐正常、拒绝、异常、取消、恢复、并发和 cleanup 测试，以及能够阻止同类回归的架构 gate；
7. 运行工作包直接测试、直接消费者测试、`ztest/architecture` 和触及泛型/Protocol 时的 Pyright；
8. 检查最终 diff，将验证结果写回实施记录，满足第 11 节后标记 `DONE`。

### 3.4 评审意见处置与重编号映射

本版对 `core-architecture-debt-closure-requirements-review.md` 的阻断意见作如下处置：

| 评审意见 | 处置 | 理由 |
| --- | --- | --- |
| 安全前置被排到最终阶段 | 采纳 | config/extension trust、credential、MCP auth 与 typed runner 是生产安全基础，不是末期整洁度 |
| 未决语义进入 `DECISION_REQUIRED` | 采纳并完成决定 | ADR-D1–D3 已确认；R1.5 的 process-local resubmit 也已有明确产品决定，不再因旧 review 退回待决定；ADR-D1 删除无承诺的虚假 signature，不建设无消费者的 PKI |
| R2.15/R2.16 跨 owner | 采纳 | 分别拆为 spawn contract、turn admission、nickname、handle，以及 Residency、Mailbox、Cron schema |
| 所有其他大工作包立即拆分 | 分阶段采纳 | R2.11、R2.25 先完成 owner 复核；证据确认后分别抽出 R2.42 Product composition 与 R2.43 Session read model，没有按文件或主题机械拆分 |
| 每个 R 一行状态总账 | 采纳 | 里程碑状态改为派生值，避免第二状态真相 |
| 门禁分类 | 采纳并允许交叉 | 一个安全控制可同时需要静态、集成和 fault-injection 证据，但不能实现为巨型 gate |
| typed plugin loader 例外需机器定义 | 采纳 | 明确 catalog/manifest authority、typed factory、activation 和 AST allowlist |
| 连续编号 | 采纳 | 与工作包拆分一次性完成，避免实施中继续改变状态键 |

历史评审编号映射如下：旧 R0.2–R0.9 对应新 R0.1–R0.8；旧 R1.4a 对应新 R1.5，旧 R1.5–R1.21 对应新 R1.6–R1.22，R1.23 不变；旧 R2.15 拆为新 R2.15–R2.18，旧 R2.16 拆为新 R2.19–R2.21，旧 R2.17–R2.36 对应新 R2.22–R2.41；旧 R2.11 的 Product composition 子范围抽为 R2.42，旧 R2.20（重编号后 R2.25）的 Session read-model 子范围抽为 R2.43。后续全链复审发现既有条目没有合法 owner 可承接 typed observation、Prompt/cache/compaction generation 与跨 durable domain 的 clock semantics，分别新增 R2.44、R2.45、R2.46；Artifact pin registry 与 Hosted Service reconciler 则分别以 R1.24、R1.25 追加，均未改变历史编号映射。新编号是唯一状态键，历史编号只用于追溯旧 review。

## 4. P0：正确性与生产协议断链

### R0.0 修复 Product canonical composition 的配置名称断链

`product/composition/container.py` 从 `product.config.schema` 导入并使用全仓不存在的 `MoteConfig`，实际根配置类型名为 `Config`。该错误位于 `ProductContainer.standard` 的 canonical factory 上，目前会被更早发生的 optional UI eager-import 失败遮蔽。

实施任务分为两个明确层次：

1. **解析/纯构造层**：删除错误名称并统一使用唯一根配置类型；不得新增 `MoteConfig` 兼容 alias。增加不加载 TUI/PTY/optional provider 的 hermetic import/construct gate，使 canonical factory 可解析且 construct 不产生外部动作。
2. **激活层**：修复 import 不得顺带激活 checkout 中的 Agent、Skill、Hook 或 MCP。在 R1.9 的 canonical provenance/trust gate 完成前，standard composition 必须保持这些来源未激活或显式 fail closed；不得以当前 import failure 曾经遮蔽风险为理由恢复旧 discovery→activation 路径。完整 extension activation 由 R1.9 签收，不由本工作包复制临时 trust gate。

验证与签收：`ProductContainer.standard` 的模块可在最小依赖环境导入，类型名称在定义、factory、治理声明和调用方中一致；陌生 checkout fixture 中的 Agent/Skill/Hook/MCP 不注入模型、不启动进程且不建立网络连接。R0.0 只能签收解析与纯构造层；若迁移后 factory 仍无法在不激活 extension 的情况下构造，本包不得标记 `DONE`，必须继续收窄 construct/activate seam。

证据闭包结论：当前 `ProductContainer.standard` 在构造返回值时立即求值 `agent_composition.factory` 与 `agent_composition.agents`；前者执行 config source、Hook、MCP discovery，后者经 builtin catalog 扫描 Markdown Agent。因此现状并不满足 pure construction，但调用链、真实消费者、失败后果和必要 seam 已明确：必须先把 approved extension declaration/snapshot 与 discovery/materialization/activation 拆开，再将错误的 `MoteConfig` 引用迁为 canonical `Config`。这已足够固定实施范围，工作包可进入 `CONFIRMED`；实施验收仍必须用陌生 checkout negative fixture 证明修复后 construct 无扫描、模型注入、进程或网络动作。

### R0.1 `ToolResult` durable payload 必须可逆

`runtime/tools/tool_result.py::ToolResult.data` 当前以 `Any` 同时承载工具值、Workflow 值、后台控制对象和 provider 元数据。`tool_result_receipt.py` 对未知对象只持久化 `{type, repr}`，回放无法恢复原语义。

实施任务：

- 拆分 durable payload、ephemeral execution value、deferred control result 与 provider-private metadata；
- durable payload 使用封闭 tagged union、`JsonValue` 或版本化 codec；
- 未注册类型在写入边界失败，不得静默降级为 `repr`；
- compaction、rollout、resume 后保持语义等价。
- 普通Tool、BackgroundTask、Workflow、Hook与媒体大输出复用canonical artifact/content repository和typed reference，不得各自截断、写任意路径或发明result pointer。preview明确为非完整结果；reference绑定owner、content digest、retention和permission。terminal notification丢失可从canonical fact/reference重新投影。
- secret、credential、helper stdout和敏感payload不得进入preview、artifact、exception、trace或snapshot；动态wire编码只发生在外部adapter。

验证与签收：每个 durable variant 均有 encode/decode round-trip；未知对象 negative test 必须 fail closed。

### R0.2 收敛同概念 model contract 并区分不同 lifecycle capability

以下同名类型是待核实线索，存在两个定义本身不证明必须合并：

- `contracts/model/inference.py::CanonicalToolCall` 与 `contracts/model/invocation.py::CanonicalToolCall`；
- `contracts/model/failover.py::EndpointCapabilities` 与 `contracts/model/topology.py::EndpointCapabilities`。

实施任务：分别重建定义、构造、转换、持久化和消费者链，逐字段验证两组定义是否表达相同 lifecycle、identity 和不变量。若是相同概念，迁移到唯一 authoritative defining module；若 topology declaration 与 resolved snapshot 等确有不同生命周期，使用不同语义名称并提供单向显式投影。只有结论为同一概念时才删除旧定义、字段访问、字典式兼容和 re-export。

基线结论：两套 `CanonicalToolCall` 都表达 provider-neutral tool invocation；`contracts/model/inference.py` 版本通过 `command_name/args`、字典访问和宽 `Any` 保留旧适配语义，Runtime model-call 路径仍消费它，而 transport/failover 已消费 `contracts/model/invocation.py` 的严格 `name/arguments/JsonValue` 类型。应迁移前者消费者到后者并删除旧定义与兼容访问。两套 `EndpointCapabilities` 字段相近但生命周期不同：`topology.py` 是部署期声明，`failover.py` 是 resolved `EndpointDescriptor` snapshot；应使用不同语义名称并由 Product compiler 建立单向投影，不能机械合并成一个可变生命周期类型。

验证与签收：证据记录包含每个定义的 owner、生产可达性、生命周期、消费者和失败后果；相同概念只有一个定义，不同概念具有不同名称和显式投影，package facade 不遮蔽 authoritative 类型。

### R0.3 闭合媒体下载的文件权限边界

`product/toolsets/builtin/generate_media/generate_media_tool.py:68-75` 将 `GenerateMedia` 声明为 `EXTERNAL` 且只要求 `invoke_service`，但模型可传入 `output_dir`；`217-220` 在远端生成后调用 `_materialize`，`270-283` 随即创建目录并直接写文件。该工具没有覆盖 `mutates_filesystem_for` 或 `permission_targets`，因此 `runtime/tools/tool_pipeline.py:113-139` 的 pre-call authorization 看不到这次文件写入。在非 full sandbox 下，`BaseTool` 明示的“无 concrete target 拒绝写入”规则也不会被触发，因为工具默认报告 `mutates_fs=False`。

同一调用中的多个 item 可以声明相同 `filename`，缺省文件名也按 media kind 固定；并发 `_materialize` 因而可能把多个资产映射到同一 canonical target。仅枚举 permission target 不能决定冲突、transaction ownership、部分失败或重试语义。

产品决定（ADR-D4）：重复 canonical target 不拒绝整个生成请求，也不 overwrite 或 last-writer-wins；Product-owned target planner 在任何远端请求前按稳定 item identity/请求顺序生成确定性无冲突名称，并把 `requested_target -> resolved_target` 映射纳入请求/重试 identity。名称冲突属于可恢复的 target resolution disposition：最终结果必须把改名事实和实际路径通知 Agent，不能只写日志或静默改变路径。Runtime/FileOps 只负责 canonicalize、授权、原子 reservation 与写入，不拥有媒体命名策略。

远端生成后的本地 materialization/publication 采用**逐资产 typed partial settlement**：每个资产自身的 reservation、before-image、commit/rollback 原子，批次返回 `committed/failed/in_doubt` 封闭结果；成功资产不因另一资产本地失败被删除或伪装成已回滚。调用方必须获得每个 item 的稳定 identity、requested/resolved target、是否改名和 settlement。当前不承诺多路径 all-or-nothing；未来只有在真实交付 durable batch journal、commit marker、crash recovery 与 rollback-in-doubt 后，才可通过新的版本化产品决定提供显式 opt-in capability，R0.3 不预建该扩展点。

该决定没有机械接受 review 的“重复 target 整批拒绝”：拒绝虽然更容易 fail closed，但会让可以安全消解的展示名称冲突阻断已经合法的生成需求。自动改名也不能退化为写入时临时试探 `-2/-3`：同一 call 重试必须复用首次 target plan/reservation；并发调用必须通过 canonical reservation 避免 TOCTOU，不能因调度顺序覆盖彼此。若首次 planning 面对调用外已有文件占用，是否允许覆盖仍服从既有 mutation/permission contract；本 ADR 只授权消解同一批次或并发 reservation 产生的名称冲突，不把 overwrite 权限偷偷改成自动改名策略。

实施任务：

- 以 R0.1 固定的 canonical durable payload/codec 作为媒体 publication settlement 的唯一跨 replay 边界；R0.3 在 Product/FileOps owner 内定义媒体逐项 settlement DTO，只把已注册的 canonical DTO/reference 投影进 `ToolResult`，不得返回裸 dict、继续借用 `Any` 或建立媒体私有 receipt codec；
- `output_dir` 存在时必须在远端请求和本地下载之前声明文件系统 mutation，并枚举每个最终文件的 canonical target；
- 授权路径与实际落盘路径必须使用同一解析函数、cwd、basename 和 symlink 语义；
- 本地写入必须进入 FileOps transaction/`FileMutatingTool` 等 canonical 写入 seam，保留 before-image、scope 和 settlement 语义；
- 部分媒体远端成功不能把本地权限拒绝降级成普通 `materialization_error` 后继续返回成功，权限拒绝必须保持 fail closed。
- 在任何远端请求前由 Product target planner 生成完整 target plan：同批重复名称按稳定 item identity/请求顺序添加确定性后缀，保留扩展名，并记录 requested/resolved target 与 rename disposition；随后 canonicalize、授权并原子 reservation 全部 resolved targets；
- 同一 call 重试必须读取或重建完全相同的 target plan，不得因首次已写文件再次递增后缀；并发 reservation 冲突必须重新进入 typed target resolution/settlement，禁止 TOCTOU overwrite；
- 每项 publication 独立提交 `committed/failed/in_doubt`，before-image、commit/rollback 与重试 identity 服从同一 item identity；Agent 可观察每项实际路径与自动改名通知；
- 对全仓直接写盘consumer分类为owner-internal durable protocol、Product-managed config/secret、ephemeral cache/artifact、Agent-requested workspace mutation；只有最后一类必须统一进入FileOps，前三类留在真实owner并分别满足atomicity、permission和ownership，禁止以`write_text/write_bytes`扫描一刀切或强行反向依赖高层FileOps。

验证与签收：read-only 拒绝任意 `output_dir`，workspace-write 拒绝 workspace 外和 symlink escape；多资产调用逐个检查所有 resolved targets；覆盖重复 basename、默认名冲突、扩展名保留、稳定顺序、Agent 改名通知、并发 reservation、部分失败、相同 call 重试复用原映射及远端成功/本地失败；断言成功项保留、失败项不冒充成功、崩溃窗口稳定进入 `in_doubt`，且无 `output_dir` 的纯远端调用不被误报为本地写入。静态 gate 扫描模型可调用工具中的直接文件写入，并证明每条路径都有 mutation declaration、concrete targets 和 typed settlement。

### R0.4 让 generation content identity 可验证

`product/models/runtime_generation.py:194-207` 以 `generation_id + topology revision` 生成 `artifact_digest`，随后把该声明值写进 `GenerationArtifact`；它不是 artifact canonical bytes 的内容摘要。`product/inference/daemon/generation.py:33-46` 只比较 envelope 和 artifact 内两个声明值，`product/inference/backends/sqlite.py:669-693` 也只按声明 digest 与完整 JSON 做幂等冲突比较。Admin stage 入口 `product/interfaces/inference_admin_api/application.py:116-146` 同样只检查请求字段相等。`GenerationArtifact.signer_key_id/signature` 在 stage、restore、activate 链中没有验签消费者，当前 `signature="process-local-capability"` 只是占位字符串。

这意味着 digest 可用于代际引用，却不能证明收到或恢复的 topology/bindings/policy artifact 未被改写；字段名称和签名字段表达了当前实现没有提供的完整性保证。

产品决定（ADR-D1）：GenerationArtifact 只承诺 canonical content identity，不承诺发布者来源认证。删除 `signer_key_id/signature` 及 `"process-local-capability"` 占位值，不得为了满足旧字段名称引入无消费者的 PKI。未来若出现真实跨信任域发布者认证需求，必须作为新的版本化 contract 定义 trust root、key lifecycle、rotation、revocation 和 restore 行为，不能复活旧字段。

实施任务：

- 定义唯一 canonical serialization，content digest 字段自身不进入摘要输入；
- stage、durable restore 和 activate 在接受 artifact 前重算 SHA-256，并彻底删除 signer/signature 字段、占位值和伪 verifier；
- 若 generation identity 本意仅是 topology revision token，必须改名并另设真实 content digest，不能继续用 `artifact_digest` 暗示内容寻址；
- Admin、embedded、shared daemon 与 SQLite 使用同一 content verifier，不得各自只比较声明值。

验证与签收：保持 `generation_id` 和声明 digest 不变、只修改任一 bindings/policy/revision 字段时，stage 与 restore 均 fail closed；非 canonical 编码和 digest substitution 有 negative fixtures；同一语义 artifact 的 canonical digest 稳定；生产 contract、codec、store、wire 和 composition 中 signer/signature 字段与占位值归零。

### R0.5 给 Shared RPC execution 建立对象级授权绑定

Shared gRPC server 会在每次调用校验 UDS transport 和签名 session credential，但 backend 只在 start 时校验 canonical principal：`product/inference/daemon/execution_backend.py:66-115,275-291`。注册表 `62-63,244-268` 只保存 `execution_id -> execution`，没有保存 owner principal、credential session、generation 或 artifact digest。后续 `authorize`、`cancel`、`stream_events/reconcile`、双向 `session` 和 `query_receipt` 在 `117-203` 只按客户端提供的 `execution_id`/generation 查对象或 durable store，传入的 `credential` 未参与对象级授权。对应 server 路由 `product/inference/daemon/grpc_server.py:116-167` 只证明调用者是某个合法本地 principal，不能证明其拥有目标 execution。

结果是另一合法 Shared principal 一旦知道或猜到 execution id，可能取消/授权他人的执行、发送 session message、读取事件或 receipt。generation id 比对不能替代 principal ownership；持有合法 daemon credential 也不能授予全局对象访问权。

实施任务：

- 先消费 R2.54 的 typed execution owner record 与统一 verifier；不得在宽动态 registry 上另造临时 owner mapping；
- 注册 execution 时原子保存不可变 owner binding：principal、credential/application scope、generation id 与 artifact digest；
- 每个 object RPC 在查找对象、journal 或 durable receipt 前验证 binding，事件 cold-read/reconcile 也必须从 durable ownership metadata 校验；
- WirePermit 的 principal/generation/execution binding 与 registry owner 使用同一验证函数；
- 运维级跨 principal 访问必须使用独立 scope/Port 和审计，不能复用普通 execution credential。

验证与签收：两个都通过握手的 principal 之间不能 authorize、cancel、send、stream、reconcile 或 query 对方 execution；daemon 重启后对 durable receipt/event 的隔离仍成立；不存在仅凭 `execution_id` 的对象访问路径。

### R0.6 治理项目 Hooks 的命令执行与失败语义

Product composition 会从 cwd 到 git root 加载每一级 `.mote/hooks.json`：`product/composition/container.py:74-103` 与 `product/config/adapters/hooks.py:1-62`。配置中的 `HookCommandHandler.command` 是任意 shell 字符串（`runtime/config/hook.py:19-39`），`runtime/hook/command_handler.py:31-96` 直接通过 `asyncio.create_subprocess_shell` 在项目 cwd 执行，继承进程环境，并把 session/project 信息注入子进程。该路径不经过 command classifier、PermissionEngine、审批、SandboxRuntime 或 Tool effect/ledger。

同时 HookManager 明确把 command spawn、timeout 和异常折叠为 `EMPTY`：`runtime/hook/manager.py:179-215`。对 `PreToolUse` 等本应能阻断危险动作的 policy hook，这会在安全判断不可用时 fail open；项目 checkout 只需包含 hooks 文件即可在 Role 生命周期事件中启动命令。

实施任务：

- 项目/工作目录 hook 配置视为不可信可执行内容，首次启用必须有 canonical trust/approval，不能因位于 git root 自动获得执行权；
- hook command 使用统一受治理 command runner，进入 classifier、sandbox、cwd/path、env allowlist、output limit、audit 和 cancellation；不得保留独立 `create_subprocess_shell` 入口；
- policy hook 与 observation hook 明确分型：PreToolUse/permission/commit guard 等安全 hook 的超时、spawn、decode 失败必须 fail closed 或交由显式策略决定，不能统一变成 EMPTY；纯 observation hook 才可 best-effort；
- secret、credential、完整 parent environment 和敏感 transcript 不得默认暴露给项目 hook。
- control Hook必须在canonical arguments/permission targets固定后、effect intent或任何外部动作前执行，只能单调收窄permission、sandbox、budget、tool与network；argument修改后重新classifier/permission/approval，不得沿用旧批准。timeout、crash、malformed output、unknown decision对control Hook fail closed；多个Hook按稳定identity和确定顺序fold，冲突取最严格decision。Post/observation Hook可best effort但不能修改authoritative result。

验证与签收：打开含hooks的陌生checkout不启动进程；未经批准command拒绝；control Hook在effect前运行并单调收窄，argument修改触发新permission；timeout/crash/malformed/unknown阻断动作；最严格fold确定；symlink/config layering不能绕trust；日志无secret/stdout，execution以同一EffectId进入permission audit。

### R0.7 修复 File contract 的未定义类型与 digest 构造断链

`contracts/file/search.py:63-75` 在 `SearchResult.artifact/skipped_artifact` 上使用 `ContentIdentity`，但模块只导入了 `PathToken/PresentVersion`（同文件 `1-10`）。`from __future__ import annotations` 仅把普通 import 暂时推迟，Pyright 仍报告 undefined variable，`typing.get_type_hints(SearchResult)`、schema/introspection 或运行时类型解析会直接失败。这是正式 File 搜索结果 contract 的真实缺符号，不是风格问题。

同时 `contracts/file/codec.py:72-83` 严格校验 digest 字符串后，仍把普通 `str` 传给声明为 `ContentDigest` 的 `ContentIdentity.digest`；应在 codec 边界显式构造 `ContentDigest`，使静态类型与 nominal identity 一致，而不是依赖运行时 `__post_init__` 隐式归一化。

实施任务：从 authoritative defining module 顶层导入 `ContentIdentity/ContentDigest`，codec 返回值显式构造 nominal digest；不通过 facade re-export 或局部 import 修补。

验证与签收：Pyright 对两个模块无 undefined/incompatible argument；`get_type_hints(SearchResult)` 可在 hermetic import 中解析；blob canonical round-trip 保持 digest nominal type 与字节语义。

### R0.8 让本地 RunJournal durable commit 真正 fail closed

`RunJournal.record_started` 明确承诺 EXTERNAL step 必须在执行 body 前 durable（`runtime/ledger/run_journal.py:186-217`），工具流水线也依赖这一顺序：`LedgerStage` 在调用工具前写 started，恢复时用它判断外部结果是否 unknown（`runtime/tools/tool_pipeline.py:147-181`）；Jsonl/Temporal 两种 durable backend 则共同通过 `run_journaled_step` 执行“started → body → terminal”（`runtime/ledger/run_journal.py:250-284`、`runtime/durable/backend.py:90-122`、`runtime/durable/temporal/_activities.py:117-149`）。但底层 `AppendOnlyLedger.append` 先把记录写入 `_latest`，随后才落盘，并把 mkdir/append/fsync 的 `OSError` 只记 warning 后吞掉（`runtime/ledger/append_ledger.py:43-50,91-99`）。因此磁盘满、权限或 I/O 错误时，live process 会把 started 当作成功并继续执行外部工具；崩溃后磁盘没有该记录，恢复可再次执行同一个副作用。terminal 写失败也被伪装成 live-process 已完成。

同一 primitive 还把任意解析失败行静默跳过，无法区分允许忽略的尾部 torn write 与中间损坏（同文件 `127-143`）；`reap` 在 atomic rewrite 成功前先删除内存状态，rewrite 失败后内存与磁盘分裂（`100-125`）。本包只修复单一 ledger 实例的本地 durable commit、strict decode 与 rewrite 原子性；跨实例、跨进程及跨 backend 的 operation ownership 属于 R0.9，不能用本地文件 append 语义冒充 distributed fencing。

实施任务：

- `append` 返回明确 durable commit receipt 或抛 typed persistence error；只有 started 已 fsync 成功，EXTERNAL body 才能运行，terminal/reap 失败也不得伪装成本地已结算；
- 先 durable commit、再更新可观察内存 index，或使用一个原子事务使两者同成同败；reap 以新 snapshot 成功 replace 为提交点，失败保留旧内存/磁盘视图；
- JSONL decoder 只可按明确 framing 规则容忍最后一个未提交尾行；中间坏行、错误 schema/shape 和非单调 lifecycle 必须 quarantine/fail closed，并保留诊断位置；

验证与签收：注入 parent mkdir、append、flush/fsync、terminal append 与 atomic rewrite 的 ENOSPC/EACCES/EIO 时，EXTERNAL body 在无 durable started 的情况下调用次数为零，失败后内存与重启视图一致；尾部 torn line 可按协议恢复，中间损坏与 lifecycle 分叉 fail closed；崩溃点覆盖 started 前、started fsync 后、body 后和 terminal fsync 后，均不会把“未记录”误判为可安全重跑。本包不得宣称已经解决多进程 operation ownership 或 Workflow effect reconciliation。

### R0.9 建立跨 backend 的 durable operation ownership 机制

每个 executor 为同一 session 独立构造 `RunJournal`（`runtime/tools/tool_executor.py:159-168`），而 ledger 没有跨进程 owner lease、tail revision 或 fencing；Jsonl 与 Temporal 虽共同调用 `run_journaled_step`（`runtime/durable/backend.py:90-122`、`runtime/durable/temporal/_activities.py:117-149`），但 deployment scope、transaction guarantee 与 takeover 语义不同。`O_APPEND` 不能保护“读取 prior → claim → 外部副作用 → terminal”，FileOps 的本地 lease 也不能未经证明就复用于跨主机 Temporal。

复用审计结论：`contracts/ports/session/run_lease.py` 与 `contracts/ports/runtime/lease.py` 已表达 acquire/renew/release/assert/guard 和 monotonic fence，`runtime/control/leases.py` 已有 process-local 与 file-backed coordinator，`runtime/persistence/execution_transaction.py` 已消费 run/fence/revision。它们可作为本地 JSONL/单机多进程机制基础；File lease 的 host/filesystem scope 不能证明 Temporal 跨主机 ownership。当前 Temporal activity 仍依赖 process-local `StepHandlerRegistry` 与本地 RunJournal，因此“以 Temporal 原生 execution identity/history 实现同一 contract”目前只是实施方向，不是已存在的 guarantee 证据。现有 `RuntimeExecutionTransaction` 也是进程内 transaction state，不能冒充 durable operation store。

开工前 guarantee matrix 必须逐格给出源码/测试证据，未知格不得以“backend durable”统一代替：

| backend/mechanism | identity 与 scope | renew/takeover | fenced mutation | durable scan/reconcile | activation failure | 当前结论 |
| --- | --- | --- | --- | --- | --- | --- |
| InMemory coordinator | process-local subject/owner | 有，进程死亡即丢失 | 当前进程内 guard | 无跨重启 scan | construct-only | 仅用于 fake/process-local，不承载 durable Workflow |
| FileLeaseCoordinator + JSONL | 同机或明确共享文件系统 | 有 file lock/token；需 R2.29 strict schema | 可保护同一文件协调域；所有 mutation consumer 尚需接线证明 | RunJournal/owner record scan 需补齐 | I/O/lock 失败 fail closed | 可作为 local durable backend，不能宣称任意跨 host |
| RuntimeExecutionTransaction | 单进程 run/fence/revision | 无 durable takeover | 仅内存 mutation validation | 无 | construct-only | 复用 command语义，不复用为 durable store |
| Temporal | canonical workflow/run；跨 worker/host command state | workflow history/timer/retry拥有state transition；activity at-least-once | workflow revision只fence state mutation；external effect仅由provider idempotency/receipt store约束 | workflow timer/retry或明确durable enumeration推进；visibility仅作发现优化 | plugin/client/worker activation fail closed且不回退 | typed activity + effect capability/reconciliation；不宣称Temporal单独提供exactly-once effect |

canonical backend 决定：local JSONL 使用 Runtime FileLease/strict RunJournal operation owner；Temporal 使用 Temporal-owned workflow ID/run ID 与 event history 作为 command/state transition 真相。workflow timer/retry或明确durable enumeration负责恢复推进；visibility/search attribute只能是可丢失、可延迟的发现优化，不能成为authoritative scan。当前 closure-based `DurableBackend.run_step(execute=...)` 不适合作为 Temporal 跨主机 contract，必须迁为稳定注册的 typed activity handler与版本化 serializable command/result DTO，由 worker activation 显式注册；删除 `StepHandlerRegistry` 生产路径与“本地 RunJournal 是两 tier 共同跨 host 真相”的承诺。Temporal adapter 将 canonical effect intent/receipt 投影到 Runtime operation Port，但不得要求 remote worker 访问提交方进程内 closure 或仅本地 journal。Workflow effect 状态机仍归 Orchestration，Temporal 只实现机制。

Temporal external-effect guarantee与state ownership分开。每个activity command在durable intent后携带封闭effect capability：`IDEMPOTENT_BY_KEY`要求provider接受稳定logical EffectId/idempotency key，retry与新attempt复用同一key；`RECONCILABLE_BY_RECEIPT`要求首次dispatch receipt/status identity durable commit，retry/takeover先query/reconcile，只有provider明确证明未执行且contract允许时才能再次dispatch；`NON_REPLAYABLE`表示provider无幂等键、无可靠查询且无外部fenced effect store，dispatch后结果未知只提交typed `IN_DOUBT`并等待人工/显式补偿，禁止automatic activity retry或新owner重放。

workflow generation、activity attempt与revision只fence Temporal/local state mutation。除非provider或外部durable effect store实际校验fencing token，不得声称旧attempt“不能执行外部动作”；正确保证是迟到mutation被拒绝、未知外部结果被保留并对账。generic activity不得用统一retry policy覆盖所有effect：无外部effect可按声明重试，`IDEMPOTENT_BY_KEY`复用同key，`RECONCILABLE_BY_RECEIPT`先查询，`NON_REPLAYABLE`禁重试。若请求guarantee高于provider capability，Temporal planning/activation返回typed unavailable并fail closed，不回退local JSONL。由此state owner、effect replay safety、backend与复用边界均已确定；crash/worker-loss proof属于实施后`DONE`验收。

实施任务：扩展现有 Contracts lease/operation seam 为最小 typed operation-ownership Port，表达 deployment/backend identity、scope、holder、monotonic fence、expected revision、renew/takeover/release receipt、effect capability与guarantee projection，不新建同义lease facade。Runtime本地backend复用canonical coordinator；Temporal adapter映射command ownership与分类effect policy。Product显式选择backend，且不得在Temporal激活失败或provider capability不足时回退JSONL。所有backend必须满足其声明的strict codec、crash recovery、authoritative recovery progression与state fencing；旧owner不得提交intent、receipt、terminal、delivery ack、cancel settlement、lease mutation或delete，但外部动作安全只能由已声明的provider capability/receipt store保证。logical EffectId贯穿permission、intent、execute、provider receipt、audit与settlement；外部成功而receipt/terminal未提交时进入typed `IN_DOUBT`并按capability对账。Workflow状态机仍由Orchestration拥有。

验证与签收：两个进程/host争用同一operation只有当前state owner可提交，旧generation mutation均被拒绝；不得用该断言证明provider未执行。覆盖provider成功后worker断联、Temporal retry前旧activity迟到、无幂等provider timeout、receipt query返回unknown、visibility丢失/延迟以及workflow timer/restart；分别证明同EffectId不双执行、先reconcile后dispatch，或稳定进入`IN_DOUBT`且不伪报未执行。逐backend/effect capability校验scope与guarantee，能力不足时planning/activation fail closed且无新effect；架构门禁证明Runtime不拥有Workflow状态机，Product不复制operation owner。

## 5. P1：跨层 Port 与控制面闭合

### R1.1 类型化 Runtime composition lease

`contracts/runtime/application.py::RuntimeCompositionLeasePort` 的 route policy、default model、command/session/transfer runtime、permit issuer、artifact store/reader 均返回 `Any`。

实施任务：使用Contracts-owned窄Protocol/value objects，或拆成少量按能力与真实lifecycle聚合的Port。Product不得通过lease获得无类型Runtime对象图，Contracts也不得依赖Runtime concrete class。lease必须表达resource identity、scope、generation、holder identity、acquire/transfer/release状态和typed receipt；incarnation replacement转移共享ownership时使用typed transfer，旧generation holder不能release新holder资源。borrowed capability在API上没有close权，禁止`owned: bool`承担隐含所有权。

验证与签收：该Port公开面不含无界`Any`；Product inference API只依赖Contracts类型；覆盖double-close、leak、transfer、eviction、重复release和stale-generation release，borrower静态上无法关闭owner资源。

### R1.2 拆分 `RoleState.env` 的混合职责

`RoleState.env: Optional[Any]` 当前可能保存 Agent environment、`MoteEnv` 或 `PortHumanChannel`，混合多 Agent 控制面和人机交互面；CLI 还会直接替换该字段。

实施任务：

- orchestration/environment port 与 human interaction port 独立注入；
- 运行时依赖进入 `RoleComponents` 或 wiring，不进入 `RoleState`；
- `RoleState` 只保存可序列化运行状态。

Session hosting 当前还会为每个 `ConnectionScope` 覆盖共享 resident Role 的 `state.env`，关闭时再恢复旧值。重叠 scope 交错关闭时可能恢复错误 channel，使提问或审批被送到另一连接，因此该项同时属于并发正确性要求。

Orchestration 的兼容 facade 进一步放大了该混合边界：`orchestration/agents/environment_facade.py:40-115` 用 `_roles: Dict[str, Any]`、`add_role/get_role/set_addresses` 保存和返回宽 Role，并通过嵌套 `getattr/hasattr` 推断 schema/name/state/set_env；human channel 方法也以 `Any` 传递 question/request/sender（同文件 `156-196`）。Runtime `Role` 与 capabilities 随后直接假设 `state.env` 具有 `set_addresses/publish_message/ask_user/request_approval/reply_to_user` 等未声明方法（`runtime/agent/role.py:498-532,1116-1120`、`runtime/agent/capabilities.py:293-364`）。`BaseEnvironment` 仍是允许 arbitrary runtime object 的 Pydantic facade，实际生产主要由 `AgentEnvironment/MoteEnv` 继承，未形成可序列化价值。

实施任务：不仅移除字段覆盖，还要为 agent routing 与 human interaction 的真实消费者分别定义 Contracts-owned 最小 Protocol。用户已确认 `AgentEnvironment`、`BaseEnvironment`、`MoteEnv` 不存在必须兼容的仓外稳定 SDK/API；迁移仓内消费者后直接删除这些宽 facade、package export 和只验证旧路径的测试，不建立替代 facade、兼容 Port、alias 或 re-export。

验证与签收：生产代码不再通过 `role.state.env` 获取或替换服务对象；并发连接使用显式 turn/session-scoped human port，互不覆盖或恢复对方的 binding。

### R1.3 删除 Product interaction 私有反射和签名探测

`SessionDriver` 当前通过 `hasattr/setattr` 注入 `_on_interrupt`、`_is_turn_running`、`_on_steer`；`PortHumanChannel` 通过 `hasattr` 和捕获 `TypeError` 探测旧版 `ask`、`ask_questions`、`decide_approval`、`open_handoff`。

实施任务：

- 定义显式 driver-control binding seam；
- structured question、approval、handoff 使用可组合窄 Protocol；
- 迁移所有内建 Port 后删除私有字段注入和旧签名降级。

验证与签收：Product interaction 核心路径不使用反射判断能力，也不以 `TypeError` 进行 API 版本协商。

### R1.4 建立显式 deferred-result contract

BackgroundTask 和 Workflow 各自定义延迟结果，Runtime 依靠 `is_background_result` 与 `to_tool_result` 的 duck typing 消费高层对象；Workflow 还保留 `BgTaskResult = WorkflowDeferredResult` alias。

实施任务：产品语义已经确认：BackgroundTask 是 Agent-owned、process-local 临时并发；WorkflowRun 是跨进程 durable execution，二者不是同一种执行语义。分别定义窄 typed result/settlement contract，并在需要进入共同 Runtime tool/result 边界时显式投影到 canonical tagged union。Runtime 不得反射识别 Orchestration 对象。删除历史 alias，不得把 Workflow continuation/checkpoint/resume 塞入 BackgroundTask contract。

验证与签收：`runtime/tools/tool_pipeline.py` 不再读取魔法属性；跨层依赖保持单向。

### R1.5 分离 BackgroundTask 与 Workflow 状态 identity

`orchestration/workflows/types.py` 与 `orchestration/background_tasks/status.py` 各自定义字段完全相同的 `BgStatus`。Workflow 版本的注释仍把已删除的 `common/schema/node_status.py` 声明为 canonical owner。

实施任务：产品语义已经确认两者 lifecycle、durability、recovery 和 residency guarantee 不同。BackgroundTask status 由 Agent-owned process-local task lifecycle 拥有；Workflow node/run status 由 durable Workflow 状态机拥有。使用不同名称、不同 authoritative type 和显式 tagged projection，即使当前枚举值偶然相同也不得合并成一个 canonical status。删除过期 `common` owner 注释和 `BgStatus` 同名歧义。

验证与签收：BackgroundTask 与 Workflow 各自只有一个 defining module；跨边界 projection 可判别来源，Workflow durable decoder 不接受 BackgroundTask status 冒充，BackgroundTaskPool 不持有 Workflow definition、continuation、checkpoint 或 resume state。

产品已经确认保留 `BackgroundTaskPool.resubmit()` 的纯 process-local 同 TaskId retry，以降低模型跟踪逻辑任务的认知成本。`TaskId` 是模型可见、在所属 Agent pool 内稳定的逻辑任务 identity；每次 submit/resubmit 创建 Pool 内单调 `AttemptId`。旧 attempt 失去提交权，不得覆盖新 attempt 的状态、输出、progress、result pointer 或 notification。query/cancel 默认作用于 active/latest attempt；历史 attempt 只暴露 typed immutable settlement；输出携带 attempt identity，terminal notification 以 `AgentId + TaskId + AttemptId` 幂等。

resubmit 仅允许同 Agent、同 Pool、同进程，不携带 Workflow definition、continuation、checkpoint 或 durable resume state。进程终止后旧 TaskId/AttemptId 失效，新进程不得接管；需要跨进程、eviction 后继续或 effect reconciliation 的 retry 必须在提交前进入 WorkflowRun。删除 Workflow continuation/checkpoint 借原 BackgroundTask TaskId 恢复的路径和相应跨域 decoder，但不得按 `resubmit` 方法名删除合法 process-local retry。

### R1.6 修正 hosted service composition governance

当前 `hosted-service-gateway` declaration 指向 `RuntimeModelInferencePort`，实际媒体与 Web Search 通过 `ServiceGateway`、`Role.invoke_service` 和 Product service snapshot 执行，声明没有覆盖真实链路。

实施任务：

- 为真实 hosted service gateway 声明 implementation、canonical factory、required port、scope 和 lifecycle owner；
- image generation、speech、transcription、image description、Web Search 的 operation support 可在 endpoint/service capability 中表达；
- planner 在调用前拒绝不支持的 operation；
- composition gate 验证媒体链路从 Product root 唯一可达。

验证与签收：治理声明与运行时装配对象一致；不存在以模型 inference capability 代替 hosted service gateway 的声明。

### R1.7 治理配置期 API-key helper 命令执行

`product/config/model/inputs.py:104-110` 将 `api_key_helper` 暴露为字符串配置；`product/config/loader.py:131-139` 在构造 typed config 之前执行它；`product/config/secrets.py:31-52` 以 `subprocess.run(command, shell=True)` 直接交给 shell。虽然 `product/config/layers.py:25-35` 会从 WORKDIR 层剥离该字段，但 `ConfigSource.trusted` 把 SYSTEM、USER、PROJECT、PROFILE、ENV、CLI 和 PROGRAMMATIC 全部视为可信，尤其 `product/config/sources.py:109-111` 的 PROJECT 文件仍可注入任意命令。该路径绕开命令 classifier、permission mode、审计和 sandbox，并在普通配置读取时隐式产生进程副作用。

产品决定：`api_key_helper` 只允许 MANAGED 或经过 canonical ownership/permission 校验的 USER 来源提供；PROJECT、WORKDIR、ENV、CLI、PROFILE 与普通 PROGRAMMATIC 输入不得提供或覆盖。helper 使用结构化 argv/受限 executable reference，不接受 shell 字符串或 `shell=True`。

实施任务：实现单一受治理 helper runner：

- discovery/merge 在执行前验证 canonical source descriptor、resolved path 和 ownership，禁止低信任 layer 注入或覆盖 helper；
- helper 进入固定结构化 argv runner、超时、输出上限、最小环境和审计，不获得用户 shell intent runner 的宽能力；
- 配置 parse/validate 与外部 secret resolution 分阶段，普通 schema inspection、provenance 和 dry-run 不执行 helper；
- secret stdout 不进入日志、异常、provenance 或持久缓存。
- helper declaration必须解析为非空结构化argv和approved executable identity；Runtime fixed-argv runner使用受限cwd/env、timeout与output bound。stderr/error统一redact，stdout只进入secret value adapter；unknown source、空argv、nonzero exit、timeout、oversize和decode error fail closed。不为旧字符串配置保留shell fallback。

验证与签收：checkout/project config不能启动进程；metacharacter不产生shell expansion；unknown source/empty argv/nonzero/timeout/oversize/decode均fail closed；source、argv、decision和exit status可审计但stdout/stderr secret永不进入log/exception/provenance/artifact/snapshot；生产subprocess入口均分类。

### R1.8 让配置来源的信任等级绑定 canonical path

配置 discovery 当前由调用方同时传入 `user_config_root` 与 `source_root`，随后仅按枚举标签决定 trust：`product/config/sources.py:29-55,75-126`。同一个物理文件可以因参数组合被重复或以不同 trust label 加载。例如 canonical CLI 的 `product/entrypoints/cli/backend.py:73-80` 同时把 `paths.user_config_root` 传作 USER root 和 PROJECT source root，于是同一个 `config.yaml` 先作为 USER、再作为 PROJECT 合并；其他调用者又可通过任意 `source_root` 把原本工作目录附近的文件标为 trusted PROJECT。`product/config/loader.py:91-100` 不做 resolved-path 去重或 owner/permission 校验。

这会让 provenance、precedence 和敏感字段准入依赖调用参数，而不是文件真实位置/所有权；与 `api_key_helper` 等可执行配置结合时，trust misclassification 会扩大为命令执行边界问题。

实施任务：

- canonical composition 统一产生不可伪造的 config source descriptors，来源类别由 resolved path、owner/permission policy 和明确 Product root 决定；
- 同一 inode/resolved path 在一个 layer stack 中最多出现一次，若匹配多个类别必须 fail closed 或采用固定的最不信任等级；
- `source_root` 不再作为可把任意目录升级为 trusted PROJECT 的普通参数；若安装级 project config 必须存在，使用语义明确的受验证 path；
- provenance 记录 canonical source identity 和 path，不只记录可被调用方指定的枚举名。

验证与签收：USER 与 PROJECT path 相同时只加载一次；symlink/relative alias 不能绕过去重；把 cwd 或任意 checkout 作为 `source_root` 不会获得 secret/helper/endpoint 配置权限；CLI、gateway、report、watcher 使用同一 discovery 结果。

### R1.9 建立统一的项目扩展来源信任与 provenance

Product canonical composition 不只读取普通配置，还会把 checkout 内容直接提升为可影响模型行为或启动外部能力的扩展。Markdown Agent discovery 从用户目录以及 cwd 到 git root 的每一级 `.mote/agents/*.md` 读取定义（`product/agents/markdown_loader.py:131-168`），其 frontmatter 可选择 tools、model 和 aliases，正文成为 system instruction（同文件 `66-125`）；`product/composition/container.py:55-71` 自动把这些定义放入 `AgentCatalog` 和模型可见的 `Agent` 工具，后者会按 catalog definition 构造并运行子 Agent（`product/toolsets/builtin/agent_tool.py:55-107,131-147`）。陌生 checkout 因而无需独立信任决策即可改变模型可见 delegation roster、指令、工具与模型选择。

同类来源目前各自发现和降级：Skills 可从项目目录和 `extra_dirs` 加载正文并注入 prompt/fork child（`product/skills/factory.py:18-25`、`product/skills/skill_pool.py:76-149`），MCP 从层叠 `.mote/mcp.json` 读取 command/url/env 且 malformed entry best-effort 丢弃（`product/config/adapters/mcp.py:45-130`），Hooks 又有独立的 shell 执行风险（R0.6）。Skill 的启发式 body audit 能阻断少量已知 pattern，但不能替代“用户是否信任该来源”的 provenance/authorization；把扩展散落在四套 discovery 也使 symlink、覆盖优先级、hot reload 和撤销语义无法统一。

实施任务：

- 建立 Product-owned canonical extension source descriptor，至少记录 kind、canonical path/inode、scope、precedence、content digest、trust decision 和批准主体；Agent、Skill、Hook、MCP discovery 都只消费该结果；
- 用户级内建/显式安装来源与 checkout/project 来源分开授权；首次打开或来源内容变化时，具有执行/工具/模型/外连能力的项目扩展必须 fail closed，不能因处于 git root 自动获信；
- trust 必须绑定 canonical source identity 与 digest，不能只绑定可控的 agent/skill/server 名称；symlink、相对路径和同 inode 多入口不能绕过去重；
- hot reload 只能在既有批准范围内更新；新增能力、command/env/url、工具集合或 source replacement 必须重新决策；拒绝、撤销和 malformed source 产生可审计结果，不得静默换成低权限外观后继续；
- R0.6 的命令 permission/sandbox、MCP credential/transport policy 和 Skill 内容审计继续作为 trust 之后的纵深防护，不能互相替代。

验证与签收：打开含 `.mote/agents`、skills、hooks 或 MCP 的陌生 checkout 不会自动把其内容注入模型或启动进程/连接；批准单个 source 不会授权同名的另一文件；内容或 symlink target 变化触发重新校验；Agent 工具描述只列出当前已批准 snapshot；所有四类扩展可追溯到同一 provenance/trust decision。

### R1.10 让远程 Session 的 load、fork 与 turn 结果保持真实语义

ACP/AG-UI 的显式恢复入口实际使用 mint-or-return API。`SessionRegistry.get_or_create` 对未知 id 构造新 Role，并在 resume 抛异常时只记录 warning 后继续返回空 session（`product/session_hosting/registry.py:107-149`）；ACP `session/load` 直接调用它并宣称成功（`product/interfaces/acp/server.py:384-393`），AG-UI `/connect` 同样返回空 state/messages（`product/interfaces/agui/server.py:120-131`）。因此不存在、损坏、身份不匹配或 codec 不兼容的 durable session 都可能被同 id 新 session 覆盖其对外语义，客户端无法区分“恢复成功”与“重新开始”。

ACP fork 也把失败伪装成成功：先用同一 upsert API 获取 source，`fork_session` 不可用或抛错时 `_fork_role` 返回 `None`，handler 随即创建无历史的新 session 并返回其 id（`product/interfaces/acp/server.py:54-63,395-412`）。单 turn 的普通异常只写 warning，最后仍返回 `stopReason=end_turn`（同文件 `414-465`）。这破坏历史连续性和协议级重试判断，且会掩盖 R2.27/R2.34 的 identity fail-closed。

实施任务：

- 将 `create_new`、`get_resident`、`load_existing`、`fork_existing` 拆成不同 typed operation/result；load/fork 不得调用 upsert；
- `load_existing` 必须消费 canonical verified-load Port：R2.27 负责 file key/record/Role/rollout meta 身份判定，R2.34 负责 definition/source identity，R2.43 只提供统一 Session history/read projection；remote adapter 不得按路径重读 durable record、重算 definition digest 或建立第三个恢复 validator；
- verified-load 证明 durable record 存在并通过 Session/Residency/definition identity 校验，unknown、corrupt、migration-required 返回可机器判别错误，且不创建替代 session；
- fork 只有在 source 成功加载且历史分支提交完成后才返回新 id；失败不降级为 fresh session；
- turn 的 cancelled、failed、completed 使用协议真实可表达的结果或 error response；不得把执行异常编码为正常 `end_turn`；
- durable identity 验证失败时保留原始恢复证据，不注册同 id 空 Role，不被后续新写覆盖。

验证与签收：未知 id、损坏 rollout、role/toolset/definition mismatch、fork 不支持、fork 中途失败和 turn exception 在 ACP/AG-UI 中均可区分且 fail closed；失败后 registry 不出现替代 session，原 durable record 保留；只有显式 new operation 能创建空 thread；remote adapter 测试使用 typed fake verified-load Port，架构门禁禁止其导入 Residency store/codec、definition loader 或直接扫描 Session 日志。

### R1.11 让 Cron trigger 按 receipt 结算而不是按调用返回结算

Automation contract 明确定义 `ACCEPTED/DEFERRED/REJECTED` 三种 `TriggerReceipt`（`orchestration/automation/__init__.py:19-34`），Product Agent adapter 在 target active 时返回 DEFERRED、目标缺失或 dispatch 异常时返回 REJECTED（`product/automation/agent_trigger.py:14-29`）。但 `CronService._on_fire` 完全丢弃 receipt，只把抛异常视为失败（`orchestration/automation/cron/service.py:133-156`）；`CronScheduler._check` 随后无条件把 recurring task 标记已触发并推进 next fire，或直接删除 one-shot/aged task（`orchestration/automation/cron/scheduler.py:150-189`）。startup missed compensation 同样在 `_on_fire` 后批量删除（同文件 `221-248`）。

结果是 active target 返回 DEFERRED 时 one-shot 会静默消失、recurring 会跳过本次应投递；REJECTED 也被持久状态记录为成功触发。`trigger_id` 与 `receipt_id` 没有进入 durable settlement，崩溃重试无法区分未投递、已接受或重复接受。

实施任务：Cron dispatch 返回 receipt 并由 scheduler 按 disposition 做显式状态机：只有 ACCEPTED 才推进/删除；DEFERRED 保留同一 logical trigger identity 并按受限 backoff 重试；REJECTED 按声明策略进入 dead-letter/disabled/error，不得记成功。Trigger intent、attempt、receipt 与 task revision 必须 durable 关联，并支持 accepted 后、schedule commit 前的崩溃对账；sink 应按 trigger id 幂等。

每次scheduled occurrence在向Agent delivery、Workflow或external effect派发前commit durable fire intent，绑定task revision、occurrence identity和scheduler fence；随后通过对应canonical路径settle receipt。misfire、catch-up、overlap、timezone/DST、task update/delete与claimed occurrence竞态使用typed policy。Observation通知不得推进next schedule，重启后reconciler区分未派发、已派发未ack和unknown external outcome，不按当前时间盲目refire。

验证与签收：target active/missing、dispatch exception、intent/dispatch/receipt/schedule commit各crash点、重复tick与restart不丢one-shot、不跳recurring、不重复effect；只有匹配receipt的ACCEPTED推进；misfire/catch-up/overlap/DST/update-delete竞态确定。

### R1.12 让 Cron schedule 更新与 Scheduler lease 具备事务/fencing

Cron durable schedule 是一个 workspace-global JSON 文件，但 `CronTaskStore.add/remove`、scheduler `_persist_fired` 都采用独立的 load-modify-save 整文件覆盖（`orchestration/automation/cron/store.py:58-138`、`orchestration/automation/cron/scheduler.py:202-216`），没有跨实例/进程锁、revision 或 compare-and-swap。`CronService.create_task` 先用独立 `list()` 计数再 `add()`（`orchestration/automation/cron/service.py:89-117`），CLI 又绕过 Service 复制 validate/new/add 与直接 remove（`product/entrypoints/cron/cli.py:60-95`）。scheduler、CLI 和多个进程可相互丢更新，两个并发 create 也能同时通过 `MAX_CRON_TASKS`。

名义 single-writer `SchedulerLock` 也不提供可靠 fencing。lock body 只含可复用 `session_id`、PID 和 timestamp（`orchestration/automation/cron/lock.py:46-88`）；发现相同 session id 时，即使另一个 PID 仍活着也直接认作“ours”并覆写 body（`90-109`），形成双 owner。release 也只比较 session id，旧实例可删除新 owner 的同名 lock（`134-145`），存在 ABA；`refresh()` 在生产中没有调用，且即使调用也未校验唯一 acquisition token。

实施任务：

- 所有 add/remove/fire-settlement 进入一个 canonical transactional command owner，CLI 与 Service 都薄委托，limit check 与 commit 原子；
- schedule envelope 带 schema version、monotonic revision 和严格 decoder；更新使用跨进程 lock + revision CAS，冲突重读/重试，不做盲整文件覆盖；
- 每次 lock acquisition 生成不可复用 lease token 与 fencing epoch；refresh/release/commit 都同时校验 token、epoch 和 owner liveness，旧 owner 不得删除或写过新 owner；
- scheduler 的每次 durable mutation 携带 fencing token，失去 lease 后立即停止 settlement；mtime 只用于 reload hint，不能承担一致性。
- PID/session id、PID liveness或unlink不得充当ownership。lock corruption fail closed进入repair/reconcile，不能直接判stale；due claim、fire intent、delivery/effect receipt、refresh/release均校验monotonic fence，旧owner不得触发或删除新owner任务。

验证与签收：CLI add/remove、scheduler fire与并发操作不丢任务；并发create不突破cap；同session/PID reuse/container namespace下仅一个writer；stale refresh/release/save/fire拒绝；kill/reacquire、ABA和corrupt lock保持fence单调且不静默抢占。

### R1.13 让已接受的 Agent pending delivery 具备 durable 交付语义

AgentControl 对外承诺 send/communication “never fails and never drops”，在无法同步装载目标或 execution cap 满时即返回已接受并 park（`orchestration/agents/control.py:630-674,679-699`）。实际 `PendingDeliveryQueue` 只是进程内 `dict[str, deque] + asyncio.Event`，无 dump/journal/ack（`orchestration/agents/messaging/pending.py:66-173`）；只有装载成功后才转入 runtime Mailbox（`orchestration/agents/control.py:833-897`），而 Residency 只持久化已经进入 Mailbox 的内容。进程在 accept 与 flush 之间崩溃，会永久丢失调用方已被告知接受的消息。显式 release 还会直接 drop parked batch（同文件 `484`），与无条件 never-drop 文案冲突。

实施任务：产品语义已经确认：Agent delivery 关系、用户/Agent 消息及 accepted settlement 不能通过无 LLM、无费用、无时变外部调用、无副作用的确定性计算恢复，因此不属于 best effort。使用唯一durable状态机`intent_committed -> available -> claimed(fenced) -> delivered -> processed/terminal -> acked | dead_letter`；accepted只表示intent durable commit，不表示target已处理。delivery identity绑定source、target logical Agent、target lifecycle generation、payload schema、dedupe key与revision；Mailbox enqueue只是claim后的本地投影，worker crash/eviction后未ack item可重新claim，stale incarnation ack被拒绝。release/dead target、unknown definition、poison payload和retry budget耗尽进入typed rejected/dead-letter，不能静默drop。进程内queue/event只是可丢失唤醒优化，durable scan/reconcile必须能重新发现工作。

Message、InterAgentCommunication、用户输入、审批结果和control command使用各domain authoritative、versioned tagged union；unknown tag/version、extra/missing field和错误primitive fail closed，禁止持久化Python Message对象、callback、Role reference或裸dict。大payload复用canonical artifact/reference并让artifact ownership覆盖delivery retention；permission/trust/audit在decode/claim前校验，dead-letter、日志和trace不得泄漏secret。broadcast/subtree为每个target提交独立delivery identity与settlement，不能用Agent id列表冒充统一成功。

验证与签收：intent/claim/deliver/process/ack后任一崩溃点、目标eviction/rehydrate、重复flush、control/supervisor restart和release都有确定结果；消息最终恰好一次进入recipient mailbox（业务消费至少幂等）；stale ack、poison/dead-letter、broadcast部分失败、strict payload negative fixture与artifact retention通过；仅丢失进程内wake signal时durable reconcile仍能推进；不存在返回accepted后只驻留内存的分支。

### R1.14 让 MCP reload 成为校验后原子 generation swap

`runtime/tools/tool_lifecycle.py:228-243` 的 reload 先清理旧 MCP tool session、从 live catalog 删除全部旧名称并 teardown owner，之后才连接和绑定新 MCP；任一 connect、discovery、factory 或 register 失败时，旧集合已经不可恢复。新 owner 的绑定也不是原子的：`runtime/tools/mcp/lifecycle.py:49-75` 逐个向 live registrar 注册 definition，失败时只清理 MCP client，没有回滚此前已经写入 catalog 的 tool。底层 `BoundToolCatalog.register` 又对 canonical name 和 alias 逐项直接赋值（`runtime/tools/tool_catalog.py:64-68`），既不检查既有 owner，也不区分 builtin、pipeline 与不同 MCP source，因而 alias 可以静默覆盖现有工具。

实施任务：

- connect、discovery、definition compile、capability factory 和完整 canonical/alias namespace 校验全部在 shadow owner/catalog 完成；
- name-name、alias-alias、name-alias 以及与 builtin、pipeline、当前 MCP generation 的冲突统一 fail closed；
- 验证成功后一次性 swap catalog snapshot、lifecycle owner 和单调 generation，dispatch 只能观察完整旧代或完整新代；
- swap 后再清理旧 owner；旧 owner 清理失败只产生可审计 settlement，不得破坏已提交的新代；
- 任一预提交失败都保留旧 generation；`BoundToolCatalog.register` 禁止 silent overwrite，显式 replacement 必须携带 owner/generation token。

验证与签收：第二个新 tool 绑定失败时旧 MCP 集合仍完整可用且没有半套新集合；MCP alias 覆盖 builtin 或另一 MCP 时一致拒绝；并发 dispatch 与 reload 只看到完整旧/新 snapshot；变更事件携带真实 generation 及准确 added/removed/changed 集合。

### R1.15 闭合 OAuth subject、路径与凭据存储事务

`OAuthManager` 把传入 provider 直接拼入 credential filename 与 cross-process lock path（`runtime/models/auth/oauth/manager.py:41-58`、`runtime/models/auth/oauth/storage/file_store.py:23-27`）。Product model provider 与项目 MCP server name 都可成为该值，后者没有 pattern/path-segment 校验（`runtime/config/mcp.py:9-22`、`product/config/adapters/mcp.py:55-85`）；包含 `../`、绝对路径或分隔符的名称可使 token/lock 路径逃离配置的 storage root，保存时还会以 `O_TRUNC` 截断目标。

凭据持久化本身也不是崩溃安全的：File store 原地截断写且不 fsync/replace，读取截断、损坏或不合 schema 的安全状态时统一返回 `None`（`runtime/models/auth/oauth/storage/file_store.py:32-59`）；Keyring store 同样把损坏内容当不存在（`runtime/models/auth/oauth/storage/keyring_store.py:36-46`）。默认 `fallback` backend 在每次调用中独立尝试 keyring，再退到 file（`contracts/config/model/oauth.py:99-103`、`runtime/models/auth/oauth/storage/fallback_store.py:31-56`），一次 keyring save 失败可能把新 refresh token 写入文件，后续 keyring 恢复后却优先读到旧 token，形成无 generation/epoch 的双真相。`login()` 又在 refresh 使用的 subject lock 之外写 store/cache（`runtime/models/auth/oauth/manager.py:90-125`），并发 login、refresh 与 delete 无统一事务。

实施任务：

- 外部 provider/MCP 名称先转换成不可逆、固定格式的 credential subject id；token、lock 与 keyring account 均由该 id 派生，并验证最终路径严格位于 storage root；
- File store 使用 0600 唯一临时文件、flush/fsync、原子 replace 与目录 fsync；损坏、unknown version/shape 和权限异常 fail closed，不得解释成“未登录”；
- 一个 subject 在 generation 内只选择一个 authoritative backend；fallback 只能在显式初始化/迁移时选择并记录 backend、revision 和 token generation，不得逐操作漂移；
- login、refresh、force refresh、delete 与 backend migration 使用同一 subject lock/CAS transaction，锁内重读 canonical token，提交时原子更新 durable record 与 cache。

验证与签收：`../`、绝对路径、路径分隔符和碰撞名称不能创建或截断 root 外文件；任意写入崩溃点恢复为完整旧/新 token 之一；损坏 keyring/file 不会触发匿名或无状态重登录；keyring 故障后恢复不回滚 refresh token；并发 login/refresh/delete 不丢更新且不会让 cache 超前于 durable commit。

### R1.16 MCP 声明 OAuth 时认证配置必须 fail closed

项目 MCP adapter 对存在但类型错误或校验失败的 `oauth` block 仅记录 warning 并返回 `None`（`product/config/adapters/mcp.py:89-106`）。Runtime 随后把 `oauth is None` 明确定义为 unauthenticated client（`runtime/tools/mcp/oauth.py:57-71`、`runtime/tools/mcp/universal.py:189-195`），因此用户明确声明需要认证但字段损坏时，Mote 会向远程 server 发起匿名连接，而不是拒绝加载该 source。

产品决定（ADR-D3）：MCP catalog 以完整 candidate generation 为原子编译和发布单元。任一 source 的配置、trust、认证、discovery、definition 或 namespace 校验失败，都拒绝整个 candidate generation并保留旧 active generation；不得静默隔离失败 source 后发布部分能力。UI/CLI 返回带 source/path 和 typed cause 的 compilation failure；只有配置、凭据或显式 reload 改变后才重试。未来若需要 source 隔离，必须先定义显式 tagged compilation outcome 和新的 catalog identity 语义。

实施任务：区分“未声明认证”和“声明但无效”：前者可按 server policy 建立匿名 client，后者必须阻止该 server 进入 discovery/catalog，并返回可定位 source/path 的配置错误；不得把 credential/OAuth 解析失败降级为匿名重试。该语义纳入 R1.9 的 extension-source trust/provenance 与 R1.14 的原子 MCP generation。

验证与签收：无 OAuth block 的公开 server 可匿名连接；任意声明但无效、缺 secret/storage root 或损坏 credential 的 OAuth server 不产生网络请求、不暴露 tools，整个 candidate generation 被拒绝且旧 catalog generation 原样保留；同一 candidate 中其他 source 也不得被部分发布；日志与状态能区分 absent auth、invalid auth 和 generation compilation failure。

### R1.17 保持 FileChanged hook 的 canonical 类型到 wire 边界

File watcher 产生的 `FileChangedEvent` 已使用严格 `FileVersion` 与 `FileChangeAttribution`（`contracts/events/file/observation.py:24-26`），subscriber 也把这些对象原样传给 Hook（`runtime/hook/subscriber.py:41-49`）；但正式 `FileChangedPayload` 又把 prior/version/attribution 退化为 `object`，change type 退化为 `str`（`contracts/hook/invocation.py:54-60`）。`HookManager` 通过 `dict` 重建该宽 payload（`runtime/hook/manager.py:140-160`），失去静态 exhaustiveness 和稳定 codec 约束。

实施任务：内部 hook invocation 直接使用 authoritative `FileVersion`、`FileChangeKind` 与 `FileChangeAttribution`，subscriber 到 manager 保持 typed payload；只有 `HookWireSerializer` 在外部 JSON 边界调用 File canonical codec。不得为消除 import cycle复制枚举或使用 `object`，必要时将共享 value 移到正确 Contracts owner。

验证与签收：FileChanged 的内部生产链无 `object`/裸字符串退化；Absent/Present version 和 attribution wire round-trip 严格；未知 change kind、错误 version shape 在启动 command hook 前拒绝。

### R1.18 治理浏览器登录 profile 的身份、持久化与写入授权

`BrowserProfileStore` 保存的是完整 Playwright cookie/origin 登录状态，但把任意 profile name 经有损 slug 后直接作为 durable identity；例如不同名称可映射到同一文件（`runtime/interactive/browser/profile.py:35-45,81-84`）。读取时，文件截断、错误 key、认证失败和任意 JSON/shape 损坏都返回 `None`，调用方随后启动干净匿名浏览器（同文件 `87-106`、`product/toolsets/builtin/web_browser.py:238-252`）。写入则对最终 profile 文件直接 `O_TRUNC`，没有唯一临时文件、fsync、原子 replace、revision 或多 Role/进程锁（`runtime/interactive/browser/profile.py:108-134`）。这会把登录凭据损坏解释成“未登录”，并允许名称碰撞、崩溃截断和并发 last-writer clobber。

该写入还绕过工具声明的文件副作用边界。`WebBrowser` 在每次 action 成功后调用 `_persist_profile`（`product/toolsets/builtin/web_browser.py:359-389,542-552`），但未声明 `mutates_filesystem`、没有 `permission_targets`，`check_permissions` 仅处理 handoff（同文件 `397-400`）。因此 `AuthorizeStage` 看不到用户目录中的 credential write，sandbox、具体路径授权和 File mutation settlement 均不生效；一次只获准网页交互的调用可隐式改写跨 session 登录身份。

实施任务：

- profile 使用无碰撞的 canonical subject identity，并保存原始显示名、schema version、revision 与 content digest；拒绝空 slug、路径语义和 subject collision；
- credential state 使用严格 Playwright storage-state DTO/codec；损坏、错误 key、unknown version/shape 必须作为明确安全错误 fail closed，不能自动降级匿名会话；
- 写入使用 scoped cross-process transaction、0600 唯一临时文件、fsync、原子 replace 与目录 fsync，并以 revision/CAS 防止并发 profile 回滚；
- profile load/save 从 WebBrowser action 中拆成显式受治理 capability，或在调用前准确声明 credential read/write effect 与 canonical target；写入必须经过 permission/sandbox 和 durable settlement，不能在 action 成功后隐式 best-effort 吞错；
- 若产品希望“配置 profile 即授权自动更新”，该授权也必须是装配期显式、可审计且绑定具体 profile subject，而非依赖空 permission target。

验证与签收：碰撞名称不能共享文件或身份；截断、wrong key、错误 cookie/origin shape 不会启动匿名会话；并发两个 Role/进程更新同一 profile 不丢失新 revision；read-only/plan mode 不写 profile，workspace/sandbox policy 能观察并拒绝越界 target；profile commit 失败使本次需要持久化的操作返回真实失败而非伪成功。

### R1.19 将 AG-UI human reply 绑定 principal、session、turn 与 prompt

AG-UI 使用一个 app-scoped `PromptBroker` 保存全服务的 `prompt_id -> Future[Any]`（`product/session_hosting/prompt_broker.py:34-69`）。`AguiPort` 虽持有 thread/run id，但 mint/open prompt 时只把随机短 id交给 broker（`product/interfaces/agui/port.py:106-140,164-187`）；`POST /respond` 和 `/agent/{id}/respond` 最终都只读取 body 中的 `promptId` 并调用全局 `resolve`，既不校验 URL agent id，也不校验 principal、thread、run、prompt kind 或一次性 capability（`product/interfaces/agui/server.py:141-164,268-279`）。这意味着任一通过同一服务认证的客户端，只要获得另一请求的 prompt id，就能回答其问题或批准其工具动作；agent-scoped route 只是外观，不构成 owner binding。

实施任务：broker entry 使用不可变 owner binding，至少包含 authenticated principal/application scope、session/thread id、turn/run id、prompt kind、expiry 与 nonce；reply handler 从认证上下文和 URL/body 获取完整 scope 并恒定时间校验后才 resolve。随机 prompt id 只能作为相关键，不能单独成为授权凭证；approval 与普通 question 使用不同 typed payload/permission。scope 关闭、turn replacement、session eviction 和服务重启必须原子撤销 pending prompt，迟到/重复 reply 返回可机器判别的 stale/foreign 结果并审计。

验证与签收：两个都通过认证的 principal、两个 session、同 session 的两个并发 run 均不能互答 prompt；把 A 的 promptId 发到 B 的 agent route 必须拒绝；question payload 不能 resolve approval；timeout/close 后 reply 不复活旧 waiter；合法 reply 只结算一次且与 permission audit 中的 principal/turn 一致。

### R1.20 让 Agent residency eviction、rehydration 与 delivery 共享 incarnation fence

Residency 为腾容量会先从 `_residents` deque 弹出 candidate（`orchestration/agents/residency/manager.py:108-121,144-152`），此时容量统计已经不再计算它，但旧 runtime 仍在 live map，之后才依次 materialize、prepare、shutdown 和 remove（同文件 `122-134`）。慢 eviction 期间新的 reservation 可以占用“已释放”容量，实际仍存活的旧 runtime 加新 pending/runtime 会突破 hard cap；失败时再 `touch` 也无法撤销期间已经授出的容量。

消息与快照同样没有 generation fence。materialize 完成后到 shutdown/remove 之前，sync delivery 仍可从 `_runtimes` 取得旧 runtime 并把消息写进 mailbox；该消息不在已经落盘的 residency record 中，随后随旧 runtime teardown 丢失。反向 rehydrate 也没有 per-agent singleflight/CAS：`_try_load_sync` 与 `_ensure_loaded_async` 都只做无锁的 live/store 检查后各自 reserve（`orchestration/agents/control.py:758-801`），`_install_rehydrated` 可并发构造、启动并写同一个 `_runtimes[agent_id]`，各自 commit slot，最后写入者覆盖前一 incarnation 并删除共享 record（同文件 `803-829`）。

实施任务：为每个 agent 建立带单调 incarnation/generation 的原子生命周期状态机，例如 `ACTIVE -> EVICTING -> EVICTED -> REHYDRATING -> ACTIVE`；状态转换、容量 ownership、runtime map、scheduler membership、snapshot/record commit、mailbox delivery 与 teardown 共用同一 fence。EVICTING 后的新 delivery 写入 durable pending intent或在最终 snapshot fence 前结算，不能进入已封存旧 mailbox；容量只有在旧 runtime 停止且 record commit 后才转移。rehydrate 使用 per-agent singleflight/CAS，只有一个 loader 可消费指定 record generation并安装 runtime。

四类transition必须分开：eviction仅卸载resident incarnation，logical Agent仍active/known；worker crash把incarnation推进为lost并允许placement新generation；logical terminate/release先拒绝新turn/delivery/spawn，结算child、BackgroundTask和effect后进入terminal/tombstone；purge/GC仅在retention到期、无pin/未结算delivery/effect且持有当前fence时删除材料和索引。普通release不得直接purge。每个command返回typed receipt，旧incarnation迟到completion/result/ack不能复活terminal logical Agent。

验证与签收：在 materialize 后、shutdown 前注入消息不会丢失；慢/失败 eviction 期间 resident+pending+still-live runtime 不突破 cap；十个并发 sync/async load 只构造、启动和 commit 一个 incarnation；stale eviction/loader 不能 remove、forget 或覆盖新 generation；eviction、worker-loss、terminate、tombstone、purge分别产生可审计transition/receipt且late completion不能复活Agent。MANAGED `ChildAgentHandle.aclose()` 也必须通过该 lifecycle owner 停止并等待 scheduler driver 后再释放容量，不能只移除 membership 后 best-effort cleanup。

Residency 不拥有 BackgroundTask 的 task map、result、notification 或 drain 状态机；它只消费 R1.26 提供的 typed work-pin/drain/settlement Port。eviction/release 必须与该 Port 的 admission closure 原子协调，不能以一次 `has_pending()` snapshot 或直接读取 Pool 私有状态决定卸载。

### R1.21 闭合 Hosted Service 已接受操作的取消与 deadline settlement

媒体生成在 submit 后返回 durable `ServiceReceipt`，Gateway 随后循环 poll（`product/media_generation/service.py:61-110`、`runtime/service_gateway/gateway.py:394-481`）。但 poll 中调用方取消只执行 `permit.abandon()` 后重新抛出 `CancelledError`，不会调用 adapter 的 `cancel_once`，也不写 attempt/call terminal；总 deadline 在循环顶部直接抛出 `ServiceCallDeadlineExceededError`，同样不结算 receipt（`runtime/service_gateway/gateway.py:407-424,892-901`）。下一次相同调用会从 journal 恢复该 open receipt 并继续 poll（同文件 `141-168`），而不是反映调用方已经取消。虽然 Gateway 暴露独立 `cancel(service_call_id)` 并能远端 cancel 后写 terminal（`557-602`），正式 `Role.invoke_service` 只调用 `execute`，也不在取消路径转调该入口（`runtime/agent/role.py:702-736`）；仓内没有该 cancel Port 的生产消费者。结果是视频/音乐等已接受远端作业可在本地 turn/task 取消或超时后继续运行和计费，journal 长期保留 open attempt，重试又会复活用户认为已终止的操作。

产品决定（ADR-D2）：deadline 与 cancellation 是不同命令。总 deadline 只停止本地等待，持久化 `WAITING_REMOTE`（或等价 typed non-terminal disposition）并返回 durable resume handle；canonical reconciler 继续拥有 accepted receipt，调用方可查询、恢复等待或显式取消。明确 caller cancel 与 session shutdown 才在 provider 支持时请求远端取消；不支持、失败或结果未知时结算为 typed `IN_DOUBT/CANCEL_PENDING`。Product 可按 operation 定义不同 deadline 时长，但不能把 deadline 隐式解释为 cancel。

当前生产缺口：生产路径只在 `execute/resume` 同一 call 时推进 open receipt，不存在能够枚举 `WAITING_REMOTE`、周期 claim 并持续结算的 canonical reconciler。R1.22 的 per-call ownership 只解决“谁可推进”，不自动提供“谁负责持续推进”；该独立 lifecycle 已正式拆为 R1.25，本包不再含糊地要求“另立”未编号工作。

实施任务：Gateway 自己拥有 accepted receipt 的结构化 cancellation/finalization scope；caller cancellation、总 deadline、session shutdown 与显式 cancel 必须在同一 per-call lock/fence 下结算。支持远端 cancel 的 provider 调用 `cancel_once` 后再写 attempt/call `CANCELLED`；不支持、取消失败或结果未知时写带原因的 `IN_DOUBT`/cancel-pending settlement，不能留下无解释 open attempt。`Role`/后台任务取消必须传播稳定 service call identity，而非依赖无人调用的旁路方法。持续 scan、scheduler 与 takeover lifecycle 由 R1.25 拥有，本包只提交/query/cancel 同一 service call identity。

验证与签收：在 receipt 落盘后、任意 poll/backoff 点明确取消均产生唯一 durable settlement；支持取消的 provider 恰好收到一次 cancel，失败/不支持时可观察为 in-doubt 或 pending；deadline 不调用远端 cancel，原子写入 `WAITING_REMOTE` 并返回稳定 resume handle，reconciler 可继续发现和结算；相同 service_call_id 不会在用户取消后静默恢复 poll；并发 execute/resume/cancel/reconcile 只有一个 terminal，远端长任务不会因本地 task cancellation 或 deadline 成为无人管理的计费作业。

### R1.22 让 Hosted Service journal 与调用 ownership 支持多进程 fencing

CLI 将所有 session 的 Hosted Service 调用写到 workspace-global `.runtime/service-calls`（`product/entrypoints/cli/backend.py:103-123`、`runtime/service_gateway/journal.py:248-249`）。Gateway 的 per-call serialization 只是实例内 `dict[str, asyncio.Lock]`（`runtime/service_gateway/gateway.py:86,115-139`），Journal 也只有实例内 `threading.Lock`；append 在锁内先读全流并校验，再以 `O_APPEND` 写入（`runtime/service_gateway/journal.py:47-50,65-96`）。两个 CLI/Role/进程使用同一 deterministic service_call_id 时都能读取相同旧尾部、各自通过校验并并发发起/轮询远端操作；随后 append 的两条本地合法 record 组合后可能违反 ordinal/open-attempt/terminal invariant，使 journal 损坏，或者在发现损坏前已经产生双 submit、双 cancel 和重复计费。`O_APPEND` 只避免单条字节覆盖，不提供“读尾部—验证—远端副作用—提交状态”的对象级互斥或 fencing。

实施任务：为 service_call_id 建立跨进程 single-owner lease/CAS，lease 带单调 generation/fencing token；plan、attempt start、receipt、poll、cancel 与 terminal append 均校验 owner generation。Journal append 至少以跨进程锁和 expected tail revision 原子提交，record 带连续 stream revision；远端副作用前 durable claim ownership，失去 lease 的旧 worker不能继续 poll/cancel/terminal。若只允许单进程使用，composition 必须用隔离 root 并显式拒绝第二 owner，不能共享 workspace-global 路径却依赖实例内锁。

验证与签收：两个进程同时 execute/resume/cancel 同一 service_call_id 只有一个远端 owner；不会产生两个 attempt ordinal、双 submit 或交叉 terminal；kill owner 后新 generation 可接管 receipt，旧 owner 的迟到 append/poll/cancel 被 fence；journal 始终 strict round-trip，不依靠损坏检测来阻止已经发生的外部副作用。

### R1.23 让 workspace maintenance 使用跨进程 fenced owner

每个 Role 启动都会触发 Artifact GC，并可能触发 workspace cleanup（`runtime/agent/role.py:1193-1205`）。cleanup 的协调器明确只是 process-local `set + threading.Lock`（`runtime/session/workspace/cleanup_gate.py:1-23`），24 小时 stamp 也在 sweep **前**写入且不构成 owner lease（`runtime/session/workspace/cleanup.py:220-259,273-302`）；Artifact GC 甚至只有单个 `RuntimeMaintenance` 实例内的 task 去重，没有 workspace gate（`runtime/agent/runtime_maintenance.py:165-192`）。因此多个 CLI/进程可并发清理同一 workspace，且每个进程只排除自己的 session id。另一个仍活跃但 rollout mtime 已超过 TTL 的 session、其 task output/tool result，可能被其他进程当作 stale 删除。

实施任务：workspace maintenance 使用跨进程 lease、唯一 acquisition token 与 fencing epoch；sweep 前枚举所有 live session/run/residency leases，而非只排除调用者 session，删除和 release owner 时再次校验 fence。stamp 只作调度 hint，不能替代 ownership。maintenance scheduler 的 start/stop、周期、失败 backoff 与 Product composition 必须只有一个 owner；collector 通过 R1.24 的不可变 pin snapshot 查询存活闭包，不直接拥有 pin registry。

cleanup查询canonical lifecycle/pin/lease/retention facts并取得revisioned deletion claim；删除前复核无active incarnation、Workflow/effect/delivery、BackgroundTask residency pin、artifact/legal hold。每阶段受fence保护，失败留下可reconcile tombstone，不得先release artifact后把session继续视为正常active。TTL cleanup、user delete、security purge、legal hold和test temp使用不同typed command，分别定义authority、target projection、preview/approval、receipt、recoverability和audit；legal hold优先。Runtime只执行Product投影的validated retention spec，不硬编码`<=0 means never`等政策。

验证与签收：两个进程并发startup/cleanup/GC仅一个owner；mtime陈旧的活跃或pinned session不删；stale sweeper不能release/remove；kill/restart接管；legal hold覆盖TTL/user delete；partial delete留tombstone可reconcile；不可逆user/security purge范围和授权可审计；sweeper只消费pin projection。

### R1.24 闭合 Artifact pin registry 与 collector snapshot

`ArtifactRepositoryLayout.open` 支持 `pin_sources`，GC 会在 sweep 期间冻结 pins（`runtime/artifacts/layout.py:59-87`、`runtime/artifacts/gc.py:32-56`），但仓内没有任何生产调用传入 pin source；Role 与 cleanup 只传 FileOps root/metadata source（`runtime/agent/components/session.py:243-256`、`runtime/session/workspace/cleanup.py:69-87`）。FileOps 已有 durable cursor pin snapshot（`runtime/fileops/facade.py:301-305`），却未注入 workspace CAS collector。过期 session cleanup 又以 `minimum_gc_age_ns=0` 立即 collect，使正在读取、刚 stage 或尚未进入 durable root 的 blob 存在被删除窗口。

实施任务：在 Runtime Artifact bounded context 建立唯一 typed pin registry/lease service，覆盖 active read cursor、staged publication/outbox、mutation reservation、runtime checkpoint/transfer 和打开 payload；逐项登记 pin producer、identity、scope、acquire/release/expiry 与 crash recovery，不允许 collector 反向扫描各子系统私有 map。所有 collector 使用同一 revisioned immutable `ArtifactPinSource` snapshot，在 snapshot/lease 保证内完成 metadata prune 与 reclaim。若某类 transient blob 无法 pin，必须先 commit durable root 才能发布引用。不得把 workspace maintenance lease 与 blob pin lease 合并，它们的 owner 和生命周期不同。

repository维护typed ownership edge、retention class、source identity/revision与pin generation，覆盖SessionFact、Workflow checkpoint/result、BackgroundTask terminal pointer、ToolResult、model generation、FileOps snapshot和legal hold。producer先commit reference/ownership再公开pointer；owner删除先移除reference fact再GC。minimum age只是缓冲，不是reachability证明；collector以fenced snapshot扫描，stale collector不得删除被新generation重新pin的digest。

验证与签收：read/stage/publish/cursor/checkpoint/transfer 与 GC 的竞态不删除在用 blob；producer crash、重复 release、stale lease 和 snapshot revision 变化均有确定行为；所有生产 collector 均装配 canonical pin source，漏接任一 producer 的架构测试失败；registry 不复制各 producer 的业务状态机。

### R1.25 建立 Hosted Service canonical reconciliation owner

当前 `ServiceGateway` 只在 `execute/resume` 调用期间推进 open receipt，R1.22 的 per-call lease 只解决“谁可推进”，没有回答进程重启、deadline 返回或 wake 丢失后“谁持续推进”。因此 `WAITING_REMOTE` 若无 durable scan owner，会成为无人管理的远端计费作业。

实施任务：在 Hosted Service 所属 Runtime bounded context 建立 durable pending-call query/scan 与 claim Port，返回不可变 projection，不暴露 journal 内部 record；application/workspace-scoped reconciler 显式拥有 start/stop/recovery、稳定分页、R1.22 lease takeover、poll backoff、并发/预算/公平、poison disposition 与 terminal settlement。进程内 wake 仅作优化，周期 scan 必须能从 canonical durable facts 重发现全部 pending call；Product composition 负责唯一实例和 lifecycle，不得由 Role、媒体 adapter 或 Session resume 各自启动扫描器。

验证与签收：receipt 后在 deadline、进程 crash、wake 丢失及 owner loss 场景均可重新发现并结算；两个 reconciler 竞争时只有 fenced owner poll/cancel/terminal；分页无饥饿，poison call 不阻塞全局进度；construct/import 不启动 scheduler，activate/stop/restart 顺序可审计，仓内无第二扫描入口。

### R1.26 闭合 Agent-owned BackgroundTaskPool admission、pin 与 drain lifecycle

BackgroundTaskPool 的 task registry、sequence、result、progress、notification 与 cleanup 是单 Agent、process-local 状态；Residency 只需要知道该 incarnation 是否可卸载以及 drain settlement，不应成为 Pool 的第二 owner。

实施任务：每个 Agent-owned Pool 使用 `ACTIVE -> DRAINING -> CLOSED`，submit admission/work-pin acquire 与 begin eviction/release 在同一 generation-aware 同步原语下原子互斥。task 返回 local accepted receipt 前取得 pin，只有 operation、permit、output、terminal result/notification 与资源 cleanup 全部结算后释放。跨 Pool reference 绑定 process instance、Agent identity、incarnation、local TaskId，attempt mutation 再绑定 AttemptId；worker loss 不自动重放到新 incarnation。向 Residency/Supervisor 只公开最小 typed close-admission、drain/cancel command、pin snapshot 与 settlement receipt Port，不暴露 task map。release 先关闭 admission，再有界 cancel/drain；超时或失败保持 `DRAINING` 与 pin，强制终止由 supervisor policy 决定。

验证与签收：覆盖 submit/eviction TOCTOU、non-cooperative coroutine、子进程不退出、output flush/notification failure、重复 release 与 stale attempt；DRAINING/CLOSED 拒绝 submit，旧 incarnation reference 不会命中新 Pool；一个 Agent 的 release 不影响其他 Pool；Residency 只凭 typed receipt pin/unpin，架构门禁禁止其读写 Pool 内部 registry。

## 6. P2：多入口、泛型与边界整洁度

### R2.1 统一 Workflow definition 编译与身份入口

Workflow 同时暴露 `build`、`compile`、`arun`；Product adapter 还手工构造 `WorkflowDefinition`，绕过正式拓扑摘要和版本 identity 生成。

实施任务：建立唯一链路：

```text
Graph declaration -> canonical compile -> versioned WorkflowDefinition -> execute
```

便利 API 必须薄委托该链路，不能独立生成 identity 或执行语义。所有调用方使用同一 identity 算法。

definition必须是versioned immutable envelope，其digest覆盖graph topology、node kind与稳定implementation identity、输入/输出schema、routing/condition semantics、retry/timeout/cancellation policy、effect classification、tool/capability definition identity和codec version。未编码closure/callable只能由Product registry按稳定identity解析；unknown identity/version或digest mismatch fail closed。禁止用import顺序、对象地址或`inspect.getsource`形成durable identity。

本包止于 immutable definition 及唯一 compile/catalog 入口，不拥有 run store、scheduler、effect 或 reconciliation。迁移全部 declaration/Product adapter 消费者后删除绕过 compiler 的手工 definition 构造；纯本地 definition 测试 executor 不得拥有生产 RunId、进入 composition/catalog 或成为第二执行入口。

验证与签收：同一 graph 在不同进程得到同一 digest；definition mismatch、unknown implementation/version fail closed；`build/compile/arun` 便利面只薄委托 canonical compile，手工 Product definition 与第二 identity 算法归零；本包可在不启动 durable scheduler 的情况下独立签收。

### R2.47 建立 Workflow durable run command、store 与 lifecycle

当前 `WorkflowRun` 随机 UUID、`_executing`、asyncio task、内存 `_state`、`WorkflowContinuation` 与 Product continuation registry 共同形成多套 run truth，inspection 还直接读取 private state/graph。

实施任务：Orchestration 以 R2.1 definition identity 建立唯一 `RunId + expected revision + fencing token` typed command/store，query 只返回 immutable projection。拥有 checkpoint/frontier、cancel/deadline/pause/resume 与 terminal transition；Runtime 仅通过 R0.9/R2.46 提供 store、operation ownership 与 clock mechanism。cancel 定义 terminal race，deadline 持久化绝对时间，pause 保存 versioned reason/frontier/external-input contract，resume token 绑定 definition/run revision。迁移 Session resume、Product tool 与 inspection 后删除随机 run identity、进程内 production owner、continuation registry 和 private-state 读取。

验证与签收：覆盖 definition mismatch、checkpoint/frontier CAS、双 resume、进程重启、Session resume、Residency eviction、重复 cancel/resume、deadline 与 terminal race；旧 owner 的迟到 node receipt 不能推进或复活 terminal run；旧 state、registry、production entry 与 Product consumer 逐项归零。

### R2.48 建立 Workflow reconciliation、effect settlement 与 terminal delivery

durable run state 不会自行推进；scan、claim、effect intent/receipt、terminal delivery 若分散在 Product tool、Session resume 或进程内 wake，会形成第二执行入口并在 crash 后遗留未结算副作用。

实施任务：Orchestration 拥有唯一 reconciler 与 effect/terminal delivery 状态机；scan 只返回 candidate，执行前必须经 R0.9 fenced claim。定义稳定分页 cursor、公平、retry schedule、并发上限、backpressure、poison/dead-letter disposition与周期 scan；wake 仅作优化。外部 effect 先 durable intent，再执行，再以 receipt/settlement 对账；迟到 receipt 只能 settle，不能复活 terminal。terminal outcome 是唯一 durable fact，delivery 使用 durable intent/ack 并可从 scan 重发现。Product composition 只激活一个 reconciler，Product tool 与 Session resume 只能提交/query typed command。

验证与签收：覆盖 intent 后、effect 后 receipt 前、terminal commit 后 delivery 前、delivery 后 ack 前的 crash 与 lease loss；unknown outcome 进入 typed `IN_DOUBT` 且不盲重试；分页公平、poison 隔离、背压与 wake 丢失可恢复；仓内无第二 Workflow scan/runner/effect registry。

### R2.2 将 Kernel 内联 `CompletionPolicy` 提升为窄 Port

源码复核结论：`kernel/execution/engine.py::CompletionPolicy.evaluate(ModelTurn) -> CompletionDecision` 是单轮模型输出的完成判断；现有 `contracts/ports/output/run_completion_policy.py::RunCompletionPolicy.process(RunCompletionIntent) -> RunCompletionDecision` 是 Role flow 结束后的延续策略。二者名称相近但生命周期、输入和结果均不同，不能按旧评审线索机械合并，也不能让 Kernel 反向借用后者。

实施任务：由 Kernel 消费方在 `contracts/ports/execution/` 建立只包含 `evaluate(ModelTurn) -> CompletionDecision` 的窄 canonical Port，并使用能区分上述两个生命周期的正式名称；迁移 `ExecutionEngine`、`TextCompletionPolicy` 和测试 fake 后，删除 `kernel/execution/engine.py` 内联 Protocol。不得改造或复用 `RunCompletionPolicy`，不得从 Kernel facade re-export 新 Port。

验证与签收：单轮完成判断只有一个 Contracts-owned Port，Role run-completion Port 保持独立且名称无歧义；Kernel import graph 仍只依赖 Contracts；所有实现和 fake 通过各自 Protocol 的 Pyright 检查，Kernel 内联定义与错误交叉消费者归零。

### R2.3 泛型化 inference admission queue

实施任务：将 `QueueEntry.payload: Any` 与 `FairAdmissionQueue` 改为 `QueueEntry[T]`、`FairAdmissionQueue[T]`，保持 enqueue/dequeue、dispatcher 与具体 work item 的类型关系。不得引入无语义的 cast 或平行 queue 实现。

验证与签收：enqueue 的 payload 类型端到端保持到 dequeue/dispatcher；错误 work item 在 Pyright fixture 中拒绝；运行时优先级、公平、取消和 capacity 行为不变，生产边界无 `Any` 或恢复类型关系的 cast。

### R2.4 类型化 Agent control

实施任务：保留 ambient `ContextVar` 机制，但其内容必须是 Contracts-owned `AgentControlPort`；child spawn handle、message builder、on-spawn callback 和返回值表达稳定的泛型关系。迁移消费者后删除通过 `ctx.agent_control` 反射发现控制面的路径。

验证与签收：ambient bind/reset 在嵌套、并发 task、异常和取消后不泄漏 control；spawn/message/result 类型由 Pyright 贯穿；生产路径不使用 `getattr/hasattr` 发现 Agent control。

### R2.5 建立 Code Map query port

实施任务：为 `symbols_in`、`module_summary_of`、`importers`、`references_to` 定义 `CodeMapQueryPort` 和稳定 DTO，将 `build_turn_source(**kwargs: Any) -> object` 改为 typed factory contract；Product enrichment 只依赖该 Port，不用反射探测 Runtime indexer。

CodeMap继续拥有repository extraction/index/query，store、extractor和language provider留在包内。不得与LSP合成`CodeIntelligenceManager`：LSP的server lifecycle、document ingestion、diagnostics和workspace context consumer先单独形成consumer/lifecycle证据，裁决前不迁owner、不共享manager。

验证与签收：四类 query 的输入、缺失结果和稳定排序有 contract tests；Product 可使用 fake Port 构造 turn source；生产边界无 `**kwargs: Any`、`object` 返回或私有 indexer 访问。

### R2.6 类型化 hosted service 调用

实施任务：将 `Role.invoke_service(payload: dict[str, Any]) -> Any` 替换为 operation-tagged request/result，优先复用 Contracts 已有的 image generation、speech、transcription、Web Search 等 DTO，使 capability、operation key 与 payload 由类型和严格 decoder 关联。

OpenAI provider 的 `atext_to_speech`、`aspeech_to_text`、`gen_image` 若无生产消费者且不是已批准公共 API，应删除；若必须公开，只能作为 canonical gateway 的薄代理，不得独立拥有重试、路由或能力判断语义。

ServiceGateway public surface按composition与业务consumer分别推导。若failover snapshot是稳定immutable capability manifest，Contracts只拥有业务所需最小declaration；若仅由canonical Product root消费，可保留typed Runtime factory input但不能向media/search consumer公开。local journal path/layout、merge function和planner mutable state不得成为包根API，也不得为隐藏planner再造无状态Gateway facade。

验证与签收：每个 operation request/result 严格 round-trip，错误 tag/shape 在调用 provider 前拒绝；Role、工具和 Product consumer 不传裸 dict/接收 `Any`；provider 便利方法消费者归零后删除，保留者只薄委托 canonical gateway。

### R2.7 类型化内部 inference request

实施任务：`FinalizedInferenceRequest.payload: Any` 只有固定的 generate 请求生产链，不是开放插件或外部 JSON 边界；改为 typed generate request DTO，并与 `InferenceRequest`、`GenerateInput` 的职责边界对齐。

同时收窄 `kernel/inference/request.py` 中的裸 `list`、裸 `dict`、`tool_specs: Any`、`command_channel: Any` 和 `tool_snapshot: Any`；不得再让 ContextProvider 已知的类型在进入 Engine 前丢失。

验证与签收：Kernel 构造到 Runtime model gateway 的 request 类型端到端一致；错误 operation/payload 无法通过 Pyright 和运行时 decoder；现有 prompt、tool projection、command channel 与 request fingerprint 行为保持确定。

### R2.8 类型化 turn-context source 及 rebuild 生命周期

`TurnContextBus` 对 `EphemeralContextSource` 已声明的 `name`、`priority`、`save_to_context` 仍使用 `getattr`，并通过反射发现 `on_model_context_rebuilt`。Token/Fold pressure source 又将已有稳定 `TokenState`、`FoldState` 降为 `object` 后读取字段。

实施任务：直接使用基础 Protocol 字段，为 rebuild 能力定义独立可选扩展 Protocol，并让 state-provider Protocol 返回 Contracts 已有 DTO。保留 source failure 隔离，但能力协商不得依赖反射。

每个source声明稳定name、priority、typed dependencies、suppression、deterministic render和按信息性质定义的failure disposition；source不得持有完整Role、Context或Environment。Product选择启用source，Runtime拥有收集mechanism，domain只提供最小query。cwd、time、git、token pressure、BackgroundTask/LSP通知等易变内容只进入user prompt system-reminder；permission/approval等安全事实不能因source失败而消失，也不能依赖prompt注入成为authoritative truth。

验证与签收：基础 source 与 rebuild-capable source 通过显式 Protocol 区分；Token/Fold state 不退化为 `object`；source priority、suppression、failure isolation 和 rebuild callback 顺序有确定性测试，生产路径无能力反射。

### R2.9 统一 Workflow/Background progress port

实施任务：以类型化 progress DTO/sink 替换 `runtime/events/progress_scope.py` 的 `Callable[[str, Any, Any], None]`，把 Workflow/BackgroundTask progress 语义 owner 放在 Contracts 或 Orchestration；Runtime 只提供必要的 task-local binding，不拥有高层状态语义。

验证与签收：Workflow 与 BackgroundTask progress 可判别来源和 identity，Runtime binding 不持有其状态机；并发 task 的 progress 不串流，解绑、取消和异常后不泄漏；正式 Port 无 `Any`。

### R2.10 收窄 Agent wiring 与 Runtime client context

实施任务：将 `AgentDependencies.hook_config: Any` 和 `mcp_servers: tuple[Any, ...]` 直接类型化为 `HookConfig` 与 `MCPServerConfig`。

`runtime.models.clients.context.Context` 同时持有 config、成本、限流、health、持久化、模型控制、ServiceGateway 和 lifecycle，名称过宽且 `config: Any`。Product 应先把根配置投影为 Contracts-owned Runtime 配置视图，再注入语义明确的 Runtime/model-client context；Runtime 不得依赖 Product 根配置 shape。

整改前按每个Agent/Role组件建立consumer-to-capability matrix。`AgentWiring/EngineServices`可以是Product内部原子装配值，但不能成为Runtime组件跨bounded context查询服务的公共locator；每个组件只接收其窄immutable input或Port，禁止`get_service()`、字符串key、任意mapping、反射fallback，以及向无关消费者传播path/config/client/factory。

primary config path、secret predicate、user/session/browser/oauth roots和raw Hook/MCP config必须在Product先绑定canonical source/path ownership、content digest、trust decision与approval，解码为最小typed activation spec或approved path handle；Runtime/Agent不得自行重读配置、重新发现checkout extension或扩大root。

验证与签收：Runtime import graph不依赖Product config；每项capability有真实消费者、scope与lifecycle owner，组件拿不到无关配置/路径/close能力；公开字段无`Any`或locator API，construct不启动外部资源。checkout extension未批准或digest变化时不能注入模型、启动进程/网络或获得工具。

### R2.11 类型化 Agent hosting 与 facade

`product/session_hosting/registry.py` 复制 control 构造逻辑并直接写 `role.agent_control`，`ResidentSession.control/role`、role factory 和 engine 均为 `Any`。`orchestration/agents/environment_facade.py` 也用 `Dict[str, Any]` 保存 Role，相关 handle 的 control/residency slot 实际类型稳定却仍为 `Any`。

实施任务：复用统一 control composition seam，以 `RunnableAgent[T]` 和窄 control/residency Protocol 表达 resident session；Agent facade 沿用 `AgentRuntime[T]` 已建立的泛型关系，不再建立宽 Role 对象表。

该范围同时包含 CLI backend 的平行宽 hosting 门面：`product/entrypoints/cli/backend.py:157-284` 自行构造 Role、AgentControl 和 AgentRuntime，公开返回 `Optional[Any]`/`Tuple[Any, Any]`；`290-336,406-499` 又以 `Any + getattr` 暴露 human binding、history mutation、cleanup、fork、usage、session 与 runtime accessors。`product/session_hosting/registry.py` 已另有一套 `_build_control`，ACP server 也复制 `_fork_role`。要求 CLI、session hosting、ACP/AG-UI 全部依赖同一 typed session-hosting service 和 `RunnableAgent[T]` seam；测试 fake 的便利不能决定生产 facade 类型。

验证与签收：生产代码只有一个 root Role/control/runtime hosting owner；`ResidentSession[T]`、registry、CLI backend、ACP/AG-UI 不再以 `Any` 或反射判断 canonical Role 能力。Product application composition root 的收口由 R2.42 独立验收。

### R2.12 统一配置基础模型与 owner

实施任务：在逐项验证 validation/serialization 语义一致后，选择 Contracts canonical forbid-extra `ConfigModel`，迁移 Runtime/Product config 消费者；具体 capability config 仍归真实 owner，Product 根配置只负责组合，Runtime 所需配置通过窄投影下沉，不以 Product root `Any` 传播。

每个domain按“declaration→source/provenance→parse→secret resolution→validated activation spec”形成consumer matrix：Contracts仅保留确有跨层消费者的纯typed declaration；Product拥有source precedence、canonical path/ownership、默认backend/tool、trust/approval和secret resolution；Runtime只接收对应mechanism的validated activation spec；外部dynamic input在adapter严格decode。不能把result limit、compression、effect journal、loop guard、Temporal policy等不同owner重新聚成万能`ToolRuntimeConfig`。降低durability/safety guarantee的选项由Product policy决定是否准入，不能是Runtime普通bool。secret stdout不得记录。

验证与签收：生产源码只有一个基础 `ConfigModel` 定义；extra-field、default、serialization 和 error shape 在迁移前后等价；Contracts 不反向依赖，Runtime 不接收 Product root config，旧 base import 消费者归零。

### R2.13 类型化 Web Search backend registry

实施任务：为 Web Search 定义明确的 provider invocation Protocol/DTO，以稳定 `WebSearchConfig` 替换 `Any + getattr`，registry 只注册该 contract；第三方 provider wire 的动态值在 adapter 入口立即校验和投影。

验证与签收：backend 注册、选择、调用和错误结果均使用 typed DTO；错误 provider shape 在 registry/adapter 边界拒绝；Web Search one-shot settlement、timeout 和 capability planning 行为保持，内部无 `Callable[...]` ellipsis 或反射配置访问。

### R2.14 类型化 Kernel execution graph 组装边界

`kernel/execution/operations/container.py::GraphAssemblyInputs` 和 `kernel/execution/graph/nodes.py` 将 observation、inference、actions、context provider、completion policy、channel、inference engine、background pool 与 callback 大面积标为 `Any`，`ExecutionEngine` 构造器也重复丢失这些类型。多数协作者已有稳定 Protocol 或 concrete semantic interface。

实施任务：为每个节点声明其最小消费 Protocol，并让 `GraphAssemblyInputs[OutputT]` 保留 output 与协作者类型关系。不得建立第二套 execution abstraction；直接复用现有 `MessageStore`、`BackgroundTaskService`、`CompletionPolicy`、`CommandChannel`、`BaseInferenceEngine`、output 和 transaction Port。

验证与签收：Kernel graph 的构造与节点公开签名不含无界 `Any`，替代 graph builder 能由类型检查证明与 Engine 兼容。

### R2.15 类型化 Agent spawn contract 与 policy extension

`contracts/agent/spawn.py::SpawnContext` 的 agent path、config、cost tracker 仍为 `Any`，`bind_agent_control` 和 child provisioning 又退化为 `object`。Orchestration 的 spawn admission 已拿到 `SpawnPolicyExtension` factory，却仍把实例保存为 `object`，通过 `getattr` 检查 `evaluate` 后再用 type-ignore 调用。

实施任务：definition/builder/request/`RunnableAgent[OutputT]`/`AgentRuntime[OutputT]`/`ChildAgentHandle[OutputT]`/`RunOutcome[OutputT]`完整携带同一OutputT。删除`RunnableAgent[object]`、通过Protocol方法shape伪称验证`OutputT=str`的TypeGuard及事后cast。Product Coding Agent可用显式text definition或静态text builder表达当前specialization；dynamic manifest在adapter严格解码并绑定authoritative OutputContract，不能把跨层contract固定为str。

`SpawnContext`按consumer拆为Product-owned construction request、Orchestration-owned spawn identity/admission receipt、Runtime context provisioning所需窄Port、budget settlement Port与control capability，禁止重新聚合成`SpawnServices`。child builder不得取得完整parent Role、Context、CostTracker、raw config或path roots；每项capability有稳定identity，parent policy只能单调收窄。extension factory返回类型直接贯穿installed extension，不再反射验证静态已声明的方法。

验证与签收：正式边界不含`Any`、`object`、反射调用、错误TypeGuard或无理由ignore；Pyright分别证明generic child与text-only Product builder关系，dynamic output schema错误在adapter拒绝；construction/identity/context/budget/control消费者无法取得其他capability。

### R2.16 让 Agent turn admission 原子 acquire/release

Agent turn limiter 也必须使用原子 admission：`orchestration/agents/execution/limiter.py:38-50` 将 `has_capacity()` 与 `guard()` 分成两个操作，`guard()` 本身无条件递增；`orchestration/agents/control.py:621-674` 仅在消息投递时用 `has_capacity()` 决定 park，而多个已唤醒 runtime 随后可在 `turn_scheduler.py:200-212` 同时调用无条件 `guard()`。因此并发 turn 数可越过 `max_agents`，检查只影响排队时刻而不构成执行 admission。

实施任务：把 check-and-acquire 合并为原子 permit/guard 获取；permit 绑定稳定 admission receipt，从 admitted/running 到 terminal/cancel 严格一次 release。当前 process owner 内的取消、异常和重复 release 必须幂等；`active`、limit 与 permit 集合的读取遵守同一同步纪律。排队、跨进程 reclaim、公平调度与 durable scan 属于 R2.50，本包不得顺便建立 queue/store/scheduler。

验证与签收：两个或更多 runtime 同时 ready 时，实际进入 `run_one_turn()` 的数量从不超过上限；投递检查与 turn admission 间竞态有确定性测试；取消、异常、重复 release 严格一次结算；permit 泄漏可检测且 fail closed。本包可用 process-local deterministic test 独立签收，不宣称 supervisor restart 后 queue 可恢复。

### R2.17 让 Agent nickname reservation 具备事务所有权

实施任务：Nickname reservation 没有兑现 Spawn transaction 的 RAII 承诺：`_reserve_agent_nickname` 在预留时立即写入全局 used set，耗尽时甚至清空整个集合并推进 reset counter（`orchestration/agents/identity/registry.py:178-198`）；但 `SpawnReservation` 只记录 reserved path，rollback 不释放 nickname（`213-248`），`release_spawned_agent` 删除 child 时也不移除其 nickname（`99-109`）。让durable reservation显式持有不可复用token与lineage revision，commit将path/nickname索引绑定不可复用logical Agent ID；aborted且未公开的reservation仅由当前fenced reconciler精确回收，不能用清空集合处理耗尽。terminal logical Agent的path/nickname何时可重用由tombstone/retention政策决定，重用索引不得让旧delivery、lease、result或subtree snapshot命中新Agent。

验证与签收：spawn各崩溃点、构造失败、aborted reservation回收与retention后索引重用不影响仍存活owner；并发分配、pool耗尽、旧fence回收及path/nickname/agent-id ABA fixtures不产生重复或误命中。

### R2.18 类型化 ChildAgentHandle control 与 residency seam

实施任务：为 `ChildAgentHandle` 的 `control`、`residency_slot`、`release_child()` 和 `rollback()` 定义最小 Protocol，让 handle 的泛型结果关系贯穿 teardown；不得以避免循环导入为由保留 `Any` 或隐式结构约定。

验证与签收：`ChildAgentHandle[OutputT]` 的公开与内部 seam 无无界 `Any`，control、residency、rollback、release 与 outcome 的泛型及生命周期关系可由 Pyright 和 teardown 测试验证。

### R2.19 版本化 Residency durable schema

`orchestration/agents/residency/store.py::ResidencyRecord.from_json` 对缺失字段静默补空值。该数据跨进程重启和 agent eviction，属于 Residency owner 的 durable contract。

实施任务：增加明确format/schema version和严格decoder；record绑定logical Agent、root/parent/path、definition/content identity、incarnation generation、source Session stream revision、record revision与materialization fence。unknown/extra/missing field、错误primitive、identity mismatch和corruption fail closed，不能解释为空Role/Residency状态。

磁盘record只保存state/projection，不得携带可执行class path、Hook/MCP/provider/backend选择并自动激活。Product先按可信且已批准definition/config构造Role/Agent blueprint，再恢复经identity/schema验证的历史；definition/config content identity mismatch或trust失效返回typed decision-required/migration error并保留证据，禁止fallback default Role。

验证与签收：合法版本round-trip；unknown/extra/missing/wrong primitive、identity/fence/revision mismatch和截断均fail closed；篡改磁盘class/backend不能选择实现；迁移不改变canonical identity，失败不注册空Role或删除证据。

### R2.20 版本化 Mailbox durable schema

`orchestration/agents/messaging/mailbox.py::Mailbox.load` 没有格式版本或严格 shape。Mailbox dump 是 Agent messaging owner 的 durable contract，不能与 Residency store 共用状态或迁移 owner。

实施任务：定义版本化 envelope、严格 message/sequence/agent identity decoder 和有限期 upcaster；损坏或未知版本不得恢复为空 mailbox。

验证与签收：消息顺序、delivery identity 和 Agent owner round-trip 稳定；未知版本、错误 shape、重复 sequence 和 owner mismatch fail closed。

### R2.21 版本化 Cron durable schema

`orchestration/automation/cron/store.py` 与 `CronTask.from_dict` 没有 schema version/upcast 边界。Cron schedule 属于 Automation owner，不与 Residency 或 Mailbox 共享 codec、store 或 lifecycle。

实施任务：定义版本化schedule/occurrence/trigger DTO、strict decoder、task/revision identity、CAS和有限期migration；unknown version或corruption fail closed。允许的tail torn write与中间损坏分开；坏record quarantine并报告，不能返回空列表、skip后被下一次save覆盖。所有mutation经single command owner，与R1.11/R1.12的receipt/fencing使用同一canonical store入口；session-only与durable automation保持不同typed identity/guarantee。

验证与签收：合法schedule round-trip；unknown/extra/missing、错误primitive、task/revision mismatch、中间corruption和截断fail closed；quarantine不丢其他record且不被覆盖；迁移与并发scheduler owner不丢trigger、不双推进。

### R2.22 类型化 Activity topology 与 outcome

`ActivityStartedEvent.topology`、`ActivityCompletedEvent.node_states` 从 Contracts 经 Product ViewEvent、projection state、surface 一路使用 `dict[str, Any]`/`Any`，但实际 shape 已由 nodes/edges/status/attempts/error/args 固定。

实施任务：在 Contracts 定义 presentation-neutral 的 `ActivityTopology`、`ActivityNodeState`、`ActivityOutcome` DTO；Product 只做确定性 projection，renderer 可继续接收第三方 renderable。

Presentation迁移分为internal closed typed union、Product projector、各surface adapter和ACP/AGUI strict wire codec。只有稳定cross-host event/capability DTO进入Contracts；capability downgrade、fold mode、tool grouping、surface defaults、host choice和human wording留在Product。内部禁止开放字符串/`getattr`分派，旧Product DTO入口在所有consumer迁移后删除且不re-export。

验证与签收：Activity event、projection、surface 和 replay 使用同一 DTO；未知 node status、错误 topology/outcome shape 在 wire adapter 拒绝；并发节点、attempt、error 和 args 的投影稳定，内部链路无裸 dict/`Any`。

### R2.23 收口 Artifact repository 双命名

Runtime 同时存在两个活跃的 `ArtifactRepository`：

- `runtime/artifacts/repository.py`：通用 verified content-addressed store；
- `runtime/fileops/mutation/artifacts.py`：在前者之上增加 reservation、lifecycle catalog、locking 与 write scope 的 FileOps repository。

实施任务：两者不是相同实现，按职责重命名为明确的 content-addressed store 与 File mutation repository；迁移 `transactions.py`、`facade.py` 等消费者，让共同能力依赖 Contracts artifact Port，并删除内部 alias，不合并不同 reservation/lifecycle 语义。

canonical artifact content storage继续由`runtime.artifacts`拥有；FileOps只拥有mutation staging/catalog/scope、locking和journal，并通过最小FileOperations command/immutable receipt或明确artifact Port服务外部消费者。隐藏mutable scope、lock和storage layout，禁止再套第三个Repository facade。

验证与签收：两个实现具有不同且准确的公开名称，生产 import 不使用别名消歧；FileOps transaction 仍保留 reservation、locking、write scope 和 lifecycle，通用 store 仍保持 verified content addressing；旧同名/alias 消费者归零。

### R2.24 类型化 durable output event payload

`contracts/events/output.py` 的 candidate raw、accepted/migrated/committed value 是 rollout durable fact，却声明为 `Any`；通用 `DurableFact.payload()` 只执行 `vars()`，不能保证 JSON 值。Runtime OutputEngine 虽通过 decoder encode 处理 accepted value，但 raw candidate 和类型约束仍未在事件边界证明。

实施任务： durable output event 使用 `JsonValue` 或版本化 output payload contract，写入前验证；`OutputDecoder.encode` 的返回契约也必须表达 durable JSON。Telemetry-only model/provider SDK 观察值可通过单独 observation DTO 保持动态，不能混入 durable fact。

验证与签收：candidate、accepted、migrated、committed payload 均严格 round-trip，未知或非 JSON 值在 durable commit 前拒绝；compaction、replay 和 resume 后语义等价；telemetry 动态值不能进入 durable sink。

### R2.25 统一 SessionEvent catalog 的生成来源

Session 事件目前同时维护 `SessionEvent` union、`SESSION_EVENT_CLASSES` discriminator map、`SESSION_ACTIVE_CODECS` 派生列表和 `ROLLOUT_EVENT_TYPES` persistence policy。后两者部分由前表派生，但 union、map 和 persistence set 仍可独立漂移。

实施任务：为事件 identity/class/codec/persistence policy 建立一个 typed authority 或可验证的确定性派生关系；gate 必须证明 union、discriminator、codec 和 rollout policy 无遗漏、无多余项。不是要求所有 observation 都持久化，而是每个差异都必须显式声明。

`SessionFactSink`改为Session-owned closed versioned fact command union；每个variant绑定stream/session identity、event identity、schema version和strict payload。只有durable append成功才返回typed receipt、更新projection或发布后续事实；失败时不得更新内存或触发effect。迁移全部Context/Model/Routing/Role producer后删除`commit_fact(object)`、class-set admission、`isinstance+cast`恢复和宽decoder，unknown fact/version/extra/missing field fail closed。

同时收窄提交 Port：`contracts/ports/session/facts.py::SessionFactSink.commit_fact(event: object)` 当前允许任意对象进入正式 durable seam，Runtime 再依赖 `ROLLOUT_EVENT_TYPES` 和 `isinstance` 链做运行时筛选/投影。要求由同一 typed authority 生成 `RolloutSourceEvent` 封闭 union（或每类 typed sink），使 Context、Model gateway、Routing 和 Role 的调用在静态类型上只能提交已注册 durable source event。不得继续以 `object + type(event) in set + cast(SessionEvent, event)` 维持正确性。

验证与签收补充：`SessionFactSink` 不接受 `object`；新增 source event 未登记 projection/codec 时类型或 gate 失败，而不是运行到 commit 才抛错。

### R2.26 类型化 Shared inference daemon 内部执行边界

generated protobuf 对象位于网络 adapter 边界，局部动态可以保留；但当前动态性继续泄漏到内部正式 seam：

- `product/inference/daemon/grpc_server.py:20-60` 的 `SharedExecutionBackend`、`SharedGenerationControl` 对所有 RPC request/result 使用 `Any`；
- `product/inference/daemon/execution_backend.py:28-38,62-66` 的 event journal、execution registry 和 start request 均丢失类型；
- 同文件 `117-130` 通过 `hasattr(authorize_open/close)` 区分 session execution 与 finite execution，`244-293` 的注册、pump、validation 继续以 `Any` 贯穿；
- `product/inference/daemon/grpc_client.py:96-103,125-135,179-248` 已知的 protobuf request/response 仍全部返回 `Any`。

实施任务：在 generated RPC adapter 内立即投影为 typed request/result，并消费 R2.54 已固定的 finite/session execution variant 与 owner record；本包只迁移 protobuf adapter、client/backend、journal 与显式 variant dispatch，不重新定义授权 identity。authorize/cancel 不用反射猜测方法。`pb: Any` 若确因 generated module typing 限制需要保留，只能局限在 adapter module，不能成为 backend Port 的理由。

验证与签收：Shared backend 的公开 Protocol、execution registry、journal 和 control dispatch 无无界 `Any`/`hasattr`；mypy/pyright 能证明 session-only `send/authorize_open/close` 与 finite-only `authorize_wire/cancel` 不可混调。现有 UDS local credentials、signed session credential、protocol/schema negotiation、principal 和 generation binding 必须原样保留，不得以类型整改削弱认证。

### R2.27 闭合 Residency 与 Session resume 的身份一致性

`orchestration/agents/residency/store.py:119-187` 以调用方的 `session_id` 选择 residency 文件和 rollout，但加载 `role_dump` 后没有证明 `role.session_id` 等于查询 key。`189-206` 虽调用 `BaseRole.validate_resume_identity(replayed.meta)`，Session projection 的 reducer `runtime/projections/session.py:171-181` 会接受任意位置、重复出现的 `SessionMetaEvent` 并覆盖 `state.meta`，read path 没有重申 write path 在 `runtime/session/log.py:140-145` 执行的“meta 必须首条、唯一且等于 stream identity”约束。成功返回后，`orchestration/agents/control.py:803-829` 又以请求的 `agent_id` 注册 runtime 并删除该 key 对应的唯一 residency 文件。

损坏、错置或人工迁移的 record 因而可能形成 `registry key != role.session_id != rollout meta session_id` 的 split-brain runtime；随后 `forget(agent_id)` 使原始恢复证据不可再用。

实施任务：

- ResidencyRecord 增加版本化 envelope 和显式 session/agent identity，读取时严格校验文件 key、record identity、role identity、rollout stream id 与唯一 meta identity全相等；
- verified journal/read projection 必须执行与 append path 相同的 meta 首条/唯一/stream identity invariant，不能只依赖写入时约束；
- 安装 runtime、scheduler、registry 和删除 residency record 组成可恢复的 commit；全部 identity 校验通过前不得注册或删除；
- duck-typed fake role 不能成为生产恢复路径跳过 identity 校验的理由。

验证与签收：交换两个 residency 文件、篡改 role session id、缺失/重复/后置 meta、meta 与目录名不一致均 fail closed，且失败后原 record 保留；恢复完成后 registry key、runtime session、rollout stream 和 meta identity 完全一致。

### R2.28 持久化多 Agent lineage identity

Agent 的 path、nickname、parent lineage 和总量索引目前只存在于 `orchestration/agents/identity/registry.py:62-74` 的进程内字典/集合；ResidencyRecord 只保存 `role_dump/mailbox/msg_buffer`，`orchestration/agents/residency/store.py:44-67` 不含 lineage metadata。rehydrate 时 `orchestration/agents/control.py:821-825` 只能从仍存活的内存 registry 取 path。因此现有 incarnation/residency 能支持同一进程内 eviction，却不能在控制面进程重启后重建 agent tree、相对寻址、nickname 唯一性与 parent/child 广播边界。

实施任务：产品语义已经确认：Agent lineage 必须跨supervisor进程重启持久恢复。以稳定SpawnRequestId和版本化durable spawn状态机替代内存rollback原子性：`requested -> admitted -> lineage_committed -> placement_pending -> incarnation_started -> active | rejected/aborted`。logical agent/root/parent/path/nickname/definition与引用 R2.52 的 budget reservation identity 必须在worker启动前durable commit；重复request返回同一结果或typed conflict。supervisor崩溃后reconciler按durable中间状态继续placement或终止；Role构造/worker启动失败推进lifecycle/attempt，不删除已公开logical identity；未公开reservation只能由当前fenced owner回收。

lineage fact保存不可复用logical Agent ID、root/parent、path/nickname索引revision/tombstone、definition、incarnation、lifecycle、placement和budget identity，并与residency/session恢复对账。path/nickname可重用时必须防ABA；placement、mailbox、budget、delivery与residency record共同验证logical/root/definition/incarnation/generation/revision。worker崩溃只终止incarnation，不能删除logical Agent；旧incarnation失去fence后不得提交状态、结果或delivery ack。

本包拥有 lineage facts、spawn saga 与 placement handoff，不拥有 capacity counter、budget ledger 或 Pool cancellation。它只在同一 spawn transaction 中消费 R2.51/R2.52 的 typed reservation receipt，并向 R2.53 提供 revisioned subtree snapshot；不得建立全能 Agent governance store。

验证与签收：supervisor冷启动可确定性重建十代树及路径/昵称/parent-child/root/subtree索引；spawn每个中间状态崩溃可对账，重复request不产生第二child；拒绝duplicate path、orphan parent、definition mismatch、incarnation rollback和ABA；stale incarnation的commit/result/delivery ack被拒绝；capacity/budget reservation 只通过 typed receipt 关联，lineage store 不复制其余额或 mutable counter。

### R2.29 版本化 FileLeaseCoordinator durable state

`runtime/control/leases.py:62-168` 的跨进程 lease coordinator 把 `subject -> {owner_id, fencing_token, expires_at}` 裸映射直接写为 JSON，没有 schema version、format owner 或 migration boundary；读取时 `137-145` 用 `str/int/float` 宽松强转，布尔、数字字符串或其他错误 shape 可能被静默解释为合法 fencing state。该文件决定跨进程唯一 owner 和 monotonic fencing token，不能按普通缓存处理。

实施任务：增加版本化 envelope、严格字段类型/range 校验、subject 与记录身份一致性、unknown-version fail closed 和显式 migration。不得通过 Python coercion 接受非 canonical token/expiry；损坏状态不能重置为空后重新发放较小 fencing token。

验证与签收：未知版本、布尔 token、数字字符串、NaN/Infinity expiry、负 token、subject mismatch、truncated JSON 全部 fail closed；迁移保持每个 subject 的 fencing token 单调。

### R2.30 拆分固定内部进程与用户命令 runner

`runtime/process.py:12-59` 的 `aexecute(cmd: str, shell=True, sandbox_runtime: Any)` 同时服务模型触发的 Bash 和 Runtime VCS collector。它默认 shell、`shell=False` 时仍用 `cmd.split()`，返回类型随 `wait/return_partial_on_timeout` 改变且未声明，sandbox 通过 `Any.wrap_command` 注入。`product/toolsets/builtin/bash.py:205-228` 在 permission 阶段之后调用它，而 `runtime/vcs/collector.py:69-79` 又以拼接的 `git {args}` 进入同一 shell seam。

实施任务：拆为：

- 只接受 `Sequence[str]` 的固定内部 argv runner，不经 shell expansion；
- 已授权用户 shell intent runner，要求 typed permission/sandbox context；
- 明确的 result DTO 和 timeout/cancellation 语义。
- interactive/daemon process使用独立start/health/stop lifecycle，不借shell/fixed-argv一次性receipt模拟长驻owner；process receipt区分permission denied、spawn failed、exit、timeout、signal和output reference。

任何新命令消费者必须选择其中一个 owner；不得以 `shell` bool 让同一函数跨越信任边界。`SandboxRuntime` 使用窄 Protocol，不能继续为 `Any`。

验证与签收：内部git/status不经shell；用户命令无法绕classifier/permission/approval/sandbox；BackgroundTask、Hook、Workflow、MCP和internal facade分别消费正确typed seam；sandbox缺失/activation失败按Product policy typed unavailable/deny且无unsandboxed fallback；返回类型不随flags形成隐式union；不存在第三个通用subprocess facade。跨入口deny/ask rejected/stale approval/generation mismatch/sandbox failure时fake runner调用次数为零。

### R2.31 让 File search durable codec 严格 fail closed

`contracts/file/codec.py` 的 path/blob/version/mutation decoder 已严格检查 exact shape 和 primitive type，但同文件 `372-402` 的 `search_row_from_dict`、`search_skipped_from_dict` 使用 `.get` 以及 `int/str/bool` 强转，缺字段、数字字符串、任意 truthy 值和未知额外字段可能被解释为合法 SearchRow/SkippedFile。该 codec 实际被 `runtime/fileops/search.py:450-553` 用于持久化和恢复分页搜索 manifest/rows，也被 artifact root 恢复使用，不是临时 provider JSON。

实施任务： search row、skipped item 和 summary 使用同一 canonical exact-shape decoder，严格区分 `bool` 与 `int`，验证非负计数、枚举、可选字段和 path/version identity；若格式跨版本持久，增加 schema version/upcast owner。不得用 primitive constructor 做兼容解析。

验证与签收：额外/缺失字段、字符串 line/count、整数充当 bool、未知 skip reason、负计数和不一致 version/path 均 fail closed；所有 canonical search record round-trip 字节语义稳定。

### R2.32 让交互 Runtime checkpoint 的版本与状态演进可验证

`RuntimeCheckpoint` 虽携带 `schema_version`，但该字段目前没有进入实际 restore 判定：`runtime/interactive/checkpoint_codec.py:15-40` 在写入时固定填 `1`，读取只比较 `checkpoint.codec`；Terminal、Browser 和 Canvas 分别在 `runtime/interactive/terminal/driver.py:789-794`、`runtime/interactive/browser/driver.py:80-102`、`runtime/interactive/canvas/driver.py:66-72` 恢复时也不校验 `schema_version`。Kernel 的 v1/v2 迁移在 `runtime/interactive/kernel/driver.py:1043-1051` 只靠 codec 字符串分支，同样没有统一的版本/shape decoder。除 Canvas 使用 Pydantic DTO 外，Terminal 和 Browser 只验证 payload 是 `dict`，随后用 `.get` 和默认值把缺字段、错类型或未来 shape 解释为可恢复状态。

Durable replay 还有一层状态演进缺口：`runtime/session/events.py:440-473` 对 checkpoint 的整数和字符串字段做宽松 `int/str` 强转；`runtime/projections/session.py:212-217,378-379` 以 `kind:alias` 做 last-write-wins，不校验同一 Runtime 的 epoch/revision 单调性或 runtime_id 稳定；`runtime/agent/session_manager.py:122-124` 随后把该投影直接 stage，`runtime/interactive/host.py:366-378` 也会静默覆盖同 alias 的既有 staged checkpoint。旧/分叉 checkpoint 因而可能遮蔽较新的恢复点，损坏字段也可能在恢复前被兼容强转。

实施任务：

- 为每个 Runtime kind 建立唯一 checkpoint codec registry，显式绑定 codec id、schema version、严格 payload DTO 和有限期 upcaster；unknown codec/version fail closed；
- `RuntimeCheckpoint` envelope 严格验证 runtime/kind/alias、正整数 epoch/revision/schema version、digest、sensitivity 和 fidelity，不使用 `int/str/bool` 做兼容解析；
- Session projection 与 `stage_checkpoint` 对同一 `kind:alias` 校验 runtime_id 不变以及 `(epoch, revision)` 合法演进；重复记录只允许字节/语义幂等，倒退、同版本不同 digest 和 identity replacement 必须拒绝或进入显式 reconciliation；
- Terminal、Browser、Kernel、Canvas 的 restore 都由 registry 返回 typed state，不再各自消费裸 `dict`；历史 v1 兼容只存在于注册的 upcaster，不散落在 driver 分支中。

验证与签收：未知 schema version、缺失/额外字段、字符串整数、错误 env/storage-state shape、revision 倒退、同 revision 不同 digest、alias 下 runtime_id 更换均 fail closed；合法 v1→v2 migration 有 round-trip fixture；replay 顺序不能让旧 checkpoint 覆盖新 checkpoint。

### R2.33 让 Secret vault 多写者更新保持原子且严格

每个 Role 会在 `runtime/agent/components/integrations.py:98-114` 构造自己的 `SecretStore`，但默认共同指向同一用户 vault。命名 secret 上传由 prompt policy 调用 `SecretStore.add_user_secret`（`runtime/prompt/policy.py:180-202`），而 `runtime/secrets/store.py:227-235,399-424` 以“读取整个 vault → 修改一个 section → 固定 `.tmp` 文件 → `os.replace`”完成写入，没有进程内共享锁、跨进程锁、revision 或 compare-and-swap。并发 Role、配置热同步和外部进程更新不同 section 时，两个 read-modify-write 可以相互覆盖；固定临时文件名还会让并发 writer 争用同一 inode。该行为与源码宣称的 section-isolated“不 clobber”语义不一致。

此外，vault 解密后只验证顶层是 mapping；`runtime/secrets/store.py:375-397,439-443` 对缺失/未知 section、非 mapping section 和非字符串 key/value 会静默置空或丢弃。对负责 redaction 与 credential broker 的保护状态，这种宽松恢复会把损坏/未来格式解释成“秘密不存在”。`VaultCipher.decrypt` 的 docstring 仍声称损坏时 fail open（`runtime/secrets/cipher.py:52-58`），与 Store 当前 fail-loud 实现和安全边界要求漂移。

实施任务：

- vault 使用单一版本化 envelope、严格 exact-shape decoder 和 canonical writer；unknown version/section、错误 key/value 类型均 fail closed；
- 所有 section 更新通过同一 scoped lock/CAS transaction owner，锁覆盖 read-decrypt-modify-encrypt-fsync-replace，并使用唯一临时文件；多进程 writer 必须有 fencing 或文件锁，不能只锁一个 `SecretStore` 实例；
- external edit、config/file reseed 与用户上传在冲突时显式重读/重试，保证不同 section 更新不丢失；同一 key 冲突采用声明清楚的 revision/last-writer policy；
- 加密文件与 key 文件写入遵循 durable atomic-write 语义（文件 fsync、目录 fsync、权限验证），文档和异常语义统一为 fail closed。

验证与签收：两个 Role 并发写不同 secret、一个 reseed config 同时另一个上传 secret、两个进程并发首写均不丢更新且不争用固定 tmp；截断、未知版本、额外 section、非字符串 key/value 不会降级为空 vault；崩溃点测试证明旧版本或完整新版本二者之一可恢复。

### R2.34 绑定 Markdown Agent 定义身份与恢复身份

Markdown loader 按可控 `name` 生成固定 `role_type_id = mote.agent.markdown.<name>.v1`，并设置 `replace_role_type_registration = True`（`product/agents/markdown_loader.py:83-86`）；每次发现同名定义都会覆盖 Runtime 进程全局 `_ROLE_REGISTRY`（`runtime/agent/base.py:18-49`）。虽然 loader 计算了包含 metadata 与 instruction 的 `definition_version`（`product/agents/markdown_loader.py:122-127`），该 digest 没进入 `role_type_id`、Session meta 或 Residency blueprint 的持久身份。跨项目同名定义或同一文件修改后，旧 session 仍只比较相同固定 type id（`runtime/agent/session_manager.py:144-170`），无法发现实际 instruction/tools/model 已改变。

恢复还有状态吞没：`Role.incarnation_blueprint()` 从 snapshot 构造 `role_schema/state` 并传给具体类（`runtime/agent/role.py:1371-1393`），而动态 `_MarkdownAgent.__init__` 只显式接收 parent/wiring/config，将其余参数收入 `**_ignored`，随后总是按当前 Markdown 新建 `RoleSchema` 与 `RoleState`（`product/agents/markdown_loader.py:91-120`）。因此 Residency replacement 可能通过 type-id 检查，却丢失 snapshot schema/state，并把旧 incarnation 解释成当前文件定义。

实施任务：

- 持久定义身份绑定 source identity、canonical definition digest 和明确 schema version；Session meta、Residency snapshot/blueprint 与 catalog definition 使用同一 identity；
- 动态定义不得用可控名称无条件替换进程全局 role registry；若保留 registry，注册 key 必须内容寻址且 Application scoped，或改由显式 restorer catalog 管理；
- restorer 必须消费 snapshot 的 `RoleSchema`/`RoleState`，不能用 `**_ignored` 吞掉 canonical restore 参数；definition 只提供构造约束，并验证 snapshot 与批准 definition 相容；
- 定义修改、删除、source replacement 或 catalog 不可用时 fail closed，只有显式、版本化 migration 可以恢复旧 identity；不得静默回退到同名新定义。

验证与签收：两个项目存在同名 Markdown Agent 不会互相覆盖 registry；修改 instruction/tools/model 后旧 session/residency 拒绝按新定义恢复；合法 eviction round-trip 保留全部 schema/state；definition 删除或 digest mismatch 保留恢复证据并返回明确错误。

### R2.35 统一 AgentCatalog 编译、命名空间与版本算法

`AgentCatalog.from_types`、`with_types` 与 `builtin_agent_catalog` 形成三条 snapshot 编译路径。`from_types` 只分别检查 canonical name 重复和 alias-alias 重复，没有检查 alias 与另一 canonical name 冲突（`product/agents/catalog.py:34-50`），而 `get()` 顺序扫描 name/aliases（`68-71`），冲突解析取决于排序。`with_types` 改用统一 owners map 检查 name/alias，但版本只哈希 `definition.name:definition.version`（`94-118`）；`from_types` 却额外哈希 `inspect.getsource`（`121-133`）。`builtin_agent_catalog` 再绕过两者，直接拼接两个 version 字符串与私有 `_definitions`（`product/agents/discovery.py:12-27`），也只用 Python canonical names 过滤 Markdown definitions，未复用完整 alias namespace 校验。等价 definitions 因入口不同得到不同 version，冲突集合也可能被某条入口接受。

实施任务：建立唯一 canonical catalog compiler，以结构化 canonical fields 一次性完成 name/alias 全命名空间校验、稳定排序、definition identity 和 snapshot version 计算；`from_types`、增量组合和 builtin discovery 只能投影声明并薄委托该 compiler。版本不得依赖运行环境中的 `inspect.getsource` 可用性，也不得用字符串拼接多个子版本代替整体内容摘要；禁止调用方直接构造 `_definitions`。

验证与签收：任一 name-name、alias-alias、name-alias 冲突在所有入口一致拒绝；同一 definitions 集合不受输入顺序、构建入口或 source inspection 可用性影响，version 完全一致；任一 canonical identity 字段变化必然改变 version；lookup 不依赖遍历先后决定 owner。

### R2.36 统一 Tool lifecycle 的稳定关联 identity

Presentation event 允许 `ToolCallStarted` 与 `ToolCallCompleted` 缺少 `tool_use_id`。ACP fallback 为每个事件递增 block counter（`product/interfaces/acp/wire.py:157-167`），start 和 completion 是不同事件，因而分别获得不同 id；完成事件会被当作未见过的独立 call 再 promote（同文件 `340-374`）。AG-UI fallback 更直接使用 `id(e)`（`product/interfaces/agui/wire.py:248-271`），同一次调用的 start/completion 必然使用不同对象身份，且该值不稳定、不可重放。远端 UI 因此可能留下永久 in-progress call，并产生孤立 completion/result；file diff/media 等后续块也无法可靠归属。

实施任务：稳定invocation/EffectId必须在Tool execution owner创建，并贯穿definition/catalog generation、canonical args digest、permission target、approval/sandbox、intent、started/progress、artifact/diff/media、attempt receipt、audit与completed/failed事件；Presentation/transport只投影，不自行mint。若内部operation不是模型tool call，使用独立稳定operation variant，不能以`None`触发位置/对象身份关联。

ToolExecutor保持唯一执行chokepoint，不能因依赖多而拆散。外部只获得immutable ToolBindingSnapshot、typed execute command/receipt和必要query，不能取得tool instance、live mutable catalog/map或pipeline stage后直接调用绕过permission/effect pipeline。runtime-discovered definition在执行前绑定catalog generation和permission identity；reload后旧turn继续使用旧snapshot，新turn使用新generation。MCP/optional activation失败不得修改基础catalog或扩大capability。

验证与签收：同一调用的definition→permission→intent→execute→receipt→audit及ACP/AG-UI/Textual/replay使用同一logical identity和attempt ordinal；并发同名调用不串联；缺失id在transport前fail fast或明确非-tool variant；禁止`id(event)`、counter或列表位置。所有普通Tool/Workflow/BackgroundTask/Hook路径只能经ToolExecutor或其正式effect seam执行，无法从public API取得实例绕过pipeline。

### R2.37 让 WirePermit epoch 连接真实代际状态

WirePermit 已把 `backup_epoch/admission_epoch` 纳入签名 contract，Runtime 也会用 epoch provider 校验二者（`contracts/inference/wire_permit.py:17-59`、`runtime/inference/command_runtime.py:480-504`、`runtime/inference/session_runtime.py:710-738`）。但 public inference compatibility composition 为 command、response、session 和 artifact transfer gateway 全部硬编码 `epoch_provider=lambda: (0, 0)`（`product/interfaces/inference_api/composition.py:140-180`）。该入口签发与验证的 permit 永远处于零代，failover/admission 状态变化不能撤销旧授权，也无法把一次 wire admission 与当前 generation/backup ownership 隔离。

实施任务：Application/Runtime lease 暴露只读、原子的一致 epoch snapshot port，所有 embedded/shared/public gateway 的 issuer 与 verifier 使用同一 canonical owner；backup 切换、admission policy revision、runtime replacement 时单调推进相应 epoch。Shared execution permit 还必须消费 R2.54 owner record，并与 R0.5 使用同一 principal/generation/execution/epoch verifier。epoch 不可用时拒绝构造需要 permit 的 compatibility owner，不得回退常量。

验证与签收：backup/admission epoch 变化后旧 permit 在 command、session 与 transfer 全部拒绝；同一 request 只读取一次一致 epoch pair；embedded 与 shared daemon 行为一致；生产 composition 不存在 `(0, 0)` 常量 provider。

### R2.38 让 ModelGateway 的有限操作能力进入路由准入

canonical invocation 已声明 `embedding`、`image_generation`、`speech` 和 `transcription`（`contracts/model/invocation.py:19-24,81-103,181-199`），Product 也按 transport family 构造 OpenAI/Google finite transport（`product/models/runtime_generation.py:607-671`）。但 endpoint、route 与 failover capability contract 只表达 tools、schema、server web search、vision、PDF 和 native tool search（`contracts/model/topology.py:36-43`、`contracts/model/failover.py:231-238`、`contracts/model/routing.py:49-56`）；`FailoverPlanner._missing_capabilities` 也只检查这些能力（`runtime/models/failover/planner.py:128-158`）。结果是 chat-only endpoint 可以通过 embedding/image/speech/transcription 的 planning，直到 provider wire 才失败，并可能继续切换到同样不支持该 operation 的 endpoint。

实施任务：以 canonical `ModelOperation` 集合表达 endpoint 支持的有限操作，并使 config、topology、`EndpointDescriptor`、route admission、failover planner 和 transport registry 共享同一来源；未声明支持的 operation 必须在任何 wire、副作用或重试前 fail closed。Product 只为 endpoint 声明支持且存在真实 adapter 的 operation 建立 binding，异构 group 按本次 operation 过滤。

验证与签收：chat-only endpoint 对 embedding、image generation、speech 和 transcription 在 planning 期拒绝；failover plan 只包含支持本次 operation 的 endpoint；声明集合与实际 transport registry 可自动比对且双向无漂移；错误 operation 不消耗 wire attempt。媒体能力说明必须保持真实边界：图片生成、语音与转录已进入 ModelGateway，GenerateMedia 的图片/音频/音乐/视频仍走 hosted `ServiceGateway`，音乐和视频不能被宣称已统一进 ModelGateway。

### R2.39 让 failover route profile 表达真实兼容集合

`ModelGateway.route_profile()` 固定返回 failover group 的第一个 endpoint（`runtime/models/model_gateway.py:1237-1251`），`ModelRoute.profile` 也只能保存一个 `EndpointDescriptor`（`contracts/ports/model/gateway.py:50-60`）。`RuntimeModelInferencePort.pin_route()` 随后只用这个首 endpoint 决定 command protocol、schema/multimodal capability fingerprint 与 projection compatibility（`runtime/models/inference_port.py:138-174`），而实际 execute 会按 `FailoverPlan` 在多个 endpoint 间切换（`runtime/models/model_gateway.py:722-738`）。因此异构 group 的宣告能力和投影协议由排序第一项决定，实际 wire 却可能落到另一套 tool envelope、schema dialect 或 multimodal 能力上。

实施任务：route pin 必须绑定具体 endpoint，或编译出对本次 request 在 protocol 与 projection 上一致的 endpoint subset；group-level profile 应表达可证明的 intersection/coherent variants，不能以首 endpoint 冒充整组。Failover 不得跨 command protocol、structured-output dialect、multimodal envelope、tool-name/shape limits、canonicalization version 或其他 projection compatibility boundary。

验证与签收：native/XML 混合组在 pin/planning 时拆分或拒绝；切换 endpoint 后 target capability fingerprint 与真实 endpoint 保持一致；schema、vision/PDF、tool search 和有限 operation 不跨不兼容 endpoint；改变 endpoint 排序不会改变 route 宣告能力或投影协议。

### R2.40 统一 Tool definition、catalog 与 generation identity

运行时 snapshot 从 bound tool 读取不存在的 `definition_version`，缺失时统一回退 `"1"`，并把 catalog identity 和 provider descriptor 固定为 `runtime-tools@1`（`runtime/tools/snapshots.py:44-65,82-104`）；实际 `BoundTool` 和 `XmlToolDefinition/NativeToolDefinition` 没有 semantic identity/version 字段（`runtime/tools/tool_binding.py:19-32`、`runtime/tools/provider_definitions.py:32-114`）。静态 `ToolCatalog` 则使用 `inspect.getsource` 加可选 class 属性计算另一套 version（`runtime/tools/tool_registry.py:39-58,80-92`），MCP toolset 默认也永远是 version `1`（`runtime/tools/mcp/toolsets.py:10-32`）。这使 materialized definition identity 普遍退化为 `<name>@1`：schema、description、effect、alias、approval 或 MCP discovery 内容变化后，durable snapshot 仍可能把旧定义当成新定义。

实施任务：建立唯一 canonical Tool definition compiler，由结构化字段计算 authoritative semantic identity；字段至少覆盖 protocol、canonical name/aliases、description、input schema、execution/effect、approval semantics 与 source/plugin/MCP identity。`BoundTool` 显式转发 definition identity；静态 catalog、materialized snapshot 与 MCP discovery 都委托同一 compiler，不依赖 `inspect.getsource` 可用性。区分 definition semantic identity、catalog content fingerprint 与单调 generation，但三者必须来自同一 canonical definitions 集合；不得固定 `@1` 或读取未声明属性。

验证与签收：schema、description、effect、alias、approval或 source 改变时相应 identity/version 必须变化；输入顺序、进程和 source inspection 可用性不影响 identity；MCP tool 列表或 definition 变化推进 catalog generation；resume/snapshot 拒绝用旧 definition identity 绑定新工具；生产 snapshot 不再普遍出现 `<name>@1`。

### R2.41 类型化 GenerationArtifact 的各 domain binding

`GenerationArtifact` 虽有固定 `schema_version=1`，但 model/service/session/transfer bindings、pricing snapshot 与 activation policy 仍是多组 `dict[str, Any]`（`contracts/inference/generation_artifact.py:8-30`）。Product 当前只填入若干约定字符串和布尔字段（`product/models/runtime_generation.py:722-750`），shared RPC、SQLite restore 与 Runtime generation 再按整个宽字典接收。顶层版本无法约束内部字段集合、variant 与演进，digest 修复后仍只能证明一份动态 payload 未改变，不能证明其满足可执行 contract。

实施任务：为每个 domain 建立 Contracts-owned、封闭且版本化的 binding DTO/tagged union，显式表达 deployment/runtime kind、topology/catalog revision、credential/capability binding 与 activation policy；`GenerationArtifact` 只组合这些 typed views。unknown domain variant/version/extra field fail closed，migration 只进入注册 upcaster；Product compiler、shared daemon stage、SQLite restore 与 Runtime activation 使用同一 codec。

验证与签收：任一 domain 缺失/额外字段、错误 primitive、未知 variant/version 在 stage 和 restore 前拒绝；合法 artifact canonical round-trip 保持 typed identity；新增 domain 版本不能被旧 reader 当作普通 dict 接受；content digest 覆盖每个 typed binding。

### R2.42 收敛唯一 Product application composition root

`product/entrypoints/cli/bootstrap.py::build_engine` 同时承担首次文件播种、locale、cwd、配置加载、同步 `asyncio.run` 和 Application object graph 构造，是 CLI bootstrap；`product/composition/bootstrap.py::build_application` 位于正确 owner 包但当前只是薄构造函数。CLI `__main__` 和 startup tests 直接消费前者，而 gateway/daemon/SDK 需要与 presentation 无关的 canonical application factory。

实施任务：统一factory前先形成authoritative scope/lifecycle matrix，逐对象记录application/process/session/Agent/incarnation/turn scope、construct/activate/shutdown owner、能否跨session共享/跨incarnation继承、durability与required Ports。每个对象只有一个上级scope owner；低scope不能关闭共享高scope资源，高scope不能缓存低scope mutable state。

canonical composition分为typed declaration/validated config、pure object graph construction、ordered async activation和reverse settlement。文件播种/创建、secret resolution、provider/network client、watcher、daemon、process和journal open只在显式activation发生；任一步失败按已完成阶段逆序关闭并返回typed result，不留下half-active Application。CLI只在最外层拥有一次event-loop boundary；canonical factory不得内嵌`asyncio.run()`。

在`product/composition/`收敛唯一Product application factory；CLI仅保留source、locale、cwd和presentation adaptation，所有host通过同一typed request/result构造Application。迁移完成后删除CLI中迁出的对象图构造和同步activation旁路。integration/entrypoint测试调用同一factory并注入fake Ports/backend；unit测试可直接构造bounded-context内部对象，但测试便利不得迫使生产API暴露store/registry或wide optional参数。

验证与签收：生产代码只有一个Product application factory；scope matrix覆盖全部owned资源；CLI、gateway、daemon、SDK与session hosting的declaration、required Port、scope和lifecycle一致；逐activation阶段fault injection证明逆序settlement和无half-active状态；architecture gate扫描已确认constructor callsites和test bootstrap allowlist。hermetic gate不加载TUI/PTY/provider optional backend，CLI bootstrap不再成为其他host依赖。本包不实现或临时预留 hot reload。

### R2.49 闭合 Product application hot reload、generation swap 与 drain

application hot reload 依赖 canonical extension trust、MCP/catalog atomic generation、Agent/Tool definition identity 与唯一 composition lifecycle；把它留在最早 M0 composition 基线，会迫使实现复制尚未完成的 trust/catalog seam。

实施任务：由 Product composition generation owner 在 R2.42 的 application lifecycle 上构造 candidate，并复用 R1.9 trust/approval、R1.14 MCP generation swap、R2.35 Agent identity 与 R2.40 Tool identity。candidate 完整验证且 capability 不扩大后才 atomic swap；旧 generation 由 lease holder drain，新 session/turn 取得新代，同一 turn 不混代。source、permission 或 content identity 改变必须重新 trust decision；失败保留完整旧代，不原地 mutate registry/config。明确 watcher/reload trigger、debounce、generation identity、holder acquire/release、shutdown 与 Product composition owner，不在 Runtime 建第二 catalog。

验证与签收：并发 reload/turn/session 下 holder 代际一致；失败 candidate、trust 拒绝或 capability 扩大均不改变 active generation；旧 holder drain 后才释放资源，stale reload 不能覆盖新代；陌生 checkout 变化不自动激活；生产只有一个 reload trigger、candidate compiler 与 swap owner。

### R2.50 建立 Agent durable turn queue 与公平 scheduler

R2.16 只能保证取得 permit 的 turn 不超并发上限；无 permit runtime 当前仍可能无限 park，且 supervisor restart、wake 丢失、root fan-out、公平与背压没有 durable owner。现有 admission queue、mailbox、Cron scheduler、Workflow reconciler 是否能复用其核心 identity、durability和公平语义尚未完成审计，不能直接复制或强行复用。

复用审计结论：`runtime/inference/fair_queue.py::FairAdmissionQueue` 已实现 bounded hierarchical DRR、priority/deadline 与 FIFO，可复用其纯算法不变量、确定性测试模型及未来可抽取的 owner-neutral scheduling value，但其 identity 是 inference tenant/project、状态仅进程内、cost/priority aging 与 ADR-D5 不同，不能复用 concrete queue/store。`orchestration/agents/messaging/pending.py` 是 process-local delivery wake/buffer，R1.13 将其降为 durable delivery 的优化投影，不是 turn admission queue；Cron scheduler 按 wall-clock occurrence 推进独立状态机；Workflow reconciler 按 run/effect frontier 扫描。三者生命周期和 settlement 均不同。R2.50 因而允许在 Orchestration Agent bounded context 新增 durable turn-queue state，但必须复用 R2.29/R2.46 的 lease/clock mechanism、R2.16 permit 与 R2.20 mailbox identity，不新增通用 queue facade或第二 mailbox。

产品决定（ADR-D5）已确认：Agent turn scheduler 使用分层 weighted deficit round-robin。当前 Agent governance 没有独立 tenant identity；inference domain 的 tenant identity 不得越层借用，因此本阶段 `tenant == root governance owner`，不存在含糊的第三层。若未来出现独立 Agent tenant identity，必须版本化升级为 tenant WDRR → root WDRR → subtree WDRR，而不是原地改变 root 含义。

- root 间使用 WDRR；每个 eligible turn 的 scheduling cost 固定为 1，不预测 Token cost。默认 root weight 为 1；weight 是 Product schema约束的有界正整数并受全局上限约束，Runtime/extension只能保持或收窄，不能提高。
- 持续有可用 execution capacity 时，每个持续 eligible root 必须在由 active roots、subtrees 与有界 weight 推导的有限调度轮次内取得 claim；高权重只改变份额，不能饿死低权重 root。
- priority 只在同一 root 内排序，使用有界 enum/range；同 priority 按 durable enqueue sequence 稳定 FIFO。若一个 root 内有两个及以上持续积压的 subtree，先以第二级 subtree WDRR 选择 subtree，再在该 subtree 内按 priority/FIFO 选 turn，单个 subtree 不得饿死兄弟 subtree。
- deadline 只作用于尚未 claim 的 item，以 expected revision CAS 转为 typed `EXPIRED`；deadline、cancel、claim 竞争只能提交一个终态。已 claim turn 的超时由 execution settlement owner处理，queue不得回写 `EXPIRED`。
- queue capacity admission 在 durable accept 前原子完成；满队列返回 typed `BACKPRESSURED` 或 `REJECTED_CAPACITY`，不得写 accepted record，也不得驱逐已经 durable accepted 的 item。
- claim 同时绑定 queue revision、scheduler fencing token 与 R2.16 execution permit；scan/peek 不代表取得执行权。stale scheduler不能 claim、ack、settle、retry或释放新 owner 的 permit。
- retry record包含 `next_eligible_at`、有界 attempt/backoff 与 terminal disposition；未到时间或 poison item不占 root/subtree eligible队首，不形成 head-of-line blocking。
- root cancel、terminal或配置删除必须把已 accepted item逐个推进到 typed settlement；配置缺失不能制造 ownerless record。weight热更新发布新 config generation，只影响下一次尚未 claim 的调度决策，不重写 acceptance、enqueue sequence、deficit history或已有 claim。

实施任务：先搜索并记录现有 queue/scheduler/lease/store 的复用结论，尤其对照 `runtime/inference/fair_queue.py` 的 tenant/project DRR不变量；只有 identity、scope、durability、lifecycle 与失败语义一致的 primitive才可抽取复用，禁止让 Agent scheduler依赖Inference concrete queue或复制改名。固定 queue item、root/subtree、enqueue sequence、config generation、revision、deadline、retry、claim、R2.16 permit与fence contract；durable scan是发现机制，WDRR claim owner才拥有调度决定。

验证与签收：确定性 fake clock/barrier覆盖高权重root不饿死低权重root、单root priority flood不越过其他root、兄弟subtree有限公平、同priority durable FIFO、deadline/cancel/claim三方CAS、queue-full无accepted、accepted不驱逐、restart/wake丢失后的scan、stale fence不能claim/ack/settle、config generation切换保持ownership，以及poison/retry不阻塞后续eligible item。基础设施复用证据证明未建立第二 mailbox、Workflow scheduler、Inference耦合queue或通用万能queue。

### R2.51 闭合 Agent logical、resident 与 turn capacity projections

logical identity cap、resident incarnation cap 与 concurrent turn cap 的 identity、占用及释放事实不同，不能继续由含义模糊的 `max_agents` 或 lineage store 内部计数共同表达。

实施任务：定义三类 typed capacity reservation/settlement receipt：logical cap 按 root/tree/application 计数，logical Agent 进入 terminal 后立即且严格一次释放 active slot，但 AgentId 永不复用；tombstone 继续保留，只有 Product retention 到期且 delivery/effect/pin/legal hold 全部结算后才由当前 fenced owner purge。resident cap 绑定 incarnation，只有 R1.20 完成 eviction settlement 后释放；turn cap 绑定 R2.16 permit。fan-out、subtree、root total 使用 durable projection/CAS 并与 R2.28 spawn admission 原子关联；projection 可由 canonical facts 重建，但 admission 必须依赖 committed revision，不读 telemetry。删除一个配置值投给多类 cap 的 alias。

验证与签收：terminal、eviction 与 turn settlement 只释放各自 slot；并发 fan-out/root/subtree admission 不超限，重复 settle 不减两次；projection crash/rebuild 与 stale revision fail closed；lineage、Residency 和 limiter 只交换 typed receipt，不共享 mutable counter。

### R2.52 建立 Agent Token、成本、深度与能力预算 ledger

预算具有 reservation、actual settlement、refund、root/subtree 聚合与 owner-crash reconciliation，不等同于 capacity counter，也不能以 telemetry/cost display 作为真相源。

复用审计结论：`contracts/ports/inference/usage_ledger.py::UsageLedger`、`contracts/inference/governance.py::BudgetReservation/UsageSettlement` 已是 canonical reservation/settlement contract；`product/inference/backends/sqlite.py::SQLiteUsageLedger` 已拥有 tenant/project budget、reserve、settle、release、pending reconciliation、fenced reconcile 与 expiry reclaim，并被 Runtime inference/session/command runtime 和 Shared daemon composition 实际消费。Agent 治理不得新建第二 budget ledger；应扩展该 contract 的 typed budget dimension/subject projection，或在 Orchestration 通过窄 adapter 消费同一 ledger。provider quota observation 是外部证据，不是预算余额；FileOps byte reservation 生命周期不同，不复用。

实施任务：以现有 UsageLedger 为唯一 usage/budget truth，补齐 Agent/root/subtree、Token/成本/深度/能力的版本化 subject/dimension 与 immutable policy input；必要扩展必须保持现有 inference consumer 的 reserve/settle/reconcile 关系，不复制 SQLite table/store。reservation 与 spawn/turn/tool/LLM admission 原子关联；并发 child 不能基于旧 subtree total 同时超额，unknown cost 不按零继续。extension 只能单调收窄 parent 授权；owner crash由 lease/reconcile 处理未结算 reservation。Product composition 选择同一 ledger implementation，Orchestration 只拥有治理 policy 与 typed reservation coordination。

验证与签收：并发 reserve、actual settle、refund、重复请求、lease loss 与 crash recovery 不超额、不双退；Token/成本/深度/能力各自单位和 disposition 明确；预算 ledger 能与 provider usage/effect receipt 对账，telemetry 全丢不影响 admission。

### R2.53 闭合 Agent subtree cancellation epoch 与逐 Agent settlement

子树取消需要稳定 lineage snapshot 和单调 cancellation epoch，但 supervisor 不得因此拥有各 Agent BackgroundTaskPool 或 runtime mutable state。

实施任务：R2.28 lineage owner 提供 revisioned subtree snapshot；cancellation owner 以 fenced epoch 与 spawn admission 原子协调，epoch 生效后不得漏出新 child。supervisor 只向每个 Agent owner发送 typed idempotent cancel command；BackgroundTask 通过 R1.26 drain/cancel Port，turn 通过 R2.16/R2.50，resident lifecycle 通过 R1.20。聚合结果逐 Agent区分 `settled/already_terminal/owner_lost/timeout`，不退化为 bool；重试复用同一 epoch。

验证与签收：并发 spawn/cancel 不漏 child；重复 cancel 幂等，stale epoch 不能取消新 subtree generation；running、draining、evicted、lost 与 timeout 均产生逐 Agent settlement；supervisor 不读取/修改 Pool task map、runtime registry 或 budget ledger 内部状态。

### R2.54 固定 Shared execution identity、variant 与 owner-record contract

R0.5 的对象级授权需要正式 execution identity、finite/session variant、owner principal、credential/application scope、generation、artifact digest 与 permit epoch binding；这些字段目前埋在 R2.26 的宽 `Any` registry/反射 dispatch 中。先在动态 registry 上补授权再类型化会遗漏 variant/RPC，并违反 contract-first 顺序。

实施任务：在 Contracts 定义封闭 execution variant、稳定 ExecutionId、不可变 owner record、typed lookup/verify input 与 disposition；contract 只表达授权和生命周期所需事实，不包含 protobuf、backend object、journal layout 或 Product credential实现。明确 record revision、generation/epoch、principal/application scope、artifact identity 与 finite/session 合法命令矩阵。R0.5、R2.26、R2.37 必须共同消费同一 verifier contract，不能各自复制字段比较。

验证与签收：finite/session 不合法命令在静态类型或严格 decoder 拒绝；owner record canonical round-trip，unknown variant/version/extra field fail closed；principal、generation、artifact 或 epoch 任一不匹配均得到 typed denial；Contracts 不依赖 Product/protobuf，仓内只有一个 execution owner-record 定义。

### R2.43 将 Session read model 迁入 canonical Session owner

`runtime/projections/session.py::SessionProjectionState/SessionLiveProjection` 消费 Session event stream，拥有 replay/live sequence、subscription lifecycle 和 Session snapshot；`runtime/session/replay.py`、Agent session component/key/accessor、Product event governance 和 session tests 都直接依赖它。它与 `RuntimeProjectionRegistry/RuntimeProjectionReconciler` 的 checkpoint→artifact publication→ack/retry/dead-letter 管线不是同一 bounded context，当前目录形成错误 owner。

实施任务：把 Session read model 迁入 `runtime/session/`，迁移 replay、Agent component/key/accessor、Product governance 和测试；删除旧 `runtime.projections.session` module/package export，不保留 alias、第二 reducer 或第二 registry。通用 artifact projection/reconciliation owner及 Canvas/Notebook projector 保持内聚。

durable subscription使用stable subscription identity、durable cursor/checkpoint和EffectId，顺序为读取committed envelope→pure reducer或durable intent包裹的effect→commit projection/receipt→最后ack。ack前崩溃允许幂等重投；checkpoint ahead、gap、wrong stream/generation fail closed；poison进入typed dead-letter/quarantine并可reconcile；best-effort wake丢失后scan仍可发现。subscriber不得直接修改owner内部map绕过command。

验证与签收：Session replay 与 live subscription 使用同一个 authoritative reducer/state；通用 artifact projection registry 不注册或拥有 Session read model；生产与测试消费者均从 `runtime/session` 正式面导入；旧 module/export/component identity 消费者归零。

### R2.44 保持 observation telemetry 的 EventT 与控制面隔离

`TelemetryEmitter.emit(event: object)`、`EventNarrower(object)`和Kernel observer宽callback会在内部链路擦除已知event类型，并允许event bus继续混合observation、audit和control语义。

实施任务：每个domain subscription binding保持`EventT`从typed emitter到typed handler；异构存储所需erasure仅封装在Runtime owner私有层，并由构造时验证的binding恢复关系，不建立全仓`ObservationEvent` union或运行时TypeGuard猜型。Kernel只通过窄注入seam发Kernel-owned observation event，Runtime adapter映射，Kernel不依赖Runtime bus。

建立event family分类矩阵，逐项声明authoritative source、durability、delivery guarantee、consumer side-effect policy、replay/idempotency和retention。control command返回typed receipt；audit从已commit authoritative fact投影；observation可有界drop/coalesce但不能推进state或成为permission/budget/effect/delivery/lifecycle的唯一事实。subscriber不得直接改owner mutable map。

验证与签收：EventT由Pyright贯穿emitter/binding/handler；私有erasure不能接受错误binding；observation全丢不影响control/reconcile；stale callback不能修改新generation；Kernel import不依赖Runtime event实现。

### R2.45 闭合 Prompt、cache、compaction 与 generation identity

实施任务：`SYSTEM_PROMPT_DYNAMIC_BOUNDARY`上方保持byte-stable，无placeholder/cwd/time/git/memory/token/bg/LSP等运行态内容；每个dynamic turn-context source使用R2.8的typed name/priority/dependencies/suppression/render，易变信息只进入user prompt system-reminder，不进入static prefix或durable conversation history。

cache identity绑定最终system prefix、tool definitions、command protocol、model/provider capabilities、output schema与policy generation；不得依赖对象地址、import order或`inspect.getsource`。provider cache marker只能在final wire shape确定后加入并参与失效。reload后旧in-flight turn保持原snapshot，新turn使用新generation，同一turn不混model/tool/prompt/policy generation。

compaction summary只是derived context，不是用户输入、Tool/LLM output、approval、budget、lineage、terminal result、task dependency或effect receipt的authoritative state。这些事实先commit到各自canonical store，summary引用typed artifact/reference。只有声明reconstructable、可从canonical state无LLM/费用/时变调用/副作用确定性重建的结果才允许折叠；reprojection绑定source revision/generation。Prompt文案不能承担durability、permission或cleanup保证。

secret redaction覆盖prompt render、cache metadata、log/telemetry、summary、dead-letter、exception、artifact preview和snapshot，但不得修改canonical audit fact。

验证与签收：在旧turn构建prefix后reload、subscriber replay、compaction reprojection、Agent rehydrate和stale callback设置barrier，证明generation一致；cache semantic字段任一变化均失效；不可重建fact不被摘要替代；canary secret不出现在任何derived/observation surface。

### R2.46 建立跨 durable domain 的 clock contract

Workflow deadline、Cron occurrence、lease expiry 与 cleanup/retention 同时依赖时间，但 wall clock、process elapsed time、时区转换和持久化 timestamp 若由各 owner 自行解释，会在进程重启、NTP 回拨/跃进与 DST fold/gap 时产生旧 lease 复活、Cron 重复触发或 retention 提前删除。Clock contract 只统一时间语义与可测试 source，不接管各 domain 的状态机、store 或 policy。

实施任务：先只读审计 Workflow、Cron、lease、Residency、Hosted Service、queue 与 cleanup/retention 的时间字段，把它们作为 contract evidence source，而不是 R2.46 的实施前置。固定 timezone-aware absolute instant DTO、monotonic process-local source 与 schema/clock identity规则；禁止持久化或跨进程比较 monotonic 值。各 domain 后续在自己的工作包内定义 rollback/jump、restart、DST ambiguous/nonexistent local time、catch-up 与 overdue disposition，并反向依赖本包；R2.46 不拥有 Cron occurrence、Workflow deadline、lease expiry 或 retention policy。

不得通过修改系统时间、真实 `sleep`、mtime 或进程启动时刻测试/推导 durable 语义。复用一个 Contracts-owned 窄 clock source Port，由 Product composition 注入 production/fake implementation；domain owner 仍各自拥有 deadline、schedule、lease 与 retention policy，禁止建立万能 timer manager 或第二 scheduler。

验证与签收：使用 deterministic fake clock 覆盖进程重启、wall clock 回拨/前跳、monotonic 前进、DST fold/gap 和旧 generation 并发提交；证明旧 lease 不复活、同一 Cron occurrence 不重复、Workflow deadline 不因重启延长、retention 不提前，且 clock source 丢失或 record identity/version 不匹配时 fail closed。

## 7. P3：导包、兼容入口和命名

### R3.1 清零错误 owner re-export

实施任务：先逐symbol区分owner：稳定cross-boundary code/context/DTO归对应Contracts domain；provider/transport normalization归具体Runtime adapter；retryability来自authoritative typed error/disposition而非字符串/global tuple；recovery归拥有重试/补偿状态机的bounded context；human-facing render归Product presenter。将Product/Orchestration消费者迁到authoritative module，归零后删除`runtime.errors`聚合/re-export，不保留alias。

验证与签收：生产与测试从语义owner导入；Runtime facade不再导出Contracts error；definition/classification/recovery/presentation各层依赖正确；旧 re-export/alias 消费者归零且跨层捕获行为保持。本包不预设或实施 durable ErrorCode 协议迁移。

### R3.2 清理 facade 与 registry 兼容入口

实施任务：清理 provider catalog、OAuth registry 等无生产价值或仅内部二次转发的 re-export；`product.models.BaseLLM` 不得成为 Runtime 模型抽象的第二 canonical API。

Product package facade 不得 eager import TUI、PTY、provider SDK 或 optional backend；缺少 optional dependency 时，静态治理测试和核心 package import 仍须可运行。

同时清理以下已确认漂移：

- `runtime/context/turn/format.py` 仅为 `wrap_system_reminder` 保留的 historical import shim；
- OAuth error 的全局 Runtime facade 与 OAuth-local facade 双入口，权威定义仍为 `runtime/errors/oauth.py`；
- `runtime/sandbox/__init__.py` 声称公开但实际不存在的模块级 `wrap_command`。
- `runtime/interactive/__init__.py` 为获取 checkpoint store、host 或 handoff 就 eager import Chromium backend；基础 Agent component 因而被可选浏览器依赖污染；
- `product/presentation/projection/__init__.py` 明示为 existing callers 保留，但生产消费者均从 defining modules 导入，属于已归零的历史 re-export facade。

迁移消费者后直接删除 shim 或错误声明，不新增替代 alias。

验证与签收：逐个 facade 记录生产/测试/外部支持证据，迁移后旧入口消费者归零且文件/export 删除；核心 package import 在缺少 TUI、PTY、Chromium 和 provider SDK 时成功；不存在替代 alias 或第二 registry/API。

### R3.3 消除同层同名认知冲突

以下类型不是双真相，但应按职责重命名以降低误导：

- live executable `BoundTool` 与 pinned invocation `BoundTool`；

实施任务：按 live executable 与 pinned invocation 的真实职责重命名两个 `BoundTool`，迁移全部消费者后删除旧名，不保留内部兼容 alias。

验证与签收：两个类型名称、owner 和生命周期可从 import 直接区分；执行、snapshot/replay 与 presentation 消费者引用正确类型；生产源码没有旧名 alias、错误转换或基于同名的反射判断。

### R3.4 清理失效兼容承诺与已结束迁移残留

以下接口/承诺必须按 consumer evidence 处理：

- Contracts/Kernel 中仍引用已删除 `common` owner 的注释、`contracts/tool/errors.py` 声称存在但已失效的 Runtime re-export、provider/OAuth registry re-export 承诺；
- `contracts/conversation/fields.py::USE_ENCODED_MEDIA` 保留拼写错误的历史兼容字符串，需以外部/持久消费者证据决定迁移或删除。
- `contracts/file/__init__.py` 对 `ContentIdentity` 的跨 bounded-context re-export 无生产消费者；File 内部消费者迁到 `contracts.content.identity` defining module 后删除该入口。
- `runtime/code_map/model.py` 的模块说明声称 `extractor` 为 backwards compatibility re-export 所有 neutral model，但 `runtime/code_map/extractor.py:122` 实际 `__all__` 只有 `CodeMapExtractor`，且生产消费者均已从 `model.py` 导入；删除失效承诺，不补建兼容 re-export。
- `kernel/tools/definitions.py::ToolDefinition` 与 `kernel/tools/catalog.py::ToolCatalog` 没有生产消费者；真实装配使用 Runtime provider definitions/catalog 和 Contracts materialized catalog。删除这组平行 canonical source，保留仍有消费者的 `kernel.tools.docstrings/spec_adapter`，不得为死类型新增 facade 或转换器。
- `product/config/watcher.py::ConfigWatcher` 无生产消费者；真实 hot reload 经 `runtime/watching`、`runtime/agent/components/watching.py` 和 `ApplicationReloadCoordinator`。删除这套线程式 mtime watcher，不要修补其“load 前推进 snapshot、异常杀死 daemon thread”的平行语义。

基线结论：生产搜索未发现 `USE_ENCODED_MEDIA` 的读取者；`kernel.tools.ToolDefinition/ToolCatalog` 只由自身 package facade 互相导入，生产执行与 composition 使用 Runtime catalog/provider definition 和 Contracts materialized catalog；`ConfigWatcher` 也无生产构造或 activation consumer。`contracts.file.ContentIdentity` 的 facade 消费仅见测试，生产 File 消费者可直接迁到 defining module。上述项目可进入删除迁移，但实施时仍须补充外部 wire/durable 字段扫描，防止把仓外协议兼容与仓内零消费者混为一谈。

实施任务：对每个列举项执行仓内生产、测试、外部 wire/durable schema 和已承诺 SDK surface 的 exact consumer scan；迁移仍属错误 owner 的消费者，随后删除失效常量、注释、re-export、Kernel 平行 catalog/definition 和线程式 ConfigWatcher。任何仍在支持窗口的外部 wire 或 durable 字段必须转为显式版本化迁移工作，不能作为兼容 alias 留在生产内部面。

仅因当前无生产消费者，不得删除明确用于未来扩展、Python SDK 或可选装配的窄 Port/实现；本条只清理已经结束迁移后仍制造第二 canonical owner、错误 import 承诺或当前兼容歧义的残留。

验证与签收：每个删除项都有 consumer scan 证据；生产 composition、hot reload、tool materialization、File identity 和 media field 行为仍由唯一 canonical owner 提供；旧 symbol/module/export 消费者归零，外部/durable compatibility 若存在则有独立 version、decoder 和退役条件。

### R3.5 清零生产代码中的非顶部 import，并显式隔离 optional backend

仓库硬约束要求除 `ztest/` 外所有 import 位于模块顶部；可选/平台依赖也应在顶部 `try/except ImportError`，类型依赖进入顶部 `TYPE_CHECKING`。全库复核发现仍有真实运行时 import 位于函数体，例如 Squilla artifact/model loader 在调用期导入 `joblib/lightgbm/onnxruntime`（`product/routing/squilla/ml/inference/artifacts.py:19-21`）、BGE loader 导入 `onnxruntime/tokenizers`（`product/routing/squilla/ml/bge_onnx.py:21-22`）、feature loader 导入 `sentence_transformers`（`product/routing/squilla/ml/v4_features.py:27`）、telemetry reporter 在函数内选择 `requests_unixsocket/requests`（`runtime/telemetry/reporting.py:61-63`），以及 `BaseNode` 在方法体导入 `Annotated`（`orchestration/workflows/base_node.py:33`）。这些路径绕过模块 import graph/gate，使 optional dependency 缺失、循环依赖和启动副作用只在深层调用时暴露。

其余扫描命中大多是合规的模块顶部 `TYPE_CHECKING` 或模块顶部 `try/except ImportError`，例如 FileOps document/PDF adapter（`runtime/fileops/documents.py:18-41`、`runtime/fileops/pdf_views.py:20-34`）、Terminal optional rich/POSIX adapter（`product/interfaces/terminal/consumer.py:37-40`、`product/interfaces/terminal/port.py:62-65`）和 CLI 的 ACP/AG-UI/Textual adapter（`product/entrypoints/cli/__main__.py:23-36`）；它们不应被机械迁成 eager import。平台分支的模块顶部 import（`runtime/fileops/locking.py:17-29`）同样合理。

实施任务：所有真实运行时 import 移到模块顶部；标准库/必需依赖直接导入，可选依赖在顶部形成 typed availability slot，平台依赖按顶部平台分支选择。若顶部导入暴露循环，必须拆分 owner、抽取 Contracts Port 或调整初始化顺序，禁止继续以局部 import 回避。对于体积较大的 optional plugin，需要懒加载时使用一个明确的 plugin/entrypoint loader（类似 `runtime/durable/plugins.py:13-25`）并返回 typed factory，不允许在普通业务函数中散布 `import`；plugin loader 本身进入 composition declaration 和 hermetic availability gate。

合法 typed plugin loader 的机器可验证定义如下：

- 唯一登记 authority 由 Product-owned 显式 catalog/manifest 拥有，manifest 至少包含稳定 plugin identity、provider kind、module/entrypoint、factory contract identity、来源 provenance 和 capability declaration；
- loader 只能消费经过 schema 校验和 trust/approval 的 manifest，返回 Contracts-owned typed factory/Port，不返回 module、`Any`、`object` 或反射对象；
- activation 只能由 canonical Product composition 显式触发，import/construct 阶段不得扫描文件、加载 provider 或启动外部资源；
- 架构 gate 维护唯一 loader allowlist 或从 authoritative catalog 确定性派生允许位置，普通业务模块中的 `importlib`、动态字符串 import 和局部 import 一律不因“plugin”命名获得豁免；
- plugin identity、manifest 内容与已批准 generation 绑定，内容或 capability 改变必须重新经过 trust decision。

验证与签收：AST gate 对未登记动态/局部 import 有 negative fixture；hermetic composition 在 plugin 缺失时仍可导入，只有显式 activation 才解析已批准 manifest；factory 返回类型与声明 Port 由 Pyright 和 contract test 验证。

验证与签收：AST gate 对所有生产模块拒绝函数/方法/类体 import，同时豁免模块顶部 `TYPE_CHECKING`、`try/except ImportError` 与平台分支；最小依赖环境导入 canonical composition 不加载 Squilla ML、TUI、浏览器或 provider SDK；选择已安装 optional backend 时由唯一 typed loader 成功解析，未安装时在 capability planning/selection 阶段返回明确 unavailable，而不是执行深处 `ModuleNotFoundError`。

### R3.6 盘点并迁移 durable ErrorCode envelope

错误 owner/re-export 清理与 serialized error 协议演进是不同变更：前者是 import/public API 收敛，后者涉及 journal、event、wire、artifact metadata、snapshot 的协议边界。用户已授权现有 ErrorCode 持久数据从零开始，不保留旧 decoder 或 migration；是否存在独立的仓外 wire ABI 仍须以证据确认，不能把磁盘丢弃授权扩大成外部协议破坏授权。

consumer inventory 结论：本地 durable ErrorReport 主要经 `runtime/tools/tool_result_receipt.py`、`orchestration/background_tasks/model.py`/`results/attachment.py` 和 Session rollout 中的消息 JSON 持久化，当前 `ErrorReport.from_dict` 使用 `.get`、`bool()` 与裸 `dict[str, Any]` 宽松恢复；这些数据属于已授权精确丢弃/直接切换范围。`contracts/events/application.py`、`contracts/events/inference.py` 等跨边界 DTO 另以字符串 `error_code` 投影，ACP/AG-UI 只消费 presentation event；它们不是本地 ErrorReport decoder 的隐式兼容路径，任何已发布 wire ABI 必须按各自 schema/version 单独裁决。Artifact、Secret、Workspace durable data 未发现依赖 ErrorReport decoder，不在删除目标内。

外部 ABI inventory 按下表逐项关闭；“内部只读到字符串”不能证明仓外没有消费者：

| surface/consumer | 当前 error 表达 | durability/ABI | 开工前所需结论 |
| --- | --- | --- | --- |
| ToolResult receipt | `ErrorReport.as_dict/from_dict` | local durable | `AUTHORIZED_DISCARD` 精确根路径、typed discard receipt、直接切 strict envelope |
| BackgroundTask attachment/notification | ErrorReport JSON mapping | local process/result artifact；可能进入 Session | 明确随 owner scope 丢弃/重建边界，不触及无关 Artifact |
| Session rollout/message | JSON-native ErrorReport 或投影字段 | local durable session | 精确识别含 ErrorReport 的 event/message variant与删除目标 |
| ACP | tool failure仅把 presentation `error_code` 作为文本 fallback；JSON-RPC 整数 code 属于 ACP协议 | external ACP protocol | 原样保留 wire shape；不属于 Mote ErrorCode migration target |
| AG-UI | tool failure仅把 presentation `error_code` 作为文本 content fallback | external AG-UI protocol | 原样保留 event shape；不发送 ErrorReport/ErrorCode enum |
| Inference OpenAPI | nullable `error.code` string，当前 application 固定发送 `None` | v1 external schema | 保留字段与 shape；不存在旧 Mote ErrorCode enum value 兼容负担 |
| Python SDK/公共 DTO | Contracts application/inference/presentation `error_code: str` | public contract candidate | 原样保留；未来 envelope 变化由各 event/wire owner 单独版本化 |
| Artifact metadata/snapshot | 当前未发现 ErrorReport decoder | durable non-target | negative evidence与删除隔离测试，禁止纳入 broad cleanup |

inventory 裁决：R3.6 destructive target 只包含精确识别的 ToolResult receipt、BackgroundTask attachment/notification 与 Session ErrorReport variant；Artifact metadata、Secret、Workspace、ACP/AG-UI wire、Inference OpenAPI 和公共 DTO 均为 negative target。外部 surface 不发送旧 Mote ErrorCode enum，因而无需额外产品退役决定；稳定字符串字段原样保留。R3.6 可以进入 `CONFIRMED`，实施时用隔离测试证明 discard 未越界，并由对应 wire/event owner独立管理未来版本。

实施任务：先建立 serialized consumer inventory，逐 domain 记录 encoder/decoder、durability、external ABI、fixture/data 与 retention。持久化 ErrorCode 直接切换到新 schema，旧数据按精确 destructive target 执行 `AUTHORIZED_DISCARD`，不实现 upcaster、双读或兼容 migration；删除必须有 typed receipt/audit，且不得触及 Artifact、workspace、secret 等未授权数据。若跨边界确需统一外壳，Contracts 只拥有 `namespace + code + schema_version + typed context` envelope，各 domain 拥有自己的 code enum/codec；仓外 wire ABI 若存在则另按其版本/退役策略处理。禁止全局 ErrorCode manager、长期双读、旧 code alias 或在 contract 中携带英文展示文案。

验证与签收：inventory 区分已授权丢弃的本地 durable consumer与可能受支持的仓外 wire consumer；新 envelope strict round-trip，unknown namespace/version/code/context fail closed；精确丢弃目标之外的数据不受影响，旧 decoder/alias 归零；Product presenter 才拥有人类文案，Runtime/Contracts 不反向依赖 presentation。

## 8. 合理保留项

以下模式不作为本计划的合并目标：

- consumer-owned `HistoryProjection` Protocol 与 Kernel concrete DTO；
- `OutputEngine` Protocol 与 Runtime implementation；
- 不同 bounded context 中的 `MutationResult`；
- generic runtime lease 与 session run lease 的 `LeaseEpoch`；
- Contracts wire usage 与 Runtime session accumulator；
- provider SDK、ACP/AGUI wire、MCP JSON Schema、私有 telemetry binding 中经验证的局部动态值。
- Session event payload 在版本化 codec 边界内的动态 JSON；
- Sentry 等外部观测 metadata；
- 平台能力探测，例如 `hasattr(os, "getuid")`。
- 明确预留的窄 Contracts Port，即使当前 Product 尚未注入消费者，包括 `LLMClient`、`HookRunner` 和 `ResourceProvider`；可更新其过时 owner 说明，但不得以“零内部 consumer”作为删除依据。
- opt-in/SDK-first 的 LSP 配置、Runtime 服务与 Product factory；默认未构造是惰性能力语义，不等于 dead implementation。只有实际启用链发生权限绕过或错误承诺时另立缺陷。
- 尚未挂入默认 daemon 的 inference admin/webhook/inspector/reasoning-replay API 包；它们作为未来部署入口保留，不因默认 composition 尚未选择而删除。
- `GrantScope.persist` 等明确标注后续阶段的枚举预留；当前实现按 session 降级应保持文档真实，但不作为架构债清除。
- `InterAgentCommunication.attachments` 与 Kernel `RecoveryDirective`/node lifecycle callbacks 等未来协议扩展点；只要当前主路径不宣称已交付、不影响现有消息/执行语义，就不因消费者尚未接入而整改。

如整改触及这些类型，只能为明确命名或边界收窄服务，不得因同名机械合并。

### 8.1 生产包覆盖矩阵

本矩阵是本轮全库调查的可核验索引，覆盖 `contracts/`、`kernel/`、`runtime/`、`orchestration/`、`product/` 的全部一级生产包；测试与文档不作为整改对象。标记“已有 R”表示该包的高置信问题已进入上文，标记“保留”表示已反查生产 consumer、装配入口和边界类型后没有发现需要另立编号的问题，不代表永久豁免。二级包的跨 owner 问题归入其一级 owner 行。零内部 consumer 只是一项可达性事实，明确的未来扩展点、SDK surface 与 opt-in 实现仍归“保留”，不能据此判为 dead。

| 层/一级包 | 审计结论 | 对应需求或保留理由 |
| --- | --- | --- |
| `contracts/agent, artifact, authorization, composition, config` | 已有 R/部分保留 | R1.1、R1.2、R2.21、R2.24、R2.27、R2.40；未来 durable grant 枚举预留保留 |
| `contracts/content, conversation, events, execution, file` | 已有 R/部分保留 | R0.7、R1.17、R2.15、R2.23、R2.26、R2.30、R2.32、R3.1/R3.4；版本化事件 payload 的动态 JSON 保留 |
| `contracts/foundation, hook, inference, interaction, model` | 已有 R/部分保留 | R0.2、R0.6、R1.3/R1.19、R2.7、R2.11、R2.25、R2.36、R2.38/R2.39；未来 Hook Port 保留 |
| `contracts/output, runtime, service, session, surface, task, tool` | 已有 R/部分保留 | R0.1、R1.4/R1.5、R1.6、R1.21、R2.6、R2.9、R2.29、R2.41；`OutputEngine` Port/Runtime 实现分工保留 |
| `contracts/ports`（含全部 domain 子包） | 已有 R/部分保留 | R1.1、R2.4-R2.14、R2.24；未来/SDK Port 保留，只有错误 owner/迁移残留进入 R3.4 |
| `kernel/commands, execution, flow, inference` | 已有 R/部分保留 | R2.2、R2.23、R2.28；命令协议、执行图及未来 recovery callbacks 保留 |
| `kernel/output, telemetry, tools` | 保留/已有 R | consumer-owned output Protocol保留；Kernel telemetry窄seam与EventT连续性由R2.44承接；dead ToolDefinition/ToolCatalog由R3.4删除 |
| `runtime/agent, config, context, durable` | 已有 R | R0.8/R0.9、R1.1/R1.2、R2.8、R2.10、R2.21、R2.30、R2.32、R2.46 |
| `runtime/artifacts, fileops, session` | 已有 R | R0.7、R1.23/R1.24、R2.29-R2.32、R2.43、R2.46；FileOps 自有 transaction/journal 不与 RunJournal 机械合并 |
| `runtime/code_map, file_watch, hook, lsp, watching` | 已有 R/部分保留 | R0.6、R1.14、R1.17、R2.5；opt-in/SDK-first LSP 保留 |
| `runtime/control, persistence, ledger` | 已有 R | R0.8/R0.9、R2.31；本地 journal durable commit 与跨 backend operation fencing 分包覆盖 |
| `runtime/errors, resilience` | 已有 R | R2.12、R3.1；错误 taxonomy/failover owner 不新增平行 facade |
| `runtime/events, projections` | 已有 R | R2.15、R2.22、R2.26、R2.30、R2.43-R2.44；typed event/reducer、durable subscriber与observation隔离已覆盖 |
| `runtime/inference, models, service_gateway` | 已有 R | R1.6、R1.21/R1.22/R1.25、R2.7、R2.25、R2.37、R2.38/R2.39 |
| `runtime/interactive, media, sandbox, secrets` | 已有 R | R0.3、R1.18、R2.13、R2.33、R3.2；optional backend 不得由基础 facade eager load |
| `runtime/output, resources` | 保留/已有 R | output publication/durable event 问题归 R0.1/R2.24；resource spill 是 ToolResult 限流实现，不另造 canonical payload |
| `runtime/prompt, text` | 保留/已有 R | R2.8、R2.45承接typed dynamic context、prompt/cache/compaction generation；纯text normalization仍是内部实现 |
| `runtime/tools` | 已有 R | R0.1、R0.8、R1.14-R1.16、R2.11、R2.36、R2.40 |
| `runtime/vcs` 及根级 `process.py` | 已有 R | R2.30 已覆盖 shell/argv/runner 混合信任边界；其余 collector 是生产 prompt/context consumer |
| `orchestration/agents` | 已有 R | R1.13、R1.20、R2.4、R2.9、R2.24、R2.27、R2.34/R2.35 |
| `orchestration/automation` | 已有 R | R1.11/R1.12、R2.21、R2.46 |
| `orchestration/background_tasks` | 已有 R | R1.4/R1.5、R2.9 |
| `orchestration/workflows` | 已有 R | R1.4/R1.5、R2.1、R2.9、R2.46 |
| `product/agents, automation, code_map, skills, toolsets, workflows` | 已有 R | R1.9、R1.11/R1.12、R2.5、R2.34/R2.35、R2.40；package facade 均有正式装配 consumer |
| `product/composition, config, entrypoints` | 已有 R | R0.0、R0.6、R1.6-R1.9、R2.19-R2.21、R2.42、R3.4；`entrypoints/gateway` 由主 CLI 路由，不是死入口 |
| `product/i18n, paths` | 保留 | locale/catalog 与路径解析均有 Product CLI/composition consumer，未发现第二 canonical owner 或兼容旁路 |
| `product/inference, models, routing` | 已有 R | R0.2、R0.4/R0.5、R2.3、R2.7、R2.25、R2.37-R2.41、R3.2 |
| `product/interaction, interfaces, session_hosting` | 已有 R/部分保留 | R1.3、R1.10、R1.19、R2.36；未来 inference HTTP deployment facades 保留 |
| `product/lsp` | 保留 | opt-in/SDK-first Product factory，不因默认 composition 未构造而判 dead |
| `product/media_generation, web_search` | 已有 R/部分保留 | R0.3、R1.6、R1.21/R1.22、R2.6、R2.38；Web Search one-shot settlement 不机械套用 accepted media receipt |
| `product/presentation` | 已有 R/部分保留 | R2.22、R3.2；`state`/`rich_rendering` 有 CLI/TUI production consumer，不是死 facade |

覆盖判定同时执行了显式反向 import 与局部 import 检查：未发现 `contracts → kernel/runtime/orchestration/product`、`kernel → runtime/orchestration/product`、`runtime → orchestration/product`、`orchestration → product` 的生产显式反向导入。局部 import 扫描发现 R3.5 所列真实运行时命中，但当前未证明其中存在用于掩盖反向分层依赖的命中；模块顶部 `TYPE_CHECKING`、guarded optional import 和平台分支不属于违规。后续若新增一级/二级生产包，治理 gate 必须要求它进入本矩阵或由自动化 owner/consumer 清单替代，避免覆盖索引再次失真。

## 9. 治理门禁实施清单

本节编号是治理控制项，不等于“一项一个全库测试”。同一控制项可以由多种 suite 共同证明，但每个 suite 必须有明确 owner、最小 fixture 和失败定位，不得构造依赖完整 Product composition 的巨型 gate。

不得为治理新增第二份owner/public-surface manifest。先盘点现有Contracts/Product typed governance declaration、generated artifact、静态allowlist和baseline，为每类关系选择唯一authoritative declaration：有runtime/composition consumer才使用production typed declaration；纯架构约束由测试读取authoritative module/public API；generated artifact只从单一source确定性重建并diff校验；核心总账只跟踪债务状态。allowlist新增项必须绑定已确认contract/consumer，删除owner/API时同片删除declaration、artifact与gate项。

门禁按风险分层：AST/import graph证明layer、local/private import；Pyright证明Protocol/generic；runtime test证明construct/activation/cleanup；双进程与deterministic fault injection证明durability/fencing。固定字符串、类名/目录名和symbol不存在只能保护已确认局部关系，不能证明owner、lifecycle或状态机删除；外部adapter内立即严格解码的动态值不得被全局`Any`禁令误杀。

| Suite | 适用控制项 | 执行边界 |
| --- | --- | --- |
| 静态架构 gate | 1–4、7、9–10、13–14、18、24、28、30、32、38–45、53 | AST、import graph、symbol/catalog 集合和声明一致性；不得启动 Product 或可选 backend |
| Contract/codec regression | 5、11–12、17、20、24–30、41–46 | DTO、严格 decoder、canonical identity、round-trip、unknown-version/shape negative fixture |
| Component integration | 2、6、15–16、19、21–23、28、31–50 | 只装配被测 bounded context 及其 fake Port，验证权限、owner binding、状态转换和消费者链 |
| Deterministic fault injection | 11、17、25–27、35–37、42、46、48–52 | I/O 阶段、崩溃重启、取消、并发 owner、lease 丢失、ABA 和 settlement |
| Composition hermetic gate | 2、6、8–9、14、16、21、28、30、33、38、40、43、53 | 从唯一 Product root 验证 declaration/factory/Port/scope/lifecycle，不加载未启用 optional backend |

现有 local-import、SCC、governed-boundary 和 unique-production-path gate 继续保留，并补充以下控制项：

1. canonical symbol 唯一定义与 facade 遮蔽检查；
2. 模型可见工具引用与 Application catalog 一致性检查；
3. governed Protocol 中无界 `Any`、裸泛型、`object` 逃逸检查；
4. 跨 owner 私有字段访问、`hasattr/getattr` 能力探测与魔法属性 Protocol 检查；
5. durable codec 未知类型 fail-closed 与 round-trip 检查；
6. composition declaration 的 implementation/factory/required port 与真实构造链一致性检查；
7. compatibility facade exact consumer set 归零删除检查；
8. 核心 package import 不加载 optional Product backend 的 hermetic 检查。
9. canonical composition 引用的 symbol 必须真实存在，禁止未定义名称被 optional import failure 遮蔽；
10. 同一状态枚举、配置基础模型和 progress contract 的重复定义检查。
11. durable JSON 记录必须声明 schema version，并有 unknown-version negative fixture；
12. event union、discriminator map、codec catalog 与 persistence policy 的集合一致性检查；
13. 已结束迁移的 compatibility facade 与错误 owner re-export 的清零检查；未来扩展 Port、SDK surface 和 opt-in 实现必须可显式标注并豁免“零内部 consumer”。
14. 基础 Runtime facade 不得加载 Chromium、PTY、TUI 等可选 backend。
15. 模型可调用工具的真实文件写入必须与 `mutates_filesystem_for`、全部 canonical permission targets 和 transaction settlement 一致；路径解析差异、symlink escape 和“远端成功后吞掉本地拒绝”必须有 negative fixture。
16. 所有生产 subprocess 入口必须登记为固定内部 argv 或受治理用户命令；禁止配置加载路径执行未经分类的 `shell=True` 字符串。
17. 名为 digest 的 durable identity 字段必须有 canonical bytes 重算与 verifier 消费者，禁止只比较多个来源携带的同一声明值；signature 字段只有在存在明确来源认证 contract、trust root 和验签消费者时才允许出现。
18. generated wire 类型的动态豁免止于 adapter；内部 backend Protocol、execution registry 和 variant dispatch 不得因此使用无界 `Any` 或反射。
19. 共享服务的 authenticated principal 必须继续经过对象级 owner binding；以 execution/session id 访问 durable object 的路径必须有跨 principal negative fixture。
20. Session/Residency 的文件 key、stream id、唯一 meta、Role identity 与 registry identity 必须集合一致；恢复失败不得删除唯一副本。
21. 配置 layer 的 trust 必须由 canonical resolved path/ownership 决定；同一物理文件不得以多个 source label 重复合并。
22. 项目 hooks 等可执行扩展必须经过独立 trust、permission、sandbox 和审计；安全 policy hook 的失败不得静默放行。
23. 通用进程 runner 不得用 `shell` 开关混合固定内部 argv 与用户命令信任边界。
24. Durable sink 的参数必须是封闭 typed event union，不得把 `object` 留给运行时 catalog/cast 筛选。
25. 同一 codec 模块中的 durable decoder 必须统一 exact-shape/strict-primitive 语义，禁止局部用 `int/str/bool` 强转形成隐式兼容。
26. Runtime checkpoint 的 codec、schema version、typed payload decoder 和 upcaster 必须由同一 registry 管理；projection/staging 必须拒绝 identity 替换、版本倒退和同版本不同 digest。
27. Secret vault 的 read-modify-write 必须是跨 Role/进程原子事务；durable security state 不得以宽松 decoder 把损坏或未知格式恢复为空。
28. 项目 Agent、Skill、Hook、MCP 必须经同一 canonical extension-source provenance/trust gate，不能因位于 checkout 自动进入模型或执行面。
29. 动态 Agent 的 durable identity 必须绑定 definition digest/source；恢复不得吞掉 snapshot schema/state 或按同名新定义静默替换。
30. Agent catalog 只能由一个 compiler 生成，所有入口共享 name/alias 冲突规则和稳定 snapshot version。
31. 远程协议的 load/fork/turn 不得用 upsert 或成功状态掩盖恢复、分支和执行失败。
32. Tool lifecycle 的跨事件关联键必须由执行 owner 端到端提供；transport 不得使用对象身份或独立计数器猜测。
33. WirePermit epoch 必须来自真实、单调的 backup/admission state；需要 permit 的生产入口不得硬编码零代。
34. 同一 session 的 turn admission 必须是原子单 owner；ConnectionScope 的 human channel 与 telemetry 必须按 owner/turn 隔离。
35. Cron 只有在 durable ACCEPTED receipt 结算后才推进 schedule；DEFERRED/REJECTED 不得被当作成功。
36. Cron schedule 的 CRUD/settlement 使用单一事务入口和 fencing lease，CLI 不得绕过 Service 盲写 Store。
37. 对外声称 accepted/never-loss 的 Agent delivery 必须有 durable intent/ack；否则返回真实 best-effort disposition。
38. 模型 endpoint 声明的有限 operation 必须与实际 transport registry 一致；unsupported operation 在 wire 前拒绝。
39. failover group 只能在 command protocol、schema/multimodal envelope 与 projection compatibility 一致的 endpoint subset 内切换，不得以首 endpoint 代表整组。
40. 动态 tool catalog 的 reload 使用原子 generation swap，完整 canonical/alias namespace 禁止 silent overwrite。
41. Tool definition semantic identity、catalog fingerprint 与 generation 必须由同一 canonical compiler 生成；禁止固定默认版本或读取不存在的 version 属性。
42. OAuth credential subject 不得由外部名称直接构造路径；凭据写入原子且单 backend authoritative，损坏状态 fail closed。
43. 声明但无效的 MCP OAuth 配置不得退化成匿名连接。
44. GenerationArtifact 的 domain binding 必须是封闭版本化 DTO，不能用顶层 schema version 包裹 `dict[str, Any]`。
45. Contracts-owned typed event 进入 Hook 内部链路后不得退化为 `object`/裸字符串；动态编码只发生在外部 wire adapter。
46. 浏览器登录 profile 等 credential state 必须使用无碰撞 identity、严格 codec、原子事务和真实文件副作用授权。
47. Human prompt reply 必须绑定 authenticated principal、session、turn 和 prompt kind，相关 id 不得单独充当授权凭证。
48. Agent eviction、rehydration、delivery 与 residency capacity 必须共享 incarnation generation fence 和原子状态机。
49. Hosted Service 的 accepted receipt 在 caller cancel、deadline 与 shutdown 后必须有远端取消或明确 in-doubt/pending settlement，不得遗留可被静默恢复的 open attempt。
50. Workspace-global Hosted Service journal 必须有 per-call cross-process owner lease、tail revision/CAS 与 generation fence，实例内锁不能承担外部副作用互斥。
51. Workspace cleanup 与 Artifact GC 必须由跨进程 fenced owner 执行，并把所有 active cursor/stage/publication/checkpoint lease 纳入统一 pin closure。
52. 在外部副作用前声明 durable 的 ledger 写入必须返回可验证 commit；started 未提交、journal 中段损坏或 ownership 未取得时必须 fail closed，reap 不得先破坏内存真相。
53. 生产 AST 不得包含函数、方法或类体 import；optional/platform dependency 只能经模块顶部 guarded import 或登记的 typed plugin loader 进入，不能靠深层调用延迟暴露。

Gate 不得通过导入完整 Product composition 来工作，也不得因缺少 `pyte` 或 provider SDK 而收集失败。

## 10. 实施总账、顺序与依赖

### 10.1 更新规则

- 第 10.4 节每个工作包一行，是状态、Owner、阻塞项和证据的唯一人工维护总账；正文只保存范围、证据线索、实施任务和验收语义。
- 里程碑状态由所属工作包确定性派生，不单独人工填写：全部 `DONE/REJECTED` 才完成，存在 `IN_PROGRESS` 即进行中，存在 `BLOCKED/DECISION_REQUIRED/NEEDS_EVIDENCE` 即未就绪，其余为待开始。
- 同一 `R*` 只归属一个主里程碑。依赖只通过第 10.2 节的工作包边和台账“前置/阻塞项”表达，不复制工作包或建立第二实现路径。
- 工作包进入 `CONFIRMED` 前必须完成 owner/lifecycle 审核；如果仍跨多个独立状态机、store、lifecycle owner 或可独立验收变更，先拆分并连续重编号，再开始实现。
- 本次重编号一次性消除了 R0.1、R1.22 缺口和 `R1.4a`，并将原 R2.15/R2.16 按 owner 拆分；旧评审中的编号只作为历史定位，不再作为状态键。

### 10.2 可检查的关键依赖边

下表只列跨工作包硬前置；未列出的同里程碑工作包可以在 owner 和测试资源允许时并行。依赖目标未达 `DONE` 前，消费方最多进行只读复核，不能建立临时 seam。

| 消费工作包 | 必须先完成 | 原因 |
| --- | --- | --- |
| R0.3 媒体 publication settlement | R0.1 | `committed/failed/in_doubt`、requested/resolved target 与 artifact reference 必须经唯一 durable `ToolResult` contract 跨 rollout/replay/compaction 传播 |
| R0.6 Hook 治理 | R1.9、R2.30 | 必须复用 canonical extension trust 与受治理用户命令 runner |
| R1.7 API-key helper | R1.8、R2.30 | helper 来源信任与 argv runner 必须先闭合 |
| R1.14 MCP generation swap | R1.9、R1.16、R2.40 | generation 只能发布已验证来源、认证与 canonical tool definition |
| R1.16 MCP OAuth | R1.9 | 按 ADR-D3 复用完整 candidate generation 的原子编译/发布边界 |
| R0.4 Generation content identity | R2.41 | 按 ADR-D1 基于 typed canonical artifact 计算内容摘要并删除虚假签名 |
| R1.21 Hosted Service settlement | R1.6、R1.22、R2.6 | 按 ADR-D2 让 typed gateway、跨进程 owner、deadline disposition 与 cancel settlement 闭合 |
| R1.21 Hosted Service settlement | R1.25 | accepted receipt 必须先有持续 scan/reconcile 的 canonical lifecycle owner |
| R1.25 Hosted Service reconciler | R1.22 | reconcile claim 与 takeover 必须复用 per-call fencing，不建立第二 owner |
| R1.23 Workspace maintenance | R1.24、R2.19、R2.29 | sweeper 先消费统一 pin snapshot、durable residency 与 lease schema |
| R0.9 Durable operation ownership | R2.46 | lease renewal/takeover/expiry 使用 canonical clock contract |
| R2.47 Workflow durable run | R0.9、R2.1、R2.46 | definition identity、operation ownership 与 durable clock 先固定 |
| R2.48 Workflow reconciliation/effect | R0.9、R2.47 | reconciler 只推进 canonical durable run，并复用 operation fence |
| R1.11、R1.12 Cron receipt/fencing | R2.21 | durable schedule/trigger schema 必须先固定 |
| R1.11、R1.12 Cron durable time | R2.46 | occurrence、misfire、lease expiry 与 transaction timestamp 使用 canonical clock contract |
| R1.20 Residency 状态机 | R1.26、R2.19、R2.20、R2.27 | record、mailbox、resume identity 与 Pool drain receipt 必须共享 incarnation fence |
| R2.49 Application hot reload | R1.9、R1.14、R2.35、R2.40、R2.42 | reload 必须复用 trust、catalog identity 与唯一 application lifecycle |
| R2.10 Agent wiring projection | R1.8、R1.9、R2.42 | wiring 只消费 Product canonical owner 产出的 approved immutable projection |
| R2.50 Durable turn scheduler | R2.16、R2.20、R2.29 | permit、mailbox identity 与 fenced durable lease 先固定 |
| R2.28 Lineage/spawn saga | R1.20、R2.17、R2.51、R2.52 | spawn 复用 incarnation、nickname、capacity 与 budget typed receipt |
| R2.53 Subtree cancellation | R1.20、R1.26、R2.16、R2.28、R2.50 | cancellation 只协调各 owner 的 typed command/settlement |
| R0.5 Shared execution authorization | R2.54 | 必须先固定 typed execution variant 与 owner record |
| R2.26 Shared daemon adapters | R0.5、R1.1、R2.54 | 授权 owner binding 完成后迁移 protobuf/backend/variant dispatch |
| R2.37 WirePermit epoch | R0.5、R1.1、R2.54 | epoch 与 execution principal/generation binding 使用同一 verifier |
| R3.6 Durable ErrorCode envelope | R3.1 | 先归正 error owner，再按 serialized consumer inventory 迁移协议 |
| R1.20、R1.22、R1.23 durable lifecycle time | R2.46 | incarnation/operation lease、deadline 与 retention 不得各自发明 clock schema |
| R2.21 Cron schema | R2.46 | occurrence 与 schedule 时间字段先绑定 canonical absolute instant/clock identity |
| R2.29 File lease schema | R2.46 | expiry 与 takeover 使用 canonical durable instant，fencing token 仍由 lease owner 管理 |
| R1.10 Remote Session load/fork | R2.27、R2.34、R2.43 | adapter 只投影 canonical verified identity 与 Session read model，不复制恢复 validator |
| R1.18 Browser profile | R1.8、R2.33 | 配置 trust 与 vault/credential durable primitive 先闭合 |
| R2.14 Kernel graph typing | R2.2 | 先建立唯一 Contracts-owned `CompletionPolicy`，graph assembly 不得依赖待删除的 Kernel 重复定义 |
| R3.1–R3.5 清理 | 对应消费者迁移工作包 | 只有消费者归零后才能删除 facade、alias、旧 owner 和局部 import 绕路 |

禁止为消除依赖而复制 trust、runner、codec、lease、catalog、factory 或 store。发现新增硬依赖时必须先更新本表和对应工作包，再开始实现。

### 10.3 产品决策登记

| ADR | 工作包 | 状态 | 已确认决定 | 决定理由 | 实施影响 |
| --- | --- | --- | --- | --- | --- |
| ADR-D1 | R0.4 | CONFIRMED | 只承诺 canonical content digest；删除 signer/signature，不建设 PKI | 没有远端发布者认证、key owner 或验签消费者，旧字段只制造虚假能力 | 同一 canonical serializer/verifier 覆盖 stage、restore、activate、Admin、daemon 与 SQLite；签名字段归零 |
| ADR-D2 | R1.21 | CONFIRMED | deadline 停止等待并返回 durable resume handle；明确 cancel/shutdown 才尝试取消远端 | 时间预算耗尽不等于用户撤销付费作业，但 accepted receipt 仍必须有 canonical owner 持续 reconcile | 增加 `WAITING_REMOTE` 类 typed disposition、resume/query/cancel seam 和唯一 terminal settlement |
| ADR-D3 | R1.16 | CONFIRMED | 任一 MCP source 失败即拒绝完整 candidate generation并保留旧 active generation | catalog identity、namespace 和 capability snapshot 是整体编译结果，部分发布会产生配置与实际能力漂移 | compiler 返回 typed generation failure；无网络/无部分 tool 发布；修复输入后显式重试 |
| ADR-D4 | R0.3 | CONFIRMED | 重复 canonical target 由 Product 在远端请求前确定性改名并通知 Agent；publication 逐资产原子并返回 `committed/failed/in_doubt`，成功项保留 | 名称冲突可安全消解，不应阻断生成；普通文件系统没有多路径原子提交，远端副作用发生后必须如实表达部分成功和不确定结果 | 固定 target plan/reservation/retry identity、requested→resolved 映射和 tagged settlement；当前不承诺或预建 multi-path all-or-nothing |
| ADR-D5 | R2.50 | CONFIRMED | tenant==root governance owner；root WDRR、持续积压subtree二级WDRR、cost=1、有界weight/priority、同priority durable FIFO；deadline/cancel/claim CAS；accept前capacity admission；fenced claim绑定R2.16 permit；retry不阻塞；config generation只影响未来claim | 给出有限无饥饿保证，同时不引入Token预测成本或含糊tenant层；accepted ownership不受queue满、配置删除或热更新破坏 | 产品阻断解除；实施前仍须完成现有queue/scheduler复用审计和确定性门禁，不能直接复制Inference queue |

ADR 只决定产品语义，不得选择错误 owner 或绕过架构硬约束。D1–D5 均已写回正文与台账；未来改变已确认决定必须新增版本化 ADR 和迁移边界，不能原地恢复旧的双语义。

### 10.4 里程碑总览

| 里程碑 | 派生规则 | 目标结果 | 工作包 |
| --- | --- | --- | --- |
| M0 Composition 基线 | 由所属工作包派生 | canonical Product composition 可 hermetic 导入与构造 | R0.0、R2.42 |
| M1 生产安全基础 | 由所属工作包派生 | 配置、扩展、凭据、认证和命令 runner 的 trust boundary 先于其消费者闭合 | R1.7、R1.8、R1.9、R1.15、R1.16、R2.30 |
| M2 外部副作用与 durable commit | 由所属工作包派生 | ToolResult、媒体、Hook 与 RunJournal 在授权和 durable commit 后产生副作用 | R0.1、R0.3、R0.6、R0.8、R0.9 |
| M3 Contract 调查与基线修复 | 由所属工作包派生 | File contract 可解析；同名 model contract 按证据合并或区分；Kernel completion 依赖唯一 Contracts Port | R0.2、R0.7、R2.2 |
| M4 Workflow、BackgroundTask 与 Cron | 由所属工作包派生 | identity、deferred result、progress、receipt、transaction、schema、clock 与 fencing 闭合 | R1.4、R1.5、R1.11、R1.12、R1.26、R2.1、R2.9、R2.21、R2.46–R2.48 |
| M5 Role、交互、事件与Prompt控制面 | 由所属工作包派生 | RoleState、interaction、remote session、Session read model、typed observation、Prompt/cache generation与human reply owner闭合 | R1.2、R1.3、R1.10、R1.17、R1.19、R2.43-R2.45 |
| M6 Inference 与 Hosted Service | 由所属工作包派生 | Shared daemon、ModelGateway、ServiceGateway 的类型、身份、能力、结算与 fencing 闭合 | R0.5、R1.1、R1.6、R1.21、R1.22、R1.25、R2.6、R2.7、R2.13、R2.25、R2.26、R2.37、R2.38、R2.39、R2.41、R2.54 |
| M7 Agent 与 Kernel | 由所属工作包派生 | control、hosting、spawn、turn admission/scheduling、nickname、handle、Code Map、context、graph 与 Activity 类型化 | R2.3、R2.4、R2.5、R2.8、R2.11、R2.14–R2.18、R2.22、R2.50 |
| M8 Durable identity、恢复与维护 | 由所属工作包派生 | generation、Agent、Residency、Mailbox、lineage/capacity/budget/cancellation、delivery、lease、vault、profile、cleanup/GC 与 durable clock 真相链闭合 | R0.4、R1.13、R1.18、R1.20、R1.23、R1.24、R2.19、R2.20、R2.24、R2.27–R2.29、R2.31–R2.34、R2.46、R2.51–R2.53 |
| M9 Catalog、配置与 identity | 由所属工作包派生 | MCP/catalog/config/tool identity、wiring、reload generation 与 Artifact 命名形成唯一生产路径 | R1.14、R2.10、R2.12、R2.23、R2.35、R2.36、R2.40、R2.49 |
| M10 迁移清零 | 由所属工作包派生 | 已迁移消费者不再依赖错误 owner、兼容 facade、重复类型、旧协议或非法 import | R3.1–R3.6 |

### 10.5 工作包执行台账

每行只能表示一个工作包。Owner、阻塞项与证据链接在实施时填写；状态不得使用自由文本。

| 里程碑 | 工作包 | 状态 | Owner | 前置/阻塞项 | 实施与验证证据 |
| --- | --- | --- | --- | --- | --- |
| M0 | R0.0 | CONFIRMED | — | — | `MoteConfig` 断链及 standard→agent factory/catalog 立即 discovery 调用链已复核；先拆 declaration/materialization/activation，再修 canonical Config |
| M0 | R2.42 | CONFIRMED | — | R0.0 | CLI/Product composition factory 与 host 消费链已复核 |
| M1 | R1.7 | CONFIRMED | — | R1.8、R2.30 | helper config→loader→`shell=True` 执行链及来源已反查 |
| M1 | R1.8 | CONFIRMED | — | M0 | `source_root`、枚举 trust、CLI 重复 root 与 loader 消费链已反查 |
| M1 | R1.9 | CONFIRMED | — | R1.8 | Agent/Skill/Hook/MCP discovery 与 Product injection/activation 链已反查 |
| M1 | R1.15 | CONFIRMED | — | R1.8 | OAuth subject/path、file/keyring fallback 与 manager transaction 链已反查 |
| M1 | R1.16 | CONFIRMED | — | R1.9、R1.15 | ADR-D3 已确认完整 candidate generation 原子失败 |
| M1 | R2.30 | CONFIRMED | — | M0 | Bash/VCS 到 `aexecute(shell=...)` 的双信任消费者链已反查 |
| M2 | R0.1 | CONFIRMED | — | M0 | `ToolResult.data: Any`、未知对象 `{type, repr}` 降级及 settlement/replay 消费链已复核 |
| M2 | R0.3 | CONFIRMED | — | R0.1 | ADR-D4 已确认 deterministic rename + Agent notification；逐资产 publication 复用 canonical durable ToolResult contract 返回 tagged partial settlement |
| M2 | R0.6 | CONFIRMED | — | R1.9、R2.30 | layered Hook discovery、独立 `create_subprocess_shell` 与 policy 异常折叠 `EMPTY` 已复核 |
| M2 | R0.8 | CONFIRMED | — | M0 | RunJournal→AppendOnlyLedger 的先改内存、吞写错、宽松 fold 与多实例链已复核 |
| M2 | R0.9 | CONFIRMED | — | R0.8、R2.46 | Temporal history只拥有state；external effect按幂等键/receipt reconcile/non-replayable分型，visibility非真相，删除process-local closure bridge |
| M3 | R0.2 | CONFIRMED | — | — | defining modules、Runtime/transport/failover/Product compiler 消费链已复核 |
| M3 | R0.7 | CONFIRMED | — | — | `SearchResult` 缺 defining-module import及 codec 普通 `str` digest 构造已复核 |
| M3 | R2.2 | CONFIRMED | — | — | Kernel turn-completion 与 Contracts Role run-completion 生命周期差异已复核，拒绝机械合并 |
| M4 | R1.4 | CONFIRMED | — | R0.1 | 两套 deferred result、魔法属性/方法探测、Workflow alias 与 Runtime 反射消费链已复核 |
| M4 | R1.5 | CONFIRMED | — | 已确认 process-local TaskId/AttemptId 语义 | 两套同名 `BgStatus`、过期 common owner 注释及无 AttemptId 的原地 resubmit 覆盖链已复核 |
| M4 | R1.26 | CONFIRMED | — | R1.5 | Agent-owned Pool admission/pin/drain 与 Residency lifecycle owner 已分离，process-local task truth 保持 Pool 独占 |
| M4 | R1.11 | CONFIRMED | — | R2.21、R2.46 | TriggerReceipt 被 Service 丢弃，Scheduler 无条件推进/删除及 missed 批量删除链已复核 |
| M4 | R1.12 | CONFIRMED | — | R2.21、R2.46 | schedule 整文件读改写、非原子 cap、CLI 旁路及 session-id lock ABA 链已复核 |
| M4 | R2.1 | CONFIRMED | — | R1.4、R1.5 | Workflow definition 的 build/compile/arun 多入口、窄摘要 digest 与手工 Product 构造已复核 |
| M4 | R2.9 | CONFIRMED | — | R1.4、R1.5 | Runtime `Callable[[str, Any, Any], None]` 被 Workflow/BackgroundTask 共用且高层语义下沉链已复核 |
| M4 | R2.21 | CONFIRMED | — | — | Cron 无版本宽松 dict codec、primitive 强转、损坏记录跳过及裸整文件 envelope 已复核 |
| M4 | R2.47 | CONFIRMED | — | R0.9、R2.1、R2.46 | Workflow durable run 的随机 identity、进程内 state/continuation 与多恢复入口已复核 |
| M4 | R2.48 | CONFIRMED | — | R0.9、R2.47 | Workflow scan/claim、effect receipt、terminal delivery 与 wake/recovery owner 缺口已复核 |
| M5 | R1.2 | CONFIRMED | — | M0 | `RoleState.env: Any`、Agent/human 混用、ConnectionScope 交错覆盖与 environment facade 反射链已复核 |
| M5 | R1.3 | CONFIRMED | — | R1.2 | SessionDriver 私有回调注入及 HumanChannel `hasattr`/`TypeError` 签名协商链已复核 |
| M5 | R1.10 | CONFIRMED | — | R1.2、R1.3、R2.27、R2.34、R2.43 | remote adapter 只投影 canonical verified-load/fork result，不重读 Residency/definition/Session durable state |
| M5 | R1.17 | CONFIRMED | — | R0.6 | typed FileChanged fact 经 dict 重建为 `object`/`str` Hook payload 的退化链已复核 |
| M5 | R1.19 | CONFIRMED | — | R1.2、R1.3 | app-scoped `prompt_id -> Future[Any]` broker 与仅凭 promptId 跨请求 resolve 链已复核 |
| M5 | R2.43 | CONFIRMED | — | R2.25 | Session replay/live/component/governance/test 消费链已复核 |
| M5 | R2.44 | CONFIRMED | — | — | Telemetry emitter/narrower/Kernel observer的object/Any擦除及control/audit/observation混合风险已复核 |
| M5 | R2.45 | CONFIRMED | — | R0.1、R2.8、R2.40 | Prompt动态边界、cache semantic generation、compaction canonical truth与redaction链已复核 |
| M6 | R0.5 | CONFIRMED | — | R2.54 | Shared backend 仅按 execution id 查对象/持久记录，credential 未参与后续对象 ownership 校验链已复核 |
| M6 | R1.1 | CONFIRMED | — | M0 | Runtime composition lease 的 gateway 周边 capability、runtime、issuer 与 artifact 面大量 `Any` 已复核 |
| M6 | R1.6 | CONFIRMED | — | R1.1 | hosted-service governance declaration 与真实 ServiceGateway/Product snapshot 装配链不一致已复核 |
| M6 | R1.21 | CONFIRMED | — | R1.6、R1.22、R1.25、R2.6 | cancel/deadline 缺口与 ADR-D2 已确认；持续 reconciliation lifecycle 已拆归 R1.25 |
| M6 | R1.22 | CONFIRMED | — | R1.6、R2.46 | Gateway 实例级 asyncio lock、Journal 实例级线程锁与共享 workspace stream 的多进程竞态已复核 |
| M6 | R1.25 | CONFIRMED | — | R1.22 | execute/resume 外无持续 pending-call scan owner 的生产缺口已确认 |
| M6 | R2.6 | CONFIRMED | — | R1.6 | `Role.invoke_service`/ServiceInvocation 与媒体 provider payload 的裸 dict/Any 边界已复核 |
| M6 | R2.7 | CONFIRMED | — | R1.1 | `FinalizedInferenceRequest.payload: Any` 及 Kernel→Runtime 已知 generate 请求类型退化链已复核 |
| M6 | R2.13 | CONFIRMED | — | R1.6 | Web Search registry 公开可变 class map、宽 config/getattr 与无 typed factory identity 已复核 |
| M6 | R2.25 | CONFIRMED | — | — | Session event union/map/codec/persistence/sink 消费链已复核 |
| M6 | R2.26 | CONFIRMED | — | R0.5、R1.1、R2.54 | protobuf 动态类型泄漏到 backend registry/journal/control，且以 `hasattr` 区分 execution variant 已复核 |
| M6 | R2.37 | CONFIRMED | — | R0.5、R1.1、R2.54 | compatibility command/session/transfer gateway 的 `(0, 0)` epoch provider 与 execution owner binding 断链已复核 |
| M6 | R2.38 | CONFIRMED | — | R0.2、R2.7 | canonical finite operation 已存在，但 endpoint capability 与 planner admission 未表达支持集合已复核 |
| M6 | R2.39 | CONFIRMED | — | R2.38 | route profile 固定首 endpoint，而实际 failover 跨完整 endpoint group 的投影分裂已复核 |
| M6 | R2.41 | CONFIRMED | — | R0.2 | GenerationArtifact 各 domain binding/activation/pricing 仍为宽 dict，顶层版本无法约束内部演进已复核 |
| M6 | R2.54 | CONFIRMED | — | — | Shared execution identity/variant/owner-record 是授权、adapter 与 WirePermit 的共同 contract 前置，依赖倒置已纠正 |
| M7 | R2.3 | CONFIRMED | — | — | `QueueEntry.payload`、enqueue 与 dispatcher 的 `Any` 类型断链已复核 |
| M7 | R2.4 | CONFIRMED | — | — | ambient control 保存 `Any`、显式 ctx 属性反射及 spawn helper 宽输入输出链已复核 |
| M7 | R2.5 | CONFIRMED | — | — | Code Map 四类稳定 query、Product `**kwargs: Any` factory 与 Runtime consumer 链已复核；不与 LSP 机械合并 |
| M7 | R2.8 | CONFIRMED | — | — | TurnContextBus rebuild 能力反射、name fallback 与 state provider 宽化链已复核 |
| M7 | R2.11 | CONFIRMED | — | R2.42 | SessionRegistry、CLI backend、ACP/AG-UI hosting 消费链已复核 |
| M7 | R2.14 | CONFIRMED | — | R2.2 | GraphAssemblyInputs 与 Engine 对已有稳定协作者重复使用 `Any` 的组装链已复核 |
| M7 | R2.15 | CONFIRMED | — | R2.4 | SpawnContext/object provisioning、extension 反射及 OutputT 关系中断链已复核 |
| M7 | R2.16 | CONFIRMED | — | R2.3 | delivery 前 `has_capacity()` 与 scheduler 无条件 `guard()` 分离的原子 permit 竞态链已复核 |
| M7 | R2.17 | CONFIRMED | — | R2.15 | nickname 即时写全局 set、耗尽全清及 reservation 未持有/回收 nickname 已复核 |
| M7 | R2.18 | CONFIRMED | — | R2.4、R2.15 | ChildAgentHandle 的 typed runtime 与 `Any` control/residency/teardown seam 已复核 |
| M7 | R2.22 | CONFIRMED | — | — | Activity topology/node_states 从 Contracts 到 Product projection/surface 的宽 dict/Any 链已复核 |
| M7 | R2.50 | CONFIRMED | — | R2.16、R2.20、R2.29、R2.46 | 复用 Inference fair queue 的纯算法/测试不变量，拒绝复用其 process-local tenant/project concrete queue；Agent durable queue 复用 canonical permit/mailbox/lease/clock mechanism |
| M8 | R0.4 | CONFIRMED | — | R2.41 | ADR-D1 已确认 content digest-only contract |
| M8 | R1.13 | CONFIRMED | — | R2.20、R2.28 | accepted parked delivery 仅存进程内 queue/event，release 可直接 drop 且无 durable ack 已复核 |
| M8 | R1.18 | CONFIRMED | — | R1.8、R2.33 | profile 有损 slug、损坏降级匿名、原地截断写及 WebBrowser 隐式持久化链已复核 |
| M8 | R1.20 | CONFIRMED | — | R1.26、R2.19、R2.20、R2.27、R2.46 | eviction 提前释放容量、delivery/snapshot 无 fence 与并发 rehydrate 覆盖链已复核；Pool lifecycle 归 R1.26 |
| M8 | R1.23 | CONFIRMED | — | R1.24、R2.19、R2.29、R2.46 | workspace cleanup/GC 仅进程内去重且活跃 session 视图局部的 owner 缺口已复核 |
| M8 | R1.24 | CONFIRMED | — | R2.24 | Artifact pin source 未统一装配、cursor/stage/publication/checkpoint/transfer pin closure 缺口已复核 |
| M8 | R2.19 | CONFIRMED | — | — | Residency 无版本 envelope、缺字段补空值及宽松恢复链已复核 |
| M8 | R2.20 | CONFIRMED | — | — | Mailbox 以裸 list/dict dump，load 跳过错误 Message 并宽松 bool 转换已复核 |
| M8 | R2.24 | CONFIRMED | — | R0.1 | durable output candidate/accepted/migrated/committed payload 使用 `Any` 已复核 |
| M8 | R2.27 | CONFIRMED | — | R2.19 | residency key、role session、rollout meta 与 registry install/delete 缺统一 identity commit 已复核 |
| M8 | R2.28 | CONFIRMED | — | R1.20、R2.17、R2.51、R2.52 | lineage/path/nickname/parent 索引与 spawn saga 已复核；capacity、budget、subtree cancel 已分包 |
| M8 | R2.29 | CONFIRMED | — | R2.46 | Runtime lease coordinator 裸 JSON mapping、primitive 强转与无版本迁移边界已复核 |
| M8 | R2.31 | CONFIRMED | — | R0.7 | File search row/skipped decoder 的 `.get` 与 `int/str/bool` 兼容强转已复核 |
| M8 | R2.32 | CONFIRMED | — | — | checkpoint schema version 未进入统一 restore 判定、driver 宽 dict 与 replay LWW 覆盖链已复核 |
| M8 | R2.33 | CONFIRMED | — | R1.15 | 多 SecretStore 对共享 vault 的无 CAS 整文件改写、固定 tmp 与损坏置空链已复核 |
| M8 | R2.34 | CONFIRMED | — | R1.9 | Markdown 固定 role_type_id 覆盖全局注册、definition digest 未绑定恢复及 snapshot 参数吞没已复核 |
| M8 | R2.46 | CONFIRMED | — | — | Workflow/Cron/lease/retention 是只读 evidence source；clock contract 必须先于 durable 时间字段实施 |
| M8 | R2.51 | CONFIRMED | — | R1.20、R2.16 | logical/resident/turn 三类 cap 的 identity、占用与释放事实混用链已复核 |
| M8 | R2.52 | CONFIRMED | — | existing UsageLedger extension | canonical UsageLedger、BudgetReservation/Settlement、SQLite fenced implementation 与生产 inference consumers 已复核，Agent 治理不得新建第二 ledger |
| M8 | R2.53 | CONFIRMED | — | R1.20、R1.26、R2.16、R2.28、R2.50 | subtree cancellation epoch 与各 Agent owner typed settlement 边界已复核 |
| M9 | R1.14 | CONFIRMED | — | R1.9、R1.16、R2.40 | MCP reload 先删旧 catalog/teardown，再逐项直写新工具且失败无 rollback 的链已复核 |
| M9 | R2.10 | CONFIRMED | — | R1.2、R1.8、R1.9、R1.17、R2.42 | Agent wiring 只消费 Product canonical owner 产出的 approved immutable projection，不自行发现/trust checkout |
| M9 | R2.12 | CONFIRMED | — | M0 | Contracts/Runtime/Product 多个 ConfigModel owner 与散落 BaseModel validation 语义已复核 |
| M9 | R2.23 | CONFIRMED | — | — | 通用 CAS store 与 File mutation repository 同名但 reservation/lifecycle 不同，确认应改名而非合并 |
| M9 | R2.35 | CONFIRMED | — | R2.34 | AgentCatalog 的 class/markdown 多套 version 算法、namespace 与 source inspection 依赖已复核 |
| M9 | R2.36 | CONFIRMED | — | R2.40 | Tool lifecycle 可缺 invocation id，ACP counter/AG-UI `id(event)` fallback 关联分裂已复核 |
| M9 | R2.40 | CONFIRMED | — | R1.9 | snapshot `<name>@1`、`runtime-tools@1` 与 inspect-source catalog identity 多真相链已复核 |
| M9 | R2.49 | CONFIRMED | — | R1.9、R1.14、R2.35、R2.40、R2.42 | application hot reload 的 trust/catalog/generation/drain 生命周期已从 M0 composition 抽离 |
| M10 | R3.1 | CONFIRMED | — | 对应消费者归零 | Runtime error 聚合/re-export 的 definition、classification、recovery、presentation 错误 owner 链已复核 |
| M10 | R3.2 | CONFIRMED | — | 对应消费者归零 | context/OAuth/sandbox/interactive/presentation facade 与 optional eager import 漂移已复核 |
| M10 | R3.3 | CONFIRMED | — | 对应消费者迁移 | live executable 与 pinned invocation 两个 `BoundTool` 同名生命周期冲突已复核 |
| M10 | R3.4 | CONFIRMED | — | 外部 wire/durable 字段最终扫描 | 仓内生产 consumer 复核完成；仓外协议扫描留作实施前置 |
| M10 | R3.5 | CONFIRMED | — | typed plugin loader gate | Squilla、telemetry 与 Workflow 等函数体 import 已复核，并与顶部 TYPE_CHECKING/optional/platform import 区分 |
| M10 | R3.6 | CONFIRMED | — | R3.1 | destructive target 精确限定本地 ErrorReport durable variants；ACP/AG-UI/OpenAPI/公共 DTO 原样保留并作为 negative target |

### 10.6 每个里程碑的签收步骤

1. 逐个工作包核对正文中的全部实施任务与验证条件，不以里程碑摘要替代工作包验收；
2. 核对新增能力的 canonical owner、真实消费者、scope、lifecycle 和 Product composition declaration；
3. 核对旧定义、旧 factory、alias、re-export、兼容探测、双读/双写和无人使用类型已在同一切片删除；
4. 汇总实际执行的直接测试、消费者测试、架构门禁、Pyright 和 fault injection，记录通过/失败数量；
5. 重新执行 `rg` 与架构 gate，证明没有平行入口、跨层反向依赖、局部 import 或未登记生产能力；
6. 对照第 11 节完成定义，更新工作包与里程碑状态并记录提交证据。

### 10.7 当前实施就绪复评结果

截至 2026-08-01，本计划完成以下计划级校验：

- 96 个工作包均在唯一台账中恰好出现一次，并各自具备独立实施任务和验证签收；
- 具体 `.py` 证据引用已做存在性复核；行号仍只作为线索，实施时必须从当前源码重建调用链，不能把静态计数当作覆盖率；
- 台账当前 122 条显式工作包依赖边通过拓扑检查，无循环；R2.2→R2.14 的 `CompletionPolicy` 前置及 R0.1→R0.3 的 durable ToolResult contract 前置已明确；
- ADR-D1–D3 与 BackgroundTask resubmit、Agent delivery/lineage 等已确认产品语义均已写回正文和台账；
- M0 的 R0.0 已完成 construct→agent factory/catalog→extension discovery 调用链复核，实施顺序固定为先拆 approved declaration/materialization/activation，再修 canonical Config；现状不纯属于待实施债务，不再属于未知证据。
- M2 的 ToolResult、媒体写入、Hook 与 RunJournal 副作用链，以及 M3 的 File contract 和两类 completion policy 生命周期均已完成源码复核；R2.2 已据此纠正旧评审的错误合并前提。
- M4 的 Workflow definition、durable run、reconciliation/effect 与 Agent-owned BackgroundTaskPool 已按 owner 拆包；Cron receipt/transaction/schema/fencing 和 durable clock 边界已完成源码复核。保留 process-local resubmit 产品决定，但要求以单调 AttemptId 消除当前原地覆盖竞态。
- M5 的 remote Session load/fork 已增加 R2.27、R2.34、R2.43 硬前置：adapter 只投影 canonical verified-load 与 Session read model，不复制 Residency/definition validator。RoleState、FileChanged Hook 与 AG-UI prompt owner 保持原结论。
- M6 的 Shared execution 已按 typed owner contract → object authorization → protobuf adapter → epoch/permit 联合验证排序，纠正原 R0.5/R2.26 依赖倒置；Hosted Service、inference request、Web Search 与 GenerationArtifact 边界保持原 owner。
- M7 的原子 turn permit 已与 durable queue/fair scheduler 分包；ADR-D5 已确认，R2.50 复用 Inference fair queue 的纯算法/测试不变量但拒绝其 process-local tenant/project concrete queue，并复用 Agent canonical permit/mailbox/lease/clock mechanism，现已达到 `CONFIRMED`。
- M8 的 durable clock 已纠正为零硬前置的基础 contract，Cron、Residency、Hosted Service、maintenance、File lease、operation ownership 与 Workflow durable run 反向依赖它；domain 调研关系不再伪装成实施依赖。lineage/spawn、capacity、budget ledger 与 subtree cancellation保持四个 owner。
- M9 的 application hot reload 已从 M0 composition 抽离并依赖 canonical trust/catalog identity；R3.6 已将 destructive target 精确限定为本地 Tool/BackgroundTask/Session ErrorReport variants，ACP/AG-UI/OpenAPI/公共 DTO 原样保留为 negative target。
- 复用审计确认 R2.52 扩展现有 UsageLedger、R2.50 只复用 owner-neutral 调度不变量；R0.9 明确 Temporal history只fence state mutation，external effect按`IDEMPOTENT_BY_KEY/RECONCILABLE_BY_RECEIPT/NON_REPLAYABLE`分型，visibility不是恢复真相，process-local closure bridge必须删除。

ADR-D4 已确认，96 个工作包均达到 `CONFIRMED`，当前不存在计划级开工阻断。该状态只表示实施定义、owner、依赖与验收已复核，不表示债务已经修复或投产完成；工作包仍须按硬前置逐项进入 `IN_PROGRESS` 并以实际证据签收。

### 10.8 机器校验协议与安全切片发布准入

第 10.5 节仍是唯一人工状态账本；在首个工作包进入 `IN_PROGRESS` 前，建立只读取本文或由本文确定性生成的结构化 sidecar 的轻量 validator，并纳入 `ztest/architecture/`。validator 不导入 Product composition，不扫描网络/checkout extension，不以正则名称推断运行时保证，至少检查：

1. 每个正文 `R*` 在台账中恰好出现一次，台账无未知工作包；
2. 每个依赖目标与 ADR 均存在，依赖图无环；
3. `IN_PROGRESS/DONE` 的全部硬前置均为 `DONE`，`DECISION_REQUIRED` 链接未决 ADR；
4. `DONE` 有 Owner、实施证据、实际测试命令/数量及旧入口归零证据；
5. 状态只能取第 3.3 节枚举值，里程碑状态只由工作包状态派生；
6. 文档内生产 `.py` 证据路径存在；引用数量只作漂移提示，不冒充覆盖率。

安全修复不等待全部 96 个工作包完成。定义独立发布准入 `SAFETY-FOUNDATION`：R1.7–R1.9、R1.15–R1.16、R2.30、R0.3、R0.6、R0.8 及它们的全部硬前置达到 `DONE`，并通过 checkout negative fixture、permission/sandbox bypass、secret leakage、durable-started fail-closed 与媒体 target/settlement fault injection 后，即可声明配置、扩展、命令和本地副作用安全边界闭合。该声明不代表 Workflow、Agent governance、类型整洁或全计划完成。

静态 validator 只证明编号、依赖、import、owner/public API 等静态事实；permission target 一致性、lease loss、并发竞态、crash recovery、远端 settlement 与 filesystem rollback 必须由对应 bounded-context integration/fault-injection 测试证明，禁止以全库字符串扫描替代行为验收。

## 11. 完成定义

本计划只有在以下条件全部满足时才可关闭：

1. P0 至 P3 的所有工作包均为 `DONE` 或有完整保留依据的 `REJECTED`；不存在 `TODO`、`CONFIRMED`、`DECISION_REQUIRED`、`NEEDS_EVIDENCE`、`IN_PROGRESS` 或 `BLOCKED`，所有正式边界已有唯一 owner 和可执行类型契约；
2. 所有 enabled capability 从 Product canonical root 唯一可达；
3. durable result 在 compaction、rollout、resume 后语义等价；
4. 重复 canonical DTO、重复 Workflow identity 和 deferred magic protocol 已消失；
5. `RoleState` 不持有运行时服务，Product 不通过私有反射完成装配；
6. compatibility consumer 归零，对应 alias/re-export/旧实现已删除；
7. Runtime 不反向依赖 Orchestration/Product，生产代码继续保持模块顶部 import；
8. 新增治理 gate 在 hermetic 环境中通过，并至少包含每类违规的 negative fixture；
9. Pyright 对触及的公开边界无新增裸泛型、无界 `Any`、未知类型或无理由 ignore；
10. 不以未设期限的 waiver、baseline 或“后续迁移”替代上述闭合条件。
11. Product canonical composition 不包含未定义 symbol，session hosting 的并发连接不会互相覆盖 human channel。
12. Residency、Mailbox、Cron 和 durable output event 均有严格版本化 codec；损坏或未知版本不会静默恢复为空状态。
13. Kernel execution graph、Agent spawn/policy extension 和 Activity projection 的稳定内部关系不再退化为 `Any`。
14. 所有模型触发的本地文件写入在副作用前按实际 canonical target 授权；配置加载不会执行未经治理的 shell 字符串。
15. Generation content digest 能验证实际 canonical artifact 内容，虚假 signer/signature 字段已经删除，Shared daemon 内部执行 variant 不依赖 `Any` 或方法反射。
16. Shared execution 的所有对象操作绑定 owner principal；Residency/Session 恢复不会产生 key、Role、stream、meta 或 lineage 的身份分裂。
17. 配置来源不会因调用方路径参数被重复加载或提升信任等级，敏感/可执行配置只来自经验证的 canonical source。
18. 项目 Hook 不构成独立 shell 绕过，安全 Hook 不会因异常/超时 fail open；固定内部进程与已授权用户命令使用不同 typed runner。
19. Session durable sink 只接收封闭事件类型，File search 的持久记录使用严格且版本化的 canonical decoder。
20. 交互 Runtime checkpoint 使用注册的严格版本 codec，replay/staging 不允许旧或分叉状态覆盖 canonical 恢复点。
21. Secret vault 的并发 section 更新不丢失，损坏、未知版本或错误 shape 不会静默削弱 redaction/credential 保护状态。
22. 项目扩展只有在 canonical source/digest 获批后才进入模型、命令或外部连接面，且 provenance 可统一追溯。
23. Markdown Agent 的持久身份绑定定义内容与来源，Catalog 的所有构建入口产生相同冲突判定和稳定版本。
24. ACP/AG-UI 的 load、fork 和 turn failure 不会被替换为空 session 或正常完成，Tool start/completion 可稳定关联。
25. 所有 WirePermit 签发与校验都使用真实 canonical epoch，代际变化会撤销旧 permit。
26. Cron 的 receipt settlement、schedule transaction 和 scheduler fencing 在并发、崩溃与重启下不丢任务、不双触发。
27. 已被 API 接受的 Agent parked delivery 与其公开保证一致：durable 模式可恢复，best-effort 模式返回真实 disposition。
28. ModelGateway 的 finite operation 在 planning 期按真实 endpoint 能力准入，failover 只发生在投影兼容的 endpoint subset 内。
29. MCP reload 失败不破坏旧 catalog，动态 tool 名称不能静默覆盖，definition/catalog identity 随 canonical 内容稳定演进。
30. OAuth token/lock 不能逃离 storage root，凭据损坏、backend 切换与并发更新不会退化为匿名、回滚或双真相。
31. GenerationArtifact 的 domain payload 可严格演进；Kernel 的未来 recovery 扩展点不被误当作当前已交付能力或强制删除目标。
32. 浏览器 credential profile 不因名称碰撞、损坏或并发写退化为错误身份/匿名会话，且隐式写入不能绕过 permission。
33. AG-UI 的 human reply 不能跨 principal/session/turn 授权，Residency 并发 eviction/rehydration 不丢消息、不超 cap、不产生双 incarnation。
34. Hosted Service 的 caller cancel、deadline 和 shutdown 会结算 accepted receipt；支持远端取消时不会遗留无人管理的媒体作业，不支持时留下明确可恢复 disposition。
35. 多进程不能并发拥有同一个 Hosted Service call。
36. Workspace maintenance 不删除其他进程的活跃 session 或在用/staged Artifact；LSP/API 等未来 opt-in surface 可保留，默认未装配不作为删除条件。
37. RunJournal 在 started durable commit 失败时不会执行 EXTERNAL body；损坏 journal、reap 失败和多进程竞争不会造成同一 step 的无记录重跑、双 owner 或内存/磁盘分裂。
38. 所有生产 import 位于模块顶部或通过唯一 typed plugin loader 解析；optional backend 缺失不会污染核心 import，也不会到业务执行深处才以 `ModuleNotFoundError` 暴露。
39. Residency 磁盘记录不能选择可执行 Role/class/backend；materialize、rehydrate、forget 的 record revision 与 incarnation fence 能阻止旧 owner 覆盖或删除新 generation。
40. Workflow deadline、Cron occurrence、lease expiry 与 retention 在 restart、NTP 回拨/前跳及 DST fold/gap 下保持确定语义；旧 lease 不复活、occurrence 不重复、retention 不提前，测试使用注入 fake clock 而非系统时间或真实 sleep。
41. Workspace cleanup 只依据 canonical lifecycle/lease/pin/hold facts 和 fenced deletion claim；Artifact collector 使用完整 typed reachability 与 revisioned pin generation，且 TTL、用户删除、安全清除、legal hold、测试临时数据分别结算。
