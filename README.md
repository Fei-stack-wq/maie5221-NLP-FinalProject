# miniagent

**一个可运行的多智能体协作系统**：以 RAO（Reason–Act–Observe）为原子单元，在其上叠加
单 agent 运行时、任务编排（串行 / 分层并发 / 事件驱动）、消息总线与指标观测。

面向 MAIE5221 NLP 期末项目。只用 DeepSeek / HKGAI 主干，产出只用 Markdown / 纯文本，
提供轻量本地网页工作室与 CLI。课程要求界面简单实用，允许基础网页文本界面；
前端围绕真实任务操作与观测，复用同一套 AI 工程与编排后端。

```
版本 0.4.0 · Python 3.10+ · pydantic 与 psutil · 本地网页与离线演示
```

---

## 它现在能做什么

| 能力                                        | 命令                        | 产出                                           |
| ------------------------------------------- | --------------------------- | ---------------------------------------------- |
| 本地网页工作室 | `studio` | 发起任务、任务树、状态、日志、产物与验收报告 |
| 单 agent 干活（读写文件 / 跑代码 / 抓网页） | `demo`、`run`、`eval` | 产物文件 + 完成率/耗时报告                     |
| 目标拆成子任务图并调度                      | `orchestrate`             | `plan.json` + 子任务完成率 + 总体目标状态    |
| 三种调度：串行 / 分层并发 / 事件驱动        | `--scheduler`             | 可对比的墙钟数据                               |
| agent 之间互相委派                          | `--delegate`              | `bus.jsonl` 里的 `delegate_request/result` |
| 群聊与私信                                  | `bus.jsonl` 频道          | 每个 agent 的 `report` 消息                  |
| 持久任务、执行身份与状态查询 | `tasks runs/<run_id>` | `tasks.sqlite3` + 事务事件，重启可查 |
| 一切操作可追溯                              | `trace.jsonl`             | 每步的决策、耗时、token、错误类                |

不需要 API key 也能跑完整链路（内置 ScriptedProvider）。

---

## 快速开始

macOS 可直接双击仓库根目录的 [Start Studio.command](<Start Studio.command>)，启动本地服务并打开默认浏览器；首次使用若没有 `.venv`，启动器会显示环境安装步骤，不会自动安装或启动模型任务。

也可以从终端启动本地网页工作室（在仓库根目录，使用已有虚拟环境）：

```sh
.venv/bin/python -m miniagent studio --port 8765 --runs-dir runs
```

浏览器打开 <http://127.0.0.1:8765>。先选择离线单任务或协作演示，即可看到真实工具执行、任务树、产物和验收结果。真实模型模式读取服务端环境变量；`studio` 还支持从本地 `.env` 加载字面量配置，已有环境变量优先，密钥不发送到浏览器。CLI 的其他命令继续只读进程环境。

工作室支持刷新与重新打开历史运行；自动续跑、角色/skill 管理及人工审批会话属于后续阶段。完整启动、API、视觉参考与边界见 [本地 GUI 说明](docs/local-studio-gui.md)。

原有命令行入口继续保留：

```powershell
pip install -r requirements.txt

# 1) 单 agent 闭环：列目录 → 写文件 → 回读 → 执行代码 → 最终答复
python -m miniagent demo
# 重启后仍可查询本次任务、Attempt 和 Run
python -m miniagent tasks runs/<run_id>

# 2) 批量指标：任务完成率 / 平均耗时 / P95 / token
python -m miniagent eval --tasks examples/verified_mock_tasks.jsonl --mock examples/mock_task.json

# 3) 目标 → 子任务图 → 调度（默认串行，可复现）
python -m miniagent orchestrate "整理笔记并写摘要" --mock examples/mock_plan.json --acceptance examples/acceptance-plan.json

# 4) 委派：agent 把写作子任务派给 writer
python -m miniagent orchestrate "整理资料" --mock examples/mock_delegate.json --delegate
```

接真实模型（DeepSeek 官方 API；2026-11-17 已通过 thinking 关闭/开启两种线上探针）：

```powershell
$env:DEEPSEEK_API_KEY = "sk-..."
python -m miniagent provider-smoke --thinking disabled --output build/probe-disabled.json
python -m miniagent provider-smoke --thinking enabled --output build/probe-enabled.json
python -m miniagent run "调研 ReAct 并写一份 notes.md"
python -m miniagent orchestrate "调研并写报告" --scheduler evented --max-parallel 2
```

