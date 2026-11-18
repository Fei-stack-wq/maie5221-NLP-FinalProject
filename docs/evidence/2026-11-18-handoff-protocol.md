# 真实软件组的末轮交接与轮数预算

2026-11-18 软件组运行 `studio-20261118-035123-21f37d898a08` 中，成员任务 `task-9555c1c57c464063bc13fad102b7c210` 在第 21 轮、`task-a4eb6c7a91f04f07ae89bba211a7f26f` 在第 3 轮进入交接。两次模型在正文返回工具协议文字。两次 `step_start` 的 `offered_tools` 为空，本次调用扣除后 `steps_remaining=0`，随后 `tool_protocol_correction.retry=false`。累计 token 均不限。宿主正确地拒绝执行工具文字，但先前仅为一次正常交接预留轮数，无法在这两个分配内请求纠正。

OpenAI Agents SDK 在每次模型工作前检查 `current_turn > max_turns`，其测试检查超限处理、终态输出的校验和失败时保留记录。Pydantic AI 将工具输入与最终输出的纠正分别计数。Studio 借鉴两者，在本地已有预算内为成员和负责人分配末轮机会。每次分工中，工作工具协议和收尾文字协议各允许一次纠正。协议文字只保留作审计；已执行工具、用户决策、工件与独立验收仍按原入口处理。[SDK 限步处理](https://github.com/openai/openai-agents-python/blob/e7ce15a25b336bb3d0840ad16dee597f69a6e6d3/src/agents/run.py#L1585) · [SDK 超限/失败处理测试](https://github.com/openai/openai-agents-python/blob/e7ce15a25b336bb3d0840ad16dee597f69a6e6d3/tests/deferred_session_writes/test_max_turns.py#L40-L108) · [Pydantic AI 输出纠正](https://github.com/pydantic/pydantic-ai/blob/f29b6cdff38c9128c0504bdf8278d8c9c77f80a8/pydantic_ai_slim/pydantic_ai/_agent_graph.py#L4251) · [工具与输出的重试配置](https://github.com/pydantic/pydantic-ai/blob/f29b6cdff38c9128c0504bdf8278d8c9c77f80a8/docs/retries.md#L30-L34)

## 回归

- 6 步的真实 Studio 群组服务使用脚本模型，先委派，成员写入文件，成员和负责人分别返回一次未结构化的工具文字，两人各在一次无工具的后续请求中纠正。文件通过独立验收，所用总数恰好 6 步，伪工具未产生工件。
- 总计 4 步时，成员仍有一次可执行的工作调用。剩余分工轮数不足以纠正无效交接时，成员的草稿保留、公开交付失败，始终遵守房间硬预算。
- 在一次角色执行内，工作阶段已纠正一次错误工具正文，收尾阶段仍可在其分配内纠正一次。模型再次返回畸形正文、检查失败等原有门禁仍然生效。

上述用例独立于软件题的真实模型交付验收。[群内回归](../../tests/test_studio_crew.py) · [协议回归](../../tests/test_step_policy.py)
