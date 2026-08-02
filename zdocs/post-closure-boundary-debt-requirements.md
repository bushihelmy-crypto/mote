# Mote 全仓剩余类型与持久化边界债务审计总索引

> 文档状态：审计总索引，**不是可直接整体实施或整体关闭的需求**。
>
> B1–B37 用于保存当前源码反证、风险和关闭方向。任何代码改动开始前，必须按本文件第 3 节拆出独立实施需求并完成准入评审；不得以“完成本索引”名义跨 owner 批量修改，也不得用一个测试批次或一个总状态替代各 bounded context 的签收。

## 1. 背景与目标

`core-architecture-debt-closure-implementation.md` 的 96 个工作包已基本收敛，但该台账不是对整个生产仓库所有动态边界的穷举。全仓静态复核仍发现若干不在原工作包描述粒度内、或跨越多个既有工作包的正式边界缺口。

本索引要求后续独立实施需求在不建立兼容层、平行入口或第二状态真相的前提下，分别闭合以下剩余问题：

- 已知内部类型不得退化为 `Any`、`object`、裸容器或宽 `Callable`；
- 泛型关系必须从 declaration/request 保持到 runtime/result；
- durable fact 必须经版本化、严格、可判别的 decoder 恢复；
- 内部 capability 不得通过 `getattr/hasattr` 或属性猜测发现；
- 外部动态值只能在 adapter 入口短暂存在，并立即投影为 canonical type；
- 可执行扩展、权限、恢复和持久化输入必须 fail closed。

本需求不要求机械清零全仓所有 `Any`、`Callable`、`BaseModel`、`object` 或反射。第三方 SDK、protobuf、JSON/wire、Pydantic validator、tree-sitter、平台能力探测和严格 decoder 入口可保留必要动态性，但必须局限于明确 adapter/private-erasure 边界。

## 2. 事实来源与审计纪律

- 事实优先级遵循仓库根 `AGENTS.md`。
- 当前源码决定现状；旧文档和既有测试不能证明关闭。
- 修改前必须搜索现有 DTO、Port、codec、catalog、output contract、journal 和 lifecycle owner，优先扩展 canonical owner，不新增同义 facade。
- 每个独立实施切片必须同步闭合 contract/declaration、owner、consumer、composition、lifecycle、persistence、observability 和 tests；任何一项未闭合均不得签收。
- 不保留旧签名 alias、兼容 wrapper、双读、双写、fallback decoder 或长期 feature flag。
- 不用 cast、TypeGuard、`# type: ignore` 或测试 fake 掩盖泛型关系。
- 开发过程中优先使用小批量定向测试、静态架构门禁和逐文件类型检查，避免在资源受限环境反复运行全仓测试；最终签收仍必须满足第 6 节的完整证据要求，环境无法安全执行的项目必须记录阻断和等价证据，不能静默豁免。

## 3. 独立实施需求准入

### 3.1 审计视图与 canonical workstream

F1–F9只是在全仓扫描中帮助定位相邻问题的**审计视图**，不是project、Epic、ticket hierarchy、实施顺序或共同owner。不得按F编号建总单、共享状态机或统一composition；同一行内的B项仍可属于不同canonical authority。

| 审计视图（不可建单） | 审计发现 | 拆分警告 |
|---|---|---|
| F1 Inference相关扫描 | B1、B2、B21、B30、B33 | Model execution、OAuth credential、daemon supervision、admin准入是四个owner；B21/B30/B33不得并入B2 |
| F2 Tool/安全相关扫描 | B4、B12、B13、B14、B15、B17、B32、B37 | Tool binding、Permission/Hook、MCP activation、runner、Sandbox、moderation删除分别建单 |
| F3 Agent governance | B15 中 interrupt/cancel/steer authoritative control面、B19 中 Agent mailbox/delivery 面、B23、B27 | `orchestration/agents`/session control 的 command receipt、delivery、admission、lineage/retention owner；不得下沉Runtime或由presentation拥有；B19的进程内queue不并入本状态机 |
| F4 Workflow durable state machine | B3 中 graph workflow面、B13 中对应durable reconciliation面、B25、B29 | `orchestration/workflows`；definition、run、reconciliation、inspection分清command/query owner，各自管理scan/wake/backoff，不消费B13 generic callback scheduler |
| F5 BackgroundTask lifecycle | B24 | Agent-owned `BackgroundTaskPool` 与 supervisor admission Port 分开闭合，不建立共享 task registry |
| F6 Artifact、FileOps 与删除治理 | B10、B13 中 destructive cleanup/GC面 | Runtime artifact/FileOps canonical store、owner edge projection、fenced governed deletion与Product authority分切片闭合；不与advisory scan共享gate |
| F7 Runtime/durable扫描 | B19、B5/B11、B8、B34、B35、B36 | Process queue、各event family、RunJournal、subscription、service-call journal均为不同authority |
| F8 Product surface扫描 | B6、B7、B9、B13、B15、B20、B22、B31 | Skill、Presentation、security config、code-map、Connection、Role surface、LSP、Notebook无共同identity |
| F9 Governance/automation扫描 | B16、B18、B26、B28 | B16/B18/B26属治理/删除收口；B28归`orchestration/automation/cron`，不得放入architecture owner |

实施计划只允许在以下canonical workstream目录下建单；每个workstream内部若仍有多个store/schema/lifecycle，继续按authority拆分：

| Workstream | 主要B项 | authoritative boundary |
|---|---|---|
| Model execution | B1、B2 | finalized inference request、model-call checkpoint/receipt |
| OAuth credential | B21 | subject credential/tombstone/backend-selection lifecycle |
| Inference daemon/API准入 | B30、B33 | daemon supervision与未接入admin删除/产品准入分别建单 |
| Tool binding/snapshot | B4、B14、B15 Tool面 | compiled binding、ToolExecutor chokepoint、MCP generation |
| Permission/Hook | B12 | permission activation与immutable Hook generation |
| Sandbox/process | B17、B32 | fixed-argv trust boundary与per-spawn enforcement profile |
| Agent delivery/turn | B15 control面、B19 Agent面、B23、B27 | Agent/session control、delivery、TurnRequest、capacity |
| Workflow | B3、B25、B29及B13对应面 | definition/run/reconciliation/inspection |
| BackgroundTask | B24 | Agent-owned process-local pool lifecycle |
| Artifact deletion | B10及B13 destructive面 | blob/session/identity各自retention/deletion authority |
| Session/Event authorities | B5/B11分派项、B8、B34、B35、B36、B19 Runtime面 | 每个codec/store/journal/queue按自己的owner建单 |
| Cron | B28 | `orchestration/automation/cron`唯一schedule/occurrence owner |
| Product surfaces | B6、B7、B9、B13 Product面、B15 Connection面、B16、B20、B22、B31、B37 | 每个Product bounded context独立activation/lifecycle |
| Architecture gates | B18、B26及各domain gate | gate证明能力、inventory projection、最终ratchet |

B1–B37 编号是发现 identity，不是实施 ticket identity。独立需求必须声明纳入了哪些 B 项及其中哪些具体 symbol/path；一个 B 项可以因 owner 不同拆入多个需求，但每个具体反证只能有一个负责关闭的 canonical 实施单元。

术语固定如下：B1–B37称`finding`；F1–F9称`audit view`；workstream只是canonical owner方向目录，仍须按authority拆成ticket；Wave只是准入阶段，不是全局barrier或实施授权；decision ledger与closure ledger使用各自明确状态，禁止用“完成/关闭”混称。

### 3.2 每个独立实施需求的强制内容

实施需求未同时包含以下内容时不得进入编码：

1. 精确 problem statement、当前源码 symbol/path 反证和风险；
2. 已确认产品决定，以及仍未确认并阻断实施的决定；
3. canonical owner、authoritative type、唯一 composition root 和状态真相链；
4. contract/DTO/Port、泛型关系、identity、revision、generation 与 fencing 设计；
5. lifecycle/state transition、失败语义、恢复、取消、shutdown 和 retention；
6. persistence/wire schema、strict decoder、版本演进与旧数据退出策略；
7. permission、effect、trust、authority 与 observability/audit 语义；
8. 复用检索证据和 `AGENTS.md §6.4` owner/服务面矩阵；
9. production composition、明确activation point、pre-activation rollback、forward-only migration/recovery、允许条件明确的operational rollback、不可逆action边界和旧入口删除清单；已写入新durable fact后不得笼统承诺Git回滚或部署旧writer；
10. 正向、负向、corruption、restart、CAS/fencing、并发及架构门禁测试；
11. 明确非目标、prerequisite contract revision、DAG依赖和可独立签收边界；purge/安全清除等不可逆action必须列authority、claim、backup/evidence与阶段receipt，不声称代码回滚；
12. 可逐项验证的关闭条件，以及源码变化后使证据失效并重新审计的规则。
13. 写入集合与共享热点文件、增加/删除的public symbol、authoritative owner、完整consumer迁移名单、与其他active需求的互斥/合并点及合并后cross-domain gate；
14. 并行冲突控制：同一authoritative type、store schema或composition recipe同一时刻只能有一个writer需求；上游contract revision变化时，依赖需求证据自动失效并重新base/review。

### 3.3 产品决定与实施阻断

下表中的默认状态均为“未确认”。相关独立需求只能完成调查和方案比较，不能擅自进入改变生产语义的实现；用户明确确认后，必须把决定、适用 domain、版本和退出条件写入对应实施需求。

| 待确认决定 | 必须回答的最小问题 | 未确认时的处理 |
|---|---|---|
| durable 旧格式处置 | 原样保留、一次性 migration、直接拒绝，还是经授权清除；migration 的审计、幂等、partial failure 和旧 decoder 退出条件 | 阻断任何格式切换、删除或长期双读 |
| retention 与 tombstone | 各 authority 的 retention 期限、terminal tombstone 幂等窗口、pin/effect/delivery/legal-hold 结算条件 | 阻断 purge/compaction 默认值和身份事实删除 |
| 删除 authority | legal hold、用户删除、安全清除、TTL 与测试临时数据分别由谁授权，receipt/audit 如何保存 | 阻断不可逆删除路径变更 |
| Sandbox profile | 按真实command/effect class定义哪些有限versioned profile；每个profile哪些control为required、哪些为advisory，以及运行中失效的终止/IN_DOUBT策略 | required不足时fail closed；阻断任意bool矩阵、默认降级或把启动探测当长期授权 |
| moderation能力准入 | 是否存在真实consumer、调用时机和authoritative gate；若保留，`UNAVAILABLE`是拒绝、暂停、人工复核还是仅advisory | 无确认consumer时删除死代码；不得把`UNAVAILABLE`解释为`ALLOWED`或预建未来Port |
| daemon 升级 | 滚动升级、generation 切换、旧进程 drain/kill、socket/discovery 退出和失败恢复保证 | 阻断引入双 generation 服务或隐式接管 |
| 有界治理参数 | queue、payload、scan、retry、attempt、backoff、deadline 等上限及配置 owner | 阻断拍脑袋常量、无限队列和无界重试 |
| Cron storage trust | workspace/store filesystem是否为 trusted authority；是否要求检测 command path 外的篡改及其跨信任域 provenance | 未确认时只删除受支持的 external-edit API，不得承诺技术上拒绝所有合法 shape旁路写入 |
| LSP 支持范围 | 支持的 protocol version、method/capability subset，或是否实现该版本完整合法 result union | 阻断以单一 DTO decoder 激活未经 capability negotiation 的 provider |
| daemon corruption evidence | 是否存在 incident consumer、短期 quarantine期限、authority和清理策略 | 未确认时不得创建业务 durable quarantine/retention 状态机 |
| Workflow effect 对账 | 各 capability 的 provider idempotency key/query保证、允许重试条件、无法证明结果时的人工/自动 reconciliation disposition | 阻断把 lease refresh当作原子性、stale owner提交或未知结果盲重试 |
| Presentation event兼容 | 采用静态closed union，还是generation-bound开放registry；旧consumer遇到unknown event的typed disposition与可观测性 | 阻断同时承诺exhaustive静态检查和无需升级的开放前向兼容 |
| Fixed argv调用策略 | 各同步consumer改async、移入activation或保留同步入口；Product/domain所需最小PATH/locale/home/config环境 | 阻断`asyncio.run()`/私有loop桥接、第二信任入口和默认继承完整环境 |
| Permission applicability | 哪些effect必须gate、哪些纯内部/read-only类别为NOT_APPLICABLE，以及显式bypass的authority/scope/audit | 阻断用`None`或`require_permission` bool决定信任边界、默认bypass或机械全deny |
| Hook更新策略 | activation后freeze，还是允许Product-owned generation hot swap；callback来源和批准authority | 未确认时activation后拒绝注册mutation，不建立动态callback registry |
| Connection强制退出 | DRAINING cleanup到期后是强制终止、泄漏报告还是阻断process shutdown；各surface适用策略 | 阻断丢弃失败token、伪装CLOSED或自行选择破坏性终止 |
| Inference admin surface准入 | 是否存在真实管理员用例、consumer、transport/listener、认证authority、network exposure及与gRPC surface边界 | 未确认时删除未接入surface；阻断新增admin Port、route、CAS state或后台server |
| Turn直接输入identity | 普通Message先成为canonical delivery，还是使用独立stable input identity；delivery batching、排序和单delivery归属规则 | 阻断用空delivery_ids或mailbox时序生成TurnRequestId |
| DLQ replay策略 | 是否开放re-admit、与live stream的并发/顺序、重复effect处置及人工authority | 未确认时可quarantine但不得自动replay、回退checkpoint或盲重试effect |
| Notebook stdin lifecycle | stale/cancelled/kernel-restarted reply的产品反馈、password value可见范围和connection close结算策略 | 阻断仅凭request_id接受reply、跨incarnation提交或secret进入普通投影 |
| Public API retirement | 待删除symbol是否属于稳定export、文档/example、stub、plugin contract或semver承诺；谁批准breaking removal | 只阻断已证明为承诺public surface的删除；private/internal accidental export经证据确认后直接删除且不留compat alias |

上表和D01–D21是**decision family catalog**，用于描述问题类别，不拥有可推进状态。表中标记固定为`CATALOG_ONLY`，不得改为OPEN/CONFIRMED/SUPERSEDED，也不得被requirement引用为批准证据。真正准入必须创建domain-scoped decision instance，例如`D01-model-checkpoint-v1`、`D01-oauth-record-v1`，绑定domain、authority、schema/profile/protocol/surface generation和affected requirement。

Decision instance状态只允许`OPEN / CONFIRMED / SUPERSEDED`。`CONFIRMED`必须记录decision date/version、accountable Product owner/批准authority、选择方案、affected repository-local requirement ID、authoritative contract/schema及验证artifact；`SUPERSEDED`只使其scope内证据失效，并指向替代instance。D04/D09/D12等包含多个profile/protocol/surface时同样逐scope实例化。当前authority未指定时不得由开发者代签。

现有`zdocs/architecture/contracts-decisions.toml`只拥有既有Contracts module/symbol的retain/move/delete治理，使用不同状态与facts gate；它不是本需求的scoped product decision truth。可以复用“仓内strict declaration加机械验证”的模式，但不得把D01–D21实例追加进旧ledger、把旧`approved`自动映射为`CONFIRMED`，或建立同scope第二份decision truth。Wave 0必须建立独立versioned decision-instance schema/ledger；未来若统一development governance容器，须先保持各domain owner、schema和migration边界。

| ID | 决策 | affected B/authority | catalog marker | contract evidence | 无instance时仍可进行 |
|---|---|---|---|---|---|
| D01 | durable旧格式处置 | B2/B5/B11/B20/B21/B25/B30/B34–B36；用户/Product owner待指定 | CATALOG_ONLY | — | 只读inventory、corruption fixture、decoder反例 |
| D02 | retention/tombstone | B10/B19/B21/B23/B27/B28/B30/B34–B36；用户/Product owner待指定 | CATALOG_ONLY | — | resource分类、阻止明显误删、规模基线 |
| D03 | 删除authority | B10/B21/B27/B28/B30/B34–B36；用户/Product owner待指定 | CATALOG_ONLY | — | authority inventory、只读reachability审计 |
| D04 | Sandbox profile | B32；用户/Product owner待指定 | CATALOG_ONLY | — | capability探测反例、per-spawn TOCTOU测试 |
| D05 | moderation能力准入 | B37；用户/Product owner待指定 | CATALOG_ONLY | — | consumer复核；无consumer时确定性删除 |
| D06 | daemon升级 | B30；用户/Product owner待指定 | CATALOG_ONLY | — | protocol/consumer inventory、严格decoder测试 |
| D07 | 有界治理参数 | B10/B19/B23–B28/B30/B34–B36；用户/Product owner待指定 | CATALOG_ONLY | — | 当前规模测量、无界路径反例 |
| D08 | Cron storage trust | B28；用户/Product owner待指定 | CATALOG_ONLY | — | 删除external-edit API、威胁模型调查 |
| D09 | LSP支持范围 | B31；用户/Product owner待指定 | CATALOG_ONLY | — | capability采样、wire decoder反例 |
| D10 | daemon corruption evidence | B30；用户/Product owner待指定 | CATALOG_ONLY | — | stale资源分类、generation-safe cleanup设计 |
| D11 | Workflow effect对账 | B25/B34；用户/Product owner待指定 | CATALOG_ONLY | — | provider guarantee调查、identity fixture |
| D12 | Presentation event兼容 | B7/B8；用户/Product owner待指定 | CATALOG_ONLY | — | consumer inventory、删除反射/default=str |
| D13 | Fixed argv调用策略 | B17/B32；用户/Product owner待指定 | CATALOG_ONLY | — | 同步/异步consumer矩阵、trust-boundary测试 |
| D14 | Permission applicability | B12；用户/Product owner待指定 | CATALOG_ONLY | — | effect分类inventory、默认bypass反例 |
| D15 | Hook更新策略 | B12；用户/Product owner待指定 | CATALOG_ONLY | — | callback consumer审计、activation冻结测试 |
| D16 | Connection强制退出 | B15；用户/Product owner待指定 | CATALOG_ONLY | — | token保留/DRAINING故障fixture |
| D17 | Inference admin准入 | B33；用户/Product owner待指定 | CATALOG_ONLY | — | production consumer复核；无consumer时删除 |
| D18 | Turn直接输入identity | B27；用户/Product owner待指定 | CATALOG_ONLY | — | crash-point测试、canonical preimage设计 |
| D19 | DLQ replay策略 | B35；用户/Product owner待指定 | CATALOG_ONLY | — | quarantine/lease修复；不启用replay |
| D20 | Notebook stdin lifecycle | B20；用户/Product owner待指定 | CATALOG_ONLY | — | stale reply/secret泄漏反例、identity设计 |
| D21 | Public API retirement | B1/B9/B16/B33/B37及其他删除symbol中实际public者；用户/Product owner待指定 | CATALOG_ONLY | — | export/doc/stub/plugin/semver调查；private死代码删除不受阻断 |
每张实施需求必须引用具体scoped D-instance及状态，不能只引用D-family或写“产品决定均已确认”。OPEN允许的工作仅限只读调查、consumer/entrypoint复核、需求与方案编写、治理artifact生成、先失败测试设计和owner/依赖矩阵整理；不得据此修改生产代码。即使候选项看似只是确定性死代码删除或不改变格式的局部收敛，也必须先完成该独立需求的public-surface、owner、consumer、write-set和所需scoped decision准入，不能由实施者在编码时补决定。

#### 3.3.1 第一批已确认scoped decision

用户已确认下列产品方向。当前正文记录是Wave 0 authoritative decision ledger的输入，不替代最终versioned declaration；ledger落地时必须逐项保存确认来源、reviewed requirement revision、affected implementation requirement和contract/schema落点，机械验证不得从family catalog或本表自动推导其他scope：

| Scoped instance | 状态 | 已确认决定 | 解除范围与仍保留阻断 |
|---|---|---|---|
| `D05-openai-chat-moderation-v1` | CONFIRMED | 当前产品不交付moderation；删除`OpenAIChat.amoderation`、专用`handle_exception`及相关残渣，不保留兼容入口；未来以真实consumer和typed fail-closed gate重新准入 | 解除B37产品方向阻断，不替代独立删除需求准入 |
| `D17-inference-http-admin-surface-v1` | CONFIRMED | 当前不交付aiohttp inference admin surface；删除未激活API、read/mutation model、daemon projection、export、fixture及专用依赖，只保留现有shared gRPC daemon入口 | 解除B33方向阻断；不得新增admin CAS、HTTP wire schema、server或未来stub |
| `D15-runtime-hook-generation-v1` | CONFIRMED | Hook只在Role/Application activation前注册和编译，原子发布immutable generation后拒绝mutation；更新通过下一Product/Role generation，不支持hot swap | 只解除B12 Hook lifecycle子需求；D14 Permission applicability仍阻断其余permission语义 |
| `D19-event-subscription-dlq-replay-v1` | CONFIRMED | 当前不提供DLQ re-admit/自动replay；quarantine后checkpoint单调前进，DLQ只提供typed调查与有界retention，未知effect不得重试 | B35按无replay边界设计；retention/delete/bounds仍依赖D02/D03/D07 scoped instances |
| `D10-inference-daemon-discovery-corruption-v1` | CONFIRMED | discovery损坏fail closed，不建立业务durable quarantine；current supervisor generation复核path、PID incarnation、socket generation和lock ownership后typed清理，只保留secret-safe摘要与receipt | 解除B30 corruption/stale cleanup方向；protocol upgrade仍依赖D06，cleanup bound依赖D07 |
| `D21-llm-client-port-v1` | CONFIRMED | `LLMClient`是无稳定facade export、无consumer的internal dead declaration，允许直接breaking删除 | 解除B1 retirement方向，仍须重跑consumer/public证据 |
| `D21-inference-admin-api-v1` | CONFIRMED | inference admin exports是未激活、未交付surface，按D17 breaking删除 | 与D17共同解除B33 retirement方向 |
| `D21-provider-moderation-method-v1` | CONFIRMED | `OpenAIChat.amoderation`不是稳定Product contract，按D05 breaking删除 | 与D05共同解除B37 retirement方向 |
| `D21-i18n-registry-v1` | CONFIRMED | `register_catalog`/`register_rule`未形成受支持外部扩展API；迁移全部仓内consumer、测试和文档后breaking删除public mutation | 解除B16对应registry方向；`__all__`和测试consumer必须显式迁移 |
| `D21-fixed-optional-loaders-v1` | CONFIRMED | Temporal/Squilla固定loader/catalog是内部composition detail，删除伪manifest/loader并改为各自owner的静态typed activation | 解除B16对应loader方向；两个backend必须拆成不同writer需求 |

上述D21确认统一禁止alias、re-export、wrapper、compat registry或第二入口。它不确认其他public symbol，也不把整个B9/B16或相关finding自动视为可实施/已关闭。其余D01–D21 family仍须逐domain实例化；下节只关闭Event/DLQ与daemon local cleanup的D01/D02/D03/D07实例，其他domain同family决定仍未确认，D06和D14仍是直接下游阻断。

#### 3.3.2 第二批已确认scoped decision：Event/DLQ与daemon local cleanup

用户已确认以下五个domain-scoped instances及其具体数值。确认范围只覆盖Event subscription/DLQ和inference daemon本地corrupt/stale cleanup，不确认D01/D02/D03/D07 family的其他domain。Wave 0 ledger必须逐项投影确认来源、decision revision、affected requirement、authoritative schema/contract和验证artifact。

`D01-event-subscription-state-v2`为`CONFIRMED`：

- 合法SQLite subscription-state v1在subscriber activation前，由`runtime/events` subscription-state owner通过唯一入口执行一次性、事务化、可审计的v1→v2 migration；Product composition只选择数据库路径和调用activation，不成为第二store owner；
- v2引入subscription generation、lease/fence、strict DLQ identity/lifecycle及retention字段；migration严格验证v1 schema和每条record，原样保留checkpoint sequence、subscription/stream/event identity、原始envelope和failure time；
- 迁入记录均无active execution owner，等待新generation claim；不得让旧worker或migration进程继承提交权；
- receipt记录源/目标version、database identity、迁移行数、content digest和commit revision；任一损坏、identity conflict或strict decode失败使整个transaction回滚并fail closed，不启动subscriber、不清空问题行；
- v2 commit并完成首次recoverability验证后删除v1 reader/writer，不保留运行期双读，部署不得回滚到v1 writer，只允许v2 forward recovery。

`D02-event-subscription-dlq-retention-v1`为`CONFIRMED`：

- 从`last_failed_at`起完整inline envelope/error默认保留30天；超过inline上限的payload从进入DLQ起使用canonical `ArtifactRef`，不得复制第二份blob；
- 到期且effect、delivery、artifact pin和legal hold全部结算后，由current fenced Event owner执行typed compaction，仅保留subscription/stream/sequence/event identity、failure/terminal disposition、timezone-aware时间、schema generation和content digest；
- 最小tombstone默认保留180天，用于checkpoint解释、identity reuse检测和审计；到期且所有hold/reference关闭后才可purge；
- `IN_DOUBT`、未结算external effect、active legal hold或仍被canonical artifact/session引用的记录不按时间删除；期限使用timezone-aware absolute instant与versioned clock identity，不使用mtime；
- 30/180天是versioned Product schema默认值，Runtime和extension只能在获准range内收窄，不得散落常量或任意覆盖。

`D03-event-subscription-dlq-delete-authority-v1`为`CONFIRMED`：

- 正常TTL compact/purge只由唯一Product application maintenance generation发起typed command；`runtime/events` current fenced store owner重新核验expected revision、retention eligibility、effect/delivery settlement、artifact pin和legal hold后执行；
- legal hold只能由Product批准的governance/incident authority设置或解除；当前不提供用户逐条删除API，也不允许handler、operator或maintenance直接SQL；
- security clear、正常TTL和test fixture数据库清理使用不同typed command、authority与receipt；每次不可逆操作产生不复制敏感payload的immutable receipt，记录command identity、authority generation、target identity/revision、fence、删除类别、前后digest和result。

`D07-event-subscription-bounds-v1`为`CONFIRMED`：

- 保持hard bounds：subscription capacity 65,536、handler retry最多100、单attempt timeout最多300秒、dead-letter error最多16 KiB、DLQ query page最多1,000；默认retry为3、timeout 30秒、initial/max backoff 0.1/5秒、jitter 0.2，不得借治理扩大；
- `checkpoint.persist_every`最大10,000，recoverable external-effect subscription固定为1；inline DLQ record最大1 MiB，超限payload必须预先使用canonical `ArtifactRef`，不得静默截断；
- 单次maintenance最多处理1,000条eligible records、单transaction最多100条；continuation cursor/receipt只是可重扫进度，不成为retention truth；
- maintenance连续失败最多重试3次，monotonic backoff为1/5/30秒；之后返回typed degraded/backlog，由下个正常周期重新scan，不得无限紧循环；
- 所有配置只允许Product versioned schema在hard range内单调收窄，extension和Runtime不得放大。Event journal的64 MiB record上限不得被错误复用为SQLite DLQ inline上限。

`D07-inference-daemon-local-cleanup-bounds-v1`为`CONFIRMED`：

- discovery文件最大64 KiB，超限返回typed corruption且不得解析或连接；
- current supervisor每次持锁prepare/reconcile最多检查并结算128个精确命名布局中的本地候选；每个候选删除前重新核验runtime directory、symlink/path、UID/mode、PID incarnation、socket generation和current discovery，任何不确定均保留并报告；
- 单候选最多3次删除尝试，使用0.1/0.5/2秒monotonic backoff；整批最多10秒，超限或失败返回typed partial cleanup receipt，剩余项由下次持锁scan重发现；
- 不持久化候选清单或原始corrupt payload为业务backlog，不赋予legal-hold语义；observability只保存secret-safe path category、digest、reason和receipt；
- cleanup失败仅在新安全path/generation与残留资源隔离已被证明时允许daemon继续启动；否则fail closed。失败旧资源不得复用或伪装已清理。

这五项关闭了`R-W3-EVENT-001`的migration、retention、delete authority、bounds和D19 no-replay产品决定，也关闭了`R-W3-DAEMON-001` local cleanup子范围的evidence/bounds决定；它们仍不授予生产编码。Event必须先证明external-effect checkpoint settlement及ArtifactRef/pin projection的canonical owner；daemon protocol upgrade随后已由D06 scoped decision确认。

#### 3.3.3 第三批已确认decision：daemon upgrade与Permission applicability

下列两个scoped instance均为`CONFIRMED`，完整语义和具体边界作为Wave 0 authoritative ledger输入；确认仍不替代独立需求及生产编码总准入。

`D06-inference-daemon-single-generation-upgrade-v1`确认采用单generation停旧启新：

- 每个Product release只声明一个current Shared RPC protocol generation；discovery、handshake、session credential、request envelope和server只接受该generation，删除永久`current - 1`接受路径；
- current supervisor lock owner先把旧generation原子推进为DRAINING，拒绝新start/open-session，只允许已有operation查询、结算和有界drain；
- graceful drain上限推荐30秒；超时operation必须先进入canonical recoverable/`IN_DOUBT`事实并撤销旧generation提交权，再terminate旧进程；terminate等待上限推荐10秒，仍未退出才kill，禁止先杀进程再补未知effect事实；
- 只有确认旧PID incarnation终止、旧socket不再接受连接且旧fence撤销后，才能发布新discovery和接受请求，不得存在双active generation窗口；
- 客户端遇到DRAINING、protocol mismatch或connection close时返回typed reconnecting/unavailable并有界重连；仅未被durable accept的调用可使用同一stable request identity重试，已accept或结果未知的调用只能query/reconcile；
- RPC protocol切换不授权durable store schema migration；触及store格式必须另有对应D01 instance。当前不建设blue/green、双socket切流、跨版本session migration或长期capability negotiation；
- Product inference composition/schema拥有protocol generation、30/10秒policy；daemon application/backend owner闭合execution settlement/reconciliation；supervisor只拥有本地process/socket/discovery lifecycle，不成为execution truth owner。

拒绝永久`current/current-1`、双daemon generation并行、直接kill旧进程和无限等待drain：它们分别形成无退出兼容债、第二套placement/effect ownership、未知副作用丢失或永久升级阻塞。

确认文本：`D06-inference-daemon-single-generation-upgrade-v1`采用单generation停旧启新，只支持current RPC protocol；旧generation先拒绝新工作并最多drain 30秒，超时operation先结算为recoverable/IN_DOUBT并撤销fence，再terminate等待10秒、必要时kill；旧PID/socket/fence全部退出后才发布新generation；只有未durable-accept请求可有界重试，不建设滚动双generation或跨版本session migration。

`D14-published-tool-permission-applicability-v1`确认所有published Tool统一经过授权链：

