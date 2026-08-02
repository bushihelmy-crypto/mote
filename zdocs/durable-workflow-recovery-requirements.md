# Mote Durable Workflow 故障恢复需求

状态：待评审  
性质：独立架构能力需求  
基线日期：2026-07-31  
适用范围：`contracts <- kernel <- runtime <- orchestration <- product`

## 1. 背景

Mote 已有 Workflow graph、BackgroundTaskPool、节点状态和暂停/恢复相关实现，但当前生产工具目录没有暴露 `ResumeTasks`、`GetNodeStates`，Workflow 通知仍提示模型调用 `resume_tasks`、`get_node_state`。现有恢复语义还依赖进程内 BackgroundTaskPool 和运行对象，不能证明 Runtime 崩溃、Session resume 或 Residency eviction 后仍可继续同一逻辑 Workflow。

本需求将故障恢复从历史工具兼容问题提升为独立 durable execution 能力。目标与 LangGraph 的 durable execution 语义对齐，但不复制其内部实现：Mote 仍遵守五层架构、Contracts-owned 边界、单向依赖、唯一 canonical owner 和副作用前置持久化规则。

## 2. 产品决策

1. Workflow 是 Mote 的可恢复执行单元，必须支持进程崩溃、Session resume 和 Residency eviction 后继续执行。
2. BackgroundTask 是 process-local 的临时并发机制：每个逻辑 Agent/Role 独立拥有一个 canonical `BackgroundTaskPool`；它不承担跨进程 durable Workflow 恢复语义。
3. 恢复的是版本化 Workflow state 和 pending frontier，不是 Python coroutine、closure、线程栈或任意进程内对象。
4. 同一个逻辑 Workflow run 在任意时刻最多有一个 fenced execution owner。
5. 已发生或可能发生的外部副作用必须通过 durable intent/receipt 对账，不能因恢复而盲目重放。

## 3. 目标

1. 建立唯一执行链：

   ```text
   Graph declaration
       -> canonical compile
       -> versioned WorkflowDefinition
       -> durable WorkflowRun
       -> checkpointed execution
       -> resume/reconcile
   ```

2. 为 Workflow definition、run、checkpoint、attempt 和副作用建立稳定身份。
3. 每次状态推进以 committed checkpoint 为恢复真相源。
4. 支持显式 interrupt、pause、continue、retry-failed 和受约束的 checkpoint replay。
5. 恢复失败必须可机器判别、fail closed，并保留最后一份有效恢复证据。
6. 模型可见的查询和恢复工具必须从 Product canonical tool catalog 唯一可达。
7. Workflow progress、checkpoint、terminal outcome 和恢复事件可以投影到 Session 与远程协议，但不能由 presentation 状态反推执行真相。

## 4. 非目标

- 不恢复任意 Python 内存对象或协程调用栈。
- 不允许未注册 codec 的节点状态进入 durable checkpoint。
- 不在 Workflow definition 内容变化后按同名定义静默恢复旧 run。
- 不把普通 BackgroundTask 自动升级成 durable Workflow。
- 不保证无法查询结果的外部副作用可以自动重试。
- 不建立第二套 Agent execution engine；Workflow 继续属于 Orchestration bounded context。
- 不以长期兼容 alias 保留 `resume_tasks`、`get_node_state` 作为第二 canonical API。

## 5. 核心身份模型

### 5.1 WorkflowDefinitionIdentity

由 canonical compiler 基于结构化定义计算，至少覆盖：

- definition schema version；
- graph topology；
- node kind、canonical node identity 与稳定 implementation identity；
- node input/output contract identity；
- reducer、routing 与 condition semantics；
- retry、timeout、cancellation 和 interrupt policy；
- effect classification；
- tool/service capability definition 与 permission binding identity；
- definition/checkpoint codec versions。

