# EventBus 架构设计

> `common/events/` 的统一事件脊柱。本文记录：从「三套独立机制」收敛成「一条有序异步总线」的设计、双平面分发模型、生产者→事件→订阅者映射、关键代码、设计规律与取舍，供后续维护参考。

---

## 一、总览：从「三套机制」收敛成「一条脊柱」

重构前，三个横切关注点各搞各的一套：

```
   重构前（发散）                          重构后（收敛）

  session 持久化 ── SessionRecorder        ┌──────────────────────────┐
     注入 ContextManager                   │      EventBus（一条）      │
                                           │                          │
  hook 拦截 ────── HookManager.fire    ⇒   │  producer.emit(event)    │
     散落各调用点                          │        │                 │
                                           │        ▼                 │
  流式输出 ─────── 全局 stream sink         │  control / observation   │
     进程级单例                            │  两平面分发给所有订阅者    │
                                           └──────────────────────────┘
  风险：显示的 ≠ 存的（两条独立代码路径）    收益：屏幕和磁盘吃同一条流，
                                                  结构上无法发散
```

`common/events/` 是**零依赖叶子包**：只 import common 兄弟（`common.logs` + `common.schema` 的 `PermissionBehavior`）+ 订阅者 Protocol，**从不 import roles / context / executor / hook / 任何 tool**——它们反过来把自己注册成订阅者。（v2 起 typed outcome 自成一档，events 不再 import `common.hook`。）

```
┌─────────────────────────────────────────────────────────────────────┐
│  生产者（emit 事件，从不认识订阅者）                                  │
│   Role(SessionStart/UserPromptSubmit/TurnEnd)                        │
│   ContextManager(MessageAppended/PreCompact/Checkpoint/PostCompact)  │
│   ToolExecutor(PreToolUse/PostToolUse/FileMutated)                   │
│   BaseLLM(LLMRequest/Response/Error/StreamDelta)                     │
└───────┬─────────────────────────────────────────────────────────────┘
        │ emit / observe / emit_sync
        ▼
┌─────────────────────────────────────────────────────────────────────┐
│  common/events  ★叶子层·只 import common 兄弟·谁都能 import 不成环★    │
│   EventBus   types(20+事件)   context(contextvar)   outcomes(6 typed)  │
└───────▲─────────────────────────────────────────────────────────────┘
        │ 实现订阅者 Protocol（鸭子类型，无需继承）
        │
┌───────┴─────────────────────────────────────────────────────────────┐
│  订阅者（具体类，散落各高层；运行时由 Role 在装配点注入）             │
│   控制面: HookSubscriber(common/hook)  PermissionSubscriber(executor) │
│           SpawnGate(environment)                                    │
│   观察面: RecorderSubscriber(session)  LogSubscriber(events)         │
│           TracingSubscriber  CompactionNotice  ReporterSubscriber    │
│           LspService + DiagnosticsBuffer  _RenderSubscriber(cli)     │
└───────────────────────────────────────────────────────────────────────┘
```

---

## 二、核心：双平面分发（control vs observation）

**一个事件不带「我是控制/观察」的标记**——平面是**订阅者的属性**，不是事件的属性。bus 按「有没有 `handle_control` 方法」自动分类；控制订阅者再按它声明的 `handles`（事件名元组）**路由进每事件一个桶**，桶内按 `ControlStage`（REWRITE→GATE）排序：

