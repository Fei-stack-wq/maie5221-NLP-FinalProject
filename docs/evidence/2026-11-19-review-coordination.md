# 已签署评审的下一项分工

11 月 19 日的预订系统运行 `studio-20261119-175301-f21a7ceb3f42` 在作者返工超时后，负责人反复读取已经核对的 README 和源码。基金运行 `studio-20261119-175926-2afc79571582` 已有风控退修意见，负责人仍报告阻塞。两次运行均有可以从宿主状态确定的下一位成员。

Studio 继续让负责人决定初始分工和验收标准。一旦当前候选有待执行的签署结果或冻结的待裁决探针，宿主就从评审队列读取目标、版本、负责人和下一动作。负责人正确派工时照常保留其补充说明，也可以询问真实业务问题。如果负责人尝试结案或再次翻读文件，宿主会沿正常委派入口把工作交给已经确定的成员。变更后的版本仍须独立签署。没有绑定目标的调研与用户点名的独立执行，仍可按需要查阅项目。

工具协议保留模型原回复，给被取代的工具请求逐条配对回执，然后单独写入宿主的任务调用与动作 ID。`bound_handoff_dispatched` 记录哪个候选被交给谁。取消、总预算、权限、作用域和持久动作登记都在正常执行路径上检查。对同一项任务、版本、签署状态与证据修订，宿主只接管一次；实际派工受阻时保留原始错误。

## 上游依据

OpenAI Agents SDK 的 `NextStepHandoff` 将后继执行者作为显式结果。运行循环消费这个结果并更新当前 agent，测试覆盖交接边界取消时的历史保存。Studio 采用明确表示后继成员和保留原始事件的做法，沿用项目自己的冻结候选、独立签署和成员目录隔离。此改动未引入上游运行时依赖。[结果类型](https://github.com/openai/openai-agents-python/blob/91fb508a8931016efa793b033147d08cd20308cf/src/agents/run_internal/run_steps.py#L164) · [运行循环](https://github.com/openai/openai-agents-python/blob/91fb508a8931016efa793b033147d08cd20308cf/src/agents/run_internal/run_loop.py#L1617) · [交接取消测试](https://github.com/openai/openai-agents-python/blob/91fb508a8931016efa793b033147d08cd20308cf/tests/test_soft_cancel.py#L550)

## 社区 skills 的适用环节

| 角色与问题 | 可借鉴的做法 | 维护者原文 |
| --- | --- | --- |
| 测试员误猜浏览器控件 | 打开实际应用，检查渲染后的 DOM，再选择控件并执行完整操作 | [Anthropic webapp-testing](https://github.com/anthropics/skills/blob/main/skills/webapp-testing/SKILL.md) |
| 作者收到有争议的复核意见 | 核对意见与实际实现，逐项修订并验证，保留异议的证据 | [Superpowers receiving-code-review](https://github.com/obra/superpowers/blob/main/skills/receiving-code-review/SKILL.md) |
| 基金风控复核行业演示 | 按页抽取数字，对齐单位、期间、数据与投研结论。上游输入是 PowerPoint，Studio 的 HTML 还要逐页播放并核查原始公告 | [Anthropic ib-check-deck](https://github.com/anthropics/financial-services/blob/main/plugins/vertical-plugins/financial-analysis/skills/ib-check-deck/SKILL.md) |

## 回归入口

Studio 的 `run_browser` 工具说明采用了上表的控件发现步骤，提示从真实页面选取角色和标签，并按控件类型操作。[Playwright 定位器](https://playwright.dev/python/docs/locators)

运行 `python -m unittest tests.test_studio_native_review tests.test_rao_protocol`。Studio 正常入口的测试让负责人先反复查文件、随后提前报阻塞；断言旧版被退回原作者，新版交给独立评审，最终结果具有新版本签署。协议测试覆盖截断输出、被取代的请求和失败交接的重复执行。
