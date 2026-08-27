"""openalex_adapter：OpenAlex API 查 OA PDF 直链（DOI → open_access / locations[].pdf_url）。

为什么加它：OpenAlex 是覆盖面最大的开放学术索引，常比 Unpaywall 多收录 OA 版本与
preprint 镜像（实测：有些论文 Unpaywall 返 None，OpenAlex 仍给出 oa_url）。免费、无需 key。

W8：走后端进程网络环境（直连），不设代理。
"""
from __future__ import annotations

import logging

from .proxy import async_client_for

from .cooldown_http import cooldown_get
from .robust_fetch import fetch_pdf_simple

logger = logging.getLogger(__name__)

_TIMEOUT_SEC = 12
# 礼貌参数：OpenAlex 建议带 mailto 进 polite pool（更稳定的限速）
_MAILTO = "paperpilot@example.com"


async def probe_oa(doi: str | None) -> tuple[bool | None, list[str], bool]:
    """查 OpenAlex 一次，返回 (is_oa, pdf_urls, not_found)。

    is_oa：True/False = OpenAlex 明确收录的开放状态；None = 没查到/查询失败（未知，别据此短路）。
    pdf_urls：可下载的 OA PDF 直链候选（保序去重）。
    not_found：True 仅当 OpenAlex 明确 404（未收录该 DOI）——调用方可据此复核 DOI 存在性。

    download_pdf 用它做**早期短路**：明确 is_oa=False 的付费论文，跳过后面所有昂贵的浏览器/重复抓取，
    直接判定需机构访问——省掉对付费论文 ~50 秒的无用功。
    """
    d = (doi or "").strip()
    if not d:
        return None, [], False
    url = f"https://api.openalex.org/works/doi:{d}"
    try:
        async with async_client_for(url, follow_redirects=True, timeout=_TIMEOUT_SEC) as client:
            resp = await cooldown_get(client, url, params={"mailto": _MAILTO})
            if resp is None:
                return None, [], False
            if resp.status_code == 404:
                return None, [], True
            if resp.status_code != 200:
                return None, [], False
            data = resp.json()
    except Exception as exc:
        logger.debug("openalex: 查询失败 doi=%s（%s）", d, exc)
        return None, [], False

    oa = data.get("open_access") or {}
    is_oa = oa.get("is_oa")
    if not isinstance(is_oa, bool):
        is_oa = None

    out: list[str] = []
    if oa.get("oa_url"):
        out.append(oa["oa_url"])
    primary = data.get("primary_location") or {}
    if primary.get("pdf_url"):
        out.append(primary["pdf_url"])
    for loc in data.get("locations") or []:
        if loc and loc.get("pdf_url"):
            out.append(loc["pdf_url"])
    best = data.get("best_oa_location") or {}
    if best.get("pdf_url"):
        out.append(best["pdf_url"])

    seen: set[str] = set()
    pdf_urls: list[str] = []
    for u in out:
        if u and u not in seen:
            seen.add(u)
            pdf_urls.append(u)
    return is_oa, pdf_urls, False


async def fetch_via_openalex(doi: str | None) -> bytes | None:
    """DOI → OpenAlex 的 OA pdf_url，逐个走快路下载（浏览器预算留给专用段）。"""
    d = (doi or "").strip()
    if not d:
        return None
    _is_oa, pdf_urls, _not_found = await probe_oa(d)
    if not pdf_urls:
        return None
    ref = f"https://doi.org/{d}"
    for url in pdf_urls:
        pdf = await fetch_pdf_simple(url, referer=ref)
        if pdf is not None:
            logger.info("openalex: 命中 %s", url)
            return pdf
    return None
