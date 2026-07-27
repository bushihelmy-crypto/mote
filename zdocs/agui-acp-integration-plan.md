# 点五 — AG-UI (CopilotKit) + ACP 前端集成落地计划

> 范围：**含 ACP 全量**。HTTP 栈：**零新依赖**（复用已在 `pyproject.toml` 的 `aiohttp`
> 承载 SSE；复用 `roles/lsp/jsonrpc.py` 承载 ACP 的 stdio JSON-RPC）。
>
> 目标：10 年 0 负债、可扩展、优雅。**协议脊柱只有一个（`ViewEvent`），永不为某个前端加
> `if consumer==` 分支。** AG-UI 是开放标准（不锁 CopilotKit）：实现 AG-UI 即兼容 CopilotKit
> React SDK + 任意 AG-UI 客户端。

---

## 一、现状契约（已核实，逐字使用）

### 两个对称半边
mote 的每个 host 集成有**两半**，terminal 已同时具备：
- **输出半 = `Consumer`**（`cli/contracts/interface/consumer.py`）：`capabilities` + `async handle(ev)` + `async aclose()`。`BaseConsumer` 提供 `on_<kind>` 分派。ViewEvent → wire。
- **输入半 = `InputPort`**（`cli/contracts/interface/ports.py`）：`ask/ask_questions/decide_approval/signal_interrupt/submit_steer`；`InteractivePort` 加 `read_turn()`。wire → 引擎。

### 事件脊柱（`cli/contracts/view/events.py`）
`ViewEvent(kind, scope)` 子类（逐字字段见 events.py）：
`MessageBlockStarted/Delta/Completed`、`ReasoningDelta`、`ToolCallStarted`（tool_name/title/headline/body/lexer/tool_use_id）、`ToolCallCompleted`（ok/summary/result_kind∈{plain,diff,table,media}/detail/error_*/retryable/recovery）、`MediaBlock`、`FileDiffBlock`（path/old/new）、`TaskProgress`、`ActivityStarted/Completed`、`QuestionAsked`、`ApprovalRequested`（approval_id/action/args_preview/risk）+ `ApprovalDecision`（approval_id/outcome/edited_args，走 InputPort 上行）、`Notice/ErrorRaised/RetryStatus/SystemReminder/ConversationCompacted/TranscriptCleared`、`SessionListShown`、`UsageUpdated`。

### 降级 & 扇出
- `Capabilities(streaming/markdown/syntax_highlight/interactive/rich_panels/images)`。
- `CapabilityAdapter.adapt(ev)->List[ev]`：`streaming=False` 时缓冲 delta→单个 `MessageBlockCompleted`；`syntax_highlight=False` 时清 lexer。**per-consumer 降级，projector 无感**。
- `BaseProjector(consumers, projector=ViewProjector())`：订阅 role 的 `AgentEvent` bus，`project()` fold 成 ViewEvent，逐 consumer 过 adapter 后 `handle`。scope 自动前传。

### 单会话 vs 多会话（**关键 gap**）
- `cli/app.py build_app`：`config→control→driver→projector→[consumer]`，**单会话**（一个 stdin port + 一个 `driver.run()` while 循环）。
- 网络 server 是**多会话**：一个进程并发服务 N 条连接/thread，每条要自己的 `{port, consumer, projector}`，复用同一 `control` 构建路径。
- ⇒ 需要一个薄的**多会话 server 层**（在 driver 之上），不改核心。

### 可复用基建
- `aiohttp`（已依赖）→ SSE（chunked `text/event-stream`）。
- `roles/lsp/jsonrpc.py` `JsonRpcEndpoint`（Content-Length 帧 + request/notify/dispatch）→ ACP stdio。
- `cli/proto/` 机器协议 twin 的 stub + ARCHITECTURE §6 映射表（app-server），ACP 与之同构。
- `PortHumanChannel`（`cli/io/human_channel.py`）：`_prompt_lock` 串行化并发 prompt——网络端并发 turn 也需要。

---

## 二、架构（一脊柱，N transport）

