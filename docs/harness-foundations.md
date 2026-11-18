# Harness 基础能力：失败复盘、开源参照与验收门槛

> 更新：2026-11-17。关联 [开发计划](agent-studio-development-plan.md)、[Provider 协议](m0-03-provider-protocol.md)、[本地工作室](local-studio-gui.md)。本文说明 M0-H01 本轮修复范围及后续基础门槛；不以单元测试数量或功能列表宣称达到所有商用 Agent 的完成率。

## 1. 最新任务为什么失败

`studio-20261117-045015-573d15536ea1` 的任务是“帮我完成一个贪吃蛇 web 游戏，画面精美一点”。这次中断的原因是 **最多 12 轮模型调用**，不是单次输出上限。

| 证据 | 实际值 | 说明 |
|---|---|---|
| 运行配置快照 | `max_steps=12`、`max_output_tokens=300000` | 提高输出上限没有同时修复轮数预算 |
| 12 次模型响应 | 均为 `finish_reason=tool_calls` | 模型还在通过工具工作，没有返回最终答复 |
| 累计 completion usage | 31,709 tokens | 这是各次响应累计，不是单次输出长度 |
| 终止事件 | `limit_exceeded`，`超过最大步数 12` | 在允许的循环次数耗尽后停止 |
| 保留下来的工件 | `index.html`、`style.css`、`game.js`、`test/smoke.js` | 已写出代码并进行修正；失败不能把已有成果变成不可取用 |

原始证据位于对应运行的配置、`trace.jsonl` 与 `agents/worker/`。这次工具成功和 Node 进程退出正常，只能证明这些操作发生过；不能替代浏览器输入、碰撞、暂停、重开、移动端交互等游戏功能验收。

更早的 `studio-20261117-042241-18dcd49cc364` 才是实际输出截断：日志记录该次 completion 为 4096 tokens，模型返回 `finish_reason=length`；旧运行没有完整配置快照，不能仅凭用量反推当时显式配置的上限。它与本次 12 步耗尽是两种故障。诊断不能沿用前一任务的原因，也不能把执行错误统一翻译成“未验证”。

## 2. 必须分别管理的限制

| 限制 | 含义 | 运行时应该怎样处理 |
|---|---|---|
| 模型上下文窗口 | 一次请求可容纳的输入及相应输出空间 | 控制请求历史大小，为输出留空间；过大时有限恢复 |
| 单次输出上限 | 本次模型响应最多生成多少 token，可能包含推理 | 截断时禁止执行不完整工具参数，尝试更小的工作单元 |
| 任务累计 token 预算 | 多次调用的 prompt + completion 总和 | 每次调用都记录用量。用户设置正数上限时，恢复调用也计入该上限 |
| 步数预算 | 允许多少轮模型调用 | 合理默认、用户可调，终止时清楚显示已用 / 上限 |
| 时间与请求超时 | 整个任务墙钟时间、单请求等待边界 | 剩余时间约束后续动作，避免新子任务重置时钟 |
| 单工具输出及文件大小 | 防止一次读取或输出压垮进程和模型上下文 | 分页、明确截断标记、按需重读；不能静默伪造完整内容 |

扩大某一项不会自动扩大其余各项。一次生成整份大文件不是唯一工作方式；分页读取、局部修改、分段构建和持续测试，可以让多个小而完整的操作完成同一目标。

### 2.1 可选的累计 token 上限

自 2026-11-18 这次修订起，新任务默认仅统计整个任务累计使用的输入和输出 token。即使一次请求只用 8 万个 token，二十次这样的请求也会累计为 160 万。单次请求能容纳多少输入和输出仍由模型上下文窗口决定。Studio 保留轮数、墙钟时间及用户取消等停止条件。

