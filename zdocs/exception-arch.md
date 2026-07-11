# Exception 架构设计

> `common/exception/` 的类型化异常体系。本文记录：以 `MoteError` 为根、用「可重试性 + 恢复动作」作语义元数据的设计、重试/恢复/展示三环节如何消费这些元数据、`RecoveryRunner` 在 LLM/Tool/Graph 三领域的不同用法、两个核心算法（`is_retryable`/`classify_llm_error`）、跨层分层纪律、设计规律与取舍，供后续维护参考。

---

## 一、总览：一套语义元数据，三处消费

```
┌─────────────────────────────────────────────────────────────────────┐
│  异常携带两个语义维度（不是控制流里 except 元组判断，是异常自带元数据） │
│                                                                       │
│     retryable: bool              recovery: RecoveryAction             │
│     (能不能重试)                  (该怎么恢复)                          │
└───────┬───────────────────────────────────────────────────┬─────────┘
        │ 三个环节各取所需地消费                               │
        ▼                            ▼                        ▼
┌──────────────┐      ┌──────────────────┐    ┌────────────────────┐
│ ① 重试判断    │      │ ② 恢复调度          │    │ ③ 失败展示          │
│ is_retryable │      │ RecoveryRunner      │    │ ErrorReport         │
│ 读 retryable │      │ 读 recovery → 派策略 │    │ +render_error_block │
│ → 退避重试    │      │ COMPRESS/ROTATE/... │    │ → <error> 给 LLM    │
└──────────────┘      └──────────────────┘    └────────────────────┘
        ▲                            ▲                        ▲
        └────────────────────────────┴────────────────────────┘
              都基于「语义」判断，从不 re-parse 厂商异常类型 / sniff "Error:" 字符串
```

解决重构前三个痛点：① 重试判断散落在 `except (A,B,C)` 元组里、脆弱重复；② 工具失败靠返回 `"Error:"` 字符串前缀、sniff 文本判断成败；③ 错误展示各 executor 各搞一套、不统一。

---

## 二、核心：继承树 + MRO 标记机制

```
                       Exception (Python 内置)
                              │
                              ▼
            ┌────────────────────────────┐
            │           MoteError （根类）              │
            │  • code: ErrorCode      稳定机器码（跨版本稳定）│
            │  • cause / __cause__    链式异常              │
            │  • context              结构化诊断字段        │
            │  • retryable=False      ◄── ClassVar 默认     │
            │  • default_recovery     ◄── 显式恢复 hint     │
            │  • to_dict() / detail() 序列化契约 + 可重写钩子 │
            │  • __reduce__()         pickle 跨进程安全      │
            └──────────────┬─────────────┘
                              │
            ┌────────────────┴────────────────┐
            ▼                                  ▼
   ┌──────────────────┐          ┌──────────────────┐
   │  RetryableError   │ 标记混入  │  NonRetryableError │ 标记混入
   │  retryable=True   │          │  retryable=False   │
   └────────┬─────────┘          └─────────┬────────┘
            │  各 tier 多继承其一翻转 retryable │
            ▼                                ▼
   LLMRateLimitError                LLMAuthenticationError
   LLMTimeoutError                  LLMBillingError
   LLMConnectionError               ContextWindowExceededError
   GraphNodeTimeoutError            ToolError（默认非重试）
                                    GraphRouterError / GraphRecursionError
```

**MRO 巧思（面试常被追问）** —— `RetryableToolError`：

```python
class RetryableToolError(RetryableError, ToolError):  # RetryableError 写在前
```

`ToolError` 继承 `NonRetryableError`（默认不重试），但把 `RetryableError` 放**前面**，靠 MRO 让 `retryable=True` 覆盖掉父类的 `False` → `recovery` 自动推导成 `RETRY`。**标记类在 MRO 中先于 `MoteError`，所以标记的值赢。**

---

## 三、RecoveryAction：恢复动作语义

