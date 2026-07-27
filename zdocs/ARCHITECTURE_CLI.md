# CLI / 展示框架架构设计

> 本文是 `mote.product.cli` 的**目标架构**：一个面向未来十年的、充分解耦、可扩展、可维护的**多消费者**展示框架。它**不重造** Mote 已有的事件脊柱（`common/events/`），而是在其之上补齐缺失的「投影层」与「消费层」。
>
> **一句话纲领**：核心只发射「发生了什么」（`AgentEvent`，**单一真相源**）；两个投影器把它折叠成「人该看什么」（`ViewEvent`）与「机器该收什么」（`ServerNotification`）；任意数量的消费者各自决定「怎么投递」。**人与机器是同一真相源的对称下游，谁都没有特权路径。**
>
> **诚实约定**：全文凡涉及具体行为的论断，标 ★实证（有 Mote 代码直接支撑）或 ☆推断（据 codex / symphony 文档与代码形态推断，需实现期对齐）。本文是设计，不是已实现的事实。

---

## 0. 设计动机与现状

### 0.1 现状：一个上帝对象

当前 `cli/repl.py` 的 `Repl`（~700 行）同时握着六类互不相关的职责：

```
┌────────────────────────────────────────────────┐
│                Repl  (god object)                │
├────────────────────────────────────────────────┤
│ ① stdin 读取 + SIGINT 两段式状态机                 │
│ ② 流式 token 输出 (_stream_sink)                  │
│ ③ rich 渲染委托 (_renderer.*)                     │
│ ④ 直接读 role.state.context.messages 取回复        │  ← 特权路径，必须砍掉
│ ⑤ agent 生命周期 (adopt/new/fork/switch/resume)    │
│ ⑥ AskUserQuestion 的 stdin 回传纠缠                │
└────────────────────────────────────────────────┘
```

而 `cli/render.py` 的 `ConsoleRenderer` 是**终端专属**的 rich 渲染器，把**所有格式化决策**焊死在终端实现里：`_HEADLINE_ARG`（工具标题取哪个参数）、`_BODY`、`_EXT_LEXER`、`_MAX_BODY_LINES`、markdown 分块边界 `_split_committable`……★实证

### 0.2 问题：两类复制

1. **格式化复制 N 份**：要新增 Web / 飞书，每个前端都得重新解读裸 `PreToolUseEvent.tool_input`，重新推导标题、重新分块、重新截断。格式化逻辑会复制 N 份。
2. **真相源被旁路**：`Repl` 直接读 `context.messages` 取 assistant 回复（职责④）。这是一条**特权路径**——它让「人类终端」凌驾于事件流之上，于是任何**非终端**消费者（机器、Web）都拿不到对称的待遇。

```
   ✗ 反模式：各前端解读裸事件 + 终端走特权路径
   AgentEvent ──┬─▶ Terminal：自推导 + 偷读 context.messages（特权）
                ├─▶ Web：自推导（重复）
                └─▶ 飞书：自推导（重复）

   ✓ 目标：单一真相源 → 投影一次 → 消费者对称共享
   AgentEvent ─▶ Projector（折叠/格式化一次）─▶ 契约 ──┬─▶ Terminal
                                                      ├─▶ Web
                                                      ├─▶ 飞书
                                                      └─▶ 机器（App-Server）
```

### 0.3 既有的好底子：事件脊柱（不动）

Mote 的 `common/events/` 已经实现了多消费者解耦里**最难的那一半**，本设计完整复用、不做改动：

| 组件 | 文件 | 提供的能力 |
|------|------|-----------|
| `EventBus` | `common/events/bus.py` | **两平面分发**：observation 平面**扇出**（任意多订阅者，互不干扰）/ control 平面**折叠**（收敛到单序列）；按 `DeliveryPolicy` 分级（mirror 尽力 + 超时；durable 必达）；单订阅者异常/超时**不拖垮主干** ★实证 |
| `AgentEvent` | `common/events/types.py` | **开放的、带 `name` 判别符的标签联合**；新增事件是纯叶子扩展，永不破坏旧订阅者 ★实证 |
| 订阅者协议 | `common/interface/event_subscriber.py` | observation / control 两平面；影响力由「注册在哪个平面」结构性决定，而非数据 flag ★实证 |

> **本设计最重要的一个认知**：人机解耦不需要新机制——`EventBus` 的「observation 扇出 / control 折叠」两平面**恰好**就是答案（见 §5）。observation 扇出 = 人和机器可同时**观察**；control 折叠 = 驱动权必须**仲裁**。这条结构性事实贯穿全文。

### 0.4 三个参考项目的收敛教训

| 项目 | 关键模式 | 本设计的吸收 / 扬弃 |
|------|---------|------------------|
| **codex** | 两层协议：细粒度 `EventMsg` → 粗粒度 `ThreadItem`（`event_mapping.rs` 居中翻译）；**每个前端都是 app-server 客户端**，无特权路径 | 吸收「投影器居中翻译」「无特权路径」。**扬弃**「连人用 TUI 也走 app-server」的极致统一——那会让人类表现污染机器协议，或被迫加进程层（见 §4 为何**两个协议**而非一个） |
| **流式 Agent CLI** | 核心是 async generator 产出类型化 message union；单个解码函数坐在流与渲染 sink 之间 | 投影器是唯一的解码/折叠点；消费者只认折叠后的契约 |
| **hermes-agent** | 表现层专用事件词汇 + `BasePlatformAdapter`（可「吃掉」渲染不了的事件）+ 单例 registry 自注册 | 消费者声明 `capabilities`、可吃掉/降级事件；registry 自注册，新通道零核心改动 |

