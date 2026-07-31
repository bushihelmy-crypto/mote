# ADR-0006：BgGraph 迁移、恢复查询与 Tool 执行契约

- 状态：Accepted
- 日期：2026-07-29
- 决策 owner：Workflow、Background Tasks、Product Workflows、Runtime Tools 联合评审

## 1. 背景

现有 `bggraph` 同时承载：

1. 通用图拓扑、并行执行、暂停和恢复。
2. 模型可见 `run_graph` GraphSpec、Tool dispatch 和展示。
3. 手写 `BgGraph.compile()` 到 BackgroundTaskPool 的 deferred pipeline。
4. `resume_tasks/get_node_state` 通过 Pool TaskMeta 私有字段查询和恢复 Workflow。
5. 通过扫描 Tool 实例字段上的动态 marker 识别 pipeline Tool。

ADR-0001 至 ADR-0005 已确定通用 Workflow、后台任务和 Product adapter 的方向，但尚未规定旧 `bggraph` 的完整迁移契约。本 ADR 只闭合该迁移，不重新定义五层架构。

## 2. Owner 切分

### 2.1 `orchestration.workflows`

拥有：

- `WorkflowBuilder -> WorkflowDefinition -> WorkflowRun`。
- Operation/Node callable、NodeDefinition、结构化 InputRef/StateRef。
- frozen topology、channels/reducers、执行、retry、pause、resume、continuation。
- immutable RunSnapshot 和结构化 WorkflowEvent。

禁止依赖：

- ToolResult、Tool catalog、BaseTool、AgentControl。
- GraphSpec、模型 JSON Schema、Tool-style docstring metadata。
- BackgroundTaskPool、TaskMeta 或 Product presentation。

### 2.2 `product.workflows.run_graph`

拥有：

- 模型可见 GraphSpec、binding grammar、predicate、map/fold/compute DSL。
- Tool/Agent operation compiler 和 live Tool catalog validation。
- docstring/Annotated/From adapter、JSON Schema 和参数描述。
- graph-in-graph/excluded Tool policy。
- `run_graph` Tool、activity projection 输入和模型文本。
- `resume_tasks/get_node_state` Tool facade。

Agent Tool 与普通 Tool 都编译为 operation；Workflow 内核不知道 operation 是否 Spawn Agent。

### 2.3 `product.workflows`

拥有跨 Workflow/Background 的代码 adapter：

- WorkflowTaskAdapter。
- WorkflowContinuationRegistry。
- WorkflowInspectionPort 实现。
- WorkflowOutcome -> OperationOutcome 转换。
- resume reference 到新 DeferredOperation 的映射。
- Workflow decision/interaction request 到 Agent turn context 的交付。

Product composition 只负责按 session 创建、注入和关闭这些 adapter，不收纳其实现。

## 3. Continuation 与 Inspection 契约

### 3.1 稳定类型

```text
ResumeRef
  version
  session_scope
  workflow_definition_id
  source_run_id
  continuation_id

RunRef
  version
  session_scope
  workflow_definition_id
  run_id

RunSnapshot (immutable)
  run_ref
  workflow status
  definition summary
  tuple[NodeSnapshot]
  tuple[StateFieldSnapshot(name, immutable_value | value_ref, visibility)]
  allowed resume actions
```

`ResumeRef`/`RunRef` 的 wire form 是 opaque string；调用方不能解析字段作决策。Registry 必须验证版本和 session scope，防止跨 session 查询或恢复。

### 3.2 ResumeCapability 与终态

Workflow 终态显式区分：

```text
WorkflowSucceeded(output)
WorkflowPaused(reason, ResumeCapability)
WorkflowFailed(error, recoverability, ResumeCapability?)
WorkflowCancelled(reason, ResumeCapability?)
WorkflowTimedOut(reason, ResumeCapability?)
```

