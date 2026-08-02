# Mote 架构边界闭合实施需求整合稿评审

> 审核对象：`zdocs/post-closure-boundary-debt-implementation-requirements.md`
>
> 对照依据：根目录`AGENTS.md`、当前源码、`ztest/architecture/`、`post-closure-boundary-debt-requirements-review.md`第29–40节已确认结论
>
> 当前状态：`APPROVED / REVIEW CLOSED / READY TO IMPLEMENT IN ORDER`

## 1. 审核结论

整合稿方向总体正确，但存在实质回归，当前不能作为脱离原审核稿的唯一实施基线，也不能据此启动production writer、migration cutover或destructive cleanup。

已关闭的domain产品决定并未失效；重新打开的是“整合稿是否准确、完整承接这些决定”的文档评审。当前允许继续：修订Markdown、补identity/decision/bounds/write-set对照、只读源码再基线。修复本评审列出的P0/P1问题后，才能恢复：

> `REVIEW CLOSED / READY TO IMPLEMENT IN ORDER`

## 2. 总体评价

整合稿正确保留了：

- 五层依赖与单canonical owner；
- 无compat、fallback、长期双读双写和第二执行链；
- strict、forward-only durable migration骨架；
- 四类external-effect capability及`IN_DOUBT`语义；
- Product唯一composition root；
- Agent delivery/turn双owner事务；
- Workflow与BackgroundTask分离；
- RunJournal拆分退役而非建设v2；
- ServiceCall remote operation与caller等待分离；
- Artifact typed edge、完整reachability与fenced deletion；
- Session rollout唯一truth，Presentation/wire/Notebook不建第二durable store；
- Runtime maintenance按domain拆分；
- strict codec、stale fence、crash、capacity和production recipe验收。

问题主要来自整合压缩：已冻结的数值和逐状态迁移被概括为“bounded/按已冻结策略”，稳定requirement ID被合并或改名，唯一writer矩阵没有承接，并出现安全保留期和开工DAG的直接语义变化。

## 3. P0阻断回归

### 3.1 S0全局前置被弱化

整合稿一方面声明`Governance/source baseline -> 每个workstream首节点`，另一方面写“Wave名称不构成全局barrier”。这会被理解为可以先改production、后补治理baseline。

确认过的准确语义是：

- `R-W0-GOVERNANCE-001`是唯一全局production-write前置；
- S0完成前只允许只读inventory、需求和治理文档工作；
- S0完成后，各domain按自身DAG流水推进；
- 不要求所有S1完成后才能开始任一已经满足依赖的S2。

整合稿必须显式写出上述四点。“无全局barrier”只能描述S0之后的domain流水，不能覆盖S0。

### 3.2 OAuth错误地让plaintext secret等待180天

`R-W3-OAUTH-RETIRE-001`把plaintext v1、fallback、writer和migration reader统一放到“cutover与180天proof后删除”，混淆了三类退出：

1. access/refresh secret material：替换及borrow/refresh/revoke结算后默认24小时内擦除；
2. production fallback/writer：cutover同一迁移切片立即退出；
3. migration source/manifest/conflict evidence：按180天proof窗口保留，但不得因此保留可解密token。

必须拆成`SECRET_ERASURE`、`PRODUCTION_PATH_RETIREMENT`和`MIGRATION_EVIDENCE_RETIREMENT`。审计证据保留期不能延长秘密材料寿命。

### 3.3 Model durable旧数据处置重新变成实现时选择

`R-W3-MODEL-PERSISTENCE-MIGRATION-001`写“完成一次性migration或明确拒绝”。这里的“或”重新开放了已要求提前关闭的数据处置决定。

必须按现有每一种inference JSONL/SQLite restore source指定唯一disposition：

- canonical result/effect evidence：forward migration；
- 可确定重建且非authoritative的projection：明确退役；
- corrupt、unknown、identity conflict：blocked evidence；
- migration candidate、cutover与旧reader退出条件。

不得把选择留给实现者，不得用清空、completed-result双读或新空记录绕过。

### 3.4 已冻结的D07数值大面积缺失

第17节声称“本文列出的数值是hard实施合同”，但正文多数只写“bounded”或“执行已冻结的Product bounds”。作为独立实施基线，这等于没有给合同。

至少缺少：

