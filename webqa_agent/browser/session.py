"""
BrowserSession - 浏览器会话管理（重构版）

设计原则：
1. 单一入口：Session 是唯一对外接口，不再暴露 Driver
2. 属性访问：page/context 通过属性而非方法访问
3. 资源安全：支持 async with，自动清理资源
"""

import asyncio
import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Union
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from webqa_agent.browser.config import DEFAULT_CONFIG


class BrowserSession:
    """浏览器会话 - 唯一对外接口"""

    def __init__(self, session_id: str = None, browser_config: Dict[str, Any] = None):
        self.session_id = session_id or str(uuid.uuid4())
        self.config = {**DEFAULT_CONFIG, **(browser_config or {})}
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._playwright = None
        self._is_closed = False
        self._lock = asyncio.Lock()  # 实例级别锁，每个 session 独立

    # ==================== 属性访问 ====================
    @property
    def page(self) -> Page:
        self._check_state()
        return self._page

    @property
    def context(self) -> BrowserContext:
        self._check_state()
        return self._context

    def is_closed(self) -> bool:
        return self._is_closed

    # ==================== 生命周期 ====================
    async def initialize(self) -> "BrowserSession":
        """初始化浏览器会话"""
        async with self._lock:
            if self._is_closed:
                raise RuntimeError("Session already closed")
            if self._page:
                return self  # 已初始化

            cfg = self.config
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=cfg["headless"],
                args=[
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-gpu",
                    "--force-device-scale-factor=1",
                    f'--window-size={cfg["viewport"]["width"]},{cfg["viewport"]["height"]}',
                    "--block-new-web-contents",
                ],
            )
            self._context = await self._browser.new_context(
                viewport=cfg["viewport"],
                device_scale_factor=1,  # 设备像素比，css像素到物理像素的缩放比。值越高，截图更大
                locale=cfg.get("language", "en-US"),
            )
            self._page = await self._context.new_page()

            # 拦截新标签页，session 保持“单页化”，测试逻辑基本只在 self._page 上跑
            self._context.on("page", self._close_unexpected_page)
            self._page.on("popup", self._close_unexpected_page)

            logging.debug(f"Session {self.session_id} initialized")
            return self

    async def close(self):
        """关闭会话，释放资源"""
        async with self._lock:
            if self._is_closed:
                return
            self._is_closed = True

            try:
                if self._browser:
                    await self._browser.close()
                if self._playwright:
                    await self._playwright.stop()
            except Exception as e:
                logging.error(f"Error closing session {self.session_id}: {e}")
            finally:
                self._page = self._context = self._browser = self._playwright = None
                logging.debug(f"Session {self.session_id} closed")

    # ==================== 导航 ====================
    async def navigate_to(self, url: str, cookies: Optional[Union[str, List[dict]]] = None, **kwargs):
        """导航到 URL，支持 Cookie 注入"""
        self._check_state()
        logging.debug(f"Session {self.session_id} navigating to: {url}")

        # Cookie 注入
        if cookies:
            try:
                cookie_list = json.loads(cookies) if isinstance(cookies, str) else cookies
                cookie_list = [cookies] if isinstance(cookies, dict) else list(cookies)
                await self._context.add_cookies(cookie_list)
                logging.debug("Cookies added successfully")
            except Exception as e:
                logging.error(f"Failed to add cookies: {e}")

        # 导航
        kwargs.setdefault("timeout", 60000)
        kwargs.setdefault("wait_until", "domcontentloaded")

        try:
            await self._page.goto(url, **kwargs)
            await self._page.wait_for_load_state("networkidle", timeout=60000)
            is_blank = await self._page.evaluate(
                "!document.body || document.body.innerText.trim().length === 0"
            )
            logging.debug(f"Page content check: is_blank={is_blank}")
        except Exception as e:
            logging.warning(f"Error during navigation: {e}")
            is_blank = False  # Fail-open: 检测失败时不阻塞

        if is_blank:
            raise RuntimeError(
                f"Page load timeout or blank content after navigation to {url}, "
                "Please check the url and try again."
            )

    async def get_url(self) -> tuple[str, str]:
        """返回当前 URL 和标题"""
        self._check_state()
        return self._page.url, await self._page.title()

    # ==================== 内部方法 ====================
    def _check_state(self):
        if self._is_closed or not self._page:
            raise RuntimeError("Session not initialized or closed")

    async def _close_unexpected_page(self, page: Page):
        try:
            await page.close()
            logging.warning(f"Closed unexpected page: {page.url}")
        except Exception as e:
            logging.debug(f"Failed to close unexpected page: {e}")

    # ==================== 上下文管理器 ====================
    # 浏览器生命周期管理，async with BrowserSession as s, 确保初始化和异常退出释放资源
    async def __aenter__(self) -> "BrowserSession":
        await self.initialize()
        return self

    async def __aexit__(self, *args):
        await self.close()
