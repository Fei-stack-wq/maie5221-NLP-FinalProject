# M0-01 核心数据契约（v1）

> 2026-11-15；原代码基线 `3e195e2067058d9034831912ef8204f6126fa3cd`。核心模型、字段校验、纯状态转换和完整快照校验已在独立的 `miniagent/contracts/` 包实现，使用边界见第 11 节。M0-01 本身不提供持久事务、真实授权或执行恢复，也不接入执行流程；后续验收接入见 [M0-02](m0-02-acceptance-and-metrics.md)，任务持久化和事务接入见 [M0-04](m0-04-task-store.md)。代码、测试与本文作为 M0-01 的同一交付维护。
> 上层计划：[个人 Agent 工作室开发计划](agent-studio-development-plan.md)。完整数据：[电脑维护任务示例](m0-01-computer-maintenance-example.md)。当前实现：[RAO](rao-spec.md)、[编排](scheduler.md)。

## 1. 范围与本次定细的决定

本契约覆盖 Task、Acceptance、Budget、Artifact、HumanRequest，以及让它们能可靠关联的 Attempt/Run、Action、Reply、Grant、Checkpoint。它们是逻辑对象，不要求拆成独立服务；本轮实现独立数据层，不选定表结构、不改造现有执行器。

相对于前一版概述，本草案明确以下选择：

- `Task` 是逻辑工作，`Attempt` 是一次求解，`Run` 是一次运行片段。恢复沿用 attempt、新建 run；结案后显式重试才新建 attempt。
- HumanRequest 生命周期统一为 `pending/answered/cancelled/expired`。**拒绝记录为 Reply 的 `decision=deny`，请求仍是 answered**，替代概述中把 denied 混入请求生命周期的表示。
- artifact_id 唯一标识不可变内容版本；新内容使用新 ID。完整 ArtifactRecord 由平台持有，给执行者的是受限 ArtifactRef。访问授权独立存储，不写进可由模型转交的“通行引用”。
- root 和 child 使用同一预算账本；child 的 limit 是局部上限，不会创造新资源。时间使用活跃区间并集与绝对期限，不能像 token 一样相加预留。
- 以下细化与开发计划摘要不一致处，以本文 v1 的明确约定为后续评审依据；独立数据层落地不代表团队已批准全部后续架构或完成运行时接入。

## 2. 类型、必填与引用规则

| 记法 | 约束 |
|---|---|
| `Id` | 非空字符串，示例使用 T0、AT0 等；平台生成，唯一性不依赖显示名或委派深度 |
| `Timestamp` | UTC RFC 3339 字符串，如 `2026-11-15T10:00:00Z` |
| `uint` / `positive_int` | 非负整数 / 大于零整数；布尔值不能当整数 |
| `Hash` | `sha256:` 加 64 位小写十六进制；对确定的原始字节计算 |
| `T[]` | 有序数组；除非明确允许，否则 ID 不重复；空数组不等于缺字段 |
| `T?` | 字段值可为 null；canonical 持久记录保留该键，不能填假 ID |
| `JsonObject` | JSON 对象；其内部结构由指定版本的注册项校验，不能用它绕开类型检查 |
| `VersionRef` | `{id: Id, version: positive_int}`；禁止 `latest` |
| `ActorRef` | `{kind: user/profile/service, id: Id}`；从认证上下文产生，不相信模型自报身份 |
| `ArtifactRef` | `{artifact_id: Id, content_hash: Hash}`；解析时双重校验，不授予读取权 |
| `EvidenceRef` | `{kind: artifact/receipt/reply/catalog, id: Id}`；artifact 的固定 hash 由其记录解析 |
| `ScopeRef` | `{project_id: Id, root_task_id: Id}` |
| `ResourceVersion` | present：`{resource_id:Id,state:"present",version:Hash}`；absent：`{resource_id:Id,state:"absent"}`，明确区分不存在与空文件 |

下文字段表中的“创建”指服务接受创建时必须具备；“条件”指允许 null/空，但进入指定状态前必须具备。默认值只可用于无歧义的空列表、null、初始状态，不能默认验收通过、用户同意或资源有权限。

每个**顶层记录**共有：`schema_version: positive_int`（本草案为 1）、`record_type: string`（对应类型名）、`created_at: Timestamp`。可变记录还共有 `revision: positive_int`（从 1 开始）和 `updated_at: Timestamp`。不可变记录没有 revision/updated_at，内容变化新建记录或版本。嵌套值对象不重复这些公共字段。

Task、Attempt、Run、BudgetScope、BudgetLease、ArtifactGrant、HumanRequest、ActionRecord 是可变记录；AcceptanceSpec/Report、ArtifactRecord、HumanReply、Checkpoint 是不可变记录。Action 的操作、参数、对象版本、scope、约束和意图摘要从创建起不可变；授权决定在执行前补齐，进入 running 后固定；只更新执行状态和结果。改变意图新建 Action。

所有 ID 引用必须存在、类型匹配且满足项目/root 范围；外部目录中的角色、用户、环境、策略、消息、回执和上下文用明确 catalog 条目表示，不伪造为本地任务。模型可见视图不包含存储定位、凭证或无权读取的来源。

