# 个人 Agent 工作室的角色记忆、代码上下文与交付验收

2026-11-18 调研。用户可以培养一个长期服务的电脑维护角色，并让它在不同群里参与工作。角色需要记住哪些经验？成员怎样接着上一次的改动做下去？负责人凭什么宣布交付完成？长期知识需要审核与版本，任务上下文需要指向实际工作区，交付结论需要可以重跑的检查。

## 两道软件题揭示的缺口

同一群使用真实 DeepSeek，先维护会议室预约系统，再要求基于实际预约生成仪表盘。这次测试复用了已有预约系统和数据库。下表记录该次配对运行的结果。它可以用来诊断继续维护项目的能力，从空目录生成项目应另设实验。

| 观察对象 | 独立检查 | 对架构的含义 |
| --- | --- | --- |
| 预约题正式交付 `studio-20261118-024859-d49497082037` | Chromium、HTTP 和重启检查通过 19/27。6 条原记录逐字段保留。零点、20:15、23:30 及 23:45 至 24:00 四组时段均在网页和 API 失败。6 次成员交接均未发布 | 需同时记录模型回合、成功检查和实际发布版本 |
| 预约题最后一份私有发布员草稿 | 同一套检查通过 26/27。API 已可创建四组预约，网页时间控件无法提交 24:00，选次日 00:00 返回 400 | 可恢复工作保护了部分进展。真实界面仍暴露了 HTTP 测试遗漏的问题 |
| 仪表盘题正式交付 `studio-20261118-025523-32e7f57745e0` | 父任务、数据库六行及文件哈希吻合。公开首页没有仪表盘入口，仪表盘页面/API 均为 404。7 份私有检查点中也未发现仪表盘实现 | 跨题数据继承成功，第二题产品交付失败。两个完整任务的公开业务通过数为 0/2 |

检查在交付副本上进行。6 条预约包含 4 条有效、2 条取消。测试中新增和取消的记录留在副本内。复核本机运行可用上述 ID 查找 `runs/<id>/studio.json`、`public-room.jsonl`、`trace.jsonl` 和负责人目录 `agents/lead/`。成员目录的内容只有在成功发布后才能算作公开交付。

## 可借鉴的上游机制

### 让角色跨会话学习

