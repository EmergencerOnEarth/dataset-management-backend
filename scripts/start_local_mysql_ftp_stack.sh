#!/usr/bin/env bash
# 本地联调：FTP(2121) + Uvicorn(MySQL + STORAGE_BACKEND=ftp, 8092)
# 使用前请确保：
#   - MySQL 已建库 eye_research_dataset（见下方 mysql 命令）
#   - 已安装 Python 依赖（同仓库 pyproject / pip install）

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="$ROOT"
export DATABASE_URL="${DATABASE_URL:-mysql+pymysql://root@127.0.0.1:3306/eye_research_dataset?charset=utf8mb4}"
export STORAGE_BACKEND="${STORAGE_BACKEND:-ftp}"
export FTP_HOST="${FTP_HOST:-127.0.0.1}"
export FTP_PORT="${FTP_PORT:-2121}"
export FTP_USER="${FTP_USER:-dataset}"
export FTP_PASSWORD="${FTP_PASSWORD:-change-me}"
export FTP_ROOT="${FTP_ROOT:-/dataset}"
export APP_AUTH_DISABLED="${APP_AUTH_DISABLED:-true}"
export DATASET_RUNTIME_DIR="${DATASET_RUNTIME_DIR:-.runtime}"
export DATASET_API_PORT="${DATASET_API_PORT:-8092}"

mkdir -p "${DATASET_RUNTIME_DIR}/ftp_home/dataset"

echo "Starting FTP on ${FTP_HOST}:${FTP_PORT} (home ${DATASET_RUNTIME_DIR}/ftp_home)..."
python3 scripts/run_local_ftp_server.py \
  --host "$FTP_HOST" --port "$FTP_PORT" \
  --user "$FTP_USER" --password "$FTP_PASSWORD" &
FTP_PID=$!

sleep 1
echo "Starting API on 127.0.0.1:${DATASET_API_PORT}..."
python3 -m uvicorn backend.app.main:app --host 127.0.0.1 --port "$DATASET_API_PORT" &
API_PID=$!

cleanup() {
  kill "$API_PID" 2>/dev/null || true
  kill "$FTP_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

wait
