# 个人 Agent 工作室：分阶段开发计划

> 日期：2026-11-15（America/Los_Angeles）。代码基线：`main` / `3e195e2067058d9034831912ef8204f6126fa3cd`。
> 状态：**M0-01 独立数据层已实现，后续阶段仍为开发提案**。核心模型、字段/引用校验和纯状态转换位于 `miniagent/contracts/`，现有执行流程未接入。技术选型仍需团队契约评审；代码、测试和设计文档随 M0-01 一同维护。
> 依据：[接入评估](agent-studio-integration.md)、当前源码及课程规格。既有实现见 [architecture.md](architecture.md)、[scheduler.md](scheduler.md)、[rao-spec.md)。本文细化并更新接入评估的 M0–M4；与仓库历史 P0–P5 是不同编号体系，不把历史“已完成”自动当作新门槛已通过。

## 1. 交付目标与开发边界

交付一个极简 CLI 工作室：用户可以创建长期角色，为同一角色绑定不同项目、工具和版本化 skill；角色在私聊或群聊中接任务，按授权选择协作者、交换明确成果，遇到需要用户决定的情况暂停并恢复；用户纠正能够分别进入偏好、项目事实和经过验证的方法更新。系统自动记录每道任务的验收、时间、成本与成长来源。

第一版保留现有 Python 单进程、Agent/RAO、三种调度器与工具注册机制。`Agent` 继续是单次运行实例；长期身份由 `Profile` 提供。`Orchestrator` 负责调度规则和约束执行；“代码协调员”是可配置角色，不能绕过这些规则。

推荐本地存储方案：控制层使用 SQLite 保存任务、会话、版本绑定、授权和动作记录；每个角色有独立的私有记忆/skill 存储目录，工件使用不可变内容文件。需要事务的状态和事件采用同一事务写入，再投递消息。角色执行环境只接收本次获准的上下文和文件副本，不挂载整个工作室存储。SQLite 不是安全边界，执行隔离由独立后端提供。第一版不引入 Redis、分布式队列、向量数据库或完整 actor 框架。

以下区分贯穿全部模块：

| 对象 | 稳定含义 | 不应混淆的对象 |
|---|---|---|
| Profile | 用户长期培养的职责主体 | 一次模型调用、一次任务 |
| Project | 一组有访问边界的工作资源与目标 | 角色的全部私有记忆 |
| Conversation / Team | 消息空间 / 成员与协作规则 | 每次都执行任务的 agent 集合 |
| Task | 一个目标及其负责人、验收与依赖 | 会话、临时模型进程 |
| Attempt / Run | 某任务的一次执行尝试及运行环境 | 长期角色身份 |
| SkillVersion | 某种方法的不可变版本 | 工具实现、访问权限 |

课程必需项在本计划中均有落点：私聊、群聊、角色协商、主动委派、HITL、私有记忆和隔离、搜索与检索、文件操作、代码运行/测试、GitHub commit/push、自动 TCR 与耗时。所有模型调用，包括规划、路由、反思和模型评审，统一使用获准的 DeepSeek 或 HKGAI；先完成其中一个真实适配。文档产物限 md/txt，内部数据库、JSON 状态与 Python 源码不受“文档格式”规则限制。

## 2. 基础缺口：先修什么、为什么

当前 204 个离线测试通过：原有 96 项回归、新增 108 项 M0-01 契约测试。M0-01 已有独立模型和纯函数级证据，详见核心契约第 11 节；未修改旧执行路径。没有真实 API、搜索、GitHub push 或用户电脑维修的成功证据。以下运行时与产品验收仍是后续开发要求。

### 2.1 M0 的开发顺序

先冻结最小契约，再并行推进 provider、验收、任务存储、隔离；在共同契约上接预算、工件、人工通道与课程工具。评测记录从 M0 开始建设。

| 编号 / 优先级 | 当前缺口与源码位置 | 开发交付 | 依赖 / 完成证据 |
|---|---|---|---|
| M0-01 / 首先 | `schemas.py`、`SubTask`、Board、bus 各持有部分状态 | 已新增独立模型、字段/引用校验、纯状态转换与完整示例回归；旧执行流程保持原状 | 108 项契约测试及 96 项原有回归通过；存储、事务、事件接入和旧结果迁移由后续模块实现 |
| M0-02 / 并行 A | `rao.py:evaluate_acceptance` 无验收时通过；`orchestrator.py` 由子任务 done 推导总目标成功 | 独立 AcceptanceSpec/Report；强制分离未验证与通过；增加 root 验收；修正报告分母 | M0-01；模型谎称完成、文件内容不对、root 不满足目标均不能算成功 |
| M0-03 / 并行 B | `providers.py:_parse` / `schemas.py:LLMResponse` 未完整保留协议字段；无真实往返证据 | 明确模型、thinking 配置与工具协议；修请求/响应往返、超时及有限重试；统一配置说明 | M0-01；离线请求体契约测试，再做真实多轮工具冒烟；无 key 时明确标待验证 |
| M0-04 / 并行 C | 委派 ID 由深度组成，`parent_id` 未填；Board 仅结束时保存 | TaskStore、唯一 task/attempt/action ID；登记计划任务和动态子任务；事务状态更新与事件 | M0-01；同父同深度两个委派互不覆盖，并发更新无静默丢失 |
| M0-05 / 并行 D | `tools.py:_run_python` 是宿主子进程；删除拒绝后还提示覆盖；网络跳转未逐跳检查 | ExecutionBackend + PolicyGateway；代码在真实隔离后端执行，受控外部操作统一授权；修替代路径和网络出口 | M0-01；宿主/其他角色读取被拒，受保护资源不能换工具修改，超时与资源限制生效 |
| M0-06 / 随后 | `_execute` 重发完整预算；`agent.py:138` 覆盖注入时间；planner 未纳入统一总账 | 统一 BudgetLedger：调用前预留、调用后结算、根 deadline、直接与汇总消耗分开 | M0-03/04；并行不重复放行完整余额，父子成本仅计一次，deadline 不被子任务重置 |
| M0-07 / 随后 | `_execute` 只给下游 objective，deps 没有数据交接 | TaskContext + ArtifactStore；显式注册、授权、快照和交付上游成果；验收条件进入上下文 | M0-02/04/05；随机上游值经文件交接被下游正确加工，邻近私有文件仍不可读 |
| M0-08 / 汇合 | CLI 的 human_handler 为 None 或固定话术；工具仅有 http_get，没有搜索/Git | 接真实同步 CLI 问答和审批；增加 search/fetch 与受控 Git diff/commit/push；修规划器强制每项写 Markdown 的提示 | M0-03/05/06；真实回答可影响行动，授权测试仓库完成 commit/push；此时持久恢复仍由 M2 交付 |
| M0-09 / 收口 | 现有测试不证明上述真实能力 | 建立分层证据包和一条完整执行链；明确旧测试预期迁移 | M0-02 至 08；达到下述 M0 出口门槛，再接工作室产品功能 |

