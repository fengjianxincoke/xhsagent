#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

STAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="data/backups/browser_state_$STAMP"

SESSION_FILE="data/session.json"
PROFILE_DIR="data/browser-profile"
DEBUG_DIR="data/debug"

mkdir -p "$BACKUP_DIR"

backup_path() {
    local path="$1"
    if [ -e "$path" ]; then
        local name
        name="$(basename "$path")"
        mv "$path" "$BACKUP_DIR/$name"
        echo "已备份: $path -> $BACKUP_DIR/$name"
    else
        echo "跳过: $path 不存在"
    fi
}

echo "重置小红书浏览器状态"
echo "项目目录: $PROJECT_DIR"
echo "备份目录: $BACKUP_DIR"
echo ""

backup_path "$SESSION_FILE"
backup_path "$PROFILE_DIR"
backup_path "$DEBUG_DIR"

mkdir -p data data/exports "$PROFILE_DIR"

echo ""
echo "浏览器状态已重置。"
echo "保留内容:"
echo "- data/posts.db"
echo "- data/exports/"
echo ""
echo "下一步:"
echo "1. 确认 settings.yaml 中 browser.headless=false"
echo "2. 运行: ./script/start.sh"
echo "3. 在弹出的浏览器里重新扫码登录"
echo "4. 登录成功后再把 headless 改回 true"
