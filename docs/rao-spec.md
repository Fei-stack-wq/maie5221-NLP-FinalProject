# RAO 契约规范（Reason · Act · Observe）

> 本文是 `miniagent/rao.py` 的**定义文档**：字段、闭集、错误分类、终止谓词、验收规则。
> 改代码前先看这里，改完记得同步（代码与本文不一致时，以本文为准再修代码）。
>
> 相关文档：[系统架构](architecture.md) · [要求分析](requirements-analysis.md)

| 项           | 内容                                                       |
| ------------ | ---------------------------------------------------------- |
| 版本         | v1.0                                                       |
| 状态         | 已实现（`miniagent/rao.py`）                             |
| 测试         | `tests/test_rao.py`（21 个用例，单步断言）               |
| 对应课程要求 | 2.1b 委派 / 2.1c HITL / 2.2a 记忆演化（注入点） / 评估指标 |

---

## 1. 定位与边界

**RAO 负责"一步"，不负责"一路"。**

| 属于 RAO                        | 不属于 RAO                          |
| ------------------------------- | ----------------------------------- |
| 把模型回复解析成闭集决策        | 决定任务怎么拆（属于编排器 L2）     |
| 执行动作、把结果归一成观察      | 重试策略与预算分配（属于运行时 L1） |
| 检测重复动作并发出终止信号      | 决定"完成"是否成立（属于验收函数）  |
| 把观察追加进历史（append-only） | 多 agent 共享状态（属于黑板 L3）    |

这条边界很重要：**RAO 里没有"规划"**。规划每步重做一次会导致行为不稳定、成本翻倍；规划属于 L2，RAO 只消费"当前子目标"。

形式化：

```
s_{t+1} = Observe( Act( Reason(s_t) ) )
```

---

## 2. 数据契约

### 2.1 `RAOState`（一步的输入）

| 字段           | 类型       | 说明                                                         |
| -------------- | ---------- | ------------------------------------------------------------ |
| `goal`       | str        | 本任务/子任务的目标                                          |
| `task_id`    | str        | 用于 trace 与观察的 step_id 前缀                             |
| `step_index` | int        | 已执行步数，`step()` 入口自增                              |
| `history`    | list[dict] | OpenAI 消息格式，**只追加**                            |
| `scratch`    | dict       | 临时标记：`repeat`（重复计数）、`acceptance_failures` 等 |

不变量：`history` 永不重排、永不删除（可重放、可审计）。

### 2.2 `Decision`（Reason 的输出，闭集五选一）

| `kind`      | 触发条件                            | 由谁处理                          | 产生的观察                       |
| ------------- | ----------------------------------- | --------------------------------- | -------------------------------- |
| `act`       | 模型返回了 `tool_calls`           | RAO `act()`                     | 每个动作一条 `Observation`     |
| `answer`    | 模型返回纯文本（无 `tool_calls`） | L1 运行时 → 验收                 | 无（终止候选）                   |
| `ask_human` | 模型调用 `ask_human` 控制工具     | RAO `_ask_human()`              | 人类答复，或 `A_HUMAN_DENIED`  |
| `delegate`  | 模型调用 `delegate_task`          | RAO `_delegate()` → 编排器回调 | 子任务结果，或 `A_UNSUPPORTED` |
| `abort`     | 模型调用 `abort_task`             | RAO `_run_control()`            | 终止信号 `T_ABORT`             |

```python
class Action(BaseModel):
    id: str                  # 对应 tool_call.id，回填 tool 消息时必须匹配
    tool: str
    arguments: dict
    raw_arguments: str       # 原始字符串，排查用
    parse_error: str | None  # 模型输出非法 JSON 时非空
```

**不变量**：`Decision.kind` 只能取上表五个值。任何自由文本决策都必须先落到 `answer`。

### 2.3 `Observation`（Act 的输出，唯一合法形状）

| 字段            | 类型        | 说明                                                       |
| --------------- | ----------- | ---------------------------------------------------------- |
| `step_id`     | str         | `{task_id}:s{step}`                                      |
| `source`      | str         | 工具名 / `human` / `delegate` / `abort` / `system` |
| `ok`          | bool        | 是否成功                                                   |
| `content`     | str         | 已截断的文本（喂回模型的就是它）                           |
| `error_class` | str \| None | 失败时必填，见 §4                                         |
| `artifacts`   | list[str]   | 产物相对路径，供验收与黑板索引                             |
| `meta`        | dict        | 工具自定义元数据                                           |
| `bytes`       | int         | 内容字节数（上下文预算用）                                 |
| `truncated`   | bool        | 是否发生截断                                               |