identity 不得依赖 `inspect.getsource` 是否可用、Python 对象地址、import/输入声明顺序或进程局部注册顺序。durable definition不得直接持有未编码closure/callable；此类实现必须由Product registry按稳定identity解析。unknown implementation identity、unknown version或content digest mismatch必须fail closed。

### 5.2 WorkflowRunIdentity

一个逻辑 run 至少包含：

- `run_id`：全局稳定逻辑身份；
- `definition_id`：绑定完整 WorkflowDefinition identity；
- `session_id`：所属 Session；
- `parent_run_id`：可选 lineage；
- `created_at`；
- `run_revision`。

同一 `run_id` 不得绑定不同 definition 或 Session。

### 5.3 ExecutionIncarnation

每次初始执行或故障恢复产生新的 execution incarnation：

- `execution_id`；
- `run_id`；
- `owner_id`；
- `fencing_token`；
- `started_from_checkpoint_revision`；
- lease expiry/heartbeat 状态。

旧 incarnation 失去 lease 后不得提交 checkpoint、terminal outcome 或副作用 settlement。

### 5.4 NodeAttemptIdentity

节点执行使用稳定 attempt identity：

- `run_id`；
- `node_id`；
- `logical_attempt`；
- `execution_id`；
- `checkpoint_base_revision`。

retry 必须推进 logical attempt；进程重连不得通过生成无关联的新 id 掩盖旧 attempt 的未知状态。

## 6. Durable checkpoint contract

### 6.1 Checkpoint envelope

每个 checkpoint 使用 Contracts-owned、版本化、严格解码的 envelope，至少包含：

- schema/codec version；
- `run_id`、`definition_id`；
- monotonic `checkpoint_revision`；
- parent checkpoint revision/digest；
- execution fencing token；
- canonical state payload；
- completed node outputs；
- pending/runnable frontier；
- running/in-doubt attempts；
- paused/waiting/failed node states；
- reducer state；
- external effect intents/receipts；
- content digest；
- committed timestamp。

未知版本、额外/缺失字段、错误 primitive、identity mismatch、revision rollback、同 revision 不同 digest 必须 fail closed。

### 6.2 Commit protocol

checkpoint commit 必须满足：

1. 验证当前 execution lease/fencing；
2. 验证 expected parent revision；
3. 编码并验证全部 durable payload；
4. durable write + fsync；
5. 原子推进 canonical checkpoint head；
6. 返回包含 revision、digest 和 fencing token 的 commit receipt；
7. commit 成功后才对外发布已推进事件。

并发 writer 使用 revision CAS；旧 owner、错误 parent 或重复 revision 不得覆盖新 checkpoint。语义相同的重复提交可以幂等返回原 receipt，同 revision 不同内容必须拒绝。

### 6.3 Checkpoint timing

至少在以下边界提交 checkpoint：

- run 创建并绑定 definition 后；
- node attempt 开始前的 durable intent；
- node terminal state 与输出提交后；
- interrupt/pause 生效后；
- external effect receipt 更新后；
- frontier/reducer 状态改变后；
- run terminal settlement 前后。

实现可以安全合并纯内存计算步骤，但不能跨越外部副作用或模型可见 interrupt 边界。

## 7. 节点 durable payload

### 7.1 允许的状态

节点 input、output 和 reducer state 必须使用：

- `JsonValue`；或
- 注册的版本化 codec/tagged union；或
- durable `ArtifactRef`。

未知 Python 对象不得使用 `repr`、pickle 或隐式 `str()` 进入 checkpoint。

### 7.2 Codec registry

Workflow checkpoint codec registry 必须显式绑定：

- codec id；
- schema version；
- owning domain；
- strict decoder；
- canonical encoder；
- content digest algorithm；
- 支持窗口内的有限 upcaster。

upcaster 只负责 durable data migration，不成为第二运行时 API。

## 8. 外部副作用恢复

### 8.1 Effect classification

节点或节点内调用必须声明：

