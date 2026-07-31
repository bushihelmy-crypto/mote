# 动态边界、Product 依赖与事件治理实施规范

规范状态：架构批准，实施 Gate 未就绪  
代码合规状态：迁移中  
Hard Gate 状态：由第 13 节声明所指向的生成状态工件分别记录  
基线日期：2026-07-31  
适用范围：`contracts/`、`kernel/`、`runtime/`、`orchestration/`、`product/` 及其架构门禁

## 1. 目标状态

本文统一治理事件语义、持久化、投递、动态类型边界、Product 依赖、运行时装配、兼容迁移和派生工件。目标不是建立一套人工维护的架构 CMDB，而是让每个关键事实只有一个权威来源，让每次跨边界转换都可定位、可测试、可删除。

“唯一”始终由复合治理键限定，不表示全系统只有一种格式、一个实例或一个物理 root：

| 治理对象 | 唯一性键 | 不变量 |
| --- | --- | --- |
| Durable activation | `(logical_store, event_family)` 或其 `cutover_unit_id` | 一个 active generation，且该 generation 只有一个 active writer/schema |
| Canonical read service | `(logical_store, deployed_generation)` | 一条对业务公开的 canonical read path |
| Wire contract | `(api_id, deployed_protocol_generation)` | 一个 authoritative contract |
| Construction recipe | `(capability_id, applicable_root, deployment_mode, instance_scope)` | 一条 canonical recipe |
| Runtime lifecycle | `runtime_instance_id` | 一个 lifecycle owner |
| Representation conversion | `(bounded_domain, source_stage, target_stage)` | 一个 canonical conversion owner |

同一 capability 可以为 embedded Application、shared daemon 或 optional host 声明不同 recipe；test double 不属于 production recipe。唯一性是在键内成立，不跨 bounded domain、logical store、cutover unit、root 或 deployment mode 误判。`store_generation` 是 activation 的结果，不能用于把跨 generation 双写隔离成两个各自“唯一”的 writer。

迁移可以短期引入旧格式 reader、upcaster、adapter、alias 或双读，但不能双写，也不能把迁移设施永久化。所有临时路径必须有存量证据、责任 owner、截止日期、删除条件和逾期 hard fail。

最终原则是：

> 可迁移，不永久兼容；可归档，不污染在线 Runtime；可短期双读，不允许双写；迁移结束后代码、声明、fixture、测试和装配一起删除。

## 2. 已确认的架构决议

1. 事件的语义权威性、持久化、投递可靠性和表示阶段是四个正交治理维度，不是必须加入每个事件 dataclass 的字段。
2. EventFabric 不在运行时证明 `UncommittedFact` 的 codec provenance；写入权由窄业务 API、构造点限制、import/type gate 和测试闭包保证。
3. 具有阶段语义的顶层消息只能属于一个 representation stage；稳定、不可变且无 transport/presentation 行为的 value object 可以跨阶段组合。
4. Durable codec/catalog 按 bounded domain 所有；Runtime journal 不建立全局领域 event switch。
5. Python typed declaration 是 codec、policy 和 composition capability 的权威源；JSON/YAML/Markdown 是派生工件，默认不参与运行时路由或装配。
6. Telemetry 的异构 binding 只在 Runtime 私有边界做受控存在类型擦除，公开 API 不暴露 `Any` binding。
7. Recoverable subscriber 的副作用只允许 pure reducer、transactional projection、幂等 external effect 或 outbox/inbox。
8. LSP 可靠收敛由独立 ADR 设计；本文只冻结其权威输入与 advisory/observational 输出边界。
9. Product owner/unit scope 采用父级继承，仅稳定跨 owner 边界和高风险装配入口显式声明。
10. Product static runtime-import graph 保持零 SCC；type-only、export、composition 分别建模，不混成一张伪完整图。
11. 内建组件从包扫描、import side effect 和可变全局 registry 迁移到显式 immutable Application catalog；外部插件通过声明式 metadata 和隔离加载进入 per-Application snapshot。
12. Facade 分为 `canonical`、`compatibility`、`internal_aggregation`；compatibility consumer 只减不增，归零即删除。
13. 同一生产能力只有一条 canonical 新写入、新调用、新注册和新装配路径，禁止双写、双发、双注册和双装配。
14. Governed production boundary 不暴露 `Any`，生产代码不使用局部 import、动态 re-export、反射或 service locator 隐藏 owner 和依赖。
15. 每项 enabled infrastructure capability 必须从适用的 Product-owned canonical production root 可达，并只有一条 canonical construction path 和一个 lifecycle owner。
16. 同一语义事实只有一个 authoritative source；其他等价表示由确定性生成器派生，无法生成时由单向 conformance gate 证明实现符合 authority，不能获得共同决策权。
17. 历史 decoder、upcaster、旧格式 adapter 和迁移程序是有期限的迁移设施，不是永久 durable contract。在线 Runtime 的最终状态只保留 active writer、active decoder、canonical model 和 canonical read service。
18. Store schema 切换必须通过持久化 writer fence、quiesce、验证和原子 generation activation；rollback 可以回滚部署，但不得恢复旧格式写入，数据问题只允许 forward recovery。
19. 架构决议不能由普通 store ADR 绕过；本规范范围内不批准双写。需要改变单一 authoritative writer 原则时，必须先修改并重新批准本规范。

## 3. 源码事实与治理边界

### 3.1 当前事件链已存在正确的反腐层

Session 持久化已经把运行时语义事件与持久化 payload 分开：

```text
MessageAppendedEvent
  ├─> telemetry / presentation consumer
  └─> SessionFactCommitter
        -> MessageEvent
        -> domain codec
        -> UncommittedFact
        -> EventEnvelope
        -> subscriber / reducer
```

治理应固化这一显式转换链，而不是让同一个对象同时成为 observation、persisted payload、view model 和 wire contract。一个语义事实可以合法拥有多个目标 projection；唯一性约束作用于每个 source-stage → target-stage 路径，而不是要求一个 source 只能有一个 projection。

### 3.2 投递可靠性与事实持久性不同

`SubscriptionSpec` 的 `LIVE / LOSSY / RELIABLE / DURABLE` 描述 subscriber 的 checkpoint、重试和失败语义。`Reliability.DURABLE` 不表示收到的 Python 对象天然是 durable domain fact。

### 3.3 Runtime transport 不拥有领域 schema

Journal/EventFabric 负责：

- 顺序、完整性和 checksum；
- CAS、fsync 和物理 storage format；
- envelope 与 subscriber lifecycle；
- checkpoint、retry、quarantine 或 fail-closed。

Domain 负责：

- logical event family 与 payload schema；
- active encoder/decoder；
- migration 与数据验证；
- sensitivity、大小、retention requirement、redaction 和 artifact policy；
- reducer 和 semantic compatibility。

### 3.4 Product 图的证明能力不同

| 图 | 可证明程度 | 用途 |
| --- | --- | --- |
| static runtime-import | 高，可近似完备 | 分层、初始化顺序、SCC |
| type-only | 高，可近似完备 | 类型依赖和抽象泄漏 |
| public API/export | 中 | facade、symbol definition、owner |
| governed composition seams | 仅覆盖声明的高风险 seam | loader、registry、plugin、factory、lifecycle transfer |
| production composition declaration | 对受治理 capability 必须完整 | root 到实现的构造与生命周期闭包 |

Governed composition seams 不恢复任意运行时对象引用；production composition declaration 则必须覆盖受治理基础设施能力。不能用“运行时图无法静态完整恢复”作为不验证 capability 可达性的理由。

## 4. 事件治理模型

### 4.1 四个正交轴

#### Semantic authority

| 值 | 含义 | 约束 |
| --- | --- | --- |
| `authoritative` | 建立或改变权威业务状态 | 不得只经 loss-tolerant telemetry |
| `advisory` | 影响质量、上下文或后续决策 | 可缺失，但不能是不可恢复状态的唯一真相源 |
| `observational` | trace、metrics、日志、UI mirror | 缺失、重复、乱序和 handler 失败不得改变业务状态 |

#### Persistence

| 值 | 含义 |
| --- | --- |
| `transient` | 不进入 durable store |
| `journaled` | 经 domain codec 写入 Mote journal |
| `externally_durable` | 由外部系统提供 durability，并声明 reconciliation contract |

