#!/usr/bin/env bash
# 本地 MySQL + 本地 FTP 回归：避免无意中使用 .secrets/local.env 等文件里指向远端 TiDB/FTP 的 DATABASE_URL。
# 用法：在项目根目录执行  bash scripts/run_local_mysql_ftp_regression.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

unset DATABASE_URL MYSQL_URL
export MYSQL_COMPOSE_ONLY=true
export PYTHONPATH="${ROOT}"

# ——按需修改为本机回归库——
export MYSQL_HOST="${MYSQL_HOST:-127.0.0.1}"
export MYSQL_PORT="${MYSQL_PORT:-3306}"
export MYSQL_USER="${MYSQL_USER:-root}"
export MYSQL_PASSWORD="${MYSQL_PASSWORD:-}"
export MYSQL_DATABASE="${MYSQL_DATABASE:-eye_research_dataset}"

export STORAGE_BACKEND="${STORAGE_BACKEND:-ftp}"
export FTP_HOST="${FTP_HOST:-127.0.0.1}"
export FTP_PORT="${FTP_PORT:-2121}"
export FTP_BIND="${FTP_BIND:-127.0.0.1}"
export FTP_USER="${FTP_USER:-dataset}"
export FTP_PASSWORD="${FTP_PASSWORD:-change-me}"
export FTP_ROOT="${FTP_ROOT:-/dataset}"

python3 <<'PY'
import os

def masked(k: str, v: str) -> str:
    if any(x in k.upper() for x in ("PASSWORD", "SECRET", "TOKEN")):
        return "***" if v else ""
    return v

print("———— 本轮回归将要使用的数据库 / 存储连接（请先确认主机不是测试环境 TiDB/FTP）————")
for key in sorted(os.environ):
    if key.startswith(("MYSQL_", "FTP_", "STORAGE_BACKEND")):
        print(f"{key}={masked(key, os.environ[key])}")
print("------------------------------------------------------------------------------------------------")
PY

python3 -m compileall -q backend scripts tests

echo "———— MySQL+FTP 集成（单独进程，勿与 mock 混跑）————"
export PYTEST_INTEGRATION=1
python3 -m pytest tests/test_integration_mysql_ftp.py -v --tb=short

echo "———— 其余单元测试 ————"
unset PYTEST_INTEGRATION
python3 -m pytest tests/ --ignore=tests/test_integration_mysql_ftp.py -v