---

## 1. 三轴正交：本设计的骨架

整套架构建立在**三个彼此正交的轴**上。把它们分开，是全文的根基——混淆任意两轴，就会滑向「上帝对象」或「伪二分端点」。

```
                    ┌─────────────────────────────────────┐
轴一·真相源（唯一）   │   AgentEvent  在 per-role EventBus 上    │  谁都不许绕过它直读 context
                    └──────────────────┬──────────────────┘
                                       │ observation 平面扇出
                       ┌───────────────┴───────────────┐
轴二·协议（恰好两个）  ViewProjector                AppServerProjector
                  （折叠成人类表现）              （折叠成机器协议）
                       │                                │
                  ViewEvent                      ServerNotification
                       │                                │
轴三·宿主（自由组合）   └──────┬──────┬──────┐     ┌──────┴──────┐
                       Terminal  Web  飞书     Symphony   另一个 agent
```

| 轴 | 取值 | 互斥性 | 误解（要避免） |
|----|------|--------|--------------|
| **轴一·真相源** | 唯一的 `AgentEvent` 流 | 单一，无旁路 | 让终端偷读 `context.messages`（今天的职责④） |
| **轴二·协议** | 恰好两个：`ViewEvent`(人) / `ServerNotification`(机器) | **永不合并** | 把两者并成一个「通用协议」（人类表现会污染机器契约） |
| **轴三·宿主** | 终端 / Web / 飞书 / Symphony / IDE… | **自由组合** | 以为「一个宿主只能是纯人 or 纯机器」（伪二分） |

> 关键推论：**没有「人类端点」和「机器端点」之分**，只有「一个宿主挂载了哪几个 (投影器→消费者) 对」。终端默认挂人类投影器，**可叠加**机器投影器；反之亦然。这一点取代了任何「端点类型表」。

### 1.1 为什么协议必须是两个，而不是一个，也不是 N 个

- **不能是一个**（codex 极致统一的代价）：人类协议天然携带**纯人眼**语义——markdown 分块边界、语法高亮 lexer、rich 面板、截断阈值。塞进通用协议 → 机器消费者背着用不到的负担；不塞 → 终端又得自己推导，退回 §0.2 的复制反模式。
- **不必是 N 个**：Web / 飞书 / Twitter 的差异是**能力差异**（流式✓✗、markdown✓✗），由 `capabilities` + `CapabilityAdapter` 在**同一个** `ViewEvent` 上降级吸收，不需要各自的协议。
- **恰好两个**：分水岭只有一条——**消费者是人眼还是程序契约**。人眼侧共享 `ViewEvent`，程序侧共享 `ServerNotification`。这是「关注点」层面的二分，不是「通道」层面的。

### 1.2 包索引（目标结构）

| 子包 | 轴 | 一句话职责 | 关键入口 |
|------|----|-----------|---------|
| `cli/contracts` | 共享层 | 跨宿主复用的协议契约与基类（结构化 Protocol + 可继承基类 + 人类视图契约） | `contracts/interface/{ports,consumer,projector}.py` · `contracts/base/{consumer,projector}.py` · `contracts/view/{events,capabilities}.py` |
| `cli/view` | 协议·人 | `AgentEvent` → `ViewEvent` 的唯一折叠点（宿主特定的折叠）；契约本体在 `contracts/view` | `view/projector.py`（`ViewProjector`） |
| `cli/proto` | 协议·机器 | `AgentEvent` → `ServerNotification`；机器契约（codex app-server） | `proto/projector.py` · `proto/schema.py` |
| `cli/consumers` | 宿主 | 各消费者适配器 + 自注册表（终端/Web/飞书/机器…）；基类 `BaseConsumer` 在 `contracts/base` | `consumers/registry.py` |
| `cli/io` | 输入 | 输入端口（对话式 / 广播式 / 协议式三种语义）；端口 Protocol 在 `contracts/interface/ports.py` | `io/terminal_io.py` |
| `cli/router` | 路由 | 多租户会话路由：入站 → session（公众平台必需） | `router/session_router.py` |
| `cli/commands` | 命令 | 斜杠命令注册表（脱离循环与宿主） | `commands/registry.py` |
| `cli/driver.py` | 编排 | 瘦循环 + **per-session 驱动权仲裁**（见 §5.2） | `driver.py` |
| `cli/app.py` | 装配 | `config → control → driver → 投影器 → 消费者` 组装 | `app.py` |

---

## 2. 核心抽象

### 2.1 `ViewEvent` —— 人类协议（窄腰之一）

稳定、粗粒度、纯表现的**开放标签联合**，承载*显示意图*而非*投递方式*。新增一种 ViewEvent 永不破坏旧消费者。

```python
# cli/contracts/view/events.py  — 用 pydantic（项目已用），便于导出 JSON Schema
class ViewEvent(BaseModel):
    kind: ClassVar[str]                                       # 判别符，对齐 AgentEvent.name

class MessageBlockStarted(ViewEvent):   role: str
class MessageBlockDelta(ViewEvent):     text: str            # 流式 token
class MessageBlockCompleted(ViewEvent): markdown: str        # 完整一段
class ReasoningDelta(ViewEvent):        text: str            # think 流
class ToolCallStarted(ViewEvent):       title: str; headline: str          # 已推导好的标题
                                        body: str | None; lexer: str | None # 已选好的高亮
class ToolCallCompleted(ViewEvent):     ok: bool; summary: str             # 已判定成败 + 摘要
class TaskProgress(ViewEvent):          stage: str; status: str; detail: str
class Notice(ViewEvent):                text: str; level: str
class ErrorRaised(ViewEvent):           text: str
class QuestionAsked(ViewEvent):         question: str; options: list | None
```

