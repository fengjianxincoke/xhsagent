from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


class AppConfig:
    def __init__(self, settings_path: str | Path | None = None) -> None:
        root = Path(__file__).resolve().parent.parent
        if settings_path:
            self.settings_path = Path(settings_path)
        else:
            self.settings_path = root / "settings.yaml"
        try:
            with self.settings_path.open("r", encoding="utf-8") as handle:
                self.raw: dict[str, Any] = yaml.safe_load(handle) or {}
            log.info("配置加载成功: %s", self.settings_path)
        except Exception as exc:
            raise RuntimeError(f"加载配置失败: {exc}") from exc

    def get_requirement(self) -> str:
        return self._str("requirement")

    def get_keywords(self) -> list[str]:
        value = self.raw.get("keywords", [])
        return [str(item) for item in value] if isinstance(value, list) else []

    def get_claude_api_key(self) -> str:
        return self._str("claude.apiKey")

    def get_claude_model(self) -> str:
        return self._str("claude.model")

    def get_claude_api_url(self) -> str:
        return self._str("claude.apiUrl")

    def get_match_threshold(self) -> int:
        return self._num("claude.matchThreshold", 60)

    def get_claude_max_tokens(self) -> int:
        return self._num("claude.maxTokens", 300)

    def is_douyin_enabled(self) -> bool:
        value = self._resolve("douyin.enabled", None)
        if value is None:
            return False
        return self._bool("douyin.enabled")

    def get_douyin_cooldown_minutes(self) -> int:
        return self._num("douyin.cooldownMinutes", 10)

    def is_feishu_enabled(self) -> bool:
        return self._bool("feishu.enabled")

    def get_feishu_webhook(self) -> str:
        return self._str("feishu.webhookUrl")

    def get_feishu_app_id(self) -> str:
        return self._str("feishu.appId")

    def get_feishu_app_secret(self) -> str:
        return self._str("feishu.appSecret")

    def get_feishu_app_token(self) -> str:
        return self._str("feishu.appToken")

    def get_feishu_table_id(self) -> str:
        return self._str("feishu.tableId")

    def get_feishu_auth_url(self) -> str:
        value = self._str("feishu.authUrl")
        return value if value else "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"

    def get_feishu_bitable_batch_create_url(self) -> str:
        value = self._str("feishu.bitableBatchCreateUrl")
        if value:
            return value
        return "https://open.feishu.cn/open-apis/bitable/v1/apps/{app}/tables/{table}/records/batch_create"

    def get_crawl_interval_minutes(self) -> int:
        return self._num("schedule.crawlIntervalMinutes", 30)

    def get_csv_export_interval_hours(self) -> int:
        return self._num("schedule.csvExportIntervalHours", 6)

    def get_feishu_sync_interval_minutes(self) -> int:
        return self._num("schedule.feishuSyncIntervalMinutes", 5)

    def get_max_posts_per_keyword(self) -> int:
        return self._num("schedule.maxPostsPerKeyword", 20)

    def get_startup_delay_seconds(self) -> int:
        return self._num("schedule.startupDelaySeconds", 3)

    def is_browser_headless(self) -> bool:
        return self._bool("browser.headless")

    def get_browser_locale(self) -> str:
        return self._str("browser.locale")

    def get_viewport_width(self) -> int:
        return self._num("browser.viewportWidth", 1280)

    def get_viewport_height(self) -> int:
        return self._num("browser.viewportHeight", 900)

    def get_min_delay_ms(self) -> int:
        return self._num("browser.minDelayMs", 1500)

    def get_max_delay_ms(self) -> int:
        return self._num("browser.maxDelayMs", 4000)

    def is_save_session(self) -> bool:
        return self._bool("browser.saveSession")

    def get_session_file(self) -> str:
        return self._str("browser.sessionFile")

    def get_xhs_session_file(self) -> str:
        value = self._str("browser.xhsSessionFile")
        return value if value.strip() else "data/session_xhs.json"

    def get_douyin_session_file(self) -> str:
        value = self._str("browser.douyinSessionFile")
        return value if value.strip() else "data/session_douyin.json"

    def get_browser_profile_dir(self) -> str:
        value = self._str("browser.profileDir")
        return value if value.strip() else "data/browser-profile"

    def get_browser_proxy_mode(self) -> str:
        value = self._str("browser.proxy.mode").strip().lower()
        return value if value in {"auto", "direct", "custom"} else "auto"

    def get_browser_proxy_server(self) -> str:
        return self._str("browser.proxy.server").strip()

    def get_browser_proxy_bypass(self) -> str:
        return self._str("browser.proxy.bypass").strip()

    def get_browser_proxy_username(self) -> str:
        return self._str("browser.proxy.username").strip()

    def get_browser_proxy_password(self) -> str:
        return self._str("browser.proxy.password").strip()

    def get_db_path(self) -> str:
        return self._str("storage.dbPath")

    def get_csv_output_dir(self) -> str:
        return self._str("storage.csvOutputDir")

    def get_dedup_days(self) -> int:
        return self._num("storage.dedupDays", 7)

    def get_logging_level(self) -> str:
        value = self._str("logging.level")
        return value if value else "INFO"

    def get_logging_file(self) -> str:
        value = self._str("logging.file")
        return value if value else "data/agent.log"

    def is_comment_enabled(self) -> bool:
        return self._bool("comments.enabled")

    def use_latest_comment_csv_only(self) -> bool:
        value = self._resolve("comments.latestCsvOnly", True)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def get_comment_max_per_platform(self) -> int:
        return self._num("comments.maxPerPlatform", 0)

    def is_platform_comment_enabled(self, platform: str) -> bool:
        platform = platform.strip().lower()
        if platform not in {"xiaohongshu", "douyin"}:
            return False
        if not self.is_comment_enabled():
            return False
        enabled = self._resolve(f"comments.{platform}.enabled", None)
        if enabled is None:
            return bool(self.get_platform_comment_content(platform))
        return self._bool(f"comments.{platform}.enabled")

    def get_platform_comment_content(self, platform: str) -> str:
        platform = platform.strip().lower()
        return self._str(f"comments.{platform}.content").strip()

    def get_comment_platforms(self) -> list[str]:
        return [
            platform
            for platform in ("xiaohongshu", "douyin")
            if self.is_platform_comment_enabled(platform) and self.get_platform_comment_content(platform)
        ]

    def _resolve(self, path: str, default: Any = None) -> Any:
        current: Any = self.raw
        for key in path.split("."):
            if not isinstance(current, dict) or key not in current:
                return default
            current = current[key]
        return current

    def _str(self, path: str) -> str:
        value = self._resolve(path, "")
        return "" if value is None else str(value)

    def _num(self, path: str, default: int) -> int:
        value = self._resolve(path, default)
        if isinstance(value, (int, float)):
            return int(value)
        try:
            return int(str(value))
        except Exception:
            return default

    def _bool(self, path: str) -> bool:
        value = self._resolve(path, False)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in {"1", "true", "yes", "on"}
        return bool(value)
