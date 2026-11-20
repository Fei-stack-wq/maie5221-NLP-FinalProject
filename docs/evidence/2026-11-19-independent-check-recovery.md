# 软件组错误断言的独立复核

2026-11-19 的软件第 1 题暴露了一个交接问题。开发用 `#startInput` 检查预约表单，页面实际使用 `#startSelect`。原脚本及其原样重放都失败。使用实际控件的后续浏览器测试通过，但普通交接仍因原检查失败被反复退回。开发返工在该次线上运行中发生了 66 次这种退回。

## 运行契约

作者提交本轮交接时，如果全部未关闭的失败都有完整、可转交的内联断言和宿主错误回执，而且同一成员在最后一次失败后验证了当前文件版本，宿主可以冻结完整候选并结束该成员的尝试。原任务仍留有失败记录。群聊展示待独立处理的候选版本、原调用和修订调用。普通 QA 在原文件上工作时也能这样交接，可以没有新文件或指定交付路径。

接手者须独立于编写错误检查的角色。针对其他成员已经交付的文件做 QA 时，Studio 也排除原发布者。已经处于修订流程的产品沿用原验收者与原业务标准，同时为每个旧失败追加一项可执行核查。独立成员在隔离副本中看到失败来源与后续实测。宿主仍验证副本完整性、独立尝试的回执、检查对象和用户指定的交付条件。只有对同一冻结版本逐项签署通过，宿主才有权发布。检查来源不完整、候选变化、仅引用作者的回执、没有独立成员或无关文件的 PASS，都不能完成这次核销。

裸端口不能标识项目版本。静态页面可以通过 `file://` 读取冻结文件并对实际控件执行断言。对于经本地服务提供静态入口的应用，下面是一种可归因的复核方式。将地址和文件名替换为本次候选的实际值。

```python
from pathlib import Path

response = page.goto("http://127.0.0.1:8123/booking")
assert response.body() == Path("static/index.html").read_bytes()
assert page.get_by_label("开始时间").input_value() == "09:00"
```

Playwright 实际观察最后一个主框架文档的本地响应。宿主核对响应正文的 SHA-256、冻结文件及独立脚本中对页面的断言。HTTP 压缩和包装路由可以改变传输方式，解码后的文档须能对得上候选字节。只比较正文、只读源码、只访问某端口或在替换页面后再断言，都不足以签署浏览器检查。成员日常回执以临时进程内的一次性校验密钥区分驱动采集的数据与脚本输出。这个机制服务于可复现的应用测试；执行任意本地代码的进程仍受现有操作系统权限约束。

若服务端动态渲染页面，找不到字节一致的 HTML，浏览器仍可执行检查并保留结果。可以在独立副本启动冻结后端，运行项目自带的业务集成测试，将预约、冲突、取消、重启或 HTTP 规则验证为独立的业务标准。浏览器原失败仍须对应可归因的界面证据才能签署；暂时无法绑定时提交 `inconclusive / verification`，把实际测到的范围与缺项写入报告。

## 上游实现

OpenAI Agents SDK 的 `RunState._approval_items_match` 在比较工具调用前先辨别归属。无法复制或区分的身份不能安全匹配；`approve` 把决定传回相应状态。其测试还验证调用者修改取出的中断对象不会改变待处理对象。Studio 采用相同的身份与快照思路，另行绑定候选文件摘要、原失败 scope 与独立成员本次执行的签署。实现未复制上游代码。[源码及异常处理](https://github.com/openai/openai-agents-python/blob/91fb508a8931016efa793b033147d08cd20308cf/src/agents/run_state.py#L1146-L1220) · [快照独立性测试](https://github.com/openai/openai-agents-python/blob/91fb508a8931016efa793b033147d08cd20308cf/tests/test_run_state.py#L1460-L1509)

浏览器数据来自 Microsoft Playwright 提供的 page response 事件与 Response.body()。Studio 只为当前主文档保留摘要，不以控制台文字代替实际响应。[网络事件](https://playwright.dev/python/docs/network#network-events) · [Response.body](https://playwright.dev/python/docs/api/class-response#response-body)

## 本地回归

在仓库根目录运行以下命令可复验首轮选择器纠错、公开版本返工、无文件改动的 QA、错误候选版本和证据，以及本地包装页面的真实浏览器检查。浏览器用例需要 Playwright、Chromium 和可监听的本地端口。

```sh
python -m unittest tests.test_private_candidate_review tests.test_bound_reviewer_probe tests.test_crew_progress tests.test_browser_tool
```