`render.py` 里那些 `_HEADLINE_ARG` / `_BODY` / `_EXT_LEXER` / `_MAX_*` / `_split_committable`，**全部上移到投影器**，在这里变成已经算好的中立数据（`title` / `lexer` / `summary` 是字段，不是各消费者再推导的逻辑）。

### 2.2 `ServerNotification` —— 机器协议（窄腰之二）

对齐 codex app-server 的服务端→客户端通知词汇。**不与 `ViewEvent` 合并**（§1.1）。它**没有**markdown 分块、lexer、面板这些人眼概念，只有结构化的 thread/turn/item/diff/usage。详见 §6（机器消费者）。

> 两个协议**同源不同形**：都从 `AgentEvent` 折叠而来，但目标词汇表不同。这正是「投影一次」原则的体现——折叠逻辑里**与目标无关**的部分（成败判定、工具语义）可共享，**与目标相关**的部分（markdown 分块 vs item 边界）各投影器自理。

#### 2.2.1 共享落在投影器，不落在事件类型（关于「要不要基类」）

「同源」很容易诱出一个错误推论：给 `ViewEvent` 和 `ServerNotification` 加一个共同基类 `ProjectedEvent`。**不要。** 判断基类该不该存在只有一条准绳——**有没有人会多态地使用它**：

- **事件类型：没有，故无基类。** `TerminalConsumer.handle` 只吃 `ViewEvent`，`AppServerConsumer.handle` 只吃 `ServerNotification`，**永不存在同时面对两者的 handler**。共同基类在这里退化为无行为的空标记，且会变成**耦合后门**：一旦有人往基类加一个对人有用的字段（如 `markdown_hint`），它会自动泄漏进机器协议——§1.1 明令禁止的「两协议合并」就从基类这条缝爬回来。两个事件家族应各自沿用核心 `AgentEvent` 的约定（带 `name`/`kind` 判别符的**开放标签联合**，是 union 不是 hierarchy）。★实证（核心即此约定）

- **投影器：有，故设基类。** 真正的共享代码——「与目标无关的折叠」——落在这里，且确有多态（模板方法）：

```python
# cli/view/projector.py（主干）
class BaseProjector(ObservationSubscriber):
    """共享主干：订阅 EventBus + 通用折叠（与目标无关的部分）。"""
    def _judge_ok(self, ev: PostToolUseEvent) -> bool: ...      # 成败判定，两边共用
    def _tool_semantics(self, ev) -> ToolMeta: ...              # 工具语义，两边共用
    async def on_event(self, ev: AgentEvent) -> None:
        vm = self._common_fold(ev)            # 共享：算出中立中间量
        out = self._shape(vm)                 # 分叉：子类成形为各自协议
        if out is not None: await self._emit(out)

class ViewProjector(BaseProjector):           # 枝：markdown 分块 / lexer / 截断
    def _shape(self, vm) -> ViewEvent | None: ...

class AppServerProjector(BaseProjector):      # 枝：item 切分 / 关联 id / schema 对齐
    def _shape(self, vm) -> ServerNotification | None: ...
```

> 一句话：**共享的是「怎么折叠」（投影器·主干），不是「折叠成什么」（事件类型·枝尾）。** 基类放投影器上有真实复用与多态；放事件类型上只有空标记 + 一条把已禁合并偷渡回来的暗道。这也是「投影一次」里那个「一次」的物理落点——`_common_fold` 写一遍，两个协议共享。


### 2.3 `Consumer`（协议）—— 一个消费者

本质是一个消费某协议事件的 `ObservationSubscriber`。

```python
# cli/contracts/base/consumer.py
class Consumer(Protocol):
    capabilities: Capabilities
    async def handle(self, ev: ViewEvent | ServerNotification) -> None   # 渲染/投递，或「吃掉」
    async def aclose(self) -> None

class BaseConsumer:
    """提供吃掉/降级辅助；子类只覆写关心的事件类型。"""
```

「吃掉」是 hermes 模式：渲染不了的事件可忽略或降级。飞书不便做 token 流，就缓冲 `MessageBlockDelta`，到 `MessageBlockCompleted` 再发一张卡片。

### 2.4 `Capabilities` —— 声明式能力位

```python
# cli/view/capabilities.py
@dataclass(frozen=True)
class Capabilities:
    streaming: bool; markdown: bool; syntax_highlight: bool
    interactive: bool; rich_panels: bool; images: bool
```

`CapabilityAdapter` 按能力**降级**事件流：给飞书把 `MessageBlockDelta` 合并成单个完成块，给终端保留逐 token 流。**同一条 ViewEvent 流如何同时喂养实时 TUI 与批量聊天卡片，全靠它。** 杜绝 `if consumer == "feishu"` 这类分支反模式。

### 2.5 输入端口 —— 三种入站语义

输入侧的抽象按**入站语义**分三类，共享一个基协议。注意：这是「入站长什么样」的分类，**不是**「这个会话归谁」——后者是 §5 的驱动权问题。