| Domain | 必须恢复的数值类别 |
|---|---|
| Agent ingress | delivery/turn容量、inline/artifact threshold、claim/retry/scan/compaction、scheduler weight/priority/deadline、30/90/365天retention |
| Workflow effect | payload/evidence、attempt、reconcile scan、lease、90天完整记录与1年tombstone |
| Cron | schedule/occurrence容量、claim、retry/backoff、payload、scan/storage、30天完整记录与180天tombstone |
| OAuth | active subject、secret/metadata大小、attempt、borrow TTL、scan/compaction、90天metadata与1年tombstone |
| ServiceCall | per-root/deployment cap、1 MiB/64 KiB payload/receipt/response、attempt/poll/query/deadline、index/stream/store、90天/1年retention |
| Artifact | edge cap、64/16 KiB inline、10,000-edge closure、500/100 deletion batch、10/100 GiB、30秒claim、orphan 180天 |
| Session | 1/2 MiB record、100 facts/8 MiB batch、256 MiB/1 GiB/1,000,000 facts、10/100 GiB、10,000 replay、500 listing、30秒lease |
| Event/Daemon | 65,536 subscription及retry/timeout/page；1 MiB DLQ、1,000/100 maintenance；64 KiB discovery、128 candidates、3 retries、0.1/0.5/2秒、10秒batch |
| Product surfaces | Connection、Notebook stdin、LSP的timeout、frame、depth/item和generation limits |

每个workstream必须增加versioned bounds表，至少包含：`default`、`hard max`、Product owner、超限disposition、是否允许extension收窄。不能用“按已冻结bounds”替代实际数值。

## 4. P1架构与实施回归

### 4.1 Stable requirement identity无映射地合并或改名

整合稿把原`R-W1-001..006`合成`R-W1-DEAD-SURFACES-001`，修改了Workflow requirement名称，并新增多个W0/W2节点，但没有说明旧ID的disposition。

必须增加完整映射表：

```text
old requirement
  -> integrated requirement
  -> PRESERVED | SPLIT_INTO | MERGED_INTO | SUPERSEDED
  -> reason
  -> inherited decisions/evidence
```

Stable identity不能因文档整合静默变化。内容修改只推进reviewed revision；只有owner或状态机真正拆分/合并时才建立新ID并显式supersede。

### 4.2 唯一writer矩阵退化为“编码前再提交”

整合稿要求实施前登记write set，但没有承接已确认的逻辑writer矩阵，因此仍可能出现：

- `ARTIFACT-CONSUMERS-001`跨写Workflow、Tool、Model、ServiceCall和delivery源码；
- RunJournal migration同时修改三个target domain store；
- Session deletion/retention并行改同一lifecycle/store；
- Artifact deletion/GC/workspace cleanup并行改相同删除路径；
- LSP、Presentation、Tool、OAuth、ServiceCall争写Product composition热点。

必须恢复canonical write-set矩阵。规则是：

- target domain owner修改自己的producer/store；
- integration requirement只协调Port、fixtures和验收，不取得其他domain writer权；
- migration owner读取legacy source并写candidate，不修改target状态机owner；
- 跨owner原子性通过Contracts-owned prepare/commit/receipt表达；
- 同一module同一时刻只有一个requirement writer lease。

特别需要明确：`R-W3-SESSION-DELETION-001`与`R-W3-SESSION-RETENTION-001`使用同一个Session writer lease，不得并行修改store/lifecycle。

### 4.3 删除项再次合并成跨域巨型ticket

`R-W1-DEAD-SURFACES-001`同时覆盖Model、HTTP interface、provider moderation、Runtime registry、Temporal/Squilla和i18n。即使写“每个bounded context独立签收”，一张ticket仍可能取得跨域write set。

该ID只能作为无production-write权限的epic/index。至少拆为：

- Model client retirement；
- inference HTTP admin retirement；
- provider moderation retirement；
- Runtime AES registry retirement；
- Temporal/Squilla loader retirement；
- i18n registry retirement。

每项独立登记consumer/public/export/docs/plugin审计、D21 authority、write set和`VERIFIED`状态。

### 4.4 Domain migration、retention和authority被过度摘要

通用迁移模板不能替代domain-specific决定。整合稿至少应为每个durable domain增加四张紧凑表：

1. `legacy state -> target disposition`；
2. active/terminal/IN_DOUBT/hold的retention；
3. submit/cancel/owner action/compact/purge authority；
4. D07 bounds。

当前缺失的关键内容包括：

