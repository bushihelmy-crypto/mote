# Contracts Phase 0A Facts 生成视图

- 状态：Draft v10；当前为 bootstrap coverage seed，Phase 0A 后由 `contracts-facts.json` 确定性生成
- 基线日期：2026-07-29
- 覆盖：`mote/contracts/` 当前 182 个 Python 模块（以 Phase 0A 自动扫描为准）
- 主计划：[`contracts-package-governance-plan.md`](./contracts-package-governance-plan.md)

本文件在首次 Phase 0A 前仅作为 182 模块 coverage seed；`snapshot` 后必须由 `contracts-facts.json` 生成，展示事实状态、强制拆分规则与 Phase 0B 决策结果，不作为人工维护的 proposed 表。标记为 `split-contract` 的模块必须按符号生成 moves；标记为 `move-up` 的符号必须通过 projected graph 门禁。

受 AGENTS 硬约束，Protocol、事件数据、配置的目标路径分别位于 `contracts/ports/<domain>/`、`contracts/events/<domain>/`、`contracts/config/<domain>/`。任何无法判断的值必须显示 `fact_status=unknown`；Phase 0B 完成后本视图不得出现候选 owner、二选一、多 owner 或未知目标。

处置含义：

- `retain-contract`：契约语义保留；不表示当前路径保留，目标路径与 canonical facade 由 Phase 0B 决定。
- `split-contract`：当前模块包含多个 owner 或变化轴，按符号拆分。
- `move-up`：不是跨边界契约，迁到 Kernel/Runtime/Orchestration/Product 的实际 owner。
- `delete`：聚合门面或重复定义在全部消费者迁移后删除。
- `retain-package`：保留 Python package shell，但随每个 cutover 删除对应业务 re-export，终态只含包说明/版本。

“依赖边”只写目标态主要边，最终以字段级草图为准。“稳定身份”列说明必须盘点的兼容性资产；`—` 表示未发现持久化身份，但仍需 Phase 0A 扫描验证。

当前表格把若干事实压缩为人类可读摘要。机器逐符号 inventory 必须分开记录：

- ownership：`symbol/current_module/semantic_owner/target_layer/target_module/disposition/decision_status/decision_evidence`；
- evidence：`all_production_consumers/test_only_consumers/implementers/consumer_layers/lowest_consumer_layer/target_layer/legal_dependency_after_move/consumer_migration_closure`；
- compatibility：`public_api/wire_schema/persistent_identity/event_tag/error_code/config_compatibility/module_discriminator/protocol_signature/fixture_status`。

当前 seed 中的 owner、路径和处置只是待验证规则输入，所有行明确为 `fact_status=unknown`，不能据此启动迁移。首次 snapshot 后由 facts 替换状态；Protocol 必须以真实生产消费者和实现者证明需求方 owner。

## A. 根模块（28）

