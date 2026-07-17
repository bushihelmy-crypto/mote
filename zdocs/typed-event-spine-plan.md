# 十年零负债方案：泛型化事件脊椎（Typed Event Spine）

> 目标：把 `common/events` + `common/interface/event_subscriber` 从"运行期声明 +
> `Any`/`cast` 兜底"升级为"编译期静态链接"，让事件↔outcome 的契约由 pyright 强制。
> 面向 agentswarm 场景下控制事件持续膨胀，做到"加一个事件/字段，改错即报错"。

## 背景：一个缺失的链接，五处下游症状

现有架构 90% 已经很好（两阶段总线、名义化订阅者、per-event outcome、rewrite 自记录）。
唯一贯穿全脊椎的缺陷：**事件与其 outcome 之间只有运行期声明（`outcome_type` ClassVar），
没有静态类型链接。** 所有下列症状都是这一个缺失的下游表现：

| # | 位置 | 现状 | 症状 |
|---|------|------|------|
| 1 | `types.py` `AgentEvent = Any` | 事件联合退化为 Any | `event.name`/字段访问全不校验 |
| 2 | `subscriber.py` `payload: Callable[[Any], dict]` | payload 入参 Any | 读错事件字段静默通过（运行期 AttributeError）|
| 3 | `bus.py` `emit(event) -> Optional[ControlOutcome]` | 返回基类 | 调用方必须 cast |
| 4 | `executor/tool_executor.py` `cast("ToolCallOutcome", outcome)` | 手写断言 | pyright 无法验证事件↔outcome 匹配 |
| 5 | `event_subscriber.py` `merge(self, other: ControlOutcome)` | 参数是基类 | 跨类型 merge 不报错 |

## 方案：建立静态链接（已由 pyright 1.1.411 验证）

三步建立 `event → outcome` 的静态链接，5 处症状连锁消失。

### 步骤 A — outcome 基类改 CRTP 自参数化

```python
# common/interface/event_subscriber.py
T = TypeVar("T", bound="ControlOutcome")

class ControlOutcome(abc.ABC, Generic[T]):
    @property
    @abc.abstractmethod
    def is_blocking(self) -> bool: ...
    @abc.abstractmethod
    def merge(self, other: T) -> T: ...          # 自类型：只能同类 merge
    def rebind(self, event: Any, *, by: str = "") -> Any:
        return event
```

每个具体 outcome 用自身参数化基类：

```python
# common/events/outcomes.py
class ToolCallOutcome(ControlOutcome["ToolCallOutcome"]): ...
class SpawnOutcome(ControlOutcome["SpawnOutcome"]): ...
```

验证结论：`merge` 重写零 override 报错；`tool_outcome.merge(spawn_outcome)` 编译期报错（#5 关闭）。
注意：不要用 `Self` 做 `merge` 参数——pyright 会判协变位置不兼容重写。必须用 CRTP。

### 步骤 B — 事件在其 outcome 类型上泛型

```python
# common/events/rewrite.py 或 types.py 顶部
O = TypeVar("O", bound=ControlOutcome)

class ControlEvent(Generic[O]):
    """所有控制事件的泛型基类；O 是本事件的 outcome 类型。"""
    name: ClassVar[str] = ""

# 控制事件声明其 outcome（替换旧的 outcome_type ClassVar 声明）
class PreToolUseEvent(ControlEvent[ToolCallOutcome], Rewritable):
    ...
class PreAgentSpawnEvent(ControlEvent[SpawnOutcome]):
    ...
```

保留 `outcome_type: ClassVar` 作为运行期反射入口（`bus.py` 的 isinstance 纵深防御仍用它），
但类型层面的真相来自 `ControlEvent[O]`。二者可用一个 `__init_subclass__` 断言保持一致，
杜绝漂移（泛型参数与 `outcome_type` ClassVar 必须指向同一类）。

### 步骤 C — emit 顺链接推断，删除所有 cast

```python
# common/events/bus.py
async def emit(self, event: ControlEvent[O]) -> Optional[O]: ...
```

验证结论：`await bus.emit(PreToolUseEvent())` 静态推断为 `ToolCallOutcome | None`；
`tool_executor.py` 的 `cast("ToolCallOutcome", ...)` 可删除（#3/#4 关闭）。
纯观察事件（不继承 `ControlEvent`）走 `observe()`，不返回 outcome，天然区分。

### 步骤 D — _HookBinding 双泛型

```python
# common/hook/subscriber.py
E = TypeVar("E")
Ob = TypeVar("Ob", bound=ControlOutcome)

@dataclass(frozen=True)
class _HookBinding(Generic[E, Ob]):
    hook_name: str
    payload: Callable[[E], dict]
    project: Optional[Callable[[HookOutcome], Ob]] = None

_BINDINGS: dict[str, _HookBinding] = {
    PRE_TOOL_USE: _HookBinding[PreToolUseEvent, ToolCallOutcome](
        "PreToolUse",
        lambda e: {"tool_name": e.tool_name, ...},   # e 是 PreToolUseEvent，字段被校验
        lambda ho: ToolCallOutcome(behavior=ho.behavior, ...),
    ),
    ...
}
```

验证结论：payload 读错事件字段、project 读错 HookOutcome 字段、binding 绑错 outcome 类型——
三类错误全部编译期报错（#2 关闭）。
代价：`subscriber.py` 需 import 具体事件类，打破其"import 图极简"原则——
用 `if TYPE_CHECKING:` 块 import，运行期零开销，原则基本保留。

### AgentEvent 联合（#1）

`AgentEvent = Any` 的存在是因为 30+ 事件手动维护 Union 太脆。两个选择：
- 保守：保留 `Any`，但让所有**控制**事件继承 `ControlEvent[O]`（控制路径已强类型，观察路径本就不读返回值，Any 影响小）。
- 彻底：`AgentEvent = Union[所有事件]` 自动由 `__all__` 生成或用 `type[ControlEvent]` 收窄控制子集。
推荐先做保守版，#1 的收益远小于 #2–#5，不值得为它冒 Union 维护风险。

## 实施顺序（自底向上，每步 pyright 零回归）

1. `event_subscriber.py`：`ControlOutcome` → CRTP（步骤 A 基类侧）
2. `outcomes.py`：6 个 outcome 自参数化（步骤 A 子类侧）
3. `rewrite.py`/`types.py`：引入 `ControlEvent[O]`，控制事件继承之（步骤 B）
4. `bus.py`：`emit` 泛型签名（步骤 C）
5. `tool_executor.py` 等调用点：删除 cast（步骤 C 下游）
6. `subscriber.py`：`_HookBinding` 双泛型 + TYPE_CHECKING import（步骤 D）
7. 全量 `pyright .` 确认零新增错误；`ztest` 全绿

## 不做什么（避免过度设计）

- 不引入运行期泛型校验（Generic 在运行期是擦除的，`bus.py` 的 isinstance 保留即可）。
- 不改两阶段/名义化订阅者/rewrite 机制——它们已是零负债。
- 不为 #1 冒 Union 维护风险（收益最小）。
- 不改任何事件的字段语义，纯类型层加固。
```