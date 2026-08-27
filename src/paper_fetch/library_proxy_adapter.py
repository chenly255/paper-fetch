"""library_proxy_adapter：经学校图书馆代理（如复旦 libproxy Squid）下载付费论文全文。

机制：学校图书馆开放一台带认证的 HTTP 正向代理（复旦 = libproxy.fudan.edu.cn:8080，
Basic 认证）。出口 IP 是学校订阅 IP，出版商据此放行付费全文。相比浏览器 CARSI/WebVPN：
纯 HTTP、不碰验证码、不逐库导航，校外任意机器都能用。

★ 混合提速策略（2026-07-03 真跑定）：复旦这台代理被学校**刻意限速**（~5-10KB/s，大 PDF 要几分钟）。
但实测出版商的机构访问 cookie **不绑 IP**——所以：
  ① 只用（慢的）代理抓 landing page（HTML 小，~10-30s）→ 拿到机构 cookie + citation_pdf_url；
  ② 带着 cookie **本机直连（不走代理）全速下 PDF**（实测 Nature 2.4MB/s，比走代理快 ~300 倍）；
  ③ 万一某出版商 cookie 绑 IP、直连拿不到 → 回代理下 PDF 兜底（慢但正确）。

⚠ 合规 + 限速：本通道只在用户主动「获取原文」按需触发，且经 institution_credential_service
五道闸限速（调用方 carsi_channel 记账），绝不批量爬。

网络：代理 URL 显式传入；两个 httpx client 都 trust_env=False（代理路只用这台学校代理、
直连路彻底不走任何代理），共享同一 cookie jar。
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .meta_adapter import _PAYWALL_MARKERS, _extract_pdf_url
from .robust_fetch import _looks_like_pdf

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# landing 经（慢）代理抓，HTML 小，给 90s 足够（含机构 IdP 重定向握手）。
_LANDING_TIMEOUT_SEC = 90
# PDF 下载：httpx timeout 是「两次读间隔」上限。直连很快；代理兜底慢但稳定流，180s 够。
_PDF_TIMEOUT_SEC = 180
# 整体墙钟硬上限：正常路（代理抓 landing + 直连下 PDF）~15-40s。代理兜底大 PDF 实测 ~17KB/s、
# 22.8MB 要 20 分钟（2026-08-21 事故：HTTP/2 流中途崩断 + 无断点续传导致整段放弃）——续传循环
# 把上限放宽到 30 分钟。下载链是后台任务（fetch API 同步等待超时后任务继续跑），不影响调用方。
_TOTAL_CAP_SEC = 1800
# 代理路 PDF 断点续传：最多重连轮数 / 连续停滞（一轮没新增字节）多少轮放弃。
_RESUME_MAX_ROUNDS = 10
_RESUME_MAX_STALLS = 3
# 粗上限防异常大响应（真正 size 校验在上层 _validate_and_return）。
_MAX_BYTES = 120 * 1024 * 1024
_MAX_META_REFRESHES = 3

# ---- 机构登录 302 循环治理（2026-08-23 nature.com 事故）--------------------
# 事故：复旦 WAYF 账号对 nature.com 下载陷入 authorize→transit→cookies_not_supported
# 的 302 循环，傻跑 30 分钟到整体超时（_TOTAL_CAP_SEC），账号还被记成「连续失败 3 次」
# 进 30 分钟冷却。治理：手动逐跳跟随重定向并检测同一 host+path 反复出现——
# 达到阈值立即放弃，reason=institutional_flow_loop（不是账号问题，不计失败）。
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_REDIRECT_HOPS = 15  # 机构 SSO 重定向链本来就长（WAYF→IdP→back），比 httpx 默认 10 略宽
_LOOP_SAME_TARGET = 3  # 同一 host+path（不含 query，authorize 的 state 每轮不同）出现次数


class InstitutionalFlowLoop(Exception):
    """机构登录重定向循环：同一 authorize/transit 目标反复出现，快速放弃。"""

    def __init__(self, url: str, hits: int = 0):
        super().__init__(f"redirect loop at {url} ({hits} hits)")
        self.url = url
        self.hits = hits


def _loop_key(url: str) -> str:
    """循环检测键：host+path，不含 query（IdP 流程的 state/nonce 每轮都变）。"""
    p = urlparse(url)
    netloc = (p.netloc or "").lower()
    return f"{netloc}{p.path or '/'}"


async def _get_with_loop_guard(
    client: httpx.AsyncClient, url: str, headers: dict
) -> httpx.Response:
    """手动逐跳跟随重定向（替代 follow_redirects=True）+ 循环检测。

    - 每跳都过循环检测：同一 host+path 出现 ≥ _LOOP_SAME_TARGET 次抛 InstitutionalFlowLoop
      （cookie 种不上时 nature.com authorize?response_type=cookie 会把请求送回起点打转）；
    - cookie jar 由 client 自持（构造参数 cookies= 共享 jar），逐跳 Set-Cookie 自动收发；
    - 超过 _MAX_REDIRECT_HOPS 跳也按循环处理（链再长也不该超过这个数）。
    """
    current = url
    seen: dict[str, int] = {}
    for _ in range(_MAX_REDIRECT_HOPS):
        key = _loop_key(current)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] >= _LOOP_SAME_TARGET:
            raise InstitutionalFlowLoop(current, seen[key])
        resp = await client.get(current, headers=headers)
        if resp.status_code in _REDIRECT_STATUSES:
            location = resp.headers.get("location")
            if not location:
                return resp  # 3xx 无 location：按最终响应交上层处理
            current = urljoin(current, location)
            continue
        return resp
    raise InstitutionalFlowLoop(current, _MAX_REDIRECT_HOPS)


def _build_proxy_url(proxy_host_port: str, username: str, password: str) -> str:
    """拼带认证的代理 URL：http://user:pass@host:port（账密 URL 编码，容忍特殊字符如 !@:）。"""
    return f"http://{quote(username, safe='')}:{quote(password, safe='')}@{proxy_host_port}"


