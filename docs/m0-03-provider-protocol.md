# M0-03：DeepSeek 多轮工具协议与请求边界

> 更新 2026-11-17（America/Los_Angeles）；基于已合入 `main` 的 M0-02 `07c9fe2`。开发分支 `jinyuwu/m0-03-provider-protocol`。本阶段选择 **DeepSeek 官方 Chat Completions API**；离线协议实现与 L1 回归已落地，thinking 关闭/开启两种模式的 L2 线上往返均已通过。不得将 mock、请求体 fixture 或 `not_run` 报告视为线上通过。

## 1. 范围与责任

本阶段让现有 Provider → RAO → Agent/Planner 链路正确传递模型协议，失败时停止不完整响应的执行，并记录实际可知的请求用量。仍使用同步、非流式调用和现有调度器；不引入 HKGAI、Beta strict tools、数据库或根预算账本。

| 模块 | 写入/输出 | 调用者责任 |
| --- | --- | --- |
| `config.py` / `.env.example` | 校验模型、thinking、请求超时、输出上限及重试配置 | CLI 显式读取进程环境；程序调用者可直接传 Settings/Provider 参数 |
| `providers.py` | 构建完整请求快照，校验响应结构、调用 ID、结束原因和 usage，产出 LLMResponse | 不执行工具；畸形参数保留原串交 RAO 反馈；畸形信封直接失败 |
| `provider_http.py` | HTTPS POST、同一请求体有限重试、可解释的 ProviderError 元数据 | 不重放任何工具操作；不回显远端错误正文、请求内容或密钥 |
| `schemas.py` | 规范化回复、原始参数和单次用量；唯一 assistant 消息序列化入口 | `reasoning_content` 属协议历史，只在内存往返；不得直接将完整对象 dump 为普通日志 |
| `rao.py` | 完整批次的 assistant/tool 回写；T_PROTOCOL 终止；每步安全元数据 | 每个 tool_call_id 恰好得到一条结果；异常后剩余动作只写 skipped |
| `agent.py` / `orchestrator.py` | 响应正常结束后才执行；Worker 再进入验收；记录 step、plan_usage、provider_error | Planner 的截断 JSON 即使能解析，也不能产生可执行计划 |
| `metrics.py` | 已知 token、估算来源、HTTP 次数和未知用量标记 | 不用零值推定未知消费；不重复累加 task_end 的聚合值 |
| `provider_smoke.py` / CLI | 无外部副作用的三轮工具探针与脱敏报告 | 仅真实 API 调用且完整通过时记录 `live_verified=true` |

## 2. 配置契约

以下默认值是**项目策略**，不表示官方 API 的默认值。官方当前支持的模型和参数以文末资料为准；更换模型必须更新配置校验和协议证据。

| 环境变量 | Settings 字段 / 类型 | 默认与约束 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | `api_key: str` | 空值仅允许离线工作；真实调用前拒绝；不参与 Settings repr |
| `DEEPSEEK_BASE_URL` | `base_url: str` | `https://api.deepseek.com`；HTTPS，无 userinfo/query/fragment，可使用官方 `/v1` 路径 |
| `DEEPSEEK_MODEL` | `model: str` | `deepseek-flash`；另支持 `deepseek-v4-pro` |
| `DEEPSEEK_THINKING` | `thinking: Literal['enabled','disabled']` | `disabled`；每次请求显式传 `thinking.type` |
| `DEEPSEEK_REASONING_EFFORT` | `reasoning_effort: Literal['low','high','max']` | `high`；仅 thinking 开启时发送 |
| `DEEPSEEK_MAX_TOKENS` | `max_output_tokens: int` | 300000；1..393216 |
| `DEEPSEEK_TIMEOUT` | `request_timeout_seconds: float` | 120；有限正数，单次网络等待上限 |
| `DEEPSEEK_DEADLINE` | `request_deadline_seconds: float` | 3600；有限正数，一次 post_json 传输包含重试的 deadline |
| `DEEPSEEK_MAX_RETRIES` | `max_retries: int` | 2；0..10，表示初次请求以外的次数 |
| `MA_TEMPERATURE` | `temperature: float` | 0；0..2；非 thinking 请求显式发送零，thinking 请求省略 |

直接构造 Settings/DeepSeekProvider 时同样校验；布尔值不充当数字，NaN/Infinity 不可作为有效配置。修改 Settings 后，创建 Provider 仍会重新验证。

`.env` **不会自动加载**。macOS / Linux 可复制仓库的 `.env.example`，只在自己的可信本地文件中填入密钥，再在同一终端运行：

```sh
set -a
. ./.env
set +a
python -m miniagent provider-smoke --thinking disabled --output build/probe-disabled.json
python -m miniagent provider-smoke --thinking enabled --output build/probe-enabled.json
```