**Hermes 用 profile 保存长期身份。** 每个 profile 有自己的配置、会话、记忆与技能。记忆工具处理稳定的事实和偏好，技能先以目录出现，需要时再读取完整过程。个人工作室可沿用这种使用体验。例如，用户说“维护报告用中文”，可以形成角色级记忆。确认某个项目的预约系统支持用环境变量指定测试数据库，则适合纳入该项目的维护技能。持续身份由 Studio 绑定的角色和项目范围提供，无需为每个新任务复制凭据和整份历史。[Hermes profiles](https://github.com/NousResearch/hermes-agent/blob/09ded63c4fedcc8fe975d37f9765797d904fc75d/website/docs/user-guide/profiles.md) · [持久记忆](https://github.com/NousResearch/hermes-agent/blob/09ded63c4fedcc8fe975d37f9765797d904fc75d/website/docs/user-guide/features/memory.md) · [渐进式技能加载](https://github.com/NousResearch/hermes-agent/blob/09ded63c4fedcc8fe975d37f9765797d904fc75d/website/docs/user-guide/features/skills.md)

**MASkills 将改进目标缩小到实际使用过的技能。** 作者对角色和被调用技能分配反馈，提出新增、修订、合并、剪除四类操作，通过保留验证集评估候选。原研究的任务包括 HotpotQA、LoCoMo 和 GAIA。可用于 Studio 的是可定位的技能修订与验证设计。当前本地实现提供候选、新修订、审核及回退。按技能分配贡献分数、自动合并剪除、候选版本对照实验仍属提议。[MASkills 论文](https://aclanthology.org/2026.findings-emnlp.1453/) · [作者实现与验证门槛](https://github.com/DaRL-GenAI/MASkills/blob/7b72f8b412a5b2143cedc6986261dd06fffe04c1/README.md)

### 让成员续接已经读过的代码

**Pi 组合模型摘要和确定性文件清单。** 压缩时保留较新的上下文，并把目标、约束、决定、进度、阻碍和下一步汇入摘要。运行时另外从工具调用提取读过或改过的路径，并记录从哪条会话记录起继续保留原文。这个路径清单由调用产生，无法独自证明写入成功。Studio 应把成功观察中的版本哈希、读取区间和检查回执作为事实来源。模型生成的摘要可以解释修改意图，生成费用也要计入同一任务预算。[Pi 摘要结构](https://github.com/earendil-works/pi/blob/efd284ecca3788ca1345fdcc927a1f97e7ceeeaf/packages/coding-agent/src/core/compaction/compaction.ts#L507-L539) · [文件清单来源](https://github.com/earendil-works/pi/blob/efd284ecca3788ca1345fdcc927a1f97e7ceeeaf/packages/coding-agent/src/core/compaction/utils.ts#L31-L72)

**SWE-agent 提供低成本的对照。** 其窗口处理器倒序遍历历史，对同一文件保留最近的输出窗口，用占位文字缩短更早的窗口。Studio 的 `app.py` 可能同时容纳后端时间解析和前端表单。针对这类文件，应试验按文件版本及区间去重，保留不相交的两段和对应的回读位置。JetBrains 在 SWE-bench Verified 的五种模型配置上比较过遮蔽旧观察与模型摘要，前者相对原始历史约减半成本，解题率与摘要相近。这给出了先测便宜方案的依据。[SWE-agent 论文](https://arxiv.org/abs/2405.15793) · [窗口处理器源码](https://github.com/SWE-agent/SWE-agent/blob/3ea751c087f32b16e039a2233dd6eefecef325d5/sweagent/agent/history_processors.py#L215-L258) · [上下文策略对照研究](https://arxiv.org/abs/2508.21433)

### 让负责人根据证据做决定

**DeepSeek Harness 和 dsh-superagent 处理两个不同边界。** Harness 的 Messages 适配器将文字、思考和结构化工具块分别解析，并核对调用标识。Studio 使用官方 Chat Completions 的 `tool_calls` 协议。模型正文偶尔出现 DSML 调用标记时，宿主应记录一次协议失败，可在预算内要求纠正。正文不能生成执行回执。dsh-superagent 提供经过 schema 验证的 `orchestrator_report`，把成员状态和摘要写入事务再通知负责人。原型允许无 PR 的任务报告 ready 后完成。Studio 的 ready 可以结束分工，发布和用户验收仍须核对交付版本和检查结果。[官方工具协议](https://api-docs.deepseek.com/guides/tool_calls/) · [Harness 结构化事件解析](https://github.com/deepseek-ai/deepseek-harness/blob/ca33fd6843b205e279c7ccb6cb73a472c78f5760/packages/llm/llm-deepseek/src/translate.ts#L46-L85) · [成员报告事务](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/src/app/tools/executor.ts#L75-L103) · [原型完成条件](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/docs/003-execution.md#L90-L96)

**Magentic-One 使用两份账本。** 任务账本保存事实、待查项与计划，进展账本评估是否完成、是否重复、下一位成员应做什么，停滞可以触发重新规划。对应到工作室，成员已公开交付且全部必需检查通过时，负责人可以直接收尾。需要返工时，应引用失败的具体检查、恢复的版本及负责角色，再从总预算中分配额度。模型对“有进展”的判断适合辅助路由，业务判定仍由宿主持有的结果决定。[微软论文](https://www.microsoft.com/en-us/research/wp-content/uploads/2024/11/MagenticOne.pdf) · [AutoGen 协调器](https://github.com/microsoft/autogen/blob/8390985108cb1e3881f5f7297765758d8ad2540c/python/packages/autogen-agentchat/src/autogen_agentchat/teams/_group_chat/_magentic_one/_magentic_one_orchestrator.py)

### 一条消息执行到结果

Studio 的 `POST /api/runs` 和 `POST /api/superagent/messages` 接受本轮的 `interaction_mode`。采用 `autonomous` 时，负责人、执行成员、复核员和 Superagent 下派的会话共用这个模式。可逆偏好和模拟情景由承办角色决定，在交接中写明重要假设和理由。工具表移除 `ask_human`，执行器在整批动作生效前拒绝对未开放工具的调用。需要批准的动作仍需真实许可。必需的外部权限、私有数据或独立评审条件缺失时，负责人收回可交付的部分，陈述具体缺口，保留未通过的验收结果。

新任务的 API 调用缺省按 `interactive` 处理。续聊缺省沿用所回复那一轮的模式，显式传值可以切换。运行详情的 `run.interaction_mode` 返回实际模式，旧运行显示为 `interactive`。同一请求重试须匹配生效的模式。互动运行继续使用已有的问答队列和真实回复。

这个设计参考 AutoGen 的 `check_termination_and_human_reply`。该方法依据 `human_input_mode` 分派，在 `NEVER` 遇到终止条件时跳过人工通道。上游测试用两个角色验证消息交接及停止。Studio 在此基础上按整轮透传模式，在宿主的工具分派和独立签署两个位置分别落实许可与验收。回归包括负责人及成员实际尝试提问后写出交付物、删除申请缺少授权时保留文件，以及独立评审无法核实必需条件时结束任务。[AutoGen 输入与终止逻辑](https://github.com/microsoft/autogen/blob/3b4c0170b058a3629bfc58a75e1bc22e2b9a29eb/autogen/agentchat/conversable_agent.py#L1675-L1716) · [AutoGen 双角色回归](https://github.com/microsoft/autogen/blob/3b4c0170b058a3629bfc58a75e1bc22e2b9a29eb/test/agentchat/test_conversable_agent.py#L346-L374)

真实基金题的首轮派工额外要求至少二十条来源，收集了二十五个已打开的来源后，研究稿因一条公开新闻网址触发交接检查。这两个环节都会增加耗时。负责人现在按成品需要定义研究问题和足够的证据，缺口或证据冲突再触发追加调研。来源数量、额外界面和内部报告需能对应用户要求或具体交付缺口。这是规划提示，宿主继续按实际产物验收。此做法参考 Anthropic 对研究负责人区分调查深度、逐项判断子任务必要性的实现。[上游负责人提示](https://github.com/anthropics/anthropic-cookbook/blob/main/patterns/agents/prompts/research_lead_agent.md) · [多智能体研究系统的工程验证](https://www.anthropic.com/research/multi-agent-research-system)

## 本仓库的实现边界

| 能力 | 已有实现 | 下一项验证或实验 |
| --- | --- | --- |
| 长期角色知识 | 宿主绑定角色、项目和来源运行。工具可检索已生效条目、按版本读完整技能、提交待审候选。修订待审时继续提供上一生效版。SQLite 保留版本和用户决策 | 统计真实角色在多少次适用任务中主动检索、读全、采用并改善了独立结果。[存储](../miniagent/studio_role_memory.py) · [模型工具](../miniagent/role_tools.py) |
| 成员上下文 | 记录读过的文件版本及区间、修改和真实运行。请求视图可裁去早期完整消息批次，原始轨迹保留 | 在工作区版本一致时保留关键局部代码，比较固定证据和额外语义摘要。新版本使旧片段失效。[成员观察](../miniagent/crew_progress.py) · [请求视图](../miniagent/context_window.py) |
| 交接、停止与验收 | 成员受工作/交接阶段的工具和预算约束，保留私有检查点。负责人使用公开交接和独立预留收尾。正文伪调用会触发协议纠正。浏览器工具直接调用 Playwright，脚本断言失败可返回非零状态 | 给区别于开发自测的验收器固定输入及预期结果，按公开版本重新运行。网页操作、退出码和产物哈希分别记录。[角色协作](../miniagent/studio_crew.py) · [协议决策](../miniagent/rao.py) · [真实浏览器](../miniagent/browser_tool.py) |

上述角色、压缩、报告和账本属于设计参照。运行时直接调用的浏览器库是 Playwright，文本搜索使用 ripgrep。Pi、SWE-agent、Hermes、DeepSeek Harness 和 dsh-superagent 的固定提交均提供 MIT 许可。引入上游代码时，应保留相应的版权与许可文件。当前架构继续使用自己的 Python 工具注册表、任务存储和事务发布。[Pi 许可](https://github.com/earendil-works/pi/blob/efd284ecca3788ca1345fdcc927a1f97e7ceeeaf/LICENSE) · [SWE-agent 许可](https://github.com/SWE-agent/SWE-agent/blob/3ea751c087f32b16e039a2233dd6eefecef325d5/LICENSE) · [Hermes 许可](https://github.com/NousResearch/hermes-agent/blob/09ded63c4fedcc8fe975d37f9765797d904fc75d/LICENSE) · [Harness 许可](https://github.com/deepseek-ai/deepseek-harness/blob/ca33fd6843b205e279c7ccb6cb73a472c78f5760/LICENSE) · [原型许可](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/LICENSE)

## 角色隔离负例

| 试验 | 可观察的正确结果 |
| --- | --- |
| 同一开发角色分别维护 A、B 项目，在 B 搜索 A 项目专用的原文，再猜 A 条目的 ID | 检索结果只包含角色通用项及 B 项目项。按 ID 读取 A 条目返回与未知 ID 相同的错误 |
| QA 看到开发角色在群聊里引用的知识 ID，再用自身工具读取或修订 | QA 查不到开发者的私人记忆。分享给群成员时应显式发布允许共享的内容 |
| 开发成员提出“全天预约回归”技能第 2 版，用户暂未审核，随后拒绝 | 重试收到同一候选回执。检索与读取继续返回原生效版本。拒绝的候选在后续项目中保持不可调用 |
| 模型伪造 `source=user`、`status=active`、其他角色或项目，或把凭据写进候选 | 工具参数或内容校验失败，存储无新增。只读注册表仅提供检索和读取 |
| 用户已批准新版本，成员仍基于旧 revision 修订 | 返回版本冲突，保留用户修改。查看历史并回退时形成新修订，可追溯原批准版本 |

离线验证可在仓库的项目 Python 环境运行下列命令。它们覆盖接口与状态行为，实际产品效果由下一节的独立验收评定。

```sh
python -m unittest tests.test_role_tools tests.test_studio_role_memory
python -m unittest tests.test_step_policy tests.test_crew_progress tests.test_studio_workspace_members
python -m unittest -v tests.test_browser_tool
```

真实浏览器用例需要 Playwright 和本机 Chrome 或已安装的 Chromium。检查输出中的 skipped 计数，单独记录实际打开浏览器的用例数。

## 怎样复现、比较并判断是否值得合入

SkillsBench 的论文版固定 84 个任务，在相同任务上比较无技能、人工整理技能、模型自行生成技能三种条件。人工技能平均提升 16.2 个百分点，但 16 个任务出现负收益。当前官网的 v1.1 任务数已有变化，引用论文实验时用论文版，另跑新版则写明数据版本。Studio 应检验每个角色技能在留出项目上的效果。[SkillsBench 论文](https://arxiv.org/abs/2602.12670) · [官方基准与版本](https://www.skillsbench.ai/)

1. **冻结输入。** 记录 Studio 提交与未提交补丁摘要、各角色设置和已生效技能版本、模型标识、thinking 开关、总 token、步数和时限。保存初始项目文件清单及哈希、题目和授权过的业务回答。在每个实验条件使用独立的群和知识存储副本，同一条件的两题保持同一群。第 2 题读取该条件第 1 题的实际公开数据库。
2. **一次只改一个因素。** 先以当前运行时为对照，仅增加按内容版本和读区间省略重复观察，再试加入可追溯的语义摘要。单独比较无角色知识、用户整理的技能、模型提议并经用户审核的技能。测试学习效果时先在训练项目上生成和审阅候选，再冻结知识库去做留出项目。交接工具/预算的对比另开一组，各组均保留协议和发布校验。
3. **保留定额和人工边界。** 先做离线轨迹回放，核对旧文件引用、工具配对、失败检查及会话恢复。随后每条件先做三次真实配对运行，每次都从同一初始快照开始。按相同规则处理业务澄清，记录谁在何时给了什么答案。禁止人在进行中替模型催交接或修产物。每题单独记平台终态、成员公开情况和外部业务结果。
4. **用副本做独立验收。** 第 1 题从文档所写命令启动，Chromium 经可见控件预约、过滤、取消，再约已释放时段。HTTP 与界面各测 00:00–00:15、20:15–20:30、23:30–23:45、23:45–24:00，另测冲突、相邻、粒度、姓名、重启持久化和历史数据。第 2 题从首页打开仪表盘，独立 SQL 按预约日期过滤，比较有效预约数量、分钟数、取消率和高峰时段。覆盖并列、空区间，在副本上新增和取消后再刷新。上述六行基线在完整区间应得到 225 个有效分钟和 2/6 取消率。遇到其他真实输入时由 SQL 重新计算。
5. **记录分子与分母。** 每条件报告公开交付通过题数/总题数、必需检查通过数、错误标为成功的次数、人工催办次数、已公开与仅私有草稿数。失败归因同时记录首次真实编辑与首次可执行检查的轮次、同版本已读范围的重读字节、工具正文伪调用及纠正次数、每角色消耗和负责人实际剩余额度。对于新技能，再记录可适用任务数、检索/加载次数和留出任务业务表现。任何凭据持久化、跨角色读取或错误公开都需单独列出。三次配对只用于初筛，报告波动后再决定是否增加样本。


最终计分按完整任务统计。只有用户能取得确切版本，并且该版本通过全部必需的独立业务检查，该任务才计入公开通过数。
