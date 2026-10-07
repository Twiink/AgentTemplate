#!/usr/bin/env sh

set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT_DIR"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  printf '%s\n' '当前目录不在 Git 仓库中。' >&2
  exit 1
fi

HOOK_NAMES='pre-commit commit-msg pre-push post-merge pre-rebase'
for hook_name in $HOOK_NAMES; do
  if [ ! -f ".githooks/$hook_name" ]; then
    printf '未找到 Git hook：.githooks/%s\n' "$hook_name" >&2
    exit 1
  fi
  chmod +x ".githooks/$hook_name"
done

git config core.hooksPath .githooks

printf 'Git hooks 已配置：%s/.githooks (%s)\n' "$ROOT_DIR" "$HOOK_NAMES"