Persistence 属于 projection/codec edge，不是源语义事件的固有属性。

#### Delivery

| 机制 | 失败语义 | 合法用途 |
| --- | --- | --- |
| direct call/transaction | 同步返回或抛出 | 正确性边界 |
| telemetry | best effort/drop/degrade | advisory、observation |
| `LIVE` | 不跨重启补偿，失败推进 | live mirror |
| `LOSSY` | 可丢弃，失败推进 | 可损失派生视图 |
| `RELIABLE` | retry，poison event 可 quarantine 后推进 | 可隔离失败的可靠 projection |
| `DURABLE` | retry，最终 fail-closed，不推进 checkpoint | 必须追平的 projection/barrier |

#### Representation

```text
runtime semantic type
  -> persisted domain DTO
  -> UncommittedFact / EventEnvelope
  -> current domain/read model
  -> ViewEvent / machine projection
  -> interface-owned wire payload
```

具有阶段语义的顶层 object 不得跨阶段复用。`FileVersion`、`ContentIdentity`、`ArtifactRef`、typed ID、enum 和 timestamp 等稳定 immutable value object 可以被多阶段组合，但必须有明确 owner、序列化规则和演进策略。Gate 检查顶层 message/event/DTO，不递归禁止字段类型复用。

### 4.2 转换边界

每个跨阶段路径必须有显式、类型化、单向且可测试的 translator/projector/codec/adapter。以下行为禁止：

- transport envelope 直接成为领域模型；
- persisted aggregate 直接进入 View；
- ViewEvent 直接成为外部 wire contract；
- `dict[str, Any]` 或开放 object graph 跨阶段传播；
- 一个阶段消息兼任 command、fact、projection 和 wire payload；
- 通过 re-export 掩盖 defining module 或 owner。

### 4.3 Transformation inventory

Inventory 是从 typed declarations、源码索引、codec catalog 和 `SubscriptionSpec` 生成或校验的聚合视图，不是第二真相源。只有源码无法表达的 authority、owner 和 side-effect policy 进入小型 manifest。

聚合视图应解析：source type、authority、conversion owner、persisted type、event identity、subscription、reducer、view/wire projection、side-effect policy、sensitivity/retention 和 owner。不得为了填表而复制已有 delivery/persistence 配置。

## 5. Durable 数据、版本与迁移窗口

### 5.1 Per-domain typed catalog

每个 bounded domain 自有不可变 typed codec declarations。Callable 直接以 Python symbol 引用；Runtime journal 不依赖跨领域全局 registry。

示意：

```python
MESSAGE_ACTIVE = EventCodecEntry(
    event_type=EventType("mote.session.message"),
    event_schema_version=4,
    state=CodecState.ACTIVE,
    encoder=encode_message_v4,
    decoder=decode_message_v4,
    policy=MESSAGE_STORAGE_POLICY,
)
```

JSON/YAML 可以作为 CI artifact、审阅快照或跨语言 schema，但不得通过字符串名称反向路由 encoder/decoder。

### 5.2 Admission

- 业务 API 接收领域 DTO，不接收 `UncommittedFact`；
- `UncommittedFact(...)` 只允许出现在批准的 domain encoder、journal infrastructure 和隔离测试 fixture；
- audit stream 必须有封闭 audit DTO 和 codec，禁止任意 `str(object)`；
- EventFabric 不增加 provenance token 或万能 runtime metadata；
- 构造点限制由 import/type/AST gate 和负例测试执行。

### 5.3 版本术语

| 名称 | Owner | 含义 |
| --- | --- | --- |
| `storage_format_version` | journal backend | record/checksum/JSONL 等物理格式 |
| `event_schema_version` | domain | event payload 版本 |
| `embedded_contract_version` | value-object/domain owner | payload 内嵌 contract 版本 |
| `projection_version` | projector owner | read model/checkpoint 结构版本 |
| wire protocol version | interface owner | HTTP/gRPC/ACP/AG-UI 等外部格式 |

这些版本独立演进，不使用一个全局 schema version 联动。

### 5.4 Codec generation declaration

Codec generation 与 migration debt 分开声明。“Current”不同时表示候选和已激活版本。Codec generation 使用封闭状态：

| 状态 | 规则 |
| --- | --- |
| `active` | 由 `(logical_store, event_family)` 或 cutover-unit activation record 唯一选中的生产读写版本 |
| `candidate` | codec、migration 和 validator 已部署但未激活；不得承接生产读写 |
| `migrating` | 禁止写入；读取权限由 migration mode 决定；必须有 deadline 和删除计划 |
| `retired` | 数据已迁移、归档或销毁；生产代码、声明和 fixture 已删除，仅留审计证据 |

`archived` 不是 online catalog 状态。归档数据进入独立 archive manifest，且先转换为第 5.10 节规定的 canonical archival format。`retired` 也是审计结果，不在生产 registry 中保留空壳对象。Event type 若禁止复用，由生成的历史分配账本记录，不保留 executable tombstone、decoder 或空类型。

每项 active/candidate codec generation declaration 包含 schema identity、owner、encoder/decoder、validation policy、target store generation、activation prerequisite 和 admission policy。Candidate 不携带旧数据 remaining count、`last_write_at`、archive/destroy policy 或 deletion deadline。

迁移期间每个 logical store/event family 或 cutover unit 始终只有一个 active generation 和 active writer。`candidate` 和 `migrating` type 不得成为新业务 API、subscriber 或 projection 的直接依赖。

### 5.5 Migration debt declaration

Migration debt declaration 只描述 source generation 中待删除的 `migrating` reader/adapter/schema，不适用于 candidate。每项必须声明：

- `owner_id`；
- `last_write_at`；
- 由存储事实生成的 `remaining_record_count` 和 `remaining_stream_count`；
- `migration_strategy` 与可恢复 `migration_job`；
- `verification_method`；
- `deadline`；
- `deletion_change`；
- `archive_or_destroy_policy`；
- rollback/forward-recovery 边界；
- 观察期与退出证据。

Migration debt 还必须包含 `logical_store`、`cutover_unit_id`、`source_generation`、inventory snapshot ID、扫描口径和 deletion evidence。Source debt 清零后该声明与旧实现一起删除。

存量计数不能手写，必须由实际 store、stream index 或可验证扫描生成。趋势比较只能在相同 `(logical_store, source_generation, inventory_scope)` 下进行；备份恢复、compaction 或扫描范围变化必须生成新 snapshot lineage，不能把不同口径数字当作单调序列。迁移窗口日常 gate 检查不增长、计划进度、deadline、无新消费者和 writer fence；“remaining count 为零”只在完成态 hard fail。

### 5.6 Cutover declaration 与原子边界

Cutover declaration 独立描述 source → target，至少包含：

- `cutover_unit_id` 与 `logical_store`；
- `included_event_families`；
- `source_generation` 与 `target_generation`；
- `shared_sequence_domain`、`shared_checksum_domain`、`shared_checkpoint_domain` 和 transaction boundary；
- migration mode、writer fence、lease/quiesce policy；
- activation record、forward-recovery owner、cleanup prerequisite。

Cutover unit 是最小原子 activation 边界。同一 sequence、checksum、checkpoint 或 transaction domain 内的 event family 必须属于同一个 cutover unit，整体复制、验证和激活；不能只切其中一个 family 后宣称 store generation 已整体激活。完全独立的 family 只有在 store 明确提供独立 sequence/checkpoint/transaction domain 时才可使用不同 cutover unit。Projection/checkpoint 必须声明归属 cutover unit 或独立迁移映射。

Activation record 按 `cutover_unit_id` 指向唯一 active generation，并由此约束 included families 的 active writer/schema。

### 5.7 Migration mode

每个 cutover unit 必须显式选择一种模式：

| 模式 | 旧数据是否可供在线业务读取 | 约束 |
| --- | --- | --- |
| `offline_cutover` | 否 | quiesce 后停机迁移，适合可停机且数据量受控的 store |
| `generation_copy` | 是，但只能经唯一 canonical read service | read-online/write-offline：source generation 继续可读；从 writer fence 到 activation 暂停生产写入 |
| `archive_export` | 否 | 转换为 canonical archival format 后退出在线业务 |
| `destroy_on_expiry` | 否 | retention 已到期并经授权销毁 |