```
═══════════════════════════════════════════════════════════════════
 Action               含义                  典型携带者          接没接
───────────────────────────────────────────────────────────────────
 ABORT                放弃，抛给调用方        非重试默认           —
 RETRY                原样重试（带退避）      可重试默认           ✓
 COMPRESS             压缩上下文再重试        ContextWindowExceeded ✓(LLM)
                                            LLMPayloadTooLarge
 ROTATE_CREDENTIAL    换 key/账号            LLMAuth/LLMBilling   ✓(LLM)
 FALLBACK             换模型/provider        LLMContentPolicy     ✓(LLM)
 ─────────────── 改写请求体再重试同一 provider（预埋，未接 transformer）───────
 SHRINK_IMAGE         缩图                   LLMImageTooLarge     ✗预留
 DOWNGRADE_TOOL_CONTENT 降级多模态工具内容    LLMMultimodalTool    ✗预留
 STRIP_REQUEST_STATE  剥离请求状态           LLMInvalidReqState   ✗预留
═══════════════════════════════════════════════════════════════════
 recovery 解析：有显式 default_recovery 就用，否则从 retryable 推导(RETRY/ABORT)
```

**关键纪律**：异常层是「叶子」，**只发 hint，绝不执行恢复**——执行恢复需要业务能力（压缩消息/轮转 key/换 provider），那些在上层，异常层去 import 会形成 `common → business → common` 循环。

---

## 四、两个核心算法（`handlers.py`）

### 1. `is_retryable(exc)` —— 「该不该重试」单一真相源

```
                    is_retryable(exc)
                          │
                          ▼
            控制流异常? (KeyboardInterrupt/SystemExit/
            GeneratorExit/CancelledError)  ─── yes ──► False
                          │ no                        (绝不重试/吞掉)
                          ▼
            isinstance(MoteError)? ──── yes ──► return exc.retryable
                          │ no                     (自带语义)
                          ▼  厂商/stdlib 白名单兜底（未迁移异常）
            json.JSONDecodeError      ──► True (流截断瞬时)
            ConnectionError/TimeoutError ─► True (stdlib transport)
            openai/anthropic SDK:        ─► True
              APIConnection/APITimeout/
              RateLimit/InternalServerError
                          │ else
                          ▼
                        False
```

> **3.11+ 坑**：`asyncio.CancelledError` 继承 `BaseException`，谓词必须显式 guard，否则被取消的 turn 会被错误重试。

### 2. `classify_llm_error(exc)` —— 原始 provider 错误 → typed LLMError

在 provider 调用点用：`raise classify_llm_error(e) or e`（openai_api / anthropic_api 三处）。**HTTP 状态码驱动，文本模式只在状态码本身有歧义时消歧**：

```
   401 ───────────────────────► LLMAuthenticationError
   402 ───────────────────────► LLMBillingError
   403 ──┬ billing 模式 ────────► LLMBillingError
        └ 否则 ─────────────────► LLMAuthenticationError
   413 ──┬ image 模式 ─────────► LLMImageTooLargeError
        └ 否则 ─────────────────► LLMPayloadTooLargeError
   429 ──┬ billing 模式 ────────► LLMBillingError  ★永久!
        └ 否则 ─────────────────► LLMRateLimitError 瞬时
   400 ──┬ context_window ─────► ContextWindowExceededError
        ├ image ──────────────► LLMImageTooLargeError
        ├ multimodal ─────────► LLMMultimodalToolContentError
        ├ request_state ──────► LLMInvalidRequestStateError
        ├ content_policy ─────► LLMContentPolicyError
        ├ billing ────────────► LLMBillingError
        └ 否则 ─────────────────► LLMBadRequestError
   500/502 ─────────────────────► LLMServerError
   503/529 ─────────────────────► LLMOverloadedError

   ★ 同一个 429 既是瞬时限流又是永久 insufficient_quota 计费耗尽
     → 状态码不够，必须文本消歧（误判会触发无意义重试、最坏烧钱）
   OpenAI + Anthropic 两套 SDK 共用 _classify_api_status_error
   （都有 status_code，语义一致）
```

---

## 五、RecoveryRunner：领域无关恢复骨架（`recovery.py`）

