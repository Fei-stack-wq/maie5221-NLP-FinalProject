# 工作室里的团队任务与续聊

在「对话 → 工作室角色」填写角色 ID、名称和职责。保存后，该角色可用于新私聊或多个群。每个群在建群时保存自己的成员和职责快照。已有会话仍可继续使用原来的内置角色。

## 在一个群里做两道题

1. 新建群聊，选择成员和负责人，选择「由负责人分工」，发送第一道任务。负责人通过委派工具唤醒相应成员，成员在自己的项目副本上执行、提交文件和公开答复。群里显示由谁派给谁、交付状态以及是否发布文件。
2. 展开选项，可以给整体交付填写文件路径和检查规则，也可以按成员 ID 指定子任务检查。单独运行的任务图按规划器分配的子任务 ID 配置。只读提问由指定成员处理。老师可以在任务详情直接回答执行中出现的问题，刷新网页后仍能看到待答项，同一服务进程上的原执行尝试收到答复后继续。取消会结束等待。关闭服务会终止当前等待；持久日志可读，进程重启后的自动执行检查点恢复仍待开发。
3. 在原群继续发送第二题。「本轮选项与预算 → 项目文件」可以沿用最近的已发布版本，或为独立选题使用空工作区。两者都保留本群公开对话。角色可据新的要求修改方案。点击「导出完整会话」下载按轮编排的完整公开答复、人工交流、成员分工与回报、机器验收报告、交付文件及 SHA-256 清单。旧运行未保存的答复会标明缺失。
4. 在产物页选中 HTML 并点击「打开交互预览」，可以实际操作交付的网页或逐页播放幻灯片。预览支持内嵌及相对引用的本地 JS/CSS，运行在独立的受限页面中。需要联网服务、存储或后端的项目可以下载后按其说明运行。在「验收 → 人工评审」记录操作步骤和结论，历史人工意见和机器判定分别保留。

例如，会议室团队可以先实际新增、尝试冲突预订、取消，再在原群要求负责人根据这些记录生成日期范围仪表盘。通过文件检查可以验证约定的输出，通过人工操作可以验证按钮和图表。基金或新闻组可以在第二题保留原群，并使用空工作区开展新研究。研究与数据库交接的支持范围见 [检索与项目数据](research-and-project-data.md)。

## 交付检查

单个回合可用一个 JSON 数组描述确定性条件。路径相对于被检查的项目工作区。每条规则必须通过。金额、频率和时长的期望值须来自已知输入，交付内容及规则都包含在评审包中。

```json
[
  {"kind": "contains", "path": "usage.html", "text": "使用时长"},
  {"kind": "json_equals", "path": "totals.json", "pointer": "/rooms/A/hours", "expected": 2}
]
```

还支持用 `{"kind":"sha256","path":"bookings.csv","value":"<64 位小写十六进制摘要>"}` 核对相同的原始记录。给某一成员单独规定交付要求时使用例如 `{"qa":{"files":["checks.md"],"checks":[]}}`。用户未配置检查的交付保留「未验证」状态。文章论据是否可靠、配置是否适合客户，仍应根据来源和公开对话评审。

## 超出预算后继续项目

累计预算包括每次模型请求实际计入的输入和输出，输入包括重发的历史。当前请求会根据本轮上下文空间和剩余任务预算裁剪较早的工具交互，保留用户要求、人工答复和近期结果。DeepSeek 请求也会根据剩余空间调整这一次调用的输出上限。预计额度不足时在发送前停止，诊断页显示可用的实际输入与输出数据。

当一次编辑失败而文件已经写入，在该次运行的「项目对话 → 本轮选项与预算 → 项目文件」选择「接着本轮未完成文件」，填写要继续的目标并发送。系统在新的运行目录载入这些文件。原记录和上一次发布的版本都可查询。新的尝试有单独预算和验收；原来的 Python 进程、浏览器状态和已执行的工具调用不会被自动重放。

## 本机接口

