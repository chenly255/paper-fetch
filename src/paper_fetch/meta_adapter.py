"""meta_adapter：抓论文 landing page HTML，从 meta 标签里读 PDF URL + DOI。

为什么这一层 ROI 最高：几乎所有正规学术出版商（Elsevier、Nature、Springer、Wiley、
ACS、Frontiers 等）都按 Google Scholar 规范在 landing page <head> 里放：
  <meta name="citation_pdf_url" content="...">
  <meta name="citation_doi"     content="10.xxxx/yyyy">

副产物 DOI：搜索源（PubMed/SS/Tavily）没拿到 DOI 时，这一层能补给上——后续
Unpaywall / Europe PMC 都需要 DOI。

付费墙短路：HTML 含「access denied」「subscription required」「sign in to access」
等签名时直接 return (None, None)，不浪费时间下假 PDF。

W8 网络军规：landing page fetch 走 17891 代理；PDF 下载复用 oa_adapter（同样走代理）。
"""
from __future__ import annotations

import logging
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .proxy import async_client_for, proxy_for_url

from .domain_cooldown import observe_http_status, should_skip_url
from .oa_adapter import fetch_oa_pdf
from .robust_fetch import _REDIRECT_STATUSES, _ensure_public_async, _looks_like_pdf, is_free_site
from .url_safety import MAX_SAFE_REDIRECTS, pin_url_host

logger = logging.getLogger(__name__)

# 浏览器 UA，避免被出版商当爬虫拒
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_TIMEOUT_SEC = 15

_REQ_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# HTML 里的付费墙签名（小写匹配；命中其一直接返 None 不浪费下游）
_PAYWALL_MARKERS = (
    "access denied",
    "subscription required",
    "purchase article",
    "sign in to access",
    "institutional login",
)


async def fetch_via_landing_page(
    paper_url: str | None,
) -> tuple[bytes | None, str | None, dict]:
    """抓 paper_url 的 HTML 找 PDF URL 下载；同时尽量抽出 DOI + 付费墙信号给下游/前端用。

    参数：
        paper_url — 论文页 URL（PubMed/SS/Tavily 的 source_url 或用户原文）

    返回三元组 (pdf_bytes, doi, landing_info)：
        pdf_bytes   — PDF 字节，抓不到为 None
        doi         — 从 HTML 抽到的 DOI（给下游 Unpaywall/Europe PMC 用），可能 None
        landing_info — {"url": 落地页 URL, "publisher": 出版商名|None, "requires_auth": bool}
                       requires_auth=True 表示检到付费墙（需机构登录）——块B 据此走机构通道 + 前端兜底。
    """
    landing_info: dict = {"url": paper_url, "publisher": None, "requires_auth": False}
    if not paper_url:
        return None, None, landing_info
    if should_skip_url(paper_url):
        return None, None, landing_info

    html, pdf_direct, status_code, final_url = await _fetch_html(paper_url)
    # paper_url 本身就是 PDF（content-type=application/pdf 或 magic bytes）——搜索源把直链
    # 当 source_url 给的情况，别再当 HTML 解析丢弃，直接返回这份已下到的字节（双保险）。
    if pdf_direct is not None:
        logger.info("meta_adapter: %s 本身即 PDF 直链，直接返回（%d 字节）", paper_url, len(pdf_direct))
        return pdf_direct, None, landing_info
    if html is None:
        # 401/402/403 是明确的付费墙信号，保留 landing_url 给机构登录通道 + 前端兜底。
        # ★免费站豁免：用最终跳转后的网址判（doi.org 会 302 到 biorxiv 才返 403，光看入参
        # doi.org 认不出免费站）。biorxiv/medrxiv 的 403 是 Cloudflare 反爬不是付费墙。
        check_url = final_url or paper_url
        if status_code in (401, 402, 403) and not is_free_site(check_url):
            landing_info["requires_auth"] = True
        return None, None, landing_info

    soup = BeautifulSoup(html, "html.parser")
    landing_info["publisher"] = _extract_publisher(soup, paper_url)

    # 先查付费墙签名——命中就早返，省下游一次失败下载，但保留 landing_url 给机构通道
    lowered = html.lower()
    if any(marker in lowered for marker in _PAYWALL_MARKERS):
        doi = _extract_doi(soup)
        landing_info["requires_auth"] = True
        logger.debug("meta_adapter: 付费墙签名命中（url=%s），仅返 DOI=%s", paper_url, doi)
        return None, doi, landing_info

    doi = _extract_doi(soup)
    pdf_url = _extract_pdf_url(soup, paper_url)

    if not pdf_url:
        logger.debug("meta_adapter: %s 未找到 PDF URL（DOI=%s）", paper_url, doi)
        return None, doi, landing_info

    logger.debug("meta_adapter: %s 找到 PDF URL %s（DOI=%s）", paper_url, pdf_url, doi)
    pdf_bytes = await fetch_oa_pdf(pdf_url)
    return pdf_bytes, doi, landing_info


