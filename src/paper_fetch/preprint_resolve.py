"""预印本 DOI → 正式发表版 DOI 解析（从 PaperPilot paper_search.preprint_resolve 迁移）。

只迁下载链所需部分（PREPRINT_DOI_PREFIXES / doi_looks_like_preprint /
resolve_published_doi 三招）；enrich_published_versions（搜索候选批量增强，
依赖 PaperCandidate 模型）留在 PaperPilot 的 paper_search 包里。

★PREPRINT_DOI_PREFIXES 是跨模块共享的唯一定义（PaperPilot 评审 m2 收敛）：
paper-fetch 侧下载链（升级判断 / europe_pmc 守门）都从这里 import；
PaperPilot 侧 paper_search.preprint_resolve 对外 re-export 本模块的定义，
防止两份表漂移。

三招依次试：① Crossref relation.is-preprint-of（最准）② bioRxiv/medRxiv API
published 字段 ③ 按标题在 Crossref 反查（前两招没登记时兜底，调用务必传 title）。

守门（2026-08-21 DOI 误映射事故）：输入 DOI 不像预印本（前缀不在已知平台）直接
返 None，不查网络——正式版 DOI 送进标题反查只会张冠李戴。
"""
from __future__ import annotations

import logging
import re

from .proxy import async_client_for
from .text_match import title_match_score

logger = logging.getLogger(__name__)

_TIMEOUT_SEC = 10.0
_MAILTO = "paper-fetch@example.com"
# 标题反查认定阈值：预印本标题与候选正式版标题相似度 ≥ 此值才认是同一篇（防张冠李戴）
_TITLE_MATCH_MIN = 0.72

# 预印本 DOI 前缀 → 大多数预印本平台用固定 registrant 前缀。
PREPRINT_DOI_PREFIXES = (
    "10.1101/",    # bioRxiv / medRxiv（Cold Spring Harbor）
    "10.64898/",   # bioRxiv 2025 起启用的新前缀
    "10.21203/",   # Research Square
    "10.20944/",   # Preprints.org
    "10.2139/",    # SSRN
    "10.31234/",   # PsyArXiv
    "10.31219/",   # OSF Preprints
    "10.26434/",   # ChemRxiv
    "10.48550/",   # arXiv（DataCite）
)

# 更正/勘误/撤稿/评论类「通知」标题前缀——标题几乎复刻原文但只是几页通知，
# 反查正式版时按标题前缀剔除（冒号是关键：区分真通知与以这些词开头的正文）。
_NOTICE_TITLE_RE = re.compile(
    r"^\s*(?:"
    r"(?:publisher |author |editorial )?correction(?:s)?|"
    r"corrigendum|erratum|errata|"
    r"retraction(?: note)?|retracted(?: article)?|withdrawal|withdrawn|"
    r"expression of concern|editorial expression of concern|"
    r"comment|reply|response|addendum"
    r")(?:\s+(?:to|on|of|note|article|for))?\s*:",
    re.IGNORECASE,
)

# 会议摘要类标题前缀（2026-08-21 DOI 误映射事故）：「Abstract IA11: 原标题」形式的
# 墙报/口头摘要与论文标题几乎逐字相同，必须剔除（真论文标题不会是编号冒号格式）。
_ABSTRACT_TITLE_RE = re.compile(
    r"^\s*abstract\s*(?:[A-Za-z]{0,4}\d{0,5}(?:-\d{1,5})?)?\s*[:：]", re.IGNORECASE
)


def _is_notice_title(title: str) -> bool:
    """标题是否属更正/勘误/撤稿类通知或会议摘要（不是论文正文）。"""
    return bool(_NOTICE_TITLE_RE.match(title or "") or _ABSTRACT_TITLE_RE.match(title or ""))


def doi_looks_like_preprint(doi: str | None) -> bool:
    """DOI 是否像预印本 DOI（前缀启发式，纯本地判断、零网络）。"""
    doi = (doi or "").strip().lower()
    return bool(doi) and doi.startswith(PREPRINT_DOI_PREFIXES)


