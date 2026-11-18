# 受阻成员的诊断报告

在会议室预订系统第三轮中，QA 被明确委派产出 `QA_REPORT.md`。成员发现两种并发探针本身有问题，又发现残缺预约记录会导致查询返回 HTTP 500。宿主记录了失败检查并阻止发布。负责人拿到的交接摘要只包含失败标签，后续成员根据这些标签进行排查时看不到报告。档案中的原始报告有 18,988 字节。

## 运行行为

- 负责人使用 `delegate_task.deliverable_paths` 指定报告，用户配置的成员验收文件也可声明报告。交接受阻时，宿主只收取成员已经修改的、指名的 `.md`、`.rst`、`.txt` 文档，逐个验证文件和凭据，排除链接、未指定文件及超过 256 KiB 的报告。其他私有文件继续留在原分工目录。
- 诊断附件保存在该轮的只读交接收据中，内容和 SHA-256 绑定。它不会写入负责人的公开工程或本地工作区。群聊仍显示「交接受阻」，用户可点开「成员诊断报告」。成员任务、失败检查、根任务验收继续使用原有状态与规则。
- 负责人和接手成员会看到有来源的报告节选，明确标识为尚未独立核验的成员陈述。需要证据细节时调用 `read_handoff_report(task_id, path, offset)`。`next_offset` 为下一页的 Unicode 字符偏移。宿主只接受本群提供过的任务和报告名称，读取已有收据并复核摘要。负责人读取仍计入原有检查额度。
- 原群接续直接失败的上一轮时，可以读取来源明确的诊断文档。对于旧版档案，宿主查上一轮 TaskStore、委派与未发布回报，仅取指名文件与实际变更清单的交集；还须通过原成员目录和冻结检查点的逐文件一致性校验。新文档副本保存在新轮次的证据区。工作区版本、旧轮消息和历史验收均保留原记录。选用空工作区会跳过这项继承。

## 设计依据

A2A 官方 Python SDK 的 `TaskUpdater.add_artifact` 可给带任务/上下文身份的工件投递更新事件；`TaskUpdater.failed` 独立投递终态并可携带失败消息。官方测试分别验证自定义工件编号、名称、事件类型和失败消息。Studio 采用这两种事实可以同时成立的设计，额外复用现有文件清单、访问范围和 SHA-256 校验，以适配同一台机器上的群组工作区。没有引入上游实现代码或新增运行依赖。[SDK 工件与失败状态实现](https://github.com/a2aproject/a2a-python/blob/bdef0c3c036602cfdc7aedc7119fe81b77820f46/src/a2a/server/tasks/task_updater.py#L108-L164) · [工件事件测试](https://github.com/a2aproject/a2a-python/blob/bdef0c3c036602cfdc7aedc7119fe81b77820f46/tests/server/tasks/test_task_updater.py#L160-L196) · [失败状态含消息的测试](https://github.com/a2aproject/a2a-python/blob/bdef0c3c036602cfdc7aedc7119fe81b77820f46/tests/server/tasks/test_task_updater.py#L404-L437) · [A2A 官方协议](https://a2a-protocol.org/latest/specification/)

## 验证

`python -m unittest tests.test_crew_diagnostics` 从 Studio 入口重现红色断言与一份带结论的报告。负责人和后继成员均能读取首尾节选以外的证据。红色回执、根任务失败、空的公开工程以及真实保存的报告内容都在测试中断言。回归还用旧格式的同群上一轮验证跨轮读取，覆盖越权任务号、未指名文件、凭据、链接及收据篡改。

对 `studio-20261118-042845-70fae89c24f3` 的只读复查使用临时目录保存新轮证据。成员任务 `task-d60b4e44e2154c22a9b2cc5f4ed0ebb0` 的 `QA_REPORT.md` 可分页还原全部字节，同时保留有关无效探针和残缺记录错误的原文。这项复查验证了证据传递；缺陷是否已修复需由新运行的真实独立检查判断。
