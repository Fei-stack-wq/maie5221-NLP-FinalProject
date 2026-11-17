# M0-02：独立验收与逻辑任务指标

> 2026-11-16。基于 M0-01 提交 `fa6d928`，开发分支 `jinyuwu/m0-02-acceptance`。本轮接入现有单进程执行流程；没有实现 TaskStore、ArtifactStore、预算账本、真实隔离或持久等待恢复。
> 数据结构继续遵循 [M0-01 核心契约](m0-01-data-contracts.md)，阶段范围见 [开发计划](agent-studio-development-plan.md)。

## 1. 行为变化

模型最终答复是候选结果。只有控制层配置的检查器产生有效、可追溯的 passed 报告，任务才能算成功。

| 场景 | 运行结果 | 后续行为 |
|---|---|---|
| 未配置检查器 | unverified | 保留模型答复，不继续调用模型以等待凭空出现的验收 |
| 文件/内容检查失败 | failed_acceptance（修复次数耗尽后） | 按现有上限打回修复，重验沿用同一 attempt 与固定 Spec |
| 检查器异常、返回类型错误或配置不匹配 | error | 记录错误报告；不把检查器故障当作产物不合格反复重试 |
| 所有必需检查通过且执行正常结束 | completed | 记录 Spec、Report 和检查回执 |
| 超时、模型异常、主动中止 | 保留原执行状态 | 不能由旧 passed 报告覆盖本次致命错误 |

`RunResult.succeeded` 同时核对状态、task/attempt 身份、Spec/Report 与证据闭包。旧历史数据仍可读取，但单独一个 `status="completed"` 不再代表经过验证的成功。

## 2. 模块职责

| 模块 | 职责 |
|---|---|
| `acceptance.py` | 将宿主检查器绑定为固定 Spec；执行检查，生成 Report 与真实调用/文件观察回执 |
| `acceptance_evidence.py` | 核验报告引用的回执闭包、调用身份与环境、资源版本；不鉴定外部事实真实性 |
| `agent.py` | 在运行开始固定验收配置；收到候选答复后验收；区分 failed/unverified/error/passed；保存每次重验记录 |
| `orchestrator.py` | 独立 root 验收；登记全部逻辑子任务；保留未验证状态并阻断依赖；汇总动态委派 |
| `metrics.py` | 按逻辑 root/child 去重统计；保留缺失终态和未启动题；只计有报告及回执的成功 |
| `cli.py` | 加载用户提供的确定性文件规则，预登记评测题目，输出统一统计 |

检查器来自宿主代码或用户提供的验收配置。模型计划里的自然语言 `acceptance` 字段仅描述期望，不会被自动转成授权检查器，也不能覆盖控制层的验收配置。

## 3. Python 接口与证据

旧 `Callable[[Path], tuple[bool, str]]` 仍可作为显式宿主检查器。返回值严格要求 bool 和非空说明；`("false", "...")` 等返回值属于 error。

```python
from miniagent.acceptance import accept_files

result = agent.run(
    "修复启动配置",
    task_id="repair-A",
    acceptance=accept_files(
        "launch.txt", min_bytes=1,
        contains={"launch.txt": "python=project-venv"},
    ),
)
assert result.succeeded  # 只有实际检查通过时成立
```

默认文件检查只证明文件存在、字节数和行数满足要求；需要证明内容时明确提供 `contains` 或 `sha256`。hash 接受小写 64 位十六进制，可带 `sha256:` 前缀。回执保存实际读取字节的 hash、大小、相对路径、观察时间与资源版本；不存在的文件记录 absent。

`bind(checker, workspace, task_id=..., objective=...)` 将运行期模板固定为 `AcceptanceBinding(spec, evaluator)`。同一 attempt 的修复重验复用该 Binding，每次生成独立 Report。显式 Binding 可直接使用 M0-01 Spec；多检查项检查器返回 `{criterion_id: (bool, reason)}`，必须完整覆盖，最终 verdict 仍按 M0-01 required 规则聚合。

