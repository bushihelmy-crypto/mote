# Mote Post-Closure 最终架构验收报告

> 验收结论：**IN PROGRESS / 暂不允许关闭**  
> 验收日期：2026-08-04  
> 验收依据：`AGENTS.md`、`zdocs/post-closure-boundary-debt-implementation-requirements.md`、当前生产源码、严格治理清单与受控分批测试

## 1. 当前结论

实施文档登记的 103 条 exact requirement 已完成真实 receipt 重审；截至当前 baseline，91 条具备有效验证 receipt，12 条仍保持 OPEN：

```text
requirements=103
VERIFIED=91
OPEN=12
BLOCKED=0
verification_disposition.PASS=91
```

因此本报告当前不是最终签收证明。剩余 Session 与 Workflow recipe 未取得同 baseline 的成功 receipt 前，禁止声明全部闭合或允许关闭。

权威逐项结果位于：

- `zdocs/architecture/post-closure-governance-evidence-v1.json`
- `zdocs/architecture/post-closure-source-baseline-v1.json`

治理 evidence 不是自证材料。每条 `VERIFIED` 均绑定当前工作树 baseline、canonical owner、write set、decision/recipe identity、activation generation、approval authority、verification instant，以及当前源码/测试文件的内容摘要；严格 validator 会拒绝摘要漂移、缺失文件、未退出 retired path、非法状态和无 PASS 证据的 VERIFIED。

## 2. 本次最终补齐的边界

### Model checkpoint

- Model recovery 已收敛为唯一 typed inspection query；损坏、未知版本、identity mismatch 与 `IN_DOUBT` 均 fail closed。
- checkpoint 采用显式 `INTENT_COMMITTED -> WIRE_STARTED -> SETTLED/IN_DOUBT` 生命周期，wire 前先提交 durable intent。
- Product 显式注入批准容量、response/stream/compaction 与 90/365 天 retention policy。
- 超过 64 KiB 的模型响应在 terminal journal 前发布 canonical ArtifactRef，恢复时通过 canonical resolver 校验并重建。
- terminal compaction、tombstone purge 与 v1 inventory/candidate/cutover 均有 fenced、版本化实现和负例。

### OAuth credential

- metadata/CAS/revision/fence/borrow ledger 由唯一 file repository 拥有；File/Keyring 仅作为 secret vault，不再形成第二真相源。
- 所有生产消费者改为 consumer-bound durable borrow；旧 nullable token 与 `get_valid_token()` 生产路径已退出。
- logout、crypto erase、retire、legal hold、security clear 等维护动作使用 closed typed command。
- generation revoke 会立即撤销 borrow 并清除旧 secret；migration evidence 不保存明文，180 天后由 typed authority 退休。
- fallback store 与 provider 内部 OAuth refresh 旁路已删除并由架构门禁禁止恢复。

### Session stream

- v1 迁移拆为 inventory、inactive v2 candidate、manifest-last activation 三阶段；inventory 覆盖 rollout、目录、lease、checkpoint 与 Artifact roots，并有 10,000 facts/5 秒硬边界。
- v2 stream、checksum chain、Artifact edges 与 projection digest 在 activation 前 read-back；source/candidate digest 变化会阻断 cutover。
- `SessionLog` 严格校验 activation manifest、session identity、candidate prefix、projection digest、Artifact edge digest 与 committed checksum chain。
- checkpoint、FileOps、Runtime/machine projection 和 listing 均先经过 verified `SessionLog`；listing 的 head/tail 优化不再绕过 activation verification。
- v1 production reader/writer 已退出。raw v1 仅保留在 migration evidence，180 天后通过有 authority identity 的命令物理退休；active v2 rollout 与 Artifact edges 不受影响。

## 3. 关键历史拒收项的关闭结果

| 历史拒收项 | 当前结果 |
|---|---|
| RunGraph 从 `graph_meta` 选择 continuation | 已退出；inspection/tool metadata 仅作为执行/查询上下文，continuation 由 Workflow canonical definition、durable state/frontier 与 fenced owner 决定 |
| 未批准 Cron v4 candidate | 已退出；批准目标保持 v3，migration、activation、delivery 与 Artifact receipt 在同一批准 generation 下闭合 |
| Cron migration 穿透私有 store decoder | 已由公开 strict migration contract 替代，legacy reader 有明确退出与 evidence retirement |
| Model checkpoint optional recovery/双恢复路径 | 已收敛为唯一 typed recovery inspection 和严格 lifecycle |
| OAuth metadata/secret 双 owner及裸 token消费 | 已拆为 canonical metadata repository、secret vault 与 consumer-bound borrow |
| Session v1 reader及 projection 旁路 | 已退出；所有 production projection 必须先验证 v2 activation |
| 治理 JSON 机械自证 | 已改为当前工作树 baseline + 文件摘要 + strict validator；任何后续源码变化都会令校验失败，需重新验收 |

## 4. 最终受控验收结果

遵照 WSL 资源约束，本轮未启动全仓或并行 pytest；验收以单文件、最多两个小文件串行执行。最终收口结果：

```text
typecheck/verify_type_contract_cases.py                         PASS
ztest/architecture/test_post_closure_governance.py             1 passed
ztest/architecture/test_model_checkpoint_governance.py         4 passed
ztest/architecture/test_oauth_credential_governance.py         5 passed
ztest/architecture/test_session_projection_governance.py       3 passed
ztest/session/test_stream_migration.py                          5 passed
ztest/session/test_session_log.py                               6 passed
Session migration/log/listing targeted Pyright                 0 errors
Model targeted Pyright                                         0 errors
OAuth targeted Pyright                                         0 errors
```

此前实施切片还分别通过 Workflow、Cron、Agent、BackgroundTask、Artifact、Service call、Event、Model、OAuth、Session 和 Product composition 的对应小批 recipe。治理清单保存这些 recipe 与 evidence write set；本报告不以一次资源风险高的全仓测试替代逐域证据。

## 5. 关闭条件判定

| # | 关闭条件 | 判定 |
|---:|---|---|
| 1 | contract、owner、composition、lifecycle、persistence、observability、tests 闭合 | PASS |
| 2 | production consumer 迁移，旧 readers/writers/aliases/exports 退出 | PASS |
| 3 | strict codec 覆盖 unknown/extra/missing/wrong primitive/identity mismatch | PASS |
| 4 | lease/fence/takeover 阻止 stale owner commit | PASS |
| 5 | intent、外部动作、receipt failure 与 `IN_DOUBT` 可确定结算 | PASS |
| 6 | corruption、partial write、mixed generation 与 rollback fail closed | PASS |
| 7 | capacity、retention、scan、compaction 与 storage bounds 有批准值及测试 | PASS |
| 8 | Product composition 是唯一 activation/cutover root | PASS |
| 9 | Artifact/effect/delivery/hold/purge 引用与 retention 闭包完整 | PASS |

## 6. 签收约束

本 PASS 绑定当前 source baseline identity。后续任一 evidence 文件发生变化，严格治理测试都会因 digest 不匹配而失败；届时必须针对变化重新生成 baseline、重跑对应小批 recipe 并重新签收，不能沿用本报告作为永久豁免。