```
        emit(event)  ─────────────────────────────────────────┐
            │                                                  │
            ▼                                                  │
 ┌───────────────────────────────┐                            │
 │ Phase ① CONTROL（控制面）      │  只跑 event.name 那个桶     │
 │  ControlSubscriber.handle_     │  桶内按 ControlStage 串行   │
 │  control() → Optional[         │  chained-reduce：           │
 │              ControlOutcome]   │   merge 折叠 + rebind 前向穿 │
 │  • 桶=HookSub/Permission/Spawn │   (deny>ask>allow,累积ctx,  │
 │  • 120s 超时熔断 + FailMode    │    sticky stop)             │
 │  • 能 veto/改参/注入ctx/stop   │  ──► 折叠结论回传 emitter   │
 │  • blocking(deny/stop) 短路    │      (它据此决定放不放行)   │
 └───────────────┬───────────────┘                            │
                 │ 过了（没被 deny/stop）                       │
                 ▼                                              ▼
 ┌───────────────────────────────────────────────────────────────┐
 │ Phase ② OBSERVATION（观察面）  fan-out，返回值一律丢弃          │
 │  ObservationSubscriber.handle()  按 DeliveryPolicy 投递：       │
 │   • MIRROR（默认）：best-effort + 30s 超时，慢/挂→丢弃+计数     │
 │   • DURABLE（recorder）：不超时·必须完成·失败大声报+计 durable_  │
 │                          failures（丢一条=数据丢失）            │
 │  recorder(pri80) → logger(pri90) → tracing → lsp → render ...  │
 └───────────────────────────────────────────────────────────────┘

 三种入口（四条调用路径，详见第五节时序图）：
  emit(e)        = Phase① + Phase②，返回 folded outcome   （控制语义；持显式 bus）
  observe(e)     = 只 Phase②                              （深层观察；contextvar 取 bus）
  emit_sync(e)   = 同步 fan-out 给有 handle_sync 的观察者  （高频/同步调用点的观察）
                   ├ observe_event_sync：深层免穿参，contextvar 取 bus（token/用量/进度）
                   └ bus.emit_sync：编排层自持运行时级 bus（agent 生命周期）
```

---

## 三、生产者 → 事件 → 订阅者 映射

```
═══════════════════════════════════════════════════════════════════════════════════
 领域        事件                     生产者(emit点)          消费订阅者         平面
───────────────────────────────────────────────────────────────────────────────────
 会话        SessionStartEvent        Role._emit_session_start  Recorder(建log)   观察
             SessionEndEvent          Role                      Hook(SessionEnd)  控制+观察
 轮次        TurnStartEvent           Loop                      Log               观察
             TurnEndEvent             Role._emit_turn_end       Recorder(+drain)  观察(durable)
───────────────────────────────────────────────────────────────────────────────────
 消息        MessageAppendedEvent     ContextManager.add        Recorder→rollout  观察(durable)
 压缩        PreCompactEvent          ContextManager            Hook(可veto)      控制
             CompactionCheckpoint     ContextManager            Recorder(检查点)  观察(durable)
             PostCompactEvent         ContextManager            CompactionNotice  观察
───────────────────────────────────────────────────────────────────────────────────
 工具        PreToolUseEvent          ToolExecutor.run_command  Hook(deny/改参)   控制 ★
             PostToolUseEvent         ToolExecutor              Hook(注入/block)  控制 ★
             FileMutatedEvent         ToolExecutor(写盘后)       LspService/Watcher观察
 用户        UserPromptSubmitEvent    Role.run                  Hook(注入/veto)   控制
───────────────────────────────────────────────────────────────────────────────────
 LLM         LLMRequest/Response/Error BaseLLM._run_with_recovery TracingSubscriber 观察
             LLMStreamDeltaEvent      log_llm_stream(每token)   _RenderSub(画屏)  观察(sync)★
───────────────────────────────────────────────────────────────────────────────────
 诊断/资源   DiagnosticsEvent         LspService                DiagnosticsBuffer 观察
             ResourceReportEvent      ResourceReporter          ReporterSubscriber观察
             TaskProgressEvent        bggraph 进度写            _RenderSub        观察(sync)
═══════════════════════════════════════════════════════════════════════════════════
 ★ = 控制面真正能影响宿主的关键事件（无控制订阅者映射的事件 emit 返回 None）
```

---

## 四、关键代码

### 1. 双平面分类 + 两阶段 emit（`bus.py`）