- 每个immutable published binding调用一律经过identity/argument validation、可选control Hook、重新classification/permission targets、core permission decision、sandbox/effect permit、适用时durable intent及execute；Runtime `ToolExecutor`是唯一chokepoint；
- 未配置用户permission rules不等于跳过gate；Product始终装配versioned baseline policy。普通workspace read/search可自动`ALLOW`，但仍必须执行target normalization、symlink/path boundary、sensitive-source、sandbox/capability检查并产生typed decision/trace；
- local mutation、subprocess、network、IPC、human-visible action、spawn/delegation、MCP/dynamic provider及secret/credential access固定为`REQUIRED`，mode、extension或tool metadata不得扩大为`NOT_APPLICABLE`；
- `NOT_APPLICABLE`只适用于非published、非模型可选、无IO/secret/capability/state mutation/external effect的内部纯计算；固定argv、maintenance、migration和reconciliation使用各自窄typed authority，不得伪装为Tool bypass；
- 若保留break-glass，只能跳过可选用户`ASK`，不能跳过bypass-immune deny、argument validation、Hook收窄、required sandbox control、effect intent、receipt或audit，并绑定authority、scope、expiry和receipt；
- 删除`permissions=None`、`require_permission`、`requires_permission_gate`等决定信任旁路的bool语义；baseline缺失、unknown applicability、空/无效facts、timeout、crash或malformed均fail closed；Hook只能单调收窄，修改arguments后必须重新classification/permission/approval，Hook `ALLOW`不能覆盖core permission或sandbox；
- applicability enum与baseline decision contract归Contracts authorization/tool policy；Product选择baseline及批准规则；Runtime实现decision Port；Tool只声明canonical target/effect/classification facts，不能自行决定跳过gate。

拒绝“所有Tool每次ASK”“只gate危险toolset”“`permissions=None`完全不检查”和“`PURE`自动跳过权限”：它们分别破坏可用性、依赖易漏分类、把默认配置变成最大权限，或混淆可重放副作用与读取/secret/caller安全。

确认文本：`D14-published-tool-permission-applicability-v1`要求所有published Tool经过唯一ToolExecutor authorization/sandbox/effect链；无用户规则时仍使用versioned Product baseline，普通read/search可自动ALLOW但不跳过path/secret检查，mutation/process/network/IPC/human/spawn/MCP/dynamic provider/secret为REQUIRED；NOT_APPLICABLE仅限非published内部纯计算；删除以`None`或bool决定旁路，缺失/unknown/malformed/timeout fail closed，Hook只能单调收窄。

D06已解除`R-W3-DAEMON-001`的single-generation protocol cutover产品阻断；D14已解除`R-W2-001`的Permission applicability产品阻断。依赖方向固定为typed applicability/baseline decision先于各effect class的Sandbox enforcement profile；D04也已在下节确认。

#### 3.3.4 第四批已确认decision：Sandbox profiles与fixed argv

本节两个instance均为`CONFIRMED`，与上一节D06/D14分别保持独立scope和依赖方向。

`D04-tool-process-sandbox-profiles-v1`确认删除任意bool矩阵，只保留三个versioned Product profiles：

1. `trusted-host-fixed-v1`仅用于Product批准的固定内部程序，不得承载模型/用户shell。required保证包括activation解析并批准absolute regular executable及不可替换identity、spawn前复核、structured argv且无shell/glob/expansion/用户可控executable、consumer-specific cwd/minimal env/stdin/timeout/output bound、process-group有界终止和typed process receipt；secret stdout只进入secret resolver。namespace/seccomp/cgroup仅advisory，receipt不得虚假声称filesystem/network isolation。
2. `isolated-workspace-offline-v1`用于获准且不需要网络的用户/模型命令。每次spawn必须实际证明OS filesystem/process namespace、workspace writable roots外不可写、kernel-enforced network-off、generation/permission/command digest/cwd/root-bound permit、process hardening和secret/env stripping；required backend不可证明即`SANDBOX_UNAVAILABLE`且不spawn。seccomp附加过滤与cgroup resource limits默认advisory，但不得用其缺失否定已经required的namespace/network保证。
3. `isolated-workspace-allowlist-v1`包含offline profile全部filesystem/process保证，并required netns唯一出口、default-deny nft或等价规则、Product allowlist proxy及SSRF/private-address拒绝。使用brokered credential时proxy、secret resolver、MITM CA/trust bundle和target-domain binding全部required；任一activation/spawn-time health失败均不spawn，禁止退化为secret env或无凭据直连。

共同规则：Product按真实Tool/effect class选择profile；用户和extension只能从批准集合单调收窄，不能选择`none`扩权。`backend=auto`只探测实现，不改变required guarantee，NullBackend不能承载B/C。activation receipt只证明plan，operation receipt必须证明spawn瞬间actual posture；运行中required enforcement丢失时owner终止/隔离process，ToolExecutor按effect事实结算typed failure或`IN_DOUBT`。删除`enabled/fail_if_unavailable/network_enforcement/seccomp`等任意组合决定安全承诺的公共Product schema；backend内部参数只能由profile compiler生成。

拒绝“所有control均required”“所有缺失仅warning降级”和继续开放bool矩阵：前者混淆真实安全边界并无谓排除宿主，后两者无法机械证明profile保证。

authoritative落点：profile identity及required/advisory contract归Contracts security/sandbox policy；Product拥有effect→profile选择和有限schema；Runtime sandbox实现per-spawn permit/actual-posture receipt；ToolExecutor消费receipt并拥有effect terminal settlement。

确认文本：`D04-tool-process-sandbox-profiles-v1`只提供`trusted-host-fixed-v1`、`isolated-workspace-offline-v1`、`isolated-workspace-allowlist-v1`。fixed profile不承诺OS隔离；offline必须证明workspace OS隔离及hard network-off；allowlist还必须证明唯一出口、default-deny、proxy/SSRF及credential broker链。required不可证明则不spawn，advisory缺失进入receipt，删除任意bool组合和silent degradation政策入口。

`D13-fixed-internal-argv-execution-v1`确认固定程序只保留async verified argv入口：

- production consumer在既有async lifecycle内await canonical typed API；同步CLI只可在最外层Product entrypoint运行一次`asyncio.run(application_command())`，Runtime/adapter不得嵌套loop、线程桥接或保留同步/异步双runner；
- Product activation为每个consumer解析absolute executable path、regular-file identity和来源，生成immutable `FixedExecutableBinding`；spawn通过fd或等价机制复核device/inode，PATH只用于activation解析，effect执行时不得重新选择程序；
- 数据参数必须由consumer strict decoder验证，不得改变executable、插入shell语义或扩张runner authority；用户命令必须进入D14/D04链，不能伪装为fixed argv；
- 默认环境为空白基线，只注入`LANG=C.UTF-8`、`LC_ALL=C.UTF-8`及consumer明确所需变量，不复制`os.environ`；
- credential helper仅允许USER/MANAGED来源、absolute verified executable、无shell，不注入HOME、云credential、proxy或完整PATH，secret stdout有byte bound且不进入日志；
- Hook command在activation前绑定approved argv/source identity、明确cwd和versioned wire stdin，只注入locale及明确业务变量；VCS使用verified git和repo-root cwd，通过显式flags关闭external diff、pager、interactive prompt、hooks/config command execution，只对白名单用户config开放；media helper绑定批准的ffmpeg/ffprobe、固定operation template、typed参数allowlist、显式path及timeout/output bound；
- 各consumer拥有独立最小argv/env policy，不建立万能`InternalProcessService`或`trust_mode/shell`开关。共享Runtime spawn/receipt原语，不共享高层authority。

拒绝同步runner、运行期PATH重解析、默认继承环境和万能内部runner：它们分别制造第二入口/loop问题、TOCTOU、secret与用户config泄漏，以及混合credential/VCS/Hook/media信任边界。

authoritative落点：Runtime拥有最小fixed spawn与typed receipt；各Product/domain consumer拥有program admission、argv/env policy和lifecycle；跨层`FixedExecutableBinding`由消费方所需的最小Contracts contract表达，不导出Runtime具体runner。

确认文本：`D13-fixed-internal-argv-execution-v1`只保留async verified argv；Product activation绑定absolute executable identity且spawn前复核，运行时不按PATH重选；环境默认最小化，credential/Hook/VCS/media分别拥有严格policy，用户命令不得伪装为fixed argv；同步CLI只在最外层运行一次application coroutine。

D04已解除B32 profile、required/advisory、per-spawn fail-closed与credential enforcement产品阻断；D13已解除B17 fixed argv、同步桥接、环境最小化和唯一runner产品阻断。两者仍不单独授权生产修改。

#### 3.3.5 第五批已确认decision：Connection close与Notebook stdin

`D16-product-connection-close-settlement-v1`与`D20-notebook-stdin-incarnation-v1`均为`CONFIRMED`，具体超时、重试、identity和secret边界必须原样进入Wave 0 ledger。

`D16-product-connection-close-settlement-v1`确认connection cleanup失败时隔离本connection generation，不杀共享Agent且不伪装关闭：

- 每个`ConnectionScope`绑定stable connection、session/Agent identity及单调generation，生命周期为`NEW -> ACTIVE -> DRAINING -> CLOSED`；`CLEANUP_FAILED`是typed settlement，失败后状态仍为DRAINING；
- close在owner gate下原子进入DRAINING，立即拒绝新turn、human reply、steer和rebind；transport停止接收不等于scope已CLOSED；
- cleanup按pending control drain/cancel、telemetry unsubscribe、human binding reset、projector/consumer close、port/transport close独立记录process-local immutable settlement；成功阶段不重复，retry只执行未完成阶段；
- human binding token、telemetry handles、owner/generation及failure detail在对应阶段成功前必须保留；reset失败不得清空token或`_env_bound`，stale connection generation不得reset新binding；
- 单次connection close总预算推荐10秒；超时返回typed `DRAINING_TIMEOUT`及未结算阶段，scope保持DRAINING并由session-hosting owner持pin、有界续清理，不无限阻塞request handler；每个失败阶段最多连续重试3次，使用0.1/0.5/2秒monotonic backoff，之后等待下一reconcile或application shutdown；
- 多连接AG-UI/ACP等surface中，单connection失败只隔离其generation并禁止冲突human capability重绑，不得终止共享Agent、ResidentSession或server；其他无binding冲突的session可继续；
- 单进程CLI/application shutdown由全局owner最多等待所有DRAINING connections 30秒；仍未settle则输出secret-safe leak report并以typed incomplete/non-zero退出，不伪造CLOSED或删除canonical durable session/Agent facts；
- `ConnectionScope`无权kill共享process，只能终止自身独占且contract明确可丢弃的presentation child；authoritative Agent control/turn/effect由各自owner结算。presentation-only close失败可DEGRADED，但human binding、control command及telemetry ownership必须settle或保持DRAINING。

拒绝失败后直接清token、无限等待、单连接失败杀共享Agent和吞错报告CLOSED：它们分别造成stale binding/ABA、server耗尽、authority越界或第二human/control入口。

authoritative落点：Connection state与阶段settlement归`product/session_hosting` owner；Telemetry、human binding和Agent control各自暴露最小typed close/reset/cancel receipt，不建立共享cleanup manager；Product application owner选择全局30秒shutdown policy。

确认文本：`D16-product-connection-close-settlement-v1`采用owner-local分阶段close，单次预算10秒；失败/超时保留human token、telemetry handle、generation及未结算阶段并保持DRAINING，每阶段最多按0.1/0.5/2秒重试3次。多连接surface不得因单connection失败终止共享Agent/session/process；全局shutdown最多等待30秒，仍失败则secret-safe leak report加incomplete/non-zero结果，不伪造CLOSED或删除durable facts。

`D20-notebook-stdin-incarnation-v1`确认stdin reply严格绑定current kernel execution：

- canonical pending identity至少绑定`RuntimeRef + Runtime epoch + kernel incarnation + execution msg_id + cell_id + stdin request_id + handoff generation + surface handle generation`；wire只携带opaque signed/unguessable reply capability或该identity的canonical token，不接受裸request_id；
- kernel stdin必须严格匹配current execution parent msg_id；缺失kernel request identity返回typed malformed并interrupt/settle execution，不得本地随机生成协议身份；同一execution最多一个active pending request，新request须先结算旧request；
- reply owner在同一generation gate原子claim pending，核验Runtime lease/fence、kernel incarnation、execution仍blocked、handoff/surface仍active后才调用kernel input；重复相同reply返回幂等receipt，不同value返回typed identity conflict；
- kernel restart、execution idle/terminal、cell cancel、handoff detach、surface close或deadline到期分别推进typed terminal disposition并撤销capability，旧reply不得提交到新kernel；
- stdin沿用cell execution absolute deadline，最长不超过现有600秒execute hard bound；到期先撤销pending identity，再interrupt kernel并等待既有5秒grace，不建立独立无限deadline；
- non-password input是canonical用户输入事实：kernel提交前记录durable typed intent，成功后记录receipt；恢复只解释历史settlement，不自动重放到新kernel；
- password plaintext只在surface adapter到current kernel input调用的受控内存路径短暂存在，不进入NotebookDocument、SurfaceFrame、ViewEvent、Session message、log、trace、exception、checkpoint或普通durable intent。Canonical事实只保存`password=true`、request/reply identity、keyed digest/length class和terminal receipt；
- frontend对stale/cancelled/restarted/expired显示不含value的typed结果并清空输入；password UI禁止回显、autocomplete及普通clipboard/history投影。Connection进入DRAINING时先撤销该surface stdin capability并结算pending outcome，再清human binding。

拒绝只增强随机request id、所有input均不持久化、password明文持久化和restart后自动重放：它们无法证明owner、丢失普通用户输入事实、扩大secret泄漏或把输入送进不同代码上下文。

authoritative落点：Notebook stdin identity/state归`runtime/interactive/kernel` driver与ManagedRuntime incarnation lifecycle；surface只传opaque capability/typed outcome，Product presentation只拥有password UI；non-secret durable input intent复用Runtime operation/journal contract，不新建Notebook event store。

确认文本：`D20-notebook-stdin-incarnation-v1`将pending/reply绑定Runtime epoch、kernel incarnation、execution、cell、request、handoff与surface generation；缺失kernel identity fail closed且不随机补ID，每execution仅一个pending，reply原子claim并支持同值幂等/异值冲突。restart/terminal/cancel/detach/close/expiry撤销旧capability，等待不超过600秒execution bound；普通输入记录intent/receipt但不自动重放，password plaintext仅走受控内存直达kernel且不得进入普通投影、日志或checkpoint。

D16已解除B15 Connection lifecycle的token保留、retry、timeout和强制退出产品阻断，不合并Tool/MCP cleanup或Agent control状态机。D20已解除B20 stdin incarnation、stale reply、password范围及cancel/restart/close产品阻断；Notebook其他schema/codec仍需独立requirement和适用D01 migration决定。两项仍不单独授权编码。

#### 3.3.6 第六批已确认decision：Turn input与Cron storage trust

`D18-agent-turn-input-via-delivery-v1`为`CONFIRMED`：

- 所有驱动Agent业务turn的external/user/inter-agent Message、Cron trigger及Product surface input必须先由Agent delivery owner提交stable durable delivery intent；禁止先放process mailbox后补identity，不建立`DirectInputId`第二状态机；
- delivery identity绑定source kind/identity、target logical Agent及lifecycle generation、canonical payload digest、delivery mode、request/batch identity和accepted revision；仅全部facts一致的重复请求幂等，任何差异typed conflict；
- 删除生产公共`notify(Message)`裸入口；内部`wake(agent_id, reason)`只唤醒已有durable pending/accepted fact的reconciler，不携带业务payload、不创建turn、不返回durable accepted；
- Mailbox只是current incarnation有界projection，enqueue必须携带delivery identity；canonical truth为delivery store与turn queue，mailbox丢失由durable scan重投影；
- turn owner按durable acceptance sequence选择稳定有序batch，不使用内存mailbox顺序、wall clock或hash。一个delivery最多归属一个accepted TurnRequest；先以同一owner transaction/generation提交`STAGED_FOR_TURN(request_id, batch_ordinal)`等canonical ownership，再提交bounded acceptance，crash后只完成或回滚同一stage；
- `TurnRequestId`确定性绑定queue identity、target Agent/generation、有序delivery tuple、batch digest、root/subtree、priority、deadline、config generation和maximum attempts；delivery相同但其他facts变化必须conflict；retry沿用同一request/batch，terminal settlement后才逐delivery ack；
- `QUEUE_ONLY`可durable accepted但不自行触发，后续合法trigger到达后按sequence合批；wake不得升级其语义。broadcast/subtree逐target生成独立delivery/settlement；无payload maintenance/reconcile使用typed control command，不伪装Message/turn。

拒绝独立DirectInputId、允许空delivery tuple、只用message id派生以及先drain mailbox再accept：它们分别复制durability/ack状态机、产生不可重建request、遗漏target/mode/payload冲突，或在crash时丢失/重复输入。

authoritative落点：delivery intent/identity/ack归`orchestration/agents/messaging`；TurnRequest/batch ownership/scheduler fence归`orchestration/agents/turn_queue`；Product surfaces与Cron只消费最小delivery command Port，Runtime Role/Kernel不拥有input acceptance。

确认文本：`D18-agent-turn-input-via-delivery-v1`要求所有业务Message先成为canonical durable delivery，删除裸`notify(Message)`，wake只唤醒已有durable fact。TurnRequest绑定target lifecycle generation和有序delivery batch，一个delivery最多属于一个accepted turn；stage/accept/retry/ack沿用同一request identity并可crash恢复，QUEUE_ONLY不自行触发，broadcast/subtree逐target结算。

该决定解除B27 direct Message identity、batch/ordering/ownership及mailbox drain/accept crash产品阻断；B19 delivery owner、B23/B27 capacity原子性和无delivery旧mailbox/residency数据的D01实例仍须独立闭合。

`D08-cron-trusted-local-store-v1`为`CONFIRMED`：

- Cron durable root由当前Mote application OS identity独占管理；directory/file/lock/temp必须owner-only，拒绝symlink、错误UID/mode、path escape和non-regular file；
- 当前不承诺抵抗同OS identity、root、disk administrator或offline disk edit。重启时可读取trusted root中的严格合法shape，但文档/receipt不得声称cryptographic provenance或能检测所有旁路写入；
- 删除external-edit hot reload能力、注释、mtime notification及测试；production schedule mutation和CLI统一经过typed `CronTaskCommands/service`，不得直接构造或修改store；
- scheduler只依赖canonical revision、expected revision和lease/fence。best-effort notification只降延迟，每个有界reconcile必须读取/比较canonical revision；mtime最多用于诊断，不能决定跳过读取；
- startup严格验证version、exact schema、revision、identity、clock、occurrence transition及filesystem安全属性；unknown/corrupt fail closed并进入typed recovery，不能当空schedule；
- 不新增HMAC/signature、remote attestation、append-only audit service或第二snapshot伪造provenance。未来多租户/高权限/不可信workspace需求另行决定trust root、key rotation、backup/restore和cross-host verification；
- store具体实现不向包外公开；Product只获得`CronCommandPort`与immutable query snapshot，scheduler获得窄owner service。backup/restore或offline repair若未来支持，必须是停机typed Product command/receipt，不复活文件热编辑。

拒绝mtime hot reload、无独立trust root却声称检测同用户合法shape改写，以及现在建设签名/远端authority：它们分别绕过command/revision、提供虚假保证或引入无consumer未来能力。

authoritative落点：Cron schedule/occurrence state归`orchestration/automation/cron`；Product选择trusted root并装配唯一command surface；filesystem safety check属于store adapter但不是业务mutation authority。

确认文本：`D08-cron-trusted-local-store-v1`把Cron root定义为当前Mote OS identity独占的trusted local authority，严格检查owner/mode/symlink/path/schema/revision，但不承诺抵抗同用户/root/offline disk篡改，也不建设签名provenance。删除external-edit hot reload和mtime控制；mutation/CLI统一走typed command owner，scheduler以canonical revision和周期reconcile推进。

该决定解除B28 storage trust、external edit、mtime reload、唯一command surface和provenance声明阻断；Cron专属D01/D02/D03/D07已在第3.3.11节确认。

#### 3.3.7 后续评审授权与外部阻断边界

用户已授权架构评审在本需求审核范围内，依据当前产品定位、`AGENTS.md`、源码事实和零债务原则，对能够确定的技术/架构选择直接比较方案、记录拒绝理由并标记scoped decision为`CONFIRMED`，不再逐项请求技术确认。后续authoritative ledger必须机械承接这些决定，不能只依赖自然语言。

只有当前事实无法推导真实业务目标，或决定涉及明确授权丢弃现有用户数据、新增仓外兼容承诺、引入付费/第三方依赖、扩大网络或权限暴露时，才保留为external product blocker；即使阻断，也必须先给出完整推荐、影响、owner/schema落点及解除映射。本授权仅用于完成审核和开工前决定，不授权修改生产代码、删除数据或产生外部副作用。

#### 3.3.8 第七批已确认decision：LSP profile与Presentation event generation

`D09-lsp-3.17-code-map-profile-v1`为`CONFIRMED`：

- 正式profile identity为`lsp-3.17-code-map-v1`，只装配stdio JSON-RPC，不预留TCP/WebSocket；封闭method集合为`initialize/initialized/shutdown/exit`、`textDocument/didOpen/didChange(FULL)/didSave`、`publishDiagnostics/documentSymbol/definition/references`；
- initialize声明profile/client/static capabilities，禁止dynamic registration；server result严格解码并确认实际method/sync capability，缺失返回typed `UNSUPPORTED_CAPABILITY`且不激活对应query；
- position encoding只接受协商后的UTF-16；server未声明按LSP 3.17默认UTF-16，明确仅支持不兼容encoding则拒绝profile。URI只接受approved workspace root内canonical `file:`，拒绝其他scheme、path/symlink escape、非法percent encoding和错误primitive；
- JSON-RPC严格验证2.0 envelope、id correlation、result/error互斥、error object、唯一header、Content-Length数值/上限、UTF-8与top-level object；malformed frame关闭endpoint并typed结算所有pending，不能返回空对象继续；
- LSP wire允许标准扩展字段，但必需/已消费字段严格；canonical DTO frozen/exact。`documentSymbol`完整支持`DocumentSymbol[] | SymbolInformation[] | null`并保持variant；`definition`支持`Location | Location[] | LocationLink[] | null`；`references`支持`Location[] | null`，空/null为`SUCCESS_EMPTY`；递归depth/item count有界；
- diagnostics严格投影consumer使用字段，非法单项typed reject/计数且不污染成功集合，envelope损坏关闭endpoint；receipt至少区分`SUCCESS_EMPTY / SUCCESS_WITH_ITEMS / UNAVAILABLE / UNSUPPORTED_CAPABILITY / TIMEOUT / INVALID_RESPONSE / SERVER_ERROR / CANCELLED`，只有SUCCESS可写code-map cache；
- manager/service/Role/code-map只传canonical DTO/receipt，不传播provider dict/list；endpoint owner持有reader task并在exception/EOF/shutdown结算pending。新增method/encoding/transport/LSP version须新profile generation，禁止裸dict扩张。

拒绝完整LSP 3.17、过窄DTO、best-effort空列表和开放dynamic capability registry：分别超出consumer范围、拒绝合法variant、污染cache或引入未批准变化轴。

authoritative落点：canonical query/result DTO与Port归Contracts code-intelligence；Runtime LSP adapter拥有3.17 wire decoder/endpoint/server lifecycle；Product只选择批准server command/language mapping/profile activation；code-map不读取LSP wire。

该决定解除B31 protocol/capability范围、合法result union、failure disposition及cache语义阻断；实施仍依赖D13 fixed server runner及逐method fixtures。

`D12-presentation-view-event-closed-generation-v1`为`CONFIRMED`：

- 每个Product presentation generation拥有唯一closed `ViewEvent` discriminated union、catalog和strict codec；`kind`为Literal/tagged variant，不允许任意subclass/plugin/import副作用扩展；
- 新ViewEvent是schema change，须同切片更新union/catalog、projector、capability adapter、全部production consumer disposition、wire codec和fixtures；gate双向验证union项与handler无漏项/越界；
- 每个surface对每个已知event显式声明`REPRESENTED / EXPLICITLY_DOWNGRADED / NOT_REPRESENTABLE`并计量。不存在default unhandled→空输出或unknown silent ignore；control/approval/question/error/durability failure等关键event若surface不能表达则activation negotiation失败或typed unsupported；
- ACP/AG-UI各自拥有external adapter/disposition manifest，可approved downgrade或明确不表示；external protocol的unknown extension宽容只停留在adapter边缘，不反向开放内部union；
- Structured JSON Lines是versioned正式surface，每行strict envelope至少含`schema_version/presentation_generation/sequence/closed payload`；删除`default=str`，Path/enum/scope/identity/media/artifact ref走canonical encoder，失败返回typed projection failure；
- external consumer必须协商共同presentation generation，无共同generation则拒绝activation/connection，同连接不混发generation。ViewEvent scope使用Contracts-owned versioned declaration/codec，machine event→ViewEvent→wire保持同一scope identity；
- ViewEvent仅是presentation intent，不成为Agent/session business truth；跨重连projection从上游durable facts重建，不建立第二event store。新generation同切片迁移仓内producer/consumer并退出旧内部generation；只有真实旧外部client SLO才另立有期限边缘adapter。

拒绝开放Python subclass registry、unknown silent ignore、强迫所有external surface一一表达及永久内部多generation：它们破坏确定性/安全事件可见性、错误统一协议或制造双读双写债务。

authoritative落点：canonical scope声明归Contracts activity/scope bounded context；closed ViewEvent generation与projection归`product/presentation`；ACP/AG-UI/Structured各自拥有wire adapter及manifest；Runtime machine event不得import Product type。

该决定解除B7 open/closed冲突、consumer穷尽、unknown disposition和Structured strict codec阻断，并确定B8 scope owner/identity方向；涉及durable machine-event历史仍需domain D01 migration。

#### 3.3.9 第八批已确认decision：Workflow effect reconciliation与RunJournal separation

`D11-workflow-effect-reconciliation-v1`为`CONFIRMED`。Lease/fence只决定谁能提交Mote canonical settlement，不能证明external action未发生或授权retry。每个Workflow effect在definition activation时必须选择一个封闭capability：

- `NO_EXTERNAL_EFFECT`：仅确定性纯计算或同一authoritative transaction内、可证明无外部可观察动作的mutation；可自动retry但保持同一logical EffectId、command digest和definition generation；新增IO/process/network/user-visible action必须重新分类；
- `IDEMPOTENT_BY_KEY`：provider正式保证同namespace/account下stable key在声明窗口绑定同一canonical request，同key同payload返回同operation/receipt，异payloadconflict且不执行第二动作。外部动作前durable intent，key、request digest、account/endpoint和retention window进入contract；retry必须保持全部facts，任何改变创建新EffectId；
- `RECONCILABLE_BY_RECEIPT`：provider提供stable operation identity/receipt及不会制造第二动作的status query。execute自动发起最多一次；异常后先query `NOT_STARTED/PENDING/SUCCEEDED/FAILED/UNKNOWN`，只有明确NOT_STARTED且intent仍current才可重新发起，UNKNOWN进入IN_DOUBT；
- `NON_REPLAYABLE`：无可靠idempotency/query/receipt且可能外部动作；intent后最多执行一次，任何非可信terminal结果进入IN_DOUBT/OWNER_ACTION_REQUIRED，不自动retry/dead-letter。补偿使用新Workflow EffectId，不重放旧effect。

同EffectId的canonical preimage绑定WorkflowRunId、definition id/generation、node/step/logical key、capability、versioned payload/digest、provider endpoint/account、permission/effect generation和provider contract revision；attempt ordinal不进入logical EffectId。重复全部facts一致才幂等，任何差异返回`EFFECT_IDENTITY_CONFLICT`。payload/receipt使用domain-owned tagged union或ArtifactRef，strict decoder拒绝unknown/extra/missing/wrong primitive。

effect state至少包含`INTENT_COMMITTED / CLAIMED / EXECUTION_STARTED / RECONCILING / SETTLED_SUCCEEDED / SETTLED_FAILED / IN_DOUBT / OWNER_ACTION_REQUIRED / COMPENSATED`；claim绑定record revision、run/definition generation、owner fence和attempt ordinal，stale owner不得开始attempt或提交transition。

stale owner取得receipt时不得提交canonical state或lease mutation。优先由current owner通过provider idempotency/query重建事实；不可重建raw receipt可写attempt-scoped append-only immutable evidence inbox，仅接受绑定EffectId、attempt、digest、provider identity的typed evidence，同内容幂等、异内容conflict。Inbox不是settlement truth；current fenced reconciler验证后CAS提交。evidence write失败且动作可能发生时保持IN_DOUBT/owner action，irreplaceable evidence按effect retention/legal hold保存。

Workflow terminal delivery只拥有outbox intent/outcome digest；Agent destination调用canonical Agent delivery command并保存stable receipt reference，其他destination调用对应domain Port。retry/ack/dead-letter归destination owner，Workflow只按typed receipt结算outbox；identity还绑定terminal revision、outcome digest和destination generation，冲突fail closed。

`D11-workflow-effect-run-journal-separation-v1`为`CONFIRMED`：Workflow effect intent/claim/attempt/reconciliation/settlement唯一归`orchestration/workflows`；Temporal history只是backend execution/attempt evidence。它首先禁止Runtime RunJournal接收Workflow definition/effect/delivery/reconciliation；第3.3.14节`D34-run-journal-domain-split-v1`进一步取代早期“剩余per-session steps继续共用RunJournal”的暂定范围，将Tool/think/timer分别迁入canonical owner并最终退役RunJournal。删除`application-workflow-effects` writer；旧数据必须经`D01-workflow-effect-run-journal-cutover-v1` inventory后选择migration为canonical evidence/settlement、archive-only evidence或经授权清除，禁止静默删除和运行期双读。

`D07-workflow-effect-reconciliation-bounds-v1`为`CONFIRMED`：

- NO_EXTERNAL_EFFECT/IDEMPOTENT_BY_KEY最多3次execute，backoff 1/5/30秒，attempt timeout由definition声明且Product hard range 1秒至5分钟；
- RECONCILABLE自动execute最多1次，query最多12次，backoff依次5秒、30秒、2分钟、10分钟后指数增长但单次不超过1小时，总观察窗口不超过24小时，仍UNKNOWN转OWNER_ACTION_REQUIRED且不再execute；
- NON_REPLAYABLE execute最多1次、retry为0；无query直接IN_DOUBT/owner action；
- reconciler scan每次最多500个eligible effects并逐项claim；unresolved capacity默认10,000、hard max100,000，满时BACKPRESSURED且不写intent、不驱逐accepted；
- inline command payload最大1 MiB，receipt最大64 KiB，超限分别使用canonical ArtifactRef或irreplaceable evidence ArtifactRef，不截断伪装完整；owner action无时间自动决议，必须typed command、authority、reason和audit receipt，legal hold/IN_DOUBT未关闭不得purge。

Product versioned schema选择这些数值，Runtime/extension只能收窄。下节已闭合Workflow D01/D02/D03；逐handler capability inventory及Temporal/provider/lease/commit fault fixtures仍是独立需求证据，无法证明provider guarantee的handler固定为NON_REPLAYABLE。

#### 3.3.10 Workflow effect migration、retention与authority已确认

`D01-workflow-reconciliation-v2-to-v3`为`CONFIRMED`。v3 activation前由Workflow store owner在独占migration lock内执行strict full-read→complete candidate→flush/fsync temp→atomic replace→parent fsync→read-back；任一损坏、重复identity、unsupported state/clock/payload使整次rollback且effect admission/reconciler不启动。Receipt记录schema/digest/count/disposition/implementation/instant；backup只是migration evidence，不是active store。状态映射固定为：

- v2 terminal且receipt可严格解释者保留terminal事实和`imported_from_v2`；AVAILABLE因缺批准provider contract转OWNER_ACTION_REQUIRED/LEGACY_CONTRACT_REBIND_REQUIRED；CLAIMED/执行中断/receipt非terminal转IN_DOUBT；原IN_DOUBT保持；external-effect DEAD_LETTER无可信terminal receipt转OWNER_ACTION_REQUIRED；
- legacy payload重编码为tagged legacy-command并保留EffectId/digest，不虚构definition/provider/idempotency facts；terminal delivery迁为outbox intent，非terminal delivery必须重新绑定current destination contract并query，不直接重发；
- candidate离线验证identity/preimage唯一、revision/state/attempt、ArtifactRef、run/definition reference和terminal不可eligible。首次activation后production仅v3；v2 decoder成为migration-only并从production recipe移除，全部deployment完成后删除。