## 3. TaskRecord：工作、关系与当前进展

| 字段 | 类型 | 必填/时机 | 含义 |
|---|---|---|---|
| task_id / root_id | Id / Id | 创建 | root 的 root_id 等于自身 task_id |
| parent_id | Id? | 创建，可空 | 仅 root 为空；parent 链必须到 root，禁止循环 |
| creation_key | string | 创建 | 同一入口请求/同一次委派的幂等键；相同键不同内容报冲突 |
| objective | string | 创建，非空 | 可观察的目标，不能只是角色名字 |
| project_id | Id | 创建 | 首版任务限定一个项目；跨项目通过有明确范围的子任务实现 |
| owner_profile_id / assignee_profile_id | Id? / Id? | ready 前 | 一个当前负责人和一个当前执行者；未分配时可空 |
| deps | Id[] | 创建，可空数组 | 同 root 的前置任务，不能包含自身；与 parent 分开 |
| constraints_ref | VersionRef | 创建 | 当前有效约束；约束更新必须保留来源与版本 |
| acceptance_ref | VersionRef? | 创建可空 | 空意味着没有可验证要求，不能进入 completed |
| budget_scope_id | Id | 创建 | 服务端预算引用，不允许模型注入余额 |
| input_artifact_refs / output_artifact_refs | ArtifactRef[] / ArtifactRef[] | 创建可空 | 输入为获准固定版本；输出是本次选定的成果，不覆盖旧 attempt 成果 |
| status | TaskStatus | 创建 | 初始 created，枚举及迁移见下表 |
| reason | string? | blocked/failed/unverified/cancelled 时非空 | 机器可分类原因，如 deps_pending、external_state_unknown、budget_exhausted |
| current_attempt_id | Id? | 首次领取后 | 最新一次求解；恢复不更换 |
| latest_report_id | Id? | 验收后 | 当前 attempt 的最近报告；completed 必须引用适用的 passed 报告 |
| wait_ref | WaitRef? | 等待/特定阻塞时 | 离开等待时清空；历史由事件/检查点保留 |

`WaitRef` 是带 kind 的联合类型：human 为 `{kind, request_id}`；children 为 `{kind, child_task_ids: Id[]}`；external 为 `{kind, action_id}`。前两种分别对应 waiting_user/waiting_agent；external 对应 blocked + external_state_unknown。依赖未满足使用 blocked + deps_pending，wait_ref 可空。

只有 TaskService 可写 TaskRecord；规划器、Agent、Router、HITL、Acceptance 均提交命令或证据，不直接改 status。存储更新带 expected_revision；状态、所需关联和待发送事件在同一事务提交。Board 和 bus 是视图/通知，不能成为第二份状态真源。

### 3.1 合法状态转换

| 从 | 到 | 必须具备的条件 |
|---|---|---|
| created | ready / blocked / cancelled | 校验引用、任务图、范围；尚缺依赖则 blocked |
| ready | running | 独占领取；有有效身份、输入和预算；创建/恢复 attempt 与 run |
| ready | blocked / failed / cancelled | 执行条件变化、预算/期限不足或明确取消；记录 reason |
| running | verifying | 提交候选输出，固定被检版本；不是直接 completed |
| running | waiting_user / waiting_agent | 请求/子任务存在；先保存检查点和续接位置，再释放执行槽 |
| running | blocked / failed / cancelled | 资源/外部状态阻塞，或明确结案；记录原因 |
| verifying | completed | 当前 Spec 对当前对象的 Report=passed；必需工作已解决，受影响的外部动作无 unknown |
| verifying | running | 检查失败后继续修正，仍属同 attempt；有修复次数与预算 |
| verifying | waiting_user / unverified / failed / cancelled | 人类验收等待、证据不足、验收失败/异常等分别记录 |
| waiting_user | ready / failed / cancelled | 有有效回答/明确超时处置；deny 不执行原动作，可在有依据时排队考虑替代方案 |
| waiting_agent | ready / failed / cancelled | 必需结果/失败处理已登记；不得重新创建已有 child |
| blocked | ready / failed / cancelled | 阻塞已解除并重新检查，或明确停止 |
| completed | 无普通后继 | 再次执行已完成工作，新建关联任务 |
| failed / unverified / cancelled | 无普通后继 | 仅显式 retry 可新建 attempt 并将任务置 ready；取消后的 retry 需新请求 |

retry 是专门事务，不是任意 status 更新：旧 attempt 已结案、没有未核实副作用、root 预算和 expires_at 仍允许，才创建新 attempt。旧报告保留，Task 的当前结果指针重置；不重发 root 预算。暂停后恢复不是 retry。

root 结案前必须处置所有仍活跃或等待的后代：完成、失败或明确取消并确认不再执行；不能留下后台副作用继续发生。closed root 下不得创建、恢复或 retry child。failed/unverified/cancelled root 的显式 retry 必须先原子重新打开 root 并指定后代保留/重试安排，child 不得隐式打开 root；completed root 要继续处理则建立关联新 root。

### 3.2 AttemptRecord 与 RunRecord

