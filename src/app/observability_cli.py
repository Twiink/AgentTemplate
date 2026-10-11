"""观测部署配置生成；私有凭据只保存到受保护文件，Collector 按实际目标组合。"""

import base64
import secrets
from pathlib import Path

import yaml

from app.core.errors import AppError


def initialize_observability(langfuse: bool, langsmith: bool):
    """生成本地观测服务凭据与 Collector 文件，不覆盖已有文件，不修改共享配置。"""
    from app.cli import private_write

    if not langfuse and not langsmith:
        raise AppError("observability_targets", "至少选择一个观测后端")
    path = Path(".data/observability.env")
    if path.exists():
        raise AppError("observability_exists", "观测私有配置已存在；请保留并人工修改目标")
    secret_key = secrets.token_urlsafe(32)
    public_key = "pk-lf-" + secrets.token_hex(16)
    env = {
        "LANGFUSE_PUBLIC_KEY": public_key,
        "LANGFUSE_SECRET_KEY": secret_key,
        "LANGFUSE_AUTH": "Basic "
        + base64.b64encode(f"{public_key}:{secret_key}".encode()).decode(),
        "LANGFUSE_SALT": secrets.token_urlsafe(32),
        "LANGFUSE_ENCRYPTION_KEY": secrets.token_hex(32),
        "LANGFUSE_NEXTAUTH_SECRET": secrets.token_urlsafe(32),
        "LANGFUSE_DB_PASSWORD": secrets.token_urlsafe(32),
        "LANGFUSE_CLICKHOUSE_PASSWORD": secrets.token_urlsafe(32),
        "LANGFUSE_S3_USER": "langfuse-" + secrets.token_hex(8),
        "LANGFUSE_S3_PASSWORD": secrets.token_urlsafe(32),
        "LANGFUSE_REDIS_PASSWORD": secrets.token_urlsafe(32),
        "GRAFANA_PASSWORD": secrets.token_urlsafe(32),
        "MINIO_ROOT_USER": "agent-storage",
        "MINIO_ROOT_PASSWORD": secrets.token_urlsafe(32),
    }
    if langsmith:
        env["LANGSMITH_API_KEY"] = ""
        env["LANGSMITH_PROJECT"] = "agent-template"
    private_write(path, "".join(f"{k}={v}\n" for k, v in env.items()))
    exporters = {}
    if langfuse:
        exporters["otlphttp/langfuse"] = {
            "traces_endpoint": "http://langfuse-web:3000/api/public/otel/v1/traces",
            "headers": {"Authorization": "${env:LANGFUSE_AUTH}"},
        }
    if langsmith:
        exporters["otlphttp/langsmith"] = {
            "traces_endpoint": "${env:LANGSMITH_OTLP_ENDPOINT}",
            "headers": {
                "x-api-key": "${env:LANGSMITH_API_KEY}",
                "Langsmith-Project": "${env:LANGSMITH_PROJECT}",
            },
        }
    collector = {
        "receivers": {"otlp": {"protocols": {"http": {"endpoint": "0.0.0.0:4318"}}}},
        "processors": {"batch": {}},
        "exporters": exporters,
        "service": {
            "pipelines": {
                "traces": {
                    "receivers": ["otlp"],
                    "processors": ["batch"],
                    "exporters": list(exporters),
                }
            }
        },
    }
    private_write(Path(".data/collector.yaml"), yaml.safe_dump(collector, sort_keys=False))
    return path
