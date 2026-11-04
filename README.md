# miniagent

**一个可运行的多智能体协作系统**：以 RAO（Reason–Act–Observe）为原子单元，在其上叠加
单 agent 运行时、任务编排（串行 / 分层并发 / 事件驱动）、消息总线与指标观测。

面向 MAIE5221 NLP 期末项目。只用 DeepSeek / HKGAI 主干，产出只用 Markdown / 纯文本，
UI 只有 CLI —— 把精力全部放在「AI 工程与编排后端」。

```
版本 0.4.0 · Python 3.10+ · 仅依赖 pydantic · 96 个测试（离线可跑）
```

---

## 它现在能做什么

| 能力                                        | 命令                        | 产出                                           |
| ------------------------------------------- | --------------------------- | ---------------------------------------------- |
| 单 agent 干活（读写文件 / 跑代码 / 抓网页） | `demo`、`run`、`eval` | 产物文件 + 完成率/耗时报告                     |
| 目标拆成子任务图并调度                      | `orchestrate`             | `plan.json` + 子任务完成率 + 总体目标状态    |
| 三种调度：串行 / 分层并发 / 事件驱动        | `--scheduler`             | 可对比的墙钟数据                               |
| agent 之间互相委派                          | `--delegate`              | `bus.jsonl` 里的 `delegate_request/result` |
| 群聊与私信                                  | `bus.jsonl` 频道          | 每个 agent 的 `report` 消息                  |
| 一切操作可追溯                              | `trace.jsonl`             | 每步的决策、耗时、token、错误类                |

不需要 API key 也能跑完整链路（内置 ScriptedProvider）。

---

## 快速开始

```powershell
pip install -r requirements.txt          # 只有 pydantic

# 1) 单 agent 闭环：列目录 → 写文件 → 回读 → 执行代码 → 最终答复
python -m miniagent demo

# 2) 批量指标：任务完成率 / 平均耗时 / P95 / token
python -m miniagent eval --tasks examples/tasks.jsonl --mock examples/mock_task.json

# 3) 目标 → 子任务图 → 调度（默认串行，可复现）
python -m miniagent orchestrate "整理笔记并写摘要" --mock examples/mock_plan.json

# 4) 委派：agent 把写作子任务派给 writer
python -m miniagent orchestrate "整理资料" --mock examples/mock_delegate.json --delegate
```

接真实模型：

```powershell
$env:DEEPSEEK_API_KEY = "sk-..."
python -m miniagent run "调研 ReAct 并写一份 notes.md"
python -m miniagent orchestrate "调研并写报告" --scheduler evented --max-parallel 2
```

> `--mock` 只能配 `--scheduler serial`：`ScriptedProvider` 带游标、不是线程安全的，
> 并发/事件驱动请用真实模型（或用 `agent_factory` 注入独立 provider）。

每次运行在 `runs/<run_id>/` 下留下：

```
trace.jsonl   每步事件（决策/工具/验收/终态）
plan.json     子任务图（降级时标记 degraded）
board.json    黑板快照（每步摘要、产物、错误类）
bus.jsonl     群聊/委派消息
report.md     可直接贴进报告的表
agents/<agent>-<task>/   该子任务的私有工作区
```

---

## 成本与模型选择（2026-10 官网价，非高峰为高峰五折）

| 模型                | 输入（缓存命中） | 输入（未命中） | 输出      | 适合                   |
| ------------------- | ---------------- | -------------- | --------- | ---------------------- |
| `deepseek-flash`  | 0.02 元/M        | 1 元/M         | 4 元/M    | 路由、摘要、多数子任务 |
| `deepseek-v4-pro` | 0.15 元/M        | 4.5 元/M       | 13.5 元/M | 规划、难题推理         |

一个 15 步任务约 0.2 元（flash）；把上下文从 8K 膨胀到 50K 就是 6 倍。
省钱三招：**系统提示词放最前面**（命中缓存后输入价降约 50 倍）、模型分层、批量评测放非高峰。

---

## 架构速览

