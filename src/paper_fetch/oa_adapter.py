"""oa_adapter：直接拿 OA URL 的 PDF 字节（薄封装，委托给 robust_fetch）。

安全考量（T-08-07）：前 5 字节 %PDF- magic bytes 校验，防止拿到 HTML 伪装成 PDF。
任何失败返 None，不抛异常，让上层降级链继续。

升级（2026-06-12）：底层改走 robust_fetch.fetch_pdf_simple —— 在原 httpx 之上自动追加
curl_cffi（模拟 Chrome TLS 指纹）兜底，过掉一批「非浏览器指纹就拒」的 OA 站，且 magic bytes
严格校验（不再信任 Content-Type，挑战页常返 200+text/html 伪装）。preprint/meta 复用本函数，
一并受益。

W8 网络军规：外部 URL fetch 走后端进程网络环境，由进程启动 env 控制，此处不硬写。
"""
from __future__ import annotations

import logging

from .robust_fetch import fetch_pdf_simple

logger = logging.getLogger(__name__)


async def fetch_oa_pdf(oa_url: str) -> bytes | None:
    """拿 oa_url 的 PDF 字节（httpx → curl_cffi 两级），验证 magic bytes；任何失败返 None。

    参数：
        oa_url — 开放访问 PDF 直链（来自 Unpaywall / Semantic Scholar openAccessPdf）

    返回：
        bytes — PDF 原始字节（前 5 字节 = b'%PDF-'）
        None  — 任何失败（HTTP 错误 / 超时 / 非 PDF 内容）
    """
    pdf = await fetch_pdf_simple(oa_url)
    if pdf is None:
        logger.debug("oa_adapter: 未拿到 PDF，url=%s", oa_url)
    else:
        logger.debug("oa_adapter: 成功拿到 PDF，大小 %d 字节，来源 %s", len(pdf), oa_url)
    return pdf