| 当前模块 | 处置 | 目标 owner | 主要依赖边 | 稳定身份 | 事实状态 |
| --- | --- | --- | --- | --- | --- |
| `contracts/__init__.py` | retain-package | 根包说明与版本；各 cutover 同步删除对应业务导出 | 无业务导出 | 公共 API 版本 | `fact_status=unknown` |
| `contracts/agents.py` | split-contract | `agent` | foundation | Agent ID/catalog identity | `fact_status=unknown` |
| `contracts/artifacts.py` | split-contract | `artifact` | content identity 内聚 | artifact ID/revision/retention | `fact_status=unknown` |
| `contracts/background_tasks.py` | split-contract | `task` 仅保留 TaskId、TaskResultPointer、outcome DTO；BuildContext、ServiceFactory、OutputLocationPort、wake/lifecycle、pool control move-up；Kernel 若需等待只定义窄 TaskObservationPort | agent/artifact/session 的窄 ID；禁止构建 service locator | task/session ID、result state；Any wait result 必须处置 | `fact_status=unknown` |
| `contracts/canvas.py` | retain-contract | `surface` | artifact ID | canvas operation/document version | `fact_status=unknown` |
| `contracts/completion.py` | split-contract | Output completion contract 归 `output`；Task terminal outcome 归 `task` | conversation/tool/task ID | completion kind；每个符号唯一 owner | `fact_status=unknown` |
| `contracts/content.py` | retain-contract | `content.identity` | foundation | digest/size/algorithm identity | `fact_status=unknown` |
| `contracts/execution.py` | split-contract | 持久化 execution operation/recovery DTO 按 0A fixture 证据归生成的 execution owner；含 Any 的 turn/results/rejection 参数必须收窄 | output/runtime | fencing/revision/frontier identity | `fact_status=unknown` |
| `contracts/handoff.py` | split-contract | 6E1 `interaction.handoff` 与 6E2 `runtime.handoff` 独立 cutover；6E3 等二者 verified 后删除旧模块 | interaction 依赖 3B；runtime handoff 依赖 6D | handoff ID/status/revision/fencing；共享旧文件不代表共享 owner | `fact_status=unknown` |
| `contracts/inference.py` | split-contract | provider-independent Model invocation/result 归 `model`；路由租约/fencing 与 Runtime 装配按逐符号 facts 拆分；所有 Any/dict wire shape 必须收窄 | model/tool/runtime | call/attempt/target lease/fingerprint | `fact_status=unknown` |
| `contracts/interaction.py` | split-contract | `interaction` | authorization response | question/request/answer identity | `fact_status=unknown` |
| `contracts/leases.py` | split-contract | Session run lease 归 `session`；Runtime resource lease 归 `runtime`；删除 generic lease 抽象 | foundation | lease ID/epoch/fencing/policy 分域 | `fact_status=unknown` |
| `contracts/model_actions.py` | split-contract | command/action discriminator 归 `tool` 或经持久化证据生成的 execution owner；模型响应 DTO 归 `model` | conversation/tool/output | Phase 0B 必须逐符号消除 multi-owner | `fact_status=unknown` |
| `contracts/net.py` | move-up | Phase 7D 与 `settings/sandbox.py` consumer closure 同切片迁 `runtime.sandbox.network` | sandbox config | pattern 文本若进入配置；禁止 Contracts → Runtime | `fact_status=unknown` |
| `contracts/notebook.py` | retain-contract | `surface` | artifact ID | cell/output kind | `fact_status=unknown` |
| `contracts/output.py` | split-contract | `output` | artifact | contract ID/version/run outcome | `fact_status=unknown` |
| `contracts/permissions.py` | split-contract | `authorization`；human choice 归 `interaction` | foundation | behavior/risk/decision/rule source | `fact_status=unknown` |
| `contracts/prompt.py` | split-contract | 稳定 PromptSection/identity 归 `conversation`；protocol vocabulary 归 `tool` 或 Kernel parser 的唯一事实 owner | conversation/tool | section/protocol version/fingerprint | `fact_status=unknown` |
| `contracts/resilience.py` | split-contract | Model failover DTO 归 `model`；Service retry DTO 归 `service`；breaker 算法/状态实现归 Runtime | foundation | 不生成 Resilience 横向领域 | `fact_status=unknown` |
| `contracts/run_context.py` | split-contract | run identity 归其事实 owner；DI container、组件引用与 lifecycle 全部 move-up Kernel/Runtime | agent/session ID | 禁止 service locator 进入 Contracts | `fact_status=unknown` |
| `contracts/runtimes.py` | split-contract | `runtime` | session/surface ID | runtime ID/epoch/revision/checkpoint version | `fact_status=unknown` |
| `contracts/serialization.py` | move-up | Phase 7E 与 document/env/gym consumer closure 同切片迁 `runtime.serialization` | schema consumers | `__module_class_name` 必须先迁移；禁止 Contracts → Runtime | `fact_status=unknown` |
| `contracts/service_journal.py` | retain-contract | `service` | service invocation/outcome；消除 model.failover 错误边，不依赖 session | call ID/state/receipt version | `fact_status=unknown` |
| `contracts/services.py` | split-contract | `service` | artifact identity；消除 model.failover 依赖 | service/endpoint ID、semantics | `fact_status=unknown` |
| `contracts/spawn.py` | split-contract | `agent` | conversation identity | lifecycle/context policy/spec | `fact_status=unknown` |
| `contracts/surfaces.py` | split-contract | `surface` | foundation | surface kind/ref/frame sequence | `fact_status=unknown` |
| `contracts/terminal.py` | retain-contract | `surface` | surface | terminal input discriminator | `fact_status=unknown` |
| `contracts/workflow_control.py` | move-up | 整体迁 `orchestration.workflows.control`；持久化 checkpoint 另建窄 DTO，不复用原模块 | — | 原模块不保留 | `fact_status=unknown` |
## B. Config（18）