`M0-03/05` 可以先用 fixture 开发，不必等待真实凭证。`M0-08` 的真实 GitHub 写入只在专用测试仓库及授权分支进行；未获得测试环境时标记该项未验收，不能把本地 Git 或 mock 当作已完成 push。

### 2.2 M0 出口门槛

1. 一条真实获准模型的“工具调用 → 结果回传 → 再次工具调用 → 答复”链成功，协议配置可复现。thinking 模式若需要携带附加协议字段，由适配层完整保存和回传；调试报告不倾倒原始推理内容。
2. 一个父任务连续委派两次，ID、目录、关系、预算和报告均完整；一个下游实际消费指定上游文件，root 有独立验收。
3. 文件读/写/更新/删除、代码编写/运行/测试、搜索/抓取、Git commit/push 均有真实工具证据；测试过程有明确授权和作用范围。
4. 拒绝和隔离用例通过，未回复不能被当成同意；耗尽预算后不再启动新的模型或工具动作。
5. 原有有意义的回归通过；原先把“无验收”当成功的用例随新契约显式改写，不能仅为保留“96 全绿”继续保留错误语义。离线、真实协议、真实工具和自主任务四类结果分开报告。

## 3. 跨模块契约：先固定这些对象

以下是跨阶段概要；M0-01 的独立数据 API 已实现，其余服务 API 仍为提案。具体类型、必填条件、状态和读写职责以 [核心数据契约 v1](m0-01-data-contracts.md) 为准；[完整电脑维护数据示例](m0-01-computer-maintenance-example.md) 同时作为契约测试 fixture。独立数据层完成不代表运行时已接入或团队已批准全部架构；后续字段变更需同步消费者与迁移策略。

### 3.1 身份、任务与状态

| 契约 | 最小字段 | 必须保持的不变量 |
|---|---|---|
| AgentProfile | `profile_id, owner_user_id, name, responsibility, lifecycle, policy_ref, skill_bindings, revision` | 更名不换 ID；角色状态至少 active/archived；临时角色有到期策略；公开卡不含私有记忆 |
| Project / Binding | `project_id, resource_refs, access_policy, revision`；绑定含 `profile_id, project_id, responsibility, scoped_overrides, revision` | 同角色可服务 A/B；项目事实归 Memory；项目设置不能自行扩大基础授权 |
| TaskRecord | `task_id, root_id, parent_id, owner_profile_id, assignee_profile_id, project_id, conversation_id, objective, constraint_revision, deps, acceptance_ref, input_refs, status, revision` | owner/assignee 均引用 Profile，用户身份另存；每任务一个当前 owner；root owner 对总体结果负责；parent 是分解关系，deps 是先后依赖；动态任务也登记 |
| Attempt / Run | `attempt_id, task_id, run_id, context_ref, backend_ref, budget_scope_id, checkpoint_ref, started_at, ended_at` | 重试保留逻辑 task_id，新建 attempt；运行目录不因同角色并发而共用；重试不重置 root 预算 |
| AcceptanceSpec / Report | `evaluator_id, evaluator_version, params, required_outputs`；报告含 `verdict, evidence_refs, reasons, evaluator_ref` | verdict 为 passed/failed/unverified/error；自然语言要求和可执行检查都保留；验收器不由被评测 agent 修改 |

推荐任务状态流：`created → ready → running → verifying → completed`。依赖未满足进入 `blocked`；等用户进入 `waiting_user`；等委派或协商进入 `waiting_agent`；终态另有 `unverified / failed / cancelled`。失败、预算耗尽、人工超时、验收器异常由明确 reason 区分。验收失败可以回到有上限的修复尝试，但只在 `passed` 时进入 `completed`。未配置验收保留输出并标 `unverified`，不计为已验证成功。

任务状态、验收状态和执行动作状态分别记录：工具执行完不等于任务完成，子任务全部完成不等于 root 通过。现有 `RunResult.status` 可先通过适配层投影，旧历史结果没有足够证据时不能自动升级为 verified。

验收报告还应标注证据类型：确定性检查、真实用户判定、或获准模型按固定 rubric 评审。文件存在/字数合格只证明结构；研究结论等开放任务需要相应事实/内容检查。模型评审记录模型、rubric 与来源，不能伪装成确定性证明；所有评审调用进入预算。

owner 在任务授权范围内分解、委派、汇总并请求验收，不自动拥有 assignee 的工具或私有资源。owner 转移由 TaskService 以 expected_revision 更新并记录原因，不能通过换负责人修改长期权限。角色只负责发起和解释验收，最终判定仍由 Acceptance 模块按既定检查器完成。

### 3.2 上下文、工件与预算

| 契约 | 最小内容 | 边界 |
|---|---|---|
| TaskContext | root/current objective、自然语言验收、约束、必要上游结论、输入 ArtifactRef、项目与任务身份 | 下游只接收任务所需内容；传整个历史或私有目录不作为默认交接 |
| RunContext | TaskContext + profile/project revision、选中 memory IDs/revisions、skill versions/hashes、工具集、policy_ref、constraint_revision、预算与组装时间 | 固定知识和 skill 快照；用户约束作为可更新的版本化增量；每次实际动作检查最新约束与权限 |
| ArtifactRef / ArtifactRecord / ArtifactGrant | 固定 artifact_id/hash、创建者与多来源、元数据、scope；授权另存 Grant | 引用不授予读取权；内容不可变，新内容新 ID；当前授权单独校验 |
| BudgetScope / Lease | root/task/attempt、llm_calls/tokens/tool_calls、预留额度、直接消耗、active_execution_budget_remaining、可选 expires_at、归还状态 | 原子预留/结算；一次调用只在一处计费；规划、路由、修复、反思、评审也计入 |

