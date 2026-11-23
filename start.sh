#!/usr/bin/env bash
# 快速启动主机巡检服务。零依赖，默认前台运行，Ctrl+C 退出。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
DB="${DB:-$DIR/inspection.db}"
DAEMON=0
LOG_FILE="$DIR/server.log"
PID_FILE="$DIR/server.pid"

pid_alive() {
  local pid="${1:-}"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  local state
  state="$(ps -o stat= -p "$pid" 2>/dev/null | tr -d '[:space:]')"
  [[ -n "$state" && "$state" != Z* ]]
}

usage() {
  cat <<'USAGE'
用法: ./start.sh [选项]

选项:
  --host HOST      监听地址，默认 0.0.0.0（HOST 环境变量同效）
  --port PORT      监听端口，默认取环境变量 PORT，未设置时 8000
  --db PATH        数据库路径，默认仓库下 inspection.db（DB 环境变量同效）
  -d, --daemon     后台运行，日志写入 server.log，PID 写入 server.pid
  -h, --help       显示帮助
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="${2:?--host 需要参数}"; shift 2 ;;
    --port) PORT="${2:?--port 需要参数}"; shift 2 ;;
    --db)   DB="${2:?--db 需要参数}"; shift 2 ;;
    -d|--daemon) DAEMON=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "未找到 $PYTHON_BIN，请先安装 Python 3.11+" >&2
  exit 1
fi

if ! "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "Python 版本过低，需要 3.11+，当前: $("$PYTHON_BIN" -V 2>&1)" >&2
  exit 1
fi

if [[ $DAEMON -eq 1 && -f "$PID_FILE" ]] && pid_alive "$(cat "$PID_FILE")"; then
  echo "服务已在后台运行 (PID $(cat "$PID_FILE"))，停止: kill \$(cat $PID_FILE)"
  exit 0
fi

if ! "$PYTHON_BIN" - "$HOST" "$PORT" <<'PY'
import socket
import sys

host, port = sys.argv[1], int(sys.argv[2])
sock = socket.socket()
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    sock.bind((host, port))
except OSError:
    sys.exit(1)
finally:
    sock.close()
PY
then
  echo "端口 $PORT 已被占用，换一个端口: ./start.sh --port 8001" >&2
  exit 1
fi

ARGS=(app.py --host "$HOST" --port "$PORT" --db "$DB")

if [[ $DAEMON -eq 1 ]]; then
  PYTHONUNBUFFERED=1 nohup "$PYTHON_BIN" "${ARGS[@]}" >>"$LOG_FILE" 2>&1 &
  pid=$!
  echo "$pid" >"$PID_FILE"
  sleep 1
  if pid_alive "$pid"; then
    echo "服务已启动 (PID $pid): http://localhost:$PORT/"
    echo "日志: $LOG_FILE"
    echo "停止: kill \$(cat $PID_FILE)"
  else
    echo "启动失败，最近日志:" >&2
    tail -n 20 "$LOG_FILE" >&2 || true
    rm -f "$PID_FILE"
    exit 1
  fi
else
  echo "服务地址: http://localhost:$PORT/  (db=$DB)，Ctrl+C 退出"
  exec "$PYTHON_BIN" "${ARGS[@]}"
fi