```python
# cli/contracts/interface/ports.py

class InputPort(Protocol):
    """公共面：把外部输入归一化成核心可驱动的输入。"""
    async def ask(self, ctx, question: str) -> str    # AskUserQuestion 回传
    def signal_interrupt(self, ctx) -> None            # 取消 / Ctrl+C

class InteractivePort(InputPort, Protocol):
    """对话式：终端 / Web / IM。有清晰「一轮」语义，可线性 await。"""
    async def read_turn(self) -> str | None            # 下一轮输入（None=结束）

class BroadcastPort(InputPort, Protocol):
    """广播式：Twitter / 公众号 / 邮件。无会话边界，推一条触发一次驱动。"""
    def subscribe(self, on_message: Callable[[InboundMessage], Awaitable[None]]) -> None

class ProtocolPort(InputPort, Protocol):
    """协议式：被外部程序（Symphony）以结构化 RPC 驱动。载荷非自然语言。"""
    def serve(self, on_request: Callable[[RpcRequest], Awaitable[RpcResult]]) -> None
```

- **交互式**：保留 `read_turn()` 线性语义，`SessionDriver` 直接 await——终端/Web 今天的形态。
- **广播式**：没有「下一轮」，它**推**消息，经 `SessionRouter`（§7）路由到归属 session。
- **协议式**：被机器以 `turn/start` 等 RPC 驱动（§6）；语义上近广播（推一条触发一轮），但载荷是结构化请求。
- 三者共享 `InputPort`（`ask`/`signal_interrupt`），所以 `AskUserQuestion`、中断在各平台语义一致。

> 注意：`ProtocolPort` 是**入站**轴的成员，不是「第四种端点类型」。一个机器消费者 = `ProtocolPort`(入站) + `AppServerProjector→机器`(出站)，二者都挂在同一 session 上，与人类消费者**平权并存**。

### 2.6 `SessionDriver` —— 瘦循环 + 驱动权仲裁

只做**编排**，不做 I/O 也不做渲染。相比今天的 `Repl`，多了一件事：**它是 per-session 驱动权（control 平面）的唯一仲裁者**（§5.2）。

```python
# cli/driver.py 伪代码
async def run(self):
    while not self._exit:
        src = await self._next_driver_source()        # 仲裁：本轮由谁发起（人/机器/广播）
        if src is None: break
        text = src.payload
        if self._commands.is_command(text):
            await self._commands.handle(text); continue
        async with self._turn_lock:                    # ★ 驱动权锁：一轮一个发起者
            self._control.send_input(self._agent_id, UserMessage(content=text))
            await self._await_quiescent()              # 输出全程由 投影→消费者 管线处理
        self._drain_feedback_into_next_turn()          # 协作：边界处吸收旁观者反馈（§5.3）
```

关键变化：**不再直接读 `context.messages`**（assistant 回复经 `MessageAppendedEvent` → 投影 → 消费者），**不再持有 renderer**。今天 ~700 行的 god object 缩到 ~120 行纯编排 + 仲裁。

### 2.7 `ConsumerRegistry` & `CommandRegistry` —— 自注册

```python
# cli/consumers/registry.py
@register_consumer("feishu", capabilities=FEISHU_CAPS, validate=validate_feishu_cfg)
def build_feishu_consumer(cfg) -> Consumer: ...

def build_consumers(config) -> list[Consumer]:
    """按配置实例化激活的消费者集合（可多个同时激活：终端 + Web 镜像 + 机器）。"""
```

新通道 = 新模块 + 一个装饰器，**零核心改动**。命令同理对象化、注册进表；命令输出也走 `ViewEvent`/`Notice`，于是在每个消费者上都能正确渲染（终端打印、Web 推送、飞书发卡片），而非像今天 `self._repl._notice` 直写 stdout。

---

## 3. 目录结构

```
mote/cli/
  view/                # 协议·人
    events.py          # ViewEvent 联合（人类契约·窄腰）★
    projector.py       # AgentEvent → ViewEvent（一个 ObservationSubscriber）★
    capabilities.py    # Capabilities + 按能力降级规则
    schema.py          # 导出 JSON Schema → 供 Web/飞书前端生成 TS 类型
  proto/               # 协议·机器
    projector.py       # AgentEvent → ServerNotification（ViewProjector 的孪生）★
    schema.py          # 对齐 codex app-server schema（逐字段核对）
    rpc.py             # newline-delimited JSON 解码 + 请求路由
  consumers/           # 宿主（自由组合）
    base.py            # Consumer 协议 + BaseConsumer（吃掉/降级辅助）
    registry.py        # @register_consumer + build_consumers
    terminal/          # rich TUI（今天的 render.py 重构进来）
    structured/        # JSON-lines 消费者（无头/SDK，对标 codex exec --json）
    web/               # SSE/WebSocket
    feishu/  wechat/  twitter/   # IM / 公众平台
    app_server/        # 机器消费者：codex app-server 协议（§6）
  common/              # 本地 common 层（跨宿主契约·叶子）
    interface/
      ports.py         # InputPort / Interactive / Broadcast / Protocol 协议
  io/
    terminal_io.py     # stdin + SIGINT 两段式（今天的 _read_line/_on_sigint）
  router/
    session_router.py  # 多租户：入站 → session 路由 + driver 懒创建（§7）
    delivery.py        # 出站投递可靠性：限流 / 重试 / 回执（§7.3）
  commands/
    registry.py        # Command + CommandRegistry
    builtin.py         # /help /exit /agents /new /fork /sessions /resume …
  driver.py            # SessionDriver（瘦循环 + 驱动权仲裁）
  app.py               # build_app(): 装配
  __main__.py          # 入口（解析 argv，选宿主，跑 app）
```

---

## 4. 数据流：同一真相源喂养异质消费者

用三个**对立**的消费者验证「单一真相源 + 两协议 + 自由宿主」是否成立。