- `PURE`：可安全重算；
- `IDEMPOTENT`：需稳定 idempotency key；
- `EXTERNAL`：必须在调用前记录 durable intent；
- `IN_DOUBT`：结果未知，只能 reconcile 或人工决策。

### 8.2 Intent/receipt

外部副作用前必须提交：

- stable operation id；
- node attempt identity；
- canonical request digest；
- target identity；
- idempotency key；
- execution fencing token。

接受或完成后提交 provider receipt/terminal settlement。恢复发现 started intent 无 terminal 时：

1. 优先按 receipt/provider query reconcile；
2. 只有 contract 明确幂等且 identity 一致时才允许重试；
3. 其余进入 `IN_DOUBT`，不得自动重复副作用。

## 9. 恢复状态机

### 9.1 Run states

```text
CREATED
RUNNING
PAUSED
WAITING
RECOVERING
SUCCEEDED
FAILED
CANCELLED
IN_DOUBT
CORRUPT
MIGRATION_REQUIRED
```

terminal state 只能由拥有有效 fencing token 的 execution 提交。`CORRUPT` 与 `MIGRATION_REQUIRED` 不得自动降级成 fresh run。

所有mutation而非仅checkpoint/terminal都必须校验`run_id + generation + fencing_token + expected revision`，包括frontier CAS、effect intent/receipt、delivery ack、cancel settlement、lease refresh/release和GC/delete。每条durable record保存相应generation/fence/revision；旧owner的coroutine即使仍运行也无提交权。

### 9.2 Recovery algorithm

```text
load durable run
    -> validate run/definition/session identity
    -> validate checkpoint chain and head digest
    -> resolve supported codec/upcaster set
    -> acquire new execution lease and fencing token
    -> reconcile open node/effect attempts
    -> reconstruct state + pending frontier
    -> commit RECOVERING checkpoint
    -> continue, pause, or return IN_DOUBT
```

全部校验完成前不得注册 live run、删除旧证据或写入替代空状态。

### 9.3 Resume strategies

- `continue`：从 canonical pending frontier 继续；
- `retry_failed`：只为允许重试的 failed node 创建新 logical attempt；
- `replay_from_checkpoint`：从指定 checkpoint 创建显式分支或新 revision 链；
- `reconcile`：只对账 open/in-doubt attempts，不启动新的业务执行。

`replay_from_checkpoint` 不得改写原 checkpoint 历史；若会重复外部副作用，必须拒绝或要求明确人工授权。

### 9.4 Terminal outcome 与 durable delivery

- terminal outcome由有效owner以fence/CAS提交到唯一durable run state；
- delivery intent/ack是独立durable事实，可复用canonical mailbox/event delivery机制，按run、outcome revision和recipient identity幂等；
- progress与telemetry仅是best-effort observation，不得推进run或充当terminal truth；
- Product按RunId查询immutable terminal projection/result reference，不返回mutable run/checkpoint对象；
- terminal commit后、delivery通知前崩溃时，周期scan/reconcile必须重新发现未ack intent；通知后ack前崩溃允许幂等重复投递，但不得重复effect或改变outcome。

### 9.5 Cancel、deadline、pause 与 resume

- cancel是绑定command identity、run identity和expected revision的durable命令；重复命令幂等，并明确其与terminal commit的CAS竞态；
- deadline使用持久化绝对时间和注入clock contract，重启后继续生效，不能只依赖进程内timeout；
-进入cancelling后不得派发新节点，并按effect classification等待、reconcile或进入IN_DOUBT后再terminal settle；
- pause保存versioned reason、pending frontier和所需external input schema；
- resume token绑定definition identity、run revision和pause fact；重复resume幂等或返回typed stale/already-resumed disposition；
- cancel/timeout后迟到的node output或effect receipt只能在有效settlement规则下记录，不得复活run或覆盖terminal outcome。

## 10. Interrupt 与 human-in-the-loop

