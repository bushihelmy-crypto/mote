# AgentFrame 简历素材

> 面试简历用。本框架（`metagpt.*` 包）由本人独立设计与实现，是一个组合式、事件驱动、分层解耦的 Agent 运行框架。

---

## 一句话定位

> **AgentFrame** —— 把单 Agent 运行抽象成「想(think)/做(act)」对偶的 ReAct 循环，外接多 Agent 运行时、会话持久化、统一 LLM 路由与权限沙箱，全部架在一个零反向依赖的 `common` 基础层之上。

- **技术栈**：Python / asyncio / pydantic v2 / OpenAI & Anthropic SDK / jupyter_client / LightGBM+ONNX / loguru
- **规模**：6 层架构、13 个顶层包、~5.5 万行生产代码 + ~3.4 万行测试

---

## 简历「项目经历」模板（可直接改写）

**AgentFrame · Agent 运行框架（独立设计与实现）**

- **架构**：设计 6 层单向依赖架构（common → context/executor/router/session → parser/think/loop → roles → environment → cli），跨层能力一律经 `common/interface` 的 `@runtime_checkable` Protocol 做**依赖倒置**，底层定接口面、高层注入实现，实现同层零耦合；`common` 作为叶子层永不反向依赖上层。

- **核心抽象**：以**组合优于继承**重构 Role——拆成静态配置(RoleSchema)/可序列化运行态(RoleState)/惰性装配子系统(RoleComponents)三支柱；ReAct 主循环设计为 **role-agnostic**（loop 不持有 Role，仅接收可调用协作者），保证循环逻辑可独立测试与替换。

- **事件总线**：把原本分散的「流式 sink / 会话录制 / Hook 触发」三套机制**收敛为单条两平面异步 EventBus**（控制面按事件名路由折叠 typed `ControlOutcome` 反向影响宿主、观察面按 priority 扇出），让屏幕渲染与磁盘持久化由同一事件流喂养、永不发散。

- **会话持久化**：以**追加式 JSONL（rollout）为崩溃安全真相源**，崩溃后**单次正向重放**即可重建历史+身份（压缩检查点自包含完整历史，无需反扫）；支持 `fork` 派生血缘子会话；文件历史走**内容寻址快照**，代码工作区自动切换 git 底座（独立 bare 仓隔离用户历史）。

- **安全沙箱**：实现**双轴正交权限**（审批轴 + 文件系统沙箱轴）；移植 codex 命令安全分类器做确定性危险命令识别；deny/ask 规则与工具自检**穿透 bypass 模式**作为硬安全红线；工具经**能力注入而非反射**绑定（只拿到白名单方法，永远触不到 Role 内部 / RoleState）。

- **LLM 路由**：统一多 provider 抽象（按**传输层 wire 协议而非模型名**选 client，修复 Claude 经 OpenAI 网关时工具被网关静默丢弃的真实 bug）；提供显式 / task-map / 智能策略三种路由，智能路由含 **LightGBM⊕MLP 集成推理（390 维特征 → R0-R3 路由档）**，artifact 缺失时优雅降级到启发式。

---

## 面试「深挖话题库」（每个都能讲 5-10 分钟）

| 话题 | 亮点关键词 | 体现能力 |
|------|-----------|---------|
| **分层 + Protocol 依赖倒置** | 零反向依赖、接口隔离（同一对象对不同消费方暴露不同侧面） | 架构设计、SOLID |
| **事件总线收敛** | 三套机制 → 一条脊柱、control vs observation 事件 | 重构、抽象收敛 |
| **会话持久化 rollout** | 追加式 JSONL、单次正向重放、fork 血缘、内容寻址 blob/git 双底座 | 系统设计、崩溃安全 |
| **双轴权限沙箱** | 审批×沙箱正交、免 bypass 红线、能力注入而非反射 | 安全意识 |
| **多 Agent 运行时** | 事件驱动调度、LRU 驻留淘汰+磁盘真相、turn 原子邮箱、RAII 资源管理 | 并发、调度 |
| **bggraph 声明式 DAG** | langgraph 风「转移」模型(非静态拓扑)、环/动态路由/AND-join、**LLM-in-the-loop** 中途暂停决策 | 并发编排、创新 |
| **两级历史压缩** | microcompact(无 LLM 原地折叠) + autocompact(LLM 摘要+熔断) | token 成本优化 |
| **wire 协议选 client** | OpenAI vs Anthropic envelope、网关静默丢工具 bug | debug、协议理解 |
| **持久化 PTY/Kernel 工具** | 一次性 Bash + 持久 Terminal(PS1 哨兵法) + Jupyter kernel | 系统编程 |