| 当前模块 | 处置 | 目标 owner | 主要依赖边 | 稳定身份 | 事实状态 |
| --- | --- | --- | --- | --- | --- |
| `contracts/config/__init__.py` | retain-package | 仅包说明/版本，不聚合领域配置；随切片删除旧导出 | — | 公共 import 映射 | `fact_status=unknown` |
| `contracts/config/base.py` | split-contract | 各领域；通用 BaseModel 不下沉 foundation | — | extra-field policy | `fact_status=unknown` |
| `contracts/config/context.py` | retain-contract | `conversation.config` | conversation | 字段/default/schema | `fact_status=unknown` |
| `contracts/config/langfuse.py` | move-up | `product.config`/observability adapter | observability | 字段/default | `fact_status=unknown` |
| `contracts/config/llm.py` | split-contract | `model.config` | model/authorization secrets ID | provider/API type、字段/default | `fact_status=unknown` |
| `contracts/config/mcp.py` | split-contract | `product.config`；跨层 Tool transport DTO 留 `tool` | tool/authorization | transport enum、server config schema | `fact_status=unknown` |
| `contracts/config/model_failover.py` | retain-contract | `model.config` | model | 字段/default | `fact_status=unknown` |
| `contracts/config/models.py` | retain-contract | `config/model`；必要聚合由 `config/deployment` 引用 | model | 字段/default | `fact_status=unknown` |
| `contracts/config/multimodal.py` | split-contract | `model.config` 与 `service.config` | artifact | 字段/default | `fact_status=unknown` |
| `contracts/config/oauth.py` | move-up | `product.config`/runtime auth integration | authorization | grant/store/provider schema | `fact_status=unknown` |
| `contracts/config/observability.py` | split-contract | `observability.config`；provider 部分上移 Product | observability | 字段/default | `fact_status=unknown` |
| `contracts/config/resilience.py` | split-contract | `model.config`、`service.config` | model/service | 字段/default | `fact_status=unknown` |
| `contracts/config/routing.py` | retain-contract | `model.config` | model | route ID/字段/default | `fact_status=unknown` |
| `contracts/config/secrets.py` | move-up | `product.config` 与 runtime secret store | authorization | secret reference schema，禁止 secret value | `fact_status=unknown` |
| `contracts/config/sentry.py` | move-up | `product.config`/observability adapter | observability | 字段/default | `fact_status=unknown` |
| `contracts/config/tools.py` | split-contract | `config/tool`、`config/authorization`；聚合在 `config/deployment` | tool/conversation | 字段/default | `fact_status=unknown` |
| `contracts/config/ui.py` | move-up | `product.config` | surface | 字段/default | `fact_status=unknown` |
| `contracts/config/workspace.py` | move-up | `product.config`/runtime workspace | file/authorization | 字段/default | `fact_status=unknown` |
## C. Constants（4）

| 当前模块 | 处置 | 目标 owner | 主要依赖边 | 稳定身份 | 事实状态 |
| --- | --- | --- | --- | --- | --- |
| `contracts/constants/__init__.py` | delete | — | — | — | `fact_status=unknown` |
| `contracts/constants/context.py` | split-contract | `conversation.config`；算法默认值上移 Runtime | conversation | persisted config defaults | `fact_status=unknown` |
| `contracts/constants/messages.py` | split-contract | `conversation` | artifact/tool ID | wire field names | `fact_status=unknown` |
| `contracts/constants/tool_output.py` | retain-contract | `tool` | conversation | persisted-output marker | `fact_status=unknown` |
## D. Errors（15）

所有保留的稳定错误必须进入 `contracts-errors.toml`；没有稳定 code、wire namespace 或跨层恢复语义的异常直接 move-up，不允许仅因当前位于 `contracts/errors/` 而保留。

| 当前模块 | 处置 | 目标 owner | 主要依赖边 | 稳定身份 | 事实状态 |
| --- | --- | --- | --- | --- | --- |
| `contracts/errors/__init__.py` | delete | — | — | 公共 import 映射 | `fact_status=unknown` |
| `contracts/errors/artifacts.py` | retain-contract | `artifact` | foundation error root | error code | `fact_status=unknown` |
| `contracts/errors/base.py` | split-contract | 最小 error identity 可留 `foundation`；pickle/loguru cause sanitization 迁 Runtime logging | 标准库 | error code/recovery identity；禁止实现型 pickling 进入 Foundation | `fact_status=unknown` |
| `contracts/errors/codes.py` | split-contract | foundation code primitive + 各领域 code | foundation | error code/recovery action | `fact_status=unknown` |
| `contracts/errors/config.py` | move-up | `product.config` | — | CLI/config error code | `fact_status=unknown` |
| `contracts/errors/environment.py` | split-contract | 跨层稳定 Agent code 归 `agent`；Orchestration 内部异常 move-up | foundation | 稳定 code 必须登记 errors manifest | `fact_status=unknown` |
| `contracts/errors/graph.py` | split-contract | Kernel flow error move-up Kernel；Orchestration workflow error move-up Orchestration；仅稳定 wire code 可留对应 owner | — | 无稳定 code 的异常不留 Contracts | `fact_status=unknown` |
| `contracts/errors/models.py` | retain-contract | `model` | foundation | model error codes | `fact_status=unknown` |
| `contracts/errors/output.py` | retain-contract | `output` | foundation | output rejection/correction code | `fact_status=unknown` |
| `contracts/errors/report.py` | split-contract | error report DTO 归 foundation；render 上移 Product/Runtime | foundation | report wire schema | `fact_status=unknown` |
| `contracts/errors/routing.py` | retain-contract | `model` | foundation | routing error code | `fact_status=unknown` |
| `contracts/errors/runtimes.py` | retain-contract | `runtime` | foundation | runtime error code | `fact_status=unknown` |
| `contracts/errors/services.py` | retain-contract | `service` | foundation | service error code | `fact_status=unknown` |
| `contracts/errors/tasks.py` | retain-contract | `task` | foundation | task error code | `fact_status=unknown` |
| `contracts/errors/tools.py` | split-contract | `tool` 与 `authorization` | foundation | tool/protocol/auth error code | `fact_status=unknown` |
## E. Events（3）

