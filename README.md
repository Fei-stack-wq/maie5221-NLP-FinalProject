# miniagent — 演化式多智能体协作系统（最小骨架）

MAIE5221 NLP 期末项目的起步骨架：**一个能跑、能读、能扩展的单 agent 循环**，把课程要求里
最难的部分（隔离、指标、HITL、可复现）做成可运行的代码，而不是文档里的承诺。

设计取舍只有一条：**不引入任何 agent 框架**。整个系统 900 行左右，每个机制你都能读到源码。
先用它把"完成率/耗时"跑出来，再决定要不要上 LangGraph（见 [docs/agent-dev-primer.md](docs/agent-dev-primer.md) 附录 B）。

> 课程硬约束已内置：模型只走 DeepSeek（`providers.py`），产出只用 md/txt（system prompt），
> UI 只有 CLI（`cli.py`）。**不要**在这里加 PDF/DOCX 解析或前端页面，那些不加分。

---

## 30 秒跑起来

不需要 API key、不需要联网：

```powershell
python -m miniagent demo                     # 单 agent：走一遍完整工具调用闭环
python -m miniagent eval --tasks examples/tasks.jsonl --mock examples/mock_task.json
python -m miniagent orchestrate "整理笔记并写摘要" --mock examples/mock_plan.json
python -m miniagent orchestrate "整理资料" --mock examples/mock_delegate.json --delegate
```

- `demo` / `eval`：单 agent，打印 **任务完成率 / 平均耗时 / P95 / token**。
- `orchestrate`：**目标 → 子任务图 → 调度**。`--scheduler` 可选
  `serial`（默认、可复现）/ `parallel`（按层并发）/ `evented`（依赖完成即解锁）；
  `--delegate` 允许 agent 用 `delegate_task` 再派子任务（深度默认 2）。
- 编排运行时报告会同时给 **子任务完成率**、**总体目标**，以及 `bus.jsonl` 里的群聊记录。

所有产物在 `runs/<run_id>/`：`trace.jsonl`、`plan.json`、`board.json`、`report.md`。
环境要求：Python 3.10+，`pip install -r requirements.txt`（只依赖 pydantic）。

---

## 文件地图：按这个顺序读

| 顺序 | 文件 | 行数 | 你会看到什么 |
|---|---|---|---|
| 1 | [miniagent/rao.py](miniagent/rao.py) | ~690 | **原子单元**。决策闭集、Observation 归一、预算、循环检测、验收、错误分类 |
| 2 | [miniagent/agent.py](miniagent/agent.py) | ~260 | 薄壳：循环 + 预算 + 验收 + 终态映射（只驱动 RAO，不写工具逻辑） |
| 3 | [miniagent/orchestrator.py](miniagent/orchestrator.py) | ~810 | **编排**：计划校验与降级、能力路由、S1/S2/S3 调度、黑板、委派 |
| 4 | [miniagent/bus.py](miniagent/bus.py) | ~140 | 消息总线：群聊/私信/委派事件，落 `bus.jsonl` |
| 5 | [miniagent/tools.py](miniagent/tools.py) | ~400 | 工具协议、参数校验、错误吸收、SSRF 防护 |
| 6 | [miniagent/sandbox.py](miniagent/sandbox.py) | ~105 | 路径越狱防护（隔离的第一道门） |
| 7 | [miniagent/providers.py](miniagent/providers.py) | ~275 | 真实 HTTP 调用 + 可复现的 ScriptedProvider |
| 8 | [miniagent/metrics.py](miniagent/metrics.py) | ~230 | 完成率/耗时/token 口径（含子任务层与总体目标） |
| 9 | [miniagent/schemas.py](miniagent/schemas.py) | ~145 | 数据契约（改这里要想清楚） |
| 10 | [miniagent/cli.py](miniagent/cli.py) | ~420 | 一次 run 的组装方式 |
| 11 | [miniagent/config.py](miniagent/config.py) | ~60 | 护栏的默认值与理由 |

配套文档：
- [docs/architecture.md](docs/architecture.md) — **系统架构设计**（分层、组件、接口契约、指标口径）
- [docs/scheduler.md](docs/scheduler.md) — **调度器设计**（S1 串行 / S2 分层并发 / S3 事件驱动、失败语义、预算、选型）
- [docs/rao-spec.md](docs/rao-spec.md) — **RAO 契约规范**（字段、错误码、终止谓词、单步测试方法）
- [docs/requirements-analysis.md](docs/requirements-analysis.md) — 课程要求逐条拆解

---

## 一次 run 里到底发生了什么

```
CLI
 └─ 为任务创建 Sandbox(runs/<id>/agents/<agent>-<task>)        ← 隔离
 └─ 组装 Agent(provider, registry, sandbox, tracer, acceptance)
     └─ RAOState.history = [system prompt, 用户任务]
     └─ while 预算未耗尽:
          record = RAOUnit.step(state, budget)
            Reason  → provider.chat(历史, 工具清单)   → Decision（闭集五选一）
            Act     → 工具 / ask_human / delegate_task / abort_task
            Observe → Observation(ok, error_class, artifacts) → 追加进历史
          if decision == answer: 跑验收(sandbox)
                通过 → status=completed
                不通过 → 打回一次（注入验收意见）→ 仍不通过则 failed_acceptance
          if terminate_reason（T_LOOP / T_ABORT）→ 对应终态
     └─ 写 task_end；summarize(trace) → 指标 + report.md
```

关键不变量（改代码时别破坏它们）：

1. **完成必须过验收**：`completed` 当且仅当验收函数通过；没有验收函数才算"软完成"。
2. **每个 tool_call 都必须有对应的 tool 消息**，否则下一轮请求会 400。
3. **工具永远不抛异常**，错误变成 `Observation(error_class=...)` 喂回模型，让它自己纠错。
4. **任何耗时/token/错误类都进 tracer**，否则报告无法追溯。
5. **决策是闭集**（act / answer / ask_human / delegate / abort），不接受自由文本决策。