```
             ┌── ViewEvent (中立脊柱，已存在)
Role/loop ──┤
             └── BaseProjector + CapabilityAdapter (已存在，降级)
                     │  每连接一个 projector 实例，订阅该 role 的 bus
   ┌─────────────────┼──────────────┬────────────────┐
 terminal          agui             acp            (feishu/web 后续复用同层)
 (已实现)      Consumer+Port     Consumer+Port
                     │                │
              aiohttp SSE       stdio JSON-RPC
              /run /info        (reuse jsonrpc.py)
                     └──── 共享纯映射器 ────┘
                       cli/consumers/_wire/agui.py (ViewEvent→AG-UI dict)
                       cli/consumers/_wire/acp.py  (ViewEvent→ACP item)
```

**核心资产 = 纯映射器**（无 transport、可单测）：`ViewEvent → 目标协议 dict`。AG-UI 和 ACP 各一个映射函数表，共享同一套 ViewEvent 输入。

---

## 三、AG-UI 协议对齐（CopilotKit v2，纯 SSE，不碰 GraphQL）

端点（`POST /agent/:agentId/run`→SSE、`/info` 发现、`/connect` load-state、`/stop/:threadId`）。
`threadId` ↔ mote `session_id`。**每个 POST /run 驱动一个 turn**（AG-UI 是无状态请求 + 服务端 thread 态模型；session 常驻服务端注册表，请求注入新 user message → 跑一 turn → 该请求的 SSE 流出本 turn 事件 → `RUN_FINISHED` 关流）。

映射表（`ViewEvent → AG-UI event`）：

| ViewEvent | AG-UI |
|---|---|
| turn 开始（driver 注入） | `RUN_STARTED{threadId,runId}` |
| `MessageBlockStarted` | `TEXT_MESSAGE_START{messageId}` |
| `MessageBlockDelta` | `TEXT_MESSAGE_CONTENT{messageId,delta}` |
| `MessageBlockCompleted` | `TEXT_MESSAGE_END{messageId}` |
| `ReasoningDelta` | `THINKING`/reasoning delta |
| `ToolCallStarted` | `TOOL_CALL_START{toolCallId,toolName}` + `TOOL_CALL_ARGS` |
| `ToolCallCompleted` | `TOOL_CALL_END` + `TOOL_CALL_RESULT{result}` |
| `TaskProgress`/`ActivityStarted/Completed` | `STEP_STARTED/STEP_FINISHED` |
| `UsageUpdated` | `STATE_SNAPSHOT{usage}` |
| `ApprovalRequested` | `CUSTOM_EVENT`(approval) 或 tool `requiresApproval` |
| `ErrorRaised` | `RUN_ERROR` |
| turn 结束 | `RUN_FINISHED{runId}` |

HITL：`ApprovalRequested`→前端提示→回传（下一个 /run 或专用回传端点）→`AguiPort.decide_approval`→现有 `ApprovalDecision` 闭环。

---

## 四、ACP 协议对齐（Zed 编辑器，stdio JSON-RPC）

持久连接 + 方法：session load/resume/fork（mote 已有 session resume/fork）、prompt turn、streaming、tool_call（kind/locations）、edit_approval（diff）。复用 `roles/lsp/jsonrpc.py` 帧。

映射：`ViewEvent`→ACP item（text chunk / thinking chunk / tool_call{kind,locations} / tool_call_update）。`FileDiffBlock(path,old,new)`→ACP `edit_approval`（天然契合）。`ApprovalRequested`→`ToolCallApprovalRequest`。session/turn→ACP session 生命周期。

---

## 五、实施阶段（每阶段可独立测试、可停）

### Phase 0 — 多会话 server 脊柱（使能层，AG-UI/ACP/web/feishu 共用）
**新增** `cli/serving/`：
- `session_registry.py`：`SessionRegistry`——按 `session_id`(threadId) 常驻 `{control, role}`；`get_or_create(session_id)` / `evict`。复用 `backend.build_control` + `role_factory`（与 `build_app` 同路径，抽公共构建函数避免重复）。
- `connection_scope.py`：`ConnectionScope`——每连接/每 turn 创建 `{port, consumer, BaseProjector(ViewProjector())}`，订阅该 role bus，跑一 turn，解订阅，`aclose`。这是 driver.run() 单会话循环的多会话对偶。
- **零核心改动**：只在 driver 之上加编排；`build_app` 抽出的公共构建函数被单/多会话共用。

**测试**：两个并发假连接各自独立事件流不串扰；session 常驻跨 turn。