区分两个时钟：`active_execution_budget_remaining` 是剩余执行时间额度，`expires_at` 是可选的绝对墙钟截止时间；本文的 root deadline 指后者。没有执行者活跃（例如全部等待用户）时暂停前者；仍有独立分支运行则继续扣减。后者一旦设置，到期后不能继续执行，恢复也不偷偷顺延。交互任务可不设绝对期限，正式评测按 manifest 设置硬期限。root 活跃时间按至少一个执行者活跃的时间区间并集计算，不把并行子任务秒数相加；子任务还受各自分配的上限约束。

模型请求和工具超时受剩余执行额度及绝对期限共同限制。端到端耗时始终包含人工等待，等待时长另报；重启读取已消费额度和原期限，不重发额度。真实 API 若只能事后报告 token，预留估计与实际值都记录，单次超额后停止新调用，不能宣称未经实现的绝对 token 硬上限。

### 3.3 消息、授权与恢复

| 契约 | 最小内容 | 边界 |
|---|---|---|
| MessageEnvelope | `message_id, conversation_id, task_id, sender, recipients, kind, correlation_id, payload_ref, created_at` | 身份由运行时绑定；收件人不是授权证明；持久消息与 inbox 消费游标分开保存 |
| HumanRequest | `request_id, task_id, action_id, kind, prompt, choices, action_digest, status, reply_ref, expires_at` | question 与 approval 分开；请求为 pending/answered/cancelled/expired，allow/deny 记录在 Reply；answered 不等于批准 |
| ActionRecord | `action_id, task_id, tool_call_id, operation, args_hash, resource_version, policy_revision, constraint_revision, approval_ref, status, receipt_ref` | prepared/running/succeeded/failed/unknown；批准绑定具体动作、参数、资源版本与有效范围；动作改变重新判定 |
| Checkpoint | task/attempt、消息历史引用与已应用游标、执行游标、预算账、验收重试计数、循环检测状态、上下文/约束版本、待回答请求、动作记录、pending_delegate_action_id/child_task_id/result_delivery_id | M2 支持步骤完成、动作尚未执行的等待和委派挂起边界；子任务结果去重注入父任务，不能重新创建 child；不承诺任意指令位置恢复 |

已存在且覆盖当前动作的授权可以直接使用，不机械重复提问。授权不覆盖或需要澄清时才创建请求。崩溃发生在外部动作成功、结果落盘之前，记录 `unknown`，先通过幂等键或外部回执核对，不能盲目重放。Git push 可核对远端 ref/commit；通用外部副作用不能仅靠本地 checkpoint 保证 exactly-once。

策略区分“可以亲自执行哪些动作”和“可以为本目标请求哪些协作者”。协调员不必拥有专家的全部工具，但委派必须同时满足 root 的授权范围、协调员的委派授权与接收者的资源权限；不能借一个权限更大的角色突破本任务范围。

约束检查以实际工具动作提交为边界，不能只在模型 step 开始时检查。同一响应含多个动作时，若第一个执行后用户新增约束，剩余尚未执行动作先停止，将约束增量送入执行者后重新判定。已完成的动作不会被新约束追溯撤销；当前有效的 constraint_revision 与已应用消息游标进入检查点。

### 3.4 共同接口规则

- 公开方法接受由控制层绑定的 actor/scope；模型不能传一个别人的 profile_id 就切换身份。
- 更新接口使用 `expected_revision` 防止并发覆盖；创建/反馈/投递接受请求去重键。冲突返回明确错误，不静默“最后写入者胜出”。
- 统一可区分的失败：not_found、permission_denied、revision_conflict、budget_exhausted、approval_required、invalid_transition、external_state_unknown；工具层再映射到现有 RAO 错误类别。
- 需要原子性的任务状态、动作与待发送事件一起落盘；总线负责通知，TaskStore 是任务状态真源。JSONL trace 继续用于审计和报告，不单独承担恢复真源。
- 持久记录带 schema_version。存储根目录、私有角色目录、工件和测试凭证不进入 Git；共享的 skill 源文档和开发 fixture 可按权限纳入版本控制。

## 4. 模块职责与接口

这些是逻辑模块，可以共享一个进程；不是要求每行都新建一个服务。接口名称表示操作语义，不是本轮要编写的函数。

### 4.1 执行与可靠性模块

| 模块 / 建议接入位置 | 负责与不负责 | 输入 → 输出接口 | 核心验收 |
|---|---|---|---|
| ProviderAdapter / `providers.py, schemas.py` | 模型协议、usage、超时重试；不决定任务成功 | complete(messages, tools, model_config, lease) → normalized_response | 连续工具往返完整；非法参数不执行；401 不盲重试，429/暂时失败有上限；HTTP 重试不重放工具 |
| TaskStore + TaskService / 新增，接 `orchestrator.py` | 身份、关系、状态、检查点和任务创建；不把群消息都转成任务 | create / get / transition(expected_revision) / children / checkpoint / resume_candidates | 两次同深度委派无冲突，父子链完整；恢复不丢预算和上下文 |
| BudgetLedger / 接 `rao.py, agent.py` | 调用准入、预留、结算、根预算；不靠 prompt 自律 | reserve(scope, estimate) → lease；settle(lease, usage)；remaining(root) | 并发不超分同一余额；取消释放一次；所有辅助模型调用入账 |
| ExecutionBackend / 接 `sandbox.py, tools.py` | 受限文件/进程/网络执行；不持有用户主目录与控制层数据库 | prepare(run, mounts, limits) → sandbox_ref；execute(code, timeout) → result；stop / cleanup | 不能越界读写、继承宿主秘密或访问被禁网络；限制输出、内存、进程数和时间，超时终止子进程树 |
| PolicyGateway + ToolBroker / 接 ToolContext | 检查实际操作和资源，使用已有授权或发起审批；不把 skill 当授权 | authorize(actor, operation, resources, args) → allow/deny/request；execute(action) → receipt | 改工具名、代码路径或委派不能扩大权限；拒绝删除后不能覆盖受保护原件达到同样效果 |
| ArtifactStore / 新增，接 Observation/Board | 注册固定版本、授权共享、物化输入；不共享整个角色目录 | publish(run, path, scope) → ArtifactRef；grant / resolve / materialize(refs, recipient) | hash 一致、同名不覆盖、源文件后改不影响消费者；非法路径/符号链接/越权引用拒绝 |
| Acceptance + Metrics / 接 `rao.py, metrics.py` | 独立验收与统一统计；不让执行者自报成功覆盖证据 | evaluate(spec, result_refs) → Report；summarize(case_records, traces) → md/structured report | 未验证不计成功；root 单独验证；多个 root 按题汇总，不能只取最后一个 |