---

## 指标是怎么来的

`runs/<run_id>/trace.jsonl` 是唯一事实来源，一行一个事件：

| kind | 关键字段 |
|---|---|
| `task_start` | task_id, agent, task, sandbox |
| `step` | step, model, latency_ms, prompt_tokens, completion_tokens, tools |
| `tool_result` | tool, ok, preview（前 200 字符） |
| `task_end` | status, duration_ms, steps, tool_calls, tokens, error |

`report.md` 由 `summarize()` 生成，口径固定：

- 任务完成率 = `status == "completed"` 的数量 / 总任务数
- 执行时间 = 每个任务 `duration_ms`（从进入循环到退出），同时给均值与 P95
- 失败模式 = 按 `error` 分类计数（这是报告里"故障模式"一节的数据）

---

## 接真实 DeepSeek

```powershell
Copy-Item .env.example .env      # 填入 DEEPSEEK_API_KEY
$env:DEEPSEEK_API_KEY = "sk-..."  # 或写进系统环境变量（.env 不会被自动加载）
python -m miniagent run "调研 ReAct 方法并写一份 notes.md 笔记"
```

模型选择（2026-10 官网价，非高峰为高峰五折）：

| 模型 | 输入(未命中缓存) | 输出 | 适合 |
|---|---|---|---|
| `deepseek-flash` | 1 元/百万 token | 4 元 | 路由、摘要、大部分子任务 |
| `deepseek-v4-pro` | 4.5 元 | 13.5 元 | 规划、难题推理 |

一个 15 步任务约 0.2 元；把上下文从 8K 膨胀到 50K 就是 6 倍。**省钱三招**：系统提示词放最前面
（命中缓存后输入价降 50 倍）、模型分层、批量评测放非高峰。

没 key 时怎么办：把 `mock_task.json` 当作"模型剧本"，照样能开发调度器、沙箱、指标——这条链路
完全不依赖模型质量。**密钥晚发不是进度风险，架构耦合才是。**

---

## 沙箱的诚实边界

现在是**软隔离**：独立目录 + 路径校验（绝对路径、`..`、盘符、符号链接越界全部拒绝）。
它能挡住"手滑"和"被提示词诱导去读别的 agent 的文件"，**挡不住**恶意代码主动访问任意绝对路径
（`run_python` 里写 `open(r"C:\Windows\...")` 依然能过）。

要硬隔离，在 `Sandbox` 外层换实现即可：独立子进程 + 受限用户 → Docker 容器（只挂载自己的目录）。
写架构文档时把"软做到什么程度、硬隔离成本多少"讲清楚，比假装安全更能拿分。

---

## 扩展路线（按拿分优先级）

| 阶段 | 加什么 | 改哪里 | 对应要求 |
|---|---|---|---|
| ① 现在 | 单 agent + 工具 + 沙箱 + 指标 | — | 2.2/2.3/指标 |
| ② 下一步 | **任务分解**：planner 输出子任务 JSON（含 assignee/deps），调度器按依赖跑 | 新增 `miniagent/orchestrator.py` | 2.1b 自主委派 |
| ③ | **多 agent 群聊**：共享黑板 + 消息总线（谁说了什么、谁负责哪块） | 新增 `miniagent/bus.py` | 2.1a |
| ④ | **记忆与演化**：每个 agent 一份 JSONL（成功/失败/教训），下次注入 system prompt | 新增 `miniagent/memory.py` | 2.2a |
| ⑤ | **HITL 三档**：interactive / semi / unattended（自动批准 + 记录） | 已有 `approve` 回调，扩成策略类 | 2.1c |
| ⑥ | **批量评测 + baseline 对照**：单 agent vs 多 agent 的完成率/耗时对比表 | `cli.py` 的 `_execute` | 报告 |

每一步都要保持"没有 API key 也能跑测试"。

---

## 测试

```powershell
python -m unittest discover -s tests -t . -v
```

96 个用例覆盖：**RAO 单步断言**（决策闭集、观察归一、产物索引、预算扣减、循环告警/终止、
HITL 有/无人、委派有/无编排器、验收通过/打回/失败）、**编排与调度**（计划校验与降级、S1 执行顺序、
失败只阻塞下游、S2 并发重叠与并发上限、**S3 依赖完成即解锁**、深/浅层依赖、委派与深度上限）、
**消息总线**（订阅/广播/落盘/订阅者异常隔离）、沙箱越狱、工具参数校验、护栏（超步数/超时）、
错误吸收、模型响应解析（非法 JSON / 空 choices / BOM 剧本）、指标口径、CLI 端到端。

> 注意：测试的临时目录写在 `build/tmp`（已 gitignore）。有些受限机器上系统 temp 不可写，
> 用 `tempfile` 会全线 PermissionError —— 这是踩过的坑。

---

## 协作规范（关系成绩）

课程明确要求"持续的协作开发过程"，期末批量上传会被重罚。所以：

- 每人一个 `feature/<名字>` 分支，走 PR 合并，**小步频繁提交**
- commit message 用 `feat:/fix:/docs:/test:` 前缀
- 每次提交前跑一遍测试
- 不要提交 `runs/`、`build/`、`.env`

---

## 已知限制（写进报告的"未来工作"）

- 软隔离，不是安全边界（见上）
- 单 agent，尚无编排器
- `http_get` 没有关闭 3xx 自动跳转，存在被重定向到内网的可能（代码里有 TODO）
- ScriptedProvider 的 token 数是估算值，不能用于成本核算
- 没有流式输出，CLI 要等整步结束才显示
