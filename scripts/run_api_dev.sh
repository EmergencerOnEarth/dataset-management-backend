#!/usr/bin/env bash
# 在本地开发机上启动 API。
# 强制使用 MYSQL_* 拼装 DSN，避免 .env / 终端里遗留的 DATABASE_URL 指向其它库。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="$ROOT"
export APP_AUTH_DISABLED="${APP_AUTH_DISABLED:-true}"
export STORAGE_BACKEND="${STORAGE_BACKEND:-local}"
export APP_PORT="${APP_PORT:-8092}"

export MYSQL_HOST="${MYSQL_HOST:-127.0.0.1}"
export MYSQL_PORT="${MYSQL_PORT:-3306}"
export MYSQL_USER="${MYSQL_USER:-root}"
export MYSQL_PASSWORD="${MYSQL_PASSWORD:-}"
export MYSQL_DATABASE="${MYSQL_DATABASE:-eye_research_dataset}"
export MYSQL_COMPOSE_ONLY="${MYSQL_COMPOSE_ONLY:-true}"

exec env -u DATABASE_URL python3 -m uvicorn backend.app.main:app \
  --host "${APP_BIND:-0.0.0.0}" \
  --port "$APP_PORT" \
  "$@"
