"""Redis Streams 事件流：后台档的跨进程增量通道，SSE 用阻塞读取并支持断点续传。

落库的事件仍以 run_events 为准，Streams 只承载运行期间的即时增量。后台运行时
API 进程与 Worker 进程不在一起，进程内总线无法送达，因此文本增量合并后写入
Streams，订阅端按 Last-Event-ID 接着读。Redis 中事件流过期后，订阅端回落到
run_events 回放，最终结果不受影响。
"""

from __future__ import annotations

import json
import time
from typing import Any

from redis.exceptions import RedisError

from app.runtime.events import Event

# 事件流的最大长度与保留时间；长回答只保最近一段增量，完整进度由 run_events 提供。
MAX_LENGTH = 2000
TTL_SECONDS = 3600
# 合并窗口：短时间内的多次增量合并为一条，连续输出的模型不必每个 token 写一次 Redis。
MERGE_SECONDS = 0.15
# 阻塞读取的等待上限；到期返回心跳，不产生业务序号。
BLOCK_MILLISECONDS = 15000


def key(run_id: str) -> str:
    """运行的事件流键；按运行隔离，删除运行后随 TTL 过期。"""
    return f"run:stream:{run_id}"


def cursor_key(run_id: str) -> str:
    """运行内非落库事件的序号游标；Redis Streams 要求 ID 单调递增。"""
    return f"run:stream:cursor:{run_id}"


def parse_id(value: str | None) -> str:
    """把 Last-Event-ID 解析为 Streams 使用的 ID；无法解析时从头读取。

    落库事件的 ID 为「序号-0」，与 SSE 的 id 字段一致，因此浏览器断线重连
    可以直接把 id 回传，不需要服务端保存订阅状态。
    """
    text = (value or "").strip()
    if not text:
        return "0-0"
    parts = text.split("-")
    if len(parts) == 1 and parts[0].isdigit():
        return f"{parts[0]}-0"
    if len(parts) == 2 and all(part.isdigit() for part in parts):
        return text
    return "0-0"