| 当前模块 | 处置 | 目标 owner | 主要依赖边 | 稳定身份 | 事实状态 |
| --- | --- | --- | --- | --- | --- |
| `contracts/events/__init__.py` | retain-package | 仅包说明/版本，不导出基础 envelope；Session envelope canonical API 归 `events.session` | — | public import mapping | `fact_status=unknown` |
| `contracts/events/envelope.py` | retain-contract | `session` | content.identity | EventId/EventType/envelope schema、ContentDigest | `fact_status=unknown` |
| `contracts/events/types.py` | split-contract | 各事件所属领域 | 窄 ID/value | tag、payload version、fixture | `fact_status=unknown` |
## F. Fileops（7）

| 当前模块 | 处置 | 目标 owner | 主要依赖边 | 稳定身份 | 事实状态 |
| --- | --- | --- | --- | --- | --- |
| `contracts/fileops/__init__.py` | delete | `file` 精选 facade | — | public API snapshot | `fact_status=unknown` |
| `contracts/fileops/errors.py` | split-contract | `file` | foundation | error code | `fact_status=unknown` |
| `contracts/fileops/event_codec.py` | split-contract | schema/value 留 `file`，registry/dispatch 上移 Runtime | session | tag/version/decoder | `fact_status=unknown` |
| `contracts/fileops/events.py` | split-contract | `file` 子概念 | file identity | event tag/payload | `fact_status=unknown` |
| `contracts/fileops/models.py` | split-contract | `file` identity/view/mutation/search/recovery | artifact content ID | file identity/version/kind | `fact_status=unknown` |
| `contracts/fileops/ports.py` | split-contract | `file`，按消费者接口隔离 | file values | Protocol API | `fact_status=unknown` |
| `contracts/fileops/serialization.py` | split-contract | stable codec 留 `file`，通用执行上移 Runtime | file values | canonical payload schema | `fact_status=unknown` |
## G. Hooks 与 Introspection（5）

| 当前模块 | 处置 | 目标 owner | 主要依赖边 | 稳定身份 | 事实状态 |
| --- | --- | --- | --- | --- | --- |
| `contracts/hooks/__init__.py` | delete | Hook 业务 facade 仅在跨 Kernel/Runtime 证据成立时由 decisions 生成 | — | public API snapshot | `fact_status=unknown` |
| `contracts/hooks/invocation.py` | split-contract | 跨 Kernel/Runtime 的 invocation/outcome 保留为 hook contract；否则整体 move-up Runtime | authorization/tool ID | 0B 输出必须固定单一结果 | `fact_status=unknown` |
| `contracts/hooks/types.py` | split-contract | 稳定 invocation/outcome 随 hook contract；runner/registry/lifecycle move-up Runtime | authorization | behavior/outcome/event identity | `fact_status=unknown` |
| `contracts/introspection/__init__.py` | delete | 包壳随 docstring parser cutover 删除；不得建立横向 introspection facade | — | public import mapping | `fact_status=unknown` |
| `contracts/introspection/docstrings.py` | split-contract | 通用 tool spec/docstring parsing 归最低消费者 `kernel.tools.spec`；Workflow 特有解析可归 Orchestration | kernel/orchestration consumers；禁止 Kernel → Runtime | parser 行为 fixture | `fact_status=unknown` |
## H. Models（11）

