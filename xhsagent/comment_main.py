from __future__ import annotations

import csv
import logging
import signal
from pathlib import Path

from .agent import XHSAgent
from .config import AppConfig
from .database import Database
from .main import configure_logging

log = logging.getLogger(__name__)

URL_HEADER_CANDIDATES = ("帖子链接", "url", "链接")


def main() -> int:
    config = AppConfig()
    configure_logging(config)

    if not config.is_comment_enabled():
        log.error("❌ 评论功能未启用，请先在 settings.yaml 中将 comments.enabled 设为 true")
        return 1

    comment_platforms = config.get_comment_platforms()
    if not comment_platforms:
        log.error("❌ 未配置任何可用的评论平台文案")
        log.error("   请检查 settings.yaml 中 comments.xiaohongshu.content / comments.douyin.content")
        return 1

    db = Database(config.get_db_path())
    agent = XHSAgent(config, db)
    stop_requested = {"value": False}

    def handle_signal(signum, frame) -> None:  # type: ignore[override]
        if stop_requested["value"]:
            log.info("⚠️  已收到退出请求，等待当前评论步骤结束...")
            return
        stop_requested["value"] = True
        log.info("⚠️  收到退出信号，等待当前评论步骤完成后停止...")
        agent.request_stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        jobs = build_comment_jobs(config)
        if not jobs:
            log.warning("⚠️ 未从 CSV 中读取到任何待评论链接")
            return 0

        log.info("💬 即将启动评论任务，平台: %s", "、".join(comment_platforms))
        agent.start(platforms=comment_platforms)

        for platform in comment_platforms:
            ready = agent.ensure_platform_ready(platform)
            if not ready:
                log.error("❌ 平台 [%s] 登录/访问状态校验失败，评论任务终止", platform)
                return 1

        try:
            summary = agent.run_comment_jobs(jobs)
        except KeyboardInterrupt:
            stop_requested["value"] = True
            log.info("⚠️  收到中断请求，正在停止评论任务...")
            agent.request_stop()
            return 130
        log.info(
            "💬 评论执行完成：成功 %s，跳过 %s，失败 %s",
            summary["success"],
            summary["skipped"],
            summary["failed"],
        )
        return 0 if summary["failed"] == 0 else 2
    except KeyboardInterrupt:
        stop_requested["value"] = True
        log.info("⚠️  评论任务已取消")
        agent.request_stop()
        return 130
    finally:
        try:
            agent.close()
        except Exception:
            pass


def build_comment_jobs(config: AppConfig) -> list[dict[str, str]]:
    jobs: list[dict[str, str]] = []
    output_dir = Path(config.get_csv_output_dir())
    latest_only = config.use_latest_comment_csv_only()
    max_per_platform = config.get_comment_max_per_platform()

    for platform in config.get_comment_platforms():
        content = config.get_platform_comment_content(platform)
        csv_paths = resolve_csv_paths(output_dir, platform, latest_only)
        if not csv_paths:
            log.warning("⚠️ 平台 [%s] 未找到可用 CSV: %s", platform, output_dir)
            continue

        urls = collect_urls_from_csvs(csv_paths)
        if max_per_platform > 0:
            urls = urls[:max_per_platform]
        if not urls:
            log.warning("⚠️ 平台 [%s] 的 CSV 中没有可评论链接", platform)
            continue

        log.info(
            "💬 平台 [%s] 将从 %s 个 CSV 读取 %s 条链接进行评论",
            platform,
            len(csv_paths),
            len(urls),
        )
        for url in urls:
            jobs.append(
                {
                    "platform": platform,
                    "url": url,
                    "content": content,
                }
            )
    return jobs


def resolve_csv_paths(output_dir: Path, platform: str, latest_only: bool) -> list[Path]:
    if not output_dir.is_dir():
        return []

    candidates = sorted(
        output_dir.glob(f"{platform}_posts_*.csv"),
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    if not candidates:
        return []
    return candidates[:1] if latest_only else candidates


def collect_urls_from_csvs(csv_paths: list[Path]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for csv_path in csv_paths:
        try:
            with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    if not isinstance(row, dict):
                        continue
                    url = extract_url_from_row(row)
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    urls.append(url)
        except Exception as exc:
            log.warning("读取 CSV 失败 %s: %s", csv_path, exc)
    return urls


def extract_url_from_row(row: dict[str, str]) -> str:
    for header in URL_HEADER_CANDIDATES:
        value = str(row.get(header, "")).strip()
        if value:
            return value
    return ""


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
