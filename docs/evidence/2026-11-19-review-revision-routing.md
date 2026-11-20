# 已签产品返工与评审附件发布

## 基金组原运行

运行 `studio-20261119-175926-2afc79571582` 中，基金经理发布了 `semiconductor.html`，文章摘要以 `83743bab` 开头。风控任务 `task-e35537284f594cd6a4c3c5a578bb56a7` 在第 29 步对候选清单版本 `5ad0bff9…` 成功提交正式审查。必需项 I-01 指出产品文字需要修改，归因为 `product`。R-01 的结论为 `inconclusive/verification`，要求复核一条自建探针。风控明确列出了作者需要修改的位置。

风控工作副本中的一份网页归档触发凭据扫描，整树发布被拒。当时的成员报告记录了完整的 `changes_requested`，同时将流程状态写为 `blocked`。负责人第 5 步的 `delegate` 因此返回失败。原运行中没有作者返工和新版风控签署事件，最终状态为 `failed`。本节依据该运行 `trace.jsonl` 的工具回执、`public-room.jsonl` 的分工和成员报告，以及 `studio.json` 的终态。

## 返工与重新签署

1. 宿主核对正式签署的目标、评审角色、清单 SHA-256、必需项及归因。带有必需产品缺陷的 `changes_requested` 进入 `repair`，交给原作者。其他需核查的问题保留在原签署中。成员后续文字交接的 `[BLOCKED]` 可以说明无法发布的部分。
2. 若公开候选的评审副本未能发布，宿主再次从公开工作区验证签署对应的候选字节。确认匹配后，派工结果返回 `review_action`，包含作者、原评审角色、候选版本和必修改项。成员报告分别保留 `published: false`、原始发布问题和签署。存在风险的私有附件继续留在交接边界内。
3. 作者提交新版本后，原评审角色收到同一组标准和此前约定的报告路径。例如风控仍需交付 `risk_review.md`，即使下一次委派省略了文件名。新版全部必需项需重新取得证据。旧清单签署和只提交部分新结论都会被拒绝。若签署后的候选字节发生变化，宿主会指出快照差异，过期判断不能驱动本轮返工。

## 实现依据

LangGraph 的 `Branch._finish` 将节点返回的结构化结果解析为后继节点，并对非法目的地抛出异常。对应的回归用例实际执行改写节点，再由条件函数决定分析节点。Studio 借鉴了由状态决定下一位执行者的机制，将合法的产品退件作为审查结果，同时保留快照验证、私有附件发布及取消各自的失败边界。本次复用了 Studio 既有的候选摘要、`Review.apply` 和独立签署实现。[上游分支结果与异常处理](https://github.com/langchain-ai/langgraph/blob/84ced603d3a201ab0f9379936bb35868e243f3f3/libs/langgraph/langgraph/graph/_branch.py#L192-L218) · [上游条件路由回归](https://github.com/langchain-ai/langgraph/blob/84ced603d3a201ab0f9379936bb35868e243f3f3/libs/langgraph/tests/test_pregel.py#L2924-L2948)

## 验证

`python -m unittest tests.test_review_revision_routing` 从 Studio 普通群聊入口覆盖两种交接。一次由来源缓存的凭据检测拒绝整树发布，另一次由风控在实际签署后主动报告阻塞。两者都按作者、风控、作者、风控的次序运行，公开工作区收到修订产品和新版报告，问题缓存没有被发布。独立负例在签署同批次改变评审副本，验证该签署不能成为对当前候选有效的返工指令。

这些是本机平台回归。上面列出的真实运行仍保留原来的失败结果。

## 实际成稿与来源之间的核对

11 月 19 日的新一轮风控发现，同一张图把不同期间的增速并列成了上半年实绩。资料文件和来源链接都存在。冻结版本的来源评审现在提示评审员先选影响结论的断言，标明成稿位置，查原始发布者的数字、期间、单位与当时已发布的版本。推算值从原始端点复算，政策事件区分公告与生效。正文矛盾沿用已有的产品退修，证据不足沿用已有的未决状态。取证方法由评审者按实际材料选择。

Anthropic 的投资演示质检 skill 要求沿页码抽取数字，统一单位和期间并复算增长率。其脚本的 `extract_numbers` 保留数字附近的文字与所在页，`find_inconsistencies` 标记同指标的潜在差异；缺失输入文件会以非零状态退出。Studio 在来源评审提示中采用这项工作方法，由评审者取原文并判断；外部事实、HTML 表现与数字含义仍分别核实。本仓库的 `test_source_review_requires_both_candidate_and_independently_fetched_source` 通过正常群聊入口检查指导可见，并验证签署同时引用工件与来源。[上游 skill](https://github.com/anthropics/financial-services/blob/3e32c2287dcb637c184c01cc0ad82d6af7e9bb0f/plugins/vertical-plugins/financial-analysis/skills/ib-check-deck/SKILL.md) · [上游抽取与一致性检查](https://github.com/anthropics/financial-services/blob/3e32c2287dcb637c184c01cc0ad82d6af7e9bb0f/plugins/vertical-plugins/financial-analysis/skills/ib-check-deck/scripts/extract_numbers.py#L100-L219)