**不变量**：`ok=False` 时 `error_class` 必须非空；工具层不抛异常，所有失败都走这里。

回灌格式（模型看到的样子）：

```
{"role": "tool", "tool_call_id": "...", "content": "[A_SANDBOX_DENY] 沙箱拒绝访问：路径越界"}
```

前缀错误类是为了让模型能区分"我参数写错了"和"这条路被禁了"，从而换策略而不是重复重试。

### 2.4 `Budget`（四维预算）

| 维度             | 默认值                               | 来源                                       |
| ---------------- | ------------------------------------ | ------------------------------------------ |
| `steps_left`   | 12                                   | `Limits.max_steps`                       |
| `seconds_left` | 120                                  | `Limits.max_seconds`（由驱动方每轮刷新） |
| `tokens_left`  | 200000                               | `Limits.max_total_tokens`                |
| `actions_left` | max_steps × max_tool_calls_per_step | 派生                                       |

- `spend()` 只会让数字变小；`exhausted` 在任一维度 ≤0（token 为 <0）时成立。
- `reason()` 的判定顺序固定为 **时间 → 步数 → 动作数 → token**，保证同一个 run 报告的原因稳定。
- `fork(ratio)` 把剩余预算切一块给子任务（默认各半），用于委派。

---

## 3. 控制工具：决策闭集在工具协议里的落地

模型的协议只有"调工具 / 回文本"，因此 `ask_human` / `delegate_task` / `abort_task` 是**工具形态的特殊决策**：RAO 拦截它们的名字，转成对应 `kind`，同时仍然回一条 `role=tool` 消息（否则下一轮请求 400）。

| 工具名            | 参数                                          | 无 handler 时                                |
| ----------------- | --------------------------------------------- | -------------------------------------------- |
| `ask_human`     | `question`                                  | `A_HUMAN_DENIED`（无人值守，提示自行判断） |
| `delegate_task` | `objective`, `assignee?`, `acceptance?` | `A_UNSUPPORTED`（未接编排器，提示自己做）  |
| `abort_task`    | `reason`                                    | 直接终止：`T_ABORT`                        |

设计意图：**降级要诚实**。没有编排器时，委派不是"假装成功"，而是明确告诉模型"这条路不通，请自己做"。

---

## 4. 错误分类（报告里的失败模式就是按这个统计）

| 常量                    | 阶段    | 触发                            | 是否建议重试                     |
| ----------------------- | ------- | ------------------------------- | -------------------------------- |
| `R_INVALID_DECISION`  | Reason  | 既没调工具也没给答复 / 不可解析 | 是（带错误重试一次）             |
| `R_PROVIDER_ERROR`    | Reason  | 网络、限流、鉴权失败            | 是（指数退避，由 Provider 层做） |
| `A_INVALID_ARGS`      | Act     | 参数非法 JSON / 不符合 schema   | 是（把校验错误回灌）             |
| `A_UNKNOWN_TOOL`      | Act     | 模型幻觉出不存在的工具          | 是（回灌可用工具列表）           |
| `A_SANDBOX_DENY`      | Act     | 路径越界                        | **否**（改路径，不是重试） |
| `A_HUMAN_DENIED`      | Act     | 人类拒绝 / 无人可问             | **否**（换方案）           |
| `A_TOOL_TIMEOUT`      | Act     | 工具超时                        | 是（放大超时重试一次）           |
| `A_TOOL_ERROR`        | Act     | 工具内部异常                    | 视情况（同参数重试无意义）       |
| `A_SKIPPED`           | Act     | 超过单步动作上限                | 否                               |
| `A_UNSUPPORTED`       | Act     | 当前形态不支持（如未接委派）    | 否                               |
| `O_TRUNCATED`         | Observe | 输出被截断                      | 否（信息可能不全，需换查询）     |
| `T_ACCEPTANCE_FAILED` | 终止    | 验收多次未通过                  | 打回一次后终止                   |
| `T_LOOP`              | 终止    | 重复动作达阈值                  | 终止                             |
| `T_BUDGET`            | 终止    | 预算耗尽                        | 终止                             |
| `T_ABORT`             | 终止    | 模型主动放弃                    | 终止                             |

分类函数是廉价启发式（`classify_failure`），升级路径是让工具直接返回 `error_class`（`ToolResult.meta["error_class"]`），避免文本匹配。

---

## 5. 终止与验收

### 5.1 终止谓词