### 4.1 终端（人 · 交互式 · 全流式）

```
用户输入 ──TerminalPort.read_turn()──▶ SessionDriver（取得驱动权）
  └─▶ control.send_input → 核心跑 turn → EventBus 发 AgentEvent
        └─▶ ViewProjector 折叠 → ViewEvent 流
              └─▶ TerminalConsumer (caps: streaming✓ markdown✓ panels✓)
                    · MessageBlockDelta → rich Live 增量渲染
                    · ToolCallStarted   → 圆角 Panel（title 已含 headline）
                    · ToolCallCompleted → ✓/✗ 摘要行
```

### 4.2 飞书（人 · 推送式 · 非流式）

```
Lark 入站 ──▶ FeishuPort（入站当作一轮）──▶ SessionDriver
  └─▶ 核心 → AgentEvent → ViewProjector → ViewEvent 流
        └─▶ FeishuConsumer (caps: streaming✗ markdown✓)
              · CapabilityAdapter 合并缓冲 MessageBlockDelta（streaming✗）
              · MessageBlockCompleted → webhook 发 Lark 卡片
              · ToolCall* → 折叠进卡片「执行过程」块
```

### 4.3 机器（Symphony · 协议式）—— 走另一个协议，仍是同一真相源

```
Symphony ──turn/start (RPC)──▶ ProtocolPort ──▶ SessionDriver（取得驱动权）
  └─▶ control.send_input → 核心跑 turn → EventBus 发 AgentEvent
        └─▶ AppServerProjector 折叠 → ServerNotification 流   ← 注意：换了投影器
              └─▶ AppServerConsumer
                    · turn/started · item/agentMessage/delta · turn/diff/updated · turn/completed
```

> 三条流**消费同一条 `AgentEvent`**。4.1/4.2 共享 `ViewEvent`（差异由 capabilities 吸收），4.3 换 `ServerNotification`（差异由「人眼 vs 程序」吸收）。**新增任何通道都套这个模板，不触碰核心。**

### 4.4 叠加：终端 + 机器同会话（§1 三轴正交的兑现）

```
                       AgentEvent（同一条流，EventBus observation 扇出）
                            │
              ┌─────────────┴──────────────┐
        ViewProjector                 AppServerProjector
              │                             │
        TerminalConsumer              AppServerConsumer
        （人看 rich TUI）              （Symphony 收结构化进度）
              └────── 同一进程 · 同一 session · 同一批 turn ──────┘
```

人看着实时流、随时能介入，Symphony 同时消费结构化进度收 CI/diff 证据。**这不需要新机制**——`EventBus` observation 平面本就扇出给任意多订阅者，两个投影器只是两个订阅者。「人机同时」在**观察侧**是免费的。

---

## 5. 人机协作：观察可叠加，驱动需轮换

本节是全文**概念密度最高**的一节，也是几轮辩证后收敛的结论。它回答一个诱人但容易想错的问题：「人和机器能同时用一个 session 吗？」

### 5.1 两平面，两套规则

答案藏在 `EventBus` 既有的两平面里，不需要新机制：

| 平面 | 行为 | 人机能否同时 | 根据 |
|------|------|------------|------|
| **observation（看）** | 扇出给任意多订阅者 | ✅ **真·同时** | 纯旁观，互不干扰 ★实证 |
| **control（驱动）** | 折叠到单一序列 | ⚠️ **必须轮换** | 两个发起者同时 `turn/start` 会撞车 |

所以「人机同时」是**半真命题**：

> **观察侧**——人看、IDE 读 diff、Symphony 收证据，同一 turn 同时喂三方，免费。
> **驱动侧**——一个 turn 只能有**一个发起者**。这不是限制，是自回归执行的物理事实。

### 5.2 驱动权仲裁：per-session 锁 + FIFO，不是分布式所有权

`SessionDriver` 持有一把 **per-session 驱动权锁**（§2.6 的 `_turn_lock`）。规则极简：

- 谁拿到锁，谁发起这一轮（人打字 / Symphony 发 `turn/start` / 广播消息入站）。
- 其余方在这一轮里**只能观察**，或走**非发起类**操作：`interrupt`（取消）、`ask` 应答、`steer` 入队（§5.3）。
- turn 结束（`quiescent`）释放锁，下一个发起者按 FIFO 取得。

> **明确不做**：人和机器之间的「无序热切换 / 控制权移交」。codex 协议假设「一 thread 一 driver」，没有「控制权转移」一等概念。强行让两方在 turn 内争夺驱动权，会撞上分布式所有权移交 + 世界模型一致性的经典难题。**我们用回合制（轮换）绕开它，而不是假装解决它。** 这是一个克制的、诚实的设计选择——面向十年有时靠**敢于不做**某件看起来很酷的事。

### 5.3 协作的本质：在执行边界消费对方的输入

「人机协作是未来」——方向对，但落到架构，它**不是**任何新颖机制，而是一句朴素的事实：

> **协作 = 不同粒度的轮换；轮换 = 在执行边界消费对方的输入。**

自回归模型的执行是**离散步进**的：`think → tool_call → observe → think …`。它**不是**可中途插针的连续流。所以两个常见幻觉必须破除：

- **幻觉一「同时驱动」**：被 control 平面折叠排除（§5.1）。
- **幻觉二「实时打断流程纠偏」**：物理上不存在。人的反馈只能**入队**，模型跑完当前 step、在**边界处**消费它，据此修正下一 step。「纠偏」永远发生在 step 边界，不在 step 内部。

于是「协作」退化成一个**粒度选择**，而非两种性质：

