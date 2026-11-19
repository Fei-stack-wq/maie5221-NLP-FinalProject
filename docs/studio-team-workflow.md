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

成员在当前分工执行的浏览器断言失败时，工作记录中的 `replayable_browser_call_ids` 给出仍待核实的原工具调用。修复后，成员可显式调用 `rerun_browser_check` 并传入原调用 ID。宿主在同一成员工作区取回原脚本、相对目录和超时参数，经过原有工具权限、截止时间和进程回收，再记录当前真实退出状态。不同脚本通过后，原检查继续待验。宿主为每个仍失败的断言身份保留一个原调用，同一断言反复失败仍沿用首个回执。通过后释放对应的复验入口，其他待核查项继续保留。交接时每个未验证的调用可获得一次定向复验机会。原检查仍失败时，成员须如实交接，由已有独立核查流程判断下一步。执行历史保留原失败和复验调用之间的关联。

Playwright 的重试调度保留失败的测试对象，对可重试测试检查剩余次数并另行记录实际结果。Studio 借鉴这个身份和追踪方式，复验由成员主动发起，范围限定为当次工作区的浏览器调用。[Playwright 调度实现](https://github.com/microsoft/playwright/blob/54c711571a37de525377e6f3d3608c3e029b1829/packages/playwright/src/runner/dispatcher.ts#L502-L544) · [两次真实结果的重试测试](https://github.com/microsoft/playwright/blob/54c711571a37de525377e6f3d3608c3e029b1829/tests/playwright-test/retry.spec.ts#L19-L40)

### 开发成员修订自己的探索用例

会议室预订系统的开发成员曾将三个预约时段的总长写成 180 分钟。实际建立的三个时段分别为 45、60、45 分钟，合计 150 分钟。成员在自己的脚本里改正该数值、剩余分钟和两个时间窗，完整重跑得到 58 个 PASS。由于四个旧 FAIL 的标题包含旧预期或实际值，调度器曾一直阻止移交。

逐项检查的身份包含工作目录、运行器、参数和脚本内容摘要，同名旧失败保留在原版本。对于成员在本次分工通过写文件工具新建的 Python 脚本，宿主可将较早版本的失败登记为“开发自测已替代，待独立验收”。要求两次直接执行同一无筛选脚本，取得真实的完整退出状态和未截断的输出，修订前后实际输出的用例顺序、数量和来源断言位置均相符。宿主核对两版的断言数量、检查表达式形状以及报告函数和退出逻辑；测试数据及预期字面值可修订。业务含义由独立验收判定。删除断言、换成恒真条件、改写报告函数或仅打印 PASS 均保留原阻塞。现有项目脚本及用户提供的脚本按原来的版本复验。宿主认可的修订再次变化时，原阻塞会重新打开。

成员报告中的 `superseded_member_checks` 记录脚本路径、两个 SHA-256、旧失败文本、当前标题、观察到的用例数、两次退出码与待独立验收的处置。最终验收仍执行用户设置的条件和负责验收者的独立检查。这个设计借鉴 pytest 的逐项结果与仅更新已执行子集的行为，也采用 GitHub Checks 将一次结论绑定到特定代码修订的机制。自定义脚本没有 pytest 的 node ID，宿主在可识别的脚本上结合语法结构和实际输出核对身份。[pytest 的逐项状态更新](https://github.com/pytest-dev/pytest/blob/b55ab2aabb68c0ce94c3903139b062d0c2790152/src/_pytest/cacheprovider.py#L354-L360) · [pytest 子集复验测试](https://github.com/pytest-dev/pytest/blob/b55ab2aabb68c0ce94c3903139b062d0c2790152/testing/test_cacheprovider.py#L551-L608) · [GitHub Checks 的修订与结论](https://docs.github.com/en/rest/checks/runs#create-a-check-run) · [本项目回归](../tests/test_crew_progress.py)

成员修订自己新增的探索性检查时，原失败随两次脚本的摘要值进入交接与 trace。获得团队内部交接后，负责人须指派另一个角色根据用户要求核对这些断言。复核者收到具体改动，在相同测试文件版本上实际取得通过信号并于公开交接中指明所核对的任务。只有完成复核的任务才从待办中移除。用户指定的验收及外部逐题验收仍分别保留结论。[复核边界测试](../tests/test_handoff_review.py)

成员自建核查脚本可以在通过行和失败行中都打印实际值。两份完整运行报告都能唯一定位同一源码断言标题时，宿主按该断言位置核对顺序，将分隔符后的实测值保留作诊断。因此复验数值变化不会创造新的用例。标题只有相同的单词前缀、脚本删减断言或一份报告缺少对应行时，历史失败继续阻止交接。即使核查员已经签出其他标准，修订过的自测仍须由另一名成员独立核对。这个处理沿用 pytest 按用例身份更新失败记录的做法，Studio 额外核对自建脚本版本及报告结构。[pytest 失败缓存更新](https://github.com/pytest-dev/pytest/blob/b55ab2aabb68c0ce94c3903139b062d0c2790152/src/_pytest/cacheprovider.py#L354-L360) · [子集复验边界](https://github.com/pytest-dev/pytest/blob/b55ab2aabb68c0ce94c3903139b062d0c2790152/testing/test_cacheprovider.py#L551-L608) · [Studio 群内复验测试](../tests/test_studio_native_review.py)

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

如果要核对通知中的日期是否符合工作区的原始记录，负责人可给这项标准设置 `"method": "inspect", "reference_path": "change.txt"`。评审员亲自读取成稿和指定原件，在该项同时引用两次读取的回执，并说明日期是否相符。两份文件各自绑定路径和 SHA-256，其中一份更新后需要复核。外部事实核查使用 `source`，评审员须亲自打开原始网页，同时引用成稿与网页回执。文件版本匹配借鉴了 in-toto 对指定路径和摘要的检查。[in-toto MATCH 实现](https://github.com/in-toto/in-toto/blob/e352b43ad7cb8915d84c36d791aa61346152a0a3/in_toto/verifylib.py#L645-L751) · [规则测试](https://github.com/in-toto/in-toto/blob/e352b43ad7cb8915d84c36d791aa61346152a0a3/tests/test_verifylib.py#L549-L743)

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

针对成员临时编写的内联断言，宿主在能够完整提供探针代码、原失败回执与完整错误输出时，为每条探针绑定一个必需的独立执行标准。内联 Python 与浏览器探针采用下述相同的 32 KiB 源码、8 KiB 输出和 96 KiB 总交接预算，不再因浏览器脚本超过 6,000 字节而失去完整移交途径；截断、取消、宿主执行超时或没有真实非零退出码时不转为可独立裁决的完整失败证据。评审者需在冻结文件上实际运行断言，说明原失败与复验结果。原作者的失败仍可查阅。未能完整提供的探针、未经本成员改写的项目测试文件和用户配置的硬性检查继续走原验收规则。实现沿用 LangGraph 的按状态选择下一节点与独立评估的工作流，用未关闭的检查身份作为转交条件。[LangGraph 条件边实现](https://github.com/langchain-ai/langgraph/blob/84ced603d3a201ab0f9379936bb35868e243f3f3/libs/langgraph/langgraph/graph/state.py#L982-L1013) · [条件路由测试](https://github.com/langchain-ai/langgraph/blob/84ced603d3a201ab0f9379936bb35868e243f3f3/libs/langgraph/tests/test_pregel.py#L2924-L2948) · [独立评估工作流](https://docs.langchain.com/oss/python/langgraph/workflows-agents#evaluator-optimizer)

成员本轮通过写入工具实际改过的 Python 检查脚本，若直接执行得到完整的非零退出回执，但只有汇总、没有可归属的逐项结果，也可以成为上述独立复核的对象。宿主保存**失败当时**的完整源码、脚本路径和 SHA-256、原调用 ID、检查进程退出码及完整失败输出；原作者修复一次后仍未关闭的失败，才随冻结候选交给另一成员。每份源码至多 32 KiB、失败输出至多 8 KiB，交接总量至多 96 KiB。未由该成员编辑、输出截断、执行期间文件变化、没有原源码或真实非零退出的检查不进入这条路径。作者修改检查脚本后，即使相同命令实际退出 0 并打印 `PASS 200 / FAIL 0`，也不会自行核销原脚本失败；原检查字节不变、只修正产品后重跑通过，仍可关闭对应失败。独立成员须说明每项原失败，并提供当前冻结工件上的实际断言回执；只读文件、进程退出 0、删除检查或作者的汇总均不能代替它。用户配置的硬性验收继续单独执行。

有名 `FAIL` 使用同一途径：完整原脚本及输出绑定到实际仍失败的“脚本版本＋用例”范围，并携带 `case_label`，不会另造一个没有对应失败的汇总范围。重复恢复时保留最初原文；后续浏览器失败与原命名失败分别裁决。原作者可以不改原断言、不制造输入来满足错误假设，由第三方根据原始任务逐项判断探针缺陷或真实产品缺陷。同名的其他脚本通过、只读回执或仅正常退出均不能核销。参考 pytest 的 `LFPlugin.pytest_runtest_logreport` 按 `nodeid` 更新当前结果，以及收集异常、子集复验不清除其他失败的回归；Studio 另用脚本 SHA 绑定候选版本，未复制上游代码。[pytest 失败身份处理](https://github.com/pytest-dev/pytest/blob/b55ab2aabb68c0ce94c3903139b062d0c2790152/src/_pytest/cacheprovider.py#L354-L367) · [异常与子集复验回归](https://github.com/pytest-dev/pytest/blob/b55ab2aabb68c0ce94c3903139b062d0c2790152/testing/test_cacheprovider.py#L518-L608) · [原日期与原命名脚本不变、经正常群入口独立裁决的回归](../tests/test_private_candidate_review.py)

原检查证据按仍未解决的检查范围保留一份，重复失败不会替换原件；解决后释放。达到 16 个原文名额时保留先前证据，将未收录的范围与原调用明确列入 `undisclosed_failure_probes`。交接总量或内容校验导致无法转交的范围列入 `untransferred_failure_probes`。这些失败继续阻止发布，不会作为已复核项省略。

运行证据 `failed_check_details` 显示原检查调用、步骤、工具外层退出码、检查进程退出码和错误节选。`scope_id` 与 `complete_verification` 的键是检查范围标识，文件摘要使用明确的 `sha256` 字段；不会要求模型猜测这些键或轮询不存在的异步进程。带 `subprocess` 和顶层断言的 `run_python` 是独立内联检查，内层退出 0 不掩盖外层 `AssertionError`。候选已经齐全、同一版本的其他检查重复通过而原失败仍未关闭时，成员收到定向的 `[BLOCKED]` 交接提示；新文件尚未交付或候选真实发生变化时，继续保留工作工具。该提示只改变移交时机，不批准成果，也不自动重放命令。

这一补充参考 pytest 将 `nodeid`、`outcome`、异常 `longrepr` 和捕获输出分别保存的机制，尤其是 `TestReport.from_item_and_call` 对实际异常的判断；其回归确认异常在序列化后仍存在、导入包装错误不会盖住原始异常。Studio 使用既有工具身份和冻结候选，另外保存成员编辑过的脚本原文供第三方裁决；未复制上游代码或引入依赖。[pytest 8.3.5 的报告构造与异常分支](https://github.com/pytest-dev/pytest/blob/b55ab2aabb68c0ce94c3903139b062d0c2790152/src/_pytest/reports.py#L270-L403) · [异常保留与导入错误回归](https://github.com/pytest-dev/pytest/blob/b55ab2aabb68c0ce94c3903139b062d0c2790152/testing/test_reports.py#L390-L414) · [Studio 普通群入口的 191/9→200/0→独立裁决回归](../tests/test_private_candidate_review.py) · [步骤证据与停滞回归](../tests/test_crew_progress.py)

### 独立评审期间的冻结文件

验收者拿到的候选与工作副本中原有的文件全部按字节冻结，包括别的成员已发布的检查脚本。新探针可放在系统临时目录，需交接的评审说明使用新路径或宿主指定的附件路径。直接写入、编辑、追加或删除冻结文件时，文件工具立即返回原定 SHA-256。通过 Python 或命令改动继承文件后，下一轮提示和提交检查会显示变动路径及原定、实际摘要。验收者应把差异交接给负责人，由负责人从原冻结快照重新发起评审。确有产品或旧检查缺陷时，由作者另行修订并交接新版本。

文件身份按路径和摘要匹配的做法参考 in-toto 的实现和匹配测试。Studio 在工具调用后和提交评审时检查本机工作副本。本项目的群工作流回归覆盖直接文件操作与间接命令改动。[in-toto 的路径及摘要匹配](https://github.com/in-toto/in-toto/blob/e352b43ad7cb8915d84c36d791aa61346152a0a3/in_toto/verifylib.py#L727-L765) · [上游匹配测试](https://github.com/in-toto/in-toto/blob/e352b43ad7cb8915d84c36d791aa61346152a0a3/tests/test_verifylib.py#L549) · [Studio 群评审回归](../tests/test_studio_native_review.py)

### 负责人结案时的独立验收回执

负责人收尾时，宿主检查公开工作区最新文件摘要与独立评审绑定的候选版本。用户列出的交付路径和结构化验收路径全部由已通过的评审覆盖，且成员、命令、业务问题和交付检查均已收束时，收尾输入会先列出评审人、候选版本、必需标准与文件 SHA-256。成员无额外自检标准时显示的 `unverified` 只描述该成员分工。相同操作后来成功时，早先的委派拒绝保留在轨迹，退出当前回合的纠正反馈。

若负责人依然回复 `[BLOCKED]`，在既有预算内给予一次纯文字核对机会，并在 `reviewed_delivery_blocked` 事件保存依据及处置结果。模型指出的独立验收以外的阻塞仍可成立，二次报告阻塞将原样结束任务。缺文件、显式验收未过、待答业务问题、未出版或已改变的候选都无法触发这种核对。最终交付继续经过 Studio 原有工作区和验收流程。

此机制参考 Pydantic AI 将输出验证错误交还模型、在重试上限后保留失败的处理。本项目自行实现了基于同群发布版本和用户交付文件的资格判定，并使用原有预算和审计轨迹。[Pydantic AI 输出验证与反馈](https://github.com/pydantic/pydantic-ai/blob/ef740deb08789941192f093436dd1a1c31ba4532/pydantic_ai_slim/pydantic_ai/_output.py#L222) · [Pydantic AI 重试上限测试](https://github.com/pydantic/pydantic-ai/blob/ef740deb08789941192f093436dd1a1c31ba4532/tests/test_agent.py#L14684)

成稿来源核查复用了现有 `ReviewSession`。每份指定交付的路径及 SHA-256 须出现在当轮独立评审者提交的必需 source 结论中，并与当轮亲自打开的原始来源一起引用。稿件版本变化即重新进入评审。以校规与论文为例，评审人需对照正式规定、建议、反证、研究样本和受限的适用范围。路径及哈希绑定借鉴 in-toto 对跨步骤材料与产物的匹配；本系统使用 Studio 宿主保存的文件读取及来源回执来识别评审角色，没有使用上游的外部签名基础设施。[in-toto 工件匹配实现](https://github.com/in-toto/in-toto/blob/e352b43ad7cb8915d84c36d791aa61346152a0a3/in_toto/verifylib.py#L645) · [上游匹配测试](https://github.com/in-toto/in-toto/blob/e352b43ad7cb8915d84c36d791aa61346152a0a3/tests/test_verifylib.py#L549)

在网页新建群聊时打开「选项」，或在群内继续对话时打开「本轮选项与预算」。在「需要独立核查来源的文件」中填写最终稿相对路径。选择「由负责人分工」，群内至少需要负责人、作者和另一位核查成员。

### 同一版本的未决核查

独立验收者提交证据不足时，负责人可用 `delegate_task` 把核查交回原验收者。应在目标里说明准备检查的新来源或方法。再次引用同一候选时，宿主沿用已绑定的验收者、文件版本和完整标准，并在公开的 `review_resumed` 事件中记录实际标准和待核查项。交接提示先展示缺口及建议行动，较长的原任务放在后面。

复查时，原版本已通过且引用材料哈希仍匹配的结论可连同原评审回执保留。未决的来源核查要通过，仍须在复查期间直接读取成稿并取得独立来源。重复遇到 403、改写说明或改变读取范围均不能让同一版本无限复查。只有必需的未决标准真正减少，才继续下一轮；原件变化则重新检查新版本。

设计参考 LangGraph 按检查点和任务保存已完成写入，以及 Pydantic AI 把具体验证错误交回调用方的方式。Studio 另行核对候选与证据哈希，并以未决标准的实际减少限制复查次数。[LangGraph 的中间写入](https://github.com/langchain-ai/langgraph/blob/84ced603d3a201ab0f9379936bb35868e243f3f3/libs/checkpoint-sqlite/langgraph/checkpoint/sqlite/__init__.py#L458-L499) · [Pydantic AI 验证反馈](https://github.com/pydantic/pydantic-ai/blob/ef740deb08789941192f093436dd1a1c31ba4532/pydantic_ai_slim/pydantic_ai/_output.py#L222) · [Pydantic AI 重试边界测试](https://github.com/pydantic/pydantic-ai/blob/ef740deb08789941192f093436dd1a1c31ba4532/tests/test_agent.py#L14684)

### 评审收尾期间的取证

评审人可调用 `list_review_evidence` 分页查阅本次尝试的宿主回执。每条短 ID 指向实际观察及其类别，来源附原始 URL 和记录摘要，文件附路径、版本和读取范围。同一 URL 返回不同内容时，每次取得的版本有独立 ID。逐项提交会一起返回存在问题的标准 ID、证据要求和缺失的工具操作。报告保留本次引用的证据、任务尝试与原工具调用 ID。

例如，评审人已执行网页测试并从发行方取得公告，但只用 shell 查看成稿时，收尾仍允许用 `read_lines` 读取指定成稿。拿到文件回执后，以它和对应的来源回执提交事实判断。若满足剩余取证条件的集合持续缩小，可在本次分配的时间和预算内继续。相同缺口连续三个收尾工具回合仍未缩小时，系统收回工具并要求明确交接未验证项。独立验收与最终发布仍分别检查绑定版本和用户标准。

普通 `execute` 评审的提交错误列出该标准可选的最新 `passed_check` 短 ID 和用例名称，评审人须选择实际支持该项判断的回执。只有读取、文件摘要或自定义脚本退出记录时，目录会按本角色获准的工具说明如何对冻结输入执行断言。达到收尾条件时仍沿用本轮取证次数和预算。测试以完整退出 0 的自建脚本和一个真实文件哈希重现错误，再经实际断言及短 ID 完成签署。[普通入口与权限回归](../tests/test_execute_review_guidance.py) · [Pydantic AI 工具验证纠错测试](https://github.com/pydantic/pydantic-ai/blob/6d52359b40e72b643306902b672dc963c660beb5/tests/test_tools.py#L4792-L4818)

这一实现借鉴 Pydantic AI 按字段返回验证错误、对同一次工具纠错限制重试的处理。Studio 自行实现按标准整理多个错误、真实工件回执与尚缺取证操作的关联。[Pydantic AI 的错误回执](https://github.com/pydantic/pydantic-ai/blob/6d52359b40e72b643306902b672dc963c660beb5/pydantic_ai_slim/pydantic_ai/messages.py#L2938-L2958) · [工具级重试测试](https://github.com/pydantic/pydantic-ai/blob/6d52359b40e72b643306902b672dc963c660beb5/tests/test_tools.py#L1765-L1803) · [Studio 普通入口回归](../tests/test_studio_required_source_review.py)

### 在后续群聊中直接复核原件

同群继续工作时，负责人可直接在 `delegate_task.review.candidate` 中填写成稿路径、原作者任务 ID 或可唯一确定的作者角色。例如，研究员可以检查上一轮营销成员发布的 `proposal.md`。宿主核对发布事件、原作者、本轮成稿 SHA-256 和归档快照中的实际字节。选择本地工作区时还须有该文件成功投影到同一目录的回执，因此上轮整体失败前已经公开交付的成员工件也可核对。内部工作区沿实际继承的基础版本查找。开启新项目会截断文件来源链。

绑定后，群事件 `published_review_bound` 记录原运行、作者任务、文件摘要、本轮评审者和标准。评审者独立取证并提交本轮结论。原件上的未决标准会带入复核；同一版本已有明确产品缺陷时，应由作者修订再发布。缺失原始来源或本轮硬性验收不通过时，复核不能授权交付。实际作者只有在需要修订产品时才需再次交接。

同一已发布原件已有未决必需标准时，负责人可只写 `review.candidate`，宿主从该原件的签署记录恢复完整定义。`review.criteria` 可只列本轮追加的条款，额外要求使用新 ID。若复用了旧 ID 却改变方法、描述或必需性，宿主返回完整原定义和修正方式。当前用户绑定的来源核查和最终交付检查仍须满足。首次评审必须显式提出至少一个必需标准。按具体字段回传可纠正错误的做法参考 Pydantic AI，Studio 在同群发布及版本核对后选择原定义。[Pydantic AI 验证反馈](https://github.com/pydantic/pydantic-ai/blob/6d52359b40e72b643306902b672dc963c660beb5/pydantic_ai_slim/pydantic_ai/messages.py#L2938-L2958) · [工具纠错测试](https://github.com/pydantic/pydantic-ai/blob/6d52359b40e72b643306902b672dc963c660beb5/tests/test_tools.py#L4792-L4818)

路径和版本匹配参考 in-toto 逐项核对上一步产物和下一步材料的做法。Studio 使用本机群发布回执、不可变交接归档与即时工作区摘要来确认来源。[in-toto 的材料匹配](https://github.com/in-toto/in-toto/blob/e352b43ad7cb8915d84c36d791aa61346152a0a3/in_toto/verifylib.py#L727-L765) · [上游匹配测试](https://github.com/in-toto/in-toto/blob/e352b43ad7cb8915d84c36d791aa61346152a0a3/tests/test_verifylib.py#L549) · [Studio 跨轮复核回归](../tests/test_studio_inherited_review.py)

### DeepSeek 额度不足时的诊断与接续

DeepSeek 官方将 HTTP 402 定义为账户额度不足。Studio 将其显示为“DeepSeek 账户额度不足”，并保留原任务、尝试、HTTP 状态码、真实请求次数及不可重试标记，不保存响应正文、凭据或服务地址。既有历史轨迹即使只记录 `provider_error.code=http_error`，也可通过同一尝试的 provider 配置识别；没有服务身份的普通 402 不推断为 DeepSeek 额度不足。新配置只记录 `official_api` 布尔值，第三方兼容端点不会被按官方 402 处理。[官方错误码说明](https://api-docs.deepseek.com/quick_start/error_codes/)

首次确认官方 402 后，同一轮共享的 provider 实例停止发出后续模型请求，并通过原有 ProviderError 收尾路径保存成员状态和草稿。首次错误保留真实 `request_attempts`，之后本地拒绝为 0；不会把本地拒绝算作新的 HTTP 请求。已经在途的并发请求不被强行撤回，也不把其他 `retryable=False` 错误一概锁住。新一轮新建实例，仍可正常尝试；接续需要可用额度，Studio 不执行余额或支付操作。

API 诊断项为 `provider_balance_exhausted`。最新群头若有经过校验的成员检查点，`recoverable_drafts` 列出其任务 ID 和角色，GUI 提示可在项目文件选项中选择。未找到检查点时不声称草稿一定可恢复；旧轮详情不借用后续轮的草稿。后续同一任务获得有效响应时，先前 402 保留为 `resolved=true` 的历史警告，不覆盖新的执行或验收结果。其他任务的成功、晚到的旧尝试响应均不能据此声称恢复。额度受阻与成稿是否满足业务目标是不同结论。

实现参考维护中的 DeepSeek Harness 将 402 映射到 QUOTA、默认不重试此类非瞬态错误及其“仅一次请求、无重试计时器”回归。Studio 额外按一轮共用实例保存终止原因，以避免成员失败后负责人再次请求同一服务；未复制上游代码。[官方适配器分类](https://github.com/deepseek-ai/deepseek-harness/blob/ca33fd6843b205e279c7ccb6cb73a472c78f5760/packages/llm/llm-deepseek/src/transport.ts#L34-L54) · [重试异常分支](https://github.com/deepseek-ai/deepseek-harness/blob/ca33fd6843b205e279c7ccb6cb73a472c78f5760/packages/llm/llm-retry/src/index.ts#L191-L221) · [单次请求回归](https://github.com/deepseek-ai/deepseek-harness/blob/ca33fd6843b205e279c7ccb6cb73a472c78f5760/packages/llm/llm-retry/tests/retry.spec.ts#L454-L465) · [Studio 新鲜/历史诊断及正常群入口回归](../tests/test_studio_diagnostics.py)

### 产品通过后的附属检查复核

产品的原生评审 `passed` 与成员修订检查脚本后留下的 `pending_check_reviews` 是两种义务。真实运行中曾出现：评审者修正自己的检查器，原稿评审通过；负责人却不断重开原稿的全套评审，而剩余义务其实是由另一成员独立运行当前检查器。当前公开状态同时显示仍匹配文件 SHA 的产品通过回执，以及待核任务、scope、命令、检查路径、原始与当前脚本 SHA、可选的独立成员和 `[CHECKED 原任务ID]` 交接标记。不会在每轮指令重复附带整个候选项目清单。

自动纠偏只在用户当前预期交付全部由有效通过回执覆盖，且没有其他产品标准、来源要求、成员交接或交付门禁未决时启用。负责人误用精确任务 ID 或唯一成稿路径、按原有标准重开已通过的评审，会转给有资格的同伴核验剩余检查。作者角色别名不会被猜成某份产品；新标准、不同候选和普通、不带 `review` 的后续业务分工继续走原路径，不能把它们静默丢弃。重开请求无法成立时，错误反馈会附上具体剩余义务与合法派工方式。多项检查由各自的作者及最后编辑者决定资格，一位成员只收到自己可独立核验的项目；没有独立成员时如实报告阻塞。同伴这次检查使用当前公共快照，不会接回该角色自己的旧私有草稿。

纠偏不运行命令，也不核销任何责任。成员仍须自行读取原要求、完整执行相同 scope 的当前版本检查、取得真实回执并明确交接。现有同版本、同 scope、非作者/最后编辑者且本轮未改待核文件的条件继续生效。产品通过回执不会被改回待审，也不会替代任何真实失败。`peer_check_routed` 群事件记录请求成员、实际接收者、待核任务和对应产品回执。

参考 OpenAI Agents SDK 的 `execute_handoffs`：可恢复派工错误返回原调用者，成功转移前保留错误并允许纠正；取消和不可恢复异常不会被当作成功。本项目将错误说明具体化为剩余义务与合法角色。另参考 LangGraph 的剩余计划测试，保存未完成步骤并只路由下一项；Studio 沿用自己的双队列和已有核销机制，没有引入上游框架或复制源码。两个项目均为 MIT 许可证。

- [Agents SDK 交接与异常处理](https://github.com/openai/openai-agents-python/blob/91fb508a8931016efa793b033147d08cd20308cf/src/agents/run_internal/turn_resolution.py#L545-L633) · [纠错和异常边界测试](https://github.com/openai/openai-agents-python/blob/91fb508a8931016efa793b033147d08cd20308cf/tests/test_handoff_failure_error_function.py#L31-L217) · [许可证](https://github.com/openai/openai-agents-python/blob/91fb508a8931016efa793b033147d08cd20308cf/LICENSE)
- [LangGraph 剩余计划路由测试](https://github.com/langchain-ai/langgraph/blob/bbd52757e72d8611b07ae2831b9529a6ba03f2a1/libs/langgraph/tests/test_pregel.py#L5230-L5284) · [Command 目标校验](https://github.com/langchain-ai/langgraph/blob/bbd52757e72d8611b07ae2831b9529a6ba03f2a1/libs/langgraph/langgraph/pregel/_io.py#L56-L78) · [许可证](https://github.com/langchain-ai/langgraph/blob/bbd52757e72d8611b07ae2831b9529a6ba03f2a1/LICENSE)
- [正常群入口和误路由反例](../tests/test_studio_peer_checks.py) · [同版本独立回执与反例](../tests/test_handoff_review.py)。这些是无付费模型请求的平台回归，不代表真实模型运行或原题交付已经通过。

### 已签评审引用未决反证时的独立裁决

评审人可能已提交原生 `passed`，同时明确引用自己检查中的一个真实 `failed_check`。例如，来源记录把公示稿 URL 放在 `links[].url`，评审人最初却只在 `text` 里查找；修订脚本后检查通过，旧版本的命名失败仍然成立。此时不能由改脚本的人直接清掉旧反证，也不能因为成员最后回复 `[BLOCKED]` 就丢失已提交的其他标准和来源回执。

宿主只在以下条件同时成立时安排一次定向裁决：原生评审已接受 `passed`；每个当前未决失败都被已签结论显式引用；这些失败都来自本次评审人，且能提供完整原代码、真实错误与退出回执；候选及继承文件未变化，没有缺失交付文件或失败的用户显式验收。没有被正式引用的普通探索失败不因此新增产品标准。证据缺失、超出既有限额、其他未决失败或取消运行时仍明确阻塞。

`reviewer_probe_pending` 事件保存原产品目标、版本、评审身份、完整已签结论，以及逐 scope 的冻结原检查。原 `Review.latest` 保留已签项，状态保持待审；负责人下一次正常 `delegate_task` 优先转给独立于产品作者和原评审人的成员。负责人及以上两位当事人均不能担任裁决者。没有符合身份的成员时保留证据并明确结束为阻塞。不会自动执行旧 shell 命令，也不会让负责人通过新建一个较宽松的 `review` 替换原标准。

子复核复用既有 `Review.probes`、冻结私稿和发布检查。`kind=reviewer_probe` 与 `probe_parent` 连接原产品；`probe_<原scope>` 保留反证身份、原代码及实际错误，原涉事标准的 inspect/source 语义继续保留。source 裁决须读同候选的业务成稿并亲自打开来源；不强制使用 Python。execute 裁决须有当前冻结产品上的实际执行证据：支持直接读取文件数据的 Python 断言，以及打开本地冻结入口后对该页面执行的真实浏览器 `expect`/`assert`。宿主仅保守记录可归属的输入路径与 SHA，不声称静态分析能证明业务标准；裁决者仍须逐项说明原失败和实际行为。只读后 `assert True`、另一个版本、替换页后的无关 DOM 断言和仅进程退出 0 都不能提供所需执行依据。动态服务器 URL 无法归属本地候选时不凭猜测签发执行凭证，可改用可绑定入口的真实浏览器检查；不能降低原标准。

裁决通过后仍先走原私稿发布、成员验收、最终用户检查及文件 SHA 校验，再恢复原产品评审人签署的结果；第三人不会代签其他产品标准。确认产品确有问题时回原产品作者修订，并保留原产品评审人及完整标准。无法核实、修改冻结 HTML/脚本、来源不足或新失败保持阻塞。`reviewer_probe_resolved` 记录具体 scope、裁决者、候选版本和结论；修复后的脚本 PASS 不覆盖历史失败回执。

独立裁决逐项按必需标准和原因分流。`verification` 保留候选版本并交给同一裁决者补证，`product` 的返工结论才会回到原产品作者。`business` 通过本群原人工请求等待答复，用户的 `reply_human` 恢复同一版本、同一项独立裁决，评审人仍须提交带证据的结果。下面的 Studio 入口测试分别经过了补证和一次真实的请求/答复，再核对最终公开版本。

机制参考 LangGraph 按检查点/任务身份恢复未决项，并拒绝无法定位的恢复值；Studio 另以原产品版本和失败 scope 绑定子义务，不自动重放节点。也参考 OpenAI Agents SDK 在可恢复交接错误后保留调用状态、向调用者返回具体验证错误；这里的业务裁决仍使用 Studio 原来的独立评审和硬性验收。两项目均为 MIT 许可证，未复制源码或添加依赖。

- [LangGraph 恢复及错误处理](https://github.com/langchain-ai/langgraph/blob/bbd52757e72d8611b07ae2831b9529a6ba03f2a1/libs/langgraph/langgraph/pregel/_loop.py#L1029-L1075) · [按未决身份恢复的测试](https://github.com/langchain-ai/langgraph/blob/bbd52757e72d8611b07ae2831b9529a6ba03f2a1/libs/langgraph/tests/test_interrupt_state.py#L100-L143) · [歧义恢复反例](https://github.com/langchain-ai/langgraph/blob/bbd52757e72d8611b07ae2831b9529a6ba03f2a1/libs/langgraph/tests/test_interrupt_state.py#L352-L367)
- [Agents SDK 交接和异常处理](https://github.com/openai/openai-agents-python/blob/91fb508a8931016efa793b033147d08cd20308cf/src/agents/run_internal/turn_resolution.py#L545-L633) · [失败返回与异常边界](https://github.com/openai/openai-agents-python/blob/91fb508a8931016efa793b033147d08cd20308cf/tests/test_handoff_failure_error_function.py#L31-L217)
- [普通 Studio 入口、命名失败、来源及反例](../tests/test_bound_reviewer_probe.py) · [真实本地浏览器与替换页反例](../tests/test_bound_reviewer_browser.py)。这些为脚本模型、模拟来源及本地浏览器的平台回归，不代表原题成稿或真实模型重新执行已通过。

壳命令仍向模型展示原有的 1,400 字节首部与 2,200 字节尾部。若真实非零退出的完整失败输出（含 stdout、stderr 和退出码）不超过 8 KiB，宿主另存 `complete_failure_output`；模型摘要的 `truncated=true` 原样保留，冻结探针的 `display_truncated` 表明二者区别。解析命名失败和移交原错误时使用这份有相同调用身份的完整宿主记录。输出超界、超时、取消或信号中止均不获该例外；成功但截断的输出仍只说明进程完成。旧运行没有存下完整原输出时不能事后补造，因此历史 T2-04 的 `a6b1f4…` 仍应显示缺少完整证据；本修复使相同真实输出规模的新调用可以完整保留、正常移交。

这一分离参考 Pi 的 `OutputAccumulator`/shell 工具：模型展示截断内容，结构化结果引用独立保存的完整输出，并区分真实退出码与后台管道超时。Studio 只保存满足既有 8 KiB 探针上限的失败原输出，不增加模型展示预算或自动执行。未复制源码。[Pi 完整输出与截断显示](https://github.com/earendil-works/pi/blob/f6d0c2f9b6ffbcb00c5da52e7e88570539c78bde/packages/coding-agent/src/core/tools/bash.ts#L304-L436) · [真实退出与超时回归](https://github.com/earendil-works/pi/blob/f6d0c2f9b6ffbcb00c5da52e7e88570539c78bde/packages/coding-agent/test/suite/regressions/11613-bash-timeout-after-shell-exit.test.ts) · [MIT 许可证](https://github.com/earendil-works/pi/blob/f6d0c2f9b6ffbcb00c5da52e7e88570539c78bde/LICENSE)

### 根据已签评审直接完成成员交接

独立成员对已绑定的公开候选成功 `submit_review`，且候选、必需工件、指定检查及当前分工门禁全部满足时，宿主直接从已签记录生成可读交接，再执行原有成员验收与发布流程。不会再向压缩成纯文字的模型请求一次“是否完成”的判断。逐项理由、来源访问限制、URL 与读取/执行回执保持原签内容；例如入口网页返回 CAPTCHA、后来用官方 PDF 核实，交接仍逐字保留这个区别，不能改写成入口访问成功。

此路径只处理成功签署所在工具批次的结尾，或已存在的精确同伴复跑结束后。签署之后同一批次新增操作、工具错误、失败检查、缺失交付、未完成命令、未满足的指定复跑、文件 SHA 变化、取消、预算或用户显式验收问题继续走原门禁。私稿及私稿晋升仍使用原成员交接、独立复跑和发布检查；本次不改变其完成方式。模型未签署时的 `[BLOCKED]` 不被关键词改判；新出现的真实问题也不会被忽略。

运行轨迹的 `tool_completion` 保存候选版本、原 assessment、触发调用及宿主交接文本；`member_report.handoff.signed_handoff` 连到相同签署。原工具步骤记录不改写为模型答复，用户验收仍由原来的验收器记录，发布仍校验当前文件。没有结构化契约的普通成员分工继续使用原文字交接。

实现参考 OpenAI Agents SDK 的 `check_for_final_output_from_tools` 与 `ToolsToFinalOutputResult`：受宿主管理的工具结果可以直接产生最终输出，不必再调用模型，并继续经过已有最终输出处理；无结果、错误的回调返回类型和无效策略不会被误认为成功。Studio 额外检查冻结候选、原评审和任务门禁，并保留原逐项理由，没有采用“任意第一个工具即完成”。未复制上游代码或引入依赖；上游为 MIT 许可证。[工具结果决定完成与异常分支](https://github.com/openai/openai-agents-python/blob/91fb508a8931016efa793b033147d08cd20308cf/src/agents/run_internal/turn_resolution.py#L801-L834) · [复用最终输出处理](https://github.com/openai/openai-agents-python/blob/91fb508a8931016efa793b033147d08cd20308cf/src/agents/run_internal/turn_resolution.py#L235-L272) · [无第二次模型调用与返回类型反例](https://github.com/openai/openai-agents-python/blob/91fb508a8931016efa793b033147d08cd20308cf/tests/test_tool_use_behavior.py#L184-L237) · [本项目普通入口回归](../tests/test_signed_review_handoff.py)

### 复用已有 Python 业务检查函数

独立脚本可能通过 `ok = identity() and paging(); sys.exit(0 if ok else 1)` 汇总结果。普通退出码没有说明哪些检查实际执行，因此不会仅因这一语法或文件名自动得到 `passed_check`。这条边界保持不变：纯打印、恒真条件、没有进入检查分支、吞掉错误后正常退出都不能自动认证成业务通过。

若原脚本已有清楚返回 bool 的真实业务检查函数，评审人可以先读原脚本，然后在现有 `run_python` 中用 `runpy.run_path(..., run_name='__review_probe__')` 载入它，分别顶层断言原函数返回值 `is True`，最后输出一条 PASS。不必重写长浏览器检查。`list_review_evidence` 的 `execute.existing_python_check` 给出可复制模板；仅在该成员实际拥有 `run_python` 时展示。模板捕获 `SystemExit` 并转为断言失败，避免函数提前退出 0 被当作完成。它不调用脚本的 `__main__` 汇总分支，不以子进程退出码或恒真断言代替业务核查。

原脚本与工件仍需保持冻结版本，引用本次尝试、本次工具实际产生的 E 回执。脚本、工件变化或原函数真实返回 False/异常时，不能冒用先前回执提交。该方式是普通产品 execute 标准的聚合断言；`runpy` 内部函数调用没有通用的静态输入追踪，不能凭它核销另一个版本、同伴指定脚本的独立退出义务或要求输入绑定的争议 scope。找不到可复用业务函数时，仍需写较小的独立检查或使用标准测试运行器；系统不会解释任意 Python 程序来推定通过。

依据 Python 标准库的实现：`run_path` 明确设置模块名并返回执行后的全局字典；其测试验证自定义 `run_name`、脚本路径与异常。`unittest` 先获得已执行测试的结果，再用 `wasSuccessful()` 决定进程退出码，并对零测试单独处理，因此单独观察 exit 0 不能反推出具体业务断言。这里只提供该标准 API 的适配指引，没有复制上游源码或改变验收判定。[CPython run_path](https://github.com/python/cpython/blob/v3.14.0/Lib/runpy.py#L262-L294) · [脚本名、路径与异常测试](https://github.com/python/cpython/blob/v3.14.0/Lib/test/test_runpy.py#L631-L683) · [unittest 结果与退出码](https://github.com/python/cpython/blob/v3.14.0/Lib/unittest/main.py#L264-L277) · [端到端回归](../tests/test_existing_python_check.py)

T2-06 原脚本与冻结 HTML 的授权离线副本已实测：原身份及双尺寸翻页函数真实完成后产生可引用 E 回执；更改 HTML 的负例真实退出 1，旧候选版本与旧 E 被拒绝。这是新离线验证，未改写历史 failed，也不代表线上任务重新执行完成。

### Superagent 的统一对话与工作室工具

统一助手沿用单聊的模型、工具注册表、文件工作区、运行轨迹和任务状态。首页获得查询角色、创建角色或群、向目标派工及读取已交付文件的工作室工具。已保存的角色可立即用于私聊，或作为群聊成员与负责人。群内成员使用各自的执行工具和已授权的上下文。

`GET /api/superagent` 返回固定会话的 `conversation_id` 和当前 `head_run_id`，首条消息之前两者为 null。`POST /api/superagent/messages` 接受 UUID `request_id`、文本 `message`、当前 `parent_run_id` 及可选的 `workspace_path`。首次发送可省略父轮或传 null。刷新后可以按返回的运行 ID 读取原有详情接口。发送方复用相同请求 ID 可以找回已有运行。来自多个页面的不同消息争用同一父轮时，后到的页面取得 `stale_head`，应先同步对话。工作进程异常退出会释放该轮的 SQLite 写锁。用户随后可以发起新一轮，已执行过的工具不会自动重放。

每次成功创建会写入 `studio_entity` 事件，内含类型、名称和实体 ID。原生空群的 `mode` 为 `room`，状态为 `ready`，轮数为零。在首次发送消息前，用户可以打开、刷新和检查成员。第一次消息会使用这个群实际保存的角色与负责人。用户明确切换本地目录时，统一助手保留聊天历史，重新取所选目录的文件。新群默认使用创建时的项目目录，第一条真实消息再从磁盘读取工作副本。

工具定义参考 OpenAI Agents SDK 将工具名、参数 schema 和当前运行的执行上下文绑定，在参数不匹配或执行失败时给模型返回可修正的错误。Studio 使用现有 ToolRegistry 和服务端角色、群聊存储完成这些操作。参考了接口设计，未复制上游源码。上游采用 MIT 许可证。[函数工具调用实现](https://github.com/openai/openai-agents-python/blob/91fb508a8931016efa793b033147d08cd20308cf/src/agents/tool.py#L2809-L2851) · [错误输入和执行异常测试](https://github.com/openai/openai-agents-python/blob/91fb508a8931016efa793b033147d08cd20308cf/tests/test_function_tool.py#L642-L665) · [统一对话端到端测试](../tests/test_studio_superagent.py)

### 跨会话派工

`list_studio_targets` 列出工作室可接单的角色和当前项目的命名群。角色目录只含公开职责，群目录列出成员、负责人和稳定 ID。模型用 `dispatch_studio_task` 提交目标类型、ID、整项任务需要的参与者、工作说明和可选的输入与验收文件。宿主检验成员与项目归属。单人私聊只能承接该角色能够独立完成的工作。命名群使用已保存的负责人和真实成员。

每个派工有持久的 `dispatch_id` 和预留的子运行 ID。来源轮成功收尾并释放工作位置后，调度器创建原生私聊或群任务。相同来源轮对同一目标重复提交同一工作会返回同一回执。目标会话忙时保留队列项。进程重启后按已发布运行的来源身份重新关联。来源轮失败或取消时，尚未发出的派工会记录原因。角色接单沿用该助手在本项目的派工私聊，个人另开的私聊独立保存。

两人群里，如果负责人也负责撰稿或汇总，可以认领具体文件，在单独的执行任务中写入并交接。另一位成员须实际参与，作者和独立验收人分开记录。按文件名发起正式复核时，宿主核对当前版本的生产者和 SHA。本轮显式转入的角色私聊原件也可按文件路径定位原作者；发生变更后，须依据新版本重新核对。

来源回合的 `dispatches` 列出收件者、工作说明、状态、子运行与会话 ID、待答问题、答复和交付文件。每次派工都有独立的原生运行和验收记录，预算亦按运行计量。要求文件存在的验收只证明该项文件检查通过，判断其内容可采用独立成员复核和已有的验收规则。主页可继续接收新消息，并刷新较早回合的待办。

选了本机目录时，接单方从该项目目录签出工作副本。显式转交的工作区文件或历史交付按项目相对路径放入子工作区，同名冲突会报告。成功或未配置独立验收的子运行收尾时，只发布负责人的公开工作区里新增、修改或明确指定验收的文件。交付封存到原生 handoff 快照。后续工作看得到相对路径、SHA-256 和 `dispatch:<UUID>/<项目路径>` 引用。打开或转交时核对内容哈希。模型可通过 `list_studio_deliveries` 查找本项目记录，通过 `read_studio_delivery` 阅读实际文本。

作者修订后，可以把新版本再交给原评审员。宿主会查出评审员上次收到的版本，仅当当前私有副本仍与该版本逐字节相同、新版来自同一上游会话且轮次更晚，才替换本轮输入。评审员另行编辑过的文件、异源同名文件、用户本地修改都会触发冲突。版本覆盖规则参考 Git 的双树合并，并增加了派工身份核对。[Git 版本选择](https://github.com/git/git/blob/v2.47.0/unpack-trees.c#L2935-L2941) · [工作副本保留测试](https://github.com/git/git/blob/v2.47.0/t/t1001-read-tree-m-2way.sh#L282-L303) · [工作室四轮回归](../tests/test_studio_route_revisions.py)

开源参照采用 OpenAI Agents SDK 的管理者调用专家并提取本次结果的模式，以及按实际能力向模型开放工具的做法。Studio 将专家执行落到原生持久会话，使单执行位置也能完成交接。Temporal SDK 的稳定运行身份与重复启动处理用于核对本地崩溃恢复语义。当前实现沿用项目原有运行时与 SQLite。[专家调用实现](https://github.com/openai/openai-agents-python/blob/91fb508a8931016efa793b033147d08cd20308cf/src/agents/agent.py#L646-L692) · [当前调用结果测试](https://github.com/openai/openai-agents-python/blob/91fb508a8931016efa793b033147d08cd20308cf/tests/test_agent_as_tool.py#L896-L938) · [工具可见性](https://github.com/openai/openai-agents-python/blob/91fb508a8931016efa793b033147d08cd20308cf/src/agents/run_internal/turn_preparation.py#L98-L141) · [Temporal 重复运行处理](https://github.com/temporalio/sdk-python/blob/e64f9dd3b3b513da6676caf5c1a6cbbb36119503/temporalio/client/_impl.py#L201-L221) · [重复运行测试](https://github.com/temporalio/sdk-python/blob/e64f9dd3b3b513da6676caf5c1a6cbbb36119503/tests/test_client.py#L111-L145)