`ResumeCapability` 是 Workflow owner 签发的恢复动作能力，不等同于所有非成功 outcome 都可恢复。它携带 `allowed_actions`；局部恢复需要一致 checkpoint，只有 FULL_RESTART 时 checkpoint 可以为空，但必须保存 definition identity、execution revision 和 initial input reference：

- `Paused`：frontier 已停止、siblings 已 cancel/join，必须携带 capability。
- `Failed`：执行期 node/batch failure 在冻结一致 snapshot 后可携带局部恢复能力；若无安全 checkpoint 但 Definition 与 initial input 有效，可携带仅允许 FULL_RESTART 的 capability。Definition/build/topology/schema/identity mismatch 等 fatal failure 不得携带。
- `TimedOut`：先取消并 join 全部 sibling/node task，再冻结 snapshot；只有 operation effect policy 允许 replay/resume 时携带。
- `Cancelled`：用户/task cancel 默认通过协作式 CHECKPOINT stop，在完成 cancel/join 后生成局部恢复或 restart-only capability；明确 DISCARD/force-abort 不生成。
- output commit/terminal contract failure 是否可恢复由 Definition 的 terminal recovery policy 显式声明，默认不可恢复。

Background-owned outcome 对应为：

```text
OperationSucceeded
OperationPaused(opaque_resume_ref)
OperationFailed(failure, opaque_resume_ref?)
OperationCancelled(reason, opaque_resume_ref?)
OperationTimedOut(reason, opaque_resume_ref?)
```

Pool 只保存 optional opaque ref，不解释 recoverability、node state 或 resume action。

允许动作：

| 动作 | 条件 |
| --- | --- |
| `from_nodes` | 有一致 snapshot；指定 nodes 存在；upstream feasibility 校验通过 |
| `skip_nodes` | 有一致 snapshot；Definition/Node policy 允许 skip |
| `from + skip` | 同时满足上述条件 |
| full restart | Definition 仍注册且 initial input 可用；创建全新 Run，不复活旧 operation |

因此 `resume_tasks` 仍只需要读取一个 opaque ResumeRef：不能局部恢复但允许从头重启的 outcome 也会携带 restart-only ResumeCapability。checkpoint 已显式 discard 后不允许借旁路 restart；调用方应重新发起顶层 Tool 调用。fatal identity/schema/definition failure 不签发任何 capability。

Workflow 内部 node retry budget 属于 WorkflowRun/Definition retry policy；跨终态 resume/restart budget 属于 `product.workflows` 的 ResumePolicy/Registry entry。BackgroundTaskPool 不保存 graph retry_count/max_restarts。

### 3.3 WorkflowContinuationRegistry

```text
register(resume_capability, snapshot, resume_policy) -> ResumeRef
consume(resume_ref, ResumeRequest) -> WorkflowRun
inspect(run_ref | resume_ref) -> RunSnapshot
discard(resume_ref)
retire_inspection(run_ref)
aclose()
```

语义：

- Registry owner 是 `product.workflows`，实例 scope 是一个 Agent session。
- Continuation 默认 single-consume；`consume` 原子地把 LIVE ref 变为 CONSUMED。
- 第二次 consume 返回稳定 `AlreadyConsumed`，不能重复执行。
- consume 校验 ResumeRequest 后返回新 WorkflowRun，并登记新的 RunRef；旧 ResumeRef 永久失效。
- resume 再次暂停时登记新的 ResumeRef，不复用旧 ref。
- Task result presentation 被消费只停止重复投影，不 retire ResumeRef 或 RunSnapshot。
- ResumeRef 仅在 consume、明确 cancel-and-discard、retention expiry 或 session shutdown 时失效。
- RunSnapshot 使用独立 inspection retention；ResumeRef 和 RunRef 不共享 GC deadline。
- task metadata GC 前必须通知 Registry 解除 task/reference ownership；被 live task outcome/reference 持有的 entry 不得提前回收。
- expiry 返回稳定 `ResumeExpired`；与 `AlreadyConsumed`、`InteractionExpired`、`ResumeUnavailableAfterRestart` 区分。
- inspection 只返回 immutable snapshot，不能返回 Definition、Run、Continuation 或可变 state object。
- BackgroundTaskPool 只保存 opaque RunRef/ResumeRef 和 background-owned OperationOutcome，不保存 registry object。

