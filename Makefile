# AgentTemplate Makefile - 常用命令快捷方式
# 使用 PRESET 变量选择档位：make setup PRESET=minimal 或 make setup PRESET=full

PRESET ?= minimal
COMPOSE_FILES = -f compose.yaml
ifeq ($(PRESET),full)
	EXTRA_FLAG = --extra full
	COMPOSE_FILES += -f compose.full.yaml
else
	EXTRA_FLAG =
endif

.PHONY: help setup setup-minimal setup-full up down restart logs migrate preflight check
.PHONY: init dev worker scheduler doctor clean

# 显示帮助信息
help:
	@echo "AgentTemplate - 通用 Agent 后端开发模板"
	@echo ""
	@echo "一键启动："
	@echo "  make setup              初始化项目（默认 minimal 档）"
	@echo "  make setup PRESET=full  初始化项目（full 档）"
	@echo "  make setup-minimal      初始化 minimal 档（快捷方式）"
	@echo "  make setup-full         初始化 full 档（快捷方式）"
	@echo ""
	@echo "Docker 服务："
	@echo "  make up                 启动 Docker 服务"
	@echo "  make down               停止 Docker 服务"
	@echo "  make restart            重启 Docker 服务"
	@echo "  make logs               查看服务日志"
	@echo ""
	@echo "开发命令："
	@echo "  make migrate            执行数据库迁移"
	@echo "  make preflight          启动预检"
	@echo "  make check              代码检查（格式、lint、类型、规范）"
	@echo "  make dev                启动开发服务器"
	@echo "  make worker             启动 Worker（仅 full 档）"
	@echo "  make scheduler          启动调度器（仅 full 档）"
	@echo ""
	@echo "其他："
	@echo "  make doctor             完整诊断（预检 + 主动探测）"
	@echo "  make clean              清理生成的文件"

# 一键初始化与启动
setup: init
	@echo "正在安装依赖..."
	uv sync $(EXTRA_FLAG)
	@echo "正在启动 Docker 服务..."
	$(MAKE) up
	@echo "正在执行数据库迁移..."
	$(MAKE) migrate
	@echo "正在执行启动预检..."
	$(MAKE) preflight
	@echo ""
	@echo "初始化完成！"
	@echo ""
	@echo "启动开发服务器："
	@echo "  make dev"
ifeq ($(PRESET),full)
	@echo ""
	@echo "启动 Worker 和调度器（新终端）："
	@echo "  make worker"
	@echo "  make scheduler"
endif
	@echo ""
	@echo "API 文档："
	@echo "  http://localhost:8000/docs"

setup-minimal:
	$(MAKE) setup PRESET=minimal

setup-full:
	$(MAKE) setup PRESET=full

# 初始化配置
init:
	uv run agent-template init --preset $(PRESET)

# Docker 服务管理
up:
	docker compose $(COMPOSE_FILES) --env-file .data/compose.env up -d

down:
	docker compose down

restart:
	$(MAKE) down
	$(MAKE) up

logs:
	docker compose logs -f

# 数据库迁移
migrate:
	uv run agent-template migrate

# 检查命令
preflight:
	uv run agent-template preflight

doctor:
	uv run agent-template doctor

check:
	uv run agent-template check

# 开发服务
dev:
	uv run agent-template dev

worker:
	uv run agent-template worker

scheduler:
	uv run agent-template scheduler

# 清理
clean:
	@echo "清理 Python 缓存..."
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	@echo "清理完成"