| 当前模块 | 处置 | 目标 owner | 主要依赖边 | 稳定身份 | 事实状态 |
| --- | --- | --- | --- | --- | --- |
| `contracts/models/__init__.py` | delete | `model` facade | — | public API snapshot | `fact_status=unknown` |
| `contracts/models/capabilities.py` | retain-contract | `model` | foundation | capability names | `fact_status=unknown` |
| `contracts/models/constants.py` | split-contract | stable wire constant 留 `model`，实现默认值上移 Runtime | — | wire fields | `fact_status=unknown` |
| `contracts/models/failover.py` | split-contract | `model` | model endpoint | failure disposition/endpoint ID | `fact_status=unknown` |
| `contracts/models/invocation.py` | split-contract | `model` | conversation/tool/artifact ID | request/response mode | `fact_status=unknown` |
| `contracts/models/model_journal.py` | retain-contract | `model` | model invocation/result；不依赖 session | call/attempt ID/state/version | `fact_status=unknown` |
| `contracts/models/profile.py` | split-contract | profile value 留 `model`，profile registry/lookup 上移 Product/Runtime | model | profile ID/capabilities | `fact_status=unknown` |
| `contracts/models/responses.py` | retain-contract | `model` | tool/artifact ID | response/tool-call wire shape | `fact_status=unknown` |
| `contracts/models/routing.py` | split-contract | `model` | conversation/model endpoint | route/session state/decision | `fact_status=unknown` |
| `contracts/models/tokenization.py` | split-contract | 模型无关 token 估算归最低消费者 Kernel；价格表/cost 归 Runtime；usage DTO 另留 `model` | kernel routing、runtime cost；禁止 Kernel → Runtime | token estimate 行为、usage wire DTO | `fact_status=unknown` |
| `contracts/models/transport.py` | retain-contract | `model` | foundation | transport/API type | `fact_status=unknown` |
## I. Policy（6）

| 当前模块 | 处置 | 目标 owner | 主要依赖边 | 稳定身份 | 事实状态 |
| --- | --- | --- | --- | --- | --- |
| `contracts/policy/__init__.py` | delete | — | — | public import mapping | `fact_status=unknown` |
| `contracts/policy/compaction.py` | retain-contract | `conversation` | conversation | intent/decision/reason | `fact_status=unknown` |
| `contracts/policy/prompt.py` | split-contract | `conversation`/agent，按消费语义 | conversation | contribution/spec identity | `fact_status=unknown` |
| `contracts/policy/run_completion.py` | retain-contract | `output` | tool/model action ID | intent/decision | `fact_status=unknown` |
| `contracts/policy/spawn.py` | retain-contract | `agent` | agent identity | intent/decision/trace | `fact_status=unknown` |
| `contracts/policy/tool.py` | split-contract | `authorization` 与 `tool` result policy | tool | intent/decision/trace | `fact_status=unknown` |
## J. Ports（51）

端口的语义 owner 归“定义需求与抽象的一方”，不是实现方；物理路径统一位于 `contracts/ports/<domain>/`。以下 owner 均需在 Phase 0A/0B 以消费者列表复核。