import re as _re

# 匹配 URL 里的 user:pass@ 凭证段（含 URL 编码的），脱敏成 ***@ 防账密落日志/泄前端。
_CRED_RE = _re.compile(r"://[^/@:]+:[^/@]+@")


def _redact_creds(text: str) -> str:
    """脱敏异常/日志文本里的代理凭证（user:pass@ → ***@），其余原样。"""
    if not text:
        return text
    return _CRED_RE.sub("://***@", str(text))


def _extract_meta_refresh_url(soup: BeautifulSoup, base_url: str) -> str | None:
    """解析 LinkingHub 等中转页的 HTML 元刷新，只接受 HTTP(S) 目标。"""
    for meta in soup.find_all("meta"):
        if str(meta.get("http-equiv") or "").strip().lower() != "refresh":
            continue
        content = str(meta.get("content") or "")
        match = _re.search(r"(?:^|;)\s*url\s*=\s*(.+?)\s*$", content, _re.IGNORECASE)
        if not match:
            continue
        raw = match.group(1).strip().strip("'\"")
        target = urljoin(base_url, raw)
        if urlparse(target).scheme.lower() in {"http", "https"}:
            return target
    return None


async def fetch_via_library_proxy(
    *,
    doi: str | None,
    landing_url: str | None,
    username: str,
    password: str,
    proxy_host_port: str,
) -> tuple[bytes | None, str | None]:
    """经学校图书馆代理（混合提速）下载付费论文 PDF。

    返回 (pdf_bytes, None) 成功；(None, reason) 失败。reason 是给调用方做失败分类的
    机器可读原因（2026-08-21 事故整改：机构通道「传输中断」曾被笼统报成付费墙 auth_required，
    用户和运维都被误导——现在付费墙判定与通道尝试结果分开）。reason 取值见 _fetch 内各分支。
    不抛异常，交上层降级。
    """
    target = landing_url or (f"https://doi.org/{doi}" if doi else None)
    if not target:
        return None, "no_target"
    proxy_url = _build_proxy_url(proxy_host_port, username, password)
    try:
        pdf, reason = await asyncio.wait_for(_fetch(target, proxy_url), timeout=_TOTAL_CAP_SEC)
        return pdf, reason
    except TimeoutError:
        logger.warning("library_proxy: 整体超时（超 %ds）target=%s", _TOTAL_CAP_SEC, target)
        return None, "timeout"
    except Exception as exc:
        # REASON: 机构代理通道是可选增强，任何异常都降级让下载链走后续兜底。
        logger.warning("library_proxy: 异常 target=%s（%s）", target, _redact_creds(str(exc)))
        return None, "exception"


