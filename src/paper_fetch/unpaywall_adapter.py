"""unpaywall_adapter：调 Unpaywall API 解析 OA PDF URL 后 fetch。

流程：
  GET https://api.unpaywall.org/v2/{doi}?email={email}
  → 解析 best_oa_location.url_for_pdf
  → 若为 None，尝试 oa_locations 数组里第一个有 url_for_pdf 的条目
  → 拿到 URL → 复用 oa_adapter.fetch_oa_pdf

W8 网络军规：外部 API 走 17891 代理，由进程启动 env 控制，此处不硬写。
"""
from __future__ import annotations

import logging

import httpx

from .proxy import async_client_for

from .cooldown_http import cooldown_get
from .oa_adapter import fetch_oa_pdf

logger = logging.getLogger(__name__)

_UNPAYWALL_API = "https://api.unpaywall.org/v2/{doi}"
_TIMEOUT_SEC = 15


async def fetch_via_unpaywall(doi: str, email: str) -> bytes | None:
    """调 Unpaywall API 解析 PDF URL，然后 fetch；失败返 None。

    参数：
        doi   — 论文 DOI（如 10.1038/s41587-020-0739-1）
        email — Unpaywall 免费 API 要求的邮箱参数

    返回：
        bytes — PDF 原始字节
        None  — 论文无 OA 版 / Unpaywall API 异常 / PDF fetch 失败
    """
    try:
        url = _UNPAYWALL_API.format(doi=doi)
        async with async_client_for(url, follow_redirects=True, timeout=_TIMEOUT_SEC) as client:
            resp = await cooldown_get(client, url, params={"email": email})
            if resp is None:
                return None
            resp.raise_for_status()

            data = resp.json()

            # is_oa=false → 直接返 None
            if not data.get("is_oa"):
                logger.debug("unpaywall_adapter: doi=%s 无 OA 版本", doi)
                return None

            # 找第一个有效的 url_for_pdf
            pdf_url = _extract_pdf_url(data)
            if not pdf_url:
                logger.debug("unpaywall_adapter: doi=%s 有 OA 但 url_for_pdf 均为空", doi)
                return None

            logger.debug("unpaywall_adapter: doi=%s 找到 PDF URL %s", doi, pdf_url)
            return await fetch_oa_pdf(pdf_url)

    except httpx.HTTPStatusError as exc:
        logger.debug("unpaywall_adapter: HTTP 错误 %s，doi=%s", exc.response.status_code, doi)
        return None
    except httpx.TimeoutException:
        logger.debug("unpaywall_adapter: 超时，doi=%s", doi)
        return None
    except httpx.RequestError as exc:
        logger.debug("unpaywall_adapter: 请求错误（%s），doi=%s", exc, doi)
        return None
    except Exception as exc:
        # REASON: 五段下载链一段，未知异常降级 None 让上层走下一档。
        logger.warning("unpaywall_adapter: 未知错误（%s），doi=%s", exc, doi, exc_info=True)
        return None


def _extract_pdf_url(data: dict) -> str | None:
    """从 Unpaywall 响应中提取第一个有效的 url_for_pdf。

    优先级：
    1. best_oa_location.url_for_pdf（非空）
    2. oa_locations 数组中第一个非空 url_for_pdf
    """
    best = data.get("best_oa_location") or {}
    url = best.get("url_for_pdf")
    if url:
        return url

    for loc in data.get("oa_locations") or []:
        url = loc.get("url_for_pdf")
        if url:
            return url

    return None