执行后端推荐第一版采用每 run 的 Linux 容器：非 root、根文件系统只读、仅挂本次工作目录与获准只读输入、默认关闭直接网络、限制资源，不挂宿主主目录、容器控制 socket 或密钥。容器是推荐部署选择；M0-01 核实演示机器可用性后定版。若选择其他后端，必须满足同一拒绝测试，不能静默降级到宿主 `python -I`。

代码可自由修改自己的临时工作副本；用户原件、共享工件版本和外部 Git 通过 broker 发布。这样对受保护原件的删除/覆盖审批不会被私有工作区里的任意代码绕过。搜索和抓取走受控出口，校验域名解析与每次重定向目标，并限制响应体与时间；外部内容只作为不可信资料输入。

Git broker 至少支持状态/diff、测试证据关联、commit、push：绑定仓库、允许分支、预期基线、变更摘要、凭证引用与回执。首版不需要 force push；远端变化导致冲突时停止并报告。凭证留在可信 broker，不能作为环境变量注入任意代码执行器。

### 4.2 个人工作室模块

| 模块 / 建议位置 | 核心职责 | 输入 → 输出接口 | 验收标准 |
|---|---|---|---|
| ProfileStore / `profiles.py` | 创建/修改/归档/恢复长期身份，生成公开卡；不保存全部会话文本 | create(spec, actor)；update(id, changes, expected_revision)；archive/restore；public_card(viewer_scope) | 重启和更名不丢关系；归档不接新任务；卡片不含私有事实 |
| ProjectStore + Binding / `projects.py` | 项目资源、成员、角色持续职责与局部设置 | create/update；bind_profile；grant/revoke_access | 同角色绑定 A/B；项目级设置只收窄或经明确授权变更范围；撤权后下一动作拒绝 |
| ContextBuilder / `context.py` | 按任务组装角色、skill、记忆、约束、工件；不隐式写回记忆 | build(profile, project, task_context, actor_scope) → RunContext | A 事实不混入 B；验收与用户新约束真实进入上下文；每次运行可追溯版本 |
| MemoryStore / `memory.py` | 偏好、事实、经历、失效/冲突与来源；不把反思当真相 | query(scope, query, kinds, limit)；record_preference；propose/verify/supersede_fact；append_episode | 身份/项目过滤先于相关性检索；过时事实不默认使用；失败经历保持失败验收证据 |
| SkillStore / `skills.py` | 人工/生成 skill、不可变版本、绑定与候选状态；不授予工具权限 | import；propose_change(base, evidence, diff)；publish(validation_ref)；activate/rollback(binding, version, expected_revision) | 未验证候选不进入正式绑定；发布不自动升级所有角色；回滚恢复旧 hash 并保留历史 |
| EvolutionService / `evolution.py` | 反馈分类、提出方法修改、安排基线/候选验证；不直接改历史任务结果 | ingest_feedback(task, message) → categorized_updates；validate(candidate, suite, environment) → report | 重复反馈不重复建候选；验证失败不能发布；扩大权限的内容转 Policy，不走记忆旁路 |
| Router / `routing.py`，接现有 route | 先筛合格候选，再选择负责人/协作者；不读取私有记忆或自动改组织 | select(task_requirements, public_cards, policy) → RoutingDecision；propose_team(goal) → TeamProposal | 点名也须合格；无候选明确返回缺口；选择原因、候选与预算可审计 |
| DelegationService / 接 `_handle_delegate` | 发出/接收/拒绝/补问委派并登记任务；不把委派视为提权 | offer(parent, target, objective, acceptance, input_refs, budget)；accept/decline/clarify；report(child) | 至少一种角色自主发起委派；有界协商、拒绝后重选；父子结果和工件可追踪 |
| Conversation/Team + Inbox / `conversations.py`，扩展 `bus.py` | 私聊、群聊、成员与可见消息、输入去重；不把群成员全部唤醒 | create_dm/group；add/remove_member；send；poll/ack(cursor) | 定向消息仅正确接收者可见；群消息不触发全员模型调用；重启后消费游标有效 |
| HITLService + CLI / `human.py, cli.py` | 展示真实问题、接收回复、等待/恢复和审批；不生成伪用户回答 | request；list_pending；reply(request_id, answer)；resume(task_id) | 同一请求重启后可答；已完成动作不盲目重放；拒绝、过期与未回复保持区别 |

上表中的新增文件名只是边界建议。落地时可以先把紧密相关的小对象放在同一模块；避免把所有业务继续塞进 `orchestrator.py`，也避免提前为每个对象建立一套框架。

## 5. 分阶段实施与出口验收

M0 的详细修复顺序见第 2 节。各阶段按出口证据完成，不以“新增了文件/类/命令”判断。可以提前并行开发后续模块的存储和 fixture，但集成发布必须满足前置门槛。

| 阶段 | 可交付的用户行为 | 依赖 | 完成后的能力边界 |
|---|---|---|---|
| M0 基线可信 | 临时执行者可靠地调用模型与课程工具、交接成果、接受真实人工输入并产生可信验收 | M0-01 至 09 | 尚无长期角色培养，也未承诺退出进程后恢复 |
| M1 最小个人工作室 | 创建角色、绑定项目、使用人工 skill、保存明确偏好与事实，重启后继续服务 | M0 可信执行/权限/验收；存储部分可提前并行 | 有持久身份和固定方法；复杂群协作和自动方法演化未完成 |
| M2 路由与人机协作 | 私聊/群聊交任务，选择负责人，协商和委派；等待用户后可重启恢复 | M1 身份与上下文；M0 TaskStore/工件/动作账 | 达成完整协作闭环；方法更新仍需 M3 验证流程 |
| M3 可验证成长 | 用户纠正分类保存，方法形成候选，通过验证后启用，可追溯和回退 | M1 版本/记忆；M2 真实反馈；M0 验收 | 达成最小工作室与课程功能闭环；改善幅度须经 M4 实验确认 |
| M4 对照评测与提交 | 固定条件运行未知任务，自动报告成功率、时间、失败与成长证据 | M0 起持续建设的评测体系；M3 功能冻结 | 可复现的最终交付，不能用功能演示代替性能实验 |