| 操作 | 接口 |
| --- | --- |
| 保存可复用角色 | POST /api/profiles，字段为 id、name、prompt |
| 群内自动分工 | POST /api/runs，mode 为 team，conversation.kind 为 group，recipient 为负责人，首次请求附所选自定义 profiles |
| 原群继续 | POST /api/runs，conversation.parent_run_id 指向最新一轮。recover_failed=true 时可用 recover_task_id 指定该轮成员草稿。inherit_files=false 用于另起空项目 |
| 回答执行中的问题 | POST /api/runs/{run}/questions/{request}，字段为 answer |
| 记录人工评审 | POST /api/runs/{run}/review，字段为 verdict 和 note |
| 交互预览 | GET /api/runs/{run}/preview?path={已生成的 HTML 路径} |
| 导出整群 | GET /api/runs/{run}/room.zip |

上述写操作沿用 Studio 的同源与 CSRF 校验。软件、新闻、基金测试使用通用的角色、委派、文件和检索能力。脚本模型回归检验平台行为，六道真实任务的最终质量与完成率需要独立测评。

## 负责人如何结束一轮

负责人先选择与目标相关的成员，通过委派工具的 `remaining_roles` 声明后续分工。负责人首次分工或处理受阻交接时，可在公共项目中只读查找、搜索和查看文件。下一次路由会携带最近查到的内容与行号。系统按实际用量限制同一公共版本上的查阅开销，到达额度后进入委派，具体人选、任务和验收由负责人决定。每位成员在独立副本工作，回执包含分工目标、实际修改的路径与内容摘要、观察到的检查、剩余问题和实际用量。发布或受阻都会进入公共群记录。文件发布和用户配置的独立验收各自记录结论。

成员可以按不同位置阅读已有项目、搜索入口、修改源码、运行测试并准备交付说明。缺少文档时会提示补齐文件，成员在这一阶段继续拥有完整的工作工具。同一版本连续通过相同检查，或多轮只重复查看已有证据，会触发成员的交接阶段。文件有实质变动后，先前的测试结果需要重验。交接阶段只允许给出最终答复，也可以明确报告阻塞。未解决的已知失败会阻止发布。

分配成员前先计算负责人汇总和后续路由所需的轮数、工具次数、时间与 token，再在待办成员之间分配可用额度。预算按预计的收尾输入和输出留存。成员只接收当前分工、用户已确认的决定、可见文件清单和近期成功交付的摘要。长任务压缩工作历史时，保留最近读取的位置和文件版本、实际改动及测试回执。服务端产生的 `assignment.reserved`、`assignment.allocation` 和 `member_report.usage` 可与模型实际用量核对。负责人最后一次请求只携带必要目标、决定和交接摘要，当轮可用工具为空。用户配置的最终验收仍由运行时独立执行。连续达到同一阻塞时，以失败回执结束，保留文件和证据供续聊。

## 使用本机工作区

启动私聊、群聊或单 Agent 离线演示时可以选取本机项目文件夹，也可以填写绝对路径。普通续聊沿用同一工作区，并在开始前重新读取其中的文件，因此能接上用户在编辑器里保存的改动。只读提问会读取副本。群成员在独立副本工作。每次成员成功交接后，宿主立即把该次公开变更写入所选目录。群聊里的成员附件指向该次发布的固定版本。单 Agent 在本轮正常交付时写回。整轮验收和各成员文件的写回各自记录，后续失败时可以在本机继续查看此前已发布的文件。写回遇到本机并发修改时会显示冲突，回复仍保留本轮草稿。

当前工作区支持 UTF-8 文本、Markdown、Python、Shell、HTML、CSS、JavaScript/TypeScript、Go、C/C++、Rust、Java、Make/CMake 和 Dockerfile、Go 模块与校验文件（.mod、.sum）、UTF-8 依赖锁文件（.lock）、JSON/JSONL、SQL、CSV/TSV、TOML、YAML、日志、静态 PNG 截图和已关闭的 SQLite 数据库。每个文件上限 8 MiB，项目总量 20 MiB。快照会跳过隐藏目录、node_modules、venv 和 __pycache__。编译后的前端 JS/CSS 可随应用一同交付。其他格式留在本机。系统在发布前检查修改目标的原始内容和目录身份，通过子目录文件描述符执行原子文件替换。独立进程崩溃期间的多文件提交仍需通过文件版本核对恢复。

## 执行成员的编码工具

