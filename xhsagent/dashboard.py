from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .database import Database

log = logging.getLogger(__name__)


class Dashboard(threading.Thread):
    console = Console()

    def __init__(self, db: Database) -> None:
        super().__init__(name="dashboard", daemon=True)
        self.db = db
        self._running = threading.Event()
        self._running.set()
        self._stats_lock = threading.Lock()
        self._last_stats: dict[str, Any] = {}

    def update_stats(self, stats: dict[str, Any]) -> None:
        with self._stats_lock:
            self._last_stats = dict(stats)

    def stop(self) -> None:
        self._running.clear()

    def run(self) -> None:
        self.print_banner()
        while self._running.is_set():
            try:
                self.render_dashboard()
                time.sleep(2)
            except Exception as exc:
                log.debug("Dashboard 刷新异常: %s", exc)

    def render_dashboard(self) -> None:
        with self._stats_lock:
            stats = dict(self._last_stats)

        try:
            posts = self.db.get_all_posts(10)
        except Exception:
            posts = []

        summary = Table.grid(padding=(0, 2))
        summary.add_column(style="cyan", no_wrap=True)
        summary.add_column(style="bold")
        summary.add_row("🕐 时间", datetime.now().strftime("%H:%M:%S"))

        status = str(stats.get("status", "idle"))
        keyword = str(stats.get("currentKeyword", ""))
        if status == "crawling" and keyword:
            summary.add_row("🔍 状态", f"正在搜索: {keyword}")
        else:
            summary.add_row("😴 状态", "等待下次采集")

        summary.add_row("📦 总记录帖子", str(stats.get("total", 0)))
        summary.add_row("🧲 本轮原始爬取", str(stats.get("totalCrawled", 0)))
        summary.add_row("✅ 本轮已保存", str(stats.get("totalSaved", 0)))
        summary.add_row("📤 待推送飞书", str(stats.get("pending", 0)))
        summary.add_row("🎯 平均匹配分", str(stats.get("avgScore", 0.0)))
        top_platforms = stats.get("topPlatforms") or []
        if top_platforms:
            summary.add_row(
                "📱 平台分布",
                " / ".join(f"{name}:{count}" for name, count in top_platforms),
            )
        summary.add_row("🕓 上次运行", str(stats.get("lastRun", "—")))

        posts_table = Table(show_header=True, header_style="bold magenta")
        posts_table.add_column("评分", justify="right", width=6)
        posts_table.add_column("平台", width=8)
        posts_table.add_column("标题", width=30)
        posts_table.add_column("作者", width=12)
        posts_table.add_column("点赞", justify="right", width=8)
        posts_table.add_column("收藏", justify="right", width=8)

        if posts:
            for post in posts:
                posts_table.add_row(
                    str(post.match_score),
                    truncate(post.platform, 6),
                    truncate(post.title, 28),
                    truncate(post.author, 10),
                    str(post.likes),
                    str(post.collects),
                )
        else:
            posts_table.add_row("-", "-", "暂无数据，等待采集...", "-", "-", "-")

        keyword_table = Table(show_header=True, header_style="bold green")
        keyword_table.add_column("关键词")
        keyword_table.add_column("数量", justify="right")
        top_keywords = stats.get("topKeywords") or []
        if top_keywords:
            for keyword_name, count in top_keywords:
                keyword_table.add_row(str(keyword_name), str(count))
        else:
            keyword_table.add_row("暂无", "0")

        body = Table.grid(expand=True)
        body.add_column(ratio=1)
        body.add_column(ratio=2)
        body.add_row(summary, posts_table)
        body.add_row(keyword_table, "")

        panel = Panel(
            body,
            title="🌸 社媒 AI Agent 实时监控面板",
            subtitle="按 Ctrl+C 停止 Agent（会先导出 CSV 再退出）",
            border_style="magenta",
        )
        self.console.print(panel)

    @staticmethod
    def print_banner() -> None:
        banner = Panel.fit(
            "[bold magenta]🌸 社媒 AI Agent — Python 3.11[/bold magenta]\n"
            "[white]自主浏览 · Claude AI匹配 · 飞书多维表格同步[/white]",
            border_style="magenta",
        )
        Dashboard.console.print(banner)


def truncate(value: str | None, max_width: int) -> str:
    if not value:
        return ""
    current_width = 0
    chars: list[str] = []
    for char in value:
        char_width = 2 if ord(char) > 127 else 1
        if current_width + char_width > max_width:
            chars.append("…")
            break
        chars.append(char)
        current_width += char_width
    return "".join(chars)