`evaluate(binding, workspace, task_id=..., attempt_id=..., objective=...)` 返回 `AcceptanceEvaluation`，包含 `.spec`、`.report`、`.receipts`；`.trace_payload()` 可序列化为 `{spec, report, receipts}`。传入的 task 和实际 workspace 必须与显式 Spec 匹配。`workspace_environment_id` 与 `workspace_resource_id` 用于构造该本地目标。

普通回调的证据是“宿主检查器确实调用并返回了这个结果”，不自动证明其内部检查充分。文件检查额外采集目标资源版本。普通回调无法采集的显式 target resources、需要 ArtifactStore 的 output roles，以及 human/LLM 类型验收器，在当前适配器中明确为 unverified。

生成的 `runtime-local:*` 主体、约束与检查器标识只在本次进程中表达绑定，不是已经落库的权限、版本注册或持久任务身份。文件观察不冒充已注册、获授权的 ArtifactRecord。无 tracer 时，最终结果仍保留 Spec、Report 与 `acceptance_evidence`；启用 tracer 后每次失败/成功重验都保留。

文件读取使用 POSIX 目录描述符与 no-follow，拒绝越界、符号链接、非普通文件和检测到的读取期间变更；它不提供运行工具的操作系统隔离，也不能保证检查后的文件永远不变。目前该文件检查器需要 `O_DIRECTORY/O_NOFOLLOW` 能力；不支持这些能力的平台（包括当前 Windows 路径）返回 error，不降级为可跟随链接的读取。普通宿主回调与数据模型不依赖该文件读取实现。Windows 文件适配仍需专门实现及实机验证。

## 4. root、子任务与工作目录

Orchestrator 额外接受 `root_acceptance`；`acceptances`/`default_acceptance` 只适用于子任务。root 检查针对本次 root workspace 的实际产物执行，不从 done 数量构造 passed。子任务均通过但 root 目标文件错误时，root 为 failed_acceptance；root 缺检查器为 unverified；即使 root 检查通过，执行中存在必要子任务未完成或致命错误也不能算 completed。

每次执行生成新的 attempt_id 与独立目录，防止重试误读上一轮文件：

```text
run_dir/
  trace.jsonl
  report.md
  plan.json / board.json       当前运行的总览
  workspaces/<attempt_id>/    root 检查器收到的实际目录
    agents/task-s1/...        当前 s1 的工作目录
    agents/task-s2/...
```

常用 `s1` 对应 `agents/task-s1`；特殊子任务 ID 使用安全编码，不能借 ID 逃逸目录。`OrchestrationResult.workspace` 返回当前 root workspace。相同逻辑题重试时显式复用 `goal_id`（兼容旧 `task_id` 参数），attempt 与 workspace 仍更新；不传逻辑题 ID 则生成新 root。

三种调度器均只允许 verified done 满足依赖；unverified、失败、blocked、skipped 不会解锁下游，无关分支继续执行。动态委派使用运行内唯一 ID、登记 parent，并纳入返回结果与统计。这里只修复运行内身份和分母，持久幂等、事务领取及崩溃恢复仍属 M0-04/M2。

## 5. CLI 与可复现示例

```sh
# 明确检查生成内容，成功时退出码为 0
python -m miniagent run "生成离线说明 result.md" --mock examples/mock_task.json --acceptance examples/acceptance-file.json

# 每题使用自己的规则，题目目标与 mock 实际行为相符
python -m miniagent eval --tasks examples/verified_mock_tasks.jsonl --mock examples/mock_task.json

# 子任务验收与 root 验收分别配置
python -m miniagent orchestrate "整理笔记并写摘要" --mock examples/mock_plan.json --acceptance examples/acceptance-plan.json
```

单任务/默认验收文件格式为 `{"files":[{"path":"result.md","contains":"全程离线"}],"min_bytes":20,"min_lines":1}`。`sha256` 可替代或补充 contains。编排配置为 `{"root": <文件规则>, "subtasks": {"s1": <文件规则>}}`；路径分别相对于当前 root 或对应 child workspace。