`migrating` 的 reader 权限由该模式决定。`generation_copy` 只保证读服务持续可用，不保证写服务持续可用；migration infrastructure 内部可有 source/candidate readers，但业务层始终只看到 source generation 的 active canonical read service，直至原子 activation。

`offline_cutover` 与 `generation_copy` 都必须声明最大 write-unavailable window、drain deadline、拒绝新写入的 typed error/retry semantics、在途请求完成或失败规则、background writer stop/lease proof，以及 CLI/UI/health API 的只读状态。超过窗口进入 post-fence blocked 状态，不能悄然延长停写。

需要在迁移窗口持续写入，或无法建立停写/quiesce 窗口的 cutover unit，不得套用通用流程，必须先通过独立 online-migration ADR。ADR 必须证明始终只有一个 authoritative writer，并定义 snapshot high-water mark、change capture/log shipping 或 write-forwarding、delta ordering、幂等复制、read-your-writes、统一 canonical read overlay、崩溃恢复和 activation。本规范范围内不批准双写；普通 ADR 无权例外。确需改变该原则时，必须先修改并重新批准本规范。

### 5.8 Store Cutover Protocol

每次 cutover 由 store owner 执行以下持久化状态机：

| 状态 | 进入条件与不变量 |
| --- | --- |
| `PREPARED` | Candidate codec、migration job、validator、forward-repair tool 已部署；production routing 仍指向 active generation |
| `WRITER_FENCED` | 以持久化 CAS 提升 writer generation；旧 generation token/lease 的写入被 store 拒绝，而非仅依赖进程约定 |
| `QUIESCED` | 所有旧 writer lease 已失效或被逐一证明停止；固定 inventory snapshot ID 与 source-generation high-water mark |
| `MIGRATED` | 数据及引用迁移完成；记录、语义、摘要、sequence/event identity、checkpoint、projection、DLQ/quarantine、backup、artifact 与 secondary copy 校验通过 |
| `ACTIVATED` | 以单一原子 generation record 同时切换 canonical reader/writer；所有新 writer 必须携带 active generation token |
| `OBSERVED` | 监控和恢复演练通过；只允许 active-format forward repair，不重新启用旧 writer |
| `CLEANED` | 删除旧 reader/upcaster/adapter/catalog/fixture、旧实现测试和主生产树迁移程序；保留不 import 旧代码的审计证据 |
| `ABORTED_PRE_FENCE` | Fence 前 candidate 部署或验证失败；可安全销毁 candidate，active generation 不变 |
| `BLOCKED_POST_FENCE` | Fence 后迁移无法继续；保持 fence 与只读/停写 health，等待具名 owner forward repair |
| `FAILED_VALIDATION` | 数据已复制但完整性验证失败；禁止 activation，保持 fence 并修复/重跑 |
| `CLEANUP_BLOCKED` | 已 activation，但仍有旧消费者、引用或证据缺失；active generation 继续服务，旧代码不得恢复写入 |

Writer fence 的 authority 是 store 中的持久化 generation/CAS record。CLI、daemon 和其他进程启动或续租时必须校验 generation；仅关闭已知进程、发布新二进制或写配置不构成 fence 证明。Inventory 在 `QUIESCED` 后绑定 generation 与 high-water mark，迁移期间的所有变化必须由所选 mode 明确定义。

关键规则：

- 在 `ACTIVATED` 前 active writer 不变；candidate writer 不能生产写入；
- `ACTIVATED` 是 reader/writer routing 的原子切换点，不能先切一半；
- event identity、sequence 和 checksum 若不能原样保持，必须由 store-specific ADR 定义映射、引用改写和 subscriber checkpoint 迁移；
- rollback 可以回滚部署或停用 candidate 服务，但不能降低 writer fence 或恢复旧格式写入；
- activation 后发现问题只通过 active schema 的 forward repair、补迁移或新 generation 修复；
- migration job 必须可重入、可 checkpoint、可从最后确认位置恢复；
- 观察期结束后才进入 cleanup，但观察期不等于允许 fallback-to-old。

每个失败状态声明 health、operator query、推进/重试 authority、deadline escalation 和 candidate disposal policy。只有 `PREPARED` 在 fence 前可以转为 `ABORTED_PRE_FENCE` 并销毁 candidate；fence 后状态不得回退到 `PREPARED`。人工 override 必须审计且只能推进 forward repair、延长具名短期 deadline 或安全停服，不能降低 generation fence 或恢复旧 writer。

合法 transition 是封闭集合：

```text
PREPARED            -> WRITER_FENCED | ABORTED_PRE_FENCE
WRITER_FENCED       -> QUIESCED | BLOCKED_POST_FENCE
QUIESCED            -> MIGRATED | FAILED_VALIDATION | BLOCKED_POST_FENCE
FAILED_VALIDATION   -> MIGRATED | BLOCKED_POST_FENCE
BLOCKED_POST_FENCE  -> QUIESCED | MIGRATED | FAILED_VALIDATION
MIGRATED            -> ACTIVATED
ACTIVATED           -> OBSERVED | CLEANUP_BLOCKED
OBSERVED            -> CLEANED | CLEANUP_BLOCKED
CLEANUP_BLOCKED     -> OBSERVED | CLEANED
```

`MIGRATED` 只表示复制与全部 prerequisite validation 已通过；复制完成但验证失败直接进入 `FAILED_VALIDATION`。`BLOCKED_POST_FENCE` 的恢复目标必须等于 transition evidence 所记录的最后一个已验证 checkpoint，不可任意跳转。`ABORTED_PRE_FENCE` 与 `CLEANED` 是终态。

每次 transition 追加不可变 history record，包含 previous/next state、expected activation generation、CAS revision、actor/owner、timestamp、prerequisite evidence digests、failure reason 和 retry/checkpoint reference。Cutover checker 必须重放并验证完整 history、CAS 单调性、状态边合法性和 prerequisite，不得只检查当前状态快照。

### 5.9 标准迁移流程

标准顺序为：声明 mode → 部署 candidate → fence writers → quiesce/fix inventory → migrate → 完整验证 → 原子 activate → observe/forward repair → cleanup。旧格式绝不再写入，不成为新业务 API 输入，不增加消费者，也不叠加无限 upcast 链。新版本必须同时提交旧版本退出计划；前一迁移债务未清零不得继续叠加版本，紧急安全版本只有短期具名 hard deadline。

删除“旧测试”专指依赖旧生产实现的 fixture/test。转换向量摘要、迁移报告、数据校验结果和恢复演练证据作为不可变审计材料保留，但不得 import 已删除 decoder。

### 5.10 Canonical archival format

归档入口先把业务数据转换为单一、版本受治理的 canonical archival representation。系统长期维护一个 archive reader，而不是为每个业务 schema 或 batch 永久维护 executable reader。Archive package 可以包含：

- canonical archival representation；
- WORM 原始字节；
- 原始 schema 文本；
- hash/signature；
- conversion provenance；
- retention、legal hold 与 destruction metadata。

归档领域自身声明 archival-format authority、archival generation、active archive reader、migration verifier、crypto policy 和 destruction policy。归档系统同样只有一个 active archival representation 和 reader；新 archival generation 发布时，旧 canonical archives 按等价的 generation migration/cutover/verification 流程清零，随后删除旧 archive reader。Legal hold 可以阻止数据销毁，但不能自动阻止 representation migration；若法规要求原始位级不变，则 WORM bytes 作为 evidence 保留，active archive reader 不把它们当在线读取源。Hash/signature/encryption 算法退役时使用保留 provenance 的重新封装或重签流程。

在线 Runtime 不 import、装配或调用 archive reader。若法律要求原始格式可重放，执行环境必须作为不可变、离线验证的合规制品封存，并与对应数据具有相同的销毁期限；它是有预算、有期限的 legal archival obligation，不是 current product capability 或无限维护的软件分支。需要当前产品在线恢复的数据不是冷归档，必须迁移到 active online format。

### 5.11 Restore Admission Protocol

所有能够重新进入生产的数据副本都必须经过统一 restore admission，包括 backup、snapshot、delayed replica、离线节点磁盘、export/session bundle、灾备介质、DLQ/quarantine 原始 payload、artifact 内嵌 payload，以及测试或演练环境持有的生产 snapshot。每个 restore-capable copy 必须携带：

