#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

PIDS="$(pgrep -f "python.*-m xhsagent.main" || true)"

if [ -z "$PIDS" ]; then
    echo "未发现正在运行的 Agent 进程"
    exit 0
fi

echo "准备停止 Agent 进程: $PIDS"
kill -INT $PIDS

for _ in {1..10}; do
    sleep 1
    if ! pgrep -f "python.*-m xhsagent.main" >/dev/null 2>&1; then
        echo "Agent 已停止"
        exit 0
    fi
done

PIDS="$(pgrep -f "python.*-m xhsagent.main" || true)"
if [ -n "$PIDS" ]; then
    echo "Agent 未在预期时间内退出，发送 SIGTERM: $PIDS"
    kill -TERM $PIDS
fi

echo "停止请求已发送"