async def _fetch_html(url: str) -> tuple[str | None, bytes | None, int | None, str | None]:
    """GET landing page；返回 (html, pdf_bytes, status_code, final_url)。

    final_url 是跳转后的最终网址（doi.org 会 302 到真出版商/预印本站）——给上层判免费站用
    （光看入参 doi.org 认不出 biorxiv，看 final_url 才认得出）。

    SSRF 校验（与 robust_fetch._httpx_get 同款军规，R2-1）：paper_url 是用户可控输入，
    follow_redirects=False + 手动逐跳跟随，每跳过 url_safety 公开地址校验；直连模式把
    已校验 IP 固化进连接防 DNS rebinding。内网/本机/云元数据地址一律拒绝，按本段
    失败降级（上层换下一来源），不再让服务端裸连任意地址。

    - URL 本身就是 PDF（content-type 含 pdf 或响应体 magic bytes 命中）→ (None, pdf_bytes, status, final_url)
    - 正常 HTML/XML 落地页 → (html, None, status, final_url)
    - 其余失败 → (None, None, status 或 None, final_url 或 None)
    """
    current = url
    try:
        for _ in range(MAX_SAFE_REDIRECTS + 1):
            ip = await _ensure_public_async(current)
            if ip is None:
                logger.warning("meta_adapter: 拒绝非公开地址 url=%s", current)
                return None, None, None, None
            # 冷却器在 SSRF 校验通过之后：校验失败的内网地址不进冷却表。
            if should_skip_url(current):
                return None, None, None, None
            async with async_client_for(
                current, follow_redirects=False, timeout=_TIMEOUT_SEC
            ) as client:
                if proxy_for_url(current):
                    resp = await client.get(current, headers=_REQ_HEADERS)
                else:
                    connect_url, host, is_https = pin_url_host(current, ip)
                    resp = await client.get(
                        connect_url,
                        headers={**_REQ_HEADERS, "Host": host},
                        extensions={"sni_hostname": host} if is_https else None,
                    )
            if resp.status_code in _REDIRECT_STATUSES:
                location = resp.headers.get("location")
                if not location:
                    return None, None, resp.status_code, current
                # 重定向目标基于「逻辑 URL」解析（同 robust_fetch._httpx_get）
                current = urljoin(current, location)
                continue
            final_url = current

            # 401/402/403 → 付费墙重定向，没救（但把 status + final_url 带回去当付费墙信号）
            if resp.status_code in (401, 402, 403):
                logger.debug("meta_adapter: %s 返 HTTP %d（付费墙）", final_url, resp.status_code)
                observe_http_status(final_url, resp.status_code, resp.headers)
                return None, None, resp.status_code, final_url
            if observe_http_status(final_url, resp.status_code, resp.headers):
                return None, None, resp.status_code, final_url

            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "").lower()
            # 落地页本身就是 PDF：content-type 标 pdf，或正文 magic bytes 是 %PDF-（不信任 CT）
            if "pdf" in content_type or _looks_like_pdf(resp.content):
                if _looks_like_pdf(resp.content):
                    return None, resp.content, resp.status_code, final_url
                # 声称 pdf 但正文非 PDF（挑战页伪装）→ 当失败处理
                logger.debug("meta_adapter: %s 声称 PDF 但正文非 %%PDF，丢弃", final_url)
                return None, None, resp.status_code, final_url
            if "html" not in content_type and "xml" not in content_type:
                logger.debug("meta_adapter: %s 返非 HTML（content-type=%s）", final_url, content_type)
                return None, None, resp.status_code, final_url

            return resp.text, None, resp.status_code, final_url
        return None, None, None, None
    except httpx.HTTPStatusError as exc:
        logger.debug("meta_adapter: %s HTTP 错误 %s", url, exc.response.status_code)
        return None, None, exc.response.status_code, None
    except httpx.TimeoutException:
        logger.debug("meta_adapter: %s 超时", url)
        return None, None, None, None
    except httpx.RequestError as exc:
        logger.debug("meta_adapter: %s 请求错误（%s）", url, exc)
        return None, None, None, None
    except Exception as exc:
        # REASON: meta 抓 landing page 是五段下载链中的一段，任何未预期异常（SSL 协商
        # 出错 / DNS 失败 / 解析框架抛奇怪异常）都要降级 None 让上层走下一档。
        logger.warning("meta_adapter: %s 未知错误（%s）", url, exc, exc_info=True)
        return None, None, None, None