async def _fetch(target: str, proxy_url: str) -> tuple[bytes | None, str | None]:
    # 共享 cookie jar：代理路抓 landing 时种下的机构 cookie，直连路下 PDF 时带上。
    # cookie jar 核查（2026-08-23 nature.com cookies_not_supported 事故）：本通道全程
    # httpx（CARSI/WebVPN 浏览器路已砍），jar 以构造参数传给 proxy_client、client 每次
    # 重定向跳转的 Set-Cookie 都自动收入并在下一跳回带；landing 结束后把
    # proxy_client.cookies 复制给直连/续传路。经代理与直连两路共享同一份 cookie。
    cookies = httpx.Cookies()

    # ① 经代理抓 landing page（拿机构 cookie + citation_pdf_url）
    #    follow_redirects=False + 手动逐跳：既检测登录 302 循环（事故 c），也保证
    #    每跳的 Set-Cookie 都经共享 jar 种下（authorize?response_type=cookie 流程依赖）。
    async with httpx.AsyncClient(
        proxy=proxy_url,
        trust_env=False,
        follow_redirects=False,
        timeout=_LANDING_TIMEOUT_SEC,
        headers={"User-Agent": _USER_AGENT},
        cookies=cookies,
    ) as proxy_client:
        request_headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        try:
            resp = await _get_with_loop_guard(proxy_client, target, request_headers)
        except InstitutionalFlowLoop as exc:
            logger.warning(
                "library_proxy: 机构登录重定向循环（%s，%d 次命中），快速放弃 target=%s",
                exc.url,
                exc.hits,
                target,
            )
            return None, "institutional_flow_loop"
        except Exception as exc:
            logger.debug("library_proxy: 抓 landing 失败 %s（%s）", target, _redact_creds(str(exc)))
            return None, "landing_failed"

        for refresh_count in range(_MAX_META_REFRESHES + 1):
            body = resp.content
            # landing 本身就是 PDF（少数直链）——直接经代理这份返回
            if _looks_like_pdf(body):
                logger.info("library_proxy: landing 即 PDF（%d 字节）%s", len(body), target)
                return _sized(body)

            ctype = resp.headers.get("content-type", "").lower()
            if "html" not in ctype and "xml" not in ctype:
                logger.debug(
                    "library_proxy: landing 非 HTML（content-type=%s）放弃 %s", ctype, target
                )
                return None, "landing_not_html"

            html = resp.text
            soup = BeautifulSoup(html, "html.parser")
            final_url = str(resp.url)
            refresh_url = _extract_meta_refresh_url(soup, final_url)
            if refresh_url and refresh_count < _MAX_META_REFRESHES:
                logger.debug("library_proxy: 跟随 HTML 元刷新 %s", refresh_url)
                try:
                    resp = await proxy_client.get(refresh_url, headers=request_headers)
                except Exception as exc:
                    logger.debug(
                        "library_proxy: 元刷新目标读取失败 %s（%s）",
                        refresh_url,
                        _redact_creds(str(exc)),
                    )
                    return None, "landing_failed"
                continue

            if any(m in html.lower() for m in _PAYWALL_MARKERS):
                logger.info("library_proxy: 经代理仍命中付费墙签名（该刊可能未订阅）%s", target)
                return None, "paywall_no_subscription"

            pdf_url = _extract_pdf_url(soup, final_url)
            if not pdf_url:
                logger.debug("library_proxy: landing 未找到 citation_pdf_url %s", target)
                return None, "pdf_url_not_found"
            break
        # proxy_client.cookies 已含机构 cookie，复制给直连路用
        cookies = proxy_client.cookies

    # ② 本机直连（不走代理）+ 机构 cookie 全速下 PDF —— 主路，大 PDF 也秒下。
    #    直连快、失败少见，保持单次 GET（KISS：续传加固只加在下面的代理兜底路上）。
    pdf = await _download_pdf_once(pdf_url, referer=final_url, cookies=cookies, proxy=None)
    if pdf is not None:
        logger.info("library_proxy: 直连+机构cookie 全速拿到 PDF（%d 字节）%s", len(pdf), pdf_url)
        return _sized(pdf)

    # ③ 回代理下 PDF 兜底（该出版商 cookie 绑 IP 时；慢但正确）——Range 断点续传加固：
    #    该路实测 HTTP/2 流会中途崩断（curl 报 stream INTERNAL_ERROR）、~17KB/s 慢速，
    #    单次 GET 一断就整段放弃曾导致 22.8MB 全文拿不到（2026-08-21 事故）。
    #    实测同代理 `curl --http1.1 -C -` 续传 5 轮能拉完整，故这里同样 HTTP/1.1 + Range 循环。
    logger.info("library_proxy: 直连拿不到 PDF，回代理兜底（慢，带断点续传）%s", pdf_url)
    pdf, reason = await _download_pdf_resumable(
        pdf_url, referer=final_url, cookies=cookies, proxy_url=proxy_url
    )
    if pdf is not None:
        logger.info("library_proxy: 经代理续传拿到完整 PDF（%d 字节）%s", len(pdf), pdf_url)
        return _sized(pdf)
    logger.debug("library_proxy: 直连与代理都没拿到 PDF %s（reason=%s）", pdf_url, reason)
    return None, f"pdf_download_failed:{reason}"