```
CLI
 └─ Orchestrator          计划校验 → 路由 → 调度（S1/S2/S3）→ 黑板/总线
     └─ Agent             循环 + 预算 + 验收 + 终态映射
         └─ RAOUnit       一步 = Reason → Act → Observe
             ├─ Tools     文件 / 代码 / 网络 / 控制工具
             ├─ Sandbox   每个 agent 私有目录 + 路径越狱防护
             └─ Provider  DeepSeek / Scripted(离线) / HKGAI(预留)
```

| 层   | 一句话职责                    | 文件                                         |
| ---- | ----------------------------- | -------------------------------------------- |
| L0   | 一步的决策、执行、观察归一化  | `rao.py`                                   |
| L1   | 驱动 RAO 到终止，管预算与验收 | `agent.py`                                 |
| L2   | 计划 → 子任务 DAG → 调度    | `orchestrator.py`                          |
| L3   | 群聊/私信/委派事件            | `bus.py`                                   |
| L4   | 事件流 → 指标 → 报告        | `metrics.py`                               |
| 横切 | 工具、沙箱、模型接入          | `tools.py` `sandbox.py` `providers.py` |

三条贯穿全局的判断：

1. **决策是闭集**（`act / answer / ask_human / delegate / abort`），不接受自由文本决策。
2. **完成由代码判定**：`completed` 必须有验收函数通过，不是"模型说做完了"。
3. **指标只有一个来源**：所有数字由 `trace.jsonl` 派生，禁止旁路计算。

---

## 代码地图（按阅读顺序）

| #  | 文件                                                  | 行数 | 你会看到什么                                                                   |
| -- | ----------------------------------------------------- | ---- | ------------------------------------------------------------------------------ |
| 1  | [miniagent/rao.py](miniagent/rao.py)                   | ~690 | **原子单元**：决策闭集、Observation 归一、预算、循环检测、验收、错误分类 |
| 2  | [miniagent/agent.py](miniagent/agent.py)               | ~260 | 薄壳：循环 + 预算 + 验收打回 + 终态映射（只驱动 RAO）                          |
| 3  | [miniagent/orchestrator.py](miniagent/orchestrator.py) | ~810 | **编排**：计划校验与降级、能力路由、S1/S2/S3、黑板、委派                 |
| 4  | [miniagent/bus.py](miniagent/bus.py)                   | ~140 | 消息总线：订阅/广播/落盘，线程安全                                             |
| 5  | [miniagent/tools.py](miniagent/tools.py)               | ~400 | 工具协议、参数校验、错误吸收、SSRF 防护                                        |
| 6  | [miniagent/sandbox.py](miniagent/sandbox.py)           | ~107 | 路径越狱防护（隔离的第一道门）                                                 |
| 7  | [miniagent/providers.py](miniagent/providers.py)       | ~275 | DeepSeek（urllib + 重试）与可复现的 ScriptedProvider                           |
| 8  | [miniagent/metrics.py](miniagent/metrics.py)           | ~230 | 任务/子任务/目标三层口径与报告生成                                             |
| 9  | [miniagent/cli.py](miniagent/cli.py)                   | ~410 | 五个命令的组装方式                                                             |
| 10 | [miniagent/schemas.py](miniagent/schemas.py)           | ~141 | 数据契约（改这里要全体 review）                                                |
| 11 | [miniagent/config.py](miniagent/config.py)             | ~60  | 护栏默认值与理由                                                               |

---

## 文档

| 文档                                        | 内容                                                               |
| ------------------------------------------- | ------------------------------------------------------------------ |
| [docs/architecture.md](docs/architecture.md) | 系统架构：分层、组件状态、事件表、接口契约、安全模型、课程要求对照 |
| [docs/scheduler.md](docs/scheduler.md)       | 调度器：S1/S2/S3 算法、失败语义、预算、指标口径、选型与实测        |
| [docs/rao-spec.md](docs/rao-spec.md)         | RAO 契约：字段、15 个错误码、终止谓词、单步测试方法                |

---

## 指标口径