- `logical_store` 与 `cutover_unit_id`；
- source generation、schema/storage format；
- creation timestamp 与 authority digest；
- included sequence/checkpoint domain 与 high-water mark；
- retention、legal-hold、destruction metadata；
- restore-conversion contract identity（如适用）。

Restore 入口在读取或挂载前验证 metadata、签名和 generation。非 active generation 不得直接挂载、注册为 replica、恢复 writer lease 或暴露给在线 Runtime。旧副本只能：

1. 在网络和权限隔离的 staging 环境恢复；
2. 通过已批准的 restore-time migration 转换成新的 active-compatible generation；
3. 执行与正常 cutover 等价的 identity、sequence、checksum、checkpoint、projection、DLQ、artifact、secondary-copy 和安全验证；
4. 以新的 generation/CAS activation record 进入在线系统。

旧 decoder 从在线代码删除后，restore-time migration 只能使用 canonical archive converter、与副本同期限封存的离线恢复制品，或在 cleanup 前已将副本重写为 active/archival format。封存恢复制品不得被 current Application import/装配，并与对应副本共享权限、漏洞处置、演练和销毁期限。

`CLEANED` 的前置条件是所有 restore-capable copies 已满足以下之一：已重写为 active/archival format；已验证销毁；或绑定隔离、有期限且经过演练的 restore-time conversion contract。Delayed replica 或离线节点重新上线必须先走 restore admission，不能凭旧 lease/generation 加入集群。Restore checker 维护 restore-capable inventory；只清零活跃主库不足以完成 migration debt。

### 5.12 Retention、compaction 与安全

Retention 在 stream/store/session 级执行，不在 checksum append-only stream 中逐 event 删除。Catalog policy 应包含：

- sensitivity；
- semantic inline-size limit；
- stream/store retention requirement；
- redaction-at-source；
- compaction disposition；
- legal-hold behavior；
- artifact policy 与 reference lifetime；
- encryption/export/delete policy；
- DLQ/log/diagnostic secondary-copy policy。

不同 TTL 数据应进入不同 stream/store，或通过有校验的 snapshot + stream rewrite/migration 处理。删除 session/stream 前先释放 artifact ownership，再按引用关系回收 blob。

`PromptRejectedEvent` 等敏感事件不能仅凭类型名宣称 secret-safe；应持久化 redacted excerpt、digest 和 classification，而不是默认完整 prompt。Transport 的物理大小上限不能替代每类事实的语义上限。

### 5.13 Replay 与 recoverable side effect

Reducer 必须确定、无 IO、无外部副作用。Recoverable subscriber 使用封闭策略：

- `pure_reducer`；
- `transactional_projection`；
- `idempotent_external_effect`；
- `outbox_required`。

外部 effect 的默认 idempotency key 由稳定 `event_id + effect_kind + target` 派生。Effect 与 checkpoint 无法原子提交时必须允许安全重试。不支持幂等的外部目标不得由 recoverable subscriber 直接调用；`RELIABLE` quarantine 不等于 exactly-once。

## 6. Telemetry 与 LSP 边界

### 6.1 Typed-only telemetry

- Public registration/subscribe 是使用传统 `TypeVar` 的单 binding 泛型入口；
- Runtime-owned builder 逐项接收不同 `TypedTelemetryBinding[T]`，立即擦除为 private `_ErasedTelemetryBinding`；
- 异构 manifest 只保存 private erased binding，不伪装成 `TypedTelemetryBinding[object]`，不公开 `TypedTelemetryBinding[Any]`；
- erased 类型不 export，core 外引用 hard fail；
- all-events sink 使用诚实的 `TelemetryHandler[object]`；`accepts_all` 可表达订阅意图，但不是安全验证器；
- raw binding API 在 typed caller 迁移完成后删除。

### 6.2 LSP 权威边界

本文冻结：

> LSP 启用时，每个适用 LSP 的 confirmed file version 最终被 LSP 观察到，或系统进入显式、可查询的 degraded 状态。

- managed change 来自 File Operations committed fact；
- external change 来自 watcher 完成 attribution/invalidation 后的 confirmed transition；
- telemetry 只镜像状态；diagnostics 是 advisory output，不是 sync ack；
- 不得从 tool name、permission target 或 best-effort observation 推断权威版本；
- file commit 与 LSP 不组成分布式原子事务；
- 新可靠链完成后删除旧 telemetry inference pipeline。

Work identity、monotonic version、queue、coalescing、retry、reconciliation、shutdown、health、delete/rename/open-document semantics 由独立 LSP ADR 决定。现有模型没有 rename 时，验收不得先写“rename 正确”；ADR 必须先选择原子 rename、相关 delete+create 或 stable-identity transition pair。

## 7. Product Owner、依赖与 Facade

### 7.1 Owner/unit 继承

Product 一级 package 提供默认 `owner_id` 和 `kind`；子路径继承最近父 scope。仅以下情况显式声明 sub-unit：

- 跨 owner 的稳定 public surface；
- 独立 lifecycle/composition owner；
- delivery adapter；
- capability/use-case boundary；
- 单独受依赖矩阵治理的 volatile boundary；
- dynamic loading entry。

所有受治理 edge 的 source/target 必须通过最具体路径规则解析到唯一 active owner。内部文件无需逐项登记。Gate 失败条件是同等具体度重叠、孤立显式 scope、无效 replacement、retired owner 被新能力引用或受治理 edge 无法解析，不是“某文件没有 manifest row”。

Owner registry 只保存稳定 ID、状态和 replacement。负责人、路径和 CODEOWNERS 信息不复制到每个 manifest。Dynamic boundary 默认从 symbol path 推导 owner，只有跨 owner 时显式覆盖。

### 7.2 四类图

1. Static runtime-import graph：真实 runtime import，禁止 SCC 和分层反向边。
2. Type-only graph：独立分析 `TYPE_CHECKING` 等类型依赖。
3. Public API/export graph：分别记录 caller → facade、facade → defining symbol、symbol → active owner。
4. Governed composition seams graph：只记录 dynamic loader、global registry、plugin extension、跨 owner factory、lifecycle transfer 和 service locator lookup。

Composition edge 使用封闭角色：`declares | discovers | constructs | injects | starts | stops | owns_lifecycle | calls_port | publishes_to`。

允许 Host → Plugin `start()` 与 Plugin → Host port `publish()` 的双向通信。禁止的是 lifecycle 多 owner、ownership cycle、capability 反向构造/启动/停止 delivery adapter、plugin 自行寻找或销毁 composition root、registry declaration/snapshot owner 含混及未经声明的 service locator。

### 7.3 Facade

| 状态 | 规则 |
| --- | --- |
| `canonical` | owner 明确的窄公共入口，允许新调用方 |
| `compatibility` | exact consumer set 只减不增，有 owner、deadline、exit condition 和 deletion evidence |
| `internal_aggregation` | 仅 owner 内部使用，跨 owner import 失败 |

合法 re-export 仅限 canonical narrow facade、Contracts 稳定入口和明确 package public API。禁止错误 owner 重导出实现、连续多层 re-export、PEP 562 隐式暴露未声明 symbol、compatibility facade 新增 consumer，以及用 re-export 伪造依赖方向。

Runtime 对 Contracts event 的 compatibility re-export 按 exact consumer set 单向切换；归零后在同一里程碑删除，不留 alias 空壳。确有稳定公共价值的入口必须重新声明为 canonical facade，不能借 compatibility 名义续命。

### 7.4 耦合治理

- 移动职责到正确 owner 优先于抽取 Protocol；
- 只有真实跨层、多实现或测试替换 seam 才在 `contracts/ports/` 建 port；
- capability 不得 import delivery adapter；
- 不新增 `common/shared/utils/helpers`；
- fan-in/fan-out 用于评审，不以裸数量惩罚合理内聚；
- Product runtime-import graph 永久保持零 SCC。

## 8. Dynamic Boundary 与类型 Gate

### 8.1 Governed production boundary

以下 boundary 不允许 `Any` 或 `cast(Any, ...)`：

- public API 和 canonical facade；
- port/Protocol；
- event、command 和跨层 DTO；
- codec、projector 和 adapter 的内部输出；
- capability/composition declaration；
- telemetry typed registration；
- wire/application boundary 进入内部后的类型。