### 3.4 Snapshot 投影、深不可变与可见性

Workflow 定义稳定 `SnapshotValueProjector`：

```text
safe scalar/container       -> recursive immutable value
large serializable value    -> stable value_ref
sensitive value             -> RedactedValue(reason)
unsupported runtime object  -> UnavailableValue(type_tag, reason)
```

- inspection projection 与 resume checkpoint state 是两个契约：前者允许 redacted/unavailable；后者必须完整覆盖 Definition 声明的恢复必需字段。
- Snapshot 从 live state 投影而不是盲目 `deepcopy()`；构造后不随 Run 继续变化。
- 只包含 frozen DTO、tuple、frozenset、标量和递归深冻结 mapping；不保存 list/dict/Pydantic live state。
- 不保存 callable、exception object、Run、Definition、Continuation 或 capability instance。
- 大字段保存稳定 value/result reference，inspection query 显式选择字段和大小上限。
- 每个 StateFieldSnapshot 携带 visibility；敏感/secret/internal 字段默认不可由 `get_node_state` 读取。
- Product presentation 只能渲染 query 已授权返回的 snapshot，不得绕过 visibility 访问 registry internals。
- 单个 inspection 字段不可投影不得阻止 Workflow 暂停，使用 UnavailableValue；但任何恢复必需字段无法安全冻结时，不得签发局部 ResumeCapability，只能在 Definition/initial input 仍有效时签发 restart-only capability，否则终态无 ResumeRef。

### 3.5 协作式停止与终态线性化

BackgroundTaskPool 按 ADR-0001 调用 `request_stop(reason, disposition)`，WorkflowTaskAdapter 将停止请求交给 WorkflowRun：

1. 停止接收新节点，取消并 join 已启动节点。
2. CHECKPOINT 尝试冻结恢复必需状态并登记 ResumeCapability；DISCARD 跳过登记。
3. adapter 把 WorkflowCancelled/TimedOut 转成带 optional opaque ResumeRef 的 background-owned outcome。
4. operation 内部原子发布唯一终态，使并发 execute/request_stop 获得同一结果。
5. 超过 Pool grace period才允许强制 cancel；该终态没有 ResumeRef。

Pool shutdown 默认 `SHUTDOWN + DISCARD`，因为首版 registry 在进程退出后不可恢复；user cancel/timeout 默认 CHECKPOINT。Pool timeout 不再同时使用 `asyncio.wait_for` 取消 execute。

### 3.6 首版 durability

首版 ContinuationRegistry 明确是 **process-local、session-scoped**：

- conversation compaction/result spill 不影响 registry；opaque ref 可随 TaskResultPointer 保存并重新投影。
- 进程重启后旧 ref 返回稳定 `ResumeUnavailableAfterRestart`，不暗示 crash-safe resume。
- ref 本身持久化不等于 continuation 可恢复。
- 在没有 stable DefinitionResolver + versioned RunSnapshot store 前，禁止宣称跨进程 resume。

若未来建设 durable continuation，必须新增 ADR，定义 DefinitionResolver、snapshot migration、operation reconciliation 和 effect safety。

## 4. Tool 查询和恢复调用链

### 4.1 `resume_tasks`

```text
resume_tasks(task_id, request)
  -> BackgroundTaskQuery.get_outcome(task_id)
  -> outcome.opaque_resume_ref
  -> WorkflowContinuationRegistry.consume(ref, request)
  -> WorkflowTaskAdapter.defer(new_run)
  -> BackgroundTaskPool.resubmit(task_id, DeferredOperationFactory)
```

要求：

- Tool 不读取 TaskMeta、graph_meta、run_state、state_snapshot 或 graph._nodes。
- ResumeRequest 的 node existence、override fields 和 upstream feasibility 由 Workflow continuation/definition owner 校验。
- Pool resubmit 只接收 factory；每次生成全新 single-use operation。

