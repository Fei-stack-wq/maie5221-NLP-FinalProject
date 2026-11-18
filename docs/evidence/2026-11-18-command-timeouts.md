# 前台测试、提前返回和进程回收

会议室预订任务 `studio-20261118-045023-7355b3cdc398` 的首位开发者在第 18 步运行 `python3 test_api.py`，宿主记录 `A_TOOL_TIMEOUT`、20 秒时限和退出码 `-9`。原实现把未指定的时限和显式申请的时限都夹在同一个 20 秒上限内。代码当时已有管道输出截取、进程组和成员交接清理；工具注册表里没有用于接续测试进程并领取其退出码的操作。

## 在 Studio 运行

- `run_shell`、`run_python` 和 `run_browser` 默认等候 120 秒。传 `timeout` 可指定本次执行秒数，最大值来自部署的 `Limits.tool_timeout_seconds`，默认为 600 秒。宿主还取任务、成员剩余墙钟和当步保留的收尾时间所允许的较小值。检索网络请求继续使用原来的 15 秒上限，文件搜索维持至多 20 秒。
- 比如 `run_shell(command="python3 test_api.py", timeout=180)` 在命令退出后直接给出真实退出码、用时、限时、裁剪标记和两个输出流的字节数。单次提交即发起一次执行。
- POSIX 的 Agent 可以在 `run_shell` 传 `yield_after_seconds`。以 `0` 立即返回或以一个正数先等候；如果尚在运行，宿主给 `process_id`，此时还没有退出码。用 `poll_process(process_id, wait_seconds=10)` 领取输出，`wait_seconds=0` 可立即查询。记录中的最终 `process_state` 为 `exited`、`timed_out` 或 `cancelled`。其他分工不能使用这一句柄；终态只能领取一次。未实际启动的命令无法出具退出码。
- 每个 Agent 作用域最多同时保留四个待领取结果。提前返回与轮询都不会刷新命令的执行期限。任务取消或到达宿主执行期限时，POSIX runner 清理进程树。成员离开作用域前回收未结命令，主动宣告完成时必须先领取待结结果。文字收尾仍遵守原始步数和时间预算。
- `run_shell`、`poll_process` 和浏览器使用 POSIX 进程组。Windows 原生 `run_python` 维持同步执行，并按新的执行时限及任务剩余时间截断。

## 上游实现

Pi 的 `resolveTimeoutMs` 单独处理省略超时和显式有限超时，运行器分别监听用户中止和限时，并保留命令已经退出而后代仍持有输出管道的事实。这个项目额外施加任务时限和有界输出。[Pi Bash 工具](https://github.com/earendil-works/pi/blob/f6d0c2f9b6ffbcb00c5da52e7e88570539c78bde/packages/coding-agent/src/core/tools/bash.ts#L26-L41) · [Pi 超时与进程退出回归](https://github.com/earendil-works/pi/blob/f6d0c2f9b6ffbcb00c5da52e7e88570539c78bde/packages/coding-agent/test/suite/regressions/11613-bash-timeout-after-shell-exit.test.ts#L16-L49)

Codex 把启动命令后等待的时间和进程本身的寿命分开。命令可以先返回会话编号，后续查询取回终态与退出码，并释放会话。Studio 复用了既有前台 runner、成员进程组和清理作用域，用同一套真正的命令结果支持同步或提前返回；未引入终端输入、TTY 或上游依赖。[Codex shell 接口](https://github.com/openai/codex/blob/29cb67623c6c4360b29d09e66010ac71cef1a693/codex-rs/core/src/tools/handlers/shell_spec.rs#L30-L45) · [启动并登记进程](https://github.com/openai/codex/blob/29cb67623c6c4360b29d09e66010ac71cef1a693/codex-rs/core/src/tools/handlers/unified_exec/exec_command.rs#L218-L245) · [进程生命周期管理](https://github.com/openai/codex/blob/29cb67623c6c4360b29d09e66010ac71cef1a693/codex-rs/core/src/unified_exec/process_manager.rs) · [终态和会话回收测试](https://github.com/openai/codex/blob/29cb67623c6c4360b29d09e66010ac71cef1a693/codex-rs/core/tests/suite/unified_exec.rs#L2381-L2424)

## 验证

在临时目录复制首位开发者冻结的工作副本后，原命令实际运行 1.848 秒并通过 132 项。为稳定复现旧阈值，在同一测试前显式等候 21 秒，`sleep 21 && python3 test_api.py` 在新 runner 中实际耗时 22.921 秒，退出码 0，汇总 132 项通过、0 项失败。这是隔离的工具回归，未重跑 DeepSeek。历史 SIGKILL 并不说明此测试套件每次运行都需 20 秒以上。

`tests/test_shell_sessions.py` 覆盖默认时限、显式时限、部署上限、真实任务/收尾时间、关闭输出管道后的取消、超时清理、句柄隔离、非零退出码、未领取结果与防止提前宣告成功。`tests/test_shell_tool.py` 和 `tests/test_browser_tool.py` 继续覆盖有界输出、环境、子进程回收和真实浏览器执行。