`D01-workflow-effect-run-journal-cutover-v1`为`CONFIRMED`。旧application journal逐条按EffectId/RunId/payload/receipt digest严格关联：唯一缺失receipt转attempt evidence；重复terminal只登记deduplicated reference；冲突使effect保持IN_DOUBT/OWNER_ACTION_REQUIRED；无法关联的合法record转无执行/query能力的ARCHIVE_ONLY；损坏中段/重复identity fail closed。完成evidence/archive、计数对账和fsync receipt后删除Temporal writer，旧source先retired保存，最终由migration-retirement authority清理。

`D02-workflow-effect-retention-v1`为`CONFIRMED`：retention仅从effect/outbox/attempt/delivery/compensation/pin/provider全部settled的terminal instant开始；完整command/result/diagnostic默认90天，之后compact为含EffectId、generation、digest、capability、provider、attempt、terminal、evidence/compensation/outbox/migration引用的最小tombstone；tombstone及irreplaceable evidence默认1年。IN_DOUBT/OWNER_ACTION_REQUIRED/RECONCILING/未结算引用/hold不启动时钟。ARCHIVE_ONLY payload默认180天、migration tombstone 1年；source proof至少保留全deployment cutover后180天。期限由Product schema选择，只能延长或加hold。

`D03-workflow-effect-disposition-and-purge-authority-v1`为`CONFIRMED`：Product Workflow maintenance发起TTL command，current fenced Workflow owner复核revision、terminal、retention、effect/delivery/compensation、pin和hold后执行。OWNER_ACTION_REQUIRED只允许批准operator执行`CONFIRM_SUCCEEDED / CONFIRM_FAILED / KEEP_IN_DOUBT / AUTHORIZE_NEW_COMPENSATION / REBIND_NEVER_STARTED_LEGACY`，绑定authority/reason/evidence/audit；无retry-anyway/force bool。Legal hold和security clear各有独立authority；普通用户无逐effect物理删除API，run deletion返回逐effecttyped disposition。每个不可逆阶段前后复核，commit失败进入cleanup IN_DOUBT并保留deletion evidence。

v3 writer activation必须同时满足：v3 contract冻结、v2/RunJournal inventory与fixtures、atomic migration/fault evidence、D02/D03/D07进入Product schema、provider inventory完成且unknown均NON_REPLAYABLE、migration先于Temporal/reconciler/admission activation，以及inventory证明无v2/v3双writer、旧journal writer或第二cleanup入口。

#### 3.3.11 Cron migration、retention、authority与bounds已确认

`D01-cron-schedule-v2-to-v3`为`CONFIRMED`。Cron owner继续以单一transactional envelope拥有tasks、task tombstones、occurrences、revision和migration provenance；不拆分平行store。新TaskId至少128-bit且不可复用，legacy 8-hex映射到`legacy-v2` namespace；occurrence显式绑定schedule、task generation和clock。ACCEPTED/REJECTED保留并以migration instant作为terminal lower-bound；INTENT_COMMITTED/DEFERRED保留，DISPATCHING→IN_DOUBT，原IN_DOUBT保持。孤儿/重复/unknown/invalid reference全量fail closed。Migration在scheduler未active时lock、candidate、fsync/replace/read-back，首次activation后production只v3，v2仅migration-only并按deployment evidence退出。

`D02-cron-occurrence-retention-v1`为`CONFIRMED`：active task及未结算occurrence不受TTL；已知terminal完整payload/receipt/reason默认30天，之后最小occurrence tombstone180天；task删除/one-shot完成/age expiry生成180天task tombstone，TaskId永不重分配。IN_DOUBT/OWNER_ACTION_REQUIRED无自动到期。Migration proof/identity mapping至少全deployment cutover后180天。Product policy可延长或加hold，不能缩短，也不与其他domain合并TTL。

`D03-cron-occurrence-disposition-and-purge-authority-v1`为`CONFIRMED`：用户/CLI task deletion走Product-authorized Cron command，未结算occurrence返回BLOCKED_BY_UNSETTLED_OCCURRENCE；one-shot和age expiry是不同system dispositions。未知结果只允许operator command`CONFIRM_ACCEPTED / CONFIRM_REJECTED / KEEP_IN_DOUBT / CANCEL_IF_PROVEN_NOT_DISPATCHED / CREATE_NEW_OCCURRENCE`，最后一项创建新identity/supersedes edge；无retry-anyway/force/delete-unknown。TTL、legal hold、security clear、offline repair分别由不同authority；current Cron owner逐阶段复核revision/fence/hold/reference并返回receipt。损坏store fail closed，本评审不授权丢弃用户数据。

`D07-cron-schedule-and-occurrence-bounds-v1`为`CONFIRMED`：durable task默认50/hard 10,000，session-local 50/1,000；每task最多一个unsettled occurrence，全store默认1,000/hard 10,000。DEFERRED最多8次，backoff 1/2/4/8/16/32/60/60秒，无法证明未投递则IN_DOUBT。每tick claim/create各最多100、wall 5秒；maintenance每批500、transaction100、wall 5秒。inline prompt 1 MiB、receipt/reason 64 KiB，snapshot soft 64 MiB/hard 256 MiB。Cron为5-field、horizon 366天、IANA timezone、`EARLIEST_FOLD_SKIP_GAP`、misfire FIRE_ONCE、overlap FORBID；每tick读取/比较canonical revision，mtime不控制reload。Product schema只能收窄。

#### 3.3.12 Agent delivery、turn acceptance与Mailbox projection已确认

`D19-agent-ingress-owner-separation-v1`为`CONFIRMED`：`orchestration/agents/messaging`唯一拥有delivery intent/payload/target generation/lifecycle/turn assignment/ack/dead-letter/retention；`orchestration/agents/turn_queue`唯一拥有TurnRequest、有序delivery refs、capacity/enqueue/WDRR/claim permit/retry/terminal。Mailbox是per-incarnation可丢弃projection，Residency只保存projection cursor，PendingDeliveryQueue只是有界wake/scan hint，Runtime buffer只接收已绑定current TurnRequest的immutable input。两个owner通过最小typed Ports协作，不合并为巨型AgentExecutionDB，也不允许Control/Product跨store mutation。

`D23-agent-delivery-turn-atomic-acceptance-v1`为`CONFIRMED`。唯一acceptance协议为：delivery owner按durable sequence选eligible refs；turn owner原子`PREPARE_ACCEPTANCE`并预留capacity/sequence写PREPARED；delivery owner以expected revisions原子`BIND_TO_TURN`整个batch；turn owner验证binding receipt后`COMMIT_ACCEPTANCE`，只有此时公开ACCEPTED。Crash reconciler按transaction id双向query：未bind的expired prepare可abort；已bind只能完成同request或ACCEPTANCE_IN_DOUBT；无prepare的已bind进入OWNER_ACTION_REQUIRED且禁止重绑。Commit后才生成Mailbox/wake projection。

Execution settlement使用第二个prepare协议：turn owner写`EXECUTION_SETTLEMENT_PREPARED`及outcome digest，delivery owner原子ack batch，turn owner再terminal commit；结果可能发生但未提交则EXECUTION_IN_DOUBT，不自动重跑。Retry沿用request/assignment；delivery ack只表示输入已由canonical turn结算，不表示成功。Deadline/cancel/claim以expected revision竞争，任一store/fence/fsync失败fail closed；process rollback、Mailbox、Residency和wake不能推进canonical state。

`D01-agent-delivery-v1-to-v2`、`D01-agent-turn-queue-v1-to-v2`、`D01-agent-mailbox-projection-cutover-v1`均为`CONFIRMED`。Agent governance在admission/rehydrate/scheduler前独占cutover，同时strict inventory delivery v1、approved Residency mailbox、turn v1及lineage/generation，生成inactive candidates与cross-store manifest，再由单一generation pointer激活，禁止逐文件mixed generation。

- Delivery legacy identity保留；target generation无法唯一证明者转OWNER_ACTION_REQUIRED，CLAIMED→DELIVERY_IN_DOUBT，ACKED/DEAD_LETTER保留terminal，ACCEPTED保持eligible；
- Residency item与唯一delivery严格匹配者只转cursor/evidence；无delivery但current fenced且identity唯一者导入LEGACY_IMPORTED并默认OWNER_ACTION_REQUIRED；冲突阻断该Agent activation，stale incarnation只作evidence；
- turn terminal保留；ACCEPTED只有delivery完整且唯一时补assignment并保留，否则ACCEPTANCE_IN_DOUBT；CLAIMED→EXECUTION_IN_DOUBT且不得自动重跑；同DeliveryId属于多个turn则全部conflict；
- candidate验证assignment唯一、tuple完整有序、target/generation一致、terminal/ack无矛盾、capacity含PREPARED/ACCEPTED及sequence/revision单调。Receipt记录source/target digests、state counts、imports/conflicts、invariant digest和cutover generation；首次v2后v1仅migration-only并按deployment evidence退出。不得清空旧消息或假设CLAIMED未执行。

`D02-agent-delivery-retention-v1`与`D02-agent-turn-retention-v1`为`CONFIRMED`：active/in-doubt/owner-action不启动TTL；terminal delivery完整payload 30天后compact为1年tombstone；terminal turn完整input/attempt/outcome/receipt 90天后compact为1年tombstone。Mailbox/wake/hint无独立retention，可重建即丢弃但不得删除canonical delivery。Migration proof至少全deployment cutover后180天；hold、unsettled Artifact/effect/audit/cancellation暂停清理。

`D03-agent-delivery-turn-disposition-and-purge-authority-v1`为`CONFIRMED`：ack与turn terminal分别由current delivery/turn owner执行；target/subtree terminal由supervisor发typed cancellation，两个owner逐项receipt结算，PendingDelivery drop或批量覆盖不代表删除。Dead-letter仅限永久target/schema/有证据的有界失败，capacity不足是BACKPRESSURED；无replay，新发送使用新DeliveryId/supersedes。Owner action封闭为`CONFIRM_NOT_EXECUTED_AND_RELEASE / CONFIRM_CONSUMED / CONFIRM_TURN_SUCCEEDED / CONFIRM_TURN_FAILED / KEEP_IN_DOUBT / CREATE_SUPERSEDING_DELIVERY`，无retry-anyway/force-ack/delete-unknown。Product maintenance、legal hold、security clear分别授权；Session/Agent deletion返回逐类typed result，identity永不复用。

`D07-agent-delivery-turn-bounds-v1`为`CONFIRMED`：pending delivery每target默认1,000/hard10,000、每root10,000/100,000；payload inline 1 MiB；turn batch最多100/inline aggregate 4 MiB；active turn每queue默认1,000/hard100,000并计入PREPARED/ACCEPTED/CLAIMED/settlement-prepared/IN_DOUBT。Prepare lease30秒；reconciler500 transactions/每transaction100 deliveries/5秒。Projection scan每target500/总2,000，Mailbox500，hint全进程5,000。Turn最多3 attempts、backoff1/5/30秒，仅证明无不可重复结果才retry。Maintenance500/100/5秒；store soft256 MiB/hard1 GiB。Cancellation batch500并绑定snapshot epoch。所有capacity在durable accept事务原子检查，满时typed backpressure且不写ACCEPTED；WDRR cost仍为1，extension只能收窄。

Agent ingress activation必须机械证明：全部Message走delivery command且wake仅identity；v2 generation manifest无mixed start；fault fixtures覆盖prepare/bind/commit/execution/ack/terminal各crash点、fence/fsync；每DeliveryId最多一个TurnRequest且公开ACCEPTED均有binding；capacity/WDRR/deadline/retention/Artifact edge可确定验证；删除projection后仍能从canonical facts恢复且projection不能反写。

#### 3.3.13 OAuth credential owner、cutover与revocation lifecycle已确认

`D21-oauth-credential-owner-and-backend-v1`为`CONFIRMED`：`runtime/models/auth/oauth`拥有CredentialSubjectId、provider/config generation、backend binding、credential generation/state/scopes/expiry、SecretRef、revision及refresh/revoke metadata；plaintext access/refresh/client secret只归canonical vault。OAuth JSON不保存token/raw claims。Product activation显式冻结一个FILE_VAULT_V1或OS_KEYRING_V1 binding；`fallback`仅首次inventory resolver，零/一/多份按policy/唯一绑定/conflict处理，运行期不fallback。Subject identity绑定批准integration/account/config stable preimage；consumer只获得generation-bound短borrow，health authority不拥有token lifecycle。

Metadata v2 closed state为`ACTIVE / REFRESHING / REAUTH_REQUIRED / REVOCATION_PENDING / REVOKED / MATERIAL_LOST / IN_DOUBT / OWNER_ACTION_REQUIRED / RETIRED`；ABSENT只作为query结果。`token_generation`仅在vault material与metadata原子发布后增加。`LOCAL_LOGOUT / REVOKE_AT_PROVIDER / RETIRE_CONFIG_SLOT / SECURITY_CLEAR`是不同typed commands，不再用`commit(None)`。

`D11-oauth-refresh-and-revocation-effect-v1`为`CONFIRMED`：refresh/revoke在网络前durable intent，默认NON_REPLAYABLE；timeout/crash/远端成功本地commit失败→IN_DOUBT，不自动重复。Response严格验证后写inactive vault generation，再CAS metadata；publish前不可borrow。Refresh token缺失只有provider contract明确时才沿用；rotation旧ref失权但在settlement前受控pin。Stale response只能进入attempt-scoped immutable secret evidence inbox，不得发布generation。AbsoluteInstant用于expiry，raw JWT claims不决定identity/scope。

`D01-oauth-credential-v1-to-v2`为`CONFIRMED`：activation前逐subject一次inventory selector/file/keyring/config/vault。零record→ABSENT；selector有而material缺→MATERIAL_LOST/OWNER_ACTION_REQUIRED；单合法source冻结binding；双source完全一致也只保留policy优先source，差异→BACKEND_CONFLICT，不按revision/mtime/可refresh任选。Non-null token先写inactive vault、read-back digest后发布ACTIVE metadata；`token=None`→REVOKED/LEGACY_LOCAL_DELETE而非ABSENT。Config/store差异→CONFIG_STORE_CONFLICT，不以网络refresh裁决。Partial failure不激活OAuth且不边读v1边写v2；首次v2后v1/fallback仅migration-only。

`D02-oauth-credential-retention-v1`为`CONFIRMED`：active/in-doubt/lost所需metadata/material/evidence无普通TTL。过期或rotated material在borrow/effect/rollback闭合后立即eligible crypto-erase，正常最长24小时；REVOKED/RETIRED/REAUTH_REQUIRED完整非secret metadata90天后compact为1年tombstone。Tombstone不含token/raw JWT/client secret；MATERIAL_LOST/IN_DOUBT不自动terminal。v1 plaintext在全deployment cutover及secure-erasure receipt后删除，coordination proof180天；hold延长evidence但不强制保留非必要bearer plaintext。

`D03-oauth-credential-command-and-purge-authority-v1`为`CONFIRMED`：interactive login、proactive refresh、logout、provider revoke、backend migration、conflict owner action、TTL、legal hold和security clear分别由批准authority发typed command。Conflict action只允许KEEP_FILE/KEEP_KEYRING/KEEP_CONFIG/MARK_ALL_REAUTH_REQUIRED/KEEP_IN_DOUBT，不merge token fields。Runtime metadata owner复核revision/borrow/effect/hold/erase receipt；SecretStore只执行typed erasure，不删OAuth metadata。Corrupt subject fail closed但隔离于其他subject，所有receipt/log secret-opaque。

`D07-oauth-credential-bounds-v1`为`CONFIRMED`：每integration subjects默认32/hard1,000；每subject一个active generation、一个mutation、最多64 borrows。各secret64 KiB，provider body1 MiB，scope256×256 chars，claims64 KiB；connect10秒/response30秒；interactive默认10分钟/hard30分钟，device poll1–30秒且总≤30分钟/provider expiry。Proactive refresh默认expiry前5分钟且不早于lifetime50%，hard range30秒–30分钟；unknown expiry默认24小时内revalidate。自动refresh网络attempt最多1。Reconcile200 subjects/5秒，secret retirement100 generations。Metadata256 KiB/subject；keyring不承载mutable metadata。Cross-process使用canonical CAS/fence，不支持shared atomic backend则拒绝shared mode。Extension只能收窄。

OAuth activation必须先完成v2 contract、全source inventory/cutover、metadata/vault atomicity和effect evidence；consumer只能拿borrow capability。MCP与LLM可复用lifecycle mechanism，但provider/account/scope/consumer authorization使用不同binding，不存在模糊default credential。

#### 3.3.14 RunJournal按domain拆分并退役已确认

`D34-run-journal-domain-split-v1`为`CONFIRMED`：不建设RunJournal v2。Tool effect归`runtime/tools`的ToolExecutor effect owner；Model call唯一归现有ModelCallJournal，Session只拥有ModelCall terminal到assistant message的projection intent/ack；durable timer归Session operation/timer owner。`AppendOnlyLedger`只能作为明确domain adapter可复用的私有append/fsync mechanism，不公开`RunJournal`、`StepRecord`、字符串kind/status或万能start/complete/fail API。FileOps、Session event、service-call、Event journal均不在本次拆分范围。

`D11-runtime-tool-effect-reconciliation-v1`为`CONFIRMED`：每个published Tool invocation在activation时声明`NO_EXTERNAL_EFFECT / IDEMPOTENT_BY_KEY / RECONCILABLE_BY_RECEIPT / NON_REPLAYABLE`之一；ToolExecutor在permission/hook/sandbox之后、外部动作之前提交typed intent。ToolEffectId绑定invocation、Agent/incarnation、turn/run、definition/catalog/permission generation与arguments digest，attempt ordinal不进入logical identity。External action后本地commit失败、stale owner或unknown result进入`IN_DOUBT/OWNER_ACTION_REQUIRED`并保存receipt/evidence，Tool自身duck-typed resume方法不能授予重试权。

ModelCall terminal先提交canonical response/usage/cost，再由Session提交projection intent并在assistant message落盘后ack；任一crash point只补投影或ack，不重新请求LLM。Session timer使用独立SessionTimerId、deadline clock、resume generation和misfire/cancel状态，不能保存任意callback或重放实际属于Tool/Workflow的effect。

`D01-run-journal-tool-cutover-v1`、`D01-run-journal-think-cutover-v1`、`D01-run-journal-timer-cutover-v1`均为`CONFIRMED`：激活前严格inventory整个source及Session log、ModelCall、Tool invocation与Temporal来源，按三个domain构造inactive candidate并以单一Session migration manifest发布。只允许协议规定的单个末尾torn frame；中段损坏、unknown kind/status、forked identity或跨truth冲突fail closed。Tool STARTED默认IN_DOUBT；think不能因缺assistant message重付费；timer过期迁为typed misfire而非直接执行。三个target全部激活前不得删除共同source或启动mixed reader/writer。

`D02-runtime-tool-effect-retention-v1`、`D02-model-call-session-projection-retention-v1`、`D02-session-timer-retention-v1`与`D03-run-journal-cutover-and-purge-authority-v1`均为`CONFIRMED`：terminal Tool effect与ModelCall完整事实默认90天后compact为1年tombstone；terminal Session projection payload30天后可缩为identity/digest edge；terminal timer完整记录30天后compact为180天tombstone。IN_DOUBT、OWNER_ACTION_REQUIRED、未结算submission、Artifact pin及hold不启动TTL。旧RunJournal source在全deployment cutover加180天且无active reference/hold后，才由Product migration authority安全删除；generic `reap()`、Session cleanup或ledger rewrite没有业务删除authority。

`D07-runtime-run-domain-bounds-v1`为`CONFIRMED`：Tool arguments inline 1 MiB、receipt/result 64 KiB，Model response inline 64 KiB，大内容走ArtifactRef；每Session active Tool/Model/timer默认1,000/100/1,000，hard 10,000/1,000/10,000。Reconcile batch分别500/200/500且5秒；NO_EXTERNAL/IDEMPOTENT最多3次，RECONCILABLE execute一次并query最多12次/24小时，NON_REPLAYABLE execute一次。Domain frame 2 MiB，stream soft/hard 64/256 MiB，compaction每批1,000 identities、candidate 64 MiB、5秒。容量满typed backpressure且不删除active事实；新schema使用AbsoluteInstant，跨进程writer必须CAS/fence，删除全量`records()`、`max(all)+1`和任意`reap(list)`生产入口。

RunJournal activation顺序固定为：先冻结三个target contract，再实现各owner，之后做strict inventory与单manifest cutover，最后迁移consumer并开始180天retirement clock。Temporal application writer按Workflow workstream先退出；Hosted service-call、FileOps和Session event journal不因名称相似被删除。

#### 3.3.15 Hosted service-call v3治理已确认

`D11-hosted-service-execution-capability-v1`为`CONFIRMED`：capability由Product frozen binding选择NO_EXTERNAL_EFFECT、IDEMPOTENT_BY_KEY、RECONCILABLE_BY_RECEIPT或NON_REPLAYABLE；ServiceCallId绑定caller/ToolEffect、definition/config、payload、provider/account/endpoint contract与permission generation。Submit/poll/cancel均是动作前intent、typed provider evidence与current-fence settlement；UNKNOWN、caller deadline和transport failure不等于provider失败，cancel unknown进入CANCELLATION_IN_DOUBT。

`D01-hosted-service-call-v2-to-v3`为`CONFIRMED`：activation前全store inventory per-call JSONL、owner、cancel及pending projection，strict转换为v3 candidate/index/manifest并单generation切换；STARTED无可信receipt迁IN_DOUBT，open receipt迁WAITING_REMOTE，unknown receipt fields进入OWNER_ACTION_REQUIRED，owner file只提供legacy evidence/minimum next generation。迁移不调用远端；v2首次cutover后只migration-only。

`D02-hosted-service-call-retention-v1`、`D03-hosted-service-call-command-and-purge-authority-v1`和`D07-hosted-service-call-bounds-v1`均为`CONFIRMED`：active/unknown facts无普通TTL，terminal完整事实90天后compact为1年tombstone，v2 proof 180天；ToolExecutor/批准consumer提交，Runtime fenced owner执行，maintenance/hold/security authority分离。Active root/deployment默认1,000/10,000、hard10,000/100,000；payload1 MiB、receipt/response64 KiB；scan500/page256/5秒，store soft/hard10/100 GiB，所有retry/poll/cancel有界且extension只能收窄。

#### 3.3.16 Artifact ownership edge与Session fenced deletion已确认

`D01-artifact-ownership-and-deletion-v1-to-v2`为`CONFIRMED`：在Artifact/Session/FileOps/GC activation前联合inventory现有SQLite owner/retention/outbox、CAS、Session/tool/task output、FileOps roots与pin sources；可证明owner转换为typed edge，未知owner进入ORPHAN_QUARANTINED evidence，corruption/conflict fail closed。V2 metadata、edges、holds、deletion tombstones、orphan evidence与completeness manifest以单generation激活，v1不作fallback。

`D02-artifact-retention-v2`为`CONFIRMED`：EPHEMERAL在相关operation全部结算后24小时，SESSION在terminal及外部edge结算后30天，PROJECT只响应明确删除；active/held/IN_DOUBT无普通TTL。Deletion tombstone保留1年，orphan/migration proof至少180天，domain更长retention优先，storage pressure不能驱逐已接受事实。

`D03-artifact-and-session-deletion-authority-v1`为`CONFIRMED`：Artifact owner独占edge/hold/claim/metadata/blob GC，Session owner只验证lifecycle并提交自身edge release/delete intent；各domain只能释放自己的edge。TTL、user delete、security clear、hold与test cleanup分别授权，CAS/workspace/CLI/test helper无业务purge权。

`D07-artifact-reachability-and-gc-bounds-v1`为`CONFIRMED`：每Artifact/Session active edge默认1,000/100,000，hard10,000/1,000,000；closure每批10,000 edges/5秒，deletion每批500、transaction100/5秒，soft/hard capacity10/100 GiB，claim30秒。Canonical completeness generation未闭合时禁止删除；hard capacity只停止新content admission，继续结算。

#### 3.3.17 Durable scope disposition与Session rollout v2已确认

Durable scope disposition固定为：Presentation ViewEvent/widget/surface state与ACP/AG-UI/Structured wire是可重建representation，不建独立store；machine event按Session、FileOps、Tool/Model/effect、delivery、Workflow、ServiceCall等domain归属，不建全局永久event journal；Notebook document/output是Runtime checkpoint结构化视图，stdin已由D20闭合，均不建Notebook恢复truth；interactive checkpoint由Session fact与Artifact edge持有。真正剩余的canonical durable scope是`runtime/session`拥有的rollout stream。

`D01-session-rollout-v1-to-v2`为`CONFIRMED`：在resume/Event binding/checkpoint/FileOps recovery/v2 append前，逐Session inventory目录、rollout、run lease、checkpoint/projection metadata与Artifact roots，严格验证UTF-8、sequence/checksum、EventId、SessionId、唯一meta及torn boundary。V2加入store/lifecycle/producer generation、clock identity与Artifact binding；每个v1 event经authoritative class strict decode/re-encode，大payload先进入Artifact candidate/edge。完整verify/replay得到projection digest后以manifest/CAS切换；blocked Session只读保留，不新建空v2或mixed append。

`D02-session-rollout-retention-v2`与`D03-session-rollout-command-and-purge-authority-v1`为`CONFIRMED`：active/draining/recovery/handoff及任一domain settlement、IN_DOUBT或hold未闭合时完整stream无TTL；terminal且全部结算后完整stream30天，随后最小tombstone1年，v1 proof 180天。Session terminal只由current lifecycle owner提交；user delete、TTL compact、security clear、hold、tombstone purge和test cleanup分别授权，Session只释放自身stream/edge，共享blob由Artifact owner删除。

`D07-session-rollout-bounds-v2`为`CONFIRMED`：semantic inline fact 1 MiB、storage record 2 MiB，atomic batch最多100 facts/8 MiB；单Session soft/hard 256 MiB/1 GiB或1,000,000 facts，全deployment soft/hard10/100 GiB。Replay每批10,000 facts/5秒，migration每批100 Sessions且每Session每轮10,000 facts/5秒，listing page500/full repair10,000 dirs。Hard limit拒绝新turn/大payload但允许terminal/settlement/maintenance；append lease30秒，fsync失败不推进revision。

### 3.4 Owner、复用与最小服务面证据模板

每个独立需求必须填写下表，不接受“全仓搜索过”之类不可复核的概述：

| 项目 | 必填证据 |
|---|---|
| 现有机制检索 | 搜索命令、命中 symbol/path、真实消费者 |
| 复用决定 | 对每个候选说明直接复用、在 canonical owner 内最小扩展或拒绝复用；拒绝必须指出不同的不变量/lifecycle/durability/安全边界 |
| canonical owner | owner 包、核心不变量、authoritative identity/type/state truth |
| 包内内聚 | declaration、policy、state、store、adapter 为什么共同变化；哪些职责必须留在包外 |
| 最小服务面 | 每个 Protocol/service 方法、typed input/output/receipt 及其真实消费者 |
| 隐藏实现 | 不向外泄漏的 store、lock、task、client、registry、mutable collection、backend 与并发细节 |
| composition/lifecycle | 谁选择实现、谁构造/activate/shutdown/recover，scope 与 generation 如何推进 |
| 防双 owner 门禁 | 禁止的旧入口、跨层/跨包私有访问、平行 factory/store/codec/registry 及对应架构测试 |
| change/writer register | 精确write set与共享热点文件、public symbol增删、prerequisite contract revision、consumer迁移名单、当前唯一writer repository-local requirement ID、可选external ticket映射、互斥/合并点、合并后cross-domain gate及上游变化导致证据失效规则 |

### 3.5 Finding closure ledger

B1–B37是finding集合，不是ticket。Wave 0必须把每个原子反证分配不可复用stable evidence identity，例如`B04-E001`。一条identity只对应一个owner、一种风险/关闭条件和一个最终disposition；不随文件移动/修复重用。相同symbol若同时有raw-capability exposure与permission bypass，必须拆成不同identity；新扫描债务分配新identity，旧identity obsolete时链接replacement或no-debt evidence。

Closure truth采用仓内版本控制、versioned strict declaration，canonical path固定为`zdocs/architecture/post-closure-finding-ledger-v1.json`，owner是development architecture-governance声明/评审流程及明确governance maintainer；不得放入Runtime/Product或靠import副作用加载。`ztest/architecture/`只严格解码并验证schema、状态不变量、引用可达性和源码交叉证据，CI只生成只读报告，均不得拥有或推进状态。路径/schema未来变更必须走versioned governance migration并退出旧ledger，不得并存。

每条记录必须包含：stable evidence ID、current symbol locator/path与source digest、evidence source-baseline identity/dependency set、canonical workstream、repository-local `implementation_requirement_id`、可选external ticket/PR、按disposition约束的owner/writer requirement ID、disposition、prerequisite scoped D-instance/contract revision、deletion/migration/retention/fault-test evidence、验证命令及其证据分类。外部ticket/PR只作导航，不承担稳定身份、owner或关闭真相。

`implementation_requirement_id`在创建时分配、不可复用，不因rename或内容编辑变化；`requirement_revision`是单调revision或immutable content/tree digest，`canonical_locator`是可受控更新的当前path，`reviewed_revision`是获准编码的精确revision。内容变化只使reviewed revision和依赖证据失效；只有语义拆成不同owner/状态机时才分配新requirement ID并显式split/supersede旧项。该本地identity不依赖GitHub/Jira或预先merge commit。

变化证据分三阶段：working change记录base/current source-baseline identity与patch digest；integrated change记录integration commit/tree identity；verified release记录验证对应的immutable source tree。最终无保留签收要求integrated immutable revision，本地编码/review不要求merge commit。

disposition只允许：

- `OPEN`：反证有效、owner requirement为空、writer为空；
- `ASSIGNED`：恰有一个owner requirement与一个current writer登记；
- `IMPLEMENTED`：保留同一owner并具有working或integrated change evidence，但尚未最终验证；
- `VERIFIED`：保留同一owner，具有有效reviewed decision、immutable integrated source identity、verification declaration和机械证据；
- `OBSOLETE_BY_SOURCE_CHANGE`：无current writer，必须附replacement finding或明确no-debt evidence，不能计为已修复。

状态推进authority：governance maintainer/review流程批准OPEN→ASSIGNED及ASSIGNED→IMPLEMENTED；进入某Wave ticket前，其覆盖evidence必须ASSIGNED。VERIFIED采用双条件：governance reviewer/正确批准authority对指定immutable revision签署verification declaration，architecture gate机械验证authority/scope、状态transition、hash、decision和证据；人工不能绕过机械失败，CI也不自动推进canonical状态。

Git文件并发只使用expected ledger schema/revision、base source identity、review和merge conflict；CI拒绝revision回退、重复evidence identity、非法transition及同一write set的并行ASSIGNED writer。除非未来出现真实多进程ledger service consumer，不新增lock、lease、transaction store、Runtime式CAS或后台协调器。