def _extract_publisher(soup: BeautifulSoup, base_url: str) -> str | None:
    """抽出版商名：citation_publisher > og:site_name > dc.publisher > 域名兜底。"""
    for _name, attrs in (
        ("citation_publisher", {"name": "citation_publisher"}),
        ("og:site_name", {"property": "og:site_name"}),
        ("dc.publisher", {"name": "dc.publisher"}),
    ):
        tag = soup.find("meta", attrs=attrs)
        content = (tag.get("content") if tag else "") or ""
        if content.strip():
            return content.strip()[:120]
    # 域名兜底
    try:
        host = urlparse(base_url).hostname or ""
        host = host.removeprefix("www.")
        return host or None
    except Exception:
        return None


def _extract_pdf_url(soup: BeautifulSoup, base_url: str) -> str | None:
    """优先级：citation_pdf_url > link rel=alternate type=application/pdf > og:url(.pdf)。"""
    # 1. citation_pdf_url（Google Scholar 标准，覆盖率最高）
    tag = soup.find("meta", attrs={"name": "citation_pdf_url"})
    content = (tag.get("content") if tag else "") or ""
    if content.strip():
        return urljoin(base_url, content.strip())

    # 2. <link rel="alternate" type="application/pdf" href="...">
    tag = soup.find("link", attrs={"rel": "alternate", "type": "application/pdf"})
    href = (tag.get("href") if tag else "") or ""
    if href.strip():
        return urljoin(base_url, href.strip())

    # 3. og:url 以 .pdf 结尾的情况
    tag = soup.find("meta", attrs={"property": "og:url"})
    content = (tag.get("content") if tag else "") or ""
    if content.strip().lower().endswith(".pdf"):
        return urljoin(base_url, content.strip())

    return None


def _extract_doi(soup: BeautifulSoup) -> str | None:
    """优先级：citation_doi > dc.identifier scheme=doi > 任意 dc.identifier 以 10. 开头。"""
    tag = soup.find("meta", attrs={"name": "citation_doi"})
    content = (tag.get("content") if tag else "") or ""
    if content.strip():
        return _clean_doi(content.strip())

    # dc.identifier 带 scheme=doi
    tag = soup.find("meta", attrs={"name": "dc.identifier", "scheme": "doi"})
    content = (tag.get("content") if tag else "") or ""
    if content.strip():
        return _clean_doi(content.strip())

    # 通用 dc.identifier，挑第一个像 DOI 的
    for tag in soup.find_all("meta", attrs={"name": "dc.identifier"}):
        c = (tag.get("content") or "").strip()
        if c.startswith("10."):
            return _clean_doi(c)

    return None


def _clean_doi(doi: str) -> str:
    """去掉 doi: / https://doi.org/ 等前缀，返裸 DOI。"""
    s = doi.strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if s.lower().startswith(prefix):
            return s[len(prefix):]
    return s