```
  RecoveryRunner.run(call):
  ┌────────────────────────────────────────────────┐
  │  while True:                                                    │
  │    try: return await call()  ◄────────────────┐ 重试           │
  │    except Exception as exc:                     │                │
  │        action = _action_for(exc)                │                │
  │          ├ MoteError → exc.recovery          │                │
  │          └ 其他 → is_retryable?RETRY:ABORT      │                │
  │                                                 │                │
  │        action == ABORT ──────────────┼──► raise (永久失败上抛)   │
  │        recoveries >= max_recoveries ──┼──► raise (封顶防死循环)   │
  │        strategy = strategies.get(action)         │               │
  │        strategy is None ──────────────┼──► raise (无策略→降级)   │
  │        await strategy(exc) == False ──┼──► raise (修复失败)      │
  │        recoveries += 1 ───────────────┘ (修好了→循环回去重试)    │
  └────────────────────────────────────────────────┘

  纪律：
   • 只拥有控制流（try/分类/dispatch/重试/预算），绝不 import 业务模块
   • is_retryable 在 _action_for 里懒加载 → 保持叶子纯净
   • 策略由调用方注入 {RecoveryAction: strategy(exc)->bool} → 一套骨架服务三领域
   • 空注册表 = 行为等价裸调用（默认配置零成本）
   • CancelledError(BaseException) 永不被 catch，直接穿透
```

---

## 六、三领域对比：同一骨架，三种用法

```
═══════════════════════════════════════════════════════════════════════════
 领域      RecoveryRunner 注入了啥？        谁在 RETRY？           出口方向
───────────────────────────────────────────────────────────────────────────
 LLM      COMPRESS/ROTATE/FALLBACK         外层 runner 换条件      raise 上抛
          (RETRY 留给内层 tenacity @retry)  +内层 tenacity 瞬时退避  (provider 调用方)
───────────────────────────────────────────────────────────────────────────
 Tool     空注册表 {} （预留·未接策略）      没人，除非 tool 自带     catch 转值
                                           @retryable_tool          (ToolResult)
───────────────────────────────────────────────────────────────────────────
 Graph    {RETRY: _retry}  ◄ 真接了!       runner 自己 RETRY        raise 上抛
 node     max_recoveries=_AUTO_RETRIES     (退避+重跑节点·无tenacity)(pool)
═══════════════════════════════════════════════════════════════════════════
```

### 路径 A —— LLM：双层重试（`base_llm.py`）

```
  acompletion_text()
   ├ @retry(tenacity, retry_if_exception(is_retryable))  ◄ 内层：原样重试
   │    wait_random_exponential(1..60) · stop_after_attempt(N)
   └ _run_with_recovery → RecoveryRunner.run(_call)       ◄ 外层：换条件再重试
        state = {messages, llm}  ← 闭包捕获的可变领域状态
        strategies = { COMPRESS:重压缩  ROTATE:换key  FALLBACK:换provider
                       SHRINK_IMAGE/...:message_transformers 改写 payload }
        _call: send(llm,msgs) → provider → raise classify_llm_error(e) or e
```

> 分工：内层 tenacity 管「原样重试」（瞬时），外层 runner 管「改变条件再重试」。

### 路径 B —— Tool：保守默认 + 双重 opt-in（`tool_executor.py`）

```
  run_command(name, args)
   ├ ① 权限门(执行前)：PermissionEngine/PreToolUse hook
   │     deny → return _failed_result(ToolPermissionDeniedError) 不进 call
   ├ ② RecoveryRunner.run(_call)  ◄ 空注册表=惰性透传（预留扩展位）
   │     _call: _validate_call_args(回路内校验) → tool.call(**args)
   │     └ ③ @retryable_tool(tenacity) ◄ tool 自选 opt-in，装了才有这层
   │           predicate=is_retryable: RetryableToolError 重试 / 纯 ToolError 不重试
   │           reraise=True 耗尽后原异常上抛
   └ ④ except 收口（统一）：
        except ToolError → ErrorReport → ToolResult(success=False)  不记错误日志
        except Exception → ErrorReport(UNKNOWN) → ToolResult(success=False)
        → render_error_block → <error> 给 LLM（成败靠结构字段，不靠 sniff 字符串）

  要点：tool 错误在 executor 内「收口吞掉」转返回值，不上抛
        重试是双重 opt-in：raise RetryableToolError 且 @retryable_tool 装饰
        ②与③是 OR 不是 AND：装饰器单独就够触发重试，runner 是另一条预留通道
```

### 路径 C —— Graph：节点级重试 + 图级收口（`bggraph/engine.py`）