| 工作 | 工具 | 可复核的输出 |
| --- | --- | --- |
| 定位实现和测试 | `find_files`、`list_dir`、`search_files` | 项目相对路径、匹配行号、相邻上下文、下一页位置 |
| 阅读长文件 | `read_lines`、`read_file` | 指定行或字符区间、文件摘要；默认可一次读取 32768 字符，支持连续分页 |
| 修改项目 | `edit_file`、`write_file`、`append_file`、`delete_file` | 精确替换、可选版本检查、写入摘要；单文件删除工具沿用人工确认 |
| 构建、调试和测试 | `run_shell`、`run_python` | 可在项目子目录调用已安装的解释器、构建工具和测试程序，返回退出状态与输出 |
| 验证网页交互 | `run_browser` | 真实 Chromium 下操作页面并执行 Playwright 断言，返回退出码和浏览器测试输出 |
| 查阅外部资料 | `search_sources`、`http_get` | 保存来源和摘录供其他成员核对 |

只读问答可使用表中前两组工具。执行成员在自己的项目副本运行命令，交接时发布文件。命令使用本机账户权限；工作目录和环境变量管理属于进程级软隔离。工作室在期间记录工具调用、可用工具清单、文件变更与检查结果，结束后仍能区别模型报告和独立验收。

## 设计来源与致谢

