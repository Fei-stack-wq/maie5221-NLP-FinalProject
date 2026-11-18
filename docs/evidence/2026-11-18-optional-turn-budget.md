# 可选的累计模型轮数

在新任务或续聊的「本次执行预算」选择「不限轮数」，或把「执行轮数上限」设为 0。每次模型决策和实际工具调用仍计入用量。CLI 支持 `python -m miniagent run "…" --max-steps 0`。`MA_MAX_STEPS=0` 作为本机默认值；已有 `.env` 中明确配置的正数仍然生效。

## 配额与终止条件

Studio API 的 `limits.max_steps` 接受整数 0 至 1000。任务入场时保存实际配置。早期版本未保存配置快照的运行，会话从原请求或该次运行的 `studio_configuration` 读取记录。

运行时的 `Budget.steps_left` 和 `Budget.actions_left` 各自使用 `None` 表示无累计上限。有限余额减到 0 表示用尽。父任务、子任务和返工继续继承这一语义；`steps_used`、`actions_used` 对两种模式都累计真实消耗。正数上限仍派生相应的累计动作预算。显式不限时，不再对整个群轮次套用固定 16 次委派次数。每个工具批次、负责人用于勘察的局部配额、已设定的总时间和 token 预算仍分别生效。

任务在提交并通过指定验收、收到取消、超时、用尽已配置的有限预算或遇到运行错误时结束。原有的重复工具调用检测、无进展返工判定、检查证据和交接保留仍参与决策。应用可在无限累计轮数模式下对无效循环给出具体原因。单次上下文、最大输出和协议重试规则也继续独立生效。

## 实现依据

AutoGen 的群管理器会先处理配置的终止条件。它在 `max_turns` 有值时才比较累计轮数。官方 RoundRobinGroupChat 测试展示了使用语义终止条件的群聊。[管理器实现](https://github.com/microsoft/autogen/blob/8390985108cb1e3881f5f7297765758d8ad2540c/python/packages/autogen-agentchat/src/autogen_agentchat/teams/_group_chat/_base_group_chat_manager.py#L195-L226) · [AutoGen 群聊测试](https://github.com/microsoft/autogen/blob/8390985108cb1e3881f5f7297765758d8ad2540c/python/packages/autogen-agentchat/tests/test_group_chat.py#L226-L259)

OpenAI Agents SDK for TypeScript 支持 `maxTurns: null` 和独立的 `AbortSignal`。其测试让运行越过默认轮数并正常结束，也覆盖对恢复后的运行显式取消轮数上限。Studio 沿用自己的整数 JSON 配置接口，用 0 对应内部可空余额，另外保留用户控制的墙钟预算和成员分配。[SDK 接口](https://github.com/openai/openai-agents-js/blob/1215a24fb4725742c7b88094812b7ba18c46041c/packages/agents-core/src/run.ts#L462-L472) · [恢复时检查限额](https://github.com/openai/openai-agents-js/blob/1215a24fb4725742c7b88094812b7ba18c46041c/packages/agents-core/src/runState.ts#L3231-L3244) · [不限轮数与恢复测试](https://github.com/openai/openai-agents-js/blob/1215a24fb4725742c7b88094812b7ba18c46041c/packages/agents-core/test/run.test.ts#L5414-L5489)

## 验证入口

- `tests/test_open_turn_budget.py` 通过完整 Agent 调度完成 102 次决策和 404 个工具动作，实际验收最终文件。另一路经 Studio 完成 17 次各有文件变化的派工及第 18 次独立审阅。用例还验证有限上限、显式取消、重复操作及子任务预算继承。
- `tests/test_studio_crew.py` 覆盖受阻草稿在同一版本的返工、交接纠正及明确有限租额下的保留。`tests/test_cli.py`、`tests/test_studio_harness.py` 验证 CLI 和历史快照。
- `tests/web/room_workflow.cjs` 用真实浏览器在新群指定 37 轮，下一轮切换为不限，并检查发给 Studio 的请求值。
