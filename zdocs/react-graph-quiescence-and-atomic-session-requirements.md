# ReactGraph 静默终止、原子 Session 事实与输出发布统一需求

## 1. 文档性质

本文记录已经确认的 ReactGraph 产品语义与实施要求。实现必须以当前源码、`AGENTS.md` 和架构门禁为准；本文不授权兼容层、双读、双写或第二套执行链。

目标是让 ReAct 的节点职责、后台任务等待、消息观察、输出验证、Session 持久化、崩溃恢复和最终发布形成一条唯一状态真相链。

## 2. 已确认的核心决定

1. ReactGraph 继续使用显式节点和显式边，不退化为巨型 `while True`。
2. Role 私有 `message_buffer` 是已经完成路由准入的 inbox；Kernel `OBSERVE` 不再重复判断 `watch`、`send_to` 或广播目标。
3. `send_to` 为空的消息不投递；包含当前 Role 和其他目标时，本地与外部目标分别投递且各一次。
4. 用户消息和后台任务通知统一进入所属 Agent 的 `message_buffer`，它是 ReAct 唤醒的唯一消息真相。
5. 后台任务完成必须先可靠投递通知到所属 Agent 的 buffer；任务完成但通知未投递不能伪装成功。
6. `INTERPRET` 识别最终候选后先进入 `AWAIT_QUIESCENCE`，此时候选只存在于当前执行状态中，不验证、不写 Session。
7. 后台通知和用户消息都能从 `AWAIT_QUIESCENCE` 唤醒图并转回 `OBSERVE`；本轮暂存候选随即失效。
8. buffer 为空且后台任务全部结算后，才进入 `VALIDATE_OUTPUT`。
9. `VALIDATE_OUTPUT` 校验成功后，原子写入 `FinalOutputCommittedEvent + AIMessage`，然后真正 `End`。
10. 原有所有准备终止的路径都必须先收敛到 `AWAIT_QUIESCENCE`。
11. `THINK` 不再拥有独立的 `WAIT_BACKGROUND` 路由；旧 `WAIT_BACKGROUND` 节点删除。
12. 最终对外发布不属于 Kernel Graph。Graph 返回后，由 Product/Role 的发布能力处理。
13. 发布队列接管成功后，Graph 不再背负交付状态；发布系统自己拥有 pending、retry、ack 和 dead-letter。
14. 旧输出链 `accepted -> commit_started -> committed` 不再作为生产主路径保留。

## 3. 术语

### 3.1 `PendingCandidate`

`INTERPRET` 选中的 `FinalCandidateAction`。它只属于当前 `ExecutionState`，尚未通过 decoder 和 validators，也没有写入 Session。等待期间出现任何新消息都会使它失效。

### 3.2 `FinalOutputCommittedEvent`

最终候选在系统静默后通过 decoder 和 validators，并与最终 AIMessage 一起原子写入 Session 的 durable 终态事实。它至少包含：

- run identity；
- candidate identity；
- contract identity 和 schema fingerprint；
- 编码后的类型化输出值；
- validator provenance；
- 对应最终 AIMessage；
- correction attempt。

该事件一旦成功写入，本次 run 不得再回到 `OBSERVE/THINK`。它必须使用严格、版本化 schema；未知版本、缺字段、额外关键字段或错误 primitive 类型必须 fail closed。

### 3.3 `AWAIT_QUIESCENCE`

唯一终止协调节点。它不执行模型、不验证输出、不发布消息，只判断当前 Agent 是否已经静默，并在必要时等待 inbox 活动。

### 3.4 静默

同时满足：

- 当前 Role 的 `message_buffer` 没有待观察消息；
- 当前 Agent 的 `BackgroundTaskPool` 没有 pending task；
- 没有正在与终止判定竞争的 inbox admission 或 task completion delivery。

## 4. 目标图拓扑

```text
RESTORE
   |
   v
OBSERVE -> BUDGET -> THINK -> INTERPRET
   ^                         /       \
   |                        /         \
   |                      ACT      pending candidate
   |                       |             |
   +-----------------------+             v
                                  AWAIT_QUIESCENCE
                                    /          \
                          inbox wake            quiescent
                               |                   |
                               +----> OBSERVE      v
                                          VALIDATE_OUTPUT
                                            /          \
                                        rejected     committed
                                           |             |
                                           v             v
                                        OBSERVE          End
```

