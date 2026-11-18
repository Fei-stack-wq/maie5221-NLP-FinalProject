# M0-04：持久任务身份与事务状态

> 2026-11-17（America/Los_Angeles）；基于 M0-03 `21b45e6`，开发分支 `jinyuwu/m0-04-task-store`。本阶段将任务、Attempt、Run、验收历史和待发送事件写入 SQLite，并接入现有运行链路。持久记录可重新打开查询；自动执行恢复、预算账本与外部动作核对仍由后续阶段实现。

## 1. 状态真源和范围

TaskStore 是任务生命周期的持久真源，TaskService 是受信宿主的写入入口。规划器给出计划，Agent 执行并提交结果，调度器请求领取和收尾；它们通过服务更新记录。`SubTask`、Board、trace 和 bus 继续提供兼容输出与投影，不能反过来证明数据库中任务已完成。

| 模块 | 本阶段职责 | 边界 |
| --- | --- | --- |
| `task_store.py` | SQLite schema、事务、记录读写、CAS、图与内部引用校验、幂等键、不可变 blob、outbox | 内部存储 API，不是认证或权限边界 |
| `task_service.py` | 创建、计划登记、原子领取、验收保存、终态联合更新、动作身份映射 | 不自动重试已结束任务、不自动恢复未结束任务 |
| `agent.py` | 领取或接收执行句柄；持久化验收阶段、每次报告、最终 Task/Attempt/Run 状态 | 直接构造 Agent 时可显式注入 service；CLI 始终接入 |
| `rao.py` | 工具执行前登记整批动作身份，记录 provider 调用 ID 与平台动作 ID 的关联 | 身份登记不是授权、工具执行回执或恰好一次执行保证 |
| `orchestrator.py` | 持久 root、原子登记整份计划、领取 child、登记动态委派、从存储判断依赖状态 | 保留三种同步调度算法，不实现 M2 的持久挂起调度 |
| `cli.py` | 为 demo/run/eval/orchestrate 建立控制层数据库；提供 `tasks` 查询 | 不把历史 trace 自动迁移成 verified 任务 |

默认数据库位置为运行目录的 `tasks.sqlite3`，位于 `agents/` 或子任务工作目录之外。Orchestrator 没有 run_dir 时使用临时控制目录；需要跨对象或跨进程共享状态时，显式注入同一路径的 TaskService。原生路径沙箱仍不能约束任意宿主 Python 子进程，真正执行隔离属于 M0-05；SQLite 文件位置不是安全边界。

## 2. 三种执行身份与幂等创建

| 身份 | 类型与来源 | 稳定性 / 唯一性 |
| --- | --- | --- |
| `task_id` | M0-01 `Id`，非空严格字符串；宿主可指定，或生成完整 UUID | 标识逻辑任务；重新打开数据库不改变 |
| `creation_key` | 非空字符串，服务按 project 命名空间绑定语义请求指纹 | 同键同请求返回已有任务；同键异目标/父关系/依赖/分配显式冲突 |
| `attempt_id` | 平台 UUID，或受信调用者显式提供 | 标识一次求解；当前阶段只领取未执行任务，不隐式创建第二次 attempt |
| `run_id` | 平台 UUID，或受信调用者显式提供 | 标识执行实例，绑定 task/attempt；进入 trace 与 RunResult |
| `action_id` | 平台生成 UUID | 与 provider 的 tool_call_id 分离；具体绑定一个 run、step 和批次内调用 ID |
| `tool_call_id` | 原样保留 provider 字符串 | 同批内唯一；后续 step 可以复用，不覆盖旧 action |

原计划的局部 `s1/s2` 仍用于依赖、验收配置和界面展示，持久任务 ID 保留 `parent_task_id:local_id` 兼容映射。动态委派使用独立持久 ID；同一父任务、同一深度的两个真实委派动作会得到两个 child。重放同一动作的创建请求会命中原 child，语义变化则报冲突。

整份计划作为一个事务登记：先解析局部 ID 与依赖，再创建全部节点和事件；未知依赖、跨 root/project 引用、重复标识或组合等待图成环时整批回滚。组合图同时考虑父任务等待 children 和显式 deps，防止 child 依赖尚在等待自己的父任务。

