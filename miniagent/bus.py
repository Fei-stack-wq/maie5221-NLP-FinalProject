"""消息总线：agent 之间 / agent 与人之间的共享频道（课程 2.1a 群聊与私信）。

它解决三件事：
1. **群聊/私信**：消息有 ``sender / recipients / kind``，可以按 agent 或频道订阅。
2. **事件驱动调度（S3）**：子任务完成/失败以消息形式广播，调度器据此解锁下游，
   而不是"整层跑完再进入下一层"。
3. **审计**：所有消息落 ``bus.jsonl``，报告里可以直接引用"谁在什么时候说了什么"。

设计取舍：**进程内 pub/sub + 文件落盘**，不做网络传输（单机课程项目）。
消息 id 单调递增，用来给并发线程的事件排一个确定的序。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field


class Message(BaseModel):
    id: int = 0
    ts: float = 0.0
    iso: str = ""
    sender: str
    recipients: list[str] = Field(default_factory=lambda: ["all"])
    kind: str = "report"
    """chat / report / delegate_request / delegate_result / system"""

    content: str = ""
    task_id: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    def addresses(self, topic: str) -> bool:
        return topic in self.recipients or "all" in self.recipients or "*" in self.recipients

    def matches(self, topic: str) -> bool:
        """订阅主题是否命中本条消息。``*``/``all`` 作为订阅主题表示"全都收"。"""
        return topic in {"*", "all"} or self.addresses(topic)


Handler = Callable[[Message], None]


class MessageBus:
    """线程安全的发布/订阅。订阅者按 topic 匹配 ``recipients``（含 all / *）。"""

    def __init__(self, run_dir: Path | str | None = None) -> None:
        self._subs: list[tuple[str, Handler]] = []
        self._history: list[Message] = []
        self._seq = 0
        self._lock = threading.Lock()
        self._path: Path | None = None
        if run_dir is not None:
            directory = Path(run_dir)
            directory.mkdir(parents=True, exist_ok=True)
            self._path = directory / "bus.jsonl"

    # ------------------------------------------------------------------ 订阅
    def subscribe(self, topic: str, handler: Handler) -> None:
        with self._lock:
            self._subs.append((topic, handler))

    # ------------------------------------------------------------------ 发布
    def publish(self, message: Message) -> Message:
        with self._lock:
            self._seq += 1
            message.id = self._seq
            message.ts = round(time.time(), 4)
            message.iso = datetime.now().isoformat(timespec="milliseconds")
            self._history.append(message)
            line = json.dumps(message.model_dump(), ensure_ascii=False) + "\n"
            if self._path is not None:
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
            targets = [
                handler for topic, handler in self._subs if message.matches(topic)
            ]
        # 回调放在锁外：订阅者可能很慢，甚至自己发消息
        for handler in targets:
            try:
                handler(message)
            except Exception:  # 订阅者出错不能影响发布者
                continue
        return message

    def post(
        self,
        sender: str,
        content: str,
        *,
        kind: str = "chat",
        recipients: list[str] | None = None,
        task_id: str | None = None,
        **meta: Any,
    ) -> Message:
        return self.publish(
            Message(
                sender=sender,
                recipients=recipients or ["all"],
                kind=kind,
                content=content,
                task_id=task_id,
                meta=meta,
            )
        )

    # ------------------------------------------------------------------ 读取
    def channel(self, topic: str | None = None, limit: int | None = None) -> list[Message]:
        with self._lock:
            messages = list(self._history)
        if topic is not None:
            messages = [message for message in messages if message.matches(topic)]
        return messages[-limit:] if limit else messages

    def transcript(self, limit: int = 12) -> str:
        """给 CLI/报告用的一行一条摘要。"""
        rows: list[str] = []
        for message in self.channel(limit=limit):
            task = f" ({message.task_id})" if message.task_id else ""
            rows.append(f"[{message.kind}] {message.sender}{task}: {message.content[:120]}")
        return "\n".join(rows)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._history)

    def close(self) -> None:
        """预留：将来换成外部队列时在这里断开连接。"""
        return None


__all__ = ["Message", "MessageBus"]