- Agent delivery/mailbox/turn逐类迁移、owner action和capacity-before-accept；
- Workflow Temporal history仅为attempt evidence、legacy capability downgrade与application RunJournal退出；
- Cron 128-bit TaskId、occurrence disposition、mtime lower-bound和purge authority；
- OAuth selector/file/keyring/config/vault冲突规则、Product冻结backend且无fallback；
- ServiceCall PLANNED/STARTED/receipt/`.cancel`/owner/index逐状态迁移、closed owner-action集合；
- Artifact orphan quarantine、mtime仅为legacy lower-bound、producer completeness manifest；
- Session checksum/torn-write、blocked只读及security/hold/user/TTL命令分离；
- Event/daemon的D01/D02/D03/D06/D07/D10/D19实例。

## 5. P2歧义与可维护性问题

### 5.1 OAuth ABSENT语义

整合稿称“ABSENT只为query结果”，原确认清单又把ABSENT列入closed lifecycle。必须唯一选择并明确：

- 若ABSENT是query disposition，则metadata codec不得持久化ABSENT；
- 若ABSENT是持久状态，则必须有identity、revision和transition。

禁止同名表示两种truth。

### 5.2 BackgroundTask W0/W2边界

`R-W0-BGTASK-GOVERNANCE-VERIFY-001`只能验证owner、composition和已有事实，不能因为测试当前通过就把cleanup settlement视为已经交付。W2 query/cleanup/integration仍须独立实现和验证。

### 5.3 Notebook requirement混合两个owner

`R-W2-NOTEBOOK-001`同时包含Canvas/Notebook discriminated union和Notebook stdin incarnation。Document codec与stdin pending/reply lifecycle的变化原因、consumer和write set不同，应拆成两个requirement；若保留总ID，只能作为epic。

### 5.4 “旧fixture为零”过宽

应删除依赖旧入口或旧schema的fixture，迁移仍证明canonical行为的测试。不能为了关键词清零删除有效反例、corruption样本或migration source fixture。

### 5.5 Deployment proof与生产路径退出

必须区分：

- cutover时立即退出production reader/writer/fallback；
- evidence window内保留migration-only decoder/source；
- retention到期后物理删除source/evidence。

Proof window不能成为长期生产兼容期。

## 6. 未回归内容的保留要求

修订时不得以“统一格式”为由改变下列正确结论：

- delivery与turn继续两个store owner；
- Workflow不得借BackgroundTaskPool执行durable run；
- RunJournal不建设v2；
- ServiceCall pending index不保存第二payload/receipt truth；
- caller deadline不取消remote operation；
- Artifact owner不理解所有domain状态机；
- Session只释放自身edge，CAS删除由Artifact fenced owner执行；
- Presentation、wire、Notebook/ipynb不成为durable truth；
- Runtime maintenance不建立新的通用Manager；
- migration-only decoder不是production fallback。

## 7. 修订顺序

建议按下列顺序修订整合稿：

1. 修正S0全局前置和OAuth secret retention两个P0语义错误；
2. 关闭Model persistence每类source的唯一disposition；
3. 建立旧/新requirement identity映射并拆回跨bounded-context epic；
4. 承接canonical write-set/唯一writer矩阵；
5. 为每个durable domain补migration、retention、authority、bounds四表；
6. 消除OAuth ABSENT、BackgroundTask W0、Notebook epic和proof window歧义；
7. 运行机械coverage检查并复审全文。

在第1–4步完成前，不应继续扩写实现细节，否则新的章节会建立在不稳定identity和错误DAG上。

## 8. 恢复实施基线的复审门禁

整合稿必须同时满足：

1. 所有已确认stable `R-*`都有唯一identity mapping disposition；
2. 第29–39节全部`CONFIRMED` scoped decision映射到实施章节和requirement；
3. 每个适用D01/D02/D03/D07 scope可直接找到migration、retention、authority和具体数值；
4. 每个canonical write-set只有一个integrated writer族，epic/integration requirement无越权write lease；
5. S0明确为唯一全局production-write前置，S0后才允许domain流水；
6. OAuth secret erasure、production path retirement、migration evidence retention分离；
7. 全文不存在“迁移或拒绝”“按已冻结bounds”“必要时兼容”“实施时确认”等开放选择；
8. production reader/writer退出与migration source物理retention明确分离；
9. 文档自身足以回答实施者的owner、状态、失败、迁移、权限和上限问题；
10. 机械对照报告无遗漏、重复owner、identity漂移或无disposition决定。

全部通过后，将本文件状态改为：

> `APPROVED / REVIEW CLOSED / READY TO IMPLEMENT IN ORDER`

在此之前，原审核稿第29–40节仍是已确认产品与架构决定的事实来源，整合稿只能作为待修订候选，不能覆盖原结论。

## 9. 第二轮复审：第一批修复结果

### 9.1 结论

