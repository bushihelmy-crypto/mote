# Mote Python 包治理控制面 MVP 执行规范

- 状态：Draft v1；允许实现与隔离验证，不具备 production cutover 授权能力
- 依据：PYTHON_PACKAGE_GOVERNANCE_STRATEGY.md
- 范围：Phase -1A、-1B0、-1B1，以及 -1B2～-1D 的最小只读闭环
- 强制开关：G6_MIGRATION_READINESS = disabled

本文把总策略收敛为可顺序实现的最小控制面。它不批准移动任何生产定义，不创建目标业务包，不更新 public API，也不把旧 governance 产物升级为可信证据。

## 1. 总体完成条件

MVP 完成只表示治理工具能够可信地产生候选事实并拒绝无效输入。必须同时满足：

1. 旧治理入口不能产生新的 approved、cutting-over、verified 或 completed 状态；
2. Bootstrap validator 只依赖 Python 标准库且不 import mote；
3. 所有输出绑定同一冻结 Git tree 和唯一 facts generation；
4. G1～G5 可以计算，unknown/unsupported 均 fail closed；
5. G6 无论输入如何都返回 production authorization denied；
6. Journal、facts、provenance 和 projection 可从干净环境确定性重建；
7. 失败注入不会修改生产源码或用户现有脏工作树。

## 2. 目录与所有权建议

实现评审可调整物理目录，但逻辑边界为：bootstrap 负责 canonical encoding/schema/journal/ID validator；discovery 负责 Git tree 与 AST；graph 负责 import/symbol/re-export/provenance；gates 负责 G1～G5 和永久关闭的 G6 stub；CLI 负责 hermetic parser 与 structured runner；fixtures 保存正向、反向、mutation 和 rebuild 语料。

治理实现放在架构测试/治理工具范围，不进入 contracts、kernel、runtime、orchestration 或 product，也不得被产品运行时 import。

## 3. MVP-1：撤销 Legacy Authority

输入包括旧 CLI/测试入口、zdocs/architecture 下旧 contracts manifests、CI/脚本引用和当前 commit。

依次执行：

1. 建立版本化 legacy-authorities 清单；
2. 将旧产物标为 untrusted_legacy，保留审计内容；
3. 让旧入口无法产生任何生产授权状态；
4. 禁止硬编码 coverage、空 identity/event/error manifest 和空 DAG 进入授权判断；
5. 添加 negative tests 覆盖旧 CLI、旧 phase gate、旧 manifest 和旧 CI command。

退出条件：所有 legacy entrypoint 的授权请求均明确拒绝；拒绝使用稳定机器退出码；旧产物未被覆盖或伪装成新 schema；生产源码零改动。

本阶段不重新生成可信 facts，不修正旧 decisions，不运行 production cutover。

## 4. MVP-2：Bootstrap Validator 与 Genesis

Validator 最小验证范围：规范编码；schema version/必需字段；journal sequence/aggregate revision/ancestor reference；ID namespace/collision/tombstone；projection 无独立授权权；structured command 无自由 shell 字符串；legacy authority 不能成为 active authority。

Genesis 由两名独立复核者从干净环境计算相同 digest，记录参与者、输入 digest、genesis event/commit、签名或 attestation、镜像/封存位置和 activation observation。

若暂时没有真实签名或远端 branch protection，必须标记 bootstrap_mode = isolated_unsigned，且 G6 永久关闭；不得以模拟签名宣称生产可信。

退出条件：validator 在不 import mote 的环境运行；同输入重复运行字节级一致；sequence gap、revision conflict、ID reuse、projection edit、command injection 均被拒绝；validator 只读且不修改 mtime、facts 或 lock state。

## 5. MVP-3：原子 Static Facts Generation

正式运行只读取一个冻结 Git tree，并拒绝脏工作树授权输入、shallow/sparse 缺失 tree、未裁决 submodule/LFS/symlink、case/Unicode 等价碰撞和扫描期间 tree 漂移。

首批扫描组件仅要求：Git tree/file inventory、Python module normalization、runtime import 与 TYPE_CHECKING edge、local import 候选、__all__/re-export/root facade，以及语法错误和无法解析引用的 unknown。

Identity、lifecycle、security dataflow、packaging、generated/vendored provenance 尚未实现时必须输出 unsupported，不能输出空数组或 100%。

所有组件先写隔离临时目录，成功后发布包含 facts generation ID、source commit/tree digest、scanner/schema digest、input manifest digest、component digests 和 generation_complete 的唯一 index。只有全部声明 supported 的组件成功后才能 CAS 发布；禁止跨 generation 读取。

退出条件：两个干净环境生成相同稳定内容；中途失败不发布 partial generation；tree 漂移失败；各分类状态可区分；coverage 从候选全集计算，分母为零不会变成 100%。

## 6. MVP-4：Provenance 与 G1～G5

每个可引用 artifact 记录来源 artifact/source digest 和消费者反向索引；无 provenance、跨 generation edge、环或悬空引用均拒绝。

- G1 Discovery：所有声明支持的 tree entry/AST 候选已枚举；
- G2 Classification：相关候选无 unknown/conflicted；
- G3 Identity：未实现时为 unsupported，不能通过；
- G4 Ownership：只接受显式人工 decision，scanner 不猜 owner；
- G5 Closure：只在 scanner 声明的 soundness 范围内计算；
- G6 Migration Readiness：固定 disabled，任何调用返回 policy rejection。

Gate applicability 由 registry rule 计算。人工只能把 N/A 升级为 required，不能反向降低。