重复调用已经结束的 task_id **不会再次执行模型或工具**。这与早期“同一个 goal ID 再调用一次就开新 attempt”的临时行为不同。独立的新任务使用新 ID；显式 retry 需要后续根预算、deadline 和未核实副作用检查，resume 还需要完整执行检查点，本阶段不绕过这些条件。

## 3. SQLite 事务和并发约束

数据库采用标准库 `sqlite3`，schema version 为 1。新建数据库权限为 0600；已有文件权限不被擅自修改。空库可初始化，非空外部数据库、未知 schema version 或不兼容核心表结构会明确拒绝，不会静默重建。每个操作独立连接，开启 foreign keys、WAL 和 busy timeout；写操作使用 `BEGIN IMMEDIATE`，在多线程和多进程间串行化短写事务。网络请求、工具、验收回调都在数据库事务外执行。

| 存储对象 | 主要内容 | 更新方式 |
| --- | --- | --- |
| records | TaskRecord、AttemptRecord、RunRecord、AcceptanceSpec/Report、Checkpoint 的类型化 JSON 及索引列 | 可变记录 CAS；不可变记录仅插入；Spec 以 ID + version 定位 |
| creation_keys | project 范围内的创建键、语义指纹、task_id | 唯一绑定；冲突拒绝 |
| blobs | 执行上下文、约束兼容绑定、验收回执、动作身份等有限 JSON | 同 ID 同值可重复提交，异值拒绝 |
| outbox | 单调 sequence、event_id、kind、task_id、payload、时间与投递标记 | 与状态同事务追加，投递确认单独事务 |

`expected_revision` 必须是严格整数并等于数据库当前版本。更新再次使用 SQL revision 条件，版本只逐次增加；过期读不会静默覆盖新状态。事务提交前校验最终组合快照，因此可以在一个事务内同时建立相互关联的 Task/Attempt/Run；任何异常均回滚状态、关联、上下文和事件。

同一次领取中，只有一个竞争者能成功。领取返回的 ExecutionHandle 在执行器进入时还需 `attach_execution(handle)` 原子登记一次性消费；第二个执行器复用同一句柄也会被拒绝，不能依靠动作身份幂等而重复执行工具。该标记不提供外部系统执行租约或崩溃恢复保证。第二个执行者要么遇到版本冲突，要么发现任务已被领取，不能获得另一份活动 Run。进程在提交前退出，未提交数据不会变为可见记录；提交后重开连接则可读取相同身份和状态。SQLite busy/磁盘故障会明确失败，不被当成更新成功。

outbox 是持久的待发送记录，`events(after_sequence=...)` 支持游标查询，`pending_events()` 返回尚未确认的事件，`acknowledge(event_id)` 可重复确认。发送成功后、确认前崩溃仍可能造成重复投递，因此消费者应按 event_id 去重；本阶段未把 MessageBus 改造成自动 outbox relay，也不声称恰好一次投递。

## 4. 状态转换与联合写入

沿用 M0-01 类型和纯转换规则，不另建一套字符串状态机。

| 操作 | 前置条件 | 一个事务内的结果 |
| --- | --- | --- |
| create / create_plan | 创建键未占用或匹配；父关系/依赖合法 | created Task、创建绑定、初始上下文/约束身份、事件 |
| start | created/ready，expected_revision 匹配，deps 均 completed；child 的父任务正在运行 | Task ready→running；pending→running Attempt；prepared→running Run；上下文与事件 |
| transition verifying | 当前 running，revision 匹配 | Task verifying；Attempt/Run 保持 running |
| record_acceptance | 当前 verifying，报告 task/attempt/spec 绑定正确 | 保存不可变 Spec/Report/回执；Attempt.report_ids 追加；Task 更新当前验收指针及事件 |
| transition running | 当前 verifying，继续同一次尝试修复 | Task running；原 Attempt/Run 与历史报告保留 |
| finish completed | 有匹配的 passed 报告及 M0-02 回执闭包，后代均已结束 | Task completed、Attempt succeeded、Run ended、相关记录与事件 |
| finish unverified/failed/cancelled | 当前 running/verifying，reason/报告满足契约 | Task 和 Attempt 对应终态、Run ended；保留已有验收历史 |
| cancel | 尚未执行的 created/ready/blocked | Task cancelled + 明确原因；不捏造一次已经执行的 Attempt |