| 粒度 | 形态 | 机制 | 难度 |
|------|------|------|------|
| **turn 级**（粗） | 人看完一轮，下一轮再发 | `read_turn` 边界处取输入 | 现成 ★ |
| **step 级**（细） | 人在 turn 进行中提交评论，下个 step 边界生效 | react loop 每个 step 边界查输入队列 | 一行位置选择，非架构难题 |

两者是**同一个机制**（边界处消费输入队列）的粗细，不是两个东西。曾以为的「turn 内一致性难题」**不存在**——因为修改不在 step 内发生，在 step 边界发生，而边界处状态天然干净（上一步已完整结束）。

### 5.4 真正剩下的开放点（小而诚实）

祛魅后，`steer` 不是难题，只剩两个工程小决定：

1. **队列在 loop 哪个缝隙检查**（think 前 / observe 后）——核心 loop 的一行位置，非架构问题。☆待定
2. **粒度是否暴露给协议**：要不要让人/Symphony 知道「你的反馈下个 step 生效」还是「下个 turn 生效」——UX/协议**措辞**问题，非机制问题。☆待定

> 收口：**面向十年的协作，不需要任何尚未解决的硬问题。** 它要的全部——单一真相源（人看得全）、observation 扇出（多方同时看）、control 折叠（驱动有序）、离散 step loop（边界处可消费输入）——核心**已经具备**。协作的深浅由「边界粒度」决定，不由「能不能同时驱动」决定，而同时驱动是个伪需求。

---

## 6. 机器消费者：App-Server 协议（Symphony 兼容）

> Symphony 是个**工作编排器**（Linear 看板 → 派生自治 agent → CI/PR/证据 → 安全合并），它通过 **codex app-server 协议**驱动 coding agent。让 Mote 兼容它，**不是加一个前端**，而是让 Mote 成为这个协议的一个 server。这正是 §1 轴二（机器协议）+ 轴三（机器宿主）的落地。

### 6.1 关键发现：thread/turn/session 语义核心里**已存在**

兼容成本小的根因，有强代码实证：

- `environment/control.py` 文件头自述是 **codex `core/src/agent/control.rs` 的 port**——thread/turn/session 是 `AgentControl` 既有的一等公民，不是要新造的。★实证
- `AgentControl` 公共方法几乎一一对应 app-server 客户端请求（§6.2）。★实证
- `TurnStartEvent` / `TurnEndEvent` **已携带 `turn_id`**，`SessionStartEvent` 携带 `session_id`——出站通知所需的关联 id 已在事件里，**无需新增核心字段**。★实证

结论：兼容 Symphony **不重构核心**，只在 `cli/consumers/app_server/` + `cli/proto/` 加一个薄适配层。

### 6.2 入站映射：app-server 请求 → `AgentControl`

传输是 **stdio 上的 newline-delimited JSON**，形状近 JSON-RPC 但**非严格 2.0**（codex 实测无 `jsonrpc` 字段）。`ProtocolPort` 解码每行，按 `method` 派发：

| app-server 请求 | → 核心方法 | 备注 |
|----------------|-----------|------|
| `initialize` | （端口握手，回 capabilities） | 不触核心 ☆推断 |
| `thread/start` | `control.add_agent(runtime, root=True)` | 用 `runtime.session_id` 作 `thread.id` ★实证 |
| `turn/start` | `control.send_input(agent_id, UserMessage(...))` | 经驱动权锁仲裁（§5.2）★实证 |
| `turn/interrupt` | `await control.interrupt(agent_id)` | 非发起类操作，无需持锁 ★实证 |
| `turn/steer` | 入队，下个 step 边界消费（§5.3） | 不是 turn 内打断 ☆推断 |
| `thread/resume` | `role.resume_session(session_id)` | 复用 `session/` rollout 持久化 ☆推断（方法名待核） |
| `thread/fork` | 派生新 runtime + `add_agent` | 对齐既有 fork ☆推断 |
| （turn 完成判定） | `control.quiescent()` | 静默即 turn 结束 ★实证 |
| （thread 查询） | `control.runtimes()` / `get_runtime(id)` | 列举/定位活跃 thread ★实证 |

### 6.3 出站映射：`AgentEvent` → `ServerNotification`（AppServerProjector）

`AppServerProjector` 是 **`ViewProjector` 的孪生体**：同样订阅 role EventBus、同样折叠一次，区别只是**目标契约**是机器协议而非人眼。

| 核心 `AgentEvent` | → codex 通知 | → Symphony 事件（SPEC §10.4）|
|------------------|-------------|---------------------------|
| `SessionStartEvent` | `thread/started` | `session_started` |
| `TurnStartEvent` | `turn/started`(带 `turn_id`) | — |
| `LLMStreamDeltaEvent` | `item/agentMessage/delta` | （流式增量） |
| `MessageAppendedEvent`(assistant) | `item/completed`(agentMessage) | — |
| think 流 | `item/reasoning/textDelta` | — |
| `PreToolUseEvent` | `item/started` | — |
| `PostToolUseEvent` | `item/completed`(带 ok/输出) | `unsupported_tool_call`(识别不了时) |
| `FileMutatedEvent` | `item/fileChange/patchUpdated` + `turn/diff/updated` | （工作区 diff） |
| `ResourceReportEvent` / token | `thread/tokenUsage/updated` | （用量） |
| `TurnEndEvent`(ok) | `turn/completed`(ok) | `turn_completed` |
| `LLMErrorEvent` / turn 失败 | `error` + `turn/completed`(failed) | `turn_failed` |