退出条件：source 变化失效完整相关闭包；缺边、环、跨 generation 和 unknown applicability 均失败；G6 不存在环境变量、隐藏开关或手工 manifest 绕过路径。

## 7. MVP-5：验证语料与失败注入

必须覆盖 deterministic rebuild、journal sequence/revision conflict、projection edit、ID collision/tombstone reuse、mixed generation、missing/cyclic provenance、relative/alias/local/TYPE_CHECKING import、annotation string、dynamic import unknown、re-export chain、syntax/version error、repository capability mismatch、manifest command injection、partial write、runner crash 和 G6 bypass。

全部失败注入在临时 Git repository/worktree 执行。结束后验证生产源码、人工 decisions 和用户工作树 digest 未变化。

## 8. CLI 最小命令契约

MVP 至少提供 legacy-check、bootstrap-check、discover、facts-check、graph、provenance-check、gate-check G1～G6 和 rebuild-check。

读命令不得写文件。写命令必须显式指定 output-dir，只能写隔离目录并通过原子发布进入受治理路径。外部命令使用结构化 argv，不使用 shell=True。

统一退出码建议：0 pass；2 drift；3 unknown/conflict；4 invalid schema/integrity；5 policy rejection；6 unsupported；7 internal error。机器输出使用版本化 JSON envelope；stderr 只写人类诊断且不得包含 secret。

## 9. MVP 性能与运维基线

首次实现记录 full scan、facts generation、provenance build、journal replay、projection rebuild 的 wall time 和 peak memory。暂不设脱离当前仓库的绝对目标，但第二次运行不得依赖不可验证缓存才能完成。

每个组件登记 service owner、backup owner、runbook 和 restore drill。依赖不可用时默认禁止新 approval/cutover，只允许读取已有状态、freeze、containment 和不扩大风险的 rollback。

## 10. MVP 完成后仍然禁止

即使 MVP-1～MVP-5 全部通过，仍禁止：将 G6 设置为 pass；执行真实 cutover；修改生产模块包路径或 owner；用旧 decisions 自动填充新 owner；把 unsupported 当作 not-applicable；发布 release completion；宣称 Phase -1G 或 Phase 0 完成。

解除禁令需要另行审核 MVP 实现、Git branch protection/签名/镜像、merged-unverified/repair 状态机、lease/trust/time、evidence availability 和 post-merge failure fixtures。

## 11. 推荐实现顺序

MVP-1 legacy revocation → MVP-2 bootstrap validator/genesis → MVP-3 atomic facts → MVP-4 provenance/G1～G5（G6 disabled）→ MVP-5 failure fixtures → implementation review。完成该链仍不自动获得真实试点授权。

## 12. 阶段验收矩阵

| 阶段 | 必需产物 | 必需机器命令 | 成功条件 | 必须拒绝 |
| --- | --- | --- | --- | --- |
| MVP-1 | legacy authority inventory、revocation record、negative evidence | legacy-check | 所有旧入口只能返回不可授权状态 | approved/cutting-over/verified/completed |
| MVP-2 | bootstrap envelope、genesis event、validator、ID/journal schema | bootstrap-check、rebuild-check | 两个干净环境得到相同 digest；validator 只读 | sequence gap、revision conflict、ID reuse、projection edit、自由 shell 命令 |
| MVP-3 | repository profile、facts artifacts、generation index | discover、facts-check、rebuild-check | 单一冻结 tree、完整 generation、字节级确定性 | partial/mixed generation、tree drift、unsupported 伪装完成 |
| MVP-4 | provenance graph、reverse index、Gate registry、G1～G5 evidence | graph、provenance-check、gate-check | 失效闭包完整；applicability 无 unknown | 缺边、环、跨 generation、人工降低 Gate、任何 G6 pass |
| MVP-5 | fixture corpus、mutation cases、crash/rebuild evidence | 全套 fixture test | 非法输入 fail closed，环境清理通过 | 修改生产源码、人工 decisions 或用户工作树 |

任一阶段只有在前序阶段产物 digest 当前有效时才能开始。发现前序缺陷时生成新的修订产物，并使相关下游 authorization 失效。

## 13. 必需机器输出 Envelope

所有命令 stdout JSON 至少包含 schema version、command ID/definition digest、authority ID、source commit/tree digest、facts generation ID（若适用）、开始/结束时间、result、exit code、diagnostic codes、artifact IDs、unsupported capabilities，以及 production_authorization=false。

production_authorization 在 MVP-1～MVP-5 必须恒为 false，字段缺失也视为失败。诊断使用稳定 code；自然语言 message 不参与机器判定。

## 14. G6 不可绕过要求

G6 hard-disable 必须同时成为 schema、validator、CLI 和 Gate evaluator 的共同不变量：

1. MVP authority 的 allowed transitions 不含 production approval/cutover/completion；
2. Gate registry 将 G6 固定为 policy rejection；
3. CLI 不提供 enable/override 参数；
4. 环境变量、配置、manual ruling、exception 和 legacy manifest 均不能改变结果；
5. 测试枚举所有 public command paths，验证 G6 恒定返回退出码 5；
6. 只有后续独立 MVP-6 activation event 才能引入新的 authority version，不能原地修改 MVP authority。

## 15. 实现提交边界

每个 MVP 使用独立可评审提交，只包含该阶段工具、schema、fixture 和生成证据。禁止混入生产包移动、业务重构、依赖升级或配置行为变化。

提交说明列出阶段 ID、输入 digest、修改/生成文件集合、验证命令、unsupported、non-capabilities、回滚方式和下一阶段解锁条件。