```python
def subscribe(self, sub) -> None:
    # 有 handle_control → 控制面；否则观察面。平面=订阅者能力，不是事件标记
    if hasattr(sub, "handle_control"):
        self._register_control(sub)           # 按 handles 路由进每事件一个桶
    else:
        _insert_by(self._observers, sub, _priority_of)   # 观察面按 priority 升序

def _register_control(self, sub) -> None:
    handles = getattr(sub, "handles", None)   # 必须声明非空事件名元组
    if not handles: raise ValueError(...)
    # fail-closed 订阅者必须暴露 on_failure（bus 不知该造哪个 typed deny）
    if getattr(sub, "fail_mode", FAIL_OPEN) == FAIL_CLOSED and not hasattr(sub, "on_failure"):
        raise ValueError(...)
    for name in handles:
        _insert_by(self._control[name], sub, _stage_of)  # 桶内按 ControlStage 升序

async def emit(self, event) -> Optional[ControlOutcome]:
    outcome, final_event = await self._run_control(event)  # Phase① 折叠+穿线
    await self._dispatch_observers(final_event)            # Phase② 喂最终改写后的事件
    return outcome                            # 无控制订阅者映射该事件 → None
```

### 2. Phase① 控制面：只跑 event.name 那个桶 + chained-reduce + FailMode

```python
async def _run_control(self, event):          # 返回 (folded_outcome, final_event)
    acc: Optional[ControlOutcome] = None
    current = event
    for sub in self._control.get(event.name, ()):   # 只跑这个事件的桶，按 stage
        try:
            out = await asyncio.wait_for(sub.handle_control(current), self._control_timeout)
        except (asyncio.TimeoutError, Exception) as exc:
            out = self._on_control_failure(sub, current, exc)  # 按 FailMode 决定
            if out is None: continue           # FAIL_OPEN：丢贡献，链继续
            acc = out if acc is None else acc.merge(out)
            break                              # FAIL_CLOSED：折叠 on_failure deny 后短路
        if out is None: continue
        acc = out if acc is None else acc.merge(out)   # 每 outcome 自己的 merge 折叠
        current = out.rebind(current, by=_name_of(sub)) # 改写前向穿 + 盖章「谁改的」(provenance)
        if out.is_blocking: break              # deny/stop 是终态，后来者无法翻案
    return acc, current
```

### 3. Phase② 观察面：按可靠性分级（MIRROR vs DURABLE）

```python
async def _dispatch_observers(self, event) -> None:
    for sub in self._observers:
        policy = getattr(sub, "delivery", None) or "mirror"
        try:
            if policy == DURABLE:
                await sub.handle(event)                      # 必须完成，不超时
            else:
                await asyncio.wait_for(sub.handle(event), self._observer_timeout)
        except (TimeoutError, Exception) as exc:
            if policy == DURABLE:
                self.durable_failures += 1                   # 失败不吞，大声报+计数
                logger.error("... rollout may be incomplete")
            else:
                logger.warning("... dropped")                # mirror 丢了无所谓
```

### 4. contextvar「全局登记」：深层调用点免穿参（`context.py`）

```python
_ACTIVE_BUS: ContextVar[Optional[EventBus]] = ContextVar("metagpt_event_bus", default=None)

@contextmanager
def set_bus(bus):                 # Role.run() 一开始 with set_bus(self.event_bus)
    token = _ACTIVE_BUS.set(bus)
    try: yield bus
    finally: _ACTIVE_BUS.reset(token)

async def observe_event(event):   # 深层「只观察」入口——结构上无法携带控制
    bus = _ACTIVE_BUS.get()
    if bus is None: return         # 未绑定→静默 no-op（standalone/测试安全）
    await bus.observe(event)       # 只跑 Phase②
```
> **铁律**：contextvar 入口只有 `observe_event`/`observe_event_sync`（纯观察）。控制发射方（executor/context/role）始终持**显式 bus 引用**调 `emit`——丢了 contextvar 最坏丢一个观察，**永远丢不了一个 veto**。

### 5. 同步快车道：流式 token（`common/logs/stream.py`）

```python
def log_llm_stream(msg):          # provider 在 async for chunk 循环里同步调
    from metagpt.common.events import LLMStreamDeltaEvent, observe_event_sync
    observe_event_sync(LLMStreamDeltaEvent(token=msg))   # 不 await，扔了就走
```
```python
def emit_sync(self, event) -> None:        # bus.py：只通知有 handle_sync 的观察者
    for sub in self._observers:
        handler = getattr(sub, "handle_sync", None)
        if handler is None: continue        # 没有就跳过（如 LogSubscriber 故意不接 token）
        try: handler(event)
        except Exception: logger.warning(...)   # 隔离，永不抛
```