| 记录 | 字段与类型（均创建必填，? 可空） | 状态约束 |
|---|---|---|
| AttemptRecord | `attempt_id:Id, task_id:Id, number:positive_int, state:AttemptState, context_ref:Id?, checkpoint_id:Id?, report_ids:Id[], started_at:Timestamp?, ended_at:Timestamp?, end_reason:string?` | pending → running/failed/cancelled；running → suspended/succeeded/unverified/failed/cancelled；suspended → running/failed/cancelled；终态不可重新打开 |
| RunRecord | `run_id:Id, task_id:Id, attempt_id:Id, state:RunState, started_at:Timestamp?, ended_at:Timestamp?, end_reason:string?, resumed_from:Id?` | prepared → running 或 ended(startup_failed/cancelled)；running → ended/crashed；新 run 引用恢复检查点 |

Task 同时最多一个未结案 attempt，attempt 同时最多一个 running run。run 的 ended 可能只是 suspended，不表示目标成功。Attempt 的 succeeded 必须跟随 Task 当前尝试通过；挂起时同 attempt、新 run。普通验收修正回合不必新建 attempt。

running 必有 started_at；Attempt suspended 必有 checkpoint_id；Attempt 终态、Run ended/crashed 必有 ended_at/end_reason。未启动即失败/取消时 started_at 保持 null，不计活跃时间。Attempt 进入 running 前必须有 context_ref。进程崩溃的 run 由恢复核对流程补记结束边界，不能伪造成功。

## 4. Acceptance：要求与证据

### 4.1 AcceptanceSpec（不可变）

| 字段 | 类型 | 必填/时机 | 约束 |
|---|---|---|---|
| spec_id / version / task_id | Id / positive_int / Id | 创建 | 版本唯一，绑定逻辑任务 |
| objective | string | 创建 | 与 Task 目标一致或更精确，不能悄悄降低要求 |
| target | `{project_id:Id, environment_id:Id, resource_ids:Id[]}` | 创建 | 明确验收范围；快照不能冒充宿主 |
| criteria | Criterion[] | 创建，非空 | `{id:Id, description:string, required:bool}`；ID 唯一，至少一个 required；覆盖成功条件与禁止破坏的约束 |
| constraints_ref | VersionRef | 创建 | 验收所依据的约束版本 |
| evaluator_ref | `{id:Id, version:positive_int, kind:deterministic/human/llm}` | 创建 | 可信注册表提供；执行者不能修改检查器 |
| params | JsonObject | 创建，可空对象 | 注册检查器参数；可引用待完成的用户选择，检查前必须解析 |
| required_output_roles | string[] | 创建，可空 | 例如 diagnosis、patch；不是强迫每个任务产出 Markdown |
| authority_ref | Id | 创建 | 谁确定要求及依据，如用户请求或获准的子任务验收策略 |

TaskService 接受有权主体的验收要求，Acceptance 服务管理版本与注册检查器。Agent 可建议修改，但不能覆盖旧 Spec。改变目标/约束需要相应授权；发布新版本并记录来源。

### 4.2 AcceptanceReport（不可变）

| 字段 | 类型 | 必填/时机 | 约束 |
|---|---|---|---|
| report_id / task_id / attempt_id | Id / Id / Id | 创建 | 绑定具体尝试 |
| spec_ref | VersionRef? | 创建 | 无验收要求时仅可生成 unverified，spec_ref 可空 |
| evaluator_ref | 同 Spec 类型或 null | 创建 | 有实际检查时与所用检查器一致 |
| verdict | passed/failed/unverified/error | 创建 | 不能只存 bool |
| checks | CheckResult[] | 创建 | `{criterion_id:Id, verdict:同四值, reason:string, evidence_refs:EvidenceRef[]}` |
| evaluated_artifact_refs | ArtifactRef[] | 创建 | 固定被检内容；不存在的产物不得伪造 hash |
| evaluated_resource_versions | ResourceVersion[] | 创建 | 使用第 2 节 present/absent 联合类型；目标环境引用由 Spec 确定 |
| evidence_refs | EvidenceRef[] | 创建 | 来源于工具/回执/人类或明确标识的模型评审 |
| reason | string? | 非 passed 必须非空 | 未验证/检查器错误不能伪装成任务成功 |
| evaluated_at | Timestamp | 创建 | 检查时刻 |

有 Spec 的 Report 对每个 criterion 恰有一条检查，不能遗漏、重复或引用未知 criterion；未执行用 unverified/error。passed/failed 必须有非空且匹配的 spec_ref/evaluator_ref，作出确定判断的检查须有证据。required 的聚合顺序固定为：有 failed 则 failed，否则有 error 则 error，否则有 unverified 则 unverified，否则 passed。optional 不改变聚合结果。无 Spec 时 checks 为空且 verdict=unverified。结构检查只证明结构，模型评审须标明证据类型。

报告只证明 evaluated_* 中的版本；对象变化后不能沿用旧报告作为新版本成功证据。root 有自己的 Spec/Report，不能由子任务计数推导。审批 allow 只允许动作，不可当成“修复成功”的人类验收。

进入 completed 前，Report 的 task/attempt/spec 必须匹配 Task 当前引用，Spec.constraints_ref 必须等于 Task 当前有效约束，且被检版本覆盖当前候选输出和目标。约束变化后先形成对应的新 Spec 再验收；旧报告保留，不自动作为当前通过证据。