---

## 关键设计决策速记（口头讲稿要点）

### 1. 为什么用 Protocol 依赖倒置而不是直接 import
- `common` 是叶子层，被所有上层依赖；若它反向 import 上层会产生环。
- 解法：底层定义 `@runtime_checkable` Protocol（接口面），高层在装配期注入具体实现。
- 典型：`executor ↛ session`（经 `FileSnapshotStore`）、`executor ↛ roles`（经 `HookRunner`/`LspNotifier`）、`turn_context ↛ tasks`（经 `EphemeralContextSource`）。
- 附加收益：**接口隔离**——`ContextManager` 同时满足 `MessageStore`（存储面）与 `RequestAssembler`（请求构建面），不同消费方只看到对象的不同侧面，触不到其编排逻辑。

### 2. 事件总线如何收敛三套机制
- 重构前：流式 token 输出、会话录制落盘、Hook 生命周期拦截各有一套 fire-site，散落各处、容易发散。
- 重构后：统一为 `common/events` 的**两平面** `EventBus`——控制面（按事件名路由进桶、桶内 `ControlStage` 串行、折叠 typed `ControlOutcome`）先跑，观察面（按 `priority` 扇出：recorder→logger→…）后跑。
- 平面是**订阅者属性**不是事件标记：暴露 `handle_control` 的（HookSubscriber/PermissionSubscriber/SpawnGate）进控制面，可折叠 typed `ControlOutcome` 反向影响宿主（拒绝/改参/注入上下文/阻止停止）；其余是观察者，返回值被丢弃、无控制订阅者映射的事件 `emit` 返回 `None`。
- 用 ContextVar `_ACTIVE_BUS` 让深层调用点（LLM 流式、文件快照）无需穿线即可发事件。

### 3. 会话持久化为什么是「追加式 + 单次正向重放」
- 追加式 JSONL：崩溃安全、单一真相源、无损坏风险（O_APPEND + flush）。
- 重放只需一次正向扫描：`message`→append、`compacted`→把历史 RESET 为检查点的完整历史（检查点自包含，无需 codex 式反扫）、坏行跳过容错。
- `fork`：纯磁盘操作，replay 父 → 为子写 session_meta（带 parent_session_id 血缘）→ 继承历史逐条 append，子从父最终状态起步且完全独立。
- 快照双底座：默认 sha256 内容寻址 blob；代码工作区自动切 git（独立 bare 仓，隔离用户历史避免被 `git gc` 回收）。
- 关键取舍：rollout 只重建「历史 + 身份」，**不含配置(RoleSchema)**，配置由 caller / environment 的 residency 互补存储。

### 4. 双轴权限为什么正交
- **轴 A 审批**（要不要问用户）与 **轴 B 沙箱**（能不能碰这条 path）相互独立，可任意组合。
- 例：规则放行但沙箱越界 → 升级问用户；bypass 模式放行但沙箱仍拦截。
- **免 bypass 红线**：deny/ask 规则与工具 `check_permissions` 自检穿透 bypass 模式，适合硬安全约束（如 `rm -rf`/fork bomb/`curl|sh`）。
- 命令安全分类器移植自 codex：确定性零依赖，按分隔符切段逐段验白名单，保守策略（不可解析/有副作用 → 非已知安全）。

### 5. 能力注入而非反射
- 工具声明 `requires: tuple[str,...]`（所需 Role 能力方法名）。
- `bind()` 仅注入这些命名属性，且必须落在 `Role.tool_capabilities()` 显式白名单内。
- 结果：工具**永远拿不到** RoleState / memory / Role 本体，安全面被收窄到白名单方法。