- **CLI 与本机网页**。环境变量 `MA_MAX_TOTAL_TOKENS=0` 表示累计不限，缺省也是 0。设置为正整数可启用上限，例如 `1000000`。Studio 新运行会保存生效配置，诊断页把 0 显示为“不限”。上述选项均保留用量统计。
- **单次任务与已有会话**。启动接口的 `limits.max_total_tokens` 可覆盖服务配置，接受 0 至 100000000 的整数。旧运行的请求、配置快照和已记录故障保留当时的值。在旧会话的“本轮选项与预算”中点击“本轮设为不限”，会为新一轮提交 0。明确写入环境或请求的正数上限继续生效。
- **上游设计依据**。OpenAI Agents SDK 的 `Usage` 在每次运行中累加请求数及输入、输出 token，并提供逐请求明细。其 `Runner.run` 将 `max_turns` 作为独立的执行参数。Studio 同样把用量统计与停止策略分开，累计额度由用户选择。[OpenAI Agents SDK Usage](https://openai.github.io/openai-agents-python/usage/) · [Runner.run](https://openai.github.io/openai-agents-python/ref/run/#agents.run.Runner.run)
- **上下文依据**。Pi 的指定版本中，工具及待处理消息驱动 `runLoop` 继续执行。编码助手在当前上下文接近模型窗口时生成摘要，并保留近期消息。Pi 的 `reserveTokens` 和 `keepRecentTokens` 分别调节当前窗口的预留空间和近期历史的保留数量。Studio 用独立配置项分别表达请求上下文容量和任务累计额度。[Pi agent loop，固定源码版本](https://github.com/earendil-works/pi/blob/efd284ecca3788ca1345fdcc927a1f97e7ceeeaf/packages/agent/src/agent-loop.ts) · [Pi 压缩机制，同一版本](https://github.com/earendil-works/pi/blob/efd284ecca3788ca1345fdcc927a1f97e7ceeeaf/packages/coding-agent/docs/compaction.md)

## 3. 开源项目提供了什么机制

以下依据 2026-11-17 读取的官方文档与代码。链接指向可变分支，因此这是机制调研，不是固定版本性能实验；后续比较必须固定 commit。`badlogic/pi-mono` 的当前官方内容已转向 `earendil-works/pi`。DeepSeek Harness 官方 README 标记为 developer preview，不能将其全部行为当作成熟性保证。

| 能力 | 可核查的官方参照 | 本项目采取的做法 / 尚存差距 |
|---|---|---|
| 任务循环与结束信号 | Hermes CLI 支持配置 `--max-turns`；当前文档默认 unlimited 或配置值，并为预算失败、取消、完成提供不同结果。[CLI 参考](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/cli-commands.md) | 默认 100 轮并可配置，保留有界预算；明确区分步数、输出、累计 token、时间和取消 |
| 上下文管理 | pi 在完整工具批次之后检查压力，加入摘要及保留边界，再派生下一次请求；原始会话记录仍保留。[Compaction](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/compaction.md) | 本轮区分原始历史和请求视图，裁剪早期完整批次；**没有语义摘要**，不能宣称完整保留旧决策 |
| 压力估计 | Hermes 将供应商 usage、增量估计和无 usage 的回退分开；恢复有尝试预算和无进展保护。[Context compression](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/context-compression-and-caching.md) | 本轮使用真实 usage 锚点与保守增量估计；前缀/schema 必须精确匹配。它是准入启发式，不是供应商精确 tokenizer，也不是账单 |
| 大输出与恢复 | DeepSeek Harness 可先裁剪过大工具结果，再摘要；确认上下文溢出后有限重试。单个不可分的大工具调用不能靠历史压缩解决。[Compaction basic](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/compaction/compaction-basic/README.md) | 本轮加入有界恢复、小块文件工具及明确诊断；摘要调用、摘要质量验证留到下一基础阶段 |
| 编程工具 | pi `read` 支持行 offset / limit，`edit` 支持唯一精确匹配和多个不重叠修改。[read 源码](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/tools/read.ts)、[edit 源码](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/tools/edit.ts) | 本轮提供分页读取、精确编辑和追加；模型无需为一行修正重发整份文件 |
| 会话与继续工作 | pi 自动保存会话，提供 continue / resume，压缩不删除原始条目。[Sessions](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sessions.md) | TaskStore 已持久化身份与状态；完整执行检查点、预算恢复、未知副作用核对仍未交付 |
| 审计与取消 | DeepSeek Harness 从追加事件日志派生模型历史，并暴露活动 turn 的 cancel 接口。[Session](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/session.md) | 本轮保留原始消息/用量/恢复事件，增加协作式取消；尚不能即时中断当前同步 HTTP 调用 |

这些设计并不意味着其他 Agent 永远不会截断、超时或失败。它们表明：限制应当可见，失败应当可恢复，文件与会话应当可继续使用；每种恢复也必须有预算和可验证的副作用边界。

## 4. M0-H01 本轮职责与行为契约

### 4.1 配置、诊断和成果取回

- `Limits`、配置入口及 Studio 负责一致的运行预算。默认 `max_steps=0`（不限轮数及派生的任务工具调用总数）、任务累计 `max_total_tokens=0`（不限）、`max_seconds=3600`；单次模型输出仍为 `300000`。新任务保存实际生效快照，旧任务快照保持原样。
- `MA_MAX_STEPS`、`MA_MAX_TOTAL_TOKENS`、`MA_MAX_SECONDS` 支持本机默认；任务发起入口可覆盖相应预算。输入校验不能接受布尔值、非有限数或越界值。
- `StudioService` / 诊断层根据真实退出原因显示限制名称、消费及上限；恢复事件显示为恢复过程。文件存在、模型自称完成、工具返回成功均不能升级为独立验收通过。
- 已失败或已取消任务仍能下载工件。工作区 ZIP 是当时可读取的受支持文本 / 代码文件副本，不是不可变 ArtifactStore；每文件不超过 8 MiB、总内容不超过 20 MiB，超限拒绝导出而不截断文件。它不包含符号链接、隐藏文件、二进制文件或工作区外文件，也不把原始推理和密钥一并导出。

### 4.2 长度恢复与请求上下文

- Provider 继续负责协议校验、原始 usage 和结束原因；RAO 标记可恢复截断并阻止工具执行，Agent 决定是否在同一任务预算内恢复。HTTP 传输重试和模型长度恢复是不同计数，不重放已执行工具。
- 当 `finish_reason=length` 时，无论 JSON 看起来是否完整，都不执行该响应的工具调用。恢复请求要求缩小操作或输出；失败响应保留审计证据，并从后续模型请求视图隔离。
- `MA_OUTPUT_RECOVERY_ATTEMPTS` 默认 2，允许 0–3；用尽后显式失败。恢复不能绕过 token、时间、步数和取消检查。
- 模型返回后先计入实际已报告的用量和时间，再决定能否执行工具；工具批次中每个新动作也检查余额，防止长请求或长工具之后继续产生副作用。实际响应仍可能超出调用前剩余 token，记录真实消费并停止；未知 usage 不伪装成精确计量。这不替代并发预算预留 / 结算的 M0-06。
- `MA_CONTEXT_WINDOW_TOKENS` 默认 1,000,000，用于当前模型的配置边界。请求上下文优先使用 `provider_usage_anchor`：精确匹配上一请求前缀及工具 schema，以供应商 prompt + completion 用量计入已有消息，再估算新增工具结果和用户消息。前缀变化后使用绑定到精确消息快照的 completion 用量与新增字节估计（`reported_completion_and_bytes`）；完全没有真实用量时才使用 `utf8_bytes_upper_estimate`。避免把 700000 字符的代码误当成 700000 tokens。保留所有 system / user 消息与最近两个完整 assistant 批次，不能产生孤立的 tool result 或缺失结果的旧工具调用。此估计不是精确 token 数，也不证明供应商一定接纳请求。
- 原始消息历史与发给模型的精简视图分开。`context_compacted` 的 `strategy=deterministic_omission` 表示删除较早的完整批次；省略标记不是语义总结。当前 `state.history` 在内存中保留完整追加历史，trace 记录运行事件；这还不是崩溃后可重建的持久会话。模型也不能自动找回所有已省略的旧事实。
- 当前请求如果只剩不可删除的目标或一个过大的必要批次，应明确报告不能容纳，而不是无限重试或静默丢掉任务目标。

`step_start` 附带输入估计、输入限额、输出预留、上下文大小和剩余步数；`budget_warning` 在步数 / token 余额较低时各提示一次。长度恢复记录 `output_recovery` / `output_recovery_exhausted`；保留部分仍放不下时以 `context_limit` 结束。这是 11 月 17 日历史版本的行为。11 月 18 日加入的语义摘要及服务端窗口恢复见下节。

### 4.3 文件工作流

`tools.py` 提供以下接口。这里选择字符分页，和 pi 的行号分页不同，调用者不得混用单位。

| 工具 | 参数 / 结果 | 边界 |
|---|---|---|
| `read_file` | `path, offset=0, limit=32768`；正文及可见 hash / `next_offset` 页脚；metadata 包含 `returned_chars, bytes, chars, sha256, next_offset, eof, truncated` | offset 从 0 起算，单位是 Unicode 字符；limit 为 1–65536；逐块扫描并计算全文件 hash，内存有界 |
| `write_file` | `path, content, expected_sha256?` | 每次至多 1,000,000 字符；可用预期 hash 拒绝基于旧版本的覆盖 |
| `edit_file` | `path, old_text, new_text, expected_sha256?` | old_text 非空且恰好一个精确匹配；重叠重复也拒绝；原文件至多 8 MiB |
| `append_file` | `path, content, expected_sha256?` | 向已有 UTF-8 文件末尾追加，不自动添加换行；文件大小边界 8 MiB |

匹配不存在、有歧义或版本过期时，返回可恢复错误并保持原文件。写入采用同目录临时文件、fsync 和替换，并保留权限模式；进程内锁配合版本检查不等于跨进程 CAS。工具沿用既有 Sandbox 路径范围，不构成任意 Python 代码的宿主隔离。

### 4.4 用户取消

Studio → service → scheduler / RAO 传递取消信号。收到信号后，在下一模型 / 工具动作前停止；当前 HTTP 返回后还要再次检查，不能执行它新提出的工具。重复取消请求幂等，已结束运行不改写历史终态。

本轮为**协作式取消**：正在进行的同步 HTTP 或工具调用可能要返回或超时后才落终态。页面显示“正在停止”与实际结束的“已取消”，不能收到请求就假装底层工作已经停止。即时传输取消和子进程树终止留到 M0-H02 / M0-05。

### 11 月 18 日实现与核验

**运行内语义压缩。** 宿主选出可移除的连续工具交互批次。当前请求、真实人工答复和最近的完整工具交互继续以原文提供。单轮接近物理窗口时，系统为新工作腾出空间，必要时节选大型工具回执的首尾。DeepSeek 的辅助请求只读取旧批次的证据和上一次摘要，返回目标、决定、文件、检查、阻塞五个字段。摘要带模型生成标记，主调用仍使用自己的 thinking 配置。无效 JSON、被截断的摘要、经费或时间不足都会降级。每次辅助请求的用量与主调用一起记账。两次官方接口验证与用量见[实测证据](evidence/2026-11-18-semantic-compaction.md)。此阶段只在执行期间维护增量语义摘要；跨轮继续聊天使用持久对话与公开工件重建上下文。[Pi 压缩实现](https://github.com/earendil-works/pi/blob/efd284ecca3788ca1345fdcc927a1f97e7ceeeaf/packages/coding-agent/src/core/compaction/compaction.ts) · [DeepSeek Harness 摘要器](https://github.com/deepseek-ai/deepseek-harness/blob/ca33fd6843b205e279c7ccb6cb73a472c78f5760/packages/compaction/compaction-basic/src/summarizer.ts) · [DeepSeek 工具结果裁剪](https://github.com/deepseek-ai/deepseek-harness/blob/ca33fd6843b205e279c7ccb6cb73a472c78f5760/packages/compaction/compaction-tool-result-pruner/src/index.ts)

**窗口拒绝与恢复。** 普通工具循环收到服务端明确的上下文过长错误后，以更小的输入和输出额度恢复一次。HTTP 层只分类结构化错误、保留已脱敏的错误代码，普通参数错误仍按原路径返回。宿主核对历史末端和新请求的大小，已执行的工具不会因重发而再次执行。当前可用输入空间和单次输出上限分别计算，需要时缩小本次输出。仍无法容纳用户强制保留的输入时，在 GUI 中区分本地预检、供应商窗口拒绝与累计预算不足。恢复测试包含真实路径上的一次文件写入，验证重试未再次写入文件。[Pi 溢出恢复](https://github.com/earendil-works/pi/blob/efd284ecca3788ca1345fdcc927a1f97e7ceeeaf/packages/coding-agent/src/core/agent-session.ts#L3171-L3213) · [Hermes 上下文管理](https://github.com/NousResearch/hermes-agent/blob/09ded63c4fedcc8fe975d37f9765797d904fc75d/website/docs/developer-guide/context-compression-and-caching.md) · [项目回归](../tests/test_context_recovery.py)

## 5. 必须有的可复现验收

以下是行为门槛，不是单纯增加测试数量。测试证据必须区分确定性离线模拟、真实协议与真实任务；具体已运行结果以本次实现的测试报告为准。

| ID | 输入 / 故障注入 | 必须观察到的结果 |
|---|---|---|
| HF01 | 至少 13 次有效工具往返之后才返回最终答复 | 默认预算不会在第 12 次结束；显式设低预算时恰在限制处停止 |
| HF02 | 网页 / 环境设置不同轮数、token、秒数 | 新运行快照与实际限制一致；旧运行仍显示原值；非法输入在启动前拒绝 |
| HF03 | 第一次 `length` 带一个看似完整的写文件调用，第二次正常 | 第一次零副作用；恢复次数和 usage 均可查；仅正常响应工具执行一次 |
| HF04 | 连续 `length` 或恢复中耗尽预算 | 不无限继续；停止原因准确；所有已收到 usage 计入累计 |
| HF05 | 长历史含多个工具调用 / 结果及大文本 | 请求缩小且协议批次完整；原目标保留；原始审计历史不被删除；估计值明确标识 |
| HF06 | 大文件分段读、局部改、追加；错误 hash / 多处匹配 / 写失败 | 可小块完成修改；错误时原内容不变；路径和符号链接边界仍有效 |
| HF07 | 模型在等待期间收到取消，返回新工具调用 | 新工具不执行；记录取消；重复取消不重复收尾；已完成任务不被改成取消 |
| HF08 | 失败 / 取消运行包含 HTML、CSS、JS、嵌套测试文件 | ZIP 可取回相对目录，隐藏/链接/越界文件不被导出；不依赖任务成功 |
| HF09 | 分别制造步数、输出、累计 token、时间和验收缺失 | 页面给出各自原因，预算失败不被“未验证”掩盖；已有产物入口仍可用 |
| HF10 | 真实模型完成小型网页任务并由独立浏览器检查 | 能打开、操作并观察关键功能；代码生成、Node 检查、模型自测与功能验收分开报告 |

HF01–HF09 可由 fixture 覆盖大部分边界；运行时专用回归见 `tests/test_harness_runtime.py`、`tests/test_context_window.py`，文件工具回归见 `tests/test_coding_tools.py`，Studio 控制与产物见 `tests/test_studio_harness.py`。后者 10 项测试在 2026-11-17 通过，覆盖真实 runtime 的单任务 / 规划等待期间取消、实际预算覆盖、持久终态、ZIP 路径与大小边界，全程使用离线 provider。完整回归结果单独记录。HF10 必须有真实模型和浏览器证据。已有游戏不能仅因文件可下载或 Node 退出码为 0 就标成 HF10 通过。

## 6. 在角色培养之前继续补齐的基础能力

| 顺序 | 交付 | 验收门槛 |
|---|---|---|
| M0-H02a | 持久检查点及显式继续：消息视图、原历史游标、动作回执、预算、工作目录一致恢复 | 在工具完成边界重启后继续；已成功写入不重放；未知外部副作用先核对；新 attempt 与原任务关联可查 |
| M0-H02b | 语义压缩与可检索原历史：摘要保留目标、约束、决策、未完成项、文件引用；调用也计费 | 长任务压缩后仍满足预置约束；摘要失败有界退出；能追溯摘要来源；不能借压缩删除失败历史 |
| M0-H02c + M0-05 | 可中断传输、子进程树停止、真实隔离执行 | 取消后限定时间内停止网络/进程；沙箱不能读取宿主秘密或他人工作区；无后台遗留进程 |
| M0-06 / 07 | 原子预算总账、不可变工件与显式交接 | 并发不重复发放余额；恢复不重置 root deadline；下游只收到授权版本 |
| M0-UI-02 / M0-09 | 网页任务的独立功能验收与可重复基准 | 预先冻结规则，既测成功也测失败；报告功能完成率、耗时、用量与人工介入 |

“重开历史查看”和“继续执行”必须是不同入口。TaskStore 的存在、日志完整或下载成功都不证明安全续跑已经实现。基础安全续跑提前到 M0；M2 再扩展人工等待、群聊和委派挂起的恢复。

对“达到常用 Agent 水准”的判断应来自固定测试：至少覆盖从零制作小网页、已有项目修 bug、大文件局部修改、长历史约束保持、输出 / 网络故障、取消 / 重启。固定模型版本、预算、初始文件、独立验收器和运行次数；比较最终通过率、恢复成功率、耗时、实际 token 与人工接管次数。本轮没有完成这一组跨产品对照实验，因此只能声明已修复的行为，不能声明全面对齐。


## 7. 本轮验证记录

2026-11-17：完整离线回归第一次 624 项通过；修正上下文用量锚点后 630 项通过。700000 个 ASCII 字符、供应商报告 175000 completion tokens 的写文件响应，在 1M 上下文 / 300000 输出预留下继续下一轮并通过验收；这使用合成响应，不是实际付费生成。实际仓库最终回归 **631 项通过（6.684 秒）**，ResourceWarning 作为错误；新增非文本伪装产物的导出拒绝测试，避免 ZIP 解码损坏。

真实浏览器在隔离的运行副本与离线模型夹具上验证：150 轮 / 2000000 tokens / 7200 秒覆盖进入配置快照；等待模型时点击取消，先显示待返回状态，返回的 `write_file` 没有写入，持久任务最终 cancelled。下载游戏 ZIP 后四个文件与原产物逐字节一致。9 项持续轮询 DOM 稳定性测试全部通过；390px 视口没有文档横向溢出；控制台无错误/警告。以上没有新增付费模型请求，原任务状态未被改写，亦未将游戏计作 HF10 功能验收通过。

### 累计用量与单次窗口的回放

沿用 OpenAI Agents SDK 逐次记录调用量的做法，Studio 独立保存已用 token 和任务剩余额度。上游的运行循环也把轮数作为单独的停止条件。Studio 默认允许累计用量增长，用户显式设置正数时启用本任务的累计预算。每次发送的输入和输出仍须适配模型窗口。[上游用量记录](https://openai.github.io/openai-agents-python/usage/) · [上游运行循环](https://openai.github.io/openai-agents-python/ref/run/)

2026-11-18 使用任务 `studio-20261117-233025-9ea412ddcfa4` 的 40 组已报告用量做离线回放。原始轨迹 SHA-256 为 `2165c1ba09d7c354f1ff547ebdb9863722648d9e779c2a253aad01785c782d5f`。单次最多输入 43,380 tokens，从头到尾共输入 1,010,961、输出 24,388。测试使用固定的本地动作，在临时目录写入 39 份回执，最后给出答复。每步用量沿用原记录，独立文件断言逐份比对内容。这项实验检验限额行为，未重演模型决策，软件题成绩另行验收。

| 同一组用量 | 实际调用轮数 | 计入用量 | 结果 |
| --- | ---: | ---: | --- |
| 默认不限制累计 token | 40 | 1,035,349 | 39 份回执验收通过 |
| 用户指定累计 1,000,000 | 39 | 991,690 | 剩余 8,310，下一轮输入预检停止，验收尚未执行 |

在仓库中运行 `python -m unittest tests.test_task_token_replay` 即可复算。测试自带 40 对纯数值用量，不需要原机器的记录、凭据或网络。[回放输入和断言](../tests/test_task_token_replay.py)
