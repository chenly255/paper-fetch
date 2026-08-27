"""CARSI 机构登录下载用的浏览器会话（原生 Playwright，async 版）。

移植自 feishu_agent browser.py 的 `_launch_playwright` 分支，删掉所有
CloakBrowser 逻辑（红线：只用原生 Playwright），并由同步 Playwright 改成
``playwright.async_api`` —— PaperPilot 后端是 asyncio，必须全程 await。

隐身默认值：
- 启动参数 ``--disable-blink-features=AutomationControlled``
- ``add_init_script`` 抹掉 ``navigator.webdriver``
- ``locale="en-US"`` + ``timezone_id="Asia/Shanghai"``
- viewport 1366x900

用法::

    async with BrowserSession() as session:
        await session.page.goto("https://example.com")
        # session.page / session.context 可直接用
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 全局无头浏览器并发上限：所有走 BrowserSession 的下载（robust_fetch / browser_fetch / carsi）
# 共用这个信号量，防止 20 人课题组同时下付费/挑战站论文时同时拉起几十个 Chromium 打爆内存。
# 每个 Chromium ~150-300MB，本机还常驻 Marker GPU worker，故默认只放行 2 个并发。
_MAX_BROWSERS = max(1, int(os.getenv("PAPER_DOWNLOAD_MAX_BROWSERS", "2")))
_browser_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    """惰性建信号量（绑定到当前事件循环；模块级直接建会绑错 loop）。"""
    global _browser_semaphore
    if _browser_semaphore is None:
        _browser_semaphore = asyncio.Semaphore(_MAX_BROWSERS)
    return _browser_semaphore


def _bool_env(name: str, default: bool) -> bool:
    val = os.getenv(name, "").strip().lower()
    if not val:
        return default
    return val not in ("0", "false", "no", "off")


class BrowserSession:
    """单页浏览器会话，自带隐身默认值（原生 Playwright，async）。

    headless 默认 True（服务器无显示器）。chromium 用 Playwright 缓存的版本
    （自动从 ``~/.cache/ms-playwright/chromium-1223`` 找）。
    """

    def __init__(
        self,
        *,
        headless: bool | None = None,
        downloads_path: Path | None = None,
        page_timeout_sec: int = 60,
        viewport: dict | None = None,
        user_agent: str | None = None,
        locale: str = "en-US",
        timezone: str = "Asia/Shanghai",
        proxy_server: str | None = None,
    ) -> None:
        self.headless = (
            headless if headless is not None
            else _bool_env("PAPER_DOWNLOAD_HEADLESS", True)
        )
        self.downloads_path = Path(downloads_path) if downloads_path else None
        self.page_timeout_sec = int(page_timeout_sec)
        self.viewport = viewport or {"width": 1366, "height": 900}
        self.user_agent = user_agent
        self.locale = locale
        self.timezone = timezone
        # 出口代理（http://127.0.0.1:<mihomo mixed-port>）：None=直连。
        # 只允许来自 proxy_pool_service 的判定（2026-08-22 产品自管网络出口）；
        # 机构登录下载（library_proxy_adapter）不传此参数，保持原有出口。
        self.proxy_server = proxy_server

        self._pw_handle: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._sem_held: bool = False

    @property
    def page(self) -> Any:
        if self._page is None:
            raise RuntimeError("BrowserSession.page accessed before __aenter__")
        return self._page

    @property
    def context(self) -> Any:
        return self._context

    async def __aenter__(self) -> BrowserSession:
        if self.downloads_path:
            self.downloads_path.mkdir(parents=True, exist_ok=True)
        # 先抢并发额度（全局限 _MAX_BROWSERS），抢到才启动 Chromium；抢不到就排队等。
        await _get_semaphore().acquire()
        self._sem_held = True
        try:
            await self._launch_playwright()
        except Exception:
            await self._cleanup()
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._cleanup()

    async def _launch_playwright(self) -> None:
        from playwright.async_api import async_playwright

        self._pw_handle = await async_playwright().start()
        # 隐身启动参数（贴近真实 Chromium 表面特征）
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--no-sandbox" if os.geteuid() == 0 else "--disable-setuid-sandbox",
        ]
        launch_kwargs: dict[str, Any] = dict(headless=self.headless, args=launch_args)
        if self.proxy_server:
            launch_kwargs["proxy"] = {"server": self.proxy_server}
        self._browser = await self._pw_handle.chromium.launch(**launch_kwargs)
        ctx_kwargs: dict[str, Any] = dict(
            viewport=self.viewport,
            locale=self.locale,
            timezone_id=self.timezone,
            accept_downloads=True,
        )
        if self.user_agent:
            ctx_kwargs["user_agent"] = self.user_agent
        self._context = await self._browser.new_context(**ctx_kwargs)
        # JS 隐身：抹掉 navigator.webdriver（在每个新文档执行）
        try:
            await self._context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
        except Exception as exc:  # REASON: 隐身脚本注入失败不致命，主流程仍可跑
            logger.warning("add_init_script 注入 webdriver 隐身失败：%s", exc)
        self._page = await self._context.new_page()
        self._page.set_default_timeout(self.page_timeout_sec * 1000)

    async def _cleanup(self) -> None:
        for closer in (self._page, self._context, self._browser):
            if closer is None:
                continue
            try:
                await closer.close()
            except Exception:  # REASON: 关闭顺序中任一步失败都不应阻断后续清理
                pass
        if self._pw_handle is not None:
            try:
                await self._pw_handle.stop()
            except Exception:  # REASON: Playwright 句柄 stop 失败无关紧要，进程退出会回收
                pass
        self._page = self._context = self._browser = self._pw_handle = None
        # 释放并发额度（放在最后，确保 Chromium 真正关掉后才让下一个排队的启动）
        if self._sem_held:
            self._sem_held = False
            try:
                _get_semaphore().release()
            except Exception:  # REASON: 释放失败（理论不会）不应阻断清理
                pass