class StreamLog:
    """运行事件流；发布失败不阻断执行，订阅端会退回到 run_events 回放。

    连接来自资源容器共享的 Redis 客户端，因此不需要额外的连接生命周期管理。
    读写失败只记录并降级，不把 Redis 故障升级成运行失败。
    """

    def __init__(self, resources):
        # 容器构造阶段还没有连接，Redis 客户端在进入 async with 时才建立，
        # 因此这里只保存容器引用，连接在使用时再取。
        self.resources = resources
        self.logger = resources.logger
        # 合并状态：按运行累计待写入的增量，减少并发写入次数。
        self.pending: dict[str, dict[str, Any]] = {}

    @property
    def redis(self):
        """资源容器中共享的 Redis 客户端；延迟取得，避免构造顺序依赖。"""
        return self.resources.redis

    async def seed(self, run_id: str, sequence: int) -> None:
        """把游标抬到已知的持久序号；运行开始时调用。

        不落库的增量事件不带序号，只能接在游标之上编号。若游标仍为 0，执行早期的
        增量会拿到 `0-1` 这样小于持久序号 `1-0` 的 ID，按 Last-Event-ID 续传的订阅
        端会把它们全部跳过。因此在运行开始与每次落库时都抬一次游标。
        """
        try:
            current = int(await self.redis.get(cursor_key(run_id)) or 0)
            if sequence > current:
                await self.redis.set(cursor_key(run_id), sequence)
                await self.redis.expire(cursor_key(run_id), TTL_SECONDS)
        except (RedisError, OSError) as error:
            self.logger.warning(
                "事件流游标推进失败，增量事件序号可能回退",
                run_id=run_id,
                exception_type=type(error).__name__,
            )

    async def append(self, run_id: str, event: Event) -> None:
        """写入一条事件；文本增量在合并窗口内累积，其余事件立即写入。

        合并只影响传输粒度，不改变事件语义：合并后仍是一条 message.delta，
        携带累计正文，订阅端按顺序拼接即可。
        """
        if event.name == "message.delta":
            self._merge(run_id, event)
            # 窗口到期才真正落盘；连续 token 因此按约 150 毫秒一批写入。
            await self.collect(run_id)
            return
        # 落库事件之前先把待发增量写出，避免增量落到它后面造成顺序颠倒。
        await self.flush(run_id, force=True)
        await self._write(run_id, event.name, event.payload, event.sequence)

    def _merge(self, run_id: str, event: Event) -> None:
        """累积增量正文；首次增量记录到达时间，由 collect 判断是否已经可以写入。"""
        pending = self.pending.get(run_id)
        content = event.payload.get("content") or ""
        if pending is None:
            self.pending[run_id] = {"content": content, "at": time.monotonic()}
            return
        pending["content"] += content

    async def collect(self, run_id: str, *, force: bool = False) -> None:
        """把超过合并窗口的增量写入；未到期且未强制时保留在内存中。"""
        pending = self.pending.get(run_id)
        if pending is None:
            return
        if not force and time.monotonic() - pending["at"] < MERGE_SECONDS:
            return
        self.pending.pop(run_id, None)
        await self._write(run_id, "message.delta", {"content": pending["content"]}, None)

    async def flush(self, run_id: str, *, force: bool = False) -> None:
        """写出该运行的待发增量；运行结束前必须强制写出，避免丢失末尾正文。"""
        await self.collect(run_id, force=force)

    async def _next_id(self, run_id: str, sequence: int | None) -> str:
        """分配单调递增的事件 ID；落库事件使用持久序号，增量事件接在它之后。

        游标记录该运行已用到的最大序号：落库事件把游标推进到自己的序号，增量事件
        在游标之上递增。否则执行早期的增量会拿到 `0-1` 这样小于持久序号的 ID，
        断线重连按 Last-Event-ID 续传时会被跳过。
        """
        cursor = int(await self.redis.get(cursor_key(run_id)) or 0)
        if sequence is not None and sequence > cursor:
            await self.redis.set(cursor_key(run_id), sequence)
            cursor = sequence
        counter = await self.redis.incr(cursor_key(run_id))
        base = sequence if sequence is not None else cursor
        return f"{base}-{counter}"

    async def _write(self, run_id: str, name: str, payload: dict[str, Any], sequence) -> None:
        """写入单条事件；任何 Redis 故障都只降级，不向执行链路抛出。"""
        try:
            identifier = await self._next_id(run_id, sequence)
            await self.redis.xadd(
                key(run_id),
                {"kind": name, "payload": json.dumps(payload, ensure_ascii=False, default=str)},
                id=identifier,
                maxlen=MAX_LENGTH,
                approximate=True,
            )
            await self.redis.expire(key(run_id), TTL_SECONDS)
        except (RedisError, OSError) as error:
            self.logger.warning(
                "事件流写入失败，订阅端将回落到 run_events",
                run_id=run_id,
                exception_type=type(error).__name__,
            )

    async def close(self, run_id: str) -> None:
        """运行结束：写出剩余增量并给事件流设置过期时间，Redis 不可用时静默降级。"""
        try:
            await self.flush(run_id, force=True)
            await self.redis.expire(key(run_id), TTL_SECONDS)
            await self.redis.expire(cursor_key(run_id), TTL_SECONDS)
        except (RedisError, OSError):
            pass
        self.pending.pop(run_id, None)

    async def read(
        self, run_id: str, last_id: str, *, count: int = 200, block: bool = True
    ) -> tuple[list[dict[str, Any]], str] | None:
        """阻塞读取增量；返回事件与新的游标，超时返回 None，Redis 不可用返回 None。

        调用方在收到 None 时发送心跳并检查运行是否已结束；运行结束后不再重复阻塞。
        """
        try:
            response = await self.redis.xread(
                {key(run_id): last_id},
                count=count,
                block=BLOCK_MILLISECONDS if block else None,
            )
        except (RedisError, OSError) as error:
            self.logger.warning(
                "事件流读取失败，改为回放 run_events",
                run_id=run_id,
                exception_type=type(error).__name__,
            )
            return None
        events: list[dict[str, Any]] = []
        cursor = last_id
        for _, frames in response or []:
            for identifier, fields in frames:
                cursor = identifier
                try:
                    payload = json.loads(fields.get("payload") or "{}")
                except ValueError:
                    payload = {}
                events.append(
                    {"id": identifier, "kind": fields.get("kind", "custom"), "payload": payload}
                )
        return events, cursor
