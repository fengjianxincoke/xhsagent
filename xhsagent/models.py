from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping


def iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass(slots=True)
class Post:
    platform: str = "xiaohongshu"
    post_id: str = ""
    title: str = ""
    author: str = ""
    author_id: str = ""
    content: str = ""
    publish_time: str = ""
    likes: int = 0
    collects: int = 0
    comments: int = 0
    url: str = ""
    images: list[str] = field(default_factory=list)
    keyword: str = ""
    match_score: int = 0
    match_reason: str = ""
    crawled_at: str = field(default_factory=iso_now)
    pushed_to_feishu: bool = False

    def short_title(self) -> str:
        if not self.title:
            return ""
        return f"{self.title[:25]}…" if len(self.title) > 25 else self.title

    def score_emoji(self) -> str:
        if self.match_score >= 85:
            return "🔥"
        if self.match_score >= 70:
            return "✨"
        return "📌"

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "Post":
        images = data.get("images") or []
        if not isinstance(images, list):
            images = []
        return cls(
            platform=str(data.get("platform", "xiaohongshu")) or "xiaohongshu",
            post_id=str(data.get("postId", "")),
            title=str(data.get("title", "")),
            author=str(data.get("author", "")),
            author_id=str(data.get("authorId", "")),
            content=str(data.get("content", "")),
            publish_time=str(data.get("publishTime", "")),
            likes=to_int(data.get("likes", 0)),
            collects=to_int(data.get("collects", 0)),
            comments=to_int(data.get("comments", 0)),
            url=str(data.get("url", "")),
            images=[str(item) for item in images if item],
            keyword=str(data.get("keyword", "")),
            match_score=to_int(data.get("matchScore", 0)),
            match_reason=str(data.get("matchReason", "")),
            crawled_at=str(data.get("crawledAt", iso_now())),
            pushed_to_feishu=bool(data.get("pushedToFeishu", False)),
        )


def to_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value))
    except Exception:
        return 0