- **Pillow 贡献者（MIT-CMU）**。工作室使用 Pillow 校验 PNG 与像素范围，再将原始截图存入成员快照；群消息可以直接打开截图。[Pillow](https://github.com/python-pillow/Pillow)
- **Andrew Gallant 与 ripgrep 贡献者（MIT / Unlicense）**。Studio 的项目搜索和文件查找直接调用 ripgrep，复用其正则引擎、忽略规则和二进制文件处理，并把稳定排序、分页结果和行号接入模型工具回执。[ripgrep](https://github.com/BurntSushi/ripgrep)
- **Mario Zechner 与 Pi 贡献者（MIT）**。工作目录绑定到会话，模型每次调用都接收当前可用的工具集。Studio 把这两个设计用于本机项目会话和交接阶段；本仓库继续用 Python 执行和保存测试结果。[Pi 编码工具](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/tools/index.ts) · [Pi SDK](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sdk.md) · [许可证](https://github.com/earendil-works/pi/blob/main/LICENSE)
- **DeepSeek Harness Team，dsh-superagent-prototype（MIT）**。我们采用路由者和执行者使用不同工具、独立执行目录、通过报告向原会话交付的设计。群回执、阶段切换和预算分配在本仓库实现。[执行模型](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/main/docs/003-execution.md) · [许可证](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/main/LICENSE)
- **DeepSeek，DeepSeek Harness（MIT）**。Web 界面显式选择工作区的流程参考了该项目的使用指南。[使用指南](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/index.md) · [许可证](https://github.com/deepseek-ai/deepseek-harness/blob/master/LICENSE)
- **psutil 贡献者（BSD-3-Clause）**。工作室直接依赖 psutil 的进程身份、环境与退出等待接口。群成员可在多步验证中启动本机测试服务。交接或失败时，宿主检查进程的私有工作目录和本次分工的随机标记，只回收两者均匹配的子进程。用户自己从交付目录启动的应用独立运行。[项目与文档](https://github.com/giampaolo/psutil) · [许可证](https://github.com/giampaolo/psutil/blob/master/LICENSE)

### 在成员工作区运行浏览器测试

macOS 和 Linux 的编码成员可以调用 `run_browser(code, cwd=".", timeout=...)`。Studio 在项目的 Python 环境安装 `requirements.txt` 后可使用本机 Chrome。安装独立 Chromium 可运行 `python -m playwright install chromium`。Linux 主机若需系统库，可运行 `python -m playwright install --with-deps chromium`。[Playwright 浏览器安装说明](https://playwright.dev/python/docs/browsers)

每次调用使用独立的临时浏览器配置，脚本从成员工作目录运行一次。已创建的同步 API 对象为 `playwright`、`browser`、`context`、`page` 和 `expect`。对交付网页可从本地测试服务进入预约表单，例如将以下代码作为 `code` 传入。脚本可导入项目中的测试辅助模块。对静态页面，可先 `from pathlib import Path`，然后调用 `page.goto(Path("index.html").resolve().as_uri())`。调用使用编码工具的总时限、有界输出和进程回收。断言失败的回执保留真实非零退出码与错误输出。需要截图时可在脚本里调用 `page.screenshot(path="browser-check.png")`。

```python
page.goto("http://127.0.0.1:8000")
page.get_by_label("Room").select_option("A101")
page.get_by_label("Guest").fill("Ada")
page.get_by_role("button", name="Reserve").click()
expect(page.get_by_text("Reserved for Ada")).to_be_visible()
print("PASS: submitted the booking through the visible form")
```

浏览器操作和断言直接使用 Microsoft 的开源 Playwright for Python（Apache-2.0）。[同步 API 文档](https://playwright.dev/python/docs/library) · [源代码与许可证](https://github.com/microsoft/playwright-python)

## 已有草稿的返工

负责人可以在同一次委派中声明成员负责的 `deliverable_paths`。分工已有测试证据但还缺指定的文本产物时，成员会看到缺失路径、最近的检查结果，并保留编辑源码和复测的工具。系统保存上一次未发布的草稿及其公共版本，恢复时会把实际检查、缺失路径和工作目录一并交还。对同一组缺口反复改写任务描述，仍计入同一次返工；为其他已选成员保留的执行额度会单独计算。日志可区分成员间发布、草稿续修、向本机工作区写回和最后一次独立验收。

最近一轮群组编辑失败后，打开「项目对话 → 本轮选项与预算 → 项目文件」。每个可接续的「成员草稿」显示成员、委派目标和任务编号的末八位。选定后发送，新轮会基于该任务保存的完整文件执行，验收仍按新轮配置运行。聊天记录保留 `recovery_source` 的成员和任务编号。

客户端读取 `GET /api/runs/<群头运行编号>` 的 `conversation.recovery_candidates`，每项含 `task_id`、`role` 和 `objective`。发送时，`conversation` 传入最新 `parent_run_id`、`intent: "edit"`、回复角色、`recover_failed: true` 和选中的 `recover_task_id`。草稿可以交给群中其他角色继续。无需提供本地路径。

宿主把委派和成员回报与直接父任务的 TaskStore 执行上下文核对，在受控目录复验文件，并逐文件比对原任务与检查点的 SHA-256。新的公开交接会淘汰之前的候选。用户已编辑绑定工作区时返回 `workspace_conflict`。群头前移时返回 `stale_head`，跨群或无效的成员任务返回 `invalid_recovery_checkpoint`。草稿不会直接发布到用户文件夹。

保留旧客户端的「接着本轮未完成文件」操作。有公开交接时载入公开项目。负责人尚无文件、只有一名成员有合格草稿时可自动接续该成员最新检查点；多名成员有合格草稿且该轮没有公开交接时，返回 `recovery_selection_required` 并保留原群头。只读问答与默认续聊沿用已有项目。

选择机制参考 LangGraph 的 SQLite 检查点，每个显式版本按线程、命名空间和检查点编号联合查找。LangGraph 的搜索测试覆盖多线程与命名空间过滤。Studio 使用已有的直接父轮、角色与持久任务身份，额外检查本机目录版本和快照完整性。实现未引入上游代码或依赖。[LangGraph SQLite 实现](https://github.com/langchain-ai/langgraph/blob/84ced603d3a201ab0f9379936bb35868e243f3f3/libs/checkpoint-sqlite/langgraph/checkpoint/sqlite/__init__.py#L244-L268) · [上游搜索测试](https://github.com/langchain-ai/langgraph/blob/84ced603d3a201ab0f9379936bb35868e243f3f3/libs/checkpoint-sqlite/tests/test_sqlite.py#L78-L119) · [LangGraph 官方恢复文档](https://docs.langchain.com/oss/python/langgraph/use-time-travel)

### 按检查身份核销已修复的失败

在 2026-11-18 的软件组真题中，开发者改正了重复取消与取消不存在的预约所返回的 HTTP 状态。第 13 轮和第 21 轮均执行同一个 `python3 selftest.py`。两次使用了不同的临时日志、去色和检索命令。旧版本将整条 shell 命令纳入用例身份，导致两项新 PASS 与旧 FAIL 同时留在交接清单。

宿主借鉴 pytest 按稳定 node ID 更新 `lastfailed` 的做法。对于可识别的自定义逐项输出，它将工作目录、实际运行的脚本及参数与完整用例标题组合成检查身份。相同运行器可以改变日志名、使用文本筛选或去掉显示颜色。只看到一个通过项时，只核销该项。读取历史日志、混合执行多个脚本、可能改写结果的管道、不同的工作目录或运行参数都不能借用原检查身份。复杂 shell 仍使用原命令匹配。完整业务验收由独立验收器执行。[pytest 8.3.5 的状态更新](https://github.com/pytest-dev/pytest/blob/b55ab2aabb68c0ce94c3903139b062d0c2790152/src/_pytest/cacheprovider.py#L354-L360) · [不同调用与子集复验用例](https://github.com/pytest-dev/pytest/blob/b55ab2aabb68c0ce94c3903139b062d0c2790152/testing/test_cacheprovider.py#L551-L608) · [本项目回归](../tests/test_crew_progress.py)

### 开发成员修订自己的探索用例

会议室预订系统的开发成员曾将三个预约时段的总长写成 180 分钟。实际建立的三个时段分别为 45、60、45 分钟，合计 150 分钟。成员在自己的脚本里改正该数值、剩余分钟和两个时间窗，完整重跑得到 58 个 PASS。由于四个旧 FAIL 的标题包含旧预期或实际值，调度器曾一直阻止移交。

逐项检查的身份包含工作目录、运行器、参数和脚本内容摘要，同名旧失败保留在原版本。对于成员在本次分工通过写文件工具新建的 Python 脚本，宿主可将较早版本的失败登记为“开发自测已替代，待独立验收”。要求两次直接执行同一无筛选脚本，取得真实的完整退出状态和未截断的输出，修订前后实际输出的用例顺序、数量和来源断言位置均相符。宿主核对两版的断言数量、检查表达式形状以及报告函数和退出逻辑；测试数据及预期字面值可修订。业务含义由独立验收判定。删除断言、换成恒真条件、改写报告函数或仅打印 PASS 均保留原阻塞。现有项目脚本及用户提供的脚本按原来的版本复验。宿主认可的修订再次变化时，原阻塞会重新打开。

成员报告中的 `superseded_member_checks` 记录脚本路径、两个 SHA-256、旧失败文本、当前标题、观察到的用例数、两次退出码与待独立验收的处置。最终验收仍执行用户设置的条件和负责验收者的独立检查。这个设计借鉴 pytest 的逐项结果与仅更新已执行子集的行为，也采用 GitHub Checks 将一次结论绑定到特定代码修订的机制。自定义脚本没有 pytest 的 node ID，宿主在可识别的脚本上结合语法结构和实际输出核对身份。[pytest 的逐项状态更新](https://github.com/pytest-dev/pytest/blob/b55ab2aabb68c0ce94c3903139b062d0c2790152/src/_pytest/cacheprovider.py#L354-L360) · [pytest 子集复验测试](https://github.com/pytest-dev/pytest/blob/b55ab2aabb68c0ce94c3903139b062d0c2790152/testing/test_cacheprovider.py#L551-L608) · [GitHub Checks 的修订与结论](https://docs.github.com/en/rest/checks/runs#create-a-check-run) · [本项目回归](../tests/test_crew_progress.py)

成员修订自己新增的探索性检查时，原失败随两次脚本的摘要值进入交接与 trace。获得团队内部交接后，负责人须指派另一个角色根据用户要求核对这些断言。复核者收到具体改动，在相同测试文件版本上实际取得通过信号并于公开交接中指明所核对的任务。只有完成复核的任务才从待办中移除。用户指定的验收及外部逐题验收仍分别保留结论。[复核边界测试](../tests/test_handoff_review.py)

### QA 脚本的逐项结果与汇总

`pytest.TestReport` 将用例身份、执行阶段和结果单独保存，捕获的标准输出属于诊断信息。TAP 同样区分逐项结果与说明。Studio 接受普通脚本的文本输出，因此只识别其中可核对的简易格式。当脚本给出 `PASS n/n, FAIL 0` 且 n 大于零时，紧随其后的 `FAIL details: []`、`FAIL 明细：无` 等空清单属于汇总。同一工具把这段清单再次显示时可保留原始日志；空清单不会生成用例。总数行自身不会授予通过。实际逐项 FAIL、非零退出、相互矛盾的总数或缺失计数仍保留原来可见的失败证据。[pytest 8.3.5 的 TestReport](https://github.com/pytest-dev/pytest/blob/b55ab2aabb68c0ce94c3903139b062d0c2790152/src/_pytest/reports.py) · [TAP 13 的结果与诊断](https://testanything.org/tap-version-13-specification.html)

QA 重新编写脚本时可沿用 `C4` 等编号。两个不同脚本中的编号和标题即使完全相同，也无法建立可靠的用例对应关系。新脚本记录自己的执行结果，原脚本的失败继续留在交接清单。负责人的诊断可以分别引用两份证据。[回归测试](../tests/test_crew_progress.py)

### 执行结果、诊断输出与待复验的候选版本

Shell 回执记录实际执行的命令、退出码、超时和输出裁剪；Python 包装程序打印的文字只证明该包装程序输出了这些文字。成员进度只把可归属的用例或断言失败计入硬失败。诊断脚本自己加工的输出、无法归属的混合命令与中断检查保留为观察。成员报告和检查点最多公布八条诊断摘要，每条包括工具、步骤、原因和实际退出码。回执不会复制任意命令内容或参数值到其他成员的提示中。后台任务只在宿主返回终态时记录执行结果。

当已知套件中断或返回矛盾结果时，成员可在原工作目录直接重跑同一命令。完整的当前版本回执可解除待复验项。输出缓冲已裁剪时，宿主仍可记录受识别的测试进程已完整退出；这种回执不提供缺失的逐项结果，也不能核销已有断言失败。成员在当前候选版本上有其他完成的检查时可以交接工件。未确认的范围随工件送到另一成员。负责人收尾前，独立成员须对同一参数范围实际复验并在交接中说明。改变候选文件后需要复核新版本。用户绑定的验收条件仍由独立验收器执行。

在 booking-04 中，20 秒系统超时产生了第一个旧阻塞。后来的 Python 包装程序在套件退出 0 后打印的 `FAIL lines: none` 产生了第二个。直接重跑套件时，“截断的 JSON”是用例名称，通过宿主的 `truncated` 字段可以区分该名称与输出裁剪。普通测试文本不能替代该字段。测试在独立项目副本上完整运行，实际退出 0，返回 6,083 字节标准输出。宿主记录完整进程的状态和裁剪情况；套件自身报告 132 项通过。

Pi 的 Bash 工具及其超时回归分别保存进程退出、超时原因与截取输出。pytest 的 TestReport 将用例身份、结果与捕获的标准输出分开，原生退出码还能区分断言失败、用户中断、命令用法错误、缺少测试等情况。本项目复用这两个边界，并用工件摘要和执行范围约束跨成员复验。[Pi Bash 固定版本](https://github.com/earendil-works/pi/blob/f6d0c2f9b6ffbcb00c5da52e7e88570539c78bde/packages/coding-agent/src/core/tools/bash.ts#L388-L437) · [Pi 进程已退出而管道超时的回归](https://github.com/earendil-works/pi/blob/f6d0c2f9b6ffbcb00c5da52e7e88570539c78bde/packages/coding-agent/test/suite/regressions/11613-bash-timeout-after-shell-exit.test.ts#L16-L48) · [pytest TestReport](https://github.com/pytest-dev/pytest/blob/b55ab2aabb68c0ce94c3903139b062d0c2790152/src/_pytest/reports.py) · [pytest 退出码](https://docs.pytest.org/en/stable/reference/exit-codes.html)

自定义检查可以静默结束，也可以自行选择语言与汇总格式。对于使用明确命令直接执行的脚本，宿主记录所运行的范围、脚本摘要、实际工具回执 ID 和退出码。相同范围在当前候选版本上退出 0，可让成员交接给独立验收者。该执行本身不会产生逐项通过结论，原来的待复验项仍随交接保留。验收者需要在相同工件版本上亲自重跑该命令，另行取得正面的断言证据并提交明确结论。候选文件变化会清除此前的执行回执。已观察的断言失败和用户配置的验收仍按各自规则处理。此处沿用 Pi 的进程回执接口，额外把脚本退出与独立验收分开记录。[Pi 执行与错误处理](https://github.com/earendil-works/pi/blob/f6d0c2f9b6ffbcb00c5da52e7e88570539c78bde/packages/coding-agent/src/core/tools/bash.ts#L388-L437)

### 同群独立成员的结构化验收与返工

负责人把已交付的候选交给群内另一位成员时，可在 `delegate_task` 中提交 `review`。候选可用作者的任务 ID 或该角色最近一次公开交付定位。每条标准包含稳定 ID、描述、是否必需以及取证方式。检查文字内容、合同或版面可选 `inspect`；核对程序行为或可计算结果选 `execute`。宿主绑定原作者任务、完整候选文件的 SHA-256 清单及其版本摘要。普通讨论和仅需交付的委派维持原有流程。

```json
{
  "assignee": "qa",
  "objective": "独立验收这个版本",
  "review": {
    "candidate": "dev",
    "criteria": [
      {"id":"no-overlap","description":"同一会议室时间重叠时须拒绝第二次预约。","required":true,"method":"execute"},
      {"id":"start-command","description":"README 明确给出本地启动命令。","required":true,"method":"inspect"}
    ]
  }
}
```

验收者通过 `submit_review` 提交精确的候选版本和逐项判断。每项须引用当前验收尝试实际产生的读取、检查或诊断回执。宿主在工具提示中列出可引用的 ID。文件证据记录路径、摘要及实际读取位置。执行证据记录真实工具调用及检查范围。通过执行型标准须引用正面执行证据；因程序行为要求返工须引用失败检查。审查过程中改动候选原件会使该结论失效。报告保留基于 M0 `CheckResult` 与 `EvidenceRef` 的结构化判断，以及有待验证的范围。

| 判断 | 宿主的下一步 |
| --- | --- |
| 所有必需项均为 `passed` | 记录候选版本的独立验收。用户的硬性验收仍按原配置执行。 |
| `changes_requested`，原因 `product` | 原作者收到相同目标、稳定标准和操作建议。修复产生新版本后由独立验收者复验。 |
| `inconclusive` 或 `changes_requested`，原因 `verification` | 验收者改进检查方法并在同一候选上复验。 |
| `inconclusive`，原因 `business` | 负责人经现有 `ask_human` 向用户询问缺失业务决定；收到群内真实回答后继续。 |

报告位于群事件 `member_report.handoff.native_review`。其中 `target` 跨修订不变，`candidate_version` 绑定本轮文件，`assessment` 记录验收者、尝试、逐项理由、证据和需执行的修复。重复同版、同缺口、同证据会显式停止。原先通过的候选被后续交付改动时，旧结论保留在历史事件，新版本重新进入验收。子候选的独立结论与群目标的最终验收分别展示。

这一工作流借鉴 LangGraph evaluator–optimizer 的结构化反馈和条件路由，以及 A2A 区分任务、工件和等待用户输入的状态模型。实现沿用 Studio 的角色、任务记录、工具回执、预算和现有群运行器。[LangGraph evaluator–optimizer 示例](https://docs.langchain.com/oss/python/langgraph/workflows-agents#evaluator-optimizer) · [条件路由实现](https://github.com/langchain-ai/langgraph/blob/84ced603d3a201ab0f9379936bb35868e243f3f3/libs/langgraph/langgraph/graph/state.py#L982) · [条件执行回归](https://github.com/langchain-ai/langgraph/blob/84ced603d3a201ab0f9379936bb35868e243f3f3/libs/langgraph/tests/test_pregel.py#L2924-L2947) · [A2A 任务状态与工件](https://github.com/a2aproject/A2A/blob/5f931b8a25fe66789f987ec4a55fc16ee9fe41ee/specification/a2a.proto#L167-L290) · [本项目普通群入口回归](../tests/test_studio_native_review.py)


同一成员恢复自己的完整草稿后，宿主比较先前与当前可归属的失败检查 ID。实际关闭原失败项可继续由该成员修复。若所有原失败仍未关闭，下一次请求继续修这个草稿就转入冻结候选的独立验收。组长能按角色职责指定评审者；组长再次点名作者时，宿主优先选择已有独立评审记录的已计划成员，再按原计划选择其他可用成员。原分工目标、验收期望、候选摘要和本次改派均写入回执。

针对成员临时编写的内联断言，宿主在能够完整提供探针代码、原失败回执与错误节选时，为每条探针绑定一个必需的独立执行标准。评审者需在冻结文件上实际运行断言，说明原失败与复验结果。原作者的失败仍可查阅。未能完整提供的探针、项目中的测试文件和用户配置的硬性检查继续走原验收规则。实现沿用 LangGraph 的按状态选择下一节点与独立评估的工作流，用未关闭的检查身份作为转交条件。[LangGraph 条件边实现](https://github.com/langchain-ai/langgraph/blob/84ced603d3a201ab0f9379936bb35868e243f3f3/libs/langgraph/langgraph/graph/state.py#L982-L1013) · [条件路由测试](https://github.com/langchain-ai/langgraph/blob/84ced603d3a201ab0f9379936bb35868e243f3f3/libs/langgraph/tests/test_pregel.py#L2924-L2948) · [独立评估工作流](https://docs.langchain.com/oss/python/langgraph/workflows-agents#evaluator-optimizer)

### 负责人结案时的独立验收回执

负责人收尾时，宿主检查公开工作区最新文件摘要与独立评审绑定的候选版本。用户列出的交付路径和结构化验收路径全部由已通过的评审覆盖，且成员、命令、业务问题和交付检查均已收束时，收尾输入会先列出评审人、候选版本、必需标准与文件 SHA-256。成员无额外自检标准时显示的 `unverified` 只描述该成员分工。相同操作后来成功时，早先的委派拒绝保留在轨迹，退出当前回合的纠正反馈。

若负责人依然回复 `[BLOCKED]`，在既有预算内给予一次纯文字核对机会，并在 `reviewed_delivery_blocked` 事件保存依据及处置结果。模型指出的独立验收以外的阻塞仍可成立，二次报告阻塞将原样结束任务。缺文件、显式验收未过、待答业务问题、未出版或已改变的候选都无法触发这种核对。最终交付继续经过 Studio 原有工作区和验收流程。

此机制参考 Pydantic AI 将输出验证错误交还模型、在重试上限后保留失败的处理。本项目自行实现了基于同群发布版本和用户交付文件的资格判定，并使用原有预算和审计轨迹。[Pydantic AI 输出验证与反馈](https://github.com/pydantic/pydantic-ai/blob/ef740deb08789941192f093436dd1a1c31ba4532/pydantic_ai_slim/pydantic_ai/_output.py#L222) · [Pydantic AI 重试上限测试](https://github.com/pydantic/pydantic-ai/blob/ef740deb08789941192f093436dd1a1c31ba4532/tests/test_agent.py#L14684)

成稿来源核查复用了现有 `ReviewSession`。每份指定交付的路径及 SHA-256 须出现在当轮独立评审者提交的必需 source 结论中，并与当轮亲自打开的原始来源一起引用。稿件版本变化即重新进入评审。以校规与论文为例，评审人需对照正式规定、建议、反证、研究样本和受限的适用范围。路径及哈希绑定借鉴 in-toto 对跨步骤材料与产物的匹配；本系统使用 Studio 宿主保存的文件读取及来源回执来识别评审角色，没有使用上游的外部签名基础设施。[in-toto 工件匹配实现](https://github.com/in-toto/in-toto/blob/e352b43ad7cb8915d84c36d791aa61346152a0a3/in_toto/verifylib.py#L645) · [上游匹配测试](https://github.com/in-toto/in-toto/blob/e352b43ad7cb8915d84c36d791aa61346152a0a3/tests/test_verifylib.py#L549)

在网页新建群聊时打开「选项」，或在群内继续对话时打开「本轮选项与预算」。在「需要独立核查来源的文件」中填写最终稿相对路径。选择「由负责人分工」，群内至少需要负责人、作者和另一位核查成员。