### 4.2 `get_node_state`

```text
get_node_state(task_id, query)
  -> BackgroundTaskQuery.get_outcome(task_id)
  -> outcome.opaque_run_ref/resume_ref
  -> WorkflowInspectionPort.inspect(ref)
  -> immutable RunSnapshot
  -> Product presentation
```

Pool 不是 Workflow 查询数据库。Node description、input source、consumer、attempt、error 和 state field view 全部来自 snapshot/inspection contract。

## 5. BaseNode 拆分

当前 BaseNode 不整体迁入 Workflow 内核。

通用内核保留：

```text
Operation = async callable
NodeDefinition
InputRef / StateRef
NodeResult / state updates
retry/timeout metadata
```

Product RunGraph adapter 拥有：

- `Params:` docstring parsing。
- `Annotated[..., From(...)]` syntax adapter。
- JSON Schema generation。
- `kernel.tools.spec_adapter` 使用。
- 模型可见 description/parameter metadata。

手写 Python Workflow 若需要 typed binding，使用显式 InputRef/StateRef，不通过 Tool-style docstring 隐式生成核心执行契约。

## 6. Tool ExecutionKind

删除 `_is_bg_pipeline_executor` marker、实例字段扫描和 `getattr` 分类。Tool definition 增加名义化执行分类：

```text
ToolExecutionKind
  ATOMIC
  WORKFLOW_FOREGROUND
  WORKFLOW_DEFERRED
```

所有权：

- Enum 是 Runtime 与 Product 共享的稳定 Tool contract，放在 `contracts.tools`。
- Kernel ToolDefinition 携带 frozen `execution_kind` 字段，默认 ATOMIC。
- Product Tool definition/catalog 显式声明类别。
- Runtime ToolLifecycle/ToolCatalog 只读取 definition 字段，不扫描 capability instance。

语义：

- `run_graph` 是 WORKFLOW_FOREGROUND。
- 手写 compiled workflow Tool 是 WORKFLOW_DEFERRED，并显式提供 DeferredOperationFactory。
- pipeline enable gate 同时控制两种 WORKFLOW 类别。
- graph-in-graph 策略按 execution_kind 拒绝 WORKFLOW_FOREGROUND/DEFERRED 节点。
- 普通 Tool 保持 ATOMIC。
- `prefixed()`、`renamed()`、copy/replace、namespace/toolset composition 和 dynamic toolset materialization 必须原样传播 execution_kind。
- alias definition 的 execution_kind 必须与 primary definition 一致；catalog 注册时不一致即失败。
- MCP Tool 和未声明 Workflow 语义的默认 function Tool 明确归类为 ATOMIC。
- execution_kind 是本地执行契约，不进入模型 wire schema；不得因此改变 Tool JSON Schema。
- WORKFLOW_DEFERRED 注册时必须同时提供 DeferredOperationFactory，遗漏属于构造错误。
- ATOMIC Tool 若在运行时返回 DeferredOperation 属于稳定的执行契约错误，Runtime 不得猜测并自动升级分类。

这不是给 `BaseTool` 增加可被遗忘的布尔值；分类属于 immutable ToolDefinition/catalog registration。

## 7. 两条执行路径

| 行为 | 前台 `run_graph` | 后台 compiled Workflow |
| --- | --- | --- |
| 定义来源 | 模型 GraphSpec，经 Product compiler | 手写 WorkflowBuilder/Definition |
| 执行 owner | 顶层 Tool call | BackgroundTaskPool operation |
| Task ID | 无 | 有 |
| Approval/AskUser | Tool node 可直接复用当前 live 交互通道 | 必须支持；显式 interaction/decision edge 暂停并释放 worker，由 Agent turn 完成交互后 resume |
| Resume | 顶层 Tool crash restore/replay；普通失败由模型重新调用 | opaque continuation + registry |
| Progress | WorkflowEvent -> Product activity projector | WorkflowEvent -> task adapter -> TaskEvent/projector |
| Cancellation | 顶层 Tool call | Pool 独占 operation 并 close |
| Effect ledger | 一个顶层 EXTERNAL Tool receipt | task lifecycle；operation 自身按声明的 effect policy |
| Node inspection | Product activity/RunSnapshot | registry-owned RunSnapshot |