新版本从455行扩展到610行，上一轮四个P0已基本修复：

- S0已明确为唯一全局production-write前置，并正确限定“无全局barrier”只适用于S0之后；
- OAuth已拆分secret erase、production path retirement和migration evidence retirement，明确可解密secret正常最长24小时；
- Model persistence已取消“迁移或拒绝”的开放选择，改为canonical evidence迁移、projection退役、corrupt/conflict blocked的唯一disposition；
- 新增migration、retention/authority和D07表，Agent、Workflow、Cron、OAuth、ServiceCall、Artifact、Session、Event/Daemon及部分Product surface的主要数值已承接；
- 增加stable identity映射、canonical write-set矩阵，删除项和Notebook已降为无write lease epic并拆分owner-specific requirements；
- fixture、production reader退出与migration-only evidence window的措辞已纠正。

因此上一轮状态从“四项P0阻断”降级为：**无已知数据安全P0，但仍有P1一致性和新增decision provenance阻断**。整合稿尚不能标记`APPROVED`，本轮状态保持`CHANGES_REQUIRED`。

### 9.2 已关闭的上一轮finding

| 上轮finding | disposition | 证据 |
|---|---|---|
| 3.1 S0被弱化 | `FIXED` | 新稿第3节明确S0前禁止production语义修改、writer、cutover和destructive cleanup |
| 3.2 OAuth plaintext等待180天 | `FIXED` | 新稿第11节拆成三个requirements，secret正常最长24小时且180天只留secret-safe evidence |
| 3.3 Model migration二选一 | `FIXED` | 新稿第5.2节为canonical/projection/corrupt source指定唯一disposition |
| 3.4 D07数值大面积缺失 | `PARTIALLY_FIXED` | 第17.3节覆盖多数domain；Run domains仍缺，新增数值的decision provenance待补 |
| 4.1 identity无映射 | `PARTIALLY_FIXED` | 第4.4节已有表，但存在重叠/伪旧ID，见9.3 |
| 4.2 唯一writer缺失 | `FIXED_WITH_MINOR_FOLLOWUP` | 第4.5节承接逻辑矩阵；需补Run domains/Model writer行 |
| 4.3 删除巨型ticket | `FIXED` | epic无write lease，六类删除已拆owner-specific ID |
| 4.4 domain表过度摘要 | `PARTIALLY_FIXED` | 第17.1/17.2已补核心表，少数逐状态与authority仍需精确化 |
| 5.2 BackgroundTask W0/W2 | `FIXED` | 明确W0只读验证不代表交付 |
| 5.3 Notebook混合owner | `FIXED` | document/stdin拆分，旧ID仅epic |
| 5.4 fixture清零 | `FIXED` | 明确保留/迁移canonical migration、corruption和反例fixture |
| 5.5 proof window混淆 | `MOSTLY_FIXED` | 大部分domain已区分；Cron仍有残留措辞，见9.6 |

## 10. 第二轮新增与剩余问题

### 10.1 P1：RunJournal三个target domain的D07 bounds仍缺失

第17.2节已列Tool/Model/timer retention，但第17.3节没有`Run domains`行，遗漏第36.6节已确认合同：

- 单Tool intent arguments 1 MiB、terminal result/receipt 64 KiB；Model inline response 64 KiB；timer不得携带callback/blob；
- 每Session active Tool effects默认1,000/hard 10,000；ModelCalls默认100/hard 1,000；timers默认1,000/hard 10,000；
- Tool/Model/timer reconcile batch分别500/200/500、每批5秒；
- Tool四类capability的3次或1次attempt、1/5/30秒、query 12次/24小时；timer只允许一次canonical submission；
- domain frame 2 MiB，stream soft/hard 64/256 MiB；
- compaction 1,000 identities、candidate 64 MiB、5秒。

这不是新设计，必须原样加入第17.3节。否则`R-W3-RUN-DOMAINS-001`、`TOOL-EFFECT`、`MODEL-PROJECTION`和`SESSION-TIMER`仍需回查旧审核稿。

### 10.2 P1：identity mapping存在重叠规则和虚构的“原ID”

第4.4节同时写：

- `R-W3-WORKFLOW-* -> 同名 PRESERVED`；
- `R-W3-WORKFLOW-EFFECT-002 -> MIGRATION-001/002 SUPERSEDED`。

精确ID被family wildcard同时判定为PRESERVED和SUPERSEDED，违反“一条旧ID一个disposition”。类似问题可能出现在OAuth的family/RETIRE例外。必须改成穷举，或明确“family except下列exact IDs”，但authoritative ledger最终不得使用通配符。