### 5.1 M1：让角色真正“活过一次任务”

按以下顺序拆成小交付：

1. **M1-01 身份与项目**：Profile/Project/Binding 的创建、查看、编辑、归档、恢复；稳定 ID 与 revision；每角色私有存储。角色类别支持临时助手、项目角色、长期角色，以生命周期元数据表达，不建立三种不同运行时。
2. **M1-02 固定 skill**：允许用户编写或导入 Markdown skill；manifest 至少含适用问题、前提、步骤、所需工具、成功/失败判断、可见范围与版本。导入先为 draft；首个种子版本通过人工触发的固定开发用例、使用 M0 验收器生成验证记录后，才能发布并绑定。M1 只实现这条最小路径，M3 再增加自动提案和对照演化。所需工具是声明，不能自动授予权限；缺少权限时明确返回缺口。
3. **M1-03 最小记忆**：明确用户偏好、项目事实和任务经历分别存储；优先采用作用域/类型/时间过滤及简单检索。用户提供、已观察、推断、冲突、失效等状态可解释，不要求模型输出未经校准的置信分数。
4. **M1-04 上下文接入**：通过 `agent_factory` 生成带角色职责、固定 skill、获准记忆和工具的 Agent；ContextBuilder 输出快照；任务结果保存验收与版本来源。
5. **M1-05 管理 CLI**：提供角色/项目/skill 列表、详情、绑定、归档与发起后续任务入口；保留现有 run/orchestrate/eval 作为低层接口。此时“继续服务”指持久角色开始新任务，未完成任务的 checkpoint resume 由 M2 交付。命令名字在实现时统一，暂不做 Web 界面。

M1 出口验收：

- 创建“小维”，绑定 A、B 两个项目；退出重启后 ID、职责、绑定与明确偏好都存在，更名不破坏关联。
- A 的解释器路径和私有故障记录不进入 B 上下文；明确标为角色通用的“先诊断再修改”偏好可以用于两者。共享事实须显式发布，Project 不另存一份重复事实。
- 同一输入交给不同角色时，能观察到职责/skill/工具集的实际差异；不是仅更换名字。
- 运行中更新 skill 绑定，当前 run 仍使用已固定版本，后续 run 使用新版；撤销权限则从下一次动作立即生效。
- 归档角色不接新任务，历史可查可恢复；既有运行由显式保留/取消策略处理。每次临时任务不会新增永久角色。

### 5.2 M2：按目标组织协作，支持真实等待与恢复

**M2-01 路由。** 输入包括目标、项目、所需能力、资源、用户点名/排除项、预算。分别筛选协调与执行资格：owner 需要必要摘要可见性及委派权限，assignee 需要具体资源和工具权限；再检查角色状态、职责与能力前提。合格候选按职责匹配、已验证经验摘要、当前负载与成本排序。第一版使用可解释规则和现有关键词匹配；复杂目标可由获准模型选择候选，但结果必须再次经过程序检查，调用计入总预算。不建设学习型路由模型。

用户点名默认优先，但不能绕过资格检查。没有合格角色时，输出具体缺口：补充信息、请求已有范围内的授权，或创建临时助手。不要自动选一个无权执行的 default agent。`RoutingDecision` 至少保存候选 ID、过滤原因、owner/assignee 选择、建议成员和所用依据。

**M2-02 组队与角色协商。** 群成员从目标需要的能力选出，公开卡足以完成初筛。新任务指定一个 root owner；任务分解可以产生不同 assignee。第一版委派协议支持 offer → accept / decline / clarify → result；角色可主动判断是否适合，提出缺失输入或替代分工，程序仍校验依赖、授权、预算与深度。默认协商最多两轮、委派深度最多两层，参数可配置并写入记录；超限交回 owner 或询问用户，不循环拉更多角色。

单个 Agent 内的 RAO 和无需等待的委派可以保留同步执行；不要求完整异步 actor 架构。遇到等待时必须有结构化挂起结果（Suspended）：逐层保存父子检查点、待完成 tool_call/child/request 关联，退出执行栈并释放槽位；回复或子任务结果到达后由 TaskService 恢复相应任务并补入结果观察。不能依赖保留 Python 调用栈跨进程恢复。无关分支继续执行，等待的父任务不能占住唯一槽位。单纯订阅状态消息不算实现了角色协商。

**M2-03 私聊、群聊与 inbox。** 用户可以向指定角色发起 DM，也可以向小组给出目标；Conversation 管可见性，TaskService 管是否建立任务，Router 管唤醒谁。普通消息在执行步骤边界进入相关任务；用户约束更新立即登记 revision，在每个尚未提交的工具动作前检查，不等到整个模型 step 结束。群成员身份不能直接读取彼此记忆或全部项目文件。

**M2-04 人工请求与持久恢复。** CLI 列出待回答问题和具体待批准动作；保存请求、检查点、动作账、预算和上下文版本。默认无人值守模式遇到必须由人决定的问题进入等待/超时策略；显式预授权策略可以继续，但记录来源为 policy，不能伪装成用户现场回答。恢复后先重新检查权限与外部动作状态，再继续。

**M2-05 培养反馈入口。** 每次纠正绑定到来源消息与任务，分流为偏好、事实修正、权限变更请求或 skill 候选。此阶段先完成真实反馈采集和分类，候选验证发布由 M3 收口。

M2 出口验收：

