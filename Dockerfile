# 构建阶段只安装锁文件依赖，生产镜像不包含开发工具及私有配置。
FROM python:3.12.14-slim-bookworm AS build
RUN pip install --no-cache-dir uv==0.12.22
WORKDIR /opt/app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
# 默认安装 full 预设的模块依赖；构建 minimal 镜像时传入 --build-arg EXTRAS= 只装核心依赖。
ARG EXTRAS=full
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project ${EXTRAS:+--extra "$EXTRAS"}
COPY src ./src
RUN uv sync --locked --no-dev ${EXTRAS:+--extra "$EXTRAS"}

# 固定 Python 补丁版本，使用非 root 身份；配置和数据由运行时挂载。
FROM python:3.12.14-slim-bookworm
RUN groupadd --gid 10001 agent && useradd --uid 10001 --gid 10001 --create-home agent
WORKDIR /opt/app
COPY --from=build --chown=agent:agent /opt/app /opt/app
COPY --chown=agent:agent config ./config
RUN mkdir -p .data prompts && chown -R agent:agent .data prompts
ENV PATH="/opt/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER agent
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