```
  ┌─ 节点级 _run_node ─────────────────────────────────┐
  │  RecoveryRunner({RETRY:_retry}, max=_AUTO_RETRIES) ◄ 真接策略 │
  │    _execute: node.fn → submit → poll                       │
  │       except (Timeout/Connection) → raise GraphNodeTimeout  │
  │    _retry: report "retrying N/_AUTO" → sleep(退避) → True   │
  │    分类：transient(RETRY 消耗预算) / permanent(ABORT 快失败) │
  │    收口：CancelledError→mark_cancelled+raise                │
  │          GraphNodeTimeout(耗尽)→wrap GraphNodeRetryExhausted │
  │          Exception→mark_failed+raise                       │
  └────────────────────┬──────────────────────────────┘
                       │ 节点最终失败 → 异常冒到图级
                       ▼
  ┌─ 图级 _run_driver（★无 RecoveryRunner·零重试·纯收口）─────┐
  │  while frontier: activations>recursion_limit→GraphRecursion │ 熔断(非重试)
  │                  router 炸 → GraphRouterError              │
  │                  节点失败 → 收进 all_errors                 │
  │  三路互斥收口：                                            │
  │   ① _LlmPauseSignal → return LlmPauseResult（可 resume）   │
  │   ② fatal(Recursion/Router) → 挂快照 fatal.run_state →raise │
  │   ③ all_errors → GraphBatchFailureError(挂快照) → raise    │
  │      detail() 逐节点递归归一成嵌套 ErrorReport             │
  └────────────────────────────────────────────────────┘
```

> **重试只在节点级**；图级是「熔断 + 三路收口 + 挂快照 resume + 终端通知」，不重试。
> graph 默认是**后台任务**：tool.call() 只返回 `BgTaskResult` 句柄（提交成功），
> 真正跑图的 `_run_driver` 由 **BgTaskPool** 后台调度，其异常被 **pool 的 except**
> 收口（`tasks/pool.py`），**不回**到 ToolExecutor 的 try/except。

---

## 七、统一展示契约（`report.py`）

```
        任意 BaseException
              │
              ▼
   ErrorReport.from_exception(exc)
       ├ MoteError? → 贡献完整契约 code/retryable/recovery/detail
       └ 其他异常 → 降级 UNKNOWN（retryable 复用 is_retryable）
              │
              ▼
       ErrorReport (frozen dataclass, 零依赖叶子)
              │
              ▼  render_error_block() ◄ 唯一渲染器
   <error code="LLM_RATE_LIMIT" recovery="retry" retryable="true">
     message...
     detail key: value...     ← failures 列表逐节点展开
     cause: ...
   </error>
              │
   ┌──────────┼──────────┬──────────────┐
   ▼          ▼          ▼              ▼
 工具结果   graph失败   bg任务通知   task attachment
 （长得完全一致，不管哪个 executor 产生 → 开标签携带机器码让模型推理「怎么反应」）
```

---

## 八、四条贯穿性规律

**规律 1 —— 决策即元数据**：把「重试/恢复决策」从控制流抽出来，变成异常自带的 `retryable`+`recovery`。重试判断收敛到 `is_retryable` 单一真相源，再不靠 `except (厂商A,厂商B)` 元组。

**规律 2 —— 异常层是叶子，只发 hint 不执行**：恢复动作需要业务能力（压缩/轮转/换 provider），异常层去做会成环。`RecoveryRunner` 只拥有控制流，策略由各调用方注入；`is_retryable` 在 `_action_for` 里懒加载、`__init__.py` 用 PEP 562 `__getattr__` 懒导出 handlers——都是为守分层付的真实代价。

**规律 3 —— 一套骨架，按领域注入不同策略**：同一个 `RecoveryRunner`，LLM 注入 COMPRESS/ROTATE/FALLBACK、Graph 注入 RETRY、Tool 留空预留。空注册表等价裸调用，所以默认配置零成本。

**规律 4 —— 一套展示契约，每个边界渲染不重新推导**：`ErrorReport.from_exception` 归一任意异常，`render_error_block` 唯一渲染，tool/graph/task 失败长得一模一样。开标签携带 `code`/`recovery`/`retryable`，让模型推理「怎么反应」而非只知「啥挂了」。




