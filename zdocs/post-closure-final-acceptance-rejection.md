# Mote Post-Closure 最终架构验收拒收报告

> 验收结论：**REJECTED / 不允许关闭**  
> 验收日期：2026-08-04  
> 验收对象：`zdocs/post-closure-final-acceptance-report.md`  
> 治理证据：`zdocs/architecture/post-closure-governance-evidence-v1.json`  
> 验收依据：`AGENTS.md`、`zdocs/post-closure-boundary-debt-implementation-requirements.md`、当前生产源码、当前测试与可执行治理门禁

## 1. 最终判定

原报告声称的以下结论不予接受：

```text
requirements=103
VERIFIED=103
OPEN=0
BLOCKED=0
verification_disposition.PASS=103
```

现有治理证据不能证明 103 条 exact requirement 已全部闭合。当前不得关闭本轮 post-closure 工作，不得继续沿用 `PASS / 允许关闭`、`VERIFIED=103` 或等价表述。

**所有未能以真实执行结果和完整闭环证据证明已经满足验收合同的项目，一律打回重新修改。** 对应 requirement 必须撤销无效的 `VERIFIED/PASS` 状态，回退到与实际进度相符的 `OPEN`、`ASSIGNED`、`IN_PROGRESS`、`IMPLEMENTED` 或 `BLOCKED`，完成修改、验证和独立复核后方可重新申请签收。

## 2. 拒收原因

### 2.1 治理 evidence 属于机械自证

`zdocs/architecture/generate_post_closure_governance.py` 没有执行各 requirement 的 `verification_commands`。生成器仅根据 requirement 是否存在于静态 `_VERIFIED_REQUIREMENTS` 字典，直接生成：

- `status: VERIFIED`；
- `verification_disposition: PASS`；
- 当前时间形式的 `verification_instant`；
- 文件路径及内容摘要。

生成器中的 `subprocess` 仅用于读取 Git revision，没有运行或核验所登记的 pytest、Pyright 或其他验收命令。因此，JSON 中的 103 个 `PASS` 是生成器赋值，不是 103 次可审计验证的结果。

### 2.2 strict validator 没有复算当前 source baseline

`ztest/architecture/post_closure_governance.py` 仅校验 baseline manifest 内已有字段和摘要格式，没有根据当前工作树重新计算并比对：

- `AGENTS.md`；
- production source tree；
- `ztest/` tree；
- requirements 文档。

因此，当前源码或测试发生变化后，只要变化文件不在某条 record 的有限 evidence 集合中，治理测试仍可能通过。原报告关于“任何后续源码变化都会令校验失败”的声明不成立。

### 2.3 `VERIFIED` record 尚缺有效闭环凭据

当前 103 条 record 均存在：

```text
decision_ids 为空                 103/103
legacy_exit_receipts 为空         103/103
recovery_conditions 为空          103/103
migration_disposition 统一为       NOT_APPLICABLE
```

validator 只检查部分字段的数据类型，没有证明其内容满足 requirement，也没有校验：

- scoped decision 与 affected requirement 的绑定；
- integrated source identity；
- verification command 的实际开始、结束、exit code 和输出摘要；
- migration inventory、candidate、cutover、rollback 和旧路径退出凭据；
- takeover、stale fence、corruption、partial write 和 `IN_DOUBT` 结算证据；
- capacity、latency、compaction、storage 与 retention bounds；
- Product construct、activate、restart/resume 和 shutdown smoke。

仅有源码文件摘要和测试文件摘要，不能替代上述运行和闭环证据。

### 2.4 最终全仓签收条件未满足

实施需求明确要求最终签收完成：

- 全部 `ztest/architecture/`；
- 全量 Pyright；
- 受影响完整测试；
- 最终全仓测试；
- 五层逆向依赖、生产局部 import、宽类型、反射、动态 import 和第二 owner/path 等全局门禁。

原验收报告明确承认未运行全仓 pytest，只登记了少量定向测试和 targeted Pyright。这不满足最终全仓签收合同，资源限制不能自动构成验收豁免。

## 3. 已完成的独立抽查

本次独立复核运行结果为：

