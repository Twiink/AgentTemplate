"""单一 OpenTelemetry 导出与低基数指标；正文采集只在显式脱敏配置后开放。"""

import atexit
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

requests = Counter("agent_http_requests_total", "接口请求次数", ["method", "route", "status"])
latency = Histogram("agent_http_duration_seconds", "接口处理耗时", ["method", "route"])
runs = Counter("agent_runs_total", "运行状态统计", ["agent", "status"])
tools = Counter("agent_tool_calls_total", "工具调用统计", ["tool", "status"])
models = Counter("agent_model_calls_total", "模型调用统计", ["provider", "status"])
queue_depth = Gauge("agent_queue_depth", "持久排队运行数量", multiprocess_mode="livemax")
active_runs = Gauge("agent_active_runs", "持久执行中运行数量", multiprocess_mode="livemax")
worker_heartbeat = Gauge(
    "agent_worker_heartbeat_timestamp", "Worker 最近任务处理时间", multiprocess_mode="livemax"
)
retrieval = Histogram("agent_retrieval_seconds", "检索耗时")
ratelimit_rejections = Counter("agent_ratelimit_rejections_total", "限流拒绝次数", ["route"])
ratelimit_backend_errors = Counter(
    "agent_ratelimit_backend_errors_total", "限流后端异常次数", ["route"]
)


class Telemetry:
    """管理独立导出器生命周期；所有组件共享进程唯一 provider。"""

    def __init__(self, settings: Settings):
        self.config = settings.tracing
        self.redactor = Redactor(settings.redaction_patterns)
        self.provider = None
        if self.config.enabled:
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
                        headers={k: v.get_secret_value() for k, v in self.config.headers.items()},
                        timeout=5,
                    )
                    provider.add_span_processor(BatchSpanProcessor(exporter))
                    _providers[identity] = provider
                    if len([key for key in _providers if key[0] == os.getpid()]) == 1:
                        trace.set_tracer_provider(provider)
                    atexit.register(provider.shutdown)
                self.provider = _providers[identity]
            self.tracer = self.provider.get_tracer("agent-template")
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
