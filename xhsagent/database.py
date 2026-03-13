from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .models import Post

log = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()
        log.info("数据库已初始化: %s", self.db_path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            if not self._table_exists(conn, "posts"):
                self._create_posts_table(conn)
            else:
                columns = self._get_columns(conn, "posts")
                if "platform" not in columns:
                    self._migrate_posts_table(conn)
            self._ensure_crawl_logs_schema(conn)
            self._ensure_comment_logs_schema(conn)

    def _create_posts_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE posts (
                platform         TEXT DEFAULT 'xiaohongshu',
                post_id          TEXT NOT NULL,
                title            TEXT,
                author           TEXT,
                author_id        TEXT,
                content          TEXT,
                publish_time     TEXT,
                likes            INTEGER DEFAULT 0,
                collects         INTEGER DEFAULT 0,
                comments         INTEGER DEFAULT 0,
                url              TEXT,
                images           TEXT DEFAULT '[]',
                keyword          TEXT,
                match_score      INTEGER DEFAULT 0,
                match_reason     TEXT,
                crawled_at       TEXT,
                pushed_to_feishu INTEGER DEFAULT 0,
                PRIMARY KEY (platform, post_id)
            )
            """
        )

    def _ensure_crawl_logs_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS crawl_logs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                platform     TEXT DEFAULT 'xiaohongshu',
                keyword      TEXT,
                started_at   TEXT,
                finished_at  TEXT,
                found_count  INTEGER DEFAULT 0,
                saved_count  INTEGER DEFAULT 0,
                status       TEXT
            )
            """
        )
        columns = self._get_columns(conn, "crawl_logs")
        if "platform" not in columns:
            conn.execute("ALTER TABLE crawl_logs ADD COLUMN platform TEXT DEFAULT 'xiaohongshu'")

    def _ensure_comment_logs_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS comment_logs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                platform     TEXT DEFAULT 'xiaohongshu',
                url          TEXT,
                comment_text TEXT,
                status       TEXT,
                message      TEXT,
                created_at   TEXT
            )
            """
        )

    def has_successful_comment(self, platform: str, url: str, comment_text: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM comment_logs
                WHERE platform=? AND url=? AND comment_text=? AND status='success'
                ORDER BY id DESC
                LIMIT 1
                """,
                (platform, url, comment_text),
            ).fetchone()
            return row is not None

    def log_comment(self, platform: str, url: str, comment_text: str, status: str, message: str) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO comment_logs
                    (platform, url, comment_text, status, message, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        platform,
                        url,
                        comment_text,
                        status,
                        message,
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
        except Exception as exc:
            log.warning("写入 comment_log 失败: %s", exc)

    def _migrate_posts_table(self, conn: sqlite3.Connection) -> None:
        conn.execute("ALTER TABLE posts RENAME TO posts_legacy")
        self._create_posts_table(conn)
        conn.execute(
            """
            INSERT INTO posts (
                platform, post_id, title, author, author_id, content, publish_time,
                likes, collects, comments, url, images, keyword,
                match_score, match_reason, crawled_at, pushed_to_feishu
            )
            SELECT
                'xiaohongshu', post_id, title, author, author_id, content, publish_time,
                likes, collects, comments, url, images, keyword,
                match_score, match_reason, crawled_at, pushed_to_feishu
            FROM posts_legacy
            """
        )
        conn.execute("DROP TABLE posts_legacy")

    def _table_exists(self, conn: sqlite3.Connection, table_name: str) -> bool:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        return row is not None

    def _get_columns(self, conn: sqlite3.Connection, table_name: str) -> set[str]:
        return {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }

    def post_exists(self, platform: str, post_id: str, dedup_days: int) -> bool:
        cutoff = (datetime.now() - timedelta(days=dedup_days)).isoformat(timespec="seconds")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM posts WHERE platform=? AND post_id=? AND crawled_at > ?",
                (platform, post_id, cutoff),
            ).fetchone()
            return row is not None

    def save_post(self, post: Post) -> bool:
        platform = (post.platform or "").strip() or "xiaohongshu"
        post_id = (post.post_id or "").strip()
        title = (post.title or "").strip()
        content = (post.content or "").strip()
        if not post_id or (not title and not content):
            log.warning("跳过无效帖子: postId='%s' title='%s'", post_id, title)
            return False

        payload = (
            platform,
            post.post_id,
            post.title,
            post.author,
            post.author_id,
            post.content,
            post.publish_time,
            post.likes,
            post.collects,
            post.comments,
            post.url,
            json.dumps(post.images or [], ensure_ascii=False),
            post.keyword,
            post.match_score,
            post.match_reason,
            post.crawled_at,
            int(post.pushed_to_feishu),
        )
        with self._lock:
            try:
                with self._connect() as conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO posts
                        (platform,post_id,title,author,author_id,content,publish_time,
                         likes,collects,comments,url,images,keyword,
                         match_score,match_reason,crawled_at,pushed_to_feishu)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        payload,
                    )
                return True
            except Exception as exc:
                log.error("保存帖子失败 %s: %s", post.post_id, exc)
                return False

    def get_unpushed_posts(self) -> list[Post]:
        return self._query_posts(
            "SELECT * FROM posts WHERE pushed_to_feishu=0 ORDER BY crawled_at DESC"
        )

    def mark_pushed(self, platform: str, post_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE posts SET pushed_to_feishu=1 WHERE platform=? AND post_id=?",
                (platform, post_id),
            )

    def get_all_posts(self, limit: int) -> list[Post]:
        return self._query_posts(
            "SELECT * FROM posts ORDER BY match_score DESC, crawled_at DESC LIMIT ?",
            (limit,),
        )

    def get_posts_by_platform(self, platform: str, limit: int) -> list[Post]:
        return self._query_posts(
            """
            SELECT * FROM posts
            WHERE platform=?
            ORDER BY match_score DESC, crawled_at DESC
            LIMIT ?
            """,
            (platform, limit),
        )

    def log_crawl(
        self,
        platform: str,
        keyword: str,
        started_at: str,
        finished_at: str,
        found: int,
        saved: int,
        status: str,
    ) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO crawl_logs
                    (platform, keyword, started_at, finished_at, found_count, saved_count, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (platform, keyword, started_at, finished_at, found, saved, status),
                )
        except Exception as exc:
            log.warning("写入 crawl_log 失败: %s", exc)

    def get_stats(self) -> dict[str, Any]:
        try:
            with self._connect() as conn:
                total = self._scalar(conn, "SELECT COUNT(*) FROM posts")
                pushed = self._scalar(conn, "SELECT COUNT(*) FROM posts WHERE pushed_to_feishu=1")
                avg_score = self._scalar_float(conn, "SELECT AVG(match_score) FROM posts")
                top_keywords = [
                    (str(row[0]), int(row[1]))
                    for row in conn.execute(
                        """
                        SELECT keyword, COUNT(*)
                        FROM posts
                        GROUP BY keyword
                        ORDER BY COUNT(*) DESC
                        LIMIT 5
                        """
                    ).fetchall()
                ]
                top_platforms = [
                    (str(row[0]), int(row[1]))
                    for row in conn.execute(
                        """
                        SELECT platform, COUNT(*)
                        FROM posts
                        GROUP BY platform
                        ORDER BY COUNT(*) DESC
                        """
                    ).fetchall()
                ]
            return {
                "total": total,
                "pushed": pushed,
                "pending": total - pushed,
                "avgScore": round(avg_score, 1),
                "topKeywords": top_keywords,
                "topPlatforms": top_platforms,
            }
        except Exception as exc:
            log.error("获取统计信息失败: %s", exc)
            return {}

    def _query_posts(self, sql: str, params: tuple[Any, ...] = ()) -> list[Post]:
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_post(row) for row in rows]

    def _row_to_post(self, row: sqlite3.Row) -> Post:
        try:
            images = json.loads(row["images"] or "[]")
        except Exception:
            images = []
        return Post(
            platform=row["platform"] or "xiaohongshu",
            post_id=row["post_id"] or "",
            title=row["title"] or "",
            author=row["author"] or "",
            author_id=row["author_id"] or "",
            content=row["content"] or "",
            publish_time=row["publish_time"] or "",
            likes=int(row["likes"] or 0),
            collects=int(row["collects"] or 0),
            comments=int(row["comments"] or 0),
            url=row["url"] or "",
            images=[str(item) for item in images if item],
            keyword=row["keyword"] or "",
            match_score=int(row["match_score"] or 0),
            match_reason=row["match_reason"] or "",
            crawled_at=row["crawled_at"] or "",
            pushed_to_feishu=bool(row["pushed_to_feishu"]),
        )

    def _scalar(self, conn: sqlite3.Connection, sql: str) -> int:
        row = conn.execute(sql).fetchone()
        return int(row[0]) if row else 0

    def _scalar_float(self, conn: sqlite3.Connection, sql: str) -> float:
        row = conn.execute(sql).fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0
