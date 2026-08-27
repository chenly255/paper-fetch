"""crossref_adapter：Crossref API 查出版商登记的全文链接（DOI → message.link[]）。

很多出版商按 TDM（文本数据挖掘）规范在 Crossref 登记了 application/pdf 全文链接，
这些链接 Unpaywall/OpenAlex 不一定收。免费、无需 key。

W8：走后端进程网络环境（直连），不设代理。
"""
from __future__ import annotations

import logging

from .proxy import async_client_for

from .cooldown_http import cooldown_get
from .robust_fetch import fetch_pdf_simple

logger = logging.getLogger(__name__)

_TIMEOUT_SEC = 12
_MAILTO = "paperpilot@example.com"


async def fetch_via_crossref(doi: str | None) -> bytes | None:
    """DOI → Crossref work → 挑 content-type 含 pdf 的 link 逐个尝试下载。"""
    d = (doi or "").strip()
    if not d:
        return None

    links = await _lookup_pdf_links(d)
    if not links:
        return None

    ref = f"https://doi.org/{d}"
    for url in links:
        pdf = await fetch_pdf_simple(url, referer=ref)
        if pdf is not None:
            logger.info("crossref: 命中 %s", url)
            return pdf
    return None


async def _lookup_pdf_links(doi: str) -> list[str]:
    url = f"https://api.crossref.org/works/{doi}"
    try:
        async with async_client_for(url, follow_redirects=True, timeout=_TIMEOUT_SEC) as client:
            resp = await cooldown_get(client, url, params={"mailto": _MAILTO})
            if resp is None or resp.status_code != 200:
                return []
            data = resp.json()
    except Exception as exc:
        logger.debug("crossref: 查询失败 doi=%s（%s）", doi, exc)
        return []

    msg = data.get("message") or {}
    out: list[str] = []
    for link in msg.get("link") or []:
        ct = (link.get("content-type") or "").lower()
        u = link.get("URL")
        if not u:
            continue
        # 明确 pdf 的优先；unspecified 的也试（部分出版商不标 content-type）
        if "pdf" in ct or ct in ("", "unspecified"):
            out.append(u)
    # 去重保序，pdf 明确的排前
    seen: set[str] = set()
    result: list[str] = []
    for u in out:
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result
