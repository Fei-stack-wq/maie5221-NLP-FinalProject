# 调度器设计（S1 · S2 · S3）

> 本文是调度器的**唯一详细说明**：三种实现的算法、失败语义、预算、指标口径与选型依据。
> 架构位置见 [architecture.md](architecture.md) §4.3；执行单元见 [rao-spec.md](rao-spec.md)。

| 项 | 内容 |
|---|---|
| 版本 | v1.0 |
| 状态 | 已实现（`miniagent/orchestrator.py`、`miniagent/bus.py`） |
| 测试 | `tests/test_orchestrator.py`、`tests/test_bus.py`（22 个用例） |
| 对应要求 | 2.1a 群聊 / 2.1b 自主委派 / 评估指标（子任务 + 总体完成率） |

---

## 1. 调度器在系统里的位置

```
人类目标
   │
   ▼
Planner（LLM 出计划）──► 校验 ──►（失败则降级为单任务）
   │
   ▼
SubTask DAG ──► Scheduler ──► Agent(RAO) × N ──► 黑板 / 总线 / trace
```

三个层次别混：

| 层 | 谁做 | 产物 |
|---|---|---|
| 计划层 | LLM（一次性）+ 代码校验 | `SubTask[]`（含 deps/assignee/acceptance） |
| 调度层 | **纯代码**（不做模型调用） | 决定"谁、什么时候、并发几个" |
| 执行层 | `RAOUnit` | 一步一个 Decision/Observation |

调度的关键设计判断：**计划可以由模型给，但调度必须由代码做**。让模型每步决定"下一个跑谁"，完成率就不可复现，也没法做失败归因。

---

## 2. 三种实现对比

| 维度 | **S1 串行** | **S2 分层并发** | **S3 事件驱动** |
|---|---|---|---|
| 解锁规则 | 拓扑序逐个 | 整层跑完才进下一层 | **任一依赖完成即解锁** |
| 并发 | 1 | 层内 ≤ `max_parallel` | 全局 ≤ `max_parallel` |
| 墙钟 | Σ 全部耗时 | 受层栅栏影响 | 最接近关键路径 |
| 确定性 | **高**（按 id 排序） | 中（同层顺序不定） | 低（按完成时机） |
| 失败定位 | 最易 | 中（日志交织） | 较难（时序相关） |
| 实现复杂度 | 低 | 中 | 中高（队列 + 阻塞传播） |
| 适用 | 跑指标、出报告、演示 | 独立子任务多 | 依赖链深、层内耗时差异大 |

**共同的接口**：`Orchestrator.run(goal, scheduler=..., max_parallel=...)`，只换调度循环，不换数据结构和执行逻辑。

---

## 3. 算法

### 3.1 S1 串行拓扑

```python
for task in topological_order(tasks):     # Kahn 分层后按 id 摊平
    if any(dep failed/blocked/skipped):   # 依赖没成 → 直接 blocked
        continue
    execute(task)
```

- `topological_order` = 广度优先分层 + **层内按 id 排序**，保证同输入得到同顺序。
- 有环时 `validate_plan` 已经抛错（不会走到执行阶段）。

### 3.2 S2 分层并发

```python
for layer in topological_layers(tasks):   # 第 k 层只依赖前 k-1 层
    runnable = [t for t in layer if deps ok]
    with ThreadPoolExecutor(max_workers) as pool:
        list(pool.map(execute, runnable)) # 栅栏：整层等齐
```

- 优点：结构简单、每层结束是一个天然检查点（适合演示与日志分段）。
- 缺点：**层栅栏浪费**——同层最慢的任务拖住下一层，即使下游只依赖同层里那个快的。

### 3.3 S3 事件驱动（依赖完成即解锁）

```python
remaining = {t.id: set(t.deps)}
ready     = sorted([t for t in tasks if not t.deps])
inflight  = {}

while ready or inflight:
    填充: while ready 且 inflight < max_parallel: submit(ready.pop(0))
    等待: futures.wait(inflight, return_when=FIRST_COMPLETED)
    处理每个完成的 t：
        t 成功 → 对每个下游 c：remaining[c] -= {t}
                 remaining[c] 空 → ready.append(c); ready.sort(by id)
        t 失败 → 递归把所有下游标 blocked（否则它们永远等不到依赖）
```

关键点：

1. **必须 `FIRST_COMPLETED`**，否则退化成 S2。
2. **失败要传递性阻塞**：上游失败 → 下游 blocked → 再下游也 blocked；只标一级会让调度循环空转（`ready` 与 `inflight` 都空、但还有 pending 任务）。
3. `ready` 每次按 id 排序，至少让同输入下的初始顺序确定。

**实测差异**（同一张图：s1 依赖无人、s2 依赖 s1、s3 独立；耗时 0.15 / 0.05 / 0.45 s，并发 2）：

```
S2 分层：s1 与 s3 同层 → 等 s3 的 0.45s 结束后才跑 s2
S3 事件：s1 在 0.15s 完成 → s2 立刻开始（此时 s3 还在跑）
```

`tests/test_orchestrator.py::EventedSchedulingTest` 用时间戳断言了这一点。

---

## 4. 关键机制

### 4.1 计划校验与降级

| 校验项 | 不通过时 |
|---|---|
| `subtasks` 非空、数量 ≤ `max_subtasks` | 抛 `PlanError` |
| id 唯一、objective 非空 | 抛 `PlanError` |
| deps 只能是已存在的 id、不能自依赖 | 抛 `PlanError` |
| 无环（Kahn） | 抛 `PlanError` |
| assignee 不存在 | **不报错**，按技能关键词路由，否则落默认 agent |

