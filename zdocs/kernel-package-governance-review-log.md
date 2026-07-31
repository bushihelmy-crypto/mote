# Kernel 分包治理评审记录

本文件保存 [`kernel-package-governance-plan.md`](./kernel-package-governance-plan.md) 的评审历史。主计划只保留当前有效约束和决策。

## v2：能力 owner 与依赖矩阵

- 拆解混合四层策略的 `AgentSpec`，不预建贫血 `kernel.agent`。
- parser 改为 commands 语义边界，采用 `InferenceResult -> ModelTurn`。
- Product 模型任务和 Prompt 文案回到实际 owner。
- 采用封闭依赖矩阵、API 稳定性清单与永久架构门禁。
- contracts 只保存跨边界 DTO，不保存共享 schema 编译算法。

## v3：Tool、Output 与恢复边界

- ToolCatalog 协议中立；provider lifecycle、permission 和动态物化归 Runtime。
- Output evaluation 归 Kernel，delivery/publication 归 Runtime。
- Kernel telemetry 不暴露 logger；output snapshot 累积状态归 Output。
- 删除 FlowServices service locator，按 node 注入窄依赖。
- 首版曾提出 History/Output/Inference 三个独立 transaction port，后在 v6 被统一事务边界取代。

## v4：兼容表面与可实施迁移

- 旧 manifest wire 永久可读，Runtime replay 投影到新内存模型。
- Product facade 保留 stable Toolset 用户 API，旧模型不进入 Kernel。
- Tool 迁移采用纵向切片，消费方切完后原子删除旧真相源。
- CommandCodec 拆为 decoder、tool projector、history projector。
- 增加 API TOML、统一 import scanner、wheel smoke test 和可计算迁移指标。

## v5：第四轮评审的先行修订

- 明确 Kernel 是 IO 实现无关，而不是运行时不调用 IO。
- 引入 `ModelInferencePort`，禁止 Kernel 遍历 `ModelRoute` service locator。
- 增加 PromptSection/ProtocolVocabulary 与唯一 PromptAssembler。
- 引入 ToolBindingSnapshot、事件三平面和 SLO owner 审计草案。
- 将“Phase 独立提交”修正为提交可构建、Phase 可验收/回滚。

## v6：故障一致性闭合

- 拒绝 History/Output/Inference 三个独立 mutation port；Kernel 只看到 run-scoped `ExecutionTransactionPort`，Runtime 内部仍可拆 store/journal。
- 所有 mutation 明确 run/attempt/operation identity、fencing、expected revision、幂等、冲突与取消语义。
- AcceptedOutput 增加 validator/migration provenance，并采用 stage → atomic terminal commit；stage 后禁止重新 evaluation。
- 第三协议的特有实现集中在 commands，但允许其他层增加稳定 identity 与装配映射，删除无法兑现的“只改 commands”承诺。
- `ProtocolContext` 拆为 Decode/ToolProjection/HistoryProjection 三个窄 context。
- Runtime 保留 pinned BoundToolRegistry；Kernel 只持不可变 snapshot identity/catalog，并通过 `ToolExecutionPort` 按 revision dispatch。
- 固化 RunEvent、ObservationEvent、SessionEvent 的可靠性、背压、恢复和转换 owner；ProtocolIssue 不得降级为 telemetry。
- 数值 SLO 按硬安全上限、Runtime 资源限制、Product 性能目标和运维告警重新归属。
- Product legacy adapter 增加单向依赖和对象模型隔离约束。

## v7：能力解析与诊断语义闭合

- `ModelInferencePort` 改为 resolve/infer 两阶段；`ResolvedInferenceTarget` 只暴露请求定稿所需的稳定 capability 与 lease identity。
- target capability 变化的 failover 必须重新物化 ToolBindingSnapshot、协议投影、output binding 和完整请求。
- DecodeResult 分离 ProtocolIssue 与 ObservationDiagnostic；前者必须先产生明确语义决策，不能直接降级为 telemetry。
- SessionEvent 是业务 replay 真相；TransactionRecord 仅是未决原子提交的基础设施恢复真相，AcceptedOutput 是不可变 payload。
- commands 只消费 contracts MaterializedToolCatalog，删除预留的 commands → kernel.tools 依赖边。
- 新架构公共 API 从 provisional 起步；补充工具 snapshot retention/GC 与 PromptSection namespaced owner 约束。

## v8：Phase 0 批准与强制产物

- 总体架构评审通过，允许启动 Phase 0；Phase 1 代码迁移仍以 Phase 0 验收为门禁。
- InferenceIntent 增加 resolve 前的语义能力需求，禁止先选择不满足需求的 target 再碰运气降级。
- ResolvedInferenceTarget 增加 capability fingerprint 与 projection compatibility key；复用请求资产必须显式证明兼容。
- target lease、inference attempt fencing、run transaction fencing 分域；模型调用未知结果与重复付费窗口进入 recovery matrix。
- protocol/vocabulary/tool projection/PromptSection set fingerprints 同时进入最终请求和 checkpoint。
- Phase 0 增加 contracts DTO 晋升清单，逐项证明跨边界必要性和版本 owner。
- 主计划增加 D21–D25，完成标准允许未过期且已批准的 provisional 跨层入口。

## Finding 追踪

| Plan version | Findings | Disposition | Current plan sections |
| --- | --- | --- | --- |
| v2 | 1–8 | accepted / modified | 2.3、3.1–3.6、4.3–4.5、Phase 0 |
| v3 | 9–20 | accepted / modified | 2.3、3.2–3.9、5.3、Phase 0–5 |
| v4 | 21–35 | accepted / modified | 4.4–4.5、6、8、11 |
| v6 | 36–53 | accepted / modified | 2.3、3.5–3.9、5.3、Phase 0 |
| v7 | 54–60 | accepted / modified | 2.3、3.5、3.8、4.3–4.4、5.3、Phase 0/4 |
| v8 | 61–67 | accepted / modified | 2.3、3.5、5.3、Phase 0、D21–D25、11 |

## 批判式吸收原则

- 评审提出的目录建议只有在 owner、状态真相源、依赖矩阵和故障语义同时闭合时才进入主计划。
- 历史方案不因曾被记录而继续兼容；例如三个 mutation port 已被统一事务边界明确取代。
- 源码兼容、顶级用户 API 兼容和持久化 wire 兼容分别决策，不能用“无残渣”或“保持兼容”互相覆盖。
- 实现期若推翻任一已决策事项，必须通过新 ADR 同步更新主计划决策索引、API/依赖机器清单及相关 recovery/compatibility fixtures，而不是在代码中形成事实例外。