### 6. 按 wire 协议而非模型名选 client（修过的真实 bug）
- 症状：Claude 模型走 native 协议时不执行工具，吐自造文本命令当正文。
- 根因：旧实现按模型名选 tool-spec envelope（名含 "claude" → Anthropic 形），但本 fork 除 `api.anthropic.com` 外一律走 OpenAI 兼容 client；Claude 经 OpenAI 网关时被喂 Anthropic 形 tools → 网关静默丢弃 malformed `tools` → 模型收不到工具。
- 修复：envelope 必须匹配**端点 wire 协议(transport)而非 model 名**，改用 `resolve_api_type(llm_config)` 作为判别 key（与选 client 同源）。

---

## 数据流：一次 turn 的运行时（面试可手画）

```
用户输入
  → [environment] 调度器在 turn 边界 drain 邮箱 → msg_buffer
  → [roles] Role.run(): bind_trace + set_bus → 发 SessionStart/UserPromptSubmit 事件 → loop.run()
      → [loop] ReActLoop: observe → think → act → finish
          (think) context_provider.prepare() → PromptBuilder 组装 system(可缓存前缀)+user
                  → user 末尾追加 turn_context bus 的 <system-reminder>(git/token压力/诊断, 不进cache/history)
                  → ContextManager.prepare_request() → microcompact + autocompact
                  → ThinkEngine 后台 Task → LLMRouter 选模型 → BaseLLM.aask_tool(stream)
                     (流式 token → 屏幕 / TokenUsage → 计费 / 透明恢复 COMPRESS·ROTATE·FALLBACK)
          (act)   CommandChannel.iter_commands() → 统一命令 IR
                  → ToolExecutor.run_command() 单咽喉: 校验→PreToolUse→权限闸(双轴)→恢复循环
                     →tool.call()→结果归一→PostToolUse→FileMutated(LSP同步/watcher抑制)→结果限流
          (finish) is_terminal 判定(空 tool_calls / <end>) → 收尾产出回复
  → [roles] turn 边界: 发 TurnEndEvent(触发 Stop hook) → 回复打标签
  → [environment] 回复发布到 env
```

两条横切脊柱：**事件总线**（同一事件流喂屏幕+磁盘，不发散）+ **持久化**（每条消息/压缩检查点/文件 before-image 追加进 rollout.jsonl）。

---

## 分层依赖（背下来）

```
common  ◀──  context / executor / router / session  ◀──  parser / think / loop  ◀──  roles  ◀──  environment  ◀──  cli
```

- `common`：叶子层，只依赖标准库 + 第三方，永不反向 import 上层。
- 低层要用高层能力 → 先在 `common/interface/` 定 Protocol，高层装配期注入。
- `environment → session` 可直接 import（无环）；`environment → roles` 必须惰性 import 打破环。

---

## 准备追问的高频问题

1. **"common 怎么保证不反向依赖？"** → Protocol 依赖倒置 + interface 是 LEAF 包（只 import typing）。
2. **"会话很长 token 爆了怎么办？"** → 两级压缩：microcompact 无 LLM 原地折叠旧工具结果，autocompact LLM 摘要保尾部 + 熔断防雪崩。
3. **"多个 Agent 同时跑内存怎么控？"** → LRU 驻留淘汰 idle agent 落盘，按需 rehydrate；历史走 rollout（真相源）、配置+运行态走 residency，互补不双写。
4. **"工具乱删文件怎么防？"** → 双轴权限 + 命令安全分类器 + 沙箱 path 校验 + 能力注入收窄安全面 + 改盘前 before-image 快照可回滚。
5. **"为什么 loop 不持有 Role？"** → role-agnostic：loop 只接收可调用协作者，循环逻辑可独立测试/替换，Role 退化为装配器+消息发布者。

---

# 成长故事线：底座 → 原创范式（面试核心叙事）

> 这是简历最有价值的部分——它把"我复现了一个框架"升级成"我搭好了底座，正在上面做一个有明确 thesis 的原创范式"。

## 一、定位话术（一句话讲清"为什么先复现"）

> "这个框架我没追求全原创。我先系统性研究了 codex / claude-code 这类工业级 Agent 实现，把它们的设计意图吃透，**复现 + 收敛 + 适配成一套我完全掌控、分层清晰的底座**——fork、分页式驻留、工具隔离、无损持久化这些苦活全做完了。
>
> 但我发现它们和所有主流框架一样，最终都靠**有损压缩**对付 context 爆炸。我的下一步原创是**用『管理/执行分离 + 分页记忆』彻底取代压缩**：让 Parent 只做管理、Fork 只做执行，context 通过无损调入调出而非压缩流转。这是我做 Agent Swarm 的基础。"

