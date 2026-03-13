#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

echo "🌸 Social AI Agent — Python 3.11"
echo "================================"

if command -v python3.11 >/dev/null 2>&1; then
    PYTHON_BIN="python3.11"
else
    echo "❌ 未找到 python3.11"
    exit 1
fi

echo "✅ Python: $($PYTHON_BIN --version)"
export PIP_DISABLE_PIP_VERSION_CHECK=1

mkdir -p data/exports

if [ -d .venv ]; then
    VENV_DIR=".venv"
else
    VENV_DIR=".venv"
    echo "📦 创建虚拟环境..."
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

if python - <<'PY' >/dev/null 2>&1
import apscheduler
import playwright
import requests
import rich
import yaml
PY
then
    echo "✅ Python 依赖已就绪"
else
    echo "📦 检测到缺少 Python 依赖，开始安装..."
    if ! python -m pip install -r requirements.txt; then
        echo "❌ Python 依赖安装失败。"
        echo "   如果当前环境无法联网，请先确保 .venv 中已安装 requirements.txt 里的依赖。"
        exit 1
    fi
fi

PLAYWRIGHT_CHROMIUM_PATH="$(python -c 'from playwright.sync_api import sync_playwright; p = sync_playwright().start(); print(p.chromium.executable_path); p.stop()')"
if [ ! -x "$PLAYWRIGHT_CHROMIUM_PATH" ]; then
    echo "📦 当前 Playwright 版本缺少 Chromium，可执行文件不存在，开始安装..."
    python -m playwright install chromium
fi

echo "🚀 启动 Agent..."
echo ""
exec python -m xhsagent.main
