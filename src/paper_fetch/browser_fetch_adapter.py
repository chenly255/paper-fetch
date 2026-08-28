"""browser_fetch_adapter：通用「无头浏览器抓 landing page → 读 citation_pdf_url → 下 PDF」兜底。

publisher_direct 覆盖有已知模板的出版商；这一层是**通用兜底**——对任何带 landing page 的
论文，用浏览器打开（过 JS 挑战 + JS 渲染），从活的 DOM 里读 citation_pdf_url（比 httpx 静态
解析更稳，能拿到 JS 注入的 meta），再用浏览器会话下 PDF。

放在降级链靠后（OA/API 全失败后）：开销大（浏览器），由 FetchBudget 限到每次下载最多 1 次。
只对真付费/挑战站有意义；免费站（arXiv/PMC 等）前面的快路早命中了。
"""
from __future__ import annotations

import logging
from urllib.parse import urljoin

from .robust_fetch import (
    FetchBudget,
    _browser_context_get,
    _browser_exit_guard,
    _ensure_public_async,
    _looks_like_pdf,
    _should_skip_cooled,
)

logger = logging.getLogger(__name__)

_NAV_WAIT_MS = 3500
_REQ_TIMEOUT_MS = 60_000

# 从活 DOM 读 citation_pdf_url + 常见 PDF 链接的 JS
_EXTRACT_JS = """
() => {
  const pick = (sel, attr) => {
    const el = document.querySelector(sel);
    return el ? (el.getAttribute(attr) || '') : '';
  };
  let u = pick('meta[name="citation_pdf_url"]', 'content');
  if (!u) u = pick('link[rel="alternate"][type="application/pdf"]', 'href');
  if (!u) {
    const a = document.querySelector('a[href$=".pdf"], a[href*="/pdf"]');
    if (a) u = a.getAttribute('href') || '';
  }
  return u;
}
"""


async def fetch_via_browser_landing(
    landing_url: str | None, *, budget: FetchBudget | None = None,
) -> bytes | None:
    """浏览器打开 landing page，读出 citation_pdf_url，再用同会话下 PDF。"""
    if not landing_url or budget is None or budget.browser <= 0:
        return None
    try:
        from .browser_session import BrowserSession
    except Exception as exc:
        logger.debug("browser_fetch: 浏览器会话不可用（%s）", exc)
        return None

    # SSRF 入口校验（与 robust_fetch._browser_get 同款军规，R2-1）：landing_url 可能
    # 来自用户可控 paper_url，内网/本机/云元数据地址不进浏览器；冷却中的域名也不
    # 浪费浏览器预算。
    if await _ensure_public_async(landing_url) is None:
        return None
    if _should_skip_cooled(landing_url):
        return None

    budget.browser -= 1
    # 境外源走内嵌 mihomo（proxy_pool_service 统一判定；直连域不传代理）。
    from .proxy import proxy_for_url

    try:
        async with BrowserSession(proxy_server=proxy_for_url(landing_url)) as session:
            page = session.page
            # 出口重定向拦截（与 robust_fetch._browser_get 同款军规）：context 级路由
            # 逐跳校验导航 URL，公网 landing 302 到内网时第二跳被 abort。
            await session.context.route("**/*", _browser_exit_guard)
            try:
                await page.goto(landing_url, wait_until="domcontentloaded")
                await page.wait_for_timeout(_NAV_WAIT_MS)
            except Exception as exc:
                logger.debug("browser_fetch: 打开 landing %s 失败（%s）", landing_url, exc)
                return None

            try:
                pdf_url = await page.evaluate(_EXTRACT_JS)
            except Exception as exc:
                logger.debug("browser_fetch: 读 citation_pdf_url 失败（%s）", exc)
                pdf_url = ""

            if not pdf_url:
                logger.debug("browser_fetch: %s 活 DOM 里没找到 PDF 链接", landing_url)
                return None

            # 相对链接补全（urljoin 同时覆盖 "/x" 与 "x" 两种相对形态）
            pdf_url = urljoin(landing_url, pdf_url)

            logger.debug("browser_fetch: 活 DOM 读到 PDF 链接 %s", pdf_url)
            # 会话内拉 PDF：pdf_url 来自活 DOM（页面内容完全可控），不能交给
            # Playwright 自动跟重定向——走 _browser_context_get 的逐跳 SSRF 校验
            # + max_redirects=0（javascript:/内网地址在第一跳即被拒）。
            body = await _browser_context_get(
                session.context, pdf_url, timeout_ms=_REQ_TIMEOUT_MS
            )
            if _looks_like_pdf(body):
                logger.info("browser_fetch: 成功拿到 PDF（%d 字节）%s", len(body or b""), pdf_url)
                return body
            return None
    except Exception as exc:
        # REASON: 浏览器跨进程，任何启动/超时异常都降级 None。
        logger.warning("browser_fetch: 浏览器兜底异常 landing=%s（%s）", landing_url, exc)
        return None
