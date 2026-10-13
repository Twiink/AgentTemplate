"""请求内事件总线：minimal 档不依赖 Redis，增量事件直接在进程内推送给当前订阅者。

总线只服务于同一进程内的订阅，用于请求内流式输出；跨进程的续传仍以 run_events
为准。总线保留最近的事件，使订阅稍晚建立时也能补齐已经发生的关键事件。
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.runtime.events import Event


class EventBus:
    """单个运行的事件总线；发布、订阅与关闭都在同一事件循环内完成。"""

    def __init__(self, history: int = 200):
        self.history = history
        self._events: list[Event] = []
        self._subscribers: list[asyncio.Queue] = []
        self._closed = asyncio.Event()

    def publish(self, event: Event) -> None:
        """记录并广播事件；已关闭的总线不再接受新事件。"""
        if self._closed.is_set():
            return
        self._events.append(event)
        if len(self._events) > self.history:
            del self._events[: len(self._events) - self.history]
        for queue in self._subscribers:
            queue.put_nowait(event)

    def subscribe(self) -> asyncio.Queue:
        """建立订阅并回放已记录的事件，使晚到的订阅者不丢关键进度。"""
        queue: asyncio.Queue = asyncio.Queue()
        for event in self._events:
            queue.put_nowait(event)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """移除订阅；订阅者提前退出时调用，避免队列常驻。"""
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    @property
    def closed(self) -> bool:
        """总线是否已结束；结束表示不会再有新事件。"""
        return self._closed.is_set()

    def close(self) -> None:
        """结束总线并唤醒全部订阅者，使它们能正常退出而不是永久等待。"""
        self._closed.set()
        for queue in self._subscribers:
            queue.put_nowait(None)


class BusRegistry:
    """按运行 ID 管理总线；运行开始建立，结束后移除。"""

    def __init__(self):
        self._items: dict[str, EventBus] = {}

    def open(self, run_id: str) -> EventBus:
        """为运行建立总线；重复建立时复用已有实例。"""
        bus = self._items.get(run_id)
        if bus is None:
            bus = EventBus()
            self._items[run_id] = bus
        return bus

    def get(self, run_id: str) -> EventBus | None:
        """取得运行的总线；不在本进程执行时返回 None。"""
        return self._items.get(run_id)

    def close(self, run_id: str) -> None:
        """结束并移除总线。"""
        bus = self._items.pop(run_id, None)
        if bus is not None:
            bus.close()

    def publish(self, run_id: str, event: Event) -> None:
        """向运行的总线发布事件；没有订阅者时代价为一次字典查找。"""
        bus = self._items.get(run_id)
        if bus is not None:
            bus.publish(event)


def sse(event: Event) -> str:
    """把事件格式化为 SSE 帧；单行 JSON 防止正文换行破坏格式。"""
    import json

    lines = []
    if event.sequence is not None:
        lines.append(f"id: {event.sequence}")
    lines.append(f"event: {event.name}")
    lines.append("data: " + json.dumps(event.payload, ensure_ascii=False, default=str))
    return "\n".join(lines) + "\n\n"


__all__ = ["BusRegistry", "EventBus", "sse", "Any"]
