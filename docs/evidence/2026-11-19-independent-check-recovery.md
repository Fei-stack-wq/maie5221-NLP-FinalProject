# 软件组错误断言的独立复核

2026-11-19 的软件第 1 题暴露了一个交接问题。开发用 `#startInput` 检查预约表单，页面实际使用 `#startSelect`。原脚本及其原样重放都失败。使用实际控件的后续浏览器测试通过，但普通交接仍因原检查失败被反复退回。开发返工在该次线上运行中发生了 66 次这种退回。

## 运行契约

作者提交本轮交接时，如果全部未关闭的失败都有完整、可转交的内联断言和宿主错误回执，而且同一成员在最后一次失败后验证了当前文件版本，宿主可以冻结完整候选并结束该成员的尝试。原任务仍留有失败记录。群聊展示待独立处理的候选版本、原调用和修订调用。普通 QA 在原文件上工作时也能这样交接，可以没有新文件或指定交付路径。

接手者须独立于编写错误检查的角色。针对其他成员已经交付的文件做 QA 时，Studio 也排除原发布者。已经处于修订流程的产品沿用原验收者与原业务标准，同时为每个旧失败追加一项可执行核查。独立成员在隔离副本中看到失败来源与后续实测。宿主仍验证副本完整性、独立尝试的回执、检查对象和用户指定的交付条件。只有对同一冻结版本逐项签署通过，宿主才有权发布。检查来源不完整、候选变化、仅引用作者的回执、没有独立成员或无关文件的 PASS，都不能完成这次核销。

对本地服务的浏览器核查可以直接使用业务控件。下面的脚本打开本地应用，找到可访问名称为「开始时间」的控件并检查当前值。

```python
page.goto("http://127.0.0.1:8123/booking")
start = page.get_by_label("开始时间")
expect(start).to_have_value("09:00")
```

宿主在 Playwright 确实执行 Locator 断言时取得本地主框架 HTTP 200 响应正文的 SHA-256，将解压后的字节匹配到冻结清单内唯一对应的 HTML。原生 Python `assert` 中实际执行的控件取值也可取证。正常的 `try/finally`、循环和控件别名都可使用。`file://` 入口直接绑定工作区内的相对文件路径与响应摘要。同一候选包含多份相同字节的 HTML 而服务没有可区分的物理路径时，可以用对应文件入口做静态页面核验。不同的 URL 包装和 HTTP 压缩不影响字节比对。

只访问端口、检查 HTTP 200、未调用的辅助函数、其他页签和 `set_content` 生成的测试页面均不会签发该产品的 DOM 断言回执。失败的真实 UI 断言也记录实际页面，正面证据须覆盖它的冻结版本。系统另行保留原失败调用、复验调用及独立角色签署。签署由独立成员对照原业务标准作出，页面摘要只证明断言所见的页面版本。该协议用于诚实编写的应用验收脚本，执行任意本地 Python 的权限边界仍由操作系统提供。

若服务端动态渲染页面，找不到字节一致的 HTML，浏览器仍可执行检查并保留结果。可以在独立副本启动冻结后端，运行项目自带的业务集成测试，将预约、冲突、取消、重启或 HTTP 规则验证为独立的业务标准。浏览器原失败仍须对应可归因的界面证据才能签署；暂时无法绑定时提交 `inconclusive / verification`，把实际测到的范围与缺项写入报告。

## 29f7f1c 真实盲测与原调用重放

软件第 1 题 `studio-20261119-193126-6a6b60526c03` 的产品版本为 `3d78b0dbc69f50731a0f252322a1befc5eb1d2cadd820ea021b50ac02534a19c`。第二轮 QA 的唯一未决项是 `single-command-start`，签署为 `inconclusive/verification`。此处记录的是该次历史失败与修复版对其原脚本的复现；历史运行保持原判。

| 原 QA 步骤 | 原工具调用 | 在隔离副本重放的退出码 | 观察 |
| --- | --- | --- | --- |
| 8 | `call_00_BK3DgOM5imqo7uInwlZV3542` | 1 | 表单能打开，猜测的 `#booking-form` 不存在 |
| 12 | `call_00_DToJrDbPpz1ifhzz9fMi5770` | 0 | 真实 test ID、房间、起止时间及预约人控件可用 |
| 14 | `call_00_kQo3LkE3ENKCFCZVodd81714` | 1 | 通过 `rerun_browser_check` 重放第 8 步的原代码并保留其失败身份 |
| 17 | `call_00_OCqW88rP0hLJtDXTQZr81139` | 0 | 真实后端打印启动地址，页面和四间会议室均得到验证 |

重放从运行数据库的 `action_identity` 取出参数，使用公开产品副本，每次恢复空账本，实际执行 `python3 app.py` 和无头 Chromium。四次收据均匹配 `static/index.html` 的 SHA-256 `e287f772aa4ca5f7059fcebfeb37607938dd3125ae8ae46fe653820f149b4e10`。原脚本无需写响应与文件的字节比较。