### 6. DURABLE 订阅者：存档 + turn 边界落盘屏障（`session/subscribers.py`）

```python
class RecorderSubscriber:
    priority: int = 80                       # 排在 HookSubscriber 之后
    delivery: DeliveryPolicy = DURABLE       # 不超时·失败上报

    async def handle(self, event):
        if not self.enabled: return          # resume 重放时关闭
        if isinstance(event, MessageAppendedEvent):
            self._log.append(MessageEvent(message=event.message))
        elif isinstance(event, CompactionCheckpointEvent):
            self._log.append(CompactedEvent(...))
        elif isinstance(event, TurnEndEvent):
            self._log.append(TurnContextEvent(...))
            from metagpt.common.disk import get_disk_writer
            await get_disk_writer().drain()  # ★每轮结束确认落盘（崩溃最多丢进行中一轮）
```

### 7. 装配点：Role 一处把所有订阅者挂上（`roles/role_components.py`）

```python
def _build_event_bus(self):
    bus = EventBus()
    if self.hook_manager is not None:                 # opt-in：有 hook 层才挂
        bus.subscribe(HookSubscriber(self.hook_manager))   # → 控制面
    bus.subscribe(RecorderSubscriber(self.session_log))    # → 观察面(durable)，always
    bus.subscribe(LogSubscriber())                         # → 观察面
    if tracing_enabled():
        bus.subscribe(TracingSubscriber(LangfuseBackend(), ...))
    bus.subscribe(self.compaction_notice)
    if METAGPT_REPORTER_DEFAULT_URL:
        bus.subscribe(ReporterSubscriber(...))
    if (lsp := self.lsp_service) is not None:              # opt-in
        lsp.bus = bus; bus.subscribe(lsp)
        if (buf := self.diagnostics_buffer): bus.subscribe(buf)
    return bus
```

### 8. 生产者侧：emit 两轮 + 读 outcome（`executor/tool_executor.py`）

```python
# PreToolUse：权限闸门之前发，hook 可改参/拦截
if self._bus is not None:
    outcome = await self._bus.emit(PreToolUseEvent(tool_name=name, tool_input=args, ...))
    if outcome is not None:               # 无 hook/gate 映射该事件 → None
        if outcome.updated_args is not None: args = outcome.updated_args   # 改参
        if outcome.behavior == "deny" or outcome.stop:
            return _failed_result(ToolPermissionDeniedError(...))          # 拦下，工具不跑
# ... 权限引擎再查一道（deny-wins 复合） ... 真正执行 ...
# PostToolUse：跑完发，hook 可注入上下文/block
# FileMutated：成功且改了文件才发，纯观察（LSP/watcher 消费）
```

---

## 五、四条调用路径 + 各自时序图

一个 bus，三个入口方法，但按「谁持 bus、走不走审批、同步还是异步」实际分出**四条路径**。下面每条给一张时序图。

### 路径 A —— `emit`：控制语义（重，两轮，持显式 bus）

代表：一次工具调用。发射方手里攥着 bus，必须先等控制面折叠出结论再决定放不放行。

```
 ToolExecutor(持显式 bus)   bus              HookSub(控制)    Recorder/Log(观察)   权限引擎
      │                     │                   │                 │                 │
      │ emit(PreToolUse) ──►│                   │                 │                 │
      │                     │ Phase① handle_control(挨个await,升序)│                 │
      │                     │──────────────────►│                 │                 │
      │                     │◄── Optional[Outcome] (deny/改参/stop)│                 │
      │                     │ fold → folded outcome               │                 │
      │                     │ Phase② handle()(fan-out,返回值丢弃)  │                 │
      │                     │────────────────────────────────────►│                 │
      │◄── folded outcome ──│                   │                 │                 │
      │ 读 outcome：updated_args?改参 / deny|stop?拦下不执行          │                 │
      │ 没被拦 ──────────────────────────────────────────────────────────────────────►│ 再查一道(deny-wins)
      │ 真正执行工具 ...                          │                 │                 │
      │ emit(PostToolUse) ─►│ (同上两轮：hook 可注入ctx/block)      │                 │
      │ emit(FileMutated) ─►│ (纯观察：LspService/Watcher 消费)     │                 │
```
要点：**控制面顺序 await**（不并发），因为「veto 必须在动作发生前折叠完」；`Recorder`（观察面）靠 **phase**（控制面整体先于观察面）排在 `Hook`（控制面）之后 ⇒ 被否决的动作永不被记成发生过（观察面内部才用 priority 排序）。

