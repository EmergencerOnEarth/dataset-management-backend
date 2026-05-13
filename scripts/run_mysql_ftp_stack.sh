#!/usr/bin/env bash
# MySQL 元数据 + pyftpdlib FTP 大文件 + API 一键联调（与设计「库 + FTP」一致）
#
# 用法（在工程根目录）:
#   chmod +x scripts/run_mysql_ftp_stack.sh
#   bash scripts/run_mysql_ftp_stack.sh
#
# 环境变量可覆盖:
#   DATABASE_URL  整条 DSN（未设则连 127.0.0.1 上 eye_research_dataset）
#   FTP_PORT      默认 2121
#   APP_PORT      默认 8092
#   FTP_BIND      FTP 监听地址，默认 0.0.0.0
#   FTP_HOST_FOR_API  API 里 ftplib 连接的主机，FTP 与 API 同机时用 127.0.0.1（默认）
#   STACK_EXIT_AFTER_E2E=yes  跑完 E2E 后脚本退出且保留 FTP/API 进程（不设则前台 wait 直至 Ctrl+C）
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="$ROOT"
export APP_AUTH_DISABLED="${APP_AUTH_DISABLED:-true}"
export APP_PORT="${APP_PORT:-8092}"
export FTP_PORT="${FTP_PORT:-2121}"
export FTP_BIND="${FTP_BIND:-0.0.0.0}"
export STORAGE_BACKEND=ftp
export FTP_ROOT="${FTP_ROOT:-/dataset}"
export FTP_USER="${FTP_USER:-dataset}"
export FTP_PASSWORD="${FTP_PASSWORD:-change-me}"
export FTP_HOST="${FTP_HOST_FOR_API:-127.0.0.1}"

if [[ -z "${DATABASE_URL:-}" ]]; then
  export DATABASE_URL="mysql+pymysql://root:@127.0.0.1:3306/eye_research_dataset?charset=utf8mb4"
fi
export MYSQL_COMPOSE_ONLY=false

mkdir -p .runtime/ftp_home/dataset .runtime/logs

echo "== 停止旧进程（${APP_PORT}/${FTP_PORT}）=="
lsof -ti:"${FTP_PORT}" | xargs kill -9 2>/dev/null || true
lsof -ti:"${APP_PORT}" | xargs kill -9 2>/dev/null || true
sleep 1

echo "== 启动 FTP (pyftpdlib ${FTP_BIND}:${FTP_PORT}) → 物理目录: ${ROOT}/.runtime/ftp_home/dataset/ =="
nohup python3 scripts/run_local_ftp_server.py \
  --host "${FTP_BIND}" \
  --port "${FTP_PORT}" \
  --user "${FTP_USER}" \
  --password "${FTP_PASSWORD}" \
  > .runtime/logs/ftpd.log 2>&1 &
FTP_PID=$!

sleep 1
if ! kill -0 "$FTP_PID" 2>/dev/null; then
  echo "FTP 启动失败，见 .runtime/logs/ftpd.log"
  cat .runtime/logs/ftpd.log || true
  exit 1
fi
echo "FTP PID=$FTP_PID"

API_PID=""
cleanup() {
  echo "== 停止 FTP / API =="
  [[ -n "${API_PID:-}" ]] && kill "$API_PID" 2>/dev/null || true
  kill "$FTP_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "== 启动 API (${STORAGE_BACKEND}) FTP_HOST=${FTP_HOST} FTP_PORT=${FTP_PORT} =="
nohup env -u MYSQL_COMPOSE_ONLY \
  PYTHONPATH="$ROOT" \
  APP_AUTH_DISABLED="$APP_AUTH_DISABLED" \
  STORAGE_BACKEND=ftp \
  FTP_HOST="$FTP_HOST" \
  FTP_PORT="$FTP_PORT" \
  FTP_USER="$FTP_USER" \
  FTP_PASSWORD="$FTP_PASSWORD" \
  FTP_ROOT="$FTP_ROOT" \
  DATABASE_URL="$DATABASE_URL" \
  python3 -m uvicorn backend.app.main:app \
  --host 0.0.0.0 \
  --port "$APP_PORT" \
  > .runtime/logs/api.log 2>&1 &
API_PID=$!

for i in 1 2 3 4 5 6 7 8 9 10; do
  if curl -sf "http://127.0.0.1:${APP_PORT}/health" >/dev/null; then
    echo "API 已就绪 http://0.0.0.0:${APP_PORT}  (本机可 http://127.0.0.1:${APP_PORT})"
    break
  fi
  sleep 0.5
  if [[ "$i" == 10 ]]; then
    echo "API 启动超时，见 .runtime/logs/api.log"
    cat .runtime/logs/api.log || true
    exit 1
  fi
done

echo "== 端到端自测（经 FTP 读写）=="
python3 scripts/integration_mysql_ftp_e2e.py --base-url "http://127.0.0.1:${APP_PORT}" || {
  echo "E2E 失败：见上方面板输出；日志 .runtime/logs/api.log"
  exit 1
}

echo ""
echo "==== 完成 ===="
echo "FTP 物理路径: ${ROOT}/.runtime/ftp_home/dataset/  （逻辑 /dataset/... 对应其下 upload|import|export）"
echo "API: http://127.0.0.1:${APP_PORT}  演示页 /demo/"
echo "FTP 进程 PID=${FTP_PID}, API 进程 PID=${API_PID}"

if [[ "${STACK_EXIT_AFTER_E2E:-no}" == "yes" ]]; then
  trap - EXIT
  echo "STACK_EXIT_AFTER_E2E=yes：FTP/API 继续在后台运行，本脚本退出。"
  exit 0
fi

echo "前台挂起直至 API 进程结束；Ctrl+C 会停止 FTP 与 API。"
wait "$API_PID"