### 6.4 审批与「无人在环」

codex 有两个反向请求 `item/commandExecution/requestApproval`、`item/fileChange/requestApproval`，客户端回 `{id, result:{decision:"accept"}}`。被编排场景无人点同意：

- **高信任自动批准**：Symphony SPEC §10.5 假设高信任环境 → 配 **auto-approve 权限剖面**，直接回 `accept`。这是**消费者级配置**，非核心改动（核心 PreToolUse 否决权仍在，只是应答方从「人」变「自动」）。
- **`AskUserQuestion` = 硬失败**：被编排 turn 里若触发 `AskUserQuestion`，无人可答 → 映射为 `turn_input_required` 并以 `turn_failed` 结束，而非无限等待。这与 §2.5 `InputPort.ask` 在交互式端口下「真去问人」形成对照：**同一核心能力，不同端口剖面下不同兜底。**

### 6.5 真实工作量（纯叠加，不动 §1~§5 任何决策）

| 物 | 确定性 |
|----|--------|
| `cli/proto/rpc.py`：StdioTransport + method 路由 + thread/turn 关联状态机 | ★骨架确定，细节待联调 |
| `AppServerProjector`（§6.3 映射实现） | ★模式确定（ViewProjector 孪生） |
| `cli/proto/schema.py`：对齐 codex 提交的 schema（`codex-rs/app-server-protocol/schema/json/...`）| ☆唯一需逐字段核对的硬工作 |
| auto-approve 剖面 + `AskUserQuestion→turn_failed` 兜底 | ★策略确定 |
| 入口 `python -m mote.product.cli app-server`（stdio，无 TUI） | ★确定 |
| **不需要**：改 `AgentControl` / `AgentEvent` / `ViewEvent` / 任何 §1~§5 抽象 | — |

---

## 7. 多租户会话路由与出站投递（公众平台层）

> §1~§6 是**单会话**框架。接入公众平台（微信公众号、Twitter、邮件列表）时单会话假设不成立：成千上万用户的消息洪流入站，每个用户应映射到独立 session。这需要在管线**上游**加路由、**下游**加投递可靠性。本节是迈向「平台网关」的质变。

### 7.1 缺口：单会话 ≠ 多租户网关

```
§1~§6（单会话）：              公众平台需要的（多租户）：
单 InputPort                  SessionRouter（多租户分发）← §7 新增
  → 单 SessionDriver            user_id → session_id 映射
  → 单 agent                   懒创建 driver · 并发隔离
                                  ┌──────┼──────┐
                              driver#A  #B  #C  （用户甲乙丙各自独立）
```

### 7.2 `SessionRouter` —— 入站 → session

```python
# cli/router/session_router.py
class SessionRouter:
    def __init__(self, control, driver_factory, *, key_fn=lambda m: m.user_id):
        self._key_fn = key_fn                          # 平台用户 → 路由键（openid/handle/email）
        self._drivers: dict[str, SessionDriver] = {}

    async def on_message(self, msg: InboundMessage) -> None:
        key = self._key_fn(msg)
        driver = self._drivers.get(key) or self._spawn(key, msg)   # 懒创建
        await driver.submit(msg.text)                  # 触发该 session 跑一轮
```

- **路由键可插拔**：公众号用 openid、Twitter 用 handle + 会话串 id、邮件用 thread id。
- **懒创建 + 驻留**：新用户首条消息懒创建；空闲 session 由 `environment` 既有 **LRU 驻留**淘汰（复用，不重造）。
- **并发隔离**：每个 driver 独立持有 agent 与 EventBus；一个用户 turn 异常不影响他人。
- **持久化复用**：每路由键映射一个 `session_id`，落到既有 `session/` rollout，用户「下次再来」能 resume。

### 7.3 `DeliveryManager` —— 出站可靠性

`Consumer.handle(ev)` 当前是 fire-and-forget；公众平台有严格限流、可能发失败/需重试、有异步回执。`EventBus` 的 mirror/durable 只管「核心→投影」，管不到「消费者→外部平台网络」。故在出站侧补一层：

```python
# cli/router/delivery.py
class DeliveryManager:
    """消费者与外部平台之间的出站可靠性层：限流 / 重试 / 回执 / 背压。"""
    async def send(self, target, payload) -> DeliveryReceipt: ...
    # · 令牌桶限流  · 指数退避重试（区分 429/5xx 可重试 vs 权限/封禁终态）  · 背压反馈避免无界堆积
```

### 7.4 出站身份：平台原生形态留在各消费者

- **Twitter**：`MessageBlockCompleted` 超 280 字 → 拆 thread（1/n…），`TwitterConsumer` 完成。
- **公众号**：受「48 小时内可主动推送」约束 → `WechatConsumer` 把超窗口回复降级为模板消息或落库待取。
- 共性：**入站**经 `BroadcastPort` 归一化、**路由**经 `SessionRouter`、**出站可靠性**经 `DeliveryManager`、**平台格式**留在各消费者。四者各司其职。

---

## 8. 非破坏式迁移路径

每个旧组件都有明确去处，平滑演进，不推倒重来：

| 今天 | 拆成 | 说明 |
|------|------|------|
| `Repl` | `SessionDriver` + `TerminalPort` + `TerminalConsumer` | god object 按职责三分 |
| `Repl._RenderSubscriber` | `ViewProjector` | 从「直接调 renderer」变「发 ViewEvent」 |
| `Repl` 偷读 `context.messages`（职责④） | `MessageAppendedEvent` → 投影 | **砍掉特权路径**（§0.2） |
| `ConsoleRenderer`(render.py) | `TerminalConsumer` | rich 样式留下；`_HEADLINE_ARG`/`_BODY`/分块上移投影器 |
| `_ConsoleHumanChannel` + stdin | `InteractivePort` + `terminal_io.py` | 输入独立成口 |
| `SlashCommands` | `CommandRegistry` | 命令对象化、自注册 |
| `AgentEvent`(common/events) | **不动** | 两协议都是新增薄层，非替换 |