PowerShell 可直接设置进程环境变量后执行相同 Python 命令。`.env` 保持 Git 忽略，`.env.example` 可提交。密钥不进入聊天、测试 fixture 或报告。基地址由本机操作者配置；传输层拒绝重定向，避免携带凭证自动跳到另一个地址。

## 3. 请求、回复和历史的数据契约

`chat(messages, tools=None, temperature=0.0) -> LLMResponse` 先复制 JSON 消息快照，再发 HTTP。重试期间不重新读取调用者可变对象。仅支持文本 content；非流式 `stream=false`。有 tools 时使用 `tool_choice=auto`。

| 字段 | 类型 / 默认 | 规则 |
| --- | --- | --- |
| `LLMResponse.text` | `str = ''` | 公开答复文本；不拼入推理正文 |
| `content_is_null` | `StrictBool = false` | 保留官方 `content: null`；不能与非空 text 同时成立 |
| `reasoning_content` | `StrictStr \| None = None` | 保留原始推理字段；repr 不展示。thinking + tools 的历史 assistant 均须有字符串字段，包括之前的最终答复 |
| `tool_calls` | `list[ToolCall]` | 保留响应顺序；同一批次 ID 非空且唯一 |
| `finish_reason` | `str` | 官方适配器要求明确且为已知值；空值只兼容旧自定义 Provider |
| `model` / `request_id` | `str` / `StrictStr \| None` | 响应模型和响应 ID；可记录在普通 trace |
| `request_attempts` | 严格正整数，默认 1 | 适配器尝试次数；transport=http 时为实际 HTTP 次数，scripted 为一次虚拟调用；与 RAO step 不同 |
| `usage` | `Usage` | 见下节 |
| `ToolCall.id` / `name` | `StrictStr`，必填 | 原生适配器与 RAO 执行边界均拒绝空值 |
| `ToolCall.type` | `Literal['function']` | 当前只允许函数工具 |
| `ToolCall.raw_arguments` | `StrictStr \| None = None` | 来自 API 时必为原始字符串；空串仍原样保留。只有宿主构造的 None 才从 arguments 序列化 |
| `ToolCall.arguments` | `dict[str, Any]` | 解析结果；执行时使用 raw_arguments 中的真实值，防止双份参数不一致 |
| `ToolCall.parse_error` | `str \| None` | 无效 JSON、重复键、非对象、非有限数值均拒绝执行，并生成错误工具结果 |

`to_assistant_message()` 统一处理普通 answer、控制工具和多个工具的消息。禁止手写一个只含 content/tool_calls 的简化版本而丢失 reasoning。

历史满足以下顺序：`assistant(tool_calls=[c1,c2]) → tool(c1) → tool(c2) → 下一条 assistant/user`。工具警告及人工反馈必须等批次闭合后再追加；达到动作上限、循环检测、abort 后，尚未执行的调用各回一条 skipped，不能遗漏或重复 tool result。调用 ID 以批次为作用域。

正常 `stop` 必须无 tool_calls，正常 `tool_calls` 必须有调用。`length`、`content_filter`、`insufficient_system_resource`、`aborted` 不视为成功答复：保留用量，RAO 返回 `T_PROTOCOL`，Agent 终态 `error`；不执行该响应中的工具，Worker 不调用验收器；编排器仍可生成 root 验收报告，但规划/执行错误优先决定 error 终态，即使 root 检查 passed 也不计成功。合法但不完整的调用批次可以写入历史并补 skipped；坏信封整批拒绝。

## 4. 用量和重试语义

`Usage.prompt_tokens`、`completion_tokens` 为严格非负整数，总量只取两者之和。

- `source='reported'`：供应方给出的有效计数。
- `source='estimated'`：ScriptedProvider 的离线估算；不构成真实账单证据。
- `source='unknown'`：未提供 usage 或调用失败而未知。保留已知部分，但数值 0 不代表零成本。

可选字段 `reported_total_tokens`、`prompt_cache_hit_tokens`、`prompt_cache_miss_tokens`、`reasoning_tokens` 保留在单次响应。官方 usage 中 total 必须等于 prompt+completion，缓存子项不得超过 prompt，两个缓存子项均存在时必须合计为 prompt，reasoning 不得超过 completion。这些子项不再次加到 total。聚合明细只在两边均有该项时相加；当前 Agent 的 StepRecord 仅汇总 prompt/completion 与来源，不宣称保留全部缓存明细。

`Provider.transport` 标记 `http/offline/unknown`；`step` 和 `plan_usage` 记录 `transport/usage_source/finish_reason/model/request_attempts/request_id`；`provider_error` 记录 transport、静态 code、HTTP status、attempts、未知来源。普通 trace 不记录 reasoning 正文、HTTP body 或 API key。工具自身的输出仍遵循原 trace 规则，不代表日志系统已实现通用敏感数据过滤。

