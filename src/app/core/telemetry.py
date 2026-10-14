"""单一 OpenTelemetry 导出与低基数指标；正文采集只在显式脱敏配置后开放。"""

import atexit
import logging
import os
import threading

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter, Gauge, Histogram

from app.core.config import Settings
from app.core.logging import Redactor

# 多进程指标文件仅在同一 Worker 容器内共享，启动前创建目录。
if os.getenv("PROMETHEUS_MULTIPROC_DIR"):
    from pathlib import Path

    Path(os.environ["PROMETHEUS_MULTIPROC_DIR"]).mkdir(parents=True, exist_ok=True)

_providers = {}
_provider_lock = threading.Lock()
_instrumented = set()
_instrumentation_failures = {}
_instrumentation_lock = threading.Lock()

http_requests_total = Counter(
    "agent_http_requests_total", "接口请求次数", ["method", "route", "status"]
)
http_request_duration_seconds = Histogram(
    "agent_http_request_duration_seconds", "接口处理耗时", ["method", "route"]
)
llm_requests_total = Counter(
    "agent_llm_requests_total", "模型调用统计", ["model", "provider", "status"]
)
llm_request_duration_seconds = Histogram(
    "agent_llm_request_duration_seconds", "模型调用耗时", ["model", "provider"]
)
llm_time_to_first_token_seconds = Histogram(
    "agent_llm_time_to_first_token_seconds", "首 token 延迟", ["model", "provider"]
)
llm_tokens_total = Counter("agent_llm_tokens_total", "模型 Token 消耗", ["model", "type"])
llm_cost_total = Counter("agent_llm_cost_total", "模型费用", ["model"])
llm_fallbacks_total = Counter(
    "agent_llm_fallbacks_total", "模型降级次数", ["from_model", "to_model"]
)
tool_calls_total = Counter("agent_tool_calls_total", "工具调用统计", ["tool", "status"])
tool_duration_seconds = Histogram("agent_tool_duration_seconds", "工具调用耗时", ["tool"])
runs_total = Counter("agent_runs_total", "运行状态统计", ["agent", "status"])
run_duration_seconds = Histogram("agent_run_duration_seconds", "运行耗时", ["agent", "status"])
run_queue_wait_seconds = Histogram("agent_run_queue_wait_seconds", "运行排队耗时")
retrieval = Histogram("agent_retrieval_duration_seconds", "知识与记忆检索耗时")
db_pool_connections = Gauge(
    "agent_db_pool_connections", "数据库连接池状态", ["state"], multiprocess_mode="livesum"
)
queue_depth = Gauge("agent_queue_depth", "持久排队运行数量", multiprocess_mode="livemax")
active_runs = Gauge("agent_active_runs", "持久执行中运行数量", multiprocess_mode="livemax")
worker_heartbeat = Gauge(
    "agent_worker_heartbeat_timestamp", "Worker 最近任务处理时间", multiprocess_mode="livemax"
)
ratelimit_rejections = Counter("agent_ratelimit_rejections_total", "限流拒绝次数", ["route"])
ratelimit_backend_errors = Counter(
    "agent_ratelimit_backend_errors_total", "限流后端异常次数", ["route"]
)


def instrument_libraries():
    """启用 OpenTelemetry 自动插桩；每个进程只初始化一次，幂等。"""
    pid = os.getpid()
    with _instrumentation_lock:
        if pid in _instrumented:
            return _instrumentation_failures.get(pid, ())
        _instrumented.add(pid)
    failures = []

    # FastAPI 自动插桩由 main.py 中的 FastAPIInstrumentor 处理

    # httpx 插桩：LLM 调用、外部 API 请求
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except Exception as error:
        failures.append("httpx")
        logging.getLogger("agent.telemetry").warning(
            "OpenTelemetry 自动插桩初始化失败",
            extra={"component": "httpx", "exception_type": type(error).__name__},
        )

    # SQLAlchemy 插桩：数据库查询
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument()
    except Exception as error:
        failures.append("sqlalchemy")
        logging.getLogger("agent.telemetry").warning(
            "OpenTelemetry 自动插桩初始化失败",
            extra={"component": "sqlalchemy", "exception_type": type(error).__name__},
        )

    # Redis 插桩：缓存与队列
    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor

        RedisInstrumentor().instrument()
    except Exception as error:
        failures.append("redis")
        logging.getLogger("agent.telemetry").warning(
            "OpenTelemetry 自动插桩初始化失败",
            extra={"component": "redis", "exception_type": type(error).__name__},
        )

    # Celery 插桩：后台任务
    try:
        from opentelemetry.instrumentation.celery import CeleryInstrumentor

        CeleryInstrumentor().instrument()
    except Exception as error:
        failures.append("celery")
        logging.getLogger("agent.telemetry").warning(
            "OpenTelemetry 自动插桩初始化失败",
            extra={"component": "celery", "exception_type": type(error).__name__},
        )

    # LangChain 插桩：Agent 执行链路
    try:
        from openinference.instrumentation.langchain import LangChainInstrumentor

        LangChainInstrumentor().instrument()
    except Exception as error:
        failures.append("langchain")
        logging.getLogger("agent.telemetry").warning(
            "OpenTelemetry 自动插桩初始化失败",
            extra={"component": "langchain", "exception_type": type(error).__name__},
        )
    with _instrumentation_lock:
        _instrumentation_failures[pid] = tuple(failures)
    return tuple(failures)