### 8.1 分阶段落地

```
阶段①  建 ViewEvent + ViewProjector，TerminalConsumer 消费它；砍掉 context.messages 特权读
        ──► 行为不变的纯重构；格式化表上移投影器
阶段②  抽 InputPort（Interactive 先行），拆 Repl → SessionDriver + terminal_io + 驱动权锁
        ──► 输入/中断/驱动权与主循环解耦
阶段③  加 StructuredConsumer（JSON-lines 无头）
        ──► 验证解耦是否成立的试金石（对标 codex exec --json）
阶段④  加 proto/ + AppServerProjector + app_server 消费者（§6）
        ──► Mote 可被 Symphony 编排；验证「第二协议几乎免费」
阶段⑤  上 Web / 飞书 / ACP（皆 InteractivePort）+ step 级 steer（§5.3）
        ──► 每个都是「新模块 + @register_consumer」，零核心改动
阶段⑥  上 SessionRouter + BroadcastPort + DeliveryManager（§7）
        ──► 解锁公众平台：微信公众号 / Twitter（多租户 + 可靠投递）
```

> 阶段①完成即收回主要技术债（格式化收敛、特权路径消除、god object 开始瓦解），且对用户**行为零感知**——验证投影层正确性的安全里程碑。

---

## 9. 设计原则（对齐 Mote 全局约束）

1. **单一真相源**：`AgentEvent` 是唯一事实，**无人有特权路径**——人类终端不许偷读 `context.messages`，机器也一样。所有消费者一律是它的下游。这是全文第一根基。
2. **单向数据流**：`核心 → 投影 → 消费者`，消费者永不反向 import 核心；输入经 `InputPort` 单独回流。与全局「依赖单向向下」一致。
3. **两个协议，不多不少**：人眼侧 `ViewEvent`、程序侧 `ServerNotification`；分水岭是「消费者是人还是程序」，不是「哪个通道」。通道差异由 `capabilities` 在单一协议内吸收。
4. **三轴正交**：真相源（唯一）× 协议（两个）× 宿主（自由组合）。不存在「人类端点 / 机器端点」之分，只有「宿主挂了哪几个投影器→消费者对」。
5. **观察可叠加，驱动需轮换**：对应 `EventBus` 的 observation 扇出 / control 折叠两平面。人机「同时」只在观察侧为真；驱动侧靠 per-session 锁有序轮换，**不做控制权热切换**。
6. **协作 = 边界处消费输入**：协作的深浅是「轮换粒度」（turn / step），不是「能否同时驱动」（伪需求）。无尚未解决的硬问题。
7. **格式化只做一次**：所有与目标无关的展示决策收敛到投影器，绝不在消费者重复。
8. **能力声明而非分支判断**：消费者声明 `capabilities` 由 `CapabilityAdapter` 统一降级；杜绝 `if consumer == "feishu"`。
9. **影响力由结构决定**：消费者是 `ObservationSubscriber`，结构上**永不能 veto** 核心——只能观察，不能干预 agent 行为（沿用两平面契约）。
10. **优雅降级**：rich / 某 transport 不可用时回退纯文本或「吃掉」事件，消费者缺失永不致命（沿用 `EventBus` 隔离 + 超时）。

---

## 10. 诚实的边界

把话说死，避免本文被读成「无所不能」：

- **本文是设计，不是实现**。★实证项有代码支撑（事件脊柱、`control.py` 是 codex port、turn_id 已在事件里）；☆推断项（codex 协议字段、resume/fork/steer 的核心支持度）需实现期对齐，可能推翻局部。
- **「人机同时」是半真命题**：观察侧真免费（§5.1）；驱动侧是**轮换**不是并发。任何把它说成「人和机器同时控制一个 agent」的表述都是错的。
- **「实时打断纠偏」不存在**：被自回归的离散 step 本质排除。存在的只是「更细粒度的轮换」（step 边界消费输入）。曾以为的「turn 内一致性难题」是伪问题。
- **控制权热切换，明确不做**（§5.2）：不是没想到，是**主动不做**——它撞上分布式所有权移交难题，回合制足以覆盖真实协作需求（人布置→机器执行→人反馈→机器再执行）。
- **第二协议「几乎免费」有前提**：免费的是「投影器模式可复用」；不免费的是「逐字段对齐 codex schema」（§6.5），这是真实的一次性工作量。
- **真正面向十年的，是底层那条脊柱**（单一真相源 + 两平面 + 离散 step loop），不是任何一个酷炫场景。人机同时、协作、被编排——都是这条脊柱**碰巧**让其变便宜的副产品，而非设计目标本身。

> 总纲收口：**核心 → 人**可插任意人类通道（§2~§4）；**核心 → 机器**是对称的第二协议（§6）；**人 ⇄ 机器**靠边界处轮换协作（§5）；**单租户 → 平台网关**靠上游路由 + 下游投递（§7）。四个方向共用同一条「单一真相源 · 投影一次 · 契约为腰 · 两平面分权」的脊柱——这才是「面向十年」在**输出通道 / 驱动方向 / 协作粒度 / 平台规模**四个正交维度上同时成立的依据。