| 当前模块 | 处置 | 目标 owner | 主要依赖边 | 稳定身份 | 事实状态 |
| --- | --- | --- | --- | --- | --- |
| `contracts/ports/__init__.py` | retain-package | 仅包说明/版本，不聚合 Protocol；随切片删除旧导出 | — | public import mapping | `fact_status=unknown` |
| `contracts/ports/agent_catalog.py` | retain-contract | `agent` | agent | Protocol API | `fact_status=unknown` |
| `contracts/ports/agent_factory.py` | retain-contract | `agent` | agent | Protocol API | `fact_status=unknown` |
| `contracts/ports/artifact_store.py` | split-contract | `artifact` | artifact | Protocol API/receipt | `fact_status=unknown` |
| `contracts/ports/canvas_backend.py` | retain-contract | `surface` | surface/artifact | Protocol API | `fact_status=unknown` |
| `contracts/ports/code_map.py` | move-up | 当前无 Kernel 生产消费者时整体上移 Product/Runtime；若 0A 发现 Kernel 证据，必须拆出窄 DTO/port 并在 0B 固定，不保留条件结果 | file | fact_status 驱动唯一决定 | `fact_status=unknown` |
| `contracts/ports/commit_fence.py` | split-contract | `session` 或具体事务领域 | session identity | fence token | `fact_status=unknown` |
| `contracts/ports/compaction_policy.py` | retain-contract | `conversation` | conversation policy | extension spec identity | `fact_status=unknown` |
| `contracts/ports/completion_policy.py` | retain-contract | `output` | output | Protocol API | `fact_status=unknown` |
| `contracts/ports/context_reducer.py` | retain-contract | `conversation` | conversation/model | Protocol API | `fact_status=unknown` |
| `contracts/ports/event_journal.py` | split-contract | `session` | event envelope | stream version/fact identity | `fact_status=unknown` |
| `contracts/ports/event_subscription.py` | split-contract | session subscription/checkpoint/dead-letter；observability lossy subscription；managed backend lifecycle move-up Runtime | event envelope/typed observation | durable cursor/retry/checkpoint 与 lossy overflow 分离 | `fact_status=unknown` |
| `contracts/ports/file_changes.py` | retain-contract | `file` | file event | Protocol API | `fact_status=unknown` |
| `contracts/ports/hook_runner.py` | move-up | Hook runner/registry/lifecycle 归 Runtime；Contracts 只保留稳定 invocation/outcome DTO | hook facts | 不保留 runner Protocol | `fact_status=unknown` |
| `contracts/ports/human_interaction.py` | retain-contract | `interaction` | interaction | Protocol API | `fact_status=unknown` |
| `contracts/ports/lease.py` | split-contract | `session`/`runtime` | lease value | epoch/fencing token | `fact_status=unknown` |
| `contracts/ports/llm_client.py` | retain-contract | `model` | model request/response | Protocol API | `fact_status=unknown` |
| `contracts/ports/lsp.py` | move-up | 无 Kernel 生产消费者则整体上移 Product/Runtime；Phase 0B 必须由 facts 固定目标 | file | 不以现存 Protocol 推定 Contracts owner | `fact_status=unknown` |
| `contracts/ports/message_activity.py` | retain-contract | `conversation` | conversation | Protocol API | `fact_status=unknown` |
| `contracts/ports/message_sink.py` | retain-contract | `conversation` | conversation | Protocol API | `fact_status=unknown` |
| `contracts/ports/message_store.py` | retain-contract | `conversation` | conversation | Protocol API | `fact_status=unknown` |
| `contracts/ports/model_call_journal.py` | retain-contract | `model` | model journal | call/attempt identity | `fact_status=unknown` |
| `contracts/ports/model_endpoint.py` | retain-contract | `model` | model endpoint/request | Protocol API | `fact_status=unknown` |
| `contracts/ports/model_gateway.py` | split-contract | `ports/model/inference.py` 窄 invocation/result；gateway 装配留 Runtime | model request/result | 禁止 resolver/fact sink/transformer service locator | `fact_status=unknown` |
| `contracts/ports/model_operator.py` | retain-contract | `model` | model | audit/control API | `fact_status=unknown` |
| `contracts/ports/model_request_transformer.py` | split-contract | Phase 0B 依据消费者生成唯一结果：Kernel 生产需求保留窄 Model transformation port；否则整体 move-up Runtime | model request；不得进入 ModelInferencePort service locator | 出口禁止条件表达或双目标 | `fact_status=unknown` |
| `contracts/ports/output.py` | split-contract | `output` | output | Protocol API | `fact_status=unknown` |
| `contracts/ports/output_migration.py` | retain-contract | `output` | output contract | migration edge/version | `fact_status=unknown` |
| `contracts/ports/prompt_policy.py` | split-contract | `conversation`/agent | conversation | extension spec identity | `fact_status=unknown` |
| `contracts/ports/request_assembler.py` | retain-contract | `conversation` | conversation | Protocol API | `fact_status=unknown` |
| `contracts/ports/resource_loader.py` | split-contract | 稳定 resource/artifact ID 与 reference 归 `artifact`；loader/factory/registry/lifecycle move-up Runtime/Product | artifact | Any 参数必须消除 | `fact_status=unknown` |
| `contracts/ports/routing.py` | retain-contract | `model` | model routing | Protocol API | `fact_status=unknown` |
| `contracts/ports/run_completion_policy.py` | retain-contract | `output` | output | extension spec identity | `fact_status=unknown` |
| `contracts/ports/run_lease.py` | retain-contract | `session` | runtime/session ID | lease epoch/fence | `fact_status=unknown` |
| `contracts/ports/runtime_checkpoint.py` | retain-contract | `runtime` | runtime | checkpoint version | `fact_status=unknown` |
| `contracts/ports/runtime_driver.py` | split-contract | `ports/runtime` 基础端口；Interaction adapter 在上层 composition | runtime/surface/handoff | Protocol API | `fact_status=unknown` |
| `contracts/ports/runtime_handoff.py` | retain-contract | `ports/runtime/handoff` | runtime checkpoint | handoff ID/revision | `fact_status=unknown` |
| `contracts/ports/runtime_operation.py` | retain-contract | `runtime` | runtime | operation ID/state | `fact_status=unknown` |
| `contracts/ports/runtime_projection.py` | retain-contract | `runtime` | runtime/artifact ID | projection intent/ack | `fact_status=unknown` |
| `contracts/ports/service_call_journal.py` | retain-contract | `service` | service journal | call ID/state | `fact_status=unknown` |
| `contracts/ports/service_endpoint.py` | retain-contract | `service` | service descriptor | Protocol API | `fact_status=unknown` |
| `contracts/ports/service_gateway.py` | retain-contract | `service` | service invocation | Protocol API | `fact_status=unknown` |
| `contracts/ports/session_facts.py` | retain-contract | `session` | event envelope | Protocol API | `fact_status=unknown` |
| `contracts/ports/skills.py` | split-contract | Skill ID/version 归 `agent`，artifact reference 归 `artifact`；reload/factory/registry/config/lifecycle move-up Product/Runtime | artifact | 消除全部 Any；禁止 service locator | `fact_status=unknown` |
| `contracts/ports/spawn_policy.py` | retain-contract | `agent` | agent policy | extension spec identity | `fact_status=unknown` |
| `contracts/ports/surface_presenter.py` | retain-contract | `surface` | surface | Protocol API | `fact_status=unknown` |
| `contracts/ports/team_roster.py` | retain-contract | `agent` | agent identity | roster member identity | `fact_status=unknown` |
| `contracts/ports/telemetry.py` | split-contract | 删除 `event: object`；跨层值改为 typed observation 或 bounded envelope；backend/subscription lifecycle move-up Runtime | foundation/typed observation | 禁止 arbitrary object | `fact_status=unknown` |
| `contracts/ports/tool_policy.py` | split-contract | `authorization` 与 `tool` result policy | tool | extension spec identity | `fact_status=unknown` |
| `contracts/ports/turn_context.py` | retain-contract | `conversation` | conversation | source name/priority | `fact_status=unknown` |
| `contracts/ports/window_surface.py` | retain-contract | `surface` | surface | Protocol API | `fact_status=unknown` |
## K. Schema（8）

