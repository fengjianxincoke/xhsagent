from __future__ import annotations

import json
import logging
import random
import re
import time
import threading
from pathlib import Path
from typing import Any
from urllib.parse import quote

from playwright.sync_api import Browser, BrowserContext, Error as PlaywrightError, Page, Response, TimeoutError, sync_playwright

from .config import AppConfig

log = logging.getLogger(__name__)


class BaseBrowser:
    def __init__(self, config: AppConfig, platform_name: str = "xiaohongshu") -> None:
        self.config = config
        self.platform_name = platform_name
        self.playwright = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.authenticated_session = False
        self.stop_requested = False
        self.capture_search_responses = False
        self.active_search_keyword = ""
        self.active_search_platform = "xiaohongshu"
        self.active_search_cards: dict[str, dict[str, Any]] = {}
        self._risk_lock = threading.Lock()
        self._douyin_risk_triggered = False

    # Browser lifecycle and platform hooks.
    def start(self) -> None:
        self.stop_requested = False
        self.playwright = sync_playwright().start()
        profile_dir = self.get_profile_dir()
        session_path = Path(self.config.get_session_file())
        platform_session_path = self.get_platform_session_path()
        has_session_snapshot = self.config.is_save_session() and session_path.is_file()
        profile_dir.mkdir(parents=True, exist_ok=True)

        launch_args = [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--disable-setuid-sandbox",
            "--no-first-run",
        ]
        launch_proxy = self.build_launch_proxy(launch_args)
        headers = {"Accept-Language": "zh-CN,zh;q=0.9"}
        viewport = {
            "width": self.config.get_viewport_width(),
            "height": self.config.get_viewport_height(),
        }

        if self.config.is_browser_headless():
            context_kwargs: dict[str, Any] = {
                "locale": self.config.get_browser_locale(),
                "viewport": viewport,
                "user_agent": self.build_user_agent(),
                "extra_http_headers": headers,
            }
            if has_session_snapshot:
                context_kwargs["storage_state"] = str(session_path)
                log.info("已发现总 Session 快照: %s", session_path)
            elif self.config.is_save_session():
                log.warning("未找到 Session 快照: %s，本次启动可能需要人工登录", session_path)

            launch_kwargs: dict[str, Any] = {
                "headless": True,
                "args": launch_args,
            }
            if launch_proxy:
                launch_kwargs["proxy"] = launch_proxy

            self.browser = self.playwright.chromium.launch(**launch_kwargs)
            self.context = self.browser.new_context(**context_kwargs)
            log.info("已启动无头浏览器（storageStateLoaded=%s）", has_session_snapshot)
        else:
            launch_kwargs: dict[str, Any] = {
                "user_data_dir": str(profile_dir),
                "headless": False,
                "locale": self.config.get_browser_locale(),
                "viewport": viewport,
                "user_agent": self.build_user_agent(),
                "extra_http_headers": headers,
                "args": launch_args,
            }
            if launch_proxy:
                launch_kwargs["proxy"] = launch_proxy

            self.context = self.playwright.chromium.launch_persistent_context(**launch_kwargs)
            self.browser = self.context.browser
            log.info("已加载%s浏览器 Profile: %s", self.platform_name, profile_dir)

        if self.config.is_save_session():
            self.log_session_presence(self.get_platform_session_label(), platform_session_path)

        if has_session_snapshot and not self.config.is_browser_headless():
            self.restore_session_from_file(session_path)
        self.restore_platform_session()

        assert self.context is not None
        self.context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins',   {get: () => [1,2,3,4,5]});
            window.chrome = {runtime: {}};
            """
        )

        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.on("response", self.capture_search_response)
        log.info("✅ 浏览器已启动 (headless=%s)", self.config.is_browser_headless())
        self.log_platform_session_summary()

    def get_profile_dir(self) -> Path:
        base = Path(self.config.get_browser_profile_dir())
        if self.platform_name == "xiaohongshu":
            return base
        return base.parent / f"{base.name}-{self.platform_name}"

    def build_launch_proxy(self, launch_args: list[str]) -> dict[str, str] | None:
        mode = self.config.get_browser_proxy_mode()
        if mode == "direct":
            launch_args.append("--no-proxy-server")
            log.info("浏览器代理模式: direct（禁用系统代理）")
            return None

        if mode == "custom":
            server = self.config.get_browser_proxy_server()
            if not server:
                log.warning("browser.proxy.mode=custom 但未配置 browser.proxy.server，已回退为 auto")
                return None

            proxy: dict[str, str] = {"server": server}
            bypass = self.config.get_browser_proxy_bypass()
            username = self.config.get_browser_proxy_username()
            password = self.config.get_browser_proxy_password()
            if bypass:
                proxy["bypass"] = bypass
            if username:
                proxy["username"] = username
            if password:
                proxy["password"] = password
            log.info("浏览器代理模式: custom（server=%s）", self.mask_proxy_server(server))
            return proxy

        log.info("浏览器代理模式: auto（跟随系统网络设置）")
        return None

    def mask_proxy_server(self, server: str) -> str:
        return re.sub(r"//([^:@/]+):([^@/]+)@", r"//\1:***@", server)

    def goto_with_handling(self, url: str, *, page: Page | None = None) -> bool:
        active_page = page or self.page
        assert active_page is not None
        try:
            active_page.goto(url, wait_until="domcontentloaded")
            return True
        except PlaywrightError as exc:
            self.log_navigation_error(url, exc)
            return False

    def log_navigation_error(self, url: str, exc: Exception) -> None:
        message = str(exc)
        lowered = message.lower()

        log.error("❌ %s 页面访问失败: %s", self.get_platform_label(), url)
        if "err_tunnel_connection_failed" in lowered:
            log.error("   当前浏览器代理隧道不可用，Chromium 无法建立到目标站点的连接。")
            log.error("   可在 settings.yaml 中设置 browser.proxy.mode: direct 关闭系统代理，或改为 custom 并填写可用代理。")
        elif "err_proxy_connection_failed" in lowered:
            log.error("   当前代理服务器连接失败，请检查代理地址、端口和本机代理软件状态。")
        elif "err_name_not_resolved" in lowered:
            log.error("   域名解析失败，请检查 DNS、网络连接或代理配置。")
        elif "err_internet_disconnected" in lowered:
            log.error("   当前网络未连接，浏览器无法访问目标站点。")
        elif "timeout" in lowered:
            log.error("   页面打开超时，请检查网络是否可访问目标站点。")
        else:
            log.error("   Playwright 导航异常: %s", message)

    def ensure_logged_in(self) -> bool:
        raise NotImplementedError

    def search_posts(self, keyword: str, max_count: int) -> list[dict[str, Any]]:
        raise NotImplementedError

    def fetch_post_detail(self, post_url: str) -> dict[str, Any]:
        raise NotImplementedError

    def comment_on_url(self, post_url: str, content: str) -> tuple[bool, str]:
        raise NotImplementedError

    def looks_like_search_api(self, url: str) -> bool:
        raise NotImplementedError

    def extract_search_cards_from_json(
        self,
        root: Any,
        cards: dict[str, dict[str, Any]],
        keyword: str,
    ) -> int:
        raise NotImplementedError

    def get_platform_label(self) -> str:
        return self.platform_name

    def get_default_platform(self) -> str:
        return self.platform_name

    def normalize_platform_post_id(self, post_id: str) -> str:
        raise NotImplementedError

    def get_platform_session_label(self) -> str:
        return self.get_platform_label()

    def get_platform_session_path(self) -> Path:
        raise NotImplementedError

    def get_platform_session_domains(self) -> tuple[str, ...]:
        raise NotImplementedError

    def has_platform_session_cookie(self) -> bool:
        raise NotImplementedError

    def wait_for_any_selector(self, *selectors: str, page: Page | None = None) -> bool:
        active_page = page or self.page
        assert active_page is not None
        for selector in selectors:
            try:
                active_page.wait_for_selector(selector, timeout=5000)
                log.info("  ✅ 选择器命中: %s", selector)
                return True
            except TimeoutError:
                continue
            except Exception:
                continue
        return False

    def click_first_visible(
        self,
        selectors: tuple[str, ...],
        *,
        page: Page | None = None,
        require_enabled: bool = False,
    ) -> str:
        active_page = page or self.page
        assert active_page is not None
        for selector in selectors:
            try:
                locator = active_page.locator(selector).first
                if not locator.is_visible():
                    continue
                if require_enabled:
                    try:
                        if locator.is_disabled():
                            continue
                    except Exception:
                        pass
                    try:
                        if locator.get_attribute("disabled") is not None:
                            continue
                        if str(locator.get_attribute("aria-disabled") or "").lower() == "true":
                            continue
                    except Exception:
                        pass
                locator.scroll_into_view_if_needed(timeout=2000)
                locator.click(timeout=3000)
                return selector
            except Exception:
                continue
        return ""

    def fill_first_editable(self, selectors: tuple[str, ...], text: str, *, page: Page | None = None) -> str:
        active_page = page or self.page
        assert active_page is not None
        for selector in selectors:
            try:
                locator = active_page.locator(selector).first
                if not locator.is_visible():
                    continue
                locator.scroll_into_view_if_needed(timeout=2000)
                if self.type_into_locator(locator, text, page=active_page):
                    return selector
                locator.click(timeout=3000)
                tag_name = str(locator.evaluate("(el) => el.tagName.toLowerCase()"))
                is_content_editable = bool(
                    locator.evaluate(
                        "(el) => el.isContentEditable || el.getAttribute('contenteditable') === 'true'"
                    )
                )
                if tag_name in {"textarea", "input"}:
                    locator.fill(text, timeout=3000)
                    if self.locator_has_text(locator, text):
                        return selector
                if is_content_editable:
                    locator.evaluate(
                        """
                        (el, value) => {
                            el.focus();
                            if ((el.innerText || el.textContent || '').trim() !== value.trim()) {
                                el.textContent = value;
                            }
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                        """,
                        text,
                    )
                    if self.locator_has_text(locator, text):
                        return selector
            except Exception:
                continue
        return ""

    def locator_has_text(self, locator: Any, text: str) -> bool:
        normalized_text = re.sub(r"\s+", " ", text).strip()
        if not normalized_text:
            return False
        try:
            current_value = locator.evaluate(
                """
                (el) => {
                    const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                    if (el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement) {
                        return normalize(el.value || '');
                    }
                    return normalize(el.innerText || el.textContent || '');
                }
                """
            )
        except Exception:
            return False
        current_text = re.sub(r"\s+", " ", str(current_value or "")).strip()
        return bool(current_text and (current_text == normalized_text or normalized_text in current_text))

    def has_text_in_selectors(self, selectors: tuple[str, ...], text: str, *, page: Page | None = None) -> bool:
        active_page = page or self.page
        assert active_page is not None
        for selector in selectors:
            try:
                locators = active_page.locator(selector)
                count = min(locators.count(), 5)
                for index in range(count):
                    locator = locators.nth(index)
                    if not locator.is_visible():
                        continue
                    if self.locator_has_text(locator, text):
                        return True
            except Exception:
                continue
        return False

    def type_into_locator(self, locator: Any, text: str, *, page: Page | None = None) -> bool:
        active_page = page or self.page
        assert active_page is not None
        try:
            locator.scroll_into_view_if_needed(timeout=2000)
            locator.click(timeout=3000)
            tag_name = str(locator.evaluate("(el) => (el.tagName || '').toLowerCase()"))
            is_content_editable = bool(
                locator.evaluate(
                    "(el) => el.isContentEditable || el.getAttribute('contenteditable') === 'true' || el.getAttribute('contenteditable') === 'plaintext-only'"
                )
            )
            if tag_name in {"textarea", "input"}:
                locator.fill("", timeout=3000)
                try:
                    locator.type(text, delay=random.randint(40, 90), timeout=3000)
                except Exception:
                    locator.fill(text, timeout=3000)
                active_page.wait_for_timeout(200)
                return self.locator_has_text(locator, text)
            if is_content_editable:
                locator.evaluate(
                    """
                    (el) => {
                        el.focus();
                        el.textContent = '';
                        el.dispatchEvent(new InputEvent('input', { bubbles: true, data: null, inputType: 'deleteContentBackward' }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                    """
                )
                try:
                    locator.type(text, delay=random.randint(40, 90), timeout=3000)
                except Exception:
                    active_page.keyboard.insert_text(text)
                active_page.wait_for_timeout(200)
                if self.locator_has_text(locator, text):
                    return True
                locator.evaluate(
                    """
                    (el, value) => {
                        el.focus();
                        el.textContent = value;
                        el.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                    """,
                    text,
                )
                return self.locator_has_text(locator, text)
            return False
        except Exception:
            return False

    def fill_active_or_last_editable(self, text: str, *, page: Page | None = None) -> bool:
        active_page = page or self.page
        assert active_page is not None
        try:
            if self.type_into_active_or_last_editable(text, page=active_page):
                return True
            return bool(
                active_page.evaluate(
                    """
                    (value) => {
                        const isEditable = (el) => {
                            if (!el) return false;
                            if (el instanceof HTMLTextAreaElement) return !el.disabled && !el.readOnly;
                            if (el instanceof HTMLInputElement) {
                                const type = (el.type || 'text').toLowerCase();
                                return !el.disabled && !el.readOnly && ['text', 'search', ''].includes(type);
                            }
                            return el.isContentEditable || el.getAttribute('contenteditable') === 'true' || el.getAttribute('contenteditable') === 'plaintext-only';
                        };

                        const applyValue = (el) => {
                            if (!isEditable(el)) return false;
                            el.focus();
                            if (el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement) {
                                el.value = value;
                            } else {
                                el.textContent = value;
                            }
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                            return true;
                        };

                        if (applyValue(document.activeElement)) {
                            return true;
                        }

                        const candidates = Array.from(
                            document.querySelectorAll(
                                'textarea, input[type="text"], input[type="search"], input:not([type]), [contenteditable="true"], [contenteditable="plaintext-only"]'
                            )
                        ).filter((el) => {
                            const style = window.getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            return style.display !== 'none'
                                && style.visibility !== 'hidden'
                                && rect.width > 0
                                && rect.height > 0
                                && isEditable(el);
                        });

                        for (let index = candidates.length - 1; index >= 0; index -= 1) {
                            if (applyValue(candidates[index])) {
                                return true;
                            }
                        }
                        return false;
                    }
                    """,
                    text,
                )
            )
        except Exception:
            return False

    def type_into_active_or_last_editable(self, text: str, *, page: Page | None = None) -> bool:
        active_page = page or self.page
        assert active_page is not None
        try:
            focused = bool(
                active_page.evaluate(
                    """
                    () => {
                        const isEditable = (el) => {
                            if (!el) return false;
                            if (el instanceof HTMLTextAreaElement) return !el.disabled && !el.readOnly;
                            if (el instanceof HTMLInputElement) {
                                const type = (el.type || 'text').toLowerCase();
                                return !el.disabled && !el.readOnly && ['text', 'search', ''].includes(type);
                            }
                            return el.isContentEditable || el.getAttribute('contenteditable') === 'true' || el.getAttribute('contenteditable') === 'plaintext-only';
                        };
                        const isVisible = (el) => {
                            if (!el) return false;
                            const style = window.getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            return style.display !== 'none'
                                && style.visibility !== 'hidden'
                                && rect.width > 0
                                && rect.height > 0;
                        };
                        const focusNode = (el) => {
                            if (!isEditable(el) || !isVisible(el)) return false;
                            el.focus();
                            if (!(el instanceof HTMLTextAreaElement) && !(el instanceof HTMLInputElement)) {
                                const range = document.createRange();
                                range.selectNodeContents(el);
                                const selection = window.getSelection();
                                selection?.removeAllRanges();
                                selection?.addRange(range);
                            }
                            return true;
                        };
                        if (focusNode(document.activeElement)) {
                            return true;
                        }
                        const candidates = Array.from(
                            document.querySelectorAll(
                                'textarea, input[type="text"], input[type="search"], input:not([type]), [contenteditable="true"], [contenteditable="plaintext-only"]'
                            )
                        );
                        for (let index = candidates.length - 1; index >= 0; index -= 1) {
                            if (focusNode(candidates[index])) {
                                return true;
                            }
                        }
                        return false;
                    }
                    """
                )
            )
            if not focused:
                return False
            for shortcut in ("Meta+A", "Control+A"):
                try:
                    active_page.keyboard.press(shortcut)
                    break
                except Exception:
                    continue
            for clear_key in ("Backspace", "Delete"):
                try:
                    active_page.keyboard.press(clear_key)
                    break
                except Exception:
                    continue
            active_page.keyboard.type(text, delay=random.randint(40, 90))
            active_page.wait_for_timeout(200)
            return self.has_visible_editable_text(text, page=active_page)
        except Exception:
            return False

    def has_visible_editable_text(self, text: str, *, page: Page | None = None) -> bool:
        active_page = page or self.page
        assert active_page is not None
        normalized_text = re.sub(r"\s+", " ", text).strip()
        if not normalized_text:
            return False
        try:
            return bool(
                active_page.evaluate(
                    """
                    (expectedText) => {
                        const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                        const isVisible = (el) => {
                            if (!el) return false;
                            const style = window.getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            return style.display !== 'none'
                                && style.visibility !== 'hidden'
                                && rect.width > 0
                                && rect.height > 0;
                        };
                        const readValue = (el) => {
                            if (el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement) {
                                return el.value || '';
                            }
                            return el.innerText || el.textContent || '';
                        };
                        const candidates = document.querySelectorAll(
                            'textarea, input[type="text"], input[type="search"], input:not([type]), [contenteditable="true"], [contenteditable="plaintext-only"]'
                        );
                        for (const node of candidates) {
                            if (!isVisible(node)) continue;
                            const current = normalize(readValue(node));
                            if (current && (current === expectedText || current.includes(expectedText))) {
                                return true;
                            }
                        }
                        return false;
                    }
                    """,
                    normalized_text,
                )
            )
        except Exception:
            return False

    def focus_editable_with_text(self, text: str, *, page: Page | None = None) -> bool:
        active_page = page or self.page
        assert active_page is not None
        normalized_text = re.sub(r"\s+", " ", text).strip()
        try:
            return bool(
                active_page.evaluate(
                    """
                    (expectedText) => {
                        const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                        const isVisible = (el) => {
                            if (!el) return false;
                            const style = window.getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            return style.display !== 'none'
                                && style.visibility !== 'hidden'
                                && rect.width > 0
                                && rect.height > 0;
                        };
                        const isEditable = (el) => {
                            if (!el) return false;
                            if (el instanceof HTMLTextAreaElement) return !el.disabled && !el.readOnly;
                            if (el instanceof HTMLInputElement) {
                                const type = (el.type || 'text').toLowerCase();
                                return !el.disabled && !el.readOnly && ['text', 'search', ''].includes(type);
                            }
                            return el.isContentEditable || el.getAttribute('contenteditable') === 'true' || el.getAttribute('contenteditable') === 'plaintext-only';
                        };
                        const readValue = (el) => {
                            if (el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement) {
                                return el.value || '';
                            }
                            return el.innerText || el.textContent || '';
                        };
                        const focusNode = (el) => {
                            if (!isEditable(el) || !isVisible(el)) return false;
                            el.focus();
                            if (!(el instanceof HTMLTextAreaElement) && !(el instanceof HTMLInputElement)) {
                                const range = document.createRange();
                                range.selectNodeContents(el);
                                const selection = window.getSelection();
                                selection?.removeAllRanges();
                                selection?.addRange(range);
                            }
                            return true;
                        };
                        const candidates = Array.from(
                            document.querySelectorAll(
                                'textarea, input[type="text"], input[type="search"], input:not([type]), [contenteditable="true"], [contenteditable="plaintext-only"]'
                            )
                        ).filter((el) => isEditable(el) && isVisible(el));

                        const matching = candidates.filter((el) => normalize(readValue(el)).includes(expectedText));
                        for (const node of matching) {
                            if (focusNode(node)) {
                                return true;
                            }
                        }
                        return focusNode(document.activeElement);
                    }
                    """,
                    normalized_text,
                )
            )
        except Exception:
            return False

    def click_editable_adjacent_action(
        self,
        preferred_terms: tuple[str, ...],
        *,
        content: str = "",
        page: Page | None = None,
    ) -> str:
        active_page = page or self.page
        assert active_page is not None
        normalized_terms = tuple(term.strip() for term in preferred_terms if term.strip())
        normalized_content = re.sub(r"\s+", " ", content).strip()
        try:
            descriptor = str(
                active_page.evaluate(
                    """
                    ({ preferredTerms, expectedText }) => {
                        const marker = 'data-codex-comment-action';
                        document.querySelectorAll(`[${marker}]`).forEach((el) => el.removeAttribute(marker));

                        const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                        const isVisible = (el) => {
                            if (!el) return false;
                            const style = window.getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            return style.display !== 'none'
                                && style.visibility !== 'hidden'
                                && rect.width > 0
                                && rect.height > 0;
                        };
                        const isDisabled = (el) => el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true';
                        const isEditable = (el) => {
                            if (!el) return false;
                            if (el instanceof HTMLTextAreaElement) return !el.disabled && !el.readOnly;
                            if (el instanceof HTMLInputElement) {
                                const type = (el.type || 'text').toLowerCase();
                                return !el.disabled && !el.readOnly && ['text', 'search', ''].includes(type);
                            }
                            return el.isContentEditable || el.getAttribute('contenteditable') === 'true' || el.getAttribute('contenteditable') === 'plaintext-only';
                        };
                        const readValue = (el) => {
                            if (el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement) {
                                return normalize(el.value || '');
                            }
                            return normalize(el.innerText || el.textContent || '');
                        };
                        const attrText = (el) => normalize([
                            el.className || '',
                            el.getAttribute('aria-label') || '',
                            el.getAttribute('title') || '',
                            el.getAttribute('data-e2e') || '',
                        ].join(' '));
                        const isSearchLike = (el) => {
                            const combined = `${readValue(el)} ${attrText(el)}`.toLowerCase();
                            return combined.includes('搜索') || combined.includes('search');
                        };
                        const isCandidate = (el) => {
                            if (!el || !isVisible(el) || isDisabled(el)) return false;
                            if (isSearchLike(el)) return false;
                            const tag = (el.tagName || '').toLowerCase();
                            const attrs = attrText(el).toLowerCase();
                            const text = readValue(el);
                            return ['button', 'a'].includes(tag)
                                || el.getAttribute('role') === 'button'
                                || !!el.onclick
                                || /submit|send|publish|comment|reply|icon/.test(attrs)
                                || preferredTerms.some((term) => text.includes(term) || attrs.includes(term));
                        };
                        const score = (el, editor) => {
                            const text = readValue(el);
                            const attrs = attrText(el);
                            const rect = el.getBoundingClientRect();
                            const editorRect = editor.getBoundingClientRect();
                            const verticalCenterDistance = Math.abs(
                                (rect.top + rect.bottom) / 2 - (editorRect.top + editorRect.bottom) / 2
                            );
                            const horizontallyAdjacent = rect.left >= editorRect.right - 48 && rect.left <= editorRect.right + 180;
                            const verticallyAdjacent = verticalCenterDistance <= Math.max(90, editorRect.height * 2);
                            if (!horizontallyAdjacent || !verticallyAdjacent) {
                                return -1;
                            }
                            let value = 0;
                            for (const term of preferredTerms) {
                                if (!term) continue;
                                if (text.includes(term)) value += 80;
                                if (attrs.includes(term)) value += 40;
                            }
                            if (/submit|send|publish|reply/.test(attrs.toLowerCase())) value += 30;
                            if ((el.tagName || '').toLowerCase() === 'button' || el.getAttribute('role') === 'button') value += 15;
                            if (rect.left >= editorRect.right - 8) value += 25;
                            if (rect.right <= editorRect.right + 220) value += 10;
                            if (rect.top >= window.innerHeight - 260) value += 12;
                            if (editor.closest('[class*="comment"], [data-e2e*="comment"]')?.contains(el)) value += 8;
                            if ((rect.width * rect.height) <= 5000) value += 8;
                            return value;
                        };

                        const editors = Array.from(
                            document.querySelectorAll(
                                'textarea, input[type="text"], input[type="search"], input:not([type]), [contenteditable="true"], [contenteditable="plaintext-only"]'
                            )
                        ).filter((el) => isEditable(el) && isVisible(el));

                        const sortedEditors = editors.sort((left, right) => {
                            const leftMatch = expectedText && readValue(left).includes(expectedText) ? 1 : 0;
                            const rightMatch = expectedText && readValue(right).includes(expectedText) ? 1 : 0;
                            return rightMatch - leftMatch;
                        });

                        let best = null;
                        let bestScore = -1;
                        for (const editor of sortedEditors) {
                            let ancestor = editor;
                            for (let depth = 0; depth < 4 && ancestor; depth += 1) {
                                ancestor = ancestor.parentElement;
                                if (!ancestor) break;
                                if (isSearchLike(ancestor)) {
                                    continue;
                                }
                                const candidates = Array.from(
                                    ancestor.querySelectorAll('button, [role="button"], div, span, a')
                                ).filter((el) => el !== editor && !editor.contains(el) && isCandidate(el));
                                for (const candidate of candidates) {
                                    const currentScore = score(candidate, editor);
                                    if (currentScore > bestScore) {
                                        best = candidate;
                                        bestScore = currentScore;
                                    }
                                }
                            }
                            if (bestScore >= 80) {
                                break;
                            }
                        }

                        if (!best || bestScore < 20) {
                            return '';
                        }
                        best.setAttribute(marker, '1');
                        return `${(best.tagName || '').toLowerCase()}|${readValue(best)}|${attrText(best)}`.slice(0, 200);
                    }
                    """,
                    {"preferredTerms": normalized_terms, "expectedText": normalized_content},
                )
            )
            if not descriptor:
                return ""
            locator = active_page.locator("[data-codex-comment-action='1']").first
            locator.scroll_into_view_if_needed(timeout=2000)
            locator.click(timeout=3000)
            return descriptor
        except Exception:
            try:
                active_page.evaluate(
                    """
                    () => {
                        const el = document.querySelector('[data-codex-comment-action="1"]');
                        el?.click();
                    }
                    """
                )
                return "dom-click"
            except Exception:
                return ""

    def click_editor_row_end_action(self, content: str, *, page: Page | None = None) -> str:
        active_page = page or self.page
        assert active_page is not None
        normalized_content = re.sub(r"\s+", " ", content).strip()
        if not normalized_content:
            return ""
        try:
            descriptor = str(
                active_page.evaluate(
                    """
                    (expectedText) => {
                        const marker = 'data-codex-comment-row-action';
                        document.querySelectorAll(`[${marker}]`).forEach((el) => el.removeAttribute(marker));

                        const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                        const isVisible = (el) => {
                            if (!el) return false;
                            const style = window.getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            return style.display !== 'none'
                                && style.visibility !== 'hidden'
                                && rect.width > 0
                                && rect.height > 0;
                        };
                        const isEditable = (el) => {
                            if (!el) return false;
                            if (el instanceof HTMLTextAreaElement) return !el.disabled && !el.readOnly;
                            if (el instanceof HTMLInputElement) {
                                const type = (el.type || 'text').toLowerCase();
                                return !el.disabled && !el.readOnly && ['text', 'search', ''].includes(type);
                            }
                            return el.isContentEditable || el.getAttribute('contenteditable') === 'true' || el.getAttribute('contenteditable') === 'plaintext-only';
                        };
                        const readValue = (el) => {
                            if (el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement) {
                                return normalize(el.value || '');
                            }
                            return normalize(el.innerText || el.textContent || '');
                        };
                        const attrText = (el) => normalize([
                            el.className || '',
                            el.getAttribute('aria-label') || '',
                            el.getAttribute('title') || '',
                            el.getAttribute('data-e2e') || '',
                        ].join(' '));
                        const combinedText = (el) => `${readValue(el)} ${attrText(el)}`.toLowerCase();
                        const isSearchLike = (el) => combinedText(el).includes('搜索') || combinedText(el).includes('search');
                        const resolveClickable = (el) => el.closest('button, [role="button"], a') || el;
                        const seen = new Set();

                        const editors = Array.from(
                            document.querySelectorAll(
                                '[contenteditable="true"].public-DraftEditor-content, .public-DraftEditor-content[contenteditable="true"], [class*="DraftEditor"] [contenteditable="true"], [data-e2e*="comment"] [contenteditable="true"], [class*="comment-input"] textarea, [class*="comment-input"] [contenteditable="true"], [class*="comment"] [contenteditable="true"]'
                            )
                        ).filter((el) => isEditable(el) && isVisible(el) && readValue(el).includes(expectedText));

                        let best = null;
                        let bestScore = -1;
                        for (const editor of editors) {
                            const editorRect = editor.getBoundingClientRect();
                            const rawCandidates = Array.from(document.querySelectorAll('button, [role="button"], a, div, span'));
                            for (const rawCandidate of rawCandidates) {
                                const candidate = resolveClickable(rawCandidate);
                                if (!candidate || candidate === editor || editor.contains(candidate)) continue;
                                if (seen.has(candidate)) continue;
                                seen.add(candidate);
                                if (!isVisible(candidate) || isSearchLike(candidate)) continue;
                                const rect = candidate.getBoundingClientRect();
                                const centerY = (rect.top + rect.bottom) / 2;
                                const editorCenterY = (editorRect.top + editorRect.bottom) / 2;
                                const sameRow = Math.abs(centerY - editorCenterY) <= Math.max(28, editorRect.height * 1.2);
                                const toRight = rect.left >= editorRect.right - 4 && rect.left <= editorRect.right + 140;
                                const compact = rect.width <= 56 && rect.height <= 56;
                                if (!sameRow || !toRight || !compact) continue;

                                let score = 0;
                                score += rect.left * 2;
                                if (rect.width <= 40 && rect.height <= 40) score += 80;
                                const styles = window.getComputedStyle(candidate);
                                const bg = styles.backgroundColor || '';
                                if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') score += 40;
                                if (/send|submit|publish|comment|reply|icon/.test(attrText(candidate).toLowerCase())) score += 20;
                                if (!readValue(candidate)) score += 10;
                                if (candidate.closest('[class*="comment"], [data-e2e*="comment"]')) score += 20;
                                if (score > bestScore) {
                                    best = candidate;
                                    bestScore = score;
                                }
                            }
                        }

                        if (!best) {
                            return '';
                        }
                        best.setAttribute(marker, '1');
                        return `${best.tagName.toLowerCase()}|${readValue(best)}|${attrText(best)}`.slice(0, 200);
                    }
                    """,
                    normalized_content,
                )
            )
            if not descriptor:
                return ""
            locator = active_page.locator("[data-codex-comment-row-action='1']").first
            locator.scroll_into_view_if_needed(timeout=2000)
            locator.click(timeout=3000)
            return descriptor
        except Exception:
            try:
                active_page.evaluate(
                    """
                    () => {
                        const el = document.querySelector('[data-codex-comment-row-action="1"]');
                        el?.click();
                    }
                    """
                )
                return "dom-click"
            except Exception:
                return ""

    def wait_for_comment_submission_confirmation(
        self,
        content: str,
        *,
        success_terms: tuple[str, ...] = (),
        failure_terms: tuple[str, ...] = (),
        page: Page | None = None,
        timeout_ms: int = 5000,
    ) -> tuple[bool, str]:
        active_page = page or self.page
        assert active_page is not None
        deadline = time.monotonic() + max(timeout_ms, 0) / 1000
        while time.monotonic() <= deadline:
            body_text = self.get_body_snippet(1500, page=active_page)
            for failure_term in failure_terms:
                if failure_term and failure_term in body_text:
                    return False, failure_term
            for success_term in success_terms:
                if success_term and success_term in body_text:
                    return True, success_term
            if not self.has_visible_editable_text(content, page=active_page):
                return True, "评论输入已清空"
            try:
                active_page.wait_for_timeout(400)
            except Exception:
                break
        return False, "未确认评论已提交"

    def parse_compact_number(self, value: Any) -> int | None:
        text = re.sub(r"[\s,]+", "", str(value or "")).lower()
        if not text:
            return None
        matched = re.search(r"(\d+(?:\.\d+)?)", text)
        if not matched:
            return None
        try:
            number = float(matched.group(1))
        except Exception:
            return None
        multiplier = 1
        if "万" in text or "w" in text:
            multiplier = 10000
        elif "千" in text or "k" in text:
            multiplier = 1000
        return int(number * multiplier)

    def capture_comment_submission_markers(self, content: str, *, page: Page | None = None) -> dict[str, Any]:
        active_page = page or self.page
        assert active_page is not None
        normalized_content = re.sub(r"\s+", " ", content).strip()
        try:
            markers = active_page.evaluate(
                """
                (expectedText) => {
                    const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                    const isVisible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.display !== 'none'
                            && style.visibility !== 'hidden'
                            && rect.width > 0
                            && rect.height > 0;
                    };
                    const textWalker = document.createTreeWalker(
                        document.body,
                        NodeFilter.SHOW_TEXT,
                        {
                            acceptNode(node) {
                                const parent = node.parentElement;
                                if (!parent || !isVisible(parent)) {
                                    return NodeFilter.FILTER_REJECT;
                                }
                                if (parent.closest('textarea, input, [contenteditable="true"], [contenteditable="plaintext-only"]')) {
                                    return NodeFilter.FILTER_REJECT;
                                }
                                return normalize(node.textContent) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
                            },
                        }
                    );
                    const parts = [];
                    while (textWalker.nextNode()) {
                        parts.push(normalize(textWalker.currentNode.textContent));
                    }
                    const pageText = parts.join(' ');
                    let occurrences = 0;
                    if (expectedText) {
                        const escaped = expectedText.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
                        const matches = pageText.match(new RegExp(escaped, 'g'));
                        occurrences = matches ? matches.length : 0;
                    }
                    const commentNode = Array.from(
                        document.querySelectorAll(
                            '[data-e2e="comment-count"], [class*="comment"] [class*="count"], [data-e2e="comment-area"] [class*="count"]'
                        )
                    ).find(isVisible);
                    return {
                        nonEditableOccurrences: occurrences,
                        commentCountText: normalize(commentNode?.innerText || ''),
                    };
                }
                """,
                normalized_content,
            )
        except Exception:
            markers = {}
        if not isinstance(markers, dict):
            return {"nonEditableOccurrences": 0, "commentCount": None, "commentCountText": ""}
        comment_count_text = str(markers.get("commentCountText", "")).strip()
        return {
            "nonEditableOccurrences": int(markers.get("nonEditableOccurrences", 0) or 0),
            "commentCountText": comment_count_text,
            "commentCount": self.parse_compact_number(comment_count_text),
        }

    def wait_for_posted_comment_visibility(
        self,
        content: str,
        *,
        baseline_occurrences: int,
        baseline_comment_count: int | None,
        failure_terms: tuple[str, ...] = (),
        page: Page | None = None,
        timeout_ms: int = 6000,
    ) -> tuple[bool, str]:
        active_page = page or self.page
        assert active_page is not None
        deadline = time.monotonic() + max(timeout_ms, 0) / 1000
        while time.monotonic() <= deadline:
            body_text = self.get_body_snippet(1500, page=active_page)
            for failure_term in failure_terms:
                if failure_term and failure_term in body_text:
                    return False, failure_term
            markers = self.capture_comment_submission_markers(content, page=active_page)
            current_occurrences = int(markers.get("nonEditableOccurrences", 0) or 0)
            current_comment_count = markers.get("commentCount")
            if current_occurrences > baseline_occurrences:
                return True, "评论内容已出现在页面上"
            if (
                baseline_comment_count is not None
                and current_comment_count is not None
                and current_comment_count > baseline_comment_count
            ):
                return True, "评论数已增加"
            try:
                active_page.wait_for_timeout(500)
            except Exception:
                break
        return False, "未确认评论已出现在页面上"

    def submit_comment_with_fallbacks(
        self,
        submit_selectors: tuple[str, ...],
        content: str,
        *,
        success_terms: tuple[str, ...] = (),
        failure_terms: tuple[str, ...] = (),
        page: Page | None = None,
        allow_keyboard_shortcuts: bool = True,
    ) -> tuple[bool, str]:
        active_page = page or self.page
        assert active_page is not None

        clicked_selector = self.click_first_visible(submit_selectors, page=active_page, require_enabled=True)
        if clicked_selector:
            self.human_delay(800, 1500)
            confirmed, reason = self.wait_for_comment_submission_confirmation(
                content,
                success_terms=success_terms,
                failure_terms=failure_terms,
                page=active_page,
                timeout_ms=2500,
            )
            if confirmed or "频繁" in reason or "失败" in reason or "无法评论" in reason:
                return confirmed, reason

        adjacent_action = self.click_editable_adjacent_action(
            ("发送", "发布"),
            content=content,
            page=active_page,
        )
        if adjacent_action:
            self.human_delay(800, 1500)
            confirmed, reason = self.wait_for_comment_submission_confirmation(
                content,
                success_terms=success_terms,
                failure_terms=failure_terms,
                page=active_page,
                timeout_ms=2500,
            )
            if confirmed or "频繁" in reason or "失败" in reason or "无法评论" in reason:
                return confirmed, reason

        if allow_keyboard_shortcuts:
            for shortcut in ("Enter", "Meta+Enter", "Control+Enter"):
                try:
                    self.focus_editable_with_text(content, page=active_page)
                    active_page.keyboard.press(shortcut)
                except Exception:
                    continue
                self.human_delay(700, 1300)
                confirmed, reason = self.wait_for_comment_submission_confirmation(
                    content,
                    success_terms=success_terms,
                    failure_terms=failure_terms,
                    page=active_page,
                    timeout_ms=2500,
                )
                if confirmed or "频繁" in reason or "失败" in reason or "无法评论" in reason:
                    return confirmed, reason

        if not clicked_selector:
            return False, "未找到评论提交按钮"
        return False, "未确认评论已提交"

    def scroll_near_page_bottom(self, *, page: Page | None = None) -> None:
        active_page = page or self.page
        assert active_page is not None
        try:
            active_page.evaluate(
                """
                () => {
                    const scrollers = [
                        document.scrollingElement,
                        document.documentElement,
                        document.body,
                        ...Array.from(document.querySelectorAll('[class*="scroll"], [style*="overflow"]')),
                    ].filter(Boolean);
                    for (const node of scrollers) {
                        try {
                            node.scrollTop = node.scrollHeight;
                        } catch (error) {}
                    }
                    window.scrollTo(0, document.body.scrollHeight);
                }
                """
            )
        except Exception:
            pass

    # Search response capture shared by both platforms.
    def begin_search_capture(
        self,
        keyword: str,
        cards: dict[str, dict[str, Any]],
        platform: str | None = None,
    ) -> None:
        self.active_search_keyword = keyword
        self.active_search_platform = platform or self.platform_name
        self.active_search_cards = cards
        self.capture_search_responses = True

    def end_search_capture(self) -> None:
        self.capture_search_responses = False
        self.active_search_keyword = ""
        self.active_search_platform = self.platform_name
        self.active_search_cards = {}

    def capture_search_response(self, response: Response) -> None:
        if not self.capture_search_responses:
            return

        url = response.url
        if not self.looks_like_search_api(url):
            return

        try:
            body = response.text()
            if not body.strip():
                return
            root = json.loads(body)
            added = self.extract_search_cards_from_json(
                root,
                self.active_search_cards,
                self.active_search_keyword,
            )
            if added > 0:
                log.info(
                    "  捕获%s搜索接口响应: %s (+%s，累计 %s)",
                    self.get_platform_label(),
                    self.abbreviate_url(url),
                    added,
                    len(self.snapshot_cards(self.active_search_cards, 10_000)),
                )
        except Exception as exc:
            log.debug("解析搜索接口失败 %s: %s", self.abbreviate_url(url), exc)

    def process_search_response(
        self,
        response: Response,
        *,
        page: Page,
        cards: dict[str, dict[str, Any]],
        keyword: str,
        platform: str,
    ) -> None:
        if self.stop_requested:
            return
        url = response.url
        if platform != self.platform_name or not self.looks_like_search_api(url):
            return
        try:
            if response.frame.page != page:
                return
        except Exception:
            return
        try:
            body = response.text()
            if not body.strip():
                return
            root = json.loads(body)
            added = self.extract_search_cards_from_json(root, cards, keyword)
            if added > 0:
                log.info(
                    "  捕获%s搜索接口响应: %s (+%s，累计 %s)",
                    self.get_platform_label(),
                    self.abbreviate_url(url),
                    added,
                    len(self.snapshot_cards(cards, 10_000)),
                )
        except Exception as exc:
            log.debug("解析搜索接口失败 %s: %s", self.abbreviate_url(url), exc)

    # JSON parsing helpers shared by the Xiaohongshu extractor.
    def extract_cards_from_json(
        self,
        root: Any,
        cards: dict[str, dict[str, Any]],
        keyword: str,
    ) -> int:
        before = len(self.snapshot_cards(cards, 10_000))
        stack: list[Any] = [root]
        while stack:
            node = stack.pop()
            if node is None:
                continue
            card = self.to_card_from_json(node, keyword)
            post_id = str(card.get("postId", ""))
            if post_id:
                cards.setdefault(post_id, card)

            if isinstance(node, dict):
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)

        return len(self.snapshot_cards(cards, 10_000)) - before

    def to_card_from_json(self, node: Any, keyword: str) -> dict[str, Any]:
        if not isinstance(node, dict):
            return {}

        envelope = node
        payload = node.get("note") if isinstance(node.get("note"), dict) else node
        note_card = payload.get("note_card") if isinstance(payload.get("note_card"), dict) else payload

        post_id = self.first_non_blank(
            self.text_value(envelope, "id", "note_id"),
            self.text_value(payload, "id", "note_id"),
            self.text_value(note_card, "id", "note_id"),
        )
        post_id = self.extract_post_id(post_id)
        if not self.is_likely_note_id(post_id):
            return {}

        xsec_token = self.first_non_blank(
            self.text_value(envelope, "xsec_token"),
            self.text_value(payload, "xsec_token"),
            self.text_value(note_card, "xsec_token"),
        )
        title = self.first_non_blank(
            self.text_value(note_card, "display_title", "title", "name"),
            self.text_value(payload, "display_title", "title", "name"),
        )
        content = self.first_non_blank(
            self.text_value(note_card, "desc", "display_desc", "content"),
            self.text_value(payload, "desc", "display_desc", "content"),
            title,
        )
        user = self.first_object(
            note_card.get("user"),
            payload.get("user"),
            payload.get("author"),
            envelope.get("user"),
        )
        author = self.first_non_blank(
            self.text_value(user, "nickname", "nick_name", "name"),
            self.text_value(note_card, "nickname", "author"),
            self.text_value(payload, "nickname", "author"),
        )
        author_id = self.first_non_blank(
            self.text_value(user, "user_id", "id"),
            self.text_value(payload, "user_id", "author_id"),
        )
        url = self.first_non_blank(
            self.text_value(envelope, "note_url"),
            self.text_value(payload, "note_url"),
            self.build_note_url(post_id, xsec_token),
        )

        card: dict[str, Any] = {
            "postId": post_id,
            "title": title,
            "author": author,
            "authorId": author_id,
            "content": content,
            "publishTime": self.extract_publish_time(envelope, payload, note_card),
            "likes": self.extract_counter(note_card, payload, "liked_count", "likes"),
            "collects": self.extract_counter(note_card, payload, "collected_count", "collects"),
            "comments": self.extract_counter(note_card, payload, "comment_count", "comments"),
            "url": url,
            "xsecToken": xsec_token,
            "images": self.extract_images(note_card, payload),
            "keyword": keyword,
        }
        self.normalize_card(card)
        looks_like_note = bool(card.get("title") or card.get("content"))
        return card if looks_like_note else {}

    def extract_counter(
        self,
        primary: dict[str, Any] | None,
        secondary: dict[str, Any] | None,
        interact_field: str,
        flat_field: str,
    ) -> int:
        interact_primary = primary.get("interact_info") if isinstance(primary, dict) else None
        interact_secondary = secondary.get("interact_info") if isinstance(secondary, dict) else None
        raw = self.first_non_blank(
            self.text_value(interact_primary, interact_field),
            self.text_value(interact_secondary, interact_field),
            self.text_value(primary, flat_field),
            self.text_value(secondary, flat_field),
        )
        return self.parse_count(raw)

    def extract_images(self, *nodes: dict[str, Any] | None) -> list[str]:
        images: list[str] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            cover = self.pick_image_url(node.get("cover"))
            if cover:
                images.append(cover)
            image_list = node.get("image_list")
            if isinstance(image_list, list):
                for image in image_list:
                    url = self.pick_image_url(image)
                    if url:
                        images.append(url)
        deduped: list[str] = []
        for image in images:
            if image and image not in deduped:
                deduped.append(image)
        return deduped[:9]

    def pick_image_url(self, image_node: Any) -> str:
        if not isinstance(image_node, dict):
            return ""
        direct = self.first_non_blank(
            self.text_value(image_node, "url_default", "url", "url_pre", "url_size_large")
        )
        if direct:
            return direct
        info_list = image_node.get("info_list")
        if isinstance(info_list, list):
            for info in info_list:
                url = self.text_value(info, "url")
                if url:
                    return url
        return ""

    def extract_publish_time(self, *nodes: dict[str, Any] | None) -> str:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            value = self.first_non_blank(
                self.text_value(node, "publish_time", "time", "last_update_time", "last_update_time_text")
            )
            if value:
                return value
            corner_tags = node.get("corner_tag_info")
            if isinstance(corner_tags, list):
                for tag in corner_tags:
                    text = self.text_value(tag, "text")
                    if text:
                        return text
        return ""

    def snapshot_cards(self, cards: dict[str, dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        snapshot = list(cards.values())
        return snapshot[:limit]

    # Debugging and normalization helpers.
    def log_platform_session_summary(self) -> None:
        status = "已恢复" if self.has_platform_session_cookie() else "未恢复"
        log.info("🔎 Session 摘要: %s=%s", self.get_platform_session_label(), status)

    def is_visible(self, selector: str, *, page: Page | None = None) -> bool:
        active_page = page or self.page
        assert active_page is not None
        try:
            return active_page.locator(selector).first.is_visible()
        except Exception:
            return False

    def save_search_debug_artifacts(self, keyword: str, stage: str, *, page: Page | None = None) -> None:
        active_page = page or self.page
        assert active_page is not None
        try:
            debug_dir = Path("data/debug")
            debug_dir.mkdir(parents=True, exist_ok=True)
            safe_keyword = re.sub(r"[^\w-]", "_", keyword)
            if not safe_keyword or set(safe_keyword) == {"_"}:
                safe_keyword = f"kw_{abs(hash(keyword))}"
            base_name = f"{safe_keyword}_{stage}"
            active_page.screenshot(path=str(debug_dir / f"{base_name}.png"), full_page=False)
            (debug_dir / f"{base_name}.txt").write_text(
                self.get_debug_page_snapshot(page=active_page),
                encoding="utf-8",
            )
            log.info("  🧪 调试信息已保存: data/debug/%s.(png|txt)", base_name)
        except Exception as exc:
            log.debug("保存调试信息失败: %s", exc)

    def get_body_snippet(self, max_chars: int, *, page: Page | None = None) -> str:
        active_page = page or self.page
        assert active_page is not None
        try:
            body_text = active_page.evaluate(
                "() => document.body?.innerText?.replace(/\\s+/g, ' ').trim() || ''"
            )
            text = str(body_text or "")
            return text[:max_chars]
        except Exception:
            return ""

    def get_debug_page_snapshot(self, *, page: Page | None = None) -> str:
        active_page = page or self.page
        assert active_page is not None
        body_text = self.get_body_snippet(4000, page=active_page)
        try:
            controls = active_page.evaluate(
                """
                () => {
                    const isVisible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.display !== 'none'
                            && style.visibility !== 'hidden'
                            && rect.width > 0
                            && rect.height > 0;
                    };
                    const truncate = (value) => String(value || '').replace(/\\s+/g, ' ').trim().slice(0, 120);
                    const attrText = (el) => truncate([
                        el.className || '',
                        el.getAttribute('aria-label') || '',
                        el.getAttribute('title') || '',
                        el.getAttribute('data-e2e') || '',
                    ].join(' '));
                    const looksActionLike = (el) => {
                        const attrs = attrText(el).toLowerCase();
                        const text = truncate(el.innerText || el.textContent || '');
                        return /submit|send|publish|comment|reply|icon/.test(attrs)
                            || /发送|发布|评论|回复/.test(text)
                            || el.tagName.toLowerCase() === 'button'
                            || el.getAttribute('role') === 'button';
                    };
                    const editables = Array.from(
                        document.querySelectorAll(
                            'textarea, input[type="text"], input[type="search"], input:not([type]), [contenteditable="true"], [contenteditable="plaintext-only"]'
                        )
                    )
                        .filter(isVisible)
                        .slice(0, 8)
                        .map((el) => ({
                            tag: el.tagName.toLowerCase(),
                            placeholder: truncate(el.getAttribute('placeholder') || ''),
                            value: truncate(el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement ? el.value : (el.innerText || el.textContent || '')),
                            className: truncate(el.className || ''),
                            contenteditable: truncate(el.getAttribute('contenteditable') || ''),
                        }));
                    const actions = Array.from(
                        document.querySelectorAll('button, [role="button"], div, span, a')
                    )
                        .filter((el) => isVisible(el) && looksActionLike(el))
                        .slice(0, 20)
                        .map((el) => ({
                            tag: el.tagName.toLowerCase(),
                            text: truncate(el.innerText || el.textContent || ''),
                            className: attrText(el),
                            disabled: el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true',
                        }));
                    const commentActions = [];
                    const editors = Array.from(
                        document.querySelectorAll(
                            '[contenteditable="true"].public-DraftEditor-content, .public-DraftEditor-content[contenteditable="true"], [class*="DraftEditor"] [contenteditable="true"], [data-e2e*="comment"] [contenteditable="true"], [class*="comment-input"] textarea, [class*="comment-input"] [contenteditable="true"], [class*="comment"] [contenteditable="true"]'
                        )
                    ).filter(isVisible);
                    for (const editor of editors.slice(0, 2)) {
                        const editorRect = editor.getBoundingClientRect();
                        const nearby = Array.from(document.querySelectorAll('button, [role="button"], a, div, span'))
                            .map((el) => el.closest('button, [role="button"], a') || el)
                            .filter((el, index, array) => array.indexOf(el) === index)
                            .filter((el) => {
                                if (!isVisible(el) || editor.contains(el)) return false;
                                const rect = el.getBoundingClientRect();
                                const centerY = (rect.top + rect.bottom) / 2;
                                const editorCenterY = (editorRect.top + editorRect.bottom) / 2;
                                return Math.abs(centerY - editorCenterY) <= Math.max(28, editorRect.height * 1.2)
                                    && rect.left >= editorRect.right - 4
                                    && rect.left <= editorRect.right + 160
                                    && rect.width <= 72
                                    && rect.height <= 72;
                            })
                            .slice(0, 8)
                            .map((el) => ({
                                tag: el.tagName.toLowerCase(),
                                text: truncate(el.innerText || el.textContent || ''),
                                className: attrText(el),
                                left: Math.round(el.getBoundingClientRect().left),
                                top: Math.round(el.getBoundingClientRect().top),
                                width: Math.round(el.getBoundingClientRect().width),
                                height: Math.round(el.getBoundingClientRect().height),
                            }));
                        commentActions.push({
                            editorValue: truncate(editor.innerText || editor.textContent || (editor.value || '')),
                            nearby,
                        });
                    }
                    return { editables, actions, commentActions };
                }
                """
            )
        except Exception:
            controls = {"editables": [], "actions": [], "commentActions": []}
        try:
            controls_text = json.dumps(controls, ensure_ascii=False, indent=2)
        except Exception:
            controls_text = str(controls)
        return f"{body_text}\n\n[controls]\n{controls_text}\n"

    def normalize_card(self, card: dict[str, Any]) -> None:
        platform = str(card.get("platform", self.get_default_platform())).strip() or self.get_default_platform()
        card["platform"] = platform
        card["postId"] = self.normalize_platform_post_id(str(card.get("postId", "")))
        card["title"] = str(card.get("title", "")).strip()
        card["author"] = str(card.get("author", "")).strip()
        card["authorId"] = str(card.get("authorId", "")).strip()
        card["content"] = str(card.get("content", "")).strip()
        card["publishTime"] = str(card.get("publishTime", "")).strip()
        card["url"] = str(card.get("url", "")).strip()
        card["likes"] = self.parse_count(card.get("likes", 0))
        card["collects"] = self.parse_count(card.get("collects", 0))
        card["comments"] = self.parse_count(card.get("comments", 0))

    def build_note_url(self, post_id: str, xsec_token: str) -> str:
        if not self.is_likely_note_id(post_id):
            return ""
        url = f"https://www.xiaohongshu.com/explore/{post_id}"
        if xsec_token:
            url = f"{url}?xsec_token={quote(xsec_token)}"
        return url

    def extract_post_id(self, value: str) -> str:
        if not value:
            return ""
        matched = self.NOTE_ID_PATTERN.search(value)
        if matched:
            candidate = matched.group(1)
            return candidate if self.is_likely_note_id(candidate) else ""
        candidate = value.strip()
        return candidate if self.is_likely_note_id(candidate) else ""

    def is_likely_note_id(self, value: str) -> bool:
        return bool(value and self.NOTE_HEX_ID_PATTERN.match(value.strip()))

    def parse_count(self, raw_value: Any) -> int:
        raw = (
            str(raw_value or "")
            .replace(",", "")
            .replace("点赞", "")
            .replace("收藏", "")
            .replace("评论", "")
            .strip()
        )
        if not raw:
            return 0
        try:
            if raw.endswith("万"):
                return int(round(float(raw[:-1]) * 10_000))
            if raw.endswith("亿"):
                return int(round(float(raw[:-1]) * 100_000_000))
            return int(round(float(raw)))
        except Exception:
            return 0

    def first_object(self, *nodes: Any) -> dict[str, Any] | None:
        for node in nodes:
            if isinstance(node, dict):
                return node
        return None

    def pick_first_url(self, values: Any) -> str:
        if isinstance(values, list):
            for value in values:
                text = str(value).strip()
                if text:
                    return text
        return ""

    def text_value(self, node: Any, *field_names: str) -> str:
        if not isinstance(node, dict):
            return ""
        for field_name in field_names:
            value = node.get(field_name)
            if isinstance(value, (str, int, float, bool)):
                text = str(value).strip()
                if text:
                    return text
        return ""

    def first_non_blank(self, *values: str) -> str:
        for value in values:
            if value and value.strip():
                return value.strip()
        return ""

    def abbreviate_url(self, url: str) -> str:
        return f"{url[:117]}..." if len(url) > 120 else url

    # Session snapshot persistence.
    def save_session(self) -> None:
        if not self.config.is_save_session() or self.context is None:
            return
        try:
            snapshot = self.context.storage_state()
            path = Path(self.config.get_session_file())
            self._write_snapshot(path, snapshot)
            self._write_snapshot(
                self.get_platform_session_path(),
                self.filter_storage_state(snapshot, domains=self.get_platform_session_domains()),
            )
            log.info("Session 已保存: %s", path)
        except Exception as exc:
            log.warning("保存 Session 失败，已跳过: %s", exc)

    def restore_platform_session(self) -> None:
        session_path = self.get_platform_session_path()
        main_path = Path(self.config.get_session_file())
        if session_path == main_path or not session_path.is_file():
            return
        self.restore_session_from_file(session_path, label=self.get_platform_session_label())

    def restore_session_from_file(self, session_path: Path, label: str | None = None) -> None:
        if self.context is None:
            return
        try:
            root = json.loads(session_path.read_text(encoding="utf-8"))
            cookies = root.get("cookies") or []
            normalized_cookies = []
            for cookie in cookies:
                if not isinstance(cookie, dict) or not cookie.get("name"):
                    continue
                normalized = {
                    "name": cookie.get("name", ""),
                    "value": cookie.get("value", ""),
                    "domain": cookie.get("domain", ""),
                    "path": cookie.get("path", "/") or "/",
                    "httpOnly": bool(cookie.get("httpOnly", False)),
                    "secure": bool(cookie.get("secure", False)),
                }
                if "expires" in cookie and isinstance(cookie.get("expires"), (int, float)):
                    normalized["expires"] = float(cookie["expires"])
                if cookie.get("sameSite"):
                    normalized["sameSite"] = cookie["sameSite"]
                normalized_cookies.append(normalized)

            if normalized_cookies:
                self.context.add_cookies(normalized_cookies)
                prefix = f"{label} " if label else ""
                log.info(
                    "已从%sSession 快照恢复 %s 个 Cookie: %s",
                    prefix,
                    len(normalized_cookies),
                    session_path,
                )
            origins = root.get("origins") or []
            local_storage_by_origin: dict[str, list[dict[str, str]]] = {}
            for origin in origins:
                if not isinstance(origin, dict):
                    continue
                origin_url = str(origin.get("origin", "")).strip()
                if not origin_url:
                    continue
                items: list[dict[str, str]] = []
                for entry in origin.get("localStorage") or []:
                    if not isinstance(entry, dict):
                        continue
                    name = str(entry.get("name", "")).strip()
                    if not name:
                        continue
                    items.append(
                        {
                            "name": name,
                            "value": str(entry.get("value", "")),
                        }
                    )
                if items:
                    local_storage_by_origin[origin_url] = items
            if local_storage_by_origin:
                payload = json.dumps(local_storage_by_origin, ensure_ascii=False)
                self.context.add_init_script(
                    script=f"""
                    (() => {{
                        const storageByOrigin = {payload};
                        const items = storageByOrigin[window.location.origin];
                        if (!items) return;
                        for (const item of items) {{
                            try {{
                                window.localStorage.setItem(item.name, item.value);
                            }} catch (error) {{}}
                        }}
                    }})()
                    """,
                )
                prefix = f"{label} " if label else ""
                log.info(
                    "已注册%sSession 快照中的 LocalStorage 恢复: %s 个源",
                    prefix,
                    len(local_storage_by_origin),
                )
        except Exception as exc:
            log.warning("恢复 Session 快照失败: %s", exc)

    def filter_storage_state(
        self,
        snapshot: dict[str, Any],
        *,
        domains: tuple[str, ...],
    ) -> dict[str, Any]:
        cookies = [
            cookie
            for cookie in snapshot.get("cookies") or []
            if isinstance(cookie, dict)
            and any(domain in str(cookie.get("domain", "")) for domain in domains)
        ]
        origins = [
            origin
            for origin in snapshot.get("origins") or []
            if isinstance(origin, dict)
            and any(domain in str(origin.get("origin", "")) for domain in domains)
        ]
        return {"cookies": cookies, "origins": origins}

    def _write_snapshot(self, path: Path, snapshot: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

    def log_session_presence(self, platform_name: str, session_path: Path) -> None:
        if session_path.is_file():
            log.info("已发现%s Session 快照: %s", platform_name, session_path)
        else:
            log.info("未发现%s Session 快照: %s", platform_name, session_path)

    # Generic runtime utilities.
    def human_delay(self, min_ms: int, max_ms: int) -> None:
        time.sleep(random.randint(min_ms, max_ms) / 1000)

    def build_user_agent(self) -> str:
        return (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        )

    def request_stop(self) -> None:
        self.stop_requested = True

    def close(self, *, interrupted: bool = False) -> None:
        self.stop_requested = True
        if interrupted:
            if self.authenticated_session:
                log.info("停止中跳过 Session 收尾保存：登录校验成功后已保存当前快照")
            elif self.config.is_save_session():
                log.info("跳过保存 Session：当前未确认登录成功")
            log.info("检测到中断退出，跳过浏览器收尾关闭步骤")
            return
        if self.authenticated_session:
            self.save_session()
        elif self.config.is_save_session():
            log.info("跳过保存 Session：当前未确认登录成功")
        try:
            if self.context is not None:
                self.context.close()
        finally:
            if self.browser is not None:
                try:
                    self.browser.close()
                except Exception:
                    pass
            if self.playwright is not None:
                self.playwright.stop()
        log.info("浏览器已关闭")


class XHSBrowser(BaseBrowser):
    XHS_HOME = "https://www.xiaohongshu.com"
    XHS_SEARCH = (
        "https://www.xiaohongshu.com/search_result?keyword={kw}&source=web_search_result_notes"
    )
    NOTE_ID_PATTERN = re.compile(r"/explore/([a-zA-Z0-9]+)")
    NOTE_HEX_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{24}$")

    def looks_like_search_api(self, url: str) -> bool:
        return (
            "xiaohongshu.com" in url
            and "/api/" in url
            and ("/search/" in url or "/search_result" in url or "/feed" in url)
        )

    def extract_search_cards_from_json(
        self,
        root: Any,
        cards: dict[str, dict[str, Any]],
        keyword: str,
    ) -> int:
        return self.extract_cards_from_json(root, cards, keyword)

    def get_platform_label(self) -> str:
        return "小红书"

    def normalize_platform_post_id(self, post_id: str) -> str:
        return self.extract_post_id(post_id)

    def get_platform_session_path(self) -> Path:
        return Path(self.config.get_xhs_session_file())

    def get_platform_session_domains(self) -> tuple[str, ...]:
        return ("xiaohongshu.com",)

    def has_platform_session_cookie(self) -> bool:
        assert self.context is not None
        cookies = self.context.cookies()
        return any(
            cookie.get("name") in {"web_session", "a1"}
            and "xiaohongshu.com" in str(cookie.get("domain"))
            for cookie in cookies
        )

    def is_xhs_login_required(self, *, page: Page | None = None) -> bool:
        return (
            self.is_visible("[class*='login-container']", page=page)
            or self.is_visible("text=扫码登录", page=page)
            or self.is_visible("text=请登录后继续", page=page)
        )

    def has_active_session(self) -> bool:
        assert self.context is not None
        cookies = self.context.cookies()
        has_web_session = any(
            cookie.get("name") == "web_session" and "xiaohongshu.com" in str(cookie.get("domain"))
            for cookie in cookies
        )
        has_a1 = any(
            cookie.get("name") == "a1" and "xiaohongshu.com" in str(cookie.get("domain"))
            for cookie in cookies
        )
        login_gate_visible = self.is_xhs_login_required()
        return has_web_session and has_a1 and not login_gate_visible

    def ensure_logged_in(self) -> bool:
        assert self.page is not None
        log.info("🔐 检查小红书登录状态...")
        if not self.goto_with_handling(self.XHS_HOME):
            return False
        self.human_delay(2000, 3000)

        if self.has_active_session():
            self.authenticated_session = True
            self.save_session()
            log.info("✅ 登录状态正常")
            return True

        log.warning("⚠️  检测到未登录！")
        self.save_search_debug_artifacts("login", "login_required")
        if self.config.is_browser_headless():
            log.warning("请将 settings.yaml 中 browser.headless 改为 false，手动扫码后改回 true")
            return False

        log.info("请在浏览器中完成扫码登录，然后在终端按 Enter 键继续...")
        try:
            input()
        except EOFError:
            pass
        self.human_delay(1500, 2500)
        ok = self.has_active_session()
        if ok:
            self.authenticated_session = True
            self.save_session()
        if not ok:
            log.warning("⚠️  未检测到有效登录态，本次不会覆盖现有 Session 快照")
        return ok

    def search_posts(self, keyword: str, max_count: int) -> list[dict[str, Any]]:
        assert self.context is not None
        page = self.context.new_page()
        page.on(
            "response",
            lambda response: self.process_search_response(
                response, page=page, cards=api_cards, keyword=keyword, platform="xiaohongshu"
            ),
        )
        log.info("🔍 搜索关键词: [%s]，目标 %s 条", keyword, max_count)
        posts: list[dict[str, Any]] = []
        api_cards: dict[str, dict[str, Any]] = {}

        try:
            url = self.XHS_SEARCH.format(kw=quote(keyword))
            page.goto(url, wait_until="domcontentloaded")
            self.human_delay(3000, 5000)

            log.info("  页面 URL  : %s", page.url)
            log.info("  页面 title: %s", page.title())
            log.info("  页面文本摘要: %s", self.get_body_snippet(180, page=page))

            scrolls = 0
            max_scrolls = max(4, max_count // 4)
            while len(self.snapshot_cards(api_cards, max_count)) < max_count and scrolls < max_scrolls:
                if self.stop_requested:
                    log.info("  ⏹️ 收到停止请求，提前结束当前搜索")
                    break
                before = len(self.snapshot_cards(api_cards, 10_000))
                self.human_delay(1200, 2200)
                after = len(self.snapshot_cards(api_cards, 10_000))
                log.info(
                    "  接口采集轮次 %s/%s → 新增 %s 条，累计 %s 条",
                    scrolls + 1,
                    max_scrolls,
                    max(0, after - before),
                    after,
                )
                if after >= max_count:
                    break
                page.evaluate("window.scrollBy(0, Math.max(window.innerHeight * 0.9, 600))")
                scrolls += 1

            posts.extend(self.snapshot_cards(api_cards, max_count))
            if posts:
                log.info("  ✅ 已从页面接口捕获 %s 条帖子", len(posts))
                return posts

            self.save_search_debug_artifacts(keyword, "api_empty", page=page)

            ready = self.wait_for_any_selector(
                "a[href*='/explore/']",
                "a[href*='xsec_token=']",
                ".note-item",
                "[class*='NoteCard']",
                "[class*='note-item']",
                "[class*='noteItem']",
                "section[class*='note']",
                "article",
                page=page,
            )
            if not ready:
                log.warning("  ⚠️ 未找到帖子元素，可能命中验证码/登录墙/页面改版")
                log.warning("  页面文本摘要: %s", self.get_body_snippet(300, page=page))
                return posts

            seen_ids: set[str] = set()
            scrolls = 0
            while len(posts) < max_count and scrolls < max_scrolls:
                if self.stop_requested:
                    log.info("  ⏹️ 收到停止请求，提前结束当前搜索")
                    break
                batch = self.extract_cards_from_page(keyword, page=page)
                log.info(
                    "  DOM 兜底轮次 %s/%s → 本次提取 %s 张卡片，累计 %s 条",
                    scrolls + 1,
                    max_scrolls,
                    len(batch),
                    len(posts),
                )
                for card in batch:
                    post_id = str(card.get("postId", ""))
                    if post_id and post_id not in seen_ids:
                        seen_ids.add(post_id)
                        posts.append(card)
                        if len(posts) >= max_count:
                            break
                page.evaluate("window.scrollBy(0, Math.max(window.innerHeight * 0.9, 600))")
                self.human_delay(1500, 2600)
                scrolls += 1
        except Exception as exc:
            if self.stop_requested:
                log.info("搜索 [%s] 因停止请求中断: %s", keyword, exc)
            else:
                log.error("搜索 [%s] 时出错: %s", keyword, exc, exc_info=True)
        finally:
            page.close()

        log.info("  搜索完成，采集 %s 条原始帖子", len(posts))
        return posts

    def fetch_post_detail(self, post_url: str) -> dict[str, Any]:
        if not post_url or self.stop_requested:
            return {}
        assert self.context is not None
        detail_page = self.context.new_page()
        try:
            detail_page.goto(post_url, wait_until="domcontentloaded")
            self.human_delay(1500, 3000)
            detail = detail_page.evaluate(
                """
                () => {
                    const desc = document.querySelector('#detail-desc, .desc, [class*="content"], [class*="desc"], meta[name="description"]');
                    const likes = document.querySelector('[class*="like-wrapper"] span, [class*="likes"], [class*="like"] [class*="count"]');
                    const collects = document.querySelector('[class*="collect-wrapper"] span, [class*="collects"], [class*="collect"] [class*="count"]');
                    const comments = document.querySelector('[class*="comment-wrapper"] span, [class*="comments"], [class*="comment"] [class*="count"]');
                    const imgs = Array.from(document.querySelectorAll('.swiper-slide img, [class*="image"] img')).slice(0, 9);
                    const author = document.querySelector('[class*="author"] [class*="name"], [class*="nickname"], [class*="user"] [class*="name"]');
                    const time = document.querySelector('[class*="date"], [class*="time"], time, [class*="publish"]');
                    const metaDesc = document.querySelector('meta[name="description"]')?.content?.trim() || '';
                    return {
                        content: desc?.innerText?.trim() || metaDesc,
                        likes: likes?.innerText?.trim() || '',
                        collects: collects?.innerText?.trim() || '',
                        comments: comments?.innerText?.trim() || '',
                        images: imgs.map(i => i.src).filter(s => s && !s.includes('data:')),
                        author: author?.innerText?.trim() || '',
                        publishTime: time?.innerText?.trim() || ''
                    };
                }
                """
            )
            if not isinstance(detail, dict):
                return {}
            self.normalize_card(detail)
            return detail
        except Exception as exc:
            log.debug("获取帖子详情失败 %s: %s", post_url, exc)
            return {}
        finally:
            detail_page.close()

    def comment_on_url(self, post_url: str, content: str) -> tuple[bool, str]:
        if not post_url or not content.strip():
            return False, "评论地址或内容为空"
        if self.stop_requested:
            return False, "收到停止请求"

        assert self.context is not None
        page = self.context.new_page()
        debug_label = self.extract_post_id(post_url) or "xhs_comment"
        input_selectors = (
            "textarea[placeholder*='说点什么']",
            "textarea[placeholder*='发表评论']",
            "textarea[placeholder*='留下你的评论']",
            "[contenteditable='true'][placeholder*='说点什么']",
            "[contenteditable='true'][placeholder*='评论']",
            "[class*='comment'] textarea",
            "[class*='comment'][contenteditable='true']",
            "[class*='comment'] [contenteditable='true']",
            "[class*='editor'] [contenteditable='true']",
            "[class*='input'] [contenteditable='true']",
            "textarea",
            "[contenteditable='true']",
            "[contenteditable='plaintext-only']",
        )
        opener_selectors = (
            "button:has-text('说点什么')",
            "button:has-text('写评论')",
            "div:has-text('说点什么')",
            "div:has-text('写评论')",
            "span:has-text('说点什么')",
            "text=说点什么...",
            "text=评论",
        )
        submit_selectors = (
            "button:has-text('发送')",
            "button:has-text('发布')",
            "button:has-text('提交')",
            "text=发送",
            "text=发布",
            "[class*='submit']",
            "[class*='send']",
        )

        try:
            log.info("💬 准备评论小红书帖子: %s", self.abbreviate_url(post_url))
            page.goto(post_url, wait_until="domcontentloaded")
            self.human_delay(2000, 3500)
            if self.is_xhs_login_required(page=page):
                self.save_search_debug_artifacts(debug_label, "comment_login_required", page=page)
                return False, "小红书页面要求登录"

            comment_text = content.strip()
            self.scroll_near_page_bottom(page=page)
            self.human_delay(500, 1200)
            self.click_first_visible(opener_selectors, page=page)
            self.human_delay(500, 1200)
            entered_selector = self.fill_first_editable(input_selectors, comment_text, page=page)
            filled = bool(entered_selector)
            if not filled:
                filled = self.fill_active_or_last_editable(comment_text, page=page)
            if not filled:
                self.save_search_debug_artifacts(debug_label, "comment_input_missing", page=page)
                return False, "未找到小红书评论输入框"
            if not self.has_visible_editable_text(comment_text, page=page):
                self.save_search_debug_artifacts(debug_label, "comment_input_unconfirmed", page=page)
                return False, "未确认小红书评论内容已写入输入框"

            self.human_delay(500, 1200)
            body_text = self.get_body_snippet(600, page=page)
            if "登录" in body_text and "评论" in body_text:
                self.save_search_debug_artifacts(debug_label, "comment_login_blocked", page=page)
                return False, "小红书页面要求登录后评论"
            confirmed, confirmation_reason = self.submit_comment_with_fallbacks(
                submit_selectors,
                comment_text,
                success_terms=("评论成功", "发送成功", "发布成功"),
                failure_terms=("评论过于频繁", "操作过于频繁", "发送失败", "发布失败", "评论失败"),
                page=page,
            )
            if not confirmed:
                if "频繁" in confirmation_reason:
                    self.save_search_debug_artifacts(debug_label, "comment_rate_limited", page=page)
                    return False, "小红书评论过于频繁或被限制"
                if "未找到评论提交按钮" in confirmation_reason:
                    self.save_search_debug_artifacts(debug_label, "comment_submit_missing", page=page)
                    return False, "未找到小红书评论提交按钮"
                self.save_search_debug_artifacts(debug_label, "comment_submit_unconfirmed", page=page)
                return False, confirmation_reason
            return True, "已提交小红书评论"
        except Exception as exc:
            self.save_search_debug_artifacts(debug_label, "comment_error", page=page)
            return False, f"小红书评论异常: {exc}"
        finally:
            page.close()

    def extract_cards_from_page(self, keyword: str, *, page: Page | None = None) -> list[dict[str, Any]]:
        active_page = page or self.page
        assert active_page is not None
        try:
            raw = active_page.evaluate(
                """
                () => {
                    const result = [];
                    const seen = new Set();
                    const links = document.querySelectorAll('a[href*="/explore/"], a[href*="xsec_token="]');
                    links.forEach(link => {
                        try {
                            const href = link.href || '';
                            const matched = href.match(/explore\\/([a-zA-Z0-9]+)/);
                            if (!matched || seen.has(matched[1])) return;
                            seen.add(matched[1]);

                            const container = link.closest('[class*="item"], [class*="card"], [class*="note"], section, article, li') || link.parentElement;
                            const titleEl = container?.querySelector('[class*="title"], [class*="desc"], [class*="content"] p');
                            const title = titleEl?.innerText?.trim()
                                || link.querySelector('span, p')?.innerText?.trim()
                                || link.innerText?.trim()
                                || '';

                            const authEl = container?.querySelector('[class*="author"], [class*="name"], [class*="nick"], [class*="user"]');
                            const author = authEl?.innerText?.trim() || '';

                            result.push({ postId: matched[1], title, author, url: href, content: title });
                        } catch (error) {}
                    });
                    return result;
                }
                """
            )
            cards: list[dict[str, Any]] = []
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict):
                        card = {str(key): value for key, value in item.items()}
                        card["keyword"] = keyword
                        self.normalize_card(card)
                        cards.append(card)
            return cards
        except Exception as exc:
            log.warning("提取卡片失败: %s", exc)
            return []


class DOUYINBrowser(BaseBrowser):
    DOUYIN_HOME = "https://www.douyin.com"
    DOUYIN_SEARCH = "https://www.douyin.com/search/{kw}?type=video"
    DOUYIN_VIDEO_ID_PATTERN = re.compile(r"/video/(\d+)")
    DOUYIN_VIDEO_NUMERIC_PATTERN = re.compile(r"^\d{8,}$")

    def looks_like_search_api(self, url: str) -> bool:
        return "douyin.com" in url and ("search" in url or "aweme" in url or "discover" in url)

    def extract_search_cards_from_json(
        self,
        root: Any,
        cards: dict[str, dict[str, Any]],
        keyword: str,
    ) -> int:
        return self.extract_douyin_cards_from_json(root, cards, keyword)

    def get_platform_label(self) -> str:
        return "抖音"

    def normalize_platform_post_id(self, post_id: str) -> str:
        return self.extract_douyin_post_id(post_id)

    def get_platform_session_path(self) -> Path:
        return Path(self.config.get_douyin_session_file())

    def get_platform_session_domains(self) -> tuple[str, ...]:
        return ("douyin.com",)

    def has_platform_session_cookie(self) -> bool:
        assert self.context is not None
        cookies = self.context.cookies()
        return any(
            "douyin.com" in str(cookie.get("domain"))
            and str(cookie.get("name")) in {"sessionid", "sessionid_ss", "passport_csrf_token"}
            for cookie in cookies
        )

    def extract_douyin_cards_from_json(
        self,
        root: Any,
        cards: dict[str, dict[str, Any]],
        keyword: str,
    ) -> int:
        before = len(self.snapshot_cards(cards, 10_000))
        stack: list[Any] = [root]
        while stack:
            node = stack.pop()
            if node is None:
                continue
            card = self.to_douyin_card_from_json(node, keyword)
            post_id = str(card.get("postId", ""))
            if post_id:
                cards.setdefault(post_id, card)

            if isinstance(node, dict):
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
        return len(self.snapshot_cards(cards, 10_000)) - before

    def to_douyin_card_from_json(self, node: Any, keyword: str) -> dict[str, Any]:
        if not isinstance(node, dict):
            return {}

        payload = self.first_object(
            node.get("aweme_info"),
            node.get("aweme"),
            node.get("data"),
            node if self.text_value(node, "aweme_id", "group_id", "desc") else None,
        )
        if not isinstance(payload, dict):
            return {}

        post_id = self.first_non_blank(
            self.text_value(payload, "aweme_id", "group_id", "id"),
            self.text_value(node, "aweme_id", "group_id", "id"),
        )
        post_id = self.extract_douyin_post_id(post_id)
        if not self.is_likely_douyin_video_id(post_id):
            return {}

        author_node = self.first_object(
            payload.get("author"),
            node.get("author"),
            payload.get("user"),
        )
        statistics = self.first_object(
            payload.get("statistics"),
            node.get("statistics"),
            payload.get("stats"),
        )
        title = self.first_non_blank(
            self.text_value(payload, "desc", "title"),
            self.text_value(node, "desc", "title"),
        )
        images = self.extract_douyin_images(payload, node)
        card = {
            "platform": "douyin",
            "postId": post_id,
            "title": title,
            "author": self.first_non_blank(
                self.text_value(author_node, "nickname", "unique_id", "short_id"),
                self.text_value(payload, "author_name"),
            ),
            "authorId": self.first_non_blank(
                self.text_value(author_node, "sec_uid", "uid", "id"),
                self.text_value(payload, "author_user_id"),
            ),
            "content": title,
            "publishTime": self.extract_douyin_publish_time(payload, node),
            "likes": self.first_non_blank(
                self.text_value(statistics, "digg_count"),
                self.text_value(payload, "digg_count"),
            ),
            "collects": self.first_non_blank(
                self.text_value(statistics, "collect_count"),
                self.text_value(payload, "collect_count"),
            ),
            "comments": self.first_non_blank(
                self.text_value(statistics, "comment_count"),
                self.text_value(payload, "comment_count"),
            ),
            "url": self.build_douyin_video_url(post_id),
            "images": images,
            "keyword": keyword,
        }
        self.normalize_card(card)
        return card if (card.get("title") or card.get("author")) else {}

    def extract_douyin_images(self, *nodes: dict[str, Any] | None) -> list[str]:
        images: list[str] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            for field_name in ("video", "cover", "origin_cover", "dynamic_cover"):
                item = node.get(field_name)
                if isinstance(item, dict):
                    url = self.pick_first_url(item.get("url_list"))
                    if url:
                        images.append(url)
                elif isinstance(item, list):
                    for child in item:
                        if isinstance(child, dict):
                            url = self.pick_first_url(child.get("url_list"))
                            if url:
                                images.append(url)
        deduped: list[str] = []
        for image in images:
            if image and image not in deduped:
                deduped.append(image)
        return deduped[:9]

    def build_douyin_video_url(self, post_id: str) -> str:
        if not self.is_likely_douyin_video_id(post_id):
            return ""
        return f"{self.DOUYIN_HOME}/video/{post_id}"

    def extract_douyin_post_id(self, value: str) -> str:
        if not value:
            return ""
        matched = self.DOUYIN_VIDEO_ID_PATTERN.search(value)
        if matched:
            candidate = matched.group(1)
            return candidate if self.is_likely_douyin_video_id(candidate) else ""
        candidate = value.strip()
        return candidate if self.is_likely_douyin_video_id(candidate) else ""

    def is_likely_douyin_video_id(self, value: str) -> bool:
        return bool(value and self.DOUYIN_VIDEO_NUMERIC_PATTERN.match(value.strip()))

    def extract_douyin_publish_time(self, *nodes: dict[str, Any] | None) -> str:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            raw = self.first_non_blank(
                self.text_value(node, "create_time", "publish_time", "time"),
            )
            if not raw:
                continue
            if raw.isdigit() and len(raw) >= 10:
                try:
                    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(raw)))
                except Exception:
                    return raw
            return raw
        return ""

    def has_active_douyin_session(self) -> bool:
        assert self.context is not None
        cookies = self.context.cookies()
        has_session_cookie = any(
            "douyin.com" in str(cookie.get("domain"))
            and str(cookie.get("name")) in {"sessionid", "sessionid_ss", "passport_csrf_token"}
            for cookie in cookies
        )
        return has_session_cookie and not self.is_douyin_login_required()

    def is_douyin_accessible(self, *, page: Page | None = None) -> bool:
        active_page = page or self.page
        assert active_page is not None
        if active_page.url.startswith(f"{self.DOUYIN_HOME}/search/"):
            return self.wait_for_any_selector(
                "a[href*='/video/']",
                "[data-e2e='search-result-container']",
                "[class*='search-result']",
                "article",
                page=active_page,
            )
        if self.is_douyin_login_required(page=active_page) or self.is_douyin_blocked(page=active_page):
            return False
        return bool(self.get_body_snippet(200, page=active_page))

    def is_douyin_login_required(self, *, page: Page | None = None) -> bool:
        return (
            self.is_visible("text=登录后即可查看", page=page)
            or self.is_visible("text=登录后继续", page=page)
            or self.is_visible("text=扫码登录", page=page)
            or self.is_visible("text=手机号登录", page=page)
            or self.is_visible("[class*='login']", page=page)
        )

    def is_douyin_sms_verification_required(self, *, page: Page | None = None) -> bool:
        return (
            self.is_visible("text=短信验证码", page=page)
            or self.is_visible("text=请输入验证码", page=page)
            or self.is_visible("text=请输入短信验证码", page=page)
            or self.is_visible("text=发送验证码", page=page)
            or self.is_visible("text=获取验证码", page=page)
            or self.is_visible("text=手机号验证", page=page)
            or self.is_visible("text=验证手机号", page=page)
            or self.is_visible("input[placeholder*='验证码']", page=page)
        )

    def is_douyin_blocked(self, *, page: Page | None = None) -> bool:
        return (
            self.is_visible("text=验证码", page=page)
            or self.is_visible("text=验证", page=page)
            or self.is_visible("text=请完成下列验证后继续", page=page)
            or self.is_visible("text=拖动完成上方拼图", page=page)
            or self.is_visible("text=按住左边按钮拖动完成上方拼图", page=page)
            or self.is_visible("text=请选择所有包含上述描述的图片", page=page)
            or self.is_visible("text=拖拽到这里", page=page)
            or self.is_visible("text=提交", page=page)
            or self.is_visible("text=访问过于频繁", page=page)
            or self.is_visible("text=安全验证", page=page)
        )

    def mark_douyin_risk_triggered(self) -> None:
        with self._risk_lock:
            self._douyin_risk_triggered = True

    def consume_douyin_risk_triggered(self) -> bool:
        with self._risk_lock:
            triggered = self._douyin_risk_triggered
            self._douyin_risk_triggered = False
            return triggered

    def wait_for_douyin_sms_verification(self, *, page: Page | None = None, timeout_seconds: int = 90) -> bool:
        active_page = page or self.page
        assert active_page is not None
        if not self.is_douyin_sms_verification_required(page=active_page):
            return True
        if self.config.is_browser_headless():
            return False

        log.warning("⚠️  抖音评论触发短信验证，请在浏览器中完成验证，最多等待 %s 秒...", timeout_seconds)
        deadline = time.monotonic() + max(timeout_seconds, 1)
        while time.monotonic() <= deadline:
            if self.stop_requested:
                return False
            if not self.is_douyin_sms_verification_required(page=active_page):
                self.human_delay(800, 1400)
                return True
            try:
                active_page.wait_for_timeout(1000)
            except Exception:
                break
        return False

    def ensure_logged_in(self) -> bool:
        assert self.page is not None
        log.info("🔐 检查抖音访问状态...")
        if not self.goto_with_handling(self.DOUYIN_HOME):
            return False
        self.human_delay(2000, 3000)

        if self.has_active_douyin_session():
            self.save_session()
            log.info("✅ 抖音登录状态正常")
            return True

        if self.is_douyin_accessible(page=self.page):
            self.save_session()
            log.info("✅ 抖音页面可访问，将继续执行抖音采集")
            return True

        log.warning("⚠️  抖音当前不可访问或需要登录")
        self.save_search_debug_artifacts("douyin", "login_required", page=self.page)
        if self.config.is_browser_headless():
            log.warning("请将 settings.yaml 中 browser.headless 改为 false，完成抖音登录后重试")
            return False

        log.info("请在浏览器中完成抖音登录/验证，然后在终端按 Enter 键继续...")
        try:
            input()
        except EOFError:
            pass
        self.human_delay(1500, 2500)
        if self.has_active_douyin_session() or self.is_douyin_accessible(page=self.page):
            self.save_session()
            log.info("✅ 抖音状态校验通过")
            return True

        log.warning("⚠️  抖音状态仍不可用")
        return False

    def search_posts(self, keyword: str, max_count: int) -> list[dict[str, Any]]:
        assert self.context is not None
        page = self.context.new_page()
        page.on(
            "response",
            lambda response: self.process_search_response(
                response, page=page, cards=api_cards, keyword=keyword, platform="douyin"
            ),
        )
        log.info("🎵 搜索抖音关键词: [%s]，目标 %s 条", keyword, max_count)
        posts: list[dict[str, Any]] = []
        api_cards: dict[str, dict[str, Any]] = {}

        try:
            url = self.DOUYIN_SEARCH.format(kw=quote(keyword))
            page.goto(url, wait_until="domcontentloaded")
            self.human_delay(3000, 5000)

            ready = self.wait_for_any_selector(
                "a[href*='/video/']",
                "[data-e2e='search-result-container']",
                "[class*='search-result']",
                "[class*='video-card']",
                "article",
                page=page,
            )
            if not ready:
                if self.is_douyin_blocked(page=page) and self._resolve_verification(page, keyword):
                    ready = self.wait_for_any_selector(
                        "a[href*='/video/']",
                        "[data-e2e='search-result-container']",
                        "[class*='search-result']",
                        "[class*='video-card']",
                        "article",
                        page=page,
                    )
                if self.is_douyin_login_required(page=page):
                    log.warning("  ⚠️ 抖音搜索页要求登录，当前无法抓取")
                elif self.is_douyin_blocked(page=page):
                    log.warning("  ⚠️ 抖音搜索页疑似触发验证或风控")
                else:
                    log.warning("  ⚠️ 抖音搜索页未找到结果元素，页面结构可能已变化")
                log.warning("  页面文本摘要: %s", self.get_body_snippet(300, page=page))
                self.save_search_debug_artifacts(keyword, "douyin_unavailable", page=page)

            scrolls = 0
            max_scrolls = max(4, max_count // 4)
            while len(self.snapshot_cards(api_cards, max_count)) < max_count and scrolls < max_scrolls:
                if self.stop_requested:
                    log.info("  ⏹️ 收到停止请求，提前结束当前搜索")
                    break
                batch = self.extract_douyin_cards_from_page(keyword, page)
                for card in batch:
                    post_id = str(card.get("postId", ""))
                    if post_id:
                        api_cards.setdefault(post_id, card)
                if len(self.snapshot_cards(api_cards, max_count)) >= max_count:
                    break
                page.evaluate("window.scrollBy(0, Math.max(window.innerHeight * 0.9, 700))")
                self.human_delay(1500, 2600)
                scrolls += 1

            posts.extend(self.snapshot_cards(api_cards, max_count))
            if not posts:
                if self.is_douyin_blocked(page=page) and self._resolve_verification(page, keyword):
                    return self.search_posts(keyword, max_count)
                if self.is_douyin_login_required(page=page):
                    log.warning("  抖音未返回结果：需要登录")
                elif self.is_douyin_blocked(page=page):
                    log.warning("  抖音未返回结果：疑似被风控/验证拦截")
                else:
                    log.warning("  抖音未返回结果：当前关键词没有可解析内容或页面结构已变更")
        except Exception as exc:
            if self.stop_requested:
                log.info("搜索抖音 [%s] 因停止请求中断: %s", keyword, exc)
            else:
                log.error("搜索抖音 [%s] 时出错: %s", keyword, exc, exc_info=True)
        finally:
            page.close()

        log.info("  抖音搜索完成，采集 %s 条原始帖子", len(posts))
        return posts

    def fetch_post_detail(self, post_url: str) -> dict[str, Any]:
        if not post_url or self.stop_requested:
            return {}
        assert self.context is not None
        detail_page = self.context.new_page()
        try:
            detail_page.goto(post_url, wait_until="domcontentloaded")
            self.human_delay(1500, 3000)
            detail = detail_page.evaluate(
                """
                () => {
                    const text = selector =>
                        document.querySelector(selector)?.innerText?.trim() || '';
                    const meta = name =>
                        document.querySelector(`meta[name="${name}"]`)?.content?.trim() || '';
                    const cover = document.querySelector('video')?.poster
                        || document.querySelector('img[src]')?.src
                        || '';

                    return {
                        title: text('h1') || meta('title') || document.title || '',
                        content: meta('description') || text('[data-e2e="video-desc"]') || text('h1') || '',
                        author: text('[data-e2e="video-author-name"]') || text('[class*="author"]') || '',
                        likes: text('[data-e2e="like-count"]') || text('[class*="like"] [class*="count"]'),
                        comments: text('[data-e2e="comment-count"]') || text('[class*="comment"] [class*="count"]'),
                        images: cover ? [cover] : [],
                    };
                }
                """
            )
            if not isinstance(detail, dict):
                return {}
            detail["platform"] = "douyin"
            self.normalize_card(detail)
            return detail
        except Exception as exc:
            log.debug("获取抖音详情失败 %s: %s", post_url, exc)
            return {}
        finally:
            detail_page.close()

    def comment_on_url(self, post_url: str, content: str) -> tuple[bool, str]:
        if not post_url or not content.strip():
            return False, "评论地址或内容为空"
        if self.stop_requested:
            return False, "收到停止请求"

        assert self.context is not None
        page = self.context.new_page()
        expected_post_id = self.extract_douyin_post_id(post_url)
        debug_label = expected_post_id or "douyin_comment"
        opener_selectors = (
            "[data-e2e='comment-input']",
            "[class*='comment-input']",
            "text=留下你的精彩评论吧",
            "text=留下你的评论",
        )
        input_selectors = (
            "[contenteditable='true'].public-DraftEditor-content",
            ".public-DraftEditor-content[contenteditable='true']",
            "[class*='DraftEditor'] [contenteditable='true']",
            "[data-e2e*='comment'] [contenteditable='true']",
            "[class*='comment-input'] textarea",
            "[class*='comment-input'] [contenteditable='true']",
            "[class*='comment'] [contenteditable='true']",
        )
        submit_selectors = (
            "button:has-text('发送')",
            "button:has-text('发布')",
            "text=发送",
            "text=发布",
            "[data-e2e*='comment-submit']",
            "[class*='comment-submit']",
            "[class*='send']",
        )

        try:
            def ensure_comment_editor_ready(*, reopen_post: bool = False) -> tuple[bool, str]:
                if reopen_post or self.extract_douyin_post_id(page.url) != expected_post_id:
                    log.info("💬 重新打开抖音视频详情页以继续评论: %s", self.abbreviate_url(post_url))
                    page.goto(post_url, wait_until="domcontentloaded")
                    self.human_delay(2000, 3500)
                if self.is_douyin_blocked(page=page) and not self._resolve_verification(page, debug_label):
                    return False, "抖音页面触发验证或风控"
                if self.is_douyin_login_required(page=page):
                    self.save_search_debug_artifacts(debug_label, "comment_login_required", page=page)
                    return False, "抖音页面要求登录"
                self.click_first_visible(opener_selectors, page=page)
                entered_selector = self.fill_first_editable(input_selectors, comment_text, page=page)
                if not entered_selector:
                    self.save_search_debug_artifacts(debug_label, "comment_input_missing", page=page)
                    return False, "未找到抖音评论输入框"
                if not self.has_text_in_selectors(input_selectors, comment_text, page=page):
                    self.save_search_debug_artifacts(debug_label, "comment_input_unconfirmed", page=page)
                    return False, "未确认抖音评论内容已写入输入框"
                return True, ""

            log.info("💬 准备评论抖音视频: %s", self.abbreviate_url(post_url))
            page.goto(post_url, wait_until="domcontentloaded")
            self.human_delay(2000, 3500)
            if self.is_douyin_blocked(page=page) and not self._resolve_verification(page, debug_label):
                return False, "抖音页面触发验证或风控"
            if self.is_douyin_login_required(page=page):
                self.save_search_debug_artifacts(debug_label, "comment_login_required", page=page)
                return False, "抖音页面要求登录"

            comment_text = content.strip()
            baseline_markers = self.capture_comment_submission_markers(comment_text, page=page)
            ready, ready_message = ensure_comment_editor_ready()
            if not ready:
                return False, ready_message

            self.human_delay(500, 1200)
            if self.is_douyin_login_required(page=page):
                self.save_search_debug_artifacts(debug_label, "comment_login_blocked", page=page)
                return False, "抖音页面要求登录后评论"
            if self.is_douyin_blocked(page=page):
                self.save_search_debug_artifacts(debug_label, "comment_rate_limited", page=page)
                return False, "抖音评论过于频繁或被限制"
            for submit_round in range(2):
                clicked_editor_action = self.click_editor_row_end_action(comment_text, page=page)
                if clicked_editor_action:
                    self.human_delay(800, 1500)
                    confirmed, confirmation_reason = self.wait_for_comment_submission_confirmation(
                        comment_text,
                        success_terms=("评论成功", "发送成功", "发布成功", "评论已发送"),
                        failure_terms=("评论过于频繁", "操作过于频繁", "发送失败", "发布失败", "评论失败", "暂时无法评论"),
                        page=page,
                        timeout_ms=2500,
                    )
                else:
                    confirmed, confirmation_reason = self.submit_comment_with_fallbacks(
                        submit_selectors,
                        comment_text,
                        success_terms=("评论成功", "发送成功", "发布成功", "评论已发送"),
                        failure_terms=("评论过于频繁", "操作过于频繁", "发送失败", "发布失败", "评论失败", "暂时无法评论"),
                        page=page,
                        allow_keyboard_shortcuts=False,
                    )

                if self.is_douyin_sms_verification_required(page=page):
                    self.save_search_debug_artifacts(debug_label, "comment_sms_verification", page=page)
                    if not self.wait_for_douyin_sms_verification(page=page, timeout_seconds=90):
                        return False, "抖音评论触发短信验证"
                    visible_confirmed, visible_reason = self.wait_for_posted_comment_visibility(
                        comment_text,
                        baseline_occurrences=int(baseline_markers.get("nonEditableOccurrences", 0) or 0),
                        baseline_comment_count=baseline_markers.get("commentCount"),
                        failure_terms=("评论过于频繁", "操作过于频繁", "发送失败", "发布失败", "评论失败", "暂时无法评论"),
                        page=page,
                        timeout_ms=2500,
                    )
                    if visible_confirmed:
                        return True, visible_reason
                    if submit_round >= 1:
                        self.save_search_debug_artifacts(debug_label, "comment_post_unverified", page=page)
                        return False, "短信验证完成后仍未确认评论已提交"
                    ready, ready_message = ensure_comment_editor_ready(reopen_post=True)
                    if not ready:
                        return False, ready_message
                    continue

                visible_confirmed, visible_reason = self.wait_for_posted_comment_visibility(
                    comment_text,
                    baseline_occurrences=int(baseline_markers.get("nonEditableOccurrences", 0) or 0),
                    baseline_comment_count=baseline_markers.get("commentCount"),
                    failure_terms=("评论过于频繁", "操作过于频繁", "发送失败", "发布失败", "评论失败", "暂时无法评论"),
                    page=page,
                    timeout_ms=7000 if confirmed else 4000,
                )
                if self.is_douyin_sms_verification_required(page=page):
                    self.save_search_debug_artifacts(debug_label, "comment_sms_verification", page=page)
                    if not self.wait_for_douyin_sms_verification(page=page, timeout_seconds=90):
                        return False, "抖音评论触发短信验证"
                    ready, ready_message = ensure_comment_editor_ready(reopen_post=True)
                    if not ready:
                        return False, ready_message
                    continue
                if visible_confirmed:
                    return True, visible_reason

                if not confirmed:
                    if "频繁" in confirmation_reason:
                        self.save_search_debug_artifacts(debug_label, "comment_rate_limited", page=page)
                        return False, "抖音评论过于频繁或被限制"
                    if "未找到评论提交按钮" in confirmation_reason:
                        self.save_search_debug_artifacts(debug_label, "comment_submit_missing", page=page)
                        return False, "未找到抖音评论提交按钮"
                    self.save_search_debug_artifacts(debug_label, "comment_submit_unconfirmed", page=page)
                    return False, confirmation_reason

                if "频繁" in visible_reason:
                    self.save_search_debug_artifacts(debug_label, "comment_rate_limited", page=page)
                    return False, "抖音评论过于频繁或被限制"
                self.save_search_debug_artifacts(debug_label, "comment_post_unverified", page=page)
                return False, visible_reason

            self.save_search_debug_artifacts(debug_label, "comment_post_unverified", page=page)
            return False, "抖音评论未通过提交验证"
        except Exception as exc:
            self.save_search_debug_artifacts(debug_label, "comment_error", page=page)
            return False, f"抖音评论异常: {exc}"
        finally:
            page.close()

    def extract_douyin_cards_from_page(self, keyword: str, page: Page) -> list[dict[str, Any]]:
        try:
            raw = page.evaluate(
                """
                () => {
                    const result = [];
                    const seen = new Set();
                    const links = document.querySelectorAll('a[href*="/video/"]');
                    links.forEach(link => {
                        try {
                            const href = link.href || '';
                            const matched = href.match(/\\/video\\/(\\d+)/);
                            if (!matched || seen.has(matched[1])) return;
                            seen.add(matched[1]);
                            const container = link.closest('article, [class*="card"], [class*="item"], li, div') || link.parentElement;
                            const title = container?.querySelector('h3, h2, [class*="title"], [data-e2e="search-card-desc"]')?.innerText?.trim()
                                || link.innerText?.trim()
                                || '';
                            const author = container?.querySelector('[class*="author"], [class*="name"], [data-e2e="video-author-name"]')?.innerText?.trim()
                                || '';
                            const likeText = container?.querySelector('[class*="like"], [data-e2e="video-like-count"]')?.innerText?.trim()
                                || '';
                            const cover = container?.querySelector('img')?.src || '';
                            result.push({
                                platform: 'douyin',
                                postId: matched[1],
                                title,
                                content: title,
                                author,
                                likes: likeText,
                                url: href,
                                images: cover ? [cover] : [],
                            });
                        } catch (error) {}
                    });
                    return result;
                }
                """
            )
            cards: list[dict[str, Any]] = []
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict):
                        card = {str(key): value for key, value in item.items()}
                        card["keyword"] = keyword
                        card["platform"] = "douyin"
                        self.normalize_card(card)
                        cards.append(card)
            return cards
        except Exception as exc:
            log.warning("提取抖音卡片失败: %s", exc)
            return []

    def _resolve_verification(self, page: Page, keyword: str) -> bool:
        if not self.is_douyin_blocked(page=page):
            return False
        log.warning("  ⚠️ 抖音当前关键词触发滑块/安全验证: %s", keyword)
        self.save_search_debug_artifacts(keyword, "douyin_captcha", page=page)
        if self.config.is_browser_headless():
            log.warning("  当前为无头模式，无法人工完成抖音验证")
            self.mark_douyin_risk_triggered()
            return False
        log.info("请在浏览器中完成抖音滑块/安全验证，然后在终端按 Enter 键继续...")
        try:
            input()
        except EOFError:
            pass
        self.human_delay(1000, 2000)
        resolved = not self.is_douyin_blocked(page=page)
        if not resolved:
            self.mark_douyin_risk_triggered()
        return resolved