## 8. 后台 Interaction/Decision Continuation

人类/模型路由是首版必须能力，不是未来扩展。后台 Workflow 不应让 Pool worker 同步阻塞在人类输入上，而应把交互建模为结构化暂停：

```text
node completes
  -> DecisionEdge / InteractionEdge
  -> WorkflowOutcome.Paused(
       reason=DECISION_REQUIRED | HUMAN_INPUT_REQUIRED | APPROVAL_REQUIRED,
       interaction_request,
       continuation,
     )
  -> WorkflowTaskAdapter registers continuation
  -> OperationPaused(opaque_resume_ref, interaction_ref)
  -> Pool releases execution slot
  -> Product projects request into Agent turn
  -> model/human decides
  -> resume_tasks -> registry.consume -> new operation
```

### 8.1 通用 Decision 模型

`orchestration.workflows` 只拥有 capability-agnostic 决策语义：

```text
DecisionPoint
  decision_id
  DecisionRequest(prompt_ref/payload_ref)
  tuple[DecisionOption(stable_key, result_binding)]
  DecisionBinding
  DecisionPolicy(required, timeout, missing_result_action)
  InteractionContinuation
```

Workflow 内核只知道需要外部决策、稳定 option keys、result binding、timeout/missing-result policy 和 continuation。它不 import `contracts.interaction.AskUserQuestionInput`、`contracts.permissions.ApprovalRequest`、Product human channel 或 allow/deny 展示语义。

手写 Workflow 使用显式 API：

```text
builder.add_decision(...)
builder.add_operation(..., interaction_policy=...)
```

无法声明交互性质的 deferred operation 才在后台提交边界 fail-closed 拒绝。

### 8.2 Product Decision adapters

`product.workflows` 拥有：

```text
ModelRouteAdapter
HumanQuestionAdapter        # AskUserQuestionInput/Answers
ApprovalDecisionAdapter     # ApprovalRequest + allow/deny policy
```

Product compiler 把 HumanQuestion/Approval 编译为通用 DecisionPoint：

- Human answer 映射为 typed DecisionBinding。
- Approval 编译为 `required=True`、`missing_result_action=FAIL`，没有明确 allow 时不能进入允许分支。
- Product adapter 决定请求进入 Agent turn、human channel 或具体 Interface。
- 通用 Workflow snapshot/event 只保存 decision ID、option key 和 opaque payload/result refs。

### 8.3 Product adapter 的 Agent/model 行为

- Product 将通用 DecisionRequest 投影为 model-route 请求后，模型可以直接调用 `resume_tasks` 选择 route。
- 模型也可以先调用 `AskUserQuestion` 获取人类判断，再携带答案/route 调用 `resume_tasks`。
- 模型可以暂时不调用任何工具；Workflow 保持 PAUSED，不占 Pool semaphore，不自动选择默认路由。
- Product 的 human-question projection 可以由 interaction port 直接呈现；回答写入 ResumeRequest 的 typed answer binding。
- Product 的 approval projection 必须保持 fail-closed；没有明确 allow 不能进入允许分支。

### 8.4 并发、取消与生命周期