| 谓词                  | 判定者                     | 结果 `status`       | `error`                       |
| --------------------- | -------------------------- | --------------------- | ------------------------------- |
| `T_accept` 通过     | 验收函数（代码）           | `completed`         | 空                              |
| `T_accept` 多次失败 | 运行时（默认重试 1 次）    | `failed_acceptance` | `T_ACCEPTANCE_FAILED: <原因>` |
| `T_budget`          | `Budget.exhausted`       | `limit_exceeded`    | 预算原因文本（含"最大步数"等）  |
| `T_loop`            | RAO 重复计数 ≥5           | `limit_exceeded`    | 含 `T_LOOP`                   |
| `T_abort`           | 模型 `abort_task`        | `aborted`           | "任务被主动中止"                |
| `T_error`           | Provider 异常 / 运行时异常 | `error`             | 异常摘要                        |

### 5.2 验收函数

```python
Acceptance = Callable[[Path], tuple[bool, str]]   # (workspace) -> (ok, reason)
```

三种写法（按成本递增，按任务选）：

| 写法                          | 适用       | 例子                                       |
| ----------------------------- | ---------- | ------------------------------------------ |
| 文件存在 + 非空 + 行数下限    | 绝大多数   | `accept_files("notes.md")`               |
| 结构断言                      | 有格式要求 | 标题数 ≥3 / JSON schema 通过              |
| 执行断言脚本 / reviewer agent | 关键任务   | `run_python` 跑断言，或第二个 agent 判定 |

规则：

1. **没有验收函数 = 软完成**，`status=completed`，但必须在报告里注明"该批次未启用验收"。
2. 验收函数抛异常 → 判为不通过（`evaluate_acceptance` 兜底），不能把循环带走。
3. 验收失败会以 `[验收未通过] <原因>` 注入历史，给模型一次修正机会（可配置重试次数）。
4. 验收失败次数记入 `scratch`，避免无限打回。

---

## 6. 单步测试方法

不需要网络、不需要 API key、不需要跑完整任务：

```python
unit = RAOUnit(name="worker", provider=ScriptedProvider([...]),
               registry=build_default_registry(), sandbox=Sandbox(tmp), limits=Limits())
state = RAOState(goal="g", task_id="t1", history=[{"role": "user", "content": "g"}])
record = unit.step(state, Budget())

assert record.decision_kind == "act"
assert record.observations[0].artifacts == ["a.md"]
assert record.tool_calls == 1
```

可断言的维度：决策形态、观察内容与错误类、产物索引、预算扣减、终止信号、历史追加。

`tests/test_rao.py` 已覆盖：产物索引、沙箱拒绝分类、闭集解析、空决策、循环告警/终止、
HITL 有/无人、委派有/无编排器、预算切分、验收通过/打回/失败、trace 事件。

---

## 7. 与 ReAct 的区别

| 维度 | 朴素 ReAct              | 本 RAO                                       |
| ---- | ----------------------- | -------------------------------------------- |
| 观察 | 自由文本直接拼回上下文  | 对象化：ok / error_class / artifacts / bytes |
| 决策 | 文本 + 工具调用混在一起 | 闭集五选一，可校验                           |
| 终止 | 模型说停就停            | 外部验收函数判定                             |
| 循环 | 无检测                  | 签名重复 ≥3 告警、≥5 终止                  |
| 重试 | 统一重试                | 按 error_class 分策略                        |
| 测试 | 只能端到端              | 单步断言                                     |

---

## 8. 扩展点（下一位 owner 从这里接）

| 扩展         | 接口                                                                  | 谁来接       |
| ------------ | --------------------------------------------------------------------- | ------------ |
| 多智能体委派 | `RAOUnit(delegate_handler=fn)`，返回 `Observation`                | 编排器 owner |
| 记忆注入     | 在 `RAOState.history` 构造时插入"经验"块；结束后写 `MemoryRecord` | 记忆 owner   |
| HITL 持久化  | `human_handler` 换成"写挂起记录 → 阻塞/恢复"                       | 运行时 owner |
| 上下文压缩   | `Observation.bytes` + 历史滑窗，记 `context_compressed` 事件      | 运行时 owner |
| 硬隔离       | `RAOUnit` 不感知，换 `Sandbox` 实现即可                           | 运行时 owner |

---

## 9. 变更记录

| 版本 | 日期       | 变更                                                                               |
| ---- | ---------- | ---------------------------------------------------------------------------------- |
| v1.0 | 2026-11-03 | 首版：从 `agent.py` 抽出 RAOUnit；确立闭集决策、观察归一、验收驱动终止、错误分类 |