interrupt 是 durable Workflow 状态，不是进程内 Event：

- interrupt request 必须绑定 run、expected checkpoint revision 和 requester；
-安全点提交 `PAUSED` checkpoint 后才返回已暂停；
-等待人工输入时保存 versioned prompt identity 和 expected response contract；
-回复必须绑定 principal、session、run、checkpoint revision 和 prompt id；
-重复回复幂等，不匹配或过期回复拒绝；
-进程崩溃后等待状态仍可恢复。

## 11. Canonical Product tools

### 11.1 `GetWorkflowRun`

查询 durable run，不改变执行状态。输入至少包括：

- `run_id`；
-可选 detail level。

输出至少包括：

- run/definition identity；
- current state；
- checkpoint revision/digest；
- node states；
- pending frontier；
- failed/in-doubt attempts；
- supported resume strategies；
- machine-readable unavailable/corrupt/migration-required error。

### 11.2 `ResumeWorkflowRun`

输入至少包括：

- `run_id`；
- `expected_checkpoint_revision`；
- strategy；
- strategy-specific node/checkpoint selection；
-必要时的 approval reference。

调用必须经过权限、run ownership、definition identity、checkpoint CAS 和 execution lease 校验。

### 11.3 历史工具迁移

`ResumeTasks`、`GetNodeStates`、`resume_tasks`、`get_node_state` 不作为长期 alias 保留。迁移切片必须原子完成：

-新 durable tools 注册并从 Application catalog 可达；
-所有生产通知改用新名称和 typed 参数；
-旧实现、alias、提示和消费者删除；
-静态 gate 验证通知引用的工具名可由同一 catalog 解析。

## 12. 分层与 owner

### Contracts

定义 Workflow identity、run/checkpoint DTO、状态、错误、codec Port、checkpoint store Port、lease/fencing Port 和 effect settlement contract，不依赖具体 Runtime/Orchestration。

### Kernel

不拥有 Workflow durable 状态机。Kernel 仅通过已有执行/工具 Port 参与节点内部 Agent/LLM 执行，不反向依赖 Orchestration。

### Runtime

提供 durable storage、strict codec、lease/fencing、artifact、journal、permission 和副作用执行 seam，不理解 Workflow graph topology。

### Orchestration

拥有 Workflow compiler、run state machine、frontier、checkpoint timing、恢复与 reconciliation。

### Product

拥有 Workflow tools、通知、CLI/API surface、composition declaration 和用户可见错误投影。

## 13. 并发与多进程

-同一 run 的 resume 使用跨进程 lease；
-每次 acquisition 产生不可复用 fencing token；
-heartbeat/release/checkpoint/frontier/effect intent/effect receipt/terminal/delivery ack/cancel settlement/GC-delete均校验owner、fencing token与expected revision；
-lease 丢失后立即停止 node dispatch 和 checkpoint commit；
-两个并发 resume 最多一个成功获得执行权；
-stale owner 不得删除、覆盖或终结新 owner 的 run；
-read-only query 不要求 execution ownership，但必须通过 run authorization。

### 13.1 Durable scan、claim、公平与背压

- Orchestration唯一拥有reconciliation policy与run admission；Runtime只实现store、lease、cursor和clock机制；
- scan结果只是候选，不授予执行权；执行前必须取得fenced claim，重复scanner不能直接推进run；
- scan contract定义稳定分页cursor、排序键与公平策略，防止大run或持续retry饿死其他run；
- retry使用durable next-attempt schedule；超过政策或持续失败进入typed poison/dead-letter disposition，不进行无界热循环；
- admission定义全局/tenant/root并发上限和backpressure disposition；
- best-effort wake丢失后，周期durable scan仍能发现所有可推进、待对账和待delivery的事实；
- Product tool、Session resume和presentation adapter不得各自扫描并启动run，只能提交typed command或唤醒canonical reconciler。

### 13.2 Durable backend guarantee profile