外部 SDK 原始响应、通用 JSON 或无类型第三方 API 可以在 owner 明确的 delivery adapter 内短暂为动态值，但必须以 `object` + 显式收窄、判别联合、validator 或 concrete model 在进入内部边界前解析，不能把动态性传播到下一层。

### 8.2 禁止边界逃逸

生产代码禁止：

- 函数、方法或类体内局部 import；
- 用 `Any`/`cast(Any, ...)` 压制类型不兼容；
- 非字面量且未声明的 dynamic import；
- 动态 re-export 或 PEP 562 隐式 API；
- 反射或 process-global service locator 装配能力；
- 通过 generated report/inventory 形成第二运行时配置源。

循环依赖通过移动职责、拆模块，或在真实跨层 seam 提取 port 解决。现有 local-import architecture test 可直接作为 hard gate。

### 8.3 Gate 证据

证据分为：

1. AST/import/type checker 可穷举的禁止语法；
2. strict positive/negative type cases；
3. 无法静态证明的动态入口 manifest 与 executable evidence。

动态例外必须具名、最小化、有 owner、理由、证据、expiry 和 removal condition。Expiry 前预警，到期 hard fail；不能以永久 allowlist 替代设计修复。

## 9. Composition 与 Production Reachability

### 9.1 Canonical production roots 与作用域

系统可以有多个 Product-owned root，例如主 Application/CLI、shared inference daemon、optional interface host。每项 capability 只需从其适用 root 可达，不强制所有能力进入一个物理 root。Runtime implementation 由 Product 装配，Runtime 不反向 import Product。

Composition 分为三个层次：capability declaration 描述“存在什么能力”；construction recipe 描述某个 `(capability, root, deployment_mode, instance_scope)` 如何构造；runtime instance 是一次实际构造结果并拥有唯一 lifecycle owner。一个 capability 可以拥有多个适用 root/mode recipe，但同一复合键内只能有一条 canonical recipe。

每项 capability declaration/recipe 至少包含：

- `capability_id`；
- `implementation_owner`；
- `applicable_root`；
- `enablement_predicate`；
- `canonical_factory`；
- `required_ports`；
- `instance_scope`；
- `lifecycle_owner`；
- `start_owner` 和 `stop_owner`；
- `status/replacement`。

### 9.2 Reachability 不变量

- enabled capability 从适用 root 经 canonical factory、port 或 immutable catalog 到达实现；
- canonical construction path 唯一；
- lifecycle owner 唯一，start/stop 责任闭合；
- optional/lazy capability 有明确 predicate 和 factory，但不要求 eager instantiate；
- production implementation 无 root path 时是 orphan，不能合入；
- 不存在 import side effect、module singleton 或 service locator 第二装配路径；
- external plugin 只经 validated per-Application snapshot 可达；
- test-only implementation 和 disabled capability 不误判为生产 orphan。

首批覆盖 model runtime、telemetry、EventFabric/journal、session persistence、artifact store、tool executor/catalog、permission/sandbox、background task、LSP、file watch、hook、hosted service gateway、agent catalog/factory、optional interface host 和 inference daemon infrastructure。

### 9.3 Capability declaration 完整性

Declaration 不能自行证明完整性。必须由独立、版本化的 candidate-source classifier 生成生产候选集合，再与 composition declarations 双向比较：

```text
versioned source classifiers -> discovered production candidates
                                      ⇅ exact identity match
typed composition declarations -> declared capabilities/recipes
```

候选来源使用少量封闭角色，不扫描所有 class 猜测 capability：

- `governed_port_implementation`：实现受治理 `contracts/ports` 的生产类型；
- `infrastructure_factory`：Runtime/Orchestration infrastructure scope 中显式标注的 factory；
- `lifecycle_factory`：明确产生 start/stop/aclose lifecycle 对象的 factory；
- `production_root_reference`：被 production root、factory 或 immutable catalog 引用；
- `bundled_discovery_result`：迁移期动态发现找到的 built-in component；
- `plugin_extension_declaration`：Product-owned plugin/catalog entry；
- `explicit_capability`：typed capability decorator/declaration。

Classification role 和 scope 是 typed、版本化的 authority；普通类名、`start` 方法文本或目录位置本身不能自动把所有 class 判为 capability。Checker 必须验证：

- `declared - discovered = ∅`：不存在虚假、过期或指向缺失实现的声明；
- `discovered - declared = ∅`：不存在 classifier 覆盖范围内的 orphan、隐藏装配或未治理生产能力；
- 同一 candidate identity 不被多个不兼容 role 重复分类；
- classification 规则变化会更新 source digest 和 evidence。

为封闭危险入口集合，所有 Runtime/Orchestration public factory/export 必须被显式分类为以下之一：

- `production_capability`；
- `internal_factory`；
- `external_adapter`；
- `test_only`。

未分类 public factory/export hard fail；`internal_factory` 被 Product root 或 production catalog 引用时 hard fail；登记为 infrastructure scope 的未分类 lifecycle factory hard fail。私有且不可达的普通实现不被伪称为“已证明非 orphan”，而由独立 dead-code/export/reachability inventory 治理。本文只证明 production-candidate classifier 闭包内不存在 orphan capability。

### 9.4 Dynamic discovery 退出

Built-in Agent/Tool/Provider 最终来自显式 immutable Application-owned catalog。`pkgutil.walk_packages`、全包 import side effect、decorator global registry 只能作为有 deadline 的迁移路径。External plugin 使用声明式 metadata/entry point、验证、隔离加载和 immutable per-Application snapshot。

## 10. 唯一生产路径与债务清零

### 10.1 禁止双轨

同一能力不得同时存在：

- 两个接受新 caller 的 API；
- 新旧 event 双写或双发；
- 两条 pipeline 发布同一 authoritative fact；
- 旧 registry 与新 catalog 同时用于 production discovery；
- Product root 与 module singleton 同时构造同一能力；
- 两个 lifecycle owner；
- generated manifest 与 handwritten manifest 同时影响运行时；
- 两份手写 IDL/OpenAPI/schema/DTO；
- 无限期 capability alias、config fallback 或 feature flag 分支。

### 10.2 临时路径由 typed debt role 声明

Debt inventory 由封闭角色声明驱动，不扫描变量名、注释或字符串中的 `fallback`、`compatibility`、`legacy`、`v1` 等词。必须清零的角色是：

- `migration_reader`；
- `compatibility_facade`；
- `deprecated_api`；
- `temporary_alias`；
- `rollout_flag`；
- `legacy_writer`；
- `temporary_discovery_path`；
- `temporary_construction_path`。

每项记录 exact consumer/data set、owner、禁止增长 gate、deadline、exit condition、deletion change 和验证证据。角色适用于 event/storage schema、API、facade/import path、registry、composition、config migration、feature rollout、LSP replacement pipeline、telemetry raw API 和业务 `UncommittedFact` 构造点。

以下长期结构不是 migration debt，不强制删除期限：`resilience_fallback`、`protocol_compatibility_adapter`、`input_decoding_fallback`、`optional_capability_degradation`、`current_versioned_wire_contract`。它们仍必须有 owner、typed declaration、测试、安全/资源策略和明确语义，且不能借长期角色承担旧系统迁移流量。

### 10.3 最终完成态

| 对象 | 最终状态 |
| --- | --- |
| Event/storage schema | 活跃 store 只含 active format |
| API/facade/import path | 只保留 canonical entry |
| Registry/discovery | immutable Application catalog |
| Composition | Product root canonical path |
| IDL/OpenAPI | 一个 authority，其余生成或单向 conformance-check |
| Capability inventory | 从 typed composition declaration 派生 |
| Configuration | 旧字段迁移后删除 parser、alias 和 fallback |
| Feature flag | rollout 后删除 flag 与两侧旧分支 |
| LSP | 可靠链建立后删除 telemetry inference 链 |
| Telemetry | typed API 完成后删除 raw binding |
| `UncommittedFact` | 业务构造点归零，仅 domain codec/infrastructure 构造 |

## 11. 权威源与派生工件

### 11.1 核心不变量

同一语义只能有一个 authoritative source。其他表示优先确定性生成；无法直接生成时，它只能是从 authority 人工实现的单向 adapter，并由 authority 产生的 conformance vectors/schema assertions 证明符合合同。Adapter 的修改不能反向改变 authority，也不能通过“两边一起修改并让 parity 通过”取得共同决策权。兼容性检查可以评估新 authority 是否破坏外部合同，但不创造第二 authority。权威源按 bounded domain 和 production root 分治，不建立 mega-schema。