**关键**：能力是「会读顶级开源源码并消化落地」（比写功能稀缺得多），定位是「平台底座 + 创新发射台」，而非 claim 原创。

## 二、原创范式：管理/执行分离

**核心命题**：Agent 不是执行者，是管理者；执行交给 Fork。**管理/执行分离 = 控制平面/数据平面分离。**

- **认知资源论**：管理者不能同时是执行者——执行会吞噬管理所需的认知资源（注意力）。Fork 让 Agent 永远站在执行之外，专注观察、管理、决策。
- **Fork 三位一体**：一个 Fork 同时是 **执行单元**（隔离，不污染 Parent）+ **调度单元**（可并行）+ **记忆单元**（一次 Fork = 一条完整情景记忆）。
- **工具物理隔离**：Parent 用管理工具集、Fork 用业务工具集，API 层硬隔离（非 prompt 约束）。每个 context 工具集生命周期内固定 → prefix 永不失效，天然 cache 友好。
- **无状态 Parent（马尔可夫性）**：每次唤起都是全新的，靠 memory 工具恢复上下文；Fork 前框架自动清理管理阶段的 think/act/observe。

**Fork 完整生命周期**：
```
Parent 调 fork
  → 框架切换「状态 + 工具集」(管理工具 → 业务工具)，在 user prompt 中提示任务 + 上下文
  → Fork 独立 React 执行（隔离，不污染 Parent）
  → 完成时调 summary 工具 → 把整段执行总结成结构化记忆单元
  → (并行 fork 全部完成) → 各自上下文按序拼接 → 回到 Parent 继续管理
```
（按序拼接对应 cache/attention 设计：最相关的留底部离 query 近，prefix 永不动。）

## 三、分页记忆：用调入/调出替代压缩

**核心理念**：不需要有损压缩，用 OS 虚拟内存式的分页替代。

| 旧范式（压缩派） | 新范式（分页派） |
|------------------|------------------|
| 压缩上下文（有损） | Fork 隔离（细节不进 Parent） |
| 压缩历史（有损） | page_out 到磁盘（无损） |
| 压缩记忆（有损） | collapse = 活动上下文换摘要，完整单元仍在盘（无损） |

**记忆管理工具**：`memory.search` / `page_in` / `page_out` / `collapse` / `list`。

**零有损（最硬的卖点）**：
> 本架构没有任何真正有损的操作。`page_out` / `collapse` 都只改变信息在「活动上下文」里的呈现密度，不销毁任何信息——所有记忆单元全部结构化落盘，随时可 `page_in`。这是它和所有「压缩派」框架的根本分野：压缩真的丢字节，分页只是换存储位置。

**与现有框架的取舍对比**（面试会被追问"值得吗"）：压缩自动且几乎免费；分页要 Parent 花真实 LLM turn 管理记忆——**用算力换保真度**，需准备数量级直觉。

## 四、三层记忆：从同一真相源派生的三种投影

三层不是平行分类，是一条**派生链**（与框架核心哲学"rollout 是真相源、其余皆派生"自我一致）：

```
情景记忆 (episodic) = fork 单元 = 一段完整经历        ← 真相源（全部落盘）
        │
        ├─ 提炼(去情境化) → 语义记忆 (semantic) = 可脱离具体情况的经验/知识
        │
        └─ 抽取(固化路径) → 程序记忆 (procedural) = 免 LLM 直接调工具的"肌肉记忆"
```

**情景记忆单元 schema**：
```
元数据:   session_id · 时间 · 地点 · 隶属 userquery
单元信息: { 题目 · 背景 · 执行 · 结果 · 反思 }
```
（`反思` 字段 = 语义/程序记忆的提炼种子，派生链从 schema 层就埋好钩子。）