所有其他准备终止的路径也必须先进入 `AWAIT_QUIESCENCE`：

```text
BUDGET stop ----------------------+
THINK stopped/no inference -------+--> AWAIT_QUIESCENCE
INTERPRET final candidate ---------+
```

## 5. 节点需求

### 5.1 `RESTORE`

- 不扫描任意 Python 对象，不从历史尾部类型猜测是否续跑。
- 从严格解码的 Session projection 恢复已原子提交的 `FinalOutputCommittedEvent`。
- 已有 committed 终态时重建 `ExecutionResult` 并结束，不补做旧 `commit_started`，也不重新验证。
- 没有 committed 终态时进入 `OBSERVE`；不得从未持久化的进程内 candidate 猜测恢复点。

### 5.2 `OBSERVE`

- 一次性消费当前 Role 私有 buffer 中满足优先级的消息。
- 不再执行 `watch/send_to/<all>` 路由过滤。
- 保留 stable message identity 去重，避免同一 durable message 重复进入历史。
- 同一次 drain 得到的消息批次必须原子追加到 Session。
- 区分用户输入与后台任务通知，并产出类型化 observation result。
- 新用户消息可以激活推理；后台通知不得把用户/工具明确停止的 Role 无条件重新激活。
- ReAct 中途用户消息继续使用 interjection framing；初始输入不包装。
- 任何新观察都会使 `pending_candidate` 失效。

### 5.3 `BUDGET`

- 每次模型推理前执行预算门禁。
- 允许时推进 turn identity 并进入 `THINK`。
- 停止时生成 typed stop result，进入 `AWAIT_QUIESCENCE`，不得直接 `End`。

### 5.4 `THINK`

- 只负责一次推理或恢复一个合法 inference checkpoint。
- 将 provider 结果通过当前 `CommandChannel` 转换为 `ModelTurn`。
- 推理成功后进入 `INTERPRET`。
- `active=False` 时不得调用模型，必须请求终止并进入 `AWAIT_QUIESCENCE`。
- 不检查后台任务，不路由 `WAIT_BACKGROUND`。
- `InferenceService.infer()` 后续应从含混布尔值收窄为类型化 disposition，明确 completed、stopped 和 failed。

### 5.5 `INTERPRET`

- 使用 completion policy 判定 `ModelTurn`。
- 无 final candidate 时进入 `ACT`。
- 恰好一个 final candidate 且没有普通 ToolCall 时，将它保存为 `pending_candidate` 并进入 `AWAIT_QUIESCENCE`。
- 多个 final candidate，或 final candidate 与普通 ToolCall 同回合出现，fail closed。
- 不检查后台任务，不发布，不持久化最终输出。

### 5.6 `ACT`

- 执行 `ModelTurn` 中的普通文本动作与 ToolCall。
- ToolCall、ToolResult、effect receipt 和 inference checkpoint settlement 必须按不可分割语义单元原子提交。
- 完成后进入 `OBSERVE`。
- 工具可以请求停止推理，但仍不能绕过 `AWAIT_QUIESCENCE` 直接终止。

### 5.7 `VALIDATE_OUTPUT`

- 只接受 `CandidateSelection`。
- 只有 `AWAIT_QUIESCENCE` 已证明静默后才能进入。
- 从 `FinalCandidateAction.raw` 解码为 `OutputT`。
- 依序执行 structural、semantic、policy validators。
- 校验失败时原子记录 rejection 与 correction feedback，然后进入 `OBSERVE`；耗尽修正次数时抛 typed error。
- 校验成功时生成 immutable typed final output。
- `FinalOutputCommittedEvent` 与对应 AIMessage 必须在同一次 Session 原子事务中提交。
- durable append 成功前不得修改权威内存 projection，不得返回成功。
- 原子提交成功后直接返回 `End`；之后不得重新进入 ReAct。
- 不再产生 `OutputAcceptedEvent`、`OutputCommitStartedEvent` 或 `OutputCommittedEvent`。

### 5.8 `AWAIT_QUIESCENCE`

