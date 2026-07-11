# Protocol 架构设计

> `common/interface/` 的 10 个结构化 Protocol（PEP 544）。本文记录：消费方 → 契约 → 实现方的映射、分层依赖、设计规律与取舍，供后续维护参考。

---

## 一、总览：Protocol 是"零依赖叶子层"

```
┌─────────────────────────────────────────────────────────────────────┐
│  消费方（只声明依赖窄契约，从不 import 右边的具体类）                 │
│                                                                       │
│   ReActLoop   ThinkEngine   ContextProvider   File工具   ToolExecutor │
│   TurnContextBus   EventBus   Role                                    │
└───────┬───────────────────────────────────────────────────┬─────────┘
        │ 依赖契约（向下）                                     │
        ▼                                                     ▼
┌─────────────────────────────────────────────────────────────────────┐
│  common/interface  ★叶子层·只 import typing·谁都能 import 不成环★     │
│                                                                       │
│   MessageStore   RequestAssembler   MessageActivity   MessageSink     │
│   LLMClient   BackgroundPool   HookRunner   FileSnapshotStore         │
│   EphemeralContextSource   ControlSubscriber   ObservationSubscriber  │
└───────▲───────────────────────────────────────────────────▲─────────┘
        │ 实现契约（鸭子类型，无需继承）                       │
        │                                                     │
┌───────┴───────────────────────────────────────────────────┴─────────┐
│  实现方（具体类，散落在各高层；运行时由 Role 在装配点注入）           │
│                                                                       │
│   ContextManager(context/)   MessageQueue(common/)                    │
│   BaseLLM子类+FakeLLM(router/+测试)   BackgroundTaskPool(tasks/)       │
│   HookManager(common/hook)   FileSnapshotRecorder(session/)           │
│   Git/TokenPressure/BgTask/Lsp Sources   各 ObservationSubscriber     │
└───────────────────────────────────────────────────────────────────────┘
```

中间这层只 import `typing`（连 `Message` 都在 `TYPE_CHECKING` 下）。消费方在上、实现方在下，两边都只指向中间契约，彼此从不直接相连。

---

## 二、10 个契约按领域归类

```
═══════════════════════════════════════════════════════════════════════════════════════
 领域            契约 (Protocol)         消费方              实现方               方法切面
───────────────────────────────────────────────────────────────────────────────────────
 ① 会话/消息     MessageStore ★         Loop/通道/Think     ContextManager       get/add/
                                                            (+测试double)        add_batch/delete
                 RequestAssembler        ContextProvider     ContextManager       prepare_request
                 MessageActivity         Role.wait_*/        MessageQueue         wait_for_message
                                         BgPool.wait_any                          (只能等)
                 MessageSink ★           BgPool.deliver      MessageQueue         push (只能塞)
───────────────────────────────────────────────────────────────────────────────────────
 ② LLM           LLMClient               ThinkEngine/Loop    BaseLLM子类+FakeLLM  model/aask/aask_tool
───────────────────────────────────────────────────────────────────────────────────────
 ③ 后台任务      BackgroundPool          ReActLoop(idle turn)BackgroundTaskPool   has_pending/wait_any/
                                                                                 wait_for_completion
───────────────────────────────────────────────────────────────────────────────────────
 ④ 事件/钩子     HookRunner ★            ToolExecutor/       HookManager          fire(event,payload)
                                         ContextManager/Role
                 Control/ObservationSubscriber ★  EventBus    控制面/观察面订阅者  handle_control / handle
───────────────────────────────────────────────────────────────────────────────────────
 ⑤ 上下文/持久   FileSnapshotStore ★     Write/Edit工具       FileSnapshotRecorder snapshot(path)
                                                             (session/)
                 EphemeralContextSource ★TurnContextBus      Git/Token/BgTask/    name/priority/render
                                                             Lsp Sources
═══════════════════════════════════════════════════════════════════════════════════════
 ★ = 加了 @runtime_checkable（外部插件式契约，测试里做 isinstance 合规断言）
   未加：RequestAssembler / LLMClient / BackgroundPool / MessageActivity（纯静态内部契约）
```

---

## 三、"一个对象，多张脸"（接口隔离 ISP）