- 用户在含三个角色的群中提交一个只需两个角色的任务：root owner 明确，只有必要成员执行；至少发生一次 agent 主动委派与一次拒绝/补问后的合理调整。
- 一名角色不具备项目权限，即使被点名或加群也不能读取相关资源；可授权的请求进入正确人工流程。
- 协调员可以在授权范围组织 A/B 专员，但不能因此读取全部 A/B 私有资料；更换 owner 不改变长期授权。
- 用户补充“不得升级依赖”，相关执行者在下一动作前得到约束；不相关成员不收到私有回复。
- 任务等待用户时其他独立分支继续；退出、重启、回复原 request_id 后继续同一逻辑任务，不重置预算，不重复问题，不重做已确认完成动作。
- 只有一个执行槽时，父任务委派子任务、子任务等待用户；退出重启后先完成子任务再恢复父任务，无死锁，也不重放父任务之前完成的动作。
- 重复投递同一消息/回复只应用一次；审批绑定的文件版本或动作参数变化后必须重新判定。
- 注入“push 已成功但回执未落盘”的故障，恢复先查远端 commit，不盲目重复 push。无法核实时显示待核实，而非假装成功。

长期关系策略在 M2 固定为：系统可以建议关系，用户或预设组织规则决定持续授权；本次 owner 身份不意味着永久“升职”。A/B 同类小任务先复用角色并隔离项目上下文；B 有持续独立责任、不同授权或专有方法时可新建 B 专员；跨 A/B 协调频繁时建议协调角色，保留旧角色、事实和历史。第一版不自动合并记忆、不自动升格或删除角色。

### 5.3 M3：把“记住了”与“方法变好了”分开验收

| 输入类型 | 处理 | 生效条件 |
|---|---|---|
| 明确偏好：“以后先查实际启动入口” | 保存来源、角色/项目范围与 revision | 可立即用于后续相关任务；不因此改变权限 |
| 环境事实：“A 使用解释器 X” | 保存用户提供/观察等状态、证据与时间 | 需要作为执行依据时核验；冲突进入待核验，不简单覆盖 |
| 任务成功/失败 | 保存 episode、产物和真实验收报告 | 不能由反思把失败重写成成功 |
| 通用方法改进 | 从任务证据产生 skill diff，移除项目私密数据 | 通过规定验证后发布，角色绑定另行启用 |
| 工具/资源权限变化 | 转 PolicyGateway | 需要有效授权，不以 skill 文本作为通行证 |

实施拆分：

1. **M3-01 候选生成**：用户直接编辑与模型提出修改都成为候选；记录 base_version、来源任务/反馈、修改理由、适用范围和 diff。默认一次反馈最多产生一个待处理候选，重复事件去重。
2. **M3-02 验证运行**：在独立环境比较基线和候选。至少包含目标故障、相似症状不同原因、旧行为回归；验证器和答案不挂载到 agent 环境。候选脚本不能在可信控制进程直接执行。
3. **M3-03 版本发布与激活**：状态为 candidate → validating → validated/rejected → published → retired；published 内容不可变，角色绑定独立保存。自动激活仅适用于用户预先允许的同角色、同范围、通过规定验证的修改；其他情况展示具体 diff、结果与范围供选择。
4. **M3-04 观察与回退**：后续任务记录版本和失败类型；出现回归可把绑定恢复至已知版本。回退不删除候选/验证历史，也不宣称撤销已经发生的电脑或 Git 操作。

Skill 验证报告必须包含候选与基线版本、测试集版本、环境、逐案例验收、回归结果、预算/耗时、验收器版本和已知限制。最小维护案例集包括错误解释器、真实缺依赖、应用代码故障；测试这些分类和约束，不能只检查 skill 文档中是否出现关键词。

M3 出口验收：未验证候选无法激活为正式版本；失败案例阻止发布；发布一个角色的 skill 不改变其他角色；运行中版本不漂移；回退后下一任务加载旧 hash；从 A 抽取的方法能在新的 B 实例使用而不包含 A 私有路径。通过这些门槛只证明演化机制有效，实际性能改善由 M4 对照测量。

### 5.4 M4：可复现评测与课程提交

开发初期建立开发集和独立验证集；正式测试发布后遵守课程协议，不能用最终答案挑选 skill 或路由参数。默认正式测评冻结初始记忆与 skill，按题从相同状态开始；若课程允许在线学习，单列连续学习轨，固定题序、写回规则和人类反馈来源，不混在冷启动结果中。

记忆消融使用仅从开发任务产生、版本固定的非空记忆快照，并在 manifest 登记来源和 hash。关闭组禁止检索，开启组使用同一快照；每道正式题从快照副本开始，该题新增记录不回灌后续题。新旧 skill 对照也使用完全相同的记忆快照。另可报告空记忆冷启动，不能用空快照代替历史记忆收益实验。

最低对照设计：

| 配置 | 用途 |
|---|---|
| 单角色、无持久记忆、固定 skill | 基线 |
| 固定团队、无持久记忆、固定 skill | 观察协作相对于单角色的收益与成本 |
| 按需组队、无持久记忆、固定 skill | 观察路由与组队影响 |
| 按需组队 + 记忆、固定 skill | 单独观察记忆作用 |
| 按需组队 + 相同记忆起点、验证后的 skill | 单独观察方法版本改变的作用 |

同组实验固定模型/协议配置、工具和权限、root 总预算、初始项目快照、人工反馈规则、验收器和任务顺序。演化的离线验证成本单列，运行期反思/检索/路由成本计入各自任务总量。报告逐题结果及重复运行的波动；具体重复次数在正式运行前按配额登记，不能结果出来后只选最好的一次。

自动报告至少包含：

- **Goal TCR** = 通过独立验收且无致命崩溃的 root 题数 / 全部指定评测题数。失败、超时、验收错误和未验证题不能从分母消失。
- **Subtask TCR** = 通过验收的子任务数 / 本次产生的逻辑子任务数；列出 blocked/cancelled/unverified 等状态。重试 attempt 不重复制造逻辑子任务。不同分解策略会改变分母，所以该指标只作诊断，不能单独宣称路由提升。
- 端到端墙钟时间（从分配到最终输出）、人工等待时长、至少一个执行者活跃的时间区间并集，以及各 agent 活跃时间之和。并行时这些量不相等；不能简单用子任务耗时总和充当端到端时间。超时题显示截止时间与状态。
- 全部模型调用、token、工具次数、重试/委派/审批次数、预算超额、失败类别；逐题连接 context、artifact、memory、skill 和 evaluator 版本。
- 候选提出数、验证通过/拒绝、激活与回退、同类失败是否复发。无改善或有退化也如实报告。