- 进入 interaction pause 前取消并 join 其他仍运行的 sibling node task，冻结一致 RunSnapshot。
- interaction request 与 ResumeRef 同属 ContinuationRegistry entry；consume/retire/shutdown 原子处理二者。
- 同一 request 只接受一个成功 answer/route；并发回答只有一个 consume 成功。
- 普通 task cancel 在生成可恢复 checkpoint 后保留 ResumeRef/interaction request；只有显式 cancel-and-discard、continuation expiry 或 session shutdown 才撤销未决 request，使迟到回答返回稳定 `InteractionExpired`。presentation retirement 不产生该效果。
- session shutdown 清理未决请求；首版进程重启后返回 `ResumeUnavailableAfterRestart`。
- 可选 interaction timeout 必须在 Definition 中显式声明；超时映射到 fail-closed route 或 terminal failure，禁止隐式选择 allow/default branch。

### 8.5 直接交互 Tool node

- 前台 `run_graph` 继续允许 ToolExecutor 内的 AskUserQuestion/approval 直接等待 live channel。
- 后台 Product DSL 中，声明为需要交互的 operation 由 Product compiler/adapter 转换为 DecisionPoint；不得占用 Pool worker 无限等待。
- 手写 Workflow 通过 `add_decision` 或 operation `interaction_policy` 显式声明，不依赖 Product compiler。
- 无法声明交互性质的 deferred operation 按 fail-closed 拒绝进入后台，错误必须指出应使用 decision API。

这保留现有 LLM route 的“暂停 -> 通知 -> resume”本质，并把 AskUserQuestion/approval 纳入同一受治理 continuation，而不是删除后台交互能力。

## 9. `run_graph` 副作用与崩溃恢复不变量

Phase 1 必须保持：

1. 图内 Tool/Agent dispatch 继续通过 ToolExecutor chokepoint，权限、hook 和 observability 不旁路。
2. 图内 dispatch 不传顶层 `result_id`，不为节点创建 durable tool_call/effect-ledger entry。
3. 不产生 durable history 无法配对、reap 的逐节点 `started` ledger。
4. 整个 `run_graph` 仍是唯一顶层 EXTERNAL Tool receipt 和崩溃恢复单元。
5. 顶层 receipt 可由现有 session reconcile/replay 处理。
6. Approval/AskUser 在前台 graph 中继续使用当前交互通道。
7. 顶层取消会 join/close 所有节点 task，不遗留后台执行。

该保证不是节点外部副作用的 exactly-once：顶层 `run_graph` 有 durable receipt，但进程可能在节点外部副作用完成后、顶层 receipt commit 前崩溃，replay 因而可能再次执行该节点，当前语义是 at-least-once。未来若要求增强，必须另行设计 workflow operation effect key/journal；禁止复用顶层 `result_id` 给节点制造伪 exactly-once。

后台 compiled Workflow 不继承“顶层 run_graph 单 receipt”这一特例；其 effect/retry 安全必须由 operation declaration 和后台 task policy 明确，不能盲目重放未知 EXTERNAL operation。

## 10. Definition identity

### Product RunGraph

```text
definition_id = digest(
  compiler_schema_version,
  canonical_json(GraphSpec)
)
```

- Canonical JSON 固定 key order、enum representation、default materialization 和 number/string encoding。
- 注入的 dispatch/service capability、callable、closure identity、module path、source hash 不进入 identity。
- 相同 GraphSpec 在不同构建顺序、进程和模块路径下得到相同 identity。
- `compiler_schema_version` 是编译语义版本；binding 解释、reducer 语义、predicate/compute 表达式、retry/timeout 默认值、output contract、omitted default materialization 或 Tool operation 编译规则变化时必须递增。

身份拆成两个维度：

```text
definition_id       # 图结构 + compiler_schema_version 所代表的编译语义
execution_revision  # Tool catalog、policy 与执行环境兼容性
```

- 同名 Tool 的实现版本不进入 definition_id；否则结构相同的图会因部署细节漂移。
- continuation 恢复在适用时必须校验 execution_revision，拒绝在不兼容 catalog/policy/environment 上继续旧 checkpoint。
- 首版 continuation 仅进程内，execution_revision 可以只保存在 Registry entry 中，但该概念和校验点不能省略。

### 手写 Workflow

