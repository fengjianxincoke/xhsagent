from __future__ import annotations

import csv
import logging
from datetime import datetime
from pathlib import Path

from .database import Database
from .models import Post

log = logging.getLogger(__name__)

HEADERS = [
    "平台",
    "帖子ID",
    "标题",
    "作者",
    "正文摘要",
    "发布时间",
    "点赞数",
    "收藏数",
    "评论数",
    "帖子链接",
    "搜索关键词",
    "AI匹配评分",
    "匹配理由",
    "采集时间",
]


class CsvExporter:
    def __init__(self, db: Database, output_dir: str) -> None:
        self.db = db
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def export(self) -> list[str]:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        exported: list[str] = []
        for platform in ("xiaohongshu", "douyin"):
            path = self.export_platform(platform, f"{platform}_posts_{stamp}.csv", 5000)
            if path:
                exported.append(path)
        return exported

    def export_to(self, filename: str, limit: int) -> str:
        file_path = self.output_dir / filename
        try:
            posts = self.db.get_all_posts(limit)
            with file_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(HEADERS)
                for post in posts:
                    writer.writerow(self._to_row(post))
            log.info("📄 CSV 导出完成: %s (%s 条)", file_path, len(posts))
            return str(file_path)
        except Exception as exc:
            log.error("CSV 导出失败: %s", exc)
            return ""

    def export_platform(self, platform: str, filename: str, limit: int) -> str:
        file_path = self.output_dir / filename
        try:
            posts = self.db.get_posts_by_platform(platform, limit)
            if not posts:
                log.info("📄 跳过 %s CSV 导出: 无数据", platform)
                return ""
            with file_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(HEADERS)
                for post in posts:
                    writer.writerow(self._to_row(post))
            log.info("📄 %s CSV 导出完成: %s (%s 条)", platform, file_path, len(posts))
            return str(file_path)
        except Exception as exc:
            log.error("%s CSV 导出失败: %s", platform, exc)
            return ""

    def _to_row(self, post: Post) -> list[str]:
        content = post.content or ""
        if len(content) > 200:
            content = f"{content[:200]}..."
        return [
            post.platform,
            post.post_id,
            post.title,
            post.author,
            content,
            post.publish_time,
            str(post.likes),
            str(post.collects),
            str(post.comments),
            post.url,
            post.keyword,
            str(post.match_score),
            post.match_reason,
            post.crawled_at,
        ]