def _pdf_complete(content: bytes | None) -> bool:
    """PDF 完整性校验：%PDF- 头 + %%EOF 尾（在文件末尾 4KB 内，容忍增量更新尾随垃圾）。

    断点续传必须校验尾巴——流中途崩断时头部早到了，光看 %PDF- 会把半截文件当完整交付。
    """
    if not content or not _looks_like_pdf(content):
        return False
    return b"%%EOF" in content[-4096:]


def _sized(pdf: bytes) -> tuple[bytes | None, str | None]:
    """粗上限守门后的统一返回：超限返回 (None, oversize)，否则 (pdf, None)。"""
    if _guard_size(pdf) is None:
        return None, "oversize"
    return pdf, None


async def _download_pdf_once(
    url: str, *, referer: str, cookies: httpx.Cookies, proxy: str | None
) -> bytes | None:
    """下 PDF（单次 GET）；proxy=None 走本机直连（快），proxy=<url> 走学校代理。带机构 cookie。"""
    try:
        async with httpx.AsyncClient(
            proxy=proxy,
            trust_env=False,
            follow_redirects=True,
            timeout=_PDF_TIMEOUT_SEC,
            headers={"User-Agent": _USER_AGENT},
            cookies=cookies,
        ) as client:
            resp = await client.get(
                url, headers={"Accept": "application/pdf,*/*", "Referer": referer}
            )
            if _looks_like_pdf(resp.content):
                return resp.content
    except Exception as exc:
        logger.debug(
            "library_proxy: 下 PDF 失败（proxy=%s）%s（%s）",
            bool(proxy),
            url,
            _redact_creds(str(exc)),
        )
    return None


