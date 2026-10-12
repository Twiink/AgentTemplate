# 常用命令统一使用 Python CLI，避免多份配置与路径逻辑。
.PHONY: init preflight doctor dev worker scheduler migrate check compose compose-full
init:
	uv run agent-template init
preflight:
	uv run agent-template preflight
doctor:
	uv run agent-template doctor
dev:
	uv run agent-template dev
worker:
	uv run agent-template worker
scheduler:
	uv run agent-template scheduler
migrate:
	uv run agent-template migrate
check:
	uv run agent-template check
# 两档共用的基础服务：PostgreSQL、Redis、迁移与 API。
compose:
	docker compose --env-file .data/compose.env up -d
# full 预设：在基础服务上追加 Worker 与调度进程。
compose-full:
	docker compose -f compose.yaml -f compose.full.yaml --env-file .data/compose.env up -d
