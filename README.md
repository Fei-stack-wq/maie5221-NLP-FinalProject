# miniagent

**一个可运行、有边界的多智能体协作系统。** 群聊里有一位负责人和若干成员，各自带角色提示词。
你在群里发布任务后，负责人会规划并把工作逐项委派给成员，再请其他成员复核。需要你决定的事会在群里问你。
最后负责人给出交付说明，文件保存在团队项目里。

```
版本 1.0.0 · Python 3.10+ · DeepSeek API · 本地网页工作室 · 离线演示
```

## 快速开始

```sh
pip install -r requirements.txt
python -m playwright install chromium        # 成员用真实浏览器检查网页时需要
cp .env.example .env                         # 填入 DEEPSEEK_API_KEY
python -m miniagent studio                   # 打开 http://127.0.0.1:8765
```

1. 点「新建群聊」，选一个 Mid_Test 场景模板（软件团队、新闻编辑部、证券基金团队），或者自己填写成员 ID、名称和职责。
2. 点「填入任务 1」或直接输入任务后发送。负责人提问时，在输入框回答即可。
3. 右上角「文件」可浏览、预览和下载团队项目。HTML 交付在独立的预览端口打开（端口号+1）。
4. 第一个任务结束后，在同一个群里发任务 2，团队会接着用已有的对话和文件。

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
| 人在回路 | `ask_human` 把问题发到群里并等你回答。任务进行中你发的消息会在成员的下一步送达。自主模式下不提问，团队自行作出并写明假设。 |
| 记忆 | 每个智能体能看到群里的历史对话（以往任务、派工、报告、问答、最终答复），也能看到项目文件、自己用 `remember` 记下的长期笔记和近期分工。 |
| 有边界 | 成员默认最多 80 步，负责人 60 步、24 次派工。几乎相同的派工最多重复 3 次。预算用尽或检测到循环时，模型会被要求不用工具写出报告，所以调用方总能拿到报告和真实状态。 |
| 上下文 | 每一步发送完整对话。超过约 11 万 token 时，先裁掉旧的大段工具输出，必要时把较早部分替换为结构化摘要（目标、约束、进展、决定、已确认事实、下一步）。系统提示和任务原文始终保留。 |
| 工具 | 文件读写和局部编辑、ripgrep 搜索、Python、Bash（可启动后台服务，结束时回收）、无头 Chromium 浏览器检查、新闻/论文检索和网页/PDF 抓取（存档在 `sources/`）。 |

设计和上游参考（OpenHands 卡死检测、Pi 上下文压缩、OpenAI Agents SDK、AutoGen Magentic-One）见 [docs/architecture.md](docs/architecture.md)。Mid_Test 实测记录见 [docs/mid-test-status.md](docs/mid-test-status.md)。

## 配置（`.env` 或环境变量，环境变量优先）

| 变量 | 默认 | 含义 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | — | 必填 |
| `DEEPSEEK_MODEL` | `deepseek-flash` | 或 `deepseek-v4-pro` |
| `DEEPSEEK_THINKING` | `disabled` | `enabled` 开启思考 |
| `MA_MAX_STEPS` | 80 | 成员每次分工的步数上限（1–1000） |
| `MA_LEAD_MAX_STEPS` | 60 | 负责人每个任务的步数上限 |
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