## 5. Budget：额度、预留、消耗与两个时钟

### 5.1 BudgetScope（可变账本视图）

| 字段 | 类型 | 必填/时机 | 约束 |
|---|---|---|---|
| scope_id / task_id / root_scope_id | Id / Id / Id | 创建 | root 指自身 scope |
| parent_scope_id | Id? | 创建 | child 指所属父 scope；不能成环 |
| limits | Quantity | 创建 | `{tokens:uint, llm_calls:uint, tool_calls:uint}`；llm_calls 覆盖规划、路由、反思和评审 |
| active_limit_ms | uint | 创建 | 此 scope 子树的活跃时间上限 |
| active_used_ms | uint | 创建初始 0 | 根据 run/调用活跃区间并集推导，不手工填写 |
| expires_at | Timestamp? | 创建，可空 | child 不晚于父期限；无期限仍受活跃额度限制 |

可用额度由 limits 减去子树内已确认消耗及尚未结算的预留得到，**不另存一份可被独立改写的 remaining**。同一 Lease 在每个适用 scope 的检查中出现，但真实消耗只有一笔；不把 child 汇总再追加为 parent 新消费。limits 不是为 child 独占预留的资源，实际调用才申请 Lease。

### 5.2 BudgetLease（可变）

| 字段 | 类型 | 必填/时机 | 约束 |
|---|---|---|---|
| lease_id / scope_id / task_id / attempt_id / run_id | Id 各项 | 创建 | 调用执行归属 |
| call_id | Id | 创建 | 一次真实请求的唯一键；HTTP 新尝试用新 call_id，不能掩盖未知消费 |
| kind / purpose | llm/tool；string | 创建 | purpose 如 planning、diagnosis、validation |
| reserved / actual | Quantity / Quantity? | 创建 / 结算 | llm 至少预留一个 llm_call；tool 至少一个 tool_call |
| status | reserved/in_flight/settled/released/unknown | 创建 | 初始 reserved |
| dispatched_at / settled_at | Timestamp? / Timestamp? | 发出 / 结案 | unknown 不伪造 settled_at |
| evidence_ref | EvidenceRef? | settled 前 | usage 或执行回执证据；released 记录未发出的证明 |
| reason | string? | unknown/released 时 | 解释未知或释放原因 |

| 转换 | 规则 |
|---|---|
| reserved → in_flight | 原子检查所有祖先 scope、时间与权限；记录后发出请求 |
| reserved → released | 确认未发出；释放一次 |
| in_flight → settled | 实际 usage 已知，结算一次，释放差额；已发出请求至少计一次对应调用，即使失败且 tokens=0；超预留仍记录真实值并阻止超额准入 |
| in_flight → unknown | 请求发出但用量/结果不明；保留预留，不作为零成本 |
| unknown → settled / released | 获得核实证据；released 只适用于证实未发出且所有维度消费均为零，已发出但无 token 账单必须 settled 并保留调用次数 |

reserve/settle 只有 BudgetLedger 写；工具和 provider 提交用量回执。所有准入/结算幂等，不能同一 lease 重复扣款。预留估计不是 provider 的绝对 token 硬限制；报告显式列未知与超额。额度恰好用完不会追溯性否定已获准且完成的动作，但不足以准入后续验收时不能声称 verified success。

in_flight/unknown 的调用次数在确认 dispatch 时已计一次；派生余额按该 lease 的预留覆盖已确认维度和未知部分，不能再额外叠加一遍调用次数。实际 usage 未知时保留预留；确认后的成本超过预留仍必须披露，不能为了账面不超限丢弃消费。

时间单独处理：root 的 active_used_ms 是整个子树活跃区间并集；并行 2 秒不是根消耗 4 秒。全部等待用户时暂停活跃计时，但端到端耗时和 expires_at 继续前进。调用超时受剩余活跃额度与绝对期限共同约束；恢复不改历史计时。child 有自己的局部上限，等待期间其他分支活跃仍计入 root。

## 6. Artifact：固定内容、来源与可撤销访问

### 6.1 ArtifactRecord（不可变）

| 字段 | 类型 | 必填/时机 | 约束 |
|---|---|---|---|
| artifact_id / logical_name / role | Id / string / string | 创建 | ID 对应固定版本，logical_name 可重名；role 如 diagnosis/patch/probe |
| media_type / size_bytes / content_hash | string / uint / Hash | 注册完成 | 对实际字节计算，不相信模型报值 |
| storage_key | string | 注册完成 | 平台内部不透明定位，不向模型提供宿主任意路径 |
| creator_ref | CreatorRef | 创建 | task 型 `{kind,task_id,attempt_id,action_id:Id?}`；import 型 `{kind,actor:ActorRef,source_id:Id}` |
| input_refs | ArtifactRef[] | 创建，可空 | 多个上游输入，保留固定版本 |
| source_refs | EvidenceRef[] | 创建，可空 | 用户消息、工具回执、检索来源等；完整来源同样受权限过滤 |
| scope | ScopeRef | 创建 | 归属范围，不表示读者自动获得授权 |