async def _published_doi_via_crossref(doi: str) -> str | None:
    """Crossref works/{doi} 的 relation.is-preprint-of → 正式版 DOI。"""
    url = f"https://api.crossref.org/works/{doi}"
    try:
        async with async_client_for(url, follow_redirects=True, timeout=_TIMEOUT_SEC) as client:
            resp = await client.get(url, params={"mailto": _MAILTO})
            if resp.status_code != 200:
                return None
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.debug("preprint_resolve: crossref 查询失败 doi=%s（%s）", doi, exc)
        return None

    relation = (data.get("message") or {}).get("relation") or {}
    for rel in relation.get("is-preprint-of") or []:
        if (rel.get("id-type") or "").lower() == "doi" and rel.get("id"):
            return str(rel["id"]).strip()
    return None


async def _published_doi_via_biorxiv(doi: str) -> str | None:
    """bioRxiv/medRxiv details API 的 collection[].published → 正式版 DOI（10.1101 专用兜底）。"""
    # 10.64898/ 新前缀是否被 api.biorxiv.org 支持未验证，只认 10.1101/ 防无效请求。
    if not doi.lower().startswith("10.1101/"):
        return None
    for server in ("biorxiv", "medrxiv"):
        url = f"https://api.biorxiv.org/details/{server}/{doi}"
        try:
            async with async_client_for(url, follow_redirects=True, timeout=_TIMEOUT_SEC) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    continue
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.debug("preprint_resolve: biorxiv 查询失败 doi=%s（%s）", doi, exc)
            continue
        for rec in data.get("collection") or []:
            pub = str(rec.get("published") or "").strip()
            if pub and pub.upper() != "NA" and "/" in pub:
                return pub
    return None


async def _published_doi_via_title_search(title: str, preprint_doi: str) -> str | None:
    """按标题在 Crossref 反查正式期刊版（预印本元数据没登记正式版时的兜底）。"""
    t = (title or "").strip()
    if len(t) < 15:  # 标题太短，反查噪声大，不做
        return None
    url = "https://api.crossref.org/works"
    params = {
        "query.bibliographic": t,
        "rows": "5",
        "select": "DOI,title,type",
        "mailto": _MAILTO,
    }
    try:
        async with async_client_for(url, follow_redirects=True, timeout=_TIMEOUT_SEC) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                return None
            items = (resp.json().get("message") or {}).get("items") or []
    except Exception as exc:  # noqa: BLE001
        logger.debug("preprint_resolve: crossref 标题反查失败（%s）", exc)
        return None

    pre = preprint_doi.strip().lower()
    best_doi: str | None = None
    best_score = _TITLE_MATCH_MIN
    for it in items:
        cand_doi = str(it.get("DOI") or "").strip()
        if not cand_doi or cand_doi.lower() == pre:
            continue
        # 只认正式期刊论文；会议摘要/又一个预印本/book chapter 一律排除（事故整改）。
        if (it.get("type") or "").lower() != "journal-article":
            continue
        cand_title = " ".join(it.get("title") or [])
        if _is_notice_title(cand_title):
            continue  # 更正/撤稿声明/会议摘要（「Abstract IA11: 原标题」），跳过
        score = title_match_score(t, cand_title)
        if score >= best_score:
            best_score = score
            best_doi = cand_doi
    return best_doi


async def resolve_published_doi(doi: str | None, title: str | None = None) -> str | None:
    """预印本 DOI → 正式发表版 DOI；三招依次试，全失败 / 没正式版 / 不像预印本返 None。"""
    doi = (doi or "").strip()
    if not doi:
        return None
    if not doi_looks_like_preprint(doi):
        logger.debug(
            "preprint_resolve: DOI %s 不像预印本前缀，跳过正式版解析（守门）", doi,
        )
        return None
    published: str | None = None
    published = await _published_doi_via_crossref(doi)
    if not published:
        published = await _published_doi_via_biorxiv(doi)
    if not published and title:
        published = await _published_doi_via_title_search(title, doi)
    if published and published.strip().lower() != doi.lower():
        return published.strip()
    return None
