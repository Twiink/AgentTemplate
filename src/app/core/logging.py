"""结构化日志：structlog 与标准库共用一条处理链，自动附加请求与链路上下文，写出前统一脱敏。"""

from __future__ import annotations

import logging
import re
import sys
import threading
import time
from collections.abc import Iterable, MutableMapping
from typing import Any

import structlog
from opentelemetry import trace

from app.core.config import Settings

MASK = "[已脱敏]"

# 字段名本身表示凭据时整体替换；input_tokens、token_budget 这类计数字段不会命中。
SENSITIVE_KEY = re.compile(
    r"(?i)(^|_)(authorization|password|passwd|secret|cookie|credential|dsn"
    r"|api_?key|access_?token|refresh_?token|token)$"
)

# 内置值规则始终生效：连接串中的密码、Bearer 凭据和常见服务商密钥格式。
BUILTIN_RULES = [
    (re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@]+:)[^\s@]+@"), r"\1" + MASK + "@"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1" + MASK),
    (re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{16,}"), MASK),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), MASK),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), MASK),
]

# 第三方库的请求级日志会带完整 URL，部分服务商把密钥放在查询参数里，只保留警告以上级别。
NOISY_LOGGERS = ("httpx", "httpcore", "openai", "anthropic", "urllib3")

_configured: tuple[Any, ...] | None = None
_configure_lock = threading.Lock()


class Redactor:
    """按字段名和正则规则脱敏；递归处理嵌套字典、列表以及已格式化的异常堆栈文本。"""

    def __init__(self, patterns: Iterable[str] = ()):
        self.rules = [*BUILTIN_RULES, *((re.compile(pattern), MASK) for pattern in patterns)]

    def text(self, value: str) -> str:
        """依次应用全部规则，返回脱敏后的文本。"""
        for pattern, replacement in self.rules:
            value = pattern.sub(replacement, value)
        return value

    def value(self, value: Any, depth: int = 0) -> Any:
        """脱敏任意日志字段值；嵌套过深的结构原样保留，避免异常数据拖慢日志写出。"""
        if isinstance(value, str):
            return self.text(value)
        if depth >= 6:
            return value
        if isinstance(value, dict):
            return {
                key: MASK
                if isinstance(key, str) and SENSITIVE_KEY.search(key)
                else self.value(item, depth + 1)
                for key, item in value.items()
            }
        if isinstance(value, list | tuple):
            return [self.value(item, depth + 1) for item in value]
        return value

    def __call__(
        self, logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
    ) -> dict[str, Any]:
        """structlog 处理器入口；必须位于异常格式化之后、渲染之前，堆栈文本才会被脱敏。"""
        return self.value(dict(event_dict))


def add_trace_context(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """写入当前 OTel span 的 trace_id 与 span_id，日志可以直接对照链路追踪。"""
    context = trace.get_current_span().get_span_context()
    if context.is_valid:
        event_dict.setdefault("trace_id", format(context.trace_id, "032x"))
        event_dict.setdefault("span_id", format(context.span_id, "016x"))
    return event_dict


def resolve_format(settings: Settings) -> str:
    """auto 在 local 环境输出便于阅读的控制台格式，其余环境输出便于采集的 JSON。"""
    if settings.log_format != "auto":
        return settings.log_format
    return "console" if settings.environment == "local" else "json"


def setup_logging(settings: Settings) -> Any:
    """按配置初始化进程日志并返回应用 logger；配置未变化时重复调用直接返回，不重建处理链。"""
    global _configured
    output = resolve_format(settings)
    fingerprint = (settings.log_level, output, tuple(settings.redaction_patterns))
    with _configure_lock:
        if _configured != fingerprint:
            _configure(settings.log_level, output, settings.redaction_patterns)
            _configured = fingerprint
    return structlog.get_logger(settings.app_name)


def _configure(level: str, output: str, patterns: list[str]) -> None:
    """安装统一处理链；标准库日志经 ProcessorFormatter 进入同一套脱敏和渲染流程。"""
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        add_trace_context,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    renderer = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if output == "json"
        else structlog.dev.ConsoleRenderer(
            colors=sys.stdout.isatty(), exception_formatter=structlog.dev.plain_traceback
        )
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            # 先把异常转成文本再脱敏；直接交给渲染器会绕过脱敏规则。
            structlog.processors.format_exc_info,
            Redactor(patterns),
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # uvicorn 自带处理器会绕过统一格式；访问日志由应用中间件记录，关闭重复的访问日志。
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers = []
    access.propagate = False
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(
            max(logging.WARNING, logging.getLevelNamesMapping()[level])
        )
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )


class Throttle:
    """按键限制同类告警的输出频率；依赖故障期间不会每个请求都写一条相同的日志。"""

    def __init__(self, interval: float = 60):
        self.interval = interval
        self.last: dict[str, float] = {}
        self.lock = threading.Lock()

    def ready(self, key: str) -> bool:
        """距离上次输出超过间隔时返回 True，并记录本次输出时间。"""
        now = time.monotonic()
        with self.lock:
            if now - self.last.get(key, float("-inf")) < self.interval:
                return False
            self.last[key] = now
            return True