先写内容并完成 hash/权限校验，再以一个事务发布元数据；未完成上传不是可消费 ArtifactRecord。无 pending 半成品被下游当作正式引用。新内容新 ID，撤销访问不改内容；首版不做物理清理和复杂保留策略。

ArtifactStore 才能注册/物化文件；生成者只能提出发布请求。来源可能是外部导入或多个工件，不能用单一 producer 路径丢失来源。hash 只证明内容身份，不证明正确性或权限。

### 6.2 ArtifactGrant（可变；由 PolicyGateway 签发）

| 字段 | 类型 | 必填/时机 | 约束 |
|---|---|---|---|
| grant_id / subject | Id / ActorRef | 创建 | 绑定可信主体 |
| artifact_ids / task_ids | Id[] / Id[] | 创建，非空 | 首版仅精确集合，不使用隐式全局通配 |
| project_id / operations | Id / (read/materialize)[] | 创建，非空操作 | 发布和转授权限由策略单独控制，read 不等于可以转授 |
| authority_ref / expires_at | Id / Timestamp? | 创建 | 授权来源与有效期 |
| status | active/revoked/expired | 创建 | active → revoked/expired；重新授权新 grant |

每次消费检查当前主体 × task × artifact × operation × 最新策略；知道引用、进入群聊、成为 assignee 都不构成授权。取消/撤销使后续受控读取被拒，但不能让执行者忘掉已经获准读取的信息。高敏感撤权还需停止正在运行的相关执行环境并重建上下文，这是执行策略职责。

## 7. HumanRequest、Reply 与 Action

### 7.1 HumanRequest（可变）

| 字段 | 类型 | 必填/时机 | 约束 |
|---|---|---|---|
| request_id / task_id / attempt_id | Id 各项 | 创建 | 同请求重发沿用 ID，不创建额外审批 |
| kind | question/approval | 创建 | 不把问题回复当授权 |
| prompt / choices | string / string[] | 创建；choices 可空 | 面向用户的具体问题/具体修改 |
| requested_from | ActorRef | 创建 | 首版 kind=user；由控制层确认回复者身份 |
| action_id / action_digest | Id? / Hash? | approval 必须非空 | question 两者必须空；摘要绑定第 7.3 节动作意图 |
| status | pending/answered/cancelled/expired | 创建 | 初始 pending；只到其余三种，不能重新 pending |
| expires_at / reply_id | Timestamp? / Id? | 创建 / answered 前 | 到期由服务执行 expired；answered 恰有一个有效回复 |
| continuation | `{kind:answer_question/authorize_action, tool_call_id:Id?}` | 创建 | 恢复到哪个边界；不是保存函数调用栈 |

HumanRequest 只有 HITLService 写。过期/取消后的晚到回复不激活请求；可另行创建新请求。相同 reply_id 重复提交返回原结果，不再次执行；不同回复抢占同一 pending 请求通过 revision 检查，不能覆盖已经生效的决定。

### 7.2 HumanReply（不可变）

| 字段 | 类型 | 必填/时机 | 约束 |
|---|---|---|---|
| reply_id / request_id / actor | Id / Id / ActorRef | 创建 | 身份来自 CLI/认证通道，不从模型文本提取 |
| kind | clarification/approval | 创建 | 与请求 question/approval 对应 |
| answer / decision | string? / allow/deny/null | 条件 | clarification：非空 answer、decision=null；approval：非空 decision，answer 为可选说明 |
| replied_at | Timestamp | 创建 | 校验请求仍有效；来源事件可另外记录 |

回复和请求 pending→answered 原子提交；`deny` 也是有效回答。有效既有授权可以直接使用，不为满足 schema 人工制造 HumanRequest。拒绝只阻止所拒动作，后续可提出不同方案；没有新授权不能换工具达成同一被拒副作用。

基于新审批执行时，Action.approval_request_id 必须指向相同 action_id/task_id/attempt_id 的 approval 请求，digest 等于 intent_digest；请求 answered，其唯一有效 Reply 指回该 request、kind=approval、decision=allow，actor 等于 requested_from（首版不提供代答）。授权决定另须绑定当前执行主体、该 task/attempt/action 和 scope；有效回复不能替代最新策略检查。

### 7.3 ActionRecord（可变执行状态；不可变动作意图）

| 字段 | 类型 | 必填/时机 | 约束 |
|---|---|---|---|
| action_id / task_id / attempt_id | Id 各项 | 创建 | action_id 是平台逻辑操作 ID，不等于 provider tool_call_id |
| tool_call_id | Id? | 创建，可空 | 程序触发验收可空；模型工具结果按此关联 |
| operation / effect | string / read/private_write/external_write | 创建 | 用来判断实际副作用，不只检查工具名 |
| args / args_hash | JsonObject / Hash | 创建 | 参数可审阅、无凭证；hash 按下述编码计算 |
| expected_resources | ResourceVersion[] | 创建，可空 | 需要比较的原资源版本；写外部资源前必须完整 |
| scope / constraints_ref | ScopeRef / VersionRef | 创建 | 具体授权作用范围与约束 |
| intent_digest | Hash | 创建 | 审批绑定值；参数/资源/范围改变使用新 ActionRecord |
| authorization_ref / approval_request_id | Id? / Id? | running 前 / 需要新审批时 | authorization_ref 指最新策略决定；可基于既有授权或有效 reply |
| status | prepared/running/succeeded/failed/unknown/cancelled | 创建 | 初始 prepared；批准不等于 running/succeeded |
| execution_run_id / lease_id | Id? / Id? | running 前 | 真正执行的 run 与工具额度；等待审批不提前占住调用 lease |
| receipt_ref / reason | EvidenceRef? / string? | 成功/失败后 | succeeded 要回执；failed/unknown/cancelled 要原因 |

