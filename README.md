# miniagent

工作室有一个统一助手。你可以直接与它交谈，请它创建角色、组建团队并派工，也可以进入角色私聊或已有群聊。所有入口都使用同一个执行核心。群聊负责人可以独立完成任务，也可以委派成员或请人复核。

```
版本 1.0.0 · Python 3.10+ · DeepSeek API · 本地网页工作室 · 离线演示
```

## 快速开始

```sh
pip install -r requirements.txt
python -m playwright install chromium        # 成员用真实浏览器检查网页时需要
cp .env.example .env                         # 可选，也可在网页「模型连接」中保存密钥
python -m miniagent studio                   # 打开 http://127.0.0.1:8765
```

1. 点「新建群聊」，选一个 Mid_Test 场景模板（软件团队、新闻编辑部、证券基金团队），或者自己填写成员 ID、名称和职责。
2. 点「填入任务 1」或直接输入任务后发送。负责人提问时，在输入框回答即可。
3. 右上角「文件」可浏览、预览和下载团队项目。HTML 交付在独立的预览端口打开。启动器会显示实际使用的两个端口。
4. 第一个任务结束后，在同一个群的同一会话里发任务 2，团队会接着用已有的对话和文件。

从「角色库」新建角色，在角色详情里开私聊、查看它参与的任务、编写或批准记忆和技能。同一角色的不同私聊有各自的历史和文件。只有批准为通用的知识会随角色进入其他会话。群聊输入 `@` 可按名字或 ID 选择群成员，方向键选择，回车确认，Esc 收起。句首点名可以把工作直接交给该成员；未点名的交办由群负责人接手，并根据本会话的需求与成员职责决定是否派工。

同一群可以同时处理多个独立项目。在群聊顶部点「新建会话」并命名，用旁边的下拉框切换。群成员和负责人在整个群共用；每个会话保留自己的聊天、附件、任务队列、工作区和产物。相关任务留在同一会话，便可继续修改上次交付。已有群聊沿用原链接，原项目仍在 `<runs_dir>/groups/<群 ID>/project`。新会话默认使用该群目录下的 `sessions/<会话短 ID>/project`。

点击聊天标题下的「工作区」可查看完整路径、选择本机项目文件夹，或手动输入绝对路径。选好后就可以在聊天里问目录结构或文本文件内容，助手会按需只读查看。交办任务时，执行成员拿到所选目录的工作副本，结束时将本次发布的文件写回。Super Agent 新建的群聊会自动固定到左侧，并沿用助手当前选择的本地目录。可在群聊的「会话管理」中取消固定。发消息时可添加多个附件、调整思考模式。群聊和私聊会结合本会话记录理解消息。问进展、原因或上次交付，会直接在聊天里回答。明确改要求会送到当前任务，独立的新工作在团队忙时排队。勾选「排队新任务」会直接交办。「任务记录」提供单次任务的对话、轨迹、交付文件快照和验收结果。「文件」面板展示团队最新项目，可导出群聊记录与文件供独立验收。

不联网、没有密钥也能看完整流程：

```sh
python -m miniagent demo                      # 脚本模型：负责人 → 写作者 → 审阅者 → 交付
python -m unittest discover -s tests -t .     # 离线测试
```

命令行运行一个群任务。成员提问时在终端回答：

```sh
python -m miniagent team --template scenario_1 --task 1
python -m miniagent team --group <group_id> --message "Extend the system with a dashboard …"
python -m miniagent report                    # 完成率、耗时、步数、派工、token
```

## 它怎样工作

| 机制 | 说明 |
| --- | --- |
| 委派 | 负责人调用 `delegate_task(assignee, task, deliverables)`，成员在调用内跑完自己的工具循环，交回报告和已发布的文件清单。委派按顺序进行。 |
| 隔离 | 每个智能体只能在自己的沙箱目录里读写。文件只通过团队项目流转：开始前检出，结束时发布新增、修改和删除。 |
| 人在回路 | `ask_human` 把问题发到群里并等你回答。属于当前工作的要求调整会在成员下一步送达。自主模式下不提问，团队自行作出并写明假设。 |
| 记忆 | 每个智能体能看到群里的历史对话（以往任务、派工、报告、问答、最终答复），也能看到项目文件、经过用户批准的角色知识和本群近期分工。 |
| 有边界 | 成员默认最多 100 步，负责协调的负责人 80 步、24 次派工。几乎相同的派工最多重复 3 次。预算用尽或检测到循环时，模型会被要求不用工具写出报告，所以调用方总能拿到报告和真实状态。 |
| 上下文 | 每一步发送完整对话。超过约 11 万 token 时，先裁掉旧的大段工具输出，必要时把较早部分替换为结构化摘要（目标、约束、进展、决定、已确认事实、下一步）。系统提示和任务原文始终保留。 |
| 工具 | 文件读写和局部编辑、ripgrep 搜索、Python、Bash（可启动后台服务，结束时回收）、无头 Chromium 浏览器检查、新闻/论文检索和网页/PDF 抓取（存档在 `sources/`）。 |