所有可承载WorkflowRun的backend共同满足：single fenced owner、revision CAS、strict codec、crash recovery、effect reconciliation和durable scan。typed activation result至少声明：

- backend identity与process/host scope；
- durability与fsync/transaction commit保证；
- fencing/CAS保证；
- crash recovery、effect reconciliation与scan保证。

Product只有在请求的最低保证全部满足时才能激活。Temporal与JSONL可以在部署拓扑、吞吐和运维能力上不同，但JSONL若缺少跨进程ownership、明确commit或recovery语义，就不能标记为同等级durable Workflow backend。选择Temporal失败时不得回退JSONL；显式选择JSONL也不等于自动接受较弱保证。

## 14. Session、Residency 与 BackgroundTask 边界

1. Session rollout 可以记录 Workflow run reference 和 presentation facts，但 Workflow checkpoint store 是执行恢复真相源。
2. Session resume 必须重新发现未终结 durable runs，不得把它们伪装成普通 BackgroundTask。
3. Residency eviction 不得丢失 Workflow run；live runner 可停止，run 通过 checkpoint 恢复。
4. 每个逻辑 Agent/Role 独立构造并拥有一个 canonical BackgroundTaskPool；task registry、counter、operation、meta、output、progress、notification、wake callback、cancel 和 cleanup 均不得跨 Agent 共享。
5. supervisor 只可通过独立、窄且类型化的 admission/permit Port 治理进程级或树级并发、资源、预算、公平性、背压和级联取消，不得拥有 task/result/notification 或 mutable pool state，也不得建立第二套 task registry。
6. 存在未结算 BackgroundTask 的 Agent 必须 pin residency，不得 eviction；任务全部结算后才允许卸载。不得为 eviction 建立 process singleton、scope registry 或 rebind registry。
   submit/work-pin acquire 与 ACTIVE→DRAINING 必须在同一 incarnation/generation gate原子互斥；单次 pending snapshot 不构成 eviction 安全保证。
7. 同一进程内的 Session detach/reconnect 不等于任务恢复；Agent 仍驻留时可继续观察自己的 pool。新的进程或不同 Agent 不得仅凭可控 session/task id 接管旧任务。
8. 普通 BackgroundTask 不跨进程恢复。进程终止后，旧 process instance 下的任务失效；需要跨进程恢复的工作必须进入 durable WorkflowRun。若磁盘保留输出，只能作为带 owner identity 的历史 artifact，不能被新进程解释为仍在运行的任务。
9. Agent release 必须显式取消或结算自己的 pool，等待 operation/output/notification cleanup 完成后再回收，且不得影响其他 Agent。
10. BackgroundTask 丢失、取消或 terminal delivery 不能推进、覆盖或伪造 Workflow checkpoint。

### 14.1 BackgroundTask isolation invariants

- `TaskId` 只在所属 Agent pool 内唯一；跨 Agent 操作的 canonical reference 至少包含稳定 `agent_id + task_id`；
- task sequence 在同一 Agent pool 内单调，不能覆盖该 pool 的既有输出；
- 同一 TaskId 的 process-local retry 使用 Pool 内单调 AttemptId；旧 attempt 失去状态、输出、progress 和 notification 提交权，query/cancel 默认作用于 active/latest attempt，历史只保留 typed immutable settlement；
- output store 使用 owner-scoped path 或等价不可碰撞 identity，打开既有输出不得默认 truncate；
-所有 lookup/cancel/consume API 同时验证 agent owner，不接受裸 task id 作为授权凭证；
-跨 Pool reference 还必须验证 process instance 与 incarnation/generation；attempt-scoped mutation验证 AttemptId，owner mismatch fail closed；
-跨 Agent progress、terminal notification 和 ResourceRegistry reference 不得串线；
-Agent scope teardown 必须等待或取消其任务并产生明确 settlement，不能影响其他 scope；
-process shutdown 统一停止 service 下的全部 scope，但逐 Agent 结算和审计；
-测试 fake 可以使用 in-memory service，但必须保持与生产相同的 scope/ownership 语义。