async def _download_pdf_resumable(
    url: str, *, referer: str, cookies: httpx.Cookies, proxy_url: str
) -> tuple[bytes | None, str | None]:
    """代理兜底路的加固下载：HTTP/1.1 优先 + Range 断点续传循环 + 完整性校验。

    为什么（2026-08-21 Nature 22.8MB 事故）：复旦代理路上 HTTP/2 流会中途崩断
    （curl 报 stream INTERNAL_ERROR），旧实现单次 GET 一断就整段放弃；实测同代理
    `curl --http1.1 -C -` 续传 5 轮能拉完整。httpx 默认就是 HTTP/1.1（不设 http2=True
    即不会协商 h2），这里显式注释锁定该行为，防止未来有人全局开 h2 把坑引回来。

    循环语义：
    - 每轮带 `Range: bytes=<已收字节>-` 请求；服务器返 206 续传、返 200 说明不支持
      Range（清空重来）、返 416 说明文件已变（清空重下）。
    - 流中断（ReadTimeout/RemoteProtocolError 等）不丢已收字节，下一轮续传。
    - 完整判定：优先 Content-Length（累计相等 + %PDF 头），否则 %%EOF 尾校验。
    - 放弃条件：总轮数 > _RESUME_MAX_ROUNDS、连续 _RESUME_MAX_STALLS 轮无新增字节
      （停滞）。墙钟上限不在此循环内逐轮检查——由外层 fetch_via_library_proxy 的
      asyncio.wait_for(_TOTAL_CAP_SEC) 统一兜底（超时取消整个协程，已收字节随之放弃）。
    """
    buf = bytearray()
    stalls = 0
    reason = "incomplete"
    try:
        # 不设 http2=True：显式保持 HTTP/1.1（见 docstring）。
        async with httpx.AsyncClient(
            proxy=proxy_url,
            trust_env=False,
            follow_redirects=True,
            timeout=_PDF_TIMEOUT_SEC,
            headers={"User-Agent": _USER_AGENT},
            cookies=cookies,
        ) as client:
            for round_no in range(1, _RESUME_MAX_ROUNDS + 1):
                if len(buf) > _MAX_BYTES:
                    return None, "oversize"
                headers = {"Accept": "application/pdf,*/*", "Referer": referer}
                if buf:
                    headers["Range"] = f"bytes={len(buf)}-"
                before = len(buf)
                expected_total: int | None = None
                broken = False
                try:
                    async with client.stream("GET", url, headers=headers) as resp:
                        if resp.status_code == 416:
                            # Range 不可满足（文件已变/偏移越界）：**不读 body**——416 的 body
                            # 是 HTML 错误页，读进缓冲区会污染已收字节、后续所有轮的 Range
                            # 偏移全错（评审 M2）。丢弃已有字节，直接下一轮全量重下。
                            buf.clear()
                            broken = True
                        elif resp.status_code == 200:
                            # 服务器不支持 Range：丢弃已有字节从头下
                            buf.clear()
                        elif resp.status_code != 206:
                            return None, f"http_{resp.status_code}"
                        if not broken:
                            cl = resp.headers.get("content-range") or resp.headers.get(
                                "content-length"
                            )
                            if cl and "/" in cl:
                                try:
                                    expected_total = int(cl.rsplit("/", 1)[1])
                                except ValueError:
                                    expected_total = None
                            elif cl:
                                try:
                                    expected_total = int(cl) + (
                                        0 if resp.status_code == 200 else len(buf)
                                    )
                                except ValueError:
                                    expected_total = None
                            async for chunk in resp.aiter_bytes():
                                buf.extend(chunk)
                except Exception as exc:
                    # 断流（HTTP/2 INTERNAL_ERROR / 读超时 / 连接重置）：保留已收字节续传。
                    # 不 continue——要走下面的停滞检测（断流且零进展也是停滞）。
                    broken = True
                    logger.debug(
                        "library_proxy: 续传第 %d 轮断流（已收 %d 字节）%s（%s）",
                        round_no,
                        len(buf),
                        url,
                        _redact_creds(str(exc)),
                    )

                if not broken:
                    got = bytes(buf)
                    if _looks_like_pdf(got) and (
                        (expected_total is not None and len(got) == expected_total)
                        or _pdf_complete(got)
                    ):
                        logger.debug(
                            "library_proxy: 续传完成（%d 轮，%d 字节）%s",
                            round_no,
                            len(got),
                            url,
                        )
                        return got, None

                # 停滞检测（断流轮与未完整轮共用）：本轮没有任何新增字节 → 大概率死路，
                # 攒够轮数就放弃；有进展则重置计数。
                if len(buf) <= before:
                    stalls += 1
                    if stalls >= _RESUME_MAX_STALLS:
                        return None, "stalled"
                else:
                    stalls = 0
                logger.debug(
                    "library_proxy: 续传第 %d 轮未完整（已收 %d 字节%s），继续",
                    round_no,
                    len(buf),
                    f"/{expected_total}" if expected_total else "",
                )
    except Exception as exc:
        logger.warning("library_proxy: 续传循环异常 %s（%s）", url, _redact_creds(str(exc)))
        return None, "exception"
    return None, reason


def _guard_size(pdf: bytes) -> bytes | None:
    if len(pdf) > _MAX_BYTES:
        logger.warning("library_proxy: PDF 超粗上限（%d 字节）丢弃", len(pdf))
        return None
    return pdf


async def test_proxy_auth(proxy_host_port: str, username: str, password: str) -> tuple[bool, str]:
    """连通测试：经图书馆代理发一个轻量请求，只验「账号能否过代理认证」，不下任何全文。

    返回 (ok, 中文说明)。ok=True 表示代理接受了账号（407=账密不对）。快、无副作用、不记账。
    """
    proxy_url = _build_proxy_url(proxy_host_port, username, password)
    try:
        async with httpx.AsyncClient(
            proxy=proxy_url,
            trust_env=False,
            follow_redirects=False,
            timeout=30,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            # HEAD 一个订阅域名：CONNECT 隧道建成即说明账密通过；407 会被 httpx 抛成 ProxyError。
            await client.head("https://www.nature.com/")
        return True, "连通正常，学校账号可用"
    except httpx.ProxyError as exc:
        # ProxyError 异常消息可能内嵌完整 proxy_url（http://user:pass@host:port）——绝不透传给前端。
        if "407" in str(exc):
            return False, "学校账号或密码不对（代理拒绝认证）"
        logger.debug("test_proxy_auth: 代理连接失败（%s）", _redact_creds(str(exc)))
        return False, "代理连接失败（非账密问题，可能是代理地址不对或网络不通）"
    except Exception as exc:  # noqa: BLE001
        # REASON: 连通测试仅供前端提示，任何异常都归为「连不上」，不抛给调用方；
        # 异常消息可能含 proxy_url 凭证，只回固定文案 + debug 记脱敏后的原因。
        logger.debug("test_proxy_auth: 连不上图书馆代理（%s）", _redact_creds(str(exc)))
        return False, "连不上图书馆代理，请检查代理地址与网络"
