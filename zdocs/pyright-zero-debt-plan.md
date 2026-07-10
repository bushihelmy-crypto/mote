# Pyright 类型负债清零计划（面向「10 年零债 + strict CI 锁死」）

## 目标（用户明确）
1. **清零全部生产代码类型负债**（不是 baseline 糊弄）。
2. 清零后用 **pyright strict + 生产代码 CI 关卡**锁死，从此新增类型错误无法合并。
3. pre-commit 钩子挂在**内层工作仓 `metagpt/`**（真正的 `.git`）。

## 范围决定（用户 2026-07-10 明确）
- **测试代码（ztest，550 个错误）不修**。
- 连带后果：类型检查关卡**必须排除 ztest**（否则 550 个测试错误令关卡永远红、永远过不了）。
- 终态 = **仅生产代码 strict 零错误**；`pyrightconfig.json` 的 `exclude` 必须含 `metagpt/ztest`。
- 清零目标范围因此从 1051 → **生产 501**。

## 约束（决定节奏）
- **同事正在做 EventBus 的 ABC 契约迁移**（`bus.py` / `common/interface/event_subscriber.py` / `common/events/outcomes.py` / 16 个 subscriber）。
  → 这批文件**冻结不碰**，等同事合并后再处理其类型错误，避免撞车。
- 「与你无关的不管」原则在这里**被用户显式覆盖**：这次任务就是清全部负债。但仍**顺序化**推进，不并行动同事的文件。

## 基线数据（pyright 1.1.411，default 档，2026-07-10）
- **总计 1051 errors**（default 档；strict 档会更多，清完 default 再评估 strict 增量）。
- 分布：**生产 501 / 测试(ztest) 550**。
- 按规则 top：
  | 数量 | 规则 | 性质 |
  |------|------|------|
  | 335 | reportArgumentType | 多为参数类型不符/标注缺失，需逐类判断 |
  | 246 | reportAttributeAccessIssue | 访问不存在属性/动态属性，混真 bug + 标注缺失 |
  | 129 | reportOptionalMemberAccess | **真 None-deref 风险**（生产 61 个），高优先 |
  | 73  | reportIncompatibleMethodOverride | 覆写签名不符，改基类或子类 |
  | 60  | reportCallIssue | 调用参数不符（含我发现的 role_components 那类） |
  | 42  | reportIncompatibleVariableOverride | 变量覆写类型不符 |
  | 38  | reportOperatorIssue | 运算符类型不符 |
  | 26  | reportReturnType | 返回类型不符 |
  | 19  | reportPossiblyUnboundVariable | **真 unbound 风险**（environment 16 个），高优先 |
  | 16  | reportAssignmentType | 赋值类型不符 |
  | 10  | reportMissingImports | **真问题**：见下 |
- **reportMissingImports（10，最先查——可能是真断裂或缺可选依赖）**：
  - `common/observability/langfuse_*.py` — `langfuse` 可选依赖未装（配 stub/忽略）
  - `router/ml/v4_features.py` — `sentence_transformers` 可选依赖
  - `executor/tools/media_pipeline/nodes.py` — `metagpt.utils.workspace_media` **疑似真断裂**
  - `memory/procedural_memory/manager.py` — `metagpt.common.rag.{engines,schema}` **疑似真断裂**（6 处）

## 已完成（本次已做，作为样板）
- `roles/role_components.py`：loop/think 注册表隐式契约收紧为命名类型 `LoopBuilder`/`ThinkBuilder`；
  **pyright 当场抓出 `BuildContext` 从未 import（字符串标注在空引用）——已修**（加进 `component_graph` import）。
  该文件剩 5 个错误均属改动范围外的既有 issue（379/631/727-729），归入下方分阶段清理。

## 分阶段计划（顺序化，避开同事）

### Phase 0 — 立即可做，零撞车（安装 + 轻关卡 + 基线冻结）
- [x] 安装 pyright 1.1.411（已装）。
- [ ] 建 `metagpt/pyrightconfig.json`：**basic 档起步**（不是 strict，先让清理可推进），`exclude` 掉可选依赖缺失的 stub 问题。
- [ ] pre-commit（内层 `metagpt/`）加 pyright 钩子，**只跑改动文件**：新代码立即受约束，不阻塞历史清理。
- [ ] 记录基线快照 `zdocs/pyright-baseline.txt`（1051），作为「只减不增」的对照。

### Phase 1 — 真 bug 优先（生产代码，高价值，与同事文件无交集）
按「最可能是真运行时 bug」排序，逐目录清：
1. **reportMissingImports（10）**：先分清真断裂 vs 可选依赖。真断裂修 import；可选依赖在 config 里 `reportMissingImports` 对特定模块降级或装 stub。
2. **reportPossiblyUnboundVariable 生产 19**（environment 集中 16）：真 unbound 风险，逐个补初始化/分支兜底。
3. **reportOptionalMemberAccess 生产 61**（router 17 / executor 16 / loop 9 / memory 8）：真 None-deref，补 None 检查或收紧上游类型。
   - ⚠️ loop 的 9 个：确认不在同事 ABC 迁移面里再动（loop/react_loop.py 本次读过，应无交集，但动前复查同事进度）。

### Phase 2 — 结构性类型不符（生产，需要判断改基类还是调用点）
4. reportCallIssue 60 / reportArgumentType 生产部分 / reportReturnType 26 / reportAssignmentType 16。
5. reportIncompatibleMethodOverride 73 / reportIncompatibleVariableOverride 42：
   - ⚠️ **override 类问题极可能落在同事的 subscriber/ABC 面上** → 这一批**等同事合并后再做**，否则必撞。
6. reportAttributeAccessIssue 246：量最大，混杂，放结构性问题之后逐目录啃。

### Phase 3 — 测试代码（ztest 550）：**不做**（用户 2026-07-10 明确）
- ztest 从 `pyrightconfig.json` `exclude`，不进关卡、不清理。

### Phase 4 — 升档 strict + 上 CI 生产关卡（终态锁死）
- 生产 default 清零后，切 `pyrightconfig.json` 到 **strict**，评估 strict 新增量，再清一轮。
- strict 也清零后：建 CI workflow 跑 `pyright`（**exclude ztest**），生产**零错误才准合并**。
- pre-commit 钩子从「只跑改动文件」升级为可选的生产全量跑。

## 关键决策点（需你拍板 / 推进中确认）
1. **CI 关卡位置**：外层 `/home/longert/new/MetaGPT/.github/` 还是内层？（记忆：只在 metagpt/ 内操作，但 GitHub Actions 通常认仓库根）
2. **可选依赖**（langfuse / sentence_transformers）：装进环境还是 config 忽略？
3. **strict vs standard 终态档**：strict 最严但增量可能大；standard 是务实折中。清完 default 后按增量数据再定。
4. **同事 ABC 迁移的合并时点**：Phase 2 的 override 批 + Phase 1 的 loop 项都要等这个信号。

## 不做 / 红线
- 不在同事合并前碰 `bus.py`/`event_subscriber.py`/`outcomes.py`/16 subscriber。
- 不 `# type: ignore` 糊弄（那是把负债藏起来，不是清零）——除非是第三方 stub 缺失这类外部不可控项。
- 不动运行时行为：类型修复应是「补标注 / 补 None 检查 / 修真 bug」，不改业务逻辑。每阶段跑 pytest 验证零回归。
