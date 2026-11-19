# 争议子评审收尾时完成私稿指定检查

T2-04 的风险评审私稿除了原命名反证，还保留自己修订脚本的 `pending_verification`。这份私稿尚未公开，因此它不在公开成员检查的 `pending_check_reviews` 中。第三方完成原 source 判断后，私稿发布检查会正确要求独立运行当前脚本；但在收尾阶段，原先按 source 标准开放的工具没有 shell，无法完成这项已存在的义务。

现在仅当 `reviewer_probe` 冻结私稿的逐项 passed 结果已满足原证据约束、且确实被发布检查拒绝后，宿主将该私稿中仍未完成的指定检查交给既有 `ClosingPeerChecks`。命令必须能还原原 scope，仅允许单个已绑定的 Python/Node 脚本与原目录；候选和脚本摘要在展示与实际调用前都重新核对。每个候选的每条义务只提供一次执行机会。模型可以执行或交接阻塞，没有自动重放，其他 source 评审不会因此获得命令权限。

执行后仍使用既有收尾工具提交原标准。该次调用之前的 rejected submission 不是有效签署。没有真实当前回执、换命令、改文件、预算不足或脚本依然失败，都不能通过原发布检查。该补充不清除 pending，也不改变来源或用户验收。

实现复用项目现有 `ClosingPeerChecks`，它参考了 Pi 将具体命令交给既有执行层并保留真实退出/异常状态的机制。新增的只是未公开私稿义务的合法派工时机及重新提交步骤。[Pi shell 实现](https://github.com/earendil-works/pi/blob/f6d0c2f9b6ffbcb00c5da52e7e88570539c78bde/packages/coding-agent/src/core/tools/bash.ts#L390-L436) · [实际退出与超时回归](https://github.com/earendil-works/pi/blob/f6d0c2f9b6ffbcb00c5da52e7e88570539c78bde/packages/coding-agent/test/suite/regressions/11613-bash-timeout-after-shell-exit.test.ts) · [普通 Studio 入口回归](../../tests/test_private_probe_closing.py)。验证使用脚本模型和模拟来源，不代表原题交付已由第三方重新验收。