默认 `deepseek-flash`、显式关闭 thinking、每次最多输出 300000 token；请求重试与 deadline 可配置。任务默认最多 100 轮、累计 1000000 tokens、3600 秒；网页选项可按任务设置预算，运行时可请求停止，结束后可下载保留相对目录的产物 ZIP。输出截断最多恢复两次，上下文按完整工具批次裁剪，大文件支持分页读取、局部编辑及追加；详见 [Harness 基础能力](docs/harness-foundations.md)。除 `studio` 的本地配置加载入口外，`.env` 不自动加载；变量模板见 [.env.example](.env.example)，完整字段、探针退出码和线上证据限制见 [M0-03](docs/m0-03-provider-protocol.md)。

> `--mock` 只能配 `--scheduler serial`：`ScriptedProvider` 带游标、不是线程安全的，
> 并发/事件驱动请用真实模型（或用 `agent_factory` 注入独立 provider）。

没有配置独立验收时，答复和产物仍保留，但结果为 `unverified`，不会计入成功。原 `examples/tasks.jsonl` 和无验收的委派演示属于这种情况。文件检查器当前需要 POSIX no-follow 能力，Windows 上会明确报错；接口、配置与平台边界见 [M0-02 说明](docs/m0-02-acceptance-and-metrics.md)。

编码成员可用 `run_python` 执行 Python，或在 macOS、Linux 上用 `run_shell` 启动非交互式 `/bin/sh`。两个工具共用工具时限和项目环境，PATH 优先使用 Studio 的 Python 解释器，子进程仅继承选定的系统变量。在 macOS、Linux 上，两者共用输出限额和进程组清理，均记录退出码、耗时和标准输出、标准错误的首尾片段。Shell 还可指定项目内的相对工作目录。Windows 的 Python 工具沿用宿主原生子进程。成员需要跨步使用本地测试服务时，请把服务输出重定向到项目日志。命令以 Studio 进程的系统用户权限运行，工作目录用于定位项目文件。

每次运行在 `runs/<run_id>/` 下留下：

```
tasks.sqlite3 任务、Attempt/Run、验收与事务事件
trace.jsonl   每步事件（决策/工具/验收/终态）
plan.json     子任务图（降级时标记 degraded）
board.json    黑板快照（每步摘要、产物、错误类）
bus.jsonl     群聊/委派消息
report.md     可直接贴进报告的表
agents/<agent>-<id>/    单任务工作区
workspaces/<attempt>/agents/task-<subtask>/   编排子任务工作区
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
浏览器 → 本地 HTTP → StudioService（后台执行、运行查询）
                       ├─ Agent（单任务）
CLI / StudioService ───┴─ Orchestrator          计划校验 → 路由 → 调度（S1/S2/S3）→ 黑板/总线
     └─ Agent             循环 + 预算 + 验收 + 终态映射
         └─ RAOUnit       一步 = Reason → Act → Observe
             ├─ Tools     文件 / 代码 / 网络 / 控制工具
             ├─ Sandbox   每个 agent 私有目录 + 路径越狱防护
             └─ Provider  DeepSeek / Scripted(离线) / HKGAI(预留)
```

| 层   | 一句话职责                    | 文件                                         |
| ---- | ----------------------------- | -------------------------------------------- |
| 入口 | 本地 HTTP 与网页；启动后轮询运行状态 | `studio_server.py` `studio_service.py` `web/` |
| 控制 | 持久任务生命周期与事务状态 | `task_service.py` `task_store.py` |
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
| [docs/agent-studio-integration.md](docs/agent-studio-integration.md) | 个人 Agent 工作室接入评估：代码核验、现有差距、对象边界与阶段建议（提案） |
| [docs/agent-studio-development-plan.md](docs/agent-studio-development-plan.md) | 分阶段开发计划：修复优先级、模块接口、验收门槛、协作分工与首批 PR（提案） |
| [docs/m0-01-data-contracts.md](docs/m0-01-data-contracts.md) | M0-01 核心数据契约 v1：字段、状态、读写职责及独立数据层 API/测试边界 |
| [docs/m0-01-computer-maintenance-example.md](docs/m0-01-computer-maintenance-example.md) | M0-01 完整电脑维护数据示例：任务、验收、预算、工件、审批与恢复（虚构数据） |
| [docs/m0-02-acceptance-and-metrics.md](docs/m0-02-acceptance-and-metrics.md) | M0-02 运行期独立验收、root 检查、trace v2、逻辑任务指标与 CLI 验收规则 |
| [docs/m0-03-provider-protocol.md](docs/m0-03-provider-protocol.md) | DeepSeek thinking/工具协议、请求重试与 deadline、用量来源、L1/L2 探针证据 |
| [docs/local-studio-gui.md](docs/local-studio-gui.md) | 本地网页工作室：操作、服务接口、状态边界、设计参考和验收 |
| [docs/m0-04-task-store.md](docs/m0-04-task-store.md) | SQLite TaskStore、持久身份、幂等委派、CAS/事务事件、重启查询与恢复边界 |