| 当前模块 | 处置 | 目标 owner | 主要依赖边 | 稳定身份 | 事实状态 |
| --- | --- | --- | --- | --- | --- |
| `contracts/schema/__init__.py` | delete | — | — | lazy public mapping/migration map | `fact_status=unknown` |
| `contracts/schema/context.py` | split-contract | `conversation` | conversation | token/fold state、config schema | `fact_status=unknown` |
| `contracts/schema/document.py` | split-contract | Conversation document reference 归 `conversation`；Artifact reference 归 `artifact`；可变 document 实现 move-up | artifact | CauseBy/document/resource fixture 决定保留 DTO | `fact_status=unknown` |
| `contracts/schema/env.py` | split-contract | environment 实现整体 move-up Product/Orchestration；仅有历史 fixture 的 discriminator DTO 留其事实 owner | conversation | polymorphic discriminator 必须登记 | `fact_status=unknown` |
| `contracts/schema/gym_env.py` | split-contract | Gym/RL 实现整体 move-up Product/Orchestration；仅有历史 fixture 的 discriminator DTO 留其事实 owner | agent | polymorphic discriminator 必须登记 | `fact_status=unknown` |
| `contracts/schema/messages.py` | split-contract | `conversation` | artifact/tool ID | message kind/content fields | `fact_status=unknown` |
| `contracts/schema/queue.py` | split-contract | queue DTO 归 conversation；可变 queue 实现上移 Runtime | conversation | priority/queued-message shape | `fact_status=unknown` |
| `contracts/schema/tool_config.py` | split-contract | `tool.config`、conversation spill config、Runtime durable config | tool/conversation | config fields/defaults | `fact_status=unknown` |
## L. Settings（8）

| 当前模块 | 处置 | 目标 owner | 主要依赖边 | 稳定身份 | 事实状态 |
| --- | --- | --- | --- | --- | --- |
| `contracts/settings/__init__.py` | delete | — | — | public import mapping | `fact_status=unknown` |
| `contracts/settings/device.py` | move-up | Product browser/device config | authorization | config schema/defaults | `fact_status=unknown` |
| `contracts/settings/hooks.py` | move-up | Hook registry/loading 配置归 Runtime/Product；稳定 invocation/outcome 不携带加载配置 | hook | config schema/defaults fixture 后上移 | `fact_status=unknown` |
| `contracts/settings/lsp.py` | move-up | Product LSP config | file | config schema/defaults | `fact_status=unknown` |
| `contracts/settings/permissions.py` | split-contract | `config/authorization`；sandbox 实现配置上移 Runtime | authorization | mode/rule/defaults | `fact_status=unknown` |
| `contracts/settings/sandbox.py` | split-contract | Runtime sandbox config；合格静态 DTO 可由 `config/deployment` 引用 | authorization | sandbox mode/domain credential schema | `fact_status=unknown` |
| `contracts/settings/watching.py` | split-contract | Runtime watching config；跨层 FileChange contract 留 file | file | config schema/defaults | `fact_status=unknown` |
| `contracts/settings/web_search.py` | move-up | Product web-search config | service/model | config schema/defaults | `fact_status=unknown` |
## M. Text（11）