设计和上游参考（OpenHands 卡死检测、Pi 上下文压缩、OpenAI Agents SDK、AutoGen Magentic-One）见 [docs/architecture.md](docs/architecture.md)。Mid_Test 实测记录见 [docs/mid-test-status.md](docs/mid-test-status.md)。

## 配置（`.env` 或环境变量，环境变量优先）

| 变量 | 默认 | 含义 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | — | 真实模型调用需要，网页和离线演示可直接打开 |
| `DEEPSEEK_MODEL` | `deepseek-flash` | 或 `deepseek-v4-pro` |
| `DEEPSEEK_THINKING` | `enabled` | 发送任务时可单独开启或关闭 |
| `MA_MAX_STEPS` | 100 | 成员每次分工的步数上限（1–1000） |
| `MA_LEAD_MAX_STEPS` | 80 | 负责人每个任务的步数上限 |
| `MA_MAX_DELEGATIONS` | 24 | 每个任务的派工上限 |
| `MA_MAX_SECONDS` | 0 | 每次智能体运行的时间上限，0 表示只受步数约束 |
| `MA_COMPACT_AT_TOKENS` | 110000 | 达到此请求规模时开始压缩 |
| `MA_INTERACTION_MODE` | `interactive` | 新群默认的协作方式 |

## 代码地图

| 文件 | 职责 |
| --- | --- |
| [agent.py](miniagent/agent.py) | 单个智能体的有界“推理—行动—观察”循环 |
| [context.py](miniagent/context.py) | token 估计、工具输出裁剪、摘要压缩 |
| [loop_guard.py](miniagent/loop_guard.py) | 重复、报错、交替和反复读取等卡死模式检测 |
| [team.py](miniagent/team.py) | 负责人和成员的协作协议、提示词、跨任务上下文 |
| [workspace.py](miniagent/workspace.py) | 私有工作区与团队项目之间的检出和发布 |
| [store.py](miniagent/store.py) | 群、聊天记录、任务、轨迹和记忆的持久化 |
| [studio.py](miniagent/studio.py) · [server.py](miniagent/server.py) · [web/](miniagent/web) | 服务、HTTP 接口、网页界面 |
| [tools.py](miniagent/tools.py) 等 | 文件、代码、Shell、浏览器和资料检索工具 |
| [providers.py](miniagent/providers.py) · [provider_http.py](miniagent/provider_http.py) | DeepSeek 客户端和离线脚本模型 |

每个群的数据保存在 `runs/groups/<id>/`：`chat.jsonl` 是公开时间线，`project/` 是交付文件，`tasks/<id>/trace.jsonl` 记录每一步的工具调用。

## 迁移与验收

用单独的数据目录测试和迁移。指定同一个 `--runs-dir` 的网页服务和写入型 CLI 命令会竞争同一把锁。macOS 双击仓库中的 `Start Studio.command` 启动工作室。主工作室的固定地址是 `http://127.0.0.1:8765/`。再次双击会打开同一实例和原聊天记录。如果其他程序占用 8765，启动器会提示端口冲突。

```sh
python -m miniagent --runs-dir ./runs-import migrate --source ../MultiAgentNLP/runs
python -m miniagent --runs-dir ./runs-import studio
```

迁移读取旧角色库和 SQLite 中已生效的知识。旧群的 `memory.md` 没有批准记录，会作为本群的待审知识导入。重复运行迁移会跳过已有条目。迁移命令仅处理角色知识与定义。

批量运行入口为 `python -m miniagent --runs-dir ./runs-eval eval --tasks tasks.jsonl`。它默认调用真实模型，会产生 API 用量。每一行描述一次用户发言，共用 `group` 的行按顺序进入同一群。下面两行演示格式。

```jsonl
{"group":"software","template":"scenario_1","text":"Create the booking system and save its instructions in README.md.","checks":[{"kind":"exists","path":"README.md"}]}
{"group":"software","text":"Build a dashboard from the saved bookings.","checks":[{"kind":"contains","path":"README.md","text":"dashboard"}]}
```

验收也支持 `{"kind":"command","command":"python -m unittest discover"}`。它在临时项目副本执行并保存实际退出码。任务状态表示执行如何结束，检查结果表示交付满足了哪些条件。查看本次离线验收与历史真机运行的证据见 [Mid_Test 记录](docs/mid-test-status.md)。