```
任务完成率     = completed 的 task_end（不含 level=goal）/ 总数
子任务完成率   = done 的 subtask_end / 全部 subtask_end
总体目标       = orchestration_end.status
执行时间       = 单任务墙钟；批量另报 P50 / P95
失败分类       = 按 error_class 计数（R_ 决策 / A_ 执行 / O_ 观察 / T_ 终止）
```

`report.md` 自动生成：总览、失败模式、总体目标、子任务表、逐任务明细。

---

## 命令参考

| 命令                     | 作用                   | 主要参数                                                                             |
| ------------------------ | ---------------------- | ------------------------------------------------------------------------------------ |
| `demo`                 | 离线单 agent 闭环      | `--runs-dir`                                                                       |
| `run "<任务>"`         | 单任务（默认真实模型） | `--mock` `--agent` `--yes`                                                     |
| `eval`                 | 批量任务集 → 指标     | `--tasks` `--mock`                                                               |
| `orchestrate "<目标>"` | 编排执行               | `--scheduler serial\|parallel\|evented` `--max-parallel` `--delegate` `--mock` |
| `report <run_dir>`     | 重新汇总某个 run       | —                                                                                   |

`--yes`：无人值守模式（自动批准危险操作、自动应答人类提问，并留痕）。

---

## 测试与开发

```powershell
python -m unittest discover -s tests -t . -v
```

96 个用例，全部离线（不需要 key、不联网）：RAO 单步断言、沙箱越狱、工具参数校验、
护栏（超步数/超时）、验收打回、计划校验与降级、S1/S2/S3 调度差异、失败局部传播、
委派与深度上限、总线订阅/广播、指标口径、CLI 端到端。

开发约定：

- 每个模块必须能**单独跑**（mock 优先），不依赖别人在场；
- 改 `schemas.py` 要全体 review；
- 每次提交前跑测试；提交信息用 `feat: / fix: / docs: / test: / chore:` 前缀。

---

## 课程约束遵守情况

| 约束               | 状态                                                       |
| ------------------ | ---------------------------------------------------------- |
| 2.1a 私信 + 群聊   | ✅ `MessageBus`（`recipients` + `*`/`all` 广播）   |
| 2.1b 自主委派      | ✅ Planner 出 DAG + `delegate_task` 真派发（限深 2）     |
| 2.1c HITL          | ✅ `ask_human` 决策 + `approve` 审批（跨进程恢复未做） |
| 2.2a 记忆与演化    | ⏳ P4                                                      |
| 2.2b 沙箱隔离      | ✅ L1 软隔离（分级与残余风险见架构文档）                   |
| 2.3 行动空间       | ✅ 文件 / 代码 / 网络；⏳ git 提交推送                     |
| 2.4 只用 md/txt    | ✅ 依赖里没有 docx/pdf 库                                  |
| 2.5 极简 UI        | ✅ 纯 CLI                                                  |
| 2.6 DeepSeek/HKGAI | ✅ Provider 抽象；HKGAI 预留                               |
| 评估指标           | ✅ 完成率 / 耗时 / token / 失败分类自动产出                |

---

## 已知限制

- 隔离是**软隔离**（目录 + 路径校验），不是安全边界；
- `http_get` 未禁用 3xx 自动跳转；
- `ScriptedProvider` 的 token 是估算值，不能用于成本核算；
- 并发/事件驱动不支持 mock（线程安全限制）；
- 委派是同步递归（会阻塞父 agent 那一步），不是完整 actor 运行时；
- 无优先级 / 抢占 / 限流感知。

## 路线图

| 阶段   | 内容                           | 状态                  |
| ------ | ------------------------------ | --------------------- |
| P0–P2 | 骨架 → RAO → 编排（S1/S2）   | ✅                    |
| P3     | 总线、S3 事件驱动、委派        | ✅（HITL 持久化待做） |
| P4     | 记忆与演化 + A/B 实验          | ⏳ 下一步             |
| P5     | 批量评测 + 基线对照 + 报告打磨 | ⏳                    |