- 是所有未提交终态请求的统一静默门。
- 只通过窄的 inbox activity Port 和 BackgroundTask query Port 工作，不持有完整 Role 或具体 pool。
- 若 buffer 已有消息，立即进入 `OBSERVE`。
- 若有 pending background task，等待 `message_buffer` 活动；后台完成通知和用户输入使用同一个唤醒面。
- 被唤醒后进入 `OBSERVE`，不得自行消费消息。
- 无 pending task 时必须再次检查 inbox revision，避免 completion delivery 与终止判定间的丢失唤醒。
- 静默成立后：
  - 有 `pending_candidate`：进入 `VALIDATE_OUTPUT`；
  - 无 candidate：以对应 stop/empty result `End`。
- 终止判定必须与 BackgroundTask submit admission、terminal delivery 和 inbox activity generation 正确同步；单次 `has_pending()` snapshot 不能独立证明静默。

## 6. `active` 语义

`active` 只表示是否允许继续模型推理，不表示是否还有后台任务，也不表示 Graph 是否已经结束。

- 初始为 `False`。
- 首次合法用户输入使其变为 `True`。
- 用户或工具停止使其变为 `False`。
- `active=False` 进入 `THINK` 时不调用模型，而是转入终止协调。
- 后台任务通知只能唤醒 `OBSERVE`，不能无条件把它改回 `True`。
- 新用户消息是否重新激活必须由 typed observation policy 明确决定。

不得再用一个布尔返回值同时表达“未推理”“用户停止”“等待后台任务”和“模型失败”。

## 7. MessageBuffer 与路由边界

### 7.1 投递规则

- `send_to` 为空：直接返回，不投递。
- 只包含当前 Role：只进入本地私有 buffer。
- 只包含其他目标：只进入 routing port。
- 同时包含当前 Role 和其他目标：拆分目标，各投递一次。
- routing port/控制面负责目标解析、广播展开和 admission；Kernel 不重复路由。

### 7.2 唤醒保证

- 用户消息和后台通知都通过 buffer 的 activity generation 唤醒 waiter。
- `wait_for_message()` 在已有消息时必须立即返回。
- push 与 wake 必须属于同一 owner 的确定性操作。
- 后台 task terminal settlement 只有在 notification、result pointer 和 resource retirement 都结算后才完成。

## 8. 原子 Session 写入

原子单位不是整个 ReAct，而是一个不可分割的语义步骤。

| 步骤 | 必须原子提交的事实 |
|---|---|
| OBSERVE | 同一次 buffer drain 的消息批次 |
| THINK | AI/model turn projection、模型调用结果、checkpoint consumption fact |
| ACT | ToolCall、ToolResult、effect receipt、checkpoint settlement |
| VALIDATE reject | rejection、issues、correction feedback、checkpoint settlement |
| VALIDATE accept | `FinalOutputCommittedEvent` 与对应 AIMessage |

当前 `ContextManager.add_batch()` 逐条调用 `add()`，不满足批次原子性，必须替换为真正的 EventFabric batch append。内存 projection 只能在 durable append 成功后一次性推进。

不得用进程内 `_operations` map、rollback closure 或多个顺序 `commit_fact()` 冒充崩溃原子性。

## 9. 输出状态与恢复

目标生产状态只保留：

```text
committed（静默后验证并原子提交）
published（发布系统确认对外交付）
```

`committed` 是 Session 内不可替换的终态事实；`published` 是发布系统的确认事实。发布系统内部可以拥有 pending/retry/ack/dead-letter，但不得复制到 Kernel Graph 状态机。

恢复时：

1. 严格重放 Session；
2. 查找当前 run 的 `FinalOutputCommittedEvent`；
3. 存在时重建最终结果，不重新验证或调用模型；
4. 不存在时没有 durable pending candidate，从正常 `OBSERVE` 入口开始；
5. 已有 published receipt 时不得重复发布；
6. 所有重复操作使用稳定 run/candidate/publication identity 幂等。

不得根据“历史最后一条是 ToolResult”自动启动 THINK；主动停止和崩溃待续不能靠消息类型猜测。

## 10. 发布边界

- Kernel Graph 不调用 `Role.publish_message()`。
- Graph `End` 返回 typed `ExecutionResult`。
- Product/Role 使用稳定 publication id 把结果交给可靠发布能力。
- 发布能力接受后，Graph 生命周期结束。
- 发布成功记录 typed published receipt；失败进入发布系统 retry/dead-letter，不重新运行 ReAct、不重新生成输出。
- `publish_message()` 的本地 buffer 路由不能导致最终响应被当前已结束的 Graph 自消费。