此前四处分支影响交接。

1. 原 QA 已经在真实后端运行断言，宿主仍要求脚本手写正文比较并将断言放在顶层。真实脚本里的 `try/finally` 和控件别名使正面回执的 `asserted_inputs` 为空。当前按原生运行时对象、实际执行结果、主文档响应与冻结摘要取证。
2. 第 8、14 步原 stderr 均为 8,131 字节，第一轮的 Python 启动检查 stderr 为 3,961 字节。原观察器因为展示预览截断而放弃内联探针。修复后，Python 和浏览器分别继续使用原来的头尾展示，同时保存至多 32 KiB 的完整运行错误交给同伴。本次第 8 步的真实重放保存了 8,250 字节原错误和原脚本，直到第 17 步也保留原调用 ID。超出保留范围的回执会列出原因、调用 ID 与原输出大小，失败仍然待处理。
3. 正式 QA 对这个 execute 标准引用了真实失败与正面执行回执，没有重复引用源码读取。旧队列仅从 read 回执选文件，回退到负责人待交付的 README.md，而 README.md 不在产品冻结清单里。当前队列也接受签署本项时确实断言过且与原产品 SHA 相同的输入。
4. 在正常 Studio 群聊回归中，第三方成功裁决后，旧发布逻辑会要求负责人后续承诺的 README.md 立即存在。该步骤发布的是 QA 的争议结论，现在仍核查冻结副本、原公开基线、角色验收和第三方独立性。作者私稿提升保留原硬验收，群任务最终关闭仍要求完整交付。对原标准签署 `inconclusive` 的成员在争议关闭后须用同一冻结版本再签，独立裁决本身不把产品标成通过。

## 上游实现

OpenAI Agents SDK 的 `RunState._approval_items_match` 在比较工具调用前先辨别归属。无法复制或区分的身份不能安全匹配；`approve` 把决定传回相应状态。其测试还验证调用者修改取出的中断对象不会改变待处理对象。Studio 采用相同的身份与快照思路，另行绑定候选文件摘要、原失败 scope 与独立成员本次执行的签署。实现未复制上游代码。[源码及异常处理](https://github.com/openai/openai-agents-python/blob/91fb508a8931016efa793b033147d08cd20308cf/src/agents/run_state.py#L1146-L1220) · [快照独立性测试](https://github.com/openai/openai-agents-python/blob/91fb508a8931016efa793b033147d08cd20308cf/tests/test_run_state.py#L1460-L1509)

Microsoft Playwright 的 `Expect.__call__` 按实际对象类型分派，Locator 与 APIResponse 有不同的断言对象。其 `test_to_be_in_viewport_should_have_good_stack` 用实际 Locator 验证失败会抛出 AssertionError 并检查错误内容。Studio 执行原断言，在对应结果处记录当前主文档，保留 Playwright 的原生返回值与错误。实现未复制上游代码。[Expect 类型分派源码](https://github.com/microsoft/playwright-python/blob/main/playwright/sync_api/__init__.py#L115-L191) · [原生断言及错误测试](https://github.com/microsoft/playwright-python/blob/main/tests/sync/test_expect_misc.py) · [Response.body](https://playwright.dev/python/docs/api/class-response#response-body)

## 本地回归

在仓库根目录运行以下命令可复验首轮选择器纠错、公开版本返工、无文件改动的 QA、错误候选版本和证据，以及本地包装页面的真实浏览器检查。浏览器用例需要 Playwright、Chromium 和可监听的本地端口。

```sh
python -m unittest tests.test_private_candidate_review tests.test_bound_reviewer_probe tests.test_crew_progress tests.test_browser_tool tests.test_bound_failure_output
```

## 已签判断与阻塞状态

公开候选的评审者可以签署业务项，同时明确引用仍有争议的测试。当原失败均可转交、工件版本一致且指定检查与附件已齐备，宿主在签署工具返回后就结束这次成员执行，把争议留给独立成员。签署中的原判断和反例都随交接保存。其他等待中的指定检查、正在运行的命令和发生变化的快照仍须先处理。若最终因证据问题阻塞，负责人和用户看到实际签署项数、通过数和不同回执数，已执行的业务检查不会被概括为“没有新证据”。

私有冻结候选的独立评审只能增加指定的正式报告和有工具回执的外部材料。评审者尝试在产品目录另建临时测试，文件工具会在创建前说明如何使用系统临时目录。由应用或命令实际写入的额外文件仍由最终快照校验发现，宿主不会删除用户文件。两条路径分别从普通群聊入口测试。独立任务完成与由宿主交接的实现参考同目录 [OpenAI Agents SDK 的结果消费与回归](2026-11-19-review-coordination.md)。浏览器交接时保留隔离的输入和原始检查过程。pytest 的 [tmp_path](https://docs.pytest.org/en/stable/how-to/tmp_path.html) 同样为单次检查提供独立的临时路径。