M4 出口：从固定 manifest 一次生成逐题结果与 md/txt 报告；结果可追溯到证据；至少覆盖检索写作、代码修改测试、文件整理等不同任务族，不能只测电脑维护。整理完整 GitHub 代码仓库链接与最终提交 SHA/标签、架构文档、隔离实现与限制、路由/记忆/演化设计、失败分析、运行说明、全体成员持续贡献记录与课程要求的提交历史截图、week 13 展示材料。截图是课程专门要求的证明材料，不据此开发复杂文档格式支持。

## 6. 贯穿各阶段的电脑维护验收故事

演示使用可控项目快照和诊断 fixture；若要证明真实宿主修复，另接受控主机连接器并在目标主机独立复验。容器里测试通过只证明相应快照，不代表用户电脑已修好。

| 步骤 | 用户/系统行为 | 涉及模块 | 可检查结果 / 最早阶段 |
|---|---|---|---|
| 1 | 创建“小维”，职责为开发环境维护，绑定 A/B，选择诊断 skill v1 | Profile、Project、Skill | 一个稳定角色，两个项目范围；M1 |
| 2 | A 项目启动失败，小维读取获准的启动配置与解释器证据 | Context、Memory、Backend | A 的真实输入与证据被引用，不凭症状直接重装；M1 |
| 3 | 问题是 VS Code 启动解释器与终端不同，提出具体修改 | ToolBroker、Acceptance、HITL | 修改范围清晰，审批如有需要在执行前完成，复验相同启动路径；M0/M1 |
| 4 | 用户纠正：“以后先核对实际启动入口” | Conversation、Evolution、Memory | 偏好立即保存，A 环境事实有范围，方法更新另建候选；M2 |
| 5 | 比较新旧方法，运行三类故障开发案例 | Skill、Evolution、Acceptance | 避免把所有故障都归为解释器；通过后发布 v2；M3 |
| 6 | 重启程序，B 项目出现新的启动故障 | Profile、Context、Memory | 同一小维加载 v2，未读取 A 私有事实；M3 |
| 7 | 小维确认是调用签名不匹配，选择代码助手 | Router、Delegation、TaskStore | 记录 owner/assignee/parent，助手可补问或拒绝，预算从 root 分配；M2 |
| 8 | 助手接收 B 的授权快照，修改代码并测试，交回补丁和结果 | Artifact、Backend、Acceptance | 实际读取指定快照，补丁固定版本，无权读取其他资料；M0/M2 |
| 9 | 用户补充“不要升级依赖”，并在需要决定时退出再恢复 | Inbox、HITL、TaskStore | 下一动作前应用约束；重启不重置状态或重复外部动作；M2 |
| 10 | 小维验证整体启动目标，在授权测试仓库提交并推送 | Acceptance、Git broker | root 独立通过，真实 commit/远端 ref 回执可核对；M0/M2 |
| 11 | 展示来源、成本与成长；将角色绑定回退到 v1 再开新任务 | Metrics、Skill | 完整事件链，下一任务使用 v1；已做的代码修改不被伪称撤销；M3/M4 |

拉群并不是这条流程的前置条件：用户可以一直与小维私聊，由其在后台发起受控委派。需要多人参与决策时再进入群聊；任务责任和实际执行记录仍然可见。

## 7. 验收测试目录与证据等级

以下是未来测试清单，不表示本轮已执行。单元/集成测试优先覆盖边界和失败，不写只重复实现的测试。真实 API 和外部工具测试单独标识，缺凭证时显示未验证，不能悄悄视作通过。

| 编号 | 场景 / 必须观察到的结果 | 阶段 |
|---|---|---|
| A01 | 两轮工具调用请求体、tool_call_id 与所选协议字段完整往返；参数错误不执行 | M0 |
| A02 | 模型声称完成但文件不存在/内容不符，未计成功；子任务全通过但 root 条件不满足亦失败 | M0 |
| A03 | 同父同深度两次委派 ID/目录不同、关系完整；并发状态 revision 冲突明确返回 | M0 |
| A04 | 两并行任务竞争余额；预留/释放不重复；父子与 planner 计费正确，子任务不延长 deadline | M0 |
| A05 | 上游写入每次不同的随机值，下游读取授权文件后输出指定变换；未发布邻近文件不可见 | M0 |
| A06 | 尝试读取宿主/其他角色/秘密环境变量、路径与符号链接逃逸、直连禁用网络均被阻止；输出/内存/子进程超限受控 | M0 |
| A07 | 删除受保护原件遭拒后，覆盖/Python/委派不能达成同一未授权副作用；授权范围内的私有副本修改可用 | M0 |
| A08 | 真实搜索返回来源并能抓取；重定向到内网被拒；代码测试与专用 GitHub commit/push 有回执 | M0 |
| A09 | 重启、更名、归档/恢复、A/B 同角色并行；项目事实不串用，私有 store 不可挂载 | M1 |
| A10 | 固定版本与上下文可追溯；运行中发布不漂移，撤权从下一动作生效 | M1 |
| A11 | DM 与群消息正确路由；无关成员不被唤醒；委派 offer/accept/decline/clarify 有界完成 | M2 |
| A12 | waiting_user 释放执行资源；重启回复原请求；重复消息不重用授权；动作变更触发重新判定 | M2 |
| A13 | 外部操作已成功、结果落盘前故障；恢复先查证，不能无条件重放 | M2 |
| A14 | 用户反馈正确分流；重复反馈去重；失败任务不变成成功经验；候选验证/拒绝/发布/回退均有效 | M3 |
| A15 | 相似症状不同病因能分流；旧行为回归；新项目实例复用方法但不带旧项目秘密 | M3/M4 |
| A16 | 多个 root、并行子任务、重试、等待、未验证和超时混合的确定性日志，算出的 TCR/时间/usage 与手算一致 | M0 起，M4 收口 |
| A17 | 同一 fixture 在三种调度器下均满足状态/工件/验收不变量；Mock 按任务独立，不能并发共用不安全游标 | M0/M2 |
| A18 | 人工等待暂停执行额度时，等待后仍可用剩余额度；超过绝对 expires_at 则不得继续，重启不改变两者 | M2 |
| A19 | 同一轮两个工具动作，第一个完成后新增“不得升级依赖”；第二个尚未开始的升级动作被拦下并重新判定 | M2 |