`args_hash` 对 UTF-8 JSON 计算：键按字典序、无多余空白、非 ASCII 不转义、参数不允许浮点数（用整数单位或十进制字符串）。`intent_digest` 同样编码以下对象：operation、effect、args_hash、expected_resources（按 resource_id 排序）、scope、constraints_ref。协议版本由 schema_version 决定；字段或算法变化必须升版本。示例中的 JSON 排版不是 hash 输入，使用上述确定性编码。

| 转换 | 谁写 / 条件 |
|---|---|
| prepared → running | ToolBroker 在最新授权、动作摘要、约束、资源版本和预算检查后领取；先记录意图再执行 |
| prepared → cancelled | ToolBroker 根据拒绝、过期、变更或取消请求关闭；不能冒充执行失败 |
| running → succeeded | 有可核对回执；不代表 Task 通过 |
| running → failed | 能确定执行失败，记录已发生的部分效果；后续恢复操作另建动作 |
| running → unknown | 可能有外部效果但缺回执；阻塞相关后续操作，预留不自动归还 |
| unknown → succeeded / failed | 核对外部状态并保存证据；无法核实则保持 unknown |

失败后重做使用新 action_id（显式关联旧动作可扩展），不得把终态动作改回 running。CLI 只是展示和提交回复，PolicyGateway 产生授权决定，ToolBroker 写动作状态，执行后端产生回执。受保护资源的最终写入需由 broker 保证版本检查与写入的有效原子性或使用外部系统的条件更新，不能“检查后任意晚些时候覆盖”。

## 8. Checkpoint 与写入事务

Checkpoint 是不可变的续接记录，字段均须存在：`checkpoint_id:Id, task_id:Id, attempt_id:Id, run_id:Id, context_ref:Id, history_ref:Id, resume_cursor:ResumeCursor, constraints_ref:VersionRef, budget_scope_id:Id, acceptance_failures:uint, loop_state:JsonObject, applied_message_ids:Id[], wait_ref:WaitRef?, pending_delegations:PendingDelegation[], completed_action_ids:Id[]`。

ResumeCursor 为 `{step:uint, phase:before_model/before_action/after_action/verifying, pending_action_ids:Id[], pending_tool_call_ids:Id[], observed_tool_call_ids:Id[]}`，列表按执行顺序排列；避免同一模型响应中前一个动作完成、后一个动作等待时恢复整个 step。废弃的剩余工具调用要形成 cancelled/superseded 工具结果并保持协议关联，不能留下悬空调用或执行已失效动作。

PendingDelegation 为 `{child_task_id:Id, tool_call_id:Id?, delivery_key:Id, delivered:bool}`。原计划子任务可没有 tool_call_id，模型主动委派则必须保留关联。delivery_key 防止同一结果重复注入父上下文。context/history 是控制层保存的完整受限状态引用，不能用面向用户的截断日志替代；本草案不规定 provider 私有消息序列化格式。

最小原子操作边界：

1. 建 task + parent/deps + creation_key + 初始事件；重复 creation_key 不产生第二个 child。
2. 任务领取/挂起 + attempt/run 变化 + checkpoint 引用 + 待发送事件；挂起完成后释放执行槽。
3. 预算预留/结算 + 唯一 call/lease 键。
4. 人类回复 + pending 请求转换 + 对应待恢复事件；回答不直接启动副作用。
5. 动作领取 + 当前授权/约束核对 + 工具 lease 关联；外部动作本身不在本地数据库事务中，必须处理 unknown。

模型 step 含多个动作时，每个尚未提交的动作都检查最新 constraint revision；约束变化后暂停剩余动作、更新上下文并重新判定。固定 skill/知识版本不意味着固定旧权限或忽略新约束。只有一个执行槽时也应能挂起父子等待链，重启后继续 child 并单次交回结果，不依赖保留递归调用栈。

## 9. 模块读写职责与现有代码接入