指标保留已有 `total_tokens`（已知部分，可能含离线估算），增加 `usage_estimated_calls`、`http_attempts`、`usage_unknown_attempts`。仅 transport=http 计 HTTP；scripted/offline 始终为零，自定义或旧记录未声明 transport 则计入 `http_attempts_unknown_calls`，不推断已经联网。成功请求若经历 n 次 HTTP，最后一份 receipt 只能证明最后一次用量，其余 n−1 次可能已经产生费用，均标未知；逻辑调用也标为用量不完整。失败的 provider_error 将其全部已发起 attempts 标为未知；准入失败 attempts=0 不凭空产生 HTTP 消费。重放 trace 仍按 call/step 身份去重。Agent 聚合 Usage 在有重试未知部分时标 unknown。

HTTP 自动重试仅覆盖网络/超时和状态 `408/429/500/502/503/504`。`400/401/402/403/422`、响应 JSON/协议错误、重定向均不自动重试。指数退避加少量 jitter；最后一次失败后不再 sleep。有效 Retry-After 是等待下限，超过 10 秒退避上限或剩余 deadline 时直接结束，不缩短后提前重试。

每次 HTTP 等待使用 `min(timeout, remaining_deadline)`；读取响应按块检查 deadline，最多 16 MiB，非流式 keepalive 空白也占用时间和容量。同步 urllib 无法强行中断其内部 DNS、连接/TLS/header 处理或 JSON 解析；返回控制后再拒绝超时结果。此 deadline 从 post_json 开始计时，包含传输与外层 JSON 解码，不包含 chat 的前置消息快照、后置协议校验及 arguments JSON 解析。因此这是合作式传输边界，**不保证进程级硬墙钟终止**，也不是 M0-06 的根预算/全任务 deadline。

自动重试只重发 HTTP，不重新执行 RAO 工具。传输失败不证明供应方没收到请求，因此不能宣称模型请求“恰好一次”或零额外费用。

## 5. 探针和验收证据

命令 `python -m miniagent provider-smoke` 最多调用模型三次（每次再受 retries/deadline 限制）：

1. 模型请求 `get_probe_token({})`；宿主此时才生成随机 token，在内存作为工具结果返回。
2. 模型请求 `verify_probe_token({'token': 刚收到的值})`；宿主检查精确匹配，再回结果。
3. 模型给出纯文本 `PROBE_OK` 且正常 stop。

工具没有 shell、文件、网络或真实电脑维护动作。随机值不预先进入提示，第二轮必须消费第一轮结果。顺序、工具名、参数或结束原因错误即失败；不无限追问直到模型碰巧通过。thinking/disabled 两种模式分别跑并留存报告，模型或关键配置变化后须重做对应 L2。

报告只含状态、证据级别、模型/模式和调用元数据；不保存 prompt、response 正文、reasoning、参数、随机 token 或密钥。退出码：0=passed，1=探针失败，2=not_run/配置或报告写入错误。无密钥时输出 `status=not_run, reason_code=missing_api_key, live_verified=false`，不会联网；这里的 `evidence_level=L2` 表示待验证的证据类别，并非已取得 L2。

`run_provider_smoke(provider, live=False)` 用于离线 fixture，默认 L1。CLI 真实适配器调用显式设置 live=True；`live` 不是密码学证明，独立审计仍需结合运行环境和官方控制台记录。探针报告不能直接冒充任务的 AcceptanceReport，也不计入 Goal TCR。

电脑维护场景中，维护 Agent 可先请求“读取诊断”工具，再依据工具结果调用“生成修复建议”工具，最后交由 M0-02 验收；M0-03 保障这段消息往返和异常停止。真实电脑访问、危险操作授权、持久角色记忆、根预算和恢复执行依然分别由后续模块承担。探针只证明协议能力，不证明维修效果。

## 6. 回归与后续

```sh
python -m unittest tests.test_providers tests.test_provider_http tests.test_rao_protocol tests.test_provider_runtime tests.test_provider_smoke tests.test_provider_usage_metrics tests.test_config -q
python -m unittest discover -s tests -t . -q
```

完整离线回归 **417 项通过**（比 M0-02 增加 121 项）；53 个 Python 文件通过 Python 3.10 语法解析检查。L1 覆盖请求体、thinking 历史、原始参数、批次闭合、坏 ID/参数、截断终止、HTTP 重试/deadline/redirect、用量来源、trace 隐私和探针完整性。完整套件还回归 M0-01 契约、M0-02 独立验收及三种调度器。当前验证环境 Python 3.14.7 / Pydantic 2.14.3；Python 3.10+ 仍为声明支持范围，语法检查不能替代各版本运行测试。