## 11. 删除与替换清单

同一迁移切片内删除生产消费者和旧入口：

- `NodeId.WAIT_BACKGROUND`；
- `WaitBackgroundNode`；
- `THINK -> WAIT_BACKGROUND`；
- `OutputAcceptedEvent`；
- `OutputCommitStartedEvent`；
- `OutputCommittedEvent`；
- `stage_accepted_output()`；
- `commit_terminal_output()`；
- staged/accepted/committed 三套可变输出状态；
- Restore 中的补提交逻辑；
- Session projection 中旧输出状态链；
- `publication_queued` 作为 Graph/输出生命周期状态。

替换为：

- `NodeId.AWAIT_QUIESCENCE`；
- `AwaitQuiescenceNode`；
- `PendingCandidate` transient state；
- `FinalOutputCommittedEvent`；
- 原子 `commit_final_output(...)` 事务；
- typed publication receipt。

## 12. Durable migration

旧 Session 格式不得长期双读。实施选择一次性、版本化、幂等 migration：

- 将可证明完整的旧 `accepted/commit_started/committed` 链投影为一个 `FinalOutputCommittedEvent`；
- 保留 candidate、contract、schema、value、validator provenance、run 和 fence 证据；
- 只有能够唯一关联最终 AIMessage 时才迁移；歧义或损坏 fail closed；
- migration 写审计 receipt，支持 partial failure 后重入；
- migration 完成并通过门禁后删除旧 decoder 和旧事件生产代码；
- 不在正常 resume 路径保留 fallback。

## 13. 验收场景

至少覆盖：

1. 空 buffer、无后台任务、无候选：静默结束且不调用模型。
2. 用户输入启动 ReAct。
3. ACT 期间后台任务完成：通知入 buffer，ACT 完成后由 OBSERVE 消费。
4. 最终候选出现但后台任务 pending：不校验、不写输出，等待通知后回 OBSERVE。
5. 等待期间用户输入：立即唤醒并使 pending candidate 失效。
6. 全静默后才执行 decoder/validators；成功时原子提交并结束。
7. 用户/工具停止且后台任务 pending：不再调用模型，但继续等待和结算通知。
8. 后台通知不会把 stopped Role 自动重新激活。
9. 多个后台任务逐个完成：无忙轮询、无死循环、无丢通知。
10. `FinalOutputCommittedEvent + AIMessage` 要么都存在，要么都不存在。
11. OBSERVE 批次中途写失败：内存和 durable projection 都不前进。
12. ACT 原子事务失败：不能出现 ToolCall 有记录但 ToolResult/effect settlement 丢失的伪完成。
13. 崩溃后恢复 committed final output，不重新调用模型。
14. published receipt 已存在时不重复交付。
15. 混合本地/外部 `send_to` 各投递一次；空目标不投递。
16. 所有未提交终态的图终止路径经过 `AWAIT_QUIESCENCE`。

## 14. 架构门禁

- `contracts <- kernel <- runtime <- orchestration <- product` 依赖方向保持不变。
- Kernel 只依赖 Contracts-owned inbox activity、background query、transaction 和 publication-free DTO。
- BackgroundTaskPool 仍由每个 Agent 独立拥有；静默门不得读取 pool 内部 task map。
- 不新增 `Any`、裸 dict boundary、字符串状态 discriminator、局部 import 或动态 import。
- `OutputT` 的泛型关系必须贯穿 candidate、validation、transaction、result 和 publication。
- Graph 结构测试必须证明唯一 End owner、合法边和无 `WAIT_BACKGROUND` 残留。
- 架构搜索门禁必须证明旧事件、旧接口和旧状态名称退出生产源码。

## 15. 非目标

- 不把 BackgroundTask 变成 durable Workflow。
- 不允许恢复进程接管旧 process-local TaskId。
- 不在 Kernel 内实现 Agent 控制平面或消息目标路由。
- 不为未知未来需求增加 feature flag、兼容 alias 或第二套 Graph runner。
- 不把最终发布失败解释为需要重新 THINK。
