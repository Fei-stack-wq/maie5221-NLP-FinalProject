# 对私有候选的 QA 检查进行独立裁决

软件第 1 题的第三轮盲测运行在版本 `80cdb95`，运行 ID 为 `studio-20261119-202647-200c1f8680b3`。开发第一次浏览器检查 `call_00_fEiPZ26sHQbU7Quu1mgB8537` 有失败，后续实测产生了不同结果。Studio 冻结了开发私稿，派 QA 对原业务要求和开发的反例各自签署。候选版本为 `c4f24d1506cb6f1e5ff43f64b983b4d40d746292132a61ac3be168e40253e525`。

QA 在尝试 `attempt-4c4633db6f1c4d92b21493d07184a4ef` 有四次实际执行失败的 Python 检查，调用是 `call_00_SN0AjZy8xDZ5Weq2UP9u8611`、`call_00_Q2AkZJ8P7GZnusfobdJT7553`、`call_00_f19zVxWjZ1fXcmtbUKNi5863` 和 `call_00_plrube0qwAYyiqMfeI3b4681`。它在第 23、38 步尝试提交通过判断，分别是 `call_00_wZH8mZxrCqwNdlOCteHh0537` 和 `call_00_klQplwm9rHZxPgHMFL6s3206`。两个调用都收到重复的“当前版本仍有失败检查”。这两个提交均未获宿主认可。

旧代码仅为公开产品配置了本次评审任务身份。私稿 QA 提交时直接触发提升前的失败检查，甚至无法获得失败的 scope、原始调用、可引用的回执与移交条件。公开评审已有的第三人路径也跳过了私稿。四次程序失败均须保留，每一项是不是产品缺陷仍需按被冻结产品的实际行为判断。

## 谁能做什么

1. 原 QA 在当前冻结副本按业务要求检查。每个未关闭的本次探针都须有完整可转交的代码、原错误和回执。提交时若遗漏，会收到对应调用、错误摘录、短回执和可否交同伴的提示。保留失败并明确指出它与哪一项实际业务证据冲突，才能将这项签署连同反例交出。不完整或继承的未处理失败仍阻止提升。
2. 第三人的隔离副本包含原产品和原 QA 产生的核验材料。Studio 校验原作者任务身份、原作者私有检查点、QA 检查点和公共基线；同一角色的其他私稿不会被覆盖。原作者与原 QA 均不可签第三人裁决。能否核销由原失败、业务标准、冻结输入与第三人本次实际观察共同决定。只读无关材料或零退出码不足以核销程序断言。
3. 第三人发现真实产品缺陷时，将原产品发回作者。确认检查方法错误时，争议记为已裁决并附第三人的尝试、逐项结论和证据 ID。第三人无权发布原私稿或 QA 附件，也没有一项额外的文件交付义务。其自身若写错探针，应保留反例并从同一冻结输入独立重测。复核期间，同一范围已由第三人裁决的失败不会再派生下一层裁决。
4. 原 QA 再次接到原任务和原产品版本，独立作出新的产品判断。签署成功且原产品通过发布校验时，才可依原作者身份提升。获准附上的最终评审文档与产品文件仍分别处理。第三人工作期间，公共目录不出现私稿与临时报告；内部运行日志继续保留签署、错误及角色审计记录。

## 上游与回归

OpenAI Agents SDK 的 `RunState._approval_items_match` 优先核对决定的所有者和工具调用身份。`_find_current_approval_item` 向当前待处理集合匹配，并对无法区分的当前调用与嵌套调用抛出异常。其 `test_get_interruptions_returns_detached_item_snapshots` 修改已传出的对象、参数及内层列表后，断言真正待决的对象仍保持原值。Studio 借用这两个设计约束，另外校验文件清单和各层签署权限。实现未复制上游代码。[匹配及异常处理](https://github.com/openai/openai-agents-python/blob/91fb508a8931016efa793b033147d08cd20308cf/src/agents/run_state.py#L1146-L1220) · [独立快照回归](https://github.com/openai/openai-agents-python/blob/91fb508a8931016efa793b033147d08cd20308cf/tests/test_run_state.py#L1460-L1509)

`tests.test_bound_reviewer_probe` 的群聊测试走 `StudioService.start`，使用脚本化模型、真实 Python 子进程和任务仓库。它验证开发先错、QA 再错、漏引失败被驳回、第三人签署仅关闭争议，以及原 QA 重签后才发布。另两例在第三人签署时替换原作者私有文件，或由原 QA 在同一版本重做已裁决的错误断言，检查原稿仍未发布。真实模型盲测的最终表现应以对应运行的验收材料为准。

## 冻结的产品与独立成员提交的检查程序

11 月 20 日运行 `studio-20261120-013306-664d181a18a7` 的 QA 同时接到两个要求，保留开发版本的清单，以及提交新文件 `qa_e2e_check.py`。旧宿主只允许附带文本报告。工具拒绝写入七次，经 shell 在项目根写入又破坏冻结清单，最终没有公开交付。

现在，受委派的检查脚本保存在独立评审工作副本的附件目录。宿主在给成员的任务中提供原名与实际路径的对应关系。成员从项目根运行该路径，读取检查程序自身不能获得产品验证回执。签署通过前，程序必须能安全地作为非空文本附件保存。作者原始快照按原摘要发布，脚本在成员交接中使用自己的只读文件地址与 SHA-256。未委派的私稿仍属于成员；对产品源码的保护、旧失败的独立裁决及真实运行检查继续生效。

此设计参考 Playwright 的 `TestInfo.outputPath` 和 `attach`。上游把测试输出限制在指定目录，另行复制、命名并提供附件。Studio 在成员签署和项目发布之间绑定各自的文件版本。[目录边界与 attach](https://github.com/microsoft/playwright/blob/a018f24a4fc76e2120d3b35d2415f1e5bd4b073e/packages/playwright/src/worker/testInfo.ts) · [输出目录测试](https://github.com/microsoft/playwright/blob/a018f24a4fc76e2120d3b35d2415f1e5bd4b073e/tests/playwright-test/test-output-dir.spec.ts) · [附件回归](https://github.com/microsoft/playwright/blob/a018f24a4fc76e2120d3b35d2415f1e5bd4b073e/tests/playwright-test/reporter-attachment.spec.ts)
