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
| 原群继续 | POST /api/runs，conversation.parent_run_id 指向最新一轮，可指定 inherit_files=false 或 recover_failed=true |
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

在「接着本轮未完成文件」续聊时优先使用负责人保存的项目。若负责人还没有公开文件，且只有一名受阻成员、其最后一次任务留有完整且经过宿主校验的项目检查点，可以从该检查点恢复。新回合记录源任务和角色，原回合的失败状态可在历史消息中核对。只读提问和默认续聊沿用已经发布的版本。
