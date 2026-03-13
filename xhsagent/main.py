from __future__ import annotations

import logging
import signal
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler

from .agent import XHSAgent
from .config import AppConfig
from .csv_exporter import CsvExporter
from .dashboard import Dashboard
from .database import Database
from .feishu_exporter import FeishuExporter

log = logging.getLogger(__name__)


def main() -> int:
    config = AppConfig()
    configure_logging(config)
    Dashboard.print_banner()

    log.info("📋 采集需求: %s", config.get_requirement())
    log.info("🔑 关键词: %s", config.get_keywords())
    enabled_platforms = "小红书、抖音" if config.is_douyin_enabled() else "小红书"
    log.info("🎯 平台: %s", enabled_platforms)

    db = Database(config.get_db_path())
    csv_exporter = CsvExporter(db, config.get_csv_output_dir())
    feishu_exporter = FeishuExporter(config, db)
    agent = XHSAgent(config, db)
    dashboard = Dashboard(db)
    shutdown_event = threading.Event()
    stop_requested = threading.Event()
    cleanup_lock = threading.Lock()
    crawl_schedule_lock = threading.Lock()
    state = {"cleaned": False}
    scheduler_holder: dict[str, BackgroundScheduler | None] = {"scheduler": None}

    def on_post_saved(post) -> None:
        if config.is_feishu_enabled():
            feishu_exporter.send_webhook_notification(post)

    agent.set_on_post_saved(on_post_saved)

    def cleanup() -> None:
        with cleanup_lock:
            if state["cleaned"]:
                return
            state["cleaned"] = True
        log.info("⚠️  正在停止...")
        dashboard.stop()
        scheduler = scheduler_holder["scheduler"]
        if scheduler is not None:
            try:
                scheduler.shutdown(wait=False)
            except Exception:
                pass
        try:
            agent.close(interrupted=stop_requested.is_set())
        except Exception:
            pass
        try:
            paths = csv_exporter.export()
            if paths:
                log.info("📄 退出前已导出 CSV: %s", ", ".join(paths))
        except Exception as exc:
            log.warning("退出 CSV 导出失败: %s", exc)
        log.info("👋 Agent 已安全退出")
        shutdown_event.set()

    def handle_signal(signum, frame) -> None:  # type: ignore[override]
        if stop_requested.is_set():
            log.info("⚠️  已收到退出请求，等待当前步骤结束...")
            return
        log.info("⚠️  收到退出信号，等待当前步骤完成后停止...")
        stop_requested.set()
        agent.request_stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    agent.start()
    xhs_ready = agent.ensure_platform_ready("xiaohongshu")
    if not xhs_ready:
        log.error("❌ 未能完成小红书登录，退出")
        log.error("   登录态未恢复成功，启动自动采集不会执行")
        log.error("   请将 settings.yaml 中 browser.headless 设为 false，手动扫码后改回 true")
        try:
            agent.close()
        except Exception:
            pass
        return 1

    if config.is_douyin_enabled():
        douyin_ready = agent.ensure_platform_ready("douyin")
        if not douyin_ready:
            log.error("❌ 抖音状态校验失败，退出")
            log.error("   为避免只抓取小红书，本次启动已中止")
            log.error("   请将 settings.yaml 中 browser.headless 设为 false，完成抖音登录/验证后重试")
            try:
                agent.close()
            except Exception:
                pass
            return 1

    log.info("✅ 平台校验通过，准备启动调度器并执行首轮采集")

    scheduler = BackgroundScheduler(
        job_defaults={"coalesce": True, "max_instances": 1},
        timezone="Asia/Shanghai",
    )
    scheduler_holder["scheduler"] = scheduler

    def run_crawl_job(trigger: str) -> int:
        started_at = time.monotonic()
        saved = agent.run_crawl_cycle()
        duration = time.monotonic() - started_at
        log.info("✅ %s采集完成，新增 %s 条，用时 %.1f 秒", trigger, saved, duration)
        interval_seconds = config.get_crawl_interval_minutes() * 60
        if duration >= interval_seconds:
            log.warning(
                "采集耗时 %.1f 秒，已达到或超过当前间隔 %s 分钟；下轮将从本轮完成后开始计时",
                duration,
                config.get_crawl_interval_minutes(),
            )
        return saved

    def scheduled_crawl() -> None:
        if stop_requested.is_set():
            return
        log.info("⏰ APScheduler 触发采集任务")
        try:
            run_crawl_job("定时")
        except Exception as exc:
            log.error("❌ 定时采集失败: %s", exc, exc_info=True)
        finally:
            schedule_next_crawl()

    def scheduled_csv_export() -> None:
        if stop_requested.is_set():
            return
        log.info("⏰ 触发定时 CSV 导出")
        csv_exporter.export()

    def scheduled_feishu_sync() -> None:
        if stop_requested.is_set():
            return
        log.info("⏰ 触发飞书多维表格同步")
        feishu_exporter.push_pending_to_bitable()

    def schedule_next_crawl() -> None:
        if stop_requested.is_set():
            return
        scheduler = scheduler_holder["scheduler"]
        if scheduler is None:
            return
        next_run_at = datetime.now() + timedelta(minutes=config.get_crawl_interval_minutes())
        with crawl_schedule_lock:
            try:
                scheduler.add_job(
                    scheduled_crawl,
                    "date",
                    run_date=next_run_at,
                    id="crawl",
                    replace_existing=True,
                )
                log.info("📆 下轮采集已安排在 %s", next_run_at.strftime("%Y-%m-%d %H:%M:%S"))
            except Exception as exc:
                if not stop_requested.is_set():
                    log.warning("安排下轮采集失败: %s", exc)

    scheduler.add_job(
        scheduled_csv_export,
        "interval",
        hours=config.get_csv_export_interval_hours(),
        next_run_time=datetime.now(),
        id="csv-export",
    )
    if config.is_feishu_enabled():
        scheduler.add_job(
            scheduled_feishu_sync,
            "interval",
            minutes=config.get_feishu_sync_interval_minutes(),
            next_run_time=datetime.now(),
            id="feishu-sync",
        )
        log.info("✅ 飞书多维表格同步已启用（每%s分钟）", config.get_feishu_sync_interval_minutes())
    else:
        log.info("💡 飞书推送未启用（在 settings.yaml 中配置后重启）")

    scheduler.start()
    log.info("🚀 Agent 已启动！每轮采集完成后等待 %s 分钟再开始下一轮", config.get_crawl_interval_minutes())

    dashboard.start()
    stats_updater = threading.Thread(
        target=run_stats_updater,
        args=(dashboard, agent, shutdown_event),
        name="stats-updater",
        daemon=True,
    )
    stats_updater.start()

    delay = config.get_startup_delay_seconds()
    if delay > 0:
        log.info("  %s秒后执行启动首轮采集...", delay)
        if stop_requested.wait(delay):
            log.info("⏹️ 启动首轮采集已取消，准备退出")
    else:
        log.info("  立即执行启动首轮采集...")

    if not stop_requested.is_set():
        try:
            run_crawl_job("启动首轮")
            try:
                paths = csv_exporter.export()
                if paths:
                    log.info("📄 启动首轮采集后已导出 CSV: %s", ", ".join(paths))
            except Exception as exc:
                log.warning("启动首轮后的 CSV 导出失败: %s", exc)
        except Exception as exc:
            log.error("❌ 启动首轮采集失败: %s", exc, exc_info=True)
        finally:
            if not stop_requested.is_set():
                schedule_next_crawl()

    try:
        while not stop_requested.wait(1):
            pass
    finally:
        cleanup()
    return 0


def run_stats_updater(dashboard: Dashboard, agent: XHSAgent, shutdown_event: threading.Event) -> None:
    while not shutdown_event.is_set():
        try:
            dashboard.update_stats(agent.get_stats())
            time.sleep(2)
        except Exception:
            time.sleep(2)


def configure_logging(config: AppConfig) -> None:
    log_level = getattr(logging, config.get_logging_level().upper(), logging.INFO)
    log_file = Path(config.get_logging_file())
    log_file.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)

    logging.basicConfig(
        level=log_level,
        handlers=[console_handler, file_handler],
        force=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
