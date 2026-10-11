# 常用命令统一使用 Python CLI，避免多份配置与路径逻辑。
.PHONY: init doctor dev worker scheduler migrate check test integration compose
init:
	uv run agent-template init
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
test:
	uv run pytest -q -m 'not integration and not providers'
integration:
	uv run pytest -q -m integration
compose:
	docker compose --env-file .data/compose.env up -d