| 语义 | 权威源 | 派生工件 |
| --- | --- | --- |
| Durable event | domain typed `EventCodecEntry` | JSON/YAML catalog、schema/migration report |
| Transformation chain | codec/translator/subscription declarations | inventory、关系图 |
| Composition capability | per-root typed immutable declaration | capability/reachability/lifecycle report |
| Public API | canonical export declaration + source symbol | export graph、API docs |
| Gate status | executable check + raw evidence | JSON/Markdown dashboard |
| Plugin | plugin metadata/entry point | validated Application snapshot |
| Wire API | 选定的 IDL、typed contract 或 OpenAPI | 其他 wire models、client、server、compatibility report |

### 11.2 派生工件声明

每个 artifact 声明 authoritative source、owner、generator、generator version、canonicalization、source digest、output format、committed/CI-only、stale detection、runtime-input permission 和 replacement/deletion policy。

Generated documentation、inventory、graph、dashboard 和 review snapshot 默认不得作为运行时输入。Runtime 直接消费 authoritative typed declaration。Gate report 只能描述 executable evidence，不能手写 `passed` 或 `complete`。Committed generated artifact stale 时 CI hard fail。

### 11.3 Wire authority

每个 API 单独选择：

- contract-first：IDL 是 authority；
- code-first：typed request/response 是 authority；
- 外部标准指定：OpenAPI 可以是 authority。

其余表示生成；技术上无法生成时，人工 adapter 通过从 authority 单向导出的 conformance vectors/schema assertions 验证。禁止双向 parity 把两个手写表示提升为共同事实源，也禁止同时手写等价 Pydantic model、IDL、OpenAPI、client DTO 和 gateway capability list。

## 12. 自动门禁

### 12.1 Event transformation gate

- 顶层阶段消息只有一个 representation stage；
- 共享 value object immutable、无阶段行为且 owner/serialization 明确；
- 每个 source-stage → target-stage 有唯一 canonical conversion owner；
- authoritative change 无 telemetry-only path；
- reducer 确定、无 IO 和外部副作用；
- wire adapter 不直接消费 telemetry object 或 persisted DTO。

### 12.2 Durable admission、安全与 retention gate

- 非批准位置不能构造 `UncommittedFact`；
- 每个 `(logical_store, event_family)` 或 cutover unit 只有一个 active generation；该 generation 只有一个 active writer/schema；
- policy 包含 sensitivity、semantic size、redaction、artifact 和 stream/store retention requirement；
- secret-prohibited validator 有正反测试；
- DLQ/log/diagnostic 不复制受限 payload；
- retention policy 与承载 store 的物理删除/compaction 能力一致。

### 12.3 Historical debt zero gate

- 新写入绝不产生旧版本；
- 每个 `migrating` entry 有非空 deadline、migration job、验证和 deletion change；
- remaining record/stream count 从固定 store generation、inventory snapshot lineage 和统一扫描口径的 storage evidence 生成；同口径窗口内不得增长，并须满足计划下降目标；
- `WRITER_FENCED` 后旧 generation 写入 hard fail，`QUIESCED` 前不得固定最终 inventory；
- cutover state 只能按状态机前进；activation 后不得重新启用旧 writer，只能 forward recovery；
- deadline 到达且存量非零时 hard fail；
- 存量归零后仍保留 decoder/upcaster/adapter/catalog/fixture 时 hard fail；
- 不允许无限链式 upcast；
- 新业务代码不能依赖 migrating type；
- 不以 fixture 为理由保留 retired production code；
- archive reader 不得被在线 Runtime import 或装配；
- 旧版本债务未清零时禁止继续叠加 schema 版本；紧急安全版本只有短期具名例外；
- compatibility budget 最终为零；迁移窗口内非零不构成日常失败，只要 fence 生效、无增长、无新消费者、未过 deadline 且进度符合计划。

### 12.4 Restore admission gate

- restore-capable inventory 覆盖 backup、snapshot、replica、offline node、export bundle、DLQ/quarantine、artifact payload 和演练副本；
- 每份 copy 具有 generation、authority digest、retention/destruction 和 restore contract metadata；
- 非 active generation 无法直接挂载、获取 writer lease 或加入在线 replica set；
- restore-time conversion 在隔离 staging 执行并产生与 cutover 等价的验证 evidence；
- `CLEANED` 前每份旧 copy 已迁移、销毁或绑定有期限且演练通过的隔离 conversion contract；
- restore converter/current archive converter 不把旧业务 decoder 重新引入在线 Runtime。

### 12.5 Subscription reliability gate

- LIVE/LOSSY 不承担 authoritative barrier；
- DURABLE 最终失败不推进 checkpoint；
- RELIABLE quarantine 与 checkpoint 原子；
- recoverable handler 使用封闭 side-effect policy；
- external effect 使用 outbox/inbox 或稳定 idempotency key；
- 不支持幂等的外部 target 不由 recoverable subscriber 直接调用。

### 12.6 Product graph/export/composition gate

- 受治理 edge 解析到唯一 active owner；
- runtime-import graph 零 SCC；
- type-only、export、composition 按各自语义检查；
- facade status 合法，compatibility consumer 只减不增；
- caller → canonical facade → defining symbol → exactly one active owner 可追踪；
- lifecycle owner 唯一且无 cycle；窄 port 双向通信不误判；
- dynamic loader/registry/plugin/factory/lifecycle transfer 可追踪；
- built-in scanning discovery 有 deadline 和退出条件。

### 12.7 Dynamic/type gate

- 生产代码无 local import；
- governed boundary 无 `Any` 和 `cast(Any, ...)`；
- 无未声明 dynamic import、PEP 562 export、reflection/service locator；
- private erased telemetry type 不被外部引用；
- compatibility facade 无新 consumer；
- generated report/inventory 不作为未批准 Runtime input；
- strict positive/negative type cases 与动态入口 evidence 均通过。

### 12.8 Unique production path gate

- 无双写、双发、双注册、双装配、第二 singleton/root 或两个 canonical API；
- typed debt role item 有 exact set、owner、deadline、exit/deletion evidence；不按名称关键词识别债务；
- set 只减不增，归零后同一里程碑删除；
- feature flag rollout 完成后 flag 和旧分支一起删除；
- `temporary_alias` 或迁移 fallback 在数据迁移后删除；长期 `resilience_fallback` 不按名称误判。

### 12.9 Composition reachability gate

- enabled capability 从适用 Product root 可达；
- construction recipe 按 `(capability_id, applicable_root, deployment_mode, instance_scope)` 唯一；每个 runtime instance lifecycle owner 唯一；
- start/stop 闭合；lazy/optional 不要求 eager instantiate；
- classifier 覆盖范围内无 production orphan 或 alternate import-side-effect/service-locator path；未分类 public factory/export hard fail；
- external plugin 经 validated snapshot 可达；
- Runtime 不反向 import Product；
- versioned candidate classifier 与 declaration 双向差集为空；未声明 orphan 和无实现声明均失败。

### 12.10 Authority/derivation gate

- 重复表达的语义有唯一 authority；
- generator deterministic，artifact 带 version、source digest 和 canonicalization；
- stale artifact hard fail；
- IDL/typed schema/OpenAPI/client model 只有一个 authority；人工 adapter 只做单向 conformance，不以双向 parity 共享决策权；
- capability inventory 从 typed composition declaration 派生；
- Gate status 从 executable evidence 派生。

## 13. Gate declaration 与生成状态

### 13.1 静态 Gate declarations

规范只声明稳定配置，不复制动态状态、违规数、时间戳或通过结论。一个 Gate ID 只有一种检查语义。`checker_status=absent` 时 `fixed_command` 必须为 `null`；`present` 时必须是可直接执行的完整命令。Absent → present 必须修改静态 declaration 并经过 review；生成 status 不得发明 declaration 中不存在的命令。CI 验证 `present <=> fixed_command != null`。