此外，旧审核稿实际存在聚合`R-W1-006`，不存在已经发布的`R-W1-006-temporal`和`R-W1-006-squilla`原ID。当前表先把两个新ID写成“原ID同名PRESERVED”，再把旧006写成SPLIT，伪造了历史identity。

正确映射应是：

```text
R-W1-006
  -> R-W1-006-temporal + R-W1-006-squilla
  -> SPLIT_INTO
```

两个子ID是新stable identity，不是PRESERVED旧identity。所有family wildcard必须在S0生成exact展开报告并证明无重复命中。

### 10.3 P1：新增hard bounds没有scoped decision provenance

第17.3节混合了三类数值：已由第31–39节确认的数值、当前源码已有默认值、以及整合时新增的hard max。后两类不能自动冒充已确认D07 instance。

明显新增或至少未在原确认章节出现的包括：

- Agent `root weight hard 64`与`priority closed 0..3`；原决定只要求有界正整数/有界enum，没有冻结这两个具体hard值；
- LSP `16 MiB frame`、`depth 64`、`10,000 items`、`1,024 pending`、`query hard 30秒`；原D09只要求设置上限，没有确认数值；
- LSP `diagnostics每文件10、总30`与`render每文件12 symbols`把当前consumer展示/配置默认与wire/protocol hard contract放在同一行；`render 12`是presentation truncation，不是LSP response admission hard max；
- Connection/Notebook中的部分grace/retry数值需明确是D16/D20原合同还是当前源码默认，不得靠表位置隐式升级为十年stable hard contract。

按第29节授权，reviewer可直接确认合理选择，不需要询问用户，但必须产生新的scoped instance或现有D07 revision，记录：来源、为何选择该值、Product owner、超限语义、是否只属presentation而非protocol。建议拆为：

- `D07-agent-scheduler-weight-priority-bounds-v1`；
- `D07-lsp-code-map-profile-bounds-v1`；
- 必要时`D07-product-connection-and-notebook-bounds-v1`。

在此之前，这些值只能标`PROPOSED_NEW_BOUND`，不能写成“本文hard实施合同”。尤其不能把当前实现的12-symbol展示截断提升为LSP decoder拒绝第13个symbol。

### 10.4 P1：Model与Run domains缺少完整decision绑定和writer矩阵行

第4.6节列了Workflow、Cron、Agent、OAuth、Run domains等决定，但没有独立`Model execution/checkpoint`行。第5.2节新增五个Model requirements，其中旧source disposition、checkpoint retention、purge authority和bounds必须绑定精确scoped decisions，不能只依赖RunJournal的Model projection决定。

第4.5节也没有ModelCall/checkpoint canonical write-set行，导致`MODEL-CHECKPOINT`、`MODEL-PERSISTENCE-MIGRATION`、`MODEL-COMPOSITION`、`MODEL-RECOVERY`及`RUNJOURNAL` target migration的writer关系仍不机械。

修复要求：

- 为ModelCall/checkpoint/store添加唯一writer行；
- migration只读legacy并写candidate，Model owner写target state；
- RunJournal think migration不得修改Model state machine源码；
- 明确哪些Model D01/D02/D03/D07来自既有确认，缺失的直接形成scoped decision revision；
- `ABSENT`只有在canonical inventory证明无call/attempt时才能创建新调用。

### 10.5 P1：Confirmed decision绑定仍使用不可执行的压缩identity

第4.6节虽声明“多个/三个只是阅读压缩”，但该文件自称直接实施依据，表中仍出现：

- “三个`D01-agent-*-...`”；
- “两个`D02-agent-*-...`”；
- “三个`D01-run-journal-*-cutover-v1`”；
- “三个domain D02 retention instances”。

这类自然语言不能用于ledger引用、coverage gate或检测遗漏。整合正文可保留摘要，但必须附一张exact decision appendix，逐一列出完整ID、status、reviewed revision和affected requirements。否则“第29–39节所有CONFIRMED instance均已承接”仍无法机械证明。

### 10.6 P1：Cron production retirement仍把不同退出阶段写在一起

第10节第7项仍写“deployment proof后删除v2 decoder、旧identity和mtime控制路径”。这会让旧production decoder/mtime control继续存在至proof到期，与第3节和17.1节的“cutover立即停旧production path”冲突。

必须改为：

- v3 cutover同一切片删除v2 production reader/writer、短identity生成器和mtime control semantics；
- migration-only decoder/source/identity mapping不在production recipe，保留180天proof；
- proof满足后只物理删除migration evidence/source。

