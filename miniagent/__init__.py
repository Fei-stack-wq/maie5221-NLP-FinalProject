"""miniagent —— 一个"看得见每个环节"的多智能体最小骨架。

设计原则
--------
1. **零魔法**：不依赖任何 agent 框架，所有机制（循环、工具、沙箱、指标）都是你能读完的代码。
2. **零外网也能跑**：自带 ScriptedProvider，不需要 API key、不联网，随时可复现。
3. **每个文件对应课程要求的一项**：
   - ``sandbox.py``  -> 要求 2.2 隔离环境
   - ``tools.py``    -> 要求 2.3 行动空间（搜索/文件/代码）
   - ``agent.py``    -> 要求 2.1 通信与委派的原子单元
   - ``metrics.py``  -> 评估指标（任务完成率 / 执行时间）
   - ``providers.py``-> 要求 2.6 标准化 LLM 主干（DeepSeek）

下一步该往哪长
--------------
单 agent 跑通后，按 README 的"扩展路线"依次加：多 agent 群聊 -> 任务分解与委派
-> per-agent 记忆与演化 -> HITL 闸门 -> 批量评测。不要一次性全上。
"""

from .config import Limits, Settings
from .rao import (
    Acceptance,
    Action,
    Budget,
    Decision,
    ErrorClass,
    Observation,
    RAOState,
    RAOUnit,
    StepRecord,
    accept_files,
    evaluate_acceptance,
)
from .schemas import LLMResponse, RunResult, ToolCall, ToolResult, ToolSpec, Usage

__version__ = "0.2.0"

__all__ = [
    "Limits",
    "Settings",
    "LLMResponse",
    "RunResult",
    "ToolCall",
    "ToolResult",
    "ToolSpec",
    "Usage",
    # RAO 原子（v0.2）
    "Acceptance",
    "Action",
    "Budget",
    "Decision",
    "ErrorClass",
    "Observation",
    "RAOState",
    "RAOUnit",
    "StepRecord",
    "accept_files",
    "evaluate_acceptance",
    "__version__",
]
