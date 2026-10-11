#!/usr/bin/env bash
# 始终从仓库根运行，安装锁定依赖后初始化；已有私有文件不覆盖。
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync --locked
uv run agent-template init