| 模块 | 唯一/主要写入职责 | 读取范围与输入 | 当前接入点 |
|---|---|---|---|
| TaskService / TaskStore | Task、Attempt、Run 的状态；Checkpoint 持久化及任务图 | 已授权计划、上下文、验收/人工/子任务事件 | `orchestrator.py:SubTask, _execute, _handle_delegate`；调度器查询统一状态 |
| AcceptanceService | Spec 版本、Report；不能直接把 agent 文本当成功 | 被检固定版本、可信检查器、Task 当前要求 | `rao.py:evaluate_acceptance`、`agent.py` answer 分支；root 增加独立检查 |
| BudgetLedger | Scope、Lease 与派生使用量 | 有身份的调用申请及 provider/工具回执 | `rao.py:Budget`、模型请求边界；移除 `agent.py` 每轮重置注入时间 |
| ArtifactStore | Artifact 内容/元数据与物化操作 | 获准 workspace、来源、当前授权 | `Observation.artifacts` 注册，Board 仅保存引用；`_execute` 加载输入 |
| PolicyGateway | Grant、授权决定目录项 | actor、当前 task/scope、动作意图、有效 reply | 扩展 `ToolContext`；工具名、skill 或委派都不自动提权 |
| HITLService | HumanRequest、HumanReply | 认证用户回复、待问问题/具体动作 | `rao.py:_ask_human`、CLI、现有 approve callback |
| ToolBroker / Backend | Action 状态 / 执行回执 | 当前授权、资源版本、预算、获准工件 | `tools.py` registry 调用边界；真实执行隔离另属 M0-05 |
| Agent / RAO | 提案、候选结果、运行历史；不直接写上述权威状态 | 本次受限 TaskContext、工具反馈与授权输入 | 继续运行 RAO；结果区分提交结果、挂起与错误 |
| Board / MessageBus / Metrics | 投影视图、通知、派生报告 | 有权限的事件/记录 | 不取代 TaskStore，不从“模型回答结束”推导完成率 |

拟议命令语义：TaskService 的 create/transition/retry/resume；Acceptance 的 register_spec/evaluate；Ledger 的 reserve/dispatch/settle/reconcile；Artifact 的 publish/materialize；HITL 的 request/reply/expire；Broker 的 prepare/execute/reconcile。每项接受 actor 与幂等键/expected_revision（适用时），返回记录或明确错误，不以随意字符串决定状态。

统一错误至少区分 invalid_reference、invalid_transition、revision_conflict、permission_denied、budget_exhausted、approval_required、expired、external_state_unknown。错误不能被误写为某个成功状态。

现有 `RunResult` 继续作为运行结果的兼容输出；未来接入时不再兼任持久 Task/Attempt 历史，老数据缺验收证据时不能迁移成 verified。M0-01 提供独立契约与校验，TaskStore 的 M0-04 基础事务已实现（具体范围见 [说明](m0-04-task-store.md)），预算账本/工件存储分别在 M0-06/07 实现；同步人工通道在 M0-08，持久等待恢复在 M2 实现。完整示例展示目标语义，不表示这些服务已经存在。

## 10. 设计验收与待实施验证

- 字段类型、必填条件、引用、写入主体都能从本文找到；示例不使用未定义状态、伪 ID 或省略号。
- 相同深度的两个 child 有不同 ID；任务/角色/项目/attempt/run 不混用；已完成动作不因恢复重放。
- 无验收不 completed；root 结果绑定准确目标与内容；模型、用户批准、工具回执、验收报告分别记录。
- 并行预留不会重复放行余额；unknown 不释放；恢复不重置预算；活跃并集与墙钟分开。
- 工件固定内容、授权可撤销；审批绑定意图与资源版本；answered + deny 不执行动作。
- 独立数据层已覆盖成功、验收失败、拒绝、过期、超额、外部状态 unknown 和续接记录等正反例；真实并发事务、执行隔离、同槽父子挂起恢复须在相应运行时模块接入后验证。

设计文档阶段核验（实现前，2026-11-15）：示例的 7 个 JSON 块可解析并合并；82 个核心记录的字段集合、对象引用、审批身份与意图摘要、9 份工件的 UTF-8 hash/size、输入授权、验收版本、预算和时间算术已检查。当时仅修改文档；当前实现与测试状态见下节。

## 11. 独立数据层实现与使用边界

### 11.1 模块与入口

统一从 `miniagent.contracts` 显式导入；M0-01 提交未改动旧 `miniagent/schemas.py`、Agent、RAO、Orchestrator 和工具调用路径。后续 M0-02 使用其中的 Spec/Report 接入运行期验收，其余契约服务边界保持不变。新包没有数据库、网络、工具执行或自动状态推进副作用。

| 文件 | 实现职责 |
|---|---|
| `base.py` | 严格基础类型、版本与主体引用、不可变 JSON、确定性摘要 |
| `tasks.py` | TaskRecord、AttemptRecord、RunRecord、Checkpoint 与单记录不变量 |
| `acceptance.py` | AcceptanceSpec/Report、检查项与证据、报告与规范的对应关系 |
| `budget.py` | BudgetScope、BudgetLease、数量与租约结算条件 |
| `artifacts.py` | ArtifactRecord/Grant、内容 hash/size 校验 |
| `human.py` | HumanRequest/Reply、ActionRecord、动作摘要与审批绑定 |
| `transitions.py` | 状态图、revision 检查、不可变字段保护、显式 retry |
| `snapshot.py` | 完整导出快照的跨记录引用、任务图、预算、验收和授权链一致性 |
| `__init__.py` | 公共 API、按 record_type 分派的 `parse_record` |

校验分三层，不能只解析单条 Task 就宣称整个任务图有效：

