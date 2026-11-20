# 已执行的独立 unittest 为何又被委派

第五轮预订系统 `studio-20261119-211722-50fe965b6e68` 有一项未核销的独立检查。开发者最初执行 `python3 tests/test_bookings.py -v 2>&1 | tail -60`，因此后继成员须独立运行完整的 `python3 tests/test_bookings.py -v`，作用域为 `4a331a6c51ef39bdb5591f97faffece28326a4a6e451fa9ab4efea0f8c371488`。复核时的文件 SHA-256 为 `f802b47430c23742494c23d86792a79c564cb8bbff15085f077d8e64701dbd01`。

release 第 6 步显式设置了宿主已经设为 1 的 `PYTHONDONTWRITEBYTECODE`，第 7 步又将标准错误合并到标准输出。两次都完整运行了原命令，退出码都是 0。旧识别器把前置赋值当成程序名，因此判为归属不明。后来 release 省略这个冗余赋值，保留 `-v`，相同文件的独立检查才获登记。unittest 默认向 stderr 写结果；流向对进程退出码和原检查作用域没有影响。

现在只在直接调用 Python 时识别这个与 `project_environment()` 相同的字面赋值。解释器、脚本相对路径、脚本选项、测试选择和工作目录仍属于命令身份。`-v` 在这份 unittest 中控制输出详细程度，宿主继续区分带与不带该选项的两种调用。待复核提示保留原始 `-v`；旧记录只含相对路径时，须从候选命令重新计算出记录的原始作用域才能提供重放。其他环境变量、不同取值、切换目录、管道过滤和未识别的退出码包装不会因此获得完整套件的回执。

从公开交付复制冻结文件，在独立临时工作区使用正式 `run_shell` 和成员进度账本重放。第 6 步得到 stdout 0 字节与 stderr 3316 字节，第 7 步得到 stdout 3316 字节与 stderr 0 字节。两次分别登记 `unittest · 16 tests OK` 并核销原作用域。普通群聊回归另行验证，一次独立成员调用即可关闭同样的待办。可运行 `python -m unittest tests.test_closing_peer_checks tests.test_studio_peer_checks`。

CPython 的初始化代码将该环境变量解释为禁写字节码，并有环境标志的回归测试；unittest 解析 `-v` 时设置详细程度，默认用 stderr 输出。本改动依据这些语义和 Studio 的固定进程环境，没有引入上游运行时依赖。[CPython 配置源码](https://github.com/python/cpython/blob/v3.14.0/Python/initconfig.c#L1848-L1852) · [环境标志测试](https://github.com/python/cpython/blob/v3.14.0/Lib/test/test_cmd_line.py#L637-L663) · [unittest 选项解析](https://github.com/python/cpython/blob/v3.14.0/Lib/unittest/main.py#L160-L171) · [默认输出流](https://github.com/python/cpython/blob/v3.14.0/Lib/unittest/runner.py#L192-L201)