def init_sentry(settings: Settings):
    """初始化 Sentry 并返回 disabled、initialized 或 failed 状态。"""
    if not settings.sentry.enabled or not settings.sentry.dsn:
        return "disabled"

    try:
        import sentry_sdk
        from sentry_sdk.integrations.asyncio import AsyncioIntegration
        from sentry_sdk.integrations.celery import CeleryIntegration
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.httpx import HttpxIntegration
        from sentry_sdk.integrations.redis import RedisIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

        sentry_sdk.init(
            dsn=settings.sentry.dsn.get_secret_value(),
            environment=settings.sentry.environment or settings.environment,
            traces_sample_rate=settings.sentry.traces_sample_rate,
            profiles_sample_rate=settings.sentry.profiles_sample_rate,
            send_default_pii=settings.sentry.send_default_pii,
            integrations=[
                FastApiIntegration(),
                AsyncioIntegration(),
                CeleryIntegration(),
                HttpxIntegration(),
                RedisIntegration(),
                SqlalchemyIntegration(),
            ],
        )
        return "initialized"
    except Exception as error:
        logging.getLogger("agent.telemetry").warning(
            "Sentry 初始化失败，应用继续启动",
            extra={"exception_type": type(error).__name__},
        )
        return "failed"


class Telemetry:
    """管理独立导出器生命周期；所有组件共享进程唯一 provider。"""

    def __init__(self, settings: Settings):
        self.config = settings.tracing
        self.redactor = Redactor(settings.redaction_patterns)
        self.provider = None
        self.instrumentation_failures = ()
        self.tracing_status = "disabled"

        # 初始化 Sentry 错误追踪
        self.sentry_status = init_sentry(settings)

        if self.config.enabled:
            # 启用追踪时同时启用自动插桩
            self.instrumentation_failures = instrument_libraries()

            try:
                # Celery 任务不断创建资源容器，provider 必须按进程复用，不能每次任务关闭全局 provider。
                identity = (os.getpid(), settings.app_name, self.config.endpoint)
                with _provider_lock:
                    if identity not in _providers:
                        provider = TracerProvider(
                            resource=Resource.create(
                                {
                                    "service.name": settings.app_name,
                                    "deployment.environment.name": settings.environment,
                                }
                            )
                        )
                        exporter = OTLPSpanExporter(
                            endpoint=self.config.endpoint,
                            headers={
                                k: v.get_secret_value() for k, v in self.config.headers.items()
                            },
                            timeout=5,
                        )
                        provider.add_span_processor(BatchSpanProcessor(exporter))
                        _providers[identity] = provider
                        if len([key for key in _providers if key[0] == os.getpid()]) == 1:
                            trace.set_tracer_provider(provider)
                        atexit.register(provider.shutdown)
                    self.provider = _providers[identity]
                self.tracer = self.provider.get_tracer("agent-template")
                self.tracing_status = "degraded" if self.instrumentation_failures else "initialized"
            except Exception as error:
                logging.getLogger("agent.telemetry").warning(
                    "OpenTelemetry 初始化失败，应用继续启动",
                    extra={"exception_type": type(error).__name__},
                )
                self.provider = None
                self.tracer = trace.NoOpTracerProvider().get_tracer("agent-template")
                self.tracing_status = "failed"
        else:
            self.tracer = trace.NoOpTracerProvider().get_tracer("agent-template")

    def content(self, text: str) -> str | None:
        """正文默认不采集；启用时先经过内置与配置的脱敏规则，再截断最大长度。"""
        if not self.config.capture_content:
            return None
        return self.redactor.text(text)[:8192]

    def close(self) -> None:
        """刷新并释放 exporter；进程资源关闭的最后阶段调用。"""
        if self.provider:
            self.provider.force_flush(timeout_millis=5000)