### 路径 B —— `observe`：深层观察（异步，contextvar 取 bus）

代表：LLM 请求/响应/错误。发射点深在网络恢复逻辑里，没法层层穿 bus 参，靠 contextvar 取；纯观察，结构上带不了控制。

```
 BaseLLM._run_with_recovery(深层)     contextvar(_ACTIVE_BUS)    bus           Tracing/Log(观察)
      │                                    │                     │                 │
      │ await observe_event(LLMRequest) ──►│                     │                 │
      │                                    │ get() → bus(Role.run 开头 set_bus 登记)│
      │                                    │────────────────────►│ 只 Phase②       │
      │                                    │                     │────────────────►│ handle()
      │ ... 真正发请求、流式、重试 ...        │                     │                 │
      │ await observe_event(LLMResponse)──►│ get() → bus ────────►│ 只 Phase② ─────►│ handle()
      │ (失败) observe_event(LLMError) ───►│ get() → bus ────────►│ 只 Phase② ─────►│ handle()
```
要点：bus 未绑定（standalone / 测试无 bus）→ `get()` 返回 None → **静默 no-op**。丢 contextvar 最坏丢一条观察，**永不丢一个 veto**（控制走路径 A 的显式引用）。

### 路径 C —— `observe_event_sync`：同步快车道·contextvar 版（不 await，扔了就走）

代表：流式 token，以及资源用量、后台任务进度。调用点是**同步**的（在 `async for chunk` 循环里 / 同步上报代码里），没法 await，且无可拦截语义。

```
 Provider async-for(同步调用点)   contextvar    bus              有 handle_sync 的观察者
      │                            │            │                 │
   每个 chunk:                      │            │                 │
      │ log_llm_stream(token) ─────│            │                 │
      │   observe_event_sync(LLMStreamDelta) ──►│                 │
      │                            │ get()→bus  │ emit_sync:只挑有 handle_sync 的
      │                            │───────────►│────────────────►│ _RenderSub.handle_sync(token)
      │                            │            │                 │   → renderer.stream → 屏幕 Live 区
      │                            │            │ (LogSub 故意无此 token 分支→不刷日志)
   循环结束:                        │            │                 │
      │ log_llm_stream("\n") ──────────────────►│ emit_sync ─────►│ 收尾
```
同走此版的还有：`ResourceReportEvent`（`common/utils/report.py` ResourceReporter._report）、`TaskProgressEvent`（`executor/tasks/bggraph/report.py` 进度写）。三者共性：**纯观察 + 同步调用点 + 高频或无关紧要**，丢一帧最多少显示一点。

### 路径 D —— `bus.emit_sync`：同步快车道·编排层版（自持运行时级 bus）

代表：agent 生命周期里程碑（added / rehydrated / evicted / interrupted）。编排层（`control.py` / `residency.py`）**跑在任何 per-turn bus 之外**，没有「当前这一轮」的 contextvar bus 可取，所以它自己持有一个**运行时级 bus**，直接 emit_sync。

```
 ControlPlane / ResidencyStore(自持 runtime bus)    bus              有 handle_sync 的观察者
      │                                              │                 │
   add_agent(runtime) → touch ...                    │                 │
      │ self._event_bus.emit_sync(                    │                 │
      │     AgentLifecycle phase="added") ───────────►│ emit_sync ─────►│ LogSub.handle_sync(INFO 里程碑)
      │                                              │                 │ _RenderSub.handle_sync
   evict(候选) → materialize+shutdown ...             │                 │
      │ emit_sync(AgentLifecycle phase="evicted") ───►│ ───────────────►│
   还有 phase="rehydrated" / "interrupted"            │                 │
```
要点：C 与 D 是**同一个 emit_sync 终点**，差别只在「bus 从 contextvar 全局取（深层免穿参）」还是「编排层手里本来就有」。