失败处理：带错误原文重试 1 次 → 仍失败**降级为单任务**（`plan_degraded` 事件）。宁可跑一个不拆的计划，也不要整盘失败。

### 4.2 失败语义：局部阻塞，不做 fail-fast

```
s1 失败 → s2（依赖 s1）blocked → s4（依赖 s2）blocked
        → s3（与 s1 无关）照常跑
```

理由：完成率 = 通过验收的子任务 / 总数。全局停机会让无关分支的分数一起归零。

### 4.3 预算

| 层级 | 规则 |
|---|---|
| 每个子任务 | `Limits × 0.8` 的独立预算（不然 12 步分给 3 个子任务只剩 2–4 步，跑不动） |
| 全局 | token / 时间池，子任务实际用量回扣；池空 → 剩余任务标 `skipped` |
| 嵌套委派 | `Budget.fork(ratio)` 预留，用于子 agent 再派子任务时切预算 |

### 4.4 黑板与总线

| 结构 | 内容 | 落盘 |
|---|---|---|
| `Board` | 每步摘要、产物索引、错误类 | `board.json` |
| `MessageBus` | `system` / `report` / `delegate_request` / `delegate_result` | `bus.jsonl` |

两个用途要分开：

- **黑板 = 现在是什么状态**（调度器与下一阶段读）。
- **总线 = 谁在什么时候说了什么**（人看、报告引用、群聊演示）。

### 4.5 并发安全

| 风险 | 处理 |
|---|---|
| 多线程写 trace | `Tracer` 内部加锁，整行写入 + flush |
| 多线程写黑板 | `Board.record` 加锁 |
| 共享 provider 游标 | `ScriptedProvider` **不是线程安全的** → CLI 拒绝 `--mock` 配并行/事件驱动 |
| 文件互相覆盖 | 每个子任务独立 sandbox 目录，写者唯一 |
| 预算竞争 | 全局池用锁扣减；单任务预算互不相交 |

### 4.6 委派（真派出去，不是假成功）

```python
agent = Agent(..., delegate_handler=orchestrator._handle_delegate)
```

- RAO 的 `delegate_task` → 编排器创建子任务 `{parent}.d{depth+1}` → **同步执行** → 返回 `Observation`。
- 深度上限 `max_delegation_depth`（默认 2），超限返回 `A_UNSUPPORTED` 并提示自行完成。
- 未启用委派时不假装成功，同样返回 `A_UNSUPPORTED`（诚实降级）。
- 委派过程在总线上留下 `delegate_request` / `delegate_result`，报告可引用。

---

## 5. 事件与指标口径

| 事件 | 写入者 | 用途 |
|---|---|---|
| `plan` / `plan_invalid` / `plan_degraded` | 编排器 | 计划合规率、降级率 |
| `subtask_start` / `subtask_end` | 编排器 | **子任务完成率**、子任务耗时分布 |
| `task_end`（`level=goal`） | 编排器 | **总体目标**状态 |
| `task_end`（无 level） | 各 Agent | 每个子任务自身的执行记录 |
| `orchestration_end` | 编排器 | 目标级状态/耗时/token |

口径（`metrics.summarize`）：

```
总体任务完成率 = 完成数 / 任务总数              （不含 level=goal 行）
子任务完成率   = done 的 subtask_end / 全部 subtask_end
执行时间       = 单任务墙钟；批量另报 P50/P95
```

**并发下的耗时口径要注意**：S1 的墙钟 = 工作量之和；S2/S3 的墙钟受并发与关键路径影响，不能与 S1 直接比较"效率"，只能比较"端到端时间"。报告里建议同时给：总工作量（Σ 子任务耗时）与墙钟（makespan）。

---

## 6. 选型与决策记录

| 场景 | 选 |
|---|---|
| 跑测试集、出对比报告、需要可复现 | **S1 串行**（默认） |
| 子任务彼此独立、要压墙钟 | S2 分层并发（`--scheduler parallel`） |
| 依赖链深、同层耗时差异大 | S3 事件驱动（`--scheduler evented`） |
| 任务集合固定、要求完全确定 | S1 |

**本项目默认 S1**，原因：

1. 评分要"完成率 + 执行时间"，串行版的数字最干净、可复现；
2. S1 是 S2/S3 的**对照组**——报告里"串行 vs 并发"本身就是一张有说服力的实验表；
3. 并发带来的收益在子任务少、LLM 延迟占主导时并不大（实测 3 个 0.2s 任务：0.61s → 0.21s，属于理想情况）。

**已实测数据**（3 个独立子任务，每次模型调用 0.2s）：

| 调度 | 墙钟 | 峰值并发 |
|---|---|---|
| serial | 0.61 s | 1 |
| parallel | 0.21 s | 3 |

---

## 7. 已知限制与扩展方向

| 限制 | 影响 | 扩展 |
|---|---|---|
| 无优先级/抢占 | 长任务占着并发位 | 任务带 priority + 抢占式队列 |
| 无动态重规划 | 计划错了只能局部失败 | 失败后触发 replan（LLM 重排剩余图） |
| 单进程线程池 | 不能跨机器 | 换消息队列（Redis/Celery）即分布式，代价是可复现性 |
| 并发下 trace 交织 | 排障要按 task_id 过滤 | 加 span/parent 字段 |
| 委派是同步递归 | 子任务会阻塞父 agent | 改成异步子任务 + 结果回调（真 actor） |
| 无速率限制感知 | 高并发可能触发限流 | 按 provider 并发上限动态调 `max_parallel` |

---

## 8. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-11-03 | 首版：S1/S2/S3 算法、失败语义、预算、指标口径、选型记录；总线上线 |