其他domain已经使用这一拆分，Cron必须一致。

### 10.7 P2：OAuth ABSENT歧义已选择，但需进入codec门禁

新稿选择“ABSENT只为query result，metadata state不含ABSENT”，该选择合理，并消除了同名双truth。还需在`R-W3-OAUTH-001`验收中明确：

- metadata decoder遇到持久化`ABSENT`必须拒绝；
- query在确无subject row时返回typed ABSENT；
- migration的“零source”不得写一条ABSENT metadata row；
- tombstone存在时不能返回ABSENT，而应返回RETIRED/REVOKED等canonical disposition。

这是codec门禁补强，不重新打开OAuth架构决定。

### 10.8 P2：整合稿状态提前写成“实施基线”

文件头当前写“状态：实施基线”，但独立评审仍为`CHANGES_REQUIRED`。这会使工具或实施者绕过评审状态直接使用。

在本轮P1关闭前，头部应写：

> `状态：候选实施基线 / CHANGES_REQUIRED；禁止production write`

复审通过后再原子改为`APPROVED / 实施基线`。仅写“production write等待S0”不足，因为当前问题发生在S0将要登记的identity、decision和bounds本身。

## 11. 第二轮复审后的准入状态

当前可继续：

- 修正文档和exact identity/decision appendix；
- 补Run domains bounds；
- 对新增Agent/LSP/Product数值做源码核对并形成scoped decision；
- 补Model writer/decision映射；
- 生成只读S0预览和coverage报告。

当前不可开始：

- 正式关闭S0 ledger；
- 依据新增但无provenance的bounds编写production enforcement；
- Cron/OAuth/Model/RunJournal migration cutover；
- destructive retirement或cleanup。

剩余关闭条件：10.1–10.6全部修复，10.7进入codec验收，文件状态与评审状态一致；随后运行第8节十项机械门禁的第二次对照。完成后才可把独立评审状态改为`APPROVED`。

## 12. 第三轮关闭性复审

### 12.1 判定

**暂时不能关闭。** 第二轮列出的核心阻断中，Cron retirement已修复，新增的requirement执行状态和workstream准入矩阵有价值；但其余P1多数仍原样存在，文件头仍提前宣称“实施基线”。当前保持：

> `CHANGES_REQUIRED / NOT READY TO CLOSE`

本轮没有发现新的数据安全P0；剩余问题集中在S0即将写入的identity、decision、bounds和状态机自身，因此仍阻断正式关闭S0及production write。

### 12.2 第二轮问题逐项复核

| 第二轮finding | 当前状态 | 复核结论 |
|---|---|---|
| 10.1 Run domains D07缺失 | `NOT_FIXED` | 第17.3节仍没有Tool/Model/timer行；仅17.2 retention存在 |
| 10.2 identity重叠/伪旧ID | `NOT_FIXED` | 仍同时存在Workflow family PRESERVED与EFFECT-002 SUPERSEDED；仍把新`R-W1-006-temporal/squilla`写成旧ID PRESERVED |
| 10.3 新hard bounds无provenance | `NOT_FIXED` | Agent/LSP新增数值仍直接列作实施合同，无新scoped D07 instance或来源分类 |
| 10.4 Model decision/writer缺失 | `NOT_FIXED` | 第4.5无ModelCall/checkpoint writer行，第4.7无Model workstream decision绑定 |
| 10.5 exact decision appendix缺失 | `NOT_FIXED` | 仍使用“三个D01”“两个D02”“三个domain D02”等自然语言压缩 |
| 10.6 Cron退出阶段混合 | `FIXED` | 第10节已明确v3 activation立即退production path，180天只保留migration-only source/decoder |
| 10.7 OAuth ABSENT codec门禁 | `NOT_FIXED` | 仍只有正文语义，没有decoder/query/migration/tombstone四项验收 |
| 10.8 文件状态提前 | `NOT_FIXED` | 头部仍写“状态：实施基线”而独立评审仍为CHANGES_REQUIRED |

### 12.3 新增内容中正确的改进

- 第4.6节为requirement增加`OPEN -> ASSIGNED -> IN_PROGRESS -> IMPLEMENTED -> VERIFIED`及`BLOCKED`记录语义，明确epic无writer lease和IMPLEMENTED状态；
- requirement record字段补齐source baseline、decision、recipe、cutover、旧路径receipt和批准authority；
- 第4.8节增加workstream准入/cutover/关闭矩阵，正确区分migration evidence retention与domain进入VERIFIED；
- Cron第10节已经按cutover立即退出production、180天后物理退migration evidence拆分；
- OAuth、RunJournal、ServiceCall和Session的production reader退出措辞保持正确。