- 调用方必须显式提供 `definition_id` 和 `definition_version`。
- Builder build 检测重复 node/edge，冻结 topology，并可生成内容 digest 用于一致性校验；digest 不替代显式 identity。
- Continuation 只保存 definition identity/ref，不持久化 callable。
- 首版没有 DefinitionResolver，因此 continuation 仅在 Registry 进程生命周期内可用。

## 11. Phase 1 验收

- `resume_tasks/get_node_state` 不读取 Pool TaskMeta 私有字段。
- Pool 不存在 graph_meta、run_state、node_names、state_snapshot 或 Workflow 类型分支。
- Continuation registry 覆盖 consume、重复 resume、retire、session scope、shutdown 和 restart-unavailable。
- Failed/TimedOut/Cancelled 的可恢复矩阵、fatal failure 无 ResumeRef、timeout/cancel 在 snapshot 前 cancel/join siblings 均有测试。
- execute/request_stop completion race 只发布一个终态；pre-start checkpoint stop、grace timeout 强制终止、DISCARD/shutdown 无 ResumeRef 均有测试。
- from/skip/restart 校验覆盖各终态；无安全 checkpoint但可重启时签发 restart-only capability；full restart 创建新 Run；Workflow retry budget 与 Product resume budget 不混用。
- presentation consumption、continuation retention、inspection retention 独立；覆盖 expiry、discard、GC 通知及稳定 ResumeExpired。
- SnapshotValueProjector 覆盖深冻结值、value ref、redacted 和 unavailable；查询不返回 live Run/Definition/Continuation。
- inspection 不可投影字段不阻止暂停；恢复必需字段不可冻结时不签发局部 capability，只允许 restart-only 或无 ResumeRef。
- 前台/后台行为矩阵逐项测试。
- `run_graph` Tool JSON Schema golden test。
- 顶层单 effect-ledger receipt，节点无 result_id/started ledger。
- Approval/AskUser 前台直接交互测试。
- 后台 ModelRoute/HumanQuestion/ApprovalDecision pause、释放 worker、交付、answer、resume 测试。
- 模型不调用工具时保持 PAUSED 且不占执行槽；并发/迟到回答、cancel/retire/shutdown 和 fail-closed timeout 测试。
- pipeline enable、graph nesting、普通 Tool、自定义 deferred Workflow Tool 分类测试。
- rename/prefix/copy/alias/dynamic composition 保持 execution_kind；MCP/default Tool 为 ATOMIC；缺 factory 和 ATOMIC 返回 DeferredOperation 均 fail-fast。
- 分类不再使用 marker、`vars(instance)`、`getattr` 或实例字段扫描，且 execution_kind 不进入模型 wire schema。
- Definition identity 不受 module move、closure identity、injection 和构建顺序影响；compiler semantic version 与 execution_revision 分别验证。
- 拆分后 `orchestration.workflows` 不 import ToolResult、Tool catalog、BaseTool、AgentControl、AskUserQuestion/Approval DTO 或 Product DSL。
- 手写 Workflow 的 `add_decision`/`interaction_policy` 与 Product 三类 decision adapter 均有契约测试。
- crash 位于节点外部副作用完成和顶层 receipt commit 之间时，测试/文档明确允许 at-least-once replay，不声称 exactly-once。
- cancellation 后无 node/operation task 泄漏。

## 12. 拒绝方案

- 继续把 Workflow state 塞进 TaskMeta：维持跨领域数据库。
- ResumeRef 持有 Continuation object 并暴露给 Pool：不透明性失效。
- 用 marker 或 capability instance scan 识别 pipeline：分类不稳定且依赖构造副作用。
- 将 BaseNode docstring/schema introspection 放入通用内核：Product DSL 泄漏。
- 禁止后台人类路由：丢失首版必须能力。
- 让后台 worker 直接无限阻塞等待人类：占用并发槽且 shutdown/恢复所有权不清。
- 将前台/后台路径强行统一成同一等待语义：掩盖 ownership 差异。
- 从 closure/source/module 推导 Definition identity：部署和搬包不稳定。
