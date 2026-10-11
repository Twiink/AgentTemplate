"""收敛所有配置来源；加载过程不连接外部服务，也不会在导入时生成凭据。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, EnvSettingsSource, SettingsConfigDict


class StrictModel(BaseModel):
    """拒绝未知字段，避免配置拼写错误被静默忽略。"""

    model_config = ConfigDict(extra="forbid")


class Credential(StrictModel):
    """稳定 API Key 身份；只存令牌摘要，不把原始令牌写入共享配置。"""

    digest: str
    tenant_id: str
    subject_id: str
    scopes: list[str] = Field(default_factory=lambda: ["resources:read", "resources:write"])


class AuthConfig(StrictModel):
    """API Key 与 JWT 验证配置；生产环境始终需要已验证身份。"""

    api_keys: list[Credential] = Field(default_factory=list)
    jwt_key: SecretStr | None = None
    jwks_url: str | None = None
    jwt_algorithms: list[str] = Field(default_factory=lambda: ["RS256"])
    jwt_issuer: str | None = None
    jwt_audience: str | None = None


class ModelProfile(StrictModel):
    """独立聊天或向量模型配置；原生参数仅补充参数，不允许替换认证与治理字段。"""

    provider: Literal["openai", "anthropic", "gemini"]
    model: str
    api_key: SecretStr
    endpoint: str | None = None
    proxy: str | None = None
    timeout_seconds: float = Field(default=120, gt=0)
    retries: int = Field(default=2, ge=0, le=5)
    concurrency: int = Field(default=8, ge=1)
    max_output_tokens: int = Field(default=4096, ge=1)
    context_tokens: int = Field(default=32000, ge=1)
    dimensions: int | None = Field(default=None, ge=1, le=16000)
    capabilities: set[str] = Field(default_factory=lambda: {"streaming", "tools", "structured"})
    parameters: dict[str, Any] = Field(default_factory=dict)
    price_version: str | None = None
    input_price_per_million: float | None = Field(default=None, ge=0)
    output_price_per_million: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def check_parameters(self) -> ModelProfile:
        """禁止通过透传参数覆盖凭据、重试、连接与预算，错误在启动配置验证时暴露。"""
        reserved = {
            "api_key",
            "timeout",
            "max_retries",
            "max_tokens",
            "http_client",
            "http_async_client",
            "client",
            "model",
            "model_name",
            "base_url",
            "max_output_tokens",
            "num_predict",
            "timeout_seconds",
            "retries",
        }
        if reserved & self.parameters.keys():
            raise ValueError("模型透传参数包含受治理的保留字段")
        if self.price_version is None and (
            self.input_price_per_million is not None or self.output_price_per_million is not None
        ):
            raise ValueError("价格配置必须提供 price_version")
        return self


class StorageConfig(StrictModel):
    """附件后端；本地和 S3 使用相同不透明对象键，路径不来自用户输入。"""

    backend: Literal["local", "s3"] = "local"
    directory: Path = Path(".data/artifacts")
    max_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    endpoint: str | None = None
    bucket: str | None = None
    region: str = "us-east-1"
    access_key: SecretStr | None = None
    secret_key: SecretStr | None = None


class ToolConfig(StrictModel):
    """工具执行边界；网络允许列表默认为空，由项目显式开放目标。"""

    directory: Path = Path(".data/workspace")
    allowed_hosts: list[str] = Field(default_factory=list)
    allow_private_networks: bool = False
    timeout_seconds: float = Field(default=30, gt=0)
    max_bytes: int = Field(default=2 * 1024 * 1024, gt=0)
    concurrency: int = Field(default=8, ge=1)
    confirm_writes: bool = True
    tavily_key: SecretStr | None = None
    readonly_database_url: SecretStr | None = None
    sql_row_limit: int = Field(default=100, ge=1, le=1000)
    sql_allowed_tables: list[str] = Field(default_factory=list)


class MCPServer(StrictModel):
    """MCP 连接声明；秘密值通过环境变量引用，stdio 只启动显式配置的进程。"""

    transport: Literal["stdio", "http"]
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    environment_refs: dict[str, str] = Field(default_factory=dict)
    header_refs: dict[str, str] = Field(default_factory=dict)
    allowed_tools: list[str] = Field(default_factory=list)
    write_tools: list[str] = Field(default_factory=list)


class TraceConfig(StrictModel):
    """应用只导出一次 OTLP，由 Collector 分发到已配置后端。"""

    enabled: bool = False
    endpoint: str = "http://localhost:4318/v1/traces"
    headers: dict[str, SecretStr] = Field(default_factory=dict)
    capture_content: bool = False
    redaction_patterns: list[str] = Field(default_factory=list)
    langfuse_url: str | None = None
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    langsmith_url: str | None = None
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str | None = None


class Settings(BaseSettings):
    """完整运行配置；显式 load_settings 控制来源，避免隐式 .env 改变进程行为。"""

    model_config = SettingsConfigDict(
        env_prefix="AGENT_", env_nested_delimiter="__", extra="forbid", enable_decoding=True
    )
    environment: Literal["local", "test", "staging", "production"] = "local"
    app_name: str = "AgentTemplate"
    database_url: SecretStr = SecretStr("postgresql+asyncpg://localhost/agenttemplate")
    migration_database_url: SecretStr | None = None
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")
    database_pool_size: int = Field(default=10, ge=1)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    chat_models: dict[str, ModelProfile] = Field(default_factory=dict)
    embedding_models: dict[str, ModelProfile] = Field(default_factory=dict)
    default_embedding: str | None = None
    storage: StorageConfig = Field(default_factory=StorageConfig)
    tools: ToolConfig = Field(default_factory=ToolConfig)
    mcp: dict[str, MCPServer] = Field(default_factory=dict)
    tracing: TraceConfig = Field(default_factory=TraceConfig)
    registrations: list[str] = Field(default_factory=list)
    prompts_directory: Path = Path("prompts")
    run_timeout_seconds: int = Field(default=300, ge=1)
    run_token_budget: int = Field(default=64000, ge=1)
    run_cost_budget: float | None = Field(default=None, gt=0)
    recursion_limit: int = Field(default=25, ge=1)
    lease_seconds: int = Field(default=60, ge=15)
    run_retention_days: int = Field(default=30, ge=1)
    memory_retention_days: int | None = Field(default=None, ge=1)
    memory_extraction_registration: str | None = None
    rate_limit_per_minute: int = Field(default=60, ge=1)
    cors_origins: list[str] = Field(default_factory=list)
    log_level: str = "INFO"
    config_sources: list[str] = Field(default_factory=list, exclude=True)

    @model_validator(mode="after")
    def validate_auth(self) -> Settings:
        """生产配置必须具有可验证凭据；模型缺省允许基础服务先启动。"""
        if self.environment == "production" and not (
            self.auth.api_keys or self.auth.jwt_key or self.auth.jwks_url
        ):
            raise ValueError("生产环境必须配置 API Key 或 JWT 验证")
        if self.auth.jwt_key or self.auth.jwks_url:
            if not self.auth.jwt_issuer or not self.auth.jwt_audience:
                raise ValueError("JWT 必须配置 issuer 与 audience")
        if self.default_embedding and self.default_embedding not in self.embedding_models:
            raise ValueError("默认 Embedding 配置不存在")
        if self.tracing.capture_content and not self.tracing.redaction_patterns:
            raise ValueError("正文采集必须提供脱敏规则")
        return self


def merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """递归合并配置字典；列表整体覆盖，调用者提供的原字典不被修改。"""
    result = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result


def load_settings(overrides: dict[str, Any] | None = None, root: Path | None = None) -> Settings:
    """按默认、环境、私有、环境变量、显式覆盖顺序读取配置并校验；秘密从不输出。"""
    root = root or Path(os.environ.get("AGENT_CONFIG_ROOT", "."))
    environment = (overrides or {}).get("environment", os.getenv("AGENT_ENVIRONMENT", "local"))
    data: dict[str, Any] = {}
    sources: list[str] = []
    for path in [
        root / "config/base.yaml",
        root / f"config/{environment}.yaml",
        root / "config/private.yaml",
    ]:
        if path.exists():
            content = yaml.safe_load(path.read_text()) or {}
            if not isinstance(content, dict):
                raise ValueError(f"配置必须是映射：{path}")
            data = merge(data, content)
            sources.append(str(path))
    # BaseSettings 的环境源优先于文件，显式覆盖最后深度合并；嵌套值不会丢失。
    env = EnvSettingsSource(Settings)()
    data = merge(data, env)
    data = merge(data, overrides or {})
    data["environment"] = environment
    data["config_sources"] = sources + ["AGENT_* 环境变量", "程序显式覆盖"]
    return Settings.model_validate(data)