JSONL 每题可增加 `acceptance` 对象，优先于 `eval --acceptance` 默认规则；显式 null 表示该题无检查器。任务清单中的非法 JSON、空目标和重复 task_id 会被拒绝，不再静默跳过；单题验收配置/Provider 初始化失败记录为该题 error，并继续其他题。

原 `examples/tasks.jsonl` 没有独立验收；配合原 mock 运行时会输出 unverified，退出码为 1，不再声称三道实际不同的任务全部完成。编排计划中的自然语言标准也不会替代用户规则。CLI 退出码约定：全部 verified 为 0，任务失败/未验证为 1，配置/清单错误为 2。

## 6. trace v2 与指标口径

trace v2 使用 `root_id/task_id/attempt_id/level` 关联，M0-01 记录的 `schema_version=1` 保持不变。

- `case_registered` 在执行前登记全部指定 root；计划和委派登记 `task_registered`。
- `task_start/end`、`subtask_start/end` 与 `orchestration_start/end` 表达实际运行及控制层投影；同一逻辑任务不因多种终态事件而重复计数。
- `acceptance` 保存 Spec/Report/receipts；终态也包含最终报告及证据，检查器异常不会被计为任务成功。
- `step` 和 `plan_usage` 记录实际模型调用的原始 usage；规划重试同样计入。root 汇总 token 不再重复加到子调用之上。

`summarize(events, expected_cases=None)` 与 `render_report(..., expected_cases=None)` 可接收固定题目清单（ID 字符串或带 task_id/root_id/id 的对象）。提供清单时，额外发现的 root 单独列在 unexpected_goals，不改变指定题分母；未提供时从登记/运行记录建立题目集合。

| 指标 | 定义 |
|---|---|
| Goal TCR / completion_rate | 独立验收通过且正常完成的逻辑 root / 全部指定或登记的逻辑 root |
| Subtask TCR | verified done 的逻辑 child / 全部创建的逻辑 child，包括动态委派、blocked、skipped、未开始 |
| 重试 | 同 task 的不同 attempt；不增加逻辑题数，采用最新 attempt 的结果，保留历史消耗 |
| 缺失结果 | 有 start 无 end 为 incomplete，仅登记为 not_started，均留在分母 |
| 历史 completed | 没有可绑定的 Spec/Report/回执时为 unverified，不补造证据 |
| 耗时 | 分别列 root 墙钟耗时之和与子任务耗时之和；都不能当作并发批次的端到端墙钟时间 |
| 活跃/等待时长 | 无实际记录则 null/未记录，不填 0；真实计量后续接预算/等待运行时 |

兼容的 `total_tasks/completed/completion_rate` 现在指逻辑 root。多 root 输出逐题明细，`goal_status="multiple"`，不再只取最后一个；未知时间与未知 usage 显式统计。报告核对记录之间的一致性，不替代可信存储、事件签名或外部环境复验。

## 7. 验收与后续边界

本轮完整离线测试共 296 项通过（比 M0-01 增加 92 项），包括原 108 项契约测试；原示例 JSON 保持不变。验证命令：`python -m unittest discover -s tests -t . -q`。运行环境为 Python 3.14.7、Pydantic 2.14.3；未声称已验证所有支持版本。

本轮离线测试覆盖 A02：文件缺失、内容不符、无检查器、异常检查器、修复后重验、子任务全通过但 root 不满足；覆盖 A16 的当前运行期部分：多 root、固定题目分母、重试、重复事件、未启动/缺终态、规划与子调用成本、独立并行耗时字段。

M0-01 的完整电脑维护示例仍是纯数据 fixture，未改写其 JSON，也没有声称已维修真实主机。新增 CLI 示例证明离线执行及文件验收闭环，不证明真实模型协议、ArtifactStore 工件交接或安全执行隔离。

仍待后续阶段：持久 TaskStore/事务、完整 grant 与 ArtifactStore resolver、真实调用预算、Windows 文件检查、实时活跃/等待时钟，以及当前进程外的权限与环境证明。课程评测涉及的真实模型、搜索、Git 工具与跨任务演化证据保持原阶段安排。