```text
typecheck/verify_type_contract_cases.py                                      PASS
ztest/architecture/test_post_closure_governance.py                          1 passed
Model/OAuth/Session 报告所列五组定向测试                                   23 passed
```

这些结果只证明被抽查的小范围测试在当前工作树通过。由于治理门禁存在上述证据生成和验证缺陷，它们不能外推为 103 条 requirement 全部闭合。

## 4. 打回重新修改范围

本次不是只打回治理 JSON 或验收报告。**所有无法逐项提交有效闭环证据的 requirement 均属于打回范围。** 重新修改至少包括：

1. 修复治理生成与验证机制，禁止在未运行 recipe 时生成 `PASS`；
2. 对 103 条 requirement 逐项重新评估真实状态，撤销机械生成的 `VERIFIED`；
3. 对仍有 contract、owner、composition、lifecycle、persistence、observability、consumer、migration、retirement 或 test 缺口的项目重新修改生产实现；
4. 对仅有静态断言、缺少运行负例或故障恢复证据的项目补齐测试；
5. 对旧 reader、writer、alias、export、fallback、兼容路径、第二 owner/store/execution path 和 migration 残渣重新搜索并清理；
6. 对 durable domain 补齐 restart、CAS/fencing、takeover、corruption、partial failure、migration、retention 和 purge 证据；
7. 对存在外部动作的 domain 补齐 intent、execute、receipt、`IN_DOUBT` 和恢复对账；
8. 完成全量架构门禁、全量 Pyright、受影响完整测试及最终全仓测试；
9. 生成不可由声明字段自证的执行凭据，并由独立 validator 校验；
10. 在所有阻断项真实闭合后重新生成验收报告并申请重新签收。

不得通过重新生成摘要、更新 manifest identity、修改 verification instant 或再次把静态字典项目标记为 `PASS` 来视为完成整改。

## 5. 重新申请验收的最低条件

重新申请前必须同时满足：

1. baseline validator 从当前工作树独立复算所有 authoritative source sets；
2. 每条 `VERIFIED` 都绑定实际执行过的 recipe、exit code、输出摘要、执行环境和时间；
3. recipe 或必要门禁失败时，生成器 fail closed，禁止产生 `VERIFIED/PASS`；
4. scoped decision、approval authority、write set、integrated source identity 和 dependency DAG 可机械核验；
5. migration requirement 提交真实 inventory、candidate、read-back、cutover、rollback、retirement receipt；
6. requirement 的全部关闭条件有正例、负例及故障恢复证据；
7. 原需求第 18 节规定的最终全仓签收项目全部完成；
8. 独立复核确认不存在第二真相、兼容残渣或未迁移 production consumer。

在上述条件完成前，所有相关项目保持未关闭状态，不得发布“全部闭合”“允许关闭”或等价结论。

## 6. 签收状态

### 6.1 整改进展（2026-08-04）

原机械 `VERIFIED` 生成路径已经移除。治理生成器现会串行执行精确 recipe，并保存命令、源码 baseline、完成时 baseline、退出码、输出摘要、输出 artifact、环境和时间；validator 会独立重算 baseline 并校验 receipt 与输出 artifact。当前重新生成的权威 evidence 为：

```text
requirements=103
VERIFIED=91
OPEN=12
BLOCKED=0
```

剩余 OPEN 集中在 Session 与 Workflow 的 7 条尚未执行 recipe，以及依赖这些 recipe 的 governance requirement。由于 WSL 稳定性限制，当前未继续启动测试；这些条目不得以静态检查或治理 JSON 自证替代真实 receipt。

```text
acceptance=REJECTED
closure_allowed=false
claimed_verified_records=91
accepted_verified_records=91
open_records=12
action=COMPLETE_REMAINING_RECEIPTS_AND_REAPPLY_FOR_ACCEPTANCE
```

这里的 91 条 accepted record 均来自整改后的真实执行 receipt，而非继承原机械 `VERIFIED` 状态；它们仍不足以签收整个 requirement 集合。12 条 OPEN 全部闭合并通过独立 validator 前，本拒收结论继续有效。