已结束记录不能通过普通 transition 重新打开。运行结果 `error/failed_acceptance/limit_exceeded/partial` 持久为 `failed`，`aborted` 持久为 `cancelled`。未启动的依赖阻断或预算跳过在兼容 SubTask/trace 中仍展示 blocked/skipped，但持久 Task 结束为 cancelled 并保留原因，避免已结束 root 下残留活动 child。

验收失败后修复仍在同一个 Attempt 和 Run 中进行。每次失败/通过报告都存储，不只保留最后一条 trace。无检查器的模型答复为 unverified；传输失败、截断和工具身份登记失败不能被旧 passed 报告掩盖。

## 5. 动作身份与副作用边界

RAO 在执行合法工具批次前，先把整批动作登记到 TaskService。登记失败则整批不执行，写入 `T_STORE` 结果并保留该模型回复已知的 token 用量。`tool_result` trace 带 `run_id/action_id/tool_call_id`，模型协议消息继续只使用 provider 的 tool_call_id。

身份键包含 run、step 和 tool_call_id，绑定 operation、结构化参数与原始参数字符串的摘要。同一键同内容返回相同 action_id；内容冲突拒绝，不能把原动作改名后覆盖。合法有限浮点可以进入这一身份摘要；它不是 M0-01 `ActionRecord.intent_digest` 的规范动作摘要。

委派的 `__runtime_action_id` 由运行时写入并覆盖模型同名参数，Coordinator 用它建立创建键。普通模型参数不能指定一个旧动作身份来覆盖其他 child。

这些记录只证明“该意图获得了持久身份”。它们不表示动作已执行，也不会伪造授权、BudgetLease、ActionRecord.receipt_ref 或外部状态。崩溃后，工具可能尚未执行、执行中或已执行；没有后续 Broker/回执/核对机制时，不能根据身份记录盲目重放。

## 6. 复用 M0-01 与 M0-02 的明确限制

Store 复用 Task/Attempt/Run、验收和 Checkpoint 模型，并检查其内部身份、状态、任务图、活动实例和引用关系。它不声称整个数据库已构成通过完整 `ContractSnapshot` 的工作室：Profile/Project 注册表、BudgetLedger、ArtifactStore、授权与外部动作尚未接入。

兼容阶段使用 local project 命名空间、角色名作为 profile 引用，分配 budget_scope_id 作为未来账本的关联身份；没有对应预算余额或额度放行能力。上下文 blob 记录 objective、角色和 workspace 等启动信息，不是含完整私有模型历史的恢复检查点。

M0-02 本地验收器使用 workspace 作用域的 target project/constraints ID。服务仅对显式 runtime-local 适配器做一次可审计的约束绑定并记录前后引用，避免悄悄抹掉不匹配；该标记不是授权证明。一般跨任务报告、错 attempt 或无证据的 passed 仍拒绝。全局 project 与授权解析由后续模块补齐。

Checkpoint 的不可变存储和内部引用校验是基础能力，现有循环尚不生成可执行恢复点。`resume_candidates()` 只列出需要检查的未结束任务；即使记录仍显示 running，也不能据此判断旧执行者已经死亡或安全重试。没有自动重置预算、换 owner、重放工具或启动模型。

### 6.1 公共查询与命令接口