| 当前模块 | 处置 | 目标 owner | 主要依赖边 | 稳定身份 | 事实状态 |
| --- | --- | --- | --- | --- | --- |
| `contracts/text/__init__.py` | delete | — | — | public import mapping | `fact_status=unknown` |
| `contracts/text/ansi.py` | split-contract | terminal stream 处理归 Runtime；CLI styling 归 Product | — | 逐函数唯一目标 | `fact_status=unknown` |
| `contracts/text/elision.py` | split-contract | 稳定 marker 归 `conversation`；cap/elision 算法归其最低消费者 Kernel/Runtime | — | model-facing marker shape | `fact_status=unknown` |
| `contracts/text/hashing.py` | split-contract | Runtime file/code-map | artifact | digest algorithm/version | `fact_status=unknown` |
| `contracts/text/html.py` | split-contract | parsing/content adapter 归 Runtime；presentation/rendering 归 Product | — | 逐函数唯一目标 | `fact_status=unknown` |
| `contracts/text/humanize.py` | move-up | Product presentation | — | — | `fact_status=unknown` |
| `contracts/text/hunks.py` | split-contract | Hunk value 归 file；apply/revert/diff 上移 Runtime fileops | file | hunk payload/line convention | `fact_status=unknown` |
| `contracts/text/markers.py` | split-contract | system reminder 归 conversation；persisted output 归 tool | conversation/tool | marker literals | `fact_status=unknown` |
| `contracts/text/paths.py` | split-contract | URI wire value/spec 归 surface/file；display helper 上移 Product | file/surface | URI format | `fact_status=unknown` |
| `contracts/text/plural.py` | move-up | Product/Runtime message owner | — | — | `fact_status=unknown` |
| `contracts/text/whitespace.py` | split-contract | Phase 0A 按逐函数生产消费者计算最低合法 owner；Phase 0B 每个函数固定唯一 target | — | 禁止“各实际 owner”进入 decisions | `fact_status=unknown` |
## N. Tools（8）

| 当前模块 | 处置 | 目标 owner | 主要依赖边 | 稳定身份 | 事实状态 |
| --- | --- | --- | --- | --- | --- |
| `contracts/tools/__init__.py` | delete | `tool` facade | — | public API snapshot | `fact_status=unknown` |
| `contracts/tools/calls.py` | split-contract | `tool` | foundation | call ID/argument wire shape | `fact_status=unknown` |
| `contracts/tools/catalog.py` | split-contract | Tool catalog identity/materialized definitions/binding snapshot 归 `tool`；execution port 按需求方 owner 进入 `ports/tool`；Any schema/result 必须收窄 | tool/model/runtime | catalog/snapshot/revision/fingerprint | `fact_status=unknown` |
| `contracts/tools/constants.py` | retain-contract | `tool` | foundation | protocol constants | `fact_status=unknown` |
| `contracts/tools/effects.py` | retain-contract | `tool` | foundation | effect kind | `fact_status=unknown` |
| `contracts/tools/execution.py` | retain-contract | `tool` | foundation | ToolExecutionKind；跨 Kernel/Runtime/Product 的稳定分类 | `fact_status=unknown` |
| `contracts/tools/identity.py` | split-contract | `tool` | foundation | toolset/tool identity/manifest | `fact_status=unknown` |
| `contracts/tools/protocol.py` | split-contract | 稳定 Tool semantic schema 归 `tool`；XML/native command projection 归 Kernel parser | model | command protocol/schema identity | `fact_status=unknown` |
## O. Phase 0B 强制决策队列

本文件当前逐项覆盖 182 个 Python 模块。首次 Phase 0A `snapshot` 以文件系统为事实来源，允许重建陈旧 seed，并确定性报告 added/removed/content-identical-moved/symlink/mode diff；seed mismatch 不得使 snapshot 本身无法完成。差异必须经受审提交更新本生成视图；在该提交完成前，后续 `check` 必须失败。此后新增、遗漏、重复或未受审 seed 漂移均失败；文档不把数量本身当作永久常量。

该队列不是开放评审热点，而是 Phase 0B 必须清零的机器任务：

1. 将每个 `fact_status=unknown` 补齐证据；Phase 0B 总出口要求 unknown 为零，任一残留项都会阻塞 0C 及全部迁移阶段。
2. 按表中强制拆分规则为 completion、lease、resilience、execution/model actions、run context、errors、Hook/Telemetry、LSP/code map、skills/resource、schema/text/tool protocol 生成逐符号唯一 moves。
3. 将所有稳定 error code 写入 `contracts-errors.toml`；没有稳定 code 的异常一律 move-up。
4. 为 Model request transformer、Service 的 Artifact/Content 引用、Task 字段依赖等事实驱动项写入唯一目标和精确 DAG 边。
5. 清零 `undecided_symbols/candidate_domains/candidate_targets/multi_owner_symbols/illegal_move_up/unowned_legacy_paths`。

队列非空时阻塞 Phase gate 0B，并因 0C 依赖 0B 而阻塞全部后续阶段；它不触发重新设计主计划。Markdown 与 manifests 不一致时治理 CLI 返回 4。