```
        ContextManager（同一个实例，真身几十个方法）
        ┌──────────────────────────────────────────────┐
        │  脸A: MessageStore     脸B: RequestAssembler   │
        │  ├ get                 └ prepare_request       │
        │  ├ add                                         │
        │  ├ add_batch          ┌────────────────────┐  │
        │  └ delete             │ manage_history /    │  │
        │                       │ 压缩 / 组装 ...     │  │
        │                       │ （不在任何契约里）  │  │
        │                       └────────────────────┘  │
        └──────────────────────────────────────────────┘
                │脸A                │脸B          ╳ 调不到
                ▼                   ▼
   ReActLoop/通道/Think      ContextProvider
   「只看见存取4方法」        「只看见组装1方法」


        MessageQueue（同一个实例）
        ┌──────────────────────────────────────────────┐
        │  脸C: MessageSink       脸D: MessageActivity   │
        │  └ push（只能塞）       └ wait_for_message     │
        │              ┌─────────────────────────┐      │
        │              │ drain / serialize 内部   │      │
        │              │ + 清信号的权力（自己留） │      │
        │              └─────────────────────────┘      │
        └──────────────────────────────────────────────┘
            │脸C                      │脸D
            ▼                         ▼
   生产者/后台池deliver        Role.wait_interruptible / 后台池wait_any
   「只能投递」                「只能等待，不能清信号」
```

---

## 四、分层依赖方向（运行时无环）

```
       roles / context / router / tasks / session / executor   （高层·具体实现方）
              │  继承 ABC          │  运行时 import 契约
              ▼                    ▼
      common/base (ABC)      common/interface (Protocol)
              │                    ▲
              │ 依赖               │  ★只在 TYPE_CHECKING 下引用★
              ▼                    │     （运行时无此 import）
      common/schema ◄─────────────┘
       (Pydantic 模型)

  不变式：base → interface 的引用只存在于 TYPE_CHECKING，运行时不产生真实 import
          ⇒ 永不成环；interface 自身只依赖 typing，是绝对叶子。
```

---

## 五、三条贯穿性规律

**规律 1 —— 同一对象拆多张窄脸（接口隔离）**
- `ContextManager` → `MessageStore`（只存取）+ `RequestAssembler`（只组装），`manage_history` 谁都碰不到。
- `MessageQueue` → `MessageSink`（只能塞）+ `MessageActivity`（只能等），清信号权力 queue 自留。

**规律 2 —— 消费方与实现方"跨层零依赖"，靠 Role 在装配点注入**
- executor 工具 永不 import session → 用 `FileSnapshotStore`。
- context 总线 永不 import tasks/roles → 用 `EphemeralContextSource`。
- 守住"下层不依赖上层"的硬分层。

**规律 3 —— 是否 `@runtime_checkable` = 是否"外部可扩展"**
- 内部固定契约：不加，纯静态检查兜底。
- 外部插件契约（源/订阅者/快照器）：加，测试 `isinstance` 当合规护栏。加它等于一种 API 承诺。

---

## 六、Protocol 校验机制（两层）

| | 静态检查 (mypy/pyright) | 运行时 isinstance (@runtime_checkable) |
|---|---|---|
| 触发点 | 注入点/赋值那一行 | 显式调用 isinstance 时 |
| 检查内容 | 成员齐全 + 签名兼容(变型) + 属性类型 | 仅成员名是否 hasattr |
| 能否查签名 | 能 | 否 |
| 能否查属性类型 | 能 | 否（只查有没有） |
| 实现方要不要继承 | 否（结构匹配） | 否 |
| 性能 | 零运行时开销 | 慢（遍历成员 hasattr），别进热路径 |
| 用在哪 | 生产期所有注入点（主力） | 仅测试里做合规断言 |

**坑**：协议含数据成员时（如 `EphemeralContextSource.name/priority`），`issubclass` 报 TypeError，只能用 `isinstance`。

### 变型规则（签名兼容判定）
- 函数：**参数逆变、返回协变** → 实现方可放宽参数(父类型)、收窄返回(子类型)。
- 容器：可变 `list[T]` 不变；只读 `Sequence[T]` 协变；`Callable[[P],R]` 对 P 逆变、R 协变。
- 口诀：**出口协变，入口逆变，可变不变。**

---

## 七、Protocol vs DI：正交，叠加用

解耦三轴：
- 轴1 构造耦合（谁来 new/传入）—— 由 **DI** 解决。
- 轴2 类型耦合（消费方写哪个类型名）—— 由**抽象**解决；Protocol(结构) 比 ABC(名义) 更彻底，实现方连 import/继承都不需要。
- 轴3 表面耦合（能看见多少方法）—— 由**窄 Protocol(ISP)** 解决。

本框架是 **DI + 窄 Protocol** 叠加：构造权交出去、类型绑窄契约、表面只暴露必需方法，三轴全解。需要强制实现 + 复用基类逻辑时才退回 ABC 的显式继承。

