"""事件协议：运行期间对外推送与落库的事件类型，以及两者的对应关系。

增量事件只推送不落库，关键状态才写入 run_events。这样长回答不会按 token 打满数据库，
同时断线重连仍能通过关键事件还原运行进度。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# 对外事件名；SSE 的 event 字段即取这里的值。
RUN_STATUS = "run.status"
MESSAGE_DELTA = "message.delta"
TOOL_STARTED = "tool.started"
TOOL_COMPLETED = "tool.completed"
INTERRUPT = "interrupt"
CUSTOM = "custom"
ERROR = "error"
RUN_COMPLETED = "run.completed"

# 需要写入 run_events 的事件；其余只推送。
PERSISTED = frozenset({RUN_STATUS, TOOL_STARTED, TOOL_COMPLETED, INTERRUPT, ERROR, RUN_COMPLETED})

# 内部记录用的种类到对外事件名的映射。
# status 同时承载排队、运行中与终态，按 payload 中的 status 再细分。
MAPPING: dict[str, str] = {
    "status": RUN_STATUS,
    "text_delta": MESSAGE_DELTA,
    "tool_delta": CUSTOM,
    "tool_result": TOOL_COMPLETED,
    "node": CUSTOM,
    "custom": CUSTOM,
}


@dataclass(frozen=True)
class Event:
    """一条事件；sequence 为落库后分配的序号，仅推送的事件为 None。"""

    name: str
    payload: dict[str, Any]
    sequence: int | None = None
    persisted: bool = False


def canonical(kind: str, payload: dict[str, Any]) -> tuple[str, bool]:
    """把内部记录种类映射为对外事件名，并给出是否需要落库。

    status 需要区分中间态与终态：queued、running、waiting_input 落库，
    其余终态由 run.completed 表达，但仍保留 status 记录以便回放。
    """
    name = MAPPING.get(kind, CUSTOM)
    if kind == "status" and payload.get("status") in {"succeeded", "failed", "cancelled"}:
        return RUN_STATUS, True
    return name, name in PERSISTED or kind == "status"


# 运行状态取值；对外统一使用这些字符串。
STATUSES: tuple[str, ...] = (
    "queued",
    "running",
    "waiting_input",
    "succeeded",
    "failed",
    "cancelled",
)

Status = Literal["queued", "running", "waiting_input", "succeeded", "failed", "cancelled"]

# 终态集合；判定运行是否结束时统一使用。
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