1. `TaskRecord.model_validate(payload)` 或 `parse_record(payload)` 校验一条记录：严格字段类型、必填键、状态相关字段和局部不变量。
2. `transition_record(...)` 校验旧快照到新快照的合法转换，生成 revision 增加 1 的新对象；`validate_transition(before, after, ...)` 校验调用方已有的新旧对象。
3. `ContractSnapshot.model_validate(payload)` 校验完整关联数据：包含目录、13 类记录集合、交付回执与可选的 UTF-8 工件内容。它会检查引用、版本、父子/依赖环、run 时间、预算占用、报告绑定、授权链和 checkpoint 历史一致性。它不是单条增量事件的入口，导出时须提供引用闭包。

使用形式如下（payload 来自调用方，示例不是自动创建/执行任务）：

```python
from miniagent.contracts import ContractSnapshot, TaskRecord, transition_record

task = TaskRecord.model_validate(task_payload)
ready = transition_record(
    task, "ready", expected_revision=task.revision,
    at="2026-11-15T10:00:01Z",
)
snapshot = ContractSnapshot.model_validate(snapshot_payload)
wire_record = ready.model_dump(mode="json")
```

`task_payload` 必须满足相应转换的前置条件，`ready` 也需放入更新后的完整快照检查外部引用。显式重试使用 `command="retry"`，从 failed/unverified/cancelled 到 ready，并给出新的 `current_attempt_id`、清空旧结果引用；普通 resume 沿用 attempt。完整快照进一步检查预算与 deadline。转换函数不创建 Attempt/Run，也不写入存储。字段/引用错误为 Pydantic `ValidationError`；转换错误为 `TransitionError`，其 `code` 区分 `invalid_transition` 和 `revision_conflict`。

### 11.2 序列化约定与边界

- 文档标记为可空的字段仍须提供键，明确写 `null`；只有 `schema_version=1` 和具体类的 `record_type` 提供固定默认值。未定义字段、未支持版本、空 ID、用字符串/布尔值冒充整数均被拒绝。
- 所有模型都是不可变快照；逻辑“可变记录”通过新 revision 表达。数组在 Python 内保存为 tuple，嵌套 JsonObject 也冻结；`model_dump(mode="json")` / `model_dump_json()` 输出正常 JSON 数组和对象。不要使用跳过校验的 `model_construct` 或 `model_copy(update=...)` 作为业务更新接口。
- v1 时间使用 UTC `Z` 或 `+00:00`，精度不超过微秒；输出统一 `Z`。hash 为小写 SHA-256。不存在的资源使用 `state="absent"`，不能伪造空文件 hash。动作参数的确定性摘要拒绝浮点数；用整数单位或十进制字符串表达。
- `validate_report`、`validate_reply`、`validate_approval`、`validate_artifact_content` 只核对调用方提供的数据。声明的 actor/effect、catalog 记录及外部回执均不因通过模型校验而获得真实性；认证、最新策略、真实副作用判定和回执来源仍由未来控制层负责。
- `catalog` 是完整快照的辅助目录。适配器须提供带类型的主体、项目/环境、版本化约束/策略/验收器、资源、上下文、授权及回执记录；具体所需字段由引用用途校验。它不是通用运行时数据库 schema。资源须能通过自身或所属项目确定 environment_id，不能把 staging 证据标成目标环境。
- `ContractSnapshot.validate_artifact_access(...)` 针对所提供快照和时间检查 grant；不会读取最新策略或访问文件。撤销 grant 不应抹除历史执行记录，下一次读取须用最新快照重新判断。历史报告的固定资源版本也不会被当前资源版本替换。
- 预算允许记录真实超额消耗，不能为了满足 limit 丢掉实际 usage；unknown 保留占用。快照可拒绝预算耗尽后的 ready/retry，但并发预留、防重复扣费、实时 deadline 与活跃时间计费需要 BudgetLedger。
- `expected_revision` 只是本次纯函数中的一致性条件，不是数据库原子 CAS。M0-04 已在 TaskStore 中实现 Task/Attempt/Run 的联合事务、CAS、创建键去重及 outbox。Reply/Request、Action/Lease 联合更新、自动事件投递与外部状态核对仍需后续服务实现。纯数据层不保证外部动作恰好执行一次。

### 11.3 M0-01 交付时执行的验证

新增 108 项契约测试，原有 96 项回归测试保留且通过，共 204 项。契约测试直接解析完整电脑维护文档的 7 个 JSON 块，对 82 条核心记录逐条验证及 JSON 往返，并验证整个关联快照；示例 JSON 未为适配实现而改写。测试同时覆盖字段缺失/类型错误、非法转换与 revision 冲突、任务图环、验收证据与目标环境、预算 unknown/retry、工件 hash/授权撤销、拒绝/过期/错误审批链及历史 checkpoint 等反例。

```sh
python -m unittest discover -s tests -p 'test_contract*.py' -t . -v
python -m unittest discover -s tests -t . -v
```

本次运行环境为 Python 3.14.7、Pydantic 2.14.3，未新增依赖。另检查新增源码/测试符合 Python 3.10 语法；未把此语法检查声称为所有受支持 Python/Pydantic 版本的运行验证。以上均为离线数据与回归测试，没有调用真实模型 API 或维修用户电脑，也不代表后续执行流程已满足本文全部服务语义。
