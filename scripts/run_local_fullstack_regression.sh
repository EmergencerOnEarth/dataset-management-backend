#!/usr/bin/env bash
# 本地全栈回归：Docker MySQL（可选）+ pyftpdlib FTP + MySQL 元数据 + FTP 文件分区。
# 在仓库根目录执行:  bash scripts/run_local_fullstack_regression.sh
#
# 环境变量（可选）：
#   SKIP_DOCKER_MYSQL=1  不尝试启动 Docker MySQL（沿用本机已运行的 3306）
#   MYSQL_HOST / FTP_*   覆盖默认
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

unset DATABASE_URL MYSQL_URL
export MYSQL_COMPOSE_ONLY=true
export PYTHONPATH="${ROOT}"
export MYSQL_HOST="${MYSQL_HOST:-127.0.0.1}"
export MYSQL_PORT="${MYSQL_PORT:-3306}"
export MYSQL_USER="${MYSQL_USER:-root}"
export MYSQL_PASSWORD="${MYSQL_PASSWORD:-}"
export MYSQL_DATABASE="${MYSQL_DATABASE:-eye_research_dataset}"

export STORAGE_BACKEND=ftp
export FTP_HOST="${FTP_HOST:-127.0.0.1}"
export FTP_PORT="${FTP_PORT:-2121}"
export FTP_USER="${FTP_USER:-dataset}"
export FTP_PASSWORD="${FTP_PASSWORD:-change-me}"
export FTP_ROOT="${FTP_ROOT:-/dataset}"

if [[ "${SKIP_DOCKER_MYSQL:-0}" != "1" ]]; then
  if command -v docker >/dev/null 2>&1; then
    echo "======== Docker MySQL（容器名 de_sys_mysql）========"
    if docker ps -a --format '{{.Names}}' | grep -q '^de_sys_mysql$'; then
      docker start de_sys_mysql >/dev/null
    else
      docker run -d --name de_sys_mysql \
        -e MYSQL_ALLOW_EMPTY_PASSWORD=yes \
        -e MYSQL_DATABASE="${MYSQL_DATABASE}" \
        -p "${MYSQL_PORT}:3306" \
        mysql:8.0 \
        --character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci
    fi
    echo "等待 MySQL 就绪..."
    for i in $(seq 1 60); do
      if python3 -c "
import pymysql
pymysql.connect(host='${MYSQL_HOST}', port=int('${MYSQL_PORT}'), user='${MYSQL_USER}', password='${MYSQL_PASSWORD}', database='${MYSQL_DATABASE}', connect_timeout=2)
" 2>/dev/null; then
        break
      fi
      sleep 2
      if [[ "$i" -eq 60 ]]; then
        echo "MySQL 无法在约 120s 内连接，请检查 Docker 与本机 ${MYSQL_HOST}:${MYSQL_PORT}"
        exit 1
      fi
    done
  else
    echo "未安装 docker，跳过拉起 MySQL；请确保 ${MYSQL_HOST}:${MYSQL_PORT} 已有实例。"
  fi
fi

echo "======== 依赖（editable + dev + ftp-test）========"
python3 -m pip install -q --upgrade pip setuptools wheel 2>/dev/null || true
if ! python3 -m pip install -q -e ".[dev,ftp-test]"; then
  echo "pip install -e 失败，退回 pytest/httpx/pyftpdlib + PYTHONPATH"
  python3 -m pip install -q pytest httpx pyftpdlib
fi

echo "======== 启动本机 FTP（后台）========"
if command -v lsof >/dev/null 2>&1; then
  pids="$(lsof -ti ":${FTP_PORT}" 2>/dev/null || true)"
  if [[ -n "${pids}" ]]; then
    echo "释放 FTP 端口 ${FTP_PORT}（原进程: ${pids}）"
    kill -9 ${pids} 2>/dev/null || true
    sleep 0.5
  fi
fi
python3 scripts/run_local_ftp_server.py --host 127.0.0.1 --port "${FTP_PORT}" &
FTP_PID=$!
trap 'kill "${FTP_PID}" 2>/dev/null || true' EXIT
sleep 1

echo "======== 集成测试（MySQL + FTP + 新视野解析）========"
export PYTEST_INTEGRATION=1
python3 -m pytest tests/test_integration_mysql_ftp.py -v --tb=short

echo "======== 单元测试（不含集成文件）========"
unset PYTEST_INTEGRATION
python3 -m pytest tests/ --ignore=tests/test_integration_mysql_ftp.py -q

echo "======== 全栈回归完成 =========="