架构门禁必须按状态验证：OPEN无owner/writer；ASSIGNED/IMPLEMENTED/VERIFIED恰有一个owner requirement，ASSIGNED另有一个current writer；OBSOLETE无writer且有replacement/no-debt evidence。最终无保留签收时未obsolete debt只能VERIFIED，不能仍为OPEN/ASSIGNED/IMPLEMENTED。当前扫描不得自动进入ASSIGNED/VERIFIED；ledger未知version、损坏或source-baseline mismatch fail closed。Decision instance状态与closure disposition不得混称。

### 3.6 生产编码总准入

Wave 0是评审与治理准备，不是任何生产代码治理的开工授权。只有准备修改的首个及后续独立实施需求各自满足下列条件，才能按已批准顺序进入生产编码；不得以“编码过程中再补齐”代替准入：

1. 与该需求及其下游语义相关的scoped decision instance均已`CONFIRMED`，确认文本包含选择方案、被拒绝备选及理由、数据/兼容/安全/运维影响、批准authority、authoritative contract/schema落点和解除的requirement阻断；
2. 覆盖的原子finding已有stable evidence identity、唯一repository-local implementation requirement和canonical owner；
3. implementation requirement具有稳定ID、获准的reviewed revision、精确write set、唯一writer、完整consumer迁移/删除清单和public-surface处理决定；
4. 每个durable格式已明确选择原样保留、一次性migration、直接拒绝或经授权清除，并定义partial failure、forward recovery、不可逆边界和旧decoder退出条件；
5. retention、tombstone、purge、cleanup、legal hold和安全失效路径已有Product authority、typed command/receipt及确定性验收标准；
6. 依赖已细化为authoritative contract/schema/type/composition deliverable DAG，不以B编号、F审计视图或Wave名称充当前置条件；
7. production-capable recipe、public declaration、durable authority、lease/CAS authority和approved dynamic boundary适用集合已经各自闭合，并通过跨集合完整性检查；
8. 关闭条件已有与风险相称的正向、负向、corruption、restart、CAS/fencing、并发和fault-injection证据计划；
9. 独立需求级实施顺序、共享write-set互斥点和合并后cross-domain gate已经明确，实施者无需临场选择owner、迁移或产品语义；
10. 当前准备开工需求的全部前置artifact和批准证据已经存在并可引用，不得只描述为未来Wave 0交付物。

未决scope的评审不能只记录“OPEN”，必须形成可供用户/Product owner确认的完整决定提案。确认结果写入scoped decision instance及受影响独立需求；对话内容、family catalog行或开发者默认判断均不能替代正式确认。

## 4. 审计发现索引

### B1. 删除无消费者 `LLMClient`，收敛到现有 canonical inference Port

当前事实：`contracts/ports/model/client.py::LLMClient` 的 `aask/aask_tool` 接受 `msg: Any`、`**kwargs: Any` 和 `list[dict]`，但当前全仓生产代码没有该 Port 的 import/use consumer。真实模型调用链已经由 `FinalizedInferenceRequest`/`FinalizedGenerateRequest`、`contracts/ports/model/inference.py`、`runtime/models/inference_port.py` 和 `runtime/models/model_calls.py` 承担。继续类型化无消费者声明会保留第二个模型调用 seam，违反唯一入口和零未使用类型原则。

实施要求：

- 实施开始时重新以 symbol/import/call-site 搜索证明 `LLMClient` 是否仍无生产消费者，并核对现有 finalized inference Port是否已覆盖全部真实 text/native-tool用例；
- 若仍无消费者，在同一切片删除 `client.py::LLMClient`、所有公开导出、测试 fake/fixture和只为该接口存在的适配代码；不得为了清除 `Any` 重设计或保留死 Port；
- 若源码变化产生真实消费者，必须先证明该用例不能由现有 finalized inference request Port表达，并另立独立需求完成 owner/复用评审；“未来可能使用”或测试便利不构成保留理由；
- provider专用 kwargs继续只存在于 Product provider adapter，由现有 typed activation/request决定，不得借删除动作迁入另一个宽 seam。

关闭条件：无消费者 `LLMClient` declaration、导出、fake和适配残渣为零；生产模型调用只走现有 finalized inference Port真相链，Kernel/Runtime不依赖 provider SDK类型。若独立复核发现不可替代消费者，则 B1 不得按本条关闭，必须由新需求证明必要性并替换本索引事实。

### B2. 闭合 Inference Engine 与 durable checkpoint 类型链

当前事实：`InferenceCheckpoint`将journal/message state/engine保存为`Any`并执行`InferenceCheckpointState(str(state))`；`InferenceJournal.begin_think()`仍接受state/string/None。`_started_checkpoint()`只保留dataclass已知key、让缺失字段由默认值补齐，并把解析失败、shape错误、缺少model_call_id统一返回`None`；`reconcile_think_journal()`随后可能reap该started证据并重新调用模型。`_completed_result()`长期猜测带/不带`result`两种payload。timer以裸wall-clock float/string宽松恢复，`target_lease_expires_at`也是未验证float。损坏、旧版本、identity mismatch因此可能被解释为“没有checkpoint”，造成重复外部调用和费用。

实施要求：

- 逐协作者按真实依赖边界选择类型：Kernel消费的外部能力才在Contracts定义Port；Runtime同一bounded context可使用精确私有具体类型/包内Protocol；Runtime跨bounded context使用被调用包承诺的最小service。禁止为journal、message state、engine机械创建三个Contracts Port、`InferenceCheckpointServices`巨型bundle或无其他consumer的稳定抽象；
- `InferenceRequest`、target、attempt fence、tool snapshot、output binding/schema 与 result 类型贯穿 `start -> _run -> checkpoint -> reinstate`；
- codec返回至少ABSENT、VALID、CORRUPT、UNSUPPORTED_VERSION、LEGACY_REQUIRES_MIGRATION、IDENTITY_MISMATCH和UNKNOWN/IN_DOUBT typed disposition；只有明确ABSENT允许开始新模型调用。unknown/extra字段不得过滤，缺失字段不得由dataclass default补齐；migration只在codec边界一次性执行并有旧decoder退出条件；
- 删除`begin_think(InferenceCheckpointState | str | None)`中的字符串旧形状和`InferenceCheckpointState(str(state))`恢复入口；生产只接收严格canonical state；
- `_completed_result()`带/不带`result`两种payload必须由产品确认一次性migration或直接拒绝，并删除旧decoder；禁止永久shape猜测双读；
- checkpoint schema/version、model-call identity/receipt、attempt/fence、target lease和generation在恢复前共同验证；`target_lease_expires_at`与timer统一使用strict `AbsoluteInstant`/versioned clock contract，拒绝bool、NaN、Infinity、未知clock和过期lease；
- `reconcile_think_journal()`不得自动reapcorrupt/unsupported/identity-mismatch model-call evidence；若provider请求可能已发生，保留evidence并按model-call receipt结算UNKNOWN/IN_DOUBT，禁止以“think pure”或无checkpoint为由盲目重付费；
- durable timer使用专用typed record与 `AbsoluteInstant`/版本化clock identity，duration有限且有界；损坏、未知clock或缺失started timer返回typed recovery failure，不能跳过后重启完整等待；进程内剩余时间投影到monotonic clock执行，持久层不保存裸wall-clock float；
- 不恢复 coroutine、SDK client 或进程对象。

关闭条件：正式边界协作者有符合层级的精确类型且无巨型/无consumer Contracts Port；只有ABSENT触发新模型调用，corrupt/unsupported/legacy/identity mismatch不会被过滤、补默认、reap或解释为absence；字符串入口与长期completed-result双读为零；stale attempt/fence/lease不能恢复，timer/checkpoint坏数据不重新计时或盲目重付费；负向fixture覆盖每个typed disposition和model-call IN_DOUBT。

### B3. 删除死 Graph committer seam并闭合动态 JSON Graph output 主链

当前事实：`runtime/tools/capability_types.py` 将 graph commit/resume 定义为 `Callable[..., Awaitable[Any]]`，`runtime/output/graph_service.py` 与 Role capability继续使用宽 output/contract/result。`runtime/output/graph_committer.py::GraphOutputCommitter` 当前没有生产引用，不能为了清除 `Any` 泛型化一个死 Protocol。真实主链是 `RunGraphTool -> Role capability -> GraphOutputService -> OutputEngine`；Product `GraphOutputContractSpec.schema_` 和 `GraphSpec.output` 均为运行时 JSON Schema/binding tree，当前没有静态 Python `GraphSpec[OutputT]` consumer。

实施要求：

- 删除无消费者 `GraphOutputCommitter` declaration、导出和仅为其存在的 fixture；除非实施前出现真实依赖反转消费者并通过独立 owner/复用评审，不得保留或泛型化死 seam；
- 对真实动态主链固定采用 `GraphOutputContractSpec -> strict canonical JSON-schema declaration -> OutputContract[JsonValue] -> OutputEngine[JsonValue] -> validated committed output -> ToolResult JSON payload`，绑定 contract identity、schema fingerprint和严格 validator，不以 `Any`传播；
- `CommitGraphOutput`/`ResumeGraphOutput` capability与 `GraphOutputService` 使用精确 typed command/service方法，不用 `Callable[...]` ellipsis；逐 consumer核验 Role capability是否必要，能直接注入更窄 graph execution service时删除同义 Role facade，禁止 committer/service/facade三层平行入口；
- 当前不新增 static/dynamic declaration union或 `GraphSpec[OutputT]` 未来变化轴。只有仓内出现已确认的静态 graph consumer时，另立需求证明 `OutputT`端到端关系；禁止 cast/TypeGuard把动态 schema冒充静态泛型；
- restored state 绑定 run id、contract id/schema fingerprint、revision 与 fence。

关闭条件：无消费者 `GraphOutputCommitter`及同义入口残渣为零；真实动态链只产生与 contract identity/fingerprint绑定的 canonical `JsonValue` validated document；resume不能用不同 schema/identity接管旧状态；主链无 `Callable[..., Awaitable[Any]]`、宽 output result或 cast/TypeGuard伪造泛型，且未新增无消费者静态 Graph abstraction。

### B4. 建立唯一 typed executable-tool capability

当前事实：`BoundToolCatalog._tools: dict[str, Any]`同时接受raw tool与`ExecutableToolBinding`；`register()`不验证元素identity/generation，未知对象默认builtin，name/summary/category/deferred通过`getattr`猜测，`_live_tools()`把mutable map交给executor。`ExecutableToolBinding.wrapped_tool`又公开raw capability，schema rendering、validation、effect、permission、cleanup和call继续回穿live instance；binding还藏有`BoundApprovalPolicy` callback。catalog definition generation、B12 permission/control和B14 snapshot因此可能成为三个独立执行真相。

实施要求：

- catalog元素唯一固定为canonical immutable compiled executable binding；raw builtin/MCP/workflow capability只存在于各definition compiler/adapter，在注册前完成shape、definition identity、generation、schema、effect、permission target、cleanup和invocation adapter验证；catalog注册后不得再见raw instance；
- schema/summary/category/deferred声明、argument decoder、effect identity、permission targets、result contract、cleanup command与调用seam全部编译进immutable definition/binding snapshot；删除`wrapped_tool`和任何从catalog/binding回穿raw capability的公开面；
- catalog不返回live mutable map；executor只接收generation-bound immutable binding snapshot/query，registration/swap以expected generation原子发布；
- 分类使用 definition 的封闭 `ToolExecutionKind/category`，不得通过实例 shape 或 `getattr` 猜测；
- 区分catalog definition generation与Agent-local deferred/revealed presentation state：revealed state只在已批准definition集合内单调收窄当前Agent可见/可执行投影，不作为binding callback、不修改canonical snapshot，也不改变semantic identity；
- B4只拥有compiled definition/capability binding及immutable semantic identity；B12 permission/control pipeline消费binding声明，任意approval callback不得藏在binding；B14只持久化/恢复已激活binding snapshot identity/generation，不复制capability、approval或permission状态机；ToolExecutor仍是唯一调用chokepoint。

关闭条件：catalog中只有一种immutable compiled binding元素；raw instance、`wrapped_tool`、live map、shape反射和binding内approval callback为零；schema/execute/permission/effect/cleanup消费同一semantic identity；revealed state不改变definition generation；B4/B12/B14 owner边界有门禁，catalog/binding/snapshot均不能绕开ToolExecutor直接调用capability。

### B5. Durable event-family codec/migration 交叉审计索引

当前事实：`DurableFact`被Output、Conversation、Model等多个domain family继承，`runtime/session/codec.py`与file event codec又有独立envelope/consumer。generic `from_payload()`只核对顶层key后`cls(**payload)`，不严格验证primitive/range/identity/state relation；许多dataclass还用空identity/零revision/fence默认值。Routing payload可变，Session timestamp为本地naive时间。B5本身不是全仓codec owner，不能通过中央catalog统一这些family。

实施要求：

- B5只维护symbol→authoritative domain owner→consumer→migration需求矩阵；Output、Session、FileOps、Conversation、Model routing等各自拥有tag/version、strict decoder、migration和旧decoder退出，不建立B5共享codec实施单；
- Session family明确进入第3.3.17节rollout v2 workstream：`runtime/session`拥有SessionId、stream lifecycle/order/revision、strict closed SessionEvent union、append CAS、replay、retention eligibility和delete intent；`LocalEventJournal`只提供journal mechanism。RunLease、checkpoint/projection metadata及Artifact binding与同一migration inventory协调，不再拆出第二Session event store；
- Session stream不得保存ViewEvent/widget state、wire payload、live Role/service/task、provider object、plaintext secret、未知Python object或裸dict。Fact append与Artifact edge publication使用durable intent/ack或可恢复事务，防止event存在而payload被GC或artifact永久泄漏；
- replay只从current generation、完整checksum chain和strict codec产生projection；unknown/corrupt/identity mismatch fail closed，不跳过坏行。SessionMetaEvent只能是sequence 1，directory不能覆盖stream/meta内冲突SessionId，append/delete同时受stream revision、run lease fence和lifecycle generation约束；
- 每个family decoder对exact fields、tag/version、primitive、enum、非空identity、revision、非bool正fence、nested JSON与跨字段状态关系严格fail closed；exact top-level keys不等于严格解码完成；
- bool 不得作为 int/token/revision，NaN/Infinity 不得进入 durable 时间、费用或比例；
- session durable event occurrence统一使用timezone-aware UTC `AbsoluteInstant`或等价版本化字段；禁止本地naive ISO时间，旧naive记录必须明确一次性migration/拒绝策略，不能按当前机器时区猜测；
- 所有 collection 深冻结，encoder 输出必须能被同版本 decoder canonical round-trip；
- Routing decision 使用 canonical immutable DTO，绑定 route schema version、catalog/config generation、request/model-call identity 与决定 provenance；
- `DurableFact`最多保留不拥有codec/tag/migration策略的纯辅助declaration；consumer迁移后删除generic `vars(self)` serializer/`from_payload()`，禁止全仓event class registry、任意subclass自报tag或import-side-effect注册；
- 每个symbol只能进入一个domain实施需求和一个migration owner；B5最终矩阵用于证明无遗漏/重复，不产生生产registry、store或decoder。

关闭条件：B5矩阵中每个symbol唯一归属已签收domain需求，无中央codec/catalog/registry、subclass自注册或重复migration；Session rollout v2是唯一Session durable truth，Presentation/wire/Notebook没有平行历史，event与Artifact edge无悬空窗口；generic serializer/decoder不再拥有安全语义；各family独立fixture证明unknown tag/version、额外/缺失字段、错误primitive、空identity、负或bool fence、非法状态关系和mutable nested payload拒绝，损坏记录不降级为空/默认状态。

### B6. 分离 Skill manifest、source admission 与 activated/compiled snapshot

当前事实：`product/skills/skill_definition.py::SkillDefinition` 直接继承默认 `BaseModel`，未知字段会被忽略，非法 `context` 会静默降级为 `inline`，metadata为裸 dict；同一对象还混合 frontmatter/完整 instructions、本地 `source_path`、运行时 `token_count`、`model_post_init()`中硬编码 `gpt-4o` 的派生行为，以及 fork model/effort/tool capability选择。manifest declaration、来源证据和 activation projection因此形成一条混合真相。

实施要求：

- 建立 Product-owned strict/frozen/forbid-extra `SkillManifest`，只表达经 parser验证的声明字段；原始 YAML metadata只留在 parser adapter用于定位错误，不进入 canonical manifest，也不能成为任意扩展或权限入口；
- admitted source evidence独立绑定 canonical path、content digest、trust decision、approval authority与generation；manifest不能自报或修改来源信任；
- activated/compiled Skill snapshot绑定 manifest/source identity、resolved model/effort、allowed tool binding generation、tokenizer identity和计算后的 token cost；token count不得在 manifest construction中以硬编码模型计算或成为 declaration truth；
- prompt projection与fork request只消费已批准 activation snapshot，不重新读取manifest/raw metadata或自行解析tool权限；
- `context`、model、effort、allowed-tools和paths使用明确 enum/DTO与严格 decoder；
- malformed/unknown execution mode fail closed，不得降级为另一种权限或 lifecycle；
- source canonical path、content digest、trust decision、approval、definition generation 与激活结果绑定；
- fork capability 只能单调收窄父授权，不能由 manifest 扩权。

关闭条件：未知字段、错误 context/allowed-tools shape、路径逃逸和digest变化均在激活前拒绝；manifest、source evidence、activated snapshot有不同 authoritative type/identity且无双写；token cost可追溯到 tokenizer/model generation；未批准 Skill不进入 prompt、catalog、child construction或工具权限链，raw metadata不授予能力。

### B7. 类型化 Presentation event consumer

当前事实：已有 `RetryStatus`、`UsageUpdated`、`RuntimeDurabilityStatus` 等明确 ViewEvent，但 Textual status bar仍用 `Any + getattr`读取字段，ACP/AG-UI等 adapter也使用宽事件和反射。现有 ViewEvent contract声明为 forward-compatible开放 tagged union，未知 kind可被旧 consumer忽略；原要求输入必须是 closed union与此保证冲突。`product/interfaces/structured/consumer.py` 还使用 `json.dumps(..., default=str)`，会把未知 scope、Path、enum或对象静默字符串化。

Durable disposition：ViewEvent、TranscriptReducer/widget/surface state与ACP/AG-UI/Structured wire均为从Session/machine facts重建的Product representation，不建立独立journal、逐帧历史或恢复truth；connection delivery/cursor由各transport contract拥有。该NOT_APPLICABLE结论不放宽strict type、wire version、scope identity或secret redaction。

实施要求：

- 按D12建立Product-generation-bound closed `ViewEvent` discriminated union、唯一catalog和strict codec；每个已知handler直接接收对应subtype，异构dispatch使用typed visitor/projector，不依赖动态subclass registry、import副作用、`ClassVar kind + getattr`或untyped handler dict；
- 新event同切片更新union/catalog、projector、capability adapter、全部production consumer disposition、wire codec和fixtures；gate双向验证union每项都有handler/disposition且不存在catalog外handler；
- ACP、AG-UI、Structured对每个已知event显式声明REPRESENTED、EXPLICITLY_DOWNGRADED或NOT_REPRESENTABLE并计量；禁止default unhandled→空列表或unknown silent ignore。安全/交互关键event无法表达时activation negotiation失败或typed unsupported；
- Structured JSON Lines使用绑定schema version、presentation generation和sequence的strict envelope，删除`json.dumps(default=str)`；ACP/AG-UI只在external adapter边缘容忍其协议允许的unknown extension，不反向开放内部union；
- external consumer协商共同generation，无共同generation拒绝activation/connection，同连接不混发。新内部generation同切片迁移全部仓内producer/consumer并退出旧generation，不保留双union/双projector；
- transport wire 动态值不得反向决定 control、approval 或 session lifecycle。

关闭条件：closed generation contract已落地；已知handler无`Any/getattr`，union/catalog/consumer disposition双向完整；不存在开放registry、unknown silent ignore、宽松字符串化、无证据空结果或内部多generation双路径，wire round-trip/generation negotiation/unknown tag负向fixture通过。

### B8. 固定 ViewEvent execution scope identity

当前事实：`product/presentation/events/events.py::ViewEvent.scope` 使用 `tuple[object, ...]`；Contracts-owned `TaskProgressEvent`、`ActivityStartedEvent`、`ToolCallFinishedEvent`等 machine event同样以 `tuple[object, ...]`表达scope。纯 `ScopeRef`/`ScopePath` declaration当前位于 `runtime/events/scope.py`，与 `ContextVar`、`push_scope`等ambient mechanism混在同一模块。只修ViewEvent末端会让上游machine event继续丢型，也无法建立projector/wire端到端identity。

实施要求：

- 先核验 `contracts/activity`及现有 identity/DTO，选择拥有跨层activity/execution identity不变量的 Contracts bounded context；将纯 `ScopeRef` DTO、kind enum/tag和 `ScopePath` declaration移入该 authoritative owner。Runtime只拥有ambient `ContextVar`、push/pop/current-scope mechanism；
- Contracts machine events、Product ViewEvent、projector和wire adapter复用同一scope declaration；同一切片删除 `tuple[object, ...]`与Runtime重复 declaration，不保留alias/re-export；
- projector从 committed machine event单向投影scope，consumer不自行拼接字符串或猜测层级；
- scope使用canonical versioned wire representation并由B7 strict ViewEvent encoder消费；不可编码、未知tag/version或identity mismatch为typed projection failure，禁止`default=str`；
- scope identity 绑定 run/agent/activity generation，stale scope 不得修改新 presentation lifecycle。

关闭条件：Scope纯声明只有一个Contracts owner，Runtime scope模块只保留运行机制；machine event到ViewEvent再到wire端到端使用同一authoritative type且无 `object`/重复 declaration；跨Agent/Activity同名节点不会串流；错误scope tag/version/identity fail closed。

### B9. 清理安全配置上的内部反射与宽松默认

当前事实：`SecretsCipherConfig`已要求`cipher`和`key_path`，但`build_cipher()`/`_build_aes()`仍以`getattr(..., default)`掩盖composition错误；Runtime `_REGISTRY`只有固定AES builder，没有provider discovery、external manifest或第二实现consumer。保留/类型化单项mutable registry会制造未识别变化轴，且Runtime自行用字符串选择安全策略。

实施要求：

- 核验现有Product config DTO/Contracts declaration并复用canonical类型；若`SecretsCipherConfig`确为跨Product/Runtime composition contract，移动/收敛到正确owner而不新造同义config。默认cipher只在Product schema验证阶段产生，Runtime接收已批准、完整配置；
- 对typed config直接访问必需字段，删除`getattr(..., default)`；当前唯一产品实现仍为AES-GCM时，Product composition显式构造AES adapter并删除`_REGISTRY`、register/mutation API和字符串runtime selection；不得用typed单项catalog替代；
- 只有用户确认cipher provider变化轴和真实第二consumer后，才由Product-owned显式catalog选择实现；Runtime不得拥有安全provider discovery；
- permission、cipher、sandbox、secret、approval 与 trust 配置缺失或 malformed 必须在 activation 前 fail closed；
- `getattr/hasattr` 仅允许在第三方 SDK/platform adapter 或显式 plugin discovery adapter，且结果立即校验。

关闭条件：安全配置consumer无反射fallback；AES-only现状下无单项cipher registry、register API或runtime字符串选择，且没有重复config DTO；未知cipher/policy/permission mode不启动资源或外部动作，composition错误typed fail closed。若未来有已确认多provider变化轴，必须另立Product catalog需求而非恢复Runtime registry。

### B10. 闭合 Artifact GC 与删除治理

当前已存在且必须复用的治理事实：`runtime/artifacts`已经拥有CAS、SQLite logical index、owner/retention与publication outbox，必须在该owner内扩展typed ownership edge、hold、deletion claim和GC receipt；不得新增RetentionManager、第二pin store或平行GC registry。Session lifecycle/delete intent仍归`runtime/session`，CAS物理删除权只归Artifact owner。

剩余反证：`ArtifactGarbageCollector`仍以一次reachability/pin snapshot加`minimum_age`直接回收CAS；`ArtifactPinRegistry` generation/source/direct pin/freeze lease只是进程内投影，不能证明跨进程无引用。Session workspace cleanup仍以rollout/目录mtime、stamp和调用参数legal-hold推导TTL并直接删除session/tool-results/task-outputs；它没有canonical edge completeness、durable deletion claim或每阶段fence复核。`mutation_guard`不能证明业务删除authority，stamp在sweep前写入还会使crash后的未完成删除延迟24小时。

实施要求：

- 实施前建立删除资源分类并逐 path/symbol归属：`canonical governed resource`受durable identity、retention/legal hold和外部消费者约束；`committed projection/cache`可由canonical generation确定性重建；`uncommitted owner-local temporary`尚未成为canonical fact且由创建owner在同一lifecycle清理；测试临时数据只在隔离根和明确test authority下处理。不得把所有`unlink/remove_tree`机械接入统一durable删除状态机；
- 只有 canonical governed resource 的不可逆删除必须先提交 typed deletion command/intent并取得durable fenced claim；committed projection/cache删除前验证canonical generation且不得删除唯一事实；owner-local temporary由创建owner按本地lifecycle清理，不发明全局identity/receipt；
- 在现有Artifact store中定义closed typed edge union，至少覆盖Session、Workflow、BackgroundTask terminal pointer、Tool/Model result、Hosted ServiceCall、Agent delivery、FileOps snapshot/before-image、stage/publication和legal hold。每条edge绑定EdgeId、domain owner/generation/fence、ArtifactId/digest、retention class、revision、created/released AbsoluteInstant与release command；active edge只以新edge commit后释放旧edge替换；
- canonical reachability closure必须来自同一revisioned snapshot generation，并由completeness manifest证明所有注册producer均推进到对应watermark；缺producer、generation不一致、decode corruption或manifest不完整均fail closed；
- 删除状态机固定为`REQUESTED -> CLAIMED -> REFERENCES_RELEASING -> METADATA_TOMBSTONED -> BLOBS_RECLAIMING -> DIRECTORY_RETIRING -> SETTLED`，并有`BLOCKED_ACTIVE_REFERENCE / BLOCKED_LEGAL_HOLD / BLOCKED_OWNER / IN_DOUBT / FAILED` disposition；claim绑定CommandId、resource identity、owner/lifecycle revision、lease/fence、pin/closure generation、retention、hold和authority；
- 在释放edge、tombstone metadata、reclaim blob、删除目录等每个不可逆阶段重新读取canonical facts并验证claim；metadata tombstone先于blob删除，partial success保持IN_DOUBT并按stage receipt继续，不能回滚为“未删除”；
- Artifact blob reachability必须覆盖Session、Workflow、BackgroundTask terminal pointer、Tool/Model result、FileOps snapshot、publication/stage、delivery/effect和legal hold，但各canonical owner只通过最小immutable root/pin/retention projection暴露其承诺的edge；collector不得理解或扫描其他包的私有map、数据库表、目录布局或mutable store。任一projection不可读/不完整时fail closed保留资源；
- `runtime/fileops/cursor_registry.py` 的 active cursor lease、observed snapshot 与 pin revision 是 Artifact reachability 的 canonical consumer；GC deletion claim 必须绑定并复核 cursor `epoch + pin_revision`，cursor 并发 issue/renew/observe 不得在旧 snapshot 后被删除；
- expired/released cursor lease、grant、pin 与旧 timeline transition 由 cursor owner 以 typed retention/purge command 有界清理；清理不能仅因 wall-clock expiry删除仍被审计、legal hold 或并发 generation引用的事实；
- `minimum_age`、mtime、目录位置和“当前进程未见 pin”只能作为调度提示，不能证明 unreachable；
- session TTL、用户删除、安全清除、测试临时数据和 legal-hold override 使用不同 typed command/receipt；
- `TTL_EXPIRE / USER_DELETE / SECURITY_CLEAR / LEGAL_HOLD_APPLY / LEGAL_HOLD_RELEASE / TEST_FIXTURE_CLEANUP`具有不同authority；Session owner只能释放本Session当前generation拥有的edge，其他domain各自释放自己的edge，CAS/workspace/CLI/test helper没有业务purge权；
- 分离Artifact blob reachability closure、Session directory retention closure和owner identity/tombstone retention；三者可复用canonical reference/claim contract与edge evidence，但删除对象、owner、lifecycle和不可逆阶段不同，不得合并为万能`RetentionManager`；
- workspace session删除必须消费各owner承诺的typed retention projection而不是只看run lease；任何未知/不可读projection、未结算delivery/effect/workflow/background task或pin都fail closed保留session，`mutation_guard`不能替代authoritative deletion permit；
- cleanup wake/throttle 丢失后，durable reconcile scan 仍能发现未完成删除，不能由 stamp mtime 延迟唯一推进路径。
- 按D01联合inventory SQLite owner/retention/outbox、CAS、FileOps、Session/tool/task-output与legacy roots，未知owner进入`ORPHAN_QUARANTINED` evidence而非按mtime删除；candidate、edges、holds、tombstones与completeness manifest必须单generation切换，无v1/v2 mixed writer；
- 执行D02/D07边界：EPHEMERAL/SESSION默认24小时/30天，PROJECT不按无访问自动删除；deletion tombstone保留1年，migration/orphan proof至少180天。Closure每批10,000 edges/5秒，deletion每批500且单generation事务100，soft/hard capacity 10/100 GiB；达到hard只停止新content admission，继续settlement/cleanup。

关闭条件：现有Artifact store是唯一edge/hold/deletion/GC owner，Session仅拥有lifecycle/delete intent；所有edge producer与completeness manifest双向一致，旧collector不能删除新generation引用；跨进程竞争只有current fence推进每个不可逆stage，crash可从receipt恢复；无mtime/minimum-age/stamp/参数式hold或直接remove_tree/reclaim旁路。owner-local temp/cache不误纳入业务状态机，未知owner/producer fail closed，retention/capacity/closure scan均有确定性边界。

### B11. RunLease、Session event 与 inference restore metadata 交叉审计索引

当前事实：`runtime/session/run_lease.py`使用无版本裸映射并以`str/int/float`解码；`runtime/session/events.py`的operation/handoff/projection/ack decoder广泛强转primitive；Inference timer/checkpoint和SQLite restore metadata也存在宽松解析、缺字段默认及损坏即跳过。但这些分别归Session rollout/lease、Inference execution和Product inference persistence owner；B11本身不是canonical owner。Session lease/event/checkpoint inventory现统一指向第3.3.17节Session v2 migration，不再为它们建立多个Session migration或store。

实施要求：

- B11只维护symbol→canonical owner→独立需求矩阵：Inference checkpoint/timer归B2；Session event family归B5或独立Session codec需求；RunLease归Session lease独立需求；SQLite restore metadata归Product inference persistence独立需求；每个具体symbol只能由一个实施单元关闭；
- 各实施需求分别要求versioned exact-shape envelope、strict primitive/identity/generation/instant、typed corruption/unsupported-version disposition和明确migration/拒绝策略；缺失fact与decoder failure不得混同；
- 禁止建立`B11 codec`、跨owner shared decoder、重复migration或为统一验收而交叉修改多个authority；最终只由矩阵和门禁证明无漏项/重复owner。

关闭条件：B11矩阵中每个symbol均唯一指向已独立签收需求，B2/B5与新增lease/restore需求无重叠decoder或migration；相应负向证据共同证明数字字符串、bool token、NaN/Infinity、缺失/额外字段、unknown version和wrong identity fail closed。B11自身不产生生产类型、codec、store、Port或实施状态机。

### B12. Permission 与 Hook composition 必须 fail closed