这些改进应保留，不应因剩余问题回滚。

### 12.4 P1：requirement状态机缺少BLOCKED恢复路径

第4.6节图只表达`IN_PROGRESS -> BLOCKED`，正文又要求BLOCKED保存“恢复条件”，但没有定义恢复后回到何种状态、是否重新取得writer lease和是否必须重新验证baseline。若把BLOCKED视为终态，就不能恢复；若允许直接回IN_PROGRESS，旧lease/source可能已经失效。

修复要求：

```text
BLOCKED -> ASSIGNED
```

只能在阻断事实消失、source baseline重新验证、依赖仍VERIFIED、write set重新取得唯一lease后恢复；不得`BLOCKED -> IMPLEMENTED/VERIFIED`，也不得沿用阻断前的过期writer lease。

`OBSOLETE`是finding/requirement经源码漂移证明不再适用的独立disposition，不应伪装成BLOCKED恢复或VERIFIED。

### 12.5 P1：状态机把所有前置要求为VERIFIED可能错误阻断同一切片原子cutover

第4.6节规定进入IN_PROGRESS时“所有前置requirement为VERIFIED”。这对普通DAG合理，但第4.8节存在需要同一cutover generation原子激活的candidate、target writer、consumer path和legacy exit。如果把它们建成相互独立requirement，candidate/cutover requirement可能只有在集成验证后才能VERIFIED，而consumer又被要求等待其VERIFIED，形成循环或迫使提前把未激活candidate标VERIFIED。

修复要求：明确两类依赖：

- `completion dependency`：前置必须VERIFIED；
- `activation cohort dependency`：各成员可在独立write lease下达到IMPLEMENTED，由唯一cutover requirement在同一generation验证并原子激活；cohort成员不能各自发布writer。

禁止用降低为“前置只需IMPLEMENTED”作为通用规则。只允许schema中显式登记的activation cohort采用集体cutover，且最终每个成员仍需绑定同一integrated generation后VERIFIED。

### 12.6 P2：新增矩阵和验收列表存在机械重复

- 第4.8节`Event/Daemon`行重复两次；
- 第18节验收第9项重复两次；
- 第4.4节声称family wildcard逐一保持同名，却又对family内部exact ID给出SUPERSEDED例外，机械解析无法确定优先级。

这些不是纯排版问题：重复/重叠会让coverage validator产生双owner或双disposition。关闭前必须删除重复行、恢复连续编号，并让每个exact identity只命中一条mapping。

### 12.7 关于新增LSP/Agent bounds的最终处置

本轮仍未提供这些数值的decision evidence。审核不要求再询问用户，但整合者必须二选一并写明：

1. 建立新scoped D07 instance，由reviewer依据源码和产品定位直接`CONFIRMED`；或
2. 将现有实现数字标为`CURRENT_DEFAULT / NOT_YET_HARD_CONTRACT`，并在对应contract requirement中先完成负载/安全验证后再确认hard max。

推荐选择1，并修正LSP维度混淆：

- wire frame、pending request、query timeout、recursive decode depth/item是LSP protocol bounds；
- diagnostics展示10/30和render 12是Product projection/presentation bounds，不得导致LSP合法response在第13项被decoder拒绝；
- Agent weight64/priority0..3属于scheduler policy generation，不是delivery wire codec上限。

不同owner的bounds不能只因同属一张表而由同一D07 instance拥有。

### 12.8 最小关闭清单

下一版只需完成以下机械修订，不需要再扩展新架构章节：

1. 补Run domains D07表行；
2. 把requirement mapping展开为exact、无重叠记录，正确处理旧`R-W1-006`拆分；
3. 为新增Agent scheduler、LSP protocol和Product projection bounds建立分owner scoped decisions；
4. 补Model writer矩阵和exact D01/D02/D03/D07 decision绑定；
5. 展开所有“多个/三个”decision ID；
6. 加OAuth ABSENT strict codec/query/migration/tombstone门禁；
7. 定义BLOCKED恢复和activation cohort；
8. 删除重复Event/Daemon行和重复验收第9项；
9. 将文件头改为候选状态；通过复审时再与本评审一起原子改为APPROVED。

九项全部完成后，可以直接做最后一次机械对照并关闭；当前没有理由再进行新的domain产品设计轮次。

## 13. 最终关闭复审

### 13.1 最终结论

