"""robust_fetch：稳健 PDF 抓取原语。三级降级 httpx → curl_cffi → 无头浏览器。

为什么要三级（实测于 Nature/Springer 等出版社）：
  ① httpx           —— 最快，对干净直链够用。
  ② curl_cffi       —— impersonate 模拟真实 Chrome 的 TLS/HTTP2 指纹，过掉一批
                       「非浏览器指纹就拒」的站点（部分 Cloudflare/Akamai）。
  ③ 无头浏览器       —— 跑真实 JS，过 Springer Nature「Client Challenge」/ Cloudflare
                       「Just a moment」这类 JS 挑战页。这是 httpx/curl_cffi 都过不了、
                       而 OA 论文（如 Nature Communications）PDF 必须过的一关。
                       浏览器开销大（每次约 6~8 秒），由 FetchBudget 限到每次下载最多 1 次。

为什么这是合规的：跑标准无头浏览器执行页面 JS 不是破验证码（红线），只是让 OA 论文的
PDF 端点正常放行——feishu_agent / scansci-pdf 同样这么做。撞到图形验证码会自然失败返 None。

网络军规（2026-08-22 更新）：三级出口（httpx / curl_cffi / 无头浏览器）按 URL 走
proxy_pool_service 的统一判定——境外源走内嵌 mihomo（产品自管，不依赖机器环境变量
或机器上装的代理），本机/国内直连；一切 client 显式禁用环境代理（httpx trust_env=False /
curl CURLOPT_PROXY=""）。同文件旧军规「继承后端进程环境」作废。

SSRF 防护（R2-1）：所有网络出口（httpx / curl_cffi / 无头浏览器）请求前统一走
url_safety 校验（scheme/端口/内网 IP），重定向逐跳校验。用户可控的 paper_url /
oa_url 直连内网（如 127.0.0.1 / 169.254.169.254）一律拒绝——校验失败按「该来源
下载失败」降级返 None，不影响下载链继续试下一来源；出版社域名解析为公网 IP 照常通过。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from .url_safety import (
    MAX_SAFE_REDIRECTS,
    UnsafeUrlError,
    UrlResolveError,
    pin_url_host,
    resolve_public_url,
    resolve_public_url_sync,
)

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_TIMEOUT_SEC = 12
_BROWSER_CHALLENGE_POLL_ATTEMPTS = 10
_BROWSER_CHALLENGE_POLL_MS = 2_000

# 免费站（预印本/OA 仓库）域名标记。两类用途共用：
# ① _should_try_institutional：免费站不走机构登录，省学校账号（不浪费在本来就免费的站上）；
# ② is_free_site：免费站的 403/401 是反爬不是付费墙——meta_adapter 抓 landing 撞 Cloudflare
#    返 403 时，不该标 requires_auth 误导前端弹「校园网打开 / 付费墙」兜底（biorxiv 2025-12
#    加 Cloudflare 后的真实场景：免费预印本被反爬挡，跟付费墙无关）。
# 放这个叶子模块是因为 meta_adapter / paper_download_service 都已 import 它，避免循环引用。
# ★与 paper_download_service._PREPRINT_URL_MARKERS 对齐（2026-08-23 审计修复 4）：那边认的
#   预印本站这里必须全收——ResearchSquare 预印本被反爬 403 曾因不在本表被误判成付费墙。
#   一致性由 test_audit_download_chain_fixes 锁死，两张表以后要一起改。
FREE_SITE_MARKERS = (
    "arxiv.org", "biorxiv.org", "medrxiv.org", "ncbi.nlm.nih.gov",
    "europepmc.org", "plos.org", "peerj.com", "doaj.org",
    "researchsquare.com", "preprints.org", "chemrxiv.org", "osf.io", "ssrn.com",
)


def is_free_site(url: str | None) -> bool:
    """URL 是否属免费站（预印本/OA 仓库）。免费站的 403 是反爬不是付费墙。"""
    if not url:
        return False
    low = url.lower()
    return any(m in low for m in FREE_SITE_MARKERS)


@dataclass
class FetchBudget:
    """一次 download_pdf 调用内的「昂贵尝试」预算。

    browser：允许的无头浏览器抓取次数（默认 1）。download_pdf 全程共享一个 budget，
    多段下载链里谁先用上谁消耗，用完后续段只走 httpx + curl_cffi。
    """

    browser: int = 1


def _looks_like_pdf(content: bytes | None) -> bool:
    """PDF magic bytes 校验。

    只认 magic bytes，不信任 Content-Type——挑战页/付费墙常返 200 + text/html 伪装。
    容忍前导 UTF-8 BOM / 空白 / NUL（部分 CDN/代理会在 PDF 前注入这些，合法 PDF 阅读器照样能开），
    剥掉后再比前 5 字节，避免把带 BOM 的合法 PDF 误判为非 PDF。
    """
    if not content:
        return False
    head = content[:1024].lstrip(b"\xef\xbb\xbf \t\r\n\x00")
    return head[:5] == b"%PDF-"


_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def _should_skip_cooled(url: str | None) -> bool:
    """SSRF 通过后查冷却表。延迟 import，避免与 domain_cooldown 循环引用。"""
    from .domain_cooldown import should_skip_url

    return should_skip_url(url)


def _observe_block(url: str | None, status_code: int, headers) -> bool:  # noqa: ANN001
    """记录 429 / 反爬 403。headers 可以是 httpx/curl 的大小写不敏感映射。"""
    from .domain_cooldown import observe_http_status

    hdrs = None
    if headers is not None:
        try:
            hdrs = dict(headers)
        except Exception:  # noqa: BLE001
            hdrs = None
    return observe_http_status(url, status_code, hdrs)


async def _ensure_public_async(url: str) -> str | None:
    """SSRF 校验（异步段）：通过则返回**已验证的公网 IP 字面值**（供固化），失败返 None。"""
    try:
        _, ip = await resolve_public_url(url)
        return ip
    except (UnsafeUrlError, UrlResolveError) as exc:
        logger.warning("robust_fetch: 拒绝非公开地址 url=%s（%s）", url, exc)
        return None


def _ensure_public_sync(url: str) -> str | None:
    """SSRF 校验（同步段，给线程池里跑的 curl_cffi 用）：返回已验证 IP 或 None。"""
    try:
        _, ip = resolve_public_url_sync(url)
        return ip
    except (UnsafeUrlError, UrlResolveError) as exc:
        logger.warning("robust_fetch: 拒绝非公开地址 url=%s（%s）", url, exc)
        return None


async def _httpx_get(url: str, referer: str | None) -> bytes | None:
    from .proxy import async_client_for, proxy_for_url

    headers = {"User-Agent": _USER_AGENT, "Accept": "application/pdf,*/*"}
    if referer:
        headers["Referer"] = referer
    current = url
    try:
        # follow_redirects=False + 手动逐跳跟随：每跳都过 SSRF 校验，
        # 防「公网域 302 → 内网地址」绕过（R2-1）。
        # 每跳按 host 判定直连/代理（async_client_for）：
        # - 直连：DNS rebinding 防护（评审1#1/评审2 F2）——校验返回的公网 IP 直接固化进
        #   本次连接（URL 换 IP 字面值 + Host 头 + sni_hostname 扩展），消除二次解析窗口。
        # - 代理（内嵌 mihomo）：跳过 IP 替换——DNS 必须由代理端解析才能正确按域名分流，
        #   IP 字面值请求会毁掉代理语义。SSRF 防护在代理模式下分两层：本地预解析校验
        #   仍先做（尽力而为，不构成硬约束——代理端 DNS 不受它约束）；内网目标由
        #   mihomo 内核的私网 REJECT 规则兜底拒绝（build_mihomo_config）。
        for _ in range(MAX_SAFE_REDIRECTS + 1):
            ip = await _ensure_public_async(current)
            if ip is None:
                return None
            # 冷却器必须在 SSRF 校验通过之后：校验失败的内网地址不进冷却表。
            if _should_skip_cooled(current):
                return None
            async with async_client_for(
                current, follow_redirects=False, timeout=_TIMEOUT_SEC
            ) as client:
                if proxy_for_url(current):
                    resp = await client.get(current, headers=headers)
                else:
                    connect_url, host, is_https = pin_url_host(current, ip)
                    request_headers = {**headers, "Host": host}
                    extensions = {"sni_hostname": host} if is_https else None
                    resp = await client.get(
                        connect_url, headers=request_headers, extensions=extensions
                    )
            if resp.status_code in _REDIRECT_STATUSES:
                location = resp.headers.get("location")
                if not location:
                    return None
                # 重定向目标基于「逻辑 URL」（原主机名）解析，不是 IP 版连接 URL
                current = urljoin(current, location)
                continue
            if _observe_block(current, resp.status_code, resp.headers):
                return None
            return resp.content
        return None
    except Exception as exc:
        # REASON: 抓取原语第一级，任何 transport 异常都降级到下一级。
        logger.debug("robust_fetch: httpx 失败 url=%s（%s）", url, exc)
        return None


def _curl_cffi_get_sync(url: str, referer: str | None) -> bytes | None:
    from curl_cffi import CurlOpt
    from curl_cffi.requests import Session

    from .proxy import proxy_for_url

    headers = {"Referer": referer} if referer else {}
    current = url
    for _ in range(MAX_SAFE_REDIRECTS + 1):
        ip = _ensure_public_sync(current)
        if ip is None:
            return None
        if _should_skip_cooled(current):
            return None
        proxy = proxy_for_url(current)
        curl_options: dict = {CurlOpt.PROXY: proxy or ""}
        # 代理端点用 "" 显式禁用：libcurl 默认读 http_proxy 等环境变量，必须钉死；
        # 代理来自 proxy_pool_service（产品自管 mihomo），绝不来自环境。
        if not proxy:
            # 直连才做 DNS rebinding 防护：CURLOPT_RESOLVE 把已验证 IP 固化到本次会话
            # （与 web_clip_service._fetch_curl 同款实现）。同时固化 80/443 两个端口，
            # 跳到同域另一端口的重定向也不回退真实 DNS。代理模式下解析在代理端，跳过。
            host = (urlsplit(current).hostname or "").rstrip(".")
            curl_options[CurlOpt.RESOLVE] = [f"{host}:{port}:{ip}" for port in (80, 443)]
        with Session(curl_options=curl_options) as session:
            resp = session.get(
                current, impersonate="chrome120", timeout=_TIMEOUT_SEC,
                allow_redirects=False, headers=headers,
            )
        if resp.status_code in _REDIRECT_STATUSES:
            location = resp.headers.get("location")
            if not location:
                return None
            current = urljoin(current, location)
            continue
        if _observe_block(current, resp.status_code, resp.headers):
            return None
        return resp.content
    return None


async def _curl_cffi_get(url: str, referer: str | None) -> bytes | None:
    try:
        # curl_cffi.requests 是同步库，丢线程池跑避免阻塞事件循环。
        # 拷贝 ContextVar，让线程里的冷却记录能写回本次 download_pdf 的捕获列表。
        import contextvars

        ctx = contextvars.copy_context()
        return await asyncio.to_thread(ctx.run, _curl_cffi_get_sync, url, referer)
    except Exception as exc:
        # REASON: curl_cffi 未装/请求失败都降级。
        logger.debug("robust_fetch: curl_cffi 失败 url=%s（%s）", url, exc)
        return None


def _derive_article_url(pdf_url: str, referer: str | None) -> str:
    """从 PDF URL 推出同域「文章页」URL，供浏览器先访问以过 JS 挑战 + 拿 cookie。

    优先用显式传入的 referer；否则去掉 PDF URL 尾部的 .pdf / /pdf 还原文章页。
    """
    if referer:
        return referer
    u = pdf_url
    if u.lower().endswith(".pdf"):
        return u[:-4]
    if u.lower().endswith("/pdf"):
        return u[:-4]
    return u


async def _browser_exit_guard(route) -> None:  # noqa: ANN001
    """浏览器出口逐跳校验（评审1#2）：导航请求（含 302 重定向跳转）过 url_safety。

    命中内网/本机地址即 abort——初始 URL 公网、服务器 302 到 127.0.0.1 这类
    「浏览器自动跟随重定向绕过出口校验」的路径在这里被拦下。静态子资源
    （图片/JS）不是导航请求，直接放行，避免逐请求 DNS 解析拖慢页面加载。
    """
    request = route.request
    if request.is_navigation_request():
        if await _ensure_public_async(request.url) is None:
            await route.abort()
            return
    await route.continue_()


async def _browser_get(url: str, referer: str | None) -> bytes | None:
    """无头浏览器抓 PDF：先访问文章页过 JS 挑战，再用浏览器会话上下文拉 PDF（带 cookie + 浏览器 TLS）。"""
    try:
        from .browser_session import BrowserSession
    except Exception as exc:
        logger.debug("robust_fetch: 浏览器会话不可用（%s），跳过浏览器抓取", exc)
        return None

    nav = _derive_article_url(url, referer)
    # SSRF 校验（R2-1）：PDF URL 和导航页都要过公开地址校验，内网/本机地址不进浏览器。
    for target in (url, nav):
        if await _ensure_public_async(target) is None:
            return None
        if _should_skip_cooled(target):
            return None
    # 境外源走内嵌 mihomo（proxy_pool_service 统一判定；直连域不传代理）。
    from .proxy import proxy_for_url

    browser_proxy = proxy_for_url(url)
    try:
        async with BrowserSession(proxy_server=browser_proxy) as session:
            page = session.page
            # 出口重定向拦截（评审1#2）：context 级路由逐跳校验导航 URL，
            # 公网页面 302 到内网时第二跳被 abort，浏览器不会真的去连内网。
            await session.context.route("**/*", _browser_exit_guard)
            try:
                await page.goto(nav, wait_until="domcontentloaded")
                await page.wait_for_timeout(3500)  # 给 JS 挑战脚本执行时间
            except Exception as exc:
                logger.debug("robust_fetch: 浏览器导航 %s 失败（%s），仍尝试直接拉 PDF", nav, exc)

            # 用浏览器会话上下文拉 PDF —— cookie + 浏览器 TLS 指纹都带上
            body = await _browser_context_get(session.context, url)
            if _looks_like_pdf(body):
                logger.info("robust_fetch: 浏览器成功拿到 PDF（%d 字节）url=%s", len(body), url)
                return body

            # 退一步：直接 goto PDF 让浏览器跑完任何挑战脚本，再拉一次
            try:
                await page.goto(url, wait_until="domcontentloaded")
            except Exception as exc:
                logger.debug("robust_fetch: 浏览器打开 PDF/挑战页失败（%s），仍继续轮询", exc)

            # NCBI 等站点的工作量证明在 ECS 负载下可能需要十几秒。固定等 2.5 秒
            # 会产生间歇性失败；改为有上限的轮询，一拿到真 PDF 就立即返回。
            for _ in range(_BROWSER_CHALLENGE_POLL_ATTEMPTS):
                await page.wait_for_timeout(_BROWSER_CHALLENGE_POLL_MS)
                body = await _browser_context_get(session.context, url)
                if _looks_like_pdf(body):
                    logger.info("robust_fetch: 浏览器挑战完成后拿到 PDF（%d 字节）", len(body))
                    return body
            return None
    except Exception as exc:
        # REASON: 浏览器跨进程，启动/超时等任何异常都降级 None，不炸下载链。
        logger.warning("robust_fetch: 浏览器抓取异常 url=%s（%s）", url, exc)
        return None


def _response_location(headers) -> str | None:  # noqa: ANN001
    """从 Playwright / 类 dict 响应头取出 Location（大小写不敏感）。"""
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if callable(getter):
        return getter("location") or getter("Location")
    return None


async def _browser_context_get(context, url: str) -> bytes | None:  # noqa: ANN001
    """在已建立的浏览器会话内请求一次 PDF；传输失败交给上层继续轮询。

    Playwright 的 APIRequestContext.get 不走 context.route，默认会自动跟随
    重定向。公网 302 到 127.0.0.1 时第二跳不会被 _browser_exit_guard 拦住。
    这里 max_redirects=0 + 手动逐跳，每跳过 url_safety（与 httpx/curl 同款）。
    """
    current = url
    try:
        for _ in range(MAX_SAFE_REDIRECTS + 1):
            if await _ensure_public_async(current) is None:
                return None
            if _should_skip_cooled(current):
                return None
            resp = await context.request.get(
                current, timeout=_TIMEOUT_SEC * 1000, max_redirects=0,
            )
            status = getattr(resp, "status", None)
            headers = getattr(resp, "headers", None)
            if isinstance(status, int) and status in _REDIRECT_STATUSES:
                location = _response_location(headers)
                if not location:
                    return None
                current = urljoin(current, location)
                continue
            if isinstance(status, int) and _observe_block(current, status, headers):
                return None
            # 兜底：万一实现忽略 max_redirects 自动跟跳，最终落点再过一遍校验。
            final_url = getattr(resp, "url", None)
            if final_url and await _ensure_public_async(str(final_url)) is None:
                return None
            return await resp.body()
        return None
    except Exception as exc:
        logger.debug("robust_fetch: 浏览器 ctx.request 拉 PDF 失败（%s）", exc)
        return None


async def fetch_pdf_simple(url: str | None, *, referer: str | None = None) -> bytes | None:
    """两级抓取（httpx → curl_cffi），不开浏览器。快，给逐个候选 URL 批量试用。"""
    if not url:
        return None
    content = await _httpx_get(url, referer)
    if _looks_like_pdf(content):
        return content
    content = await _curl_cffi_get(url, referer)
    if _looks_like_pdf(content):
        return content
    return None


async def fetch_pdf_via_browser(
    url: str | None, *, referer: str | None = None, budget: FetchBudget | None = None,
) -> bytes | None:
    """仅第三级（无头浏览器）。budget.browser 有余额才跑，跑一次扣一次。"""
    if not url or budget is None or budget.browser <= 0:
        return None
    # 冷却中不扣浏览器预算，留给其他来源；未冷却时行为与改前完全一致。
    if _should_skip_cooled(url):
        return None
    budget.browser -= 1
    return await _browser_get(url, referer)


async def fetch_pdf(
    url: str | None, *, referer: str | None = None, budget: FetchBudget | None = None,
) -> bytes | None:
    """三级全开：httpx → curl_cffi →（budget 允许时）无头浏览器。"""
    pdf = await fetch_pdf_simple(url, referer=referer)
    if pdf is not None:
        return pdf
    return await fetch_pdf_via_browser(url, referer=referer, budget=budget)
