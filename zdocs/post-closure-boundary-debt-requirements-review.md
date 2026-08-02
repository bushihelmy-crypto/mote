# 《Mote 全仓剩余类型与持久化边界清债需求》架构审核

## 1. 审核结论

审核结论：**当前版本已不再授权整体实施，但仍需定向修订后才能作为独立需求创建的完整审计索引签收。**

原需求识别的方向与 `AGENTS.md` 基本一致，抽查的多项源码反证也确实存在；但它把 37 个跨 bounded context、跨 canonical owner、包含多项未确认产品决定的治理项目合并成一个“剩余清债”需求。当前版本尚未闭合需求边界、产品决策、基础设施复用证据、状态机影响范围和可复现审计基线。

当前版本已经将原文定位为全仓审计总索引，并要求按 canonical owner 拆出可独立合入、独立验收的实施需求；这一总体整改方向已满足。后续阻断集中在若干工作包的 owner、幂等 identity、原子性和协议变体定义。

## 2. 审核依据

本次审核以以下事实源为准：

1. 当前任务中用户确认的决定；
2. 仓库根 `AGENTS.md`；
3. 当前 dirty worktree 中的生产源码与 `ztest/architecture/` 门禁；
4. 当前测试表达的既有行为；
5. `zdocs/post-closure-boundary-debt-requirements.md`。

本次只进行静态审核，没有修改生产源码，没有运行生产入口或全仓测试。工作区已有的大规模未提交修改均视为用户现状。

## 3. 阻断问题

### 3.1 单一需求横跨过多 canonical owner

原需求把 37 个工作包及多套独立状态机合并为一个最终验收单元，并以“完成全部工作包后”定义全局关闭条件。这不满足 `AGENTS.md` 要求的每个合入切片独立闭合：

- contract、owner、composition、lifecycle、persistence、observability 和 tests 无法逐切片签收；
- 工作包之间存在交叉依赖，实施期间容易形成半迁移、平行入口或临时双真相；
- 任一 bounded context 未完成都会使整体需求长期处于不可关闭状态；
- B18 可能演变为同时改动所有 domain 的全仓机械重写。

至少应拆分为以下独立需求族：

- Inference request、checkpoint 与 clock；
- Tool binding、snapshot 与 effect settlement；
- Agent delivery、capacity 与 turn queue；
- Workflow durability、inspection 与 reconciliation；
- BackgroundTask settlement 与 retention；
- Artifact reachability 与删除治理；
- Event、Session 与 durable codec；
- Product security activation 与 sandbox enforcement；
- Architecture governance 与最终动态边界 ratchet。

B18 只能作为最后的审计和门禁收口，不应承担跨 domain 的实际重构。

### 3.2 未确认的产品决定被留给实现者

多个工作包要求实现者在实施过程中决定以下产品语义：

- 旧 durable record 是直接拒绝还是一次性 migration；
- Session、Cron、turn、DLQ、service-call、daemon quarantine 的 retention 策略；
- terminal tombstone 的幂等窗口；
- legal hold、用户删除、安全清除的 authority；
- Sandbox 哪些能力属于批准保证，哪些只是 defence-in-depth；
- moderation unavailable 时各真实 consumer 的 disposition；
- daemon 是否允许滚动协议升级及旧版本退出期限；
- queue、payload、scan、retry 和 backoff 的具体上限。

这些不是可由架构原则唯一推导的实现细节。依据 `AGENTS.md`，durable 格式、权限和 retention 行为必须在实施前明确确认，不得由开发者自行选择。

每个独立需求必须增加“已确认产品决定”表，至少明确 migration、retention、authority、failure disposition、容量上限和旧版本退出条件。

### 3.3 非目标与工作包内容冲突

原需求声明不重做 Workflow 状态机、BackgroundTaskPool 或 Agent scheduler，但以下工作包实际改变其 canonical 状态语义：

- B24 改变 BackgroundTask query、result retirement 与 reap 的原子结算；
- B25 改变 Workflow reconciliation codec、claim lease 和查询面；
- B27 改变 turn acceptance、capacity admission、retention 与 compaction；
- B28 改变 Cron reload truth 与 occurrence lifecycle；
- B29 删除 Product live state 链并改变 Workflow resume command。

应删除该非目标，或将上述内容移入独立状态机需求。不能把状态机改造描述为外围类型清理。

### 3.4 缺少基础设施复用与服务面评审证据

原需求要求新增或扩展 Port、codec catalog、deletion claim、retention closure、typed command、lease/fence owner 和 indexed projection，但没有逐项提供 `AGENTS.md §6.4` 要求的证据：

1. 搜索过哪些现有实现；
2. 为什么复用、扩展或拒绝复用；
3. canonical owner 与核心不变量；
4. 最小服务方法及每个真实消费者；
5. 被隐藏的实现、生命周期和并发细节；
6. 防止平行入口、双状态和越层依赖的门禁。

“实施前搜索”不足以完成需求级架构评审。每个拟新增机制必须先提交 owner/复用矩阵。

### 3.5 审计基线不可稳定复现

原需求承认结论来自 dirty worktree，且没有运行测试或生产入口，同时宣称 B1–B37 均有当前生产源码反证。该基线只能代表当前 workspace 的临时快照，不能作为未来 commit 的稳定事实。

需求必须记录：

- 审计所基于的 commit；
- dirty diff identity 或 digest；
- 可重复执行的扫描命令；
- 每个工作包对应的 symbol、路径和结构化证据；
- 实施开始前的失效重检规则。

仅保存关键词命中数不能证明缺陷，也不能证明整改关闭。

## 4. 重要设计问题

### 4.1 B26 不应先定义尚未闭合的未来架构（当前版本已部分吸收）

推荐顺序将 B26 放在所有 domain 改造之前，容易形成“先写门禁，再让源码符合门禁”。门禁应保护已经确认的 authoritative contract，不能替尚未设计完成的 owner、store declaration 和 exception taxonomy 作出设计决定。

B26 当前版本已经拆为两阶段，方向正确：

- 立即修复已确认的假绿，如 dynamic-import alias 漏检、无错误码 ignore、已存在 durable store 的 inventory 漏项；
- 各 domain 完成 canonical 设计后，在同一实施切片补充对应门禁。

### 4.2 B18 的中央 allowlist 可能成为永久债务台账（当前版本已修正）

当前版本已经删除“所有命中进入中央机器可读 allowlist”的要求，改为正式边界 debt 清零、必要动态点就近精确豁免。这一修订符合 `AGENTS.md`。

应改为：

- AST 门禁按正式边界语义检查；
- 只有真正的外部 adapter、strict decoder input 或必要 private erasure 使用局部结构化豁免；
- owner 内部合法索引不进入中央清单；
- 豁免绑定精确 symbol、owner、类别、理由和负向测试；
- 禁止由当前扫描结果自动生成并接受基线。

### 4.3 B3 混淆静态泛型与运行时 JSON Schema

动态 JSON Schema 可以编译为带稳定 identity/fingerprint 的 runtime validator，但不能自动生成可信的 Python 静态类型实参。应明确区分：

- 静态 graph definition：以 `OutputT` 端到端保持泛型关系；
- 动态 JSON graph：输出固定为 canonical `JsonValue` 或 validated document type，以 contract identity、schema fingerprint 和严格 validator 保证隔离。

否则实现者可能通过 `cast` 伪造动态 schema 的泛型关系。

### 4.4 B32 引用了错误的 owner（当前版本已修正）

B32 当前版本已明确 ToolExecutor authoritative effect settlement owner，并声明 B14 不拥有 effect 状态机，原错误引用已经消除。

### 4.5 最终验收强度不足

开发阶段使用定向测试和逐文件 Pyright 合理，但如此广泛的迁移不能只以小批量测试作为最终签收依据。最终验收至少应包括：

- 全部架构门禁；
- 全量 Pyright，或明确且可审计的覆盖清单；
- 各 bounded context 的完整测试；
- Product composition/activation smoke tests；
- durable corruption、restart、CAS 与 fencing 测试集；
- 最终全仓测试，或记录环境阻断及等价替代证据。

## 5. 已抽查确认的真实缺口

静态抽查确认以下源码反证存在：

- B1：`contracts/ports/model/client.py::LLMClient` 仍有宽消息和工具输入；
- B2：`runtime/durable/inference_checkpoint.py` 存在 `InferenceCheckpointState(str(state))`，timer 恢复仍有宽松解析；
- B4：`BoundToolCatalog` 与 `ExecutableToolBinding` 仍持有宽 capability；
- B12：缺失 permission config 时存在构造 bypass 的路径；
- B14：Tool snapshot 直接访问 `executor._catalog`；
- B15：多个 control/presentation 路径使用无 owner 的 `asyncio.ensure_future(...)`；
- B16：Temporal 与 Squilla 固定内部依赖仍通过动态 import；
- B17：多个生产 consumer 直接调用 `subprocess.run/Popen`；
- B27：`LogicalCapacityProjection` 仍以进程内治理为核心；
- B37：万能 provider exception decorator 仍存在。

这些证据支持继续整改，但不能证明原需求中全部目标状态已经获得产品确认，也不能替代逐 owner 的完整审计。

## 6. 需求拆分后的强制模板

每个独立实施需求必须包含：

1. 当前源码反证及对应 revision；
2. canonical owner、identity 和核心不变量；
3. 已搜索、复用、扩展和拒绝复用的基础设施；
4. 已确认产品决定；
5. authoritative contract 与最小真实消费者；
6. 状态迁移、失败、恢复、retention 和 observability；
7. Product composition 与唯一生产入口；
8. migration、直接拒绝或经授权清除的明确选择；
9. 同一切片删除的旧入口、旧 decoder 和旧状态路径；
10. 类型 fixture、负向测试、架构门禁和集成验收；
11. 不受影响的相邻 bounded context；
12. 证明不存在平行 owner、双写、双读、fallback 和兼容残渣的关闭证据。

## 7. 重新进入实施评审的准入条件

满足以下条件后方可重新评审：

1. 原文被明确标记为审计索引，而非单一实施需求；
2. B1–B37 按 canonical owner 拆为独立需求；
3. 每个 durable/security 工作包补齐已确认产品决定；
4. 每个新增机制补齐 `AGENTS.md §6.4` 复用与服务面证据；
5. 删除“非目标”与实际状态机改造之间的冲突；
6. 保持 B32 当前对 ToolExecutor effect settlement owner 的修正，并在独立 effect lifecycle 需求中闭合；
7. 明确静态泛型与动态 JSON Schema 的不同类型保证；
8. 建立可复现、绑定 revision 的审计证据；
9. 为每个独立切片定义可单独合入的关闭条件；
10. 定义最终跨域集成验收，而不只依赖关键词扫描或定向测试。

当前版本已经明确禁止 B1–B37 整体实施。剩余条件满足前，不应把对应工作包转化为已批准的独立实施需求。

## 8. 第二轮源码深审新增结论

### 8.1 B19 遗漏 `MessageQueue` 本身的 owner 错置

`contracts/conversation/queue.py::MessageQueue` 不只是 DTO 或窄 Port。它实际拥有：

- 可变 `_items` 队列；
- `asyncio.Event`；
- push/pop/drain/wait 调度行为；
- JSON dump/load 恢复逻辑。

这违反 `contracts/` 只能拥有纯数据和窄行为契约、不得拥有调度或运行机制的硬边界。当前 B19 仅要求给 `MessageQueue.dump/load` 增加版本化严格 codec，会把错误 owner 固化为更正式的 durable owner。

B19 必须先作 owner 决策：

- 若它是 process-local inbox mechanism，应移动到 Runtime，由 Contracts 只保留 `Message` DTO 与最小 message sink/activity Port；
- 若 Residency 需要恢复 mailbox，应由 authoritative delivery/session owner 持久化版本化 mailbox projection，Runtime queue 只负责重建后的进程内投影；
- `RoleState.msg_buffer` 当前虽 `exclude=True`，仍不能证明 queue mechanism 应归 Contracts；
- 不能在 Contracts DTO 中保存 `asyncio.Event` 或用 Pydantic arbitrary type 掩盖 runtime state。

因此 B19 当前仍是阻断项，不能只做 decoder 加固。

### 8.2 B19 同时混合了两种不同消息机制

源码中至少存在两种不同生命周期：

- `MessageQueue`：Role/Kernel 上下文使用的进程内消息缓冲；
- `orchestration.agents.messaging.Mailbox`：按 Agent runtime ownership、turn boundary 和 delivery id 管理的 mailbox。

它们字段相似，但 owner、调度语义、恢复来源和 durability 保证不同。B19 不能因都承载 `Message` 就建立统一 queue codec 或错误抽象。需求应明确两者的 canonical projection 关系，以及哪一个只属于 Runtime mechanism、哪一个由 Agent durable delivery 重建。

### 8.3 B23 未覆盖 durable delivery 的幂等冲突校验

`AgentDeliveryStore.identity()` 当前只使用：

```text
message.sent_from + target_agent_id + message.id
```

`accept()` 若发现相同 `delivery_id`，会直接返回既有 record，不验证本次请求的 message payload、delivery mode 或其他语义是否与原请求一致。因此相同 message id 被错误复用、payload 改变或 mode 改变时，可能被误报为幂等成功，而不是 identity conflict。

B23 必须补充：

- stable request identity 与 canonical arguments/payload digest；
- 相同 identity、相同 digest 才允许幂等返回；
- 相同 identity、不同 target/mode/payload/generation 必须 typed conflict；
- broadcast/subtree fan-out 的 parent request identity 与每 target child delivery identity；
- receipt 必须返回 canonical record identity、revision 和 disposition，不能只返回 runtime presence。

这与 `AGENTS.md` 对 stable request identity、重复请求不得创建第二事实、effect arguments digest 的要求一致。

### 8.4 B23 的“capacity admission”概念仍不够精确

需求要求在 durable accept 前原子完成 capacity admission，但没有区分：

- durable delivery storage/queue capacity；
- logical Agent capacity；
- resident incarnation capacity；
- concurrent turn capacity。

delivery 的 durable acceptance 上限应由 delivery owner 的有界队列/存储 admission 决定，不能预占并长期持有 resident 或 turn permit。否则一个长期排队 delivery 会泄漏执行容量，或把 delivery admission 与 R2.16 turn admission 错误合并。

B23 必须明确 admission scope、receipt、reservation lifecycle、释放事实及与 B27 turn acceptance 的事务边界。

### 8.5 B22 过度规定所有迁移目标都是 Contracts-owned Port

B22 要求把所有 Role concrete component consumer 迁移到 Contracts-owned Port 或 capability bundle。这个方向对跨层调用成立，但不应机械套用于 Runtime 包内协作或同层 bounded context：

- Runtime 包内可通过 owner 的具体私有类型协作；
- 同层跨 bounded context 应优先使用被调用包的稳定 public service/Port；
- 只有真正跨层、需要依赖反转的能力才应进入 `contracts/ports/`。

否则会把 Runtime 内部实现细节提升成十年稳定的 Contracts API，造成 Port 爆炸和错误 owner。B22 应要求逐 consumer 分类，而不是预先规定统一落点。

此外，当前 `RoleComponentAccessors` 自称 stable Role-facing API，却直接返回 `ToolExecutor`、`RunJournal`、`RuntimeHost`、`SQLiteSubscriptionStateStore` 等具体 Runtime 类型。需求应明确它是待删除的临时现状，不能把 accessor facade 继续保留为第二公共面。

### 8.6 B34 对 RunJournal 的 canonical owner 结论下得过早

当前 `RunJournal` 同时被用于：

- per-session Tool/Inference think、tool、timer step；
- Product Temporal Workflow effect plane，且使用固定 session id `application-workflow-effects`。

这并不能直接证明它们应继续共享同一个 authoritative journal Port。两者的 identity、bounded context、retention、effect reconciliation 和 lifecycle 可能不同：Workflow durable effect 状态原则上应由 `orchestration/workflows/` 闭合，而不是因为底层 JSONL 结构相似就复用 session run-step state machine。

B34 在要求“Tool、Inference 和 Temporal 注入同一 owner Port”前，必须先回答：

- Temporal backend 的 journal 是仅记录通用 execution attempt，还是在承担 Workflow effect truth；
- `StepRecord.kind` 的封闭集合与 Workflow effect kind 是否一致；
- 固定 application-wide journal 与 per-session journal 是否共享 identity/lifecycle；
- Workflow reconciliation store 与 RunJournal 是否形成双 effect truth；
- 若语义不同，是否应保留显式投影而不是强行统一。

在这些问题关闭前，不能以“复用现有基础设施”为由预设同一个 canonical owner。

### 8.7 B20 的通用 Surface 基类只能是严格配置，不得成为新泛用抽象

B20 允许在 `contracts/surface/` 建立 canonical 基类。该基类若仅封装 frozen/strict/forbid-extra 的共同配置，可以接受；但 Canvas、Notebook、stdin 与 execute input 的 version、tag、大小限制和 trust/rendering 语义不同，不能把它们合成统一 document 状态机或万能 envelope。

需求应明确：共同基类不拥有 schema version、tag dispatch、codec catalog 或 extension policy；这些继续归各自 domain declaration/codec owner。

### 8.8 当前修改仍未解除总体审核阻断

当前需求已正确吸收以下意见：

- B18 删除中央永久 allowlist；
- B26 拆分立即修假绿与 domain 同切片门禁；
- B32 修正 B14/IN_DOUBT owner 错误。

但新增核验表明，B19、B22、B23 和 B34 仍存在 owner 或保证边界未闭合问题。它们不得直接进入独立实施评审。

## 9. 第三轮源码深审新增结论

### 9.1 B24 不能把跨资源 cleanup 描述成单一同步原语内的原子事务

BackgroundTask 的 process-local owner 需要协调 asyncio task、output writer、notification、Runtime resource registry 和 residency pin。它们不是同一个可事务提交的存储。当前需求要求这些步骤“在同一 owner synchronization/generation 下按明确顺序推进”，如果被理解为持有一个 pool lock 完成所有 I/O 和 await，会造成死锁、阻塞 submit/cancel，且仍不能获得真正跨资源原子性。

B24 应明确使用 owner-local cleanup state machine：

1. 在 owner/generation gate 下原子进入 `DRAINING` 并禁止新 mutation；
2. 为 output、notification 和 resource retirement 分别记录 process-local typed settlement；
3. 外部 cleanup 不在 pool lock 下执行；
4. 每次返回 owner gate 后复核 generation/attempt，再提交该阶段结果；
5. 所有阶段完成后才提交 terminal cleanup fact并释放 pin；
6. 任一步失败保持 `DRAINING` 和 pin，由同 owner 幂等重试。

这符合 BackgroundTask process-local 语义，不应为它发明跨进程 transaction、durable task registry 或伪原子提交。

### 9.2 B24 需要为 `_retire_result` 推导窄 typed Port，而不是继续使用 callback

当前 `_retire_result: Callable[[str], None]` 无法表达 owner identity、attempt、generation、幂等结果或 cleanup failure。需求已经要求 typed receipt，但还应明确该能力由消费方推导为最小 retirement command Port，至少携带绑定后的 task reference 和 expected attempt/generation。

它不能接收裸 `task_id`，也不能返回 `None` 后靠异常区分全部状态。否则即使停止吞异常，stale attempt 仍可能删除新 attempt 的 resource pointer。

### 9.3 B25 关于 `records()` 暴露“内部结构”的现状描述不准确

`WorkflowReconciliationStore.records()` 当前调用 `_read()`，每次都从磁盘解码并创建新的 dict 和 frozen record；它没有返回 store 内部长期持有的 mutable map。因此“直接返回含 mutable dict 的内部结构”会误导实施者把问题当成可变引用绕过 owner。

真实问题是：

- query contract 无类型且返回裸 mutable mapping；
- collection discriminator 使用字符串；
- caller 可以修改查询副本，但不会直接修改 durable truth；
- decoder 未验证 collection 为 list、重复 identity 会被覆盖。

B25 应修正文案为“返回无类型 mutable query projection”，避免错误声称存在 live internal-state mutation path。

### 9.4 B25 遗漏 reconciliation request identity 冲突

`submit_effect()` 的 identity 只由 `run_id + logical_key` 派生；发现既有 effect 后直接返回，不核验 capability 或 payload。`submit_terminal()` 同样只由 `run_id + destination_id` 派生，不核验 outcome payload。

因此相同 identity、不同 command/outcome payload 可能被误报为幂等成功。B25 必须补充：

- effect identity 对应 canonical command digest、capability、run/definition generation；
- terminal delivery identity对应 destination、outcome digest和run terminal revision；
- 相同 identity且全部事实一致才幂等返回；
- 相同 identity但 capability/payload/generation 不同返回 typed conflict；
- conflict 不得覆盖旧 record或创建第二 effect/delivery。

### 9.5 B25 的 lease renewal 仍缺少“外部动作已发生但 fence 已失效”的落点

续租只能降低 lease 在长调用期间过期的概率，不能消除网络分区、进程暂停或 refresh failure。旧 owner 可能完成外部动作后才发现 fence 已失效，此时不能再写 canonical reconciliation store。

独立需求必须明确：

- provider/process receipt 如何在 current owner 无提交权时保留为可调查证据；
- provider idempotency key或查询 API 如何让新 owner对账；
- 哪些 effect capability允许新 owner重试；
- 无法证明结果时如何进入 canonical `IN_DOUBT`，且不能由 stale owner自行提交；
- lease refresh task的 owner、取消和shutdown settlement。

不能把“执行期间续租”写成崩溃原子性的替代品。

### 9.6 B28 无法仅靠删除 mtime hot reload 排除外部文件修改

删除 scheduler 对 mtime 的依赖，可以防止运行时把任意文件变化当作通知；但如果 `CronTaskStore` 在重启时仍直接读取同一 JSON 文件，那么具有合法 shape 的外部编辑仍可能成为 canonical state。

“未经 owner command 的外部文件修改不会成为合法状态”需要先定义威胁模型和 provenance：

- 若 workspace 文件系统被视为 trusted authority，应把关闭条件改成“生产 API 不支持或承诺 external-edit control path”，不能声称技术上拒绝所有外部修改；
- 若必须检测旁路修改，durable record需要 command revision、writer generation/fence及可验证 provenance，且密钥/authority不能与可编辑文件同域；
- CLI 必须调用 canonical command owner，而不是直接写 store；
- restart decoder 只能验证 shape，不能证明写入是经 command path产生。

当前关闭条件超出了其实施要求能够证明的保证。

### 9.7 B31 必须定义支持的 LSP 合法 tagged union，而不是只列最小 DTO 名称

LSP 的合法结果并非单一 `DocumentSymbol`/`Location` shape。例如：

- document symbol 可以返回 `DocumentSymbol[]` 或 `SymbolInformation[]`；
- definition/references 可能涉及 `Location`、`LocationLink`、数组或 `null`；
- provider capability negotiation决定实际允许的返回变体；
- 不同协议版本和 server capability 可能开放额外字段。

如果 decoder 只按当前列出的最小 DTO fail closed，会把合法 provider response 误判为 corruption。B31 必须先选择：

- 支持完整、版本绑定的合法 tagged union；或
- Product activation 明确只接受某个 capability 子集，并在 server capability negotiation 阶段拒绝不兼容 provider。

“允许未知扩展字段”与“canonical exact shape”也必须按层区分：JSON-RPC envelope exact，LSP 标准允许扩展的对象由 adapter白名单投影，canonical DTO再保持 exact/frozen。

### 9.8 B30 不应默认把 daemon discovery quarantine 提升成业务 durable retention 状态机

Daemon discovery/socket 是 Product-owned 跨进程协调记录，不当然等同 Session、Workflow 或 effect 等 canonical business fact。其 stale 文件需要 generation-safe cleanup 和有界收敛，但是否需要长期审计、legal hold 或 durable quarantine lifecycle必须由真实安全/运维消费者证明。

B30 应区分：

- current discovery record：安全关键协调 truth；
- stale socket/path：经 current owner复核后可删除的本地资源；
- corruption evidence：是否需要短期 quarantine，由 Product observability/incident policy决定。

不能仅因文件跨进程存在，就建立一套新的 durable retention authority。否则会违反“不为也许以后需要增加机制”。

### 9.9 B36 的 indexed projection 不能成为第二条 pending truth

当前 `pending_calls()` 每轮扫描所有 `.jsonl`，性能问题真实存在。但新增 durable index/cursor会引入另一份状态；如果 index 丢失、落后或损坏后能够抑制 canonical journal 中的 pending call，就形成第二真相和 lost wake。

B36 必须明确：

- canonical per-call journal仍是唯一 pending truth；
- index只作为可丢弃、确定性重建的projection；
- 周期性分片 canonical scan必须发现 index 未登记的 open call；
- compaction 后保留的 tombstone能够维持 request identity和幂等；
- cursor只表示扫描位置，不表示 call 已处理或允许跳过未结算 call；
- index corruption不能降级为空 pending集合。

如果 terminal retention与目录分片已经能使扫描有界，应优先采用更小机制，不预设必须新增 durable indexed authority。

### 9.10 第三轮结论

当前审计索引的总体形态已明显改善，以下旧阻断已基本解除：

- 已明确它不是整体实施授权；
- 已按独立需求创建与评审排序；
- 已补充最终全仓验收；
- 已记录 HEAD、dirty digest和重检条件；
- 已吸收 B18、B20、B22、B23、B26、B32 的多项具体修订。

第三轮仍要求修订 B24、B25、B28、B30、B31 和 B36。特别是 B25 的现状描述与幂等 identity、B28 的 provenance 保证、B31 的合法协议 union 属于进入独立需求前的阻断问题。

## 10. 第四轮源码深审新增结论

### 10.1 B1 的 `LLMClient` 当前没有生产消费者，应先判断删除而非类型化

全仓生产代码搜索只命中 `contracts/ports/model/client.py` 中的声明本身，没有 Kernel、Runtime、Orchestration 或 Product consumer import/use `LLMClient`。当前真实模型调用链已经存在：

- `FinalizedInferenceRequest` / `FinalizedGenerateRequest`；
- `contracts/ports/model/inference.py` 的 inference Port；
- `runtime/models/inference_port.py` 的实现链；
- `runtime/models/model_calls.py` 的模型调用适配。

因此 B1 不能预设“类型化 canonical LLM client Port”。按 `AGENTS.md` 的唯一入口、零未使用类型原则，独立需求必须先证明：

- `LLMClient` 是否仍有任何真实消费者；
- 它是否与现有 finalized inference request Port 表达不同且必要的用例；
- 若无消费者，直接删除该文件及公开导出；
- 若确有尚未接入的消费者，不得以未来可能使用为理由保留，必须先由已确认需求建立消费链。

当前证据更支持“删除死 Port并以现有 inference Port 为 canonical truth”，而不是重设计第二个模型调用 seam。B1 的标题、实施要求和关闭条件应据此修正。

### 10.2 B3 同样包含无消费者的 `GraphOutputCommitter` 声明

`runtime/output/graph_committer.py::GraphOutputCommitter` 当前没有生产引用；实际调用链是 `RunGraphTool -> Role capability -> GraphOutputService -> OutputEngine`。独立需求不应为了清除 `Any` 而泛型化一个未使用 Protocol。

B3 应拆分：

- 删除无消费者的 `GraphOutputCommitter`，除非能证明它是实际依赖反转 seam；
- 对真实 `CommitGraphOutput`/`ResumeGraphOutput` capability 和 `GraphOutputService` 做类型闭合；
- 检查 Role capability 是否本身应保留，还是应由更窄的 graph execution service直接注入；
- 不保留同义 committer、service、Role facade 三层平行入口。

### 10.3 B3 的动态 Graph output 实际上不是静态泛型用例

当前 `GraphOutputContractSpec` 是 Product `run_graph` wire model，`schema_` 为运行时 JSON Schema，`GraphSpec.output` 也是动态 binding tree。现行主路径没有静态 Python `GraphSpec[OutputT]` declaration。

因此索引中“对源码静态声明且输出类型已知的 graph 建立 `OutputT`”只有在仓内存在真实静态 graph consumer时才应进入生产设计。否则这是未识别消费者的未来变化轴。

当前可关闭的真实链应优先是：

```text
GraphOutputContractSpec
  -> strict canonical JSON-schema declaration
  -> OutputContract[JsonValue]
  -> OutputEngine[JsonValue]
  -> validated committed output
  -> ToolResult JSON payload
```

若没有静态 graph definition consumer，不应为“也许以后”新增封闭 static/dynamic declaration union。

### 10.4 B8 必须先移动 scope declaration owner，不能只改 ViewEvent

`ScopeRef`/`ScopePath` 当前定义在 `runtime/events/scope.py`，该模块同时拥有 declaration 与 `ContextVar`/`push_scope` 运行机制。与此同时，Contracts-owned `TaskProgressEvent`、`ActivityStartedEvent`、`ToolCallFinishedEvent` 已经使用 `tuple[object, ...]` 表达 scope。

如果只把 Product `ViewEvent.scope` 改为 Runtime `ScopePath`：

- Product 虽可向下 import Runtime，但 Contracts machine event 仍然丢型；
- projector 仍无法证明 machine event scope 到 ViewEvent scope 的端到端关系；
- wire codec仍缺少 Contracts-owned authoritative identity；
- Runtime declaration继续和 ContextVar mechanism混在同一 owner。

正确边界应是：

- 将纯 `ScopeRef` DTO、kind enum/tag和 `ScopePath` declaration 移到合适的 Contracts bounded context（优先核验现有 `contracts/activity`）；
- Runtime 只拥有 ambient `ContextVar`、push/pop 与当前 scope mechanism；
- Contracts machine events、Product ViewEvent、projector和 wire adapter复用同一 authoritative declaration；
- 同一切片删除 `tuple[object, ...]` 和 Runtime-owned重复 declaration，不保留 alias/re-export。

B8 当前只描述 presentation 末端，尚未闭合 declaration owner 与上游 producer。

### 10.5 B6 应拆分 Skill manifest declaration 与 activated/compiled Skill

当前 `SkillDefinition` 同时包含：

- manifest/frontmatter 输入；
- 完整 instructions；
- 本地 `source_path`；
- 运行时计算的 `token_count`；
- `model_post_init()` 中固定使用 `gpt-4o` 的派生行为；
- 未经约束的原始 metadata；
- fork model/effort/tool capability选择。

把该类直接改成 frozen strict manifest仍会混合静态声明、来源证据和 activation projection。尤其 token count依赖实际 tokenizer/model generation，不应由 manifest construction以硬编码模型计算并成为 definition truth。

B6 独立需求应至少区分：

- strict `SkillManifest`：只表达经解析验证的声明字段；
- admitted source evidence：canonical path、content digest、trust/approval generation；
- activated/compiled Skill snapshot：resolved model、effort、allowed tool binding generation、tokenizer identity和计算后的 token cost；
- prompt projection/fork request：只消费已批准 activation snapshot。

原始 YAML metadata只可留在 parser adapter用于错误报告，不能同时作为 canonical manifest和任意扩展权限入口。

### 10.6 B7 的“closed union”与当前 forward-compatible ViewEvent contract冲突

`ViewEvent` 模块当前明确声明它是开放 tagged union，新增事件不应破坏旧 consumer；ACP/AG-UI 对未知 kind 返回空列表。B7 又要求输入必须是 closed/registered union，新增事件必须更新显式 catalog/visitor或得到 ignore disposition。

这两种保证可以通过 generation-bound registry协调，但不能含糊并存：

- 若采用静态 closed union，新增 subtype必须更新 union和所有 exhaustive visitor，旧 consumer不再天然前向兼容；
- 若采用开放注册 union，registry generation和 unknown disposition必须是正式 contract，consumer可显式 ignore不支持事件；
- 不能继续依赖 `ClassVar kind + getattr + untyped handler dict` 假装静态安全；
- wire adapter的“忽略”必须返回 typed projection disposition/metric，而不是无证据的 `[]`。

独立需求必须先确认 Product presentation 的兼容策略。不能仅为消除 `getattr` 把开放协议意外改成全仓封闭 union。

### 10.7 Structured ViewEvent wire 仍存在 `default=str`，B7/B8 未覆盖

`product/interfaces/structured/consumer.py` 使用：

```python
json.dumps(payload, ensure_ascii=False, default=str)
```

这会把未知 scope、Path、enum或其他不可编码对象静默字符串化，掩盖 presentation contract 违约。B8 若引入 typed scope但不建立严格 wire codec，Structured surface仍可能输出非版本化、不可逆的字符串投影。

B7/B8 应明确：

- ViewEvent wire envelope的 version/tag和 strict encoder；
- scope的 canonical wire representation；
- 不可编码字段是 typed projection failure；
- 删除 `default=str`；
- ACP、AG-UI、Structured各自只在 adapter边缘映射 validated ViewEvent，不反向定义 canonical event shape。

### 10.8 B14 不应预设必须新增“可信 Product generation artifact”存储

Tool snapshot恢复确实不能依赖 live closure或磁盘选择任意 factory。但“从可信 Product generation artifact重建 binding”可能被误解为新增一套 durable tool-generation artifact/store。

独立需求必须先核验现有 Product composition blueprint、tool definition generation、MCP/provider generation和 activation digest能否完成确定性重建。优先路径应是：

1. Product按可信配置构造当前批准 generation；
2. snapshot record只能引用已批准 generation identity；
3. Runtime从当前 composition提供的 immutable binding catalog重建；
4. identity/schema/effect/permission digest不匹配则拒绝恢复。

只有现有 composition artifact无法满足已确认的跨重启恢复保证时，才允许新增 durable generation artifact，并须提供 `AGENTS.md §6.4` 的完整复用拒绝证据。

### 10.9 B17 需要处理同步 consumer 与异步 canonical runner 的生命周期差异

现有 `runtime.process.run_fixed_argv()` 是 async one-shot runner，而 File candidate discovery、Git checkpoint和部分 sandbox probe是同步 API。要求它们统一调用现有 runner会引出真实设计选择：

- 是否把这些 consumer改为 async并端到端传播；
- 是否由 Runtime process owner提供语义一致的同步 fixed-argv入口；
- 是否将启动时平台探测移入显式 async activation；
- 禁止在已有 event loop中用 `asyncio.run()`或私建线程/loop适配；
- 同步与异步 runner若共存，必须共享同一底层 argv validation、env policy、output bound和typed disposition，不能形成两个信任入口。

此外，当前 runner在 `env=None` 时继承完整进程环境。B17 的“不继承无关环境”需要由 Product或调用 domain明确最小环境策略；Runtime runner不能自行猜测 PATH、locale、HOME、Git/systemd所需变量。

### 10.10 第四轮结论

第四轮要求在独立需求创建前修订 B1、B3、B6、B7、B8、B14 和 B17：

- B1 与 `GraphOutputCommitter` 应先删除无消费者入口，而不是加固死接口；
- B3 不应在没有静态 graph consumer时新增未来泛型变化轴；
- B8 必须把 scope纯声明移到 Contracts owner并闭合 machine event到wire全链；
- B6 必须分离 manifest、source admission和activated Skill；
- B7 必须确认开放或封闭 presentation union策略；
- B14 必须优先复用 Product composition generation，避免新建第二 artifact owner；
- B17 必须明确同步/异步 runner与环境策略。

其中 B1 的无消费者事实、B8 的 owner错置和 B7 的协议兼容策略属于阻断问题。

## 11. 第五轮源码深审新增结论

### 11.1 B10 的“所有物理删除”范围过宽

B10 要求所有物理删除先提交 durable typed intent并取得 fenced deletion claim。这个保证适用于 canonical Artifact、Session workspace、durable metadata和受 retention/legal-hold约束的资源，但不应自动扩展到：

- 未发布的临时文件；
- 当前进程明确拥有且尚未成为 canonical fact的 staging scratch；
- 可确定性重建、无外部消费者的进程缓存；
- 已在独立 lifecycle内结算的短期 process-local文件。

如果所有 `unlink/remove_tree` 都进入 durable deletion state machine，会制造万能删除服务和大量没有真实 consumer的 identity/receipt。

B10 独立需求必须先建立删除资源分类：

- canonical governed resource：需要 authority、retention closure、claim、fence和audit；
- committed projection/cache：需要验证 canonical generation并可重建；
- uncommitted owner-local temporary：由创建 owner在同一 lifecycle内清理；
- 测试临时数据：仅在明确 test authority和隔离根下处理。

关闭条件应限定为“没有仅凭 mtime/minimum age删除 canonical governed resource的生产路径”，不能泛化为仓内所有物理删除。

### 11.2 B10 不应建立读取所有 owner 私有 store 的巨型 retention manager

Artifact reachability确实必须覆盖所有 committed ownership edge，但 Artifact GC或workspace cleanup不应直接理解 Workflow、Agent delivery、BackgroundTask、Tool、Model、FileOps等每个 store的内部 schema。

应由各 owner通过最小 immutable root/pin/retention projection提供其承诺的边，聚合方只消费统一的 canonical reference/claim contract。任何 source不可读时fail closed保留资源。不能让 collector扫描其他包的私有 map、数据库表或目录结构。

同时应区分：

- Artifact blob reachability closure；
- Session directory retention closure；
- owner identity/tombstone retention。

三者可能共享部分 evidence，但删除对象、生命周期和不可逆阶段不同，不能强行合并成一个“统一 retention closure manager”。

### 11.3 B11 不是一个可独立实施的 canonical owner

B11 同时聚合：

- RunLease codec；
- Session Runtime event family；
- Inference timer/checkpoint；
- inference SQLite restore metadata。

这些分别归 Session lease、Session event codec、Inference execution和Product inference persistence owner。B2、B5又已覆盖其中大部分内容。如果把 B11直接转成实施单，会产生重复 decoder、重复 migration和交叉修改。

B11 应降级为交叉审计索引：每个具体 symbol只能进入一个独立需求；B2负责 inference checkpoint/timer，B5或Session codec单负责 Session event family，RunLease与SQLite restore metadata分别建单。最终仅通过矩阵证明没有漏项，不建立“B11 codec”。

### 11.4 B12 必须区分无 permission engine 与显式批准的 bypass

当前 `build_permission_engine()` 在 `permission_config is None and not require_permission` 时返回 `None`，而在 `require_permission=True` 时会用默认 bypass config构造 engine。真实缺口不仅是 `PermissionConfig(mode="bypass")`，还包括“无 engine”在不同 tool/effect类别中的语义。

独立需求必须先枚举：

- 哪些调用属于 governed effect，必须有 validated activation；
- 哪些纯内部/read-only机制若无 permission engine仍可合法运行；
- ToolExecutor如何从 immutable binding/effect classification判定是否需要 gate；
- `None` 表示 capability unavailable、not applicable还是composition error；
- 显式 Product bypass如何进入 generation、approval/audit identity。

不能简单把所有 `None` 替换成 deny，也不能继续让 `require_permission` bool承担信任边界。应使用 typed activation/disposition表达真实状态。

### 11.5 B12 的 Hook callback注册也必须有 activation lifecycle

HookManager除了配置 command handler，还支持 `register(event, fn, matcher)` 动态注册 Python callback。当前注册：

- 接受任意 event字符串；
- matcher在 fire时临时编译；
- 没有重复 identity；
- 没有 activation generation；
- Role components可在 manager构造后继续注册。

因此“activation前完整编译”必须同时覆盖 config handler和programmatic callback。若动态扩展是真实需求，应使用 Product-owned generation swap：先验证 event、callback identity、matcher和权限收窄语义，再原子发布 immutable snapshot。若不是热更新需求，activation后应freeze注册并拒绝 mutation。

control callback的 malformed return当前虽通过 `_failure_outcome` 可deny，但 `parse_callback_result()` 对未知返回类型先降级为空结果；独立需求应保证 control path在adapter边缘直接得到 typed decode failure，而不是依赖外围异常偶然转成deny。

### 11.6 B13 不能保留低内聚的万能 `RuntimeMaintenance`

当前 `RuntimeMaintenance` 同时拥有：

- advisory code-map reindex/cold scan；
- workspace cleanup与Artifact GC；
- generic `schedule_reconciliation(name, Callable[[], Awaitable[bool]])`；
- Skill hot reload；
- MCP hot reload；
- 多组 task、gate、retry/backoff和shutdown状态。

这些能力的安全性、durability、scope和失败语义完全不同。仅注入 code-map/workspace gate并移除 Role/service locator，仍会留下巨型 manager、任意 callback scheduler和多个无关 lifecycle owner。

B13 应按共同不变量拆分：

- code-map scan owner：advisory、repository scope；
- destructive retention/GC owner：workspace scope、authority/fence、fail closed；
- 每个 durable reconciliation owner：由对应 subsystem管理其scan/wake/backoff，不接受任意 name/callback；
- Skill/MCP reload owner：Product activation generation，不属于 Runtime maintenance通用调度器。

Product composition负责装配这些 owner及其 lifecycle，但不应重新创建一个 `MaintenanceManager` facade。原 `RuntimeMaintenance` 在消费者迁移后应删除。

### 11.7 B13 不应把 CodeMap 与 Workspace gate抽象成同一个 coordination Port

`CodeMapScanGate`和`WorkspaceCleanupGate`表面上都有 acquire/release，但前者协调 advisory scan，后者保护破坏性 cleanup/GC。它们的 authority、失败语义、scope identity和恢复要求不同。

独立需求应为真实 consumer保留不同的窄 service/Port，不建立通用 `CoordinationGate`、字符串 key或万能 permit。相同方法形状不是共享 owner的证据。

### 11.8 B15 必须继续按 Tool lifecycle、Connection lifecycle和Control command拆单

B15 聚合了至少三套 owner：

- Runtime Tool/MCP generation lifecycle；
- Product Connection/human binding/presentation lifecycle；
- Agent interrupt/cancel/steer control command lifecycle。

索引虽允许一个 B项拆入多个需求，但当前 F2需求族没有列入 B15，容易把 ToolLifecycle cleanup错误放进 F8 Product surface单。应修正需求族矩阵：

- Tool/MCP restore、activation、prior-generation cleanup进入 F2；
- ConnectionScope、human binding和presentation close进入 F8；
- interrupt/cancel/steer receipt进入其 authoritative Agent/session control owner，并由F8只消费 command Port。

三者可以共享 typed settlement设计原则，不能共享状态机、registry或cleanup manager。

### 11.9 Connection close需要保留失败 token，而不是只返回聚合错误

`ConnectionScope.aclose()` 当前即使 reset失败也会清除 `_env_bound`，部分路径还丢弃 human token。B15正确指出该问题，但独立需求还必须规定重试状态：

- 每个 telemetry handle、human binding token、projector和port有独立 settlement；
- 已成功阶段不重复执行非幂等 close；
- reset失败时保留唯一 generation token和owner identity；
- connection保持 `DRAINING`，不能重新bind或接收新turn；
- typed receipt返回未结算阶段，允许同 owner幂等续清理；
- process退出前仍失败时由Product policy决定强制终止/泄漏报告，不能伪装 CLOSED。

### 11.10 B16 还遗漏可变 plural-rule registry和公共 catalog mutation入口

i18n问题不只发生在 import-side-effect catalog：

- `register_catalog()` 是公开 mutable process registry入口；
- `register_rule()` 是没有生产消费者的公共 plural-rule mutation入口；
- `_RULES`和 `_CATALOGS` 分别形成 locale generation之外的可变状态；
- `set_locale`的process default与 `ContextVar` active locale需要明确 application/connection scope。

如果当前只有内置 en/zh且没有已确认热加载变化轴，应：

- 删除 `register_catalog`和 `register_rule`公共 mutation API；
- 由Product composition直接构造包含 locale definition、catalog、plural rule和fallback policy的 immutable snapshot；
- connection/request只绑定 active locale identity，不修改全局 catalog内容；
- snapshot generation变更必须整体替换，不能分别修改 catalog和plural registry。

若未来确有外部 locale provider，再通过显式 manifest建立新变化轴；当前不能为未使用的 `register_rule`保留 plugin入口。

### 11.11 B16 的 fixed optional backend不需要伪 catalog

Temporal与Squilla当前都加载固定仓内模块：

- Temporal manifest/catalog没有真实多实现选择；
- Squilla manifest的 `module`字段不驱动实际 import；
- loader仍硬编码模块路径并用 `getattr + cast`验证类。

在没有真实 provider变化轴时，最小修复是模块顶部可选 import或静态 typed factory注入，并删除伪 manifest/loader。不能为了消除动态 import又建立一个只含单项的 Product catalog和generation状态。

### 11.12 第五轮结论

第五轮要求修订 B10、B11、B12、B13、B15和B16：

- B10限定 governed deletion范围，并通过各 owner的最小 edge projection聚合，不建立万能 retention manager；
- B11只作为交叉索引，不形成重复实施 owner；
- B12区分 permission unavailable/not-applicable/explicit bypass，并闭合 callback activation generation；
- B13按不变量拆除万能 RuntimeMaintenance和generic reconciliation callback；
- B15按三套 lifecycle owner拆单并补充可重试close settlement；
- B16删除无真实变化轴的 mutable registry、register_rule和伪 backend catalog。

其中 B13 的低内聚 owner、B10 的删除治理范围和 B12 的 permission activation语义属于阻断问题。

## 12. 第六轮源码深审新增结论

### 12.1 B27 遗漏了 durable turn request identity 的完整语义

`EventDrivenScheduler._stage_and_accept()` 当前以 `session_id + delivery_ids` 的拼接摘要生成 `request_id`。该 identity 没有绑定 canonical message payload digest、输入 generation、root/subtree、Agent lifecycle generation或调度配置 generation。相同 session与delivery id集合下若消息事实不一致，store仍可能把后者误判为 duplicate；反过来，无法证明 delivery id顺序是否属于request identity，也没有 duplicate时逐项核对原始canonical facts的要求。

更直接的断裂是：`notify()`允许调用方把普通 `Message`直接放入 mailbox，而 `TurnQueueIdentity`强制 `delivery_ids`为非空唯一tuple。直接输入到达durable scheduler后会先从mailbox drain，再因空 `delivery_ids`构造 identity而抛 `ValueError`。这不是capacity或retention问题，而是当前durable acceptance根本没有覆盖全部合法输入路径。

B27独立需求必须补充：

- 定义 stable `TurnRequestId`及其canonical preimage，至少绑定Agent/root/subtree、logical input或delivery identity、canonical payload digest、Agent/incarnation/config generation；
- 直接输入必须先成为canonical delivery，或拥有独立且稳定的input identity；不得继续用可空delivery id列表临时推导turn identity；
- duplicate acceptance必须校验全部immutable request facts，identity相同但payload、target或generation不同须fail closed为integrity conflict；
- 明确 mailbox drain、durable accept与restore的顺序和崩溃恢复语义。accept失败、进程在drain后崩溃以及duplicate恢复都不能丢消息或产生第二次逻辑turn；
- delivery集合的排序、批次边界和同一delivery只能进入哪个turn必须由canonical规则确定，不能由当前进程mailbox时序偶然决定。

因此，B27目前的关闭条件不足。除capacity permit、fence和retention外，还必须证明每种输入路径都能形成唯一、可恢复且内容绑定的durable turn request。

### 12.2 B33 应先处理“未接入生产入口”，再决定是否建设完整 admin contract

源码中 `SharedDaemonApplication.admin_read_model()`会构造 `AdminReadModel`，但全仓对 `build_inference_admin_api()` 的搜索只命中其定义和导出，没有 Product entrypoint、daemon lifecycle或server composition调用它；`admin_read_model()`本身也只有定义，没有生产消费者。当前真正启动的是 `SharedGrpcServer`，没有证据表明这套 aiohttp admin surface会被activation。

因此，B33不能直接把未接入的未来能力扩建为一整套query Port、mutation Port、CAS命令和wire schema。独立需求必须先给出已确认产品用例、调用方、监听/transport owner、认证来源、activation/shutdown lifecycle以及与现有gRPC daemon surface的边界：

- 若没有已确认consumer，删除 `product/interfaces/inference_admin_api/`、`AdminReadModel`、daemon projection和 `admin_read_model()`死入口；
- 若确认需要该surface，再按B33现有严格DTO、authoritative codec、generation CAS、typed receipt和fail-closed authorizer要求实施，并由唯一Product composition root显式装配；
- 不得仅因 admin API与主 inference API名称相似就合并二者；它们若有不同authority和lifecycle，应保留不同bounded context，但只能保留真实激活的入口；
- 现有 projection测试只能证明孤立构造行为，不能证明生产consumer或activation，应补production composition/route lifecycle门禁。

B33的类型、动态 `getattr`、宽松JSON和缺少CAS问题均属实，但“是否保留该功能”是更前置的产品决定。该决定未关闭前，阻断新增admin抽象和durable状态。

### 12.3 B35 的 re-admission 不能通过回退 checkpoint 或重新塞回原流实现

`SQLiteSubscriptionStateStore._quarantine_sync()`在一个SQLite事务中写dead letter并调用 `_save_in_transaction()`推进checkpoint；`SubscriptionWorker._quarantine()`随后也把内存 acknowledged/persisted sequence推进到poison event。这个设计使live subscription能够越过隔离项继续前进，但也意味着未来re-admission不能倒退该checkpoint：倒退会重放poison event之后已经处理的live events，并与现有单调检查直接冲突。

B35需要进一步规定独立的DLQ replay语义：

- quarantined entry拥有稳定DLQ identity、revision和独立 replay attempt identity；re-admit是新command，不修改历史checkpoint；
- replay与live stream必须定义去重、顺序和并发边界。原event identity保留，但每次replay attempt及其effect settlement必须可区分；
- re-admit必须重新经过原versioned decoder、当前permission/effect policy和当前consumer generation，不能直接调用旧handler或把记录插回内部队列；
- external effect evidence与quarantine/replay settlement关联；结果未知时进入typed `IN_DOUBT`，不得因checkpoint已前进而purge；
- subscriber lease/fence应属于EventFabric/subscription activation的同一owner状态机，不能为DLQ worker另建平行ownership truth；Contracts只表达generation/fence binding和typed disposition，不泄漏SQLite lease实现。

关闭条件应增加：re-admit不会回退canonical checkpoint，不会与live delivery形成重复外部副作用，并能在重启、takeover和重复command下幂等结算。

### 12.4 B37 当前首先是无消费者死代码，不是待扩展的 moderation Port

全仓搜索未发现 `OpenAIChat.amoderation()` 的生产或测试调用；`handle_exception`也只被该方法使用。当前 decorator捕获所有异常、记录包含 `args/kwargs` 的日志并返回默认 `None`，确实可能泄漏待moderate内容并把provider失败伪装为空成功，但没有证据表明moderation是已交付产品能力。

按零伪预留原则，B37的首选关闭方式应是：

- 若产品没有已确认moderation consumer，删除 `amoderation()`、其import以及仅为它存在的 `error_handling.py`，不得新增typed moderation Port、策略状态机、provider facade或feature flag；
- 同步核实删除后没有残留export、测试fake或文档宣称该能力；
- 只有用户确认具体consumer、调用时机和安全保证后，才设计typed moderation request/result/error disposition，并明确它在prompt/effect前后的authoritative gate位置；
- 即使保留，也禁止记录原始content、完整args/kwargs或secret，provider timeout/error必须fail closed，不能返回 `None`冒充安全结果。

“安全能力应fail closed”不能成为创建无consumer未来抽象的理由。当前B37若直接按完整新contract实施，会违反最小服务面和未识别变化轴约束。

### 12.5 第六轮结论

第六轮要求继续修订 B27、B33、B35和B37：

- B27补齐内容绑定的TurnRequestId、直接输入identity及drain/accept崩溃原子性；
- B33先做生产consumer与activation准入，无consumer则删除整套未接入admin surface；
- B35把DLQ replay建模为独立、fenced、可幂等结算的command，禁止回退checkpoint；
- B37在无consumer事实下直接删除dead method和专用万能异常decorator，不预建moderation架构。

B34与B36在本轮没有发现推翻前述结论的新事实。B27的合法输入路径断裂、B33的未接入生产入口和B37的无消费者事实属于本轮新增阻断问题。

## 13. 第七轮源码深审新增结论

### 13.1 B2 不只是类型签名宽松，当前恢复 decoder 会把不兼容记录降级成“重新调用模型”

`runtime/durable/inference_journal.py::_started_checkpoint()`先从JSON取出checkpoint，再仅保留 `InferenceCheckpointState.__dataclass_fields__` 中存在的key构造对象。它没有拒绝额外字段，缺失字段又会被dataclass默认值补齐。解析失败、shape错误或缺少有效 `model_call_id`时统一返回 `None`；`reconcile_think_journal()`随后会reap无法解析的started record，执行链可能把它解释为没有可恢复调用并重新请求模型。

这与B2“严格decoder”的方向一致，但独立需求还应明确：

- checkpoint corruption、unsupported version、legacy shape、identity mismatch和“确实没有checkpoint”必须是不同typed结果；只有最后一种允许正常开始新调用；
- unknown/extra字段不能被过滤，缺失字段不能靠dataclass默认值伪造。版本migration必须在codec边界显式完成；
- `_completed_result()`对带 `result` 和不带 `result` 的两种payload长期双读，同样需要确认一次性migration与旧decoder退出条件，不能永久猜测格式；
- `reconcile_think_journal()`不得自动reap损坏或未知版本的canonical model-call证据。至少应保留corruption evidence并阻断盲目重付费；若provider请求可能已经发生，必须结合model-call receipt判断是否进入unknown/in-doubt，而不是以“think是pure”概括所有费用与外部调用语义；
- `InferenceCheckpointState.target_lease_expires_at`仍是裸float wall-clock，必须与B2 timer要求一致改为严格absolute instant/clock contract；bool、NaN、Infinity和过期lease均不得恢复。

另外，`InferenceJournal.begin_think()`仍接受 `InferenceCheckpointState | str | None`，并把字符串直接作为 `model_call_id`构造状态；`InferenceCheckpoint.resume()`也保留 `InferenceCheckpointState(str(state))`。独立需求必须删除这些旧形状入口，而不是只在最外层加validator。

### 13.2 B2 不应为三个同层具体协作者机械创建 Contracts Port

`InferenceCheckpoint`只在 `runtime/agent/components/cognition.py` 内由Runtime composition构造，当前传入的是Runtime-owned `InferenceJournal`、context manager和Kernel inference engine。B2直接要求“为journal、message state和engine定义Contracts-owned Port”过度规定了迁移手段：这些消费者和实现未必跨层，且三个协作者的bounded context与生命周期不同。

独立需求应逐依赖判断：

- Kernel需要的外部能力才由消费方在Contracts定义Port；
- Runtime同一bounded context的私有协作可使用精确具体类型或包内Protocol；
- Runtime跨bounded context只暴露被调用包承诺的最小service；
- 不得为消除三个 `Any`一次性建立 `InferenceCheckpointServices`、巨型Port或无其他consumer的稳定抽象。

B2的关闭条件应是正式边界有真实精确类型且依赖方向正确，而不是“所有协作者均提升到Contracts”。

### 13.3 B4 当前同时保留 raw tool 与 compiled binding，独立需求必须先决定唯一catalog元素

`BoundToolCatalog`声明自己存放bound tools，但实际 `_tools: dict[str, Any]`同时接受任意tool和 `ExecutableToolBinding`：

- `register()`不验证元素类型、definition identity或generation；
- 未识别对象由 `category()`默认分类为builtin；
- name、summary、deferred membership继续通过 `getattr`猜测；
- `_live_tools()`把可变map交给 `ToolExecutor`；
- `ExecutableToolBinding.wrapped_tool`公开返回raw capability，catalog schema rendering又依赖该入口；
- `validation_callable`、effect、permission、cleanup和call仍从live capability逐项读取。

因此不能只新增一个Protocol后保留两种catalog元素。B4必须选择并迁移为唯一的immutable compiled executable binding；definition compiler/adapter在注册前完成对raw builtin/MCP/workflow capability的校验，catalog之后不得再见raw instance。所有schema rendering所需信息也应在compiled definition/binding snapshot内，不通过 `wrapped_tool`回穿实例。

需要特别区分两类generation：catalog definition generation与Agent-local deferred/revealed presentation state。当前 `_get_revealed`是读取RoleState可变set的callback，不能被误装进immutable tool definition generation。revealed state只收窄当前Agent可见/可执行集合，不修改canonical definition snapshot，也不能让同一snapshot的semantic identity随callback结果漂移。

### 13.4 B4 与 B12/B14 的边界必须避免三个并行策略 owner

`ExecutableToolBinding`当前还拥有 `BoundApprovalPolicy`任意callback，并在 `check_permissions()`里组合capability自带decision、definition `approval_required`与toolset callback；B12负责Permission/Hook activation，B14负责tool snapshot恢复。如果三项分别新增policy、binding和snapshot generation，很容易形成三个可独立变化的执行真相。

拆单时应固定：

- B4只拥有经编译的tool definition/capability binding及其immutable semantic identity；
- B12的canonical permission/control pipeline消费该binding声明，不把任意approval callback藏回binding；
- B14持久化/恢复的是已激活binding snapshot identity与generation，不复制capability或permission状态机；
- ToolExecutor仍是唯一执行chokepoint，catalog、binding或snapshot都不公开可直接调用的raw capability。

### 13.5 B5 应继续作为 event-family 迁移索引，不能创建全仓万能 codec catalog

`contracts/events/_base.py::DurableFact`被Output、Conversation、Model等多个domain family继承，`runtime/session/codec.py`及file event codec又各有独立envelope和恢复消费者。B5虽已写“按domain event family”，但“建立显式codec catalog”的表述仍可能被实施为一个跨domain中央registry。

独立实施必须按authoritative owner拆分：Output、Session、FileOps、Model routing等各自拥有tag/version、strict decoder、migration与consumer。`DurableFact`最多保留不拥有codec策略的纯辅助声明；更稳妥的是在消费者迁完后删除其通用 `vars(self)` serializer。不得建立全仓event class registry、import-side-effect注册或允许任意event subclass自报tag。

当前generic `from_payload()`只校验顶层字段集合，随后 `cls(**payload)`：它不会按注解验证primitive，许多event dataclass又给identity、revision和fence提供空字符串/零默认值。例如Output commit facts即使字段齐全，也未普遍验证非空identity、非bool正fence及状态关系。第family codec不能把“exact top-level keys”误当严格解码完成。

B5应像B11一样保留最终覆盖矩阵，但每个symbol只能属于一个domain实施单和一个migration owner。

### 13.6 B9 的单项 cipher registry是未识别变化轴，应与反射fallback一并删除

`runtime/secrets/cipher.py`中的 `SecretsCipherConfig`已经明确要求 `cipher`和 `key_path`字段，因此 `build_cipher()`和 `_build_aes()`使用 `getattr(..., default)`确实会掩盖composition错误。除此之外，当前 `_REGISTRY`只有固定的 `aes` builder，没有provider discovery、外部manifest或第二实现consumer。

B9不应把修复设计成“类型化cipher registry”。若当前唯一产品决定仍是AES-GCM，应由Product config严格解析enum/配置，再显式构造AES实现并删除单项mutable registry。只有出现已确认cipher provider变化轴时，才由Product-owned显式catalog选择实现；Runtime不能通过字符串registry自行决定安全策略。

同时，`SecretsCipherConfig`声明位于Runtime实现模块。若它是跨Product/Runtime composition contract，应核实是否已有canonical Product config DTO或Contracts declaration可复用；不能为直接字段访问再造同义config类型。默认cipher只能在Product schema验证阶段产生，Runtime收到的必须是已批准配置。

### 13.7 B21 应把 credential tombstone 与 backend selection 纳入同一恢复事实

B21已正确指出file/keyring commit缺少跨进程CAS。进一步看，`commit(None)`只是写入token为空、generation递增的record；`FallbackCredentialStore`的backend selection则是另一文件，只在构造期持锁。若selection文件存在但所选backend record丢失、被回滚或恢复自不同backup，当前 `load_record()`会返回 `None`，无法区分“从未登录”“已logout tombstone”“credential record丢失”和“selection/record generation不一致”。这会削弱旧refresh不得复活logout的保证。

独立需求必须规定：

- logout保留带subject、backend identity、revision和token generation的durable tombstone，不能被物理缺失替代；
- backend selection与credential record具有可校验的共同identity/generation或由同一subject authority事务化推进；
- backup/restore必须同时恢复selection、record、lock/owner所需元数据，并对partial restore fail closed；
- `load_record() is None`只表示明确的初始absence，不能兼任corruption、丢失或已撤销；
- refresh command绑定读取到的token generation与expected revision，provider返回后再次CAS，确保logout/新login在外部请求期间获胜。

### 13.8 第七轮结论

第七轮要求修订 B2、B4、B5、B9和B21：

- B2区分absence与corruption，删除字符串/宽松双读恢复入口，并按真实层级决定协作者类型而非机械提升Port；
- B4选择唯一compiled binding作为catalog元素，移除raw capability、live map和并行approval入口；
- B5按domain拆分codec/migration owner，只保留覆盖索引，不建中央事件registry；
- B9删除反射默认和无真实变化轴的单项cipher registry；
- B21把logout tombstone、backend selection、credential generation及partial restore闭合为同一subject事实链。

本轮阻断项是B2把损坏checkpoint降级为重新模型调用、B4的raw/bound双catalog元素，以及B21无法区分credential初始absence与撤销/丢失事实。

## 14. 第八轮源码深审新增结论

### 14.1 B18 只能作为正式边界债务索引，不能成为横跨所有 domain 的实施需求

B18列出的Inference、Hook、Tool、Event、Workflow、Session projection等边界分别由不同owner、schema、lifecycle和migration策略管理，并且大量symbol已经被B2、B4、B5、B7、B12、B14、B20、B21、B25、B29等专项需求覆盖。若按B18整体实施，会同时修改多个canonical owner，或创建一个负责“消灭dict”的中央DTO/codec工程。

B18应采用与B11、B5相同的治理方式：

- 保留逐symbol分类与最终覆盖矩阵，不创建B18-owned contract、codec、base DTO或migration；
- 每个formal boundary debt只能归入一个domain实施单，B18记录其owner、目标类型、consumer和验收需求；
- `dict[str, JsonValue]`也不能仅凭类型名判定合法。必须证明开放JSON语义真实存在、值已深冻结、大小/深度受限且不会承担已知状态机shape；
- “正式边界dict debt为零”应由各public surface的typed tests与少量AST ratchet共同证明，不能要求所有内部dict进入中央豁免台账；
- 同一个dict若同时跨durable/wire/service边界，应由最外部adapter解码一次后贯穿canonical type，不能在每层各包一套同义DTO。

因此B18可以作为最终验收项，但不应排入可独立开工的需求队列。

### 14.2 B20 的 Surface DTO 不是只缺少 strict/forbid-extra，还缺少互斥状态建模

`CanvasOperation`以一个model加optional字段表达三种操作。当前只验证upsert必须有element、remove必须有element_id，却仍允许：

- upsert同时携带 `element_id`；
- remove同时携带 `element`；
- clear携带element或element_id；
- remove的element_id不满足Canvas element identity的同一格式约束。

`NotebookOutput`同样以 `output_type`加大量optional/default字段表达stream、execute_result、display_data和error。当前validator只检查media类型和大小，没有拒绝与tag不匹配的字段组合；例如error output可携带stdout name和display data，stream output可携带traceback。仅增加 `extra="forbid"`仍会接受这些协议内的矛盾shape。

B20应要求Canvas operation和Notebook output分别使用真正的discriminated union，或在各domain codec中执行等价exact variant校验。每个variant只拥有自身合法字段，不能靠默认空字符串让非法组合看起来可序列化。

### 14.3 B20 遗漏 Notebook 人机输入的 incarnation fence

`NotebookInputRequest`携带 `request_id`和 `cell_id`，`NotebookInputReply`却只携带 `request_id + value`；二者都没有surface identity、document revision、kernel epoch、connection/human binding generation。kernel restart、document替换或connection重连后，旧窗口提交的同名request reply可能进入新incarnation，单靠自由字符串request id无法证明owner。

独立需求必须补充：

- input request reference绑定Notebook/surface identity、cell identity、kernel epoch、document/request revision和human connection generation；
- reply携带该完整opaque reference及expected revision，并由Notebook input owner以CAS结算；
- stale、already-replied、cancelled、kernel-restarted、owner-gone和invalid-value返回不同typed disposition；
- password input的value不得进入普通event、日志、artifact、exception detail或presentation echo；
- request terminal/cancel、kernel restart和connection close必须原子禁止旧reply继续提交。

B20当前的关闭条件只提schema strictness，未覆盖交互输入生命周期安全，属于需求遗漏。

### 14.4 B26 必须治理“源码字符串断言”产生的另一类假绿

`ztest/architecture/`中存在大量读取源码后断言某个字符串存在/不存在的门禁，例如检查 `LeaseCoordinator`、`assert_current`、`decode_residency_record`、特定调用文本或禁止 `.get(`。这类测试适合临时retirement ratchet，但不能证明调用顺序、owner唯一性、fence确实在mutation前校验或decoder真实fail closed：重命名、注释、死代码、同名无关调用都可能使门禁假绿或假红。

B26除修复dynamic import alias与路径白名单外，还必须对门禁本身分级：

- import、声明、调用图等静态语义使用AST/symbol resolution，不用substring；
- durable CAS/fence、严格decoder、状态transition和生产composition使用可执行负向测试；
- source token只允许验证明确的已删除compat symbol或禁止语法，并记录它不能证明运行语义；
- 门禁必须从真实entrypoint构造对象并触发竞争、stale fence、corruption和activation failure，不能只实例化孤立fake；
- 同一保证只设一个authoritative gate，避免AST scanner、artifact清单和字符串测试分别维护三份范围。

B26第一阶段应同时盘点并替换安全关键的substring门禁，否则新增37项门禁仍可能得到一份更大的假绿集合。

### 14.5 B26 的 active-store inventory 应由 canonical composition 生成，而非再维护一份手写真相

B26要求补齐active stores是正确的，但如果继续人工编辑JSON治理artifact和Product declaration列表，源码新增store后仍可能两处同时遗漏。inventory生成必须从唯一Product composition声明或每个store的显式activation recipe导出，并与真实production entrypoint reachability交叉验证。

测试需要证明：

- 每个声明active的store确实由某个生产composition激活；
- 每个从生产entrypoint可达且实施durable write/restore的authority恰好对应一个声明；
- test-only、migration-only、archive projection和process-local cache不会误列为active canonical store；
- 删除store时其activation、restore、backup、retention和inventory entry在同一切片退出。

不能让“治理artifact生成成功”替代“inventory与生产对象图一致”。

### 14.6 B29 当前在 durable resume command 前直接修改 live state，失败无法回滚

`ResumeTasks.call()`从inspection view取得 `meta.state_snapshot`，随后对override逐字段 `setattr(state, key, value)`；之后才验证node、计算可行性、重新compile/resume并调用 `pool.resume_workflow_result()`。任一步在mutation后失败，Product缓存state已经改变，却没有durable command receipt、expected checkpoint revision或回滚before-image。查询与失败命令因此都可能改变下一次恢复输入。

更严重的是 `_workflow_run()`从 `graph_meta.graph_ref`执行 `build()`，在 `graph_meta.request_id`为空时原地写入uuid，并把 `graph_meta.state/run_state/from_nodes`重新喂给definition.start。虽然随后会查询durable projection，但可执行definition和continuation选择仍由process-local graph_meta参与，尚未做到“只从durable definition identity + checkpoint恢复”。

B29应补充：

- Product先构造immutable typed resume command，不修改任何view/cache；override、from/skip及其canonical digest全部进入command；
- Workflow owner在current fenced run下验证definition digest、checkpoint revision、frontier和字段schema，并原子提交新的resume intent/attempt；
- validation或commit失败不改变canonical state，也不改变inspection projection；
- Product不得从live `graph_ref.build()`恢复已存在run。definition必须从Product批准且由Workflow durable owner绑定的definition catalog按identity/digest解析；
- request identity在首次create前产生并持久化，不能在恢复辅助对象上懒写uuid；
- inspection丢失后，resume所需全部事实仍能从canonical definition/run/checkpoint重建。

### 14.7 B29 还混淆了 BackgroundTask 查询身份与 WorkflowRun identity

`GetNodeState`和`ResumeTasks`通过 `get_bg_pool()`取得 `WorkflowBackgroundPort`，再以名为 `task_id/run_id`的裸字符串查询；`AgentBackgroundTasks`则同时包装process-local `BackgroundTaskPool`和durable Workflow service。即使当前实现能路由，API仍容易让模型可见TaskId与WorkflowRunId共享查询/恢复入口。

独立需求应将两者明确分开：

- process-local BackgroundTask query/cancel使用Agent-bound Task reference；
- durable Workflow inspect/resume使用 `WorkflowRunReference`和对应typed service；
- Product工具adapter可解析模型输入字符串，但必须在边缘解码成唯一identity并拒绝跨域混用；
- Workflow completion向Agent投递可以建立显式destination projection，但不能让BackgroundTask registry成为Workflow run lookup或resume owner。

### 14.8 B32 的启动期 capability probe 不能证明执行瞬间的 enforcement posture

`SandboxRuntime.start()`只在session启动时探测bwrap、cgroup、seccomp和netns能力，并把结果保存在mutable字段；之后每次 `wrap_command/wrap_exec`据此拼装命令。实际执行前，systemd user manager、proxy、netns launcher、BPF文件或trust material可能已失效；当前没有per-spawn activation/health proof，也没有把实际launcher结果反馈到effect settlement。

B32的 `SandboxActivationReceipt`必须区分：

- generation-level已编译plan与静态host capability；
- operation-level spawn permit和执行时验证；
- process启动后可观察到的实际backend/namespace/cgroup/credential evidence；
- 运行中enforcement owner丢失时的终止或IN_DOUBT语义。

一次start成功不能授权该generation下无限期执行。每个effect在spawn前必须校验所需generation仍active、关键资源健康且plan未变；spawn receipt绑定实际argv launcher、namespace/cgroup/process identity和enforcement evidence。若执行前无法证明则不spawn，若外部动作期间enforcement意外失效则由ToolExecutor按effect语义结算，不能补写一张成功activation receipt。

### 14.9 B32 不应把所有 defence-in-depth 失败都提升为同一种外部 effect 状态

B32正确要求批准保证缺失时fail closed，但seccomp hardening、core-dump关闭、filesystem isolation、network-off、credential brokering和resource caps并非每个command都具有相同安全承诺。若统一要求全部能力在所有执行中存在，会让Product无法表达合法的低保证本地命令；若统一允许degrade，又会重现当前问题。

应由Product按command/effect class选择有限、versioned的enforcement profile，profile明确required与advisory controls。Runtime只证明实际posture是否满足profile，不自行降级或扩大权限。advisory control失败仍进入receipt/telemetry，但只有required control失败阻断spawn。profile集合必须由真实产品用例驱动，不能成为用户可任意组合的布尔矩阵或未来配置轴。

### 14.10 第八轮结论

第八轮要求修订 B18、B20、B26、B29和B32：

- B18降级为跨domain覆盖索引，不作为中央“消灭dict”实施单；
- B20补齐variant互斥校验与Notebook stdin incarnation/fence；
- B26治理substring门禁，并让store inventory从真实composition reachability闭合；
- B29禁止Product先改live state再提交resume，分离WorkflowRun与BackgroundTask identity；
- B32增加per-spawn enforcement proof，并以有限Product profile区分required/advisory control。

本轮阻断项是B20的stale human reply风险、B29的失败resume仍污染live state和process-local definition恢复路径，以及B32启动探测与实际spawn之间的安全TOCTOU。

## 15. 第九轮实施准入与依赖拓扑评审

### 15.1 当前需求族仍不是 canonical owner 边界，不能直接转成项目或Epic

第3.1节已经声明需求族不是巨型改动，但当前分组仍会误导建单：

- F1把Inference engine/checkpoint、OAuth credential、shared daemon discovery和未接入admin surface放在一起。它们分别属于model execution、credential lifecycle、daemon supervision和Product API准入，没有共同状态机；
- F2把Tool binding、Permission/Hook、maintenance中的MCP reload、process runner、Sandbox和dead moderation放在一起，至少包含五个activation/lifecycle owner；
- F7把Runtime process-local queue、多个domain event codec、RunJournal、Event subscription和service-call journal并列，它们只共享“durable/消息”表面特征；
- F8把Skill、Presentation、security config、code-map、Connection、Role component surface、LSP和Notebook放入Product大桶，没有共同identity或变化原因；
- F9把architecture ratchet、dynamic registry清理和Cron durable scheduler放在一起。B28明显属于 `orchestration/automation/cron`，不是架构治理owner。

建议把“F族”明确改名为审计视图，不作为ticket hierarchy；实施计划至少拆成以下独立workstream：model execution、OAuth credential、inference daemon、tool binding、permission/hook、sandbox/process、Agent delivery/turn、Workflow、BackgroundTask、Artifact deletion、Session/Event、Cron、Product surface、architecture gates。每个workstream再按其canonical authority拆单。

尤其应把B21从F1移出形成Credential workstream，把B28从F9移出形成Cron workstream；B30与B33也不应因“inference”名称与B2共享实施owner。

### 15.2 第5节的单一线性顺序过度串行，并存在逆依赖

当前顺序把37项近似串成一条链，会造成数周不必要等待，而且有几处与正文要求冲突：

- B9被排在接近末尾，但正文要求security config反射清理先于相关capability activation；它应先于B12/B32或与其首个composition切片同时完成；
- B16动态import/伪registry清理被排在B9之后，但B26第一阶段正需要它提供真实反例和移除错误豁免，应进入最早治理波次；
- B17 canonical runner被排在B32之后，而Sandbox capability probe和process activation本身依赖固定argv runner边界。runner的trust boundary必须先确定，Sandbox integration随后接入；
- B22 Role对象图收敛被排在B24和大量Runtime改动之后，可能迫使前序需求继续通过即将删除的Role accessor装配。至少应先产出consumer迁移矩阵与目标service seam，再由各domain切片顺手迁移；
- B10被排得很早，但其purge/retention实现受三项未确认产品决定阻断。可以先做resource分类和阻止明显误删，不能在retention/authority未确认时进入完整删除状态机；
- B2、B4/B14、B6/B3被强制串行，但checkpoint、tool binding、Skill activation和Graph output存在可分离子切片；只在共享contract或composition generation处建立明确依赖即可。

实施顺序应由DAG表达，不应使用一个总序列。每个节点只列真实前置contract/decision和共享修改面。

### 15.3 应采用四个准入波次，而不是等待所有产品决定后“全面开工”

建议将实施准入改为：

1. Wave 0：事实与门禁修复。完成B26已确认假绿、owner/consumer矩阵、production entrypoint reachability和dirty HEAD digest；不改变durable格式或产品语义。
2. Wave 1：确定性删除与局部收敛。重新搜索后删除B1、B33、B37死入口，删除B9单项registry/反射fallback、B16无consumer registry和固定伪catalog；这些切片仍各自独立签收。
3. Wave 2：不依赖retention默认值的contract/owner修复。包括process runner trust boundary、Role consumer迁移、严格adapter decoder、raw capability入口删除等；任何durable schema改变仍需对应migration决定。
4. Wave 3：状态机与持久化治理。Agent、Workflow、BackgroundTask、Artifact、Cron、Event subscription、RunJournal、service journal、Sandbox/effect分别在产品决定闭合后实施。

Wave之间不是全局barrier：某个domain的产品决定和独立需求先闭合，就可进入下一波；其他domain继续调查。这样能避免一个LSP或retention决定阻塞无关的dead-code删除。

### 15.4 产品决定表缺少适用范围、决策者和关闭证据，仍无法作为真实准入门

第3.3节列出20项未确认决定，但只有问题和默认处理，没有：

- decision id与适用的具体B项/symbol/store；
- accountable product owner/批准authority；
- 当前状态、候选方案、决定日期和版本；
- 哪些调查/删除工作不受该决定阻断；
- 决定落入哪个authoritative contract/schema及验证链接；
- 决定变化时哪些已签收需求必须失效重审。

如果保持“默认全部未确认”，团队无法机械判断某张独立需求是否可开工，只能再次解释整张表。应建立decision ledger，每项至少为 `OPEN / CONFIRMED / SUPERSEDED`，绑定affected implementation tickets与contract evidence。未确认只阻断会固化该产品语义的步骤，不阻断只读调查、consumer复核、测试反例和确定性死代码删除。

### 15.5 缺少独立需求之间的共享契约变更与冲突控制

多个B项会同时修改高冲突文件：Contracts exports、Product composition、RoleComponents、ToolExecutor、RunJournal、Workflow durability和architecture governance。仅要求“独立切片”不能避免并行分支分别发明同义DTO或反复修改同一composition root。

每个实施单还应登记：

- 写入集合与共享热点文件；
- 新增/删除的public symbol和authoritative owner；
- prerequisite contract revision与consumer迁移名单；
- 与其他活动需求的互斥或合并点；
- 合并后需要重新运行的cross-domain gate；
- 若上游contract变化，本需求证据如何自动失效。

可以并行的是不同owner的调查、adapter整改和测试；同一authoritative type、store schema或composition recipe只能有一个当前writer需求。

### 15.6 “可独立回滚”不适用于所有零兼容migration切片

第3.2节要求“可独立回滚/签收边界”，但AGENTS原则同时禁止长期双读、双写和compat path。对已经提交durable schema写入、identity推进或不可逆purge的切片，代码回滚可能让旧binary无法读取新事实，不能笼统承诺Git级回滚。

独立需求必须区分：

- pre-activation rollback：新schema尚未发布，可以撤销代码；
- forward-only migration：activation后只能通过已设计的forward recovery继续，不能部署旧writer；
- operational rollback：切回旧服务generation，但前提是wire/store版本明确兼容且不是长期双路径；
- irreversible action：purge或安全清除只能依赖授权、claim、备份/证据和阶段receipt，不存在代码回滚。

建议把模板中的“可独立回滚”改为“明确activation point、rollback/forward-recovery策略和不可逆边界”。

### 15.7 最终验收缺少运行规模与故障演练证据

第6节覆盖类型、decoder、composition、门禁和全仓测试，但治理目标还包括长期有界运行、跨进程takeover和副作用对账。仅单元/架构测试不足以证明这些保证。

最终签收还应包含按风险选择的确定性故障演练：

- 两个进程竞争同一lease/CAS，旧owner在外部动作前后分别失效；
- mailbox drain、turn accept、delivery ack各crash point恢复；
- Workflow/credential/RunJournal在partial write、torn tail、mixed backup generation下fail closed；
- terminal记录达到规模阈值后的scan latency、storage bound和compaction并发；
- Sandbox required control在activation后、spawn前及运行中失效；
- Product entrypoint重启后只恢复批准definition/config identity。

这些不是要求一次性做全系统压测，而是每个durable authority提供与其承诺匹配的fault-injection fixture，最终再做少量跨owner演练。

### 15.8 第九轮结论

需求索引的技术问题覆盖已接近完整，但实施规划尚不能直接作为“全面开工”依据。必须继续修订：

- F族降级为审计视图，B21、B28、B30/B33等回到各自canonical workstream；
- 用依赖DAG和四个准入波次替换单一线性总序列；
- 建立带状态、owner、scope和contract evidence的产品decision ledger；
- 为并行实施登记共享文件/contract writer冲突；
- 将笼统“可回滚”改为activation、rollback/forward recovery和不可逆边界；
- 最终验收增加按authority的fault injection与有界性证据。

完成上述计划层修订后，可以正式启动Wave 0和Wave 1；不需要等待所有durable/retention产品决定关闭。完整状态机改造仍必须逐domain准入，不能以“总索引已评审”整体授权。

## 16. 第十轮内部一致性与签收证据评审

### 16.1 Dirty worktree基线摘要不完整，当前值不能复现审计源码

第8节使用 `git diff --binary | sha256sum`记录tracked dirty digest，但该命令只覆盖unstaged tracked diff，不覆盖：

- index中的staged diff；
- untracked文件内容；
- intent-to-add、submodule或文件模式等需要单独表达的状态；
- 审计过程中持续变化但最终恢复为相同普通diff文本的路径元数据。

当前工作区有数百个tracked修改，并且需求与评审文档为untracked。仅保存untracked文件清单不能证明其内容，也不能重建审计时的源码。实施前的证据刷新必须至少分别保存：

- `HEAD`；
- `git diff --binary HEAD`或等价的index+worktree tracked patch digest；
-所有scope内untracked文件的相对路径、mode、size和content digest；
- symlink target、submodule state及删除/rename事实；
- AGENTS.md digest，因为它是高优先级事实来源。

更稳妥的是生成一个只读source manifest：对 `contracts/kernel/runtime/orchestration/product/ztest/zdocs`及根AGENTS.md逐文件记录path/type/mode/content SHA-256，再对canonical排序后的manifest取总digest。不得把生成物写入生产目录或让manifest成为第二源码真相。

### 16.2 基线失效规则需要按证据依赖收窄，不能每次HEAD变化都全量重审

第8节同时说源码变化后相关结论失效，又把 `HEAD`、tracked digest、untracked集合任一变化列为失效触发。大型dirty仓库中，若任何无关文档或测试变化都让全部B1–B37证据失效，实施将无法推进；如果团队实际忽略该规则，基线又会沦为形式材料。

每项B证据和独立需求应登记evidence dependency set：涉及的symbol/path、consumer search scope、composition entrypoint、gate和产品decision。source manifest变化后先计算受影响集合：

- owner/definition/consumer/composition/gate范围变化则该项必须重审；
- 只改变无关bounded context时保留证据，但记录新manifest与依赖判定；
- 跨全仓“无消费者”“唯一入口”“无第二owner”结论依赖全局搜索，任何新增生产文件或相关import/call pattern变化都必须重跑；
- AGENTS.md变化按新规则重新判断所有受影响需求。

这样才能同时保证可复核性和持续实施能力。

### 16.3 “仓内无消费者”不足以授权删除已公开export

B33的 `build_inference_admin_api`、B16的 `register_catalog`等通过包 `__init__.py`或 `__all__`公开。仓内无production call site只能证明内部未使用，不能证明没有仓外plugin、嵌入式调用方或已发布API承诺。AGENTS要求迁移仓内消费者并删除旧入口，但外部public contract是否存在仍属于产品事实，不能仅用 `rg`决定。

Wave 1删除准入需要增加public-surface retirement核验：

- symbol是否从稳定包入口导出、出现在用户文档/example、类型stub、plugin contract或发布说明；
- 当前项目是否承诺semver/public API，谁有authority批准breaking removal；
- 若从未承诺且无外部surface，记录为internal accidental export并直接删除；
- 若属于真实公共能力，必须由Product owner确认直接breaking replacement策略；不能因外部消费者未知而保留compat alias，也不能擅自删除。

建议新增D21“public API retirement authority”，只影响实际被证明为承诺public surface的删除；不应让每个private死方法都等待该决定。

### 16.4 Wave 2 的“strict adapter decoder”仍可能改变产品协议，不能按波次自动授权

把strict decoder列入“不依赖默认产品语义”的Wave 2过于宽泛。拒绝额外字段、未知tag、旧版本或宽松primitive会改变现有wire/durable输入的接受集合；若存在外部producer或旧持久数据，就分别依赖协议兼容决定或D01 migration决定。

Wave 2只能包含：

- 当前authoritative schema已经明确、现实现错误接受非法输入，且没有旧数据/外部兼容承诺的adapter修复；
- 只在外部adapter后增加canonical projection而不改变已承诺wire acceptance的切片；
- 先失败fixture、inventory与decoder设计。

任何删旧decoder、拒绝曾被承诺的合法variant、改变wire version或持久格式的工作仍进入对应decision准入。Wave表示可规划范围，不是语义变更授权。

### 16.5 DAG 应把“核心能力”与“Product集成”拆成节点，避免整项伪依赖

当前最小依赖边仍以B编号表示，例如 `B6 -> B4`、`B4 -> B12/B14/B3`。一个B项内部包含多个可独立切片，这种表示容易把整个B4都阻塞到Skill完成，或让所有Permission工作等待完整Tool catalog迁移。

应将边改成具体交付物：

- compiled tool binding authoritative type -> permission pipeline消费binding声明；
- compiled binding generation identity -> snapshot restore codec；
- activated Skill tool-selection projection -> Product tool catalog publication adapter；
- graph executable adapter -> ToolExecutor dispatch integration；
- scope declaration type -> machine event迁移 -> presentation projector -> wire codec。

只有被下游实际import/消费的contract revision构成依赖。B编号继续只用于追踪发现覆盖。

### 16.6 缺少B项到实施ticket及签收状态的机器可检查closure ledger

文档要求每个具体反证只有一个实施单负责，但目前没有规定如何证明37项中的每个symbol最终已分派、签收或因源码变化失效。仅靠Markdown段落和ticket描述会出现漏项或两个ticket都声称owner。

需要一个可审计closure ledger，至少记录：

- finding id + 精确symbol/path + evidence revision；
- canonical workstream/owner + implementation ticket id；
- disposition：`OPEN / ASSIGNED / IMPLEMENTED / VERIFIED / OBSOLETE_BY_SOURCE_CHANGE`；
- prerequisite D-ID/contract revision；
- deletion/migration/retention/fault-test evidence；
-唯一writer与最终merge revision；
- 验证命令和结果artifact。

ledger可以是严格schema的治理artifact，但必须由架构门禁校验“一条active finding恰有一个owner ticket、VERIFIED都有仍有效的源码证据”，不能自动把当前扫描结果标成accepted。它只追踪closure，不成为production owner或运行时registry。

### 16.7 最终验收中的全称保证需要精确定义适用集合

第6节的“所有durable decoder”“每个durable authority”“每个lease/CAS authority”等表述方向正确，但若没有由production reachability生成的适用集合，仍会在签收时依赖人工解释。

最终验收前必须冻结并生成：

- active durable authority set；
- active lease/CAS mutation authority set；
- public Port/factory/registry/callback/checkpoint/codec set；
- production entrypoint/composition recipe set；
- approved dynamic boundary set。

每个全称门禁遍历相应集合，并确保集合自身与production对象图双向一致。test fake、archive reader、migration-only tool和process-local cache需要typed分类而不是口头排除。否则“全部通过”仍无法证明没有漏扫对象。

### 16.8 文档存在一处机械重复和术语状态不一致

第7节“不为迁移保留旧 API 或双 codec”重复两次，应删除一项。另需统一使用：

- B1–B37为finding，不称工作包；
- F1–F9为audit view，不称需求族或实施边界；
- workstream不是ticket，仍须按authority拆单；
- Wave是准入阶段，不是全局barrier或实施授权；
- closure ledger的状态与decision ledger状态分开，不能都用模糊“完成/关闭”。

这些虽不改变架构，但会直接影响项目管理工具如何建单和统计。

### 16.9 第十轮结论

需求正文已经吸收第九轮的大部分结构意见，距离可启动Wave 0/1只剩最后一层治理元数据闭合：

- 用完整source manifest替代不完整dirty diff摘要；
- 按evidence dependency set计算局部失效；
- 对已公开export增加public API retirement authority核验；
- 收窄Wave 2 strict decoder的自动准入范围；
- 把B编号依赖细化为具体contract deliverable DAG；
- 建立finding-to-ticket closure ledger；
- 为最终全称门禁生成production-reachable适用集合；
- 清理重复条目并统一finding/audit view/workstream/Wave术语。

完成这些修订后，审核阶段可以从“继续发现结构问题”转入独立需求编写与产品decision确认；后续无需继续对总索引做无限轮泛化评审，除非源码基线或产品目标发生实质变化。

## 17. 第十一轮开工前验收新增结论

### 17.1 “Production reachability”不能只基于一次默认配置激活

第6节要求从冻结的production entrypoint/composition生成五类适用集合，但Mote存在Textual/ACP/AG-UI、optional backend、Cron、shared inference daemon等按Product配置或入口选择的生产路径。若只实际构造默认CLI配置，未启用的合法生产recipe、store和Port不会进入集合，从而在最终门禁中被误当成不适用。

应区分：

- `production-capable recipe set`：所有受支持entrypoint及经Product schema批准的可选activation branch；
- `activated instance set`：某个具体部署/config generation实际启用的owner；
- `retired/unreachable set`：源码存在但没有任何批准recipe可达，应删除或明确为migration/test-only；
- `external public declaration set`：即使不由本进程activation，仍可能被仓外消费者导入的正式contract。

最终架构治理遍历production-capable recipes，不要求执行配置值的笛卡尔积；每个独立变化轴至少覆盖关闭、每个合法实现和activation failure。运行期health/receipt则只针对具体activated generation。Public Port集合也不能仅从运行时reachability生成，必须结合package export/public-symbol classification和D21证据。

### 17.2 五类适用集合需要不同的发现算法，不能由一个中央reachability扫描器推导

五类集合的事实来源不同：

- durable authority来自explicit store/activation recipe加实际writer/restore静态反向扫描；
- lease/CAS authority来自mutation service声明与负向并发fixture；
- public contract来自authoritative package export、public symbol classification和文档/API承诺；
- entrypoint/composition来自Product entrypoint catalog与构造链；
- dynamic boundary来自显式manifest/catalog及AST dynamic-import扫描的差集。

可以生成统一签收报告，但不能建立一个万能“架构对象发现器”并让它成为新的owner。每类集合由对应governance owner生成，最终只做交叉一致性检查；任一发现器失败或范围不完整都使签收fail closed。

### 17.3 Closure ledger缺少稳定的子finding identity

B1–B37中很多finding包含多个symbol、owner甚至不同实施结果。仅记录 `finding_id + symbol/path`无法稳定引用：文件移动、同一symbol多个独立缺口或一个finding拆成多个owner ticket时，外部ticket和验证artifact难以引用唯一项。

Wave 0应为每条原子反证分配不可复用的stable evidence identity，例如 `B04-E001`。identity一经发布不随文件移动或修复重用；记录中另存current symbol locator和source digest。拆分规则应是“一条identity对应一个owner、一种风险/关闭条件和一个最终disposition”。同一symbol存在raw-capability exposure与permission旁路两类问题时，应有两个evidence identity并明确各自ticket，不能用路径唯一化。

后续扫描发现的新债务使用新identity，不改写旧记录；旧记录因源码变化obsolete时链接replacement finding或no-debt evidence。

### 17.4 Closure ledger自身尚未定义schema owner、存放位置和更新authority

第3.5节要求严格schema ledger和架构门禁，但没有说明：

- schema由哪个development-governance bounded context拥有；
- canonical ledger是版本控制文件、CI artifact还是外部ticket系统投影；
- 谁能把状态推进到ASSIGNED/IMPLEMENTED/VERIFIED；
- 两个并行分支如何CAS/合并同一记录；
- result artifact链接失效或CI retention到期后如何处理；
- ledger损坏、未知版本和source manifest mismatch如何fail closed。

建议使用仓内版本化、strict、reviewed declaration作为closure truth，并由CI生成只读验证报告；外部issue/PR ID只是引用，不作为唯一状态真相。ledger更新与对应代码/测试在同一切片review，VERIFIED只能由门禁根据有效证据判定或校验，不能由脚本自动把当前状态批量接受。具体路径应放在架构治理bounded context下，不能放入Runtime/Product生产包，也不能依赖import副作用加载。

### 17.5 不应强制依赖仓外ticket系统或“merge revision”才能开始本地实施

当前closure ledger要求implementation ticket ID、writer ticket和final merge revision，但仓库工作流未在本任务中确认存在GitHub/Jira或必须先commit。若把外部ticket设为必填，Wave 0会因工具/流程不存在而阻塞；若用随意字符串填充，又失去治理价值。

应定义repository-local `implementation_requirement_id`为authoritative identity，例如独立需求Markdown的版本化ID/path+digest。外部ticket/PR可选映射。变化证据分阶段记录：

- working change：base source-manifest + current source-manifest/patch digest；
- integrated change：integration commit/tree identity；
- verified release：验证所针对的immutable source tree和result artifact。

最终无保留签收可以要求immutable integrated revision，但本地编码和review不能要求不存在的merge commit。唯一writer约束绑定requirement ID与write-set lease/登记，不依赖特定托管平台。

### 17.6 Source manifest必须是可寻址、不可变且保留期覆盖整个治理周期的证据

第8节允许manifest写入 `/tmp`。这适合临时计算，却不能作为closure ledger、decision evidence和数周实施周期的引用目标；文件被清理后，VERIFIED记录无法复核。

要求应改为：生成过程可使用 `/tmp`，最终manifest和扫描结果必须发布到内容寻址、不可变、访问受控且retention覆盖治理及审计窗口的artifact store，或以经过review的仓内治理artifact保存必要摘要。artifact identity包含schema version和content digest；敏感路径/content不得无必要上传。若artifact丢失，依赖它的VERIFIED状态失效，不得只凭记录中的旧digest继续签收。

### 17.7 Decision ledger也需要domain scope，不能用一个D01决定覆盖所有durable格式

D01–D03、D07横跨多个authority。不同store可能分别选择保留、migration或直接拒绝，retention和删除authority也不会共享统一期限。一个全局D01标记CONFIRMED会错误暗示B2、OAuth、Workflow、daemon和RunJournal全部采用同一策略。

这些ID应是decision family，实际确认必须产生scoped decision instance，例如 `D01-model-checkpoint-v1`、`D01-oauth-record-v1`，绑定domain/schema generation和独立authority。总表只描述必须回答的问题，不能整体推进为CONFIRMED。独立需求引用具体instance；SUPERSEDED也只使其scope内证据失效。

D04、D09、D12等若对多个profile/protocol/surface分别作决定，同样按真实Product scope实例化，避免一个布尔状态吞掉差异。

### 17.8 Wave 0交付物仍缺少明确的“允许建首批需求”门槛

Wave 0列出多项工作，但没有最小exit criteria。建议至少要求：

- 新source manifest及持久artifact identity有效；
- 原子subfinding ledger已生成，所有active记录为OPEN且无自动接受；
- production-capable recipe catalog与五类集合的owner/生成方法已登记，即使内容仍待domain补齐；
- B1/B33/B37/B9/B16候选删除的consumer/public-surface审计已分别完成；
- 首批独立需求拥有repository-local requirement ID、write set、唯一writer和所需D-instance状态；
- 已知B26假绿先失败fixture存在，确保后续修复不是修改期望值自证。

满足这些条件即可进入对应Wave 1 ticket，不必等待所有37项分派完成。

### 17.9 第十一轮结论

第十轮提出的结构已被需求正文充分吸收。当前剩余阻断集中在治理artifact本身：

- 生产适用集合必须覆盖所有批准recipe/optional branch，而非单一默认activation；
- 五类集合各自由真实governance owner发现，不建万能扫描器；
- closure ledger需要原子subfinding identity、版本化schema owner和仓内authoritative declaration；
- 本地实施使用repository-local requirement identity，不强依赖外部ticket/merge commit；
- source manifest必须持久、不可变、内容寻址；
- D01等跨域决定必须实例化为domain-scoped decision；
- Wave 0需要明确exit criteria。

完成上述补充后，总索引已具备停止循环评审、正式生成Wave 0交付物和首批独立实施需求的条件。继续评审的边际收益将很低，除非这些治理artifact的具体schema草案暴露新的owner或状态问题。

## 18. 第十二轮治理artifact状态机一致性评审

### 18.1 Source manifest与closure ledger存在内容摘要自引用

第8节要求source manifest覆盖全部 `zdocs/` tracked/untracked entry；第3.5节又要求closure ledger记录evidence source-manifest identity，而ledger本身位于 `zdocs/architecture/post-closure-finding-ledger-v1.json`。如果ledger内容写入manifest digest，manifest又包含ledger content digest，就无法得到稳定有限的固定点：更新任一方都会改变另一方。

必须拆成两层证据：

- source baseline manifest只覆盖被审计的规则、生产源码、测试与普通需求文档，但明确排除由该baseline派生且会反向引用它的closure ledger、scan result和generated verification report；
- governance evidence manifest单独记录ledger/schema/report自身的digest，并引用已经冻结的source baseline identity；它不被source baseline反向包含。

排除必须是精确path与schema-owned规则，不能用整个`zdocs/architecture`目录豁免。AGENTS.md和原需求文档仍应进入source baseline；评审/ledger若需审计，则由上层evidence manifest覆盖。否则Wave 0无法生成自洽artifact。

### 18.2 `implementation_requirement_id`不能包含可变content digest

当前定义用“versioned ID + canonical path + content digest”组成authoritative requirement identity。独立需求在评审中每次修改都会改变digest，从而改变identity；closure ledger的ASSIGNED owner、write-set登记和DAG边会全部失效。这与stable identity原则冲突。

应分离：

- stable `implementation_requirement_id`：创建时分配、不可复用，不因rename/content修改变化；
- `requirement_revision`：单调revision或immutable content/tree digest；
- `canonical_locator`：当前path，可随受控move更新；
- `reviewed_revision`：当前获准编码的精确revision。

上游需求内容变化只使依赖的reviewed revision失效，不创建新的逻辑requirement identity。只有语义被拆成不同owner/状态机时才新建requirement ID并显式supersede/split旧项。

### 18.3 OPEN finding与“每条active finding恰有一个owner”直接冲突

closure disposition定义OPEN为“尚未分派”，但随后门禁要求“每条active evidence identity恰有一个owner requirement”。Wave 0 exit又要求active记录初始为OPEN。按字面执行，刚生成ledger门禁就必然失败。

状态不变量应改为：

- OPEN：owner requirement必须为空，writer为空；
- ASSIGNED：恰有一个owner requirement和一个current writer登记；
- IMPLEMENTED：保留同一owner，具有working/integrated change evidence；
- VERIFIED：保留owner并具有有效reviewed decision、immutable source revision和verification evidence；
- OBSOLETE：无current writer，必须有replacement/no-debt evidence。

Wave 0允许OPEN；进入某个Wave 1/2/3 ticket前，该ticket覆盖的evidence必须ASSIGNED。最终无保留签收时active debt不得仍为OPEN/ASSIGNED/IMPLEMENTED，只能VERIFIED；OBSOLETE单独证明不属于“已修复”计数。

### 18.4 `ztest/architecture`不能成为closure ledger的状态owner

当前写“schema/governance owner由 `ztest/architecture/` 门禁维护”。测试应验证声明，不能拥有或推进治理状态，否则会让测试代码成为需求分派、review authority和生产事实范围的真相源。

应明确：

- ledger/schema declaration归development architecture-governance文档/治理bounded context拥有；
- review流程或明确governance maintainer授权状态transition；
- `ztest/architecture`只严格解码、校验状态不变量、引用可达性和与源码的交叉证据；
- CI生成报告但不修改canonical ledger；
- Product composition只提供production recipe/store事实，不反向拥有development ticket状态。

仓库可以选择 `zdocs/architecture/`作为声明位置，但不能因为文件由测试读取就把owner写成测试目录。

### 18.5 Git ledger不需要伪造Runtime式CAS状态机

当前要求“ledger record revision/CAS和write-set登记合并”。显式record revision有利于审计，但仓内版本控制文件的并发由Git base revision、review和merge conflict管理；再实现一套运行时CAS store会制造无consumer开发基础设施。

最小机制应是：每次更新声明expected ledger schema/revision与base source identity；CI拒绝revision回退、重复evidence identity、非法transition和同一write-set的并行ASSIGNED writer；Git合并冲突由review解决。除非存在真实的多进程ledger service consumer，不新增lock、lease、transaction store或后台协调器。

### 18.6 VERIFIED不能完全由架构门禁“判定”

门禁可以验证证据存在、hash匹配、测试通过、decision状态和transition合法，但不能独立判断产品决定是否由正确authority批准、风险是否可接受或人工对账证据是否充分。另一方面，也不能允许人工直接把任意项标为VERIFIED后让测试照单全收。

应采用双条件：

- governance reviewer/批准authority对指定immutable revision签署verification declaration；
- architecture gate机械验证declaration authority、scope、证据完整性和当前源码有效性。

CI不自动推进canonical状态，只验证拟议transition；人工批准不能绕过机械失败。这样才形成清晰authority链。

### 18.7 证据保留应区分长期规范证据与可再生运行artifact

当前规定任一result/source artifact retention到期就让VERIFIED失效。对于最终已经绑定integration commit/tree、严格测试源码和decision contract的finding，如果一次CI原始日志按正常策略过期，不应导致架构债务自动重新打开；否则系统需要永久保存所有日志，形成新的无界retention问题。

应分类：

- normative evidence：source tree、ledger、decision、contract/schema和测试定义，必须长期可寻址；
- reproducible evidence：命令、环境/依赖identity和deterministic fixture，可在需要时重跑；
- ephemeral diagnostic artifact：完整日志、profile、临时scan output，按有界retention保存；
- irreplaceable evidence：外部effect/provider receipt或不可重现migration proof，必须按对应domain retention长期保存。

只有normative或不可替代evidence丢失、或reproduction失败，才使VERIFIED失效。普通成功日志到期不应单独触发重开。

### 18.8 最终验收第18项仍使用已废弃的旧模型

第6节第18项仍写“owner ticket、merge revision、五类集合均由production reachability生成”，与正文最新规则冲突：

- authoritative identity已经改为repository-local requirement ID，external ticket可选；
- evidence分working/integrated/verified阶段，不应只写merge revision；
- 五类集合由不同governance owner和不同算法生成，不能都称production reachability；
- active finding在Wave 0可以OPEN，只有最终签收才要求所有active debt VERIFIED。

第18项应改为：最终签收时每条未obsolete原子evidence均为VERIFIED，绑定stable requirement ID、reviewed requirement revision、scoped decision、immutable integrated source identity和有效规范/不可替代证据；五类集合分别由其authoritative discovery method生成并通过交叉完整性校验。

### 18.9 Decision family表中的全局OPEN状态仍容易被误读

正文已说明D01–D21只是family catalog且实际使用scoped instance，但表格仍有单一“状态=OPEN”列。项目工具很可能继续把D01整体推进CONFIRMED或将其作为全局阻断。

family catalog不应拥有状态列；它只记录family、scope template和默认fail-closed规则。另建decision instance ledger保存真实状态。若保留表格显示，应把状态改为`CATALOG_ONLY`，并禁止任何requirement引用family row作为批准证据。

同理，最终验收开头的“实际依赖的D-ID已CONFIRMED”应明确为scoped decision instance ID，而不是family D01–D21。

### 18.10 第十二轮结论

第十一轮内容已经进入正文，但具体schema草案暴露了新的状态一致性问题，必须在生成Wave 0 artifact前修正：

- 打破source manifest与ledger的摘要自引用；
- 分离stable requirement identity与content revision；
- 修正OPEN/ASSIGNED/VERIFIED各状态的owner不变量；
- governance declaration拥有ledger，测试只验证；
- 使用Git/review并发控制，不建设ledger Runtime CAS服务；
- VERIFIED由authority声明与机械门禁共同成立；
- 对证据实行分级retention，不永久保存所有CI日志；
- 更新最终验收第18项的旧术语与旧生成模型；
- decision family catalog移除全局状态，真实状态只属于scoped instance。

这些是Wave 0治理schema的必要修正，不是新的生产架构范围。修正后应直接编写schema/ledger/manifest独立需求，而不是继续扩展B1–B37。

## 19. 开工准入复核结论

### 19.1 结论

可以开始，但当前只授权启动Wave 0治理交付物，尚不能直接全面修改生产代码，也尚未满足Wave 1退出门槛。

第十二轮提出的schema一致性问题已被需求正文吸收：manifest自引用已拆除、requirement identity与revision已分离、closure状态不变量已修正、测试不再拥有ledger状态、Git并发不再伪装Runtime CAS、证据已分级、decision family已固定为`CATALOG_ONLY`，最终验收第18项也已更新。总索引级设计评审可以结束。

### 19.2 当前Wave 0 exit criteria实况

| 条件 | 当前状态 | 判断 |
|---|---|---|
| Source baseline与governance evidence manifest | 未生成 | 阻断Wave 1 |
| Versioned closure ledger schema/declaration | 目标路径尚不存在 | 阻断Wave 1 |
| 原子subfinding identity | 尚未分配 | 阻断finding分派 |
| Production-capable recipe catalog及集合owner/算法 | 仅有需求描述，尚无交付artifact | 阻断完整适用集合门禁 |
| B1/B33/B37/B9/B16 consumer/public retirement复核 | 审核中有静态证据，但尚未形成独立requirement与D21 scoped evidence | 阻断对应删除切片 |
| Repository-local requirement ID/write set/唯一writer | 尚未创建 | 阻断生产修改 |
| B26先失败fixture | 需求已列反例，尚未形成Wave 0独立测试交付 | 阻断门禁修复签收 |

仓内检查确认 `zdocs/architecture/post-closure-finding-ledger-v1.json` 当前不存在；现有 `zdocs/architecture/contracts-decisions.toml` 不能自动视为本需求的scoped decision instance ledger，必须先核实其owner、schema和语义，禁止换名复用或建立平行decision truth。

### 19.3 现在允许开始的工作

可以立即创建并实施一个Wave 0独立需求，范围仅包括：

1. 定义development architecture-governance artifact的owner和versioned schema；
2. 生成不自引用的source baseline manifest及governance evidence manifest；
3. 把B1–B37拆成稳定原子evidence identity，初始全部为OPEN；
4. 建立closure ledger declaration与只读validator，不自动分派或验证；
5. 建立production-capable recipe catalog和五类集合各自的owner/发现接口；
6. 为已确认的B26假绿加入先失败fixture；
7. 为首批候选删除分别编写consumer/public-surface审计结果，但不在本Wave删除生产symbol。

该需求不得顺手修改Runtime、Orchestration或Product状态机，不得确认任何产品decision，不得自动把finding推进为ASSIGNED/VERIFIED，也不得创建通用Runtime registry/CAS服务。

### 19.4 下一开工点

Wave 0通过后，首批适合进入Wave 1的是B1、B33、B37、B9和B16的独立删除/局部收敛需求。每项必须先取得stable requirement ID、精确write set、唯一writer，并完成仓内consumer与public API retirement核验；实际属于承诺public surface的symbol还需D21 scoped instance确认。

因此当前状态是：

- 总索引评审：通过，可以收口；
- Wave 0治理工程：可以立即开工；
- Wave 1生产删除：尚未准入；
- Wave 2/3 contract与状态机治理：尚未准入；
- B1–B37整体实施：始终不允许作为单一工程开工。

## 20. 用户确认的最终评审目标

用户进一步明确：目标不是“先启动Wave 0，再在开发过程中逐步补齐准入”，而是在任何生产代码治理开工前，提前解决所有会阻断实施的产品决定、架构选择、迁移策略、owner、依赖和验收问题。

因此，后续评审与最终“可以开工”结论采用以下更严格标准。

### 20.1 不允许遗留到编码阶段的事项

下列问题必须在对应独立实施需求获准编码前具有明确、可引用的scoped decision或authoritative contract，不得由开发者临场选择：

- durable旧格式是原样保留、一次性migration、直接拒绝还是经授权清除；
- retention期限、terminal tombstone幂等窗口、pin/effect/delivery/legal-hold结算条件；
- legal hold、用户删除、安全清除、TTL和测试临时数据各自的删除authority；
- Sandbox有限profile、required/advisory control及activation后、spawn前、运行中失效策略；
- Workflow/effect的provider idempotency/query保证、允许重试条件和IN_DOUBT对账；
- Turn直接输入采用canonical delivery还是独立stable input identity，以及batch/排序/归属规则；
- DLQ是否开放re-admit、与live stream的顺序/并发以及重复effect处置；
- Notebook stdin stale/cancelled/kernel-restarted reply、password可见范围和connection close结算；
- public API retirement authority及breaking removal决定；
- daemon升级、corruption evidence、LSP支持范围、Presentation兼容、Permission applicability、Hook更新和Connection强制退出策略；
- 每个canonical owner、authoritative type/state truth、唯一composition root与唯一writer；
- 每个durable schema的migration/拒绝路径、activation point、forward recovery和旧decoder退出条件；
- 每个独立需求的contract deliverable DAG、共享write set、互斥点与最终验收证据。

调查、证据生成和需求编写可以先行，但任何会固化上述语义的生产改动不得以“后续再确认”为前提启动。

### 20.2 最终判定“可以按顺序开工”的必要条件

只有同时满足以下条件，审核结论才能从“可做治理准备”升级为“生产治理可以按既定顺序开工”：

1. 与首批及其所有下游实施语义相关的scoped decision instance均为`CONFIRMED`，不存在由开发者自行解释的OPEN项；
2. B1–B37已拆成稳定原子evidence identity，每条active finding均有且仅有一个repository-local implementation requirement与canonical owner；
3. 每个implementation requirement均有稳定ID、获准的reviewed revision、精确write set、唯一writer和完整consumer迁移清单；
4. 所有durable变更均已选择migration/保留/拒绝策略，并定义partial failure、forward recovery、旧decoder退出和不可逆边界；
5. 所有retention、purge、cleanup和安全失效路径均有明确Product authority、typed command/receipt与测试标准；
6. 依赖已细化为具体contract deliverable DAG，不以B编号、F审计视图或Wave名称代替真实前置条件；
7. production-capable recipe、public declaration、durable authority、lease/CAS authority和dynamic boundary适用集合已闭合，不依赖单一默认配置或人工漏项清单；
8. 每项关闭条件都有可执行的正向、负向、corruption、restart、CAS/fencing、并发或fault-injection证据计划；
9. 实施顺序已经明确到独立需求级别，开发者不需要在编码期间新增产品决定、选择owner或发明迁移策略；
10. 首个生产实施需求的全部前置证据已存在，而不是只描述为未来Wave 0交付物。

### 20.3 Wave 0的重新定位

Wave 0仍可用于生成source baseline、closure ledger、recipe catalog、适用集合和先失败门禁，但它只是评审/治理准备工作，不等于生产治理已经获准开工。

若用户要求“所有不可开工点都提前解决”，则最终交付不能停在Wave 0 exit。必须继续完成：

- scoped decision的方案评审与用户/Product owner确认；
- 原子finding到独立requirement的完整分派；
- domain migration、retention和failure policy的预先设计；
- 独立需求级依赖排序和冲突控制；
- 首批至后续批次均不再需要实施者临场作架构决定的证据。

### 20.4 后续评审方式

后续不再仅报告“某决定仍OPEN”。对每个未决scope，评审必须给出：

- 推荐选择；
- 被拒绝的备选及理由；
- 对现有数据、兼容性、安全、运维和实施顺序的影响；
- authoritative owner/contract/schema落点；
- 需要用户或Product owner确认的精确决定文本；
- 决定确认后解除哪些implementation requirement阻断。

待所有必要决定形成可确认文本后，集中提交用户确认；确认结果写入scoped decision instance和对应独立需求，不能只保留在对话中。

### 20.5 修正后的当前结论

当前总索引的技术审计已接近完整，但按用户确认的严格目标，尚不能判定生产治理可以开工。现阶段只具备继续完成治理artifact、产品决定方案和独立需求设计的条件。

下一阶段评审必须把D01–D21 family逐domain实例化，给出推荐决定并完成独立需求级排序。只有这些阻断全部提前关闭后，才输出最终“可以按以下顺序开工”的结论。

## 21. 第一批 scoped product decision 推荐：删除与不启用未来能力

### 21.1 现有 `contracts-decisions.toml` 不可直接复用为本次decision truth

`zdocs/architecture/contracts-decisions.toml`服务于既有Contracts package治理，记录module/symbol的retain/move/delete决定，使用`approved/unresolved`等旧状态，并由`contracts_governance.py`按Contracts facts校验。它不拥有moderation、admin activation、Hook更新、DLQ replay、daemon incident policy或public product API retirement语义。

本次scoped decision可以复用其“仓内strict declaration + facts/gate验证”的基础模式，但必须使用独立versioned schema和authoritative ledger。禁止：

- 把D01–D21记录追加到旧Contracts ledger，制造跨bounded-context巨型decision store；
- 将旧`approved`自动映射为本次`CONFIRMED`；
- 因旧facts中出现public symbol就推定其当前仍是稳定public API；
- 建立第二份同scope decision truth。

若未来统一development governance存储，必须先设计能够保持domain-scoped owner、不同schema和独立migration的容器；当前最小方案是本需求独立decision instance ledger。

### 21.2 D05 moderation能力准入：推荐删除，不交付moderation能力

推荐scoped instance：`D05-openai-chat-moderation-v1`。

推荐决定：当前Mote产品不声明、激活或承诺provider moderation能力。`OpenAIChat.amoderation()`及仅为它存在的万能异常decorator属于无consumer危险死代码，直接删除；不新增Port、feature flag、policy状态机或兼容stub。

理由：

- 全仓无生产/测试consumer；
- decorator会记录完整args/kwargs并把异常伪装为`None`；
- 保留方法不能形成真正的authoritative safety gate；
- 未来若出现真实用例，应以新产品能力和新generation contract重新准入，而不是复活旧方法。

确认文本：

> 确认 `D05-openai-chat-moderation-v1`：当前产品不交付moderation能力。删除`OpenAIChat.amoderation`、专用`handle_exception`及相关残渣；不保留兼容入口。未来moderation必须以真实consumer和typed fail-closed gate另立新需求。

解除阻断：B37删除需求。

### 21.3 D17 inference admin surface准入：推荐删除未激活HTTP admin surface

推荐scoped instance：`D17-inference-http-admin-surface-v1`。

推荐决定：当前只保留已接入生产composition的shared gRPC inference surface；删除未由任何Product entrypoint/listener/lifecycle激活的aiohttp inference admin API、read/mutation model、daemon projection、孤立fixture和公开导出。不建设admin CAS、HTTP wire schema或后台server。

理由：

- `build_inference_admin_api()`和`admin_read_model()`均无生产consumer；
- 真实daemon启动链只装配`SharedGrpcServer`；
- 为未确认管理员用例扩建严格Port和durable mutation会制造未来能力；
- 若未来需要admin plane，应重新决定network exposure、authentication authority及与gRPC的边界。

确认文本：

> 确认 `D17-inference-http-admin-surface-v1`：当前产品不交付aiohttp inference admin surface。删除该未激活surface、model/projection/export/fixture及专用依赖；生产只保留现有gRPC daemon入口，不保留compat或未来stub。

解除阻断：B33删除需求。

### 21.4 D15 Hook更新策略：推荐activation后冻结

推荐scoped instance：`D15-runtime-hook-generation-v1`。

推荐决定：Hook config handler与programmatic Python callback只允许在Role/Application activation编译阶段注册；activation原子发布一个immutable Hook generation，之后`register()`拒绝mutation。当前不支持运行期hot swap；Role重建或下一Product application generation才能改变Hook集合。

具体语义：

- Product config和程序callback在activation前统一获得stable identity、validated event enum、预编译matcher和source/authority；
- callback只能单调收窄已有权限，control callback malformed/timeout/crash fail closed；
- activation后manager只执行/query immutable snapshot；
- 测试可以构造新manager/generation，不能在已激活实例上随意追加；
- 不建立callback registry持久化、动态plugin discovery或双generation热切换。

理由：当前没有已确认运行期Hook更新consumer；freeze是最小变化轴，并消除构造后mutation和matcher延迟失败。

确认文本：

> 确认 `D15-runtime-hook-generation-v1`：Hook集合仅在activation前构建并原子发布，activation后冻结；当前不支持hot swap。更新通过下一Product/Role generation完成，不建立动态callback registry或兼容mutation入口。

解除阻断：B12 Hook activation子需求；不自动确认D14 Permission applicability。

### 21.5 D19 DLQ replay策略：推荐当前不开放re-admit

推荐scoped instance：`D19-event-subscription-dlq-replay-v1`。

推荐决定：当前Event subscription支持严格quarantine、typed query和有界retention，但不向Product、operator或自动reconciler开放re-admit/replay command。Checkpoint在quarantine事务后保持单调前进，不回退、不重新插入live stream、不调用旧handler closure。

具体语义：

- DLQ是隔离与调查事实，不是隐藏重试队列；
- external effect未知结果保留IN_DOUBT evidence，禁止自动重试；
- 修复producer/consumer后产生的新event使用新event identity；
- 如未来必须重放历史event，另立产品需求决定人工authority、current decoder/policy、replay attempt和live并发顺序。

理由：当前没有re-admit consumer；直接关闭该变化轴能避免第二delivery状态机和checkpoint回退。B35仍需修复subscriber lease/fence、DLQ identity/query/retention，但无需建设完整replay状态机。

确认文本：

> 确认 `D19-event-subscription-dlq-replay-v1`：当前产品不提供DLQ re-admit或自动replay。Quarantine后checkpoint单调前进；DLQ仅供typed调查/retention，未知effect不重试。未来replay作为新能力重新准入。

解除阻断：B35可按“无replay”缩小实施范围；retention仍依赖D02/D03/D07 scoped instances。

### 21.6 D10 daemon corruption evidence：推荐不建立业务quarantine

推荐scoped instance：`D10-inference-daemon-discovery-corruption-v1`。

推荐决定：daemon discovery corruption不进入长期业务durable quarantine。Strict decoder返回typed corruption并拒绝连接；current supervisor owner在重新核验path、PID incarnation、socket generation和lock ownership后执行generation-safe本地清理。Observability只记录secret-safe metadata/digest和typed cleanup receipt，不永久随机改名堆积原文件。

具体语义：

- current discovery record仍是协调truth；
- corrupt/stale socket与record是本地资源，不获得Session/Workflow/legal-hold语义；
- 无incident consumer，因此不保留完整corrupt payload；
- 清理失败保持typed local cleanup backlog并重试有界次数，不能连接或隐式接管；
- 若未来建立incident取证产品，再以新retention authority准入。

确认文本：

> 确认 `D10-inference-daemon-discovery-corruption-v1`：daemon discovery损坏时fail closed，不建立业务durable quarantine。由current supervisor generation复核后执行typed本地清理，仅保留secret-safe摘要和receipt；未来incident取证能力另立需求。

解除阻断：B30 corruption/stale cleanup子需求；daemon升级仍依赖D06，清理上限依赖D07 scoped instance。

### 21.7 D21 public API retirement：推荐按symbol分类直接退役意外export

推荐创建以下scoped instances，而不是一个全局D21：

| Instance | Symbol/surface | 推荐分类与决定 |
|---|---|---|
| `D21-llm-client-port-v1` | `contracts/ports/model/client.py::LLMClient` | 未从稳定package facade导出、无consumer，internal dead declaration，直接删除 |
| `D21-inference-admin-api-v1` | `product/interfaces/inference_admin_api` exports | 未激活Product surface，属于accidental/unlaunched export，按D17直接删除 |
| `D21-provider-moderation-method-v1` | `OpenAIChat.amoderation` | provider具体实现上的无consumer方法，不是稳定Product contract，按D05直接删除 |
| `D21-i18n-registry-v1` | `product.i18n.register_catalog`、`plurals.register_rule` | 当前仅内置en/zh且无获批plugin/hot-load contract；视为内部mutable construction seam，迁移消费者后直接删除public mutation |
| `D21-fixed-optional-loaders-v1` | Temporal/Squilla固定loader/catalog | 内部composition detail，不是external public provider API；删除伪manifest/loader，改静态typed activation |

`register_catalog`确实通过`product/i18n/__init__.py::__all__`导出，测试也直接调用；因此不能只凭无production consumer静默删除。推荐产品明确声明该导出从未构成稳定外部扩展API，并接受breaking removal。测试应迁移为直接构造immutable locale snapshot，不成为保留public mutation的理由。

统一确认文本：

> 确认上述五个D21 scoped instances：所列symbol/surface均未形成受支持的仓外公共能力，属于dead declaration、未激活surface或内部mutable composition seam。允许在迁移全部仓内consumer、测试和文档后直接breaking删除，不保留alias、re-export、wrapper或兼容registry。未来外部扩展必须经manifest/consumer重新准入。

解除阻断：B1、B33、B37、B9与B16对应删除/局部收敛需求。

### 21.8 本批决定确认后的首批生产实施顺序

若用户确认21.2–21.7的精确决定，并且Wave 0治理artifact已实际生成，则首批生产需求可按以下顺序启动：

1. `R-W1-001` 删除B37 moderation与专用decorator；独立文件面最小，无下游contract依赖。
2. `R-W1-002` 删除B33未激活inference admin surface；先复核gRPC唯一入口，再删除projection/fixture/export。
3. `R-W1-003` 删除B1 dead `LLMClient`；重新确认finalized inference Port覆盖真实consumer。
4. `R-W1-004` 收敛B9 cipher配置；先确认Product strict config consumer，再删除Runtime反射default和单项registry。
5. `R-W1-005` 收敛B16 i18n registry；构造immutable内置locale snapshot，迁移测试后删除publicmutation/import副作用。
6. `R-W1-006` 删除B16 Temporal/Squilla固定伪catalog/loader；按各optional backend owner分别拆成两个writer需求，不与i18n合并。
7. `R-W2-001` 实施B12 Hook activation freeze；依赖D15与B9 strict config deliverable。
8. `R-W3-EVENT-001` 修复B35 subscriber lease/fence与无replay DLQ lifecycle；retention实施必须等待对应D02/D03/D07确认。
9. `R-W3-DAEMON-001` 修复B30 strict discovery与generation-safe local cleanup；protocol upgrade部分继续等待D06。

以上编号是推荐的repository-local requirement identity草案，正式创建时固定identity并分别登记reviewed revision、write set和唯一writer。第1–6项可并行调查，但涉及`product/i18n`的第5项与任何locale相关修改互斥；第7–9项只有各自全部decision前置闭合后才获准编码。

### 21.9 本轮仍需用户确认的决定

本节没有擅自把决定标为CONFIRMED。要解除本批阻断，需要用户确认或逐项修改以下选择：

1. 当前不交付moderation，删除旧方法；
2. 当前不交付HTTP inference admin，删除未激活surface；
3. Hook activation后冻结，不支持hot swap；
4. 当前DLQ不开放re-admit/replay；
5. Daemon corruption不建立长期业务quarantine；
6. 五类列出的public/dead/internal seams允许直接breaking删除且无compat。

确认必须随后写入scoped decision instance和独立需求；仅在对话中同意不能作为最终代码准入证据。

## 22. 第一批 scoped product decision 确认结果

### 22.1 确认来源与范围

用户在当前评审任务中已明确回复“确认”。结合紧邻该回复前对第21.9节六项选择的逐项通俗解释，本次确认按“第21.2–21.7节所列六组决定全部确认”解释，不扩张到D01–D21中的其他decision family，也不自动确认仍待实例化的retention、migration、authority、bounded parameter或protocol upgrade决定。

本节是审核Markdown中的确认记录。后续创建authoritative scoped decision ledger时，必须把下列决定逐项写成独立instance并关联用户确认来源、reviewed requirement revision和受影响的implementation requirement；不得把本节当成长期唯一decision truth。

### 22.2 已确认决定

以下决定状态由`PROPOSED`变更为`CONFIRMED`：

1. `D05-openai-chat-moderation-v1`：当前产品不交付moderation能力；删除`OpenAIChat.amoderation`、专用`handle_exception`及相关残渣，不保留兼容入口。
2. `D17-inference-http-admin-surface-v1`：当前产品不交付aiohttp inference admin surface；删除未激活surface及其model、projection、export、fixture和专用依赖，生产仅保留现有gRPC daemon入口。
3. `D15-runtime-hook-generation-v1`：Hook集合仅在activation前构建并原子发布，activation后冻结；当前不支持hot swap，更新通过下一Product/Role generation完成。
4. `D19-event-subscription-dlq-replay-v1`：当前产品不提供DLQ re-admit或自动replay；quarantine后checkpoint单调前进，DLQ仅提供typed调查与retention，未知effect不得重试。
5. `D10-inference-daemon-discovery-corruption-v1`：daemon discovery损坏时fail closed，不建立业务durable quarantine；current supervisor generation复核后执行typed本地清理，只保留secret-safe摘要与receipt。
6. 以下D21 scoped instances均确认允许breaking removal：
   - `D21-llm-client-port-v1`；
   - `D21-inference-admin-api-v1`；
   - `D21-provider-moderation-method-v1`；
   - `D21-i18n-registry-v1`；
   - `D21-fixed-optional-loaders-v1`。

第6项要求先迁移全部仓内consumer、测试和文档，再删除对应dead declaration、未激活surface或内部mutable composition seam；不得保留alias、re-export、wrapper、兼容registry或第二入口。

### 22.3 已解除与尚未解除的阻断

本次确认解除以下“产品方向未决定”阻断：B1、B33、B37可以形成直接删除型独立需求；B9与B16中已列明的registry/伪loader收敛可以按第21.8节拆分；B12可以按activation freeze设计；B35可以按无replay边界设计；B30可以按无长期业务quarantine边界设计。

但这不等于上述需求已经获得生产代码开工许可：

- 全部需求仍须先取得稳定requirement identity、reviewed revision、精确write set、唯一writer、依赖边和机械验收门禁；
- Wave 0的authoritative decision ledger、closure ledger与source/evidence manifest必须先落地并通过治理门禁；
- B35的retention仍被D02、D03和D07的domain-scoped instance阻断；
- B30的protocol upgrade仍被D06阻断，本次只关闭corruption evidence与cleanup方向；
- B12的Permission applicability仍被D14阻断，本次只确认Hook generation lifecycle；
- B9/B16只有第21.7节明确列出的删除面获得产品方向确认，不能把该确认扩大为整个finding已可实施。

因此，当前结论是：第一批六组产品方向已经确定，评审可以继续实例化下一批阻断决定；尚未达到“所有不可开工点均已提前解决”的最终准入状态。

### 22.4 下一批必须提前关闭的决定

下一轮按依赖优先级评审：

1. D01 durable旧格式处置：逐domain选择直接保留、一次性migration或经授权丢弃；
2. D02 retention/tombstone：逐domain定义保留边界、pin/hold条件与purge前置；
3. D03 delete authority：逐domain确定谁有权发起、批准和执行不可逆删除；
4. D07 bounded governance parameters：明确容量、重试、cleanup、backoff和retention等有界参数的Product owner与schema；
5. 再沿已登记DAG处理D06、D14及其他仍阻断独立需求的scoped instances。

只有每项都形成通俗解释、推荐选择、被拒备选、精确确认文本、owner/schema落点及解除阻断映射后，才提交下一批用户确认。

## 23. 第二批 scoped product decision 提案：先闭合Event/DLQ与daemon cleanup

### 23.1 为什么不能直接确认“全局D01/D02/D03/D07”

D01、D02、D03和D07都是问题目录，不是一个可横跨全仓的答案。OAuth credential、Agent tombstone、Workflow effect、Cron occurrence、Artifact、Event DLQ和daemon本地文件的价值、风险、authority及生命周期不同。统一规定“都保留30天”或“都由maintenance删除”会制造错误抽象，甚至让低价值本地垃圾获得业务legal-hold语义，或让普通cleanup误删安全/副作用证据。

本轮只实例化当前已确认下游中最先需要的两个scope：

- `runtime/events`的SQLite subscription checkpoint与无replay DLQ；
- `product/inference/daemon`中已经确认不进入业务quarantine的本地corrupt/stale资源。

其他durable authority仍保持OPEN，后续逐domain确认。

### 23.2 D01：Event subscription v1旧库推荐做一次性原地migration

推荐instance：`D01-event-subscription-state-v2`。

通俗解释：现有SQLite v1里已经有“消费到哪里”和“哪些事件进了隔离区”的真实数据。直接清空可能让旧事件再次被处理；长期同时支持v1/v2又会留下双读债务。因此推荐升级时只执行一次事务migration，保留已有checkpoint和DLQ事实，但不伪造旧worker仍拥有执行权。

推荐决定：

- v2引入subscription generation、lease/fence、strict DLQ identity/lifecycle与retention字段；
- migration在独占activation/SQLite transaction内执行，先验证v1 schema与每条record；
- checkpoint sequence、subscription/stream identity、event identity、原始envelope和失败时间原样保留；
- 所有迁入记录初始为“无active execution owner、等待新generation claim”，不得把migration进程或旧进程伪装成owner；
- migration receipt记录源版本、目标版本、数据库identity、迁移行数、内容摘要和commit revision；
- 任一中段损坏、identity冲突或无法严格解码时整体回滚并fail closed，不启动subscriber、不清空问题行；
- v2 commit并完成首次可恢复验证后，只保留v2 reader/writer；删除v1 decoder和所有运行期双读。部署不能回滚到v1 writer，恢复只能继续v2 forward recovery。

拒绝的备选：

- 直接清空：会丢失checkpoint并可能重复外部effect，未经授权不可接受；
- 永久拒绝整个旧库：安全但会让已有正常状态无法升级，运维代价不必要；
- 长期v1/v2双读：违反零兼容债务原则，且无法证明两个generation共享ownership语义。

authoritative落点：schema/migration由`runtime/events` subscription-state bounded context拥有；Product composition只选择数据库路径并在subscriber activation前调用唯一migration/activation入口。migration tool不是第二个生产store owner。

确认文本：

> 确认 `D01-event-subscription-state-v2`：现有合法SQLite subscription-state v1在activation前执行一次性、事务化、可审计的v1→v2 migration，保留checkpoint和DLQ canonical facts但不继承active owner。损坏或冲突时整体回滚并fail closed，不清空数据。v2首次验证后删除v1 reader/writer，不保留运行期双读，部署只允许forward recovery。

解除阻断：B35 schema/fence/DLQ lifecycle实施的旧数据处置阻断。它不确认其他SQLite、JSONL或durable store的migration。

### 23.3 D02：无replay DLQ采用“短期完整内容 + 较长期最小tombstone”

推荐instance：`D02-event-subscription-dlq-retention-v1`。

通俗解释：DLQ完整事件主要用于查错，长期保存可能包含大量或敏感内容；但马上全删又会破坏审计、checkpoint解释和幂等。因此推荐完整内容保留30天，之后只保留最小身份/处置事实180天。仍有未知外部effect或legal hold的记录不能按时间自动删除。

推荐决定：

- 从`last_failed_at`起，完整inline envelope/error默认保留30天；超过inline阈值的payload从一开始就使用canonical ArtifactRef，DLQ不复制第二份大payload；
- 完整内容到期且effect、delivery、artifact pin和legal hold均结算后，current fenced owner执行typed compaction，只留下subscription/stream/sequence/event identity、failure/terminal disposition、时间、schema generation和内容digest；
- 最小tombstone默认保留180天，用于解释checkpoint、检测identity reuse和审计；180天到期且所有hold/引用均关闭后才允许purge；
- `IN_DOUBT`、未结算external effect、active legal hold或仍被canonical artifact/session引用的记录不按期限删除，直到产生typed settlement；
- retention instant使用timezone-aware absolute time和版本化clock identity，不能用mtime；
- 30/180天是Product schema中的versioned默认值，不是Runtime散落常量。当前不增加每个plugin任意覆盖期限的扩展面。

拒绝的备选：永久保存完整DLQ会形成无界敏感数据堆积；隔离后立即删除会使调查和checkpoint因果链断裂；仅按数据库mtime/大小清理无法证明单条record已结算。

authoritative落点：retention policy declaration由Product config/composition拥有；eligible状态、pin/hold投影、compact/purge command及receipt由`runtime/events` subscription-state owner执行。Artifact payload沿用canonical Artifact authority，不建立DLQ blob store。

确认文本：

> 确认 `D02-event-subscription-dlq-retention-v1`：无replay DLQ完整内容默认保留30天，结算后compact为最小tombstone；tombstone默认保留180天。IN_DOUBT、未结算effect、active legal hold及仍被canonical reference/pin引用的记录不按时间删除。期限由versioned Product schema选择，Runtime只能由current fenced owner按typed command执行。

解除阻断：B35的DLQ retention与payload compaction方向；仍需D03删除authority和D07操作上限共同确认后才能实施purge。

### 23.4 D03：DLQ删除由Product maintenance授权、Event owner执行

推荐instance：`D03-event-subscription-dlq-delete-authority-v1`。

通俗解释：Product决定“这类数据何时该清”，真正了解checkpoint、fence和effect是否安全的是Event store owner。二者必须分开：maintenance不能直接执行SQL，Runtime也不能自行发明保留政策。

推荐决定：

- 正常TTL compaction/purge由唯一Product application maintenance generation发起typed command；
- `runtime/events` current subscription-state fenced owner重新核验expected revision、retention eligibility、effect/delivery settlement、artifact pin与legal hold后执行；
- legal hold只能由Product批准的governance/incident authority设置或解除，maintenance和extension不能绕过；
- 当前不提供逐条“用户删除DLQ”产品API，也不允许subscriber handler或operator直接SQL删除；
- security clear必须是独立typed command和批准authority，仍保留secret-safe不可替代receipt；测试临时数据库由test fixture owner删除，不复用生产purge command；
- 每次compact/purge产生不可变receipt，包含command identity、authority generation、target identity/revision、fence、删除类别、前后digest和结果，但不复制被删敏感payload。

拒绝的备选：Event Runtime自行定时删除会越过Product政策；Product maintenance直接操作SQLite会形成第二store owner；提供万能`delete(force=True)`会混合TTL、security clear、legal hold和测试语义。

确认文本：

> 确认 `D03-event-subscription-dlq-delete-authority-v1`：正常DLQ TTL清理由唯一Product maintenance generation发起typed command，`runtime/events` current fenced store owner在逐项复核revision、retention、settlement、pin和hold后执行并返回审计receipt。当前不提供用户逐条删除或直接SQL/operator删除；legal hold、security clear和测试清理使用不同authority与typed语义。

解除阻断：B35不可逆compact/purge authority；与D02、D07共同闭合后才允许删除路径编码。

### 23.5 D07：Event治理上限优先固化现有约束，只新增缺失边界

推荐instance：`D07-event-subscription-bounds-v1`。

源码已有且推荐继续作为hard bound：subscription capacity最大65,536；handler retry最大100次；单attempt timeout最大300秒；dead-letter error最大16 KiB；DLQ query单页最大1,000。默认retry仍为3次、单次30秒、初始backoff 0.1秒、最大5秒、jitter 0.2，不因本治理扩大。

新增推荐边界：

- `checkpoint.persist_every` hard max为10,000，recoverable external-effect subscription固定为1，避免内存ack领先durable事实；
- 单条inline DLQ envelope record最大1 MiB；更大payload必须在进入DLQ前使用canonical ArtifactRef，绝不静默截断；
- 单次maintenance最多处理1,000条eligible record、单transaction最多100条；返回continuation cursor/receipt，由下一有界周期继续；cursor只是查询进度，不成为retention truth；
- maintenance失败最多连续重试3次，backoff为1秒、5秒、30秒；之后返回typed degraded/backlog，由下一正常周期重新scan，不能无限紧循环；
- 所有配置只能在Product schema规定的hard range内单调收窄，extension和Runtime不得放大。

这里没有把event journal已有64 MiB record上限直接复用为DLQ inline上限：64 MiB适合canonical journal的极端合法event，却不适合在SQLite隔离表中长期复制。超过1 MiB转ArtifactRef是在不同生命周期下复用canonical大对象机制，不是第二identity。

确认文本：

> 确认 `D07-event-subscription-bounds-v1`：保留现有capacity/retry/timeout/error/page hard bounds和当前retry默认值；新增checkpoint persist_every最大10,000（external-effect subscription固定1）、inline DLQ record最大1 MiB、单maintenance最多1,000条且每transaction最多100条、maintenance连续重试最多3次并采用1/5/30秒backoff。超限payload使用canonical ArtifactRef，配置只能由Product schema在hard range内收窄。

解除阻断：B35 capacity/payload/scan/retry边界。该instance不批准Agent、Workflow、Cron、RunJournal或service-call的上限。

### 23.6 D07：daemon本地cleanup采用小批量、可重扫，不建durable业务backlog

推荐instance：`D07-inference-daemon-local-cleanup-bounds-v1`。

推荐决定：

- discovery文件严格限制为64 KiB，超过即typed corruption且不得解析/连接；
- 每次持有current supervisor lock的prepare/reconcile最多检查并结算128个符合`gateway.json.stale-*`、`gateway-*.sock.stale-*`或已确认新布局的本地候选；
- 每个候选删除前重新核验runtime directory、symlink/path、UID/mode、PID incarnation、socket generation和current discovery，任一不确定即保留并报告；
- 单候选最多尝试3次本地删除，使用0.1/0.5/2秒monotonic backoff；批次耗时上限10秒。超限或失败返回typed partial cleanup receipt，剩余候选由下一次持锁scan重新发现；
- 不把候选清单或原始corrupt payload持久化为业务backlog，不引入legal hold。Observability仅保存secret-safe path category、digest、reason与receipt；
- 不允许cleanup失败阻止一个已经证明使用全新安全path/generation的daemon启动，但失败旧资源绝不能被复用或视为已清理。若无法证明新generation与残留资源隔离，则fail closed不启动。

拒绝永久随机改名堆积、无限目录全扫、无限重试以及无条件“cleanup失败也继续”。这些选择分别造成无界磁盘、启动卡死或旧资源复用风险。

确认文本：

> 确认 `D07-inference-daemon-local-cleanup-bounds-v1`：discovery最大64 KiB；current supervisor每次持锁cleanup最多处理128个候选，单候选最多3次删除尝试（0.1/0.5/2秒backoff），整批最多10秒。失败返回typed partial receipt并由后续持锁scan重发现，不建立业务durable backlog；只有新path/generation与残留资源隔离得到证明时才可继续启动，否则fail closed。

解除阻断：B30 local corruption/stale cleanup的有界实施。daemon protocol upgrade仍依赖D06。

### 23.7 本批需要用户确认的五个选择（通俗版）

1. Event旧SQLite不清空：升级时一次性迁移，坏数据则停住报告，绝不一边兼容旧格式一边运行。
2. DLQ完整内容保留30天，之后保留最小记录到180天；未知副作用或legal hold未结束时不删除。
3. 清理政策由Product maintenance下命令，Event store的当前合法owner执行；不开放人工直接删数据库。
4. Event沿用现有上限，并增加1 MiB inline DLQ、每批1,000/每事务100、最多3次maintenance重试等硬边界。
5. Daemon坏文件每批最多处理128个、每个最多重试3次、整批最多10秒；不长期保存坏文件，但清理失败时只有能证明新generation隔离才允许继续启动。

推荐五项全部确认。确认后，Event/DLQ仍需先生成Wave 0 ledger和独立requirement才能编码；daemon cleanup也仍须独立requirement准入。若对30/180天、1 MiB、1,000/100、128或10秒有不同运维目标，应在确认时直接修改具体数值，不能留给实施者临场选择。

## 24. 第二批 scoped product decision 确认结果

### 24.1 确认来源与解释边界

用户在紧邻第23.7节五项通俗选择后明确回复“确认”。本次按“五项全部确认，且接受第23节列出的具体数值、authority、migration与failure语义”记录。确认范围严格限定为Event subscription/DLQ和inference daemon本地cleanup，不确认D01/D02/D03/D07 family下的其他domain实例。

这些记录是Wave 0 authoritative decision-instance ledger的审核输入。ledger落地时必须逐项保存确认来源、decision revision、affected requirement、authoritative schema/contract和验证artifact；不得由本节或family catalog自动推导其他scope为CONFIRMED。

### 24.2 已确认的五个scoped instances

1. `D01-event-subscription-state-v2`：`CONFIRMED`。合法SQLite subscription-state v1在activation前执行一次性、事务化、可审计的v1→v2 migration；保留checkpoint和DLQ canonical facts，不继承active owner。损坏或冲突时整体回滚并fail closed。v2首次验证后退出v1 reader/writer，只允许forward recovery。
2. `D02-event-subscription-dlq-retention-v1`：`CONFIRMED`。完整DLQ内容默认保留30天，结算后compact为最小tombstone；tombstone默认保留180天。IN_DOUBT、未结算effect、active legal hold及仍被canonical reference/pin引用的记录不按期限删除。
3. `D03-event-subscription-dlq-delete-authority-v1`：`CONFIRMED`。正常TTL清理由唯一Product maintenance generation发起typed command，`runtime/events` current fenced owner在重新核验revision、retention、settlement、pin和hold后执行并返回receipt；不开放用户逐条删除、直接SQL或operator旁路删除。
4. `D07-event-subscription-bounds-v1`：`CONFIRMED`。保留现有capacity/retry/timeout/error/page hard bounds及retry默认值；新增`checkpoint.persist_every <= 10,000`、external-effect subscription固定为1、inline DLQ record最大1 MiB、单maintenance最多1,000条、单transaction最多100条、连续maintenance retry最多3次且backoff为1/5/30秒。超限payload使用canonical ArtifactRef。
5. `D07-inference-daemon-local-cleanup-bounds-v1`：`CONFIRMED`。discovery最大64 KiB；每次current supervisor持锁cleanup最多128个候选；单候选最多3次删除尝试并使用0.1/0.5/2秒backoff；整批最多10秒。失败返回typed partial receipt并由后续持锁scan重发现，不建立业务durable backlog。只有新path/generation与残留资源隔离得到证明时才可继续启动，否则fail closed。

### 24.3 解除的阻断与当前准入状态

对`R-W3-EVENT-001`而言，D01 migration、D02 retention、D03 delete authority、D07 bounds和先前确认的D19 no-replay方向现已齐备。它不再需要实施者决定旧库是否清空、DLQ保留多久、谁能删除、payload/scan/retry上限或是否支持replay。

对`R-W3-DAEMON-001`的local corruption/stale cleanup子范围而言，D10 evidence policy与D07 cleanup bounds现已齐备。它不再需要实施者决定是否长期保存坏文件、如何有界清理或清理失败后何时允许继续启动。

但两项仍未获得立即修改生产代码的许可：

- 必须先生成Wave 0 decision ledger、closure ledger、source/evidence manifest并通过相应机械门禁；
- 必须分别建立稳定独立requirement ID、精确write set、唯一writer、owner/复用证据、contract/schema revision和fault-injection验收；
- Event requirement仍需证明external-effect checkpoint settlement与ArtifactRef/pin projection的canonical owner，不得因产品决定已确认而新建平行effect或blob状态机；
- daemon requirement当前只可覆盖strict discovery和local cleanup；protocol version升级仍被`D06-inference-daemon-upgrade-*`阻断，不能夹带永久双读清理或滚动升级设计。

结论：第二批五项产品阻断已关闭；Event/DLQ与daemon local cleanup已达到“可以完成独立需求设计且无需临场补产品决定”的状态。全项目仍未达到全面开工状态，下一轮继续处理D06 daemon upgrade、D14 Permission applicability及其直接依赖的独立需求。

## 25. 第三批 scoped product decision 提案：daemon升级与Permission适用范围

### 25.1 D06：Shared daemon推荐单generation停旧启新，不做滚动双版本

推荐instance：`D06-inference-daemon-single-generation-upgrade-v1`。

通俗解释：Mote当前是同一用户、同一主机上的共享daemon，不是必须零停机的多节点服务。为几秒级本地重连长期保留“当前协议和前一协议都能跑”，会让每次协议演进都背负双decoder、双行为和交叉测试。推荐升级时先停止旧daemon，再启动新generation；客户端短暂等待并重连，不让两个协议generation同时拥有执行权。

推荐决定：

- 每个Product release只声明一个current Shared RPC protocol generation；discovery、handshake、session credential、request envelope和server只接受该generation，不再隐式接受`current - 1`；
- 升级由current supervisor lock owner发起，先把旧generation原子标记为DRAINING并拒绝新start/open-session，只允许已有操作查询、结算和有界drain；
- 默认graceful drain上限30秒。期限内完成则提交terminal drain receipt、停止listener/process并清理旧socket/discovery；
- 30秒后仍未结算的operation必须先写入canonical recoverable/IN_DOUBT事实并失去旧generation提交权，再向旧进程发送terminate；terminate等待上限10秒，仍未退出才kill。不得先杀进程再补写“可能执行过”的事实；
- 必须确认旧PID incarnation已终止、旧socket不再接受连接、旧generation fence已撤销后，才能发布新discovery并接受新请求；不存在双active generation窗口；
- 客户端遇到DRAINING、协议不匹配或连接关闭时返回typed reconnecting/unavailable并进行有界重连；未被daemon durable accept的调用可用同一stable request identity重试，已accept或结果未知的调用只能query/reconcile，不能盲目重新start；
- durable execution/state schema migration与RPC protocol切换是两个决定。若底层store也变更格式，必须先取得对应D01 scoped instance；D06不能授权静默清库或运行期双读；
- 当前不建设blue/green、双socket流量切换、跨版本session迁移或长期capability negotiation。未来出现明确零停机SLO时，以新产品需求和有期限退出窗口重新准入。

拒绝的备选：

- 永久接受`current/current-1`：没有退出条件，形成持续兼容债务；
- 两个daemon generation并行：需要额外placement、capacity、effect与state ownership状态机，当前没有产品用例；
- 直接kill旧进程：可能丢失accepted/effect结算并触发重复外部动作；
- 无限等待drain：会让产品升级和shutdown永久阻塞。

authoritative落点：升级policy、30/10秒边界和release protocol generation由Product inference composition/schema拥有；DRAINING、execution settlement和reconciliation由现有daemon application/backend canonical owners闭合；supervisor只管理本地process/socket/discovery lifecycle，不能成为execution truth owner。

确认文本：

> 确认 `D06-inference-daemon-single-generation-upgrade-v1`：Shared daemon升级采用单generation停旧启新，Product release只支持一个current RPC protocol，不再永久接受`current-1`。旧generation先拒绝新工作并最多drain 30秒；超时operation先持久结算为recoverable/IN_DOUBT并撤销旧fence，再terminate等待10秒，必要时kill。确认旧PID/socket/fence退出后才发布新generation；客户端只对未durable-accept的stable request有界重试，未知结果必须query/reconcile。当前不建设滚动双generation或跨版本session迁移。

解除阻断：B30 protocol negotiation、旧decoder退出、drain/kill和generation切换方向。若daemon durable store schema变化，仍需该store自己的D01/D02/D03/D07实例。

### 25.2 D14：所有已发布Tool都经过授权链，但“经过授权”不等于“每次询问用户”

推荐instance：`D14-published-tool-permission-applicability-v1`。

通俗解释：是否弹出审批框是产品策略；是否经过安全检查是架构保证。读文件可能泄露secret，写文件会修改状态，命令/MCP/网络可能产生外部动作，因此不能靠toolset上的布尔值决定是否完全跳过授权链。推荐任何发布给模型或外部调用者的Tool都进入同一个ToolExecutor授权chokepoint，再由typed policy决定自动允许、询问或拒绝。

推荐决定：

- 每个进入immutable published tool binding snapshot的调用一律运行：identity/argument validation → control Hook（如配置）→重新classification/permission targets → core permission decision → sandbox/effect permit → durable intent（适用时）→execute；
- “没有用户自定义permission rules”不等于跳过gate。Product必须始终装配一个明确的versioned baseline policy；它可以对普通低风险读取自动ALLOW，但仍产生typed decision/trace并执行path、secret、sandbox和capability检查；
- LOCAL mutation、subprocess、network、IPC、human-visible action、spawn/delegation、MCP/dynamic provider以及secret/credential访问必须是`REQUIRED`，不能由mode、extension或tool metadata扩大为NOT_APPLICABLE；具体是否ASK可由Product baseline和用户批准规则决定；
- 普通workspace read/search可以由baseline自动ALLOW，不要求每次弹窗，但必须经过target normalization、symlink/path boundary、sensitive-source policy和审计identity；
- `NOT_APPLICABLE`只允许Product/Runtime内部、非published、非模型可选、无IO/secret/capability、无state mutation且无外部可观察effect的纯计算步骤。它们不应伪装成Tool binding，也不进入用户permission rule namespace；
- 固定内部argv、maintenance、migration和reconciliation不是“隐藏Tool bypass”。它们分别使用批准的窄typed command/runner authority和独立audit contract，不能借NOT_APPLICABLE调用任意工具或用户命令；
- 显式break-glass/bypass若产品保留，只能跳过可选用户ASK，不能跳过bypass-immune deny、argument validation、control Hook收窄、sandbox required controls、effect intent或receipt/audit；必须绑定批准authority、scope、expiry和receipt；
- 删除`permissions=None -> no approval layer`、`require_permission`、`requires_permission_gate`等决定信任边界的布尔语义。缺失baseline policy、unknown applicability、空/无效permission facts、permission timeout/crash/malformed均fail closed；
- Hook只能单调收窄。Hook返回ALLOW不能覆盖core permission/sandbox；Hook修改参数后必须重新计算classification、targets和approval。此项与已确认D15 activation freeze共同构成B12的完整产品方向。

拒绝的备选：

- 所有Tool每次都ASK：安全但严重破坏可用性，也把“经过检查”错误等同于“人工批准”；
- 只gate危险toolset：分类错误、动态provider或metadata遗漏会直接形成旁路；
- `permissions=None`即完全不检查：默认配置反而成为最大权限模式；
- 用`ToolEffect.PURE`自动跳过全部permission：PURE只表达重放副作用，不证明读取范围、secret暴露或调用authority安全。

authoritative落点：applicability enum/baseline decision contract属于Contracts authorization/tool policy；Product拥有baseline policy选择和批准规则composition；Runtime ToolExecutor是唯一执行chokepoint并实现decision Port；各Tool只声明canonical targets/effect/classification facts，不能自行决定是否跳过gate。

确认文本：

> 确认 `D14-published-tool-permission-applicability-v1`：所有进入published binding snapshot的Tool调用都必须经过唯一ToolExecutor授权、sandbox和effect链；未配置用户规则时仍使用versioned Product baseline policy。普通workspace read/search可自动ALLOW但不跳过target/path/secret检查；mutation、process、network、IPC、human、spawn、MCP/dynamic provider及secret访问为REQUIRED。NOT_APPLICABLE仅限非published、无IO/secret/capability/state/effect的内部纯计算。删除以`None`或`require_permission`类bool决定旁路的语义；缺失/unknown/malformed/timeout均fail closed，Hook只能单调收窄。

解除阻断：B12 Permission applicability、默认bypass和Hook/core permission组合方向；B32实际Sandbox enforcement profile仍依赖D04，不因本决定自动确认具体OS控制保证。

### 25.3 对开工顺序的影响

若本批两项确认：

1. `R-W3-DAEMON-001`可完整覆盖strict discovery、local cleanup和单generation protocol cutover，不再等待D06；若触及daemon durable store格式，另拆migration需求，不夹带在protocol切换中。
2. `R-W2-001`可完整设计Hook activation freeze与Permission applicability compiler，但只能先闭合ToolExecutor授权链；涉及OS sandbox实际保证的部分必须等待D04 scoped profiles。
3. B12与B32必须保留contract依赖方向：先形成typed applicability/baseline decision，再由Sandbox profile声明各effect class需要哪些实际enforcement；不能让Sandbox探测结果反向决定某Tool是否需要permission。

### 25.4 本批需要确认的两个选择（通俗版）

1. Daemon升级时不同时跑新旧两个版本：旧daemon最多等30秒收尾，仍未结束的工作先记录为可恢复/结果未知，再终止；确认旧进程彻底退出后启动新版本。
2. 所有对模型开放的工具都走安全授权链，但低风险读操作可以自动放行，不会每次弹窗；只有完全内部、无IO无副作用的纯计算才可以标记“不适用权限”。

推荐两项全部确认。这两项是在消除永久兼容路径和默认权限旁路，不会新增用户可见能力；其中daemon drain/terminate时间和“所有published Tool统一过gate”属于产品语义，仍应由用户确认后才能固化。

## 26. 第四批 scoped product decision 提案：Sandbox profiles与fixed argv

### 26.1 状态说明

用户回复“继续”只授权继续评审，不等于确认第25.4节。因此`D06-inference-daemon-single-generation-upgrade-v1`和`D14-published-tool-permission-applicability-v1`仍为`PROPOSED`。本节不把它们或下列新提案写成CONFIRMED。

### 26.2 D04：不再暴露任意Sandbox bool矩阵，收敛为三个Product profile

推荐instance：`D04-tool-process-sandbox-profiles-v1`。

当前`SandboxRuntimeConfig`允许独立组合`enabled/backend/fail_if_unavailable/harden_process/seccomp/network/network_enforcement/memory/pids/cpu`等开关，并且大量缺失能力只warning后降级。这无法回答一次effect究竟获得了什么保证。例如配置写着`network="proxy"`并不证明直接socket被阻断，`seccomp=True`也可能没有生成filter，`memory_max="4G"`也可能因cgroup不可用而无效。

推荐只保留三个有限、versioned Product profile：

#### Profile A：`trusted-host-fixed-v1`

适用：Product批准的固定内部程序，例如严格绑定的credential helper、git只读采集、Hook command handler和media backend helper；不能用于模型/用户提供的shell字符串。

Required controls：

- activation时解析并批准absolute executable，绑定regular executable file的device/inode或等价不可替换identity；spawn前重新验证；
- structured argv，无shell、无glob/expansion、无用户控制executable；
- 每个consumer声明固定cwd policy、最小环境、stdin允许性、timeout与output byte bound；
- process group有界终止，receipt区分spawn failure、exit、signal、timeout、output limit和decode failure；
- secret stdout只进入调用方的secret resolver，永不进入普通日志/telemetry。

Advisory controls：OS namespace、seccomp和cgroup可以记录但不是该profile承诺。该profile的安全性来自固定程序/argv/环境与consumer authority，不能在receipt中声称filesystem或network isolation。

#### Profile B：`isolated-workspace-offline-v1`

适用：被批准但不需要网络的用户/模型命令或interactive process。

Required controls：

- actual spawn必须具备OS级filesystem/process namespace，workspace writable roots以外不可写；仅逻辑path precheck不够；
- network必须被net namespace或等价kernel enforcement关闭；proxy env、空allowlist或“工具通常遵守代理”不算；
- generation、permission decision、canonical argv/command digest、cwd/root policy与spawn permit绑定；
- required backend在每次spawn前即时验证，不可证明即`SANDBOX_UNAVAILABLE`且根本不spawn；
- process hardening与secret/environment stripping required。

Advisory controls：seccomp危险syscall附加过滤、cgroup memory/pid/cpu限制。缺失必须进入receipt，但不得把其缺失描述为offline/filesystem隔离仍不成立；真正required的namespace/network control缺失则fail closed。

#### Profile C：`isolated-workspace-allowlist-v1`

适用：需要访问Product批准域名的用户/模型命令。

Required controls：Profile B的filesystem/process保证，加上netns唯一出口、default-deny nft/等价规则、Product allowlist proxy和SSRF/private-address拒绝。直接socket不能绕过allowlist。

若使用brokered credential，proxy、secret resolver、MITM CA/trust bundle及目标domain绑定全部required；任一初始化或spawn-time health失败则该credentialed effect不spawn，不能退化为把secret放进进程env或无凭据直连。

Advisory controls：与Profile B相同的额外seccomp/cgroup项。对资源耗尽敏感的特定Tool若将cgroup升级为required，应另立新profile generation，不能运行时改变本profile含义。

共同规则：

- Product根据真实Tool/effect class选择profile；用户和extension只能在批准集合内单调收窄，例如从allowlist降为offline，不能选择`none`扩大权限；
- `backend=auto`只可用于探测候选实现，不能改变required guarantee；探测到NullBackend时B/C必须拒绝spawn；
- activation receipt只证明generation plan已准备，operation receipt必须证明spawn瞬间actual posture；启动后资源失效不能继续使用旧receipt；
- 运行中required enforcement丢失时，owner终止/隔离process并由ToolExecutor按effect事实结算为typed failure或IN_DOUBT；不得伪造普通exit/success；
- 删除`enabled/fail_if_unavailable/network_enforcement/seccomp`等任意组合决定安全承诺的公共Product schema。底层backend可以保留实现参数，但只能由profile compiler生成，不能成为第二政策入口。

拒绝的备选：把所有控制都设为required会让很多宿主无法运行且混淆真正安全边界；把所有缺失都允许warning降级会让profile名失去意义；继续开放bool矩阵无法机械证明配置组合的真实保证。

authoritative落点：profile identity和required/advisory contract应位于Contracts security/sandbox policy；Product拥有effect→profile选择及有限schema；Runtime sandbox实现per-spawn permit和actual posture receipt；ToolExecutor消费receipt并拥有effect terminal settlement。

确认文本：

> 确认 `D04-tool-process-sandbox-profiles-v1`：Product只提供`trusted-host-fixed-v1`、`isolated-workspace-offline-v1`和`isolated-workspace-allowlist-v1`三个versioned profile。固定内部argv profile不承诺OS隔离；offline profile必须实际证明workspace OS隔离和hard network-off；allowlist profile还必须证明netns唯一出口、default-deny与代理/SSRF控制，credential brokering相关链路全部required。required不可证明则不spawn，advisory缺失进入receipt。删除由任意bool组合和silent degradation决定安全保证的公共配置面。

解除阻断：B32 profile集合、required/advisory语义、per-spawn fail-closed与credential enforcement方向。具体backend实现可更换，但不得降低profile保证。

### 26.3 D13：固定内部程序统一采用async verified argv，不保留同步桥接

推荐instance：`D13-fixed-internal-argv-execution-v1`。

当前生产consumer已经可以沿async路径调用`run_fixed_argv`/`run_verified_fixed_argv`：credential source、Hook command、VCS collector和media video。没有证据需要为它们保留同步process入口；因此无需`asyncio.run()`、私有event loop、线程桥接或同步/异步双API。

推荐决定：

- canonical fixed-program runner只保留async typed API；所有production consumer在其既有async lifecycle内await。同步CLI只允许在最外层Product entrypoint调用一次`asyncio.run(application_command())`，不能在Runtime/adapter内部嵌套loop；
- Product activation为每个固定程序解析absolute path、regular executable identity、来源与consumer，生成immutable`FixedExecutableBinding`；每次spawn通过fd/等价机制复核device/inode，PATH只用于activation解析，不在effect执行时重新选择程序；
- argv中可以包含经过该consumer严格decoder验证的数据参数，但不能改变executable、插入shell语法或扩张该runner authority。若参数本身是用户命令，它必须走D14/D04用户命令链而不是fixed runner；
- fixed runner默认环境为空白基线，只注入`PATH`以外的明确字段；采用`LANG=C.UTF-8`、`LC_ALL=C.UTF-8`和consumer所需的最小变量。禁止默认复制`os.environ`；
- credential helper：只允许USER/MANAGED批准来源、absolute verified executable、无shell；不得注入HOME、云凭据、代理或完整PATH，stdout按secret byte bound读取且永不记录；
- Hook command：配置必须在activation前解析为absolute approved argv并绑定source identity；允许明确cwd和版本化Hook wire stdin，只注入locale及明确业务变量，不继承secret/env；
- VCS collector：使用verified git executable和仓库根cwd；通过显式git flags关闭external diff、pager、interactive prompt、hooks/config command execution等非必要扩展，仅注入locale与禁止prompt变量。若真实用例需要用户git config，逐项白名单，不默认继承HOME/XDG；
- media helper：使用Product批准的verified ffmpeg/ffprobe等binding、固定operation模板、显式输入输出path和timeout/output bound；用户可控filter/codec参数必须经typed allowlist，不能成为命令注入面；
- 每个consumer拥有自己的最小argv/env policy，不建立万能`InternalProcessService`或带`trust_mode/shell`开关的统一runner。共享的是Runtime的spawn/receipt原语，不是共享高层authority。

拒绝的备选：保留同步runner会引入loop嵌套和第二执行入口；运行时按PATH重新解析会产生TOCTOU；默认继承环境会泄漏secret并让用户config改变固定程序行为；把所有consumer塞进一个万能runner会混合credential、VCS、Hook和media信任边界。

authoritative落点：Runtime继续拥有最小fixed spawn/typed receipt；各Product/domain consumer拥有自身program admission、argv/env policy和lifecycle；`FixedExecutableBinding`若跨层使用，应由消费方所需的最小Contracts contract表达，而不是导出Runtime具体runner。

确认文本：

> 确认 `D13-fixed-internal-argv-execution-v1`：固定内部程序只使用async verified argv入口；Runtime/adapter不保留同步桥接或第二runner。Product activation解析并绑定absolute executable identity，spawn前复核，运行时不按PATH重选。环境默认最小化，credential helper、Hook、VCS和media分别拥有严格argv/env policy；用户命令不得伪装成fixed argv。同步CLI只在最外层entrypoint运行一次application coroutine。

解除阻断：B17 fixed argv调用策略、同步桥接、环境最小化和唯一runner方向；D04决定这些consumer使用`trusted-host-fixed-v1`时不虚假承诺OS隔离。

### 26.4 本批建议与待确认汇总

本批推荐D04和D13全部确认。通俗地说：

1. 固定可信内部程序可以在宿主运行，但必须锁定“到底运行哪个文件、哪些参数和哪些环境”；不能拿它运行用户命令。
2. 用户/模型命令若声明“离线隔离”或“只访问允许域名”，实际系统做不到时就不执行，不能警告一下后裸跑。
3. 固定程序全部走async单入口，不在Runtime内部临时开event loop。

为了减少确认轮次，下一次用户若回复“确认本批”，只确认本节D04/D13；第25节D06/D14仍需明确一并确认。用户也可以回复“确认第25和26节”，一次确认四项。

## 27. 第五批 scoped product decision 提案：Connection close与Notebook stdin

### 27.1 状态说明

本轮“继续”仍只表示继续评审。第25节D06/D14和第26节D04/D13保持`PROPOSED`，本节D16/D20也仅为提案。

### 27.2 D16：Connection cleanup失败不杀共享Agent，但也不能伪装关闭

推荐instance：`D16-product-connection-close-settlement-v1`。

通俗解释：AG-UI/ACP连接关闭时，presentation、telemetry订阅和human input binding都要清理。当前代码即使reset失败也会丢掉token并标记未绑定，下一连接可能在旧binding还活着时重新绑定。另一方面，单个浏览器/SSE连接没有权力因为自己的清理失败就杀掉共享Agent或整个服务。

推荐决定：

- 每个`ConnectionScope`具有stable connection identity、session/Agent identity和单调generation，生命周期为`NEW -> ACTIVE -> DRAINING -> CLOSED`，另有typed `CLEANUP_FAILED` settlement但状态仍保持DRAINING；
- close开始时在owner gate下原子进入DRAINING，立即拒绝新turn、human reply、steer和rebind；transport可以停止接收新输入，但不能把socket断开等同于scope已CLOSED；
- cleanup按独立阶段记录：pending control command drain/cancel、telemetry unsubscribe、human binding reset、projector/consumer close、port/transport close。成功阶段写入process-local immutable settlement，重试只执行未完成阶段；
- human binding token、telemetry handles、owner/generation和失败详情在对应阶段成功前必须保留。reset失败不能清空token或`_env_bound`，stale connection generation不能reset新连接的binding；
- 单次connection close总预算10秒。到期返回typed `DRAINING_TIMEOUT`及未结算阶段，scope保持DRAINING并由session-hosting owner持pin、后台有界续清理；不得无限阻塞断网请求handler；
- 每个失败阶段最多由owner连续重试3次，使用0.1/0.5/2秒monotonic backoff；之后等待下一reconcile/应用shutdown，不紧循环；
- AG-UI/ACP/其他多连接server：单connection cleanup失败只隔离该connection generation，禁止重绑同一human capability，不能终止共享Agent、ResidentSession或server process；其他无共享binding冲突的session可以继续；
- 单进程CLI/application shutdown：全局owner最多等待所有DRAINING connection 30秒。仍无法settle时输出secret-safe leak report并以typed incomplete/non-zero shutdown退出；进程退出可回收process-local资源，但不得在退出前伪造CLOSED或清除canonical durable session/Agent事实；
- `ConnectionScope`自身不得kill共享process。只可强制终止由该connection独占、且contract明确可丢弃的presentation child task/process；任何authoritative Agent turn/control/effect必须由其owner返回settlement；
- presentation-only consumer close失败可标记DEGRADED并继续其他阶段，但human binding、control command和telemetry ownership阶段必须settle或保持DRAINING。

拒绝的备选：失败后直接清token会造成stale binding/ABA；无限等待会耗尽server request；单连接失败就杀共享Agent会越权；吞错并报告CLOSED会让下一连接建立第二human/control入口。

authoritative落点：Connection state/阶段settlement由`product/session_hosting` owner拥有；Telemetry、human binding、Agent control各自只暴露最小typed close/reset/cancel receipt，不共享cleanup manager；Product application owner决定全局30秒shutdown政策。

确认文本：

> 确认 `D16-product-connection-close-settlement-v1`：Connection close采用owner-local分阶段状态机，单次预算10秒；失败或超时保留human token、telemetry handle、generation与未结算阶段并保持DRAINING，最多按0.1/0.5/2秒连续重试3次。多连接surface不得因单connection失败终止共享Agent/session/process；应用全局shutdown最多等待30秒，仍失败则生成secret-safe leak report并以incomplete/non-zero结果退出，不伪造CLOSED或删除durable事实。ConnectionScope无权kill共享process。

解除阻断：B15 Connection lifecycle的token保留、重试、超时和强制退出策略。Tool/MCP cleanup与Agent control command仍分别属于其他独立需求，不能合并进Connection状态机。

### 27.3 D20：Notebook stdin reply必须绑定kernel incarnation和execution

推荐instance：`D20-notebook-stdin-incarnation-v1`。

当前`NotebookInputReply`只携带`request_id + value`，driver也只比较pending request id。request id来自Jupyter message header，缺失时甚至本地随机生成；它没有绑定Runtime epoch、kernel restart generation、execution message、handoff或surface handle。旧浏览器页面在kernel restart或新cell恰好复用/混淆request时可能把输入送进错误执行。password value还可能因普通surface/state投影被扩大可见范围。

推荐决定：

- canonical pending identity至少绑定`RuntimeRef + Runtime epoch + kernel incarnation + execution msg_id + cell_id + stdin request_id + handoff generation + surface handle generation`；reply wire携带一个opaque signed/unguessable reply capability或上述identity的canonical token，不能只传裸request_id；
- kernel stdin message必须严格匹配当前execution parent msg_id；缺失kernel request id不得用随机ID伪造协议事实，应返回typed malformed request并interrupt/settle当前execution；
- 同一kernel execution最多一个active stdin request。新request到达时旧request必须先有typed terminal settlement，禁止覆盖pending slot；
- reply owner在同一generation gate下原子claim pending request并核验Runtime lease/fence、kernel incarnation、execution仍blocked、handoff与surface仍active。只有claim成功者可调用kernel client input；重复相同reply返回幂等receipt，不同value返回typed identity conflict；
- kernel restart、execution idle/terminal、cell cancel、handoff detach、surface close或deadline到期会把pending request推进到`KERNEL_RESTARTED / EXECUTION_FINISHED / CANCELLED / HANDOFF_GONE / EXPIRED`之一并撤销reply capability；旧reply永远不能提交到新kernel；
- stdin等待不另开无限deadline，沿用当前cell execution绝对deadline，最长不超过现有600秒execute hard bound。到期先撤销pending identity，再interrupt kernel并等待既有5秒grace；
- non-password value是用户输入canonical fact：在kernel提交前记录durable typed intent，成功后记录receipt；恢复时不得自动重放到新kernel，只用于解释历史settlement；
- password value只在surface adapter到current kernel input调用的受控内存路径短暂存在，不进入NotebookDocument、SurfaceFrame、ViewEvent、Session message、日志、trace、exception、checkpoint或普通durable intent。Canonical事实只保存`password=true`、request/reply identity、value keyed digest/length class和terminal receipt，不保存plaintext；
- frontend对stale/cancelled/restarted/expired reply显示不含value的明确结果并清除输入框。password输入框禁止回显、autocomplete和普通clipboard/history投影；失败不能把value附进错误文本；
- close settlement与D16协调：Connection进入DRAINING先撤销该surface的stdin reply capability并返回typed terminal outcome，再清human binding；不能丢弃pending request后让kernel永久等待。

拒绝的备选：只增强随机request id仍无法证明kernel/execution owner；把所有input value都不持久化会丢失普通用户输入事实；把password明文持久化会扩大secret泄漏面；restart后自动重放reply可能把secret或操作输入送进不同代码上下文。

authoritative落点：Notebook stdin identity/state属于`runtime/interactive/kernel` driver与ManagedRuntime incarnation lifecycle；surface wire只传opaque reply capability和typed outcome；Product presentation负责password UI，不拥有pending truth；durable non-secret input intent复用Runtime operation/journal contract，不新建Notebook事件store。

确认文本：

> 确认 `D20-notebook-stdin-incarnation-v1`：Notebook stdin pending/reply绑定Runtime epoch、kernel incarnation、execution、cell、request、handoff和surface generation；缺失kernel request identity fail closed，不生成随机协议身份。每个execution仅一个pending request，reply原子claim且重复同值幂等、不同值冲突。restart/terminal/cancel/detach/close/expiry撤销旧reply capability，最长等待不超过600秒execution bound。普通输入记录durable intent/receipt但不自动重放；password plaintext只走受控内存直达kernel，durable事实仅保存identity、digest/length class和receipt，任何投影/日志/checkpoint不得保存或回显value。

解除阻断：B20 stdin incarnation、stale reply、password范围、cancel/restart/close settlement产品语义。Notebook document/schema的其他strict codec工作仍需独立requirement及适用的D01 migration决定。

### 27.4 本批建议与集中确认方式

推荐D16和D20全部确认。通俗地说：连接清理失败就保留“还没清完”的证据并隔离该连接，不杀共享Agent；Notebook输入只能回复当前kernel当前cell，重启后的旧回复无效，密码不落普通历史。

当前累计六项待确认：第25节D06/D14、第26节D04/D13、第27节D16/D20。若全部接受，可一次回复“确认第25至27节”；若只想继续评审，回复“继续”即可，所有提案继续保持未确认。

## 28. 第六批 scoped product decision 提案：Turn输入identity与Cron存储信任

### 28.1 状态说明

用户明确要求“继续下一轮评审”，因此第25–27节六项仍为`PROPOSED`。本节D18/D08同样不自动确认。

### 28.2 D18：普通Message先成为canonical delivery，不建立第二种Turn input identity

推荐instance：`D18-agent-turn-input-via-delivery-v1`。

当前`EventDrivenScheduler.notify()`可以把普通`Message`直接放入Mailbox，未携带delivery identity；durable turn queue却要求`delivery_ids`为非空。到了turn boundary，直接消息会形成空delivery集合并在构造`TurnQueueIdentity`时失败。若为直接消息另造`DirectInputId`，Turn scheduler就必须同时理解delivery和direct-input两套accept/recovery/ack状态机。

推荐决定：

- 所有会驱动Agent业务turn的外部输入、用户Message、Agent间Message、Cron trigger和Product surface input，都必须先通过Agent delivery owner形成stable durable delivery intent；不存在“先放process mailbox、稍后再补identity”的合法入口；
- delivery identity绑定source kind/source identity、target logical Agent、target lifecycle generation、canonical message payload digest、delivery mode、request/batch identity和accepted revision。相同identity且所有facts一致才幂等；任一事实不同返回typed conflict；
- `EventDrivenScheduler.notify(Message)`这类裸入口从生产公共面删除。内部仅允许`wake(agent_id, reason)`唤醒已经存在durable pending/accepted fact的reconciler；wake不携带业务payload、不能单独创建turn或返回durable accepted；
- Mailbox只是current incarnation的有界projection。enqueue必须携带delivery identity；residency snapshot可保存投影以加速恢复，但delivery store/turn queue才是canonical acceptance truth；mailbox丢失后可由durable scan重投影；
- turn boundary在owner transaction/generation下选择一个稳定有序delivery batch。batch顺序使用durable delivery acceptance sequence，不用Mailbox当前内存顺序、wall clock或哈希排序；
- 一个delivery最多归属一个accepted TurnRequest。选择batch时先提交`STAGED_FOR_TURN(request_id, batch_ordinal)`或等价canonical ownership，再提交bounded turn acceptance；crash后只能完成或回滚同一stage，不能让同一delivery进入第二turn；
- `TurnRequestId`从queue identity、target Agent/lifecycle generation和按顺序排列的delivery identity tuple确定性派生，并绑定batch内容digest、root/subtree、priority、deadline、config generation和maximum attempts。仅delivery IDs相同但其他acceptance facts变化必须typed conflict；
- delivery batching不改变单条delivery的ack语义。只有该TurnRequest terminal settlement证明相应输入已被处理后，才逐delivery ack；turn失败/retry沿用同一request identity与batch，不重新accept第二turn；
- `QUEUE_ONLY` delivery仍可durable accepted但不单独触发turn；它在后续合法trigger delivery到达时按acceptance sequence一起进入batch。若没有trigger，wake不能擅自把它升级为trigger；
- broadcast/subtree继续为每个target生成独立delivery identity和settlement，不建立一个跨Agent共享TurnRequest；
- 无业务payload的owner maintenance/reconcile不是Message，不伪装成delivery或Agent turn；它走各自typed control command。

拒绝的备选：独立`DirectInputId`会复制delivery durability与ack；允许空delivery tuple会产生不可重建请求；用message id单独派生会遗漏target generation/mode/payload冲突；先drain mailbox再accept会在crash时丢输入或重复归属。

authoritative落点：delivery intent/identity与ack由`orchestration/agents/messaging`拥有；TurnRequest/batch ownership与scheduler fence由`orchestration/agents/turn_queue`拥有；Product surfaces和Cron只调用最小delivery command Port；Runtime Role/Kernel不拥有输入acceptance。

确认文本：

> 确认 `D18-agent-turn-input-via-delivery-v1`：所有驱动Agent业务turn的Message先成为canonical durable delivery，不建立DirectInputId第二状态机。删除生产裸`notify(Message)`入口；wake只唤醒已存在durable fact且不能承载payload。TurnRequestId绑定target lifecycle generation和有序delivery batch，一个delivery最多归属一个accepted turn；stage/accept/retry/ack使用同一request identity并可从crash恢复。QUEUE_ONLY不自行触发，broadcast/subtree逐target独立结算。

解除阻断：B27普通Message identity、batch/排序/归属和mailbox drain/accept crash语义；B19 delivery owner与B23/B27 capacity原子性仍须各自独立需求闭合。现有无delivery mailbox/residency旧数据如何处置仍需`D01-agent-mailbox-turn-*`实例，不能由D18静默清除。

### 28.3 D08：Cron存储目录是可信本地authority，不承诺抵抗同权限篡改

推荐instance：`D08-cron-trusted-local-store-v1`。

通俗解释：Cron JSON位于当前用户可写的本地存储中。同一OS用户若绕过Mote直接改文件，应用很难仅靠普通checksum证明“这不是命令面写的”；引入签名密钥、TPM或远端authority会新增安全产品和运维成本。当前产品没有跨信任域防篡改要求，因此推荐明确：Cron store目录属于可信部署边界，但外部编辑不是受支持API，也不会热加载。

推荐决定：

- Product声明Cron durable store root由当前Mote application OS identity独占管理；目录、文件、lock和临时文件必须owner-only，拒绝symlink、错误UID/mode、路径逃逸和非regular file；
- 当前威胁模型不防御同一OS identity、root、磁盘管理员或离线磁盘编辑。合法shape文件在重启时可作为该可信本地authority的状态读取；文档不得声称有cryptographic provenance或检测所有旁路写入；
- 删除“external edit hot reload”产品能力、注释、mtime通知和对应测试。运行中schedule mutation只能经过canonical typed `CronTaskCommands/service`，CLI也必须调用该command owner，不能直接构造/修改store；
- scheduler正确性只依赖canonical store revision、expected revision和scheduler lease/fence。最佳努力notification可以减少延迟，但每个有界reconcile周期必须读取/比较canonical revision；mtime最多作为非authoritative诊断，不得决定是否跳过revision读取；
- store startup严格校验version、exact schema、revision、identity、clock、occurrence transition和文件安全属性。未知/损坏状态fail closed并进入已确认的typed recovery流程，不把corruption当空schedule；
- 不新增HMAC/signature、远端attestation、append-only审计服务或第二snapshot来伪造provenance。若未来Cron用于多租户、高权限基础设施或不可信workspace，另立产品需求决定密钥owner、rotation、backup/restore和跨主机验证；
- store具体实现不能从包外公开。Product composition获得`CronCommandPort`和immutable query snapshot；scheduler获得窄schedule/occurrence owner service。测试fixture可直接构造store，但不形成生产绕过证据；
- backup/restore、管理员离线修复若未来支持，必须是停机下的typed Product command和receipt，不得复活文件热编辑。

拒绝的备选：继续mtime hot reload会绕过revision/command；声称能检测同用户合法shape改写却不引入独立信任根是不真实保证；现在建设签名/远端authority属于无consumer未来能力。

authoritative落点：Cron schedule/occurrence state仍由`orchestration/automation/cron`拥有；Product选择trusted store root并装配唯一command surface；filesystem安全检查属于store adapter，但不能反向成为业务mutation authority。

确认文本：

> 确认 `D08-cron-trusted-local-store-v1`：Cron store root属于当前Mote OS identity独占的可信本地authority，严格检查owner/mode/symlink/path/schema/revision，但当前不承诺抵抗同OS用户、root或离线磁盘篡改，也不建设签名/远端provenance。删除external-edit hot reload和mtime控制语义；生产mutation与CLI统一走typed Cron command owner，scheduler以canonical revision和周期reconcile推进。未来跨信任域防篡改另立需求。

解除阻断：B28 storage trust、external edit、mtime reload、唯一命令面和provenance声明。Cron terminal occurrence retention/purge仍需Cron专属D02/D03/D07，任何schema改变仍需D01 Cron实例。

### 28.4 本批建议与累计待确认

推荐D18和D08全部确认：所有Agent输入先有durable delivery身份；Cron本地文件可信但不支持直接编辑，也不假装具备防同用户篡改能力。

当前累计八项待确认：

- 第25节：D06 daemon单generation升级、D14 published Tool统一授权链；
- 第26节：D04三个Sandbox profile、D13 async verified fixed argv；
- 第27节：D16 Connection close、D20 Notebook stdin；
- 第28节：D18 Turn via delivery、D08 Cron trusted store。

全部接受时可回复“确认第25至28节”；继续评审则保持全部为PROPOSED并进入下一批。

## 29. 第三至第六批决定确认及后续评审授权方式

### 29.1 用户确认与工作方式修正

用户已明确表示“我都接受，我让你给需求提意见，别问我”。据此：

- 第25–28节共八项推荐决定全部由`PROPOSED`变更为`CONFIRMED`；
- 后续评审不再把能够依据当前产品定位、`AGENTS.md`、源码事实和零债务原则确定的技术/架构选择逐项提交用户确认；
- reviewer应直接比较方案、选择推荐项、记录被拒备选和理由，并把结论写入审核Markdown及后续authoritative decision ledger输入；
- 只有当前事实无法推导真实业务目标，或决定需要明确授权实际丢弃现有用户数据、承担新的外部兼容承诺、引入新付费/第三方依赖、扩大网络/权限暴露时，才记录为外部产品阻断。即使存在这类阻断，也应先给出完整推荐与影响，不用连续技术问答打断评审；
- 本授权只用于完成需求审核和形成开工前决定，不授权现在修改生产代码或实际删除数据。

这修正了此前把用户当作逐项技术决策人的低效流程。后续目标仍是：在生产实施前把所有可预见的owner、contract、migration、retention、authority、failure policy、bounds和DAG提前写清，让实施者不需要临场做架构决定。

### 29.2 已确认的八个scoped instances

以下决定状态均为`CONFIRMED`，完整语义以其提案节为准：

1. `D06-inference-daemon-single-generation-upgrade-v1`：daemon单generation停旧启新，30秒drain、10秒terminate等待，退出旧PID/socket/fence后才发布新generation，不保留`current-1`永久兼容。
2. `D14-published-tool-permission-applicability-v1`：所有published Tool统一经过ToolExecutor授权/sandbox/effect链；普通低风险读取可由baseline自动允许，但不跳过安全检查；NOT_APPLICABLE仅限非published内部纯计算。
3. `D04-tool-process-sandbox-profiles-v1`：Product只提供三个versioned profile；required enforcement无法证明时不spawn，禁止silent degradation冒充已满足保证。
4. `D13-fixed-internal-argv-execution-v1`：固定内部程序只走async verified argv，activation绑定absolute executable identity，按consumer使用最小argv/env policy，无同步桥接或第二runner。
5. `D16-product-connection-close-settlement-v1`：Connection分阶段close，10秒单次预算、三次有界重试；失败保留token/generation并保持DRAINING，不杀共享Agent；全局shutdown最多等待30秒后报告incomplete。
6. `D20-notebook-stdin-incarnation-v1`：stdin绑定Runtime/kernel/execution/cell/handoff/surface generation；旧reply capability在restart/terminal/cancel/close/expiry后失效；password plaintext不进入普通durable state、投影或日志。
7. `D18-agent-turn-input-via-delivery-v1`：所有业务Message先成为canonical durable delivery；删除裸`notify(Message)`生产入口；TurnRequest绑定有序delivery batch且单delivery最多属于一个accepted turn。
8. `D08-cron-trusted-local-store-v1`：Cron store是当前Mote OS identity的可信本地authority；删除external-edit hot reload和mtime控制，不承诺抵抗同用户/root/离线磁盘篡改，也不预建签名provenance。

### 29.3 阻断解除映射

- B30的daemon protocol upgrade方向不再被D06阻断；结合已确认D10/D07，strict discovery、local cleanup和单generation cutover的产品语义已齐备。
- B12的Hook lifecycle与Permission applicability方向不再被D14/D15阻断；B32 profile方向不再被D04阻断。
- B17 fixed argv策略不再被D13阻断。
- B15 Connection lifecycle强制退出策略不再被D16阻断；Tool/MCP与Agent control仍按不同owner拆单。
- B20 Notebook stdin产品语义不再被D20阻断。
- B27直接输入identity不再被D18阻断；旧mailbox数据、capacity、retention仍需各自scoped决定。
- B28 storage trust不再被D08阻断；Cron migration、retention、delete authority和bounds仍需逐scope设计。

上述“解除产品决定阻断”不等于立即获准修改生产代码。Wave 0治理artifact、独立requirement、唯一writer/write set、owner/复用证据、schema revision和机械验收仍是编码前置。

### 29.4 后续评审输出格式

从下一轮开始，不再设置“请用户确认”章节。每轮直接输出：

1. reviewer最终选择及其scoped decision状态；
2. 被拒方案和原因；
3. authoritative owner/contract/schema落点；
4. migration、retention、authority、failure和bounds；
5. 解除的阻断与仍存在的真实依赖；
6. 对独立requirement及最终开工顺序的影响。

凡属于本次授权范围且没有真实外部产品阻断的推荐项，直接按`CONFIRMED`写入审核记录；后续authoritative ledger必须机械承接，不能只依赖本节自然语言。

## 30. 第七批已确认评审结论：LSP子协议与Presentation事件兼容

依据第29节授权方式，本节对能够由现有产品consumer和源码事实推导的D09/D12直接作出`CONFIRMED`结论，不再设置用户确认题。

### 30.1 D09：只支持LSP 3.17 code-map profile，不承诺完整LSP

scoped instance：`D09-lsp-3.17-code-map-profile-v1`，状态`CONFIRMED`。

当前真实consumer只使用document sync、diagnostics、document symbols、definition和references。产品没有通用IDE client、动态plugin capability或实现LSP全部method的需求。因此正式支持面定义为一个有限profile，而不是含糊地宣称“支持LSP”。

确认决定：

- profile identity固定为`lsp-3.17-code-map-v1`，语义基于LSP 3.17；只装配stdio JSON-RPC transport，不预留TCP/WebSocket transport；
- 支持的lifecycle/method集合封闭为：`initialize`、`initialized`、`shutdown`、`exit`、`textDocument/didOpen`、`didChange`（FULL sync）、`didSave`、`publishDiagnostics`、`documentSymbol`、`definition`和`references`；
- client在initialize中声明该profile、client identity与静态capabilities，禁止dynamic registration。server initialize result必须严格解码并确认实际method/sync能力；缺失能力返回typed `UNSUPPORTED_CAPABILITY`，对应query不激活；
- position encoding只支持协商后的UTF-16。server明确只支持不兼容encoding时拒绝该profile；未声明时按3.17默认UTF-16。不得按Python code-point索引猜测；
- URI只接受当前approved workspace root内的canonical `file:` URI。非file scheme、路径逃逸、非法percent encoding、symlink escape和错误primitive fail closed；
- JSON-RPC transport严格验证`jsonrpc="2.0"`、id correlation、result/error互斥、error object、header唯一性、Content-Length数值/上限、UTF-8和JSON top-level object。malformed frame关闭endpoint并结算全部pending request为typed transport/protocol failure，禁止返回`{}`后继续；
- LSP wire object按标准允许未知扩展字段，但必需字段和已消费字段严格校验；投影后的canonical DTO必须frozen/exact。不能把LSP合法扩展字段误判为corruption，也不能把错误类型强转；
- `documentSymbol`完整支持`DocumentSymbol[] | SymbolInformation[] | null`。两种symbol类型保持不同typed variant；递归children有深度/总项数上限，range/selectionRange和container/URI语义分别校验；
- `definition`完整支持`Location | Location[] | LocationLink[] | null`，不能把`LocationLink`当`Location`；`references`支持`Location[] | null`；空数组/null均为合法`SUCCESS_EMPTY`；
- diagnostics至少严格投影Range、message、severity、code/source和related information中当前consumer使用的合法变体；未知severity不能伪造成默认级别。单条非法diagnostic产生typed reject/计数，不污染成功集合；整帧/envelope损坏关闭endpoint；
- query result使用typed receipt，至少区分`SUCCESS_EMPTY / SUCCESS_WITH_ITEMS / UNAVAILABLE / UNSUPPORTED_CAPABILITY / TIMEOUT / INVALID_RESPONSE / SERVER_ERROR / CANCELLED`。只有两个SUCCESS状态可写code-map cache；其余状态可使LSP advisory能力降级，但不能冒充空结果；
- manager/service/Role/code-map边界只传canonical DTO/receipt，不传播provider dict/list；notification reader task由endpoint lifecycle owner持有，异常、EOF和shutdown结算所有pending future；
- 新增method、encoding、transport或LSP版本必须创建新profile generation和合法union fixtures；不能靠裸dict或“server通常这样返回”扩张当前profile。

被拒方案：实现完整LSP 3.17成本远超真实consumer；仅定义最小DTO会拒绝`SymbolInformation`/`LocationLink`等合法响应；继续best-effort返回空列表会污染cache并掩盖provider故障；开放dynamic capability registry会引入未经Product批准的变化轴。

authoritative落点：LSP canonical query/result DTO与最小Port应位于Contracts code-intelligence bounded context；Runtime LSP adapter拥有3.17 wire decoder、endpoint和server lifecycle；Product配置只选择批准的server command/language映射和profile activation；code-map消费typed projection，不读取LSP wire。

解除阻断：B31 protocol/capability范围、合法result union、failure disposition与cache语义已确定。实施仍须独立需求登记fixed server process runner依赖D13、write set和各method fixture，但无需实施者决定支持哪些LSP变体。

### 30.2 D12：ViewEvent按Product generation封闭，不承诺未知event免升级兼容

scoped instance：`D12-presentation-view-event-closed-generation-v1`，状态`CONFIRMED`。

当前文档把`ViewEvent`称为开放union并要求consumer忽略未知kind，但代码又依赖具体Pydantic subclasses、`isinstance`分支和静态mapper。Structured consumer还用`json.dumps(default=str)`把未知对象静默字符串化。这既不能获得静态穷尽性，也没有真正的跨版本wire negotiation。

确认决定：

- Product内部每个presentation generation拥有一个closed `ViewEvent` discriminated union、唯一event catalog和唯一strict codec；`kind`使用Literal/tagged variant，不能由任意subclass、plugin或import副作用扩展；
- 当前generation内新增ViewEvent是schema变更，必须同时更新union/catalog、projector、capability adapter、所有production consumer的显式disposition、wire codec和fixtures；architecture gate双向检查“union中每项都有consumer disposition，consumer没有catalog外handler”；
- consumer disposition使用typed三类：`REPRESENTED`（映射到surface wire）、`EXPLICITLY_DOWNGRADED`（由批准adapter转换为已知事件）、`NOT_REPRESENTABLE`（该surface明确丢弃并计数/观测）。不存在默认`on_unhandled -> []`或无记录忽略；
- `NOT_REPRESENTABLE`只表示当前已知event对某surface无表达，不是unknown event fallback。control、approval、question、error、durability failure等安全/交互关键事件不得无声NOT_REPRESENTABLE；若surface无法表达则activation capability negotiation失败或连接返回typed unsupported；
- ACP/AG-UI是各自外部协议adapter，不要求一一映射，但每个ViewEvent必须在各adapter manifest中有上述显式disposition。外部协议没有对应概念时可按approved downgrade组合多个wire event或明确不表示；
- Structured JSON Lines成为versioned正式surface schema。每行使用严格envelope，至少绑定`schema_version`、`presentation_generation`、`sequence`和closed event payload；删除`default=str`。Path、enum、scope、identity、media/artifact ref均通过canonical encoder；不可编码值返回typed projection failure，不能输出部分伪合法JSON；
- 外部consumer必须显式声明/协商支持的presentation generation。只发送双方共同generation；无共同generation时拒绝activation/connection。当前不支持在同一连接中混发generation，也不让旧consumer接收未知kind后自行猜测；
- 若ACP/AG-UI底层协议自身允许未知extension，容忍发生在该external wire adapter边界，不反向把内部ViewEvent union变成开放registry；
- ViewEvent scope使用Contracts-owned canonical versioned scope declaration与strict wire codec。machine event到ViewEvent再到surface wire保持同一scope identity；禁止tuple[Any]、`default=str`或presentation自造scope tag；
- event是纯presentation intent，不成为Agent/session canonical business truth。需要跨重连恢复的surface sequence/projection依据上游durable facts重建，不能为closed union新建第二业务event store；
- 新事件/字段通过新presentation generation演进。同一切片迁移全部仓内producer/consumer并退出旧内部generation；只有确有同时服务旧外部client的产品SLO时，才另立有期限的边缘adapter，不在内部保留双union/双projector。

被拒方案：开放Python subclass registry会依赖import顺序且破坏穷尽门禁；未知kind静默忽略可能丢失审批/错误；要求所有外部surface一一表达会错误统一不同协议；永久多generation内部双写/双读违反零兼容债务。

authoritative落点：canonical scope纯声明位于Contracts合适的activity/scope bounded context；closed ViewEvent generation与Product projection属于`product/presentation`；ACP、AG-UI、Structured分别拥有自己的external wire adapter和显式disposition manifest；Runtime machine events不import Product type。

解除阻断：B7 closed/开放冲突、consumer穷尽策略、unknown disposition和Structured strict codec方向已确定；B8 scope owner与端到端identity方向已确定。具体machine-event scope migration如涉及durable历史，仍需其domain D01实例。

### 30.3 对实施顺序的影响

新增独立需求建议：

1. `R-W2-LSP-001`：先建立Contracts typed LSP result/receipt和3.17 profile declaration；不得同时改code-map cache。
2. `R-W2-LSP-002`：实现strict JSON-RPC/LSP adapter、capability activation和typed service；依赖`R-W2-LSP-001`及D13 fixed server runner。
3. `R-W2-LSP-003`：迁移code-map consumer/cache并删除裸dict/list和成功空集合fallback；依赖前两项。
4. `R-W2-PRESENTATION-001`：先移动canonical scope declaration并建立strict scope codec；machine durable history范围另拆migration单。
5. `R-W2-PRESENTATION-002`：建立closed ViewEvent generation、catalog/disposition gate并迁移Product producers/consumers。
6. `R-W2-PRESENTATION-003`：分别收敛Structured、ACP、AG-UI wire adapter，删除`default=str`和unknown silent drop；与各surface文件write set互斥。

LSP与Presentation两个workstream没有真实contract依赖，可以并行；各自内部必须按上述顺序推进，不能先在末端consumer添加cast或临时fallback。

## 31. 第八批已确认评审结论：Workflow effect对账与RunJournal退场边界

依据第29节授权，本节直接关闭D11及B25/B34之间最关键的owner选择。不会把provider未知保证留给实施者决定。

### 31.1 D11：effect capability决定重试权，lease只决定提交权

scoped instance：`D11-workflow-effect-reconciliation-v1`，状态`CONFIRMED`。

核心结论：lease/fence只能证明“谁可以提交Mote canonical settlement”，不能证明外部动作没有发生，也不能因此授权重试。每个Workflow effect在definition activation时必须选择以下四种封闭capability之一，并满足对应provider contract：

#### `NO_EXTERNAL_EFFECT`

- 仅用于确定性纯计算或只修改同一authoritative transactional state、且失败可证明没有外部可观察动作的activity；
- 可以自动重试，但每次使用同一logical EffectId、command digest和definition generation；
- 任意新增IO、进程、网络、用户可见动作会使classification失效，必须升级capability并重新批准；
- “写本地文件”不自动属于此类，除非写入已由canonical owner transaction/rollback闭合且不越出其状态边界。

#### `IDEMPOTENT_BY_KEY`

- provider必须正式承诺：同一namespace/account下stable idempotency key永久或在声明窗口内绑定同一canonical request；重复同key同payload返回同一操作/receipt，不同payload返回conflict且不会执行第二次；
- Mote在外部动作前durable commit intent，EffectId同时作为provider key或确定性派生provider key；provider key、request digest、account/endpoint identity和有效窗口进入definition contract；
- 自动重试只能使用完全相同的key、payload、provider account和definition generation；任何变化创建新EffectId并重新走Workflow状态机，不能“修参数后沿用旧key”；
- provider未证明conflict detection、key retention窗口或query语义时，不得标此类，降级为`RECONCILABLE_BY_RECEIPT`或`NON_REPLAYABLE`。

#### `RECONCILABLE_BY_RECEIPT`

- provider必须提供stable request/operation identity或可验证receipt，以及不会产生第二动作的status query；
- execute只有一次自动发起权。连接中断、timeout、worker crash或fence丢失后不得再次execute，先进入reconciliation query；
- query可返回`NOT_STARTED / PENDING / SUCCEEDED / FAILED / UNKNOWN`。只有provider明确证明`NOT_STARTED`且原intent仍current时，current fenced owner才能重新发起；`UNKNOWN`进入canonical IN_DOUBT，不能把query失败解释为未执行；
- provider/process raw receipt是irreplaceable evidence，绑定EffectId、attempt ordinal、request digest和provider identity；current owner只根据严格验证的evidence/query提交canonical settlement。

#### `NON_REPLAYABLE`

- 用于无idempotency key、无可靠query/receipt、且可能产生外部动作的能力；
- durable intent提交后最多发起一次。任何未获得可信terminal receipt的timeout/crash/fence loss直接进入IN_DOUBT；
- 不自动重试、不自动标FAILED、不因最大attempt耗尽进入普通dead letter；必须由Product owner通过typed reconciliation command选择“外部已成功、外部已失败、保持未知或创建全新补偿effect”；
- 补偿是新的Workflow effect与identity，不是重放旧effect。

### 31.2 stale owner取得receipt后的证据交接

外部动作返回与canonical commit之间可能丢失lease。确认如下：

- stale owner绝不能提交Workflow effect state、IN_DOUBT或terminal result，也不能release/refresh新owner lease；
- provider adapter必须优先依赖provider-side idempotency/query identity，使current owner能从外部authority重新取得事实；
- 若provider返回不可从query重建的原始receipt，允许写入一个attempt-scoped、append-only、immutable evidence inbox。它不是settlement truth，只接受绑定`EffectId + attempt ordinal + command digest + provider identity`的typed evidence；重复同内容幂等，不同内容conflict并告警；
- evidence inbox不得包含“SUCCEEDED/FAILED由谁决定”的可变状态，不得被Workflow runner当作已结算。current fenced reconciler严格验证evidence后，使用expected revision提交canonical Workflow settlement；
- Temporal backend优先以Temporal activity history/result作为attempt evidence；provider原始receipt仍按provider contract保存。LOCAL_FILE backend若没有可复用的canonical evidence mechanism，不得自行建随意JSON日志，必须先完成`AGENTS.md §6.4`复用审计；
- evidence写入失败且外部动作可能发生时，operation保持IN_DOUBT/owner-action-required。不能为了“没有证据”伪装成未执行；
- irreplaceable evidence按对应effect retention/legal hold保存，不能跟普通CI日志一起过期。

### 31.3 canonical Workflow effect identity与冲突语义

当前`effect_identity(run_id, logical_key)`不足以阻止同identity不同capability/payload被误报幂等。新contract要求：

- logical EffectId由WorkflowRunId、definition id/generation、node/step identity和logical effect key确定性派生；attempt ordinal不进入logical EffectId；
- canonical command preimage绑定capability、versioned payload variant、payload digest、provider/endpoint/account identity、permission/effect definition generation和provider idempotency/query contract revision；
- `submit_effect`遇到同EffectId时，全部preimage facts一致才返回idempotent receipt；任一不同返回typed `EFFECT_IDENTITY_CONFLICT`，不得覆盖旧record或创建第二effect；
- `command_payload`不再是裸JSON字符串，改为domain-owned versioned tagged union或ArtifactRef；strict decoder拒绝unknown version/tag、额外/缺失字段和错误primitive；
- provider receipt同样使用typed tagged evidence/reference，不以任意字符串同时表达“无receipt、原始receipt和已对账结果”；
- state至少区分`INTENT_COMMITTED / CLAIMED / EXECUTION_STARTED / RECONCILING / SETTLED_SUCCEEDED / SETTLED_FAILED / IN_DOUBT / OWNER_ACTION_REQUIRED / COMPENSATED`。普通retry exhaustion不能把未知external effect标成DEAD_LETTER；
- claim绑定record revision、Workflow run/definition generation、execution owner fence和attempt ordinal。stale owner不能开始新attempt或提交任何canonical transition。

### 31.4 Workflow terminal delivery复用domain delivery owner，不复制delivery状态机

`WorkflowTerminalDelivery`是Workflow outbox intent，不应成为第二个Agent/Event delivery engine：

- Workflow ownerdurable记录“该terminal outcome必须交付到destination”的outbox intent与outcome digest；
- destination为Agent时调用已确认的canonical Agent delivery command，保存其stable delivery identity/receipt引用；destination为其他domain时调用该domain正式delivery Port；
- deliver/retry/ack/dead-letter由destination delivery owner闭合，Workflow store只根据typed receipt结算自己的outbox intent；不能自己复制target mailbox、subscriber lease或transport retry状态；
- `delivery_identity(run_id, destination_id)`还须绑定run terminal revision、outcome digest和destination generation/contract。相同identity不同outcome返回typed conflict；
- Workflow observation/query返回immutable typed projection，不暴露内部dict、store record或mutable collection。

### 31.5 RunJournal与Temporal Workflow effect必须分离

scoped owner决定：`D11-workflow-effect-run-journal-separation-v1`，状态`CONFIRMED`。

`RunJournal("application-workflow-effects", ...)`不再作为Temporal Workflow effect plane的共享memoization/authoritative记录：

- Workflow effect canonical intent、claim、attempt、reconciliation和terminal settlement唯一属于`orchestration/workflows`；
- Temporal workflow/activity history属于Temporal backend的execution history和attempt evidence，不是第二Mote settlement truth；
- Runtime `RunJournal`只保留经逐consumer审计证明属于Runtime session/run step的不变量：per-session Tool/Inference think/tool/timer execution事实。它不接收Workflow definition、effect、terminal delivery或reconciliation record；
- `TemporalBackend`不得要求或公开`RunJournal`来表示“两个tier共享step truth”。Product Temporal adapter只注入typed Workflow effect command/evidence Port与frozen activity catalog；
- 若确需把Workflow outcome投影到Session/Runtime观察面，只能发布immutable typed projection，不能由RunJournal反向驱动Workflow恢复或结算；
- 删除固定application-wide session id `application-workflow-effects`及其生产writer。旧数据不得静默删除：先inventory其记录kind、关联Workflow effect和是否存在唯一receipt，再由`D01-workflow-effect-cutover-v1`决定一次性迁移为canonical Workflow evidence/settlement、archive-only evidence或经授权清除；cutover完成后旧reader/writer退出。

这一选择拒绝“因JSONL接口相似就统一journal”。Session step与Workflow effect的identity、scope、retention、owner、crash recovery和external reconciliation不同，强行复用只会产生巨型execution journal或双effect truth。

### 31.6 自动处理上限与人工处置

scoped instance：`D07-workflow-effect-reconciliation-bounds-v1`，状态`CONFIRMED`。

- `NO_EXTERNAL_EFFECT`与`IDEMPOTENT_BY_KEY`最多3次execute attempt，backoff为1/5/30秒；每次attempt timeout由definition声明，Product hard range为1秒至5分钟；
- `RECONCILABLE_BY_RECEIPT`自动execute最多1次；自动query最多12次，backoff为5秒、30秒、2分钟、10分钟，之后按指数增长但单次不超过1小时，总自动观察窗口不超过24小时；仍UNKNOWN转`OWNER_ACTION_REQUIRED`，不再次execute；
- `NON_REPLAYABLE`自动execute最多1次、自动execute retry为0；没有可靠query时直接IN_DOUBT并转owner action；
- 单次reconciler scan最多处理500个eligible effect，每个claim后单独结算；一个poison/未到期item不能阻塞后续eligible item；
- active unresolved effect capacity默认10,000、Product hard max100,000；容量耗尽返回typed BACKPRESSURED且不提交新intent，不驱逐已accepted effect；
- command inline payload最大1 MiB，超过使用canonical ArtifactRef；provider receipt inline最大64 KiB，超过使用irreplaceable evidence ArtifactRef；禁止截断后仍声称完整receipt；
- owner action没有时间自动转成功/失败；必须有typed command、批准authority、reason和audit receipt。legal hold/IN_DOUBT未关闭前不能purge。

这些数值由versioned Product schema选择，Runtime/extension只能收窄。它们不适用于Agent delivery、Cron或普通RunJournal compaction。

### 31.7 仍需后续闭合但不再开放的实现决定

本轮已关闭D11、Workflow effect owner和核心bounds，不再允许实施者重新选择重试语义。仍须继续设计：

- `D01-workflow-reconciliation-v2-to-v3`：现有reconciliation v2及旧application Workflow RunJournal的一次性cutover；
- Workflow effect、terminal delivery及irreplaceable evidence的D02 retention和D03 purge/owner-action authority；
- provider/capability inventory：每个真实Workflow effect handler逐项证明属于四类中的哪一类及其idempotency/query证据；无法证明的一律`NON_REPLAYABLE`；
- Temporal history、provider evidence与Workflow settlement的fault-injection fixtures，覆盖外部动作前后、activity return前后、lease loss和canonical commit failure。

### 31.8 独立需求与顺序

1. `R-W3-WORKFLOW-EFFECT-001`：建立typed EffectId、command/evidence/settlement contract和四类capability gate。
2. `R-W3-WORKFLOW-EFFECT-002`：设计并验证v2→v3/旧RunJournal cutover；依赖001和D01 cutover实例，不能先改writer。
3. `R-W3-WORKFLOW-EFFECT-003`：重构Workflow reconciliation canonical store/command/query与fenced attempts；依赖001/002。
4. `R-W3-WORKFLOW-TEMPORAL-001`：从TemporalBackend移除RunJournal，接入typed command/evidence并证明Temporal history只是backend evidence；依赖003。
5. `R-W3-WORKFLOW-DELIVERY-001`：把terminal outbox接入destination canonical delivery Port并删除复制的delivery lifecycle；可与Temporal单并行，但共享Workflow schema时必须由同一writer协调。
6. `R-W3-RUNJOURNAL-001`：在Workflow writer退出后，单独治理剩余Runtime session RunJournal的跨进程revision/fence/compaction；不得与Workflow effect需求合并。

Workflow effect contract是该组唯一最前置；migration必须先于新writer activation；RunJournal治理最后只面对确认保留的Runtime consumer集合。

## 32. 第九批已确认评审结论：Workflow effect migration、retention与删除authority

本节承接第31节，直接关闭`R-W3-WORKFLOW-EFFECT-002/003`开工前的D01/D02/D03阻断。原则是保留已有事实、不给缺少新contract的旧记录虚构执行权、migration完成后立即退出旧reader/writer。

### 32.1 D01：Workflow reconciliation v2一次性迁移到v3

scoped instance：`D01-workflow-reconciliation-v2-to-v3`，状态`CONFIRMED`。

确认决定：

- v3 activation前由`orchestration/workflows` canonical store owner在独占migration lock下执行一次性migration；Product只触发该唯一入口，CLI或Temporal worker不得自行迁移文件；
- migration先严格验证整个v2 envelope、collection类型、重复identity、primitive、state transition、AbsoluteInstant clock和每条payload。任一损坏/重复/unsupported state使整次migration回滚并fail closed，Workflow effect admission和reconciler均不得启动；
- migration使用`read v2 -> build complete v3 candidate -> flush/fsync temporary -> atomic replace -> parent fsync`。源文件在成功replace前保持不变；receipt记录source/target schema、source digest、record counts、各disposition counts、target digest、开始/完成instant和migration implementation identity；
- migration可在同一事务中生成内容寻址的只读source backup identity，但backup不是第二active store。v3验证成功和声明的rollback禁用点之后，source backup仅按migration evidence retention保存；生产恢复不能切回v2 writer；
- v2 effect的logical EffectId保留。`command_payload`严格解析为旧canonical JSON值后重新编码为v3 tagged legacy-command variant并计算digest；不伪造旧记录不存在的definition generation、provider account或idempotency contract；
- v2 `SETTLED`且具有可严格解释terminal settlement/receipt的记录迁为相同terminal事实，标记`imported_from_v2` provenance；migration不能把FAILED/SUCCEEDED互换或重新执行；
- v2 `AVAILABLE`尚未执行的effect因缺少已批准provider contract，迁为`OWNER_ACTION_REQUIRED`，reason=`LEGACY_CONTRACT_REBIND_REQUIRED`。只有Product重新绑定到当前definition/provider contract、全部preimage一致并产生typed rebind receipt后，才能创建新attempt；不得由migration自动分类为IDEMPOTENT；
- v2 `CLAIMED`、执行中断、存在receipt但非terminal或无法证明是否发生的effect迁为`IN_DOUBT`，attempt ordinal至少保留为1；绝不回到AVAILABLE；
- v2 `IN_DOUBT`原样保持IN_DOUBT；v2 `DEAD_LETTER`若capability涉及外部动作且没有可信terminal receipt，迁为`OWNER_ACTION_REQUIRED`而非永久失败。仅能证明未执行或纯计算retry exhaustion的记录可迁为typed terminal failure；
- v2 terminal delivery计算outcome digest并迁为Workflow outbox intent。已SETTLED且receipt可验证则保留；AVAILABLE/CLAIMED/DEAD_LETTER必须重新绑定current destination contract/identity，通过destination canonical delivery query确认后再结算，不能直接把旧payload重新发送；
- governance cancellation records保持其独立schema/state语义，不混入effect capability migration；若v3修改其shape，使用同一candidate transaction中的独立strict converter和计数，不复用effect字段默认值；
- v3 candidate写完后执行离线全量不变量验证：identity/preimage唯一、revision单调、state/attempt合法、ArtifactRef可寻址、run/definition reference一致、terminal record不可重新eligible。验证通过才replace；
- v3首次activation完成后，生产代码只含v3 decoder/writer。v2 decoder移入versioned migration-only工具并从production-capable recipe set移除；下一个治理release在所有批准deployment完成migration后删除migration-only decoder，退出条件由active-store inventory证明v2为零。

被拒方案：给旧记录补默认provider generation会伪造安全保证；把所有AVAILABLE直接重试可能重复effect；把所有旧记录都IN_DOUBT会不必要地阻塞已严格terminal事实；长期双读会把migration变成兼容层。

解除阻断：Workflow reconciliation v3 schema/writer cutover可以设计；仍须按下述RunJournal cutover处理旧application-wide evidence。

### 32.2 D01：旧`application-workflow-effects` RunJournal只迁移为证据，不迁移为第二truth

scoped instance：`D01-workflow-effect-run-journal-cutover-v1`，状态`CONFIRMED`。

确认决定：

- migration inventory逐条严格解码旧RunJournal，按step/effect identity、WorkflowRunId、payload/receipt digest和时间关联v2/v3 effect；仅名称、相近时间或固定session id不能证明关联；
- 与唯一Workflow effect严格匹配且提供canonical store没有的provider/activity receipt时，转换为v3 attempt-scoped immutable evidence，并记录source journal record identity/digest；它不能单独把effect推进为SUCCEEDED/FAILED；
- 与canonical terminal settlement一致的重复record只登记deduplicated evidence reference，不复制payload或建立第二terminal事实；
- 与canonical effect冲突的record进入typed migration conflict，整个相关effect保持IN_DOUBT/OWNER_ACTION_REQUIRED；不得选择“较新时间”覆盖；
- 无法关联到Workflow effect但严格合法的record进入`ARCHIVE_ONLY` migration artifact，保留原始record identity、digest、kind和secret-safe索引；archive无execute/query-as-current能力，不进入Workflow recovery scan；
- 损坏中段、重复record identity或无法区分允许torn tail时fail closed，不删除源journal。只有一个未换行尾部且现有protocol明确允许时，migration receipt可记录truncated torn-tail boundary；
- 完成v3 evidence/archive发布、全量计数对账和receipt fsync后，删除Product Temporal对`RunJournal("application-workflow-effects")`的构造与writer。旧路径只由migration/archive inspector识别，不能被Runtime session RunJournal owner重新激活；
- migration完成不立即物理删除旧journal；先将其标记为retired source evidence并按第32.3节保留。所有deployment cutover且retention/hold关闭后，由第32.4节authority purge。

解除阻断：Temporal effect plane可从Runtime RunJournal彻底分离；B34后续只面对真实Runtime session consumers。

### 32.3 D02：Workflow effect采用90天完整事实、1年最小证据

scoped instance：`D02-workflow-effect-retention-v1`，状态`CONFIRMED`。

确认决定：

- retention clock只从effect/outbox真正terminal且所有attempt、destination delivery、compensation、Artifact pin和provider settlement完成的instant开始；run terminal、lease过期或最后修改时间不能替代结算事实；
- terminal后完整command payload、普通result、非必要diagnostic和可重建执行细节默认保留90天；超过inline阈值的内容从开始即使用canonical ArtifactRef，不能在Workflow store复制blob；
- 90天到期且无hold/reference后，由current fenced ownercompact为最小effect tombstone：EffectId、run/definition generation、command digest、capability、provider identity、attempt count、terminal disposition、terminal revision/time、provider receipt/evidence reference digest、compensation/outbox settlement引用和migration provenance；
- 最小effect tombstone及不可替代provider/process/Temporal attempt evidence默认保留1年，用于幂等、争议调查和防identity reuse；一年后只有全部legal hold、audit/export、destination delivery、effect query、compensation和artifact edge关闭才可purge；
- `IN_DOUBT`、`OWNER_ACTION_REQUIRED`、RECONCILING、未结算delivery、未结算compensation或active legal hold不启动任何自动retention clock，完整必要evidence持续保留直到typed settlement；不得把“超过一年仍未知”自动判为失败或可删；
- security-sensitive command plaintext可以在90天前由独立security-clear command做typed redaction，但必须保留digest、identity、provider evidence和redaction receipt；不能借security clear删除仍需对账的唯一receipt；
- 第32.2节无法关联的合法`ARCHIVE_ONLY`旧RunJournal默认保留180天；若被investigation/hold引用则延长。180天后可删除archive payload，但保留1年的migration tombstone、source digest和disposition count；
- v2 source backup/migration proof作为irreplaceable migration evidence至少保留到所有deployment完成cutover加180天；若它是解决migration conflict的唯一证据，则随该effect hold保留；
- 所有期限由versioned Product Workflow retention schema选择，允许部署在hard范围内延长，不允许Runtime、Temporal handler或extension缩短；absolute instant使用versioned UTC clock，不用mtime。

被拒方案：永久保存完整command会无界积累敏感数据；只留90天不给tombstone会破坏幂等/调查；给IN_DOUBT设置自动到期会删除未知外部动作证据；把旧archive永久保存会把迁移残渣变成新债务。

### 32.4 D03：Product决定处置，Workflow fenced owner执行不可逆变更

scoped instance：`D03-workflow-effect-disposition-and-purge-authority-v1`，状态`CONFIRMED`。

确认决定：

- 正常retention compact/purge由唯一Product Workflow maintenance generation发起typed command；`orchestration/workflows` current fenced store owner在expected revision下重新核验terminal lifecycle、retention instant、provider/effect/delivery/compensation settlement、Artifact pins和legal hold后执行；
- Product maintenance只能执行已满足政策的机械清理，不能把IN_DOUBT改为成功/失败、不能解除hold、不能重新执行effect；
- `OWNER_ACTION_REQUIRED`由Product批准的Workflow operator authority处理。允许的typed disposition封闭为：`CONFIRM_SUCCEEDED(receipt/evidence)`、`CONFIRM_FAILED(evidence)`、`KEEP_IN_DOUBT`、`AUTHORIZE_NEW_COMPENSATION(definition)`和`REBIND_NEVER_STARTED_LEGACY(command contract)`；不存在`retry_anyway`或`force_success`裸布尔；
- operator decision绑定actor/authority generation、reason、EffectId/current revision、evidence identities、decision timestamp和audit receipt。extension、activity handler、Temporal worker、presentation/UI不能代签；UI只提交command并显示canonical receipt；
- legal hold由独立Product governance authority设置/解除，优先于TTL和用户普通删除。maintenance及Workflow owner均不能绕过；
- security clear由独立security authority发起，最多对允许字段做cryptographic erase/redaction并保留secret-safe tombstone/receipt；若目标内容是唯一reconciliation evidence，必须先提供等价受控evidence或保持加密hold，不能直接抹除后宣称已结算；
- 当前不提供普通用户逐条物理删除Workflow effect history的产品API。用户删除Workflow run只能发起scope deletion request，逐effect返回`PURGED / COMPACTED / RETAINED_IN_DOUBT / RETAINED_HOLD / RETAINED_REFERENCE`，不能把目录删除当成功；
- test fixture可以删除自己创建的临时store，但不得调用或仿冒生产authority；migration source cleanup使用独立`MIGRATION_RETIREMENT` command和receipt；
- 每个不可逆阶段前后复核fence/revision/hold/pin。若payload已删但receipt commit失败，保留before digest和deletion-stage evidence并进入typed cleanup IN_DOUBT；不能重复删除后伪造原子成功；
- purge/compact receipt不复制被删敏感内容，只记录command identity、authority、target/revision、retention class、settlement closure digest、before/after digest、deleted reference identities和结果。

被拒方案：Workflow Runtime自行TTL删除越过Product政策；operator直接编辑JSON形成第二writer；万能force命令混淆未知effect、安全清除和普通retention；用户删除run时递归删目录会破坏provider/effect证据。

### 32.5 v3 activation门禁

`R-W3-WORKFLOW-EFFECT-002/003`只有同时满足以下条件才可进入生产writer改动：

1. v3 contract、strict codec、state transition和identity preimage已由`R-W3-WORKFLOW-EFFECT-001`冻结；
2. v2 reconciliation与旧RunJournal的完整inventory、fixture及逐state migration期望已登记；
3. migration candidate/rollback/forward recovery、source backup和receipt schema通过partial-write/torn-tail/corruption测试；
4. D02 retention、D03 authority与D07 bounds写入Product Workflow schema及typed command contract；
5. provider handler inventory已分类，无法证明idempotency/query者均为NON_REPLAYABLE；
6. Product composition确保migration在Temporal worker、Workflow reconciler和effect admission启动前完成，失败时三者均未激活；
7. production-capable recipe和active-store inventory证明同一deployment不存在v2/v3双writer、旧RunJournal effect writer或第二cleanup入口。

### 32.6 修订后的实施顺序

1. `R-W3-WORKFLOW-EFFECT-001`：冻结v3 contract和capability gate。
2. `R-W3-WORKFLOW-MIGRATION-001`：实现离线v2/RunJournal inventory、candidate converter与dry-run验证；只读，不激活v3 writer。
3. `R-W3-WORKFLOW-MIGRATION-002`：交付atomic cutover、receipt和activation barrier；与001使用同一schema writer串行评审。
4. `R-W3-WORKFLOW-EFFECT-003`：启用v3 store/reconciler、retention command和owner-action command。
5. `R-W3-WORKFLOW-TEMPORAL-001`：移除TemporalBackend RunJournal并接入v3 command/evidence。
6. `R-W3-WORKFLOW-DELIVERY-001`：接入destination canonical delivery。
7. 完成所有deployment migration和证据窗口后，再单独执行migration-only decoder/source retirement；不夹在功能切换PR中。

至此，Workflow effect工作流的产品决定、owner、migration、retention、authority、bounds和主DAG已提前闭合；后续独立需求仍需精确write set与机械验收，但不应再要求实施者选择旧数据或未知effect的处置方式。
## 33. 第十批已确认评审结论：Cron migration、occurrence retention与处置authority

本节承接`D08-cron-trusted-local-store-v1`，关闭B28剩余的D01/D02/D03/D07阻断。源码事实是：`orchestration/automation/cron/store.py`当前以`mote.cron-schedule/v2`单文件快照共同保存task与occurrence，已有revision、原子替换和scheduler fence；但TaskId只有8位十六进制、terminal occurrence没有terminal instant与回收入口、删除task直接移除identity、`IN_DOUBT`没有正式处置面，scheduler仍以mtime决定是否重读。这些行为不能留给实施阶段临场解释。

### 33.1 D01：v2到v3是保留事实的一次性迁移，不是兼容双读

scoped instance：`D01-cron-schedule-v2-to-v3`，状态`CONFIRMED`。

确认决定：

- `orchestration/automation/cron`继续拥有schedule、task identity、occurrence和迁移；Product composition只触发唯一activation/migration入口。CLI、scheduler和filesystem adapter均不得各自升级文件；
- v3在一个严格versioned envelope中保存active tasks、task tombstones、occurrences、schedule revision和migration provenance。它们仍是一条transactional truth，不拆成会独立提交的task文件与occurrence日志；若未来规模要求换backend，只替换同一owner下的原子store实现；
- 新建durable task使用至少128-bit不可复用identity。迁移中的8-hex v2 id确定性映射为带`legacy-v2` namespace的v3 identity，映射进入migration receipt；不得继续把短id放入新identity空间，也不得因显示方便截断后作为command key；
- occurrence identity保留原始`task revision + scheduled absolute instant`语义，但v3显式绑定schedule identity、task generation和clock identity。迁移不得仅重算字符串后丢失原id；旧id作为provenance保存，所有引用在同一次candidate构建中重写并验证一一对应；
- v2的`ACCEPTED`、`REJECTED`保留原disposition和receipt/reason，补入`terminal_at = migration instant`以及`terminal_time_provenance = MIGRATION_LOWER_BOUND`，不能伪造真实处理时刻；它们的retention从migration成功instant起算；
- v2的`INTENT_COMMITTED`、`DEFERRED`保留为未结算状态及原next-attempt；v2的`DISPATCHING`表示外部结果未知，统一迁为`IN_DOUBT`，不得重新dispatch；原`IN_DOUBT`原样保留。所有未结算记录不启动retention clock；
- v2 task缺少新generation时，以迁移生成的generation 1保留；task与active occurrence revision必须严格匹配。孤儿occurrence、重复task/id、重复occurrence、unknown state、非法clock、错误primitive或引用冲突使整次迁移fail closed，不猜测、不丢弃；
- migration先取得store command lock并验证scheduler未active，再执行`strict read v2 -> complete v3 candidate -> flush/fsync temporary -> atomic replace -> parent fsync -> strict read-back`。任何失败保持v2源文件不变且Cron activation失败；
- migration receipt至少包含source/target schema、source/target digest、schedule identity/revision、task/occurrence/tombstone计数、各state计数、identity mapping digest、migration implementation identity和起止AbsoluteInstant；source proof按33.2保存；
- v3首次成功activation是forward-only边界：生产reader/writer只支持v3。v2 decoder仅位于migration命令且有退出门禁；禁止在普通load中保留v2 fallback、备份回切或双写。

被拒方案：继续使用8位随机id会让长期创建/删除产生碰撞和ABA；把terminal occurrence在迁移时清空会丢失已发生delivery证据；把`DISPATCHING`改回可重试会重复投递；普通reader永久兼容v2会形成第二路径。

### 33.2 D02：active truth、terminal payload、tombstone与migration proof分级保留

scoped instance：`D02-cron-occurrence-retention-v1`，状态`CONFIRMED`。

确认决定：

- active task以及`INTENT_COMMITTED / DEFERRED / DISPATCHING / IN_DOUBT / OWNER_ACTION_REQUIRED` occurrence属于canonical active truth，不受普通TTL清除；删除task也不能带走其未结算occurrence；
- `ACCEPTED / REJECTED / CONFIRMED_FAILED`等已知terminal occurrence的完整payload、receipt reference、reason和审计字段，自可信terminal instant起默认保留30天；存在legal hold、security investigation或下游delivery/effect未结算引用时暂停时钟；
- 完整payload到期后压缩为最小occurrence tombstone并保留180天。tombstone至少保存schedule/task/generation/occurrence identity、scheduled instant、terminal disposition、terminal revision、receipt或destination delivery identity digest、purge generation和来源；它足以拒绝同identity重放，但不保存prompt正文；
- task被用户删除、one-shot被accept后自动移除或age policy过期时，生成task tombstone并保留180天；TaskId永久不重新分配。tombstone保存identity、generation、删除disposition、last revision和删除receipt，不得靠“当前tasks里找不到”表示从未存在；
- `IN_DOUBT`和`OWNER_ACTION_REQUIRED`没有自动过期。经33.3的typed处置变成terminal后才启动retention；`KEEP_IN_DOUBT`不推进时钟；
- v2->v3 migration source proof及identity mapping至少保留所有已部署实例成功cutover后180天；无法证明全体cutover时不得仅因本机时间到期删除migration decoder/source proof；
- prompt/receipt超过inline上限时使用canonical ArtifactRef，并将ownership edge与上述lifecycle绑定。payload压缩前必须确认下游引用已结算并以同一事务把必要digest写入tombstone；
- retention由Product versioned Cron policy选择，extension只能收窄删除（延长保存或加hold），不能提前清除。30/180天是当前默认，不与Event DLQ、Workflow effect或Artifact全局TTL合并。

### 33.3 D03：删除与未知结果处置必须是typed command

scoped instance：`D03-cron-occurrence-disposition-and-purge-authority-v1`，状态`CONFIRMED`。

确认决定：

- 用户/CLI删除schedule task调用Product授权的`CronTaskCommand`；`orchestration/automation/cron`的current revision owner执行并返回typed receipt。若存在未结算occurrence，命令返回`BLOCKED_BY_UNSETTLED_OCCURRENCE`，不得级联删除或把未知投递当取消成功；
- one-shot accepted和age expiry是不同typed system commands/dispositions，均由current fenced scheduler提交；它们不得伪装成用户删除。scheduler fence只授权调度transition，不自动获得人工处置、legal hold或security clear权；
- `IN_DOUBT / OWNER_ACTION_REQUIRED`只允许Product Cron operator authority发出以下封闭命令：`CONFIRM_ACCEPTED(receipt/delivery evidence)`、`CONFIRM_REJECTED(evidence)`、`KEEP_IN_DOUBT(reason)`、`CANCEL_IF_PROVEN_NOT_DISPATCHED(evidence)`、`CREATE_NEW_OCCURRENCE`。最后一项创建全新occurrence identity和明确的supersedes edge，绝不重开旧attempt；
- 不提供`retry_anyway`、`force_accepted`或删除未知record。无可信证据时只能保持未知；operator command绑定expected revision、authority identity、reason、evidence digest与审计receipt；
- TTL maintenance由Product Cron maintenance authority发起有界purge command；只有Cron store current command owner能在expected revision下把完整terminal记录压缩为tombstone或清除到期tombstone。文件扫描、CLI直接改JSON、scheduler tick和通用Artifact GC都没有该authority；
- legal hold的设置/解除由Product governance authority发起，Cron owner只执行并记录；security clear是独立高权限command与审计语义，可以缩短内容保留但仍保留法律/安全允许的最小清除证明，不能复用普通TTL；
- purge每个不可逆阶段重新校验record terminal、retention instant、hold、下游receipt/artifact edge、schedule revision和owner fence/generation。CAS失败返回typed conflict并留待下轮，不以旧snapshot继续删除；
- corrupt store不自动quarantine后创建空schedule，也不允许maintenance绕过。activation和所有mutation fail closed，进入显式offline repair/restore需求；当前评审不授权丢弃损坏用户数据。

### 33.4 D07：Cron容量、重试、扫描与时间边界固定

scoped instance：`D07-cron-schedule-and-occurrence-bounds-v1`，状态`CONFIRMED`。

- active durable tasks默认上限50，Product schema hard max 10,000；达到上限返回typed `CAPACITY_EXHAUSTED`且不写task。session-local task另计，默认50、hard max 1,000，进程退出即失效；
- 同一task同时最多一个未结算occurrence；全store未结算occurrence默认上限1,000、hard max 10,000。容量不足时不提交新occurrence并返回typed backpressure；不得驱逐已提交intent或无限park；
- dispatch在durable intent后进行。`DEFERRED`最多8次dispatch attempt，backoff按现有1/2/4/8/16/32/60/60秒并使用persisted `next_eligible_at`；异常、timeout、fence loss或无法证明未交付的crash直接`IN_DOUBT`，不消耗剩余次数继续dispatch；
- scheduler每次tick最多claim 100个eligible occurrence、最多创建100个新due occurrence；poison、IN_DOUBT和未到期DEFERRED不能阻塞后续eligible task。一次reconcile wall-time预算5秒，超出后让出至下一tick；
- maintenance每批最多500个record、每事务最多100个、wall-time最多5秒；单个失败隔离为typed item result，不删除其他不满足条件的record；
- prompt/trigger inline正文最大1 MiB，receipt/reason inline合计最大64 KiB，超过走canonical ArtifactRef；不截断后声称完整。单个schedule快照默认软上限64 MiB、Product hard max256 MiB，超过返回backpressure并要求maintenance/backend migration，不能继续无界重写；
- cron表达式仍为5-field profile，下一次发生时间搜索horizon 366天；timezone必须是activation批准的IANA zone identity。persistent instant使用`AbsoluteInstant + clock identity`，进程内tick/lock elapsed使用monotonic clock；不得持久化epoch-ms而不声明clock；
- DST维持已实现的`EARLIEST_FOLD_SKIP_GAP`：fold只触发较早一次，gap跳过不存在的local time；misfire只`FIRE_ONCE`，overlap只`FORBID`。这些是closed current-generation policy，不用任意字符串或callback扩展；
- scheduler每个tick都读取或比较canonical schedule revision；mtime只能作为诊断/性能提示，不能决定跳过authoritative revision检查。删除现有“mtime未变即不load”的控制语义和external hot-reload说明。

数值由versioned Product Cron policy选定，Runtime、CLI和extension不得提高。未来需要超过hard max时先提供同owner backend与负载证据，不能只调常量。

### 33.5 解除阻断、剩余依赖与独立实施顺序

本轮解除B28的schema migration、短identity ABA、terminal growth、unknown disposition、task/occurrence deletion authority、重试和扫描上限阻断。结合D08，Cron的trusted-local threat model、唯一command面、migration、retention、authority、failure和bounds已足够形成可实施需求，不再有需要实施者选择的产品架构决定。

仍存在的真实依赖不是开放决定：Cron dispatch到Agent时必须接入已确认的canonical Agent delivery command并取得stable delivery receipt；因此Cron store/codec可先开工，但新的dispatch activation必须等待Agent delivery contract切片。ArtifactRef大payload依赖canonical Artifact ownership edge；未完成前可用1 MiB fail-closed上限，不能自建Cron blob目录。

独立requirements及顺序：

1. `R-W3-CRON-001`：定义v3 typed TaskId/generation、occurrence/disposition/tombstone、strict codec、policy和migration receipt contract。
2. `R-W3-CRON-002`：实现并fault-injection验证v2->v3 forward-only migration；依赖001，新v3 writer activation前完成。
3. `R-W3-CRON-003`：收敛Cron command/query owner、typed deletion/operator commands及retention maintenance；依赖001/002。
4. `R-W3-CRON-004`：迁移scheduler到revision-driven reconcile、bounded claim与v3 fenced transition，删除mtime hot reload和包外store/scheduler访问；依赖003。
5. `R-W3-CRON-DELIVERY-001`：把Cron trigger接入Agent canonical delivery Port并保存stable receipt；依赖004与Agent delivery contract，不与Cron migration强制串行。
6. `R-W3-CRON-ARTIFACT-001`：接入大payload/evidence ArtifactRef ownership edge；依赖Artifact canonical edge，未交付时保持inline fail-closed。
7. 最后删除v2 migration decoder/source、旧8位identity构造和mtime控制测试；删除条件由migration deployment evidence门禁证明，而不是人工宣称完成。

`R-W3-CRON-001`是本workstream唯一首节点；002之后003/Artifact contract准备可并行，004只等待canonical command/store，delivery integration等待Agent delivery。不得把七项重新合成一个B28巨型改动。

## 34. 第十一批已确认评审结论：Agent delivery、mailbox projection与turn acceptance闭环

本节关闭B19、B23、B27在D01/D02/D03/D07以及跨store崩溃原子性上的剩余阻断。源码已有三个各自严格但不能共同证明端到端不变量的v1事实面：`mote.agent-delivery-store/v1`、Residency record内的`mote.agent-mailbox/v1` snapshot，以及`mote.agent-turn-queue/v1`。当前scheduler先`drain_for_processing()`移除mailbox item，再写turn queue；执行完成后先settle turn再逐delivery ack。两个阶段都跨独立提交，进程内restore closure和`PendingDeliveryQueue`不能承担崩溃恢复。

### 34.1 canonical ownership不合并成万能消息队列

scoped owner决定：`D19-agent-ingress-owner-separation-v1`，状态`CONFIRMED`。

- `orchestration/agents/messaging`唯一拥有delivery intent、payload/reference、target logical identity/generation、delivery lifecycle、turn-assignment edge、ack/dead-letter和delivery retention；
- `orchestration/agents/turn_queue`唯一拥有TurnRequest、delivery batch的有序引用、capacity reservation、enqueue sequence、WDRR scheduling state、claim/execution permit、retry和turn terminal settlement；
- per-incarnation `Mailbox`只是delivery owner面向resident Runtime的可丢弃有序projection/cache，不是第三份delivery truth。Residency可以保存projection cursor以减少重扫，但不能保存有独立ack/重放语义的完整canonical mailbox；
- `PendingDeliveryQueue`降级为有界、可重建的进程内wake/scan hint。durable accept成功与否只由delivery store receipt决定；队列满、进程退出或`drop()`不能丢delivery，也不能把durable ACCEPTED改为失败；
- Runtime message buffer只接收已经绑定到current TurnRequest的immutable input projection。它不持有delivery ack权，不通过保存`Message`对象证明turn已接受；
- delivery owner和turn owner保持两个bounded context，但通过最小typed prepare/commit/query Ports协作，不允许Control、scheduler或Product直接同时修改两个store内部record。

拒绝把三个store物理合并为“AgentExecutionDB”：delivery可能存在而尚不触发turn，turn queue还拥有不属于payload delivery的公平调度与execution permit不变量；强行统一会形成巨型状态机。也拒绝维持当前best-effort拼接，因为它无法证明一个delivery最多属于一个accepted turn。

### 34.2 delivery v2与turn queue v2的可恢复acceptance protocol

scoped instance：`D23-agent-delivery-turn-atomic-acceptance-v1`，状态`CONFIRMED`。

确认以下唯一协议；`drain -> accept -> restore on failure`退出生产正确性路径：

1. delivery owner按target identity/generation和durable delivery sequence读取eligible delivery references，不从Mailbox取canonical batch。`QUEUE_ONLY`只有在同target另有合法trigger或显式Product command时可与batch一起选择，不能单独产生turn；
2. scheduler确定性计算`TurnRequestId`，preimage至少绑定queue identity、target AgentId/lifecycle generation、ordered DeliveryId tuple、每项payload digest/mode、batch policy generation和request schema generation；同id不同preimage返回typed conflict；
3. turn owner执行`PREPARE_ACCEPTANCE`：在同一turn-queue事务中原子检查capacity、delivery tuple未被其他本地request使用、deadline/config，并预留capacity与enqueue sequence，写入`PREPARED` item和transaction id。PREPARED不是公开ACCEPTED、不可claim，但占用容量，防止并发超卖；
4. delivery owner执行`BIND_TO_TURN`：在一个delivery-store事务中以expected revisions验证全部delivery仍eligible、target generation相同、没有绑定其他request，然后原子写全部assignment edge。全有或全无；逐条bind循环不合格；
5. turn owner执行`COMMIT_ACCEPTANCE`：验证自身prepare及delivery owner返回的binding receipt identity/digest，把同一item推进ACCEPTED。只有此commit完成才向调用者返回durable `ACCEPTED`；
6. crash reconciler以transaction id双向query。若turn PREPARED而delivery未绑定，在有界prepare lease过期后abort并释放capacity；若delivery已绑定，则只能完成同一TurnRequest commit或进入`ACCEPTANCE_IN_DOUBT`，不得另建request；若delivery已绑定但turn prepare不存在，delivery owner保持`OWNER_ACTION_REQUIRED`并禁止重绑，除非严格证明bind事务从未对外commit；
7. commit后Mailbox projection和wake signal才可生成。projection丢失可从delivery assignment + accepted turn重建；旧incarnation/generation不能投影、claim或ack；
8. turn claim绑定queue revision、scheduler fence、target incarnation、process instance与execution capacity permit。claim前必须验证delivery assignment receipt仍对应同TurnRequest；scan/rehydrate不等于执行权；
9. turn执行terminal commit与delivery ack使用第二个可恢复settlement transaction。turn owner先写`EXECUTION_SETTLEMENT_PREPARED`及outcome/result digest，delivery owner原子ack整个batch并返回receipt，turn owner再提交SUCCEEDED/FAILED terminal。若执行可能完成而结果未提交，进入`EXECUTION_IN_DOUBT`，不得自动重跑；
10. delivery ack只表示该delivery已经被canonical turn结算消费，不表示turn成功。失败turn是否重试由同一TurnRequest attempt状态机决定；retry沿用assignment和request identity，不把delivery退回未分配队列。达到attempt上限后turn terminal失败，delivery以该失败receipt结算ack；
11. deadline/cancel与prepare/commit/claim均通过expected revision竞争唯一状态。未commit PREPARED可取消并解绑；已ACCEPTED不得因deadline回退delivery，未claim过期以typed EXPIRED结算整个batch；已claim超时由execution settlement处理；
12. 任一store不可用、corrupt、fsync失败或owner fence不current时fail closed。wake、Mailbox、Residency snapshot和进程内rollback不得推进canonical transition。

这不是分布式ACID伪装，而是有唯一transaction identity、durable prepare、幂等commit、明确in-doubt与reconciler的跨owner协议。实现可以在同一可靠数据库事务中优化，但contract不能依赖当前两个文件恰好位于同一磁盘。

### 34.3 D01：三个v1来源一次性迁移并对账到delivery/turn v2

scoped instances：`D01-agent-delivery-v1-to-v2`、`D01-agent-turn-queue-v1-to-v2`、`D01-agent-mailbox-projection-cutover-v1`，状态均为`CONFIRMED`。

迁移由Orchestration Agent governance activation在所有delivery admission、Residency rehydrate、turn scheduler启动前取得独占cutover generation后执行。Product只装配并触发入口，不逐文件迁移。

- inventory同时读取delivery v1、全部approved Residency v1 mailbox snapshot、turn queue v1、lineage与residency generation facts；先全量严格decode并计算source digest，再生成candidate。任何unknown schema、重复identity、错误primitive、非法state、同DeliveryId不同payload或target、无法区分的中段损坏使整个scope fail closed；
- delivery v1 identity保留为legacy identity，不重新hash后冒充同一事实。v2补齐source generation、payload digest、accepted instant provenance、assignment和terminal字段；v1 target_generation为0时，只有lineage/residency能唯一证明接收generation才可绑定，否则迁为`OWNER_ACTION_REQUIRED(LEGACY_TARGET_GENERATION_UNKNOWN)`；
- v1 ACKED/DEAD_LETTER保留terminal disposition，因缺少可信terminal instant使用migration instant作为retention lower bound并标provenance；v1 ACCEPTED保留eligible；v1 CLAIMED不能证明是否已投影/执行，迁为`DELIVERY_IN_DOUBT`，不得退回ACCEPTED；
- Residency mailbox中能与唯一delivery v1 record按DeliveryId、message digest、target和generation严格匹配的item只转为v2 projection cursor/evidence，不复制payload；若delivery store没有对应record，但Residency record是current fenced、严格合法且message identity唯一，则创建`LEGACY_IMPORTED` canonical delivery并保留source digest，默认进入`OWNER_ACTION_REQUIRED`，由reconciler确定是否可投影，不能静默丢弃；
- 同一mailbox item与delivery record发生payload/target/generation冲突时保持双方source evidence并阻断该Agent activation，不按mtime或“较新的文件”选择；stale Residency incarnation中的mailbox只作为migration evidence，不向current generation投影；
- turn queue v1 terminal item保留terminal事实；v1 ACCEPTED若其全部DeliveryId存在且未绑定其他request，则在candidate中原子补建assignment edge并成为v2 ACCEPTED；任一delivery缺失/冲突则转`ACCEPTANCE_IN_DOUBT`，不可claim；
- v1 CLAIMED可能已经执行，统一迁为`EXECUTION_IN_DOUBT`并释放旧process-local permit的执行权，但保留claim/attempt evidence；绝不回到ACCEPTED自动重跑。stale scheduler/process fence只证明旧owner不能提交，不证明执行未发生；
- 若同一DeliveryId出现在多个v1 TurnRequest，相关requests与delivery全部进入migration conflict，不能按enqueue sequence任选一个；若DeliveryId既已ACKED又只对应一个terminal turn，可保留两者并补建settlement evidence edge；矛盾terminal disposition阻断；
- candidate必须验证：每个nonterminal delivery最多一个assignment、每个accepted/nonterminal turn的delivery tuple完整且有序、target/generation一致、每个delivery最多一个accepted turn、terminal turn/ack关系无矛盾、capacity包含PREPARED与ACCEPTED、sequence/revision单调；
- 写入使用`complete candidates + cross-store manifest -> fsync -> generation activation record -> parent fsync`。若backend无法提供原子generation publication，则先写inactive content-addressed candidates，再以单一activation pointer切换；不能依次覆盖三个active文件造成mixed generation；
- migration receipt记录每个source/target digest、各state/disposition数量、legacy mailbox import/conflict数、cross-store invariant digest、cutover generation和implementation identity。首次v2 activation后生产只读写v2；v1 decoder进入migration-only路径并按deployment evidence退出。

该迁移明确保留旧用户消息，不授权清空Mailbox、CLAIMED delivery或CLAIMED turn。真正损坏或冲突需要offline repair/owner action，不能为了开机假设未执行。

### 34.4 D02：delivery、turn和projection分别保留，不让Mailbox决定TTL

scoped instances：`D02-agent-delivery-retention-v1`、`D02-agent-turn-retention-v1`，状态`CONFIRMED`。

- delivery在`ELIGIBLE / BOUND_TO_TURN / DELIVERY_IN_DOUBT / OWNER_ACTION_REQUIRED`时属于active truth，不自动过期；只有ACKED、DEAD_LETTER或经authority确认的terminal disposition启动retention；
- terminal delivery完整payload默认保留30天；随后压缩为最小delivery tombstone并保留1年。tombstone保存DeliveryId、source/target Agent与lifecycle generation、mode、payload digest、assignment/TurnRequestId、terminal disposition/revision/instant、ack/dead-letter receipt digest和migration provenance；
- terminal turn的完整input references、claim/attempt、outcome/result reference和settlement receipts默认保留90天；随后压缩为最小TurnRequest tombstone并保留1年，至少保存identity preimage digest、ordered delivery digest、target/root/subtree、enqueue/config generation、attempt count、terminal disposition/time/revision与delivery settlement digest；
- `DELIVERY_IN_DOUBT / ACCEPTANCE_IN_DOUBT / EXECUTION_IN_DOUBT / OWNER_ACTION_REQUIRED`不启动retention。legal hold、未结算effect/result Artifact、appeal/audit和subtree cancellation settlement均暂停compact/purge；
- Mailbox projection、wake hint和PendingDeliveryQueue没有独立retention。projection在current generation失效、turn commit/terminal或Residency retirement后可直接重建/丢弃，但丢projection绝不能删canonical delivery；
- 大payload/result复用canonical ArtifactRef并登记ownership edge。delivery/turn compact前原子验证edge已结算或把必要reference digest带入tombstone；不复制blob到messaging/turn目录；
- migration source/cross-store proof至少保留所有approved deployment完成cutover后180天；若是解决in-doubt/conflict的唯一证据则随该record hold；
- Product Agent governance retention schema可延长期限或加hold，Runtime/extension不能缩短。delivery 30天与turn 90天反映不同争议/恢复周期，不为“统一TTL”强行相同。

### 34.5 D03：terminal、dead-letter、取消和物理删除authority

scoped instance：`D03-agent-delivery-turn-disposition-and-purge-authority-v1`，状态`CONFIRMED`。

- 正常delivery ack由第34.2节settlement protocol中的current delivery owner执行；turn terminal由current fenced turn owner执行。scheduler、Runtime、Mailbox和Product UI不能直接改record；
- target logical Agent terminal时，supervisor发出typed target/subtree cancellation command。delivery owner逐delivery结算为`TARGET_TERMINAL`或保持IN_DOUBT，turn owner逐request结算cancel/claimed execution；禁止`dead_letter_target()`扫描后无逐项receipt地批量覆盖，也禁止`PendingDeliveryQueue.drop()`代表canonical删除；
- delivery dead-letter只用于目标永久不存在、schema永久不支持、已达有界投递失败且能证明没有执行等封闭原因；capacity不足是BACKPRESSURED，不是dead-letter。dead-letter无replay；重新发送必须创建新DeliveryId并记录supersedes edge；
- owner-action命令封闭为：`CONFIRM_NOT_EXECUTED_AND_RELEASE`、`CONFIRM_CONSUMED(turn/evidence)`、`CONFIRM_TURN_SUCCEEDED(result evidence)`、`CONFIRM_TURN_FAILED(evidence)`、`KEEP_IN_DOUBT`、`CREATE_SUPERSEDING_DELIVERY`。不存在`retry_anyway`、`force_ack`或删除未知状态；
- 普通用户取消只影响尚未claim的TurnRequest或尚未绑定的delivery；已claim/可能执行的请求进入cancellation settlement/IN_DOUBT，不能因为用户点击取消就ack输入或删除结果证据；
- Product Agent maintenance authority发起retention compact/purge command，两个canonical owner分别在expected revision/fence下执行并交换typed reference-closure receipt。Artifact GC、Residency cleanup、Session delete和filesystem scan都不能级联删delivery/turn；
- legal hold由独立governance authority设置/解除；security clear可以在保留identity/digest/receipt的前提下提前擦除允许的plaintext，但不能删除唯一execution或用户输入证据后宣称已结算；
- 每个不可逆阶段复核target lifecycle generation、record revision、assignment/settlement edge、hold、Artifact pin和owner fence。partial purge进入typed cleanup IN_DOUBT并保留stage evidence；
- 用户删除Session/Agent返回逐类typed结果：`PURGED / COMPACTED / RETAINED_ACTIVE / RETAINED_IN_DOUBT / RETAINED_HOLD / RETAINED_REFERENCE`。AgentId、DeliveryId和TurnRequestId不复用，删除内容不等于删除identity fact。

### 34.6 D07：有界admission、payload、batch、prepare和reconcile

scoped instance：`D07-agent-delivery-turn-bounds-v1`，状态`CONFIRMED`。

- canonical pending delivery容量按root和target同时限制：每target默认1,000、hard max10,000；每root默认10,000、hard max100,000；全deployment由Product另设有限hard bound。容量必须在durable accept事务中原子检查，满时返回typed BACKPRESSURED/REJECTED且不写ACCEPTED；
- 单delivery inline payload最大1 MiB，超过使用ArtifactRef；单turn batch最多100个delivery、解码后inline输入合计最大4 MiB，超过拆分为多个有序turn或使用ArtifactRef，不能截断；
- turn active capacity沿现有Product governance配置，但必须同时计入PREPARED、ACCEPTED、CLAIMED、settlement-prepared和IN_DOUBT；默认每queue1,000、hard max100,000。terminal records不占active capacity但受store size/maintenance约束；
- acceptance prepare lease为30秒；reconciler每批最多500个transaction、每事务最多100个delivery、wall-time最多5秒。过期prepare只有在delivery未绑定时可abort；已绑定不因超时解绑；
- delivery projection scan每target每批最多500，总批最多2,000；进程内Mailbox默认最多500项、PendingDelivery hint全进程默认5,000。projection/hint满时停止投影并依赖durable scan，不能扩大canonical acceptance或丢弃；
- turn execution最多3个attempt，backoff 1/5/30秒；只有typed settlement证明attempt未产生不可重复外部结果时才自动retry。worker crash、permit/fence丢失或terminal commit未知进入EXECUTION_IN_DOUBT；
- turn scheduler继续使用已确认的两级WDRR、eligible cost=1、root/subtree有限公平性。batch大小不改变调度cost，也不能用100条消息的batch规避root公平；Product可把batch policy收窄；
- deadline只作用于未claim item；durable acceptance前capacity admission，queue full不写accepted；accepted item不驱逐。retry的`next_eligible_at`是AbsoluteInstant，进程内等待用monotonic；
- delivery/turn maintenance各批最多500、每事务最多100、5秒预算；单项poison/conflict不阻塞后续eligible item。canonical store软上限各256 MiB、hard max1 GiB，达到软限触发backpressure/maintenance，达到hard限停止新accept且保留query/settlement能力；
- target terminal/subtree cancellation每批最多500个identity并使用fenced stable snapshot+cancellation epoch；新spawn/delivery admission与snapshot generation协调，不能无限扫描live map；
- 所有上限属于versioned Product Agent governance schema，extension只能收窄。删除“同步accept永远成功”“parking永不失败”“无限等待容量”等文案与测试；durable acceptance和typed backpressure才是真实承诺。

### 34.7 解除阻断、准入门禁和实施DAG

本轮已经关闭B19的三层消息概念混合、B23的capacity/claim/settlement原子性以及B27的mailbox drain/turn accept/ack crash窗口。结合已确认`D18-agent-turn-input-via-delivery-v1`，Agent ingress不再有需要实施者选择的migration、retention、authority、failure或bounds产品决定。

生产activation前必须机械证明：

1. 全部业务Message入口调用delivery command，wake只携带identity；无裸`notify(Message)`、`Mailbox.enqueue_communication()`或直接PendingDelivery生产accept入口；
2. v2 cross-store generation manifest保证delivery/turn/residency projection不会mixed-generation启动；
3. fault injection覆盖turn prepare前后、delivery batch bind前后、accept commit前后、execution前后、delivery ack前后、turn terminal commit前后、fence loss和fsync失败；
4. 每个crash point恢复后，一个DeliveryId最多属于一个TurnRequest、每个公开ACCEPTED turn都有完整binding、stale generation无提交权；
5. capacity/backpressure、WDRR公平、deadline/cancel CAS、prepare expiry、retention/hold和Artifact edge均有deterministic fake-clock/规模fixture；
6. Mailbox/Residency/PendingDelivery删除后仍可从canonical facts恢复投影，且projection不能反向覆盖delivery state。

独立requirements与顺序：

1. `R-W3-AGENT-INGRESS-001`：冻结delivery v2、turn v2、transaction/assignment/settlement、projection cursor和strict codec contract。
2. `R-W3-AGENT-INGRESS-MIGRATION-001`：实现三个v1 source inventory、cross-store dry-run和conflict report；依赖001，只读不激活writer。
3. `R-W3-AGENT-INGRESS-MIGRATION-002`：实现inactive candidates、generation manifest与forward-only cutover；依赖前两项。
4. `R-W3-AGENT-DELIVERY-001`：实现delivery command/query、atomic batch bind/ack、retention与owner-action；依赖migration contract/cutover。
5. `R-W3-AGENT-TURN-001`：实现PREPARED/ACCEPTED/settlement-prepared、capacity reservation、claim/retry/cancel；依赖001/003，可与delivery内部实现并行但共同协议由001唯一writer控制。
6. `R-W3-AGENT-INGRESS-RECONCILE-001`：实现跨owneracceptance/settlement reconciler和fault injection；依赖004/005。
7. `R-W3-AGENT-PROJECTION-001`：把Mailbox、Residency mailbox snapshot和PendingDeliveryQueue降级为有界可重建projection，迁移所有入口；依赖006。
8. `R-W3-AGENT-INGRESS-SURFACES-001`：迁移Product、Cron、Workflow terminal和Agent communication入口到canonical delivery Port；依赖004/006，各surface adapter可分单并行。
9. 所有deployment cutover及证据窗口满足后，删除v1 decoder、旧mailbox payload snapshot、裸enqueue/notify和migration source；不得夹入首次activation切片。

首节点只有001；migration必须先于新writer activation；delivery和turn实现共享protocol但不共享内部store owner；reconciler完成后才能删除projection正确性路径。Cron delivery、Workflow terminal delivery等workstream现在获得了明确依赖目标，不再等待一个抽象的“未来Agent消息系统”。

## 35. 第十二批已确认评审结论：OAuth credential identity、backend cutover与撤销生命周期

本节关闭B21剩余的D01/D02/D03/D07阻断。当前实现已有hashed subject、record v1、revision/token_generation、本地0600文件、keyring backend和一次性backend selector，这些方向可以复用；但`token=None`无法区分从未存在、本地删除、远端撤销和材料丢失，keyring的`get_password -> set_password`不是backend原生CAS，`fallback`首次选择没有对两个backend做冲突inventory，OAuth自有JSON还持久化明文bearer/refresh token与宽松`raw` claims。

### 35.1 canonical owner与secret material分离

scoped instance：`D21-oauth-credential-owner-and-backend-v1`，状态`CONFIRMED`。

- OAuth credential lifecycle metadata由`runtime/models/auth/oauth`拥有：stable CredentialSubjectId、provider/config generation、backend binding、credential generation、state、scopes、expiry、SecretRef、revision、refresh/revocation attempt和receipt；
- access token、refresh token、client secret等敏感材料只由canonical Runtime secret/vault capability拥有。OAuth record保存opaque typed SecretRef与material digest/version，不再把plaintext token或JWT raw claims写入OAuth JSON；OAuth adapter在最小使用窗口解析外部响应并立即写vault；
- Contracts拥有跨层的opaque credential identity、state/disposition、typed command/query/receipt和最小Port，不拥有OAuth HTTP、keyring/file选择或Product默认值；
- Product composition根据versioned credential policy显式选择一个approved backend binding。Runtime执行所选adapter，但不在读写失败时fallback，不根据“哪个能用”永久漂移；
- backend selection本身属于credential metadata truth并带revision/generation。`fallback`只允许作为首次activation的Product resolver名称：它必须inventory所有候选backend；零记录时按policy选一个，一份记录时绑定该backend，多份一致/冲突记录都不得静默任选，必须进入migration/reconciliation；activation后解析结果冻结为`FILE_VAULT_V1`或`OS_KEYRING_V1`等具体binding，普通运行不再保留fallback分支；
- CredentialSubjectId由Product-approved provider integration identity、account/slot identity和config generation的稳定preimage派生，不仅依赖可变display/provider name或client_id。相同display name不同integration/account不能共享token，config rename也不能意外创建平行authority；
- provider SDK/client只获得当前credential generation的短生命周期borrow/capability，不获得store、refresh token或backend。刷新完成后新generation构造新client；旧client/borrow失效并按有界lifecycle关闭；
- inference `CredentialHealthAuthority`只拥有某credential generation能否用于attempt的健康/隔离判定，不拥有OAuth token、refresh、delete或backend selection；两者通过generation identity关联，不能互相覆盖状态。

被拒方案：继续让OAuth file store保存明文会复制SecretStore；把keyring具体类当公共contract会锁死backend；运行中keyring失败自动读file会形成双truth；仅用provider显示名派生subject会把配置重命名变成credential fork。

### 35.2 credential lifecycle是closed state，不用nullable token表达事实

credential metadata v2使用封闭状态：

- `ABSENT`只存在于query结果，表示该subject从未提交canonical record；它不是持久record；
- `ACTIVE`：当前generation有可借用SecretRef；
- `REFRESHING`：已有durable refresh intent/attempt，旧access token是否仍可用由明确policy与expiry决定；
- `REAUTH_REQUIRED`：refresh永久失败或interactive grant需要重新登录，保留identity/tombstone与必要receipt；
- `REVOCATION_PENDING`：已提交远端revocation intent但结果未知/未terminal；
- `REVOKED`：provider receipt或policy允许的local-only revocation已terminal；
- `MATERIAL_LOST`：metadata存在但vault material缺失/不可解密，不能当ABSENT后自动重新mint；
- `IN_DOUBT`和`OWNER_ACTION_REQUIRED`：refresh/revoke外部结果未知或migration/backend冲突；
- `RETIRED`：credential slot由Product配置退出且所有borrow/effect已结算。

所有transition绑定CredentialSubjectId、credential generation、record revision、Product config generation、backend binding generation和owner fence/lock generation。`token_generation`只在新secret material成功提交并与metadata原子发布后增加；失败刷新、query或健康观察不能冒充rotation。

`delete()`拆成不同typed命令：`LOCAL_LOGOUT`只撤销Mote本地使用权并删除/crypto-erase secret material；`REVOKE_AT_PROVIDER`先写durable effect intent，再调用正式revocation endpoint并保存receipt；`RETIRE_CONFIG_SLOT`处理Product配置退出；`SECURITY_CLEAR`执行受控紧急擦除。UI可以把它们显示为不同操作，不能都映射成`commit(None)`。

### 35.3 refresh与revocation遵循external-effect协议

scoped instance：`D11-oauth-refresh-and-revocation-effect-v1`，状态`CONFIRMED`。

- refresh和provider revoke均在网络动作前durable提交typed intent，identity绑定subject、旧generation、provider/endpoint、client/config generation、grant/revocation kind和canonical request digest；
- provider支持稳定idempotency key/query时按正式contract使用；多数OAuth token/revocation endpoint不保证可靠query，因此默认分类为`NON_REPLAYABLE`。连接中断、timeout、进程崩溃或远端成功后本地commit失败进入IN_DOUBT，不自动重复refresh/revoke；
- refresh response先严格验证token_type、access token、可选refresh token、scope、expires_in/absolute expiry和provider identity，再把新material写入vault的inactive generation，随后CAS发布metadata。metadata publish前新material不可借用；publish失败进入cleanup/IN_DOUBT，不能覆盖旧generation引用；
- provider未返回新refresh token时，只有provider contract明确表示“沿用旧refresh token”才能保留旧值；缺失字段不能自动解释为删除或沿用；
- refresh token rotation后，旧refresh SecretRef立即失去借用权，但在新metadata commit和必要rollback evidence闭合前保持受控pin；不允许两个generation同时作为active fallback；
- `expires_at`使用timezone-aware `AbsoluteInstant`和clock identity；`expires_in`在adapter收到响应时用同一clock转换。禁止持久化裸float epoch或以`time.time()`直接决定跨进程事实；进程内等待/timeout用monotonic；
- JWT claims只做严格、最小、非authoritative projection。`email/account/exp`不能选择credential identity、backend或授权scope；unknown claim保留为受控原始evidence Artifact/secret reference或丢弃，不进入`Dict/raw` durable schema；
- stale process即使获得token response也不能发布新generation。若response包含不可重建的rotated refresh token，允许写attempt-scoped immutable secret evidence inbox；current owner验证后CAS settlement，它不是第二active credential；
- 外部动作发生而vault/metadata/audit commit失败时保持IN_DOUBT并禁用自动credential使用。不能返回“刷新失败可再试”并丢失provider receipt。

### 35.4 D01：record v1、backend selector与候选material一次性迁移

scoped instance：`D01-oauth-credential-v1-to-v2`，状态`CONFIRMED`。

迁移在任何OAuth manager、provider client或refresh worker构造前，由Product credential activation触发Runtime canonical migration service；按CredentialSubjectId逐subject独占，但同一subject的selector、file、keyring和vault candidate必须一次对账。

- inventory读取v1 backend selector、file record、keyring record、approved legacy config refresh token来源及canonical SecretStore中已有对应entry；只读取当前Mote scope，不扫描任意系统keyring service；
- 每个v1 record严格校验subject/backend/revision/generation/token shape。file与keyring均无record时不创建v2 record，query仍为ABSENT；selector存在但record缺失迁为MATERIAL_LOST/OWNER_ACTION_REQUIRED，不能当初次登录；
- 只有一个backend有合法record时，冻结为对应v2 backend binding；selector缺失可由事实补建，selector指向另一空backend则记录migration discrepancy但以唯一material source为候选，必须产生显式receipt；
- 两个backend都有record时，比较credential identity、generation、access/refresh material digest、scope和expiry。完全一致也只选择Product policy优先backend并把另一份标为retired source，不能继续双写；任何差异进入`BACKEND_CONFLICT`并阻断该subject activation，禁止按revision、mtime、expiry或“能refresh”任选；
- v1 token不为null时，先写入canonical vault inactive generation，验证SecretRef可回读/digest一致，再写v2 ACTIVE metadata candidate；plaintext source在v2 activation和验证完成前保留，之后由独立migration retirement命令安全清除；
- v1 `token=None`迁为`REVOKED` tombstone，provenance=`LEGACY_LOCAL_DELETE`，不能迁为ABSENT。它不证明provider已撤销，因此provider revocation status为`NOT_PROVEN`，不得自动发起远端revocation；
- config中legacy refresh token与store material相同则去重并迁入同一vault generation；不同则进入CONFIG_STORE_CONFLICT。迁移不尝试网络refresh来判定哪个有效；
- v1 access token已经过期但refresh material存在时仍迁为ACTIVE/REAUTH decision所需的当前事实，activation后由正式refresh state machine处理；没有refresh且已过期迁为REAUTH_REQUIRED，不自动client_credentials mint；
- v1 claims的已知字段经严格验证后迁为non-authoritative projection；raw claims默认不迁入metadata，若审计确需保存则作为加密migration evidence并按35.5 retention；
- candidate包含v2 metadata、vault generation references、backend binding和source disposition；全部flush/fsync/secure-store commit后，以单一subject activation generation发布。任何partial failure不改变active v1读取路径，但OAuth使用保持未激活，不能一边继续用v1一边写v2；
- migration receipt记录source backend/selector digests、secret-safe material digests、各disposition、target metadata/vault generations、conflict和retired-source identity。v2首次activation后生产只读写v2，v1 decoder与fallback resolver进入migration-only路径。

此迁移不授权丢弃任何冲突credential，也不通过实际登录/refresh产生费用或外部副作用。冲突只允许后续typed owner action选择保留哪份，并把未选source安全退休。

### 35.5 D02：secret material、metadata tombstone和effect evidence分级保留

scoped instance：`D02-oauth-credential-retention-v1`，状态`CONFIRMED`。

- ACTIVE/REFRESHING/REVOCATION_PENDING/IN_DOUBT/OWNER_ACTION_REQUIRED/MATERIAL_LOST所需metadata、current SecretRef和不可替代effect evidence不受普通TTL；
- access token在过期、generation被替换且所有active borrow/inference attempt关闭后立即eligible for crypto-erase，最长保留24小时用于crash settlement；不得因metadata保留期长期保存过期bearer plaintext；
- rotated/retired refresh material在新generation发布、所有旧borrow关闭、refresh effect terminal和rollback禁用后立即crypto-erase，正常最长24小时；IN_DOUBT且它是唯一provider reconciliation evidence时保持加密pin，直到typed settlement；
- REVOKED/RETIRED/REAUTH_REQUIRED完整非secret metadata、effect receipts和migration provenance默认保留90天；随后compact为最小credential tombstone并保留1年。tombstone至少保存subject/provider/config/backend binding generations、last credential generation、terminal disposition/revision/instant、scope digest、revocation proof status和secret-erasure receipt digest；
- tombstone不保存access/refresh token、raw JWT或client secret。MATERIAL_LOST与IN_DOUBT不因90天到期转terminal；
- refresh/revocation不可替代provider receipt、rotated-token evidence和migration conflict source按对应record lifecycle保留；普通HTTP debug body/log不得成为唯一evidence；
- v1 plaintext source在全部approved deployment完成v2 cutover且逐source secure-erasure receipt提交后立即删除，部署协调proof默认保留180天；存在conflict/hold时source保持加密隔离而非普通文件明文；
- legal hold可延长metadata/evidence，但不得要求保留可被安全crypto-erased且非必要的bearer plaintext；security authority可以立即吊销borrow并擦除material，同时保留secret-safe tombstone和receipt。

### 35.6 D03：login、rotation、logout、revoke与purge authority

scoped instance：`D03-oauth-credential-command-and-purge-authority-v1`，状态`CONFIRMED`。

- interactive login只由Product用户交互/approved operator authority发起；Runtime OAuth flow执行并返回typed receipt。模型、Tool、provider SDK、background refresh线程不能自行弹浏览器、切grant或创建credential subject；
- proactive refresh由Product-approved credential policy授权，Runtime current credential owner执行；refresh只能保持同provider/subject和已批准scope，不得扩大scope、account或网络endpoint；scope变化要求新login intent与用户授权；
- LOCAL_LOGOUT可由credential owner/user authority发起，立即撤销本地borrow并crypto-erase；它明确不声称provider revoke。REVOKE_AT_PROVIDER需要单独用户/operator authority及provider capability，按D11 effect结算；
- backend migration只由Product credential migration authority发起，要求source/target inventory、expected revision、inactive target写入、read-back和cutover receipt；普通load/refresh失败不能切backend；
- conflict owner action封闭为`KEEP_FILE_SOURCE`、`KEEP_KEYRING_SOURCE`、`KEEP_CONFIG_SOURCE`、`MARK_ALL_REAUTH_REQUIRED`、`KEEP_IN_DOUBT`。选择必须绑定secret-safe digest、actor、reason、expected revision并安全退休未选source；不存在“merge token fields”；
- retention compact/purge由Product credential maintenance authority发起，Runtime metadata owner复核terminal state、borrow/attempt/effect、hold、vault erase receipt和revision后执行；SecretStore只按typed erasure command删除material，不自行删除OAuth metadata；
- legal hold、security clear和普通logout是不同authority。security clear可优先停止使用/擦除secret，但不能伪造远端revocation成功；legal hold不能重新开放已撤销token；
- corrupt/unknown metadata、selector、vault reference或keyring response使该subject fail closed，其他subject可继续。不得删除corrupt file后自动登录，也不得将keyring暂时不可用解释为ABSENT；
- 所有command receipt和日志secret-opaque，只含identity/generation/digest/disposition，不输出token、authorization code、device code、PKCE verifier、client secret或raw provider body。

### 35.7 D07：刷新、登录、存储与并发边界

scoped instance：`D07-oauth-credential-bounds-v1`，状态`CONFIRMED`。

- 每个Product integration默认最多32个credential subjects、hard max1,000；每subject同时只有一个active credential generation、一个refresh/revoke mutation和最多64个有界borrow；超过返回typed backpressure，不创建第二active generation；
- access/refresh/client secret各最大64 KiB，authorization/device/provider response body最大1 MiB，scope最多256项且每项最大256字符；claims projection最大64 KiB。超过严格拒绝，不截断后使用；
- OAuth HTTP connect timeout10秒、response timeout30秒；interactive authorization总窗口默认10分钟、hard max30分钟；device polling遵守provider interval但最小1秒、最大30秒、总窗口不超过provider expiry及30分钟；
- proactive refresh在expiry前默认5分钟触发，但不得早于token lifetime的50%；Product hard range30秒至30分钟。unknown expiry不视为永久有效：interactive/refresh-token credential默认要求24小时内重新验证，client-credentials按provider policy，不能用`None => never expires`；
- 同一subject自动refresh最多1次网络attempt。可靠idempotency/query contract另经批准方可增加；timeout/connection loss进入IN_DOUBT。认证失败不能在provider client内部循环刷新；由Product gateway按同一attempt identity最多选择一次refresh slot；
- metadata/effect reconcile每批最多200 subjects、5秒预算；secret retirement每批最多100 generations。单个corrupt/locked subject隔离并报告，不阻塞其他subject；
- file metadata最大256 KiB/subject；secret material使用vault backend自身hard bounds。keyring单entry若无法原子CAS，不允许以keyring value同时承载mutable metadata：metadata进入canonical revisioned store，keyring只保存generation-scoped secret material；
- cross-process mutation必须使用canonical CAS/fence。`FileLock`只能作为本机adapter互斥优化，不是跨主机authority；部署若共享credential store，必须使用支持原子revision/fence的backend，否则Product activation拒绝shared mode；
- refresh/login/revoke的wall-clock事实使用AbsoluteInstant，网络等待与lock timeout使用monotonic；NTP回拨不能延长已过期token，clock mismatch返回typed failure；
- 上限由versioned Product credential policy选择，extension只能收窄。不得以无限等待file lock、无限device polling、无限retired client list或静默fallback承诺“总能拿到token”。

### 35.8 解除阻断、准入门禁与实施顺序

本轮关闭B21的absence/tombstone混淆、backend双truth、非原子keyring CAS、明文OAuth副本、refresh/revoke未知结果和旧数据cutover问题。OAuth workstream已不再需要实施者决定保留哪种状态语义；实际发现多backend冲突时需要operator按已确认typed command选择，但这属于数据实例处置，不是开放架构设计。

独立requirements及顺序：

1. `R-W3-OAUTH-001`：冻结CredentialSubjectId、metadata v2、state/command/receipt、SecretRef、backend binding和strict codec。
2. `R-W3-OAUTH-MIGRATION-001`：实现selector/file/keyring/config/vault只读inventory、secret-safe conflict report和dry-run；依赖001。
3. `R-W3-OAUTH-MIGRATION-002`：实现inactive vault/material candidate、metadata candidate和per-subject forward cutover；依赖前两项。
4. `R-W3-OAUTH-STORE-001`：实现revisioned metadata owner、具体backend binding、CAS/fence和secret borrow/retirement；依赖001/003。
5. `R-W3-OAUTH-EFFECT-001`：实现refresh/revocation intent、attempt evidence、IN_DOUBT与reconciler；依赖004。
6. `R-W3-OAUTH-COMMAND-001`：迁移login/logout/revoke/backend migration/owner action/retention command；依赖004/005。
7. `R-W3-OAUTH-CONSUMER-001`：迁移ModelGateway、provider clients和MCP OAuth consumer到generation-bound borrow，删除provider内部store/backend/refresh选择；依赖005/006，可按consumer拆单并行。
8. `R-W3-OAUTH-RETIRE-001`：全部deployment cutover后安全清除v1 plaintext、fallback selector、nullable-token writer和migration decoder；依赖deployment evidence及180天proof，不与首次activation合并。

首节点为001；migration先于v2 writer；metadata/secret owner完成后才启用网络effect；consumer最后只拿borrow capability。MCP与LLM可以共享OAuth lifecycle机制，但provider/account/scopes和consumer authorization保持不同binding，不能共享一个模糊“default”credential。

## 36. 第十三批已确认评审结论：RunJournal拆分迁移与通用StepRecord退场

本节关闭B34剩余阻断。`runtime/ledger/run_journal.py`当前把think、tool、timer塞进同一`StepRecord`，以字符串`kind/effect/status`、float wall time和nullable string payload表达三个不同状态机；生产consumer只有ToolExecutor与per-session durable inference/timer，另有已在第31/32节确认退出的Temporal Workflow effect writer。仓内已存在typed `LocalModelCallJournal`、Session runtime operation journal、ToolExecutor唯一执行chokepoint和可复用append/fsync机制。结论不是扩建RunJournal v2，而是按owner迁出并删除业务级RunJournal。

### 36.1 owner决定：复用mechanism，不共享错误业务状态机

scoped instance：`D34-run-journal-domain-split-v1`，状态`CONFIRMED`。

- Tool effect intent、attempt、provider/process receipt、result settlement和recovery由`runtime/tools`内ToolExecutor effect bounded context拥有；它复用第31节四类effect capability思想和`AGENTS.md §10`统一EffectId链，但不是Workflow effect store，也不import Orchestration；
- 模型调用plan、wire authorization、attempt、费用、response和IN_DOUBT唯一属于现有`contracts/model/model_journal.py`与`runtime/models/failover/model_journal.py`。InferenceJournal不得再把付费模型调用称为PURE或建立第二个think truth；
- Session中“模型结果尚未投影为assistant message”的事实属于Session log/projection owner，只保存ModelCallId、accepted response/result ArtifactRef和projection settlement edge，不复制整个模型执行状态机；
- durable timer属于Runtime Session operation/timer owner，identity绑定Session、operation/turn generation、deadline clock和resume generation；它不与Tool effect共享step id、reap或payload codec；
- `AppendOnlyLedger`可作为Runtime内部存储机制继续被明确domain adapter复用，但不公开`RunJournal`、`StepRecord`、`KIND_*`、`record_started/completed/failed`等万能业务API。每个domain提供自己的strict tagged records、transition、Port和retention；
- FileOps journal、Session event journal、ModelCallJournal、service-call journal和Event journal名字相似但不属于本迁移，禁止借机合并；它们只接受各自owner的后续评审；
- Product只装配Tool effect、ModelCall、Session timer/projection三个服务，不把一个RunJournal实例注入所有consumer。`RunJournalConfig(enabled=False)`退出：外部Tool effect安全记录是正确性保证，不允许Product关闭；纯Tool根本不进入effect store。

被拒方案：给StepRecord增加更多optional字段会继续制造巨型union；按`kind`在同一JSONL长期分流仍共享retention、lock和corruption blast radius；Temporal与local tier共写RunJournal会让backend history成为第二truth；保留enabled flag会允许绕过external effect intent。

### 36.2 Tool effect使用closed capability与typed lifecycle

scoped instance：`D11-runtime-tool-effect-reconciliation-v1`，状态`CONFIRMED`。

- 每个published Tool invocation在binding activation时选择`NO_EXTERNAL_EFFECT / IDEMPOTENT_BY_KEY / RECONCILABLE_BY_RECEIPT / NON_REPLAYABLE`之一；ToolExecutor在permission/hook/sandbox之后、外部动作之前提交typed intent；classification改变、arguments改变或definition generation改变必须创建新的decision/attempt，不能沿用旧RunJournal call id；
- logical ToolEffectId绑定ToolInvocationIdentity、caller Agent/incarnation、turn/run、definition/catalog/permission generation、canonical arguments digest与effect capability；attempt ordinal不进入logical identity；同id不同preimage返回typed conflict；
- lifecycle至少区分`INTENT_COMMITTED / CLAIMED / EXECUTION_STARTED / RECONCILING / SETTLED_SUCCEEDED / SETTLED_FAILED / IN_DOUBT / OWNER_ACTION_REQUIRED`；STARTED不能同时表示“尚未调用、正在调用和结果丢失”；
- terminal ToolResult使用canonical typed receipt/ArtifactRef，不把`ToolResult`压成任意string payload。failed settlement必须区分permission deny、spawn/transport failure、external action failure和unknown-after-effect；
- `NO_EXTERNAL_EFFECT`最多3次execute；`IDEMPOTENT_BY_KEY`仅在provider contract证明相同key/payload冲突语义时最多3次；`RECONCILABLE_BY_RECEIPT`execute最多1次，随后query；`NON_REPLAYABLE`execute最多1次且unknown直接IN_DOUBT。具体bounds见36.6；
- Tool自身的`can_resume_started_call()`不能单独授予重试权；它必须由activation时批准的typed capability/provider contract支持，并由ToolExecutor current owner验证receipt/query。duck-typed方法和字符串effect退出；
- deferred Workflow/BackgroundTask submission在Tool effect中只记录submission receipt及目标canonical identity。后续状态由Workflow/BackgroundTask owner拥有，Tool store不复制其状态机；
- stale Agent incarnation、Tool definition/catalog generation或effect owner fence不得commit result、ack或retry。外部动作已发生而terminal commit失败进入IN_DOUBT并保存不可替代evidence。

### 36.3 ModelCall与Session projection之间只有typed settlement edge

- RunJournal think STARTED不再授权重新调用模型。模型是否可继续、重试或对账只看ModelCallJournal plan/attempt/wire authorization/terminal及current inference owner；
- 模型成功后先在ModelCallJournal提交canonical terminal response/usage/cost，再由Session owner提交`MODEL_RESULT_PROJECTION_INTENT(ModelCallId, response digest/ref, target session revision)`；assistant message落入Session log后提交projection ack；
- crash发生在模型terminal与Session intent之间时，reconciler从ModelCall terminal事实发现未投影结果；crash发生在intent与message commit之间时，按同一projection identity幂等完成；message已存在但ack缺失时只补ack，不重新请求LLM；
- ModelCall STARTED/open wire attempt在恢复时是IN_DOUBT，不因RunJournal记录缺失、`reap_think()`或Session history没有assistant message而重新付费；
- RunJournal中完整InferenceResult只作为migration evidence。迁移后canonical response在ModelCallJournal/Artifact，Session只持ref；删除`reconcile_think_journal()`对损坏payload的silent continue/reap，以及“think是PURE、丢失只会repay”的产品声明；
- ModelCall journal本身后续可以更换storage adapter，但不能让Session或RunJournal成为fallback truth。Temporal backend只提供attempt evidence/transport，不接管ModelCall settlement。

### 36.4 D01：按kind对账迁移，不把旧StepRecord整体搬家

scoped instances：`D01-run-journal-tool-cutover-v1`、`D01-run-journal-think-cutover-v1`、`D01-run-journal-timer-cutover-v1`，状态均为`CONFIRMED`。

迁移在对应Session/Agent激活前由Runtime migration coordinator取得该Session独占generation；先inventory全部`run-journal.jsonl`及相关Session log、ModelCall journals、Tool invocation facts和Temporal legacy source。迁移按整个source文件严格验证后构建三个domain candidate，不能某kind成功后删除共同source。

共同规则：

- 每一newline-terminated frame严格解码；只允许最后一个未换行torn tail按现有protocol记录并忽略。中段JSON损坏、unknown field/kind/status、非法transition、重复/forkedidentity或同step不同immutable facts使该Session migration fail closed；
- float `started_at/ended_at`只接受finite nonnegative值并转换为明确Unix UTC AbsoluteInstant，provenance标为legacy float；ended早于started、无法表达clock或timer deadline非法均冲突；
- `application-workflow-effects`由第32节迁移处理，本轮只验证其不被当普通Session重新迁移；Temporal Workflow writer必须先退出；
- candidate全部strict read-back并生成source digest、逐kind/state计数、target identities/digests、conflict和Artifact edges；三个target发布后以单一Session migration manifest激活。失败保持source不变且三个新consumer均不启动，禁止mixed path。

Tool记录：

- 必须有完整`ToolInvocationIdentity`且能唯一匹配Tool definition/catalog/arguments/owner/run identity；缺失identity的legacy record不获得execute权，迁为OWNER_ACTION_REQUIRED或ARCHIVE_ONLY evidence；
- COMPLETED/FAILED且payload能严格解码为canonical ToolResult receipt时保留terminal事实；解码失败不能当普通失败，进入migration conflict；
- STARTED + external/unknown effect迁为IN_DOUBT，绝不调用旧Tool；STARTED + 可严格证明NO_EXTERNAL_EFFECT且从未越过execute boundary时才可迁为可重试intent，但RunJournal没有该boundary evidence时默认IN_DOUBT；
- RunJournal中`effect=local/pure`记录若仅是优化memoization，不进入Tool effect truth；合法内容迁为ARCHIVE_ONLY或由consumer证明可重建后在receipt中标记discardable，不能为了减少记录猜测删除。

Think记录：

- 从严格checkpoint/result中提取ModelCallId；与唯一ModelCallJournal按call identity、request/response digest、generation和Session relation匹配。不得只按think seq或时间邻近关联；
- COMPLETED且ModelCall terminal response一致时，补建Session projection intent/ack缺口；不复制response。若ModelCall缺失但RunJournal有完整可信result，迁为`LEGACY_MODEL_RESULT_EVIDENCE`并进入OWNER_ACTION_REQUIRED，不能虚构wire attempt/费用或直接重放为assistant message；
- STARTED若对应ModelCall存在，RunJournal仅成为checkpoint evidence，恢复由ModelCall state决定；缺ModelCall则迁为`LEGACY_MODEL_CALL_UNKNOWN`，禁止自动重付费；FAILED/reaped残余作为migration evidence，不覆盖ModelCall terminal；
- 同一ModelCall对应多个不一致think record、result与Session assistant message冲突或费用/response不一致时阻断该Session activation。

Timer记录：

- timer identity确定性映射为SessionTimerId，绑定Session generation、legacy step id/seq和deadline；STARTED且deadline未来迁为PENDING，deadline已过迁为ELIGIBLE_MISFIRE而非直接执行；
- COMPLETED保留terminal；FAILED只有能证明timer operation未产生外部动作时迁为terminal failure。timer callback若实际承载Tool/Workflow effect，拆出对应canonical intent，不能由timer owner重放callback；
- 缺deadline、repr无法严格解析、NaN/无限或clock不明进入OWNER_ACTION_REQUIRED；禁止用`eval`、宽松float或当前时间重算。

v1 source在全部三个target activation并运行一个验证周期后仍作为retired migration evidence保留；普通恢复不再读取。退出条件见36.7。

### 36.5 D02/D03：各domain retention与authority，不由generic reap决定

scoped instances：`D02-runtime-tool-effect-retention-v1`、`D02-model-call-session-projection-retention-v1`、`D02-session-timer-retention-v1`、`D03-run-journal-cutover-and-purge-authority-v1`，状态均为`CONFIRMED`。

- terminal Tool effect完整arguments/result/receipt默认保留90天，随后compact为最小effect tombstone并保留1年；IN_DOUBT/OWNER_ACTION_REQUIRED、未结算Workflow/BackgroundTask submission、Artifact pin和legal hold不启动TTL；
- ModelCall完整plan/attempt/accepted response/usage/cost默认保留90天，最小call/attempt/cost tombstone保留1年；Session projection payload在message+ack terminal后30天可compact为identity/digest edge，tombstone随ModelCall/Session较长引用期；IN_DOUBT wire attempt不自动过期；
- terminal Session timer完整record默认保留30天，最小timer tombstone保留180天；pending/misfire/owner-action timer不自动过期；
- RunJournal retired source默认保留至所有approved deployment完成cutover加180天；若它是migration conflict、unknown Tool/model effect或legal hold的唯一evidence，则随相关domain lifecycle保留。source不是生产fallback；
- Tool effect正常settlement由ToolExecutor current effect owner执行；model terminal由ModelCall owner；Session projection/timer由current Session fenced owner。任何一方不能用generic`reap(keys)`删除另一domain事实；
- Product对应maintenance authority分别发起typed compact/purge command，各owner复核terminal/revision/fence/Artifact/reference/hold后执行。Session cleanup、workspace目录删除和AppendOnlyLedger rewrite无业务删除authority；
- Tool/Model IN_DOUBT owner action使用各自已确认的evidence-based disposition：确认成功、确认失败、保持未知或创建新补偿/新call；不存在retry_anyway。timer只允许确认已触发、确认未触发并取消、保持未知或创建新timer identity；
- legal hold与security clear独立。security clear可删除敏感payload并保留digest/receipt，但不能删除唯一Tool receipt、模型费用/response证据后声称terminal；
- migration retirement由Product Runtime migration authority在manifest证明三个consumer均切换、source无active reference、hold关闭后执行secure deletion并返回逐source receipt。测试临时store不构成生产authority。

### 36.6 D07：记录、payload、scan、compaction和recovery上限

scoped instance：`D07-runtime-run-domain-bounds-v1`，状态`CONFIRMED`。

- 单Tool intent inline arguments最大1 MiB、terminal receipt/result inline最大64 KiB，超过使用ArtifactRef；单Model response inline最大64 KiB，完整大response走ArtifactRef；timer payload不得承载任意callback/blob；
- 每Session active Tool effects默认1,000、hard max10,000；active ModelCalls默认100、hard max1,000；active timers默认1,000、hard max10,000。容量耗尽返回typed backpressure且不提交新intent，不删除旧active事实；
- Tool reconciliation每批500、Model reconciliation每批200、timer scan每批500；每批wall-time5秒，每record单独claim/settle，poison和未到期item不阻塞后续；
- Tool capability自动attempt：NO_EXTERNAL/IDEMPOTENT最多3次，backoff1/5/30秒；RECONCILABLE execute1次、query最多12次且总窗口24小时；NON_REPLAYABLE execute1次。Model attempt上限由已批准AttemptBudget且不得由RunJournal增加；timer触发最多1次canonical submission，unknown不自动再触发；
- domain append stream单frame hard max2 MiB；单stream soft max64 MiB、hard max256 MiB。达到soft limit触发有界compaction/backpressure，hard limit停止新admission但保留query/settlement；不通过读取整个无界JSONL后内存fold继续运行；
- compaction每批最多1,000 terminal identities、每次candidate最大64 MiB、5秒预算。使用generationed inactive candidate、fsync、atomic manifest switch与parent fsync；旧generation在新generationread-back和pin关闭前不可删；
- 每个domain writer必须有跨进程CAS/fence或明确single-process scope。当前`AppendOnlyLedger`仅进程内dict且无writer lock，不能直接作为多进程canonical owner；Product若允许Session跨进程resume，旧owner失去fence后不得append/reap；
- committed中段corruption阻断该domain mutation和recovery；只允许协议定义的单个torn tail。corruption evidence最大保留原source，不复制无限quarantine；repair需要offline typed command；
- timestamps使用AbsoluteInstant/clock identity，elapsed timeout用monotonic；禁止`time.time()` float进入新durable schema。migration-only legacy decoder例外但必须标provenance；
- bounds由versioned Product Runtime policy选择，extension只能收窄。删除`records()`全量无界scan、`next_seq=max(all records)`以及任意`reap(list)`生产接口，改为owner query/cursor和有界maintenance command。

### 36.7 解除阻断、准入门禁与实施DAG

本轮关闭B34的consumer owner、共享step状态机、Temporal双truth、think重付费、跨进程writer、compaction/retention和旧JSONL处置阻断。RunJournal不再是待治理的长期公共基础设施，而是有明确退出条件的migration source。

独立requirements及顺序：

1. `R-W3-RUN-DOMAINS-001`：冻结Tool effect、ModelCall↔Session projection、Session timer的typed contract、Port、identity和retention policy；同时冻结RunJournal source inventory schema。
2. `R-W3-TOOL-EFFECT-001`：实现ToolExecutor typed effect owner、capability/reconciliation、fence与Artifact receipt；依赖001。
3. `R-W3-MODEL-PROJECTION-001`：接通ModelCall terminal到Session projection intent/ack，删除InferenceJournal第二truth；依赖001及现有ModelCallJournal。
4. `R-W3-SESSION-TIMER-001`：实现Session timer owner、misfire/cancel/recovery；依赖001。
5. `R-W3-RUNJOURNAL-MIGRATION-001`：实现全量strict inventory、三类cross-domain dry-run与conflict report；依赖001–004，不写active target。
6. `R-W3-RUNJOURNAL-MIGRATION-002`：实现三个inactive candidates、Session migration manifest和forward-only activation；依赖005。
7. `R-W3-RUNJOURNAL-CONSUMERS-001`：迁移ToolExecutor、inference、sleep/timer、Jsonl durable backend；删除RunJournal config/StepRecord API；依赖006。
8. `R-W3-TEMPORAL-RUNJOURNAL-RETIRE-001`：按第31/32节移除TemporalBackend RunJournal及application Workflow writer；可在contract冻结后与002–004并行，但必须在migration inventory最终签名前完成。
9. `R-W3-RUNJOURNAL-RETIRE-001`：所有deployment cutover、180天proof和hold关闭后删除source、migration decoder、`runtime/ledger/run_journal.py`及仅服务它的公开导出/测试。

首节点001；三个target domain实现可以并行；inventory必须等待target contract冻结，active cutover必须等待三个target可写；consumer迁移后才开始retirement clock。Hosted service-call journal、FileOps和Session event journal不依赖RunJournal迁移，继续按自身owner评审，不能因本节“journal退场”被误删。

## 37. 第十四批已确认评审结论：Hosted service-call canonical state、索引投影与远端对账

本节关闭B36的D01/D02/D03/D07和D11阻断。当前`contracts/service/journal.py`与`runtime/service_gateway/journal.py`已经形成独立typed service-call owner，不能并入RunJournal或Workflow；但当前claim generation没有进入每条record，cancel使用独立`.cancel`文件，pending query扫描目录并以文件名作为cursor，receipt含裸`dict[str, JsonValue]`，调用方还可以自行声明execution semantics与idempotency key。这些缺口会使projection、取消和重试获得平行真相或过宽权限。

### 37.1 owner与最小服务面保持独立

scoped instance：`D36-hosted-service-call-owner-v1`，状态`CONFIRMED`。

- `runtime/service_gateway`唯一拥有HostedServiceCall的plan、logical identity、attempt、remote acceptance receipt、poll/reconcile、cancel intent、terminal settlement、retention和query projection；
- hosted service-call表示Runtime调用Product批准的远端Tool service，不等于Workflow activity、ModelCall、Tool local process或Agent delivery。它可以作为Tool effect的下游canonical operation；Tool effect只保存ServiceCallId/submission/terminal receipt edge，不复制service attempt状态；
- Contracts只公开versioned ServiceInvocation command、closed capability binding、typed receipt/response/failure、immutable recovery/query projection和最小command/query Port。外部consumer不得取得journal path、record append、ownership claim、pending index或adapter instance；
- Product composition冻结service catalog、endpoint descriptors、credential binding、execution capability、region/governance policy和backend。Runtime根据binding执行；caller只能选择已发布capability及传入typed payload，不能提升semantics、attempt budget、idempotency保证或endpoint范围；
- `ServiceCallJournal`的公开`append(records)`、`records()`和`claim()`是实现内部面，不继续作为Contracts consumer Port。正式服务面为`submit/query/cancel/reconcile/owner_action` typed commands与receipts；journal adapter隐藏在Runtime package内；
- pending index是canonical journal的可重建projection，只用于发现候选。claim前必须回读canonical call revision/state；索引缺失不得永久丢工作，索引多出不得产生执行权，存在有界canonical reconcile scan修复；
- service call的Remote OperationId/receipt是provider evidence，不成为Mote canonical state owner；Mote terminal只能由current fenced service-call owner根据严格evidence提交。

拒绝把service-call并入Tool effect：一个remote call可以跨caller deadline长期WAITING_REMOTE并拥有poll/cancel协议，Tool effect只需引用其结果。也拒绝用通用“effect manager”统一Workflow/OAuth/Service，因为provider capability、identity与retention不同。

### 37.2 capability决定submit、poll、retry与cancel权

scoped instance：`D11-hosted-service-execution-capability-v1`，状态`CONFIRMED`。

Product service binding从以下closed capability中选择，语义与第31/36节一致但由service domain正式声明：

- `NO_EXTERNAL_EFFECT`：仅确定性远端纯查询且provider正式保证失败不会产生可观察副作用；最多3次submit；
- `IDEMPOTENT_BY_KEY`：provider保证同account/endpoint/contract revision下，同key同payload映射同operation，不同payload冲突且不执行第二次，并声明key retention窗口；最多3次相同submit；
- `RECONCILABLE_BY_RECEIPT`：provider返回stable OperationId/receipt并提供不会创建第二动作的query；submit最多1次，之后只poll/query；provider明确证明NOT_STARTED时方可重新submit同logical call；
- `NON_REPLAYABLE`：无可靠idempotency/query的外部动作；submit最多1次，timeout/connection loss/worker crash直接IN_DOUBT；
- `ServiceExecutionSemantics`不再由每次`ServiceInvocation`自由传入；invocation只绑定service definition generation，Runtime从frozen binding取得capability。caller提供的idempotency key只能是logical request identity的确定性投影，不能证明provider合同；
- ServiceCallId preimage绑定caller Agent/incarnation、turn/ToolEffectId、service definition/config generation、capability、canonical payload digest、provider/account/endpoint contract与permission generation。attempt ordinal不进入logical identity；同id不同preimage返回`SERVICE_CALL_IDENTITY_CONFLICT`；
- provider idempotency key由ServiceCallId和binding namespace确定性派生；禁止调用方任意复用key。provider key、payload digest、account、endpoint和有效窗口进入plan；
- provider receipt改为service-definition-owned versioned tagged union或ArtifactRef，至少绑定OperationId、provider/endpoint/account identity、request digest、receipt contract revision、poll cursor和expiry；删除任意`state: dict`；
- query结果是closed union：`NOT_STARTED / ACCEPTED / RUNNING / SUCCEEDED / FAILED / CANCELLED / UNKNOWN`。transport failure不是provider FAILED；UNKNOWN不能转FAILED或触发submit；
- cancel是独立远端effect。仅provider capability明确支持幂等cancel/query时自动调用一次；cancel timeout/unknown保持`CANCELLATION_IN_DOUBT`，不把call标CANCELLED；不支持远端cancel时只停止本地等待并保持remote operation WAITING_REMOTE/OWNER_ACTION_REQUIRED；
- caller deadline只终止当前等待，不终止canonical remote operation。存在receipt的call转WAITING_REMOTE并由reconciler推进；没有receipt且submit结果未知转IN_DOUBT；不得因deadline重选endpoint重新submit。

### 37.3 v3 lifecycle、fence和取消进入同一truth

service-call v3至少使用以下closed state：

`PLANNED -> INTENT_COMMITTED -> CLAIMED -> SUBMIT_STARTED -> WAITING_REMOTE/RECONCILING -> SETTLED_SUCCEEDED/SETTLED_FAILED`，并包含`CANCEL_REQUESTED / CANCELLING / CANCELLED / CANCELLATION_IN_DOUBT / IN_DOUBT / OWNER_ACTION_REQUIRED`。普通retry exhaustion只能在能证明每次未产生未知动作时终止FAILED。

- 每个mutation record显式绑定call revision、attempt ordinal、execution owner subject/owner id/fencing token或single-process generation、definition/config generation和AbsoluteInstant；append位置不能隐式充当完整revision contract；
- ownership acquire原子CAS推进durable owner generation，并返回带expiry/lease或明确process-local lock scope的claim。所有append、poll、cancel、terminal、compact和purge验证current fence；旧owner即使仍持ContextVar或网络response也不能commit；
- 当前`fcntl.flock`只适用于同一主机、同一filesystem能证明锁语义的local backend。Product若允许跨主机worker/shared storage，必须选择lease+monotonic fencing backend；否则activation拒绝distributed mode，不能把`.owner.json`计数器冒充跨主机lease；
- cancel command进入canonical stream/store，绑定CancelCommandId、authority、expected revision、reason和instant；删除`.cancel`旁路文件。重复相同command幂等，不同command/revision冲突；
- submit前commit durable intent；remote accepted receipt先作为attempt evidence提交，再转WAITING_REMOTE。外部response返回时若fence丢失，可写attempt-scoped immutable evidence inbox，current owner验证后settle；evidence inbox不是第二terminal truth；
- terminal response绑定payload/result digest、provider provenance、cost、credential generation和ServiceCallId。大结果使用ArtifactRef；不能把provider成功后本地Artifact失败伪装成remote failure，进入IN_DOUBT；
- pending projection保存CallId、canonical revision/state、next_eligible_at、priority/governance scope和projection generation，不保存可独立修改的payload/receipt。projection ack只是“已索引该revision”，不改变call lifecycle；
- durable scan以canonical store generation/cursor发现遗漏；目录mtime、文件名排序和进程内task不是唯一推进机制。

### 37.4 D01：现有per-call JSONL、owner和cancel文件迁移到v3

scoped instance：`D01-hosted-service-call-v2-to-v3`，状态`CONFIRMED`。

迁移在ServiceGateway、HostedServiceReconciler及Tool consumer启动前，由Runtime service-call migration owner取得全store独占cutover generation；Product只触发唯一入口。

- inventory枚举approved service journal root内的`.jsonl`、`.owner.json`、`.cancel`及pending projection，拒绝symlink、错误owner/mode、未知文件类型和path identity冲突；不扫描workspace外同名目录；
- 每个JSONL完整strict decode；当前实现不接受未换行尾部，因此迁移同样将其视为corruption evidence而非自动截断。中段/尾部损坏、mixed CallId、非法transition、重复attempt/receipt ordinal或terminal后记录使该call fail closed；
- v2 plan的logical ServiceCallId保留，但v3补算完整preimage digest。plan capability/semantics只能在当前Product service definition与payload/endpoint/account完全一致时绑定；无法证明provider contract者降级为NON_REPLAYABLE，不虚构idempotency；
- v2 PLANNED且从未有attempt可迁为INTENT_COMMITTED，但须重新绑定current definition/config且不得自动执行，activation command确认后才eligible；
- 存在attempt STARTED但没有可信receipt/terminal的记录迁为IN_DOUBT，无论旧recovery称RUNNING；不得因owner generation已过期认为未执行；
- 有严格ServiceReceiptAcceptedRecord的open attempt迁为WAITING_REMOTE/RECONCILING，receipt严格转换为definition-owned tagged evidence；裸state中存在unknown/额外字段时整条进入OWNER_ACTION_REQUIRED，不宽松保留dict；
- v2 SUCCEEDED/FAILED/CANCELLED terminal保留原事实和provider provenance；若成功response/receipt不能严格解码则迁为terminal conflict而非丢payload。v2 terminal IN_DOUBT保持IN_DOUBT，不作为普通terminal启动retention；
- `.cancel`存在时转换为canonical legacy CancelCommand，instant使用文件创建/mtime只能标为`LEGACY_FILESYSTEM_LOWER_BOUND`而非可信业务时刻；若call已terminal，command作为late no-op audit evidence；若无call JSONL则保留orphan conflict，不凭cancel文件创建call identity；
- `.owner.json`只迁为legacy ownership evidence和minimum next fencing generation，不能恢复旧owner执行权；generation缺失可从零开始新v3 owner，损坏/倒退则阻断该call；
- pending scan结果与canonical calls对账：多余entry丢弃并记录，缺失entry由v3 candidate重建。projection不能覆盖canonical state；
- candidate包含v3 canonical records、typed evidence/Artifact edges、cancel facts、pending index和migration manifest。全部strict read-back后用单一active generation切换；不能逐call覆盖造成同一Gateway混用v2/v3；
- receipt记录source/target digests、逐state/capability/cancel/receipt counts、downgrade与conflict、projection rebuild digest、cutover generation和implementation identity。首次v3 activation后普通reader/writer只支持v3；v2 decoder进入migration-only路径。

迁移不调用远端query、cancel或submit，不增加费用和副作用；旧WAITING_REMOTE的首次query只能在v3 current owner、current credential/definition验证后由reconciler执行。

### 37.5 D02：remote operation、terminal response和最小tombstone保留

scoped instance：`D02-hosted-service-call-retention-v1`，状态`CONFIRMED`。

- PLANNED/INTENT/CLAIMED/SUBMIT_STARTED/WAITING_REMOTE/RECONCILING/IN_DOUBT/OWNER_ACTION_REQUIRED/CANCELLATION_IN_DOUBT所需plan、payload digest/ref、receipt/evidence和cancel facts不受普通TTL；
- SETTLED_SUCCEEDED/FAILED/CANCELLED在所有ToolEffect、Artifact、cost/audit和caller projection引用结算后，完整payload/response/provider receipt默认保留90天；随后compact为最小ServiceCall tombstone并保留1年；
- tombstone至少保存CallId/preimage digest、definition/config/capability、payload digest、attempt count、provider/endpoint/account/credential generation、OperationId/receipt digest、terminal disposition/revision/instant、cost、cancel disposition和ToolEffect/Artifact edge digest；
- IN_DOUBT及未知remote/cancel结果不因90天或provider receipt expiry自动terminal/purge；receipt临近expiry只能提升reconcile优先级，不能授权盲重试；
- irreplaceable provider receipt、response、cost/audit和stale-owner evidence随call lifecycle保留；普通transport/debug log不是唯一evidence。大payload/response走ArtifactRef并绑定retention edge；
- pending projection只保留active call；terminal后可立即移除，随后由canonical query/tombstone服务。projection generation/repair receipts保留30天，不延长业务payloadTTL；
- v2 source和migration manifest至少保留所有approved deployment完成cutover后180天；conflict/hold唯一证据随相关call保存；source不是fallback reader；
- legal hold延长必要evidence。security clear可提前擦除允许的敏感payload并保留digest/receipt，但不能删除唯一OperationId或remote result后声称已对账。

### 37.6 D03：submit、cancel、owner action和purge authority

scoped instance：`D03-hosted-service-call-command-and-purge-authority-v1`，状态`CONFIRMED`。

- submit只由ToolExecutor或Product批准的service consumer通过capability-scoped Port发起；模型不能自行选择endpoint/semantics/idempotency，service adapter不能扩大payload/region/credential；
- current fenced Runtime service-call owner执行submit/poll/cancel/settlement。Product policy选择binding和budget但不能直接append状态；provider adapter只返回typed outcome/evidence；
- caller cancel只提交CancelCommand。是否调用remote cancel由binding capability决定；caller/UI不能把request accepted显示为remote CANCELLED；
- owner action封闭为`CONFIRM_SUCCEEDED(evidence)`、`CONFIRM_FAILED(evidence)`、`CONFIRM_CANCELLED(evidence)`、`KEEP_IN_DOUBT`、`QUERY_WITH_CURRENT_BINDING`、`CREATE_COMPENSATING_SERVICE_CALL`。没有`retry_anyway`、`force_cancelled`或重开旧attempt；
- `QUERY_WITH_CURRENT_BINDING`只允许同provider/account/OperationId和query contract revision，不产生新remote operation；compensation创建新CallId/ToolEffectId并记录supersedes/compensates edge；
- Product service maintenance authority发起compact/purge；Runtime current store owner复核terminal、revision/fence、ToolEffect/Artifact/cost/hold和migration evidence后执行。Tool cleanup、Session delete、Artifact GC和目录扫描无service-call删除权；
- legal hold与security clear分别由governance/security authority发起；maintenance不能解除hold，security clear不能伪造远端terminal；
- corrupt call隔离阻断该call mutation，但其他call可继续；若corruption影响全store manifest/index generation则停止全store admission。不得删除corrupt文件后重新submit同CallId；
- 每项不可逆delete/compact使用fenced deletion claim并在payload、Artifact、source各阶段复核；partial failure进入cleanup IN_DOUBT并保存before/after digest与stage receipt。

### 37.7 D07：admission、attempt、poll、scan和存储边界

scoped instance：`D07-hosted-service-call-bounds-v1`，状态`CONFIRMED`。

- active unresolved calls默认每root1,000、全deployment10,000；Product hard max分别10,000和100,000。容量在durable intent前原子reservation，满时返回typed BACKPRESSURED且不写PLANNED/ACCEPTED，不驱逐已有call；
- inline request payload最大1 MiB、provider receipt64 KiB、terminal response64 KiB；超过使用ArtifactRef。OperationId/idempotency key各最大512 bytes，endpoint/capability identity各最大256字符；不截断；
- NO_EXTERNAL/IDEMPOTENT submit最多3次，backoff1/5/30秒；RECONCILABLE/NON_REPLAYABLE submit最多1次。所有attempt还受Product AttemptBudget约束，较小者生效；extension不能提高；
- WAITING_REMOTE自动poll最多12次，backoff使用provider建议但夹在5秒至1小时，总自动观察窗口24小时；provider建议0或负数不能形成busy loop。窗口结束转OWNER_ACTION_REQUIRED，不标FAILED；
- remote cancel自动请求最多1次、query最多12次、总窗口24小时；未知保持CANCELLATION_IN_DOUBT；
- 单submit connect timeout10秒、response timeout60秒；单poll/cancel timeout30秒；caller总等待deadline由Product policy限制1秒至30分钟，但不改变remote operation lifecycle；
- canonical reconcile scan每批500、pending index page最多256、wall-time5秒；每call单独claim。poison、未到期和locked call不阻塞后续；full canonical repair scan每轮最多10,000 paths并持久cursor，不能一次`glob`全目录后驻内存；
- pending projection backlog默认10,000、hard max100,000；index写失败不回滚已提交canonical call，但call保持可由canonical scan发现并发出projection-degraded告警；index不能无限积累terminal；
- 单call stream单frame最大2 MiB、soft max16 MiB、hard max64 MiB；全store soft10 GiB、hard100 GiB。达到soft触发maintenance/backpressure，hard停止新submit但保留query/poll/cancel/settlement；
- compaction每批500 calls、每事务/manifest generation100、5秒预算；new generation read-back和pins关闭前不删old。owner claim默认30秒lease并有界refresh；process-local flock模式持锁不跨await外部网络，必须使用durable claim+fence后释放OS锁；
- persistent deadline/receipt expiry使用AbsoluteInstant和clock identity，process网络/lock/backoff用monotonic。restart后从absolute deadline确定是否停止caller等待，但remote receipt仍按provider query policy处理；
- Product service governance schema选择上述bounds，Runtime和consumer只能收窄。删除无限目录scan、旁路cancel文件、任意receipt dict和caller-controlled semantics。

### 37.8 解除阻断、准入门禁和实施顺序

本轮关闭B36的pending第二truth、cancel旁路、claim generation、provider receipt shape、caller semantics提升、unknown remote result、migration、retention、authority和bounds。Hosted service-call workstream已达到无需实施者临场决定即可拆单开工的状态。

独立requirements及顺序：

1. `R-W3-SERVICE-CALL-001`：冻结v3 CallId/preimage、capability binding、state/command/evidence/receipt、owner fence、index projection和strict codec。
2. `R-W3-SERVICE-CALL-MIGRATION-001`：实现v2 JSONL/owner/cancel/index只读inventory、capability downgrade和dry-run conflict report；依赖001。
3. `R-W3-SERVICE-CALL-MIGRATION-002`：实现v3 inactive candidates、projection rebuild、store generation manifest和forward cutover；依赖前两项。
4. `R-W3-SERVICE-CALL-STORE-001`：实现canonical command/query、CAS/fenced claims、cancel facts、retention和Artifact edges；依赖003。
5. `R-W3-SERVICE-CALL-EXECUTION-001`：实现capability-controlled submit/poll/cancel、attempt evidence和IN_DOUBT settlement；依赖004。
6. `R-W3-SERVICE-CALL-RECONCILE-001`：实现bounded canonical scan、rebuildable pending index、owner action与fault injection；依赖004/005。
7. `R-W3-SERVICE-CALL-CONSUMERS-001`：迁移ToolExecutor/Product surfaces，删除caller semantics/idempotency提升与public journal append/claim/records；依赖005/006。
8. `R-W3-SERVICE-CALL-RETIRE-001`：deployment cutover和180天proof后删除v2 decoder、`.owner.json`/`.cancel`、旧index/source及migration reader；不与首次activation合并。

首节点001；migration先于v3 writer；store完成后execution与index reconciler按共同contract推进；consumer最后只调用capability-scoped service。它依赖第36节Tool effect identity edge和Artifact ownership edge，但不依赖RunJournal迁移完成即可先做contract/store。

## 38. 第十五批已确认评审结论：Artifact reachability、Session deletion与fenced GC

结论：B10不能以“给现有GC补锁”关闭。当前`runtime/artifacts`已经拥有CAS、SQLite logical index、owner/retention和publication outbox，必须在该owner内扩展为durable typed ownership-edge与fenced deletion lifecycle；`runtime/session`只拥有Session lifecycle及删除请求，不拥有CAS物理删除权。`ArtifactPinRegistry`、workspace目录、mtime、cleanup stamp和调用参数中的legal-hold集合都不是canonical truth。以下决定均为`CONFIRMED`，实施无需再选择owner、保留期、删除权限、迁移失败策略或扫描上限。

### 38.1 源码事实与被拒绝的现状

- `ArtifactGarbageCollector.collect()`把store/root/pin source的当前快照合并为reachable set，以`time.time_ns()`、blob mtime和minimum age筛选后直接调用`repository.reclaim()`；它没有durable deletion claim，也没有在unlink前复核owner generation、edge revision、legal hold和current fence。
- `ArtifactPinRegistry`的generation、source、direct pin与freeze lease均为进程内状态。它可以继续作为本进程快速projection和操作期pin adapter，但不能证明其他进程不存在引用，也不能授权删除。
- `SessionWorkspace` cleanup用rollout/目录mtime、cleanup stamp和调用方传入的`legal_hold_session_ids`判断TTL，随后直接删除Session目录、tool result和task output目录。目录树删除不是原子业务事务；shared CAS、journal、delivery/effect和legal-hold引用不会因目录消失自动结算。
- `runtime/artifacts/store.py`已有SQLite owner row、retention promotion、publication outbox及`release_session_scope()`等机制。治理必须复用并扩展这些canonical能力，不新增`RetentionManager`、第二套pin store或平行GC registry；现有批量release API必须收敛为带command identity、expected generation和逐项receipt的owner命令。
- CAS repository的`scan()`、`modified_time_ns()`和`reclaim()`只允许作为受控存储adapter原语。目录出现、mtime足够老或当前进程未见pin都不能形成业务删除权；全目录scan求和也不能成为长期容量控制主路径。

### 38.2 canonical owner、edge与完整reachability证明

- Artifact metadata、content identity、CAS adapter、typed ownership edge、retention、hold projection、deletion claim与GC receipt的canonical owner是`runtime/artifacts`；Session terminal/delete state与Session deletion command的owner是`runtime/session`。
- Product composition选择retention、legal-hold/security-clear policy及maintenance schedule；domain owner发布自身事实，Product不得直接改artifact row，CAS adapter不得解释业务retention。
- 至少为Session、Workflow、BackgroundTask terminal pointer、Tool/Model result、hosted ServiceCall、Agent delivery、FileOps before-image/snapshot、stage/publication和legal hold定义typed edge。不得用通用字符串`owner_kind`加裸payload冒充封闭union。
- 每条edge至少绑定EdgeId、domain owner identity、owner generation/fence、target ArtifactId/digest、retention class、edge revision、created/released absolute instant、release command identity及必要的source fact digest。active edge不可原地改指向；替换通过新edge commit后释放旧edge。
- canonical reachability closure必须来自同一revisioned snapshot generation，并携带声明所有已注册edge producer均完成到该generation的completeness manifest。缺producer、generation不一致、decode corruption或manifest不完整一律fail closed。
- transient operation pin提交前必须先取得与owner generation绑定的pin。若artifact可能跨崩溃、进程或eviction继续被需要，该pin必须升级为durable edge；进程内registry只加速查询，重启后从canonical edge重建。
- 大型evidence的retention随引用它的domain lifecycle绑定。Session deletion只能释放本Session拥有且当前generation可证明的edge，不能越权释放Workflow、delivery、effect、publication或legal-hold edge。

### 38.3 删除状态机与不可逆阶段

Artifact/Session删除使用可恢复状态机：

```text
REQUESTED -> CLAIMED -> REFERENCES_RELEASING -> METADATA_TOMBSTONED
          -> BLOBS_RECLAIMING -> DIRECTORY_RETIRING -> SETTLED
```

并具有typed `BLOCKED_ACTIVE_REFERENCE`、`BLOCKED_LEGAL_HOLD`、`BLOCKED_OWNER`、`IN_DOUBT`和`FAILED` disposition。每个transition绑定CommandId、expected revision、current lease/fence、stage ordinal和幂等receipt；旧owner不得release edge、tombstone metadata、unlink blob或删除目录。

- `TTL_EXPIRE`、`USER_DELETE`、`SECURITY_CLEAR`、`LEGAL_HOLD_APPLY/RELEASE`和`TEST_FIXTURE_CLEANUP`是不同typed command、authority和审计语义，不能复用布尔force或同一cleanup入口。
- Session delete先阻止新Session-owned引用并取得lifecycle fence，再逐项结算/释放edge；共享blob只有在完整closure证明无active edge、hold、pin和publication后才可claim reclaim。
- 每个不可逆阶段执行前重新读取canonical lifecycle、edge revision、pin generation、hold和deletion fence。检查与unlink之间必须由repository deletion claim保护；claim失效则不执行。
- metadata tombstone先于物理blob reclaim，且必须足以阻止相同旧generation重新发布。物理删除部分成功时保持`IN_DOUBT`并从stage receipt继续，不回滚为“未删除”，不凭目录缺失宣称settled。
- Security clear可优先擦除获授权的敏感内容，但仍保留允许的最小digest、authority、stage receipt和审计事实；若内容同时受legal hold，冲突进入typed blocked disposition，不由maintenance自行裁决。

### 38.4 D01：现存Artifact、owner row与workspace联合迁移

scoped instance：`D01-artifact-ownership-and-deletion-v1-to-v2`，状态`CONFIRMED`。

- migration owner在Artifact store、Session cleanup、FileOps publication和GC activation前取得全store独占cutover generation；inventory仅覆盖Product批准的artifact/session roots，拒绝symlink、错误owner/mode、路径逃逸和未知布局。
- 联合枚举SQLite logical entries/owner rows/retention、publication outbox、CAS blobs、FileOps roots/snapshots、Session directories、tool result/task output与legacy overflow目录，以及所有已注册pin/root source；不能只迁SQLite或只扫文件。
- 可严格证明的owner转换为v2 typed edge，保留ArtifactId/digest、scope、retention和publication事实。旧枚举retention只作为迁移输入，不能虚构未存在的domain terminal/hold事实。
- 无法证明owner的blob进入`ORPHAN_QUARANTINED`迁移evidence，默认保留180天并等待bounded reconciliation；不得因为mtime老、目录名陌生或当前无内存pin直接删除。mtime只记录为`LEGACY_FILESYSTEM_LOWER_BOUND`。
- legacy cleanup stamp、touch time和调用参数式legal-hold不迁为authority。能够从canonical Product governance来源证明的hold才生成typed hold edge；不确定则阻断相关Session/Artifact删除。
- 缺blob、digest mismatch、重复identity不同内容、owner冲突、publication half-state、corrupt SQLite/manifest均fail closed并进入逐项conflict report；迁移不修补内容、不释放edge、不执行GC。
- candidate包含v2 metadata、edges、holds、deletion tombstones、orphan evidence、bounded index和completeness manifest。全部strict read-back后原子切换active store generation；普通writer不得产生v1/v2 mixed state。
- receipt保存source/target digest、各scope/retention/edge/blob/hold/conflict数量、orphan清单digest、cutover generation与implementation identity。v1 reader首次activation后只进入migration-only路径，不作生产fallback。

### 38.5 D02：Artifact内容、删除证据与orphan保留

scoped instance：`D02-artifact-retention-v2`，状态`CONFIRMED`。

- `EPHEMERAL`内容在所属turn/operation terminal且所有delivery/effect/publication结算后保留24小时；`SESSION`内容在Session terminal且外部edge结算后保留30天；`PROJECT`内容不采用“90天无访问”自动删除，只响应明确project deletion/retention command。
- `PINNED`、legal hold、active publication、active Workflow/delivery/effect、`IN_DOUBT`和owner conflict没有普通TTL。访问时间、mtime和磁盘压力不能降级这些保证。
- Tool/Model/ServiceCall等domain若在各自章节规定更长保留期，edge采用更严格期限；extension只能延长或收窄删除权限，不能缩短Product hard minimum。
- deletion tombstone与stage receipt在SETTLED后保留1年，至少含ArtifactId/digest、edge closure generation/digest、command/authority、claim fence、各stage disposition/instant和最终物理状态。
- orphan migration evidence与v1 source/manifest至少保留全部approved deployment完成cutover后180天；仍有冲突、hold或唯一审计价值者继续保留。source不是fallback reader。
- storage pressure达到hard limit只能停止新可产生artifact的admission并继续settlement/cleanup，不能驱逐已接受、active、held或IN_DOUBT内容。

### 38.6 D03：delete、hold、security clear与purge authority

scoped instance：`D03-artifact-and-session-deletion-authority-v1`，状态`CONFIRMED`。

- Product Session maintenance authority只可依据durable terminal/retention事实发出`TTL_EXPIRE`；用户删除由用户授权surface发出scope-bound `USER_DELETE`；security与legal-hold authority分别独立，maintenance不能解除hold或扩大security-clear范围。
- `runtime/session` current fenced owner验证Session lifecycle并发出edge-release/deletion intent；`runtime/artifacts` current fenced owner验证完整closure、执行metadata/GC状态机并返回typed receipt。
- Workflow、BackgroundTask、Tool、Model、ServiceCall、delivery、FileOps与publication owner只能提交/释放自己拥有的edge，不得直接删除blob或另一个domain的edge。
- CAS repository、workspace cleanup、disk pressure monitor、CLI/UI和测试helper没有业务purge权。测试只可通过`TEST_FIXTURE_CLEANUP`删除已证明属于隔离fixture scope的内容。
- hold release不等于delete；它只释放hold edge，之后重新参加正常retention与closure判定。user delete也不能越过legal hold、active external effect或未知publication result。
- corrupt entry隔离阻断相关artifact删除；若corruption使completeness manifest或generation不可验证，则停止整个generation的GC admission，但允许不扩大损害的查询和evidence capture。

### 38.7 D07：edge、closure、迁移、GC与存储边界

scoped instance：`D07-artifact-reachability-and-gc-bounds-v1`，状态`CONFIRMED`。

- 单artifact active edges默认1,000、hard max10,000；单Session active artifact edges默认100,000、hard max1,000,000。达到上限在edge durable commit前返回typed BACKPRESSURED，不接受后再遗漏引用。
- inline metadata最大64 KiB、单edge extension evidence最大16 KiB；大evidence另存Artifact并建立edge，identity/path/tag各自采用有界typed schema，不截断。
- closure/reconcile每批最多10,000 edges、wall-time 5秒并持久cursor；每个snapshot generation记录producer watermark。poison edge、locked owner或未完成producer不阻塞其他partition扫描，但阻断其覆盖范围的删除。
- deletion每批最多500 artifacts、单事务/manifest generation最多100、wall-time 5秒；单blob stage最多自动重试3次，随后进入`IN_DOUBT/FAILED`，不得跳过后宣称Session settled。
- repository capacity维护使用bounded durable index；repair full scan每轮最多10,000 paths并持久cursor。禁止每次GC或写入前无界遍历整个CAS并把全部digest驻内存。
- 默认soft capacity 10 GiB、hard capacity 100 GiB，由Product schema配置且有部署hard max；soft触发maintenance/backpressure，hard停止新content admission但继续edge release、reconcile和已接受settlement。
- deletion claim/lease默认30秒，refresh有界且每个不可逆stage复核fence；不得持OS文件锁跨网络/长await。claim丢失或clock identity异常即停止mutation。
- persistent retention/deletion instant使用timezone-aware AbsoluteInstant与clock identity；进程内扫描预算、lease等待和retry backoff使用monotonic clock。cleanup周期由durable scheduled fact推进，不再用stamp/目录mtime。

### 38.8 解除阻断、门禁与实施顺序

本轮关闭B10的canonical reachability、跨进程pin truth、Session删除原子性误述、mtime/cleanup stamp、legal-hold authority、fenced deletion claim、migration、retention和bounded scan阻断。Artifact/Session删除workstream已达到无需实施者临场决定即可拆单开工的状态。

独立requirements及顺序：

1. `R-W3-ARTIFACT-EDGE-001`：冻结v2 Artifact/Edge/Hold/DeletionCommand/Claim/Receipt、generation、strict codec和completeness manifest。
2. `R-W3-ARTIFACT-MIGRATION-001`：实现SQLite/CAS/Session/FileOps/outbox/root/pin只读inventory、conflict与orphan dry-run；依赖001。
3. `R-W3-ARTIFACT-MIGRATION-002`：实现v2 inactive candidate、typed edges、orphan quarantine、manifest read-back和forward cutover；依赖前两项。
4. `R-W3-ARTIFACT-STORE-001`：在现有artifact store内实现edge command/query、CAS/revision、hold、retention和bounded index；依赖003。
5. `R-W3-ARTIFACT-DELETION-001`：实现fenced deletion claim、分阶段metadata/blob reclaim、IN_DOUBT恢复和tombstone；依赖004。
6. `R-W3-SESSION-DELETION-001`：实现Session lifecycle fence、typed delete intent、edge逐项release与settlement；依赖004/005。
7. `R-W3-ARTIFACT-GC-001`：实现generation-complete closure、bounded cursor reconcile、capacity/backpressure与fault injection；依赖004/005。
8. `R-W3-ARTIFACT-CONSUMERS-001`：迁移Workflow/BackgroundTask/Tool/Model/ServiceCall/delivery/FileOps/publication的edge producer及transient pin projection；依赖004，并在005/007启用前完成。
9. `R-W3-WORKSPACE-CLEANUP-RETIRE-001`：迁移Product maintenance和测试fixture cleanup，删除mtime/stamp/参数式hold、直接`remove_tree`/`reclaim`及旧v1生产reader；依赖006/007/008，v1 source最终删除仍受180天proof约束。

首节点001；migration先于v2 writer；store contract完成后deletion、Session lifecycle与consumer迁移可按write-set并行，但GC activation必须等待全部canonical edge producer及completeness门禁。Hosted ServiceCall、Tool/Model result和Agent delivery只依赖EDGE-001即可先声明edge，不能以等待最终GC为由继续制造无owner artifact。

## 39. 第十六批已确认评审结论：durable scope扫尾与Session rollout闭合

结论：Presentation、machine-event projection和Notebook document不应各自获得durable store；它们分别是可重建Product投影、typed machine fact的消费链和Runtime checkpoint的结构化视图。为其新增journal会制造第二真相。真正剩余的durable scope是`runtime/session`拥有的Session rollout：当前`rollout.jsonl`已是canonical truth并具有checksum/fsync/expected-version，但generation 1 codec、笼统retention和目录式cleanup不足以闭合十年治理。以下适用性与Session决定均为`CONFIRMED`。

### 39.1 durable scope disposition清单

| Scope | Disposition | canonical truth与证据 |
|---|---|---|
| Product Presentation `ViewEvent`、TranscriptReducer与surface state | `NOT_APPLICABLE` | `product/presentation/state/driver.py`仅在内存fold并落到surface；断线/重启由Session/machine canonical facts重新projection。不得持久化ViewEvent或widget tree形成第二历史。 |
| ACP/AG-UI/Structured wire event | `NOT_APPLICABLE` | wire adapter是外部representation；连接delivery语义由各transport/Connection contract处理，wire payload不是Mote canonical durable fact。需要断线恢复时从Session projection和typed connection cursor恢复，不建立通用wire journal。 |
| Runtime machine event本身 | `SCOPED_BY_DOMAIN` | event envelope/dispatcher是机制，不是统一业务truth。Session/FileOps进入Session rollout；Tool/Model/effect、Agent delivery、Workflow、ServiceCall分别进入其domain owner；telemetry/progress若无domain承诺则best effort。不得建立全局“所有machine events”永久日志。 |
| Event subscription checkpoint/DLQ | `ALREADY_CLOSED` | 第23–24节已确认D01/D02/D03/D07及no-replay policy，不重复owner或迁移。 |
| Notebook document/cell/output | `NOT_APPLICABLE_AS_INDEPENDENT_STORE` | `runtime/interactive/kernel/driver.py`从validated Runtime checkpoint恢复Notebook document并推进kernel epoch；`notebook_export.py`是纯确定性export。checkpoint payload由Session fact与Artifact edge持有，Notebook不得另写`.ipynb`作为恢复truth。 |
| Notebook stdin | `ALREADY_CLOSED` | 第27节D20已闭合incarnation、reply CAS、password plaintext边界和typed receipt；普通input intent归Runtime operation/Session fact，不建Notebook输入journal。 |
| Runtime interactive checkpoint | `SCOPED_BY_SESSION` | checkpoint identity/codec/digest进入Session rollout，payload由Artifact store持有；其retention和删除服从本节Session stream与第38节Artifact edge，不能靠checkpoint文件或surface snapshot独立恢复。 |
| Session rollout/Event facts | `APPLICABLE` | `runtime/session/log.py`明确把每Session的`rollout.jsonl`作为append-only truth；`runtime/session/codec.py`当前只有store generation 1，故必须补齐下述D01/D02/D03/D07。 |

`NOT_APPLICABLE`只表示不建立独立durable lifecycle，并不放宽strict type、scope identity、wire version、secret redaction或delivery保证。若未来Product承诺跨断线逐帧重放Presentation，必须作为新的外部delivery domain重新准入，不能把当前ViewEvent缓存悄悄升级成truth。

### 39.2 Session rollout canonical边界

- `runtime/session`拥有SessionId、stream lifecycle、fact ordering、stream revision、strict codec、append CAS、replay、retention eligibility和stream deletion intent；`runtime/events.LocalEventJournal`只提供本地journal机制，不拥有Session业务状态机。
- FileOps facts虽由FileOps domain定义，其进入Session stream的稳定event type、ordering与stream retention由Session owner协调；FileOps effect/transaction自身不可被Session compaction改写为较弱事实。
- Session stream只持久化closed `SessionEvent` union。Presentation ViewEvent、surface/widget state、live Role/service/lock/task、provider object、plaintext secret、任意Python object和未知裸dict不得进入rollout。
- 大payload只保存canonical ArtifactRef/digest并提交第38节typed edge；fact append与edge publication必须使用durable intent/ack或同一可恢复事务，不能出现event已提交而payload可被GC、或artifact永久泄漏但event未提交。
- replay只从已验证的current generation、完整checksum chain和strict event codec产生projection。corruption、未知schema/tag、extra key和identity mismatch fail closed；不得跳过坏行继续构造看似完整Session。
- `SessionMetaEvent`保持唯一sequence 1；SessionId永不由目录名覆盖stream/envelope/meta中的冲突identity。stream revision、run lease fence和Session lifecycle generation共同约束append/delete，旧resident owner不得提交。

### 39.3 D01：Session rollout generation 1到generation 2

scoped instance：`D01-session-rollout-v1-to-v2`，状态`CONFIRMED`。

- Session migration owner在Session resume、Event Fabric binding、Runtime checkpoint open、FileOps recovery和任何v2 append前取得该Session的migration claim；全workspace inventory只由Product唯一入口调度，但不同Session可在独立fence下有界迁移。
- inventory核对approved sessions root内的目录、`rollout.jsonl`、run lease、projection/checkpoint metadata及Artifact roots；拒绝symlink、path escape、非法mode、重复SessionId目录、stream/meta/directory identity冲突和未知同级控制文件。
- v1逐行验证UTF-8、JSON shape、storage envelope version、sequence连续性、checksum chain、EventId唯一性、SessionId及唯一首条meta；当前journal允许的明确尾部torn-write语义必须与已fsync committed boundary区分，中段损坏和已承诺尾部损坏不得截断修复。
- 每个v1 event必须经当前authoritative event class严格decode后再encode为v2；未知tag/version、extra/missing field、primitive错误、FileOps非法transition或checkpoint/artifact identity mismatch使该Session进入`MIGRATION_BLOCKED`，不使用`str/int/bool`强转。
- v2 envelope加入store generation、stream lifecycle generation、producer domain/definition generation、明确clock identity及Artifact edge binding；保留原EventId、logical sequence、occurred instant和source digest，不虚构原记录没有的run/turn identity。
- v1内联大payload经验证后写入Artifact candidate并建立edge；candidate event只有在artifact content/digest与edge全部read-back后才可提交。迁移不启动Runtime、LLM、Tool、process或外部effect。
- candidate写入独立generation，完整verify/replay得到确定性projection digest后，以manifest/CAS原子切换该Session active generation。不得逐行原地改写或允许同一stream混读v1/v2。
- migration receipt至少保存source/target stream digest、event family/version/count、artifact extraction、clock/identity downgrade、conflict、projection digest、cutover generation和implementation identity。首次v2 activation后普通reader/writer仅支持v2；v1 decoder只在migration/forensics路径存在，并在退出条件满足后删除。
- corrupt或blocked Session保持原v1只读和evidence，不得静默新建空v2 Session、删除rollout或用旧reader恢复后继续append。

### 39.4 D02：Session完整stream、tombstone与migration evidence保留

scoped instance：`D02-session-rollout-retention-v2`，状态`CONFIRMED`。

- ACTIVE、DRAINING、recovery pending、runtime handoff pending、FileOps/effect/delivery/approval/Workflow引用未结算、`IN_DOUBT`或legal hold的Session完整stream不受普通TTL。
- Session达到terminal，且所有Artifact edge、runtime operation/checkpoint、FileOps transaction、Tool/Model effect、delivery、Workflow、approval和publication均结算后，完整stream默认保留30天；Product可在hard范围内延长，不能由Runtime/extension缩短。
- 30天后由typed compaction生成最小Session tombstone并保留1年。tombstone至少含SessionId、definition/config identity、stream generation/final revision/digest、创建/terminal instant、terminal disposition、run/incarnation最后generation、各domain settlement digest、Artifact closure digest、delete command与hold/security disposition。
- compaction不保留可独立恢复会话的半套event；完整stream删除后不能以tombstone resume。若Product承诺长期resume，应提升完整stream retention，而不是让replay猜测缺失状态。
- v1 source、migration candidate/manifest/receipt至少保留全部approved deployment完成cutover后180天；blocked/corrupt/hold的唯一evidence随Session保存。v1 source不是fallback reader。
- security clear可以按独立authority提前擦除获授权的用户内容/secret payload，保留允许的digest与receipt；不得删除唯一effect、approval或审计事实后宣称Session正常结算。

### 39.5 D03：Session terminal、compact、delete与purge authority

scoped instance：`D03-session-rollout-command-and-purge-authority-v1`，状态`CONFIRMED`。

- Session terminal由current fenced Session lifecycle owner依据canonical transition提交；UI、Role析构、连接关闭、目录cleanup、进程退出和Residency eviction都不能把Session标为terminal或删除stream。
- 正常TTL compact/delete只由Product Session maintenance generation发出typed command；`runtime/session` current fenced owner复核expected lifecycle/revision、run lease、pending domain settlement、Artifact closure和hold后执行并返回逐stage receipt。
- `USER_DELETE_SESSION`、`TTL_COMPACT_SESSION`、`SECURITY_CLEAR_SESSION`、`LEGAL_HOLD_APPLY/RELEASE`、`PURGE_SESSION_TOMBSTONE`和`TEST_FIXTURE_DELETE_SESSION`是不同command/authority；没有`force=True`、直接目录删除或“ignore errors视为成功”。
- Session owner只释放自身stream及Session-owned Artifact edges；其他domain引用由其owner结算。第38节Artifact owner执行共享blob reclaim，Session cleanup不得直接unlink CAS。
- legal hold阻断普通compact/delete且maintenance不能解除；hold release仅恢复retention评估。security clear与legal hold冲突进入typed blocked disposition，不默认任一方覆盖。
- 每个不可逆阶段绑定deletion claim、fence、expected revision与receipt；partial directory/source deletion进入`IN_DOUBT`，保留已发生事实并可恢复，不重建同SessionId空目录冒充回滚。

### 39.6 D07：Session append、stream、replay与maintenance边界

scoped instance：`D07-session-rollout-bounds-v2`，状态`CONFIRMED`。

- 单fact semantic inline payload最大1 MiB，单storage record hard max2 MiB；更大内容必须使用ArtifactRef。现有journal 64 MiB record/256 MiB batch仅作为底层绝对防御上限，不是Session可用contract，Session writer必须先执行较小上限。
- 单append默认1 fact，内部原子transaction batch最多100 facts且总inline最大8 MiB；任何调用者不得用底层256 MiB batch上限绕过admission。
- 单Session完整stream默认soft 256 MiB、hard 1 GiB或1,000,000 facts（先到者）；全deployment默认soft 10 GiB、hard 100 GiB。Product schema可在部署hard max内选择，Runtime和extension只能收窄。
- soft limit触发checkpoint/Artifact外置、用户可见pressure和新turn backpressure；hard limit在新turn/新大payload durable accept前拒绝，但仍允许terminal、cancel、settlement、hold和maintenance必要facts。不得丢弃旧committed fact腾位后继续接受。
- recovery/replay每批最多10,000 facts或5秒并持久/可重建cursor；projection activation只在完整stream verification后发生。poison Session隔离自身，不阻断其他Session；全store manifest/identity冲突则停止相关root admission。
- migration/compaction每批最多100 Sessions、每Session每轮最多10,000 facts、wall-time 5秒；单次candidate最大1 GiB，超过按有界分段manifest处理，不把全workspace日志读入内存。
- Session listing/maintenance每页最多500，full repair scan每轮最多10,000目录并持久cursor；禁止无界`glob`后整体排序/驻内存。
- append lease默认30秒并有界refresh；本地file lock不跨LLM、Tool、网络或长await。persistent event/retention instant使用AbsoluteInstant与clock identity，进程内lock/retry/scan预算使用monotonic。
- append fsync/parent durability失败不推进公开revision；projection/checkpoint落后通过durable cursor重建。达到bounds、corruption、stale fence和clock异常均返回typed disposition，不fallback到内存-only accepted。

### 39.7 解除阻断、门禁与实施顺序

本轮关闭durable scope“是否遗漏独立store”的歧义，并关闭Session rollout migration、retention、authority、bounds和Artifact binding。Presentation/Notebook明确不建立平行truth；Session rollout workstream达到无需实施者临场决定即可拆单开工的状态。

独立requirements及顺序：

1. `R-W3-SESSION-STREAM-001`：冻结v2 envelope、stream lifecycle、strict event codec、Artifact binding、storage/retention policy和typed errors。
2. `R-W3-SESSION-MIGRATION-001`：实现v1 rollout/lease/checkpoint/artifact-root只读inventory、strict validation、conflict与dry-run；依赖001及`R-W3-ARTIFACT-EDGE-001`。
3. `R-W3-SESSION-MIGRATION-002`：实现v2 inactive candidate、Artifact extraction/edges、projection digest、manifest/CAS cutover；依赖前两项及Artifact store cutover。
4. `R-W3-SESSION-STORE-001`：实现v2 append CAS/fence、bounds、typed query/replay cursor和lifecycle command；依赖003。
5. `R-W3-SESSION-RETENTION-001`：实现terminal eligibility、compact/delete state、tombstone、hold/security/user/TTL authority与fault recovery；依赖004和第38节Artifact deletion contract。
6. `R-W3-SESSION-PROJECTIONS-001`：迁移Runtime checkpoint、FileOps、machine-event projection、ACP/AG-UI resume及Notebook restore只从v2 verified stream重建；依赖004。
7. `R-W3-SESSION-LEGACY-RETIRE-001`：删除v1生产reader/writer、目录/mtime cleanup与任何Presentation/Notebook旁路恢复truth；依赖005/006，v1 source物理退出仍受180天proof约束。

首节点SESSION-STREAM-001；它与Artifact EDGE contract共同冻结后才能迁移。Presentation scope workstream可继续按第30节独立推进类型/wire兼容，但不阻塞Session store；Notebook stdin按第27节推进，Notebook restore consumer必须等待v2 verified projection。至此已识别durable scope均有`APPLICABLE`、`ALREADY_CLOSED`、`SCOPED_BY_DOMAIN`或`NOT_APPLICABLE` disposition，不再遗留“实施时决定是否持久化”的开放项。

## 40. 最终全局收口：当前有效结论、唯一writer与开工DAG

### 40.1 最终判定

**REVIEW CLOSED / READY TO IMPLEMENT IN ORDER**。

截至本节，原需求涉及的B1–B37及深审新增子问题，已经具备以下开工前信息：canonical owner、复用/拒绝复用结论、contract/schema方向、scoped product decision、旧数据处置、retention、delete/command authority、failure/IN_DOUBT policy、hard bounds、独立requirement和跨requirement依赖。不存在需要实施者临场决定的兼容策略、数据丢弃策略、第二真相、权限扩大或无界默认值。

该结论的准确含义是：可以从下述Wave 0开始，按DAG逐项修改生产代码；不是允许把B1–B37合并成一个分支、一次性并行写同一owner，亦不是跳过migration dry-run、唯一writer登记或每项验收门禁。实际丢弃用户数据、新增外部兼容SLO、新付费依赖或扩大权限/网络暴露仍需新的明确授权；当前审核没有作出这些授权。

### 40.2 文档时间截面与supersession规则

第1–28节保留各轮发现、当时状态和决策演进作为审计记录，不回写成“当时已经确认”。实施和准入只读取以下当前有效规则：

1. 同一事项以编号更大的后续章节为准；本节是全局状态总表。
2. 第19节“仅Wave 0可开工”、第22.3/24.3节“全项目尚未全面开工”、第25–28节`PROPOSED/待确认`均为历史时间截面，已被第29节确认授权及第30–39节scoped结论`SUPERSEDED`。
3. 第21.9、23.7、26.4、27和28节的“需用户确认/推荐回复”不再是当前action item；对应决定分别由第22、24、29节明确确认为`CONFIRMED`。
4. 旧段落中的“推荐instance/推荐决定”若已有后续同identity确认，以后续`CONFIRMED`语义为准；没有独立durable scope的对象按第39.1节disposition处理，不可据旧问题描述另建store。
5. B编号是finding/覆盖索引，不是实施owner；D编号是scoped decision，不是代码ticket；只有`R-*` requirement取得write-set lease后可以写生产代码。

因此，全文搜索仍会看到“待确认”“尚未准入”等词，但它们不表示当前开放阻断。authoritative ledger必须记录其disposition为`SUPERSEDED`并链接取代章节，不能删除历史后伪装决策从未变化。

### 40.3 B1–B37覆盖与requirement归属

最终覆盖按owner而不是按旧F/B大桶组织：

| 覆盖集合 | 最终requirement归属 | 当前状态 |
|---|---|---|
| B1/B33/B37及D21确认的dead/public seams | `R-W1-001..006`对应的breaking removal单 | `READY_AFTER_W0` |
| B9/B16 registry、loader、mutable composition seam | W1 owner-specific removal/composition单；不得建通用registry | `READY_AFTER_W0` |
| B7/B8 Presentation/scope，B31 LSP | `R-W2-PRESENTATION-001..003`、`R-W2-LSP-001..003` | `READY_AFTER_W0` |
| B12/B17/B30/B32及daemon/Event决定 | owner-specific W2/W3 requirements、`R-W3-DAEMON-001`、`R-W3-EVENT-001` | `READY_AFTER_W0` |
| B25/B34 Workflow/effect/Temporal | `R-W3-WORKFLOW-*`、`R-W3-TOOL-EFFECT-001` | `READY_AFTER_SHARED_CONTRACTS` |
| B28 Cron | `R-W3-CRON-*` | `READY_AFTER_CRON-001` |
| B27 Agent ingress/delivery/turn | `R-W3-AGENT-*` | `READY_AFTER_AGENT-INGRESS-001` |
| B29 OAuth | `R-W3-OAUTH-*` | `READY_AFTER_OAUTH-001` |
| B11/B18 journal/codec横切索引 | 不建B-owned实现；分别归`R-W3-RUNJOURNAL-*`及各domain migration，B项只做最终coverage gate | `NO_INDEPENDENT_OWNER` |
| B36 hosted service-call | `R-W3-SERVICE-CALL-*` | `READY_AFTER_SERVICE-CALL-001` |
| B10 Artifact/cleanup | `R-W3-ARTIFACT-*`、`R-W3-WORKSPACE-CLEANUP-RETIRE-001` | `READY_AFTER_ARTIFACT-EDGE-001` |
| Session rollout、checkpoint、Notebook restore扫尾 | `R-W3-SESSION-*` | `READY_AFTER_SESSION-STREAM-001_AND_ARTIFACT_EDGE` |
| 其余B项的contract/type/process/permission/composition缺口 | 已由第6节独立模板、第15–18节ledger门禁及第21–30节对应scoped decisions约束；在Wave 0拆成owner-specific `R-W1/R-W2`原子项，不允许回落为B编号巨型ticket | `READY_FOR_W0_ASSIGNMENT` |

任何原子evidence若在Wave 0发现不能映射到上述owner，门禁必须fail closed：先补一个owner-specific requirement和依赖边，不把它塞入`ARTIFACT-CONSUMERS`、`PROJECTIONS`、`governance`或“misc cleanup”。这属于需求机械实例化，不重新打开已确认的产品语义；只有触发40.1列出的外部授权事项才重新打开评审。

### 40.4 唯一writer与跨包write-set矩阵

下表规定逻辑write-set owner。具体文件列表在Wave 0按当时源码baseline展开，但不得改变owner；同一行同一时间只能有一个active writer lease。

| Canonical write-set | 唯一writer requirement族 | 允许的消费者改动方式 | 互斥/顺序 |
|---|---|---|---|
| governance schema、finding/decision/coverage ledger与validator | `R-W0-GOVERNANCE-001`（由第15–18节正式实例化） | 其他ticket只提交typed evidence/result artifact | W0先完成；不得由测试直接推进ledger状态 |
| Contracts公共DTO/Port/codec declaration | 对应domain首个`*-001` contract requirement | consumer requirement只在contract合入后迁移调用，不重复定义类型 | 同一module按requirement lease串行；共享identity变更先于所有producer/consumer |
| `runtime/events` subscription state | `R-W3-EVENT-001` | Product仅composition/config，subscriber仅经Port | 与其他修改`runtime/events`同文件的W2项串行 |
| Workflow run/effect state | `R-W3-WORKFLOW-EFFECT-*`/migration | Tool/Temporal只写各自adapter和edge | contract→migration→writer activation；RunJournal retirement最后 |
| Tool effect/execution chokepoint | `R-W3-TOOL-EFFECT-001`及Tool owner单 | Workflow/ServiceCall提交typed command，不写Tool store | 与Permission/Hook/runner触及相同pipeline文件串行 |
| Cron schedule/occurrence store | `R-W3-CRON-*` | delivery/artifact只实现Port binding | 001→002→003/004；旧store retirement最后 |
| Agent delivery store | `R-W3-AGENT-DELIVERY-001` | Cron/Workflow/Product surface提交delivery command | migration切换前禁止新writer；projection/reconciler后退旧入口 |
| Agent turn queue | `R-W3-AGENT-TURN-001` | delivery只做prepare/bind/settlement协议 | 与delivery共享事务contract但不共享内部store writer |
| OAuth metadata/secret/effect | `R-W3-OAUTH-STORE/EFFECT/COMMAND-*`按第35节阶段 | MCP/Model只持borrow capability | store与effect触及manager文件时串行；consumer最后 |
| RunJournal legacy source | `R-W3-RUNJOURNAL-MIGRATION/RETIRE-*` | target domain writer拥有目标记录；migration不修改目标状态机源码 | inventory等待target contract；cutover等待target writer；retire最后 |
| hosted service-call | `R-W3-SERVICE-CALL-*` | Tool/Product只经capability-scoped Port | contract→migration→store→execution/reconcile→consumer→retire |
| Artifact metadata/edge/CAS/GC | `R-W3-ARTIFACT-*` | domain producer拥有自己的源码并调用Artifact Port | `ARTIFACT-CONSUMERS-001`是集成验收协调项，不取得其他domain文件writer权 |
| Session rollout/lifecycle | `R-W3-SESSION-*` | FileOps/checkpoint/Notebook/ACP/AG-UI各由自身consumer ticket迁移 | `SESSION-DELETION-001`并入Session retention阶段的同一writer lease，不与其并行改store |
| workspace cleanup | `R-W3-WORKSPACE-CLEANUP-RETIRE-001` | 只在Session/Artifact新路径启用后删除旁路 | 必须等待Session retention、Artifact GC和consumer edges完成 |
| Product presentation、LSP与各external wire | 各自`R-W2-PRESENTATION-*`、`R-W2-LSP-*` | Contracts scope/profile先行；各external adapter拥有自己的wire文件 | Presentation与LSP可并行；同一Product composition文件由W0登记串行窗口 |

跨domain requirement不得通过“一张ticket方便完成”同时取得两行canonical store的writer权。真正需要原子协议时，先由消费方Contracts Port冻结prepare/commit/receipt，再由两侧owner分别实现；集成requirement只运行协议和故障验收，不成为第三writer。

### 40.5 全局contract与实施DAG

最终开工顺序按下列stage执行。stage内仅在write-set不相交且前置contract已合入时并行：

```text
S0 Governance baseline/ledger/recipe catalog/failing gates
  -> S1 direct removals + foundational typed contracts
       -> S2 dry-run inventories and inactive migration candidates
            -> S3 canonical stores/commands/fences
                 -> S4 execution/reconciliation/projections/consumers
                      -> S5 cutover, old-entry retirement, retention clocks
                           -> S6 integrated architecture/fault/scale acceptance
```

具体关键路径：

1. **S0**：建立`R-W0-GOVERNANCE-001`，生成source/evidence manifest、atomic finding ledger、scoped decision ledger、production-capable recipe catalog、write-set leases和B26先失败门禁。它是唯一的全局开工前置，不修改domain生产语义。
2. **S1**：并行推进已确认breaking removal的`R-W1-*`，以及LSP/Presentation、Workflow effect、Cron、Agent ingress、OAuth、ServiceCall、Artifact edge、Session stream等首contract节点。`ARTIFACT-EDGE-001`必须先于所有持久payload producer；`AGENT-INGRESS-001`先于Cron/Workflow delivery integration；`TOOL-EFFECT-001`先于Workflow/ServiceCall effect integration。
3. **S2**：各D01 migration先只读inventory/dry-run，再生成inactive candidate。Session migration等待Artifact edge/store；RunJournal migration等待Tool/Model/Session timer目标contract；任何corruption/conflict不得用清空或fallback越过。
4. **S3**：按owner启用canonical store、CAS/fence、typed command/query和retention metadata。migration active cutover必须在新writer activation前完成；不存在同一deployment长期v1/v2双写双读。
5. **S4**：实现Workflow/Tool/Service execution、Agent/turn reconciliation、GC、Session projection及Product consumers。Cron/Workflow terminal delivery等待Agent delivery；Artifact GC activation等待所有producer completeness watermark；Notebook restore只读verified Session v2 projection。
6. **S5**：完成consumer/public-surface migration后删除RunJournal、旧service journal、旧OAuth backend/fallback、旧Cron/Agent/Session reader、mtime/stamp/direct-delete及其他compat residue。需要180天proof的source先进入retention clock，生产reader立即退出，物理source按已确认期限后删。
7. **S6**：在冻结integration source identity上执行每domainstrict codec、migration replay/read-back、stale fence、fsync/partial failure、IN_DOUBT、capacity/backpressure、clock、legal hold和reconcile fault injection；再执行production-capable recipe、公共面、依赖层级、无局部import/动态旁路、唯一入口和retired symbol全局门禁。

不存在一个要求所有S1节点完成后才开始任何S2的全局barrier；DAG按domain流水推进。但共享contract、目标migration和write-set lease的显式依赖不可因“其他domain已准备好”跳过。

### 40.6 每个requirement的开工与关闭门禁

一个`R-*`只有同时满足下列条件才从`READY`进入`IN_PROGRESS`：

- stable requirement identity/revision、scoped decisions和authoritative owner已登记；
- production source baseline仍匹配，漂移已重新inventory；
- 精确write set取得唯一writer lease，无重叠active writer；
- 现有基础设施搜索/复用证据和拒绝复用理由完整；
- contract deliverable、migration/failure/retention/authority/bounds及依赖均来自本审核，无开放`TODO/TBD`；
- 先失败fixture或现状反证可复现，且不依赖修改后的测试自证。

关闭该requirement必须证明：contract/owner/composition/lifecycle/persistence/observability/tests在该切片闭合；所有仓内consumer已迁移；旧入口、alias、fallback、re-export、dead type、migration residue在其退出阶段删除；定向类型/测试、architecture gate和适用fault injection通过；ledger由reviewed evidence推进为`VERIFIED`。测试、关键词扫描、注释或“当前调用不到”均不能单独关闭finding。

最终项目关闭还要求所有未obsolete atomic evidence均为`VERIFIED`，没有`OPEN/ASSIGNED/IMPLEMENTED`遗留；所有`OBSOLETE`都有source漂移和不适用证明；D01–D21适用scope均链接`CONFIRMED`或明确`NOT_APPLICABLE/SUPERSEDED`；全仓测试若受环境阻断，必须记录阻断、受影响适用集合及等价证据，不能把未运行写成通过。

### 40.7 剩余项性质

当前剩余工作全部是**实施工作**，不是需求评审开放项：

- Wave 0把本审核中的requirement草案、decision和write-set机械写入authoritative ledger；
- 按实时源码baseline展开精确文件列表和atomic evidence identity；
- 实现、迁移、验证并按retention退出旧source。

若Wave 0仅发现文件移动、symbol已被用户改动或某反证已自然消失，更新locator/digest并给出`OBSOLETE`证据即可；若发现新的canonical owner、外部承诺、真实数据丢弃、依赖或权限变化轴，则创建新finding并只重新打开受影响scope，不推翻无关workstream的已关闭评审。

## 41. 整合实施需求回归审核

审核对象：`zdocs/post-closure-boundary-debt-implementation-requirements.md`。

### 41.1 结论

**存在实质回归，当前整合稿不能独立替代第29–40节作为唯一实施基线。**

整合稿正确保留了五层依赖、单owner、strict codec、forward-only migration、effect capability、主要workstream、Artifact/Session truth分离及大部分首节点，方向没有整体倒退。但“实施基线”要求读者无需返回长审核稿即可确定migration、retention、authority、bounds和DAG；当前稿在这些位置把已确认合同压缩成概括性语句，另有两处直接改变原结论。状态应标记为`REVIEW_REOPENED_FOR_INTEGRATION_REGRESSION`，只阻断把该文件发布为唯一实施依据，不推翻第30–40节已经确认的domain决定。

必须修复下列P0/P1问题并重新逐项diff后，才能恢复`REVIEW CLOSED / READY TO IMPLEMENT IN ORDER`。

### 41.2 P0：S0全局前置被弱化为“Wave不构成全局barrier”

整合稿第3节先画出`Governance/source baseline -> 每个workstream首节点`，随后又写“Wave名称不构成全局barrier”。这会让实施者把S0理解为普通contract依赖，甚至先修改production再补baseline。

第40.5节的确认决定是：

- `R-W0-GOVERNANCE-001`是**唯一全局开工前置**；
- S0完成后，各domain可流水推进，不要求所有S1完成才开始任一S2；
- “无全局barrier”只适用于S0之后的同stage/跨domain流水，不适用于S0本身。

修复要求：整合稿必须逐字区分“`S0` mandatory global prerequisite”和“`S1–S6`无全局齐步barrier”。在S0完成前只允许只读inventory/评审，不允许production write。当前表述为开工顺序P0回归。

### 41.3 P0：OAuth把敏感明文退出错误地绑定180天proof

整合稿`R-W3-OAUTH-RETIRE-001`写为“cutover与180天proof后删除plaintext v1、fallback、nullable-token writer和migration reader”。这把四类生命周期不同的对象合并：

- access/refresh secret material在成功替换、consumer borrow与revoke/refresh settlement闭合后，默认24小时内擦除；
- metadata、最小tombstone、migration manifest与冲突evidence按第35节各自期限保存；
- v1 source/proof的180天窗口不能授权继续保存可解密plaintext token；
- production fallback/writer在cutover同一迁移切片退出，不等待180天；等待窗口只约束migration-only source/evidence的物理退出。

修复要求：拆成`SECRET_ERASURE`、`PRODUCTION_PATH_RETIREMENT`和`MIGRATION_EVIDENCE_RETIREMENT`三个明确阶段。禁止以审计窗口延长秘密材料寿命。这是安全与retention合同P0回归。

### 41.4 P0：已关闭的Model durable迁移选择被重新开放

整合稿`R-W3-MODEL-PERSISTENCE-MIGRATION-001`要求“完成一次性migration或明确拒绝”。“或”把旧数据处置再次留给实施者，与第40.1节“不存在需要实施者临场决定的数据处置”冲突。

修复要求：对inventory中的每一种authoritative inference JSONL/SQLite restore record写出唯一disposition、candidate schema、conflict结果和退出条件。若某source只是假投影/无consumer，应明确`RETIRE_AS_NON_AUTHORITATIVE`；若是canonical result/effect evidence，应明确forward migration；corrupt/unknown应明确blocked evidence。不得保留“实现时选择迁移或拒绝”的分支。

在该disposition补齐前，Model checkpoint/persistence workstream不得进入writer或migration cutover；只读inventory可继续。

### 41.5 P0：硬数值合同大面积丢失，且第17节形成自相矛盾

整合稿第17节称“本文列出的数值是hard实施合同”，但正文没有列出大多数已确认D07数值，只写“执行已冻结的Product bounds”或“bounded”。若该稿是直接实施依据，实施者无法知道默认值、hard max、batch、retry、deadline、retention和storage limit，只能回查审核稿或重新选择。

至少缺失以下已确认合同：

- Agent ingress：delivery/turn容量、inline payload、artifact threshold、claim/retry/scan/compaction、WDRR weight/priority/deadline及30天/90天/1年retention；
- Workflow effect：payload/evidence、attempt/reconcile scan、lease与90天/1年retention；
- Cron：schedule/occurrence容量、单轮claim、retry/backoff、payload、scan/storage，以及occurrence完整30天、tombstone180天、`IN_DOUBT`无TTL；
- OAuth：active subject、secret/metadata大小、refresh/revoke attempt、borrow TTL、scan/compaction、metadata 90天与tombstone1年；
- Hosted ServiceCall：每root/deployment active cap、1 MiB/64 KiB payload/receipt/response、capability-dependent attempts、poll/query窗口、caller deadline、stream/store/index limits、90天/1年retention；
- Artifact：edge cap、64/16 KiB inline、10,000-edge closure、500/100 deletion batches、10/100 GiB storage、30秒claim及orphan180天；
- Session rollout：1/2 MiB semantic/storage record、100 facts/8 MiB batch、256 MiB/1 GiB/1,000,000 facts、10/100 GiB deployment、10,000-fact replay、500 listing和30秒lease；
- Event/daemon：Event的65,536 subscription、retry/timeout/error/page、1 MiB DLQ、1,000/100 maintenance；daemon的64 KiB discovery、128 candidates、3 retries、0.1/0.5/2秒与10秒batch；
- Connection、Notebook stdin、LSP等第25–30节已确认的timeout、frame、depth/item和generation bounds。

修复要求：整合稿必须包含逐workstream versioned bounds表，列`default / hard max / owner / exceeded disposition / shrink-only rule`。不能用交叉引用“已冻结”替代直接实施合同；若选择引用审核稿，则该文件不能宣称自己是直接、独立实施基线。

### 41.6 P1：requirement identity被合并/改名但没有supersession映射

第40节要求stable requirement identity与content revision分离。整合稿发生以下变化：

- 原`R-W1-001..006`被合成`R-W1-DEAD-SURFACES-001`，一张需求横跨Model、HTTP interface、provider、Runtime registry、Temporal/Squilla和i18n多个bounded context；
- Workflow原有`R-W3-WORKFLOW-EFFECT-002/003`等被改为`RECONCILIATION/INSPECTION`，但未声明一对一/拆分映射；
- 新增`R-W0-WORKFLOW-GOVERNANCE-VERIFY-001`、`R-W0-BGTASK-GOVERNANCE-VERIFY-001`及若干W2节点，却没有说明它们来自哪些B evidence和是否取代旧ID；
- `R-W3-SESSION-DELETION-001`与`R-W3-SESSION-RETENTION-001`仍可能同时修改Session lifecycle/store，但整合稿没有承接第40.4节“同一writer lease、不得并行”的裁决。

修复要求：增加完整`old requirement -> integrated requirement -> disposition`表，状态只能是`PRESERVED / SPLIT_INTO / MERGED_INTO / SUPERSEDED`并给理由。跨bounded context的直接删除仍须拆成独立requirement；总览可使用epic identity，但epic不得取得production write lease。

### 41.7 P1：唯一writer规则退化为“编码前再提交”

整合稿第2.1节要求每个工作包编码前提交write set和唯一writer，但没有携带第40.4节已经确认的逻辑write-set矩阵。这会把owner冲突重新推迟到实施准入时，尤其是：

- `ARTIFACT-CONSUMERS-001`可能被理解为可以修改Workflow/Tool/Model/ServiceCall/delivery所有producer文件；
- RunJournal migration可能同时修改三个target domain store；
- Session deletion/retention、Artifact deletion/GC及workspace cleanup可能并发写相同lifecycle或cleanup文件；
- Product composition是LSP、Presentation、Tool、OAuth、ServiceCall和Session surface的共享热点。

修复要求：原样承接第40.4节矩阵并补充具体integrated requirement ID。集成/consumer requirement只协调Port和验收，不自动取得其他domain源码writer权；target domain owner修改自己的producer，跨owner原子性通过Contracts prepare/commit/receipt表达。

### 41.8 P1：domain migration/retention/authority被过度摘要

以下段落方向正确，但不足以独立实施：

- Agent ingress只列步骤，遗漏v1 delivery/mailbox/turn逐类迁移规则、30/90/365天retention、owner action封闭集合和capacity-before-accept；
- Workflow只写“v2/RunJournal inventory与v3 cutover”，遗漏Temporal history只是attempt evidence、`RunJournal("application-workflow-effects")`退出、capability downgrade与unknown-effect迁移；
- Cron未写TaskId 128-bit migration、occurrence disposition、purge authority和mtime lower-bound evidence规则；
- OAuth未写selector/file/keyring/config/vault逐类candidate/conflict，backend必须由Product冻结且无fallback；
- ServiceCall未写PLANNED/STARTED/receipt/`.cancel`/owner/index逐状态迁移、closed lifecycle/owner-action集合和purge authority；
- Artifact未写orphan quarantine、mtime只能作legacy lower-bound、completeness producer manifest及typed command authority；
- Session未写v1逐行identity/checksum/torn-write判定、blocked只读、security/hold/user/TTL命令分离；
- Event/daemon一行式描述不能替代已确认的D01/D02/D03/D06/D07/D10/D19实例。

修复要求：每个durable章节至少增加四张紧凑表：`legacy state disposition`、`retention`、`command/purge authority`、`bounds`。通用协议只能消除重复文字，不能取代domain-specific语义。

### 41.9 P1：删除需求再次形成跨域巨型切片

`R-W1-DEAD-SURFACES-001`同时列出六类不同owner的删除，随后只写“每个bounded context独立签收”。这仍是一张跨域write ticket，与第3、6、40节“每个合入切片独立闭合”和第40.3节owner-specific W1单相冲突。

修复要求：把它降为无write权限的epic/index，并恢复至少以下独立ticket：Model client、inference HTTP admin、provider moderation、Runtime AES registry、Temporal/Squilla loader、i18n registry。每项分别登记consumer/public/export/docs/plugin审计与D21 retirement authority，不以一个epic的`VERIFIED`代替六项关闭。

### 41.10 P2：若干状态/术语需消除歧义

- OAuth第11节写“ABSENT只为query结果”，而第35节closed state清单包含ABSENT。应明确ABSENT是canonical query disposition还是可持久metadata state，并让contract/codec只选择一种，避免同名双语义。
- BackgroundTask首节点放在W0但后续是W2 implementation；需明确W0只验证现状、不以现有测试直接标记生产治理已闭合。
- 第18节要求“旧fixture为零”过宽；应删除依赖旧入口/旧schema的fixture，保留或迁移仍证明canonical行为的测试，不能为了关键词清零删除有效反例。
- “deployment proof后退役”必须区分production reader/writer立即退出、migration-only decoder和source physical retention；不能让proof window变成长期生产兼容。
- `R-W2-NOTEBOOK-001`把Canvas union与Notebook stdin合在一个ID，二者owner/consumer/lifecycle不同；应拆为document codec与stdin incarnation两个requirement或明确无共享write set的epic关系。

### 41.11 未回归且应保留的内容

以下整合是正确的，不应因修复上述问题重写：

- 五层依赖、单canonical owner、无compat/fallback/双读双写的总目标；
- 通用forward-only migration骨架及effect四类capability；
- Product唯一composition root和projection不可反写truth；
- Agent delivery与turn双owner的prepare/bind/commit及settlement协议；
- Workflow与BackgroundTask严格分离；
- RunJournal按Tool/Model/Session timer分治而不建v2；
- ServiceCall caller deadline不终止remote operation，pending index仅为projection；
- Artifact typed edge/completeness/fenced deletion及Session只释放自身edge；
- Session rollout是唯一Session durable truth；Presentation/wire/Notebook/ipynb不建独立durable truth；
- Runtime maintenance按domain拆分，不建新的万能Manager；
- 最终验收要求strict codec、stale fence、crash、capacity、production recipe和无第二入口。

### 41.12 回归修复后的复审门禁

整合稿恢复“实施基线”状态前必须机械通过：

1. 所有第40节stable `R-*`均在identity mapping表中有唯一disposition，无静默消失或无映射改名；
2. 第29–39节所有`CONFIRMED` scoped decision均映射到一个实施章节和至少一个requirement；
3. 每个适用D01/D02/D03/D07 scope在整合稿中能直接找到migration、retention、authority和数值bounds；
4. 第40.4节每个canonical write-set有唯一integrated writer族，epic/integration requirement无越权write lease；
5. S0明确为唯一全局production-write前置，S0之后才允许domain流水并行；
6. OAuth secret erasure、production path retirement和migration evidence retention完全分离；
7. 全文不存在“迁移或拒绝”“按已冻结bounds”“由实施时确认”“必要时兼容”等重新开放选择；
8. 整合稿自身能独立回答实施者的问题，不依赖口头历史或长审核稿补足关键合同。

当前允许继续的工作仅限：修订整合Markdown、生成identity/decision/bounds/write-set对照及只读源码再基线。不得依据当前整合稿启动production writer、migration cutover或destructive cleanup。