### 同步快车道全员名册（`emit_sync` 的乘客）

```
═══════════════════════════════════════════════════════════════════════════
 事件                     发射点                          入口             路径
───────────────────────────────────────────────────────────────────────────
 LLMStreamDeltaEvent      common/logs/stream.py            observe_event_sync  C
   (每个流式 token)         log_llm_stream()                 (contextvar)
 ResourceReportEvent      common/utils/report.py           observe_event_sync  C
   (资源用量上报)            ResourceReporter._report         (contextvar)
 TaskProgressEvent        executor/tasks/bggraph/report.py observe_event_sync  C
   (后台任务进度)            进度写                            (contextvar)
───────────────────────────────────────────────────────────────────────────
 AgentLifecycleEvent      environment/control.py           bus.emit_sync       D
   (added/rehydrated/      environment/residency.py         (自持 runtime bus)
    evicted/interrupted)
═══════════════════════════════════════════════════════════════════════════
 共性：纯观察(无 veto) + 同步调用点(没法 await) + 高频或编排级里程碑
 投递：只给实现了 handle_sync 的观察者；其余观察者(如 Recorder)结构上收不到
```

---

## 六、四条贯穿性规律

**规律 1 —— 平面是订阅者属性，不是事件标记**
事件数据上没有 `is_control`，bus 按 `hasattr(sub,"handle_control")` 分类。于是「标记与行为对不上」这类 bug 从结构上不存在；只有控制面能 fold（且各返回自己那个 typed `ControlOutcome`），观察者返回值被丢弃 ⇒ 观察者**永远无法影响宿主**。

**规律 2 —— 控制面按事件名路由，桶内按 ControlStage；phase 编码因果语义**
控制订阅者按 `handles` 分进每事件一个桶，桶内 `REWRITE→GATE` 保证「先改写、后裁决」。观察面整体排在控制面之后（phase 边界）⇒「被否决的动作永远不会被记录为发生过」；观察面**内部**再用 `priority` 排 `Recorder(80)`/`Log(90)`。控制排序（ControlStage）与观察排序（priority）是两套独立维度。

**规律 3 —— 按「可丢/不可丢」分级投递**
MIRROR（渲染/日志）慢了挂了直接丢弃+计数，绝不拖垮 turn；DURABLE（rollout）不超时·必须完成·失败大声报+计 `durable_failures`。对两种截然不同的可靠性需求诚实区分。

**规律 4 —— contextvar 只搬运观察，控制走显式引用**
深层调用点免穿参靠 contextvar；但其入口结构上只能发观察事件。丢 contextvar 最坏丢一帧显示，绝不漏放一个该拦的操作。

---

## 七、取舍与风险（诚实记录）

| 取舍 | 说明 |
|---|---|
| 控制面顺序 await，非并发 | 为「veto 必须在动作前折叠」有意取舍吞吐，靠 120s 超时兜底 |
| 控制排序改为 ControlStage 命名枚举 | 取代 v1 的 priority 魔数：桶内只有 REWRITE/GATE 两档语义命名，一事件一般就一个订阅者（仅 PreToolUse 桶=Hook+Permission 两个），排序歧义基本溶解 |
| 观察面 priority 改为 ObserverPriority 命名枚举 | 取代旧的裸魔数：LIVE/STREAM/REPORT/PERSIST/TRACE/LOG/BOOKKEEPING 七档语义命名（曾出现 LogSubscriber 与 FileWatchService 双 90 静默撞车，正是此改动催化剂）。加新观察者挑一档带语义的 tier 而非猜一个不撞的整数 |
| 全局 contextvar 态 | 忘 set_bus 则 observe 静默 no-op，测试易踩；反面是 standalone/测试自动安全 |
| turn 边界粒度持久性 | 崩溃丢进行中一轮，换来不必每条消息 fsync（每轮 drain 一次） |


