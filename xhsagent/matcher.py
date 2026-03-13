from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable

import requests

from .config import AppConfig

log = logging.getLogger(__name__)
SYSTEM_PROMPT = """
你是一个内容匹配专家。分析社交平台帖子或短视频内容，判断是否符合用户需求。

评分（0-100）：
- 90-100：完全匹配，高度相关且信息详尽
- 70-89：较好匹配，主要内容相关
- 50-69：部分匹配，有相关性但不完全
- 30-49：弱相关，关联有限
- 0-29：不匹配

严格只返回 JSON，不要其他内容：
{"score":<0-100整数>,"reason":"<匹配理由，50字以内>"}
""".strip()


@dataclass(slots=True)
class MatchResult:
    score: int
    reason: str


class AIMatcher:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.http = requests.Session()
        self.executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="matcher")

    def match(self, requirement: str, post: dict[str, Any]) -> MatchResult:
        prompt = self._build_prompt(requirement, post)
        try:
            return self._call_claude(prompt)
        except Exception as exc:
            log.warning("AI 匹配失败，降级为关键词匹配: %s", exc)
            return self._fallback_match(requirement, post)

    def batch_match(
        self,
        requirement: str,
        posts: list[dict[str, Any]],
        should_stop: Callable[[], bool] | None = None,
    ) -> list[dict[str, Any]]:
        pending = {
            self.executor.submit(self._match_one, requirement, post): post
            for post in posts
        }
        matched: list[dict[str, Any]] = []
        stop_logged = False
        while pending:
            if should_stop is not None and should_stop():
                cancelled = 0
                for future in list(pending):
                    if future.cancel():
                        cancelled += 1
                if not stop_logged:
                    log.info("  ⏹️ 收到停止请求，取消剩余 AI 匹配任务（已取消 %s 个）", cancelled)
                    stop_logged = True
                break

            done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            if not done:
                continue

            for future in done:
                try:
                    item = future.result()
                    if int(item.get("matchScore", 0)) >= self.config.get_match_threshold():
                        matched.append(item)
                except Exception as exc:
                    log.error("批量匹配任务异常: %s", exc)
        log.info(
            "  AI 匹配完成：%s 条 → %s 条符合阈值(%s分)",
            len(posts),
            len(matched),
            self.config.get_match_threshold(),
        )
        return matched

    def _match_one(self, requirement: str, post: dict[str, Any]) -> dict[str, Any]:
        result = self.match(requirement, post)
        post["matchScore"] = result.score
        post["matchReason"] = result.reason
        time.sleep(0.3)
        return post

    def _call_claude(self, prompt: str) -> MatchResult:
        api_key = self.config.get_claude_api_key().strip()
        if not api_key:
            raise RuntimeError("未配置 Claude API Key")

        payload = {
            "model": self.config.get_claude_model(),
            "max_tokens": self.config.get_claude_max_tokens(),
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
        response = self.http.post(
            self.config.get_claude_api_url(),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=(15, 30),
        )
        response.raise_for_status()
        data = response.json()
        content = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content", "")
        return self._parse_match_result(content)

    def _parse_match_result(self, content: Any) -> MatchResult:
        text = self._sanitize_match_response(self._normalize_content(content))

        for candidate in self._candidate_json_payloads(text):
            try:
                result = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(result, dict):
                return self._match_result_from_mapping(result)

        recovered = self._recover_match_result(text)
        if recovered is not None:
            log.debug("AI 响应不是严格 JSON，已按宽松规则恢复: %s", self._abbreviate_text(text, 200))
            return recovered

        raise ValueError(f"无法解析 AI 响应: {self._abbreviate_text(text, 200)}")

    def _sanitize_match_response(self, text: str) -> str:
        cleaned = re.sub(r"```json|```", "", text, flags=re.IGNORECASE).strip()
        return (
            cleaned.replace("“", '"')
            .replace("”", '"')
            .replace("‘", "'")
            .replace("’", "'")
            .replace("\u00a0", " ")
        )

    def _candidate_json_payloads(self, text: str) -> list[str]:
        candidates: list[str] = []
        if text:
            candidates.append(text)
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            candidates.append(text[start : end + 1])

        deduped: list[str] = []
        for candidate in candidates:
            normalized = candidate.strip()
            repaired = re.sub(r",\s*([}\]])", r"\1", normalized)
            for item in (normalized, repaired):
                if item and item not in deduped:
                    deduped.append(item)
        return deduped

    def _match_result_from_mapping(self, payload: dict[str, Any]) -> MatchResult:
        score = self._coerce_score(payload.get("score", 0))
        reason = str(payload.get("reason", "AI评分")).strip() or "AI评分"
        return MatchResult(score, reason)

    def _recover_match_result(self, text: str) -> MatchResult | None:
        score = self._extract_score(text)
        if score is None:
            return None
        reason = self._extract_reason(text) or "AI评分"
        return MatchResult(score, reason)

    def _extract_score(self, text: str) -> int | None:
        patterns = (
            r'["\']?score["\']?\s*[:=：]\s*(-?\d{1,3})',
            r"评分\s*[:=：]\s*(-?\d{1,3})",
        )
        for pattern in patterns:
            matched = re.search(pattern, text, flags=re.IGNORECASE)
            if matched:
                return self._coerce_score(matched.group(1))
        return None

    def _extract_reason(self, text: str) -> str:
        patterns = (
            r'["\']?reason["\']?\s*[:=：]\s*',
            r"理由\s*[:=：]\s*",
        )
        for pattern in patterns:
            matched = re.search(pattern, text, flags=re.IGNORECASE)
            if not matched:
                continue
            fragment = text[matched.end() :].strip()
            fragment = re.split(
                r',\s*["\']?(?:score|reason|理由|comment|explanation)["\']?\s*[:=：]',
                fragment,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0]
            fragment = fragment.replace("\\n", " ").replace("\n", " ").replace("\r", " ")
            fragment = re.sub(r"\s+", " ", fragment).strip()
            fragment = fragment.rstrip(",} ]").strip()
            if fragment[:1] in {'"', "'"}:
                fragment = fragment[1:].strip()
            if fragment[-1:] in {'"', "'"}:
                fragment = fragment[:-1].strip()
            if fragment:
                return fragment
        return ""

    def _coerce_score(self, value: Any) -> int:
        if isinstance(value, (int, float)):
            score = int(value)
        else:
            matched = re.search(r"-?\d+", str(value))
            score = int(matched.group(0)) if matched else 0
        return max(0, min(100, score))

    def _abbreviate_text(self, text: str, limit: int) -> str:
        text = re.sub(r"\s+", " ", text).strip()
        return text if len(text) <= limit else f"{text[: limit - 3]}..."

    def _normalize_content(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        return str(content)

    def _build_prompt(self, requirement: str, post: dict[str, Any]) -> str:
        content = str(post.get("content", ""))
        if len(content) > 500:
            content = f"{content[:500]}..."
        return f"""
用户需求：
{requirement}

帖子信息：
- 标题：{post.get("title", "")}
- 平台：{post.get("platform", "")}
- 作者：{post.get("author", "")}
- 正文：{content}
- 点赞：{post.get("likes", 0)}  收藏：{post.get("collects", 0)}  评论：{post.get("comments", 0)}
- 搜索关键词：{post.get("keyword", "")}

请判断该帖子与需求的匹配程度，返回 JSON。
""".strip()

    def _fallback_match(self, requirement: str, post: dict[str, Any]) -> MatchResult:
        text = f"{post.get('title', '')} {post.get('content', '')}".lower()
        words = [item for item in re.split(r"[\s，,。.！!？?]+", requirement) if item]
        match_count = sum(1 for word in words if word.lower() in text)
        denominator = max(len(words), 1)
        score = min(80, int(match_count * 100 / denominator))
        return MatchResult(score, f"关键词匹配({match_count}/{denominator}个词)")

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)