| Gate ID | Authority | Checker ID | Checker status | Fixed command | Declaration owner | Final-hard prerequisite | Evidence schema |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `ARCH-LOCAL-IMPORT` | Python AST | `pytest-local-imports` | `present` | `python -B -m pytest ztest/architecture/test_local_imports.py -q --tb=short -p no:cacheprovider` | architecture owner | 始终 | `gate-status-v1` |
| `PRODUCT-IMPORT-SCC` | Python imports | `pytest-product-scc` | `present` | `python -B -m pytest ztest/architecture/test_product_dependencies.py::test_product_cycles_match_exact_migration_facts -q --tb=short -p no:cacheprovider` | product architecture owner | expected SCC sets 为空 | `gate-status-v1` |
| `TYPE-TELEMETRY-ERASURE` | typed boundary + exact debt baseline | `telemetry-erasure` | `absent` | `null` | runtime-telemetry owner | raw caller 归零 | `gate-status-v1` |
| `EVENT-FACT-ADMISSION` | codec declarations + Python AST + exact debt baseline | `event-fact-admission` | `absent` | `null` | event-runtime owners | business construction 归零 | `gate-status-v1` |
| `PRODUCT-OWNER-EXPORT` | owner scopes + source exports | `product-owner-export` | `absent` | `null` | product architecture owner | export classifier 闭合 | `gate-status-v1` |
| `CAP-REACHABILITY` | candidate classifiers + composition declarations | `capability-reachability` | `absent` | `null` | Product composition owner | 双向差集归零 | `gate-status-v1` |
| `STORE-CUTOVER` | cutover declaration + transition history + generation/CAS record | `store-cutover` | `absent` | `null` | store owner | fence/quiesce/activation/restore 可执行 | `cutover-status-v1` |
| `RESTORE-ADMISSION` | restore inventory + copy metadata + conversion evidence | `restore-admission` | `absent` | `null` | store/backup owners | restore entry 全部受控 | `restore-status-v1` |
| `MIGRATION-DEBT` | store evidence + typed debt declarations | `migration-debt` | `absent` | `null` | domain owner | remaining debt 归零 | `migration-debt-status-v1` |
| `DYNAMIC-DISCOVERY` | bundled discovery evidence + Application catalog | `dynamic-discovery-diff` | `absent` | `null` | Product composition owner | legacy discovery 归零 | `gate-status-v1` |
| `DERIVED-ARTIFACT` | authority + generator declarations | `derived-artifact-conformance` | `absent` | `null` | owning domain | generator 确定 | `gate-status-v1` |

`PRODUCT-IMPORT-SCC` 使用精确 node ID。源码中的一级和二级 expected cycle sets 当前均为空，因此该测试证明零非平凡 SCC；若 expected set 非空，生成状态不得把它标为 final hard/pass。其他依赖方向断言属于不同 Gate，不能借该 ID 混合执行。

### 13.2 生成的 Gate status artifact

每次执行由 checker 生成状态工件，至少包含：

- `gate_id`、`checker_id` 和 checker version；
- exact executed command；
- source digest 与 declaration digest；
- execution timestamp；
- `checker_status: present | absent | error`；
- `enforcement: unavailable | report | ratchet | hard`；
- `result: pass | fail | not_run`；
- violation count 和结构化 violations；
- evidence path；
- remediation owner。

Markdown 只链接生成工件，不复制其动态内容。当前迁移事实（`cast(Any, ...)`、`pkgutil.walk_packages`、直接构造 `UncommittedFact`、PEP 562 动态导出等）应由相应 checker 在生成 artifact 中报告；在 checker 缺失前只能称“已知待迁移事实”，不能包装成已有 report Gate。批准的是目标模型与实施顺序，不是当前代码合规。

Checker 落地后按 `report → ratchet → hard` 收紧。Ratchet baseline 必须有 owner、source digest、exact violations 和 deadline；既有条目只允许删除，不允许新增或扩大范围，新文件/新调用点零容忍，修改 baseline 增加条目 hard fail。清零后移除 baseline 并切换 final invariant hard。这样 final-hard prerequisite 是终态条件，不妨碍迁移期间尽早阻止债务增长。

## 14. 实施顺序

### A. 建立事实模型和只读报告

- 定义四轴、representation stage、owner/unit inheritance 和 typed transformation declarations；
- 分离 runtime-import、type-only、export 和 governed composition seams；
- 生成现有 event、codec、subscription、dynamic boundary 和 compatibility inventory；
- Checker 已存在的规则可先以 report mode 建立基线；checker 尚不存在的规则保持 unavailable，不先登记永久例外。

### B. 关闭结构性旁路

- telemetry raw/erased API 私有化并迁移 caller；
- 收口 `UncommittedFact` 构造点并建立 audit codec；
- 启用 local-import gate；
- 清除 governed boundary 的 `Any`、`cast(Any, ...)`、错误 re-export 和 service locator。

### C. 建立 active durable generation 并清零历史债务

按 domain 执行：

1. 选择 migration mode，提交 Store Cutover ADR/声明；
2. 部署 candidate codec、migration、validator 和 forward-repair tool，但不启用 candidate writer；
3. Drain 新写请求和 background writer，在声明的 write-unavailable window 内通过持久化 CAS writer fence 阻止旧 generation 写入；
4. 等待旧 writer lease 失效并进入 `QUIESCED`；
5. 绑定 source generation/high-water mark，生成旧数据、checkpoint、DLQ、backup、replica、export、artifact 与所有 restore-capable copy inventory；
6. 执行可恢复 migration；
7. 校验记录数、摘要、语义、sequence/identity、引用、checkpoint、projection 和安全属性；
8. 原子激活 canonical reader/writer generation；
9. 在观察期只做 forward repair；
10. 迁移/销毁所有可恢复副本，或为其绑定隔离、有期限且演练通过的 restore-time conversion contract；
11. 删除旧 decoder、upcaster、entry、依赖旧实现的 fixture/test 与主生产树 migration program；
12. 保留不 import 旧代码的审计 evidence，确认 typed debt 与 restore-capable inventory 均闭合。

### D. LSP 独立演进

- 先批准 reliable-derivation ADR；
- 实施 committed/confirmed transition 到 LSP work 的可靠链；
- diagnostics 保持 advisory；
- 新链验收后删除旧 telemetry inference path。

### E. 唯一生产路径与动态 discovery

- 冻结 compatibility exact consumer set；
- 单向切换 API、facade、registry、catalog 和 composition root；
- built-in discovery 迁移到 immutable Application catalog；
- consumer 归零即删除旧 path、alias 和 global registry。

### F. Composition reachability 与派生工件

- 定义 Product-owned roots 和 per-root typed capability declarations；
- 定义 versioned candidate-source classifiers，并生成 discovered/declaration 双向差集；
- 生成 reachability/lifecycle view；
- 清理 orphan、alternate construction path 和 multiple owner；
- 为 IDL/OpenAPI、public API、Gate evidence 和 capability list 确定 authority、generator 或单向 conformance rule。

### G. Product 耦合治理

- 修复 capability → delivery adapter 反向认知；
- 将错误 owner 的 assembly 移入 composition/inference owner；
- 仅在真实 seam 抽 port；
- 按 exact consumer set 删除 Runtime event compatibility re-export；
- 保持 runtime-import 零 SCC。

### H. 启用 hard gates

按第 13 节逐 Gate 启用：checker 缺失时 unavailable；落地后先 report，再用 exact baseline 进入 no-growth ratchet，清零且 final-hard prerequisite 成立后转 hard。迁移进行中不要求所有 remaining count 每次提交都为零；要求 fence 有效、口径固定、不增长、按计划下降、无新 consumer 且未过 deadline。Ratchet baseline 只减不增，不是永久 allowlist。

## 15. 建议变更集