当前事实：`runtime/tools/policy.py::build_permission_engine()`在`permission_config is None and not require_permission`时返回`None`，而`require_permission=True`时会以默认bypass config构造engine；`None`可能被不同调用方解释为unavailable、not applicable或composition error，`require_permission` bool承担了信任边界。Hook matcher非法regex会降级为exact match。`HookManager.register(event, fn, matcher)`还允许构造后以任意event字符串动态注册Python callback，无稳定identity、activation generation或预编译matcher；`parse_callback_result()`对未知返回类型先降级为空结果。

实施要求：

- 按immutable published binding/effect classification枚举全部Tool调用；所有published Tool无论读写均进入唯一ToolExecutor authorization/sandbox/effect链。Product composition提供versioned baseline与typed disposition，至少区分REQUIRED_ACTIVE、UNAVAILABLE和受限EXPLICIT_BYPASS；普通workspace read/search可由baseline自动ALLOW但仍执行target/path/secret/capability检查。`NOT_APPLICABLE`只用于非published、非模型可选且无IO/secret/capability/state mutation/external effect的内部纯计算，不能覆盖read-only Tool；`None`与`require_permission` bool不再表达信任语义，baseline/engine缺失必须deny；
- `EXPLICIT_BYPASS`只能由已批准Product policy选择，绑定effect/tool definition generation、principal、scope、理由和audit identity；不能由默认config或engine缺失推导；
- config handler和programmatic callback均在activation前编译为带event、callback identity、matcher、权限单调收窄语义和generation的immutable Hook snapshot；非法regex、未知event、重复identity或malformed group fail closed；
- 按已确认D15，activation后freeze并拒绝`register()` mutation；更新只能通过下一Product/Role generation，当前不保留hot swap、live逐项修改或动态callback registry；
- control callback在adapter边缘严格解码；未知返回类型直接产生typed decode failure/deny，不先降级为空结果再依赖外围异常；
- Hook config consumer 直接访问 typed fields，不用 `getattr(..., default)` 将错误 shape 降级为空配置；
- control Hook timeout/crash/decode/unknown decision 继续 fail closed，observation Hook 的 best effort 不得影响 control fact。

关闭条件：每个effect类别对permission activation的语义唯一；governed effect在UNAVAILABLE/composition error时runner调用为零，NOT_APPLICABLE不被滥用于effect，EXPLICIT_BYPASS可追溯到批准generation/audit；Hook callback/config共享一个immutable generation snapshot，activation后无旁路注册，malformed matcher/result在adapter边缘fail closed。

### B13. 拆除低内聚 `RuntimeMaintenance` 并归还各 subsystem lifecycle

当前事实：`runtime/agent/runtime_maintenance.py::RuntimeMaintenance`持有完整Role并通过`role.wiring.services`反射取gate，失败时私建gate；同时聚合advisory code-map reindex/cold scan、破坏性workspace cleanup/Artifact GC、`schedule_reconciliation(name, Callable[[], Awaitable[bool]])`任意调度、Skill hot reload、MCP hot reload，以及多组task/gate/retry/backoff/shutdown状态。这些能力的scope、authority、durability、安全性和失败语义不同，不能通过注入更多gate继续保留巨型manager。

实施要求：

- 按共同不变量拆分并迁移全部consumer：code-map scan由repository-scoped advisory owner管理；workspace cleanup/Artifact GC由destructive retention owner管理authority/fence/fail-closed；每个durable reconciliation由对应subsystem管理scan/wake/backoff；Skill/MCP reload归Product activation generation owner；
- 删除generic `schedule_reconciliation(name, callback)`，不允许字符串name/任意callback成为跨domain scheduler；每个subsystem使用自身typed command/query和task lifecycle；
- `CodeMapScanGate`与`WorkspaceCleanupGate`即使方法形状相似也必须保持不同窄service/Port：前者advisory，后者destructive，authority、scope、失败与恢复保证不同；禁止通用`CoordinationGate`、字符串key或万能permit；
- Product composition分别装配这些owner及activation/shutdown，不创建新的`MaintenanceManager` facade；迁移后删除`RuntimeMaintenance`、Role/service-locator访问、fallback gate和相关公共导出；
- 每个owner分别闭合task identity、取消、shutdown、retry/backoff和typed settlement，不共享mutable task registry。

关闭条件：`RuntimeMaintenance`、generic reconciliation callback scheduler及公共facade残渣为零；code-map、destructive retention、各durable reconciliation、Skill/MCP activation分别只有一个owner/lifecycle；CodeMap/Workspace无通用gate abstraction；composition缺失typed unavailable而非私建fallback，且没有新的万能maintenance manager。

### B14. Tool snapshot 必须可恢复且不穿透 executor 私有状态

当前事实：`runtime/tools/snapshots.py` 直接访问 `executor._catalog`，并将 live binding/executor 捕获为进程内调用 closure；durable checkpoint 只保留 snapshot id/revision，进程重启后只能返回 `unrecoverable_binding`。

实施要求：

- snapshot 使用 immutable canonical definition/binding projection，不读取 executor 私有 map；
- snapshot identity 绑定 tool definition generation、semantic identity、argument schema、effect、permission target semantics、Product activation digest 和 provider/MCP generation；
- 先核验现有 Product composition blueprint、tool definition generation、MCP/provider generation和activation digest能否确定性重建 binding；优先由 Product按可信配置构造当前批准 generation，Runtime snapshot record只引用该已批准 generation identity，并从 composition提供的 immutable binding catalog重建；identity/schema/effect/permission digest不匹配即拒绝恢复；
- 不预设新增 durable Product generation artifact/store。只有现有 composition证据无法满足已确认的跨重启恢复保证时，独立需求才可提出新artifact，并必须提交 `AGENTS.md §6.4` 的现有机制检索、拒绝复用、owner/lifecycle/retention和防双store证据；磁盘snapshot record始终不能选择任意 capability factory；
- live closure/instance 仅为当前 incarnation cache，不是 snapshot 的 authoritative identity；
- release/retention 与 active inference/turn/checkpoint reference 原子结算。

关闭条件：进程重启后同一已批准 generation可由唯一 Product composition链重建精确binding；generation/digest/schema改变时拒绝旧snapshot；snapshot manager不访问 `_catalog`或持有executor closure作为唯一执行权；未证明现有composition不足时没有新增generation artifact/store，若新增则不存在与composition activation并行的第二generation truth。

### B15. 按 owner 拆分 Tool、Connection 与 control command lifecycle settlement

当前事实：`ToolLifecycle._restore_configured_toolsets()` 吞掉恢复失败；MCP candidate 失败清理可能丢失 cleanup failure；`ConnectionScope.aclose()` 吞掉 telemetry unsubscribe、human binding reset 和 port close 失败，并清除本地绑定标记。Product interaction driver、Terminal/Textual port 与 ACP cancel handler 又通过无 owner handle 的 `asyncio.ensure_future(...)` 发出 interrupt/steer control command；异常无人观察、shutdown 不等待，界面可能已经提示 interrupt/steer 但 authoritative Agent control并未执行。

实施要求：

- B15不得形成单一实施需求或共享cleanup manager：Tool/MCP restore、generation activation与prior-generation cleanup进入F2 Tool lifecycle；ConnectionScope、telemetry/human binding/projector/port close进入F8 Product connection lifecycle；interrupt/cancel/steer receipt进入F3 authoritative Agent/session control owner，F8只消费窄command Port；
- lifecycle close/restore 返回 typed settlement，至少区分 settled、draining、cleanup failed、owner/generation lost；
- restore/cleanup 失败保持不可接受新工作的状态，不将 catalog、binding token 或 lifecycle flag伪装为已恢复；
- MCP generation activation 与 prior-generation cleanup 分开结算，外部/远端动作已发生后失败进入 typed in-doubt/degraded；
- Connection close为owner-local分阶段state machine：telemetry handle、human binding token、projector和port分别记录settlement；成功阶段不重复执行非幂等close；reset失败保留唯一generation token与owner identity，Connection保持DRAINING、拒绝rebind和新turn，并在receipt列出未结算阶段供同owner幂等续清理；
- process退出前仍无法settle时，由已确认Product policy决定强制终止或泄漏报告；不得清除token、开放新binding或伪装CLOSED；
- interrupt、cancel、steer 等 control command 由 connection/session owner 持有 task/command receipt，支持幂等、取消、close drain和typed failure；禁止无 handle fire-and-forget；
- UI 只有在 control command accepted/settled 后呈现对应状态，异步排队则呈现 typed pending，不能把 coroutine scheduled 当作动作成功；
- presentation consumer 的 best-effort close 可记录降级，但 control/human capability cleanup 不能吞错。

关闭条件：三套独立需求分别签收且无共享状态机/registry/cleanup manager；fault injection覆盖Tool、Connection各cleanup阶段；Connection reset失败保留token和DRAINING状态，重复close只续跑未结算阶段；control command由Agent/session owner给出receipt，UI只投影结果；生产路径不再`except Exception: pass`后宣称lifecycle已关闭。

### B16. 删除 import-side-effect registry

当前事实：`product/i18n/__init__.py`与`product/i18n/catalog/__init__.py`依赖导包副作用注册zh/en catalog；公开`register_catalog()`允许运行时修改catalog，`register_rule()`是无生产消费者的plural-rule mutation入口，`_CATALOGS`与`_RULES`形成locale generation之外两套mutable state，process default locale与`ContextVar` active locale scope也未分清。Temporal/Squilla则对固定仓内单实现使用dynamic import、伪manifest/catalog和`getattr + cast`，没有真实provider选择轴。

实施要求：

- 若当前只有内置en/zh且无已确认hot-load变化轴，删除`register_catalog`、无消费者`register_rule`及公共mutable registry入口；Product composition直接构造同时包含locale definition、catalog、plural rule与fallback policy的immutable snapshot；
- application locale catalog snapshot generation与connection/request active locale identity分离；`set_locale`不得逐项修改全局catalog/plural内容，generation变化整体原子替换snapshot；
- import 不修改 registry，不依赖模块加载顺序，不允许测试 import 顺序改变行为；
- 未来若确认外部locale provider，再以显式manifest建立新变化轴；当前不为未使用mutation API保留plugin入口；
- Temporal/Squilla等固定可选backend使用模块顶部可选依赖import或静态typed factory注入；当前只有单实现且无已识别变化轴时删除伪manifest、loader、单项catalog和generation状态，不得以Product-owned catalog包装固定依赖；
- 禁止以 `getattr + cast` 充当 factory contract 验证，factory 必须由静态 Protocol/具体类型表达并在 composition activation 时 fail closed。

关闭条件：导入i18n模块无状态副作用；无公开catalog/plural mutation入口或无消费者`register_rule`残渣；同一application generation的locale definition/catalog/plural/fallback是单一immutable snapshot，connection只绑定active locale identity；Temporal/Squilla无动态import、伪manifest/loader或单项catalog。除经批准且由真实manifest驱动的外部discovery外，生产内部依赖无`importlib/__import__`。

### B17. 固定内部命令统一消费 typed argv runner

当前事实：File candidate discovery、worktree checkpoint和sandbox resource probe仍直接使用同步 `subprocess.run`；Product clipboard直接 `Popen`且没有完整process settlement。现有 `runtime.process.run_fixed_argv()` 是async one-shot runner，不能要求同步consumer直接复用而不决定其lifecycle；用 `asyncio.run()`、私建thread/event loop桥接会破坏已有loop、取消和shutdown语义。当前runner在 `env=None`时还继承完整进程环境。

实施要求：

- 按已确认D13把所有production fixed-program consumer迁入既有async lifecycle并await唯一async typed runner；同步CLI只在最外层Product entrypoint运行一次application coroutine。Runtime/adapter不得保留同步fixed-argv入口、嵌套`asyncio.run()`、thread/private-loop桥接或同步/异步双API；
- Product activation为每个consumer生成immutable verified executable binding，执行前复核absolute regular-file identity；PATH只在activation解析，effect执行时不得重新选择程序。各consumer分别拥有argv/env/cwd policy，共享底层spawn/receipt原语而不是万能高层runner；
- ripgrep、git、systemctl等固定内部命令通过所选canonical fixed-argv service执行，明确cwd、timeout、output bound、signal/cancellation与typed result；
- 最小环境策略由Product或调用domain按真实命令声明/编译，明确PATH、locale、Git/systemd及必要home/config语义；Runtime runner验证并执行已编译环境，不能自行猜测，也不得以`env=None`继承完整进程环境或把provider secret写入argv/log；
- interactive/daemon/platform integration 若生命周期不同，使用其独立 typed start/health/stop owner，不伪装为 one-shot runner；
- clipboard 明确是否为 presentation-owned daemon/one-shot adapter，并报告 spawn/write/exit failure。

关闭条件：除已声明interactive/daemon owner和最底层canonical runner implementation外，生产源码无直接`subprocess.run/Popen/create_subprocess_*`；fixed-program生产consumer只存在一个async verified入口，Runtime/adapter无同步runner、`asyncio.run()`或私有loop桥接；每个命令有可审计binding与最小环境fixture且不默认继承secret，固定内部argv不经shell，用户命令不能进入fixed runner。

### B18. 裸 `dict` 正式边界债务交叉审计与最终 ratchet 索引

当前事实：全仓仍有大量 `dict`/`dict[str, Any]`，其中既包含合理 JSON/wire adapter，也包含 durable event、Workflow snapshot、Tool catalog/arguments、Skill metadata、presentation event、permission facts 和内部 mutable service state。关键词本身不能判错，但已确认多个正式边界用裸 dict 替代已知 shape。首轮确认的具体债务至少包括：

- `contracts/inference/executions.py`、`attempt.py`、`transport.py` 把跨进程 execution payload、application message、model invocation 和 provider result 定义为 `dict[str, Any]`，没有 domain-owned tagged payload 或受限 JSON value；
- `contracts/hook/invocation.py` 与 `contracts/ports/hook/runner.py` 已经拥有 typed invocation union，却仍同时公开 `event: str + payload: dict` 入口，tool input/error 也为裸 dict，形成 typed/unchecked 双入口；
- `contracts/tool/actions.py`、`protocol.py`、`catalog.py`、`ports/tool/policy.py` 将模型 tool arguments、schema、dispatch request 和 permission resolver 固定为 `dict[str, Any]`，effect identity 到 authorization 的 canonical arguments 形状未被类型系统约束；
- `contracts/inference/events.py`、`contracts/events/model.py`、`contracts/events/tool.py` 与 `runtime/session/events.py` 持久化任意 payload/decision/state/usage/tool input，部分 decoder 只复制或强转而没有 exact-shape validation；
- `orchestration/workflows/definition.py` 的 `RunSnapshot.state: Mapping[str, Any]`、`initial_input: dict[str, Any]` 和 definition payload，以及 `workflows/types.py::GraphState(extra="allow")`/node params，把 durable Workflow state 与可执行 definition 退化为开放 mapping；
- `runtime/session/projection.py` 的 routing decision、output state 和 session meta read model，以及 `runtime/agent/role_state.py` 的 pending output restore，使用裸 dict 保存或重新投影恢复状态，没有 canonical immutable snapshot type；
- `runtime/tools/base_executor.py`、`tool_pipeline.py`、`tool_binding.py`、`tool_settlement.py` 和 `bound_registry.py` 继续以 `dict[str, Any]` 贯穿执行、permission、effect settlement 与 pinned invocation，扩大了 B4/B14 已识别的 capability/snapshot 缺口。

首轮明确不应机械判错的类别：`dict[str, JsonValue]` 形式的开放 JSON 文档树、只在 owner 内部使用且值类型完整的索引、严格 decoder 的临时 object map，以及 ACP/AGUI/provider SDK 边缘的 wire encoder/decoder；这些仍须证明不越过 adapter 边界且不会泄漏可变引用。

实施要求：

- B18不创建独立实施需求，不拥有contract、codec、base DTO、migration或中央“消灭dict”工程；建立逐symbol分类/分派表：`external wire`、`strict decoder input`、`canonical JSON value`、`private mutable implementation`、`formal boundary debt`，并记录canonical owner、目标类型、consumer、唯一domain实施单和验收；
- 已由B2/B4/B5/B7/B12/B14/B20/B21/B25/B29等覆盖的symbol只引用其唯一owner，不在B18重复设计/迁移；同一dict跨durable/wire/service多层时由最外部adapter解码一次，之后贯穿同一canonical type，不在每层各包同义DTO；
- external wire/decoder 的 dict 只存在于 adapter 私有函数，立即严格投影为 canonical DTO；
- durable、跨层、跨包 service、permission/effect、lifecycle、checkpoint、registry snapshot 与泛型 result 不得返回裸 dict；
- `dict[str, JsonValue]`/`Mapping[str, JsonValue]`也必须证明真实开放JSON语义、深冻结、大小/深度边界且不承担已知状态机shape；不能仅凭值类型进入合法类别；
- 命令返回 typed receipt/query snapshot，调用者不得通过取得 mutable dict 绕过 owner；
- 不为消除 dict 创建万能 envelope 或巨型 DTO，每个 shape 归真实 domain owner。

关闭条件：B18矩阵中每个formal boundary debt唯一归属已签收domain需求，B18自身无生产类型/codec/migration；正式public surface的dict debt由typed tests和少量AST ratchet证明为零。最终ratchet禁止自动接受当前扫描基线或中央永久allowlist；必要动态点就近记录symbol/owner/category/reason/negative test。owner内部合法索引不入中央清单，开放JsonValue有语义/冻结/大小深度证据，无多层同义DTO。

### B19. 分离 MessageQueue Runtime mechanism、Agent mailbox 与 durable delivery owner

当前事实：`orchestration/agents/messaging/mailbox.py::InterAgentCommunication` 使用 `ConfigDict(arbitrary_types_allowed=True)` 和 `attachments: list[object]`，但 `to_message()` 只投影 content/author/recipient/kind/channel，attachments 被静默丢弃。`contracts/conversation/queue.py::MessageQueue` 不是纯 DTO：它拥有可变 `_items`、`asyncio.Event`、push/pop/drain/wait 调度行为和 JSON dump/load 恢复逻辑，违反 Contracts 纯数据/窄 Port 边界；其 `load()` 还会吞掉损坏 JSON 并恢复为空队列。源码同时存在 Role/Kernel 上下文使用的进程内 `MessageQueue` 和由 Agent runtime ownership、turn boundary、delivery identity 管理的 Orchestration `Mailbox`，两者 owner、lifecycle、恢复来源和 durability 保证不同，不能因都承载 `Message` 而统一为一个 queue codec。

实施要求：

- 将 `MessageQueue` 的 mutable queue、event、push/pop/drain/wait 等 process-local mechanism 移出 Contracts，归入拥有其 incarnation/lifecycle 的 Runtime bounded context；Contracts 只保留 `Message` DTO 与真实跨层消费者所需的最小 message sink/activity Port。是否需要 durable mailbox projection 只影响 codec owner，不改变 queue mechanism 必须移出的结论；不得在 Contracts DTO 中保存 `asyncio.Event`、mutable queue 或以 Pydantic arbitrary type 掩盖 runtime state；
- 按D19 owner separation固定投影关系：messaging owner是delivery truth，turn_queue owner是TurnRequest/scheduling truth；Mailbox、Residency cursor、PendingDelivery hint和Runtime buffer均为有界可重建projection，不拥有accept/assignment/ack。Runtime `MessageQueue`只管理incarnation内缓冲；不得保存canonical mailbox payload snapshot、queue dump第二truth或用projection drop改变delivery；
- attachment 使用 domain-owned versioned tagged union 或 canonical artifact reference，绑定 delivery retention；未知 tag/version、额外字段和错误 primitive fail closed；
- `InterAgentCommunication -> Message` 必须完整、可验证地投影所有已承诺字段；不支持的 attachment 在 admission 前 typed reject，禁止接受后静默丢弃；
- 若 owner 审计证明仍需要持久 mailbox projection，其 codec 归 delivery/session durable owner并使用版本化 envelope、exact-shape decoder 和 typed corruption/unsupported-version result；若没有真实 durable consumer，删除 `MessageQueue.dump/load`，不得为保留错误 owner 而加固 codec；
- priority、delivery identity、owner Agent/lifecycle generation/fence 与 enqueue sequence 只出现在拥有这些不变量的 authoritative projection 中；Runtime process-local queue 不伪造 delivery fence或 durable acceptance。

关闭条件：`contracts/`不再拥有queue/event/wait/drain/restore mechanism；delivery与turn两个canonical owner边界明确，Mailbox/Residency/Pending/Runtime均可删除后重建且不能反写；accepted payload不丢字段，projection损坏/容量满不改变durable事实，process-local queue不伪装accepted。

### B20. Surface document 与交互输入 schema 必须 fail closed

当前事实：Canvas/Notebook DTO宽松解析。更具体地，`CanvasOperation`以tag加optional字段表达upsert/remove/clear，仍接受tag不匹配字段组合；`NotebookOutput`同样以`output_type`加optional/default字段混合stream/execute_result/display_data/error，protocol内矛盾shape可通过。`NotebookInputRequest/Reply`只有自由字符串request/cell/value，缺少surface/document revision、kernel epoch和connection/human generation；restart/reconnect后旧reply可能进入新incarnation。

实施要求：

- `contracts/surface/` 可提供仅封装 frozen、strict、`extra="forbid"` 共同配置的私有/受限 DTO 基类，或采用逐类等价配置；该基类不得拥有 schema version、tag dispatch、codec catalog、extension policy、大小限制或状态机行为；
- Canvas、Notebook、stdin 与 execute input 分别由其 domain declaration/codec owner 定义 version、tag、大小限制、trust/rendering 和 extension 语义；字段相似不得成为统一 document envelope 或状态机的理由；
- CanvasOperation和NotebookOutput使用真正discriminated union或domain codec等价exact variant校验；每个variant只拥有合法字段，upsert/remove/clear及stream/execute_result/display_data/error拒绝所有tag不匹配字段、默认空值伪装和错误identity格式；
- Notebook input request reference绑定surface/notebook identity、cell identity、document/request revision、kernel epoch和human connection generation；reply携带完整opaque reference与expected revision，由Notebook input owner以CAS返回REPLIED、ALREADY_REPLIED、STALE、CANCELLED、KERNEL_RESTARTED、OWNER_GONE或INVALID_VALUE；
- request terminal/cancel、kernel restart和connection close与reply admission在同一owner/generation gate原子互斥，旧window reply不能提交到新incarnation；password input value不得进入普通event/log/artifact/exception detail/presentation echo；
- `metadata/extensions: dict[str, JsonValue]` 仅在确有开放扩展语义时保留，并明确 namespace、大小限制、信任/渲染规则和 round-trip 保证；不得让 extension 覆盖 canonical 字段；
- artifact 编解码和 Product presentation adapter 只消费验证后的 canonical document，外部 ipynb/provider payload 在 adapter 内立即投影；
- Notebook document/cell/output不建立独立durable store或以导出`.ipynb`作为恢复truth；validated Runtime checkpoint identity/digest进入Session rollout，payload由Artifact edge持有。Notebook export保持确定性projection，恢复只消费verified current Session generation与kernel epoch；
- 对旧 artifact 明确一次性 migration 或拒绝策略，不建立长期宽松双读。

关闭条件：Surface DTO不忽略字段/强转primitive；Canvas/Notebook每个union variant拒绝互斥字段组合；stale/cancelled/restarted Notebook reply无法跨surface/kernel/connection generation提交且password无泄漏；Notebook checkpoint只经Session fact+Artifact edge恢复，无独立Notebook/ipynb truth；各domain只有一个schema/input lifecycle truth，无拥有codec状态机的泛用基类或万能envelope。

### B21. OAuth credential 状态与 provider response 严格类型化

当前事实：`runtime/models/auth/oauth/models.py` 中 `TokenClaims`、`DeviceCodeInfo`、`OAuthToken` 直接使用默认 `BaseModel`；未知字段会被忽略、primitive 可被 Pydantic 宽松转换，`TokenClaims.raw: Dict` 可保存任意对象。`expires_at`/`exp` 参与 credential 有效性和刷新决策，却没有 strict finite-number、absolute clock/schema-version 约束。该数据还会进入 credential store，因此不是可容忍的临时 SDK dict。

补充事实：File/Keyring commit先load再以进程内expected revision检查，缺少跨进程CAS；Fallback store的lock只保护构造期backend selection。`commit(None)`以token为空record表达logout，但selection与credential record是两份恢复事实；selection存在而record丢失/回滚/来自不同backup时，`load_record()`返回`None`，无法区分从未登录、logout tombstone、material丢失或selection/generation不一致。Provider client还能自行选择backend或触发refresh，使旧refresh可能复活已撤销状态，并形成credential lifecycle的第二owner。

实施要求：

- `runtime/models/auth/oauth`只拥有credential metadata lifecycle；canonical vault独占access/refresh/client secret material。Metadata只保存`SecretRef`，OAuth JSON、receipt、日志、异常和普通telemetry均不得保存明文token或raw JWT claims；
- canonical metadata使用versioned、frozen、strict、`extra="forbid"` DTO，状态集合固定为`ACTIVE / REFRESHING / REAUTH_REQUIRED / REVOCATION_PENDING / REVOKED / MATERIAL_LOST / IN_DOUBT / OWNER_ACTION_REQUIRED / RETIRED`；`ABSENT`仅是查询结果，不能与`REVOKED`或material丢失共用空值；
- provider token/device/JWT response只在OAuth adapter边缘保留外部wire shape，并立即按1 MiB response、64 KiB secret/claims、256 scopes及单scope 256字符的已确认上界严格投影；unknown/extra字段、错误primitive、非有限时间、错误identity或越界均fail closed。`exp`等时间转换为`AbsoluteInstant`并使用注入clock，raw claims不得决定subject identity或scope authority；
- Product activation必须为每个approved subject冻结唯一`FILE_VAULT_V1`或`OS_KEYRING_V1` binding。`fallback`只作为v1→v2首次inventory resolver，运行期不得fallback、按mtime/revision猜测或让provider client自行选择backend；keyring不承载mutable metadata，不支持shared atomic mutation的部署必须拒绝shared mode；
- subject metadata mutation由唯一owner以expected revision、credential generation和跨进程CAS/fence推进；每subject最多一个active generation、一个mutation及64个generation-bound borrow。Vault先写inactive generation并read-back验证，再由metadata CAS发布；发布前不可borrow，旧generation在borrow/effect/rollback结算前受控pin；
- login、refresh、local logout、provider revoke、backend migration、config retirement、conflict resolution、TTL、hold和security clear均为不同typed command/receipt。`commit(None)`必须删除；`REVOKED`/`RETIRED`保留typed tombstone，物理缺失只在严格inventory证明从未存在时投影为`ABSENT`；
- refresh/revoke是`D11-oauth-refresh-and-revocation-effect-v1`定义的NON_REPLAYABLE effect：网络动作前提交durable intent，自动refresh最多一次网络attempt；timeout、crash或远端可能成功而本地receipt/metadata commit失败进入`IN_DOUBT`，不得盲重试。Provider response先进入attempt-scoped secret evidence，再写inactive material并CAS；stale response不得发布或覆盖logout/new login；
- v1→v2 activation前一次性inventory selector/file/keyring/config/vault并生成可审计cutover receipt。零source、单source、多source一致、多source冲突、selector/material丢失、config/store冲突和mixed backup generation分别按`D01-oauth-credential-v1-to-v2`处置；partial failure阻断该subject activation，首次v2后旧file/keyring/fallback path仅为migration-only，禁止长期双读双写；
- retention、purge和owner action按D02/D03执行：过期/rotated secret在全部borrow/effect/rollback结算后尽快crypto-erase且正常最长24小时；terminal完整非secret metadata保留90天后compact为1年tombstone；migration proof保留180天。`MATERIAL_LOST`/`IN_DOUBT`不因TTL自动terminal，hold与security clear使用不同authority；
- reconcile、interactive/device flow和metadata storage执行D07上界：每integration subjects默认32/hard 1,000，interactive默认10分钟/hard 30分钟，connect 10秒/response 30秒，reconcile每批200 subjects且5秒，retirement每批100 generations，metadata每subject 256 KiB。Extension只能收窄这些边界；
- MCP与LLM consumer只获得绑定provider/account/scope/consumer authorization及credential generation的短时borrow capability；二者可复用同一lifecycle mechanism但不得共享模糊default credential，health/query authority不得取得secret或推进credential lifecycle。

关闭条件：OAuth安全决策只消费strict metadata与generation-bound secret borrow；`ABSENT`、`REVOKED`、`MATERIAL_LOST`、corrupt和binding/generation mismatch可机械区分；v1 inventory/cutover无mixed generation或运行期fallback；并发owner不能以同一expected revision提交不同状态；provider动作前后crash fixture证明logout/new login获胜且unknown remote result进入`IN_DOUBT`；plaintext/raw claims在metadata、receipt、日志与telemetry中为零；retention、authority、容量和MCP/LLM binding均有负向与边界证据，旧codec/backend selection/`commit(None)`/provider-owned refresh入口全部退出。

### B22. 收敛 Role 对外对象图为最小 typed capability surface

当前事实：`runtime/agent/role.py` 公开 `components: RoleComponents`，并通过大量 property 直接暴露 `ToolExecutor`、journal、artifact store/resolver/publisher、runtime host、hook manager、LSP service、sandbox runtime、Context、BackgroundTask service 等具体实现。`RoleComponentAccessors` 虽自称 stable Role-facing API，仍直接返回 `ToolExecutor`、`RunJournal`、`RuntimeHost`、`SQLiteSubscriptionStateStore` 等具体 Runtime 类型，因此是待删除的现状，不是可继续加固的第二公共面。调用方可以穿透 owner 读取 registry/store 或调用 lifecycle/control 方法；`runtime/agent/runtime_maintenance.py` 通过 `role.wiring.services` 取 gate 只是已观察到的一个实例。

实施要求：

- 全仓枚举每个 `role.components`、`role.wiring`、`RoleComponentAccessors` 和 concrete component property 消费者，并逐项分类：Runtime 同一 bounded context 内可使用 owner 私有具体类型；Runtime 同层跨 bounded context 使用被调用包承诺的最小稳定 public service/Port；只有真正跨层且需要依赖反转的能力才进入消费方定义的 Contracts-owned 最小 Port。禁止为了表面一致把 Runtime 实现细节提升为十年稳定 Contracts API；
- `RoleComponents`、component graph/slots、wiring、具体 store/client/registry/lock/task 均保持 Runtime package-private，不作为 Role 公共面；
- query 返回 immutable snapshot/projection，command 返回 typed receipt；不得返回 live catalog、mutable collection、具体 backend 或 lifecycle resource；
- Product composition 是唯一可见具体实现并完成注入的位置；Orchestration 只依赖 RunnableAgent/治理所需窄 Port，不通过 Role 取得 Runtime 实现；
- 在对应 owner 切片迁移其全部仓内消费者并删除旧 property 和 `RoleComponentAccessors` concrete-return seam，不保留 facade alias、兼容 property 或第二套 control path；跨 owner 消费者不得用一次巨型切片半迁移；
- 架构门禁禁止 `runtime.agent.role.Role` 新增返回具体 component/service 的公开 property，并禁止包外 import `role_components`/wiring 内部类型。

关闭条件：包外无法从 Role 或 accessor facade 获取完整组件图、内部 mutable state 或具体 backend；每个保留的 Role 公共方法对应明确用例和 typed contract；每个 consumer 的协作方式与其层级/ bounded context 匹配，无无消费者 Contracts Port、Port 爆炸或错误 owner；B13 maintenance gate 等消费者只走唯一 owner service/Port。

### B23. 闭合 Agent delivery 容量 admission、typed receipt 与有界恢复