证据等级分别标为：L1 静态/离线回归；L2 真实模型协议；L3 真实工具端到端；L4 未脚本化任务与独立验收；L5 固定对照的跨任务实验。一个等级的通过不能替代另一个等级。已有 96 项测试属于 L1，不据此宣称 L2–L5 已完成。

## 8. 团队协作与第一批开发任务

### 8.1 按边界分工

| 工作流 | 主要负责 | 主要协作接口 |
|---|---|---|
| 执行与接入 | Provider、Backend、Policy/ToolBroker、搜索/Git | TaskContext、ActionRecord、BudgetLease |
| 任务与协作 | TaskStore、Artifact、Router/Delegation、Conversation/HITL | TaskRecord、MessageEnvelope、Checkpoint |
| 角色与成长 | Profile/Project、ContextBuilder、Memory、Skill/Evolution | RunContext、EvidenceRef、SkillVersion |
| 验收与集成 | Acceptance、Metrics、fixture、实验 manifest、端到端集成 | AcceptanceReport、事件 schema、阶段门槛 |

4 人时各一条主责；3 人时将角色成长与 CLI 产品入口合并，集成主责轮值；2 人时执行接入一人、持久对象和协作成长一人，验收与契约共同负责。每人同时为自己模块提供测试，不能把测试全部甩给集成人员。实际人员、工期和课程日期未知，本文按依赖排期，不虚构日历承诺。

每个跨模块契约有一位 owner 和一位 reviewer；`schemas.py` 沿用仓库全体 review 约定。先合小的公共契约，再用 fixture 并行开发，减少多人同时长期修改 `orchestrator.py`。使用小分支和持续提交记录真实贡献，不能到结课时批量补历史。自动化代理创建分支时默认 `codex/` 前缀；团队其他成员遵循团队命名约定。

### 8.2 下一批可直接领取的 PR 单元

| PR 单元 | 范围 | 验收 / 合并顺序 |
|---|---|---|
| 01 契约与指标口径 | M0-01 + AcceptanceSpec/状态定义 + trace 版本；补对象关系 fixture | 先评审；A02/A16 的期望先明确 |
| 02 Provider 多轮协议 | M0-03，仅适配/配置/协议测试 | 在 01 后并行；L1 与 L2 分开记录 |
| 03 TaskStore 与委派身份 | M0-04，含 plan/dynamic task 统一登记 | 在 01 后并行；A03 |
| 04 隔离与统一动作授权 | M0-05，含受控执行后端和拒绝路径 | 在 01 后并行；A06/A07；没有后端时禁止宣称真实隔离 |
| 05 强验收与统一报告 | M0-02，root 验收、unverified 与多 root 统计 | 在 01 后并行；A02/A16 |
| 06 预算总账 | M0-06，接 planner/agent/delegate | 02/03 的接口就绪后；A04 |
| 07 工件与任务上下文 | M0-07，接现有 factory 与结果记录 | 03/04/05 后；A05/A17 |
| 08 真实人工入口及课程工具 | M0-08，内部再按 CLI、搜索、Git 拆小 PR | 02/04/06 后；A08 及真实问答；达到 M0-09 收口 |

每个 PR 写清：改变的用户/系统行为、触及的契约、正反路径证据、真实外部验证是否完成、迁移/兼容影响。未验证项留在阶段清单，不用“已接入”掩盖。表中 PR 单元不是必须一次提交的大包；真实实现时继续拆分到可独立审阅。

## 9. 需定版的环境事项、范围取舍与接手规则

| 事项 | 推荐默认 / 需要的信息 | 最晚决策点 |
|---|---|---|
| 模型配置 | 以学校实际下发的 DeepSeek/HKGAI 之一为首个真实适配；确定 model、协议模式、配额 | M0-03 在线冒烟前 |
| 隔离环境 | 推荐 Linux 容器，确认开发/演示机器可运行并能执行拒绝测试 | M0-05 开始实现前 |
| 搜索与 GitHub 测试环境 | 确定搜索服务及凭证；专用仓库、分支、可验证 push 权限 | M0-08 真实验收前 |
| 存储 | 推荐中央 SQLite 元数据 + 角色私有存储 + 不可变工件；确定根目录和迁移策略 | M0-01 / M1-01 |
| 正式测试协议 | 规格中“提前 3 周”与“mid/final 3/1 周”不一致；确认实际日期、人工回复、跨题记忆、计时规则 | 正式测评 manifest 冻结前 |
| 演示任务族 | 默认电脑维护故事，另加检索写作、代码修改、文件整理覆盖泛化 | M1 产品验收前 |
| 团队成员与时间 | 指定工作流主责、reviewer、实际可用工作量 | 领取第一批实现 PR 前 |

这些事项进入团队任务列表；不阻塞可以独立进行的契约、离线测试设计与 fixture 工作。涉及真实外部写入时先具备具体、可审阅的动作与有效授权。

**必须保留的最小交付**：可信执行与课程工具、独立验收/指标、持久角色与私有记忆、版本化 skill、真实 DM/群聊和主动委派、明确工件交接、可恢复 HITL、至少一条有验证的跨任务改进链。

**时间紧时优先延期**：复杂 UI、向量数据库、技能市场、自动永久建角/合并/升格、多层管理组织、学习型路由、全异步 actor 化、多 provider 全覆盖、真实用户电脑的广泛维护权限。不能通过删除课程工具、隔离、验收或 HITL 来给高级组织功能让时间。

本计划可以作为后续领取任务和更新进度的入口。接手者先核对 HEAD、`git status` 和实际测试结果，再从第一批 PR 单元推进；实现完成时在同一计划对应项记录提交、证据与偏差。M0-01 交付分支为 `jinyuwu/m0-01-contracts`，包含独立数据层、108 项契约测试及相关设计文档；完整离线回归共 204 项通过。后续执行流程接入继续按上述模块边界推进。