## 15. 错误语义

至少定义以下机器可判别错误：

- run not found；
- definition unavailable/mismatch；
- checkpoint corrupt；
- checkpoint version unsupported；
- migration required；
- stale checkpoint revision；
- execution lease unavailable；
- execution fenced；
- effect in doubt；
- resume strategy unsupported；
- run already terminal；
- unauthorized run access。

load/resume 失败不得创建同 id 的 fresh run，不得删除或覆盖原 durable record。

## 16. 可观测性

通过 typed events 观察：

- run created/started/recovering/paused/terminal；
- checkpoint commit started/committed/rejected；
- node attempt started/settled/in-doubt；
- lease acquired/lost/fenced；
- effect reconciliation started/settled；
- migration required/corrupt evidence quarantined。

日志和 telemetry 不得包含 secret、完整敏感节点 payload 或 provider credential。观察事件不作为恢复真相源。

## 17. 治理门禁

1. 生产通知引用的 Workflow tool 必须可由 canonical Application catalog 解析。
2. WorkflowDefinition 只能由 canonical compiler 生成。
3. durable checkpoint payload 不接受无界 `Any`、pickle、`repr` fallback 或未注册 codec。
4. checkpoint decoder 必须 exact-shape、strict-primitive、unknown-version fail closed。
5. run、definition、checkpoint、execution、node attempt 和 effect identity 必须全链一致。
6. checkpoint revision/digest/fencing 必须单调且 CAS 提交。
7. EXTERNAL intent 未 durable commit 时不得执行副作用。
8. open effect 未 reconcile 时不得自动重放。
9. BackgroundTask 不得被当作 durable Workflow 恢复来源；Agent-owned pool、独立 supervisor permit 和禁止集中 task state 必须由 architecture/composition gate 验证。
10. load/resume 失败不得调用 create/upsert 或注册替代空 run。
11. 生产run mutation只能经过RunId command；query只返回immutable projection，禁止生产`WorkflowRun`内存owner和Product private-state inspection。
12. fencing gate覆盖effect、delivery、cancel、lease和GC在内的所有mutation。
13. 只有Orchestration reconciler可scan/claim推进run；Product/Session不得建立第二scanner。
14. backend activation必须校验共同最低guarantee profile。

## 18. 验收场景

至少覆盖：