当前事实：当前源码已有 `orchestration/agents/messaging/durable.py::AgentDeliveryStore`：send 在入 mailbox 前写 durable ACCEPTED，启动时扫描 pending record 重建内存投影，delivery claim 绑定 target generation，turn 成功后 ack，terminal target 可 dead-letter。因此不能再把 `PendingDeliveryQueue` 描述为唯一真相源或断言重启必然丢 accepted delivery。剩余真实缺口是：`AgentDeliveryStore.accept()` 在任何 delivery storage/queue capacity admission 前无界写入全量 JSON store，`PendingDeliveryQueue` 也无界；`send_input/send_inter_agent_communication` 只返回 `Optional[AgentRuntime]`，无法表达 accepted/queued/backpressured/rejected；“never fails”允许永久无 slot 的 target 无限积压；mode 仍以裸字符串持久化，initial target generation 为 0，且缺少明确 retry/dead-letter/retention 上界和公平调度证据。其 `identity()` 只组合 `message.sent_from + target_agent_id + message.id`，重复 `delivery_id` 会直接返回既有 record，却不校验 payload、delivery mode、target/generation 等 canonical request facts，可能把 identity reuse 或参数冲突误报为幂等成功。

实施要求：

- 按D07在delivery durable accept事务内同时原子检查target/root/deployment容量；满时typed BACKPRESSURED/REJECTED且不写ACCEPTED、不无限park。Projection/hint容量独立且满时停止投影、依赖durable scan，不能丢canonical delivery；
- delivery command/query只由messaging owner实现；Control/Product/Runtime不能直接写store，PendingDeliveryQueue不能返回durable accepted、dead-letter或drop canonical record；
- payload/batch/store/scan/maintenance bounds、target terminal batch与cancellation epoch按D07执行；大payload走ArtifactRef且建立ownership edge，禁止截断或私建blob；

- 保留并收敛唯一 durable delivery owner；进程内 mailbox/pending queue 只能是可重建投影，启动与周期 reconcile 都从 canonical pending facts 恢复；
- durable state machine 的 claim/ack 继续绑定 target AgentId、lifecycle generation、incarnation 与 monotonic fence，并用 typed `DeliveryMode`/disposition 替代持久裸字符串；
- 为 delivery accept 定义 stable request identity、canonical payload/arguments digest、mode、target 与 target generation。只有 identity 与全部 canonical digest/事实一致才允许幂等返回；相同 identity但 target、mode、payload、generation不同必须返回 typed `IDENTITY_CONFLICT`，不得创建第二 delivery fact；
- broadcast/subtree fan-out 使用稳定 parent request identity，并为每个 target 派生独立 child delivery identity/digest；单 target 的重试和结算不改变或复用其他 target identity；
- durable accept 前只原子取得 delivery owner 管理的有界 storage/queue admission，并定义 receipt、reservation lifecycle 和唯一释放事实；不得预占或长期持有 logical Agent、resident incarnation 或 concurrent turn permit。logical/resident/turn capacity 分别由其 canonical owner在实际 lifecycle/turn admission 时处理，B27 的 turn acceptance 不与 delivery accept 合并为一个 permit或事务；
- delivery queue有界并返回 typed accepted/queued/backpressured/rejected/conflict disposition，receipt携带 canonical delivery record identity、revision和 disposition，禁止“never fail”、无限 parked queue或仅返回 runtime presence；
- completion notification、raw message、communication 和 broadcast/subtree delivery 复用同一 canonical delivery owner，每个 target 独立结算；
- waker 只是 best-effort signal，durable scan/reconcile 必须能在重启、lost wake、flush exception 后重新发现；
- target terminal、generation mismatch、retention expiry 和 poison payload 进入 typed terminal/dead-letter，不能静默 drop。

关闭条件：control-plane 重启与 lost wake 后可从 durable facts 恢复；相同 request/digest严格幂等，相同 identity不同语义得到 conflict；fan-out parent/child identity 可审计；accepted delivery 总量有明确上界、公平推进、reservation释放和 terminal settlement，且不会占用 resident/turn capacity 等待执行；所有成功、排队、背压、拒绝、冲突与 dead-letter 都有含 record identity/revision 的 typed receipt，不再用 `Optional[AgentRuntime]` 或“never fails”表达 delivery 保证。

### B24. BackgroundTask 查询、result retirement 与 metadata reap 必须由 owner-local cleanup state machine 分阶段结算

当前已存在且必须保留的治理事实：`BackgroundTaskPool`是per-Agent/process-local owner，`BackgroundTaskOwner`绑定process instance、Agent与incarnation；`LocalTaskReference`绑定TaskId与AttemptId；submit返回typed local `BackgroundTaskAcceptance`。Pool已有`ACTIVE -> DRAINING -> CLOSED`、与submit共用的lifecycle lock、submit前work pin、owner校验和typed drain receipt；`Role.prepare_for_eviction()`先关闭admission，有pin时拒绝卸载，Role cleanup以有界drain结算。因此本项不是建设第二个task registry、durable Workflow或新治理状态机，而是补齐该canonical pool内部尚未闭合的cleanup阶段。

剩余反证：`get_task_info()`和`list_tasks()`仍直接返回pool-owned mutable `TaskMeta`；调用者可以绕过owner修改status、attempt/result/notification状态。`mark_retrieved()`与resubmit对`_retire_result: Callable[[str], None]`失败按best-effort吞掉，但仍推进`retrieved`、`registered_resource`或metadata reap；result projection可能遗留，而canonical metadata已删除。`DiskTaskOutput.__init__()`还会仅凭模型可见`task_id`立即`truncate_file()`，发生在Agent/process/incarnation/Attempt ownership校验之外；同名旧attempt或并发构造可在新owner提交前清空已有输出。pool多个terminal/cleanup路径可直接`_release_pin()`，仍需机械证明operation、permit、output、terminal result/notification与resource retirement全部结算后才释放。

实施要求：

- query 只返回 frozen typed `TaskSnapshot`/`AttemptSettlement`，不暴露 `TaskMeta`、task、output store 或内部 collection；
- consume/retrieve/resubmit 使用 owner command 和 typed receipt，以 AgentId + incarnation/generation + TaskId + AttemptId 校验 active attempt；
- 将 `_retire_result: Callable[[str], None]` 替换为由 BackgroundTask cleanup consumer 推导的最小 typed retirement command Port；请求携带已绑定的 process/Agent/incarnation/generation/TaskId/AttemptId resource reference 与 expected attempt/generation，返回至少区分 RETIRED、ALREADY_RETIRED、STALE_ATTEMPT、OWNER_LOST 和 CLEANUP_FAILED 的 typed receipt，不接受裸 `task_id` 或以异常/`None` 表达全部结果；
- 使用 owner-local cleanup state machine而不是伪造跨资源原子事务：在 owner/generation gate 下原子进入 `DRAINING` 并禁止新 mutation；output、notification、Runtime resource retirement 分别形成 process-local typed settlement；外部 I/O/await 不持有 pool lock；每阶段返回 owner gate 后复核 generation/attempt再提交阶段结果；全部阶段完成后才提交 terminal cleanup fact并释放 pin；
- result/resource retirement 不得 best effort；任一步失败保持 DRAINING、pin、metadata和已完成阶段 settlement，由同 owner/generation幂等续跑，返回 CLEANUP_FAILED。不得为此引入跨进程 transaction、durable task registry或第二 task lifecycle；
- 保留现有BackgroundTaskOwner、LocalTaskReference、AttemptId、lifecycle lock、work pin、typed acceptance/drain receipt和Role eviction gate作为canonical实现；新增能力必须在这些owner/type上最小扩展，不得平行创建TaskManager、进程singleton pool、durable task store或让supervisor直接读取pool map；
- stale attempt 不能 retire 新 attempt 的 pointer，resubmit 不能在旧 projection 未结算时清空标志并覆盖真相；
- output location必须由pool owner以Agent/process/incarnation/generation/TaskId/AttemptId派生并以exclusive create/typed adoption建立；构造普通writer不得truncate既有文件，清空/替换只能在校验current attempt和pin后通过typed command执行；
- 架构门禁禁止 BackgroundTask service 返回 mutable internal metadata。

关闭条件：现有per-Agent canonical pool及Role eviction pin gate保持唯一；包外无法修改pool内部task truth；cleanup外部I/O不在pool lock下执行；stale attempt/generation不能retirement新attempt resource；任一阶段失败可在保持DRAINING/pin下由同owner幂等恢复，且不会提前reap metadata；release receipt能证明operation、permit、output、terminal fact、notification与resource retirement各阶段全部完成；不存在第二task registry或把BackgroundTask升级为durable Workflow的路径。

### B25. 保留现有 durable Workflow owner并闭合 reconciliation identity、effect 与查询面

当前已存在且必须复用的治理事实：`orchestration/workflows/durable/`已经拥有canonical `WorkflowRunProjection` closed phase、stable RunId/DefinitionId、strict terminal/provenance/access codec、revisioned `WorkflowRunStore`、create admission、caller authorization、checkpoint/pause/resume/cancel/terminal command owner及operation ownership fence。`product/workflows/durability.py::ProductWorkflowDurability`是唯一Product activation/scan/shutdown owner，执行projection以独立`workflow-execution:<RunId>` ownership运行，并明确不进入BackgroundTaskPool；Agent service通过durable create admission、definition catalog和typed WorkflowRunReference提交/取消/恢复。不得因本B项重建第二Workflow engine/store或把现有治理描述为未实现。

剩余反证集中在reconciliation而非run主状态机：`WorkflowReconciliationStore`虽检查envelope字段集合，却未验证`effects/deliveries`的list类型，重复`effect_id/delivery_id`会在dict投影时被后项静默覆盖，identity、payload、reason可为空且缺少交叉identity校验。`records()`每次解码新副本，并未泄漏store长期live map；真实缺口是query contract无类型且返回裸mutable mapping、collection discriminator使用字符串。`submit_effect()`仅以`run_id + logical_key`派生identity，`submit_terminal()`仅以`run_id + destination_id`派生identity；遇到既有record时不校验capability、command/outcome payload或generation，可能把冲突请求误报为幂等成功。Reconciler每项只claim固定30秒ownership lease，长耗时外部动作后可能才发现fence失效；lease/fence只能保护canonical commit，不能证明外部动作未发生。

实施要求：

- reconciliation envelope 与 effect/delivery record 使用 versioned strict codec；collection 必须是 list，identity 唯一且非空，所有 primitive、enum、counter、instant、payload 和跨字段关系 exact validate；
- 直接扩展现有WorkflowRunStore/Control、WorkflowReconciliationStore/Reconciler、operation ownership和ProductWorkflowDurability composition；保持run、reconciliation、inspection各自最小服务面，不新增同义DurableWorkflowManager、第二run store或BackgroundTask adapter；
- query 返回 frozen typed snapshot，不返回裸 mutable mapping；这是稳定 query contract整改，不得误报为调用者正在直接修改 durable internal state。commit/claim/replace 使用具体泛型/DTO、expected revision 和 typed conflict；
- 按D11让definition activation为每个effect选择NO_EXTERNAL_EFFECT、IDEMPOTENT_BY_KEY、RECONCILABLE_BY_RECEIPT或NON_REPLAYABLE；effect identity/preimage绑定capability、typed payload digest、provider endpoint/account/contract、permission/effect generation与run/definition/node/logical key。只有全部facts一致才幂等；任何差异返回EFFECT_IDENTITY_CONFLICT；
- 外部 effect 前先 durable intent/CLAIMED，claim 持有 monotonic fence并由 owner-owned refresh task在执行窗口续租；refresh task有显式activation、cancel、shutdown settlement。stale/expired owner不能向 canonical store提交 receipt、retry、IN_DOUBT或terminal state；续租只是降低失效概率，不是崩溃原子性；
- 为“外部动作已经发生但返回时 fence 已失效”设计证据移交与对账：在动作前绑定 provider idempotency key/查询 identity；独立需求必须明确 provider/process原始 receipt evidence 的 owner、durability、writer authority、current-owner读取方式、retention与清理条件，并证明它只是调查/对账证据而非第二 settlement truth。新 current owner通过 provider query/idempotency/evidence对账后提交 canonical receipt或 IN_DOUBT；无法证明结果时禁止盲重试，stale owner不得向 canonical store自行提交 receipt或 IN_DOUBT；
- retry严格按四类capability及D07 bounds执行；无法证明provider idempotency/query guarantee固定降级NON_REPLAYABLE。UNKNOWN/无receipt进入IN_DOUBT或OWNER_ACTION_REQUIRED，不能复用普通retry/dead-letter路径；
- stale receipt只进入typed append-only evidence inbox且不成为settlement truth；current fenced owner通过provider query/evidence与expected revision提交canonical settlement，evidence失败且动作可能发生时保持IN_DOUBT；
- terminal delivery outbox按destination调用canonical delivery Port，Workflow只保存stable receipt reference并结算outbox，不复制deliver/retry/ack/dead-letter状态机。

关闭条件：现有durable Workflow run owner、create admission、caller fence、Product composition与BackgroundTask分离保持唯一；损坏/重复record fail closed；四类capability逐handler有provider evidence，未知保证固定NON_REPLAYABLE；相同facts幂等、不同preimage conflict；stale owner无canonical结算权，动作后失fence由current owner对账且不盲重试；terminal outbox不复制delivery engine；query只返回frozen typed projection，无第二run/effect truth。

### B26. 架构门禁必须覆盖真实生产边界并消除假绿

当前事实：dynamic import检测漏alias，typed path覆盖不足，active-store artifact漏多项。另有大量架构测试读取源码后断言`LeaseCoordinator`、`assert_current`、特定调用文本或`.get(`等substring；它们不能证明owner唯一、调用顺序、fence在mutation前或decoder fail closed，注释/死代码/重命名可造成假绿假红。若active-store JSON与declaration列表继续手工维护，又会形成与production composition并行的第三份范围真相。

实施要求：

- 本项分两阶段：第一阶段立即修dynamic-import alias、Temporal/Squilla错误豁免、active-store漏项，并盘点/替换安全关键substring门禁；第二阶段各domain在设计完成的同一切片交付其门禁。B26不得预先替domain定义合法shape；
- 门禁按证明能力分级：import/declaration/call graph用AST与symbol resolution；durable CAS/fence顺序、strict decoder、state transition和production composition用可执行负向/竞争/corruption/activation测试；source token只用于明确删除compat symbol或禁止语法，并记录其不能证明runtime语义；
- 安全关键测试必须从真实production entrypoint/composition构造对象并触发stale fence、竞争、corruption和activation failure，不得只实例化孤立fake或搜索同名字符串；同一保证只保留一个authoritative gate，避免AST scanner、artifact和substring测试维护三份范围；
- AST 门禁解析 import alias/binding，识别 `importlib.import_module`、直接 `import_module`、alias、`__import__`、PEP 562 `__getattr__` 和其他批准 discovery primitive；
- 动态 discovery 豁免必须位于对应 owner 的局部门禁，并逐项包含精确 symbol、真实变化轴、manifest identity、原因、到期/退出条件和负向测试；固定仓内依赖不得豁免，禁止中央永久 allowlist；
- typed-boundary 门禁按语义覆盖全部 `contracts/ports/**`、跨层 DTO、public factory/service/query、durable codec/checkpoint/receipt、registry snapshot、Role/Tool/Workflow/BackgroundTask 边界，而不是维护易漏路径白名单；
- `Any/object/bare dict/getattr/hasattr/Callable[..., ...]/BaseModel` 门禁支持精确、可审计的结构化豁免，豁免只允许外部 adapter、严格 decoder、canonical JSON 或必要 private erasure，并验证动态值在 adapter 入口立即投影；
- 门禁拒绝无错误码 `# type: ignore`；当前 `runtime/fileops/document_adapters/{docx,pypdf_pdf,pdfminer_pdf,fitz_pdf,xlsx}.py` 的第三方 stub 缺口必须改为精确错误码并说明不可替代原因，或通过 adapter-local typed Protocol/stub 消除，不能用整条 import ignore 掩盖真实 API shape；
- durable-store inventory从唯一Product composition声明或每个store的显式activation recipe确定性导出，并与真实production entrypoint reachability交叉验证；artifact只是生成投影，不是手写真相。每个声明store必须确由production composition激活，每个可达durable write/restore authority恰好一个声明；test-only、migration-only、archive projection和process-local cache不得误列；
- 删除store时activation、restore、backup、retention和inventory projection同切片退出；“artifact生成成功”不能替代inventory与production对象图一致性；
- 当前漏项还包括 `runtime.ledger.RunJournal`、`runtime.service_gateway.LocalServiceCallJournal` 与 `runtime.events.backends.SQLiteSubscriptionStateStore`；后者虽出现在 restore-source classification，却未进入 `ACTIVE_STORE_DECLARATIONS`，source classification不能替代active authority inventory；
- gate artifact/status 不得通过执行生产源码 `exec`、宽松基线更新或自动接受当前命中生成；generator 与 committed artifact diff 必须可解释且 CI hard enforce。

关闭条件：Temporal/Squilla、alias import、关键substring假绿与active-store漏项均有先失败后通过fixture；安全语义由真实composition负向测试而非源码文本证明；每个domain切片证明其边界/store漏治理会失败；inventory由composition/activation导出并与entrypoint对象图双向一致，无手写范围、test/cache误列、重复gate、自动基线接受或永久allowlist。

### B27. 闭合 Agent capacity 与 turn acceptance 的跨进程治理和 retention

当前事实：`orchestration/agents/capacity.py::LogicalCapacityProjection`只使用进程内lock/atomic replace，`reserve()`允许空limits；`DurableTurnQueueStore.accept()`不要求admission fence且terminal history无界。另有更前置的identity断裂：`EventDrivenScheduler._stage_and_accept()`以`session_id + delivery_ids`拼接摘要生成request id，未绑定canonical payload、Agent/root/subtree、lifecycle/incarnation或scheduler config generation，也未在duplicate时核对immutable facts。`notify()`允许普通`Message`直接进入mailbox，但`TurnQueueIdentity`要求非空delivery_ids；直接输入drain后会因空tuple构造失败。delivery集合顺序、batch边界和同一delivery归属哪个turn仍可能由当前进程mailbox时序偶然决定。

实施要求：

- 按D23把`drain -> accept -> restore`替换为durable `PREPARE_ACCEPTANCE -> atomic BIND_TO_TURN -> COMMIT_ACCEPTANCE`；PREPARED占capacity但不可claim，只有commit后公开ACCEPTED并生成Mailbox/wake projection；
- reconciler以transaction id双向query：未bind且prepare lease过期才abort，已bind只能完成同request或ACCEPTANCE_IN_DOUBT，无prepare的binding进入OWNER_ACTION_REQUIRED且禁止重绑；
- terminal采用`EXECUTION_SETTLEMENT_PREPARED -> atomic delivery batch ack -> turn terminal commit`；可能执行但结果未知进入EXECUTION_IN_DOUBT，retry保持同TurnRequest/assignment且只有证明无不可重复结果时允许；

- logical capacity 由唯一 durable owner 管理，所有 reserve/settle 在跨进程 transaction lock 与 current lease/fence 下执行；stale owner 返回 typed STALE_FENCE/OWNER_LOST；
- reserve 必须至少包含一个 canonical scope，并原子校验 application/root/subtree/parent 全部限制；空 scope、重复 identity、scope 与 lineage request 不匹配 fail closed；
- capacity fact 写入、lineage request admission 与 budget reservation形成可恢复状态机；任一 crash point不会永久泄漏 counter或产生已提交 child identity但未占 capacity；
- 按已确认D18，定义stable `TurnRequestId`与versioned canonical preimage，绑定Agent/root/subtree identity、target lifecycle generation、有序canonical delivery tuple及payload/batch digest、scheduler/admission config generation和batch规则；所有业务输入必须先形成canonical durable delivery，不允许空tuple、DirectInputId或其他logical-input第二状态机；
- 删除生产裸`notify(Message)`；wake只唤醒已有durable fact且不携带payload。不得先从mailbox移除再提交delivery/stage/accept；相同request identity且全部facts一致才幂等，payload、target、generation、batch或config任一不同返回typed INTEGRITY_CONFLICT；
- 明确定义delivery集合canonical排序、batch边界、单delivery至多归属一个logical turn及重试保持规则；不得由mailbox arrival/drain时序改变identity。broadcast target delivery仍分别结算，不因批处理合并identity；
- mailbox projection只在durable accept成功或可恢复accept intent已提交后确认drain/ack；对accept failure、drain后进程崩溃、duplicate restore和partial batch分别定义恢复transition，保证不丢消息、不创建第二logical turn且未accepted item可重新发现；
- turn accept 绑定当前 admission/scheduler config generation、lease/fence 与 capacity decision；旧 owner不能 accept，capacity admission 必须发生在 durable ACCEPTED 前；
- terminal turn fact 保留审计/幂等所需最小 immutable tombstone；完整 payload 按 Product retention、delivery/effect/pin/legal-hold settlement 后由 fenced owner purge；
- compaction/purge 使用 expected store revision 和 generation，不改写 enqueue sequence、历史 acceptance identity或尚未结算 claim；
- 架构门禁把 logical capacity 与 turn queue登记为 canonical durable authorities，并验证跨进程 mutation 均要求 fence。

关闭条件：每种输入先有delivery并形成唯一TurnRequestId；全部prepare/bind/commit/execution/ack crash点可恢复，一个DeliveryId最多属于一个TurnRequest，公开ACCEPTED都有完整binding；projection丢失不丢输入，capacity含所有active/in-doubt状态，stale owner不能accept/claim/ack/terminal，retention不破坏幂等与reference closure。

### B28. Cron durable occurrence retention、唯一命令面与 reload truth 不得依赖 mtime

当前事实：Cron scheduler lock、schedule/occurrence codec、AbsoluteInstant 和 fenced durable dispatch总体严格，但 `CronTaskStore` 会永久保留所有 ACCEPTED/REJECTED/IN_DOUBT terminal occurrence，没有 retention/purge/compaction命令；单一 JSON snapshot随每次触发持续增长并在每次 mutation 全量读写。`CronScheduler._reload_durable()` 仅比较文件 mtime 决定是否读取 canonical revision；同一 mtime、mtime 回拨或 coarse filesystem timestamp 会永久漏过更新。注释宣称支持“external edit hot reload”，这允许绕过 `CronTaskStore` command lock、expected revision、schema admission和 scheduler fence直接修改 durable truth。与此同时，`CronService.store` 和 `CronService.scheduler` 公开具体可变实现，`product/entrypoints/cron/cli.py` 也直接构造 `CronTaskStore`，因此 Product/调用者可以绕过 `CronTaskCommands` 建立平行 mutation/control path。

实施要求：

- 按已确认D08，Cron root是当前Mote OS identity独占的trusted local authority；严格检查owner/mode/symlink/path/regular-file/schema/revision，但不承诺抵抗同OS identity、root或offline disk edit，不建设HMAC/remote provenance，也不得声称strict decoder证明文件必由command写入；
- 按D01构建单一transactional v3 envelope；新TaskId至少128-bit且不可复用，legacy 8-hex进入独立namespace并保留mapping provenance。DISPATCHING迁为IN_DOUBT，terminal以migration instant作为lower-bound，unknown/orphan/duplicate/invalid reference使全量migration fail closed；candidate必须在scheduler activation前atomic replace/read-back，production不保留v2 fallback；
- durable schedule 的生产 mutation 只能通过 canonical typed command/service；删除 external-edit hot reload 作为受支持控制面，不把 mtime变化视为合法通知或 mutation。CLI 必须调用 canonical command owner，不能直接写 store；
- 删除 `CronService` 对具体 store/scheduler 的公开暴露；CLI、Product composition 与其他消费者统一使用最小 typed create/list/remove/query/activation service，不能直接取得 store、lock、scheduler 或内部 collection；
- scheduler以canonical revision与有界周期reconcile推进；notification只可best effort降延迟，mtime最多是非authoritative诊断且不得决定是否跳过revision读取；
- terminal occurrence 按幂等窗口、审计、delivery/effect settlement、retention class和 legal hold 保留最小 tombstone；完整 payload由当前 fenced owner按 typed purge command清理；
- 按D02保留已知terminal完整payload 30天、occurrence/task tombstone 180天；IN_DOUBT/OWNER_ACTION_REQUIRED及未结算引用无自动TTL，TaskId永不重分配。按D03分别处理用户delete、one-shot、age expiry、operator disposition、TTL、legal hold与security clear，未知结果无force/retry-anyway；
- 按D07执行task/occurrence capacity、8次dispatch backoff、每tick 100/100与5秒、maintenance 500/100与5秒、1 MiB/64 KiB inline和64/256 MiB snapshot bounds；使用5-field/366天/IANA/EARLIEST_FOLD_SKIP_GAP/FIRE_ONCE/FORBID closed policy，Runtime/CLI/extension不得提高；
- purge/compaction 在 store lock、expected revision和 scheduler fence下原子执行，不删除 active/deferred/dispatching/IN_DOUBT证据；
- 将 schedule、occurrence 与 lease authority登记到 durable governance inventory，并对 restart、mtime不变/回拨、长期 recurring task和purge crash进行确定性 fake-clock测试。

关闭条件：v2→v3 migration保留事实且无双reader/writer、短identity ABA或DISPATCHING重投；Cron正确推进不依赖mtime；生产API/CLI/composition无external-edit或直接store mutation；包外不能取得具体store/scheduler；terminal/unknown/task deletion按D02/D03/D07有界结算且不损失幂等、审计、IN_DOUBT或恢复证据。

### B29. Workflow Product inspection 不得成为第二条 live 可变状态链

当前事实：canonical durable Workflow run/control/store、typed WorkflowRunReference和Product durability composition已经存在，且执行调度明确不使用BackgroundTaskPool；本项只针对其上方尚未收敛的Product live projection/continuation旁路。`WorkflowExecutionViews`仍保存live `WorkflowRun`与mutable `graph_meta`，`state_snapshot` setter可直接回写metadata；`ResumeTasks.call()`先逐字段`setattr`再验证/compile/resume，后续失败不会回滚Product cache。`AgentWorkflowService._workflow_run/_restore_run()`仍从process-local`graph_meta.graph_ref.build()`选择definition，懒写request uuid并在无durable checkpoint时把live state/run_state/from_nodes喂给definition。`GetNodeState`/`ResumeTasks`继续读取`graph_meta/state_snapshot`；这形成Product continuation truth，但不等于Workflow durable owner不存在。

实施要求：

- Workflow run phase、checkpoint、frontier、node settlement、retry 与 resumable state 只由 `orchestration/workflows/` 的 canonical durable owner 推进；Product inspection 只能投影不可变查询结果；
- 删除公开 live `WorkflowRunView`、`graph_meta`、`run_state` 和 `state_snapshot` setter；query 返回 frozen、版本化、typed `WorkflowInspectionSnapshot`，不得携带可执行 graph、coroutine、closure 或 owner-owned mutable record；
- Product先构造immutable typed resume/skip/retry command，不修改view/cache；override/from/skip、canonical digest、WorkflowRunReference、definition identity/digest、expected checkpoint revision和current execution fence全部进入command；
- Workflow owner在current fenced run下验证definition、checkpoint、frontier、override字段schema并原子提交新resume intent/attempt；validation/commit失败不改变canonical state或inspection projection。Product不把缓存Python state或live graph对象喂回引擎；
- 已存在run的definition只从Product批准且由Workflow durable owner绑定的definition catalog按identity/digest解析；禁止`graph_ref.build()`或process-local graph_meta选择continuation。request identity在首次create前产生并持久化，不在恢复helper上懒写uuid；inspection完全丢失后仍可从definition/run/checkpoint重建；
- `register/record_retry/discard` 不得作为平行状态 mutation API；若只管理 process-local presentation subscription，须使用独立 identity/lifecycle，并能完全从 durable run snapshot 重建；
- `RunSnapshot` 不再以裸 dict 临时拼装正式 state；checkpoint/frontier/terminal payload 使用 B3/B18 要求的 domain-owned strict DTO/受限 JSON value；
- 分离Product service和identity：process-local BackgroundTask query/cancel只接受Agent-bound TaskReference；durable Workflow inspect/resume只接受WorkflowRunReference。tool adapter可在模型输入边缘解析字符串，但必须解码成唯一domain identity并拒绝跨域混用；Workflow completion可投影到Agent destination，但BackgroundTask registry不得成为Workflow lookup/resume owner；
- 架构门禁禁止 Product Workflow service 返回 `WorkflowRun`、`GraphRunState`、graph metadata 或 mutable view，并验证所有恢复入口要求 durable revision/fence。

关闭条件：Product inspection/query及失败resume不改变任何live/canonical state；resume只从approved definition identity+durable run/checkpoint经fenced command原子产生attempt，无graph_ref.build、lazy uuid或cache continuation；inspection丢失仍可恢复；TaskReference与WorkflowRunReference/API不可混用，BackgroundTask registry不拥有Workflow run；inspection与canonical run无双状态推进。

### B30. Shared inference daemon discovery 必须使用严格版本协议并闭合 generation-safe cleanup

当前事实：`product/inference/daemon/supervisor.py::DaemonDiscovery` 是客户端定位共享 daemon、验证 PID incarnation、socket generation 与 RPC protocol 的跨进程安全记录，但 `read_discovery()` 直接执行 `json.loads(...)` 后 `DaemonDiscovery(**payload)`：没有 exact-shape decoder、字段 primitive/range/non-empty 校验，`state` 仍是裸字符串，Python 的 `bool`/`int` 相容性也未被排除。`discover_ready_socket()` 永久接受 `current protocol` 和 `current - 1` 两个版本，却没有 migration generation、退出条件或旧 decoder 删除机制。stale discovery/socket 只重命名为随机 `.stale-*` 文件，不登记 retention/清理 owner，反复 crash/reconcile 会无界累积；supervisor lock 释放与 daemon process terminal settlement 也没有统一 typed receipt。

实施要求：

- discovery 使用 Product-owned versioned strict codec；顶层和 payload exact shape，schema/protocol/PID/ticks 必须是非 bool 的有效整数，generation/boot id/path/state 非空且满足 canonical 格式，未知字段、状态、版本和错误 primitive fail closed；
- `DaemonState` 作为 authoritative enum 贯穿 record、publish 与 query，不在 validated record 中退化为 `str`；socket generation、文件名、进程 incarnation 与 record identity 做交叉校验；
- protocol 升级必须明确直接替换或一次性 migration；若确需滚动升级窗口，使用有期限、可观测的版本 negotiation contract，并在同一迁移完成后删除旧 decoder，禁止永久 `current - 1` 双读；
- STARTING/READY/DRAINING/STOPPED/CRASHED/RECONCILING transition 由持锁 generation owner 通过 typed command/receipt 推进；release supervisor lock 前必须明确 daemon 是否仍由新 owner合法接管，旧 generation 不得 publish/stop/delete 新 generation；
- 区分三类事实：current discovery record 是安全关键协调 truth；stale socket/path 是经 current owner复核后可删除的本地资源；corruption evidence是否短期 quarantine 由已确认的 Product observability/incident policy决定。不得仅因文件跨进程存在就建立与 Session/Workflow/effect 同级的业务 durable retention authority；
- stale socket/discovery 先以当前 owner/fence和 incarnation复核，再用 generation-safe typed cleanup有界收敛；随机改名不能永久堆积。只有存在真实 incident consumer、期限和删除 authority时才建立短期 quarantine，否则返回 typed corruption并按已确认本地清理策略处理；
- discovery write 保留 flush/fsync/replace/parent fsync，读取还须限制文件大小并区分 corruption、unsupported version、stale incarnation 和 unsafe ownership。