| 接口 | 输入 / 输出 | 关键约束 |
| --- | --- | --- |
| `store.get(task_id)` / `tasks(root_id=None)` / `children(parent_id)` | 严格非空 ID；返回 M0-01 TaskRecord 快照或列表 | 没有任务时 get 抛 not_found |
| `store.attempts(task_id)` / `runs(attempt_id)` | 对应持久 ID；返回历史记录 | 不生成新 attempt/run |
| `store.get_record(kind, id)` | 已支持的记录类型与 ID | AcceptanceSpec 可传 `(spec_id, version)` 精确定位，字符串取最高版本仅供便利查询 |
| `store.get_blob(blob_id)` | 不可变 blob 身份 | 返回有限 JSON 的副本 |
| `service.create(...)` / `create_plan(parent_id, definitions)` | objective、creation_key、owner/assignee、project、parent/deps；计划带局部 id | 返回已有或新建的 TaskRecord，冲突拒绝 |
| `service.start(task_id, expected_revision=..., context=...)` | 正整数 revision 与 JSON 上下文，可选 attempt_id/run_id | 返回 `ExecutionHandle(task, attempt, run)` |
| `service.attach_execution(handle)` | 完整且当前有效的执行句柄 | 原子登记一次；重复接入拒绝 |
| `service.transition(...)` / `record_acceptance(...)` / `finish(...)` | expected_revision；目标状态或 Spec/Report/receipts | 返回新的 TaskRecord；需显式保存新 revision |
| `service.action_identities(...)` | task/attempt/run、非负 step、包含 tool_call_id/operation/arguments/raw_arguments 的调用列表 | 返回原顺序的身份字典列表，不改变 Task revision |
| `service.resume_candidates()` | 无参数 | 未结束任务列表，仅供审查 |
| `store.transaction()` | 受信内部写事务上下文 | `insert/update/put_blob/bind_creation/emit` 联合提交；调用方须遵循 TaskService 约束 |

StoreError 携带 code，区分 `not_found/conflict/revision_conflict/integrity_error/schema_version` 等失败；SQL busy/磁盘/权限等操作错误也可直接抛出 SQLite/OSError。CLI 将这些情况明确转换为非零退出码，不打印成功结果或覆盖任务状态。底层 transaction 是受信扩展接口，不应交给 Agent 工具；业务完成必须走 TaskService 的验收闭包检查。

## 7. 使用与电脑维护流程

```sh
# 离线运行，产生 trace、报告、工作目录和控制层 tasks.sqlite3
python -m miniagent demo

# 在另一个进程中读取相同持久记录
python -m miniagent tasks runs/<run_id>
python -m miniagent tasks runs/<run_id> --task-id demo-1
```

程序可从 `miniagent` 导入 `TaskStore/TaskService/ExecutionHandle/StoreError`，并注入 `TaskService(TaskStore(path))`，供多个 Coordinator/Agent 共享。关闭一次 Python 进程后，重新构造同一路径的 TaskStore 即可查询；读取历史目录没有数据库时明确提示，不从旧 completed 标签推导真实成功。

电脑维护任务中，“修复 A 项目启动错误”先形成一个稳定 root。计划将“检查启动配置”和“修正配置”登记为两个 child，第二个依赖第一个。调度器领取诊断任务时一次性写 Task、Attempt、Run，再调用模型。模型发起工具调用前取得平台 action_id；如果同一角色接着委派“检查依赖”与“检查启动脚本”，两个委派动作分别创建 child，不因相同父级/深度而相互覆盖。

诊断结束后，独立检查器的报告和回执与终态一起持久化。修复任务只有看到依赖的持久状态为 completed 才能领取。维护 Agent 最终仍需独立 root 验收，不能仅根据所有 child 返回文字宣布成功。调度器只收尾自己取得的执行句柄；遇到其他执行者的活动 claim 时明确报冲突并保留其记录，root 也不会伪造已结束。进程途中退出时，已提交的身份、关系和状态还在；本阶段支持检查这些记录，实际从动作中间继续维修属于后续恢复阶段。

## 8. 验证

```sh
python -m unittest tests.test_task_store tests.test_task_service tests.test_task_store_runtime tests.test_task_store_orchestrator -q
python -m unittest discover -s tests -t . -q
```

验收重点：重开持久化；幂等创建与冲突；全计划回滚；同父同深度委派独立身份；同一动作重放不新建 child；真实多进程 CAS/领取竞争；事务中断回滚；状态/Attempt/Run/outbox 一致性；验收证据与历史报告；动作身份失败零工具副作用；三种调度器和 CLI 查询集成。完整离线回归 **504 项通过**（比 M0-03 新增 87 项），运行命令启用 `-W error::ResourceWarning`；59 个 Python 文件通过 Python 3.10 语法解析检查。实际运行环境为 Python 3.14.7 / Pydantic 2.14.3；未声称已运行全部支持版本。M0-04 测试不访问模型 API。