1. 纯节点完成后崩溃，恢复不重复已完成节点；
2. node started checkpoint 前崩溃，副作用调用次数为零；
3. external intent commit 后、请求前崩溃；
4. provider accepted 后、receipt commit 前崩溃；
5. receipt commit 后、node terminal checkpoint 前崩溃；
6. checkpoint fsync/replace/CAS 失败；
7.两个进程同时 resume；
8. lease 过期后旧 owner 尝试提交；
9. Session resume 后继续 run；
10. Residency eviction 后继续 run；
11. interrupt 等待期间进程重启；
12. definition digest mismatch；
13. checkpoint 中间损坏、尾部 torn write、未知版本；
14.同 revision 不同 digest；
15. retry_failed 只重试允许的节点；
16. replay_from_checkpoint 不覆盖原历史；
17. in-doubt effect 阻止自动重放；
18.通知工具名与 catalog 一致；
19.未知 run 不创建 fresh run；
20.恢复失败保留原始证据。
21.两个 Agent 使用相同局部 task sequence 时，输出、进度、查询和取消保持隔离；
22. Agent 存在未结算 BackgroundTask 时 residency eviction 被拒绝，全部结算后才可卸载；
23. Agent release 只结算自己的 pool，不影响其他 Agent；
24.新进程不能以旧 task id 接管或截断旧进程的 BackgroundTask 输出。
25.同一 TaskId 的 process-local retry 生成单调 AttemptId；旧 attempt 的迟到状态、输出、progress 和 notification 不能覆盖 active/latest attempt。
26.query/cancel 默认作用于 active/latest attempt；历史 attempt settlement 不可变，terminal notification 以 AgentId + TaskId + AttemptId 幂等。
27.submit 与 begin_eviction/begin_release 并发时原子互斥；DRAINING/CLOSED 拒绝 submit，pin 在全部 operation/output/notification/resource settlement 后释放。
28.worker crash 后旧 TaskRef 返回 owner-gone/incarnation-lost，不自动重放且不命中新 incarnation的同名 local TaskId。
29.release 超时、non-cooperative operation、output flush 或 notification failure 返回 typed disposition并保持 DRAINING/pin；不伪装 settled。
30.subtree cancellation 使用 fenced snapshot/epoch并与 spawn admission 协调；重复命令幂等，逐 Agent返回 settled/already-terminal/owner-lost/timeout。
31. lease分别在node执行中、intent后、effect后、receipt前和terminal commit前丢失时，旧owner的后续mutation全部被拒绝，且不能refresh/release新owner lease。
32. terminal commit后通知前崩溃可由scan恢复delivery；通知后ack前崩溃只产生幂等重复delivery，不重复effect。
33. cancel与terminal并发、重启后的absolute deadline、重复/过期resume token及cancel后的迟到receipt均按durable revision/fence确定性settle。
34. 多scanner分页时先claim后执行，满足公平与并发上限；wake丢失由周期scan补偿，poison run不会形成热循环。
35. Temporal/JSONL activation result逐项证明请求的durability/fencing/commit/recovery guarantee；不足时fail closed。

## 19. 实施切片

```text
W0  产品语义、ADR、identity/state/error Contracts
W1  canonical Workflow compiler、完整Definition identity；迁移definition consumer并删除第二identity/生产随机RunId owner
W2  RunId command/immutable query、versioned checkpoint codec/store、revision CAS；删除`_executing/_state/task`与Product private inspection
W3  execution lease、all-mutation fencing、durable cancel/deadline/pause/resume、唯一scan/claim/reconcile；删除continuation registry与各入口scanner
W4  node/effect intent/receipt、terminal outcome、delivery intent/ack/reconciliation；删除BackgroundTask/progress terminal truth
W5  Product tools、Session/Residency discovery与remote/CLI immutable projection
W6  backend guarantee activation、crash/concurrency matrix与architecture gates
```

每个切片必须独立可合入，并在该片列出和删除被替代的旧state field、registry、production entry及Product consumer，不允许旧工具和新工具长期形成两条canonical恢复路径。Product tool上线的同一迁移窗口必须完成catalog、通知、旧实现与alias清理。纯本地definition测试executor可以保留，但不能拥有生产RunId、进入composition/catalog或作为第二生产入口。

## 20. 完成定义

本需求只有在以下条件全部满足时才可关闭：

1. Workflow definition、run、checkpoint、execution 和 node attempt 均有唯一 canonical identity；
2. Runtime 崩溃、Session resume 和 Residency eviction 后可以从 committed checkpoint 恢复；
3. 同一 run 只有一个 fenced execution owner；
4. durable payload 严格可逆，未知类型/version fail closed；
5.已完成纯节点不重复执行，外部副作用不盲目重放；
6. interrupt/human reply 在重启后保持真实状态和 ownership；
7. load/resume 失败不创建替代 run、不删除恢复证据；
8. canonical Product tools 和全部生产通知一致；
9.旧恢复工具、alias、提示和 BackgroundTask 恢复旁路已删除；
10. crash、并发、损坏、identity mismatch 和 in-doubt negative fixtures 全部通过；
11.治理 gate 在最小依赖环境运行，不加载 optional Product backend；
12.实现继续遵守 `contracts <- kernel <- runtime <- orchestration <- product` 单向分层。