关闭条件：任意 malformed/unknown daemon discovery都不会进入连接或认证路径；旧协议有明确退出且不存在永久双读；stale generation不能覆盖或删除 current generation；stale local resource通过 current-owner复核的typed cleanup有界收敛。除非有已确认 incident consumer/policy，系统不新增 quarantine durable状态机、legal-hold语义或长期审计 authority。

### B31. LSP JSON-RPC adapter 必须严格投影为 typed code-intelligence result

当前事实：`contracts/ports/code_intelligence/code_map.py::CodeMapLspQueryPort` 声明 `list[dict[str, JsonValue]]`，但 `runtime/lsp/jsonrpc.py::request()`、`LspServerInstance`、`LspServerManager`、`LspService` 与 `runtime/agent/components/context.py` 均以裸 `dict/list` 传递 provider reply。JSON-RPC endpoint 没有对 envelope、id、error/result 互斥关系和 params/result 类型做严格校验；documentSymbol/definition/references 只检查最外层 list/dict，随后 code-map 用 `.get`、`int()`、`str()` 和异常吞并解析嵌套 Location/Symbol。malformed reply 因而可能被静默当作合法空结果、部分结果或触发 best-effort fallback，调用方无法区分“确实没有符号/引用”和“provider 协议损坏/超时/进程已死”。这会污染缓存并掩盖 code-intelligence 边界失效。

实施要求：

- JSON-RPC transport 只负责 framing，但必须严格验证 response envelope、版本、id correlation、`result`/`error` 互斥、错误对象和 frame/body 大小；malformed/unknown response 返回 typed protocol failure并终止或隔离该 endpoint；
- Product activation先声明支持的 LSP protocol version与 capability subset，并在 initialize/capability negotiation阶段拒绝无法安全投影的 provider，或选择实现该版本完整合法 union；不得在收到 response 后用单一 DTO 猜测 provider shape；
- LSP wire decoder按 negotiated capability处理封闭合法变体：document symbol至少区分 `DocumentSymbol[]` 与 `SymbolInformation[]`；definition/reference按所选协议支持 `Location`、`LocationLink`、相应数组与合法 `null`。每个 method 的允许 union、null/empty语义、协议版本与 server capability绑定，未知不在协商集合内的变体返回 typed INVALID_RESPONSE；
- 只激活D09的`lsp-3.17-code-map-v1` stdio profile及封闭method集合，禁止dynamic registration；initialize严格验证FULL sync、method capability和UTF-16 position encoding，不支持则typed `UNSUPPORTED_CAPABILITY`；URI只接受approved workspace root内canonical `file:`；
- JSON-RPC envelope exact验证2.0、id correlation、result/error互斥、header/Content-Length/UTF-8/top-level object；malformed frame关闭endpoint并结算pending。LSP wire可忽略标准合法扩展字段，但必需/已消费字段严格；canonical DocumentSymbol、SymbolInformation、Location、LocationLink、Position、Range及provider error DTO frozen/exact，禁止宽松强转；
- 完整支持`documentSymbol`两种array variant/null、`definition`的Location/Location array/LocationLink array/null、`references` Location array/null，并为recursive children设置depth/item bounds；diagnostic非法单项typed reject/计数，损坏envelope关闭endpoint；
- `CodeMapLspQueryPort`返回typed receipt，至少区分SUCCESS_EMPTY、SUCCESS_WITH_ITEMS、UNAVAILABLE、UNSUPPORTED_CAPABILITY、TIMEOUT、INVALID_RESPONSE、SERVER_ERROR和CANCELLED；只有两个SUCCESS状态可写code-map cache；
- Runtime 内部不继续传播第三方裸 dict/list；adapter 边缘完成投影后，manager/service/Role context 只传 immutable tuple/DTO；
- notification diagnostics 同样严格解析，单条非法 diagnostic 可以 typed reject/计数，但不能通过默认 severity/position 伪造有效诊断；transport failure 必须结算 pending request，reader task 的异常由 endpoint owner观察；
- LSP 仍是 advisory 能力，失败可以不阻断 Agent turn，但必须可观测且不能把协议失败承诺为成功空结果。

关闭条件：profile只暴露已确认method/transport/encoding；每个method有capability-bound union fixture，覆盖DocumentSymbol/SymbolInformation、Location/LocationLink、array/null/empty和合法扩展；未协商或malformed结果不进入cache/projection，Port边界无裸list/dict，合法空与各failure可区分，reader/进程关闭结算全部pending。新增method/encoding/transport/version必须新profile generation。

### B32. Sandbox 实际 enforcement posture 必须绑定 permission/effect 并 fail closed

当前事实：Sandbox启动期探测bwrap/cgroup/seccomp/netns并缓存mutable capability字段，之后`wrap_command/wrap_exec`长期依赖该snapshot。systemd manager、proxy/netns launcher、BPF/trust material可在实际spawn前失效，当前没有per-operation health/permit或实际launcher evidence，形成activation-to-spawn TOCTOU。各control的产品保证也不同，不能把seccomp/core-dump/filesystem/network/credential/resource全部统一为必需或统一允许degrade。

实施要求：

- Product按真实command/effect class选择有限、versioned enforcement profile；profile明确required/advisory controls及批准identity，不开放用户任意bool矩阵或未识别未来配置轴。Runtime只证明实际posture满足profile，不自行降级/扩权；
- 区分generation-level compiled plan/static host capability、operation-level spawn permit/即时health验证，以及process启动后的actual enforcement evidence；一次`start()`成功不能无限授权后续effect；
- permission/classification/approval绑定profile/plan identity；每次spawn前验证generation仍active、plan未变、required resource健康并取得typed permit。required不可证明则不spawn/重新审批；advisory failure进入receipt/telemetry但不伪装required guarantee；
- network `off`/受限模式必须由不可绕过的 netns/seccomp/等价 backend证明；仅修改 proxy env 不能声明强制网络隔离。credential interception 初始化失败时，要求 brokered credential 的 effect fail closed，不能无凭据直连后伪装同一策略；
- seccomp、cgroup、hardening与 trust-anchor 安装分别返回 typed activation disposition；安全相关异常不得只 warning/pass。若某项仅为 defence-in-depth，contract须明确其不属于批准保证且不得被上层误报为 active；
- spawn/process receipt绑定profile/plan identity、actual generation、launcher argv identity、namespace/netns/cgroup/process/credential evidence和即时health result；不得在动作后补写无法证明执行瞬间posture的activation receipt；
- 运行中required enforcement owner/resource意外失效时，按profile/effect语义终止、隔离或由ToolExecutor进入typed IN_DOUBT；Runtime不能自行改写成功。外部动作后receipt/artifact/audit/terminal commit失败同样由ToolExecutor authoritative effect owner结算；B14不拥有effect状态机；
- 架构测试覆盖 backend缺失、seccomp编译失败、cgroup不可用、netns launcher失败、MITM CA/trust bundle失败，验证动作在所需保证不满足时根本没有 spawn。

关闭条件：每次成功receipt证明spawn瞬间posture满足批准profile的required controls并记录advisory状态；start后资源失效、generation swap和launcher变化有TOCTOU负向fixture且不凭旧receipt spawn；运行中enforcement loss有typed终止/IN_DOUBT；profile集合有限且由真实Product用例驱动，无任意bool矩阵、Runtime降级或advisory冒充required。

### B33. 删除未接入的 inference admin surface，或经产品准入后另建严格 contract

当前事实：`AdminReadModel`/`AdminMutationModel`、HTTP adapter确有`Any`、动态`getattr`、宽松JSON和缺少generation CAS等问题；但全仓生产搜索中`build_inference_admin_api()`只命中定义/导出，`SharedDaemonApplication.admin_read_model()`也无消费者。当前激活入口是`SharedGrpcServer`，没有Product entrypoint、daemon lifecycle或server composition装配aiohttp admin surface。孤立projection测试不能证明生产consumer或activation。

实施要求：

- 实施开始前重新证明production consumer、route registration和activation/shutdown是否存在；若仍无已确认产品用例和consumer，删除`product/interfaces/inference_admin_api/`、`AdminReadModel`/`AdminMutationModel`、daemon projection、`admin_read_model()`、公开导出、孤立fixture及仅为其存在的依赖，不新增Port、wire schema、CAS command或durable state；
- 只有用户确认具体admin用例、调用方、监听/transport owner、认证authority、network exposure、activation/shutdown lifecycle及与现有gRPC surface边界后，才另立独立需求；名称相似不能成为合并admin与inference API的理由，不同authority/lifecycle应保持不同bounded context但必须有真实入口；
- 获准保留时，原严格目标继续作为新需求准入：typed query/mutation DTO/receipt、strict wire/authorizer、secret-safe projection、authoritative generation codec、expected revision/lease/fence CAS、typed HTTP mapping、删除`default=str`，并由唯一Product composition显式装配；
- 增加production composition/route lifecycle门禁，证明真实activation；单元测试直接构造model/API不能作为保留入口的证据。

关闭条件：默认路径下未接入admin surface、model/projection/export/fixture残渣为零，生产只保留已激活gRPC surface且未新增未来admin抽象。若产品明确保留，则本B项转为新独立需求并证明唯一route activation、strict contract、authorizer、generation CAS/fence、typed durable receipt与secret-safe projection；在产品决定前不得实施扩建。

### B34. RunJournal 必须按domain迁出并退役通用 StepRecord

当前事实：`runtime/ledger/append_ledger.py::AppendOnlyLedger` 声称 crash-durable，但只在构造时把JSONL折叠进进程内 `_latest`。`append()` 基于该snapshot校验transition后直接append并更新内存；没有跨进程lock、store revision、lease或fence。`reap()` 更会从进程内 `_latest` 生成全量candidate并 `atomic_write` 覆盖原文件。两个Role/恢复进程/Temporal activity并发持有同一 `RunJournal` 时，可基于相同旧snapshot分别追加相冲突transition，或由旧owner的reap覆盖并删除新owner刚提交的record。`next_think_seq/next_timer_seq` 同样从本地snapshot分配，可能重复identity。该 ledger 被 per-session Tool/Inference think、tool、timer step 与 Product Temporal Workflow effect plane共同消费，Temporal 还使用固定 session id `application-workflow-effects`。共同使用 JSONL/record API 不能证明它们共享 identity、bounded context、retention、effect reconciliation 或 lifecycle，也不能证明 Workflow effect truth 应由 Runtime session journal拥有。

实施要求：

- 按D11 Workflow separation先退出`application-workflow-effects` writer；Runtime侧不保留“per-session通用RunJournal”，而按D34分别建立Tool effect、ModelCall→Session projection和Session timer的canonical owner、strict tagged records、Port、retention及compaction；
- ToolExecutor是唯一Tool effect chokepoint，使用四类closed capability、统一ToolEffectId、动作前durable intent、typed receipt/ArtifactRef及fenced reconciliation；字符串`effect`、duck-typed resume和`ToolResult`字符串payload退出；
- ModelCallJournal是模型调用唯一truth。模型terminal与Session assistant message之间使用projection intent/ack；RunJournal think STARTED、reap或缺失assistant message均不能授权重新付费；
- Session timer使用独立identity、AbsoluteInstant deadline、resume generation、misfire/cancel/recovery；timer callback若承载Tool/Workflow effect必须提交对应canonical intent，timer owner不能重放任意callback；
- 按三个已确认D01对完整source做strict inventory，结合Session log、ModelCall、Tool invocation和Temporal facts生成三个inactive candidate及单一Session manifest。中段损坏、unknown/fork/conflict fail closed；三个target未同时可写前不切换，不进行逐kind mixed migration；
- 各domain采用D02/D03 retention与typed maintenance authority；generic `reap(keys)`、全量`records()`、`next_seq=max(all)`和Product `RunJournalConfig(enabled=False)`删除。Tool external effect记录不可关闭，纯Tool不进入effect store；
- 执行D07 payload、active identity、attempt、scan、stream、compaction和wall-time上界；domain writer明确single-process scope或使用CAS/fence，stale owner不得append/settle/compact；
- consumer迁移完成后删除`RunJournal`、`StepRecord`、`KIND_*`、万能record API、公共导出和普通恢复reader。旧source仅作为migration evidence保留到全deployment cutover加180天并满足hold/reference/unknown-effect退出条件；
- `AppendOnlyLedger`若继续存在，只能是owner私有存储mechanism，不能承载共享业务状态机。FileOps、Session event、ModelCall、service-call和Event journal不得借本切片合并或误删。

关闭条件：Workflow、Tool、ModelCall projection和Session timer各只有一个owner与状态真相；通用RunJournal/StepRecord无生产writer、reader、config、export或composition入口；模型恢复不会因think残余重付费，Tool unknown effect不会盲重试，timer不会重放任意callback；迁移对三个target原子激活且损坏/冲突fail closed；每个domain的CAS/fence、retention、capacity、Artifact edge、crash point和compaction均有确定性证据，旧source只在获准的180天退出门禁后删除。

### B35. Event subscription checkpoint 与 dead letter 必须绑定 fenced subscriber generation

当前事实：`SQLiteSubscriptionStateStore`可被多个进程打开，checkpoint没有subscriber generation/lease/fence；旧worker可提交更高sequence。Reliable handler失败时，`_quarantine_sync()`在同一SQLite事务写dead letter并推进checkpoint，worker内存ack/persisted sequence也越过poison event，使live subscription可继续前进。未来re-admission因此不能回退checkpoint或重新塞回原stream：这会重放其后的live events并违反checkpoint单调性。当前DLQ也没有独立replay identity/settlement/retention。

实施要求：

- recoverable subscription activation先取得stable subscription identity下的durable lease与monotonic fence；checkpoint/quarantine绑定subscriber generation、stream revision和fence；
- load/claim/process/checkpoint形成一条owner状态机，stale worker不能ack、checkpoint、quarantine或覆盖新ownerhealth；仅sequence单调不等于ownership正确；
- external-effect subscription还必须将effect receipt与checkpoint原子关联，handler返回不能单独证明副作用已结算；
- 按D19，dead letter只提供strict quarantine、typed query、retention/compaction/purge lifecycle，不开放re-admit/replay command，不回退checkpoint、不重新插入live stream、不调用旧handler closure，也不建立DLQ worker或第二delivery truth；
- external effect evidence绑定original event与DLQ settlement；未知结果进入typed IN_DOUBT，checkpoint已前进不能成为purge或重试依据。修复producer/consumer后产生的新event使用新identity；未来replay必须作为新产品能力重新准入；
- DLQ retention受session/legal hold/effect evidence约束并有界；payload过大走artifact reference，不能无限把完整envelope复制进SQLite；
- store登记完整schema generation、reader/writer、lease/fence、restore和retention，restart/双worker/fence takeover有确定性测试。

关闭条件：旧subscriber不能推进checkpoint；persisted sequence证明对应generation已处理或隔离event；生产面无re-admit/replay、checkpoint rollback或旧handler恢复入口；DLQ query/retention有界且不purge IN_DOUBT或未结算effect evidence。

### B36. Hosted service-call 必须迁入canonical v3 state并闭合远端effect对账

当前事实：`LocalServiceCallJournal`对单call已有flock、owner generation、strict stream transition与fsync，不能误判为完全无owner；但`.jsonl`、`.owner.json`、`.cancel`和pending扫描形成分散truth。`ContextVar`缓存、append序号和`.owner.json`不能证明current fence；caller可传execution semantics/idempotency，receipt含任意state dict，cancel marker旁路canonical lifecycle。Reconciler每轮glob全部call，terminal永久增长，远端submit/poll/cancel未知结果缺少统一IN_DOUBT与证据移交。

实施要求：

- 建立v3 canonical ServiceCallId/preimage与closed state：`PLANNED / INTENT_COMMITTED / CLAIMED / SUBMIT_STARTED / WAITING_REMOTE / RECONCILING / SETTLED_* / CANCEL_REQUESTED / CANCELLING / CANCELLED / CANCELLATION_IN_DOUBT / IN_DOUBT / OWNER_ACTION_REQUIRED`；append位置不能充当revision；
- Product frozen service binding选择`NO_EXTERNAL_EFFECT / IDEMPOTENT_BY_KEY / RECONCILABLE_BY_RECEIPT / NON_REPLAYABLE`，invocation不得自由提升semantics。CallId绑定Agent/incarnation、turn/ToolEffectId、definition/config/capability、payload digest、provider/account/endpoint contract与permission generation；同id不同preimage返回identity conflict；
- provider receipt与query outcome使用service-definition-owned tagged union/ArtifactRef；UNKNOWN、transport failure、caller deadline与provider FAILED/CANCELLED严格区分。Cancel作为独立typed effect进入canonical state，删除`.cancel`旁路；
- submit前durable intent；remote receipt先进入attempt evidence再转WAITING_REMOTE。所有append/poll/cancel/terminal/compact/purge验证current ownership fence；动作后失fence只写immutable evidence inbox，由current owner query/settle，不能盲重试；
- pending index是保存CallId/revision/state/next eligible/projection generation的可重建投影，不保存可独立修改的payload/receipt；canonical bounded scan可发现漏索引call，index失败不能回滚canonical intent或产生空pending假象；
- 按D01在Gateway/Reconciler启动前整体inventory `.jsonl/.owner.json/.cancel/index`，严格转换capability、receipt、cancel和owner evidence；open STARTED无receipt迁IN_DOUBT，未知receipt字段进入OWNER_ACTION_REQUIRED。单一active generation切换后v2只migration-only；
- 按D02/D03保留与authority：active/unknown call无普通TTL；terminal完整事实90天后compact为1年tombstone；v2 proof 180天。Submit只由ToolExecutor/批准consumer发起，Runtime current owner执行，owner action无retry-anyway，Tool/Session/Artifact GC不得删除call；
- 按D07执行root/deployment active 1,000/10,000默认与10,000/100,000 hard、payload1 MiB、receipt/response64 KiB、submit/poll/cancel次数与24小时观察窗口、scan500/page256/5秒、stream16/64 MiB soft/hard及store10/100 GiB。容量在intent前reservation，满时typed backpressure且不写accepted。

关闭条件：v3 store是唯一ServiceCall lifecycle truth，pending index仅为可重建投影；caller不能选择capability/idempotency或直接append/cancel；相同preimage幂等、冲突fail closed；remote submit/poll/cancel动作前后失fence均可对账且unknown不盲重试；terminal/active规模有界，canonical cursor scan可发现漏项；v2 journal/owner/cancel reader按180天门禁退出，stale owner和其他domain无purge权。

### B37. 删除无消费者 moderation 死代码及专用万能异常装饰器

当前事实：`OpenAIChat.amoderation()`使用`handle_exception`捕获所有异常、记录完整`args/kwargs`并返回默认`None`，存在敏感内容泄漏和failure伪装风险；但全仓无生产或测试调用，`handle_exception`也只被该方法使用，没有证据表明moderation是已交付产品能力。

实施要求：

- 重新搜索consumer后，若仍无已确认moderation用例，删除`amoderation()`、相关import/export、仅为它存在的`error_handling.py::handle_exception`及残留fixture/doc宣称；不得新增moderation Port、provider facade、policy状态机、feature flag或兼容stub；
- 删除前后不得记录或输出原始content、完整args/kwargs、headers、credential、client/config repr或provider body；确认历史/测试日志路径不因异常fixture继续泄漏；
- 只有用户确认具体consumer、调用时机、prompt/effect前后的authoritative gate位置和安全保证后，才另立typed moderation需求，定义request/result/error和ALLOWED/BLOCKED/UNAVAILABLE/INVALID_RESPONSE/DENIED disposition；timeout/error必须fail closed且不得以`None`表示；
- TTS/STT/image等已有真实consumer的provider边界另按其owner审计，不以删除moderation为由创建万能provider辅助operation抽象。

关闭条件：无消费者`amoderation`、专用万能decorator、export/fake/doc能力宣称残渣为零，敏感输入不会进入异常日志，且未新增无consumer moderation抽象。若产品确认该能力，则必须由独立需求和真实consumer证明typed fail-closed gate，不能按本删除条件直接签收。

## 5. 依赖DAG与准入波次

不得把B1–B37串成单一总序列。每张独立需求只登记真实前置contract/decision、共享写入面和下游consumer；不同owner的调查、adapter整改与测试可以并行，同一authoritative type/store schema/composition recipe同一时刻只有一个writer。

### 5.1 已确认的最小依赖边

```text
production entrypoint/store reachability artifact (B26)
  -> domain gate适用集合与active-store双向核验
owner/consumer/closure matrices (B5/B11/B18/B22)
  -> 对应symbol的stable implementation requirement与ASSIGNED owner/writer（索引自身不实施）

strict approved security config contract (B9)
  -> permission activation compiler (B12)
  -> Sandbox profile composition adapter (B32)
fixed dependency/import retirement evidence (B16)
  -> dynamic-import exemption removal gate (B26)
fixed-argv validation/env/disposition contract revision (B17)
  -> Sandbox process launcher integration (B32)

compiled tool binding authoritative type (B4)
  -> permission pipeline binding-declaration consumer (B12)
compiled binding generation identity (B4)
  -> snapshot restore codec (B14)
graph executable adapter contract (B3)
  -> ToolExecutor dispatch integration (B4)
activated Skill tool-selection projection (B6)
  -> Product tool-catalog publication adapter (B4)

Contracts scope declaration type (B8)
  -> machine-event scope migration (B8/B5 domain ticket)
  -> presentation projector (B7)
  -> ViewEvent wire codec (B7/B8)

durable schema/retention/purge deliverable
  -> 对应D-ID CONFIRMED、migration/authority evidence与唯一schema writer
```

除上述边及独立需求登记的共享contract revision外，不推断跨workstream依赖。例如OAuth(B21)、daemon(B30)、admin删除(B33)不因名称含inference而等待B2；Cron(B28)不依赖architecture ratchet完成。

### 5.2 四个准入波次

| Wave | 规划中的工作范围 | 禁止越过的边界 |
|---|---|---|
| Wave 0：事实与门禁 | B26已确认假绿、substring gate分级、production entrypoint/store reachability、B5/B11/B18 owner矩阵、B22 consumer/target seam矩阵、HEAD/diff证据刷新 | 不改变durable格式、产品语义或不可逆状态 |
| Wave 1：确定性删除与局部收敛 | 复核后删除B1/B33/B37死入口；删除B9单项registry/反射fallback、B16无consumer registry与固定伪catalog；每项独立签收 | 删除前检查稳定package export/`__all__`、user docs/example、stub、plugin contract、release/semver承诺。internal accidental export记录证据后直接删；真实public surface要求D21 CONFIRMED的breaking retirement，不留compat alias |
| Wave 2：不依赖默认产品语义的contract/owner修复 | B17 runner trust boundary、B22按domain迁移、B4 raw capability入口删除、局部类型/owner收敛；authoritative schema已明确且无旧数据/外部兼容承诺的adapter projection/非法输入拒绝；先失败fixture、inventory和decoder设计 | Wave不是协议变更授权。删除旧decoder、拒绝曾承诺variant、改变wire/durable version或接受集合必须有D01/对应protocol decision；不得预设retention、purge、profile或replay策略 |
| Wave 3：状态机与持久化治理 | Agent、Workflow、BackgroundTask、Artifact、Cron、Event subscription、RunJournal、service journal、OAuth、Sandbox/effect等按canonical authority实施 | 每个节点须有相关D-ID CONFIRMED、唯一writer、migration/forward recovery和fault fixture |

Wave之间不是全局barrier，但Wave名称本身不授予生产编码。不同domain的调查、决定提案和独立需求设计可以并行；任何生产切片只有满足第3.6节总准入后才能按独立需求级顺序实施。OPEN decision不阻断无关只读调查和Wave 0治理准备，但阻断其scope内生产修改。B10可先完成resource分类和明显误删风险分析，retention/authority未确认前不得修改purge状态机。B2、B4/B14、B6/B3只在上表共享identity/composition处登记真实contract依赖，不因Wave相同推断额外前置。

### 5.3 Wave 0 exit criteria

满足以下最小条件即可完成对应scope的Wave 0治理准备并编写Wave 1 requirement草案，不表示该生产需求已经获准编码，也不替代第3.6节总准入：

1. 新source baseline manifest已发布，具有persistent immutable artifact identity且可访问，并与引用它的governance evidence manifest分离；
2. versioned closure ledger schema/declaration已建立，原子subfinding identity已生成，active记录初始为OPEN且无自动接受；OPEN允许owner/writer为空，只有进入具体Wave实施前才必须推进为ASSIGNED并登记唯一owner/writer；
3. production-capable recipe catalog、四类scope set和五类签收集合的governance owner/生成算法已登记，即使部分domain内容仍待补齐；
4. B1/B33/B37/B9/B16候选删除分别完成consumer search与public-surface retirement审计，实际public symbol已有D21 scoped instance；
5. 首批独立需求拥有repository-local requirement ID、精确write set、唯一writer登记及所需scoped D-instance状态；
6. 已知B26假绿存在先失败fixture，修复不得通过改期望值或自动接受当前输出自证。

本节治理元数据闭合后，不再扩展B1–B37总索引；工作转入Wave 0 artifact、逐domain scoped decision提案和全部独立requirement设计。必须继续完成会阻断实施的产品决定、migration/retention/failure policy、owner、contract deliverable DAG、write-set冲突和独立需求级排序，直至首个及后续生产需求均无需实施者临场作架构决定。只有source baseline/`AGENTS.md`/产品目标发生实质变化，或具体schema草案暴露新的总索引级owner/状态冲突时，才重新开启总索引评审；domain问题回到其scoped decision和独立requirement处理。

### 5.4 第一批独立需求顺序草案

下列identity是待Wave 0 ledger正式创建的repository-local requirement ID草案；创建后固定identity，并分别登记reviewed revision、精确write set、唯一writer、consumer迁移清单和第3.6节全部准入证据。顺序表达contract/write-set依赖，不表示当前已经获准修改生产代码：

1. `R-W1-001`：删除B37 moderation与专用decorator，依赖`D05-openai-chat-moderation-v1`和`D21-provider-moderation-method-v1`；
2. `R-W1-002`：删除B33未激活inference admin surface，先机械证明shared gRPC是唯一production activation，依赖D17与对应D21 instance；
3. `R-W1-003`：删除B1 dead `LLMClient`，重新证明finalized inference Port覆盖全部真实consumer，依赖`D21-llm-client-port-v1`；
4. `R-W1-004`：收敛B9 cipher strict config，先证明Product strict config consumer和单实现边界，再删除Runtime反射default/单项registry；不得把D21确认扩大到未评审的cipher public面；
5. `R-W1-005`：收敛B16 i18n，构造immutable内置locale snapshot，迁移仓内测试/consumer/doc后删除public mutation和import副作用；
6. `R-W1-006-temporal`与`R-W1-006-squilla`：按各optional backend owner分别删除固定伪catalog/loader并改静态typed activation；二者不得共享writer需求或建立通用loader facade；
7. `R-W2-001`：实施B12 Hook activation freeze与Permission applicability compiler，D15/D14均已确认；必须先形成typed applicability/baseline decision contract，再由已确认D04 profiles声明各effect class的实际Sandbox enforcement；
8. `R-W3-EVENT-001`：修复B35 subscriber lease/fence和无replay DLQ lifecycle；D01 migration、D02 retention、D03 delete authority、D07 bounds与D19 no-replay均已确认，但仍须在独立需求中证明external-effect checkpoint settlement及canonical ArtifactRef/pin projection owner；
9. `R-W3-DAEMON-001`：实施B30 strict discovery、generation-safe local cleanup与D06 single-generation protocol cutover；D10/D07/D06均已确认，若触及daemon durable store格式必须另拆对应migration需求，不得夹带清库或双读。

第1–6项可以并行完成只读consumer/public/write-set调查，但只有Wave 0 authoritative manifests、decision/closure ledgers、stable requirement record和各自全部前置实际存在后才能逐项编码。`product/i18n`相关写入互斥；第7–9项虽已闭合列明的产品决定，仍须完成owner/复用、contract/schema revision、write-set和fault-injection独立需求准入。本批确认不构成B1/B9/B12/B16/B30/B33/B35/B37 finding整体关闭。

D06、D14、D04、D13、D16、D20以及D18、D08八个scoped instances均已确认；它们只解除各自列明的产品方向阻断，不自动满足Wave 0或独立需求准入。其他durable domain仍须各自实例化D01 migration、D02 retention、D03 delete authority和D07 bounds，不能复用Event/daemon local cleanup的数值或authority。后续评审按第3.3.7节直接确定可由当前事实推导的架构选择，仅对真实外部产品阻断保留OPEN。

### 5.5 LSP与Presentation独立需求顺序

下列ID是待Wave 0正式创建的稳定requirement草案，仍须逐项登记reviewed revision、write set、唯一writer和机械验收：

1. `R-W2-LSP-001`：建立Contracts typed LSP query/result/receipt与`lsp-3.17-code-map-v1` profile declaration，不同时修改code-map cache；
2. `R-W2-LSP-002`：实现strict JSON-RPC/LSP adapter、capability activation和typed service，依赖LSP-001及D13 fixed server runner；
3. `R-W2-LSP-003`：迁移code-map consumer/cache，删除裸dict/list和failure→成功空集合fallback，依赖LSP-001/002；
4. `R-W2-PRESENTATION-001`：移动canonical scope declaration并建立strict scope codec；涉及machine-event durable history的迁移另拆D01-scoped requirement；
5. `R-W2-PRESENTATION-002`：建立closed ViewEvent generation、catalog/disposition gate并迁移Product producer/consumer，依赖Presentation-001；
6. `R-W2-PRESENTATION-003`：分别收敛Structured、ACP和AG-UI adapters，删除`default=str`与unknown silent drop，依赖Presentation-002；三个surface按实际write set拆writer并互斥共享catalog/codec文件。

LSP与Presentation无真实contract依赖，可以并行；每个workstream内部按上述deliverable DAG推进，禁止先在末端consumer增加cast、临时fallback或第二wire DTO。

### 5.6 Workflow effect与RunJournal独立需求顺序

1. `R-W3-WORKFLOW-EFFECT-001`：冻结v3 EffectId、command/evidence/settlement、四类capability及D02/D03/D07 contracts；唯一最前置deliverable；
2. `R-W3-WORKFLOW-MIGRATION-001`：实现离线v2/RunJournal inventory、candidate converter和dry-run全量验证，只读且不激活v3 writer；
3. `R-W3-WORKFLOW-MIGRATION-002`：交付atomic cutover、receipt、source evidence及activation barrier，与MIGRATION-001使用同一schema writer串行评审；
4. `R-W3-WORKFLOW-EFFECT-003`：启用v3 canonical store/reconciler、fenced attempts、retention和owner-action commands，依赖前三项；
5. `R-W3-WORKFLOW-TEMPORAL-001`：移除Temporal RunJournal并接入v3 command/evidence，依赖EFFECT-003；
6. `R-W3-WORKFLOW-DELIVERY-001`：terminal outbox接入destination delivery Port；可与Temporal并行，共享schema由同一writer协调；
7. Runtime侧不再实施`R-W3-RUNJOURNAL-001`通用journal加固；按5.10三个domain拆分DAG执行，Workflow writer退出是其migration inventory前置；
8. 全deployment migration与evidence窗口满足后，另行删除migration-only decoder/source，不夹在功能cutover中。

Migration必须先于v3 writer activation；provider inventory和动作前后fault fixtures是准入证据，不能重新选择旧数据或unknown effect处置。

### 5.7 Cron v3独立需求顺序