### Phase 1 — ViewEvent→AG-UI 纯映射器（核心资产）
**新增** `cli/consumers/_wire/agui.py`：`to_agui_event(ev: ViewEvent, *, thread_id, run_id, message_id_of) -> Optional[dict]`（纯函数）。id 关联策略（messageId/toolCallId ← tool_use_id / block 计数）。
**测试**：每类 ViewEvent → 期望 AG-UI dict；单行 JSON、无换行；未知 kind→None（老前端忽略）。

### Phase 2 — AG-UI SSE transport + Consumer + Port
**新增** `cli/consumers/agui/`：
- `consumer.py`：`AguiConsumer(BaseConsumer)`，`capabilities=Capabilities(streaming=True,markdown=True,interactive=True,images=True)`；`on_<kind>`→`to_agui_event`→写 SSE 帧。`@register_consumer("agui", capabilities=...)`。
- `port.py`：`AguiPort(InputPort)`——`read_turn` 取本请求注入的 user message；`ask/decide_approval` 通过回传通道（future+approval_id 关联，复用 `_prompt_lock` 模式）。
- `server.py`：`aiohttp.web` app——`/agent/{id}/run`(POST→SSE)、`/info`、`/connect`、`/stop/{tid}`。每请求：registry.get_or_create(threadId)→ConnectionScope→注入消息→跑 turn→流 SSE→RUN_FINISHED。
- `__main__.py` / CLI flag：`mote serve --agui --port 8xxx`。

**安全**：网络暴露 endpoint **必须**默认带 auth（token/bearer）——否则等于开放未鉴权 agent 执行面。默认 bind `127.0.0.1`，token 必填或显式 `--insecure`。

**测试**：起 aiohttp test server，POST /run → 断言 SSE 帧序列（RUN_STARTED…TEXT_*…TOOL_*…RUN_FINISHED）；/info 列出 agent。

### Phase 3 — HITL / approval / question 回传闭环
`ApprovalRequested`→AG-UI approval 事件；前端回传→`AguiPort.decide_approval`→`ApprovalDecision`。`QuestionAsked`同理。
**测试**：approval 往返、edited_args 透传、reject 路径。

### Phase 4 — ACP consumer（复用 Phase 1 思想，换 transport）
**新增** `cli/consumers/_wire/acp.py`（ViewEvent→ACP item 纯映射）+ `cli/consumers/acp/{consumer,port,server}.py`（复用 `jsonrpc.py` + ConnectionScope）。`product/integrations/acp/registry`（agent.json/icon）对标 hermes。
**测试**：session load/prompt/fork；tool_call kind/locations；edit_approval diff 往返。

---

## 六、零负债守则（贯穿所有阶段）
1. 协议脊柱唯一（ViewEvent）；映射器纯函数、可单测、无 transport 耦合。
2. Consumer/Port 是 Protocol（鸭子类型），叶子层，不侵入核心。
3. `@register_consumer` 自注册：新 transport = 新目录 + 装饰器，`build_consumers` 零改。
4. 多会话层在 driver 之上，`build_app` 单会话路径不变（抽公共构建函数共用）。
5. 零新依赖（aiohttp/jsonrpc.py 复用）；不碰 CopilotKit 私有 API（走 AG-UI 开放标准）；不引 GraphQL。
6. 网络面默认 auth + 本地 bind——发布安全底线。

---

## 七、文件清单（新增，无删改核心）
```
cli/serving/session_registry.py        (Phase0)
cli/serving/connection_scope.py        (Phase0)
cli/app.py                             (Phase0: 抽公共 build 函数, 单会话路径不变)
cli/consumers/_wire/agui.py            (Phase1 核心资产)
cli/consumers/agui/consumer.py         (Phase2)
cli/consumers/agui/port.py             (Phase2/3)
cli/consumers/agui/server.py           (Phase2)
cli/consumers/_wire/acp.py             (Phase4)
cli/consumers/acp/{consumer,port,server}.py  (Phase4)
product/integrations/acp/registry/{agent.json,icon.svg}     (Phase4)
ztest/cli/serving/…                    (每阶段)
ztest/cli/consumers/agui/…
ztest/cli/consumers/acp/…
```

## 八、里程碑顺序
Phase0（使能）→ Phase1（映射器，核心资产，可脱离 transport 交付）→ Phase2（AG-UI 跑通只读流）→
Phase3（HITL 闭环）→ Phase4（ACP，复用一切）。每阶段独立可测可停。
