# 独立成员在交接前复跑作者的指定检查

基金组第二题第四轮的风控成员在第 4 步亲自执行了作者的脚本。第 5 步下载两份监管来源，工作区改变，之前的零退出码失效。第 28 步提交了七项通过的业务评审时，其当前回执对应风控自己写的另一个脚本。运行记录中的作者脚本 SHA-256 为 `32f146f2f6595e9106544d66d9da74a1389dd8716740981f58f3be6af97d5806`。

业务评审已签发通过、要求的交付路径齐备、预算够完成一次工具调用和交接时，宿主检查该成员是否还欠本次评审对象一项独立复跑。原作者及候选的最后编辑者不能核销。已有该 scope 的当前回执时直接结束。否则模型可主动调用收窄后的原 `run_shell`，或直接说明缺口。工具只接收能还原同一执行范围的单个 Python 或 Node 脚本命令及原工作目录。执行前再核对候选文件与脚本的实际摘要。每个候选版本的每个缺口只提供一次机会，检查仍由原工具执行，交接仍依原有验收逻辑判定。成员修改了其他输入后，先前的通过结果不会自动复用。

Pi 的 `createBashToolDefinition` 将运行操作交回已有执行层，并保留工作目录、超时、输出及异常状态。其 #11613 回归测试区分子进程真实退出码和输出管道延迟。这里沿用项目已注册的成员工具与执行上下文，增加的限制集中在当前候选、命令和目录；项目自己的业务验收继续独立工作。参考了该机制，未复制上游代码。[Pi 固定版本的 shell 实现](https://github.com/earendil-works/pi/blob/f6d0c2f9b6ffbcb00c5da52e7e88570539c78bde/packages/coding-agent/src/core/tools/bash.ts) · [Pi 超时与退出码回归](https://github.com/earendil-works/pi/blob/f6d0c2f9b6ffbcb00c5da52e7e88570539c78bde/packages/coding-agent/test/suite/regressions/11613-bash-timeout-after-shell-exit.test.ts)

可运行 `python -m unittest tests.test_closing_peer_checks`。普通 Studio 入口用例先让独立评审执行作者脚本，再通过写报告使该回执失效。提交业务评审后主动复跑可核销作者的检查，换成其他命令则不会执行。单元用例另覆盖脚本版本变化、工作目录、额外参数、重复调用与实际执行前变更文件。