1. `R-W3-CRON-001`：定义v3 typed TaskId/generation、occurrence/disposition/tombstone、strict codec、policy及migration receipt contract；唯一首节点；
2. `R-W3-CRON-002`：实现并fault-injection验证v2→v3 forward-only migration，依赖001且先于v3 writer activation；
3. `R-W3-CRON-003`：收敛command/query owner、typed deletion/operator commands及retention maintenance，依赖001/002；
4. `R-W3-CRON-004`：迁移scheduler到revision-driven reconcile、bounded claim和v3 fenced transition，删除mtime hot reload及包外store/scheduler访问，依赖003；
5. `R-W3-CRON-DELIVERY-001`：Cron trigger接入Agent canonical delivery Port并保存stable receipt，依赖004与Agent delivery contract；
6. `R-W3-CRON-ARTIFACT-001`：接入大payload/evidence ArtifactRef ownership edge；依赖Artifact canonical edge，未交付前保持inline fail closed；
7. 最后按deployment migration evidence门禁删除v2 decoder/source、8位identity构造和mtime控制测试。

002后003与Artifact contract准备可并行；delivery不与migration强制串行，但不得在Agent delivery contract前激活。禁止把七项重新合成B28巨型改动。

### 5.8 Agent ingress v2独立需求顺序

1. `R-W3-AGENT-INGRESS-001`：冻结delivery v2、turn v2、acceptance/assignment/settlement transaction、projection cursor和strict codec contract；唯一首节点；
2. `R-W3-AGENT-INGRESS-MIGRATION-001`：只读inventory三个v1来源，生成cross-store dry-run/conflict report，依赖001；
3. `R-W3-AGENT-INGRESS-MIGRATION-002`：实现inactive candidates、generation manifest及forward-only cutover，依赖前两项且先于新writer；
4. `R-W3-AGENT-DELIVERY-001`：实现delivery command/query、atomic batch bind/ack、retention及owner-action，依赖cutover contract；
5. `R-W3-AGENT-TURN-001`：实现PREPARED/ACCEPTED/settlement-prepared、capacity reservation、claim/retry/cancel，依赖001/003；可与delivery内部实现并行，但共同protocol由001唯一writer控制；
6. `R-W3-AGENT-INGRESS-RECONCILE-001`：实现跨owner acceptance/settlement reconciler及全crash-point fault injection，依赖004/005；
7. `R-W3-AGENT-PROJECTION-001`：将Mailbox、Residency mailbox snapshot和PendingDeliveryQueue降级为有界可重建projection并迁移入口，依赖006；
8. `R-W3-AGENT-INGRESS-SURFACES-001`：迁移Product、Cron、Workflow terminal和Agent communication到canonical delivery Port，依赖004/006；各surface adapter按write set分单；
9. 全deployment cutover及evidence窗口满足后，删除v1 decoder、旧mailbox payload snapshot、裸enqueue/notify和migration source，不夹入首次activation。

Migration先于writer；delivery与turn共享protocol但不共享store owner；reconciler完成后才能退出projection正确性路径。`R-W3-CRON-DELIVERY-001`和`R-W3-WORKFLOW-DELIVERY-001`依赖本组004/006，不再依赖抽象的未来消息系统。

### 5.9 OAuth credential v2独立需求顺序

1. `R-W3-OAUTH-001`：冻结CredentialSubjectId、backend binding、metadata closed state、SecretRef、generation-bound borrow、strict provider projection及D02/D03/D07 policy contract；这是唯一首节点，不能同时实现backend或consumer；
2. `R-W3-OAUTH-MIGRATION-001`：只读inventory selector/file/keyring/config/vault，生成逐subject source classification、conflict及dry-run报告，依赖001且不得选择或删除source；
3. `R-W3-OAUTH-MIGRATION-002`：实现inactive vault candidate、metadata candidate、read-back digest、cutover manifest与atomic activation barrier，依赖前两项；partial/mixed generation fail closed，先于v2 writer activation；
4. `R-W3-OAUTH-STORE-001`：实现metadata唯一owner、跨进程CAS/fence、vault inactive→published generation、borrow/pin/retirement与strict codec，依赖001/003；不把keyring包装成伪CAS，也不保留runtime fallback；
5. `R-W3-OAUTH-EFFECT-001`：实现login/refresh/revoke durable intent、attempt-scoped secret evidence、NON_REPLAYABLE reconciliation及`IN_DOUBT`，依赖STORE-001；fault fixture覆盖网络前失权、远端动作后失权、本地publish失败和stale response；
6. `R-W3-OAUTH-COMMAND-001`：实现logout、config retirement、backend migration、conflict owner action、TTL、hold、security clear的typed authority/receipt及retention maintenance，依赖STORE-001/EFFECT-001；SecretStore只执行授权erasure，不修改OAuth metadata；
7. `R-W3-OAUTH-CONSUMER-001`：迁移MCP、LLM及health consumer到明确provider/account/scope/consumer binding的generation-bound borrow/query Port，删除直接token、provider-owned refresh和default credential入口，依赖STORE-001/EFFECT-001；不同consumer adapter按write set拆分；
8. `R-W3-OAUTH-RETIRE-001`：在全deployment v2 cutover、180天coordination proof及secure-erasure receipt满足后，删除v1 codec、plaintext source、selector/fallback、`commit(None)`和migration-only reader；不得与首次activation合并，也不得在证据窗口前物理清除未知状态。

Migration inventory先于cutover，cutover先于v2 writer；store与effect共享credential identity但只有001能修改contract。Consumer迁移不能反向扩大borrow权限，retirement不能作为处理backend conflict、`MATERIAL_LOST`或`IN_DOUBT`的捷径。

### 5.10 RunJournal domain split与退役独立需求顺序

1. `R-W3-RUN-DOMAINS-001`：冻结Tool effect、ModelCall→Session projection、Session timer的typed contract、identity、Port、retention/bounds policy及RunJournal source inventory schema；唯一首节点，不实现通用journal v2；
2. `R-W3-TOOL-EFFECT-001`：实现ToolExecutor effect owner、四类capability、动作前intent、fenced reconciliation、typed receipt/ArtifactRef及D02/D07 maintenance，依赖001；
3. `R-W3-MODEL-PROJECTION-001`：接通ModelCall terminal到Session projection intent/message/ack，删除InferenceJournal/think第二truth和恢复重付费路径，依赖001及现有ModelCallJournal；
4. `R-W3-SESSION-TIMER-001`：实现Session timer identity、deadline/misfire/cancel/recovery、fence与有界maintenance，依赖001；不得承载任意callback或其他domain effect；
5. `R-W3-RUNJOURNAL-MIGRATION-001`：严格inventory完整RunJournal、Session log、ModelCall、Tool invocation与Temporal来源，生成三类cross-domain dry-run/conflict report；依赖001–004且不写active target；
6. `R-W3-RUNJOURNAL-MIGRATION-002`：构造三个inactive candidates、strict read-back及单一Session migration manifest并forward-only activation，依赖005；任一target冲突或损坏阻断全部切换；
7. `R-W3-RUNJOURNAL-CONSUMERS-001`：迁移ToolExecutor、inference projection、sleep/timer及JSONL adapter consumer，删除RunJournal config、StepRecord、kind/status字符串API、全量scan/reap和公共导出，依赖006；
8. `R-W3-TEMPORAL-RUNJOURNAL-RETIRE-001`：按Workflow effect workstream删除TemporalBackend RunJournal及application-wide writer；contract冻结后可与002–004并行，但必须在005最终inventory签名前完成；
9. `R-W3-RUNJOURNAL-RETIRE-001`：全deployment cutover、180天proof、unknown effect/Artifact/reference/hold均结算后，删除旧source、migration decoder、`runtime/ledger/run_journal.py`及仅服务它的测试/导出，依赖007和retirement evidence。

三个target owner可以在001后并行，inventory必须等待其contract与writer可验证，consumer只能在单manifest cutover后迁移。Hosted service-call、FileOps、Session event、ModelCall和Event journal不依赖本DAG，不能因通用RunJournal退场而合并或删除。

### 5.11 Workflow与BackgroundTask现有治理的核验优先顺序

本节不新增状态机，只约束B24/B25/B29及相关独立需求必须先复用当前源码已经存在的canonical治理。当前dirty worktree变化已使早期扫描基线部分过时；正式ledger创建时必须重新冻结source baseline，并把“已存在且通过”“已存在但缺证据”“仍有反证”分别登记，禁止把历史finding描述直接当实施清单。

1. `R-W0-BGTASK-GOVERNANCE-VERIFY-001`：从Product/Role composition反向证明每Agent恰有一个BackgroundTaskPool，记录BackgroundTaskOwner、LocalTaskReference、AttemptId、lifecycle lock、work pin、typed acceptance/drain receipt及eviction gate的真实consumer和负向fixture；只读核验，不创建新Port/store；
2. `R-W2-BGTASK-QUERY-001`：将mutable TaskMeta query替换为frozen typed snapshot/attempt settlement并迁移全部consumer，依赖前项；不修改task execution状态机；
3. `R-W2-BGTASK-CLEANUP-001`：在现有pool owner/generation gate内实现typed result retirement与分阶段cleanup settlement，修复裸callback、truncate-before-ownership和pin提前释放，依赖前两项；
4. `R-W2-BGTASK-GOVERNANCE-INTEGRATION-001`：验证Role eviction/release、supervisor admission/cancellation和Artifact/result projection只通过最小typed Port协作，无共享task registry或Workflow混入；依赖003；
5. `R-W0-WORKFLOW-GOVERNANCE-VERIFY-001`：从ProductWorkflowDurability与AgentWorkflowService反向枚举run store/control、definition catalog、create admission、caller authorization、execution ownership、reconciler、governance cancellation及terminal delivery；为每项登记composition reachability、唯一writer和已有fixture，禁止新建第二durable Workflow plane；
6. `R-W3-WORKFLOW-RECONCILIATION-001`：仅收敛现有reconciliation codec、identity preimage、四类effect capability、stale evidence handoff、retention/bounds与typed query，依赖005及第5.6节Workflow migration decisions；
7. `R-W3-WORKFLOW-INSPECTION-001`：把Product live WorkflowRun/graph_meta/state setter改为immutable typed inspection与fenced resume command，删除process-local continuation truth，依赖005/006；
8. `R-W3-WORKFLOW-GOVERNANCE-INTEGRATION-001`：以真实Product composition验证restart/resume、两个owner竞争、动作前后失fence、cancel、terminal delivery与shutdown，并机械证明Workflow执行不进入BackgroundTaskPool；依赖006/007。

BackgroundTask的VERIFY-001与Workflow的VERIFY-001可并行；后续切片只能修改各自证据确认的剩余缺口。若核验发现已有实现满足关闭条件，应直接登记证据并缩减write set，不能为满足旧计划而重写；若发现现有contract未由production composition激活，则先报告reachability冲突，不以新增第二入口修复。

### 5.12 Hosted service-call v3独立需求顺序

1. `R-W3-SERVICE-CALL-001`：冻结v3 CallId/preimage、binding capability、closed lifecycle、command/evidence/receipt、owner fence、pending projection与strict codec；唯一首节点；
2. `R-W3-SERVICE-CALL-MIGRATION-001`：只读inventory v2 JSONL/owner/cancel/index，完成capability downgrade与conflict报告，不调用远端；依赖001；
3. `R-W3-SERVICE-CALL-MIGRATION-002`：构造v3 inactive candidates、projection rebuild与store generation manifest并forward cutover，依赖前两项且先于writer；
4. `R-W3-SERVICE-CALL-STORE-001`：实现canonical command/query、revision/CAS、fenced claim、cancel facts、retention及Artifact edges，依赖003；
5. `R-W3-SERVICE-CALL-EXECUTION-001`：实现capability-controlled submit/poll/cancel、attempt evidence、stale-owner handoff与IN_DOUBT settlement，依赖004；
6. `R-W3-SERVICE-CALL-RECONCILE-001`：实现bounded canonical cursor scan、可重建pending index、owner action、compaction及fault injection，依赖004/005；
7. `R-W3-SERVICE-CALL-CONSUMERS-001`：迁移ToolExecutor/Product consumer，删除caller semantics/idempotency提升、public append/claim/records与cancel旁路，依赖005/006；
8. `R-W3-SERVICE-CALL-RETIRE-001`：全deployment cutover加180天proof后删除v2 decoder、owner/cancel文件、旧index/source与migration reader，依赖007及retirement evidence。

ServiceCall依赖第5.10节ToolEffectId edge和第5.13节Artifact edge declaration，但不等待RunJournal或最终GC完成即可冻结contract/store。Migration先于writer，consumer最后只获得capability-scoped service。

### 5.13 Artifact ownership与Session deletion独立需求顺序

1. `R-W3-ARTIFACT-EDGE-001`：冻结v2 Artifact/typed Edge/Hold/DeletionCommand/Claim/Receipt、generation、strict codec与producer completeness manifest；唯一首节点；
2. `R-W3-ARTIFACT-MIGRATION-001`：只读联合inventory SQLite/CAS/Session/FileOps/outbox/root/pin、conflict与orphan，不执行GC；依赖001；
3. `R-W3-ARTIFACT-MIGRATION-002`：构造v2 inactive store、edges/holds/tombstones/orphan evidence及manifest并forward cutover，依赖前两项；
4. `R-W3-ARTIFACT-STORE-001`：在现有Artifact store实现edge command/query、revision/CAS、hold、retention和bounded index，依赖003；
5. `R-W3-ARTIFACT-DELETION-001`：实现fenced claim与references→metadata→blob→directory分阶段删除、IN_DOUBT恢复和tombstone，依赖004；
6. `R-W3-SESSION-DELETION-001`：实现Session lifecycle fence、typed delete intent、自有edge逐项release与settlement，依赖004/005；
7. `R-W3-ARTIFACT-GC-001`：实现generation-complete closure、bounded cursor reconcile、capacity/backpressure和竞争/crash fixture，依赖004/005；
8. `R-W3-ARTIFACT-CONSUMERS-001`：迁移Workflow、BackgroundTask、Tool、Model、ServiceCall、delivery、FileOps及publication edge producer与transient pin projection，依赖004，并在GC activation前完成；
9. `R-W3-WORKSPACE-CLEANUP-RETIRE-001`：迁移Product maintenance/test cleanup并删除mtime/stamp/参数式hold、直接remove_tree/reclaim和v1生产reader，依赖006/007/008；旧source删除仍受180天proof约束。

Store完成后deletion、Session lifecycle和producer迁移可按write set并行，但GC activation必须等待全部producer completeness。各domain只声明/释放自己的edge，不因共享ArtifactId而合并状态机或把Artifact owner变成万能业务manager。

### 5.14 Session rollout v2独立需求顺序

1. `R-W3-SESSION-STREAM-001`：冻结v2 envelope、stream/lifecycle generation、closed SessionEvent codec、Artifact binding、append/replay/retention policy及typed errors；唯一首节点；
2. `R-W3-SESSION-MIGRATION-001`：只读inventory v1 rollout、directory identity、run lease、checkpoint/projection metadata与Artifact roots，严格验证checksum/sequence/meta/torn boundary并生成conflict报告；依赖001及`R-W3-ARTIFACT-EDGE-001`；
3. `R-W3-SESSION-MIGRATION-002`：构造v2 inactive stream、Artifact extraction/edges、deterministic projection digest与manifest/CAS cutover，依赖前两项及Artifact store cutover；blocked Session保持v1只读；
4. `R-W3-SESSION-STORE-001`：实现v2 append CAS/fence、semantic bounds、typed query、bounded replay cursor与lifecycle command，依赖003；
5. `R-W3-SESSION-RETENTION-001`：实现terminal eligibility、compact/delete stages、tombstone、hold/security/user/TTL authority和fault recovery，依赖004及Artifact deletion contract；
6. `R-W3-SESSION-PROJECTIONS-001`：迁移Runtime checkpoint、FileOps、machine-event projection、ACP/AG-UI connection resume与Notebook restore，只从verified v2 stream和Artifact edge重建，依赖004；不得持久化ViewEvent、widget tree、wire stream或ipynb第二truth；
7. `R-W3-SESSION-LEGACY-RETIRE-001`：删除v1生产reader/writer、目录/mtime cleanup及Presentation/Notebook旁路恢复，依赖005/006；v1 source物理退出仍受180天proof与blocked/hold evidence约束。

Session STREAM-001与Artifact EDGE-001共同冻结后才能迁移；Presentation类型/wire工作可独立推进但不能建立durable store，Notebook stdin继续按D20推进而restore consumer等待v2 verified projection。所有durable scope必须在ledger中标记APPLICABLE、ALREADY_CLOSED、SCOPED_BY_DOMAIN或NOT_APPLICABLE，禁止留给实施者临场决定。

## 6. 最终全仓验收门禁

只有所有独立实施需求已各自关闭，且被保留、删除或迁移语义实际依赖的scoped decision instance已`CONFIRMED`，或由`SUPERSEDED`指向有效替代并落入authoritative contract后，才进行最终全仓签收。已确定删除且无生产能力的路径仍须完成适用的consumer/public-surface/retirement决定；确实不适用的decision family不创建scoped instance，而不能创建后保持`OPEN`来规避准入。删除签收还必须证明代码、入口、schema和产品承诺残留为零。必须满足：

签收适用范围先区分：`production-capable recipe set`覆盖所有受支持entrypoint及Product schema批准的optional activation branch；`activated instance set`只表示某deployment/config generation实际启用owner；`retired/unreachable set`是源码存在但无批准recipe可达、应删除或typed分类为migration/test-only的对象；`external public declaration set`覆盖即使不由本进程activation、仍可能被仓外import的正式contract。治理门禁遍历production-capable recipes，不执行配置笛卡尔积，但每个独立变化轴至少覆盖disabled、每个合法实现及activation failure；运行期health/receipt只针对具体activated generation。Public集合结合package export/public classification与D21，不能只靠runtime reachability。

在此基础上生成五类集合：`active durable authority set`由explicit store/activation recipe和writer/restore反向扫描生成；`active lease/CAS mutation authority set`由mutation service declaration与并发fixture生成；`public Port/factory/registry/callback/checkpoint/codec set`由authoritative export/public evidence生成；`production entrypoint/composition recipe set`由Product entrypoint catalog/构造链生成；`approved dynamic boundary set`由manifest/catalog与AST dynamic-import差集生成。每类由对应governance owner负责，禁止中央万能“架构对象发现器”；最终报告只交叉一致性检查。任一发现器失败/范围不完整均fail closed。

集合与production-capable对象图必须双向一致；test fake、archive reader、migration-only tool、process-local cache使用typed classification排除，不能口头豁免。以下“所有/每个”门禁分别遍历其authoritative集合，集合漏项本身即签收失败。

1. 五层 import 方向仍为 `contracts <- kernel <- runtime <- orchestration <- product`，逆向依赖为零；
2. 生产函数/方法/类体中的局部 import 为零；
3. 正式 Port、factory、registry、callback、handle、checkpoint 和 durable codec 中，已知类型不含无界 `Any/object`、裸 dict 或 `Callable[...]` ellipsis；
4. 所有保留的动态边界均有 owner、原因、严格投影位置和负向测试；
5. 所有 durable decoder 对 unknown version/tag、额外字段、错误 primitive 和 identity mismatch fail closed；
6. 类型 fixture覆盖现有 finalized inference request/result与Tool capability的端到端关系；Graph output fixture覆盖动态 `JsonValue` declaration、contract identity/schema fingerprint、严格validator、commit/resume和ToolResult投影，不要求不存在消费者的静态泛型；
7. capability、permission、effect、approval、audit 和 cleanup 使用同一 definition/generation identity；
8. 没有新增兼容 alias、re-export、双 API、平行 catalog、第二 output engine 或第二 durable event decoder；
9. `ztest/architecture/` 全部架构门禁通过，不得仅运行与本次修改直接相关的几个测试文件；
10. 全量 Pyright 通过；若仓库因第三方 stub 或平台矩阵无法一次执行，必须提交可审计的文件/包覆盖清单、每批命令与结果，证明生产模块没有空白区；
11. 每个受影响 bounded context 的完整测试通过，且 Product composition/activation 至少有可信 blueprint 构造、activate、shutdown 的 smoke evidence；
12. 每个 durable authority 有 corruption、restart、partial write、revision CAS、lease/fencing、stale owner 和 migration/拒绝路径的确定性证据；
13. 最终运行全仓测试。若 WSL 资源限制会导致环境崩溃，必须记录具体环境阻断、未执行范围、分批等价命令与结果，并在可承载环境完成最终运行后才能形成无保留签收；不得把“WSL 不宜运行”写成永久豁免；
14. 搜索命中不能直接作为关闭证据：每个剩余 `Any/getattr/hasattr/BaseModel/object/Callable/dict` 必须归类为外部 adapter、严格 decoder、canonical JSON、框架内部必要 erasure、private implementation 或已批准动态 extension boundary；豁免满足 B18 的局部精确规则。
15. 每个lease/CAS authority有两个owner竞争与takeover fixture，覆盖旧owner在外部动作前、动作后分别失效；stale owner不能提交，动作后结果按provider evidence对账为receipt/IN_DOUBT；
16. 按authority提供确定性crash/corruption/规模演练：mailbox drain、turn accept、delivery ack各crash point；Workflow/OAuth/RunJournal的partial write、torn tail、mixed backup generation；terminal record达到声明阈值后的storage bound、scan latency和compaction并发。阈值与通过标准来自CONFIRMED D-ID/Product schema，不以开发机偶然耗时签收；
17. Sandbox required control覆盖activation后、spawn前、运行中三阶段失效；Product entrypoint重启只恢复已批准definition/config identity。最终再选少量跨owner链路演练composition、takeover、effect reconciliation和shutdown，不要求一次性全系统压测，但任何durable authority不得只凭单元happy path签收。
18. Closure ledger中每条未obsolete原子finding均为`VERIFIED`，绑定stable requirement ID、获准的reviewed requirement revision、有效scoped decision instance、immutable integrated source identity以及仍有效的normative/irreplaceable evidence；批准authority的verification declaration与架构门禁机械验证缺一不可。五类适用集合分别由各自authoritative discovery method生成，最终报告只执行跨集合完整性和production-capable对象图双向一致性验证。

## 7. 非目标

- 不机械替换所有 Pydantic `BaseModel`；普通 DTO 可继续使用适合其语义的模型基类。
- 不把所有事件合成全仓巨型 union。
- 不建立万能 `Services/Manager/Context` 解决类型问题。
- 不将第三方 SDK 类型提升为 Contracts canonical type。
- 不为迁移保留旧 API 或双 codec。
- 本索引不直接实施任何生产状态机或目录重构。B24、B25、B27、B28、B29已确认涉及BackgroundTask、Workflow、Agent turn和Cron状态语义，必须进入各自canonical workstream并继续按authority拆单；审计视图F编号不构成实施归属。
- 不因同属本索引而把不同 canonical owner、durable authority 或 lifecycle 合并成一次跨域重构。

## 8. 当前审计基线与可复现性

- 历史审计基点：Git `HEAD`为`0911b7d10c13a5e69f6f3e2bf6261554e7d05c13`，旧unstaged tracked diff摘要为`992dfc6a2fa340cc9e01f35a40572824441dd4f3`。该摘要不覆盖index/staged diff、untracked内容、mode/symlink/submodule等，**不能单独复现审计源码，也不得继续作为实施准入identity**。
- Wave 0可在`/tmp`生成artifact，但最终必须发布两个单向依赖的治理对象。`source baseline manifest`覆盖根`AGENTS.md`、生产源码、测试和普通需求/设计文档，冻结被审计源码事实；它不得包含任何反向引用该baseline identity或digest的closure ledger、generated scan result、verification report或governance evidence manifest，避免摘要自引用。排除规则必须由manifest schema按精确canonical path或明确的schema-owned artifact class声明，不得笼统排除整个`zdocs/architecture/`或其他目录。`governance evidence manifest`记录closure ledger/schema、扫描结果和verification report的identity/digest，并单向引用已冻结的source baseline identity；source baseline不得反向包含它。
- source baseline每个纳入项记录canonical relative path、entry type、mode、size、content SHA-256，symlink记录target，submodule记录commit/dirty state，并显式记录delete/rename/intent-to-add；按canonical path/type排序后计算总SHA-256，同时保存HEAD和`git diff --binary HEAD`摘要以覆盖index、worktree tracked patch。tracked/untracked范围、精确纳入项与精确排除项均进入manifest identity；未知artifact class或排除规则不匹配时fail closed。
- 两类manifest必须内容寻址、不可变、访问受控，或以reviewed仓内治理artifact保存必要非敏感摘要；不得写入生产包或成为第二源码真相。identity绑定schema version和content digest，敏感path/content按最小披露处理。Git仓内治理文件使用review、base revision和merge conflict保证并发一致性，不为此新增Runtime式CAS、lease或后台服务。
- evidence retention按证明性质分类：`normative evidence`包括source tree/baseline、ledger、scoped decision、contract/schema和test definition，必须长期可寻址；`reproducible evidence`保存精确命令、环境/依赖identity、deterministic fixture和预期判定，可从normative source重跑；`ephemeral diagnostic`包括普通CI log、profile、临时扫描输出和调试转储，允许按有界策略过期；`irreplaceable evidence`包括provider/process原始receipt、不可重现migration proof及其他无法从canonical state重建的事实，按所属domain retention/legal-hold策略保存。normative或仍被依赖的irreplaceable evidence丢失、不可访问，或reproducible evidence按声明环境重跑失败，会使相关`VERIFIED`失效；普通CI日志或临时诊断自然过期本身不得重开finding。verification declaration必须引用所依赖的证据类别和identity，不能用未分类的“result artifact仍存在”作为统一关闭条件。
- 原 96 项验收台账当前为 79 `DONE`、17 `IN_PROGRESS`；本需求不修改该台账状态。
- 早期关键词初筛约2253个命中、330个生产文件，AST初筛约971个宽签名候选、66个直接 `BaseModel` class；本轮已按owner、consumer、durability、安全和失败语义完成分类，原始命中数不是缺陷数量，也不得作为整改KPI。
- 已确认生产 nested import 为 0，五层逆向 import 为 0。
- 本文B1–B37 finding均有当前生产源码反证；其中部分finding聚合同一canonical owner下的多个具体缺口，不表示只有37个文件或37行问题。
- 本次扫描未运行测试、未执行生产入口、未访问仓外项目；结论来自当前dirty worktree的静态源码与架构门禁。由于用户改动仍在进行，后续源码变化必须重新跑同一静态分类矩阵，不能把本基线当作未来commit的永久证明。

最低可复现扫描记录必须保存命令、原始输出或机器可读 artifact，并按生产包分别定位 symbol/path。至少包括：

```bash
git rev-parse HEAD
git status --short
git diff --binary HEAD | sha256sum
rg -n --glob '*.py' '\bAny\b|\bobject\b|Callable\[\.\.\.|\b(getattr|hasattr)\s*\(|\bBaseModel\b|dict\s*\[' contracts kernel runtime orchestration product
rg -n --glob '*.py' '^\s+(from|import)\s+' contracts kernel runtime orchestration product
rg -n --glob '*.py' 'importlib|import_module|__import__|__getattr__' contracts kernel runtime orchestration product
rg -n --glob '*.py' 'subprocess\.|create_subprocess_|Popen\(' contracts kernel runtime orchestration product
```

关键词输出只是候选集。每条finding和独立需求必须登记evidence dependency set：精确symbol/path、consumer search scope、owner declaration、composition entrypoint、authoritative gate和scoped D-instance。source baseline变化后计算受影响集合：owner/definition/consumer/composition/gate范围变化才使对应证据失效；无关bounded context变化可保留证据，但必须记录新baseline identity和依赖判定。

“无消费者”“唯一入口”“无第二owner”等全仓全称结论依赖全局production search scope；任何新增生产文件、相关import/call pattern或entrypoint变化必须重跑该全局搜索。`AGENTS.md`变化必须按新规则重审所有受影响需求。不得因任意无关HEAD变化全量作废全部B项，也不得忽略dependency set实际命中；closure ledger据此将记录保持有效或转为`OBSOLETE_BY_SOURCE_CHANGE`/重新OPEN。

## 9. 本轮全仓覆盖矩阵与排除结论

| 审计面 | 覆盖结论 | 对应finding/排除证据 |
|---|---|---|
| 五层依赖、nested/dynamic import、side-effect registry | 静态五层逆向与nested import未见新增；dynamic import/side-effect门禁存在漏检 | B16、B26 |
| `Any/object/Callable[..., ...]/getattr/hasattr/BaseModel` | 正式边界按external adapter、strict decoder、private implementation与真实debt分类 | B1–B9、B18、B20–B22、B31、B33、B37 |
| 裸dict、mutable query、service/object graph泄漏 | durable/query/control面仍存在明确反证 | B4、B14、B18、B22、B24、B25、B29、B33 |
| Agent identity、lineage、capacity、turn、delivery、budget | lineage/turn codec严格项已排除；capacity/accept/delivery/retention仍未闭合；budget部分结算可由lineage reservation IDs幂等重试，暂不另立包 | B19、B23、B27 |
| Workflow与BackgroundTask | durable run/reconciliation、Product live view、task mutable metadata/output retirement均覆盖 | B24、B25、B29 |
| Durable store、codec、CAS/fence、clock、retention | 已枚举Session、Inference SQLite、RunJournal、service-call、subscription、Agent、Workflow、Cron、Artifact/FileOps/OAuth；治理inventory仍漏项 | B2、B5、B10、B11、B21、B25–B28、B30、B34–B36 |
| Artifact/FileOps/session cleanup删除 | metadata manifest、cursor SQLite事务等严格实现已排除；GC/workspace cleanup闭包与fenced delete不足 | B10 |
| Tool/effect/permission/hook/sandbox/process | fixed argv与authorized shell公开入口已确认分离；sandbox实际posture、tool binding/snapshot/effect仍有缺口 | B4、B12、B14、B17、B32 |
| Provider、OAuth、admin、webhook、secret | secret vault codec/lock与webhook evidence binding已确认严格；OAuth CAS仍有问题；inference admin是未接入surface，moderation及专用异常decorator是无consumer危险死代码，均删除优先 | B21、B33、B37 |
| LSP、interactive、UI connection/task lifecycle | LSP wire未严格投影；无owner cancel/interrupt/presentation task已覆盖 | B15、B31 |
| Import activation与可选backend | Web/media显式registry中的isolated registration不误判；Temporal/Squilla/i18n存在真实dynamic/side-effect问题 | B16、B26 |

明确排除的误报：

- `runtime/process.py` 的fixed argv与authorized shell是两个公开typed入口；私有collector内部的exec/shell分支本身不是双信任API；
- `runtime/secrets/store.py` 的vault读取采用exact-shape decoder、跨实例FileLock和fsync atomic replace，损坏fail closed；其问题不与OAuth backend CAS混同；
- provider webhook当前验签在parse/admission前且evidence store检查execution/generation/event digest冲突，未发现验签绕过；
- `LocalServiceCallJournal`已有单call flock、owner generation、strict transition和fsync，B36只针对retention/scan，不把它误报成无owner journal；
- `ResidencyStore.forget()`要求record revision、current lease和install fence，当前删除入口本身不列为无fence删除；
- isolated inference restore只在空目录、cutover前清理失败target，当前证据不足以归类为删除active authority；
- canonical `SessionLog`通过 `LocalEventJournal.verify_committed()` 校验sequence/checksum，中段损坏fail closed；通用line journal注释不能反推SessionLog会跳过中段损坏。