**两个锋利的 reframe**：
1. **程序记忆 = 给 Agent 行为做 JIT 编译**。把高频情景里确定性的工具调用序列抽取固化成"肌肉记忆"，下次同类触发**直接执行、跳过 think**。等价于：LLM 推理是"解释执行"，程序记忆是"编译后的热路径"。价值主张：省 LLM 调用 = 省延迟 + 省钱。
2. **三层派生顺手让 collapse 彻底无损**：情景的价值在 collapse 前已被提炼到语义/程序层——细节丢了也无妨。**就像人脑**：记不得三年前 debug 的每一步（情景遗忘），但学会了"这类报错先查环境变量"（语义）和"闭眼敲出修复命令"（程序）。

**Multi-Agent = 记忆分治而非劳动分工**："专家之所以是专家，是因为他只记一个领域的东西，记忆定义了身份。" 单 agent + fork 搞定 99% 场景，multi-agent 只在"不同领域记忆需各自专精"时不可替代。

## 五、底座 ↔ 原创 能力映射（证明"苦活已做完，差临门一脚"）

| 原创范式需要的能力 | 现有框架已实现的底座 | 关系 |
|--------------------|----------------------|------|
| **Fork 执行原语** | `session/fork.py` 血缘 fork + `Agent` 工具孵化子 agent + environment 多 agent 运行时 | 已有 fork，差「工具物理隔离 + 结果回流」语义 |
| **page_out / page_in** | `environment/residency.py` LRU 驻留淘汰落盘 + `session/replay.py` 重放调回 | **这就是分页雏形**：淘汰=page_out、replay=page_in，差暴露成 Agent 可调工具 |
| **工具物理隔离** | `executor` 能力注入而非反射 + `RoleSchema.tools` per-session 固定 | 隔离基建已在，差「管理工具集 vs 业务工具集」二分 |
| **summary 工具** | router task_map 的 `SUMMARY_TASK`（已有 `summary_llm` 通道）+ autocompact 摘要 | 复用 |
| **状态 + 工具切换** | `session.fork` + `RoleSchema.tools` 固定 + 能力注入 | 复用 |
| **情景 episodic** | rollout.jsonl（真相源）+ 内容寻址持久化 | fork 单元天然是情景，差更高层「结构化记忆单元」schema |
| **语义 semantic** | `context/skills/` 可注入指令包 + `memory/semantic_memory` 占位 | 语义记忆可**生成 skill** |
| **程序 procedural** | `executor/tasks/bggraph/` 声明式 DAG（已能无 LLM 跑工具序列）+ `memory/procedural_memory` 占位 | **程序记忆 = 从情景自动编译出的 bggraph** |
| **要被取代的旧范式** | 两级压缩（microcompact / autocompact） | **正是分页记忆要替换的对象** |

**金句**：bggraph 现在是"手写的工具流水线"，下一步会变成"从情景记忆自动编译出来的肌肉记忆"——底座和原创无缝咬合。

## 六、终极目标：Agent Swarm

> Swarm 的真正瓶颈不是"怎么启动更多 agent"，是"怎么让每个 agent 长时间运行后还保持质量"。

本架构的能力来源：Fork 隔离（单 agent 无限运行不退化）+ 无损调出（经验永不丢失、越跑越强）+ 记忆分治（多 agent 各自专精）+ 无状态 Parent（随时唤起/消失零负担）+ 任务粒度记忆（千个 agent 共存不爆）。

## 七、面试防崩：原创范式的硬问题（提前想清楚）

1. **程序记忆的失效与守卫**（最危险）：跳过 LLM 的工具宏，环境变了（文件移位/状态不同）会闯祸 → 需前置条件校验 + 失配回退 LLM（经典 cache 失效问题）。
2. **`memory.search` 的语义**：无状态 Parent 失忆，凭什么知道该 search 什么？query 从哪来？（关键词/语义向量/任务图谱？）——这是分页设计最薄的一环。
3. **语义提炼的触发与过度泛化**：何时提炼、谁判断"去情境化"提对了？提错经验比没有更糟。
4. **Fork 结果回流的 context 尖峰**：大 fork 返回巨上下文，page_out 前有瞬时峰值；summary/full 选择是泄压阀。
5. **拆分粒度押在 Parent 判断力上**：强依赖模型能力。
6. **分页 vs 压缩的成本账**：用真实 LLM turn 换保真度，准备数量级直觉。
