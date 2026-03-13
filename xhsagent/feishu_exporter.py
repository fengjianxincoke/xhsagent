from __future__ import annotations

import logging
import threading
import time
from typing import Any

import requests

from .config import AppConfig
from .database import Database
from .models import Post

log = logging.getLogger(__name__)


class FeishuExporter:
    def __init__(self, config: AppConfig, db: Database) -> None:
        self.config = config
        self.db = db
        self.http = requests.Session()
        self._token_lock = threading.Lock()
        self._cached_token = ""
        self._token_expires_at = 0.0

    def send_webhook_notification(self, post: Post) -> None:
        webhook = self.config.get_feishu_webhook().strip()
        if not webhook or "YOUR" in webhook:
            return

        template = "blue" if post.match_score >= 70 else "grey"
        payload = {"msg_type": "interactive", "card": self._build_card(post, template)}
        try:
            response = self.http.post(webhook, json=payload, timeout=(10, 15))
            if response.ok:
                log.debug("飞书通知发送成功: %s", post.short_title())
            else:
                log.warning("飞书通知失败: %s %s", response.status_code, response.text)
        except Exception as exc:
            log.error("飞书 Webhook 异常: %s", exc)

    def push_pending_to_bitable(self) -> int:
        if not self.config.is_feishu_enabled():
            return 0
        try:
            pending = self.db.get_unpushed_posts()
            if not pending:
                return 0
            log.info("推送 %s 条帖子到飞书多维表格...", len(pending))
            return self.push_to_bitable(pending)
        except Exception as exc:
            log.error("推送飞书表格失败: %s", exc)
            return 0

    def push_to_bitable(self, posts: list[Post]) -> int:
        if self._is_config_invalid():
            log.warning("飞书多维表格配置不完整，跳过")
            return 0

        token = self._get_access_token()
        if not token:
            return 0

        url = self.config.get_feishu_bitable_batch_create_url().format(
            app=self.config.get_feishu_app_token(),
            table=self.config.get_feishu_table_id(),
        )

        pushed = 0
        batch_size = 50
        for offset in range(0, len(posts), batch_size):
            batch = posts[offset : offset + batch_size]
            payload = {"records": [{"fields": self._build_fields(post)} for post in batch]}
            try:
                response = self.http.post(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    json=payload,
                    timeout=(10, 15),
                )
                data = response.json()
                if data.get("code", -1) == 0:
                    added = len((data.get("data") or {}).get("records") or [])
                    pushed += added
                    for post in batch:
                        self.db.mark_pushed(post.platform, post.post_id)
                    log.info("✅ 飞书多维表格写入 %s 条", added)
                else:
                    log.error("飞书 Bitable 写入失败: %s", data.get("msg", "unknown error"))
            except Exception as exc:
                log.error("飞书 Bitable 请求异常: %s", exc)
            time.sleep(0.5)
        return pushed

    def _build_fields(self, post: Post) -> dict[str, Any]:
        content = post.content or ""
        if len(content) > 200:
            content = content[:200]
        return {
            "平台": post.platform,
            "标题": post.title,
            "作者": post.author,
            "发布时间": post.publish_time,
            "点赞数": post.likes,
            "收藏数": post.collects,
            "评论数": post.comments,
            "帖子链接": {"link": post.url, "text": post.title or post.url},
            "搜索关键词": post.keyword,
            "AI匹配评分": post.match_score,
            "匹配理由": post.match_reason,
            "正文摘要": content,
            "采集时间": post.crawled_at,
        }

    def _build_card(self, post: Post, template: str) -> dict[str, Any]:
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"{post.score_emoji()} 发现匹配帖子 | 评分 {post.match_score}/100",
                },
                "template": template,
            },
            "elements": [
                {
                    "tag": "div",
                    "fields": [
                        {
                            "is_short": True,
                            "text": {"tag": "lark_md", "content": f"**📝 标题**\n{post.title}"},
                        },
                        {
                            "is_short": True,
                            "text": {"tag": "lark_md", "content": f"**👤 作者**\n{post.author}"},
                        },
                    ],
                },
                {
                    "tag": "div",
                    "fields": [
                        {
                            "is_short": True,
                            "text": {"tag": "lark_md", "content": f"**❤️ 点赞**\n{post.likes}"},
                        },
                        {
                            "is_short": True,
                            "text": {"tag": "lark_md", "content": f"**⭐ 收藏**\n{post.collects}"},
                        },
                    ],
                },
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": f"**🤖 匹配理由**\n{post.match_reason}"},
                },
                {"tag": "hr"},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "查看帖子"},
                            "type": "primary",
                            "url": post.url,
                        }
                    ],
                },
            ],
        }

    def _get_access_token(self) -> str:
        now = time.time()
        with self._token_lock:
            if self._cached_token and now < self._token_expires_at - 60:
                return self._cached_token

            try:
                response = self.http.post(
                    self.config.get_feishu_auth_url(),
                    json={
                        "app_id": self.config.get_feishu_app_id(),
                        "app_secret": self.config.get_feishu_app_secret(),
                    },
                    timeout=(10, 15),
                )
                data = response.json()
                if data.get("code", -1) == 0:
                    token = str(data.get("tenant_access_token", ""))
                    expire = int(data.get("expire", 7200))
                    self._cached_token = token
                    self._token_expires_at = now + expire
                    log.debug("飞书 Token 已更新")
                    return token
                log.error("获取飞书 Token 失败: %s", data.get("msg", "unknown error"))
            except Exception as exc:
                log.error("飞书认证异常: %s", exc)
            return ""

    def _is_config_invalid(self) -> bool:
        app_id = self.config.get_feishu_app_id()
        app_token = self.config.get_feishu_app_token()
        return not app_id or "YOUR" in app_id or "YOUR" in app_token