---

## 指标口径

```
任务完成率     = 有有效验收报告及证据、正常完成的逻辑 root / 全部指定或登记 root
子任务完成率   = verified done 的逻辑 child / 全部创建的逻辑 child
总体目标       = 逐 root 独立验收；重试不增加题数，缺终态/未启动仍在分母
执行时间       = 分别统计 root 墙钟和子任务耗时；未知活跃/等待时间保持未记录
失败分类       = 按 error_class 计数（R_ 决策 / A_ 执行 / O_ 观察 / T_ 终止）
```

`report.md` 自动生成：总览、失败模式、总体目标、子任务表、逐任务明细。

---

## 命令参考

| 命令                     | 作用                   | 主要参数                                                                             |
| ------------------------ | ---------------------- | ------------------------------------------------------------------------------------ |
| `studio` | 本地网页任务工作室 | `--port` `--runs-dir` `--env-file` `--no-env` `--open` |
| `tasks <run_dir>` | 查询持久任务及执行历史 | `--task-id` |
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

M0-04 基线有 504 个离线用例；本地网页交付另补服务与 HTTP 测试，验证方法见 [GUI 文档](docs/local-studio-gui.md#7-验证与复现)。无需 API key 或外部模型服务，HTTP 用例仅使用本机回环地址。最初 96 项覆盖：RAO 单步断言、沙箱越狱、工具参数校验、
护栏（超步数/超时）、验收打回、计划校验与降级、S1/S2/S3 调度差异、失败局部传播、
委派与深度上限、总线订阅/广播、指标口径、CLI 端到端。

M0-01 的 108 项测试覆盖核心契约的严格类型、必填字段、状态转换、引用与任务图、验收证据、预算、工件授权、审批链和 checkpoint，并直接校验完整电脑维护数据示例。M0-02 再新增 92 项运行期验收和统计测试；原有“无验收也算成功”的预期已迁移为未验证。M0-03 新增 121 项测试，补充请求协议、thinking 历史、批次回写、重试、超时、用量来源与无副作用探针测试；离线与线上证据分别保存；2026-11-17 另完成两种模式的真实 API 往返，见 [线上记录](docs/m0-03-provider-protocol.md#61-2026-11-17-线上实测记录)。M0-04 再新增 87 项事务、持久身份、并发、崩溃回滚和运行期集成测试。

新数据层通过 [miniagent/contracts/](miniagent/contracts/__init__.py) 显式导入；它提供不可变数据快照、校验和纯状态转换；M0-02 已接入运行期 AcceptanceSpec/Report；M0-04 已接入 Task/Attempt/Run、验收历史和事务事件的 SQLite 持久化，预算账本和安全执行恢复仍待后续模块。使用方法和后续存储/执行职责见 [核心契约第 11 节](docs/m0-01-data-contracts.md#11-独立数据层实现与使用边界)。只运行契约测试：

```sh
python -m unittest discover -s tests -p 'test_contract*.py' -t . -v
```

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
| 2.5 极简 UI        | ✅ 轻量本地网页 + CLI                                                  |
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

以下 P0–P5 保留原仓库编号；当前工作室开发顺序以 [M0–M4 计划](docs/agent-studio-development-plan.md) 为准。M0-UI-01 接入本地任务控制台，角色与 skill 管理随后在 M1 接入，会话与 HITL 在 M2 接入。

| 阶段   | 内容                           | 状态                  |
| ------ | ------------------------------ | --------------------- |
| P0–P2 | 骨架 → RAO → 编排（S1/S2）   | ✅                    |
| P3     | 总线、S3 事件驱动、委派        | ✅（HITL 持久化待做） |
| P4     | 记忆与演化 + A/B 实验          | ⏳ 下一步             |
| P5     | 批量评测 + 基线对照 + 报告打磨 | ⏳                    |

### 长期角色助手

从本地 Studio 侧栏的「角色库」创建助手，填写职责后可以长期私聊或邀请进群。私聊中发送 `请记住：每次修改数据库前先备份。`，下次打开新的私聊仍可使用这条偏好。项目专属资料在角色详情中选择适用项目，技能建议也在这里审核。[个人角色库使用指南](docs/personal-role-library.md) · [执行框架与实验依据](docs/agent-harness-research.md)