**可以关闭。** 整合稿已经实质承接原审核第29–40节的owner、decision、migration、retention、authority、bounds、唯一writer和实施DAG，并修复前三轮发现的架构与安全回归。

最终状态：

> `APPROVED / REVIEW CLOSED / READY TO IMPLEMENT IN ORDER`

该批准允许在完成S0后按requirement DAG进入production implementation；不允许跳过S0、合并跨owner write lease、跳过migration dry-run/cutover门禁或执行未经授权的数据丢弃。

### 13.2 第三轮九项关闭清单结果

| 关闭项 | disposition | 最终证据 |
|---|---|---|
| Run domains D07 | `CLOSED` | 第17.3节新增Tool effect、ModelCall/projection、Session timer三行，承接capacity/payload/reconcile/attempt/stream/compaction/retention |
| exact requirement mapping | `CLOSED` | 第4.4节只使用实际旧exact ID；旧`R-W1-006`正确SPLIT为两个NEW_ID；Workflow EFFECT-002例外不再被family wildcard重复命中 |
| 新Agent/LSP/Product bounds provenance | `CLOSED` | 第4.7节新增分owner scoped D07 instances，区分Agent scheduler、LSP protocol、Product projection及Connection/Notebook |
| Model writer与decisions | `CLOSED` | 第4.5节新增ModelCall/checkpoint/store唯一writer；第4.7节增加Model D01/D02/D03/D07及affected requirements |
| exact decision identities | `CLOSED` | Agent和Run domains等压缩identity已逐项展开；ledger明确禁止通配符和自然语言数量 |
| OAuth ABSENT | `CLOSED` | 第11节增加decoder/query/migration/tombstone四项fail-closed门禁，ABSENT不进入metadata truth |
| BLOCKED恢复与activation cohort | `CLOSED` | 第4.6节规定唯一`BLOCKED -> ASSIGNED`恢复边，并区分completion dependency和原子activation cohort |
| 重复矩阵/验收项 | `CLOSED_WITH_EDITORIAL_FIX` | Event/Daemon重复行已删除；第18节仍重复一次第1项，内容完全相同，不改变合同，发布时删除一行 |
| 文档状态 | `CLOSED_PENDING_PUBLICATION_EDIT` | 候选状态正确等待本次批准；发布本批准时同步改为`APPROVED / 实施基线` |

### 13.3 新增scoped decisions审核结果

本轮接受整合稿直接确认的新增instances：

- `D07-agent-scheduler-weight-priority-bounds-v1`：weight default 1/hard 64、priority 0..3属于Product Agent scheduler generation，不污染delivery codec；
- `D07-lsp-code-map-profile-bounds-v1`：16 MiB frame、depth 64、10,000 items、1,024 pending、30秒query作为有限LSP profile hard bounds；
- `D07-product-code-map-projection-bounds-v1`：10/30 diagnostics和12 symbols仅为typed truncated projection，不拒绝合法LSP response；
- `D07-product-connection-and-notebook-bounds-v1`：保持D16/D20 lifecycle范围，timeout不伪造settlement；
- Model checkpoint D01/D02/D03/D07四个instances：从已有ModelCall/RunJournal确认语义收窄形成，owner、retention、purge和bounds未建立第二Model truth。

这些决定均有明确Product/Runtime owner、超限语义和affected requirements，且extension只能收窄，不构成未知未来抽象或权限扩大。

### 13.4 发布前两项非阻断编辑

批准发布整合稿时只需执行两项无语义机械编辑：

1. 把文件头改为：

   > `状态：APPROVED / 实施基线；所有production write必须等待S0关闭`

2. 删除第18节重复的第一条“authoritative contract、owner、composition、lifecycle、persistence、observability和tests全部闭合”，保留一条并维持1–10连续编号。

这两项不需要新一轮架构评审；编辑后做一次Markdown结构/重复项检查即可。若编辑同时改变任何decision、数值、authority、identity或DAG，则本批准不自动覆盖额外变化。

### 13.5 实施起点

第一个可实施目标仍是`R-W0-GOVERNANCE-001`。S0必须先冻结source baseline、exact requirement/decision ledger、production-capable recipe、write-set leases和先失败门禁。S0通过后，各domain按第3、4.8及第5–17节DAG流水推进；不存在要求所有S1完成后才开始任一满足依赖S2的全局齐步barrier。

评审关闭不等于治理实施完成。最终项目关闭仍以每个非obsolete evidence为`VERIFIED`、旧production路径和migration残渣退出、全量architecture/type/test/fault证据通过为准。
