from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Callable

from .browser import DOUYINBrowser, XHSBrowser
from .config import AppConfig
from .database import Database
from .matcher import AIMatcher
from .models import Post

log = logging.getLogger(__name__)


class XHSAgent:
    def __init__(self, config: AppConfig, db: Database) -> None:
        self.config = config
        self.db = db
        self.matcher = AIMatcher(config)
        self.platform_executors = {
            "xiaohongshu": ThreadPoolExecutor(max_workers=1, thread_name_prefix="xhs-platform"),
            "douyin": ThreadPoolExecutor(max_workers=1, thread_name_prefix="douyin-platform"),
        }
        self.platform_browsers = {
            "xiaohongshu": XHSBrowser(config, "xiaohongshu"),
            "douyin": DOUYINBrowser(config, "douyin"),
        }
        self.on_post_saved: Callable[[Post], None] | None = None

        self._cycle_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop_requested = threading.Event()
        self._started_platforms: set[str] = set()
        self.total_crawled = 0
        self.total_saved = 0
        self.current_keyword = ""
        self.status = "idle"
        self.last_run = "—"
        self.platform_cooldown_until: dict[str, float] = {}

    def set_on_post_saved(self, callback: Callable[[Post], None] | None) -> None:
        self.on_post_saved = callback

    def start(self, platforms: list[str] | None = None) -> None:
        self._stop_requested.clear()
        target_platforms = platforms or self._enabled_platforms()
        for platform in target_platforms:
            if platform not in self.platform_browsers or platform in self._started_platforms:
                continue
            self.platform_executors[platform].submit(self.platform_browsers[platform].start).result()
            self._started_platforms.add(platform)
        log.info("🤖 Agent 启动")
        log.info("📋 需求: %s", self.config.get_requirement())
        log.info("🔑 关键词: %s", self.config.get_keywords())

    def ensure_logged_in(self) -> bool:
        return self.ensure_platform_ready("xiaohongshu")

    def ensure_platform_ready(self, platform: str) -> bool:
        browser = self.platform_browsers[platform]
        try:
            return self.platform_executors[platform].submit(browser.ensure_logged_in).result()
        except Exception as exc:
            log.error("❌ 平台 [%s] 状态校验异常: %s", platform, exc, exc_info=True)
            return False

    def request_stop(self) -> None:
        self._stop_requested.set()
        for platform in self._started_platforms:
            self.platform_browsers[platform].request_stop()

    def run_comment_jobs(self, jobs: list[dict[str, str]]) -> dict[str, int]:
        summary = {"success": 0, "skipped": 0, "failed": 0}
        if not jobs:
            return summary

        for index, job in enumerate(jobs):
            if self._stop_requested.is_set():
                log.info("⏹️ 收到停止请求，终止评论任务")
                break

            platform = str(job.get("platform", "")).strip().lower()
            url = str(job.get("url", "")).strip()
            content = str(job.get("content", "")).strip()
            if platform not in self.platform_browsers or not url or not content:
                summary["failed"] += 1
                log.warning("💬 跳过无效评论任务: platform=%s url=%s", platform, url)
                continue

            if self.db.has_successful_comment(platform, url, content):
                summary["skipped"] += 1
                log.info("💬 评论任务已成功执行过，跳过: [%s] %s", platform, url)
                continue

            try:
                ok, message = self.platform_executors[platform].submit(
                    self.platform_browsers[platform].comment_on_url,
                    url,
                    content,
                ).result()
            except Exception as exc:
                ok = False
                message = str(exc)

            status = "success" if ok else "failed"
            self.db.log_comment(platform, url, content, status, message)
            if ok:
                summary["success"] += 1
                log.info("💬 评论成功: [%s] %s", platform, url)
            else:
                summary["failed"] += 1
                log.warning("💬 评论失败: [%s] %s | %s", platform, url, message)
            if index < len(jobs) - 1 and self._sleep_or_stop(random.uniform(3.0, 6.0)):
                log.info("⏹️ 收到停止请求，终止后续评论任务")
                break
        return summary

    def run_crawl_cycle(self) -> int:
        with self._cycle_lock:
            self._set_state(status="crawling", last_run=datetime.now().isoformat(timespec="seconds"))
            cycle_saved = 0
            try:
                for keyword in self.config.get_keywords():
                    if self._stop_requested.is_set():
                        log.info("⏹️ 收到停止请求，结束当前采集轮次")
                        break
                    self._set_state(current_keyword=keyword)
                    try:
                        futures = {
                            self.platform_executors[platform].submit(self._crawl_keyword, platform, keyword): platform
                            for platform in self._enabled_platforms()
                            if not self._is_platform_cooling_down(platform)
                        }
                        for platform in self._enabled_platforms():
                            if platform not in futures.values() and self._is_platform_cooling_down(platform):
                                remaining = int(
                                    max(0, self.platform_cooldown_until.get(platform, 0.0) - time.time()) / 60
                                ) + 1
                                log.warning("平台 [%s] 处于风控冷却中，跳过当前关键词 [%s]（约%s分钟后恢复）", platform, keyword, remaining)
                        for future in as_completed(futures):
                            platform = futures[future]
                            if self._stop_requested.is_set():
                                log.info("⏹️ 收到停止请求，结束当前平台采集")
                                break
                            try:
                                cycle_saved += future.result()
                            except Exception as exc:
                                log.error("平台 [%s] 关键词 [%s] 采集出错: %s", platform, keyword, exc)
                        if self._sleep_or_stop(random.uniform(3.0, 6.0)):
                            log.info("⏹️ 收到停止请求，跳过后续关键词")
                            break
                    except Exception as exc:
                        log.error("关键词 [%s] 采集出错: %s", keyword, exc)
            finally:
                self._set_state(status="idle")
            log.info("✅ 本轮采集完成，新增 %s 条", cycle_saved)
            return cycle_saved

    def _crawl_keyword(self, platform: str, keyword: str) -> int:
        started_at = datetime.now().isoformat(timespec="seconds")
        saved_count = 0
        found_count = 0
        crawl_status = "success"

        log.info("── 开始采集平台 [%s] 关键词: [%s] ──", platform, keyword)
        try:
            raw_posts = self._search_platform_posts(platform, keyword)
            if platform == "douyin" and self.platform_browsers[platform].consume_douyin_risk_triggered():
                self._set_platform_cooldown(platform)
                crawl_status = "blocked"
            found_count = len(raw_posts)
            if self._stop_requested.is_set():
                crawl_status = "stopped"
                log.info("  [%s] 收到停止请求，终止当前关键词", keyword)
                return 0

            if not raw_posts:
                crawl_status = "empty"
                log.warning("  [%s] 未找到帖子", keyword)
                return 0

            with self._state_lock:
                self.total_crawled += len(raw_posts)

            new_posts: list[dict[str, Any]] = []
            for post in raw_posts:
                post["platform"] = platform
                post_id = str(post.get("postId", ""))
                if post_id and not self.db.post_exists(platform, post_id, self.config.get_dedup_days()):
                    new_posts.append(post)

            log.info("  去重后新帖: %s/%s", len(new_posts), len(raw_posts))
            if not new_posts:
                crawl_status = "deduped"
                return 0
            if self._stop_requested.is_set():
                crawl_status = "stopped"
                log.info("  [%s] 收到停止请求，跳过详情补全与匹配", keyword)
                return 0

            enriched = self._enrich_posts(new_posts)
            matched = self.matcher.batch_match(
                self.config.get_requirement(),
                enriched,
                should_stop=self._stop_requested.is_set,
            )

            for item in matched:
                post = Post.from_mapping(item)
                if self.db.save_post(post):
                    saved_count += 1
                    with self._state_lock:
                        self.total_saved += 1
                    log.info(
                        "  ✨ [%s][%s] 评分:%s | %s",
                        post.platform,
                        post.short_title(),
                        post.match_score,
                        post.match_reason,
                    )
                    if self.on_post_saved:
                        threading.Thread(
                            target=self._safe_post_saved_callback,
                            args=(post,),
                            name="post-saved-callback",
                            daemon=True,
                        ).start()

            if not matched:
                crawl_status = "filtered"
            return saved_count
        except Exception:
            crawl_status = "error"
            raise
        finally:
            self.db.log_crawl(
                platform,
                keyword,
                started_at,
                datetime.now().isoformat(timespec="seconds"),
                found_count,
                saved_count,
                crawl_status,
            )

    def _safe_post_saved_callback(self, post: Post) -> None:
        try:
            assert self.on_post_saved is not None
            self.on_post_saved(post)
        except Exception as exc:
            log.debug("新帖回调执行失败: %s", exc)

    def _enrich_posts(self, posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self._stop_requested.is_set():
            return []
        return [self._enrich_single_post(post) for post in posts if not self._stop_requested.is_set()]

    def _enrich_single_post(self, post: dict[str, Any]) -> dict[str, Any]:
        if self._stop_requested.is_set():
            return post
        url = str(post.get("url", ""))
        platform = str(post.get("platform", "xiaohongshu"))
        browser = self.platform_browsers[platform]
        missing_content = not str(post.get("content", "")).strip()
        missing_author = not str(post.get("author", "")).strip()
        missing_stats = (
            int(post.get("likes", 0) or 0) == 0
            and int(post.get("collects", 0) or 0) == 0
            and int(post.get("comments", 0) or 0) == 0
        )
        if url and (missing_content or missing_author or missing_stats):
            detail = browser.fetch_post_detail(url)
            post.update(detail)
            time.sleep(1)
        return post

    def _enabled_platforms(self) -> list[str]:
        platforms = ["xiaohongshu"]
        if self.config.is_douyin_enabled():
            platforms.append("douyin")
        return platforms

    def _search_platform_posts(self, platform: str, keyword: str) -> list[dict[str, Any]]:
        browser = self.platform_browsers[platform]
        return browser.search_posts(keyword, self.config.get_max_posts_per_keyword())

    def _is_platform_cooling_down(self, platform: str) -> bool:
        if platform != "douyin":
            return False
        return time.time() < self.platform_cooldown_until.get(platform, 0.0)

    def _set_platform_cooldown(self, platform: str) -> None:
        if platform != "douyin":
            return
        cooldown_seconds = max(1, self.config.get_douyin_cooldown_minutes()) * 60
        until = time.time() + cooldown_seconds
        self.platform_cooldown_until[platform] = until
        log.warning(
            "平台 [%s] 已进入风控冷却，持续 %s 分钟",
            platform,
            self.config.get_douyin_cooldown_minutes(),
        )

    def get_stats(self) -> dict[str, Any]:
        stats = dict(self.db.get_stats())
        with self._state_lock:
            stats.update(
                {
                    "totalCrawled": self.total_crawled,
                    "totalSaved": self.total_saved,
                    "currentKeyword": self.current_keyword,
                    "status": self.status,
                    "lastRun": self.last_run,
                }
            )
        return stats

    def _set_state(
        self,
        *,
        current_keyword: str | None = None,
        status: str | None = None,
        last_run: str | None = None,
    ) -> None:
        with self._state_lock:
            if current_keyword is not None:
                self.current_keyword = current_keyword
            if status is not None:
                self.status = status
            if last_run is not None:
                self.last_run = last_run

    def _sleep_or_stop(self, seconds: float) -> bool:
        return self._stop_requested.wait(max(0.0, seconds))

    def close(self, *, interrupted: bool = False) -> None:
        self.request_stop()
        self.matcher.shutdown()
        for platform in self._started_platforms:
            try:
                self.platform_executors[platform].submit(
                    self.platform_browsers[platform].close,
                    interrupted=interrupted,
                ).result()
            except Exception:
                pass
        for platform in self.platform_executors.values():
            platform.shutdown(wait=False, cancel_futures=True)
        self._started_platforms.clear()
        log.info("🤖 Agent 已关闭")
