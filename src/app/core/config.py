"""收敛所有配置来源；加载过程不连接外部服务，也不会在导入时生成凭据。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
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
    # 网页正文直接进入模型上下文，超出部分截断并标记，避免一次抓取耗尽上下文预算。
    fetch_max_chars: int = Field(default=20000, ge=1000)


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
    langfuse_url: str | None = None
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    langsmith_url: str | None = None
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str | None = None


class PreflightConfig(StrictModel):
    """启动预检；API 与 Worker 启动前执行，阻断项失败时停止启动。"""

    enabled: bool = True
    # 滚动发布中允许代码先于迁移启动时，可把未执行迁移降级为警告。
    pending_migrations: Literal["fail", "warn"] = "fail"
    timeout_seconds: float = Field(default=5, gt=0)


PRESETS = ("minimal", "full")
# 预设文件缺失时使用的内置默认值，与 config/presets 下的文件保持一致。
PRESET_MODULES = {
    "minimal": ["storage"],
    "full": ["storage", "background", "knowledge", "memory"],
}


class Settings(BaseSettings):
    """完整运行配置；显式 load_settings 控制来源，避免隐式 .env 改变进程行为。"""

    model_config = SettingsConfigDict(
        env_prefix="AGENT_", env_nested_delimiter="__", extra="forbid", enable_decoding=True
    )
    environment: Literal["local", "test", "staging", "production"] = "local"
    # 预设只决定 modules 的默认值；init 写入私有配置，之后可以直接修改 modules 列表增减模块。
    preset: Literal["minimal", "full"] = "full"
    modules: list[str] = Field(default_factory=lambda: list(PRESET_MODULES["full"]))
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
    # open 在限流后端故障时放行并告警，closed 返回 503；由部署方在可用性与防护之间选择。
    rate_limit_failure_mode: Literal["open", "closed"] = "open"
    cors_origins: list[str] = Field(default_factory=list)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    # auto 在 local 环境输出控制台格式，其余环境输出 JSON。
    log_format: Literal["auto", "json", "console"] = "auto"
    slow_query_ms: int | None = Field(default=500, ge=1)
    # 日志与 Trace 正文共用的脱敏正则；内置规则始终生效，这里只追加项目特有的格式。
    redaction_patterns: list[str] = Field(default_factory=list)
    preflight: PreflightConfig = Field(default_factory=PreflightConfig)
    config_sources: list[str] = Field(default_factory=list, exclude=True)

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: Any) -> Any:
        """日志级别不区分大小写，统一转为标准库使用的大写名称。"""
        return value.upper() if isinstance(value, str) else value

    @field_validator("redaction_patterns")
    @classmethod
    def compile_patterns(cls, value: list[str]) -> list[str]:
        """启动时编译全部脱敏规则；写错的正则直接报错，不能等到写日志时才失效。"""
        for pattern in value:
            try:
                re.compile(pattern)
            except re.error as error:
                raise ValueError(f"脱敏规则不是合法正则：{pattern}") from error
        return value

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
        if self.tracing.capture_content and not self.redaction_patterns:
            raise ValueError("正文采集必须提供脱敏规则")
        return self


class ConfigurationError(ValueError):
    """配置校验失败；只包含字段位置和原因，不回显输入值，避免凭据出现在启动输出中。"""


def describe(error: ValidationError) -> str:
    """把校验错误整理成"字段：原因"列表；pydantic 默认信息会附带整段输入，其中可能有密钥。"""
    items = []
    for item in error.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in item["loc"]) or "配置"
        items.append(f"{location}：{item['msg']}")
    return "；".join(items)


def merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """递归合并配置字典；列表整体覆盖，调用者提供的原字典不被修改。"""
    result = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result


def read_layer(path: Path) -> dict[str, Any] | None:
    """读取一层 YAML 配置；文件不存在返回 None，解析失败只报行号不回显原文。"""
    if not path.exists():
        return None
    try:
        content = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as error:
        # YAML 解析错误会附带出错行的原文，私有配置的这一行可能是密钥，只报行号。
        mark = getattr(error, "problem_mark", None)
        line = f"第 {mark.line + 1} 行" if mark else "位置未知"
        raise ConfigurationError(f"配置文件不是合法 YAML：{path}（{line}）") from None
    if not isinstance(content, dict):
        raise ConfigurationError(f"配置必须是映射：{path}")
    return content


def load_settings(overrides: dict[str, Any] | None = None, root: Path | None = None) -> Settings:
    """按基础、预设、环境、私有、环境变量、显式覆盖的顺序读取配置并校验；秘密从不输出。

    预设决定默认启用的模块。预设名依次取自显式覆盖、AGENT_PRESET、私有配置、环境配置、
    基础配置中的 preset 字段，都没有时使用 full。
    """
    root = root or Path(os.environ.get("AGENT_CONFIG_ROOT", "."))
    overrides = overrides or {}
    environment = overrides.get("environment", os.getenv("AGENT_ENVIRONMENT", "local"))
    files = {
        "base": root / "config/base.yaml",
        "environment": root / f"config/{environment}.yaml",
        "private": root / "config/private.yaml",
    }
    layers = {name: read_layer(path) for name, path in files.items()}
    declared = [
        layer["preset"]
        for layer in (layers["private"], layers["environment"], layers["base"])
        if layer and "preset" in layer
    ]
    preset = overrides.get("preset") or os.getenv("AGENT_PRESET") or next(iter(declared), "full")
    if preset not in PRESETS:
        raise ConfigurationError(f"未知预设：{preset}，可选值为 minimal、full")
    files["preset"] = root / f"config/presets/{preset}.yaml"
    layers["preset"] = read_layer(files["preset"])
    data: dict[str, Any] = {}
    sources: list[str] = []
    for name in ("base", "preset", "environment", "private"):
        layer = layers[name]
        if layer is not None:
            data = merge(data, layer)
            sources.append(str(files[name]))
        elif name == "preset":
            # 预设文件缺失时退回内置默认值，保证启用的模块与声明的预设一致。
            data = merge(data, {"modules": list(PRESET_MODULES[preset])})
            sources.append(f"内置预设 {preset}")
    # BaseSettings 的环境源优先于文件，显式覆盖最后深度合并；嵌套值不会丢失。
    env = EnvSettingsSource(Settings)()
    data = merge(data, env)
    data = merge(data, overrides)
    data["environment"] = environment
    data["preset"] = preset
    data["config_sources"] = sources + ["AGENT_* 环境变量", "程序显式覆盖"]
    try:
        return Settings.model_validate(data)
    except ValidationError as error:
        # from None 去掉原始异常链，原始异常的文本里带有输入值。
        raise ConfigurationError("配置校验失败：" + describe(error)) from None