1. 四轴术语、owner/unit scopes、transformation declarations。
2. Product runtime/type/export/seams 图报告。
3. 唯一生产路径、compatibility 与 migration debt inventory。
4. Telemetry typed-only API 和 raw caller 迁移。
5. Durable admission 收口与 audit codec。
6. Store writer fence、lease/quiesce、封闭 transition history、generation activation 和 cutover evidence 基础设施。
7. Session active/candidate catalog、存量生成、migration、安全 fixture 与旧代码删除。
8. File Operations active/candidate catalog、存量生成、migration、安全 fixture 与旧代码删除。
9. PromptRejected/audit 数据最小化。
10. 独立 LSP reliable-derivation ADR。
11. ADR 批准后的 LSP 实施及旧 inference path 删除。
12. Diagnostics advisory context 分离。
13. Product production roots、public factory/export 分类闭包、candidate classifier 与 typed capability declarations。
14. Composition reachability 双向差集和 orphan/alternate-path 清理。
15. Authority/derivation declarations 与 generated inventory。
16. IDL/OpenAPI、Gate report、capability list 的生成或单向 conformance gate。
17. Canonical archival format、archive converter 和 destruction policy。
18. Restore admission、restore-capable inventory 和旧副本迁移/销毁。
19. Runtime event re-export 单向切换并删除。
20. Built-in discovery 迁移到 immutable Application catalog。
21. Product 高风险 owner/依赖边治理。
22. 各 Gate 按 unavailable → report → ratchet → hard 独立推进。

大规模目录移动、journal 物理格式迁移、LSP 队列、UI projection 重写不得塞进同一个变更集。

## 16. 完成定义

1. 四轴不再混用，也不机械进入所有事件字段。
2. 顶层阶段对象不跨 representation stage；共享 value object 的 owner 和序列化规则明确。
3. 每个 source-stage → target-stage 路径有 canonical、窄、类型化、可测试的 conversion owner。
4. Raw telemetry binding 和 erased type 不再公开。
5. 普通业务代码不能构造 `UncommittedFact`。
6. 每个 durable domain 分离 codec generation、migration debt 和 cutover declarations，Runtime journal 无领域 switch。
7. 所有在线有效数据已迁移为 active format；每个 `(logical_store, event_family)` 或 cutover unit 只有一个 active generation 及 writer/schema，每个 deployed store 只有一个业务 canonical read service。
8. 历史 decoder、upcaster、旧 schema entry、旧 fixture、旧测试和 migration code 已删除。
9. 冷归档只有一个 active archival generation 和 reader；旧 archival generation 已迁移清零。在线 Runtime 与 archive reader 隔离，原始重放环境若依法封存则与数据同期限销毁。
10. 活跃 store 中没有旧 storage/schema version，所有 migration remaining count 为零。
11. 所有 restore-capable copy 已迁移到 active/archival format、验证销毁，或绑定隔离且与副本同期限的 restore conversion contract；旧 generation 不能绕过 admission 重返生产。
12. Durable 数据具备 sensitivity、store-compatible retention、redaction、size 和 artifact lifecycle policy。
13. Reducer 确定、无 IO；recoverable external effect 有事务或幂等保证。
14. LSP 仅由 committed/confirmed transition 驱动；旧 telemetry inference 链已删除。
15. Product runtime-import graph 零 SCC；type/export/composition 按各自语义治理。
16. 受治理 edge 解析到唯一 active owner，内部文件通过 scope 继承而非逐项登记。
17. Facade defining symbol 和 owner 可追踪；compatibility facade consumer 为零并已删除。
18. Governed production boundary 无 `Any`、`cast(Any, ...)`、local import 和未批准动态逃逸。
19. 同一治理复合键内无双写、双发、双注册、双装配、迁移性 fallback-to-old 或两个 canonical API。
20. Production typed migration-debt inventory 为零；不存在未清除的 `migration_reader`、`compatibility_facade`、`deprecated_api`、`temporary_alias`、`rollout_flag`、`legacy_writer`、`temporary_discovery_path` 或 `temporary_construction_path`。
21. 迁移性 config alias/fallback 和已完成 rollout 的 feature flag 及旧分支已删除；声明为长期韧性或输入策略的 fallback 不因名称被误删。
22. 每项 enabled capability 从适用 Product root 可达；每个 `(capability, root, deployment_mode, instance_scope)` 只有一条 canonical recipe，每个 runtime instance 只有一个 lifecycle owner。
23. Lazy/optional capability 有明确 predicate、factory 和关闭责任；production-candidate classifier 闭包内不存在 orphan。
24. 所有 public factory/export 属于封闭角色之一，versioned candidate classifier 与 composition declarations 双向差集为空；Built-in component 由 immutable Application catalog 装配，无扫描/import-side-effect 第二路径。
25. 每个派生工件可追溯到唯一 authority、generator version 和 source digest。
26. Gate report、capability list、依赖图和审阅 catalog 不承担第二配置源职责。
27. IDL、typed schema、OpenAPI 和 client model 有唯一 authority；无法生成的 adapter 只有单向 conformance 权限。
28. Generated artifact stale 可由 CI 确定识别并 hard fail。
29. 每次新版本提交都包含旧版本退出计划；旧版本债务未清零时不继续叠加版本，安全紧急例外有短期 hard deadline。

完成不要求全仓文本意义上的 `Any` 为零，也不建立全局 event union、全局 durable catalog、全局 capability service locator 或完整运行时对象图。但 governed production boundary 不得暴露 `Any`；任何动态外部输入必须在所属 adapter 内完成验证和类型收窄。

## 17. 正式决议摘要

| 议题 | 正式决议 |
| --- | --- |
| 四轴治理 | 约束类型、转换和 transport，不成为万能 event metadata。 |
| Codec admission | 由窄 API、构造点、import/type gate 和测试闭包保证，不增加 provenance token。 |
| Representation | 顶层阶段消息单一 stage；稳定 immutable value object 可复用。 |
| Durable evolution | 旧 reader/upcaster 是有 deadline 的迁移设施；按 Store Cutover 状态机切换，在线最终只保留 active generation。 |
| Store cutover | 使用持久化 writer fence、lease quiesce、完整验证和原子 activation；rollback 不得恢复旧 writer，只能 forward recovery。 |
| Cutover history | 只允许封闭 transition graph；checker 重放完整 CAS/history 和 prerequisite evidence，不只看当前状态。 |
| Migration mode | `generation_copy` 是 read-online/write-offline；持续写迁移必须独立 ADR，且仍保持单一 authoritative writer。 |
| Cutover unit | 共享 sequence/checkpoint/transaction domain 的 families 整体迁移并原子 activation。 |
| Restore admission | 旧 backup/replica/export/DLQ/artifact 副本只能隔离恢复并迁移，不能直接进入 active Runtime。 |
| Cold archive | 归档系统同样只有一个 active archival generation/reader；旧代迁移清零，依法封存环境与数据同期限销毁。 |
| Retention | 由 stream/store 执行，不逐行剪除 append-only journal。 |
| Catalog authority | Python typed declaration 是事实源；JSON/YAML 是派生 artifact。 |
| Telemetry | 异构 binding 只在 Runtime 私有边界擦除；all-events handler 接受 `object`。 |
| Subscriber effect | 只允许 reducer、transactional projection、idempotent effect 或 outbox/inbox。 |
| LSP | 本文冻结 confirmed transition 边界；可靠机制由独立 ADR 定义。 |
| Owner/unit | 默认继承，稳定边界显式声明；不建立逐文件 CMDB。 |
| Composition graph | Seams 图只治理高风险动态边；capability declaration 对生产构造闭包完整。 |
| Lifecycle | 禁止 ownership cycle/multiple owner，不禁止经窄 port 的双向通信。 |
| Discovery | Built-in 使用 immutable Application catalog；包扫描是到期迁移路径。 |
| Facade | canonical/compatibility/internal 分治；compatibility 只减不增、归零即删。 |
| Any/local import | Governed boundary 禁止 `Any`/`cast(Any, ...)`，生产代码禁止局部 import。 |
| Reachability | Public factory/export 封闭分类；classifier 覆盖范围内与 declarations 双向差集为空，不虚称证明所有私有死代码。 |
| 唯一生产路径 | 禁止双写、双发、双注册、双装配及无限期 fallback/alias。 |
| 双写决议 | 本规范不批准双写；普通 ADR 无权例外，改变原则必须修改并重新批准本规范。 |
| Debt classification | 只由 typed debt role 识别迁移债务，不按 fallback/compatibility/v1 等名称扫描；长期韧性与协议能力另行治理。 |
| 派生工件 | 同一语义只有一个 authority；其余确定性生成或单向 conformance，不允许双向 parity 共享决策权。 |
| Gate status | Declaration 的 absent checker 必须配 `fixed_command=null`；状态工件支持 unavailable/report/ratchet/hard。 |
| 零债务验收 | Online data 全部处于 active format，production compatibility/migration inventory 为零。 |