### 6.1 2026-11-17 线上实测记录

在 `deepseek-flash`、官方 `https://api.deepseek.com` 上实际执行以下两次探针，均满足 `status=passed`、`live_verified=true`、`reason_code=ok`、`logical_calls=3`。

| 模式 | 开始时间（America/Los_Angeles） | 结束原因序列 | HTTP 次数 | 总 token（API reported） | 探针墙钟 |
| --- | --- | --- | --- | --- | --- |
| thinking=disabled | 2026-11-17 02:31:29 PST | tool_calls → tool_calls → stop | 3，无重试 | 1518 | 4.45 秒 |
| thinking=enabled | 2026-11-17 02:31:58 PST | tool_calls → tool_calls → stop | 3，无重试 | 1641 | 4.81 秒 |

实际配置：输出上限 4096 token，网络等待上限 60 秒，单请求传输 deadline 120 秒，额外重试上限 2；关闭模式 temperature=0，开启模式 reasoning_effort=high。开启模式的三份回复均含 reasoning 字段（允许空串），并由序列化入口原样回传；报告只记字段是否存在，未存正文。两种模式均实际消费了上轮随机工具结果，最终正常答复；总计 6 次请求、3159 reported token。真实请求通过不等于所有网络失败路径已在线故障注入；后者仍以 L1 为证据。

脱敏证据随本阶段提交保存：

- [关闭模式原始元数据报告](evidence/m0-03/2026-11-17/probe-disabled.json)
- [开启模式原始元数据报告](evidence/m0-03/2026-11-17/probe-enabled.json)
- [验证清单与源码/报告 SHA-256](evidence/m0-03/2026-11-17/manifest.json)

清单中的 base_commit 是开发基线，源码哈希标识实际验证的未提交实现；发布提交包含相同源码及这些证据。`.env` 留在本机且被 Git 忽略。2026-11-16 的 `not_run` 仅是当时缺少凭证的记录，不再代表本轮验收状态。

M0-03 的本阶段出口已满足；M0-04 持久任务身份、M0-05 隔离、M0-06 根预算账本继续按分阶段计划推进。

## 7. 官方依据

本阶段核对官方资料，查阅日期 2026-11-16：

- [Create Chat Completion](https://api-docs.deepseek.com/api/create-chat-completion/)：模型、thinking/effort、max_tokens、finish_reason、usage 与工具字段。
- [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)：thinking 的工具历史和 reasoning_content 回传要求。
- [Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/)：assistant/tool 消息对应、工具循环与 Beta strict 模式的额外要求。
- [Error Codes](https://api-docs.deepseek.com/quick_start/error_codes/)：鉴权、参数、限流及服务错误分类。
- [Rate Limit](https://api-docs.deepseek.com/quick_start/rate_limit/)：排队与非流式 keepalive 行为。

官方文档可能更新；本项目的重试次数、退避上限、超时、响应大小和探针轮数均为本地工程策略。


## 2026-11-17 输出预算调整

按用户要求，将项目默认单次输出上限及本机配置提高至 **300000 token**。官方 `deepseek-flash` 的上下文窗口为 1M，Chat Completions 的 `max_tokens` 范围为 1..393216；上下文窗口、单次输出上限、任务多轮累计预算是不同参数。500000 不能作为该接口的 `max_tokens`。上限不代表模型必须输出这么长，正常 `stop` / `tool_calls` 仍可提前结束。

为避免下一级限制立即中止长任务，`Limits.max_total_tokens` 从 200000 改为 1000000（现有子任务 0.8 分配后为 800000），`Limits.max_seconds` 从 120 改为 3600；provider 的 socket 等待从 60 改为 120 秒，总请求 deadline 从 120 改为 3600 秒。步数 12、每轮工具数 4、工具超时 20 秒保持原值。这些是项目运行策略，并非官方 SLA；现有任务预算仍是轮次间检查和事后累计，不是严格的费用预留账本。

新默认值统一用于 Settings、直接构造 Provider 和环境变量缺省值；`.env.example` 同步更新。已验证本机 `.env` 的三个非密钥数值项已更新，密钥内容保持原样。历史事故日志、旧配置快照与 M0-03 线上验证记录中的 4096 保留为当时的证据。

完整离线回归 **566 项通过**（39.722 秒，ResourceWarning 作为错误），包括模拟 300000 输出 token 的工具调用后继续第二轮并通过独立验收；该用例不会真实生成或消费 30 万 token。未重新发起付费任务。

官方依据：[Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing/)、[Chat Completions max_tokens](https://api-docs.deepseek.com/api/create-chat-completion/)、[连接保活说明](https://api-docs.deepseek.com/quick_start/rate_limit/)。
